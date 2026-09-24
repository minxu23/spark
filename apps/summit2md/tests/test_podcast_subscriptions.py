"""「Podcast 跟进」的订阅：跟信息跟进放在同一个文件里，用 kind 区分；「更新」把
新单集交给跟临时链接同一套 process_job。模型和网络全部 mock 掉。"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from apps.summit2md import pipeline, server, subscriptions_store, tracking


def _discover(entries, title="示例节目"):
    return {"entries": entries, "summit_title": title, "content_type": "series"}


class PodcastSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self._patch = mock.patch.object(subscriptions_store, "STORE_PATH",
                                        os.path.join(self.root, "subscriptions.json"))
        self._patch.start()
        self.client = server.app.test_client()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _add(self, url="https://pod.example/feed", kind="podcast", **kw):
        with mock.patch.object(pipeline, "fetch_playlist",
                               return_value=_discover([{"id": "e1", "source_type": "rss"}])):
            return self.client.post("/api/subscriptions", json={
                "url": url, "kind": kind, "output_dir": self.root, **kw})

    def test_两种订阅各看各的_同一链接可以各订一次(self):
        self.assertEqual(self._add(kind="track").status_code, 200)
        r = self._add(kind="podcast")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._add(kind="podcast").status_code, 409)

        pods = self.client.get("/api/subscriptions?kind=podcast").get_json()
        tracks = self.client.get("/api/subscriptions").get_json()
        self.assertEqual([p["kind"] for p in pods], ["podcast"])
        self.assertEqual([t["kind"] for t in tracks], ["track"])
        # 节目文件夹就是 输出目录/节目名，跟临时链接处理同一个节目是同一个文件夹
        self.assertEqual(pods[0]["folder"], os.path.join(self.root, "示例节目"))
        self.assertEqual(tracks[0]["folder"], os.path.join(self.root, "信息跟进", "示例节目"))

    def test_老订阅没有_kind_算信息跟进(self):
        with open(subscriptions_store.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"id": "a", "url": "https://x/feed", "name": "X", "folder": "/tmp/x"}], f)
        self.assertEqual(len(self.client.get("/api/subscriptions").get_json()), 1)
        self.assertEqual(self.client.get("/api/subscriptions?kind=podcast").get_json(), [])

    def test_检查全部和类别改名只动这一种(self):
        self._add(kind="track", category="AI")
        self._add(url="https://pod.example/2", kind="podcast", category="AI")
        with mock.patch.object(tracking, "check", side_effect=lambda s: {"id": s["id"], "kind": s["kind"]}):
            res = self.client.post("/api/subscriptions/check_all", json={"kind": "podcast"}).get_json()
        self.assertEqual([r["kind"] for r in res["results"]], ["podcast"])
        self.client.post("/api/subscriptions/rename_category", json={"old": "AI", "new": "播客", "kind": "podcast"})
        cats = {s["kind"]: s["category"] for s in subscriptions_store.list_all()}
        self.assertEqual(cats, {"track": "AI", "podcast": "播客"})

    def test_更新只把没处理过的单集交给_process_job(self):
        sub = self._add().get_json()
        folder = sub["folder"]
        os.makedirs(folder)
        with open(os.path.join(folder, ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {"e1": {"ok": True}, "e2": {"ok": False, "error": "超时"}}}, f)
        entries = [{"id": f"e{i}", "title": f"第{i}期", "source_type": "rss"} for i in (1, 2, 3)]
        seen = {}

        def fake_launch(payload):
            seen.update(payload)
            return {"job_id": "job123"}, 200

        with mock.patch.object(tracking, "list_entries", return_value=_discover(entries)), \
             mock.patch.object(server, "_launch_run", side_effect=fake_launch):
            r = self.client.post(f"/api/subscriptions/{sub['id']}/update", json={
                "backend": "api", "api_key": "k", "summary_length": "short",
                "output_dir": "/别处", "summit_title": "别的名字"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["count"], 2)
        self.assertEqual([e["id"] for e in seen["entries"]], ["e2", "e3"])   # 失败过的也重试
        self.assertNotIn("last_error", seen["entries"][0])
        # 节目名和输出目录只由订阅决定，请求里带的不算
        self.assertEqual((seen["output_dir"], seen["summit_title"]), (self.root, "示例节目"))
        self.assertEqual((seen["content_type"], seen["source_url"]), ("series", "https://pod.example/feed"))
        self.assertEqual((seen["summary_length"], seen["api_key"]), ("short", "k"))

    def test_更新可以只挑几期_没有新单集时报错(self):
        sub = self._add().get_json()
        entries = [{"id": "e1", "source_type": "rss"}, {"id": "e2", "source_type": "rss"}]
        with mock.patch.object(tracking, "list_entries", return_value=_discover(entries)), \
             mock.patch.object(server, "_launch_run", return_value=({"job_id": "j"}, 200)) as launch:
            self.client.post(f"/api/subscriptions/{sub['id']}/update", json={"entry_ids": ["e2"]})
            self.assertEqual([e["id"] for e in launch.call_args[0][0]["entries"]], ["e2"])
            r = self.client.post(f"/api/subscriptions/{sub['id']}/update", json={"entry_ids": ["nope"]})
            self.assertEqual(r.status_code, 400)

    def test_信息跟进的订阅不能走更新_podcast_订阅也进不了简报批次(self):
        track = self._add(kind="track").get_json()
        r = self.client.post(f"/api/subscriptions/{track['id']}/update", json={})
        self.assertEqual(r.status_code, 400)
        pod = self._add(url="https://pod.example/2").get_json()
        r = self.client.post("/api/track/run", json={
            "selections": [{"sub_id": pod["id"], "entry_ids": ["e1"]}], "backend": "api", "api_key": "k"})
        self.assertEqual(r.status_code, 400)

    def test_改_podcast_文件夹时节目名按处理时的规则清洗(self):
        sub = self._add().get_json()
        r = self.client.patch(f"/api/subscriptions/{sub['id']}", json={"folder": os.path.join(self.root, "a/b:c?")})
        self.assertEqual(r.get_json()["folder"],
                         os.path.join(self.root, "a", pipeline.sanitize_filename("b:c?")))

    def test_找出以前处理过_还没订阅的节目(self):
        def make(name, manifest):
            os.makedirs(os.path.join(self.root, name))
            with open(os.path.join(self.root, name, ".manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
        make("节目A", {"entries": {"e1": {"ok": True}}, "content_type": "series", "source_url": "https://a.example/feed"})
        make("大会B", {"entries": {}, "content_type": "summit", "source_url": "https://b.example/list"})
        make("节目C", {"entries": {}, "content_type": "series", "source_url": "https://c.example/feed"})
        make("坏的", {"not": "a manifest"})
        with mock.patch.object(pipeline, "fetch_playlist",
                               return_value=_discover([{"id": "e1", "source_type": "rss"}], title="节目C")):
            self.client.post("/api/subscriptions", json={"url": "https://c.example/feed", "kind": "podcast",
                                                         "output_dir": self.root})
        found = self.client.post("/api/subscriptions/podcast_candidates", json={"output_dir": self.root}).get_json()
        self.assertEqual(found["candidates"], [{"name": "节目A", "url": "https://a.example/feed", "episodes": 1}])

    def test_名字截断在空格后_订阅文件夹和处理时的文件夹一致(self):
        name = "a" * 119 + " b"
        once = pipeline.sanitize_filename(name)
        self.assertEqual(pipeline.sanitize_filename(once), once)

    def test_更新_处理记录坏了报400_entry_ids_不是列表报400(self):
        sub = self._add().get_json()
        os.makedirs(sub["folder"])
        with open(os.path.join(sub["folder"], ".manifest.json"), "w", encoding="utf-8") as f:
            f.write("{坏的")
        with mock.patch.object(tracking, "list_entries", return_value=_discover([{"id": "e1"}])):
            r = self.client.post(f"/api/subscriptions/{sub['id']}/update", json={})
            self.assertEqual(r.status_code, 400)
            self.assertIn("处理记录", r.get_json()["error"])
            r = self.client.post(f"/api/subscriptions/{sub['id']}/update", json={"entry_ids": "e1"})
            self.assertEqual(r.status_code, 400)

    def test_更新按源里的顺序交给_process_job_标题沿用原来的节目名(self):
        sub = self._add().get_json()
        os.makedirs(sub["folder"])
        with open(os.path.join(sub["folder"], "示例节目.md"), "w", encoding="utf-8") as f:
            f.write("# 示例: 节目\n")
        entries = [{"id": "old", "publish_date": "20240101"}, {"id": "new", "publish_date": "20250101"}]
        with mock.patch.object(tracking, "list_entries", return_value=_discover(entries)), \
             mock.patch.object(server, "_launch_run", return_value=({"job_id": "j"}, 200)) as launch:
            r = self.client.post(f"/api/subscriptions/{sub['id']}/update", json={})
        payload = launch.call_args[0][0]
        self.assertEqual([e["id"] for e in payload["entries"]], ["old", "new"])
        self.assertEqual(payload["index_title"], "示例: 节目")
        self.assertEqual(r.get_json()["output_dir"], self.root)

    def test_节目文件夹不能是信息跟进的根_家目录或根目录(self):
        with mock.patch.object(pipeline, "fetch_playlist",
                               return_value=_discover([{"id": "e1"}], title="信息跟进")):
            r = self.client.post("/api/subscriptions", json={"url": "https://x/feed", "kind": "podcast",
                                                             "output_dir": self.root})
        self.assertEqual(r.status_code, 400)
        sub = self._add().get_json()
        for bad in ("~", "/", os.path.join(self.root, "信息跟进")):
            r = self.client.patch(f"/api/subscriptions/{sub['id']}", json={"folder": bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_检查全部_请求体不是对象也不报错(self):
        r = self.client.post("/api/subscriptions/check_all", json=[1, 2])
        self.assertEqual(r.status_code, 200)

    def test_名字会被批量导入改掉的节目单独列出(self):
        os.makedirs(os.path.join(self.root, "- 节目"))
        with open(os.path.join(self.root, "- 节目", ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {}, "content_type": "series", "source_url": "https://d.example/feed"}, f)
        found = self.client.post("/api/subscriptions/podcast_candidates", json={"output_dir": self.root}).get_json()
        self.assertEqual(found["candidates"], [])
        self.assertEqual([m["name"] for m in found["manual"]], ["- 节目"])

    def _seed_processed(self):
        sub = self._add().get_json()
        os.makedirs(sub["folder"])
        today = time.strftime("%Y%m%d")
        rows = {
            "recent": {"ok": True, "entry": {"title": "谈递归自我改进", "publish_date": today},
                       "summary": {"tldr": "RSI 离我们还远", "topics": ["AI安全"]},
                       "note_relative_path": "近.md"},
            "old": {"ok": True, "entry": {"title": "Nike 衰落", "publish_date": "20200101"},
                    "summary": {"tldr": "品牌", "topics": []}},
            "bad": {"ok": False, "entry": {"title": "失败的"}},
        }
        with open(os.path.join(sub["folder"], ".manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": rows}, f)
        return sub

    def test_筛选更新_按时间和关键词(self):
        sub = self._seed_processed()
        body = {"kind": "podcast", "days": 7, "query": "", "new_entries": [
            {"sub_id": sub["id"], "id": "n1", "title": "没日期的新单集"},
            {"sub_id": "别的订阅", "id": "x", "title": "不是这一种的"}]}
        items = self.client.post("/api/subscriptions/search_updates", json=body).get_json()["items"]
        self.assertEqual([i["id"] for i in items], ["n1", "recent"])
        self.assertEqual(items[1]["path"], os.path.join(sub["folder"], "近.md"))
        body.update(days=0, query="nike", use_model=False)
        items = self.client.post("/api/subscriptions/search_updates", json=body).get_json()["items"]
        self.assertEqual([i["id"] for i in items], ["old"])

    def test_筛选更新_交给模型按意思挑(self):
        sub = self._seed_processed()
        with mock.patch.object(pipeline, "summarize", return_value="2 | 讲 RSI\n2 | 重复\n9 | 越界") as m:
            r = self.client.post("/api/subscriptions/search_updates", json={
                "kind": "podcast", "days": 0, "query": "RSI 相关", "backend": "api", "api_key": "k"})
        prompt = m.call_args[0][0]
        self.assertIn("RSI 相关", prompt)
        self.assertIn("1｜示例节目｜", prompt)
        items = r.get_json()["items"]
        self.assertEqual([(i["id"], i["reason"]) for i in items], [("old", "讲 RSI")])


if __name__ == "__main__":
    unittest.main()
