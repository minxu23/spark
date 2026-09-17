"""
notes2insight 核心流水线：勾选的笔记 → 技术洞察报告。

三个阶段：
  1. 摘取（map）   每篇笔记 → 一张结构化"技术摘要卡"，超长笔记先分块再合卡；结果按内容哈希缓存
  2. 归纳（reduce）所有摘要卡 → 报告骨架（主题簇 / 全局判断 / 分歧 / 时间线）；卡太多时分批预归并
  3. 成文（compose）按骨架逐章生成正文，再拼上 frontmatter、目录和来源索引

所有对模型的调用都走 llm.complete()，后端可切换（claude CLI / Anthropic API / 第三方 / Ollama）。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Callable, Optional

import llm

from core import digest as _digest
import vault

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DIGEST_CACHE_DIR = os.path.join(APP_DIR, ".cache", "digests")

PROMPT_VERSION = "v1"

# 单次送进模型的笔记正文上限（字符）。中文约 1 字 ≈ 1 token，留足输出与指令空间。
CHUNK_CHARS = 28000
# 归纳阶段单批摘要卡的字符上限，超过就先分批预归并
FRAMEWORK_BATCH_CHARS = 90000
# 成文阶段每章能携带的摘要卡字符上限
COMPOSE_CONTEXT_CHARS = 70000

MAX_NOTES = 400  # 一次任务的笔记数量硬上限，防止误勾整库

DEPTH_PRESETS = {
    "brief":    {"label": "简报", "clusters": 3, "words": "约 5000-8000 字",  "chapter_words": "600-900 字"},
    "standard": {"label": "标准", "clusters": 6, "words": "约 1.2-1.8 万字",  "chapter_words": "1200-1800 字"},
    "deep":     {"label": "深度", "clusters": 9, "words": "约 3 万字以上",     "chapter_words": "2500-4000 字"},
}


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

_COMMON_RULES = (
    "写作纪律：\n"
    "- 只使用给定材料中真实出现的内容，不要补充材料之外的知识，不要编造数字、日期或人名。\n"
    "- 公司、产品、芯片、模型、论文、人名等专有名词保留英文原名。\n"
    "- 材料中的数字要保留口径（单位、时间范围、谁说的）。\n"
    "- 材料本身可能来自语音转写或 AI 整理，含少量错误；遇到明显矛盾时指出矛盾，不要强行调和。\n"
    "- 中文正文使用中文全角标点（，。：；""），英文术语、数字与单位保持半角。\n"
    "- 直接输出 Markdown 正文，不要用 ```markdown 代码块把整段内容包起来。\n"
)

DIGEST_PROMPT = """你是一名技术情报分析师。下面是知识库中的一篇笔记（编号 [{idx}]），可能是播客整理稿、文章、论文或会议记录。

请把它压缩成一张"可供跨笔记综合分析"的技术摘要卡。{focus_clause}

{rules}
输出格式（严格使用下列小标题；某一节没有内容就写"无"）：

## 一句话主旨
## 核心技术论点
- 3-7 条，每条一句话，说清"主张是什么 + 依据是什么"
## 关键事实与数据
- 具体数字、规格、价格、比例、时间；注明是谁说的
## 时间锚点
- 事件 / 发布 / 里程碑 + 日期；笔记未注明日期就写"未注明"
## 涉及主体
- 公司 / 产品 / 技术名词 / 人物，逗号分隔
## 判断与预测
- 材料中对未来的判断，标明出自谁
## 分歧与不确定
- 材料内部的争议、前提限制、反对意见
## 可引用原话
- 最多 3 条短句，加引号

笔记标题：{title}
笔记路径：{path}
笔记日期：{date}
{chunk_note}
--- 笔记正文开始 ---
{body}
--- 笔记正文结束 ---
"""

MERGE_CHUNK_PROMPT = """下面是同一篇长笔记被切成 {n} 段后各自生成的摘要卡。请合并成一张完整摘要卡。

要求：去重、按重要性排序、保留所有具体数字与日期、不要新增材料之外的内容。
输出格式与输入保持一致（同样的小标题）。

笔记标题：{title}

{parts}
"""

FRAMEWORK_PROMPT = """你是一名技术趋势分析师。下面是 {n} 篇笔记的技术摘要卡，每篇有编号 [N]。

