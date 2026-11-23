"""文档头的发布时间、收录时间（core/doctime.py）：写法、时区、新旧写法都认、排先后；
手机 Shared/DocTime.swift 编出来跑一遍，跟电脑一字不差；剪藏按发表那天起名、按时刻排。"""

import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from core import clip_compose as cc
from core import clips, doctime

ROOT = os.path.join(os.path.dirname(__file__), "..")
SWIFT_DOCTIME = os.path.join(ROOT, "apps", "mobile-ios", "Shared", "DocTime.swift")

# 2026-11-19 13:12:00 UTC
INSTANT = datetime(2026, 11, 19, 13, 12, 0, tzinfo=timezone.utc)
ZONES = {"Asia/Shanghai": "2026年11月19日，21:12（UTC+8）", "Etc/GMT+7": "2026年11月19日，06:12（UTC-7）",
         "Asia/Kolkata": "2026年11月19日，18:42（UTC+5:30）", "UTC": "2026年11月19日，13:12（UTC+0）"}


@pytest.fixture
def zone():
    old = os.environ.get("TZ")

    def use(name):
        os.environ["TZ"] = name
        time.tzset()
    yield use
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("name", list(ZONES))
def test_cn_converts_into_this_machine_zone(zone, name):
    zone(name)
    assert doctime.cn(INSTANT) == ZONES[name]
    assert doctime.lines(INSTANT, INSTANT) == [f"- 发布时间：{ZONES[name]}", f"- 收录时间：{ZONES[name]}"]


def test_iso_day_and_date_only(zone):
    zone("Asia/Shanghai")
    assert doctime.iso(INSTANT) == "2026-11-19T21:12:00+08:00"
    late = datetime(2026, 11, 19, 20, 30, tzinfo=timezone.utc)      # 北京时间已经是第二天
    assert doctime.day(late) == date(2026, 11, 20), "日期取本机时区的那天，不是 UTC 的"
    zone("UTC")
    assert doctime.iso(INSTANT) == "2026-11-19T13:12:00+00:00", "零时差也写 +00:00，不写 Z（跟手机一样）"
    assert doctime.cn(date(2026, 11, 9)) == "2026年11月09日", "只知道哪天：不带时刻、不带时区"
    assert doctime.lines(None, INSTANT) == ["- 收录时间：2026年11月19日，13:12（UTC+0）"], "不知道发布时间就不写这一行"
    assert doctime.lines(date(2026, 11, 9), None) == ["- 发布时间：2026年11月09日"]
    assert doctime.cn(None) == ""


def test_parse_old_and_new_forms():
    assert doctime.parse("2026年11月19日，21:12（UTC+8）") == datetime(2026, 11, 19, 21, 12, tzinfo=timezone(timedelta(hours=8)))
    assert doctime.parse("2026年11月19日，08:05（UTC+5:30）").utcoffset() == timedelta(hours=5, minutes=30)
    assert doctime.parse("2026年11月19日，08:05（UTC-7）").utcoffset() == timedelta(hours=-7)
    assert doctime.parse("2026年11月09日") == date(2026, 11, 9)
    assert doctime.parse("2026-11-09") == doctime.parse("20261109") == date(2026, 11, 9)
    assert doctime.parse("2026-11-19T13:12:00Z") == INSTANT
    assert doctime.parse('"2026-11-19T21:12:00+08:00"') == INSTANT
    assert doctime.parse("2026-11-19T21:12:00") == date(2026, 11, 19), "不带时差的不猜时区，只取日期"
    assert doctime.parse("2026-02-30") is None and doctime.parse("") is None and doctime.parse("昨天") is None
    for old, new in (("- 发布：2026-11-01", "2026-11-01"), ("- 播出时间：2026-09-15", "2026-09-15"),
                     ("- 发布日期：2026-09-15", "2026-09-15"), ("- 发布时间：2026年11月19日，21:12（UTC+8）", None),
                     ("- 发表时间：2026年11月19日，21:12（UTC+8）", "2026年11月19日，21:12（UTC+8）")):   # 上一版的标签照认
        m = doctime.PUBLISHED_LINE.match(old)
        assert m and (new is None or m.group(1) == new)
    assert doctime.CAPTURED_LINE.match("- 剪藏：2026-11-09").group(1) == "2026-11-09"
    assert doctime.CAPTURED_LINE.match("- 收录时间：2026年11月20日，08:05（UTC+8）")


def test_entry_stamp_and_published(zone):
    zone("Asia/Shanghai")
    e = doctime.stamp_entry({}, datetime(2026, 11, 19, 20, 30, tzinfo=timezone.utc))
    assert e == {"publish_date": "20261120", "publish_at": "2026-11-20T04:30:00+08:00"}
    assert doctime.stamp_entry({"publish_at": "x"}, date(2026, 11, 1)) == {"publish_date": "20261101"}
    assert doctime.entry_published({"publish_date": "20261101"}) == date(2026, 11, 1)
    # 日期被别处改过、跟时刻不是同一天：信日期
    assert doctime.entry_published({"publish_date": "20261105", "publish_at": "2026-11-20T04:30:00+08:00"}) == date(2026, 11, 5)
    assert doctime.parse_local("2026-11-20T08:05:31").tzinfo is not None


