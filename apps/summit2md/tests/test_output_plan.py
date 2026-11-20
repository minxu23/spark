"""Podcast 订阅的输出计划：讨论、保存（顺带改忽略列表）、按计划更新，节目总结按计划主题写。
模型和网络全部 mock 掉。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import output_plan, pipeline, server, subscriptions_store, tracking


def _discover(entries):
    return {"entries": entries, "summit_title": "示例节目", "content_type": "series"}


ENTRIES = [{"id": f"e{i}", "title": f"第{i}期", "duration": 3600 * i, "view_count": 1000 * i, "source_type": "rss"}
           for i in range(1, 6)]


class PlanHelpersTests(unittest.TestCase):
    def test_主题分组去重去空名(self):
        got = output_plan.topic_groups([
            {"title": " 睡眠 ", "ids": ["a", "b", "a"]}, {"title": "", "ids": ["c"]},
            {"title": "动力", "ids": ["b", "c"]}, "乱写的"])
        self.assertEqual(got, [{"title": "睡眠", "ids": ["a", "b"]}, {"title": "动力", "ids": ["c"]}])

    def test_计划去掉列表里没有的期数_篇幅不认识就用标准(self):
        plan = output_plan.normalize_plan({"summary_length": "超长", "groups": [{"title": "A", "ids": ["e1", "x"]}]},
                                          {"e1"})
        self.assertEqual(plan, {"summary_length": "medium", "groups": [{"title": "A", "ids": ["e1"]}]})

    def test_解析模型回复(self):
        raw = '```json\n{"reply": "挑了 2 期", "summary_length": "long", "groups": [{"title": "A", "ids": ["e1", "e9"]}]}\n```'
        reply, plan = output_plan.parse_reply(raw, {"e1", "e2"})
        self.assertEqual(reply, "挑了 2 期")
        self.assertEqual(plan["groups"], [{"title": "A", "ids": ["e1"]}])
        with self.assertRaises(output_plan.PlanError):
            output_plan.parse_reply("我觉得都挺好", {"e1"})
        with self.assertRaises(output_plan.PlanError):
            output_plan.parse_reply('{"groups": [{"title": "A", "ids": ["zz"]}]}', {"e1"})

    def test_计划外没处理的进忽略列表_计划里的移出(self):
        sub = {"ignored_ids": ["e2", "old"]}
        episodes = [{"id": "e1", "done": True}, {"id": "e2", "done": False},
                    {"id": "e3", "done": False}, {"id": "e4", "done": False}]
        ignored, to_run = output_plan.apply_ignores(
            sub, {"groups": [{"title": "A", "ids": ["e1", "e2"]}]}, episodes)
        self.assertEqual(to_run, ["e2"])              # e1 做过了，不再做
        self.assertEqual(ignored, ["old", "e3", "e4"])  # e2 移出忽略列表；做过的 e1 不用忽略


class PlanPromptTests(unittest.TestCase):
    def _rows(self):
        return [{"ok": True, "entry": {"id": "e1", "title": "睡觉"}, "summary": {"tldr": "t"}},
                {"ok": True, "entry": {"id": "e2", "title": "跑步"}, "summary": {"tldr": "t"}},
                {"ok": False, "entry": {"id": "e3", "title": "没做成"}}]

    def test_没有计划不加说明(self):
        self.assertEqual(pipeline.topic_plan_note({}, self._rows()), "")

    def test_节目总结按计划主题写_只列做好的期数(self):
        manifest = {"topic_plan": [{"title": "睡眠", "ids": ["e1", "e3"]}, {"title": "运动", "ids": ["e2"]}]}
        prompt = pipeline._series_overall_prompt("节目", manifest, self._rows(), [], None)
        self.assertIn("主题：睡眠\n- 睡觉", prompt)
        self.assertIn("主题：运动\n- 跑步", prompt)
        self.assertNotIn("- 没做成\n", prompt.split("输出计划里定好了主题")[1])

    def test_主题改过就整篇重写_没改就不动(self):
        rows = self._rows()
        manifest = {"topic_plan": [{"title": "睡眠", "ids": ["e1"]}], "overall_summary": "### 长期主线\n旧的",
                    "entries": {}}
        job = mock.Mock(out_dir=tempfile.mkdtemp(), summit_title="节目", content_type="series", backend="api",
                        api_key="k", model="m", api_base="", llm_cache="", stop_flag=None, report=lambda **k: None)
        with mock.patch.object(pipeline, "_cached_summarize", return_value="### 内容总结\n新\n\n### 长期主线\n#### 睡眠\n好") as llm, \
             mock.patch.object(pipeline, "_save_manifest"):
            pipeline._refresh_overall_summary(job, manifest, rows, do_summary=True, regenerate_summary=True,
                                              overall_model="", was_stopped=False, new_rows=[])
            self.assertEqual(llm.call_count, 1)
            self.assertNotIn("原总结", llm.call_args[0][0])     # 不是增量更新
            self.assertEqual(manifest["topic_plan_applied"], ["睡眠"])
            pipeline._refresh_overall_summary(job, manifest, rows, do_summary=True, regenerate_summary=True,
                                              overall_model="", was_stopped=False, new_rows=[])
            self.assertEqual(llm.call_count, 1)                  # 主题没变、也没有新单集：不再调用


class PlanApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self._patch = mock.patch.object(subscriptions_store, "STORE_PATH",
                                        os.path.join(self.root, "subscriptions.json"))
        self._patch.start()
        output_plan._ENTRY_CACHE.clear()
        self.client = server.app.test_client()
        with mock.patch.object(pipeline, "fetch_playlist", return_value=_discover(ENTRIES[:1])):
            self.sub = self.client.post("/api/subscriptions", json={
                "url": "https://pod.example/feed", "kind": "podcast", "output_dir": self.root}).get_json()
        os.makedirs(self.sub["folder"])
        with open(os.path.join(self.sub["folder"], ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {"e1": {"ok": True, "entry": ENTRIES[0]}}}, f)
        self._list = mock.patch.object(tracking, "list_entries", return_value=_discover(ENTRIES))
        self._list.start()

    def tearDown(self):
        self._list.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def test_读取计划带上全部单集和状态(self):
        d = self.client.get(f"/api/subscriptions/{self.sub['id']}/plan").get_json()
        self.assertIsNone(d["plan"])
        self.assertEqual([e["id"] for e in d["episodes"]], ["e1", "e2", "e3", "e4", "e5"])
        self.assertEqual((d["episodes"][0]["done"], d["episodes"][1]["views"]), (True, 2000))

    def test_讨论一轮返回说明和计划(self):
        reply = json.dumps({"reply": "挑了 2 期", "summary_length": "long",
                            "groups": [{"title": "入门", "ids": ["e2", "e3"]}]}, ensure_ascii=False)
        with mock.patch.object(pipeline, "_cached_summarize", return_value=reply) as llm:
            r = self.client.post(f"/api/subscriptions/{self.sub['id']}/plan/discuss", json={
                "backend": "api", "api_key": "k", "messages": [{"role": "user", "content": "只要前几期"}]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["plan"]["groups"], [{"title": "入门", "ids": ["e2", "e3"]}])
        prompt = llm.call_args[0][0]
        self.assertIn("用户：只要前几期", prompt)
        self.assertIn("e1 | 第1期 | 60 | 1K | 已处理", prompt)

    def test_保存计划改忽略列表_按计划更新带上主题(self):
        plan = {"summary_length": "long", "groups": [{"title": "入门", "ids": ["e1", "e2"]}, {"title": "进阶", "ids": ["e4"]}]}
        r = self.client.put(f"/api/subscriptions/{self.sub['id']}/plan",
                            json={"plan": plan, "messages": [{"role": "user", "content": "好"}]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["to_run"], ["e2", "e4"])
        sub = subscriptions_store.get(self.sub["id"])
        self.assertEqual(sorted(sub["ignored_ids"]), ["e3", "e5"])
        self.assertEqual(sub["plan"]["groups"][1], {"title": "进阶", "ids": ["e4"]})
        self.assertEqual(sub["plan_messages"], [{"role": "user", "content": "好"}])

        with mock.patch.object(server, "_launch_run", return_value=({"job_id": "j"}, 200)) as launch:
            r = self.client.post(f"/api/subscriptions/{self.sub['id']}/update",
                                 json={"entry_ids": ["e2", "e4"], "summary_length": "long"})
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = launch.call_args[0][0]
        self.assertEqual([e["id"] for e in payload["entries"]], ["e2", "e4"])
        self.assertEqual(payload["topic_plan"][0]["title"], "入门")

    def test_计划里的往期比已处理的还早也能做(self):
        # e1 已处理且排在最前（最新），e3 更早：「只要更新的」会把它筛掉，挑了就要能做
        self.client.put(f"/api/subscriptions/{self.sub['id']}/plan",
                        json={"plan": {"groups": [{"title": "A", "ids": ["e3"]}]}})
        with mock.patch.object(server, "_launch_run", return_value=({"job_id": "j"}, 200)) as launch, \
             mock.patch.object(tracking, "newer_than_processed", return_value=set()):
            r = self.client.post(f"/api/subscriptions/{self.sub['id']}/update", json={"entry_ids": ["e3"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual([e["id"] for e in launch.call_args[0][0]["entries"]], ["e3"])

    def test_计划的期数都做过了_只按新主题重写总结(self):
        self.client.put(f"/api/subscriptions/{self.sub['id']}/plan",
                        json={"plan": {"groups": [{"title": "A", "ids": ["e1"]}]}})
        with mock.patch.object(server, "_launch_run", return_value=({"job_id": "j"}, 200)) as launch:
            r = self.client.post(f"/api/subscriptions/{self.sub['id']}/update",
                                 json={"entry_ids": [], "refresh_summary": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        payload = launch.call_args[0][0]
        self.assertEqual([e["id"] for e in payload["entries"]], ["e1"])
        self.assertIs(payload["skip_existing"], True)

    def test_空计划不能保存(self):
        r = self.client.put(f"/api/subscriptions/{self.sub['id']}/plan", json={"plan": {"groups": [{"title": "A", "ids": ["zz"]}]}})
        self.assertEqual(r.status_code, 400)


class PlanOrderTests(unittest.TestCase):
    def test_主线和索引按计划顺序_名字照计划写(self):
        text = ("### 内容总结\n总述\n\n### 长期主线\n\n#### 学习\n学\n\n#### 睡眠\n睡\n\n#### AndyGalpin系列\n练\n"
                "### 主题索引\n\n**AndyGalpin系列**\n- [c](x)\n\n**睡眠**\n- [a](y)\n\n**学习**\n- [b](z)\n")
        groups = {"AndyGalpin系列": ["c"], "睡眠": ["a"], "学习": ["b"]}
        out, g = pipeline.order_by_topic_plan(text, groups, ["睡眠", "学习", "Andy Galpin 系列"])
        import re
        self.assertEqual(re.findall(r"(?m)^#### (.+)$", out), ["睡眠", "学习", "Andy Galpin 系列"])
        self.assertEqual(re.findall(r"(?m)^\*\*(.+)\*\*$", out), ["睡眠", "学习", "Andy Galpin 系列"])
        self.assertEqual(list(g), ["睡眠", "学习", "Andy Galpin 系列"])
        self.assertTrue(out.startswith("### 内容总结\n总述"))
        self.assertIn("#### 睡眠\n睡\n", out)
        # 组和组之间要空一行，不然 **主题** 会被当成上一条列表项的续行
        self.assertIn("- [b](z)\n\n**Andy Galpin 系列**", out)
        self.assertIn("练\n### 主题索引", out)   # 区段末尾原来的空白不动


if __name__ == "__main__":
    unittest.main()
