"""
summit2md 核心处理逻辑：
1. 解析 YouTube 播放列表，拿到所有议题的视频链接
2. 用 yt-dlp 拉取自动字幕，清洗成整洁的文字记录（对谈记录）
3. 用 LLM（本地 claude CLI 或 Anthropic API）给每个议题写小结，并汇总成大会总结

不依赖网络之外的外部服务；字幕/转写全部本地处理。
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

# 仓库根目录进 import 路径，好让 `python3 -m apps.summit2md` 和 pytest 都能 import core
_SPARK_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SPARK_ROOT not in sys.path:
    sys.path.insert(0, _SPARK_ROOT)

from core.certs import ensure_ca_env  # noqa: E402

# 这台机器的系统 Python 缺 CA 证书，yt-dlp 的 https 请求会直接 CERTIFICATE_VERIFY_FAILED。
# 必须在 import yt_dlp 之前设好。这是进程级的设置，合并进程后 notes2insight 也会受到
# 影响——影响是良性的（同一份 certifi CA 包），但放在 core/certs.py 里显式调用，
# 而不是留成一个藏在 import 里的副作用。
ensure_ca_env()

import yt_dlp  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from core import atomic  # noqa: E402
from core import sources  # noqa: E402


# --------------------------------------------------------------------------
# Spark 共享层：LLM 后端与 API Key 查找都由 core/ 提供，summit2md 和 notes2insight
# 用同一份实现。下面几行把仓库根目录放进 import 路径，好让 `python3 server.py` 和
# pytest 都能直接 import core，不需要额外的安装步骤。
# --------------------------------------------------------------------------

from core import digest as _digest  # noqa: E402
from core import keys as _keys  # noqa: E402
from core.keys import read_key_file  # noqa: E402,F401  （server.py 走 pipeline.read_key_file）

# 界面上提示"把 key 放在这里"时用：已经有 key 文件就指向它所在的目录，
# 否则指向新位置 ~/.spark/keys
KEYS_DIR = _keys.display_dir()


# --------------------------------------------------------------------------
# 播放列表解析
# --------------------------------------------------------------------------

RAW_SESSION_TITLE_RE = re.compile(
    r".*(Stage|Track|Room).{0,20}(Morning|Afternoon|Full)?\s*Session\s*$", re.IGNORECASE
)
RAW_SESSION_DURATION_THRESHOLD = 3 * 3600  # 超过3小时的多半是整场直播录像，不是单个议题


def is_raw_session(title: str, duration: float) -> bool:
    if duration and duration > RAW_SESSION_DURATION_THRESHOLD:
        return True
    if title and RAW_SESSION_TITLE_RE.match(title.strip()):
        return True
    return False


MULTI_SPEAKER_TITLE_RE = re.compile(
    r"^(Panel|Fireside Chat|Workshop|Startup Spotlight|Q&A|Roundtable)\s*[:\-]", re.IGNORECASE
)
_SINGLE_SPEAKER_NAME_RE = re.compile(r"^([A-Z][^\-–:]{1,60}?)\s*[-–:]\s+\S")


def guess_single_speaker(title: str) -> Optional[str]:
    """
    很多议题标题本身就是"人名 - 标题"格式（比如个人演讲），这种可以直接从标题里
    拿到确切的发言人，不需要再调用 LLM 去猜。
    Panel / Fireside Chat / Workshop / Startup Spotlight 这类明显是多人对话的标题，
    返回 None，交给上层用 LLM 做上下文推测。
    """
    title = (title or "").strip()
    if not title or MULTI_SPEAKER_TITLE_RE.match(title):
        return None
    m = _SINGLE_SPEAKER_NAME_RE.match(title)
    if not m:
        return None
    name = m.group(1).strip()
    if 1 <= len(name.split()) <= 5:
        return name
    return None


_SERIES_TITLE_HINT_RE = re.compile(
    r"(podcast|interview|talk\s*show|访谈|播客|对谈|专访)", re.IGNORECASE
)
_SUMMIT_TITLE_HINT_RE = re.compile(
    r"(summit|conference|forum|expo|峰会|大会|论坛|展会|研讨会)", re.IGNORECASE
)


def _sample_upload_date_spread_days(entries: list[dict]) -> Optional[int]:
    """轻量抽样：只取列表首尾各一个视频，各打一次请求拿上传日期，估算这批视频的
    时间跨度——持续更新的播客/访谈栏目跨度通常是几个月甚至几年，集中录制的大会/
    峰会通常就一两天。只在标题关键词判断不出结果、且也不是频道 videos 标签页时
    才会用到，不会拖慢常规的"获取议题列表"流程。拿不到就返回 None，不影响主流程。
    """
    sample_ids = []
    for e in (entries[0], entries[-1]):
        vid = e.get("id")
        if vid and vid not in sample_ids:
            sample_ids.append(vid)
    if len(sample_ids) < 2:
        return None
    dates = []
    for vid in sample_ids:
        try:
            ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True, "ignoreerrors": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                video_info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False) or {}
            upload_date = video_info.get("upload_date")
            if upload_date:
                dates.append(datetime.strptime(upload_date, "%Y%m%d"))
        except Exception:  # noqa: BLE001
            continue
    if len(dates) < 2:
        return None
    return abs((max(dates) - min(dates)).days)


def guess_content_type(info: dict, entries: Optional[list[dict]] = None) -> str:
    """粗略猜测这批视频是"一场大会/峰会的演讲合集"（summit）还是"一个频道/播客栏目的多期内容"（series）。
    依次尝试三个信号，命中一个就不看后面的：
    1. 播放列表/频道标题里的关键词（"Podcast""Interview""峰会""Summit" 之类）——
       最快，大部分播客/访谈类播放列表（而不只是频道 videos 标签页）都能靠这条识别出来。
    2. YouTube 频道的"视频/直播/Shorts"等标签页在 yt-dlp 里没有独立的播放列表实体，
       返回的 id 直接就是频道 id（channel_id）；用户自建或会议官方整理的播放列表则有
       自己独立的 id，和频道 id 不同——这条能识别"整个频道当播客栏目在处理"的情况。
    3. 都判断不出来时，抽样看首尾两个视频的发布时间跨度：跨度很大（同一个"系列"
       里的视频分散在几个月甚至几年）大概率是持续更新的播客/栏目，跨度很小
       （比如就一两天）大概率是集中录制的大会/峰会。
    这只是一个默认猜测，前端会展示出来并允许用户手动改，不影响实际处理逻辑，
    只影响生成的总结用什么语气和结构撰写、以及产物文件名是编号还是播出日期前缀。
    """
    title = info.get("title") or ""
    has_series_hint = bool(_SERIES_TITLE_HINT_RE.search(title))
    has_summit_hint = bool(_SUMMIT_TITLE_HINT_RE.search(title))
    if has_series_hint and not has_summit_hint:
        return "series"
    if has_summit_hint and not has_series_hint:
        return "summit"

    playlist_id = info.get("id")
    channel_id = info.get("channel_id")
    if playlist_id and channel_id and playlist_id == channel_id:
        return "series"

    if entries and len(entries) >= 2:
        spread_days = _sample_upload_date_spread_days(entries)
        if spread_days is not None and spread_days >= 30:
            return "series"

    return "summit"


def sanitize_filename(name: str, maxlen: int = 120) -> str:
    name = (name or "untitled").strip()
    name = re.sub(r'[\\/:*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # "." / ".." 当目录名会指向当前/上级目录；开头的点会变成隐藏文件
    name = name.lstrip(". ")
    if not name:
        name = "untitled"
    return name[:maxlen]


def fetch_playlist(url: str, light: bool = False) -> dict:
    """发现入口：按链接形态分派到对应来源的解析路径。light=True 时 sitemap 源只列
    链接、不逐篇抓正文（添加订阅只需要知道"这是什么源、有多少条"）。"""
    host = urllib.parse.urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return _fetch_youtube_playlist(url)
    if sources.is_apple_podcast_url(url):
        return sources.fetch_apple_podcast_playlist(url)
    if sources.is_wechat_article_url(url):
        return sources.fetch_wechat_article_playlist(url)
    if sources.is_rss_url(url):
        return sources.fetch_rss_playlist(url)
    try:
        return _fetch_substack_playlist(url)
    except RuntimeError as substack_err:
        # 域名不像 Substack、路径也没有 .xml/.rss 这类明显后缀——有可能是个不走
        # 寻常路径的 RSS/Atom 地址（不少播客 feed 长在 /feed、/podcast.rss 这类
        # 没有固定规律的路径上），顺手当 RSS 试一次；再不行，试试这个网站有没有
        # sitemap.xml（多数现代网站都有，不管是不是博客）；三条路都走不通，就把
        # Substack 那次的报错抛出去，它的措辞已经提示了"确认是有效链接"，对
        # 用户更好懂，比亮出最后一次尝试（sitemap）的报错更贴近用户实际输错了
        # 什么。
        try:
            return sources.fetch_rss_playlist(url)
        except Exception:
            pass
        try:
            return sources.fetch_sitemap_playlist(url, fetch_bodies=not light)
        except Exception:
            raise substack_err from None


# --------------------------------------------------------------------------
# 从剪贴板粘贴的自由文本里批量提取链接，逐条解析成独立议题——跟上面
# fetch_playlist 不一样：这里每个链接各自代表"一条"内容，而不是它背后可能
# 指向的一整份播放列表/订阅源/节目，链接之间也不属于同一份列表。
# （extract_urls 本身定义在 core/sources.py，notes2insight 的"导入链接"
# 功能也要用同一份，不是 summit2md 专属的。）
# --------------------------------------------------------------------------

def _fetch_single_youtube_entry(url: str) -> tuple[Optional[dict], Optional[str]]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        vid = parsed.path.strip("/").split("/")[0] or None
    else:
        vid = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
    if not vid:
        return None, "这是 YouTube 播放列表/频道链接，不是单条视频链接"
    try:
        playlist = _fetch_youtube_playlist(f"https://www.youtube.com/watch?v={vid}")
    except Exception as e:  # noqa: BLE001
        return None, f"解析失败：{e}"
    entries = playlist.get("entries") or []
    if not entries:
        return None, "没能解析出这条视频的信息"
    return entries[0], None


def fetch_single_entry(url: str) -> tuple[Optional[dict], Optional[str]]:
    """把一个链接解析成"一条"议题。返回 (entry, skip_reason)，二者恰好一个为 None——
    链接指向的是一整份列表（播放列表/订阅源/节目主页）而不是单条内容时，给出
    人能看懂的原因，而不是把整份列表都展开（用户粘贴的这几个链接彼此独立，
    展开一整份列表会让"专题"变成大杂烩，不是这个功能的本意）。
    """
    url = url if "://" in url else f"https://{url}"
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()

    if "youtube.com" in host or "youtu.be" in host:
        return _fetch_single_youtube_entry(url)
    if sources.is_wechat_article_url(url):
        try:
            playlist = sources.fetch_wechat_article_playlist(url)
        except Exception as e:  # noqa: BLE001
            return None, f"解析失败：{e}"
        return playlist["entries"][0], None
    if sources.is_apple_podcast_url(url):
        return None, "这是 Apple Podcast 节目主页链接，不是单集"
    if sources.is_rss_url(url):
        return None, "这是 RSS/Atom 订阅源链接，不是单篇文章"

    if _SUBSTACK_POST_PATH_RE.match(parsed.path):
        try:
            playlist = _fetch_substack_playlist(url)
            return playlist["entries"][0], None
        except Exception:
            pass  # 路径长得像 Substack 单篇文章，但请求失败——当普通网页文章再试一次

    try:
        entry = sources.fetch_generic_article_entry(url)
    except Exception as e:  # noqa: BLE001
        return None, f"解析失败：{e}"
    return entry, None


def fetch_entries_from_text(text: str) -> dict:
    """从一段自由文本里批量提取链接、逐条解析成议题，供"剪贴板批量导入拼成
    专题"这个入口用。返回 {"entries", "skipped", "content_type"}；跟
    fetch_playlist 不同，这里没有单一的"节目/大会标题"可言，交给调用方自己定。
    """
    urls = sources.extract_urls(text)
    if not urls:
        raise RuntimeError("没有在这段文字里找到任何链接")
    entries: list[dict] = []
    skipped: list[dict] = []
    for url in urls:
        entry, reason = fetch_single_entry(url)
        if entry is None:
            skipped.append({"url": url, "reason": reason or "解析失败"})
            continue
        entry["index"] = len(entries) + 1
        entries.append(entry)
    if not entries:
        raise RuntimeError(
            "提取到了链接，但一条都没能解析成功——" + "；".join(f"{s['url']}：{s['reason']}" for s in skipped[:3])
        )
    return {"entries": entries, "skipped": skipped, "content_type": "series"}


_CHANNEL_SUBTAB_EXCLUDE_RE = re.compile(r"/(shorts|streams|playlists|community|store|about)(/|$|\?)", re.IGNORECASE)


def _fetch_youtube_playlist(url: str) -> dict:
    """轻量拉取播放列表元数据（不解析每条视频的完整信息，速度快）。"""
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("无法解析该链接，请确认是有效的 YouTube 播放列表或视频链接")

    if "entries" in info and info["entries"] is not None:
        raw_entries = list(info["entries"])
        summit_title = info.get("title") or "Untitled Summit"
        playlist_id = info.get("id")

        # 频道「视频」标签页有时候第一页就没有翻页 token 了，导致抓不全
        # （实测 SemiAnalysis 频道实际发布 185 期，直接抓 /videos 标签页却只拿到 35 期）。
        # 频道自带的「上传」播放列表（list=UUxxxx）走的是另一条更可靠的分页路径，
        # 这里做兜底：只要能从结果里识别出频道 id，且原链接本身不是一个具体播放列表
        # 或 shorts/直播回放等子标签页，就用 uploads 播放列表重抓一遍，条目更多就采用它。
        channel_id = info.get("channel_id")
        if "list=" not in url and not _CHANNEL_SUBTAB_EXCLUDE_RE.search(url) and channel_id and channel_id.startswith("UC"):
            uploads_url = f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}"
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                    uploads_info = ydl2.extract_info(uploads_url, download=False)
            except Exception:
                uploads_info = None
            if uploads_info and uploads_info.get("entries") is not None:
                uploads_entries = list(uploads_info["entries"])
                if len(uploads_entries) > len(raw_entries):
                    raw_entries = uploads_entries
    else:
        raw_entries = [info]
        summit_title = info.get("title") or "Untitled Video"
        playlist_id = info.get("id")

    entries = []
    for idx, e in enumerate(raw_entries, start=1):
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or f"Untitled-{idx}"
        if not vid or title in ("[Private video]", "[Deleted video]"):
            continue
        duration = e.get("duration") or 0
        entries.append(
            {
                "index": idx,
                "id": vid,
                "title": title,
                "duration": duration,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "is_raw_session": is_raw_session(title, duration),
            }
        )

    return {
        "summit_title": summit_title,
        "playlist_id": playlist_id,
        "entries": entries,
        "content_type": guess_content_type(info, entries),
    }


def fetch_subtitle_languages(url: str) -> dict:
    """探测某个视频实际可用的字幕语言（自动字幕 + 人工上传字幕），用于 GUI 里的语言下拉菜单。
    传入单个视频链接（不要带 &list=... 播放列表参数），避免误触发整个播放列表的深度抓取。
    """
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("entries") is not None:
        info = next((e for e in info["entries"] if e), {}) or {}

    auto = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}

    # YouTube 的自动字幕列表几乎总是带着一整套"自动翻译"目标语言——不管这条视频
    # 实际说的是什么语言，都会把 ab/aa/af/ak/sq/... 这上百种翻译目标一起列出来，
    # GUI 下拉菜单里一次甩出一大串，挑起来很难受，而且这些译文对我们没用：翻译轨道
    # 是拿原始字幕机翻出来的，比直接喂原始语言给后面的 LLM 更差。真正有意义的只有
    # 两种：key 以 "-orig" 结尾的轨道（YouTube 用这个后缀标记"不是翻译来的、直接
    # 识别出的原始语言"，yt-dlp 接受这种带后缀的 code 直接下载）和人工上传的官方字幕。
    codes = {c for c in auto if c.endswith("-orig")} | set(manual)
    if not codes:
        # 极少数视频探测不到任何 -orig 轨道（比如自动字幕本身就没生成），退回
        # 完整列表——不能因为猜不出"原始语言"就让语言选项直接消失。
        codes = set(auto) | set(manual)

    languages = [{"code": c, "name": _lang_display_name(c)} for c in sorted(codes)]
    return {"languages": languages, "original_language": info.get("language")}


# --------------------------------------------------------------------------
# Substack 播客站点的发现 + 转写抓取
#
# 这类站点（包括自定义域名，比如 dwarkesh.com）每篇播客文章正文里通常已经带有
# 官方整理好、按发言人分段的完整对话转写（不是语音识别产物），并且站点自己有一套
# 公开、不需要鉴权的 JSON API，比爬取页面 HTML 或指望 yt-dlp/语音识别更快也更准。
# --------------------------------------------------------------------------

_SUBSTACK_POST_PATH_RE = re.compile(r"^/p/([a-zA-Z0-9_-]+)")
_TRANSCRIPT_HEADING_RE = re.compile(r"^\s*transcript\s*$", re.IGNORECASE)
_CHAPTER_TIME_RE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")
# 有些播客单集正文其实很短，只是"这是我上周那篇文章的语音朗读版，完整原文在这里"
# 之类的一句话加一个跳转链接（真正的转写/正文在被链接的那篇文章里）。正文长度明显
# 超过这个阈值就不再当作跳转页处理，避免误把长文里的引用链接当成跳转目标。
_SUBSTACK_REDIRECT_MAX_BODY_LEN = 3000
# 兜底把正文按纯段落抠出来时，抠到的文字太少（比如整页就一句"本期仅限付费订阅者"）
# 就不当作有效内容，视为该文章确实没有公开内容，避免把付费墙提示当成"转写"存下来。
_SUBSTACK_MIN_PLAIN_BODY_LEN = 300


def _substack_api_get(domain: str, path: str) -> dict:
    return json.loads(sources.http_get(f"https://{domain}{path}"))


def _guess_substack_pub_title(domain: str) -> str:
    name = re.sub(r"^www\.", "", domain)
    name = re.sub(r"\.(substack\.com|com|org|net|io|co)$", "", name)
    return name.replace("-", " ").replace(".", " ").title() or domain


def _fetch_substack_archive_podcasts(domain: str) -> list[dict]:
    """翻页拉取该站点全部「播客」类型的文章（跳过普通图文/newsletter），
    最多翻 100 页防止异常情况下死循环。

    注意：Substack 这个接口某一页实际返回的条数可能比请求的 limit 少，
    但这不代表已经翻到最后——比如 dwarkesh.com 第一页明明还有上百集，却只
    返回 23 条。所以翻页时 offset 必须按"实际收到的条数"累加，而不是按固定
    的 limit 累加或者一见到不满页就提前终止，否则会漏掉后面所有的单集
    （之前就是这么漏掉了 dwarkesh.com 一百多集里的大部分）。只有当某一页
    真的一条都没返回时，才说明翻到底了。
    """
    items: list[dict] = []
    offset, limit = 0, 50
    for _ in range(100):
        page = _substack_api_get(domain, f"/api/v1/archive?sort=new&search=&offset={offset}&limit={limit}")
        if not isinstance(page, list):
            # 不是 Substack 的站点（或者接口改了）会返回一个对象甚至 HTML 错误页，
            # 按"不是 Substack"报错，好让 fetch_playlist 接着试 RSS / sitemap
            raise RuntimeError("这个站点的 /api/v1/archive 返回的不是 Substack 的文章列表")
        if not page:
            break
        items.extend(p for p in page if isinstance(p, dict) and p.get("type") == "podcast")
        offset += len(page)
    return items


_SUBSTACK_POST_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _substack_item_to_entry(item: dict, idx: int) -> dict:
    slug = item.get("slug") or f"untitled-{idx}"
    entry = {
        "index": idx,
        "id": slug,
        "title": item.get("title") or f"Untitled-{idx}",
        "duration": round(item.get("podcast_duration") or 0),
        "url": item.get("canonical_url") or "",
        "source_type": "substack",
        "is_raw_session": False,
    }
    date_m = _SUBSTACK_POST_DATE_RE.match(item.get("post_date") or "")
    if date_m:
        # 播客单集用发布日期命名文件（而不是处理顺序编号），方便直接从文件名看出期数时间。
        entry["publish_date"] = "".join(date_m.groups())
    return entry


def _fetch_substack_playlist(url: str) -> dict:
    """单集页面（路径形如 /p/<slug>）直接返回这一集；否则当成归档/标签页
    （比如 /t/podcast、/archive 或站点首页），翻页拉取该站点全部播客单集。
    """
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    domain = parsed.netloc
    m = _SUBSTACK_POST_PATH_RE.match(parsed.path)
    try:
        if m:
            post = _substack_api_get(domain, f"/api/v1/posts/{m.group(1)}")
            entries = [_substack_item_to_entry(post, 1)]
            return {
                "summit_title": post.get("title") or "Untitled Podcast",
                "playlist_id": m.group(1),
                "entries": entries,
                "content_type": "series",
            }

        items = _fetch_substack_archive_podcasts(domain)
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        raise RuntimeError(
            f"无法解析该链接（{e}），请确认是有效的 YouTube 播放列表/视频，或 Substack 播客链接"
        ) from e
    if not items:
        raise RuntimeError("无法解析该链接：既不是 YouTube 播放列表/视频，也没能在这个 Substack 站点里找到任何播客单集")
    entries = [_substack_item_to_entry(it, idx) for idx, it in enumerate(items, start=1)]
    return {
        "summit_title": _guess_substack_pub_title(domain),
        "playlist_id": domain,
        "entries": entries,
        "content_type": "series",
    }


def _extract_substack_youtube_id(body_html: str, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    soup = soup if soup is not None else BeautifulSoup(body_html, "lxml")
    youtube_id = None
    wrap = soup.select_one("[data-attrs*='videoId']")
    if wrap is not None:
        try:
            attrs = json.loads(html.unescape(wrap.get("data-attrs", "")))
            youtube_id = attrs.get("videoId")
        except (ValueError, TypeError):
            youtube_id = None
    if not youtube_id:
        yt_link = soup.select_one("a[href*='youtube.com/watch'], a[href*='youtu.be']")
        if yt_link is not None:
            youtube_id = _extract_youtube_id(yt_link.get("href", ""))
    return youtube_id


def _find_substack_redirect_link(body_html: str, own_host: str, own_slug: str) -> Optional[str]:
    """短正文里如果有一个指向同站另一篇文章的链接（比如"完整原文在这里"），
    返回那篇文章的 slug；找不到就返回 None。不跟随跳到别的站点的链接。
    """
    soup = BeautifulSoup(body_html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        parsed = urllib.parse.urlparse(href)
        if parsed.netloc and parsed.netloc != own_host:
            continue
        m = _SUBSTACK_POST_PATH_RE.match(parsed.path or href)
        if m and m.group(1) != own_slug:
            return m.group(1)
    return None


def _extract_substack_plain_paragraphs(body_html: str) -> list[tuple[float, str]]:
    """把一篇没有「Transcript」小节的文章正文（比如某期播客其实是另一篇文章的
    语音朗读版，原文本身是随笔而不是对话）按段落抠成 paragraphs。这种情况下没有
    真实的语音时间戳，统一用 0 占位——下游据此生成的"跳转到这段"链接实际就是
    跳到视频开头，不会指向错误的时间点。
    """
    soup = BeautifulSoup(body_html, "lxml")
    paragraphs: list[tuple[float, str]] = []
    for tag in soup.find_all(["p", "h1", "h2", "h3", "h4"]):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if tag.name in ("h1", "h2", "h3", "h4"):
            text = f"**{text}**"
        paragraphs.append((0.0, text))
    return paragraphs


def _substantial_substack_plain_paragraphs(body_html: str) -> Optional[list[tuple[float, str]]]:
    """跟 _extract_substack_plain_paragraphs 一样抠段落，但抠出来的文字太短
    （比如整页正文就一句"本期仅限付费订阅者"）就当作没有有效内容返回 None，
    避免把付费墙提示之类的空壳当成"转写"存下来。
    """
    paragraphs = _extract_substack_plain_paragraphs(body_html)
    if not paragraphs or sum(len(t) for _, t in paragraphs) < _SUBSTACK_MIN_PLAIN_BODY_LEN:
        return None
    return paragraphs


def _parse_substack_transcript_html(body_html: str) -> Optional[dict]:
    """从 Substack 文章正文 HTML 里抠出「Transcript」小节，还原成
    (paragraphs, speakers, speaker_mode)：小节里按时间戳分成若干章节（<h3>），
    发言人切换是一个只包含一段粗体文字的独立 <p>，后面跟着这个人说的话，
    直到下一个发言人标记或下一个章节标题。找不到「Transcript」标题就返回 None，
    调用方据此判断这篇文章没有公开转写。
    """
    soup = BeautifulSoup(body_html, "lxml")
    youtube_id = _extract_substack_youtube_id(body_html, soup)

    heading = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        if _TRANSCRIPT_HEADING_RE.match(tag.get_text(strip=True)):
            heading = tag
            break
    if heading is None:
        return None

    paragraphs: list[tuple[float, str]] = []
    speakers: list[Optional[str]] = []
    cur_time = 0.0
    cur_speaker: Optional[str] = None
    for sib in heading.find_all_next():
        if sib.name in ("h1", "h2"):
            break
        if sib.name == "h3":
            tm = _CHAPTER_TIME_RE.match(sib.get_text(" ", strip=True))
            if tm:
                hh = int(tm.group(1)) if tm.group(1) else 0
                cur_time = hh * 3600 + int(tm.group(2)) * 60 + int(tm.group(3))
            continue
        if sib.name != "p":
            continue
        text = re.sub(r"\s+", " ", sib.get_text(" ", strip=True)).strip()
        if not text:
            continue
        strong = sib.find(["strong", "b"])
        strong_text = re.sub(r"\s+", " ", strong.get_text(" ", strip=True)).strip() if strong else ""
        if strong is not None and strong_text == text and len(text) <= 60:
            cur_speaker = text
            continue
        paragraphs.append((float(cur_time), text))
        speakers.append(cur_speaker)

    if not paragraphs:
        return None

    speaker_mode = None
    speakers_out: Optional[list[str]] = None
    if any(s is not None for s in speakers):
        distinct = {s for s in speakers if s is not None}
        speaker_mode = "single" if len(distinct) <= 1 else "multi"
        speakers_out = [s or "未知发言人" for s in speakers]

    return {
        "paragraphs": paragraphs,
        "speakers": speakers_out,
        "speaker_mode": speaker_mode,
        "youtube_id": youtube_id,
    }


def _read_json_cache(path: str) -> Optional[dict]:
    """读缓存；文件不存在或损坏（比如上次写到一半被中断）都当没缓存，重新抓一次。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("paragraphs") else None


