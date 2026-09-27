"""
导入 Typora / Obsidian 主题：把主题 CSS 里能套到阅读页正文上的部分挑出来，其余一律去掉。

主题是给编辑器写的：Typora 的正文在 #write 里，Obsidian 的在 .markdown-preview-view /
.markdown-rendered 里，配色多半写成 CSS 变量（--text-normal、--background-primary、
--bg-color……），夜间版本挂在 .theme-dark 或 @media (prefers-color-scheme: dark) 上。
这里用 tinycss2 解析，逐条规则判断：

- 选择器：开头的 html / body / :root / .theme-dark / .theme-light 只当「这是哪个配色版本」，
  接着的 #write、.markdown-preview-view 这类换成阅读页正文的根
  :is(html.reader.theme-custom article.doc, .read-scope.theme-custom)，后面只许跟
  标题、段落、链接、引用、代码、表格、列表这些元素（外加 :hover、:first-child 之类）。
  打到侧栏、标题栏、编辑区、阅读页自己的顶栏 /「Aa」/ 目录上的，整条去掉。
- 属性：只留颜色、背景色（渐变可以，url() 不行）、字体、文字装饰、边框、圆角、内外边距、
  列表样式、阴影、对齐这些；定位、层级、尺寸、变形、动画、content、带 url() 的一律去掉。
  @import、@font-face 这种会去外面拉东西的整条去掉。
- 变量：主题自己的变量全部改名成 --tt-*（免得撞上阅读页的 --paper、--ink……），值里带
  url() 的去掉。认得的几组（Obsidian --background-primary / --text-normal / --text-accent /
  --h1-color…，Typora --bg-color / --text-color / --side-bar-bg-color…）再接到阅读页自己的
  配色 token 上，这样整页的底色、文字、链接、标题颜色都跟着主题走。
- 深浅色：.theme-light / .theme-dark 和 prefers-color-scheme 分别落到
  @media not all and (prefers-color-scheme: dark) / @media (prefers-color-scheme: dark) 里，
  跟着系统切换（用了自定义主题时阅读页按「自动」配色处理）。

产物存在 ~/.spark/themes/<id>.css（SPARK_THEMES_DIR 可以改，没设时跟着 SPARK_SETTINGS_FILE
所在的目录走，测试靠这个不碰真目录），旁边一份 index.json 记名字、来源、导入时间和
保留 / 去掉了多少。阅读页以同源样式表 /read/themes/<id>.css 加载，CSP 不用放松。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

import tinycss2
from tinycss2 import ast

from core import atomic
from core import settings as core_settings

MAX_CSS_BYTES = 1_000_000
MAX_RULES = 20_000
MAX_THEMES = 50
MAX_VALUE_CHARS = 2_000
MAX_DETAILS = 200
MAX_NAME_CHARS = 60

ID_RE = re.compile(r"^t[0-9a-f]{10}$")

# 阅读页正文的根：阅读页是 <html class="reader theme-custom"> 里的 <article class="doc">，
# 设置页是挂了 .read-scope.theme-custom 的预览框
SCOPE = ":is(html.reader.theme-custom article.doc, .read-scope.theme-custom)"
TOKEN_ROOTS = ("html.reader.theme-custom", ".read-scope.theme-custom")
TOKEN_SCOPE = ", ".join(TOKEN_ROOTS)
MEDIA = {
    "light": "@media not all and (prefers-color-scheme: dark)",
    "dark": "@media (prefers-color-scheme: dark)",
}

REASONS = {
    "external": "外链资源",
    "ui": "影响页面控件",
    "editor": "编辑器界面",
    "selector": "不支持的选择器",
    "layout": "定位、尺寸或动画",
    "pseudo": "伪元素装饰",
    "property": "不支持的属性",
    "unsafe": "不安全的写法",
    "atrule": "不支持的 @ 规则",
    "syntax": "语法错误",
}


class ThemeError(ValueError):
    """导入失败：message 直接给用户看；report 有的话是去掉了什么。"""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report


# ---------------------------------------------------------------- 选择器

ELEMENTS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "blockquote", "code", "pre", "table", "thead",
    "tbody", "tfoot", "tr", "th", "td", "caption", "ul", "ol", "li", "hr", "mark", "img", "strong",
    "em", "b", "i", "del", "s", "sup", "sub", "kbd", "dl", "dt", "dd", "figure", "figcaption",
}
# 编辑器里的类名换成阅读页里对应的元素
CLASS_TO_ELEMENT = {"md-fences": "pre", "external-link": "a", "internal-link": "a"}
ROOT_IDS = {"write"}
ROOT_CLASSES = {
    "markdown-preview-view", "markdown-rendered", "markdown-reading-view",
    "markdown-preview-section", "markdown-preview-sizer",
}
CONTEXT_TAGS = {"html", "body"}
CONTEXT_CLASSES = {"theme-dark", "theme-light", "typora-export"}
PSEUDO_CLASSES = {
    "hover", "focus", "focus-visible", "active", "visited", "link", "first-child", "last-child",
    "only-child", "first-of-type", "last-of-type", "only-of-type", "empty",
}
PSEUDO_FUNCS = {"nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"}
PSEUDO_ELEMENTS = {"marker", "first-letter", "first-line", "selection"}
DECOR_PSEUDOS = {"before", "after"}

# 阅读页自己的界面：顶栏、「Aa」、高亮工具条、提示、目录……
UI_TAGS = {"header", "nav", "footer", "main", "aside", "button", "input", "select", "textarea",
           "details", "summary", "svg", "html", "body", "form", "label", "dialog"}
UI_NAMES = {
    "bar", "prefs", "selbar", "toast", "crumbs", "actions", "btn", "opt", "sw", "swatches", "more-prefs",
    "toc", "toc-top", "toc-side", "toc-list", "toc-h", "with-toc", "content", "docmeta", "meta",
    "meta-list", "pair", "orig", "tr", "list", "chip", "excerpt-form", "reader", "read-scope",
    "theme-custom", "doc", "aa", "size-now", "episodes", "custom-theme",
}
# 编辑器自己的界面（侧栏、标题栏、编辑区、弹窗、插件……）：认出来是为了告诉用户「这部分没法照搬」
EDITOR_RE = re.compile(
    r"^(workspace|nav-|side-dock|sidebar|typora|titlebar|title-bar|view-|status-bar|cm-|codemirror|"
    r"hypermd|markdown-source-view|markdown-embed|modal|menu|mod-|tree-item|md-|ty-|megamenu|file-|"
    r"outline|popover|prompt|setting|vertical-tab|app-|horizontal-|is-|mobile|suggestion|search|"
    r"frontmatter|metadata|inline-title|callout|tag|pdf|canvas|graph|ribbon|clickable|backlink|"
    r"footnote|task-list|dataview|internal-embed|image-embed|collapse|heading-collapse|el-|"
    r"block-language|copy-code|code-block|list-bullet|list-collapse|lp-|info-panel|context-menu|"
    r"dropdown|window|toolbar|mac-|win-|os-|unibody|quick-open|export|mathjax|katex|plugin|community)",
    re.I)


class _Drop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _split_commas(tokens: list) -> list[list]:
    out, cur = [], []
    for t in tokens:
        if t.type == "literal" and t.value == ",":
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return out


def _name_reason(name: str) -> str:
    low = name.lower()
    if low in UI_NAMES or low.startswith(("read-", "toc-", "spark")):
        return "ui"
    if EDITOR_RE.match(name):
        return "editor"
    return "selector"


def _nth_ok(args: list) -> bool:
    return all(t.type in ("whitespace", "number", "dimension", "ident") or
               (t.type == "literal" and t.value in "+-") for t in args) and bool(args)


def _compounds(tokens: list) -> list[tuple[str, dict]]:
    """一个选择器 → [(前面的组合符, 复合选择器)]。认不出的写法抛 _Drop。"""
    toks = list(tokens)
    while toks and toks[0].type == "whitespace":
        toks.pop(0)
    while toks and toks[-1].type == "whitespace":
        toks.pop()
    if not toks:
        raise _Drop("syntax")
    out: list[tuple[str, dict]] = []
    comb = ""
    cur: dict | None = None
    i = 0

    def new():
        return {"tag": None, "universal": False, "ids": [], "classes": [], "pseudos": [], "attrs": 0}

    while i < len(toks):
        t = toks[i]
        if t.type == "whitespace" or (t.type == "literal" and t.value in (">", "+", "~")):
            # 组合符：连着的空白和 > + ~ 合成一个
            c = " "
            while i < len(toks) and (toks[i].type == "whitespace" or
                                     (toks[i].type == "literal" and toks[i].value in (">", "+", "~"))):
                if toks[i].type == "literal":
                    if c != " ":
                        raise _Drop("selector")
                    c = toks[i].value
                i += 1
            if cur is None:
                raise _Drop("selector")
            out.append((comb, cur))
            cur, comb = None, c
            continue
        if cur is None:
            cur = new()
        if t.type == "ident":
            if cur["tag"] or cur["universal"] or cur["ids"] or cur["classes"] or cur["pseudos"]:
                raise _Drop("selector")
            cur["tag"] = t.lower_value
        elif t.type == "literal" and t.value == "*":
            cur["universal"] = True
        elif t.type == "hash":
            cur["ids"].append(t.value)
        elif t.type == "literal" and t.value == ".":
            if i + 1 >= len(toks) or toks[i + 1].type != "ident":
                raise _Drop("syntax")
            cur["classes"].append(toks[i + 1].value)
            i += 1
        elif t.type == "literal" and t.value == ":":
            elem = i + 1 < len(toks) and toks[i + 1].type == "literal" and toks[i + 1].value == ":"
            if elem:
                i += 1
            if i + 1 >= len(toks):
                raise _Drop("syntax")
            nt = toks[i + 1]
            if nt.type == "ident":
                cur["pseudos"].append((nt.lower_value, None, elem))
            elif nt.type == "function":
                cur["pseudos"].append((nt.lower_name, nt.arguments, elem))
            else:
                raise _Drop("syntax")
            i += 1
        elif t.type == "[] block":
            cur["attrs"] += 1
        else:
            raise _Drop("selector")
        i += 1
    if cur is None:
        raise _Drop("selector")
    out.append((comb, cur))
    return out


def _is_context(c: dict) -> str | None:
    """html / body / :root / .theme-dark / .theme-light 这种只说明「在哪个版本下」的：
    返回 'base' / 'light' / 'dark'；不是就返回 None。"""
    if c["ids"] or c["attrs"] or c["universal"] or (c["tag"] and c["tag"] not in CONTEXT_TAGS):
        return None
    if any(p != ("root", None, False) for p in c["pseudos"]):
        return None
    if not set(c["classes"]) <= CONTEXT_CLASSES:
        return None
    if not (c["tag"] or c["classes"] or c["pseudos"]):
        return None
    if "theme-dark" in c["classes"] and "theme-light" in c["classes"]:
        return None
    if "theme-dark" in c["classes"]:
        return "dark"
    if "theme-light" in c["classes"]:
        return "light"
    return "base"


def _is_root(c: dict) -> bool:
    if c["attrs"] or c["universal"] or c["pseudos"] or (c["tag"] not in (None, "div")):
        return False
    if not (set(c["ids"]) & ROOT_IDS or set(c["classes"]) & ROOT_CLASSES):
        return False
    return set(c["ids"]) <= ROOT_IDS and set(c["classes"]) <= ROOT_CLASSES


def _content(c: dict) -> str:
    """正文里的一个复合选择器 → 输出用的文本（只用白名单里的名字重新拼，不照抄原文）。"""
    if c["universal"] or c["attrs"]:
        raise _Drop("ui" if c["universal"] and not c["tag"] else "selector")
    for i in c["ids"]:
        raise _Drop(_name_reason(i))
    tag = c["tag"]
    if tag and tag not in ELEMENTS:
        raise _Drop("ui" if tag in UI_TAGS else "editor" if tag in ("content", "tt") else "selector")
    for cls in c["classes"]:
        mapped = CLASS_TO_ELEMENT.get(cls)
        if not mapped:
            raise _Drop(_name_reason(cls))
        if tag and tag != mapped:
            raise _Drop("selector")
        tag = mapped
    if not tag:
        raise _Drop("selector")
    out = tag
    for name, args, elem in c["pseudos"]:
        if name in DECOR_PSEUDOS:
            raise _Drop("pseudo")
        if args is None and name in PSEUDO_CLASSES and not elem:
            out += ":" + name
        elif args is None and name in PSEUDO_ELEMENTS:
            out += "::" + name
        elif args is not None and name in PSEUDO_FUNCS and not elem and _nth_ok(args):
            out += f":{name}({tinycss2.serialize(args).strip()})"
        else:
            raise _Drop("editor" if name.startswith("-webkit-scrollbar") else "selector")
    return out


def map_selector(tokens: list, forced: str = "base") -> tuple[str, str, str]:
    """一个选择器 → (版本, 种类, 映射后的后半截)。
    种类：'vars' 只有 html/body/:root 这种上下文（放变量、映射配色 token）；
          'root' 就是正文根；'rule' 正文里的元素，后半截带着开头的组合符。"""
    comps = _compounds(tokens)
    variant = forced
    i = 0
    while i < len(comps):
        v = _is_context(comps[i][1])
        if v is None:
            break
        if v != "base":
            if variant != "base" and variant != v:
                raise _Drop("selector")
            variant = v
        i += 1
    has_root = False
    while i < len(comps) and _is_root(comps[i][1]):
        has_root = True
        i += 1
    rest = comps[i:]
    if not rest:
        if has_root:
            return variant, "root", ""
        return variant, "vars", comps[i - 1][1]["tag"] or ""
    out = ""
    for n, (comb, c) in enumerate(rest):
        text = _content(c)
        if n == 0:
            if not has_root or comb == " ":
                lead = " "
            elif comb == ">":
                lead = " > "
            else:
                raise _Drop("selector")   # #write + p 这种：正文根的兄弟，不在正文里
        else:
            lead = " " if comb == " " else f" {comb} "
        out += lead + text
    return variant, "rule", out


# ---------------------------------------------------------------- 属性

ALLOWED_PROPS = {
    "color", "background-color", "background", "background-image",
    "font", "font-family", "font-size", "font-weight", "font-style", "font-variant", "font-stretch",
    "font-feature-settings", "font-variant-numeric", "font-variant-ligatures", "font-kerning",
    "line-height", "letter-spacing", "word-spacing", "text-transform", "text-indent", "text-shadow",
    "text-decoration", "text-decoration-line", "text-decoration-color", "text-decoration-style",
    "text-decoration-thickness", "text-underline-offset", "text-underline-position",
    "text-align", "vertical-align", "white-space", "word-break", "overflow-wrap", "word-wrap",
    "hyphens", "tab-size", "text-rendering", "font-smoothing", "font-synthesis",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-color", "border-style", "border-width",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-radius", "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "border-collapse", "border-spacing", "caption-side", "empty-cells",
    "outline", "outline-color", "outline-style", "outline-width", "outline-offset",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "margin-block", "margin-block-start", "margin-block-end",
    "margin-inline", "margin-inline-start", "margin-inline-end",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "padding-block", "padding-block-start", "padding-block-end",
    "padding-inline", "padding-inline-start", "padding-inline-end",
    "list-style", "list-style-type", "list-style-position",
    "box-shadow", "box-decoration-break", "display", "overflow", "overflow-x", "overflow-y",
}
LAYOUT_PROPS = {
    "position", "top", "right", "bottom", "left", "inset", "z-index", "float", "clear",
    "width", "height", "min-width", "min-height", "max-width", "max-height",
    "transform", "transform-origin", "translate", "rotate", "scale", "transition", "animation",
    "visibility", "opacity", "clip", "clip-path", "mask", "filter", "backdrop-filter", "zoom",
    "pointer-events", "cursor", "user-select", "resize", "columns", "column-count", "contain",
}
UNSAFE_PROPS = {"behavior", "binding", "-moz-binding"}
DISPLAY_OK = {"block", "inline", "inline-block", "list-item", "flow-root", "table", "table-row",
              "table-cell", "table-header-group", "table-row-group", "inline-table"}
OVERFLOW_OK = {"auto", "scroll", "visible"}
BAD_FUNCS = {"url", "image", "image-set", "element", "cross-fade", "src", "paint"}
UNSAFE_FUNCS = {"expression", "attr", "env"}
COLOR_FUNCS = {"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color",
               "color-mix", "var", "light-dark"}


def _scan(tokens) -> str | None:
    """值里有没有会去外面拉东西或能执行的写法。"""
    for t in tokens:
        if t.type == "url":
            return "external"
        if t.type == "function":
            name = t.lower_name.lstrip("-")
            name = re.sub(r"^(webkit|moz|ms|o)-", "", name)
            if name in BAD_FUNCS:
                return "external"
            if name in UNSAFE_FUNCS:
                return "unsafe"
        if t.type == "string" and re.search(r"javascript:|expression\(|url\(", t.value, re.I):
            return "unsafe"
        if t.type == "{} block" or (t.type == "literal" and t.value in (";", "}", "{")):
            return "syntax"
        if t.type in ("function", "() block", "[] block"):
            inner = t.arguments if t.type == "function" else t.content
            r = _scan(inner)
            if r:
                return r
    return None


def _rename(tokens) -> list:
    """值里的 var(--x) 改成 var(--tt-x)。"""
    out = []
    for t in tokens:
        if t.type == "function":
            args = _rename(t.arguments)
            if t.lower_name == "var":
                args = [ast.IdentToken(a.source_line, a.source_column, "--tt-" + a.value[2:])
                        if a.type == "ident" and a.value.startswith("--") else a for a in args]
            out.append(ast.FunctionBlock(t.source_line, t.source_column, t.name, args))
        elif t.type == "() block":
            out.append(ast.ParenthesesBlock(t.source_line, t.source_column, _rename(t.content)))
        elif t.type == "[] block":
            out.append(ast.SquareBracketsBlock(t.source_line, t.source_column, _rename(t.content)))
        else:
            out.append(t)
    return out


def _significant(tokens) -> list:
    return [t for t in tokens if t.type not in ("whitespace", "comment")]


def is_colorish(tokens) -> bool:
    """单独一个颜色（或者一个 var()）：可以接到阅读页的配色 token 上。"1px solid #ddd" 这种不算。"""
    sig = _significant(tokens)
    if len(sig) != 1:
        return False
    t = sig[0]
    if t.type == "hash":
        return True
    if t.type == "ident":
        return t.lower_value not in ("inherit", "initial", "unset", "revert", "none", "auto", "currentcolor")
    return t.type == "function" and t.lower_name in COLOR_FUNCS


def check_decl(decl) -> tuple[str | None, str]:
    """一条声明 → (理由, 输出文本)。理由为 None 表示留下。"""
    name = decl.lower_name
    if name.startswith("--"):
        if not re.fullmatch(r"--[a-z0-9_-]{1,100}", name):
            return "property", ""
        bad = _scan(decl.value)
        if bad:
            return bad, ""
        value = tinycss2.serialize(_rename(decl.value)).strip()
        if len(value) > MAX_VALUE_CHARS:
            return "property", ""
        return None, f"--tt-{name[2:]}: {value}"
    base = re.sub(r"^-(webkit|moz|ms|o)-", "", name)
    if name in UNSAFE_PROPS or base in UNSAFE_PROPS:
        return "unsafe", ""
    if base == "content":
        return "pseudo", ""
    if base in LAYOUT_PROPS or base.startswith(("animation", "transition", "transform", "inset", "grid", "flex")):
        return "layout", ""
    if base not in ALLOWED_PROPS:
        return ("external" if base in ("list-style-image", "border-image", "border-image-source", "src")
                else "property"), ""
    bad = _scan(decl.value)
    if bad:
        return bad, ""
    sig = _significant(decl.value)
    if not sig:
        return "syntax", ""
    if base == "display" and (len(sig) != 1 or sig[0].type != "ident" or sig[0].lower_value not in DISPLAY_OK):
        return "layout", ""
    if base.startswith("overflow") and base != "overflow-wrap" and any(
            t.type != "ident" or t.lower_value not in OVERFLOW_OK for t in sig):
        return "layout", ""
    value = tinycss2.serialize(_rename(decl.value)).strip()
    if len(value) > MAX_VALUE_CHARS:
        return "property", ""
    return None, f"{name}: {value}{' !important' if decl.important else ''}"


# ---------------------------------------------------------------- 主题变量 → 阅读页 token / 元素样式

# 阅读页的配色 token ← 主题里的变量（按顺序取第一个有的）
TOKEN_SOURCES = [
    ("paper", ["background-primary", "bg-color", "background-color"]),
    ("paper-2", ["background-secondary", "background-primary-alt", "code-background", "side-bar-bg-color",
                 "code-block-bg-color"]),
    ("ink", ["text-normal", "text-color"]),
    ("ink-2", ["text-muted", "blockquote-color", "meta-content-color", "text-faint"]),
    ("accent", ["link-color", "text-accent", "interactive-accent", "primary-color", "accent-color"]),
    ("rule", ["background-modifier-border", "hr-color", "table-border-color", "border-color"]),
    ("chip", ["background-secondary-alt", "tag-background", "background-modifier-hover", "item-hover-bg-color"]),
]
# 主题换了底色时，没给的几个 token 从文字色 / 底色调出来（--hl 是阅读页高亮的底色）
DERIVED_TOKENS = {
    "ink-2": "color-mix(in srgb, var(--ink) 68%, var(--paper))",
    "accent": "color-mix(in srgb, var(--ink) 88%, var(--paper))",
    "rule": "color-mix(in srgb, var(--ink) 20%, var(--paper))",
    "paper-2": "color-mix(in srgb, var(--ink) 7%, var(--paper))",
    "chip": "color-mix(in srgb, var(--ink) 12%, var(--paper))",
    "hl": "color-mix(in srgb, #e8c33a 45%, var(--paper))",
}
# 正文元素 ← 主题变量（Obsidian 的标题 / 链接 / 引用 / 代码变量）
ELEMENT_VARS = [
    ("", "font-family", "font-text-theme", "var(--tt-font-text-theme), var(--body-font)"),
    ("", "line-height", "line-height-normal", None),
    (" :is(code, pre)", "font-family", "font-monospace-theme", "var(--tt-font-monospace-theme), ui-monospace, monospace"),
    (" a", "color", "link-color", None),
    (" a:hover", "color", "link-color-hover", None),
    (" a", "text-decoration-line", "link-decoration", None),
    (" strong", "color", "bold-color", None),
    (" strong", "font-weight", "bold-weight", None),
    (" em", "color", "italic-color", None),
    (" blockquote", "color", "blockquote-color", None),
    (" blockquote", "background-color", "blockquote-background-color", None),
    (" blockquote", "border-left-color", "blockquote-border-color", None),
    (" blockquote", "border-left-width", "blockquote-border-thickness", None),
    (" code", "color", "code-normal", None),
    (" :is(code, pre)", "background-color", "code-background", None),
    (" hr", "border-top-color", "hr-color", None),
    (" mark", "background-color", "text-highlight-bg", None),
    (" :is(th, td)", "border-color", "table-border-color", None),
    (" th", "background-color", "table-header-background", None),
] + [
    (f" h{n}", prop, f"h{n}-{suffix}", None)
    for n in range(1, 7)
    for prop, suffix in (("color", "color"), ("font-size", "size"), ("font-weight", "weight"),
                         ("font-family", "font"), ("line-height", "line-height"), ("font-style", "style"),
                         ("letter-spacing", "letter-spacing"))
]

# html / body 上的普通属性：配色进 token，字体排版并到正文根上，其余算页面级（去掉）
BODY_TOKEN_PROPS = {"color": "ink", "background-color": "paper", "background": "paper"}
BODY_TO_ROOT = {"font-family", "font-size", "line-height", "letter-spacing", "font-weight",
                "text-rendering", "font-feature-settings", "word-spacing"}


def _detect_source(css: str) -> str:
    if re.search(r"#write\b|\.md-fences|--bg-color|typora", css, re.I):
        return "typora"
    if re.search(r"markdown-preview-view|markdown-rendered|theme-dark|--background-primary|--text-normal", css):
        return "obsidian"
    return "css"


def _short(tokens) -> str:
    s = " ".join(tinycss2.serialize(tokens).split())
    return s if len(s) <= 120 else s[:117] + "…"


class _Report:
    def __init__(self):
        self.kept = 0
        self.dropped: dict[str, int] = {}
        self.decl_dropped: dict[str, int] = {}
        self.details: list[dict] = []
        self.mapped: list[str] = []

    def note(self, reason: str, what: str):
        if len(self.details) < MAX_DETAILS:
            self.details.append({"what": what, "reason": reason, "kind": "selector"})

    def drop(self, reason: str, what: str, *, decl: bool = False):
        bucket = self.decl_dropped if decl else self.dropped
        bucket[reason] = bucket.get(reason, 0) + 1
        if len(self.details) < MAX_DETAILS:
            self.details.append({"what": what, "reason": reason, "kind": "decl" if decl else "rule"})


def sanitize(css: str, *, name: str = "") -> tuple[str, dict]:
    """主题 CSS → (只作用在正文上的 CSS, 报告)。完全没有能用的就抛 ThemeError。"""
    if not isinstance(css, str) or not css.strip():
        raise ThemeError("CSS 是空的")
    size = len(css.encode("utf-8", "surrogatepass"))
    if size > MAX_CSS_BYTES:
        raise ThemeError(f"文件太大（{size / 1_000_000:.1f} MB），最多 1 MB")
    if "\x00" in css:
        raise ThemeError("这不像是 CSS 文本（里面有二进制内容）")
    try:
        nodes = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    except Exception as e:   # tinycss2 很宽容，一般不会走到这里
        raise ThemeError(f"CSS 解析失败：{e}") from e
    if len(nodes) > MAX_RULES:
        raise ThemeError(f"规则太多（{len(nodes)} 条），最多 {MAX_RULES} 条")

    rep = _Report()
    # 每个版本：变量声明、token 映射、正文根上的声明、规则
    parts = {v: {"vars": {}, "tokens": {}, "root": [], "rules": []} for v in ("base", "light", "dark")}
    saw_variant = {"light": False, "dark": False}

    def handle_rule(rule, forced: str):
        prelude = _short(rule.prelude)
        if not tinycss2.serialize(rule.prelude).strip():
            rep.drop("syntax", "(空选择器)")
            return
        items = tinycss2.parse_blocks_contents(rule.content, skip_comments=True, skip_whitespace=True)
        decls = [d for d in items if d.type == "declaration"]
        nested = [d for d in items if d.type != "declaration"]
        if not decls and not nested:
            return   # 空规则：不算
        targets: list[tuple[str, str, str]] = []
        reasons: list[str] = []
        partial: list[tuple[str, str]] = []
        for sel in _split_commas(rule.prelude):
            try:
                targets.append(map_selector(sel, forced))
            except _Drop as d:
                reasons.append(d.reason)
                partial.append((d.reason, _short(sel)))
        if not targets:
            reason = _pick(reasons)
            rep.drop(reason, prelude)
            return
        for n in nested:
            rep.drop("syntax" if n.type == "error" else "selector", f"{prelude} 里嵌套的规则", decl=True)
        kept_any = False
        decl_reasons = []
        checked = [(d, *check_decl(d)) for d in decls]
        # 外链图标（a.external-link { background-image: url(…); padding-right: 12px }）去掉了，
        # 给图标留的内边距也一起去掉，不然链接后面平白多一块空
        if any(r == "external" and d.lower_name.startswith("background") for d, r, _t in checked):
            checked = [(d, "external", "") if r is None and d.lower_name.startswith("padding") else (d, r, t)
                       for d, r, t in checked]
        # 普通属性：对这条规则的每个选择器都一样
        plain = [text for d, reason, text in checked if not d.lower_name.startswith("--") and reason is None]
        if any(kind != "vars" for _v, kind, _r in targets):
            decl_reasons += [(reason, d) for d, reason, _t in checked
                             if not d.lower_name.startswith("--") and reason is not None]
        decl_reasons += [(reason, d) for d, reason, _t in checked
                         if d.lower_name.startswith("--") and reason is not None]
        rests: dict[str, list[str]] = {}
        for variant, kind, rest in targets:
            if variant != "base":
                saw_variant[variant] = True
            bucket = parts[variant]
            for d, reason, text in checked:
                if d.lower_name.startswith("--") and reason is None:
                    bucket["vars"][text.split(":", 1)[0]] = text
                    kept_any = True
            if kind == "vars":
                # html / body / :root 上的普通属性：颜色接到配色 token，body 的字体排版并到正文根上
                for d, reason, text in checked:
                    name = d.lower_name
                    if name.startswith("--"):
                        continue
                    if name in BODY_TOKEN_PROPS and reason is None and is_colorish(d.value):
                        bucket["tokens"][BODY_TOKEN_PROPS[name]] = tinycss2.serialize(_rename(d.value)).strip()
                        kept_any = True
                    elif name in BODY_TO_ROOT and reason is None and rest == "body":
                        bucket["root"].append(text)
                        kept_any = True
                    else:
                        decl_reasons.append((reason or "ui", d))
            elif plain:
                kept_any = True
                if kind == "root":
                    bucket["root"].extend(plain)
                elif rest not in rests.setdefault(variant, []):
                    rests[variant].append(rest)
        for variant, sels in rests.items():
            parts[variant]["rules"].append((sels, plain))
        if kept_any:
            rep.kept += 1
            for reason, sel in partial:   # 同一条规则里别的选择器还在：只记一笔，不算去掉一条
                rep.note(reason, sel)
            seen = set()
            for reason, d in decl_reasons:
                key = (reason, d.lower_name)
                if key in seen:
                    continue
                seen.add(key)
                rep.drop(reason, f"{prelude} {{ {d.name}: {_short(d.value)} }}", decl=True)
        else:
            rep.drop(_pick([r for r, _ in decl_reasons] or ["property"]), prelude)

    def handle(nodes, forced: str):
        for node in nodes:
            if node.type == "error":
                rep.drop("syntax", node.message[:120])
            elif node.type == "qualified-rule":
                handle_rule(node, forced)
            elif node.type == "at-rule":
                kw = node.lower_at_keyword
                pre = " ".join(tinycss2.serialize(node.prelude).lower().split())
                if kw == "charset":
                    continue
                if kw in ("import", "font-face"):
                    rep.drop("external", f"@{kw} {pre}".strip())
                elif kw == "media" and node.content is not None:
                    variant = _media_variant(pre)
                    if variant is None or (forced != "base" and variant not in ("base", forced)):
                        rep.drop("atrule", f"@media {pre}")
                        continue
                    if variant != "base":
                        saw_variant[variant] = True
                    inner = tinycss2.parse_rule_list(node.content, skip_comments=True, skip_whitespace=True)
                    handle(inner, variant if variant != "base" else forced)
                else:
                    rep.drop("atrule", f"@{kw} {pre}".strip())

    handle(nodes, "base")

    out, mapped = _emit(parts)
    if rep.kept == 0 or not out.strip():
        report = rep_dict(rep, _detect_source(css), False, [])
        raise ThemeError("没找到能用在正文上的样式。" + summary_text(report), report)
    has_dark = bool(saw_variant["dark"] and _has_content(parts["dark"]))
    header = (f"/* Spark 导入的主题：{_comment_safe(name)}。由阅读页根据原主题生成，"
              f"只作用于正文（{SCOPE}）。请勿手改，重新导入即可。 */\n")
    text = header + out
    _verify(text)
    return text, rep_dict(rep, _detect_source(css), has_dark, mapped)


def _pick(reasons: list[str]) -> str:
    for r in ("external", "unsafe", "ui", "editor", "pseudo", "layout", "selector", "property", "atrule", "syntax"):
        if r in reasons:
            return r
    return "selector"


def _media_variant(pre: str) -> str | None:
    """@media 的条件 → 'dark' / 'light' / 'base'；print、按宽度的这种不支持，返回 None。"""
    p = re.sub(r"\s+", "", pre)
    parts = [x for x in re.split(r"\band\b", pre) if x.strip()]
    rest = [x.strip() for x in parts if "prefers-color-scheme" not in x]
    if any(x not in ("screen", "all", "only screen") for x in rest):
        return None
    if "prefers-color-scheme:dark" in p:
        return "dark"
    if "prefers-color-scheme:light" in p:
        return "light"
    return "base" if rest and "," not in pre else None


def _has_content(part: dict) -> bool:
    return bool(part["vars"] or part["tokens"] or part["root"] or part["rules"])


def _comment_safe(s: str) -> str:
    return re.sub(r"\*/|/\*|[\x00-\x1f]", "", s)[:MAX_NAME_CHARS]


def _tokens(parts: dict, variant: str) -> dict[str, str]:
    """某个版本里阅读页配色 token 接到哪儿。"""
    defined = set(parts[variant]["vars"])
    visible = defined | set(parts["base"]["vars"])
    tokens = {}
    for token, sources in TOKEN_SOURCES:
        src = next((s for s in sources if f"--tt-{s}" in visible), None)
        # 这个版本自己定义了（或者基础版本定义了、这是基础版本本身）才在这里接
        if src and (f"--tt-{src}" in defined or variant == "base"):
            tokens[token] = f"var(--tt-{src})"
    tokens.update(parts[variant]["tokens"])   # body { color / background } 直接写的优先
    return tokens


def _emit(parts: dict) -> tuple[str, list[str]]:
    defined_base = set(parts["base"]["vars"])
    mapped: list[str] = []
    blocks = []
    token_maps = {v: _tokens(parts, v) for v in ("base", "light", "dark")}
    # 主题换了底色、却没给次要文字 / 链接 / 分隔线这些颜色：从它的文字色和底色调出来，
    # 免得顶栏、目录还用内置配色的深蓝、浅灰，在深底上看不清
    derived = {}
    if any("paper" in tm for tm in token_maps.values()):
        derived = {k: v for k, v in DERIVED_TOKENS.items() if k not in token_maps["base"]}
    for variant in ("base", "light", "dark"):
        p = parts[variant]
        if not _has_content(p) and not (variant == "base" and derived):
            continue
        defined = set(p["vars"])
        visible = defined | defined_base
        lines = []
        decls = list(p["vars"].values())
        if variant == "base":
            decls += [f"--{k}: {v}" for k, v in derived.items()]
        tokens = token_maps[variant]
        for token, value in tokens.items():
            decls.append(f"--{token}: {value}")
            if token not in mapped:
                mapped.append(token)
        if decls:
            lines.append(f"{TOKEN_SCOPE} {{\n  " + ";\n  ".join(decls) + ";\n}")
        # 主题变量 → 正文元素（放在主题自己的规则前面，主题明写的规则优先）
        by_sel: dict[str, list[str]] = {}
        for rest, prop, var, value in ELEMENT_VARS:
            if f"--tt-{var}" in defined or (variant == "base" and f"--tt-{var}" in visible):
                by_sel.setdefault(rest, []).append(f"{prop}: {value or f'var(--tt-{var})'}")
                if var not in mapped:
                    mapped.append(var)
        for rest, ds in by_sel.items():
            lines.append(f"{SCOPE}{rest} {{ " + "; ".join(ds) + "; }")
        if p["root"]:
            lines.append(f"{SCOPE} {{ " + "; ".join(p["root"]) + "; }")
        for rests, ds in p["rules"]:
            lines.append(", ".join(f"{SCOPE}{r}" for r in rests) + " { " + "; ".join(ds) + "; }")
        # 编辑器里代码块不是 <pre><code>：主题给 code 加的边框、底色、内边距别在代码块里再套一层
        if any(" code" in rests for rests, _ in p["rules"]):
            lines.append(f"{SCOPE} pre > code {{ border: 0; background: none; padding: 0; box-shadow: none; "
                         "border-radius: 0; }")
        body = "\n".join(lines)
        if variant == "base":
            blocks.append(body)
        else:
            blocks.append(f"{MEDIA[variant]} {{\n{body}\n}}")
    return "\n".join(blocks) + "\n", mapped


def _verify(text: str) -> None:
    """再解析一遍产物：每条规则都得落在正文的根或 token 的根上，否则直接拒绝（兜底，正常不会发生）。"""
    def check(nodes, depth=0):
        for n in nodes:
            if n.type == "qualified-rule":
                for part in _split_commas(n.prelude):
                    sel = " ".join(tinycss2.serialize(part).split())
                    if not (sel.startswith(SCOPE) or sel in TOKEN_ROOTS):
                        raise ThemeError("生成的样式没通过检查，已放弃导入")
            elif n.type == "at-rule":
                if depth or n.lower_at_keyword != "media":
                    raise ThemeError("生成的样式没通过检查，已放弃导入")
                check(tinycss2.parse_rule_list(n.content, skip_comments=True, skip_whitespace=True), 1)
            elif n.type == "error":
                raise ThemeError("生成的样式没通过检查，已放弃导入")
    check(tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True))


def rep_dict(rep: _Report, source: str, has_dark: bool, mapped: list[str]) -> dict:
    return {
        "source": source,
        "kept": rep.kept,
        "dropped": sum(rep.dropped.values()),
        "dropped_by": rep.dropped,
        "decl_dropped": sum(rep.decl_dropped.values()),
        "decl_dropped_by": rep.decl_dropped,
        "details": rep.details,
        "has_dark": has_dark,
        "mapped": mapped,
    }


def summary_text(report: dict) -> str:
    """「保留了 N 条规则，去掉了 M 条：外链资源 x、影响页面控件 y……」"""
    def by(d):
        return "、".join(f"{REASONS.get(k, k)} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))
    s = f"保留了 {report['kept']} 条规则，去掉了 {report['dropped']} 条"
    if report["dropped"]:
        s += "：" + by(report["dropped_by"])
    if report.get("decl_dropped"):
        s += f"；保留的规则里另外去掉了 {report['decl_dropped']} 个属性（{by(report['decl_dropped_by'])}）"
    return s + "。"


# ---------------------------------------------------------------- 存储

_lock = threading.Lock()


def themes_dir() -> str:
    env = os.environ.get("SPARK_THEMES_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.dirname(core_settings.settings_path()), "themes")


def valid_id(theme_id: str) -> bool:
    return isinstance(theme_id, str) and bool(ID_RE.fullmatch(theme_id))


def css_path(theme_id: str) -> str | None:
    """主题文件的路径；id 不合规或者文件不在就是 None。"""
    if not valid_id(theme_id):
        return None
    path = os.path.join(themes_dir(), theme_id + ".css")
    return path if os.path.isfile(path) else None


def _index_path() -> str:
    return os.path.join(themes_dir(), "index.json")


def _read_index() -> list[dict]:
    try:
        with open(_index_path(), encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("themes") if isinstance(data, dict) else None
        return [t for t in items or [] if isinstance(t, dict) and valid_id(t.get("id", ""))]
    except (OSError, ValueError):
        return []


def list_themes() -> list[dict]:
    """导入过的主题（新的在前）；文件已经不在的不列。"""
    return [t for t in _read_index() if css_path(t["id"])]


def clean_name(name: str, filename: str = "") -> str:
    raw = (name or "").strip() or re.sub(r"\.css$", "", os.path.basename(filename or ""), flags=re.I)
    raw = re.sub(r"[\x00-\x1f<>\\]", "", raw).strip()
    return raw[:MAX_NAME_CHARS] or "导入的主题"


def import_theme(css: str, *, name: str = "", filename: str = "") -> dict:
    """清洗 + 存盘，返回这条主题的记录（带报告）。"""
    name = clean_name(name, filename)
    text, report = sanitize(css, name=name)
    with _lock:
        existing = list_themes()
        if len(existing) >= MAX_THEMES:
            raise ThemeError(f"最多存 {MAX_THEMES} 个主题，先删掉一些再导入")
        os.makedirs(themes_dir(), exist_ok=True)
        theme_id = "t" + secrets.token_hex(5)
        while css_path(theme_id):
            theme_id = "t" + secrets.token_hex(5)
        atomic.write_text(os.path.join(themes_dir(), theme_id + ".css"), text)
        entry = {"id": theme_id, "name": name, "source": report["source"],
                 "imported_at": time.strftime("%Y-%m-%d %H:%M"), **report}
        atomic.write_json(_index_path(), {"version": 1, "themes": [entry] + existing}, indent=1)
    return entry


def delete_theme(theme_id: str) -> bool:
    if not valid_id(theme_id):
        return False
    with _lock:
        items = _read_index()
        path = os.path.join(themes_dir(), theme_id + ".css")
        found = os.path.isfile(path) or any(t["id"] == theme_id for t in items)
        if not found:
            return False
        if os.path.isfile(path):
            os.remove(path)
        atomic.write_json(_index_path(), {"version": 1, "themes": [t for t in items if t["id"] != theme_id]},
                          indent=1)
    return True