请据此设计一份**技术洞察报告**的骨架。{focus_clause}

{rules}
- 主题簇要按"技术议题"切分，不要按笔记来源或时间顺序切分。
- 每个判断都要能追溯到具体编号。

输出格式（严格遵守，编号格式不要改动）：

## 报告标题
<一行，具体、有信息量，不要用"综述""汇总"这类空词做主语>

## 副标题
<一行，点出这批材料真正的共同问题>

## 主题簇
### T1. <主题名>
- 概括：<一句话>
- 相关笔记：[1][3][7]
- 核心张力：<这个主题里真正没有定论或正在变化的是什么>
### T2. ...
（共 {clusters} 个左右，按重要性排序）

## 全局判断
- J1. <一句话判断> —— 依据 [2][5]
- （6-10 条，每条必须带依据编号）

## 主要分歧
- D1. <A 方主张> vs <B 方主张> —— [4] vs [9]
- （3-8 条）

## 时间线
- <日期>｜<事件>｜[编号]
- （按时间排序，只列材料中有明确日期的）

--- 摘要卡开始 ---
{cards}
--- 摘要卡结束 ---
"""

PREMERGE_PROMPT = """下面是一批技术摘要卡（编号 [N]）。请压缩成一份"批次要点"，保留：核心论点、关键数字与日期、判断、分歧，并在每条后面保留来源编号。
不要丢失编号，不要新增材料之外的内容。控制在 2500 字以内。

{cards}
"""

COMPOSE_HEADER_PROMPT = """你正在撰写一份技术洞察报告的**开篇部分**。报告骨架与材料如下。

请输出两节：

## 执行摘要
用 400-700 字说清楚：这批材料共同回答了什么问题、最重要的三到五个变化是什么、读者应该带走什么。不要罗列笔记标题。

## 关键判断
把骨架中的"全局判断"改写成 6-10 条，每条格式：
**判断 N：<一句话结论>**
<2-4 句展开：机制是什么、证据是什么、什么情况下这条判断会失效>（依据 [编号]）

{rules}
{focus_clause}

--- 报告骨架 ---
{framework}

--- 摘要卡 ---
{cards}
"""

COMPOSE_CHAPTER_PROMPT = """你正在撰写一份技术洞察报告的**第 {n} 章**，主题是：{topic}

本章篇幅 {chapter_words}。结构自拟，但必须包含：
1. 这个主题当前的事实状态（谁在做什么，到了哪一步，有哪些具体数字）
2. 技术机制层面的解释（为什么会这样，约束在哪里）
3. 材料之间的相互印证或冲突
4. 这个主题的"核心张力"——尚未有定论的是什么

写作要求：
- 段落式论述，不要通篇要点罗列；需要对比时可以用 Markdown 表格。
- 每个具体主张后面用 [编号] 标注来源，一段里出现多个来源就并列标注。
- 不要重复执行摘要已经说过的话，这一章要给出更细的机制与证据。
- 只输出本章内容，以 `## 第 {n} 章 {topic}` 开头，不要写其他章节。

{rules}
{focus_clause}

--- 报告骨架（供定位，不要整段复述）---
{framework}

--- 本章相关摘要卡 ---
{cards}
"""

COMPOSE_TAIL_PROMPT = """你正在撰写一份技术洞察报告的**收尾部分**。请输出三节：

## 分歧与开放问题
把骨架中的分歧展开成 4-8 组，每组格式：
**分歧 N：<争论点>**
- 一方：<主张与依据> [编号]
- 另一方：<主张与依据> [编号]
- 目前无法判定的原因：<一句话>

## 信号与展望
- 先给出未来 6-24 个月的 3 个情景（基线 / 加速 / 受阻），每个情景 100-200 字，说明触发条件。
- 再给出 8-12 个**可观测信号**，每条写清"看什么指标、出现什么读数意味着哪个情景在兑现"。

## 启示
面向一位关注这些技术的从业者，给出 5-8 条可执行的启示，每条一句结论加一句理由。不要写成空泛建议。

{rules}
{focus_clause}

--- 报告骨架 ---
{framework}

