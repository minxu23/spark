"""论文的阅读侧：阅读页的三个页签（导读 / 原文 / PDF）怎么定，和针对单篇论文的「追问」。

页签：导读是文件里「## 摘要」那一节，原文是「## 正文」那一节（到「## 追问」为止），PDF 是文件头 pdf: 指向的原件，
本机确实有这个文件才给。默认页签：这篇的正文是从 PDF 取的字（from_pdf）、PDF 又在本机，默认开 PDF，否则开导读。

「从 PDF 取字」的判断：文件头里没有专门的标记，按收录代码（core/papers.py）的走法推：
- 有 pdf: 一行、来源不是 arXiv（贴的 PDF 链接、上传的 PDF）：正文一定是 PDF 取的字。
- 来源是 arXiv、有 pdf: 一行：网页版全文转来的正文带「#」小标题（每节一个），PDF 取的字只有段落（papers.paragraphs
  不出小标题，只有参考文献那一行会写成「## References」，这一行不算）。正文里别的「#」小标题一行都没有，算 PDF 取的字。

追问：把这篇的正文（参考文献前面的部分，太长按论文写摘要的办法截到 clip_compose.PAPER_MAX_CHARS 字）连同之前几轮问答交给模型，只根据这篇回答。
「存进本文」把问答写进文件末尾「## 追问」一节：已有这一节就接在它后面，不另开第二个；只往里加，不动已有的字。

手机上同一套（apps/mobile-ios/Spark/PaperRead.swift），tests/test_paper_read.py 用 swiftc 逐字对照。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from core import clip_compose, clips, review, vault

HEADING = "追问"
NOT_IN_PAPER = "文中没有提到这一点。"
MAX_PAPER_CHARS = clip_compose.PAPER_MAX_CHARS   # 同论文写摘要（clip_compose.summary_prompt）的截断
MAX_HISTORY_TURNS = 6
HISTORY_ANSWER_CHARS = 800
MAX_QUESTION_CHARS = review.MAX_QUESTION_CHARS
MAX_ANSWER_CHARS = 30000
MAX_SAVE_TURNS = 30
ARXIV_SOURCE = "arXiv"         # 同 core/papers.ARXIV_SOURCE

_NEXT_SECTION_RE = re.compile(r"^## ", re.M)
_NOTE_DEF_RE = re.compile(r"^\[\^note-\d+\]:", re.M)
_HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]")
# 参考文献标题行（收录时 PDF 取字写成「## References」这类，规则同 clip_compose.REFERENCES_TITLE / 手机 Paper.referencesTitle）
_REFS_HEAD_RE = re.compile(r"^## " + clip_compose.REFERENCES_TITLE + r"$")
_NOTE_REF_RE = re.compile(r"\[\^note-\d+\]")
_DEMOTE_RE = re.compile(r"^ {0,3}#{1,3}(?=[ \t]|$)")


# ---------------------------------------------------------------- 「## 追问」一节在哪

followup_head = clip_compose.followup_head       # 标题行：最后一个「[阅读原文](」后面的最后一个「## 追问」
before_followup = clip_compose.before_followup   # 一段字里追问一节前面的部分：正文、翻译、重写摘要都先过这里


def followup_range(text: str) -> tuple[int, int] | None:
    """「## 追问」一节：(标题行起, 节尾)。节尾是下一个「## 」或第一条批注脚注，没有就是文末。"""
    m = followup_head(text)
    if not m:
        return None
    ends = [x.start() for x in (_NEXT_SECTION_RE.search(text, m.end()), _NOTE_DEF_RE.search(text, m.end())) if x]
    return m.start(), min(ends) if ends else len(text)


# ---------------------------------------------------------------- 页签

def _front(text: str) -> dict[str, str]:
    """文件头里的几行 key: value（值是 JSON 字符串的解开）。只看第一行「---」到下一个「---」之间。"""
    out: dict[str, str] = {}
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return out
    for ln in lines[1:]:
        if ln == "---":
            break
        k, sep, v = ln.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z_]+", k):
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            try:
                v = json.loads(v)
            except ValueError:
                continue
            if not isinstance(v, str):
                continue
        out[k] = v
    return out


