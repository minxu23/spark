"""补翻对照整理稿里没有译文的段落（fill_translations）。模型调用 mock 掉。"""

import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import fill_translations, pipeline

SPEECH = """# T

- 说明：原文/中文对照

## 演讲稿

**Alice**: First paragraph.

> **Alice**：第一段。

**Bob**: Second ==highlighted== paragraph.

Third paragraph.

> 第三段。

Fourth paragraph.

## 我的高亮

- ==highlighted== English quote
"""


class FillTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "show", "speech"))
        self.path = os.path.join(tmp, "show", "speech", "ep.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(SPEECH)

    def test_找出没有译文的段落_高亮一节不算(self):
        spans = fill_translations.missing_spans(SPEECH)
        self.assertEqual([SPEECH[a:b] for a, b in spans],
                         ["**Bob**: Second ==highlighted== paragraph.", "Fourth paragraph."])

    def test_不是对照稿不管(self):
        self.assertEqual(fill_translations.missing_spans("## 演讲稿\n\nOnly English.\n"), [])

    def test_整篇没有译文的对照稿也找得出来(self):
        text = "- 说明：原文/中文对照\n\n## 演讲稿\n\nFirst one.\n\nSecond one.\n"
        self.assertEqual([text[a:b] for a, b in fill_translations.missing_spans(text)],
                         ["First one.", "Second one."])

    def test_正文小标题后面接着查_高亮一节不查(self):
        text = SPEECH.replace("Fourth paragraph.\n", "## Closing\n\nFourth paragraph.\n")
        self.assertEqual([text[a:b] for a, b in fill_translations.missing_spans(text)],
                         ["**Bob**: Second ==highlighted== paragraph.", "Fourth paragraph."])

    def test_中文发言人标签要翻_单独的图和链接不翻(self):
        text = ("- 说明：原文/中文对照\n\n## 演讲稿\n\n**张三**: Hello there\n\n"
                "![](https://a.com/x.png)\n\nhttps://a.com/page\n\n[Link](https://a.com)\n\n"
                "这段本来就是中文 abc\n")
        self.assertEqual([text[a:b] for a, b in fill_translations.missing_spans(text)],
                         ["**张三**: Hello there"])

    def test_只插入译文_别的不动(self):
        seen = {}

        def fake(prompt, *a, **kw):
            seen["prompt"] = prompt
            return "[1] **鲍勃**: 第二段。\n\n[2] 第四段。"

        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            self.assertEqual(fill_translations.fill(self.path, "api", "m", log=lambda *_: None), 2)
        self.assertNotIn("==", seen["prompt"])
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, SPEECH.replace(
            "**Bob**: Second ==highlighted== paragraph.\n",
            "**Bob**: Second ==highlighted== paragraph.\n\n> **Bob**：第二段。\n").replace(
            "Fourth paragraph.\n", "Fourth paragraph.\n\n> 第四段。\n"))
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(self.path)),
                                                    ".cache", "speech_backup", "ep.md")))
        self.assertEqual(fill_translations.missing_spans(text), [])

    def test_一段也没译出来不改文件(self):
        with mock.patch.object(pipeline, "_cached_summarize", return_value="我无法翻译。"):
            self.assertEqual(fill_translations.fill(self.path, "api", "m", log=lambda *_: None), 0)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), SPEECH)


if __name__ == "__main__":
    unittest.main()
