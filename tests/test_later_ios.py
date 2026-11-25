"""稍后再读、占位剪藏的写法：电脑上 core/annotations.py（later_value、is_later、later_at、with_later、is_stub）、
core/clip_compose.py（stub_title、stub_source、STUB_NOTE）、core/clip_capture._keep_flags，手机上 apps/mobile-ios/Spark/Later.swift。
同一张用例表两边都跑（Swift 用 swiftc 单独编出来，只依赖 Foundation），同一输入两边输出逐字相同。
再核对：手机上现有的改文件头的函数（DocEdit.starring、retitled、withSummary，Jottings.updated 等，在 DocEdits.swift）
都不动 later 行——这几个函数依赖界面库，编不出来，用源码检查加上和 LaterRule 一起的写法核对。"""

import json
import os
import re
import shutil
import subprocess

import pytest

from core import annotations as an
from core import clip_capture, clip_compose

ROOT = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios")
SRC = os.path.join(ROOT, "Spark", "Later.swift")

T1 = "2026-11-24T10:00:00+00:00"
T2 = "2026-11-24T11:30:00+08:00"
T3 = "2026-11-20T08:05:31-07:00"

# 共用用例表：每一项是一个文件的全文
TEXTS = [
    # 没有文件头
    "# 整理稿\n\n正文\n",
    "",
    "\n\n# 前面有空行\n",
    "正文里写了 later: 2026-11-24T10:00:00+00:00 不算\n",
    # 有文件头，没有 later
    '---\ntitle: "a"\nurl: "https://x"\n---\n\n# a\n',
    "---\nstarred: true\n---\n\n# x\n",
    "---\npinned: true\n---\n\n# x\n",
    '---\ntitle: "a"\nstarred: true\npinned: true\n---\n\n# a\n',
    '---\ntitle: "a"\npinned: true\nstarred: true\n---\n\n# a\n',
    # 已在稍后再读里：带时刻、带引号、单引号、true、false、空
    f'---\ntitle: "a"\nlater: {T1}\n---\n\n# a\n',
    f'---\ntitle: "a"\nlater: "{T1}"\n---\n\n# a\n',
    f"---\ntitle: 'a'\nlater: '{T2}'\n---\n\n# a\n",
    f"---\nlater:   {T3}  \n---\n\n# a\n",
    "---\nlater: true\n---\n\n# a\n",
    "---\nlater: True\n---\n\n# a\n",
    "---\nlater: false\n---\n\n# a\n",
    "---\nlater: FALSE\n---\n\n# a\n",
    '---\nlater: "false"\n---\n\n# a\n',
    "---\nlater:\n---\n\n# a\n",
    '---\nlater: ""\n---\n\n# a\n',
    # 和星标、置顶一起
    f'---\ntitle: "a"\nstarred: true\nlater: {T1}\npinned: true\n---\n\n# a\n',
    f"---\nstarred: true\nlater: {T1}\n---\n\n# x\n",
    # 脏数据：两行 later（前一行是 false、前一行是时刻、两行都有时刻）
    f"---\nlater: false\nlater: {T1}\n---\n\n# x\n",
    f"---\nlater: {T1}\nlater: {T2}\n---\n\n# x\n",
    f"---\nlater: \nlater: true\n---\n\n# x\n",
    # 文件头里别的键长得像 later
    f"---\nlater_at: {T1}\ntitle: x\n---\n\n# x\n",
    f"---\n  later: {T1}\n---\n\n# x\n",
    # 文件头不完整、特别写法
    f"---\nlater: {T1}\n\n# 没有结尾\n",
    f"---\nlater: {T1}\n---",
    f"---\nlater: {T1}\n---  \n\n# 结尾后面有空格\n",
    "---\n---\n\n# 空的文件头\n",
    "---\n\n---\n\n# 文件头里只有一个空行\n",
    f"---\r\nlater: {T1}\r\n---\r\n\r\n# 换行是 CRLF\r\n",
    '---\ntitle: "中文 标题"\nsource: "公众号"\nstub: true\n---\n\n# 占位\n',
    f'---\ntitle: "x"\nstub: true\nlater: {T1}\n---\n\n# 占位\n',
    '---\ntitle: "x"\nstub: false\n---\n\n# x\n',
    '---\ntitle: "x"\nstub:   true   \n---\n\n# x\n',
    "---\ntitle: x\n---\nstub: true\n",
    "stub: true\n---\ntitle: x\n---\n",
]

URLS = [
    "https://example.com/blog/post",
    "https://www.example.com/blog/post-1/?utm=x",
    "https://WWW.Example.COM/",
    "https://example.com",
    "https://example.com/a//",
    "https://example.com/中文/路径?x=1#frag",
    "https://example.com/%E4%B8%AD%E6%96%87/%2Fa",
    "http://example.com:8080/x/y.html",
    "https://user@example.com/p",
    "https://" + "a" * 150 + ".com/" + "b" * 100,
    "mailto:a@b.com",
]