def fetch_substack_transcript(entry: dict, cache_dir: str) -> Optional[dict]:
    """抓取某个 Substack 播客单集的完整对话文字稿。本地缓存里已经解析过这一集
    就直接读缓存，不用再重新请求一遍。返回 {"paragraphs", "speakers", "speaker_mode",
    "youtube_id", "lang"}；该文章没有公开转写就返回 None。
    """
    slug = entry["id"]
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{slug}.substack.json")
    cached = _read_json_cache(cache_path)
    if cached:
        cached["paragraphs"] = [tuple(p) for p in cached["paragraphs"]]
        return cached

    parsed = urllib.parse.urlparse(entry["url"])
    m = _SUBSTACK_POST_PATH_RE.match(parsed.path)
    post_slug = m.group(1) if m else slug
    post = _substack_api_get(parsed.netloc, f"/api/v1/posts/{post_slug}")
    body_html = post.get("body_html") or ""
    lang_post = post
    result = _parse_substack_transcript_html(body_html)

    if result is None and len(body_html) <= _SUBSTACK_REDIRECT_MAX_BODY_LEN:
        # 这一集本身没有转写，正文可能只是"这是我上周那篇文章的语音朗读版，
        # 完整原文在这里"外加一个跳转链接——顺着同站链接去看原文章：可能是
        # 另一期带转写的播客（少见），更常见的是一篇没有「Transcript」小节、
        # 需要按纯文章正文抠段落的随笔。
        redirect_slug = _find_substack_redirect_link(body_html, parsed.netloc, post_slug)
        if redirect_slug:
            own_youtube_id = _extract_substack_youtube_id(body_html)
            target_post = _substack_api_get(parsed.netloc, f"/api/v1/posts/{redirect_slug}")
            target_body = target_post.get("body_html") or ""
            result = _parse_substack_transcript_html(target_body)
            if result is None:
                plain = _substantial_substack_plain_paragraphs(target_body)
                if plain:
                    result = {
                        "paragraphs": plain, "speakers": None, "speaker_mode": None,
                        "youtube_id": _extract_substack_youtube_id(target_body),
                    }
            if result is not None:
                lang_post = target_post
                if not result.get("youtube_id"):
                    result["youtube_id"] = own_youtube_id

    if result is None:
        # 既没有「Transcript」小节，也不是指向别处的跳转页——很可能这一集本身
        # 就是图文/清单型的文章（发布在播客分类下，但没有对话转写这种结构），
        # 正文本身就是完整内容，直接按段落抠出来用即可。
        plain = _substantial_substack_plain_paragraphs(body_html)
        if plain:
            result = {
                "paragraphs": plain, "speakers": None, "speaker_mode": None,
                "youtube_id": _extract_substack_youtube_id(body_html),
            }

    if result is None:
        return None
    result["lang"] = lang_post.get("language") or "en"
    atomic.write_json(cache_path, result)
    return result


# --------------------------------------------------------------------------
# 按会议官网议程重新排序
# --------------------------------------------------------------------------

_AGENDA_TRACK_PRIORITY = {"Plenary": 0, "Atlas": 1, "Nexus": 2, "Compass": 3}
_AGENDA_DAY_PRIORITY = {"sat": 0, "sun": 1, "mon": 2, "tue": 3, "wed": 4, "thu": 5, "fri": 6}
_AGENDA_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")
_YOUTUBE_ID_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{6,})")


def _parse_agenda_time(text: str) -> int:
    """把"9:15 AM"这样的文本转成从当天0点算起的分钟数，解析不出来就排到当天最后。"""
    m = _AGENDA_TIME_RE.search(text or "")
    if not m:
        return 24 * 60
    h, minute, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + minute


def _extract_youtube_id(href: str) -> Optional[str]:
    m = _YOUTUBE_ID_RE.search(href or "")
    return m.group(1) if m else None


def _extract_entry_id(url: str) -> str:
    """从渲染产物里记录的链接还原出议题的稳定 id（YouTube 视频 id 或 Substack 文章 slug），
    用于 manifest 的兜底恢复/校验逻辑（读旧文件、按链接反查议题）——必须和发现阶段
    构造 entry["id"] 时用的是同一个 id，否则这些兜底逻辑会错认成两个不同的议题。
    """
    vid = _extract_youtube_id(url)
    if vid:
        return vid
    m = _SUBSTACK_POST_PATH_RE.match(urllib.parse.urlparse(url).path)
    if m:
        return m.group(1)
    return url.rsplit("=", 1)[-1].rsplit("/", 1)[-1]


_TRAILING_VIDEO_TAG_RE = re.compile(r"\s*\[Video\]\s*$", re.IGNORECASE)


def fetch_agenda_order(agenda_url: str) -> dict:
    """抓取会议官网议程页面，尽量解析出每场议题对应的 YouTube 视频、以及它在议程里的真实顺序
    （按"天 -> 时间 -> 分会场"排序，同一时间不同分会场按分会场名做稳定排序）。

    这是针对"标签页(tab) + 按时间排列的事件卡片"这种常见议程页面结构写的通用解析：
    按 class="agenda-tab" 取各个 tab 的标题（一般是"分会场 - 星期几"），按
    class="resource-container" 取每个 tab 对应的内容区，再按文档顺序遍历其中的
    class="session-header"（分组标题，如"Panel: xxx"）和 class="event"（时间 + 具体条目）。
    视频链接有两种常见挂法：挂在某个 class="speaker" 下（单人演讲，一个 event 一个视频），
    或者直接挂在 event 内、不属于任何单个 speaker（多人对话共用一个视频）——按文档顺序
    遍历 event 内所有指向 YouTube 的链接，能落在具体 speaker 下就取那个人的名字，
    否则取该 event 内全部发言人的名字。标题优先用 event 自己的标题，没有就用最近一个
    session-header 的标题（多人对话场次通常是这种情况）。

    如果页面完全是另一套结构、找不到任何 YouTube 链接，返回空结果，调用方应回退为原始
    播放列表顺序，不强行报错。

    返回 {"matched": {<video_id>: {"order": int, "day":, "track":, "time":, "agenda_title":,
    "agenda_speaker":}}}，order 是可以直接用于排序的整数，值本身没有业务含义。
    """
    html_bytes = sources.http_get(agenda_url)
    soup = BeautifulSoup(html_bytes, "lxml")

    tabs = [t.get_text(strip=True) for t in soup.select(".agenda-tab")]
    containers = soup.select(".resource-container")
    if not containers:
        containers = [soup]
        tabs = [""]

    matched: dict[str, dict] = {}
    for tab_idx, container in enumerate(containers):
        tab_label = tabs[tab_idx] if tab_idx < len(tabs) else ""
        track_name, _, day_name = tab_label.rpartition(" - ")
        track_name = (track_name or tab_label).strip()
        day_name = day_name.strip()
        day_key = _AGENDA_DAY_PRIORITY.get(day_name[:3].lower(), len(_AGENDA_DAY_PRIORITY))
        track_key = _AGENDA_TRACK_PRIORITY.get(track_name, len(_AGENDA_TRACK_PRIORITY))

        current_session_title = ""
        nodes = container.select(".session-header, .event") or container.select(".timeline .event")
        for node in nodes:
            if "session-header" in (node.get("class") or []):
                title_el = node.select_one(".session-title")
                current_session_title = title_el.get_text(strip=True) if title_el else node.get_text(strip=True)
                continue

            event = node
            time_el = event.select_one(".event-time")
            time_text = time_el.get_text(strip=True) if time_el else ""
            time_key = _parse_agenda_time(time_text)
            title_el = event.select_one(".event-title")
            event_title = title_el.get_text(strip=True) if title_el else ""
            title_text = event_title or current_session_title

            links = event.select("a[href*='youtube.com/watch'], a[href*='youtu.be']")
            for sp_idx, link in enumerate(links):
                vid = _extract_youtube_id(link.get("href", ""))
                if not vid or vid in matched:
                    continue
                speaker_el = link.find_parent(class_="speaker")
                if speaker_el is not None:
                    name_el = speaker_el.select_one(".speaker-name")
                    speaker_name = name_el.get_text(strip=True) if name_el else ""
                else:
                    names = [n.get_text(strip=True) for n in event.select(".speaker-name")]
                    speaker_name = "、".join(names)
                order = day_key * 10_000_000 + time_key * 10_000 + track_key * 100 + sp_idx
                matched[vid] = {
                    "order": order,
                    "day": day_name,
                    "track": track_name,
                    "time": time_text,
                    "agenda_title": _TRAILING_VIDEO_TAG_RE.sub("", title_text),
                    "agenda_speaker": speaker_name,
                }
    return {"matched": matched}


# --------------------------------------------------------------------------
# 字幕下载 + 清洗
# --------------------------------------------------------------------------


def download_subtitle(video_id: str, out_dir: str, lang_prefs: list[str]) -> Optional[dict]:
    """
    下载自动字幕（vtt），顺带把该视频的简介（description）和发布日期（upload_date，
    形如 "20260915"）一起取回——简介里常有嘉宾名单，供后面做发言人推测用；发布日期
    给播客/访谈类节目的文件名当前缀用。都不用再多打一次请求。
    返回 {"lang":, "path":, "description":, "upload_date":}，找不到字幕则返回 None。
    """
    os.makedirs(out_dir, exist_ok=True)
    # 本地缓存里已经有这个视频的字幕文件，就不用再问 YouTube 要一遍
    # （字幕内容不会变，重跑/补生成时这一步经常是纯浪费的重复请求）。
    # 这个分支没有真的请求 yt-dlp，description/upload_date 从当初下载时顺手存的
    # {id}.meta.json 里取——不然重试时笔记的文件名就丢了日期前缀。
    meta_path = os.path.join(out_dir, f"{video_id}.meta.json")
    for lang in lang_prefs:
        p = os.path.join(out_dir, f"{video_id}.{lang}.vtt")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            return {"lang": lang, "path": p, "description": meta.get("description") or "",
                    "upload_date": meta.get("upload_date") or ""}

    def found(lang: str, path: str) -> dict:
        if description or upload_date:
            atomic.write_json(meta_path, {"description": description, "upload_date": upload_date})
        return {"lang": lang, "path": path, "description": description, "upload_date": upload_date}

    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": lang_prefs,
        "subtitlesformat": "vtt",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    # YouTube 的字幕接口偶尔会瞬时失败/被限流（连续给一整场大会的十几个视频要字幕
    # 时最容易触发 HTTP 429）——429 跟"这个视频真的没字幕"长得不一样：真没字幕时
    # yt-dlp 正常返回、只是没写出文件；429 会抛 DownloadError。这里特意不吞掉异常
    # （ignoreerrors=False，自己 try/except），才分得清"该多等一会儿重试"还是"这
    # 条大概率是真没字幕"——统一按 3 秒退避重试的话，429 基本等不过去，会被误判
    # 成"未找到可用的自动字幕"。
    description = ""
    upload_date = ""
    attempts = 4
    for attempt in range(attempts):
        rate_limited = False
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            rate_limited = "429" in msg or "Too Many Requests" in msg
            info = {}
        description = info.get("description") or description
        upload_date = info.get("upload_date") or upload_date

        for lang in lang_prefs:
            p = os.path.join(out_dir, f"{video_id}.{lang}.vtt")
            if os.path.exists(p):
                return found(lang, p)
        # yt-dlp 有时会返回带地区后缀的语言代码（如 en-US），兜底模糊匹配一次
        matches = sorted(glob.glob(os.path.join(out_dir, f"{video_id}.*.vtt")))
        if matches:
            m = re.search(rf"{re.escape(video_id)}\.([\w-]+)\.vtt$", matches[0])
            lang = m.group(1) if m else "unknown"
            return found(lang, matches[0])

        if attempt < attempts - 1:
            time.sleep(20 if rate_limited else 3)
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_TIME_LINE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)