# ---------------------------------------------------------------- 剪藏：写法、起名、排先后

ARTICLE = {"url": "https://example.com/a", "title": "T", "source": "S", "published": "2026-11-19",
           "publishedAt": "2026-11-19T13:12:00Z", "markdown": "正文"}


def test_clip_render_with_times(zone):
    zone("Asia/Shanghai")
    captured = datetime(2026, 11, 20, 0, 5, 31, tzinfo=timezone.utc)
    text = cc.render(ARTICLE, "", None, captured)
    assert text.startswith('---\ntitle: "T"\nsource: "S"\nurl: "https://example.com/a"\npublished: 2026-11-19\n'
                           'published_at: "2026-11-19T21:12:00+08:00"\nclipped: 2026-11-20\n'
                           'clipped_at: "2026-11-20T08:05:31+08:00"\n---\n\n# T\n\n- 来源：S\n'
                           '- 发布时间：2026年11月19日，21:12（UTC+8）\n- 收录时间：2026年11月20日，08:05（UTC+8）\n\n## 正文\n')
    day_only = cc.render(dict(ARTICLE, publishedAt=""), "", None, captured)
    assert "published: 2026-11-19\nclipped: 2026-11-20\n" in day_only and "- 发布时间：2026年11月19日\n" in day_only
    none = cc.render(dict(ARTICLE, published="", publishedAt=""), "", None, captured)
    assert "published" not in none and "发布时间" not in none and "- 收录时间：2026年11月20日，08:05（UTC+8）" in none
    # 文件名：发表那天（本机时区），不知道发布时间用收录那天
    assert cc.file_name("T", cc.name_day(ARTICLE, captured)) == "20261119_T.md"
    assert cc.file_name("T", cc.name_day(dict(ARTICLE, published="", publishedAt=""), captured)) == "20261120_T.md"
    late = dict(ARTICLE, publishedAt="2026-11-19T20:30:00Z")     # 北京时间 11 月 20 日
    assert cc.name_day(late, captured) == date(2026, 11, 20)
    assert "published: 2026-11-20\n" in cc.render(late, "", None, captured), "日期跟着本机时区的那天"


def _clip(name, published="", published_at="", clipped="2026-11-20", clipped_at=""):
    return {"name": name, "published": published, "published_at": published_at, "clipped": clipped, "clipped_at": clipped_at}


SAME_DAY = [
    _clip("a.md", "2026-11-19", "2026-11-19T09:00:00+08:00", clipped_at="2026-11-20T08:00:00+08:00"),
    _clip("b.md", "2026-11-19", "2026-11-19T21:12:00+08:00", clipped_at="2026-11-20T07:00:00+08:00"),
    _clip("c.md", "2026-11-19", "2026-11-19T15:00:00+08:00", clipped_at="2026-11-20T09:00:00+08:00"),
    _clip("d.md", "2026-11-19", clipped_at="2026-11-20T10:00:00+08:00"),
    _clip("e.md", "2026-11-19", clipped="2026-11-21"),
]


def test_clip_sort_same_day_times_first(zone):
    zone("Asia/Shanghai")
    order = [c["name"] for c in sorted(SAME_DAY, key=clips.order_published, reverse=True)]
    # 有时刻的按时刻（新的在前），只有日期的排在后面，两篇只有日期的按收录时间（e 收录得晚）
    assert order == ["b.md", "c.md", "a.md", "e.md", "d.md"]
    by_clip = [c["name"] for c in sorted(SAME_DAY, key=clips.order_clipped, reverse=True)]
    assert by_clip == ["e.md", "d.md", "c.md", "a.md", "b.md"]
    nopub = _clip("f.md", clipped="2026-11-19", clipped_at="2026-11-19T23:00:00+08:00")
    assert [c["name"] for c in sorted(SAME_DAY + [nopub], key=clips.order_published, reverse=True)][:2] == ["f.md", "b.md"], \
        "不知道发布时间的按收录时刻算"


def test_load_clips_reads_times_and_keeps_capture_day(tmp_path, zone):
    zone("Asia/Shanghai")
    folder = tmp_path / "Spark" / "剪藏"
    folder.mkdir(parents=True)
    captured = datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)
    (folder / cc.file_name("T", cc.name_day(ARTICLE, captured))).write_text(cc.render(ARTICLE, "", None, captured))
    (folder / "20261101_老.md").write_text("# 老\n")
    got = {c["name"]: c for c in clips.load_clips(str(tmp_path))}
    assert got["20261119_T.md"]["clipped"] == "2026-11-20", "收录日期读文件头，不拿文件名前面的发表日期"
    assert got["20261119_T.md"]["published_at"] == "2026-11-19T21:12:00+08:00"
    assert got["20261101_老.md"]["clipped"] == "2026-11-01", "没有文件头的老文件还看文件名"


# ---------------------------------------------------------------- 手机：DocTime.swift 编出来跑，跟电脑对照

