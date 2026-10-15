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


    def test_老格式没有含义行也能用_当成一个含义(self):
        with mock.patch.object(llm, "complete", return_value="3|递归自我改进\n2|foom\n"):
            a = search.analyze_topic("RSI")
        self.assertEqual(a["decision"], "auto")
        self.assertEqual(dict(a["senses"][0]["terms"]), {"递归自我改进": 3, "foom": 2, "rsi": 3})


def _ctx(recent, older, recent_hits=None, older_hits=None):
    samples = [{"text": t, "date": "2026-10-01", "recent": True, "folder": "x"} for t in recent]
    samples += [{"text": t, "date": "2025-01-01", "recent": False, "folder": "x"} for t in older]
    return {"samples": samples, "recent_hits": recent_hits if recent_hits is not None else len(recent),
            "older_hits": older_hits if older_hits is not None else len(older)}


TWO_SENSES = ("含义|A|递归自我改进|AI 自己改进自己|{a}\n"
              "含义|B|相对强弱指标|股票技术分析指标|{b}\n"
              "近期|{recent}\n"
              "A|3|recursive self-improvement\nA|2|智能爆炸\n"
              "B|3|relative strength index\nB|2|超买\n")


class AnalyzeTopicTests(unittest.TestCase):
    def run_analyze(self, raw, ctx, activity=None):
        with mock.patch.object(llm, "complete", return_value=raw) as m:
            a = search.analyze_topic("RSI", ctx=ctx, activity=activity)
        return a, m.call_args[0][0]

    def test_片段和近期关注进提示词_含义按片段估篇数_一边倒时自动定(self):
        ctx = _ctx(["近期 RSI 片段"] * 2, ["较早 RSI 片段"] * 4, recent_hits=20, older_hits=200)
        raw = TWO_SENSES.format(a="1,2,3,4,5,6", b="-", recent="-")
        a, prompt = self.run_analyze(raw, ctx, activity=["2026-10-10 检索「RSI」"])
        self.assertIn("[1] (2026-10-01，近期) 近期 RSI 片段", prompt)
        self.assertIn("2026-10-10 检索「RSI」", prompt)
        self.assertEqual((a["decision"], a["chosen"]), ("auto", "A"))
        sa, sb = a["senses"]
        self.assertEqual((sa["est_recent"], sa["est_older"]), (20, 200))
        self.assertEqual((sb["est_recent"], sb["est_older"]), (0, 0))
        self.assertIn(("rsi", 3), sa["terms"])   # 用户写的缩写一定在检索词里
        self.assertEqual(dict(sb["terms"])["relative strength index"], 3)

    def test_近期关注指向另一个含义时请用户选(self):
        ctx = _ctx(["a"] * 2, ["b"] * 4)
        a, _ = self.run_analyze(TWO_SENSES.format(a="1,2,3,4,5,6", b="-", recent="B"), ctx)
        self.assertEqual(a["decision"], "ask")
        self.assertIn("相对强弱指标", a["reason"])

    def test_近期笔记和较早笔记的主流含义不一样时请用户选(self):
        ctx = _ctx(["a"] * 3, ["b"] * 3, recent_hits=6, older_hits=300)
        a, _ = self.run_analyze(TWO_SENSES.format(a="4,5,6", b="1,2,3", recent="-"), ctx)
        self.assertEqual(a["decision"], "ask")
        self.assertIn("最近 90 天", a["reason"])

    def test_近期笔记权重更高_两边差不多时请用户选(self):
        ctx = _ctx(["a"] * 2, ["b"] * 4, recent_hits=2, older_hits=4)
        # A 只在较早里出现 2 段，B 近期 1 段 + 较早 2 段：B 分数更高但不到八成
        a, _ = self.run_analyze(TWO_SENSES.format(a="5,6", b="2,3,4", recent="-"), ctx)
        self.assertEqual(a["decision"], "ask")

    def test_库里没有这个词_近期关注能定就自动定_定不了就问(self):
        a, prompt = self.run_analyze(TWO_SENSES.format(a="-", b="-", recent="B"), _ctx([], []))
        self.assertIn("库里没有找到这个词", prompt)
        self.assertEqual((a["decision"], a["chosen"]), ("auto", "B"))
        a, _ = self.run_analyze(TWO_SENSES.format(a="-", b="-", recent="-"), _ctx([], []))
        self.assertEqual(a["decision"], "ask")

    def test_模型省掉含义前缀_还多塞一个序号_也认得出(self):
        raw = ("A|1|混合专家模型|Mixture of Experts 架构|1,2\nB|2|人名|说唱歌手名|-\n近期|A\n"
               "A|3|MoE\nA|2|inference cost|推理费用\nA|2|H100|2024\nB|3|Kool Moe Dee\n")
        a, _ = self.run_analyze(raw, _ctx(["x", "y"], []))
        self.assertEqual([(x["key"], x["label"], x["meaning"]) for x in a["senses"]],
                         [("A", "混合专家模型", "Mixture of Experts 架构"), ("B", "人名", "说唱歌手名")])
        terms = dict(a["senses"][0]["terms"])
        self.assertEqual((terms["inference cost"], terms["推理费用"], terms["h100"]), (2, 2, 2))
        self.assertEqual((a["decision"], a["chosen"]), ("auto", "A"))

    def test_expand_terms_取自动定下的含义(self):
        info = {}
        with mock.patch.object(llm, "complete", return_value=TWO_SENSES.format(a="1", b="-", recent="-")):
            terms = search.expand_terms("RSI", ctx=_ctx(["x"], []), info=info)
        self.assertEqual(info["meaning"], "AI 自己改进自己")
        self.assertIn(("recursive self-improvement", 3), terms)


class CollectContextsTests(unittest.TestCase):
    def test_近期较早分层_英文按词边界_同标题只取一段_不同来源轮流取(self):
        with tempfile.TemporaryDirectory() as root:
            notes = [
                dict(_note(root, "Spark/甲/a.md", "PRSI 和 RSIX 都不算。"), date="2026-10-01", folder="Spark/甲"),
                dict(_note(root, "Spark/甲/b.md", "甲台最近讲 RSI。"), date="2026-10-02", folder="Spark/甲"),
                dict(_note(root, "Spark/甲/c.md", "甲台又讲 RSI。"), date="2026-10-03", folder="Spark/甲"),
                dict(_note(root, "Spark/乙/d.md", "乙台讲 RSI。"), date="2026-09-30", folder="Spark/乙"),
                dict(_note(root, "Spark/乙/e.md", "同一期逐字稿 RSI。", title="d"), date="2026-09-30",
                     folder="Spark/乙/transcripts"),
                dict(_note(root, "Old/f.md", "早年的 RSI。"), date="2024-01-01", folder="Old"),
            ]
            ctx = search.collect_contexts(root, "RSI", notes, limit=4, today="2026-10-15")
        self.assertEqual((ctx["recent_hits"], ctx["older_hits"]), (4, 1))
        texts = [r["text"] for r in ctx["samples"]]
        # 近期两段：甲台最新一篇、乙台一篇（不是甲台连取两篇）；较早一段；剩下的名额用近期补
        self.assertEqual(texts[:2], ["甲台又讲 RSI。", "乙台讲 RSI。"])
        self.assertIn("早年的 RSI。", texts)
        self.assertNotIn("同一期逐字稿 RSI。", texts)
        self.assertTrue(all("PRSI" not in t for t in texts))


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
