"""
notes2insight 演示层：把生成好的技术洞察报告压成一份可翻页的单文件 HTML 演示。

为什么不直接把报告塞进幻灯片：报告是几万字的长文论证，机械按标题切页得到的东西没法看。
这里分三步走，只花一次便宜的模型调用：

  1. outline()     从 Markdown 里确定性地抽骨架——摘要 / 判断 / 分歧 / 展望 / 启示整节保留，
                   每章只留小标题和首段。8 万字的报告压到 2 万字上下再送模型。
  2. build_deck()  模型据此输出结构化的 slide JSON（页面类型、要点、来源编号）。
  3. render_html() 纯 Python 渲染成自包含 HTML：无外链、无构建步骤，双击就能看，Cmd+P 就是 PDF。

演示里的 [N] 角标可以点开，直接跳回 Obsidian 里的原始笔记——自己回顾时最需要的就是这个。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import date as _date

from . import llm

# 整节保留的结论性小节（这些本来就是压缩过的，再压就没信息了）
FULL_SECTIONS = ("执行摘要", "关键判断", "分歧与开放问题", "信号与展望", "启示")
MAX_SECTION_CHARS = 6000    # 单个结论节送进模型的上限
MAX_CHAPTER_CHARS = 2600    # 单章送进模型的上限（只要小标题和首段）
MAX_OUTLINE_CHARS = 60000   # 骨架总上限，超了就砍后面的章节


class DeckError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 从报告 Markdown 里抽骨架
# --------------------------------------------------------------------------

_RE_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_RE_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
_RE_H3 = re.compile(r"^###\s+(.+?)\s*$", re.M)
_RE_DETAILS = re.compile(r"<details>.*?</details>", re.S)
_RE_SRC_ROW = re.compile(r"^\|\s*\[(\d+)\]\s*\|.*$", re.M)
# 表格里的单元格内容会把 | 转义成 \|（笔记标题里带竖线很常见），按裸竖线切列会错位
_RE_CELL_SEP = re.compile(r"(?<!\\)\|")
_RE_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def parse_frontmatter(md: str) -> dict:
    m = _RE_FRONT.search(md)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_sources(md: str) -> list[dict]:
    """从"来源索引"表里还原每篇笔记的编号、标题、日期和相对路径。

    表里的"位置"列是文件夹、双链里是文件名，两者拼起来就是 vault 内的相对路径，
    演示页面据此生成 obsidian:// 链接。"""
    m = re.search(r"###\s*来源索引\s*\n(.*?)(?=\n##|\Z)", md, re.S)
    if not m:
        return []
    out: list[dict] = []
    for row in _RE_SRC_ROW.finditer(m.group(1)):
        cells = [c.strip().replace("\\|", "|") for c in _RE_CELL_SEP.split(row.group(0))[1:-1]]
        if len(cells) < 5:
            continue
        idx = int(row.group(1))
        cell, date, folder, status = cells[1], cells[2], cells[3], cells[4]
        link = _RE_WIKILINK.search(cell)
        stem = link.group(1).strip() if link else ""
        title = _RE_WIKILINK.sub("", cell).strip() or stem
        folder = "" if folder in (".", "—", "") else folder
        rel = f"{folder}/{stem}" if folder else stem
        out.append({
            "idx": idx, "title": title, "stem": stem, "date": "" if date == "—" else date,
            "folder": folder or "（库根目录）", "rel": rel, "ok": status.startswith("✅"),
        })
    return out


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _head_paras(text: str, n: int = 2, cap: int = 700) -> str:
    """取一段内容的前 n 个自然段，表格整块保留（表格往往就是这一节最硬的证据）。"""
    got: list[str] = []
    for p in _paragraphs(text):
        if p.startswith("|") or p.startswith(">"):
            got.append(p[:cap * 2])
        else:
            got.append(p[:cap])
        if len(got) >= n:
            break
    return "\n\n".join(got)


def _split_h2(md: str) -> list[tuple[str, str]]:
    heads = list(_RE_H2.finditer(md))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(md)
        out.append((h.group(1).strip(), md[h.end():end].strip()))
    return out


def _compress_chapter(body: str) -> str:
    """章节只留结构：每个 ### 小标题 + 它下面的第一段。没有小标题就取前两段。"""
    subs = list(_RE_H3.finditer(body))
    if not subs:
        return _head_paras(body, 2)
    parts = []
    for i, s in enumerate(subs):
        end = subs[i + 1].start() if i + 1 < len(subs) else len(body)
        parts.append(f"### {s.group(1).strip()}\n{_head_paras(body[s.end():end], 1, 500)}")
    return "\n\n".join(parts)


def outline(md: str) -> dict:
    """把整份报告压成送模型的骨架。返回 {title, subtitle, meta, chapters, text}。"""
    meta = parse_frontmatter(md)
    body = _RE_FRONT.sub("", md, count=1)
    body = _RE_DETAILS.sub("", body)

    title = meta.get("title", "")
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m and not title:
        title = m.group(1).strip()
    subtitle = ""
    m = re.search(r"^#\s+.+\n+##\s+(.+)$", body, re.M)
    if m:
        subtitle = m.group(1).strip()

    chapters: list[str] = []
    chunks: list[str] = []
    for head, content in _split_h2(body):
        if head == subtitle or head.startswith("方法、来源"):
            continue
        if head.startswith("阅读地图"):
            chunks.append(f"## 阅读地图\n{content[:2500]}")
        elif re.match(r"第\s*\d+\s*章", head):
            chapters.append(head)
            chunks.append(f"## {head}\n{_compress_chapter(content)[:MAX_CHAPTER_CHARS]}")
        elif any(head.startswith(k) for k in FULL_SECTIONS):
            chunks.append(f"## {head}\n{content[:MAX_SECTION_CHARS]}")

    text = "\n\n".join(chunks)
    if len(text) > MAX_OUTLINE_CHARS:      # 极端长的报告，先保结论节再保章节
        text = text[:MAX_OUTLINE_CHARS] + "\n\n（骨架过长已截断）"
    return {"title": title, "subtitle": subtitle, "meta": meta,
            "chapters": chapters, "text": text}


