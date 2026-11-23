"""会议时间（core/summit_dates.py）：写法、手机 SummitDates.swift 一字不差、显示时的先后（手动 > 存着 > 现推）、
跨 10 天以上不当会议时间、流水线存处理记录不冲掉手动填的；阅读页改会议时间的接口；补日期工具 tools/backfill_summit_dates.py。"""

import json
import os
import shutil
import subprocess
import threading
import urllib.parse

import pytest

from core import summit_dates as sd

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "SummitDates.swift")


def _m(*days, event=None, sessions=None):
    """处理记录：每个 day 一场（publish_date），sessions 是对应的 session_date。"""
    entries = {}
    for i, d in enumerate(days):
        e = {"id": f"v{i}", "title": f"t{i}"}
        if d:
            e["publish_date"] = d
        if sessions and i < len(sessions) and sessions[i]:
            e["session_date"] = sessions[i]
        entries[f"v{i}"] = {"rank": i + 1, "entry": e, "ok": True}
    m = {"content_type": "summit", "entries": entries}
    if event is not None:
        m["event_dates"] = event
    return m


LABELS = [
    (("2026-10-13", "2026-10-13"), "2026年10月13日"),
    (("2026-10-13", ""), "2026年10月13日"),
    (("2026-10-13", "2026-10-16"), "2026年10月13日—16日"),
    (("2026-09-30", "2026-10-02"), "2026年9月30日—10月2日"),
    (("2026-12-30", "2027-01-02"), "2026年12月30日—2027年1月2日"),
    (("20261013", "20261016"), "2026年10月13日—16日"),
    (("2026-10-16", "2026-10-13"), "2026年10月16日"),
    (("2026-02-30", ""), ""),
    (("", "2026-10-13"), ""),
]


@pytest.mark.parametrize("args,want", LABELS)
def test_label(args, want):
    assert sd.label(*args) == want


def test_iso_day_and_span():
    assert sd.iso_day("20240229") == "2024-02-29" and sd.iso_day("2023-02-29") == ""
    assert sd.iso_day("1989-12-31") == "" and sd.iso_day("2101-01-01") == "" and sd.iso_day(20261013) == ""
    assert sd.span_days("2026-12-30", "2027-01-02") == 3 and sd.span_days("2024-02-28", "2024-03-01") == 2


def test_resolve_order_manual_agenda_video():
    m = _m("20261014", "20261016", event={"start": "2026-10-01", "end": "2026-10-03", "source": "manual"})
    assert sd.resolve(m) == {"start": "2026-10-01", "end": "2026-10-03", "source": "manual"}
    m = _m("20261014", "20261016", event={"start": "2026-10-13", "source": "agenda"})
    assert sd.resolve(m) == {"start": "2026-10-13", "end": "2026-10-13", "source": "agenda"}, "end 空着当成一天"
    m = _m("20261014", "", "20261016")
    assert sd.resolve(m) == {"start": "2026-10-14", "end": "2026-10-16", "source": "sessions"}
    m = _m("20261014", "20261015", event={"start": "2026-10-13", "end": "2026-10-12", "source": "manual"})
    assert sd.resolve(m)["source"] == "sessions", "存着的不合法（结束早于开始）当没存"
    m = _m("20261014", "20261015", event={"start": "2026-10-13", "source": "guess"})
    assert sd.resolve(m)["source"] == "sessions", "不认识的来源当没存"
    m = _m("20261014", "20261015", event={"start": "2026-09-01", "end": "2026-09-02", "source": "sessions"})
    assert sd.resolve(m) == {"start": "2026-10-14", "end": "2026-10-15", "source": "sessions"}, "老的 sessions 当没存，现推"
    assert sd.resolve(_m("", "")) is None and sd.resolve({}) is None and sd.resolve(None) is None
    # 议程日期现推出来是 agenda；议程日期、视频发布日期不混在一起算
    m = _m("20261101", "20261102", sessions=["2026-10-13", "2026-10-14"])
    assert sd.resolve(m) == {"start": "2026-10-13", "end": "2026-10-14", "source": "agenda"}
    assert sd.derived(m) == {"start": "2026-11-01", "end": "2026-11-02", "source": "sessions"}
    assert sd.session_day(m["entries"]["v0"]["entry"]) == "2026-10-13"
    assert sd.session_day({"publish_date": "20261101"}) == "", "视频发布那天不当这一场的日期"


