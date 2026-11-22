"""手机上改过、还没确认电脑收下的修改（电脑这边）：bases、按手机上改的先后收、删掉的不被更早的修改变回来、
结果里带回 applied_sha。手机那边的规则在 tests/test_doctime.py（Spark/PendingEdits.swift）。"""

import hashlib
import json
import os
import subprocess

import pytest

from core import cable_sync as cs
from core import clips, remote_tasks

CLIP = """---
title: "T"
source: "S"
url: "https://e.com/a"
published: 2026-11-03
clipped: 2026-11-05
---

# T

- 来源：S
- 发表时间：2026年11月03日
- 收录时间：2026年11月05日

## 正文

第一段。
"""
REL = "Spark/剪藏/20261103_T.md"
NOTE_REL = "Spark/节目/transcripts/20261103_一.md"
NOTE = "# 一\n\n- 时长：约 1:00\n- 发表时间：2026年11月03日\n\n## 文字记录\n\n正文\n"


def sha(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Spark" / "剪藏").mkdir(parents=True)
    (v / "Spark" / "节目" / "transcripts").mkdir(parents=True)
    (v / REL).write_text(CLIP, encoding="utf-8")
    (v / NOTE_REL).write_text(NOTE, encoding="utf-8")
    monkeypatch.setenv("SPARK_VAULT", str(v))
    monkeypatch.setattr(remote_tasks, "note_conflicts_dir", lambda: str(tmp_path / "conflicts"))
    monkeypatch.setattr(remote_tasks, "trash_dir", lambda: str(tmp_path / "trash"))
    monkeypatch.setattr(remote_tasks.clips, "download_images", lambda images, root: (0, 0))
    monkeypatch.setitem(cs._CALL, "fn", None)
    monkeypatch.setitem(cs._BOOK, "book", None)
    return v, tmp_path


def save(path, content, bases=None, at=None, base=None):
    raw = {"type": "save", "path": path, "content": content, "replace": True}
    if bases:
        raw["bases"] = bases
        raw["base"] = base or bases[0]
    if at:
        raw["edited_at"] = at
    return remote_tasks.parse_task(raw)


T0 = 1_795_000_000_000


def test_second_edit_with_bases_after_quick_check_applied_first(env):
    """E1 已经被看一眼（quick check）存下，手机还没收到结果；E2 带 bases [H0, sha(E1)]：照收，不进 note-conflicts。"""
    v, tmp = env
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    e2 = e1.replace("==一==", "==一== ==二==")
    remote_tasks.save_file(save(REL, e1, [sha(CLIP)], T0))
    remote_tasks.save_file(save(REL, e2, [sha(CLIP), sha(e1)], T0 + 1000))
    assert (v / REL).read_text(encoding="utf-8") == e2
    n1 = NOTE.replace("正文", "正文 ==一==")
    n2 = n1.replace("==一==", "==一== ==二==")
    remote_tasks.save_file(save(NOTE_REL, n1, [sha(NOTE)], T0))
    remote_tasks.save_file(save(NOTE_REL, n2, [sha(NOTE), sha(n1)], T0 + 1000))
    assert (v / NOTE_REL).read_text(encoding="utf-8") == n2
    assert not (tmp / "conflicts").exists()


def test_edit_of_current_version_with_smaller_clock_is_written(env):
    """手机 A 的钟快一小时、它的修改先收下了；手机 B 在电脑现在这版上改（bases 里有现在的指纹），edited_at 更小：照收，不看钟。"""
    v, tmp = env
    a = CLIP.replace("第一段。", "第一段 ==A==。")
    remote_tasks.save_file(save(REL, a, [sha(CLIP)], T0 + 3_600_000))
    b = a.replace("==A==", "==A== ==B==")
    task = save(REL, b, [sha(a)], T0)
    remote_tasks.save_file(task)
    assert (v / REL).read_text(encoding="utf-8") == b
    assert task["_applied_sha"] == cs.blob_sha(str(v / REL))
    assert not (tmp / "conflicts").exists()


def test_unrelated_stale_content_is_conflict_never_done_without_write(env):
    v, tmp = env
    mac = CLIP.replace("第一段。", "第一段（电脑上改的）。")
    (v / REL).write_text(mac, encoding="utf-8")
    phone = CLIP.replace("第一段。", "第一段 ==手机==。")
    with pytest.raises(remote_tasks.TaskError) as e:
        remote_tasks.save_file(save(REL, phone, [sha(CLIP)], T0 + 9_999_999))
    assert e.value.kind == "conflict"
    assert (v / REL).read_text(encoding="utf-8") == mac
    kept = list((tmp / "conflicts").iterdir())
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == phone


def test_superseded_by_descendant_is_done_with_current_blob(env):
    """晚到的一份修改，内容是电脑现在这版的祖先（后一次就是从它改来的、已经收下）：done，applied_sha 是现在这版。"""
    v, tmp = env
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    e2 = e1.replace("==一==", "==一== ==二==")
    remote_tasks.save_file(save(REL, e2, [sha(CLIP), sha(e1)], T0 + 1000))
    late = save(REL, e1, [sha(CLIP)], T0)
    msg = remote_tasks.save_file(late)
    assert "已经有这一版的改动" in msg and (v / REL).read_text(encoding="utf-8") == e2
    assert late["_applied_sha"] == cs.blob_sha(str(v / REL))
    assert not (tmp / "conflicts").exists()


def _outbox_item(outbox, tid, task, created):
    outbox.mkdir(exist_ok=True)
    (outbox / f"{tid}.sync.json").write_text(json.dumps({"id": tid, "title": "x", "task": task, "created_at": created},
                                                        ensure_ascii=False), encoding="utf-8")


class Tr:
    def __init__(self, phone, outbox):
        self.phone, self.outbox, self.pushes = phone, outbox, 0

    def push(self, local_dir, dest):
        import shutil
        self.pushes += 1
        shutil.copytree(local_dir, os.path.join(self.phone, dest), dirs_exist_ok=True)

    def pull_outbox(self, local_dir):
        import shutil
        if os.path.isdir(self.outbox):
            shutil.copytree(self.outbox, local_dir, dirs_exist_ok=True)


def _device():
    return {"id": "dev-1", "udid": "dev-1", "name": "测试 iPhone", "transport": "wired"}


def test_quick_check_orders_by_edited_at_not_uuid(env):
    """文件名（UUID）的顺序跟改的先后相反：照样按 edited_at 先后做；两次都写进去（后一次的 bases 里有前一次），都不冲突。"""
    v, tmp = env
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    e2 = e1.replace("==一==", "==一== ==二==")
    outbox = tmp / "outbox"
    _outbox_item(outbox, "ffff-old", {"type": "save", "path": REL, "content": e1, "replace": True,
                                      "base": sha(CLIP), "bases": [sha(CLIP)], "edited_at": T0}, "2026-11-22T10:00:00Z")
    _outbox_item(outbox, "0000-new", {"type": "save", "path": REL, "content": e2, "replace": True,
                                      "base": sha(CLIP), "bases": [sha(CLIP), sha(e1)], "edited_at": T0 + 5},
                 "2026-11-22T10:00:00Z")
    tr = Tr(str(tmp / "phone"), str(outbox))
    cs.check_device(_device(), transport=tr)
    assert (v / REL).read_text(encoding="utf-8") == e2
    book = cs._book()
    assert book.tasks["ffff-old"]["state"] == "done" and book.tasks["0000-new"]["state"] == "done"
    res = json.loads((tmp / "phone" / cs.DEST / cs.RESULTS).read_text(encoding="utf-8"))["tasks"]
    git = subprocess.run(["git", "hash-object", str(v / REL)], capture_output=True, text=True, check=True).stdout.strip()
    assert res["0000-new"]["applied_sha"] == git, "结果里带回写进去那一版的 blob sha"
    assert not (tmp / "conflicts").exists()


def test_delete_then_save_and_save_then_delete(env):
    v, tmp = env
    outbox = tmp / "outbox"
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    # 先改后删（文件名顺序反过来）：先改、再删，最后是删掉
    _outbox_item(outbox, "aaaa-delete", {"type": "delete", "path": REL, "edited_at": T0 + 10}, "2026-11-22T10:00:01Z")
    _outbox_item(outbox, "zzzz-save", {"type": "save", "path": REL, "content": e1, "replace": True,
                                       "base": sha(CLIP), "bases": [sha(CLIP)], "edited_at": T0}, "2026-11-22T10:00:00Z")
    cs.check_device(_device(), transport=Tr(str(tmp / "phone"), str(outbox)))
    assert not (v / REL).exists() and (tmp / "trash" / "20261103_T.md").read_text(encoding="utf-8") == e1
    # 删了之后又到一份修改（带 base）：说清楚，不建文件，不看时间
    with pytest.raises(remote_tasks.TaskError, match="已经没有这篇了") as e:
        remote_tasks.save_file(save(REL, e1.replace("==一==", "==旧=="), [sha(CLIP)], T0 + 99_999_999))
    assert e.value.kind == "gone" and not (v / REL).exists()
    res = json.loads((tmp / "phone" / cs.DEST / cs.RESULTS).read_text(encoding="utf-8"))["tasks"]
    assert res["aaaa-delete"]["state"] == "done"


def test_mac_deleted_then_phone_reclips_and_deletes(env):
    """复审第 3 条：Mac 上删了一篇；手机（离线）又剪了同一篇（新剪藏，没有 base），5 分钟后在手机上删掉：电脑上也删掉。"""
    v, tmp = env
    remote_tasks.delete_file({"type": "delete", "path": REL})
    remote_tasks.save_file(remote_tasks.parse_task({"type": "save", "path": REL, "content": CLIP, "replace": True}))
    assert (v / REL).exists()
    msg = remote_tasks.delete_file(remote_tasks.parse_task({"type": "delete", "path": REL, "edited_at": T0}))
    assert "废纸篓" in msg and not (v / REL).exists()


def test_failure_kinds_in_results(env):
    v, tmp = env
    outbox = tmp / "outbox"
    (v / NOTE_REL).write_text(NOTE + "电脑上又改了\n", encoding="utf-8")
    _outbox_item(outbox, "c1", {"type": "save", "path": NOTE_REL, "content": NOTE + "手机\n", "replace": True,
                                "base": sha(NOTE), "edited_at": T0}, "2026-11-22T10:00:00Z")
    _outbox_item(outbox, "g1", {"type": "save", "path": "Spark/节目/transcripts/没有.md", "content": "x", "replace": True,
                                "base": sha("x"), "edited_at": T0 + 1}, "2026-11-22T10:00:00Z")
    _outbox_item(outbox, "r1", {"type": "save", "path": REL, "content": "x" * (remote_tasks.MAX_SAVE_CHARS + 1),
                                "replace": True}, "2026-11-22T10:00:00Z")
    cs.check_device(_device(), transport=Tr(str(tmp / "phone"), str(outbox)))
    res = cs._book().results()["tasks"]
    assert {k: (res[k]["state"], res[k]["kind"]) for k in ("c1", "g1", "r1")} == \
        {"c1": ("failed", "conflict"), "g1": ("failed", "gone"), "r1": ("failed", "rejected")}


def test_new_clip_without_base_still_created_and_later_recreate_allowed(env):
    v, _ = env
    rel = "Spark/剪藏/20261120_新.md"
    msg = remote_tasks.save_file(remote_tasks.parse_task({"type": "save", "path": rel, "content": CLIP, "replace": True}))
    assert "存进了笔记库" in msg and (v / rel).exists()
    remote_tasks.delete_file({"type": "delete", "path": rel, "edited_at": T0})
    remote_tasks.save_file(remote_tasks.parse_task({"type": "save", "path": rel, "content": CLIP, "replace": True,
                                                    "edited_at": T0 + 1000}))
    assert (v / rel).exists(), "删了之后手机上又新存一篇同名的：照存"


def test_save_of_vanished_noted_file_fails_without_creating(env):
    v, _ = env
    (v / NOTE_REL).unlink()
    with pytest.raises(remote_tasks.TaskError, match="已经没有这篇了"):
        remote_tasks.save_file(save(NOTE_REL, NOTE + "x", [sha(NOTE)], T0))
    assert not (v / NOTE_REL).exists()


def test_migrated_from_still_works_with_bases(env):
    v, tmp = env
    old = CLIP.replace("- 发表时间：2026年11月03日\n- 收录时间：2026年11月05日\n", "- 发布：2026-11-03\n- 剪藏：2026-11-05\n")
    clips.update_journal(str(v), lambda j: j["rewritten"].update({REL: {"before": [sha(old)], "after": sha(CLIP)}}))
    phone = old.replace("第一段。", "第一段 ==一==。")
    remote_tasks.save_file(save(REL, phone, [sha(old), "0" * 64], T0))
    got = (v / REL).read_text(encoding="utf-8")
    assert "==一==" in got and "- 发表时间：2026年11月03日\n" in got and not (tmp / "conflicts").exists()


def test_journal_concurrent_writers_keep_everything(env):
    import threading
    v, _ = env

    def worker(k):
        for i in range(25):
            clips.update_journal(str(v), lambda j, k=k, i=i: j["renames"].update({f"Spark/剪藏/{k}_{i}.md": f"Spark/剪藏/n{k}_{i}.md"}))
            clips.record_edit(str(v), f"p{k}_{i}.md", f"c{i}", f"w{i}", [])
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(clips.load_journal(str(v))["renames"]) == 200
    assert len(clips.load_edits(str(v))) == 200


def test_corrupt_journal_is_moved_aside_never_overwritten(env):
    v, _ = env
    path = clips.journal_path(str(v))
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"renames": {"a": "b"')   # 写了一半
    assert clips.load_journal(str(v))["renames"] == {}, "读的人当没有"
    bad = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".renames.json.bad-")]
    assert len(bad) == 1 and open(os.path.join(os.path.dirname(path), bad[0]), encoding="utf-8").read() == '{"renames": {"a": "b"'
    with open(path, "w", encoding="utf-8") as f:
        f.write("不是 JSON")
    with pytest.raises(clips.JournalError):
        clips.update_journal(str(v), lambda j: j["renames"].update({"x": "y"}))
    assert open(path, encoding="utf-8").read() == "不是 JSON", "还有没处理的坏文件：不写，也没拿空表盖掉"
    clips.load_journal(str(v))   # 读的人把它也挪开
    assert len([n for n in os.listdir(os.path.dirname(path)) if ".bad-" in n]) == 2, "同一秒坏两次，两份都留着"


