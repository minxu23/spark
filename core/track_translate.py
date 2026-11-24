"""「信息」文章（笔记库 Spark/信息跟进/<订阅>/x.md）的中文译文。

文章还是一个 Markdown 文件：「# 标题」、几行说明、「## 小结」（中文）、「## 原文」（抓来的正文，每段一行）。
外文的原文跟剪藏一样逐段翻译（core/clip_compose.translate）：每段原文后面空一行，跟一段「> 译文」，
仍写在「## 原文」一节里，不另起小节。哪些算外文、怎么分段、哪些不译、怎么认已经译过的，都跟剪藏是同一套规则，
只是正文在「## 原文」一节（到下一个「## 」或批注脚注为止），不是剪藏的「## 正文」。

- 抓取时自动翻译：apps/summit2md/tracking.process_item；
- 已有的文章单篇补译：start_fill（阅读页 /read/api/track/translate），只补没译的段落；
- 下游只要原文的（复盘挑的篇目、笔记洞察的检索和报告）用 original_only 去掉译文。
"""
from __future__ import annotations

import os
import re
import threading
from typing import Callable, Optional

from core import annotations, clip_compose, vault

TRACK_DIR = f"{vault.SPARK_DIRNAME}/信息跟进"   # 笔记库里的文件夹名，界面上叫「信息」
BRIEF_DIRNAME = "简报"
HEADING = "原文"
# 有正文可译的来源：RSS、公众号、网页文章、Substack。视频（YouTube，没有 source_type）走节目那套，有自己的对照整理稿
TEXT_SOURCE_TYPES = ("rss", "wechat", "article", "substack")
MAX_FILL = 50


def is_article_path(rel: str) -> bool:
    """rel 相对笔记库根目录：Spark/信息跟进/<订阅>/x.md（不含简报、不在子文件夹里、不带点开头的部分）。"""
    rel = str(rel or "").replace("\\", "/").strip("/")
    parts = rel.split("/")
    if len(parts) != 4 or "/".join(parts[:2]) != TRACK_DIR or not parts[3].endswith(".md"):
        return False
    if parts[2] == BRIEF_DIRNAME or any(not p or p.startswith(".") for p in parts):
        return False
    return parts[3] != parts[2] + ".md"


def wants_source(entry: dict) -> bool:
    return entry.get("source_type") in TEXT_SOURCE_TYPES


# ---------------------------------------------------------------- 「## 原文」一节

def original_section(text: str) -> str:
    return clip_compose.section_body(text, HEADING)


def replacing_original(text: str, body: str) -> str:
    return clip_compose.replacing_section(text, body, HEADING)


def needs_translation(body: str) -> bool:
    """同剪藏：去掉图片和链接地址后不少于 200 字、汉字占比低于 10%。"""
    return clip_compose.needs_translation(body)


def wants_translation(text: str) -> bool:
    """整篇文章：原文是外文、还没译或者没译完。"""
    return clip_compose.wants_translation(text, HEADING)


def untranslated(text: str) -> int:
    return clip_compose.untranslated(original_section(text))


def original_only(text: str) -> str:
    """去掉「## 原文」里的译文（带汉字的「> 」段），别的地方不动。没有译文的文章原样返回。"""
    span = clip_compose.section_span(text, HEADING)
    if not span:
        return text
    body = text[span[0]:span[1]]
    plain = clip_compose.without_translation(body)
    return text if plain == body else text[:span[0]] + plain + text[span[1]:]


def original_only_if_article(rel: str, text: str) -> str:
    return original_only(text) if is_article_path(rel) else text


def translate(body: str, ask, progress: Callable[[int, int], None] = lambda n, total: None,
              stop: Optional[Callable[[], bool]] = None) -> tuple[str, int]:
    """返回 (对照稿, 没译成的段数)。已经带译文的段落不再送（同剪藏补译）。"""
    return clip_compose.translate(body, ask, progress, stop)


# ---------------------------------------------------------------- 重新抓取同一篇：旧的译文、划线接到新正文上

def _plain(s: str) -> str:
    """比较段落用：去掉划线的「==」和批注号。"""
    return annotations.MARK_TEXT_RE.sub(r"\1", re.sub(r"\[\^note-\d+\]", "", s)).strip()


