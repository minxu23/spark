"""
非本地/非 YouTube 来源的发现 + 抓取：RSS/Atom 订阅、Apple Podcast（转发到它
背后真正的 RSS）、微信公众号单篇文章、任意网页文章，外加从一段自由文本里
批量提取链接的小工具（extract_urls）。

放进 core/ 而不是某个具体 app 目录下，是因为 summit2md（把这类来源整理成
带小结的文字记录）和 notes2insight（把这类来源导入成笔记库外的临时笔记）都
需要同一套抓取/解析逻辑——两边只是"抓到内容之后怎么用"不同，"怎么把内容
抓下来"应该只有一份实现。

对 summit2md/pipeline.py 暴露的两种统一形状：
  - fetch_xxx_playlist(url) -> {summit_title, playlist_id, entries, content_type}
  - fetch_source_text(entry, cache_dir) -> {paragraphs, speakers, speaker_mode, lang} | None
notes2insight 走的是更底层的 fetch_generic_article_entry / fetch_wechat_article_playlist /
fetch_rss_playlist 等函数，自己把结果拼成笔记，不需要上面这两种形状。
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import feedparser
from bs4 import BeautifulSoup

from core import atomic

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# --------------------------------------------------------------------------
# 从一段自由文本（聊天记录、笔记之类）里批量抠出链接——两边都要用：summit2md
# 拿去逐条解析成独立议题，notes2insight 拿去逐条导入成笔记。
# --------------------------------------------------------------------------

_URL_IN_TEXT_RE = re.compile(
    # URL 本身只会是 ASCII，中文文本紧贴在链接后面时没有空格分隔（"看这篇：https://x，还有…"），
    # 必须显式排除中文标点/汉字，不然会把后面一整句话也吞进链接里。
    r'https?://[^\s<>"\')\]　-〿＀-￯一-鿿㐀-䶿]+',
    re.IGNORECASE,
)
_URL_TRAILING_PUNCT_RE = re.compile(r'[.,;:!?、，。！？）】》"\'\)\]]+$')


def extract_urls(text: str) -> list[str]:
    """从一段自由文本里抠出全部链接，去重但保留首次出现的顺序。常见的中文/
    英文标点经常会紧贴在链接后面（"看这篇：https://xxx。"），这类尾部标点要
    剥掉，不然链接本身就是错的，请求肯定失败。
    """
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_IN_TEXT_RE.finditer(text or ""):
        u = _URL_TRAILING_PUNCT_RE.sub("", m.group(0))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


# 抠出来的正文太短（付费墙提示、"仅限登录查看"之类的空壳）就不当作有效内容，
# 避免把这种空壳存进缓存当成"抓到了"。RSS/公众号正文长度差异很大，门槛给得松一点
# （Substack 那边同类阈值是 300，这里保持独立，互不影响）。
_MIN_PLAIN_BODY_LEN = 120


def _stable_id(s: str) -> str:
    """RSS guid / 文章链接不一定适合直接当文件名（可能带斜杠、特殊字符、过长），
    统一换成一个短而稳定的 id——同一个 guid/链接每次都会得到相同的 id，重跑/补生成
    不会认成新条目。"""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# 单次下载的上限：文章/PDF/sitemap 都远小于这个数，超过的多半是链接指错了地方
# （视频、安装包），没必要整个读进内存。
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _check_fetchable(url: str) -> None:
    """要抓的链接很多不是用户亲手填的（feed 里的文章链接、robots.txt 里声明的
    sitemap），只放行公网上的 http/https：file:// 会把本机文件读进笔记、发给模型；
    本机回环和链路本地地址（云主机元数据之类）也不该被一条 feed 条目支使去访问。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise urllib.error.URLError(f"只支持 http/https 链接：{url}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise urllib.error.URLError(f"链接里没有主机名：{url}")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        raise urllib.error.URLError(f"解析不了主机名 {host}：{e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise urllib.error.URLError(f"不抓取本机/链路本地地址：{host}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跳转后的地址也要过一遍 _check_fetchable，不然公网页面一个 302 就能绕过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_fetchable(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirectHandler)


def _http_get(url: str, timeout: int = 20, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """GET 一个公网 http/https 链接。网络层面的各种失败（超时、连接被重置、读到一半
    断开、链接格式不对）统一包成 urllib.error.URLError——调用方只需要接这一种
    （HTTPError 是它的子类，照旧能按状态码区分）。"""
    _check_fetchable(url)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.URLError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise urllib.error.URLError(f"{type(e).__name__}: {e}") from e
    if len(data) > max_bytes:
        raise urllib.error.URLError(f"内容超过 {max_bytes // (1024 * 1024)} MB，不下载")
    return data


def _guess_lang(text: str) -> str:
    """没有明确语言标注时的兜底：中文字符占比过半就当中文，否则当英文——
    只用来决定文字记录里"字幕来源"那行怎么措辞，不影响实际内容。"""
    if not text:
        return "en"
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return "zh" if cjk / max(len(text), 1) > 0.15 else "en"


def _html_to_paragraphs(body_html: str) -> list[tuple[float, str]]:
    """把一段正文 HTML 按段落抠成 paragraphs。这些来源没有真实的语音时间戳，
    统一用 0 占位——下游生成的"跳转到这段"链接实际就是跳到原文/节目页开头。"""
    soup = BeautifulSoup(body_html, "lxml")
    paragraphs: list[tuple[float, str]] = []
    for tag in soup.find_all(["p", "h1", "h2", "h3", "h4"]):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if tag.name in ("h1", "h2", "h3", "h4"):
            text = f"**{text}**"
        paragraphs.append((0.0, text))
    if not paragraphs:
        # RSS 的 description/summary 经常就是没有任何标签的纯文本（不是完整
        # HTML 正文），上面按 <p>/<hN> 抠段落的逻辑会一段都抠不到——退一步把
        # 整段纯文本当成一个段落，好过直接判定"没有正文"。
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        if text:
            paragraphs.append((0.0, text))
    return paragraphs


def _substantial_paragraphs(body_html: str) -> Optional[list[tuple[float, str]]]:
    paragraphs = _html_to_paragraphs(body_html)
    if not paragraphs or sum(len(t) for _, t in paragraphs) < _MIN_PLAIN_BODY_LEN:
        return None
    return paragraphs


# 很多网站的"相关文章/猜你喜欢"这类推荐区块，是直接嵌在 <article> 标签内部
# 的（不是页面级侧边栏），选正文容器时会被一起选进来——读起来像是这篇文章
# 自己写的内容，连标题、简介一起被当成"正文"喂给摘要模型，实际上跟这篇
# 文章毫无关系（是网站自己推荐的其它文章）。
_RELATED_CONTENT_HEADING_RE = re.compile(
    r"^(related( (content|articles|posts|reading|stories))?|"
    r"you (might|may) also like|more (from|stories|articles|news)|"
    r"read (more|next)|keep reading|up next|"
    r"相关(文章|阅读|推荐|内容|新闻)|猜你喜欢|延伸阅读|推荐阅读|你可能还喜欢)$",
    re.IGNORECASE,
)


def _strip_related_content_sections(container) -> None:
    """按常见的推荐区块标题文字（"Related content"/"猜你喜欢"之类）找到这类
    区块，把标题所在的最近语义容器（section/aside，找不到就用标题的直接父
    元素）整块摘掉，剩下内容原地保留、顺序不变。"""
    for heading in container.find_all(["h1", "h2", "h3", "h4"]):
        text = heading.get_text(strip=True)
        if not text or not _RELATED_CONTENT_HEADING_RE.match(text):
            continue
        section = heading.find_parent(["section", "aside"])
        target = section if (section is not None and section is not container) else heading.parent
        if target is not None and target is not container:
            target.decompose()


def _select_article_container(soup: BeautifulSoup):
    """网页正文的容器选择，两处调用（fetch_generic_article_entry 和
    _fetch_generic_article_paragraphs）共用同一份逻辑——挑到 <article> 之后，
    顺手把"相关文章"这类混进来的推荐区块摘掉，不然两边都得各自处理一遍。"""
    article = soup.select_one("article") or soup.body
    if article is not None:
        _strip_related_content_sections(article)
    return article


# --------------------------------------------------------------------------
# RSS / Atom
# --------------------------------------------------------------------------


def is_rss_url(url: str) -> bool:
    """启发式判断链接本身像不像 RSS/Atom 源（而不是网站首页）。这里宁可漏判
    也不要误判——误判会导致 fetch_playlist 走错分支、给出误导性的报错。真正
    权威的判断是 fetch_rss_playlist 里 feedparser 解析后有没有 entries。"""
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith((".xml", ".rss")) or path.endswith(("/rss", "/feed", "/atom", "/rss/", "/feed/"))


RSS_TIMEOUT = 20


def fetch_rss_playlist(url: str) -> dict:
    """RSS/Atom 订阅源发现：一次性解析出全部条目。"""
    # 自己下载再交给 feedparser 解析，而不是 feedparser.parse(url)——后者没有超时，
    # 一个卡住的源能让「检查全部订阅」一直挂着。
    try:
        raw = _http_get(url, timeout=RSS_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"无法打开该地址（HTTP {e.code}），确认链接本身就是 RSS/Atom 源，而不是网站首页/播客页面"
        ) from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"无法打开该地址（{getattr(e, 'reason', e)}）") from e
    feed = feedparser.parse(raw)
    if not feed.entries:
        if getattr(feed, "bozo", 0):
            raise RuntimeError(f"无法解析该 RSS/Atom 地址：{feed.get('bozo_exception') or '格式无法识别'}")
        raise RuntimeError("这个 RSS/Atom 地址里没有找到任何条目")

    channel_title = (feed.feed.get("title") or "").strip() or "Untitled Feed"
    feed_lang = (feed.feed.get("language") or "").strip()[:2] or None

    entries = []
    for idx, e in enumerate(feed.entries, start=1):
        link = e.get("link") or ""
        guid = e.get("id") or link
        if not guid:
            continue
        title = (e.get("title") or f"Untitled-{idx}").strip()
        entry = {
            "index": idx,
            "id": _stable_id(guid),
            "title": title,
            "duration": 0,
            "url": link or guid,
            "source_type": "rss",
            "is_raw_session": False,
            "rss_content_html": _rss_content_html(e),
            "rss_lang": feed_lang,
        }
        publish_date = _rss_publish_date(e)
        if publish_date:
            entry["publish_date"] = publish_date
        entries.append(entry)

    return {
        "summit_title": channel_title,
        "playlist_id": _stable_id(url),
        "entries": entries,
        "content_type": "series",
    }


def _rss_publish_date(e) -> Optional[str]:
    parsed = e.get("published_parsed") or e.get("updated_parsed")
    if not parsed:
        return None
    return time.strftime("%Y%m%d", parsed)


def _rss_content_html(e) -> str:
    # 很多 feed 会把全文塞进 content:encoded（feedparser 解析成 entry.content），
    # 没有就退到 summary/description——podcast 类 feed 这里往往只有几句话的节目简介，
    # 而不是完整转写，fetch_source_text 会据此判断要不要再抓一次文章原页面兜底。
    content_list = e.get("content")
    if content_list:
        parts = [c.get("value", "") for c in content_list if c.get("value")]
        if parts:
            return "\n".join(parts)
    return e.get("summary") or e.get("description") or ""


# --------------------------------------------------------------------------
# Apple Podcast：本身不是独立的内容源，只是播客 RSS 的一个目录/播放器——
# 用 iTunes 公开、不需要鉴权的 Lookup 接口把网址换成真正的 feed 地址，
# 换到之后剩下的处理跟普通播客 RSS 完全一样，不用再单独实现一套。
# --------------------------------------------------------------------------

_APPLE_PODCAST_ID_RE = re.compile(r"/id(\d+)")


def is_apple_podcast_url(url: str) -> bool:
    return "podcasts.apple.com" in urllib.parse.urlparse(url if "://" in url else f"https://{url}").netloc


def fetch_apple_podcast_playlist(url: str) -> dict:
    m = _APPLE_PODCAST_ID_RE.search(url)
    if not m:
        raise RuntimeError("无法从这个 Apple Podcast 链接里识别出节目 id（形如 .../id1234567890）")
    podcast_id = m.group(1)
    lookup_url = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"
    try:
        data = json.loads(_http_get(lookup_url))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
        raise RuntimeError(f"查询 Apple Podcast 节目信息失败：{e}") from e
    results = data.get("results") or []
    if not results or not results[0].get("feedUrl"):
        raise RuntimeError("这个 Apple Podcast 节目没有提供公开的 RSS 地址，无法导入")
    feed_url = results[0]["feedUrl"]
    playlist = fetch_rss_playlist(feed_url)
    playlist["summit_title"] = results[0].get("collectionName") or playlist["summit_title"]
    return playlist


# --------------------------------------------------------------------------
# 微信公众号：没有官方的"按账号批量取文章列表"接口，只支持贴具体某一篇文章的
# 链接——文章页本身是公开可访问的静态 HTML，不需要登录，正文固定在 #js_content
# 容器里。批量订阅某个账号不在这个函数的能力范围内。
# --------------------------------------------------------------------------


def is_wechat_article_url(url: str) -> bool:
    return "mp.weixin.qq.com/s" in url


_WECHAT_CREATE_TIME_RE = re.compile(r"var\s+oriCreateTime\s*=\s*['\"]?(\d+)['\"]?")


def fetch_wechat_article_playlist(url: str) -> dict:
    """单篇导入：返回结构跟其它来源一样，只是 entries 里永远只有一条。"""
    try:
        raw = _http_get(url)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"无法打开这篇公众号文章（{e}），确认链接完整、未过期，且不需要登录即可查看") from e
    html_text = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "lxml")

    content_node = soup.select_one("#js_content")
    if content_node is None:
        raise RuntimeError("没能在页面里找到正文内容，可能链接已失效、被删除，或需要登录才能查看")

    title_node = soup.select_one("#activity-name") or soup.select_one("h1")
    title = title_node.get_text(strip=True) if title_node else "Untitled"
    account_node = soup.select_one("#js_name")
    account_name = account_node.get_text(strip=True) if account_node else "微信公众号"

    entry = {
        "index": 1,
        "id": _stable_id(url),
        "title": title,
        "duration": 0,
        "url": url,
        "source_type": "wechat",
        "is_raw_session": False,
        "wechat_content_html": str(content_node),
    }
    m = _WECHAT_CREATE_TIME_RE.search(html_text)
    if m:
        entry["publish_date"] = time.strftime("%Y%m%d", time.localtime(int(m.group(1))))

    return {
        "summit_title": account_name,
        "playlist_id": _stable_id(url),
        "entries": [entry],
        "content_type": "series",
    }


# --------------------------------------------------------------------------
# 统一的正文抓取入口：rss/wechat 这类"发现阶段已经拿到、或者能直接拿到全文"的
# 来源，走这里而不是 YouTube 那条下载字幕的路径。
# --------------------------------------------------------------------------


def fetch_source_text(entry: dict, cache_dir: str) -> Optional[dict]:
    """返回 {"paragraphs", "speakers", "speaker_mode", "lang"}；抓不到正文返回 None。
    结果按 id 缓存到本地 json，重跑/补生成时不用再请求一遍。
    """
    source_type = entry.get("source_type")
    entry_meta: dict = {}
    cache_id = entry.get("id") or _stable_id(entry.get("url") or "")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{cache_id}.{source_type}.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, dict) and cached.get("paragraphs"):
            cached["paragraphs"] = [tuple(p) for p in cached["paragraphs"]]
            # 当初处理时从页面里取到的真实标题/日期：轻量检查给的只是从链接推出来的临时值
            for key, value in (cached.pop("entry_meta", None) or {}).items():
                if value:
                    entry[key] = value
            return cached
    except (OSError, ValueError):
        pass  # 没有缓存，或者缓存写到一半坏了——当没缓存，重新抓

    if source_type == "rss":
        paragraphs = _substantial_paragraphs(entry.get("rss_content_html") or "")
        if paragraphs is None and entry.get("url"):
            # feed 里往往只有摘要（尤其是播客类 feed），全文/详细说明在文章自己的
            # 页面——退一步直接抓文章页正文兜底。
            paragraphs = _fetch_generic_article_paragraphs(entry["url"])
        lang = entry.get("rss_lang") or _guess_lang(" ".join(t for _, t in (paragraphs or [])))
    elif source_type == "wechat":
        paragraphs = _substantial_paragraphs(entry.get("wechat_content_html") or "")
        lang = "zh"
    elif source_type == "article":
        if not entry.get("article_text") and not entry.get("article_content_html") and entry.get("url"):
            # 轻量检查（sitemap 只列链接）拿到的条目还没抓正文，处理时才去抓——
            # 顺带用页面里的真实标题/日期替换掉从链接推出来的临时值。
            try:
                fetched = fetch_generic_article_entry(entry["url"])
            except RuntimeError:
                return None
            for key in ("article_text", "article_content_html", "title", "publish_date"):
                if fetched.get(key):
                    entry[key] = fetched[key]
            entry_meta = {k: fetched.get(k) for k in ("title", "publish_date") if fetched.get(k)}
        if entry.get("article_text"):
            # PDF 抽出来的是纯文本，不是 HTML，没有标签可供 _substantial_paragraphs
            # 那套按 <p>/<hN> 抠段落的逻辑用——按空行分段，跟 render_transcript_md
            # 那边"没有真实时间戳的纯文本"走的是同一种呈现方式。
            paragraphs = [
                (0.0, re.sub(r"\s+", " ", p).strip())
                for p in re.split(r"\n\s*\n", entry["article_text"]) if p.strip()
            ]
            if not paragraphs or sum(len(t) for _, t in paragraphs) < _MIN_PLAIN_BODY_LEN:
                paragraphs = None
        else:
            paragraphs = _substantial_paragraphs(entry.get("article_content_html") or "")
        lang = _guess_lang(" ".join(t for _, t in (paragraphs or [])))
    else:
        return None

    if not paragraphs:
        return None

    result = {"paragraphs": paragraphs, "speakers": None, "speaker_mode": None, "lang": lang}
    atomic.write_json(cache_path, {**result, "entry_meta": entry_meta} if entry_meta else result)
    return result


