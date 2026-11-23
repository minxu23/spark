"""播放列表里没有标题的条目（私享、已删除、还没公开的视频）：不列、不记成已处理、不当失败报，
结果说明里说一句跳过了几个。yt-dlp 全部 mock 掉，不联网。手机上同一套口径见 tests/test_untitled_entries_swift.py。"""

import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline, server, subscriptions_store, tracking

# 跟 tests/test_untitled_entries_swift.py 共用的判定用例：(标题, 算不算没有标题)
CASES = [
    (None, True),
    ("", True),
    ("   ", True),
    ("\t\n", True),
    ("　", True),
    ("\xa0", True),
    ("[Private video]", True),
    ("[Deleted video]", True),
    ("  [Private video]\n", True),
    ("[private video]", False),          # 占位文字只认整串一模一样的
    ("[Private video] part 2", False),
    ("[ORIGINAL] Kid cussing", False),   # 2026-11-23 实测列表里的真标题，方括号开头
    ("Untitled-3", False),               # 真有人这么起标题：只看原始标题，不看我们以前补的名字
    ("Talk", False),
    ("#ai", False),
]

NOTE = "跳过了 2 个没有标题的条目（可能是私享、已删除或还没公开的视频）"


def _fake_dl(info):
    class FakeDL:
        def __init__(self, opts):
            pass

        def extract_info(self, url, download=False):
            return json.loads(json.dumps(info))

    return lambda opts: contextlib.nullcontext(FakeDL(opts))


def _playlist(entries, title="Summit"):
    return {"id": "PLtest", "title": title, "entries": entries}


# 第 2 个是 title=None（Training Data 里的私享视频那样），第 4 个是占位文字
MIXED = [
    {"id": "aaaaaaaaaaa", "title": "Opening", "duration": 600},
    {"id": "tRaFbDFBl_A", "title": None, "duration": None},
    {"id": "bbbbbbbbbbb", "title": "Keynote #ai", "duration": 1200},
    {"id": "ccccccccccc", "title": "[Deleted video]", "duration": None},
    {"id": "ddddddddddd", "title": "Panel", "duration": 1800},
]
URL = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"


class UntitledPredicateTests(unittest.TestCase):
    def test_判定(self):
        for title, want in CASES:
            with self.subTest(title=title):
                self.assertIs(pipeline.is_untitled_entry(title), want)

    def test_非字符串算没有标题(self):
        self.assertTrue(pipeline.is_untitled_entry(123))

    def test_结果说明(self):
        self.assertEqual(pipeline.untitled_skip_note(0), "")
        self.assertEqual(pipeline.untitled_skip_note(2), NOTE)