# --------------------------------------------------------------------------
# 让模型排版
# --------------------------------------------------------------------------

SLIDES_PROMPT = """你要把一份技术洞察报告改写成一套**作者自己回顾用**的幻灯片脚本。

读者就是报告作者本人，几周后回头看：他要在十几分钟内重新捡回这份报告的判断、证据和尚未解决的问题，
并且能顺着来源编号跳回原始笔记。因此：

- 一页只讲一件事。页标题写**结论**，不要写话题——"推理单价两年掉两个数量级"是标题，"关于推理成本"不是。
- 要点写完整的句子，带上具体数字和口径（谁说的、什么时间、什么单位）。不要"多个方面""若干趋势"这类空话。
- 每条要点用 cites 标出来源编号，编号只能取自报告正文里已经出现过的 [N]，不要自己编号，没有把握就留空数组。
- note 写一句"当时为什么这么判断 / 这条结论的软肋在哪"，自己回顾时提醒用，可以是空串。
- 只使用报告里出现过的内容，不要补充报告之外的知识，不要编造数字。
- 专有名词保留英文原名，中文用全角标点。
- **JSON 字符串里绝对不要出现英文双引号**，要加引号一律用中文引号“”，否则 JSON 解析会失败。

页面类型（kind）：
- "section"：章节分隔页，只要 title 和 lead（一句话说明这章要解决什么）
- "points"：最常用，title + lead + bullets
- "compare"：两种对立主张，用 columns（正好两列）
- "timeline"：时间线，用 events

严格输出下面这个 JSON，不要用 Markdown 代码块包起来，不要写任何解释文字：

{{
  "slides": [
    {{"kind": "points", "section": "所属章节名", "title": "页标题", "lead": "一句话核心，可为空",
      "bullets": [{{"text": "要点全句", "cites": [1, 3]}}], "note": "回顾提醒，可为空"}},
    {{"kind": "section", "section": "章节名", "title": "第 N 章 主题", "lead": "这一章要解决什么"}},
    {{"kind": "compare", "section": "章节名", "title": "页标题", "lead": "",
      "columns": [{{"head": "一方主张", "items": [{{"text": "", "cites": []}}]}},
                  {{"head": "另一方主张", "items": [{{"text": "", "cites": []}}]}}],
      "note": "目前无法判定的原因"}},
    {{"kind": "timeline", "section": "章节名", "title": "页标题",
      "events": [{{"date": "2026-03", "text": "发生了什么", "cites": [2]}}]}}
  ]
}}

结构要求（一共 {target} 页左右，封面页和来源索引页由程序生成，你不要输出）：
1. 开篇 1 页 points：这批材料共同回答了什么问题、最重要的变化是什么（取自执行摘要）
2. 关键判断 3-5 页，每页 2-3 条判断，一条判断就是一个 bullet
3. 每一章：1 页 section 分隔页 + 1-2 页 points（讲这一章的事实状态、机制和核心张力）
4. 分歧 2-3 页，能对立的尽量用 compare
5. 报告里如果有时间线或情景推演，给 1 页 timeline
6. 展望与可观测信号 1-2 页（信号要写清"看什么指标、什么读数说明哪个情景在兑现"）
7. 启示 1 页

--- 报告标题 ---
{title}

--- 来源编号对照（cites 只能用这些编号）---
{sources}

--- 报告骨架 ---
{outline}
"""