def _fetch_generic_article_paragraphs(url: str) -> Optional[list[tuple[float, str]]]:
    try:
        raw = _http_get(url)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    if _looks_like_pdf(url, raw):
        try:
            text = _extract_pdf_text(raw)
        except RuntimeError:
            return None
        paragraphs = [(0.0, re.sub(r"\s+", " ", p).strip())
                      for p in re.split(r"\n\s*\n", text) if p.strip()]
        return paragraphs or None
    html_text = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "lxml")
    article = _select_article_container(soup)
    if article is None:
        return None
    return _substantial_paragraphs(str(article))


# --------------------------------------------------------------------------
# 链接直接指向一份 PDF（而不是网页）：不少论文/报告/白皮书就是裸的 .pdf 链接，
# 跟网页文章共用同一个入口（fetch_generic_article_entry），只是正文抽取方式
# 换成 pypdf，不走 HTML 解析。
# --------------------------------------------------------------------------


def _looks_like_pdf(url: str, data: bytes) -> bool:
    if url.split("?", 1)[0].lower().endswith(".pdf"):
        return True
    return data[:5] == b"%PDF-"


def _extract_pdf_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError as e:
        raise RuntimeError("未安装 pypdf（pip install pypdf），无法解析 PDF") from e
    import io
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:  # pypdf 对损坏/加密 PDF 的异常类型不固定，统一包装
        raise RuntimeError(f"PDF 解析失败：{e}") from e
    text = "\n\n".join(p for p in pages if p)
    if not text.strip():
        # 常见于扫描版 PDF（整页是图片，没有可提取的文字层）；不是代码的错，
        # 但必须明确说出来，不能悄悄生成一篇空笔记/空文字记录。
        raise RuntimeError("这份 PDF 提取不出文字——大概率是扫描件/图片版，没有文字层")
    return text


