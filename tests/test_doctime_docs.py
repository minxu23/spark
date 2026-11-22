"""节目、信息跟进、剪藏的文档都写「发表时间」「收录时间」两行；源里的时刻记下来；读文档头的地方新旧写法都认。"""

import os
import time
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from apps.notes2insight import vault as n2i_vault
from apps.reader import server as reader
from apps.summit2md import label_speakers, pipeline, tracking
from core import clip_capture, clips, doctime, remote_tasks, sources
from tests.test_clip_capture import capture, payload, wait  # noqa: F401  （fixture）


@pytest.fixture
def shanghai():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


ENTRY = {"id": "v1", "title": "第一期", "url": "https://www.youtube.com/watch?v=v1", "duration": 3600,
         "publish_date": "20261119", "publish_at": "2026-11-19T21:12:00+08:00", "captured_at": "2026-11-20T08:05:00+08:00"}


def test_transcript_speech_note_lines(shanghai):
    t = pipeline.render_transcript_md(dict(ENTRY), "节目", [(0.0, "正文")], None, "zh", content_type="series")
    assert "- 时长：约 1:00:00\n- 发表时间：2026年11月19日，21:12（UTC+8）\n- 收录时间：2026年11月20日，08:05（UTC+8）\n" in t
    assert "播出时间" not in t
    s = pipeline.render_speech_md(dict(ENTRY), "节目", "稿子", None, None, content_type="series", sub_lang="zh")
    assert "- 发表时间：2026年11月19日，21:12（UTC+8）\n- 收录时间：" in s
    row = {"entry": dict(ENTRY), "summary": {"tldr": "一句话", "body": "### 要点\n\n- a"}, "relative_path": "transcripts/x.md"}
    note = pipeline.render_episode_note(row, "节目")
    assert '播出: 2026-11-19\npublished_at: "2026-11-19T21:12:00+08:00"\ncaptured_at: "2026-11-20T08:05:00+08:00"\n' in note
    assert "# 第一期\n\n- 发表时间：2026年11月19日，21:12（UTC+8）\n- 收录时间：2026年11月20日，08:05（UTC+8）\n\n> 一句话" in note


def test_summit_session_without_publish_date_gets_capture_time_only(shanghai):
    entry = {"id": "s", "title": "议题", "url": "u", "duration": 60}
    t = pipeline.render_transcript_md(entry, "大会", [(1.0, "x")], None, "en")
    assert "发表时间" not in t and "- 收录时间：" in t
    assert doctime.parse(entry["captured_at"]), "收录时刻记回条目，跟着存进处理记录"
    again = pipeline.render_transcript_md(entry, "大会", [(1.0, "x")], None, "en")
    assert again == t, "再写一遍收录时间不变"


def test_guessed_publish_date_not_written(shanghai):
    e = dict(ENTRY, publish_guessed=True)
    e.pop("publish_at")
    t = pipeline.render_transcript_md(e, "节目", [(0.0, "x")], None, "en", content_type="series")
    assert "发表时间" not in t
    note = pipeline.render_episode_note({"entry": e, "summary": {"tldr": "", "body": "x"}}, "节目")
    assert "播出:" not in note and "发表时间" not in note


def test_old_note_row_date_from_file_name(shanghai):
    row = {"entry": {"id": "a", "title": "老", "captured_at": "2026-11-20T08:05:00+08:00"},
           "summary": {"tldr": "", "body": "x"}, "relative_path": "transcripts/20250912_老.md"}
    note = pipeline.render_episode_note(row, "节目")
    assert "播出: 2025-09-12" in note and "- 发表时间：2025年09月12日\n- 收录时间：" in note


def test_track_note_lines(shanghai):
    e = {"id": "a" * 16, "title": "T", "url": "https://x", "publish_date": "20261119", "captured_at": "2026-11-20T08:05:00+08:00"}
    text = tracking.render_note(e, "量子位", {"tldr": "", "body": "b"}, ["p"])
    assert text.startswith("# T\n\n- 来源：量子位\n- 发表时间：2026年11月19日\n- 收录时间：2026年11月20日，08:05（UTC+8）\n- 原文链接：")


