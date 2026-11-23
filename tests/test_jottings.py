"""随手记（core/jottings.py）：写法、改正文不动读后感和批注、卡片上的数字；手机 Jottings.swift 编出来跟电脑一字不差；
手机交来的存、改、删（core/remote_tasks）；阅读页的笔记页、编辑页、保存接口（apps/reader）。"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from core import annotations, doctime
from core import jottings as jt
from core import remote_tasks as rt

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT_DOCTIME = os.path.join(ROOT, "apps", "mobile-ios", "Shared", "DocTime.swift")
SWIFT_JOTTINGS = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Jottings.swift")
SWIFT_CLIPS = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift")
SWIFT_DOCEDITS = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "DocEdits.swift")
SWIFT_PENDING = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "PendingEdits.swift")

T0 = datetime(2026, 11, 22, 13, 30, 5, tzinfo=timezone.utc)        # 北京时间 21:30:05
T1 = datetime(2026, 11, 22, 13, 42, 10, tzinfo=timezone.utc)       # 21:42:10
T2 = datetime(2026, 11, 23, 1, 0, 0, tzinfo=timezone.utc)          # 11 月 23 日 09:00


@pytest.fixture
def zone():
    old = os.environ.get("TZ")

    def use(name):
        os.environ["TZ"] = name
        time.tzset()
    use("Asia/Shanghai")
    yield use
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def vault(tmp_path, monkeypatch, zone):
    v = tmp_path / "vault"
    (v / "Spark").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setattr(rt, "note_conflicts_dir", lambda: str(tmp_path / "conflicts"))
    monkeypatch.setattr(rt, "trash_dir", lambda: str(tmp_path / "Trash"))
    return v


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- 写法

def test_render_layout(zone):
    text = jt.render("", "  第一行想法\n\n第二段  \n", T0)
    assert text == ('---\ntitle: "第一行想法"\ncreated_at: "2026-11-22T21:30:05+08:00"\nupdated_at: "2026-11-22T21:30:05+08:00"\n'
                    '---\n\n# 第一行想法\n\n- 收录时间：2026年11月22日，21:30（UTC+8）\n\n  第一行想法\n\n第二段\n'), "首行缩进留着"
    assert jt.file_name(jt.title_of("", "第一行想法"), T0) == "20261122_第一行想法.md"
    tagged = jt.render("标题", "正文", T0, tags=["读书", "A:B"], starred=True)
    assert 'updated_at: "2026-11-22T21:30:05+08:00"\ntags: ["读书", "A:B"]\nstarred: true\n---\n' in tagged
    assert annotations.is_starred(tagged)
    only_title = jt.render("只有标题", "", T0)
    assert only_title.endswith("- 收录时间：2026年11月22日，21:30（UTC+8）\n")
    assert doctime.modified_line(T1) == "- 修改时间：2026年11月22日，21:42（UTC+8）"


@pytest.mark.parametrize("title,body,want", [
    ("", "# 标题行\n正文", "标题行"), ("", "> ==划线==[^note-1] 的话", "划线 的话"), ("", "\n\n  \n- 第一条\n", "第一条"),
    ("", "中" * 40, "中" * 30), ("", "", "未命名"), ("  多\n行\r\n标题 ", "x", "多 行 标题"), ("", "**粗**体", "粗体"),
])
def test_title_of(title, body, want):
    assert jt.title_of(title, body) == want


def _noted(zone_text: str) -> str:
    """一条随手记，上面划了两处线、挂了两条批注，还有一条没挂在划线上的批注和读后感。"""
    text = zone_text.replace("正文第一段", "正文==第一段==[^note-1]").replace("第二段", "==第二段==[^note-2]")
    text = text.rstrip("\n") + "\n\n[^note-1]: 批注一\n[^note-2]: 批注二\n[^note-3]: 没挂的\n"
    return annotations.with_reflection(text, "读完的想法\n# 井号开头")


def test_update_keeps_reflection_and_footnotes(zone):
    base = jt.render("标题", "正文第一段\n\n第二段", T0)
    noted = _noted(base)
    assert jt.editable_body(noted) == "正文==第一段==[^note-1]\n\n==第二段==[^note-2]"
    # 删掉第二处的批注号，改一句话、改标题
    out = jt.updated(noted, "新标题", "正文==第一段==[^note-1]\n\n第二段改了\n\n加一段", T1)
    assert out.startswith('---\ntitle: "新标题"\ncreated_at: "2026-11-22T21:30:05+08:00"\n'
                          'updated_at: "2026-11-22T21:42:10+08:00"\n---\n\n# 新标题\n\n'
                          '- 收录时间：2026年11月22日，21:30（UTC+8）\n- 修改时间：2026年11月22日，21:42（UTC+8）\n\n')
    assert "[^note-1]: 批注一" in out and "[^note-2]:" not in out, "删掉的批注号，脚注一起删"
    assert "[^note-3]: 没挂的" in out, "原来就没挂在划线上的批注不动"
    assert annotations.reflection(out) == "读完的想法\n# 井号开头"
    assert out.index("## 读后感") < out.index("[^note-1]:")
    assert jt.editable_body(out) == "正文==第一段==[^note-1]\n\n第二段改了\n\n加一段"
    # 再改一次：修改时间换掉，不叠两行；文件头别的项（星标）留着
    starred = annotations.with_star(out, True)
    again = jt.updated(starred, "", "就一行", T2)
    assert again.count("- 修改时间：") == 1 and "- 修改时间：2026年11月23日，09:00（UTC+8）" in again
    assert annotations.is_starred(again) and 'title: "就一行"' in again
    assert "[^note-1]:" not in again and "[^note-3]: 没挂的" in again


def test_update_odd_files(zone):
    # 没有文件头、没有时间行（Obsidian 里自己建的）：补上文件头和修改时间，正文不丢
    assert jt.updated("随手写的\n第二行\n", "", "随手写的\n第二行\n第三行", T1) == (
        '---\ntitle: "随手写的"\nupdated_at: "2026-11-22T21:42:10+08:00"\n---\n\n# 随手写的\n\n'
        "- 修改时间：2026年11月22日，21:42（UTC+8）\n\n随手写的\n第二行\n第三行\n")
    # 正文自己以「- 要点：」开头的列表：不当成文档头吃掉
    t = jt.render("T", "- 要点：一\n- 要点：二", T0)
    assert jt.editable_body(t) == "- 要点：一\n- 要点：二"
    # Windows 换行
    assert jt.editable_body(t.replace("\n", "\r\n")).startswith("---")   # 认不出文件头时整篇都是正文（不丢字）


def test_reflection_reference_keeps_footnote(zone):
    """正文里删掉批注号，读后感里还引着它：脚注留着，读后感里的引用不落空。"""
    base = jt.render("标题", "正文第一段\n\n第二段", T0)
    text = annotations.with_reflection(
        base.replace("正文第一段", "正文==第一段==[^note-1]").rstrip("\n") + "\n\n[^note-1]: 批注一\n", "见[^note-1]")
    out = jt.updated(text, "", "正文第一段\n\n第二段", T1)
    assert "[^note-1]: 批注一" in out and annotations.reflection(out) == "见[^note-1]"


def test_body_range_edges(zone):
    # 正文中间自己写的「## 读后感」、后面还有别的小节：整段都在编辑框里，不被截掉
    mid = jt.render("t", "a\n## 读后感\nb 用户自己写的\n\n## 别的\n\nc", T0)
    assert jt.editable_body(mid) == "a\n## 读后感\nb 用户自己写的\n\n## 别的\n\nc"
    # 代码块里的「[^note-3]: …」不算脚注
    code = jt.render("t", "前\n```\n[^note-3]: 代码里\n```\n后面的字", T0)
    assert jt.editable_body(code) == "前\n```\n[^note-3]: 代码里\n```\n后面的字"
    # 首行缩进留着，只去掉开头的空行和末尾的空白
    t = jt.render("t", "\n\n　　首行缩进\n第二行\u3000 \n", T0)
    assert jt.editable_body(t) == "　　首行缩进\n第二行"
    assert jt.title_of("", "　　首行缩进") == "首行缩进"


def test_control_characters_in_title(vault):
    from core import clip_compose as cc
    rel = jt.create("a\x0bb\x00c", "x", root=str(vault), now=T0)
    assert rel == "Spark/笔记/20261122_a b c.md" and jt.is_path(rel) and (vault / rel).is_file()
    # 剪藏起名同一条规矩：原来的名字不变，控制字符换成空格
    day = T0.date()
    assert cc.file_name("a/b:c*d?e\"f<g>h|i#j^k[l]m\tn", day) == "20261122_a b c d e f g h i j k l m n.md"
    assert cc.file_name("　全角空格标题　", day) == "20261122_全角空格标题.md"
    assert cc.file_name("a\x0bb\x7f", day) == "20261122_a b.md"


PENDING_HARNESS = r'''
import Foundation
var out: [String: Any] = [:]
let r1 = PendingEdits.submitted(PendingEdits.edited(previous: nil, startText: nil, onMac: false, content: "v1", now: 1), taskID: "t1")
let r2 = PendingEdits.submitted(PendingEdits.edited(previous: r1, startText: "v1", onMac: false, content: "v2", now: 2), taskID: "t2")
let r3 = PendingEdits.edited(previous: r2, startText: "v2", onMac: false, content: "v3", now: 3)
let rv = PendingEdits.edited(previous: r1, startText: "v1", onMac: false, content: "v2", now: 2, chainNew: false)
out["bases"] = [r1.bases, r2.bases, r3.bases, rv.bases]
out["created"] = [r1.created, r2.created, r3.created]
let old = PendingEdits.edited(previous: nil, startText: "m", onMac: true, content: "x", now: 1)
out["oldCreated"] = old.created
// 新建的一条，电脑上还没有、也查不到任务状态：不算「电脑上没有了」
out["newNotGone"] = PendingEdits.reconcile(r2, deletedLocally: false, treeSHA: nil, last: PendingEdits.Last(state: nil), held: false, disk: "v2")
    == .pending(rewrite: false)
let back = try! JSONDecoder().decode(PendingEdits.Record.self, from: JSONEncoder().encode(r2))
out["roundtrip"] = back == r2
// 刚存的剪藏（新建时交出去的 v0 没有记录）第一次改：bases 从 v0 起，带 new
let c1 = PendingEdits.edited(previous: nil, startText: "c0", onMac: false, content: "c1", now: 1)
let c2 = PendingEdits.edited(previous: c1, startText: "c1", onMac: false, content: "c2", now: 2)
let c3 = PendingEdits.edited(previous: c2, startText: "c2", onMac: false, content: "c3", now: 3)
out["clip"] = [c1.bases, c2.bases, c3.bases]
func fields(_ r: PendingEdits.Record, _ review: Bool = false) -> String {
    let f = PendingEdits.taskFields(r, review: review)
    return "\(f["new"] as? Bool ?? false)|\((f["bases"] as? [String])?.count ?? 0)|\(f["base"] as? String == r.bases.first)"
}
out["fields"] = [fields(r1), fields(c3), fields(c3, true), fields(old)]
// 电脑收下过（文件树里是之前任务写进去的那版）：不再带 new
let s1 = PendingEdits.settled(c3, treeSHA: PendingEdits.blobSHA("c1"), applied: [PendingEdits.blobSHA("c1")])
let s0 = PendingEdits.settled(c3, treeSHA: "别人的", applied: [])
let sv = PendingEdits.settled(c1, treeSHA: PendingEdits.blobSHA("c0"), applied: [])   // 新建时交的那份到了
out["settled"] = [s1.created, s0.created, sv.created, fields(s1)]
// 编辑页从 S 开始改，期间又有一份 P1（划了线）：bases 只写 S，电脑上已经是 P1 就判冲突，P1 不被悄悄盖掉
let p1 = PendingEdits.edited(previous: nil, startText: "S", onMac: true, content: "P1", now: 1)
let stale = PendingEdits.edited(previous: p1, startText: "S", onMac: true, content: "S'", now: 2)
let fresh2 = PendingEdits.edited(previous: p1, startText: "P1", onMac: true, content: "P2", now: 2)
out["stale"] = [stale.bases, fresh2.bases]
out["mayDrop"] = [PendingEdits.mayDrop(pendingContent: "P1", editedFrom: "S"), PendingEdits.mayDrop(pendingContent: "P1", editedFrom: "P1")]
// 两份都交、顺序颠倒：后一份（S'）先到、写进去了；前一份（P1）晚到被判冲突 / 还没结果
func show(_ o: PendingEdits.Outcome) -> String {
    switch o { case .remove: "remove"; case .applied: "applied"; case .conflict(let k): "conflict:\(k)"; case .gone: "gone"
               case .resubmit: "resubmit"; case .pending(let w): w ? "pending-rewrite" : "pending" }
}
var both = PendingEdits.submitted(PendingEdits.submitted(stale, taskID: "p1"), taskID: "s2")
let tree = PendingEdits.blobSHA("S'"), done = PendingEdits.Last(state: "done", appliedSHA: tree)
func rc(_ e: [PendingEdits.Last]) -> String {
    show(PendingEdits.reconcile(both, deletedLocally: false, treeSHA: tree, last: done, held: false, disk: "S'", earlier: e))
}
out["order"] = [rc([PendingEdits.Last(state: "failed", kind: "conflict")]), rc([PendingEdits.Last(state: "waiting")]),
                rc([PendingEdits.Last(state: "done")]), rc([PendingEdits.Last(state: nil)]), rc([]),
                rc([PendingEdits.Last(state: "failed", kind: nil)])]
// 连改 40 次：最多 32 个，最早的 8 个都在
var long = PendingEdits.edited(previous: nil, startText: "x0", onMac: true, content: "x1", now: 1)
for i in 2...40 { long = PendingEdits.edited(previous: long, startText: long.content, onMac: true, content: "x\(i)", now: Int64(i)) }
out["cap"] = long.bases
// 报过冲突以后：case R 仍提醒；用户接着改，新任务收下 → 放手；新任务也失败 → 仍提醒；没提醒过的失败不去掉
let failed = PendingEdits.Last(state: "failed", kind: "conflict")
var marked = PendingEdits.markConflict(both, kind: "conflict", statuses: ["p1": failed, "s2": done])
out["marked"] = [marked.state, marked.failedIDs.joined(separator: ","), "\(marked.failedEarlier)", "\(marked.noticed)"]
// 两份都失败：「后来的修改已经交给电脑」说不通，failedEarlier 是 false
let bothFailed = PendingEdits.markConflict(both, kind: "conflict", statuses: ["p1": failed, "s2": failed])
out["bothFailed"] = [bothFailed.failedIDs.joined(separator: ","), "\(bothFailed.failedEarlier)"]
// 标成冲突、但提醒还没显示过（用户没打开这篇，在列表里滑动加了星标）：再改时失败的那个照样带着
let unshown = PendingEdits.submitted(PendingEdits.edited(previous: marked, startText: "S'", onMac: true, content: "v3", now: 9), taskID: "t3")
marked.noticed = true      // 阅读页上显示过了
let again = PendingEdits.markConflict(marked, kind: "conflict", statuses: ["p1": failed, "s2": done])
out["noticedKept"] = again.noticed
let v3 = PendingEdits.submitted(PendingEdits.edited(previous: marked, startText: "S'", onMac: true, content: "v3", now: 9), taskID: "t3")
func after(_ r: PendingEdits.Record, _ last: PendingEdits.Last, tree t: String) -> String {
    let st: [String: PendingEdits.Last] = ["p1": failed, "s2": done, "t3": last]
    return show(PendingEdits.reconcile(r, deletedLocally: false, treeSHA: t, last: st[r.taskIDs.last!], held: false, disk: r.content,
                                       earlier: r.taskIDs.dropLast().map { st[$0] ?? PendingEdits.Last(state: nil) }))
}
let v3blob = PendingEdits.blobSHA("v3")
out["after"] = [v3.taskIDs.joined(separator: ","),
                after(v3, PendingEdits.Last(state: "done", appliedSHA: v3blob), tree: v3blob),
                after(v3, PendingEdits.Last(state: "failed", kind: "conflict"), tree: tree)]
var unseen = both; unseen.state = "pending"     // 还没提醒过（没标成冲突）就又改了：失败的那个照样带着
let v3b = PendingEdits.submitted(PendingEdits.edited(previous: unseen, startText: "S'", onMac: true, content: "v3", now: 9), taskID: "t3")
out["unseen"] = [v3b.taskIDs.joined(separator: ","), after(v3b, PendingEdits.Last(state: "done", appliedSHA: v3blob), tree: v3blob)]
let mback = try! JSONDecoder().decode(PendingEdits.Record.self, from: JSONEncoder().encode(marked))
out["markedRoundtrip"] = mback == marked
out["unshown"] = [unshown.taskIDs.joined(separator: ","), after(unshown, PendingEdits.Last(state: "done", appliedSHA: v3blob), tree: v3blob)]
// 手机上删掉、电脑还没删的一篇：什么时候不用再藏
func del(_ id: String, _ last: PendingEdits.Last?, _ inTree: Bool) -> String { "\(PendingEdits.deleteOutcome(taskID: id, last: last, inTree: inTree))" }
out["delete"] = [del("d1", PendingEdits.Last(state: "waiting"), false), del("d1", PendingEdits.Last(state: nil), false),
                 del("d1", PendingEdits.Last(state: "done"), false), del("d1", PendingEdits.Last(state: "done"), true),
                 del("d1", PendingEdits.Last(state: "failed", kind: "conflict"), false), del("", nil, true), del("", nil, false)]
let oldRec = #"{"content":"x","bases":[],"editedAt":3,"taskIDs":["a"],"state":"conflict","failKind":"conflict"}"#
out["oldRec"] = (try? JSONDecoder().decode(PendingEdits.Record.self, from: Data(oldRec.utf8))).map { "\($0.failedIDs.count)|\($0.failedEarlier)|\($0.created)" } ?? "fail"
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_new_file_edits_chain_bases(tmp_path):
    """手机上新建的一篇（随手记、剪藏……）再改时，bases 从前一份接起；复盘（chainNew: false）一直不带。"""
    main = tmp_path / "main.swift"
    main.write_text(PENDING_HARNESS, encoding="utf-8")
    exe = tmp_path / "pending"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_PENDING, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout)
    assert got["bases"] == [[], [_sha("v1")], [_sha("v1"), _sha("v2")], []]
    assert got["created"] == [True, True, True] and got["oldCreated"] is False
    assert got["newNotGone"] is True and got["roundtrip"] is True
    assert got["clip"] == [[_sha("c0")], [_sha("c0"), _sha("c1")], [_sha("c0"), _sha("c1"), _sha("c2")]]
    assert got["fields"] == ["true|0|true", "true|3|true", "false|0|false", "false|1|true"]
    assert got["settled"] == [False, True, False, "false|3|true"]
    assert got["stale"] == [[_sha("S")], [_sha("S"), _sha("P1")]]
    assert got["mayDrop"] == [False, True], "发件箱里的 P1 不是这次的起点：留着，两份都交"
    assert got["order"] == ["conflict:conflict", "pending", "applied", "applied", "applied", "conflict:rejected"], \
        "前一份冲突：提醒，不放手；前一份还在等：先不放手；查不到结果的老任务不等"
    assert got["marked"] == ["conflict", "p1", "true", "false"]
    assert got["bothFailed"] == ["p1,s2", "false"], "两份都失败：不说「后来的修改已经交给电脑」"
    assert got["noticedKept"] is True, "同样的失败再对一次账：显示过的不算没显示"
    assert got["unshown"] == ["p1,s2,t3", "conflict:conflict"], "提醒没显示过就又改了：失败的那个照样带着"
    assert got["delete"] == ["keep", "keep", "clear", "keep", "failed", "keep", "clear"], \
        "文件树里没有但删除还没结果：还藏着；做完、树里也没有：放掉；没删成：不再藏；旧版记号照旧看文件树"
    assert got["after"] == ["s2,t3", "applied", "conflict:conflict"], "报过冲突后再改：收下就放手，又失败仍提醒"
    assert got["unseen"] == ["p1,s2,t3", "conflict:conflict"], "没提醒过的失败不悄悄去掉"
    assert got["markedRoundtrip"] is True and got["oldRec"] == "0|false|false"
    want = [_sha(f"x{i}") for i in range(40)]
    assert got["cap"] == want[:8] + want[-24:] and len(got["cap"]) == 32


# ---------------------------------------------------------------- 卡片上的数字

def _e(rel, created, changed, title="t"):
    return {"rel": rel, "title": title, "created": created, "updated": changed, "changed": changed}


def test_summary_numbers_and_window(zone):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    edge = now - timedelta(days=7)
    js = [_e("Spark/笔记/a.md", edge, edge),                                      # 正好 7 天前新建：算新增
          _e("Spark/笔记/b.md", edge - timedelta(seconds=1), edge),               # 之前就有、正好 7 天前改：算修改
          _e("Spark/笔记/c.md", edge - timedelta(days=3), edge - timedelta(seconds=1)),   # 改在 7 天前：不算
          _e("Spark/笔记/d.md", now - timedelta(hours=1), now - timedelta(minutes=5))]   # 这几天新建又改：只算新增
    reading = [_e("Spark/剪藏/x.md", now, now - timedelta(days=1)), _e("Spark/剪藏/y.md", now, now - timedelta(days=9)),
               _e("Spark/笔记/d.md", now, now - timedelta(minutes=5))]          # 随手记里划了线：不重复算
    s = jt.summarize(js, reading, now)
    assert (s["jottings"], s["reflections"], s["added"], s["edited"]) == (4, 2, 2, 2)
    assert s["latest"] == now - timedelta(minutes=5)
    assert [(r["rel"], r["kind"]) for r in s["recent"]] == [("Spark/笔记/d.md", "随手记"), ("Spark/剪藏/x.md", "读后感"),
                                                             ("Spark/笔记/a.md", "随手记")]
    assert jt.summarize([], [], now) == {"jottings": 0, "reflections": 0, "latest": None, "added": 0, "edited": 0, "recent": []}


def test_summary_from_vault(vault):
    """「笔记」里只放随手记和写了读后感的文档：只有划线、批注的不算；随手记自己写了读后感不重复算。"""
    rel = jt.create("想法", "写一点", root=str(vault), now=T0)
    clips = vault / "Spark" / "剪藏"
    clips.mkdir(parents=True)
    (clips / "20261101_文.md").write_text('---\ntitle: "一篇文"\n---\n\n# 一篇文\n\n## 正文\n\n==划线==\n\n## 读后感\n\n读完想到的\n',
                                         encoding="utf-8")
    (clips / "20261102_只划线.md").write_text('---\ntitle: "只划线"\n---\n\n# 只划线\n\n## 正文\n\n==划线==\n', encoding="utf-8")
    (clips / "20261103_空读后感.md").write_text('---\ntitle: "空"\n---\n\n# 空\n\n## 正文\n\n==划线==\n\n## 读后感\n\n', encoding="utf-8")
    full = vault / rel
    full.write_text(annotations.with_reflection(full.read_text(encoding="utf-8"), "随手记的读后感"), encoding="utf-8")
    s = jt.summary(str(vault), now=T1)
    assert (s["jottings"], s["reflections"], s["added"]) == (1, 1, 1)
    assert {r["title"] for r in s["recent"]} == {"想法", "一篇文"}
    assert {r[0] for r in jt.reflection_docs(str(vault))} == {"Spark/剪藏/20261101_文.md"}


# ---------------------------------------------------------------- 手机：Jottings.swift 编出来跑，跟电脑一字不差

def _swift_chunk(src: str, start: str) -> str:
    i = src.index(start)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise ValueError(start)


HARNESS = r'''
import Foundation
enum Clips {
%s
}
enum DocEdit {
%s
}
func d(_ v: Any?) -> Date { Date(timeIntervalSince1970: v as! Double) }
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
out["render"] = (input["render"] as! [[String: Any]]).map { c -> [String: String] in
    let title = c["title"] as! String, body = c["body"] as! String
    return ["text": Jottings.render(title: title, body: body, created: d(c["created"]), tags: c["tags"] as! [String], starred: c["starred"] as! Bool),
            "file": Jottings.fileName(title: Jottings.titleOf(title, Jottings.normalize(body)), created: d(c["created"]))]
}
out["updated"] = (input["updated"] as! [[String: Any]]).map { c in
    Jottings.updated(c["text"] as! String, title: c["title"] as! String, body: c["body"] as! String, now: d(c["now"]))
}
out["body"] = (input["body"] as! [String]).map { Jottings.editableBody($0) }
out["title"] = (input["title"] as! [[String]]).map { Jottings.titleOf($0[0], $0[1]) }
out["modified"] = DocTime.modifiedLine(d(input["now"]))
out["excerpt"] = (input["excerpt"] as! [String]).map { Jottings.excerpt($0, limit: 5) }
out["files"] = (input["files"] as! [String]).map { Jottings.fileName(title: $0, created: d(input["now"])) }
let s = input["summary"] as! [String: Any]
func entries(_ k: String) -> [Jottings.Entry] {
    (s[k] as! [[String: Any]]).map { Jottings.Entry(path: $0["rel"] as! String, title: "t", created: d($0["created"]), changed: d($0["changed"])) }
}
let r = Jottings.summarize(jottings: entries("jottings"), reflections: entries("reflections"), now: d(s["now"]))
out["summary"] = ["counts": [r.jottings, r.reflections, r.added, r.edited], "latest": r.latest.map { $0.timeIntervalSince1970 } ?? -1,
                  "recent": r.recent.map { "\($0.entry.path)|\($0.kind)" }]
let metas = (input["meta"] as! [[String]]).map { Jottings.meta(path: $0[0], text: $0[1], fallback: Date(timeIntervalSince1970: 0)) }
out["meta"] = metas.map { ["title": $0.title, "created": $0.created.timeIntervalSince1970, "updated": $0.updated.timeIntervalSince1970,
                           "starred": $0.starred] as [String: Any] }
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


def _cases():
    base = jt.render("标题", "正文第一段\n\n第二段", T0)
    noted = _noted(base)
    render = [{"title": "", "body": "  第一行想法\n\n第二段  \n", "created": T0.timestamp(), "tags": [], "starred": False},
              {"title": "a/b:c*d?e\"f<g>h|i#j^k[l]m\tn", "body": "x", "created": T0.timestamp(), "tags": ["读书", "A:B"], "starred": True},
              {"title": "", "body": "中" * 70, "created": T2.timestamp(), "tags": [], "starred": False},
              {"title": "只有标题", "body": "", "created": T0.timestamp(), "tags": [], "starred": False},
              {"title": "", "body": "\r\n> ==划==[^note-1] 一\r\n\r\n二\r", "created": T0.timestamp(), "tags": [], "starred": False},
              {"title": "　全角空格　", "body": "é" * 40, "created": T1.timestamp(), "tags": [], "starred": False},
              {"title": "", "body": "", "created": T0.timestamp(), "tags": [], "starred": False}]
    edits = [(noted, "新标题", "正文==第一段==[^note-1]\n\n第二段改了\n\n加一段"),
             (noted, "", "全删了"),
             (annotations.with_star(noted, True), "星", noted.split("- 收录时间：")[1].split("\n", 2)[2]),
             ("随手写的\n第二行\n", "", "随手写的\n第二行\n第三行"),
             (jt.render("T", "- 要点：一\n- 要点：二", T0), "T", "- 要点：一"),
             (jt.updated(base, "", "改过一次", T1), "再改", "改过两次"),
             ("---\ntitle: x\nfoo: bar\n---\n# x\n- 收录时间：2026年11月20日\n- 来源：书\n正文\n## 读后感\n\n感\n", "", "新正文"),
             (base, "", "## 读后感 不算\n正文"), ("", "", "空文件"), (base.replace("\n", "\r\n"), "", "crlf")] + _review_edits()
    bodies = [base, noted, "", "---\n---\n", "# 只有标题\n", base.replace("\n", "\r\n")] + [e[0] for e in edits]
    titles = [["", "# 标题行\n正文"], ["", "> ==划线==[^note-1] 的话"], ["", "中" * 40], ["", ""], ["  多\n行\r\n标题 ", "x"],
              ["", "**粗**体"], ["", "\t- 「引号」"], ["", "🙂" * 31]]
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    edge = now - timedelta(days=7)
    summary = {"now": now.timestamp(),
               "jottings": [{"rel": "Spark/笔记/a.md", "created": edge.timestamp(), "changed": edge.timestamp()},
                            {"rel": "Spark/笔记/b.md", "created": edge.timestamp() - 1, "changed": edge.timestamp()},
                            {"rel": "Spark/笔记/c.md", "created": edge.timestamp() - 9e4, "changed": edge.timestamp() - 1},
                            {"rel": "Spark/笔记/e.md", "created": now.timestamp() - 300, "changed": now.timestamp() - 300},
                            {"rel": "Spark/笔记/d.md", "created": now.timestamp() - 3600, "changed": now.timestamp() - 300}],
               "reflections": [{"rel": "Spark/剪藏/x.md", "created": 0, "changed": now.timestamp() - 86400},
                           {"rel": "output/y.md", "created": 0, "changed": now.timestamp() - 9 * 86400},
                           {"rel": "Spark/笔记/d.md", "created": 0, "changed": now.timestamp() - 300}]}
    meta = [["Spark/笔记/20261122_标题.md", base], ["Spark/笔记/20261122_星.md", annotations.with_star(noted, True)],
            ["Spark/笔记/20261101_老.md", "# 老\n\n- 收录时间：2026年11月01日，08:00（UTC+8）\n\n正文\n"],
            ["Spark/笔记/20261102_名.md", "正文\n"]]
    render += [{"title": t, "body": "x", "created": T0.timestamp(), "tags": [], "starred": False}
               for t in ("a\x0bb", "\u200b", "\u200b标题\u200b", "a\u200b#", "\x00\x1f")]
    render.append({"title": "", "body": "\n\n　　首行缩进的一段\n第二行\u3000 \n", "created": T0.timestamp(), "tags": [], "starred": False})
    excerpt = ["一二三四五六👩‍👩‍👧é́", "👩‍👩‍👧" * 6, "\u200b正文\u200b", "　　缩进"]
    files = ["a\x0bb", "\u200b", "a\u200b#", "x/y", "中" * 70, "\x7f"]
    return {"render": render, "excerpt": excerpt, "files": files, "updated": [{"text": t, "title": ti, "body": b, "now": T2.timestamp()} for t, ti, b in edits],
            "body": bodies, "title": titles, "now": T1.timestamp(), "summary": summary, "meta": meta}


def _review_edits():
    """评审找出来的几种：正文里自己写的「## 读后感」、代码块里的脚注写法、读后感引着的批注、缩进、零宽空格、Windows 换行。"""
    base = jt.render("标题", "正文第一段\n\n第二段", T0)
    user_refl = jt.render("t", "a\n## 读后感\nb 用户自己写的\n\n## 别的\n\nc", T0)
    code = jt.render("t", "前\n```\n[^note-3]: 代码里\n```\n后面的字", T0)
    refl_ref = annotations.with_reflection(
        base.replace("正文第一段", "正文==第一段==[^note-1]").rstrip("\n") + "\n\n[^note-1]: 批注一\n", "见[^note-1]")
    cr_refl = "---\ntitle: t\n---\n\n# t\n\n- 收录时间：x\n\nbody\n## 读后感\r\n\n感\n"
    return [(user_refl, "", "a 改了"), (code, "", "前 改"), (refl_ref, "", "正文第一段 去掉批注号\n\n第二段"),
            (jt.render("t", "x", T0), "", "  　缩进的第一行\n内容　 "), (cr_refl, "", "body2"),
            (jt.render("t", "x", T0), "\u200b", "\u200b正文\u200b"),
            (base.replace("\n", "\r\n"), "", "c"),
            (jt.render("t", "a\n## 读后感\n结尾自己写的", T0), "", "a2")]


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
@pytest.mark.parametrize("tzname", ["Asia/Shanghai", "America/Los_Angeles"])
def test_swift_jottings_match_python(tmp_path, zone, tzname):
    zone(tzname)
    with open(SWIFT_CLIPS, encoding="utf-8") as f:
        src = f.read()
    parts = [_swift_chunk(src, s) for s in ("static func yaml(", "static func fileName(", "static func frontmatter(")]
    with open(SWIFT_DOCEDITS, encoding="utf-8") as f:
        refl = _swift_chunk(f.read(), "static func reflectionRange(")
    main = tmp_path / "main.swift"
    main.write_text(HARNESS % ("\n".join(parts), refl), encoding="utf-8")
    exe = tmp_path / "jottings"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_DOCTIME, SWIFT_JOTTINGS, str(main)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    cases = _cases()
    p = subprocess.run([str(exe)], input=json.dumps(cases), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "TZ": tzname})
    assert p.returncode == 0, p.stderr
    got = json.loads(p.stdout)
    utc = lambda v: datetime.fromtimestamp(v, timezone.utc)   # noqa: E731
    for c, g in zip(cases["render"], got["render"]):
        assert g["text"] == jt.render(c["title"], c["body"], utc(c["created"]), tags=c["tags"], starred=c["starred"]), c
        assert g["file"] == jt.file_name(jt.title_of(c["title"], jt.normalize_body(c["body"])), utc(c["created"])), c
    for c, g in zip(cases["updated"], got["updated"]):
        assert g == jt.updated(c["text"], c["title"], c["body"], utc(c["now"])), c
    assert got["body"] == [jt.editable_body(t) for t in cases["body"]]
    assert got["title"] == [jt.title_of(a, b) for a, b in cases["title"]]
    assert got["modified"] == doctime.modified_line(utc(cases["now"]))
    assert got["excerpt"] == [jt.excerpt(t, 5) for t in cases["excerpt"]]
    assert got["files"] == [jt.file_name(t, utc(cases["now"])) for t in cases["files"]]
    assert all(jt.is_path(f"Spark/笔记/{f}") for f in got["files"])
    s = cases["summary"]
    ent = lambda k: [{"rel": e["rel"], "title": "t", "created": utc(e["created"]), "updated": utc(e["changed"]),   # noqa: E731
                      "changed": utc(e["changed"])} for e in s[k]]
    py = jt.summarize(ent("jottings"), ent("reflections"), utc(s["now"]))
    assert got["summary"]["counts"] == [py["jottings"], py["reflections"], py["added"], py["edited"]] == [5, 2, 3, 2]
    assert got["summary"]["latest"] == py["latest"].timestamp()
    assert got["summary"]["recent"] == [f'{r["rel"]}|{r["kind"]}' for r in py["recent"]]
    for (rel, text), g in zip(cases["meta"], got["meta"]):
        m = jt.parse(text, os.path.basename(rel), 0)
        assert (g["title"], g["created"], g["updated"], g["starred"]) == \
            (m["title"], m["created"].timestamp(), m["updated"].timestamp(), m["starred"]), rel