--- 摘要卡 ---
{cards}
"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class RunConfig:
    vault_root: str
    notes: list[str]                      # 相对路径列表（勾选结果）
    focus: str = ""                       # 用户关注点，可空
    depth: str = "standard"
    backend: str = "cli"
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    concurrency: int = 3
    timeout: int = 900
    output_dir: str = ""
    use_cache: bool = True
    topic: str = ""                       # 主题模式下的主题；手动勾选时为空
    retrieval: Optional[dict] = None      # 主题检索的溯源信息，写进"方法"一节
    model_digest: str = ""                # 摘取阶段用的模型；空则用 model
    model_compose: str = ""               # 归纳与成文阶段用的模型；空则用 model
    max_note_chars: int = 0               # 单篇笔记最多读多少字符，0 = 不限


@dataclass
class NoteRef:
    idx: int
    path: str
    title: str
    date: str
    chars: int
    digest: str = ""
    error: str = ""
    truncated: bool = False


@dataclass
class Cluster:
    no: int
    topic: str
    summary: str = ""
    tension: str = ""
    note_ids: list[int] = field(default_factory=list)


ProgressFn = Callable[[str, int, int, str], None]


def _noop(stage: str, cur: int, total: int, msg: str) -> None:
    pass


# --------------------------------------------------------------------------
# 阶段一：摘取
# --------------------------------------------------------------------------

def _strip_noise(text: str) -> str:
    return _digest.strip_noise(text)


def _chunks(text: str, size: int) -> list[str]:
    return _digest.chunks(text, size)


# 关注点短于这个长度就原样用在摘取阶段；更长的说明是"报告规格"，没必要每篇笔记都喂一遍
FOCUS_INLINE_LIMIT = 150


def digest_focus(cfg: RunConfig) -> str:
    """摘取阶段的定向语。

    关注点常被写成一整段报告要求（章节、结论口径、行动建议……）。那是给成文阶段的，
    逐篇摘取时重复几十遍既费 token，也会把摘要卡的措辞带偏向"报告结构"。
    这里只取能定向的那一小段：主题模式用主题，否则用关注点的第一句。
    """
    focus = (cfg.focus or "").strip()
    topic = (cfg.topic or "").strip()
    if topic:
        return topic
    if len(focus) <= FOCUS_INLINE_LIMIT:
        return focus
    first = re.split(r"[。！？!?\n]", focus)[0].strip() or focus
    if len(first) > FOCUS_INLINE_LIMIT:
        return first[:FOCUS_INLINE_LIMIT].rstrip() + "…"
    return first


def _focus_clause(focus: str) -> str:
    focus = (focus or "").strip()
    if not focus:
        return ""
    return f"\n**本次分析的关注点：{focus}**\n与关注点相关的内容要保留细节；无关内容压缩成一两句带过，但不要完全丢弃。\n"


def _cache_key(cfg: RunConfig, title: str, date: str, text: str) -> str:
    """按内容寻址：键里只放真正决定这张卡的东西——提示词版本、后端与摘取模型、
    关注点，以及笔记正文本身的哈希（外加同样进了提示词的标题和日期）。

    刻意不放的两样：

    * **路径**。放了的话，移动文件或给文件夹改名会让整批缓存作废，而内容一个字
      都没变——2026-09-17 把库里的 会议/ 改名成 Spark/ 就一次性废掉了那 835 篇的
      缓存。路径仍然会进提示词（给模型一点来源上下文），但不进键：这跟 idx 的
      处理一致，它也在提示词里、也不在键里，因为输出格式里没有它，卡片内容是由
      正文决定的。
    * **mtime**。同步工具、Finder 操作、Obsidian 插件碰一下文件都会改它，于是
      内容没变也要重新摘取一遍。正文哈希本来就能准确回答"内容变没变"这个问题，
      mtime 只是它的一个不可靠代理。

    代价是：两篇正文、标题、日期完全相同但路径不同的笔记会共用一张卡。对内容
    派生的产物来说这是对的行为，真要区分的是内容，不是它躺在哪个目录。
    """
    return _digest.cache_key(PROMPT_VERSION, cfg.backend, model_for(cfg, "digest") or "-",
                             digest_focus(cfg), title, date, _digest.content_hash(text))


def _cache_get(key: str) -> Optional[str]:
    return _digest.cache_get(key, DIGEST_CACHE_DIR)


