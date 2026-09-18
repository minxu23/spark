"""/api/focus_from_topic：根据一句话主题自动生成一段关注点说明。单次模型调用，
同步接口，没有停止/暂停——跟"生成报告"那种要跑几分钟的任务不是一回事。"""

import os
import unittest
from unittest import mock

from apps.notes2insight import llm, server


class FocusFromTopicTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_主题为空时报错(self):
        r = self.client.post("/api/focus_from_topic", json={"topic": "  ", "backend": "cli"})
        self.assertEqual(r.status_code, 400)

    def test_正常调用返回生成的关注点(self):
        with mock.patch("apps.notes2insight.server.llm.complete", return_value="  覆盖 A、B 两个角度  ") as m:
            r = self.client.post("/api/focus_from_topic", json={
                "topic": "AI 智能体长时程自主运行的工程约束", "backend": "cli",
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"focus": "覆盖 A、B 两个角度"})
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("timeout"), 60)

    def test_模型调用失败时返回502(self):
        with mock.patch("apps.notes2insight.server.llm.complete", side_effect=llm.LLMError("挂了")):
            r = self.client.post("/api/focus_from_topic", json={
                "topic": "AI 智能体长时程自主运行的工程约束", "backend": "cli",
            })
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json(), {"error": "挂了"})

    def test_api后端没填key时按现有规则报错(self):
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("apps.notes2insight.server.llm.read_key_file", return_value=""):
            r = self.client.post("/api/focus_from_topic", json={
                "topic": "随便什么主题", "backend": "api", "api_key": "",
            })
        self.assertEqual(r.status_code, 400)
        self.assertIn("Key", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
