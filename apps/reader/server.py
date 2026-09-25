"""
Spark 阅读：在浏览器里直接读笔记库 Spark/ 目录下生成的 Markdown。

平时由 spark.py 挂在 /read 下。页面在服务端渲染好（mistune）。整理稿里「原文段落 +
中文引用块」的对照结构，宽屏下排成左右两栏。

排版像 Safari 阅读模式那样可以现场切换：顶栏「Aa」里选配色、字体、字号和栏宽，
全靠 CSS 变量，选择存在浏览器本地（static/theme.js 在首屏前套上，避免闪一下）。

读的时候选中文字可以「高亮」或「摘录」：
- 高亮直接写回原文件，用 Obsidian 自己的 ==文字== 语法，两边看到的一样；是某一期的
  笔记、整理稿或文字记录的话，再在这一期 notes/ 里的笔记末尾「我的高亮」记一条（重新
  生成小结时这一节会被接回去，整理稿重跑丢了行内高亮，这里也还在）；
- 摘录追加到 Spark/摘录.md（新的在上面），带出处链接和可选的想法，同时把这段高亮。
写之前核对页面打开时文件的修改时间，文件在别处（比如 Obsidian）改过就拒绝，免得覆盖。
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
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------------------------------------------------------- 路径

def _resolve(rel: str) -> str | None:
    """rel 是相对 Spark/ 的路径。点开头的（.cache、.manifest.json）
    一律不给看，也不许 .. 跳出 Spark/。"""
    rel = rel.strip("/")
    parts = [p for p in rel.split("/") if p]
    if any(p.startswith(".") for p in parts):
        return None
    root = os.path.realpath(_root())
    path = os.path.realpath(os.path.join(root, *parts))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path


def _rel(path: str) -> str:
    return os.path.relpath(path, os.path.realpath(_root())).replace(os.sep, "/")


def _url(rel: str, *, is_dir: bool = False) -> str:
    rel = rel.strip("/")
    tail = urllib.parse.quote(rel) + ("/" if is_dir and rel else "")
    return f"{request.script_root}/f/{tail}" if rel else f"{request.script_root}/"


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


def render_markdown(text: str, here_dir: str, *, bilingual: bool = False) -> tuple[str, str]:
    """返回 (标题, 正文 HTML)。"""
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
    if meta:
        rows = "".join(f"<dt>{html.escape(k)}</dt><dd>{_inline(v, here_dir)}</dd>" for k, v in meta)
        out = f'<dl class="meta">{rows}</dl>\n' + out
    return title, out


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
    f'<div class="row" role="group" aria-label="字体">{_choices("font", _FONTS)}</div>'
    '<div class="row" role="group" aria-label="字号">'
    '<button type="button" class="opt" data-size="-1" aria-label="缩小字号">A−</button>'
    '<output id="size-now" aria-live="polite"></output>'
    '<button type="button" class="opt" data-size="+1" aria-label="放大字号"><span class="big">A+</span></button></div>'
    f'<div class="row" role="group" aria-label="栏宽">{_choices("width", _WIDTHS)}</div>'
    '</div>'
)


def _page(title: str, crumbs: list[tuple[str, str]], body: str, *, wide: bool = False,
          actions: str = "") -> str:
    sr = request.script_root
    nav = " <span class=\"sep\">/</span> ".join(
        f'<a href="{html.escape(u)}">{html.escape(t)}</a>' if u else f"<span>{html.escape(t)}</span>"
        for t, u in crumbs)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title if title == "Spark 阅读" else f"{title} · Spark 阅读")}</title>
<link rel="icon" href="/static/icon.png" />
<link rel="stylesheet" href="{sr}/static/reader.css" />
<script src="{sr}/static/theme.js"></script>
</head>
<body>
<header class="bar"><nav class="crumbs">{nav}</nav><div class="actions">{actions}{_SETTINGS}</div></header>
<main class="{'wide' if wide else ''}">
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


def _listing(path: str) -> str:
    dirs, files = [], []
    for name in os.listdir(path):
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            dirs.append(name)
        elif name.endswith(".md"):
            files.append(name)
    rows = []
    for name in sorted(dirs):
        n, _ = _count_md(os.path.join(path, name))
        rows.append(f'<li class="dir"><a href="{html.escape(_url(_rel(os.path.join(path, name)), is_dir=True))}">'
                    f'<span class="name">{html.escape(name)}/</span><span class="count">{n} 篇</span></a></li>')
    for name in sorted(files, reverse=True):
        date, label = _entry_label(name)
        rows.append(f'<li><a href="{html.escape(_url(_rel(os.path.join(path, name))))}">'
                    f'<span class="date">{date}</span><span class="name">{html.escape(label)}</span></a></li>')
    return f'<ul class="list">{"".join(rows)}</ul>' if rows else '<p class="empty">这个文件夹里没有 Markdown 文件。</p>'


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
    body = (f'<h1>Spark 阅读</h1><p class="lede">笔记库 Spark 目录下的会议、播客和栏目，按最近更新排列。</p>'
            f'<ul class="list">{rows}</ul>')
    if loose_html:
        body += f'<h2>其他文件</h2><ul class="list">{loose_html}</ul>'
    return _page("Spark 阅读", [("Spark", "")], body)


@app.route("/f/")
def root_redirect():
    return redirect(_url(""))


@app.route("/f/<path:rel>")
def view(rel: str):
    path = _resolve(rel)
    if not path or not os.path.exists(path):
        abort(404)
    rel = _rel(path)
    if os.path.isdir(path):
        if not request.path.endswith("/"):
            return redirect(_url(rel, is_dir=True))
        return _dir_page(path, rel)
    if not path.endswith(".md"):
        return send_file(path)
    return _file_page(path, rel)


def _dir_page(path: str, rel: str):
    name = os.path.basename(path)
    home = os.path.join(path, name + ".md")
    if os.path.isfile(home):
        # 节目 / 会议文件夹：直接把主页排出来，文件列表放在后面
        with open(home, encoding="utf-8", errors="replace") as f:
            title, body = render_markdown(f.read(), path)
        body = f'{_article(home, body)}<section class="files"><h2>文件夹</h2>{_listing(path)}</section>'
        actions = _actions(_rel(home))
        return _page(title or name, _crumbs(rel), body, actions=actions)
    body = f"<h1>{html.escape(name)}</h1>{_listing(path)}"
    return _page(name, _crumbs(rel), body)


def _actions(rel: str) -> str:
    vault = core_vault.vault_root()
    obs = "obsidian://open?" + urllib.parse.urlencode(
        {"vault": os.path.basename(vault.rstrip("/")), "file": f"{core_vault.SPARK_DIRNAME}/{rel}"},
        quote_via=urllib.parse.quote)
    return f'<a class="btn" href="{html.escape(obs)}">在 Obsidian 打开</a>'


def _file_page(path: str, rel: str):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    bilingual = "/speech/" in f"/{rel}"
    title, body = render_markdown(text, os.path.dirname(path), bilingual=bilingual)
    wide = bilingual and 'class="pair"' in body
    return _page(title or os.path.basename(path)[:-3], _crumbs(rel),
                 _article(path, body), wide=wide, actions=_actions(rel))


def _mtime(path: str) -> str:
    return str(os.stat(path).st_mtime_ns)


def _article(path: str, body: str) -> str:
    """正文外面这一层带上高亮 / 摘录要用的信息：哪个文件、打开时的修改时间。"""
    rel = _rel(path)
    excerptable = "false" if rel == EXCERPTS_NAME else "true"
    return (f'<article class="doc" data-path="{html.escape(rel)}" data-mtime="{_mtime(path)}" '
            f'data-api="{request.script_root}/api" data-excerptable="{excerptable}">{body}</article>')


def _render_body(path: str) -> str:
    rel = _rel(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        return render_markdown(f.read(), os.path.dirname(path), bilingual="/speech/" in f"/{rel}")[1]


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)



# ---------------------------------------------------------------- 高亮 / 摘录

class AnnotateError(Exception):
    pass


# 页面上的文字和 Markdown 源码之间差着格式符号：选中的 "foo bar" 在源码里可能是
# "foo **bar**"，也可能在软换行处断成两行（引用块里下一行还带 "> "）
_MARKUP = r"(?:\*\*|__|~~|==|[*_`])*"
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
                if not os.path.isfile(note_path):
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


def _record_highlight(path: str, span: str) -> None:
    hit = _episode_note(path)
    if not hit:
        return
    note, label = hit
    line = f"- {_flat(span)}"
    if label:
        link = os.path.relpath(path, os.path.dirname(note)).replace(os.sep, "/")
        line += f"（[{label}](<{link}>)）"
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


_SOURCE_SUFFIX_RE = re.compile(r"（\[[^\]]*\]\(<[^>]*>\)）$")


def _forget_highlight(path: str, text: str) -> None:
    hit = _episode_note(path)
    if not hit:
        return
    note, _ = hit
    with open(note, encoding="utf-8") as f:
        full = f.read()
    bounds = _section_bounds(full)
    if not bounds:
        return
    a, b = bounds
    lines = full[a:b].split("\n")
    want = _norm(text)
    for i, ln in enumerate(lines):
        if ln.startswith("- ") and _norm(_SOURCE_SUFFIX_RE.sub("", ln[2:])) == want:
            del lines[i]
            break
    else:
        return
    if any(ln.startswith("- ") for ln in lines):
        full = full[:a] + "\n".join(lines) + full[b:]
    else:
        # 最后一条也删了：标题一起去掉，前后多出来的空行收掉
        full = full[:a].rstrip("\n") + "\n" + full[b:].lstrip("\n")
    atomic.write_text(note, full)


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
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path)})


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
    return jsonify({"ok": True, "mtime": _mtime(path), "html": _render_body(path), "note": note,
                    "excerpts_url": _url(saved)})


if __name__ == "__main__":
    app.run("127.0.0.1", 8766, debug=False, threaded=True)