def _cache_put(key: str, value: str) -> None:
    _digest.cache_put(key, value, DIGEST_CACHE_DIR)


def model_for(cfg: RunConfig, stage: str) -> str:
    """摘取阶段调用量最大但活儿简单，成文阶段次数少却最吃质量，允许分开配。"""
    if stage == "digest":
        return cfg.model_digest or cfg.model
    return cfg.model_compose or cfg.model


def _call(cfg: RunConfig, prompt: str, *, max_tokens: int, timeout: int,
          stage: str = "compose") -> str:
    return llm.complete(prompt, cfg.backend, api_key=cfg.api_key, model=model_for(cfg, stage),
                        api_base=cfg.api_base, max_tokens=max_tokens, timeout=timeout)


def digest_note(cfg: RunConfig, ref: NoteRef) -> str:
    """生成单篇笔记的摘要卡，命中缓存则直接返回。"""
    text = _strip_noise(vault.read_note(cfg.vault_root, ref.path))
    if not text:
        raise PipelineError("笔记为空")
    if cfg.max_note_chars and len(text) > cfg.max_note_chars:
        # 长逐字稿按字符截断，省掉后面每 28000 字一次的分块调用
        text = text[:cfg.max_note_chars]
        ref.truncated = True
    key = _cache_key(cfg, ref.title, ref.date or "", text)
    if cfg.use_cache:
        hit = _cache_get(key)
        if hit:
            return hit

    parts = _chunks(text, CHUNK_CHARS)
    digests: list[str] = []
    for i, part in enumerate(parts, 1):
        chunk_note = "" if len(parts) == 1 else f"（这是全文的第 {i}/{len(parts)} 段，只针对本段做卡片）\n"
        prompt = DIGEST_PROMPT.format(
            idx=ref.idx, title=ref.title, path=ref.path, date=ref.date or "未注明",
            focus_clause=_focus_clause(digest_focus(cfg)), rules=_COMMON_RULES,
            chunk_note=chunk_note, body=part,
        )
        digests.append(_call(cfg, prompt, max_tokens=4000, timeout=cfg.timeout, stage="digest"))

    if len(digests) == 1:
        result = digests[0]
    else:
        merged_parts = "\n\n".join(f"--- 第 {i} 段的卡片 ---\n{d}" for i, d in enumerate(digests, 1))
        result = _call(
            cfg,
            MERGE_CHUNK_PROMPT.format(n=len(digests), title=ref.title, parts=merged_parts),
            max_tokens=4000, timeout=cfg.timeout, stage="digest",
        )

    _cache_put(key, result)
    return result


def digest_all(cfg: RunConfig, refs: list[NoteRef], progress: ProgressFn) -> None:
    """并发生成所有摘要卡；单篇失败只记录错误，不中断整个任务。"""
    total = len(refs)
    done = 0
    lock = __import__("threading").Lock()

    def work(ref: NoteRef) -> None:
        nonlocal done
        try:
            ref.digest = digest_note(cfg, ref)
        except Exception as e:  # 单篇失败不应让整份报告失败
            ref.error = str(e)[:300]
        with lock:
            done += 1
            tail = "（失败）" if ref.error else ""
            progress("digest", done, total, f"摘取 {done}/{total}：{ref.title[:40]}{tail}")

    workers = max(1, min(cfg.concurrency, 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, refs))


# --------------------------------------------------------------------------
# 阶段二：归纳
# --------------------------------------------------------------------------

def _cards_text(refs: list[NoteRef], ids: Optional[set[int]] = None, budget: int = COMPOSE_CONTEXT_CHARS) -> str:
    """拼接摘要卡文本，超出预算时按顺序截断并注明。"""
    out: list[str] = []
    used = 0
    for r in refs:
        if not r.digest:
            continue
        if ids is not None and r.idx not in ids:
            continue
        block = f"\n===== [{r.idx}] {r.title}｜{r.date or '未注明日期'}｜{r.path} =====\n{r.digest.strip()}\n"
        if used + len(block) > budget:
            picked = len(out)
            total = len([x for x in refs if x.digest and (ids is None or x.idx in ids)])
            out.append(f"\n（余下摘要卡因长度限制未展开，共省略 {total - picked} 张）\n")
            break
        out.append(block)
        used += len(block)
    return "".join(out)


