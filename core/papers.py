"""论文：arXiv 链接、PDF（链接或上传的文件）存进 Spark/论文/（剪藏式文件夹，格式同剪藏，见 core/clips.py）。

电脑这边收（阅读页、Mac App 的「添加论文」走 /api/papers 和 /api/papers/upload）。手机自己收（apps/mobile-ios/Spark/Papers.swift），
规则和这里一致：arXiv 链接的认法、Atom 的解析、PDF 的上限和扫描版的认法、段落合并、文件头多出来的几行
（authors、arxiv、pdf，见 clip_compose.paper_head_lines），tests/test_papers_capture.py 用 swiftc 两边对表。

取内容的顺序（arXiv）：信息接口拿标题、作者、日期、摘要（失败退回摘要页的 citation_* 标签）；再试网页版全文
（arxiv.org/html/<id>，用 sources 的正文提取）；404 或正文太短就下 PDF 取字。摘要、翻译、索引沿用剪藏那一套
（clip_capture._process），任务位也和剪藏共用，进度用 /api/clips/<id> 查。

PDF 原件：来自链接的存在 attachments/<网址的 sha256 前 16 位>.pdf，上传的按内容的 sha256 前 16 位起名。
查重：同一个 arXiv 编号（不分版本）、同一个网址（clips.link_identity）、同一份 PDF（文件名里的哈希）算同一篇，
再收一次直接返回已有的那篇，不重新抓。所有网络请求都走 GET（测试里换掉），一个接一个，不并发。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import xml.etree.ElementTree as ET

from core import clip_capture, clip_compose, clips, doctime, github_sync, remote_tasks, sources, vault

TIMEOUT = 30                      # 信息接口、网页版的超时（秒）；PDF 用 PDF_TIMEOUT
PDF_TIMEOUT = 90
MAX_PAGE_BYTES = 20 * 1024 * 1024
HTML_MIN_CHARS = 2000             # 网页版全文去掉空白后不到这么多字算太短，退回 PDF（同手机 Paper.htmlMinChars）
ARXIV_SOURCE = "arXiv"
FILE_SOURCE = "PDF"               # 上传的 PDF 没有网址，来源写这个（同手机）
EXISTS_MESSAGE = "这篇论文已经有了，没有再存"
NOT_PDF_MESSAGE = "这不是 PDF 文件"
UPLOAD_SLACK = 1024 * 1024        # 上传请求比 PDF 本身多出来的表单开销


def _http_get(url: str, timeout: int = TIMEOUT, max_bytes: int = MAX_PAGE_BYTES) -> bytes:
    return sources.http_get(url, timeout=timeout, max_bytes=max_bytes)


GET = _http_get   # 所有网络请求都走它，测试里换成录好的样本


# ---------------------------------------------------------------- arXiv 链接（同手机 Paper.arxivID）

_ARXIV_RE = re.compile(
    r"^(?i:https?://(?:www\.|export\.)?arxiv\.org)/(?:abs|pdf|html)/"
    r"(\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$")


def arxiv_id(url: str) -> str:
    """arXiv 的 abs、pdf、html 链接里的编号（不带版本号）；不是 arXiv 链接返回空串。镜像（alphaxiv 之类）不认。"""
    m = _ARXIV_RE.match((url or "").strip())
    return m.group(1) if m else ""


def abs_url(aid: str) -> str:
    return f"https://arxiv.org/abs/{aid}"


def pdf_url(aid: str) -> str:
    return f"https://arxiv.org/pdf/{aid}"


def is_paper_link(url: str) -> bool:
    """贴进来的链接要不要直接存为论文：arXiv，或者网址以 .pdf 结尾（内容是 PDF 的在抓到后才知道）。"""
    return bool(arxiv_id(url)) or (url or "").split("?", 1)[0].split("#", 1)[0].lower().endswith(".pdf")


def pdf_name(key: bytes | str) -> str:
    """PDF 原件在论文文件夹里的位置：attachments/<sha256 前 16 位>.pdf。来自链接的按网址算，上传的按内容算。"""
    data = key.encode("utf-8") if isinstance(key, str) else key
    return f"attachments/{hashlib.sha256(data).hexdigest()[:16]}.pdf"


# ---------------------------------------------------------------- 信息接口、摘要页（同手机 Paper.parseAtom、Paper.parseAbs）

_ATOM = "{http://www.w3.org/2005/Atom}"


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_atom(data: bytes) -> dict | None:
    """export.arxiv.org/api/query 的 Atom：{title, authors, published (YYYY-MM-DD，首次提交), summary}。
    没有条目、报错条目（arXiv 给错编号时回一条 title 是 Error 的）、XML 坏了都返回 None。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        return None
    if "/api/errors" in (entry.findtext(f"{_ATOM}id") or ""):
        return None
    title = _squash(entry.findtext(f"{_ATOM}title") or "")
    if not title:
        return None
    published = (entry.findtext(f"{_ATOM}published") or "").strip()[:10]
    return {"title": title,
            "authors": [n for n in (_squash(a.findtext(f"{_ATOM}name") or "") for a in entry.findall(f"{_ATOM}author")) if n],
            "published": published if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) else "",
            "summary": _squash(entry.findtext(f"{_ATOM}summary") or "")}