# ---------------------------------------------------------------- 手机交来的：新建、改、冲突、删

def _task(**kw):
    return rt.parse_task(dict({"type": "save", "replace": True}, **kw))


def _conflicts(vault):
    d = vault.parent / "conflicts"
    return sorted(p.read_text(encoding="utf-8") for p in d.iterdir()) if d.is_dir() else []


def test_phone_new_jotting_lands_at_its_own_path(vault):
    rel = "Spark/笔记/20261122_想法.md"
    v1 = jt.render("想法", "一", T0)
    t = _task(path=rel, content=v1, edited_at=1795000000000)
    assert "base" not in t and t["replace"]
    assert "存进了" in rt.save_file(t)
    assert (vault / rel).read_text(encoding="utf-8") == v1 and t["_applied_sha"] == rt.blob_sha(v1.encode())
    assert not (vault / "Spark" / "笔记" / "20261122_想法_2.md").exists()
    assert "已经是这一版" in rt.save_file(_task(path=rel, content=v1)), "同一份再交一次：不动"
    # 手机上新建后又改了两次（都还没同步回手机）：第二次起带 bases（之前那份的指纹），照顺序都收
    v2 = jt.updated(v1, "", "二", T1)
    v3 = jt.updated(v2, "", "三", T2)
    assert "存进了" in rt.save_file(_task(path=rel, content=v2, bases=[_sha(v1)], edited_at=1795000000500))
    assert "存进了" in rt.save_file(_task(path=rel, content=v3, bases=[_sha(v1), _sha(v2)], edited_at=1795000000900))
    assert (vault / rel).read_text(encoding="utf-8") == v3
    # 晚到的重复任务（v1、v2）：内容已经是现在这版的祖先，算做过，不动
    for old in (v1, v2):
        t = _task(path=rel, content=old, edited_at=1795000000000)
        assert "已经有这一版的改动" in rt.save_file(t) and t["_applied_sha"] == rt.blob_sha(v3.encode())
    assert (vault / rel).read_text(encoding="utf-8") == v3 and _conflicts(vault) == []


