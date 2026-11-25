"""
复盘里的「讨论」：基于笔记库和云端模型多轮问答。服务端不存会话，每次请求由客户端带上之前的问答。

一轮：用一次小的模型调用把问题（连同前几轮的要点）变成 3 到 6 个检索词 → 全文索引（core/search_index）按「任一词命中」
取片段，同一篇最多 2 个片段、最多 12 篇 → 材料和问题一起交给模型回答，结论后面标材料编号 [n]。
范围限定在一个标签时，只留这个标签下的文章（core/tags.items_for），命中太少就用这个标签下最近几篇的一句话补足。
存成复盘：整段讨论写成 output/复盘/复盘_讨论_….md，种类「讨论」，之后照常能划线、继续追问。

手机上同一套提示词和存盘格式（apps/mobile-ios/Spark/DiscussPrompt.swift），tests/test_review_discuss.py 用 swiftc 逐字对照。
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from core import review, search_index, tags, vault

MAX_DOCS = 12           # 材料最多几篇
MAX_PER_DOC = 2         # 同一篇最多留几个片段
SNIPPET_CHARS = 600     # 每个片段截到多长
MAX_TERMS = 6           # 检索词最多几个
MAX_TERM_CHARS = 20     # 一个检索词最长
MAX_HISTORY_TURNS = 6   # 提示词里带最近几轮问答
HISTORY_ANSWER_CHARS = 800
QUERY_TURNS = 3         # 提检索词时带最近几轮
QUERY_ANSWER_CHARS = 200
TAG_MIN_DOCS = 3        # 标签范围内命中少于这么多篇，用最近几篇的一句话补
TAG_FILL_DOCS = 6       # 补到这么多篇
TAG_SEARCH_ROWS = 400   # 标签范围：先多取一些片段再过滤
LIB_SEARCH_ROWS = 80
MAX_QUESTION_CHARS = review.MAX_QUESTION_CHARS
MAX_SAVE_TURNS = 30
MAX_ANSWER_CHARS = 30000
MAX_SOURCES = MAX_DOCS
SKIP_KINDS = ("复盘", "报告", "总览页")   # 不当材料：自己写的报告、复盘越滚越自我引用
NOT_FOUND = "笔记库里没找到相关内容，换个说法试试。"

_SPLIT_CHARS = set(" \t\r\n，,、;；。！!？?：:（）()【】「」《》\"“”")


# ---------------------------------------------------------------- 提示词（手机 DiscussPrompt.swift 逐字一致）

def _cut(text: str, n: int) -> str:
    return review._cut(text, n)


def strip_cites(text: str) -> str:
    """历史回答里的 [n] 是上一轮材料的编号，这一轮的编号重新排，带进提示词只会混淆。"""
    return re.sub(r"\[\d+\]", "", text)


def _turns(history: list[dict], keep: int, chars: int) -> str:
    return "\n\n".join(f"问：{t['q']}\n答：{_cut(strip_cites(t['a']), chars)}" for t in history[-keep:])


def query_prompt(question: str, history: list[dict]) -> str:
    past = f"\n===== 之前的对话（要点） =====\n{_turns(history, QUERY_TURNS, QUERY_ANSWER_CHARS)}\n" if history else ""
    return f"""我在笔记库里提问。请把我最新的问题变成 {MAX_TERMS - 3} 到 {MAX_TERMS} 个检索词，用来在笔记库里全文搜索。

规则：
- 检索词是名词性的短词，每个 2 到 8 个字：人名、公司、产品、技术、概念。英文词和缩写原样写。
- 不写「什么」「怎么样」「为什么」这类疑问词，不写整句话。
- 问题里的「它」「这个」「那家公司」，用之前对话里说到的具体名词代替。
- 同一个意思有常见的另一种写法（中英文、简称全称）时，补一两个。

