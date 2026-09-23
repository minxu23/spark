"""
从剪贴板粘贴的一段文字（或者单个链接）里批量导入外部内容当笔记：RSS/Atom
订阅源、Apple Podcast、微信公众号文章、普通网页文章。跟 uploads.py 是同一个
套路——落进一个临时目录，当作这次任务专属的"笔记库根"传给 pipeline.run()，
不进真正的笔记库，返回的笔记字典形状跟 vault.scan()/uploads.save_batch() 一致。

实际的"怎么把这几种链接抓下来"全部复用 core/sources.py——summit2md 那边已经
把这套逻辑跑通、测试过了，这里只负责"抓到的内容怎么落成一篇笔记"，不重新
实现一遍抓取。

RSS/Atom 订阅源、Apple Podcast 背后是一整份列表，会展开成多篇笔记（这跟
summit2md"剪贴板批量提取链接"故意不展开列表不一样——那边是给"专题"用的，
链接之间要保持独立；这里是"导入笔记"，谁的内容多让谁多导几篇是好事）。
微信公众号文章、普通网页文章本来就是一条，落一篇笔记。暂不支持 YouTube——
那边需要 yt-dlp 加一整套字幕下载逻辑，用 summit2md 处理更合适。
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

from core.certs import ensure_ca_env

# notes2insight 原来完全不碰网络，没人调用过这个——现在这个文件是它第一处真正
# 发起 HTTPS 请求的地方，必须显式调一次（这台机器的系统 Python 缺 CA 证书，
# 不然的话请求会直接 CERTIFICATE_VERIFY_FAILED）。跟 summit2md/pipeline.py 顶部
# 是同一处调用，多次调用是安全的（内部用 setdefault）。
ensure_ca_env()

from core import sources  # noqa: E402

# 一次请求最多处理这么多条链接（提取阶段），和 uploads.py 的 MAX_FILES_PER_BATCH
# 是同一个考虑：不让一次请求本身处理太久。
MAX_LINKS_PER_BATCH = 40

# 一份订阅源/播客节目可能有几十上百期——不加限制的话，粘贴一个链接就会触发
# 几十次网络请求，一次请求跑很久，体验很差。只展开最新的这么多条，多出来的
# 部分在结果里说明一声，而不是悄悄丢掉。
MAX_ENTRIES_PER_FEED = 20

RETENTION_SECONDS = 7 * 24 * 3600


def _safe_stem(title: str) -> str:
    """跟 uploads.py 的 _safe_stem 同一个考虑：中文标题占大多数，不能用
    werkzeug.secure_filename 那种会把非 ASCII 整段砍掉的消毒方式。"""
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", title or "").strip(" .")
    return stem[:120] or "未命名笔记"


def _unique_path(dest_dir: str, stem: str) -> tuple[str, str]:
    name = f"{stem}.md"
    n = 2
    while os.path.exists(os.path.join(dest_dir, name)):
        name = f"{stem} ({n}).md"
        n += 1
    return name, os.path.splitext(name)[0]


def _format_publish_date(d: Optional[str]) -> str:
    if not d or len(d) != 8 or not d.isdigit():
        return ""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _entry_to_note(dest_dir: str, entry: dict, cache_dir: str, source_label: str) -> Optional[dict]:
    """把 sources.py 抓到的一条 entry 落成一篇 .md 笔记；抓不到正文返回 None。"""
    fetched = sources.fetch_source_text(entry, cache_dir)
    if not fetched or not fetched.get("paragraphs"):
        return None
    body_text = "\n\n".join(t for _, t in fetched["paragraphs"])

    title = entry.get("title") or "Untitled"
    stem = _safe_stem(title)
    fname, note_title = _unique_path(dest_dir, stem)

    date_str = _format_publish_date(entry.get("publish_date"))
    date_line = f"- 发布时间：{date_str}\n" if date_str else ""
    header = (
        f"# {note_title}\n\n"
        f"- 来源：{source_label}「{entry.get('url', '')}」（不在笔记库里，只用于这次生成）\n"
        f"{date_line}\n"
    )
    body = header + body_text.strip() + "\n"

    full = os.path.join(dest_dir, fname)
    with open(full, "w", encoding="utf-8") as f:
        f.write(body)

    st = os.stat(full)
    return {
        "path": fname, "folder": ".", "name": fname, "title": note_title, "date": date_str,
        "bytes": st.st_size, "chars": len(body), "mtime": int(st.st_mtime),
    }


def _fetch_entries_for_url(url: str) -> tuple[list[dict], str, Optional[str]]:
    """返回 (entries, 来源说明, 跳过原因)。entries 非空时跳过原因为 None，反之亦然。
    RSS/Apple Podcast 这类"一份链接背后是一整份列表"的来源，entries 可能不止一条。
    """
    url = url if "://" in url else f"https://{url}"
    if "youtube.com" in url or "youtu.be" in url:
        return [], "", "暂不支持导入 YouTube 视频，可以用 summit2md 处理成文字记录后再拖进来"
    if sources.is_wechat_article_url(url):
        try:
            playlist = sources.fetch_wechat_article_playlist(url)
        except Exception as e:  # noqa: BLE001
            return [], "", f"解析失败：{e}"
        return playlist["entries"], "微信公众号文章", None
    if sources.is_apple_podcast_url(url):
        try:
            playlist = sources.fetch_apple_podcast_playlist(url)
        except Exception as e:  # noqa: BLE001
            return [], "", f"解析失败：{e}"
        return playlist["entries"], f"Apple Podcast「{playlist['summit_title']}」", None
    if sources.is_rss_url(url):
        try:
            playlist = sources.fetch_rss_playlist(url)
        except Exception as e:  # noqa: BLE001
            return [], "", f"解析失败：{e}"
        return playlist["entries"], f"RSS 订阅源「{playlist['summit_title']}」", None
    try:
        entry = sources.fetch_generic_article_entry(url)
    except Exception as e:  # noqa: BLE001
        return [], "", f"解析失败：{e}"
    return [entry], "网页文章", None


def extract_import_urls(text: str) -> list[str]:
    """从一段自由文本里挑出要导入的链接；一条都没有或太多就直接报错（不用等抓取）。"""
    urls = sources.extract_urls(text)
    if not urls:
        raise RuntimeError("没有在这段文字里找到任何链接")
    if len(urls) > MAX_LINKS_PER_BATCH:
        raise RuntimeError(f"一次最多处理 {MAX_LINKS_PER_BATCH} 条链接，这次提取到了 {len(urls)} 条")
    return urls


def import_from_text(dest_dir: str, text: str, progress=None) -> tuple[list[dict], list[dict]]:
    """从一段自由文本（或者就是一个链接）里批量提取链接，逐条抓取、转成笔记。
    返回 (notes, errors)，形状跟 uploads.save_batch() 一致，前端可以复用同一套
    渲染/勾选逻辑。errors 里既有真的失败，也有"订阅源太大只导入了前 N 条"这种
    说明性的条目（不是失败，只是截断，用同一个展示位置说明白）。
    """
    return import_urls(dest_dir, extract_import_urls(text), progress)


def import_urls(dest_dir: str, urls: list[str], progress=None) -> tuple[list[dict], list[dict]]:
    """逐条抓取链接转成笔记。progress(stage, 当前, 总数, 说明) 每开始一条调一次。"""
    os.makedirs(dest_dir, exist_ok=True)
    cache_dir = os.path.join(dest_dir, ".cache")
    notes: list[dict] = []
    errors: list[dict] = []

    for n, url in enumerate(urls, start=1):
        if progress:
            progress("import", n - 1, len(urls), f"抓取 {n}/{len(urls)}：{url}")
        entries, source_label, reason = _fetch_entries_for_url(url)
        if reason:
            errors.append({"name": url, "error": reason})
            continue
        truncated = len(entries) > MAX_ENTRIES_PER_FEED
        for entry in entries[:MAX_ENTRIES_PER_FEED]:
            note = _entry_to_note(dest_dir, entry, cache_dir, source_label)
            if note:
                notes.append(note)
            else:
                errors.append({
                    "name": entry.get("title") or url,
                    "error": "没能获取到正文内容（可能是付费墙、需要登录，或者只有简介没有全文）",
                })
        if truncated:
            errors.append({
                "name": url,
                "error": f"{source_label}共 {len(entries)} 条，为避免一次导入太多，只取了最新 {MAX_ENTRIES_PER_FEED} 条",
            })

    return notes, errors


def prune_old_batches(root: str) -> None:
    import shutil
    now = time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) > RETENTION_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue
