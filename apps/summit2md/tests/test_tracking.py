"""「信息跟进」的新流程：轻量检查 → 逐条笔记 → 跨订阅的本批简报。"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline, server, subscriptions_store, tracking
from core import sources

LLM = {"backend": "api", "api_key": "k", "model": "m", "api_base": ""}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._patch = mock.patch.object(
            subscriptions_store, "STORE_PATH", os.path.join(self.root, "subscriptions.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def add(self, name="AI Insider", url="https://example.com/feed", source_type="rss"):
        return subscriptions_store.add(url=url, name=name, category="海外AI媒体",
                                       output_dir=self.root, source_type=source_type)


class StoreTests(_StoreCase):
    def test_新订阅有固定文件夹_改名不会跟着变(self):
        sub = self.add()
        self.assertEqual(sub["folder"], os.path.join(self.root, "信息跟进", "AI Insider"))
        subscriptions_store.update(sub["id"], {"name": "改了名字"})
        self.assertEqual(subscriptions_store.get(sub["id"])["folder"], sub["folder"])

    def test_老记录没有folder时自动补上(self):
        with open(subscriptions_store.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"id": "x", "url": "u", "name": "旧订阅", "category": "c",
                        "output_dir": "/vault/Spark", "source_type": "rss"}], f)
        item = subscriptions_store.get("x")
        self.assertEqual(item["folder"], os.path.join("/vault/Spark", "信息跟进", "旧订阅"))
        self.assertEqual(item["ignored_ids"], [])
        with open(subscriptions_store.STORE_PATH, encoding="utf-8") as f:
            self.assertIn("folder", json.load(f)[0])

    def test_忽略条目去重并保留顺序(self):
        sub = self.add()
        subscriptions_store.ignore(sub["id"], ["a", "b"])
        subscriptions_store.ignore(sub["id"], ["a", "c"])
        self.assertEqual(subscriptions_store.get(sub["id"])["ignored_ids"], ["b", "a", "c"])


class FindNewTests(_StoreCase):
    def test_已成功的和已忽略的不算新_失败过的带上原因(self):
        sub = self.add()
        os.makedirs(sub["folder"])
        pipeline._save_manifest(sub["folder"], {"entries": {
            "done": {"ok": True}, "failed": {"ok": False, "error": "付费墙"}}})
        subscriptions_store.ignore(sub["id"], ["ignored"])
        sub = subscriptions_store.get(sub["id"])
        entries = [{"id": i, "title": i, "publish_date": d} for i, d in
                   [("done", "20260101"), ("failed", "20260102"), ("ignored", "20260103"), ("fresh", "20260104")]]
        new = tracking.find_new(sub, entries)
        self.assertEqual([e["id"] for e in new], ["fresh", "failed"])
        self.assertEqual(new[1]["last_error"], "付费墙")

    def test_按来源类型直接走对应抓取(self):
        with mock.patch.object(sources, "fetch_rss_playlist", return_value={"entries": []}) as rss:
            tracking.list_entries({"url": "https://x/feed", "source_type": "rss"})
        rss.assert_called_once()
        with mock.patch.object(sources, "fetch_sitemap_playlist", return_value={"entries": []}) as sm:
            tracking.list_entries({"url": "https://x/news", "source_type": "article"})
        sm.assert_called_once_with("https://x/news", fetch_bodies=False)
        with mock.patch.object(pipeline, "fetch_playlist", return_value={"entries": []}) as fp:
            tracking.list_entries({"url": "https://podcasts.apple.com/x/id123", "source_type": "rss"})
        fp.assert_called_once()


class LightSitemapTests(unittest.TestCase):
    def test_只列链接不抓正文_标题从链接推断(self):
        with mock.patch.object(sources, "_discover_sitemap_url", return_value="https://x/sitemap.xml"), \
             mock.patch.object(sources, "_collect_sitemap_urls", return_value=[
                 ("https://x/news/partnering-with-accenture", "2026-09-18"),
             ]), \
             mock.patch.object(sources, "fetch_generic_article_entry") as fetch_body:
            result = sources.fetch_sitemap_playlist("https://x/news", fetch_bodies=False)
        fetch_body.assert_not_called()
        entry = result["entries"][0]
        self.assertEqual(entry["title"], "Partnering with accenture")
        self.assertEqual(entry["publish_date"], "20260918")
        self.assertEqual(entry["id"], sources._stable_id("https://x/news/partnering-with-accenture"))

    def test_没抓过正文的文章条目在处理时才去抓(self):
        entry = {"id": "a1", "url": "https://x/news/a", "source_type": "article", "title": "A"}
        fetched = {"article_content_html": "<p>" + "正文内容。" * 60 + "</p>", "title": "真实标题"}
        with tempfile.TemporaryDirectory() as cache_dir, \
             mock.patch.object(sources, "fetch_generic_article_entry", return_value=fetched):
            got = sources.fetch_source_text(entry, cache_dir)
        self.assertTrue(got["paragraphs"])
        self.assertEqual(entry["title"], "真实标题")


class ProcessAndBriefTests(_StoreCase):
    def _entries(self):
        return [
            {"id": "e1", "title": "Robotiq 开源工具", "url": "https://x/1", "source_type": "rss",
             "publish_date": "20260923", "rss_content_html": "<p>" + "机器人夹爪软件。" * 30 + "</p>"},
            {"id": "e2", "title": "没有正文的一条", "url": "https://x/2", "source_type": "rss",
             "rss_content_html": ""},
        ]

    def test_整批处理_单条笔记_manifest_跨订阅简报(self):
        sub = self.add()
        calls = []

        def fake_llm(prompt, *a, **kw):
            calls.append(prompt)
            if "资讯主编" in prompt:
                return "## 要点速览\n\n- 夹爪软件开源 [1]\n\n### 机器人\n\nRobotiq 开源了工具 [1][9]。"
            return "TLDR: 夹爪软件开源\n- 要点一\n- 要点二"

        with mock.patch.object(tracking, "list_entries", return_value={"entries": self._entries()}), \
             mock.patch.object(sources, "_fetch_generic_article_paragraphs", return_value=None), \
             mock.patch.object(pipeline, "_cached_summarize", side_effect=fake_llm):
            result = tracking.run_batch([(sub, ["e1", "e2"])], output_dir=self.root, llm=LLM)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["failed"][0]["id"], "e2")
        self.assertEqual(len(calls), 2)  # 一条小结 + 一份简报；没正文的那条不调模型

        note = os.path.join(sub["folder"], "20260923_Robotiq 开源工具.md")
        with open(note, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("## 小结", text)
        self.assertIn("**夹爪软件开源**", text)
        self.assertIn("## 原文", text)
        self.assertIn("机器人夹爪软件", text)

        manifest = pipeline._load_manifest(sub["folder"])["entries"]
        self.assertTrue(manifest["e1"]["ok"])
        self.assertFalse(manifest["e2"]["ok"])
        self.assertEqual(tracking.find_new(sub, self._entries())[0]["id"], "e2")

        brief = result["brief_markdown"]
        self.assertTrue(result["brief_path"].startswith(os.path.join(self.root, "信息跟进", "简报")))
        rel = "../AI Insider/20260923_Robotiq 开源工具.md"
        self.assertIn(f"[\\[1\\]](<{rel}>)", brief)  # 引用编号变成链接
        self.assertIn("[9]", brief)  # 不存在的编号原样保留
        self.assertIn(f"1. [Robotiq 开源工具](<{rel}>) — AI Insider · 2026-09-23", brief)

    def test_中途停止不出简报_已处理的保留(self):
        sub = self.add()
        entries = self._entries()[:1] + [dict(self._entries()[0], id="e3", title="第三条")]
        seen = []

        def stop_after_first():
            return len(seen) >= 1

        def fake_llm(prompt, *a, **kw):
            seen.append(prompt)
            return "TLDR: x\n- y"

        with mock.patch.object(tracking, "list_entries", return_value={"entries": entries}), \
             mock.patch.object(pipeline, "_cached_summarize", side_effect=fake_llm):
            result = tracking.run_batch([(sub, ["e1", "e3"])], output_dir=self.root, llm=LLM,
                                        stop_flag=stop_after_first)
        self.assertTrue(result["stopped"])
        self.assertIsNone(result["brief_path"])
        self.assertEqual(result["processed"], 1)
        self.assertTrue(pipeline._load_manifest(sub["folder"])["entries"]["e1"]["ok"])

    def test_条目已经不在源里了记为失败(self):
        sub = self.add()
        with mock.patch.object(tracking, "list_entries", return_value={"entries": []}):
            result = tracking.run_batch([(sub, ["gone"])], output_dir=self.root, llm=LLM)
        self.assertEqual(result["processed"], 0)
        self.assertIn("不在", result["failed"][0]["error"])


class TrackApiTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.client = server.app.test_client()

    def test_忽略接口(self):
        sub = self.add()
        r = self.client.post(f"/api/subscriptions/{sub['id']}/ignore", json={"entry_ids": ["a"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(subscriptions_store.get(sub["id"])["ignored_ids"], ["a"])
        self.assertEqual(self.client.post("/api/subscriptions/nope/ignore",
                                          json={"entry_ids": ["a"]}).status_code, 404)

    def test_没勾选任何内容拒绝(self):
        r = self.client.post("/api/track/run", json={"selections": [], "api_key": "k"})
        self.assertEqual(r.status_code, 400)

    def test_启动任务并轮询到完成(self):
        sub = self.add()
        fake_result = {"processed": 1, "failed": [], "stopped": False,
                       "brief_path": "/x.md", "brief_markdown": "# b", "brief_error": None}

        def fake_run(selections, **kw):
            kw["progress_cb"]({"log": "处理中", "current": 1, "total": 1})
            return fake_result

        with mock.patch.object(tracking, "run_batch", side_effect=fake_run) as run:
            r = self.client.post("/api/track/run", json={
                "selections": [{"sub_id": sub["id"], "entry_ids": ["e1", "e1"]}],
                "api_key": "k", "output_dir": self.root,
            })
            job_id = r.get_json()["job_id"]
            for _ in range(100):
                d = self.client.get(f"/api/track/status/{job_id}").get_json()
                if d["done"]:
                    break
                time.sleep(0.02)
        self.assertTrue(d["done"])
        self.assertEqual(d["result"], fake_result)
        self.assertIn("处理中", d["log"])
        selections = run.call_args[0][0]
        self.assertEqual(selections[0][1], ["e1"])  # 重复 id 去重
        key = os.path.normcase(os.path.realpath(sub["folder"]))
        self.assertNotIn(key, server.ACTIVE_OUTPUT_DIRS)

    def test_文件夹被占用时拒绝(self):
        sub = self.add()
        key = os.path.normcase(os.path.realpath(sub["folder"]))
        server.ACTIVE_OUTPUT_DIRS[key] = "other-job"
        try:
            r = self.client.post("/api/track/run", json={
                "selections": [{"sub_id": sub["id"], "entry_ids": ["e1"]}], "api_key": "k"})
        finally:
            server.ACTIVE_OUTPUT_DIRS.pop(key, None)
        self.assertEqual(r.status_code, 409)

    def test_状态查询不存在的任务返回404(self):
        self.assertEqual(self.client.get("/api/track/status/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
