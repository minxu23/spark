"""节目（series）的新输出格式：单集小结篇幅跟时长走、单集笔记、节目主页、节目总结增量更新。
模型调用全部 mock 掉。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline

RAW = """TLDR: 一句话结论
嘉宾: Jason (Jason Calacanis，主持人), David Sacks
话题: AI安全, Nike

### 本期要点
- 要点一

### 分段
#### [0:46] 开场
- 说了什么
"""


class EpisodeSummaryTests(unittest.TestCase):
    def test_要点多少跟着时长走_篇幅选项升降一档(self):
        tier = pipeline.series_points_tier
        self.assertEqual(tier(25 * 60, "", "medium"), 0)
        self.assertEqual(tier(60 * 60, "", "medium"), 1)
        self.assertEqual(tier(96 * 60, "", "medium"), 2)
        self.assertEqual(tier(96 * 60, "", "short"), 1)
        self.assertEqual(tier(25 * 60, "", "long"), 1)
        self.assertEqual(tier(None, "a" * 50000, "medium"), 1)     # 没时长按文字量估：约 55 分钟英文
        self.assertEqual(tier(None, "中" * 5000, "medium"), 0)

    def test_提示词里带时间戳_文章没时间戳就不要求(self):
        paras = [(0.0, "hello"), (75.0, "world")]
        p = pipeline.build_series_episode_prompt(role_context="r", title="t", duration=3600, paragraphs=paras,
                                                 max_chars=0, summary_length="medium")
        self.assertIn("[1:15] world", p)
        self.assertIn("6-9 条", p)
        p = pipeline.build_series_episode_prompt(role_context="r", title="t", duration=None,
                                                 paragraphs=[(0.0, "正文")], max_chars=0, summary_length="medium")
        self.assertNotIn("时间戳", p.split("文字记录")[-1][:20])
        self.assertIn("#### 段落标题", p)

    def test_解析出嘉宾和话题_括号里的逗号不拆(self):
        s = pipeline.parse_series_summary(RAW)
        self.assertEqual(s["tldr"], "一句话结论")
        self.assertEqual(s["guests"], ["Jason (Jason Calacanis，主持人)", "David Sacks"])
        self.assertEqual(s["topics"], ["AI安全", "Nike"])
        self.assertTrue(s["body"].startswith("### 本期要点"))

    def test_时间戳变成视频链接_已经是链接的不动(self):
        url = "https://www.youtube.com/watch?v=abc"
        out = pipeline.linkify_timestamps("#### [1:02:03] 段\n见 [0:46](x)", url)
        self.assertIn("[1:02:03](https://www.youtube.com/watch?v=abc&t=3723s)", out)
        self.assertIn("[0:46](x)", out)
        self.assertEqual(pipeline.linkify_timestamps("[0:46]", "https://a.substack.com/p/x"), "[0:46]")


def _row(eid, date, ok=True, summary=None, title=None):
    return {"ok": ok, "rank": 1, "entry": {"id": eid, "title": title or f"第{eid}期", "url":
            f"https://www.youtube.com/watch?v={eid}", "duration": 3600, "publish_date": date},
            "relative_path": f"transcripts/{date}_第{eid}期.md", "speech_relative_path": f"speech/{date}_第{eid}期.md",
            "summary": summary if summary is not None else {"tldr": f"{eid} 的结论", "body": "- 老格式要点"},
            "error": None if ok else "无字幕"}


class NotesAndIndexTests(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(), "某节目")
        os.makedirs(self.dir)

    def test_单集笔记_文件名和内容(self):
        self.assertEqual(pipeline.episode_note_name("transcripts/20260912_标题.md"), "20260912 标题.md")
        self.assertEqual(pipeline.episode_note_name("transcripts/007_标题.md"), "007 标题.md")
        row = _row("a", "20260912", summary=dict(pipeline.parse_series_summary(RAW)))
        note = pipeline.render_episode_note(row, "某节目")
        self.assertIn('节目: "[[某节目]]"', note)
        self.assertIn("播出: 2026-09-12", note)
        self.assertIn('嘉宾: ["Jason (Jason Calacanis，主持人)", "David Sacks"]', note)
        self.assertIn("[0:46](https://www.youtube.com/watch?v=a&t=46s)", note)
        self.assertIn("(<transcripts/20260912_第a期.md>)", note)
        # 老格式的小结只有要点，补上分节标题
        self.assertIn("### 本期要点\n\n- 老格式要点", pipeline.render_episode_note(_row("b", "20260101"), "某节目"))

    def test_已有笔记不覆盖_除非这期小结重新生成了(self):
        rows = [_row("a", "20260912"), _row("b", "20260101", ok=False)]
        self.assertTrue(pipeline.write_episode_notes(self.dir, rows))
        path = os.path.join(self.dir, rows[0]["note_relative_path"])
        self.assertEqual(os.listdir(self.dir), ["notes"])
        self.assertEqual(os.listdir(os.path.join(self.dir, "notes")), [os.path.basename(path)])   # 失败的那期不写
        self.assertIn("(<../transcripts/20260912_第a期.md>)", open(path, encoding="utf-8").read())
        with open(path, "a", encoding="utf-8") as f:
            f.write("我的批注")
        pipeline.write_episode_notes(self.dir, rows)
        self.assertIn("我的批注", open(path, encoding="utf-8").read())
        pipeline.write_episode_notes(self.dir, rows, overwrite_ids={"a"})
        self.assertNotIn("我的批注", open(path, encoding="utf-8").read())

    def test_节目主页按月份倒序_链到单集笔记(self):
        rows = [_row("old", "20260105"), _row("new", "20260912"), _row("bad", "20260910", ok=False)]
        pipeline.write_episode_notes(self.dir, rows)
        page = pipeline.render_index_md("某节目", "https://x", "### 内容总结\n好节目", rows, content_type="series")
        self.assertLess(page.index("### 2026-09"), page.index("### 2026-01"))
        self.assertIn("## 节目总结", page)
        self.assertIn("(<notes/20260912 第new期.md>)", page)
        self.assertIn("⚠️ 无字幕", page)
        self.assertIn("共 2 期", page)

    def test_老节目文件夹不调模型就能转成新格式(self):
        rows = {r["entry"]["id"]: r for r in (_row("a", "20260912"), _row("b", "20260801"))}
        with open(os.path.join(self.dir, ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": rows, "overall_summary": "### 节目内容概览\n老长文", "source_url": "https://x"}, f)
        with open(os.path.join(self.dir, "某节目.md"), "w", encoding="utf-8") as f:
            f.write("# 某节目: 原标题\n")
        res = pipeline.refresh_series_outputs(self.dir)
        self.assertEqual(res["new_notes"], 2)
        page = open(os.path.join(self.dir, "某节目.md"), encoding="utf-8").read()
        self.assertTrue(page.startswith("# 某节目: 原标题"))
        self.assertIn("老长文", page)
        manifest = json.load(open(os.path.join(self.dir, ".manifest.json"), encoding="utf-8"))
        self.assertEqual(manifest["entries"]["a"]["note_relative_path"], "notes/20260912 第a期.md")


class OverallSummaryTests(unittest.TestCase):
    def _job(self, out_dir, calls):
        job = mock.Mock(out_dir=out_dir, summit_title="某节目", content_type="series", backend="api",
                        api_key="k", model="m", api_base="", llm_cache="", stop_flag=None, report=mock.Mock())
        return job

    def _run(self, manifest, rows, new_rows, regenerate=True):
        prompts = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return "### 内容总结\n新\n### 长期主线\n#### 线一\n说明\n### 主题索引\n主题：线一\n- 第a期"
        out_dir = tempfile.mkdtemp()
        with mock.patch.object(pipeline, "_cached_summarize", side_effect=fake):
            summary, _ = pipeline._refresh_overall_summary(
                self._job(out_dir, prompts), manifest, rows, do_summary=True, regenerate_summary=regenerate,
                overall_model="", was_stopped=False, new_rows=new_rows)
        return summary, prompts

    def test_老版长文_整篇按新格式重写(self):
        rows = [_row("a", "20260912")]
        summary, prompts = self._run({"entries": {}, "overall_summary": "### 节目内容概览\n老"}, rows, rows)
        self.assertIn("只包含下面三节", prompts[0])
        self.assertIn("### 长期主线", summary)

    def test_新版总结_只把新增的期数并进去(self):
        rows = [_row("a", "20260912"), _row("b", "20260101")]
        manifest = {"entries": {}, "overall_summary": "### 内容总结\n旧\n### 长期主线\n#### 线一\n旧说明",
                    "topic_groups": {"线一": ["b"]}}
        summary, prompts = self._run(manifest, rows, [rows[0]])
        self.assertIn("这次新增的 1 期", prompts[0])
        self.assertIn("【第a期】", prompts[0])
        self.assertIn("主题：线一\n- 第b期", prompts[0])   # 原来的分组以原始格式交回给模型

    def test_总结成功后记下并进去了哪些单集(self):
        rows = [_row("a", "20260912"), _row("bad", "20260101", ok=False)]
        manifest = {"entries": {}, "overall_summary": "### 节目内容概览\n老"}
        self._run(manifest, rows, rows[:1])
        self.assertEqual(manifest["summary_merged_ids"], ["a"])

    def test_新版总结_这次没有新单集就不调模型(self):
        rows = [_row("a", "20260912")]
        manifest = {"entries": {}, "overall_summary": "### 内容总结\n旧\n### 长期主线\n#### 线一"}
        summary, prompts = self._run(manifest, rows, [])
        self.assertEqual(prompts, [])
        self.assertIn("旧", summary)


class ReviewFixTests(unittest.TestCase):
    def test_解析容忍加粗和列表写法_没有TLDR就用第一句(self):
        s = pipeline.parse_series_summary("**TLDR:** 结论\n- 嘉宾：A, B\n\n### 本期要点\n- x")
        self.assertEqual((s["tldr"], s["guests"]), ("结论", ["A", "B"]))
        s = pipeline.parse_series_summary("嘉宾: A\n\n### 本期要点\n- 第一条要点")
        self.assertEqual(s["tldr"], "第一条要点")

    def test_时间戳链接按视频id重拼(self):
        for url in ("https://www.youtube.com/watch?v=abc&t=30s", "https://youtu.be/abc",
                    "https://www.youtube.com/watch?v=abc#x"):
            self.assertIn("(https://www.youtube.com/watch?v=abc&t=60s)", pipeline.linkify_timestamps("[1:00]", url), url)

    def test_笔记放在notes里_不占用别人的文件(self):
        d = os.path.join(tempfile.mkdtemp(), "Show")
        os.makedirs(os.path.join(d, "notes"))
        with open(os.path.join(d, "notes", "别人的.md"), "w", encoding="utf-8") as f:
            f.write("我的东西")
        row = _row("a", "20260101")
        row["relative_path"] = "transcripts/Show.md"
        row2 = _row("b", "20260102")
        row2["relative_path"] = "transcripts/别人的.md"
        pipeline.write_episode_notes(d, [row, row2])
        self.assertEqual(row["note_relative_path"], "notes/Show.md")
        self.assertEqual(row2["note_relative_path"], "notes/别人的 (2).md")
        self.assertEqual(open(os.path.join(d, "notes", "别人的.md"), encoding="utf-8").read(), "我的东西")

    def test_根目录的老笔记挪进notes_批注保留_链接跟着改(self):
        d = tempfile.mkdtemp()
        row = _row("a", "20260912")
        row["note_relative_path"] = "20260912 第a期.md"
        with open(os.path.join(d, "20260912 第a期.md"), "w", encoding="utf-8") as f:
            f.write('节目: "[[x]]"\n[完整文字记录](<transcripts/20260912_第a期.md>)\n我的批注')
        manifest = {"overall_summary": "主题：A\n- [第a期](<20260912 第a期.md>)"}
        self.assertTrue(pipeline.write_episode_notes(d, [row], manifest=manifest))
        self.assertEqual(row["note_relative_path"], "notes/20260912 第a期.md")
        self.assertFalse(os.path.exists(os.path.join(d, "20260912 第a期.md")))
        content = open(os.path.join(d, "notes", "20260912 第a期.md"), encoding="utf-8").read()
        self.assertIn("我的批注", content)
        self.assertIn("(<../transcripts/20260912_第a期.md>)", content)
        self.assertIn("(<notes/20260912 第a期.md>)", manifest["overall_summary"])

    def test_小结只留在笔记里_整理稿和文字记录改成链接(self):
        d = tempfile.mkdtemp()
        row = _row("a", "20260912")
        row["speech_relative_path"] = "speech/20260912_第a期.md"
        entry = dict(row["entry"], duration=60)
        summary = {"tldr": "一句话", "body": "### 本期要点\n- 要点"}
        os.makedirs(os.path.join(d, "speech"))
        os.makedirs(os.path.join(d, "transcripts"))
        with open(os.path.join(d, row["speech_relative_path"]), "w", encoding="utf-8") as f:
            f.write(pipeline.render_speech_md(entry, "S", "整理后的正文", None, None, summary=summary,
                                              transcript_relative_path=row["relative_path"], content_type="series"))
        with open(os.path.join(d, row["relative_path"]), "w", encoding="utf-8") as f:
            f.write(pipeline.render_transcript_md(entry, "S", [(0.0, "原文")], summary, "en",
                                                  speech_relative_path=row["speech_relative_path"],
                                                  content_type="series"))
        pipeline.write_episode_notes(d, [row])
        pipeline.write_episode_notes(d, [row])   # 再跑一次不重复加链接
        for rel in (row["speech_relative_path"], row["relative_path"]):
            content = open(os.path.join(d, rel), encoding="utf-8").read()
            self.assertNotIn("小结", content)
            self.assertNotIn("要点", content)
            self.assertEqual(content.count("- 单集笔记：[打开笔记](<../notes/20260912 第a期.md>)"), 1)
        self.assertIn("整理后的正文", open(os.path.join(d, row["speech_relative_path"]), encoding="utf-8").read())
        note = open(os.path.join(d, "notes", "20260912 第a期.md"), encoding="utf-8").read()
        self.assertIn("要点", note)
        self.assertIn("[整理稿](<../speech/20260912_第a期.md>)", note)

    def test_没有笔记时小结留在整理稿里(self):
        d = tempfile.mkdtemp()
        row = _row("a", "20260912")
        row["speech_relative_path"] = "speech/20260912_第a期.md"
        os.makedirs(os.path.join(d, "speech"))
        with open(os.path.join(d, row["speech_relative_path"]), "w", encoding="utf-8") as f:
            f.write("# T\n\n- 链接：x\n\n## 单集小结\n\n要点\n\n## 演讲稿\n\n正文\n")
        pipeline._point_files_to_note(d, row, "notes/20260912 第a期.md")
        self.assertIn("要点", open(os.path.join(d, row["speech_relative_path"]), encoding="utf-8").read())

    def test_转换老目录时拒绝会议目录(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {}, "content_type": "summit"}, f)
        with self.assertRaises(ValueError):
            pipeline.refresh_series_outputs(d)


if __name__ == "__main__":
    unittest.main()
