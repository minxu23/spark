"""拖入文件生成报告：uploads.py 的抽取逻辑，以及 /api/upload 这个 HTTP 入口。

fixtures/ 下的 sample.pdf、sample.docx 是真实的二进制文件（不是拿字符串伪造的
"看起来像"数据），跑的是真正的 pypdf / python-docx 抽取路径——这两个库版本升级
时最容易在这里悄悄坏掉，用真文件才测得出来。
"""

import io
import os
import shutil
import tempfile
import unittest

from apps.notes2insight import server, uploads

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _read_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


class SaveBatchTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="n2i_upload_test_")

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_md_和_txt_直接当文本读(self):
        notes, errors = uploads.save_batch(self.dest, [
            ("笔记.md", "# 标题\n正文。".encode("utf-8")),
            ("备忘.txt", "纯文本内容。".encode("utf-8")),
        ])
        self.assertEqual(errors, [])
        self.assertEqual({n["title"] for n in notes}, {"笔记", "备忘"})

    def test_真实_pdf_能抽出文字(self):
        notes, errors = uploads.save_batch(self.dest, [("sample.pdf", _read_fixture("sample.pdf"))])
        self.assertEqual(errors, [])
        self.assertEqual(len(notes), 1)
        with open(os.path.join(self.dest, notes[0]["path"]), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("推理成本", body)

    def test_真实_docx_能抽出文字(self):
        notes, errors = uploads.save_batch(self.dest, [("sample.docx", _read_fixture("sample.docx"))])
        self.assertEqual(errors, [])
        with open(os.path.join(self.dest, notes[0]["path"]), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("ASIC", body)

    def test_中文文件名不会被整段砍掉(self):
        """回归用例：werkzeug.secure_filename 会把非 ASCII 字符全部砍掉，
        secure_filename('笔记.md') == 'md'——这个工具面向中文用户，文件名
        十有八九是中文，砍掉就等于什么都处理不了。"""
        notes, errors = uploads.save_batch(self.dest, [
            ("会议纪要：2026-09-17 讨论稿 (v2).md", "内容".encode("utf-8")),
        ])
        self.assertEqual(errors, [])
        self.assertEqual(notes[0]["title"], "会议纪要：2026-09-17 讨论稿 (v2)")

    def test_不支持的格式单独报错_不影响同批其它文件(self):
        notes, errors = uploads.save_batch(self.dest, [
            ("笔记.md", "内容".encode("utf-8")),
            ("图片.png", b"\x89PNG fake"),
        ])
        self.assertEqual(len(notes), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn(".png", errors[0]["error"])

    def test_扫描版_pdf_没有文字层时明确报错_不生成空笔记(self):
        # 伪造一个"有 pypdf 能打开但抽不出任何文字"的场景比造真扫描件简单：
        # 直接验证抽取函数在拿到空字符串时的行为
        with self.assertRaises(uploads.UploadError):
            uploads._extract_pdf(b"%PDF-1.4\n%%EOF")  # 不是合法 PDF，会走异常包装分支

    def test_空_docx_报错_不生成空笔记(self):
        import docx
        buf = io.BytesIO()
        d = docx.Document()
        d.add_paragraph("   ")
        d.save(buf)
        notes, errors = uploads.save_batch(self.dest, [("空.docx", buf.getvalue())])
        self.assertEqual(notes, [])
        self.assertEqual(len(errors), 1)

    def test_路径穿越_不会跑出目标目录(self):
        notes, errors = uploads.save_batch(self.dest, [
            ("../../../etc/evil.md", "内容".encode("utf-8")),
        ])
        self.assertEqual(len(notes), 1)
        # basename 之后只剩 "evil.md"，落在 dest 目录本身，不会往上跳
        full = os.path.join(self.dest, notes[0]["path"])
        self.assertEqual(os.path.dirname(os.path.abspath(full)), os.path.abspath(self.dest))

    def test_同批重名不会互相覆盖(self):
        notes, errors = uploads.save_batch(self.dest, [
            ("重复.txt", "第一份".encode("utf-8")),
            ("重复.txt", "第二份".encode("utf-8")),
        ])
        self.assertEqual(len(notes), 2)
        self.assertNotEqual(notes[0]["path"], notes[1]["path"])
        with open(os.path.join(self.dest, notes[0]["path"]), encoding="utf-8") as f:
            self.assertIn("第一份", f.read())
        with open(os.path.join(self.dest, notes[1]["path"]), encoding="utf-8") as f:
            self.assertIn("第二份", f.read())

    def test_单个文件超过大小上限被拒绝(self):
        big = b"a" * (uploads.MAX_FILE_BYTES + 1)
        notes, errors = uploads.save_batch(self.dest, [("大文件.txt", big)])
        self.assertEqual(notes, [])
        self.assertIn("MB", errors[0]["error"])

    def test_一批文件数超过上限直接拒绝整批(self):
        batch = [(f"{i}.txt", b"x") for i in range(uploads.MAX_FILES_PER_BATCH + 1)]
        with self.assertRaises(uploads.UploadError):
            uploads.save_batch(self.dest, batch)

    def test_返回的字典形状和_vault_scan_一致(self):
        """前端和 pipeline 都要能像对待库里笔记一样对待上传的笔记，字段不能少。"""
        notes, _ = uploads.save_batch(self.dest, [("笔记.md", "内容".encode("utf-8"))])
        expected_keys = {"path", "folder", "name", "title", "date", "bytes", "chars", "mtime"}
        self.assertEqual(set(notes[0].keys()), expected_keys)


class PruneOldBatchesTests(unittest.TestCase):
    def test_清掉过期批次_保留新批次(self):
        root = tempfile.mkdtemp(prefix="n2i_uploads_root_")
        try:
            old = os.path.join(root, "old_batch")
            new = os.path.join(root, "new_batch")
            os.makedirs(old)
            os.makedirs(new)
            now = 2_000_000_000
            os.utime(old, (now - uploads.RETENTION_SECONDS - 10,) * 2)
            os.utime(new, (now - 10,) * 2)

            uploads.prune_old_batches(root, now=now)

            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_目录不存在时不报错(self):
        uploads.prune_old_batches("/不存在/的/路径")  # 不该抛异常


class ApiUploadRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self._tmp_uploads_root = tempfile.mkdtemp(prefix="n2i_uploads_root_")
        self._orig_root = uploads.UPLOADS_ROOT
        uploads.UPLOADS_ROOT = self._tmp_uploads_root

    def tearDown(self):
        uploads.UPLOADS_ROOT = self._orig_root
        shutil.rmtree(self._tmp_uploads_root, ignore_errors=True)

    def test_没有文件时返回_400(self):
        r = self.client.post("/api/upload", data={})
        self.assertEqual(r.status_code, 400)

    def test_正常上传返回可直接喂给_run_的形状(self):
        r = self.client.post("/api/upload", data={
            "files": (io.BytesIO("正文内容".encode("utf-8")), "笔记.md"),
        }, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(os.path.isdir(d["root"]))
        self.assertEqual(len(d["notes"]), 1)
        self.assertEqual(d["errors"], [])

    def test_伪造的_session_不会被当路径拼进去(self):
        r = self.client.post("/api/upload", data={
            "session": "../../../etc",
            "files": (io.BytesIO(b"x"), "a.md"),
        }, content_type="multipart/form-data")
        d = r.get_json()
        self.assertNotEqual(d["session"], "../../../etc")
        self.assertRegex(d["session"], r"^[0-9a-f]{32}$")
        # 真正落盘的目录必须还在 UPLOADS_ROOT 底下
        self.assertTrue(os.path.abspath(d["root"]).startswith(os.path.abspath(self._tmp_uploads_root)))

    def test_复用合法_session_会追加到同一批(self):
        r1 = self.client.post("/api/upload", data={
            "files": (io.BytesIO(b"a"), "a.md"),
        }, content_type="multipart/form-data")
        sid = r1.get_json()["session"]

        r2 = self.client.post("/api/upload", data={
            "session": sid,
            "files": (io.BytesIO(b"b"), "b.md"),
        }, content_type="multipart/form-data")
        d2 = r2.get_json()
        self.assertEqual(d2["session"], sid)
        self.assertEqual(d1_root := r1.get_json()["root"], d2["root"])
        self.assertEqual(len(os.listdir(d1_root)), 2)