def test_new_jotting_first_save_dropped_from_outbox(vault):
    """(H) 只用数据线：新建的 v1 在发件箱里被 v2 替掉了，电脑只收到 v2（new + bases=[v1]）：照写，不算「没有了」。"""
    rel = "Spark/笔记/20261122_h.md"
    v1 = jt.render("h", "v1", T0)
    v2 = jt.updated(v1, "h", "v2", T1)
    t = _task(path=rel, content=v2, bases=[_sha(v1)], new=True, edited_at=1795000000000)
    assert t["new"] is True and "存进了" in rt.save_file(t)
    assert (vault / rel).read_text(encoding="utf-8") == v2
    # 之后那份 v1 才到（另一条路交的）：是 v2 的祖先，算做过
    assert "已经有这一版的改动" in rt.save_file(_task(path=rel, content=v1, new=True))
    # 不是新建的（改电脑上原有的一条）带 bases、电脑上没有：仍是 gone，不变回来
    gone = "Spark/笔记/20261122_没了.md"
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=gone, content=v2, bases=[_sha(v1)]))
    assert e.value.kind == "gone" and not (vault / gone).exists()
    assert _conflicts(vault) == []


def test_new_jotting_taken_by_other_device_with_chain(vault):
    """new + bases，但电脑上这个名字是另一台设备的一条：kind=taken（手机换名重交），电脑上的不动。"""
    rel = "Spark/笔记/20261122_买菜.md"
    other = jt.render("买菜", "iPad 写的", T0)
    rt.save_file(_task(path=rel, content=other))
    v1 = jt.render("买菜", "iPhone 写的", T0)
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel, content=jt.updated(v1, "", "iPhone 又改", T1), bases=[_sha(v1)], new=True))
    assert e.value.kind == "taken" and (vault / rel).read_text(encoding="utf-8") == other
    # 同一篇：手机 v1 到了，电脑网页上改过，手机再改（new 还没清）：是两边都改过，kind=conflict（照常提醒，不换名）
    rel2 = "Spark/笔记/20261122_同一篇.md"
    v1 = jt.render("同一篇", "v1", T0)
    rt.save_file(_task(path=rel2, content=v1, new=True))
    jt.update(rel2, "", "网页上改的", jt.sha256((vault / rel2).read_text(encoding="utf-8")), root=str(vault))
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel2, content=jt.updated(v1, "", "手机改的", T1), bases=[_sha(v1)], new=True))
    assert e.value.kind == "conflict" and "网页上改的" in (vault / rel2).read_text(encoding="utf-8")


