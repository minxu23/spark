"""全文索引（core/search_index.py）：分块、各种查询、排序、增量更新、重建、并发，和 /read/api/search 的新旧对照。

索引文件一律建在 tmp_path 里（conftest 也把默认位置指到临时目录）。"""

import os
import sqlite3
import sys
import threading
import time
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import atomic
from core import search_index as si


def _w(root, rel, text, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def lib(tmp_path):
    """一个小笔记库 + 建好的索引。"""
    v = tmp_path / "vault"
    _w(v, "Spark/Show/notes/20260110 第一期.md",
       '---\n节目: "[[Show]]"\n话题: ["算力", "芯片"]\n---\n\n# 第一期讲算力\n\n> 这一期聊数据中心的供电。\n\n'
       "## 要点\n\n- 液冷散热越来越重要。\n")
    _w(v, "Spark/Show/speech/20260110_第一期.md",
       "# 第一期讲算力\n\n## 演讲稿\n\nThe Robot Gripper software is open source.\n\n> 机器人夹爪软件已经开源。\n\n"
       "Scaling laws still hold.\n\n> 扩展法则依然成立。\n")
    _w(v, "Spark/Show/transcripts/20260110_第一期.md",
       "# 第一期讲算力\n\n## 文字记录\n\n**[0:01](https://www.youtube.com/watch?v=abc&t=1s)**  \nwe talk about GPU clusters.\n")
    _w(v, "Spark/剪藏/20261101_复盘方法.md",
       '---\ntitle: "复盘方法"\ntags: ["方法"]\ntldr: "怎样做一次好的复盘"\n---\n\n# 复盘方法\n\n'
       "![图](attachments/abcdef0123.png)\n\n正文说的是[项目管理](https://example.com/pm)。\n")
    _w(v, "Spark/剪藏/20261102_别的.md", "# 别的文章\n\n这里也提到复盘，但标题里没有。\n")
    _w(v, "Spark/Show/.cache/x.md", "secret 算力")
    _w(v, "Spark/Show/Show.md", "# Show\n\n- 第一期\n")
    _w(v, "output/技术洞察报告_光通信.md", "# 光通信报告\n\n## 背景\n\nCPO 与 LPO 两条路线。Über alles.\n")
    _w(v, "output/复盘/复盘_20261120.md", "# 本周复盘\n\n读了三篇。\n")
    idx = si.SearchIndex(str(tmp_path / "idx" / "s.sqlite"), str(v))
    idx.refresh()
    si_stale = si.STALE_SECONDS
    si.STALE_SECONDS = 1e9          # 测试里不要后台自动刷新，由用例自己调 refresh
    yield v, idx
    si.STALE_SECONDS = si_stale
    idx.wait()
    idx.close()


def _paths(rows):
    return [r["path"] for r in rows]


# ---------------------------------------------------------------- 分块

def test_分块_按小节和段落_带标题路径和位置():
    body = "# 标题\n\n" + "\n\n".join(f"第{i}段。" + "字" * 200 for i in range(6)) + "\n\n## 第二节\n\n短段。\n\n### 小小节\n\n" + "长" * 2000
    chunks = si.chunk_body(body, offset=10)
    assert all(len(c["text"]) <= si.CHUNK_MAX for c in chunks)
    assert all(len(c["text"]) >= 100 for c in chunks)
    assert chunks[0]["heading"] == "标题" and chunks[0]["pos"] == 10
    assert chunks[0]["text"].startswith("标题\n第0段"), "小标题的字也在块里"
    assert any(c["heading"] == "标题 > 第二节 > 小小节" for c in chunks), "超长段落按句子 / 硬切开，带三级标题路径"
    # 位置指向文件里块开头那一行
    full = "x" * 10 + body
    for c in chunks[1:]:
        first = c["text"].split("\n")[0][:5]
        assert full[c["pos"]:c["pos"] + 200].find(first) >= 0


def test_分块_小节太短就合进下一块_零头并进上一块():
    body = "## 甲\n\n一句。\n\n## 乙\n\n" + "乙" * 500 + "\n\n## 丙\n\n尾巴。\n"
    chunks = si.chunk_body(body)
    assert len(chunks) == 1, chunks
    assert chunks[0]["heading"] == "甲" and "尾巴。" in chunks[0]["text"]


def test_解析_文件头图片链接地址不进正文_标题摘要标签另存():
    text = ('---\ntitle: "T"\ntags: ["机器人", "触觉"]\ntldr: "一句话摘要"\n---\n\n# 真标题\n\n'
            "![图](attachments/0123456789abcdef.png)\n\n看[这里](https://example.com/very/long)和 [[Show|节目]]。\n\n"
            "**加粗** 和 ==高亮==\n")
    d = si.parse_doc("Spark/剪藏/20261101_x.md", text)
    assert d["title"] == "真标题" and d["date"] == "2026-11-01" and d["kind"] == "剪藏"
    assert d["summary"] == "一句话摘要" and d["tags"] == "机器人、触觉"
    body = "\n".join(c["text"] for c in d["chunks"])
    assert "attachments" not in body and "example.com" not in body and "tldr" not in body
    assert "看这里和 节目。" in body and "加粗 和 高亮" in body


def test_种类按路径推():
    cases = {
        "Spark/剪藏/20261101_x.md": "剪藏",
        "Spark/剪藏/索引.md": "总览页",
        "Spark/论文/20261101_x.md": "论文",
        "Spark/论文/索引.md": "总览页",
        "Spark/论文/sub/x.md": "会议资料",
        "Spark/论文集/x.md": "会议资料",
        "Spark/笔记/20261101.md": "随手记",
        "output/复盘/a.md": "复盘",
        "output/a.md": "报告",
        "Spark/Show/Show.md": "总览页",
        "Spark/Show/notes/a.md": "单集小结",
        "Spark/Show/speech/a.md": "整理稿",
        "Spark/Show/transcripts/a.md": "文字记录",
        "Spark/Show/topics/a.md": "专题",
        "Spark/Show/前四个Topic的小结.md": "会议资料",
        "Spark/信息跟进/AI Insider/20261101_a.md": "信息文章",
        "Spark/信息跟进/简报/简报_1.md": "简报",
        "Spark/摘录.md": "总览页",
    }
    assert {k: si.kind_of(k) for k in cases} == cases


# ---------------------------------------------------------------- 查询

def test_中文三字以上_两字_一字_英文_大小写(lib):
    _v, idx = lib
    assert _paths(idx.search_docs("数据中心")) == ["Spark/Show/notes/20260110 第一期.md"]
    assert "Spark/Show/notes/20260110 第一期.md" in _paths(idx.search_docs("算力"))
    assert set(_paths(idx.search_docs("复盘"))) == {"Spark/剪藏/20261101_复盘方法.md", "Spark/剪藏/20261102_别的.md",
                                                  "output/复盘/复盘_20261120.md"}
    assert _paths(idx.search_docs("夹")) == ["Spark/Show/speech/20260110_第一期.md"], "一个字也能搜"
    assert _paths(idx.search_docs("gripper")) == _paths(idx.search_docs("GRIPPER")) == ["Spark/Show/speech/20260110_第一期.md"]
    assert _paths(idx.search_docs("gpu")) == ["Spark/Show/transcripts/20260110_第一期.md"], "两个字母，不分大小写"
    assert _paths(idx.search_docs("über")) == ["output/技术洞察报告_光通信.md"], "非 ASCII 字母也不分大小写"
    assert idx.search_docs("没有这个词") == []
    assert not any(".cache" in p for p in _paths(idx.search_docs("secret"))), "点开头的目录不进索引"


def test_多个词都要出现_可以不在同一块(lib):
    v, idx = lib
    assert _paths(idx.search_docs("gripper 夹爪")) == ["Spark/Show/speech/20260110_第一期.md"]
    assert idx.search_docs("gripper 光通信") == []
    # 两个词分在两节里（不同块）也算，排在同块命中的后面
    _w(v, "Spark/Show/notes/20260111 第二期.md",
       "# 第二期\n\n## 一\n\n" + "前面讲 gripper。" + "填" * 700 + "\n\n## 二\n\n" + "后面讲夹爪。" + "充" * 700)
    idx.refresh()
    rows = idx.search_docs("gripper 夹爪")
    assert _paths(rows) == ["Spark/Show/speech/20260110_第一期.md", "Spark/Show/notes/20260111 第二期.md"]
    assert rows[1]["tier"] == 2


def test_标题命中排在正文命中前面_同分新的在前(lib):
    _v, idx = lib
    rows = idx.search_docs("复盘")
    tiers = {r["path"]: r["tier"] for r in rows}
    assert tiers["Spark/剪藏/20261102_别的.md"] == 1
    assert _paths(rows)[-1] == "Spark/剪藏/20261102_别的.md", "标题里没有「复盘」的排最后"
    assert _paths(rows)[0] in ("Spark/剪藏/20261101_复盘方法.md", "output/复盘/复盘_20261120.md")


def test_对照稿的原文和译文都搜得到_片段标出命中(lib):
    _v, idx = lib
    for q in ("Robot Gripper software", "夹爪软件已经开源", "扩展法则"):
        rows = idx.search_docs(q)
        assert _paths(rows) == ["Spark/Show/speech/20260110_第一期.md"], q
    r = idx.search_docs("gripper")[0]
    a, b = r["hits"][0]
    assert r["snippet"][a:b].lower() == "gripper"


def test_标题_摘要_标签_文件头也搜得到(lib):
    _v, idx = lib
    assert "Spark/剪藏/20261101_复盘方法.md" in _paths(idx.search_docs("怎样做一次"))   # tldr
    assert "Spark/剪藏/20261101_复盘方法.md" in _paths(idx.search_docs("方法"))       # 标签 / 标题
    assert "Spark/Show/notes/20260110 第一期.md" in _paths(idx.search_docs("[[Show]]"))  # 文件头原文


def test_按种类筛选_和_search_chunks(lib):
    _v, idx = lib
    assert _paths(idx.search_docs("第一期讲算力", kinds=["整理稿"])) == ["Spark/Show/speech/20260110_第一期.md"]
    chunks = idx.search_chunks("扩展法则", 5)
    assert chunks and chunks[0]["path"] == "Spark/Show/speech/20260110_第一期.md"
    # 文件很短，「演讲稿」这节并进了从一级标题开始的那块，块的标题路径记开头那节
    assert chunks[0]["heading"] == "第一期讲算力" and "扩展法则" in chunks[0]["text"]
    assert {"path", "title", "kind", "heading", "text", "score", "ord", "pos"} <= set(chunks[0])
    anyrows = idx.search_chunks("扩展法则 光通信", 5, mode="any")
    assert {c["path"] for c in anyrows} == {"Spark/Show/speech/20260110_第一期.md", "output/技术洞察报告_光通信.md"}
    assert idx.search_chunks("扩展法则 光通信", 5) == []


def test_太常见的词不全枚举_结果和全枚举一样(lib, monkeypatch):
    _v, idx = lib
    want = {q: set(_paths(idx.search_docs(q))) for q in ("复盘", "算力", "gripper 夹爪", "期 算力", "第一期讲算力")}
    monkeypatch.setattr(si, "FREQ_MAX", 0)       # 所有词都当「太常见」
    monkeypatch.setattr(si, "EXPAND_MAX", 0)
    got = {q: set(_paths(idx.search_docs(q))) for q in want}
    assert got == want


# ---------------------------------------------------------------- 增量更新

def test_增量更新_新增_修改_删除_改名(lib):
    v, idx = lib
    s = idx.refresh()
    assert (s["added"], s["changed"], s["removed"]) == (0, 0, 0)
    _w(v, "Spark/剪藏/20261103_新.md", "# 新文章\n\n讲触觉传感器。\n")
    p = v / "Spark/剪藏/20261102_别的.md"
    p.write_text("# 别的文章\n\n改成讲液冷散热了，稍微长一点。\n", encoding="utf-8")
    (v / "output/复盘/复盘_20261120.md").unlink()
    os.rename(v / "output/技术洞察报告_光通信.md", v / "output/技术洞察报告_光通信2.md")
    s = idx.refresh()
    assert (s["added"], s["changed"], s["removed"]) == (2, 1, 2), s
    assert _paths(idx.search_docs("触觉传感器")) == ["Spark/剪藏/20261103_新.md"]
    assert "Spark/剪藏/20261102_别的.md" not in _paths(idx.search_docs("复盘"))
    assert set(_paths(idx.search_docs("液冷散热"))) == {"Spark/剪藏/20261102_别的.md", "Spark/Show/notes/20260110 第一期.md"}
    assert _paths(idx.search_docs("本周复盘")) == []
    assert _paths(idx.search_docs("CPO")) == ["output/技术洞察报告_光通信2.md"]
    with sqlite3.connect(idx.db_path) as c:
        assert c.execute("SELECT count(*) FROM chunks WHERE id >> 16 NOT IN (SELECT id FROM docs)").fetchone()[0] == 0


def test_刚保存的文件下一次搜索就有_不用整库刷新(lib):
    v, idx = lib
    calls = []
    idx.refresh_async = lambda: calls.append(1)   # 不让它起后台整库刷新
    idx._seen_dirty = si._DIRTY[0]
    atomic.write_text(str(v / "Spark/剪藏/20261104_刚存.md"), "# 刚存的\n\n讲先进封装。\n")
    assert _paths(idx.search_docs("先进封装")) == ["Spark/剪藏/20261104_刚存.md"]
    assert not calls
    si.mark_dirty()
    idx.search_docs("先进封装")
    assert calls == [1], "说不清改了什么时，搜索触发后台刷新"


# ---------------------------------------------------------------- 重建、并发、文件位置

def test_版本不对就整库重建_重建期间退回(lib, monkeypatch):
    v, idx = lib
    idx.close()
    monkeypatch.setattr(si, "SCHEMA_VERSION", si.SCHEMA_VERSION + 1)
    fresh = si.SearchIndex(idx.db_path, str(v))
    with pytest.raises(si.IndexUnavailable):
        fresh._reader()
    s = fresh.refresh()
    assert s["rebuilt"] and s["added"] == 8
    assert _paths(fresh.search_docs("数据中心")) == ["Spark/Show/notes/20260110 第一期.md"]
    fresh.close()


def test_索引文件坏了就删掉重建(lib):
    v, idx = lib
    idx.close()
    with open(idx.db_path, "r+b") as f:   # 把文件头和前面几页写成乱码
        f.write(b"garbage!" * 4096)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(idx.db_path + suffix):
            os.remove(idx.db_path + suffix)
    fresh = si.SearchIndex(idx.db_path, str(v))
    with pytest.raises(si.IndexUnavailable):
        fresh.search_docs("数据中心")
    fresh.wait()
    s = fresh.refresh()
    assert s["added"] == 8 or s["scanned"] == 8
    assert _paths(fresh.search_docs("数据中心")) == ["Spark/Show/notes/20260110 第一期.md"]
    fresh.close()


def test_刷新期间照样能搜_同一时间只有一个刷新(lib):
    v, idx = lib
    for i in range(5):
        _w(v, f"Spark/剪藏/2026111{i}_批量{i}.md", f"# 批量{i}\n\n讲数据中心第{i}篇。\n")
    entered, release = threading.Event(), threading.Event()

    def slow(rel):
        entered.set()
        release.wait(5)
    idx.before_file = slow
    assert idx.refresh_async()
    assert entered.wait(5)
    assert not idx.refresh_async(), "已经有一个在跑"
    t = time.monotonic()
    rows = idx.search_docs("数据中心")
    assert time.monotonic() - t < 2
    assert _paths(rows) == ["Spark/Show/notes/20260110 第一期.md"], "刷新还没提交：用旧的索引回答"
    release.set()
    idx.before_file = None
    idx.wait()
    assert len(idx.search_docs("数据中心")) == 6


def test_索引文件只建在给定的目录里(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    _w(v, "Spark/a.md", "# A\n\n算力\n")
    before = set(os.listdir(tmp_path))
    idx = si.SearchIndex(str(tmp_path / "only" / "x.sqlite"), str(v))
    idx.refresh()
    idx.close()
    assert set(os.listdir(tmp_path)) - before == {"only"}
    assert set(os.listdir(tmp_path / "only")) <= {"x.sqlite", "x.sqlite-wal", "x.sqlite-shm"}
    assert not any(n.endswith((".sqlite", "-wal", "-shm")) for n in os.listdir(v))
    # 默认位置跟设置文件一处（conftest 指到临时目录），环境变量可以改
    monkeypatch.delenv(si.ENV_PATH, raising=False)
    monkeypatch.setenv("SPARK_SETTINGS_FILE", str(tmp_path / "home" / "settings.json"))
    assert si.default_path() == str(tmp_path / "home" / si.DEFAULT_NAME)
    monkeypatch.setenv(si.ENV_PATH, str(tmp_path / "elsewhere.sqlite"))
    assert si.default_path() == str(tmp_path / "elsewhere.sqlite")
    assert not si.default_path().startswith(os.path.expanduser("~/.spark"))


# ---------------------------------------------------------------- /read/api/search：新旧对照

@pytest.fixture
def reader_lib(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv(si.ENV_PATH, str(tmp_path / "idx" / "search.sqlite"))
    monkeypatch.setattr(si, "STALE_SECONDS", 1e9)
    v = tmp_path / "vault"
    _w(v, "Spark/Show/notes/20260110 第一期.md", '---\n节目: "[[Show]]"\n---\n\n# 第一期\n\n> 一句话：讲 Hello World。\n')
    _w(v, "Spark/Show/speech/20260110_第一期.md", "# 第一期\n\n## 演讲稿\n\nHello world.\n\n> 你好，世界。\n")
    _w(v, "Spark/剪藏/20261101_hello.md", "# 另一篇\n\nsay hello to the world\n")
    _w(v, "Spark/剪藏/20261102_世界地图.md", "# 世界地图\n\n地图。\n")
    _w(v, "output/报告_世界.md", "# 报告\n\n世界很大。HELLO WORLD!\n")
    from apps.reader import server as reader
    yield v, reader


def _api(q):
    import spark
    from werkzeug.test import Client
    return Client(spark.application).get("/read/api/search?q=" + urllib.parse.quote(q)).get_json()["items"]


def test_接口返回结构和旧实现一致_命中不少于旧实现(reader_lib):
    v, reader = reader_lib
    queries = ["hello world", "世界", "Hello", "地图", "界", "第一期", "world"]
    with reader.app.test_request_context("/api/search", base_url="http://localhost/read"):
        old = {q: reader._scan_search(q) for q in queries}
    idx = si.get_index()
    with pytest.raises(si.IndexUnavailable):
        idx._reader()
    assert _api("世界") == old["世界"], "索引没建好：退回老办法，结果一模一样"
    idx.wait()
    idx.refresh()
    for q in queries:
        new = _api(q)
        assert {i["id"] for i in old[q]} <= {i["id"] for i in new}, q
        for item in new:
            assert set(item) == set(old[q][0]), q
            assert isinstance(item["snippet"], str) and isinstance(item["files"], list)
        same = {i["id"]: i for i in old[q]}
        for item in new:
            if item["id"] in same:
                o = same[item["id"]]
                assert {k: item[k] for k in item if k != "snippet"} == {k: o[k] for k in o if k != "snippet"}, q
    assert {i["id"] for i in _api("世界")[:2]} == {"剪藏/20261102_世界地图.md", "@r/报告_世界.md"}, \
        "文件名命中排前面，同旧实现"
    # 旧实现把整句当一个词；新实现每个词都要有：多出来的是词分开出现的
    assert {i["id"] for i in _api("hello world")} == {
        "Show/notes/20260110 第一期.md", "Show/speech/20260110_第一期.md", "剪藏/20261101_hello.md", "@r/报告_世界.md"}
    assert "剪藏/20261101_hello.md" not in {i["id"] for i in old["hello world"]}


def test_接口条数上限照旧(reader_lib, monkeypatch):
    v, reader = reader_lib
    for i in range(reader.SEARCH_LIMIT + 5):
        _w(v, f"Spark/剪藏/202611{i:02d}_x{i}.md", f"# 第{i}篇\n\n共同的词语。\n")
    idx = si.get_index()
    idx.refresh()
    assert len(_api("共同的词语")) == reader.SEARCH_LIMIT
