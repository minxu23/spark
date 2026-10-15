"""所有 YouTube 请求全局排队：同时最多几个、两次开始之间最少隔多久。"""

import threading
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline


class YouTubeGateTests(unittest.TestCase):
    def test_同时最多两个_开始时间按间隔错开(self):
        running, peak, starts = [0], [0], []
        lock = threading.Lock()

        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                    starts.append(time.monotonic())
                time.sleep(0.05)
                return self

            def __exit__(self, *a):
                with lock:
                    running[0] -= 1
                return False

        def call():
            with pipeline._youtube_dl({}):
                pass

        with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL), \
                mock.patch.object(pipeline, "YOUTUBE_MIN_INTERVAL", 0.02):
            threads = [threading.Thread(target=call) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(len(starts), 6)
        self.assertLessEqual(peak[0], pipeline.YOUTUBE_MAX_CONCURRENT)
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertTrue(all(g >= 0.015 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