CLIP0 = ('---\ntitle: "c"\nsource: "s"\nurl: "https://e.com"\nclipped: 2026-11-22\nclipped_at: "2026-11-22T21:30:05+08:00"\n---\n\n'
         '# c\n\n- 来源：s\n- 收录时间：2026年11月22日，21:30（UTC+8）\n\n## 正文\n\nbody\n')


@pytest.fixture
def no_images(monkeypatch):
    monkeypatch.setattr(rt.clips, "download_images", lambda images, root: (0, 0))


def test_new_clip_offline_saves(vault, no_images):
    c0 = CLIP0
    c1, c2 = c0.replace("body", "body tagged"), c0.replace("body", "body tagged starred")
    # (K) 新存的剪藏 v0、v1 都在发件箱里被替掉，只到了 v2（new + bases=[v0, v1]）：照写
    k = "Spark/剪藏/20261122_k.md"
    assert "存进了" in rt.save_file(_task(path=k, content=c2, bases=[_sha(c0), _sha(c1)], new=True, images={}))
    assert (vault / k).read_text(encoding="utf-8") == c2
    # (L) v0 走 GitHub 到了，v1 在发件箱里被替掉，v2（new + bases=[v0, v1]）：电脑上是 v0，照写，不进 note-conflicts
    l = "Spark/剪藏/20261122_l.md"
    rt.save_file(_task(path=l, content=c0, images={}))
    assert "存进了" in rt.save_file(_task(path=l, content=c2, bases=[_sha(c0), _sha(c1)], new=True, images={}))
    assert (vault / l).read_text(encoding="utf-8") == c2
    # 离线存下、几秒里自动又存了三次（标签、星标、摘要）：发件箱里只剩最后一份，链上是前面每一份
    m = "Spark/剪藏/20261122_m.md"
    versions = [c0.replace("body", f"body {i}") for i in range(4)]
    last = _task(path=m, content=versions[3], bases=[_sha(v) for v in versions[:3]], new=True, images={})
    rt.save_file(last)
    assert (vault / m).read_text(encoding="utf-8") == versions[3]
    # 原样再交一次、早先的几份晚到：都算做过
    for i, v in enumerate(versions):
        rt.save_file(_task(path=m, content=v, bases=[_sha(x) for x in versions[:i]], new=True, images={}))
    assert (vault / m).read_text(encoding="utf-8") == versions[3]
    assert _conflicts(vault) == []
    assert {n for n in os.listdir(vault / "Spark" / "剪藏") if n.startswith("2026")} == \
        {"20261122_k.md", "20261122_l.md", "20261122_m.md"}, "一篇一个文件，没有 _2"


def test_cable_result_carries_taken_kind(vault, tmp_path, monkeypatch):
    """数据线交的任务没成功：结果文件里带 kind（taken、gone……），手机同步时按它判（Vault.reconcilePending → cableTaskResults）。"""
    from core import cable_sync as cs
    monkeypatch.setitem(cs._CALL, "fn", None)
    monkeypatch.setitem(cs._BOOK, "book", None)
    rel = "Spark/笔记/20261122_买菜.md"
    rt.save_file(_task(path=rel, content=jt.render("买菜", "iPad 写的", T0)))
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    task = {"type": "save", "path": rel, "content": jt.render("买菜", "iPhone 写的", T0), "replace": True, "new": True,
            "edited_at": 1795000000000}
    (outbox / "j1.sync.json").write_text(json.dumps({"id": "j1", "title": "x", "task": task, "created_at": "2026-11-22T10:00:00Z"},
                                                   ensure_ascii=False), encoding="utf-8")

    class Tr:
        def push(self, local_dir, dest):
            shutil.copytree(local_dir, os.path.join(str(tmp_path / "phone"), dest), dirs_exist_ok=True)

        def pull_outbox(self, local_dir):
            shutil.copytree(outbox, local_dir, dirs_exist_ok=True)

    cs.check_device({"id": "dev-1", "udid": "dev-1", "name": "测试 iPhone", "transport": "wired"}, transport=Tr())
    res = cs._book().results()["tasks"]["j1"]
    assert (res["state"], res["kind"]) == ("failed", "taken")


