import tempfile
import time
import unittest
from unittest import mock

from apps.summit2md import server


class _FrozenThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class ServerRegressionTests(unittest.TestCase):
    def setUp(self):
        with server.JOBS_LOCK:
            server.JOBS.clear()
            server.ACTIVE_OUTPUT_DIRS.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        with server.JOBS_LOCK:
            server.JOBS.clear()
            server.ACTIVE_OUTPUT_DIRS.clear()

    def _payload(self, output_dir):
        return {
            "summit_title": "Same Summit",
            "source_url": "https://www.youtube.com/watch?v=test",
            "entries": [{
                "id": "test", "title": "Test", "duration": 1,
                "url": "https://www.youtube.com/watch?v=test",
            }],
            "output_dir": output_dir,
            "do_summary": False,
            "do_speaker_label": False,
            "do_speech_script": False,
            "backend": "cli",
        }

    def test_same_output_directory_rejects_second_active_job(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            server.threading, "Thread", _FrozenThread
        ):
            first = self.client.post("/api/run", json=self._payload(root))
            second = self.client.post("/api/run", json=self._payload(root))

        self.assertEqual(200, first.status_code)
        self.assertEqual(409, second.status_code)
        self.assertIn("已有任务正在运行", second.get_json()["error"])

    def test_completed_jobs_are_pruned(self):
        now = time.time()
        with server.JOBS_LOCK:
            server.JOBS["expired"] = {
                "done": True, "created_at": 0,
                "finished_at": now - server.JOB_RETENTION_SECONDS - 1,
            }
            server.JOBS["active"] = {"done": False, "created_at": 0}
            server._prune_jobs_locked(now)
        self.assertNotIn("expired", server.JOBS)
        self.assertIn("active", server.JOBS)

    def test_security_headers_are_present(self):
        response = self.client.get("/")
        try:
            self.assertEqual(200, response.status_code)
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()


