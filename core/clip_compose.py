"""
剪藏写成一篇：文件名、文件头和正文的写法、摘要、全英文文章逐段翻译。
跟手机一字不差（apps/mobile-ios/Spark/Clips.swift 的 fileName / render / summarize，
ClipTranslate.swift 的 run），tests/test_clip_compose.py 对比提示词。Mac 上添加剪藏时由电脑这边写（core/clip_capture.py）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from typing import Callable, Optional

from core import doctime

# ---------------------------------------------------------------- 写一篇


def yaml(s: str) -> str:
    """文件头里的字符串：JSON 写法，斜杠不转义（同 Clips.yaml）。"""
    return json.dumps(s, ensure_ascii=False)


# 文件名两头去掉的空白：同手机 CharacterSet.whitespaces（Unicode 的 Zs 类空格加 Tab，含全角空格、不换行空格）
_SPACES = "\t" + "".join(chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Zs")
_SPACES_NL = _SPACES + "\n\x0b\x0c\r\x85\u2028\u2029"   # 同 CharacterSet.whitespacesAndNewlines


def file_name(title: str, day: date) -> str:
    """day：发表那天（本机时区）；不知道发表时间时用收录那天（name_day）。"""
    # 控制字符（\x0b 这类）也换掉：不然写出来的文件名电脑上的随手记、剪藏路径检查都不认（同手机 Clips.fileName）
    t = re.sub(r'[\\/:*?"<>|\n\r\t#^\[\]\x00-\x1f\x7f]+', " ", title).strip(_SPACES)
    if len(t) > 60:   # 按 Unicode 码位截（手机按 unicodeScalars 截，一样）
        t = t[:60]
    return f"{day.strftime('%Y%m%d')}_{t or '未命名'}.md"


def published_of(article: dict) -> Optional[doctime.When]:
    """文章的发表时间：publishedAt（ISO 带时差）优先，没有用 published（YYYY-MM-DD）。"""
    return doctime.best(article.get("publishedAt"), article.get("published"))


def name_day(article: dict, clipped: doctime.When) -> date:
    """文件名前面的日期：发表那天，不知道就用收录那天。"""
    return doctime.day(published_of(article) or clipped)


def render(article: dict, note: str, summary: Optional[dict], clipped: doctime.When) -> str:
    """clipped：收录的那一刻（带时区）；旧调用传日期的只写日期。"""
    published = published_of(article)
    fm = ["---", f"title: {yaml(article['title'])}", f"source: {yaml(article['source'])}", f"url: {yaml(article['url'])}"]
    if published is not None:
        fm.append(f"published: {doctime.day(published).isoformat()}")
        if isinstance(published, datetime):
            fm.append(f"published_at: {yaml(doctime.iso(published))}")
    fm.append(f"clipped: {doctime.day(clipped).isoformat()}")
    if isinstance(clipped, datetime):
        fm.append(f"clipped_at: {yaml(doctime.iso(clipped))}")
    if summary:
        fm.append("tags: [" + ", ".join(yaml(t) for t in summary["tags"]) + "]")
        fm.append(f"tldr: {yaml(summary['tldr'])}")
    if note:
        fm.append(f"note: {yaml(note)}")
    fm += paper_head_lines(article.get("authors") or [], article.get("arxiv") or "", article.get("pdf") or "")
    fm.append("---")
    lines = fm + ["", f"# {article['title']}", "", f"- 来源：{article['source']}"]
    if article.get("authors"):
        lines.append(f"- 作者：{authors_line(article['authors'])}")
    lines += doctime.lines(published, clipped)
    if note:
        lines.append(f"- 备注：{note.replace(chr(10), ' ')}")
    lines.append("")
    if summary:
        lines += ["## 摘要", "", summary['tldr'], ""]   # 一句话放在最前面，不再加「一句话：」的标签
        if summary["body"]:
            lines += [summary["body"], ""]
    lines += ["## 正文", "", article["markdown"].strip(_SPACES_NL), ""]
    if article["url"]:   # 手写的没有原文链接（同手机）
        lines += [f"[阅读原文]({article['url']})", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- 论文多出来的几行（同手机 Shared/Paper.swift 的 headLines、authorsLine）

MAX_AUTHORS = 20      # 文件头里最多写前 20 位作者
AUTHORS_SHOWN = 5     # 正文开头「- 作者：」超过 5 位写前 5 位加「等 N 人」


def paper_head_lines(authors: list[str], arxiv: str, pdf: str) -> list[str]:
    """论文文件头比剪藏多的几行，没有值的不写：authors（前 20 位）、arxiv（不带版本号）、pdf（相对论文文件夹）。"""
    out = []
    if authors:
        out.append("authors: [" + ", ".join(yaml(a) for a in authors[:MAX_AUTHORS]) + "]")
    if arxiv:
        out.append(f"arxiv: {yaml(arxiv)}")
    if pdf:
        out.append(f"pdf: {yaml(pdf)}")
    return out


def authors_line(authors: list[str]) -> str:
    """「A、B」；超过 5 位：「A、B、C、D、E 等 N 人」（N 是作者总数）。"""
    if len(authors) <= AUTHORS_SHOWN:
        return "、".join(authors)
    return "、".join(authors[:AUTHORS_SHOWN]) + f" 等 {len(authors)} 人"


# ---------------------------------------------------------------- 占位剪藏（存链接时正文没抓到）

STUB_NOTE = "正文还没抓到。可以在阅读页点「补抓正文」再抓一次，也可以先看原文。"


def stub_title(url: str) -> str:
    """没有标题时用的：网址的主机名加路径（去掉 www.、查询串和末尾的斜杠），例如 example.com/blog/post。"""
    from urllib.parse import unquote, urlsplit
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(u.path or "").rstrip("/")
    return (host + path)[:200] or url[:200]


def stub_source(url: str) -> str:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def render_stub(url: str, title: str, note: str, clipped: doctime.When) -> str:
    """占位剪藏：照常写的剪藏（render），正文是一句说明，文件头多一行 stub: true（在 clipped 这些后面）。
    稍后再读的那一行由调用方用 annotations.with_later 加在最后。"""
    article = {"url": url, "title": title.strip() or stub_title(url), "source": stub_source(url),
               "published": "", "publishedAt": "", "markdown": STUB_NOTE}
    text = render(article, note, None, clipped)
    end = text.index("\n---\n", 4)
    return text[:end] + "\nstub: true" + text[end:]


# ---------------------------------------------------------------- 摘要

SUMMARY_PROMPT = '''你是资讯编辑。下面是我收藏的一篇文章的正文（网页抓取，可能夹杂少量噪音）。只根据正文归纳，不要补充正文里没有的信息。

来源：{source}
标题：{title}

请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

TLDR: <一句话核心结论，不超过40字，不要markdown>
TAGS: <2 到 4 个主题标签，用中文逗号分隔，每个 2 到 8 个字，用具体的领域、技术或公司名（比如 具身智能、AI芯片、OpenAI），不要用「科技」「行业动态」这类泛词>
- <要点1：背景/问题>
- <要点2：核心方法或观点>
- <要点3：关键数据、案例或产品名（如有，保留英文原名）>
- <要点4，如有必要>
- <要点5，如有必要>
写 3-5 条要点，每条一句话即可，不用展开。
{existing}
正文：
"""
{text}
"""
'''
GUIDE_PROMPT = '''你是资讯编辑。下面是我收藏的一篇长文章的正文（网页抓取，可能夹杂少量噪音）。只根据正文写导读，不要补充正文里没有的信息。

来源：{source}
标题：{title}

请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

TLDR: <一句话核心结论，不超过40字，不要markdown>
TAGS: <2 到 4 个主题标签，用中文逗号分隔，每个 2 到 8 个字，用具体的领域、技术或公司名（比如 具身智能、AI芯片、OpenAI），不要用「科技」「行业动态」这类泛词>
### 这篇在讲什么
<一两段话：背景，以及要解决的问题>
### 核心观点或方法
<一两段话：作者的主要观点或做法，可以用「- 」列几条>
### 关键数据和例子
<一两段话：重要的数字、案例或产品名（如有，保留英文原名）>
### 局限与争议
<一两段话：正文提到的局限、反对意见或没解决的问题；正文没提就写「正文没有提到。」>
每段两到四句话，四段加起来不超过 800 字。小标题只用上面这四个，写成「### 」开头，不要用「#」「##」，不要加粗，不要另加别的标题。
{existing}
正文：
"""
{text}
"""
'''
PAPER_PROMPT = '''你是资讯编辑。下面是我收藏的一篇论文的正文（网页或 PDF 取字，可能夹杂少量噪音，参考文献已去掉）。只根据正文写导读和分节摘要，不要补充正文里没有的信息。

来源：{source}
标题：{title}

请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

TLDR: <一句话核心结论，不超过40字，不要markdown>
TAGS: <2 到 4 个主题标签，用中文逗号分隔，每个 2 到 8 个字，用具体的领域、技术或公司名（比如 具身智能、AI芯片、OpenAI），不要用「科技」「行业动态」这类泛词>
<一段话：依次说清这篇论文要解决什么问题、用了什么方法、主要结果是什么、有什么局限或适用范围；正文没提局限就写「正文没有提到局限。」>
### <论文第一个章节的名字>
<2 到 4 句话：这一节讲了什么、得出了什么>
### <论文第二个章节的名字>
<2 到 4 句话>
（按论文自己的章节顺序，每节一个小标题加一段话，一直写到最后一节。）
开头这一段直接写内容，前面不加小标题，也不要写「导读：」这样的字样。分节摘要的小标题用论文自己的章节名（可以译成中文，专有名词保留英文原名），写成「### 」开头，不要用「#」「##」，不要加粗。不要写「摘要」「正文」这两个小标题，也不要另加别的标题。参考文献、致谢不用写。全部加起来不超过 1500 字。
{existing}
正文：
"""
{text}
"""
'''
# 论文送给模型的正文上限（写摘要、追问都用它；剪藏仍是 30000）。长论文三四十万字，3 万字只够看到开头（同手机 Paper.maxChars）
PAPER_MAX_CHARS = 400000
GUIDE_MIN_CHARS = 5000   # 去掉图片后的正文超过这么多字，摘要写成导读（GUIDE_PROMPT），否则照旧 3-5 条要点
TITLE_ASK = "这篇没有标题。第一行先写：TITLE: <给它起的标题，不超过 24 字，说清内容，不要写作者名>\n\n"
# 论文没有元数据标题时：让模型照抄原题，不另起、不翻译（同手机 Clips.paperTitleAsk）
PAPER_TITLE_ASK = "这篇论文没有标题。第一行先写：TITLE: <论文的原标题，照正文开头的标题原样抄写，保持原文的语言，不要翻译，不要改写，不要写作者名>\n\n"
IMAGES_ASK = "这篇文章的内容主要在图片里，附上了图片，请看图归纳。\n\n"
EXISTING_TAGS = "\n已经用过的标签（合适就沿用同一个写法，不合适再起新的）：{tags}\n"

Ask = Callable[..., str]   # ask(prompt, max_tokens=, timeout=, images=) -> 模型的回复


class SummaryError(Exception):
    pass


def _summary_text(article: dict) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", article["markdown"])
    return re.sub(r"\n{3,}", "\n\n", text)


# 论文取字后的参考文献标题：单独一行的 References / Bibliography / 参考文献（不分大小写，可带编号前缀）。
# 手机同一条：Paper.referencesTitle（apps/mobile-ios/Shared/Paper.swift）
REFERENCES_TITLE = r"(?:(?:[0-9]{1,2}|[ivx]{1,5})[.)]?\s+)?(?:references|bibliography|参考文献)"
_REFERENCES_HEAD_RE = re.compile(r"^## " + REFERENCES_TITLE + r"$")
REFERENCES_MIN_SHARE = 5   # 标题前面的字不到全文的 1/5，当作目录里的一行，不在这里截断


def before_references(text: str) -> str:
    """摘要只看参考文献标题（「## References」这类单独一行）之前的正文。标题前面的字（空白不算）不到全文的 1/5 的不算，
    找下一个；没有合适的标题就原样返回。手机 Paper.beforeReferences 同一规则。"""
    total = len(text)
    pos = 0
    for line in text.split("\n"):
        if _REFERENCES_HEAD_RE.match(line.strip().lower()):
            head = text[:pos].rstrip()
            if head and len(head) * REFERENCES_MIN_SHARE >= total:
                return head
        pos += len(line) + 1
    return text


def is_guide(article: dict) -> bool:
    return len(_summary_text(article)) > GUIDE_MIN_CHARS


def summary_prompt(article: dict, existing_tags: list[str], with_images: bool = False, paper: bool = False) -> str:
    """paper：论文，不看长短都用 PAPER_PROMPT，参考文献不送给模型。"""
    text = _summary_text(article)
    if paper:
        text = before_references(text)
    template = PAPER_PROMPT if paper else GUIDE_PROMPT if len(text) > GUIDE_MIN_CHARS else SUMMARY_PROMPT
    limit = PAPER_MAX_CHARS if paper else 30000
    if len(text) > limit:
        text = text[:limit]
    existing = EXISTING_TAGS.format(tags="、".join(existing_tags[:40])) if existing_tags else ""
    # 跟手机一样按顺序替换
    prompt = template.replace("{source}", article["source"]).replace("{title}", article["title"]) \
        .replace("{existing}", existing).replace("{text}", text)
    if article.get("titleGuessed"):
        prompt = (PAPER_TITLE_ASK if paper else TITLE_ASK) + prompt
    if with_images:
        prompt = IMAGES_ASK + prompt
    return prompt


def parse_summary(raw: str, guide: bool = False) -> dict:
    """guide：导读（GUIDE_PROMPT 的回答）。正文原样保留段落和「### 」小标题，「#」「##」降成「###」
    （下游按「\n## 」切节，导读里不能有）。"""
    tldr, tags, body, title = "", [], [], ""
    for line in (ln.strip() for ln in raw.split("\n")):
        if m := re.match(r"^\**TITLE\**\s*[:：]\s*", line):
            title = line[m.end():].strip(" *《》\"「」")
            continue
        if m := re.match(r"^\**TLDR\**\s*[:：]\s*", line):
            tldr = line[m.end():]
            continue
        if m := re.match(r"^\**TAGS\**\s*[:：]\s*", line):
            tags = [t.strip(" #`\"") for t in re.split(r"[，,、;；]", line[m.end():])]
            tags = [t for t in tags if t and len(t) <= 12]
            continue
        if guide:
            if not re.match(r"^(-{3,}|\*{3,}|_{3,})$", line):
                # 小标题叫「正文」「摘要」的改成加粗行：重写摘要时按「## 正文」找边界，「### 正文」也会被认成它
                line = re.sub(r"^#+\s*(正文|摘要)\s*$", r"**\1**", line)
                body.append(re.sub(r"^#{1,2}\s+", "### ", line))
            continue
        if line.startswith(("- ", "* ")):
            body.append("- " + line[2:])
    if guide:
        # 没写「TLDR:」时用导读里第一句顶上
        tldr = tldr or re.sub(r"^[-*] ", "", next((ln for ln in body if ln and not ln.startswith("#")), ""))[:40]
        if not tldr:
            raise SummaryError(f"摘要格式不对：{raw[:60]}")
        return {"tldr": tldr, "tags": tags[:4], "body": re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip(), "title": title}
    # 模型没按格式写「TLDR:」时，用第一条要点顶上，别让整篇没有摘要
    if not tldr and body:
        tldr = body[0][2:][:40]
    if not tldr:
        raise SummaryError(f"摘要格式不对：{raw[:60]}")
    return {"tldr": tldr, "tags": tags[:4], "body": "\n".join(body), "title": title}


