import os
import tempfile
import socket
import unittest
import urllib.error
import urllib.request
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

    def _fake_get(self, xml):
        return mock.patch.object(sources, "_http_get", return_value=xml.encode("utf-8"))

    def test_解析条目_标题_发布时间(self):
        with self._fake_get(self._FEED_XML):
            result = sources.fetch_rss_playlist("https://example.com/feed.xml")
        self.assertEqual(result["summit_title"], "测试播客节目")
        self.assertEqual(result["content_type"], "series")
        entry = result["entries"][0]
        self.assertEqual(entry["title"], "第一期：开场")
        self.assertEqual(entry["source_type"], "rss")
        self.assertEqual(entry["publish_date"], "20260101")

    def test_下载带超时_不交给feedparser自己去请求(self):
        with self._fake_get(self._FEED_XML) as fake:
            sources.fetch_rss_playlist("https://example.com/feed.xml")
        fake.assert_called_once_with("https://example.com/feed.xml", timeout=sources.RSS_TIMEOUT)

    def test_没有条目就报错(self):
        empty_feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>空节目</title></channel></rss>"""
        with self._fake_get(empty_feed):
            with self.assertRaises(RuntimeError):
                sources.fetch_rss_playlist("https://example.com/feed.xml")

    def test_链接实际是404页面时报错说明是http状态而不是xml格式问题(self):
        # 常见情况：链接猜错了（网站首页/播客落地页而不是真正的 feed 地址）——
        # 报错里要亮出 HTTP 状态码，而不是一句看不懂的 XML 解析错误。
        err = urllib.error.HTTPError("https://example.com/feed/", 404, "Not Found", {}, None)
        with mock.patch.object(sources, "_http_get", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_rss_playlist("https://example.com/feed/")
        self.assertIn("404", str(ctx.exception))

    def test_超时报错说明打不开而不是一直挂着(self):
        with mock.patch.object(sources, "_http_get", side_effect=TimeoutError("timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                sources.fetch_rss_playlist("https://example.com/feed.xml")
        self.assertIn("无法打开", str(ctx.exception))

    def test_条目内容够长直接用不用再抓文章页(self):
        with self._fake_get(self._FEED_XML):
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

    def test_没写scheme的链接也按https去找robots(self):
        seen = []

        def fake_get(url, timeout=20):
            seen.append(url)
            raise Exception("404")

        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                sources.fetch_sitemap_playlist("example.com/news", fetch_bodies=False)
        self.assertEqual(seen[0], "https://example.com/robots.txt")

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


class HttpGetGuardTests(unittest.TestCase):
    """_http_get 要抓的链接很多来自 feed/robots.txt，不是用户亲手填的。"""

    def test_只放行_http_https(self):
        for url in ("file:///etc/hosts", "ftp://example.com/x", "data:text/plain,hi"):
            with self.assertRaises(urllib.error.URLError, msg=url):
                sources._http_get(url)

    def test_不抓本机回环和链路本地地址(self):
        for url in ("http://127.0.0.1:8760/api/jobs", "http://localhost/", "http://[::1]/",
                    "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/"):
            with self.assertRaises(urllib.error.URLError, msg=url):
                sources._http_get(url)

    def test_跳转到本机地址或_file_都拦住(self):
        handler = sources._SafeRedirectHandler()
        for url in ("file:///etc/passwd", "http://127.0.0.1:8760/api/jobs",
                    "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(urllib.error.URLError, msg=url):
                handler.redirect_request(mock.Mock(), None, 302, "Found", {}, url)

    def test_IPv6_里夹带的本机地址也拦住(self):
        for addr in ("::127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1", "64:ff9b::a9fe:a9fe",
                     "64:ff9b:1::7f00:1", "64:ff9b::0.0.0.0", "::ffff:0.0.0.0",
                     "64:ff9b:1:7f00:0:100::",       # /48 排布的 127.0.0.1
                     "64:ff9b:1:ab7f:0:1::",         # /56
                     "64:ff9b:1:abcd:7f:0:100::"):   # /64
            info = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 80, 0, 0))]
            with mock.patch.object(sources.socket, "getaddrinfo", return_value=info):
                with self.assertRaises(urllib.error.URLError, msg=addr):
                    sources._check_fetchable("http://evil.example/")
        for addr in ("2606:4700::1111", "64:ff9b:1:808:8:800::", "64:ff9b::808:808"):
            info = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 80, 0, 0))]
            with mock.patch.object(sources.socket, "getaddrinfo", return_value=info):
                sources._check_fetchable("http://ok.example/")

    def _fake_open(self, read=None, error=None):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        if read is not None:
            resp.read.side_effect = read
        opener = mock.Mock()
        opener.open.side_effect = error or (lambda *a, **k: resp)
        return mock.patch.multiple(sources, _opener=opener, _check_fetchable=mock.Mock())

    def test_读正文时超时_断开都包成_URLError(self):
        import http.client
        for exc in (TimeoutError("timed out"), http.client.IncompleteRead(b"x"),
                    ConnectionResetError("reset")):
            with self._fake_open(read=exc):
                with self.assertRaises(urllib.error.URLError, msg=repr(exc)):
                    sources._http_get("https://example.com/a")

    def test_不请自来的gzip正文会解压(self):
        import gzip
        body = gzip.compress(b"<rss></rss>")
        with self._fake_open(read=lambda n: body):
            self.assertEqual(sources._http_get("https://example.com/feed"), b"<rss></rss>")

    def test_分成几段的gzip全部解开_断了半截的原样返回(self):
        import gzip
        multi = gzip.compress(b"<rss>part1") + gzip.compress(b"part2</rss>")
        with self._fake_open(read=lambda n: multi):
            self.assertEqual(sources._http_get("https://example.com/feed"), b"<rss>part1part2</rss>")
        with self._fake_open(read=lambda n: gzip.compress(b"hello") + b"\n"):
            self.assertEqual(sources._http_get("https://example.com/feed"), b"hello")
        whole = gzip.compress(bytes(range(256)) * 50)
        cut = whole[:len(whole) // 2]
        with self._fake_open(read=lambda n: cut):
            self.assertEqual(sources._http_get("https://example.com/feed"), cut)

    def test_解压后超过上限也不要(self):
        import gzip
        bomb = gzip.compress(b"\0" * 10_000_000)
        with self._fake_open(read=lambda n: bomb):
            with self.assertRaises(urllib.error.URLError):
                sources._http_get("https://example.com/feed", max_bytes=1_000_000)

    def test_超过大小上限不下载(self):
        with self._fake_open(read=lambda n: b"x" * n):
            with self.assertRaises(urllib.error.URLError):
                sources._http_get("https://example.com/big", max_bytes=10)


def _addrinfo(*addrs, port=80):
    out = []
    for a in addrs:
        if ":" in a:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, port, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)))
    return out


class PrivateAddressWarningTests(unittest.TestCase):
    """内网地址不拦，照常抓，只提醒；同一个任务里同一个主机只提醒一次。"""

    def _check(self, url, *addrs):
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=_addrinfo(*addrs)):
            return sources._check_fetchable(url)

    def test_内网_IPv4_IPv6_照常放行但会提醒(self):
        for addr in ("192.168.1.5", "10.0.0.8", "172.16.3.4", "100.64.1.1", "fd00::5",
                     "::ffff:192.168.1.5"):
            got = []
            with sources.collect_fetch_warnings(got.append) as warns:
                addrs = self._check("http://nas.example/feed", addr)
            self.assertEqual(addrs[0][1][0], addr)
            self.assertEqual(len(warns.messages), 1, addr)
            self.assertEqual(got, warns.messages)
            self.assertIn("nas.example", got[0])
            self.assertIn(addr, got[0])
            self.assertIn("已照常抓取", got[0])

    def test_公网地址不提醒_夹带公网IPv4的也不提醒(self):
        # 2002:808:808:: 是 6to4 包着的 8.8.8.8，本身不算"全局"地址，不能误报
        for addr in ("93.184.216.34", "2606:4700::1111", "2002:808:808::1", "64:ff9b::808:808"):
            with sources.collect_fetch_warnings() as warns:
                self._check("http://ok.example/", addr)
            self.assertEqual(warns.messages, [], addr)

    def test_同一主机只提醒一次_不同主机各一次(self):
        with sources.collect_fetch_warnings() as warns:
            for _ in range(3):
                self._check("http://nas.example/a", "192.168.1.5")
            self._check("http://nas.example:8080/b", "192.168.1.5")
            self._check("http://other.example/", "10.1.2.3")
        self.assertEqual(len(warns.messages), 2)

    def test_没开收集器时写到标准错误_不影响抓取(self):
        import io
        err = io.StringIO()
        with mock.patch.object(sources.sys, "stderr", err):
            self._check("http://nas.example/", "192.168.1.5")
        self.assertIn("192.168.1.5", err.getvalue())

    def test_本机和夹带本机的地址照样拦住_就算同时有内网地址(self):
        for addrs in (("192.168.1.5", "127.0.0.1"), ("10.0.0.1", "64:ff9b::7f00:1"), ("fe80::1",)):
            with sources.collect_fetch_warnings():
                with self.assertRaises(urllib.error.URLError, msg=addrs):
                    self._check("http://evil.example/", *addrs)

    def test_跳转到内网地址会提醒_跳转到本机拦住(self):
        handler = sources._SafeRedirectHandler()
        req = urllib.request.Request("https://pub.example/a")
        with sources.collect_fetch_warnings() as warns:
            with mock.patch.object(sources.socket, "getaddrinfo", return_value=_addrinfo("192.168.9.9")):
                new = handler.redirect_request(req, None, 302, "Found", {}, "http://intranet.example/x")
        self.assertEqual(new.full_url, "http://intranet.example/x")
        self.assertEqual(new._spark_pinned, ("intranet.example", [(socket.AF_INET, ("192.168.9.9", 80))]))
        self.assertEqual(len(warns.messages), 1)
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(urllib.error.URLError):
                handler.redirect_request(req, None, 302, "Found", {}, "http://rebind.example/x")


class PinnedConnectionTests(unittest.TestCase):
    """检查过的地址就是连接用的地址：检查完 DNS 再换答案也连不到别处去。"""

    def test_DNS_换了答案也只连检查过的那个IP(self):
        answers = [_addrinfo("93.184.216.34"), _addrinfo("127.0.0.1")]
        connected = []

        class FakeSock:
            def __init__(self, family, type_):
                pass

            def settimeout(self, t):
                pass

            def connect(self, sockaddr):
                connected.append(sockaddr)
                raise ConnectionRefusedError("假的，不真连")

            def close(self):
                pass

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), sources._SafeRedirectHandler,
                                             sources._PinnedHTTPHandler, sources._PinnedHTTPSHandler)
        with mock.patch.object(sources.socket, "getaddrinfo", side_effect=answers) as gai, \
             mock.patch.object(sources.socket, "socket", FakeSock), \
             mock.patch.object(sources, "_opener", opener):
            with self.assertRaises(urllib.error.URLError):
                sources._http_get("http://rebind.example/feed")
        self.assertEqual(connected, [("93.184.216.34", 80)])
        self.assertEqual(gai.call_count, 1)   # 连接时没有再解析一次

    def test_连的是钉住的IP_Host头还是原来的域名(self):
        import http.server
        import threading
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["host"] = self.headers.get("Host")
                body = b"pinned ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), sources._PinnedHTTPHandler)
            url = f"http://pinned.example:{port}/x"
            req = urllib.request.Request(url)
            # 直接钉一个地址（绕开 _check_fetchable 对本机的拦截，只验证"连到钉住的 IP"）
            sources._pin(req, url, [(socket.AF_INET, ("127.0.0.1", port))])
            with mock.patch.object(sources.socket, "getaddrinfo", side_effect=AssertionError("不该再解析")):
                with opener.open(req, timeout=5) as resp:
                    self.assertEqual(resp.read(), b"pinned ok")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(seen["host"], f"pinned.example:{port}")

    def test_没钉地址或钉的不是这个主机时当场检查(self):
        req = urllib.request.Request("http://other.example/")
        sources._pin(req, "http://first.example/", [(socket.AF_INET, ("93.184.216.34", 80))])
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(urllib.error.URLError):
                sources._pinned_addrs(req)

    def test_走代理时不钉IP_交给代理解析(self):
        for url, proxy_type in (("http://site.example/", "http"), ("https://site.example/", "http")):
            req = urllib.request.Request(url)
            sources._pin(req, url, [(socket.AF_INET, ("93.184.216.34", 80))])
            req.set_proxy("proxy.local:3128", proxy_type)
            self.assertIsNone(sources._pinned_addrs(req), url)


class SitemapFetchOnceTests(unittest.TestCase):
    def test_猜到的_sitemap_只下载一次(self):
        xml = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               b'<url><loc>https://example.com/news/a</loc></url></urlset>')
        calls = []

        def fake_get(url, timeout=20):
            calls.append(url)
            if url.endswith("robots.txt"):
                raise urllib.error.URLError("404")
            return xml

        with mock.patch.object(sources, "_http_get", side_effect=fake_get):
            result = sources.fetch_sitemap_playlist("https://example.com/news", fetch_bodies=False)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(calls.count("https://example.com/sitemap.xml"), 1)


class SourceTextCacheMetaTests(unittest.TestCase):
    def test_缓存命中时仍用当初抓到的真实标题和日期(self):
        fetched = {"article_content_html": "<p>" + "正文内容。" * 60 + "</p>",
                   "title": "真实标题", "publish_date": "20260920"}
        with tempfile.TemporaryDirectory() as cache_dir:
            with mock.patch.object(sources, "fetch_generic_article_entry", return_value=fetched):
                sources.fetch_source_text({"id": "a1", "url": "https://x/a", "source_type": "article",
                                           "title": "A"}, cache_dir)
            again = {"id": "a1", "url": "https://x/a", "source_type": "article", "title": "A"}
            with mock.patch.object(sources, "fetch_generic_article_entry") as refetch:
                got = sources.fetch_source_text(again, cache_dir)
            refetch.assert_not_called()
        self.assertTrue(got["paragraphs"])
        self.assertNotIn("entry_meta", got)
        self.assertEqual((again["title"], again["publish_date"]), ("真实标题", "20260920"))


def test_gbk_网页按_meta_charset_解码_不会整篇乱码():
    body = "<html><head><meta charset=\"gb2312\"></head><body><article>" + \
        "".join(f"<p>这是第{i}段正文，讲的是国产大模型推理成本的变化，内容足够长才会被当成正文。</p>" for i in range(12)) + \
        "</article></body></html>"
    raw = body.encode("gb2312")
    with mock.patch.object(sources, "_http_get", return_value=raw):
        entry = sources.fetch_generic_article_entry("https://example.com/news/1")
    assert "�" not in entry["article_content_html"]
    assert "国产大模型推理成本" in entry["article_content_html"]