def test_edits_pruned_and_moved_with_rename(env):
    v, _ = env
    for i in range(20):
        clips.record_edit(str(v), REL, f"c{i}", f"w{i}", [])
    assert len(clips.load_edits(str(v))[REL]) == clips.EDITS_KEEP
    clips.move_edits(str(v), [(REL, "Spark/剪藏/新.md")])
    assert REL not in clips.load_edits(str(v)) and len(clips.load_edits(str(v))["Spark/剪藏/新.md"]) == clips.EDITS_KEEP
    assert "edits" not in json.loads(open(clips.journal_path(str(v)), encoding="utf-8").read()) if os.path.exists(clips.journal_path(str(v))) else True


def test_applied_sha_in_github_status(env, monkeypatch):
    task = save(REL, CLIP.replace("第一段。", "第一段 ==一==。"), [sha(CLIP)], T0)
    remote_tasks.save_file(task)
    body = remote_tasks.status_comment("done", "完成", applied_sha=task["_applied_sha"])
    meta = json.loads(body.split(remote_tasks.STATUS_PREFIX, 1)[1].split(" -->", 1)[0])
    assert meta["applied_sha"] == cs.blob_sha(os.path.join(os.environ["SPARK_VAULT"], *REL.split("/")))


# ---------------------------------------------------------------- 复核：撤销改回原样也要写；记录坏了不中断