def build_framework(cfg: RunConfig, refs: list[NoteRef], progress: ProgressFn) -> str:
    preset = DEPTH_PRESETS.get(cfg.depth, DEFAULT_DEPTH)
    ok = [r for r in refs if r.digest]
    if not ok:
        raise PipelineError("没有任何笔记成功生成摘要卡，无法归纳")

    all_cards = _cards_text(ok, budget=10 ** 9)
    if len(all_cards) > FRAMEWORK_BATCH_CHARS:
        # 卡太多：先分批压成"批次要点"，再统一归纳
        batches: list[list[NoteRef]] = []
        cur: list[NoteRef] = []
        cur_len = 0
        for r in ok:
            blk = len(r.digest) + 120
            if cur and cur_len + blk > FRAMEWORK_BATCH_CHARS:
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(r)
            cur_len += blk
        if cur:
            batches.append(cur)

        progress("framework", 0, len(batches) + 1, f"摘要卡较多，先分 {len(batches)} 批预归并")
        summaries = []
        for i, batch in enumerate(batches, 1):
            text = _cards_text(batch, budget=FRAMEWORK_BATCH_CHARS)
            summaries.append(_call(cfg, PREMERGE_PROMPT.format(cards=text),
                                   max_tokens=6000, timeout=cfg.timeout, stage="digest"))
            progress("framework", i, len(batches) + 1, f"预归并 {i}/{len(batches)}")
        cards = "\n\n".join(f"--- 第 {i} 批要点 ---\n{s}" for i, s in enumerate(summaries, 1))
    else:
        cards = all_cards
        progress("framework", 0, 1, "归纳报告骨架")

    framework = _call(
        cfg,
        FRAMEWORK_PROMPT.format(
            n=len(ok), clusters=preset["clusters"], cards=cards,
            focus_clause=_focus_clause(cfg.focus), rules=_COMMON_RULES,
        ),
        max_tokens=8000, timeout=cfg.timeout,
    )
    progress("framework", 1, 1, "骨架完成")
    return framework


DEFAULT_DEPTH = DEPTH_PRESETS["standard"]

_RE_SECTION_TITLE = re.compile(r"^##\s*报告标题\s*\n+(.+?)\s*$", re.M)
_RE_SECTION_SUB = re.compile(r"^##\s*副标题\s*\n+(.+?)\s*$", re.M)
_RE_CLUSTER = re.compile(r"^###\s*T(\d+)[.、．]?\s*(.+?)\s*$", re.M)
_RE_IDS = re.compile(r"\[(\d+)\]")


def parse_framework(framework: str) -> tuple[str, str, list[Cluster]]:
    title = ""
    m = _RE_SECTION_TITLE.search(framework)
    if m:
        title = m.group(1).strip().lstrip("#").strip()
    subtitle = ""
    m = _RE_SECTION_SUB.search(framework)
    if m:
        subtitle = m.group(1).strip()

    clusters: list[Cluster] = []
    matches = list(_RE_CLUSTER.finditer(framework))
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(framework)
        body = framework[body_start:body_end]
        # 只取下一节标题之前的内容，避免把"全局判断"整节吃进来
        cut = re.search(r"^##\s+", body, re.M)
        if cut:
            body = body[:cut.start()]
        summary = ""
        sm = re.search(r"概括\s*[:：]\s*(.+)", body)
        if sm:
            summary = sm.group(1).strip()
        tension = ""
        tm = re.search(r"核心张力\s*[:：]\s*(.+)", body)
        if tm:
            tension = tm.group(1).strip()
        ids_line = re.search(r"相关笔记\s*[:：]\s*(.+)", body)
        ids = [int(x) for x in _RE_IDS.findall(ids_line.group(1))] if ids_line else []
        clusters.append(Cluster(no=int(m.group(1)), topic=m.group(2).strip(),
                                summary=summary, tension=tension, note_ids=ids))
    return title, subtitle, clusters


# --------------------------------------------------------------------------
# 阶段三：成文
# --------------------------------------------------------------------------

