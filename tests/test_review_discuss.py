"""复盘里的「讨论」（core/discuss.py）：检索词解析和退路、材料的选取规则、标签范围、一个都没找到时不调回答的模型、
多轮历史进提示词、出处和 sources 对得上、存成复盘（结构、列表、继续追问、非法输入），
以及手机提示词和存盘格式（apps/mobile-ios/Spark/DiscussPrompt.swift）和电脑逐字一致。
笔记库用临时目录，全文索引也放临时目录，模型是假的，不碰真实笔记库。"""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import discuss, review, search_index

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT_SRC = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "DiscussPrompt.swift")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _clip(v, name, title, body, tag="线", day="2026-03-01", source="新智元", tldr=None):
    fm = (f"---\ntitle: \"{title}\"\nsource: \"{source}\"\nclipped: {day}\ntags: {json.dumps([tag], ensure_ascii=False)}\n"
          f"tldr: \"{tldr if tldr is not None else title + '的一句话'}\"\n---\n\n# {title}\n\n## 正文\n\n{body}\n")
    _write(str(v / "Spark" / "剪藏" / f"{day.replace('-', '')}_{name}.md"), fm)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setenv(search_index.ENV_PATH, str(tmp_path / "idx" / "search.sqlite"))
    monkeypatch.setattr(search_index, "STALE_SECONDS", 1e9)
    calls = []
    replies = {"terms": "英伟达 算力", "answer": "英伟达的算力在涨 [1]。"}

    def fake_llm(prompt, max_tokens=8000):
        calls.append(prompt)
        return replies["terms"] if "变成 3 到 6 个检索词" in prompt else replies["answer"]

    monkeypatch.setattr(review, "_llm", fake_llm)
    yield v, calls, replies
    search_index.get_index(vault=str(v)).close()


def _index(v):
    search_index.get_index(vault=str(v)).refresh()


def _answers(calls):
    return [c for c in calls if "变成 3 到 6 个检索词" not in c]


# ---------------------------------------------------------------- 检索词

def test_检索词按空格和标点拆_去重_最多6个():
    assert discuss.parse_terms("英伟达 算力，GPU、gpu\n别的行", "问题") == ["英伟达", "算力", "GPU"]
    assert discuss.parse_terms("\n\n  a1 b2 c3 d4 e5 f6 g7 h8", "问题") == ["a1", "b2", "c3", "d4", "e5", "f6"]
    assert discuss.parse_terms("x " + "长" * 21 + " 有效词", "问题") == ["有效词"]   # 单字和超长的丢掉


def test_检索词解析失败时用问题本身拆():
    assert discuss.parse_terms("", "英伟达的算力，怎么样？") == ["英伟达的算力", "怎么样"]
    assert discuss.parse_terms("？ ，", "RSI 走到哪了") == ["RSI", "走到哪了"]
    # 超过 8 个字的一段每 4 个字切一个
    assert discuss.parse_terms("", "具身智能的数据飞轮是什么") == ["具身智能", "的数据飞", "轮是什么"]
    assert discuss.parse_terms("", "") == []


# ---------------------------------------------------------------- 材料

def test_同一篇最多2个片段_最多12篇_片段截断(lib):
    v, calls, _ = lib
    for n in range(1, 16):
        body = "\n\n".join(f"第 {k} 段讲英伟达。" + "字" * 300 for k in range(1, 6)) if n == 1 else f"第 {n} 篇讲英伟达。" + "字" * 900
        _clip(v, f"文{n:02d}", f"文章{n:02d}", body, day=f"2026-03-{n:02d}")
    _index(v)
    mats = discuss.gather(["英伟达"])
    assert len(mats) == discuss.MAX_DOCS == 12
    assert all(len(m["snippets"]) <= discuss.MAX_PER_DOC for m in mats)
    assert max(len(s) for m in mats for s in m["snippets"]) <= discuss.SNIPPET_CHARS + 2   # 截到 600 字，加上「……」
    one = [m for m in mats if m["title"] == "文章01"]
    assert one and len(one[0]["snippets"]) == 2
    assert one[0]["source"] == "《文章01》（新智元，2026-03-01）" and one[0]["tldr"] == "文章01的一句话" and one[0]["kind"] == "剪藏"
    assert len({m["path"] for m in mats}) == len(mats)


