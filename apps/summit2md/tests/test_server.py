import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline, server


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

    def _run_and_wait(self, path, payload, timeout=5):
        """主题总结这类路由现在是"提交任务、轮询状态"的异步写法（好让前端能点
        停止），不再是发一次请求就直接拿到结果——测试也要跟着轮，不能假设
        POST 一回来就已经跑完了。"""
        r = self.client.post(path, json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        job_id = r.get_json()["job_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            sr = self.client.get(f"/api/simple_job_status/{job_id}")
            status = sr.get_json()
            if status["done"]:
                return job_id, status
            time.sleep(0.02)
        raise AssertionError("任务在测试超时前没有跑完")

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
                _job_id, status = self._run_and_wait("/api/custom_topic_summary", {
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "手选的两个",
                    "backend": "api", "api_key": "k",
                })
            self.assertIsNone(status["error"])
            d = status["result"]
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
                self._run_and_wait("/api/custom_topic_summary", {
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "老标签",
                    "backend": "api", "api_key": "k",
                })
            with mock.patch("apps.summit2md.pipeline._cached_summarize") as mocked:
                _job_id, status = self._run_and_wait("/api/custom_topic_summary", {
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "老标签",
                    "backend": "api", "api_key": "k", "reuse": True,
                })
            mocked.assert_not_called()
            self.assertIn("第一次的正文", status["result"]["content"])

    def test_custom_topic_summary_点了停止后任务标记为已停止_不写文件(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)

            def fake_stopped(*a, **k):
                raise pipeline.Stopped("已停止")

            with mock.patch("apps.summit2md.pipeline._cached_summarize", side_effect=fake_stopped):
                _job_id, status = self._run_and_wait("/api/custom_topic_summary", {
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "会被停止的标签",
                    "backend": "api", "api_key": "k",
                })
            self.assertTrue(status["stopped"])
            self.assertIsNone(status["error"])
            self.assertIsNone(status["result"])
            self.assertFalse(os.path.isfile(os.path.join(out_dir, "topics", "会被停止的标签.md")))

    def test_simple_job_stop_设置了stop_requested标志(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            release = threading.Event()
            seen_stop_flag = {}

            def fake_summarize(*a, stop_flag=None, **k):
                seen_stop_flag["flag"] = stop_flag
                release.wait(timeout=5)
                return "正文"

            with mock.patch("apps.summit2md.pipeline._cached_summarize", side_effect=fake_summarize):
                r = self.client.post("/api/custom_topic_summary", json={
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1", "vid2"], "label": "还没跑完就点停止",
                    "backend": "api", "api_key": "k",
                })
                job_id = r.get_json()["job_id"]
                for _ in range(100):
                    if "flag" in seen_stop_flag:
                        break
                    time.sleep(0.02)
                sr = self.client.post(f"/api/simple_job_stop/{job_id}")
                self.assertEqual(sr.get_json(), {"ok": True})
                self.assertTrue(seen_stop_flag["flag"]())
                release.set()

    def test_simple_job_stop对不存在的任务返回404(self):
        r = self.client.post("/api/simple_job_stop/不存在的id")
        self.assertEqual(r.status_code, 404)

    def test_simple_job_status对不存在的任务返回404(self):
        r = self.client.get("/api/simple_job_status/不存在的id")
        self.assertEqual(r.status_code, 404)


class TopicSummaryRouteTests(unittest.TestCase):
    """/api/topic_summary：按自动分出的主题名字生成聚焦总结，跟手选议题那条路
    共用同一套任务壳子（/api/simple_job_status、/api/simple_job_stop）。"""

    def setUp(self):
        self.client = server.app.test_client()

    def _seed_with_topic_group(self, out_dir):
        pipeline._save_manifest(out_dir, {
            "entries": {
                "vid1": {"rank": 1, "ok": True, "entry": {"title": "Talk A"}, "summary": {"tldr": "x"}},
                "vid2": {"rank": 2, "ok": True, "entry": {"title": "Talk B"}, "summary": {"tldr": "y"}},
            },
            "topic_groups": {"AI 安全": ["vid1", "vid2"]},
        })

    def _run_and_wait(self, payload, timeout=5):
        r = self.client.post("/api/topic_summary", json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        job_id = r.get_json()["job_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.client.get(f"/api/simple_job_status/{job_id}").get_json()
            if status["done"]:
                return status
            time.sleep(0.02)
        raise AssertionError("任务在测试超时前没有跑完")

    def test_没有选主题时报错(self):
        with tempfile.TemporaryDirectory() as out_dir:
            r = self.client.post("/api/topic_summary", json={
                "output_dir": out_dir, "themes": [], "backend": "api", "api_key": "k",
            })
            self.assertEqual(r.status_code, 400)
            self.assertIn("至少选择一个主题", r.get_json()["error"])

    def test_真正调用生成并返回结果(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed_with_topic_group(out_dir)
            with mock.patch("apps.summit2md.pipeline._cached_summarize", return_value="生成的正文"):
                status = self._run_and_wait({
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "themes": ["AI 安全"], "backend": "api", "api_key": "k",
                })
            self.assertIsNone(status["error"])
            self.assertEqual(status["result"]["count"], 2)
            self.assertEqual(status["result"]["relative_path"], os.path.join("topics", "AI 安全.md"))

    def test_点了停止后任务标记为已停止(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed_with_topic_group(out_dir)

            def fake_stopped(*a, **k):
                raise pipeline.Stopped("已停止")

            with mock.patch("apps.summit2md.pipeline._cached_summarize", side_effect=fake_stopped):
                status = self._run_and_wait({
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "themes": ["AI 安全"], "backend": "api", "api_key": "k",
                })
            self.assertTrue(status["stopped"])
            self.assertIsNone(status["error"])
            self.assertIsNone(status["result"])


class TopicSummaryExistsRouteTests(unittest.TestCase):
    """/api/topic_summary_exists：给"沿用/重新生成"这个选择判断要不要露出来。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_没有标签也没有entry_ids时返回False_不报错(self):
        r = self.client.post("/api/topic_summary_exists", json={"output_dir": "/tmp", "label": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"exists": False, "label": ""})

    def test_文件已存在时返回True(self):
        import os
        with tempfile.TemporaryDirectory() as out_dir:
            topics_dir = os.path.join(out_dir, "topics")
            os.makedirs(topics_dir, exist_ok=True)
            with open(os.path.join(topics_dir, "老标签.md"), "w", encoding="utf-8") as f:
                f.write("正文")
            r = self.client.post("/api/topic_summary_exists", json={"output_dir": out_dir, "label": "老标签"})
            self.assertEqual(r.get_json(), {"exists": True, "label": "老标签"})

    def test_目录不存在时返回False_不报错(self):
        r = self.client.post("/api/topic_summary_exists", json={"output_dir": "/这个路径/不存在", "label": "随便"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"exists": False, "label": "随便"})

    def test_标题留空但带entry_ids且有记录时能查到(self):
        with tempfile.TemporaryDirectory() as out_dir:
            pipeline._save_manifest(out_dir, {"entries": {
                "vid1": {"rank": 1, "ok": True, "entry": {"title": "A"}, "summary": {"tldr": "x"}},
            }})
            with mock.patch(
                "apps.summit2md.pipeline._cached_summarize",
                return_value="标题：自动概括的标题\n\n正文",
            ):
                r = self.client.post("/api/custom_topic_summary", json={
                    "output_dir": out_dir, "summit_title": "测试大会", "content_type": "summit",
                    "entry_ids": ["vid1"], "label": "", "backend": "api", "api_key": "k",
                })
                job_id = r.get_json()["job_id"]
                deadline = time.time() + 5
                while time.time() < deadline:
                    if self.client.get(f"/api/simple_job_status/{job_id}").get_json()["done"]:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError("任务在测试超时前没有跑完")
            r = self.client.post("/api/topic_summary_exists", json={
                "output_dir": out_dir, "label": "", "entry_ids": ["vid1"],
            })
            self.assertEqual(r.get_json(), {"exists": True, "label": "自动概括的标题"})

    def test_标题留空且没有entry_ids对应记录时返回False(self):
        import os
        with tempfile.TemporaryDirectory() as out_dir:
            r = self.client.post("/api/topic_summary_exists", json={
                "output_dir": out_dir, "label": "", "entry_ids": ["vid1", "vid2"],
            })
            self.assertEqual(r.get_json(), {"exists": False, "label": ""})


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
