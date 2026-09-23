"""
主题检索：给一个主题，从整库里找出最相关的笔记。

两段式，先便宜后贵：
  1. 关键词扩展（1 次模型调用）：把主题扩成中英文混合的检索词，带权重
  2. 本地全文打分（不花钱）：BM25 变体，中文用子串计数，英文用带词边界的单遍扫描
  3. 相关度筛选（几次模型调用）：只对打分靠前的候选，看标题+命中片段判相关度

最终按 (相关度, 文本分) 排序，交给报告流水线。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

from . import llm
from . import vault

# 检索时每篇最多读这么多字符：再长的笔记，前面部分已足够判断主题相关性
READ_CAP = 200000
# 本地打分后保留多少篇进入模型筛选
DEFAULT_CANDIDATES = 60
# BM25 参数
BM25_K1 = 1.4
BM25_B = 0.72

EXPAND_PROMPT = """你是检索助手。用户要在一个中英文混排的技术笔记库（科技播客整理稿、行业文章、论文、大会记录）里，找出与下面主题相关的笔记。

请给出用于**全文关键词匹配**的检索词。

要求：
- 一行一个，格式严格为 `权重|检索词`，权重只能是 3、2、1。
  3 = 主题的核心概念（命中基本就相关）；2 = 强相关术语、代表性公司/产品/技术名；1 = 弱相关线索词。
- 中英文都要给：同一个概念的中文说法和英文说法**各占一行**，不要写在同一行里。
- 给**具体**的词：公司名、产品名、芯片名、模型名、协议名、指标名。不要给"技术""趋势""发展""影响"这类泛词。
- 不要给单个汉字，不要给长句，每个词不超过 12 个字符。
- 一共 15-28 个词。

主题：{topic}

只输出检索词列表，不要解释。
"""

SCREEN_PROMPT = """你是检索筛选助手。当前主题是：

**{topic}**

下面是若干候选笔记的标题与全文命中片段。请逐条判断它与这个主题的相关度。

相关度定义：
5 = 核心材料，整篇都在讲这个主题
4 = 高度相关，有大段直接讨论
3 = 相关，有明确且可用的内容
2 = 部分相关，只是顺带提到
1 = 边缘，只有词面命中
0 = 无关（命中的是同形词或完全不同的语境）

输出格式严格为每行一条：`编号|相关度|不超过25字的理由`
不要输出表头、解释或多余空行。编号必须与输入一致，每个候选都要有一行。

