"""/read：在网页里读 Spark 产物。"""

import json
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark
from apps.reader import server as reader


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    show = tmp_path / "Spark" / "Show"
    for d in ("notes", "speech", "transcripts", ".cache"):
        (show / d).mkdir(parents=True)
    (show / "Show.md").write_text("# Show\n\n> 共 1 期\n\n- [第一期](<notes/20260110 第一期.md>)\n", encoding="utf-8")
    (show / "notes" / "20260110 第一期.md").write_text(
        '---\n节目: "[[Show]]"\n话题: ["A", "B"]\n链接: "https://example.com/v"\n---\n\n'
        "# 第一期\n\n> 一句话\n\n- [整理稿](<../speech/20260110_第一期.md>)\n\n<script>alert(1)</script>\n",
        encoding="utf-8")
    (show / "speech" / "20260110_第一期.md").write_text(
        "# 第一期\n\n## 演讲稿\n\nHello world.\n\n> 你好，世界。\n\nSecond.\n\n> 第二段。\n", encoding="utf-8")
    (show / ".manifest.json").write_text("{}", encoding="utf-8")
    (show / ".cache" / "x.md").write_text("secret", encoding="utf-8")
    return tmp_path


def _get(c, path):
    return c.get(urllib.parse.quote(path, safe="/?=&"))


def _client():
    from werkzeug.test import Client
    return Client(spark.application)


def test_首页列出节目_文件夹页排出主页(vault):
    c = _client()
    r = _get(c, "/read/")
    assert r.status_code == 200 and "Show" in r.get_data(as_text=True)
    assert _get(c, "/read/f/Show").status_code in (301, 302, 308), "文件夹不带斜杠要跳转，相对链接才解析得对"
    page = _get(c, "/read/f/Show/").get_data(as_text=True)
    assert "共 1 期" in page and "第一期" in page


