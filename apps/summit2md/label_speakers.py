"""给已有的整理稿补标发言人：不重新整理、不重新翻译，只在每一轮发言开头加「**名字**：」。

11 月以后从「新单集」更新的单集没有推测发言人，整理稿是一大段不分人的对话。重做一遍整理稿
要把翻译也重新花一遍钱，这里只补名字：

- 读 speech/ 里「## 演讲稿」下面的段落（双语稿是原文段 + 紧跟的「> 译文」段），交给模型判断每段是谁说的；
- 一段里中途换了人（主持人的问题和嘉宾回答的开头挤在一段里），模型给出换人那句话的开头，
  原文、译文各在那句话前面断开——文字一个字不改；
- 参考材料：视频简介（多半列着嘉宾和主持人）、节目设置里的主持人、YouTube 字幕里的「>>」换人标记；
- 写回前核对：把加上的名字去掉、断开的段落接回去，原文、译文都必须跟原来一字不差，不然不写；
- 改之前备份到节目文件夹的 .cache/speaker_backup/（文字记录、笔记在 speaker_backup_transcripts/、speaker_backup_notes/），只留最早那一份。划线、批注都在段落文字里，加名字不影响它们。

命令行：
    python3 -m apps.summit2md.label_speakers --file <speech.md> [--file ...] [--dry-run]
    python3 -m apps.summit2md.label_speakers --show "a16z - Videos"     # 这个节目里还没标的全部
默认用 Anthropic API 的 claude-sonnet-5-5（--model 可改）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from typing import Optional

from core import llm_config
from core import vault as core_vault

from . import pipeline
from . import subscriptions_store

SPEECH_HEADING = "## 演讲稿"
AI_LABEL_LINE = "- 发言人：AI 基于上下文推测标注，可能不准确，仅供参考"
TRANSCRIPT_SEE_SPEECH_LINE = "- 发言人：AI 推测标注在整理稿里（这份文字记录没能对上，没有分人）"
DEFAULT_MODEL = "claude-sonnet-5-5"
BATCH_CHARS = 60000          # 一批交给模型的原文字数；一小时的节目通常一批就够
TURN_HINT_WORDS = 14         # 字幕换人标记：每一轮只给开头这么多个词，够模型对上位置
_LABEL_RE = re.compile(r"^(>\s*)?\*\*([^*\n]{1,60})\*\*\s*[：:]\s*")
_VIDEO_ID_RE = re.compile(r"^- 链接：.*?[?&]v=([A-Za-z0-9_-]{11})", re.M)
_SUB_LANG_RE = re.compile(r"^- 字幕来源：.*?（([A-Za-z-]+)[），]", re.M)


class LabelError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 整理稿的结构
# ---------------------------------------------------------------------------

def split_doc(content: str) -> tuple[str, str, str]:
    """(开头到「## 演讲稿」那一行, 正文, 正文后面的部分：读后感/我的高亮/批注脚注)"""
    head, sep, rest = content.partition(SPEECH_HEADING)
    if not sep:
        raise LabelError("没有「## 演讲稿」这一节")
    # 正文后面接的是读者自己的东西（读后感、我的高亮、批注脚注）；正文里的「## 章节标题」不算
    m = re.search(r"^(## (?:读后感|我的高亮|以前的划线|划线和批注|其他批注)[ \t]*$|\[\^note-\d+\]:)", rest, re.M)
    body, tail = (rest[:m.start()], rest[m.start():]) if m else (rest, "")
    return head + sep, body, tail


def units_of(body: str) -> list[dict]:
    """正文 → [{"orig": 原文段, "zh": 译文段或 None}]。双语稿原文段后面紧跟一个「> 」开头的译文段。"""
    blocks = [b for b in re.split(r"\n\s*\n", body.strip("\n")) if b.strip()]
    units: list[dict] = []
    for b in blocks:
        if b.lstrip().startswith(">") and units and units[-1]["zh"] is None and not units[-1]["orig"].lstrip().startswith(">"):
            units[-1]["zh"] = b
        else:
            units.append({"orig": b, "zh": None, "heading": b.lstrip().startswith("#")})
    return units


def strip_label(block: str) -> str:
    m = _LABEL_RE.match(block)
    return (m.group(1) or "") + block[m.end():] if m else block


def add_label(block: str, name: str, zh: bool) -> str:
    if zh:
        m = re.match(r"^(>\s*)", block)
        lead = m.group(1) if m else ""
        return f"{lead}**{name}**：{block[len(lead):]}"
    return f"**{name}**: {block}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"(?m)^>\s?", "", text))


def _texts(units: list[dict]) -> tuple[str, str]:
    return ("".join(_norm(strip_label(u["orig"])) for u in units),
            "".join(_norm(strip_label(u["zh"])) for u in units if u["zh"]))


# ---------------------------------------------------------------------------
# 参考材料：简介、主持人、字幕换人标记
# ---------------------------------------------------------------------------

def host_names_for(show_dir: str) -> str:
    folder = os.path.normpath(show_dir)
    for sub in subscriptions_store.list_all("podcast"):
        if os.path.normpath(sub.get("folder") or "") == folder and sub.get("host_name"):
            return sub["host_name"]
    try:
        return pipeline._load_manifest(show_dir).get("host_name") or ""
    except Exception:  # noqa: BLE001
        return ""


def _caption_lang(show_dir: str, speech_path: str) -> list[str]:
    tpath = os.path.join(show_dir, "transcripts", os.path.basename(speech_path))
    try:
        with open(tpath, encoding="utf-8") as f:
            m = _SUB_LANG_RE.search(f.read(4000))
    except OSError:
        m = None
    lang = m.group(1) if m else "en"
    return [lang] + [x for x in ("en", "zh-Hans", "zh") if x != lang]


def caption_turns(vtt_path: str) -> list[str]:
    """字幕按「>>」换人标记切成一轮一轮，每轮只留开头几个词。字幕里没有这种标记就返回空。"""
    turns: list[list[str]] = []
    last = None
    marked = False
    for _, raw_lines in pipeline._parse_vtt_cues(vtt_path):
        for raw in raw_lines:
            line, change = pipeline._clean_cue_text([raw])
            if not line or line == last:
                continue
            last = line
            if change or not turns:
                marked = marked or change
                turns.append([])
            turns[-1].append(line)
    if not marked:
        return []
    out = []
    for words in turns:
        ws = " ".join(words).split()
        out.append(" ".join(ws[:TURN_HINT_WORDS]) + (" …" if len(ws) > TURN_HINT_WORDS else ""))
    return out


def episode_context(show_dir: str, speech_path: str, head: str) -> dict:
    """简介和字幕换人标记。本地缓存里没有就让 yt-dlp 只取字幕和简介（不下载音视频）；取不到也照常标。"""
    ctx = {"description": "", "turns": []}
    m = _VIDEO_ID_RE.search(head)
    if not m:
        return ctx
    status: dict = {}
    try:
        sub = pipeline.download_subtitle(m.group(1), os.path.join(show_dir, ".cache", "subtitles"),
                                         _caption_lang(show_dir, speech_path), status=status)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 取简介/字幕失败（{e}），只看正文推测", flush=True)
        return ctx
    if not sub:
        print(f"  ⚠️ 取简介/字幕失败（{status.get('reason') or '没有字幕'}），只看正文推测", flush=True)
        return ctx
    ctx["description"] = sub.get("description") or ""
    try:
        ctx["turns"] = caption_turns(sub["path"])
    except Exception:  # noqa: BLE001
        pass
    return ctx


# ---------------------------------------------------------------------------
# 问模型
# ---------------------------------------------------------------------------

PROMPT = """你是播客文字编辑。下面是一期节目整理好的文字稿，分成编号段落。请判断每一段是谁在说话。

节目：{show}
本期标题：{title}{hosts_line}

视频简介（常列出嘉宾和主持人的全名；为空可忽略）：
\"\"\"
{description}
\"\"\"
{turns_block}
判断依据：简介里的名单、自我介绍、被点名（"Ami, what do you think"）、提问与回答的关系、
"thanks for having me" 这类话。规则：
- 一定要写人名（全名，照抄简介或主持人名单里的写法），同一个人全篇只用一种写法。
  只要能从简介、主持人名单、自我介绍、对方称呼、节目常识（比如这档节目的固定主持人是谁）
  知道这个人是谁，就写名字，不要写「主持人」「嘉宾」。
- 只有全篇都没法知道这个人叫什么时，才写「主持人」或「嘉宾」；连角色都判断不了写「未知发言人」。
- 相邻段落明显是同一个人接着说，就标同一个名字。
- 一段里中途换了人（比如主持人问完问题、嘉宾在同一段里开始回答），就在这一段写出换人的位置：
  照抄换人那句话开头的 4-8 个词（必须是这一段原文里一字不差、连续的一串），
  再照抄对应中文译文里那句话开头的 4-10 个字（同样一字不差）。没有中文译文就不写中文部分。
  只在确实换人时才这样写，一段里换好几次就写好几组。

输出格式，每段一行，编号照抄，不要漏段，不要输出其他任何内容：
12: Sarah Wang
13: Sarah Wang >> Ami Luttwak @ "Yes. First, great to be" @ "是的。首先，很高兴"
{numbered_note}
文字稿段落：
{paragraphs}
"""

_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(.+?)\s*$")
_ROLE_ALIASES = {"host": "主持人", "主持": "主持人", "主持人": "主持人", "guest": "嘉宾", "嘉宾": "嘉宾",
                 "未知": "未知发言人", "未知发言人": "未知发言人", "unknown": "未知发言人", "unknown speaker": "未知发言人",
                 "不明": "未知发言人", "未知说话人": "未知发言人", "speaker": "未知发言人"}


def norm_name(name: str) -> str:
    """模型偶尔写成 Host、未知、Unknown 这类：统一成「主持人」「嘉宾」「未知发言人」，后面才能按角色找回名字。"""
    n = name.strip().strip('"「」*')
    return _ROLE_ALIASES.get(n.lower(), n)
_SWITCH_RE = re.compile(r'>>\s*([^@>]+?)\s*@\s*"([^"]+)"(?:\s*@\s*"([^"]*)")?')


def parse_reply(raw: str, lo: int, hi: int) -> dict[int, dict]:
    """{段号: {"name": 开头的人, "switches": [(换成谁, 原文开头, 译文开头或""), ...]}}"""
    got: dict[int, dict] = {}
    for line in raw.splitlines():
        m = _LINE_RE.match(line)
        if not m or not lo <= int(m.group(1)) <= hi:
            continue
        rest = m.group(2)
        first = norm_name(rest.split(">>", 1)[0])
        switches = [(norm_name(s.group(1)), s.group(2), s.group(3) or "") for s in _SWITCH_RE.finditer(rest)]
        if first:
            got[int(m.group(1))] = {"name": first, "switches": switches}
    return got


def ask_model(units: list[dict], ctx: dict, show: str, title: str, hosts: str, llm: dict,
              cache_dir: str) -> dict[int, dict]:
    hosts_line = pipeline.host_prompt_line(hosts)
    turns = ctx.get("turns") or []
    turns_block = ""
    if turns:
        turns_block = ("\nYouTube 字幕里的换人标记（每一轮换人后的开头几个词，按时间顺序；字幕是语音识别的，"
                       "措辞跟文字稿不完全一样，只用来判断哪里换了人、一共几轮）：\n"
                       + "\n".join(f"T{i}: {t}" for i, t in enumerate(turns, 1)) + "\n")
    numbered = [(i, f"[{i}] {u['orig']}" + (f"\n（译文：{re.sub(r'(?m)^>\s?', '', u['zh'])}）" if u["zh"] else ""))
                for i, u in enumerate(units, 1) if not u.get("heading")]
    batches, cur, size = [], [], 0
    for i, text in numbered:
        if cur and size + len(text) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append((i, text))
        size += len(text)
    if cur:
        batches.append(cur)

    result: dict[int, dict] = {}
    known = pipeline.parse_host_names(hosts)
    for batch in batches:
        lo, hi = batch[0][0], batch[-1][0]
        note = f"\n（这一批是第 {lo} 到第 {hi} 段，共 {len(units)} 段；编号中间跳过的是章节标题。）\n"
        if lo > 1:
            named = [n for n in known if n not in ROLE_LABELS]
            if named:
                note += ("前面的段落已经认出这些人，后面同一个人照样写名字、写法不变：" + "、".join(named) + "\n")
            tail = [f"[{i}] {result[i]['name']}：{units[i - 1]['orig'][:200]}" for i in range(max(1, lo - 3), lo) if i in result]
            if tail:
                note += "前一批最后几段（只作上下文，不用输出）：\n" + "\n".join(tail) + "\n"
        prompt = PROMPT.format(show=show, title=title, hosts_line=hosts_line,
                               description=(ctx.get("description") or "")[:4000], turns_block=turns_block,
                               numbered_note=note, paragraphs="\n\n".join(t for _, t in batch))
        got = parse_reply(pipeline._cached_summarize(
            prompt, llm["backend"], api_key=llm["api_key"], model=llm["model"], api_base=llm.get("api_base", ""),
            cache_dir=cache_dir, max_tokens=8000, timeout=600), lo, hi)
        missing = [i for i, _ in batch if i not in got]
        if missing:
            got.update(ask_missing(missing, numbered, got, show, title, hosts_line, ctx, llm, cache_dir))
            missing = [i for i, _ in batch if i not in got]
        if len(missing) > 0.25 * len(batch):
            raise LabelError(f"模型只答了 {len(batch) - len(missing)}/{len(batch)} 段（第 {lo}-{hi} 段），没有改文件")
        # 补问之后还缺的零星几段：沿用前一段的人（前面没有就用后一段的）
        for i in missing:
            near = [k for k in sorted(got) if k < i] or [k for k in sorted(got) if k > i]
            if near:
                got[i] = {"name": got[near[-1] if near[0] < i else near[0]]["name"], "switches": [], "guessed": True}
        result.update(got)
        for g in got.values():
            for n in [g["name"], *(s[0] for s in g["switches"])]:
                if n not in known and n not in ROLE_LABELS:
                    known.append(n)
    return resolve_roles(result, units, show, title, ctx, hosts, llm, cache_dir)


MISSING_PROMPT = """你在给一期播客文字稿标发言人，上一次回答漏了几段。下面是漏掉的段落，以及它们前后已经标好的段落。

节目：{show}
本期标题：{title}{hosts_line}

视频简介：
\"\"\"
{description}
\"\"\"

已经认出的人：{names}

前后已标好的段落（只作参考，不用输出）：
{context}

请只给下面这些段落标发言人，规则同前：写人名，实在不知道名字才写「主持人」「嘉宾」。
每段一行「编号: 名字」，一段里换了人照前面的格式写「编号: 甲 >> 乙 @ "原文开头" @ "译文开头"」，不要别的内容：
{paragraphs}
"""


def ask_missing(missing: list[int], numbered: list[tuple[int, str]], got: dict[int, dict], show: str, title: str,
                hosts_line: str, ctx: dict, llm: dict, cache_dir: str) -> dict[int, dict]:
    text = dict(numbered)
    near = sorted({k for i in missing for k in range(i - 2, i + 3) if k in got and k in text})
    context = "\n".join(f"[{k}] {got[k]['name']}：{text[k][len(f'[{k}] '):][:200]}" for k in near)
    names = sorted({g["name"] for g in got.values()} - set(ROLE_LABELS))
    prompt = MISSING_PROMPT.format(show=show, title=title, hosts_line=hosts_line,
                                   description=(ctx.get("description") or "")[:3000], names="、".join(names),
                                   context=context, paragraphs="\n\n".join(text[i] for i in missing if i in text))
    raw = pipeline._cached_summarize(prompt, llm["backend"], api_key=llm["api_key"], model=llm["model"],
                                     api_base=llm.get("api_base", ""), cache_dir=cache_dir, max_tokens=4000, timeout=600)
    wanted = set(missing)
    return {i: g for i, g in parse_reply(raw, min(missing), max(missing)).items() if i in wanted}


ROLE_LABELS = ("主持人", "嘉宾", "未知发言人")

RESOLVE_PROMPT = """一期播客文字稿已经按段标了发言人，但有些段落只标了角色（{roles}），没写名字。
同一期里已经认出的人：{names}

节目：{show}
本期标题：{title}{hosts_line}

视频简介：
\"\"\"
{description}
\"\"\"

每个标签下面是标成这个标签的几段话：

{samples}

请判断每个角色标签实际是上面已认出的哪一个人（「未知发言人」常常是片头剪辑里某位嘉宾或主持人的话）。
确实对不上任何人就写「无」。每个角色一行，不要别的内容：
{format_lines}
"""


def resolve_roles(result: dict[int, dict], units: list[dict], show: str, title: str, ctx: dict,
                  hosts: str, llm: dict, cache_dir: str) -> dict[int, dict]:
    """有的段落只标了「主持人」「嘉宾」，同一期里却已经认出了名字（长节目分批时前几批还不知道是谁）：
    把各标签的几段话给模型看，问角色对应哪个已认出的人。答得上来才换，答不上来保持原样。"""
    def labels():
        for g in result.values():
            yield g["name"]
            for sw in g["switches"]:
                yield sw[0]
    used = set(labels())
    roles = [r for r in ROLE_LABELS if r in used]
    names = [n for n in dict.fromkeys(labels()) if n not in ROLE_LABELS]
    names += [h for h in pipeline.parse_host_names(hosts) if h not in names]
    if not roles or not names:
        return result
    samples = []
    for label in roles + names:
        said = [units[i - 1]["orig"][:220] for i, g in sorted(result.items()) if g["name"] == label][:4]
        if said:
            samples.append(f"【{label}】\n" + "\n".join(said))
    prompt = RESOLVE_PROMPT.format(roles="、".join(roles), names="、".join(names), show=show, title=title,
                                   hosts_line=pipeline.host_prompt_line(hosts),
                                   description=(ctx.get("description") or "")[:3000], samples="\n\n".join(samples),
                                   format_lines="\n".join(f"{r}: <名字或无>" for r in roles))
    raw = pipeline._cached_summarize(prompt, llm["backend"], api_key=llm["api_key"], model=llm["model"],
                                     api_base=llm.get("api_base", ""), cache_dir=cache_dir, max_tokens=300)
    mapping = {}
    for line in raw.splitlines():
        m = re.match(r"^\s*(主持人|嘉宾|未知发言人)\s*[:：]\s*(.+?)\s*$", line)
        if m and m.group(2) in names:
            mapping[m.group(1)] = m.group(2)
    if not mapping:
        return result
    for g in result.values():
        g["name"] = mapping.get(g["name"], g["name"])
        g["switches"] = [(mapping.get(w, w), a, b) for w, a, b in g["switches"]]
    return result


# ---------------------------------------------------------------------------
# 按模型的回答改写正文
# ---------------------------------------------------------------------------

def _cut(text: str, start_words: str, zh: bool) -> Optional[int]:
    """start_words 在 text 里的位置，只认句子开头（前面是句末标点；中文还认冒号）。找不到返回 None。"""
    probe = start_words.strip()
    if not probe:
        return None
    ends = ".?!…。？！”\"'）)" + ("：:" if zh else "")
    for m in re.finditer(re.escape(probe), text):
        before = text[:m.start()].rstrip()
        if before and before[-1] in ends:
            return m.start()
    return None


def apply_labels(units: list[dict], answer: dict[int, dict]) -> tuple[list[dict], dict]:
    """按回答把段落拆开、标上名字。返回 (新的段落列表, 统计)。"""
    stats = {"split": 0, "split_skipped": 0}
    pieces: list[dict] = []   # {"orig", "zh", "who"}
    last = ""
    for i, u in enumerate(units, 1):
        if u.get("heading"):
            pieces.append({"orig": u["orig"], "zh": u["zh"], "who": None})
            continue
        a = answer.get(i) or {"name": last or "未知发言人", "switches": []}
        orig, zh = strip_label(u["orig"]), (strip_label(u["zh"]) if u["zh"] else None)
        segs = [(a["name"], orig, zh)]
        for who, en_start, zh_start in a["switches"]:
            spk, o, z = segs[-1]
            k = _cut(o, en_start, zh=False)
            kz = _cut(z, zh_start, zh=True) if z is not None else None
            if k is None or k == 0 or (z is not None and (kz is None or kz == 0)):
                stats["split_skipped"] += 1
                continue
            segs[-1] = (spk, o[:k].rstrip(), z[:kz].rstrip() if z is not None else None)
            o2 = o[k:].lstrip()
            z2 = z[kz:].lstrip() if z is not None else None
            if z2 is not None and not z2.startswith(">"):
                z2 = "> " + z2
            segs.append((who, o2, z2))
            stats["split"] += 1
        for who, o, z in segs:
            pieces.append({"orig": o, "zh": z, "who": who})
            last = who
    out, prev = [], None
    for p in pieces:
        if p["who"] is None:          # 章节标题：原样放着，下一段重新标一次名字
            out.append({"orig": p["orig"], "zh": p["zh"]})
            prev = None
        elif p["who"] != prev:
            out.append({"orig": add_label(p["orig"], p["who"], zh=False),
                        "zh": add_label(p["zh"], p["who"], zh=True) if p["zh"] else None})
            prev = p["who"]
        else:
            out.append({"orig": p["orig"], "zh": p["zh"]})
    return out, stats


def render_body(units: list[dict]) -> str:
    blocks = []
    for u in units:
        blocks.append(u["orig"])
        if u["zh"]:
            blocks.append(u["zh"])
    return "\n\n" + "\n\n".join(blocks) + "\n\n"


def set_header(head: str) -> str:
    lines = head.split("\n")
    lines = [ln for ln in lines if not ln.startswith("- 发言人：")]
    at = next((i for i, ln in enumerate(lines) if ln.startswith(("- 说明：", "- 整理稿见"))), None)
    if at is None:
        # 插在时间那几行后面：新写法「- 发布时间：」「- 收录时间：」，上一版的「- 发表时间：」、老文件的「- 播出时间：」
        at = max((i + 1 for i, ln in enumerate(lines) if ln.startswith(("- 播出时间：", "- 发布时间：", "- 发表时间：", "- 收录时间："))),
                 default=1)
    lines.insert(at, AI_LABEL_LINE)
    return "\n".join(lines)


def already_labelled(content: str) -> bool:
    head = content.split(SPEECH_HEADING, 1)[0]
    return "AI 基于上下文推测标注" in head or "官方转写标注" in head


def label_file(speech_path: str, llm: dict, *, dry_run: bool = False, force: bool = False) -> dict:
    show_dir = os.path.dirname(os.path.dirname(os.path.abspath(speech_path)))
    with open(speech_path, encoding="utf-8") as f:
        content = f.read()
    if already_labelled(content) and not force:
        return {"skipped": "已经标过发言人"}
    head, body, tail = split_doc(content)
    units = units_of(body)
    if not units:
        raise LabelError("正文是空的")
    title = (re.match(r"^#\s+(.+)$", head, re.M) or [None, os.path.basename(speech_path)])[1]
    show = os.path.basename(show_dir)
    hosts = host_names_for(show_dir)
    ctx = episode_context(show_dir, speech_path, head)
    answer = ask_model(units, ctx, show, title, hosts, llm, pipeline.llm_cache_dir(show_dir))
    new_units, stats = apply_labels(units, answer)
    if _texts(new_units) != _texts(units):
        raise LabelError("核对没通过：去掉名字后跟原文对不上，没有改文件")
    new_content = set_header(head) + render_body(new_units) + tail.lstrip("\n")
    if not tail:
        new_content = new_content.rstrip("\n") + "\n"
    speakers: dict[str, int] = {}
    for u in new_units:
        m = _LABEL_RE.match(u["orig"])
        if m:
            speakers[m.group(2)] = speakers.get(m.group(2), 0) + 1
    result = {"paragraphs": len(units), "turns": sum(speakers.values()), "speakers": speakers,
              "caption_turns": len(ctx.get("turns") or []), "has_description": bool(ctx.get("description")),
              "guessed": sum(1 for g in answer.values() if g.get("guessed")),
              **stats}
    if dry_run:
        result["preview"] = new_content
        return result
    _backup(show_dir, "speaker_backup", speech_path)
    _write(speech_path, new_content)
    return result


def _write(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 文字记录、单集笔记：跟着整理稿补上
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9']+|[\u4e00-\u9fff]")
_TS_LINE_RE = re.compile(r"^\*\*\[\d+(?::\d+){1,2}\]\([^)]+\)\*\*\s*$")
MIN_TRANSCRIPT_MATCH = 0.15  # 文字记录里被锚点覆盖到的词少于这个比例（比如整理稿是译成中文的）就不标；自动字幕错字多，长节目常在 15%–30%，锚点仍然很密


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def speech_word_speakers(speech_content: str) -> tuple[list[str], list[str]]:
    """标好发言人的整理稿 → (原文的词, 每个词是谁说的)。"""
    _, body, _ = split_doc(speech_content)
    words, who, cur = [], [], "未知发言人"
    for u in units_of(body):
        m = _LABEL_RE.match(u["orig"])
        if m:
            cur = m.group(2)
        ws = _tokens(strip_label(u["orig"]))
        words += ws
        who += [cur] * len(ws)
    return words, who


ANCHOR_N = 4   # 用几个词连成的短语当锚点


def _anchors(flat: list[str], words: list[str]) -> list[tuple[int, int]]:
    """两边都只出现一次的 N 词短语当锚点，取前后顺序一致的最长一串（片头剪辑会把正文里的话提前说一遍，
    整体比对容易被这种重复带偏，只认唯一短语、再要求顺序一致就不会）。"""
    import bisect

    def grams(seq):
        pos: dict[tuple, list[int]] = {}
        for i in range(len(seq) - ANCHOR_N + 1):
            pos.setdefault(tuple(seq[i:i + ANCHOR_N]), []).append(i)
        return {g: p[0] for g, p in pos.items() if len(p) == 1}
    ga, gb = grams(flat), grams(words)
    pairs = sorted((i, gb[g]) for g, i in ga.items() if g in gb)
    # 按 j 求最长递增子序列
    tails: list[int] = []
    tail_idx: list[int] = []
    prev = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        t = bisect.bisect_left(tails, j)
        if t == len(tails):
            tails.append(j)
            tail_idx.append(k)
        else:
            tails[t] = j
            tail_idx[t] = k
        prev[k] = tail_idx[t - 1] if t else -1
    chain, k = [], (tail_idx[-1] if tail_idx else -1)
    while k >= 0:
        chain.append(pairs[k])
        k = prev[k]
    return chain[::-1]


def transcript_speakers(transcript_body: str, words: list[str], who: list[str]) -> tuple[list[Optional[str]], float]:
    """文字记录正文里每个时间戳段落的发言人，以及文字记录里被锚点覆盖到的词的比例。
    每个词按前后两个锚点之间的比例映射到整理稿里的位置，段落取映射到的位置上多数人的名字。"""
    import bisect
    lines = transcript_body.split("\n")
    paras: list[list[str]] = []
    for i, line in enumerate(lines[:-1]):
        if _TS_LINE_RE.match(line):
            paras.append(_tokens(lines[i + 1]))
    flat = [w for ws in paras for w in ws]
    if not flat or not words:
        return [None] * len(paras), 0.0
    anchors = _anchors(flat, words)
    if len(anchors) < 2:
        return [None] * len(paras), 0.0
    exact: dict[int, int] = {}   # 落在锚点短语里的词：在整理稿里的确切位置
    for i, j in anchors:
        for d in range(ANCHOR_N):
            exact.setdefault(i + d, j + d)
    ai = [i for i, _ in anchors]

    def mapped(x: int) -> int:
        k = bisect.bisect_right(ai, x) - 1
        if k < 0:
            (i0, j0), (i1, j1) = anchors[0], anchors[1]
        elif k >= len(anchors) - 1:
            (i0, j0), (i1, j1) = anchors[-2], anchors[-1]
        else:
            (i0, j0), (i1, j1) = anchors[k], anchors[k + 1]
        j = j0 + (x - i0) * (j1 - j0) / max(1, i1 - i0)
        return min(len(words) - 1, max(0, round(j)))

    out, x = [], 0
    for ws in paras:
        votes: dict[str, int] = {}
        for _ in ws:
            if x in exact:
                sp, weight = who[exact[x]], 5   # 对上原话的词说了算，按比例估的只在没对上时起作用
            else:
                sp, weight = who[mapped(x)], 1
            votes[sp] = votes.get(sp, 0) + weight
            x += 1
        out.append(max(votes.items(), key=lambda kv: kv[1])[0] if votes else (out[-1] if out else None))
    return out, len(exact) / len(flat)


def label_transcript(speech_path: str, *, dry_run: bool = False) -> dict:
    show_dir = os.path.dirname(os.path.dirname(os.path.abspath(speech_path)))
    tpath = os.path.join(show_dir, "transcripts", os.path.basename(speech_path))
    with open(speech_path, encoding="utf-8") as f:
        speech = f.read()
    if "AI 基于上下文推测标注" not in speech.split(SPEECH_HEADING, 1)[0]:
        return {"skipped": "整理稿还没标发言人"}
    try:
        with open(tpath, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {"skipped": "没有文字记录"}
    head, sep, body = content.partition("## 文字记录")
    if not sep:
        return {"skipped": "文字记录格式不认识"}
    if "### 🗣️ " in body:
        return {"skipped": "文字记录已经分了发言人"}
    words, who = speech_word_speakers(speech)
    speakers, ratio = transcript_speakers(body, words, who)
    reason = ("文字记录里没有时间戳段落" if not speakers
              else f"文字记录跟整理稿只对上 {ratio:.0%}，不标" if ratio < MIN_TRANSCRIPT_MATCH else "")
    if reason:
        # 分不了人，但开头那行「根据标题解析，单人演讲」是错的：改成指向整理稿
        fixed = re.sub(r"^- 发言人：.*（根据标题解析，单人演讲）$", TRANSCRIPT_SEE_SPEECH_LINE, head, count=1, flags=re.M)
        if fixed != head and not dry_run:
            _backup(show_dir, "speaker_backup_transcripts", tpath)
            _write(tpath, fixed + sep + body)
        return {"skipped": reason}
    lines = body.split("\n")
    insert_at: dict[int, str] = {}
    k, last = 0, None
    for i, line in enumerate(lines[:-1]):
        if _TS_LINE_RE.match(line):
            sp = speakers[k]
            k += 1
            if sp and sp != last:
                insert_at[i] = sp
                last = sp
    out = []
    for i, line in enumerate(lines):
        if i in insert_at:
            out += [f"### 🗣️ {insert_at[i]}", ""]
        out.append(line)
    new_body = "\n".join(out)
    check = re.sub(r"(?m)^### 🗣️ .+\n\n", "", new_body)
    if check != body:
        raise LabelError("文字记录核对没通过，没有改")
    new_content = set_header(head) + sep + new_body
    result = {"turns": len(insert_at), "match": round(ratio, 2)}
    if dry_run:
        result["preview"] = new_content
        return result
    _backup(show_dir, "speaker_backup_transcripts", tpath)
    _write(tpath, new_content)
    return result


def fix_note_header(speech_path: str, *, dry_run: bool = False) -> bool:
    """单集笔记开头写着「发言人：xx（根据标题解析，单人演讲）」的，整理稿标好之后改成 AI 推测那一行。"""
    show_dir = os.path.dirname(os.path.dirname(os.path.abspath(speech_path)))
    with open(speech_path, encoding="utf-8") as f:
        m = re.search(r"^- 单集笔记：\[[^\]]*\]\(<?\.\./(notes/[^>)]+)>?\)", f.read(4000), re.M)
    if not m:
        return False
    npath = os.path.join(show_dir, m.group(1))
    try:
        with open(npath, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    new = re.sub(r"^- 发言人：.*（根据标题解析，单人演讲）$", AI_LABEL_LINE, content, count=1, flags=re.M)
    if new == content:
        return False
    if not dry_run:
        _backup(show_dir, "speaker_backup_notes", npath)
        _write(npath, new)
    return True


def _backup(show_dir: str, kind: str, path: str) -> None:
    """备份只留最早那一份：重标一次不能拿已经改过的版本把原件盖掉。"""
    d = os.path.join(show_dir, ".cache", kind)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(path))
    if not os.path.exists(dst):
        shutil.copy2(path, dst)


def fix_roles(speech_path: str, llm: dict, *, dry_run: bool = False) -> dict:
    """已经标好的整理稿里还有 Host/未知/主持人 这类标签：先统一写法，再问一次各角色是谁（不重标整篇）。"""
    show_dir = os.path.dirname(os.path.dirname(os.path.abspath(speech_path)))
    with open(speech_path, encoding="utf-8") as f:
        content = f.read()
    head, body, tail = split_doc(content)
    units = units_of(body)
    answer: dict[int, dict] = {}
    cur = "未知发言人"
    for i, u in enumerate(units, 1):
        if u.get("heading"):
            continue
        m = _LABEL_RE.match(u["orig"])
        if m:
            cur = norm_name(m.group(2))
        answer[i] = {"name": cur, "switches": []}
    before = {g["name"] for g in answer.values()}
    if not before & set(ROLE_LABELS) and before == {norm_name(n) for n in before}:
        return {"skipped": "没有角色标签"}
    title = (re.match(r"^#\s+(.+)$", head, re.M) or [None, os.path.basename(speech_path)])[1]
    hosts = host_names_for(show_dir)
    ctx = {"description": "", "turns": []}
    m = _VIDEO_ID_RE.search(head)
    if m:
        try:
            with open(os.path.join(show_dir, ".cache", "subtitles", f"{m.group(1)}.meta.json"), encoding="utf-8") as f:
                ctx["description"] = (json.load(f) or {}).get("description") or ""
        except (OSError, ValueError):
            pass
    answer = resolve_roles(answer, units, os.path.basename(show_dir), title, ctx, hosts, llm,
                           pipeline.llm_cache_dir(show_dir))
    new_units, _ = apply_labels(units, answer)
    if _texts(new_units) != _texts(units):
        raise LabelError("核对没通过，没有改")
    new_content = head + render_body(new_units) + tail.lstrip("\n")
    if not tail:
        new_content = new_content.rstrip("\n") + "\n"
    after = {g["name"] for g in answer.values()}
    if new_content == content:
        return {"skipped": "问过了，角色还是对不上具体的人"}
    if not dry_run:
        _backup(show_dir, "speaker_backup", speech_path)
        _write(speech_path, new_content)
    return {"before": sorted(before & set(ROLE_LABELS) | {n for n in before if norm_name(n) != n}),
            "left": sorted(after & set(ROLE_LABELS))}


def labelled_files(show_dir: str) -> list[str]:
    speech_dir = os.path.join(show_dir, "speech")
    out = []
    for name in sorted(os.listdir(speech_dir)) if os.path.isdir(speech_dir) else []:
        p = os.path.join(speech_dir, name)
        if name.endswith(".md"):
            with open(p, encoding="utf-8") as f:
                if "AI 基于上下文推测标注" in f.read(4000).split(SPEECH_HEADING, 1)[0]:
                    out.append(p)
    return out


def run_followups(files: list[str], dry_run: bool = False) -> int:
    done = skipped = notes = failed = 0
    reasons: dict[str, int] = {}
    for path in files:
        try:
            r = label_transcript(path, dry_run=dry_run)
            if fix_note_header(path, dry_run=dry_run):
                notes += 1
        except (LabelError, OSError) as e:
            failed += 1
            print(f"  ❌ {os.path.basename(path)}：{e}", flush=True)
            continue
        if r.get("skipped"):
            skipped += 1
            key = re.sub(r"\d+%", "N%", r["skipped"])
            reasons[key] = reasons.get(key, 0) + 1
        else:
            done += 1
    print(f"文字记录分了发言人 {done} 篇，跳过 {skipped} 篇 {reasons}；笔记开头改了 {notes} 篇；失败 {failed} 篇", flush=True)
    return 1 if failed else 0


def show_files(show_dir: str) -> list[str]:
    out = []
    speech_dir = os.path.join(show_dir, "speech")
    for name in sorted(os.listdir(speech_dir)) if os.path.isdir(speech_dir) else []:
        p = os.path.join(speech_dir, name)
        if name.endswith(".md"):
            with open(p, encoding="utf-8") as f:
                if not already_labelled(f.read(4000)):
                    out.append(p)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="给已有整理稿补标发言人（不重新整理、不重新翻译）")
    p.add_argument("--file", action="append", default=[], help="整理稿路径，可以给多个")
    p.add_argument("--show", action="append", default=[], help="节目文件夹名或路径：这个节目里还没标的全部")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--backend", default="api")
    p.add_argument("--dry-run", action="store_true", help="只看结果，不写文件（预览写到 --preview-dir）")
    p.add_argument("--preview-dir", default="")
    p.add_argument("--force", action="store_true", help="已经标过的也重标")
    p.add_argument("--fix-roles", action="store_true",
                   help="已经标好的整理稿：把 Host/未知 这类写法统一，再问一次角色是谁（每篇一次小调用）")
    p.add_argument("--followups", action="store_true",
                   help="不问模型：给已经标好发言人的整理稿，把文字记录分上发言人、改掉笔记开头的错误发言人行")
    args = p.parse_args(argv)
    try:
        llm = llm_config.resolve({"backend": args.backend, "model": args.model}, default_backend=args.backend)
    except llm_config.ConfigError as e:
        print(f"⛔ {e}", file=sys.stderr)
        return 1
    files = list(args.file)
    for show in args.show:
        show_dir = show if os.path.isdir(show) else os.path.join(core_vault.spark_dir(), show)
        files += labelled_files(show_dir) if (args.followups or args.fix_roles) else show_files(show_dir)
    if args.followups:
        return run_followups(files, dry_run=args.dry_run)
    if args.fix_roles:
        # 只管这个工具标过的（有 speaker_backup 备份）：流水线当初标好的那些，文字记录里已经有它自己的发言人标题，
        # 这里改了名字两边就对不上了
        files = [f for f in files if os.path.exists(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(f))), ".cache", "speaker_backup", os.path.basename(f)))]
        for path in files:
            try:
                r = fix_roles(path, llm, dry_run=args.dry_run)
            except (LabelError, pipeline.SummarizeError, OSError) as e:
                print(f"  ❌ {os.path.basename(path)}：{e}", flush=True)
                continue
            print(f"  {os.path.basename(path)[:60]}：{r.get('skipped') or ('处理前 ' + '、'.join(r['before']) + '；还剩 ' + ('、'.join(r['left']) or '无'))}", flush=True)
        return 0
    if not files:
        print("没有要标的整理稿")
        return 0
    print(f"要标 {len(files)} 篇（{llm['backend']} / {llm['model']}）", flush=True)
    failed = 0
    for n, path in enumerate(files, 1):
        print(f"[{n}/{len(files)}] {os.path.basename(path)}", flush=True)
        try:
            r = label_file(path, llm, dry_run=args.dry_run, force=args.force)
        except (LabelError, pipeline.SummarizeError, OSError) as e:
            failed += 1
            print(f"  ❌ {e}", flush=True)
            continue
        if r.get("skipped"):
            print(f"  跳过：{r['skipped']}", flush=True)
            continue
        who = "、".join(f"{k}×{v}" for k, v in sorted(r["speakers"].items(), key=lambda kv: -kv[1]))
        print(f"  ✅ {r['paragraphs']} 段，{r['turns']} 轮发言，拆开 {r['split']} 段"
              f"（没找准没拆 {r['split_skipped']} 处）{f'，{r[chr(103)+"uessed"]} 段模型没答、沿用前一段' if r.get('guessed') else ''}；简介{'有' if r['has_description'] else '无'}，"
              f"字幕换人标记 {r['caption_turns']} 处；{who}", flush=True)
        if args.dry_run and args.preview_dir:
            os.makedirs(args.preview_dir, exist_ok=True)
            with open(os.path.join(args.preview_dir, os.path.basename(path)), "w", encoding="utf-8") as f:
                f.write(r["preview"])
    print(f"完成：{len(files) - failed} 篇，失败 {failed} 篇", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
