"""手机「主题」页的标签规则（apps/mobile-ios/Spark/TagIndex.swift）跟电脑 core/tags.py 一致。
同一份输入两边都跑（Swift 用 swiftc 单独编出来，只依赖 Foundation）：.tags.json 的解析（坏文件、版本、类型不对的项）、
别名（链、环）、一篇里两个写法指向同一个标签、主文件和同一期别的文件的键、篇数降序同数按标签升序、某个标签下的条目先后。
比对：每条的正式标签、cloud（标签、篇数、类别、顺序）、items_for 的先后逐项相同。"""

import base64
import json
import os
import shutil
import subprocess

import pytest

from core import clips, tags

ROOT = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios")
SRC = os.path.join(ROOT, "Spark", "TagIndex.swift")
FIXTURE = os.path.join(ROOT, "dev", "fixture-vault")

MAIN = """import Foundation
let cases = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [[String: Any]]
var out: [[String: Any]] = []
for c in cases {
    let idx = TagIndex(json: (c["index"] as? String).map { String(decoding: Data(base64Encoded: $0)!, as: UTF8.self) })   // base64：JSON 里的 BOM 会被 Foundation 吃掉
    let items = (c["items"] as! [[String: Any]]).map {
        TagItem(ref: $0["ref"] as! String, type: $0["type"] as! String, paths: $0["paths"] as! [String],
                headerTags: $0["header"] as? [String] ?? [], date: $0["date"] as! String)
    }
    let cloud = idx.cloud(items, minCount: c["min"] as! Int).map { ["tag": $0.tag, "count": $0.count, "kind": $0.kind] as [String: Any] }
    var per: [String: Any] = [:]
    for q in c["queries"] as! [String] { per[q] = idx.items(for: q, in: items).map(\\.ref) }
    out.append(["cloud": cloud, "tags": items.map { idx.tags(of: $0) }, "items": per])
}
print(String(decoding: try! JSONSerialization.data(withJSONObject: out), as: UTF8.self))
"""


