"""
单文档压缩的共用管道：缓存、正文准备（去噪/截断/分块）。

**不统一的是提示词。** summit2md 的「议题小结」和 notes2insight 的「摘要卡」看着像
同一件事，其实不是：前者是给人读的（TLDR + 几条要点，直接嵌在演讲稿文档里），后者
是给机器读的（8 个固定小节，喂给跨笔记归纳）。把两者合成一份模板，只会要么把演讲稿
撑成结构化表格，要么把综合分析的输入抽稀。所以这里只收管道，各自的提示词留在各自
的 pipeline 里。

缓存一律按内容寻址——键由调用方给出的若干"决定输出的因素"拼成，其中正文用哈希
参与。路径和 mtime 绝不进键：改个文件夹名或让同步工具碰一下文件，都不该让一批
缓存作废（2026-09-17 把库里的 会议/ 改名成 Spark/ 就一次性废掉了 835 篇的缓存）。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Optional

from core import atomic


def content_hash(text: str) -> str:
    """正文的指纹。用来回答"内容变没变"——这正是 mtime 想代理却代理不好的那个问题。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def cache_key(*parts: str) -> str:
    """把"决定这次输出的因素"拼成一个键。调用方自己决定放什么：

    放进来的应该是真正影响输出的东西（提示词版本、后端、模型、关注点、正文哈希）；
    不该放的是那些变了而输出不该变的东西（文件路径、mtime、笔记在本次选择里的序号）。

    已知边界：用 "|" 连接，所以 ("a|b",) 和 ("a","b") 会撞成同一个键。要撞上得有人
    把 "|" 写进关注点、且刚好和另一组参数拼出同一串，概率极低、后果也轻（复用一张
    稍微不对的卡）。修它要改算法，而改算法会让现有缓存整批作废——正是这串改动想
    避免的事。将来要改就连同缓存迁移一起做。
    """
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def cache_get(key: str, cache_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(cache_dir, key + ".md"), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def cache_put(key: str, value: str, cache_dir: str) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        atomic.write_text(os.path.join(cache_dir, key + ".md"), value)
    except OSError:
        pass  # 缓存写不进去不该让任务失败，大不了下次重算


def cached_call(key: str, fn: Callable[[], str], *, cache_dir: str,
                use_cache: bool = True) -> tuple[str, bool]:
    """命中就返回缓存，否则调用 fn 并把结果存下来。

    返回 (结果, 是否命中缓存)——第二个值是给日志和成本记账用的，不然"这次到底省了
    多少"只能靠感觉。空结果不写缓存：上游把空内容当失败，别把失败缓存下来。
    """
    if use_cache:
        hit = cache_get(key, cache_dir)
        if hit:
            return hit, True
    result = fn()
    if result and result.strip():
        cache_put(key, result, cache_dir)
    return result, False


def strip_noise(text: str) -> str:
    """去掉图片引用和连续空行，这些对分析没用但很占上下文。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def prepare_body(text: str, max_chars: int) -> tuple[str, bool]:
    """按上限截断正文，并明确告诉调用方截没截断。

    返回 (正文, 是否被截断)。截断绝不能悄悄发生——两个 app 都会在产物里标出来是
    哪几篇被截了，靠的就是这个布尔值。max_chars 为 0 表示不限制。
    """
    if max_chars and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def chunks(text: str, size: int) -> list[str]:
    """按长度切块，尽量切在段落边界上，避免把一句话劈成两半。"""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            brk = text.rfind("\n\n", start + size // 2, end)
            if brk > 0:
                end = brk
        out.append(text[start:end])
        start = end
    return out
