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
            result = pipeline.fetch_subtitle_languages("https://www.youtube.com/watch?v=xxxxxxxxxxx")
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
            result = pipeline.fetch_subtitle_languages("https://www.youtube.com/watch?v=xxxxxxxxxxx")
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


class PickSubtitleTrackTests(unittest.TestCase):
    def test_上传者字幕优先_带地区的也认(self):
        info = {"subtitles": {"en-US": [{}]}, "automatic_captions": {"en": [{}], "en-orig": [{}]}}
        self.assertEqual(pipeline._pick_subtitle_track(info, ["en"]), ("en-US", "manual"))

    def test_没有上传者字幕时用orig_不用可能是机翻的en(self):
        # 开了 AI 配音的频道：每种配音都有一条 xx-orig，"en" 可能是从别的语种机翻回来的
        info = {"subtitles": {}, "automatic_captions": {"en": [{}], "en-orig": [{}], "ko-orig": [{}]}}
        self.assertEqual(pipeline._pick_subtitle_track(info, ["en"]), ("en-orig", "auto"))

    def test_只有普通自动字幕时照用(self):
        info = {"automatic_captions": {"en": [{}]}}
        self.assertEqual(pipeline._pick_subtitle_track(info, ["en"]), ("en", "auto"))

    def test_没有匹配的语言(self):
        self.assertIsNone(pipeline._pick_subtitle_track({"automatic_captions": {"ja-orig": [{}]}}, ["en"]))


class DownloadSubtitleReasonTests(unittest.TestCase):
    def test_一直限流时原因是rate_limited(self):
        status = {}
        with mock.patch.object(pipeline.time, "sleep"), \
             mock.patch.object(pipeline.yt_dlp, "YoutubeDL",
                               return_value=_fake_ydl(raise_on_download=Exception("HTTP Error 429: Too Many Requests"))), \
             tempfile.TemporaryDirectory() as d:
            self.assertIsNone(pipeline.download_subtitle("vid1", d, ["en"], status=status))
        self.assertEqual(status["reason"], "rate_limited")
        self.assertIn("429", pipeline.subtitle_error_message("rate_limited"))

    def test_只下挑中的那条_记住轨道供缓存命中(self):
        calls = []

        with tempfile.TemporaryDirectory() as d:
            def make(opts):
                ydl = _fake_ydl()

                def _extract(url, download=True):
                    calls.append(dict(opts))
                    if download:
                        with open(f"{d}/vid1.en-US.vtt", "w", encoding="utf-8") as f:
                            f.write("WEBVTT\n")
                    return {"subtitles": {"en-US": [{}]}, "automatic_captions": {"en": [{}]},
                            "upload_date": "20261002"}
                ydl.extract_info.side_effect = _extract
                return ydl

            with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", side_effect=make):
                got = pipeline.download_subtitle("vid1", d, ["en"])
            again = pipeline.download_subtitle("vid1", d, ["en"])   # 不再请求，直接命中缓存
        self.assertEqual((got["lang"], got["kind"]), ("en-US", "manual"))
        dl = calls[-1]
        self.assertEqual((dl["subtitleslangs"], dl["writesubtitles"], dl["writeautomaticsub"]),
                         (["en-US"], True, False))
        self.assertEqual((again["lang"], again["kind"], again["upload_date"]), ("en-US", "manual", "20261002"))

    def test_上传者字幕在文字记录开头写明来源(self):
        md = pipeline.render_transcript_md({"title": "T", "url": "u", "duration": 60, "sub_kind": "manual"}, "S",
                                           [(0, "hi")], None, "en-US")
        self.assertIn("YouTube 上传者提供的字幕（en-US）", md)
        self.assertEqual(pipeline._SUB_SOURCE_RE.search(md).group(1), "en-US")



class KeepOriginalLanguageTests(unittest.TestCase):
    def test_英文原文被整理成中文时带提醒重试一次(self):
        prompts = []

        def fake(prompt, *_a, **_kw):
            prompts.append(prompt)
            return "健康检查分为两种。" if len(prompts) == 1 else "There are two kinds of health checks."

        with mock.patch.object(pipeline, "summarize", side_effect=fake):
            out = pipeline._generate_original_language_script(
                {"title": "T"}, ["so there are basically two types of health checks"], "", None, "英语",
                "api", "k", "m", "")
        self.assertEqual(out, "There are two kinds of health checks.")
        self.assertIn("Do NOT translate", prompts[1])

    def test_重试后还是中文就报错_不写进整理稿(self):
        with mock.patch.object(pipeline, "summarize", return_value="健康检查分为两种。"):
            with self.assertRaises(pipeline.SummarizeError):
                pipeline._generate_original_language_script(
                    {"title": "T"}, ["two types of health checks"], "", None, "英语", "api", "k", "m", "")

    def test_中文节目不检查(self):
        self.assertIsNone(pipeline._check_kept_language("健康检查分为两种", "健康检查分为两种。"))


if __name__ == "__main__":
    unittest.main()