class FetchPlaylistTests(unittest.TestCase):
    def _fetch(self, info, url=URL, **kw):
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(info)):
            return pipeline.fetch_playlist(url, **kw)

    def test_跳过_序号仍是列表里的位置(self):
        # 不往前补：已处理条目的编号就是当时的位置，补位会让新条目跟它们撞号
        got = self._fetch(_playlist(MIXED))
        self.assertEqual([e["id"] for e in got["entries"]], ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"])
        self.assertEqual([e["index"] for e in got["entries"]], [1, 3, 5])
        self.assertEqual([e["title"] for e in got["entries"]], ["Opening", "Keynote", "Panel"])
        self.assertEqual(got["skipped_untitled"], 2)
        self.assertNotIn("Untitled", json.dumps(got))

    def test_没有这种条目时跟原来一样(self):
        entries = [e for e in MIXED if pipeline.is_untitled_entry(e["title"]) is False]
        got = self._fetch(_playlist(entries))
        self.assertNotIn("skipped_untitled", got)
        self.assertEqual([e["index"] for e in got["entries"]], [1, 2, 3])
        self.assertEqual([(e["title"], e["duration"]) for e in got["entries"]],
                         [("Opening", 600), ("Keynote", 1200), ("Panel", 1800)])

    def test_原有的空洞不变_取不到的条目和不合规的id照旧占位置(self):
        # yt-dlp ignoreerrors 时取不到的条目是 None；id 不合规的照旧丢掉。这两种和没有标题的条目一样，都占着列表里的位置
        got = self._fetch(_playlist([
            {"id": "aaaaaaaaaaa", "title": "A"}, None, {"id": "bad id!", "title": "B"},
            {"id": "ccccccccccc", "title": None}, {"id": "ddddddddddd", "title": "D"},
        ]))
        self.assertEqual([(e["id"], e["index"]) for e in got["entries"]], [("aaaaaaaaaaa", 1), ("ddddddddddd", 5)])
        self.assertEqual(got["skipped_untitled"], 1)

    def test_全是没有标题的(self):
        got = self._fetch(_playlist([{"id": "aaaaaaaaaaa", "title": None}, {"id": "bbbbbbbbbbb", "title": "[Private video]"}]))
        self.assertEqual(got["entries"], [])
        self.assertEqual(got["skipped_untitled"], 2)

    def test_单个视频照旧(self):
        # 不是播放列表的来源不受影响：单个视频没有标题时还是原来的 Untitled-1
        got = self._fetch({"id": "aaaaaaaaaaa", "title": None, "duration": 60},
                          url="https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertEqual([(e["id"], e["title"], e["index"]) for e in got["entries"]], [("aaaaaaaaaaa", "Untitled-1", 1)])
        self.assertNotIn("skipped_untitled", got)


class DiscoverTests(unittest.TestCase):
    def test_会议获取列表时说一句(self):
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED))):
            r = server.app.test_client().post("/api/discover", json={"url": URL})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertEqual(len(d["entries"]), 3)
        self.assertEqual(d["warnings"], [NOTE])

    def test_没跳过就不说(self):
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED[:1]))):
            d = server.app.test_client().post("/api/discover", json={"url": URL}).get_json()
        self.assertEqual(d["warnings"], [])

    def test_待完成会议刷新列表时说一句_本地的旧记录不补回来(self):
        with tempfile.TemporaryDirectory() as root:
            imported = {"entries": [{"id": "tRaFbDFBl_A", "title": "Untitled-2", "rank": 2},
                                    {"id": "aaaaaaaaaaa", "title": "Opening", "rank": 1}],
                        "source_url": URL, "summit_title": "S", "imported_dir": root}
            with mock.patch.object(pipeline, "import_output_directory", return_value=imported), \
                 mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED))):
                d = server.app.test_client().post("/api/import_dir", json={"path": root, "refresh_source": True}).get_json()
        self.assertTrue(d["source_refreshed"])
        self.assertNotIn("tRaFbDFBl_A", [e["id"] for e in d["entries"]])
        self.assertEqual(d["entries"][0]["rank"], 1)
        self.assertEqual(d["skipped_untitled_note"], NOTE)


class SubscriptionCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self._patch = mock.patch.object(subscriptions_store, "STORE_PATH", os.path.join(self.root, "subscriptions.json"))
        self._patch.start()
        tracking._list_cache.clear()

    def tearDown(self):
        tracking._list_cache.clear()
        self._patch.stop()
        self._tmp.cleanup()

    def _sub(self, kind):
        folder = os.path.join(self.root, kind)
        os.makedirs(folder)
        return {"id": f"s-{kind}", "url": URL, "kind": kind, "source_type": "youtube", "folder": folder, "name": "S"}

    def test_检查订阅_不列_不算失败_不记成已处理_说一句(self):
        for kind in ("podcast", "track"):
            with self.subTest(kind=kind):
                sub = self._sub(kind)
                with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED))):
                    got = tracking.check(sub, fresh=True)
                self.assertIsNone(got["error"])
                self.assertEqual(got["total"], 3)
                self.assertEqual(sorted(e["id"] for e in got["new_entries"]), ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"])
                self.assertEqual(got["skipped_untitled_note"], NOTE)
                self.assertTrue(all(not e.get("last_error") for e in got["new_entries"]))
                # 检查不写处理记录：跳过的条目没记成已处理，下次有了标题照常列出来
                self.assertEqual(pipeline._load_manifest(sub["folder"])["entries"], {})

    def test_有了标题以后照常列出来(self):
        sub = self._sub("track")
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED))):
            tracking.check(sub, fresh=True)
        later = [dict(e, title=e["title"] or "Premiere") for e in MIXED]
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(later))):
            got = tracking.check(sub, fresh=True)
        self.assertIn("tRaFbDFBl_A", [e["id"] for e in got["new_entries"]])
        self.assertEqual(got["skipped_untitled_note"], pipeline.untitled_skip_note(1))

    def test_没跳过就不带这一项(self):
        sub = self._sub("track")
        with mock.patch.object(pipeline, "_youtube_dl", _fake_dl(_playlist(MIXED[:1]))):
            got = tracking.check(sub, fresh=True)
        self.assertNotIn("skipped_untitled_note", got)


if __name__ == "__main__":
    unittest.main()