def test_mac_accepts_32_bases(vault):
    """连改十几次、只有最早那份到过电脑：bases 里第 1 个以后的也认（电脑上限跟手机一样 32 个）。"""
    rel = jt.create("长链", "v0", root=str(vault), now=T0)
    v0 = (vault / rel).read_text(encoding="utf-8")
    chain = [v0] + [jt.updated(v0, "", f"v{i}", T1) for i in range(1, 12)]
    t = _task(path=rel, content=jt.updated(v0, "", "最后", T2), bases=[_sha(chain[1])] + [_sha(c) for c in chain[2:]] + [_sha(v0)])
    assert len(t["bases"]) == 12 and "存进了" in rt.save_file(t), "电脑上的 v0 排在第 12 个也认"
    assert len(rt.parse_task(dict(t, bases=[_sha(str(i)) for i in range(40)]))["bases"]) == 32


def test_phone_clock_set_back_still_applies(vault):
    """收不收只看内容：手机时钟往回拨了，v2 的 edited_at 比 v1 小，照样写进去。"""
    rel = "Spark/笔记/20261122_g.md"
    v1 = jt.render("g", "v1", T0)
    v2 = jt.updated(v1, "g", "v2 重要", T0)
    rt.save_file(_task(path=rel, content=v1, edited_at=1795000001000))
    assert "存进了" in rt.save_file(_task(path=rel, content=v2, bases=[_sha(v1)], edited_at=1795000000000))
    assert "v2 重要" in (vault / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("skew", [-60000, 0, 60000])
@pytest.mark.parametrize("b_first", [False, True])
def test_two_devices_same_name_nothing_lost(vault, skew, b_first):
    """iPhone 和 iPad 同一天写了开头一样的一条（同一个文件名），时钟不准、先后不定：
    先到的写进去，后到的存进 note-conflicts、任务失败（kind=conflict），哪份都不丢，不看时间。"""
    rel = "Spark/笔记/20261122_买菜.md"
    a = _task(path=rel, content=jt.render("买菜", "iPhone 写的：鸡蛋", T0), edited_at=1795000000000)
    b = _task(path=rel, content=jt.render("买菜", "iPad 写的：重要会议纪要", T0), edited_at=1795000000000 + skew)
    first, second = (b, a) if b_first else (a, b)
    assert "存进了" in rt.save_file(first)
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(second)
    assert e.value.kind == "taken", "同名的另一篇：手机换个名字重交"
    assert (vault / rel).read_text(encoding="utf-8") == first["content"]
    assert _conflicts(vault) == [second["content"]]


def test_baseless_save_never_overwrites_other_content(vault):
    """不带 bases 的、电脑上已经有别的内容（旧版手机连着改、电脑网页上同名新建、网页上改过）：一律冲突，电脑上的不动。"""
    rel = "Spark/笔记/20261122_x.md"
    v1 = jt.render("x", "AAA", T0)
    rt.save_file(_task(path=rel, content=v1))
    for content in (jt.render("x", "BBB", T0), jt.updated(v1, "", "旧版手机第二次改，没带 bases", T1)):
        with pytest.raises(rt.TaskError) as e:
            rt.save_file(_task(path=rel, content=content))
        assert e.value.kind == "taken" and (vault / rel).read_text(encoding="utf-8") == v1
    web = jt.create("同日", "电脑网页写的", root=str(vault))
    with pytest.raises(rt.TaskError):
        rt.save_file(_task(path=web, content=jt.render("同日", "手机写的", T0)))
    assert "电脑网页写的" in (vault / web).read_text(encoding="utf-8")
    assert len(_conflicts(vault)) == 3


def test_phone_edit_with_bases_and_conflict(vault):
    rel = jt.create("想法", "原来的", root=str(vault), now=T0)
    full = vault / rel
    v0 = full.read_text(encoding="utf-8")
    v1 = jt.updated(v0, "", "手机上改的", T1)
    t = _task(path=rel, content=v1, bases=[_sha(v0)], edited_at=1795000000000)
    assert t["base"] == _sha(v0)
    assert "存进了" in rt.save_file(t)
    assert full.read_text(encoding="utf-8") == v1
    # 改回原样（撤销）也写
    assert "存进了" in rt.save_file(_task(path=rel, content=v0, bases=[_sha(v0), _sha(v1)]))
    assert full.read_text(encoding="utf-8") == v0
    # 电脑上也改过：不覆盖，手机这一版存进 note-conflicts
    full.write_text(jt.updated(v0, "", "电脑上改的", T2), encoding="utf-8")
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel, content=v1, bases=[_sha(v0)]))
    assert e.value.kind == "conflict" and "电脑上改的" in full.read_text(encoding="utf-8")
    # 电脑上已经删了：gone，不变回来
    full.unlink()
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel, content=v1, bases=[_sha(v0)]))
    assert e.value.kind == "gone" and not full.exists()


def test_phone_delete_moves_jotting_to_trash(vault):
    rel = jt.create("要删的", "x", root=str(vault), now=T0)
    t = rt.parse_task({"type": "delete", "path": rel})
    assert "废纸篓" in rt.delete_file(t)
    assert not (vault / rel).exists() and os.listdir(vault.parent / "Trash") == [os.path.basename(rel)]


@pytest.mark.parametrize("path", ["Spark/笔记/sub/a.md", "Spark/笔记/.a.md", "Spark/笔记/a.txt", "Spark/笔记/../a.md"])
def test_bad_jotting_paths_rejected(vault, path):
    with pytest.raises(rt.TaskError):
        rt.parse_task({"type": "delete", "path": path})


def test_jottings_folder_is_not_a_show(vault):
    jt.create("a", "b", root=str(vault), now=T0)
    with pytest.raises(rt.TaskError, match="文件夹"):
        rt.parse_task({"type": "link", "url": "https://e.com/x", "dest": {"kind": "show", "folder": "笔记"}})
    with pytest.raises(rt.TaskError, match="节目文件夹"):
        rt.parse_task({"type": "episode_item", "folder": "Spark/笔记", "entry": {}, "summary": {}})


def test_create_dedupes_and_validates(vault):
    a = jt.create("同名", "一", root=str(vault), now=T0)
    b = jt.create("同名", "二", root=str(vault), now=T0)
    assert (a, b) == ("Spark/笔记/20261122_同名.md", "Spark/笔记/20261122_同名_2.md")
    with pytest.raises(jt.JottingError):
        jt.create(" ", "\n", root=str(vault))
    with pytest.raises(jt.Conflict):
        jt.update(a, "", "改", "0" * 64, root=str(vault))


# ---------------------------------------------------------------- 阅读页

def _client():
    import spark
    from werkzeug.test import Client
    return Client(spark.application)


def _get(c, path):
    return c.get(urllib.parse.quote(path, safe="/?=&"))


def test_reader_jottings_pages(vault):
    from apps.reader import server as reader
    c = _client()
    empty = _get(c, "/read/jottings").get_data(as_text=True)
    assert "还没有笔记" in empty and 'href="/read/jottings/new"' in empty and "jt-card" not in empty
    show = vault / "Spark" / "Show"
    (show / "notes").mkdir(parents=True)
    (show / ".manifest.json").write_text('{"content_type": "series"}', encoding="utf-8")
    (show / "notes" / "20261101 一期.md").write_text("# 一期\n\n==划==\n\n## 读后感\n\n第一句感想\n第二行\n", encoding="utf-8")
    (show / "notes" / "20261102 二期.md").write_text("# 二期\n\n==只划线==\n", encoding="utf-8")
    rel = jt.create("<b>标题</b>", "正文 <script>alert(1)</script>", root=str(vault))
    page = _get(c, "/read/jottings").get_data(as_text=True)
    assert "jt-card" in page and "随手记 1 篇 · 读后感 1 篇" in page and "近 7 天：新增 1，修改 1" in page, "刚写的读后感算修改"
    # 读后感一节：标题、第一句、来源，点进去是这篇的读书笔记；只划了线的不在这里
    assert "<h2>读后感" in page and "第一句感想" in page and "Show・小结" in page and "二期" not in page
    one = _get(c, "/read/notes?path=Spark/Show/notes/20261101 一期.md").get_data(as_text=True)
    assert "第一句感想" in one and "只划线" not in one
    assert "/read/notes?path=" in page
    assert "<script>alert" not in page and "&lt;b&gt;标题&lt;/b&gt;" in page
    assert "最近更新 · 今天" in page
    assert page.count('aria-current="page"') == 1
    # 首页：笔记在第一行，随手记的文件夹不当节目列
    home = _get(c, "/read/").get_data(as_text=True)
    assert home.index('/read/jottings"') < home.index("Show") and "随手记 1 篇 · 读后感 1 篇" in home
    assert '/read/f/%E7%AC%94%E8%AE%B0/' not in home
    assert all(s["name"] != "笔记" for s in reader._shows())
    assert _get(c, "/read/f/笔记/").status_code in (301, 302, 303, 307, 308)
    # 读一条：正常的阅读页（能做笔记），顶栏有「编辑」
    name = os.path.basename(rel)
    doc = _get(c, f"/read/f/笔记/{name}").get_data(as_text=True)
    assert 'data-notes="true"' in doc and "/read/jottings/edit?path=" in doc
    edit = _get(c, f"/read/jottings/edit?path=笔记/{name}").get_data(as_text=True)
    text = (vault / rel).read_text(encoding="utf-8")
    assert f'data-base="{_sha(text)}"' in edit and "&lt;script&gt;" in edit and "jottings.js" in edit
    assert _get(c, "/read/jottings/edit?path=Show/notes/20261101 一期.md").status_code == 404
    items = json.loads(_get(c, "/read/api/items?place=latest").get_data(as_text=True))["items"]
    assert any(i["show"] == "随手记" for i in items)


