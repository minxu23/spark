"""补会议日期的工具 tools/backfill_summit_dates.py：只列计划、照计划改、再跑不改；
日期从文件头、字幕缓存、议程、联网（假的）补；查不到连着 5 次停；联网结果记下来再跑不重查；
CRLF 不改、修改时间照旧、已有的日期行不动、处理记录别的内容不动、手动填的会议时间不动。"""

import json
import os
import time

import pytest

from core import summit_dates as sd
from tools import backfill_summit_dates as bf

YT = "https://www.youtube.com/watch?v="


def _entry(vid, **kw):
    return {"id": vid, "title": f"Talk {vid}", "url": YT + vid, "duration": 60, "is_raw_session": False, **kw}


def _save(folder, data, tail=""):
    os.makedirs(folder, exist_ok=True)
    with open(sd.manifest_path(folder), "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + tail)


def _load(folder):
    with open(sd.manifest_path(folder), encoding="utf-8") as f:
        return json.load(f)


def _doc(title, *items):
    return "\n".join([f"# {title}", "", *items, "", "## 文字记录", "", "正文。", ""])


@pytest.fixture
def vault(tmp_path):
    spark = tmp_path / "Spark"
    conf = spark / "Conf"
    for d in ("transcripts", "speech", ".cache/subtitles", "Theme/transcripts"):
        (conf / d).mkdir(parents=True)
    files = {
        "transcripts/001_a.md": _doc("a", "- 链接：x", "- 时长：约 1:00", "- 字幕来源：YouTube"),
        "transcripts/002_b.md": _doc("b", "- 时长：约 1:00", "- 播出时间：2026-10-15"),
        "speech/002_b.md": _doc("b", "- 时长：约 1:00"),
        "transcripts/003_c.md": _doc("c", "- 链接：x"),
        "transcripts/004_d.md": _doc("d", "- 时长：约 1:00"),
        "transcripts/005_e.md": _doc("e", "- 时长：约 1:00"),
        "transcripts/006_f.md": _doc("f", "- 时长：约 1:00").replace("\n", "\r\n"),
    }
    for rel, text in files.items():
        with open(conf / rel, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    old = time.time() - 86400 * 30
    for rel in files:
        os.utime(conf / rel, (old, old))
    (conf / ".cache/subtitles/c.meta.json").write_text(json.dumps({"upload_date": "20261016", "kind": "auto"}))
    rows = {
        "a": {"rank": 1, "entry": _entry("a", publish_date="20261014"), "ok": True, "relative_path": "transcripts/001_a.md"},
        "b": {"rank": 2, "entry": _entry("b"), "ok": True, "relative_path": "transcripts/002_b.md",
              "speech_relative_path": "speech/002_b.md"},
        "c": {"rank": 3, "entry": _entry("c", publish_date=None), "ok": True, "relative_path": "transcripts/003_c.md"},
        "d": {"rank": 4, "entry": _entry("d", agenda_meta={"day": "2026-10-13", "time": "09:00"}), "ok": True,
              "relative_path": "transcripts/004_d.md"},
        "e": {"rank": 5, "entry": _entry("e", agenda_meta={"day": "2026-10-14"}), "ok": True, "relative_path": "transcripts/005_e.md"},
        "f": {"rank": 6, "entry": _entry("f", agenda_meta={"day": "2026-10-14"}), "ok": True, "relative_path": "transcripts/006_f.md"},
    }
    _save(str(conf), {"overall_summary": "大会于 2026年10月13日至16日举行", "content_type": "summit",
                      "source_url": "https://conf.example/agenda", "entries": rows, "topic_groups": {"x": ["a"]}}, tail="\n")
    _save(str(conf / "Theme"), {"content_type": "summit", "entries": {"b": {"rank": 2, "entry": _entry("b"), "ok": True,
                                                                         "relative_path": "transcripts/002_b.md"}}})
    # 手动填过会议时间的会议：不动
    _save(str(spark / "Manual"), {"content_type": "summit", "event_dates": {"start": "2026-01-01", "end": "2026-01-02", "source": "manual"},
                                  "entries": {"m": {"rank": 1, "entry": _entry("m", publish_date="20260301"), "ok": True}}})
    # 视频传了三周：不当会议时间
    _save(str(spark / "Spread"), {"overall_summary": "Held May 1-3, 2026 in SF.", "entries": {
        "s1": {"rank": 1, "entry": _entry("s1", publish_date="20260501"), "ok": True},
        "s2": {"rank": 2, "entry": _entry("s2", publish_date="20260522"), "ok": True}}})
    _save(str(spark / "Pod"), {"content_type": "series", "entries": {"p": {"rank": 1, "entry": _entry("p"), "ok": True}}})
    return tmp_path


class FakeFetch:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, url):
        vid = url.rsplit("=", 1)[1]
        self.calls.append(vid)
        a = self.answers.get(vid)
        if isinstance(a, Exception) or a is None:
            raise a or RuntimeError("Video unavailable")
        return a


def _by(summits):
    return {s.name: s for s in summits}


def _snapshot(root):
    out = {}
    for folder, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(folder, n)
            with open(p, "rb") as f:
                out[p] = (f.read(), os.stat(p).st_mtime_ns)
    return out


def test_dry_run_plans_without_writing(vault):
    before = _snapshot(vault / "Spark")
    fetch = FakeFetch({"d": {"upload_date": "20261020"}})
    s = _by(bf.build(str(vault), fetch=fetch, log=lambda *_: None))
    assert "Pod" not in s
    conf = s["Conf"]
    assert (conf.total, conf.dated, conf.from_files, conf.from_meta, conf.from_network) == (6, 1, 1, 1, 1)
    assert [v for v, _ in conf.failed] == ["e", "f"]
    assert conf.updates["b"] == {"publish_date": "20261015"}
    assert conf.updates["c"] == {"publish_date": "20261016"}
    assert conf.updates["d"] == {"session_date": "2026-10-13", "publish_date": "20261020"}
    assert conf.event_after == {"start": "2026-10-13", "end": "2026-10-14", "source": "agenda"} and conf.event_change
    assert ("Spark/Conf/transcripts/006_f.md", "CRLF 换行") not in conf.skipped, "f 没有日期，不用补"
    assert s["Manual"].event_after["source"] == "manual" and not s["Manual"].event_change
    assert s["Spread"].event_after is None and s["Spread"].manual_hint == ("2026-05-01", "2026-05-22")
    assert set(conf.manifest_changes) == {str(vault / "Spark" / "Conf"), str(vault / "Spark" / "Conf" / "Theme")}
    after = _snapshot(vault / "Spark")
    cache = str(vault / "Spark" / "Conf" / ".cache" / bf.CACHE_NAME)
    assert {k: v for k, v in after.items() if k != cache} == before, "只列计划时只写联网结果的缓存"
    assert json.load(open(cache))["d"] == {"upload_date": "20261020"}


def test_no_network_counts_what_needs_network(vault):
    conf = _by(bf.build(str(vault), fetch=None))["Conf"]
    assert sorted(conf.need_network) == ["d", "e", "f"] and conf.from_network == 0
    assert not os.path.exists(vault / "Spark" / "Conf" / ".cache" / bf.CACHE_NAME)


def test_apply_writes_only_dates_and_is_idempotent(vault):
    conf_dir = str(vault / "Spark" / "Conf")
    raw_before = open(sd.manifest_path(conf_dir), encoding="utf-8").read()
    manual_before = open(sd.manifest_path(str(vault / "Spark" / "Manual")), "rb").read()
    mtimes = {n: os.stat(os.path.join(conf_dir, n)).st_mtime_ns for n in
              ("transcripts/001_a.md", "transcripts/002_b.md", "speech/002_b.md", "transcripts/006_f.md")}
    crlf_before = open(os.path.join(conf_dir, "transcripts/006_f.md"), "rb").read()
    fetch = FakeFetch({"d": {"upload_date": "20261020", "timestamp": 1792500000}, "f": {"upload_date": "20261017"}})
    summits = bf.build(str(vault), fetch=fetch, log=lambda *_: None)
    bf.apply(summits, log=lambda *_: None)

    m = _load(conf_dir)
    assert m["entries"]["b"]["entry"]["publish_date"] == "20261015"
    assert m["entries"]["c"]["entry"]["publish_date"] == "20261016", "原来是 null 的也补上，位置不变"
    assert list(m["entries"]["c"]["entry"]).index("publish_date") == 5
    d = m["entries"]["d"]["entry"]
    assert d["session_date"] == "2026-10-13" and d["publish_date"] and d["publish_at"].startswith("2026-")
    assert m["entries"]["a"]["entry"]["publish_date"] == "20261014"
    assert m["event_dates"] == {"start": "2026-10-13", "end": "2026-10-14", "source": "agenda"}
    # 别的内容一个字节不动：把补的字段拿掉、event_dates 拿掉，跟原来一样（结尾的换行也留着）
    raw_after = open(sd.manifest_path(conf_dir), encoding="utf-8").read()
    assert raw_after.endswith("}\n")
    orig = json.loads(raw_before)
    for vid, row in orig["entries"].items():
        bf._fill(row["entry"], bf._missing(row["entry"], summits[0].truth[vid]))
    orig["event_dates"] = m["event_dates"]
    assert raw_after == json.dumps(orig, ensure_ascii=False, indent=2) + "\n"
    # 专题文件夹的处理记录里同一场也补
    assert _load(os.path.join(conf_dir, "Theme"))["entries"]["b"]["entry"]["publish_date"] == "20261015"
    assert "event_dates" not in _load(os.path.join(conf_dir, "Theme"))
    # 手动填的不动
    assert open(sd.manifest_path(str(vault / "Spark" / "Manual")), "rb").read() == manual_before

    a = open(os.path.join(conf_dir, "transcripts/001_a.md"), encoding="utf-8").read()
    assert "- 时长：约 1:00\n- 发表时间：2026年10月14日\n- 字幕来源" in a
    b = open(os.path.join(conf_dir, "transcripts/002_b.md"), encoding="utf-8").read()
    assert "- 播出时间：2026-10-15" in b and "发表时间" not in b, "已有的日期行不动"
    sb = open(os.path.join(conf_dir, "speech/002_b.md"), encoding="utf-8").read()
    assert "- 时长：约 1:00\n- 发表时间：2026年10月15日\n" in sb
    c = open(os.path.join(conf_dir, "transcripts/003_c.md"), encoding="utf-8").read()
    assert "- 链接：x\n- 发表时间：2026年10月16日\n" in c
    assert open(os.path.join(conf_dir, "transcripts/006_f.md"), "rb").read() == crlf_before, "CRLF 的不改"
    assert ("Spark/Conf/transcripts/006_f.md", "CRLF 换行") in summits[0].skipped
    for n in ("transcripts/001_a.md", "speech/002_b.md"):
        assert os.stat(os.path.join(conf_dir, n)).st_mtime_ns == mtimes[n], "补了一行，修改时间照旧"

    # 再跑一遍：什么都不改，也不重新联网
    calls = len(fetch.calls)
    again = bf.build(str(vault), fetch=fetch, log=lambda *_: None)
    assert not any(s.changes for s in again)
    assert fetch.calls[calls:] == ["e"], "查到的记下来了不重查；没查到的下次再试"


def test_stops_after_five_failures_and_resumes_from_cache(tmp_path):
    folder = tmp_path / "Spark" / "Big"
    rows = {f"v{i}": {"rank": i, "entry": _entry(f"v{i}"), "ok": True} for i in range(8)}
    _save(str(folder), {"content_type": "summit", "entries": rows})
    fetch = FakeFetch({"v0": {"upload_date": "20261001"}})
    s = bf.build(str(tmp_path), fetch=fetch, log=lambda *_: None)[0]
    assert fetch.calls == ["v0", "v1", "v2", "v3", "v4", "v5"] and s.network_stopped
    assert len(s.need_network) == 2 and s.from_network == 1
    fetch2 = FakeFetch({f"v{i}": {"upload_date": "20261002"} for i in range(8)})
    s = bf.build(str(tmp_path), fetch=fetch2, limit=3, log=lambda *_: None)[0]
    assert s.from_cache == 1 and "v0" not in fetch2.calls, "上次查到的从缓存里拿"
    assert len(fetch2.calls) == 3 and len(s.need_network) == 4, "--limit 3：只查 3 个"


def test_manifest_not_written_by_pipeline_is_left_alone(tmp_path):
    folder = tmp_path / "Spark" / "Odd"
    folder.mkdir(parents=True)
    raw = json.dumps({"entries": {"v": {"rank": 1, "entry": _entry("v"), "ok": True}}})   # 没缩进
    (folder / ".manifest.json").write_text(raw, encoding="utf-8")
    s = bf.build(str(tmp_path), fetch=FakeFetch({"v": {"upload_date": "20261001"}}), log=lambda *_: None)
    assert s[0].skipped_manifests and "格式" in s[0].skipped_manifests[0][1] and not s[0].manifest_changes
    bf.apply(s, log=lambda *_: None)
    assert (folder / ".manifest.json").read_text(encoding="utf-8") == raw


def test_main_refuses_apply_while_server_runs(vault, monkeypatch, capsys):
    monkeypatch.setattr(bf, "server_running", lambda: True)
    assert bf.main(["--root", str(vault), "--apply", "--no-network"]) == 3
    assert "event_dates" not in _load(str(vault / "Spark" / "Conf"))
    assert bf.main(["--root", str(vault), "--no-network"]) == 0
    out = capsys.readouterr().out
    assert "需要手动填会议时间" in out and "会议总结里提到：2026年5月1日—3日" in out


def test_allow_running_server_flag_is_gone(vault, monkeypatch):
    monkeypatch.setattr(bf, "server_running", lambda: True)
    with pytest.raises(SystemExit):
        bf.main(["--root", str(vault), "--apply", "--no-network", "--allow-running-server"])
    assert "event_dates" not in _load(str(vault / "Spark" / "Conf"))


def test_no_network_writes_nothing_at_all(vault, monkeypatch, capsys):
    before = _snapshot(vault)
    assert bf.main(["--root", str(vault), "--no-network"]) == 0
    assert _snapshot(vault) == before
    assert "写了联网结果缓存" not in capsys.readouterr().out


def test_cache_dir_keeps_cache_out_of_vault_and_is_reported(vault, tmp_path, capsys):
    before = _snapshot(vault / "Spark")
    cache = tmp_path / "outside"
    assert bf.main(["--root", str(vault), "--cache-dir", str(cache), "--only", "Conf"],
                   fetch=FakeFetch({"d": {"upload_date": "20261020"}})) == 0
    assert _snapshot(vault / "Spark") == before, "缓存放到笔记库外面时笔记库一个字节不动"
    out = capsys.readouterr().out
    assert f"写了联网结果缓存（再跑不重查）：{cache / 'Conf.json'}" in out and "只写了上面列的联网结果缓存" in out
    assert json.load(open(cache / "Conf.json"))["d"] == {"upload_date": "20261020"}


def test_interrupted_apply_is_finished_on_rerun(vault, monkeypatch):
    """--apply 改完会议目录那份就停了：再跑一遍，专题文件夹那份照会议目录里的日期补齐。"""
    conf_dir = str(vault / "Spark" / "Conf")
    real = bf._apply_manifest
    calls = []

    def stop_after_first(folder, truth, *, summit_level):
        if calls:
            raise KeyboardInterrupt
        calls.append(folder)
        return real(folder, truth, summit_level=summit_level)
    monkeypatch.setattr(bf, "_apply_manifest", stop_after_first)
    with pytest.raises(KeyboardInterrupt):
        bf.apply(bf.build(str(vault), fetch=None), log=lambda *_: None)
    assert _load(conf_dir)["entries"]["b"]["entry"]["publish_date"] == "20261015"
    assert not _load(os.path.join(conf_dir, "Theme"))["entries"]["b"]["entry"].get("publish_date")
    monkeypatch.setattr(bf, "_apply_manifest", real)
    again = bf.build(str(vault), fetch=None)
    assert os.path.join(conf_dir, "Theme") in again[0].manifest_changes
    bf.apply(again, log=lambda *_: None)
    assert _load(os.path.join(conf_dir, "Theme"))["entries"]["b"]["entry"]["publish_date"] == "20261015"
    assert not any(s.changes for s in bf.build(str(vault), fetch=None))


def test_mismatched_track_manifest_is_listed(vault, capsys):
    theme = vault / "Spark" / "Conf" / "Theme" / ".manifest.json"
    theme.write_text(json.dumps(json.loads(theme.read_text())), encoding="utf-8")   # 没缩进
    summits = bf.build(str(vault), fetch=None)
    bf.report(summits)
    assert "不改处理记录：Spark/Conf/Theme/.manifest.json" in capsys.readouterr().out


def test_stale_sessions_removed_track_event_dates_stripped(tmp_path):
    folder = tmp_path / "Spark" / "Old"
    rows = {"v1": {"rank": 1, "entry": _entry("v1", publish_date="20261014"), "ok": True}}
    stale = {"start": "2026-10-14", "end": "2026-10-14", "source": "sessions"}
    _save(str(folder), {"content_type": "summit", "event_dates": stale, "entries": rows})
    _save(str(folder / "Track"), {"content_type": "summit", "event_dates": stale, "entries": rows})
    _save(str(tmp_path / "Spark" / "Man" / "Track"), {"event_dates": {"start": "2026-10-01", "source": "manual"}, "entries": {}})
    _save(str(tmp_path / "Spark" / "Man"), {"entries": {}})
    bf.apply(bf.build(str(tmp_path), fetch=None), log=lambda *_: None)
    assert "event_dates" not in _load(str(folder)) and "event_dates" not in _load(str(folder / "Track"))
    assert _load(str(tmp_path / "Spark" / "Man" / "Track"))["event_dates"]["source"] == "manual", "手动填的不动"


def test_guessed_publish_date_counts_as_undated(tmp_path):
    folder = tmp_path / "Spark" / "G"
    (folder / "transcripts").mkdir(parents=True)
    (folder / "transcripts" / "a.md").write_text(_doc("a", "- 时长：约 1:00", "- 播出时间：2026-10-10"), encoding="utf-8")
    rows = {"a": {"rank": 1, "entry": _entry("a", publish_date="20261120", publish_guessed=True), "ok": True,
                  "relative_path": "transcripts/a.md"}}
    _save(str(folder), {"content_type": "summit", "entries": rows})
    s = bf.build(str(tmp_path), fetch=None)
    assert s[0].dated == 0 and s[0].from_files == 1
    bf.apply(s, log=lambda *_: None)
    e = _load(str(folder))["entries"]["a"]["entry"]
    assert e["publish_date"] == "20261010" and "publish_guessed" not in e