class ExistingSummaryRouteTests(unittest.TestCase):
    """/api/existing_summary：重新发现流程用来决定要不要露出"沿用/重新生成"选择。"""

    def setUp(self):
        self.client = server.app.test_client()

    def _write_manifest(self, output_base_dir, summit_title, overall_summary):
        import os
        from apps.summit2md import pipeline
        out_dir = os.path.join(output_base_dir, pipeline.sanitize_filename(summit_title))
        os.makedirs(out_dir, exist_ok=True)
        pipeline._save_manifest(out_dir, {"entries": {}, "overall_summary": overall_summary})

    def test_没有标题时直接返回False_不报错(self):
        r = self.client.post("/api/existing_summary", json={"output_dir": "/tmp", "summit_title": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"has_overall_summary": False})

    def test_有真实旧总结时返回True(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_manifest(root, "老节目", "真实总结内容")
            r = self.client.post("/api/existing_summary",
                                 json={"output_dir": root, "summit_title": "老节目"})
            self.assertEqual(r.get_json(), {"has_overall_summary": True})

    def test_全新播放列表没有旧总结时返回False(self):
        with tempfile.TemporaryDirectory() as root:
            r = self.client.post("/api/existing_summary",
                                 json={"output_dir": root, "summit_title": "从没跑过的节目"})
            self.assertEqual(r.get_json(), {"has_overall_summary": False})

    def test_不给output_dir时退回默认值_不报错(self):
        r = self.client.post("/api/existing_summary", json={"summit_title": "随便什么标题"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("has_overall_summary", r.get_json())


class TopicEntriesAndCustomSummaryRouteTests(unittest.TestCase):
    """/api/topic_entries + /api/custom_topic_summary：手选议题生成聚焦总结。"""

    def setUp(self):
        self.client = server.app.test_client()

    def _seed(self, out_dir):
        from apps.summit2md import pipeline
        pipeline._save_manifest(out_dir, {"entries": {
            "vid1": {"rank": 1, "ok": True, "entry": {"title": "Talk A"}, "summary": {"tldr": "x"}},
            "vid2": {"rank": 2, "ok": True, "entry": {"title": "Talk B"}, "summary": {"tldr": "y"}},
        }})

    def test_topic_entries_列出目录里的全部议题(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            r = self.client.post("/api/topic_entries", json={"output_dir": out_dir})
            self.assertEqual(r.status_code, 200)
            ids = [e["id"] for e in r.get_json()["entries"]]
            self.assertEqual(ids, ["vid1", "vid2"])

    def test_topic_entries_缺输出目录时报错(self):
        r = self.client.post("/api/topic_entries", json={})
        self.assertEqual(r.status_code, 400)

    def test_topic_entries_目录不存在时报错(self):
        r = self.client.post("/api/topic_entries", json={"output_dir": "/不存在/的/目录"})
        self.assertEqual(r.status_code, 400)

    def test_custom_topic_summary_没勾选任何议题时报错(self):
        with tempfile.TemporaryDirectory() as out_dir:
            r = self.client.post("/api/custom_topic_summary", json={
                "output_dir": out_dir, "entry_ids": [], "backend": "api", "api_key": "k",
            })
            self.assertEqual(r.status_code, 400)
            self.assertIn("至少勾选一个议题", r.get_json()["error"])

    def test_custom_topic_summary_真正调用生成并返回结果(self):
        # 打桩在 pipeline._cached_summarize 这一层——跟 pipeline 层的测试用同一个
        # 桩点，不碰真正的模型调用。之前误写成打桩 pipeline.llm.complete（根本不存在
        # 这个属性），_resolve_llm_config 会退回读本机 ~/.spark/keys/anthropic.key，
        # 结果这条测试真的打了一次线上 API——用固定输出的桩把这类风险彻底堵死。
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch("apps.summit2md.pipeline._cached_summarize", return_value="生成的正文"):
                r = self.client.post("/api/custom_topic_summary", json={
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "手选的两个",
                    "backend": "api", "api_key": "k",
                })
            self.assertEqual(r.status_code, 200, r.get_json())
            d = r.get_json()
            self.assertEqual(d["count"], 2)
            self.assertIn("手选的两个", d["relative_path"])

    def test_custom_topic_summary_缺后端配置时报错而不是崩溃(self):
        # 用 openai_compatible 而不是 api：api 后端在 api_key 留空时会退回读本机
        # 的 key 文件/环境变量，在配好了本机 key 的机器上不会按预期报 400（之前
        # 就是这样意外打了一次真实 API）。openai_compatible 缺了 api_base/model
        # 时必定拒绝，不依赖这台机器有没有配置任何 key。
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            r = self.client.post("/api/custom_topic_summary", json={
                "output_dir": out_dir, "entry_ids": ["vid1"],
                "backend": "openai_compatible", "api_key": "", "api_base": "", "model": "",
            })
            self.assertEqual(r.status_code, 400)

    def test_custom_topic_summary_reuse为True且文件已存在时不调模型(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch("apps.summit2md.pipeline._cached_summarize", return_value="第一次的正文"):
                self.client.post("/api/custom_topic_summary", json={
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "老标签",
                    "backend": "api", "api_key": "k",
                })
            with mock.patch("apps.summit2md.pipeline._cached_summarize") as mocked:
                r = self.client.post("/api/custom_topic_summary", json={
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "老标签",
                    "backend": "api", "api_key": "k", "reuse": True,
                })
            self.assertEqual(r.status_code, 200, r.get_json())
            mocked.assert_not_called()
            self.assertIn("第一次的正文", r.get_json()["content"])


class TopicSummaryExistsRouteTests(unittest.TestCase):
    """/api/topic_summary_exists：给"沿用/重新生成"这个选择判断要不要露出来。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_没有标签时返回False_不报错(self):
        r = self.client.post("/api/topic_summary_exists", json={"output_dir": "/tmp", "label": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"exists": False})

    def test_文件已存在时返回True(self):
        import os
        with tempfile.TemporaryDirectory() as out_dir:
            topics_dir = os.path.join(out_dir, "topics")
            os.makedirs(topics_dir, exist_ok=True)
            with open(os.path.join(topics_dir, "老标签.md"), "w", encoding="utf-8") as f:
                f.write("正文")
            r = self.client.post("/api/topic_summary_exists", json={"output_dir": out_dir, "label": "老标签"})
            self.assertEqual(r.get_json(), {"exists": True})

    def test_目录不存在时返回False_不报错(self):
        r = self.client.post("/api/topic_summary_exists", json={"output_dir": "/这个路径/不存在", "label": "随便"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"exists": False})


class BrowseDirRouteTests(unittest.TestCase):
    """/api/browse_dir：给"导入目录""输出目录"这类路径输入框做自动补全。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_列出匹配前缀的子目录(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "All-In Podcast"))
            os.makedirs(os.path.join(root, "Another Show"))
            r = self.client.post("/api/browse_dir", json={"path": os.path.join(root, "All")})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["entries"], [os.path.join(root, "All-In Podcast")])

    def test_路径不存在时返回空列表而不是报错(self):
        r = self.client.post("/api/browse_dir", json={"path": "/这个路径/不存在/xyz"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"entries": []})

    def test_缺path字段时不崩溃(self):
        r = self.client.post("/api/browse_dir", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("entries", r.get_json())


class DirPlausibleRouteTests(unittest.TestCase):
    """/api/dir_plausible：给"记住最近用过的目录"做把关，别把打错的路径也记下来。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_真实存在的目录返回True(self):
        with tempfile.TemporaryDirectory() as root:
            r = self.client.post("/api/dir_plausible", json={"path": root})
            self.assertEqual(r.get_json(), {"plausible": True})

    def test_上级目录都不存在时返回False(self):
        r = self.client.post("/api/dir_plausible", json={"path": "/这个路径/不存在/xyz"})
        self.assertEqual(r.get_json(), {"plausible": False})
