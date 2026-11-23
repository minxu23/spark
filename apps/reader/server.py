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
  整理稿或文字记录的话，再在这一期 notes/ 里的小结末尾「我的高亮」记一条。小结、整理稿、
  专题整篇重新生成时，划线、批注、读后感由 core.annotations.carry_over 接回新内容；
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
import threading
import time
import urllib.parse
from datetime import datetime

import mistune
from flask import Flask, abort, jsonify, redirect, request, send_file, send_from_directory
from mistune.renderers.html import HTMLRenderer

from apps.reader import themes as reader_themes
from core import annotations as core_notes
from core import atomic
from core import clip_capture
from core import remote_tasks
from core import clips as core_clips
from core import doctime
from core import jottings as core_jottings
from core import summit_dates
from core import readlog as core_readlog
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
    try:
        path = os.path.realpath(os.path.join(root, *parts))
    except ValueError:   # 路径里带 %00 这类空字节
        return None
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

# ---- 公式：$…$ 和 $$…$$ 先换成占位符，免得下标的 _ 被当成斜体；渲染完再换回来，浏览器里由 KaTeX 排版。
# 文字稿里美元金额很多（「$5-$10」「$100BN … $」），行内公式只认像公式的：
# 开头 $ 前面不是字母数字，后面不是空格；结尾 $ 前面不是空格，后面不是字母数字；
# 内容不跨行、没有汉字，且带 \ ^ _ = { 之一，或者是 n、(k, v) 这种很短的变量写法。代码里的不动。
_CODE_OR_MATH_RE = re.compile(
    r"(?P<code>^```.*?^```[ \t]*$|`[^`\n]+`)"
    r"|\$\$(?P<block>[^$]+?)\$\$"
    r"|(?<![\w.$\\])\$(?P<inline>[^\s$](?:[^$\n]{0,298}[^\s$\\])?)\$(?![\w$])",
    re.S | re.M)
_MATHY_RE = re.compile(r"[\\^_={]")
_SHORT_VAR_RE = re.compile(r"[A-Za-z0-9 ,+\-*/<>()|.']{1,10}")
_MATH_KEY_RE = re.compile(r"SPARKMATH(\d+)X")


def _looks_math(tex: str) -> bool:
    if re.search(r"[一-鿿]", tex):
        return False
    if _MATHY_RE.search(tex):
        return True
    return bool(_SHORT_VAR_RE.fullmatch(tex)) and not re.search(r"[A-Za-z]{3}", tex)


def _take_math(body: str) -> tuple[str, list[tuple[bool, str]]]:
    maths: list[tuple[bool, str]] = []

    def alone(m) -> bool:
        s = m.string
        start = s.rfind("\n", 0, m.start()) + 1
        end = s.find("\n", m.end())
        return not s[start:m.start()].strip() and not s[m.end():end if end >= 0 else len(s)].strip()

    def sub(m):
        if m.group("code"):
            return m.group(0)
        block = m.group("block")
        tex = block if block is not None else m.group("inline")
        if not tex.strip() or "\n\n" in tex or (block is None and not _looks_math(tex)):
            return m.group(0)
        maths.append((block is not None, tex.strip()))
        key = f"SPARKMATH{len(maths) - 1}X"
        # 单独成行的 $$…$$ 自成一段
        return f"\n\n{key}\n\n" if block is not None and alone(m) else key

    return _CODE_OR_MATH_RE.sub(sub, body), maths


def _put_math(out: str, maths: list[tuple[bool, str]]) -> str:
    if not maths:
        return out

    def sub(m):
        display, tex = maths[int(m.group(1))]
        if display:
            return f'<div class="math">$${html.escape(tex)}$$</div>'
        return f'<span class="math">\\({html.escape(tex)}\\)</span>'

    # 自成一段的公式块不要再套一层 <p>
    out = re.sub(r"<p>(SPARKMATH\d+X)</p>", lambda m: m.group(1), out)
    return _MATH_KEY_RE.sub(sub, out)


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


# ---- 批注：Obsidian 脚注「==划的字==[^note-3]」，文末「[^note-3]: 批注」（同手机 DocEdit 的批注）。
# 页面上脚注号换成一个不带字的小气泡（选字、找原文都不受影响），文末那几行不显示，点气泡看、改、删。
_NOTE_DEF_RE = re.compile(r"^\[\^note-(\d+)\]:[ \t]?(.*)$", re.M)
_NOTE_REF_RE = re.compile(r"\[\^note-(\d+)\](?!:)")
_NOTE_KEY_RE = re.compile(r"SPARKNOTE(\d+)X")


def _note_defs(text: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2) for m in _NOTE_DEF_RE.finditer(text)}


def _take_notes(body: str) -> tuple[str, dict[int, str]]:
    if "[^note-" not in body:
        return body, {}
    notes = _note_defs(body)
    body = re.sub(r"^\[\^note-\d+\]:.*\n?", "", body, flags=re.M)
    return _NOTE_REF_RE.sub(lambda m: f"SPARKNOTE{m.group(1)}X" if int(m.group(1)) in notes else "", body), notes


def _put_notes(out: str, notes: dict[int, str]) -> str:
    if not notes:
        return out

    def bubble(m: re.Match) -> str:
        nid = int(m.group(1))
        return (f'<button type="button" class="note-ref" data-note="{nid}" data-text="{html.escape(notes[nid])}" '
                f'aria-label="批注：{html.escape(notes[nid])}" title="{html.escape(notes[nid])}"></button>')
    return _NOTE_KEY_RE.sub(bubble, out)


def render_markdown(text: str, here_dir: str, *, bilingual: bool = False, date_hint: str = "") -> tuple[str, str]:
    """返回 (标题, 正文 HTML)。"""
    title, out, _heads = _render(text, here_dir, bilingual=bilingual, date_hint=date_hint)
    return title, out


def _render(text: str, here_dir: str, *, bilingual: bool | None = False,
            date_hint: str = "") -> tuple[str, str, list[dict]]:
    """返回 (标题, 正文 HTML, 标题列表)。标题都带上锚点；开头的元信息收进一个默认折起的 <details>。
    只改排版不动文字：高亮是拿选中的文字回源码里找的，页面上的字要和源码对得上。"""
    meta, body = _split_frontmatter(text)

    def wl(m):
        target = _wikilink_target(m.group(1), here_dir)
        label = (m.group(2) or m.group(1)).strip()
        return f"[{label}](<{_link(target)}>)" if target else label

    body, notes = _take_notes(body)
    body = _WIKILINK_RE.sub(wl, body)
    title_m = re.search(r"^# (.+)$", body, re.M)
    title = title_m.group(1).strip() if title_m else ""
    body, maths = _take_math(body)
    out = _put_notes(_put_math(_md(body), maths), notes)
    if bilingual is None:
        bilingual = _looks_bilingual(out)
    if bilingual:
        out = _PAIR_RE.sub(r'<div class="pair"><div class="orig">\1</div><div class="tr">\3</div></div>', out)
        out = _QUOTE_PAIR_RE.sub(_quote_pair, out)
    out = _TABLE_RE.sub(_tidy_table, out)
    out, heads = _anchor_headings(out)
    out = _fold_meta(out, meta, here_dir, date_hint)
    return title, out, heads


_BLOCKQUOTE_RE = re.compile(r"<blockquote>.*?</blockquote>", re.S)
# 英文原文本身是引用、后面跟着带汉字的引用（剪藏逐段翻译）：也是一对，两边都按引用排
_QUOTE_PAIR_RE = re.compile(r"(<blockquote>\n((?:(?!</?blockquote>).)*)</blockquote>)\n(<blockquote>\n((?:(?!</?blockquote>).)*)</blockquote>)", re.S)
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _quote_pair(m: re.Match) -> str:
    if _HAN_RE.search(m.group(4)) and not _HAN_RE.search(m.group(2)):
        return f'<div class="pair"><div class="orig">{m.group(1)}</div><div class="tr">{m.group(3)}</div></div>'
    return m.group(0)


def _looks_bilingual(html_out: str) -> bool:
    """剪藏译好的英文文章（每段原文后面跟一段「> 译文」）才按中英对照排；
    同手机 Markdown.isBilingual：至少 3 段配成对，并且占了正文段落的三成以上。"""
    pairs = len(_PAIR_RE.findall(html_out)) + len(re.findall(r"</(?:ul|ol)>\n<blockquote>", html_out)) \
        + sum(1 for m in _QUOTE_PAIR_RE.finditer(html_out) if _quote_pair(m) != m.group(0))
    paras = _BLOCKQUOTE_RE.sub("", html_out).count("<p>")
    return pairs >= 3 and pairs >= paras * 0.3


# ---- 表格：整列都是「尚无定论」这种几个字的短词时不折行，免得被长列挤成一字一行。
# 不折行的列总宽要有上限，太多了长列反而没地方；放不下时先留最窄的。
# 三列以上、有数据行的表在手机上一行排成一张卡片，每格前面带上表头

_TABLE_RE = re.compile(r"<table>.*?</table>", re.S)
_ROW_RE = re.compile(r"<tr>.*?</tr>", re.S)
_CELL_RE = re.compile(r"<(t[hd])([^>]*)>(.*?)</\1>", re.S)
_SHORT_CELL = 8     # 一个汉字算 1，英文数字算 0.5
_SHORT_TOTAL = 24   # 不折行的列加起来的上限，约占默认栏宽的六成
_CELL_PADDING = 1.5


