"""
用数据线（或同一局域网里配对过的 iPhone）跟手机客户端同步，不经过 GitHub。

电脑 → 手机：笔记库里的 Spark/ 和 output/（不含点开头的隐藏文件夹和 logs/，跟 GitHub 同步一样），
直接拷进手机 App 自己的那份笔记库（App 容器里的 Library/Application Support/vault），
同时附一份文件清单 .spark-cable.json：每个文件的 git blob sha，和 GitHub 上同一个文件的
sha 一样，所以手机之后再从 GitHub 同步时，这些文件都算已经下载过。
手机 → 电脑：手机上添加的任务先放在发件箱（App Group 容器里的 outbox/，主 App 和分享扩展
都往里写），同步时拉回来，按 core/remote_tasks 同一套校验和接口在后台跑；结果写进
.spark-cable-tasks.json，下次同步带回手机。

电脑上的 Spark 开着时，用数据线插上手机就自动同步一次，一直插着每 10 分钟再同步一次（auto_tick）；
同一 Wi-Fi 下的手机要在设置页手动点。

传输用 Xcode 自带的 devicectl（真机，只拷有变化的文件）；模拟器直接写它在本机的容器目录，
方便开发时测。手机上的 App 得是从 Xcode 装的开发版，devicectl 才能读写它的容器。

模拟器（iOS 模拟器、鸿蒙模拟器）默认不认：不列出来，也不同步。模拟器里是测试数据，混进真实笔记库就清不干净了
（2026-11-23 一次手动同步就这样把模拟器的测试剪藏和阅读记录带进了笔记库）。开发时要测，
启动 Spark 前设环境变量 SPARK_CABLE_SIMULATORS=1（值去掉首尾空白后等于 1 才算开，true、01 都不算）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from core import atomic
from core import clips
from core import readlog
from core import remote_tasks
from core import settings as core_settings
from core import vault

BUNDLE_ID = "com.minxu.spark"
GROUP_ID = "group.com.minxu.spark"
DEST = "Library/Application Support/vault"     # App 容器里手机那份笔记库
OUTBOX = "Library/Spark/outbox"                # App Group 容器里的发件箱
READLOG_COPY = ".spark-readlog.jsonl"            # 带给手机的电脑阅读记录（放在手机那份笔记库根目录）
READLOG = "Library/Spark/readlog"              # App Group 容器里手机的阅读记录（复盘用，见 core/readlog.py）
# 真机上 devicectl 只让读 App Group 的 Library/。旧版 App 放在容器根目录（outbox、readlog），模拟器上还能拉到
LEGACY_DIRS = {OUTBOX: "outbox", READLOG: "readlog"}
PREFS = "prefs.json"                           # 同在 readlog/ 下：手机的阅读设置（几台设备之间同步）
PREFS_COPY = ".spark-prefs.json"               # 带给手机的：各设备里最后改的那份阅读设置
SUBS_COPY = ".spark-subscriptions.json"        # 带给手机的：订阅列表，手机自己检查信息跟进（FeedRunner.swift）
MANIFEST = ".spark-cable.json"
RESULTS = ".spark-cable-tasks.json"
SYNC_DIRS = (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME)
SKIP_DIRS = {".cache", "logs"}
SKIP_SUFFIXES = (".log", ".tmp", ".partial")
CMD_TIMEOUT = 60 * 60
BUSY_RETRY_SECONDS = 120
PROGRESS_PUSH_SECONDS = 30      # 任务跑着时多久把进度推给手机一次
PUSH_WAIT_SECONDS = 30          # 任务结束时最多等进度推送这么久
# 带回手机、显示在进度卡片上的字段
PROGRESS_KEYS = ("title", "state", "text", "updated_at", "started_at", "current", "total", "step", "item", "subject")
AUTO_POLL_SECONDS = 20          # 多久看一次有没有插着数据线的手机
AUTO_RESYNC_SECONDS = 10 * 60   # 一直插着：隔这么久整个同步一次（笔记库拷到手机）
SYNC_AFTER_TASKS_TRIES = 30     # 任务做完要整个同步一次：正在同步别的就等一会儿再试，最多试这么多次
SYNC_AFTER_TASKS_WAIT = 10
AUTO_CHECK_SECONDS = 60         # 一直插着：隔这么久看一眼手机发件箱，手机上新加的任务、剪藏、复盘不用等 10 分钟

LocalCall = Callable[[str, str, Optional[dict]], "tuple[int, Any]"]


class CableError(RuntimeError):
    pass


SIMULATORS_ENV = "SPARK_CABLE_SIMULATORS"
SIMULATOR_REFUSED = f"模拟器默认不同步（里面是测试数据）。开发时要测，设环境变量 {SIMULATORS_ENV}=1 再启动 Spark"


def simulators_allowed() -> bool:
    """开发开关：环境变量 SPARK_CABLE_SIMULATORS 的值去掉首尾空白后等于 1，才列出、同步模拟器。
    "true"、"01"、"0"、空串都不算开。每次现读，不缓存。"""
    return os.environ.get(SIMULATORS_ENV, "").strip() == "1"


def is_loopback_udid(udid: object) -> bool:
    """鸿蒙设备的 udid 是不是指向本机（鸿蒙模拟器）：「主机:端口」里的主机是 localhost（不分大小写），
    或者落在 127.0.0.0/8 里（127.0.0.2、缩写的 127.1 都算）。只给鸿蒙用：hdc 的序列号才会是这种形式。
    192.168.x.x:5555 这类无线调试的真机、普通序列号都不算。"""
    text = str(udid or "").strip()
    host, sep, _port = text.rpartition(":")
    if not sep or not host:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.lower() == "localhost":
        return True
    try:
        # inet_aton 认 127.1 这种缩写；只接受纯数字和点，免得把别的写法也当成地址
        if not re.fullmatch(r"[0-9.]+", host):
            return ipaddress.ip_address(host).is_loopback
        return socket.inet_aton(host)[0] == 127
    except (OSError, ValueError):
        return False


def is_simulator(device: Optional[dict]) -> bool:
    """是不是模拟器：transport 写着 simulator，或者是 udid 指向本机的鸿蒙设备（鸿蒙模拟器，见 is_loopback_udid），
    不管 transport 写的是什么。列设备、同步前的检查、读写模拟器容器都用这一个判断。
    iOS 设备只看 transport，不按 udid 算。"""
    d = device or {}
    if d.get("transport") == "simulator":
        return True
    return d.get("platform") == "HarmonyOS" and is_loopback_udid(d.get("udid"))


def _refuse_simulator(device: Optional[dict]) -> None:
    """第二道闸：设备信息可能是调用方直接传进来的，不经过 list_devices，这里再拦一次。"""
    if is_simulator(device) and not simulators_allowed():
        raise CableError(SIMULATOR_REFUSED)


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

def _ios_devices() -> list[dict]:
    """能同步的 iPhone、iPad：配对过、现在连着（数据线或同一局域网）的真机；开发开关打开时还有开着的模拟器。"""
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
        if hw.get("platform") != "iOS" or hw.get("deviceType") not in (None, "iPhone", "iPad"):
            continue
        simulated = hw.get("reality") == "simulated"
        if simulated:
            if not simulators_allowed() or props.get("bootState") != "booted":
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


def list_devices() -> list[dict]:
    from core import taihe_sync
    problem = None
    try:
        devices = _ios_devices()
    except CableError as error:
        devices, problem = [], error
    harmony = taihe_sync.list_devices()
    if problem and not harmony:
        raise problem
    return devices + harmony


class Transport:
    """把文件夹推到手机 / 从手机拉回来。真机走 devicectl，模拟器直接写容器目录。"""

    def __init__(self, device: dict):
        self.device = device

    def _sim_container(self, group: bool) -> str:
        _refuse_simulator(self.device)   # 推、拉模拟器都经过这里
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
            for n in (name, LEGACY_DIRS.get(name)):
                src = os.path.join(self._sim_container(True), *(n or "").split("/")) if n else ""
                if src and os.path.isdir(src):
                    shutil.copytree(src, local_dir, dirs_exist_ok=True)
            return
        p = _run(["xcrun", "devicectl", "device", "copy", "from", "--device", self.device["id"],
                  "--source", name, "--destination", local_dir,
                  "--domain-type", "appGroupDataContainer", "--domain-identifier", GROUP_ID])
        out = p.stderr + p.stdout
        # 7000：文件夹不存在，正常。但读不了（不在允许的目录里）也报 7000，那是路径放错了，要说出来
        if p.returncode != 0 and ("7000" not in out or "outside the directories" in out):
            raise CableError(f"读手机上的 {name}/ 失败：{(p.stderr or p.stdout).strip()[-300:]}")


# ---------------------------------------------------------------- 打包

def _skip(name: str) -> bool:
    # .edits.json：剪藏收下的手机修改记录，只有电脑自己用；.bad-：挪开的坏记录（core/clips.py）
    return name in (".DS_Store", ".edits.json") or name.endswith(SKIP_SUFFIXES) or ".bad-" in name


def deleted_versions(root: str, files: dict) -> dict[str, list[str]]:
    """带给手机的墓碑：{路径: [删掉的各版内容的 SHA-256]}，只放还有墓碑、这次清单里也没有的路径（数量很小）。
    手机上新建、在别的设备上删掉的一篇，手机自己一直等不到「电脑上有了」：手机上那份的指纹在这里，就认定是同一份、
    已经删了（Vault.elsewhereDeleted）。旧版手机不认这一项，照旧。"""
    out: dict[str, list[str]] = {}
    for rel, entries in clips.load_edits(root).items():
        if rel in files:
            continue
        shas = sorted({h for e in entries if isinstance(e.get("deleted"), list) for h in e["deleted"] if isinstance(h, str)})
        if shas:
            out[rel] = shas
    return out


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
                # 带上 inode：整篇重写（原子替换）总是新的 inode，修改时间照旧、大小碰巧一样（tools/migrate_times.py
                # 只换链接里的日期）也会重算指纹
                key = f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}"
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
            if rel not in keep and rel != RESULTS:   # 清单不放在这里：笔记库拷完了再单独推（见 sync_device）
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
        # 还记着「正在跑」的是上次 Spark 退出时断掉的：重新排队，已经做完的那些条会跳过
        for t in self.tasks.values():
            if t.get("state") == "running":
                t.update(state="waiting", text="Spark 重启过，下次同步时接着跑")

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic.write_json(self.path, self.tasks, indent=2)

    def receive(self, folder: str) -> int:
        """读拉回来的发件箱，新任务记成「等电脑接手」，不合规的直接记失败。返回新收到几个。"""
        new = 0
        items = []
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    item = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(item, dict):
                items.append(item)
        # 按手机上改的先后收（edited_at，没有再按放进发件箱的时间），不按文件名（文件名是随机的 UUID）
        for item in sorted(items, key=_item_order):
            tid = str(item.get("id") or "")[:64]
            if not tid or not tid.replace("-", "").isalnum():
                continue
            with self.lock:
                if tid in self.tasks:
                    continue
                entry = {"title": str(item.get("title") or "")[:200], "received_at": time.time(),
                         "updated_at": time.time(), "created_at": str(item.get("created_at") or "")[:40]}
                try:
                    entry["task"] = remote_tasks.parse_task(item.get("task"))
                    entry.update(state="waiting", text="等电脑接手")
                except remote_tasks.TaskError as e:
                    entry.update(state="failed", text=f"任务内容有问题：{e}", kind="rejected")
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
                # type：手机分得清哪些是要电脑做的任务、哪些只是把手机上做好的内容同步过来；
                # applied_sha：存进笔记库的那一版的 blob sha，手机拿它认出自己的修改到了（电脑可能接回了时间行，跟手机那份不一样）
                tid: {k: t.get(k) for k in PROGRESS_KEYS} | {"type": (t.get("task") or {}).get("type")}
                | ({"applied_sha": t["applied_sha"]} if t.get("applied_sha") else {})
                # kind：失败的是哪种（conflict 两边都改过 / gone 电脑上没有了 / rejected 电脑收不了），手机按它说清楚
                | ({"kind": t.get("kind") or "rejected"} if t.get("state") == "failed" else {})
                for tid, t in self.tasks.items()}}

    def waiting(self) -> list[str]:
        """等着的任务，按手机上改的先后（同一篇的修改、删除不会颠倒）。"""
        with self.lock:
            rows = [(tid, t) for tid, t in self.tasks.items() if t.get("state") == "waiting"]
        return [tid for tid, _t in sorted(rows, key=lambda r: _entry_order(r[0], r[1]))]


def _created_ms(created: str) -> int:
    try:
        return int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _item_order(item: dict) -> tuple:
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    at = task.get("edited_at") if isinstance(task.get("edited_at"), int) else _created_ms(item.get("created_at") or "")
    return (at, str(item.get("created_at") or ""), str(item.get("id") or ""))


def _entry_order(tid: str, t: dict) -> tuple:
    task = t.get("task") or {}
    at = task.get("edited_at") if isinstance(task.get("edited_at"), int) else _created_ms(t.get("created_at") or "")
    return (at or int(float(t.get("received_at") or 0) * 1000), t.get("created_at") or "", tid)


class _LocalProgress:
    """remote_tasks.Runner 要的进度对象：写进任务记录，不写 GitHub。
    跑的过程中隔 PROGRESS_PUSH_SECONDS 把进度推给手机（还连着的话），手机上能看到跑到第几条了。"""

    def __init__(self, book: TaskBook, tid: str, push: Optional[Callable[[], None]] = None):
        self.book, self.tid, self.push = book, tid, push
        self.comment_id = 0
        self._pushed = 0.0
        self._pushing = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def update(self, text: str, *, force: bool = False, **extra) -> None:
        self.book.update(self.tid, "running", text, **{k: v for k, v in extra.items() if k in PROGRESS_KEYS})
        now = time.time()
        if self.push is None or now - self._pushed < PROGRESS_PUSH_SECONDS or not self._pushing.acquire(blocking=False):
            return
        self._pushed = now

        def run() -> None:
            try:
                self.push()
            finally:
                self._pushing.release()

        self._thread = threading.Thread(target=run, daemon=True, name="spark-cable-progress")
        self._thread.start()

    def settle(self) -> None:
        """等还在路上的进度推送走完，结束时那次推送才是最后一次。手机断了就不等了。"""
        if self._thread:
            self._thread.join(timeout=PUSH_WAIT_SECONDS)


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


def export_subscriptions(root: str) -> list[dict]:
    """给手机的订阅列表：文件夹换成相对笔记库的路径，笔记库外面的留空（手机上写不到那里）。"""
    from apps.summit2md import subscriptions_store as store
    root = os.path.realpath(root)
    out = []
    for s in store.list_all():
        folder = os.path.realpath(s.get("folder") or "")
        inside = bool(s.get("folder")) and os.path.commonpath([folder, root]) == root
        out.append({k: s.get(k) for k in ("id", "name", "url", "kind", "source_type", "category")}
                   | {"skip_shorts": bool(s.get("skip_shorts"))}
                   | {"folder": os.path.relpath(folder, root).replace(os.sep, "/") if inside else "",
                      "ignored_ids": list(s.get("ignored_ids") or [])})
    return out


def _keep_prefs(folder: str) -> bool:
    """手机的阅读设置（{"prefs": {...}, "at": 秒}）比电脑存着的新就换上，返回换了没有。
    电脑只是转手：iPhone 上改的，下次接 iPad 时带过去（手机端见 apps/mobile-ios/Spark/DeviceSync.swift）。"""
    try:
        with open(os.path.join(folder, PREFS), encoding="utf-8") as f:
            new = json.load(f)
        at = float(new["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(new.get("prefs"), dict) or len(json.dumps(new)) > 4096:
        return False
    path = os.path.join(_spark_home(), "device-prefs.json")
    try:
        with open(path, encoding="utf-8") as f:
            if float(json.load(f).get("at", 0)) >= at:
                return False
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic.write_json(path, {"prefs": new["prefs"], "at": at})
    return True


# ---------------------------------------------------------------- 一次同步

_STATE: dict = {"running": False, "phase": "", "last": {}, "worker": False, "checked": {}, "quick": False}
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


def _pull(tr, book: TaskBook) -> tuple[int, int, int]:
    """读手机发件箱和阅读记录；手机上生成好的复盘、剪藏（type: save）马上存进笔记库。
    返回 (收到几个任务, 几条阅读记录, 存下几篇)。"""
    _STATE["phase"] = "读手机上添加的任务和阅读记录"
    with tempfile.TemporaryDirectory() as tmp:
        tr.pull_outbox(tmp)
        received = book.receive(tmp)
    reads = 0
    if hasattr(tr, "pull_group_dir"):
        with tempfile.TemporaryDirectory() as tmp:
            tr.pull_group_dir(READLOG, tmp)
            reads = _merge_readlog(tmp)
            _keep_prefs(tmp)
    return received, reads, _save_now(book)


def check_device(device: dict, *, transport: Optional[Transport] = None) -> dict:
    """只看一眼手机（阻塞，一两秒）：读发件箱和阅读记录、存下手机生成好的内容、开始跑手机加的任务。
    不往手机拷笔记库，那是整个同步（sync_device）的事。鸿蒙手机没有发件箱，走整个同步。"""
    _refuse_simulator(device)
    if device.get("platform") == "HarmonyOS":
        return sync_device(device, transport=transport)
    tr = transport or Transport(device)
    book = _book()
    received, reads, saved = _pull(tr, book)
    if saved:
        # 就地存下了手机的修改：马上把结果推回去，手机发件箱里那件能清掉（不用等下一次整个同步）
        _push_results(device, book, tr)
    if received or saved:
        _start_worker(device)
    return {"ok": True, "device": device.get("name"), "device_id": device.get("id"), "quick": True,
            "reads_received": reads, "tasks_received": received, "tasks_waiting": len(book.waiting()),
            "saved": saved}


def sync_device(device: dict, *, transport: Optional[Transport] = None) -> dict:
    """跟一台手机同步一次（阻塞）：拉发件箱 → 打包 → 推过去。"""
    _refuse_simulator(device)
    if device.get("platform") == "HarmonyOS":
        from core import taihe_sync
        try:
            return taihe_sync.sync_device(device, transport=transport)
        except taihe_sync.SyncError as error:
            raise CableError(str(error)) from error
    tr = transport or Transport(device)
    root = vault.vault_root()
    if not any(os.path.isdir(os.path.join(root, d)) for d in SYNC_DIRS):
        raise CableError(f"笔记库里没有 {' / '.join(SYNC_DIRS)}：{root}")
    home = _spark_home()
    book = _book()
    received, reads, saved = _pull(tr, book)

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
    manifest = {"version": 1, "generated_at": now, "device": device.get("name"), "files": files,
                "deleted": deleted_versions(root, files)}
    atomic.write_json(os.path.join(staging, RESULTS), book.results())
    atomic.write_json(os.path.join(staging, SUBS_COPY), {"subscriptions": export_subscriptions(root)})
    # 电脑上的阅读记录（含之前收到的手机记录）带给手机：手机上自己做阅读复盘时用
    try:
        shutil.copyfile(readlog.log_path(), os.path.join(staging, READLOG_COPY))
    except OSError:
        pass
    try:
        shutil.copyfile(os.path.join(home, "device-prefs.json"), os.path.join(staging, PREFS_COPY))
    except OSError:
        pass

    _STATE["phase"] = f"拷到「{device.get('name')}」"
    with _PUSH_LOCK:   # 跟只推任务结果的那次排队：旧的结果文件不会晚于新的落到手机上
        tr.push(staging, DEST)
        # 清单最后单独推：手机看到新清单时，这一批文件已经全拷进去了。手机开着的时候也会隔一会儿看一眼清单
        # （Vault.importCableIfChanged），不能让它在拷到一半时照新清单记「已下载」、删文件
        with tempfile.TemporaryDirectory() as tmp:
            atomic.write_json(os.path.join(tmp, MANIFEST), manifest)
            tr.push(tmp, DEST)
    _start_worker(device)
    return {"ok": True, "device": device.get("name"), "device_id": device.get("id"), "files": len(files),
            "reads_received": reads,
            "bytes": sum(s for _sha, s in files.values()), "tasks_received": received,
            "tasks_waiting": len(book.waiting()), "saved": saved}


def _save_now(book: TaskBook) -> int:
    """发件箱里「存进笔记库」「删掉复盘」的任务不用排队，同步时就地做。返回做了几个。"""
    n = 0
    # 按手机上做的先后（只决定先做哪件；收不收看内容，见 remote_tasks._same_version / _contained）
    for tid in book.waiting():
        task = book.tasks[tid].get("task") or {}
        if task.get("type") not in ("save", "delete", "purge_images", "track_item", "episode_item"):
            continue
        try:
            fn = {"save": remote_tasks.save_file, "delete": remote_tasks.delete_file, "purge_images": remote_tasks.purge_images,
                  "track_item": remote_tasks.save_track_item, "episode_item": remote_tasks.save_episode_item}[task["type"]]
            text = fn(task)
            book.update(tid, "done", f"完成：{text}", **({"applied_sha": task["_applied_sha"]} if task.get("_applied_sha") else {}))
            n += 1
        except remote_tasks.TaskError as e:
            book.update(tid, "failed", f"没成功：{e}", kind=e.kind)
        except OSError as e:
            book.update(tid, "failed", f"没成功：写文件出错：{e}", kind="rejected")
        except clips.JournalError as e:   # 写文件之前就读不了记录（写之后的已经在 remote_tasks 里接住）：这件失败，同步照常
            book.update(tid, "failed", f"没成功：{e}", kind="rejected")
    return n


def _start_worker(device: dict) -> None:
    """后台把等着的手机任务一个个跑完；跑完一个，如果手机还连着就把结果推回去。"""
    if _CALL["fn"] is None:
        return
    with _LOCK:
        if _STATE["worker"]:
            return
        _STATE["worker"] = True

    produced = {"any": False}

    def work() -> None:
        try:
            book = _book()
            while True:
                pending = book.waiting()
                if not pending:
                    return
                for tid in pending:
                    task = book.tasks[tid]["task"]
                    book.update(tid, "running", "电脑开始处理", started_at=time.time(), current=0, total=0,
                                step="", item="", subject="")
                    progress = _LocalProgress(book, tid, lambda: _push_results(device, book))
                    try:
                        text = remote_tasks.Runner(_CALL["fn"], progress).run(task)
                        book.update(tid, "done", f"完成：{text}",
                                    **({"applied_sha": task["_applied_sha"]} if task.get("_applied_sha") else {}))
                        produced["any"] = True
                    except remote_tasks.Busy:
                        book.update(tid, "waiting", "电脑上正在跑别的任务，等它结束再开始")
                        time.sleep(BUSY_RETRY_SECONDS)
                    except remote_tasks.TaskError as e:
                        book.update(tid, "failed", f"没成功：{e}", kind=e.kind)
                    except Exception as e:  # noqa: BLE001
                        book.update(tid, "failed", f"没成功：意外错误：{e}")
                    progress.settle()
                    _push_results(device, book)
        finally:
            with _LOCK:
                _STATE["worker"] = False
            if produced["any"]:
                _sync_after_tasks(device)

    threading.Thread(target=work, daemon=True, name="spark-cable-tasks").start()


def _sync_after_tasks(device: dict) -> None:
    """电脑做完手机交来的任务（处理链接、检查订阅、报告、复盘）后，马上整个同步一次，把写出来的文件送到手机。
    只推任务结果的话，手机上显示「完成」却打不开：插着线要等下一个 AUTO_RESYNC_SECONDS，Wi-Fi 连着的要等手动同步。
    正在同步别的就等一会儿再试；手机断开了就算了，下次同步再带过去。"""
    dev_id = device.get("id") or ""
    for _ in range(SYNC_AFTER_TASKS_TRIES):
        reason = start(dev_id)          # 不传 device：先确认手机还连着
        if reason == "":
            if device.get("transport") == "wired":
                _AUTO["last"][dev_id] = time.time()   # 刚整个同步过，自动同步不用马上再来一次
            return
        if not reason.startswith("正在同步"):
            return
        time.sleep(SYNC_AFTER_TASKS_WAIT)


_PUSH_LOCK = threading.Lock()


def _push_results(device: dict, book: TaskBook, tr: Optional["Transport"] = None) -> None:
    """只推任务结果那一个文件，一次推一个，不然手机可能读到写了一半的文件；手机断开了就算了，下次同步再带回去。"""
    if is_simulator(device) and not simulators_allowed():
        return          # 模拟器默认不认：连 simctl 都不调（Transport._sim_container 那里也会拦）
    with _PUSH_LOCK:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                atomic.write_json(os.path.join(tmp, RESULTS), book.results())
                (tr or Transport(device)).push(tmp, DEST)
        except (CableError, OSError):
            pass


def start(device_id: str, *, device: Optional[dict] = None, auto: bool = False, quick: bool = False) -> str:
    """在后台同步一次。返回不能开始的原因，能开始返回空串。auto：插上数据线自动开始的。
    quick：只看一眼手机发件箱（check_device），不拷笔记库。"""
    with _LOCK:
        if _STATE["running"]:
            return "正在同步，等这一次结束"
    if device is None:
        try:
            device = next((d for d in list_devices() if d["id"] == device_id), None)
        except CableError as e:
            return str(e)
    if not device:
        return "这台设备现在没连着（插上数据线，或者跟电脑在同一个 Wi-Fi 下并解锁）"
    try:
        _refuse_simulator(device)
    except CableError as e:
        return str(e)
    with _LOCK:
        if _STATE["running"]:
            return "正在同步，等这一次结束"
        _STATE["running"] = True
        _STATE["quick"] = quick
        _STATE["device"] = device.get("id") or device_id
        _STATE["phase"] = "开始"

    def run() -> None:
        try:
            result = check_device(device) if quick else sync_device(device)
        except CableError as e:
            result = {"ok": False, "device": device.get("name"), "error": str(e)}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "device": device.get("name"), "error": f"意外错误：{e}"}
        result["finished_at"] = time.time()
        result["auto"] = auto
        result.setdefault("device_id", device.get("id") or device_id)   # 出错时也带上，Mac 上「再看一次」才点得了
        if quick:
            result["quick"] = True
        with _LOCK:
            # 「上次看手机」两种都算；「上次同步」只记整个同步，不被每分钟的看一眼冲掉
            _STATE.update(running=False, phase="", quick=False, device="", checked=result)
            if not result.get("quick"):
                _STATE["last"] = result

    threading.Thread(target=run, daemon=True, name="spark-cable-sync").start()
    return ""


def status() -> dict:
    with _LOCK:
        out = {"running": _STATE["running"], "phase": _STATE["phase"], "last": dict(_STATE["last"]),
               "checked": dict(_STATE["checked"]), "quick": _STATE["quick"],
               "device_id": _STATE.get("device", "") if _STATE["running"] else "",
               "tasks_running": _STATE["worker"], "auto": _AUTO["on"]}
    try:   # 剪藏的迁移记录坏过、还没处理：同步状态里说出来
        from core import clips
        problems = clips.journal_problems(vault.vault_root())
    except OSError:
        problems = []
    if problems:
        out["warning"] = "；".join(problems)
    if out["tasks_running"]:
        book = _book()
        with book.lock:
            out["tasks_now"] = [{k: t.get(k) for k in PROGRESS_KEYS} for t in book.tasks.values()
                                if t.get("state") == "running"]
            out["tasks_left"] = sum(1 for t in book.tasks.values() if t.get("state") == "waiting")
    return out


# ---------------------------------------------------------------- 插上就同步

_AUTO: dict = {"on": False, "last": {}, "checked": {}}


def auto_tick(devices: Optional[list[dict]] = None, now: Optional[float] = None) -> list[str]:
    """用数据线连着的手机：刚插上、或者上次自动同步过了 AUTO_RESYNC_SECONDS，就整个同步一次；
    中间每 AUTO_CHECK_SECONDS 只看一眼发件箱（一两秒），手机上新加的东西一分钟内就到电脑。
    只管数据线：同一 Wi-Fi 下的手机一锁屏就掉、一解锁又冒出来，跟着它同步太频繁，要同步去设置页点。
    返回这一轮开始整个同步的设备名。"""
    now = time.time() if now is None else now
    if devices is None:
        try:
            devices = list_devices()
        except CableError:
            return []
    wired = {d["id"]: d for d in devices if d.get("transport") == "wired"}
    last: dict = _AUTO["last"]
    checked: dict = _AUTO.setdefault("checked", {})
    for book in (last, checked):
        for gone in [i for i in book if i not in wired]:
            del book[gone]      # 拔掉了：下次插上马上同步
    started = []
    for dev_id, d in wired.items():
        if dev_id in last and now - last[dev_id] < AUTO_RESYNC_SECONDS:
            if now - max(last[dev_id], checked.get(dev_id, 0)) >= AUTO_CHECK_SECONDS \
                    and start(dev_id, device=d, auto=True, quick=True) == "":
                checked[dev_id] = now
            continue
        if start(dev_id, device=d, auto=True) == "":
            last[dev_id] = now
            started.append(d.get("name") or dev_id)
        # 正在同步别的：不记时间，下一轮再试
    return started


def start_auto() -> None:
    """spark.py 启动时调：后台每 AUTO_POLL_SECONDS 秒看一眼，插上数据线的手机自动同步。"""
    with _LOCK:
        if _AUTO["on"]:
            return
        _AUTO["on"] = True

    def loop() -> None:
        while True:
            try:
                auto_tick()
            except Exception:  # noqa: BLE001  看设备出错不能让这个线程死掉
                pass
            time.sleep(AUTO_POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="spark-cable-auto").start()
