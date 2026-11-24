"""标签索引（core/tags.py）：读写 Spark/.tags.json、别名、主文件规则、标签云、补标签、接口。
全部用样例库的副本或临时目录，不读真实笔记库，不调真实模型。"""

import hashlib
import json
import os
import shutil
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import tags  # noqa: E402

FIXTURE = os.path.join(ROOT, "apps", "mobile-ios", "dev", "fixture-vault")


@pytest.fixture
def lib(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    shutil.copytree(FIXTURE, v)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setattr(tags, "_JOBS", {})
    monkeypatch.setattr(tags, "_ask", lambda: None)
    from core import github_sync
    monkeypatch.setattr(github_sync, "after_task", lambda: None)
    return str(v)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _checksums(root, skip=(".tags.json",)):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            if f in skip or f.startswith(".tags.bad-"):
                continue
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def _tags_file(root):
    return os.path.join(root, "Spark", ".tags.json")


def _strip_index(root):
    """样例库副本去掉 .tags.json：从没有标签开始。"""
    os.remove(_tags_file(root))


# ---------------------------------------------------------------- 读写

@pytest.mark.parametrize("text", ["{坏的", "[1, 2]", '"字符串"', ""])
def test_bad_file_reads_empty_and_is_kept(lib, text):
    _write(_tags_file(lib), text)
    assert tags.load(lib) == tags._empty()
    assert tags.all_items(lib)                       # 不抛错
    assert open(_tags_file(lib), encoding="utf-8").read() == text   # 读不改文件
    tags.set_kind("传感器", "tech", lib)
    bad = [n for n in os.listdir(os.path.join(lib, "Spark")) if n.startswith(".tags.bad-") and n.endswith(".json")]
    assert len(bad) == 1
    assert open(os.path.join(lib, "Spark", bad[0]), encoding="utf-8").read() == text
    assert tags.load(lib)["kinds"] == {"传感器": "tech"}


def test_invalid_items_are_dropped_not_the_whole_index(lib):
    """合法的对象里，单项不合法只丢那一项；写回时只写合法的项，不改名挪开。"""
    _write(_tags_file(lib), json.dumps({
        "version": 1,
        "aliases": {"DIY": "动手做", "坏": 3},
        "kinds": {"传感器": "tech", "坏": "food"},
        "docs": {"a.md": ["x"], "b.md": "不是列表", "c.md": ["y", 2]}}, ensure_ascii=False))
    data = tags.load(lib)
    assert data == {"version": 1, "aliases": {"DIY": "动手做"}, "kinds": {"传感器": "tech"}, "docs": {"a.md": ["x"]}, "sources": {}}
    tags.set_kind("社区", "biz", lib)
    assert not [n for n in os.listdir(os.path.join(lib, "Spark")) if n.startswith(".tags.bad-")]
    on_disk = json.load(open(_tags_file(lib), encoding="utf-8"))
    assert on_disk["kinds"] == {"传感器": "tech", "社区": "biz"} and on_disk["docs"] == {"a.md": ["x"]}
    assert on_disk["aliases"] == {"DIY": "动手做"}
    # 整个字段类型不对：只丢这个字段
    _write(_tags_file(lib), '{"version": 1, "aliases": [], "docs": {"a.md": ["x"]}}')
    assert tags.load(lib)["docs"] == {"a.md": ["x"]} and tags.load(lib)["aliases"] == {}


def test_newer_version_reads_empty_and_refuses_write(lib):
    text = '{"version": 2, "docs": {"a.md": ["x"]}, "新字段": 1}'
    _write(_tags_file(lib), text)
    assert tags.load(lib) == tags._empty()
    with pytest.raises(tags.TagsError):
        tags.set_kind("传感器", "tech", lib)
    with pytest.raises(tags.TagsError):
        tags.tag_untagged(ask=lambda *a, **k: "T|1|新的|标签", root=lib)
    assert open(_tags_file(lib), encoding="utf-8").read() == text
    assert not [n for n in os.listdir(os.path.join(lib, "Spark")) if n.startswith(".tags.bad-")]


def test_unreadable_file_is_not_overwritten(lib, monkeypatch):
    def boom(p):
        raise PermissionError("读不了")
    monkeypatch.setattr(tags, "_read", boom)
    before = open(_tags_file(lib), encoding="utf-8").read()
    with pytest.raises(tags.TagsError):
        tags.set_kind("x", "org", lib)
    assert open(_tags_file(lib), encoding="utf-8").read() == before


def test_fixture_index_is_valid_and_keys_are_main_files():
    data = json.load(open(os.path.join(FIXTURE, "Spark", ".tags.json"), encoding="utf-8"))
    assert tags._read(os.path.join(FIXTURE, "Spark", ".tags.json")) == ({"sources": {}, **data}, "")   # 每一项都合法
    items = {it["path"]: it for it in tags.all_items(FIXTURE)}
    for key in data["docs"]:
        assert key in items and items[key]["type"] in tags.UNTAGGED_TYPES, key
    assert all(tags.resolve(v, data["aliases"]) == v for v in data["aliases"].values())


# ---------------------------------------------------------------- 别名和类别

def test_alias_chain_and_cycle(lib):
    _strip_index(lib)
    tags.set_alias("B", "C", lib)
    tags.set_alias("A", "B", lib)
    assert tags.load(lib)["aliases"] == {"B": "C", "A": "C"}      # 指到最终的那个
    with pytest.raises(tags.TagsError):
        tags.set_alias("C", "A", lib)                               # C→A→C 成环
    with pytest.raises(tags.TagsError):
        tags.set_alias("X", " X ", lib)                             # 指向自己
    tags.set_alias("C", "D", lib)                                   # 原来指向 C 的一起改到 D
    assert tags.load(lib)["aliases"] == {"B": "D", "A": "D", "C": "D"}
    assert tags.resolve(" A ", tags.load(lib)["aliases"]) == "D"
    assert tags.resolve("x", {"x": "y", "y": "x"}) in ("x", "y")     # 手改出来的环不死循环
    tags.remove_alias("A", lib)
    assert "A" not in tags.load(lib)["aliases"]
    tags.set_kind("B", "org", lib)                                  # 类别记在正式标签上
    assert tags.load(lib)["kinds"] == {"D": "org"}
    with pytest.raises(tags.TagsError):
        tags.set_kind("D", "food", lib)


# ---------------------------------------------------------------- 主文件

def test_main_file_rule():
    s = "Spark/节目"
    assert tags.main_file([f"{s}/transcripts/a.md", f"{s}/speech/a.md", f"{s}/notes/a.md"]) == f"{s}/notes/a.md"
    assert tags.main_file([f"{s}/transcripts/a.md", f"{s}/speech/a.md"]) == f"{s}/speech/a.md"
    assert tags.main_file([f"{s}/transcripts/a.md"]) == f"{s}/transcripts/a.md"
    assert tags.main_file(["Spark/信息跟进/周刊/x.md"]) == "Spark/信息跟进/周刊/x.md"


def test_episode_grouping_and_types():
    items = tags.all_items(FIXTURE)
    eps = {it["path"]: it for it in items if it["type"] == "episode"}
    assert set(eps) == {"Quiet Workshop/notes/20261119 Episode 12 Keeping a Lab Notebook.md",
                        "Quiet Workshop/transcripts/20261030_Episode 11 Repairing Old Radios.md",
                        "Quiet Workshop/speech/20261008_Why Pencils Are Hexagonal.md",
                        "Quiet Workshop/speech/20261112_Episode 13 Small Habits in a Busy Workshop.md"}
    assert {it["type"] for it in items} == {"clip", "paper", "episode", "talk", "track"}   # 样例库里有 3 篇论文
    talks = [it for it in items if it["type"] == "talk"]
    assert len(talks) == 2 and all("/speech/" in it["path"] for it in talks)
    tracks = [it for it in items if it["type"] == "track"]
    assert len(tracks) == 4 and not any("简报" in it["path"] for it in tracks)
    ep12 = eps["Quiet Workshop/notes/20261119 Episode 12 Keeping a Lab Notebook.md"]
    assert ep12["title"] == "Episode 12: Keeping a Lab Notebook" and ep12["date"] == "2026-11-19" and ep12["summary"]


def test_tags_found_when_main_file_changes(lib):
    """docs 记在整理稿下，后来补了小结（主文件换成 notes/）：标签照样认。"""
    key = "Quiet Workshop/speech/20261112_Episode 13 Small Habits in a Busy Workshop.md"
    _write(os.path.join(lib, "Spark", "Quiet Workshop", "notes", "20261112 Episode 13 Small Habits in a Busy Workshop.md"),
           "# Episode 13\n")
    it = next(i for i in tags.all_items(lib) if "Episode 13" in i["path"])
    assert it["path"].startswith("Quiet Workshop/notes/") and it["tags"] == ["工作习惯", "收纳"]
    assert tags.doc_tags(key, lib) == ["工作习惯", "收纳"]


def test_papers_are_read_from_frontmatter(lib):
    _write(os.path.join(lib, "Spark", "论文", "20261101_Attention.md"),
           '---\ntitle: "Attention"\nclipped: 2026-11-01\ntags: ["DIY", "动手做", "注意力"]\ntldr: "一句话"\n---\n# A\n')
    paper = [it for it in tags.all_items(lib) if it["type"] == "paper" and it["path"] == "论文/20261101_Attention.md"]
    assert paper == [{"path": "论文/20261101_Attention.md", "title": "Attention", "date": "2026-11-01",
                      "type": "paper", "summary": "一句话", "source": "", "tags": ["动手做", "注意力"]}]
    assert tags.doc_tags("论文/20261101_Attention.md", lib) == ["动手做", "注意力"]
    assert "论文/20261101_Attention.md" not in tags.load(lib)["docs"]


# ---------------------------------------------------------------- 标签云和条目

def test_cloud_counts_sorts_and_min():
    c = tags.cloud(root=FIXTURE)
    assert len(c) == 28 and {x["kind"] for x in c} == {"tech", "org", "biz"}   # 手机「主题」词云要有 25 个以上的词
    assert c[:3] == [{"tag": "教育", "count": 8, "kind": "biz"}, {"tag": "测量", "count": 8, "kind": "tech"},
                     {"tag": "写作", "count": 6, "kind": "tech"}]
    all1 = {x["tag"]: x for x in tags.cloud(1, FIXTURE)}
    # 第 12 期同时有「实验记录」和「Lab Notebook」（指向实验记录）：只算一次
    assert all1["实验记录"]["count"] == 6 and "Lab Notebook" not in all1
    assert all1["文具"]["kind"] == "tech"                     # 没有类别按 tech
    assert tags.cloud(9, FIXTURE) == []
    counts = [(-x["count"], x["tag"]) for x in tags.cloud(1, FIXTURE)]
    assert counts == sorted(counts)


def test_items_for_across_types_newest_first():
    got = tags.items_for("DIY", FIXTURE)
    assert {it["type"] for it in got} == {"clip", "episode", "talk", "track"}
    dates = [it["date"] for it in got]
    assert dates == sorted(dates, reverse=True)
    assert tags.items_for("动手做", FIXTURE) == got


# ---------------------------------------------------------------- 补标签

def test_parse_reply_is_strict():
    text = "\n".join([
        "T|1|英伟达|Agent|英伟达|  ",          # 重复、空的去掉
        "T|2|a|b|c|d|e",                       # 超过 4 个只要前 4 个
        "T|9|越界",                            # 序号对不上
        "T|1|重复的序号",
        "1|没有前缀", "T|x|序号不是数字", "T 3|少了竖线",
        "T|3|", "T|3|" + "长" * 30,           # 一个合格标签都没有：不算
        "K|Agent|tech", "K|英伟达|food", "K|太多|tech|多余", "随便说点什么",
    ])
    got, kinds = tags.parse_reply(text, 3)
    assert got == {1: ["英伟达", "Agent"], 2: ["a", "b", "c", "d"]}
    assert kinds == {"Agent": "tech"}


def test_parse_reply_tolerates_bullets_backticks_fences_fullwidth_bars():
    text = "\n".join([
        "```", "```text",
        "- T|1|英伟达|Agent",
        "* T|2|储能",
        "• `T|3|强化学习|黄仁勋`",
        "  `K|储能|biz`  ",
        "T｜4｜全角｜竖线",
        "K｜全角｜tech",
        "```",
        "-T|5|没有空格的列表符号不认",
    ])
    got, kinds = tags.parse_reply(text, 5)
    assert got == {1: ["英伟达", "Agent"], 2: ["储能"], 3: ["强化学习", "黄仁勋"], 4: ["全角", "竖线"]}
    assert kinds == {"储能": "biz", "全角": "tech"}


def _fake_ask(calls, fail_on=None, reply=None):
    def ask(prompt, max_tokens=4000, timeout=600):
        calls.append(prompt)
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("模型出错")
        if reply is not None:
            return reply(prompt)
        n = prompt.split("内容列表")[1].count("\n") - 1
        return "\n".join(f"T|{i}|新标签{len(calls)}|动手做" for i in range(1, n + 1)) + f"\nK|新标签{len(calls)}|biz"
    return ask


def test_fill_writes_docs_and_kinds(lib):
    _strip_index(lib)
    before = _checksums(lib)
    calls = []
    r = tags.tag_untagged(ask=_fake_ask(calls), root=lib, batch=4)
    assert r["total"] == 10 and r["tagged"] == 10 and r["batches"] == 3 == len(calls)
    data = tags.load(lib)
    assert len(data["docs"]) == 10 and data["kinds"]["新标签1"] == "biz"
    assert "动手做" not in data["kinds"]                    # 已有标签不由模型定类别
    # 提示词里有已有标签表、原有话题、类型
    assert "动手做" in calls[0].split("已有标签（")[1] and "播客" in "".join(calls) and "实验记录、工作习惯" in "".join(calls)
    assert tags.tag_untagged(ask=_fake_ask(calls), root=lib)["total"] == 0   # 都有了，不再问
    assert _checksums(lib) == before                        # 除了 .tags.json 什么都没改


def test_fill_skips_items_without_tags_and_limit(lib):
    _strip_index(lib)
    calls = []
    r = tags.tag_untagged(limit=3, ask=_fake_ask(calls, reply=lambda p: "T|2|只有第二条"), root=lib)
    assert r == {"total": 3, "tagged": 1, "skipped": 2, "batches": 1, "new_tags": None}
    assert list(tags.load(lib)["docs"].values()) == [["只有第二条"]]   # 没拿到的不写空列表
    assert not os.path.exists(_tags_file(lib) + ".tmp")


def test_dry_run_writes_preview_only(lib, tmp_path):
    _strip_index(lib)
    before = _checksums(lib, skip=())
    preview = str(tmp_path / "preview.json")
    r = tags.tag_untagged(dry_run=True, ask=_fake_ask([]), root=lib, preview_path=preview, batch=40)
    assert r["tagged"] == 10 and not os.path.exists(_tags_file(lib))
    assert _checksums(lib, skip=()) == before
    p = json.load(open(preview, encoding="utf-8"))
    assert len(p["items"]) == 10 and {"path", "title", "type", "tags"} <= set(p["items"][0])
    assert p["new_tags"] == {"新标签1": "biz"}


def test_failure_mid_way_keeps_finished_batches(lib):
    _strip_index(lib)
    with pytest.raises(RuntimeError):
        tags.tag_untagged(ask=_fake_ask([], fail_on=2), root=lib, batch=4)
    assert len(tags.load(lib)["docs"]) == 4                  # 第一批留着
    r = tags.tag_untagged(ask=_fake_ask([]), root=lib, batch=4)
    assert r["total"] == 6                                   # 下次只补剩下的


def test_reconcile_only_drops_missing(lib):
    data = tags.load(lib)
    data["docs"]["没有了/notes/x.md"] = ["a"]
    with open(_tags_file(lib), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    assert "没有了/notes/x.md" in tags.load(lib)["docs"]     # 读的时候不对账
    tags.all_items(lib)
    tags.tag_untagged(ask=_fake_ask([]), root=lib)           # 补标签任务也不对账
    assert "没有了/notes/x.md" in tags.load(lib)["docs"]
    assert "没有了/notes/x.md" not in {it["path"] for it in tags.all_items(lib)}   # 留着无害
    assert tags.reconcile(lib) == 1
    after = tags.load(lib)["docs"]
    assert "没有了/notes/x.md" not in after and len(after) == 10
    assert tags.reconcile(lib) == 0


def test_classify_kinds(lib):
    calls = []

    def ask(prompt, max_tokens=4000, timeout=600):
        calls.append(prompt)
        return "K|数学|tech\nK|跑步|biz\nK|不在列表里|org\nK|阅读方法|food"
    r = tags.classify_kinds(ask, lib)
    kinds = tags.load(lib)["kinds"]
    assert kinds["数学"] == "tech" and kinds["跑步"] == "biz" and "不在列表里" not in kinds and "阅读方法" not in kinds
    assert kinds["社区"] == "biz"                             # 已有的不改
    assert "社区" not in calls[0] and r["classified"] == 2


def test_whole_flow_leaves_other_files_untouched(lib, tmp_path):
    before = _checksums(lib)
    tags.set_alias("NVIDIA", "英伟达", lib)
    tags.set_kind("英伟达", "org", lib)
    tags.reconcile(lib)
    tags.tag_untagged(ask=_fake_ask([]), root=lib)
    tags.tag_untagged(dry_run=True, ask=_fake_ask([]), root=lib, preview_path=str(tmp_path / "p.json"))
    tags.classify_kinds(lambda *a, **k: "K|数学|tech", lib)
    tags.cloud(root=lib), tags.items_for("社区", lib)
    assert _checksums(lib) == before


# ---------------------------------------------------------------- 后台任务和接口

def _wait(job_id):
    for _ in range(500):
        s = tags.status(job_id)
        if s and s["done"]:
            return s
        time.sleep(0.01)
    raise AssertionError("任务没做完")


def test_only_one_fill_job(lib, monkeypatch):
    import threading
    gate = threading.Event()

    def slow(prompt, max_tokens=4000, timeout=600):
        gate.wait(5)
        return ""
    _strip_index(lib)
    job = tags.start_fill(ask=slow, root=lib)
    with pytest.raises(tags.TagsError):
        tags.start_fill(ask=slow, root=lib)
    assert tags.active_count() == 1
    gate.set()
    s = _wait(job)
    assert s["error"] == "" and s["result"]["tagged"] == 0
    assert tags.active_count() == 0


def test_no_model_configured_refuses(lib):
    with pytest.raises(tags.TagsError):
        tags.start_fill(root=lib)


def test_ollama_backend_is_not_used(monkeypatch):
    from core import llm_config
    monkeypatch.setattr(llm_config, "resolve", lambda data, default_backend: {
        "backend": "ollama", "api_key": "", "api_base": "", "model": "m"})
    assert tags._ask() is None


@pytest.fixture
def client(lib):
    from apps.reader import server
    return server.app.test_client()


def test_api(client, lib, monkeypatch):
    r = client.get("/api/tags").get_json()
    assert r["total"] == len(tags.all_items(lib)) and [t["tag"] for t in r["tags"]][:3] == ["教育", "测量", "写作"]
    assert len(client.get("/api/tags?min=1").get_json()["tags"]) > 3
    assert client.get("/api/tags?min=x").status_code == 400
    r = client.get("/api/tags/items?tag=DIY").get_json()
    assert r["tag"] == "动手做" and len(r["items"]) == 4
    assert client.get("/api/tags/items").status_code == 400
    assert client.get("/api/tags/aliases").get_json()["aliases"]["DIY"] == "动手做"
    r = client.post("/api/tags/alias", json={"src": "NVIDIA", "dst": "英伟达"}).get_json()
    assert r["aliases"]["NVIDIA"] == "英伟达"
    assert client.post("/api/tags/alias", json={"src": "英伟达", "dst": "NVIDIA"}).status_code == 400
    assert "NVIDIA" not in client.post("/api/tags/alias", json={"src": "NVIDIA", "dst": ""}).get_json()["aliases"]
    assert client.post("/api/tags/kind", json={"tag": "DIY", "kind": "org"}).get_json()["kinds"]["动手做"] == "org"
    assert client.post("/api/tags/kind", json={"tag": "DIY", "kind": "x"}).status_code == 400
    # 跨站的写请求挡掉（core/web_guard）
    assert client.post("/api/tags/kind", json={"tag": "a", "kind": "org"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    # 补标签：没配模型拒绝；配了（假的）返回任务号
    assert client.post("/api/tags/fill", json={}).status_code == 400
    assert client.post("/api/tags/fill", json={"limit": 0}).status_code == 400
    monkeypatch.setattr(tags, "_ask", lambda: _fake_ask([]))
    r = client.post("/api/tags/fill", json={"dry_run": True})
    assert r.status_code == 202
    s = _wait(r.get_json()["id"])
    assert s["error"] == "" and s["preview"].endswith("tags-preview.json") and os.path.isfile(s["preview"])
    assert client.get(f"/api/tags/fill/{s['id']}").get_json()["done"] is True
    assert client.get("/api/tags/fill/nope").status_code == 404


def test_ask_uses_given_model_without_touching_settings(monkeypatch):
    """补标签可以只在这一步换模型：后端、Key 照设置里的，model 用传进来的。"""
    from core import llm, llm_config
    seen = {}
    monkeypatch.setattr(llm_config, "resolve", lambda *a, **k: {"backend": "api", "api_key": "k", "model": "m-default", "api_base": ""})
    monkeypatch.setattr(llm, "complete", lambda prompt, backend, **kw: seen.update(kw) or "ok")
    tags._ask("m-other")("p")
    assert seen["model"] == "m-other"
    tags._ask()("p")
    assert seen["model"] == "m-default"


def test_review_fixes(lib):
    """审查指出的几处：不是 UTF-8 的文件算坏文件；版本号写法奇怪的不读不写；手填的标签名不许带换行、竖线；补类别没有试跑。"""
    p = tags.path(lib)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\xff\xfe{}")
    assert tags.load(lib) == tags._empty()
    for bad in ('{"version": "2", "docs": {"a.md": ["x"]}}', '{"version": 2.0, "docs": {}}', '{"version": 0}'):
        with open(p, "w", encoding="utf-8") as f:
            f.write(bad)
        assert tags.load(lib) == tags._empty()
        with pytest.raises(tags.TagsError):
            tags.set_kind("x", "tech", lib)
        assert open(p, encoding="utf-8").read() == bad
    os.rename(p, p + ".keep")
    for src, dst in (("a\nb", "c"), ("a|b", "c"), ("a", "x" * 200)):
        with pytest.raises(tags.TagsError):
            tags.set_alias(src, dst, lib)
    with pytest.raises(tags.TagsError):
        tags.set_kind("a\tb", "tech", lib)
    with pytest.raises(tags.TagsError):
        tags.start_fill(kinds=True, dry_run=True, preview_path=p + ".preview", root=lib, ask=lambda *a, **k: "")
    assert not os.path.exists(p)


def test_generic_tags_are_not_accepted(lib):
    """「AI」这类太泛的词不当标签：模型给了不收，手工也设不了；一条只剩泛词时留到下次。"""
    got, _ = tags.parse_reply("T|1|AI|量子计算\nT|2|ai|人工智能\nT|3|AI安全", 3)
    assert got == {1: ["量子计算"], 3: ["AI安全"]}
    with pytest.raises(tags.TagsError):
        tags.set_alias("机器学习", "AI", lib)


def test_source_folder_rules(lib):
    """来源文件夹自带标签：来自这个文件夹的都算；上层文件夹的规则对分会也算；和模型补的同名只算一次；过别名；剪藏不受影响。"""
    idx = {**tags._empty(), "aliases": {"A16Z": "a16z"},
           "docs": {"信息跟进/a16z/x.md": ["风险投资", "a16z"], "大会/分会/speech/1.md": ["液冷"]},
           "sources": {"信息跟进/a16z": "A16Z", "a16z - Videos": "a16z", "大会": "OCP", "大会/分会": "冷却分会", "大": "不该中"}}
    assert tags.folder_of(["a16z - Videos/speech/x.md", "a16z - Videos/notes/x.md"]) == "a16z - Videos"
    assert tags.folder_of(["信息跟进/a16z/x.md"]) == "信息跟进/a16z"
    assert tags.folder_of(["大会/分会/speech/1.md"]) == "大会/分会" and tags.folder_of(["x.md"]) == ""
    assert tags.item_tags(idx, ["信息跟进/a16z/x.md"]) == (["风险投资", "a16z"], True)
    assert tags.item_tags(idx, ["a16z - Videos/notes/y.md"]) == (["a16z"], False)          # 没补过标签的也算，但仍算「没补过」
    assert tags.item_tags(idx, ["大会/分会/speech/1.md"]) == (["液冷", "OCP", "冷却分会"], True)
    assert tags.item_tags(idx, ["大会2/speech/1.md"]) == ([], False)                       # 只是前缀像，不算
    tags.set_source("信息跟进/a16z/", "a16z", lib)
    assert tags.load(lib)["sources"] == {"信息跟进/a16z": "a16z"}
    tags.set_source("信息跟进/a16z", "", lib)
    assert tags.load(lib)["sources"] == {}
    for folder, tag in (("", "a16z"), ("../x", "a16z"), ("a\nb", "a16z"), ("信息跟进/a16z", "AI"), ("信息跟进/a16z", "a|b")):
        with pytest.raises(tags.TagsError):
            tags.set_source(folder, tag, lib)


def test_source_api(lib, monkeypatch):
    """/api/tags/source：记下、去掉来源规则；写法不对的 400。"""
    from apps.reader import server
    monkeypatch.setattr(server.core_vault, "vault_root", lambda: lib)
    monkeypatch.setattr(tags.vault, "vault_root", lambda: lib)
    c = server.app.test_client()
    r = c.post("/api/tags/source", json={"folder": "信息跟进/a16z", "tag": "a16z"})
    assert r.status_code == 200 and r.get_json()["sources"] == {"信息跟进/a16z": "a16z"}
    assert c.get("/api/tags/aliases").get_json()["sources"] == {"信息跟进/a16z": "a16z"}
    assert c.post("/api/tags/source", json={"folder": "信息跟进/a16z", "tag": ""}).get_json()["sources"] == {}
    assert c.post("/api/tags/source", json={"folder": "../x", "tag": "a"}).status_code == 400
    assert c.post("/api/tags/source", json={"folder": 3, "tag": "a"}).status_code == 400