def _cell_text(inner: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()


def _text_width(s: str) -> float:
    return sum(1 if ord(c) > 0x2e7f else 0.5 for c in s)


def _short_columns(width: dict[int, float]) -> set[int]:
    short = {i for i, w in width.items() if w <= _SHORT_CELL}
    if len(short) == len(width):
        return set()
    kept, total = set(), 0.0
    for i in sorted(short, key=lambda k: width[k]):
        total += width[i] + _CELL_PADDING
        if total > _SHORT_TOTAL:
            break
        kept.add(i)
    return kept


def _tidy_table(m: re.Match) -> str:
    table = m.group(0)
    rows = [_CELL_RE.findall(r) for r in _ROW_RE.findall(table)]
    head = [_cell_text(inner) for tag, _, inner in rows[0]] if rows and rows[0][0][0] == "th" else []
    width: dict[int, float] = {}
    for cells in rows:
        for i, (_, _, inner) in enumerate(cells):
            width[i] = max(width.get(i, 0), _text_width(_cell_text(inner)))
    short = _short_columns(width)

    def row(rm: re.Match) -> str:
        i = -1

        def cell(c: re.Match) -> str:
            nonlocal i
            i += 1
            attrs = c.group(2) + (' class="nw"' if i in short else "")
            if c.group(1) == "td" and 0 < i < len(head) and head[i]:
                attrs += f' data-label="{html.escape(head[i])}"'
            return f"<{c.group(1)}{attrs}>{c.group(3)}</{c.group(1)}>"
        return _CELL_RE.sub(cell, rm.group(0))
    table = _ROW_RE.sub(row, table)
    return table.replace("<table>", '<table class="stack">', 1) if len(width) >= 3 and len(rows) > 1 else table


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
    date = first("发布时间", "发表时间", "播出时间", "播出", "date", "日期") or date_hint
    when = doctime.parse(date)
    if when is not None:   # 新写法「2026年11月19日，21:12（UTC+8）」折起来只显示哪天
        date = doctime.day(when).isoformat()
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
# 行间距、段间距：跟 /static/common/read-prefs.js 的 LINE_GAPS、PARA_GAPS 同一份档位（也跟手机一样）
_LINE_GAPS = [("tight", "紧凑"), ("standard", "标准"), ("loose", "舒展"), ("looser", "宽松")]
_PARA_GAPS = [("tight", "紧凑"), ("standard", "标准"), ("loose", "宽松")]


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
    f'<div class="row" role="group" aria-label="行间距"><span class="row-label">行距</span>{_choices("line-gap", _LINE_GAPS)}</div>'
    f'<div class="row" role="group" aria-label="段间距"><span class="row-label">段距</span>{_choices("para-gap", _PARA_GAPS)}</div>'
    '<a class="more-prefs" href="/settings#reading">更多字体、导入主题…</a>'
    '</div>'
)


