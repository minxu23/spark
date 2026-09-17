"""
迁移垫片：实现已经搬到 Spark 的 core/llm.py，与 summit2md 共用同一份。

这里只做转发，好让 server.py / pipeline.py / search.py / deck.py 里的
`import llm` 和 `llm.xxx` 调用点一行都不用改。等调用点逐步改成直接 import core.llm
之后，这个文件可以删掉。
"""

from __future__ import annotations

import os
import sys

_SPARK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SPARK_ROOT not in sys.path:
    sys.path.insert(0, _SPARK_ROOT)

from core import keys as _keys  # noqa: E402
from core.keys import read_key_file  # noqa: E402,F401
from core.llm import (  # noqa: E402,F401
    LLMError,
    complete,
    list_ollama_models,
    ssl_context,
    DEFAULT_OLLAMA_HOST,
    OPENROUTER_API_BASE,
)

# 界面上提示"把 key 放在这里"时用：已经有 key 文件就指向它所在的目录，
# 否则指向新位置 ~/.spark/keys
KEYS_DIR = _keys.display_dir()