def test_复盘和报告不当材料(lib):
    v, calls, _ = lib
    _clip(v, "甲", "甲", "讲英伟达。")
    _write(str(v / "output" / "复盘" / "复盘_讨论_x_202601010000.md"), "# 讨论：英伟达\n\n英伟达英伟达\n")
    _write(str(v / "output" / "报告_英伟达.md"), "# 报告\n\n英伟达\n")
    _index(v)
    assert [m["title"] for m in discuss.gather(["英伟达"])] == ["甲"]


def test_标签范围只留这个标签的文章(lib):
    v, calls, _ = lib
    for n in range(1, 5):
        _clip(v, f"线{n}", f"线文{n}", "讲英伟达。", tag="线", day=f"2026-03-0{n}")
    for n in range(1, 6):
        _clip(v, f"别{n}", f"别文{n}", "讲英伟达。", tag="别的", day=f"2026-04-0{n}")
    _index(v)
    got = {m["title"] for m in discuss.gather(["英伟达"], "线")}
    assert got == {"线文1", "线文2", "线文3", "线文4"}
    assert len(discuss.gather(["英伟达"])) == 9


def test_标签范围命中太少时用最近几篇的一句话补足(lib):
    v, calls, _ = lib
    _clip(v, "命中", "命中的", "讲英伟达。", day="2026-03-01")
    for n in range(2, 10):
        _clip(v, f"没中{n}", f"没中{n}", "只讲别的事。", day=f"2026-03-{n:02d}", tldr=f"第{n}篇的小结")
    _index(v)
    mats = discuss.gather(["英伟达"], "线")
    assert len(mats) == discuss.TAG_FILL_DOCS == 6
    assert mats[0]["title"] == "命中的" and mats[0]["snippets"]
    filler = mats[1:]
    assert [m["title"] for m in filler] == [f"没中{n}" for n in (9, 8, 7, 6, 5)]   # 最近的在前
    assert all(m["snippets"] == [] and m["tldr"] for m in filler)
    # 命中够 3 篇就不补
    _clip(v, "又中1", "又中1", "讲英伟达。", day="2026-03-20")
    _clip(v, "又中2", "又中2", "讲英伟达。", day="2026-03-21")
    _index(v)
    assert len(discuss.gather(["英伟达"], "线")) == 3


# ---------------------------------------------------------------- 一轮问答

def test_一个都没找到时不调回答的模型(lib):
    v, calls, _ = lib
    _clip(v, "甲", "甲", "讲别的事。")
    _index(v)
    r = discuss.ask("量子计算进展", [], "")
    assert r["answer"] == discuss.NOT_FOUND == "笔记库里没找到相关内容，换个说法试试。" and r["sources"] == []
    assert len(calls) == 1 and "变成 3 到 6 个检索词" in calls[0]
    assert not _answers(calls)


def test_回答里的出处和sources对得上(lib):
    v, calls, replies = lib
    _clip(v, "甲", "甲文", "讲英伟达的算力。", day="2026-03-01")
    _clip(v, "乙", "乙文", "也讲英伟达。", day="2026-03-02")
    _index(v)
    replies["answer"] = "结论一 [2][2]。结论二 [1]，结论三 [9]。"
    r = discuss.ask("英伟达怎么样", [], "")
    assert [s["n"] for s in r["sources"]] == [2, 1]   # 按回答里出现的先后；越界的 [9] 丢掉
    s1 = r["sources"][1]
    assert s1["title"] in ("甲文", "乙文") and s1["path"].startswith("Spark/剪藏/") and s1["kind"] == "剪藏"
    assert s1["url"].startswith("/read/f/%E5%89%AA%E8%97%8F/") and s1["source"] == "新智元" and s1["date"].startswith("2026-03-0")
    # 材料编号和提示词里的 [n] 对应：sources[n] 的标题就是提示词里 [n] 那一块的标题
    prompt = _answers(calls)[0]
    for s in r["sources"]:
        assert f"[{s['n']}] 剪藏　《{s['title']}》（新智元，{s['date']}）" in prompt
    assert review._STYLE in prompt and "「范围限定" not in prompt
    replies["answer"] = "材料里没有这方面的内容。"
    assert discuss.ask("英伟达", [], "")["sources"] == []


def test_标签范围写进回答提示词(lib):
    v, calls, _ = lib
    _clip(v, "甲", "甲文", "讲英伟达。")
    _index(v)
    discuss.ask("英伟达", [], "线")
    assert "（范围限定在「线」这个主题下的文章）" in _answers(calls)[0]


