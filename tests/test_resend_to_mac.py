"""手机上新存的剪藏交给电脑时被判「电脑上已经没有这篇了」（gone）：
- 回归：手机上新建 → 没同步前改一次或几次 → 同步，交出去的任务带 new，电脑照建，不判 gone；
  旧版（4340d72）留下的、没有 created 这一项的记录再改一次也带 new；
- 「重新交给电脑」：手机上这一版当成新的一篇交（不带 bases，带 new 和 restore）。电脑上没有就建；有墓碑清掉再建；
  已经有内容不同的一篇不覆盖（冲突，手机这一版存进 note-conflicts）。
手机那边的规则在 apps/mobile-ios/Spark/PendingEdits.swift，用 swiftc 单独编出来跑。"""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from core import clips
from core import jottings as jt
from core import remote_tasks as rt

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT_PENDING = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "PendingEdits.swift")

T0 = datetime(2026, 11, 22, 13, 30, 5, tzinfo=timezone.utc)
CLIP0 = ('---\ntitle: "c"\nsource: "s"\nurl: "https://e.com"\nclipped: 2026-11-22\nclipped_at: "2026-11-22T15:59:06+08:00"\n---\n\n'
         '# c\n\n- 来源：s\n- 收录时间：2026年11月22日，15:59（UTC+8）\n\n## 正文\n\nbody\n')


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Spark" / "剪藏").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setattr(rt, "note_conflicts_dir", lambda: str(tmp_path / "conflicts"))
    monkeypatch.setattr(rt, "trash_dir", lambda: str(tmp_path / "Trash"))
    monkeypatch.setattr(rt.clips, "download_images", lambda images, root: (0, 0))
    return v


def _task(**kw):
    return rt.parse_task(dict({"type": "save", "replace": True}, **kw))


def _conflicts(vault):
    d = vault.parent / "conflicts"
    return sorted(p.read_text(encoding="utf-8") for p in d.iterdir()) if d.is_dir() else []


def _delete(rel):
    return rt.delete_file(rt.parse_task({"type": "delete", "path": rel, "shas": [], "keys": [], "edited_at": 1795000005000}))


# ---------------------------------------------------------------- 电脑：回归

def test_old_phone_task_shape_is_gone_new_shape_is_created(vault):
    """4340d72 之前的手机：新存的剪藏改一次，发件箱里「存进笔记库」那份被拿掉，只交了带 base（改之前那份的指纹）、
    不带 new 的「更新笔记库」：电脑上本来就没有，判 gone，内容只在手机上（11-22 那四篇就是这样）。
    现在的手机：同样的步骤交的是 new + bases：照建。"""
    c1 = CLIP0.replace("body", "body ==划线==")
    c2 = c1.replace("==划线==", "==划线== 又改")
    old = "Spark/剪藏/20261122_old.md"
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_task(path=old, content=c1, base=_sha(CLIP0), images={}))
    assert e.value.kind == "gone" and not (vault / old).exists()
    new = "Spark/剪藏/20261122_new.md"
    assert "存进了" in rt.save_file(_task(path=new, content=c2, base=_sha(CLIP0), bases=[_sha(CLIP0), _sha(c1)], new=True,
                                          images={}, edited_at=1795000000000))
    assert (vault / new).read_text(encoding="utf-8") == c2 and _conflicts(vault) == []


# ---------------------------------------------------------------- 电脑：重新交给电脑（new + restore，不带 bases）

def _resend(path, content, **kw):
    return _task(path=path, content=content, new=True, restore=True, edited_at=1795000010000, **kw)


def test_resend_creates_missing_file(vault):
    rel = "Spark/剪藏/20261122_开源.md"
    t = _resend(rel, CLIP0, images={})
    assert t["new"] and t["restore"] and "base" not in t and "bases" not in t
    assert "存进了" in rt.save_file(t)
    assert (vault / rel).read_text(encoding="utf-8") == CLIP0 and t["_applied_sha"] == rt.blob_sha(CLIP0.encode())
    j = "Spark/笔记/20261122_想法.md"
    v = jt.render("想法", "只在手机上", T0)
    assert "存进了" in rt.save_file(_resend(j, v))
    assert (vault / j).read_text(encoding="utf-8") == v


