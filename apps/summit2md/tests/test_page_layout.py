"""页面分区：「用哪个模型」固定一处、任务区固定在最下面、草稿不存 API Key。

跟 test_glossary.py 一样没有 JS 运行时，检查的是源码文本里的结构和写法——这几条
对应的都是以前真出过的问题：模型面板在标签页之间被搬来搬去（找单集页上看不见），
任务列表没任务时整块消失，以及"切入口不丢草稿"不能顺手把 Key 写进浏览器存储。
"""

import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


def test_模型面板在工作区之前_任务区在之后():
    html = _read("index.html")
    model = html.index('id="backendField"')
    work = html.index('id="workArea"')
    link = html.index('id="linkModeBox"')
    results = html.index('id="discoverResults"')
    tasks = html.index('id="tasksSection"')
    assert model < work < link < results < tasks
    # 只有一个模型面板
    assert html.count('id="backendField"') == 1


def test_模型面板不再被挪来挪去():
    js = _read("app.js")
    assert "trackBackendHost" not in js
    assert "trackBackendHost" not in _read("index.html")
    assert not re.search(r"appendChild\(\s*backendPanel", js)


def test_模型面板带设置链接():
    html = _read("index.html")
    start = html.index('id="backendField"')
    end = html.index("</section>", start)
    assert 'href="/settings"' in html[start:end]


def test_任务区不整块藏起来():
    js = _read("app.js")
    assert '$("tasksSection").style.display' not in js
    assert 'id="tasksEmpty"' in _read("index.html")


def test_草稿不存_API_Key():
    js = _read("app.js")
    m = re.search(r"const DRAFT_FIELDS = \[(.*?)\];", js, re.S)
    assert m, "没找到 DRAFT_FIELDS"
    fields = re.findall(r'"(\w+)"', m.group(1))
    assert fields, "DRAFT_FIELDS 是空的"
    for key_field in ("apiKey", "openrouterApiKey", "thirdPartyApiKey"):
        assert key_field not in fields, f"{key_field} 不能写进 sessionStorage"
    # 草稿只进 sessionStorage（关标签页就没了），不进 localStorage
    save = js[js.index("function saveDraft()"):js.index("function restoreSubsDraft()")]
    assert "localStorage" not in save


def test_标签页是真的_tablist():
    html = _read("index.html")
    assert 'role="tablist"' in html
    for tab, panel in (("tabInbox", "inboxBox"), ("tabFind", "findBox"), ("tabSubs", "subsBox")):
        assert re.search(rf'id="{tab}"[^>]*role="tab"[^>]*aria-controls="{panel}"', html), tab
        assert re.search(rf'id="{panel}"[^>]*role="tabpanel"[^>]*aria-labelledby="{tab}"', html), panel