def summarize(article: dict, existing_tags: list[str], ask: Ask, images: Optional[list] = None, paper: bool = False) -> dict:
    prompt = summary_prompt(article, existing_tags, with_images=bool(images), paper=paper)
    guide = paper or is_guide(article)
    tokens = 4000 if paper else 3000 if guide else 1500
    return parse_summary(ask(prompt, max_tokens=tokens, timeout=180, images=images or None), guide=guide)


# ---------------------------------------------------------------- 翻译

TRANSLATE_BATCH_CHARS = 6000
TRANSLATE_PROMPT = '''把下面编号的英文段落翻译成中文，语言自然、准确，像中文作者写的，不要逐词硬译。人名、公司名、产品名和业内通用术语可以保留英文。保留行内的粗体和链接。

严格按这个格式输出，每段一行，不要合并、不要漏段、不要解释：
[编号] 译文

===== 原文 =====
{text}
'''


def cjk_share(text: str) -> float:
    cjk = total = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
            total += 1
        elif 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
            total += 1
    return cjk / total if total else 0.0


def needs_translation(markdown: str) -> bool:
    """中文占比很低、字数够的才算全英文（公众号里夹英文的文章不算）。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)", "", markdown)
    return len(t) >= 200 and cjk_share(t) < 0.1


def _pieces(markdown: str) -> list[dict]:
    out, code = [], None
    blocks = []
    for block in markdown.split("\n\n"):
        lines = block.split("\n")
        is_list = len(lines) > 1 and all(re.match(r"^([-*+] |\d{1,3}[.)] )", ln) for ln in lines)
        blocks += lines if is_list else [block]
    for block in blocks:
        b = block.strip()
        if not b:
            continue
        if code is not None:
            code.append(b)
            if b.endswith("```"):
                out.append({"original": "\n\n".join(code), "plain": True})
                code = None
            continue
        if b.startswith("```") and "```" not in b[3:]:
            code = [b]
            continue
        # 已经译过的（补译时）：带汉字的「> 」段紧跟在原文后面，就是它的译文，留着不再送去翻译
        prev = out[-1] if out else None
        if (b.startswith("> ") and re.search(r"[\u4e00-\u9fff]", b) and prev and not prev["plain"]
                and not prev.get("translated")):
            tr = b[2:]
            prev["translated"] = tr[len(prev["marker"]):] if prev["marker"] and tr.startswith(prev["marker"]) else tr
            continue
        # 图片、代码、表格、单独成行的公式不翻译
        if b.startswith(("![", "```", "|", "$$")) or not re.search(r"[A-Za-z]{2}", b):
            out.append({"original": b, "plain": True})
            continue
        # 原文里的引用照样是引用，译文另起一段「> 」跟在后面，阅读时按一对显示
        line = b[2:] if b.startswith("> ") else b
        m = re.match(r"^(#{1,6} |[-*+] |\d{1,3}[.)] )", line)
        marker = m.group(0) if m else ""
        # 手机上写的批注号「[^note-3]」不送去翻译，免得译文里也带一个
        source = re.sub(r"\[\^note-\d+\]", "", line[len(marker):])
        out.append({"marker": marker, "source": source, "original": b, "plain": False})
    return out


def _parse_numbered(raw: str) -> dict[int, str]:
    out: dict[int, str] = {}
    cur = None
    for line in raw.split("\n"):
        if m := re.match(r"^\s*\[(\d+)\]\s*(.*)$", line):
            cur = int(m.group(1))
            out[cur] = m.group(2)
        elif cur is not None and line.strip():
            out[cur] = out.get(cur, "") + " " + line.strip()
    return out


# 译文合理性：模型漏写某段的「[编号]」时，那段的译文会被接到上一段后面（_parse_numbered 把没有编号的行接上去）。
# 阈值按真实笔记库的对照段统计：中文译文通常比英文原文短，超出的只有并段和图片说明。手机端 TranslationRule.plausible 逐字一致。
HEADING_FACTOR, HEADING_SLACK = 2, 20
BODY_PERCENT, BODY_SLACK = 115, 25


def translation_plausible(source: str, translated: str, heading: bool) -> bool:
    """标题：译文不能有「。」，长度不超过原文字符数的 2 倍加 20；其他段：不超过 1.15 倍加 25。"""
    if heading:
        return "。" not in translated and len(translated) <= len(source) * HEADING_FACTOR + HEADING_SLACK
    return len(translated) * 100 <= len(source) * BODY_PERCENT + BODY_SLACK * 100


_CUE_SPEAKER_RE = re.compile(r"\*\*[^*\n]+\*\*[ \t]*[:：][ \t]*")
_CUE_RE = re.compile(r"(?:\[[^\[\]()]{1,40}\][ \t]*)+")


def is_stage_cue(paragraph: str) -> bool:
    """整段只有方括号舞台提示（「[applause]」「[Music] [laughter]」，前面可以有「**发言人**：」）：不用翻译，
    不送去模型。模型常整段跳过这类段落，之后的编号就整体错位。手机端 TranslationRule.isStageCue 逐字一致。"""
    p = paragraph.strip()
    m = _CUE_SPEAKER_RE.match(p)
    if m:
        p = p[m.end():]
    return _CUE_RE.fullmatch(p) is not None


def numbers_complete(got, count: int) -> bool:
    """一批的回复里的编号正好是 1..count。少了或多了，后面的译文可能整体错位（模型跳过一段后顺着往下编号），整批不收。
    手机端 TranslationRule.numbersComplete 一致。"""
    return set(got) == set(range(1, count + 1))


def translate(markdown: str, ask: Ask, progress: Callable[[int, int], None] = lambda n, total: None,
              stop: Optional[Callable[[], bool]] = None) -> tuple[str, int]:
    """返回对照稿、没译成的段数。一批失败再试一次，还不行那几段留英文。
    传进来的可以是译了一半的对照稿：已经有译文的段落原样留着，只译没译的。
    stop：返回 True 时在两批之间停下（模型调用被打断时也不再重试），已经译好的照样写进对照稿，没译的算进没译成。"""
    stopped = (lambda: bool(stop and stop()))
    items = _pieces(markdown)
    batches: list[list[int]] = [[]]
    size = 0
    for i, p in enumerate(items):
        if p["plain"] or p.get("translated"):   # 已经有译文的不再送
            continue
        if size + len(p["source"]) > TRANSLATE_BATCH_CHARS and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(i)
        size += len(p["source"])
    for n, batch in enumerate(batches):
        if not batch:
            continue
        if stopped():
            break
        progress(n + 1, len(batches))
        text = "\n\n".join(f"[{i}] {items[i]['source']}" for i in batch)
        for _ in range(2):
            left = [i for i in batch if not items[i].get("translated")]
            if not left:
                break
            try:
                raw = ask(TRANSLATE_PROMPT.replace("{text}", text), max_tokens=8000, timeout=300)
            except Exception:  # noqa: BLE001  这一批再试一次，还不行留英文
                if stopped():
                    break
                continue
            got = _parse_numbered(raw)
            for i in left:
                if got.get(i) and translation_plausible(items[i]["source"], got[i], items[i]["marker"].startswith("#")):
                    items[i]["translated"] = got[i]
    body = "\n\n".join(p["original"] if not p.get("translated") else f"{p['original']}\n\n> {p['marker']}{p['translated']}"
                       for p in items)
    return body, sum(1 for p in items if not p["plain"] and not p.get("translated"))


# ---------------------------------------------------------------- 已存的一篇：补摘要、补翻译（同手机 DocEdit.withSummary、ClipTranslate.replacingBody）

BODY_HEADING = "正文"   # 剪藏的正文一节；信息文章的是「原文」（core/track_translate.py）
_BODY_RE = re.compile(r"\n## 正文\n(.*)", re.S)
_CJK_QUOTE_RE = re.compile(r"(?m)\n\n> [^\n]*[一-鿿][^\n]*(?=\n\n|\Z)")
# 正文后面读的人自己加的：读后感、重新生成后接回来的「以前的划线」这类 ## 小节，批注脚注
_SECTION_END_RE = re.compile(r"\n## |\n\[\^note-\d+\]:")


def section_span(text: str, heading: str) -> Optional[tuple[int, int]]:
    """「## heading」一节正文在 text 里的 (起, 止)，两头的空白不算；没有这一节返回 None。
    用于每段一行、没有 ## 小标题的小节（信息文章的「原文」）：到下一个「## 」或第一条批注脚注为止。
    剪藏的「正文」不走这里：文章自己会有 ## 小标题，要一直到「[阅读原文]」（body_of）。"""
    m = re.search(rf"\n## {re.escape(heading)}[ \t]*\n", text)
    if not m:
        return None
    a = m.end()
    e = _SECTION_END_RE.search(text, a - 1)
    b = e.start() if e else len(text)
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