def test_笔记_frontmatter_维基链接_外链_转义(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    assert '<dl class="meta">' in page
    assert 'href="/read/f/Show/"' in page, "[[Show]] 指向节目文件夹页"
    assert '<span class="chip">A</span>' in page
    assert 'target="_blank"' in page
    assert "<script>alert" not in page, "正文里的原始 HTML 要转义"


def test_整理稿排成原文译文对照(vault):
    page = _get(_client(), "/read/f/Show/speech/20260110_第一期.md").get_data(as_text=True)
    assert page.count('class="pair"') == 2
    assert '<main class="wide">' in page


def test_笔记里的引用块不当成译文(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    assert 'class="pair"' not in page


@pytest.mark.parametrize("path", [
    "/read/f/Show/.manifest.json", "/read/f/Show/.cache/x.md", "/read/f/../../etc/passwd",
])
def test_隐藏文件_越界路径都是_404(vault, path):
    assert _get(_client(), path).status_code == 404


def test_每页都有阅读设置面板_偏好在首屏前套上(vault):
    page = _get(_client(), "/read/f/Show/notes/20260110 第一期.md").get_data(as_text=True)
    head = page.split("</head>")[0]
    assert "/read/static/theme.js" in head, "theme.js 要在 head 里同步加载，否则会先闪一下默认配色"
    assert 'id="aa"' in page and 'id="prefs"' in page
    for theme in ("auto", "kami", "white", "sepia", "gray", "night"):
        assert f'data-theme="{theme}"' in page
    assert _get(_client(), "/read/static/theme.js").status_code == 200


def test_阅读偏好和设置页共用一份脚本_配色字体CSS(vault):
    c = _client()
    r = _get(c, "/read/f/Show/notes/20260110 第一期.md")
    page = r.get_data(as_text=True)
    head = page.split("</head>")[0]
    assert '<html lang="zh-CN" class="reader">' in page, "配色 token 只挂在阅读页的 <html class=reader> 上"
    # 共用脚本定义 SparkRead，theme.js 用它首屏前套上偏好，所以顺序不能反
    assert head.index("/static/common/read-prefs.js") < head.index("/read/static/theme.js")
    assert head.index("/static/common/read-prefs.css") < head.index("/read/static/reader.css")
    assert 'href="/settings#reading"' in page, "「Aa」里有去设置页选更多字体的入口"
    # 只有三个常用字体按钮，更多字体在设置页
    assert page.count('class="opt " data-font=') == 3
    # CSP 没放松：普通页面仍然不许内联脚本
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    for path in ("/static/common/read-prefs.js", "/static/common/read-prefs.css"):
        assert _get(c, path).status_code == 200, path


def test_字体选项在CSS里都有字体栈_都以通用字体收尾():
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "common", "read-prefs.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(root, "static", "common", "read-prefs.css"), encoding="utf-8") as f:
        css = f.read()
    keys = re.findall(r"\{ key: '([\w-]+)'", js)
    assert len(keys) >= 15 and keys[:3] == ["serif", "sans", "kai"]
    for k in keys + ["custom"]:
        m = re.search(r'\[data-font="%s"\] \{ --body-font: ([^}]+); \}' % re.escape(k), css)
        assert m, f"字体 {k} 在 read-prefs.css 里没有字体栈"
        stack = m.group(1)
        assert stack.endswith(("serif", "sans-serif", "monospace", "var(--serif)", "var(--sans)", "var(--kai)")), k
    for theme in ("kami", "white", "sepia", "gray", "night"):
        assert f'.read-scope[data-theme="{theme}"]' in css and f'html.reader[data-theme="{theme}"]' in css
    assert '.read-scope[data-theme="auto"]' in css



# ---------------------------------------------------------------- 高亮 / 摘录

NOTE = "Show/notes/20260110 第一期.md"


def _note_path(vault):
    return vault / "Spark" / "Show" / "notes" / "20260110 第一期.md"


def _write_note(vault, body):
    p = _note_path(vault)
    p.write_text('---\n节目: "[[Show]]"\n---\n\n# 第一期\n\n' + body, encoding="utf-8")
    return str(os.stat(p).st_mtime_ns)


def _post(c, url, **body):
    return c.post(url, json=body)


def test_高亮写回原文_页面渲染成_mark(vault):
    mtime = _write_note(vault, "Sacks 搬到了奥斯汀，**财富税**导致富人出走。\n")
    c = _client()
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=mtime, text="搬到了奥斯汀", before="Sacks ")
    assert r.status_code == 200, r.get_json()
    assert "==搬到了奥斯汀==" in _note_path(vault).read_text(encoding="utf-8")
    assert "<mark>搬到了奥斯汀</mark>" in r.get_json()["html"]
    # 跨加粗也能对上，整段连格式一起包住
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=r.get_json()["mtime"], text="财富税导致", before="")
    assert r.status_code == 200, r.get_json()
    assert "==**财富税**导致==" in _note_path(vault).read_text(encoding="utf-8")


def test_同样的话出现两次_按前文挑(vault):
    mtime = _write_note(vault, "甲说：看好。\n\n乙说：看好。\n")
    r = _post(_client(), "/read/api/highlight", path=NOTE, mtime=mtime, text="看好", before="乙说：")
    assert r.status_code == 200
    assert "乙说：==看好==" in _note_path(vault).read_text(encoding="utf-8")
    assert "甲说：看好" in _note_path(vault).read_text(encoding="utf-8")


def test_高亮对不上或跨段落时拒绝_文件不动(vault):
    mtime = _write_note(vault, "第一段。\n\n第二段 [链接](https://x.com) 后面。\n")
    before = _note_path(vault).read_text(encoding="utf-8")
    c = _client()
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=mtime, text="第一段。\n\n第二段")
    assert r.status_code == 400 and "跨了段落" in r.get_json()["error"]
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=mtime, text="链接 后面")
    assert r.status_code == 400
    assert _note_path(vault).read_text(encoding="utf-8") == before


def test_文件在别处改过_拒绝写(vault):
    _write_note(vault, "内容。\n")
    r = _post(_client(), "/read/api/highlight", path=NOTE, mtime="1", text="内容")
    assert r.status_code == 409 and "改过了" in r.get_json()["error"]


def test_取消高亮(vault):
    mtime = _write_note(vault, "一 ==重点== 二 ==重点== 三\n")
    r = _post(_client(), "/read/api/highlight", path=NOTE, mtime=mtime, text="重点", nth=1, remove=True)
    assert r.status_code == 200, r.get_json()
    assert "一 ==重点== 二 重点 三" in _note_path(vault).read_text(encoding="utf-8")