def _vtt_time_to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_vtt_cues(path: str) -> list[tuple[float, list[str]]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    cues = []
    i, n = 0, len(lines)
    while i < n and "-->" not in lines[i]:
        i += 1
    while i < n:
        line = lines[i]
        m = _TIME_LINE_RE.search(line)
        if m:
            start = _vtt_time_to_seconds(*m.group(1, 2, 3, 4))
            i += 1
            text_lines = []
            # 只有真正的空行（0 个字符）才是 VTT 里两个 cue 之间的分隔符；
            # 一行只有空格字符也是合法的 cue 内容（YouTube 自动字幕里常见），
            # 不能当成分隔符提前截断，否则会把这类 cue 的正文整段丢掉。
            while i < n and lines[i] != "":
                text_lines.append(lines[i])
                i += 1
            cues.append((start, text_lines))
        i += 1
    return cues


_SPEAKER_CHANGE_RE = re.compile(r"^\s*>>\s*")


def _clean_cue_text(text_lines: list[str]) -> tuple[str, bool]:
    """返回 (清洗后的文本, 是否带有说话人切换标记)。
    YouTube 自动字幕会用行首的 ">>"（HTML 转义后是 "&gt;&gt;"）标记发言人切换，
    这里解码 HTML 实体、去掉格式标签，并把 ">>" 标记摘出来单独返回，
    而不是把 "&gt;&gt;" 这种没解码的乱码留在最终文字记录里。
    """
    joined = " ".join(text_lines)
    joined = _TAG_RE.sub("", joined)
    joined = html.unescape(joined)
    joined = re.sub(r"\s+", " ", joined).strip()
    is_speaker_change = bool(_SPEAKER_CHANGE_RE.match(joined))
    joined = _SPEAKER_CHANGE_RE.sub("", joined).strip()
    return joined, is_speaker_change


def vtt_to_paragraphs(path: str, max_chars: int = 480, gap_seconds: float = 4.0) -> list[tuple[float, str]]:
    """
    YouTube 自动字幕用的是"两行滚动窗口"：每个 cue 最多显示两行文字，
    上一个 cue 里较新的那行会原样出现在下一个 cue 的第一行（已稳定、不再变化），
    第二行才是新滚入的文字。逐行比较、跳过与"上一条保留下来的行"完全相同的行，
    就能还原出无重复的完整文字流，再按时间间隔/长度重新分段。
    字幕里如果带有 ">>" 说话人切换标记，也在这里强制断一个新段落，
    这样多人对话（Panel/Fireside Chat）不会被揉进同一段里。
    """
    cues = _parse_vtt_cues(path)
    segments: list[tuple[float, str, bool]] = []
    last_line = None
    for start, raw_lines in cues:
        for raw_line in raw_lines:
            line, speaker_change = _clean_cue_text([raw_line])
            if not line or line == last_line:
                continue
            segments.append((start, line, speaker_change))
            last_line = line

    if not segments:
        return []

    paragraphs: list[tuple[float, str]] = []
    cur_start = segments[0][0]
    cur_parts: list[str] = []
    cur_len = 0
    last_start = None
    for start, text, speaker_change in segments:
        if cur_parts and (
            speaker_change
            or (last_start is not None and start - last_start > gap_seconds)
            or cur_len > max_chars
        ):
            paragraphs.append((cur_start, " ".join(cur_parts)))
            cur_start = start
            cur_parts = []
            cur_len = 0
        cur_parts.append(text)
        cur_len += len(text) + 1
        last_start = start
    if cur_parts:
        paragraphs.append((cur_start, " ".join(cur_parts)))
    return paragraphs


def format_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_duration(seconds: float) -> str:
    return format_timestamp(seconds)


_PUBLISH_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def format_publish_date(date_str: str) -> str:
    """把 "20260915" 这种 YYYYMMDD 格式转成人读的 "2026-09-15"；格式不对就原样返回。"""
    m = _PUBLISH_DATE_RE.match(date_str or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else date_str


_MD_DURATION_LINE_RE = re.compile(r"^- 时长：约\s*\S+\s*$", re.MULTILINE)


def _ensure_publish_date_line(content: str, date_str: str) -> str:
    """给已经生成好的旧文档补一行「播出时间」（插在「时长」那行后面）；
    已经有这行了就不重复插入，方便重复调用（幂等）。"""
    if "- 播出时间：" in content:
        return content
    m = _MD_DURATION_LINE_RE.search(content)
    if not m:
        return content
    insert_at = m.end()
    return content[:insert_at] + f"\n- 播出时间：{format_publish_date(date_str)}" + content[insert_at:]


def _append_time_param(url: str, seconds: float) -> str:
    """给链接加上跳转到指定时间点的参数——YouTube watch 链接（已经带 ?v=）用 "&"，
    没有查询串的链接（比如播客文章页面）用 "?"，避免拼出 "...&t=30s" 这种不合法的写法。
    """
    if not url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(seconds)}s"


# --------------------------------------------------------------------------
# 摘要后端：实现已移到 core/llm.py（与 notes2insight 共用同一份）。这里保留原来的
# 名字和默认参数，pipeline / server 里的调用点一行都不用改。
#
# core.llm.complete() 的默认 max_tokens/timeout 比这里大（4000/600），但本文件里
# 不显式传参的调用点（发言人标注、逐议题小结）一直是按 2000/300 跑的——下面这个
# 包装保持原值，免得在"只搬代码"的这一步里顺手改掉了产出长度和成本。
# --------------------------------------------------------------------------

from core.llm import (  # noqa: E402
    LLMError as SummarizeError,
    Stopped,
    list_ollama_models,
    DEFAULT_OLLAMA_HOST,
    OPENROUTER_API_BASE,
)
from core.llm import complete as _complete  # noqa: E402


def summarize(prompt: str, backend: str, *, api_key: str = "", model: str = "",
              api_base: str = "", max_tokens: int = 2000, timeout: int = 300,
              stop_flag=None) -> str:
    return _complete(prompt, backend, api_key=api_key, model=model, api_base=api_base,
                     max_tokens=max_tokens, timeout=timeout, stop_flag=stop_flag)

# 单篇议题喂给 LLM（小结/演讲稿）的文字记录最多读多少字符；0 表示不限制。超长播客
# （两三个小时的对谈很常见）如果不限制，一次调用的输入 token 会很可观，且大部分模型
# 也有上下文长度限制。超过这个上限时只截断喂给模型的这份输入，保存到 transcripts/
# 目录的原始文字记录不受影响、永远是完整的；截断发生时会在议题小结/演讲稿末尾注明，
# 不会悄悄发生。
DEFAULT_MAX_TRANSCRIPT_CHARS = 120000


_LLM_CACHE_VERSION = "v1"


def llm_cache_dir(out_dir: str) -> str:
    """LLM 结果缓存放在输出目录下，和字幕缓存做邻居：跟着产物一起搬，整个输出目录
    挪到别处也不会丢缓存（manifest 存的同样是相对路径）。"""
    return os.path.join(out_dir, ".cache", "llm")


def _cached_summarize(prompt: str, backend: str, *, api_key: str = "", model: str = "",
                      api_base: str = "", max_tokens: int = 2000, timeout: int = 300,
                      cache_dir: str = "", force: bool = False, stop_flag=None) -> str:
    """带缓存的 summarize；cache_dir 为空就退化成直接调用，保持老行为。

    键 = 提示词全文的哈希 + 后端 + 模型 + 输出上限。这里可以直接哈希提示词，是因为
    summit2md 的提示词里没有会无谓变化的成分（不像 notes2insight 的提示词带着笔记
    路径和本次选择里的序号）：同一个议题、同一套设置，拼出来的提示词逐字相同。
    换模型、改篇幅档位、改截断上限都会让键变化，该重算的照样重算。

    这层缓存原本完全不存在——重试失败项、补生成演讲稿、换个模型重跑总结，之前都会
    把已经算过的东西整份重算一遍。

    force=True 时跳过缓存读取、强制真正调用一次模型（结果还是会写回缓存，供下次用）。
    给"主题总结/聚焦总结"的"重新生成一遍"用——选择没变、内容也没变时，提示词逐字
    相同，普通走一遍生成流程只会命中缓存拿回一模一样的旧文本，用户会觉得"重新生成
    根本没用"。这个选项存在的意义就是让用户能真正拿到模型的一次新输出，所以不能
    走缓存。
    """
    def call() -> str:
        return summarize(prompt, backend, api_key=api_key, model=model, api_base=api_base,
                         max_tokens=max_tokens, timeout=timeout, stop_flag=stop_flag)

    if not cache_dir:
        return call()
    key = _digest.cache_key(_LLM_CACHE_VERSION, backend, model or "-", str(max_tokens),
                            _digest.content_hash(prompt))
    text, _hit = _digest.cached_call(key, call, cache_dir=cache_dir, use_cache=not force)
    return text


def _cap_transcript(text: str, max_chars: int) -> str:
    return text[:max_chars] if max_chars and len(text) > max_chars else text


PER_TOPIC_ROLE_CONTEXT = {
    "summit": "你是技术峰会内容编辑。下面是一场峰会中单个议题（演讲/圆桌/工作坊）的自动语音转写文字记录，可能包含少量识别错误，请结合上下文合理理解。",
    "series": "你是播客/视频节目内容编辑。下面是该节目其中一期内容（访谈/对谈/独白等）的自动语音转写文字记录，可能包含少量识别错误，请结合上下文合理理解。",
}

PER_TOPIC_LENGTH_INSTRUCTIONS = {
    "short": (
        "- <要点1：核心方法或观点，一句话>\n"
        "- <要点2：最关键的数据、案例或产品名（如有，保留英文原名），一句话，非必要可省略>\n"
        "只写 2-3 条要点，每条一句话，不要展开分析。"
    ),
    "medium": (
        "- <要点1：背景/问题>\n"
        "- <要点2：核心方法或观点>\n"
        "- <要点3：关键数据、案例或产品名（如有，保留英文原名）>\n"
        "- <要点4，如有必要>\n"
        "- <要点5，如有必要>\n"
        "写 3-5 条要点，每条一句话即可，不用展开。"
    ),
    "long": (
        "- <要点1：背景/问题，用1-2句话说明>\n"
        "- <要点2：核心方法或观点，用1-2句话说明>\n"
        "- <要点3：关键数据、案例或产品名（如有，保留英文原名），用1-2句话说明>\n"
        "- <要点4~8，如有必要，同样每条用1-2句话展开>\n"
        "写 5-8 条要点，每条用1-2句话具体展开说明（不要只写一句话标题），信息量要明显多于简洁/标准版本。"
    ),
}

PER_TOPIC_PROMPT = """{role_context}

议题标题：{title}
大约时长：{duration}

请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

TLDR: <一句话核心结论，不超过40字，不要markdown>
{length_instruction}

文字记录：
\"\"\"
{transcript}
\"\"\"
"""

SUMMIT_PROMPT = """你是大会内容主编。以下是「{summit_title}」这场峰会中各议题的标题与小结（共 {count} 个议题）。

请用中文撰写一份内容详尽、篇幅充分的大会总结报告，Markdown 格式，包含以下小节（用 "### " 三级标题，不要用一级或二级标题）。
篇幅要求：整体不少于 3000 字（至少是"精简摘要"篇幅的 3 倍以上），每个小节都要充分展开、有具体分析和例证，
不要写成一句话标题式的流水账罗列。

### 大会概览
4-6段文字，除了主题、规模、议程结构（如分会场/track）之外，还要说明大会的定位、举办背景、
主办方/发起机构的角色，以及从整体议程编排能看出的策展思路。

### 主要趋势与共识
8-15条要点，每条至少用2-3句话展开：具体是什么趋势/共识、有哪些议题或案例支撑它、
背后的原因或对行业的影响是什么，不要只写一句话标题。

### 值得关注的演讲
挑选 8-15 个信息量最大或最具讨论性的议题，每个给出标题 + 至少2-3句话说明具体讲了什么、
为什么值得关注、和其他议题有什么关联或互补。

### 分主题看点
请根据实际议题内容自行归纳自然分组（例如基础设施、机器人、企业应用、安全治理、科研等，
具体分组以内容为准，不要生搬硬套）。每组除了列出代表性议题标题，还要用一段话说明这组议题
共同关心的问题、彼此之间的联系或分歧、以及这组议题反映出的行业动向。

### 主题索引
把上面"分主题看点"里出现的每一组，按下面这种格式**再重复列一遍**（这部分只列组名和议题标题，
不要写分析文字，也不要遗漏任何一组）：

主题：<分组名>
- <议题标题>
- <议题标题>

主题：<下一个分组名>
- <议题标题>

议题标题必须逐字复制自下面"议题列表"里的原文，不要改写、不要缩写、不要加序号或多余符号。

只输出报告正文，不要多余开场白。

议题列表：
{topic_list}
"""

SERIES_PROMPT = """你是内容主编。以下是「{summit_title}」这个视频节目/播客栏目中各期内容的标题与小结（共 {count} 期）。

请用中文撰写一份内容详尽、篇幅充分的节目内容总结报告，Markdown 格式，包含以下小节（用 "### " 三级标题，不要用一级或二级标题）。
篇幅要求：整体不少于 3000 字（至少是"精简摘要"篇幅的 3 倍以上），每个小节都要充分展开、有具体分析和例证，
不要写成一句话标题式的流水账罗列。这是一个持续更新的频道/栏目，不是某一场大会，不要使用"大会""峰会""分会场"
这类会议措辞，也不要臆测该节目隶属于哪个公司/机构的"内容矩阵"或类似归属关系——除非小结原文里明确提到，
否则只基于议题标题与小结本身分析内容，不要编造节目的主办方、定位或背景。

### 节目内容概览
4-6段文字，说明这批内容整体在讲什么、涉及哪些主题或嘉宾类型、内容形式（访谈/独白/对谈等，
如能从标题看出），以及从选题上能看出的这个节目的关注方向。

### 主要话题与观点
8-15条要点，每条至少用2-3句话展开：具体是什么话题/观点、有哪些期数或案例支撑它、
背后的原因或对相关领域的影响是什么，不要只写一句话标题。

### 值得关注的期数
挑选 8-15 期信息量最大或最具讨论性的内容，每期给出标题 + 至少2-3句话说明具体讲了什么、
为什么值得关注、和其他期数有什么关联或互补。

### 分类看点
请根据实际内容自行归纳自然分组（具体分组以内容为准，不要生搬硬套）。每组除了列出代表性期数标题，
还要用一段话说明这组内容共同关心的问题、彼此之间的联系或分歧、以及反映出的动向。

### 主题索引
把上面"分类看点"里出现的每一组，按下面这种格式**再重复列一遍**（这部分只列组名和期数标题，
不要写分析文字，也不要遗漏任何一组）：

主题：<分组名>
- <期数标题>
- <期数标题>

主题：<下一个分组名>
- <期数标题>

期数标题必须逐字复制自下面"期数列表"里的原文，不要改写、不要缩写、不要加序号或多余符号。

只输出报告正文，不要多余开场白。

期数列表：
{topic_list}
"""

TOPIC_SUMMARY_PROMPT = """你是内容编辑。以下是「{summit_title}」里「{theme_name}」这个主题下几个{unit}的标题与小结（共 {count} 个）。

请用中文撰写一份围绕这个主题的综合总结，要写得充分详尽、信息量大，不要写成蜻蜓点水的摘要。Markdown 格式，包含以下小节（用 "### " 三级标题，不要用一级或二级标题）：

### 主题综述
8-12段文字，展开说明这几个{unit}共同关心的核心问题、背景与来龙去脉、彼此之间具体的联系或分歧（点名是哪几个{unit}在哪个论点上分歧）、这个主题反映出的动向或结论，尽量引用具体的人名、数据、案例。

### 关键要点
15-25条要点，每条至少用4-6句话展开，综合这几个{unit}里具体的论据、数据、案例、背景，不要只写一句话标题，也不要几条要点写得差不多、互相重复。

### 逐{unit}要点
每个{unit}一段：标题 + 至少5-8句话说明它在这个主题下的具体贡献或独特角度，包含具体的论据、数据或案例，不要只复述标题换种说法。

只输出报告正文，不要多余开场白。

{unit}列表：
{topic_list}
"""

# 手选议题没填标题时用这份：让模型自己先概括一个标题，跟正文合在一次调用里出，
# 不为了取个标题单独再打一次 API。
TOPIC_SUMMARY_AUTO_TITLE_PROMPT = """你是内容编辑。以下是「{summit_title}」里手工挑出来的几个{unit}的标题与小结（共 {count} 个），这几个{unit}没有一个现成的统一主题名。

请先单独一行给出一个简短的中文标题，能概括这几个{unit}的共同主题或视角，8-16字，不用书名号或引号，格式严格如下（"标题："三个字必须原样出现）：
标题：xxx

空一行后，请用中文撰写一份围绕这个主题的综合总结，要写得充分详尽、信息量大，不要写成蜻蜓点水的摘要。Markdown 格式，包含以下小节（用 "### " 三级标题，不要用一级或二级标题）：

### 主题综述
8-12段文字，展开说明这几个{unit}共同关心的核心问题、背景与来龙去脉、彼此之间具体的联系或分歧（点名是哪几个{unit}在哪个论点上分歧）、这个主题反映出的动向或结论，尽量引用具体的人名、数据、案例。

### 关键要点
15-25条要点，每条至少用4-6句话展开，综合这几个{unit}里具体的论据、数据、案例、背景，不要只写一句话标题，也不要几条要点写得差不多、互相重复。

### 逐{unit}要点
每个{unit}一段：标题 + 至少5-8句话说明它在这个主题下的具体贡献或独特角度，包含具体的论据、数据或案例，不要只复述标题换种说法。

只输出开头的标题行和报告正文，不要多余开场白，不要在正文里再重复一遍这个标题。

{unit}列表：
{topic_list}
"""

_AUTO_TITLE_RE = re.compile(r"^标题[:：]\s*(.+)")


def _extract_auto_title(body: str, fallback: str) -> tuple[str, str]:
    """从模型输出开头摘掉"标题：xxx"这一行，返回 (标题, 去掉标题行之后的正文)。
    换了模型或者这次没按格式给标题时，退回 fallback，不能让整个生成因为这个失败。
    """
    stripped = body.strip()
    m = _AUTO_TITLE_RE.match(stripped)
    if not m:
        return fallback, body
    rest = stripped[m.end():].lstrip("\n")
    title = m.group(1).strip().strip("《》\"'“”")
    return (title or fallback), rest


SPEAKER_LABEL_PROMPT = """你是转写编辑，需要给一段多人对话（圆桌/炉边谈话/工作坊/路演）的文字记录标注每一段发言可能是谁说的。

议题标题：{title}

视频简介（可能列出了嘉宾名单，若为空可忽略）：
\"\"\"
{description}
\"\"\"

下面是按顺序编号的文字记录段落。请结合视频简介里的嘉宾名单、以及文字记录里的上下文线索
（自我介绍、被主持人点名、"谢谢xx，那xx怎么看"这类过渡句等）判断每一段最可能是谁在说话。

规则：
- 能确定具体姓名就写姓名（优先用视频简介里给出的全名）。
- 只能确定角色但不确定姓名，就写角色，例如"主持人"。
- 实在无法判断，写"未知发言人"。
- 相邻段落如果明显是同一个人接着说，就标注同一个名字/角色，不要每段都换人。
- 这是基于文字线索的推测，不是精确的说话人识别，给出你最合理的判断即可，不用每段都非常确信。

只输出下面这种格式，每行一个段落编号，不要输出其他任何内容（不要开场白、不要解释）：
1: <发言人>
2: <发言人>
...
{n}: <发言人>

文字记录段落：
{numbered_paragraphs}
"""


def infer_speakers(paragraphs: list[tuple[float, str]], title: str, description: str,
                    backend: str, api_key: str, model: str, api_base: str = "",
                    cache_dir: str = "", stop_flag=None) -> dict[int, str]:
    if not paragraphs:
        return {}
    numbered = "\n\n".join(f"[{i}] {text}" for i, (_, text) in enumerate(paragraphs, start=1))
    prompt = SPEAKER_LABEL_PROMPT.format(
        title=title,
        description=(description or "")[:3000],
        n=len(paragraphs),
        numbered_paragraphs=numbered[:100000],
    )
    raw = _cached_summarize(prompt, backend, api_key=api_key, model=model, api_base=api_base,
                            cache_dir=cache_dir, stop_flag=stop_flag)
    labels: dict[int, str] = {}
    for line in raw.splitlines():
        m = re.match(r"\s*(\d+)\s*[:：]\s*(.+?)\s*$", line)
        if m:
            labels[int(m.group(1))] = m.group(2).strip()
    return labels


SPEECH_SCRIPT_PROMPT_MONO = """你是文字编辑。下面是一段技术峰会内容（演讲/圆桌/工作坊）的自动语音转写文字记录，
可能带有识别错误、口语化填充词（"um"、"you know"、"like"、重复词、说到一半改口等）。

议题标题：{title}
{speaker_note}

【语言规则，最高优先级】原始文字记录的语言是：{lang_name}。你输出的正文必须逐句使用这同一种语言改写，
禁止翻译成中文或任何其他语言——哪怕本提示词、发言人角色标签（如"主持人""未知发言人"）是中文写的，
发言人实际说出的内容也必须保持 {lang_name} 原文改写，不允许出现翻译后的版本。

请把这段文字记录整理成一篇流畅、可读的演讲稿/文章：
- 正文语言见上方【语言规则】，绝对不要翻译。
- 保留全部实质内容、观点、数据、案例，不要总结、不要删减信息量，只是把口语转成书面语。
- 去掉口语填充词（um/uh/you know/like 等）、明显的重复和结巴、无意义的语气词。
- 结合上下文合理修正明显的语音识别错误（人名、专业术语、产品名），拿不准的保留原样，不要瞎编内容。
- 按内容自然分段，可以适当加小标题帮助阅读，但不要过度切割，也不要编号列表化原本连贯的叙述。
{speaker_instruction}
- 只输出整理后的正文本身：不要重复输出议题标题（文档已经有标题了），
  不要输出任何开场白、解释，或"以下是整理后的演讲稿"之类的元话语。

原始文字记录：
\"\"\"
{transcript}
\"\"\"
"""

SPEECH_SCRIPT_TRANSLATE_PROMPT = """你是专业译者。下面是一份 {lang_name} 演讲稿/文字记录，已经按段落逐一编号
（每段前面形如 "[3] " 的编号只是段落标记，不属于正文，翻译时不要输出编号本身以外的任何原文）。

请把每一段准确、通顺地翻译成中文：
- 忠实传达原意，不要总结、不要删减信息量，翻译要自然流畅，不要逐字直译导致生硬。
- 每一段如果带有发言人标签（如「**某某**：」），标签保留在译文对应位置或省略都可以，但不需要重复出现两次。
- 严格按 "[编号] 中文翻译" 的格式输出，每段对应一个编号，编号必须和原文完全一致，
  不能遗漏、合并、拆分或新增编号，一个编号只能对应这一段的翻译，不要输出原文。
- 只输出翻译结果本身，不要输出任何解释、开场白，或"以下是翻译"之类的元话语。

原文（已编号，共 {n} 段）：
\"\"\"
{numbered_paragraphs}
\"\"\"
"""

SPEECH_SCRIPT_PROMPT_ZH = """你是文字编辑兼译者。下面是一段技术峰会内容（演讲/圆桌/工作坊）的自动语音转写文字记录，
可能带有识别错误、口语化填充词（"um"、"you know"、"like"、重复词、说到一半改口等）。

议题标题：{title}
{speaker_note}

原始文字记录的语言是：{lang_name}。请把这段文字记录整理并翻译成一篇流畅、可读的中文演讲稿：
- 全文必须是中文（如果原文本来就是中文，直接整理成书面语即可，不需要翻译）。
- 忠实传达原意，保留全部实质内容、观点、数据、案例，不要总结、不要删减信息量。
- 去掉口语填充词（um/uh/you know/like 等）、明显的重复和结巴、无意义的语气词，翻译要通顺自然，
  不要逐字直译导致生硬。
- 结合上下文合理修正明显的语音识别错误（人名、专业术语、产品名可保留英文原名），拿不准的保留原样，
  不要瞎编内容。
- 按内容自然分段，可以适当加小标题帮助阅读，但不要过度切割，也不要编号列表化原本连贯的叙述。
{speaker_instruction}
- 只输出整理后的正文本身：不要重复输出议题标题（文档已经有标题了），
  不要输出任何开场白、解释，或"以下是整理后的演讲稿"之类的元话语。

原始文字记录：
\"\"\"
{transcript}
\"\"\"
"""

_LANG_NAMES = {
    "en": "英语", "zh": "中文", "zh-Hans": "中文（简体）", "zh-Hant": "中文（繁体）",
    "ja": "日语", "ko": "韩语", "es": "西班牙语", "fr": "法语", "de": "德语",
    "pt": "葡萄牙语", "ru": "俄语", "it": "意大利语", "nl": "荷兰语", "sv": "瑞典语",
    "pl": "波兰语", "tr": "土耳其语", "vi": "越南语", "th": "泰语", "id": "印尼语",
    "hi": "印地语", "ar": "阿拉伯语", "he": "希伯来语", "uk": "乌克兰语",
}


def _lang_display_name(lang: str) -> str:
    base = (lang or "").split("-")[0].lower()
    return _LANG_NAMES.get(base, lang or "原字幕语言")


def build_transcript_text_for_speech(paragraphs: list[tuple[float, str]],
                                      speakers: Optional[list[str]], speaker_mode: Optional[str]) -> str:
    if speaker_mode == "multi" and speakers:
        lines = []
        last_speaker = None
        for i, (_, text) in enumerate(paragraphs):
            sp = speakers[i] if i < len(speakers) else "未知发言人"
            if sp != last_speaker:
                lines.append(f"[{sp}]")
                last_speaker = sp
            lines.append(text)
        return "\n".join(lines)
    return "\n".join(t for _, t in paragraphs)


def _speech_speaker_instruction(lang_mode: str, lang_name: str) -> str:
    if lang_mode == "bilingual":
        return (
            "- 这是多人对话，请在发言人切换时用「**发言人名**：」的格式另起一段标出，方便阅读"
            "（可参考原文字记录里方括号标出的发言人；这些角色标签本身可能是中文，直接沿用即可，"
            f"标签只出现在 {lang_name} 正文段落前，中文翻译引用块不用重复标签）。"
        )
    if lang_mode == "zh":
        return (
            "- 这是多人对话，请在发言人切换时用「**发言人名**：」的格式另起一段标出（可参考原文字记录"
            "里方括号标出的发言人，这些角色标签本身可能已经是中文，直接沿用即可）。"
        )
    return (
        "- 这是多人对话，请在发言人切换时用「**发言人名**：」的格式另起一段标出，方便阅读"
        "（可参考原文字记录里方括号标出的发言人；这些角色标签本身可能是中文，直接沿用即可，"
        f"但标签后面引用的发言内容仍必须是 {lang_name} 原文，不要翻译）。"
    )


def _generate_original_language_script(entry: dict, transcript_text: str, speaker_note: str,
                                        speaker_mode: Optional[str], lang_name: str,
                                        backend: str, api_key: str, model: str, api_base: str,
                                        max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
                                        cache_dir: str = "", stop_flag=None) -> str:
    """按原语言整理演讲稿正文（不翻译）。bilingual 模式先靠这一步拿到干净的原文，
    再单独一步翻译，避免让模型在同一次输出里既要"保持原文"又要"翻译成中文"，
    容易顾此失彼、把正文本身也写成了中文。
    """
    speaker_instruction = _speech_speaker_instruction("original", lang_name) if speaker_mode == "multi" else ""
    prompt = SPEECH_SCRIPT_PROMPT_MONO.format(
        title=entry["title"],
        speaker_note=speaker_note,
        lang_name=lang_name,
        speaker_instruction=speaker_instruction,
        transcript=_cap_transcript(transcript_text, max_transcript_chars),
    )
    return _cached_summarize(
        prompt, backend, api_key=api_key, model=model, api_base=api_base,
        max_tokens=8000, timeout=600, cache_dir=cache_dir, stop_flag=stop_flag,
    )


def _split_speech_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n+", text.strip()) if p.strip()]


def _parse_numbered_translations(raw: str) -> dict[int, str]:
    matches = list(re.finditer(r"\[(\d+)\]\s*", raw))
    result: dict[int, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        result[int(m.group(1))] = raw[start:end].strip()
    return result


def _translate_speech_paragraphs(paragraphs: list[str], lang_name: str,
                                  backend: str, api_key: str, model: str, api_base: str,
                                  cache_dir: str = "", stop_flag=None) -> dict[int, str]:
    numbered = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs, start=1))
    prompt = SPEECH_SCRIPT_TRANSLATE_PROMPT.format(
        lang_name=lang_name, n=len(paragraphs), numbered_paragraphs=numbered[:120000],
    )
    raw = _cached_summarize(
        prompt, backend, api_key=api_key, model=model, api_base=api_base,
        max_tokens=12000, timeout=700, cache_dir=cache_dir, stop_flag=stop_flag,
    )
    return _parse_numbered_translations(raw)