def section_body(text: str, heading: str = BODY_HEADING, paper: bool = False) -> str:
    """「## heading」一节的正文。heading 是「正文」时同 body_of（剪藏；paper：论文，到「## 追问」为止）。"""
    if heading == BODY_HEADING:
        return body_of(text, paper)
    span = section_span(text, heading)
    return text[span[0]:span[1]] if span else ""


def replacing_section(text: str, body: str, heading: str = BODY_HEADING) -> str:
    """把「## heading」一节的正文换成 body，前后的别的内容不动。没有这一节就原样返回。"""
    if heading == BODY_HEADING:
        return replacing_body(text, body)
    span = section_span(text, heading)
    if not span:
        return text
    return text[:span[0]] + body + text[span[1]:]


def wants_translation_body(body: str) -> bool:
    """一节正文全英文、还没译或者没译完的（剪藏、信息文章同一条规则）。"""
    return needs_translation(without_translation(body)) and untranslated(body) > 0


# 论文末尾读的人存进来的「## 追问」一节（core/paper_read.py）：不算正文。只认最后一个「[阅读原文](」后面的，取最后一个。
# 只对论文（调用方传 paper=True，按路径 clips.is_paper_path 定）；剪藏的正文边界不看它。行尾可以带 \r（CRLF 文件）
FOLLOWUP_HEAD_RE = re.compile(r"^## 追问[ \t]*\r?$", re.M)
_READ_ORIGINAL = "\n[阅读原文]("