def _meta_values(html: str, name: str) -> list[str]:
    import html as html_mod
    out = []
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(rf"""\bname\s*=\s*["']{re.escape(name)}["']""", tag, re.I):
            c = re.search(r"""\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*'([^']*)'""", tag, re.I)
            if c:
                out.append(_squash(html_mod.unescape(c.group(1) if c.group(1) is not None else c.group(2))))
    return [v for v in out if v]


def parse_abs(html: str) -> dict | None:
    """信息接口不通时的退路：摘要页里的 citation_title、citation_author（「姓, 名」换成「名 姓」）、citation_date。"""
    titles = _meta_values(html, "citation_title")
    if not titles:
        return None
    authors = []
    for a in _meta_values(html, "citation_author"):
        parts = [p.strip() for p in a.split(",")]
        authors.append(f"{parts[1]} {parts[0]}" if len(parts) == 2 and all(parts) else a)
    day = next(iter(_meta_values(html, "citation_date")), "").replace("/", "-")
    return {"title": titles[0], "authors": authors,
            "published": day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "",
            "summary": next(iter(_meta_values(html, "citation_abstract")), "")}


# ---------------------------------------------------------------- 网页版全文里的公式、PDF 的段落（同手机 Paper.texMath、Paper.paragraphs）

_MATH_RE = re.compile(r"<math\b([^>]*)>.*?</math>", re.S | re.I)


def tex_math(html: str) -> str:
    """网页版全文里的 MathML 换成它带的 TeX（alttext）：行内 $…$，单独成行的写成一段 $$…$$。没有 alttext 的不动。"""
    def repl(m):
        alt = re.search(r"""\balttext=(?:"([^"]*)"|'([^']*)')""", m.group(1))
        if not alt:
            return m.group(0)
        tex = alt.group(1) if alt.group(1) is not None else alt.group(2)
        return f"<p>$${tex}$$</p>" if re.search(r"""\bdisplay=["']block["']""", m.group(1)) else f"${tex}$"
    return _MATH_RE.sub(repl, html)


def _title_key(s: str) -> str:
    return _squash(s).lower()


def drop_title_heading(md: str, title: str) -> str:
    """网页版全文里第一个标题行（# 开头）去掉 # 和首尾空白后跟论文标题一样（不分大小写、连续空白当一个），就去掉这一行；
    去掉后上下各有一个空行时再去掉一个空行。只看第一个标题行，别的不动（同手机 Paper.dropTitleHeading）。"""
    lines = md.split("\n")
    for i, ln in enumerate(lines):
        if re.match(r"#{1,6}\s", ln):
            if _title_key(title) and _title_key(ln.lstrip("#")) == _title_key(title):
                del lines[i]
                if i < len(lines) and not lines[i].strip() and (i == 0 or not lines[i - 1].strip()):
                    del lines[i]
            break
    return "\n".join(lines)