def _page(title: str, crumbs: list[tuple[str, str]], body: str, *, wide: bool = False,
          actions: str = "", toc: list[dict] | None = None, scripts: tuple[str, ...] = ()) -> str:
    sr = request.script_root
    # 手机上面包屑只留「上一级」：最近的一个能点的上级，跳过单集底下的 notes/speech/transcripts
    # 这一层（从单集回到节目，不回到文件夹列表）；首页没有上级，就留当前这一项
    up = next((i for i in range(len(crumbs) - 1, -1, -1)
               if crumbs[i][1] and crumbs[i][0] not in _KIND_DIRS), len(crumbs) - 1)
    nav = " <span class=\"sep\">/</span> ".join(
        (f'<a href="{html.escape(u)}"' if u else "<span") + (' class="up"' if i == up else "")
        + f">{html.escape(t)}" + ("</a>" if u else "</span>")
        for i, (t, u) in enumerate(crumbs))
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
<header class="bar"><a class="home" href="/" aria-label="回到 Spark" title="回到 Spark">←<span> 回到 Spark</span></a><nav class="crumbs">{nav}</nav><div class="actions">{actions}<a class="btn" href="/settings">设置</a>{_SETTINGS}</div></header>
<main class="{classes}">
{body}
</main>
<script src="{sr}/static/reader.js"></script>{"".join(f'<script src="{sr}/static/{s}"></script>' for s in scripts)}{_MATH_ASSETS.format(sr=sr) if 'class="math"' in body else ""}
</body>
</html>"""


# 有公式的页面才加载 KaTeX（放在仓库里，页面只允许本机脚本）
_MATH_ASSETS = ('\n<link rel="stylesheet" href="{sr}/static/katex/katex.min.css" />'
                '\n<script src="{sr}/static/katex/katex.min.js"></script>'
                '\n<script src="{sr}/static/math.js"></script>')


def _crumbs(rel: str) -> list[tuple[str, str]]:
    # 第一级是阅读页自己的首页；回 Spark 首页的是面包屑左边那个「← 回到 Spark」
    out = [("阅读", _url(""))]
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


_COUNT_CACHE: dict[str, tuple[float, tuple[int, float]]] = {}
_COUNT_CACHE_LOCK = threading.Lock()
COUNT_CACHE_SECONDS = 15.0


def _count_md(path: str) -> tuple[int, float]:
    """目录里 .md 的篇数和最新修改时间。首页对每个节目目录、目录页对每个子目录都要算一遍，
    几十个节目上千期时每次打开都是整库 os.walk；结果缓存十几秒，连着翻页不用重复走。"""
    now = time.monotonic()
    with _COUNT_CACHE_LOCK:
        hit = _COUNT_CACHE.get(path)
        if hit and now - hit[0] < COUNT_CACHE_SECONDS:
            return hit[1]
    n, latest = 0, 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.endswith(".md") and not f.startswith("."):
                n += 1
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(dirpath, f)))
                except OSError:
                    pass
    with _COUNT_CACHE_LOCK:
        if len(_COUNT_CACHE) > 2000:
            _COUNT_CACHE.clear()
        _COUNT_CACHE[path] = (now, (n, latest))
    return n, latest


_SHOW_COUNT_CACHE: dict[str, tuple[float, str]] = {}


def _show_count_label(path: str, n_md: int) -> str:
    """首页每个节目后面的数量。一期节目有文字记录、整理稿、笔记好几个文件，数 .md 会把 19 期
    算成 58 篇；有处理记录（.manifest.json）的按里面成功的期数/议题数算，没有的（比如装着
    好几个来源的「信息跟进」）才按文章篇数。处理记录可能有好几 MB，按修改时间缓存。"""
    manifest = os.path.join(path, ".manifest.json")
    try:
        mtime = os.path.getmtime(manifest)
    except OSError:
        return f"{n_md} 篇"
    hit = _SHOW_COUNT_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(manifest, encoding="utf-8") as f:
            m = json.load(f)
        n = sum(1 for r in (m.get("entries") or {}).values() if isinstance(r, dict) and r.get("ok"))
    except (OSError, ValueError, AttributeError):
        n = 0
    label = (f"{n} 期" if m.get("content_type") == "series" else f"{n} 个议题") if n else f"{n_md} 篇"
    _SHOW_COUNT_CACHE[path] = (mtime, label)
    return label


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
    date = date or next((fields[k][:10] for k in ("播出", "published", "date") if fields.get(k)), "")
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
        url = str(entry.get("url") or "")
        # 同一天的按发表时刻排（处理记录里的 publish_at，老记录没有），再按处理顺序
        when = doctime.entry_published(entry) if date else None
        at = when.timestamp() if isinstance(when, datetime) else 0.0
        rank = row.get("rank") if isinstance(row.get("rank"), int) else 0
        eps.append({"title": str(entry.get("title") or ""), "date": date, "files": files,
                    "key": _ep_key(os.path.basename(files[0][1])), "at": at, "rank": rank,
                    "url": url if re.match(r"https?://", url) else ""})
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
                by_key[k] = {"title": "", "date": "", "files": [], "key": k, "at": 0.0, "rank": 0}
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
    dated = sorted((e for e in eps if e["date"]), key=lambda e: (e["date"], e["at"] > 0, e["at"], e["rank"], e["title"], e["key"]),
                   reverse=True)
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
        return _page("Spark 阅读", [("阅读", "")],
                     f'<p class="empty">找不到笔记库里的 Spark 目录：{html.escape(root)}</p>'), 404
    shows = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if name.startswith(".") or name == JOTTINGS_NAME or not os.path.isdir(full):
            continue   # 随手记不是节目：放在最上面的「笔记」那一行
        n, latest = _count_md(full)
        if n:
            shows.append((latest, name, n))
    shows.sort(reverse=True)
    # 剪藏是自己挑的，固定放在最前面
    shows.sort(key=lambda s: s[1] != CLIPS_NAME)
    rows = "".join(
        f'<li class="dir"><a href="{html.escape(_url(name, is_dir=True))}"><span class="name">{html.escape(name)}</span>'
        f'<span class="count">{_show_count_label(os.path.join(root, name), n)} · '
        f'{time.strftime("%Y-%m-%d", time.localtime(latest))} 更新{_event_suffix(os.path.join(root, name))}</span></a></li>'
        for latest, name, n in shows)
    loose = sorted(f for f in os.listdir(root) if f.endswith(".md") and not f.startswith("."))
    loose_html = "".join(f'<li><a href="{html.escape(_url(f))}"><span class="name">{html.escape(f[:-3])}</span></a></li>'
                         for f in loose)
    reports = _reports_root()
    if os.path.isdir(reports):
        n, latest = _count_md(reports)
        if n:
            report_row = (f'<li class="dir"><a href="{html.escape(_url(REPORTS, is_dir=True))}"><span class="name">笔记洞察报告</span>'
                          f'<span class="count">{n} 篇 · {time.strftime("%Y-%m-%d", time.localtime(latest))} 更新</span></a></li>')
            # 有剪藏时剪藏在第一项、报告第二项
            if shows and shows[0][1] == CLIPS_NAME:
                first_end = rows.index("</li>") + len("</li>")
                rows = rows[:first_end] + report_row + rows[first_end:]
            else:
                rows = report_row + rows
    # 笔记（随手记 + 读后感）放在第一行
    n_jot = len(core_jottings.list_all(core_vault.vault_root()))
    n_refl = len(core_jottings.reflection_docs(core_vault.vault_root()))
    rows = (f'<li class="dir"><a href="{html.escape(request.script_root)}/jottings"><span class="name">笔记</span>'
            f'<span class="count">随手记 {n_jot} 篇 · 读后感 {n_refl} 篇</span></a></li>') + rows
    body = (f'<h1>Spark 阅读</h1><p class="lede">笔记库 Spark 目录下的会议、播客和栏目，按最近更新排列；'
            f'笔记、剪藏和笔记洞察生成的报告、演示排在最前面。</p>'
            f'<ul class="list">{rows}</ul>')
    if loose_html:
        body += f'<h2>其他文件</h2><ul class="list">{loose_html}</ul>'
    return _page("Spark 阅读", [("阅读", "")], body)


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


CLIPS_NAME = os.path.basename(core_clips.CLIPS_DIR)   # Spark/ 下的「剪藏」
JOTTINGS_NAME = core_jottings.DIRNAME                  # Spark/ 下的「笔记」：随手记


def _clips_page() -> str:
    """剪藏专页：上面一排标签（带篇数），默认按标签分组（同 core/clips.py 的索引：两篇以上的标签成组，
    剩下的放「其他」）；点了标签只看那一组。每篇显示存的日期、来源和一句话。"""
    items = sorted(core_clips.load_clips(core_vault.vault_root()), key=core_clips.order_clipped, reverse=True)
    picked = (request.args.get("tag") or "").strip()
    count: dict[str, int] = {}
    for c in items:
        for t in set(c["tags"]):
            count[t] = count.get(t, 0) + 1
    tags = sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))

    def chip(tag: str, label: str) -> str:
        href = "?" + urllib.parse.urlencode({"tag": tag}) if tag else "?"
        cur = ' aria-current="page"' if tag == picked else ""
        return f'<a href="{html.escape(href)}"{cur}>{html.escape(label)}</a>'

    def rows(cs: list[dict]) -> str:
        out = []
        for c in cs:
            meta = f'<span class="kind">{html.escape(c["source"])}</span>' if c["source"] else ""
            tldr = f'<p class="clip-tldr">{html.escape(c["tldr"])}</p>' if c["tldr"] else ""
            out.append(f'<li class="clip"><a href="{html.escape(_url(CLIPS_NAME + "/" + c["name"]))}">'
                       f'<span class="date">{html.escape(c["clipped"])}</span><span class="name">{html.escape(c["title"])}</span>'
                       f'{meta}</a>{tldr}</li>')
        return f'<ul class="list clips">{"".join(out)}</ul>'

    parts = [f'<h1>剪藏</h1><p class="lede">在手机上分享进来存下的文章，共 {len(items)} 篇。'
             f'按标签分组，一篇可能出现在几个标签下；组里新存的在前。</p>']
    heads: list[dict] = []
    if not items:
        parts.append('<p class="empty">还没有剪藏。在手机的微信或 Safari 里分享 → Spark → 「存到剪藏」。</p>')
    else:
        if tags:
            parts.append('<nav class="tagbar" aria-label="按标签看">' + chip("", f"全部 {len(items)}")
                         + "".join(chip(t, f"{t} {n}") for t, n in tags) + "</nav>")
        if picked:
            members = [c for c in items if picked in c["tags"]]
            parts.append(f'<h2>{html.escape(picked)}<span class="count">{len(members)} 篇</span></h2>{rows(members)}')
        else:
            groups = [t for t, n in tags if n >= 2]
            rest = [c for c in items if not any(t in groups for t in c["tags"])]
            sections = [(g, [c for c in items if g in c["tags"]]) for g in groups]
            if rest:
                sections.append(("其他" if groups else "全部", rest))
            for i, (g, members) in enumerate(sections):
                hid = f"tag-{i}"
                heads.append({"level": 2, "id": hid, "text": g, "sub": ""})
                parts.append(f'<h2 id="{hid}">{html.escape(g)}<span class="count">{len(members)} 篇</span></h2>{rows(members)}')
    index = os.path.join(_root(), CLIPS_NAME, core_clips.INDEX_NAME)
    actions = _actions(f"{CLIPS_NAME}/{core_clips.INDEX_NAME}") if os.path.isfile(index) else ""
    return _page("剪藏", _crumbs(CLIPS_NAME), "".join(parts), actions=actions, toc=_toc_entries(heads))


# ---------------------------------------------------------------- 会议时间（core/summit_dates.py）

_EVENT_CACHE: dict[str, tuple[float, dict | None]] = {}


def _event_info(path: str) -> dict | None:
    """会议文件夹的日期：{start, end, source, label, text}；text 是「会议时间：…」（手动填的、议程的）或
    「视频发布：…」（从视频发布日期现推的，不是会议时间）。不是会议、不知道返回 None。按处理记录的修改时间缓存。"""
    if _show_type(path) != "summit":
        return None
    manifest = summit_dates.manifest_path(path)
    try:
        mtime = os.path.getmtime(manifest)
    except OSError:
        return None
    hit = _EVENT_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    r = summit_dates.resolve(data)
    info = _event_json(r)
    _EVENT_CACHE[path] = (mtime, info)
    return info


def _event_json(r: dict | None) -> dict | None:
    return dict(r, label=summit_dates.label(r["start"], r["end"]), text=summit_dates.display(r)) if r else None


def _event_suffix(path: str) -> str:
    info = _event_info(path)
    return f" · {html.escape(info['text'])}" if info else ""


def _event_folder(rel: str) -> str | None:
    """能改会议时间的文件夹：Spark/ 下面第一层、有处理记录、是会议（不是播客、信息跟进、剪藏）。"""
    rel = rel.strip("/")
    if not rel or "/" in rel or rel in (CLIPS_NAME, _TRACK_NAME):
        return None
    path = _resolve(rel)
    if not path or not os.path.isdir(path) or os.path.dirname(path) != os.path.realpath(_root()):
        return None
    if not os.path.isfile(summit_dates.manifest_path(path)) or _show_type(path) != "summit":
        return None
    return path


def _event_block(path: str, rel: str) -> str:
    """会议文件夹页标题下面：会议时间，和「改会议时间」（两个日期，结束可以不填；手动填过的能恢复自动）。"""
    if _event_folder(rel) is None:
        return ""
    info = _event_info(path)
    manual = bool(info and info["source"] == "manual")
    # 真的会议时间（手动填的、议程的）才算填了；只有视频发布日期的，照样显示「视频发布：…」，再请人填会议时间
    known = bool(info and info["source"] in summit_dates.STORED_SOURCES)
    shown = html.escape(info["text"]) if info else "会议时间：还没填"
    if info and not known:
        shown += "</p><p class=\"ed-now\">会议时间：还没填"
    start = html.escape(info["start"]) if known else ""
    end = html.escape(info["end"]) if known and info["end"] != info["start"] else ""
    reset = ('<button type="button" class="btn ed-reset">恢复自动</button>' if manual else "")
    return (f'<div class="event-dates" data-api="{html.escape(request.script_root)}/api" data-folder="{html.escape(rel)}">'
            f'<p class="ed-now">{shown}</p>'
            f'<details class="ed-edit"><summary>{"改会议时间" if known else "填会议时间"}</summary>'
            f'<form class="ed-form"><label>开始 <input type="date" name="start" required value="{start}"></label> '
            f'<label>结束 <input type="date" name="end" value="{end}"></label> '
            f'<button type="submit" class="btn">保存</button>{reset}'
            f'<span class="ed-msg" role="status" aria-live="polite"></span>'
            f'<p class="ed-hint">只开一天的结束可以不填。</p></form></details></div>'
            f'<script src="{html.escape(request.script_root)}/static/event-dates.js" defer></script>')


def _dir_page(path: str, rel: str):
    name = os.path.basename(path)
    if rel == CLIPS_NAME:
        return _clips_page()
    if rel == JOTTINGS_NAME:   # 随手记的文件夹：换成笔记页
        return redirect(f"{request.script_root}/jottings")
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
        parts.append(f"<h1>{html.escape(title)}</h1>{_event_block(path, rel)}"
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
    # 整理稿都是对照稿；剪藏只有译过的英文文章是，按内容判断
    bilingual = True if "/speech/" in f"/{rel}" else (None if rel.startswith(CLIPS_NAME + "/") else False)
    reflection = None
    can_draft = False
    if view != "home" and core_notes.annotatable(_vault_rel(path)):
        reflection = _reflection(text)
        # 有划线或批注、也配好了模型，读后感卡上才给「让模型起个草稿」（同手机）
        can_draft = bool(re.search(r"==[^=\n]+==|\[\^note-\d+\]:", text)) and _draft_model()[1] is not None
        r = _reflection_range(text)
        if r:
            text = text[:r[0]] + text[r[1]:]
    title, out, heads = _render(text, os.path.dirname(path), bilingual=bilingual,
                                date_hint=_entry_label(os.path.basename(path))[0])
    if reflection is not None:
        out += _reflection_card(reflection, os.path.dirname(path), can_draft)
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
    # 手机上顶栏窄，class="lg" 的字藏起来，只剩「Obsidian」「演示」；读屏仍读完整的名字
    out = (f'<a class="btn" href="{html.escape(obs)}" aria-label="在 Obsidian 打开">'
           '<span class="lg">在 </span>Obsidian<span class="lg"> 打开</span></a>')
    deck = path[:-3] + ".deck.html" if path.endswith(".md") else ""
    if deck and os.path.isfile(deck):
        out = (f'<a class="btn" href="{html.escape(_url(_rel(deck)))}" aria-label="看演示">'
               '<span class="lg">看</span>演示</a>') + out
    return out


def _file_page(path: str, rel: str):
    title, body, heads = _doc(path)
    wide = 'class="pair"' in body
    actions = _actions(rel)
    if core_jottings.is_path(_vault_rel(path)):   # 随手记：顶栏多一个「编辑」，改标题和正文
        edit = f"{request.script_root}/jottings/edit?" + urllib.parse.urlencode({"path": rel})
        actions = f'<a class="btn" href="{html.escape(edit)}">编辑</a>' + actions
    return _page(title or os.path.basename(path)[:-3], _crumbs(rel),
                 _article(path, body), wide=wide, actions=actions, toc=_toc_entries(heads))


def _log_read(path: str) -> None:
    """记一笔阅读记录，复盘用（core/readlog.py）。"""
    rel = _vault_rel(path)
    parts = rel.split("/")
    kind = next((k for d, k in (("notes", "note"), ("speech", "speech"), ("transcripts", "transcript")) if d in parts),
                "article" if "信息跟进" in parts else "doc")
    core_readlog.record_desktop(rel, kind)


def _mtime(path: str) -> str:
    return str(os.stat(path).st_mtime_ns)


def _article(path: str, body: str, *, view: str = "") -> str:
    """正文外面这一层带上高亮 / 摘录要用的信息：哪个文件、打开时的修改时间、按哪种排法（高亮后重排正文要一致）。"""
    rel = _rel(path)
    excerptable = "false" if rel == EXCERPTS_NAME else "true"
    view_attr = f' data-view="{view}"' if view else ""
    # 能不能批注、撤销、写读后感由服务端定（core/annotations.annotatable），脚本不按路径猜
    notes = "true" if view != "home" and core_notes.annotatable(_vault_rel(path)) else "false"
    return (f'<article class="doc" data-path="{html.escape(rel)}" data-mtime="{_mtime(path)}" '
            f'data-api="{request.script_root}/api" data-excerptable="{excerptable}" data-notes="{notes}"{view_attr}>{body}</article>')


def _render_body(path: str, view: str = "") -> str:
    if view == "home" and os.path.basename(path) != os.path.basename(os.path.dirname(path)) + ".md":
        view = ""
    return _doc(path, view=view)[1]


# ---- Mac App 的书架：节目、单集和搜索的 JSON，认法和上面的网页一样，页面还是这个阅读页

_SHOW_TYPE_CACHE: dict[str, tuple[float, str]] = {}
_TRACK_NAME = "信息跟进"
SEARCH_LIMIT = 60


def _show_type(path: str) -> str:
    manifest = os.path.join(path, ".manifest.json")
    try:
        mtime = os.path.getmtime(manifest)
    except OSError:
        return "other"
    hit = _SHOW_TYPE_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(manifest, encoding="utf-8") as f:
            kind = "series" if json.load(f).get("content_type") == "series" else "summit"
    except (OSError, ValueError, AttributeError):
        kind = "other"
    _SHOW_TYPE_CACHE[path] = (mtime, kind)
    return kind


def _vault_rel(path: str) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(core_vault.vault_root())).replace(os.sep, "/")


def _read_paths() -> set[str]:
    return {ev["path"] for ev in core_readlog.load() if ev.get("path")}


def _show_row(path: str, rel: str, kind: str) -> dict:
    n, latest = _count_md(path)
    row = {"rel": rel, "name": os.path.basename(path), "kind": kind, "count": _show_count_label(path, n),
           "latest": latest, "n": n}
    if kind == "summit":
        # 会议日期（core/summit_dates.py）：{start, end, source, label, text}，不知道是 null；
        # source=sessions 是视频发布日期，text 写成「视频发布：…」
        row["eventDates"] = _event_info(path)
    return row


def _file_json(label: str, path: str) -> dict:
    return {"label": label, "url": _url(_rel(path))}


def _item_json(title: str, date: str, files: list[tuple[str, str]], read: set[str], show: str = "") -> dict:
    # 星标记在第一个文件的文件头里：一期节目是小结（没有小结的简讯是整理稿），同手机
    return {"id": _rel(files[0][1]), "title": title, "date": date, "show": show,
            "files": [_file_json(label, p) for label, p in files],
            "read": any(_vault_rel(p) in read for _, p in files),
            "starred": _starred_file(files[0][1])}


def _starred_file(path: str) -> bool:
    """只读文件开头一小段看文件头里有没有 starred: true（列表一次几千篇，不整篇读）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    return core_notes.is_starred(head)


