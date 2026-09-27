"""阅读页导入 Typora / Obsidian 主题：清洗、映射、存储、接口。"""

import os
import re
import sys
import urllib.parse

import pytest
import tinycss2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark
from apps.reader import themes

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "themes")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = themes.SCOPE


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _selectors(css):
    """产物里所有规则的选择器（逐个，含 @media 里面的）。"""
    out = []

    def walk(nodes):
        for n in nodes:
            if n.type == "qualified-rule":
                for part in themes._split_commas(n.prelude):
                    out.append(" ".join(tinycss2.serialize(part).split()))
            elif n.type == "at-rule":
                walk(tinycss2.parse_rule_list(n.content, skip_comments=True, skip_whitespace=True))
    walk(tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True))
    return out


def _assert_scoped(css):
    for sel in _selectors(css):
        assert sel.startswith(S) or sel in themes.TOKEN_ROOTS, sel


# ---------------------------------------------------------------- 清洗

def test_留下能映射的规则_去掉外链_定位_页面控件():
    css = """
    @import url("https://evil.example/x.css");
    @font-face { font-family: X; src: url(https://evil.example/x.woff2); }
    #write h1 { color: #123456; font-size: 2em; position: fixed; z-index: 99; }
    #write p { background: url(https://evil.example/t.png); margin: 0 0 1em; }
    #write blockquote::before { content: "“"; color: red; }
    body { margin: 0; overflow: hidden; }
    header, .bar, .prefs, .selbar, .toast, nav { display: none; }
    #write .toc { position: sticky; }
    .markdown-rendered a { color: #3366cc; text-decoration: underline; }
    p { behavior: url(x.htc); -moz-binding: url(x.xml#y); color: expression(alert(1)); }
    * { box-sizing: border-box; }
    """
    out, rep = themes.sanitize(css, name="t")
    _assert_scoped(out)
    assert f"{S} h1 {{ color: #123456; font-size: 2em; }}" in out
    assert f"{S} p {{ margin: 0 0 1em; }}" in out
    assert f"{S} a {{ color: #3366cc; text-decoration: underline; }}" in out
    for bad in ("url(", "@import", "@font-face", "position", "z-index", "fixed", "sticky", "content:",
                "behavior", "binding", "expression", "evil.example", "display: none", "box-sizing"):
        assert bad not in out.split("*/", 1)[1], bad
    by = rep["dropped_by"]
    assert by["external"] >= 2 and by["ui"] >= 2 and by["pseudo"] == 1
    assert rep["decl_dropped_by"]["layout"] >= 2 and rep["decl_dropped_by"]["external"] >= 1
    summary = themes.summary_text(rep)
    assert summary.startswith(f"保留了 {rep['kept']} 条规则，去掉了 {rep['dropped']} 条：")
    assert "外链资源" in summary and "影响页面控件" in summary
    whats = [d["what"] for d in rep["details"]]
    assert any("@import" in w for w in whats) and any(".bar" in w for w in whats)


@pytest.mark.parametrize("sel", [
    "html", "body", "header", "nav", ".bar", ".prefs", ".selbar", ".toast", "#aa", ".toc-side",
    "main", "button", "#write + p", ".workspace", ".nav-file-title", "#typora-sidebar", "a[href]",
    "body > .app-container", ".markdown-source-view .cm-line", "h1::after", "::selection",
])
def test_打到页面控件和编辑器界面的选择器整条去掉(sel):
    with pytest.raises(themes.ThemeError):
        # 只有这一条规则，而且是普通属性：去掉后什么都不剩
        themes.sanitize(f"{sel} {{ padding: 3px; margin: 1px; }}")


def test_选择器映射():
    def m(sel, forced="base"):
        toks = tinycss2.parse_component_value_list(sel)
        return themes.map_selector(toks, forced)
    assert m("#write") == ("base", "root", "")
    assert m("#write > h1:first-child") == ("base", "rule", " > h1:first-child")
    assert m(".markdown-preview-view .markdown-preview-section h2") == ("base", "rule", " h2")
    assert m("body.theme-dark .markdown-rendered pre code") == ("dark", "rule", " pre code")
    assert m(".theme-light a.external-link:hover") == ("light", "rule", " a:hover")
    assert m(".md-fences") == ("base", "rule", " pre")
    assert m("table tr:nth-child(2n)") == ("base", "rule", " table tr:nth-child(2n)")
    assert m("li::marker") == ("base", "rule", " li::marker")
    assert m(":root") == ("base", "vars", "")
    assert m(".theme-dark") == ("dark", "vars", "")
    assert m("h1", "dark") == ("dark", "rule", " h1")
    with pytest.raises(themes._Drop):
        m(".theme-light h1", "dark")