_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _repair_json(s: str) -> str:
    """写中文的时候模型很容易在字符串里直接打英文双引号（用"下一阶段"的叙事），JSON 当场就断。

    这里按"这个引号后面跟的是不是 JSON 结构符"来判断它是收尾还是正文：是正文就转义掉。
    顺手把字符串里的裸换行、制表符也转义了，同样是常见的破坏方式。"""
    out: list[str] = []
    in_str = esc = False
    for i, ch in enumerate(s):
        if not in_str:
            out.append(ch)
            in_str = ch == '"'
            continue
        if esc:
            out.append(ch)
            esc = False
        elif ch == "\\":
            out.append(ch)
            esc = True
        elif ch == '"':
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j >= len(s) or s[j] in ",:}]":
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
        elif ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        else:
            out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> dict:
    """模型偶尔会加代码围栏或前言，这里只取最外层的 JSON 对象。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*\n", "", s)
    s = re.sub(r"\n```\s*$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise DeckError(f"模型没有返回 JSON：{text[:300]}")
    body = s[start:end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as first:
        try:
            return json.loads(_repair_json(body))
        except json.JSONDecodeError:
            raise DeckError(f"模型返回的 JSON 解析失败：{first}；开头是 {body[:200]}") from first


def _sources_brief(sources: list[dict]) -> str:
    if not sources:
        return "（报告里没有来源索引，cites 一律留空数组）"
    return "\n".join(f"[{s['idx']}] {s['title'][:60]}（{s['date'] or '日期未注明'}）" for s in sources)


def _clean_cites(raw, valid: set[int]) -> list[int]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        try:
            n = int(c)
        except (TypeError, ValueError):
            continue
        if (not valid or n in valid) and n not in out:
            out.append(n)
    return out


def _clean_items(raw, valid: set[int]) -> list[dict]:
    out = []
    for it in raw if isinstance(raw, list) else []:
        if isinstance(it, str):
            text, cites = it, []
        elif isinstance(it, dict):
            text, cites = str(it.get("text", "")).strip(), _clean_cites(it.get("cites"), valid)
        else:
            continue
        if text:
            out.append({"text": text, "cites": cites})
    return out


def normalize(raw: dict, sources: list[dict]) -> list[dict]:
    """把模型输出收敛成渲染器能放心用的形状：丢掉空页、剔除不存在的来源编号。"""
    valid = {s["idx"] for s in sources}
    slides: list[dict] = []
    for s in raw.get("slides") or []:
        if not isinstance(s, dict):
            continue
        kind = s.get("kind") if s.get("kind") in ("section", "points", "compare", "timeline") else "points"
        slide = {
            "kind": kind,
            "section": str(s.get("section", "")).strip(),
            "title": str(s.get("title", "")).strip(),
            "lead": str(s.get("lead", "")).strip(),
            "note": str(s.get("note", "")).strip(),
        }
        if kind == "points":
            slide["bullets"] = _clean_items(s.get("bullets"), valid)
            if not slide["bullets"] and not slide["lead"]:
                continue
        elif kind == "compare":
            cols = []
            for c in (s.get("columns") or [])[:2]:
                if isinstance(c, dict):
                    cols.append({"head": str(c.get("head", "")).strip(),
                                 "items": _clean_items(c.get("items"), valid)})
            if len(cols) < 2:
                continue
            slide["columns"] = cols
        elif kind == "timeline":
            evs = []
            for e in s.get("events") or []:
                if isinstance(e, dict) and str(e.get("text", "")).strip():
                    evs.append({"date": str(e.get("date", "")).strip(),
                                "text": str(e.get("text", "")).strip(),
                                "cites": _clean_cites(e.get("cites"), valid)})
            if not evs:
                continue
            slide["events"] = evs
        elif not slide["title"]:
            continue
        slides.append(slide)
    if not slides:
        raise DeckError("模型没有产出任何有效页面")
    return slides


def build_deck(md: str, *, backend: str, api_key: str = "", model: str = "",
               api_base: str = "", timeout: int = 600, stop_flag=None) -> dict:
    """报告 Markdown → 幻灯片脚本（未渲染）。"""
    skel = outline(md)
    if len(skel["text"]) < 200:
        raise DeckError("这份报告里没抽到可用的章节，可能不是 notes2insight 生成的报告")
    sources = parse_sources(md)
    target = min(40, 12 + 2 * max(len(skel["chapters"]), 1))
    prompt = SLIDES_PROMPT.format(
        target=target, title=skel["title"] or "（无标题）",
        sources=_sources_brief(sources), outline=skel["text"],
    )
    raw = llm.complete(prompt, backend, api_key=api_key, model=model, api_base=api_base,
                       max_tokens=16000, timeout=timeout, stop_flag=stop_flag)
    slides = normalize(_extract_json(raw), sources)
    return {"title": skel["title"], "subtitle": skel["subtitle"], "meta": skel["meta"],
            "slides": slides, "sources": sources,
            "outline_chars": len(skel["text"])}


# --------------------------------------------------------------------------
# 渲染成自包含 HTML
# --------------------------------------------------------------------------

def obsidian_uri(vault_name: str, rel: str) -> str:
    """跳回 Obsidian 原始笔记。file 参数不带扩展名，Obsidian 自己补 .md。"""
    if not vault_name or not rel:
        return ""
    return ("obsidian://open?vault=" + urllib.parse.quote(vault_name, safe="")
            + "&file=" + urllib.parse.quote(rel, safe=""))


_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #0f1115; --panel: #171a21; --fg: #e8eaf0; --dim: #97a0b0; --line: #2a2f3a;
  --accent: #6aa9ff; --accent-soft: #1d2b44; --warn: #ffb86b;
  --serif: "Songti SC", "Source Han Serif SC", serif;
  --sans: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}
@media (prefers-color-scheme: light) {
  :root { --bg: #f7f8fa; --panel: #fff; --fg: #1b1f27; --dim: #626b7a; --line: #e2e5ec;
          --accent: #2563eb; --accent-soft: #e6eefc; --warn: #b26a00; }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--fg); font-family: var(--sans);
       -webkit-font-smoothing: antialiased; overflow: hidden; }

/* 顶部：章节名 + 页码 + 进度 */
#top { position: fixed; top: 0; left: 0; right: 0; height: 44px; display: flex; align-items: center;
       gap: 12px; padding: 0 20px; font-size: 13px; color: var(--dim); z-index: 20; }
#top .sec { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#bar { position: fixed; top: 0; left: 0; height: 2px; background: var(--accent); z-index: 21;
       transition: width .2s ease; }

/* 幻灯片 */
#stage { height: 100%; display: flex; align-items: center; justify-content: center;
         padding: 60px 48px 72px; }
.slide { width: min(1100px, 100%); max-height: 100%; overflow-y: auto; }
.slide h1 { font-size: clamp(26px, 3.4vw, 44px); line-height: 1.25; margin: 0 0 14px; letter-spacing: -.01em; }
.slide .lead { font-size: clamp(15px, 1.5vw, 20px); color: var(--dim); line-height: 1.7; margin: 0 0 28px;
               max-width: 62em; }
.slide ul { list-style: none; padding: 0; margin: 0; }
.slide li { position: relative; padding: 14px 0 14px 26px; font-size: clamp(15px, 1.45vw, 19px);
            line-height: 1.75; border-top: 1px solid var(--line); }
.slide li:first-child { border-top: 0; }
.slide li::before { content: ""; position: absolute; left: 4px; top: 25px; width: 6px; height: 6px;
                    border-radius: 50%; background: var(--accent); }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }
@media (max-width: 760px) { .cols { grid-template-columns: 1fr; } }
.col { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 18px 22px; }
.col h3 { margin: 0 0 8px; font-size: clamp(15px, 1.5vw, 19px); color: var(--accent); }
.col li { font-size: clamp(14px, 1.3vw, 17px); padding: 10px 0 10px 22px; }
.col li::before { top: 21px; }
.tl { border-left: 2px solid var(--line); margin-left: 8px; padding-left: 0; }
.tl li { padding: 12px 0 12px 22px; border-top: 0; }
.tl li::before { left: -5px; top: 22px; }
.tl .date { display: inline-block; min-width: 92px; color: var(--accent); font-variant-numeric: tabular-nums;
            font-size: .92em; }
.cover { text-align: left; }
.cover .kicker { color: var(--accent); font-size: 14px; letter-spacing: .12em; margin-bottom: 20px; }
.cover h1 { font-size: clamp(30px, 4.6vw, 60px); font-family: var(--serif); }
.cover .sub { font-size: clamp(16px, 1.9vw, 24px); color: var(--dim); margin: 16px 0 34px; line-height: 1.6; }
.cover .meta { color: var(--dim); font-size: 14px; line-height: 2; border-top: 1px solid var(--line);
               padding-top: 18px; }
.sectionpage h1 { font-family: var(--serif); font-size: clamp(28px, 4vw, 52px); }
.sectionpage .lead { margin-top: 20px; }

/* 来源角标 */
.cite { display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 20px;
        margin: 0 2px; padding: 0 5px; border: 0; border-radius: 5px; background: var(--accent-soft);
        color: var(--accent); font: inherit; font-size: 12px; font-variant-numeric: tabular-nums;
        cursor: pointer; vertical-align: 2px; }
.cite:hover { background: var(--accent); color: var(--bg); }

/* 来源索引页 */
table.src { width: 100%; border-collapse: collapse; font-size: 14px; }
table.src th, table.src td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line);
                             vertical-align: top; }
table.src th { color: var(--dim); font-weight: 500; }
table.src a { color: var(--accent); text-decoration: none; }
table.src a:hover { text-decoration: underline; }

/* 底部：备注 + 操作提示 */
#note { position: fixed; left: 0; right: 0; bottom: 34px; padding: 12px 24px; background: var(--panel);
        border-top: 1px solid var(--line); color: var(--dim); font-size: 14px; line-height: 1.7; z-index: 15; }
#foot { position: fixed; bottom: 0; left: 0; right: 0; height: 34px; display: flex; align-items: center;
        justify-content: space-between; padding: 0 20px; font-size: 12px; color: var(--dim); z-index: 20; }
#foot kbd { background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px;
            font-family: inherit; font-size: 11px; }
#foot button { background: none; border: 0; color: var(--dim); font: inherit; cursor: pointer; padding: 0 6px; }
#foot button:hover { color: var(--accent); }

/* 总览 */
#grid { position: fixed; inset: 0; background: var(--bg); overflow-y: auto; padding: 56px 24px 24px;
        display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px;
        align-content: start; z-index: 30; }
.thumb { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
         cursor: pointer; min-height: 110px; }
.thumb:hover, .thumb.on { border-color: var(--accent); }
.thumb .n { color: var(--dim); font-size: 11px; font-variant-numeric: tabular-nums; }
.thumb .t { font-size: 13px; line-height: 1.5; margin-top: 6px;
            display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }

/* 来源抽屉 */
#drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(420px, 90vw); background: var(--panel);
          border-left: 1px solid var(--line); padding: 24px; overflow-y: auto; z-index: 40;
          transform: translateX(100%); transition: transform .18s ease; }
#drawer.on { transform: none; }
#drawer h2 { margin: 0 0 4px; font-size: 18px; line-height: 1.4; }
#drawer .row { color: var(--dim); font-size: 13px; line-height: 1.9; }
#drawer a.open { display: inline-block; margin-top: 18px; padding: 9px 16px; background: var(--accent);
                 color: var(--bg); border-radius: 8px; text-decoration: none; font-size: 14px; }
#drawer .close { position: absolute; top: 14px; right: 16px; background: none; border: 0; color: var(--dim);
                 font-size: 22px; cursor: pointer; }
.hidden { display: none !important; }

/* 打印 / 导出 PDF：一页一张，展开全部备注 */
@media print {
  @page { size: 297mm 167mm; margin: 0; }
  body { overflow: visible; background: #fff; color: #111; }
  #top, #foot, #grid, #drawer, #bar, #note { display: none !important; }
  #stage { display: block; height: auto; padding: 0; }
  .slide { break-after: page; page-break-after: always; width: 100%; max-height: none;
           padding: 18mm 20mm; display: block; }
  .cite { background: none; color: #555; border: 1px solid #bbb; }
  .printnote { display: block !important; margin-top: 16px; padding-top: 10px; border-top: 1px solid #ddd;
               color: #666; font-size: 12px; }
  .col { border: 1px solid #ddd; }
}
.printnote { display: none; }
</style>
</head>
<body>
<div id="bar"></div>
<div id="top"><span class="sec" id="secName"></span><span id="pageNo"></span></div>
<div id="stage"><div class="slide" id="slide"></div></div>
<div id="note" class="hidden"></div>
<div id="foot">
  <span id="deckName"></span>
  <span>
    <button id="btnPrev">←</button><button id="btnNext">→</button>
    <button id="btnGrid">总览 <kbd>Esc</kbd></button>
    <button id="btnNote">备注 <kbd>S</kbd></button>
    <button id="btnPrint">导出 PDF <kbd>P</kbd></button>
  </span>
</div>
<div id="grid" class="hidden"></div>
<div id="drawer"><button class="close" id="drawerClose">&times;</button><div id="drawerBody"></div></div>
<script type="application/json" id="deckdata">__DECK_JSON__</script>
<script>
(function () {
  "use strict";
  var DECK = JSON.parse(document.getElementById("deckdata").textContent);
  var SLIDES = DECK.slides, SRC = {};
  DECK.sources.forEach(function (s) { SRC[s.idx] = s; });
  var cur = 0, notesOn = false;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // 要点文本里模型偶尔会留下 [3] 这种写法，统一交给 cites 渲染，正文里的就去掉免得重复
  function clean(t) { return esc(t).replace(/\s*\[\d+(?:\]\[\d+)*\]/g, ""); }
  function cites(list) {
    if (!list || !list.length) return "";
    return " " + list.map(function (n) {
      return '<button class="cite" data-src="' + n + '" title="' +
             esc((SRC[n] && SRC[n].title) || ("来源 " + n)) + '">' + n + "</button>";
    }).join("");
  }
  function items(list) {
    return list.map(function (b) { return "<li>" + clean(b.text) + cites(b.cites) + "</li>"; }).join("");
  }

  function body(s) {
    if (s.kind === "cover") {
      return '<div class="cover">' +
        '<div class="kicker">NOTES2INSIGHT · 技术洞察演示</div>' +
        "<h1>" + esc(s.title) + "</h1>" +
        (s.lead ? '<div class="sub">' + esc(s.lead) + "</div>" : "") +
        '<div class="meta">' + s.meta.map(esc).join("<br />") + "</div></div>";
    }
    if (s.kind === "sources") {
      var rows = DECK.sources.map(function (x) {
        var name = x.uri ? '<a href="' + esc(x.uri) + '">' + esc(x.title) + "</a>" : esc(x.title);
        return "<tr><td>[" + x.idx + "]</td><td>" + name + "</td><td>" + esc(x.date || "—") +
               "</td><td>" + esc(x.folder) + "</td></tr>";
      }).join("");
      return "<h1>" + esc(s.title) + "</h1>" +
        '<div class="lead">点开笔记名回到 Obsidian 原文。</div>' +
        '<table class="src"><tr><th>编号</th><th>笔记</th><th>日期</th><th>位置</th></tr>' + rows + "</table>";
    }
    var head = "<h1>" + esc(s.title) + "</h1>" + (s.lead ? '<div class="lead">' + clean(s.lead) + "</div>" : "");
    if (s.kind === "section") return '<div class="sectionpage">' + head + "</div>";
    if (s.kind === "compare") {
      return head + '<div class="cols">' + s.columns.map(function (c) {
        return '<div class="col"><h3>' + esc(c.head) + "</h3><ul>" + items(c.items) + "</ul></div>";
      }).join("") + "</div>";
    }
    if (s.kind === "timeline") {
      return head + '<ul class="tl">' + s.events.map(function (e) {
        return '<li><span class="date">' + esc(e.date) + "</span>" + clean(e.text) + cites(e.cites) + "</li>";
      }).join("") + "</ul>";
    }
    return head + "<ul>" + items(s.bullets || []) + "</ul>";
  }

  function show(i) {
    cur = Math.max(0, Math.min(SLIDES.length - 1, i));
    var s = SLIDES[cur];
    $("slide").innerHTML = body(s) + (s.note ? '<div class="printnote">' + esc(s.note) + "</div>" : "");
    $("slide").parentNode.scrollTop = 0;
    $("secName").textContent = s.section || "";
    $("pageNo").textContent = (cur + 1) + " / " + SLIDES.length;
    $("bar").style.width = ((cur + 1) / SLIDES.length * 100) + "%";
    $("note").innerHTML = s.note ? esc(s.note) : '<span style="opacity:.55">这一页没有备注</span>';
    $("note").classList.toggle("hidden", !notesOn);
    if (location.hash !== "#" + (cur + 1)) history.replaceState(null, "", "#" + (cur + 1));
    if (!$("grid").classList.contains("hidden")) drawGrid();
  }

  function drawGrid() {
    $("grid").innerHTML = SLIDES.map(function (s, i) {
      return '<div class="thumb' + (i === cur ? " on" : "") + '" data-i="' + i + '">' +
             '<div class="n">' + (i + 1) + (s.section ? " · " + esc(s.section) : "") + "</div>" +
             '<div class="t">' + esc(s.title) + "</div></div>";
    }).join("");
  }
  function toggleGrid(force) {
    var g = $("grid"), on = force !== undefined ? force : g.classList.contains("hidden");
    if (on) drawGrid();
    g.classList.toggle("hidden", !on);
  }

  function openSrc(n) {
    var s = SRC[n];
    if (!s) return;
    $("drawerBody").innerHTML =
      '<div class="row">来源 [' + n + "]</div><h2>" + esc(s.title) + "</h2>" +
      '<div class="row">日期：' + esc(s.date || "未注明") + "<br />位置：" + esc(s.folder) + "</div>" +
      (s.uri ? '<a class="open" href="' + esc(s.uri) + '">在 Obsidian 中打开原文</a>' : "");
    $("drawer").classList.add("on");
  }

  document.addEventListener("click", function (e) {
    var c = e.target.closest(".cite");
    if (c) { openSrc(+c.dataset.src); return; }
    var t = e.target.closest(".thumb");
    if (t) { show(+t.dataset.i); toggleGrid(false); return; }
    if (!e.target.closest("#drawer") && !e.target.closest(".cite")) $("drawer").classList.remove("on");
  });

  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k = e.key;
    if (k === "ArrowRight" || k === "PageDown" || k === " " || k === "j") { show(cur + 1); e.preventDefault(); }
    else if (k === "ArrowLeft" || k === "PageUp" || k === "k") { show(cur - 1); e.preventDefault(); }
    else if (k === "Home") show(0);
    else if (k === "End") show(SLIDES.length - 1);
    else if (k === "Escape") { $("drawer").classList.remove("on"); toggleGrid(); }
    else if (k === "s" || k === "S") { notesOn = !notesOn; show(cur); }
    else if (k === "p" || k === "P") window.print();
    else if (k === "f" || k === "F") {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen();
    }
  });

  $("btnPrev").onclick = function () { show(cur - 1); };
  $("btnNext").onclick = function () { show(cur + 1); };
  $("btnGrid").onclick = function () { toggleGrid(); };
  $("btnNote").onclick = function () { notesOn = !notesOn; show(cur); };
  $("btnPrint").onclick = function () { window.print(); };
  $("drawerClose").onclick = function () { $("drawer").classList.remove("on"); };
  $("deckName").textContent = DECK.title;

  // 打印时把所有页一次性铺开，打完再收回来
  window.addEventListener("beforeprint", function () {
    $("stage").innerHTML = SLIDES.map(function (s) {
      return '<div class="slide">' + body(s) +
             (s.note ? '<div class="printnote">' + esc(s.note) + "</div>" : "") + "</div>";
    }).join("");
  });
  window.addEventListener("afterprint", function () {
    $("stage").innerHTML = '<div class="slide" id="slide"></div>';
    show(cur);
  });

  show(parseInt(location.hash.slice(1), 10) - 1 || 0);
})();
</script>
</body>
</html>
"""