def test_display_wording():
    assert sd.display({"start": "2026-10-13", "end": "2026-10-16", "source": "manual"}) == "会议时间：2026年10月13日—16日"
    assert sd.display({"start": "2026-10-13", "end": "2026-10-13", "source": "agenda"}) == "会议时间：2026年10月13日"
    assert sd.display({"start": "2026-09-14", "end": "2026-09-23", "source": "sessions"}) == "视频发布：2026年9月14日—23日"
    assert sd.display({"start": "2026-09-16", "end": "2026-09-16", "source": "sessions"}) == "视频发布：2026年9月16日"
    assert sd.display(None) == ""


def test_coverage_rule():
    assert sd.derived(_m("20260830", *[""] * 25)) is None, "26 场只有 1 场有日期：不推"
    assert sd.derived(_m("20260830", "")) is None, "2 场只有 1 场：不够 2 场"
    assert sd.derived(_m("20260830")) == {"start": "2026-08-30", "end": "2026-08-30", "source": "sessions"}, "只有 1 场的会议"
    assert sd.derived(_m("20260830", "20260831", "", "")) is not None, "正好一半"
    assert sd.derived(_m("20260830", "20260831", "", "", "")) is None, "不到一半"
    assert sd.agenda_range(_m("", "", sessions=["2026-10-13", ""])) is None, "议程日期也按同一条"
    assert sd.coverage(_m("20260830", ""), "video") == (1, 2)
    assert sd.enough(1, 1) and not sd.enough(1, 2) and sd.enough(2, 4) and not sd.enough(0, 0)


def test_publish_guessed_is_not_a_date():
    m = _m("20261014", "20261015")
    m["entries"]["v1"]["entry"]["publish_guessed"] = True
    assert sd.publish_day(m["entries"]["v1"]["entry"]) == "" and sd.derived(m) is None


def test_spread_over_ten_days_is_not_shown():
    assert sd.resolve(_m("20261001", "20261011"))["end"] == "2026-10-11", "正好 10 天还算"
    assert sd.resolve(_m("20261001", "20261012")) is None, "跨 11 天不显示"
    assert sd.publish_spread(_m("20261001", "20261012")) == ("2026-10-01", "2026-10-12")


def test_auto_stores_only_agenda_and_never_touches_manual():
    manual = {"start": "2026-10-01", "end": "2026-10-01", "source": "manual"}
    m = _m("20261014", "20261015", event=dict(manual), sessions=["2026-10-13", "2026-10-13"])
    assert sd.auto_event_dates(m) is False and m["event_dates"] == manual
    m = _m("20261020", "20261021", sessions=["2026-10-13", "2026-10-14"])
    assert sd.auto_event_dates(m) and m["event_dates"] == {"start": "2026-10-13", "end": "2026-10-14", "source": "agenda"}
    assert sd.auto_event_dates(m) is False, "再跑一遍不改"
    m = _m("20261014", event={"start": "2026-10-10", "end": "2026-10-11", "source": "agenda"})
    assert sd.auto_event_dates(m) is False, "存着议程的、这次议程日期不够：留着"
    m = _m("20261014", "20261015")
    assert sd.auto_event_dates(m) is False and "event_dates" not in m, "视频发布日期不存"
    m = _m("20261014", "20261015", event={"start": "2026-10-14", "end": "2026-10-15", "source": "sessions"})
    assert sd.auto_event_dates(m) and "event_dates" not in m, "老的 sessions 删掉"
    m = {"entries": {}, "event_dates": {"start": "2026-10-14", "source": "sessions"}}
    assert sd.strip_event_dates(m) and "event_dates" not in m
    m = {"entries": {}, "event_dates": dict(manual)}
    assert sd.strip_event_dates(m) is False and m["event_dates"] == manual


