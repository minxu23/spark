"""
用数据线（或同一局域网里配对过的 iPhone）跟手机客户端同步，不经过 GitHub。

电脑 → 手机：笔记库里的 Spark/ 和 output/（不含点开头的隐藏文件夹和 logs/，跟 GitHub 同步一样），
直接拷进手机 App 自己的那份笔记库（App 容器里的 Library/Application Support/vault），
同时附一份文件清单 .spark-cable.json：每个文件的 git blob sha，和 GitHub 上同一个文件的
sha 一样，所以手机之后再从 GitHub 同步时，这些文件都算已经下载过。
手机 → 电脑：手机上添加的任务先放在发件箱（App Group 容器里的 outbox/，主 App 和分享扩展
都往里写），同步时拉回来，按 core/remote_tasks 同一套校验和接口在后台跑；结果写进
.spark-cable-tasks.json，下次同步带回手机。

传输用 Xcode 自带的 devicectl（真机，只拷有变化的文件）；模拟器直接写它在本机的容器目录，
方便开发时测。手机上的 App 得是从 Xcode 装的开发版，devicectl 才能读写它的容器。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from core import atomic
from core import readlog
from core import remote_tasks
from core import settings as core_settings
from core import vault

BUNDLE_ID = "com.minxu.spark"
GROUP_ID = "group.com.minxu.spark"
DEST = "Library/Application Support/vault"     # App 容器里手机那份笔记库
OUTBOX = "outbox"                              # App Group 容器里的发件箱
READLOG = "readlog"                            # App Group 容器里手机的阅读记录（复盘用，见 core/readlog.py）
MANIFEST = ".spark-cable.json"
RESULTS = ".spark-cable-tasks.json"
SYNC_DIRS = (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME)
SKIP_DIRS = {".cache", "logs"}
SKIP_SUFFIXES = (".log", ".tmp", ".partial")
CMD_TIMEOUT = 60 * 60
BUSY_RETRY_SECONDS = 120

LocalCall = Callable[[str, str, Optional[dict]], "tuple[int, Any]"]


class CableError(RuntimeError):
    pass


def _spark_home() -> str:
    """跟设置文件放一起；测试用 SPARK_SETTINGS_FILE 指到临时目录时它也跟着走。"""
    return os.path.dirname(core_settings.settings_path())


def _run(cmd: list[str], timeout: float = CMD_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise CableError("找不到 xcrun：要先装 Xcode") from e
    except subprocess.TimeoutExpired as e:
        raise CableError(f"{cmd[1] if len(cmd) > 1 else cmd[0]} 超过 {int(timeout // 60)} 分钟没完成") from e
    return p


# ---------------------------------------------------------------- 设备

def list_devices() -> list[dict]:
    """能同步的 iPhone：配对过、现在连着（数据线或同一局域网）的真机，以及开着的模拟器。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "devices.json")
        p = _run(["xcrun", "devicectl", "list", "devices", "--json-output", out], timeout=60)
        if p.returncode != 0 or not os.path.exists(out):
            raise CableError(f"列不出设备：{(p.stderr or p.stdout).strip()[-300:]}")
        with open(out, encoding="utf-8") as f:
            raw = json.load(f)
    devices = []
    for d in (raw.get("result") or {}).get("devices") or []:
        hw, props, conn = d.get("hardwareProperties") or {}, d.get("deviceProperties") or {}, d.get("connectionProperties") or {}
        if hw.get("platform") != "iOS" or hw.get("deviceType") not in (None, "iPhone"):
            continue
        simulated = hw.get("reality") == "simulated"
        if simulated:
            if props.get("bootState") != "booted":
                continue
            transport = "simulator"
        else:
            transport = conn.get("transportType") or ""
            if conn.get("pairingState") != "paired" or transport not in ("wired", "localNetwork"):
                continue
        devices.append({
            "id": d.get("identifier") or hw.get("udid"),
            "udid": hw.get("udid") or d.get("identifier"),
            "name": props.get("name") or hw.get("marketingName") or "iPhone",
            "model": hw.get("marketingName") or "",
            "transport": transport,       # wired 数据线 / localNetwork 局域网 / simulator 模拟器
        })
    order = {"wired": 0, "localNetwork": 1, "simulator": 2}
    return sorted(devices, key=lambda x: order.get(x["transport"], 9))