def compose(cfg: RunConfig, refs: list[NoteRef], framework: str,
            clusters: list[Cluster], progress: ProgressFn) -> list[str]:
    preset = DEPTH_PRESETS.get(cfg.depth, DEFAULT_DEPTH)
    ok = [r for r in refs if r.digest]
    total_steps = 2 + len(clusters)
    step = 0
    sections: list[str] = []

    progress("compose", step, total_steps, "撰写执行摘要与关键判断")
    sections.append(_call(
        cfg,
        COMPOSE_HEADER_PROMPT.format(
            framework=framework, cards=_cards_text(ok),
            rules=_COMMON_RULES, focus_clause=_focus_clause(cfg.focus),
        ),
        max_tokens=8000, timeout=cfg.timeout,
    ))
    step += 1

    for i, cl in enumerate(clusters, 1):
        progress("compose", step, total_steps, f"撰写第 {i} 章：{cl.topic[:30]}")
        ids = set(cl.note_ids)
        # 骨架没给编号（或编号解析失败）时退回全部卡片，保证章节仍有素材
        cards = _cards_text(ok, ids if ids else None)
        if not cards.strip():
            cards = _cards_text(ok)
        sections.append(_call(
            cfg,
            COMPOSE_CHAPTER_PROMPT.format(
                n=i, topic=cl.topic, chapter_words=preset["chapter_words"],
                framework=framework, cards=cards,
                rules=_COMMON_RULES, focus_clause=_focus_clause(cfg.focus),
            ),
            max_tokens=12000, timeout=cfg.timeout,
        ))
        step += 1

    progress("compose", step, total_steps, "撰写分歧、展望与启示")
    sections.append(_call(
        cfg,
        COMPOSE_TAIL_PROMPT.format(
            framework=framework, cards=_cards_text(ok),
            rules=_COMMON_RULES, focus_clause=_focus_clause(cfg.focus),
        ),
        max_tokens=10000, timeout=cfg.timeout,
    ))
    return sections


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

