import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline


class ProcessJobRegressionTests(unittest.TestCase):
    def _prepare_existing_topic(self, root, summit_title, *, failed_summary=False):
        out_dir = os.path.join(root, pipeline.sanitize_filename(summit_title))
        transcripts_dir = os.path.join(out_dir, "transcripts")
        speech_dir = os.path.join(out_dir, "speech")
        os.makedirs(transcripts_dir, exist_ok=True)
        os.makedirs(speech_dir, exist_ok=True)

        entry = {
            "id": "existing-video",
            "title": "Test Speaker - Existing Talk",
            "url": "https://www.youtube.com/watch?v=existing-video",
            "duration": 60,
            "index": 1,
            "is_raw_session": False,
        }
        transcript_rel = "transcripts/001_Test Speaker - Existing Talk.md"
        speech_rel = "speech/001_Test Speaker - Existing Talk.md"
        summary = (
            {"tldr": "", "body": "_（摘要生成失败：temporary error）_"}
            if failed_summary else
            {"tldr": "Existing summary", "body": "- Existing point"}
        )
        transcript = pipeline.render_transcript_md(
            entry, summit_title, [(0, "Existing transcript paragraph.")], summary, "en",
            include_summary=False, speech_relative_path=speech_rel,
        )
        with open(os.path.join(out_dir, transcript_rel), "w", encoding="utf-8") as f:
            f.write(transcript)
        speech = pipeline.render_speech_md(
            entry, summit_title, "ORIGINAL SPEECH BODY", None, None,
            summary=summary, transcript_relative_path=transcript_rel, speech_lang_mode="original",
        )
        with open(os.path.join(out_dir, speech_rel), "w", encoding="utf-8") as f:
            f.write(speech)

        manifest = {
            "entries": {
                entry["id"]: {
                    "rank": 1,
                    "entry": entry,
                    "ok": True,
                    "error": None,
                    "relative_path": transcript_rel,
                    "speech_relative_path": speech_rel,
                    "summary": summary,
                }
            },
            "overall_summary": "Existing overall summary",
        }
        with open(os.path.join(out_dir, ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        return out_dir, entry, speech_rel

    def test_stop_skips_overall_summary_and_reports_unprocessed_entries(self):
        with tempfile.TemporaryDirectory() as root:
            summit_title = "Stop Regression"
            self._prepare_existing_topic(root, summit_title)
            pending = {
                "id": "pending-video",
                "title": "Pending Talk",
                "url": "https://www.youtube.com/watch?v=pending-video",
                "duration": 30,
                "index": 2,
                "is_raw_session": False,
            }
            logs = []
            with mock.patch.object(
                pipeline, "summarize", side_effect=AssertionError("summary must not run after stop")
            ):
                result = pipeline.process_job(
                    summit_title=summit_title,
                    source_url="https://www.youtube.com/playlist?list=test",
                    entries=[pending],
                    output_base_dir=root,
                    backend="cli",
                    api_key="",
                    model="",
                    lang_prefs=["en"],
                    do_summary=True,
                    stop_flag=lambda: True,
                    progress_cb=logs.append,
                )

            self.assertTrue(result["stopped"])
            self.assertEqual([pending], result["unprocessed_entries"])
            self.assertEqual([], result["rows"])
            self.assertEqual("stopped", logs[-1]["stage"])
            self.assertTrue(os.path.exists(result["index_path"]))

    def test_summary_retry_does_not_regenerate_existing_speech(self):
        with tempfile.TemporaryDirectory() as root:
            summit_title = "Backfill Regression"
            out_dir, entry, speech_rel = self._prepare_existing_topic(
                root, summit_title, failed_summary=True
            )

            def fake_summarize(prompt, *_args, **_kwargs):
                if "议题标题" in prompt:
                    return "TLDR: Updated summary\n- Updated point"
                return "Updated overall summary"

            with mock.patch.object(pipeline, "summarize", side_effect=fake_summarize), mock.patch.object(
                pipeline, "generate_speech_script",
                side_effect=AssertionError("existing speech must not be regenerated"),
            ):
                result = pipeline.process_job(
                    summit_title=summit_title,
                    source_url="https://www.youtube.com/playlist?list=test",
                    entries=[entry],
                    output_base_dir=root,
                    backend="cli",
                    api_key="",
                    model="",
                    lang_prefs=["en"],
                    do_summary=True,
                    do_speech_script=True,
                    skip_existing=True,
                )

            self.assertFalse(result["stopped"])
            with open(os.path.join(out_dir, speech_rel), encoding="utf-8") as f:
                speech = f.read()
            self.assertIn("ORIGINAL SPEECH BODY", speech)
            self.assertIn("Updated summary", speech)
            self.assertNotIn("摘要生成失败", speech)


if __name__ == "__main__":
    unittest.main()


class ProbeOverallSummaryTests(unittest.TestCase):
    """probe_overall_summary()：给"重新发现"流程用的探测，判断要不要在开始生成前
    把"沿用/重新生成"这个选择露给用户看。见 2026-09-18 那次 bug——重新粘贴同一个
    播放列表链接（不是走「导入目录」）发现新议题、开始生成，命中"有真实旧总结"这个
    条件时会在用户完全没看到任何选择的情况下默默沿用旧总结，新议题没被纳入。
    """

    def _write_manifest(self, output_base_dir, summit_title, overall_summary):
        out_dir = os.path.join(output_base_dir, pipeline.sanitize_filename(summit_title))
        os.makedirs(out_dir, exist_ok=True)
        pipeline._save_manifest(out_dir, {"entries": {}, "overall_summary": overall_summary})

    def test_目录还不存在时探测到没有旧总结(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(pipeline.probe_overall_summary(root, "全新播放列表"))

    def test_有真实旧总结时探测到True(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_manifest(root, "老节目", "这是一份真正生成成功的大会总结。")
            self.assertTrue(pipeline.probe_overall_summary(root, "老节目"))

    def test_失败占位符不算真实旧总结(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_manifest(root, "上次生成失败的节目", "_（大会总结生成失败：timeout）_")
            self.assertFalse(pipeline.probe_overall_summary(root, "上次生成失败的节目"))

    def test_空字符串不算真实旧总结(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_manifest(root, "空总结的节目", "")
            self.assertFalse(pipeline.probe_overall_summary(root, "空总结的节目"))

    def test_标题决定的目录名和实际生成时一致(self):
        """探测用的路径推导必须和 process_job() 里真正落盘时用的是同一个函数
        （sanitize_filename），否则探测的是另一个不相关的目录，永远探测不到。"""
        with tempfile.TemporaryDirectory() as root:
            title = "Some / Weird : Title?"
            self._write_manifest(root, title, "真实总结")
            self.assertTrue(pipeline.probe_overall_summary(root, title))


class CustomTopicSummaryTests(unittest.TestCase):
    """手选议题生成聚焦总结：不依赖大会/节目总结解析出的"主题索引"，直接按用户
    手工勾出来的 entry_ids 生成一份聚焦总结。"""

    def _seed(self, out_dir, *, with_failed=False):
        entries = {
            "vid1": {"rank": 1, "ok": True, "entry": {"title": "Talk A"},
                    "summary": {"tldr": "要点A"}},
            "vid2": {"rank": 2, "ok": True, "entry": {"title": "Talk B"},
                    "summary": {"tldr": "要点B"}},
        }
        if with_failed:
            entries["vid3"] = {"rank": 3, "ok": False, "entry": {"title": "Talk C（失败）"},
                               "error": "超时"}
        pipeline._save_manifest(out_dir, {"entries": entries})
        return entries

    def test_按手选的_entry_ids_生成_不依赖主题分组(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容"):
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="我关心的两个",
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertEqual(r["count"], 2)
            self.assertEqual(r["relative_path"], os.path.join("topics", "我关心的两个.md"))
            self.assertIn("Talk A", r["content"])
            self.assertIn("Talk B", r["content"])

    def test_不给标题时模型没按格式给标题_退回手选N个(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容"):
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="  ",  # 只有空白
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertIn("手选 2 个", r["content"])
            self.assertEqual(r["relative_path"], os.path.join("topics", "手选 2 个.md"))

    def test_不给标题时用模型概括出的标题(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(
                pipeline, "_cached_summarize",
                return_value="标题：AI 芯片路线之争\n\n### 主题综述\n正文内容",
            ) as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertIn("# AI 芯片路线之争", r["content"])
            self.assertEqual(r["relative_path"], os.path.join("topics", "AI 芯片路线之争.md"))
            # 标题行不该在正文里重复出现
            self.assertNotIn("标题：AI 芯片路线之争", r["content"])
            self.assertIn("### 主题综述", r["content"])
            # 用的是自动出标题那份 prompt，不是需要现成 theme_name 的那份
            prompt_used = mocked.call_args[0][0]
            self.assertIn("标题：xxx", prompt_used)

    def test_给了标题时不请模型概括_直接用手填的(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容") as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="我自己起的标题",
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertIn("# 我自己起的标题", r["content"])
            prompt_used = mocked.call_args[0][0]
            self.assertNotIn("标题：xxx", prompt_used)

    def test_失败的议题不会被算进去(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir, with_failed=True)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容"):
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2", "vid3"], label="混着选",
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertEqual(r["count"], 2)  # vid3 处理失败，不算
            self.assertNotIn("Talk C", r["content"])

    def test_重复的_entry_id_只算一次(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容"):
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid1", "vid2"], label="重复测试",
                    backend="api", api_key="k", model="m", api_base="",
                )
            self.assertEqual(r["count"], 2)

    def test_一个都没处理成功时报错(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir, with_failed=True)
            with self.assertRaises(pipeline.SummarizeError):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid3"], label="只选了失败的",
                    backend="api", api_key="k", model="m", api_base="",
                )

    def test_主题路径的行为在重构后没有变化(self):
        """回归用例：把公共部分抽成 _compose_topic_summary() 之后，原来按主题分组
        生成的路径不能变。"""
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            manifest = pipeline._load_manifest(out_dir)
            manifest["topic_groups"] = {"AI 安全": ["vid1", "vid2"]}
            pipeline._save_manifest(out_dir, manifest)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容"):
                r = pipeline.generate_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    theme_names=["AI 安全"], backend="api", api_key="k", model="m", api_base="",
                )
            self.assertEqual(r["count"], 2)
            self.assertEqual(r["relative_path"], os.path.join("topics", "AI 安全.md"))

    def test_reuse为True且文件已存在时直接读文件_不调模型(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="第一次生成的正文"):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="老选择",
                    backend="api", api_key="k", model="m", api_base="",
                )
            with mock.patch.object(pipeline, "_cached_summarize") as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="老选择",
                    backend="api", api_key="k", model="m", api_base="", reuse=True,
                )
            mocked.assert_not_called()
            self.assertIn("第一次生成的正文", r["content"])

    def test_reuse为True但文件还不存在时照常生成(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容") as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="第一次选这个标签",
                    backend="api", api_key="k", model="m", api_base="", reuse=True,
                )
            mocked.assert_called_once()
            self.assertIn("正文内容", r["content"])

    def test_reuse为True但留空标题走自动概括时照常调模型(self):
        """自动概括标题那条路没法提前知道文件名，reuse 对它不生效，不会误判成
        "文件不存在所以直接跳过复用逻辑走正常生成"之外的任何奇怪行为。"""
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文内容") as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="", reuse=True,
                )
            mocked.assert_called_once()
            self.assertIn("正文内容", r["content"])

    def test_reuse为False时强制跳过缓存_不是原样走一遍命中缓存(self):
        """这是这个功能存在的意义：选了"重新生成一遍"，就算勾选和标题都跟上次
        一模一样、提示词逐字相同，也不能只是走一遍普通流程再命中缓存拿回旧文本
        （用户会觉得"重新生成根本没用"）——必须真正把 force=True 传给
        _cached_summarize，让它跳过缓存读取。"""
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文") as mocked:
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="老选择",
                    backend="api", api_key="k", model="m", api_base="", reuse=False,
                )
            self.assertTrue(mocked.call_args.kwargs.get("force"))

    def test_reuse为True时不强制跳过缓存(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="正文") as mocked:
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="第一次选这个标签",
                    backend="api", api_key="k", model="m", api_base="", reuse=True,
                )
            self.assertFalse(mocked.call_args.kwargs.get("force"))

    def test_主题路径的reuse也能直接读文件(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            manifest = pipeline._load_manifest(out_dir)
            manifest["topic_groups"] = {"AI 安全": ["vid1", "vid2"]}
            pipeline._save_manifest(out_dir, manifest)
            with mock.patch.object(pipeline, "_cached_summarize", return_value="主题总结正文"):
                pipeline.generate_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    theme_names=["AI 安全"], backend="api", api_key="k", model="m", api_base="",
                )
            with mock.patch.object(pipeline, "_cached_summarize") as mocked:
                r = pipeline.generate_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    theme_names=["AI 安全"], backend="api", api_key="k", model="m", api_base="",
                    reuse=True,
                )
            mocked.assert_not_called()
            self.assertIn("主题总结正文", r["content"])


class ProbeTopicSummaryTests(unittest.TestCase):
    """probe_topic_summary()：给"沿用/重新生成"这个选择判断要不要露出来。"""

    def test_目录不存在时返回False(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(pipeline.probe_topic_summary(root, "随便什么标签"))

    def test_标题留空时直接返回False(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(pipeline.probe_topic_summary(root, "   "))

    def test_文件已生成时返回True(self):
        with tempfile.TemporaryDirectory() as out_dir:
            topics_dir = os.path.join(out_dir, "topics")
            os.makedirs(topics_dir, exist_ok=True)
            with open(os.path.join(topics_dir, "老选择.md"), "w", encoding="utf-8") as f:
                f.write("# 老选择\n\n正文")
            self.assertTrue(pipeline.probe_topic_summary(out_dir, "老选择"))

    def test_文件名推导和真正生成时用的是同一个函数(self):
        with tempfile.TemporaryDirectory() as out_dir:
            label = "Some / Weird : Label?"
            topics_dir = os.path.join(out_dir, "topics")
            os.makedirs(topics_dir, exist_ok=True)
            fname = pipeline.sanitize_filename(label, 80) + ".md"
            with open(os.path.join(topics_dir, fname), "w", encoding="utf-8") as f:
                f.write("正文")
            self.assertTrue(pipeline.probe_topic_summary(out_dir, label))


class ProbeCustomTopicSummaryTests(unittest.TestCase):
    """probe_custom_topic_summary()：手选议题那条路标题常年留空（走 AI 自动概括），
    单靠 label 探测不到任何东西——这组测试专门覆盖"标题留空、靠 entry_ids 查记录"
    这条路径，是之前那次真实使用中发现的缺口：生成过一次之后，标题没填就永远
    探测不到已有文件，"沿用/重新生成"的选择也就永远不会出现。"""

    def _seed(self, out_dir):
        pipeline._save_manifest(out_dir, {"entries": {
            "vid1": {"rank": 1, "ok": True, "entry": {"title": "Talk A"}, "summary": {"tldr": "x"}},
            "vid2": {"rank": 2, "ok": True, "entry": {"title": "Talk B"}, "summary": {"tldr": "y"}},
        }})

    def test_标题手填时和probe_topic_summary行为一致(self):
        with tempfile.TemporaryDirectory() as out_dir:
            topics_dir = os.path.join(out_dir, "topics")
            os.makedirs(topics_dir, exist_ok=True)
            with open(os.path.join(topics_dir, "手填的标题.md"), "w", encoding="utf-8") as f:
                f.write("正文")
            r = pipeline.probe_custom_topic_summary(out_dir, ["vid1", "vid2"], "手填的标题")
            self.assertEqual(r, {"exists": True, "label": "手填的标题"})

    def test_标题留空且从没生成过时返回False(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            r = pipeline.probe_custom_topic_summary(out_dir, ["vid1", "vid2"], "")
            self.assertEqual(r, {"exists": False, "label": ""})

    def test_标题留空但这批议题生成过_能查到记录并探测到文件(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(
                pipeline, "_cached_summarize",
                return_value="标题：真实概括出的标题\n\n正文",
            ):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="",
                )
            r = pipeline.probe_custom_topic_summary(out_dir, ["vid1", "vid2"], "")
            self.assertEqual(r, {"exists": True, "label": "真实概括出的标题"})

    def test_entry_ids顺序不影响查到同一条记录(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(
                pipeline, "_cached_summarize", return_value="标题：某个标题\n\n正文",
            ):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="",
                )
            r = pipeline.probe_custom_topic_summary(out_dir, ["vid2", "vid1"], "")
            self.assertEqual(r, {"exists": True, "label": "某个标题"})

    def test_reuse为True且标题留空但有记录时直接读文件_不调模型(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self._seed(out_dir)
            with mock.patch.object(
                pipeline, "_cached_summarize", return_value="标题：记住的标题\n\n第一次的正文",
            ):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="",
                )
            with mock.patch.object(pipeline, "_cached_summarize") as mocked:
                r = pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="", reuse=True,
                )
            mocked.assert_not_called()
            self.assertIn("第一次的正文", r["content"])
            self.assertIn("记住的标题", r["content"])

    def test_不同的entry_ids选择不会串到别的记录(self):
        with tempfile.TemporaryDirectory() as out_dir:
            pipeline._save_manifest(out_dir, {"entries": {
                "vid1": {"rank": 1, "ok": True, "entry": {"title": "Talk A"}, "summary": {"tldr": "x"}},
                "vid2": {"rank": 2, "ok": True, "entry": {"title": "Talk B"}, "summary": {"tldr": "y"}},
                "vid3": {"rank": 3, "ok": True, "entry": {"title": "Talk C"}, "summary": {"tldr": "z"}},
            }})
            with mock.patch.object(
                pipeline, "_cached_summarize", return_value="标题：第一批的标题\n\n正文",
            ):
                pipeline.generate_custom_topic_summary(
                    out_dir=out_dir, summit_title="测试大会", content_type="summit",
                    entry_ids=["vid1", "vid2"], label="",
                    backend="api", api_key="k", model="m", api_base="",
                )
            r = pipeline.probe_custom_topic_summary(out_dir, ["vid1", "vid3"], "")
            self.assertEqual(r, {"exists": False, "label": ""})


class ListManifestEntriesTests(unittest.TestCase):
    def test_按_rank_排序_含成功和失败的(self):
        with tempfile.TemporaryDirectory() as out_dir:
            pipeline._save_manifest(out_dir, {"entries": {
                "b": {"rank": 2, "ok": True, "entry": {"title": "第二个"}},
                "a": {"rank": 1, "ok": False, "entry": {"title": "第一个"}},
            }})
            entries = pipeline.list_manifest_entries(out_dir)
            self.assertEqual([e["id"] for e in entries], ["a", "b"])
            self.assertEqual(entries[0]["ok"], False)
            self.assertEqual(entries[1]["ok"], True)

    def test_空目录返回空列表(self):
        with tempfile.TemporaryDirectory() as out_dir:
            self.assertEqual(pipeline.list_manifest_entries(out_dir), [])