def _clip_urls() -> set[str]:
    return {c["url"] for c in core_clips.load_clips(core_vault.vault_root()) if c.get("url")}


def _show_items(path: str, read: set[str], *, show: str = "", clip_urls: set[str] | None = None) -> list[dict]:
    # 信息跟进的文章带原文链接：Mac App 上能「存进剪藏」（同手机）；已经存过的标 saved
    track = _in(os.path.realpath(path), os.path.realpath(os.path.join(_root(), _TRACK_NAME)))
    if track and clip_urls is None:
        clip_urls = _clip_urls()
    items = []
    for e in _episodes(path):
        item = _item_json(e["title"], e["date"], e["files"], read, show)
        if track and e.get("url"):
            item.update(origin=e["url"], saved=e["url"] in clip_urls)
        items.append(item)
    if items:
        return items
    for name in sorted(os.listdir(path), reverse=True):
        full = os.path.join(path, name)
        if name.endswith(".md") and not name.startswith(".") and os.path.isfile(full):
            title, date = _file_info(full)
            items.append(_item_json(title, date, [("正文", full)], read, show))
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def _report_items(read: set[str]) -> list[dict]:
    items = []
    for folder, dirs, names in os.walk(_reports_root()):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.endswith(".deck.assets")]
        for name in names:
            if name.endswith(".md") and not name.startswith("."):
                full = os.path.join(folder, name)
                title, date = _file_info(full)
                date = date or time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(full)))
                items.append(_item_json(title, date, [("正文", full)], read, "报告"))
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def _clip_items(read: set[str], by: str = "clipped") -> list[dict]:
    """by：clipped 按收录时间、published 按发表时间（core/clips.order_*，跟手机、Mac App 的排法一样）。"""
    order = core_clips.order_published if by == "published" else core_clips.order_clipped
    clips = sorted(core_clips.load_clips(core_vault.vault_root()), key=order, reverse=True)
    folder = os.path.join(_root(), CLIPS_NAME)
    # Mac App 的剪藏列表跟手机一样能按标签筛、按发表日期排，标题下面显示一句话
    out = []
    for c in clips:
        full = os.path.join(folder, c["name"])
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        needs = bool(text) and clip_capture.needs_fill(text)
        # needs：缺摘要或者英文没译完，Mac App 上能一键补
        out.append(dict(_item_json(c["title"], c["clipped"], [("正文", full)], read, c["source"] or "剪藏"),
                        tags=c["tags"], published=c["published"], snippet=c["tldr"], needs=needs,
                        # 发表、收录的时刻（ISO 带时差，老文件没有）：Mac App 按时间排同一天的先后
                        publishedAt=c["published_at"], clippedAt=c["clipped_at"],
                        starred=_front_flag(text, "starred"), pinned=_front_flag(text, "pinned")))
    return out


def _jotting_items(read: set[str]) -> list[dict]:
    """随手记（Spark/笔记/）：按新建那天排进「最新」，加了星标的进「星标」（同手机）。"""
    folder = core_jottings.folder()
    out = []
    for j in core_jottings.list_all(core_vault.vault_root()):
        item = _item_json(j["title"], doctime.written_day(j["created"]).isoformat(),
                          [("正文", os.path.join(folder, j["name"]))], read, "随手记")
        item["snippet"] = j["excerpt"]
        out.append(item)
    return out


def _shows() -> list[dict]:
    root = _root()
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        full = os.path.join(root, name)
        if name.startswith(".") or name in (CLIPS_NAME, JOTTINGS_NAME) or not os.path.isdir(full):
            continue
        if name == _TRACK_NAME:
            out += [_show_row(os.path.join(full, sub), f"{name}/{sub}", "track") for sub in sorted(os.listdir(full))
                    if not sub.startswith(".") and os.path.isdir(os.path.join(full, sub))]
        else:
            out.append(_show_row(full, name, _show_type(full)))
    return sorted((s for s in out if s["n"]), key=lambda s: s["latest"], reverse=True)


@app.route("/api/library")
def api_library():
    return jsonify({"shows": _shows()})


