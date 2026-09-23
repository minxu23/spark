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
_EDITABLE_FIELDS = {"name", "category", "folder"}


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
    return changed


def _load_locked() -> list[dict]:
    if not os.path.exists(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        # 文件损坏/手改坏了不该让整个「信息跟进」页面打不开，退回空列表——
        # 大不了订阅记录丢了要重新加，比服务直接 500 安全。
        return []
    if not isinstance(data, list):
        return []
    if any([_migrate(item) for item in data]):
        _save_locked(data)
    return data


def _save_locked(items: list[dict]) -> None:
    tmp_path = f"{STORE_PATH}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STORE_PATH)


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


def add(*, url: str, name: str, category: str, output_dir: str, source_type: str) -> dict:
    item = {
        "id": uuid.uuid4().hex,
        "url": url,
        "name": name,
        "category": category,
        "output_dir": output_dir,
        "folder": default_folder(output_dir, name),
        "source_type": source_type,
        "ignored_ids": [],
        "created_at": _now(),
        "last_checked_at": None,
    }
    with _LOCK:
        items = _load_locked()
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
