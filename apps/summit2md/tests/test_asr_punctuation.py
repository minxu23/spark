"""本地语音识别：把识别全文里的标点补回字幕。"""

import unittest


class PunctuateCuesTests(unittest.TestCase):
    def test_标点补回字幕_对不上的词不影响后面(self):
        from apps.summit2md.local_asr import punctuate_cues
        text = ("It's the first time there is a cancer vaccine working. The field has been doing that for "
                "20-plus years, more than 1,000 clinical trials. What was different this time?")
        cues = [("a", "Its the first time there is a"), ("b", "cancer vaccine working The field has"),
                ("c", "been doing that for twenty plus years more than a thousand clinical trials"),
                ("d", "What was different this time")]
        got = [t for _, t in punctuate_cues(cues, text)]
        self.assertEqual(got[1], "cancer vaccine working. The field has")
        self.assertTrue(got[2].endswith("clinical trials."))
        self.assertEqual(got[3], "What was different this time?")

    def test_中文按字对齐(self):
        from apps.summit2md.local_asr import punctuate_cues
        got = punctuate_cues([("a", "我们今天讲一下"), ("b", "人工智能对吧")], "我们今天讲一下，人工智能，对吧？")
        self.assertEqual([t for _, t in got], ["我们今天讲一下，", "人工智能，对吧？"])

    def test_没有全文就原样返回(self):
        from apps.summit2md.local_asr import punctuate_cues
        cues = [("a", "hello world")]
        self.assertEqual(punctuate_cues(cues, ""), cues)


if __name__ == "__main__":
    unittest.main()