def test_unhighlight_back_to_original_is_written(env):
    """划了线（E1 收下成 M1），又撤掉：E2 的内容就是 H0，bases [H0, sha(E1)]。电脑上是手机改之前那版（M1=E1）：照写，不当成「已经在里面」。"""
    v, tmp = env
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    remote_tasks.save_file(save(REL, e1, [sha(CLIP)], T0))
    remote_tasks.save_file(save(REL, CLIP, [sha(CLIP), sha(e1)], T0 + 1))
    assert (v / REL).read_text(encoding="utf-8") == CLIP
    n1 = NOTE.replace("正文", "正文 ==一==")
    remote_tasks.save_file(save(NOTE_REL, n1, [sha(NOTE)], T0))
    remote_tasks.save_file(save(NOTE_REL, NOTE, [sha(NOTE), sha(n1)], T0 + 1))
    assert (v / NOTE_REL).read_text(encoding="utf-8") == NOTE
    assert not (tmp / "conflicts").exists()


def test_phone_b_reverting_to_original_is_written(env):
    v, _ = env
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    remote_tasks.save_file(save(REL, e1, [sha(CLIP)], T0))      # 手机 A
    remote_tasks.save_file(save(REL, CLIP, [sha(e1)], T0 + 5))    # 手机 B 同步到 M1 之后改回原样
    assert (v / REL).read_text(encoding="utf-8") == CLIP