只输出一行：检索词之间用一个空格分开。不写标点，不写解释，不写编号。
{past}
===== 我最新的问题 =====
{question}
"""


def parse_terms(raw: str, question: str) -> list[str]:
    """模型输出的第一个非空行按空格和标点拆成检索词；拆不出来就用问题本身拆（长的一段每 4 个字切一个）。"""
    line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    terms = _clean_terms(_pieces(line))
    if terms:
        return terms
    out = []
    for p in _pieces(question):
        out += [p[i:i + 4] for i in range(0, len(p), 4)] if len(p) > 8 else [p]
    return _clean_terms(out)


def _pieces(text: str) -> list[str]:
    out, cur = [], ""
    for ch in text:
        if ch in _SPLIT_CHARS:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _clean_terms(pieces: list[str]) -> list[str]:
    seen, out = set(), []
    for p in pieces:
        p = p.strip()
        k = p.lower()
        if len(p) < 2 or len(p) > MAX_TERM_CHARS or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out[:MAX_TERMS]


def material_block(i: int, m: dict) -> str:
    """m：type（剪藏、单集小结…）、source（《标题》（来源，日期））、tldr、snippets。"""
    part = [f"[{i}] {m['type']}　{m['source']}"]
    if m["tldr"]:
        part.append(f"一句话：{m['tldr']}")
    part += [f"摘录：\n{s}" for s in m["snippets"]]
    return "\n".join(part)


def answer_prompt(question: str, history: list[dict], mats: list[dict], tag: str, style: str) -> str:
    scope = f"（范围限定在「{tag}」这个主题下的文章）" if tag else ""
    past = f"\n===== 之前的对话 =====\n{_turns(history, MAX_HISTORY_TURNS, HISTORY_ANSWER_CHARS)}\n" if history else ""
    blocks = "\n".join("\n" + material_block(i, m) for i, m in enumerate(mats, 1))
    return f"""下面是我笔记库里的材料{scope}，以及我们之前的对话。请只根据这些材料，回答我最后的问题。

{style}

这次回答的补充要求（和上面冲突时，以这里为准）：
- 只用材料里写的。材料里没有的，直接说笔记库里没有这方面的内容，不要用常识补，不要编。
- 出处只写材料编号：每条结论后面写 [编号]，比如 [2]；一条结论用了几篇就连着写几个，比如 [1][3]。编号只能用下面材料里有的，不要自己造。
- 不分节，不加标题，用短段落或列表。先给结论，再说依据。
- 材料之间说法不一致的，直接指出来，各自写明出处。
{past}
===== 材料（共 {len(mats)} 篇）=====
{blocks}

