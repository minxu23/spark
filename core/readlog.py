"""
阅读记录：电脑上的阅读页和手机客户端各自记下「什么时候读了哪一篇」，汇总到这台电脑的
~/.spark/reading-log.jsonl，给复盘（core/review.py）用。

一条记录：
    {"id": "…", "at": 1793600000.0, "source": "desktop" | "phone",
     "path": "Spark/Dwarkesh/notes/….md",      # 读的那个文件（笔记库里的相对路径），可能没有
     "show": "Spark/Dwarkesh", "entry": "abc",  # 手机直接给节目文件夹和条目 id
     "kind": "note" | "speech" | "transcript" | "article" | "doc"}

手机的记录怎么来：
- 走 GitHub 的：手机在同步仓库里一个带 spark-readlog 标签的 Issue 下发评论，正文是
  ```spark-readlog 代码块（一行一条 JSON）。复盘前从那里拉一遍（只读这个 Issue）。
- 走数据线的：手机写进 App Group 容器的 readlog/，数据线同步时拉回来（core/cable_sync.py）。
按 id 去重，同一条记录不管从哪条路来几次都只算一次。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Iterable, Optional

from core import settings as core_settings

READLOG_LABEL = "spark-readlog"
# 电脑上同一篇在这么短时间里再打开（刷新、切回来）不重复记
DESKTOP_DEDUP_SECONDS = 30 * 60
_BLOCK_RE = re.compile(r"```spark-readlog[ \t]*\n(.*?)\n```", re.S)
_KINDS = {"note", "speech", "transcript", "article", "doc"}
_LOCK = threading.Lock()
_RECENT: dict[str, float] = {}


def log_path() -> str:
    return os.path.join(os.path.dirname(core_settings.settings_path()), "reading-log.jsonl")


def _clean(ev: dict) -> Optional[dict]:
    """外面来的记录只留认识的字段，格式不对的丢掉。"""
    if not isinstance(ev, dict):
        return None
    eid = str(ev.get("id") or "")[:64]
    try:
        at = float(ev.get("at"))
    except (TypeError, ValueError):
        return None
    if not eid or not (946684800 < at < 4102444800):
        return None
    out = {"id": eid, "at": at, "source": "phone" if ev.get("source") == "phone" else "desktop"}
    for k in ("path", "show", "entry"):
        v = ev.get(k)
        if isinstance(v, str) and v and len(v) < 1000 and ".." not in v.split("/"):
            out[k] = v
    if ev.get("kind") in _KINDS:
        out["kind"] = ev["kind"]
    return out if (out.get("path") or out.get("show")) else None


def load(since: float = 0.0, until: float = 0.0) -> list[dict]:
    try:
        with open(log_path(), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("at", 0) >= since and (not until or r["at"] < until)]


def merge(events: Iterable[dict]) -> int:
    """把外面来的一批记录合进来（按 id 去重）。返回新增几条。"""
    with _LOCK:
        seen = {r.get("id") for r in load()}
        new = []
        for ev in events:
            c = _clean(ev)
            if c and c["id"] not in seen:
                seen.add(c["id"])
                new.append(c)
        if new:
            os.makedirs(os.path.dirname(log_path()), exist_ok=True)
            with open(log_path(), "a", encoding="utf-8") as f:
                for c in new:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
        return len(new)


def record_desktop(rel_path: str, kind: str = "") -> None:
    """阅读页打开一篇时调。半小时内同一篇只记一次。写不进去也不影响看。"""
    now = time.time()
    with _LOCK:
        last = _RECENT.get(rel_path, 0)
        if now - last < DESKTOP_DEDUP_SECONDS:
            return
        _RECENT[rel_path] = now
    try:
        merge([{"id": uuid.uuid4().hex, "at": now, "source": "desktop", "path": rel_path,
                **({"kind": kind} if kind in _KINDS else {})}])
    except OSError:
        pass


def parse_comment(body: str) -> list[dict]:
    """手机发在 spark-readlog Issue 下的一条评论 → 记录列表。"""
    out = []
    for block in _BLOCK_RE.findall(body or ""):
        for line in block.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def pull_github(gh, me: str = "") -> int:
    """从同步仓库的 spark-readlog Issue 拉手机的阅读记录。gh 是 core.remote_tasks.GitHub。
    只认自己账号发的评论。返回新增几条。"""
    issues = gh.issues_with_label(READLOG_LABEL)
    events = []
    for issue in issues:
        for c in gh.comments(issue["number"]):
            if me and (c.get("user") or {}).get("login") != me:
                continue
            events += parse_comment(c.get("body") or "")
    return merge(events)
