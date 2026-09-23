import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import feedparser

from core import sources


class ExtractUrlsTests(unittest.TestCase):
    def test_中文标点紧贴链接时不会被吞进链接里(self):
        text = "看这篇：https://example.com/a，还有（https://example.com/b）和https://example.com/c。"
        self.assertEqual(
            sources.extract_urls(text),
            ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        )

    def test_去重但保留首次出现的顺序(self):
        text = "https://example.com/a 再贴一次 https://example.com/a 然后 https://example.com/b"
        self.assertEqual(sources.extract_urls(text), ["https://example.com/a", "https://example.com/b"])

    def test_没有链接返回空列表(self):
        self.assertEqual(sources.extract_urls("这段话里啥链接都没有"), [])


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

    def test_文章内嵌的相关文章区块不会混进正文(self):
        # 真实场景（实测 anthropic.com）：推荐区块直接嵌在 <article> 内部，
        # 不是页面级侧边栏，选正文容器时会被一起选进来。
        html = f"""
        <html><head><title>页面标题</title></head><body>
        <article>
          <h1>真实标题</h1>
          <p>{"这是真正的正文内容。" * 20}</p>
          <section>
            <h2>Related content</h2>
            <div><h3>另一篇完全无关的文章标题</h3><p>不应该出现在这里的简介文字。</p></div>
          </section>
        </article>
        </body></html>
        """.encode("utf-8")
        with mock.patch.object(sources, "_http_get", return_value=html):
            entry = sources.fetch_generic_article_entry("https://example.com/blog/post-1")
        self.assertNotIn("Related content", entry["article_content_html"])
        self.assertNotIn("另一篇完全无关的文章标题", entry["article_content_html"])
        self.assertIn("这是真正的正文内容", entry["article_content_html"])

    def test_相关文章区块用中文标题也能识别(self):
        html = f"""
        <html><head><title>页面标题</title></head><body>
        <article>
          <h1>真实标题</h1>
          <p>{"这是真正的正文内容。" * 20}</p>
          <aside><h2>猜你喜欢</h2><p>无关的推荐文字。</p></aside>
        </article>
        </body></html>
        """.encode("utf-8")
        with mock.patch.object(sources, "_http_get", return_value=html):
            entry = sources.fetch_generic_article_entry("https://example.com/blog/post-2")
        self.assertNotIn("猜你喜欢", entry["article_content_html"])
        self.assertNotIn("无关的推荐文字", entry["article_content_html"])

    def test_正文里恰好有同名小标题但不在section或aside里不会被整段删除(self):
        # "Related content" 这几个字如果只是正文自己一个普通小标题（没有被包在
        # section/aside 这种语义容器里），只删它的直接父元素，不会误伤更早的正文。
        html = f"""
        <html><head><title>页面标题</title></head><body>
        <article>
          <h1>真实标题</h1>
          <p>{"这是真正的正文内容。" * 20}</p>
          <div><h2>Related content</h2><p>这段紧跟在小标题后面，会被一起清掉，属于预期内的小代价。</p></div>
        </article>
        </body></html>
        """.encode("utf-8")
        with mock.patch.object(sources, "_http_get", return_value=html):
            entry = sources.fetch_generic_article_entry("https://example.com/blog/post-3")
        self.assertIn("这是真正的正文内容", entry["article_content_html"])
        self.assertNotIn("Related content", entry["article_content_html"])


FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class PdfLinkTests(unittest.TestCase):
    """链接直接指向一份 PDF（不是网页）时的处理——跑的是真正的 pypdf 抽取路径
    （用真实二进制 PDF 文件，不是拿字符串伪造的"看起来像"数据），这个库版本
    升级时最容易在这里悄悄坏掉，用真文件才测得出来。跟 uploads.py 是同一份
    fixture（tests/fixtures/sample.pdf 是它的拷贝）。
    """

    def _read_fixture(self, name: str) -> bytes:
        with open(os.path.join(FIXTURES, name), "rb") as f:
            return f.read()

    def test_url带pdf后缀能抽出真实文字(self):
        pdf_bytes = self._read_fixture("sample.pdf")
        with mock.patch.object(sources, "_http_get", return_value=pdf_bytes):
            entry = sources.fetch_generic_article_entry("https://example.com/report.pdf")
        self.assertEqual(entry["source_type"], "article")
        self.assertIn("推理成本", entry["article_text"])

    def test_没有pdf后缀但内容是pdf照样靠魔数识别(self):
        # 不少论文站点的 PDF 链接没有 .pdf 后缀（比如 arxiv.org/pdf/1706.03762），
        # 这种情况下只能靠内容开头的 %PDF- 魔数识别，不能靠 URL 形状。
        pdf_bytes = self._read_fixture("sample.pdf")
        with mock.patch.object(sources, "_http_get", return_value=pdf_bytes):
            entry = sources.fetch_generic_article_entry("https://arxiv.org/pdf/1706.03762")
        self.assertIn("推理成本", entry["article_text"])

    def test_url里的论文id不会被splitext错切(self):
        # os.path.splitext("1706.03762") 会把它当成"文件名 1706 + 扩展名 .03762"，
        # 标题会被误判成"1706"——回归保护这个具体的坑。用没有 /Title 元数据的
        # 假 PDF（sample.pdf 也没有），逼 _guess_pdf_title 走文件名兜底那条路。
        pdf_bytes = self._read_fixture("sample.pdf")
        with mock.patch.object(sources, "_http_get", return_value=pdf_bytes):
            entry = sources.fetch_generic_article_entry("https://arxiv.org/pdf/1706.03762")
        self.assertEqual(entry["title"], "1706.03762")

    def test_pdf抽出来的文字能被fetch_source_text按空行正确分段(self):
        # fixtures/sample.pdf 本身很短（凑不够 _MIN_PLAIN_BODY_LEN 的门槛），
        # 这里直接构造一条足够长的 entry，专测 article_text -> paragraphs 这条
        # 分段路径本身（PDF 抽取本身已经在上面几个用例里用真文件测过了）。
        entry = {
            "id": "x", "title": "测试论文", "url": "https://example.com/report.pdf",
            "source_type": "article",
            "article_text": ("第一段。" * 20) + "\n\n" + ("第二段。" * 20),
        }
        with tempfile.TemporaryDirectory() as cache_dir:
            result = sources.fetch_source_text(entry, cache_dir)
        self.assertIsNotNone(result)
        self.assertEqual(len(result["paragraphs"]), 2)
        self.assertTrue(result["paragraphs"][0][1].startswith("第一段。"))
        self.assertTrue(result["paragraphs"][1][1].startswith("第二段。"))

    def test_扫描版pdf没有文字层时明确报错(self):
        class _EmptyPage:
            def extract_text(self):
                return ""

        with mock.patch.object(sources, "_http_get", return_value=b"%PDF-1.4 fake"), \
             mock.patch("pypdf.PdfReader") as fake_reader:
            fake_reader.return_value.pages = [_EmptyPage()]
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_generic_article_entry("https://example.com/scanned.pdf")
        self.assertIn("扫描件", str(ctx.exception))


def _article_html(title, body="正文内容。" * 40):
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{body}</p></article></body></html>".encode()


_URLSET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/news/old-post</loc><lastmod>2025-01-01T00:00:00Z</lastmod></url>
<url><loc>https://example.com/news/new-post</loc><lastmod>2026-06-01T00:00:00Z</lastmod></url>
<url><loc>https://example.com/news/no-date-post</loc></url>
<url><loc>https://example.com/careers</loc><lastmod>2026-06-02T00:00:00Z</lastmod></url>
</urlset>"""

_SITEMAP_INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
<sitemap><loc>https://example.com/sitemap-news.xml</loc></sitemap>
</sitemapindex>"""