def test_late_duplicate_still_contained_and_long_chain(env):
    v, tmp = env
    texts = [CLIP]
    for i in range(18):
        texts.append(texts[-1].replace("第一段", f"第一段{i}", 1))
        bases = [sha(t) for t in texts[:-1]][-8:]
        if sha(CLIP) not in bases:
            bases = [sha(CLIP)] + bases[-7:]
        remote_tasks.save_file(save(REL, texts[-1], bases, T0 + i))
        assert (v / REL).read_text(encoding="utf-8") == texts[-1], i
    late = save(REL, texts[1], [sha(CLIP)], T0)    # 第一次修改的任务重复送到
    assert "已经有这一版的改动" in remote_tasks.save_file(late)
    assert (v / REL).read_text(encoding="utf-8") == texts[-1]
    assert not (tmp / "conflicts").exists()


def test_journal_error_after_write_does_not_abort_sync(env, monkeypatch):
    """文件写进去之后记录写不进去：任务 done（带 applied_sha），说明里提一句；同步不中断。"""
    v, tmp = env

    def broken(*a, **k):
        raise clips.JournalError(".edits.json 写不进去")
    monkeypatch.setattr(clips, "record_edit", broken)
    outbox = tmp / "outbox"
    e1 = CLIP.replace("第一段。", "第一段 ==一==。")
    _outbox_item(outbox, "j1", {"type": "save", "path": REL, "content": e1, "replace": True,
                                "base": sha(CLIP), "bases": [sha(CLIP)], "edited_at": T0}, "2026-11-22T10:00:00Z")
    r = cs.check_device(_device(), transport=Tr(str(tmp / "phone"), str(outbox)))
    assert r["saved"] == 1 and (v / REL).read_text(encoding="utf-8") == e1
    t = cs._book().tasks["j1"]
    assert t["state"] == "done" and t["applied_sha"] == cs.blob_sha(str(v / REL)) and "修改记录没记上" in t["text"]