===== 我最后的问题 =====
{question}
"""


def cited(answer: str, count: int) -> list[int]:
    """回答里写到的材料编号（按第一次出现的先后，去重，只留 1 到 count 的）。"""
    out: list[int] = []
    for m in re.finditer(r"\[(\d+)\]", answer):
        n = int(m.group(1))
        if 1 <= n <= count and n not in out:
            out.append(n)
    return out


# ---------------------------------------------------------------- 找材料

def _origin(rel: str, fields: dict) -> str:
    if fields.get("source"):
        return fields["source"]
    parts = rel.split("/")
    if parts[0] == vault.SPARK_DIRNAME and len(parts) > 2:
        if parts[1] == "信息跟进" and len(parts) > 3:
            return parts[2]
        return parts[1]
    return "报告" if parts[0] == vault.REPORTS_DIRNAME else "笔记库"


def _doc_info(root: str, rel: str, row: dict) -> dict:
    """一篇的标题、来源、日期、一句话：读文件头；读不到就用索引里的。"""
    info = {"path": rel, "title": row.get("title") or rel, "kind": row.get("kind") or "", "date": row.get("date") or "",
            "source": _origin(rel, {}), "tldr": ""}
    try:
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return info
    doc = search_index.parse_doc(rel, text)
    fields = search_index._frontmatter(text)[1]
    info.update(title=doc["title"], kind=doc["kind"], date=doc["date"], tldr=doc["summary"], source=_origin(rel, fields))
    return info


def _material(info: dict, snippets: list[str]) -> dict:
    day = info["date"].replace("-", "")
    src = review._source({"title": info["title"], "show": info["source"], "date": day})
    return dict(info, type=info["kind"], origin=info["source"], source=src, snippets=snippets)


def source_url(rel: str) -> str:
    """阅读页里打开这篇的地址：Spark/ 下的走 /read/f/，output/ 下的走 /read/r/（同 apps/reader 的 _url）。"""
    head, _, tail = rel.partition("/")
    return f"/read/{'r' if head == vault.REPORTS_DIRNAME else 'f'}/{quote(tail)}"


def gather(terms: list[str], tag: str = "", root: str = "") -> list[dict]:
    """检索词 → 材料（最多 MAX_DOCS 篇，每篇最多 MAX_PER_DOC 个片段，按相关度排）。"""
    root = root or vault.vault_root()
    allowed = None
    if tag:
        hits = tags.items_for(tag, root)
        allowed = {f"{vault.SPARK_DIRNAME}/{h['path']}" for h in hits}
    try:
        rows = search_index.get_index(vault=root).search_chunks(
            " ".join(terms), limit=TAG_SEARCH_ROWS if tag else LIB_SEARCH_ROWS,
            kinds=[k for k in search_index.KINDS if k not in SKIP_KINDS], mode="any")
    except search_index.IndexUnavailable as e:
        raise review.ReviewError(f"全文索引还没建好，稍后再试（{e}）") from e
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        if allowed is not None and r["path"] not in allowed:
            continue
        if r["path"] not in grouped:
            if len(order) >= MAX_DOCS:
                continue
            order.append(r["path"])
            grouped[r["path"]] = []
        if len(grouped[r["path"]]) < MAX_PER_DOC:
            grouped[r["path"]].append(r)
    mats = [_material(_doc_info(root, p, grouped[p][0]), [_cut(r["text"], SNIPPET_CHARS) for r in grouped[p]]) for p in order]
    if tag and len(mats) < TAG_MIN_DOCS:
        have = set(order)
        for h in hits:
            if len(mats) >= TAG_FILL_DOCS:
                break
            rel = f"{vault.SPARK_DIRNAME}/{h['path']}"
            if rel in have or not h["summary"]:
                continue
            info = {"path": rel, "title": h["title"], "kind": review.TAG_TYPE_LABELS.get(h["type"], h["type"]),
                    "date": h["date"], "source": h["source"], "tldr": h["summary"]}
            mats.append(_material(info, []))
    return mats


def _source_row(n: int, m: dict) -> dict:
    return {"n": n, "title": m["title"], "path": m["path"], "kind": m["kind"], "date": m["date"], "source": m["origin"],
            "url": source_url(m["path"])}


# ---------------------------------------------------------------- 一轮问答

def clean_history(raw: Any) -> list[dict]:
    """客户端带来的之前的问答：只收 {q, a} 都是字符串的，最多取最近 MAX_HISTORY_TURNS 轮。格式不对抛 ReviewError。"""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SAVE_TURNS:
        raise review.ReviewError("history 应该是之前的问答列表")
    out = []
    for t in raw:
        if not isinstance(t, dict) or not isinstance(t.get("q"), str) or not isinstance(t.get("a"), str) \
                or len(t["q"]) > MAX_QUESTION_CHARS or len(t["a"]) > MAX_ANSWER_CHARS:
            raise review.ReviewError("history 里每一轮要有 q 和 a，问题不能太长")
        out.append({"q": t["q"].strip(), "a": t["a"].strip()})
    return out[-MAX_HISTORY_TURNS:]


def check_question(question: Any) -> str:
    q = question.strip() if isinstance(question, str) else ""
    if not q or len(q) > MAX_QUESTION_CHARS:
        raise review.ReviewError(f"问题不能是空的，也不能超过 {MAX_QUESTION_CHARS} 字")
    return q


def ask(question: str, history: list[dict], tag: str = "", progress: review.Progress = lambda s: None) -> dict:
    """问一个问题。返回 {answer, sources}；一篇材料都没找到时不调回答的模型，answer 是 NOT_FOUND。"""
    progress("想检索词")
    terms = parse_terms(review._llm(query_prompt(question, history), max_tokens=300), question)
    progress("在笔记库里找相关内容")
    mats = gather(terms, tag)
    if not mats:
        return {"answer": NOT_FOUND, "sources": [], "terms": terms}
    progress(f"用 {len(mats)} 篇材料写回答")
    answer = review._llm(answer_prompt(question, history, mats, tag, review._STYLE), max_tokens=4000).strip()
    return {"answer": answer, "sources": [_source_row(n, mats[n - 1]) for n in cited(answer, len(mats))], "terms": terms}


# ---------------------------------------------------------------- 存成复盘（手机 DiscussPrompt.document 同一个渲染）

def _flat(text: str) -> str:
    return " ".join(text.split())


def short_question(q: str, n: int = 30) -> str:
    q = _flat(q)
    return q if len(q) <= n else q[:n] + "…"


def source_label(s: dict) -> str:
    day = s["date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s["date"] or "") else "日期不详"
    inner = f"{s['source']}，{day}" if s["source"] else day
    return f"《{_flat(s['title'])}》（{_flat(inner)}）"


def _source_line(s: dict) -> str:
    label = source_label(s)
    path = s["path"]
    if any(c in path for c in "|[]\n\r") or not path.endswith(".md"):
        return f"- [{s['n']}] {label}"
    return f"- [{s['n']}] [[{path[:-3]}|{label.replace('|', '｜').replace(']', '］')}]]"


def document(turns: list[dict], tag: str, generated: str) -> tuple[str, str, list[str]]:
    """（标题，正文，元信息）：标题用第一问；每一轮是「## 问：…」加回答加出处（出处链到原文）。"""
    title = f"讨论：{short_question(turns[0]['q'])}"
    meta = [f"生成时间：{generated}", f"范围：主题「{tag}」" if tag else "范围：整个笔记库", f"共 {len(turns)} 轮"]
    blocks = []
    for t in turns:
        part = [f"## 问：{_flat(t['q'])}", t["a"].strip()]
        if t["sources"]:
            part.append("出处：\n" + "\n".join(_source_line(s) for s in t["sources"]))
        blocks.append("\n\n".join(part))
    return title, "\n\n".join(blocks), meta


