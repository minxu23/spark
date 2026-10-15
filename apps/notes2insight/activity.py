"""
用户近期在关注什么：判断主题含义时的一个信号。

同一个缩写在不同时期可能指不同的东西（RSI 去年是递归自我改进，今年开始炒股可能就是
相对强弱指标）。库里的笔记是多年积累的，多数用法代表的是过去；最近检索过什么、生成过
什么报告、新订阅了哪些来源，更能代表现在。

检索记录存在 .cache/search_history.json，只记主题和最后按哪个含义检索的，不记结果。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from typing import Callable, Optional

from core import atomic

from . import vault

HISTORY_MAX = 200
# 多少天以内算"近期"
RECENT_DAYS = 60

_LOCK = threading.Lock()


def _history_path() -> str:
    # 每次现取：测试会把 vault.CACHE_DIR 指到临时目录
    return os.path.join(vault.CACHE_DIR, "search_history.json")


def load_history() -> list[dict]:
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [h for h in data if isinstance(h, dict) and h.get("topic")] if isinstance(data, list) else []


def record_search(topic: str, label: str = "", meaning: str = "", *, manual: bool = False,
                  now: Optional[float] = None) -> None:
    """记一次检索。manual 表示含义是用户自己点选的，比模型自动判的信号更强。"""
    topic = (topic or "").strip()
    if not topic:
        return
    item = {"topic": topic[:200], "label": (label or "")[:60], "meaning": (meaning or "")[:200],
            "manual": bool(manual), "at": int(now if now is not None else time.time())}
    with _LOCK:
        hist = load_history()
        hist.append(item)
        try:
            os.makedirs(os.path.dirname(_history_path()), exist_ok=True)
            atomic.write_json(_history_path(), hist[-HISTORY_MAX:])
        except OSError:
            pass


def _load_subscriptions() -> list[dict]:
    """信息跟进和播客的订阅列表（summit2md 管着）。读不到就当没有，不影响检索。"""
    try:
        from apps.summit2md import subscriptions_store
        return subscriptions_store.list_all()
    except Exception:
        return []


def recent_activity(output_dir: str = "", *, days: int = RECENT_DAYS, now: Optional[float] = None,
                    load_subs: Callable[[], list[dict]] = _load_subscriptions) -> list[str]:
    """近期活动的几行文字，直接拼进提示词：最近的检索、最近生成的报告、最近新加的订阅。"""
    now = now if now is not None else time.time()
    cutoff = now - days * 86400
    lines: list[str] = []

    def day(ts: float) -> str:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    seen: set[tuple[str, str]] = set()
    for h in reversed(load_history()):
        if h.get("at", 0) < cutoff or len(seen) >= 15:
            continue
        key = (h["topic"].lower(), h.get("label", ""))
        if key in seen:
            continue
        seen.add(key)
        how = f"，按「{h['label']}」" if h.get("label") else ""
        how += "（用户手动选的含义）" if h.get("manual") and h.get("label") else ""
        lines.append(f"{day(h.get('at', now))} 检索「{h['topic']}」{how}")

    if output_dir and os.path.isdir(output_dir):
        reports = []
        try:
            for name in os.listdir(output_dir):
                full = os.path.join(output_dir, name)
                if name.lower().endswith(".md") and os.path.isfile(full):
                    mt = os.path.getmtime(full)
                    if mt >= cutoff:
                        reports.append((mt, os.path.splitext(name)[0]))
        except OSError:
            reports = []
        for mt, title in sorted(reports, reverse=True)[:10]:
            lines.append(f"{day(mt)} 生成报告《{title}》")

    by_cat: dict[str, list[str]] = {}
    for s in load_subs() or []:
        created = s.get("created_at") or ""
        try:
            ts = _dt.datetime.fromisoformat(created).timestamp()
        except ValueError:
            continue
        if ts >= cutoff and s.get("name"):
            by_cat.setdefault(s.get("category") or "未分类", []).append(s["name"])
    for cat, names in sorted(by_cat.items(), key=lambda kv: -len(kv[1]))[:8]:
        more = f" 等 {len(names)} 个" if len(names) > 4 else ""
        lines.append(f"最近新订阅（{cat}）：{'、'.join(names[:4])}{more}")
    return lines
