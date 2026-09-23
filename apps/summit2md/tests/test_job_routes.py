"""/api/run 这一整套任务路由：启动、进度、暂停/继续/停止、移除、同目录互斥、导入已有目录。"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline, server


def _entry(eid, title):
    return {"id": eid, "title": title, "url": f"https://x/{eid}", "source_type": "rss", "duration": 0,
            "rss_content_html": "<p>" + f"{title}的正文内容。" * 40 + "</p>"}


def _payload(out_root, entries, **kw):
    data = {"summit_title": "测试节目", "output_dir": out_root, "entries": entries,
            "content_type": "series", "backend": "api", "api_key": "k", "do_summary": True}
    data.update(kw)
    return data


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def wait_done(self, job_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.client.get(f"/api/status/{job_id}").get_json()
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("任务在超时前没有结束")


class RealRunTests(_Base):
    """真的跑一遍 pipeline.process_job，只把模型调用换掉。"""

    def test_启动_跑完_看总结_列表里有_移除后没了(self):
        with mock.patch.object(pipeline, "_cached_summarize", return_value="TLDR: 结论\n- 要点"):
            r = self.client.post("/api/run", json=_payload(self.root, [_entry("e1", "第一期"), _entry("e2", "第二期")]))
            self.assertEqual(r.status_code, 200, r.get_json())
            job_id = r.get_json()["job_id"]
            d = self.wait_done(job_id)

        self.assertIsNone(d["error"])
        self.assertEqual(sum(1 for row in d["result"]["rows"] if row["ok"]), 2)
        self.assertTrue(os.path.exists(d["log_file"]))
        with open(d["log_file"], encoding="utf-8") as f:
            self.assertIn("全部完成", f.read())

        readme = self.client.get(f"/api/readme/{job_id}").get_json()
        self.assertIn("第一期", readme["content"])

        listed = [j["job_id"] for j in self.client.get("/api/jobs").get_json()["jobs"]]
        self.assertIn(job_id, listed)
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}").get_json(), {"ok": True})
        listed = [j["job_id"] for j in self.client.get("/api/jobs").get_json()["jobs"]]
        self.assertNotIn(job_id, listed)
        self.assertEqual(self.client.get(f"/api/status/{job_id}").status_code, 404)

        # 跑完的目录可以直接导入回来，议题列表跟刚才一致
        imported = self.client.post("/api/import_dir", json={"path": d["result"]["output_dir"]}).get_json()
        self.assertEqual({e["id"] for e in imported["entries"]}, {"e1", "e2"})
        self.assertEqual(imported["summit_title"], "测试节目")

    def test_任务出错时状态里有错误_目录释放(self):
        with mock.patch.object(pipeline, "process_job", side_effect=RuntimeError("磁盘满了")):
            job_id = self.client.post("/api/run", json=_payload(self.root, [_entry("e1", "一")])).get_json()["job_id"]
            d = self.wait_done(job_id)
        self.assertIn("磁盘满了", d["error"])
        self.assertEqual(server._active_job_for_dir(os.path.join(self.root, "测试节目")), None)


class RunningJobTests(_Base):
    """用一个会卡住的假 process_job，测任务还在跑时各个控制接口的行为。"""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.seen = {}

        def fake_process_job(**kw):
            self.seen.update(kw)
            self.release.wait(timeout=5)
            return {"output_dir": os.path.join(self.root, "测试节目"), "index_path": "", "rows": [],
                    "stopped": kw["stop_flag"](), "failed_entries": [], "unprocessed_entries": []}

        p = mock.patch.object(pipeline, "process_job", side_effect=fake_process_job)
        p.start()
        self.addCleanup(p.stop)
        r = self.client.post("/api/run", json=_payload(self.root, [_entry("e1", "一")]))
        self.job_id = r.get_json()["job_id"]
        for _ in range(250):
            if "stop_flag" in self.seen:
                break
            time.sleep(0.02)

    def tearDown(self):
        self.release.set()
        self.wait_done(self.job_id)
        super().tearDown()

    def test_暂停_继续_停止(self):
        jid = self.job_id
        self.assertEqual(self.client.post(f"/api/pause/{jid}").get_json(), {"ok": True})
        self.assertTrue(self.seen["pause_flag"]())
        self.assertTrue(self.client.get(f"/api/status/{jid}").get_json()["paused"])
        self.client.post(f"/api/resume/{jid}")
        self.assertFalse(self.seen["pause_flag"]())

        self.client.post(f"/api/pause/{jid}")
        self.client.post(f"/api/stop/{jid}")
        self.assertTrue(self.seen["stop_flag"]())
        self.assertFalse(self.seen["pause_flag"](), "停止要能打断暂停中的等待")

        self.release.set()
        d = self.wait_done(jid)
        self.assertTrue(d["result"]["stopped"])
        self.assertEqual(self.client.post(f"/api/pause/{jid}").status_code, 400)  # 结束了不能再暂停

    def test_运行中不能移除_同目录不能再开任务或导入(self):
        self.assertEqual(self.client.delete(f"/api/jobs/{self.job_id}").status_code, 400)
        r = self.client.post("/api/run", json=_payload(self.root, [_entry("e2", "二")]))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["active_job_id"], self.job_id)
        r = self.client.post("/api/import_dir", json={"path": os.path.join(self.root, "测试节目")})
        self.assertEqual(r.status_code, 409)

    def test_参数原样传给pipeline(self):
        self.assertEqual(self.seen["summit_title"], "测试节目")
        self.assertEqual(self.seen["content_type"], "series")
        self.assertEqual([e["id"] for e in self.seen["entries"]], ["e1"])


class UnknownJobTests(_Base):
    def test_不存在的任务(self):
        for method, url in (("get", "/api/status/nope"), ("post", "/api/stop/nope"),
                            ("post", "/api/pause/nope"), ("post", "/api/resume/nope"),
                            ("get", "/api/readme/nope")):
            self.assertEqual(getattr(self.client, method)(url).status_code, 404, url)
        # 移除是幂等的：本来就不在也算成功
        self.assertEqual(self.client.delete("/api/jobs/nope").get_json(), {"ok": True})

    def test_没选议题直接拒绝(self):
        self.assertEqual(self.client.post("/api/run", json=_payload(self.root, [])).status_code, 400)


if __name__ == "__main__":
    unittest.main()