@app.route("/api/items")
def api_items():
    """一个书架位置下的文章：latest、clips、reports，或 show=节目文件夹。"""
    place = request.args.get("place") or "latest"
    read = _read_paths()
    if place == "clips":
        return jsonify({"items": _clip_items(read, request.args.get("sort") or "clipped")})
    if place == "reports":
        return jsonify({"items": _report_items(read)})
    if place == "starred":
        # 书架的「星标」：随手记、剪藏、节目单集、会议、信息、报告里加了星标的，一起按日期排
        items = _jotting_items(read) + _clip_items(read) + _report_items(read)
        urls = _clip_urls()
        for s in _shows():
            items += _show_items(os.path.join(_root(), s["rel"]), read, show=s["name"], clip_urls=urls)
        items = [i for i in items if i.get("starred")]
        return jsonify({"items": sorted(items, key=lambda i: i["date"], reverse=True)})
    if place == "latest":
        items = _jotting_items(read) + _clip_items(read) + _report_items(read)
        urls = _clip_urls()
        for s in _shows():
            items += _show_items(os.path.join(_root(), s["rel"]), read, show=s["name"], clip_urls=urls)
        items = [i for i in items if i["date"]]
        return jsonify({"items": sorted(items, key=lambda i: i["date"], reverse=True)[:200]})
    path = _resolve(place)
    if not path or not os.path.isdir(path):
        abort(404)
    items = _show_items(path, read)
    home = os.path.join(path, os.path.basename(path) + ".md")
    return jsonify({"items": items, "home": _url(_rel(path), is_dir=True) if os.path.isfile(home) else ""})


def _search_files():
    for top in (_root(), _reports_root()):
        for folder, dirs, names in os.walk(top):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.endswith(".md") and not name.startswith("."):
                    yield os.path.join(folder, name)


@app.route("/api/search")
def api_search():
    """书名命中排前面，正文命中带一小段上下文。"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    needle = q.lower()
    read = _read_paths()
    titled, bodied = [], []
    for path in _search_files():
        name_hit = needle in os.path.basename(path).lower()
        if not name_hit and len(bodied) >= SEARCH_LIMIT:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        at = text.lower().find(needle)
        if not name_hit and at < 0:
            continue
        title, date = _file_info(path)
        snippet = re.sub(r"\s+", " ", text[max(0, at - 40):at + 100]).strip() if at >= 0 else ""
        kind = _KIND_DIRS.get(os.path.basename(os.path.dirname(path)), "正文")
        item = _item_json(title, date, [(kind, path)], read, kind)
        item["snippet"] = snippet
        (titled if name_hit else bodied).append(item)
    return jsonify({"items": (titled + bodied)[:SEARCH_LIMIT]})


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
# 批注号「[^note-3]」在页面上是个不带字的小气泡，也算格式符号（两种写法开头不同，不会互相覆盖、回溯）
_MARKUP = r"(?:[*_`~=]|\[\^note-\d+\])*"
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
    # 代码块 / 行内代码里 ==…== 不会渲染成高亮，只会把代码改坏
    code = _code_ranges(body)
    outside = [m for m in matches if not any(a < m.end() and m.start() < b for a, b in code)]
    if not outside:
        raise AnnotateError("代码里的文字不能高亮（Markdown 在代码里不认 ==…==）")
    matches = outside
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


_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,}).*?\n(?:.*?\n)*?[ \t]{0,3}\1[ \t]*$", re.M)
_INLINE_CODE_RE = re.compile(r"(`+)[^`\n]+?\1")


def _code_ranges(body: str) -> list[tuple[int, int]]:
    """正文源码里围栏代码块和行内代码的区间。"""
    ranges = [(m.start(), m.end()) for m in _FENCE_RE.finditer(body)]
    for m in _INLINE_CODE_RE.finditer(body):
        if not any(a <= m.start() < b for a, b in ranges):
            ranges.append((m.start(), m.end()))
    return ranges


def _unbalanced(span: str, token: str) -> bool:
    if token in ("`", "*"):
        span = span.replace("**", "").replace("__", "").replace("~~", "")
    return span.count(token) % 2 == 1


def _read_utf8(path: str) -> str:
    """要改写的文件必须是合法 UTF-8：按 errors="replace" 读进来再写回去会把原文里
    坏掉的字节换成 �，所以这里不这么干，直接告诉用户没法在网页里改。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise AnnotateError("这个文件里有不是 UTF-8 的字节，没法在网页里高亮或摘录；在 Obsidian 里另存为 UTF-8 后再试") from None


def _read_checked(path: str, mtime: str) -> tuple[str, str, int]:
    """读文件，核对页面打开时的修改时间。返回 (全文, 正文, 正文在全文里的起点)。"""
    if mtime and _mtime(path) != str(mtime):
        raise AnnotateError("这篇在别处改过了（比如 Obsidian 里），刷新页面后再试")
    text = _read_utf8(path)
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
    text = _read_utf8(note)
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
    full = _read_utf8(note)
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
    if not label and not _in(note, _reports_root()):
        return   # 小结自己里的划线：重新生成时由 core.annotations.carry_over 原处接回去，不用再记一份
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
    text = _read_utf8(report)
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
    page = _read_utf8(deck)
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
    title = _doc_title(path)
    if rel.startswith(REPORTS + "/"):
        # 笔记洞察的报告：目录在 Spark/ 之外（默认是库根下的 output/），链接要按库根算
        real = os.path.realpath(path)
        vault_root = os.path.realpath(core_vault.vault_root())
        if _in(real, vault_root):
            target = os.path.relpath(real, vault_root).replace(os.sep, "/")[:-3]
        else:
            target = os.path.basename(real)[:-3]   # 库外的报告只能按文件名让 Obsidian 自己找
        label = f"报告 · {title}"
    else:
        show = rel.split("/")[0] if "/" in rel else ""
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
        existing = _read_utf8(out)
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


def _retitled(text: str, title: str) -> str:
    """改标题：文件头的 title 和第一行「# 标题」，文件名不动，别处的链接不会断（同手机 DocEdit.retitled）。"""
    lines = text.split("\n")
    in_head = bool(lines) and lines[0] == "---"
    for i, line in enumerate(lines):
        if in_head:
            if i > 0 and line == "---":
                in_head = False
            elif i > 0 and line.startswith("title:"):
                lines[i] = "title: " + json.dumps(title, ensure_ascii=False)
            continue
        if line.startswith("# "):
            lines[i] = f"# {title}"
            break
    return "\n".join(lines)


def _retitle_ok(path: str) -> bool:
    """能改名、能删的：剪藏和复盘（同手机 DocEdit.editable）。"""
    rel = _vault_rel(path)
    return core_clips.is_clip_path(rel) or (rel.startswith(f"{core_vault.REPORTS_DIRNAME}/复盘/") and rel.endswith(".md"))


# ---- 读后感、批注、星标置顶、撤销：都改剪藏自己的文件，写法跟手机 DocEdit 一样，两边看到的是同一份

# 读后感、批注的写法在 core/annotations.py（手机 DocEdit 同一套）
_reflection_range = core_notes.reflection_range
_reflection = core_notes.reflection
_with_reflection = core_notes.with_reflection


def _reflection_card(text: str, here_dir: str, can_draft: bool = False) -> str:
    """文末的读后感卡：写了的显示出来、能改，没写的给一个写的地方（同手机）。"""
    shown = _md(_WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)) if text else ""
    body = (f'<div class="refl-body">{shown}</div>' if text
            else '<p class="refl-empty">读完有什么想法，写在这里。存在这篇文件里，Obsidian 里也看得到。</p>')
    label = "改读后感" if text else "写读后感"
    draft = ' data-draft="1"' if can_draft else ""
    return (f'<section class="reflection" aria-labelledby="refl-h" data-raw="{html.escape(text)}"{draft}>'
            f'<h2 id="refl-h">读后感</h2>{body}'
            f'<div class="refl-row"><button type="button" class="refl-edit">{label}</button>'
            f'<a class="refl-notes" href="{request.script_root}/notes">读书笔记</a></div></section>')


def _one_line(s: str) -> str:
    """脚注只能写一行：换行换成空格。"""
    return re.sub(r"\s*\n\s*", " ", s.strip())


def _annotated(path: str, mtime: str, data: dict, note: str) -> str:
    """给选中的字（或点中的那处划线）加批注：划上、后面跟脚注号，文末写批注。返回新的全文。"""
    full, body, off = _read_checked(path, mtime)
    note = _one_line(note)
    if not note or len(note) > 2000:
        raise AnnotateError("批注不能是空的，也不能太长")
    text = str(data.get("text") or "")
    if not text.strip() or len(text) > MAX_SELECTION_CHARS:
        raise AnnotateError("选中的文字是空的，或者太长了")
    defs_at = _NOTE_DEF_RE.search(body)
    limit = defs_at.start() if defs_at else len(body)
    r = _reflection_range(body)
    if r:
        limit = min(limit, r[0])
    if "nth" in data:   # 点中的已有划线
        want = _norm(text)
        hits = [m for m in _MARK_RE.finditer(body, 0, limit) if _norm(m.group(1)) == want]
        if not hits:
            raise AnnotateError("在原文里找不到这处划线，刷新页面后再试")
        try:
            m = hits[min(max(int(data.get("nth") or 0), 0), len(hits) - 1)]
        except (TypeError, ValueError):
            raise AnnotateError("nth 要是数字") from None
        a, b = m.start(), m.end()
    else:
        if "\n" in text.strip():
            raise AnnotateError("选中的文字跨了段落，批注只能在一段里面")
        code = _code_ranges(body)
        matches = [m for m in _selection_regex(text).finditer(body, 0, limit)
                   if not any(x < m.end() and m.start() < y for x, y in code)]
        if not matches:
            raise AnnotateError("这段跨了链接或特殊格式，在原文里对不上，没法批注")
        ctx = _norm(str(data.get("before") or ""))[-60:]
        best = max(matches, key=lambda m: _common_suffix(_norm(body[max(0, m.start() - 400):m.start()]), ctx))
        a, b = best.start(), best.end()
        for t in ("**", "__", "~~", "`"):
            if _unbalanced(body[a:b], t):
                if body[max(0, a - len(t)):a] == t:
                    a -= len(t)
                elif body[b:b + len(t)] == t:
                    b += len(t)
        # 选中范围碰到的已有划线，整段并进来
        for m in _MARK_RE.finditer(body):
            if m.start() < b and a < m.end() or (a == m.start() + 2 and b == m.end() - 2):
                a, b = min(a, m.start()), max(b, m.end())
        if any(_unbalanced(body[a:b], t) for t in ("**", "__", "~~", "`", "*")):
            raise AnnotateError("选区只包住了半个格式（比如加粗的一半），换个起止点再试")
    span = body[a:b]
    if "[^note-" in span:
        raise AnnotateError("这段里已经有别的批注了，换一段，或者先删掉那条批注")
    # 已经跟着的批注号留在新批注号前面
    keep = re.match(r"(?:\[\^note-\d+\])*", body[b:]).group(0)
    nid = max(_note_defs(full), default=0) + 1
    inner = span.replace("==", "")
    new_body = body[:a] + f"=={inner}==" + keep + f"[^note-{nid}]" + body[b + len(keep):]
    out = (full[:off] + new_body).rstrip("\n") + "\n"
    if not _note_defs(out):
        out += "\n"
    return out + f"[^note-{nid}]: {note}\n"


