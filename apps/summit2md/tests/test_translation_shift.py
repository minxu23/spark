"""整理稿对照翻译整体错位：模型整段跳过某一段，之后的编号各减一。模型调用全部 mock 掉。"""

import re
import unittest
from unittest import mock

from apps.summit2md import pipeline

PARAS = [
    "**Host**：Thank you all for coming today, we have a great program.",
    "[applause]",
    "**Guest**：Thanks for having me, it is great to be here with everyone.",
    "**Host**：Let me start with a question about how you got into this field.",
    "**Guest**：I started out as an engineer and slowly moved toward research.",
]
ZH = {PARAS[0]: "感谢大家今天到场。", PARAS[2]: "谢谢邀请。", PARAS[3]: "先问个问题。", PARAS[4]: "我从工程师做起。"}


def _skip_and_renumber(skip):
    """假模型：整段跳过 skip 里的段落，后面的编号各减一。"""
    def fake(prompt, *a, **kw):
        sent = re.findall(r"^\[(\d+)\] (.*)$", prompt, re.M)
        out = [ZH.get(t, "译" + t[:4]) for _, t in sent if t not in skip]
        return "\n\n".join(f"[{i}] {t}" for i, t in enumerate(out, start=1))
    return fake


class ShiftTests(unittest.TestCase):
    def test_跳过舞台提示并重新编号_不能把译文错位收下(self):
        with mock.patch.object(pipeline, "_cached_summarize", side_effect=_skip_and_renumber({"[applause]"})):
            got = pipeline._translate_speech_paragraphs(PARAS, "英语", "api", "", "m", "")
        self.assertEqual(got, {1: ZH[PARAS[0]], 3: ZH[PARAS[2]], 4: ZH[PARAS[3]], 5: ZH[PARAS[4]]})

    def test_跳过普通段落并重新编号_整批不收_拆小了补译(self):
        paras = [PARAS[0], "Right, so.", PARAS[2], PARAS[3], PARAS[4]]
        with mock.patch.object(pipeline, "_cached_summarize", side_effect=_skip_and_renumber({"Right, so."})):
            got = pipeline._translate_speech_paragraphs(paras, "英语", "api", "", "m", "")
        # 多段一批时跳过那段，整批不收；拆成单段重试后每段对得上，被跳过的那段只剩原文
        self.assertEqual(got, {1: ZH[PARAS[0]], 3: ZH[PARAS[2]], 4: ZH[PARAS[3]], 5: ZH[PARAS[4]]})

    def test_舞台提示不送去翻译(self):
        seen = []

        def fake(prompt, *a, **kw):
            seen.append(prompt)
            return _skip_and_renumber(set())(prompt)

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            pipeline._translate_speech_paragraphs(PARAS, "英语", "api", "", "m", "")
        self.assertNotIn("[applause]", seen[0])
        self.assertIn("[4] **Guest**：I started out", seen[0])    # 批内编号连续，不留空号

    def test_分开保存时舞台提示照样送去翻译(self):
        seen = []

        def fake(prompt, *a, **kw):
            seen.append(prompt)
            return "\n\n".join(f"[{i}] 译" for i in range(1, 6))

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            got = pipeline._translate_speech_paragraphs(PARAS, "英语", "api", "", "m", "", send_all=True)
        self.assertIn("[applause]", seen[0])
        self.assertEqual(set(got), {1, 2, 3, 4, 5})

    def test_译文长度离谱的不收(self):
        long_zh = "很长的译文" * 40
        with mock.patch.object(pipeline, "_cached_summarize", return_value=f"[1] {long_zh}"):
            got = pipeline._translate_speech_paragraphs(["Yeah, right."], "英语", "api", "", "m", "")
        self.assertEqual(got, {})

    def test_舞台提示不算缺译文(self):
        paras = ["**Host**：Thank you all.", "[applause]", "**Host**：[Laughter]", "[Music] [applause]", "[see note](http://x.com/a)"]
        self.assertEqual([pipeline.lacks_translation(paras, i) for i in range(5)], [True, False, False, False, False])

    def test_改好的段落那条路_编号不全整批不动(self):
        text = "第一段 hello there my friend.\n\n第二段 another english sentence here.\n\n第三段 and one more english line."

        def fake(prompt, *a, **kw):
            return "[1] 第一段 你好朋友。\n\n[2] 第三段 又一句。"   # 跳过了第 2 段，把第 3 段的改文放到编号 2

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            out = pipeline.translate_english_stretches(text, "api", "", "m")
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main()


def test_分开保存模式不因译文偏长而缺段():
    """send_all（分开保存）要求每段都有译文：编号齐全时，偏长的译文也收下，不让整份存不下。"""
    paras = ["Short one.", "Another short one."]
    long_zh = "这是一段明显比原文长很多的译文，" * 6

    def fake(prompt, *a, **kw):
        n = len(re.findall(r"^\[(\d+)\] ", prompt, re.M))
        return "\n\n".join(f"[{i}] {long_zh if i == 1 else '另一段。'}" for i in range(1, n + 1))
    with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
        got = pipeline._translate_speech_paragraphs(paras, "英语", "api", "", "m", "", send_all=True)
        plain = pipeline._translate_speech_paragraphs(paras, "英语", "api", "", "m", "")
    assert got[1] == long_zh and 2 in got
    assert 1 not in plain      # 普通模式照旧不收偏长的
