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


def _header(md: str) -> tuple[dict, str, str, str]:
    """frontmatter、去掉折叠块之后的正文、标题、副标题。"""
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
    return meta, body, title, subtitle


def outline(md: str) -> dict:
    """把整份报告压成骨架（每章只留小标题和首段）。返回 {title, subtitle, meta, chapters, text}。"""
    meta, body, title, subtitle = _header(md)
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


# 排版时每一部分的**全文**都送模型。以前只送"小标题 + 首段"，章节里的数字、口径、
# 谁说了什么全在后面几段，模型看不到，做出来的页面就只剩一句概括。
MAX_PART_CHARS = 14000      # 单个部分送模型的上限
MAX_PLAN_CHARS = 90000      # 规划那一步看的全文上限
GROUP_CHARS = 9000          # 写稿时相邻的小部分合成一组，少调几次模型
MAX_WORKERS = 4             # 写稿并发数


def parts(md: str) -> dict:
    """把报告按 ## 切成几部分（执行摘要、关键判断、各章、分歧、展望、启示），每部分保留全文。
    返回 {title, subtitle, meta, parts: [{key, head, text, chapter}]}。"""
    meta, body, title, subtitle = _header(md)
    out: list[dict] = []
    for head, content in _split_h2(body):
        if head == subtitle or head.startswith(("方法、来源", "阅读地图")):
            continue
        is_ch = bool(re.match(r"第\s*\d+\s*章", head))
        if is_ch or any(head.startswith(k) for k in FULL_SECTIONS):
            text = content[:MAX_PART_CHARS] + ("\n\n（这一部分过长，后面已截断）" if len(content) > MAX_PART_CHARS else "")
            out.append({"key": f"P{len(out) + 1}", "head": head, "text": text, "chapter": is_ch})
    return {"title": title, "subtitle": subtitle, "meta": meta, "parts": out}


# --------------------------------------------------------------------------
# 让模型排版：先规划整套页面，再按部分分头写
# --------------------------------------------------------------------------

AUDIENCE = """读者就是报告作者本人，几周后回头看：他要在十几分钟内重新捡回这份报告的判断、证据（具体数字、口径、
谁说的）和尚未解决的问题，并且能顺着来源编号跳回原始笔记。幻灯片是给眼睛扫的，不是把报告再抄一遍。"""

PLAN_PROMPT = """你要把一份技术洞察报告做成一套宽屏幻灯片。这一步只做**规划**：定下一共几页、每页讲什么、
用什么版式；具体文字下一步由别人按部分分头写。

{audience}

规划要求：
- 一共 {pmin}-{pmax} 页（封面和来源索引页由程序生成，不算在内，你也不要规划）。信息密度：{density}——{density_note}。
- 顺序：开篇 1 页（这批材料共同回答了什么、最重要的变化是什么）→ 关键判断 → 每一章 1-3 页（内容多、数字多的章多给页）
  → 分歧 → 情景与可观测信号（写清看什么指标、什么读数说明哪个情景在兑现）→ 启示。
- 一页只讲一件事。title 直接写这一页的**结论**，不写话题，不超过 {title_max} 个字——"推理单价两年掉两个数量级"是标题，"关于推理成本"不是。
- brief 写清这一页要用原文里的哪些**具体内容**：哪几个数字、哪几方的说法、哪组对照、哪条因果链。下一步写稿的人
  只看得到这一页所属部分的原文，brief 是他唯一的指引，写得越具体越好（50-120 字）。
- 版式按内容选，不要大半都是 points：
  原文里有表格、多方多维度对照 → table；同一指标的几个数值 → bars；几个并列的关键数字 → stat；
  因果链、传导路径、流程 → chain；两种对立主张 → compare；两个独立维度交叉（如速度快慢 × 可控与否）→ matrix；
  有具体时间点（年月、季度）的事件 → timeline（情景推演没有具体时间点的用 table：情景 | 触发条件 | 看什么信号）；一句分量很重的原话或判断 → quote；其余用 points。
  除 points 外，同一种版式整套不超过 {max_same} 页，也不要连着三页用同一种版式——表格好用，但满篇表格一样难读。
- 同一组数字在整套幻灯片里只出现一次，规划时就分配好由哪一页用。
- part 填这一页内容出自哪一部分（原文里每部分前面标的 P1、P2…）。一页只能属于一个部分。

严格输出下面这个 JSON，不要用 Markdown 代码块包起来，不要写任何解释文字；JSON 字符串里不要出现英文双引号，要加引号用中文“”：
{{"pages": [{{"part": "P1", "kind": "points", "title": "页标题", "brief": "这一页要用的具体内容"}}]}}

--- 报告标题 ---
{title}

--- 报告全文（按部分编号）---
{body}
"""

