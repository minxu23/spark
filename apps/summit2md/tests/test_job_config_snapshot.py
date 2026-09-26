"""刷新页面后恢复的任务卡片：任务启动时留配置快照（不含 API Key），「重试失败项」
「生成主题总结」带 from_job 时按原任务的后端/模型/输出目录跑，而不是表单当前的默认值。"""

import json
import os
import tempfile
import threading
import time
from unittest import mock

from apps.summit2md import pipeline, server
from apps.summit2md.tests.test_job_routes import _Base, _entry, _payload
from core import keys


class _SnapshotBase(_Base):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_process_job(**kw):
            self.calls.append(kw)
            out = os.path.join(kw["output_base_dir"], pipeline.sanitize_filename(kw["summit_title"]))
            os.makedirs(out, exist_ok=True)
            return {"output_dir": out, "index_path": "", "rows": [], "stopped": False,
                    "failed_entries": [], "unprocessed_entries": []}

        p = mock.patch.object(pipeline, "process_job", side_effect=fake_process_job)
        p.start()
        self.addCleanup(p.stop)
        # 不让本机真实的 key（环境变量 / ~/.spark/keys）混进来
        self._keys_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._keys_dir.cleanup)
        for patcher in (mock.patch.object(keys, "SEARCH_DIRS", (self._keys_dir.name,)),
                        mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "OPENROUTER_API_KEY": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_original(self, **kw):
        """模拟用户在表单里用 OpenRouter + 自定义输出目录发起的原任务。"""
        self.orig_root = os.path.join(self.root, "原来的目录")
        data = _payload(self.orig_root, [_entry("e1", "一")], backend="openrouter",
                        api_key="sk-or-secret", model="vendor/model-a", overall_model="vendor/model-big",
                        summary_length="long", speech_lang_mode="zh", do_speech_script=True,
                        lang_prefs="en,zh")
        data.update(kw)
        r = self.client.post("/api/run", json=data)
        self.assertEqual(r.status_code, 200, r.get_json())
        job_id = r.get_json()["job_id"]
        self.wait_done(job_id)
        return job_id

    def listed(self, job_id):
        return next(j for j in self.client.get("/api/jobs").get_json()["jobs"] if j["job_id"] == job_id)


class SnapshotStoredTests(_SnapshotBase):
    def test_快照记下后端模型输出目录_不含Key(self):
        job_id = self.start_original()
        cfg = self.listed(job_id)["config"]
        self.assertEqual(cfg["backend"], "openrouter")
        self.assertEqual(cfg["model"], "vendor/model-a")
        self.assertEqual(cfg["overall_model"], "vendor/model-big")
        self.assertEqual(cfg["output_dir"], os.path.realpath(self.orig_root))
        self.assertEqual(cfg["summit_title"], "测试节目")
        self.assertEqual(cfg["content_type"], "series")
        self.assertEqual(cfg["summary_length"], "long")
        self.assertEqual(cfg["speech_lang_mode"], "zh")
        self.assertTrue(cfg["do_speech_script"])
        self.assertEqual(cfg["lang_prefs"], "en,zh")
        self.assertNotIn("api_key", cfg)
        self.assertNotIn("sk-or-secret", json.dumps(self.client.get("/api/jobs").get_json()))
        self.assertNotIn("sk-or-secret", json.dumps(server.JOBS[job_id]["config"]))

    def test_带凭据的APIBase不进快照(self):
        job_id = self.start_original(backend="openai_compatible", api_key="k",
                                     api_base="https://user:pw@example.com/v1")
        self.assertEqual(self.listed(job_id)["config"]["api_base"], "")
        job_id2 = self.start_original(backend="openai_compatible", api_key="k",
                                      api_base="https://example.com/v1", summit_title="另一个")
        self.assertEqual(self.listed(job_id2)["config"]["api_base"], "https://example.com/v1")


class RestoredRetryTests(_SnapshotBase):
    def test_重试按快照的后端模型输出目录_不看表单默认值(self):
        job_id = self.start_original()
        # 恢复后的卡片：表单现在是默认后端、别的输出目录；Key 从表单里 OpenRouter 那一栏带过来
        r = self.client.post("/api/run", json={
            "from_job": job_id, "entries": [_entry("e2", "二")], "api_key": "sk-or-form",
            "backend": "api", "model": "", "output_dir": os.path.join(self.root, "别的目录"),
            "summit_title": "别的标题", "content_type": "summit"})
        self.assertEqual(r.status_code, 200, r.get_json())
        new_id = r.get_json()["job_id"]
        self.wait_done(new_id)
        kw = self.calls[-1]
        self.assertEqual(kw["backend"], "openrouter")
        self.assertEqual(kw["model"], "vendor/model-a")
        self.assertEqual(kw["overall_model"], "vendor/model-big")
        self.assertEqual(kw["api_key"], "sk-or-form")
        self.assertEqual(kw["output_base_dir"], os.path.realpath(self.orig_root))
        self.assertEqual(kw["summit_title"], "测试节目")
        self.assertEqual(kw["content_type"], "series")
        self.assertEqual(kw["summary_length"], "long")
        self.assertEqual([e["id"] for e in kw["entries"]], ["e2"])
        # 重试出来的新任务同样有快照，再恢复还能接着重试
        self.assertEqual(self.listed(new_id)["config"]["backend"], "openrouter")

    def test_Key从环境变量解析(self):
        job_id = self.start_original(backend="api", api_key="sk-ant-form", model="claude-x")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-env"}):
            r = self.client.post("/api/run", json={"from_job": job_id, "entries": [_entry("e2", "二")]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.wait_done(r.get_json()["job_id"])
        self.assertEqual(self.calls[-1]["api_key"], "sk-ant-env")
        self.assertEqual(self.calls[-1]["model"], "claude-x")

    def test_找不到Key时说清楚要在表单里补(self):
        job_id = self.start_original(backend="api", api_key="sk-ant-form")
        r = self.client.post("/api/run", json={"from_job": job_id, "entries": [_entry("e2", "二")],
                                               "backend": "ollama", "model": "llama"})
        self.assertEqual(r.status_code, 400)
        err = r.get_json()["error"]
        self.assertIn("Anthropic API", err)
        self.assertIn("填上", err)
        self.assertEqual(len(self.calls), 1, "Key 解析失败不能启动任务")

    def test_没有快照的旧任务拒绝重试(self):
        job_id = "oldjob000001"
        with server.JOBS_LOCK:
            server.JOBS[job_id] = {"id": job_id, "summit_title": "旧任务", "content_type": "summit",
                                   "log": [], "stage": "done", "current": 1, "total": 1, "done": True,
                                   "error": None, "result": None, "stop_requested": False,
                                   "paused": False, "created_at": time.time()}
        self.addCleanup(server.JOBS.pop, job_id, None)
        self.assertIsNone(self.listed(job_id)["config"])
        r = self.client.post("/api/run", json={"from_job": job_id, "entries": [_entry("e2", "二")],
                                               "api_key": "k"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("没有记下当时的 AI 后端/模型/输出目录", r.get_json()["error"])
        r = self.client.post("/api/topic_summary", json={
            "from_job": job_id, "output_dir": self.root, "themes": ["主题"], "api_key": "k"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("重新开始", r.get_json()["error"])
        self.assertEqual(self.calls, [])

    def test_原任务记录不在了(self):
        r = self.client.post("/api/run", json={"from_job": "nope", "entries": [_entry("e2", "二")]})
        self.assertEqual(r.status_code, 404)
        self.assertIn("原任务的记录已经不在了", r.get_json()["error"])


class RestoredTopicSummaryTests(_SnapshotBase):
    def _wait_simple(self, sjid):
        for _ in range(250):
            d = self.client.get(f"/api/simple_job_status/{sjid}").get_json()
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("主题总结任务没有结束")

    def test_主题总结按快照的后端和大会总结模型_写原任务目录(self):
        job_id = self.start_original()
        seen = {}
        done = threading.Event()

        def fake(stop_flag=None, **kw):
            seen.update(kw)
            done.set()
            return {"relative_path": "x.md", "count": 1, "content": ""}

        for url, extra, target in (
            ("/api/topic_summary", {"themes": ["主题一"]}, "generate_topic_summary"),
            ("/api/custom_topic_summary", {"entry_ids": ["e1"]}, "generate_custom_topic_summary"),
        ):
            seen.clear()
            with mock.patch.object(pipeline, target, side_effect=fake):
                r = self.client.post(url, json={
                    "from_job": job_id, "api_key": "sk-or-form", "backend": "api", "model": "",
                    "output_dir": self.root, "summit_title": "别的", **extra})
                self.assertEqual(r.status_code, 200, r.get_json())
                self._wait_simple(r.get_json()["job_id"])
            self.assertEqual(seen["backend"], "openrouter", url)
            self.assertEqual(seen["model"], "vendor/model-big", url)
            self.assertEqual(seen["api_key"], "sk-or-form", url)
            self.assertEqual(seen["out_dir"], os.path.join(os.path.realpath(self.orig_root), "测试节目"), url)
            self.assertEqual(seen["summit_title"], "测试节目", url)

    def test_主题总结找不到Key时报错(self):
        job_id = self.start_original()
        r = self.client.post("/api/topic_summary", json={"from_job": job_id, "themes": ["主题一"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("OpenRouter", r.get_json()["error"])
        self.assertIn("填上", r.get_json()["error"])