def test_摘录存进摘录本_新的在上面_同时高亮(vault):
    mtime = _write_note(vault, "第一句话。第二句话。\n")
    c = _client()
    r = _post(c, "/read/api/excerpt", path=NOTE, mtime=mtime, text="第一句话", thought="值得记")
    assert r.status_code == 200, r.get_json()
    r = _post(c, "/read/api/excerpt", path=NOTE, mtime=r.get_json()["mtime"], text="第二句话")
    assert r.status_code == 200, r.get_json()
    book = (vault / "Spark" / "摘录.md").read_text(encoding="utf-8")
    assert book.startswith("# 摘录")
    assert book.index("> 第二句话") < book.index("> 第一句话"), "新的在上面"
    assert "想法：值得记" in book
    assert "[[Spark/Show/notes/20260110 第一期|Show · 第一期]]" in book
    assert "==第一句话==。==第二句话==" in _note_path(vault).read_text(encoding="utf-8")
    # 摘录本里的出处链接在阅读页能点回原文；摘录本自己不能再摘录
    page = _get(c, "/read/f/摘录.md").get_data(as_text=True)
    assert 'href="/read/f/Show/notes/20260110%20%E7%AC%AC%E4%B8%80%E6%9C%9F.md"' in page
    assert 'data-excerptable="false"' in page


def test_摘录对不上原文时照样存_只是不高亮(vault):
    mtime = _write_note(vault, "看 [这里](https://x.com) 的说明。\n")
    r = _post(_client(), "/read/api/excerpt", path=NOTE, mtime=mtime, text="这里 的说明")
    d = r.get_json()
    assert r.status_code == 200 and "没加高亮" in d["note"]
    assert "> 这里 的说明" in (vault / "Spark" / "摘录.md").read_text(encoding="utf-8")


def test_标注接口不碰隐藏文件和库外路径(vault):
    c = _client()
    for p in (".cache/x.md", "../../etc/passwd", "Show/.manifest.json"):
        assert _post(c, "/read/api/highlight", path=p, text="x").status_code == 400


def _seed_episode(vault):
    """一期完整的：笔记 + 整理稿 + 文字记录，manifest 里对得上。"""
    show = vault / "Spark" / "Show"
    (show / ".manifest.json").write_text(json.dumps({"entries": {"e1": {
        "ok": True, "note_relative_path": "notes/20260110 第一期.md",
        "speech_relative_path": "speech/20260110_第一期.md",
        "relative_path": "transcripts/20260110_第一期.md"}}}), encoding="utf-8")
    (show / "transcripts" / "20260110_第一期.md").write_text("# 第一期\n\n完整的文字记录在这里。\n", encoding="utf-8")
    _write_note(vault, "本期要点。\n")
    return show


def _mt(p):
    return str(os.stat(p).st_mtime_ns)


def test_整理稿里的高亮汇总到这期笔记_取消时一起删(vault):
    show = _seed_episode(vault)
    speech = show / "speech" / "20260110_第一期.md"
    c = _client()
    r = _post(c, "/read/api/highlight", path="Show/speech/20260110_第一期.md", mtime=_mt(speech), text="Hello world")
    assert r.status_code == 200, r.get_json()
    note = _note_path(vault).read_text(encoding="utf-8")
    assert note.endswith("## 我的高亮\n\n- Hello world（[整理稿](<../speech/20260110_第一期.md>)）\n")
    tr = show / "transcripts" / "20260110_第一期.md"
    r = _post(c, "/read/api/highlight", path="Show/transcripts/20260110_第一期.md", mtime=_mt(tr), text="文字记录")
    assert r.status_code == 200, r.get_json()
    assert "- 文字记录（[文字记录](<../transcripts/20260110_第一期.md>)）" in _note_path(vault).read_text(encoding="utf-8")
    # 取消整理稿那条：只删它；两条都取消后标题也去掉
    r = _post(c, "/read/api/highlight", path="Show/speech/20260110_第一期.md", mtime=_mt(speech),
              text="Hello world", remove=True)
    assert r.status_code == 200, r.get_json()
    note = _note_path(vault).read_text(encoding="utf-8")
    assert "Hello world" not in note and "- 文字记录" in note
    r = _post(c, "/read/api/highlight", path="Show/transcripts/20260110_第一期.md", mtime=_mt(tr),
              text="文字记录", remove=True)
    assert r.status_code == 200
    note = _note_path(vault).read_text(encoding="utf-8")
    assert "我的高亮" not in note and note.endswith("本期要点。\n")