def test_多轮时历史进提示词_最多6轮_回答截断_去掉旧编号(lib):
    v, calls, _ = lib
    _clip(v, "甲", "甲文", "讲英伟达。")
    _index(v)
    history = [{"q": f"第{i}问", "a": f"第{i}答 [7]" + "长" * 900} for i in range(1, 9)]
    discuss.ask("还有呢", discuss.clean_history(history), "")
    terms_prompt, answer = calls[0], _answers(calls)[0]
    assert "问：第3问" in answer and "问：第2问" not in answer and "问：第8问\n答：第8答 " in answer   # 最近 6 轮
    assert "[7]" not in answer
    assert "第8答 " + "长" * 796 + "……" in answer and "长" * 797 not in answer   # 整段回答截到 800 字
    # 提检索词时只带最近 3 轮，每轮答案截到 200 字
    assert "问：第5问" not in terms_prompt and "问：第6问" in terms_prompt and "第6答 " + "长" * 196 + "……" in terms_prompt
    assert "我最新的问题 =====\n还有呢" in terms_prompt


def test_clean_history和问题的校验():
    assert discuss.clean_history(None) == []
    assert discuss.clean_history([{"q": " 问 ", "a": "答"}]) == [{"q": "问", "a": "答"}]
    for bad in ("x", [1], [{"q": "a"}], [{"q": "a", "a": 3}], [{"q": "长" * 501, "a": "a"}]):
        with pytest.raises(review.ReviewError):
            discuss.clean_history(bad)
    for bad in ("", "   ", None, 3, "长" * 501):
        with pytest.raises(review.ReviewError):
            discuss.check_question(bad)


# ---------------------------------------------------------------- 存成复盘

TURNS = [
    {"q": "英伟达的算力  怎么样？\n第二行", "a": "涨了 [1]。\n\n也有分歧 [2]。",
     "sources": [{"n": 1, "title": "甲文", "path": "Spark/剪藏/20260301_甲.md", "kind": "剪藏", "date": "2026-03-01", "source": "新智元"},
                 {"n": 2, "title": "乙|文]", "path": "Spark/某播客/notes/20260102 乙.md", "kind": "单集小结", "date": "", "source": ""}]},
    {"q": "再问一句", "a": "没有。", "sources": []},
]


def test_存成复盘的结构(lib):
    v, calls, _ = lib
    path = discuss.save("线", TURNS)
    name = os.path.basename(path)
    assert name.startswith("复盘_讨论_英伟达的算力_怎么样？_第二行_") and name.endswith(".md") and os.path.dirname(path) == review.out_dir()
    text = open(path, encoding="utf-8").read()
    head, body = text.split("\n\n", 2)[0], text.split("\n\n", 2)[2]
    assert head == "# 讨论：英伟达的算力 怎么样？ 第二行"
    meta = text.split("\n\n")[1].splitlines()
    assert meta[0].startswith("- 生成时间：") and meta[1:] == ["- 范围：主题「线」", "- 共 2 轮"]
    assert "## 问：英伟达的算力 怎么样？ 第二行\n\n涨了 [1]。\n\n也有分歧 [2]。\n\n出处：\n" in text
    assert "- [1] [[Spark/剪藏/20260301_甲|《甲文》（新智元，2026-03-01）]]\n" in text
    assert "- [2] [[Spark/某播客/notes/20260102 乙|《乙|文]》（日期不详）]]" not in text   # | 和 ] 换成全角，不破坏双链
    assert "- [2] [[Spark/某播客/notes/20260102 乙|《乙｜文］》（日期不详）]]" in text
    assert "## 问：再问一句\n\n没有。\n" in text and text.endswith("没有。\n")
    # 同名再存一次不覆盖
    again = discuss.save("", TURNS[:1])
    assert again != path and "- 范围：整个笔记库" in open(again, encoding="utf-8").read()


def test_存出的讨论在列表里是讨论_能继续追问(lib, monkeypatch):
    v, calls, _ = lib
    from werkzeug.test import Client
    import spark
    c = Client(spark.application)
    r = c.post("/review/api/discuss/save", json={"tag": "线", "turns": TURNS})
    assert r.status_code == 200
    d = r.get_json()
    assert d["rel"].startswith("output/复盘/复盘_讨论_") and d["url"].startswith("/read/r/") and os.path.isfile(d["path"])
    items = c.get("/review/api/list").get_json()
    assert items[0]["kind"] == "discuss" and items[0]["title"].startswith("讨论：英伟达的算力")
    assert review.KIND_LABELS["discuss"] == "讨论" and "discuss" in review.KINDS
    # 继续追问：沿用复盘的接口（followup 直接调，不经阅读页的 mtime 校验）
    monkeypatch.setattr(review, "_llm", lambda prompt, max_tokens=8000: "追问的答案")
    assert review.is_review_path(d["rel"])
    review.followup(d["rel"], "接着说")
    assert "## 追问：接着说\n\n追问的答案" in open(d["path"], encoding="utf-8").read()