def _guess_pdf_title(data: bytes, url: str) -> str:
    try:
        import pypdf
        import io
        meta = pypdf.PdfReader(io.BytesIO(data)).metadata
        if meta and meta.title and meta.title.strip():
            return meta.title.strip()
    except Exception:  # noqa: BLE001
        pass
    # 退到用链接里的文件名——只有链接本身真的以 .pdf 结尾时才去掉这个后缀；
    # 像 arxiv.org/pdf/1706.03762 这种论文 id 里本身带点号，用 os.path.splitext
    # 会把 "1706.03762" 错切成 "1706"，看着像被截断了。
    name = os.path.basename(urllib.parse.urlparse(url).path)
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = name.replace("_", " ").replace("-", " ").strip()
    return name or url


# --------------------------------------------------------------------------
# 任意单篇网页文章：给"从剪贴板批量提取链接"这类场景用——链接本身既不是
# YouTube/Substack/公众号，也不属于哪个订阅源，就是一篇普通网页文章，
# 尽量抠出标题、正文、发布时间，当一条独立的议题处理。
# --------------------------------------------------------------------------

_ARTICLE_DATE_META_NAMES = (
    "article:published_time", "og:article:published_time",
    "article:modified_time", "date", "pubdate", "publishdate",
)


def _guess_article_publish_date(soup: BeautifulSoup) -> Optional[str]:
    for name in _ARTICLE_DATE_META_NAMES:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", tag["content"].strip())
            if m:
                return "".join(m.groups())
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", time_tag["datetime"].strip())
        if m:
            return "".join(m.groups())
    return None


