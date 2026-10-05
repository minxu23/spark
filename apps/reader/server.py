"""
Spark 阅读：在浏览器里直接读笔记库 Spark/ 目录下生成的 Markdown。

平时由 spark.py 挂在 /read 下。页面在服务端渲染好（mistune）。整理稿里「原文段落 +
中文引用块」的对照结构，宽屏下排成左右两栏。

页面先给标题和摘要：开头的元信息（frontmatter、「- 所属节目：…」那串）折成一行，点开才看全。
标题够多的长文带目录（宽屏左侧跟着滚，窄屏正文上面折起来），每个标题有固定的锚点。
节目 / 会议文件夹页先列单集：每期一行，可读标题、日期，后面是笔记 / 整理稿 / 文字记录的入口。

排版像 Safari 阅读模式那样可以现场切换：顶栏「Aa」里选配色、字体、字号和栏宽，
全靠 CSS 变量，选择存在浏览器本地（static/theme.js 在首屏前套上，避免闪一下）。
读写偏好的脚本和配色 / 字体栈 CSS 放在根目录 static/common/read-prefs.*，跟设置页 /settings
的「阅读体验」共用同一份 localStorage；「Aa」只放常用的三种字体，更多字体在设置页选。

读的时候选中文字可以「高亮」或「摘录」：
- 高亮直接写回原文件，用 Obsidian 自己的 ==文字== 语法，两边看到的一样；是某一期的
  笔记、整理稿或文字记录的话，再在这一期 notes/ 里的笔记末尾「我的高亮」记一条（重新
  生成小结时这一节会被接回去，整理稿重跑丢了行内高亮，这里也还在）；
- 摘录追加到 Spark/摘录.md（新的在上面），带出处链接和可选的想法，同时把这段高亮。
写之前核对页面打开时文件的修改时间，文件在别处（比如 Obsidian）改过就拒绝，免得覆盖。

笔记洞察的报告和演示（库根目录 output/）也在这里读，地址是 /read/r/…，内部路径写成 "@r/…"：
- 报告就是 Markdown，跟别的页面一样能换排版、高亮、摘录，高亮汇总在报告自己末尾的「我的高亮」；
- 演示（.deck.html）原样打开，再注入一段标注脚本：在幻灯片上高亮的句子记进对应报告的
  「我的高亮」（带「演示第 N 页」），打开演示时读回来标上。演示文件本身不改，重新生成也不丢。
  演示的配图在旁边的 x.deck.assets/ 里，/read/r/x.deck.assets/slide-N.png 只给这种文件夹里的图片。

设置页「阅读体验」可以导入 Typora / Obsidian 主题 CSS（themes.py 清洗成只作用于正文的样式）：
/read/api/themes 导入 / 列出，DELETE /read/api/themes/<id> 删除，产物以同源样式表
/read/themes/<id>.css 提供，所以 CSP 不用放松。用哪个主题是浏览器本地的阅读偏好，theme.js 首屏前挂上。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse

import mistune
from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory
from mistune.renderers.html import HTMLRenderer

from apps.reader import themes as reader_themes
from core import atomic
from core import vault as core_vault
from core import web_guard

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
EXCERPTS_NAME = "摘录.md"
MAX_SELECTION_CHARS = 4000

app = Flask(__name__, static_folder=None)
web_guard.install(app)


def _root() -> str:
    return core_vault.spark_dir()


@app.after_request
def security_headers(resp):
    # 演示是笔记洞察自己生成的单文件 HTML，翻页靠它内嵌的脚本，只有这一种页面放开内联脚本
    inline = " 'unsafe-inline'" if request.path.endswith(".deck.html") else ""
    resp.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self'{inline}; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------------------------------------------------------- 路径

REPORTS = "@r"   # 内部路径里笔记洞察报告目录的前缀


def _reports_root() -> str:
    return core_vault.reports_dir()


def _resolve(rel: str) -> str | None:
    """rel 是相对 Spark/ 的路径，或 "@r/…" 相对报告目录。点开头的（.cache、.manifest.json）
    一律不给看，也不许 .. 跳出去。"""
    rel = rel.strip("/")
    parts = [p for p in rel.split("/") if p]
    base = _root()
    if parts and parts[0] == REPORTS:
        base, parts = _reports_root(), parts[1:]
    if any(p.startswith(".") for p in parts):
        return None
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(root, *parts))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path


def _in(path: str, base: str) -> bool:
    base = os.path.realpath(base)
    return path == base or path.startswith(base + os.sep)


def _rel(path: str) -> str:
    real = os.path.realpath(path)
    if _in(real, _reports_root()) and not _in(real, _root()):
        tail = os.path.relpath(real, os.path.realpath(_reports_root())).replace(os.sep, "/")
        return REPORTS if tail == "." else f"{REPORTS}/{tail}"
    return os.path.relpath(real, os.path.realpath(_root())).replace(os.sep, "/")


def _url(rel: str, *, is_dir: bool = False) -> str:
    rel = rel.strip("/")
    sr = request.script_root
    if rel == REPORTS or rel.startswith(REPORTS + "/"):
        tail = rel[len(REPORTS):].strip("/")
        return f"{sr}/r/{urllib.parse.quote(tail)}" + ("/" if is_dir and tail else "")
    tail = urllib.parse.quote(rel) + ("/" if is_dir and rel else "")
    return f"{sr}/f/{tail}" if rel else f"{sr}/"


def _link(path: str) -> str:
    """指向某个 .md 的站内链接。节目 / 会议主页（X/X.md）指到文件夹页，那里排的就是主页。"""
    d = os.path.dirname(path)
    if os.path.basename(path) == os.path.basename(d) + ".md":
        return _url(_rel(d), is_dir=True)
    return _url(_rel(path))



# ---------------------------------------------------------------- Markdown

class _Renderer(HTMLRenderer):
    def link(self, text, url, title=None):
        out = super().link(text, url, title)
        if re.match(r"(?i)https?://", url):
            out = out.replace("<a ", '<a target="_blank" rel="noopener" ', 1)
        return out


_md = mistune.create_markdown(renderer=_Renderer(escape=True),
                              plugins=["table", "strikethrough", "url", "mark"])

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
# 整理稿的双语结构：原文一段（或标题），紧跟一个只装译文的引用块
_PAIR_RE = re.compile(
    r"(<(p|h[1-6])>(?:(?!</\2>).)*</\2>)\n<blockquote>\n((?:(?!</?blockquote>).)*)</blockquote>", re.S)


def _split_frontmatter(text: str) -> tuple[list[tuple[str, object]], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return [], text
    items = []
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if not sep or not key.strip():
            continue
        val = val.strip()
        if val.startswith(("[", '"')):
            try:
                val = json.loads(val)
            except ValueError:
                pass
        items.append((key.strip(), val))
    return items, text[m.end():]


def _wikilink_target(name: str, here_dir: str) -> str | None:
    name = name.strip()
    root = os.path.realpath(_root())
    if "/" in name:
        # 带路径的写法（摘录里的出处就是这样写的）：从库根目录或 Spark/ 算起
        for base in (core_vault.vault_root(), root):
            cand = os.path.realpath(os.path.join(base, name + ".md"))
            if cand.startswith(root + os.sep) and os.path.isfile(cand):
                return cand
    d = here_dir
    while True:
        cand = os.path.join(d, name + ".md")
        if os.path.isfile(cand):
            return cand
        if d == root or not d.startswith(root):
            break
        d = os.path.dirname(d)
    cand = os.path.join(root, name, name + ".md")
    return cand if os.path.isfile(cand) else None


def _wikilink_html(m: re.Match, here_dir: str) -> str:
    target = _wikilink_target(m.group(1), here_dir)
    label = html.escape((m.group(2) or m.group(1)).strip())
    if not target:
        return label
    return f'<a href="{html.escape(_link(target))}">{label}</a>'


def _inline(value: object, here_dir: str) -> str:
    if isinstance(value, list):
        return "".join(f'<span class="chip">{_inline(v, here_dir)}</span>' for v in value)
    text = str(value)
    if re.match(r"https?://", text):
        e = html.escape(text)
        return f'<a href="{e}" target="_blank" rel="noopener">{e}</a>'
    out, pos = [], 0
    for m in _WIKILINK_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append(_wikilink_html(m, here_dir))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def render_markdown(text: str, here_dir: str, *, bilingual: bool = False, date_hint: str = "") -> tuple[str, str]:
    """返回 (标题, 正文 HTML)。"""
    title, out, _heads = _render(text, here_dir, bilingual=bilingual, date_hint=date_hint)
    return title, out


def _render(text: str, here_dir: str, *, bilingual: bool = False,
            date_hint: str = "") -> tuple[str, str, list[dict]]:
    """返回 (标题, 正文 HTML, 标题列表)。标题都带上锚点；开头的元信息收进一个默认折起的 <details>。
    只改排版不动文字：高亮是拿选中的文字回源码里找的，页面上的字要和源码对得上。"""
    meta, body = _split_frontmatter(text)

    def wl(m):
        target = _wikilink_target(m.group(1), here_dir)
        label = (m.group(2) or m.group(1)).strip()
        return f"[{label}](<{_link(target)}>)" if target else label

    body = _WIKILINK_RE.sub(wl, body)
    title_m = re.search(r"^# (.+)$", body, re.M)
    title = title_m.group(1).strip() if title_m else ""
    out = _md(body)
    if bilingual:
        out = _PAIR_RE.sub(r'<div class="pair"><div class="orig">\1</div><div class="tr">\3</div></div>', out)
    out, heads = _anchor_headings(out)
    out = _fold_meta(out, meta, here_dir, date_hint)
    return title, out, heads


# ---- 元信息：frontmatter 和标题下面「- 所属节目：…」这种键值列表，默认折起来

# 标题、一句话摘要（报告是副标题 + 摘要）之后紧跟的键值列表才算元信息。
# 副标题只在后面跟着摘要时才认，免得把「# 标题」下面直接的「## 要点」当成副标题
_META_HEAD_RE = re.compile(
    r"\A(\s*<h1[^>]*>(?:(?!</h1>).)*</h1>\n"
    r"(?:(?:<h[23][^>]*>(?:(?!</h[23]>).)*</h[23]>\n)?<blockquote>\n(?:(?!</?blockquote>).)*</blockquote>\n)?)"
    r"(<ul>\n(?:<li>[^<：\n]{1,16}：(?:(?!</li>).)*</li>\n){2,}</ul>\n)?", re.S)
_LI_RE = re.compile(r"<li>((?:(?!</li>).)*)</li>", re.S)


def _plain(value: object) -> str:
    if isinstance(value, list):
        return "、".join(_plain(v) for v in value)
    return _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), str(value)).strip()


def _meta_label(fields: dict[str, str], date_hint: str) -> str:
    """折起来时显示的一行：节目 · 日期 · 时长（报告是 类型 · 日期 · 篇数），有哪个写哪个。"""
    def first(*keys):
        return next((fields[k] for k in keys if fields.get(k)), "")
    date = first("播出时间", "播出", "date", "日期") or date_hint
    parts = [first("所属节目", "所属会议", "节目"), first("type"), date, first("时长"), first("sources")]
    return " · ".join(p for p in parts if p) or "文档信息"


def _fold_meta(out: str, meta: list[tuple[str, object]], here_dir: str, date_hint: str) -> str:
    m = _META_HEAD_RE.match(out)
    items = m.group(2) if m else None
    if not meta and not items:
        return out
    fields = {k: _plain(v) for k, v in meta}
    for li in _LI_RE.findall(items or ""):
        k, _, v = _heading_text(li).partition("：")
        fields.setdefault(k.strip(), v.strip())
    inner = ""
    if meta:
        rows = "".join(f"<dt>{html.escape(k)}</dt><dd>{_inline(v, here_dir)}</dd>" for k, v in meta)
        inner += f'<dl class="meta">{rows}</dl>'
    if items:
        inner += items.replace("<ul>", '<ul class="meta-list">', 1)
    block = (f'<details class="docmeta"><summary>{html.escape(_meta_label(fields, date_hint))}</summary>'
             f"{inner}</details>\n")
    if not m:   # 开头没有标题：放最前面
        return block + out
    return out[:m.end(1)] + block + out[m.end():]


# ---- 标题锚点和目录

# 页面上别的元素已经占用的 id，标题不能重名
_RESERVED_IDS = {"aa", "prefs", "size-now", "toc", "episodes", "main", "top",
                 "custom-theme", "ct-name", "ct-off"}
_HEADING_SCAN_RE = re.compile(r'<blockquote>|</blockquote>|<div class="tr">|</div>|<h([1-6])>(.*?)</h\1>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ORIG_OPEN = '<div class="orig">'


def _heading_text(inner: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub("", inner)).split())


def _slug(text: str) -> str:
    """标题文字 → 锚点：留中文、字母、数字，空白换成 -，标点去掉。同一个标题每次得到同一个锚点。"""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:60].strip("-") or "section"


def _anchor_headings(out: str) -> tuple[str, list[dict]]:
    """给正文里的标题加 id（重名的依次加 -2、-3）。引用块里的标题（整理稿的中文译文）不加，
    它的文字记到前面那个原文标题上，目录里当副标题。"""
    used = set(_RESERVED_IDS)
    heads: list[dict] = []
    parts, pos, quote, in_tr = [], 0, 0, False
    for m in _HEADING_SCAN_RE.finditer(out):
        tok = m.group(0)
        if tok == "<blockquote>":
            quote += 1
        elif tok == "</blockquote>":
            quote = max(0, quote - 1)
        elif tok == '<div class="tr">':
            in_tr = True
        elif tok == "</div>":
            in_tr = False
        else:
            text = _heading_text(m.group(2))
            if quote or in_tr:
                if in_tr and heads and heads[-1]["paired"] and not heads[-1]["sub"]:
                    heads[-1]["sub"] = text
                continue
            base = _slug(text)
            hid, n = base, 2
            while hid in used:
                hid, n = f"{base}-{n}", n + 1
            used.add(hid)
            level = int(m.group(1))
            parts += [out[pos:m.start()], f'<h{level} id="{hid}">{m.group(2)}</h{level}>']
            pos = m.end()
            heads.append({"level": level, "id": hid, "text": text, "sub": "",
                          "paired": out.endswith(_ORIG_OPEN, 0, m.start())})
    parts.append(out[pos:])
    return "".join(parts), heads


TOC_MIN = 3     # 少于这么多节不出目录
TOC_MAX = 40    # 多于这么多条时去掉最深的一级


def _toc_entries(heads: list[dict]) -> list[dict]:
    title = next((h for h in heads if h["level"] == 1), None)
    cand = [h for h in heads if h["level"] <= 3 and h is not title]
    # 同一节下面反复出现的标题（文字记录里每段发言前的「🗣️ 某某」）不进目录
    parent_of, stack, counts = {}, [], {}
    for h in cand:
        while stack and stack[-1]["level"] >= h["level"]:
            stack.pop()
        parent_of[h["id"]] = stack[-1]["id"] if stack else ""
        stack.append(h)
        key = (parent_of[h["id"]], h["text"])
        counts[key] = counts.get(key, 0) + 1
    out = [h for h in cand if counts[(parent_of[h["id"]], h["text"])] < 3]
    levels = sorted({h["level"] for h in out})
    if len(out) > TOC_MAX and len(levels) > 1:
        shallower = [h for h in out if h["level"] != levels[-1]]
        if len(shallower) >= TOC_MIN:
            out = shallower
    return out if len(out) >= TOC_MIN else []


def _toc_html(entries: list[dict]) -> tuple[str, str]:
    """同一份目录排两遍：宽屏是左边跟着滚动的侧栏，窄屏是正文上面默认折起的「目录」。CSS 按屏宽只显示一个。
    返回 (侧栏, 顶上折起的)。"""
    if not entries:
        return "", ""
    top = min(e["level"] for e in entries)
    items = "".join(
        f'<li class="d{e["level"] - top}"><a href="#{html.escape(e["id"])}">{html.escape(e["text"])}'
        + (f'<span class="sub">{html.escape(e["sub"])}</span>' if e.get("sub") else "")
        + "</a></li>" for e in entries)
    lst = f'<ol class="toc-list">{items}</ol>'
    return (f'<nav class="toc-side" aria-label="目录"><p class="toc-h">目录</p>{lst}</nav>',
            f'<details class="toc-top"><summary>目录 · {len(entries)} 节</summary>'
            f'<nav aria-label="目录（折叠）">{lst}</nav></details>')


# ---------------------------------------------------------------- 页面

# 顶栏「Aa」：阅读偏好。按钮上的 data-* 由 reader.js 读取，写到 <html> 的同名属性上
_THEMES = [("auto", "自动"), ("kami", "羊皮纸"), ("white", "白"), ("sepia", "米黄"), ("gray", "灰"), ("night", "夜间")]
_FONTS = [("serif", "宋体"), ("sans", "黑体"), ("kai", "楷体")]
_WIDTHS = [("narrow", "窄"), ("normal", "中"), ("wide", "宽")]


def _choices(key: str, items: list[tuple[str, str]], cls: str = "") -> str:
    return "".join(f'<button type="button" class="opt {cls}" data-{key}="{v}" aria-pressed="false">{t}</button>'
                   for v, t in items)


_SETTINGS = (
    '<button type="button" class="btn" id="aa" aria-expanded="false" aria-controls="prefs" '
    'aria-label="阅读设置">Aa</button>'
    '<div id="prefs" class="prefs" hidden role="dialog" aria-label="阅读设置">'
    f'<div class="row swatches" role="group" aria-label="配色">{_choices("theme", _THEMES, "sw")}</div>'
    # 在设置页导入并用上了 Typora / Obsidian 主题时才显示（reader.js 填名字）
    '<div class="row custom-theme" id="custom-theme" hidden>'
    '<span class="ct-now">主题：<b id="ct-name"></b></span>'
    '<button type="button" class="opt" id="ct-off">换回内置配色</button></div>'
    f'<div class="row fonts" role="group" aria-label="字体">{_choices("font", _FONTS)}</div>'
    '<div class="row" role="group" aria-label="字号">'
    '<button type="button" class="opt" data-size="-1" aria-label="缩小字号">A−</button>'
    '<output id="size-now" aria-live="polite"></output>'
    '<button type="button" class="opt" data-size="+1" aria-label="放大字号"><span class="big">A+</span></button></div>'
    f'<div class="row" role="group" aria-label="栏宽">{_choices("width", _WIDTHS)}</div>'
    '<a class="more-prefs" href="/settings#reading">更多字体、导入主题…</a>'
    '</div>'
)


def _page(title: str, crumbs: list[tuple[str, str]], body: str, *, wide: bool = False,
          actions: str = "", toc: list[dict] | None = None) -> str:
    sr = request.script_root
    nav = " <span class=\"sep\">/</span> ".join(
        f'<a href="{html.escape(u)}">{html.escape(t)}</a>' if u else f"<span>{html.escape(t)}</span>"
        for t, u in crumbs)
    side, top = _toc_html(toc or [])
    classes = " ".join(c for c in ("wide" if wide else "", "has-toc" if side else "") if c)
    if side:
        body = f'<div class="with-toc">{side}<div class="content">{top}{body}</div></div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN" class="reader">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title if title == "Spark 阅读" else f"{title} · Spark 阅读")}</title>
<link rel="icon" href="/static/icon.png" />
<link rel="stylesheet" href="/static/common/read-prefs.css" />
<link rel="stylesheet" href="{sr}/static/reader.css" />
<script src="/static/common/read-prefs.js"></script>
<script src="{sr}/static/theme.js"></script>
</head>
<body>
<header class="bar"><nav class="crumbs">{nav}</nav><div class="actions">{actions}<a class="btn" href="/settings">设置</a>{_SETTINGS}</div></header>
<main class="{classes}">
{body}
</main>
<script src="{sr}/static/reader.js"></script>
</body>
</html>"""


