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

DEFAULT_VAULT = os.path.expanduser("~/Documents/Obsidian/minxu")

# 会议 / 播客的产物都放这里。目录名沿用库里已有的习惯——虽然叫"会议"，
# 实际上播客栏目（All-In Podcast、Dwarkesh）也一直放在这下面。
CONFERENCE_DIRNAME = "会议"


def vault_root() -> str:
    return os.path.expanduser(os.environ.get("SPARK_VAULT") or DEFAULT_VAULT)


def conference_dir(root: str = "") -> str:
    return os.path.join(root or vault_root(), CONFERENCE_DIRNAME)


def default_output_dir(fallback: str) -> str:
    """summit2md 的默认输出目录：笔记库在就往库里写，库不在（换了机器、外置盘没挂）
    就退回 app 自己的目录，免得直接创建一个半路冒出来的 ~/Documents/... 目录树。"""
    root = vault_root()
    if os.path.isdir(root):
        return conference_dir(root)
    return fallback