def test_journal_refuses_writes_while_bad_copy_exists(env):
    v, _ = env
    clips.update_journal(str(v), lambda j: j["renames"].update({"Spark/剪藏/a.md": "Spark/剪藏/b.md"}))
    with open(clips.journal_path(str(v)), "w", encoding="utf-8") as f:
        f.write("坏")
    assert clips.load_journal(str(v))["renames"] == {}
    assert clips.journal_problems(str(v)), "同步状态里有提醒"
    with pytest.raises(clips.JournalError):
        clips.update_journal(str(v), lambda j: j["renames"].update({"x": "y"}))
    assert not os.path.exists(clips.journal_path(str(v))), "不从空表开始写（不然手机上会变成「没有改名」）"
    assert "warning" in cs.status()
    # 修改记录坏了可以从空的重新记
    with open(clips.edits_path(str(v)), "w", encoding="utf-8") as f:
        f.write("坏")
    clips.record_edit(str(v), REL, "a", "b", [])
    assert list(clips.load_edits(str(v))) == [REL]


def test_edits_and_bad_copies_not_synced(env, tmp_path):
    from core import github_sync
    assert cs._skip(".edits.json") and cs._skip(".renames.json.bad-20261122-101010") and not cs._skip(".renames.json")
    assert ".edits.json" in github_sync.EXCLUDE and "*.bad-*" in github_sync.EXCLUDE and ".*/" in github_sync.EXCLUDE