def test_lock_files_live_next_to_settings(tmp_path):
    from core import settings
    assert os.path.dirname(sd.lock_dir()) == os.path.dirname(settings.settings_path())
    assert str(tmp_path) in sd.lock_dir(), "测试里锁跟着设置文件去临时目录，不碰真的 ~/.spark"
    with sd.manifest_lock(str(tmp_path)):
        assert os.listdir(sd.lock_dir())
    assert sd.lock_key(str(tmp_path) + "/") == sd.lock_key(str(tmp_path).upper()) or not os.path.exists(str(tmp_path).upper())
    import unicodedata
    assert sd.lock_key(unicodedata.normalize("NFD", "/x/会议é")) == sd.lock_key(unicodedata.normalize("NFC", "/x/会议é"))


def test_check_manual():
    assert sd.check_manual("2026-10-13", "") == ("2026-10-13", "2026-10-13")
    for start, end in (("20261013", ""), ("2026-10-13", "2026-10-12"), ("2026-02-30", ""), ("2026-10-13", "2027-10-13"),
                       ("<b>", "")):
        with pytest.raises(ValueError):
            sd.check_manual(start, end)


# ---------------------------------------------------------------- 手机一字不差

@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_parity(tmp_path):
    manifests = [
        _m("20261014", "20261016"),
        _m("20261014", event={"start": "2026-10-01", "end": "2026-10-03", "source": "manual"}),
        _m("20261014", event={"start": "2026-10-13", "source": "agenda"}),
        _m("20261014", event={"start": "2026-10-13", "end": "2026-10-12", "source": "manual"}),
        _m("20261014", event={"start": "2026-10-13", "source": "guess"}),
        _m("20261001", "20261012"),
        _m("20261001", "20261011"),
        _m("20261101", "20261102", sessions=["2026-10-13", "2026-10-14"]),
        _m("", ""),
        _m("20261230", "20270102"),
        _m("2026-09-30", "20261002"),
        {"entries": {"a": {"entry": {"publish_date": 20261014}}}},
        {"entries": {}, "event_dates": "2026-10-13"},
        _m("20261014", "20261015", event={"start": "2026-09-01", "source": "sessions"}),
        _m("20260830", *[""] * 25),
        _m("20260830", ""),
        _m("20260830"),
        _m("20260830", "20260831", "", ""),
        _m("20260830", "20260831", "", "", ""),
        _m("", "", sessions=["2026-10-13", ""]),
        _m("", "", "", sessions=["2026-10-13", "2026-10-15"]),
        _m("20261101", "20261102", "", sessions=["2026-10-13", "", ""]),
    ]
    for flag in (True, False, "x", "", 0, 1, None):
        g = _m("20261014", "20261015")
        g["entries"]["v1"]["entry"]["publish_guessed"] = flag
        manifests.append(g)
    cases = {"labels": [list(a) for a, _ in LABELS], "manifests": manifests,
             "entries": [{"session_date": "2026-10-13", "publish_date": "20261101"}, {"publish_date": "20261101"},
                         {"session_date": "bad", "publish_date": "20261101"}, {},
                         {"publish_date": "20261101", "publish_guessed": True}]}
    main = tmp_path / "main.swift"
    main.write_text('''import Foundation
let data = FileHandle.standardInput.readDataToEndOfFile()
let c = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
func r(_ x: SummitDates.Range?) -> Any { x.map { ["start": $0.start, "end": $0.end, "source": $0.source, "label": $0.label, "text": SummitDates.display($0)] as Any } ?? NSNull() }
let labels = (c["labels"] as! [[String]]).map { SummitDates.label($0[0], $0[1]) }
let resolved = (c["manifests"] as! [[String: Any]]).map { r(SummitDates.resolve($0)) }
let days = (c["entries"] as! [[String: Any]]).map { [SummitDates.sessionDay($0), SummitDates.publishDay($0)] }
let out = try! JSONSerialization.data(withJSONObject: ["labels": labels, "resolved": resolved, "days": days])
print(String(data: out, encoding: .utf8)!)
''', encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], input=json.dumps(cases), capture_output=True, text=True, timeout=60).stdout)

    def py(x):
        return dict(x, label=sd.label(x["start"], x["end"]), text=sd.display(x)) if x else None
    assert got["labels"] == [sd.label(*a) for a in cases["labels"]]
    assert got["resolved"] == [py(sd.resolve(m)) for m in manifests]
    assert got["days"] == [[sd.session_day(e), sd.publish_day(e)] for e in cases["entries"]]