def test_变量改名_不会撞上阅读页自己的token():
    out, _ = themes.sanitize(":root { --paper: red; --accent: blue; --x: 1px } #write { padding: var(--x) }")
    assert "--tt-paper: red" in out and "--tt-accent: blue" in out
    assert "padding: var(--tt-x)" in out
    assert not re.search(r"(?<!-tt)--paper: red", out)


def test_变量值里的url也去掉():
    out, rep = themes.sanitize(":root { --bg: url(https://x/y.png); --c: #fff } #write { color: var(--c) }")
    assert "url(" not in out and "--tt-c: #fff" in out
    assert rep["decl_dropped_by"].get("external") == 1


def test_Typora_github风格():
    out, rep = themes.sanitize(_fixture("typora-github-like.css"), name="GitHub")
    _assert_scoped(out)
    assert rep["source"] == "typora" and not rep["has_dark"]
    # 顶层的元素规则挂到正文下面；.md-fences 当成代码块
    assert f"{S} a {{ color: #4183C4; }}" in out
    assert f"{S} pre, {S} code {{" in out
    assert f"{S} pre > code {{ border: 0;" in out, "<pre><code> 里别再套一层 code 的边框和底色"
    # #write 上的边距留下，max-width 去掉；body 的字体并到正文根上，颜色进 token
    root = re.search(re.escape(S) + r" \{ ([^}]*) \}", out).group(1)
    assert "padding: 30px" in root and "Open Sans" in root and "max-width" not in root
    assert "--ink: rgb(51, 51, 51)" in out
    assert "font-size: 16px" not in out, "html 的根字号不能搬到正文上"
    for gone in ("#typora-sidebar", "megamenu", "::before", "@media print", ".toast"):
        assert gone not in out.split("*/", 1)[1]
    assert rep["dropped_by"]["editor"] >= 3 and rep["dropped_by"]["external"] == 2


def test_Typora_night风格_变量接到阅读页token():
    out, rep = themes.sanitize(_fixture("typora-night-like.css"), name="Night")
    _assert_scoped(out)
    assert "--paper: var(--tt-bg-color)" in out and "--ink: var(--tt-text-color)" in out
    assert "--paper-2: var(--tt-side-bar-bg-color)" in out
    assert f"{S} {{ background: var(--tt-bg-color); color: var(--tt-text-color); }}" in out
    assert "url(" not in out and not rep["has_dark"]
    # 换了底色、没给链接和次要文字颜色：从文字色和底色调出来，顶栏和目录在深底上还看得清
    assert "--accent: color-mix(in srgb, var(--ink) 88%, var(--paper))" in out
    assert "--ink-2: color-mix(" in out and "--hl: color-mix(" in out
    assert "--paper-2: color-mix(" not in out, "主题给了的就不用调"


def test_只改文字色不改底色时不动别的token():
    out, _ = themes.sanitize("body { color: #333 } p { margin: 0 }")
    assert "--ink: #333" in out and "color-mix" not in out


def test_Obsidian_Minimal风格_深浅两套变量():
    out, rep = themes.sanitize(_fixture("obsidian-minimal-like.css"), name="Minimal")
    _assert_scoped(out)
    assert rep["source"] == "obsidian" and rep["has_dark"]
    light = out.split(themes.MEDIA["light"], 1)[1].split(themes.MEDIA["dark"], 1)[0]
    dark = out.split(themes.MEDIA["dark"], 1)[1]
    assert "--tt-background-primary: #fcfcfb" in light and "--tt-background-primary: #1e1e20" in dark
    for block in (light, dark):
        assert "--paper: var(--tt-background-primary)" in block
        assert "--ink: var(--tt-text-normal)" in block
        assert "--accent: var(--tt-text-accent)" in block
        assert f"{S} h1 {{ color: var(--tt-h1-color); }}" in block
    assert f"{S} code {{ color: #f0a0a0; }}" in dark, ".theme-dark 前缀的规则进深色版本"
    assert f"{S} mark {{ background: #5a4a10; }}" in dark, "@media (prefers-color-scheme: dark) 也进深色版本"
    # 字体变量接到正文上，退路是用户自己选的字体
    assert "font-family: var(--tt-font-text-theme), var(--body-font)" in out
    assert f"{S} h1 {{ font-size: var(--tt-h1-size); font-weight: var(--tt-h1-weight); }}" in out
    for gone in ("HyperMD", "view-header", "nav-file", "cm-line", "scrollbar", "transform", "status-bar", "url("):
        assert gone not in out.split("*/", 1)[1], gone
    assert "hsl(var(--tt-accent-h), 70%, 45%)" in light
    assert "padding-right: 12px" not in out, "外链图标去掉了，给它留的内边距也去掉"