_END_PUNCT = ".!?。！？:："


def _cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


def paragraphs(pages: list[str]) -> str:
    """PDF 取出来按行断开的字合成段落（最基本的合并，不做版面分析，页眉页脚、参考文献在这之前的 clean_pages 里处理）：空行分段；
    行尾「字母-」接小写字母开头的下一行，去掉连字符直接拼；两边都是汉字直接拼，别的加一个空格；
    以句末标点结尾、又明显比这一页最长的行短（不到 80%）的行，算一段的结尾。每页另起一段。"""
    out = []
    for page in pages:
        lines = [ln.strip() for ln in page.split("\n")]
        width = max((len(ln) for ln in lines), default=0)
        para = ""
        for ln in lines:
            if not ln:
                if para:
                    out.append(para)
                para = ""
                continue
            if not para:
                para = ln
            elif len(para) >= 2 and para[-1] == "-" and para[-2].isalpha() and ln[0].islower():
                para = para[:-1] + ln
            elif _cjk(para[-1]) and _cjk(ln[0]):
                para += ln
            else:
                para += " " + ln
            if ln[-1] in _END_PUNCT and len(ln) < 0.8 * width:
                out.append(para)
                para = ""
        if para:
            out.append(para)
    return "\n\n".join(out)


# ---- PDF 取字后的清理（只用在 PDF 这条路，arXiv 网页版全文不走）。规则两端一致：手机 Paper.cleanPages
HEADER_MIN_PAGES = 3          # 少于这么多页不做页眉页脚去重
HEADER_MIN_COUNT = 3          # 同一行至少在这么多页的页首页尾出现
HEADER_ZONE = 2               # 页首页尾各看前、后这么多个非空行
HEADER_MAX_CHARS = 120        # 更长的行不当页眉页脚
_PAGE_NO_RE = re.compile(r"^(?:[-–—]\s*[0-9]{1,4}\s*[-–—]|[0-9]{1,4}|[0-9]{1,4}\s*/\s*[0-9]{1,4}"
                         r"|page\s*[0-9]{1,4}(?:\s*of\s*[0-9]{1,4})?)$")
_REFERENCES_LINE_RE = re.compile("^" + clip_compose.REFERENCES_TITLE + "$")


def _line_key(line: str) -> str:
    """比较页眉页脚用：空白合并、转小写、连续数字换成一个 0（「Page 3 of 12」和「Page 4 of 12」同一行）。"""
    return re.sub(r"[0-9]+", "0", re.sub(r"\s+", " ", line.strip()).lower())