def generate_speech_script(entry: dict, paragraphs: list[tuple[float, str]],
                            speakers: Optional[list[str]], speaker_mode: Optional[str],
                            sub_lang: str, lang_mode: str,
                            backend: str, api_key: str, model: str,
                            api_base: str = "",
                            max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
                            cache_dir: str = "", stop_flag=None) -> tuple[str, str]:
    """lang_mode: "original"（保持原文不翻译）/ "zh"（整篇翻译成中文）/ "bilingual"（原文+中文对照）。
    源字幕本身就是中文时，"bilingual" 会自动降级为 "zh"（没有另一种语言可以对照）。
    返回 (演讲稿正文, 实际使用的 lang_mode)。
    """
    is_source_zh = (sub_lang or "").split("-")[0].lower() == "zh"
    if lang_mode == "bilingual" and is_source_zh:
        lang_mode = "zh"

    transcript_text = build_transcript_text_for_speech(paragraphs, speakers, speaker_mode)
    lang_name = _lang_display_name(sub_lang)
    speaker_note = f"演讲者：{speakers[0]}" if speaker_mode == "single" and speakers else ""

    if lang_mode == "bilingual":
        original_text = _generate_original_language_script(
            entry, transcript_text, speaker_note, speaker_mode, lang_name,
            backend, api_key, model, api_base, max_transcript_chars,
            cache_dir=cache_dir, stop_flag=stop_flag,
        )
        paras = _split_speech_paragraphs(original_text)
        try:
            translations = _translate_speech_paragraphs(
                paras, lang_name, backend, api_key, model, api_base,
                cache_dir=cache_dir, stop_flag=stop_flag,
            ) if paras else {}
        except Stopped:
            raise
        except SummarizeError:
            # 翻译这一步失败也不丢掉已经生成的原文演讲稿，降级成"仅原文"返回。
            return original_text, "original"
        out = []
        for i, p in enumerate(paras, start=1):
            out.append(p)
            t = translations.get(i)
            if t:
                out.append("\n".join(f"> {line}" for line in t.splitlines()))
        return "\n\n".join(out), "bilingual"

    speaker_instruction = _speech_speaker_instruction(lang_mode, lang_name) if speaker_mode == "multi" else ""
    prompt_tpl = SPEECH_SCRIPT_PROMPT_ZH if lang_mode == "zh" else SPEECH_SCRIPT_PROMPT_MONO
    prompt = prompt_tpl.format(
        title=entry["title"],
        speaker_note=speaker_note,
        lang_name=lang_name,
        speaker_instruction=speaker_instruction,
        transcript=_cap_transcript(transcript_text, max_transcript_chars),
    )
    max_tokens = 10000 if lang_mode == "zh" else 8000
    timeout = 700 if lang_mode == "zh" else 600
    text = _cached_summarize(
        prompt, backend, api_key=api_key, model=model, api_base=api_base,
        max_tokens=max_tokens, timeout=timeout, cache_dir=cache_dir, stop_flag=stop_flag,
    )
    return text, lang_mode


def parse_topic_summary(raw: str) -> dict:
    lines = (raw or "").strip().splitlines()
    tldr = ""
    body_lines = []
    for line in lines:
        if line.strip().upper().startswith("TLDR:"):
            tldr = line.split(":", 1)[1].strip()
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not tldr:
        tldr = (body_lines[0].lstrip("- ").strip() if body_lines else "")[:60]
    return {"tldr": tldr, "body": body or raw.strip()}


_TOPIC_INDEX_HEADING_RE = re.compile(r"^###\s*主题索引\s*$", re.MULTILINE)
_TOPIC_GROUP_LINE_RE = re.compile(r"^\s*主题[:：]\s*(.+?)\s*$")
_TOPIC_ITEM_LINE_RE = re.compile(r"^\s*[-*]\s*(.+?)\s*$")


def _split_topic_index_section(overall_summary: str) -> tuple[str, str]:
    """把大会总结正文里的"### 主题索引"小节摘出来（连同标题一起），
    返回 (去掉这节之后的正文, 这节的原始内容)；没有这节就返回 (原文, "")。
    """
    m = _TOPIC_INDEX_HEADING_RE.search(overall_summary or "")
    if not m:
        return overall_summary, ""
    return overall_summary[: m.start()].rstrip(), overall_summary[m.start() :]


def _parse_topic_groups(section_text: str) -> list[tuple[str, list[str]]]:
    """解析"主题：<组名>\\n- 标题\\n- 标题"这种格式，返回 [(组名, [标题, ...]), ...]。
    不符合格式的行直接跳过——这本来就是"尽量解析"，解析不出来时调用方会保留模型的原始输出兜底。
    """
    groups: list[tuple[str, list[str]]] = []
    current_name: Optional[str] = None
    current_titles: list[str] = []
    for line in section_text.splitlines():
        m = _TOPIC_GROUP_LINE_RE.match(line)
        if m:
            if current_name is not None:
                groups.append((current_name, current_titles))
            current_name = m.group(1).strip("*# ")
            current_titles = []
            continue
        m2 = _TOPIC_ITEM_LINE_RE.match(line)
        if m2 and current_name is not None:
            title = m2.group(1).strip("《》\"'` ")
            if title:
                current_titles.append(title)
    if current_name is not None:
        groups.append((current_name, current_titles))
    return groups