_RE_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n\s*```\s*$", re.S)


def _clean_section(md: str) -> str:
    """去掉模型偶尔加的外层代码围栏，并把该节标题层级归一到 ## 起。"""
    md = md.strip()
    m = _RE_FENCE.match(md)
    if m:
        md = m.group(1).strip()

    levels = [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s+\S", md, re.M)]
    if not levels:
        return md
    shift = 2 - min(levels)
    if shift == 0:
        return md

    def bump(m: re.Match) -> str:
        lvl = max(1, min(6, len(m.group(1)) + shift))
        return "#" * lvl + " "

    return re.sub(r"^(#{1,6})\s+", bump, md, flags=re.M)


def _slug(text: str, limit: int = 28) -> str:
    text = re.sub(r"[\s/\\:：，,。.、|｜]+", "_", text.strip())
    text = re.sub(r"_{2,}", "_", text).strip("_")
    return text[:limit] or "技术洞察报告"


def _date_range(refs: list[NoteRef]) -> str:
    ds = sorted(d for d in (r.date for r in refs) if d)
    if not ds:
        return "未注明"
    return ds[0] if ds[0] == ds[-1] else f"{ds[0]} → {ds[-1]}"


def _source_index(refs: list[NoteRef]) -> str:
    rows = ["| 编号 | 笔记 | 日期 | 位置 | 状态 |", "|---|---|---|---|---|"]
    for r in refs:
        stem = os.path.splitext(os.path.basename(r.path))[0]
        link = f"[[{stem}]]"
        status = "✅" if r.digest else f"⚠️ {r.error[:40] or '未生成'}"
        title = r.title.replace("|", "\\|")[:60]
        # 标题与文件名一致时只留双链，避免同一行写两遍
        cell = link if title.strip() == stem.strip() else f"{link} {title}"
        folder = (os.path.dirname(r.path) or ".").replace("|", "\\|")
        rows.append(f"| [{r.idx}] | {cell} | {r.date or '—'} | {folder} | {status} |")
    return "\n".join(rows)


def _retrieval_note(cfg: RunConfig) -> str:
    """"方法"一节开头那句：说明这批笔记是怎么选出来的。"""
    r = cfg.retrieval or {}
    if not r:
        return "从知识库中手动勾选笔记 → "
    return (f"按主题「{cfg.topic.strip()}」检索：扩展 {len(r.get('terms', []))} 个检索词，"
            f"全文扫描 {r.get('scanned', 0)} 篇、命中 {r.get('matched', 0)} 篇，"
            f"取打分最高的 {r.get('candidates_count', 0)} 篇做相关度判定，选入 {r.get('picked', 0)} 篇 → ")


def _retrieval_detail(cfg: RunConfig) -> str:
    r = cfg.retrieval or {}
    if not r:
        return ""
    terms = r.get("terms", [])
    lines = ["**检索词**（权重越高越核心）：", ""]
    for w in (3, 2, 1):
        got = [t["term"] for t in terms if t.get("weight") == w]
        if got:
            lines.append(f"- 权重 {w}：{'、'.join(got)}")
    dropped = r.get("dropped") or []
    if dropped:
        lines += ["", "**打分靠前但被判定为不相关、未纳入的笔记**：", ""]
        lines += [f"- {d['title']}（相关度 {d['relevance']}：{d.get('reason', '')}）" for d in dropped[:15]]
    return "\n".join(lines) + "\n"


def assemble(cfg: RunConfig, refs: list[NoteRef], framework: str, title: str,
             subtitle: str, clusters: list[Cluster], sections: list[str],
             elapsed: float) -> tuple[str, str]:
    preset = DEPTH_PRESETS.get(cfg.depth, DEFAULT_DEPTH)
    ok = [r for r in refs if r.digest]
    failed = [r for r in refs if not r.digest]
    today = _date.today().isoformat()
    title = title or f"技术洞察报告：{cfg.topic.strip() or cfg.focus.strip() or '多笔记综合分析'}"

    backend_label = {"cli": "claude CLI", "api": "Anthropic API",
                     "openai_compatible": "第三方 API", "ollama": "Ollama"}.get(cfg.backend, cfg.backend)
    md, mc = model_for(cfg, "digest"), model_for(cfg, "compose")
    if md == mc:
        model_label = f"（{md}）" if md else ""
    else:
        model_label = f"（摘取 {md or '默认'} / 成文 {mc or '默认'}）"
    truncated = [r for r in refs if r.truncated]

    toc = ["| 章 | 主题 | 核心张力 |", "|---|---|---|"]
    for i, cl in enumerate(clusters, 1):
        toc.append(f"| 第 {i} 章 | {cl.topic.replace('|', '\\|')} | {(cl.tension or cl.summary or '—').replace('|', '\\|')} |")

    front = [
        "---",
        f"title: {title}",
        f"date: {today}",
        f"range: {_date_range(refs)}",
        f"sources: {len(ok)} 篇笔记" + (f"（{len(failed)} 篇未成功）" if failed else ""),
        f"focus: {cfg.focus.strip() or '（未指定）'}",
        f"depth: {preset['label']}",
    ]
    if cfg.topic.strip():
        front.append(f"topic: {cfg.topic.strip()}")
    front += [
        "tags: [技术洞察, notes2insight]",
        "---",
        "",
        f"# {title}",
    ]
    if subtitle:
        front.append(f"## {subtitle}")
    front += [
        "",
        f"> 本报告由 notes2insight 基于知识库中 **{len(ok)} 篇**勾选笔记生成，"
        f"覆盖 {_date_range(refs)}，模型后端 {backend_label}{model_label}，"
        f"生成耗时 {int(elapsed // 60)} 分 {int(elapsed % 60)} 秒。"
        f"正文中的 `[N]` 对应文末来源索引；结论均来自笔记材料本身，未引入外部知识。",
        "",
        "## 阅读地图",
        "",
        "\n".join(toc),
        "",
        "---",
        "",
    ]

    body = "\n\n---\n\n".join(_clean_section(s) for s in sections)

    tail = [
        "",
        "---",
        "",
        "## 方法、来源与局限",
        "",
        f"**方法**：{_retrieval_note(cfg)}逐篇笔记生成结构化技术摘要卡 → 跨卡归纳主题簇与全局判断 → 按骨架分章成文。"
        f"共 {len(refs)} 篇输入，{len(ok)} 篇成功，{len(failed)} 篇失败。"
        f"深度档位：{preset['label']}（目标篇幅 {preset['words']}）。"
        + (f"摘取阶段用 {md or '默认模型'}，归纳与成文用 {mc or '默认模型'}。" if md != mc else "")
        + (f"摘取阶段按「{digest_focus(cfg)}」定向，完整的关注点要求在归纳与成文阶段生效。"
           if digest_focus(cfg) != cfg.focus.strip() and cfg.focus.strip() else ""),
        "",
        "**局限**：",
        "- 结论只反映所选笔记的覆盖面，不代表领域全貌；样本偏向知识库自身的采集偏好。",
    ] + ([
        "- 主题模式下的笔记是关键词检索加模型判定选出来的：换一批关键词可能选出不同材料，"
        "词面上没命中但实质相关的笔记会被漏掉。附录里列出了实际用到的检索词，可据此判断召回是否够全。",
    ] if cfg.retrieval else []) + [
        "- 播客与视频类笔记来自语音转写与 AI 整理，数字与人名可能有误，引用前请回溯原始材料。",
        "- 摘要卡是有损压缩，细节以原笔记为准；正文 `[N]` 标注用于回溯，不等于逐句引用。",
    ] + ([
        f"- **有 {len(truncated)} 篇笔记只读了前 {cfg.max_note_chars // 10000} 万字**"
        f"（设置了单篇长度上限以控制调用量）：" + "、".join(r.title[:30] for r in truncated[:8])
        + ("等" if len(truncated) > 8 else "") + "。这些笔记后半部分的内容没有进入分析。",
    ] if truncated else []) + [
        "",
        "### 来源索引",
        "",
        _source_index(refs),
        "",
        "<details>",
        "<summary>报告骨架与检索过程（中间结果，供核对）</summary>",
        "",
        _retrieval_detail(cfg),
        "",
        "```markdown",
        framework.strip().replace("```", "``\u200b`"),
        "```",
        "",
        "</details>",
        "",
    ]

    content = "\n".join(front) + body + "\n" + "\n".join(tail)
    fname = f"技术洞察报告_{_slug(cfg.focus or title)}_{today}.md"
    return content, fname


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------

