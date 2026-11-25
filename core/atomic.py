"""原子写文件：先写到同目录下一个唯一命名的临时文件，再 os.replace 过去。

直接 open(path, "w") 写到一半进程被杀（或者两个线程同时写），会留下一个截断的
文件；缓存/manifest 读到截断的 JSON 要么报错卡住，要么被当成空记录。临时文件名
带随机后缀，几个写入方同时写同一个文件时也不会互相踩到对方的临时文件。
"""

from __future__ import annotations

import json
import os
import uuid


def write_text(path: str, text: str) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if path.endswith(".md"):
        _note_search_index(path)


def _note_search_index(path: str) -> None:
    """电脑端保存的 Markdown 记给全文索引（core/search_index）：下一次搜索前补进去。只记一笔，不读文件。"""
    try:
        from core import search_index
        search_index.note_changed(path)
    except Exception:   # noqa: BLE001  索引出了问题也不能影响保存
        pass


def write_json(path: str, data, *, indent: int | None = None) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def copy_new(src: str, path: str) -> bool:
    """把 src 拷成 path，只在 path 还不存在时放上去，不覆盖已有的文件（数据线收手机的论文 PDF 用，core/cable_sync）。
    先拷到同目录下一个唯一命名的临时文件，再用硬链接放到 path：path 已经有了硬链接就失败，不会盖掉别人刚写的。
    返回 True 放上了；path 已经有了返回 False，不动它。临时文件最后总会去掉（那是自己的，不是用户的内容）。"""
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(src, "rb") as fin, open(tmp, "xb") as fout:
            for chunk in iter(lambda: fin.read(1 << 20), b""):
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError:
            # 文件系统不支持硬链接：退回「看一眼再 os.replace」。看和换之间别人写进来的会被盖掉，这个窗口很小
            if os.path.lexists(path):
                return False
            os.replace(tmp, path)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