def followup_head(text: str) -> Optional[re.Match]:
    """「## 追问」标题行（文章自己也可能有叫「追问」的小标题：只认最后一个「[阅读原文](」后面的，取最后一个）。"""
    frm = text.rfind(_READ_ORIGINAL)
    frm = frm + len(_READ_ORIGINAL) if frm >= 0 else 0
    heads = list(FOLLOWUP_HEAD_RE.finditer(text, frm))
    return heads[-1] if heads else None


def before_followup(s: str) -> str:
    """s 里「## 追问」一节前面的部分（没有这一节原样返回）。"""
    m = followup_head(s)
    return s[:m.start()] if m else s


def body_of(text: str, paper: bool = False) -> str:
    """「## 正文」后面、「[阅读原文]」前面的正文。paper：论文，存进来的「## 追问」一节也不算正文。"""
    m = _BODY_RE.search(text)
    body = ((before_followup(m.group(1)) if paper else m.group(1)).strip()) if m else ""
    cut = body.rfind("\n\n[阅读原文](")
    if cut >= 0:
        body = body[:cut]
    elif body.startswith("[阅读原文]("):
        body = ""
    return body


def without_translation(body: str) -> str:
    """去掉上次写进去的译文（带汉字的「> 」段），英文原文自带的引用不动。"""
    return _CJK_QUOTE_RE.sub("", body)