def test_save的非法输入都是400(lib):
    v, calls, _ = lib
    from werkzeug.test import Client
    import spark
    c = Client(spark.application)

    def post(**kw):
        return c.post("/review/api/discuss/save", json=kw).status_code

    good = dict(TURNS[1])
    src = dict(TURNS[0]["sources"][0])
    assert post(turns=[good]) == 200
    assert post() == 400 and post(turns=[]) == 400 and post(turns="x") == 400
    assert post(turns=[good] * 31) == 400
    assert post(turns=[{"q": "", "a": "a", "sources": []}]) == 400
    assert post(turns=[{"q": "q", "a": "", "sources": []}]) == 400
    assert post(turns=[{"q": "长" * 501, "a": "a", "sources": []}]) == 400
    assert post(turns=[{"q": "q", "a": "长" * 30001, "sources": []}]) == 400
    assert post(turns=[{"q": "q", "a": "a", "sources": "x"}]) == 400
    assert post(turns=[{"q": "q", "a": "a", "sources": [src] * 13}]) == 400
    assert post(turns=[{"q": "q", "a": 3, "sources": []}]) == 400
    assert post(turns=["x"]) == 400
    for bad in ({"path": "../../etc/passwd.md"}, {"path": "/etc/passwd.md"}, {"path": "Spark/../x.md"}, {"path": "Spark/.hidden/x.md"},
                {"path": "别处/x.md"}, {"path": "Spark/剪藏/x.txt"}, {"n": 0}, {"n": True}, {"n": "1"}, {"title": ""}, {"title": "长" * 301},
                {"date": "2026-03-01-xx"}, {"source": 3}):
        assert post(turns=[{"q": "q", "a": "a", "sources": [dict(src, **bad)]}]) == 400, bad
    assert post(tag="长" * 101, turns=[good]) == 400
    # 写到哪由服务端定：文件只会出现在 output/复盘/ 下，名字不受请求控制
    files = os.listdir(review.out_dir())
    assert files and all(f.startswith("复盘_讨论_") for f in files)
    assert not (v / "etc").exists()


def test_run接口不接讨论(lib):
    v, calls, _ = lib
    from werkzeug.test import Client
    import spark
    c = Client(spark.application)
    assert c.post("/review/api/run", json={"kind": "discuss"}).status_code == 400
    with pytest.raises(review.ReviewError):
        review.run("discuss", {})
    assert not calls


# ---------------------------------------------------------------- 接口

def _wait(c, job):
    for _ in range(200):
        st = c.get(f"/review/api/progress/{job}").get_json()
        if st["done"]:
            return st
        time.sleep(0.02)
    raise AssertionError("没结束")


def test_接口_问_追问_带标签(lib):
    v, calls, replies = lib
    from werkzeug.test import Client
    import spark
    c = Client(spark.application)
    for n in range(1, 4):
        _clip(v, f"线{n}", f"线文{n}", "讲英伟达。", day=f"2026-03-0{n}")
    _clip(v, "别", "别文", "讲英伟达。", tag="别的", day="2026-05-01")
    _index(v)
    assert c.post("/review/api/discuss", json={}).status_code == 400
    assert c.post("/review/api/discuss", json={"question": "  "}).status_code == 400
    assert c.post("/review/api/discuss", json={"question": "q", "history": "x"}).status_code == 400
    assert c.post("/review/api/discuss", json={"question": "q", "tag": 3}).status_code == 400
    r = c.post("/review/api/discuss", json={"question": "英伟达怎么样", "history": []})
    assert r.status_code == 200
    st = _wait(c, r.get_json()["job_id"])
    assert st["ok"] and st["kind"] == "discuss" and st["result"]["answer"] == "英伟达的算力在涨 [1]。"
    assert st["result"]["sources"][0]["n"] == 1 and set(st["result"]["sources"][0]) >= {"title", "path", "kind", "date", "url"}
    # 追问：历史由客户端带；带标签时只在这个标签里找
    calls.clear()
    r = c.post("/review/api/discuss", json={"question": "再说说", "history": [{"q": "英伟达怎么样", "a": st["result"]["answer"]}], "tag": "线"})
    st2 = _wait(c, r.get_json()["job_id"])
    assert st2["ok"]
    answer = _answers(calls)[0]
    assert "问：英伟达怎么样\n答：英伟达的算力在涨 。" in answer and "别文" not in answer and "线文3" in answer
    # 模型报错：错误原样给前端
    monkey_err = lambda prompt, max_tokens=8000: (_ for _ in ()).throw(review.ReviewError("模型调用失败：x"))
    review_llm, review._llm = review._llm, monkey_err
    try:
        st3 = _wait(c, c.post("/review/api/discuss", json={"question": "q"}).get_json()["job_id"])
    finally:
        review._llm = review_llm
    assert not st3["ok"] and "模型调用失败：x" in st3["error"]


