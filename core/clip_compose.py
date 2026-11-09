"""
剪藏写成一篇：文件名、文件头和正文的写法、摘要、全英文文章逐段翻译。
跟手机一字不差（apps/mobile-ios/Spark/Clips.swift 的 fileName / render / summarize，
ClipTranslate.swift 的 run），tests/test_clip_compose.py 对比提示词。Mac 上添加剪藏时由电脑这边写（core/clip_capture.py）。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Callable, Optional

# ---------------------------------------------------------------- 写一篇


def yaml(s: str) -> str:
    """文件头里的字符串：JSON 写法，斜杠不转义（同 Clips.yaml）。"""
    return json.dumps(s, ensure_ascii=False)


def file_name(title: str, clipped: date) -> str:
    t = re.sub(r'[\\/:*?"<>|\n\r\t#^\[\]]+', " ", title).strip(" \t")
    if len(t) > 60:
        t = t[:60]
    return f"{clipped.strftime('%Y%m%d')}_{t or '未命名'}.md"


def render(article: dict, note: str, summary: Optional[dict], clipped: date) -> str:
    day = clipped.isoformat()
    fm = ["---", f"title: {yaml(article['title'])}", f"source: {yaml(article['source'])}", f"url: {yaml(article['url'])}"]
    if article.get("published"):
        fm.append(f"published: {article['published']}")
    fm.append(f"clipped: {day}")
    if summary:
        fm.append("tags: [" + ", ".join(yaml(t) for t in summary["tags"]) + "]")
        fm.append(f"tldr: {yaml(summary['tldr'])}")
    if note:
        fm.append(f"note: {yaml(note)}")
    fm.append("---")
    lines = fm + ["", f"# {article['title']}", "", f"- 来源：{article['source']}"]
    if article.get("published"):
        lines.append(f"- 发布：{article['published']}")
    lines.append(f"- 剪藏：{day}")
    if note:
        lines.append(f"- 备注：{note.replace(chr(10), ' ')}")
    lines.append("")
    if summary:
        lines += ["## 摘要", "", f"**一句话**：{summary['tldr']}", ""]
        if summary["body"]:
            lines += [summary["body"], ""]
    lines += ["## 正文", "", article["markdown"].strip(), "", f"[阅读原文]({article['url']})", ""]
    return "\n".join(lines)


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
TITLE_ASK = "这篇没有标题。第一行先写：TITLE: <给它起的标题，不超过 24 字，说清内容，不要写作者名>\n\n"
IMAGES_ASK = "这篇文章的内容主要在图片里，附上了图片，请看图归纳。\n\n"
EXISTING_TAGS = "\n已经用过的标签（合适就沿用同一个写法，不合适再起新的）：{tags}\n"

Ask = Callable[..., str]   # ask(prompt, max_tokens=, timeout=, images=) -> 模型的回复


class SummaryError(Exception):
    pass


def summary_prompt(article: dict, existing_tags: list[str], with_images: bool = False) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", article["markdown"])
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 30000:
        text = text[:30000]
    existing = EXISTING_TAGS.format(tags="、".join(existing_tags[:40])) if existing_tags else ""
    # 跟手机一样按顺序替换
    prompt = SUMMARY_PROMPT.replace("{source}", article["source"]).replace("{title}", article["title"]) \
        .replace("{existing}", existing).replace("{text}", text)
    if article.get("titleGuessed"):
        prompt = TITLE_ASK + prompt
    if with_images:
        prompt = IMAGES_ASK + prompt
    return prompt


def parse_summary(raw: str) -> dict:
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
        if line.startswith(("- ", "* ")):
            body.append("- " + line[2:])
    # 模型没按格式写「TLDR:」时，用第一条要点顶上，别让整篇没有摘要
    if not tldr and body:
        tldr = body[0][2:][:40]
    if not tldr:
        raise SummaryError(f"摘要格式不对：{raw[:60]}")
    return {"tldr": tldr, "tags": tags[:4], "body": "\n".join(body), "title": title}


def summarize(article: dict, existing_tags: list[str], ask: Ask, images: Optional[list] = None) -> dict:
    prompt = summary_prompt(article, existing_tags, with_images=bool(images))
    return parse_summary(ask(prompt, max_tokens=1500, timeout=180, images=images or None))


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
        # 图片、代码、表格、单独成行的公式不翻译
        if b.startswith(("![", "```", "|", "$$")) or not re.search(r"[A-Za-z]{2}", b):
            out.append({"original": b, "plain": True})
            continue
        # 原文里的引用照样是引用，译文另起一段「> 」跟在后面，阅读时按一对显示
        line = b[2:] if b.startswith("> ") else b
        m = re.match(r"^(#{1,6} |[-*+] |\d{1,3}[.)] )", line)
        marker = m.group(0) if m else ""
        out.append({"marker": marker, "source": line[len(marker):], "original": b, "plain": False})
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


def translate(markdown: str, ask: Ask, progress: Callable[[int, int], None] = lambda n, total: None) -> tuple[str, int]:
    """返回对照稿、没译成的段数。一批失败再试一次，还不行那几段留英文。"""
    items = _pieces(markdown)
    batches: list[list[int]] = [[]]
    size = 0
    for i, p in enumerate(items):
        if p["plain"]:
            continue
        if size + len(p["source"]) > TRANSLATE_BATCH_CHARS and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(i)
        size += len(p["source"])
    for n, batch in enumerate(batches):
        if not batch:
            continue
        progress(n + 1, len(batches))
        text = "\n\n".join(f"[{i}] {items[i]['source']}" for i in batch)
        for _ in range(2):
            left = [i for i in batch if not items[i].get("translated")]
            if not left:
                break
            try:
                raw = ask(TRANSLATE_PROMPT.replace("{text}", text), max_tokens=8000, timeout=300)
            except Exception:  # noqa: BLE001  这一批再试一次，还不行留英文
                continue
            got = _parse_numbered(raw)
            for i in left:
                if got.get(i):
                    items[i]["translated"] = got[i]
    body = "\n\n".join(p["original"] if not p.get("translated") else f"{p['original']}\n\n> {p['marker']}{p['translated']}"
                       for p in items)
    return body, sum(1 for p in items if not p["plain"] and not p.get("translated"))


# ---------------------------------------------------------------- 已存的一篇：补摘要、补翻译（同手机 DocEdit.withSummary、ClipTranslate.replacingBody）

_BODY_RE = re.compile(r"\n## 正文\n(.*)", re.S)
_CJK_QUOTE_RE = re.compile(r"(?m)\n\n> [^\n]*[一-鿿][^\n]*(?=\n\n|\Z)")


def body_of(text: str) -> str:
    """「## 正文」后面、「[阅读原文]」前面的正文。"""
    m = _BODY_RE.search(text)
    body = m.group(1).strip() if m else ""
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


