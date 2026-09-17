"""合并后的单进程：落地页 + 两个 app 挂在各自前缀下。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spark


def _client():
    """打整个 WSGI 应用（含 DispatcherMiddleware），而不是只打 hub。"""
    from werkzeug.test import Client
    return Client(spark.application)


def test_落地页与接口():
    c = _client()
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    apps = c.get("/api/apps").get_json()
    assert {a["key"] for a in apps} == {"summit", "podcast", "notes"}
    assert [a["path"] for a in apps] == ["/summit/?mode=summit", "/summit/?mode=series", "/notes/"]


def test_两个_app_都挂在自己的前缀下():
    c = _client()
    for prefix in ("/summit", "/notes"):
        assert c.get(f"{prefix}/").status_code == 200, f"{prefix} 首页"
        assert c.get(f"{prefix}/api/env").status_code == 200, f"{prefix} 的 api"
        assert c.get(f"{prefix}/static/app.js").status_code == 200, f"{prefix} 的静态文件"


def test_不带斜杠会跳到带斜杠():
    """前端用的是相对路径，少了这一跳，api/env 会解析成 /api/env 打到落地页上。"""
    c = _client()
    for prefix in ("/summit", "/notes"):
        r = c.get(prefix)
        assert r.status_code in (301, 308), f"{prefix} 应当重定向"
        assert r.headers["Location"].endswith(f"{prefix}/")


def test_两个_app_的模块没有互相顶掉():
    """都有顶层 pipeline.py / server.py；改成包之前，谁先 import 谁赢。"""
    from apps.notes2insight import server as n
    from apps.summit2md import server as s
    assert s.pipeline is not n.pipeline
    assert s.app is not n.app


def test_前端不再有绝对路径():
    """挂到子路径下之后，任何以 / 开头的 api/static/deck 引用都会打偏。"""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for app in ("summit2md", "notes2insight"):
        for name in ("app.js", "index.html"):
            p = os.path.join(root, "apps", app, "static", name)
            text = open(p, encoding="utf-8").read()
            for m in re.finditer(r"""["'`](/(?:api|static|deck)/)""", text):
                bad.append(f"{app}/{name}: {m.group(1)}")
    assert not bad, "这些引用会打到落地页上：" + ", ".join(bad)


def test_悬停配色的_key_和接口返回的一致():
    """CSS 靠 data-key 区分蓝/绿。哪天把 key 改了而 CSS 没跟着改，配色会悄无声息地失效。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    css = open(os.path.join(root, "static", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "static", "app.js"), encoding="utf-8").read()
    assert "el.dataset.key" in js, "卡片必须带 data-key，否则 CSS 选择器匹配不上"
    for a in spark.APPS:
        assert f'a.card[data-key="{a["key"]}"]:hover' in css, f'{a["key"]} 没有对应的悬停配色'
