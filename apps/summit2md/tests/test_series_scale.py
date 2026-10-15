"""节目总结的篇幅随节目规模放宽：期数多、跨度长的节目，主线更多、每条展开更多，并要求写出阶段演变。"""

import unittest

from apps.summit2md import pipeline


def _rows(n, start="20260101", step_days=7):
    from datetime import datetime, timedelta
    d0 = datetime.strptime(start, "%Y%m%d")
    return [{"ok": True, "entry": {"id": f"e{i}", "title": f"第{i}期",
                                   "publish_date": (d0 + timedelta(days=i * step_days)).strftime("%Y%m%d")},
             "summary": {"tldr": "x"}} for i in range(n)]


class SeriesScaleTests(unittest.TestCase):
    def test_小节目保持原来的篇幅(self):
        s = pipeline.series_scale(_rows(10))
        self.assertEqual((s["mainlines"], s["mainline_sentences"], s["span_note"]), ("3-8", "3-6", ""))

    def test_期数多或跨度长放宽_跨度超过两年要写阶段演变(self):
        self.assertEqual(pipeline.series_scale(_rows(30))["mainlines"], "5-10")
        long_span = pipeline.series_scale(_rows(20, step_days=60))   # 约 3.2 年
        self.assertEqual(long_span["mainlines"], "8-14")
        self.assertIn("阶段", long_span["span_note"])
        self.assertIn("跨度约 3.", long_span["span_text"])
        self.assertEqual(pipeline.series_scale(_rows(80, step_days=1))["mainlines"], "8-14")

    def test_没成功的期数不算_没日期不报跨度(self):
        rows = _rows(3) + [{"ok": False, "entry": {"id": "x", "title": "x"}}] * 40
        s = pipeline.series_scale(rows)
        self.assertEqual((s["total"], s["mainlines"]), (3, "3-8"))
        self.assertEqual(pipeline.series_scale([{"ok": True, "entry": {"id": "a", "title": "a"}}])["span_text"], "")

    def test_首次生成和增量更新的提示词都带上篇幅和规模(self):
        rows = _rows(60)
        rows[0]["summary"]["body"] = "### 本期要点\n- 第0期的具体观点"
        prompt = pipeline._series_overall_prompt("节目", {}, rows, [], None)
        self.assertIn("8-14 条被反复讨论的主线", prompt)
        self.assertIn("最近各期的要点", prompt)
        self.assertIn("第0期的具体观点", prompt)   # 整篇重写时也带要点正文
        self.assertIn("共 60 期，跨度约 1.1 年", prompt)
        previous = "### 内容总结\n旧\n\n### 长期主线\n#### A\n旧\n\n### 主题索引\n主题：A\n- 第1期\n"
        prompt = pipeline._series_overall_prompt("节目", {"topic_groups": {"A": ["e1"]}}, rows, rows[-2:], previous)
        self.assertIn("这个节目一共 60 期", prompt)
        self.assertIn("下面 5-10 句话", prompt)
        self.assertNotIn("{", prompt.split("原总结")[0])   # 占位符都填上了


if __name__ == "__main__":
    unittest.main()
