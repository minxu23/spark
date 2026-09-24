"""整理稿分块生成、翻译分批：长节目不再只整理开头一段。模型调用全部 mock 掉。"""

import re
import threading
import unittest
from unittest import mock

from apps.summit2md import pipeline

ENTRY = {"id": "abc", "title": "T", "url": "https://www.youtube.com/watch?v=abc"}


def _paras(n, size=100):
    return [(float(i), f"p{i} " + "x" * size) for i in range(n)]


class ChunkTests(unittest.TestCase):
    def test_只有一块时和整篇文本逐字相同(self):
        paras = _paras(5)
        speakers = ["A", "A", "B", "B", "A"]
        self.assertEqual(
            pipeline.build_transcript_chunks(paras, speakers, "multi", 100000),
            [pipeline.build_transcript_text_for_speech(paras, speakers, "multi")])

    def test_按上限切块且每块开头补上当前发言人(self):
        paras = _paras(10)
        speakers = ["A"] * 10
        chunks = pipeline.build_transcript_chunks(paras, speakers, "multi", 350)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(c.startswith("[A]\n"))
            self.assertLessEqual(len(c), 350 + 10)
        joined = "\n".join(chunks)
        for _, t in paras:
            self.assertIn(t, joined)


class GenerateTests(unittest.TestCase):
    def test_长节目分块整理后拼成完整原文再逐段翻译(self):
        paras = _paras(600, 100)   # 约 6 万字，要切成几块
        calls = []
        lock = threading.Lock()

        def fake(prompt, *a, **kw):
            with lock:
                calls.append(prompt)
            if "专业译者" in prompt:
                n = int(re.search(r"共 (\d+) 段", prompt).group(1))
                return "\n\n".join(f"[{j}] 译{j}" for j in range(1, n + 1))
            part = re.search(r"第 (\d+)/(\d+) 部分", prompt)
            return f"段落{part.group(1)}-1\n\n段落{part.group(1)}-2"

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            text, mode = pipeline.generate_speech_script(
                ENTRY, paras, None, None, "en", "bilingual", "api", "", "m")
        self.assertEqual(mode, "bilingual")
        n_chunks = len(pipeline.build_transcript_chunks(paras, None, None, pipeline.SPEECH_CHUNK_CHARS["original"]))
        self.assertGreater(n_chunks, 1)
        blocks = pipeline._split_speech_paragraphs(text)
        originals = [b for b in blocks if not b.startswith(">")]
        self.assertEqual(originals, [f"段落{k}-{j}" for k in range(1, n_chunks + 1) for j in (1, 2)])
        self.assertEqual(sum(1 for b in blocks if b.startswith(">")), len(originals))

    def test_中文节目全中文不翻译(self):
        with mock.patch.object(pipeline, "_cached_summarize", return_value="整理后的中文") as m:
            text, mode = pipeline.generate_speech_script(
                ENTRY, _paras(3), None, None, "zh-Hans", "bilingual", "api", "", "m")
        self.assertEqual((text, mode), ("整理后的中文", "zh"))
        self.assertEqual(m.call_count, 1)


class RenderLabelTests(unittest.TestCase):
    def test_中文节目标成中文整理_外文转中文仍是翻译(self):
        entry = dict(ENTRY, duration=60)
        zh = pipeline.render_speech_md(entry, "S", "正文", None, None, speech_lang_mode="zh", sub_lang="zh-Hans")
        en = pipeline.render_speech_md(entry, "S", "正文", None, None, speech_lang_mode="zh", sub_lang="en")
        self.assertIn("（中文整理）", zh)
        self.assertIn("（中文翻译）", en)


class TranslateTests(unittest.TestCase):
    def test_分批翻译且漏掉的段落补译一次(self):
        paras = [f"para {i} " + "y" * 3000 for i in range(1, 9)]   # 每批放 3 段左右
        state = {"first": True}

        def fake(prompt, *a, force=False, **kw):
            nums = [int(x) for x in re.findall(r"^\[(\d+)\] para", prompt, re.M)]
            real = [int(x) for x in re.findall(r"^\[\d+\] para (\d+)", prompt, re.M)]
            out = []
            for j, r in zip(nums, real):
                if r == 5 and not force:
                    continue            # 第一次漏掉第 5 段
                out.append(f"[{j}] 译{r}")
            return "\n\n".join(out)

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            got = pipeline._translate_speech_paragraphs(paras, "英语", "api", "", "m", "")
        self.assertEqual(got, {i: f"译{i}" for i in range(1, 9)})

    def test_中文文章不做对照(self):
        with mock.patch.object(pipeline, "_cached_summarize", side_effect=AssertionError):
            self.assertIsNone(pipeline.bilingual_article_text(_paras(2), "zh", "api", "", "m"))

    def test_英文文章逐段配中文(self):
        with mock.patch.object(pipeline, "_cached_summarize", return_value="[1] 甲\n\n[2] 乙"):
            text = pipeline.bilingual_article_text([(0.0, "A"), (0.0, "B")], "en", "api", "", "m")
        self.assertEqual(text, "A\n\n> 甲\n\nB\n\n> 乙")


if __name__ == "__main__":
    unittest.main()