def test_笔记里自己的高亮也记进汇总_汇总里不能再高亮(vault):
    _seed_episode(vault)
    c = _client()
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=_mt(_note_path(vault)), text="本期要点")
    assert r.status_code == 200, r.get_json()
    note = _note_path(vault).read_text(encoding="utf-8")
    assert "==本期要点==。" in note and note.endswith("## 我的高亮\n\n- 本期要点\n")
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=_mt(_note_path(vault)), text="本期要点", in_summary=True)
    assert r.status_code == 400 and "高亮汇总" in r.get_json()["error"]


def test_不是某一期的文件只在原文高亮(vault):
    _seed_episode(vault)
    home = vault / "Spark" / "Show" / "Show.md"
    r = _post(_client(), "/read/api/highlight", path="Show/Show.md", mtime=_mt(home), text="共 1 期")
    assert r.status_code == 200, r.get_json()
    assert "我的高亮" not in _note_path(vault).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 笔记洞察的报告和演示

DECK_HTML = ('<!doctype html><html><head><title>d</title></head><body>'
             '<div id="top"><span id="pageNo">1 / 3</span></div><div id="stage"><div class="slide" id="slide"></div></div>'
             '<script type="application/json" id="deckdata">{}</script><script>/* deck */</script></body></html>')


def _seed_report(vault):
    out = vault / "output"
    out.mkdir()
    (out / "报告A.md").write_text("# 报告A\n\n## 执行摘要\n\n推理成本一年降了十倍。\n", encoding="utf-8")
    (out / "报告A.deck.html").write_text(DECK_HTML, encoding="utf-8")
    (out / "孤儿.deck.html").write_text(DECK_HTML, encoding="utf-8")
    return out


def test_报告目录出现在首页和列表里_带演示入口(vault):
    _seed_report(vault)
    c = _client()
    assert "笔记洞察报告" in _get(c, "/read/").get_data(as_text=True)
    page = _get(c, "/read/r/").get_data(as_text=True)
    assert 'href="/read/r/%E6%8A%A5%E5%91%8AA.md"' in page
    assert 'class="aside" href="/read/r/%E6%8A%A5%E5%91%8AA.deck.html">演示' in page
    page = _get(c, "/read/r/报告A.md").get_data(as_text=True)
    assert "推理成本一年降了十倍" in page and ">看演示</a>" in page
    assert 'data-path="@r/报告A.md"' in page


def test_报告里的高亮汇总在报告自己末尾(vault):
    out = _seed_report(vault)
    rep = out / "报告A.md"
    r = _post(_client(), "/read/api/highlight", path="@r/报告A.md", mtime=_mt(rep), text="降了十倍")
    assert r.status_code == 200, r.get_json()
    text = rep.read_text(encoding="utf-8")
    assert "一年==降了十倍==。" in text and text.endswith("## 我的高亮\n\n- 降了十倍\n")


def test_演示页注入标注脚本_只有这里放开内联脚本(vault):
    _seed_report(vault)
    c = _client()
    r = _get(c, "/read/r/报告A.deck.html")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "/read/static/deck-annotate.js" in page
    assert page.index("deck-annotate.js") < page.index("</body>")
    assert "'unsafe-inline'" in r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    r = _get(c, "/read/r/报告A.md")
    assert "'unsafe-inline'" not in r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    assert _get(c, "/read/r/孤儿.deck.html").status_code == 404, "没有同名报告的演示没地方存高亮"


def test_演示上的高亮记进报告_读回来_能取消(vault):
    out = _seed_report(vault)
    c = _client()
    r = _post(c, "/read/api/deck_highlight", deck="@r/报告A.deck.html", slide=3, text="推理  成本\n下降")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["items"] == [{"slide": 3, "text": "推理 成本 下降"}]
    rep = (out / "报告A.md").read_text(encoding="utf-8")
    assert "- 推理 成本 下降（[演示第 3 页](<报告A.deck.html#3>)）" in rep
    items = _get(c, "/read/api/deck_highlights?deck=@r/报告A.deck.html").get_json()["items"]
    assert items == [{"slide": 3, "text": "推理 成本 下降"}]
    # 报告页里取消同样文字的普通高亮，不会误删演示那条
    r = _post(c, "/read/api/deck_highlight", deck="@r/报告A.deck.html", slide=3, text="推理 成本 下降", remove=True)
    assert r.status_code == 200 and r.get_json()["items"] == []
    assert "我的高亮" not in (out / "报告A.md").read_text(encoding="utf-8")


