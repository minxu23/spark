"""持久化「信息跟进」的订阅列表：一个 JSON 文件，记录长期跟踪的信息源和它们的类别。

跟 output 目录下按次生成的 .manifest.json 是两回事——manifest 记的是"这个源
已经生成过哪些条目"，这个文件记的是"我在跟哪些源、分在什么类别、落到哪个目录"。
两者靠 output_dir + name（算出的子目录）对上，这里不维护一份"已知 id 列表"，
免得跟 manifest 脱节；"有没有新内容"每次现读 manifest 现算（见 pipeline.find_new_entries）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(APP_DIR, "subscriptions.json")

_LOCK = threading.Lock()

_EDITABLE_FIELDS = {"name", "category", "output_dir"}


def _load_locked() -> list[dict]:
    if not os.path.exists(STORE_PATH):
        return []
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        # 文件损坏/手改坏了不该让整个「信息跟进」页面打不开，退回空列表——
        # 大不了订阅记录丢了要重新加，比服务直接 500 安全。
        return []


def _save_locked(items: list[dict]) -> None:
    tmp_path = STORE_PATH + ".tmp"
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
        "source_type": source_type,
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
