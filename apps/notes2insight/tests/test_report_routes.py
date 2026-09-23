"""生成报告这一套路由：启动、参数整理、进度、结果、停止。pipeline.run 用假的替换，不调模型。"""

import os
import threading
import time
import unittest
from unittest import mock

from apps.notes2insight import llm, pipeline, server


def _payload(**kw):
    data = {"notes": ["a.md"], "root": "/tmp", "backend": "api", "api_key": "k"}
    data.update(kw)
    return data


class ReportRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def wait_done(self, job_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.client.get(f"/api/progress/{job_id}").get_json()
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("任务在超时前没有结束")

    def test_跑完能取到结果_参数被整理好(self):
        seen = {}

        def fake_run(cfg, progress):
            seen["cfg"] = cfg
            progress("digest", 1, 1, "摘取完成")
            return {"path": "/x/报告.md", "content": "# 报告", "filename": "报告.md"}

        with mock.patch.object(pipeline, "run", side_effect=fake_run):
            r = self.client.post("/api/run", json=_payload(
                output_dir="~/报告目录", concurrency="99", timeout="", max_note_chars=-5))
            self.assertEqual(r.status_code, 200, r.get_json())
            d = self.wait_done(r.get_json()["job_id"])
            result = self.client.get(f"/api/result/{r.get_json()['job_id']}").get_json()

        self.assertTrue(d["ok"])
        self.assertNotIn("content", d.get("result", {}))   # 进度接口不带正文
        self.assertEqual(result["content"], "# 报告")
        cfg = seen["cfg"]
        self.assertEqual(cfg.output_dir, os.path.join(os.path.expanduser("~"), "报告目录"))
        self.assertEqual((cfg.concurrency, cfg.timeout, cfg.max_note_chars), (16, 900, 0))

    def test_输出目录只填空格时落回默认目录(self):
        for raw in ("   ", "", None):
            self.assertEqual(server._out_dir({"output_dir": raw}), os.path.abspath(server.DEFAULT_OUTPUT_DIR))

    def test_数字参数不是数字时返回400而不是500(self):
        for key in ("concurrency", "timeout", "max_note_chars"):
            r = self.client.post("/api/run", json=_payload(**{key: "三"}))
            self.assertEqual(r.status_code, 400, key)
            self.assertIn(key, r.get_json()["error"])
        r = self.client.post("/api/search", json={"topic": "推理", "backend": "api", "api_key": "k",
                                                   "candidates": "很多"})
        self.assertEqual(r.status_code, 400)

    def test_停止后任务标成已停止(self):
        started = threading.Event()

        def fake_run(cfg, progress):
            started.set()
            for _ in range(250):
                if cfg.stop_flag():
                    raise llm.Stopped("已停止")
                time.sleep(0.02)
            return {}

        with mock.patch.object(pipeline, "run", side_effect=fake_run):
            job_id = self.client.post("/api/run", json=_payload()).get_json()["job_id"]
            self.assertTrue(started.wait(5))
            self.assertEqual(self.client.post(f"/api/stop/{job_id}").get_json(), {"ok": True})
            d = self.wait_done(job_id)
        self.assertTrue(d["stopped"])
        self.assertFalse(d["ok"])
        self.assertEqual(self.client.get(f"/api/result/{job_id}").status_code, 404)

    def test_出错时带回错误信息(self):
        with mock.patch.object(pipeline, "run", side_effect=RuntimeError("笔记读不到")):
            job_id = self.client.post("/api/run", json=_payload()).get_json()["job_id"]
            d = self.wait_done(job_id)
        self.assertIn("笔记读不到", d["error"])

    def test_没勾笔记和不存在的任务(self):
        self.assertEqual(self.client.post("/api/run", json=_payload(notes=[])).status_code, 400)
        self.assertEqual(self.client.get("/api/progress/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/stop/nope").status_code, 404)

    def test_最近任务列表里有刚跑的报告(self):
        with mock.patch.object(pipeline, "run", return_value={"path": "/x.md", "content": ""}):
            job_id = self.client.post("/api/run", json=_payload()).get_json()["job_id"]
            self.wait_done(job_id)
        jobs = self.client.get("/api/jobs?limit=abc").get_json()["jobs"]
        self.assertIn(job_id, [j["job_id"] for j in jobs])


if __name__ == "__main__":
    unittest.main()
