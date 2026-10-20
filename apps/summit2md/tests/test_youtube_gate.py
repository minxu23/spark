"""所有 YouTube 请求全局排队：同时最多几个、两次开始之间最少隔多久。"""

import threading
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline


class YouTubeGateTests(unittest.TestCase):
    def test_跨多个任务始终只允许一个YouTube操作_结束后再间隔(self):
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
        self.assertEqual(pipeline.YOUTUBE_MAX_CONCURRENT, 1)
        self.assertEqual(peak[0], 1)
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertTrue(all(g >= 0.015 for g in gaps), gaps)

    def test_429冷却对下一个任务也生效_异常后仍能释放队列(self):
        clock, waits = [100.0], []

        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL), \
             mock.patch.object(pipeline, "YOUTUBE_RATE_LIMIT_COOLDOWN", 60), \
             mock.patch.object(pipeline, "YOUTUBE_MIN_INTERVAL", 5), \
             mock.patch.object(pipeline.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(pipeline.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, "429"):
                with pipeline._youtube_dl({}):
                    raise RuntimeError("HTTP 429: Too Many Requests")
            with pipeline._youtube_dl({}) as other_task:
                self.assertEqual(other_task.opts["concurrent_fragment_downloads"], 1)
                self.assertEqual(clock[0], 160)
        self.assertEqual(waits, [60])

    def test_所有直接YoutubeDL构造都在全局串行入口内(self):
        import ast
        from pathlib import Path
        tree = ast.parse(Path(pipeline.__file__).read_text())
        calls = []
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "YoutubeDL"):
                        calls.append(function.name)
        self.assertEqual(calls, ["_youtube_dl"])

    def test_播放列表惰性分页也在串行入口内完成(self):
        inside = [False]

        def entries():
            self.assertTrue(inside[0], "分页迭代不能跑到串行锁外面")
            yield {"id": "abc12345678", "title": "Talk", "duration": 60}

        class FakeYDL:
            def __init__(self, opts):
                pass
            def __enter__(self):
                inside[0] = True
                return self
            def __exit__(self, *args):
                inside[0] = False
                return False
            def extract_info(self, *args, **kwargs):
                return {"id": "list", "title": "Summit", "entries": entries()}

        with mock.patch.object(pipeline.yt_dlp, "YoutubeDL", FakeYDL):
            result = pipeline._fetch_youtube_playlist("https://www.youtube.com/playlist?list=PLtest")
        self.assertEqual(len(result["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
