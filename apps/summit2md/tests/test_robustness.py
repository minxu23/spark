"""数据安全与"停止"：manifest/缓存写坏、并发写、停止信号、文章来源不补演讲稿。"""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from apps.summit2md import pipeline, server
from core import atomic, sources


def _rss_entry(eid="e1"):
    return {"id": eid, "title": "一篇文章", "url": "https://x/1", "source_type": "rss",
            "duration": 0, "rss_content_html": "<p>" + "正文内容。" * 60 + "</p>"}


def _run(root, entries, **kw):
    args = dict(summit_title="测试", source_url="u", entries=entries, output_base_dir=root,
                backend="api", api_key="k", model="m", lang_prefs=["en"], do_summary=True,
                content_type="series")
    args.update(kw)
    return pipeline.process_job(**args)


class ManifestTests(unittest.TestCase):
    def test_没有文件是空记录_文件坏了要报错而不是当成空(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pipeline._load_manifest(d), {"entries": {}})
            with open(os.path.join(d, ".manifest.json"), "w") as f:
                f.write('{"entries": {"a": ')
            with self.assertRaises(pipeline.ManifestCorrupt):
                pipeline._load_manifest(d)
            with open(os.path.join(d, ".manifest.json"), "w") as f:
                f.write('["not", "a", "manifest"]')
            with self.assertRaises(pipeline.ManifestCorrupt):
                pipeline._load_manifest(d)

    def test_并发保存不会留下半截文件或临时文件(self):
        with tempfile.TemporaryDirectory() as d:
            def writer(n):
                for i in range(30):
                    pipeline._save_manifest(d, {"entries": {f"w{n}-{i}": {"ok": True}}})
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertIsInstance(pipeline._load_manifest(d)["entries"], dict)
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])

    def test_坏掉的manifest让生成任务直接报错(self):
        with tempfile.TemporaryDirectory() as root:
            out = os.path.join(root, "测试")
            os.makedirs(out)
            with open(os.path.join(out, ".manifest.json"), "w") as f:
                f.write("{broken")
            with self.assertRaises(pipeline.ManifestCorrupt):
                _run(root, [_rss_entry()])


class CacheTests(unittest.TestCase):
    def test_缓存写坏了当没缓存_重新抓并覆盖(self):
        entry = _rss_entry()
        with tempfile.TemporaryDirectory() as cache_dir:
            path = os.path.join(cache_dir, "e1.rss.json")
            with open(path, "w") as f:
                f.write('{"paragraphs": [[0, "半')
            got = sources.fetch_source_text(entry, cache_dir)
            self.assertTrue(got["paragraphs"])
            with open(path, encoding="utf-8") as f:
                self.assertTrue(json.load(f)["paragraphs"])

    def test_原子写失败时不留下临时文件(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TypeError):
                atomic.write_json(os.path.join(d, "x.json"), {"bad": object()})
            self.assertEqual(os.listdir(d), [])