HARNESS = r'''
import Foundation
let instant = Date(timeIntervalSince1970: %d)
var out: [String: Any] = [:]
for name in ["Asia/Shanghai", "Etc/GMT+7", "Asia/Kolkata", "UTC"] {
    let tz = TimeZone(identifier: name)!
    out[name] = ["cn": DocTime.cn(.at(instant), tz: tz), "iso": DocTime.iso(instant, tz: tz), "day": DocTime.day(.at(instant), tz: tz)]
}
out["lines"] = DocTime.lines(published: .day("2026-11-09"), captured: .at(instant))
out["parse"] = ["2026年11月19日，21:12（UTC+8）", "2026年11月09日", "20261109", "2026-11-19T13:12:00Z", "2026-11-19T21:12:00",
                "2026-02-30", "\"2026-11-19T21:12:00+08:00\""].map { s -> String in
    switch DocTime.parse(s) { case .at(let d)?: return "at:\(Int(d.timeIntervalSince1970))"; case .day(let x)?: return "day:\(x)"; case nil: return "nil" }
}
let clips: [[String]] = %s
out["keys"] = clips.map { DocTime.sortKey(at: $0[1], day: $0[0]) }
out["published"] = DocTime.publishedLine
out["captured"] = DocTime.capturedLine
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_doctime_matches_python(tmp_path, zone):
    zone("Asia/Shanghai")
    pairs = [[c["published"], c["published_at"]] for c in SAME_DAY] + [["", ""]]
    main = tmp_path / "main.swift"
    main.write_text(HARNESS % (int(INSTANT.timestamp()), json.dumps(pairs, ensure_ascii=False)))
    exe = tmp_path / "harness"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_DOCTIME, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "TZ": "Asia/Shanghai"}).stdout)
    for name, want in ZONES.items():
        zone(name)
        assert got[name]["cn"] == want == doctime.cn(INSTANT)
        assert got[name]["iso"] == doctime.iso(INSTANT)
        assert got[name]["day"] == doctime.day(INSTANT).isoformat()
    zone("Asia/Shanghai")
    assert got["lines"] == doctime.lines(date(2026, 11, 9), INSTANT)
    at = f"at:{int(INSTANT.timestamp())}"
    assert got["parse"] == [at, "day:2026-11-09", "day:2026-11-09", at, "day:2026-11-19", "nil", at]
    # 排先后：手机的键排出来的先后跟电脑一样
    py = sorted(range(len(pairs)), key=lambda i: doctime.sort_key(pairs[i][1], pairs[i][0]), reverse=True)
    sw = sorted(range(len(pairs)), key=lambda i: got["keys"][i], reverse=True)
    assert py == sw
    assert got["published"] == "(?m)" + doctime.PUBLISHED_LINE.pattern
    assert got["captured"] == "(?m)" + doctime.CAPTURED_LINE.pattern


def test_phone_clip_render_uses_same_layout():
    """手机 Clips.render 的文件头顺序、两行时间跟电脑一样（整篇的写法见上面 test_clip_render_with_times）。"""
    with open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("static func render(_ a: Clipper.Article"):src.index("// MARK: 摘要")]
    order = [body.index(k) for k in ('"published: ', '"published_at: ', '"clipped: ', '"clipped_at: ', '"tags: [', '"tldr: ',
                                     '"note: ', '"- 来源：', "DocTime.lines(published: pub, captured: .at(clipped))", '"- 备注：')]
    assert order == sorted(order)
    assert "发布：" not in body and "剪藏：" not in body


# ---------------------------------------------------------------- 评审：认的写法、排先后、坏时间（电脑和手机一样）

# 评审时的用例（review-b2-swift/cases.json）加上 T2 列的几种
PARSE_CASES = [
    "2026-11-19T21:12:00+08:00", "2026-11-19T21:12:00Z", "2026-11-19T21:12:00z", "2026-11-19T21:12:00.123Z",
    "2026-11-19T21:12:00.123456+08:00", "2026-11-19T21:12:00.1234567+08:00", "2026-11-19T21:12+08:00",
    "2026-11-19 21:12:00+08:00", "2026-11-19T21:12:00+0800", "2026-11-19T21:12:00+08", "2026-11-19T21:12+08",
    "2026-11-19T21:12:00-03:30", "2026-11-19T21:12:00+05:45", "2026-11-19T21:12:00+14:00", "2026-11-19T21:12:00+00:00",
    "2026-11-19T21:12:00", "2026-11-19T21:12", "2026-11-19", "20261119", "2026-02-30", "20260230", "2026年11月19日",
    "2026年11月19日，21:12（UTC+8）", "2026年11月19日，21:12（UTC-7）", "2026年11月19日，21:12（UTC+5:30）", "2026年1月9日",
    "2026年11月19日，24:00（UTC+8）", "2026年11月19日，21:12（UTC+25）", "1999-12-31T23:59:59-05:00", "1969-12-31T23:59:30Z",
    "0099-01-01", "\"2026-11-19T21:12:00+08:00\"", "2026-11-19T21:12:60+08:00", "2026-11-19T25:12:00+08:00",
    "2026-03-08T02:30:00-08:00", "2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00", "2026-11-19T21:12:00 +08:00",
    "2026-11-19T21:12:00.5", "2026-11-19T21:12:00+08:00[Asia/Shanghai]", "2026-11-19T21:12+08:00[Asia/Shanghai]",
    "2026-11-19T21:12:00GMT", "2026-11-19T21:12:00 GMT", " 2026-11-19 ", "2026-11-19T21:12:00.000+0000", "１２３",
    "9999-12-31T00:00:00Z", "9999-12-31", "2030-01-01T00:00:00+08:00", "1989-12-31", "",
]

RSS_CASES = [
    "Thu, 19 Nov 2026 13:12:00 GMT", "Thu, 19 Nov 2026 21:12:00 +0800", "Thu, 19 Nov 2026 21:12:00",
    "Tue, 19 Nov 2026 21:12:00", "Thu, 19 Nov 2026 00:00:00 GMT", "Thu, 19 Nov 2026 00:00:00 +0000",
    "Thu, 19 Nov 2026 21:12:00 PST", "2026-11-19T21:12:00+08:00", "2026-11-19T21:12:00Z", "2026-11-19", "2026-11-19T21:12:00",
]

# 三篇同一天发表、时刻在不同时区写的：在哪个时区看先后都一样（T1）
ZONE_CLIPS = [
    ["2026-11-19", "2026-11-19T09:00:00-08:00"],   # 17:00Z，北京时间已经是 11 月 20 日
    ["2026-11-19", "2026-11-19T23:30:00+08:00"],   # 15:30Z
    ["2026-11-19", "2026-11-19T12:00:00+00:00"],   # 12:00Z
    ["2026-11-19", ""], ["2026-11-20", ""], ["2026-11-18", "2026-11-18T23:59:00+14:00"], ["", ""],
    ["9999-12-31", "9999-12-31T00:00:00Z"],
]

HARNESS2 = r'''
import Foundation
struct SparkError: Error { init(_ s: String) {} }
func show(_ w: (DocTime.When, Int?)?) -> String {
    switch w { case (.at(let d), let off)?: return "at:\(Int(d.timeIntervalSince1970)):\(off ?? -9999)"
               case (.day(let x), _)?: return "day:\(x)"; case nil: return "nil" }
}
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]
out["parse"] = (input["parse"] as! [String]).map { show(DocTime.parseFull($0)) }
out["rss"] = (input["rss"] as! [String]).map { s -> String in
    switch Feed.when(s) { case .at(let d)?: return "at:\(Int(d.timeIntervalSince1970))"; case .day(let x)?: return "day:\(x)"; case nil: return "nil" }
}
out["keys"] = (input["clips"] as! [[String]]).map { DocTime.sortKey(at: $0[1], day: $0[0]) }
out["epoch"] = (input["epoch"] as! [Double]).map { DocTime.fromEpoch($0).map { Int($0.timeIntervalSince1970) } ?? -1 }
out["ct"] = (input["ct"] as! [String]).map { html -> String in
    let p = input["ctPatterns"] as! [String]
    for pat in p {
        if let re = try? NSRegularExpression(pattern: pat, options: [.caseInsensitive]),
           let m = re.firstMatch(in: html, range: NSRange(location: 0, length: (html as NSString).length)), m.numberOfRanges > 1 {
            return (html as NSString).substring(with: m.range(at: 1))
        }
    }
    return ""
}
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


