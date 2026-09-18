"""
给"导入目录""输出目录"这类路径输入框做自动补全用：输入一半路径，列出匹配的
子目录。两个 app（summit2md、notes2insight）都有好几处目录路径输入框，逻辑
放这一处共用，不各写一份。
"""

from __future__ import annotations

import os

MAX_SUGGESTIONS = 20


def browse_dir_suggestions(raw_path: str) -> list[str]:
    """返回 raw_path 当前这一段能匹配上的子目录，绝对路径，按名字排序。

    raw_path 以 / 结尾（或为空）时，列出该目录本身的子目录；否则把最后一段
    当成还没打完的前缀，在其上级目录里按前缀过滤。目录不存在/无权限一律
    返回空列表，交给调用方决定要不要提示——这里不是校验，只是补全建议。
    """
    raw_path = (raw_path or "").strip()
    expanded = os.path.expanduser(raw_path) if raw_path else os.path.expanduser("~")
    if not raw_path or raw_path.endswith("/"):
        base_dir = expanded
        prefix = ""
    else:
        base_dir = os.path.dirname(expanded) or "."
        prefix = os.path.basename(expanded)

    if not os.path.isdir(base_dir):
        return []

    try:
        names = sorted(os.listdir(base_dir))
    except OSError:
        return []

    prefix_lower = prefix.lower()
    results = []
    for name in names:
        if name.startswith("."):
            continue
        if prefix and not name.lower().startswith(prefix_lower):
            continue
        full = os.path.join(base_dir, name)
        if os.path.isdir(full):
            results.append(full)
        if len(results) >= MAX_SUGGESTIONS:
            break
    return results


def dir_plausible(raw_path: str) -> bool:
    """输入框失焦时用来判断"这个路径值得记进最近使用"：目录本身已经存在，或者
    它的上级目录存在——后一种是用户在给一个还没建过的输出目录起名字，字面上
    自然不存在，但落点是真实的。纯粹打错、上级目录都不存在的路径不值得记，
    不然最近使用列表里全是误输入的垃圾。
    """
    raw_path = (raw_path or "").strip()
    if not raw_path:
        return False
    expanded = os.path.expanduser(raw_path)
    if os.path.isdir(expanded):
        return True
    parent = os.path.dirname(expanded.rstrip("/")) or "/"
    return os.path.isdir(parent)
