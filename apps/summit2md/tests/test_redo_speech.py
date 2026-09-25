import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline, redo_speech

TRANSCRIPT = """# T

- 字幕来源：YouTube 自动生成字幕（en），已去重整理

## 文字记录

### 🗣️ A

**[0:00](https://x?t=0)**
{a}

### 🗣️ B

**[0:10](https://x?t=10)**
{b}
"""

HEAD = """# T

- 说明：本文由 AI 基于自动字幕整理为流畅演讲稿（原文/中文对照），请以原始文字记录为准
- 单集笔记：[打开笔记](<../notes/x.md>)

## 演讲稿"""


class RedoSpeechTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.show = os.path.join(self.tmp.name, "Show")
        os.makedirs(os.path.join(self.show, "speech"))
        os.makedirs(os.path.join(self.show, "transcripts"))
        self.a = "Alpha sentence about compute. " * 20
        self.b = "Beta sentence about revenue. " * 20
        with open(os.path.join(self.show, "transcripts", "x.md"), "w") as f:
            f.write(TRANSCRIPT.format(a=self.a, b=self.b))
        self.speech_path = os.path.join(self.show, "speech", "x.md")
        with open(self.speech_path, "w") as f:
            f.write(HEAD + "\n\n**A**: Alpha ==sentence about compute==.\n\n> 阿尔法\n\n**B**: Be")
        with open(os.path.join(self.show, ".manifest.json"), "w") as f:
            json.dump({"entries": {"x": {
                "entry": {"id": "x", "title": "T", "url": "https://x", "duration": 60},
                "relative_path": "transcripts/x.md", "speech_relative_path": "speech/x.md",
            }}}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_flags_cut_off_script(self):
        [item] = redo_speech.scan(self.tmp.name)
        self.assertTrue(item["truncated"])
        self.assertEqual(item["mode"], "bilingual")
        self.assertEqual(item["lang"], "en")
        self.assertEqual(item["speaker_mode"], "multi")

    def test_redo_keeps_header_and_highlights(self):
        [item] = redo_speech.scan(self.tmp.name)
        full = f"**A**: {self.a.strip()}\n\n> 甲\n\n**B**: {self.b.strip()}\n\n> 乙"
        with mock.patch.object(pipeline, "generate_speech_script", return_value=(full, "bilingual")):
            result = redo_speech.redo(item, "cli", "sonnet", log=lambda *_: None)
        with open(self.speech_path) as f:
            content = f.read()
        self.assertTrue(content.startswith(HEAD))
        self.assertIn("==sentence about compute==", content)
        self.assertEqual(content.count("=="), 2)
        self.assertEqual(result["missed_highlights"], [])
        self.assertTrue(os.path.exists(os.path.join(self.show, ".cache", "speech_backup", "x.md")))
        self.assertFalse(redo_speech.scan(self.tmp.name)[0]["truncated"])

    def test_redo_refuses_when_still_short(self):
        [item] = redo_speech.scan(self.tmp.name)
        with open(self.speech_path) as f:
            before = f.read()
        with mock.patch.object(pipeline, "generate_speech_script", return_value=("**A**: Alpha", "bilingual")):
            with self.assertRaises(pipeline.SummarizeError):
                redo_speech.redo(item, "cli", "sonnet", log=lambda *_: None)
        with open(self.speech_path) as f:
            self.assertEqual(f.read(), before)

    def test_redo_refuses_when_translation_failed(self):
        [item] = redo_speech.scan(self.tmp.name)
        full = f"**A**: {self.a}\n\n**B**: {self.b}"
        with mock.patch.object(pipeline, "generate_speech_script", return_value=(full, "original")):
            with self.assertRaises(pipeline.SummarizeError):
                redo_speech.redo(item, "cli", "sonnet", log=lambda *_: None)

    def test_reapply_highlights(self):
        body, moved, missed = redo_speech.reapply_highlights("one two one", ["one", "one", "zzz"])
        self.assertEqual(body, "==one== two ==one==")
        self.assertEqual(moved, {})
        self.assertEqual(missed, ["zzz"])

    def test_reworded_highlight_lands_on_closest_clause(self):
        body = ("**A**: Something.\n\n> **甲**：开头一句，到明年年底时，增量算力的一半都已流向它们了。后面还有别的话。")
        old = "到明年年底，增量算力的一半就已经流向它们了"
        new_body, moved, missed = redo_speech.reapply_highlights(body, [old])
        self.assertEqual(missed, [])
        self.assertEqual(moved[old], "到明年年底时，增量算力的一半都已流向它们了。")
        self.assertIn("==到明年年底时，增量算力的一半都已流向它们了。==", new_body)
        self.assertIn("开头一句，==", new_body)

    def test_reworded_highlight_updates_note_summary(self):
        note = os.path.join(self.show, "note.md")
        with open(note, "w") as f:
            f.write("# N\n\n## 我的高亮\n\n- 旧的说法（[整理稿](<../speech/x.md>)）\n")
        redo_speech._update_note_highlights(note, {"旧的说法": "新的说法"})
        with open(note) as f:
            self.assertIn("- 新的说法（[整理稿](<../speech/x.md>)）", f.read())

    def test_reworded_highlight_found_through_english_paragraph(self):
        old = "Half goes to them by next year.\n\n> 到明年年底，增量算力的==一半就已经流向它们了==\n\nOther.\n\n> 别的。"
        new = ("Half goes to them by next year-end.\n\n> 答案是，到明年年底，增量算力里就已经有一半流向了它们。"
               "\n\nOther.\n\n> 别的。")
        body, moved, missed = redo_speech.reapply_highlights(new, ["一半就已经流向它们了"], old)
        self.assertEqual(missed, [])
        self.assertIn("==", body.split("Other.")[0])


class ZhPunctuationTest(unittest.TestCase):
    def test_halfwidth_next_to_chinese_becomes_fullwidth(self):
        self.assertEqual(pipeline.zh_punctuation("> **甲**:你看,这是什么?"), "> **甲**：你看，这是什么？")
        self.assertEqual(pipeline.zh_punctuation("芯片,Anthropic则用"), "芯片，Anthropic则用")

    def test_english_and_numbers_untouched(self):
        self.assertEqual(pipeline.zh_punctuation("2,500 and OpenAI, Anthropic: yes"), "2,500 and OpenAI, Anthropic: yes")


if __name__ == "__main__":
    unittest.main()