def fetch_generic_article_entry(url: str) -> dict:
    """把任意一个网页文章链接解析成一条议题（不是一份列表）。抠不出正文/链接
    打不开就抛 RuntimeError，调用方据此把这条链接标记为"跳过"。"""
    try:
        raw = _http_get(url)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"打不开这个链接（{e}）") from e

    if _looks_like_pdf(url, raw):
        text = _extract_pdf_text(raw)
        entry = {
            "index": 1,
            "id": _stable_id(url),
            "title": _guess_pdf_title(raw, url),
            "duration": 0,
            "url": url,
            "source_type": "article",
            "is_raw_session": False,
            "article_text": text,
        }
        return entry

    html_text = raw.decode("utf-8", errors="replace")
    # is_rss_url() 只是个基于 URL 形态的启发式，逮不住 feeds.xxx.com/xxx 这类看不出
    # 后缀的订阅源地址——这类链接如果漏网走到这里，用 HTML 解析器硬啃一遍 XML，
    # 会把整个 feed 当成"一大段正文"存下来，标题也会变成 feed 本身的频道名。
    # 这里直接嗅探内容开头，是 XML/RSS/Atom 就报错，让调用方按"跳过"处理。
    if re.match(r"\s*(<\?xml|<rss\b|<feed\b)", html_text, re.IGNORECASE):
        raise RuntimeError("这个链接返回的是 RSS/Atom 订阅源内容，不是单篇文章")
    soup = BeautifulSoup(html_text, "lxml")

    article = _select_article_container(soup)
    if article is None or not _substantial_paragraphs(str(article)):
        raise RuntimeError("没能在页面里抠出足够的正文内容")

    title_node = soup.select_one("h1") or soup.title
    title = title_node.get_text(strip=True) if title_node else url

    entry = {
        "index": 1,
        "id": _stable_id(url),
        "title": title or "Untitled",
        "duration": 0,
        "url": url,
        "source_type": "article",
        "is_raw_session": False,
        "article_content_html": str(article),
    }
    publish_date = _guess_article_publish_date(soup)
    if publish_date:
        entry["publish_date"] = publish_date
    return entry


