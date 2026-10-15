"""
主题检索：给一个主题，从整库里找出最相关的笔记。

两段式，先便宜后贵：
  1. 关键词扩展（1 次模型调用）：把主题扩成中英文混合的检索词，带权重
  2. 本地全文打分（不花钱）：BM25 变体，中文用子串计数，英文用带词边界的单遍扫描
  3. 相关度筛选（几次模型调用）：只对打分靠前的候选，看标题+命中片段判相关度

最终按 (相关度, 文本分) 排序，交给报告流水线。
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
import time
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

EXPAND_PROMPT = """你是检索助手。用户要在自己的中英文混排技术笔记库（科技播客整理稿、行业文章、论文、大会记录）里，找出与下面主题相关的笔记。

主题：{topic}
{contexts}{activity}
## 第一步：判断主题指什么
- 主题是缩写或多义词时，列出它在这个库里实际出现的各种含义（依据上面的原文片段）；
  用户近期关注明显指向某个片段里没有的含义时，也把它列出来；这个词广为人知的其他含义
  （比如 RSI 在金融里是相对强弱指标）即使片段里没有，也列出来，片段编号写 -，方便用户改选。
  真没有歧义的（比如"推理成本"）就只列一个。
- 最多 4 个含义，用 A、B、C、D 编号，每个一行：
  `含义|编号|简短名称（不超过 12 个字）|一句话说明|属于这个含义的片段编号，逗号分隔，没有就写 -`
- 每个片段都归到某一个含义里。
- 再写一行 `近期|编号`：用户近期的检索、报告、订阅明显指向哪个含义；看不出来就写 `近期|-`。
- 示例（行首的「含义」「近期」不能省）：
  含义|A|混合专家模型|大模型里的 Mixture of Experts 架构|1,2,4
  含义|B|人名|片段里人名的一部分|3
  近期|A

## 第二步：给每个含义写检索词（用于全文关键词匹配）
- 一行一个，格式严格为 `编号|权重|检索词`，权重只能是 3、2、1。
  3 = 这个含义的核心概念（命中基本就相关）；2 = 强相关术语、代表性公司/产品/技术名；1 = 弱相关线索词。
- 中英文都要给：同一个概念的中文说法和英文说法**各占一行**，不要写在同一行里。
- 给**具体**的词：公司名、产品名、芯片名、模型名、协议名、指标名。不要给"技术""趋势""发展""影响"这类泛词。
- 不要给单个汉字，不要给长句：中文每个词不超过 12 个字，英文不超过 4 个单词。
- 缩写要同时给出中英文全称和常见的其他写法。
- 片段只用来判断含义：片段里顺带出现的人名、节目名、公司名不要当检索词，除非它本身就是这个含义公认的代表。
- 每个含义 15-25 个词。

只输出上面这些行，不要解释。
"""

# 判断含义时看多少段库里的原文：近期、较早各取一半，每段多长
CONTEXT_SNIPPETS = 14
CONTEXT_WIDTH = 160
# 多少天以内的笔记算"近期"，近期笔记在估算含义占比时的权重
RECENT_NOTE_DAYS = 90
RECENT_WEIGHT = 3.0
# 占比超过这个值才自动定含义，不然请用户选
AUTO_SHARE = 0.8

SCREEN_PROMPT = """你是检索筛选助手。当前主题是：

**{topic}**{meaning}

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

_RE_TERM_LINE = re.compile(r"^\s*(?:([A-D])\s*[|｜]\s*)?([321])\s*[|｜]\s*(.+?)\s*$", re.M)
_RE_IDS = re.compile(r"^[\d,，、\s\-–—无]*$")
_RE_RECENT_LINE = re.compile(r"^\s*近期\s*[|｜]\s*([A-D-])", re.M)


def _topic_tokens(topic: str) -> list[str]:
    return [p.strip() for p in re.split(r"[\s,，、;；/]+", topic) if len(p.strip()) > 1]


