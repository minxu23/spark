"""划线的写法（==字==）：电脑上 core/annotations.py 的 MARK_PATTERN、add_mark，手机上 apps/mobile-ios/Spark/MarkRule.swift。
同一张用例表两边都跑（Swift 用 swiftc 单独编出来），认出来的划线、划线后的文字都要一样；
再加随机测试：对一段含「=」的文字随机划很多次，不能出现「===」，划过的字都在划线里。
网页渲染（mistune）、取消高亮、划线统计也按同一条规则。"""

import json
import os
import random
import re
import shutil
import subprocess

import pytest

from core import annotations as notes

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "MarkRule.swift")

# (文字, 认出来的划线)
PARSE_CASES = [
    ("==Gemini = 沟通==", ["Gemini = 沟通"]),
    ("==Gemini = 沟通极舒服==，后面", ["Gemini = 沟通极舒服"]),
    ("==a + b==", ["a + b"]),
    ("前面==第一处==中间==第二处==后面", ["第一处", "第二处"]),
    ("====x====", []),
    ("==x===", []),
    ("===x==", []),
    ("==x==y==", ["x"]),
    ("==划过的[^note-1]还在划==", ["划过的[^note-1]还在划"]),
    ("==划过的==[^note-2]后面", ["划过的"]),
    ("==跨\n行==", []),
    ("== 开头空格==", [" 开头空格"]),
    ("==a=b=c==", ["a=b=c"]),
    ("==a==b==", ["a"]),
    ("x == y", []),
    ("====", []),
    ("==**加粗**的==", ["**加粗**的"]),
    ("一行 ==甲== 二行\n==乙 = 丙==", ["甲", "乙 = 丙"]),
]

# (文字, 选中的字（取第一次出现）, 划线后的文字；None 是不改)
WRAP_CASES = [
    ("Gemini = 沟通极舒服", "Gemini = 沟通极舒服", "==Gemini = 沟通极舒服=="),
    ("Gemini = 沟通极舒服", "= 沟通", "Gemini = ==沟通==极舒服"),
    ("Gemini = 沟通极舒服", "Gemini =", "==Gemini== = 沟通极舒服"),
    ("Gemini = 沟通极舒服", " = ", None),
    ("Gemini = 沟通极舒服", "=", None),
    ("a + b 和 c", "a + b", "==a + b== 和 c"),
    # 已经整个划过：不再包一层
    ("前==划过的字==后", "划过的字", None),
    ("前==划过的字==后", "过的", None),
    ("前==Gemini = 沟通==后", "= 沟", None),
    # 紧挨着已有划线：并成一处，不出现「====」
    ("==甲==乙", "乙", "==甲乙=="),
    ("乙==甲==", "乙", "==乙甲=="),
    # 部分重叠：并成一处
    ("==甲乙==丙丁", "乙==丙", "==甲乙丙==丁"),
    ("==甲==中间==乙==", "甲==中间==乙", "==甲中间乙=="),
    # 包住整处已有划线
    ("前==中==后", "前==中==后", "==前中后=="),
    # 外面紧挨着一个字面的「=」：把「=」和它外面一个字包进来
    ("x=y", "y", "==x=y=="),
    ("x=y", "x", "==x=y=="),
    ("x=y", "x=y", "==x=y=="),
    ("ROI=收益", "收益", "RO==I=收益=="),
    ("a=b=c", "c", "==a=b=c=="),
    ("a=b=c 后", "a", "==a=b=c== 后"),
    # 「=」外面那个字在 UTF-16 里占两位（emoji、扩展 B 区汉字）：整个字包进来，不切开（按码位扩，同电脑）
    ("😀=收益", "收益", "==😀=收益=="),
    ("收益=😀后", "收益", "==收益=😀==后"),
    ("前𠀀=甲", "甲", "前==𠀀=甲=="),
    ("甲=𠀀", "甲", "==甲=𠀀=="),
    ("👍=a=𠀀", "a", "==👍=a=𠀀=="),
    # 扩完碰到已有划线：再并成一处
    ("==甲==x=y", "y", "==甲x=y=="),
    # 扩不了：「=」在行首、行尾，或连着两个以上
    ("=y", "y", None),
    ("x=", "x", None),
    ("x==y", "y", None),
    ("上一行\n=y", "y", None),
    ("x=\n下一行", "x", None),
    # 跨行：每行各划一处
    ("第一行\n第二行", "一行\n第二", "第==一行==\n==第二==行"),
    ("第一行\n\n第二行", "行\n\n第", "第一==行==\n\n==第==二行"),
    # 带批注号的划线后面接着划
    ("==甲==[^note-1]乙", "乙", "==甲==[^note-1]==乙=="),
    ("甲[^note-1]乙", "甲[^note-1]乙", "==甲[^note-1]乙=="),
]

