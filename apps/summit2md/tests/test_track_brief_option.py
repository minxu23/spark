"""信息跟进「新内容」页的两个按钮：「抓取」只逐条存笔记，不出简报；「抓取并生成简报」跟原来的「生成简报」一样。
后端靠 /api/track/run 的 brief（默认 true）和 tracking.run_batch 的 make_brief 区分。模型和网络全部 mock 掉。"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from apps.summit2md import pipeline, server, subscriptions_store, tracking
from core import jobs as jobs_util
from core import sources

LLM = {"backend": "api", "api_key": "k", "model": "m", "api_base": ""}
ENTRIES = [
    {"id": "e1", "title": "Robotiq 开源工具", "url": "https://x/1", "source_type": "rss",
     "publish_date": "20260923", "rss_content_html": "<p>" + "机器人夹爪软件。" * 30 + "</p>"},
    {"id": "e2", "title": "第二条", "url": "https://x/2", "source_type": "rss",
     "publish_date": "20260924", "rss_content_html": "<p>" + "另一条新闻正文。" * 30 + "</p>"},
]


def fake_llm(calls):
    def run(prompt, *a, **kw):
        calls.append(prompt)
        if "资讯主编" in prompt:
            return "主题：夹爪\n\n## 要点速览\n\n- 夹爪软件开源 [1]"
        return "TLDR: 夹爪软件开源\n- 要点一"
    return run


class RunBatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._patch = mock.patch.object(subscriptions_store, "STORE_PATH", os.path.join(self.root, "subscriptions.json"))
        self._patch.start()
        self.sub = subscriptions_store.add(url="https://example.com/feed", name="AI Insider", category="c",
                                           output_dir=self.root, source_type="rss")
        self.brief_dir = os.path.join(self.root, "信息跟进", "简报")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _run(self, **kw):
        calls, logs = [], []
        with mock.patch.object(tracking, "list_entries", return_value={"entries": ENTRIES}), \
             mock.patch.object(sources, "_fetch_generic_article_paragraphs", return_value=None), \
             mock.patch.object(pipeline, "_cached_summarize", side_effect=fake_llm(calls)):
            result = tracking.run_batch([(self.sub, ["e1", "e2"])], output_dir=self.root, llm=LLM,
                                        progress_cb=logs.append, **kw)
        return result, calls, logs

    def test_抓取_单篇照常写_不调合成简报的模型_不写简报文件(self):
        result, calls, logs = self._run(make_brief=False)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any("资讯主编" in c for c in calls), "不出简报就不能调合成简报的模型")
        self.assertIsNone(result["brief_path"])
        self.assertIsNone(result["brief_markdown"])
        self.assertFalse(os.path.exists(self.brief_dir), "不出简报就不建简报文件夹、不写简报文件")
        for name in ("20260923_Robotiq 开源工具.md", "20260924_第二条.md"):
            self.assertTrue(os.path.exists(os.path.join(self.sub["folder"], name)))
        manifest = pipeline._load_manifest(self.sub["folder"])["entries"]
        self.assertTrue(manifest["e1"]["ok"] and manifest["e2"]["ok"])
        said = json.dumps(logs, ensure_ascii=False)
        self.assertNotIn("简报", said)
        self.assertNotIn("brief", [kw.get("stage") for kw in logs])
        self.assertEqual(logs[-1].get("stage"), "done")

    def test_抓取并生成简报_跟原来一样(self):
        result, calls, logs = self._run()
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(calls), 3)
        self.assertTrue("资讯主编" in calls[-1])
        self.assertTrue(result["brief_path"].startswith(self.brief_dir))
        self.assertTrue(os.path.exists(result["brief_path"]))
        self.assertIn("brief", [kw.get("stage") for kw in logs])
        self.assertEqual(self._run(make_brief=True)[1][-1], calls[-1])

    def test_抓取中途停止_不提简报(self):
        calls, logs = [], []
        with mock.patch.object(tracking, "list_entries", return_value={"entries": ENTRIES}), \
             mock.patch.object(pipeline, "_cached_summarize", side_effect=fake_llm(calls)):
            result = tracking.run_batch([(self.sub, ["e1", "e2"])], output_dir=self.root, llm=LLM, make_brief=False,
                                        stop_flag=lambda: len(calls) >= 1, progress_cb=logs.append)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["processed"], 1)
        self.assertNotIn("简报", json.dumps(logs, ensure_ascii=False))
        self.assertFalse(os.path.exists(self.brief_dir))


class TrackRunApiTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        p1 = mock.patch.object(server, "HEAVY_JOBS", jobs_util.JobSlots(2))
        p2 = mock.patch.object(subscriptions_store, "STORE_PATH", os.path.join(self.root, "subs.json"))
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        self.sub = subscriptions_store.add(url="https://x/feed", name="某博客", category="测试",
                                           output_dir=self.root, source_type="rss")

    def _start(self, extra):
        seen = {}

        def batch(selections, **kw):
            seen.update(kw)
            return {"processed": 1, "failed": [], "stopped": False,
                    "brief_path": None, "brief_markdown": None, "brief_error": None}

        body = {"selections": [{"sub_id": self.sub["id"], "entry_ids": ["e1"]}], "api_key": "k",
                "output_dir": self.root, **extra}
        with mock.patch.object(tracking, "run_batch", side_effect=batch):
            job_id = self.client.post("/api/track/run", json=body).get_json()["job_id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                st = self.client.get(f"/api/track/status/{job_id}").get_json()
                if st["done"]:
                    break
                time.sleep(0.01)
        return seen, st

    def test_不传_照旧出简报(self):
        seen, st = self._start({})
        self.assertIs(seen["make_brief"], True)
        self.assertIs(st["brief"], True)

    def test_抓取传false(self):
        seen, st = self._start({"brief": False})
        self.assertIs(seen["make_brief"], False)
        self.assertIs(st["brief"], False)

    def test_只有false才算不出简报(self):
        self.assertIs(self._start({"brief": None})[0]["make_brief"], True)
        self.assertIs(self._start({"brief": "false"})[0]["make_brief"], True)


@pytest.mark.skipif(not shutil.which("node"), reason="没有 node")
def test_页面上两种批次的说法():
    source = (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8")
    start = source.index("  function trackBatchWords(")
    fn = source[start:source.index("  // trackBatchWords 结束", start)]
    script = fn + """
const out = {};
for (const [k, b] of [["grab", false], ["brief", true], ["old", undefined]]) {
  const w = trackBatchWords(b);
  out[k] = {button: w.button, running: w.running(2, 5), stopped: w.stopped(2), again: w.again, calls: w.calls(4)};
}
console.log(JSON.stringify(out));"""
    got = json.loads(subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True).stdout)
    assert got["grab"]["button"] == "抓取" and got["brief"]["button"] == "抓取并生成简报"
    assert got["old"] == got["brief"], "老的批次不带 brief，按出简报算"
    for key in ("running", "stopped", "again"):
        assert "简报" not in got["grab"][key]
    assert got["grab"]["again"] == "再点「抓取」" and got["brief"]["again"] == "再点「抓取并生成简报」"
    assert "没有生成简报" in got["brief"]["stopped"]
    assert got["grab"]["calls"] == 4 and got["brief"]["calls"] == 5
