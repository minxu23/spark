"""流水线记会议日期：只补小结的那条路不丢发表日期；议程给了日期记 session_date、会议时间按议程；
整理会议目录（recording_organizer）不丢条目里的发表日期；手动填的会议时间流水线不改。"""

import json
import os
from unittest import mock

from apps.summit2md import pipeline
from apps.summit2md import recording_organizer as o, recording_guide as g
from apps.summit2md.tests.test_recording_organizer import fixture
from core import summit_dates as sd

SRC = "https://www.youtube.com/playlist?list=test"


def _job(root, title, entries, **kw):
    return pipeline.process_job(summit_title=title, source_url=SRC, entries=entries, output_base_dir=str(root),
                                backend="cli", api_key="", model="", lang_prefs=["en"], **kw)


def _existing(root, title, *, publish_date, event=None):
    out_dir = os.path.join(str(root), pipeline.sanitize_filename(title))
    os.makedirs(os.path.join(out_dir, "transcripts"))
    os.makedirs(os.path.join(out_dir, "speech"))
    entry = {"id": "v1", "title": "Talk", "url": "https://www.youtube.com/watch?v=v1", "duration": 60,
             "index": 1, "is_raw_session": False}
    saved = dict(entry, publish_date=publish_date, publish_at="2026-10-14T09:00:00+00:00")
    summary = {"tldr": "", "body": "_（摘要生成失败：temporary error）_"}
    t_rel, s_rel = "transcripts/001_Talk.md", "speech/001_Talk.md"
    with open(os.path.join(out_dir, t_rel), "w", encoding="utf-8") as f:
        f.write(pipeline.render_transcript_md(saved, title, [(0, "Hello.")], summary, "en",
                                              include_summary=False, speech_relative_path=s_rel))
    with open(os.path.join(out_dir, s_rel), "w", encoding="utf-8") as f:
        f.write(pipeline.render_speech_md(saved, title, "BODY", None, None, summary=summary,
                                          transcript_relative_path=t_rel, speech_lang_mode="original"))
    m = {"content_type": "summit", "entries": {"v1": {"rank": 1, "entry": saved, "ok": True, "error": None,
                                                      "relative_path": t_rel, "speech_relative_path": s_rel,
                                                      "summary": summary}}}
    if event:
        m["event_dates"] = event
    with open(os.path.join(out_dir, ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    return out_dir, entry


def test_summary_backfill_keeps_publish_date_and_sets_event_dates(tmp_path):
    """议题目录里来的条目没有发表日期：只补小结时不重新问 YouTube，原来记着的日期要接着存回去。"""
    out_dir, entry = _existing(tmp_path, "Conf", publish_date="20261014")
    with mock.patch.object(pipeline, "summarize", return_value="TLDR: ok\n- point"):
        _job(tmp_path, "Conf", [entry], do_summary=True, do_overall_summary=False)
    m = pipeline._load_manifest(out_dir)
    e = m["entries"]["v1"]["entry"]
    assert e["publish_date"] == "20261014" and e["publish_at"] == "2026-10-14T09:00:00+00:00"
    assert "event_dates" not in m, "视频发布日期不存成会议时间"


def test_carried_guessed_date_stays_guessed(tmp_path):
    """接过来的发表日期是手机上猜的（处理那天）：猜的标记一起接过来，不变成真的发表日期。"""
    out_dir, entry = _existing(tmp_path, "Conf", publish_date="20261120")
    m = pipeline._load_manifest(out_dir)
    m["entries"]["v1"]["entry"]["publish_guessed"] = True
    with open(os.path.join(out_dir, ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    with mock.patch.object(pipeline, "summarize", return_value="TLDR: ok\n- point"):
        _job(tmp_path, "Conf", [entry], do_summary=True, do_overall_summary=False)
    e = pipeline._load_manifest(out_dir)["entries"]["v1"]["entry"]
    assert e["publish_date"] == "20261120" and e["publish_guessed"] is True


def test_stale_sessions_event_dates_removed_and_track_folder_stores_none(tmp_path):
    stale = {"start": "2026-10-14", "end": "2026-10-14", "source": "sessions"}
    out_dir, entry = _existing(tmp_path, "Conf", publish_date="20261014", event=stale)
    with mock.patch.object(pipeline, "summarize", return_value="TLDR: ok\n- point"):
        _job(tmp_path, "Conf", [entry], do_summary=True, do_overall_summary=False)
    assert "event_dates" not in pipeline._load_manifest(out_dir)
    # 大会下面的专题文件夹（collection）：议程日期再多也不存会议时间
    entries = [{"id": f"t{i}", "title": f"T{i}", "url": f"https://www.youtube.com/watch?v=t{i}", "duration": 60,
                "index": i, "is_raw_session": False, "agenda_meta": {"day": "2026-10-13"}} for i in (1, 2)]
    collection = {"conference_title": "Big", "group_title": "Track", "source_url": "https://x.example", "group_folder": "Track"}
    with mock.patch.object(pipeline, "_process_entry", return_value={"ok": True, "relative_path": "transcripts/t.md"}):
        result = _job(tmp_path, "Big - Track", entries, do_summary=False, collection=collection)
    m = pipeline._load_manifest(result["output_dir"])
    assert m["entries"]["t1"]["entry"]["session_date"] == "2026-10-13" and "event_dates" not in m


def test_pipeline_never_overwrites_manual_event_dates(tmp_path):
    manual = {"start": "2026-10-13", "end": "2026-10-16", "source": "manual"}
    out_dir, entry = _existing(tmp_path, "Conf", publish_date="20261014", event=manual)
    with mock.patch.object(pipeline, "summarize", return_value="TLDR: ok\n- point"):
        _job(tmp_path, "Conf", [entry], do_summary=True, do_overall_summary=False)
    assert pipeline._load_manifest(out_dir)["event_dates"] == manual


def test_manual_edit_during_job_survives_pipeline_saves(tmp_path):
    """任务跑到一半在阅读页上改了会议时间：任务后面几次存处理记录都不冲掉它。"""
    out_dir, entry = _existing(tmp_path, "Conf", publish_date="20261014")

    def summarize(*_a, **_k):
        sd.set_manual(out_dir, "2026-10-12", "2026-10-13")
        return "TLDR: ok\n- point"
    with mock.patch.object(pipeline, "summarize", side_effect=summarize):
        _job(tmp_path, "Conf", [entry], do_summary=True, do_overall_summary=False)
    m = pipeline._load_manifest(out_dir)
    assert m["event_dates"] == {"start": "2026-10-12", "end": "2026-10-13", "source": "manual"}
    assert m["entries"]["v1"]["summary"]["tldr"] == "ok", "流水线自己的改动也在"


def test_agenda_day_becomes_session_date_and_event_dates(tmp_path):
    entries = [{"id": f"v{i}", "title": f"Talk {i}", "url": f"https://www.youtube.com/watch?v=v{i}", "duration": 60,
                "index": i, "is_raw_session": False,
                "agenda_meta": {"day": day, "time": "09:00", "order": i}} for i, day in enumerate(["2026-10-13", "2026-10-14", "Saturday"], 1)]

    def process(job, i, total, entry, existing, rank, do_summary):
        entry["publish_date"] = "20261101"
        return {"ok": True, "relative_path": f"transcripts/00{i}.md"}
    with mock.patch.object(pipeline, "_process_entry", side_effect=process):
        result = _job(tmp_path, "Agenda Conf", entries, do_summary=False)
    m = pipeline._load_manifest(result["output_dir"])
    days = {k: r["entry"].get("session_date") for k, r in m["entries"].items()}
    assert days == {"v1": "2026-10-13", "v2": "2026-10-14", "v3": None}, "只给了星期几的不算议程日期"
    assert m["event_dates"] == {"start": "2026-10-13", "end": "2026-10-14", "source": "agenda"}


def test_series_gets_no_event_dates(tmp_path):
    entry = {"id": "p1", "title": "Ep", "url": "https://www.youtube.com/watch?v=p1", "duration": 600,
             "index": 1, "is_raw_session": False, "publish_date": "20261014"}
    with mock.patch.object(pipeline, "_process_entry", return_value={"ok": True, "relative_path": "transcripts/x.md"}):
        result = _job(tmp_path, "Pod", [entry], do_summary=False, content_type="series")
    assert "event_dates" not in pipeline._load_manifest(result["output_dir"])


def test_organizer_keeps_publish_dates_from_processing(tmp_path):
    """整理会议目录时条目换成议题目录里的那份：处理时记下的发表日期、时刻、收录时间要接过来。"""
    root, data = fixture(tmp_path)
    m = json.loads((root / ".manifest.json").read_text())
    row = m["entries"]["z1xRELMKzrA"]
    row["entry"] = dict(row["entry"], publish_date="20261014", publish_at="2026-10-14T09:00:00+00:00",
                        captured_at="2026-10-20T08:00:00+00:00", sub_kind="auto")
    (root / ".manifest.json").write_text(json.dumps(m))
    o.apply(o.plan(data, tmp_path), tmp_path / "backup")
    for path in (root / ".manifest.json", root / "Networking" / ".manifest.json"):
        e = g._json(path)["entries"]["z1xRELMKzrA"]["entry"]
        assert e["publish_date"] == "20261014" and e["publish_at"] == "2026-10-14T09:00:00+00:00"
        assert e["captured_at"] == "2026-10-20T08:00:00+00:00" and e["sub_kind"] == "auto"
        assert e["collection"]["conference_title"] == data["summit_title"]
    assert "event_dates" not in g._json(root / ".manifest.json"), "视频发布日期不存"


def test_organizer_merge_never_drops_dates_from_summit_level(tmp_path):
    """专题文件夹那份没日期（补日期中途停下）、大会目录那份有：整理目录时不拿没日期的盖掉有日期的。"""
    root, data = fixture(tmp_path)
    o.apply(o.plan(data, tmp_path), tmp_path / "backup")
    agg = json.loads((root / ".manifest.json").read_text())
    agg["entries"]["z1xRELMKzrA"]["entry"].update(publish_date="20261014", publish_guessed=True)
    agg["event_dates"] = {"start": "2026-10-14", "end": "2026-10-14", "source": "sessions"}
    (root / ".manifest.json").write_text(json.dumps(agg))
    child = json.loads((root / "Networking" / ".manifest.json").read_text())
    child["event_dates"] = {"start": "2026-10-14", "source": "sessions"}
    child["entries"]["z1xRELMKzrA"]["entry"]["publish_guessed"] = False
    (root / "Networking" / ".manifest.json").write_text(json.dumps(child))
    o.apply(o.plan(data, tmp_path), tmp_path / "backup2")
    for path in (root / ".manifest.json", root / "Networking" / ".manifest.json"):
        m = g._json(path)
        e = m["entries"]["z1xRELMKzrA"]["entry"]
        assert e["publish_date"] == "20261014" and e["publish_guessed"] is True, "日期和猜的标记一起拿"
        assert "event_dates" not in m, "老的 sessions、专题文件夹的会议时间都去掉"
