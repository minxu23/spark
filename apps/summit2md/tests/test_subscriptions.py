"""「信息跟进」订阅列表：存储层的增删改，以及挂在 server 上的那几个 API。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from apps.summit2md import pipeline, server, subscriptions_store, tracking


class SubscriptionsStoreTests(unittest.TestCase):
    """纯存储层：不碰网络，只测 json 文件的读写和字段规则。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            subscriptions_store, "STORE_PATH", os.path.join(self._tmp.name, "subscriptions.json")
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_没有文件时列表是空的(self):
        self.assertEqual(subscriptions_store.list_all(), [])

    def test_增加一条能读回来(self):
        item = subscriptions_store.add(
            url="https://example.com/feed", name="示例播客", category="AI 播客",
            output_dir="/tmp/out", source_type="rss",
        )
        self.assertTrue(item["id"])
        self.assertIsNone(item["last_checked_at"])
        got = subscriptions_store.get(item["id"])
        self.assertEqual(got["name"], "示例播客")
        self.assertEqual(subscriptions_store.list_all(), [got])

    def test_编辑只改允许的字段(self):
        item = subscriptions_store.add(
            url="https://example.com/feed", name="旧名字", category="旧类别",
            output_dir="/tmp/out", source_type="rss",
        )
        updated = subscriptions_store.update(item["id"], {
            "name": "新名字", "category": "新类别", "url": "https://should-not-change.example/",
        })
        self.assertEqual(updated["name"], "新名字")
        self.assertEqual(updated["category"], "新类别")
        self.assertEqual(updated["url"], "https://example.com/feed")

    def test_编辑不存在的订阅返回None(self):
        self.assertIsNone(subscriptions_store.update("no-such-id", {"name": "x"}))

    def test_删除只删这一条(self):
        a = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        b = subscriptions_store.add(url="b", name="B", category="c", output_dir="/tmp", source_type="rss")
        self.assertTrue(subscriptions_store.delete(a["id"]))
        self.assertFalse(subscriptions_store.delete(a["id"]))  # 已经删过了
        self.assertEqual([it["id"] for it in subscriptions_store.list_all()], [b["id"]])

    def test_检查时间戳会更新(self):
        item = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        subscriptions_store.touch_checked(item["id"])
        self.assertIsNotNone(subscriptions_store.get(item["id"])["last_checked_at"])

    def test_重命名类别只改这个类别下的订阅(self):
        a = subscriptions_store.add(url="a", name="A", category="旧类别", output_dir="/tmp", source_type="rss")
        b = subscriptions_store.add(url="b", name="B", category="别的类别", output_dir="/tmp", source_type="rss")
        n = subscriptions_store.rename_category("旧类别", "新类别")
        self.assertEqual(n, 1)
        self.assertEqual(subscriptions_store.get(a["id"])["category"], "新类别")
        self.assertEqual(subscriptions_store.get(b["id"])["category"], "别的类别")


def _fake_discover(entries, summit_title="示例节目"):
    return {"entries": entries, "summit_title": summit_title, "content_type": "series"}


