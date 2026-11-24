"""手机上认「播放列表里没有标题的条目」（apps/mobile-ios/Shared/YouTubeAccess.swift 的 isUntitled、untitledNote）：
用 swiftc 单独编出来跑，跟电脑上 pipeline.is_untitled_entry、untitled_skip_note 比，口径要一样。
YouTube 的 RSS 源里标题是空的条目，Feed.parse 照旧补成 Untitled-N，但 feedTitle 留着原样，供 isUntitled 认。"""

import json
import os
import shutil
import subprocess

import pytest

from apps.summit2md import pipeline
from apps.summit2md.tests.test_untitled_entries import CASES

SHARED = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Shared")

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<entry><id>yt:video:aaaaaaaaaaa</id><yt:videoId>aaaaaaaaaaa</yt:videoId><title>Opening</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/><published>2026-11-20T10:00:00+00:00</published></entry>
<entry><id>yt:video:bbbbbbbbbbb</id><yt:videoId>bbbbbbbbbbb</yt:videoId><title></title>
<link rel="alternate" href="https://www.youtube.com/watch?v=bbbbbbbbbbb"/><published>2026-11-21T10:00:00+00:00</published></entry>
<entry><id>yt:video:ccccccccccc</id><yt:videoId>ccccccccccc</yt:videoId>
<link rel="alternate" href="https://www.youtube.com/watch?v=ccccccccccc"/><published>2026-11-22T10:00:00+00:00</published></entry>
<entry><id>yt:video:ddddddddddd</id><yt:videoId>ddddddddddd</yt:videoId><title>[Private video]</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=ddddddddddd"/><published>2026-11-23T10:00:00+00:00</published></entry>
</feed>"""

MAIN = r'''
import Foundation
let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
let titles = input["titles"] as! [Any]
var out: [String: Any] = [:]
out["untitled"] = titles.map { YouTubeAccess.isUntitled($0 as? String) }
out["note0"] = YouTubeAccess.untitledNote(0) ?? NSNull()
out["note2"] = YouTubeAccess.untitledNote(2) ?? NSNull()
let entries = Feed.parse((input["feed"] as! String).data(using: .utf8)!)
out["feed"] = entries.map { ["\($0.videoID)", $0.title, $0.feedTitle ?? "<nil>", "\(YouTubeAccess.isUntitled($0.feedTitle))"] }
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_untitled_swift_matches_python(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    srcs = [os.path.join(SHARED, f) for f in ("YouTubeAccess.swift", "FeedParser.swift", "DocTime.swift", "SparkError.swift",
                                                   "Fixture.swift")]   # FeedParser.fetch 先过 NetGate
    r = subprocess.run(["swiftc", "-o", str(exe), *srcs, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    payload = {"titles": [t for t, _ in CASES], "feed": FEED}
    run = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    got = json.loads(run.stdout)
    assert got["untitled"] == [want for _, want in CASES]
    assert got["untitled"] == [pipeline.is_untitled_entry(t) for t, _ in CASES], "手机和电脑的判定要一样"
    assert got["note0"] is None and pipeline.untitled_skip_note(0) == ""
    assert got["note2"] == pipeline.untitled_skip_note(2)
    # RSS：标题照旧补 Untitled-N（别的源不受影响），feedTitle 是原样：空串、没写（nil）、占位文字都认成没有标题
    assert got["feed"] == [
        ["aaaaaaaaaaa", "Opening", "Opening", "false"],
        ["bbbbbbbbbbb", "Untitled-2", "", "true"],
        ["ccccccccccc", "Untitled-3", "<nil>", "true"],
        ["ddddddddddd", "[Private video]", "[Private video]", "true"],
    ]