def _py_show(w):
    if w is None:
        return "nil"
    if isinstance(w, datetime):
        return f"at:{int(w.timestamp())}:{int(w.utcoffset().total_seconds() // 60)}"
    return f"day:{w.isoformat()}"


def _py_rss(raw):
    import feedparser
    from core import sources
    parsed = feedparser.datetimes._parse_date(raw)
    w = sources._rss_published({"published": raw, "published_parsed": parsed})
    if w is None:
        return "nil"
    return f"at:{int(w.timestamp())}" if isinstance(w, datetime) else f"day:{w.isoformat()}"


def _ct_patterns():
    import re as _re
    with open(os.path.join(ROOT, "apps", "mobile-ios", "Shared", "Clipper.swift"), encoding="utf-8") as f:
        src = f.read()
    line = next(ln for ln in src.splitlines() if "create_time" in ln and "first(" in ln)
    return _re.findall(r'#"(.*?)"#', line)


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
@pytest.mark.parametrize("tzname", ["GMT", "Asia/Shanghai", "America/Los_Angeles"])
def test_swift_parse_sort_epoch_match_python(tmp_path, zone, tzname):
    zone(tzname)
    main = tmp_path / "main.swift"
    main.write_text(HARNESS2)
    exe = tmp_path / "harness"
    feed = os.path.join(ROOT, "apps", "mobile-ios", "Shared", "FeedParser.swift")
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_DOCTIME, feed, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    epochs = [1795093920.0, 1795093920123.0, 25381104000.0, 179509392012.0, 631151999.0, 9e15, float(int(time.time()) + 3 * 86400)]
    cts = ['"create_time": 1795093920123,', '"create_time":"1795093920"', '"create_time": 17950939201234,', 'var ct = "1795093920123";']
    payload = {"parse": PARSE_CASES, "rss": RSS_CASES, "clips": ZONE_CLIPS, "epoch": epochs, "ct": cts, "ctPatterns": _ct_patterns()}
    got = json.loads(subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "TZ": tzname}).stdout)
    assert got["parse"] == [_py_show(doctime.parse(s)) for s in PARSE_CASES]
    assert got["rss"] == [_py_rss(s) for s in RSS_CASES]
    py_order = sorted(range(len(ZONE_CLIPS)), key=lambda i: doctime.sort_key(ZONE_CLIPS[i][1], ZONE_CLIPS[i][0]), reverse=True)
    sw_order = sorted(range(len(ZONE_CLIPS)), key=lambda i: got["keys"][i], reverse=True)
    assert py_order == sw_order == [4, 0, 1, 2, 3, 5, 6, 7], "同一天按绝对时刻，跟在哪个时区看无关"
    py_epoch = [(lambda d: int(d.timestamp()) if d else -1)(doctime.from_epoch(v)) for v in epochs]
    assert got["epoch"] == py_epoch == [1795093920, 1795093920, -1, -1, -1, -1, -1]
    assert got["ct"] == ["1795093920123", "1795093920", "", "1795093920123"], "毫秒不会截成 11 位（2538 年）"


