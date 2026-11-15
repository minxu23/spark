"""
用 Google Drive 桌面版的文件夹跟手机同步：跟数据线同步同一套流程（core/cable_sync.py 的
sync_device），只是「推到手机」「从手机拉」换成读写一个 Google Drive 文件夹。

    <Google Drive>/我的云端硬盘/Spark 同步/
        vault/      电脑推过去的笔记库（跟数据线拷进手机 App 里的那份一样，带 .spark-cable.json 清单）
        outbox/     手机上添加的任务（手机写，电脑读）
        readlog/    手机的阅读记录和阅读设置（手机写，电脑读）

手机上装 Google Drive App，在 Spark 里选一次这个文件夹，之后从这里读、往这里写。
电脑这边打开以后每隔一段时间同步一次；Google Drive 自己负责上传下载。

只拷有变化的文件（大小或修改时间不同），vault/ 里电脑上已经没有的文件删掉（那是同步用的副本，
不是笔记本身，删了 Google Drive 也会先放进它的回收站）。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import threading
import time
from typing import Optional

from core import atomic, cable_sync

FOLDER_NAME = "Spark 同步"
DEVICE = {"id": "google-drive", "name": "Google Drive", "transport": "drive"}
AUTO_SECONDS = 10 * 60          # 打开以后多久同步一次
AUTO_POLL_SECONDS = 30
_MY_DRIVE_NAMES = ("My Drive", "我的云端硬盘", "我的雲端硬碟")


class DriveError(RuntimeError):
    pass


# ---------------------------------------------------------------- 设置

def _config_path() -> str:
    return os.path.join(cable_sync._spark_home(), "drive-sync.json")


def load_config() -> dict:
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**values) -> dict:
    cfg = load_config()
    cfg.update(values)
    atomic.write_json(_config_path(), cfg)
    return cfg


def detect_my_drive(base: str = "") -> list[str]:
    """Google Drive 桌面版挂出来的「我的云端硬盘」：~/Library/CloudStorage/GoogleDrive-<账号>/My Drive。"""
    base = base or os.path.expanduser("~/Library/CloudStorage")
    out = []
    for account in sorted(glob.glob(os.path.join(base, "GoogleDrive-*"))):
        for name in _MY_DRIVE_NAMES:
            p = os.path.join(account, name)
            if os.path.isdir(p):
                out.append(p)
                break
    return out


def sync_folder(cfg: Optional[dict] = None) -> str:
    """同步用的文件夹：设置里填了就用填的，没填就用第一个「我的云端硬盘」下的「Spark 同步」。没装 Google Drive 返回空串。"""
    cfg = load_config() if cfg is None else cfg
    if cfg.get("folder"):
        return os.path.expanduser(cfg["folder"])
    drives = detect_my_drive()
    return os.path.join(drives[0], FOLDER_NAME) if drives else ""


# ---------------------------------------------------------------- 读写文件夹

def _same(src: str, dst: str) -> bool:
    try:
        a, b = os.stat(src), os.stat(dst)
    except OSError:
        return False
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def mirror(src: str, dst: str, *, prune: bool) -> tuple[int, int]:
    """src 里的文件拷到 dst：没变的跳过（大小、修改时间都一样）。prune：dst 里 src 没有的文件删掉。
    返回 (拷了几个, 删了几个)。"""
    copied = removed = 0
    keep: set[str] = set()
    for dirpath, _dirs, files in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        for name in files:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            keep.add(rel)
            s, d = os.path.join(src, rel), os.path.join(dst, rel)
            if _same(s, d):
                continue
            os.makedirs(os.path.dirname(d), exist_ok=True)
            tmp = d + ".spark-partial"
            shutil.copy2(s, tmp)
            os.replace(tmp, d)
            copied += 1
    if prune and os.path.isdir(dst):
        for dirpath, dirs, files in os.walk(dst, topdown=False):
            rel_dir = os.path.relpath(dirpath, dst)
            for name in files:
                rel = os.path.normpath(os.path.join(rel_dir, name))
                if rel not in keep:
                    try:
                        os.remove(os.path.join(dst, rel))
                        removed += 1
                    except OSError:
                        pass
            if dirpath != dst and not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    return copied, removed


class DriveTransport:
    """交给 cable_sync.sync_device 用：推到手机 = 写进 vault/，从手机拉 = 读 outbox/、readlog/。"""

    def __init__(self, folder: str):
        self.folder = folder
        self.copied = 0
        self.removed = 0

    def push(self, local_dir: str, dest: str) -> None:
        target = os.path.join(self.folder, "vault")
        os.makedirs(target, exist_ok=True)
        try:
            c, r = mirror(local_dir, target, prune=True)
        except OSError as e:
            raise cable_sync.CableError(f"写 Google Drive 文件夹失败：{e}") from e
        self.copied += c
        self.removed += r

    def pull_outbox(self, local_dir: str) -> None:
        self._pull("outbox", local_dir)

    def pull_group_dir(self, name: str, local_dir: str) -> None:
        self._pull(os.path.basename(name.rstrip("/")), local_dir)

    def _pull(self, name: str, local_dir: str) -> None:
        src = os.path.join(self.folder, name)
        if os.path.isdir(src):
            for entry in os.listdir(src):
                p = os.path.join(src, entry)
                if os.path.isfile(p) and not entry.endswith(".spark-partial"):
                    shutil.copy2(p, os.path.join(local_dir, entry))


# ---------------------------------------------------------------- 同步

_STATE = {"running": False, "last": {}, "auto": False}
_LOCK = threading.Lock()


def sync_once() -> dict:
    """同步一次（阻塞）。跟数据线同步共用一份暂存目录和任务表，同一时间只跑一个。"""
    folder = sync_folder()
    if not folder:
        raise DriveError("这台电脑没装 Google Drive 桌面版（~/Library/CloudStorage 下没有 GoogleDrive-…）")
    os.makedirs(os.path.join(folder, "outbox"), exist_ok=True)
    os.makedirs(os.path.join(folder, "readlog"), exist_ok=True)
    tr = DriveTransport(folder)
    with cable_sync._LOCK:
        if cable_sync._STATE["running"]:
            raise DriveError("数据线正在同步，等它结束")
        cable_sync._STATE["running"] = True
        cable_sync._STATE["phase"] = "同步到 Google Drive"
    try:
        result = cable_sync.sync_device(dict(DEVICE), transport=tr)
    except cable_sync.CableError as e:
        raise DriveError(str(e)) from e
    finally:
        with cable_sync._LOCK:
            cable_sync._STATE.update(running=False, phase="")
    result.update(folder=folder, copied=tr.copied, removed=tr.removed)
    return result


def start() -> str:
    """后台同步一次。返回不能开始的原因，能开始返回空串。"""
    with _LOCK:
        if _STATE["running"]:
            return "正在同步到 Google Drive，等这一次结束"
        _STATE["running"] = True

    def run() -> None:
        try:
            result = sync_once()
        except DriveError as e:
            result = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"意外错误：{e}"}
        result["finished_at"] = time.time()
        with _LOCK:
            _STATE.update(running=False, last=result)

    threading.Thread(target=run, daemon=True, name="spark-drive-sync").start()
    return ""


def status() -> dict:
    cfg = load_config()
    with _LOCK:
        out = {"running": _STATE["running"], "last": dict(_STATE["last"]), "auto": _STATE["auto"]}
    out.update(enabled=bool(cfg.get("enabled")), folder=sync_folder(cfg), drives=detect_my_drive())
    return out


def auto_due(now: float, last_finished: float, enabled: bool, folder: str) -> bool:
    return bool(enabled and folder and os.path.isdir(os.path.dirname(folder)) and now - last_finished >= AUTO_SECONDS)


def start_auto() -> None:
    """打开了 Google Drive 同步就每隔 AUTO_SECONDS 同步一次（没打开就空转）。"""
    with _LOCK:
        if _STATE["auto"]:
            return
        _STATE["auto"] = True

    def loop() -> None:
        while True:
            cfg = load_config()
            with _LOCK:
                last = _STATE["last"].get("finished_at") or 0
                busy = _STATE["running"]
            if not busy and auto_due(time.time(), last, bool(cfg.get("enabled")), sync_folder(cfg)):
                start()
            time.sleep(AUTO_POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="spark-drive-auto").start()

