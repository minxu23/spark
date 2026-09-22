import os
import tempfile
import unittest
from unittest import mock

import feedparser

from apps.summit2md import sources


class UrlClassificationTests(unittest.TestCase):
    def test_apple_podcast域名识别(self):
        self.assertTrue(sources.is_apple_podcast_url(
            "https://podcasts.apple.com/us/podcast/the-daily/id1200361736"))
        self.assertFalse(sources.is_apple_podcast_url("https://example.com/id123"))

    def test_微信文章链接识别(self):
        self.assertTrue(sources.is_wechat_article_url("https://mp.weixin.qq.com/s/abc123"))
        self.assertFalse(sources.is_wechat_article_url("https://example.com/s/abc123"))

    def test_rss地址靠常见后缀_路径识别(self):
        self.assertTrue(sources.is_rss_url("https://example.com/feed.xml"))
        self.assertTrue(sources.is_rss_url("https://example.com/rss"))
        self.assertTrue(sources.is_rss_url("https://example.com/feed"))
        # 很多播客 feed 没有固定后缀/路径规律，识别不出来是预期内的——
        # fetch_playlist 会在 Substack 解析失败后再兜底当 RSS 试一次。
        self.assertFalse(sources.is_rss_url("https://feeds.simplecast.com/54nAGcIl"))


class WechatArticleTests(unittest.TestCase):
    def _fake_html(self, body_paragraph: str, *, create_time="1700000000") -> bytes:
        return f"""
        <html><body>
        <h1 id="activity-name">测试文章标题</h1>
        <div id="meta_content"><span id="js_name">测试公众号</span></div>
        <div id="js_content"><p>{body_paragraph}</p></div>
        <script>var oriCreateTime = "{create_time}";</script>
        </body></html>
        """.encode("utf-8")

    def test_解析标题_账号_发布时间(self):
        html = self._fake_html("这是正文内容。" * 20)
        with mock.patch.object(sources, "_http_get", return_value=html):
            result = sources.fetch_wechat_article_playlist("https://mp.weixin.qq.com/s/testtoken")
        self.assertEqual(result["summit_title"], "测试公众号")
        self.assertEqual(result["content_type"], "series")
        entry = result["entries"][0]
        self.assertEqual(entry["title"], "测试文章标题")
        self.assertEqual(entry["source_type"], "wechat")
        self.assertEqual(entry["publish_date"], "20231114")

    def test_找不到正文容器就报错(self):
        html = b"<html><body><p>no content div here</p></body></html>"
        with mock.patch.object(sources, "_http_get", return_value=html):
            with self.assertRaises(RuntimeError):
                sources.fetch_wechat_article_playlist("https://mp.weixin.qq.com/s/testtoken")

    def test_正文太短当没有有效内容(self):
        # 整篇文章正文只有一句付费墙/登录提示这种空壳，不该被当成抓到了正文。
        html = self._fake_html("仅限登录查看")
        with tempfile.TemporaryDirectory() as cache_dir:
            with mock.patch.object(sources, "_http_get", return_value=html):
                playlist = sources.fetch_wechat_article_playlist("https://mp.weixin.qq.com/s/testtoken")
            entry = playlist["entries"][0]
            self.assertIsNone(sources.fetch_source_text(entry, cache_dir))

    def test_抓到的正文会缓存到本地不用重新请求(self):
        html = self._fake_html("这是正文内容。" * 20)
        with tempfile.TemporaryDirectory() as cache_dir:
            with mock.patch.object(sources, "_http_get", return_value=html) as fake_get:
                playlist = sources.fetch_wechat_article_playlist("https://mp.weixin.qq.com/s/testtoken")
                entry = playlist["entries"][0]
                first = sources.fetch_source_text(entry, cache_dir)
                self.assertEqual(fake_get.call_count, 1)
                second = sources.fetch_source_text(entry, cache_dir)
            self.assertEqual(fake_get.call_count, 1)  # 第二次没有再发请求
            self.assertEqual(first, second)
            self.assertEqual(first["lang"], "zh")
            self.assertTrue(os.path.isdir(cache_dir))