def _clip(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip("，。、；：") + "…"


def _prepare(deck: dict, vault_name: str, report_filename: str) -> tuple[list[dict], list[dict], str]:
    """补上封面页和来源索引页，并给每条来源算出 obsidian:// 链接。
    HTML 和 PPTX 两个渲染器共用这一份页序，免得两边慢慢走样。"""
    meta = deck.get("meta") or {}
    sources = [{**s, "uri": obsidian_uri(vault_name, s.get("rel", ""))}
               for s in deck.get("sources") or []]

    cover_meta = [x for x in [
        f"来源：{meta['sources']}" if meta.get("sources") else "",
        f"材料时间跨度：{meta['range']}" if meta.get("range") else "",
        f"报告生成于 {meta.get('date') or _date.today().isoformat()}",
        # 关注点常常是一整段"报告规格"，封面上只放开头一句，完整要求本来就在报告正文里
        f"关注点：{_clip(meta['focus'], 56)}" if meta.get("focus") and meta["focus"] != "（未指定）" else "",
        f"原文：{report_filename}" if report_filename else "",
    ] if x]

    title = deck.get("title") or "技术洞察报告"
    pages = [{"kind": "cover", "section": "", "title": title,
              "lead": deck.get("subtitle", ""), "note": "", "meta": cover_meta}]
    pages += deck["slides"]
    if sources:
        pages.append({"kind": "sources", "section": "附录", "title": "来源索引",
                      "lead": "", "note": "", "meta": []})
    return pages, sources, title


def render_html(deck: dict, *, vault_name: str = "", report_filename: str = "") -> str:
    """把 slide 脚本渲染成一份自包含 HTML。不引用任何外部资源，拷到哪都能开。"""
    pages, sources, title = _prepare(deck, vault_name, report_filename)

    payload = json.dumps({"title": title, "slides": pages, "sources": sources}, ensure_ascii=False)
    # </script> 出现在 JSON 里会提前闭合脚本块；转义斜杠是最省事的防法
    payload = payload.replace("</", "<\\/")
    head = title.replace("&", "&amp;").replace("<", "&lt;")
    return _TEMPLATE.replace("__DECK_JSON__", payload).replace("__TITLE__", head)


def generate(md: str, *, backend: str, api_key: str = "", model: str = "", api_base: str = "",
             timeout: int = 600, vault_name: str = "", report_filename: str = "",
             stop_flag=None) -> tuple[str, dict]:
    """报告 Markdown → (HTML 字符串, 幻灯片脚本)。"""
    deck = build_deck(md, backend=backend, api_key=api_key, model=model,
                      api_base=api_base, timeout=timeout, stop_flag=stop_flag)
    return render_html(deck, vault_name=vault_name, report_filename=report_filename), deck


# --------------------------------------------------------------------------
# 渲染成 PPTX（和 HTML 用同一份 slide 脚本，不需要再调一次模型）
# --------------------------------------------------------------------------

# OOXML 没有 CSS 那种 sans-serif 通用族，字体只能写具体名字。默认主题把简体中文映射到
# 宋体（衬线），所以不能什么都不做。这里改主题的 script 映射而不是给每个 run 钉字体：
# 层级对了，别人拿到文件后在 PowerPoint 里换主题字体就能一次性改掉全篇。
# 选微软雅黑是因为它两边都退化成无衬线——Windows 上自带；macOS 上没有，
# 会落到系统默认中文字体（PingFang），同样是黑体。
PPT_CJK_FONT = "微软雅黑"
PPT_CJK_FONT_EN = "Microsoft YaHei"
PPT_LATIN_FONT = "Calibri"          # 主题自带的西文字体，本身就是无衬线

_INK = (0x1B, 0x1F, 0x27)
_DIM = (0x62, 0x6B, 0x7A)
_ACCENT = (0x25, 0x63, 0xEB)
_LINE = (0xE2, 0xE5, 0xEC)

_RE_INLINE_CITE = re.compile(r"\s*\[\d+(?:\]\[\d+)*\]")

# 16:9 版心里一页最多放这么多行来源，多了表格会掉出页面
SOURCES_PER_SLIDE = 14


def _plain(text: str) -> str:
    """正文里的 [3] 交给专门的角标渲染，这里去掉免得重复。"""
    return _RE_INLINE_CITE.sub("", str(text or "")).strip()


def _cite_tag(cites) -> str:
    return "".join(f"[{c}]" for c in cites or [])


def _fit(text: str, big: int, small: int, limit: int) -> int:
    """长标题自动降一档字号，免得溢出版心。"""
    return small if len(text) > limit else big


def _set_theme_fonts(prs) -> None:
    """把主题字体方案里的中日韩映射从默认的宋体改成黑体。

    OOXML 的 <a:font script="Hans"> 就是干这个用的：正文里不写字体，由主题按脚本决定，
    所以改一处就够，用户后续也能在 PowerPoint 的"主题字体"里一次性换掉。"""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    try:
        part = prs.slide_masters[0].part.part_related_by(RT.THEME)
    except (IndexError, KeyError):
        return                                    # 没主题就算了，不该让导出失败
    xml = part.blob.decode("utf-8")
    for script in ("Hans", "Hant"):
        xml = re.sub(rf'(<a:font script="{script}" typeface=")[^"]*(")',
                     rf"\g<1>{PPT_CJK_FONT}\g<2>", xml)
    xml = re.sub(r'(<a:latin typeface=")[^"]*(")', rf"\g<1>{PPT_LATIN_FONT}\g<2>", xml)
    part._blob = xml.encode("utf-8")


def render_pptx(deck: dict, out_path: str, *, vault_name: str = "",
                report_filename: str = "") -> str:
    """把 slide 脚本渲染成 .pptx。

    和 HTML 版的取舍不同：PPTX 里没法做"点开角标看原文"的抽屉，所以来源信息走两条路——
    正文后面留 [N] 角标，备注页里写清楚每个 [N] 是哪篇笔记、在库里的什么位置。
    来源索引页的笔记名带 obsidian:// 超链接，播放器认这个协议就能点回原文。"""
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_AUTO_SIZE
        from pptx.util import Emu, Inches, Pt
    except ImportError as e:
        raise DeckError("未安装 python-pptx（pip install python-pptx），无法导出 PPTX") from e

    pages, sources, _ = _prepare(deck, vault_name, report_filename)
    src_by_id = {s["idx"]: s for s in sources}

    prs = Presentation()
    _set_theme_fonts(prs)
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    W, H, PAD = prs.slide_width, prs.slide_height, Inches(0.85)
    BODY_W = W - 2 * PAD

    def style(run, size, *, bold=False, color=_INK):
        # 不设 run.font.name：字体统一由主题决定（见 _set_theme_fonts），
        # 这样用户换主题字体就能一次改全篇，不用逐个 run 改
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = RGBColor(*color)

    def box(slide, left, top, width, height):
        tf = slide.shapes.add_textbox(left, top, width, height).text_frame
        tf.word_wrap = True
        # python-pptx 量不了文本高度，写个 normAutofit 让 PowerPoint 自己缩：
        # 偶尔一页内容特别多时，宁可字小一点也不要掉出版心
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        return tf

    def para(tf, first=False, space_before=0, space_after=6):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.space_before, p.space_after = Pt(space_before), Pt(space_after)
        return p

    def write(p, text, size, *, bold=False, color=_INK, cites=None):
        run = p.add_run()
        run.text = text
        style(run, size, bold=bold, color=color)
        if cites:
            tail = p.add_run()
            tail.text = " " + _cite_tag(cites)
            style(tail, max(size - 3, 9), color=_ACCENT)

    def rule(slide, top):
        """一条淡线，替代 HTML 里的 border-top。"""
        sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, PAD, top, BODY_W, Emu(9525))
        sh.fill.solid()
        sh.fill.fore_color.rgb = RGBColor(*_LINE)
        sh.line.fill.background()
        sh.shadow.inherit = False

    def cited_ids(page):
        out = []
        groups = [page.get("bullets", []), page.get("events", [])]
        groups += [col.get("items", []) for col in page.get("columns", [])]
        for g in groups:
            for item in g:
                for c in item.get("cites") or []:
                    if c not in out:
                        out.append(c)
        return out

    def notes(slide, page):
        """备注页：讲稿 + 这一页引了哪些笔记。PPTX 里点不开角标，只能写清楚。"""
        lines = [page["note"]] if page.get("note") else []
        ids = cited_ids(page)
        if ids:
            lines += ["", "本页来源："]
            for c in ids:
                src = src_by_id.get(c)
                lines.append(f"  [{c}] {src['title']}（{src['date'] or '日期未注明'}｜{src['folder']}）"
                             if src else f"  [{c}]")
        if lines:
            slide.notes_slide.notes_text_frame.text = "\n".join(lines)

    def heading(slide, page, top=Inches(0.7)):
        t = _plain(page["title"])
        tf = box(slide, PAD, top, BODY_W, Inches(1.2))
        write(para(tf, first=True, space_after=0), t, _fit(t, 26, 20, 34), bold=True)
        cur = top + Inches(0.55 if len(t) <= 34 else 0.95)
        if page.get("lead"):
            lead = _plain(page["lead"])
            write(para(box(slide, PAD, cur + Inches(0.12), BODY_W, Inches(0.9)), first=True,
                       space_after=0), lead, 13, color=_DIM)
            cur += Inches(0.45 if len(lead) <= 60 else 0.75)
        return cur + Inches(0.3)

    def sources_table(slide, top, rows_data):
        rows = len(rows_data) + 1
        tbl = slide.shapes.add_table(rows, 4, PAD, top, BODY_W, Inches(0.32) * rows).table
        for col, w in zip(tbl.columns, (Inches(0.9), Inches(7.2), Inches(1.4), Inches(2.0))):
            col.width = w
        def cell_text(r, c, text, *, bold=False, color=_INK, size=10, uri=""):
            p = tbl.cell(r, c).text_frame.paragraphs[0]
            run = p.add_run()
            run.text = text
            style(run, size, bold=bold, color=color)
            if uri:
                try:
                    run.hyperlink.address = uri       # obsidian:// 跳回原文
                except Exception:
                    pass                              # 不认这个协议也不该让导出失败
        for j, head in enumerate(("编号", "笔记", "日期", "位置")):
            cell_text(0, j, head, bold=True, color=_DIM, size=11)
        for i, src in enumerate(rows_data, 1):
            cell_text(i, 0, f"[{src['idx']}]", color=_DIM)
            cell_text(i, 1, src["title"], uri=src.get("uri", ""))
            cell_text(i, 2, src["date"] or "—", color=_DIM)
            cell_text(i, 3, src["folder"], color=_DIM)

    for page in pages:
        slide = prs.slides.add_slide(blank)
        kind = page["kind"]

        if kind == "cover":
            write(para(box(slide, PAD, Inches(1.5), BODY_W, Inches(0.5)), first=True, space_after=0),
                  "NOTES2INSIGHT · 技术洞察演示", 12, bold=True, color=_ACCENT)
            t = page["title"]
            write(para(box(slide, PAD, Inches(2.15), BODY_W, Inches(2.0)), first=True, space_after=0),
                  t, _fit(t, 34, 26, 40), bold=True)
            cur = Inches(3.05) if len(t) <= 40 else Inches(4.0)
            if page.get("lead"):
                write(para(box(slide, PAD, cur, BODY_W, Inches(1.0)), first=True, space_after=0),
                      page["lead"], 16, color=_DIM)
                cur += Inches(0.75)
            rule(slide, cur + Inches(0.2))
            mf = box(slide, PAD, cur + Inches(0.4), BODY_W, Inches(2.0))
            for i, line in enumerate(page.get("meta", [])):
                write(para(mf, first=(i == 0), space_after=3), line, 11, color=_DIM)
            continue

        if kind == "section":
            t = _plain(page["title"])
            write(para(box(slide, PAD, Inches(2.6), BODY_W, Inches(1.6)), first=True, space_after=0),
                  t, _fit(t, 32, 24, 30), bold=True)
            if page.get("lead"):
                write(para(box(slide, PAD, Inches(4.0), BODY_W, Inches(1.2)), first=True,
                           space_after=0), _plain(page["lead"]), 14, color=_DIM)
            notes(slide, page)
            continue

        if kind == "sources":
            # 一页塞不下 20 多行，表格会掉出版心；按 14 行一页切开，续页标题加"（续）"
            chunks = [sources[i:i + SOURCES_PER_SLIDE]
                      for i in range(0, len(sources), SOURCES_PER_SLIDE)] or [[]]
            for n, chunk in enumerate(chunks):
                if n:
                    slide = prs.slides.add_slide(blank)
                head = dict(page, title=page["title"] + ("（续）" if n else ""),
                            lead=page["lead"] if n == 0 else "")
                sources_table(slide, heading(slide, head, top=Inches(0.6)), chunk)
            continue

        top = heading(slide, page)

        if kind == "compare":
            gap = Inches(0.4)
            cw = (BODY_W - gap) // 2
            for i, col in enumerate(page.get("columns", [])[:2]):
                cf = box(slide, PAD + (cw + gap) * i, top, cw, H - top - Inches(0.6))
                write(para(cf, first=True, space_after=8), _plain(col.get("head", "")),
                      15, bold=True, color=_ACCENT)
                for it in col.get("items", []):
                    write(para(cf, space_before=4, space_after=6), "· " + _plain(it["text"]),
                          12, cites=it.get("cites"))
        elif kind == "timeline":
            tf = box(slide, PAD, top, BODY_W, H - top - Inches(0.6))
            for i, e in enumerate(page.get("events", [])):
                p = para(tf, first=(i == 0), space_before=4, space_after=8)
                d = p.add_run()
                d.text = (e.get("date") or "—") + "　"
                style(d, 12, bold=True, color=_ACCENT)
                write(p, _plain(e["text"]), 13, cites=e.get("cites"))
        else:
            bullets = page.get("bullets", [])
            size = 15 if len(bullets) <= 4 else (13 if len(bullets) <= 7 else 11)
            tf = box(slide, PAD, top, BODY_W, H - top - Inches(0.6))
            for i, b in enumerate(bullets):
                write(para(tf, first=(i == 0), space_before=6, space_after=8),
                      "· " + _plain(b["text"]), size, cites=b.get("cites"))
        notes(slide, page)

    prs.save(out_path)
    return out_path


