"""大会/节目总结文件不再千篇一律叫 README.md，而是跟输出目录同名。
覆盖新命名的写入、旧版 README.md 的兼容读取，以及写入新文件时清掉旧文件。
"""

import os
import tempfile
import unittest

from apps.summit2md import pipeline


class SummaryFilenameTests(unittest.TestCase):
    def test_新命名跟目录同名(self):
        out_dir = "/tmp/whatever/All-In Summit 2026"
        self.assertEqual(
            pipeline._summary_path(out_dir),
            "/tmp/whatever/All-In Summit 2026/All-In Summit 2026.md",
        )

    def test_没有任何总结文件时返回None(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "Some Show")
            os.makedirs(out_dir)
            self.assertIsNone(pipeline._existing_summary_path(out_dir))

    def test_优先认新命名(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "Some Show")
            os.makedirs(out_dir)
            new_path = os.path.join(out_dir, "Some Show.md")
            legacy_path = os.path.join(out_dir, "README.md")
            with open(new_path, "w", encoding="utf-8") as f:
                f.write("# new")
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write("# legacy")
            self.assertEqual(pipeline._existing_summary_path(out_dir), new_path)

    def test_没有新命名时退回旧版README(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "Old Show")
            os.makedirs(out_dir)
            legacy_path = os.path.join(out_dir, "README.md")
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write("# Old Show\n\n内容")
            self.assertEqual(pipeline._existing_summary_path(out_dir), legacy_path)
            # 标题回读也要认旧文件
            self.assertEqual(pipeline._read_summit_title_from_readme(out_dir), "Old Show")

    def test_写入新文件会清掉旧版README(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "Some Show")
            os.makedirs(out_dir)
            legacy_path = os.path.join(out_dir, "README.md")
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write("# 旧内容")

            written_path = pipeline._write_summary(out_dir, "# 新内容")

            self.assertEqual(written_path, os.path.join(out_dir, "Some Show.md"))
            self.assertFalse(os.path.exists(legacy_path))  # 旧文件被清掉，不留重复内容
            with open(written_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "# 新内容")

    def test_目录名本身就是旧README时写入不出错(self):
        # 极端情况：会议标题被 sanitize 之后恰好是 "README"，新旧文件名撞了——
        # 不能因为“同名”就在写完之后把刚写的文件自己删掉。
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "README")
            os.makedirs(out_dir)
            written_path = pipeline._write_summary(out_dir, "# 内容")
            self.assertTrue(os.path.exists(written_path))
            with open(written_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "# 内容")


if __name__ == "__main__":
    unittest.main()