# (旧文件, 补抓后新渲染的文件) → _keep_flags
KEEP_CASES = [
    (f'---\ntitle: "旧"\nstub: true\nstarred: true\nlater: {T1}\npinned: true\n---\n\n# 旧\n',
     '---\ntitle: "新"\nsource: "s"\nurl: "u"\nclipped: 2026-11-24\n---\n\n# 新\n'),
    (f'---\ntitle: "旧"\nlater: {T1}\n---\n\n# 旧\n', '---\ntitle: "新"\nstarred: true\n---\n\n# 新\n'),
    (f'---\ntitle: "旧"\npinned: true\nstarred: true\n---\n\n# 旧\n',
     f'---\ntitle: "新"\nlater: {T2}\npinned: true\n---\n\n# 新\n'),
    # 论文的作者、arXiv 编号、PDF 原件几行也跟着留下（新渲染的文件头里没有）
    (f'---\ntitle: "旧"\nstub: true\nauthors: ["A", "B"]\narxiv: "1706.03762"\npdf: "attachments/0123456789abcdef.pdf"\nlater: {T1}\n---\n\n# 旧\n',
     '---\ntitle: "新"\nsource: "s"\narxiv: "9999.99999"\n---\n\n# 新\n'),
    # 旧文件没有论文那几行、新渲染的有：留着新的；旧文件只有其中一行：只盖那一行
    (f'---\ntitle: "旧"\nstub: true\nlater: {T1}\n---\n\n# 旧\n',
     '---\ntitle: "新"\nauthors: ["N"]\narxiv: "1706.03762"\npdf: "attachments/ffffffffffffffff.pdf"\n---\n\n# 新\n'),
    ('---\ntitle: "旧"\narxiv: "1111.11111"\nstarred: true\n---\n\n# 旧\n',
     '---\ntitle: "新"\nauthors: ["N"]\narxiv: "2222.22222"\npinned: true\n---\n\n# 新\n'),
    ("# 旧没有文件头\n", '---\ntitle: "新"\n---\n\n# 新\n'),
    (f'---\ntitle: "旧"\nlater: {T1}\n---\n\n# 旧\n', "# 新没有文件头\n"),
    (f'---\nstarred: true\n---\n\n# 旧\n', "---\n---\n\n# 新的文件头是空的\n"),
    (f'---\nlater: {T1}\n---\n\n# 旧\n', "---\nstarred: true\n---\n"),
    ("---\ntitle: a\n---\n", "---\nlater: false\n---\n\n正文\n"),
]

# 稍后再读列表的先后：(名字, later 的值, 文件修改时间)
ORDER_ROWS = [
    ("a", T1, 100.0), ("b", T2, 50.0), ("c", T3, 900.0), ("d", "true", 800.0), ("e", "true", 700.0),
    ("f", '"2026-11-24T10:00:00+00:00"', 10.0), ("g", "不是时刻", 1000.0), ("h", "2026-11-24", 5.0),
    ("i", "2099-01-01T00:00:00+00:00", 3.0), ("j", "2026-11-24T10:00:00", 4.0), ("k", "2026-11-24 10:00:00Z", 1.0),
    ("l", "2026-02-30T10:00:00+00:00", 2.0), ("m", "2026-11-24T25:00:00+00:00", 6.0), ("n", "1980-01-01T00:00:00+00:00", 7.0),
]

MAIN = r'''
import Foundation
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
let texts = input["texts"] as! [String]
let v1 = input["v1"] as! String
out["value"] = texts.map { LaterRule.value($0) }
out["is"] = texts.map { LaterRule.isLater($0) }
out["at"] = texts.map { t -> Any in LaterRule.at(t).map { $0 as Any } ?? NSNull() }
out["on"] = texts.map { LaterRule.with($0, on: true, value: v1) }
out["off"] = texts.map { LaterRule.with($0, on: false, value: "") }
out["twice"] = texts.map { LaterRule.with(LaterRule.with($0, on: true, value: v1), on: true, value: "2030-01-01T00:00:00+00:00") }
out["roundtrip"] = texts.map { LaterRule.with(LaterRule.with($0, on: true, value: v1), on: false, value: "") }
out["stub"] = texts.map { LaterRule.isStub($0) }
out["addStub"] = texts.map { LaterRule.addingStub($0) }
let urls = input["urls"] as! [String]
out["stubTitle"] = urls.map { LaterRule.stubTitle($0) }
out["stubSource"] = urls.map { LaterRule.stubSource($0) }
out["keep"] = (input["keep"] as! [[String]]).map { LaterRule.keepingFlags(old: $0[0], new: $0[1]) }
let rows = input["rows"] as! [[Any]]
let ranked = rows.map { r -> (String, Double?, Double) in
    let name = r[0] as! String
    return (name, LaterRule.at("---\nlater: \(r[1] as! String)\n---\n"), (r[2] as! NSNumber).doubleValue)
}.sorted { LaterRule.order((at: $0.1, modified: $0.2), (at: $1.1, modified: $1.2)) }
out["order"] = ranked.map { $0.0 }
out["note"] = LaterRule.stubNote
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
'''