# ---------------------------------------------------------------- 存处理记录：手动填的不被冲掉

def _write(folder, data):
    with open(sd.manifest_path(folder), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load(folder):
    with open(sd.manifest_path(folder), encoding="utf-8") as f:
        return json.load(f)


def test_pipeline_save_keeps_manual_from_disk(tmp_path):
    folder = str(tmp_path)
    _write(folder, _m("20261014"))
    stale = _load(folder)                         # 流水线任务开始时读的那份
    sd.set_manual(folder, "2026-10-13", "2026-10-16")
    stale["overall_summary"] = "新总结"
    sd.save_manifest(folder, stale)
    disk = _load(folder)
    assert disk["event_dates"] == {"start": "2026-10-13", "end": "2026-10-16", "source": "manual"}
    assert disk["overall_summary"] == "新总结" and stale["event_dates"]["source"] == "manual", "内存里那份也跟着改"
    # 在阅读页上清掉以后，流水线手里还拿着 manual 的旧副本：也不能写回去
    sd.set_manual(folder, "")
    sd.save_manifest(folder, stale)
    assert "event_dates" not in _load(folder)
    # 流水线自己算的（sessions）照常存
    stale["event_dates"] = {"start": "2026-10-14", "end": "2026-10-14", "source": "sessions"}
    sd.save_manifest(folder, stale)
    assert _load(folder)["event_dates"]["source"] == "sessions"
    # 清掉手动填的时候，自动算的不动
    sd.set_manual(folder, "")
    assert _load(folder)["event_dates"]["source"] == "sessions"


def test_concurrent_pipeline_saves_and_manual_edit(tmp_path):
    """流水线线程拿着旧副本一直存，同时阅读页改会议时间：两边的改动都在。"""
    folder = str(tmp_path)
    _write(folder, _m("20261014"))
    stale = _load(folder)
    stop = threading.Event()
    errors = []

    def pipeline():
        n = 0
        while not stop.is_set():
            n += 1
            stale["entries"]["v0"]["entry"]["title"] = f"t{n}"
            try:
                sd.save_manifest(folder, stale)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    t = threading.Thread(target=pipeline)
    t.start()
    try:
        for i in range(20):
            sd.set_manual(folder, f"2026-10-{i + 1:02d}", "")
    finally:
        stop.set()
        t.join()
    sd.save_manifest(folder, stale)
    disk = _load(folder)
    assert not errors
    assert disk["event_dates"] == {"start": "2026-10-20", "end": "2026-10-20", "source": "manual"}
    assert disk["entries"]["v0"]["entry"]["title"] == stale["entries"]["v0"]["entry"]["title"]


def test_lock_is_reentrant_in_one_thread(tmp_path):
    with sd.manifest_lock(str(tmp_path)):
        with sd.manifest_lock(str(tmp_path)):
            pass


# ---------------------------------------------------------------- 阅读页：显示、改会议时间

@pytest.fixture
def summit_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    show = tmp_path / "Spark" / "OCP"
    (show / "transcripts").mkdir(parents=True)
    (show / "transcripts" / "001_Talk.md").write_text("# Talk\n\n- 时长：约 1:00\n\n正文\n", encoding="utf-8")
    m = _m("20261014", "20261016")
    m["entries"]["v0"]["relative_path"] = "transcripts/001_Talk.md"
    _write(str(show), m)
    series = tmp_path / "Spark" / "Pod"
    series.mkdir()
    _write(str(series), {"content_type": "series", "entries": {}})
    return tmp_path


def _client():
    import spark
    from werkzeug.test import Client
    return Client(spark.application)


def _post(c, data, **headers):
    return c.post("/read/api/event_dates", data=json.dumps(data), headers={"Content-Type": "application/json", **headers})


def test_reader_shows_and_edits_event_dates(summit_vault):
    c = _client()
    page = c.get(urllib.parse.quote("/read/f/OCP/")).get_data(as_text=True)
    assert "视频发布：2026年10月14日—16日" in page and "会议时间：还没填" in page and "填会议时间" in page
    assert "event-dates.js" in page and "恢复自动" not in page and 'value="2026-10-14"' not in page, "视频日期不预填成会议时间"
    index = c.get("/read/").get_data(as_text=True)
    assert "视频发布：2026年10月14日—16日" in index and "会议时间：2026年10月14日" not in index
    lib = json.loads(c.get("/read/api/library").get_data(as_text=True))
    ocp = next(s for s in lib["shows"] if s["name"] == "OCP")
    assert ocp["eventDates"]["text"] == "视频发布：2026年10月14日—16日" and ocp["eventDates"]["source"] == "sessions"

    r = _post(c, {"folder": "OCP", "start": "2026-10-13", "end": "2026-10-16"})
    assert r.status_code == 200 and r.get_json()["eventDates"]["source"] == "manual"
    disk = _load(str(summit_vault / "Spark" / "OCP"))
    assert disk["event_dates"] == {"start": "2026-10-13", "end": "2026-10-16", "source": "manual"}
    assert disk["entries"]["v0"]["relative_path"] == "transcripts/001_Talk.md", "别的键一个不动"
    page = c.get(urllib.parse.quote("/read/f/OCP/")).get_data(as_text=True)
    assert "会议时间：2026年10月13日—16日" in page and "恢复自动" in page and "改会议时间" in page
    assert "会议时间：2026年10月13日—16日" in c.get("/read/").get_data(as_text=True)

    r = _post(c, {"folder": "OCP", "start": "", "end": ""})
    assert r.status_code == 200 and r.get_json()["eventDates"]["source"] == "sessions"
    assert "event_dates" not in _load(str(summit_vault / "Spark" / "OCP"))


@pytest.mark.parametrize("data", [
    {"folder": "OCP", "start": "2026-13-01"},
    {"folder": "OCP", "start": "2026-10-16", "end": "2026-10-13"},
    {"folder": "OCP", "start": ["2026-10-13"]},
    {"folder": "Pod", "start": "2026-10-13"},
    {"folder": "../OCP", "start": "2026-10-13"},
    {"folder": "OCP/transcripts", "start": "2026-10-13"},
    {"folder": "剪藏", "start": "2026-10-13"},
    {"folder": "Nope", "start": "2026-10-13"},
])
def test_reader_event_dates_validation(summit_vault, data):
    before = (summit_vault / "Spark" / "OCP" / ".manifest.json").read_bytes()
    assert _post(_client(), data).status_code == 400
    assert (summit_vault / "Spark" / "OCP" / ".manifest.json").read_bytes() == before


def test_reader_event_dates_cross_site_blocked(summit_vault):
    r = _post(_client(), {"folder": "OCP", "start": "2026-10-13"}, Origin="https://evil.example")
    assert r.status_code == 403
    assert "event_dates" not in _load(str(summit_vault / "Spark" / "OCP"))


def test_reader_old_manifest_without_dates_shows_nothing(summit_vault):
    show = summit_vault / "Spark" / "OCP"
    m = _load(str(show))
    for row in m["entries"].values():
        row["entry"].pop("publish_date", None)
    _write(str(show), m)
    page = _client().get(urllib.parse.quote("/read/f/OCP/")).get_data(as_text=True)
    assert "会议时间：还没填" in page and "填会议时间" in page
    index = _client().get("/read/").get_data(as_text=True)
    assert "会议时间：" not in index and "视频发布：" not in index, "列表里什么都不显示"
