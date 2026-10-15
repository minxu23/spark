"""演示新版：数字页 / 横条对比、不做章节分隔页、风格参数（默认瑞士网格 + 一句话描述）。"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock

from apps.notes2insight import deck, deck_theme, llm


SOURCES = [{"idx": 1, "title": "t1", "date": "2026-01-01", "folder": "f", "rel": "a.md"},
           {"idx": 2, "title": "t2", "date": "", "folder": "f", "rel": "b.md"}]


def _deck(**kw):
    d = {"title": "报告", "subtitle": "副标题", "meta": {}, "sources": SOURCES, "slides": [
        {"kind": "points", "section": "开篇", "title": "一", "lead": "", "note": "n",
         "bullets": [{"text": "要点", "cites": [1]}]},
        {"kind": "stat", "section": "数字", "title": "二", "lead": "", "note": "",
         "items": [{"value": "5GW", "label": "规模", "cites": [1]}, {"value": "3-4×", "label": "增速", "cites": []}]},
        {"kind": "bars", "section": "价格", "title": "三", "lead": "", "note": "", "unit": "$M/MW",
         "items": [{"label": "现在", "value": 12.5, "display": "$10-15M", "cites": [2]},
                   {"label": "目标", "value": 100, "display": "$100M", "cites": []}]},
        {"kind": "timeline", "section": "情景", "title": "四", "lead": "", "note": "",
         "events": [{"date": "2026", "text": "x", "cites": []}, {"date": "2027", "text": "y", "cites": [1]}]},
    ]}
    d.update(kw)
    return d


class NormalizeV2Tests(unittest.TestCase):
    def test_数字页和横条对比_数字不全的降级或丢掉_章节分隔页不要(self):
        slides = deck.normalize({"slides": [
            {"kind": "section", "title": "第 1 章"},
            {"kind": "stat", "title": "数字", "items": [{"value": "5GW", "label": "规模", "cites": [1, 9]},
                                                        {"value": "", "label": "空的"}]},
            {"kind": "stat", "title": "全空", "items": [{"label": "没数字"}]},
            {"kind": "bars", "title": "对比", "unit": "$M",
             "items": [{"label": "A", "value": "12.5", "display": "$12.5M"}, {"label": "B", "value": 100}]},
            {"kind": "bars", "title": "凑不成", "items": [{"label": "A", "value": "很多", "display": "很多"},
                                                         {"label": "B", "value": 3, "display": "3 家"}]},
        ]}, SOURCES)
        self.assertEqual([(s["kind"], s["title"]) for s in slides],
                         [("stat", "数字"), ("bars", "对比"), ("points", "凑不成")])
        self.assertEqual(slides[0]["items"], [{"value": "5GW", "label": "规模", "cites": [1]}])
        self.assertEqual([i["value"] for i in slides[1]["items"]], [12.5, 100.0])
        self.assertEqual(slides[1]["items"][1]["display"], "100")
        self.assertEqual(slides[2]["bullets"], [{"text": "B：3 家", "cites": []}])

    def test_时间轴的日期不是时间就退成要点页(self):
        slides = deck.normalize({"slides": [
            {"kind": "timeline", "title": "清单", "events": [{"date": f"信号{i}", "text": f"看{i}", "cites": [1]} for i in range(1, 5)]},
            {"kind": "timeline", "title": "真时间轴", "events": [{"date": "2026-03", "text": "a"}, {"date": "明年", "text": "b"},
                                                                {"date": "Q3", "text": "c"}]},
        ]}, SOURCES)
        self.assertEqual([s["kind"] for s in slides], ["points", "timeline"])
        self.assertEqual(slides[0]["bullets"][0], {"text": "看1", "cites": [1]})

    def test_横条对比单位不一样就退成文字页(self):
        self.assertEqual([deck._unit_of(x) for x in ("$10-15M", "$100M", "约4-5年", "1.5×", "3倍", "70%-80%")],
                         ["$m", "$m", "年", "×", "×", "%"])
        slides = deck.normalize({"slides": [{"kind": "bars", "title": "混了", "items": [
            {"label": "下限", "value": 1.5, "display": "1.5×"}, {"label": "上限", "value": 3, "display": "3倍"},
            {"label": "压缩", "value": 4.5, "display": "约4-5年"}]}]}, SOURCES)
        self.assertEqual(slides[0]["kind"], "points")
        self.assertEqual(len(slides[0]["bullets"]), 3)


class ThemeTests(unittest.TestCase):
    def test_默认是瑞士网格_不合规的值退回默认_对比度不够整组颜色退回(self):
        t = deck_theme.clean_theme({"accent": "#d9480f", "font": "comic", "radius": 99, "title_weight": 650,
                                    "evil": "x"})
        self.assertEqual((t["accent"], t["font"], t["radius"], t["title_weight"]), ("#d9480f", "sans", 0, 600))
        self.assertNotIn("evil", t)
        dark = deck_theme.clean_theme({"bg": "#111111", "ink": "#f5f5f5", "dim": "#a0a0a0", "accent": "#ff8a3d"})
        self.assertEqual(dark["bg"], "#111111")
        unreadable = deck_theme.clean_theme({"bg": "#ffffff", "ink": "#eeeeee"})
        self.assertEqual(unreadable["ink"], deck_theme.DEFAULT_THEME["ink"])

    def test_信息密度_默认高_管写稿也管字号(self):
        self.assertEqual(deck_theme.DEFAULT_THEME["density"], "high")
        self.assertEqual(deck_theme.clean_theme({"density": "low"})["density"], "low")
        self.assertEqual(deck_theme.clean_theme({"density": "超高"})["density"], "high")
        self.assertIn("--dens:0.82", deck_theme.css_vars({}))
        self.assertIn("信息密度高", deck_theme.describe({}))
        md = ("# 报告\n\n## 副标题\n\n## 执行摘要\n\n" + "这一段讲执行摘要的主要发现。" * 40
              + "\n\n## 第1章 甲\n\n" + "第一章正文。" * 40)
        seen = []

        def fake(prompt, *a, **k):
            seen.append(prompt)
            if "这一步只做**规划**" in prompt:
                return '{"pages": [{"part": "P2", "kind": "points", "title": "t", "brief": "b"}]}'
            return '{"slides": [{"kind": "points", "title": "t", "bullets": [{"text": "x", "cites": []}]}]}'
        for dens, want in (("high", "一页 4-6 条"), ("low", "一页 2-3 条")):
            seen.clear()
            with mock.patch.object(llm, "complete", side_effect=fake):
                deck.generate(md, backend="api", theme={"density": dens})
            self.assertEqual(len(seen), 2)
            self.assertIn(want, seen[1])

    def test_一句话描述转成风格参数_空描述不调模型(self):
        with mock.patch.object(llm, "complete") as m:
            self.assertEqual(deck_theme.theme_from_description("", backend="api")["name"], "瑞士网格")
            m.assert_not_called()
        raw = '好的：{"name": "深色", "bg": "#121212", "ink": "#f0f0f0", "dim": "#a8a8a8", "accent": "#ff8a3d", "display_font": "serif"}'
        with mock.patch.object(llm, "complete", return_value=raw):
            t = deck_theme.theme_from_description("深色背景、橙色点缀、标题衬线", backend="api")
        self.assertEqual((t["bg"], t["accent"], t["display_font"], t["description"]),
                         ("#121212", "#ff8a3d", "serif", "深色背景、橙色点缀、标题衬线"))


class RenderV2Tests(unittest.TestCase):
    def test_html带风格变量_风格存进去读得回来_老演示的分隔页和配图去掉(self):
        theme = deck_theme.clean_theme({"accent": "#d9480f"})
        html = deck.render_html(_deck(theme=theme))
        self.assertIn("--accent:#d9480f", html)
        self.assertNotIn("__THEME_VARS__", html)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.deck.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(html)
            back = deck.load_deck_from_html(p)
            self.assertEqual(back["theme"]["accent"], "#d9480f")
            self.assertEqual([s["kind"] for s in back["slides"]], ["points", "stat", "bars", "timeline"])
            old = html.replace('"kind": "points"', '"kind": "section"', 1).replace('"theme":', '"x_theme":')
            with open(p, "w", encoding="utf-8") as f:
                f.write(old)
            back = deck.load_deck_from_html(p)
        self.assertEqual(back["theme"]["name"], "瑞士网格")        # 老演示没存风格 → 默认
        self.assertNotIn("section", [s["kind"] for s in back["slides"]])

    def test_pptx_新页面类型都能导出(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.pptx")
            deck.render_pptx(_deck(theme={"accent": "#d9480f"}), out)
            from pptx import Presentation
            prs = Presentation(out)
        self.assertEqual(len(prs.slides), 6)    # 封面 + 4 页 + 来源
        texts = " ".join(sh.text_frame.text for sh in prs.slides[2].shapes if sh.has_text_frame)
        self.assertIn("5GW", texts)


if __name__ == "__main__":
    unittest.main()


class TwoStepTests(unittest.TestCase):
    MD = ("# 报告\n\n## 副标题\n\n## 执行摘要\n\n摘要正文。\n\n## 关键判断\n\n判断正文。\n\n"
          + "".join(f"## 第{i}章 题{i}\n\n" + f"第{i}章的正文，后面几段才有数字 42%。" * 500 + "\n\n" for i in (1, 2, 3))
          + "## 方法、来源与局限\n\n不送。\n")

    def test_各部分全文都送模型_按部分分组写稿_顺序按规划(self):
        sk = deck.parts(self.MD)
        self.assertEqual([p["head"][:4] for p in sk["parts"]], ["执行摘要", "关键判断", "第1章 ", "第2章 ", "第3章 "])
        self.assertTrue(all("方法" not in p["head"] for p in sk["parts"]))
        plan = '{"pages": [' + ",".join(
            f'{{"part": "{k}", "kind": "points", "title": "页{n}", "brief": ""}}'
            for n, k in enumerate(["P1", "P3", "P3", "P4", "P5", "XX"])) + "]}"
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            if "这一步只做**规划**" in prompt:
                self.assertIn("后面几段才有数字 42%", prompt)
                return plan
            n = int(re.search(r"只写下面这 (\d+) 页", prompt).group(1))
            titles = re.findall(r"\] (页\d+)\n", prompt)
            return json.dumps({"slides": [{"kind": "points", "title": t, "bullets": [{"text": "x"}]}
                                          for t in titles[:n]]}, ensure_ascii=False)
        with mock.patch.object(llm, "complete", side_effect=fake):
            d = deck.build_deck(self.MD, backend="api")
        self.assertEqual([s["title"] for s in d["slides"]], [f"页{i}" for i in range(6)])
        writes = [p for p in prompts if "只写下面这" in p]
        self.assertGreaterEqual(len(writes), 3)          # 三章各自够长，不会合成一组
        self.assertTrue(any("第3章的正文" in p and "第1章的正文" not in p for p in writes))

    def test_新版式_表格_链条_矩阵_引语_撑不起来就退成要点(self):
        raw = {"slides": [
            {"kind": "table", "title": "表", "columns": ["维度", "A", "B"],
             "rows": [{"cells": ["成本", "高"], "cites": [1]}, {"cells": ["速度", "快", "慢"], "cites": [9]}]},
            {"kind": "table", "title": "单行", "columns": ["a", "b"], "rows": [{"cells": ["x", "y"]}]},
            {"kind": "chain", "title": "链", "steps": [{"head": "一", "text": "a"}, {"head": "二", "text": "b"}]},
            {"kind": "matrix", "title": "缺格", "cells": [{"head": "左上", "text": "t"}]},
            {"kind": "matrix", "title": "四格", "x": "慢→快", "y": "低→高",
             "cells": [{"head": str(i), "text": "t"} for i in range(4)]},
            {"kind": "quote", "title": "引", "quote": "原话", "by": "某人", "cites": [1]},
            {"kind": "quote", "title": "空引", "quote": ""},
        ]}
        sl = deck.normalize(raw, SOURCES)
        self.assertEqual([s["kind"] for s in sl], ["table", "points", "chain", "points", "matrix", "quote"])
        self.assertEqual(sl[0]["rows"][0]["cells"], ["成本", "高", ""])
        self.assertEqual(sl[1]["bullets"][0]["text"], "x：y")
        d = _deck()
        d["slides"] = sl
        html = deck.render_html(d)
        for kind in ("table", "chain", "matrix", "quote"):
            self.assertIn(f'"kind": "{kind}"', html)
        with tempfile.TemporaryDirectory() as tmp:
            out = deck.render_pptx(d, os.path.join(tmp, "x.pptx"))
            self.assertTrue(os.path.getsize(out) > 10000)