@pytest.fixture(scope="module")
def swift(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("tags_ios")
    main = d / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = d / "tags_ios"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr

    def run(cases):
        sent = [dict(c, index=None if c["index"] is None else base64.b64encode(c["index"].encode()).decode()) for c in cases]
        run = subprocess.run([str(exe)], input=json.dumps(sent), capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)
    return run


def _py_index(text, tmp):
    """电脑读索引：_read 的结果（坏的、版本不对都是空索引）。text 是 None 表示没有这个文件。"""
    p = os.path.join(tmp, "tags.json")
    if os.path.exists(p):
        os.remove(p)
    if text is not None:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return tags._read(p)[0]


def _py_items(items, idx):
    """跟 core/tags.py 的 _frontmatter_items、_group_item 一样的找标签的办法"""
    out = []
    for it in items:
        if it["type"] in ("clip", "paper"):
            got, path = tags._clean_tags(it.get("header"), idx["aliases"]), it["paths"][0]
        else:
            main = tags.main_file(it["paths"])
            got, path = tags.item_tags(idx, it["paths"])[0], main
        out.append({"ref": it["ref"], "path": path, "date": it["date"], "type": it["type"], "tags": got})
    return out


def _compare(swift, cases, tmp):
    want = []
    for c in cases:
        idx = _py_index(c["index"], tmp)
        items = _py_items(c["items"], idx)
        want.append({"cloud": tags.cloud(c["min"], tmp, items, idx), "tags": [it["tags"] for it in items],
                     "items": {q: [it["ref"] for it in tags.items_for(q, tmp, items, idx)] for q in c["queries"]}})
    got = swift(cases)
    for i, (g, w) in enumerate(zip(got, want)):
        assert g["tags"] == w["tags"], (i, "每条的标签")
        assert g["cloud"] == w["cloud"], (i, "cloud")
        assert g["items"] == w["items"], (i, "items_for")
    return got


def _it(ref, type_, paths, date, header=None):
    return {"ref": ref, "type": type_, "paths": paths, "date": date, "header": header or []}


# ---------------------------------------------------------------- 样例库：整条线对照

def _fixture_cases():
    """样例库的 .tags.json，条目照 core/tags.py 认出来的：剪藏读文件头原样的 tags，节目、演讲、信息文章带上同一期的全部文件"""
    text = open(os.path.join(FIXTURE, "Spark", ".tags.json"), encoding="utf-8").read()
    idx = tags.load(FIXTURE)
    items = []
    for c in clips.load_clips(FIXTURE):
        items.append(_it(f"c:{c['name']}", "clip", [f"剪藏/{c['name']}"], c["clipped"] or c["published"], c["tags"]))
    for g in tags._episode_groups(FIXTURE):
        main = tags._group_item(FIXTURE, g, idx)
        items.append(_it(f"g:{main['path']}", g["type"], [f[len("Spark/"):] for f in g["files"]], main["date"]))
    return text, items


def test_fixture_matches_real_pipeline(swift, tmp_path):
    """手机这套规则在样例库上算出的 cloud 和 items_for，跟电脑 tags.cloud / items_for 直接读样例库的结果一样"""
    text, items = _fixture_cases()
    queries = ["教育", "DIY", "动手做", "Lab Notebook", "实验记录", "不存在的标签", " 教育 "]
    cases = [{"index": text, "items": items, "min": m, "queries": queries} for m in (1, 3, 5)]
    got = _compare(swift, cases, str(tmp_path))
    real = tags.cloud(3, FIXTURE)
    assert got[1]["cloud"] == real and len(real) >= 25 and {x["kind"] for x in real} == {"tech", "org", "biz"}
    by_path = {it["path"]: it["ref"] for it in (_py_items(items, tags.load(FIXTURE)))}
    for q in ("教育", "DIY"):
        assert got[0]["items"][q] == [by_path[it["path"]] for it in tags.items_for(q, FIXTURE)]
    assert got[0]["items"]["不存在的标签"] == []


# ---------------------------------------------------------------- 构造的条目：边界

INDEX = json.dumps({
    "version": 1,
    "aliases": {"NVIDIA": "英伟达", "Nvidia": "NVIDIA", "环A": "环B", "环B": "环A", "自指": "自指", "空": " ", " 空格 ": "带空格",
                "数字": 5, "列表": ["x"]},
    "kinds": {"英伟达": "org", "b": "biz", "a": "tech", "错": "food", "中": "biz", "数": 3, "环A": "org"},
    "docs": {
        "节目/notes/a.md": ["NVIDIA", "英伟达", "Nvidia", "b"],      # 同一期三种写法指向同一个：只算一次
        "节目/speech/b.md": ["a", "b", "中"],
        "节目/transcripts/b.md": ["只在文字记录下"],                  # 主文件是 speech，这个键不管用
        "节目/transcripts/c.md": ["a", "b"],                          # 主文件（只有 transcripts）
        "会议/speech/d.md": ["环A", "环B", "自指", " 空格 ", "空"],
        "会议/speech/e.md": ["a", 3],                                 # 有非字符串：整项丢掉
        "会议/speech/f.md": "不是列表",
        "信息跟进/周刊/g.md": ["  a  ", "b", "Z", "z", "中", "ä"],
        "节目/transcripts/h.md": ["b"],                               # 主文件换了：只有同一期别的文件下有键
        "没有对应条目.md": ["a"],
    },
}, ensure_ascii=False)

ITEMS = [
    _it("a", "episode", ["节目/notes/a.md", "节目/speech/a.md", "节目/transcripts/a.md"], "2026-11-01"),
    _it("b", "episode", ["节目/transcripts/b.md", "节目/speech/b.md"], "2026-11-03"),
    _it("c", "episode", ["节目/transcripts/c.md"], "2026-11-03"),
    _it("d", "talk", ["会议/speech/d.md"], "2026-10-01"),
    _it("e", "talk", ["会议/speech/e.md"], "2026-10-02"),
    _it("f", "talk", ["会议/speech/f.md"], "2026-10-02"),
    _it("g", "track", ["信息跟进/周刊/g.md"], "2026-11-20"),
    _it("h", "episode", ["节目/transcripts/h.md", "节目/speech/h.md"], "2026-11-03"),    # 键在 transcripts 下，主文件是 speech
    _it("i", "track", ["信息跟进/周刊/没有键.md"], "2026-11-21"),
    _it("p1", "clip", ["剪藏/p1.md"], "2026-11-05", ["NVIDIA", "Nvidia", "英伟达", "a", "  ", ""]),
    _it("p2", "paper", ["论文/p2.md"], "2026-11-05", ["b", " b ", "Nvidia", "a"]),
    _it("p3", "clip", ["剪藏/p3.md"], "2026-11-06", ["a", "b", "中", "环A"]),
    _it("p4", "clip", ["剪藏/p4.md"], "", ["a"]),
    _it("p5", "clip", ["剪藏/p5.md"], "2026-11-05", ["b"]),
]
QUERIES = ["a", "b", "中", "英伟达", "NVIDIA", "Nvidia", "环A", "环B", "自指", "带空格", " 空格 ", "z", "Z", "ä", "只在文字记录下", "没有", ""]


def test_constructed_items_and_edge_cases(swift, tmp_path):
    cases = [{"index": INDEX, "items": ITEMS, "min": m, "queries": QUERIES} for m in (1, 2, 3, 100)]
    got = _compare(swift, cases, str(tmp_path))
    tag_lists = dict(zip((i["ref"] for i in ITEMS), got[0]["tags"]))
    assert tag_lists["a"] == ["英伟达", "b"]                  # 三种写法合成一个
    assert tag_lists["p1"] == ["英伟达", "a"]
    assert tag_lists["h"] == ["b"]                            # 同一期别的文件下的键
    assert tag_lists["b"] == ["a", "b", "中"] and tag_lists["e"] == [] and tag_lists["f"] == []
    assert tag_lists["i"] == []
    c1 = {x["tag"]: x for x in got[0]["cloud"]}
    assert c1["英伟达"]["kind"] == "org" and c1["b"]["kind"] == "biz" and c1["a"]["kind"] == "tech"
    assert c1["中"]["kind"] == "biz" and c1["z"]["kind"] == "tech"
    assert got[3]["cloud"] == []
    # 同数的按标签字符串（码位）升序：大写在小写前，汉字在字母后
    order = [x["tag"] for x in got[0]["cloud"] if x["count"] == 1]
    assert order == sorted(order)


# ---------------------------------------------------------------- 索引文件各种坏法

BAD_INDEXES = [
    None,
    "",
    "不是 JSON",
    "[]",
    '"x"',
    "null",
    '{"version": 2, "aliases": {"a": "b"}}',
    '{"version": "1", "aliases": {"a": "b"}}',
    '{"version": true, "aliases": {"a": "b"}}',
    '{"version": 1.0, "aliases": {"a": "b"}}',
    '{"version": null, "aliases": {"a": "b"}}',
    '{"aliases": {"a": "b"}}',                                                   # 没写 version：照读
    '{"version": 1, "aliases": [], "kinds": "x", "docs": 5}',                    # 三项类型都不对：当空
    '{"version": 1, "aliases": {"a": "b", "c": 1, "d": null}, "kinds": {"b": "org", "c": "bad", "d": ["org"]}, '
    '"docs": {"x/notes/a.md": ["b", "c"], "x/notes/b.md": [1], "x/notes/c.md": "b", "x/notes/d.md": [null]}}',
    "﻿" + '{"version": 1, "aliases": {"a": "b"}}',                         # 带 BOM 的算坏文件
    '{"version": 1, "aliases": {"a": "b"}',                                      # 少个括号
    '{"version": 1, "kinds": {"b": "biz", "c": "org", "d": "tech", "e": "TECH"}}',
]
BAD_ITEMS = [
    _it("n1", "episode", ["x/notes/a.md"], "2026-11-01"),
    _it("n2", "episode", ["x/notes/b.md"], "2026-11-02"),
    _it("n3", "episode", ["x/notes/c.md"], "2026-11-03"),
    _it("n4", "episode", ["x/notes/d.md"], "2026-11-04"),
    _it("k1", "clip", ["剪藏/k1.md"], "2026-11-05", ["a", "b"]),
    _it("k2", "clip", ["剪藏/k2.md"], "2026-11-06", ["c", "d", "e"]),
]


@pytest.mark.parametrize("text", BAD_INDEXES, ids=lambda t: repr(t)[:40])
def test_bad_index_files(swift, tmp_path, text):
    cases = [{"index": text, "items": BAD_ITEMS, "min": 1, "queries": ["a", "b", "c", "d", "e"]}]
    _compare(swift, cases, str(tmp_path))


def test_bad_index_cases_cover_both_outcomes(swift, tmp_path):
    cases = [{"index": t, "items": BAD_ITEMS, "min": 1, "queries": ["b"]} for t in BAD_INDEXES]
    got = _compare(swift, cases, str(tmp_path))
    k1 = [g["tags"][4] for g in got]                                       # 剪藏 k1 文件头里是 a、b；a 有别名时只剩 b
    pick = lambda text: k1[BAD_INDEXES.index(text)]
    assert pick(None) == pick("不是 JSON") == pick('{"version": 2, "aliases": {"a": "b"}}') == ["a", "b"]   # 当空索引
    assert pick('{"version": 1.0, "aliases": {"a": "b"}}') == pick("\ufeff" + '{"version": 1, "aliases": {"a": "b"}}') == ["a", "b"]
    assert pick('{"aliases": {"a": "b"}}') == ["b"]                      # 没写 version 照读
    assert got[13]["tags"][:4] == [["b", "c"], [], [], []]                    # 类型不对的那几项丢掉，别的照用


# ---------------------------------------------------------------- 各种类型、空输入

def test_all_types_and_empty_input(swift, tmp_path):
    items = [_it(f"t{i}", t, [f"x/{t}/a{i}.md"], "2026-11-0%d" % (i + 1), ["同一个"])
             for i, t in enumerate(["clip", "paper", "clip"])]
    items += [_it("e1", "episode", ["s/notes/e.md"], "2026-11-09"), _it("e2", "talk", ["s/speech/e.md"], "2026-11-08"),
              _it("e3", "track", ["信息跟进/q/e.md"], "2026-11-07")]
    idx = json.dumps({"version": 1, "docs": {"s/notes/e.md": ["同一个"], "s/speech/e.md": ["同一个"], "信息跟进/q/e.md": ["同一个"]}},
                     ensure_ascii=False)
    cases = [{"index": idx, "items": items, "min": 3, "queries": ["同一个"]},
             {"index": idx, "items": [], "min": 1, "queries": ["同一个"]}]
    got = _compare(swift, cases, str(tmp_path))
    assert got[0]["cloud"] == [{"tag": "同一个", "count": 6, "kind": "tech"}]
    assert got[0]["items"]["同一个"] == ["e1", "e2", "e3", "t2", "t1", "t0"]    # 新到旧
    assert got[1]["cloud"] == []


def test_source_folder_rules_same_as_phone(swift, tmp_path):
    """来源文件夹自带标签（索引里的 sources）：手机和电脑对同一份输入给出的标签、篇数、顺序相同。"""
    index = json.dumps({"version": 1, "aliases": {"A16Z": "a16z", "机构": "a16z"},
                        "kinds": {"a16z": "org", "OCP": "org"},
                        "docs": {"信息跟进/a16z/x.md": ["风险投资", "a16z"], "大会/分会/speech/1.md": ["液冷"],
                                 "a16z - Videos/speech/v.md": ["机构"]},
                        "sources": {"信息跟进/a16z": "A16Z", "a16z - Videos": "a16z", "大会": "OCP", "大会/分会": "冷却分会",
                                    "大": "不该中", "": "空键", "坏值": 3, "剪藏": "不该中"}}, ensure_ascii=False)
    items = [_it("t1", "track", ["信息跟进/a16z/x.md"], "2026-01-03"),
             _it("t2", "track", ["信息跟进/a16z/notes/y.md", "信息跟进/a16z/speech/y.md"], "2026-01-02"),
             _it("e1", "episode", ["a16z - Videos/speech/v.md", "a16z - Videos/notes/v.md"], "2026-01-05"),
             _it("k1", "talk", ["大会/分会/speech/1.md"], "2026-01-01"),
             _it("k2", "talk", ["大会2/speech/1.md"], "2026-01-01"),
             _it("k3", "talk", ["大会/主会场.md"], "2026-01-04"),
             _it("c1", "clip", ["剪藏/c.md"], "2026-01-06", header=["A16Z"])]
    cases = [{"index": index, "items": items, "min": m, "queries": ["a16z", "OCP", "冷却分会", "不该中", "机构"]} for m in (1, 2)]
    got = _compare(swift, cases, str(tmp_path))
    assert got[0]["items"]["a16z"] == ["c1", "e1", "t1", "t2"] and got[0]["items"]["OCP"] == ["k3", "k1"]
    assert got[0]["items"]["不该中"] == []