class RssPlaylistTests(unittest.TestCase):
    _FEED_XML = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>测试播客节目</title>
      <language>zh</language>
      <item>
        <title>第一期：开场</title>
        <link>https://example.com/ep1</link>
        <guid>https://example.com/ep1</guid>
        <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
        <description>%s</description>
      </item>
    </channel></rss>
    """ % ("这是这一期的节目简介。" * 20)

    def test_解析条目_标题_发布时间(self):
        with mock.patch.object(sources.feedparser, "parse",
                                return_value=sources.feedparser.parse(self._FEED_XML)):
            result = sources.fetch_rss_playlist("https://example.com/feed.xml")
        self.assertEqual(result["summit_title"], "测试播客节目")
        self.assertEqual(result["content_type"], "series")
        entry = result["entries"][0]
        self.assertEqual(entry["title"], "第一期：开场")
        self.assertEqual(entry["source_type"], "rss")
        self.assertEqual(entry["publish_date"], "20260101")

    def test_没有条目就报错(self):
        empty_feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>空节目</title></channel></rss>"""
        with mock.patch.object(sources.feedparser, "parse",
                                return_value=sources.feedparser.parse(empty_feed)):
            with self.assertRaises(RuntimeError):
                sources.fetch_rss_playlist("https://example.com/feed.xml")

    def test_链接实际是404页面时报错说明是http状态而不是xml格式问题(self):
        # 常见情况：链接猜错了（网站首页/播客落地页而不是真正的 feed 地址），
        # 服务器返回一个 HTML 的 404 页面——feedparser 会把它当 XML 硬解析，
        # bozo_exception 只会是一句"格式不合法"，看不出真正原因。
        fake_result = feedparser.util.FeedParserDict({
            "feed": feedparser.util.FeedParserDict(),
            "entries": [],
            "bozo": 1,
            "status": 404,
        })
        with mock.patch.object(sources.feedparser, "parse", return_value=fake_result):
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_rss_playlist("https://example.com/feed/")
        self.assertIn("404", str(ctx.exception))

    def test_条目内容够长直接用不用再抓文章页(self):
        with mock.patch.object(sources.feedparser, "parse",
                                return_value=sources.feedparser.parse(self._FEED_XML)):
            result = sources.fetch_rss_playlist("https://example.com/feed.xml")
        entry = result["entries"][0]
        with tempfile.TemporaryDirectory() as cache_dir:
            with mock.patch.object(sources, "_fetch_generic_article_paragraphs") as fallback:
                sub = sources.fetch_source_text(entry, cache_dir)
        fallback.assert_not_called()
        self.assertTrue(sub["paragraphs"])
        self.assertEqual(sub["lang"], "zh")


class ApplePodcastTests(unittest.TestCase):
    def test_通过itunes查询接口换到真正的feed地址后按rss处理(self):
        lookup_payload = (
            '{"results": [{"feedUrl": "https://example.com/feed.xml", '
            '"collectionName": "测试播客节目"}]}'
        ).encode("utf-8")
        fake_playlist = {
            "summit_title": "测试播客节目", "playlist_id": "x", "entries": [], "content_type": "series",
        }
        with mock.patch.object(sources, "_http_get", return_value=lookup_payload):
            with mock.patch.object(sources, "fetch_rss_playlist", return_value=dict(fake_playlist)) as fake_fetch:
                result = sources.fetch_apple_podcast_playlist(
                    "https://podcasts.apple.com/us/podcast/x/id1200361736")
        fake_fetch.assert_called_once_with("https://example.com/feed.xml")
        self.assertEqual(result["summit_title"], "测试播客节目")

    def test_没有id就报错(self):
        with self.assertRaises(RuntimeError):
            sources.fetch_apple_podcast_playlist("https://podcasts.apple.com/us/podcast/x/")


class GenericArticleEntryTests(unittest.TestCase):
    def test_解析标题_正文_发布时间(self):
        html = f"""
        <html><head><title>页面标题</title>
        <meta property="article:published_time" content="2026-01-02T03:04:05Z" />
        </head><body>
        <article><h1>文章标题</h1><p>{"正文内容。" * 30}</p></article>
        </body></html>
        """.encode("utf-8")
        with mock.patch.object(sources, "_http_get", return_value=html):
            entry = sources.fetch_generic_article_entry("https://example.com/blog/post-1")
        self.assertEqual(entry["title"], "文章标题")
        self.assertEqual(entry["source_type"], "article")
        self.assertEqual(entry["publish_date"], "20260102")

    def test_是订阅源内容而不是文章就报错_不会把整份feed当成正文(self):
        # 是 is_rss_url() 逮不住的 feed 地址（比如 feeds.xxx.com/xxx 这类没有固定
        # 后缀的），如果漏网走到这里，不该把整份 XML 当成一段"正文"存下来。
        xml = '<?xml version="1.0"?><rss version="2.0"><channel><title>某播客</title></channel></rss>'.encode("utf-8")
        with mock.patch.object(sources, "_http_get", return_value=xml):
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_generic_article_entry("https://feeds.example.com/x")
        self.assertIn("订阅源", str(ctx.exception))

    def test_找不到正文就报错(self):
        html = b"<html><body><p>too short</p></body></html>"
        with mock.patch.object(sources, "_http_get", return_value=html):
            with self.assertRaises(RuntimeError):
                sources.fetch_generic_article_entry("https://example.com/empty")


if __name__ == "__main__":
    unittest.main()