def untranslated(body: str) -> int:
    """对照稿里没跟译文的英文段落有几段（同手机 Markdown.untranslated 的意思）。"""
    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]
    n = 0
    for i, b in enumerate(blocks):
        if b.startswith((">", "!", "```", "|", "$$")) or len(b) <= 40:   # 公式块不用译
            continue
        latin = sum(1 for ch in b if ch.isascii() and ch.isalpha())
        nxt = blocks[i + 1] if i + 1 < len(blocks) else ""
        # 后面跟着「> 」段就算译过（同手机：表格行这类译出来跟原文一样、没有汉字的也算）
        if latin > len(b) * 0.6 and not nxt.startswith("> "):
            n += 1
    return n


def needs_summary(text: str) -> bool:
    return "\n## 摘要\n" not in text


def wants_translation(text: str, heading: str = BODY_HEADING, paper: bool = False) -> bool:
    """全英文、还没译或者没译完的。heading：哪一节是正文（剪藏「正文」，信息文章「原文」）。paper：论文（不看「## 追问」）。"""
    return wants_translation_body(section_body(text, heading, paper))


def article_of(text: str, paper: bool = False) -> dict:
    from core import clips
    fm = clips.frontmatter(text)
    return {"url": fm.get("url") or "", "title": fm.get("title") or "", "source": fm.get("source") or "",
            "published": fm.get("published") or "", "publishedAt": fm.get("published_at") or "",
            "markdown": body_of(text, paper)}


