"""主题含义：歧义时请用户选、点选后按选定含义检索、素材不足的提示、检索记录与近期关注。"""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from apps.notes2insight import activity, llm, search, server, vault

TWO_SENSES = ("含义|A|递归自我改进|AI 自己改进自己|1\n"
              "含义|B|相对强弱指标|股票技术分析指标|-\n"
              "近期|B\n"
              "A|3|recursive self-improvement\nB|3|relative strength index\n")


def _vault(notes: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, text in notes.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return root


class FindIntentTests(unittest.TestCase):
    def setUp(self):
        self.root = _vault({"a.md": "# A\n讲 RSI，也就是 recursive self-improvement。"})
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_有歧义定不下来_返回各个含义_不打分不筛选(self):
        with mock.patch.object(llm, "complete", return_value=TWO_SENSES) as m:
            r = search.find(self.root, "RSI", backend="api")
        self.assertTrue(r["need_choice"])
        self.assertEqual([s["label"] for s in r["senses"]], ["递归自我改进", "相对强弱指标"])
        self.assertEqual(r["candidates"], [])
        self.assertEqual(m.call_count, 1)   # 只有判断含义那一次，没有进入相关度筛选
        self.assertIn("近期", r["intent_reason"])

    def test_点选的含义直接用它的检索词_不再判断含义(self):
        sense = {"key": "A", "label": "递归自我改进", "meaning": "AI 自己改进自己",
                 "terms": [{"term": "recursive self-improvement", "weight": 3}]}
        with mock.patch.object(llm, "complete", return_value="1|5|核心") as m:
            r = search.find(self.root, "RSI", backend="api", sense=sense, senses=[sense])
        self.assertEqual(m.call_count, 1)   # 只有筛选
        self.assertIn("（指：AI 自己改进自己）", m.call_args[0][0])
        self.assertEqual([c["path"] for c in r["candidates"]], ["a.md"])
        self.assertEqual(r["chosen"], "A")
        self.assertIn("只有 1 篇", r["insufficient"])

    def test_资料够多时不提示素材不足(self):
        root = _vault({f"n{i}.md": f"# N{i}\n讲递归自我改进" for i in range(4)})
        self.addCleanup(shutil.rmtree, root, True)
        sense = {"key": "A", "label": "递归自我改进", "meaning": "",
                 "terms": [{"term": "递归自我改进", "weight": 3}]}
        with mock.patch.object(llm, "complete", return_value="1|5|x\n2|4|x\n3|3|x\n4|1|x"):
            r = search.find(root, "RSI", backend="api", sense=sense)
        self.assertEqual(r["insufficient"], "")


class ActivityTests(unittest.TestCase):
    def test_近期检索_报告_新订阅写成几行_太早的不算(self):
        now = time.time()
        activity.record_search("RSI", "递归自我改进", "x", manual=True, now=now - 86400)
        activity.record_search("MoE", "", "", now=now - 200 * 86400)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        for name, age in (("技术洞察报告_RSI.md", 3), ("旧报告.md", 300)):
            p = os.path.join(out, name)
            open(p, "w").close()
            os.utime(p, (now - age * 86400, now - age * 86400))
        subs = [{"name": "交易者周刊", "category": "投资", "created_at": "2099-01-01T00:00:00"},
                {"name": "老订阅", "category": "AI", "created_at": "2000-01-01T00:00:00"},
                {"name": "坏日期", "category": "AI", "created_at": "昨天"}]
        lines = activity.recent_activity(out, now=now, load_subs=lambda: subs)
        text = "\n".join(lines)
        self.assertIn("检索「RSI」，按「递归自我改进」（用户手动选的含义）", text)
        self.assertNotIn("MoE", text)
        self.assertIn("生成报告《技术洞察报告_RSI》", text)
        self.assertNotIn("旧报告", text)
        self.assertIn("最近新订阅（投资）：交易者周刊", text)
        self.assertNotIn("老订阅", text)

    def test_记录有上限_文件坏了当成空(self):
        os.makedirs(vault.CACHE_DIR, exist_ok=True)
        with open(activity._history_path(), "w") as f:
            f.write("{坏的")
        self.assertEqual(activity.load_history(), [])
        with mock.patch.object(activity, "HISTORY_MAX", 3):
            for i in range(5):
                activity.record_search(f"t{i}")
        self.assertEqual([h["topic"] for h in activity.load_history()], ["t2", "t3", "t4"])


class SearchRouteIntentTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.root = _vault({"a.md": "# A\n讲 RSI"})
        self.addCleanup(shutil.rmtree, self.root, True)

    def wait_done(self, job_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            d = self.client.get(f"/api/progress/{job_id}").get_json()
            if d["done"]:
                return d
            time.sleep(0.02)
        self.fail("检索没有结束")

    def post(self, **kw):
        body = {"topic": "RSI", "backend": "api", "api_key": "k", "root": self.root,
                "output_dir": os.path.join(self.root, "output")}
        body.update(kw)
        return self.client.post("/api/search", json=body)

    def test_点选含义的格式不对给400(self):
        for bad in ({"terms": []}, {"terms": [{"term": "x y", "weight": 5}]}, {"terms": "abc"}, "abc",
                    {"terms": [{"term": "ok term", "weight": True}]}):
            r = self.post(sense=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_要选含义时不记检索记录_点选之后记成手动(self):
        with mock.patch.object(llm, "complete", return_value=TWO_SENSES):
            d = self.wait_done(self.post().get_json()["job_id"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(activity.load_history(), [])

        sense = {"key": "B", "label": "相对强弱指标", "meaning": "股票指标",
                 "terms": [{"term": "RSI", "weight": 3}]}
        with mock.patch.object(llm, "complete", return_value="1|4|x"):
            d = self.wait_done(self.post(sense=sense, senses=[sense]).get_json()["job_id"])
        self.assertTrue(d["ok"], d)
        hist = activity.load_history()
        self.assertEqual((hist[-1]["topic"], hist[-1]["label"], hist[-1]["manual"]),
                         ("RSI", "相对强弱指标", True))

    def test_关注点生成_选定了含义按含义写_没选时参考库里的片段(self):
        with mock.patch.object(llm, "complete", return_value="关注点") as m:
            r = self.client.post("/api/focus_from_topic", json={
                "topic": "RSI", "meaning": "递归自我改进", "backend": "api", "api_key": "k",
                "root": self.root, "output_dir": os.path.join(self.root, "output")})
        self.assertEqual(r.status_code, 200)
        prompt = m.call_args[0][0]
        self.assertIn("用户确认这个主题指的是：递归自我改进", prompt)
        self.assertNotIn("讲 RSI", prompt)   # 选定了含义就不再给库里的片段，免得被改回去
        with mock.patch.object(llm, "complete", return_value="关注点") as m:
            self.client.post("/api/focus_from_topic", json={
                "topic": "RSI", "backend": "api", "api_key": "k",
                "root": self.root, "output_dir": os.path.join(self.root, "output")})
        self.assertIn("讲 RSI", m.call_args[0][0])
        self.assertIn("按笔记库的语境理解", m.call_args[0][0])


if __name__ == "__main__":
    unittest.main()


class MergeVersionTests(unittest.TestCase):
    def test_版本优先级_整理稿_其它笔记_逐字稿_单集笔记(self):
        r = search.version_rank
        self.assertLess(r("Spark/X/speech/a.md"), r("Podcast/X/a.md"))
        self.assertLess(r("Podcast/X/a.md"), r("Podcast/_transcripts/X/a.md"))
        self.assertEqual(r("Spark/X/transcripts/a.md"), r("Podcast/_transcripts/X/a.md"))
        self.assertLess(r("Spark/X/transcripts/a.md"), r("Spark/X/notes/a.md"))

    def test_同一期合并时留整理稿_排名按这一期的最高分(self):
        long_body = "无关的长段落。" * 400
        root = _vault({
            "Spark/S/notes/20260101 第一期.md": "# 第一期\\n递归自我改进 递归自我改进 递归自我改进",
            "Spark/S/speech/20260101_第一期.md": "# 第一期\\n递归自我改进。" + long_body,
            "Spark/S/transcripts/20260101_第一期.md": "# 第一期\\n递归自我改进。" + long_body,
            "Spark/S/speech/20260102_第二期.md": "# 第二期\\n递归自我改进。" + long_body,
        })
        self.addCleanup(shutil.rmtree, root, True)
        sense = {"key": "A", "label": "递归自我改进", "meaning": "",
                 "terms": [{"term": "递归自我改进", "weight": 3}]}
        with mock.patch.object(vault, "_parse_head", side_effect=lambda full, fname: (
                fname.split("_")[-1].split(" ")[-1][:-3], "2026-01-0" + ("1" if "01" in fname[:8] else "2"))):
            r = search.find(root, "RSI", backend="api", sense=sense, do_screen=False)
        paths = [c["path"] for c in r["candidates"]]
        self.assertEqual(paths[0], "Spark/S/speech/20260101_第一期.md")   # 笔记分最高，但留整理稿
        self.assertEqual(r["duplicates"], 2)
        self.assertEqual(len(paths), 2)


class NoteToSpeechTests(unittest.TestCase):
    def test_只有单集笔记命中时换成同一期的整理稿(self):
        self.assertEqual(search.speech_for_episode_note("Spark/S/notes/20260818 What Happens.md"),
                         "Spark/S/speech/20260818_What Happens.md")
        self.assertEqual(search.speech_for_episode_note("Podcast/S/a.md"), "")
        root = _vault({
            "Spark/S/notes/20260818 第一期.md": "# 第一期\n递归自我改进",
            "Spark/S/speech/20260818_第一期.md": "# 第一期\n说法不一样的完整整理稿。" * 50,
        })
        self.addCleanup(shutil.rmtree, root, True)
        sense = {"key": "A", "label": "x", "meaning": "", "terms": [{"term": "递归自我改进", "weight": 3}]}
        r = search.find(root, "RSI", backend="api", sense=sense, do_screen=False)
        [c] = r["candidates"]
        self.assertEqual(c["path"], "Spark/S/speech/20260818_第一期.md")
        self.assertIn("递归自我改进", c["snippet"])