{items}
"""


@dataclass
class Candidate:
    path: str
    title: str
    date: str
    chars: int
    score: float = 0.0        # 本地文本分
    relevance: int = -1       # 模型判定的相关度，-1 表示未筛选
    reason: str = ""
    snippet: str = ""


ProgressFn = Callable[[str, int, int, str], None]


def _noop(stage: str, cur: int, total: int, msg: str) -> None:
    pass


# --------------------------------------------------------------------------
# 第一步：关键词扩展
# --------------------------------------------------------------------------

_RE_TERM_LINE = re.compile(r"^\s*([321])\s*[|｜]\s*(.+?)\s*$", re.M)


def expand_terms(topic: str, *, backend: str = "cli", api_key: str = "", model: str = "",
                 api_base: str = "", timeout: int = 180, stop_flag=None) -> list[tuple[str, int]]:
    """把主题扩成带权检索词；模型不可用时退回主题本身的切词。"""
    terms: list[tuple[str, int]] = []
    try:
        raw = llm.complete(EXPAND_PROMPT.format(topic=topic), backend, api_key=api_key,
                           model=model, api_base=api_base, max_tokens=1200, timeout=timeout,
                           stop_flag=stop_flag)
        for m in _RE_TERM_LINE.finditer(raw):
            weight = int(m.group(1))
            # 模型有时会把「英文|中文」塞在同一行，这里一律拆开
            for piece in re.split(r"[|｜/、,，;；]+", m.group(2)):
                term = piece.strip().strip("`\"'（）()")
                if 1 < len(term) <= 24 and not term.isdigit():
                    terms.append((term, weight))
    except llm.Stopped:
        raise  # Stopped 也是 LLMError，别被下面当成"模型不可用"吞掉
    except llm.LLMError:
        terms = []

    if not terms:
        for piece in re.split(r"[\s,，、;；/]+", topic):
            piece = piece.strip()
            if len(piece) > 1:
                terms.append((piece, 3))

    # 去重时保留权重最高的那次
    best: dict[str, int] = {}
    for t, w in terms:
        key = t.lower()
        best[key] = max(best.get(key, 0), w)
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:30]


# --------------------------------------------------------------------------
# 第二步：本地全文打分
# --------------------------------------------------------------------------

def _is_ascii_term(t: str) -> bool:
    return all(ord(c) < 128 for c in t)


def _build_ascii_regex(terms: list[str]) -> Optional[re.Pattern]:
    """把所有英文词编成一条带词边界的交替正则，一遍扫完，避免逐词重复扫描全文。"""
    if not terms:
        return None
    parts = [f"(?P<t{i}>{re.escape(t)})" for i, t in enumerate(terms)]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.I)


def _snippet(text: str, terms: list[str], width: int = 240) -> str:
    """取第一个命中词周围的片段，供模型判断相关度。"""
    low = text.lower()
    pos = -1
    for t in terms:
        p = low.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return re.sub(r"\s+", " ", text[:width]).strip()
    start = max(0, pos - width // 3)
    return re.sub(r"\s+", " ", text[start:start + width]).strip()


def score_notes(root: str, notes: list[dict], terms: list[tuple[str, int]],
                progress: ProgressFn = _noop) -> list[Candidate]:
    """对每篇笔记算一个 BM25 变体分数，标题命中额外加权。"""
    if not terms:
        return []
    words = [t for t, _ in terms]
    weights = {t.lower(): w for t, w in terms}
    ascii_terms = [t for t in words if _is_ascii_term(t)]
    cjk_terms = [t for t in words if not _is_ascii_term(t)]
    ascii_re = _build_ascii_regex(ascii_terms)

    # (note, tf, 长度, 片段)——片段在读正文的这一遍就切好，不把每篇命中笔记的全文
    # （最多 READ_CAP 字）都留在内存里等到最后
    tf_rows: list[tuple[dict, dict[str, int], int, str]] = []
    df: dict[str, int] = {}
    total_len = 0
    total = len(notes)

    for i, n in enumerate(notes, 1):
        if i % 300 == 0 or i == total:
            progress("search", i, total, f"全文检索 {i}/{total}")
        try:
            text = vault.read_note(root, n["path"], max_chars=READ_CAP)
        except (OSError, ValueError):
            continue
        low = text.lower()
        title_low = (n.get("title") or "").lower()

        tf: dict[str, int] = {}
        for t in cjk_terms:
            c = low.count(t.lower())
            if c:
                tf[t.lower()] = c
        if ascii_re:
            for m in ascii_re.finditer(low):
                key = ascii_terms[int(m.lastgroup[1:])].lower()
                tf[key] = tf.get(key, 0) + 1
        if not tf:
            continue

        # 标题命中是很强的信号，等价于正文里多出现 5 次
        for t in words:
            tl = t.lower()
            if tl in title_low and tl in tf:
                tf[tl] += 5

        length = max(1, len(text))
        total_len += length
        for k in tf:
            df[k] = df.get(k, 0) + 1
        tf_rows.append((n, tf, length, _snippet(text, words)))

    if not tf_rows:
        return []

    n_docs = len(tf_rows)
    avg_len = total_len / n_docs
    out: list[Candidate] = []
    for n, tf, length, snippet in tf_rows:
        score = 0.0
        for term, freq in tf.items():
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            norm = freq * (BM25_K1 + 1) / (freq + BM25_K1 * (1 - BM25_B + BM25_B * length / avg_len))
            score += weights.get(term, 1) * idf * norm
        out.append(Candidate(
            path=n["path"], title=n["title"], date=n["date"], chars=n["chars"],
            score=round(score, 3), snippet=snippet,
        ))
    out.sort(key=lambda c: -c.score)
    return out


# --------------------------------------------------------------------------
# 第三步：相关度筛选
# --------------------------------------------------------------------------

_RE_SCREEN_LINE = re.compile(r"^\s*\[?(\d+)\]?\s*[|｜]\s*([0-5])\s*[|｜]\s*(.*?)\s*$", re.M)
SCREEN_BATCH = 15


def screen(topic: str, cands: list[Candidate], *, backend: str = "cli", api_key: str = "",
           model: str = "", api_base: str = "", timeout: int = 300,
           progress: ProgressFn = _noop, stop_flag=None) -> None:
    """就地填 relevance / reason；模型调用失败的批次保持 relevance = -1。"""
    batches = [cands[i:i + SCREEN_BATCH] for i in range(0, len(cands), SCREEN_BATCH)]
    for bi, batch in enumerate(batches, 1):
        progress("screen", bi - 1, len(batches), f"判定相关度 {bi}/{len(batches)}")
        items = "\n\n".join(
            f"[{i}] 标题：{c.title}\n    日期：{c.date or '未注明'}｜位置：{os.path.dirname(c.path) or '.'}\n"
            f"    片段：{c.snippet[:240]}"
            for i, c in enumerate(batch, 1)
        )
        try:
            raw = llm.complete(SCREEN_PROMPT.format(topic=topic, items=items), backend,
                               api_key=api_key, model=model, api_base=api_base,
                               max_tokens=2000, timeout=timeout, stop_flag=stop_flag)
        except llm.Stopped:
            raise
        except llm.LLMError:
            continue
        for m in _RE_SCREEN_LINE.finditer(raw):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(batch):
                batch[idx].relevance = int(m.group(2))
                batch[idx].reason = m.group(3)[:60]
        progress("screen", bi, len(batches), f"判定相关度 {bi}/{len(batches)}")


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------

def find(root: str, topic: str, *, backend: str = "cli", api_key: str = "", model: str = "",
         api_base: str = "", date_from: str = "", folder: str = "", exclude_folder: str = "",
         candidates: int = DEFAULT_CANDIDATES, do_screen: bool = True,
         timeout: int = 300, progress: ProgressFn = _noop, stop_flag=None) -> dict:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("请先填写主题")

    progress("expand", 0, 1, "扩展检索关键词")
    terms = expand_terms(topic, backend=backend, api_key=api_key, model=model,
                         api_base=api_base, timeout=min(timeout, 180), stop_flag=stop_flag)
    progress("expand", 1, 1, f"检索词：{', '.join(t for t, _ in terms[:10])}…")

    def check_stop():
        # 模型调用之间还有扫库、打分这些不调模型的步骤，大库要跑一会儿，也在这里停
        if stop_flag and stop_flag():
            raise llm.Stopped("已停止")

    check_stop()

    notes = vault.scan(root)
    if date_from:
        notes = [n for n in notes if n["date"] and n["date"] >= date_from]
    if folder:
        notes = [n for n in notes if n["folder"] == folder or n["folder"].startswith(folder + "/")]
    if exclude_folder:
        notes = [n for n in notes
                 if not (n["folder"] == exclude_folder or n["folder"].startswith(exclude_folder + "/"))]
    if not notes:
        return {"topic": topic, "terms": terms, "candidates": [], "scanned": 0}

    scored = score_notes(root, notes, terms, progress)
    check_stop()

    # 同一期内容常在库里存两份（结构化整理稿 + 逐字稿），按标题+日期去重，留分高的那份
    seen: set[tuple[str, str]] = set()
    deduped: list[Candidate] = []
    duplicates = 0
    for c in scored:
        key = (re.sub(r"\s+", "", c.title).lower(), c.date)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(c)
    scored = deduped

    top = scored[:max(1, candidates)]

    if do_screen and top:
        screen(topic, top, backend=backend, api_key=api_key, model=model,
               api_base=api_base, timeout=timeout, progress=progress, stop_flag=stop_flag)
        # 模型没给出判定的，按文本分位置给个保守的默认值，避免整批被丢掉
        for c in top:
            if c.relevance < 0:
                c.relevance = 2
        top.sort(key=lambda c: (-c.relevance, -c.score))

    return {
        "topic": topic,
        "terms": [{"term": t, "weight": w} for t, w in terms],
        "scanned": len(notes),
        "matched": len(scored),
        "duplicates": duplicates,
        "candidates": [c.__dict__ for c in top],
    }