def load_deck_from_html(path: str) -> dict:
    """从已生成的 .deck.html 里把 slide 脚本读回来。

    HTML 里本来就内嵌了完整的脚本 JSON，所以补出 PPTX 不用再花一次模型调用。"""
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r'id="deckdata">(.*?)</script>', html, re.S)
    if not m:
        raise DeckError(f"{os.path.basename(path)} 里没有找到幻灯片脚本，可能不是本工具生成的演示")
    try:
        data = json.loads(m.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as e:
        raise DeckError(f"读取已有演示失败：{e}") from e
    pages = data.get("slides") or []
    cover = next((p for p in pages if p.get("kind") == "cover"), {})
    meta = {}
    for line in cover.get("meta", []):
        # 封面那几行是渲染时拼出来的，这里按原样拆回去，好让重渲染的封面保持一致
        for key, label in (("sources", "来源："), ("range", "材料时间跨度："),
                           ("focus", "关注点：")):
            if line.startswith(label):
                meta[key] = line[len(label):]
        if line.startswith("报告生成于 "):
            meta["date"] = line[len("报告生成于 "):]
    return {
        "title": data.get("title", ""),
        "subtitle": cover.get("lead", ""),
        "meta": meta,
        "slides": [p for p in pages if p.get("kind") not in ("cover", "sources")],
        "sources": data.get("sources") or [],
        "outline_chars": 0,
    }