def test_只有深色版本的规则也算有深色():
    out, rep = themes.sanitize("@media (prefers-color-scheme: dark) { #write { color: #eee } } p { margin: 0 }")
    assert rep["has_dark"]
    assert out.count(themes.MEDIA["dark"]) == 1


def test_不支持的媒体查询整块去掉():
    out, rep = themes.sanitize("@media (max-width: 600px) { p { margin: 0 } } @media print { p { color: #000 } }"
                               "@keyframes x { from { color: red } } p { color: #111 }")
    assert "@media" not in out.split("*/", 1)[1] and "keyframes" not in out
    assert rep["dropped_by"]["atrule"] == 3


@pytest.mark.parametrize("css,msg", [
    ("", "空"),
    ("   \n  ", "空"),
    ("a" * (themes.MAX_CSS_BYTES + 1), "太大"),
    ("p { color: red }\x00\x01", "二进制"),
    ("}}}{{{ ;;; @@@ ", "没找到"),
    ("#typora-sidebar { color: red } .workspace { color: blue }", "没找到"),
])
def test_空的_太大_坏的CSS_给清楚的错误(css, msg):
    with pytest.raises(themes.ThemeError) as e:
        themes.sanitize(css)
    assert msg in str(e.value)


def test_格式乱的CSS不崩_能用的照样留下():
    css = "p { color: red; ; : ; background: } h1 { color: #111 !important } }} h2 {{ x } #write > > p { color: blue }"
    out, rep = themes.sanitize(css)
    _assert_scoped(out)
    assert f"{S} h1 {{ color: #111 !important; }}" in out


def test_产物再过一遍检查_选择器逃不出正文():
    # 花招：注释、转义、奇怪的空白都不能让规则落到正文外面
    css = r"""#write h1/**/, .bar { color: red } #write\ h1 { color: red } #write h1,body{color:#111}
    #write h1 { color: red } } body { color: #222 } .x { color: red"""
    out, _ = themes.sanitize(css)
    _assert_scoped(out)


# ---------------------------------------------------------------- 存储 / 接口

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "vault"))
    (tmp_path / "vault" / "Spark" / "Show").mkdir(parents=True)
    (tmp_path / "vault" / "Spark" / "Show" / "a.md").write_text("# 标题\n\n正文。\n", encoding="utf-8")
    monkeypatch.delenv("SPARK_THEMES_DIR", raising=False)
    from werkzeug.test import Client
    return Client(spark.application)