def test_open_按绝对路径跳到阅读页(vault):
    out = _seed_report(vault)
    c = _client()
    r = c.get("/read/open", query_string={"path": str(out / "报告A.deck.html")})
    assert r.status_code in (301, 302) and r.headers["Location"].endswith("/read/r/%E6%8A%A5%E5%91%8AA.deck.html")
    r = c.get("/read/open", query_string={"path": str(_note_path(vault))})
    assert r.status_code in (301, 302) and "/read/f/Show/notes/" in r.headers["Location"]
    for bad in ("/etc/passwd", str(vault / "Spark" / "Show" / ".manifest.json")):
        assert c.get("/read/open", query_string={"path": bad}).status_code == 404


# ---------------------------------------------------------------- 层级和导航：元信息折叠、锚点、目录、单集列表

def test_标题锚点唯一_重名和特殊字符_每次一样():
    md = ("# 标题\n\n## 背景\n\n一。\n\n## 背景\n\n二。\n\n## A & B：“引号”？\n\n## prefs\n\n### 🗣️ \n\n"
          "## Why it *matters*\n\n> ## 为什么重要\n")
    _t, out = reader.render_markdown(md, "/nonexistent")
    ids = __import__("re").findall(r'<h[1-6] id="([^"]*)"', out)
    assert ids == ["标题", "背景", "背景-2", "a-b引号", "prefs-2", "section", "why-it-matters"]
    assert len(ids) == len(set(ids)), "锚点不能重复；和页面上 #prefs 这类已有的 id 也不能撞"
    assert "<h2>为什么重要</h2>" in out, "引用块里的标题（译文）不加锚点"
    assert reader.render_markdown(md, "/nonexistent")[1] == out, "同一篇每次渲染锚点一样"


def _note_with_sections(vault, n):
    body = "开头一段。\n\n" + "".join(f"## 第{i}节\n\n第{i}节的内容。\n\n" for i in range(1, n + 1))
    return _write_note(vault, body)


def test_标题够多才出目录_点目录就是锚点跳转(vault):
    _note_with_sections(vault, 2)
    page = _get(_client(), "/read/f/" + NOTE).get_data(as_text=True)
    assert "toc-side" not in page and "toc-top" not in page
    _note_with_sections(vault, 3)
    page = _get(_client(), "/read/f/" + NOTE).get_data(as_text=True)
    assert '<main class="has-toc">' in page
    assert page.count('href="#第2节"') == 2, "宽屏侧栏和窄屏折叠目录各一份"
    assert '<h2 id="第2节">第2节</h2>' in page
    assert '<details class="toc-top">' in page and '<nav class="toc-side"' in page


def test_文字记录里反复出现的发言人标题不进目录(vault):
    tr = vault / "Spark" / "Show" / "transcripts" / "20260110_第一期.md"
    tr.write_text("# 第一期\n\n## 文字记录\n\n" + "### 🗣️ 甲\n\n话。\n\n### 🗣️ 乙\n\n话。\n\n" * 5, encoding="utf-8")
    page = _get(_client(), "/read/f/Show/transcripts/20260110_第一期.md").get_data(as_text=True)
    assert "toc-side" not in page
    assert 'id="甲-5"' in page, "发言人标题照样有锚点"


def test_元信息默认折起_只露一行_内容还在(vault):
    speech = vault / "Spark" / "Show" / "speech" / "20260110_第一期.md"
    speech.write_text("# 第一期\n\n- 所属节目：Show\n- 链接：https://example.com/v\n- 时长：约 1:00:00\n"
                      "- 说明：本文由 AI 整理\n\n## 演讲稿\n\nHello world.\n\n> 你好，世界。\n", encoding="utf-8")
    page = _get(_client(), "/read/f/Show/speech/20260110_第一期.md").get_data(as_text=True)
    assert '<details class="docmeta"><summary>Show · 2026-01-10 · 约 1:00:00</summary>' in page
    assert "<li>说明：本文由 AI 整理</li>" in page, "元信息只是折起来，没删"
    assert '<details class="docmeta" open' not in page
    assert page.index("<h1") < page.index("docmeta") < page.index("演讲稿"), "标题在前，元信息紧跟其后"
    # 笔记：frontmatter 折进去，放在标题和一句话摘要后面
    page = _get(_client(), "/read/f/" + NOTE).get_data(as_text=True)
    assert '<details class="docmeta"><summary>Show · 2026-01-10</summary><dl class="meta">' in page
    assert page.index("一句话") < page.index("docmeta")