def _crumbs(rel: str) -> list[tuple[str, str]]:
    out = [("Spark", _url(""))]
    parts = [p for p in rel.split("/") if p]
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        label = p[:-3] if last and p.endswith(".md") else p
        if i == 0 and p == REPORTS:
            label = "笔记洞察报告"
        out.append((label, "" if last else _url("/".join(parts[:i + 1]), is_dir=True)))
    return out


_DATE_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})[ _](.+)$")


def _entry_label(name: str) -> tuple[str, str]:
    stem = name[:-3] if name.endswith(".md") else name
    m = _DATE_PREFIX_RE.match(stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", m.group(4)
    return "", stem


def _count_md(path: str) -> tuple[int, float]:
    n, latest = 0, 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.endswith(".md") and not f.startswith("."):
                n += 1
                latest = max(latest, os.path.getmtime(os.path.join(dirpath, f)))
    return n, latest


# 节目文件夹里按类型分的子目录
_KIND_DIRS = {"notes": "笔记", "speech": "整理稿", "transcripts": "文字记录", "topics": "专题"}
_INLINE_MD_RE = re.compile(r"\*\*|==|`")


def _file_info(path: str) -> tuple[str, str]:
    """(可读的标题, 日期)。标题取正文第一个「# 」，没有就用 frontmatter 的 title，再没有用文件名；
    日期取文件名前缀 YYYYMMDD，没有就用 frontmatter 的 播出 / date。只读文件开头一段。"""
    date, label = _entry_label(os.path.basename(path))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except OSError:
        return label, date
    meta, body = _split_frontmatter(head)
    fields = {k: _plain(v) for k, v in meta}
    m = re.search(r"^# (.+)$", body, re.M)
    title = m.group(1) if m else fields.get("title", "")
    title = _INLINE_MD_RE.sub("", _plain(title)).strip() or label.replace("_", " ")
    date = date or next((fields[k][:10] for k in ("播出", "date") if fields.get(k)), "")
    return title, date


def _row(href: str, title: str, date: str = "", kind: str = "", extra: str = "", cls: str = "") -> str:
    kind_html = f'<span class="kind">{html.escape(kind)}</span>' if kind else ""
    cls_attr = f' class="{cls}"' if cls else ""
    return (f'<li{cls_attr}><a href="{html.escape(href)}">'
            f'<span class="date">{html.escape(date)}</span><span class="name">{html.escape(title)}</span>'
            f'{kind_html}</a>{extra}</li>')


def _listing(path: str, *, skip: tuple[str, ...] = ()) -> str:
    """文件夹里的子目录和 .md。每一项显示可读的标题、日期和类型（笔记 / 整理稿 / 文字记录 / 报告），
    不直接摆文件名。skip 里的名字不列（节目文件夹页上的主页、已经在「单集」里的子目录）。"""
    dirs, files = [], []
    for name in os.listdir(path):
        if name.startswith(".") or name in skip:
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            dirs.append(name)
        elif name.endswith(".md"):
            files.append(name)
    rows = []
    for name in sorted(dirs):
        n, _ = _count_md(os.path.join(path, name))
        if not n:
            continue   # logs/ 之类没有笔记的目录不列
        label = f"{_KIND_DIRS[name]} · {name}/" if name in _KIND_DIRS else f"{name}/"
        rows.append(f'<li class="dir"><a href="{html.escape(_url(_rel(os.path.join(path, name)), is_dir=True))}">'
                    f'<span class="name">{html.escape(label)}</span><span class="count">{n} 篇</span></a></li>')
    is_reports = _in(os.path.realpath(path), _reports_root())
    kind = "报告" if is_reports else _KIND_DIRS.get(os.path.basename(path), "")
    infos = {}
    for name in files:
        full = os.path.join(path, name)
        title, date = _file_info(full)
        if title == os.path.basename(path):
            # 标题和文件夹同名的（主页的旧副本之类）用文件名，免得列表里出现两个一样的
            title = _entry_label(name)[1]
        if is_reports and not date:
            date = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(full)))
        infos[name] = (title, date)
    if is_reports:   # 报告名字不带日期前缀：按报告日期、再按修改时间，新的在前
        files.sort(key=lambda n: (infos[n][1], os.path.getmtime(os.path.join(path, n))), reverse=True)
    else:            # 带日期的新的在前；会议的 001_、002_ 按顺序
        dated = sorted((n for n in files if _entry_label(n)[0]), reverse=True)
        files = dated + sorted(n for n in files if not _entry_label(n)[0])
    for name in files:
        full = os.path.join(path, name)
        title, date = infos[name]
        deck = full[:-3] + ".deck.html"
        extra = (f'<a class="aside" href="{html.escape(_url(_rel(deck)))}">演示</a>'
                 if os.path.isfile(deck) else "")
        rows.append(_row(_url(_rel(full)), title, date, kind, extra))
    return f'<ul class="list">{"".join(rows)}</ul>' if rows else '<p class="empty">这个文件夹里没有 Markdown 文件。</p>'