def clean_pages(pages: list[str]) -> list[str]:
    """PDF 每页的字，去掉页眉页脚和页码，参考文献那一行变成「## 」标题行（前后各空一行）。纯规则：
    1. 页首页尾（各前 2、后 2 个非空行）里，归一后的同一行出现在一半以上的页、且至少 3 页，这些行去掉；少于 3 页的不做这一条；
    2. 页首页尾里单独成行的页码（「3」「- 3 -」「3 / 12」「Page 3 of 12」）去掉；
    3. 单独一行的 References / Bibliography / 参考文献（可带编号前缀，不分大小写）变成「## 」开头的标题行。
    行尾断词接上由 paragraphs 做。"""
    split = [[ln.strip() for ln in page.split("\n")] for page in pages]
    zones = []
    for lines in split:
        idx = [i for i, ln in enumerate(lines) if ln]
        zones.append(set(idx[:HEADER_ZONE] + idx[-HEADER_ZONE:]))
    repeated: set[str] = set()
    if len(pages) >= HEADER_MIN_PAGES:
        count: dict[str, int] = {}
        for lines, zone in zip(split, zones):
            for key in {_line_key(lines[i]) for i in zone if len(lines[i]) <= HEADER_MAX_CHARS}:
                count[key] = count.get(key, 0) + 1
        need = max(HEADER_MIN_COUNT, (len(pages) + 1) // 2)
        repeated = {k for k, n in count.items() if n >= need}
    out = []
    for lines, zone in zip(split, zones):
        kept = []
        for i, ln in enumerate(lines):
            if i in zone and ln and ((len(ln) <= HEADER_MAX_CHARS and _line_key(ln) in repeated) or _PAGE_NO_RE.match(ln.lower())):
                continue
            if ln and _REFERENCES_LINE_RE.match(ln.lower()):
                kept += ["", "## " + ln, ""]
            else:
                kept.append(ln)
        out.append("\n".join(kept))
    return out


def pdf_pages_markdown(pages: list[str]) -> str:
    """PDF 取出来的每页字 → 正文：先清理，再合成段落。"""
    return paragraphs(clean_pages(pages))


def pdf_title(data: bytes, name: str) -> str:
    """PDF 元数据里的标题；没有就用文件名（去掉 .pdf，下划线和连字符换成空格）。"""
    return pdf_meta_title(data) or _file_title(name)


def pdf_meta_title(data: bytes) -> str:
    """PDF 元数据里的标题，没有是空串。"""
    try:
        import io
        import pypdf
        meta = pypdf.PdfReader(io.BytesIO(data)).metadata
        if meta and meta.title and meta.title.strip():
            return _squash(meta.title)
    except Exception:  # noqa: BLE001  元数据读不出来就用文件名
        pass
    return ""


def _file_title(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    return _squash(base.replace("_", " ").replace("-", " "))


# ---------------------------------------------------------------- 取内容

def _article(url, title, source, markdown, *, published="", authors=(), aid="", pdf="", guessed=False) -> dict:
    return {"url": url, "title": title[:500], "source": source, "published": published, "publishedAt": "",
            "markdown": markdown[:clip_capture._MAX_MARKDOWN], "images": {}, "titleGuessed": guessed, "imagesOnly": False,
            "authors": list(authors), "arxiv": aid, "pdf": pdf}


def _html_markdown(url: str, raw: bytes) -> str:
    """一页 HTML 的正文（sources 的正文提取 + clip_capture 的 Markdown 写法），公式先换成 TeX。抠不出正文返回空串。"""
    try:
        entry = sources.article_entry_from_raw(url, tex_math(raw.decode("utf-8", errors="replace")).encode("utf-8"))
        return clip_capture.article_from_entry(url, entry)["markdown"]
    except RuntimeError:
        return ""


def _long_enough(md: str) -> bool:
    return sum(1 for ch in md if not ch.isspace()) >= HTML_MIN_CHARS


def _pdf_markdown(data: bytes) -> str:
    return pdf_pages_markdown(sources.pdf_pages(data))


# arXiv 的 PDF 第一页左边的戳记，取字后形如「arXiv:2506.09108v1  [cs.LG]  10 Jun 2025」（空白数量不定；旧编号 arXiv:hep-th/9901001v2）。
# 只在第一页的字里找，只认「编号 [分类] 日期」这个整体（正文里引用别人的 arXiv 编号不算）。手机 Paper.stampID 同一条
_STAMP_CAT = r"\[[A-Za-z][A-Za-z.\-]*\]"
_STAMP_DATE = r"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}"
# 新编号一定带 [分类]；旧编号的戳记常常没有 [分类]（arXiv:hep-th/9901001v2  25 Jan 1999），分类可有可无
STAMP_RE = re.compile(r"arXiv\s*:\s*(?:(\d{4}\.\d{4,5})(?:v\d+)?\s*" + _STAMP_CAT
                      + r"|([a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\s*" + _STAMP_CAT + r")?)\s*" + _STAMP_DATE)


def stamp_id(pages: list[str]) -> str:
    """PDF 第一页的 arXiv 戳记里的编号（不带版本号）；没有返回空串。"""
    m = STAMP_RE.search(pages[0]) if pages else None
    return (m.group(1) or m.group(2)) if m else ""


def _pdf_read(data: bytes) -> tuple[str, str]:
    """PDF 的 (正文, arXiv 戳记编号)。"""
    pages = sources.pdf_pages(data)
    return pdf_pages_markdown(pages), stamp_id(pages)


def _stamped(article: dict, aid: str) -> dict:
    """第一页有 arXiv 戳记的 PDF：用信息接口补 published、authors、arxiv（取不到就照原样，不报错）。
    标题、来源、网址、原件名不动。"""
    meta = _arxiv_meta(aid) if aid else {}
    if not meta:
        return article
    article["arxiv"] = aid
    article["published"] = meta.get("published") or ""
    article["authors"] = list(meta.get("authors") or [])
    return article


def _arxiv_meta(aid: str) -> dict:
    """信息接口 → 摘要页，都不通返回空的（不让整篇失败）。"""
    try:
        meta = parse_atom(GET(f"https://export.arxiv.org/api/query?id_list={aid}"))
        if meta:
            return meta
    except Exception:  # noqa: BLE001
        pass
    try:
        meta = parse_abs(GET(abs_url(aid)).decode("utf-8", errors="replace"))
        if meta:
            return meta
    except Exception:  # noqa: BLE001
        pass
    return {}


def fetch_arxiv(aid: str) -> tuple[dict, bytes, list[str]]:
    """arXiv 的一篇：(article, PDF 原件的字节（没下成是 b""）, 提示)。正文一点都没取到、摘要也没有才抛 RuntimeError。"""
    notes = []
    meta = _arxiv_meta(aid)
    if not meta:
        notes.append("arXiv 的信息接口和摘要页都没打开，标题和作者可能不全")
    md, why = "", ""
    try:
        md = _html_markdown(f"https://arxiv.org/html/{aid}/", GET(f"https://arxiv.org/html/{aid}"))
        md = drop_title_heading(md, meta.get("title") or "")
    except Exception:  # noqa: BLE001  404（这篇没有网页版）、超时都退回 PDF
        md = ""
    data = b""
    try:
        data = GET(pdf_url(aid), timeout=PDF_TIMEOUT, max_bytes=sources.PDF_MAX_BYTES)
        if not data.startswith(b"%PDF-"):
            data = b""
    except Exception as e:  # noqa: BLE001
        why = str(e)
    if not _long_enough(md):
        md = ""
        if data:
            try:
                md = _pdf_markdown(data)
            except RuntimeError as e:
                why = str(e)
    title = meta.get("title") or (pdf_title(data, "") if data else "")
    if not md:
        if not meta.get("summary"):
            raise RuntimeError(f"没取到这篇论文的正文（{why or '网页版和 PDF 都打不开'}）")
        md = meta["summary"]
        notes.append(f"正文没取到（{why or '网页版和 PDF 都打不开'}），先存了 arXiv 上的摘要")
    article = _article(abs_url(aid), title or f"arXiv {aid}", ARXIV_SOURCE, md, published=meta.get("published") or "",
                       authors=meta.get("authors") or [], aid=aid, pdf=pdf_name(pdf_url(aid)) if data else "",
                       guessed=not title)
    return article, data, notes


def fetch_link(url: str) -> tuple[dict, bytes, list[str]]:
    """论文页签里贴进来的任意链接：arXiv 按上面；内容是 PDF 的取字（原件按网址起名）；别的网页照剪藏的正文提取。"""
    aid = arxiv_id(url)
    if aid:
        return fetch_arxiv(aid)
    try:
        raw = GET(url, timeout=PDF_TIMEOUT, max_bytes=sources.PDF_MAX_BYTES)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"打不开这个链接（{e}）") from e
    if raw.startswith(b"%PDF-"):
        md, stamp = _pdf_read(raw)
        from urllib.parse import unquote, urlsplit
        meta = pdf_meta_title(raw)   # 没有元数据标题、只能用文件名的：让写摘要时顺带起一个标题（titleGuessed）
        title = meta or _file_title(unquote(urlsplit(url).path))
        return (_stamped(_article(url, title or clip_compose.stub_title(url), clip_compose.stub_source(url), md,
                                  pdf=pdf_name(url), guessed=not meta), stamp), raw, [])
    entry = sources.article_entry_from_raw(url, raw)   # 抠不出正文抛 RuntimeError
    a = clip_capture.article_from_entry(url, entry)
    article = _article(url, a["title"] or clip_compose.stub_title(url), a["source"], a["markdown"], published=a["published"],
                       guessed=not a["title"])
    article["publishedAt"] = a["publishedAt"]
    return article, b"", []


def fetch_file(data: bytes, filename: str) -> tuple[dict, bytes, list[str]]:
    """上传的一份 PDF：取字，原件按内容起名。"""
    if not data.startswith(b"%PDF-"):
        raise RuntimeError(NOT_PDF_MESSAGE)
    md, stamp = _pdf_read(data)
    meta = pdf_meta_title(data)   # 没有元数据标题、只能用文件名的：让写摘要时顺带起一个标题（titleGuessed）
    title = meta or _file_title(filename or "")
    return _stamped(_article("", title or "未命名", FILE_SOURCE, md, pdf=pdf_name(data), guessed=not meta), stamp), data, []


# ---------------------------------------------------------------- 查重

def find_existing(root: str, aid: str = "", url: str = "", pdf: str = "") -> dict | None:
    """论文文件夹里已有的同一篇：{path, title}，没有返回 None。同一个 arXiv 编号（不分版本）、同一个网址、同一份 PDF。"""
    folder = os.path.join(root, *clips.PAPERS_DIR.split("/"))
    want = clips.link_identity(url) if url else ""
    for c in clips.load_clips(root, clips.PAPERS_DIR):
        try:
            with open(os.path.join(folder, c["name"]), encoding="utf-8") as f:
                fm = clips.frontmatter(f.read(16384))
        except OSError:
            continue
        have = fm.get("url") or ""
        if (aid and (fm.get("arxiv") == aid or arxiv_id(have) == aid)) \
                or (want and have and clips.link_identity(have) == want) or (pdf and fm.get("pdf") == pdf):
            return {"path": f"{clips.PAPERS_DIR}/{c['name']}", "title": c["title"]}
    return None


# ---------------------------------------------------------------- 写一篇

def _write_pdf(root: str, name: str, data: bytes) -> None:
    full = os.path.join(root, *clips.PAPERS_DIR.split("/"), *name.split("/"))
    if os.path.exists(full):
        return
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".partial"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)