class SitemapPlaylistTests(unittest.TestCase):
    def test_discover从robots_txt里找sitemap声明(self):
        robots = b"User-agent: *\nSitemap: https://example.com/my-sitemap.xml\n"
        with mock.patch.object(sources, "_http_get", return_value=robots):
            self.assertEqual(
                sources._discover_sitemap_url("https://example.com/news"),
                "https://example.com/my-sitemap.xml",
            )

    def test_discover找不到robots声明就退回常见路径(self):
        def fake_get(url, timeout=20):
            if url.endswith("robots.txt"):
                return b"User-agent: *\n"
            if url == "https://example.com/sitemap.xml":
                return _URLSET_XML
            raise Exception("404")
        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            self.assertEqual(
                sources._discover_sitemap_url("https://example.com/news"),
                "https://example.com/sitemap.xml",
            )

    def test_discover全都找不到返回None(self):
        with mock.patch.object(sources, "_http_get", side_effect=Exception("404")):
            self.assertIsNone(sources._discover_sitemap_url("https://example.com/news"))

    def test_解析urlset拿到url和lastmod(self):
        children, urls = sources._parse_sitemap_xml(_URLSET_XML)
        self.assertEqual(children, [])
        self.assertEqual(len(urls), 4)
        self.assertIn(("https://example.com/news/new-post", "2026-06-01T00:00:00Z"), urls)
        self.assertIn(("https://example.com/news/no-date-post", None), urls)

    def test_解析sitemapindex拿到子sitemap链接(self):
        children, urls = sources._parse_sitemap_xml(_SITEMAP_INDEX_XML)
        self.assertEqual(urls, [])
        self.assertEqual(children, [
            "https://example.com/sitemap-pages.xml", "https://example.com/sitemap-news.xml",
        ])

    def test_端到端_只保留路径前缀匹配的页面_按lastmod新到旧排序(self):
        def fake_get(url, timeout=20):
            if url.endswith("robots.txt"):
                return b"Sitemap: https://example.com/sitemap.xml\n"
            if url == "https://example.com/sitemap.xml":
                return _URLSET_XML
            if url == "https://example.com/news/old-post":
                return _article_html("旧文章")
            if url == "https://example.com/news/new-post":
                return _article_html("新文章")
            if url == "https://example.com/news/no-date-post":
                return _article_html("没有日期的文章")
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            result = sources.fetch_sitemap_playlist("https://example.com/news")

        # /careers 不在 /news/ 前缀下，不该出现
        urls = [e["url"] for e in result["entries"]]
        self.assertNotIn("https://example.com/careers", urls)
        self.assertEqual(len(result["entries"]), 3)
        # 新文章（lastmod 更晚）排在旧文章前面
        self.assertLess(urls.index("https://example.com/news/new-post"),
                         urls.index("https://example.com/news/old-post"))
        self.assertEqual(result["content_type"], "series")
        self.assertEqual(result["summit_title"], "example.com · News")
        # lastmod 补成了 publish_date（页面本身没有 meta 发布时间）
        new_post = next(e for e in result["entries"] if e["url"].endswith("new-post"))
        self.assertEqual(new_post["publish_date"], "20260601")

    def test_单条文章抓不到正文不拖累其它条目(self):
        def fake_get(url, timeout=20):
            if url.endswith("robots.txt"):
                return b"Sitemap: https://example.com/sitemap.xml\n"
            if url == "https://example.com/sitemap.xml":
                return _URLSET_XML
            if url == "https://example.com/news/old-post":
                raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
            if url in ("https://example.com/news/new-post", "https://example.com/news/no-date-post"):
                return _article_html("正常文章")
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            result = sources.fetch_sitemap_playlist("https://example.com/news")
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["url"], "https://example.com/news/old-post")

    def test_没有sitemap也没有rss就报错(self):
        with mock.patch.object(sources, "_http_get", side_effect=Exception("404")):
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_sitemap_playlist("https://example.com/news")
        self.assertIn("sitemap", str(ctx.exception))

    def test_sitemap里没有匹配前缀的页面就报错(self):
        def fake_get(url, timeout=20):
            if url.endswith("robots.txt"):
                return b"Sitemap: https://example.com/sitemap.xml\n"
            if url == "https://example.com/sitemap.xml":
                return _URLSET_XML
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                sources.fetch_sitemap_playlist("https://example.com/blog")

    def test_sitemapindex会递归子sitemap并按名字里的关键字优先(self):
        news_urlset = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/news/from-child</loc><lastmod>2026-01-01T00:00:00Z</lastmod></url>
</urlset>"""
        calls = []

        def fake_get(url, timeout=20):
            calls.append(url)
            if url.endswith("robots.txt"):
                return b"Sitemap: https://example.com/sitemap_index.xml\n"
            if url == "https://example.com/sitemap_index.xml":
                return _SITEMAP_INDEX_XML
            if url == "https://example.com/sitemap-news.xml":
                return news_urlset
            if url == "https://example.com/sitemap-pages.xml":
                return b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>"
            if url == "https://example.com/news/from-child":
                return _article_html("来自子sitemap")
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            result = sources.fetch_sitemap_playlist("https://example.com/news")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["url"], "https://example.com/news/from-child")


if __name__ == "__main__":
    unittest.main()