# --------------------------------------------------------------------------
# 没有 RSS 的网站（比如资讯列表页）：绝大多数现代网站不管是不是博客，都会为
# 搜索引擎生成一份 sitemap.xml——跟 RSS 是两回事（没有摘要，不一定带更新
# 时间），但列出了"这个站点有哪些页面"，拿它模拟"这个源现在有什么"，配合
# 已有的"新增 id 跟 manifest 比对"那套逻辑，就能像跟 RSS 一样跟踪一个没有
# RSS 的网站。只在 fetch_playlist() 试过 Substack、RSS 都失败之后才会走到
# 这里（见 pipeline.fetch_playlist），不用担心"其实有 RSS 只是没试对"。
# --------------------------------------------------------------------------

# 取最近这些 + 每条都要请求正文，两头都要控制成本——sitemap 常常有几百上千个
# 页面，不加这个上限，添加一个订阅就要发几百个请求。
SITEMAP_MAX_ENTRIES = 20


def _discover_sitemap_url(seed_url: str, prefetched: Optional[dict] = None) -> Optional[str]:
    """从 robots.txt 里找 Sitemap: 声明（标准做法，比瞎猜路径准），找不到再退
    回几个最常见的固定路径。试固定路径时已经把整份 sitemap 下载下来了，传了
    prefetched 就顺手存进去，后面解析时不用再下载一遍。"""
    parsed = urllib.parse.urlparse(seed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        robots = _http_get(f"{origin}/robots.txt", timeout=10).decode("utf-8", errors="replace")
        m = re.search(r"(?im)^Sitemap:\s*(\S+)", robots)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        candidate = origin + path
        try:
            data = _http_get(candidate, timeout=10)
            if prefetched is not None:
                prefetched[candidate] = data
            return candidate
        except Exception:
            continue
    return None


def _parse_sitemap_xml(data: bytes) -> tuple[list[str], list[tuple[str, Optional[str]]]]:
    """解析一份 sitemap XML：<sitemapindex> 返回子 sitemap 的链接列表，<urlset>
    返回 (url, lastmod) 列表——两种情况互斥，用不上的那个返回空列表。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [], []
    ns = {"sm": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    tag = root.tag.rsplit("}", 1)[-1]

    def _find(elem, name):
        return elem.find(f"sm:{name}", ns) if ns else elem.find(name)

    if tag == "sitemapindex":
        children = []
        for sm in (root.findall("sm:sitemap", ns) if ns else root.findall("sitemap")):
            loc = _find(sm, "loc")
            if loc is not None and loc.text:
                children.append(loc.text.strip())
        return children, []

    urls = []
    for u in (root.findall("sm:url", ns) if ns else root.findall("url")):
        loc = _find(u, "loc")
        if loc is None or not loc.text:
            continue
        lastmod_el = _find(u, "lastmod")
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None
        urls.append((loc.text.strip(), lastmod))
    return [], urls


def _collect_sitemap_urls(sitemap_url: str, path_prefix: str, allow_children: bool = True,
                          prefetched: Optional[dict] = None) -> list[tuple[str, Optional[str]]]:
    """把一份（可能是索引式的）sitemap 拉平成 (url, lastmod) 列表，只保留路径
    以 path_prefix 开头的页面。子 sitemap 只递归这一层——大部分站点最多两层，
    再深一层容易演变成没有边界地抓遍整个站点的一堆 sitemap 文件。"""
    data = (prefetched or {}).get(sitemap_url)
    if data is None:
        try:
            data = _http_get(sitemap_url, timeout=20)
        except Exception:
            return []
    children, urls = _parse_sitemap_xml(data)
    if children:
        if not allow_children:
            return []
        # 子 sitemap 的文件名/路径经常带类别提示（sitemap-news.xml 之类），
        # 名字里含 path_prefix 关键字的优先抓，减少浪费在不相关子 sitemap 上的请求。
        hint = path_prefix.strip("/").split("/")[0] if path_prefix.strip("/") else ""
        if hint:
            children = sorted(children, key=lambda c: hint.lower() not in c.lower())
        merged: list[tuple[str, Optional[str]]] = []
        for child in children[:8]:
            merged.extend(_collect_sitemap_urls(child, path_prefix, allow_children=False))
        return merged
    return [(u, lastmod) for u, lastmod in urls if urllib.parse.urlparse(u).path.startswith(path_prefix)]


def _sitemap_lastmod_ts(lastmod: str) -> Optional[float]:
    try:
        s = lastmod.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _sitemap_lastmod_to_date(lastmod: str) -> Optional[str]:
    # 按网站自己写的日期取，不做时区换算：只写了日期的 "2026-09-18" 会被当成本地
    # 零点，换算到 UTC 后在东半球就变成前一天了。
    try:
        s = lastmod.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).strftime("%Y%m%d")
    except Exception:
        return None


def _title_from_url(page_url: str) -> str:
    slug = urllib.parse.unquote(urllib.parse.urlparse(page_url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
    words = re.sub(r"[-_]+", " ", slug).strip()
    return (words[:1].upper() + words[1:]) if words else page_url


def fetch_sitemap_playlist(url: str, fetch_bodies: bool = True) -> dict:
    """把一个没有 RSS 的网页链接（比如资讯列表页）通过它的 sitemap.xml 模拟成
    一份"订阅源"。只保留 URL 路径以种子链接路径为前缀的页面——不然会把"关于
    我们""招聘""法务条款"这些跟种子链接毫不相关的页面也当成"内容"混进来。

    sitemap 只有 URL（可能有更新时间），没有标题和正文：
    - fetch_bodies=True：逐个抓候选页面的正文（复用 fetch_generic_article_entry），
      单条打不开就跳过——临时链接这种"马上就要处理"的场景用；
    - fetch_bodies=False：只列链接，标题从链接推断，正文等真正处理时再抓
      （见 fetch_source_text）——订阅的"检查新内容"用，一次检查只需要一两个请求。
    """
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    seed_path = parsed.path.rstrip("/")
    path_prefix = f"{seed_path}/" if seed_path else "/"

    prefetched: dict = {}
    sitemap_url = _discover_sitemap_url(url, prefetched)
    if not sitemap_url:
        raise RuntimeError("这个网站没有找到 sitemap.xml，也没有 RSS/Atom 订阅源，暂不支持跟踪")

    seen: set[str] = set()
    candidates: list[tuple[str, Optional[str]]] = []
    for page_url, lastmod in _collect_sitemap_urls(sitemap_url, path_prefix, prefetched=prefetched):
        if page_url in seen:
            continue
        seen.add(page_url)
        candidates.append((page_url, lastmod))
    if not candidates:
        raise RuntimeError(f"在这个网站的 sitemap 里没有找到 {path_prefix} 开头的页面")

    def _sort_key(item):
        _u, lastmod = item
        ts = _sitemap_lastmod_ts(lastmod) if lastmod else None
        return (0, -ts) if ts is not None else (1, 0.0)

    candidates.sort(key=_sort_key)
    candidates = candidates[:SITEMAP_MAX_ENTRIES]

    entries = []
    skipped = []
    for idx, (page_url, lastmod) in enumerate(candidates, start=1):
        if not fetch_bodies:
            entry = {
                "index": idx, "id": _stable_id(page_url), "title": _title_from_url(page_url),
                "duration": 0, "url": page_url, "source_type": "article", "is_raw_session": False,
            }
            date_str = _sitemap_lastmod_to_date(lastmod) if lastmod else None
            if date_str:
                entry["publish_date"] = date_str
            entries.append(entry)
            continue
        try:
            entry = fetch_generic_article_entry(page_url)
        except Exception as e:
            skipped.append({"url": page_url, "reason": str(e)})
            continue
        entry["index"] = idx
        if lastmod and not entry.get("publish_date"):
            date_str = _sitemap_lastmod_to_date(lastmod)
            if date_str:
                entry["publish_date"] = date_str
        entries.append(entry)
    if not entries:
        raise RuntimeError("sitemap 里找到了页面，但都抓不到正文（可能是列表页而非文章页占了大多数）")

    domain = parsed.netloc.removeprefix("www.")
    path_label = seed_path.strip("/").split("/")[-1].replace("-", " ").title() if seed_path else ""
    summit_title = f"{domain}" + (f" · {path_label}" if path_label else "")

    return {
        "summit_title": summit_title,
        "playlist_id": _stable_id(sitemap_url + path_prefix),
        "entries": entries,
        "skipped": skipped,
        "content_type": "series",
    }