def compose(article: dict, summary: dict | None, captured, later: str = "") -> str:
    from core import annotations
    text = clip_compose.render(article, "", summary, captured)
    return annotations.with_later(text, True, later) if later else text


def _run(job_id: str, root: str, later: str, keys: dict, fetch) -> None:
    upd = clip_capture._update
    try:
        old = find_existing(root, **keys)
        if old:
            upd(job_id, done=True, path=old["path"], title=old["title"], phase="已经有了", warning=EXISTS_MESSAGE, existed=True)
            return
        article, data, notes = fetch()
        upd(job_id, title=article["title"])
        if article.get("arxiv") and not keys.get("aid"):
            old = find_existing(root, aid=article["arxiv"])
            if old:
                upd(job_id, done=True, path=old["path"], title=old["title"], phase="已经有了", warning=EXISTS_MESSAGE, existed=True)
                return
        summary = clip_capture._process(job_id, article, root, notes, clips.PAPERS_DIR)
        upd(job_id, phase="正在保存")
        if data and article.get("pdf"):
            _write_pdf(root, article["pdf"], data)
        captured = doctime.now()
        rel = clip_capture._unique_path(root, clip_compose.file_name(article["title"], clip_compose.name_day(article, captured)),
                                        clips.PAPERS_DIR)
        from core import atomic
        atomic.write_text(os.path.join(root, *rel.split("/")), compose(article, summary, captured, later))
        clips.rebuild_index(root, clips.PAPERS_DIR)
        upd(job_id, done=True, path=rel, phase="已保存", warning="；".join(notes))
        github_sync.after_task()
    except Exception as e:  # noqa: BLE001
        upd(job_id, done=True, error=str(e), phase="保存失败")


