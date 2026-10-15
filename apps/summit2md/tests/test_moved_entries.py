"""源里条目换了 id（站点迁移、guid 改格式）时按链接/标题对上原来的记录，不重复处理。"""

import unittest
from unittest import mock

from apps.summit2md import pipeline, tracking


def _row(eid, title, url, date="", ok=True):
    return {"rank": 1, "ok": ok, "entry": {"id": eid, "title": title, "url": url, "publish_date": date,
                                           "source_type": "rss"},
            "relative_path": f"transcripts/{date or '000'}_{title}.md", "summary": {"tldr": "x"}}


def _e(eid, title, url, date="", source_type="rss"):
    return {"id": eid, "title": title, "url": url, "publish_date": date, "source_type": source_type}


class MatchMovedTests(unittest.TestCase):
    def test_guid变了链接没变_按链接对上(self):
        rows = {"old": _row("old", "第一篇文章", "https://a.com/p/1", "20260101")}
        self.assertEqual(pipeline.match_moved_entries(rows, [_e("new", "改了标题", "https://a.com/p/1")]),
                         {"new": "old"})

    def test_guid和链接都变了_标题加日期对上_日期不同不认(self):
        rows = {"old": _row("old", "Scaling Laws 再思考", "https://old.com/1", "20260101")}
        same = _e("new", "Scaling Laws：再思考", "https://new.com/1", "20260101")
        self.assertEqual(pipeline.match_moved_entries(rows, [same]), {"new": "old"})
        other_day = _e("new", "Scaling Laws 再思考", "https://new.com/1", "20260301")
        self.assertEqual(pipeline.match_moved_entries(rows, [other_day]), {})

    def test_没日期时标题要够长且唯一_对上多条不认(self):
        rows = {"a": _row("a", "Weekly AI roundup edition", "https://o/1"),
                "b": _row("b", "Weekly AI roundup edition", "https://o/2")}
        self.assertEqual(pipeline.match_moved_entries(rows, [_e("n", "Weekly AI roundup edition", "https://x/1")]), {})
        rows = {"a": _row("a", "Weekly AI roundup edition", "https://o/1")}
        self.assertEqual(pipeline.match_moved_entries(rows, [_e("n", "Weekly AI roundup edition", "https://x/1")]),
                         {"n": "a"})
        self.assertEqual(pipeline.match_moved_entries({"a": _row("a", "短标题啊", "https://o/1")},
                                                      [_e("n", "短标题啊", "https://x/1")]), {})

    def test_旧id还在源里就不对_YouTube视频不按标题对(self):
        rows = {"old": _row("old", "Keynote Session Day Two", "https://o/1")}
        entries = [_e("old", "Keynote Session Day Two", "https://o/1"),
                   _e("new", "Keynote Session Day Two", "https://n/1")]
        self.assertEqual(pipeline.match_moved_entries(rows, entries), {})
        yt = {"id": "vid2", "title": "Keynote Session Day Two", "url": "https://www.youtube.com/watch?v=vid2"}
        self.assertEqual(pipeline.match_moved_entries(rows, [yt]), {})

    def test_挪记录时主题分组和已并入总结的id也跟着换(self):
        manifest = {"entries": {"old": _row("old", "第一篇文章", "https://a.com/p/1")},
                    "topic_groups": {"T": ["old", "x"]}, "summary_merged_ids": ["old"]}
        n = pipeline.adopt_moved_entries(manifest, [_e("new", "第一篇文章", "https://a.com/p/1")])
        self.assertEqual(n, 1)
        self.assertEqual(list(manifest["entries"]), ["new"])
        self.assertEqual(manifest["entries"]["new"]["entry"]["id"], "new")
        self.assertEqual(manifest["topic_groups"], {"T": ["new", "x"]})
        self.assertEqual(manifest["summary_merged_ids"], ["new"])


class FindNewTests(unittest.TestCase):
    def test_换了id的老内容不算新内容(self):
        done = {"old": _row("old", "第一篇文章", "https://a.com/p/1", "20260101")}
        entries = [_e("new", "第一篇文章", "https://a.com/p/1", "20260101"),
                   _e("fresh", "真正的新文章", "https://a.com/p/2", "20260105")]
        with mock.patch.object(tracking, "_manifest_entries", return_value=done):
            new = tracking.find_new({"folder": "/x", "url": "https://a.com/feed"}, entries)
        self.assertEqual([e["id"] for e in new], ["fresh"])

    def test_find_new_entries_同样认得换了id的(self):
        manifest = {"entries": {"old": _row("old", "第一篇文章", "https://a.com/p/1")}}
        with mock.patch.object(pipeline, "_load_manifest", return_value=manifest):
            got = pipeline.find_new_entries("/base", "S", [_e("new", "第一篇文章", "https://a.com/p/1")])
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