def pdf_key(text: str) -> str:
    """文件头 pdf: 指向的原件在笔记库里的路径（Spark/论文/attachments/<16 位 hex>.pdf）；没有或不合规是空串。
    pdf: 一般写相对论文文件夹的「attachments/….pdf」，写成从笔记库根算起的也认。"""
    v = _front(text).get("pdf", "")
    prefix = clips.PAPERS_DIR + "/"
    key = v if v.startswith(prefix) else prefix + v
    return key if v and clips._RE_PDF_KEY.match(key) else ""


def paper_body(text: str) -> str:
    """原文：「## 正文」到「[阅读原文]」或「## 追问」之前（同 clip_compose.body_of）。"""
    return clip_compose.body_of(text, paper=True)


def from_pdf(text: str) -> bool:
    """这篇的正文是不是从 PDF 取的字（见文件开头的说明）。"""
    if not pdf_key(text):
        return False
    if _front(text).get("source", "") != ARXIV_SOURCE:
        return True
    # 收录时 PDF 取字会把参考文献那一行写成「## References」：这一行不算小标题
    return not any(_HEADING_LINE_RE.match(ln) and not _REFS_HEAD_RE.match(ln.strip().lower())
                   for ln in paper_body(text).split("\n"))


def tabs(text: str, pdf_here: bool) -> tuple[list[str], str]:
    """(页签, 默认页签)。页签：guide 导读、text 原文、pdf（pdf_here：本机有这份原件）。"""
    out = ["guide", "text"] + (["pdf"] if pdf_here and pdf_key(text) else [])
    return out, "pdf" if "pdf" in out and from_pdf(text) else "guide"


def pdf_file(root: str, key: str) -> str | None:
    """PDF 原件的真实路径：key 要合 PDF 键的规则，realpath 落在 Spark/论文/attachments/ 里，还得是个文件；不然 None。"""
    if not isinstance(key, str) or not clips._RE_PDF_KEY.match(key):
        return None
    base = os.path.realpath(os.path.join(root, *clips.PAPERS_DIR.split("/"), "attachments"))
    try:
        real = os.path.realpath(os.path.join(root, *key.split("/")))
    except ValueError:
        return None
    if not real.startswith(base + os.sep) or not os.path.isfile(real):
        return None
    return real


# ---------------------------------------------------------------- 提示词（手机 PaperRead.swift 逐字一致）

def paper_text(text: str) -> str:
    """交给模型的正文：原文一节，参考文献标题起去掉（clip_compose.before_references，同写摘要），去掉图片，
    用户的划线记号去成纯文字、批注号删掉，连着的空行并成一个，截到 MAX_PAPER_CHARS 字。"""
    from core import annotations
    body = clip_compose.before_references(paper_body(text))
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = annotations.MARK_TEXT_RE.sub(r"\1", body)
    body = _NOTE_REF_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:MAX_PAPER_CHARS]


def _turns(history: list[dict]) -> str:
    return "\n\n".join(f"问：{t['q']}\n答：{review._cut(t['a'], HISTORY_ANSWER_CHARS)}" for t in history[-MAX_HISTORY_TURNS:])


def answer_prompt(title: str, paper: str, question: str, history: list[dict]) -> str:
    past = f"\n===== 之前的问答 =====\n{_turns(history)}\n" if history else ""
    return f"""下面是一篇论文的正文（从网页或 PDF 取出的文字，可能夹杂少量噪音，参考文献已经去掉），以及我们之前关于这篇论文的问答。请只根据这篇论文，回答我最后的问题。

要求：
- 只用论文里写的。论文里没有的，直接回答「{NOT_IN_PAPER}」，不要用常识补，不要编。
- 用中文回答。术语、方法名、模型名、数据集名可以保留英文。
- 能说出依据在论文哪一部分（章节、图表编号）的，顺带写一句。
- 不加标题，用短段落或列表。先给结论，再说依据。
{past}
===== 论文：{title} =====
{paper}

===== 我最后的问题 =====
{question}
"""


# ---------------------------------------------------------------- 存进本文（手机 PaperRead.appending 同一个写法）

def _flat(s: str) -> str:
    return " ".join(s.split())