class SubscriptionsApiTests(unittest.TestCase):
    """server 上那几个 /api/subscriptions* 路由，pipeline.fetch_playlist 全部 mock 掉——
    这层只测路由本身对不对，抓取逻辑的正确性由 core/sources、pipeline 自己的测试盯着。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            subscriptions_store, "STORE_PATH", os.path.join(self._tmp.name, "subscriptions.json")
        )
        self._patch.start()
        self.client = server.app.test_client()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_添加订阅会探测链接并存下类别(self):
        entries = [{"id": "e1", "title": "第一期", "source_type": "rss"}]
        with mock.patch.object(pipeline, "fetch_playlist", return_value=_fake_discover(entries)):
            r = self.client.post("/api/subscriptions", json={
                "url": "https://example.com/feed", "category": "AI 播客",
            })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["name"], "示例节目")  # 没填名称，落到探测出的标题
        self.assertEqual(body["category"], "AI 播客")
        self.assertEqual(body["source_type"], "rss")
        self.assertEqual(subscriptions_store.list_all()[0]["id"], body["id"])

    def test_添加订阅时链接打不开不写入列表(self):
        with mock.patch.object(pipeline, "fetch_playlist", side_effect=RuntimeError("链接失效")):
            r = self.client.post("/api/subscriptions", json={"url": "https://example.com/dead"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(subscriptions_store.list_all(), [])

    def test_不填链接直接拒绝(self):
        r = self.client.post("/api/subscriptions", json={})
        self.assertEqual(r.status_code, 400)

    def test_列出订阅(self):
        subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        r = self.client.get("/api/subscriptions")
        self.assertEqual(len(r.get_json()), 1)

    def test_编辑和删除订阅(self):
        item = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        r = self.client.patch(f"/api/subscriptions/{item['id']}", json={"name": "改名了"})
        self.assertEqual(r.get_json()["name"], "改名了")

        r = self.client.delete(f"/api/subscriptions/{item['id']}")
        self.assertEqual(r.get_json(), {"ok": True})
        self.assertEqual(subscriptions_store.list_all(), [])

    def test_编辑不存在的订阅返回404(self):
        r = self.client.patch("/api/subscriptions/no-such-id", json={"name": "x"})
        self.assertEqual(r.status_code, 404)
        r = self.client.delete("/api/subscriptions/no-such-id")
        self.assertEqual(r.status_code, 404)

    def test_检查新内容只返回manifest里没有的(self):
        with tempfile.TemporaryDirectory() as out_root:
            item = subscriptions_store.add(
                url="https://example.com/feed", name="示例节目", category="c",
                output_dir=out_root, source_type="rss",
            )
            show_dir = item["folder"]
            self.assertEqual(show_dir, os.path.join(out_root, "信息跟进", "示例节目"))
            os.makedirs(show_dir, exist_ok=True)
            pipeline._save_manifest(show_dir, {"entries": {"old-1": {"ok": True}}})

            entries = [
                {"id": "old-1", "title": "旧的一期"},
                {"id": "new-1", "title": "新的一期", "publish_date": "20260101"},
            ]
            with mock.patch.object(tracking, "list_entries", return_value=_fake_discover(entries)):
                r = self.client.post(f"/api/subscriptions/{item['id']}/check")
            body = r.get_json()
            self.assertEqual(body["new_count"], 1)
            self.assertEqual(body["new_entries"][0]["id"], "new-1")
            self.assertEqual(body["total"], 2)
            self.assertIsNotNone(subscriptions_store.get(item["id"])["last_checked_at"])

    def test_检查不存在的订阅返回404(self):
        r = self.client.post("/api/subscriptions/no-such-id/check")
        self.assertEqual(r.status_code, 404)

    def test_单条检查失败不影响返回结构(self):
        item = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        with mock.patch.object(tracking, "list_entries", side_effect=RuntimeError("暂时打不开")):
            r = self.client.post(f"/api/subscriptions/{item['id']}/check")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["new_count"], 0)
        self.assertEqual(body["error"], "暂时打不开")

    def test_批量检查逐条返回不因一条失败中断(self):
        ok_item = subscriptions_store.add(url="ok", name="OK", category="c", output_dir="/tmp", source_type="rss")
        bad_item = subscriptions_store.add(url="bad", name="Bad", category="c", output_dir="/tmp", source_type="rss")

        def _fake(sub):
            if sub["url"] == "bad":
                raise RuntimeError("打不开")
            return _fake_discover([{"id": "e1", "title": "t"}])

        with mock.patch.object(tracking, "list_entries", side_effect=_fake):
            r = self.client.post("/api/subscriptions/check_all")
        results = {row["id"]: row for row in r.get_json()["results"]}
        self.assertEqual(results[ok_item["id"]]["new_count"], 1)
        self.assertEqual(results[bad_item["id"]]["error"], "打不开")

    def test_重命名类别(self):
        subscriptions_store.add(url="a", name="A", category="旧类别", output_dir="/tmp", source_type="rss")
        r = self.client.post("/api/subscriptions/rename_category", json={"old": "旧类别", "new": "新类别"})
        self.assertEqual(r.get_json(), {"renamed": 1})
        self.assertEqual(subscriptions_store.list_all()[0]["category"], "新类别")

    def test_重命名类别缺参数拒绝(self):
        r = self.client.post("/api/subscriptions/rename_category", json={"old": "x"})
        self.assertEqual(r.status_code, 400)

    def test_批量导入按行解析名称和链接(self):
        text = (
            "MIT Technology Review AI : https://example.com/mit/feed/\n"
            "\n"
            "* MarkTechPost : https://example.com/mtp/feed/\n"
            "- 只有链接没有名字 https://example.com/noname/feed/\n"
            "https://example.com/bare/feed/\n"
        )
        parsed = server._parse_bulk_subscription_lines(text)
        self.assertEqual(parsed, [
            ("MIT Technology Review AI", "https://example.com/mit/feed/"),
            ("MarkTechPost", "https://example.com/mtp/feed/"),
            ("只有链接没有名字", "https://example.com/noname/feed/"),
            ("", "https://example.com/bare/feed/"),
        ])

    def test_批量导入逐条探测有失败也有成功(self):
        text = "A : https://a.example/feed\nB : https://b.example/feed\n"

        def _fake(url, light=False):
            if "a.example" in url:
                return _fake_discover([{"id": "e1", "title": "t"}], summit_title="A 探测到的标题")
            raise RuntimeError("B 打不开")

        with mock.patch.object(pipeline, "fetch_playlist", side_effect=_fake):
            r = self.client.post("/api/subscriptions/bulk", json={"text": text, "category": "批量类"})
        body = r.get_json()
        self.assertEqual(len(body["added"]), 1)
        self.assertEqual(body["added"][0]["name"], "A")
        self.assertEqual(body["added"][0]["category"], "批量类")
        self.assertEqual(len(body["failed"]), 1)
        self.assertEqual(body["failed"][0]["error"], "B 打不开")

    def test_批量导入名称留空时用探测到的标题(self):
        text = "https://a.example/feed\n"
        with mock.patch.object(pipeline, "fetch_playlist",
                                return_value=_fake_discover([{"id": "e1"}], summit_title="探测标题")):
            r = self.client.post("/api/subscriptions/bulk", json={"text": text})
        self.assertEqual(r.get_json()["added"][0]["name"], "探测标题")

    def test_批量导入跳过已经订阅过的链接(self):
        subscriptions_store.add(url="https://a.example/feed", name="已订阅", category="c",
                                 output_dir="/tmp", source_type="rss")
        text = "A : https://a.example/feed\nB : https://b.example/feed\n"
        with mock.patch.object(pipeline, "fetch_playlist",
                                return_value=_fake_discover([{"id": "e1"}], summit_title="B")):
            r = self.client.post("/api/subscriptions/bulk", json={"text": text})
        body = r.get_json()
        self.assertEqual(len(body["added"]), 1)
        self.assertEqual(body["added"][0]["url"], "https://b.example/feed")
        self.assertEqual(body["failed"][0]["error"], "已经订阅过了，跳过")

    def test_批量导入空文本或没有链接时拒绝(self):
        r = self.client.post("/api/subscriptions/bulk", json={"text": "随便写点什么，没有链接"})
        self.assertEqual(r.status_code, 400)


    def test_检查全部只查打开了自动检查的订阅(self):
        auto = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        manual = subscriptions_store.add(url="m", name="M", category="c", output_dir="/tmp",
                                         source_type="rss", auto_check=False)
        with mock.patch.object(tracking, "list_entries",
                               return_value=_fake_discover([{"id": "e1", "title": "t"}])) as fetch:
            body = self.client.post("/api/subscriptions/check_all").get_json()
        self.assertEqual([row["id"] for row in body["results"]], [auto["id"]])
        self.assertEqual(body["skipped"], 1)
        fetch.assert_called_once()
        # 手动点「检查」照样能查
        with mock.patch.object(tracking, "list_entries",
                               return_value=_fake_discover([{"id": "e1", "title": "t"}])):
            r = self.client.post(f"/api/subscriptions/{manual['id']}/check")
        self.assertEqual(r.get_json()["new_count"], 1)

    def test_老订阅没有开关时按自动检查处理(self):
        with open(subscriptions_store.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"id": "x", "url": "u", "name": "旧", "category": "c", "output_dir": "/tmp"}], f)
        self.assertTrue(subscriptions_store.get("x")["auto_check"])

    def test_单条和整个类别开关自动检查(self):
        a = subscriptions_store.add(url="a", name="A", category="c", output_dir="/tmp", source_type="rss")
        b = subscriptions_store.add(url="b", name="B", category="c", output_dir="/tmp", source_type="rss")
        r = self.client.patch(f"/api/subscriptions/{a['id']}", json={"auto_check": False})
        self.assertFalse(r.get_json()["auto_check"])
        self.assertEqual(self.client.patch(f"/api/subscriptions/{a['id']}",
                                           json={"auto_check": "no"}).status_code, 400)
        r = self.client.post("/api/subscriptions/auto_check", json={"ids": [a["id"], b["id"]], "auto_check": False})
        self.assertEqual(r.get_json(), {"changed": 1})
        self.assertFalse(any(s["auto_check"] for s in subscriptions_store.list_all()))
        self.assertEqual(self.client.post("/api/subscriptions/auto_check", json={"ids": []}).status_code, 400)

    def test_批量导入可以粘贴OPML_并选择不自动检查(self):
        opml = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>Blog Feeds</title></head><body>
  <outline text="Blogs" title="Blogs">
    <outline type="rss" text="simonwillison.net" title="simonwillison.net" xmlUrl="https://simonwillison.net/atom/everything/" htmlUrl="https://simonwillison.net"/>
    <outline type="rss" text="antirez.com" title="antirez.com" xmlUrl="http://antirez.com/rss"/>
    <outline type="rss" text="坏的" xmlUrl="file:///etc/passwd"/>
  </outline>
</body></opml>"""
        self.assertEqual(server._parse_bulk_subscription_lines(opml), [
            ("simonwillison.net", "https://simonwillison.net/atom/everything/"),
            ("antirez.com", "http://antirez.com/rss"),
        ])
        with mock.patch.object(pipeline, "fetch_playlist",
                               return_value=_fake_discover([{"id": "e1", "source_type": "rss"}])):
            body = self.client.post("/api/subscriptions/bulk", json={
                "text": opml, "category": "HN 热门博客", "auto_check": False}).get_json()
        self.assertEqual([it["name"] for it in body["added"]], ["simonwillison.net", "antirez.com"])
        self.assertTrue(all(it["auto_check"] is False for it in body["added"]))

    def test_OPML_声明的编码_没转义的与号_都能正常解析(self):
        opml = ('<?xml version="1.0" encoding="ISO-8859-1"?>\n<opml version="1.0"><body>'
                '<outline title="中文 A&B" htmlUrl="https://a.example/" xmlUrl="https://a.example/feed?x=1&y=2"/>'
                '</body></opml>')
        self.assertEqual(server._parse_bulk_subscription_lines(opml),
                         [("中文 A&B", "https://a.example/feed?x=1&y=2")])

    def test_OPML_带_DOCTYPE_或解析失败时报错而不是按行拆(self):
        for bad in ('<!-- x --><!DOCTYPE opml [<!ENTITY a "aaaa">]><!-- y --><opml><body>'
                    '<outline title="&a;" xmlUrl="https://a.example/feed"/></body></opml>',
                    '<opml><body><outline title="x" xmlUrl="https://a.example/feed"></body></opml>'):
            with self.assertRaises(server.BadOpml):
                server._parse_bulk_subscription_lines(bad)
            r = self.client.post("/api/subscriptions/bulk", json={"text": bad})
            self.assertEqual(r.status_code, 400)

    def test_探测期间被别处先加上的链接不重复添加(self):
        def fake_fetch(url, light=True):
            # 探测进行中，另一个请求先把同一个链接加进去了
            if not subscriptions_store.list_all():
                subscriptions_store.add(url=url, name="先到", category="c", output_dir="/tmp", source_type="rss")
            return _fake_discover([{"id": "e1", "source_type": "rss"}])

        with mock.patch.object(pipeline, "fetch_playlist", side_effect=fake_fetch):
            body = self.client.post("/api/subscriptions/bulk", json={"text": "https://a.example/feed"}).get_json()
            self.assertEqual(body["added"], [])
            self.assertEqual(len(body["failed"]), 1)
            r = self.client.post("/api/subscriptions", json={"url": "https://a.example/feed"})
            self.assertEqual(r.status_code, 409)
        self.assertEqual(len(subscriptions_store.list_all()), 1)

if __name__ == "__main__":
    unittest.main()