def carry_paragraphs(old_text: str, paragraphs: list[str]) -> str:
    """重新抓取写同一篇时的「## 原文」：新抓的段落跟旧文件里的某段原文一样（不计划线和批注号）时，
    用旧文件里那段（带着划线和批注号）和它后面的译文；别的段落用新抓的，没有译文。
    这样没变的段落不用再译，原文、译文上的划线原处留着。"""
    old: dict[str, dict] = {}
    if old_text:
        for p in clip_compose._pieces(original_section(old_text)):
            if not p["plain"]:
                old.setdefault(_plain(p["original"]), p)
    out = []
    for para in paragraphs:
        p = old.get(_plain(para))
        if p is None:
            out.append(para)
            continue
        block = p["original"]
        if p.get("translated"):
            block += f"\n\n> {p['marker']}{p['translated']}"
        out.append(block)
    return "\n\n".join(out)


# ---------------------------------------------------------------- 已有的文章单篇补译（仿 clip_capture 的补译）

def _read(full: str) -> str:
    with open(full, encoding="utf-8") as f:
        return f.read()


def fill_one(full: str, ask, progress: Callable[[int, int], None] = lambda n, total: None) -> str:
    """给一篇补译文：没译的整篇译，译了一部分的只补没译的段落，已经译完的不调模型。
    模型跑的时候文件可能被改（比如划了线），写之前重读；原文一节变了的话译文不写，免得盖掉新改的。
    返回这一篇的提示（没有就空）。"""
    from core import atomic
    text = _read(full)
    body = original_section(text)
    if not wants_translation(text):
        # 已经译完的什么都不说；中文的、太短的说一句为什么没译
        return "" if needs_translation(clip_compose.without_translation(body)) else "不是外文文章（或者太短），不用翻译"
    translated, missed = translate(body, ask, progress)
    latest = _read(full)
    if original_section(latest) != body:
        return "翻译时原文被改过，译文没写进去，再补一次"
    if translated != body:
        atomic.write_text(full, replacing_original(latest, translated))
    return f"有 {missed} 段没译成" if missed else ""


BUSY_LOCAL = "电脑上正在跑别的任务，本机模型一次只能跑一件事，等它跑完再译"


def _backend() -> str:
    """补译用的后端（跟 clip_capture._ask 同一套设置）；没配好时空串。"""
    from core import llm_config
    try:
        return llm_config.resolve({}, default_backend="api")["backend"]
    except llm_config.ConfigError:
        return ""


def start_fill(raw, busy: Optional[Callable[[], int]] = None) -> dict:
    """{paths: [Spark/信息跟进/<订阅>/x.md]}。跟剪藏的补摘要、补翻译共用一个任务位（一次只做一篇，本机模型不并发），
    进度用 clip_capture.status 查。用的模型跟剪藏、单集小结一样，取设置里的。
    busy：电脑上在跑的模型任务数（spark.py 的 _job_count，/api/activity 同一套）。后端是本机 Ollama 且有任务在跑时不开始；
    云端后端不拦。"""
    from core import clip_capture, remote_tasks
    paths = raw.get("paths") if isinstance(raw, dict) else None
    if not isinstance(paths, list) or not paths or len(paths) > MAX_FILL:
        raise remote_tasks.TaskError("要翻译的信息文章不对")
    if not all(isinstance(p, str) and is_article_path(p) for p in paths):
        raise remote_tasks.TaskError("只能翻译信息里的文章")
    root = vault.vault_root()
    if not all(os.path.isfile(os.path.join(root, *p.split("/"))) for p in paths):
        raise remote_tasks.TaskError("找不到这篇文章")
    ask = clip_capture._ask()
    if ask is None:
        raise remote_tasks.TaskError("没配好模型，去设置里填好再翻译")
    if _backend() == "ollama" and busy and busy():
        raise remote_tasks.TaskError(BUSY_LOCAL)
    job_id = clip_capture._new_job("准备中", kind="track", done_count=0, total=len(paths))
    threading.Thread(target=_fill_run, args=(job_id, paths, root, ask), daemon=True).start()
    return clip_capture.status(job_id)


def _fill_run(job_id: str, paths: list[str], root: str, ask) -> None:
    from core import clip_capture, github_sync
    notes = []
    for n, rel in enumerate(paths, 1):
        label = f"第 {n}/{len(paths)} 篇：" if len(paths) > 1 else ""
        clip_capture._update(job_id, path=rel, done_count=n - 1, phase=f"{label}准备翻译")

        def progress(i, total, label=label):
            clip_capture._update(job_id, phase=f"{label}正在翻译（{i}/{total}）")
        try:
            note = fill_one(os.path.join(root, *rel.split("/")), ask, progress)
        except Exception as e:  # noqa: BLE001  一篇不成不影响后面的
            note = str(e)
        if note:
            notes.append(note if len(paths) == 1 else f"{os.path.basename(rel)[:-3]}：{note}")
    try:
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        notes.append(f"同步没触发：{e}")
    clip_capture._update(job_id, done=True, done_count=len(paths), phase="翻译好了", warning="；".join(notes))
