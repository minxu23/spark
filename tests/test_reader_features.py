"""手机阅读页的功能开关（apps/mobile-ios/Spark/ReaderFeatures.swift）和功能表（apps/mobile-ios/阅读功能表.md）逐格一致。
用 swiftc 把开关文件单独编出来，每个种类打印一行开关，和表比对：加功能、改开关必须同时改表。"""

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios")
SRC = os.path.join(ROOT, "Spark", "ReaderFeatures.swift")
PLACE = os.path.join(ROOT, "Spark", "ReaderPlace.swift")
RULES = os.path.join(ROOT, "Spark", "PathRules.swift")
TABLE = os.path.join(ROOT, "阅读功能表.md")

MAIN = r'''
print((["种类"] + ReaderFeatures.columns.map { $0.0 } + ["链接叫法"]).joined(separator: "|"))
for k in ReaderKind.allCases {
    let f = ReaderFeatures.of(k)
    print(([k.label] + ReaderFeatures.columns.map { $0.1(f) } + [ReaderFeatures.linkCell(k)]).joined(separator: "|"))
}
'''


def _table():
    rows = []
    with open(TABLE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|") or line.startswith("|---"):
                continue
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("reader_features")
    main = d / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout
    return [ln.split("|") for ln in out.strip().split("\n")]


def test_表头和开关一致(probe):
    assert _table()[0] == probe[0]


def test_功能表逐格一致(probe):
    table = {r[0]: r for r in _table()[1:]}
    got = {r[0]: r for r in probe[1:]}
    assert sorted(table) == sorted(got), "种类不一致"
    header = probe[0]
    diffs = [f"{kind} / {header[i]}：表里写「{table[kind][i]}」，代码是「{v}」"
             for kind, row in got.items() for i, v in enumerate(row) if i < len(table[kind]) and table[kind][i] != v]
    assert not diffs, "\n".join(diffs)
    assert all(len(table[k]) == len(header) for k in table), "有的行格子数不对"


def test_没有的都写了原因(probe):
    # 每个有「没有」的列，表下面都要有这一列的原因
    text = open(TABLE, encoding="utf-8").read()
    reasons = text.split("## 「没有」的原因", 1)[1]
    header = probe[0]
    for i, col in enumerate(header[1:-1], start=1):
        if any(r[i] == "没有" for r in probe[1:]):
            assert f"\n{col}\n" in reasons, f"「{col}」列有「没有」，表下面没写原因"


def test_阅读视图不直接看路径():
    # 路径判断只在 ReaderPlace 里调；阅读外壳和两个入口不直接调
    for name in ("ReaderShell.swift", "ReaderView.swift"):
        src = open(os.path.join(ROOT, "Spark", name), encoding="utf-8").read()
        for fn in ("Clips.isClip", "DocEdit.editable", "DocEdit.annotatable", "Jottings.isJotting", "Vault.canDelete"):
            assert fn not in src, f"{name} 里直接调了 {fn}"


# ---------------------------------------------------------------- 路径 → 种类 → 收窄后的开关
# 期望值按重构前（提交 8484fd9 的 ReaderView.swift）两个外壳的行为写：
#   文档（DocReaderView）：做笔记 = annotatable；星标 = isClip || annotatable；改名称 = editable；继续追问 = editable && !isClip；
#     删除 = canDelete；语言切换、翻译、重写摘要、图片、剪藏整理、原网页 = isClip；编辑随手记 = isJotting；没进电脑提示总有。
#   单集（ReaderView）：做笔记 = 文件读到了 && annotatable；星标、原文链接每个页签都有（信息里叫原网页）；
#     小结页签读到之前显示处理记录；整理稿有语言切换（原来在阅读设置里）和未译段数提示；要下载的稿子有流量确认；文字记录有说明条。
# 只有允许的变化 b（单集各页签也显示「手机这版没进电脑」提示）在下面单独加上（B）。

CLIP_ALL = "做笔记,星标,改名称,删除,语言切换,翻译成中文,重写摘要,没进电脑提示,图片重下和删图,剪藏整理,原文链接"
CLIP_NO_NOTES = "星标,改名称,删除,语言切换,翻译成中文,重写摘要,没进电脑提示,剪藏整理,原文链接"   # 图片操作挂在笔记上，原来也没有
PLAIN = "做笔记,星标,没进电脑提示"
B = ",没进电脑提示"   # 允许的变化 b

DOC_CASES = [
    ("Spark/剪藏/20261101_文章.md", "剪藏", CLIP_ALL, "原网页"),
    ("Spark/剪藏/.隐藏.md", "剪藏", CLIP_NO_NOTES, "原网页"),
    ("Spark/剪藏/摘录.md", "剪藏", CLIP_NO_NOTES, "原网页"),
    ("Spark/剪藏/索引.md", "总览页", "没进电脑提示", "—"),
    ("Spark/剪藏/子文件夹/x.md", "会议资料", PLAIN, "—"),
    ("Spark/笔记/20261101 想法.md", "随手记", "做笔记,星标,删除,编辑随手记,没进电脑提示", "—"),
    ("Spark/笔记/索引.md", "随手记", "删除,编辑随手记,没进电脑提示", "—"),
    ("Spark/笔记/摘录.md", "随手记", "删除,编辑随手记,没进电脑提示", "—"),
    ("Spark/笔记/笔记.md", "随手记", "删除,编辑随手记,没进电脑提示", "—"),
    ("output/复盘/复盘_阅读_20261026-20261102.md", "复盘", "做笔记,星标,改名称,删除,继续追问,没进电脑提示", "—"),
    ("output/复盘/旧的/复盘_阅读_20261001-20261008.md", "复盘", "做笔记,星标,改名称,继续追问,没进电脑提示", "—"),
    ("output/复盘/索引.md", "复盘", "改名称,删除,继续追问,没进电脑提示", "—"),
    ("output/技术洞察报告_AI_20261101.md", "报告", PLAIN, "—"),
    ("output/AI/技术洞察报告_AI_20261101.md", "报告", PLAIN, "—"),
    ("Spark/信息跟进/简报/20261101.md", "简报", PLAIN, "—"),
    ("Spark/Dwarkesh/Dwarkesh.md", "总览页", "没进电脑提示", "—"),
    ("Spark/某会议/议程资料.md", "会议资料", PLAIN, "—"),
    ("Spark/剪藏/x.txt", "总览页", "没进电脑提示", "—"),
    ("output/复盘/x.pdf", "总览页", "没进电脑提示", "—"),
]

# (页签, 路径, 在信息里, 文件读到了, 种类, 开关, 链接叫法)
EP_CASES = [
    ("note", "Spark/Dwarkesh/notes/20261101 一期.md", False, False, "处理记录里的小结", "星标,处理记录小结,原文链接", "原视频"),
    ("note", "Spark/Dwarkesh/notes/20261101 一期.md", False, True, "单集小结", "做笔记,星标,小结过滤,原文链接" + B, "原视频"),
    ("note", None, False, False, "处理记录里的小结", "星标,处理记录小结,原文链接", "原视频"),
    ("speech", "Spark/Dwarkesh/speech/20261101_一期.md", False, True, "整理稿",
     "做笔记,星标,语言切换,流量下载确认,未译段数提示,原文链接" + B, "原视频"),
    ("transcript", "Spark/Dwarkesh/transcripts/20261101_一期.md", False, True, "文字记录",
     "做笔记,星标,流量下载确认,文字记录说明条,原文链接" + B, "原视频"),
    ("speech", "Spark/Dwarkesh/.speech/20261101_一期.md", False, True, "整理稿",
     "星标,语言切换,流量下载确认,未译段数提示,原文链接" + B, "原视频"),
    ("note", "Spark/信息跟进/频道/notes/20261101 一期.md", True, False, "处理记录里的小结", "星标,处理记录小结,原文链接", "原网页"),
    ("note", "Spark/信息跟进/频道/notes/20261101 一期.md", True, True, "单集小结", "做笔记,星标,小结过滤,原文链接" + B, "原网页"),
    ("speech", "Spark/信息跟进/频道/speech/20261101_一期.md", True, True, "整理稿",
     "做笔记,星标,语言切换,流量下载确认,未译段数提示,原文链接" + B, "原网页"),
    ("article", "Spark/信息跟进/博客/20261101_文章.md", True, True, "信息文章（原文）", "做笔记,星标,流量下载确认,原文链接" + B, "原网页"),
]

PLACE_MAIN = r"""
import Foundation
func line(_ p: ReaderPlace) -> String {
    let on = ReaderFeatures.columns.filter { $0.1(p.features) == "有" }.map { $0.0 }.joined(separator: ",")
    return [p.kind.label, on, p.features.link == .none ? "—" : p.features.link.label].joined(separator: "|")
}
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
out["docs"] = (input["docs"] as! [String]).map { line(ReaderPlace.doc($0)) }
out["episodes"] = (input["episodes"] as! [[Any]]).map { c -> String in
    let path = c[1] as? String
    return line(ReaderPlace.episodeTab(ReaderTab(rawValue: c[0] as! String)!, path: path, inFeed: c[2] as! Bool, fileLoaded: c[3] as! Bool))
}
out["star"] = (input["star"] as! [[Any]]).map { ReaderPlace.starPath($0.map { $0 as? String }) ?? "-" }
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
"""

STAR_CASES = [
    ([None, "Spark/X/speech/20261101_a.md", None, None], "Spark/X/speech/20261101_a.md"),          # 简讯：记在整理稿上
    (["Spark/X/notes/a.md", "Spark/X/speech/a.md", "Spark/X/transcripts/a.md", None], "Spark/X/notes/a.md"),
    (["Spark/X/.notes/a.md", "Spark/X/speech/a.md", None, None], "Spark/X/speech/a.md"),          # 不能做笔记的跳过
    ([None, None, None, None], "-"),
]


@pytest.fixture(scope="module")
def place_out(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("reader_place")
    main = d / "main.swift"
    main.write_text(PLACE_MAIN, encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), RULES, SRC, PLACE, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    payload = {"docs": [c[0] for c in DOC_CASES], "episodes": [list(c[:4]) for c in EP_CASES], "star": [c[0] for c in STAR_CASES]}
    p = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=30, check=True)
    return json.loads(p.stdout)


def _norm(on):
    return ",".join(sorted(x for x in on.split(",") if x))


def _check(cases, got, label):
    diffs = []
    for c, g in zip(cases, got):
        kind, on, link = g.split("|")
        want = (c[-3], _norm(c[-2]), c[-1])
        if (kind, _norm(on), link) != want:
            diffs.append(f"{label} {c[:4] if label == '单集' else c[0]}：期望 {want}，代码是 {(kind, _norm(on), link)}")
    assert len(got) == len(cases)
    assert not diffs, "\n".join(diffs)


def test_文档路径的种类和开关(place_out):
    _check(DOC_CASES, place_out["docs"], "文档")


def test_单集页签的种类和开关(place_out):
    _check(EP_CASES, place_out["episodes"], "单集")


def test_单集星标记在哪份稿子上(place_out):
    assert place_out["star"] == [c[1] for c in STAR_CASES]


def test_路径常量和原来的地方一致():
    # PathRules 里的文件夹常量和各处原来的常量必须一样
    rules = open(RULES, encoding="utf-8").read()

    def const(src, name):
        m = re.search(r'static let %s = "([^"]*)"' % name, src)
        assert m, name
        return m.group(1)

    def read(*p):
        return open(os.path.join(ROOT, *p), encoding="utf-8").read()

    assert const(rules, "clipFolder") == const(read("Shared", "Clipper.swift"), "folder")
    assert const(rules, "clipIndexName") == const(read("Spark", "Clips.swift"), "indexName")
    assert const(rules, "jottingFolder") == const(read("Spark", "Jottings.swift"), "folder")
    lib = read("Spark", "Library.swift")
    for a, b in (("sparkDir", "sparkDir"), ("trackDir", "trackDir"), ("reportsDir", "reportsDir"), ("reviewsDir", "reviewsDir")):
        assert const(rules, a) == const(lib, b), a


def test_路径规则只有一处():
    # Clips.isClip、Jottings.isJotting、DocEdit.annotatable / editable、Vault.canDelete 都转到 PathRules
    def read(name):
        return open(os.path.join(ROOT, "Spark", name), encoding="utf-8").read()

    assert "static func isClip(_ path: String) -> Bool { PathRules.isClip(path) }" in read("Clips.swift")
    assert "static func isJotting(_ path: String) -> Bool { PathRules.isJotting(path) }" in read("Jottings.swift")
    assert "static func editable(_ path: String) -> Bool { PathRules.editable(path) }" in read("DocEdits.swift")
    assert "static func annotatable(_ path: String) -> Bool { PathRules.annotatable(path) }" in read("DocEdits.swift")
    assert "static func canDelete(_ path: String) -> Bool { PathRules.canDelete(path) }" in read("Vault.swift")
    assert "reviewsDir" not in read("ReaderPlace.swift").replace("PathRules.reviewsDir", "")