def test_resend_clears_tombstone_and_creates(vault):
    """电脑上删过（墓碑里有这一份）：不带 restore 的同一份被挡；重新交给电脑的照建，墓碑清掉"""
    rel = "Spark/剪藏/20261122_水下.md"
    rt.save_file(_task(path=rel, content=CLIP0, images={}))
    _delete(rel)
    assert not (vault / rel).exists() and clips.deleted_shas(str(vault), rel)
    assert "已经删了" in rt.save_file(_task(path=rel, content=CLIP0, images={})), "旧任务重来：仍被挡"
    assert "存进了" in rt.save_file(_resend(rel, CLIP0, images={}))
    assert (vault / rel).read_text(encoding="utf-8") == CLIP0
    assert clips.deleted_shas(str(vault), rel) == set(), "重新交给电脑以后墓碑清掉"
    # 手机上这一版跟删掉的不一样（删了以后手机上又改过）：也照建
    j = jt.create("想法", "x", root=str(vault), now=T0)
    _delete(j)
    v2 = jt.render("想法", "手机上改过的", T0)
    assert "存进了" in rt.save_file(_resend(j, v2))
    assert (vault / j).read_text(encoding="utf-8") == v2 and clips.deleted_shas(str(vault), j) == set()


def test_resend_never_overwrites_different_file(vault):
    """电脑上这个名字已经有内容不同的一篇：不覆盖，判冲突（不是 taken，不另起名字），手机这一版存进 note-conflicts"""
    rel = "Spark/剪藏/20261122_富士.md"
    mac = CLIP0.replace("body", "电脑上的另一版")
    (vault / rel).write_text(mac, encoding="utf-8")
    phone = CLIP0.replace("body", "手机上这一版")
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_resend(rel, phone, images={}))
    assert e.value.kind == "conflict"
    assert (vault / rel).read_text(encoding="utf-8") == mac
    assert _conflicts(vault) == [phone]
    j = jt.create("同名", "电脑上的", root=str(vault), now=T0)
    before = (vault / j).read_text(encoding="utf-8")
    with pytest.raises(rt.TaskError) as e:
        rt.save_file(_resend(j, jt.render("同名", "手机上的", T0)))
    assert e.value.kind == "conflict" and (vault / j).read_text(encoding="utf-8") == before
    # 电脑上已经是这一份：算做过
    same = "Spark/剪藏/20261122_同.md"
    (vault / same).write_text(CLIP0, encoding="utf-8")
    assert rt.save_file(_resend(same, CLIP0, images={}))
    assert (vault / same).read_text(encoding="utf-8") == CLIP0


# ---------------------------------------------------------------- 手机：PendingEdits.swift 编出来跑

