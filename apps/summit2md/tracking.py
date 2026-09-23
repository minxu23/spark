"""「信息跟进」：多个订阅源的新内容 → 逐条小结笔记 → 一份跨订阅的本批简报。

跟会议/播客那条 process_job 流程的区别：
- 检查新内容只做轻量的列表请求（RSS 一个请求、sitemap 只列链接），不抓正文；
- 每条内容一篇笔记（小结在上、清洗过的原文在下），放在订阅自己的文件夹里；
- 没有"累计总结"：每次运行只针对这一批勾选的内容出一份简报，按主题归并，
  放在 信息跟进/简报/ 下，每个要点都链接回对应的单条笔记。

已处理过哪些条目仍然记在每个订阅文件夹的 .manifest.json 里（跟 process_job
同一种格式），这样检查新内容、续跑、失败重试都只看这一份记录。
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from core import sources

from . import pipeline
from . import subscriptions_store as store

ProgressCB = Callable[[dict], None]

YOUTUBE_LANG_PREFS = ["en", "zh-Hans", "zh-Hant", "zh"]
BRIEF_ITEM_BODY_CHARS = 600

TRACK_ITEM_PROMPT = """你是资讯编辑。下面是一篇资讯/博客文章或一期节目的正文（来自网页抓取或语音转写，可能夹杂少量噪音）。只根据正文归纳，不要补充正文里没有的信息。

来源：{source_name}
标题：{title}

请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

TLDR: <一句话核心结论，不超过40字，不要markdown>
{length_instruction}

正文：
\"\"\"
{text}
\"\"\"
"""

BRIEF_PROMPT = """你是资讯主编，要写一份信息简报。以下是本批从 {source_count} 个信息源收集到的 {count} 条新内容，每条带编号、来源、标题和小结。

用中文写，要求：
- 最前面是 `## 要点速览`，3-5 条，每条一句话；
- 然后按主题归并（不是按来源罗列）：不同来源说的是同一件事就合在一起写。每个主题一个 `### 小标题`，下面用 2-4 句话说清发生了什么、为什么值得关注；
- 引用具体内容时在句末用方括号标编号，比如 [3] 或 [2][7]，只能用下面列出的编号；
- 每条内容只归进最相关的一个主题，实在归不进去的放到最后的 `### 其它`；
- 不要编造列表里没有的信息，不要写开场白和结束语。