def _py_at(text):
    w = an.later_at(text)
    return None if w is None else w.timestamp()


def _py_add_stub(text):
    """电脑上占位剪藏文件头多这一行的写法（clip_compose.render_stub 里那两行）：文件头最后加一行 stub: true。"""
    m = an._FRONT_RE.match(text)
    if not m:
        return text
    head = [ln for ln in m.group(1).split("\n") if ln.strip() and not ln.startswith("stub:")] + ["stub: true"]
    return "---\n" + "\n".join(head) + "\n---\n" + text[m.end():]


def _py_order(rows):
    def key(r):
        ts = _py_at(f"---\nlater: {r[1]}\n---\n")
        return (1, ts, r[2]) if ts is not None else (0, 0.0, r[2])
    return [r[0] for r in sorted(rows, key=key, reverse=True)]


@pytest.fixture(scope="module")
def swift(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("later_ios")
    main = d / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    payload = {"texts": TEXTS, "v1": T1, "urls": URLS, "keep": [list(c) for c in KEEP_CASES], "rows": [list(r) for r in ORDER_ROWS]}
    run = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _same(got, want, what):
    bad = [f"{what}[{i}] {TEXTS[i]!r}：手机 {g!r}，电脑 {w!r}" for i, (g, w) in enumerate(zip(got, want)) if g != w]
    assert not bad, "\n".join(bad)


def test_认出来的值和是否在稍后再读里一样(swift):
    _same(swift["value"], [an.later_value(t) for t in TEXTS], "value")
    _same(swift["is"], [an.is_later(t) for t in TEXTS], "is")


def test_加入的时刻一样(swift):
    got, want = swift["at"], [_py_at(t) for t in TEXTS]
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        assert (g is None) == (w is None) and (g is None or abs(g - w) < 1e-6), f"at[{i}] {TEXTS[i]!r}：手机 {g}，电脑 {w}"


def test_加上后的文件一样(swift):
    _same(swift["on"], [an.with_later(t, True, T1) for t in TEXTS], "on")


def test_移出后的文件一样(swift):
    _same(swift["off"], [an.with_later(t, False) for t in TEXTS], "off")


def test_重复加不变_加上再移出(swift):
    _same(swift["twice"], [an.with_later(an.with_later(t, True, T1), True, "2030-01-01T00:00:00+00:00") for t in TEXTS], "twice")
    _same(swift["roundtrip"], [an.with_later(an.with_later(t, True, T1), False) for t in TEXTS], "roundtrip")


def test_占位剪藏的认法和文件头写法一样(swift):
    _same(swift["stub"], [an.is_stub(t) for t in TEXTS], "stub")
    _same(swift["addStub"], [_py_add_stub(t) for t in TEXTS], "addStub")


def test_占位剪藏的标题和来源一样(swift):
    assert swift["stubTitle"] == [clip_compose.stub_title(u) for u in URLS]
    assert swift["stubSource"] == [clip_compose.stub_source(u) for u in URLS]
    assert swift["note"] == clip_compose.STUB_NOTE


def test_补抓后保留的几行一样(swift):
    assert swift["keep"] == [clip_capture._keep_flags(o, n) for o, n in KEEP_CASES]
    # 论文那三行：旧文件没有时留新渲染的；旧文件有哪一行只盖哪一行
    keep_new = clip_capture._keep_flags(*KEEP_CASES[4])
    assert 'arxiv: "1706.03762"' in keep_new and 'authors: ["N"]' in keep_new and "attachments/ffffffffffffffff.pdf" in keep_new
    one = clip_capture._keep_flags(*KEEP_CASES[5]).split("\n---\n")[0].split("\n")
    assert one == ["---", 'title: "新"', 'authors: ["N"]', 'arxiv: "1111.11111"', "starred: true"]


def test_稍后再读列表的先后一样(swift):
    assert swift["order"] == _py_order(ORDER_ROWS)
    # 带时刻的在前（按加入时刻），later: true 和认不出时刻的在后（按文件修改时间）
    assert swift["order"] == ["a", "f", "k", "b", "c", "g", "d", "e", "n", "m", "h", "j", "i", "l"]


def test_用例表里有这几类(swift):
    # 有无文件头、和星标 / 置顶的组合、重复加、移出后文件头为空、两行 later、later: true、带引号、false
    assert any(not t.startswith("---") for t in TEXTS) and any(t.startswith("---") for t in TEXTS)
    assert an.with_later(an.with_later("# x\n", True, T1), False) == "# x\n"
    assert any(t.count("later:") >= 2 for t in TEXTS)
    assert any("later: true" in t for t in TEXTS) and any('later: "' in t for t in TEXTS) and any("later: false" in t for t in TEXTS)
    assert any("starred" in t and "pinned" in t and "later" in t for t in TEXTS)


# ---------------------------------------------------------------- 手机上现有的改文件头的函数都留着 later 行

def _doc_edits():
    return open(os.path.join(ROOT, "Spark", "DocEdits.swift"), encoding="utf-8").read()


def test_改文件头的函数不动_later_行():
    """DocEdits.swift 里改文件头的函数：starring 只去掉 starred: 行，setting 只动自己的键，retitled 只改 title，
    withSummary 只去掉 tags:、tldr:，retagged 走 setting。没有哪个按「整个文件头重写」或按位置删行。
    另有 tests/test_later_ios.py::test_改文件头的函数在假文件头上留着_later_行 用 swiftc 把这几个函数的核心逻辑编出来实测。"""
    src = _doc_edits()
    assert 'hasPrefix("starred:")' in src and 'hasPrefix("\\(key):")' in src
    assert '!$0.hasPrefix("tags:") && !$0.hasPrefix("tldr:")' in src
    jot = open(os.path.join(ROOT, "Spark", "Jottings.swift"), encoding="utf-8").read()
    assert 'head = setKey(head, "title"' in jot and 'head = setKey(head, "updated_at"' in jot


def test_改文件头的函数在假文件头上留着_later_行(tmp_path):
    """把 DocEdits.swift 里 DocEdit 枚举里和文件头有关的函数（retitled、starring、flagged、setting、retagged、withSummary）
    原样抽出来，补上它们依赖的 Clips.yaml、Clips.Summary 的最小实现，用 swiftc 编出来，在带 later 的文件头上逐个跑，
    再用电脑的 with_later 判一遍：later 行还在、位置不变。"""
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    src = _doc_edits()

    def fn(name, nxt):
        a = src.index(name)
        return src[a:src.index(nxt, a)]
    body = "\n".join([
        fn("    static func retitled", "    /// 加星标、取消星标"),
        fn("    static func starring", "    /// 文件头里有没有 starred: true"),
        fn("    static func flagged", "    /// 改文件头里的一项"),
        fn("    static func setting", "    /// 移到别的主题"),
        fn("    static func retagged", "    /// 删掉一张图"),
        fn("    static func withSummary", "    /// 复盘末尾接一段追问"),
    ])
    shim = r'''
import Foundation
enum Clips {
    struct Summary { var tldr: String; var tags: [String]; var body: String; var title = "" }
    static func yaml(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: s, options: [.fragmentsAllowed, .withoutEscapingSlashes])) ?? Data("\"\"".utf8)
        return String(decoding: data, as: UTF8.self)
    }
}
enum DocEdit {
''' + body + r'''
}
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String]
var out: [[String]] = []
for t in input {
    out.append([
        DocEdit.retitled(t, "新标题"),
        DocEdit.starring(t, true), DocEdit.starring(t, false),
        DocEdit.flagged(t, "pinned", true), DocEdit.flagged(t, "pinned", false),
        DocEdit.setting(t, "note", "备注"),
        DocEdit.retagged(t, ["甲", "乙"]),
        DocEdit.withSummary(t, Clips.Summary(tldr: "一句话", tags: ["丙"], body: "要点")),
    ])
}
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
'''
    (tmp_path / "main.swift").write_text(shim, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), str(tmp_path / "main.swift")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    heads = [
        f'---\ntitle: "a"\nsource: "s"\nstarred: true\nlater: {T1}\npinned: true\n---\n\n# a\n\n## 摘要\n\nx\n\n## 正文\n\n正文\n',
        f'---\ntitle: "a"\nlater: {T1}\n---\n\n# a\n\n## 正文\n\n正文\n',
        f'---\ntitle: "a"\ntags: ["x"]\ntldr: "y"\nlater: {T1}\nstub: true\n---\n\n# a\n\n## 正文\n\n正文\n',
        f'---\nlater: true\n---\n\n# a\n',
    ]
    run = subprocess.run([str(exe)], input=json.dumps(heads), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    outs = json.loads(run.stdout)
    for text, results in zip(heads, outs):
        want = an.later_value(text)
        assert want
        for name, got in zip(["retitled", "star on", "star off", "pinned on", "pinned off", "note", "retagged", "withSummary"], results):
            assert an.later_value(got) == want, f"{name} 把 later 行改了：{got!r}"
            assert got.count("later:") == 1, f"{name}：later 行不是一行：{got!r}"