def test_sources_capture_real_times(shanghai):
    e = {"published": "Thu, 19 Nov 2026 13:12:00 GMT", "published_parsed": time.strptime("2026-11-19 13:12", "%Y-%m-%d %H:%M")}
    assert sources._rss_published(e) == datetime(2026, 11, 19, 13, 12, tzinfo=timezone.utc)
    assert sources._rss_publish_date(dict(e, published="Thu, 19 Nov 2026 20:30:00 GMT",
                                          published_parsed=time.strptime("2026-11-19 20:30", "%Y-%m-%d %H:%M"))) == "20261120"
    day_only = {"published": "2026-11-19", "published_parsed": time.strptime("2026-11-19", "%Y-%m-%d")}
    assert sources._rss_published(day_only).isoformat() == "2026-11-19", "只写了哪天的不补 00:00"
    soup = BeautifulSoup('<meta property="article:published_time" content="2026-11-19T13:12:00Z">', "html.parser")
    assert sources._guess_article_published(soup) == datetime(2026, 11, 19, 13, 12, tzinfo=timezone.utc)
    soup = BeautifulSoup('<time datetime="2026-11-19T21:12">x</time>', "html.parser")
    assert sources._guess_article_publish_date(soup) == "20261119"
    entry = {}
    assert pipeline.stamp_video_publish(entry, {"upload_date": "20261119", "timestamp": 1795093920})
    assert entry == {"publish_date": "20261119", "publish_at": "2026-11-19T21:12:00+08:00"}
    entry = {}
    assert pipeline.stamp_video_publish(entry, {"upload_date": "20261119", "release_timestamp": 1795127400, "timestamp": 1})
    assert entry["publish_date"] == "20261120", "首播时刻优先，日期取本机时区的那天"
    entry = {}
    assert pipeline.stamp_video_publish(entry, {"upload_date": "20261119"}) and entry == {"publish_date": "20261119"}
    sub = pipeline._substack_item_to_entry({"slug": "s", "post_date": "2026-11-19T13:12:00.000Z"}, 1)
    assert sub["publish_at"] == "2026-11-19T21:12:00+08:00" and sub["publish_date"] == "20261119"


def test_phone_tasks_keep_time_fields():
    base = {"type": "episode_item", "folder": "Spark/节目", "summary": {"tldr": "", "body": ""}, "paragraphs": [[0, "x"]],
            "entry": {"id": "v1", "publish_date": "20261119", "publish_at": "2026-11-19T21:12:00+08:00",
                      "captured_at": "坏的", "publish_guessed": True}}
    got = remote_tasks._parse_episode_item(base)["entry"]
    assert got["publish_at"] == "2026-11-19T21:12:00+08:00" and "captured_at" not in got and got["publish_guessed"] is True


def test_readers_accept_old_and_new_lines(tmp_path, shanghai):
    assert reader._meta_label({"发表时间": "2026年11月19日，21:12（UTC+8）", "所属节目": "S"}, "") == "S · 2026-11-19"
    assert reader._meta_label({"播出时间": "2026-09-15"}, "") == "2026-09-15"
    note = tmp_path / "x.md"
    note.write_text('---\npublished: 2026-11-19\n---\n\n# 标题\n', encoding="utf-8")
    assert reader._file_info(str(note)) == ("标题", "2026-11-19")
    for head, want in (("# T\n\n- 发表时间：2026年11月19日，21:12（UTC+8）\n", "2026-11-19"),
                       ("# T\n\n- 发布日期：2026-09-15\n", "2026-09-15")):
        note.write_text(head, encoding="utf-8")
        assert n2i_vault._parse_head(str(note), "x.md")[1] == want
    old = "- 时长：约 1:00:00\n- 播出时间：2026-09-15\n- 说明：x"
    assert pipeline._ensure_publish_date_line(old, "20260915") == old
    new = pipeline._ensure_publish_date_line("# T\n\n- 时长：约 1:00:00\n- 说明：x", "20260915")
    assert "- 时长：约 1:00:00\n- 发表时间：2026年09月15日\n- 说明：x" in new
    assert pipeline._ensure_publish_date_line(new, "20260915") == new
    head = "# T\n\n- 链接：u\n- 时长：约 1:00\n- 发表时间：2026年09月15日\n- 收录时间：2026年09月16日，10:12（UTC+8）"
    labelled = label_speakers.set_header(head)
    assert labelled.endswith("- 收录时间：2026年09月16日，10:12（UTC+8）\n" + label_speakers.AI_LABEL_LINE)


