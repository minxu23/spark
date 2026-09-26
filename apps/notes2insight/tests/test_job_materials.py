"""报告任务记下"这次处理的是哪些材料、怎么配的"：刷新页面（?job=）或失败后重试时，
页面靠这份记录说清楚处理的是哪一批。pipeline.run 用假的替换，不调模型。"""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from apps.notes2insight import pipeline, server


class JobMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.root = tempfile.mkdtemp(prefix="n2i_mats_")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "a.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitle: 推理成本的拐点\n---\n正文")
        for i in range(10):
            with open(os.path.join(self.root, f"n{i}.md"), "w", encoding="utf-8") as f:
                f.write(f"第 {i} 篇")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def wait_done(self, job_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.client.get(f"/api/progress/{job_id}").get_json()
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("任务在超时前没有结束")

    def start(self, **kw):
        data = {"notes": ["sub/a.md"] + [f"n{i}.md" for i in range(10)], "root": self.root,
                "backend": "api", "api_key": "sk-秘密", "api_base": "https://token@example.com/v1",
                "model": "claude-sonnet-5", "depth": "brief", "focus": "推理成本",
                "max_note_chars": 50000, "use_cache": False, "source": "manual"}
        data.update(kw)
        with mock.patch.object(pipeline, "run", side_effect=RuntimeError("模型挂了")):
            r = self.client.post("/api/run", json=data)
            self.assertEqual(r.status_code, 200, r.get_json())
            job_id = r.get_json()["job_id"]
            d = self.wait_done(job_id)
        return job_id, d

    def test_进度里带材料摘要_完整清单单独取(self):
        job_id, d = self.start()
        self.assertFalse(d["ok"])
        mats = d["materials"]
        self.assertEqual(mats["count"], 11)
        self.assertEqual(mats["source"], "manual")
        self.assertEqual(mats["root"], os.path.abspath(self.root))
        # 轮询接口只带前几篇，别每 1.5 秒把几百篇的清单传一遍
        self.assertEqual(len(mats["sample"]), server._MATERIALS_SAMPLE)
        # 标题从笔记开头（frontmatter）读出来，不是文件名
        self.assertEqual(mats["sample"][0], {"path": "sub/a.md", "title": "推理成本的拐点"})
        self.assertNotIn("retrieval", d)

        full = self.client.get(f"/api/job/{job_id}/materials").get_json()
        self.assertEqual(len(full["items"]), 11)
        self.assertEqual([x["path"] for x in full["items"]][:2], ["sub/a.md", "n0.md"])
        self.assertEqual(full["focus"], "推理成本")
        s = full["settings"]
        self.assertEqual((s["backend"], s["model"], s["depth"], s["max_note_chars"], s["use_cache"]),
                         ("api", "claude-sonnet-5", "brief", 50000, False))
        self.assertTrue(s["output_dir"])

    def test_任务记录里没有_key_和_base(self):
        job_id, d = self.start()
        full = self.client.get(f"/api/job/{job_id}/materials").get_json()
        for blob in (repr(d), repr(full), repr(server.JOBS[job_id])):
            self.assertNotIn("sk-秘密", blob)
            self.assertNotIn("token@example.com", blob)

    def test_来源只认白名单_外部页面发起的记成空(self):
        _, d = self.start(source="<script>")
        self.assertEqual(d["materials"]["source"], "")
        _, d = self.start(source=None)
        self.assertEqual(d["materials"]["source"], "")

    def test_笔记库读不到时标题退回文件名_任务照常结束(self):
        _, d = self.start(root=os.path.join(self.root, "不存在"))
        self.assertEqual(d["materials"]["sample"][0]["title"], "a")
        self.assertIn("模型挂了", d["error"])

    def test_取标题不读库外和非笔记文件(self):
        from apps.notes2insight import vault
        outside = os.path.join(os.path.dirname(self.root), "n2i_outside_secret.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("---\ntitle: 库外的文件\n---\n")
        self.addCleanup(os.remove, outside)
        rel = os.path.relpath(outside, self.root)
        got = vault.note_titles(self.root, ["sub/a.md", rel, "不存在.md", "sub"])
        self.assertEqual(got, {"sub/a.md": "推理成本的拐点"})

    def test_不存在的任务和非报告任务(self):
        self.assertEqual(self.client.get("/api/job/nope/materials").status_code, 404)
        job_id = server._new_job("search", 1, "x")
        self.addCleanup(server._finish, job_id, ok=False)   # 别留一个"进行中"的任务给别的测试
        self.assertEqual(self.client.get(f"/api/job/{job_id}/materials").status_code, 404)


class EnvEstimateConstantsTests(unittest.TestCase):
    def test_前端估算用的常量和流水线一致(self):
        env = server.app.test_client().get("/api/env").get_json()
        self.assertEqual(env["estimate"], {"chunk_chars": pipeline.CHUNK_CHARS,
                                           "framework_batch_chars": pipeline.FRAMEWORK_BATCH_CHARS,
                                           "card_chars": pipeline.EST_CARD_CHARS})
        self.assertEqual({d["key"]: d["clusters"] for d in env["depths"]},
                         {k: v["clusters"] for k, v in pipeline.DEPTH_PRESETS.items()})


if __name__ == "__main__":
    unittest.main()