本批内容：
{items}
"""


# ---------------------------------------------------------------------------
# 检查新内容
# ---------------------------------------------------------------------------

def list_entries(sub: dict) -> dict:
    """按订阅记下的来源类型直接走对应的抓取方式——不用每次都把 Substack→RSS→
    sitemap 整条探测链重走一遍，sitemap 源也只列链接不抓正文。"""
    url = sub["url"]
    source_type = sub.get("source_type")
    if source_type == "rss" and not sources.is_apple_podcast_url(url):
        return sources.fetch_rss_playlist(url)
    if source_type == "article":
        return sources.fetch_sitemap_playlist(url, fetch_bodies=False)
    return pipeline.fetch_playlist(url)


def _manifest_entries(folder: str) -> dict:
    return pipeline._load_manifest(folder)["entries"]


def find_new(sub: dict, entries: list[dict]) -> list[dict]:
    """还没成功处理过、也没被忽略的条目。处理失败过的仍算新内容（下次可以重试），
    会带上上一次的失败原因。"""
    done = _manifest_entries(sub["folder"])
    ignored = set(sub.get("ignored_ids") or [])
    new = []
    for e in entries:
        eid = e.get("id")
        if not eid or eid in ignored:
            continue
        rec = done.get(eid)
        if rec and rec.get("ok"):
            continue
        item = dict(e)
        if rec and rec.get("error"):
            item["last_error"] = rec["error"]
        new.append(item)
    new.sort(key=lambda e: e.get("publish_date") or "", reverse=True)
    return new


def check(sub: dict) -> dict:
    """探测一条订阅有没有新内容——只调免费的列表请求，不碰模型。失败不抛出去。"""
    try:
        entries = list_entries(sub).get("entries") or []
        new = find_new(sub, entries)
    except Exception as e:  # noqa: BLE001
        return {"id": sub["id"], "error": str(e), "new_count": 0, "new_entries": [], "total": 0}
    store.touch_checked(sub["id"])
    return {
        "id": sub["id"],
        "error": None,
        "new_count": len(new),
        "new_entries": [
            {"id": e["id"], "title": e.get("title"), "publish_date": e.get("publish_date"),
             "url": e.get("url"), "last_error": e.get("last_error")}
            for e in new
        ],
        "total": len(entries),
    }


# ---------------------------------------------------------------------------
# 单条处理
# ---------------------------------------------------------------------------

def _fetch_paragraphs(entry: dict, cache_dir: str) -> Optional[list[str]]:
    st = entry.get("source_type")
    if st in ("rss", "wechat", "article"):
        got = sources.fetch_source_text(entry, cache_dir)
        return [t for _, t in got["paragraphs"]] if got else None
    if st == "substack":
        got = pipeline.fetch_substack_transcript(entry, cache_dir)
        return [t for _, t in got["paragraphs"]] if got else None
    got = pipeline.download_subtitle(entry["id"], cache_dir, YOUTUBE_LANG_PREFS)
    if not got:
        return None
    if got.get("upload_date"):
        entry["publish_date"] = got["upload_date"]
    return [t for _, t in pipeline.vtt_to_paragraphs(got["path"])]


def _note_filename(entry: dict) -> str:
    date = entry.get("publish_date") or ""
    title = pipeline.sanitize_filename(entry.get("title") or "untitled", maxlen=100)
    return f"{date}_{title}.md" if date else f"{title}.md"


def _unique_relpath(folder: str, fname: str, taken: set[str]) -> str:
    stem, ext = os.path.splitext(fname)
    candidate, n = fname, 2
    while candidate in taken or os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{stem}_{n}{ext}"
        n += 1
    return candidate


def render_note(entry: dict, source_name: str, summary: dict, paragraphs: list[str]) -> str:
    lines = [f"# {entry.get('title') or 'Untitled'}", ""]
    lines.append(f"- 来源：{source_name}")
    if entry.get("publish_date"):
        lines.append(f"- 发布日期：{pipeline.format_publish_date(entry['publish_date'])}")
    if entry.get("url"):
        lines.append(f"- 原文链接：{entry['url']}")
    lines += ["", "## 小结", ""]
    if summary.get("tldr"):
        lines += [f"**{summary['tldr']}**", ""]
    lines += [summary.get("body") or "", "", "## 原文", ""]
    lines += [p + "\n" for p in paragraphs]
    return "\n".join(lines).rstrip() + "\n"


class _Stop(Exception):
    pass


def process_item(sub: dict, entry: dict, *, llm: dict, summary_length: str, max_chars: int,
                 stop_flag: Optional[Callable[[], bool]] = None) -> dict:
    """处理一条：抓正文 → 小结 → 写笔记 → 记进 manifest。返回
    {ok, error, relative_path, summary, title}。"""
    folder = sub["folder"]
    os.makedirs(folder, exist_ok=True)
    cache_dir = os.path.join(folder, ".cache", "sources")
    manifest = pipeline._load_manifest(folder)
    records = manifest["entries"]
    eid = entry["id"]

    def record(ok: bool, error: Optional[str], rel: Optional[str], summary: Optional[dict]) -> dict:
        records[eid] = {
            "rank": max([r.get("rank", 0) for r in records.values()], default=0) + 1
            if eid not in records else records[eid].get("rank", 0),
            "entry": {
                "id": eid, "title": entry.get("title"), "url": entry.get("url"),
                "duration": 0, "is_raw_session": False,
                "source_type": entry.get("source_type"), "publish_date": entry.get("publish_date"),
            },
            "ok": ok, "error": error, "relative_path": rel, "speech_relative_path": None,
            "summary": summary, "truncated": False,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        pipeline._save_manifest(folder, manifest)
        return {"ok": ok, "error": error, "relative_path": rel, "summary": summary,
                "title": entry.get("title")}

    try:
        paragraphs = _fetch_paragraphs(entry, cache_dir)
    except Exception as e:  # noqa: BLE001
        return record(False, f"抓取正文失败：{e}", None, None)
    if not paragraphs:
        return record(False, "没有抓到正文（可能是付费墙、需要登录，或者只有简介）", None, None)

    text = "\n".join(paragraphs)
    length_instruction = pipeline.PER_TOPIC_LENGTH_INSTRUCTIONS.get(
        summary_length, pipeline.PER_TOPIC_LENGTH_INSTRUCTIONS["medium"])
    prompt = TRACK_ITEM_PROMPT.format(
        source_name=sub.get("name") or "",
        title=entry.get("title") or "",
        length_instruction=length_instruction,
        text=pipeline._cap_transcript(text, max_chars),
    )
    try:
        raw = pipeline._cached_summarize(
            prompt, llm["backend"], api_key=llm["api_key"], model=llm["model"], api_base=llm["api_base"],
            cache_dir=pipeline.llm_cache_dir(folder), stop_flag=stop_flag,
        )
    except pipeline.Stopped:
        raise
    except pipeline.SummarizeError as e:
        return record(False, f"小结生成失败：{e}", None, None)
    summary = pipeline.parse_topic_summary(raw)

    existing = records.get(eid) or {}
    rel = existing.get("relative_path")
    if not rel:
        taken = {r.get("relative_path") for r in records.values() if r.get("relative_path")}
        rel = _unique_relpath(folder, _note_filename(entry), taken)
    with open(os.path.join(folder, rel), "w", encoding="utf-8") as f:
        f.write(render_note(entry, sub.get("name") or "", summary, paragraphs))
    return record(True, None, rel, summary)


# ---------------------------------------------------------------------------
# 本批简报
# ---------------------------------------------------------------------------

_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _md_link(target: str) -> str:
    # 路径里经常有空格（订阅名、文章标题），用尖括号包起来 Obsidian 和 CommonMark 都认
    return f"<{target}>"


def render_brief(items: list[dict], brief_body: str, brief_dir: str, generated_at: str) -> str:
    """items: [{n, title, source_name, publish_date, note_path(绝对路径)}]"""
    by_n = {it["n"]: it for it in items}

    def cite(m: re.Match) -> str:
        it = by_n.get(int(m.group(1)))
        if not it:
            return m.group(0)
        rel = os.path.relpath(it["note_path"], brief_dir)
        return f"[\\[{it['n']}\\]]({_md_link(rel)})"

    source_count = len({it["source_name"] for it in items})
    lines = [f"# 信息简报 · {generated_at}", "",
             f"本批 {len(items)} 条新内容，来自 {source_count} 个信息源。", ""]
    lines.append(_CITE_RE.sub(cite, brief_body.strip()))
    lines += ["", "## 本批内容", ""]
    for it in items:
        rel = os.path.relpath(it["note_path"], brief_dir)
        date = pipeline.format_publish_date(it["publish_date"]) if it.get("publish_date") else ""
        meta = " · ".join(x for x in (it["source_name"], date) if x)
        lines.append(f"{it['n']}. [{it['title']}]({_md_link(rel)}) — {meta}")
    return "\n".join(lines).rstrip() + "\n"


def _brief_items_text(items: list[dict]) -> str:
    blocks = []
    for it in items:
        body = (it["summary"].get("body") or "")[:BRIEF_ITEM_BODY_CHARS]
        blocks.append(
            f"[{it['n']}] 来源：{it['source_name']}｜标题：{it['title']}\n"
            f"小结：{it['summary'].get('tldr') or ''}\n{body}"
        )
    return "\n\n".join(blocks)


def _brief_path(brief_dir: str, stamp: str) -> str:
    path = os.path.join(brief_dir, f"{stamp} 信息简报.md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(brief_dir, f"{stamp} 信息简报 ({n}).md")
        n += 1
    return path


def run_batch(
    selections: list[tuple[dict, list[str]]],
    *,
    output_dir: str,
    llm: dict,
    summary_length: str = "medium",
    max_chars: int = pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS,
    brief_model: str = "",
    stop_flag: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[ProgressCB] = None,
) -> dict:
    """selections: [(订阅, [要处理的条目 id])]。逐条处理，最后出一份本批简报
    （brief_model 非空时简报单独用这个模型）。"""
    def report(**kw):
        if progress_cb:
            progress_cb(kw)

    def stopped() -> bool:
        return bool(stop_flag and stop_flag())

    total = sum(len(ids) for _, ids in selections)
    done_items: list[dict] = []
    failed: list[dict] = []
    i = 0
    was_stopped = False

    try:
        for sub, ids in selections:
            if stopped():
                raise _Stop
            name = sub.get("name") or ""
            report(log=f"获取「{name}」的条目列表……", stage="list", current=i, total=total)
            try:
                by_id = {e["id"]: e for e in (list_entries(sub).get("entries") or []) if e.get("id")}
            except Exception as e:  # noqa: BLE001
                for eid in ids:
                    failed.append({"sub_name": name, "id": eid, "title": eid, "error": f"获取列表失败：{e}"})
                i += len(ids)
                report(log=f"  ⚠️ 获取列表失败：{e}", current=i, total=total)
                continue
            for eid in ids:
                if stopped():
                    raise _Stop
                i += 1
                entry = by_id.get(eid)
                if not entry:
                    failed.append({"sub_name": name, "id": eid, "title": eid, "error": "这一条已经不在源的列表里了"})
                    report(log=f"[{i}/{total}] ⚠️ 已不在「{name}」的列表里，跳过", current=i, total=total)
                    continue
                report(log=f"[{i}/{total}] {name}：{entry.get('title')}", stage="item", current=i, total=total)
                res = process_item(sub, entry, llm=llm, summary_length=summary_length,
                                   max_chars=max_chars, stop_flag=stop_flag)
                if res["ok"]:
                    done_items.append({
                        "title": res["title"] or entry.get("title") or "", "source_name": name,
                        "publish_date": entry.get("publish_date"), "summary": res["summary"],
                        "note_path": os.path.join(sub["folder"], res["relative_path"]),
                    })
                else:
                    failed.append({"sub_name": name, "id": eid, "title": entry.get("title"), "error": res["error"]})
                    report(log=f"  ⚠️ {res['error']}")
    except (_Stop, pipeline.Stopped):
        was_stopped = True
        report(log="收到停止指令，已停止；已处理完的条目都已保存，没处理的下次检查还会出现")

    result = {"processed": len(done_items), "failed": failed, "stopped": was_stopped,
              "brief_path": None, "brief_markdown": None, "brief_error": None}
    if not done_items:
        return result
    if was_stopped:
        report(log="本次没有生成简报（中途停止）；已处理的条目已经保存")
        return result

    for n, it in enumerate(done_items, start=1):
        it["n"] = n
    brief_dir = os.path.join(output_dir, store.TRACK_DIRNAME, store.BRIEF_DIRNAME)
    os.makedirs(brief_dir, exist_ok=True)
    report(log=f"正在生成本批简报（{len(done_items)} 条）……", stage="brief", current=total, total=total)
    prompt = BRIEF_PROMPT.format(
        source_count=len({it["source_name"] for it in done_items}),
        count=len(done_items),
        items=_brief_items_text(done_items),
    )
    try:
        body = pipeline._cached_summarize(
            prompt, llm["backend"], api_key=llm["api_key"], model=brief_model or llm["model"], api_base=llm["api_base"],
            max_tokens=8000, timeout=600, cache_dir=pipeline.llm_cache_dir(brief_dir), stop_flag=stop_flag,
        )
    except pipeline.Stopped:
        result["stopped"] = True
        report(log="收到停止指令，简报没有生成；已处理的条目已经保存")
        return result
    except pipeline.SummarizeError as e:
        result["brief_error"] = str(e)
        body = f"_（简报生成失败：{e}。下面仍列出本批已处理的内容。）_"
        report(log=f"  ⚠️ 简报生成失败：{e}")

    now = time.localtime()
    content = render_brief(done_items, body, brief_dir, time.strftime("%Y-%m-%d %H:%M", now))
    path = _brief_path(brief_dir, time.strftime("%Y-%m-%d %H.%M", now))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    result["brief_path"] = path
    result["brief_markdown"] = content
    report(log=f"简报已保存：{path}", stage="done", current=total, total=total)
    return result
