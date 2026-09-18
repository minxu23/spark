import os
import tempfile
import unittest

from apps.notes2insight import server


class BrowseDirRouteTests(unittest.TestCase):
    """/api/browse_dir：给"笔记库路径""输出目录"这类路径输入框做自动补全。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_列出匹配前缀的子目录(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "minxu"))
            os.makedirs(os.path.join(root, "other"))
            r = self.client.post("/api/browse_dir", json={"path": os.path.join(root, "min")})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["entries"], [os.path.join(root, "minxu")])

    def test_路径不存在时返回空列表而不是报错(self):
        r = self.client.post("/api/browse_dir", json={"path": "/这个路径/不存在/xyz"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"entries": []})

    def test_缺path字段时不崩溃(self):
        r = self.client.post("/api/browse_dir", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("entries", r.get_json())


class DirPlausibleRouteTests(unittest.TestCase):
    """/api/dir_plausible：给"记住最近用过的目录"做把关，别把打错的路径也记下来。"""

    def setUp(self):
        self.client = server.app.test_client()

    def test_真实存在的目录返回True(self):
        with tempfile.TemporaryDirectory() as root:
            r = self.client.post("/api/dir_plausible", json={"path": root})
            self.assertEqual(r.get_json(), {"plausible": True})

    def test_上级目录都不存在时返回False(self):
        r = self.client.post("/api/dir_plausible", json={"path": "/这个路径/不存在/xyz"})
        self.assertEqual(r.get_json(), {"plausible": False})


if __name__ == "__main__":
    unittest.main()