KINDS_SPEC = """页面类型（kind）和各自的字段（所有页都有 kind、section、title、lead、note；lead 可空，不超过 {lead_max} 字）：
- "points"：bullets: [{{"text": "要点", "cites": [1]}}]，一页 {bullets} 条
- "stat"：2-6 个并列的关键数字。items: [{{"value": "5GW", "label": "这个数字是什么（不超过 {label_max} 字）", "cites": []}}]；
  value 只写数字本身，不超过 10 个字符（如 "5GW"、"$100M/MW"、"3-4×"）
- "bars"：同一指标的几个数值放在一起比。"unit": "单位"，items 2-6 项：
  [{{"label": "2025 年水平", "value": 12.5, "display": "$10-15M", "cites": []}}]；value 是纯数字（画条形长度），
  所有项必须同一单位、同一口径（"倍"和"年"、"%"和"GW"绝对不能放一组）
- "table"：对照表。"columns": ["维度", "A 方", "B 方"]（2-5 列），
  "rows": [{{"cells": ["与表头一一对应的短文本"], "cites": []}}]（2-7 行，每格不超过 24 字）
- "chain"：因果链 / 传导路径 / 流程。"steps": [{{"head": "环节（不超过 12 字）", "text": "这一环发生了什么、凭什么这么说（30-60 字）", "cites": []}}]，
  3-5 步，按先后或因果顺序
- "compare"：两种对立主张。"columns" 正好两列：[{{"head": "主张本身（不超过 16 字）", "items": [{{"text": "", "cites": []}}]}}]，
  每列 {compare_items} 条；head 写主张，不要写成"[1] 的观点""来源 A 的解释"
- "matrix"：2×2 矩阵。"x": "横轴，左低右高，如 能力提升：慢 → 快"，"y": "纵轴，下低上高"，
  "cells" 正好 4 个：[{{"head": "", "text": "不超过 40 字", "cites": []}}]，顺序是左上、右上、左下、右下
- "timeline"："events": [{{"date": "2026-03", "text": "不超过 30 字", "cites": []}}]，3-6 个；date 必须是时间（年月、季度、"明年"）
- "quote"："quote": "原话或关键判断（不超过 80 字）"，"by": "出处，如 Karpathy、报告判断"，"cites": []，可加 bullets 1-3 条补充"""

