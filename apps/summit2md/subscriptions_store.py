"""持久化「信息跟进」的订阅列表：一个 JSON 文件，记录长期跟踪的信息源和它们的类别。

跟每个订阅文件夹里的 .manifest.json 是两回事——manifest 记的是"这个源已经
处理过哪些条目"，这个文件记的是"我在跟哪些源、分在什么类别、落到哪个文件夹、
哪些条目被我明确忽略了"。"有没有新内容"每次现读 manifest 现算（见 tracking.py）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from core import atomic

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(APP_DIR, "subscriptions.json")

# 信息跟进的产出统一放在输出根目录下的这个子目录里，跟会议/播客的目录分开。
TRACK_DIRNAME = "信息跟进"
BRIEF_DIRNAME = "简报"

# 忽略列表只需要盖住 feed 里还会出现的条目；feed 一般只保留最近几十条，
# 留个上限免得文件无限变大。
MAX_IGNORED_IDS = 2000

_LOCK = threading.Lock()

# folder 可以改（比如想挪到别的位置），但不会因为改名字而自动变——订阅的历史
# （manifest、已生成的笔记）跟着 folder 走，改名不该让它"失忆"。
_EDITABLE_FIELDS = {"name", "category", "folder", "auto_check"}


def default_folder(output_dir: str, name: str) -> str:
    from .pipeline import sanitize_filename
    return os.path.join(output_dir, TRACK_DIRNAME, sanitize_filename(name))


def _migrate(item: dict) -> bool:
    changed = False
    if not item.get("folder"):
        item["folder"] = default_folder(item.get("output_dir") or "", item.get("name") or "untitled")
        changed = True
    if not isinstance(item.get("ignored_ids"), list):
        item["ignored_ids"] = []
        changed = True
    if not isinstance(item.get("auto_check"), bool):
        # 有这个开关之前的订阅都是每次自动检查的，保持原样
        item["auto_check"] = True
        changed = True
    return changed


class DuplicateSubscription(ValueError):
    """这个链接已经订阅过了。放在锁里查：批量导入要探测好几分钟，开始时读的
    那份清单早就不是最新的了。"""


class StoreCorrupt(RuntimeError):
    """subscriptions.json 读不出来。不能当成"没有订阅"——那样下一次添加就会把
    原文件整个覆盖掉，所有订阅一起丢。"""


def _load_locked() -> list[dict]:
    if not os.path.exists(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise StoreCorrupt(f"订阅列表文件读不出来（{e}），为免覆盖原有订阅，先不做任何修改。"
                           f"请检查或移走 {STORE_PATH} 后再试") from e
    if not isinstance(data, list):
        raise StoreCorrupt(f"订阅列表文件格式不对（应该是一个列表），请检查 {STORE_PATH}")
    # 手改进去的非对象条目没法当订阅用，跳过；保存时也就不再写回去
    data = [item for item in data if isinstance(item, dict)]
    if any([_migrate(item) for item in data]):
        _save_locked(data)
    return data


def _save_locked(items: list[dict]) -> None:
    atomic.write_json(STORE_PATH, items, indent=2)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def list_all() -> list[dict]:
    with _LOCK:
        return _load_locked()


def get(sub_id: str) -> dict | None:
    with _LOCK:
        for item in _load_locked():
            if item.get("id") == sub_id:
                return item
    return None


def add(*, url: str, name: str, category: str, output_dir: str, source_type: str,
        auto_check: bool = True) -> dict:
    item = {
        "id": uuid.uuid4().hex,
        "url": url,
        "name": name,
        "category": category,
        "output_dir": output_dir,
        "folder": default_folder(output_dir, name),
        "source_type": source_type,
        "ignored_ids": [],
        # 打开页面 / 点「重新检查」时要不要检查它；关掉的只在手动点「检查」时才查
        "auto_check": bool(auto_check),
        "created_at": _now(),
        "last_checked_at": None,
    }
    with _LOCK:
        items = _load_locked()
        if any(it.get("url") == url for it in items):
            raise DuplicateSubscription(url)
        items.append(item)
        _save_locked(items)
    return item


def update(sub_id: str, patch: dict) -> dict | None:
    with _LOCK:
        items = _load_locked()
        for item in items:
            if item.get("id") == sub_id:
                for k, v in patch.items():
                    if k in _EDITABLE_FIELDS and v is not None:
                        item[k] = v
                _save_locked(items)
                return item
    return None


def delete(sub_id: str) -> bool:
    with _LOCK:
        items = _load_locked()
        kept = [it for it in items if it.get("id") != sub_id]
        if len(kept) == len(items):
            return False
        _save_locked(kept)
        return True


def touch_checked(sub_id: str) -> None:
    with _LOCK:
        items = _load_locked()
        for item in items:
            if item.get("id") == sub_id:
                item["last_checked_at"] = _now()
                _save_locked(items)
                return


def ignore(sub_id: str, entry_ids: list[str]) -> dict | None:
    """把这些条目标成"不看了"：之后检查新内容时不再列出来。"""
    with _LOCK:
        items = _load_locked()
        for item in items:
            if item.get("id") == sub_id:
                new_ids = list(dict.fromkeys(i for i in entry_ids if i))
                kept = [i for i in item["ignored_ids"] if i not in set(new_ids)]
                item["ignored_ids"] = (kept + new_ids)[-MAX_IGNORED_IDS:]
                _save_locked(items)
                return item
    return None


def set_auto_check(sub_ids: list[str], value: bool) -> int:
    """批量开关"自动检查"（比如整个类别一起改），返回改了几条。"""
    wanted = set(sub_ids)
    with _LOCK:
        items = _load_locked()
        n = 0
        for item in items:
            if item.get("id") in wanted and item.get("auto_check") != bool(value):
                item["auto_check"] = bool(value)
                n += 1
        if n:
            _save_locked(items)
        return n


def rename_category(old: str, new: str) -> int:
    """把这个类别下所有订阅的 category 字段批量改成新值，返回改了几条。"""
    old = (old or "").strip()
    new = (new or "").strip()
    with _LOCK:
        items = _load_locked()
        n = 0
        for item in items:
            if item.get("category") == old:
                item["category"] = new
                n += 1
        if n:
            _save_locked(items)
        return n