def answer_lines(a: str) -> str:
    """回答写进文件前：「#」「##」「###」开头的行降成「####」（不造出新的一节，也不跟「### 问题」混），
    「==」转义成「\\=\\=」（不造出假划线），「[^」「[阅读原文](」开头的行前面加反斜杠（不当成批注脚注、不挪动正文的边界），
    代码栅栏行（去掉行首空格后以「```」开头）是奇数个时末尾补一行「```」（不让没关上的栅栏吞掉后面的内容）。"""
    out = []
    for ln in a.strip().split("\n"):
        ln = _DEMOTE_RE.sub("####", ln, count=1)
        ln = ln.replace("==", "\\=\\=")
        if ln.startswith(("[^", "[阅读原文](")):
            ln = "\\" + ln
        out.append(ln)
    if sum(1 for ln in out if ln.lstrip(" ").startswith("```")) % 2:
        out.append("```")
    return "\n".join(out)


def block(q: str, a: str) -> str:
    return f"### {_flat(q)}\n\n{answer_lines(a)}"


def appending(text: str, turns: list[dict]) -> str:
    """把几组问答加进「## 追问」一节。没有这一节：文末另起一节；有：接在这一节最后。
    文件里原有的字一个不动，只规整接口处的换行（去掉接口前多出的换行，补成一个空行）。"""
    blocks = "\n\n".join(block(t["q"], t["a"]) for t in turns)
    r = followup_range(text)
    if r is None:
        return text.rstrip("\n") + f"\n\n## {HEADING}\n\n" + blocks + "\n"
    head, tail = text[:r[1]], text[r[1]:]
    return head.rstrip("\n") + "\n\n" + blocks + "\n" + ("\n" + tail if tail else "")


def clean_turns(raw: Any) -> list[dict]:
    """存盘请求里的 turns：[{q, a}]，1 到 MAX_SAVE_TURNS 组，问题和回答都不能空、不能太长。不对抛 ReviewError。"""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SAVE_TURNS:
        raise review.ReviewError(f"turns 应该是 1 到 {MAX_SAVE_TURNS} 组问答")
    out = []
    for t in raw:
        if not isinstance(t, dict) or not isinstance(t.get("q"), str) or not isinstance(t.get("a"), str):
            raise review.ReviewError("每一组要有 q 和 a")
        q, a = t["q"].strip(), t["a"].strip()
        if not q or len(q) > MAX_QUESTION_CHARS or not a or len(a) > MAX_ANSWER_CHARS:
            raise review.ReviewError("问题或回答是空的，或者太长")
        out.append({"q": q, "a": a})
    return out


def _paper_path(rel: Any) -> str:
    """rel：相对笔记库的「Spark/论文/x.md」。返回真实路径；不是论文、不在笔记库里、文件不在都抛 ReviewError。"""
    if not isinstance(rel, str) or not clips.is_paper_path(rel) or any(not p or p.startswith(".") for p in rel.split("/")):
        raise review.ReviewError("只有论文能追问")
    root = os.path.realpath(vault.vault_root())
    base = os.path.realpath(os.path.join(root, *clips.PAPERS_DIR.split("/")))
    try:
        real = os.path.realpath(os.path.join(root, *rel.split("/")))
    except ValueError as e:
        raise review.ReviewError("只有论文能追问") from e
    if os.path.dirname(real) != base or not os.path.isfile(real):
        raise review.ReviewError("找不到这篇论文")
    return real


def save(rel: str, turns: Any) -> str:
    """存进本文：写之前在锁里重新读盘，接上问答，原子写回。返回写好的全文。
    锁用 clips.DOC_LOCK（手机交来的存也拿它）：两次存、手机的存一个接一个，不会拿旧内容盖掉别人刚写的。"""
    from core import atomic
    turns = clean_turns(turns)
    path = _paper_path(rel)
    with clips.DOC_LOCK:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        new = appending(text, turns)
        atomic.write_text(path, new)
    return new


# ---------------------------------------------------------------- 一轮问答

def ask(rel: str, question: str, history: list[dict], progress: review.Progress = lambda s: None) -> dict:
    """问一个问题，返回 {answer, sources: []}（和讨论同一个形状，对话界面共用）。"""
    path = _paper_path(rel)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    paper = paper_text(text)
    if not paper:
        raise review.ReviewError("这篇论文还没有正文，没法追问")
    title = _front(text).get("title") or os.path.basename(path)[:-3]
    progress("读这篇论文，写回答")
    answer = review._llm(answer_prompt(title, paper, question, history), max_tokens=3000).strip()
    return {"answer": answer or NOT_IN_PAPER, "sources": []}
