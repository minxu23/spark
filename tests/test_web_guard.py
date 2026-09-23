"""本机服务只接受本机页面的请求：Host 必须是本机地址，跨站的改动请求一律拒绝。"""

import os
import tempfile
import unittest
from unittest import mock

import spark
from apps.notes2insight import server as notes_server
from apps.notes2insight import vault
from apps.summit2md import pipeline
from apps.summit2md import server as summit_server
from core import keys


class WebGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = summit_server.app.test_client()

    def test_本机请求照常通过(self):
        for host in ("127.0.0.1:8760", "localhost:8760", "[::1]:8760", "localhost"):
            r = self.client.get("/api/env", headers={"Host": host})
            self.assertEqual(r.status_code, 200, host)

    def test_dns重绑定_host不是本机就拒绝(self):
        r = self.client.get("/api/env", headers={"Host": "evil.example:8760"})
        self.assertEqual(r.status_code, 403)

    def test_看起来像本机的域名也拒绝(self):
        # 重绑定常用的写法：带尾点的 localhost、解析到 127.0.0.1 的公网域名、
        # 把 127.0.0.1 当子域名前缀、IPv6 映射地址
        for host in ("localhost.:8760", "127.0.0.1.nip.io:8760", "localtest.me",
                     "127.0.0.1.evil.example", "[::ffff:127.0.0.1]:8760", "[::1].evil.example"):
            r = self.client.get("/api/env", headers={"Host": host})
            self.assertEqual(r.status_code, 403, host)

    def test_ipv6本机地址可以带或不带端口(self):
        for host in ("[::1]", "[::1]:8760"):
            self.assertEqual(self.client.get("/api/env", headers={"Host": host}).status_code, 200, host)

    def test_ipv6同源post放行(self):
        r = self.client.post("/api/dir_plausible", json={"path": "/tmp"},
                             headers={"Host": "[::1]:8760", "Origin": "http://[::1]:8760"})
        self.assertEqual(r.status_code, 200)

    def test_跨站post拒绝_同源post放行_不带origin放行(self):
        r = self.client.post("/api/dir_plausible", json={"path": "/tmp"},
                             headers={"Host": "127.0.0.1:8760", "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/dir_plausible", json={"path": "/tmp"},
                             headers={"Host": "127.0.0.1:8760", "Origin": "http://127.0.0.1:8760"})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/dir_plausible", json={"path": "/tmp"},
                             headers={"Host": "127.0.0.1:8760"})
        self.assertEqual(r.status_code, 200)

    def test_origin为null也拒绝(self):
        r = self.client.post("/api/dir_plausible", json={"path": "/tmp"},
                             headers={"Host": "127.0.0.1:8760", "Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_落地页和notes同样受保护(self):
        for app in (spark.hub, notes_server.app):
            r = app.test_client().get("/", headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 403)


class LocalKeyNotSentToCustomBaseTests(unittest.TestCase):
    def test_summit2md_本地key不发给自定义地址(self):
        with summit_server.app.test_request_context(), \
             mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
             mock.patch.object(keys, "read_key_file", return_value="sk-or-local"):
            cfg, err = summit_server._resolve_llm_config(
                {"backend": "openrouter", "model": "m", "api_base": "https://evil.example/v1"})
            self.assertIsNone(cfg)
            self.assertEqual(err[1], 400)
            cfg, err = summit_server._resolve_llm_config({"backend": "openrouter", "model": "m"})
            self.assertEqual(cfg["api_key"], "sk-or-local")
            cfg, err = summit_server._resolve_llm_config(
                {"backend": "openrouter", "model": "m", "api_key": "typed", "api_base": "https://proxy/v1"})
            self.assertEqual(cfg["api_base"], "https://proxy/v1")

    def test_notes2insight_本地key不发给自定义地址(self):
        with notes_server.app.test_request_context(), \
             mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-env"}):
            params, err = notes_server._resolve_llm(
                {"backend": "openrouter", "model": "m", "api_base": "https://evil.example/v1"})
            self.assertIsNone(params)
            params, err = notes_server._resolve_llm({"backend": "openrouter", "model": "m"})
            self.assertEqual(params["api_key"], "sk-or-env")


class ReadNoteTests(unittest.TestCase):
    def test_只读笔记格式_不能借root读key文件(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "anthropic.key"), "w") as f:
                f.write("sk-ant-secret")
            with open(os.path.join(root, "note.md"), "w") as f:
                f.write("笔记")
            self.assertEqual(vault.read_note(root, "note.md"), "笔记")
            with self.assertRaises(ValueError):
                vault.read_note(root, "anthropic.key")
            with self.assertRaises(ValueError):
                vault.read_note(root, "../x.md")

    def test_preview接口读key文件被拒(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "anthropic.key"), "w") as f:
                f.write("sk-ant-secret")
            r = notes_server.app.test_client().get(
                "/api/preview", query_string={"root": root, "path": "anthropic.key"})
            self.assertEqual(r.status_code, 400)
            self.assertNotIn("sk-ant-secret", r.get_data(as_text=True))


class PathInputTests(unittest.TestCase):
    def test_条目id含路径分隔时拒绝(self):
        r = summit_server.app.test_client().post("/api/run", json={
            "entries": [{"id": "../../evil", "title": "x"}], "do_summary": False,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("id", r.get_json()["error"])

    def test_点号标题不会变成上级目录(self):
        self.assertEqual(pipeline.sanitize_filename(".."), "untitled")
        self.assertEqual(pipeline.sanitize_filename(".hidden"), "hidden")
        self.assertEqual(pipeline.sanitize_filename("正常 标题"), "正常 标题")


if __name__ == "__main__":
    unittest.main()