def file_name(first_question: str, stamp: str) -> str:
    """stamp：YYYYMMDDHHmm。日期放最后：书架按文件名里最后一个 8 位数字认日期。"""
    return f"复盘_讨论_{review._safe(short_question(first_question))}_{stamp}.md"


def clean_turns(raw: Any) -> list[dict]:
    """存盘请求里的 turns：{q, a, sources}，结构、长度、路径都要对，不对抛 ReviewError（接口回 400）。"""
    bad = review.ReviewError("turns 的格式不对：每一轮要有 q、a 和 sources")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SAVE_TURNS:
        raise review.ReviewError(f"turns 应该是 1 到 {MAX_SAVE_TURNS} 轮")
    out = []
    for t in raw:
        if not isinstance(t, dict) or not isinstance(t.get("q"), str) or not isinstance(t.get("a"), str):
            raise bad
        q, a, srcs = t["q"].strip(), t["a"].strip(), t.get("sources", [])
        if not q or len(q) > MAX_QUESTION_CHARS or not a or len(a) > MAX_ANSWER_CHARS:
            raise bad
        if not isinstance(srcs, list) or len(srcs) > MAX_SOURCES:
            raise bad
        rows = []
        for s in srcs:
            if not isinstance(s, dict):
                raise bad
            n = s.get("n")
            vals = {k: s.get(k, "") for k in ("title", "path", "kind", "date", "source")}
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 99 \
                    or not all(isinstance(v, str) for v in vals.values()) or not vals["title"] or not vals["path"] \
                    or any(len(vals[k]) > lim for k, lim in (("title", 300), ("path", 500), ("kind", 20), ("date", 10), ("source", 100))):
                raise bad
            parts = vals["path"].split("/")
            if parts[0] not in (vault.SPARK_DIRNAME, vault.REPORTS_DIRNAME) or not vals["path"].endswith(".md") \
                    or any(not p or p.startswith(".") for p in parts) or any(ord(c) < 32 for c in vals["path"]):
                raise bad
            rows.append(dict(vals, n=n))
        out.append({"q": q, "a": a, "sources": rows})
    return out


_SAVE_LOCK = threading.Lock()


def save(tag: str, turns: list[dict]) -> str:
    """整段讨论存成一篇复盘，返回文件路径。"""
    turns = clean_turns(turns)
    tag = _flat(tag) if isinstance(tag, str) else ""
    if len(tag) > 100:
        raise review.ReviewError("标签太长")
    title, body, meta = document(turns, tag, time.strftime("%Y-%m-%d %H:%M"))
    name = file_name(turns[0]["q"], time.strftime("%Y%m%d%H%M"))
    base, n = name[:-3], 1
    with _SAVE_LOCK:   # 查重名和写文件要连在一起：同一分钟两个请求存同一个问题，后到的另起一个名字
        while os.path.exists(os.path.join(review.out_dir(), name)):
            n += 1
            name = f"{base}_{n}.md"
        return review._write("discuss", title, meta, body, name)
