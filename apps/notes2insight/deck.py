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
from typing import Optional

from . import deck_theme
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
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out[k.strip()] = v
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

SLIDES_PROMPT = """你要把一份技术洞察报告改写成一套**作者自己回顾用**的宽屏幻灯片脚本。

读者就是报告作者本人，几周后回头看：他要在十几分钟内重新捡回这份报告的判断、证据和尚未解决的问题，
并且能顺着来源编号跳回原始笔记。幻灯片是给眼睛扫的，不是把报告再抄一遍：

- 信息密度：{density}——{density_note}。
- 一页只讲一件事。页标题写**结论**，不写话题，**不超过 {title_max} 个字**——"推理单价两年掉两个数量级"是标题，"关于推理成本"不是。
- 要点**不超过 {bullet_max} 个字**，带上具体数字和口径；一页 {bullets} 条。更细的论证、口径细节、反例写进 note，note 可以写 2-4 句。
- 报告里有关键数字的，优先做成 stat 或 bars 页，用数字说话，不要把数字埋在句子里。
- 同一组数字在整套幻灯片里只出现一次——做成了数字页或横条对比的，别的页就不要再画一遍，文字里提一句即可。
- 每条要点、每个数字都用 cites 标出来源编号，编号只能取自报告正文里已经出现过的 [N]，不要自己编号，没有把握就留空数组。
- 只使用报告里出现过的内容，不要补充报告之外的知识，**不要编造或换算数字**——数字必须是报告原文里写的。
- 专有名词保留英文原名，中文用全角标点。
- **JSON 字符串里绝对不要出现英文双引号**，要加引号一律用中文引号“”，否则 JSON 解析会失败。
- 每页的 section 写所属章节的短名（不超过 10 个字），会显示在页面左上角；不要单独做章节分隔页。

页面类型（kind）：
- "points"：最常用。title + lead（可空，不超过 {lead_max} 字）+ bullets
- "stat"：2-4 个关键数字并排。items 里每项 value 是数字本身（不超过 10 个字符，如 "5GW"、"$100M/MW"、"3-4×"），
  label 说明这个数字是什么（不超过 {label_max} 字）
- "bars"：同一个指标的几个数值放在一起比（不同时间、不同机构的估计、不同方案）。items 2-6 项，
  每项 label、value（纯数字，用来画条形长度）、display（显示出来的写法，如 "$100M/MW"），unit 写单位；
  所有项必须是同一个单位，没有可比的数字就不要用这种页
- "compare"：两种对立主张，用 columns（正好两列），每列 {compare_items} 条；head 写这一方的主张本身（不超过 16 字），
  不要写成"[1] 的观点""来源 A 的解释"这种
- "timeline"：时间线，events 3-6 个，每个 text 不超过 30 字

严格输出下面这个 JSON，不要用 Markdown 代码块包起来，不要写任何解释文字：

{{
  "slides": [
    {{"kind": "points", "section": "章节短名", "title": "页标题", "lead": "可为空",
      "bullets": [{{"text": "要点", "cites": [1, 3]}}], "note": "完整论证 / 软肋"}},
    {{"kind": "stat", "section": "章节短名", "title": "页标题", "lead": "",
      "items": [{{"value": "5GW", "label": "OpenAI 年末算力规模", "cites": [2]}}], "note": ""}},
    {{"kind": "bars", "section": "章节短名", "title": "页标题", "unit": "$M/MW",
      "items": [{{"label": "2025 年水平", "value": 12.5, "display": "$10-15M", "cites": [4]}},
                {{"label": "行业目标", "value": 100, "display": "$100M", "cites": [4, 6]}}], "note": ""}},
    {{"kind": "compare", "section": "章节短名", "title": "页标题", "lead": "",
      "columns": [{{"head": "一方主张", "items": [{{"text": "", "cites": []}}]}},
                  {{"head": "另一方主张", "items": [{{"text": "", "cites": []}}]}}],
      "note": "目前无法判定的原因"}},
    {{"kind": "timeline", "section": "章节短名", "title": "页标题",
      "events": [{{"date": "2026-03", "text": "发生了什么", "cites": [2]}}]}}
  ]
}}

结构要求（一共不超过 {target} 页，宁少勿多；封面页和来源索引页由程序生成，你不要输出）：
1. 开篇 1 页 points：这批材料共同回答了什么问题、最重要的变化是什么（取自执行摘要）
2. 关键数字 1-3 页 stat / bars（报告里有多少可比的数字就做多少，没有就不做）
3. 关键判断 2-4 页 points
4. 每一章 1 页（points、stat 或 bars，看这一章适合怎么表达），讲这一章的事实状态和核心张力
5. 分歧 1-3 页，能对立的尽量用 compare
6. 报告里如果有时间线或情景推演，给 1 页 timeline
7. 展望与可观测信号 1 页（写清"看什么指标、什么读数说明哪个情景在兑现"）
8. 启示 1 页

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


KINDS = ("points", "stat", "bars", "compare", "timeline", "section")


def _num(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return x if x == x and x >= 0 else None   # 排除 NaN 和负数（条形长度画不出负的）


def normalize(raw: dict, sources: list[dict]) -> list[dict]:
    """把模型输出收敛成渲染器能放心用的形状：丢掉空页、剔除不存在的来源编号。
    stat / bars 页的数字不全就降成 points 页或丢掉，宁可少一页图也不画错。"""
    valid = {s["idx"] for s in sources}
    slides: list[dict] = []
    for s in raw.get("slides") or []:
        if not isinstance(s, dict):
            continue
        kind = s.get("kind") if s.get("kind") in KINDS else "points"
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
        elif kind == "stat":
            items = []
            for it in (s.get("items") or [])[:4]:
                if isinstance(it, dict) and str(it.get("value", "")).strip():
                    items.append({"value": str(it["value"]).strip()[:16],
                                  "label": str(it.get("label", "")).strip(),
                                  "cites": _clean_cites(it.get("cites"), valid)})
            if not items:
                continue
            slide["items"] = items
        elif kind == "bars":
            items = []
            for it in (s.get("items") or [])[:6]:
                if not isinstance(it, dict):
                    continue
                v = _num(it.get("value"))
                if v is None or not str(it.get("label", "")).strip():
                    continue
                items.append({"label": str(it["label"]).strip(), "value": v,
                              "display": str(it.get("display") or "").strip() or f"{v:g}",
                              "cites": _clean_cites(it.get("cites"), valid)})
            if len(items) < 2 or max(i["value"] for i in items) <= 0:
                # 凑不成一组可比的数字：有说明文字的退成文字页，没有就丢
                bullets = [{"text": f"{i['label']}：{i['display']}", "cites": i["cites"]} for i in items]
                if not bullets:
                    continue
                slide.update(kind="points", bullets=bullets)
            else:
                slide["items"] = items
                slide["unit"] = str(s.get("unit") or "").strip()[:20]
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
        elif kind == "section":
            continue   # 新版不做独立的章节分隔页，章节名放在每页左上角
        slides.append(slide)
    if not slides:
        raise DeckError("模型没有产出任何有效页面")
    return slides


def build_deck(md: str, *, backend: str, api_key: str = "", model: str = "",
               api_base: str = "", timeout: int = 600, stop_flag=None, density: str = "high") -> dict:
    """报告 Markdown → 幻灯片脚本（未渲染）。density 决定每页写多少（见 deck_theme.DENSITY）。"""
    dn = deck_theme.DENSITY.get(density) or deck_theme.DENSITY["high"]
    high = dn is deck_theme.DENSITY["high"]
    skel = outline(md)
    if len(skel["text"]) < 200:
        raise DeckError("这份报告里没抽到可用的章节，可能不是 notes2insight 生成的报告")
    sources = parse_sources(md)
    target = min(dn["pages"], 12 + max(len(skel["chapters"]), 1))
    prompt = SLIDES_PROMPT.format(
        density=dn["label"], density_note=dn["note"], title_max=dn["title_max"], bullet_max=dn["bullet_max"],
        bullets=dn["bullets"], lead_max=60 if high else 40, label_max=30 if high else 20,
        compare_items="3-4" if high else "2-3",
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
/* 版面是一张 1600×900 的宽屏画布，按窗口等比缩放；颜色、字体、圆角、信息密度全部来自风格参数（deck_theme） */
:root { __THEME_VARS__ --fit: 1; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--surface); color: var(--ink); font-family: var(--font);
       -webkit-font-smoothing: antialiased; overflow: hidden; }
button { font: inherit; }

#viewport { position: fixed; inset: 0 0 40px 0; display: flex; align-items: center; justify-content: center; }
.canvas { position: relative; width: 1600px; height: 900px; flex: none; background: var(--bg);
          transform-origin: center center; overflow: hidden;
          padding: 64px 96px 72px; display: flex; flex-direction: column; }

/* 页眉：章节标签 + 页码，下面一条细线 */
.hd { display: flex; align-items: center; gap: 16px; height: 32px; font-size: 17px; color: var(--dim);
      border-bottom: 1px solid var(--line); padding-bottom: 14px; flex: none; }
.hd .mark { width: 14px; height: 14px; background: var(--accent); border-radius: min(var(--radius), 3px); flex: none; }
.hd .sec { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hd .no { font-variant-numeric: tabular-nums; }

/* 12 栏网格：标题占 10 栏，正文按页面类型排 */
.bd { flex: 1; display: grid; grid-template-columns: repeat(12, 1fr); column-gap: 32px;
      align-content: start; padding-top: calc(48px * var(--fit)); min-height: 0; }
h1 { grid-column: 1 / span 10; margin: 0; font-family: var(--display); font-weight: var(--title-weight);
     font-size: calc(60px * var(--fit)); line-height: 1.16; letter-spacing: -.01em; text-wrap: balance; }
.lead { grid-column: 1 / span 9; margin: calc(20px * var(--fit)) 0 0; font-size: calc(27px * var(--fit));
        line-height: 1.5; color: var(--dim); text-wrap: pretty; }
.body { grid-column: 1 / -1; margin-top: calc(44px * var(--fit)); }

/* 要点：左侧一个强调色小方块，行与行之间细线 */
ul.pts { list-style: none; margin: 0; padding: 0; width: calc(100% * 10 / 12); }
ul.pts li { display: grid; grid-template-columns: 38px 1fr; padding: calc(24px * var(--fit)) 0;
            border-top: 1px solid var(--line); font-size: calc(33px * var(--fit)); line-height: 1.5; text-wrap: pretty; }
ul.pts li:first-child { border-top: 0; padding-top: 0; }
/* 一页要点多（高密度常见）时排两栏，免得一长列挤到页底 */
ul.pts.two { width: 100%; display: grid; grid-template-columns: 1fr 1fr; column-gap: 56px; }
ul.pts.two li:nth-child(2) { border-top: 0; padding-top: 0; }
ul.pts li::before { content: ""; width: 12px; height: 12px; margin-top: .6em; background: var(--accent);
                    border-radius: min(var(--radius), 2px); }

/* 数字页：几个大数字并排，顶上一条强调色粗线 */
.stats { display: grid; gap: 32px; }
.stat { border-top: 4px solid var(--accent); padding-top: 22px; min-width: 0; }
.stat .v { font-family: var(--display); font-weight: var(--title-weight); font-size: calc(124px * var(--fit));
           line-height: 1.05; letter-spacing: -.02em; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; }
.stat .l { margin-top: 18px; font-size: calc(27px * var(--fit)); line-height: 1.45; color: var(--dim); text-wrap: pretty; }

/* 横条对比：标签 | 条形 | 数值，最大的一条用强调色，其余用灰 */
.bars { width: calc(100% * 11 / 12); }
.bars .unit { font-size: 19px; color: var(--dim); margin-bottom: 20px; }
.bar { display: grid; grid-template-columns: 4fr 6fr 2fr; column-gap: 28px; align-items: center;
       padding: calc(20px * var(--fit)) 0; border-top: 1px solid var(--line); }
.bar:first-of-type { border-top: 0; }
.bar .lab { font-size: calc(28px * var(--fit)); line-height: 1.4; text-wrap: pretty; }
.bar .track { height: 40px; }
.bar .fill { height: 100%; background: var(--line); min-width: 4px; border-radius: min(var(--radius), 4px); }
.bar.top .fill { background: var(--accent); }
.bar .num { font-size: calc(32px * var(--fit)); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }

/* 对比：两栏，中间一条竖线 */
.cols { display: grid; grid-template-columns: 1fr 1fr; column-gap: 64px; width: calc(100% * 11 / 12); }
.cols .col + .col { border-left: 1px solid var(--line); padding-left: 64px; }
.col h3 { margin: 0 0 18px; font-family: var(--display); font-size: calc(34px * var(--fit)); line-height: 1.3; text-wrap: balance;
          font-weight: var(--title-weight); padding-bottom: 14px; border-bottom: 3px solid var(--accent); }
.col ul.pts { width: 100%; }
.col ul.pts li { font-size: calc(29px * var(--fit)); }

/* 时间轴：横向一条线，事件挂在线上；事件太多时改竖排 */
.tl { position: relative; display: grid; column-gap: 28px; padding-top: 58px; }
.tl::before { content: ""; position: absolute; left: 0; right: 0; top: 44px; height: 2px; background: var(--ink); }
.tl .ev { position: relative; }
.tl .ev::before { content: ""; position: absolute; top: -21px; left: 0; width: 16px; height: 16px;
                  background: var(--accent); border-radius: min(var(--radius), 50%); }
.tl .d { position: absolute; top: -62px; left: 0; font-size: 24px; font-weight: 600; color: var(--accent);
         font-variant-numeric: tabular-nums; white-space: nowrap; }
.tl .t { margin-top: 20px; font-size: calc(28px * var(--fit)); line-height: 1.5; text-wrap: pretty; }
.tlv { list-style: none; margin: 0; padding: 0; width: calc(100% * 10 / 12); }
.tlv li { display: grid; grid-template-columns: 180px 1fr; padding: calc(14px * var(--fit)) 0;
          border-top: 1px solid var(--line); font-size: calc(29px * var(--fit)); line-height: 1.5; text-wrap: pretty; }
.tlv li:first-child { border-top: 0; }
.tlv .d { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; }

/* 封面 */
.cover .bd { align-content: center; padding-top: 0; }
.cover .kicker { grid-column: 1 / -1; font-size: 18px; color: var(--dim); margin-bottom: 36px; }
.cover .kicker::before { content: ""; display: inline-block; width: 56px; height: 8px; background: var(--accent);
                         margin-right: 18px; vertical-align: middle; border-radius: min(var(--radius), 2px); }
.cover h1 { font-size: calc(76px * var(--fit)); }
.cover .lead { font-size: calc(30px * var(--fit)); grid-column: 1 / span 9; }
.cover .meta { grid-column: 1 / span 8; margin-top: 56px; padding-top: 22px; border-top: 1px solid var(--line);
               font-size: 18px; line-height: 1.9; color: var(--dim); }
.section-only .bd { align-content: center; padding-top: 0; }

/* 来源角标 */
.cite { display: inline-block; min-width: 1.5em; margin-left: 6px; padding: 0 .3em; border: 0;
        background: var(--accent-soft); color: var(--accent); border-radius: min(var(--radius), 4px);
        font-size: .6em; line-height: 1.6; vertical-align: .35em; cursor: pointer; font-variant-numeric: tabular-nums; }
.cite:hover { background: var(--accent); color: var(--bg); }

/* 来源索引 */
table.src { width: 100%; border-collapse: collapse; font-size: 17px; }
table.src th, table.src td { text-align: left; padding: 9px 12px 9px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
table.src th { color: var(--dim); font-weight: 500; }
table.src a { color: var(--accent); text-decoration: none; }

/* 画布之外的操作条、备注、总览、来源抽屉 */
#foot { position: fixed; left: 0; right: 0; bottom: 0; height: 40px; display: flex; align-items: center;
        justify-content: space-between; gap: 16px; padding: 0 20px; font-size: 13px; color: var(--dim); }
#deckName { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#foot > span:last-child { flex: none; }
#foot button { background: none; border: 0; color: var(--dim); cursor: pointer; padding: 4px 8px; }
#foot button:hover { color: var(--accent); }
#foot kbd { border: 1px solid var(--line); padding: 0 5px; font-family: inherit; font-size: 11px; }
#note { position: fixed; left: 50%; bottom: 52px; transform: translateX(-50%); width: min(1100px, 92vw);
        background: var(--bg); border: 1px solid var(--line); border-left: 4px solid var(--accent);
        padding: 14px 20px; font-size: 15px; line-height: 1.75; color: var(--ink); z-index: 15;
        max-height: 38vh; overflow-y: auto; }
#grid { position: fixed; inset: 0; background: var(--surface); overflow-y: auto; padding: 40px;
        display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px;
        align-content: start; z-index: 30; }
.thumb { background: var(--bg); border: 1px solid var(--line); aspect-ratio: 16 / 9; padding: 14px 16px;
         cursor: pointer; overflow: hidden; }
.thumb:hover, .thumb.on { border-color: var(--accent); }
.thumb .n { color: var(--dim); font-size: 11px; }
.thumb .t { font-size: 14px; line-height: 1.45; margin-top: 8px; font-weight: 600; }
#drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(420px, 90vw); background: var(--bg);
          border-left: 1px solid var(--line); padding: 28px; overflow-y: auto; z-index: 40;
          transform: translateX(100%); transition: transform .18s ease; }
#drawer.on { transform: none; }
#drawer h2 { margin: 6px 0; font-size: 19px; line-height: 1.4; }
#drawer .row { color: var(--dim); font-size: 13px; line-height: 1.9; }
#drawer a.open { display: inline-block; margin-top: 18px; padding: 9px 16px; background: var(--accent);
                 color: var(--bg); text-decoration: none; font-size: 14px; border-radius: min(var(--radius), 6px); }
#drawer .close { position: absolute; top: 12px; right: 14px; background: none; border: 0; color: var(--dim);
                 font-size: 24px; cursor: pointer; }
.hidden { display: none !important; }
.printnote { display: none; }

@media print {
  @page { size: 1600px 900px; margin: 0; }
  body { overflow: visible; background: var(--bg); }
  #foot, #grid, #drawer, #note { display: none !important; }
  #viewport { position: static; display: block; }
  .canvas { transform: none !important; break-after: page; page-break-after: always; }
  .printnote { display: block; position: absolute; left: 96px; right: 96px; bottom: 20px; font-size: 13px;
               color: var(--dim); line-height: 1.5; }
  .cite { background: none; border: 1px solid var(--line); }
}
</style>
</head>
<body>
<div id="viewport"></div>
<div id="note" class="hidden"></div>
<div id="foot">
  <span id="deckName"></span>
  <span>
    <button id="btnPrev" aria-label="上一页">←</button><button id="btnNext" aria-label="下一页">→</button>
    <button id="btnGrid">总览 <kbd>Esc</kbd></button>
    <button id="btnNote">备注 <kbd>S</kbd></button>
    <button id="btnPrint">导出 PDF <kbd>P</kbd></button>
  </span>
</div>
<div id="grid" class="hidden"></div>
<div id="drawer"><button class="close" id="drawerClose" aria-label="关闭">&times;</button><div id="drawerBody"></div></div>
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
    return list.map(function (n) {
      return '<button class="cite" data-src="' + n + '" title="' +
             esc((SRC[n] && SRC[n].title) || ("来源 " + n)) + '">' + n + "</button>";
    }).join("");
  }
  function pts(list, wide) {
    return '<ul class="pts' + (wide && list.length >= 5 ? " two" : "") + '">' + list.map(function (b) {
      return "<li><span>" + clean(b.text) + cites(b.cites) + "</span></li>";
    }).join("") + "</ul>";
  }

  function inner(s) {
    var head = "<h1>" + esc(s.title) + "</h1>" + (s.lead ? '<p class="lead">' + clean(s.lead) + "</p>" : "");
    if (s.kind === "cover") {
      return '<div class="kicker">技术洞察演示</div><h1>' + esc(s.title) + "</h1>" +
        (s.lead ? '<p class="lead">' + esc(s.lead) + "</p>" : "") +
        '<div class="meta">' + (s.meta || []).map(esc).join("<br />") + "</div>";
    }
    if (s.kind === "sources") {
      var rows = DECK.sources.map(function (x) {
        var name = x.uri ? '<a href="' + esc(x.uri) + '">' + esc(x.title) + "</a>" : esc(x.title);
        return "<tr><td>[" + x.idx + "]</td><td>" + name + "</td><td>" + esc(x.date || "—") +
               "</td><td>" + esc(x.folder) + "</td></tr>";
      }).join("");
      return head + '<div class="body"><table class="src"><tr><th>编号</th><th>笔记</th><th>日期</th><th>位置</th></tr>' +
             rows + "</table></div>";
    }
    if (s.kind === "section") return head;
    if (s.kind === "stat") {
      return head + '<div class="body"><div class="stats" style="grid-template-columns:repeat(' +
        s.items.length + ',minmax(0,1fr))">' + s.items.map(function (it) {
          return '<div class="stat"><div class="v">' + esc(it.value) + '</div><div class="l">' +
                 clean(it.label) + cites(it.cites) + "</div></div>";
        }).join("") + "</div></div>";
    }
    if (s.kind === "bars") {
      var max = Math.max.apply(null, s.items.map(function (it) { return it.value; })) || 1;
      return head + '<div class="body"><div class="bars">' +
        (s.unit ? '<div class="unit">单位：' + esc(s.unit) + "</div>" : "") +
        s.items.map(function (it) {
          var pct = Math.max(1, it.value / max * 100);
          return '<div class="bar' + (it.value === max ? " top" : "") + '"><div class="lab">' + clean(it.label) +
                 cites(it.cites) + '</div><div class="track"><div class="fill" style="width:' + pct.toFixed(1) +
                 '%"></div></div><div class="num">' + esc(it.display) + "</div></div>";
        }).join("") + "</div></div>";
    }
    if (s.kind === "compare") {
      return head + '<div class="body"><div class="cols">' + s.columns.map(function (c) {
        return '<div class="col"><h3>' + clean(c.head) + "</h3>" + pts(c.items) + "</div>";
      }).join("") + "</div></div>";
    }
    if (s.kind === "timeline") {
      if (s.events.length <= 5) {
        return head + '<div class="body"><div class="tl" style="grid-template-columns:repeat(' +
          s.events.length + ',1fr)">' + s.events.map(function (e) {
            return '<div class="ev"><div class="d">' + esc(e.date) + '</div><div class="t">' +
                   clean(e.text) + cites(e.cites) + "</div></div>";
          }).join("") + "</div></div>";
      }
      return head + '<div class="body"><ul class="tlv">' + s.events.map(function (e) {
        return '<li><span class="d">' + esc(e.date) + "</span><span>" + clean(e.text) + cites(e.cites) + "</span></li>";
      }).join("") + "</ul></div>";
    }
    return head + '<div class="body">' + pts(s.bullets || [], true) + "</div>";
  }

  function canvas(s, i) {
    var cls = "canvas" + (s.kind === "cover" ? " cover" : "") + (s.kind === "section" ? " section-only" : "");
    var hd = s.kind === "cover" ? "" :
      '<div class="hd"><span class="mark"></span><span class="sec">' + esc(s.section || "") +
      '</span><span class="no">' + (i + 1) + " / " + SLIDES.length + "</span></div>";
    return '<div class="' + cls + '">' + hd + '<div class="bd">' + inner(s) + "</div>" +
           (s.note ? '<div class="printnote">' + esc(s.note) + "</div>" : "") + "</div>";
  }

  // 内容放不下就整页字号一起往下收，最多收到 70%；还放不下就让它出界（比悄悄截掉强）
  // 起点是信息密度定的字号缩放（高密度字小一档），放不下再往下收，最多收到起点的 70%
  var DENS = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--dens")) || 1;
  function fit(el) {
    var f = DENS;
    el.style.setProperty("--fit", f);
    var bd = el.querySelector(".bd");
    while (bd && bd.scrollHeight > bd.clientHeight + 2 && f > DENS * 0.7) {
      f = Math.round((f - 0.05) * 100) / 100;
      el.style.setProperty("--fit", f);
    }
    // 大数字不换行（"100GW+" 的加号掉到下一行就读不懂了）：有放不下的，整页的数字一起缩到
    // 都放得下的那个字号，几个数字大小一致
    var vs = el.querySelectorAll(".stat .v");
    if (vs.length) {
      var size = parseFloat(getComputedStyle(vs[0]).fontSize);
      var tooWide = function () {
        return Array.prototype.some.call(vs, function (v) { return v.scrollWidth > v.clientWidth + 1; });
      };
      while (tooWide() && size > 40) {
        size -= 4;
        Array.prototype.forEach.call(vs, function (v) { v.style.fontSize = size + "px"; });
      }
    }
  }
  function scaleToWindow() {
    var c = document.querySelector("#viewport .canvas");
    if (!c) return;
    var vp = $("viewport");
    var k = Math.min(vp.clientWidth / 1600, vp.clientHeight / 900);
    c.style.transform = "scale(" + k + ")";
  }

  function show(i) {
    cur = Math.max(0, Math.min(SLIDES.length - 1, i));
    var s = SLIDES[cur];
    $("viewport").innerHTML = canvas(s, cur);
    var c = $("viewport").firstChild;
    fit(c);
    scaleToWindow();
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
    if (!e.target.closest("#drawer")) $("drawer").classList.remove("on");
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
  window.addEventListener("resize", scaleToWindow);
  window.addEventListener("hashchange", function () {
    var n = parseInt(location.hash.slice(1), 10) - 1;
    if (n >= 0 && n !== cur) show(n);
  });

  $("btnPrev").onclick = function () { show(cur - 1); };
  $("btnNext").onclick = function () { show(cur + 1); };
  $("btnGrid").onclick = function () { toggleGrid(); };
  $("btnNote").onclick = function () { notesOn = !notesOn; show(cur); };
  $("btnPrint").onclick = function () { window.print(); };
  $("drawerClose").onclick = function () { $("drawer").classList.remove("on"); };
  $("deckName").textContent = DECK.title;

  // 打印时所有页铺开，每页一张画布
  window.addEventListener("beforeprint", function () {
    $("viewport").innerHTML = SLIDES.map(canvas).join("");
    Array.prototype.forEach.call(document.querySelectorAll("#viewport .canvas"), fit);
  });
  window.addEventListener("afterprint", function () { show(cur); });

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
    """把 slide 脚本渲染成一份自包含 HTML。不引用任何外部资源，拷到哪都能开。
    外观全部来自 deck["theme"]（没有就是默认的瑞士网格），风格参数也存进 HTML，复用时读得回来。"""
    pages, sources, title = _prepare(deck, vault_name, report_filename)
    theme = deck_theme.clean_theme(deck.get("theme") or {})
    if (deck.get("theme") or {}).get("description"):
        theme["description"] = deck["theme"]["description"]

    payload = json.dumps({"title": title, "slides": pages, "sources": sources, "theme": theme},
                         ensure_ascii=False)
    # </script> 出现在 JSON 里会提前闭合脚本块；转义斜杠是最省事的防法
    payload = payload.replace("</", "<\\/")
    head = title.replace("&", "&amp;").replace("<", "&lt;")
    return (_TEMPLATE.replace("__THEME_VARS__", deck_theme.css_vars(theme))
            .replace("__DECK_JSON__", payload).replace("__TITLE__", head))


def generate(md: str, *, backend: str, api_key: str = "", model: str = "", api_base: str = "",
             timeout: int = 600, vault_name: str = "", report_filename: str = "",
             stop_flag=None, theme: Optional[dict] = None) -> tuple[str, dict]:
    """报告 Markdown → (HTML 字符串, 幻灯片脚本)。"""
    theme = deck_theme.clean_theme(theme) if theme else dict(deck_theme.DEFAULT_THEME)
    deck = build_deck(md, backend=backend, api_key=api_key, model=model,
                      api_base=api_base, timeout=timeout, stop_flag=stop_flag, density=theme["density"])
    deck["theme"] = theme
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


def _set_theme_fonts(prs, latin: str = PPT_LATIN_FONT, cjk: str = PPT_CJK_FONT) -> None:
    """把主题字体方案里的西文和中日韩字体换成风格里选的字体族（默认无衬线）。

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
                     rf"\g<1>{cjk}\g<2>", xml)
    xml = re.sub(r'(<a:latin typeface=")[^"]*(")', rf"\g<1>{latin}\g<2>", xml)
    part._blob = xml.encode("utf-8")


