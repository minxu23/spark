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
