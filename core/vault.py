"""
笔记库的位置，以及各类产物在库里的落点。

两个 app 之前各自写死了自己的路径：notes2insight 认 ~/Documents/Obsidian/minxu，
summit2md 认自己目录下的 output/。结果是同一批内容有时进库、有时留在 app 目录里
（2026-09 就出现过：13-15 号的运行进了库，16 号之后的 6 个留在 app 目录）。
这里统一成一处定义。

环境变量 SPARK_VAULT 可以覆盖库的位置，便于换机器或临时指向别的库。
"""

from __future__ import annotations

import os
import re

DEFAULT_VAULT = os.path.expanduser("~/Documents/Obsidian/minxu")

# Spark 生成的内容（会议、峰会、播客栏目）都放这里。原名"会议"，2026-09-17 改名为
# Spark——那个目录下从来就不只有会议，All-In Podcast、Dwarkesh 这些栏目一直也在里面。
SPARK_DIRNAME = "Spark"

# 单集笔记末尾用户自己的一节：阅读页里高亮的句子汇总在这里（不管是在笔记、整理稿还是
# 文字记录里高亮的）。重新生成小结时笔记会整篇重写，这一节要原样接回去。
HIGHLIGHTS_HEADING = "## 我的高亮"
HIGHLIGHTS_SECTION_RE = re.compile(r"^## 我的高亮[ \t]*\n.*?(?=^## |\Z)", re.M | re.S)


def highlights_section(text: str) -> str:
    """笔记里「我的高亮」这一节（含标题），没有就是空串。"""
    m = HIGHLIGHTS_SECTION_RE.search(text)
    return m.group(0).rstrip() + "\n" if m else ""


def keep_highlights_section(old: str, new: str) -> str:
    """整篇重写笔记时，把旧笔记里的「我的高亮」接到新笔记末尾。"""
    sec = highlights_section(old)
    if not sec or highlights_section(new):
        return new
    return new.rstrip("\n") + "\n\n" + sec


def vault_root() -> str:
    return os.path.expanduser(os.environ.get("SPARK_VAULT") or DEFAULT_VAULT)


def spark_dir(root: str = "") -> str:
    return os.path.join(root or vault_root(), SPARK_DIRNAME)


# 笔记洞察（notes2insight）默认把报告和演示写到库根目录的 output/ 里
REPORTS_DIRNAME = "output"


def reports_dir(root: str = "") -> str:
    return os.path.join(root or vault_root(), REPORTS_DIRNAME)


def default_output_dir(fallback: str) -> str:
    """summit2md 的默认输出目录：笔记库在就往库里写，库不在（换了机器、外置盘没挂）
    就退回 app 自己的目录，免得直接创建一个半路冒出来的 ~/Documents/... 目录树。"""
    root = vault_root()
    if os.path.isdir(root):
        return spark_dir(root)
    return fallback