def test_t2_grammar_examples():
    sh = timezone(timedelta(hours=8))
    assert doctime.parse("2026-11-19T21:12+08:00") == datetime(2026, 11, 19, 21, 12, tzinfo=sh), "没有秒也是时刻"
    assert doctime.parse("2026-11-19T21:12:00+08") == datetime(2026, 11, 19, 21, 12, tzinfo=sh)
    assert doctime.parse("2026-11-19T21:12:00+08:00[Asia/Shanghai]") == datetime(2026, 11, 19, 21, 12, tzinfo=sh)
    assert doctime.parse("2026-11-19T21:12:00GMT") == date(2026, 11, 19), "时差写法不认识：只取日期"


def test_t3_bad_instants_rejected():
    assert int(doctime.from_epoch(1795093920123).timestamp()) == int(doctime.from_epoch(1795093920).timestamp()), "13 位是毫秒"
    assert doctime.from_epoch(25381104000) is None and doctime.from_epoch(1e30) is None
    assert doctime.parse("9999-12-31T00:00:00Z") is None and doctime.parse("1989-12-31") is None
    assert doctime.sort_key("9999-12-31T00:00:00Z", "9999-12-31") == ("", 0, 0)
    assert doctime.cn(None) == ""


def test_t1_sort_key_independent_of_viewer_zone(zone):
    orders = set()
    for name in ("GMT", "Asia/Shanghai", "America/Los_Angeles"):
        zone(name)
        orders.add(tuple(sorted(range(len(ZONE_CLIPS)), key=lambda i: doctime.sort_key(ZONE_CLIPS[i][1], ZONE_CLIPS[i][0]),
                                reverse=True)))
    assert len(orders) == 1


def test_t4_rss_without_zone_or_zero_midnight_is_date_only(zone):
    zone("Asia/Shanghai")
    assert _py_rss("Tue, 19 Nov 2026 21:12:00") == "day:2026-11-19"
    assert _py_rss("Thu, 19 Nov 2026 00:00:00 GMT") == "day:2026-11-19"
    assert _py_rss("Thu, 19 Nov 2026 00:00:00 +0000") == "day:2026-11-19"
    assert _py_rss("Thu, 19 Nov 2026 13:12:00 GMT") == f"at:{int(INSTANT.timestamp())}"


# ---------------------------------------------------------------- 手机 Clips.render / fileName 跟电脑 clip_compose 一字不差

def _swift_chunk(src: str, start: str) -> str:
    """从 start 开始取一个 Swift 声明（按花括号配对）。"""
    i = src.index(start)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise ValueError(start)


