"""
Spark 阅读：在浏览器里直接读笔记库 Spark/ 目录下生成的 Markdown。

平时由 spark.py 挂在 /read 下。页面在服务端渲染好（mistune）。整理稿里「原文段落 +
中文引用块」的对照结构，宽屏下排成左右两栏。

排版像 Safari 阅读模式那样可以现场切换：顶栏「Aa」里选配色、字体、字号和栏宽，
全靠 CSS 变量，选择存在浏览器本地（static/theme.js 在首屏前套上，避免闪一下）。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse

import mistune
from flask import Flask, abort, redirect, request, send_file, send_from_directory
from mistune.renderers.html import HTMLRenderer

from core import vault as core_vault
from core import web_guard

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")


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
                              plugins=["table", "strikethrough", "url"])

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
        body = f'<article class="doc">{body}</article><section class="files"><h2>文件夹</h2>{_listing(path)}</section>'
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
                 f'<article class="doc">{body}</article>', wide=wide, actions=_actions(rel))


@app.route("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)



if __name__ == "__main__":
    app.run("127.0.0.1", 8766, debug=False, threaded=True)