def _renoted(text: str, nid: int, note: str) -> str:
    note = _one_line(note)
    if not note or len(note) > 2000:
        raise AnnotateError("批注不能是空的，也不能太长")
    out, n = re.subn(rf"^\[\^note-{nid}\]:.*$", lambda _m: f"[^note-{nid}]: {note}", text, count=1, flags=re.M)
    if not n:
        raise AnnotateError("找不到这条批注，刷新页面后再试")
    return out


def _unnoted(text: str, nid: int) -> str:
    """删一条批注：脚注号和文末那行去掉，划线留着。"""
    out = re.sub(rf"^\[\^note-{nid}\]:.*\n?", "", text, flags=re.M).replace(f"[^note-{nid}]", "")
    return out.strip("\n") + "\n"


def _set_front(text: str, key: str, value: str | None) -> str:
    """改文件头里的一项，value 为 None 就去掉（同手机 DocEdit.setting）。"""
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise AnnotateError("这篇没有文件头，加不了")
    end = lines.index("---", 1)
    head = lines[1:end]
    at = next((i for i, ln in enumerate(head) if ln.startswith(f"{key}:")), len(head))
    head = [ln for ln in head if not ln.startswith(f"{key}:")]
    if value is not None:
        head.insert(min(at, len(head)), f"{key}: {value}")
    return "\n".join(["---"] + head + lines[end:])


def _front_flag(text: str, key: str) -> bool:
    m = _FRONTMATTER_RE.match(text)
    return bool(m and re.search(rf"^{key}:[ \t]*true[ \t]*$", m.group(1), re.M))


# 最近一次改动前的全文，「撤销」用（同手机划线后底下留几秒的撤销）。只记剪藏，只认改完后没再动过的
_LAST_EDIT: dict[str, tuple[str, str]] = {}
_LAST_EDIT_LOCK = threading.Lock()


def _write_edit(path: str, before: str, after: str) -> None:
    atomic.write_text(path, after)
    _remember_edit(path, before)


def _remember_edit(path: str, before: str) -> None:
    if core_notes.annotatable(_vault_rel(path)):
        with _LAST_EDIT_LOCK:
            _LAST_EDIT[path] = (before, _mtime(path))


def _clip_target(data: dict) -> str:
    path = _annotate_target(data)
    if not core_clips.is_clip_path(_vault_rel(path)):
        raise AnnotateError("只有剪藏能这样改")
    return path


def _note_target(data: dict) -> str:
    """能批注、写读后感、撤销的：规则在 core/annotations.annotatable，剪藏、整理稿、会议资料、报告都算。"""
    path = _annotate_target(data)
    if not core_notes.annotatable(_vault_rel(path)):
        raise AnnotateError("这个文件不能做笔记")
    return path


def _edited(path: str, data: dict):
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path, str(data.get("view") or ""))})


@app.route("/api/note", methods=["POST"])
def api_note():
    """批注：新加（选中的字或点中的划线）、改（id + note）、删（id + remove）。"""
    data = request.get_json(silent=True) or {}
    note = str(data.get("note") or "")
    try:
        path = _note_target(data)
        mtime = str(data.get("mtime") or "")
        if data.get("id") is not None:
            try:
                nid = int(data["id"])
            except (TypeError, ValueError):
                raise AnnotateError("id 要是数字") from None
            full, _, _ = _read_checked(path, mtime)
            new = _unnoted(full, nid) if data.get("remove") else _renoted(full, nid, note)
        else:
            full, _, _ = _read_checked(path, mtime)
            new = _annotated(path, mtime, data, note)
        _write_edit(path, full, new)
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return _edited(path, data)


@app.route("/api/reflection", methods=["POST"])
def api_reflection():
    data = request.get_json(silent=True) or {}
    body = str(data.get("text") or "")
    try:
        path = _note_target(data)
        if len(body) > 20000:
            raise AnnotateError("读后感太长了")
        full, _, _ = _read_checked(path, str(data.get("mtime") or ""))
        _write_edit(path, full, _with_reflection(full, body.replace("\r\n", "\n")))
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return _edited(path, data)


# spark.py 填上：数一下各模块在跑的任务。本机 Ollama 一次只跑一件事，有任务在跑时不起草
busy_jobs = None
_DRAFT_LOCK = threading.Lock()


def _draft_source(text: str) -> str:
    """给模型看的材料：标题、一句话摘要、按顺序的划线和批注（同手机 draftSource）。"""
    fm = core_clips.frontmatter(text)
    n = _reading_note(text)
    lines = [f"文章：{fm.get('title') or ''}"]
    if fm.get("tldr"):
        lines.append(f"摘要：{fm['tldr']}")
    lines.append("")
    for i, it in enumerate(n["items"], 1):
        lines.append(f"划线{i}：{_plain_inline(it['mark'])}")
        lines += [f"  我的批注：{t}" for t in it["notes"]]
    lines += [f"我的批注：{t}" for t in n["loose"]]
    return "\n".join(lines) if n["items"] or n["loose"] else ""


def _draft_prompt(source: str) -> str:
    return (
        "下面是我读一篇文章时划的重点和写的批注。请替我起草读后感：只写我关注的点，每一点直接说内容本身。\n"
        "- 1 到 3 点，每点一两句，合起来不超过 150 字；一段话，不要标题、不要分点符号。\n"
        "- 关注点从划线和批注里来；批注里我自己的判断要保留原意。\n"
        "- 不要写「我学到了」「让我意识到」「我觉得」「我的感想是」「这篇文章」「作者认为」这类引导语和感想，"
        "也不要总结、评价或夸文章。\n"
        "- 只根据下面的内容，不要编造文章里没有的观点和数字。\n\n" + source)


def _draft_model():
    """(backend, 调模型的函数)；没配好模型时 (None, None)。"""
    from core import llm as core_llm, llm_config
    try:
        cfg = llm_config.resolve({}, default_backend="api")
    except llm_config.ConfigError:
        return None, None

    def ask(prompt):
        return core_llm.complete(prompt, cfg["backend"], api_key=cfg["api_key"], model=cfg["model"],
                                 api_base=cfg["api_base"], max_tokens=600, timeout=120)
    return cfg["backend"], ask


@app.route("/api/reflection/draft", methods=["POST"])
def api_reflection_draft():
    """让模型按划线和批注起草读后感：只返回草稿，不写文件，存不存由人决定（同手机）。"""
    from core import llm as core_llm
    data = request.get_json(silent=True) or {}
    try:
        path = _note_target(data)
        source = _draft_source(_read_utf8(path))
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"读文件失败：{e}"}), 500
    if not source:
        return jsonify({"error": "这篇还没有划线和批注，没东西可起草"}), 400
    backend, ask = _draft_model()
    if not ask:
        return jsonify({"error": "没配好模型，在设置里填好模型再起草"}), 400
    if backend == "ollama" and busy_jobs and busy_jobs():
        return jsonify({"error": "本机模型正在跑别的任务，等它跑完再起草"}), 409
    if not _DRAFT_LOCK.acquire(blocking=False):
        return jsonify({"error": "正在起草另一篇，等一下再试"}), 409
    try:
        out = ask(_draft_prompt(source))
    except core_llm.LLMError as e:
        return jsonify({"error": f"没起草成：{e}"}), 502
    finally:
        _DRAFT_LOCK.release()
    return jsonify({"text": (out or "").strip()})


@app.route("/api/flag", methods=["POST"])
def api_flag():
    """星标、置顶：文件头里的 starred: true / pinned: true（同手机）。
    星标能做笔记的文件都能加（没有文件头的补一段）；置顶只有剪藏有。"""
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "")
    try:
        if key not in ("starred", "pinned"):
            raise AnnotateError("只能加星标或置顶")
        path = _note_target(data) if key == "starred" else _clip_target(data)
        full = _read_utf8(path)
        if key == "starred":
            atomic.write_text(path, core_notes.with_star(full, bool(data.get("on"))))
        else:
            atomic.write_text(path, _set_front(full, key, "true" if data.get("on") else None))
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), key: bool(data.get("on"))})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    data = request.get_json(silent=True) or {}
    try:
        path = _note_target(data)
        with _LAST_EDIT_LOCK:
            before, mtime = _LAST_EDIT.get(path, ("", ""))
            if not mtime or _mtime(path) != mtime:
                raise AnnotateError("没有能撤销的改动（或者这篇后来又改过了）")
            atomic.write_text(path, before)
            del _LAST_EDIT[path]
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return _edited(path, data)


