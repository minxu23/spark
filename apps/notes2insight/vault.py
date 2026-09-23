"""
笔记库扫描：把 Obsidian vault 里的 .md 文件整理成前端可勾选的树。

只读文件头部（4KB）取标题与日期，整库元数据缓存在 .cache/index.json，
按 (路径, mtime, 大小) 判断是否需要重新解析，4700 篇笔记的二次扫描在 1 秒内完成。
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Iterable, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(APP_DIR, ".cache")
INDEX_PATH = os.path.join(CACHE_DIR, "index.json")

# 库的位置由 core/vault.py 统一定义（支持 SPARK_VAULT 覆盖），这里只做转发。
# vault.py 可能先于 llm.py 被导入，所以这里也要自己把仓库根目录放进 import 路径。
import sys as _sys  # noqa: E402

_SPARK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SPARK_ROOT not in _sys.path:
    _sys.path.insert(0, _SPARK_ROOT)

from core import atomic  # noqa: E402
from core.vault import vault_root as _vault_root  # noqa: E402

DEFAULT_VAULT = _vault_root()

# 这些目录不是笔记内容：Obsidian 配置、图片附件、版本控制、工具自身产物
EXCLUDE_DIRS = {
    ".obsidian", ".trash", ".git", ".cache", ".smart-env", ".space", "__pycache__",
    "images", "image", "assets", "attachments", "_resources",
}
EXCLUDE_NAMES = {"README.md", "Welcome.md"}
NOTE_TEXT_EXTS = (".md", ".markdown", ".txt")

HEAD_BYTES = 4096

# index.json 里按根目录存了好几份库的索引；几个线程同时扫不同的根目录时，
# 读-改-写要串起来，不然后写的会把先写的那个根目录冲掉。
_INDEX_LOCK = threading.Lock()
DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
FM_DATE = re.compile(r"^\s*(?:date|created|发布日期)\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.M)
FM_TITLE = re.compile(r"^\s*title\s*[:：]\s*(.+)$", re.M)
MD_TITLE = re.compile(r"^#\s+(.+)$", re.M)
META_DATE = re.compile(r"^-\s*(?:发布日期|日期)\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.M)


def _read_head(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_head(path: str, fname: str) -> tuple[str, str]:
    """返回 (标题, 日期)。取不到就退回文件名 / 空串。"""
    head = _read_head(path)
    title = ""
    m = FM_TITLE.search(head)
    if m:
        title = m.group(1).strip().strip("\"'")
    if not title:
        m = MD_TITLE.search(head)
        if m:
            title = m.group(1).strip()
    if not title:
        title = os.path.splitext(fname)[0]

    date = ""
    m = FM_DATE.search(head) or META_DATE.search(head)
    if m:
        date = m.group(1)
    if not date:
        m = DATE_IN_NAME.search(fname)
        if m:
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return title[:200], date


def _load_index() -> dict:
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(index: dict) -> None:
    # 上传/链接导入的临时目录也会被扫描、记进索引；目录清理掉之后这些记录就是
    # 纯垃圾，保存时顺手去掉，免得索引文件越攒越大。
    index = {root: v for root, v in index.items() if os.path.isdir(root)}
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        atomic.write_json(INDEX_PATH, index)
    except OSError:
        pass


def scan(root: str, *, use_cache: bool = True) -> list[dict]:
    """扫描笔记库，返回按路径排序的笔记列表。"""
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise FileNotFoundError(f"笔记库目录不存在：{root}")

    cache = _load_index() if use_cache else {}
    cache_for_root = cache.get(root, {}) if isinstance(cache.get(root), dict) else {}
    fresh: dict[str, dict] = {}
    notes: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fname in filenames:
            if not fname.lower().endswith(".md") or fname in EXCLUDE_NAMES:
                continue
            full = os.path.join(dirpath, fname)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root)
            key = f"{st.st_mtime_ns}:{st.st_size}"
            cached = cache_for_root.get(rel)
            if cached and cached.get("key") == key:
                title, date = cached["title"], cached["date"]
            else:
                title, date = _parse_head(full, fname)
            fresh[rel] = {"key": key, "title": title, "date": date}
            folder = os.path.dirname(rel) or "."
            notes.append({
                "path": rel,
                "folder": folder,
                "name": fname,
                "title": title,
                "date": date,
                "bytes": st.st_size,
                # 中文笔记里一个汉字约 3 字节，用于前端估算规模，够粗略但稳定
                "chars": st.st_size // 3,
                "mtime": int(st.st_mtime),
            })

    with _INDEX_LOCK:
        # 重新读一遍再合并：扫描这段时间里别的线程可能已经写进了别的根目录
        # use_cache=False 只是这个根目录不用旧缓存，别的根目录的记录要留着。
        latest = _load_index()
        latest[root] = fresh
        _save_index(latest)
    notes.sort(key=lambda n: (n["folder"], n["name"]))
    return notes


def folder_tree(notes: Iterable[dict]) -> list[dict]:
    """按文件夹汇总，供前端渲染可折叠分组。"""
    groups: dict[str, dict] = {}
    for n in notes:
        g = groups.setdefault(n["folder"], {"folder": n["folder"], "count": 0, "bytes": 0})
        g["count"] += 1
        g["bytes"] += n["bytes"]
    return sorted(groups.values(), key=lambda g: g["folder"])


def read_note(root: str, rel: str, *, max_chars: Optional[int] = None) -> str:
    """读取一篇笔记正文；rel 必须落在 root 内，防止路径穿越。"""
    root = os.path.realpath(os.path.expanduser(root))
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([full, root]) != root:
        raise ValueError(f"非法的笔记路径：{rel}")
    # root 是前端传来的（可以是任意目录），只靠"在 root 里面"挡不住读 key 文件
    # 之类的非笔记文件——笔记只可能是这几种文本格式。
    if not full.lower().endswith(NOTE_TEXT_EXTS):
        raise ValueError(f"只能读取笔记文件（{' '.join(NOTE_TEXT_EXTS)}）：{rel}")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text