def test_reader_jotting_save_api(vault):
    c = _client()
    r = c.post("/read/api/jottings/save", json={"title": "", "body": "网页上写的\n第二行"})
    d = r.get_json()
    assert r.status_code == 200 and d["path"].startswith("笔记/") and d["url"].startswith("/read/f/")
    full = vault / "Spark" / d["path"]
    text = full.read_text(encoding="utf-8")
    assert d["base"] == _sha(text) and "# 网页上写的" in text
    # 改：带打开时的指纹
    r = c.post("/read/api/jottings/save", json={"path": d["path"], "base": d["base"], "title": "改了", "body": "新的"})
    assert r.status_code == 200 and "# 改了" in full.read_text(encoding="utf-8")
    # 指纹旧了：拒绝，文件不动
    before = full.read_text(encoding="utf-8")
    r = c.post("/read/api/jottings/save", json={"path": d["path"], "base": d["base"], "title": "", "body": "盖掉"})
    assert r.status_code == 409 and "没有覆盖" in r.get_json()["error"] and full.read_text(encoding="utf-8") == before
    # 空的、不是随手记的、别的网站发来的
    assert c.post("/read/api/jottings/save", json={"title": " ", "body": ""}).status_code == 400
    (vault / "Spark" / "Show").mkdir()
    (vault / "Spark" / "Show" / "a.md").write_text("x", encoding="utf-8")
    assert c.post("/read/api/jottings/save", json={"path": "Show/a.md", "base": _sha("x"), "body": "y"}).status_code == 400
    r = c.post("/read/api/jottings/save", json={"body": "z"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert len(os.listdir(vault / "Spark" / "笔记")) == 1


# ---------------------------------------------------------------- 手机上删掉以后：晚到的存不再把它建回来（墓碑）

def _delete(rel, shas=(), keys=(), key=None):
    raw = {"type": "delete", "path": rel, "shas": list(shas), "keys": list(keys), "edited_at": 1795000005000}
    if key:
        raw["key"] = key
    return rt.delete_file(rt.parse_task(raw))


def test_save_then_delete(vault, no_images):
    """先存后删：存进来，再移进废纸篓；同一份存再晚到一次（重复交），不建回来。"""
    rel = "Spark/剪藏/20261122_xhs.md"
    rt.save_file(_task(path=rel, content=CLIP0, images={}))
    assert "废纸篓" in _delete(rel)
    assert not (vault / rel).exists()
    t = _task(path=rel, content=CLIP0, images={})
    assert "已经删了" in rt.save_file(t) and not (vault / rel).exists()
    assert "_applied_sha" not in t


def test_delete_before_save_with_shas(vault, no_images):
    """删除先到（电脑上还没建出来），带着手机删掉的那几版：之后到的存（剪藏第一次交的、后来改的）都不建。"""
    rel = "Spark/剪藏/20261122_xhs.md"
    c1 = CLIP0.replace("body", "body tagged")
    assert "已经没有了" in _delete(rel, [_sha(CLIP0), _sha(c1)], keys=["save-key-0001", "save-key-0002"])
    assert "已经删了" in rt.save_file(_task(path=rel, content=CLIP0, images={}, new=True, key="save-key-0001"))
    assert "已经删了" in rt.save_file(_task(path=rel, content=c1, bases=[_sha(CLIP0)], new=True, images={}, key="save-key-0002"))
    # 老版手机（存不带 key）、带 new、跟删掉的一字不差：分不清，不建，任务失败让手机上看得到
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel, content=CLIP0, images={}, new=True))
    assert e.value.kind == "gone" and not (vault / rel).exists()
    assert not (vault / rel).exists()
    # 删掉之前改的、内容没记进墓碑但来历都在墓碑里（另一台设备改的）：gone，不建，手机那份留着
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=rel, content=CLIP0.replace("body", "别的设备改的"), bases=[_sha(CLIP0)], new=True, images={}))
    assert e.value.kind == "gone" and not (vault / rel).exists()


def test_delete_then_late_duplicate_and_reclip(vault, no_images):
    """删后晚到的重复存：不建；删后重新剪藏同一篇（收录时刻不同，内容不一样）：照常建。随手记同样。"""
    rel = "Spark/剪藏/20261122_xhs.md"
    rt.save_file(_task(path=rel, content=CLIP0, images={}))
    _delete(rel)
    assert "已经删了" in rt.save_file(_task(path=rel, content=CLIP0, images={}))
    again = CLIP0.replace("21:30:05", "21:45:00").replace("21:30", "21:45")
    assert "存进了" in rt.save_file(_task(path=rel, content=again, images={}))
    assert (vault / rel).read_text(encoding="utf-8") == again
    j = jt.create("想法", "x", root=str(vault), now=T0)
    v = (vault / j).read_text(encoding="utf-8")
    _delete(j, keys=["jot-key-00001"])
    assert "已经删了" in rt.save_file(_task(path=j, content=v, new=True, key="jot-key-00001")) and not (vault / j).exists()
    # 删了以后又新做了一篇一字不差的（新的 key）：照常建，墓碑清掉
    assert "存进了" in rt.save_file(_task(path=j, content=v, new=True, key="jot-key-00002")) and (vault / j).exists()
    (vault / j).unlink()
    fresh = jt.render("想法", "x", T1)
    assert "存进了" in rt.save_file(_task(path=j, content=fresh, new=True)), "重新写的一条（新建时刻不同）照常建"


# ---------------------------------------------------------------- 手机废纸篓：放回原处越过墓碑；彻底删除时只删这篇用的图片

def test_restore_crosses_tombstone(vault, no_images):
    rel = "Spark/剪藏/20261122_xhs.md"
    rt.save_file(_task(path=rel, content=CLIP0, images={}))
    _delete(rel)
    assert "已经删了" in rt.save_file(_task(path=rel, content=CLIP0, images={})), "不带 restore 的同一份：仍被挡"
    assert "存进了" in rt.save_file(_task(path=rel, content=CLIP0, images={}, restore=True))
    assert (vault / rel).read_text(encoding="utf-8") == CLIP0
    from core import clips
    assert clips.deleted_shas(str(vault), rel) == set(), "放回以后墓碑清掉"


IMG_A, IMG_B, IMG_C = "0123456789abcdef.jpg", "fedcba9876543210.png", "00aa11bb22cc33dd.webp"


def _purge(rel, images):
    return rt.purge_images(rt.parse_task({"type": "purge_images", "path": rel, "images": images}))


@pytest.fixture
def clip_images(vault):
    att = vault / "Spark" / "剪藏" / "attachments"
    att.mkdir(parents=True)
    for n in (IMG_A, IMG_B, IMG_C):
        (att / n).write_bytes(b"img")
    return att


def test_purge_images_only_unreferenced(vault, clip_images):
    trash = vault.parent / "Trash"
    rel = "Spark/剪藏/20261122_删掉的.md"   # 电脑上已经删了（不在笔记库里）
    other = vault / "Spark" / "剪藏" / "20261120_另一篇.md"
    other.write_text(f"# 另一篇\n\n![](attachments/{IMG_B})\n", encoding="utf-8")
    # 只在代码块、普通文字里提到文件名的，也按在用算；别的文件夹里同名图、只写了文件名的，也按在用算
    note = vault / "Spark" / "笔记" / "20261121_记.md"
    note.parent.mkdir(parents=True)
    note.write_text(f"# 记\n\n```\n{IMG_C.upper()}\n```\n", encoding="utf-8")
    imgs = [f"Spark/剪藏/attachments/{n}" for n in (IMG_A, IMG_B, IMG_C)]
    msg = _purge(rel, imgs)
    assert "1 张" in msg and sorted(os.listdir(trash)) == [IMG_A]
    assert (clip_images / IMG_B).exists() and (clip_images / IMG_C).exists()


def test_purge_images_keeps_when_article_still_there_or_unreadable(vault, clip_images):
    rel = "Spark/剪藏/20261122_还在.md"
    (vault / rel).write_text("# 还在\n", encoding="utf-8")
    assert "图片不动" in _purge(rel, [f"Spark/剪藏/attachments/{IMG_A}"]) and (clip_images / IMG_A).exists()
    (vault / rel).unlink()
    # (v7 I) 不是 UTF-8 的文档：按字节找，提到了（GBK 文档里的 ASCII 文件名、大写）算在用，没提到的不挡
    gbk = vault / "其他" / "gbk.md"
    gbk.parent.mkdir()
    gbk.write_bytes("中文 ".encode("gbk") + IMG_B.upper().encode())
    (vault / "Spark" / "剪藏" / "20261101_坏.md").write_bytes(b"# \xff\xfe broken")
    msg = _purge(rel, [f"Spark/剪藏/attachments/{IMG_A}", f"Spark/剪藏/attachments/{IMG_B}"])
    assert "1 张" in msg and not (clip_images / IMG_A).exists() and (clip_images / IMG_B).exists()
    # 彻底读不了的：Spark/ 下的整轮不删；别处的跳过并在结果里列出来
    locked = vault / "Spark" / "剪藏" / "20261101_锁.md"
    locked.write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(rt.TaskError, match="读不出来"):
            _purge(rel, [f"Spark/剪藏/attachments/{IMG_C}"])
        assert (clip_images / IMG_C).exists()
        locked.chmod(0o644)
        locked.unlink()
        far = vault / "其他" / "锁.md"
        far.write_text("x", encoding="utf-8")
        far.chmod(0)
        msg = _purge(rel, [f"Spark/剪藏/attachments/{IMG_C}"])
        assert "其他/锁.md" in msg and not (clip_images / IMG_C).exists()
        far.chmod(0o644)
    finally:
        for p in (locked, vault / "其他" / "锁.md"):
            if p.exists():
                p.chmod(0o644)


def test_purge_images_checks_canvas(vault, clip_images):
    (vault / "白板.canvas").write_text(json.dumps({"nodes": [{"type": "file", "file": f"Spark/剪藏/attachments/{IMG_A}"}]}),
                                       encoding="utf-8")
    assert "0 张" in _purge("Spark/剪藏/20261122_gone.md", [f"Spark/剪藏/attachments/{IMG_A}"])
    assert (clip_images / IMG_A).exists()


@pytest.mark.parametrize("img", ["Spark/剪藏/attachments/../20261120_x.md", "Spark/剪藏/attachments/sub/a.jpg",
                                 "Spark/笔记/attachments/0123456789abcdef.jpg", "/etc/a.jpg", "Spark/剪藏/a.jpg",
                                 "Spark/剪藏/attachments/.hidden.jpg", "Spark/剪藏/attachments/a.txt"])
def test_purge_images_rejects_paths_outside_attachments(vault, img):
    with pytest.raises(rt.TaskError, match="images"):
        _purge("Spark/剪藏/20261122_删掉的.md", [img])