def with_summary(text: str, s: dict) -> str:
    lines = text.split("\n")
    if lines and lines[0] == "---" and "---" in lines[1:]:
        end = lines.index("---", 1)
        head = [ln for ln in lines[1:end] if not ln.startswith(("tags:", "tldr:"))]
        head.append("tags: [" + ", ".join(yaml(t) for t in s["tags"]) + "]")
        head.append(f"tldr: {yaml(s['tldr'])}")
        lines[1:end] = head
    out = "\n".join(lines)
    section = f"## 摘要\n\n{s['tldr']}\n\n" + (s["body"] + "\n\n" if s["body"] else "")
    m = re.search(r"## 摘要\n.*?(?=## 正文)", out, re.S)
    if m:
        return out[:m.start()] + section + out[m.end():]
    i = out.find("## 正文")
    return out[:i] + section + out[i:] if i >= 0 else out


def replacing_body(text: str, body: str, paper: bool = False) -> str:
    start = text.find("## 正文\n\n")
    if start < 0:
        return text
    start += len("## 正文\n\n")
    end = text.rfind("\n\n[阅读原文](", start)
    if end < 0 and paper:   # 没有原文链接（上传的 PDF）的论文：到「## 追问」为止，追问一节留着
        f = followup_head(text)
        if f and f.start() >= start:
            end = len(text[:f.start()].rstrip())
    return text[:start] + body + (text[end:] if end >= 0 else "")