# 手机和电脑结果不同、已知：「=」外面那个字是多码位的字形簇（带肤色的 emoji、带变体选择符的 emoji、组合附加符、
# ZWJ 组合 emoji、旗帜、键帽）。手机 MarkRule.wrap 按字形簇（Character）整个包进来；电脑 core/annotations.grow_selection
# 现在按码位扩，会从中间切开。电脑端以后单独改成按字形簇，改完把这几条并进 WRAP_CASES、删掉 desktop 一列。
# (文字, 选中的字, 手机结果, 电脑现在的结果)
GRAPHEME_KNOWN_DIFF = [
    ("👍🏽=a", "a", "==👍🏽=a==", "👍==🏽=a=="),
    ("❤️=a", "a", "==❤️=a==", "❤==️=a=="),
    ("e\u0301=a", "a", "==e\u0301=a==", "e==\u0301=a=="),
    ("a=👨\u200d👩\u200d👧", "a", "==a=👨\u200d👩\u200d👧==", "==a=👨==\u200d👩\u200d👧"),
    ("甲=🇨🇳", "甲", "==甲=🇨🇳==", "==甲=🇨==🇳"),
    ("1️⃣=a", "a", "==1️⃣=a==", "1️==⃣=a=="),
]

RANDOM_SEEDS = 40
RANDOM_OPS = 25


def _py_wrap(text, sel):
    i = text.find(sel)
    return notes.add_mark(text, i, i + len(sel))


def _random_ops(seed):
    rnd = random.Random(seed)
    return [[rnd.randrange(10_000), rnd.randrange(10_000)] for _ in range(RANDOM_OPS)]


def _apply_ops(text, ops):
    """按 (x, y) 对当前文字长度取模得到选区；两端都一样算，Swift 那边照这个重放。"""
    for x, y in ops:
        a, b = sorted((x % (len(text) + 1), y % (len(text) + 1)))
        got = notes.add_mark(text, a, b)
        if got is not None:
            text = got
    return text


# 随机测试的两段原文（只用 BMP 里的字，Python 的下标和 Swift 的 UTF-16 下标一致）
# SPACED：每个「=」两边都是空格（「Gemini = 沟通」这种）。字都不重复，好核对哪些字划过
SPACED = "甲乙丙 = 丁戊己庚 = 辛壬\n癸子丑 寅卯 = 辰巳午未\n申酉戌亥 = 天地"
# DENSE：「=」紧挨着字，还有现成的「==」
# 除了「=」和空白，每个字只出现一次，好核对哪些字划过
DENSE = "a=b==c 甲=乙 x==y==z 丙==丁=戊\n己=庚 =辛 壬="


def _covered_chars(text):
    return {ch for _, _, ia, ib in notes.mark_spans(text) for ch in text[ia:ib]}


@pytest.mark.parametrize("text,want", PARSE_CASES)
def test_认划线(text, want):
    assert notes.MARK_TEXT_RE.findall(text) == want


@pytest.mark.parametrize("text,sel,want", WRAP_CASES)
def test_划线(text, sel, want):
    got = _py_wrap(text, sel)
    assert got == want
    if got is not None:
        assert "===" not in got
        # 划上的字都认得出来
        assert notes.trim_selection(sel).replace("==", "") in "".join(notes.MARK_TEXT_RE.findall(got)) or "\n" in sel


def test_随机划线_不出现三个等号_划过的字都在划线里():
    for seed in range(RANDOM_SEEDS):
        rnd = random.Random(seed)
        text = SPACED
        selected = set()
        for _ in range(RANDOM_OPS):
            a, b = sorted((rnd.randrange(len(text) + 1), rnd.randrange(len(text) + 1)))
            sel = {ch for ch in text[a:b] if ch not in "= \n"}
            got = notes.add_mark(text, a, b)
            if got is not None:
                text = got
            assert "===" not in text, (seed, text)
            selected |= sel
            assert selected <= _covered_chars(text), (seed, text, selected - _covered_chars(text))
        # 去掉划线符号还是原文
        assert text.replace("==", "") == SPACED