def wants_translation(text: str) -> bool:
    """全英文、还没译或者没译完的。"""
    body = body_of(text)
    return needs_translation(without_translation(body)) and untranslated(body) > 0


def article_of(text: str) -> dict:
    from core import clips
    fm = clips.frontmatter(text)
    return {"url": fm.get("url") or "", "title": fm.get("title") or "", "source": fm.get("source") or "",
            "published": fm.get("published") or "", "markdown": body_of(text)}


def with_summary(text: str, s: dict) -> str:
    lines = text.split("\n")
    if lines and lines[0] == "---" and "---" in lines[1:]:
        end = lines.index("---", 1)
        head = [ln for ln in lines[1:end] if not ln.startswith(("tags:", "tldr:"))]
        head.append("tags: [" + ", ".join(yaml(t) for t in s["tags"]) + "]")
        head.append(f"tldr: {yaml(s['tldr'])}")
        lines[1:end] = head
    out = "\n".join(lines)
    section = f"## 摘要\n\n**一句话**：{s['tldr']}\n\n" + (s["body"] + "\n\n" if s["body"] else "")
    m = re.search(r"## 摘要\n.*?(?=## 正文)", out, re.S)
    if m:
        return out[:m.start()] + section + out[m.end():]
    i = out.find("## 正文")
    return out[:i] + section + out[i:] if i >= 0 else out


def replacing_body(text: str, body: str) -> str:
    start = text.find("## 正文\n\n")
    if start < 0:
        return text
    start += len("## 正文\n\n")
    end = text.rfind("\n\n[阅读原文](", start)
    return text[:start] + body + (text[end:] if end >= 0 else "")
