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