# ---- 节目 / 会议文件夹里的「单集」：同一期的笔记、整理稿、文字记录并成一行

_EP_KEY_RE = re.compile(r"^(\d{3,8})[ _](.+)$")
_EP_FILES = (("note_relative_path", "notes", "笔记"), ("speech_relative_path", "speech", "整理稿"),
             ("relative_path", "transcripts", "文字记录"))


def _ep_key(name: str) -> tuple[str, str]:
    stem = name[:-3] if name.endswith(".md") else name
    m = _EP_KEY_RE.match(stem)
    prefix, rest = (m.group(1), m.group(2)) if m else ("", stem)
    return prefix, re.sub(r"[^\w]+", "", rest.lower())


def _episodes(show: str) -> list[dict]:
    """[{title, date, files: [(类型, 路径)]}]，新的在前。先按 .manifest.json 认一期有哪些文件，
    manifest 里没有的再按文件名（日期或序号前缀 + 标题）归到一起。"""
    root = os.path.realpath(show)
    eps: list[dict] = []
    seen: set[str] = set()
    try:
        with open(os.path.join(show, ".manifest.json"), encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("entries") or {}
    except (OSError, ValueError, AttributeError):
        rows = {}
    for row in (rows.values() if isinstance(rows, dict) else []):
        if not isinstance(row, dict):
            continue
        files = []
        for key, _sub, label in _EP_FILES:
            rp = row.get(key)
            p = os.path.realpath(os.path.join(show, rp)) if isinstance(rp, str) and rp else ""
            if p and _in(p, root) and os.path.isfile(p) and p not in seen:
                files.append((label, p))
                seen.add(p)
        if not files:
            continue
        entry = row.get("entry") if isinstance(row.get("entry"), dict) else {}
        pd = str(entry.get("publish_date") or "")
        date = f"{pd[:4]}-{pd[4:6]}-{pd[6:8]}" if re.fullmatch(r"\d{8}", pd) else ""
        eps.append({"title": str(entry.get("title") or ""), "date": date, "files": files,
                    "key": _ep_key(os.path.basename(files[0][1]))})
    by_key = {e["key"]: e for e in eps}
    for _key, sub, label in _EP_FILES:
        d = os.path.join(show, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.realpath(os.path.join(d, name))
            if name.startswith(".") or not name.endswith(".md") or p in seen or not os.path.isfile(p):
                continue
            seen.add(p)
            k = _ep_key(name)
            if k not in by_key:
                by_key[k] = {"title": "", "date": "", "files": [], "key": k}
                eps.append(by_key[k])
            by_key[k]["files"].append((label, p))
    order = {label: i for i, (_k, _s, label) in enumerate(_EP_FILES)}
    for e in eps:
        e["files"].sort(key=lambda f: order[f[0]])
        prefix = e["key"][0]
        if not e["date"] and len(prefix) == 8:
            e["date"] = f"{prefix[:4]}-{prefix[4:6]}-{prefix[6:]}"
        if not e["title"]:
            e["title"], date = _file_info(e["files"][0][1])
            e["date"] = e["date"] or date
    dated = sorted((e for e in eps if e["date"]), key=lambda e: (e["date"], e["key"]), reverse=True)
    rest = sorted((e for e in eps if not e["date"]), key=lambda e: e["key"])
    return dated + rest


def _episode_list(eps: list[dict]) -> str:
    rows = []
    for e in eps:
        kinds = "".join(f'<a href="{html.escape(_url(_rel(p)))}">{label}</a>' for label, p in e["files"])
        rows.append(_row(_url(_rel(e["files"][0][1])), e["title"], e["date"], "",
                         f'<span class="kinds">{kinds}</span>', "ep"))
    return f'<ul class="list episodes-list">{"".join(rows)}</ul>'


@app.route("/")
def index():
    root = _root()
    if not os.path.isdir(root):
        return _page("Spark 阅读", [("Spark", "")],
                     f'<p class="empty">找不到笔记库里的 Spark 目录：{html.escape(root)}</p>'), 404
    shows = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        n, latest = _count_md(full)
        if n:
            shows.append((latest, name, n))
    shows.sort(reverse=True)
    rows = "".join(
        f'<li class="dir"><a href="{html.escape(_url(name, is_dir=True))}"><span class="name">{html.escape(name)}</span>'
        f'<span class="count">{n} 篇 · {time.strftime("%Y-%m-%d", time.localtime(latest))} 更新</span></a></li>'
        for latest, name, n in shows)
    loose = sorted(f for f in os.listdir(root) if f.endswith(".md") and not f.startswith("."))
    loose_html = "".join(f'<li><a href="{html.escape(_url(f))}"><span class="name">{html.escape(f[:-3])}</span></a></li>'
                         for f in loose)
    reports = _reports_root()
    if os.path.isdir(reports):
        n, latest = _count_md(reports)
        if n:
            rows = (f'<li class="dir"><a href="{html.escape(_url(REPORTS, is_dir=True))}"><span class="name">笔记洞察报告</span>'
                    f'<span class="count">{n} 篇 · {time.strftime("%Y-%m-%d", time.localtime(latest))} 更新</span></a></li>'
                    + rows)
    body = (f'<h1>Spark 阅读</h1><p class="lede">笔记库 Spark 目录下的会议、播客和栏目，按最近更新排列；'
            f'笔记洞察生成的报告和演示在第一项。</p>'
            f'<ul class="list">{rows}</ul>')
    if loose_html:
        body += f'<h2>其他文件</h2><ul class="list">{loose_html}</ul>'
    return _page("Spark 阅读", [("Spark", "")], body)


@app.route("/f/")
def root_redirect():
    return redirect(_url(""))


@app.route("/open")
def open_path():
    """别的工具只知道文件的绝对路径（比如笔记洞察刚写好的报告、演示），从这里跳到阅读页。"""
    raw = os.path.realpath(os.path.expanduser(request.args.get("path") or ""))
    for base in (_reports_root(), _root()):
        if _in(raw, base) and os.path.isfile(raw) and not any(
                p.startswith(".") for p in os.path.relpath(raw, os.path.realpath(base)).split(os.sep)):
            return redirect(_url(_rel(raw)))
    return _page("打不开", [("Spark", _url(""))],
                 '<h1>阅读页打不开这个文件</h1><p class="lede">阅读页只读笔记库里 Spark 目录和 output 目录下的文件。'
                 '笔记洞察的输出目录改到别处的话，请直接在 Obsidian 或浏览器里打开。</p>'), 404


@app.route("/r/")
@app.route("/r/<path:rel>")
def view_report(rel: str = ""):
    if rel.endswith(".deck.html"):
        return _deck_page(rel)
    if ".deck.assets/" in rel or rel.endswith(".deck.assets"):
        return _deck_asset(rel)
    return view(f"{REPORTS}/{rel}")


_ASSET_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _deck_asset(rel: str):
    """演示的配图：x.deck.assets/slide-N.png，演示里用相对路径引用。只给报告目录里、
    紧挨着一份同名 .deck.html 的那个文件夹里的图片文件，别的（包括这个文件夹里的
    非图片、子目录、跳出去的路径）一律 404。"""
    path = _resolve(f"{REPORTS}/{rel}")
    if not path or not os.path.isfile(path) or not _in(path, _reports_root()):
        abort(404)
    folder = os.path.dirname(path)
    ext = os.path.splitext(path)[1].lower()
    if not folder.endswith(".deck.assets") or ext not in _ASSET_EXTS \
            or not os.path.isfile(folder[:-len(".assets")] + ".html"):
        abort(404)
    return send_file(path, mimetype=_ASSET_EXTS[ext])


@app.route("/f/<path:rel>")
def view(rel: str):
    path = _resolve(rel)
    if not path or not os.path.exists(path):
        abort(404)
    rel = _rel(path)
    if os.path.isdir(path):
        if not request.path.endswith("/"):
            return redirect(_url(rel, is_dir=True))
        if rel == REPORTS:
            return _page("笔记洞察报告", _crumbs(rel), f"<h1>笔记洞察报告</h1>{_listing(path)}")
        return _dir_page(path, rel)
    if not path.endswith(".md"):
        return send_file(path)
    return _file_page(path, rel)


def _dir_page(path: str, rel: str):
    name = os.path.basename(path)
    home = os.path.join(path, name + ".md")
    has_home = os.path.isfile(home)
    eps = _episodes(path) if has_home or os.path.isfile(os.path.join(path, ".manifest.json")) else []
    if not has_home and not eps:
        kind = _KIND_DIRS.get(name)
        heading = f"{os.path.basename(os.path.dirname(path))} · {kind}" if kind else name
        return _page(heading, _crumbs(rel), f"<h1>{html.escape(heading)}</h1>{_listing(path)}")
    # 节目 / 会议文件夹：先是单集列表，再是主页（节目总结），其余文件放最后。
    # 主页里「全部单集」那一节和上面的列表重复，不再排；主页本身也不在文件列表里重复出现。
    view = "home" if eps else ""
    title, body, heads = _doc(home, view=view) if has_home else (name, "", [])
    title = title or name
    parts = []
    if eps:
        parts.append(f"<h1>{html.escape(title)}</h1>"
                     f'<section id="episodes" class="episodes"><h2>单集<span class="count">{len(eps)} 期</span></h2>'
                     f"{_episode_list(eps)}</section>")
        heads = [{"level": 2, "id": "episodes", "text": "单集", "sub": ""}] + heads
    if has_home:
        parts.append(_article(home, body, view=view))
    listing = _listing(path, skip=(name + ".md",))
    if 'class="empty"' not in listing:
        parts.append(f'<section class="files"><h2>文件夹</h2>{listing}</section>')
    actions = _actions(_rel(home)) if has_home else ""
    return _page(title, _crumbs(rel), "".join(parts), actions=actions, toc=_toc_entries(heads))


_HOME_DUP_RE = re.compile(r'<h2 id="[^"]*">(?:全部单集|议题列表)[^<]*</h2>.*?(?=<h[12][ >]|\Z)', re.S)
_FIRST_H1_RE = re.compile(r'<h1 id="[^"]*">(?:(?!</h1>).)*</h1>\n?', re.S)


def _doc(path: str, *, view: str = "") -> tuple[str, str, list[dict]]:
    """读一篇排成 (标题, 正文 HTML, 标题列表)。view="home"：节目文件夹页上的主页，页面自己出标题和单集列表，
    这里去掉主页的大标题和「全部单集 / 议题列表」一节。页面和高亮后换上的正文都走这里，两边一致。"""
    rel = _rel(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    title, out, heads = _render(text, os.path.dirname(path), bilingual="/speech/" in f"/{rel}",
                                date_hint=_entry_label(os.path.basename(path))[0])
    if view == "home":
        out = _HOME_DUP_RE.sub("", _FIRST_H1_RE.sub("", out, count=1))
        heads = [h for h in heads if f' id="{h["id"]}"' in out]
    return title, out, heads


def _actions(rel: str) -> str:
    vault = core_vault.vault_root()
    path = _resolve(rel) or ""
    file_in_vault = os.path.relpath(path, os.path.realpath(vault)).replace(os.sep, "/")
    obs = "obsidian://open?" + urllib.parse.urlencode(
        {"vault": os.path.basename(vault.rstrip("/")), "file": file_in_vault}, quote_via=urllib.parse.quote)
    out = f'<a class="btn" href="{html.escape(obs)}">在 Obsidian 打开</a>'
    deck = path[:-3] + ".deck.html" if path.endswith(".md") else ""
    if deck and os.path.isfile(deck):
        out = f'<a class="btn" href="{html.escape(_url(_rel(deck)))}">看演示</a>' + out
    return out


def _file_page(path: str, rel: str):
    title, body, heads = _doc(path)
    wide = 'class="pair"' in body
    return _page(title or os.path.basename(path)[:-3], _crumbs(rel),
                 _article(path, body), wide=wide, actions=_actions(rel), toc=_toc_entries(heads))


def _mtime(path: str) -> str:
    return str(os.stat(path).st_mtime_ns)


def _article(path: str, body: str, *, view: str = "") -> str:
    """正文外面这一层带上高亮 / 摘录要用的信息：哪个文件、打开时的修改时间、按哪种排法（高亮后重排正文要一致）。"""
    rel = _rel(path)
    excerptable = "false" if rel == EXCERPTS_NAME else "true"
    view_attr = f' data-view="{view}"' if view else ""
    return (f'<article class="doc" data-path="{html.escape(rel)}" data-mtime="{_mtime(path)}" '
            f'data-api="{request.script_root}/api" data-excerptable="{excerptable}"{view_attr}>{body}</article>')


def _render_body(path: str, view: str = "") -> str:
    if view == "home" and os.path.basename(path) != os.path.basename(os.path.dirname(path)) + ".md":
        view = ""
    return _doc(path, view=view)[1]


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)



# ---------------------------------------------------------------- 高亮 / 摘录

class AnnotateError(Exception):
    pass


# 页面上的文字和 Markdown 源码之间差着格式符号：选中的 "foo bar" 在源码里可能是
# "foo **bar**"，也可能在软换行处断成两行（引用块里下一行还带 "> "）
# 写成字符类而不是 (?:\*\*|__|…|[*_`])*：后者里 ** 和 * 互相覆盖，遇到正文里一长串
# "********" 分隔线匹配失败时会指数级回溯，40 个星号就要几秒
_MARKUP = r"[*_`~=]*"
_WS = r"(?:[ \t]+|[ \t]*\n[ \t]*(?:>[ \t]*)?)"
_NORM_STRIP_RE = re.compile(r"[\s*_`~=#>\[\]-]+")


def _selection_regex(text: str) -> re.Pattern:
    parts: list[str] = []
    for ch in text.strip():
        if ch.isspace():
            if parts and parts[-1] != _WS:
                parts.append(_WS)
        else:
            parts.append(re.escape(ch))
    return re.compile(_MARKUP.join(parts))


def _norm(s: str) -> str:
    return _NORM_STRIP_RE.sub("", s)


def _common_suffix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _find_span(body: str, text: str, before: str) -> tuple[int, int]:
    """在正文源码里找到选中的那一段。同样的文字出现好几次时，按选区前面的文字挑最像的。"""
    if "\n" in text.strip():
        raise AnnotateError("选中的文字跨了段落，高亮只能在一段里面")
    matches = list(_selection_regex(text).finditer(body))
    if not matches:
        raise AnnotateError("这段跨了链接或特殊格式，在原文里对不上，没法高亮")
    ctx = _norm(before)[-60:]
    best = max(matches, key=lambda m: _common_suffix(_norm(body[max(0, m.start() - 400):m.start()]), ctx))
    a, b = best.start(), best.end()
    # 选区正好从加粗 / 代码的第一个字开始（或到最后一个字结束）时，旁边的格式符号没包进来，
    # 往外扩一格把它带上
    for t in ("**", "__", "~~", "`"):
        if _unbalanced(body[a:b], t):
            if body[max(0, a - len(t)):a] == t:
                a -= len(t)
            elif body[b:b + len(t)] == t:
                b += len(t)
    span = body[a:b]
    if "==" in span or body[max(0, a - 2):a] == "==":
        raise AnnotateError("这段已经高亮过了（或者和已有的高亮重叠）")
    if any(_unbalanced(span, t) for t in ("**", "__", "~~", "`", "*")):
        raise AnnotateError("选区只包住了半个格式（比如加粗的一半），换个起止点再试")
    return a, b


def _unbalanced(span: str, token: str) -> bool:
    if token in ("`", "*"):
        span = span.replace("**", "").replace("__", "").replace("~~", "")
    return span.count(token) % 2 == 1


def _read_checked(path: str, mtime: str) -> tuple[str, str, int]:
    """读文件，核对页面打开时的修改时间。返回 (全文, 正文, 正文在全文里的起点)。"""
    if mtime and _mtime(path) != str(mtime):
        raise AnnotateError("这篇在别处改过了（比如 Obsidian 里），刷新页面后再试")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = _FRONTMATTER_RE.match(text)
    start = m.end() if m else 0
    return text, text[start:], start


def _highlight(path: str, mtime: str, text: str, before: str) -> str:
    """给选中的文字加 ==...==，返回加了高亮的那段源码（摘录时照原样引用）。"""
    full, body, off = _read_checked(path, mtime)
    a, b = _find_span(body, text, before)
    sec = body.find(core_vault.HIGHLIGHTS_HEADING + "\n")
    if sec >= 0 and a > sec:
        raise AnnotateError("这里是高亮汇总；要取消某条，到原文里点那处高亮")
    span = body[a:b]
    atomic.write_text(path, full[:off + a] + "==" + span + "==" + full[off + b:])
    _record_highlight(path, span)
    return span


# ---- 汇总到这一期的笔记（notes/ 里）

_KIND_LABELS = {"speech_relative_path": "整理稿", "relative_path": "文字记录"}


def _episode_note(path: str) -> tuple[str, str] | None:
    """path 是某一期的笔记 / 整理稿 / 文字记录时，返回 (这一期笔记的路径, 来源说明)。
    来源说明：笔记自己是空串，其它是「整理稿」「文字记录」。靠节目文件夹里的 .manifest.json 认。"""
    real = os.path.realpath(path)
    if _in(real, _reports_root()) and real.endswith(".md"):
        return real, ""   # 笔记洞察的报告：汇总在报告自己末尾
    root = os.path.realpath(_root())
    d = os.path.dirname(real)
    while d.startswith(root + os.sep):
        manifest = os.path.join(d, ".manifest.json")
        if os.path.isfile(manifest):
            try:
                with open(manifest, encoding="utf-8") as f:
                    rows = (json.load(f) or {}).get("entries") or {}
            except (OSError, ValueError):
                return None
            for row in rows.values():
                note = row.get("note_relative_path") if isinstance(row, dict) else None
                if not note:
                    continue
                note_path = os.path.realpath(os.path.join(d, note))
                # manifest 和笔记一起放在会同步的库里，谁都能改；记的路径必须还在库内，
                # 不然一次高亮就能往库外任意文件末尾追加内容
                if not os.path.isfile(note_path) or not _in(note_path, root):
                    continue
                if note_path == real:
                    return note_path, ""
                for key, label in _KIND_LABELS.items():
                    other = row.get(key)
                    if other and os.path.realpath(os.path.join(d, other)) == real:
                        return note_path, label
            return None
        d = os.path.dirname(d)
    return None


def _flat(span: str) -> str:
    """高亮源码压成一行：软换行、引用块的 "> " 都去掉。"""
    return re.sub(r"[ \t]*\n[ \t]*(?:>[ \t]*)?", " ", span).strip()


def _section_bounds(text: str) -> tuple[int, int] | None:
    """「我的高亮」一节在全文里的起止（不含末尾空行）。"""
    m = core_vault.HIGHLIGHTS_SECTION_RE.search(text)
    return (m.start(), m.start() + len(m.group(0).rstrip())) if m else None


def _add_summary_line(note: str, line: str) -> None:
    with open(note, encoding="utf-8") as f:
        text = f.read()
    bounds = _section_bounds(text)
    if bounds:
        a, b = bounds
        if line in text[a:b].splitlines():
            return
        text = text[:b] + "\n" + line + text[b:]
    else:
        text = text.rstrip("\n") + f"\n\n{core_vault.HIGHLIGHTS_HEADING}\n\n{line}\n"
    atomic.write_text(note, text)


def _remove_summary_line(note: str, match) -> bool:
    """删掉汇总里第一条 match(行) 为真的；删空了标题一起去掉。"""
    with open(note, encoding="utf-8") as f:
        full = f.read()
    bounds = _section_bounds(full)
    if not bounds:
        return False
    a, b = bounds
    lines = full[a:b].split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("- ") and match(ln):
            del lines[i]
            break
    else:
        return False
    if any(ln.startswith("- ") for ln in lines):
        full = full[:a] + "\n".join(lines) + full[b:]
    else:
        full = full[:a].rstrip("\n") + "\n" + full[b:].lstrip("\n")
    atomic.write_text(note, full)
    return True


def _record_highlight(path: str, span: str) -> None:
    hit = _episode_note(path)
    if not hit:
        return
    note, label = hit
    line = f"- {_flat(span)}"
    if label:
        link = os.path.relpath(path, os.path.dirname(note)).replace(os.sep, "/")
        line += f"（[{label}](<{link}>)）"
    _add_summary_line(note, line)


_SOURCE_SUFFIX_RE = re.compile(r"（\[[^\]]*\]\(<[^>]*>\)）$")


def _forget_highlight(path: str, text: str) -> None:
    hit = _episode_note(path)
    if not hit:
        return
    want = _norm(text)
    _remove_summary_line(hit[0], lambda ln: not _DECK_LINE_RE.match(ln)
                         and _norm(_SOURCE_SUFFIX_RE.sub("", ln[2:])) == want)


# ---- 演示上的高亮：记在对应报告的汇总里

_DECK_LINE_RE = re.compile(r"^- (.*)（\[演示第 (\d+) 页\]\(<[^>]*>\)）$")


def _deck_report(deck_rel: str) -> tuple[str, str]:
    """(演示文件, 对应报告)。演示和报告同名：x.deck.html ↔ x.md。"""
    deck = _resolve(deck_rel)
    if not deck or not deck.endswith(".deck.html") or not os.path.isfile(deck) \
            or not _in(deck, _reports_root()):
        raise AnnotateError("找不到这份演示")
    report = deck[:-len(".deck.html")] + ".md"
    if not os.path.isfile(report):
        raise AnnotateError("找不到这份演示对应的报告（同名 .md），高亮没地方存")
    return deck, report


def _deck_highlights(report: str) -> list[dict]:
    with open(report, encoding="utf-8") as f:
        text = f.read()
    bounds = _section_bounds(text)
    if not bounds:
        return []
    out = []
    for ln in text[bounds[0]:bounds[1]].split("\n"):
        m = _DECK_LINE_RE.match(ln)
        if m:
            out.append({"slide": int(m.group(2)), "text": m.group(1)})
    return out


def _deck_page(rel: str):
    try:
        deck, _report = _deck_report(f"{REPORTS}/{rel}")
    except AnnotateError:
        abort(404)
    with open(deck, encoding="utf-8") as f:
        page = f.read()
    sr = request.script_root
    inject = (f'<link rel="stylesheet" href="{sr}/static/deck-annotate.css" />'
              f'<script src="{sr}/static/deck-annotate.js" data-api="{sr}/api" '
              f'data-deck="{html.escape(_rel(deck))}" data-report="{html.escape(_url(_rel(_report)))}"></script>')
    i = page.lower().rfind("</body>")
    page = page[:i] + inject + page[i:] if i >= 0 else page + inject
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


_MARK_RE = re.compile(r"==(?=\S)([^\n]*?\S)==")


def _unhighlight(path: str, mtime: str, text: str, nth: int) -> None:
    full, body, off = _read_checked(path, mtime)
    want = _norm(text)
    hits = [m for m in _MARK_RE.finditer(body) if _norm(m.group(1)) == want]
    if not hits:
        raise AnnotateError("在原文里找不到这处高亮，刷新页面后再试")
    m = hits[min(max(nth, 0), len(hits) - 1)]
    atomic.write_text(path, full[:off + m.start()] + m.group(1) + full[off + m.end():])
    _forget_highlight(path, m.group(1))


def _doc_title(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r"^# (.+)$", _split_frontmatter(f.read())[1], re.M)
    return m.group(1).strip() if m else os.path.basename(path)[:-3]


_EXCERPTS_HEAD = "# 摘录\n\n读的时候摘下来的段落，新的在上面。每条带出处，点链接回到原文。\n"


def _add_excerpt(path: str, quote: str, thought: str) -> str:
    rel = _rel(path)
    show = rel.split("/")[0] if "/" in rel else ""
    title = _doc_title(path)
    label = f"{show} · {title}" if show and show != title else title
    target = f"{core_vault.SPARK_DIRNAME}/{rel[:-3]}"
    lines = [ln.rstrip() for ln in quote.strip().splitlines()]
    quoted = "\n".join(f"> {ln}" if ln else ">" for ln in lines)
    entry = (f"## {time.strftime('%Y-%m-%d %H:%M')} · {title}\n\n{quoted}\n\n"
             f"出处：[[{target}|{label.replace('|', '｜').replace(']', '］')}]]\n")
    if thought.strip():
        entry += f"\n想法：{thought.strip()}\n"
    out = os.path.join(_root(), EXCERPTS_NAME)
    existing = ""
    if os.path.isfile(out):
        with open(out, encoding="utf-8") as f:
            existing = f.read()
    if not existing.strip():
        existing = _EXCERPTS_HEAD
    i = existing.find("\n## ")
    new = (existing[:i + 1] + entry + "\n" + existing[i + 1:]) if i >= 0 else existing.rstrip("\n") + "\n\n" + entry
    atomic.write_text(out, new)
    return EXCERPTS_NAME


def _annotate_target(data: dict) -> str:
    path = _resolve(str(data.get("path") or ""))
    if not path or not path.endswith(".md") or not os.path.isfile(path):
        raise AnnotateError("找不到这个文件")
    return path


@app.route("/api/highlight", methods=["POST"])
def api_highlight():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")
    try:
        path = _annotate_target(data)
        if not text.strip() or len(text) > MAX_SELECTION_CHARS:
            raise AnnotateError("选中的文字是空的，或者太长了")
        if data.get("in_summary"):
            raise AnnotateError("这里是高亮汇总；要取消某条，到原文里点那处高亮")
        if data.get("remove"):
            _unhighlight(path, str(data.get("mtime") or ""), text, int(data.get("nth") or 0))
        else:
            _highlight(path, str(data.get("mtime") or ""), text, str(data.get("before") or ""))
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path, str(data.get("view") or ""))})