def test_随机划线_等号紧挨着字():
    """DENSE 里「=」紧挨着字：外面紧挨一个「=」时往外扩；「=」在行首行尾、连着两个以上时扩不了，不划。
    不出现「===」；凡是划上了的，划过的字都在划线里，去掉「==」后还是原文。
    跨行的选区每行各算：扩不了的那一行不划，那一行的字不算划过。"""
    for seed in range(RANDOM_SEEDS):
        text, selected = DENSE, set()
        for x, y in _random_ops(seed):
            a, b = sorted((x % (len(text) + 1), y % (len(text) + 1)))
            got = notes.add_mark(text, a, b)
            if got is None:
                continue
            # 跨行的每行各划一处，扩不了的那一行不划：只算划上了的那几行的字
            start = a
            for line in text[a:b].split("\n"):
                end = start + len(line)
                if notes.add_mark(text, start, end) is not None:
                    selected |= {ch for ch in line if ch not in "= "}
                start = end + 1
            text = got
            assert "===" not in text, (seed, text)
            assert selected <= _covered_chars(text), (seed, text, selected - _covered_chars(text))
            assert text.replace("==", "") == DENSE.replace("==", ""), (seed, text)


def test_去掉选区两头的等号和空白():
    assert notes.trim_selection(" = 沟通 =\n") == "沟通"
    assert notes.trim_selection("==") == ""
    assert notes.trim_selection("a = b") == "a = b"


def test_网页渲染和划线统计用同一条规则():
    from apps.reader import server as reader
    for text, want in PARSE_CASES:
        html = reader._md(text)
        assert html.count("<mark>") == len(want), (text, html)
    # 读书笔记汇总、划线统计（reading_note）跟着同一条规则
    doc = "# 标题\n\n==Gemini = 沟通==，还有 ====x==== 和 ==a + b==[^note-1]。\n\n[^note-1]: 批注\n"
    n = notes.reading_note(doc)
    assert [it["mark"] for it in n["items"]] == ["Gemini = 沟通", "a + b"]
    assert n["items"][1]["notes"] == ["批注"]


