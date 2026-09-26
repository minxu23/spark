"""重任务名额：会议/节目批量处理（/api/run）和信息跟进（/api/track/run）共用一份
名额，满了直接 429；暂停中的任务照样占着名额；跑完、出错、停止后都归还。
还有单个任务的条数上限。"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline, server, subscriptions_store, tracking
from core import jobs as jobs_util


def _entry(eid):
    return {"id": eid, "title": eid, "url": f"https://x/{eid}", "source_type": "rss", "duration": 0}


def _payload(root, title, entries=None):
    return {"summit_title": title, "output_dir": root, "entries": entries or [_entry("e1")],
            "content_type": "series", "backend": "api", "api_key": "k", "do_summary": True}


class HeavyJobSlotTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.slots = jobs_util.JobSlots(2)
        p = mock.patch.object(server, "HEAVY_JOBS", self.slots)
        p.start()
        self.addCleanup(p.stop)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.flags = {}

        def fake_process_job(**kw):
            self.flags[kw["summit_title"]] = kw
            # 像真的 pipeline 一样：暂停时原地等，停止时尽快退出
            while not self.gate.wait(0.01):
                if kw["stop_flag"]():
                    break
            return {"output_dir": "", "index_path": "", "rows": [], "stopped": kw["stop_flag"](),
                    "failed_entries": [], "unprocessed_entries": []}

        p2 = mock.patch.object(pipeline, "process_job", side_effect=fake_process_job)
        p2.start()
        self.addCleanup(p2.stop)

    def wait_until(self, pred, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.01)
        self.fail("超时")

    def test_满了就拒绝_暂停中的也占名额_停止后归还(self):
        a = self.client.post("/api/run", json=_payload(self.root, "节目A")).get_json()["job_id"]
        b = self.client.post("/api/run", json=_payload(self.root, "节目B")).get_json()["job_id"]
        self.assertEqual(self.client.post(f"/api/pause/{a}").get_json(), {"ok": True})

        for title in ("节目C", "节目D", "节目E"):
            r = self.client.post("/api/run", json=_payload(self.root, title))
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r.get_json()["error"], "已经有 2 个任务在跑，等其中一个结束再开始")
        # 被拒绝的不留任务、不占目录
        with server.JOBS_LOCK:
            self.assertEqual({jid for jid, j in server.JOBS.items() if not j["done"]}, {a, b})
        self.assertIsNone(server._active_job_for_dir(os.path.join(self.root, "节目C")))
        # 同目录冲突仍然报更具体的 409
        self.assertEqual(self.client.post("/api/run", json=_payload(self.root, "节目A")).status_code, 409)

        self.client.post(f"/api/stop/{a}")   # 停止一个暂停中的任务
        self.wait_until(lambda: self.slots.running == 1)
        self.assertTrue(self.client.get(f"/api/status/{a}").get_json()["done"])
        self.assertEqual(self.client.post("/api/run", json=_payload(self.root, "节目C")).status_code, 200)

        self.gate.set()
        self.wait_until(lambda: self.slots.running == 0)

    def test_出错后归还名额(self):
        with mock.patch.object(pipeline, "process_job", side_effect=RuntimeError("磁盘满了")):
            for i in range(4):
                jid = self.client.post("/api/run", json=_payload(self.root, f"节目{i}")).get_json()["job_id"]
                self.wait_until(lambda: self.slots.running == 0)
                self.assertIn("磁盘满了", self.client.get(f"/api/status/{jid}").get_json()["error"])

    def test_建日志失败时归还名额(self):
        with mock.patch.object(server.os, "makedirs", side_effect=OSError("没权限")):
            r = self.client.post("/api/run", json=_payload(self.root, "节目A"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.slots.running, 0)

    def test_信息跟进和批量处理共用名额(self):
        store_path = os.path.join(self.root, "subs.json")
        with mock.patch.object(subscriptions_store, "STORE_PATH", store_path):
            sub = subscriptions_store.add(url="https://x/feed", name="某博客", category="测试",
                                          output_dir=self.root, source_type="rss")
            body = {"selections": [{"sub_id": sub["id"], "entry_ids": ["e1"]}], "api_key": "k",
                    "output_dir": self.root}

            def slow_batch(selections, **kw):
                while not self.gate.wait(0.01):
                    if kw["stop_flag"]():
                        break
                return {"processed": 0, "failed": [], "stopped": True,
                        "brief_path": None, "brief_markdown": None, "brief_error": None}

            with mock.patch.object(tracking, "run_batch", side_effect=slow_batch):
                self.client.post("/api/run", json=_payload(self.root, "节目A"))
                track_id = self.client.post("/api/track/run", json=body).get_json()["job_id"]
                self.assertEqual(self.slots.running, 2)
                r = self.client.post("/api/run", json=_payload(self.root, "节目B"))
                self.assertEqual(r.status_code, 429)

                self.client.post(f"/api/track/stop/{track_id}")
                self.wait_until(lambda: self.slots.running == 1)
                # 占过的订阅文件夹也放掉了
                key = os.path.normcase(os.path.realpath(sub["folder"]))
                self.assertNotIn(key, server.ACTIVE_OUTPUT_DIRS)
                self.assertEqual(self.client.post("/api/track/run", json=body).status_code, 200)
                self.gate.set()
                self.wait_until(lambda: self.slots.running == 0)


class PerJobCapTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_议题太多直接拒绝(self):
        entries = [_entry(f"e{i}") for i in range(4)]
        with mock.patch.object(server, "MAX_ENTRIES_PER_JOB", 3):
            r = self.client.post("/api/run", json=_payload(self.root, "大会", entries))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "一次最多处理 3 条，当前 4 条，分几批来")

    def test_信息跟进勾太多直接拒绝(self):
        store_path = os.path.join(self.root, "subs.json")
        with mock.patch.object(subscriptions_store, "STORE_PATH", store_path), \
                mock.patch.object(server, "MAX_TRACK_ITEMS_PER_JOB", 2):
            sub = subscriptions_store.add(url="https://x/feed", name="某博客", category="测试",
                                          output_dir=self.root, source_type="rss")
            r = self.client.post("/api/track/run", json={
                "selections": [{"sub_id": sub["id"], "entry_ids": ["a", "b", "c"]}], "api_key": "k"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "一次最多处理 2 条，当前勾了 3 条，分几批来")
