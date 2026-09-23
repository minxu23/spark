"""字幕相关的两处修复：
1. fetch_subtitle_languages 把 YouTube 自动翻译出来的一整套目标语言过滤掉，
   只留"-orig"（非翻译、直接识别）轨道和人工字幕。
2. download_subtitle 遇到 HTTP 429 时要按更长的退避重试，不能 3 秒就放弃、
   把限流误判成"这条真的没字幕"。
"""

import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline


def _fake_ydl(info=None, raise_on_download=None):
    """造一个跟 `with yt_dlp.YoutubeDL(opts) as ydl: ydl.extract_info(...)` 用法
    兼容的假对象——真代码里两处都是这个模式，测试不用管 opts 具体传了什么。"""
    ydl = mock.MagicMock()
    ydl.__enter__.return_value = ydl
    ydl.__exit__.return_value = False
    if raise_on_download is not None:
        ydl.extract_info.side_effect = raise_on_download
    else:
        ydl.extract_info.return_value = info or {}
    return ydl


class FetchSubtitleLanguagesFilterTests(unittest.TestCase):
    def test_只保留orig轨道和人工字幕(self):
        info = {
            "language": "ar",  # YouTube 的这个字段不可靠，实测遇到过明显是英语访谈却标成别的语言
            "automatic_captions": {
                "en-orig": [{}], "ja-orig": [{}],
                # 翻译目标：不管视频实际语言是什么，YouTube 总会列出这上百种
                "ab": [{}], "aa": [{}], "af": [{}], "zh-Hans": [{}],
            },
            "subtitles": {"en": [{}]},  # 人工上传的官方字幕，始终保留
        }
        with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", return_value=_fake_ydl(info)):
            result = pipeline.fetch_subtitle_languages("https://www.youtube.com/watch?v=x")
        codes = {l["code"] for l in result["languages"]}
        self.assertEqual(codes, {"en-orig", "ja-orig", "en"})
        self.assertEqual(result["original_language"], "ar")

    def test_没有orig轨道时退回完整列表(self):
        info = {
            "language": None,
            "automatic_captions": {"en": [{}], "ja": [{}]},
            "subtitles": {},
        }
        with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", return_value=_fake_ydl(info)):
            result = pipeline.fetch_subtitle_languages("https://www.youtube.com/watch?v=x")
        codes = {l["code"] for l in result["languages"]}
        self.assertEqual(codes, {"en", "ja"})

    def test_orig代码的显示名去掉后缀识别(self):
        # en-orig / nl-NL-orig 这种带地区或 -orig 后缀的 code，显示名要能认出
        # 前面的语言部分，不能因为后缀就退化成显示裸 code。
        self.assertEqual(pipeline._lang_display_name("en-orig"), "英语")
        self.assertEqual(pipeline._lang_display_name("nl-NL-orig"), "荷兰语")


class DownloadSubtitleRateLimitTests(unittest.TestCase):
    def test_遇到429会按更长的时间退避重试(self):
        sleeps = []
        with mock.patch.object(pipeline.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(
                 pipeline.yt_dlp, "YoutubeDL",
                 return_value=_fake_ydl(raise_on_download=Exception("HTTP Error 429: Too Many Requests")),
             ), \
             tempfile.TemporaryDirectory() as d:
            result = pipeline.download_subtitle("vid1", d, ["en"])
        self.assertIsNone(result)  # 一直限流、最终真的下不到，返回 None（跟原来行为一致）
        # 429 的退避要明显比其他错误长，不能是原来那种一律 3 秒
        self.assertTrue(all(s >= 10 for s in sleeps), sleeps)

    def test_非限流错误保持原来的短退避(self):
        sleeps = []
        with mock.patch.object(pipeline.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(
                 pipeline.yt_dlp, "YoutubeDL",
                 return_value=_fake_ydl(raise_on_download=Exception("some other transient error")),
             ), \
             tempfile.TemporaryDirectory() as d:
            result = pipeline.download_subtitle("vid1", d, ["en"])
        self.assertIsNone(result)
        self.assertTrue(all(s < 10 for s in sleeps), sleeps)

    def test_重试后成功会正常返回字幕信息(self):
        with tempfile.TemporaryDirectory() as d:
            vtt_path = f"{d}/vid1.en.vtt"
            calls = {"n": 0}

            def flaky_ydl(*_args, **_kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _fake_ydl(raise_on_download=Exception("HTTP Error 429: Too Many Requests"))
                # 第二次「成功」：模拟 yt-dlp 真的把 vtt 文件写到了 out_dir
                ydl = _fake_ydl()

                def _extract(*_a, **_kw):
                    with open(vtt_path, "w", encoding="utf-8") as f:
                        f.write("WEBVTT\n")
                    return {"description": "desc", "upload_date": "20260101"}
                ydl.extract_info.side_effect = _extract
                return ydl

            with mock.patch.object(pipeline.time, "sleep"), \
                 mock.patch.object(pipeline.yt_dlp, "YoutubeDL", side_effect=flaky_ydl):
                result = pipeline.download_subtitle("vid1", d, ["en"])
        self.assertIsNotNone(result)
        self.assertEqual(result["lang"], "en")
        self.assertEqual(result["description"], "desc")


if __name__ == "__main__":
    unittest.main()
