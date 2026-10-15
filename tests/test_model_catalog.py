"""模型目录：解析模型 id、排序（各系列最新版在前）、联网拉取与缓存、拉不到时的兜底。"""

import time
import unittest
from unittest import mock

from core import model_catalog as mc


def fake_get_json(anthropic=None, openrouter=None, fail=()):
    def get(url, headers):
        if "anthropic.com" in url:
            if "api" in fail:
                raise OSError("断网")
            assert headers["x-api-key"] == "k"
            return {"data": anthropic or []}
        if "openai" in fail or "openrouter" in fail:
            raise OSError("断网")
        return {"data": openrouter or []}
    return get


class ParseAndBuildTests(unittest.TestCase):
    def test_解析各种写法的模型id(self):
        self.assertEqual(mc.parse_id("claude-opus-5-5"), ("opus", (5, 5)))
        self.assertEqual(mc.parse_id("claude-haiku-4-5-20251001"), ("haiku", (4, 5)))
        self.assertEqual(mc.parse_id("anthropic/claude-fable-5.1"), ("fable", (5, 1)))
        self.assertEqual(mc.parse_id("claude-sonnet-5"), ("sonnet", (5, 0)))
        self.assertIsNone(mc.parse_id("gpt-astra"))

    def test_各系列最新版排前面_旧版在后_太老的和batch不列(self):
        opts = mc.build_options([
            ("claude-sonnet-5", "2026-06-29"), ("claude-sonnet-5-5", "2026-09-28"),
            ("claude-haiku-5-5", "2026-10-07"), ("claude-opus-5-5", "2026-09-21"),
            ("claude-3-5-sonnet-20241022", "2024-10-22"), ("claude-sonnet-4-0", "2025-05-01"),
            ("anthropic/claude-opus-5.5:batch", ""),
        ])
        ids = [o["v"] for o in opts]
        self.assertEqual(ids, ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-sonnet-5"])
        self.assertIn("推荐", opts[0]["t"])
        self.assertTrue(opts[2]["cheap"])
        self.assertIn("旧版", opts[3]["t"])

    def test_OpenRouter_每个系列前面加自动跟随最新版的别名(self):
        opts = mc.build_options([("anthropic/claude-sonnet-5.5", "2026-09-28")], latest_aliases=True)
        self.assertEqual([o["v"] for o in opts], ["~anthropic/claude-sonnet-latest", "anthropic/claude-sonnet-5.5"])


class FetchAndCacheTests(unittest.TestCase):
    def test_拉官方列表跳过已下线的_写进缓存(self):
        get = fake_get_json(
            anthropic=[{"id": "claude-sonnet-6", "created_at": "2027-01-01T00:00:00Z"},
                       {"id": "claude-opus-4-6", "created_at": "2026-02-04T00:00:00Z",
                        "retires_at": "2000-01-01T00:00:00Z"}],
            openrouter=[{"id": "anthropic/claude-sonnet-6", "created": 1798761600},
                        {"id": "anthropic/claude-sonnet-6:batch"}, {"id": "openai/gpt-x"}])
        mc.refresh(api_key="k", get_json=get)
        cat = mc.catalog(allow_fetch=False)
        self.assertEqual([o["v"] for o in cat["api"]], ["claude-sonnet-6"])
        self.assertEqual(cat["source"], {"api": "live", "openrouter": "live"})
        self.assertEqual([o["v"] for o in cat["openrouter"]],
                         ["~anthropic/claude-sonnet-latest", "anthropic/claude-sonnet-6"])

    def test_拉不到时保留缓存里的旧数据_没缓存用内置(self):
        mc.refresh(api_key="k", get_json=fake_get_json(fail=("api", "openrouter")))
        cat = mc.catalog(allow_fetch=False)
        self.assertEqual(cat["source"], {"api": "builtin", "openrouter": "builtin"})
        self.assertIn("claude-sonnet-5-5", [o["v"] for o in cat["api"]])
        self.assertIn("断网", cat["errors"]["api"])

        mc.refresh(api_key="k", get_json=fake_get_json(anthropic=[{"id": "claude-sonnet-6"}]))
        mc.refresh(api_key="k", get_json=fake_get_json(fail=("api",)))
        self.assertEqual([o["v"] for o in mc.catalog(allow_fetch=False)["api"]], ["claude-sonnet-6"])

    def test_没有Key不拉官方列表(self):
        with mock.patch.object(mc.core_keys, "resolve", return_value=""):
            mc.refresh(get_json=fake_get_json(openrouter=[]))
        self.assertIn("没有 Anthropic Key", mc.catalog(allow_fetch=False)["errors"]["api"])

    def test_缓存过期才联网_一天内不重复拉(self):
        calls = []

        def get(url, headers):
            calls.append(url)
            return {"data": []}

        with mock.patch.object(mc.core_keys, "resolve", return_value="k"):
            mc.catalog(get_json=get)
            mc.catalog(get_json=get)
            self.assertEqual(len(calls), 2)   # 第一次：官方 + OpenRouter 各一次；第二次用缓存
            with mock.patch.object(mc.time, "time", return_value=time.time() + mc.REFRESH_SECONDS + 10):
                mc.catalog(get_json=get)
        self.assertEqual(len(calls), 4)


class RouteTests(unittest.TestCase):
    def test_两个app和总入口都能取到模型目录_测试里不联网(self):
        import spark
        from apps.notes2insight import server as notes_server
        from apps.summit2md import server as summit_server
        for app in (notes_server.app, summit_server.app, spark.hub):
            r = app.test_client().get("/api/model_catalog")
            self.assertEqual(r.status_code, 200)
            d = r.get_json()
            self.assertTrue(d["api"] and d["openrouter"])
            self.assertEqual(d["source"]["api"], "builtin")


if __name__ == "__main__":
    unittest.main()