# ---- 读书笔记：每篇的划线、批注、读后感按「一篇一篇笔记」摆出来（同手机「复盘 → 笔记」）。
# 全库能做笔记的文件都算（core/annotations.annotated_docs），剪藏、整理稿、会议资料、报告一起排

_reading_note = core_notes.reading_note
_plain_inline = core_notes.plain_inline

_DRAFT_KINDS = {"speech": "整理稿", "transcripts": "文字记录", "notes": "小结"}


def _note_origin(rel: str, text: str) -> tuple[str, str, str]:
    """(标题, 出处, 原文网址)。剪藏用文件头；节目、会议的稿子写成「节目名・整理稿」。"""
    fm = dict(_split_frontmatter(text)[0])
    h1 = re.search(r"^# +(.+)$", text, re.M)
    title = str(fm.get("title") or (h1.group(1).strip() if h1 else "") or os.path.basename(rel)[:-3])
    url = str(fm.get("url") or fm.get("source_url") or "")
    parts = rel.split("/")
    if core_clips.is_clip_path(rel):
        return title, str(fm.get("source") or "剪藏"), url
    if core_jottings.is_path(rel):
        return title, "随手记", url
    if parts[0] == core_vault.REPORTS_DIRNAME:
        return title, "复盘" if len(parts) > 2 and parts[1] == "复盘" else "报告", url
    kind = next((label for d, label in _DRAFT_KINDS.items() if d in parts[1:-1]), "")
    show = parts[1] if len(parts) > 2 else ""
    return title, "・".join(x for x in (show, kind) if x), url


def _note_page_url(rel: str) -> str:
    if rel.startswith(core_vault.REPORTS_DIRNAME + "/"):
        return _url(REPORTS + rel[len(core_vault.REPORTS_DIRNAME):])
    return _url(rel.split("/", 1)[1])


def _reflection_url(rel: str) -> str:
    """一篇的读书笔记（读后感在最上面）：/notes?path=…，只列这一篇。"""
    return f"{request.script_root}/notes?" + urllib.parse.urlencode({"path": rel})


@app.route("/notes")
def reading_notes():
    root = core_vault.vault_root()
    only = request.args.get("path") or ""   # 笔记页「读后感」里点进来的：只看这一篇
    parts = []
    for rel, mtime, n in core_notes.annotated_docs(root):
        if only and rel != only:
            continue
        try:
            text = _read_utf8(os.path.join(root, *rel.split("/")))
        except (OSError, AnnotateError):
            continue
        title, source, url = _note_origin(rel, text)
        bits = [f'<h2><a href="{html.escape(_note_page_url(rel))}">{html.escape(title)}</a></h2>']
        noted = sum(len(i["notes"]) for i in n["items"]) + len(n["loose"])
        meta = [source, f"{len(n['items'])} 处划线"] + ([f"{noted} 条批注"] if noted else [])
        meta.append(time.strftime("%Y-%m-%d 改过", time.localtime(mtime)))
        bits.append(f'<p class="rn-meta">{html.escape("  ".join(x for x in meta if x))}</p>')
        if n["reflection"]:
            bits.append(f'<div class="rn-refl"><p class="rn-label">读后感</p>{_md(n["reflection"])}</div>')
        last = None
        for it in n["items"]:
            if it["section"] != last and it["section"]:
                bits.append(f'<h3>{html.escape(_plain_inline(it["section"]))}</h3>')
            last = it["section"]
            bits.append(f'<blockquote>{_md(_plain_inline(it["mark"]))}</blockquote>')
            bits += [f'<p class="rn-note">{html.escape(t)}</p>' for t in it["notes"]]
        if n["loose"]:
            bits.append('<h3>其他批注</h3>' + "".join(f'<p class="rn-note">{html.escape(t)}</p>' for t in n["loose"]))
        md = core_notes.note_markdown(title, source, url, n)
        bits.append(f'<div class="rn-row"><button type="button" class="rn-copy" data-md="{html.escape(md)}">'
                    '复制成 Markdown</button></div>')
        parts.append(f'<section class="rnote">{"".join(bits)}</section>')
    intro = ('<h1>读书笔记</h1><p class="rn-intro">剪藏、播客和会议的稿子、信息跟进、报告里划的线、写的批注和读后感，'
             '按最近改过的排。读的时候选中文字点「高亮」「批注」，读完在文末写读后感，这里就会多一篇。</p>')
    if only:
        intro = ""
        crumbs = [("阅读", _url("")), ("笔记", f"{request.script_root}/jottings"), ("读书笔记", "")]
    else:
        crumbs = [("阅读", _url("")), ("读书笔记", "")]
    body = intro + ("".join(parts) if parts else '<p class="rn-empty">还没有读书笔记。</p>')
    return _page("读书笔记", crumbs, f'<div class="rnotes">{body}</div>')


# ---- 笔记：随手记（Spark/笔记/，core/jottings.py）和读后感（写了「## 读后感」的文档，随手记除外）。
# 最上面一张卡只写数字和最近的几条，不调模型（同手机书架「笔记」）。只有划线、批注的在「读书笔记」页（/notes），不在这里

_JOT_SORTS = (("updated", "最近修改"), ("created", "创建时间"), ("name", "名称"))


def _ago(dt: datetime, now: datetime) -> str:
    """今天 21:42 / 昨天 / 3 天前 / 2026-11-02（按本机时区算哪天）。"""
    a, b = doctime.local(dt), doctime.local(now)
    days = (b.date() - a.date()).days
    if days <= 0:
        return "今天 " + a.strftime("%H:%M")
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    return a.strftime("%Y-%m-%d")


@app.route("/jottings")
def jottings_page():
    root = core_vault.vault_root()
    now = doctime.now()
    by = request.args.get("sort") or "updated"
    if by not in dict(_JOT_SORTS):
        by = "updated"
    sr = request.script_root
    new_url = f"{sr}/jottings/new"
    s = core_jottings.summary(root, now)
    items = core_jottings.sort(core_jottings.list_all(root), by)
    esc = html.escape
    parts = ['<h1>笔记</h1><p class="lede">随手记和读后感。随手记是自己写下的，一条一篇；'
             '读后感是读完一篇在文末写下的。只划了线、写了批注的在<a href="' + esc(sr) + '/notes">读书笔记</a>里。</p>']
    if s["jottings"] or s["reflections"]:
        recent = "".join(
            f'<li><a href="{esc(_note_page_url(r["rel"]) if r["kind"] == core_jottings.KIND_JOTTING else _reflection_url(r["rel"]))}"><span class="name">{esc(r["title"])}</span>'
            f'<span class="kind">{esc(r["kind"])}</span><span class="when">{esc(_ago(r["at"], now))}</span></a></li>'
            for r in s["recent"])
        parts.append(
            f'<section class="jt-card" aria-label="最近更新"><p class="jt-head">最近更新 · {esc(_ago(s["latest"], now))}</p>'
            f'<p class="jt-counts">随手记 {s["jottings"]} 篇 · 读后感 {s["reflections"]} 篇</p>'
            f'<p class="jt-week">近 7 天：新增 {s["added"]}，修改 {s["edited"]}</p>'
            f'<ul class="jt-recent">{recent}</ul></section>')
    else:
        parts.append('<p class="empty">还没有笔记。想到什么随手记一条；读完一篇在文末写下读后感，也会收在这里。</p>'
                     f'<p><a class="btn" href="{esc(new_url)}">写一条</a></p>')
    if items:
        current = ' aria-current="page"'
        nav = "".join(f'<a href="?{urllib.parse.urlencode({"sort": v})}"{current if v == by else ""}>{t}</a>'
                      for v, t in _JOT_SORTS)
        rows = []
        for j in items:
            rel = j["rel"].split("/", 1)[1]
            when = j["created"] if by == "created" else j["updated"]
            star = '<span class="kind" aria-label="星标">★</span>' if j["starred"] else ""
            edit = f"{sr}/jottings/edit?" + urllib.parse.urlencode({"path": rel})
            tail = f'{esc(j["excerpt"])}　' if j["excerpt"] else ""
            rows.append(f'<li class="clip"><a href="{esc(_url(rel))}"><span class="date">{esc(_ago(when, now))}</span>'
                        f'<span class="name">{esc(j["title"])}</span>{star}</a>'
                        f'<p class="clip-tldr">{tail}<a class="jt-editlink" href="{esc(edit)}">编辑</a></p></li>')
        parts.append(f'<h2>随手记<span class="count">{len(items)} 篇</span></h2>'
                     f'<nav class="tagbar" aria-label="排序">{nav}</nav><ul class="list clips">{"".join(rows)}</ul>')
    refl = core_jottings.reflection_docs(root)
    if refl:
        rows = []
        for rel, mtime, n in refl:
            try:
                text = _read_utf8(os.path.join(root, *rel.split("/")))
            except (OSError, AnnotateError):
                continue
            title, source, _url_ = _note_origin(rel, text)
            first = next((ln.strip() for ln in n["reflection"].split("\n") if ln.strip()), "")   # 同手机 ReadingNote.teaser
            rows.append(f'<li class="clip"><a href="{esc(_reflection_url(rel))}"><span class="date">'
                        f'{esc(_ago(datetime.fromtimestamp(mtime).astimezone(), now))}</span>'
                        f'<span class="name">{esc(title)}</span></a>'
                        f'<p class="clip-tldr">{esc(first)}</p><p class="jt-source">{esc(source)}</p></li>')
        parts.append(f'<h2>读后感<span class="count">{len(rows)} 篇</span></h2><ul class="list clips">{"".join(rows)}</ul>')
    actions = f'<a class="btn" href="{esc(new_url)}">写一条</a>'
    return _page("笔记", [("阅读", _url("")), ("笔记", "")], f'<div class="jottings">{"".join(parts)}</div>', actions=actions)