@app.route("/api/deck_highlights")
def api_deck_highlights():
    try:
        _deck, report = _deck_report(request.args.get("deck") or "")
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"items": _deck_highlights(report)})


@app.route("/api/deck_highlight", methods=["POST"])
def api_deck_highlight():
    data = request.get_json(silent=True) or {}
    text = " ".join(str(data.get("text") or "").split())
    try:
        slide = int(data.get("slide") or 0)
    except (TypeError, ValueError):
        slide = 0
    try:
        deck, report = _deck_report(str(data.get("deck") or ""))
        if not text or len(text) > MAX_SELECTION_CHARS or slide < 1:
            raise AnnotateError("选中的文字是空的，或者太长了")
        if data.get("remove"):
            want = _norm(text)
            _remove_summary_line(report, lambda ln: bool((m := _DECK_LINE_RE.match(ln)))
                                 and int(m.group(2)) == slide and _norm(m.group(1)) == want)
        else:
            link = f"{os.path.basename(deck)}#{slide}"
            _add_summary_line(report, f"- {text}（[演示第 {slide} 页](<{link}>)）")
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "items": _deck_highlights(report)})


@app.route("/api/excerpt", methods=["POST"])
def api_excerpt():
    """摘录：先试着在原文里高亮这段（引用时用带格式的原文），对不上也照样存摘录。"""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "")
    thought = str(data.get("thought") or "")[:2000]
    try:
        path = _annotate_target(data)
        if _rel(path) == EXCERPTS_NAME:
            raise AnnotateError("这里就是摘录本身")
        if not text.strip() or len(text) > MAX_SELECTION_CHARS:
            raise AnnotateError("选中的文字是空的，或者太长了")
        quote, note = text, ""
        try:
            quote = _highlight(path, str(data.get("mtime") or ""), text, str(data.get("before") or ""))
        except AnnotateError as e:
            if "改过了" in str(e):
                raise
            if "已经高亮过了" not in str(e):
                note = f"摘录存好了，但没加高亮：{e}"
        saved = _add_excerpt(path, quote, thought)
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path, str(data.get("view") or "")),
                    "note": note, "excerpts_url": _url(saved)})


