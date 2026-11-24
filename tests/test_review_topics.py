"""复盘页的「主题」：页面和静态脚本能取到；标签词云的排布不重叠、位置确定（用 node 跑 cloud.js，没有 node 就跳过）；
标签文章列表带来源。样例库的副本当笔记库，不碰真实笔记库。"""

import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FIXTURE = os.path.join(ROOT, "apps", "mobile-ios", "dev", "fixture-vault")
STATIC = os.path.join(ROOT, "apps", "review", "static")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE, v)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    return str(v)


def test_复盘页有主题入口和脚本(lib):
    from apps.review import server
    client = server.app.test_client()
    page = client.get("/").get_data(as_text=True)
    assert 'id="tab-topics-btn"' in page and ">主题</button>" in page
    for id_ in ("cloud", "tagFilter", "tagList", "items", "mergeOpen", "kindOpen", "aliasList"):
        assert f'id="{id_}"' in page, id_
    assert "合并只记在对照表里，不改原文，可以撤销。" in page
    for name in ("cloud.js", "topics.js", "app.js"):
        assert f'src="static/{name}"' in page
        assert client.get(f"/static/{name}").status_code == 200, name


def test_标签下的文章带来源(lib):
    from apps.reader import server
    client = server.app.test_client()
    got = client.get("/api/tags/items?tag=DIY").get_json()["items"]
    assert got and all("source" in it for it in got)
    assert any(it["source"] == "Quiet Workshop" for it in got)
    clip = next(it for it in client.get("/api/tags/items?tag=社区").get_json()["items"] if it["type"] == "clip")
    assert isinstance(clip["source"], str)


_NODE = shutil.which("node")


def _layout(words, width):
    script = ("const {layout}=require(process.argv[1]);const [w,W]=JSON.parse(process.argv[2]);"
              "const m=(t,s,wt)=>[...t].reduce((a,c)=>a+(c.charCodeAt(0)>255?s:s*.55),0);"
              "console.log(JSON.stringify(layout(w,W,m)));")
    out = subprocess.run([_NODE, "-e", script, os.path.join(STATIC, "cloud.js"), json.dumps([words, width])],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(not _NODE, reason="没有 node")
@pytest.mark.parametrize("width", [320, 700, 940])
def test_词云排布不重叠且位置确定(width):
    words = [{"text": ("具身智能与机器人操作基础模型" if i == 3 else f"标签{i}" * (1 + i % 3)),
              "size": 34 - i * 0.33, "weight": 500 if i < 8 else 400} for i in range(63)]
    a = _layout(words, width)
    assert a == _layout(words, width)                       # 同样的数据位置相同
    assert len(a["items"]) == 63
    for it in a["items"]:                                   # 都在容器里
        assert 0 <= it["left"] and it["left"] + it["w"] <= width and 0 <= it["top"] and it["top"] + it["h"] <= a["height"]
    for i, p in enumerate(a["items"]):                      # 两两不重叠
        for q in a["items"][i + 1:]:
            assert p["left"] >= q["left"] + q["w"] or q["left"] >= p["left"] + p["w"] \
                or p["top"] >= q["top"] + q["h"] or q["top"] >= p["top"] + p["h"], (p["text"], q["text"])
