"""主题检索里不调模型的部分：检索词解析、本地全文打分。"""

import os
import tempfile
import unittest
from unittest import mock

from apps.notes2insight import llm, search


def _note(root, rel, text, title=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"path": rel, "title": title or os.path.splitext(os.path.basename(rel))[0],
            "date": "", "chars": len(text)}


class ExpandTermsTests(unittest.TestCase):
    def test_解析带权检索词_拆开同一行里的中英文_去重保留最高权重(self):
        raw = "3|推理成本\n2|inference cost｜推理费用\n1|推理成本\n1|x\n3|12345\n"
        with mock.patch.object(llm, "complete", return_value=raw):
            terms = search.expand_terms("推理成本")
        self.assertEqual(dict(terms), {"推理成本": 3, "inference cost": 2, "推理费用": 2})

    def test_模型不可用时退回主题本身切词(self):
        with mock.patch.object(llm, "complete", side_effect=llm.LLMError("no backend")):
            terms = search.expand_terms("推理 成本, ASIC")
        self.assertEqual(dict(terms), {"推理": 3, "成本": 3, "asic": 3})


    def test_库里的原文片段进提示词_含义行单独解析_主题词本身补进检索词(self):
        raw = "含义|RSI 指递归自我改进\n3|recursive self-improvement\n3|递归自我改进\n"
        info = {}
        with mock.patch.object(llm, "complete", return_value=raw) as m:
            terms = search.expand_terms("RSI", contexts=["模型能不能 RSI，也就是递归自我改进"], info=info)
        prompt = m.call_args[0][0]
        self.assertIn("模型能不能 RSI，也就是递归自我改进", prompt)
        self.assertEqual(info["meaning"], "RSI 指递归自我改进")
        self.assertEqual(dict(terms), {"recursive self-improvement": 3, "递归自我改进": 3, "rsi": 3})
        self.assertNotIn("含义", dict(terms))


class TopicContextsTests(unittest.TestCase):
    def test_标题命中的先取_英文按词边界_同标题只取一段_取够就停(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [
                _note(root, "a/杂谈.md", "PRSI 和 RSIX 都不算。后面讲到 RSI 的瓶颈。"),
                _note(root, "b/RSI 专题.md", "这一期专门讲 RSI。"),
                _note(root, "c/RSI 专题.md", "同一期的逐字稿，RSI。", title="RSI 专题"),
                _note(root, "d/无关.md", "完全无关。"),
            ]
            ctx = search.topic_contexts(root, "RSI", notes)
            self.assertEqual(len(ctx), 2)
            self.assertIn("专门讲 RSI", ctx[0])
            self.assertIn("RSI 的瓶颈", ctx[1])
            self.assertEqual(len(search.topic_contexts(root, "RSI", notes, limit=1)), 1)


class ScreenTests(unittest.TestCase):
    def test_筛选提示词带上主题含义(self):
        c = search.Candidate(path="a.md", title="t", date="", chars=1, snippet="s")
        with mock.patch.object(llm, "complete", return_value="1|5|核心") as m:
            search.screen("RSI", [c], meaning="递归自我改进")
        self.assertIn("RSI**（指：递归自我改进）", m.call_args[0][0])
        self.assertEqual(c.relevance, 5)


class ScoreNotesTests(unittest.TestCase):
    def test_英文词匹配复数_空格和连字符互通(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [
                _note(root, "a.md", "Many agents run on GPUs."),
                _note(root, "b.md", "Recursive self improvement is near."),
                _note(root, "c.md", "recursive self-improvement again"),
                _note(root, "d.md", "agentic workflows only"),
            ]
            cands = search.score_notes(root, notes, [("agent", 3), ("GPU", 2),
                                                     ("recursive self-improvement", 3)])
        self.assertEqual(sorted(c.path for c in cands), ["a.md", "b.md", "c.md"])

    def test_标题命中排前面_英文词按词边界匹配_没命中的不出现(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [
                _note(root, "a/正文提到.md", "这篇顺带提了一句 ASIC 的进展。" + "填充内容。" * 50),
                _note(root, "b/ASIC 路线.md", "这篇正文也讲 ASIC。" + "填充内容。" * 50),
                _note(root, "c/无关.md", "BASICS of cooking, nothing else." * 5),
            ]
            cands = search.score_notes(root, notes, [("ASIC", 3)])
        self.assertEqual([c.path for c in cands], ["b/ASIC 路线.md", "a/正文提到.md"])
        self.assertIn("ASIC", cands[0].snippet)

    def test_核心词的idf按全部笔记数算_通篇讲它的排前面(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [_note(root, "a/通篇讲智能体.md", "智能体。" * 20 + "填充。" * 30),
                     _note(root, "b/提了一句.md", "提到智能体一次，还有一个冷门词。" + "填充。" * 30)]
            notes += [_note(root, f"c/无关{i}.md", "完全无关的内容。" * 20) for i in range(8)]
            cands = search.score_notes(root, notes, [("智能体", 3), ("冷门词", 1)])
        self.assertEqual(cands[0].path, "a/通篇讲智能体.md")

    def test_读不了的笔记跳过_不影响其它(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [_note(root, "ok.md", "推理成本在下降"),
                     {"path": "missing.md", "title": "missing", "date": "", "chars": 0}]
            cands = search.score_notes(root, notes, [("推理成本", 3)])
        self.assertEqual([c.path for c in cands], ["ok.md"])

    def test_没有检索词返回空(self):
        self.assertEqual(search.score_notes("/tmp", [], []), [])


if __name__ == "__main__":
    unittest.main()
