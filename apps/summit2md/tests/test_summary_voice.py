"""单集/议题小结的写法要求：不要「节目指出」「演讲提到」这种以节目为主语的转述腔。"""

import unittest

from apps.summit2md import pipeline


class SummaryVoiceTests(unittest.TestCase):
    def test_两种小结提示词都带写法要求(self):
        series = pipeline.SERIES_EPISODE_PROMPT
        topic = pipeline.PER_TOPIC_PROMPT
        for prompt in (series, topic):
            self.assertIn(pipeline.SUMMARY_VOICE_RULE, prompt)
        self.assertIn("节目指出", pipeline.SUMMARY_VOICE_RULE)
        self.assertIn("写说话人的名字", pipeline.SUMMARY_VOICE_RULE)

    def test_单人节目不再要求写嘉宾的主张(self):
        self.assertNotIn("就写嘉宾的核心主张", pipeline.SERIES_EPISODE_PROMPT)
        self.assertIn("主讲人或嘉宾（写名字）", pipeline.SERIES_EPISODE_PROMPT)


if __name__ == "__main__":
    unittest.main()