def start(raw) -> dict:
    """{url, later?}：贴进来的链接，一律存为论文。任务号加轮询（/api/clips/<id>），跟剪藏共用一个任务位。"""
    url = raw.get("url") if isinstance(raw, dict) else None
    if not isinstance(url, str) or len(url) > 2000 or not re.match(r"^https?://\S+$", url.strip()):
        raise remote_tasks.TaskError("论文的链接不对")
    url = url.strip()
    later = doctime.iso(doctime.now()) if clip_capture._later_flag(raw) else ""
    aid = arxiv_id(url)
    job_id = clip_capture._new_job("正在取论文内容", kind="paper")
    root = vault.vault_root()
    keys = {"aid": aid} if aid else {"url": url}
    threading.Thread(target=_run, args=(job_id, root, later, keys, lambda: fetch_link(url)), daemon=True).start()
    return clip_capture.status(job_id)


def start_upload(data: bytes, filename: str, later: bool = False) -> dict:
    """上传的一份 PDF（阅读页、Mac App）。超过 50MB、不是 PDF（看内容开头，不只看扩展名）的当场拒绝。"""
    if len(data) > sources.PDF_MAX_BYTES:
        raise remote_tasks.TaskError(sources.PDF_TOO_BIG_MESSAGE)
    if not data.startswith(b"%PDF-"):
        raise remote_tasks.TaskError(NOT_PDF_MESSAGE)
    job_id = clip_capture._new_job("正在取 PDF 里的字", kind="paper")
    root = vault.vault_root()
    stamp = doctime.iso(doctime.now()) if later else ""
    threading.Thread(target=_run, args=(job_id, root, stamp, {"pdf": pdf_name(data)}, lambda: fetch_file(data, filename)),
                     daemon=True).start()
    return clip_capture.status(job_id)