@app.route("/jottings/new")
@app.route("/jottings/edit")
def jotting_editor():
    """写一条、改一条：标题 + 正文。脚本（static/jottings.js）把打的字随时存在浏览器本地，⌘S 保存；
    保存时带上打开时那一版的指纹，文件在别处改过就不覆盖。"""
    sr = request.script_root
    rel, title, body, base = "", "", "", ""
    back = f"{sr}/jottings"
    if request.path.endswith("/edit"):
        path = _resolve(request.args.get("path") or "")
        if not path or not os.path.isfile(path) or not core_jottings.is_path(_vault_rel(path)):
            abort(404)
        try:
            text = _read_utf8(path)
        except (OSError, AnnotateError):
            abort(404)
        rel = _rel(path)
        title = core_jottings.parse(text, os.path.basename(path))["title"]
        body = core_jottings.editable_body(text)
        base = core_jottings.sha256(text)
        back = _url(rel)
    heading = "改随手记" if base else "写一条笔记"
    esc = html.escape
    hint = ('<p class="jt-hint">划线、批注、读后感在阅读页上加。框里的 ==…== 是划线，[^note-1] 是批注号；'
            '删掉批注号，那条批注跟着删。读后感和批注的内容不在框里，不会被改动。</p>') if base else ""
    form = (f'<form class="jt-edit" id="jt-edit" data-api="{esc(sr)}/api" data-path="{esc(rel)}" data-base="{esc(base)}">'
            f'<h1>{heading}</h1>'
            '<label class="jt-label" for="jt-title">标题</label>'
            f'<input id="jt-title" type="text" maxlength="{core_jottings.MAX_TITLE}" autocomplete="off" '
            f'placeholder="可以不填，用正文第一行" value="{esc(title)}">'
            '<label class="jt-label" for="jt-body">正文</label>'
            f'<textarea id="jt-body" rows="18" maxlength="{core_jottings.MAX_BODY}" placeholder="想到什么写什么，可以用 Markdown">'
            f'{esc(body)}</textarea>{hint}'
            '<div class="jt-row"><button type="submit" class="jt-save">保存</button>'
            f'<a class="jt-cancel" href="{esc(back)}">取消</a>'
            '<span class="jt-status" role="status" aria-live="polite"></span></div></form>')
    return _page(heading, [("阅读", _url("")), ("笔记", f"{sr}/jottings"), (heading, "")], form, scripts=("jottings.js",))


@app.route("/api/jottings/save", methods=["POST"])
def api_jottings_save():
    """存随手记：带 path（相对 Spark/）是改那一条，要带 base（打开时那一版的 SHA-256）；不带 path 是新建一条。"""
    data = request.get_json(silent=True) or {}
    title, body = str(data.get("title") or ""), str(data.get("body") or "")
    rel = str(data.get("path") or "")
    try:
        if rel:
            path = _resolve(rel)
            if not path or not core_jottings.is_path(_vault_rel(path)):
                raise core_jottings.JottingError("这不是一条随手记")
            core_jottings.update(_vault_rel(path), title, body, str(data.get("base") or ""))
        else:
            path = os.path.join(core_vault.vault_root(), *core_jottings.create(title, body).split("/"))
        text = _read_utf8(path)
    except core_jottings.Conflict as e:
        return jsonify({"error": str(e)}), 409
    except (core_jottings.JottingError, AnnotateError) as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "path": _rel(path), "url": _url(_rel(path)), "base": core_jottings.sha256(text)})


@app.route("/api/retitle", methods=["POST"])
def api_retitle():
    """Mac App 里改剪藏、复盘的名称。"""
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    try:
        path = _annotate_target(data)
        if not _retitle_ok(path):
            raise AnnotateError("只有剪藏和复盘能改名称")
        if not title or len(title) > 200 or "\n" in title:
            raise AnnotateError("名称不能是空的，也不能太长")
        full, _, _ = _read_checked(path, str(data.get("mtime") or ""))
        atomic.write_text(path, _retitled(full, title))
        if core_clips.is_clip_path(_vault_rel(path)):
            core_clips.rebuild_index()
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), "title": title})


@app.route("/api/event_dates", methods=["POST"])
def api_event_dates():
    """会议文件夹页上改会议时间：{folder, start, end}，start 空着是清掉手动填的（回到自动）。
    拿着跟流水线同一把处理记录的锁读改写（core/summit_dates.set_manual），跨站请求由 web_guard 挡掉。"""
    data = request.get_json(silent=True) or {}
    start, end = data.get("start") or "", data.get("end") or ""
    if not isinstance(start, str) or not isinstance(end, str):
        return jsonify({"error": "日期要写成 2026-10-13 这样"}), 400
    folder = _event_folder(str(data.get("folder") or ""))
    if folder is None:
        return jsonify({"error": "只有会议文件夹能改会议时间"}), 400
    try:
        r = summit_dates.set_manual(folder, start, end)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写处理记录失败：{e}"}), 500
    _EVENT_CACHE.pop(folder, None)
    return jsonify({"ok": True, "eventDates": _event_json(r)})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """Mac App 里删一篇剪藏或复盘：移进废纸篓，跟手机删了交给电脑的做法一样（remote_tasks.delete_file）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _annotate_target(data)
        if not _retitle_ok(path):
            raise AnnotateError("只有剪藏和复盘能删")
        # source=mac：没有手机废纸篓，不会有「彻底删除」来释放图片，清理工具把它用过的图按拿不准算
        message = remote_tasks.delete_file({"path": _vault_rel(path), "source": "mac"})
    except (AnnotateError, remote_tasks.TaskError) as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"移进废纸篓失败：{e}"}), 500
    return jsonify({"ok": True, "message": message})


def _clip_image_target(data: dict) -> tuple[str, str]:
    path = _annotate_target(data)
    if not core_clips.is_clip_path(_vault_rel(path)):
        raise AnnotateError("只有剪藏里的图能重下、删除")
    src = str(data.get("src") or "")
    if not core_clips.local_src_ok(src):
        raise AnnotateError("这张图不是剪藏存下的")
    return path, src


@app.route("/api/clip_image", methods=["POST"])
def api_clip_image():
    """剪藏里的一张图：action=retry 打开原文找到地址重下，action=remove 从文章里删掉（同手机）。"""
    data = request.get_json(silent=True) or {}
    try:
        path, src = _clip_image_target(data)
        full, _, _ = _read_checked(path, str(data.get("mtime") or ""))
        if data.get("action") == "remove":
            new = core_clips.without_image(full, src)
            if new == full:
                raise AnnotateError("文章里没有这张图")
            atomic.write_text(path, new)
        elif data.get("action") == "retry":
            why = core_clips.redownload_image(full, src)
            if why:
                raise AnnotateError(why)
        else:
            raise AnnotateError("不认识的操作")
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path, str(data.get("view") or ""))})


@app.route("/api/followup", methods=["POST"])
def api_followup():
    """复盘继续追问（同手机）：答案接在复盘末尾，返回重新排好的正文。"""
    from core import review as core_review
    data = request.get_json(silent=True) or {}
    try:
        path = _annotate_target(data)
        rel = _vault_rel(path)
        if not core_review.is_review_path(rel):
            raise AnnotateError("只有复盘能继续追问")
        _read_checked(path, str(data.get("mtime") or ""))
        core_review.followup(rel, str(data.get("question") or ""))
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 409 if "改过了" in str(e) else 400
    except core_review.ReviewError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"写文件失败：{e}"}), 500
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path, str(data.get("view") or ""))})


@app.route("/api/read_ping", methods=["POST"])
def api_read_ping():
    """页面打开停留够久才算读过（static/reader.js 发来）：只点开看一眼不进阅读记录，复盘才准。"""
    try:
        path = _annotate_target({"path": str((request.get_json(silent=True) or {}).get("path") or "")})
    except AnnotateError as e:
        return jsonify({"error": str(e)}), 400
    _log_read(path)
    return jsonify({"ok": True})


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
        before = _read_utf8(path)
        if data.get("remove"):
            try:
                nth = int(data.get("nth") or 0)
            except (TypeError, ValueError):
                raise AnnotateError("nth 要是数字") from None
            _unhighlight(path, str(data.get("mtime") or ""), text, nth)
        else:
            _highlight(path, str(data.get("mtime") or ""), text, str(data.get("before") or ""))
        _remember_edit(path, before)
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


@app.route("/api/clips", methods=["POST"])
def api_add_clip():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="剪藏内容格式不对"), 400
    try:
        return jsonify(clip_capture.start(data)), 202
    except remote_tasks.TaskError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/clips/fill", methods=["POST"])
def api_fill_clips():
    """已存的剪藏补摘要、补翻译（Mac App）：缺什么补什么，或者摘要重写。"""
    data = request.get_json(silent=True)
    if isinstance(data, dict) and isinstance(data.get("paths"), list):
        # Mac 那边给的是阅读页的路径（剪藏/…），换成笔记库里的
        data = dict(data, paths=[p if not isinstance(p, str) or p.startswith(core_clips.CLIPS_DIR + "/")
                                 else f"{core_vault.SPARK_DIRNAME}/{p}" for p in data["paths"]])
    try:
        return jsonify(clip_capture.start_fill(data)), 202
    except remote_tasks.TaskError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/clips/<job_id>")
def api_clip_status(job_id):
    result = clip_capture.status(job_id)
    return (jsonify(result), 200) if result else (jsonify(error="找不到剪藏任务"), 404)


if __name__ == "__main__":
    app.run("127.0.0.1", 8766, debug=False, threaded=True)
