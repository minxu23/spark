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


def write_json(path: str, data, *, indent: int | None = None) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))
