"""/api/deck 的"停止"：生成演示本质是一次模型调用，没有自然的暂停点，只给停止。"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from apps.notes2insight import llm, server


class DeckStopTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def _write_report(self, out_dir):
        path = os.path.join(out_dir, "报告.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 标题\n\n## 结论\n正文\n")
        return path

    def _run_and_wait(self, payload, timeout=5):
        r = self.client.post("/api/deck", json=payload)
        self.assertEqual(r.status_code, 200, r.get_json())
        job_id = r.get_json()["job_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            sr = self.client.get(f"/api/progress/{job_id}")
            status = sr.get_json()
            if status["done"]:
                return job_id, status
            time.sleep(0.02)
        raise AssertionError("任务在测试超时前没有跑完")

    def test_正常生成时不受影响(self):
        with tempfile.TemporaryDirectory() as out_dir:
            report = self._write_report(out_dir)
            fake_html, fake_deck = "<html>ok</html>", {"title": "T", "slides": [], "sources": []}
            with mock.patch("apps.notes2insight.server.deck.generate", return_value=(fake_html, fake_deck)):
                _job_id, status = self._run_and_wait({
                    "path": report, "output_dir": out_dir, "root": out_dir, "backend": "cli",
                })
            self.assertTrue(status["ok"])
            self.assertFalse(status.get("stopped"))

    def test_点了停止后任务标记为已停止_不算成失败(self):
        with tempfile.TemporaryDirectory() as out_dir:
            report = self._write_report(out_dir)

            def fake_generate(*a, **k):
                raise llm.Stopped("已停止")

            with mock.patch("apps.notes2insight.server.deck.generate", side_effect=fake_generate):
                _job_id, status = self._run_and_wait({
                    "path": report, "output_dir": out_dir, "root": out_dir, "backend": "cli",
                })
            self.assertFalse(status["ok"])
            self.assertTrue(status.get("stopped"))
            self.assertEqual(status.get("error"), "")

    def test_stop路由会把stop_requested设为True并传进stop_flag(self):
        with tempfile.TemporaryDirectory() as out_dir:
            report = self._write_report(out_dir)
            release = threading.Event()
            seen = {}

            def fake_generate(*a, stop_flag=None, **k):
                seen["flag"] = stop_flag
                release.wait(timeout=5)
                return "<html>ok</html>", {"title": "T", "slides": [], "sources": []}

            with mock.patch("apps.notes2insight.server.deck.generate", side_effect=fake_generate):
                r = self.client.post("/api/deck", json={
                    "path": report, "output_dir": out_dir, "root": out_dir, "backend": "cli",
                })
                job_id = r.get_json()["job_id"]
                for _ in range(100):
                    if "flag" in seen:
                        break
                    time.sleep(0.02)
                sr = self.client.post(f"/api/stop/{job_id}")
                self.assertEqual(sr.get_json(), {"ok": True})
                self.assertTrue(seen["flag"]())
                release.set()
                # 等后台线程真正结束（它还会往 out_dir 写 deck 文件）再离开临时目录，
                # 不然 TemporaryDirectory 清理时目录里又冒出新文件，偶发 "Directory not empty"
                for _ in range(250):
                    if self.client.get(f"/api/progress/{job_id}").get_json()["done"]:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("停止后任务没有结束")

    def test_stop对不存在的任务返回404(self):
        r = self.client.post("/api/stop/不存在的id")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