HARNESS = r'''
import Foundation
var out: [String: Any] = [:]
func fields(_ r: PendingEdits.Record) -> String {
    let f = PendingEdits.taskFields(r, review: false)
    return "new=\(f["new"] as? Bool ?? false) restore=\(f["restore"] as? Bool ?? false) bases=\((f["bases"] as? [String])?.count ?? 0)"
}
func show(_ o: PendingEdits.Outcome) -> String {
    switch o { case .remove: "remove"; case .applied: "applied"; case .conflict(let k): "conflict:\(k)"; case .gone: "gone"
               case .resubmit: "resubmit"; case .pending(let w): w ? "pending-rewrite" : "pending" }
}
// 回归：剪藏刚存好（Clips.deliver 记的 created 记录，交了 t0）→ 没同步前又改两次。每次起点都是上一份：发件箱里上一份拿掉，
// 交出去的都带 new 和整条 bases
var c0 = PendingEdits.Record(content: "c0", bases: [], editedAt: 1, taskIDs: [], created: true)
c0 = PendingEdits.submitted(c0, taskID: "t0")
let c1 = PendingEdits.submitted(PendingEdits.edited(previous: c0, startText: "c0", onMac: false, content: "c1", now: 2), taskID: "t1")
let c2 = PendingEdits.submitted(PendingEdits.edited(previous: c1, startText: "c1", onMac: false, content: "c2", now: 3), taskID: "t2")
out["chain"] = [fields(c0), fields(c1), fields(c2)]
out["drop"] = [PendingEdits.mayDrop(pendingContent: "c0", editedFrom: "c0"), PendingEdits.mayDrop(pendingContent: "c1", editedFrom: "c1")]
// 电脑上还没有、任务还在等 / 查不到结果：不判「没有了」
out["notGone"] = [show(PendingEdits.reconcile(c2, deletedLocally: false, treeSHA: nil, last: PendingEdits.Last(state: "waiting"), held: false, disk: "c2")),
                  show(PendingEdits.reconcile(c2, deletedLocally: false, treeSHA: nil, last: PendingEdits.Last(state: nil), held: false, disk: "c2"))]
// 旧版（4340d72）给新建的一篇记的记录：bases 空、没有 created 这一项。再改一次：认出是新建的，带 new
let legacy = try! JSONDecoder().decode(PendingEdits.Record.self, from: Data(#"{"content":"L0","bases":[],"editedAt":5,"taskIDs":["x"]}"#.utf8))
let l1 = PendingEdits.edited(previous: legacy, startText: "L0", onMac: false, content: "L1", now: 6)
out["legacy"] = [fields(legacy), fields(l1)]
// 复盘（chainNew false）照旧不带 new
let review = PendingEdits.edited(previous: legacy, startText: "L0", onMac: false, content: "R1", now: 6, chainNew: false)
out["review"] = PendingEdits.taskFields(review, review: true).isEmpty
// 电脑上有的一篇第一次改：照旧不是新建
let mac = PendingEdits.edited(previous: nil, startText: "m0", onMac: true, content: "m1", now: 1)
let mac2 = PendingEdits.edited(previous: mac, startText: "m1", onMac: true, content: "m2", now: 2)
out["mac"] = [fields(mac), fields(mac2)]

// 什么时候算「电脑上没有」
func rec(_ state: String, _ kind: String?, earlier: Bool = false) -> PendingEdits.Record {
    var r = PendingEdits.Record(content: "p", bases: ["b"], editedAt: 1, taskIDs: ["t"], state: state, failKind: kind)
    r.failedEarlier = earlier
    return r
}
func stuck(_ r: PendingEdits.Record?, _ stranded: Bool = false, _ legacy: Bool = false) -> String {
    "\(PendingEdits.stuck(record: r, stranded: stranded, legacyGone: legacy))"
}
out["stuck"] = [stuck(rec("gone", nil)), stuck(rec("conflict", "gone")), stuck(rec("conflict", "gone", earlier: true)),
                stuck(rec("conflict", "conflict")), stuck(rec("conflict", "taken")), stuck(rec("conflict", "rejected")),
                stuck(rec("pending", nil)), stuck(nil, true), stuck(nil, false, true), stuck(nil), stuck(rec("pending", nil), true, true)]
out["canResend"] = [PendingEdits.canResend(.gone, newDocPath: true), PendingEdits.canResend(.stranded, newDocPath: true),
                    PendingEdits.canResend(.legacyGone, newDocPath: true), PendingEdits.canResend(.none, newDocPath: true),
                    PendingEdits.canResend(.gone, newDocPath: false)]

// 重新交给电脑的记录：不带 bases，带 new、restore；报过失败的任务不带；原图地址合在一起（记录里的优先）
var gone = rec("conflict", "gone")
gone.editedAt = 50
gone.images = ["Spark/剪藏/attachments/a.png": "https://old/a"]
let r = PendingEdits.resent(content: "phone", previous: gone,
                            images: ["Spark/剪藏/attachments/a.png": "https://outbox/a", "Spark/剪藏/attachments/b.png": "https://outbox/b"], now: 10)
out["resent"] = [fields(r), "\(r.taskIDs.count)", "\(r.editedAt)", r.state, "\(r.created)", "\(r.restore)",
                 r.images.sorted { $0.key < $1.key }.map(\.value).joined(separator: ",")]
let fresh = PendingEdits.resent(content: "phone", previous: nil, images: [:], now: 10)
out["fresh"] = [fields(fresh), "\(fresh.editedAt)"]
// 交出去以后：还在等 → pending；电脑建出来了 → applied；电脑上有另一篇 → 冲突（提醒照旧）
let sent = PendingEdits.submitted(r, taskID: "n1")
out["after"] = [show(PendingEdits.reconcile(sent, deletedLocally: false, treeSHA: nil, last: PendingEdits.Last(state: "waiting"), held: false, disk: "phone")),
                show(PendingEdits.reconcile(sent, deletedLocally: false, treeSHA: PendingEdits.blobSHA("phone"), last: PendingEdits.Last(state: "done"), held: false, disk: "phone")),
                show(PendingEdits.reconcile(sent, deletedLocally: false, treeSHA: "别的", last: PendingEdits.Last(state: "failed", kind: "conflict"), held: false, disk: "phone")),
                stuck(PendingEdits.markConflict(sent, kind: "conflict", statuses: ["n1": PendingEdits.Last(state: "failed", kind: "conflict")]))]
// 交出去以后、同步之前又改了一次：照旧带 new、restore，bases 从这一份接起
let again = PendingEdits.edited(previous: sent, startText: "phone", onMac: false, content: "phone2", now: 11)
out["again"] = fields(again)
// 电脑收下了（文件树里是这一份）：不再带 new、restore
let settled = PendingEdits.settled(again, treeSHA: PendingEdits.blobSHA("phone"), applied: [PendingEdits.blobSHA("phone")])
out["settled"] = fields(settled)
let back = try! JSONDecoder().decode(PendingEdits.Record.self, from: JSONEncoder().encode(sent))
out["roundtrip"] = back == sent && back.restore

// 旧版交错了的那几篇：发件箱里还留着失败的存、结果是 gone、手机上还有、没有待确认记录
let outbox: [(id: String, path: String)] = [("o1", "A"), ("o2", "B"), ("o3", "C"), ("o4", "D"), ("o5", "E"), ("o6", "F")]
let results: [String: PendingEdits.Last] = ["o1": PendingEdits.Last(state: "failed", kind: "gone"),
                                            "o2": PendingEdits.Last(state: "failed", kind: "conflict"),
                                            "o3": PendingEdits.Last(state: "failed", kind: "gone"),
                                            "o4": PendingEdits.Last(state: "failed", kind: "gone"),
                                            "o5": PendingEdits.Last(state: "waiting")]
out["legacyGone"] = PendingEdits.legacyGone(outbox: outbox, results: results, recorded: ["C"], onPhone: ["A", "B", "C", "E", "F"]).sorted()
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_new_doc_edits_and_resend(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(HARNESS, encoding="utf-8")
    exe = tmp_path / "pending"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_PENDING, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout)
    assert got["chain"] == ["new=true restore=false bases=0", "new=true restore=false bases=1", "new=true restore=false bases=2"]
    assert got["drop"] == [True, True]
    assert got["notGone"] == ["pending", "pending"]
    assert got["legacy"] == ["new=false restore=false bases=0", "new=true restore=false bases=1"], \
        "旧版记录再改一次：带 new，第一份没到电脑时电脑照建，不判 gone"
    assert got["review"] is True
    assert got["mac"] == ["new=false restore=false bases=1", "new=false restore=false bases=2"]
    assert got["stuck"] == ["gone", "gone", "none", "none", "none", "none", "none", "stranded", "legacyGone", "none", "none"]
    assert got["canResend"] == [True, True, True, False, False]
    assert got["resent"] == ["new=true restore=true bases=0", "0", "51", "pending", "true", "true", "https://old/a,https://outbox/b"]
    assert got["fresh"] == ["new=true restore=true bases=0", "10"]
    assert got["after"] == ["pending", "applied", "conflict:conflict", "none"]
    assert got["again"] == "new=true restore=true bases=1"
    assert got["settled"] == "new=false restore=false bases=1"
    assert got["roundtrip"] is True
    assert got["legacyGone"] == ["A"]