def test_clip_capture_names_by_publish_day(capture, shanghai):  # noqa: F811
    job = wait(clip_capture.start(payload(title="有日期", published="2026-10-18", publishedAt="2026-10-18T13:12:00Z")))
    assert job["path"] == "Spark/剪藏/20261018_有日期.md", job
    text = (capture / job["path"]).read_text()
    assert "published_at: \"2026-10-18T21:12:00+08:00\"\nclipped: " in text and "\nclipped_at: \"" in text
    assert "- 发表时间：2026年10月18日，21:12（UTC+8）\n- 收录时间：" in text
    today = doctime.now().strftime("%Y%m%d")
    job = wait(clip_capture.start(payload(title="没日期")))
    assert job["path"] == f"Spark/剪藏/{today}_没日期.md"
    job = wait(clip_capture.start(payload(title="有日期", published="2026-10-18", markdown="另一篇")))
    assert job["path"] == "Spark/剪藏/20261018_有日期_2.md", "同名的照旧加 _2"
    c = {x["name"]: x for x in clips.load_clips(str(capture))}["20261018_有日期.md"]
    assert c["clipped"] == doctime.now().date().isoformat(), "收录日期读文件头，不是文件名前面的发表日期"
    with pytest.raises(remote_tasks.TaskError):
        clip_capture.parse(payload(publishedAt="2026-10-18"))   # 只有日期的不算时刻


# ---------------------------------------------------------------- 评审：T1、T5、T6、M1、T7

def test_t5_newer_than_processed_compares_instants_or_days_with_slack(shanghai):
    done = {"old": {"ok": True, "entry": {"publish_date": "20261119", "publish_at": "2026-11-19T21:12:00+08:00"}}}
    entries = [{"id": "a", "publish_date": "20261119", "publish_at": "2026-11-19T22:00:00+08:00"},   # 同一天、晚一点
               {"id": "b", "publish_date": "20261119", "publish_at": "2026-11-19T20:00:00+08:00"},   # 同一天、早一点
               {"id": "c", "publish_date": "20261118"},                                            # 只有日期、早一天：留着
               {"id": "d", "publish_date": "20261117"},
               {"id": "old", "publish_date": "20261119"}]
    assert tracking.newer_than_processed(entries, done) == {"a", "c"}
    guessed = {"g": {"ok": True, "entry": {"publish_date": "20261125", "publish_guessed": True},
                     "relative_path": "x.md"}}
    assert tracking.newer_than_processed([{"id": "z", "publish_date": "20261120"}, {"id": "g"}], guessed) is None, \
        "手机上猜的处理日期不当最新一期"


def test_t5_moved_entries_match_with_one_day_slack():
    rows = {"old": {"ok": True, "entry": {"id": "old", "title": "一个足够长的标题啊啊", "url": "https://a/1", "publish_date": "20261118"}}}
    new = [{"id": "new", "title": "一个足够长的标题啊啊", "url": "https://a/2", "publish_date": "20261119", "source_type": "rss"}]
    assert pipeline.match_moved_entries(rows, new) == {"new": "old"}, "UTC 那天和本机那天差一天也认"
    far = [dict(new[0], publish_date="20261121")]
    assert pipeline.match_moved_entries(rows, far) == {}


def test_t6_readers_accept_old_lines(tmp_path):
    note = tmp_path / "x.md"
    for head, want in (("# T\n\n- 播出时间：2026-09-15\n", "2026-09-15"), ("# T\n\n- 发布：2026-09-14\n", "2026-09-14")):
        note.write_text(head, encoding="utf-8")
        assert n2i_vault._parse_head(str(note), "x.md")[1] == want
    with open(os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "LocalNotes.swift"), encoding="utf-8") as f:
        assert "(?:发布日期|日期|发表时间|播出时间|发布)" in f.read(), "手机 LocalNotes.parseHead 一样认"