class Transport:
    """把文件夹推到手机 / 从手机拉回来。真机走 devicectl，模拟器直接写容器目录。"""

    def __init__(self, device: dict):
        self.device = device

    def _sim_container(self, group: bool) -> str:
        p = _run(["xcrun", "simctl", "get_app_container", self.device["udid"], BUNDLE_ID,
                  GROUP_ID if group else "data"], timeout=60)
        if p.returncode != 0:
            raise CableError("模拟器上没装 Spark 手机客户端")
        return p.stdout.strip()

    def push(self, local_dir: str, dest: str) -> None:
        """local_dir 里的内容拷到 App 容器的 dest 下（已有的同名文件覆盖，不删多出来的）。"""
        if self.device["transport"] == "simulator":
            target = os.path.join(self._sim_container(False), dest)
            os.makedirs(target, exist_ok=True)
            shutil.copytree(local_dir, target, dirs_exist_ok=True, copy_function=shutil.copy2)
            return
        p = _run(["xcrun", "devicectl", "device", "copy", "to", "--device", self.device["id"],
                  "--source", local_dir, "--destination", dest,
                  "--domain-type", "appDataContainer", "--domain-identifier", BUNDLE_ID])
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip()
            if "appDataContainer" in msg or "bundle" in msg.lower():
                raise CableError("手机上没有从 Xcode 装的 Spark 开发版，拷不进去")
            raise CableError(f"拷到手机失败：{msg[-300:]}")

    def pull_outbox(self, local_dir: str) -> None:
        """手机发件箱（App Group 容器里的 outbox/）拉到 local_dir。没有发件箱就什么都不拉。"""
        self.pull_group_dir(OUTBOX, local_dir)

    def pull_group_dir(self, name: str, local_dir: str) -> None:
        """App Group 容器里的一个文件夹（发件箱、阅读记录）拉到 local_dir。不存在就什么都不拉。"""
        if self.device["transport"] == "simulator":
            src = os.path.join(self._sim_container(True), name)
            if os.path.isdir(src):
                shutil.copytree(src, local_dir, dirs_exist_ok=True)
            return
        p = _run(["xcrun", "devicectl", "device", "copy", "from", "--device", self.device["id"],
                  "--source", name, "--destination", local_dir,
                  "--domain-type", "appGroupDataContainer", "--domain-identifier", GROUP_ID])
        if p.returncode != 0 and "7000" not in (p.stderr + p.stdout):   # 7000：文件夹不存在，正常
            raise CableError(f"读手机上的 {name}/ 失败：{(p.stderr or p.stdout).strip()[-300:]}")


# ---------------------------------------------------------------- 打包

def _skip(name: str) -> bool:
    return name == ".DS_Store" or name.endswith(SKIP_SUFFIXES)


