"""列表阶段把 YouTube 译好的英文标题换回 RSS 里的原标题（不联网、不调 yt-dlp）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from apps.summit2md import pipeline  # noqa: E402

EN = 'Jensen Huang’s "Light": A Deep Dive'
ZH = "黄仁勋的“光”：详解AI算力 #ai #podcast"
V1, V2, V3 = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def rss(pairs):
    items = "".join(
        f"<entry><yt:videoId>{i}</yt:videoId><title>{t}</title></entry>" for i, t in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        f'xmlns="http://www.w3.org/2005/Atom"><title>x</title>{items}</feed>'
    ).encode()


class FakeYDL:
    info = {}

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return dict(self.info)


def run(url, info, body=None, exc=None):
    FakeYDL.info = info
    calls = []

    def fake_get(u, **kw):
        calls.append(u)
        if exc:
            raise exc
        return body

    with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL), \
         mock.patch.object(pipeline.sources, "http_get", fake_get):
        res = pipeline._fetch_youtube_playlist(url)
    return res, calls


def listing(**extra):
    return {"id": "PLx", "title": "S", "entries": [
        {"id": V1, "title": EN, "duration": 60},
        {"id": V2, "title": "中文标题", "duration": 60},
        {"id": V3, "title": "English three", "duration": 60},
    ], **extra}


class ListOriginalTitle(unittest.TestCase):
    def titles(self, res):
        return {e["id"]: e["title"] for e in res["entries"]}

    def test_playlist_replaces_english_with_chinese(self):
        body = rss([(V1, ZH), (V2, "别的中文"), (V3, "English rss")])
        res, calls = run("https://www.youtube.com/playlist?list=PLabcdefgh12", listing(), body)
        t = self.titles(res)
        self.assertEqual(t[V1], "黄仁勋的“光”：详解AI算力")
        self.assertEqual(t[V2], "中文标题")       # 列表已是中文：不动
        self.assertEqual(t[V3], "English three")  # RSS 是英文：不动
        self.assertEqual(calls, ["https://www.youtube.com/feeds/videos.xml?playlist_id=PLabcdefgh12"])

    def test_channel_uses_channel_id(self):
        res, calls = run("https://www.youtube.com/@TheValley101/videos",
                         listing(channel_id="UCabc"), rss([(V1, ZH)]))
        self.assertEqual(self.titles(res)[V1], "黄仁勋的“光”：详解AI算力")
        self.assertIn("https://www.youtube.com/feeds/videos.xml?channel_id=UCabc", calls)

    def test_id_missing_in_rss_unchanged(self):
        res, _ = run("https://www.youtube.com/playlist?list=PLabcdefgh12", listing(), rss([("zzzzzzzzzzz", ZH)]))
        self.assertEqual(self.titles(res)[V1], EN)

    def test_failures_leave_list_untouched(self):
        for kw in ({"exc": TimeoutError("t")}, {"exc": OSError("x")}, {"body": b"<feed><oops"}, {"body": b""}):
            res, _ = run("https://www.youtube.com/playlist?list=PLabcdefgh12", listing(), **kw)
            self.assertEqual(self.titles(res)[V1], EN)

    def test_single_video_makes_no_rss_request(self):
        res, calls = run(f"https://www.youtube.com/watch?v={V1}", {"id": V1, "title": EN, "duration": 60}, rss([(V1, ZH)]))
        self.assertEqual(calls, [])
        self.assertEqual(res["entries"][0]["title"], EN)


if __name__ == "__main__":
    unittest.main()