def test_正文里普通的键值列表不当元信息(vault):
    _write_note(vault, "## 要点\n\n- 背景：一\n- 方法：二\n")
    page = _get(_client(), "/read/f/" + NOTE).get_data(as_text=True)
    assert "<li>背景：一</li>" in page and 'class="meta-list"' not in page


def test_节目文件夹_单集列表在前_可读标题日期类型_不重复主页(vault):
    _seed_episode(vault)
    c = _client()
    page = _get(c, "/read/f/Show/").get_data(as_text=True)
    eps = page[page.index('id="episodes"'):page.index("</section>")]
    assert '<span class="date">2026-01-10</span><span class="name">第一期</span>' in eps
    for kind in ("笔记", "整理稿", "文字记录"):
        assert f">{kind}</a>" in eps
    assert eps.count('<li class="ep">') == 1, "同一期的三种文件并成一行"
    assert page.index('id="episodes"') < page.index("共 1 期"), "单集列表排在主页内容前面"
    files = page[page.index('class="files"'):]
    assert "Show.md" not in files and ">Show<" not in files, "主页已经排在页面上，文件列表里不再出现"
    assert "笔记 · notes/" in files
    # 子文件夹：显示可读标题、日期、类型，不是文件名
    (vault / "Spark" / "Show" / "notes" / "20260201 raw_file_name.md").write_text(
        "# 可读的标题\n\n内容\n", encoding="utf-8")
    page = _get(c, "/read/f/Show/notes/").get_data(as_text=True)
    assert "<h1>Show · 笔记</h1>" in page
    assert ('<span class="date">2026-02-01</span><span class="name">可读的标题</span>'
            '<span class="kind">笔记</span>') in page
    assert "raw_file_name" not in page.split("<main")[1].split("</main>")[0].replace("raw_file_name.md", "")


def test_报告列表显示标题和类型(vault):
    _seed_report(vault)
    page = _get(_client(), "/read/r/").get_data(as_text=True)
    assert '<span class="name">报告A</span><span class="kind">报告</span>' in page


def test_有目录的页面上高亮照常_返回的正文带锚点和折叠元信息(vault):
    mtime = _note_with_sections(vault, 3)
    c = _client()
    page = _get(c, "/read/f/" + NOTE).get_data(as_text=True)
    assert "toc-side" in page and f'data-mtime="{mtime}"' in page
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=mtime, text="第2节的内容", before="")
    assert r.status_code == 200, r.get_json()
    html = r.get_json()["html"]
    assert "<mark>第2节的内容</mark>" in html and '<h2 id="第2节">' in html and 'class="docmeta"' in html
    assert "==第2节的内容==" in _note_path(vault).read_text(encoding="utf-8")
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=r.get_json()["mtime"], text="第2节的内容", remove=True)
    assert r.status_code == 200, r.get_json()


def test_节目文件夹页上高亮主页_换回来的正文和页面排法一致(vault):
    show = _seed_episode(vault)
    home = show / "Show.md"
    home.write_text("# Show\n\n> 共 1 期\n\n## 节目总结\n\n一档好节目。\n\n## 全部单集（共 1 期）\n\n"
                    "- [第一期](<notes/20260110 第一期.md>)\n", encoding="utf-8")
    c = _client()
    page = _get(c, "/read/f/Show/").get_data(as_text=True)
    art = page[page.index("<article"):page.index("</article>")]
    assert 'data-view="home"' in art and "全部单集" not in art and "<h1" not in art
    r = _post(c, "/read/api/highlight", path="Show/Show.md", mtime=_mt(home), text="好节目", view="home")
    assert r.status_code == 200, r.get_json()
    html = r.get_json()["html"]
    assert "<mark>好节目</mark>" in html and "全部单集" not in html and "<h1" not in html
    # 直接打开主页文件时照原样排
    page = _get(c, "/read/f/Show/Show.md").get_data(as_text=True)
    assert "全部单集" in page