def _topic_patterns(topic: str) -> list[re.Pattern]:
    pats = []
    for t in _topic_tokens(topic) or [topic.strip()]:
        if _is_ascii_term(t):
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.I))
        else:
            pats.append(re.compile(re.escape(t), re.I))
    return pats


def _note_day(n: dict) -> str:
    """笔记日期：头部写了就用头部的，没写用文件修改时间。"""
    if n.get("date"):
        return n["date"]
    mt = n.get("mtime")
    return time.strftime("%Y-%m-%d", time.localtime(mt)) if mt else ""


def collect_contexts(root: str, topic: str, notes: list[dict], *, limit: int = CONTEXT_SNIPPETS,
                     width: int = CONTEXT_WIDTH, recent_days: int = RECENT_NOTE_DAYS,
                     today: Optional[str] = None) -> dict:
    """找出主题词原样出现的全部笔记，分近期/较早两层各抽一半片段。
    缩写和多义词（RSI、MoE、Agent）光看字面模型会按最常见的领域去理解，给它看库里的
    实际用法才判得对；分两层抽样，是因为人在不同时期关注的东西不一样，近期用法要单独看。
    每层按日期从新到旧、在不同节目/目录之间轮流取，免得几段全出自同一个地方。"""
    pats = _topic_patterns(topic)
    today = today or time.strftime("%Y-%m-%d")
    cut = (_dt.date.fromisoformat(today) - _dt.timedelta(days=recent_days)).isoformat()

    hits = {"recent": [], "older": []}
    for n in notes:
        title = n.get("title") or ""
        try:
            text = vault.read_note(root, n["path"], max_chars=READ_CAP)
        except (OSError, ValueError):
            continue
        m = None
        for p in pats:
            m = p.search(text) or None
            if m:
                break
        if not m:
            continue
        day = _note_day(n)
        start = max(0, m.start() - width // 2)
        hits["recent" if day and day >= cut else "older"].append({
            "text": re.sub(r"\s+", " ", text[start:start + width]).strip(),
            "date": day, "folder": n.get("folder") or os.path.dirname(n["path"]),
            "title_key": re.sub(r"\s+", "", title).lower(),
        })

    def sample(rows: list[dict], k: int) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for r in sorted(rows, key=lambda r: r["date"], reverse=True):
            # 按节目/来源分组：Spark/Dwarkesh/notes 和 Spark/Dwarkesh/speech 算同一个来源
            src = "/".join(r["folder"].split("/")[:2])
            groups.setdefault(src, []).append(r)
        out, seen = [], set()
        while len(out) < k and any(groups.values()):
            for src in list(groups):
                while groups[src]:
                    r = groups[src].pop(0)
                    if r["title_key"] not in seen:   # 同一期的整理稿和逐字稿只取一份
                        seen.add(r["title_key"])
                        out.append(r)
                        break
                if len(out) >= k:
                    break
        return out

    half = max(1, limit // 2)
    rec = sample(hits["recent"], half)
    old = sample(hits["older"], limit - len(rec))
    if len(old) < limit - half:   # 较早的不够，用近期的补
        rec = sample(hits["recent"], limit - len(old))
    samples = [dict(r, recent=True) for r in rec] + [dict(r, recent=False) for r in old]
    for r in samples:
        r.pop("title_key", None)
    return {"samples": samples, "recent_hits": len(hits["recent"]), "older_hits": len(hits["older"]),
            "recent_since": cut}


def topic_contexts(root: str, topic: str, notes: list[dict], *,
                   limit: int = CONTEXT_SNIPPETS, width: int = CONTEXT_WIDTH) -> list[str]:
    """只要片段文字（生成报告关注点时用）。"""
    return [r["text"] for r in collect_contexts(root, topic, notes, limit=limit, width=width)["samples"]]


def _take_sense_lines(raw: str, senses: dict, n_samples: int) -> str:
    """认出含义行，填进 senses，返回去掉含义行之后的文本（剩下的给检索词解析）。
    标准格式是 `含义|A|名称|说明|片段编号`；模型偶尔省掉「含义」、或在编号后面多塞一个
    序号（`A|1|名称|说明|1,2,3`），这种和检索词行 `A|3|词` 长得像，按「至少四段、最后一段
    是片段编号」来认。"""
    rest = []
    for line in raw.splitlines():
        parts = [p.strip() for p in re.split(r"[|｜]", line.strip())]
        if parts and re.fullmatch(r"含义", parts[0]):
            parts = parts[1:]
        is_sense = (len(parts) >= 4 and re.fullmatch(r"[A-D]", parts[0] or "")
                    and _RE_IDS.match(parts[-1] or "-") and not line.strip().startswith("近期"))
        if not is_sense:
            rest.append(line)
            continue
        key, fields = parts[0], parts[1:-1]
        if len(fields) >= 3 and fields[0].isdigit():
            fields = fields[1:]   # 多塞的序号
        label = fields[0] if fields else ""
        if not label or label.isdigit():
            rest.append(line)   # 其实是 `A|2|H100|2024` 这种检索词行
            continue
        meaning = "｜".join(fields[1:])
        if key in senses:
            continue
        ids = [int(x) for x in re.findall(r"\d+", parts[-1]) if 1 <= int(x) <= n_samples]
        senses[key] = {"key": key, "label": label[:30], "meaning": meaning[:200], "snippets": ids}
    return "\n".join(rest)


def _clean_terms(pairs: list[tuple[str, int]], topic: str) -> list[tuple[str, int]]:
    # 主题词本身一定要在检索词里：模型偶尔只给全称、漏了用户写的缩写
    pairs = list(pairs) + [(p, 3) for p in _topic_tokens(topic)]
    best: dict[str, int] = {}
    for t, w in pairs:
        key = t.lower()
        best[key] = max(best.get(key, 0), w)   # 去重时保留权重最高的那次
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:30]


def _decide(senses: list[dict], ctx: dict, recent_pick: str) -> tuple[str, str, str]:
    """按库里的用法（近期加权）和用户近期关注，决定自动用哪个含义还是请用户选。
    返回 (decision, chosen_key, reason)，decision 是 auto 或 ask。"""
    if len(senses) == 1:
        return "auto", senses[0]["key"], ""
    keys = [s["key"] for s in senses]
    by = {s["key"]: s for s in senses}
    score = {k: RECENT_WEIGHT * by[k]["est_recent"] + by[k]["est_older"] for k in keys}
    total = sum(score.values())
    if total <= 0:
        # 库里没有这个词，只能看近期关注
        if recent_pick in by:
            return "auto", recent_pick, f"库里没有直接出现这个词，按你近期的关注理解为「{by[recent_pick]['label']}」"
        return "ask", "", "库里没有直接出现这个词，它有几种常见含义"
    top = max(keys, key=lambda k: score[k])
    rec_top = max(keys, key=lambda k: by[k]["est_recent"])
    old_top = max(keys, key=lambda k: by[k]["est_older"])
    if recent_pick in by and recent_pick != top:
        return ("ask", "",
                f"库里多数按「{by[top]['label']}」用，但你近期的检索/报告/订阅更像在关注「{by[recent_pick]['label']}」")
    if rec_top != old_top and by[rec_top]["est_recent"] >= 2 and by[old_top]["est_older"] >= 2:
        return ("ask", "",
                f"最近 {RECENT_NOTE_DAYS} 天的笔记以「{by[rec_top]['label']}」为主，更早的以「{by[old_top]['label']}」为主")
    share = score[top] / total
    if share >= AUTO_SHARE:
        return "auto", top, ""
    return "ask", "", "库里几种含义都有不少"


def analyze_topic(topic: str, *, backend: str = "cli", api_key: str = "", model: str = "",
                  api_base: str = "", timeout: int = 180, stop_flag=None,
                  ctx: Optional[dict] = None, activity: Optional[list[str]] = None) -> dict:
    """判断主题含义并给每个含义扩检索词（一次模型调用）。
    返回 {"senses": [...], "decision": "auto"/"ask", "chosen": 编号, "reason": 说明}。
    每个 sense：key/label/meaning/terms/est_recent/est_older（按抽样比例估的近期/较早笔记数）。
    模型不可用时退回主题本身的切词，当成只有一个含义。"""
    ctx = ctx or {"samples": [], "recent_hits": 0, "older_hits": 0}
    samples = ctx["samples"]
    ctx_text = ""
    if samples:
        ctx_text = "\n库里出现这个词的原文片段（编号、日期）：\n" + "\n".join(
            f"[{i}] ({r['date'] or '日期不详'}{'，近期' if r['recent'] else ''}) {r['text']}"
            for i, r in enumerate(samples, 1)) + "\n"
    else:
        ctx_text = "\n库里没有找到这个词原样出现的地方，按常识判断含义。\n"
    act_text = ""
    if activity:
        act_text = "\n用户最近的关注（检索、报告、订阅）：\n" + "\n".join(f"- {a}" for a in activity[:30]) + "\n"

    senses: dict[str, dict] = {}
    terms: dict[str, list[tuple[str, int]]] = {}
    recent_pick = ""
    try:
        raw = llm.complete(EXPAND_PROMPT.format(topic=topic, contexts=ctx_text, activity=act_text),
                           backend, api_key=api_key, model=model, api_base=api_base,
                           max_tokens=2500, timeout=timeout, stop_flag=stop_flag)
        raw = _take_sense_lines(raw, senses, len(samples))
        rm = _RE_RECENT_LINE.search(raw)
        recent_pick = rm.group(1) if rm and rm.group(1) != "-" else ""
        for m in _RE_TERM_LINE.finditer(raw):
            key, weight = m.group(1) or "A", int(m.group(2))
            # 模型有时会把「英文|中文」塞在同一行，这里一律拆开
            for piece in re.split(r"[|｜/、,，;；]+", m.group(3)):
                term = piece.strip().strip("`\"'（）()")
                if 1 < len(term) <= 40 and not term.isdigit():
                    terms.setdefault(key, []).append((term, weight))
    except llm.Stopped:
        raise  # Stopped 也是 LLMError，别被下面当成"模型不可用"吞掉
    except llm.LLMError:
        senses, terms = {}, {}

    # 有检索词却没写含义行（老格式或模型没照做）：当成一个含义
    for key in terms:
        senses.setdefault(key, {"key": key, "label": topic[:30], "meaning": "", "snippets": []})
    senses = {k: v for k, v in senses.items() if terms.get(k)}
    if not senses:
        senses = {"A": {"key": "A", "label": topic[:30], "meaning": "", "snippets": []}}

    # 按抽样比例估每个含义在近期/较早各有多少篇
    n_rec = sum(1 for r in samples if r["recent"])
    n_old = len(samples) - n_rec
    out = []
    for key in sorted(senses):
        sv = senses[key]
        rec = sum(1 for i in sv["snippets"] if samples[i - 1]["recent"])
        old = len(sv["snippets"]) - rec
        sv["est_recent"] = round(ctx["recent_hits"] * rec / n_rec) if n_rec else 0
        sv["est_older"] = round(ctx["older_hits"] * old / n_old) if n_old else 0
        sv["terms"] = _clean_terms(terms.get(key, []), topic)
        out.append(sv)
    decision, chosen, reason = _decide(out, ctx, recent_pick)
    return {"senses": out, "decision": decision, "chosen": chosen, "reason": reason,
            "recent_pick": recent_pick}


def expand_terms(topic: str, *, backend: str = "cli", api_key: str = "", model: str = "",
                 api_base: str = "", timeout: int = 180, stop_flag=None,
                 ctx: Optional[dict] = None, activity: Optional[list[str]] = None,
                 info: Optional[dict] = None) -> list[tuple[str, int]]:
    """只要检索词时用：取自动定下的含义，要请用户选时取第一个。含义写进 info["meaning"]。"""
    a = analyze_topic(topic, backend=backend, api_key=api_key, model=model, api_base=api_base,
                      timeout=timeout, stop_flag=stop_flag, ctx=ctx, activity=activity)
    sense = next((s for s in a["senses"] if s["key"] == a["chosen"]), a["senses"][0])
    if info is not None:
        info["meaning"] = sense["meaning"]
        info["analysis"] = a
    return sense["terms"]


# --------------------------------------------------------------------------
# 第二步：本地全文打分
# --------------------------------------------------------------------------

def _is_ascii_term(t: str) -> bool:
    return all(ord(c) < 128 for c in t)


def _build_ascii_regex(terms: list[str]) -> Optional[re.Pattern]:
    """把所有英文词编成一条带词边界的交替正则，一遍扫完，避免逐词重复扫描全文。
    词尾允许复数（agents、GPUs、data centers），词中的空格和连字符互通
    （recursive self-improvement / self improvement）——不然这些写法整篇漏掉。"""
    if not terms:
        return None

    def body(t: str) -> str:
        pieces = [re.escape(p) for p in re.split(r"[\s\-]+", t.strip()) if p]
        return r"[\s\-]?".join(pieces) if len(pieces) > 1 else (pieces[0] if pieces else re.escape(t))

    parts = [f"(?P<t{i}>{body(t)}(?:e?s)?)" for i, t in enumerate(terms)]
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

    # idf 的 N 是扫过的全部笔记数，不是命中数：按命中数算的话，主题核心词几乎每篇命中
    # 笔记都有，df ≈ N，idf ≈ 0，权重 3 乘上去也近乎 0，排序全由边缘词决定
    n_docs = max(len(notes), len(tf_rows))
    avg_len = total_len / len(tf_rows)
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
           progress: ProgressFn = _noop, stop_flag=None, meaning: str = "") -> None:
    """就地填 relevance / reason；模型调用失败的批次保持 relevance = -1。
    meaning 是扩展检索词时模型对主题的理解，带上它，缩写主题不会按别的含义判。"""
    batches = [cands[i:i + SCREEN_BATCH] for i in range(0, len(cands), SCREEN_BATCH)]
    for bi, batch in enumerate(batches, 1):
        progress("screen", bi - 1, len(batches), f"判定相关度 {bi}/{len(batches)}")
        items = "\n\n".join(
            f"[{i}] 标题：{c.title}\n    日期：{c.date or '未注明'}｜位置：{os.path.dirname(c.path) or '.'}\n"
            f"    片段：{c.snippet[:240]}"
            for i, c in enumerate(batch, 1)
        )
        try:
            raw = llm.complete(SCREEN_PROMPT.format(topic=topic, items=items,
                                                     meaning=f"（指：{meaning}）" if meaning else ""),
                               backend,
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

# 相关（≥3 分）的少于这么多篇，就提示素材不足
MIN_GOOD = 3


def _public_sense(s: dict) -> dict:
    """给前端的含义：检索词转成 {term, weight}，去掉内部用的片段编号。"""
    return {"key": s["key"], "label": s["label"], "meaning": s["meaning"],
            "est_recent": s.get("est_recent", 0), "est_older": s.get("est_older", 0),
            "terms": [{"term": t, "weight": w} for t, w in s["terms"]]}


# --------------------------------------------------------------------------
# 编排（主流程）
# --------------------------------------------------------------------------

def find(root: str, topic: str, *, backend: str = "cli", api_key: str = "", model: str = "",
         api_base: str = "", date_from: str = "", folder: str = "", exclude_folder: str = "",
         candidates: int = DEFAULT_CANDIDATES, do_screen: bool = True,
         timeout: int = 300, progress: ProgressFn = _noop, stop_flag=None,
         activity: Optional[list[str]] = None, sense: Optional[dict] = None,
         senses: Optional[list[dict]] = None) -> dict:
    """sense 是用户点选的含义（带检索词），给了就不再判断含义、不再扩展检索词；
    senses 是上一次判断出的全部含义，原样带回结果里，前端好继续给「改按某含义检索」。
    主题有歧义又定不下来时，返回 need_choice 和各个含义，不做后面的打分和筛选。"""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("请先填写主题")

    def check_stop():
        # 模型调用之间还有扫库、打分这些不调模型的步骤，大库要跑一会儿，也在这里停
        if stop_flag and stop_flag():
            raise llm.Stopped("已停止")

    notes = vault.scan(root)
    if date_from:
        notes = [n for n in notes if n["date"] and n["date"] >= date_from]
    if folder:
        notes = [n for n in notes if n["folder"] == folder or n["folder"].startswith(folder + "/")]
    if exclude_folder:
        notes = [n for n in notes
                 if not (n["folder"] == exclude_folder or n["folder"].startswith(exclude_folder + "/"))]
    if not notes:
        return {"topic": topic, "terms": [], "candidates": [], "scanned": 0}

    if sense:
        chosen = sense
        all_senses = senses or [sense]
        intent_reason = ""
        progress("expand", 1, 1, f"按「{sense.get('label', '')}」检索")
    else:
        progress("expand", 0, 1, "看主题词在库里怎么用")
        ctx = collect_contexts(root, topic, notes)
        check_stop()
        progress("expand", 0, 1, "判断主题含义、扩展检索词")
        a = analyze_topic(topic, backend=backend, api_key=api_key, model=model,
                          api_base=api_base, timeout=min(timeout, 180), stop_flag=stop_flag,
                          ctx=ctx, activity=activity)
        all_senses = [_public_sense(s) for s in a["senses"]]
        intent_reason = a["reason"]
        if a["decision"] == "ask":
            progress("expand", 1, 1, "主题有几种含义，请选一个")
            return {"topic": topic, "need_choice": True, "intent_reason": intent_reason,
                    "senses": all_senses, "terms": [], "candidates": [], "scanned": len(notes),
                    "matched": 0}
        chosen = next(s for s in all_senses if s["key"] == a["chosen"])
    terms = [(t["term"], int(t["weight"])) for t in chosen.get("terms") or []]
    meaning = chosen.get("meaning", "")
    progress("expand", 1, 1, (f"主题理解为：{chosen.get('label', '')}；" if len(all_senses) > 1 else "") +
             f"检索词：{', '.join(t for t, _ in terms[:10])}…")
    check_stop()

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
               api_base=api_base, timeout=timeout, progress=progress, stop_flag=stop_flag,
               meaning=meaning)
        # 模型没给出判定的，按文本分位置给个保守的默认值，避免整批被丢掉
        for c in top:
            if c.relevance < 0:
                c.relevance = 2
        top.sort(key=lambda c: (-c.relevance, -c.score))

    # 按这个含义库里几乎没有资料（新的兴趣还没来得及攒素材）：明说，别拿噪音凑一份报告
    good = sum(1 for c in top if c.relevance >= 3) if (do_screen and top) else len(scored)
    insufficient = ""
    if good < MIN_GOOD:
        insufficient = (f"库里按「{chosen.get('label') or topic}」相关的资料只有 {good} 篇，"
                        f"不够写一份像样的报告，可以先用「信息跟进」收集一批再来")

    return {
        "topic": topic,
        "meaning": meaning,
        "chosen": chosen.get("key", ""),
        "senses": all_senses,
        "intent_reason": intent_reason,
        "insufficient": insufficient,
        "terms": [{"term": t, "weight": w} for t, w in terms],
        "scanned": len(notes),
        "matched": len(scored),
        "duplicates": duplicates,
        "candidates": [c.__dict__ for c in top],
    }