SWIFT_TRASH = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "PhoneTrash.swift")
TRASH_HARNESS = r'''
import Foundation
var out: [String: Any] = [:]
let now = Date(timeIntervalSince1970: 1_800_000_000)
func item(_ id: String, daysAgo: Double, path: String = "Spark/剪藏/a.md", content: String = "x") -> PhoneTrash.Item {
    PhoneTrash.Item(id: id, path: path, title: id, kind: "剪藏", deletedAt: now.timeIntervalSince1970 - daysAgo * 86400, content: content)
}
let items = [item("d29", daysAgo: 29), item("d30", daysAgo: 30), item("d31", daysAgo: 31), item("future", daysAgo: -3)]
// 自动清除只看网络时间减删除时刻
func purge(_ i: PhoneTrash.Item, device: Date = now, net: Date?) -> Bool { PhoneTrash.purgeable(i, deviceNow: device, networkNow: net) }
let hour = 3600.0
let at0 = PhoneTrash.Item(id: "h", path: "Spark/剪藏/a.md", title: "h", kind: "剪藏", deletedAt: now.timeIntervalSince1970 - 719 * hour, content: "x")
let at1 = PhoneTrash.Item(id: "h", path: "Spark/剪藏/a.md", title: "h", kind: "剪藏", deletedAt: now.timeIntervalSince1970 - 720 * hour, content: "x")
let fresh = PhoneTrash.Item(id: "f", path: "Spark/剪藏/a.md", title: "f", kind: "剪藏", deletedAt: now.timeIntervalSince1970 - hour, content: "x")
out["purge"] = [purge(at1, net: nil),                                   // 没联网
                purge(at0, net: now), purge(at1, net: now),             // 719 小时不清、720 小时清
                purge(fresh, device: now.addingTimeInterval(365 * 86400), net: now),   // 手机钟调快一年、网络时间正常
                purge(item("future", daysAgo: -3), net: now)]          // 删除时刻晚于网络时间
out["left"] = items.map { PhoneTrash.daysLeft($0, now: now) }
// 移入、彻底删除
var t = PhoneTrash.adding([], items[0]); t = PhoneTrash.adding(t, items[2]); t = PhoneTrash.adding(t, items[0])
out["added"] = t.map(\.id)
out["removed"] = PhoneTrash.removing(t, id: "d29").map(\.id)
// 放回：原路径空着放回原处；被占了换名字（已有的 _N 先去掉）
let taken: Set<String> = ["Spark/剪藏/a.md", "Spark/剪藏/a_2.md", "Spark/剪藏/b_2.md"]
out["restore"] = ["Spark/笔记/x.md", "Spark/剪藏/a.md", "Spark/剪藏/b_2.md"].map { PhoneTrash.restorePath($0) { taken.contains($0) } }
var it = items[0]; it.deleteTaskID = "gh:3"; it.unsent = true
out["roundtrip"] = (try? JSONDecoder().decode(PhoneTrash.Item.self, from: JSONEncoder().encode(it))) == it
let oldJSON = #"{"id":"o","path":"Spark/剪藏/a.md","title":"t","kind":"剪藏","deletedAt":1,"content":"c"}"#
out["old"] = (try? JSONDecoder().decode(PhoneTrash.Item.self, from: Data(oldJSON.utf8))).map { "\($0.deleteTaskID)|\($0.unsent)|\($0.clockUnverified)" } ?? "fail"
// 图片
let doc = "Spark/剪藏/20261122_x.md"
out["paths"] = ["attachments/0123456789abcdef.jpg", "./attachments/fedcba9876543210.png", "attachments%2F00aa11bb22cc33dd.webp",
                "attachments/../x.jpg", "/abs/a.jpg", "https://e.com/a.jpg", "attachments/sub/a.jpg", "a.jpg"]
    .map { PhoneTrash.imagePath(src: $0, docPath: doc) ?? "-" }
let a = "Spark/剪藏/attachments/0123456789abcdef.jpg", b = "Spark/剪藏/attachments/fedcba9876543210.png",
    c = "Spark/剪藏/attachments/00aa11bb22cc33dd.webp", d = "Spark/剪藏/attachments/1111222233334444.gif"
func orph(_ texts: [String], unreadable: Bool = false, syncing: Bool = false) -> String {
    PhoneTrash.orphans(candidates: [a, b, c, d], texts: texts, unreadable: unreadable, syncing: syncing)
        .map { $0.map { ($0 as NSString).lastPathComponent.prefix(4) }.joined(separator: ",") } ?? "nil"
}
out["orphans"] = [
    orph(["![[0123456789abcdef.jpg]]", "```\nFEDCBA9876543210.PNG\n```", "../剪藏/attachments/00aa11bb22cc33dd.webp"]),
    orph(["![](attachments/1111222233334444.gif)"]),      // 废纸篓里另一条、待确认的修改里引用着
    orph([], unreadable: true), orph([], syncing: true), orph(["别的文件夹/attachments/0123456789abcdef.jpg"])]
let day = 86400.0, t0 = now.timeIntervalSince1970
// 没核对过的删除时刻：手机钟跟网络时间差 5 分钟内保留原时刻；差得多改成这次的网络时间
var unv = item("u", daysAgo: 10)
let keep = PhoneTrash.reconciled(unv, deviceNow: now.addingTimeInterval(200), networkNow: now)
let moved = PhoneTrash.reconciled(unv, deviceNow: now.addingTimeInterval(-86400 * 40), networkNow: now)
unv.clockUnverified = false
let verified = PhoneTrash.reconciled(unv, deviceNow: now.addingTimeInterval(-86400 * 40), networkNow: now)
// 复现：删的时候手机钟慢了 10 天（删除时刻早了 10 天），钟改回来以后第一次联网核对：删除时刻改成网络时间，真实过了 21 天也不清
let slow = PhoneTrash.reconciled(item("slow", daysAgo: 10), deviceNow: now, networkNow: now)
let future = PhoneTrash.reconciled(item("fut", daysAgo: -5), deviceNow: now, networkNow: now)
out["reconcile"] = [keep.deletedAt == t0, keep.clockUnverified, moved.deletedAt == t0, moved.clockUnverified,
                    verified.deletedAt == t0 - 10 * day,
                    PhoneTrash.reconciled(item("n", daysAgo: 1), deviceNow: now, networkNow: nil).clockUnverified,
                    purge(slow, net: now.addingTimeInterval(21 * day)), future.deletedAt == t0,
                    purge(slow, net: now.addingTimeInterval(30 * day))]
let st1 = PhoneTrash.deletionStamp(deviceNow: now, offset: 30), st2 = PhoneTrash.deletionStamp(deviceNow: now, offset: nil)
out["stamp"] = [st1.at == t0 + 30, st1.unverified, st2.at == t0, st2.unverified]
// Date 头的几种写法和坏输入
out["httpDate"] = ["Sun, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT", "Sun Nov  6 08:49:37 1994",
                   " Sun,  06 Nov 1994 08:49:37 GMT ", "", "yesterday", "Sun, 32 Nov 1994 08:49:37 GMT", "1994-11-06T08:49:37Z"]
    .map { PhoneTrash.httpDate($0).map { Int($0.timeIntervalSince1970) } ?? -1 }
// NFD 写的引用、+ 写的空格
out["nfc"] = PhoneTrash.orphans(candidates: ["Spark/剪藏/attachments/图片\u{e9}.jpg", "Spark/剪藏/attachments/plus a.jpg"],
                                texts: ["![](attachments/图片e\u{301}.jpg) ![](attachments/plus+a.jpg)"], unreadable: false, syncing: false) ?? ["nil"]
// 放回时装回去的待确认记录；带记录的条目编码读回
var withRec = item("r", daysAgo: 1)
withRec.record = PendingEdits.Record(content: "旧", bases: ["b0", "b1"], editedAt: 5, taskIDs: ["t1"], created: true)
withRec.deleteKey = "del-key"; withRec.deleteFailed = true
let rr = PhoneTrash.restoredRecord(withRec, target: withRec.path, now: 3)
var withBase = item("b", daysAgo: 1); withBase.base = "m0"
out["restored"] = [rr.bases.joined(separator: ","), "\(rr.created)", "\(rr.editedAt)", "\(rr.taskIDs.count)",
                   PhoneTrash.restoredRecord(withBase, target: withBase.path, now: 9).bases.joined(separator: ","),
                   "\(PhoneTrash.restoredRecord(withBase, target: "Spark/剪藏/a_2.md", now: 9).created)",
                   "\(PhoneTrash.restoredRecord(item("n", daysAgo: 1), target: "Spark/剪藏/a.md", now: 9).created)"]
withRec.record?.images = ["Spark/剪藏/attachments/0123456789abcdef.jpg": "https://e.com/a.jpg"]
out["recRoundtrip"] = (try? JSONDecoder().decode(PhoneTrash.Item.self, from: JSONEncoder().encode(withRec))) == withRec
out["imagesKept"] = PhoneTrash.restoredRecord(withRec, target: "Spark/剪藏/a_2.md", now: 9).images.count
let oldRecJSON = #"{"content":"x","bases":[],"editedAt":3,"taskIDs":["a"]}"#
out["oldRecImages"] = (try? JSONDecoder().decode(PendingEdits.Record.self, from: Data(oldRecJSON.utf8))).map { $0.images.count } ?? -1
// 电脑没删成的条目放回：书架上这篇后来又改过（有待确认的修改、或者内容不同）→ 换名字另存；没改过 → 原路径
var df = item("df", daysAgo: 1, content: "废纸篓里这版"); df.deleteFailed = true; df.unsent = true
func target(_ i: PhoneTrash.Item, _ shelf: String?, _ pend: Bool) -> String {
    let t = PhoneTrash.restoreTarget(i, shelfText: shelf, hasPending: pend) { $0 == "Spark/剪藏/a.md" }
    return "\(t.path)|\(t.renamed)"
}
out["target"] = [target(df, "废纸篓里这版", false), target(df, "书架上改过的", false), target(df, "废纸篓里这版", true),
                 target(item("ok", daysAgo: 1), nil, false), target(item("ok2", daysAgo: 1, path: "Spark/剪藏/b.md"), nil, false)]
// 没删成、还有没交的修改：31 天也不清；没删成但没有没交的：照常清
var failedUnsent = item("fu", daysAgo: 31); failedUnsent.deleteFailed = true; failedUnsent.unsent = true
var failedClean = item("fc", daysAgo: 31); failedClean.deleteFailed = true
out["failedExpire"] = [failedUnsent, failedClean].filter { purge($0, net: now) }.map(\.id)
out["hasUnsent"] = [PhoneTrash.hasUnsent(failedUnsent), PhoneTrash.hasUnsent(failedClean), PhoneTrash.hasUnsent(withRec)]
// 不是 UTF-8 的文档按字节找：GBK 文档里的大写文件名、NFD 写的名字
var gbk = Data([0xD6, 0xD0, 0x20]); gbk.append(Data("0123456789ABCDEF.JPG".utf8))
let nfdName = Data("图片e\u{301}.jpg".utf8) + Data([0xFF])
out["raw"] = PhoneTrash.orphans(candidates: [a, b, "Spark/剪藏/attachments/图片\u{e9}.jpg"], texts: [], raw: [gbk, nfdName],
                                unreadable: false, syncing: false) ?? ["nil"]
// 有 BOM 却解不开（奇数长度）：每张图都算在用
out["badBom"] = PhoneTrash.orphans(candidates: [a, b], texts: [], raw: [Data([0xFF, 0xFE, 0x41])], unreadable: false, syncing: false) ?? ["nil"]
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_phone_trash_rules(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(TRASH_HARNESS, encoding="utf-8")
    exe = tmp_path / "trash"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_TRASH, SWIFT_PENDING, str(main)], capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout)
    assert got["purge"] == [False, False, True, False, False], \
        "没联网不清；719 小时不清、720 小时清；手机钟调快一年不清；删除时刻晚于网络时间不清"
    assert got["left"] == [1, 0, 0, 30]
    assert got["reconcile"] == [True, False, True, False, True, True, False, True, True], \
        "没核对过的一律改成网络时间（钟慢 10 天时删的，真实 21 天不清、30 天清）；将来的拉回到网络时间；核对过的不动"
    assert got["stamp"] == [True, False, True, True]
    assert got["httpDate"] == [784111777, 784111777, 784111777, 784111777, -1, -1, -1, -1]
    assert got["added"] == ["d29", "d31"] and got["removed"] == ["d31"]
    assert got["restore"] == ["Spark/笔记/x.md", "Spark/剪藏/a_3.md", "Spark/剪藏/b_3.md"]
    assert got["roundtrip"] is True and got["old"] == "|false|true", "旧格式条目能读，没有新字段的当成删除时刻没核对过"
    assert got["paths"] == ["Spark/剪藏/attachments/0123456789abcdef.jpg", "Spark/剪藏/attachments/fedcba9876543210.png",
                            "Spark/剪藏/attachments/00aa11bb22cc33dd.webp", "-", "-", "-", "-", "-"]
    assert got["orphans"] == ["1111", "0123,fedc,00aa", "nil", "nil", "fedc,00aa,1111"], \
        "认得出的、代码块里的、带相对路径的都算在用；读不出来、正在同步：这一轮不删；别的文件夹同名、只写了文件名的也算在用"
    assert got["nfc"] == [], "NFD 写的引用、+ 写的空格都算在用"
    assert got["restored"] == ["b0,b1", "true", "6", "0", "m0", "true", "true"]
    assert got["recRoundtrip"] is True and got["imagesKept"] == 1 and got["oldRecImages"] == 0
    assert got["target"] == ["Spark/剪藏/a.md|false", "Spark/剪藏/a_2.md|true", "Spark/剪藏/a_2.md|true",
                             "Spark/剪藏/a_2.md|true", "Spark/剪藏/b.md|false"], "书架上改过的不盖掉，另存一份"
    assert got["failedExpire"] == ["fc"] and got["hasUnsent"] == [True, False, True], "没删成还有没交的修改：不清"
    assert got["raw"] == ["Spark/剪藏/attachments/fedcba9876543210.png"], "按字节找到的算在用"
    assert got["badBom"] == [], "有 BOM 却解不开的文件：每张图都算在用"


# ---------------------------------------------------------------- 第六轮：放回原处跟晚到的删除、电脑上已有的那篇、扫整个笔记库

def test_restore_then_late_delete_is_cancelled(vault):
    """(v6 第 2 项) 数据线取走删除的同时点了放回原处：放回的存先到、删除后到。删除带的 key 被放回取消了，不执行。"""
    rel = jt.create("r2", "原文", root=str(vault), now=T0)
    v0 = (vault / rel).read_text(encoding="utf-8")
    t = _task(path=rel, content=v0, bases=[_sha(v0)], restore=True, cancels=["del-key-00002"])
    assert "已经是这一版" in rt.save_file(t)
    assert "撤回" in _delete(rel, [_sha(v0)], key="del-key-00002") and (vault / rel).exists()
    assert not (vault.parent / "Trash").exists()
    v1 = jt.updated(v0, "", "手机改", T1)
    assert "存进了" in rt.save_file(_task(path=rel, content=v1, bases=[_sha(v0)])), "之后照常改"


def test_delete_then_restore_recreates(vault):
    """删除先执行、放回的存后到：墓碑清掉，建回来。"""
    rel = jt.create("r1", "原文", root=str(vault), now=T0)
    v0 = (vault / rel).read_text(encoding="utf-8")
    _delete(rel, [_sha(v0)], key="del-key-00001")
    assert not (vault / rel).exists()
    t = _task(path=rel, content=v0, bases=[_sha(v0)], restore=True, cancels=["del-key-00001"])
    assert "存进了" in rt.save_file(t) and (vault / rel).read_text(encoding="utf-8") == v0


def test_restore_never_overwrites_mac_edits(vault, no_images):
    """(v6 第 4、6 项) 放回原处时电脑上那篇还在、电脑上改过（划了线）：带 bases 的判冲突；不带 bases 的也不覆盖。
    手机那份存进 note-conflicts，电脑上的划线留着。"""
    k = "Spark/剪藏/20261122_k.md"
    rt.save_file(_task(path=k, content=CLIP0, images={}))
    marked = CLIP0.replace("body", "body ==Mac划的线==")
    (vault / k).write_text(marked, encoding="utf-8")
    for extra in ({"bases": [_sha(CLIP0)]}, {}):
        with pytest.raises(rt.TaskError) as e:
            rt.save_file(_task(path=k, content=CLIP0.replace("body", "body 手机改"), images={}, restore=True, **extra))
        assert e.value.kind == "conflict" and "Mac划的线" in (vault / k).read_text(encoding="utf-8")
    j = jt.create("r6", "原文", root=str(vault), now=T0)
    v0 = (vault / j).read_text(encoding="utf-8")
    jt.update(j, "", "电脑上改的", jt.sha256(v0), root=str(vault))
    for extra in ({"bases": [_sha(v0)]}, {}):
        with pytest.raises(rt.TaskError) as e:
            rt.save_file(_task(path=j, content=jt.updated(v0, "", "手机没交的", T1), restore=True, **extra))
        assert e.value.kind == "conflict", "放回的不算新建：不是 taken，手机上照常提醒"
    assert "电脑上改的" in (vault / j).read_text(encoding="utf-8")
    assert len(_conflicts(vault)) == 4
    # 电脑上没改过：带 bases 的放回照常写进去
    j2 = jt.create("r7", "原文", root=str(vault), now=T0)
    w0 = (vault / j2).read_text(encoding="utf-8")
    w1 = jt.updated(w0, "", "手机没交的", T1)
    assert "存进了" in rt.save_file(_task(path=j2, content=w1, bases=[_sha(w0)], restore=True))


def test_purge_images_scans_whole_vault_and_nfc(vault, clip_images):
    """(v6 第 8、9 项) 图片只被 Spark/、output/ 以外的笔记引用：不动；引用写成 NFD、+、%20、大小写不同：也算在用。"""
    import unicodedata
    (vault / "日记").mkdir()
    (vault / "日记" / "今天.md").write_text(f"![[{IMG_A}]]", encoding="utf-8")
    nm = "图片é.jpg"
    (clip_images / unicodedata.normalize("NFC", nm)).write_bytes(b"x")
    (clip_images / "plus a.jpg").write_bytes(b"x")
    (vault / "Spark" / "剪藏" / "keep.md").write_text(
        f"![](attachments/{unicodedata.normalize('NFD', nm)})\n![](attachments/plus+a.jpg) <img src='attachments/plus%20A.JPG'>",
        encoding="utf-8")
    imgs = [f"Spark/剪藏/attachments/{n}" for n in (IMG_A, unicodedata.normalize("NFC", nm), "plus a.jpg")]
    assert "0 张" in _purge("Spark/剪藏/20261122_gone.md", imgs)
    assert all((clip_images / os.path.basename(i)).exists() for i in imgs)


def test_v7_clip_same_name_other_device_is_taken(vault, no_images):
    """(v7 F) iPhone 剪了一篇、划了线；iPad 当天剪同一篇（同名，new、不带 bases）：不覆盖，kind=taken，iPad 那份进 note-conflicts。"""
    f = "Spark/剪藏/20261122_f.md"
    a = CLIP0.replace("body", "A的笔记")
    rt.save_file(_task(path=f, content=a, new=True, key="devA-key-1", images={}))
    (vault / f).write_text(a.replace("A的笔记", "A的笔记 ==划线=="), encoding="utf-8")
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=f, content=CLIP0.replace("body", "B的"), new=True, key="devB-key-1", images={}))
    assert e.value.kind == "taken" and "==划线==" in (vault / f).read_text(encoding="utf-8")
    assert len(_conflicts(vault)) == 1
    # 老版手机（不带 new、不带 bases）照旧覆盖：已知的旧问题，不在这次范围
    rt.save_file(_task(path=f, content=CLIP0, images={}))
    assert (vault / f).read_text(encoding="utf-8") == CLIP0


def test_v7_cancels_kept_and_recorded_before_validation(vault):
    """(v7 C2) 放回的存内容不合规、解析时被拒：取消照样记下，晚到的删除不执行。
    (v7 C3) 之后改了 17 次：撤回记录不被截掉。"""
    rel = jt.create("rr", "原文", root=str(vault), now=T0)
    with pytest.raises(rt.TaskError, match="content"):
        rt.parse_task({"type": "save", "path": rel, "content": "", "replace": True, "restore": True, "cancels": ["del-key-000r"]})
    assert "撤回" in _delete(rel, key="del-key-000r") and (vault / rel).exists()
    v = (vault / rel).read_text(encoding="utf-8")
    rt.save_file(_task(path=rel, content=v, restore=True, cancels=["del-key-000p"]))
    for i in range(17):
        n = jt.updated(v, "", f"e{i}", T1)
        rt.save_file(_task(path=rel, content=n, bases=[_sha(v)]))
        v = n
    assert "撤回" in _delete(rel, key="del-key-000p") and (vault / rel).exists()


def test_v7_late_duplicates_with_seen_keys_not_recreated(vault, no_images):
    """(v7 E) 另一台设备第一次交的存（new、它自己的 key）电脑收过，这篇删了以后它又重复到：不建。
    (v7 D) 同一台手机第一次交的存的 key 已经不在手机交来的 16 个里：电脑自己见过，也不建。
    电脑从没见过的新 key、内容一字不差：用户新做的，照建。"""
    e = "Spark/剪藏/20261122_e.md"
    e0 = CLIP0.replace("# c", "# e")
    rt.save_file(_task(path=e, content=e0, new=True, key="devB-key-01", images={}))
    _delete(e, [_sha(e0)], key="del-key-00e1")
    assert "已经删了" in rt.save_file(_task(path=e, content=e0, new=True, key="devB-key-01", images={})) and not (vault / e).exists()
    d = "Spark/剪藏/20261122_d.md"
    d0 = CLIP0.replace("# c", "# d")
    rt.save_file(_task(path=d, content=d0, new=True, key="mine-key-00", images={}))
    _delete(d, key="del-key-00d1", keys=[f"mine-key-{i:02d}" for i in range(1, 17)])
    assert "已经删了" in rt.save_file(_task(path=d, content=d0, new=True, key="mine-key-00", images={})) and not (vault / d).exists()
    assert "存进了" in rt.save_file(_task(path=d, content=d0, new=True, key="fresh-key-99", images={})) and (vault / d).exists()


def test_purge_images_reads_utf16_with_bom(vault, clip_images):
    """带 BOM 的 UTF-16 文档里的引用也认得出：不删。"""
    (vault / "其他.md").write_bytes(f"![](Spark/剪藏/attachments/{IMG_A})".encode("utf-16"))
    (vault / "其他2.md").write_bytes(b"\xfe\xff" + f"![[{IMG_B}]]".encode("utf-16-be"))
    msg = _purge("Spark/剪藏/20261122_gone.md", [f"Spark/剪藏/attachments/{IMG_A}", f"Spark/剪藏/attachments/{IMG_B}"])
    assert "0 张" in msg and (clip_images / IMG_A).exists() and (clip_images / IMG_B).exists()


def test_purge_images_bad_utf16_counts_as_used(vault, clip_images):
    """FF FE 开头、奇数长度（解不开）的文件：写着的图和没提到的图都算在用，不动。"""
    (vault / "坏.md").write_bytes(b"\xff\xfe" + IMG_A.encode("utf-16-le") + b"\x00")
    msg = _purge("Spark/剪藏/20261122_gone.md", [f"Spark/剪藏/attachments/{IMG_A}", f"Spark/剪藏/attachments/{IMG_B}"])
    assert "0 张" in msg and (clip_images / IMG_A).exists() and (clip_images / IMG_B).exists()