def blob_sha(path: str) -> str:
    """跟 git / GitHub 一样的 blob sha：sha1("blob <大小>\\0" + 内容)。"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % os.path.getsize(path))
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage(root: str, staging: str, sha_cache: dict) -> dict[str, list]:
    """把要同步的文件硬链接到 staging（同一个盘上不占空间，修改时间也跟原文件一样，
    devicectl 据此跳过没变的），返回 {相对路径: [sha, 大小]}。staging 里多出来的旧文件删掉。"""
    files: dict[str, list] = {}
    keep: set[str] = set()
    for top in SYNC_DIRS:
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # 点开头的文件夹（.cache、整理时留的备份）都不同步；.manifest.json 这类点开头的文件照常同步
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if _skip(name):
                    continue
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, root).replace(os.sep, "/")
                st = os.stat(src)
                key = f"{st.st_size}:{st.st_mtime_ns}"
                cached = sha_cache.get(rel)
                sha = cached[1] if cached and cached[0] == key else blob_sha(src)
                sha_cache[rel] = [key, sha]
                files[rel] = [sha, st.st_size]
                keep.add(rel)
                dst = os.path.join(staging, rel)
                try:
                    dst_st = os.stat(dst)
                    if dst_st.st_ino == st.st_ino:
                        continue
                    os.remove(dst)
                except FileNotFoundError:
                    pass
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
    for dirpath, _dirs, filenames in os.walk(staging):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), staging).replace(os.sep, "/")
            if rel not in keep and rel not in (MANIFEST, RESULTS):
                os.remove(os.path.join(dirpath, name))
    for k in [k for k in sha_cache if k not in keep]:
        del sha_cache[k]
    return files


# ---------------------------------------------------------------- 手机发来的任务

class TaskBook:
    """手机发件箱里的任务：收到过哪些、各自什么状态。存在 ~/.spark/cable-tasks.json。"""

    def __init__(self):
        self.path = os.path.join(_spark_home(), "cable-tasks.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.tasks: dict[str, dict] = json.load(f)
        except (OSError, ValueError):
            self.tasks = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic.write_json(self.path, self.tasks, indent=2)

    def receive(self, folder: str) -> int:
        """读拉回来的发件箱，新任务记成「等电脑接手」，不合规的直接记失败。返回新收到几个。"""
        new = 0
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    item = json.load(f)
            except (OSError, ValueError):
                continue
            tid = str(item.get("id") or "")[:64]
            if not tid or not tid.replace("-", "").isalnum():
                continue
            with self.lock:
                if tid in self.tasks:
                    continue
                entry = {"title": str(item.get("title") or "")[:200], "received_at": time.time(),
                         "updated_at": time.time()}
                try:
                    entry["task"] = remote_tasks.parse_task(item.get("task"))
                    entry.update(state="waiting", text="等电脑接手")
                except remote_tasks.TaskError as e:
                    entry.update(state="failed", text=f"任务内容有问题：{e}")
                self.tasks[tid] = entry
                new += 1
        if new:
            with self.lock:
                self.save()
        return new

    def update(self, tid: str, state: str, text: str, **extra) -> None:
        with self.lock:
            self.tasks[tid].update(state=state, text=text, updated_at=time.time(), **extra)
            self.save()

    def results(self) -> dict:
        """带回手机的：每个任务的状态和说明（不带任务参数）。"""
        with self.lock:
            return {"generated_at": time.time(), "tasks": {
                tid: {k: t.get(k) for k in ("title", "state", "text", "updated_at", "current", "total")}
                for tid, t in self.tasks.items()}}

    def waiting(self) -> list[str]:
        with self.lock:
            return [tid for tid, t in self.tasks.items() if t.get("state") == "waiting"]


class _LocalProgress:
    """remote_tasks.Runner 要的进度对象：写进任务记录，不写 GitHub。"""

    def __init__(self, book: TaskBook, tid: str):
        self.book, self.tid = book, tid
        self.comment_id = 0

    def update(self, text: str, *, force: bool = False, **extra) -> None:
        self.book.update(self.tid, "running", text, **{k: v for k, v in extra.items() if k in ("current", "total")})


def _merge_readlog(folder: str) -> int:
    """拉回来的手机阅读记录（一行一条 JSON）并进电脑这边的阅读记录，返回新增几条。"""
    events = []
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if name.endswith(".jsonl"):
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
    return readlog.merge(events)


# ---------------------------------------------------------------- 一次同步

_STATE: dict = {"running": False, "phase": "", "last": {}, "worker": False}
_LOCK = threading.Lock()
_CALL: dict = {"fn": None}


def configure(call: LocalCall) -> None:
    """spark.py 启动时把「在本进程里调 Spark 自己的接口」的函数交进来，跑手机发来的任务用。"""
    _CALL["fn"] = call


_BOOK: dict = {"book": None}


def _book() -> TaskBook:
    """全进程一份：后台跑任务和新一次同步收任务改的是同一份记录，不会互相覆盖。"""
    with _LOCK:
        b = _BOOK["book"]
        if b is None or b.path != os.path.join(_spark_home(), "cable-tasks.json"):
            b = _BOOK["book"] = TaskBook()
        return b


def sync_device(device: dict, *, transport: Optional[Transport] = None) -> dict:
    """跟一台手机同步一次（阻塞）：拉发件箱 → 打包 → 推过去。"""
    tr = transport or Transport(device)
    root = vault.vault_root()
    if not any(os.path.isdir(os.path.join(root, d)) for d in SYNC_DIRS):
        raise CableError(f"笔记库里没有 {' / '.join(SYNC_DIRS)}：{root}")
    home = _spark_home()
    book = _book()

    _STATE["phase"] = "读手机上添加的任务和阅读记录"
    with tempfile.TemporaryDirectory() as tmp:
        tr.pull_outbox(tmp)
        received = book.receive(tmp)
    reads = 0
    if hasattr(tr, "pull_group_dir"):
        with tempfile.TemporaryDirectory() as tmp:
            tr.pull_group_dir(READLOG, tmp)
            reads = _merge_readlog(tmp)

    _STATE["phase"] = "整理要同步的文件"
    staging = os.path.join(home, "cable-staging", "vault")
    os.makedirs(staging, exist_ok=True)
    cache_path = os.path.join(home, "cable-sha-cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            sha_cache = json.load(f)
    except (OSError, ValueError):
        sha_cache = {}
    files = stage(root, staging, sha_cache)
    atomic.write_json(cache_path, sha_cache)
    now = datetime.now().astimezone().isoformat(timespec="seconds")   # 2026-11-02T10:05:00+08:00，手机端按 ISO 8601 读
    atomic.write_json(os.path.join(staging, MANIFEST),
                      {"version": 1, "generated_at": now, "device": device.get("name"), "files": files})
    atomic.write_json(os.path.join(staging, RESULTS), book.results())

    _STATE["phase"] = f"拷到「{device.get('name')}」"
    tr.push(staging, DEST)
    _start_worker(device)
    return {"ok": True, "device": device.get("name"), "files": len(files), "reads_received": reads,
            "bytes": sum(s for _sha, s in files.values()), "tasks_received": received,
            "tasks_waiting": len(book.waiting())}


def _start_worker(device: dict) -> None:
    """后台把等着的手机任务一个个跑完；跑完一个，如果手机还连着就把结果推回去。"""
    if _CALL["fn"] is None:
        return
    with _LOCK:
        if _STATE["worker"]:
            return
        _STATE["worker"] = True

    def work() -> None:
        try:
            book = _book()
            while True:
                pending = book.waiting()
                if not pending:
                    return
                for tid in pending:
                    task = book.tasks[tid]["task"]
                    book.update(tid, "running", "电脑开始处理")
                    try:
                        text = remote_tasks.Runner(_CALL["fn"], _LocalProgress(book, tid)).run(task)
                        book.update(tid, "done", f"完成：{text}")
                    except remote_tasks.Busy:
                        book.update(tid, "waiting", "电脑上正在跑别的任务，等它结束再开始")
                        time.sleep(BUSY_RETRY_SECONDS)
                    except remote_tasks.TaskError as e:
                        book.update(tid, "failed", f"没成功：{e}")
                    except Exception as e:  # noqa: BLE001
                        book.update(tid, "failed", f"没成功：意外错误：{e}")
                    _push_results(device, book)
        finally:
            with _LOCK:
                _STATE["worker"] = False

    threading.Thread(target=work, daemon=True, name="spark-cable-tasks").start()


def _push_results(device: dict, book: TaskBook) -> None:
    """只推任务结果那一个文件；手机断开了就算了，下次同步再带回去。"""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            atomic.write_json(os.path.join(tmp, RESULTS), book.results())
            Transport(device).push(tmp, DEST)
    except CableError:
        pass


def start(device_id: str) -> str:
    """在后台同步一次。返回不能开始的原因，能开始返回空串。"""
    with _LOCK:
        if _STATE["running"]:
            return "正在同步，等这一次结束"
    try:
        device = next((d for d in list_devices() if d["id"] == device_id), None)
    except CableError as e:
        return str(e)
    if not device:
        return "这台设备现在没连着（插上数据线，或者跟电脑在同一个 Wi-Fi 下并解锁）"
    with _LOCK:
        if _STATE["running"]:
            return "正在同步，等这一次结束"
        _STATE["running"] = True
        _STATE["phase"] = "开始"

    def run() -> None:
        try:
            result = sync_device(device)
        except CableError as e:
            result = {"ok": False, "device": device.get("name"), "error": str(e)}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "device": device.get("name"), "error": f"意外错误：{e}"}
        result["finished_at"] = time.time()
        with _LOCK:
            _STATE.update(running=False, phase="", last=result)

    threading.Thread(target=run, daemon=True, name="spark-cable-sync").start()
    return ""


def status() -> dict:
    with _LOCK:
        return {"running": _STATE["running"], "phase": _STATE["phase"], "last": dict(_STATE["last"]),
                "tasks_running": _STATE["worker"]}
