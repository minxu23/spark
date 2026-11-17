"""字幕完整性校验：字幕只覆盖视频前面一截时，不能照常出小结。"""

import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline


def _vtt(path, last_seconds):
    lines = ["WEBVTT", ""]
    for t in range(0, int(last_seconds) + 1, 30):
        h, m, s = t // 3600, t % 3600 // 60, t % 60
        lines += [f"{h:02d}:{m:02d}:{s:02d}.000 --> {h:02d}:{m:02d}:{s:02d}.900", f"words at {t}", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _entry(duration):
    return {"id": "vid12345678", "title": "Long Talk", "url": "https://www.youtube.com/watch?v=vid12345678",
            "duration": duration, "index": 1, "is_raw_session": False}


def _run(root, entry):
    return pipeline.process_job(
        summit_title="Coverage", source_url="https://www.youtube.com/playlist?list=t",
        entries=[entry], output_base_dir=root, backend="cli", api_key="", model="",
        lang_prefs=["en"], do_summary=True, progress_cb=lambda kw: None)


class CoverageProblemTests(unittest.TestCase):
    def test_只到一分钟的一小时节目算不完整(self):
        msg = pipeline.subtitle_coverage_problem([(0, "a"), (60, "b")], 3600)
        self.assertIn("1 分 00 秒", msg)
        self.assertIn("60 分 00 秒", msg)

    def test_片尾一两分钟没字幕不算(self):
        self.assertEqual(pipeline.subtitle_coverage_problem([(0, "a"), (3500, "b")], 3600), "")

    def test_短视频和不知道时长的不查(self):
        self.assertEqual(pipeline.subtitle_coverage_problem([(0, "a")], 75), "")
        self.assertEqual(pipeline.subtitle_coverage_problem([(0, "a")], None), "")
        self.assertEqual(pipeline.subtitle_coverage_problem([], 0), "")

    def test_短节目缺一大半也算(self):
        self.assertTrue(pipeline.subtitle_coverage_problem([(0, "a"), (60, "b")], 600))


class CoverageInJobTests(unittest.TestCase):
    def test_字幕不完整时改用本地识别_识别完整就用识别结果(self):
        with tempfile.TemporaryDirectory() as root:
            short = {"lang": "en", "path": _vtt(os.path.join(root, "s.vtt"), 60), "description": "",
                     "upload_date": "", "kind": "auto"}
            full = {"lang": "en", "path": _vtt(os.path.join(root, "f.vtt"), 3580), "description": "",
                    "upload_date": "", "kind": "asr"}
            with mock.patch.object(pipeline, "download_subtitle", return_value=short), \
                 mock.patch.object(pipeline, "transcribe_without_captions", return_value=(full, "")) as asr, \
                 mock.patch.object(pipeline, "summarize", return_value="TLDR: s\n- p"):
                result = _run(root, _entry(3600))
            asr.assert_called_once()
            self.assertEqual(asr.call_args.kwargs.get("why"), "YouTube 字幕不完整")
            row = result["rows"][0]
            self.assertTrue(row["ok"], row.get("error"))
            self.assertEqual(row["entry"]["sub_kind"], "asr")

    def test_识别也不完整就记失败_不出小结(self):
        with tempfile.TemporaryDirectory() as root:
            short = {"lang": "en", "path": _vtt(os.path.join(root, "s.vtt"), 60), "description": "",
                     "upload_date": "", "kind": "auto"}
            with mock.patch.object(pipeline, "download_subtitle", return_value=short), \
                 mock.patch.object(pipeline, "transcribe_without_captions", return_value=(None, "下载音频失败")), \
                 mock.patch.object(pipeline, "summarize",
                                   side_effect=AssertionError("字幕不完整不该出小结")):
                result = _run(root, _entry(3600))
            row = result["rows"][0]
            self.assertFalse(row["ok"])
            self.assertIn("字幕只到 1 分 00 秒", row["error"])
            self.assertIn("下载音频失败", row["error"])

    def test_字幕完整时不去做本地识别(self):
        with tempfile.TemporaryDirectory() as root:
            full = {"lang": "en", "path": _vtt(os.path.join(root, "f.vtt"), 3580), "description": "",
                    "upload_date": "", "kind": "auto"}
            with mock.patch.object(pipeline, "download_subtitle", return_value=full), \
                 mock.patch.object(pipeline, "transcribe_without_captions",
                                   side_effect=AssertionError("字幕完整不该识别")), \
                 mock.patch.object(pipeline, "summarize", return_value="TLDR: s\n- p"):
                result = _run(root, _entry(3600))
            self.assertTrue(result["rows"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
