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


class ScoreNotesTests(unittest.TestCase):
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