def test_讨论和生成复盘互不挡住(lib):
    v, calls, _ = lib
    import apps.review.server as srv
    with srv.LOCK:
        srv.JOBS["x"] = {"id": "x", "kind": "discuss", "params": {}, "done": False, "ok": False, "stage": "", "error": "",
                         "result": None, "created_at": 0, "finished_at": 0}
    try:
        from werkzeug.test import Client
        import spark
        c = Client(spark.application)
        assert c.post("/review/api/discuss", json={"question": "q"}).status_code == 429   # 讨论自己一次一个
        r = c.post("/review/api/run", json={"kind": "tag", "tag": "线"})
        assert r.status_code == 200   # 在跑的讨论不挡复盘
        _wait(c, r.get_json()["job_id"])
    finally:
        with srv.LOCK:
            srv.JOBS.pop("x", None)


# ---------------------------------------------------------------- 手机提示词和存盘格式逐字一致

MAIN = """import Foundation
typealias D = DiscussPrompt
let cases = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
func turns(_ a: [[String: String]]) -> [D.Turn] { a.map { D.Turn(q: $0["q"]!, a: $0["a"]!) } }
var out: [String: Any] = [:]
out["query"] = (cases["query"] as! [[String: Any]]).map { D.queryPrompt(question: $0["question"] as! String, history: turns($0["history"] as! [[String: String]])) }
out["answer"] = (cases["answer"] as! [[String: Any]]).map { c -> String in
    let mats = (c["mats"] as! [[String: Any]]).map { D.Material(type: $0["type"] as! String, source: $0["source"] as! String, tldr: $0["tldr"] as! String, snippets: $0["snippets"] as! [String]) }
    return D.answerPrompt(question: c["question"] as! String, history: turns(c["history"] as! [[String: String]]), materials: mats, tag: c["tag"] as! String, style: c["style"] as! String)
}
out["terms"] = (cases["terms"] as! [[String]]).map { D.parseTerms($0[0], question: $0[1]) }
out["cited"] = (cases["cited"] as! [[String: Any]]).map { D.cited($0["text"] as! String, count: $0["count"] as! Int) }
out["cut"] = (cases["cut"] as! [[String: Any]]).map { D.cut($0["text"] as! String, $0["n"] as! Int) }
out["docs"] = (cases["docs"] as! [[String: Any]]).map { c -> String in
    let dec = JSONDecoder()
    let ts = try! dec.decode([D.SavedTurn].self, from: JSONSerialization.data(withJSONObject: c["turns"]!))
    let d = D.document(turns: ts, tag: c["tag"] as! String, generated: c["generated"] as! String)
    return D.render(title: d.title, meta: d.meta, body: d.body) + "\\u{1}" + D.fileName(firstQuestion: ts[0].q, stamp: "202611251030")
}
print(String(decoding: try! JSONSerialization.data(withJSONObject: out), as: UTF8.self))
"""


