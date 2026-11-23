"""tools/migrate_times.py：已有文档换成新的时间写法、剪藏按发表那天改名。只在临时目录里跑。"""

import hashlib
import io
import json
import os
import shutil
import time

import pytest

from core import doctime
from tools import migrate_times as mt

PUB, CAP = doctime.PUBLISHED_LABEL, doctime.CAPTURED_LABEL   # 标签从 core/doctime 取，改标签时测试跟着变

CLIP = """---
title: "文章"
source: "量子位"
url: "https://example.com/a"
published: 2026-11-03
clipped: 2026-11-05
tags: ["A"]
starred: true
pinned: true
---

# 文章

- 来源：量子位
- 发布：2026-11-03
- 剪藏：2026-11-05
- 备注：看看

## 摘要

一句话

## 正文

第一段 ==划的重点==[^note-1] 后面。

[阅读原文](https://example.com/a)

## 读后感

我的读后感。

[^note-1]: 我的批注
"""

TRANSCRIPT = """# 第一期

- 所属节目：Show
- 链接：https://example.com/v
- 时长：约 1:00:00
- 播出时间：2026-09-15
- 字幕来源：YouTube 自动生成字幕（en），已去重整理，可能存在识别误差

## 文字记录

正文 ==重点==
"""

NOTE = """---
节目: "[[Show]]"
播出: 2026-09-15
时长: "1:00:00"
---

# 第一期

> 一句话

### 本期要点

- a
"""

TRACK = "# T\n\n- 来源：量子位\n- 发布日期：2026-09-24\n- 原文链接：https://x\n\n## 小结\n\nb\n"

SUMMIT = "# 议题\n\n- 所属会议：2026 OCP Global Summit\n- 链接：https://x\n- 时长：约 30:00\n- 播出时间：2026-10-15\n\n## 文字记录\n\nx\n"


@pytest.fixture(autouse=True)
def no_server(monkeypatch):
    """测试里别真去看 8760 端口（开发机上 Spark 服务可能开着）。"""
    monkeypatch.setattr(mt, "server_running", lambda port=mt.SERVER_PORT: False)


