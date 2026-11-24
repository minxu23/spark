"""给已有整理稿补标发言人：只加名字、按句子拆段，原文译文一个字都不能变。"""

import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import label_speakers as ls
from apps.summit2md import pipeline

DOC = """# Wiz Talk

- 所属节目：a16z - Videos
- 链接：https://www.youtube.com/watch?v=PbD9pVXxsYM
- 播出时间：2026-11-13
- 发言人：Wiz’s Cofounder（根据标题解析，单人演讲）
- 说明：本文由 AI 整理

## 演讲稿

Thanks for joining us. Can you walk us through it? Yes. First, great to be here.

> 感谢参加。你能讲讲吗？是的。首先，很高兴来到这里。

We are calling it Black October.

> 我们称之为黑色十月。

What does the next year look like?

> 明年会怎样？
"""

REPLY = '''1: Sarah Wang >> Ami Luttwak @ "Yes. First, great" @ "是的。首先"
2: Ami Luttwak
3: Sarah Wang
'''

LLM = {"backend": "api", "api_key": "k", "model": "m"}


class LabelFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.show = os.path.join(self.tmp.name, "a16z - Videos")
        os.makedirs(os.path.join(self.show, "speech"))
        self.path = os.path.join(self.show, "speech", "20261113_Wiz.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(DOC)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, reply, **kw):
        with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
             mock.patch.object(ls, "host_names_for", return_value=""), \
             mock.patch.object(pipeline, "_cached_summarize", return_value=reply):
            return ls.label_file(self.path, LLM, **kw)

    def test_标名字并按句子拆开_原文译文不变(self):
        r = self._run(REPLY)
        self.assertEqual(r["split"], 1)
        out = open(self.path, encoding="utf-8").read()
        self.assertIn("**Sarah Wang**: Thanks for joining us. Can you walk us through it?\n\n"
                      "> **Sarah Wang**：感谢参加。你能讲讲吗？\n\n"
                      "**Ami Luttwak**: Yes. First, great to be here.\n\n"
                      "> **Ami Luttwak**：是的。首先，很高兴来到这里。\n\n"
                      "We are calling it Black October.\n\n> 我们称之为黑色十月。\n\n"
                      "**Sarah Wang**: What does", out)
        self.assertIn(ls.AI_LABEL_LINE, out)
        self.assertNotIn("根据标题解析", out)
        # 不留备份，只记下标过哪一篇（--fix-roles 靠它认）
        self.assertFalse(os.path.exists(os.path.join(self.show, ".cache", "speaker_backup")))
        self.assertEqual(ls._labelled_names(self.show), {"20261113_Wiz.md"})

    def test_换人位置找不到就不拆_照样标(self):
        r = self._run(REPLY.replace("Yes. First, great", "Nope not here"))
        self.assertEqual((r["split"], r["split_skipped"]), (0, 1))
        self.assertIn("**Sarah Wang**: Thanks for joining us.", open(self.path, encoding="utf-8").read())

    def test_模型漏答太多就不改文件(self):
        with self.assertRaises(ls.LabelError):
            self._run("1: Sarah Wang\n")
        self.assertEqual(open(self.path, encoding="utf-8").read(), DOC)

    def test_标过的跳过_试运行不写文件(self):
        r = self._run(REPLY, dry_run=True)
        self.assertIn("**Ami Luttwak**", r["preview"])
        self.assertEqual(open(self.path, encoding="utf-8").read(), DOC)
        self._run(REPLY)
        self.assertEqual(self._run(REPLY), {"skipped": "已经标过发言人"})

    def test_划线和批注留在原处(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(DOC.replace("Black October.", "==Black October==[^note-1].")
                    + "\n## 读后感\n\n好\n\n[^note-1]: 有意思\n")
        self._run(REPLY)
        out = open(self.path, encoding="utf-8").read()
        self.assertIn("We are calling it ==Black October==[^note-1].", out)
        self.assertTrue(out.rstrip().endswith("[^note-1]: 有意思"))
        self.assertIn("## 读后感\n\n好", out)


class MissingParagraphTests(unittest.TestCase):
    def _run(self, replies):
        with tempfile.TemporaryDirectory() as root:
            show = os.path.join(root, "a16z - Videos")
            os.makedirs(os.path.join(show, "speech"))
            path = os.path.join(show, "speech", "x.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(getattr(self, "doc", DOC))
            with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
                 mock.patch.object(ls, "host_names_for", return_value=""), \
                 mock.patch.object(pipeline, "_cached_summarize", side_effect=replies) as llm:
                r = ls.label_file(path, LLM)
            return r, llm, open(path, encoding="utf-8").read()

    def test_漏答的段落单独补问(self):
        r, llm, out = self._run(["1: Sarah Wang\n3: Sarah Wang\n", "2: Ami Luttwak\n"])
        self.assertEqual(llm.call_count, 2)
        self.assertIn("[2] We are calling it", llm.call_args.args[0])
        self.assertEqual(r["guessed"], 0)
        self.assertIn("**Ami Luttwak**: We are calling it", out)

    def test_补问还缺一段就沿用前一段(self):
        self.doc = DOC + "\nIt changes everything.\n\n> 这改变了一切。\n\nAnd fast.\n\n> 而且很快。\n"
        r, _, out = self._run(["1: Sarah Wang\n2: Ami Luttwak\n3: Sarah Wang\n5: Sarah Wang\n", ""])
        self.assertEqual(r["guessed"], 1)
        self.assertIn("**Sarah Wang**: What does the next year look like?\n\n> **Sarah Wang**：明年会怎样？\n\n"
                      "It changes everything.\n\n> 这改变了一切。\n\nAnd fast.", out)


class FixRolesTests(unittest.TestCase):
    def test_Host_统一成角色后换成认出的名字(self):
        with tempfile.TemporaryDirectory() as root:
            show = os.path.join(root, "a16z - Videos")
            os.makedirs(os.path.join(show, "speech"))
            path = os.path.join(show, "speech", "x.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(DOC)
            with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
                 mock.patch.object(ls, "host_names_for", return_value=""), \
                 mock.patch.object(pipeline, "_cached_summarize", return_value="1: Host\n2: Ami Luttwak\n3: Sarah Wang\n"):
                ls.label_file(path, LLM)
            self.assertIn("**主持人**: Thanks", open(path, encoding="utf-8").read())   # Host 已经统一成主持人
            with mock.patch.object(ls, "host_names_for", return_value=""), \
                 mock.patch.object(pipeline, "_cached_summarize", return_value="主持人: Sarah Wang\n") as llm:
                r = ls.fix_roles(path, LLM)
            self.assertEqual(r["left"], [])
            out = open(path, encoding="utf-8").read()
            self.assertIn("**Sarah Wang**: Thanks for joining us.", out)
            self.assertIn("> **Sarah Wang**：感谢参加。", out)
            self.assertEqual(ls.fix_roles(path, LLM), {"skipped": "没有角色标签"})


class ChapterHeadingTests(unittest.TestCase):
    def test_正文里的章节标题原样保留_标题后面重新标名字(self):
        with tempfile.TemporaryDirectory() as root:
            show = os.path.join(root, "a16z - Videos")
            os.makedirs(os.path.join(show, "speech"))
            path = os.path.join(show, "speech", "x.md")
            doc = DOC.replace("## 演讲稿\n\n", "## 演讲稿\n\n## Opening\n\n> ## 开场\n\n").replace(
                "We are calling it", "## Part Two\n\n> ## 第二部分\n\nWe are calling it")
            with open(path, "w", encoding="utf-8") as f:
                f.write(doc)
            with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
                 mock.patch.object(ls, "host_names_for", return_value=""), \
                 mock.patch.object(pipeline, "_cached_summarize",
                                   return_value='2: Sarah Wang >> Ami Luttwak @ "Yes. First, great" @ "是的。首先"\n'
                                                '4: Ami Luttwak\n5: Sarah Wang\n') as llm:
                ls.label_file(path, LLM)
            self.assertNotIn("[1]", llm.call_args.args[0])   # 标题不当成发言交给模型
            out = open(path, encoding="utf-8").read()
            self.assertIn("## 演讲稿\n\n## Opening\n\n> ## 开场\n\n**Sarah Wang**: Thanks", out)
            self.assertIn("## Part Two\n\n> ## 第二部分\n\n**Ami Luttwak**: We are calling it", out)


class RoleToNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        show = os.path.join(self.tmp.name, "a16z - Videos")
        os.makedirs(os.path.join(show, "speech"))
        self.path = os.path.join(show, "speech", "20261113_Wiz.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(DOC)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, replies):
        with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
             mock.patch.object(ls, "host_names_for", return_value=""), \
             mock.patch.object(pipeline, "_cached_summarize", side_effect=replies) as llm:
            ls.label_file(self.path, LLM)
        return llm, open(self.path, encoding="utf-8").read()

    def test_认出了名字的角色换成名字(self):
        llm, out = self._run(["1: 主持人\n2: Ami Luttwak\n3: Sarah Wang\n", "主持人: Sarah Wang\n"])
        self.assertEqual(llm.call_count, 2)
        self.assertNotIn("**主持人**", out)
        self.assertIn("**Sarah Wang**: Thanks for joining us.", out)

    def test_对不上就保留角色_名字以外的回答不采用(self):
        _, out = self._run(["1: 主持人\n2: Ami Luttwak\n3: Ami Luttwak\n", "主持人: Someone Else\n"])
        self.assertIn("**主持人**: Thanks for joining us.", out)

    def test_全部有名字就不多问(self):
        llm, _ = self._run(["1: Sarah Wang\n2: Ami Luttwak\n3: Sarah Wang\n"])
        self.assertEqual(llm.call_count, 1)


TRANSCRIPT = """# Wiz Talk

- 链接：https://www.youtube.com/watch?v=PbD9pVXxsYM
- 播出时间：2026-11-13
- 字幕来源：YouTube 自动生成字幕（en-orig）
- 发言人：Wiz’s Cofounder（根据标题解析，单人演讲）
- 整理稿见：[x](../speech/20261113_Wiz.md)

## 文字记录

**[0:00](https://y/t=0s)**  
um thanks for joining us can you walk us through it

**[0:05](https://y/t=5s)**  
yes first great to be here we are calling it black october

**[0:12](https://y/t=12s)**  
what does the next year look like
"""

NOTE = """# Wiz Talk

- 发言人：Wiz’s Cofounder（根据标题解析，单人演讲）

==划线==[^note-1]

[^note-1]: 批注
"""


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.show = os.path.join(self.tmp.name, "a16z - Videos")
        for d in ("speech", "transcripts", "notes"):
            os.makedirs(os.path.join(self.show, d))
        self.speech = os.path.join(self.show, "speech", "20261113_Wiz.md")
        self.transcript = os.path.join(self.show, "transcripts", "20261113_Wiz.md")
        self.note = os.path.join(self.show, "notes", "20261113 Wiz.md")
        with open(self.speech, "w", encoding="utf-8") as f:
            f.write(DOC.replace("- 说明：本文由 AI 整理", "- 说明：本文由 AI 整理\n- 单集笔记：[打开笔记](<../notes/20261113 Wiz.md>)"))
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(TRANSCRIPT)
        with open(self.note, "w", encoding="utf-8") as f:
            f.write(NOTE)
        with mock.patch.object(ls, "episode_context", return_value={"description": "", "turns": []}), \
             mock.patch.object(ls, "host_names_for", return_value=""), \
             mock.patch.object(pipeline, "_cached_summarize", return_value=REPLY):
            ls.label_file(self.speech, LLM)

    def tearDown(self):
        self.tmp.cleanup()

    def test_文字记录按整理稿分上发言人_正文不变(self):
        r = ls.label_transcript(self.speech)
        self.assertEqual(r["turns"], 3)
        out = open(self.transcript, encoding="utf-8").read()
        self.assertIn("## 文字记录\n\n### 🗣️ Sarah Wang\n\n**[0:00]", out)
        self.assertIn("### 🗣️ Ami Luttwak\n\n**[0:05]", out)
        self.assertIn("### 🗣️ Sarah Wang\n\n**[0:12]", out)
        self.assertIn(ls.AI_LABEL_LINE, out)
        self.assertNotIn("根据标题解析", out)
        paras, speakers, mode = pipeline._parse_transcript_body(out)
        self.assertEqual([t for _, t in paras], [t for _, t in pipeline._parse_transcript_body(TRANSCRIPT)[0]])
        self.assertEqual(speakers, ["Sarah Wang", "Ami Luttwak", "Sarah Wang"])
        self.assertEqual(ls.label_transcript(self.speech), {"skipped": "文字记录已经分了发言人"})

    def test_笔记只改开头那一行_批注不动(self):
        self.assertTrue(ls.fix_note_header(self.speech))
        out = open(self.note, encoding="utf-8").read()
        self.assertEqual(out, NOTE.replace("- 发言人：Wiz’s Cofounder（根据标题解析，单人演讲）", ls.AI_LABEL_LINE))


class HostNamesTests(unittest.TestCase):
    def test_几位主持人用逗号顿号都行(self):
        self.assertEqual(pipeline.parse_host_names("Chamath, Jason，Sacks、 Friedberg"),
                         ["Chamath", "Jason", "Sacks", "Friedberg"])
        self.assertIn("固定主持人：Chamath、Jason", pipeline.host_prompt_line("Chamath, Jason"))
        self.assertIn("主持人：张小珺（", pipeline.host_prompt_line("张小珺"))

    def test_几位主持人时不把主持人换成某一个(self):
        self.assertEqual(pipeline.apply_host_name(["主持人", "嘉宾"], [(0, "a"), (1, "b")], "A, B", "show",
                                                  "api", "k", "m"), ["主持人", "嘉宾"])


if __name__ == "__main__":
    unittest.main()