# ---------------------------------------------------------------- 真实试跑用（不进主流程，见 tools/paper_trial.py）

def capture_to_dir(source: str, out_dir: str, summarize: bool = False) -> str:
    """source：arXiv 链接、任意链接或本地 PDF 路径。按电脑的流程处理成论文 Markdown，写到 out_dir（不写笔记库），
    PDF 原件放在 out_dir/attachments/。summarize=True 时用设置里的模型写摘要（不翻译）。返回写好的文件路径。"""
    if os.path.isfile(source):
        with open(source, "rb") as f:
            data = f.read()
        if len(data) > sources.PDF_MAX_BYTES:
            raise RuntimeError(sources.PDF_TOO_BIG_MESSAGE)
        article, pdf, notes = fetch_file(data, os.path.basename(source))
    else:
        article, pdf, notes = fetch_link(source)
    summary = None
    if summarize:
        ask = clip_capture._ask()
        if ask is None:
            notes.append("没配好模型，没写摘要")
        else:
            summary = clip_compose.summarize(article, [], ask)
    os.makedirs(out_dir, exist_ok=True)
    if pdf and article.get("pdf"):
        full = os.path.join(out_dir, *article["pdf"].split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(pdf)
    captured = doctime.now()
    path = os.path.join(out_dir, clip_compose.file_name(article["title"], clip_compose.name_day(article, captured)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(compose(article, summary, captured))
    for n in notes:
        print("提示：" + n)
    return path