def run(cfg: RunConfig, progress: ProgressFn = _noop) -> dict:
    started = time.time()
    if not cfg.notes:
        raise PipelineError("没有勾选任何笔记")
    if len(cfg.notes) > MAX_NOTES:
        raise PipelineError(f"一次最多处理 {MAX_NOTES} 篇笔记，当前勾选 {len(cfg.notes)} 篇")

    index = {n["path"]: n for n in vault.scan(cfg.vault_root)}
    refs: list[NoteRef] = []
    for i, rel in enumerate(cfg.notes, 1):
        meta = index.get(rel)
        if not meta:
            raise PipelineError(f"笔记不在库中：{rel}")
        refs.append(NoteRef(idx=i, path=rel, title=meta["title"], date=meta["date"], chars=meta["chars"]))

    progress("digest", 0, len(refs), f"开始摘取 {len(refs)} 篇笔记")
    digest_all(cfg, refs, progress)
    ok = [r for r in refs if r.digest]
    if not ok:
        raise PipelineError("所有笔记的摘要卡都生成失败，请检查模型后端配置")

    framework = build_framework(cfg, refs, progress)
    title, subtitle, clusters = parse_framework(framework)
    if not clusters:
        # 骨架格式没解析出来时退回单章模式，至少还能出一份报告
        clusters = [Cluster(no=1, topic=(cfg.focus.strip() or "综合分析"), note_ids=[])]

    sections = compose(cfg, refs, framework, clusters, progress)
    elapsed = time.time() - started
    content, fname = assemble(cfg, refs, framework, title, subtitle, clusters, sections, elapsed)

    out_dir = cfg.output_dir or os.path.join(APP_DIR, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, fname)
    if os.path.exists(out_path):
        stem, ext = os.path.splitext(fname)
        for i in range(2, 100):
            cand = os.path.join(out_dir, f"{stem}_{i}{ext}")
            if not os.path.exists(cand):
                out_path = cand
                break
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    progress("done", 1, 1, f"报告已生成：{os.path.basename(out_path)}")
    return {
        "path": out_path,
        "filename": os.path.basename(out_path),
        "title": title,
        "content": content,
        "chars": len(content),
        "elapsed": elapsed,
        "ok_count": len(ok),
        "failed": [{"path": r.path, "error": r.error} for r in refs if not r.digest],
        "clusters": [{"no": c.no, "topic": c.topic, "notes": c.note_ids} for c in clusters],
    }