def test_主题目录跟着设置文件走_也能单独指定(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_THEMES_DIR", raising=False)
    settings_dir = os.path.dirname(os.environ["SPARK_SETTINGS_FILE"])
    assert themes.themes_dir() == os.path.join(settings_dir, "themes")
    assert not themes.themes_dir().startswith(os.path.expanduser("~/.spark"))
    monkeypatch.setenv("SPARK_THEMES_DIR", str(tmp_path / "t"))
    assert themes.themes_dir() == str(tmp_path / "t")


def test_导入_列出_取样式表_删除(client):
    r = client.post("/read/api/themes", json={"css": _fixture("obsidian-minimal-like.css"),
                                               "filename": "Minimal.css"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    t = d["theme"]
    assert themes.valid_id(t["id"]) and t["name"] == "Minimal" and t["source"] == "obsidian"
    assert t["kept"] > 0 and t["dropped"] > 0 and t["summary"].startswith("保留了")
    assert t["details"] and t["has_dark"]
    assert [x["id"] for x in d["themes"]] == [t["id"]]
    # 存到设置文件旁边的 themes/ 里：<id>.css + index.json
    tdir = themes.themes_dir()
    assert os.path.isfile(os.path.join(tdir, t["id"] + ".css"))
    assert os.path.isfile(os.path.join(tdir, "index.json"))

    lst = client.get("/read/api/themes").get_json()
    assert lst["themes"][0]["id"] == t["id"] and lst["max_bytes"] == themes.MAX_CSS_BYTES

    css = client.get(f"/read/themes/{t['id']}.css")
    assert css.status_code == 200
    assert css.headers["Content-Type"].startswith("text/css")
    assert css.headers["X-Content-Type-Options"] == "nosniff"
    assert "html.reader.theme-custom" in css.get_data(as_text=True)

    r = client.delete(f"/read/api/themes/{t['id']}")
    assert r.status_code == 200 and r.get_json()["themes"] == []
    assert client.get(f"/read/themes/{t['id']}.css").status_code == 404, "删掉后 404，页面退回内置配色"
    assert client.delete(f"/read/api/themes/{t['id']}").status_code == 404


def test_也能用POST删除(client):
    t = client.post("/read/api/themes", json={"css": "p { color: #111 }", "name": "x"}).get_json()["theme"]
    assert client.post(f"/read/api/themes/{t['id']}/delete").status_code == 200
    assert client.get("/read/api/themes").get_json()["themes"] == []


@pytest.mark.parametrize("bad", ["..%2F..%2Fsettings", "t123", "T0123456789", "t0123456789a", "t01234567zz",
                                 "index", "%2e%2e"])
def test_id不合规_不许路径穿越(client, bad):
    assert client.get(f"/read/themes/{bad}.css").status_code == 404
    assert client.delete(f"/read/api/themes/{bad}").status_code in (400, 404, 405)
    assert themes.css_path(urllib.parse.unquote(bad)) is None


def test_索引坏了或文件丢了也不出错(client):
    t = client.post("/read/api/themes", json={"css": "p { color: #111 }", "name": "x"}).get_json()["theme"]
    os.remove(os.path.join(themes.themes_dir(), t["id"] + ".css"))
    assert client.get("/read/api/themes").get_json()["themes"] == [], "文件没了就不列"
    with open(os.path.join(themes.themes_dir(), "index.json"), "w") as f:
        f.write("{坏的")
    assert client.get("/read/api/themes").get_json()["themes"] == []


def test_导入失败给清楚的错误_页面不崩(client):
    r = client.post("/read/api/themes", json={"css": "a" * (themes.MAX_CSS_BYTES + 10)})
    assert r.status_code == 400 and "最多 1 MB" in r.get_json()["error"]
    r = client.post("/read/api/themes", data="x" * (themes.MAX_CSS_BYTES * 3 + 1),
                    content_type="application/json")
    assert r.status_code == 413
    r = client.post("/read/api/themes", json={"nope": 1})
    assert r.status_code == 400 and r.get_json()["error"]
    r = client.post("/read/api/themes", json={"css": ".workspace { color: red }"})
    assert r.status_code == 400
    d = r.get_json()
    assert "没找到" in d["error"] and d["report"]["dropped_by"]["editor"] == 1
    assert client.get("/read/api/themes").get_json()["themes"] == []


def test_跨站POST被挡(client):
    r = client.post("/read/api/themes", json={"css": "p { color: #111 }"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.delete("/read/api/themes/t0123456789", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_阅读页CSP不变_主题样式表在head里由theme_js挂上(client):
    r = client.get(urllib.parse.quote("/read/f/Show/a.md"))
    page = r.get_data(as_text=True)
    csp = r.headers["Content-Security-Policy"]
    assert csp == ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    head = page.split("</head>")[0]
    # theme.js 在 head 里同步跑，SparkRead.apply 在首屏前加 theme-custom 和 <link>
    assert head.index("/static/common/read-prefs.js") < head.index("/read/static/theme.js")
    with open(os.path.join(ROOT, "static", "common", "read-prefs.js"), encoding="utf-8") as f:
        js = f.read()
    assert "classList.toggle('theme-custom'" in js
    assert "/read/themes/${id}.css" in js and "'blocking', 'render'" in js
    assert "addEventListener('error'" in js, "主题文件没了要退回内置配色"
    # 「Aa」里有显示当前主题、换回内置配色的地方
    assert 'id="custom-theme"' in page and 'id="ct-off"' in page
    css = client.get("/read/static/reader.css").get_data(as_text=True)
    assert ".prefs .row[hidden] { display: none; }" in css


def test_设置页有导入主题的界面(client):
    page = client.get("/settings").get_data(as_text=True)
    for id_ in ("ct-file", "ct-pick", "ct-paste", "ct-text", "ct-import", "ct-result", "ct-list", "ct-off"):
        assert f'id="{id_}"' in page, id_
    assert "没法照搬" in page
