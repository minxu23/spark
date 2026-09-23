"""deck.py 里不调模型的部分：解析报告、修复模型给的 JSON、规整页面、渲染 HTML/PPTX 再读回来。"""

import os
import tempfile
import unittest

from apps.notes2insight import deck

REPORT = """---
sources: 2 篇
---
# 推理成本报告

## 执行摘要
推理成本下降很快。

### 来源索引
| # | 笔记 | 日期 | 位置 | 状态 |
|---|---|---|---|---|
| [1] | [[成本分析]] 标题 A \\| 副标题 | 2026-09-01 | 研究/AI | ✅ |
| [2] | [[芯片路线]] | — | . | ⚠️ 失败 |
"""


class ParseTests(unittest.TestCase):
    def test_来源索引_转义竖线和库根目录(self):
        src = deck.parse_sources(REPORT)
        self.assertEqual([s["idx"] for s in src], [1, 2])
        self.assertEqual(src[0]["rel"], "研究/AI/成本分析")
        self.assertIn("|", src[0]["title"])
        self.assertEqual(src[1]["rel"], "芯片路线")
        self.assertEqual(src[1]["date"], "")
        self.assertFalse(src[1]["ok"])

    def test_frontmatter(self):
        self.assertEqual(deck.parse_frontmatter(REPORT), {"sources": "2 篇"})


class JsonRepairTests(unittest.TestCase):
    def test_去掉代码围栏和前言(self):
        self.assertEqual(deck._extract_json('好的：\n```json\n{"a": 1}\n```'), {"a": 1})

    def test_字符串里的裸双引号和换行会被修好(self):
        raw = '{"title": "进入"下一阶段"的叙事", "note": "第一行\n第二行"}'
        got = deck._extract_json(raw)
        self.assertEqual(got["title"], '进入"下一阶段"的叙事')
        self.assertEqual(got["note"], "第一行\n第二行")

    def test_没有json时报deck错误(self):
        with self.assertRaises(deck.DeckError):
            deck._extract_json("抱歉，我无法完成")


class NormalizeTests(unittest.TestCase):
    def test_丢空页_剔除不存在的来源编号(self):
        sources = [{"idx": 1}, {"idx": 2}]
        slides = deck.normalize({"slides": [
            {"kind": "points", "title": "空页"},
            {"kind": "points", "title": "有内容", "bullets": [{"text": "要点", "cites": [1, 9, "2", 1]}]},
            {"kind": "compare", "title": "只有一栏", "columns": [{"head": "A", "items": ["x"]}]},
            {"kind": "weird", "title": "未知类型当普通页", "lead": "导语"},
        ]}, sources)
        self.assertEqual([s["title"] for s in slides], ["有内容", "未知类型当普通页"])
        self.assertEqual(slides[0]["bullets"][0]["cites"], [1, 2])
        self.assertEqual(slides[1]["kind"], "points")

    def test_一页都没有就报错(self):
        with self.assertRaises(deck.DeckError):
            deck.normalize({"slides": []}, [])


class RenderTests(unittest.TestCase):
    def _deck(self):
        return {
            "title": "报告</script><b>", "subtitle": "副标题", "meta": {"sources": "2 篇"},
            "slides": [{"kind": "points", "section": "一", "title": "页一", "lead": "", "note": "",
                        "bullets": [{"text": "要点 [1]", "cites": [1]}]}],
            "sources": deck.parse_sources(REPORT),
        }

    def test_html里的脚本块不会被标题提前闭合_还能原样读回(self):
        html = deck.render_html(self._deck(), vault_name="minxu", report_filename="r.md")
        self.assertNotIn("报告</script>", html)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.deck.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            back = deck.load_deck_from_html(path)
        self.assertEqual(back["title"], "报告</script><b>")
        self.assertEqual(back["slides"][0]["title"], "页一")
        self.assertEqual(back["meta"].get("sources"), "2 篇")

    def test_pptx能生成(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.pptx")
            deck.render_pptx(self._deck(), out, vault_name="minxu", report_filename="r.md")
            self.assertGreater(os.path.getsize(out), 10_000)


if __name__ == "__main__":
    unittest.main()