class StopTests(unittest.TestCase):
    def test_小结调用中途停止_算停止而不是摘要失败(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(pipeline, "_cached_summarize", side_effect=pipeline.Stopped("stop")) as llm:
                result = _run(root, [_rss_entry("e1"), _rss_entry("e2")])
            self.assertTrue(result["stopped"])
            self.assertEqual(llm.call_count, 1)
            manifest = pipeline._load_manifest(os.path.join(root, "测试"))["entries"]
            self.assertNotIn("e1", manifest)  # 停在半路的那条不记成失败

    def test_停止信号会传给模型调用(self):
        with tempfile.TemporaryDirectory() as root:
            flag = lambda: False  # noqa: E731
            with mock.patch.object(pipeline, "_cached_summarize", return_value="TLDR: x\n- y") as llm:
                _run(root, [_rss_entry()], stop_flag=flag)
            self.assertTrue(all(c.kwargs.get("stop_flag") is flag for c in llm.call_args_list))


class TextSourceSpeechTests(unittest.TestCase):
    def test_文章来源勾了演讲稿也不会每次都去补(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(pipeline, "_cached_summarize", return_value="TLDR: x\n- y"):
                _run(root, [_rss_entry()], do_speech_script=True)
            with mock.patch.object(pipeline, "generate_speech_script",
                                   side_effect=AssertionError("文章不该生成演讲稿")), \
                 mock.patch.object(pipeline, "_cached_summarize",
                                   side_effect=AssertionError("已完成的条目不该再调模型")):
                result = _run(root, [_rss_entry()], do_speech_script=True, skip_existing=True,
                              regenerate_summary=False)
            self.assertFalse(result["stopped"])


class SubstackTests(unittest.TestCase):
    def test_archive返回的不是列表时按不是substack处理(self):
        with mock.patch.object(pipeline, "_substack_api_get", return_value={"error": "nope"}):
            with self.assertRaises(RuntimeError):
                pipeline._fetch_substack_archive_podcasts("example.com")


class BusyDirTests(unittest.TestCase):
    def test_目录正在生成时主题总结接口返回409(self):
        with tempfile.TemporaryDirectory() as d:
            key = os.path.normcase(os.path.realpath(d))
            server.ACTIVE_OUTPUT_DIRS[key] = "job"
            try:
                client = server.app.test_client()
                r = client.post("/api/topic_groups", json={"output_dir": d})
                self.assertEqual(r.status_code, 409)
                r = client.post("/api/custom_topic_summary", json={
                    "output_dir": d, "entry_ids": ["a"], "backend": "api", "api_key": "k"})
                self.assertEqual(r.status_code, 409)
            finally:
                server.ACTIVE_OUTPUT_DIRS.pop(key, None)


class BackfillTests(unittest.TestCase):
    """文字记录已有、上次小结失败：下次运行只补小结，不重新抓正文。"""

    def _first_run_with_failed_summary(self, root):
        with mock.patch.object(pipeline, "_cached_summarize", side_effect=pipeline.SummarizeError("额度不足")):
            _run(root, [_rss_entry()])
        rec = pipeline._load_manifest(os.path.join(root, "测试"))["entries"]["e1"]
        self.assertTrue(rec["ok"])
        self.assertIn("摘要生成失败", rec["summary"]["body"])

    def test_上次小结失败_这次只补小结(self):
        with tempfile.TemporaryDirectory() as root:
            self._first_run_with_failed_summary(root)
            with mock.patch.object(sources, "fetch_source_text",
                                   side_effect=AssertionError("补小结不该重新抓正文")), \
                 mock.patch.object(pipeline, "_cached_summarize", return_value="TLDR: 补上的结论\n- 要点"):
                result = _run(root, [_rss_entry()])
            self.assertFalse(result["stopped"])
            out = os.path.join(root, "测试")
            rec = pipeline._load_manifest(out)["entries"]["e1"]
            self.assertEqual(rec["summary"]["tldr"], "补上的结论")
            with open(os.path.join(out, rec["relative_path"]), encoding="utf-8") as f:
                self.assertIn("补上的结论", f.read())

    def test_补小结时停止_算停止(self):
        with tempfile.TemporaryDirectory() as root:
            self._first_run_with_failed_summary(root)
            with mock.patch.object(pipeline, "_cached_summarize", side_effect=pipeline.Stopped("stop")):
                result = _run(root, [_rss_entry()])
            self.assertTrue(result["stopped"])

    def test_概览统计和实际处理用同一个判断(self):
        with tempfile.TemporaryDirectory() as root:
            self._first_run_with_failed_summary(root)
            logs = []
            with mock.patch.object(pipeline, "_cached_summarize", return_value="TLDR: x\n- y"):
                _run(root, [_rss_entry("e1"), _rss_entry("e2")], progress_cb=logs.append)
            overview = next(e["log"] for e in logs if e.get("stage") == "overview")
            self.assertIn("1 个只补缺失部分", overview)
            self.assertIn("1 个全新处理", overview)


if __name__ == "__main__":
    unittest.main()
