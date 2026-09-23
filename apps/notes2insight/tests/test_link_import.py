"""从剪贴板粘贴的链接批量导入成笔记：link_import.py 的抓取/落盘逻辑。

实际的网络抓取全部走 core/sources.py（已经在那边测过），这里只 mock 掉
sources 层的入口函数，专注测"抓到内容之后怎么落成一篇笔记"这部分自己的逻辑：
RSS/Apple Podcast 展开成多篇、单篇来源落一篇、没有正文的怎么报错、订阅源
太大时怎么截断。
"""

import io
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from apps.notes2insight import link_import, server, uploads


class ImportFromTextTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="n2i_link_import_test_")

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_没有链接就报错(self):
        with self.assertRaises(RuntimeError):
            link_import.import_from_text(self.dest, "这段话里啥链接都没有")

    def test_一篇超时不影响同一个源里的其它篇(self):
        entries = [{"id": str(i), "title": f"第{i}篇", "url": f"https://x/{i}", "source_type": "rss"}
                   for i in (1, 2)]
        ok_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}

        def fake_text(entry, cache_dir):
            if entry["id"] == "1":
                raise TimeoutError("The read operation timed out")
            return ok_text

        with mock.patch("apps.notes2insight.link_import.sources.is_rss_url", return_value=True), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_rss_playlist",
                        return_value={"entries": entries, "summit_title": "源"}), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", side_effect=fake_text):
            notes, errors = link_import.import_from_text(self.dest, "https://x/feed")
        self.assertEqual([n["title"] for n in notes], ["第2篇"])
        self.assertIn("timed out", errors[0]["error"])

    def test_单篇网页文章落成一篇笔记(self):
        fake_entry = {"id": "x", "title": "一篇文章", "url": "https://example.com/a", "source_type": "article"}
        fake_text = {"paragraphs": [(0.0, "第一段。"), (0.0, "第二段。")], "speakers": None,
                     "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        return_value=fake_entry), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            notes, errors = link_import.import_from_text(self.dest, "https://example.com/a")
        self.assertEqual(errors, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "一篇文章")
        with open(os.path.join(self.dest, notes[0]["path"]), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("第一段。", content)
        self.assertIn("来源：网页文章", content)

    def test_没有正文时不生成笔记而是报错(self):
        fake_entry = {"id": "x", "title": "没内容的文章", "url": "https://example.com/a"}
        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        return_value=fake_entry), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=None):
            notes, errors = link_import.import_from_text(self.dest, "https://example.com/a")
        self.assertEqual(notes, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("没内容的文章", errors[0]["name"])

    def test_rss订阅源展开成多篇笔记(self):
        entries = [
            {"id": f"e{i}", "title": f"第{i}篇", "url": f"https://example.com/{i}", "source_type": "rss"}
            for i in range(3)
        ]
        fake_playlist = {"summit_title": "测试节目", "entries": entries}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.is_rss_url", return_value=True), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_rss_playlist",
                        return_value=fake_playlist), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            notes, errors = link_import.import_from_text(self.dest, "https://example.com/feed.xml")
        self.assertEqual(errors, [])
        self.assertEqual(len(notes), 3)
        self.assertEqual({n["title"] for n in notes}, {"第0篇", "第1篇", "第2篇"})

    def test_订阅源条目太多时截断并说明(self):
        entries = [
            {"id": f"e{i}", "title": f"第{i}篇", "url": f"https://example.com/{i}", "source_type": "rss"}
            for i in range(link_import.MAX_ENTRIES_PER_FEED + 5)
        ]
        fake_playlist = {"summit_title": "测试节目", "entries": entries}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.is_rss_url", return_value=True), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_rss_playlist",
                        return_value=fake_playlist), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            notes, errors = link_import.import_from_text(self.dest, "https://example.com/feed.xml")
        self.assertEqual(len(notes), link_import.MAX_ENTRIES_PER_FEED)
        self.assertEqual(len(errors), 1)
        self.assertIn("只取了最新", errors[0]["error"])

    def test_youtube链接明确提示不支持(self):
        notes, errors = link_import.import_from_text(self.dest, "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(notes, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("summit2md", errors[0]["error"])

    def test_微信文章解析失败时给出人话原因(self):
        with mock.patch("apps.notes2insight.link_import.sources.fetch_wechat_article_playlist",
                        side_effect=RuntimeError("boom")):
            notes, errors = link_import.import_from_text(self.dest, "https://mp.weixin.qq.com/s/x")
        self.assertEqual(notes, [])
        self.assertIn("boom", errors[0]["error"])

    def test_混合链接里一个失败不影响其它(self):
        fake_entry = {"id": "x", "title": "一篇文章", "url": "https://example.com/good"}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}

        def fake_fetch(url):
            if "good" in url:
                return fake_entry
            raise RuntimeError("打不开")

        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        side_effect=fake_fetch), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            notes, errors = link_import.import_from_text(
                self.dest, "https://example.com/good https://example.com/bad")
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(errors), 1)

    def test_超过单批链接数上限就报错(self):
        text = " ".join(f"https://example.com/{i}" for i in range(link_import.MAX_LINKS_PER_BATCH + 1))
        with self.assertRaises(RuntimeError):
            link_import.import_from_text(self.dest, text)


class ApiImportLinksRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp_uploads_root = tempfile.mkdtemp(prefix="n2i_uploads_root_")
        self._orig_root = uploads.UPLOADS_ROOT
        uploads.UPLOADS_ROOT = self._tmp_uploads_root

    def tearDown(self):
        uploads.UPLOADS_ROOT = self._orig_root
        shutil.rmtree(self._tmp_uploads_root, ignore_errors=True)

    def _import_and_wait(self, payload):
        """导入是后台任务：发起后轮询到结束，再取结果（mock 要在任务跑完之前一直生效）。"""
        r = self.client.post("/api/import_links", json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        started = r.get_json()
        for _ in range(250):
            p = self.client.get(f"/api/progress/{started['job_id']}").get_json()
            if p["done"]:
                break
            time.sleep(0.02)
        self.assertTrue(p["done"] and p["ok"], p)
        result = self.client.get(f"/api/result/{started['job_id']}").get_json()
        self.assertEqual((result["session"], result["root"]), (started["session"], started["root"]))
        return result

    def test_没有文字时返回_400(self):
        r = self.client.post("/api/import_links", json={})
        self.assertEqual(r.status_code, 400)

    def test_正常导入返回可直接喂给_run_的形状(self):
        fake_entry = {"id": "x", "title": "一篇文章", "url": "https://example.com/a"}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        return_value=fake_entry), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            d = self._import_and_wait({"text": "https://example.com/a"})
        self.assertTrue(os.path.isdir(d["root"]))
        self.assertEqual(len(d["notes"]), 1)
        self.assertEqual(d["errors"], [])

    def test_和_api_upload_共用同一个_session(self):
        r1 = self.client.post("/api/upload", data={
            "files": (io.BytesIO(b"a"), "a.md"),
        }, content_type="multipart/form-data")
        sid = r1.get_json()["session"]

        fake_entry = {"id": "x", "title": "一篇文章", "url": "https://example.com/a"}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        return_value=fake_entry), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            d2 = self._import_and_wait({"session": sid, "text": "https://example.com/a"})
        self.assertEqual(d2["session"], sid)
        self.assertEqual(r1.get_json()["root"], d2["root"])
        self.assertEqual(len(os.listdir(d2["root"])), 2)

    def test_伪造的_session_不会被当路径拼进去(self):
        fake_entry = {"id": "x", "title": "一篇文章", "url": "https://example.com/a"}
        fake_text = {"paragraphs": [(0.0, "正文。")], "speakers": None, "speaker_mode": None, "lang": "zh"}
        with mock.patch("apps.notes2insight.link_import.sources.fetch_generic_article_entry",
                        return_value=fake_entry), \
             mock.patch("apps.notes2insight.link_import.sources.fetch_source_text", return_value=fake_text):
            d = self._import_and_wait({"session": "../../../etc", "text": "https://example.com/a"})
        self.assertNotEqual(d["session"], "../../../etc")
        self.assertRegex(d["session"], r"^[0-9a-f]{32}$")
        self.assertTrue(os.path.abspath(d["root"]).startswith(os.path.abspath(self._tmp_uploads_root)))


    def test_链接太多直接报错_不启动任务(self):
        text = "\n".join(f"https://example.com/{i}" for i in range(link_import.MAX_LINKS_PER_BATCH + 1))
        r = self.client.post("/api/import_links", json={"text": text})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("job_id", r.get_json())

    def test_进度逐条更新(self):
        seen = []
        with mock.patch.object(link_import, "_fetch_entries_for_url", return_value=([], "", "打不开")):
            link_import.import_urls(self._tmp_uploads_root, ["https://a", "https://b"],
                                    progress=lambda *a: seen.append(a[1:3]))
        self.assertEqual(seen, [(0, 2), (1, 2)])


if __name__ == "__main__":
    unittest.main()