RENDER_HARNESS = r'''
import Foundation
enum Clipper { struct Article { var url: String; var title: String; var source: String; var published: String; var markdown: String
    var images: [String: String] = [:]; var titleGuessed: Bool? = nil; var imagesOnly: Bool? = nil; var publishedAt: String? = nil } }
enum Clips {
%s
}
let cases = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [[String: Any]]
var out: [[String: String]] = []
for c in cases {
    let a = Clipper.Article(url: c["url"] as! String, title: c["title"] as! String, source: c["source"] as! String,
                            published: c["published"] as! String, markdown: c["markdown"] as! String, publishedAt: c["publishedAt"] as? String)
    let s = (c["summary"] as? [String: Any]).map { Clips.Summary(tldr: $0["tldr"] as! String, tags: $0["tags"] as! [String], body: $0["body"] as! String) }
    let clipped = Date(timeIntervalSince1970: c["clipped"] as! Double)
    out.append(["file": Clips.fileName(title: a.title, day: Clips.nameDay(a, clipped: clipped)),
                "text": Clips.render(a, note: c["note"] as! String, summary: s, clipped: clipped)])
}
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_clip_render_matches_python(tmp_path, zone):
    zone("Asia/Shanghai")
    with open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8") as f:
        src = f.read()
    parts = [_swift_chunk(src, s) for s in ("static func yaml(", "static func fileName(", "static func published(_ a: Clipper.Article)",
                                            "static func nameDay(", "struct Summary", "static func render(_ a: Clipper.Article")]
    main = tmp_path / "main.swift"
    main.write_text(RENDER_HARNESS % "\n".join(parts))
    exe = tmp_path / "render"
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), SWIFT_DOCTIME, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    base = dict(title="标题", source="来源", url="https://e.com/a", published="", publishedAt=None, markdown="正文", note="",
                summary=None, clipped=1795000000.0)
    variants = [{}, {"published": "2026-11-19"}, {"published": "2026-11-19", "publishedAt": "2026-11-19T21:12:00-03:30"},
                {"publishedAt": "2026-11-19T21:12:00+05:45"}, {"publishedAt": "2026-11-19T21:12:00"}, {"published": "1999-01-01"},
                {"title": "a/b:c*d?e\"f<g>h|i#j^k[l]m\tn"}, {"title": "　全角空格标题　"}, {"title": " nbsp "},
                {"title": "中" * 70}, {"title": "é" * 70}, {"title": ""}, {"title": "   "}, {"url": ""}, {"source": ""},
                {"note": "多行\n备注\"引号\"：冒号"}, {"summary": {"tldr": "一句\"话\"", "tags": ["A:B", "引\"号"], "body": "- x"}},
                {"markdown": "　正文　"}, {"markdown": "\n\n正文\n\n"}, {"clipped": 1795000000.9}]
    cases = [dict(base, **v) for v in variants]
    got = json.loads(subprocess.run([str(exe)], input=json.dumps(cases), capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "TZ": "Asia/Shanghai"}).stdout)
    for c, g in zip(cases, got):
        a = {k: c[k] for k in ("url", "title", "source", "published", "markdown")}
        if c["publishedAt"] is not None:
            a["publishedAt"] = c["publishedAt"]
        clipped = datetime.fromtimestamp(c["clipped"], timezone.utc)
        assert g["file"] == cc.file_name(a["title"], cc.name_day(a, clipped)), c
        assert g["text"] == cc.render(a, c["note"], c["summary"], clipped), c


# ---------------------------------------------------------------- 手机：还没确认电脑收下的修改（Spark/PendingEdits.swift）

PENDING_HARNESS = r"""
import Foundation
typealias R = PendingEdits.Record
let h0 = "# T\n\n正文\n", e1 = "# T\n\n正文 ==一==\n", e2 = "# T\n\n正文 ==一== ==二==\n"
let mac1 = "# T\n\n- 发布时间：2026年11月03日\n\n正文 ==一==\n"     // 电脑收下 e1、接回了时间行
var out: [String: Any] = [:]
func show(_ o: PendingEdits.Outcome) -> String {
    switch o { case .remove: "remove"; case .applied: "applied"; case .conflict(let k): "conflict:\(k)"; case .gone: "gone"
               case .resubmit: "resubmit"; case .pending(let w): w ? "pending-rewrite" : "pending" }
}
// 记录：连着改两次，bases 接上
var r1 = PendingEdits.edited(previous: nil, startText: h0, onMac: true, content: e1, now: 1000)
r1 = PendingEdits.submitted(r1, taskID: "t1")
var r2 = PendingEdits.edited(previous: r1, startText: e1, onMac: true, content: e2, now: 1000)   // 同一毫秒
r2 = PendingEdits.submitted(r2, taskID: "t2")
out["r1"] = ["bases": r1.bases, "at": r1.editedAt, "tasks": r1.taskIDs]
out["r2"] = ["bases": r2.bases, "at": r2.editedAt, "tasks": r2.taskIDs, "state": r2.state]
let fresh = PendingEdits.edited(previous: nil, startText: nil, onMac: false, content: "新", now: 5)
let fresh2 = PendingEdits.submitted(fresh, taskID: "t9")
out["newFileBases"] = fresh.bases
var many = r2
for i in 0..<12 { many = PendingEdits.edited(previous: many, startText: many.content, onMac: true, content: "v\(i)", now: 2000) }
out["capped"] = ["count": many.bases.count, "first": many.bases[0]]
// 显示、下一次改的起点
out["display"] = [PendingEdits.display(record: r2, disk: mac1) ?? "nil", PendingEdits.display(record: nil, disk: mac1) ?? "nil"]
// 对账：只按内容，不看时间
let blobMac1 = PendingEdits.blobSHA(mac1), blobE2 = PendingEdits.blobSHA(e2), blobH0 = PendingEdits.blobSHA(h0)
let waiting = PendingEdits.Last(state: "waiting"), doneM1 = PendingEdits.Last(state: "done", appliedSHA: blobMac1)
// r2 的最后一个任务是 t2；t1 已经写成 M1（absorb 进 olderBlobs）
let r2a = PendingEdits.absorb(r2, earlierApplied: [blobMac1])
func rc(_ r: R = r2a, deleted: Bool = false, tree: String?, last: PendingEdits.Last?, held: Bool = false, disk: String?) -> String {
    show(PendingEdits.reconcile(r, deletedLocally: deleted, treeSHA: tree, last: last, held: held, disk: disk))
}
out["a"] = rc(deleted: true, tree: blobE2, last: waiting, disk: e2)
out["b_same"] = rc(tree: blobE2, last: waiting, disk: e2)
out["b_applied"] = rc(r1, tree: blobMac1, last: doneM1, disk: mac1)
// 复审 1：t1 收下成 M1、t2 还在等、文件树是 M1 → 不放手，写回 E2
out["t2waiting_treeM1"] = rc(tree: blobMac1, last: waiting, disk: mac1)
// 同样 t2 失败了 → 冲突，记录留着
out["t2failed_treeM1"] = rc(tree: blobMac1, last: PendingEdits.Last(state: "failed", kind: "conflict"), disk: mac1)
out["t2failed_rejected"] = rc(tree: blobE2 == blobMac1 ? nil : blobMac1, last: PendingEdits.Last(state: "failed", kind: "rejected"), disk: mac1)
// 复审 6：t1 收下（applied=M1）之后电脑又改成 X → 放手，用 X
let x = PendingEdits.blobSHA("# T\n\n电脑上又划了线\n")
out["moved_on"] = rc(r1, tree: x, last: doneM1, disk: "X")
// 结果先到、文件树还是改之前那版（H0）→ 还在等，写回
out["done_tree_old"] = rc(r1, tree: blobH0, last: doneM1, disk: h0)
// 复审 7：电脑上少了一大批先没删（held）→ 不判没有了；文件回来了 → 又是等着
out["gone"] = rc(tree: nil, last: PendingEdits.Last(state: nil), disk: nil)
out["gone_held"] = rc(tree: nil, last: PendingEdits.Last(state: nil), held: true, disk: e2)
var wasGone = r2a; wasGone.state = "gone"
out["gone_recovers"] = rc(wasGone, tree: blobMac1, last: PendingEdits.Last(state: nil), disk: e2)
out["d_waiting"] = rc(tree: nil, last: waiting, disk: e2)
out["d_newfile"] = rc(fresh2, tree: nil, last: waiting, disk: "新")
// 复审 8：没交出去（没有任务 id）→ 再交
out["resubmit"] = rc(fresh, tree: nil, last: nil, disk: "新")
out["e_same"] = rc(tree: blobMac1, last: waiting, disk: e2)
out["older"] = r2a.olderBlobs.count
// 改名：挪到新名字下；两个名字都有记录时留改得晚的
let moved = PendingEdits.retarget(["old.md": r2, "keep.md": r1, "new2.md": r1], renames: ["old.md": "new.md", "x.md": "y.md"])
out["retarget"] = moved.mapValues(\.content)
// 两条落到同一个名字、内容不一样：都留着（挪不过去的留在旧名字下），不按时间丢
let clash = PendingEdits.retarget(["old.md": r2, "new.md": r1], renames: ["old.md": "new.md"])
out["retargetClash"] = clash.mapValues(\.content)
let clash2 = PendingEdits.retarget(["a.md": r1, "b.md": r2], renames: ["a.md": "n.md", "b.md": "n.md"])
out["retargetClash2"] = clash2.mapValues(\.content)
var same = r1; same.taskIDs = ["tx"]
let merged = PendingEdits.retarget(["old.md": same, "new.md": r1], renames: ["old.md": "new.md"])
out["retargetSame"] = merged.mapValues { "\($0.content)|\($0.taskIDs.joined(separator: ","))" }
out["mayDrop"] = [PendingEdits.mayDrop(pendingContent: e1, editedFrom: e1), PendingEdits.mayDrop(pendingContent: e1, editedFrom: mac1)]
out["issue"] = PendingEdits.issueStatus(["进度", "<!-- spark-status {\"state\": \"running\"} -->\n跑着", "<!-- spark-status {\"state\": \"done\", \"applied_sha\": \"abc\"} -->\n完成"]).map { [$0.state ?? "", $0.appliedSHA ?? ""] } ?? []
out["issueFail"] = PendingEdits.issueStatus(["<!-- spark-status {\"state\": \"failed\", \"kind\": \"gone\"} -->\n没成功"]).map { [$0.state ?? "", $0.kind ?? ""] } ?? []
// 旧版存的记录（少字段）照样读
let oldJSON = #"{"content":"x","bases":[],"editedAt":3,"taskIDs":["a"],"state":"pending"}"#
out["decodeOld"] = (try? JSONDecoder().decode(R.self, from: Data(oldJSON.utf8))).map { "\($0.content)|\($0.olderBlobs.count)|\($0.failKind ?? "nil")" } ?? "fail"
out["sha"] = [PendingEdits.sha256(h0), PendingEdits.blobSHA(h0)]
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
"""


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_swift_pending_edits_rules(tmp_path):
    import hashlib
    from core import cable_sync, remote_tasks
    main = tmp_path / "main.swift"
    main.write_text(PENDING_HARNESS)
    exe = tmp_path / "pending"
    src = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "PendingEdits.swift")
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), src, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout)
    sha = lambda t: hashlib.sha256(t.encode()).hexdigest()   # noqa: E731
    h0, e1, e2 = "# T\n\n正文\n", "# T\n\n正文 ==一==\n", "# T\n\n正文 ==一== ==二==\n"
    assert got["sha"] == [sha(h0), remote_tasks.blob_sha(h0.encode())], "指纹、blob sha 跟电脑一样"
    p = tmp_path / "h0.md"
    p.write_text(h0, encoding="utf-8")
    assert got["sha"][1] == cable_sync.blob_sha(str(p))
    assert got["r1"] == {"bases": [sha(h0)], "at": 1000, "tasks": ["t1"]}
    assert got["r2"] == {"bases": [sha(h0), sha(e1)], "at": 1001, "tasks": ["t1", "t2"], "state": "pending"}, \
        "再改一次：bases 加上前一次手机这份；同一毫秒也严格变大"
    assert got["newFileBases"] == [], "手机上新建、电脑上还没有的：不带 bases"
    assert got["capped"] == {"count": 14, "first": sha(h0)}, "12 次以内不截（上限 32），最早那版一直留着"
    assert got["display"] == [e2, "# T\n\n- 发布时间：2026年11月03日\n\n正文 ==一==\n"]
    assert got["a"] == "remove"
    assert got["b_same"] == "applied" and got["b_applied"] == "applied"
    assert got["t2waiting_treeM1"] == "pending-rewrite", "复审 1：t1 收下、t2 还在等：不放手，写回 E2"
    assert got["t2failed_treeM1"] == "conflict:conflict" and got["t2failed_rejected"] == "conflict:rejected"
    assert got["moved_on"] == "applied", "复审 6：电脑收下之后又往后改了：放手"
    assert got["done_tree_old"] == "pending-rewrite", "结果先到、文件树还是改之前那版：接着写回"
    assert got["gone"] == "gone" and got["gone_held"] == "pending", "复审 7：先没删的那次不判没有了"
    assert got["gone_recovers"] == "pending", "文件回来了：又是等着"
    assert got["d_waiting"] == "pending" and got["d_newfile"] == "pending"
    assert got["resubmit"] == "resubmit", "复审 8：没交出去的再交"
    assert got["e_same"] == "pending"
    assert got["older"] >= 3
    assert got["issueFail"] == ["failed", "gone"]
    assert got["decodeOld"] == "x|0|nil"
    assert got["retarget"] == {"new.md": e2, "keep.md": e1, "new2.md": e1}
    assert got["retargetClash"] == {"new.md": e1, "old.md": e2}, "内容不一样：挪不过去的留在旧名字下，不丢"
    assert got["retargetClash2"] == {"n.md": e1, "b.md": e2}
    assert got["retargetSame"] == {"new.md": e1 + "|t1,tx"}, "内容一样才合成一条"
    assert got["mayDrop"] == [True, False]
    assert got["issue"] == ["done", "abc"]


def test_label_is_fabu_and_legacy_fabiao_still_read():
    """写出去的是「发布时间」；上一版写的「发表时间」读得出发布那天，换写法时只换标签、时刻不丢。"""
    assert doctime.PUBLISHED_LABEL == "发布时间" and doctime.LEGACY_PUBLISHED_LABEL == "发表时间"
    assert doctime.lines(date(2026, 11, 9), None) == ["- 发布时间：2026年11月09日"]
    old = "# T\n\n- 发表时间：2026年11月19日，21:12（UTC+8）\n- 收录时间：2026年11月20日\n\n正文\n"
    assert doctime.parse(doctime.PUBLISHED_LINE.search(old).group(1)).date() == date(2026, 11, 19)
    assert doctime.time_line_kind("- 发表时间：2026年11月19日") == "pub_new"
    assert doctime.header_time_lines(old)["pub"] == "- 发布时间：2026年11月19日，21:12（UTC+8）"
    assert doctime.ANY_TIME_LINE.search("- 发布时间：x") and doctime.ANY_TIME_LINE.search("- 发表时间：x")


def test_sort_option_ids_unchanged_label_renamed():
    """排序选项只改显示的字（按发布时间），内部标识（存进 UserDefaults、网页 ?sort= 的值）不变，用户选过的排序不丢。"""
    with open(os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Clips.swift"), encoding="utf-8") as f:
        src = f.read()
    assert "case clipped, published, name" in src and 'case .published: "按发布时间"' in src and "按发表时间" not in src
    with open(os.path.join(ROOT, "apps", "macos", "Sources", "Spark", "Model.swift"), encoding="utf-8") as f:
        mac = f.read()
    assert 'case .published: return "发布时间"' in mac and "发表时间" not in mac
    from apps.reader import server as reader
    import inspect
    assert 'by == "published"' in inspect.getsource(reader._clip_items)