WRITE_PROMPT = """你在把一份技术洞察报告写成宽屏幻灯片。整套页面已经规划好，这一步只写下面这 {n} 页。

{audience}

写稿要求：
- 信息密度：{density}——{density_note}。要点不超过 {bullet_max} 个字，带上具体数字、口径和主体（谁说的、哪家公司、哪个基准）。
  更细的论证、口径细节、反例写进 note，note 写 2-4 句。
- 只使用下面原文里的内容，不要补充原文之外的知识，**不要编造或换算数字**——数字必须是原文里写的。
- 每条要点、每个数字、每行表格都用 cites 标出来源编号，编号只能取自原文里已经出现过的 [N]，不要自己编号，没有把握就留空数组。
- 按规划的顺序输出正好 {n} 页。title 可以照原文微调；kind 照规划，原文撑不起这种版式（比如凑不出同口径的数字）就改用 points。
- section 写所属章节的短名（不超过 10 个字），会显示在页面左上角。
- 专有名词保留英文原名，中文用全角标点。**JSON 字符串里绝对不要出现英文双引号**，要加引号一律用中文引号“”。

{kinds}

--- 要写的页（规划）---
{plan}

--- 报告标题 ---
{title}

--- 执行摘要（只作背景；除非规划要求，不要重复这里的内容）---
{summary}

--- 来源编号对照（cites 只能用这些编号）---
{sources}

--- 原文 ---
{text}

严格输出 JSON，不要用 Markdown 代码块包起来，不要写任何解释文字：
{{"slides": [ ...正好 {n} 页... ]}}
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


KINDS = ("points", "stat", "bars", "table", "chain", "compare", "matrix", "timeline", "quote", "section")


_RE_UNIT_NOISE = re.compile(r"[\d.,，\s~～\-–—+约近超过逾以上左右]+")


def _unit_of(display: str) -> str:
    """"$10-15M" → "$M"，"约4-5年" → "年"，"1.5×" → "×"：去掉数字和修饰词，剩下的就是单位。"""
    parts = [p for p in _RE_UNIT_NOISE.split(str(display or "").lower()) if p]
    u = "".join(dict.fromkeys(parts))          # "70%-80%" 两段都是 %，算一个
    return {"倍": "×", "x": "×"}.get(u, u)


_RE_TIMEISH = re.compile(r"(19|20)\d\d|\d{1,2}\s*月|[Qq][1-4]|\d+\s*(年|个月|月|周|天|日)|今年|明年|去年|上半年|下半年|年初|年中|年末|年底")


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
            for it in (s.get("items") or [])[:6]:
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
            # 单位不一样的数字（1.5 倍和 4-5 年）画成一组条形是误导，跟数字不全一样退成文字页
            mixed_units = len({u for u in (_unit_of(i["display"]) for i in items) if u}) > 1
            if len(items) < 2 or max(i["value"] for i in items) <= 0 or mixed_units:
                # 凑不成一组可比的数字：有说明文字的退成文字页，没有就丢
                bullets = [{"text": f"{i['label']}：{i['display']}", "cites": i["cites"]} for i in items]
                if not bullets:
                    continue
                slide.update(kind="points", bullets=bullets)
            else:
                slide["items"] = items
                slide["unit"] = str(s.get("unit") or "").strip()[:20]
        elif kind == "table":
            heads = [str(h).strip()[:30] for h in (s.get("columns") or []) if str(h).strip()][:5]
            rows = []
            for r in (s.get("rows") or [])[:8]:
                cells = r.get("cells") if isinstance(r, dict) else r
                if not isinstance(cells, list):
                    continue
                cells = [str(c).strip() for c in cells][:len(heads)]
                cells += [""] * (len(heads) - len(cells))
                if any(cells):
                    rows.append({"cells": cells,
                                 "cites": _clean_cites(r.get("cites") if isinstance(r, dict) else [], valid)})
            if len(heads) < 2 or len(rows) < 2:
                # 撑不起一张表：有内容的行退成要点，"第一格：其余格"
                bullets = [{"text": "：".join(x for x in (r["cells"][0], "；".join(c for c in r["cells"][1:] if c)) if x),
                            "cites": r["cites"]} for r in rows]
                if not bullets:
                    continue
                slide.update(kind="points", bullets=bullets)
            else:
                slide.update(columns=heads, rows=rows)
        elif kind == "chain":
            steps = []
            for st in (s.get("steps") or [])[:6]:
                if isinstance(st, dict) and (str(st.get("head", "")).strip() or str(st.get("text", "")).strip()):
                    steps.append({"head": str(st.get("head", "")).strip(), "text": str(st.get("text", "")).strip(),
                                  "cites": _clean_cites(st.get("cites"), valid)})
            if len(steps) < 2:
                if not steps:
                    continue
                slide.update(kind="points", bullets=[{"text": "：".join(x for x in (st["head"], st["text"]) if x),
                                                      "cites": st["cites"]} for st in steps])
            else:
                slide["steps"] = steps
        elif kind == "matrix":
            cells = []
            for c in (s.get("cells") or [])[:4]:
                if isinstance(c, dict):
                    cells.append({"head": str(c.get("head", "")).strip(), "text": str(c.get("text", "")).strip(),
                                  "cites": _clean_cites(c.get("cites"), valid)})
            if len(cells) < 4 or not any(c["head"] or c["text"] for c in cells):
                bullets = [{"text": "：".join(x for x in (c["head"], c["text"]) if x), "cites": c["cites"]}
                           for c in cells if c["head"] or c["text"]]
                if not bullets:
                    continue
                slide.update(kind="points", bullets=bullets)
            else:
                slide.update(cells=cells, x=str(s.get("x") or "").strip()[:40], y=str(s.get("y") or "").strip()[:40])
        elif kind == "quote":
            q = str(s.get("quote") or "").strip()
            bullets = _clean_items(s.get("bullets"), valid)[:3]
            if not q:
                if not bullets:
                    continue
                slide.update(kind="points", bullets=bullets)
            else:
                slide.update(quote=q, by=str(s.get("by") or "").strip()[:40],
                             cites=_clean_cites(s.get("cites"), valid), bullets=bullets)
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
            # 时间轴的"日期"得真是时间；模型偶尔拿它当编号用（"信号1""阶段二"），那就是一张清单，退成要点页
            if sum(1 for e in evs if _RE_TIMEISH.search(e["date"])) * 2 < len(evs):
                slide.update(kind="points", bullets=[{"text": e["text"], "cites": e["cites"]} for e in evs])
            else:
                slide["events"] = evs
        elif kind == "section":
            continue   # 新版不做独立的章节分隔页，章节名放在每页左上角
        slides.append(slide)
    if not slides:
        raise DeckError("模型没有产出任何有效页面")
    return slides


def _plan_body(skel: dict) -> str:
    text = "\n\n".join(f"【{p['key']}】## {p['head']}\n{p['text']}" for p in skel["parts"])
    if len(text) > MAX_PLAN_CHARS:      # 极端长的报告：规划只看前面，写稿时各部分仍是全文
        text = text[:MAX_PLAN_CHARS] + "\n\n（全文过长已截断）"
    return text


def _group_plan(plan: list[dict], skel: dict) -> list[dict]:
    """把规划好的页按所属部分分组；相邻的小部分合成一组（少调几次模型）。
    返回 [{keys, pages: [(序号, 规划)]}]，组内保持规划顺序。"""
    order = [p["key"] for p in skel["parts"]]
    size = {p["key"]: len(p["text"]) for p in skel["parts"]}
    by_part: dict[str, list] = {}
    for i, pg in enumerate(plan):
        by_part.setdefault(pg["part"], []).append((i, pg))
    groups: list[dict] = []
    for key in order:
        if key not in by_part:
            continue
        g = groups[-1] if groups else None
        if g and g["chars"] + size[key] <= GROUP_CHARS and len(g["pages"]) + len(by_part[key]) <= 8:
            g["keys"].append(key)
            g["pages"] += by_part[key]
            g["chars"] += size[key]
        else:
            groups.append({"keys": [key], "pages": list(by_part[key]), "chars": size[key]})
    return groups


def _clean_plan(raw: dict, skel: dict) -> list[dict]:
    keys = [p["key"] for p in skel["parts"]]
    plan, last = [], keys[0]
    for pg in raw.get("pages") or []:
        if not isinstance(pg, dict) or not str(pg.get("title", "")).strip():
            continue
        part = str(pg.get("part", "")).strip().upper()
        part = part if part in keys else last          # 没写或写错部分：跟上一页同一部分
        last = part
        kind = pg.get("kind") if pg.get("kind") in KINDS and pg.get("kind") != "section" else "points"
        plan.append({"part": part, "kind": kind, "title": str(pg["title"]).strip(),
                     "brief": str(pg.get("brief", "")).strip()})
    if not plan:
        raise DeckError("模型没有规划出任何页面")
    return plan


def build_deck(md: str, *, backend: str, api_key: str = "", model: str = "",
               api_base: str = "", timeout: int = 600, stop_flag=None, density: str = "high",
               progress=None) -> dict:
    """报告 Markdown → 幻灯片脚本（未渲染）。density 决定每页写多少（见 deck_theme.DENSITY）。

    两步：先把整份报告给模型规划出整套页面（每页的结论标题、版式、要用的具体内容），
    再按部分分头写——每组只带这几页所属部分的原文全文，几组并发。
    progress(done, total, msg) 可选，用来报进度。"""
    from concurrent.futures import ThreadPoolExecutor

    dn = deck_theme.DENSITY.get(density) or deck_theme.DENSITY["high"]
    high = dn is deck_theme.DENSITY["high"]
    skel = parts(md)
    if sum(len(p["text"]) for p in skel["parts"]) < 200:
        raise DeckError("这份报告里没抽到可用的章节，可能不是 notes2insight 生成的报告")
    sources = parse_sources(md)
    n_ch = sum(1 for p in skel["parts"] if p["chapter"])
    pmax = min(dn["pages"], 8 + 2 * max(n_ch, 1))
    pmin = max(6, pmax - 6)
    call = dict(api_key=api_key, model=model, api_base=api_base, timeout=timeout, stop_flag=stop_flag)
    report = progress or (lambda *a: None)

    report(0, 1, f"规划页面：通读全文 {sum(len(p['text']) for p in skel['parts'])} 字")
    raw = llm.complete(PLAN_PROMPT.format(
        audience=AUDIENCE, pmin=pmin, pmax=pmax, max_same=max(3, pmax // 5),
        density=dn["label"], density_note=dn["note"],
        title_max=dn["title_max"], title=skel["title"] or "（无标题）", body=_plan_body(skel),
    ), backend, max_tokens=8000, **call)
    # 规划常会比要求多出一两页；只防极端情况，不从尾巴上砍——尾巴是展望和启示，砍了最可惜
    plan = _clean_plan(_extract_json(raw), skel)[:pmax + 10]
    groups = _group_plan(plan, skel)

    text_of = {p["key"]: p for p in skel["parts"]}
    summary = next((p["text"] for p in skel["parts"] if p["head"].startswith("执行摘要")), "")[:3000]
    kinds = KINDS_SPEC.format(lead_max=60 if high else 40, label_max=30 if high else 20,
                              bullets=dn["bullets"], compare_items="3-4" if high else "2-3")
    brief = _sources_brief(sources)

    def write(g: dict) -> list:
        plan_txt = "\n".join(
            f"{k + 1}. [{pg['kind']}] {pg['title']}\n   要用的内容：{pg['brief'] or '（按标题从原文里取）'}"
            for k, (_, pg) in enumerate(g["pages"]))
        text = "\n\n".join(f"## {text_of[k]['head']}\n{text_of[k]['text']}" for k in g["keys"])
        prompt = WRITE_PROMPT.format(
            n=len(g["pages"]), audience=AUDIENCE, density=dn["label"], density_note=dn["note"],
            bullet_max=dn["bullet_max"], kinds=kinds, plan=plan_txt, title=skel["title"] or "（无标题）",
            summary=summary or "（无）", sources=brief, text=text)
        budget = min(16000, 1500 + 1100 * len(g["pages"]))
        for attempt in (1, 2):
            try:
                out = _extract_json(llm.complete(prompt, backend, max_tokens=budget, **call))
                return [x for x in (out.get("slides") or []) if isinstance(x, dict)]
            except DeckError:
                if attempt == 2:
                    raise

    report(1, len(groups) + 1, f"规划好 {len(plan)} 页，分 {len(groups)} 组写稿")
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(write, g): g for g in groups}
        done = 0
        for fut in futs:
            g = futs[fut]
            got = fut.result()                       # 停止 / 出错都从这里抛出去
            for (i, pg), sl in zip(g["pages"], got):
                sl.setdefault("kind", pg["kind"])
                sl.setdefault("title", pg["title"])
                results[i] = sl
            done += 1
            report(1 + done, len(groups) + 1,
                   f"写好第 {done}/{len(groups)} 组：{'、'.join(text_of[k]['head'][:14] for k in g['keys'])}")

    slides = normalize({"slides": [results[i] for i in sorted(results)]}, sources)
    return {"title": skel["title"], "subtitle": skel["subtitle"], "meta": skel["meta"],
            "slides": slides, "sources": sources, "plan": plan,
            "outline_chars": sum(len(p["text"]) for p in skel["parts"])}


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
/* 一页要点多（高密度常见）时排两栏：先排满左栏再排右栏，阅读顺序是竖着读 */
.pts2 { display: grid; grid-template-columns: 1fr 1fr; column-gap: 56px; align-items: start; }
.pts2 ul.pts { width: 100%; }
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

/* 对照表：表头一条粗线，行间细线，第一列是维度名 */
table.tbl { width: 100%; border-collapse: collapse; font-size: calc(31px * var(--fit)); line-height: 1.45; }
table.tbl th { text-align: left; font-weight: 600; color: var(--dim); font-size: .84em; padding: 0 24px 12px 0;
               border-bottom: 3px solid var(--accent); vertical-align: bottom; }
table.tbl td { padding: calc(20px * var(--fit)) 28px calc(20px * var(--fit)) 0; border-bottom: 1px solid var(--line);
               vertical-align: top; text-wrap: pretty; }
table.tbl td:first-child { font-weight: 600; }

/* 因果链：几个环节横排，环节之间一个箭头 */
.chain { display: grid; align-items: stretch; column-gap: 0; }
.chain .st { border-top: 4px solid var(--accent); padding-top: 20px; min-width: 0; }
.chain .st h3 { margin: 0 0 12px; font-family: var(--display); font-weight: var(--title-weight);
                font-size: calc(38px * var(--fit)); line-height: 1.3; text-wrap: balance; }
.chain .st p { margin: 0; font-size: calc(31px * var(--fit)); line-height: 1.5; color: var(--ink); text-wrap: pretty; }
.chain .ar { align-self: start; padding-top: 18px; text-align: center; font-size: 40px; color: var(--accent); }

/* 2×2 矩阵：左边纵轴、下边横轴，四格之间留缝 */
.mx { display: grid; grid-template-columns: 44px 1fr 1fr; grid-template-rows: auto auto auto; gap: 16px;
      width: calc(100% * 11 / 12); }
.mx .y { grid-row: 1 / span 2; writing-mode: vertical-rl; transform: rotate(180deg); text-align: center;
         font-size: 22px; color: var(--dim); border-left: 2px solid var(--ink); padding-left: 10px; }
.mx .x { grid-column: 2 / span 2; text-align: center; font-size: 20px; color: var(--dim);
         border-top: 2px solid var(--ink); padding-top: 8px; }
.mx .cell { background: var(--surface); padding: calc(28px * var(--fit)) 32px; border-radius: min(var(--radius), 8px); min-width: 0; }
.mx .cell h3 { margin: 0 0 8px; font-family: var(--display); font-weight: var(--title-weight);
               font-size: calc(36px * var(--fit)); line-height: 1.3; color: var(--accent); }
.mx .cell p { margin: 0; font-size: calc(30px * var(--fit)); line-height: 1.5; text-wrap: pretty; }

/* 引语：左边一条强调色粗线，大字 */
.qt { width: calc(100% * 10 / 12); border-left: 8px solid var(--accent); padding-left: 40px; }
.qt blockquote { margin: 0; font-family: var(--display); font-weight: var(--title-weight);
                 font-size: calc(46px * var(--fit)); line-height: 1.4; text-wrap: pretty; }
.qt .by { margin-top: 18px; font-size: calc(24px * var(--fit)); color: var(--dim); }
.qt + ul.pts, .qt + .pts2 { margin-top: calc(40px * var(--fit)); }

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
    if (wide && list.length >= 5) {
      var half = Math.ceil(list.length / 2);
      return '<div class="pts2">' + pts(list.slice(0, half)) + pts(list.slice(half)) + "</div>";
    }
    return '<ul class="pts">' + list.map(function (b) {
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
      // 五六个数字分两行排，一行三个
      return head + '<div class="body"><div class="stats" style="row-gap:40px;grid-template-columns:repeat(' +
        (s.items.length > 4 ? 3 : s.items.length) + ',minmax(0,1fr))">' + s.items.map(function (it) {
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
    if (s.kind === "table") {
      return head + '<div class="body"><table class="tbl"><tr>' + s.columns.map(function (h) {
          return "<th>" + esc(h) + "</th>";
        }).join("") + "</tr>" + s.rows.map(function (r) {
          return "<tr>" + r.cells.map(function (c, j) {
            return "<td>" + clean(c) + (j === r.cells.length - 1 ? cites(r.cites) : "") + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</table></div>";
    }
    if (s.kind === "chain") {
      var colsCss = s.steps.map(function () { return "minmax(0,1fr)"; }).join(" 56px ");
      return head + '<div class="body"><div class="chain" style="grid-template-columns:' + colsCss + '">' +
        s.steps.map(function (st, i) {
          return (i ? '<div class="ar">→</div>' : "") + '<div class="st"><h3>' + clean(st.head) + "</h3><p>" +
                 clean(st.text) + cites(st.cites) + "</p></div>";
        }).join("") + "</div></div>";
    }
    if (s.kind === "matrix") {
      var cell = function (c) {
        return '<div class="cell"><h3>' + clean(c.head) + "</h3><p>" + clean(c.text) + cites(c.cites) + "</p></div>";
      };
      return head + '<div class="body"><div class="mx"><div class="y">' + esc(s.y || "") + "</div>" +
        cell(s.cells[0]) + cell(s.cells[1]) + cell(s.cells[2]) + cell(s.cells[3]) +
        '<div class="x">' + esc(s.x || "") + "</div></div></div>";
    }
    if (s.kind === "quote") {
      return head + '<div class="body"><div class="qt"><blockquote>' + clean(s.quote) + cites(s.cites) +
        "</blockquote>" + (s.by ? '<div class="by">' + esc(s.by) + "</div>" : "") + "</div>" +
        (s.bullets && s.bullets.length ? pts(s.bullets) : "") + "</div>";
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
             stop_flag=None, theme: Optional[dict] = None, progress=None) -> tuple[str, dict]:
    """报告 Markdown → (HTML 字符串, 幻灯片脚本)。"""
    theme = deck_theme.clean_theme(theme) if theme else dict(deck_theme.DEFAULT_THEME)
    deck = build_deck(md, backend=backend, api_key=api_key, model=model, api_base=api_base,
                      timeout=timeout, stop_flag=stop_flag, density=theme["density"], progress=progress)
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
        groups = [page.get("bullets", []), page.get("events", []), page.get("items", []),
                  page.get("rows", []), page.get("steps", []), page.get("cells", []), [page]]
        groups += [col.get("items", []) for col in page.get("columns", []) if isinstance(col, dict)]
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
            per_row = 3 if len(items) > 4 else max(len(items), 1)
            w = (span(12) - GAP * (per_row - 1)) // per_row
            two_rows = len(items) > per_row
            for i, it in enumerate(items):
                x = PAD_X + (w + GAP) * (i % per_row)
                y = top + (Inches(2.35) if i >= per_row else 0)
                rect(slide, x, y, w, Inches(0.06), ACCENT)
                v = it["value"]
                big = 54 if len(v) <= 6 else (42 if len(v) <= 9 else 32)
                write(para(box(slide, x, y + Inches(0.2), w, Inches(1.0)), first=True, space_after=0),
                      v, big - (10 if two_rows else 0), bold=TITLE_BOLD)
                write(para(box(slide, x, y + Inches(1.2 if two_rows else 1.45), w, Inches(1.0)), first=True,
                           space_after=0), _plain(it.get("label", "")), 13 if two_rows else 14,
                      color=DIM, cites=it.get("cites"))
        elif kind == "table":
            heads, rows_ = page.get("columns", []), page.get("rows", [])
            row_h = min(Inches(0.6), (H - top - Inches(0.7)) // (len(rows_) + 1))
            tbl = slide.shapes.add_table(len(rows_) + 1, len(heads), PAD_X, top, span(12),
                                         row_h * (len(rows_) + 1)).table
            size = 13 if len(heads) <= 3 else 12
            for j, h in enumerate(heads):
                c = tbl.cell(0, j)
                c.fill.solid()
                c.fill.fore_color.rgb = ACCENT
                write(c.text_frame.paragraphs[0], h, size, bold=True, color=BG)
            for i, r in enumerate(rows_, 1):
                for j, txt in enumerate(r["cells"]):
                    c = tbl.cell(i, j)
                    c.fill.solid()
                    c.fill.fore_color.rgb = BG if i % 2 else rgb("surface")
                    write(c.text_frame.paragraphs[0], _plain(txt), size, bold=(j == 0),
                          cites=r.get("cites") if j == len(r["cells"]) - 1 else None)
        elif kind == "chain":
            steps = page.get("steps", [])
            n = max(len(steps), 1)
            arrow = Inches(0.45)
            w = (span(12) - arrow * (n - 1)) // n
            for i, st in enumerate(steps):
                x = PAD_X + (w + arrow) * i
                if i:
                    tf = box(slide, x - arrow, top + Inches(0.05), arrow, Inches(0.5))
                    p = para(tf, first=True, space_after=0)
                    p.alignment = PP_ALIGN.CENTER
                    write(p, "→", 22, color=ACCENT)
                rect(slide, x, top, w - Inches(0.1), Inches(0.06), ACCENT)
                write(para(box(slide, x, top + Inches(0.2), w - Inches(0.1), Inches(0.75)), first=True,
                           space_after=0), _plain(st.get("head", "")), 17, bold=TITLE_BOLD)
                write(para(box(slide, x, top + Inches(0.95), w - Inches(0.1), Inches(2.6)), first=True,
                           space_after=0), _plain(st.get("text", "")), 14, cites=st.get("cites"))
        elif kind == "matrix":
            axis_w = Inches(0.45)
            cw = (span(11) - axis_w - GAP) // 2
            ch = min(Inches(1.9), (H - top - Inches(1.2)) // 2)
            for i, c in enumerate(page.get("cells", [])[:4]):
                x = PAD_X + axis_w + (cw + GAP) * (i % 2)
                y = top + (ch + Inches(0.15)) * (i // 2)
                rect(slide, x, y, cw, ch, rgb("surface"))
                tf = box(slide, x + Inches(0.2), y + Inches(0.12), cw - Inches(0.4), ch - Inches(0.24))
                write(para(tf, first=True, space_after=4), _plain(c.get("head", "")), 16, bold=True, color=ACCENT)
                write(para(tf), _plain(c.get("text", "")), 13, cites=c.get("cites"))
            if page.get("y"):
                tb = slide.shapes.add_textbox(PAD_X, top, axis_w, 2 * ch)
                tb.rotation = 270
                tb.left, tb.top, tb.width, tb.height = (PAD_X + axis_w // 2 - ch, top + ch - axis_w // 2,
                                                         2 * ch, axis_w)
                p = tb.text_frame.paragraphs[0]
                p.alignment = PP_ALIGN.CENTER
                write(p, page["y"], 11, color=DIM)
            if page.get("x"):
                p = para(box(slide, PAD_X + axis_w, top + 2 * ch + Inches(0.25), 2 * cw + GAP, Inches(0.35)),
                         first=True, space_after=0)
                p.alignment = PP_ALIGN.CENTER
                write(p, page["x"], 11, color=DIM)
        elif kind == "quote":
            q = _plain(page.get("quote", ""))
            qh = Inches(1.4 if len(q) <= 40 else 2.0)
            rect(slide, PAD_X, top, Inches(0.09), qh, ACCENT)
            tf = box(slide, PAD_X + Inches(0.35), top, span(10), qh)
            write(para(tf, first=True, space_after=6), q, 26 if len(q) <= 40 else 22, bold=TITLE_BOLD,
                  cites=page.get("cites"))
            if page.get("by"):
                write(para(tf), page["by"], 13, color=DIM)
            if page.get("bullets"):
                bullets(slide, page["bullets"], PAD_X, top + qh + Inches(0.4), span(10), 15)
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