def render_pptx(deck: dict, out_path: str, *, vault_name: str = "",
                report_filename: str = "") -> str:
    """把 slide 脚本渲染成 .pptx，版面和配色跟 HTML 用同一套风格参数（deck["theme"]）。

    和 HTML 版的取舍不同：PPTX 里没法做"点开角标看原文"的抽屉，所以来源信息走两条路——
    正文后面留 [N] 角标，备注页里写清楚每个 [N] 是哪篇笔记、在库里的什么位置。
    来源索引页的笔记名带 obsidian:// 超链接，播放器认这个协议就能点回原文。"""
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
        from pptx.util import Emu, Inches, Pt
    except ImportError as e:
        raise DeckError("未安装 python-pptx（pip install python-pptx），无法导出 PPTX") from e

    pages, sources, _ = _prepare(deck, vault_name, report_filename)
    src_by_id = {s["idx"]: s for s in sources}
    th = deck_theme.clean_theme(deck.get("theme") or {})

    def rgb(key):
        return RGBColor.from_string(th[key].lstrip("#").upper())

    INK, DIM, LINE, ACCENT, BG = rgb("ink"), rgb("dim"), rgb("line"), rgb("accent"), rgb("bg")
    TITLE_BOLD = th["title_weight"] >= 600

    prs = Presentation()
    _set_theme_fonts(prs, *deck_theme.PPT_FONTS[th["font"]])
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    W, H = prs.slide_width, prs.slide_height
    PAD_X, PAD_TOP = Inches(0.8), Inches(0.5)
    BODY_W = W - 2 * PAD_X
    GAP = Inches(0.27)
    COL = (BODY_W - 11 * GAP) / 12           # 12 栏网格的一栏

    def span(n):
        return int(COL * n + GAP * (n - 1))

    def style(run, size, *, bold=False, color=None):
        # 不设 run.font.name：字体统一由主题决定（见 _set_theme_fonts）
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color if color is not None else INK

    def box(slide, left, top, width, height, *, anchor=None):
        tf = slide.shapes.add_textbox(left, top, width, height).text_frame
        tf.word_wrap = True
        # python-pptx 量不了文本高度，写个 normAutofit 让 PowerPoint 自己缩：
        # 偶尔一页内容特别多时，宁可字小一点也不要掉出版心
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        if anchor is not None:
            tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = 0
        return tf

    def para(tf, first=False, space_before=0, space_after=6):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.space_before, p.space_after = Pt(space_before), Pt(space_after)
        return p

    def write(p, text, size, *, bold=False, color=None, cites=None):
        run = p.add_run()
        run.text = text
        style(run, size, bold=bold, color=color)
        if cites:
            tail = p.add_run()
            tail.text = " " + _cite_tag(cites)
            style(tail, max(size - 5, 9), color=ACCENT)

    def rect(slide, left, top, width, height, color):
        sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, max(int(height), Emu(9525)))
        sh.fill.solid()
        sh.fill.fore_color.rgb = color
        sh.line.fill.background()
        sh.shadow.inherit = False
        return sh

    def background(slide):
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = BG

    def cited_ids(page):
        out = []
        groups = [page.get("bullets", []), page.get("events", []), page.get("items", [])]
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

    def header(slide, page, no):
        """页眉：强调色小方块 + 章节名 + 页码，下面一条细线。"""
        rect(slide, PAD_X, PAD_TOP + Inches(0.05), Inches(0.12), Inches(0.12), ACCENT)
        write(para(box(slide, PAD_X + Inches(0.25), PAD_TOP - Inches(0.03), Inches(8), Inches(0.3)),
                   first=True, space_after=0), page.get("section") or "", 11, color=DIM)
        tf = box(slide, W - PAD_X - Inches(2), PAD_TOP - Inches(0.03), Inches(2), Inches(0.3))
        p = para(tf, first=True, space_after=0)
        p.alignment = PP_ALIGN.RIGHT
        write(p, f"{no} / {len(pages)}", 11, color=DIM)
        rect(slide, PAD_X, PAD_TOP + Inches(0.35), BODY_W, Emu(9525), LINE)

    def heading(slide, page, top=Inches(1.25)):
        t = _plain(page["title"])
        size = _fit(t, 32, 26, 26)
        tf = box(slide, PAD_X, top, span(10), Inches(1.3))
        write(para(tf, first=True, space_after=0), t, size, bold=TITLE_BOLD)
        cur = top + Inches(0.62 if len(t) <= 26 else 1.05)
        if page.get("lead"):
            lead = _plain(page["lead"])
            write(para(box(slide, PAD_X, cur + Inches(0.12), span(9), Inches(0.8)), first=True,
                       space_after=0), lead, 15, color=DIM)
            cur += Inches(0.5 if len(lead) <= 50 else 0.85)
        return cur + Inches(0.45)

    def bullets(slide, items, left, top, width, size):
        """要点：每条前面一个强调色小方块，条与条之间一条细线。"""
        n = max(len(items), 1)
        avail = H - top - Inches(0.6)
        row = min(Inches(1.05), avail // n)
        for i, b in enumerate(items):
            y = top + row * i
            if i:
                rect(slide, left, y - Inches(0.08), width, Emu(9525), LINE)
            rect(slide, left, y + Inches(0.12), Inches(0.1), Inches(0.1), ACCENT)
            tf = box(slide, left + Inches(0.3), y, width - Inches(0.3), row - Inches(0.12))
            write(para(tf, first=True, space_after=0), _plain(b["text"]), size, cites=b.get("cites"))

    def sources_table(slide, top, rows_data):
        rows = len(rows_data) + 1
        tbl = slide.shapes.add_table(rows, 4, PAD_X, top, BODY_W, Inches(0.32) * rows).table
        for col, w in zip(tbl.columns, (Inches(0.9), Inches(7.4), Inches(1.4), Inches(2.0))):
            col.width = w
        def cell_text(r, c, text, *, bold=False, color=None, size=10, uri=""):
            cell = tbl.cell(r, c)
            cell.fill.background()
            p = cell.text_frame.paragraphs[0]
            run = p.add_run()
            run.text = text
            style(run, size, bold=bold, color=color)
            if uri:
                try:
                    run.hyperlink.address = uri       # obsidian:// 跳回原文
                except Exception:
                    pass                              # 不认这个协议也不该让导出失败
        for j, head in enumerate(("编号", "笔记", "日期", "位置")):
            cell_text(0, j, head, bold=True, color=DIM, size=11)
        for i, src in enumerate(rows_data, 1):
            cell_text(i, 0, f"[{src['idx']}]", color=DIM)
            cell_text(i, 1, src["title"], uri=src.get("uri", ""))
            cell_text(i, 2, src["date"] or "—", color=DIM)
            cell_text(i, 3, src["folder"], color=DIM)

    for no, page in enumerate(pages, 1):
        slide = prs.slides.add_slide(blank)
        background(slide)
        kind = page["kind"]

        if kind == "cover":
            rect(slide, PAD_X, Inches(1.75), Inches(0.75), Inches(0.1), ACCENT)
            write(para(box(slide, PAD_X + Inches(0.95), Inches(1.62), Inches(6), Inches(0.35)), first=True,
                       space_after=0), "技术洞察演示", 13, color=DIM)
            t = page["title"]
            write(para(box(slide, PAD_X, Inches(2.25), span(10), Inches(2.0)), first=True, space_after=0),
                  t, _fit(t, 38, 30, 34), bold=TITLE_BOLD)
            cur = Inches(3.35) if len(t) <= 34 else Inches(4.15)
            if page.get("lead"):
                write(para(box(slide, PAD_X, cur, span(9), Inches(1.0)), first=True, space_after=0),
                      page["lead"], 17, color=DIM)
                cur += Inches(0.9)
            rect(slide, PAD_X, cur + Inches(0.2), span(8), Emu(9525), LINE)
            mf = box(slide, PAD_X, cur + Inches(0.35), span(8), Inches(1.8))
            for i, line in enumerate(page.get("meta", [])):
                write(para(mf, first=(i == 0), space_after=3), line, 11, color=DIM)
            continue

        header(slide, page, no)

        if kind == "sources":
            # 一页塞不下 20 多行，表格会掉出版心；按 14 行一页切开，续页标题加"（续）"
            chunks = [sources[i:i + SOURCES_PER_SLIDE]
                      for i in range(0, len(sources), SOURCES_PER_SLIDE)] or [[]]
            for n, chunk in enumerate(chunks):
                if n:
                    slide = prs.slides.add_slide(blank)
                    background(slide)
                    header(slide, page, no)
                head = dict(page, title=page["title"] + ("（续）" if n else ""))
                sources_table(slide, heading(slide, head), chunk)
            continue

        if kind == "section":
            heading(slide, page, top=Inches(2.8))
            notes(slide, page)
            continue

        top = heading(slide, page)

        if kind == "stat":
            items = page.get("items", [])
            n = max(len(items), 1)
            w = (span(12) - GAP * (n - 1)) // n
            for i, it in enumerate(items):
                x = PAD_X + (w + GAP) * i
                rect(slide, x, top, w, Inches(0.06), ACCENT)
                v = it["value"]
                write(para(box(slide, x, top + Inches(0.25), w, Inches(1.1)), first=True, space_after=0),
                      v, 54 if len(v) <= 6 else (42 if len(v) <= 9 else 32), bold=TITLE_BOLD)
                write(para(box(slide, x, top + Inches(1.45), w, Inches(1.2)), first=True, space_after=0),
                      _plain(it.get("label", "")), 14, color=DIM, cites=it.get("cites"))
        elif kind == "bars":
            items = page.get("items", [])
            if page.get("unit"):
                write(para(box(slide, PAD_X, top - Inches(0.1), span(6), Inches(0.3)), first=True,
                           space_after=0), "单位：" + page["unit"], 11, color=DIM)
                top += Inches(0.3)
            mx = max((it["value"] for it in items), default=1) or 1
            row = min(Inches(0.75), (H - top - Inches(0.6)) // max(len(items), 1))
            lab_w, track_w = span(4), span(6)
            for i, it in enumerate(items):
                y = top + row * i
                if i:
                    rect(slide, PAD_X, y, span(12), Emu(9525), LINE)
                write(para(box(slide, PAD_X, y, lab_w, row, anchor=MSO_ANCHOR.MIDDLE), first=True,
                           space_after=0), _plain(it["label"]), 14, cites=it.get("cites"))
                bw = max(int(track_w * it["value"] / mx), Inches(0.04))
                rect(slide, PAD_X + lab_w + GAP, y + row // 2 - Inches(0.12), bw, Inches(0.24),
                     ACCENT if it["value"] == mx else LINE)
                write(para(box(slide, PAD_X + lab_w + track_w + 2 * GAP, y, span(2), row,
                               anchor=MSO_ANCHOR.MIDDLE), first=True, space_after=0),
                      it.get("display", ""), 15, bold=True)
        elif kind == "compare":
            cw = span(6) - GAP
            for i, col in enumerate(page.get("columns", [])[:2]):
                x = PAD_X + (span(6) + GAP) * i
                if i:
                    rect(slide, x - GAP, top, Emu(9525), H - top - Inches(0.7), LINE)
                write(para(box(slide, x, top, cw, Inches(0.5)), first=True, space_after=0),
                      _plain(col.get("head", "")), 18, bold=TITLE_BOLD)
                rect(slide, x, top + Inches(0.55), cw, Inches(0.04), ACCENT)
                bullets(slide, col.get("items", []), x, top + Inches(0.8), cw, 14)
        elif kind == "timeline":
            events = page.get("events", [])
            if len(events) <= 5:
                n = max(len(events), 1)
                w = (span(12) - GAP * (n - 1)) // n
                line_y = top + Inches(0.55)
                rect(slide, PAD_X, line_y, span(12), Inches(0.03), INK)
                for i, e in enumerate(events):
                    x = PAD_X + (w + GAP) * i
                    write(para(box(slide, x, top, w, Inches(0.4)), first=True, space_after=0),
                          e.get("date") or "—", 14, bold=True, color=ACCENT)
                    rect(slide, x, line_y - Inches(0.06), Inches(0.15), Inches(0.15), ACCENT)
                    write(para(box(slide, x, line_y + Inches(0.3), w, Inches(2.2)), first=True,
                               space_after=0), _plain(e["text"]), 14, cites=e.get("cites"))
            else:
                bullets(slide, [{"text": f"{e.get('date') or '—'}　{e['text']}", "cites": e.get("cites")}
                                for e in events], PAD_X, top, span(10), 14)
        else:
            items = page.get("bullets", [])
            if len(items) >= 5:
                # 要点多（高密度常见）排两栏，跟 HTML 版一致
                half = (len(items) + 1) // 2
                size = 14 if max(len(_plain(b["text"])) for b in items) > 45 else 16
                bullets(slide, items[:half], PAD_X, top, span(6) - GAP, size)
                bullets(slide, items[half:], PAD_X + span(6) + GAP, top, span(6) - GAP, size)
            else:
                size = 19 if len(items) <= 3 else 17
                if max((len(_plain(b["text"])) for b in items), default=0) > 50:
                    size -= 2
                bullets(slide, items, PAD_X, top, span(10), size)
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
    out = {
        "title": data.get("title", ""),
        "subtitle": cover.get("lead", ""),
        "meta": meta,
        "slides": [p for p in pages if p.get("kind") not in ("cover", "sources")],
        "sources": data.get("sources") or [],
        "outline_chars": 0,
        # 老演示没存风格，复用时按默认的瑞士网格重新排；也去掉老版本的章节分隔页和配图
        "theme": data.get("theme") or dict(deck_theme.DEFAULT_THEME),
    }
    out["slides"] = [p for p in out["slides"] if p.get("kind") != "section"]
    for p in out["slides"]:
        p.pop("image", None)
    return out