def test_两端提示词和存盘格式逐字一致(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path / "vault"))   # 下面会调 discuss.save：不指到临时目录就会写进真实笔记库
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "discuss_prompt"
    r = subprocess.run(["swiftc", "-o", str(exe), SWIFT_SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr

    hist = [{"q": f"第{i}问", "a": f"第{i}答 [3][12]\n\n\n\n后面" + "长" * 900} for i in range(1, 9)]
    mats = [{"type": "剪藏", "source": "《甲》（新智元，2026-03-01）", "tldr": "一句话", "snippets": ["摘录一", "摘录二\n\n\n\n有空行"]},
            {"type": "单集小结", "source": "《乙》（某播客，日期不详）", "tldr": "", "snippets": []},
            {"type": "论文", "source": "《丙》（arXiv，2026-01-02）", "tldr": "只有一句话", "snippets": []}]
    cases = {
        "query": [{"question": "英伟达怎么样", "history": []}, {"question": "它呢？", "history": hist[:2]}, {"question": "还有", "history": hist}],
        "answer": [{"question": "英伟达怎么样", "history": [], "mats": mats, "tag": "", "style": review._STYLE},
                   {"question": "它呢", "history": hist, "mats": mats[:1], "tag": "AI \"芯片\"", "style": review._STYLE}],
        "terms": [["英伟达 算力，GPU、gpu\n别的行", "问"], ["", "英伟达的算力，怎么样？"], ["？ ，", "RSI 走到哪了"], ["\n\n a1 b2 c3 d4 e5 f6 g7", "问"],
                  ["", "具身智能的数据飞轮是什么"], ["x " + "长" * 21 + " 有效词", "问"], ["空\t格\r\n行", "问"], ["", "ab"], ["", ""]],
        "cited": [{"text": "a [2][2] b [1] c [9] d [0] [x] [1]", "count": 3}, {"text": "没有", "count": 3}],
        "cut": [{"text": "短", "n": 5}, {"text": "甲\n\n\n\n乙" + "长" * 20, "n": 6}, {"text": "  前后空白  ", "n": 20}, {"text": "长" * 9 + " \n ", "n": 5}],
        "docs": [{"tag": "线", "generated": "2026-11-25 10:30", "turns": TURNS},
                 {"tag": "", "generated": "2026-11-25 10:30（在手机上生成，Claude）", "turns": TURNS[1:]},
                 {"tag": "", "generated": "g", "turns": [{"q": "长" * 40 + "/问:题", "a": "答", "sources": TURNS[0]["sources"]}]}],
    }
    want = {
        "query": [discuss.query_prompt(c["question"], c["history"]) for c in cases["query"]],
        "answer": [discuss.answer_prompt(c["question"], c["history"], c["mats"], c["tag"], c["style"]) for c in cases["answer"]],
        "terms": [discuss.parse_terms(a, b) for a, b in cases["terms"]],
        "cited": [discuss.cited(c["text"], c["count"]) for c in cases["cited"]],
        "cut": [review._cut(c["text"], c["n"]) for c in cases["cut"]],
        "docs": [],
    }
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "202611251030" if fmt == "%Y%m%d%H%M" else "2026-11-25 10:30")
    for c in cases["docs"]:
        title, body, meta = discuss.document(c["turns"], c["tag"], c["generated"])
        text = f"# {title}\n\n" + "\n".join(f"- {m}" for m in meta) + "\n\n" + body.strip() + "\n"
        want["docs"].append(text + "\x01" + discuss.file_name(c["turns"][0]["q"], "202611251030"))
    run = subprocess.run([str(exe)], input=json.dumps(cases), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    for key in want:
        for g, w in zip(got[key], want[key]):
            assert g == w, key
        assert len(got[key]) == len(want[key])
    # 真的写出的文件和 render 一致
    path = discuss.save("线", TURNS)
    assert open(path, encoding="utf-8").read() == want["docs"][0].split("\x01")[0]


def test_本地模型时讨论和复盘互相挡住(lib, monkeypatch):
    import apps.review.server as srv
    from werkzeug.test import Client
    import spark
    monkeypatch.setattr(srv, "_one_at_a_time", lambda: True)
    c = Client(spark.application)
    for running, url, body in (("discuss", "/review/api/run", {"kind": "tag", "tag": "线"}),
                               ("tag", "/review/api/discuss", {"question": "q"})):
        with srv.LOCK:
            srv.JOBS["x"] = {"id": "x", "kind": running, "params": {}, "done": False, "ok": False, "stage": "", "error": "",
                             "result": None, "created_at": 0, "finished_at": 0}
        try:
            assert c.post(url, json=body).status_code == 429
        finally:
            with srv.LOCK:
                srv.JOBS.pop("x", None)


def test_标签里的换行存盘时压平(lib):
    v, _, _ = lib
    from core import discuss
    path = discuss.save("线\n# 假标题", [{"q": "问", "a": "答", "sources": []}])
    assert "\n# 假标题" not in open(path, encoding="utf-8").read()


def test_后端判断_本地模型才互相挡住(monkeypatch):
    import apps.review.server as srv
    from core import settings
    monkeypatch.setattr(settings, "backend", lambda default: "ollama")
    monkeypatch.setattr(settings, "model_for", lambda backend: "qwen")
    assert srv._one_at_a_time() is True
    monkeypatch.setattr(settings, "backend", lambda default: "cli")
    assert srv._one_at_a_time() is False