# ---------------------------------------------------------------- 导入的主题

@app.route("/themes/<name>")
def theme_css(name):
    m = re.fullmatch(r"(t[0-9a-f]{10})\.css", name)
    path = reader_themes.css_path(m.group(1)) if m else None
    if not path:
        abort(404)   # 删掉了或者 id 不对：页面上 theme.js 收到加载失败，退回内置配色
    resp = send_file(path, mimetype="text/css", max_age=0, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _theme_list() -> list[dict]:
    return [{**t, "summary": reader_themes.summary_text(t)} for t in reader_themes.list_themes()]


@app.route("/api/themes", methods=["GET", "POST"])
def api_themes():
    """GET：导入过的主题。POST {"css": "...", "name": "...", "filename": "..."}：清洗后存下。
    跨站 POST 由 web_guard 挡掉。"""
    if request.method == "GET":
        return jsonify({"themes": _theme_list(), "max_bytes": reader_themes.MAX_CSS_BYTES})
    # JSON 里转义会让体积变大一些，这里只挡明显超大的请求，CSS 本身的上限在 sanitize 里查
    if (request.content_length or 0) > reader_themes.MAX_CSS_BYTES * 3:
        return jsonify({"error": "文件太大，最多 1 MB"}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("css"), str):
        return jsonify({"error": "请求格式不对，应该是 {\"css\": \"…\"}"}), 400
    try:
        entry = reader_themes.import_theme(data["css"], name=str(data.get("name") or ""),
                                           filename=str(data.get("filename") or ""))
    except reader_themes.ThemeError as e:
        rep = e.report
        return jsonify({"error": str(e), "report": rep}), 400
    except OSError as e:
        return jsonify({"error": f"保存主题失败：{e}"}), 500
    return jsonify({"ok": True, "theme": {**entry, "summary": reader_themes.summary_text(entry)},
                    "themes": _theme_list()})


@app.route("/api/themes/<theme_id>", methods=["DELETE"])
@app.route("/api/themes/<theme_id>/delete", methods=["POST"])
def api_theme_delete(theme_id: str):
    if not reader_themes.valid_id(theme_id):
        return jsonify({"error": "主题 id 不对"}), 400
    try:
        if not reader_themes.delete_theme(theme_id):
            return jsonify({"error": "没有这个主题"}), 404
    except OSError as e:
        return jsonify({"error": f"删除失败：{e}"}), 500
    return jsonify({"ok": True, "themes": _theme_list()})


if __name__ == "__main__":
    app.run("127.0.0.1", 8766, debug=False, threaded=True)