MAIN = r'''
import Foundation
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
out["parse"] = (input["parse"] as! [String]).map { MarkRule.texts($0) }
out["wrap"] = (input["wrap"] as! [[String]]).map { c -> Any in
    let r = (c[0] as NSString).range(of: c[1])
    return MarkRule.wrap(c[0], r) ?? NSNull()
}
out["trim"] = (input["trim"] as! [String]).map { MarkRule.trimmed($0) }
out["grapheme"] = (input["grapheme"] as! [[String]]).map { c -> Any in
    MarkRule.wrap(c[0], (c[0] as NSString).range(of: c[1])) ?? NSNull()
}
out["random"] = (input["random"] as! [[String: Any]]).map { job -> String in
    var text = job["text"] as! String
    for op in job["ops"] as! [[Int]] {
        let n = (text as NSString).length + 1
        let a = min(op[0] % n, op[1] % n), b = max(op[0] % n, op[1] % n)
        if let got = MarkRule.wrap(text, NSRange(location: a, length: b - a)) { text = got }
    }
    return text
}
// 批注号接在划线后面的写法（DocEdit.notes、ReadingNote 用）
let noteRE = try! NSRegularExpression(pattern: MarkRule.withNotes)
let s = "==甲 = 乙==[^note-1][^note-2]，====x====[^note-3]"
out["withNotes"] = noteRE.matches(in: s, range: NSRange(location: 0, length: (s as NSString).length)).map {
    [(s as NSString).substring(with: $0.range(at: 1)), (s as NSString).substring(with: $0.range(at: 2))]
}
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''

TRIMS = [" = 沟通 =\n", "==", "a = b", "　=甲=　", ""]


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_手机和电脑一样(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    jobs = [{"text": t, "ops": _random_ops(seed)} for t in (SPACED, DENSE) for seed in range(RANDOM_SEEDS)]
    payload = {"parse": [t for t, _ in PARSE_CASES], "wrap": [[t, s] for t, s, _ in WRAP_CASES], "trim": TRIMS, "random": jobs,
               "grapheme": [[t, s] for t, s, _, _ in GRAPHEME_KNOWN_DIFF]}
    run = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    assert got["parse"] == [w for _, w in PARSE_CASES]
    assert got["parse"] == [notes.MARK_TEXT_RE.findall(t) for t, _ in PARSE_CASES], "手机和电脑数出的划线要一样"
    assert got["wrap"] == [w for _, _, w in WRAP_CASES]
    # 已知差异：手机按字形簇、电脑按码位（见 GRAPHEME_KNOWN_DIFF 上面的说明）
    assert got["grapheme"] == [p for _, _, p, _ in GRAPHEME_KNOWN_DIFF]
    assert [_py_wrap(t, s) for t, s, _, _ in GRAPHEME_KNOWN_DIFF] == [d for *_, d in GRAPHEME_KNOWN_DIFF]
    assert got["trim"] == [notes.trim_selection(t) for t in TRIMS]
    assert got["random"] == [_apply_ops(j["text"], j["ops"]) for j in jobs], "随机划线两边结果要一样"
    assert all("===" not in t for t in got["random"])
    assert got["withNotes"] == [[m.group(1), m.group(2)] for m in notes.MARK_RE.finditer("==甲 = 乙==[^note-1][^note-2]，====x====[^note-3]")]
    assert got["withNotes"] == [["甲 = 乙", "[^note-1][^note-2]"]]


# ---- 网页阅读页的高亮、取消高亮（apps/reader/server.py）按同一条规则

from tests.test_reader import NOTE, _client, _note_path, _post, _write_note, vault  # noqa: E402,F401


def test_网页_整个落在已有划线里_文件不变(vault):
    mtime = _write_note(vault, "前面 ==Gemini = 沟通极舒服== 后面。\n")
    before = _note_path(vault).read_text(encoding="utf-8")
    r = _post(_client(), "/read/api/highlight", path=NOTE, mtime=mtime, text="沟通", before="Gemini = ")
    assert r.status_code == 200, r.get_json()
    assert _note_path(vault).read_text(encoding="utf-8") == before


def test_网页_选区两头的等号去掉_紧挨等号的往外扩(vault):
    mtime = _write_note(vault, "Gemini = 沟通极舒服。ROI=收益。x==z。\n")
    c = _client()
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=mtime, text="= 沟通极舒服", before="Gemini ")
    assert r.status_code == 200, r.get_json()
    assert "Gemini = ==沟通极舒服==。" in _note_path(vault).read_text(encoding="utf-8")
    # 外面紧挨一个「=」：把「=」和它外面一个字包进来
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=r.get_json()["mtime"], text="收益", before="ROI=")
    assert r.status_code == 200, r.get_json()
    assert "RO==I=收益==。" in _note_path(vault).read_text(encoding="utf-8")
    # 连着两个「=」：扩不了，报错，文件不动
    before = _note_path(vault).read_text(encoding="utf-8")
    r = _post(c, "/read/api/highlight", path=NOTE, mtime=r.get_json()["mtime"], text="z", before="x==")
    assert r.status_code == 400 and "等号" in r.get_json()["error"]
    assert _note_path(vault).read_text(encoding="utf-8") == before
    assert "===" not in before


def test_网页_批注紧挨等号也往外扩(vault):
    mtime = _write_note(vault, "ROI=收益不错。\n")
    r = _post(_client(), "/read/api/note", path=NOTE, mtime=mtime, text="收益", before="ROI=", note="看看")
    assert r.status_code == 200, r.get_json()
    assert "RO==I=收益==[^note-1]不错" in _note_path(vault).read_text(encoding="utf-8")


def test_网页_取消含等号的划线(vault):
    mtime = _write_note(vault, "前面 ==Gemini = 沟通== 后面。\n")
    r = _post(_client(), "/read/api/highlight", path=NOTE, mtime=mtime, text="Gemini = 沟通", nth=0, remove=True)
    assert r.status_code == 200, r.get_json()
    assert "前面 Gemini = 沟通 后面" in _note_path(vault).read_text(encoding="utf-8")