@pytest.fixture
def vault(tmp_path):
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    root = tmp_path / "v"
    clips = root / "Spark" / "剪藏"
    clips.mkdir(parents=True)
    (clips / "20261105_文章.md").write_text(CLIP, encoding="utf-8")
    (clips / "20261103_文章.md").write_text(CLIP.replace("published: 2026-11-03\n", ""), encoding="utf-8")   # 占着新名字
    (clips / "20261101_没发表日期.md").write_text(CLIP.replace("published: 2026-11-03\n", "")
                                               .replace("- 发布：2026-11-03\n", ""), encoding="utf-8")
    (clips / "20261102_没有文件头.md").write_text("# 没有文件头\n\n- 来源：x\n- 发布：2026-10-01\n", encoding="utf-8")
    show = root / "Spark" / "Show"
    for d in ("transcripts", "notes"):
        (show / d).mkdir(parents=True)
    (show / "transcripts" / "20260915_第一期.md").write_text(TRANSCRIPT, encoding="utf-8")
    (show / "notes" / "20260915 第一期.md").write_text(NOTE, encoding="utf-8")
    (show / ".manifest.json").write_text(json.dumps({"content_type": "series", "entries": {"v": {
        "ok": True, "rank": 1, "entry": {"id": "v", "title": "第一期", "publish_date": "20260915"},
        "relative_path": "transcripts/20260915_第一期.md", "note_relative_path": "notes/20260915 第一期.md",
        "updated_at": "2026-09-16T10:12:00"}}}, ensure_ascii=False, indent=2), encoding="utf-8")
    track = root / "Spark" / "信息跟进" / "量子位"
    track.mkdir(parents=True)
    (track / "20260924_T.md").write_text(TRACK, encoding="utf-8")
    (track / ".manifest.json").write_text(json.dumps({"entries": {"a": {
        "ok": True, "entry": {"id": "a", "publish_date": "20260924", "captured_at": "2026-09-24T19:41:00+08:00"},
        "relative_path": "20260924_T.md", "updated_at": "2026-09-30T00:00:00"}}}), encoding="utf-8")
    review = root / "output" / "复盘"
    review.mkdir(parents=True)
    (review / "复盘_剪藏_20261101-20261107.md").write_text(
        "# 复盘\n\n见 [[20261105_文章]]、[[Spark/剪藏/20261105_文章|这篇]] 和 [文章](<../../Spark/剪藏/20261105_文章.md>)、"
        "[又一次](../../Spark/%E5%89%AA%E8%97%8F/20261105_%E6%96%87%E7%AB%A0.md)。\n", encoding="utf-8")
    yield root
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _snapshot(root):
    out = {}
    for folder, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(folder, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


def test_dry_run_plans_but_touches_nothing(vault, capsys):
    before = _snapshot(vault)
    assert mt.main(["--root", str(vault)]) == 0
    out = capsys.readouterr().out
    assert _snapshot(vault) == before
    assert out.startswith(mt.BEFORE_NOTE + "\n" + mt.AFTER_NOTE + "\n" + mt.N2I_NOTE + "\n")
    assert "每台手机先同步一次，把发件箱清空；停掉 Spark 服务" in out and "notes2insight" in out
    assert "迁移后：每台手机先同步一次，再在手机上改剪藏" in out and "迁移时关掉 Obsidian" in out
    assert "Spark/剪藏/20261105_文章.md → Spark/剪藏/20261103_文章_2.md" in out
    assert "    -- 发布：2026-11-03" in out and f"    +- {PUB}：2026年11月03日" in out
    assert "没改：Spark/剪藏/20261102_没有文件头.md：没有文件头" in out
    assert "这是计划，没动文件" in out


def test_apply_rewrites_renames_relinks_and_is_idempotent(vault):
    readlog = vault.parent / "reading-log.jsonl"
    readlog.write_text(json.dumps({"id": "1", "at": 1.0, "path": "Spark/剪藏/20261105_文章.md"}) + "\n"
                       + json.dumps({"id": "2", "at": 1.0, "path": "Spark/别的.md"}) + "\n", encoding="utf-8")
    manifests = {p: p.read_bytes() for p in vault.rglob(".manifest.json")}
    assert mt.main(["--root", str(vault), "--readlog", str(readlog), "--apply"]) == 0
    clips = vault / "Spark" / "剪藏"
    assert not (clips / "20261105_文章.md").exists()
    moved = (clips / "20261103_文章_2.md").read_text(encoding="utf-8")
    assert moved == CLIP.replace("- 发布：2026-11-03\n- 剪藏：2026-11-05\n",
                                 f"- {PUB}：2026年11月03日\n- 收录时间：2026年11月05日\n"), \
        "只换了两行时间：划线、批注、读后感、星标置顶一字不动；老剪藏的收录时间只写日期"
    occupied = (clips / "20261103_文章.md").read_text(encoding="utf-8")
    assert occupied == CLIP.replace("published: 2026-11-03\n", "").replace(
        "- 发布：2026-11-03\n- 剪藏：2026-11-05\n", f"- {PUB}：2026年11月03日\n- 收录时间：2026年11月05日\n"), "占着名字的那篇没被覆盖"
    assert (clips / "20261101_没发表日期.md").exists(), "不知道发表时间的不改名"
    assert (clips / "20261102_没有文件头.md").exists()
    index = (clips / "索引.md").read_text(encoding="utf-8")
    assert "(<20261103_文章_2.md>)" in index and "20261105_文章" not in index
    review = (vault / "output" / "复盘" / "复盘_剪藏_20261101-20261107.md").read_text(encoding="utf-8")
    assert "[[20261103_文章_2]]" in review and "[[Spark/剪藏/20261103_文章_2|这篇]]" in review
    assert "(<../../Spark/剪藏/20261103_文章_2.md>)" in review and "%E6%96%87%E7%AB%A0_2.md)" in review
    assert "20261105" not in review
    log = [json.loads(x) for x in readlog.read_text(encoding="utf-8").splitlines()]
    assert log[0]["path"] == "Spark/剪藏/20261103_文章_2.md" and log[1]["path"] == "Spark/别的.md"

    show = vault / "Spark" / "Show"
    t = (show / "transcripts" / "20260915_第一期.md").read_text(encoding="utf-8")
    assert f"- 时长：约 1:00:00\n- 播出时间：2026-09-15\n- {CAP}：2026年09月16日\n- 字幕来源：" in t, \
        "播出时间那行原样留着，下面加收录时间；老文件的收录时间取 updated_at 那天，只写日期（它是最后一次处理的时间）"
    assert t.endswith("正文 ==重点==\n")
    note = (show / "notes" / "20260915 第一期.md").read_text(encoding="utf-8")
    assert note.startswith('---\n节目: "[[Show]]"\n播出: 2026-09-15\n时长: "1:00:00"\n---\n'), "不从 updated_at 写 captured_at"
    assert f"# 第一期\n\n- {PUB}：2026年09月15日\n- 收录时间：2026年09月16日\n\n> 一句话\n" in note
    assert {p: p.read_bytes() for p in vault.rglob(".manifest.json")} == manifests, "处理记录不改"
    track = (vault / "Spark" / "信息跟进" / "量子位" / "20260924_T.md").read_text(encoding="utf-8")
    assert track.startswith(f"# T\n\n- 来源：量子位\n- {PUB}：2026年09月24日\n- 收录时间：2026年09月24日，19:41（UTC+8）\n- 原文链接：")

    after = _snapshot(vault)
    plan = mt.build(str(vault), str(readlog))
    assert plan.empty, "再跑一遍什么都不改"
    assert mt.main(["--root", str(vault), "--readlog", str(readlog), "--apply"]) == 0
    assert _snapshot(vault) == after


def test_apply_skips_files_changed_after_planning(vault):
    plan = mt.build(str(vault))
    clip = vault / "Spark" / "剪藏" / "20261105_文章.md"
    clip.write_text(CLIP + "\n新写的读后感\n", encoding="utf-8")
    mt.apply(str(vault), plan)
    assert any("列计划之后被改过" in p for p in plan.problems)
    assert clip.read_text(encoding="utf-8").endswith("新写的读后感\n") and not (vault / "Spark" / "剪藏" / "20261103_文章_2.md").exists(), \
        "内容没改的也不改名"
    journal = json.loads((vault / "Spark" / "剪藏" / ".renames.json").read_text(encoding="utf-8"))
    assert "Spark/剪藏/20261105_文章.md" not in journal["renames"], "没改的名字不留在迁移记录里（手机会按它换路径）"
    assert mt.build(str(vault)).renames == [("Spark/剪藏/20261105_文章.md", "Spark/剪藏/20261103_文章_2.md")], "下次再跑接着改"


def test_report_prints_summary(vault):
    buf = io.StringIO()
    mt.report(mt.build(str(vault)), out=buf)
    assert "共：改写" in buf.getvalue()


def test_taihe_state_paths_follow_renames(vault, tmp_path_factory):
    from core import taihe_sync
    home = tmp_path_factory.mktemp("home")
    state_dir = home / "taihe-sync" / "dev1" / taihe_sync.sha(str(os.path.realpath(vault)))[:16]
    state_dir.mkdir(parents=True)
    state = {"version": 1, "entries": {"c1": {"path": "Spark/剪藏/20261105_文章.md", "phone": "r"},
                                        "c2": {"path": "Spark/剪藏/20261101_没发表日期.md"}}}
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert mt.main(["--root", str(vault), "--taihe-home", str(home), "--apply"]) == 0
    got = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert got["entries"]["c1"]["path"] == "Spark/剪藏/20261103_文章_2.md"
    assert got["entries"]["c2"]["path"] == "Spark/剪藏/20261101_没发表日期.md"


def test_phone_edits_with_old_name_land_on_renamed_clip(vault, monkeypatch):
    from core import remote_tasks
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    journal = json.loads((vault / "Spark" / "剪藏" / ".renames.json").read_text(encoding="utf-8"))
    assert journal["renames"] == {"Spark/剪藏/20261105_文章.md": "Spark/剪藏/20261103_文章_2.md"}
    assert journal["rewritten"]["Spark/剪藏/20261103_文章_2.md"]["before"] == [hashlib.sha256(CLIP.encode()).hexdigest()]
    monkeypatch.setenv("SPARK_VAULT", str(vault))
    monkeypatch.setattr(remote_tasks.clips, "download_images", lambda images, root: (0, 0))
    msg = remote_tasks.save_file({"path": "Spark/剪藏/20261105_文章.md", "content": "手机改过的\n", "replace": True})
    assert "20261103_文章_2.md" in msg
    assert not (vault / "Spark" / "剪藏" / "20261105_文章.md").exists(), "不按旧名字另生一篇"
    assert (vault / "Spark" / "剪藏" / "20261103_文章_2.md").read_text(encoding="utf-8") == "手机改过的\n"
    monkeypatch.setattr(remote_tasks, "trash_dir", lambda: str(vault / "trash"))
    remote_tasks.delete_file({"path": "Spark/剪藏/20261105_文章.md"})
    assert not (vault / "Spark" / "剪藏" / "20261103_文章_2.md").exists() and (vault / "trash" / "20261103_文章_2.md").exists()


# ---------------------------------------------------------------- 评审的问题

def test_m1_summit_file_named_by_rank_keeps_its_date(vault):
    """大会议题：处理记录里没有 publish_date、文件名是编号开头，文件里写着的播出时间不能丢。"""
    summit = vault / "Spark" / "大会"
    (summit / "transcripts").mkdir(parents=True)
    (summit / "transcripts" / "441_议题.md").write_text(SUMMIT, encoding="utf-8")
    (summit / ".manifest.json").write_text(json.dumps({"entries": {"s": {
        "ok": True, "rank": 441, "entry": {"id": "s", "title": "议题"}, "relative_path": "transcripts/441_议题.md",
        "updated_at": "2026-10-20T09:00:00"}}}), encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    got = (summit / "transcripts" / "441_议题.md").read_text(encoding="utf-8")
    assert f"- 时长：约 30:00\n- 播出时间：2026-10-15\n- {CAP}：2026年10月20日\n\n## 文字记录" in got, "播出时间原样留着，不丢"


def test_m1_pipeline_rerender_keeps_existing_date(vault):
    from apps.summit2md import pipeline
    path = vault / "s.md"
    path.write_text(SUMMIT, encoding="utf-8")
    entry = {"id": "s", "title": "议题", "url": "https://x", "duration": 1800}
    md = pipeline.render_transcript_md(entry, "2026 OCP Global Summit", [(0.0, "x")], None, "en")
    assert f"{PUB}" not in md
    pipeline._write_keeping_highlights(str(path), md)
    got = path.read_text(encoding="utf-8")
    assert f"- {PUB}：2026年10月15日\n- 收录时间：" in got, "处理记录、文件名都没有日期时，文件里原来写着的日期留着"
    # 已经是新写法的：再重写一遍收录时间、发表时间都不变
    again = pipeline.render_transcript_md(dict(entry, captured_at="2026-11-22T10:00:00+08:00"), "2026 OCP Global Summit",
                                          [(0.0, "x")], None, "en")
    pipeline._write_keeping_highlights(str(path), again)
    assert path.read_text(encoding="utf-8").count(f"{PUB}") == 1
    assert path.read_text(encoding="utf-8").split("- 收录时间：")[1][:30] == got.split("- 收录时间：")[1][:30]


def _three_renames(vault):
    """再加两篇要改名的剪藏，复盘里链着它们。"""
    clips = vault / "Spark" / "剪藏"
    for i, day in ((2, "2026-10-02"), (3, "2026-10-03")):
        text = CLIP.replace("published: 2026-11-03", f"published: {day}").replace("- 发布：2026-11-03", f"- 发布：{day}") \
            .replace('title: "文章"', f'title: "文章{i}"').replace("# 文章\n", f"# 文章{i}\n")
        (clips / f"20261106_文章{i}.md").write_text(text, encoding="utf-8")
    review = vault / "output" / "复盘" / "复盘_剪藏_20261101-20261107.md"
    review.write_text(review.read_text(encoding="utf-8") + "还有 [[20261106_文章2]] 和 [[20261106_文章3]]。\n", encoding="utf-8")


@pytest.mark.parametrize("k", [0, 1, 2])
def test_m2_interrupted_apply_converges(vault, tmp_path, monkeypatch, k):
    """--apply 在第 k 个改名之后被打断，再跑一遍：跟一次跑完的结果一模一样（链接、索引、阅读记录都按迁移记录改完）。"""
    _three_renames(vault)
    clean = tmp_path / "clean"
    shutil.copytree(vault, clean)

    def logs(root):
        p = root.parent / f"{root.name}-log.jsonl"
        p.write_text("".join(json.dumps({"id": str(i), "at": 1.0, "path": f"Spark/剪藏/{n}"}) + "\n"
                             for i, n in enumerate(["20261105_文章.md", "20261106_文章2.md", "20261106_文章3.md"])),
                     encoding="utf-8")
        return p

    clean_log, log = logs(clean), logs(vault)
    assert mt.main(["--root", str(clean), "--readlog", str(clean_log), "--apply"]) == 0

    calls = {"n": 0}
    real = mt._rename

    def flaky(src, dst):
        if calls["n"] >= k:
            raise KeyboardInterrupt("停电")
        calls["n"] += 1
        real(src, dst)

    monkeypatch.setattr(mt, "_rename", flaky)
    with pytest.raises(KeyboardInterrupt):
        mt.main(["--root", str(vault), "--readlog", str(log), "--apply"])
    journal = json.loads((vault / "Spark" / "剪藏" / ".renames.json").read_text(encoding="utf-8"))
    assert len(journal["renames"]) == 3, "改名之前整个计划已经记进迁移记录"
    monkeypatch.setattr(mt, "_rename", real)
    assert mt.main(["--root", str(vault), "--readlog", str(log), "--apply"]) == 0

    def norm(snap):
        out = dict(snap)
        key = os.path.join("Spark", "剪藏", ".renames.json")
        out[key] = json.loads(out[key])
        return out

    assert norm(_snapshot(vault)) == norm(_snapshot(clean))
    assert log.read_text(encoding="utf-8") == clean_log.read_text(encoding="utf-8")
    assert mt.build(str(vault), str(log)).empty


def test_m2_chained_renames_resolve(vault):
    clips = vault / "Spark" / "剪藏"
    (clips / ".renames.json").write_text(json.dumps({"Spark/剪藏/a.md": "Spark/剪藏/20261105_文章.md"}), encoding="utf-8")
    review = vault / "output" / "复盘" / "旧.md"
    review.write_text("# 旧\n\n[[a]]\n", encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    assert review.read_text(encoding="utf-8") == "# 旧\n\n[[20261103_文章_2]]\n", "a→20261105_文章→20261103_文章_2 顺着找到最后的名字"
    from core import clips as core_clips
    assert core_clips.renamed(str(vault), "Spark/剪藏/a.md") == "Spark/剪藏/20261103_文章_2.md"


def test_m3_times_preserved_and_cable_cache_forgets(vault, tmp_path):
    old = 1_700_000_000
    for p in vault.rglob("*.md"):
        os.utime(p, (old, old))
    cache = tmp_path / "cable-sha-cache.json"
    cache.write_text(json.dumps({"Spark/剪藏/20261105_文章.md": ["1:2", "x"], "Spark/Show/transcripts/20260915_第一期.md": ["1:2", "x"],
                                 "Spark/没动的.md": ["1:2", "y"]}), encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply", "--cable-cache", str(cache)]) == 0
    for rel in ("Spark/剪藏/20261103_文章_2.md", "Spark/剪藏/20261103_文章.md", "Spark/Show/transcripts/20260915_第一期.md",
                "Spark/Show/notes/20260915 第一期.md", "output/复盘/复盘_剪藏_20261101-20261107.md"):
        st = os.stat(vault / rel)
        assert int(st.st_mtime) == old and int(st.st_atime) == old, rel
    assert json.loads(cache.read_text(encoding="utf-8")) == {"Spark/没动的.md": ["1:2", "y"]}


def test_m3_cable_stage_sees_rewrite_with_same_size_and_mtime(tmp_path):
    """数据线同步的指纹缓存：内容变了、大小和修改时间都一样（迁移只换了链接里的日期）也重算。"""
    from core import cable_sync
    root, staging = tmp_path / "v", tmp_path / "s"
    (root / "Spark").mkdir(parents=True)
    f = root / "Spark" / "a.md"
    f.write_text("[[20261105_x]]", encoding="utf-8")
    cache: dict = {}
    first = cable_sync.stage(str(root), str(staging), cache)["Spark/a.md"][0]
    st = os.stat(f)
    mt._write_keep_times(str(f), "[[20261103_x]]")
    assert os.stat(f).st_mtime_ns == st.st_mtime_ns and os.stat(f).st_size == st.st_size
    second = cable_sync.stage(str(root), str(staging), cache)["Spark/a.md"][0]
    assert second != first and second == cable_sync.blob_sha(str(f))


def test_m4_refuses_while_server_listens(vault, monkeypatch, capsys):
    monkeypatch.setattr(mt, "server_running", lambda port=mt.SERVER_PORT: True)
    before = _snapshot(vault)
    assert mt.main(["--root", str(vault), "--apply"]) == 3
    assert _snapshot(vault) == before
    assert "8760" in capsys.readouterr().err
    assert mt.main(["--root", str(vault), "--apply", "--allow-running-server"]) == 0
    assert (vault / "Spark" / "剪藏" / "20261103_文章_2.md").exists()


def test_m6_uses_written_day_and_own_offset(vault):
    """文件里写着 published 那天：哪天、改成什么名字都按它，不按迁移这台电脑的时区重算；时刻照它自己的时差写。"""
    clips = vault / "Spark" / "剪藏"
    text = CLIP.replace("published: 2026-11-03\n", 'published: 2026-11-03\npublished_at: "2026-11-03T23:30:00-08:00"\n')
    (clips / "20261107_西岸.md").write_text(text, encoding="utf-8")
    new_style = ("---\ntitle: \"新\"\npublished: 2026-11-03\nclipped: 2026-11-08\n---\n\n# 新\n\n- 来源：x\n"
                 f"- {PUB}：2026年11月03日，23:30（UTC-8）\n- 收录时间：2026年11月08日，08:05（UTC-7）\n")
    (clips / "20261103_新.md").write_text(new_style, encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    got = (clips / "20261103_西岸.md").read_text(encoding="utf-8")   # 北京时间已经是 11 月 4 日，名字照样按写着的 11 月 3 日
    assert f"- {PUB}：2026年11月03日，23:30（UTC-8）\n- 收录时间：2026年11月05日\n" in got
    assert (clips / "20261103_新.md").read_text(encoding="utf-8") == new_style, "已经是新写法的不换成这台电脑的时区"


def test_m7_only_links_that_resolve_to_the_clip(vault):
    review = vault / "output" / "复盘" / "杂.md"
    body = ("# 杂\n\n```\n[[20261105_文章]]\n```\n\n行内 `[[20261105_文章]]` 不动，"
            "![[attachments/20261105_文章.md]] 不动，[别处](../别的/20261105_文章.md) 不动，"
            "[[20261105_文章#小节|看]] 要改，[原文](<../../Spark/剪藏/20261105_文章.md#摘要>) 要改。\n")
    review.write_text(body, encoding="utf-8")
    other = vault / "Spark" / "别处"
    other.mkdir()
    (other / "20261106_同名.md").write_text("# 同名\n", encoding="utf-8")
    clips = vault / "Spark" / "剪藏"
    (clips / "20261106_同名.md").write_text(CLIP.replace("# 文章\n", "# 同名\n"), encoding="utf-8")
    (vault / "output" / "复盘" / "同名.md").write_text("# x\n\n[[20261106_同名]]\n", encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    got = review.read_text(encoding="utf-8")
    assert got == body.replace("[[20261105_文章#小节|看]]", "[[20261103_文章_2#小节|看]]") \
        .replace("(<../../Spark/剪藏/20261105_文章.md#摘要>)", "(<../../Spark/剪藏/20261103_文章_2.md#摘要>)")
    assert (vault / "output" / "复盘" / "同名.md").read_text(encoding="utf-8") == "# x\n\n[[20261106_同名]]\n", \
        "别处有同名笔记的 [[名字]] 说不准指哪篇，不动"


def test_m8_odd_shapes_are_skipped_and_listed(vault, capsys):
    clips = vault / "Spark" / "剪藏"
    crlf = CLIP.replace("\n", "\r\n").encode("utf-8")
    (clips / "20261107_crlf.md").write_bytes(crlf)
    bom = ("﻿" + CLIP).encode("utf-8")
    (clips / "20261107_bom.md").write_bytes(bom)
    no_title = CLIP.replace("# 文章\n\n", "")
    (clips / "20261107_没标题.md").write_text(no_title, encoding="utf-8")
    body_item = CLIP.replace("我的读后感。", "- 发布：2026-01-01 这是正文里的列表")
    (clips / "20261103_正文列表.md").write_text(body_item, encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    out = capsys.readouterr().out
    assert (clips / "20261107_crlf.md").read_bytes() == crlf and (clips / "20261107_bom.md").read_bytes() == bom
    assert (clips / "20261107_没标题.md").read_text(encoding="utf-8") == no_title
    assert "没动：Spark/剪藏/20261107_crlf.md：CRLF 换行" in out and "没动：Spark/剪藏/20261107_bom.md：文件开头有 BOM" in out
    assert "没动：Spark/剪藏/20261107_没标题.md：正文开头不是「# 标题」" in out
    got = (clips / "20261103_正文列表.md").read_text(encoding="utf-8")
    assert "- 发布：2026-01-01 这是正文里的列表" in got and f"- {PUB}：2026年11月03日\n" in got


def test_p2_highlight_made_before_migration_lands_after(vault, monkeypatch):
    """手机在迁移前那一版上划了线，迁移之后才交到电脑：落到新名字那篇，用新的时间写法，划线在，不进 note-conflicts。"""
    from core import remote_tasks
    base = hashlib.sha256(CLIP.encode()).hexdigest()
    phone = CLIP.replace("后面。", "后面，==手机上划的==。")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    conflicts = vault.parent / "conflicts"
    monkeypatch.setenv("SPARK_VAULT", str(vault))
    monkeypatch.setattr(remote_tasks, "note_conflicts_dir", lambda: str(conflicts))
    monkeypatch.setattr(remote_tasks.clips, "download_images", lambda images, root: (0, 0))
    task = remote_tasks.parse_task({"type": "save", "path": "Spark/剪藏/20261105_文章.md", "content": phone,
                                    "replace": True, "base": base})
    remote_tasks.save_file(task)
    got = (vault / "Spark" / "剪藏" / "20261103_文章_2.md").read_text(encoding="utf-8")
    assert "==手机上划的==" in got
    assert f"- {PUB}：2026年11月03日\n- 收录时间：2026年11月05日\n" in got and "- 发布：" not in got
    assert not conflicts.exists()

    # 整理稿（_save_noted）一样
    rel = "Spark/Show/transcripts/20260915_第一期.md"
    base = hashlib.sha256(TRANSCRIPT.encode()).hexdigest()
    task = remote_tasks.parse_task({"type": "save", "path": rel, "replace": True, "base": base,
                                    "content": TRANSCRIPT.replace("正文 ==重点==", "正文 ==重点== ==又一处==")})
    remote_tasks.save_file(task)
    got = (vault / rel).read_text(encoding="utf-8")
    # 电脑上迁移后留着的「播出时间」跟收录时间一起接回来，发布日照样读得出
    assert "==又一处==" in got and f"- 播出时间：2026-09-15\n- {CAP}：2026年09月16日\n" in got
    assert doctime.parse(doctime.PUBLISHED_LINE.search(got).group(1)).isoformat() == "2026-09-15"
    assert not conflicts.exists()
    # 迁移之后电脑上又改过：照旧算冲突
    (vault / rel).write_text(got + "\n电脑上又改了\n", encoding="utf-8")
    with pytest.raises(remote_tasks.TaskError):
        remote_tasks.save_file(task)
    assert conflicts.exists()


def test_t7_old_phone_clip_without_base_keeps_migrated_header(vault, monkeypatch):
    from core import remote_tasks
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    monkeypatch.setenv("SPARK_VAULT", str(vault))
    monkeypatch.setattr(remote_tasks.clips, "download_images", lambda images, root: (0, 0))
    rel = "Spark/剪藏/20261103_文章_2.md"
    migrated = (vault / rel).read_text(encoding="utf-8").replace("clipped: 2026-11-05\n",
                                                                 'clipped: 2026-11-05\nclipped_at: "2026-11-05T08:00:00+08:00"\n')
    (vault / rel).write_text(migrated, encoding="utf-8")
    old_build = CLIP.replace("clipped: 2026-11-05\n", "").replace("一句话", "手机上重写的摘要")
    remote_tasks.save_file({"path": rel, "content": old_build, "replace": True})
    got = (vault / rel).read_text(encoding="utf-8")
    assert "手机上重写的摘要" in got
    assert "clipped: 2026-11-05\n" in got and 'clipped_at: "2026-11-05T08:00:00+08:00"\n' in got
    assert f"- {PUB}：2026年11月03日\n- 收录时间：2026年11月05日\n- 备注：看看" in got and "- 发布：" not in got


# ---------------------------------------------------------------- 复审的问题

@pytest.mark.skipif(not hasattr(os.stat_result, "st_birthtime"), reason="这个系统不记创建时间")
def test_s1_birthtime_kept(vault):
    paths = [vault / "Spark" / "剪藏" / "20261103_文章.md", vault / "Spark" / "Show" / "transcripts" / "20260915_第一期.md"]
    old = 1_600_000_000
    for p in paths:
        os.utime(p, (old, old))          # 修改时间早于创建时间：创建时间一起往前拉到这里
    births = {p.name: os.stat(p).st_birthtime for p in paths}
    assert all(abs(b - old) < 2 for b in births.values())
    for p in paths:
        os.utime(p, (old + 86400 * 30, old + 86400 * 30))   # 修改时间比创建时间晚一个月
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    for p in paths:
        st = os.stat(p)
        assert CAP in p.read_text(encoding="utf-8")
        assert abs(st.st_birthtime - births[p.name]) < 2, p
        assert int(st.st_mtime) == old + 86400 * 30


def _phone_saves(vault, monkeypatch, conflicts):
    from core import remote_tasks
    monkeypatch.setenv("SPARK_VAULT", str(vault))
    monkeypatch.setattr(remote_tasks, "note_conflicts_dir", lambda: str(conflicts))
    monkeypatch.setattr(remote_tasks.clips, "download_images", lambda images, root: (0, 0))

    def save(path, content, base):
        return remote_tasks.save_file(remote_tasks.parse_task({"type": "save", "path": path, "content": content,
                                                               "replace": True, "base": base}))
    return save


def test_s2_second_phone_edit_after_carried_header_is_not_a_conflict(vault, monkeypatch):
    """手机在迁移前那版上改了两次（第二次的 base 是它本地第一次改完那份）：电脑接回时间行之后，第二次也照收。"""
    conflicts = vault.parent / "conflicts"
    save = _phone_saves(vault, monkeypatch, conflicts)
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()   # noqa: E731
    first = CLIP.replace("后面。", "后面，==第一次==。")
    second = first.replace("我的读后感。", "我的读后感。又写了一句。")
    save("Spark/剪藏/20261105_文章.md", first, sha(CLIP))
    save("Spark/剪藏/20261105_文章.md", second, sha(first))
    got = (vault / "Spark" / "剪藏" / "20261103_文章_2.md").read_text(encoding="utf-8")
    assert "==第一次==" in got and "又写了一句" in got and f"- {PUB}：2026年11月03日\n" in got
    rel = "Spark/Show/transcripts/20260915_第一期.md"
    t1 = TRANSCRIPT.replace("正文 ==重点==", "正文 ==重点== ==一==")
    t2 = t1.replace("==一==", "==一== ==二==")
    save(rel, t1, sha(TRANSCRIPT))
    save(rel, t2, sha(t1))
    got = (vault / rel).read_text(encoding="utf-8")
    assert "==一== ==二==" in got and f"- 播出时间：2026-09-15\n- {CAP}：2026年09月16日\n" in got
    assert doctime.parse(doctime.PUBLISHED_LINE.search(got).group(1)).isoformat() == "2026-09-15"
    assert not conflicts.exists()


def test_o1_before_resets_when_file_changed_between_runs(vault):
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    rel = "Spark/Show/transcripts/20260915_第一期.md"
    p = vault / rel
    other = TRANSCRIPT.replace("正文", "电脑上改过的正文")   # 又变回旧写法、内容也变了
    p.write_text(other, encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    entry = json.loads((vault / "Spark" / "剪藏" / ".renames.json").read_text(encoding="utf-8"))["rewritten"][rel]
    assert entry["before"] == [hashlib.sha256(other.encode()).hexdigest()], "中间被别的改过：只认这次改之前那版"


def test_o3_lock_blocks_second_apply_and_port_env(vault, monkeypatch):
    lock = vault / "Spark" / "剪藏" / mt.LOCK_NAME
    lock.write_text("1 x\n", encoding="utf-8")
    before = _snapshot(vault)
    assert mt.main(["--root", str(vault), "--apply"]) == 4
    assert _snapshot(vault) == before
    stale = time.time() - mt.LOCK_STALE_SECONDS - 5
    os.utime(lock, (stale, stale))
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    assert not lock.exists(), "跑完删掉锁"
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-c", "from tools import migrate_times as m; print(m.SERVER_PORT)"],
                         capture_output=True, text=True, env={**os.environ, "SPARK_PORT": "9123"},
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.stdout.strip() == "9123"


# ---------------------------------------------------------------- 剪藏以外的「播出时间」原样留着

def test_aired_kept_and_captured_added(vault):
    """节目的文字记录写着「- 播出时间：…」：那行一字不改，下面紧接着加收录时间，不加发布时间；再跑一遍不重复加。
    剪藏照旧换成发布时间、收录时间；没有时间行的单集笔记照旧加两行。读的人（core/doctime）读得出发布那天和收录那天。"""
    out = io.StringIO()
    plan = mt.build(str(vault))
    mt.report(plan, out)
    assert "保留「播出时间」1 篇（其中这次加了" in out.getvalue()
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    t = (vault / "Spark" / "Show" / "transcripts" / "20260915_第一期.md").read_text(encoding="utf-8")
    assert "- 播出时间：2026-09-15\n" in t and t.count(f"- {CAP}：") == 1 and f"- {PUB}：" not in t
    assert t.index("- 播出时间：") < t.index(f"- {CAP}：") and t.split("- 播出时间：2026-09-15\n")[1].startswith(f"- {CAP}：")
    # 读的人：发表那天从播出时间那行读，收录那天从收录时间那行读（现有代码，不改）
    assert doctime.parse(doctime.PUBLISHED_LINE.search(t).group(1)).isoformat() == "2026-09-15"
    assert doctime.parse(doctime.CAPTURED_LINE.search(t).group(1)).isoformat() == "2026-09-16"
    lines = doctime.header_time_lines(t)
    assert lines["pub"] == f"- {PUB}：2026年09月15日" and lines["cap"] == f"- {CAP}：2026年09月16日"
    # 剪藏不受影响
    clip = (vault / "Spark" / "剪藏" / "20261103_文章_2.md").read_text(encoding="utf-8")
    assert f"- {PUB}：2026年11月03日\n- {CAP}：2026年11月05日\n" in clip and "- 发布：" not in clip
    # 没有时间行的单集笔记：照旧加两行
    note = (vault / "Spark" / "Show" / "notes" / "20260915 第一期.md").read_text(encoding="utf-8")
    assert f"# 第一期\n\n- {PUB}：2026年09月15日\n- {CAP}：2026年09月16日\n\n> 一句话\n" in note
    # 幂等
    again = mt.build(str(vault))
    assert again.writes == {} and again.renames == []
    out = io.StringIO()
    mt.report(again, out)
    assert "保留「播出时间」1 篇（其中这次加了" in out.getvalue() and f"加了{CAP} 0 篇" in out.getvalue()


def test_aired_only_for_that_label(vault):
    """只有「播出时间」这个标签留着：「- 发布日期：」照旧换成发布时间、收录时间。"""
    p = vault / "Spark" / "Show" / "transcripts" / "20260915_第一期.md"
    p.write_text(p.read_text(encoding="utf-8").replace("- 播出时间：2026-09-15", "- 发布日期：2026-09-15"), encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    t = p.read_text(encoding="utf-8")
    assert f"- {PUB}：2026年09月15日\n- {CAP}：2026年09月16日\n" in t and "发布日期" not in t


def test_aired_line_survives_stale_phone_edit(vault, monkeypatch):
    """手机迁移前那一版（只有「播出时间」）迁移后才交到电脑：接回时间行时电脑上的「播出时间」「收录时间」都留着。"""
    conflicts = vault.parent / "conflicts"
    save = _phone_saves(vault, monkeypatch, conflicts)
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    rel = "Spark/Show/transcripts/20260915_第一期.md"
    save(rel, TRANSCRIPT.replace("正文 ==重点==", "正文 ==重点== ==一=="), hashlib.sha256(TRANSCRIPT.encode()).hexdigest())
    got = (vault / rel).read_text(encoding="utf-8")
    assert f"- 播出时间：2026-09-15\n- {CAP}：2026年09月16日\n" in got and "==一==" in got
    assert got.count("- 播出时间：") == 1



def test_legacy_fabiao_relabelled_values_kept(vault):
    """上一版写的「- 发表时间：…」（剪藏、文字记录）换成「- 发布时间：…」，值一个字不动；「播出时间」照旧留着；再跑一遍不动。"""
    legacy = doctime.LEGACY_PUBLISHED_LABEL
    clip = vault / "Spark" / "剪藏" / "20261120_新剪藏.md"
    clip.write_text('---\ntitle: "新"\nsource: "s"\nurl: "https://e.com/n"\npublished: 2026-11-20\n'
                    'published_at: "2026-11-20T09:30:00+08:00"\nclipped: 2026-11-21\nclipped_at: "2026-11-21T08:00:00+08:00"\n---\n\n'
                    f"# 新\n\n- 来源：s\n- {legacy}：2026年11月20日，09:30（UTC+8）\n- 收录时间：2026年11月21日，08:00（UTC+8）\n\n## 正文\n\n文\n",
                    encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    got = clip.read_text(encoding="utf-8")
    assert f"- {PUB}：2026年11月20日，09:30（UTC+8）\n- {CAP}：2026年11月21日，08:00（UTC+8）\n" in got and legacy not in got
    t = (vault / "Spark" / "Show" / "transcripts" / "20260915_第一期.md").read_text(encoding="utf-8")
    assert "- 播出时间：2026-09-15\n" in t
    again = mt.build(str(vault))
    assert again.writes == {} and again.renames == []


def test_lock_message_and_stale_tmp_cleanup(vault, capsys):
    """被锁挡住：提示写明锁文件的路径、10 分钟后过期、确认没人在跑可以删。拿到锁后清掉上次中断留下的 .md.<uuid>.tmp
    （只认这个命名、只清 10 分钟没动过的，别的文件不碰）。"""
    lock = vault / "Spark" / "剪藏" / mt.LOCK_NAME
    lock.write_text("1 x\n", encoding="utf-8")
    assert mt.main(["--root", str(vault), "--apply"]) == 4
    err = capsys.readouterr().err
    assert str(lock) in err and "10 分钟" in err and "删掉" in err
    lock.unlink()
    show = vault / "Spark" / "Show" / "transcripts"
    stale = show / ("20260915_第一期.md." + "a" * 32 + ".tmp")
    fresh = show / ("x.md." + "b" * 32 + ".tmp")
    other = show / "notes.tmp"
    for p in (stale, fresh, other):
        p.write_text("x", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    assert mt.main(["--root", str(vault), "--apply"]) == 0
    assert not stale.exists() and fresh.exists() and other.exists()
    assert "临时文件 1 个" in capsys.readouterr().out


def test_merge_rewritten_rerun_after_interrupt_keeps_before():
    """依次记 v0→v1、v1→v2、v2→v3；中断后（记录已写、文件还是 v2）重跑再记一次 v2→v3：before 不被清空。"""
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()   # noqa: E731
    rw = {}
    for old, new in (("v0", "v1"), ("v1", "v2"), ("v2", "v3")):
        mt.merge_rewritten(rw, "x", old, new)
    assert rw["x"] == {"before": [sha("v0"), sha("v1"), sha("v2")], "after": sha("v3")}
    mt.merge_rewritten(rw, "x", "v2", "v3")
    assert rw["x"] == {"before": [sha("v0"), sha("v1"), sha("v2")], "after": sha("v3")}
    mt.merge_rewritten(rw, "x", "别处改过", "v4")   # 中间被别的改过：只认这次改之前那版
    assert rw["x"] == {"before": [sha("别处改过")], "after": sha("v4")}
