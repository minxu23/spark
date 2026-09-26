"""重任务名额：生成报告、检索、导入链接同时最多跑 MAX_RUNNING_JOBS 个，满了直接拒绝。
名额要在任务跑完、出错、被停止后都还回来。pipeline 用假的替换，不调模型。"""

import threading
import time
import unittest
from unittest import mock

from apps.notes2insight import llm, pipeline, search, server
from core import jobs as jobs_util


def _payload(**kw):
    data = {"notes": ["a.md"], "root": "/tmp", "backend": "api", "api_key": "k"}
    data.update(kw)
    return data


class HeavyJobSlotTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        # 换一个独立的名额表，免得和别的测试里没结束的线程互相影响
        self.slots = jobs_util.JobSlots(2)
        p = mock.patch.object(server, "HEAVY_JOBS", self.slots)
        p.start()
        self.addCleanup(p.stop)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)

    def wait_until(self, pred, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.01)
        self.fail("超时")

    def blocking_run(self, cfg, progress):
        # 像真的 pipeline 一样在等待中检查停止
        while not self.gate.wait(0.01):
            if cfg.stop_flag():
                raise llm.Stopped()
        return {"path": "/x.md", "content": "", "filename": "x.md"}

    def test_超过上限的都被拒绝_名额在跑完后归还(self):
        with mock.patch.object(pipeline, "run", side_effect=self.blocking_run):
            ids = [self.client.post("/api/run", json=_payload()).get_json()["job_id"] for _ in range(2)]
            for _ in range(5):
                r = self.client.post("/api/run", json=_payload())
                self.assertEqual(r.status_code, 429)
                self.assertEqual(r.get_json()["error"], "已经有 2 个任务在跑，等其中一个结束再开始")
            # 检索、导入链接也占同一份名额
            r = self.client.post("/api/search", json={"topic": "x", "backend": "api", "api_key": "k"})
            self.assertEqual(r.status_code, 429)
            r = self.client.post("/api/import_links", json={"text": "https://example.com/a"})
            self.assertEqual(r.status_code, 429)
            self.assertEqual(self.slots.running, 2)
            # 被拒绝的请求不留任务记录
            with server.JOBS_LOCK:
                running = [jid for jid, j in server.JOBS.items() if not j["done"]]
            self.assertEqual(sorted(running), sorted(ids))

            self.gate.set()
            self.wait_until(lambda: self.slots.running == 0)
            r = self.client.post("/api/run", json=_payload())
            self.assertEqual(r.status_code, 200)
            self.wait_until(lambda: self.slots.running == 0)

    def test_停止后归还名额(self):
        with mock.patch.object(pipeline, "run", side_effect=self.blocking_run):
            job_id = self.client.post("/api/run", json=_payload()).get_json()["job_id"]
            self.client.post("/api/run", json=_payload())
            self.assertEqual(self.client.post("/api/run", json=_payload()).status_code, 429)
            self.client.post(f"/api/stop/{job_id}")
            self.wait_until(lambda: self.slots.running == 1)
            d = self.client.get(f"/api/progress/{job_id}").get_json()
            self.assertTrue(d["done"] and d["stopped"])
            self.assertEqual(self.client.post("/api/run", json=_payload()).status_code, 200)

    def test_出错后归还名额(self):
        with mock.patch.object(pipeline, "run", side_effect=RuntimeError("坏了")), \
                mock.patch.object(search, "find", side_effect=RuntimeError("也坏了")):
            for _ in range(3):
                job_id = self.client.post("/api/run", json=_payload()).get_json()["job_id"]
                self.wait_until(lambda: self.slots.running == 0)
                self.assertIn("坏了", self.client.get(f"/api/progress/{job_id}").get_json()["error"])
                r = self.client.post("/api/search", json={"topic": "x", "backend": "api", "api_key": "k"})
                self.assertEqual(r.status_code, 200)
                self.wait_until(lambda: self.slots.running == 0)