def _normalize_title(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _build_title_lookup(full_rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """按标题、以及归一化后的标题（去标点/空白/大小写）各建一份索引，方便模型复述标题
    时哪怕有点小出入也能对得上号。"""
    ok_rows = [r for r in full_rows if r.get("ok")]
    by_title = {r["entry"]["title"]: r for r in ok_rows}
    by_normalized = {_normalize_title(r["entry"]["title"]): r for r in ok_rows}
    return by_title, by_normalized


def _resolve_title_row(title: str, by_title: dict, by_normalized: dict) -> Optional[dict]:
    return by_title.get(title) or by_normalized.get(_normalize_title(title))


def _row_link(row: dict, from_subdir: Optional[str] = None) -> Optional[str]:
    """议题对应的可点击链接：优先演讲稿，其次文字记录，最后是原始视频/文章链接。
    from_subdir 表示调用方渲染的文档本身放在 out_dir 下的哪个子目录（比如"topics"），
    这时相对路径要多退一层，否则链接会指向子目录内部一个不存在的路径。
    """
    link = row.get("speech_relative_path") or row.get("relative_path") or row["entry"].get("url")
    if link and from_subdir and not re.match(r"^https?://", link):
        link = f"../{link}"
    return link


def _render_topic_index(
    groups: list[tuple[str, list[str]]], full_rows: list[dict]
) -> tuple[str, dict[str, list[str]]]:
    """把模型列出的"主题 -> 议题标题"分组渲染成带真实链接的索引——链接指向已经生成好的
    演讲稿/文字记录（相对 README.md 的路径），而不是模型自己编不出来的文件路径。
    同时返回 {主题名: [议题 id, ...]}，供后续"按主题单独生成总结"复用，不用再解析一遍。
    """
    by_title, by_normalized = _build_title_lookup(full_rows)

    lines = ["### 主题索引", ""]
    topic_groups: dict[str, list[str]] = {}
    for name, titles in groups:
        if not titles:
            continue
        lines.append(f"**{name}**")
        ids: list[str] = []
        for title in titles:
            row = _resolve_title_row(title, by_title, by_normalized)
            if row:
                link = _row_link(row)
                label = row["entry"]["title"]
                lines.append(f"- [{label}]({link})" if link else f"- {label}")
                if row["entry"].get("id"):
                    ids.append(row["entry"]["id"])
            else:
                # 模型复述的标题没能匹配上任何议题（比如稍微改写了一下），保留原文本，
                # 不硬造一个可能指错地方的链接，也不计入这个主题下可用于单独生成总结的议题。
                lines.append(f"- {title}")
        if ids:
            topic_groups[name] = ids
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", topic_groups


def _finalize_overall_summary(overall_summary: str, full_rows: list[dict]) -> tuple[str, dict[str, list[str]]]:
    """把大会总结里模型自己写的"### 主题索引"替换成带真实链接的版本；
    解析不出结构化分组时保留模型的原始输出兜底，不让这节直接消失。
    返回 (处理后的正文, {主题名: [议题 id, ...]})，后者解析失败时是空字典。
    """
    before, index_section = _split_topic_index_section(overall_summary)
    if not index_section:
        return overall_summary, {}
    groups = _parse_topic_groups(index_section)
    if not groups:
        return overall_summary, {}
    rendered, topic_groups = _render_topic_index(groups, full_rows)
    return f"{before}\n\n{rendered}", topic_groups


def _is_failed_summary(summary: Optional[dict]) -> bool:
    """小结生成失败时会写一条"_（摘要生成失败：...）_"占位内容而不是让整个议题失败，
    这里识别出这种占位小结，和真正生成成功的小结区分开，供"只重试小结"这类
    增量补全场景判断。
    """
    return bool(summary) and "摘要生成失败" in (summary.get("body") or "")


def _is_failed_overall_summary(text: Optional[str]) -> bool:
    """大会/节目总结生成失败时（比如 API 额度不足、网络问题）会存一条
    "_（大会总结生成失败：...）_"占位文本，而不是让整个任务失败或者留空——这样 README
    里至少能看出发生过什么，而不是这一整节凭空消失。但这段占位文本绝不能被当成
    "已经有一份可以沿用的总结"：否则下次重跑时，"沿用已有总结，节省 token"的默认
    选项会误把上一次的失败原因当成正经总结继续用下去，用户看不出任何异常。
    """
    return bool(text) and "总结生成失败" in text


_SUMMARY_SECTION_RE = re.compile(r"(## 议题小结\n\n).*?(?=\n## )", re.DOTALL)


def _replace_summary_section(content: str, summary: dict) -> str:
    """在已经渲染好的 md 文件里，原地替换"## 议题小结"这一节的内容（不改动其他部分），
    用于小结重试后同步更新已经存在的演讲稿文档，不用整篇重新生成。
    """
    body = (f"**{summary['tldr']}**\n\n" if summary.get("tldr") else "") + (summary.get("body") or "") + "\n"
    if _SUMMARY_SECTION_RE.search(content):
        return _SUMMARY_SECTION_RE.sub(lambda m: m.group(1) + body, content, count=1)
    return content


# --------------------------------------------------------------------------
# Markdown 渲染
# --------------------------------------------------------------------------


def render_transcript_md(entry: dict, summit_title: str, paragraphs: list[tuple[float, str]],
                          summary: Optional[dict], sub_lang: str,
                          speakers: Optional[list[str]] = None, speaker_mode: Optional[str] = None,
                          include_summary: bool = True, speech_relative_path: Optional[str] = None,
                          content_type: str = "summit") -> str:
    source_type = entry.get("source_type")
    is_substack = source_type == "substack"
    lines = [f"# {entry['title']}", ""]
    belong_label = "所属节目" if content_type == "series" else "所属会议"
    lines.append(f"- {belong_label}：{summit_title}")
    lines.append(f"- 链接：{entry['url']}")
    lines.append(f"- 时长：约 {format_duration(entry['duration'])}")
    if entry.get("publish_date"):
        lines.append(f"- 播出时间：{format_publish_date(entry['publish_date'])}")
    if is_substack:
        if speaker_mode:
            lines.append("- 文字记录来源：节目官方发布的对话转写（非语音识别），已按发言人分段")
        else:
            lines.append("- 文字记录来源：节目官方发布的正文内容（非语音识别）")
    elif source_type == "rss":
        lines.append("- 文字记录来源：RSS/Atom 订阅源抓取的文章正文（非语音识别）")
    elif source_type == "wechat":
        lines.append("- 文字记录来源：微信公众号文章正文（非语音识别）")
    elif source_type == "article":
        lines.append("- 文字记录来源：网页文章正文（非语音识别）")
    else:
        lines.append(f"- 字幕来源：YouTube 自动生成字幕（{sub_lang}），已去重整理，可能存在识别误差")
    if speaker_mode == "single" and speakers:
        source_note = "来自节目官方转写标注" if is_substack else "根据标题解析，单人演讲"
        lines.append(f"- 发言人：{speakers[0]}（{source_note}）")
    elif speaker_mode == "multi":
        if is_substack:
            lines.append("- 发言人：来自节目官方转写标注，按发言人分段")
        else:
            lines.append("- 发言人：AI 基于上下文推测标注，可能不准确，仅供参考")
    if speech_relative_path:
        lines.append(f"- 议题小结与整理后的双语演讲稿见：[{os.path.basename(speech_relative_path)}](../{speech_relative_path})")
    lines.append("")

    if summary and include_summary:
        lines.append("## 议题小结")
        lines.append("")
        if summary.get("tldr"):
            lines.append(f"**{summary['tldr']}**")
            lines.append("")
        lines.append(summary.get("body", ""))
        lines.append("")

    lines.append("## 文字记录")
    lines.append("")
    if not paragraphs:
        lines.append("_（未能获取到该视频的字幕）_")
    else:
        # 时间戳只有在真的对应音视频里某一刻时才有意义，能点进去跳到那一段；
        # 文章类来源（RSS/公众号/Substack 没有转写只有正文时）用 0 占位，
        # 全篇段落时间戳都是 0——这种情况直接当文章正文平铺展示，不用每段
        # 前面都摆一个没有意义的"[0:00]"，看起来像被硬套了播客的格式。
        has_real_timestamps = any(start > 0 for start, _ in paragraphs)
        last_speaker = None
        show_speakers = speaker_mode == "multi" and speakers
        for i, (start, text) in enumerate(paragraphs):
            if show_speakers:
                sp = speakers[i] if i < len(speakers) else None
                if sp and sp != last_speaker:
                    lines.append(f"### 🗣️ {sp}")
                    lines.append("")
                    last_speaker = sp
            if has_real_timestamps:
                ts = format_timestamp(start)
                link = _append_time_param(entry.get("youtube_url") or entry["url"], start)
                lines.append(f"**[{ts}]({link})**  ")
            lines.append(text)
            lines.append("")
    return "\n".join(lines)


_SPEECH_MODE_LABELS = {"bilingual": "原文/中文对照", "zh": "中文翻译", "original": "原文"}


def render_speech_md(entry: dict, summit_title: str, speech_text: str,
                      speaker_mode: Optional[str], speakers: Optional[list[str]],
                      summary: Optional[dict] = None, transcript_relative_path: Optional[str] = None,
                      speech_lang_mode: str = "bilingual", content_type: str = "summit") -> str:
    source_type = entry.get("source_type")
    is_substack = source_type == "substack"
    lines = [f"# {entry['title']}", ""]
    belong_label = "所属节目" if content_type == "series" else "所属会议"
    lines.append(f"- {belong_label}：{summit_title}")
    lines.append(f"- 链接：{entry['url']}")
    lines.append(f"- 时长：约 {format_duration(entry['duration'])}")
    if entry.get("publish_date"):
        lines.append(f"- 播出时间：{format_publish_date(entry['publish_date'])}")
    if speaker_mode == "single" and speakers:
        source_note = "来自节目官方转写标注" if is_substack else "根据标题解析，单人演讲"
        lines.append(f"- 发言人：{speakers[0]}（{source_note}）")
    elif speaker_mode == "multi":
        if is_substack:
            lines.append("- 发言人：来自节目官方转写标注，按发言人分段")
        else:
            lines.append("- 发言人：AI 基于上下文推测标注，可能不准确，仅供参考")
    mode_label = _SPEECH_MODE_LABELS.get(speech_lang_mode, speech_lang_mode)
    source_desc = {
        "substack": "官方转写", "rss": "RSS 文章正文", "wechat": "公众号文章正文", "article": "网页文章正文",
    }.get(source_type, "自动字幕")
    note = (
        f"本文由 AI 基于{source_desc}整理为流畅演讲稿（{mode_label}）"
        "，已去除口语填充词并合理分段，力求保留原意但可能存在改写/翻译误差"
    )
    if transcript_relative_path:
        note += f"，请以视频原声及 [原始文字记录](../{transcript_relative_path}) 为准"
    lines.append(f"- 说明：{note}")
    lines.append("")

    if summary:
        lines.append("## 议题小结")
        lines.append("")
        if summary.get("tldr"):
            lines.append(f"**{summary['tldr']}**")
            lines.append("")
        lines.append(summary.get("body", ""))
        lines.append("")

    lines.append("## 演讲稿")
    lines.append("")
    lines.append(speech_text.strip())
    lines.append("")
    return "\n".join(lines)


def render_index_md(summit_title: str, source_url: str, overall_summary: Optional[str],
                     rows: list[dict], logo_relative_path: Optional[str] = None,
                     content_type: str = "summit") -> str:
    lines = []
    if logo_relative_path:
        lines.append(f'<img src="{logo_relative_path}" alt="summit2md" width="64" height="64" />')
        lines.append("")
    lines.append(f"# {summit_title}")
    lines.append("")
    lines.append(f"> 本目录由 summit2md 工具自动生成于 {time.strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"> 来源播放列表：{source_url}")
    lines.append("")

    if overall_summary:
        lines.append("## 节目总结" if content_type == "series" else "## 大会总结")
        lines.append("")
        lines.append(overall_summary)
        lines.append("")

    lines.append(f"## 议题列表（共 {len(rows)} 个）")
    lines.append("")
    truncated_titles = [r["entry"]["title"] for r in rows if r.get("truncated")]
    if truncated_titles:
        lines.append(
            f"> ⚠️ 以下 {len(truncated_titles)} 个议题的文字记录超过了小结/演讲稿的读取上限，"
            "只读了前面一部分内容生成（下面「原始文字记录」链接始终是完整的，不受影响）："
            + "、".join(truncated_titles)
        )
        lines.append("")
    lines.append("| # | 议题 | 时长 | 视频链接 | 一句话小结 | 演讲稿（含小结） | 原始文字记录 |")
    lines.append("|---|------|------|----------|------------|-------------------|--------------|")
    for i, r in enumerate(rows, start=1):
        title = r["entry"]["title"].replace("|", "-")
        if r.get("truncated"):
            title = "⚠️ " + title
        dur = format_duration(r["entry"]["duration"])
        url = r["entry"]["url"]
        tldr = (r.get("summary", {}) or {}).get("tldr", "").replace("|", "-") if r.get("summary") else (
            "" if r.get("ok") else "⚠️ " + r.get("error", "处理失败")
        )
        transcript_link = f"[记录]({r['relative_path']})" if r.get("relative_path") else "-"
        speech_link = f"[演讲稿]({r['speech_relative_path']})" if r.get("speech_relative_path") else "-"
        lines.append(f"| {i} | {title} | {dur} | [观看]({url}) | {tldr} | {speech_link} | {transcript_link} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 任务编排
# --------------------------------------------------------------------------

ProgressCB = Callable[[dict], None]


def _manifest_path(out_dir: str) -> str:
    return os.path.join(out_dir, ".manifest.json")


_LEGACY_SUMMARY_FILENAME = "README.md"


def _summary_path(out_dir: str) -> str:
    """大会/节目总结文件——跟输出目录同名，而不是千篇一律的 README.md：目录名本来就是
    sanitize_filename(summit_title)，文件名跟着用同一个名字，在 Obsidian 的快速切换器/
    全局搜索里才分得清是哪一场的总结，不用靠路径肉眼辨认一堆同名 README.md。"""
    return os.path.join(out_dir, f"{os.path.basename(out_dir.rstrip(os.sep))}.md")


def _existing_summary_path(out_dir: str) -> Optional[str]:
    """新旧命名都要认——这个改动上线之前生成的目录，总结文件还叫 README.md，不会
    自动改名/迁移；只有下次真正重新生成总结（不是"沿用已有的"）时才会换成新文件名，
    到那时旧的 README.md 会被清掉，不留一份内容重复的旧文件。"""
    new_path = _summary_path(out_dir)
    if os.path.isfile(new_path):
        return new_path
    legacy_path = os.path.join(out_dir, _LEGACY_SUMMARY_FILENAME)
    if os.path.isfile(legacy_path):
        return legacy_path
    return None


def _write_summary(out_dir: str, content: str) -> str:
    """写总结文件（新命名），顺带清掉这个目录里可能残留的旧版 README.md——不然同一份
    总结会留两份文件，旧文件里的内容还是没更新过的那版。返回实际写入的路径。"""
    path = _summary_path(out_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    legacy_path = os.path.join(out_dir, _LEGACY_SUMMARY_FILENAME)
    if legacy_path != path and os.path.isfile(legacy_path):
        try:
            os.remove(legacy_path)
        except OSError:
            pass
    return path


class ManifestCorrupt(RuntimeError):
    pass


def _load_manifest(out_dir: str) -> dict:
    """读取输出目录下的处理进度清单（跨多次运行持久化），用于续跑/跳过已完成/重试失败项。

    文件不存在 = 还没处理过，返回空记录；文件存在但读不出来就报错——当成空记录的
    话，下一次运行会把已经生成过的全部内容重新处理一遍，还会冲掉已有的总结。
    """
    path = _manifest_path(out_dir)
    if not os.path.exists(path):
        return {"entries": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ManifestCorrupt(
            f"处理记录文件读不出来：{path}（{e}）。为了不把已处理的内容当成新的重新生成，"
            f"已停止；可以从备份恢复这个文件，或者确认不需要后删掉它再重试。"
        ) from e
    if not (isinstance(data, dict) and isinstance(data.get("entries"), dict)):
        raise ManifestCorrupt(f"处理记录文件格式不对：{path}。可以从备份恢复，或者确认不需要后删掉它再重试。")
    return data


def find_new_entries(output_base_dir: str, summit_title: str, entries: list[dict]) -> list[dict]:
    """给「信息跟进」订阅列表的"检查更新"用：这个标题对应的输出目录下，manifest
    里已经有哪些 id，entries 里不在这份 id 集合里的就是新内容。跟 skip_existing
    在 process_job() 里用的是同一份 manifest、同一个"entry id"概念，不另外
    在订阅记录里维护一份可能跟 manifest 脱节的"已知 id 列表"。
    """
    out_dir = os.path.join(output_base_dir, sanitize_filename(summit_title))
    manifest = _load_manifest(out_dir)
    known_ids = set(manifest.get("entries") or {})
    return [e for e in entries if e.get("id") and e["id"] not in known_ids]


def probe_overall_summary(output_base_dir: str, summit_title: str) -> bool:
    """给"重新粘贴同一个链接、再点一次获取议题列表"这条路径用的轻量探测：
    这个标题对应的输出目录下，manifest 里是不是已经有一份真正生成成功过的
    大会/节目总结。

    背景：process_job() 里"沿用已有总结、不重新生成"这个省 token 的默认值，
    原来只在走「导入已经生成过的本地目录」时才会把选择露给用户看——但后端
    判断要不要沿用，靠的是 manifest 里有没有真实总结，跟用户是不是走了导入
    这条路径无关。于是重新粘贴同一个播放列表链接（检查有没有新议题最自然的
    操作）如果发现了新议题、点了开始生成，也会命中"有真实总结"这个条件，
    在用户完全没看到任何选择的情况下，默默沿用旧总结、不把新议题纳进去。
    这个函数就是让「重新发现」也能在开始生成前，把同一个选择露出来。

    只看 manifest，不读议题列表、不解析 README——import_output_directory()
    那套完整解析是为了真正导入议题列表用的，这里只是要在开始生成之前问一句
    "有没有旧总结"，没必要付那份代价，也不需要目录已经存在（全新播放列表时
    manifest 读不到，视为没有旧总结）。
    """
    out_dir = os.path.join(output_base_dir, sanitize_filename(summit_title))
    manifest = _load_manifest(out_dir)
    overall_summary = manifest.get("overall_summary")
    return bool(overall_summary) and not _is_failed_overall_summary(overall_summary)


def _save_manifest(out_dir: str, manifest: dict) -> None:
    atomic.write_json(_manifest_path(out_dir), manifest, indent=2)


_README_TOPIC_GROUP_HEADING_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_README_TOPIC_ITEM_RE = re.compile(r"^-\s*(?:\[(.+?)\]\([^)]*\)|(.+?))\s*$")


def _parse_topic_index_from_readme(out_dir: str, manifest: dict) -> dict[str, list[str]]:
    """兜底方案：manifest 里没有存过 topic_groups 时（比如导入了这个功能上线之前生成的
    旧目录），直接读磁盘上已经生成好的总结文件，从渲染好的"### 主题索引"小节里把
    {主题名: [标题, ...]} 解析出来，再按标题反查回 manifest 里对应的议题 id。旧版总结
    没有这一节、或者当年生成总结时模型没按格式输出主题索引，就解析不到，返回空字典
    （调用方按"没有可用的主题分组"处理，不报错）。
    """
    readme_path = _existing_summary_path(out_dir)
    if not readme_path:
        return {}
    try:
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}
    m = _TOPIC_INDEX_HEADING_RE.search(content)
    if not m:
        return {}
    rest = content[m.end():]
    end_m = re.search(r"^##\s+\S", rest, re.MULTILINE)  # 下一节（「议题列表」）开始的地方，主题索引到此为止
    section = rest[: end_m.start()] if end_m else rest

    manifest_entries: dict[str, dict] = manifest.get("entries") or {}
    by_title: dict[str, str] = {}
    by_normalized: dict[str, str] = {}
    for eid, row in manifest_entries.items():
        if not row.get("ok"):
            continue
        title = (row.get("entry") or {}).get("title")
        if not title:
            continue
        by_title[title] = eid
        by_normalized[_normalize_title(title)] = eid

    groups: dict[str, list[str]] = {}
    current_name: Optional[str] = None
    for line in section.splitlines():
        stripped = line.strip()
        hm = _README_TOPIC_GROUP_HEADING_RE.match(stripped)
        if hm:
            current_name = hm.group(1).strip()
            groups.setdefault(current_name, [])
            continue
        if current_name is None:
            continue
        im = _README_TOPIC_ITEM_RE.match(stripped)
        if not im:
            continue
        label = (im.group(1) or im.group(2) or "").strip()
        if not label:
            continue
        eid = by_title.get(label) or by_normalized.get(_normalize_title(label))
        if eid and eid not in groups[current_name]:
            groups[current_name].append(eid)

    return {name: ids for name, ids in groups.items() if ids}


def _get_or_backfill_topic_groups(out_dir: str, manifest: dict) -> dict[str, list[str]]:
    """manifest 里已经存过 topic_groups 就直接用；没有就尝试从磁盘上已有的 README.md
    现读现解析，解析出来的话顺带回填进 manifest（下次不用重新解析）。纯本地解析，
    不需要重新调用模型、不需要 API Key。
    """
    topic_groups: dict[str, list[str]] = manifest.get("topic_groups") or {}
    if topic_groups:
        return topic_groups
    topic_groups = _parse_topic_index_from_readme(out_dir, manifest)
    if topic_groups:
        manifest["topic_groups"] = topic_groups
        _save_manifest(out_dir, manifest)
    return topic_groups


def list_topic_groups(out_dir: str) -> list[dict]:
    """列出这个输出目录已知的主题分组（来自大会总结里的"主题索引"），供界面渲染成勾选列表。
    manifest 里没有缓存过就现读 README.md 解析一遍；两边都没有（还没生成过总结，或者
    生成总结时模型没按格式输出主题索引）就返回空列表。
    """
    manifest = _load_manifest(out_dir)
    topic_groups = _get_or_backfill_topic_groups(out_dir, manifest)
    entries = manifest.get("entries", {})
    result = []
    for name, ids in topic_groups.items():
        titles = [entries[eid]["entry"]["title"] for eid in ids if eid in entries]
        if titles:
            result.append({"name": name, "count": len(titles), "titles": titles})
    return result


def import_output_directory(path: str) -> dict:
    """导入一个此前已经用本工具处理过的输出目录（本机之前跑过、或者从别处拷贝过来的），
    不重新解析播放列表、不重新请求网络，直接从目录里的 .manifest.json（.manifest.json
    不存在时先按老规矩从 transcripts/speech 目录扫描补一份）还原出议题列表、节目类型、
    来源链接，返回的结构和 fetch_playlist() 一致，好让前端"选择议题"那一整套流程直接
    复用：用户可以照常勾选议题，只重试失败项、补生成小结/演讲稿，或者重新生成一遍
    大会/节目总结——不需要原始播放列表链接还能不能打开。
    """
    path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.isdir(path):
        raise RuntimeError(f"目录不存在：{path}")

    manifest = _load_manifest(path)
    if _bootstrap_manifest_from_disk(path, manifest):
        _save_manifest(path, manifest)
    manifest_entries: dict[str, dict] = manifest.get("entries") or {}
    if not manifest_entries:
        raise RuntimeError(
            "这个目录里没有找到任何已生成的议题（没有 .manifest.json，也没能从 "
            "transcripts/speech 子目录扫描出内容），确认选的是 summit2md 生成的输出目录"
        )

    readme_path = _existing_summary_path(path)
    readme_content = ""
    if readme_path:
        try:
            with open(readme_path, encoding="utf-8") as f:
                readme_content = f.read()
        except OSError:
            readme_content = ""

    dir_basename = os.path.basename(path)
    title_m = re.search(r"^#\s+(.+)$", readme_content, re.MULTILINE) if readme_content else None
    candidate_title = title_m.group(1).strip() if title_m else ""
    # 只有「按候选标题重新 sanitize 一遍，能精确还原出目录名」时才用这个更好看的原始标题；
    # 否则（比如目录被手动改过名）就直接用目录名本身，保证后面 /api/run 用同一个标题算
    # 出来的输出目录，还是这一个目录，而不是意外新建一个。
    summit_title = (
        candidate_title if candidate_title and sanitize_filename(candidate_title) == dir_basename
        else dir_basename
    )

    content_type = manifest.get("content_type")
    if content_type not in ("summit", "series"):
        # 这个功能上线之前生成的旧目录没存过这个字段，退化猜一次。
        if any((r.get("entry") or {}).get("source_type") == "substack" for r in manifest_entries.values()):
            content_type = "series"
        elif "所属节目" in readme_content or "节目总结" in readme_content:
            content_type = "series"
        else:
            content_type = "summit"

    source_url = manifest.get("source_url") or ""
    if not source_url and readme_content:
        url_m = re.search(r"^>\s*来源播放列表：(\S+)", readme_content, re.MULTILINE)
        source_url = url_m.group(1) if url_m else ""

    entries = []
    for row in sorted(manifest_entries.values(), key=lambda r: r.get("rank") or 0):
        entry = dict(row.get("entry") or {})
        if not entry.get("id"):
            continue
        entry.setdefault("duration", 0)
        entry.setdefault("is_raw_session", False)
        entry["rank"] = row.get("rank")
        entries.append(entry)
    if not entries:
        raise RuntimeError("这个目录里的 .manifest.json 没有可用的议题记录")

    return {
        "summit_title": summit_title,
        "playlist_id": dir_basename,
        "entries": entries,
        "content_type": content_type,
        "source_url": source_url,
        "output_dir": os.path.dirname(path),
        # 导入目录本身的绝对路径（不是上面那个父目录）——前端读取这个目录已有的主题分组
        # （/api/topic_groups）、生成主题总结（/api/topic_summary）时要用这个，不能用
        # output_dir（那是给 /api/run 重新拼出同一个目录用的，指向的是父目录）。
        "imported_dir": path,
        # 已经有大会/节目总结了——前端据此决定要不要让用户选"沿用还是重新生成"，
        # 不然默认重新点「开始生成」就会白白重新调用一次模型生成总结，浪费 token。
        # 上一次生成失败留下的占位文本不算"有总结"：不然默认会选中"沿用"，把失败
        # 原因当成总结继续用下去，还看不出发生过什么。
        "has_overall_summary": bool(manifest.get("overall_summary")) and not _is_failed_overall_summary(manifest.get("overall_summary")),
    }


def _custom_selection_key(entry_ids: list[str]) -> str:
    """手选议题这条路的"这批议题算同一次选择"判定：跟顺序无关（界面上先勾 A 后勾 B，
    跟先勾 B 后勾 A 应该是同一次选择），去重后按字典序拼起来。"""
    return ",".join(sorted({e.strip() for e in entry_ids if e.strip()}))


def _remembered_custom_label(out_dir: str, entry_ids: list[str]) -> str:
    """标题留空、走 AI 自动概括那条路，文件名要等模型生成完才知道——但"沿用/重新
    生成"这个选择得在用户点生成之前就能判断。这里记一笔"这批 entry_ids 上次自动
    概括出的标题是什么"，下次同一批 entry_ids 再来（哪怕还是留空标题）就能查到。
    """
    manifest = _load_manifest(out_dir)
    key = _custom_selection_key(entry_ids)
    if not key:
        return ""
    return (manifest.get("custom_topic_labels") or {}).get(key, "")


def _remember_custom_label(out_dir: str, entry_ids: list[str], label: str) -> None:
    key = _custom_selection_key(entry_ids)
    if not key or not label:
        return
    manifest = _load_manifest(out_dir)
    labels = manifest.setdefault("custom_topic_labels", {})
    if labels.get(key) == label:
        return
    labels[key] = label
    _save_manifest(out_dir, manifest)


def probe_custom_topic_summary(out_dir: str, entry_ids: list[str], label: str) -> dict:
    """给"手选议题生成聚焦总结"的"沿用/重新生成"选择用：标题手填了就直接探测
    对应文件；标题留空（走自动概括）就先查这批议题上次概括出的标题是什么，
    查得到再探测那份文件在不在。返回 {"exists", "label"}——label 是探测时
    实际用的标题（可能是从记录里查出来的），调用方要用它，不能再假设是空的。
    """
    label = (label or "").strip()
    if not label:
        label = _remembered_custom_label(out_dir, entry_ids)
    if not label:
        return {"exists": False, "label": ""}
    path = os.path.join(out_dir, "topics", sanitize_filename(label, 80) + ".md")
    return {"exists": os.path.isfile(path), "label": label}


def probe_topic_summary(out_dir: str, label: str) -> bool:
    """探测这个标签对应的主题总结文件是不是已经生成过——给"按主题生成聚焦总结"
    "手选议题生成聚焦总结"两处前端用，决定要不要露出"沿用/重新生成"的选择，跟
    probe_overall_summary() 是同一个道理，不然默认重新点一下生成按钮就会白白
    重新调用一次模型，浪费 token。标题留空走自动概括标题那条路时，文件名要
    等模型生成完才知道，没法提前探测，调用方直接跳过、不露出这个选择即可。
    """
    label = label.strip()
    if not label:
        return False
    path = os.path.join(out_dir, "topics", sanitize_filename(label, 80) + ".md")
    return os.path.isfile(path)


def generate_topic_summary(
    *, out_dir: str, summit_title: str, content_type: str, theme_names: list[str],
    backend: str, api_key: str, model: str, api_base: str, reuse: bool = False, stop_flag=None,
) -> dict:
    """从已经跑完的大会/节目总结的主题分组里，挑出选中的一个或几个主题，把这些主题下的议题
    单独抽出来再生成一份聚焦总结（复用各议题已有的小结，不重新下载字幕/不重新调用逐议题小结），
    存到 out_dir/topics/ 下。选中多个主题时，两个主题下都出现的议题只算一次，一起生成一份总结。
    """
    manifest = _load_manifest(out_dir)
    manifest_entries: dict[str, dict] = manifest.get("entries", {})
    topic_groups = _get_or_backfill_topic_groups(out_dir, manifest)

    entry_ids: list[str] = []
    seen: set[str] = set()
    for name in theme_names:
        for eid in topic_groups.get(name, []):
            if eid not in seen:
                seen.add(eid)
                entry_ids.append(eid)
    return _compose_topic_summary(
        out_dir=out_dir, summit_title=summit_title, content_type=content_type,
        entry_ids=entry_ids, label="、".join(theme_names), manifest_entries=manifest_entries,
        backend=backend, api_key=api_key, model=model, api_base=api_base,
        not_found_error="选中的主题下没有找到任何已经生成成功的议题，请确认主题名没有写错",
        reuse=reuse, stop_flag=stop_flag,
    )


def generate_custom_topic_summary(
    *, out_dir: str, summit_title: str, content_type: str, entry_ids: list[str], label: str,
    backend: str, api_key: str, model: str, api_base: str, reuse: bool = False, stop_flag=None,
) -> dict:
    """跟 generate_topic_summary() 是同一件事的另一个入口：那边的议题是从大会/节目总结
    自动分出的"主题分组"里反查出来的，这边的 entry_ids 是用户直接在议题列表里手工勾出来的，
    不依赖、也不需要总结解析出"主题索引"这一节——没生成过总结、或者上次生成时模型没按
    格式输出主题索引的目录，一样能用这条路径手选几个感兴趣的议题单独出一份聚焦总结。
    """
    manifest = _load_manifest(out_dir)
    manifest_entries: dict[str, dict] = manifest.get("entries", {})
    label = label.strip()
    return _compose_topic_summary(
        out_dir=out_dir, summit_title=summit_title, content_type=content_type,
        entry_ids=entry_ids, label=label, manifest_entries=manifest_entries,
        backend=backend, api_key=api_key, model=model, api_base=api_base,
        not_found_error="勾选的议题里没有找到任何已经生成成功的，请确认是不是还没处理完或处理失败了",
        auto_title=not label, reuse=reuse, stop_flag=stop_flag,
    )


def _compose_topic_summary(
    *, out_dir: str, summit_title: str, content_type: str, entry_ids: list[str], label: str,
    manifest_entries: dict[str, dict], backend: str, api_key: str, model: str, api_base: str,
    not_found_error: str, auto_title: bool = False, reuse: bool = False, stop_flag=None,
) -> dict:
    """按主题分组、或手选议题生成聚焦总结的共用部分：给定一批 entry_ids 和一个标签，
    过滤出真正生成成功的议题、拼提示词、调用模型、渲染、存到 out_dir/topics/ 下。
    两个调用方只在"entry_ids 从哪来"和"label 怎么定"上不同，其余完全一样。
    """
    seen: set[str] = set()
    unique_ids = [eid for eid in entry_ids if not (eid in seen or seen.add(eid))]
    rows = [manifest_entries[eid] for eid in unique_ids
           if eid in manifest_entries and manifest_entries[eid].get("ok")]
    if not rows:
        raise SummarizeError(not_found_error)

    # 标题手填/主题名时，文件名在调用模型之前就能算出来；留空走自动概括标题的
    # 那条路本身算不出文件名，但如果这批 entry_ids 之前概括过一次，记录里能查到
    # 上次用的标题——两种情况选了"沿用已有的"、且文件确实存在，都直接读文件返回，
    # 完全不碰模型。查不到记录（这批议题是第一次选、或者上次没能顺利存下记录）
    # 就照常往下走生成，不能假装"没有旧总结"这件事本身能拦住生成。
    if reuse:
        recall_label = label if not auto_title else _remembered_custom_label(out_dir, entry_ids)
        if recall_label:
            fname = sanitize_filename(recall_label, 80) + ".md"
            path = os.path.join(out_dir, "topics", fname)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                return {"content": content, "relative_path": os.path.join("topics", fname), "count": len(rows)}

    unit = "期数" if content_type == "series" else "议题"
    topic_list = "\n".join(
        f"- {r['entry']['title']}"
        + (f"：{r['summary']['tldr']}" if r.get("summary") and r["summary"].get("tldr") else "")
        for r in rows
    )
    if auto_title:
        prompt = TOPIC_SUMMARY_AUTO_TITLE_PROMPT.format(
            summit_title=summit_title, unit=unit, count=len(rows), topic_list=topic_list,
        )
    else:
        prompt = TOPIC_SUMMARY_PROMPT.format(
            summit_title=summit_title, theme_name=label, unit=unit,
            count=len(rows), topic_list=topic_list,
        )
    body = _cached_summarize(
        prompt, backend, api_key=api_key, model=model, api_base=api_base,
        # 篇幅要求比之前提了 2-3 倍（8-12 段综述 + 15-25 条要点），8000 tokens 装不下，
        # 提到 20000——跟大会总结用的上限一样，也是 Anthropic SDK 非流式调用允许的
        # 单次输出上限（约 21000，再高就得改流式接口）。
        max_tokens=20000, timeout=600, cache_dir=llm_cache_dir(out_dir),
        # reuse=False 只会在用户手动选了"重新生成一遍"时才出现（默认/没有旧文件时
        # 都是 reuse=True）：这时选择和内容大概率跟上次一模一样，提示词逐字相同，
        # 不强制跳过缓存的话，用户点"重新生成"只会原样拿回旧文本，跟没点一样。
        force=not reuse, stop_flag=stop_flag,
    )
    if auto_title:
        label, body = _extract_auto_title(body, fallback=f"手选 {len(rows)} 个")

    lines = [
        f"# {label}", "",
        f"> 由 summit2md 从「{summit_title}」提取生成于 {time.strftime('%Y-%m-%d %H:%M')}，"
        f"涵盖 {len(rows)} 个{unit}", "",
        body.strip(), "",
        f"### 包含的{unit}", "",
    ]
    for r in rows:
        link = _row_link(r, from_subdir="topics")
        row_label = r["entry"]["title"]
        lines.append(f"- [{row_label}]({link})" if link else f"- {row_label}")
    content = "\n".join(lines).rstrip() + "\n"

    topics_dir = os.path.join(out_dir, "topics")
    os.makedirs(topics_dir, exist_ok=True)
    fname = sanitize_filename(label, 80) + ".md"
    path = os.path.join(topics_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if auto_title:
        _remember_custom_label(out_dir, entry_ids, label)
    return {"content": content, "relative_path": os.path.join("topics", fname), "count": len(rows)}


def list_manifest_entries(out_dir: str) -> list[dict]:
    """列出这个输出目录里 manifest 记录过的全部议题（不分主题、不去重成组），按已知顺序
    排好，供界面渲染成"手选议题"的勾选列表。跟 list_topic_groups() 不同：那个只列出模型
    自动分出来、且能在 manifest/README 里解析到的主题分组——没生成过总结、或者模型这次
    没按格式输出主题索引时会是空列表；这个只要 manifest 里有议题记录就总能列出来，跟
    有没有总结、总结解析得顺不顺利没关系。
    """
    manifest = _load_manifest(out_dir)
    entries = manifest.get("entries", {})
    rows = sorted(entries.items(), key=lambda kv: kv[1].get("rank", 0))
    return [
        {
            "id": eid,
            "title": row.get("entry", {}).get("title") or eid,
            "ok": bool(row.get("ok")),
        }
        for eid, row in rows
    ]


_MD_VIDEO_LINK_RE = re.compile(r"^-\s*(?:视频)?链接：(\S+)", re.MULTILINE)
_MD_TITLE_RE = re.compile(r"^#\s*(.+)")
_MD_FNAME_RANK_RE = re.compile(r"^(\d+)_")
_MD_DURATION_RE = re.compile(r"^-\s*时长：约\s*(\S+)", re.MULTILINE)
_MD_SUMMARY_SECTION_RE = re.compile(r"^## 议题小结\s*\n+(.*?)(?=\n## |\Z)", re.MULTILINE | re.DOTALL)
_MD_SUMMARY_TLDR_RE = re.compile(r"^\*\*(.+?)\*\*\s*$", re.MULTILINE)


def _parse_duration_display(text: str) -> int:
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _parse_summary_section(content: str) -> Optional[dict]:
    m = _MD_SUMMARY_SECTION_RE.search(content)
    if not m:
        return None
    section = m.group(1).strip()
    if not section:
        return None
    tldr_m = _MD_SUMMARY_TLDR_RE.match(section)
    if tldr_m:
        tldr = tldr_m.group(1).strip()
        body = section[tldr_m.end():].strip()
    else:
        tldr, body = "", section
    return {"tldr": tldr, "body": body}


def _bootstrap_manifest_from_disk(out_dir: str, manifest: dict) -> bool:
    """兼容"在引入 .manifest.json 之前就已经生成好"的旧输出：
    扫描 transcripts/ 和 speech/ 目录下、还没登记在 manifest 里的 .md 文件，
    从文件内容里的"视频链接"行解析出视频 id、从文件名前缀解析出编号，把它们
    补登记进 manifest。不这样做的话，续跑/按新顺序重新生成时会把这些其实已经
    生成好的旧文件当成"没处理过"，白白重新下载字幕、重新调用一遍 LLM。
    返回是否有改动（有改动调用方需要负责保存 manifest）。
    """
    manifest_entries = manifest["entries"]
    known_paths = {
        e.get(key) for e in manifest_entries.values() for key in ("relative_path", "speech_relative_path")
        if e.get(key)
    }
    changed = False
    for sub in ("transcripts", "speech"):
        d = os.path.join(out_dir, sub)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if not fname.endswith(".md"):
                continue
            rel = os.path.join(sub, fname)
            if rel in known_paths:
                continue
            try:
                with open(os.path.join(d, fname), encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            m = _MD_VIDEO_LINK_RE.search(content)
            if not m:
                continue
            vid = _extract_entry_id(m.group(1))
            if not vid:
                continue
            rank_m = _MD_FNAME_RANK_RE.match(fname)
            rank = int(rank_m.group(1)) if rank_m else None
            first_line = content.splitlines()[0] if content else ""
            title_m = _MD_TITLE_RE.match(first_line)
            title = title_m.group(1).strip() if title_m else os.path.splitext(fname)[0]
            dur_m = _MD_DURATION_RE.search(content)
            duration = _parse_duration_display(dur_m.group(1)) if dur_m else 0
            summary = _parse_summary_section(content)

            row = manifest_entries.get(vid)
            if row is None:
                row = {
                    "rank": rank,
                    "entry": {"id": vid, "title": title, "url": m.group(1), "duration": duration, "is_raw_session": False},
                    "ok": True, "error": None, "relative_path": None, "speech_relative_path": None,
                    "summary": summary, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                manifest_entries[vid] = row
            if row.get("rank") is None:
                row["rank"] = rank
            if not row["entry"].get("duration"):
                row["entry"]["duration"] = duration
            if not row.get("summary") and summary:
                row["summary"] = summary
            if sub == "transcripts" and not row.get("relative_path"):
                row["relative_path"] = rel
                row["ok"] = True
            elif sub == "speech" and not row.get("speech_relative_path"):
                row["speech_relative_path"] = rel
            known_paths.add(rel)
            changed = True
    return changed


def _repair_manifest_paths(out_dir: str, manifest: dict) -> tuple[bool, int]:
    """校验 manifest 里已登记议题的产物文件是否真的还在原地（比如手动改过文件名、
    或者改名/重排时中途出过问题）：
    - 文件不在原路径了，但能在别处按"视频链接"匹配到同一个视频的文件——修正路径指向真实文件；
    - 哪儿都找不到——判定内容已经丢失，把这条记录重置为"未处理"（清空 ok/小结/路径），
      下次跑的时候会被正常重新完整生成，而不是一直误报"已完成"却实际打不开文件。
    返回 (是否有改动, 被重置为待处理的议题数)。
    """
    manifest_entries = manifest["entries"]
    url_to_path: dict[tuple[str, str], str] = {}
    for sub in ("transcripts", "speech"):
        d = os.path.join(out_dir, sub)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if not fname.endswith(".md"):
                continue
            rel = os.path.join(sub, fname)
            try:
                with open(os.path.join(d, fname), encoding="utf-8") as f:
                    head = f.read(2000)
            except OSError:
                continue
            m = _MD_VIDEO_LINK_RE.search(head)
            if not m:
                continue
            vid = _extract_entry_id(m.group(1))
            url_to_path.setdefault((vid, sub), rel)

    changed = False
    reset_count = 0
    for vid, row in manifest_entries.items():
        for key, sub in (("relative_path", "transcripts"), ("speech_relative_path", "speech")):
            rel = row.get(key)
            if not rel or os.path.exists(os.path.join(out_dir, rel)):
                continue
            found = url_to_path.get((vid, sub))
            if found:
                row[key] = found
            else:
                row[key] = None
                if key == "relative_path":
                    row["ok"] = False
                    row["error"] = "产物文件缺失，已重置为待处理"
                    row["summary"] = None
                    reset_count += 1
            changed = True
    return changed, reset_count


def _prefer_original_manifest_paths(out_dir: str, manifest: dict) -> int:
    """旧版本在强制重跑时可能生成 ``xxx_2.md`` 并让 manifest 指向副本。

    如果同一个视频仍存在按稳定编号和标题生成的原始文件名，就把 manifest 路径纠正回
    原文件。这里只修正引用，不删除副本，避免擅自删除用户可能编辑过的文件。
    """
    changed = 0
    for row in manifest.get("entries", {}).values():
        entry = row.get("entry") or {}
        rank = row.get("rank")
        title = entry.get("title")
        if not rank or not title:
            continue
        canonical_name = f"{int(rank):03d}_{sanitize_filename(title, 80)}.md"
        for key, subdir in (("relative_path", "transcripts"), ("speech_relative_path", "speech")):
            canonical_rel = os.path.join(subdir, canonical_name)
            canonical_path = os.path.join(out_dir, canonical_rel)
            current_rel = row.get(key)
            if current_rel == canonical_rel or not os.path.exists(canonical_path):
                continue
            # 确认这个规范文件确实属于同一个视频，防止同名文件被误绑定。
            try:
                with open(canonical_path, encoding="utf-8") as f:
                    head = f.read(2000)
            except OSError:
                continue
            m = _MD_VIDEO_LINK_RE.search(head)
            canonical_vid = _extract_entry_id(m.group(1)) if m else None
            if canonical_vid != entry.get("id"):
                continue
            row[key] = canonical_rel
            changed += 1
    return changed


_FNAME_RANK_PREFIX_RE = re.compile(r"^\d+_(.+)$")


def _renumber_by_order(out_dir: str, manifest: dict, order_map: dict[str, float],
                        report: Callable[..., None]) -> dict[str, int]:
    """按 order_map（video_id -> 排序权重，通常来自"按议程重新排序"）统一重新编号：
    manifest 里已经生成好的议题，如果编号变了就把 transcripts/speech 里对应的文件改名，
    并同步修正这两个文件互相之间的相对链接；order_map 覆盖不到的议题保留原有相对顺序，
    排在后面。返回 {video_id: 新编号}，供本次运行里"还没处理过的新议题"直接使用，
    不用再各自去猜编号。
    """
    manifest_entries = manifest["entries"]
    all_ids = set(manifest_entries.keys()) | set(order_map.keys())

    def sort_key(vid: str):
        if vid in order_map:
            return (0, order_map[vid])
        old_rank = manifest_entries.get(vid, {}).get("rank")
        return (1, old_rank if old_rank is not None else float("inf"))

    ordered_ids = sorted(all_ids, key=sort_key)
    rank_map = {vid: i for i, vid in enumerate(ordered_ids, start=1)}

    renamed = 0
    path_changes: dict[str, str] = {}
    for vid, new_rank in rank_map.items():
        row = manifest_entries.get(vid)
        if not row:
            continue
        if row.get("rank") == new_rank:
            continue
        for key, subdir in (("relative_path", "transcripts"), ("speech_relative_path", "speech")):
            old_rel = row.get(key)
            if not old_rel:
                continue
            old_path = os.path.join(out_dir, old_rel)
            if not os.path.exists(old_path):
                continue
            m = _FNAME_RANK_PREFIX_RE.match(os.path.basename(old_rel))
            suffix = m.group(1) if m else os.path.basename(old_rel)
            new_rel = os.path.join(subdir, f"{new_rank:03d}_{suffix}")
            if new_rel == old_rel:
                continue
            new_path = os.path.join(out_dir, new_rel)
            if os.path.exists(new_path):
                continue  # 目标文件名已被占用，保守起见不覆盖，跳过这次改名
            os.rename(old_path, new_path)
            row[key] = new_rel
            path_changes[old_rel] = new_rel
            renamed += 1
        row["rank"] = new_rank

    if path_changes:
        # 改名之后，transcripts 和 speech 两边文档里彼此的相对链接（"../speech/xxx.md"
        # 这种）也要跟着修正，否则点进去会 404。
        for row in manifest_entries.values():
            for key in ("relative_path", "speech_relative_path"):
                rel = row.get(key)
                if not rel:
                    continue
                path = os.path.join(out_dir, rel)
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                new_content = content
                for old_rel, new_rel in path_changes.items():
                    new_content = new_content.replace(f"../{old_rel})", f"../{new_rel})")
                    # 链接的方括号显示文字用的是文件名本身（如 "[015_xxx.md](../speech/015_xxx.md)"），
                    # 光改 href 里的路径不够，显示文字里的旧文件名也要一并替换。
                    old_base, new_base = os.path.basename(old_rel), os.path.basename(new_rel)
                    if old_base != new_base:
                        new_content = new_content.replace(f"[{old_base}]", f"[{new_base}]")
                if new_content != content:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)

    if renamed:
        report(log=f"已按新顺序把 {renamed} 个已生成的文件重新编号", stage="renumber")
    _save_manifest(out_dir, manifest)
    return rank_map


def _read_summit_title_from_readme(out_dir: str) -> str:
    """从输出目录已有的总结文件里读回原始的会议/节目标题（第一个一级标题），
    读不到就退化用目录名——目录名本身就是 sanitize_filename() 处理过的安全版本，
    保证重新生成总结时标题不会丢。
    """
    readme_path = _existing_summary_path(out_dir)
    if readme_path:
        try:
            with open(readme_path, encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if m:
                return m.group(1).strip()
        except OSError:
            pass
    return os.path.basename(out_dir)


def rename_series_by_date(out_dir: str, content_type: Optional[str] = None,
                           report: Optional[Callable[..., None]] = None) -> dict:
    """把已经生成好的播客/访谈类节目文档，从编号命名（001_xxx.md）批量改成播出日期
    前缀命名（20260915_xxx.md）；纯本地操作——只改文件名、manifest、互相之间的链接、
    README，不重新下载字幕/正文，也不调用 LLM。
    - 已经是日期前缀的文件直接跳过，可以放心重复点。
    - manifest 里还没记录播出日期的 YouTube 议题（多是这个功能上线前生成的旧内容），
      用一次轻量元信息请求补一次日期（不下载字幕，比正常处理快得多）；单条拿不到
      （比如视频已下架/私有化）就跳过那一条，不影响其它议题。
    - Substack 议题按理发现阶段就带了发布日期，缺失的情况很少见，这里不额外重新
      请求，直接跳过并计入 skipped_no_date。
    """
    def _report(**kw):
        if report:
            report(**kw)

    out_dir = os.path.realpath(os.path.abspath(os.path.expanduser(out_dir)))
    manifest = _load_manifest(out_dir)
    manifest_entries: dict[str, dict] = manifest.get("entries") or {}
    if not manifest_entries:
        raise RuntimeError("这个目录里没有找到任何已生成的议题")

    effective_content_type = content_type or manifest.get("content_type") or "summit"
    # 顺手把这次确认过的节目类型存回 manifest——很多旧目录当初没存过这个字段，
    # 存一次以后重跑这个功能（或者以后导入这个目录）就不用每次都靠猜/靠调用方传参了。
    manifest["content_type"] = effective_content_type
    _save_manifest(out_dir, manifest)
    if effective_content_type != "series":
        return {
            "renamed": 0, "dates_backfilled": 0, "skipped_no_date": 0, "already_dated": 0,
            "skipped_not_series": True,
        }

    dates_backfilled = skipped_no_date = already_dated = renamed = 0
    path_changes: dict[str, str] = {}
    used_names: set[str] = set()

    rows = sorted(
        (r for r in manifest_entries.values() if r.get("ok")),
        key=lambda r: r.get("rank") or 0,
    )
    for row in rows:
        entry = row.get("entry") or {}
        title = entry.get("title") or "untitled"
        vid = entry.get("id")
        rel = row.get("relative_path")
        current_prefix = os.path.basename(rel).split("_", 1)[0] if rel else ""
        if rel and _PUBLISH_DATE_RE.match(current_prefix):
            # 已经是日期前缀命名了（比如之前点过这个功能，或者本来就是按新版本生成的）。
            already_dated += 1
            used_names.add(os.path.splitext(os.path.basename(rel))[0])
            continue

        publish_date = entry.get("publish_date")
        if not publish_date and entry.get("source_type") not in ("substack", "rss", "wechat", "article") and vid:
            _report(log=f"正在获取播出日期：{title}", stage="rename")
            try:
                ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True, "ignoreerrors": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    video_info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False) or {}
                publish_date = video_info.get("upload_date") or ""
            except Exception:  # noqa: BLE001
                publish_date = ""
            if publish_date:
                entry["publish_date"] = publish_date
                row["entry"] = entry
                dates_backfilled += 1

        if not publish_date:
            skipped_no_date += 1
            _report(log=f"  ⚠️ 拿不到播出日期，跳过改名：{title}")
            continue

        base_name = f"{publish_date}_{sanitize_filename(title, 80)}"
        new_name = base_name
        n = 2
        while new_name in used_names:
            new_name = f"{base_name}_{n}"
            n += 1
        used_names.add(new_name)

        for key, subdir in (("relative_path", "transcripts"), ("speech_relative_path", "speech")):
            old_rel = row.get(key)
            if not old_rel:
                continue
            old_path = os.path.join(out_dir, old_rel)
            if not os.path.exists(old_path):
                continue
            new_rel = os.path.join(subdir, new_name + ".md")
            new_path = os.path.join(out_dir, new_rel)
            if new_path != old_path:
                if os.path.exists(new_path):
                    continue  # 目标文件名已被占用，保守起见跳过这次改名
                os.rename(old_path, new_path)
                path_changes[old_rel] = new_rel
                row[key] = new_rel
                renamed += 1
            # 顺带把「播出时间」补进文档正文——这个功能上线前生成的旧文件没有这行。
            try:
                with open(new_path, encoding="utf-8") as f:
                    doc_content = f.read()
                new_doc_content = _ensure_publish_date_line(doc_content, publish_date)
                if new_doc_content != doc_content:
                    with open(new_path, "w", encoding="utf-8") as f:
                        f.write(new_doc_content)
            except OSError:
                pass

    if path_changes:
        # 改名之后，transcripts 和 speech 两边文档彼此的相对链接也要跟着修正。
        for row in manifest_entries.values():
            for key in ("relative_path", "speech_relative_path"):
                rel = row.get(key)
                if not rel:
                    continue
                p = os.path.join(out_dir, rel)
                if not os.path.exists(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        link_content = f.read()
                except OSError:
                    continue
                new_link_content = link_content
                for old_rel, new_rel in path_changes.items():
                    new_link_content = new_link_content.replace(f"../{old_rel})", f"../{new_rel})")
                    old_base, new_base = os.path.basename(old_rel), os.path.basename(new_rel)
                    if old_base != new_base:
                        new_link_content = new_link_content.replace(f"[{old_base}]", f"[{new_base}]")
                if new_link_content != link_content:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(new_link_content)

    _save_manifest(out_dir, manifest)

    # 重命名会改变 README 里每条议题的链接，重新生成一份索引；大会/节目总结内容本身不变。
    full_rows = sorted(manifest_entries.values(), key=lambda r: r.get("rank", 0))
    logo_relative_path = (
        "../logo.svg" if os.path.exists(os.path.join(os.path.dirname(out_dir), "logo.svg")) else None
    )
    index_content = render_index_md(
        _read_summit_title_from_readme(out_dir), manifest.get("source_url") or "",
        manifest.get("overall_summary"), full_rows, logo_relative_path,
        content_type=effective_content_type,
    )
    _write_summary(out_dir, index_content)

    if renamed or dates_backfilled:
        _report(log=f"已按播出日期重命名 {renamed} 个文件（补了 {dates_backfilled} 条缺失的播出日期）",
                stage="rename_done")
    return {
        "renamed": renamed, "dates_backfilled": dates_backfilled,
        "skipped_no_date": skipped_no_date, "already_dated": already_dated,
    }


_TRANSCRIPT_TS_LINE_RE = re.compile(r"^\*\*\[(\d+(?::\d+){1,2})\]\([^)]+\)\*\*\s*$")
_TRANSCRIPT_SPEAKER_HEADER_RE = re.compile(r"^### 🗣️ (.+)$")


def _parse_transcript_body(content: str) -> tuple[list[tuple[float, str]], Optional[list[str]], Optional[str]]:
    """从已经渲染好的 transcripts/xxx.md 文件内容里，把"## 文字记录"部分重新解析回
    (段落, 发言人列表, speaker_mode) ——这样"补生成演讲稿"这类只缺一部分产物的场景，
    可以直接复用已有文字记录，不用重新下载字幕、重新跑一遍发言人推测。
    """
    parts = content.split("## 文字记录", 1)
    if len(parts) < 2:
        return [], None, None
    lines = parts[1].splitlines()
    paragraphs: list[tuple[float, str]] = []
    speakers: list[Optional[str]] = []
    cur_speaker: Optional[str] = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = _TRANSCRIPT_SPEAKER_HEADER_RE.match(line)
        if m:
            cur_speaker = m.group(1).strip()
            i += 1
            continue
        m2 = _TRANSCRIPT_TS_LINE_RE.match(line)
        if m2:
            secs = 0
            for p in m2.group(1).split(":"):
                secs = secs * 60 + int(p)
            i += 1
            text = lines[i].strip() if i < n else ""
            paragraphs.append((float(secs), text))
            speakers.append(cur_speaker)
            i += 1
            continue
        i += 1
    if not paragraphs:
        # 文章类来源（RSS/公众号，或者 Substack 没有转写只有正文）渲染时不带
        # 时间戳标记行（见 render_transcript_md），这里按空行分段把正文原样读回来，
        # 而不是直接当成"旧格式文件解析不出段落"。
        plain_blocks = [b.strip() for b in "\n".join(lines).split("\n\n") if b.strip()]
        if plain_blocks:
            return [(0.0, b.replace("\n", " ")) for b in plain_blocks], None, None
        return [], None, None
    if all(s is not None and s == speakers[0] for s in speakers):
        return paragraphs, [speakers[0]] * len(paragraphs), "single"
    if any(s is not None for s in speakers):
        return paragraphs, [s or "未知发言人" for s in speakers], "multi"
    return paragraphs, None, None


TEXT_SOURCE_TYPES = ("rss", "wechat", "article")


@dataclass
class _Job:
    """process_job 一次运行里，各处理步骤共用的设置和状态。"""
    summit_title: str
    content_type: str
    out_dir: str
    cache_dir: str
    llm_cache: str
    backend: str
    api_key: str
    model: str
    api_base: str
    max_transcript_chars: int
    lang_prefs: list
    do_speaker_label: bool
    do_speech_script: bool
    speech_lang_mode: str
    role_context: str
    length_instruction: str
    stop_flag: Optional[Callable[[], bool]]
    report: Callable[..., None]
    used_names: set
    finalize_row: Callable[..., None]
    rows: list


def _entry_plan(entry: dict, existing: Optional[dict], out_dir: str, *, do_summary: bool,
                do_speech_script: bool, skip_existing: bool) -> tuple[str, bool, bool, bool]:
    """这一条这次该怎么处理：skip（已完整生成过）/ backfill（文字记录在，只补小结或演讲稿）/
    retry（此前失败过，重新完整处理）/ new。开头的概览统计和逐条处理用同一个判断。
    返回 (kind, 这条要不要小结, 要重试小结, 要补演讲稿)。"""
    have_transcript = bool(
        existing and existing.get("ok") and existing.get("relative_path")
        and os.path.exists(os.path.join(out_dir, existing["relative_path"]))
    )
    have_speech = bool(
        existing and existing.get("speech_relative_path")
        and os.path.exists(os.path.join(out_dir, existing["speech_relative_path"]))
    )
    have_real_summary = bool(
        existing and existing.get("summary") and not _is_failed_summary(existing["summary"])
    )
    entry_do_summary = do_summary and entry.get("want_summary", True)
    needs_summary_retry = entry_do_summary and have_transcript and not have_real_summary
    # 文章类来源本来就是书面文字，从来不生成演讲稿——不能因为"没有演讲稿"就每次都去补
    needs_speech_backfill = (do_speech_script and have_transcript and not have_speech
                             and entry.get("source_type") not in TEXT_SOURCE_TYPES)
    if skip_existing and have_transcript and not needs_summary_retry and not needs_speech_backfill:
        kind = "skip"
    elif skip_existing and have_transcript and (needs_summary_retry or needs_speech_backfill):
        kind = "backfill"
    elif existing and not existing.get("ok"):
        kind = "retry"
    else:
        kind = "new"
    return kind, entry_do_summary, needs_summary_retry, needs_speech_backfill


def _backfill_entry(job: "_Job", i: int, total: int, entry: dict, existing: dict, stable_rank: int,
                    needs_summary_retry: bool, needs_speech_backfill: bool) -> None:
    """文字记录已经有了，这次只补缺的那一步（小结失败重试 / 补生成演讲稿）：复用已有
    文字记录，不重新下载字幕。成功就把这一条重新记进 manifest；失败不影响已有内容。"""
    out_dir, summit_title, content_type = job.out_dir, job.summit_title, job.content_type
    backend, api_key, model, api_base = job.backend, job.api_key, job.model, job.api_base
    cache_dir, _llm_cache, stop_flag, report = job.cache_dir, job.llm_cache, job.stop_flag, job.report
    lang_prefs, max_transcript_chars = job.lang_prefs, job.max_transcript_chars
    do_speaker_label, do_speech_script, speech_lang_mode = job.do_speaker_label, job.do_speech_script, job.speech_lang_mode
    role_context, length_instruction = job.role_context, job.length_instruction
    title, vid = entry["title"], entry["id"]
    finalize_row, rows = job.finalize_row, job.rows
    # 文字记录已经有了，这次只是想补一部分产物（小结失败重试 / 补生成演讲稿）：
    # 复用已有内容，不重新下载字幕，只做真正缺的那一步。
    parts = [p for p, need in (("重试小结", needs_summary_retry), ("补生成演讲稿", needs_speech_backfill)) if need]
    report(log=f"[{i}/{total}] 只{'+'.join(parts)}（复用已有文字记录，跳过重新下载字幕）：{title}",
           stage="speech" if needs_speech_backfill else "summary", current=i, total=total)
    transcript_rel = existing["relative_path"]
    row = dict(existing, entry=entry)
    try:
        with open(os.path.join(out_dir, transcript_rel), encoding="utf-8") as f:
            transcript_content = f.read()
        paragraphs, speakers, speaker_mode = _parse_transcript_body(transcript_content)
        lang_m = re.search(r"字幕来源：YouTube 自动生成字幕（([^）]+)）", transcript_content)
        lang = lang_m.group(1) if lang_m else (lang_prefs[0] if lang_prefs else "en")
        if not paragraphs:
            raise SummarizeError("无法从已有文字记录解析出段落（可能是旧格式文件），跳过此议题")
        plain_text = "\n".join(t for _, t in paragraphs)
        row["truncated"] = bool(
            max_transcript_chars and len(plain_text) > max_transcript_chars
            and (needs_summary_retry or (do_speech_script and needs_speech_backfill))
        )
        if row["truncated"]:
            report(log=f"  ⚠️ 文字记录较长（{len(plain_text)} 字），只读取前 {max_transcript_chars} 字生成小结/演讲稿：{title}")

        summary = existing.get("summary")
        if needs_summary_retry and plain_text.strip():
            try:
                prompt = PER_TOPIC_PROMPT.format(
                    role_context=role_context,
                    title=title, duration=format_duration(entry.get("duration", 0)),
                    length_instruction=length_instruction,
                    transcript=_cap_transcript(plain_text, max_transcript_chars),
                )
                raw = _cached_summarize(prompt, backend, api_key=api_key, model=model,
                                        api_base=api_base, cache_dir=_llm_cache, stop_flag=stop_flag)
                summary = parse_topic_summary(raw)
                row["summary"] = summary
            except Stopped:
                raise
            except SummarizeError as e:
                report(log=f"  ⚠️ 小结重试仍然失败（{e}）")

        speech_rel = existing.get("speech_relative_path")
        # 已有演讲稿而本次只重试小结时，不应重新调用 LLM 生成整篇演讲稿；
        # 后面的分支会只替换演讲稿里的“小结”段落。
        make_speech = do_speech_script and plain_text.strip() and needs_speech_backfill
        if make_speech:
            speech_text, speech_mode_used = generate_speech_script(
                entry, paragraphs, speakers, speaker_mode, lang, speech_lang_mode,
                backend, api_key, model, api_base, max_transcript_chars,
                cache_dir=_llm_cache, stop_flag=stop_flag,
            )
            speech_rel = os.path.join("speech", os.path.basename(transcript_rel))
            speech_md = render_speech_md(
                entry, summit_title, speech_text, speaker_mode, speakers,
                summary=summary, transcript_relative_path=transcript_rel,
                speech_lang_mode=speech_mode_used, content_type=content_type,
            )
            with open(os.path.join(out_dir, speech_rel), "w", encoding="utf-8") as f:
                f.write(speech_md)
            row["speech_relative_path"] = speech_rel
        elif needs_summary_retry and speech_rel:
            # 演讲稿本来就有、这次没打算重新生成，但小结更新了：同步更新演讲稿里的小结部分
            speech_path = os.path.join(out_dir, speech_rel)
            if os.path.exists(speech_path):
                with open(speech_path, encoding="utf-8") as f:
                    speech_content = f.read()
                with open(speech_path, "w", encoding="utf-8") as f:
                    f.write(_replace_summary_section(speech_content, summary))

        # 小结出现在演讲稿里就不用在文字记录里重复；没有演讲稿时小结留在文字记录里兜底
        new_transcript_content = render_transcript_md(
            entry, summit_title, paragraphs, summary, lang,
            speakers=speakers, speaker_mode=speaker_mode,
            include_summary=not bool(speech_rel), speech_relative_path=speech_rel,
            content_type=content_type,
        )
        with open(os.path.join(out_dir, transcript_rel), "w", encoding="utf-8") as f:
            f.write(new_transcript_content)
        finalize_row(vid, stable_rank, entry, row)
        report(log=f"  ✅ 完成：{title}")
    except Stopped:
        raise
    except Exception as e:  # noqa: BLE001
        # 补生成失败不影响已经有的文字记录/小结，这一条照旧算"已有内容"
        report(log=f"  ⚠️ 处理失败（{e}），已有内容不受影响")
        rows.append(row)


def _process_entry(job: "_Job", i: int, total: int, entry: dict, existing: Optional[dict],
                   stable_rank: int, entry_do_summary: bool) -> dict:
    """完整处理一条：拿文字（字幕 / Substack 转写 / 文章正文）→ 小结 → 发言人 → 演讲稿 →
    写文件。返回要记进 manifest 的这一行；出错记在 row["error"] 里，停止信号往外抛。"""
    out_dir, summit_title, content_type = job.out_dir, job.summit_title, job.content_type
    backend, api_key, model, api_base = job.backend, job.api_key, job.model, job.api_base
    cache_dir, _llm_cache, stop_flag, report = job.cache_dir, job.llm_cache, job.stop_flag, job.report
    lang_prefs, max_transcript_chars = job.lang_prefs, job.max_transcript_chars
    do_speaker_label, do_speech_script, speech_lang_mode = job.do_speaker_label, job.do_speech_script, job.speech_lang_mode
    role_context, length_instruction = job.role_context, job.length_instruction
    title, vid = entry["title"], entry["id"]
    used_names = job.used_names
    is_substack_entry = entry.get("source_type") == "substack"
    is_text_source_entry = entry.get("source_type") in TEXT_SOURCE_TYPES
    _retry_note = ""
    if existing and not existing.get("ok"):
        _retry_note = f"（此前失败过：{existing.get('error') or '未知原因'}，现在重试）"
    _fetch_stage_log = "抓取文字稿" if (is_substack_entry or is_text_source_entry) else "下载字幕"
    report(log=f"[{i}/{total}] {_fetch_stage_log}：{title}{_retry_note}", stage="subtitle", current=i, total=total)
    # 强制重跑同一视频时先继承旧记录；成功后在原路径原位覆写，而不是另起 _2 文件。
    row = dict(existing) if existing else {}
    row.update(entry=entry, ok=False, error=None)
    row.setdefault("relative_path", None)
    row.setdefault("speech_relative_path", None)
    row.setdefault("summary", None)
    try:
        description = ""
        source_speakers, source_speaker_mode = None, None
        if is_substack_entry:
            sub = fetch_substack_transcript(entry, cache_dir)
            if not sub:
                row["error"] = "未能在该节目页面里找到完整对话文字稿（可能该节目没有公开转写）"
                report(log=f"  ⚠️ 无转写内容，跳过：{title}")
                return row
            lang = sub["lang"]
            paragraphs = sub["paragraphs"]
            source_speakers, source_speaker_mode = sub["speakers"], sub["speaker_mode"]
            if sub.get("youtube_id"):
                entry["youtube_url"] = f"https://www.youtube.com/watch?v={sub['youtube_id']}"
        elif is_text_source_entry:
            sub = sources.fetch_source_text(entry, cache_dir)
            if not sub:
                row["error"] = "未能获取到正文内容（可能是付费墙、需要登录，或者只有节目简介没有文字稿）"
                report(log=f"  ⚠️ 无正文内容，跳过：{title}")
                return row
            lang = sub["lang"]
            paragraphs = sub["paragraphs"]
            source_speakers, source_speaker_mode = sub["speakers"], sub["speaker_mode"]
        else:
            sub = download_subtitle(entry["id"], cache_dir, lang_prefs)
            if not sub:
                row["error"] = "未找到可用的自动字幕（该视频可能未生成字幕）"
                report(log=f"  ⚠️ 无字幕，跳过：{title}")
                return row
            lang, vtt_path, description = sub["lang"], sub["path"], sub["description"]
            if sub.get("upload_date"):
                entry["publish_date"] = sub["upload_date"]
            paragraphs = vtt_to_paragraphs(vtt_path)
        plain_text = "\n".join(t for _, t in paragraphs)
        row["truncated"] = bool(
            max_transcript_chars and len(plain_text) > max_transcript_chars
            and (entry_do_summary or do_speech_script)
        )
        if row["truncated"]:
            report(log=f"  ⚠️ 文字记录较长（{len(plain_text)} 字），只读取前 {max_transcript_chars} 字生成小结/演讲稿：{title}")

        summary = existing.get("summary") if existing else None
        if entry_do_summary and plain_text.strip():
            report(log=f"  正在生成议题小结：{title}", stage="summary", current=i, total=total)
            try:
                prompt = PER_TOPIC_PROMPT.format(
                    role_context=role_context,
                    title=title,
                    duration=format_duration(entry["duration"]),
                    length_instruction=length_instruction,
                    transcript=_cap_transcript(plain_text, max_transcript_chars),
                )
                raw = _cached_summarize(
                    prompt, backend, api_key=api_key, model=model, api_base=api_base,
                    cache_dir=_llm_cache, stop_flag=stop_flag,
                )
                summary = parse_topic_summary(raw)
            except Stopped:
                raise
            except SummarizeError as e:
                report(log=f"  ⚠️ 摘要失败（{e}），已保留文字记录")
                summary = {"tldr": "", "body": f"_（摘要生成失败：{e}）_"}

        speakers, speaker_mode = None, None
        single_speaker = None if is_text_source_entry else guess_single_speaker(title)
        if source_speakers:
            # 播客站点自己的转写已经标好了真实发言人，比标题解析/AI 推测都更准确，直接采用。
            speakers, speaker_mode = source_speakers, source_speaker_mode
        elif single_speaker:
            speaker_mode = "single"
            speakers = [single_speaker] * len(paragraphs)
        elif do_speaker_label and paragraphs and not is_text_source_entry:
            # 文章没有"发言人"这个概念，不用 AI 去猜——直接当正文平铺展示。
            report(log=f"  正在推测发言人：{title}", stage="speakers", current=i, total=total)
            try:
                labels = infer_speakers(
                    paragraphs, title, description, backend, api_key, model, api_base,
                    cache_dir=_llm_cache, stop_flag=stop_flag,
                )
                if labels:
                    speaker_mode = "multi"
                    speakers = [labels.get(idx, "未知发言人") for idx in range(1, len(paragraphs) + 1)]
            except Stopped:
                raise
            except SummarizeError as e:
                report(log=f"  ⚠️ 发言人推测失败（{e}），文字记录不受影响")

        if existing and existing.get("relative_path"):
            transcript_rel = existing["relative_path"]
            fname = os.path.splitext(os.path.basename(transcript_rel))[0]
            used_names.add(fname)
        else:
            if content_type == "series" and entry.get("publish_date"):
                # 播客/访谈类节目（栏目持续更新，不是某一场大会）用播出时间做文件名前缀，
                # 方便直接从文件名看出期数时间；大会类议题仍按处理顺序编号。
                base_name = f"{entry['publish_date']}_{sanitize_filename(title, 80)}"
            else:
                base_name = f"{stable_rank:03d}_{sanitize_filename(title, 80)}"
            fname = base_name
            n = 2
            while fname in used_names:
                fname = f"{base_name}_{n}"
                n += 1
            used_names.add(fname)
            transcript_rel = os.path.join("transcripts", fname + ".md")
        speech_rel = (
            existing.get("speech_relative_path")
            if existing and existing.get("speech_relative_path")
            else os.path.join("speech", os.path.basename(transcript_rel))
        )

        speech_text, speech_mode_used, speech_ok = None, speech_lang_mode, False
        # 演讲稿这道工序是把"口语转写"整理成书面表达——文章来源本来就是书面
        # 文字，不需要这道加工，直接展示原文正文即可。
        if do_speech_script and plain_text.strip() and not is_text_source_entry:
            report(log=f"  正在整理演讲稿：{title}", stage="speech", current=i, total=total)
            try:
                speech_text, speech_mode_used = generate_speech_script(
                    entry, paragraphs, speakers, speaker_mode, lang, speech_lang_mode,
                    backend, api_key, model, api_base, max_transcript_chars,
                    cache_dir=_llm_cache, stop_flag=stop_flag,
                )
                speech_ok = True
            except Stopped:
                raise
            except SummarizeError as e:
                report(log=f"  ⚠️ 演讲稿整理失败（{e}），文字记录不受影响")

        # speech 目录是主目录：生成成功时，议题小结放进演讲稿文档，
        # 原始文字记录只保留正文，避免内容重复；speech 生成失败/未开启时，
        # 小结留在文字记录里，不丢失内容。
        has_speech_after = bool(
            speech_ok
            or (existing and existing.get("speech_relative_path")
                and os.path.exists(os.path.join(out_dir, existing["speech_relative_path"])))
        )
        md_path = os.path.join(out_dir, transcript_rel)
        md_content = render_transcript_md(
            entry, summit_title, paragraphs, summary, lang, speakers=speakers, speaker_mode=speaker_mode,
            include_summary=not has_speech_after,
            speech_relative_path=speech_rel if has_speech_after else None,
            content_type=content_type,
        )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        row.update(ok=True, relative_path=transcript_rel, summary=summary)

        if speech_ok:
            speech_path = os.path.join(out_dir, speech_rel)
            speech_md = render_speech_md(
                entry, summit_title, speech_text, speaker_mode, speakers,
                summary=summary, transcript_relative_path=transcript_rel, speech_lang_mode=speech_mode_used,
                content_type=content_type,
            )
            with open(speech_path, "w", encoding="utf-8") as f:
                f.write(speech_md)
            row["speech_relative_path"] = speech_rel
        elif existing and existing.get("speech_relative_path"):
            # 本次没有要求重做演讲稿时，保留旧演讲稿及其 manifest 路径。
            row["speech_relative_path"] = existing["speech_relative_path"]

        report(log=f"  ✅ 完成：{title}")
    except Stopped:
        raise
    except Exception as e:  # noqa: BLE001
        row["error"] = str(e)
        report(log=f"  ❌ 处理出错：{title} — {e}")
        traceback.print_exc()
    return row


def _refresh_overall_summary(job: "_Job", manifest: dict, full_rows: list[dict], *, do_summary: bool,
                             regenerate_summary: bool, overall_model: str, was_stopped: bool) -> tuple[Optional[str], bool]:
    """按需重新生成大会/节目总结，返回 (总结, 是否已停止)。"""
    out_dir, summit_title, content_type = job.out_dir, job.summit_title, job.content_type
    backend, api_key, model, api_base = job.backend, job.api_key, job.model, job.api_base
    _llm_cache, stop_flag, report = job.llm_cache, job.stop_flag, job.report
    # 大会总结同理：这次运行如果没勾选生成摘要（比如只是想续跑剩下的议题），
    # 沿用上一次已经生成好的总结，不让 README 因为这次没重新生成而丢内容。
    # regenerate_summary=False 时（导入已经有总结的目录、用户选了"沿用已有总结"）
    # 同样沿用旧总结、不重新调用模型——但如果压根还没有旧总结可沿用，就算选了
    # "不重新生成"也还是要生成一次，不然这个目录会完全没有总结。
    overall_summary = manifest.get("overall_summary")
    # 上一次失败留下的占位文本（"_（大会总结生成失败：...）_"）不算"有总结可沿用"——
    # 不然选了"沿用已有总结"会把失败原因当成正经总结继续用，还提示"已经省了 token"，
    # 用户完全看不出上一次其实失败了。这里一律当成"没有总结"处理：不管
    # regenerate_summary 选没选，只要还没有一份真正生成成功的总结，就还是要重新生成。
    has_real_overall_summary = bool(overall_summary) and not _is_failed_overall_summary(overall_summary)
    want_new_overall_summary = (
        not was_stopped and do_summary and any(r["ok"] for r in full_rows)
        and (regenerate_summary or not has_real_overall_summary)
    )
    if not want_new_overall_summary and do_summary and has_real_overall_summary and not regenerate_summary:
        report(log="已沿用现有的大会总结，未重新生成（节省 token）", stage="overall_summary")
    if want_new_overall_summary:
        report(log="正在生成大会总结……", stage="overall_summary")
        # 生成失败（含返回空内容）时不能拿失败占位符去覆盖已经有的旧总结——那样一次
        # 偶发失败就会把之前好好的总结冲掉。只有压根没有旧总结可退回时才显示失败占位符；
        # 上一次本身就是失败占位符的话，也不当成"有旧总结"保留，避免占位符一直循环。
        previous_summary = overall_summary if has_real_overall_summary else None
        try:
            topic_list = "\n".join(
                f"- {r['entry']['title']}"
                + (f"：{r['summary']['tldr']}" if r.get("summary") and r["summary"].get("tldr") else "")
                for r in full_rows
                if r["ok"]
            )
            prompt_template = SERIES_PROMPT if content_type == "series" else SUMMIT_PROMPT
            prompt = prompt_template.format(
                summit_title=summit_title,
                count=sum(1 for r in full_rows if r["ok"]),
                topic_list=topic_list,
            )
            new_summary = _cached_summarize(
                prompt, backend, api_key=api_key, model=(overall_model or model), api_base=api_base,
                cache_dir=_llm_cache, stop_flag=stop_flag,
                # 议题数量多时（比如上百个）大会总结要点+主题索引里得把每个标题至少列两遍，
                # 篇幅很容易超过之前的 12000；20000 留了更多余量，同时仍在 Anthropic SDK
                # 非流式调用允许的单次输出上限内（超过约 21000 会要求改用流式接口）。
                max_tokens=20000, timeout=600,
            )
            overall_summary, topic_groups = _finalize_overall_summary(new_summary, full_rows)
            if topic_groups:
                # 解析不出结构化分组时保留上一次已经存好的分组，不因为这次格式没对上就清空。
                manifest["topic_groups"] = topic_groups
        except Stopped:
            was_stopped = True
            overall_summary = manifest.get("overall_summary")
            report(log="收到停止指令，大会总结没有重新生成（保留原有的）", stage="stopped")
        except SummarizeError as e:
            report(log=f"⚠️ 大会总结生成失败：{e}" + ("，已保留原有总结" if previous_summary else ""))
            overall_summary = previous_summary or f"_（大会总结生成失败：{e}）_"
        manifest["overall_summary"] = overall_summary
        _save_manifest(out_dir, manifest)
    return overall_summary, was_stopped


def process_job(
    *,
    summit_title: str,
    source_url: str,
    entries: list[dict],
    output_base_dir: str,
    backend: str,
    api_key: str,
    model: str,
    api_base: str = "",
    overall_model: str = "",
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    lang_prefs: list[str],
    do_summary: bool,
    regenerate_summary: bool = True,
    do_speaker_label: bool = False,
    do_speech_script: bool = False,
    speech_lang_mode: str = "bilingual",
    skip_existing: bool = True,
    agenda_order_map: Optional[dict[str, float]] = None,
    content_type: str = "summit",
    summary_length: str = "medium",
    stop_flag: Optional[Callable[[], bool]] = None,
    pause_flag: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[ProgressCB] = None,
) -> dict:
    def report(**kw):
        if progress_cb:
            progress_cb(kw)

    role_context = PER_TOPIC_ROLE_CONTEXT.get(content_type, PER_TOPIC_ROLE_CONTEXT["summit"])
    length_instruction = PER_TOPIC_LENGTH_INSTRUCTIONS.get(summary_length, PER_TOPIC_LENGTH_INSTRUCTIONS["medium"])

    safe_summit_name = sanitize_filename(summit_title)
    out_dir = os.path.join(output_base_dir, safe_summit_name)
    transcripts_dir = os.path.join(out_dir, "transcripts")
    speech_dir = os.path.join(out_dir, "speech")
    cache_dir = os.path.join(out_dir, ".cache", "subtitles")
    _llm_cache = llm_cache_dir(out_dir)
    os.makedirs(transcripts_dir, exist_ok=True)
    if do_speech_script:
        os.makedirs(speech_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    # 处理进度持久化在输出目录里的 .manifest.json，键是视频 id：
    # - 稳定编号（rank）一旦写过就不再改变，保证文件名在多次续跑之间保持一致；
    # - skip_existing 时，已经成功且产物文件仍在的议题会直接跳过，不重新下载/调用 LLM；
    # - 失败/未处理的议题会保留在 manifest 里，方便只挑失败项重跑；
    # - 大会总结和 README 汇总目录里全部议题（不只是这次运行处理的那些），保证分批多次跑
    #   出来的输出仍然是一份完整的索引，不会因为只跑了一部分就把之前的结果冲掉。
    manifest = _load_manifest(out_dir)
    manifest_entries: dict[str, dict] = manifest["entries"]
    # 记下这次跑用的节目类型和来源链接，供以后"导入已有目录重跑总结"、
    # "按播出时间重命名"这些不经过重新发现流程的操作直接读，不用重新猜。
    manifest["content_type"] = content_type
    manifest["source_url"] = source_url
    _save_manifest(out_dir, manifest)
    if _bootstrap_manifest_from_disk(out_dir, manifest):
        _save_manifest(out_dir, manifest)
        report(log=f"（已将输出目录里 {len(manifest_entries)} 个已存在的旧产物纳入续跑记录）", stage="bootstrap")

    repaired, reset_count = _repair_manifest_paths(out_dir, manifest)
    if repaired:
        _save_manifest(out_dir, manifest)
        if reset_count:
            report(log=f"⚠️ 发现 {reset_count} 个议题的产物文件已丢失，已重置为待处理，会重新完整生成",
                   stage="repair")

    restored_paths = _prefer_original_manifest_paths(out_dir, manifest)
    if restored_paths:
        _save_manifest(out_dir, manifest)
        report(
            log=f"（已把 {restored_paths} 个续跑产物路径恢复为原始文件名；后续会原位覆写，不再生成 _2 文件）",
            stage="repair",
        )

    # 如果这次带着"按议程重新排序"的完整顺序信息（覆盖的是浏览器里看到的全部议题，
    # 不只是这次勾选处理的），先把已经生成好的旧文件按新顺序重新编号、改名，
    # 再往下处理这次选中的议题——新议题也直接用这批统一算好的编号，不用另外分配。
    rank_map: dict[str, int] = {}
    if agenda_order_map:
        rank_map = _renumber_by_order(out_dir, manifest, agenda_order_map, report)

    used_names = {
        os.path.splitext(os.path.basename(r["relative_path"]))[0]
        for r in manifest_entries.values() if r.get("relative_path")
    }
    next_rank = max(
        [r.get("rank", 0) for r in manifest_entries.values()] + list(rank_map.values()), default=0
    ) + 1

    def finalize_row(vid: str, stable_rank: int, entry: dict, row: dict) -> None:
        manifest_entries[vid] = {
            "rank": stable_rank,
            "entry": {
                "id": entry.get("id"), "title": entry.get("title"), "url": entry.get("url"),
                "duration": entry.get("duration", 0), "is_raw_session": entry.get("is_raw_session", False),
                "source_type": entry.get("source_type"), "publish_date": entry.get("publish_date"),
            },
            "ok": row.get("ok", False),
            "error": row.get("error"),
            "relative_path": row.get("relative_path"),
            "speech_relative_path": row.get("speech_relative_path"),
            "summary": row.get("summary"),
            "truncated": row.get("truncated", False),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save_manifest(out_dir, manifest)
        rows.append(row)

    total = len(entries)
    rows: list[dict] = []
    job = _Job(
        summit_title=summit_title, content_type=content_type, out_dir=out_dir, cache_dir=cache_dir,
        llm_cache=_llm_cache, backend=backend, api_key=api_key, model=model, api_base=api_base,
        max_transcript_chars=max_transcript_chars, lang_prefs=lang_prefs,
        do_speaker_label=do_speaker_label, do_speech_script=do_speech_script,
        speech_lang_mode=speech_lang_mode, role_context=role_context,
        length_instruction=length_instruction, stop_flag=stop_flag, report=report,
        used_names=used_names, finalize_row=finalize_row, rows=rows,
    )
    was_stopped = False
    stopped_after = total

    # 跑之前先给一句话概览：这次运行里有多少是跳过、只补缺失部分、重试此前失败的、
    # 全新处理——避免把"重试之前失败的议题"误看成"已完成的内容又被重新处理了一遍"。
    counts = {"skip": 0, "backfill": 0, "retry": 0, "new": 0}
    for _e in entries:
        counts[_entry_plan(_e, manifest_entries.get(_e.get("id")), out_dir, do_summary=do_summary,
                           do_speech_script=do_speech_script, skip_existing=skip_existing)[0]] += 1
    skip_count, backfill_count = counts["skip"], counts["backfill"]
    retry_failed_count, new_count = counts["retry"], counts["new"]
    _overview = []
    if skip_count:
        _overview.append(f"{skip_count} 个跳过（已生成过）")
    if backfill_count:
        _overview.append(f"{backfill_count} 个只补缺失部分（小结/演讲稿）")
    if retry_failed_count:
        _overview.append(f"{retry_failed_count} 个重试此前失败的议题")
    if new_count:
        _overview.append(f"{new_count} 个全新处理")
    report(log=f"本次共选中 {total} 个议题：" + "，".join(_overview), stage="overview")

    for i, entry in enumerate(entries, start=1):
        paused_logged = False
        while pause_flag and pause_flag():
            if not paused_logged:
                report(log="⏸️ 已暂停，等待继续……", stage="paused")
                paused_logged = True
            time.sleep(0.5)
            if stop_flag and stop_flag():
                break
        if paused_logged and not (stop_flag and stop_flag()):
            report(log="▶️ 已继续", stage="subtitle")
        if stop_flag and stop_flag():
            was_stopped = True
            stopped_after = i - 1
            report(log=f"收到停止指令，已处理 {i - 1}/{total}", stage="stopped")
            break

        title = entry["title"]
        vid = entry["id"]
        existing = manifest_entries.get(vid)
        stable_rank = (
            existing["rank"] if existing and existing.get("rank")
            else rank_map.get(vid) or entry.get("rank") or entry.get("index")
        )
        if not stable_rank:
            stable_rank = next_rank
            next_rank += 1

        kind, entry_do_summary, needs_summary_retry, needs_speech_backfill = _entry_plan(
            entry, existing, out_dir, do_summary=do_summary, do_speech_script=do_speech_script,
            skip_existing=skip_existing)

        if kind == "skip":
            report(log=f"[{i}/{total}] ⏭️ 已生成过，跳过：{title}", stage="skip", current=i, total=total)
            rows.append(dict(existing, entry=entry))
            continue

        try:
            if kind == "backfill":
                _backfill_entry(job, i, total, entry, existing, stable_rank,
                                needs_summary_retry, needs_speech_backfill)
            else:
                finalize_row(vid, stable_rank, entry,
                             _process_entry(job, i, total, entry, existing, stable_rank, entry_do_summary))
        except Stopped:
            was_stopped = True
            stopped_after = i - 1
            report(log=f"收到停止指令，已处理 {i - 1}/{total}（正在处理的这一个没有保存）", stage="stopped")
            break

    # 停止信号可能在最后一个议题处理中到达，此时不会再进入下一轮循环检查。
    if not was_stopped and stop_flag and stop_flag():
        was_stopped = True
        stopped_after = len(entries)
        report(log=f"收到停止指令，已处理 {stopped_after}/{total}", stage="stopped")

    # README 汇总的是 manifest 里累计的全部议题（可能横跨好几次分批运行），
    # 不只是这次运行处理的那些，这样分批/续跑出来的输出仍然是一份完整索引。
    full_rows = sorted(manifest_entries.values(), key=lambda r: r.get("rank", 0))

    overall_summary, was_stopped = _refresh_overall_summary(
        job, manifest, full_rows, do_summary=do_summary, regenerate_summary=regenerate_summary,
        overall_model=overall_model, was_stopped=was_stopped)

    logo_relative_path = "../logo.svg" if os.path.exists(os.path.join(output_base_dir, "logo.svg")) else None
    index_content = render_index_md(
        summit_title, source_url, overall_summary, full_rows, logo_relative_path,
        content_type=content_type,
    )
    index_path = _write_summary(out_dir, index_content)

    failed = [r for r in rows if not r.get("ok")]
    unprocessed_entries = entries[stopped_after:] if was_stopped else []
    if was_stopped:
        report(log=f"任务已停止，输出目录：{out_dir}", stage="stopped")
    else:
        report(log=f"全部完成，输出目录：{out_dir}", stage="done")
    return {
        "output_dir": out_dir, "index_path": index_path, "rows": rows,
        "full_rows": full_rows, "failed_entries": [r["entry"] for r in failed],
        "stopped": was_stopped, "unprocessed_entries": unprocessed_entries,
    }