def test_t1_reader_episode_order_by_instant_then_rank_then_title(tmp_path, monkeypatch, shanghai):
    show = tmp_path / "Spark" / "节目"
    (show / "transcripts").mkdir(parents=True)
    rows = {}
    for i, (vid, at, rank) in enumerate((("a", "2026-11-19T09:00:00-08:00", 3), ("b", "2026-11-19T23:30:00+08:00", 1),
                                          ("c", "", 2), ("d", "", 5))):
        name = f"20261119_{vid}.md"
        (show / "transcripts" / name).write_text(f"# {vid}\n", encoding="utf-8")
        entry = {"id": vid, "title": vid, "publish_date": "20261119"}
        if at:
            entry["publish_at"] = at
        rows[vid] = {"ok": True, "rank": rank, "entry": entry, "relative_path": f"transcripts/{name}"}
    (show / ".manifest.json").write_text(__import__("json").dumps({"entries": rows}), encoding="utf-8")
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(reader, "_root", lambda: str(tmp_path / "Spark"))
    got = [e["title"] for e in reader._episodes(str(show))]
    assert got == ["a", "b", "d", "c"], "有时刻的按绝对时刻（a 是 17:00Z），只有日期的排后面、按处理顺序"


def test_t1_reader_clip_items_published_order(tmp_path, monkeypatch, shanghai):
    folder = tmp_path / "Spark" / "剪藏"
    folder.mkdir(parents=True)
    for name, pub, at in (("20261119_A.md", "2026-11-19", "2026-11-19T09:00:00-08:00"),
                          ("20261119_B.md", "2026-11-19", "2026-11-19T23:30:00+08:00"),
                          ("20261119_C.md", "2026-11-19", "")):
        fm = f'---\ntitle: "{name}"\npublished: {pub}\n' + (f'published_at: "{at}"\n' if at else "") + "clipped: 2026-11-20\n---\n\n# x\n"
        (folder / name).write_text(fm, encoding="utf-8")
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(reader, "_root", lambda: str(tmp_path / "Spark"))
    with reader.app.test_request_context():
        items = reader._clip_items(set(), "published")
    assert [i["title"] for i in items] == ["20261119_A.md", "20261119_B.md", "20261119_C.md"]
    with open(os.path.join(os.path.dirname(__file__), "..", "apps", "macos", "Sources", "Spark", "Model.swift"), encoding="utf-8") as f:
        assert "DocTime.sortKey(at: i.publishedAt" in f.read(), "Mac App 按发表时间排用同一个键"


def test_keep_time_lines_and_carry_time_header():
    old = "# T\n\n- 链接：u\n- 时长：约 1:00\n- 播出时间：2026-10-15\n\n正文\n"
    new = "# T\n\n- 链接：u\n- 时长：约 1:00\n- 收录时间：2026年11月22日，10:00（UTC+8）\n\n正文\n"
    assert doctime.keep_time_lines(old, new) == \
        "# T\n\n- 链接：u\n- 时长：约 1:00\n- 发表时间：2026年10月15日\n- 收录时间：2026年11月22日，10:00（UTC+8）\n\n正文\n"
    migrated = '---\ntitle: "T"\npublished: 2026-11-03\nclipped: 2026-11-05\nclipped_at: "2026-11-05T08:00:00+08:00"\n---\n\n' \
               "# T\n\n- 来源：x\n- 发表时间：2026年11月03日\n- 收录时间：2026年11月05日，08:00（UTC+8）\n\n正文\n"
    phone = '---\ntitle: "T"\npublished: 2026-11-03\n---\n\n# T\n\n- 来源：x\n- 发布：2026-11-03\n- 剪藏：2026-11-05\n\n正文改了\n'
    assert doctime.carry_time_header(migrated, phone) == \
        '---\ntitle: "T"\npublished: 2026-11-03\nclipped: 2026-11-05\nclipped_at: "2026-11-05T08:00:00+08:00"\n---\n\n' \
        "# T\n\n- 来源：x\n- 发表时间：2026年11月03日\n- 收录时间：2026年11月05日，08:00（UTC+8）\n\n正文改了\n"
    assert doctime.carry_time_header(migrated, migrated.replace("正文", "新的")) == migrated.replace("正文", "新的"), \
        "已经是新写法的不动"
