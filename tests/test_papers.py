"""论文（Spark/论文/）：和剪藏同一种「剪藏式文档」，两端（core/clips.py 和手机 PathRules.swift）规则一致，
存、删、改名、稍后再读、图片、索引、修改记录都在论文自己的文件夹里，不碰剪藏的。"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cable_sync, clip_capture, clips, remote_tasks as rt, search_index  # noqa: E402

IOS = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark")
P = "Spark/论文"
C = "Spark/剪藏"
IMG = "0123456789abcdef"

# ---------------------------------------------------------------- 路径规则：一张表两端逐项比

PATHS = [
    f"{C}/20261101_a.md", f"{P}/20261101_a.md",
    f"{C}/索引.md", f"{P}/索引.md",
    f"{C}/.a.md", f"{P}/.a.md", f"{P}/.renames.json", f"{P}/.edits.json",
    f"{C}/sub/a.md", f"{P}/sub/a.md",
    f"{P}/摘录.md", f"{P}/x.txt", f"{P}/x.MD",
    f"{C}/attachments/{IMG}.png", f"{P}/attachments/{IMG}.jpg", f"{P}/attachments/{IMG}.webp",
    f"{P}/attachments/{IMG}.bmp", f"{P}/attachments/{IMG.upper()}.png", f"{P}/attachments/{IMG[:-1]}.png",
    f"{P}/attachments/sub/{IMG}.png", f"{P}/attachments/{IMG}.png.png",
    "Spark/论文集/x.md", "Spark/剪藏2/x.md", f"Spark/论文集/attachments/{IMG}.png", f"Spark/剪藏2/attachments/{IMG}.png",
    "spark/论文/x.md", "Spark/论文.md", "论文/x.md", "output/论文/x.md", "Spark/笔记/x.md", "output/复盘/x.md",
]

MAIN = r"""
import Foundation
let paths = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String]
let out: [[Any]] = paths.map { p in
    [PathRules.clipFolderOf(p) ?? "", PathRules.isClip(p), PathRules.isPaper(p), PathRules.isClipImageKey(p), ReaderPlace.doc(p).kind.label,
     PathRules.clipFolderOf(p).map(PathRules.kindName) ?? ""]
}
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
"""


def _py(p):
    folder = clips.clip_folder(p)
    return [folder, clips.is_clip_path(p), clips.is_paper_path(p), clips.image_key_ok(p, "https://example.com/a.png"),
            search_index.kind_of(p), clips.kind_name(folder) if folder else ""]


def test_路径规则两端一致(tmp_path):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    srcs = [os.path.join(IOS, n) for n in ("PathRules.swift", "ReaderFeatures.swift", "ReaderPlace.swift")]
    r = subprocess.run(["swiftc", "-o", str(exe), *srcs, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], input=json.dumps(PATHS), capture_output=True, text=True, timeout=30,
                                    check=True).stdout)
    diffs = [f"{p}：电脑 {_py(p)}，手机 {g}" for p, g in zip(PATHS, got) if _py(p) != g]
    assert len(got) == len(PATHS) and not diffs, "\n".join(diffs)


def test_路径规则的几个关键格():
    assert clips.clip_folder(f"{P}/a.md") == P and clips.clip_folder(f"{C}/a.md") == C
    assert clips.is_paper_path(f"{P}/a.md") and not clips.is_paper_path(f"{C}/a.md")
    for p in (f"{P}/索引.md", f"{P}/.a.md", f"{P}/sub/a.md", "Spark/论文集/x.md", "Spark/剪藏2/x.md"):
        assert not clips.is_clip_path(p), p
    assert search_index.kind_of(f"{P}/a.md") == "论文" and search_index.KINDS.index("论文") == search_index.KINDS.index("剪藏") + 1


def test_手机常量和电脑一致():
    rules = open(os.path.join(IOS, "PathRules.swift"), encoding="utf-8").read()
    assert re.search(r'static let paperFolder = "([^"]*)"', rules).group(1) == clips.PAPERS_DIR
    assert re.search(r'static let clipFolder = "([^"]*)"', rules).group(1) == clips.CLIPS_DIR


# ---------------------------------------------------------------- 手机交来的 save / delete（core/remote_tasks）

@pytest.fixture
def v(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Spark").mkdir(parents=True)
    monkeypatch.setenv("SPARK_VAULT", str(root))
    monkeypatch.setattr(rt, "trash_dir", lambda: str(tmp_path / "Trash"))
    monkeypatch.setattr(rt, "note_conflicts_dir", lambda: str(tmp_path / "conflicts"))
    return root


def _doc(title, clipped, tags, tldr="一句话"):
    return ("---\ntitle: " + json.dumps(title, ensure_ascii=False) + "\nsource: \"示例期刊\"\nclipped: " + clipped
            + "\ntags: " + json.dumps(tags, ensure_ascii=False) + "\ntldr: " + json.dumps(tldr, ensure_ascii=False)
            + f"\n---\n\n# {title}\n\n![](attachments/{IMG}.png)\n")


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _read(root, rel):
    return (root / rel).read_text(encoding="utf-8")


def _with_clip(root):
    """先存一篇剪藏，返回剪藏索引和修改记录的内容（之后核对动论文时它们一字不变）。"""
    rt.save_file(rt.parse_task({"type": "save", "path": f"{C}/20261101_剪藏.md", "content": _doc("剪藏", "2026-11-01", ["x"]),
                                "replace": True, "new": True, "key": "clipkey0001"}))
    return _read(root, f"{C}/索引.md"), _read(root, f"{C}/.edits.json")


def test_parse_论文的图片只收论文文件夹的():
    t = rt.parse_task({"type": "save", "path": f"{P}/20261101_a.md", "content": "x", "replace": True,
                       "images": {f"{P}/attachments/{IMG}.png": "https://example.com/a.png"}})
    assert t["images"] == {f"{P}/attachments/{IMG}.png": "https://example.com/a.png"}
    for bad in ({f"{C}/attachments/{IMG}.png": "https://example.com/a.png"},          # 剪藏的图不能挂在论文上
                {f"Spark/论文集/attachments/{IMG}.png": "https://example.com/a.png"}):
        with pytest.raises(rt.TaskError, match="Spark/论文/attachments/"):
            rt.parse_task({"type": "save", "path": f"{P}/20261101_a.md", "content": "x", "images": bad})
    with pytest.raises(rt.TaskError, match="images"):
        rt.parse_task({"type": "save", "path": f"{C}/20261101_a.md", "content": "x",
                       "images": {f"{P}/attachments/{IMG}.png": "https://example.com/a.png"}})
    for path in (f"{P}/索引.md", f"{P}/sub/a.md", f"{P}/.a.md", "Spark/论文集/a.md"):
        with pytest.raises(rt.TaskError, match="path"):
            rt.parse_task({"type": "save", "path": path, "content": "x"})
    assert rt.parse_task({"type": "delete", "path": f"{P}/20261101_a.md"})["path"] == f"{P}/20261101_a.md"


def test_新建带图片_各进各的文件夹(v, monkeypatch):
    from core import sources
    got = []
    monkeypatch.setattr(sources, "http_get", lambda url, timeout=20, max_bytes=0: got.append(url) or b"PNG")
    clip_index, clip_edits = _with_clip(v)
    got.clear()
    rel = f"{P}/20261102_论文一.md"
    content = _doc("论文一", "2026-11-02", ["测量"])
    out = rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": content, "replace": True, "new": True,
                                      "key": "paperkey001", "images": {f"{P}/attachments/{IMG}.png": "https://example.com/p.png"}}))
    assert "论文存进了笔记库" in out and "图片下了 1 张" in out
    assert _read(v, rel) == content and got == ["https://example.com/p.png"]
    assert (v / P / "attachments" / f"{IMG}.png").read_bytes() == b"PNG"
    assert not (v / C / "attachments" / f"{IMG}.png").exists()
    index = _read(v, f"{P}/索引.md")
    assert index.startswith("# 论文索引\n") and "共 1 篇" in index and "论文一" in index
    # 剪藏的索引、修改记录一字不变；论文的修改记录在论文文件夹里
    assert _read(v, f"{C}/索引.md") == clip_index and _read(v, f"{C}/.edits.json") == clip_edits
    edits = json.loads(_read(v, f"{P}/.edits.json"))
    assert list(edits) == [rel] and rel not in json.loads(clip_edits)
    assert os.path.isdir(v / P / ".spark-locks")
    # 同一个任务再到一次：已经有了
    again = rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": content, "replace": True, "new": True,
                                        "key": "paperkey001"}))
    assert "已经有" in again or "存进了" in again
    assert _read(v, rel) == content


def test_replace_带base_电脑上改过就不覆盖(v):
    rel = f"{P}/20261103_论文二.md"
    first = _doc("论文二", "2026-11-03", ["a"])
    rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": first, "replace": True, "new": True, "key": "paperkey002"}))
    edited = first.replace("# 论文二", "# 论文二\n\n==划线==")
    rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": edited, "replace": True, "base": _sha(first)}))
    assert _read(v, rel) == edited
    assert _sha(edited) in clips.known_versions(str(v), rel)                     # 记在论文的 .edits.json
    assert rel not in clips.load_edits(str(v), C)
    (v / rel).write_text("Mac 上改的\n", encoding="utf-8")
    with pytest.raises(rt.TaskError, match="没覆盖"):
        rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": "手机版\n", "replace": True, "base": _sha(edited)}))
    assert _read(v, rel) == "Mac 上改的\n"


def test_稍后再读(v):
    rel = f"{P}/20261104_论文三.md"
    t = rt.parse_task({"type": "save", "path": rel, "content": _doc("论文三", "2026-11-04", ["a"]), "replace": True,
                       "new": True, "key": "paperkey003", "later": True})
    assert t["later"] is True
    rt.save_file(t)
    text = _read(v, rel)
    assert re.search(r"^later: ", text, re.M)
    assert clips.load_clips(str(v), P)[0]["later"] is True and clips.load_clips(str(v), C) == []


def test_删除_墓碑在论文文件夹_剪藏索引不动(v, tmp_path):
    clip_index, _ = _with_clip(v)
    keep, gone = f"{P}/20261105_留着.md", f"{P}/20261106_删掉.md"
    gone_text = _doc("删掉", "2026-11-06", ["a"])
    for rel, text in ((keep, _doc("留着", "2026-11-05", ["a"])), (gone, gone_text)):
        rt.save_file(rt.parse_task({"type": "save", "path": rel, "content": text, "replace": True, "new": True,
                                    "key": "k" + _sha(rel)[:10]}))
    (v / P / "attachments").mkdir()
    (v / P / "attachments" / f"{IMG}.png").write_bytes(b"PNG")
    out = rt.delete_file(rt.parse_task({"type": "delete", "path": gone, "key": "deletekey01"}))
    assert "废纸篓" in out and not (v / gone).exists() and (tmp_path / "Trash" / "20261106_删掉.md").exists()
    index = _read(v, f"{P}/索引.md")
    assert "留着" in index and "删掉" not in index
    assert _read(v, f"{C}/索引.md") == clip_index
    assert _sha(gone_text) in clips.deleted_shas(str(v), gone)
    assert gone not in clips.load_edits(str(v), C)
    # 晚到的旧存（删之前交的那份）不再建回来
    late = rt.save_file(rt.parse_task({"type": "save", "path": gone, "content": gone_text, "replace": True}))
    assert "已经删了" in late and not (v / gone).exists()
    # 墓碑里的图片两个文件夹都算（清理工具不动），数据线同步把论文的墓碑也带给手机
    assert f"{P}/attachments/{IMG}.png" in clips.held_images(str(v))
    assert gone in cable_sync.deleted_versions(str(v), {})


def test_改名记录在论文文件夹(v):
    old, new = f"{P}/20261101_旧名.md", f"{P}/20261020_新名.md"
    text = _doc("论文", "2026-11-01", ["a"])
    (v / P).mkdir(parents=True)
    (v / new).write_text(text, encoding="utf-8")
    (v / P / ".renames.json").write_text(json.dumps({"renames": {old: new}}), encoding="utf-8")
    # 剪藏文件夹的改名记录里写了论文的路径：不认（各在各的文件夹）
    (v / C).mkdir(parents=True)
    (v / C / ".renames.json").write_text(json.dumps({"renames": {f"{P}/20261101_别的.md": new}}), encoding="utf-8")
    assert clips.renamed(str(v), old) == new
    assert clips.renamed(str(v), f"{P}/20261101_别的.md") == f"{P}/20261101_别的.md"
    edited = text.replace("# 论文", "# 论文\n\n==改==")
    rt.save_file(rt.parse_task({"type": "save", "path": old, "content": edited, "replace": True}))
    assert _read(v, new) == edited and not (v / old).exists()
    rt.delete_file(rt.parse_task({"type": "delete", "path": old}))
    assert not (v / new).exists()


def test_修改记录坏了两个文件夹都查(v):
    (v / P).mkdir(parents=True)
    assert clips.edits_problem(str(v)) == ""
    (v / P / ".edits.json").write_text("[1]", encoding="utf-8")
    assert "坏了" in clips.edits_problem(str(v)) and "论文" in clips.edits_problem(str(v))
    (v / P / ".renames.json.bad-20261101-000000").write_text("{", encoding="utf-8")
    assert any(p.startswith("论文的迁移记录") for p in clips.journal_problems(str(v)))


def test_改剪藏不动论文索引(v):
    rt.save_file(rt.parse_task({"type": "save", "path": f"{P}/20261101_p.md", "content": _doc("p", "2026-11-01", ["a"]),
                                "replace": True, "new": True, "key": "paperkey009"}))
    paper_index = _read(v, f"{P}/索引.md")
    _with_clip(v)
    rt.delete_file(rt.parse_task({"type": "delete", "path": f"{C}/20261101_剪藏.md"}))
    assert _read(v, f"{P}/索引.md") == paper_index


# ---------------------------------------------------------------- 电脑这边存（core/clip_capture 的 folder 参数）

@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_VAULT", str(tmp_path))
    monkeypatch.setattr(clip_capture, "_JOBS", {})
    monkeypatch.setattr(clip_capture.github_sync, "after_task", lambda: None)
    monkeypatch.setattr(clips, "download_images", lambda images, root: (len(images), 0))
    monkeypatch.setattr(clip_capture, "_ask", lambda: None)
    return tmp_path


def _wait(job):
    import time
    for _ in range(300):
        s = clip_capture.status(job["id"])
        if s["done"]:
            return s
        time.sleep(.01)
    pytest.fail("没存完")


def _payload():
    return {"article": {"url": "https://example.com/p", "title": "一篇论文", "source": "示例", "published": "",
                        "markdown": f"正文\n\n![](attachments/{IMG}.jpg)", "images": {f"attachments/{IMG}.jpg": "https://example.com/i.jpg"}},
            "note": ""}


def test_clip_capture_存到论文文件夹(capture):
    seen = []
    clips.download_images = lambda images, root: seen.append(dict(images)) or (len(images), 0)   # fixture 里 monkeypatch 过，用完会还原
    done = _wait(clip_capture.start(_payload(), folder=clips.PAPERS_DIR))
    assert not done["error"] and done["path"].startswith(f"{P}/") and done["path"].endswith("_一篇论文.md")
    assert seen == [{f"{P}/attachments/{IMG}.jpg": "https://example.com/i.jpg"}]
    assert (capture / P / "索引.md").read_text(encoding="utf-8").startswith("# 论文索引\n")
    assert not (capture / C).exists()
    # 默认还是剪藏
    seen.clear()
    done = _wait(clip_capture.start(_payload()))
    assert done["path"].startswith(f"{C}/") and seen == [{f"{C}/attachments/{IMG}.jpg": "https://example.com/i.jpg"}]
    assert "一篇论文" not in (capture / P / "索引.md").read_text(encoding="utf-8").split("共")[0]
    assert (capture / C / "索引.md").read_text(encoding="utf-8").startswith("# 剪藏索引\n")
    with pytest.raises(rt.TaskError):
        clip_capture.start(_payload(), folder="Spark/别的")
    assert clip_capture._unique_path(str(capture), "x.md") == f"{C}/x.md"
    assert clip_capture._unique_path(str(capture), "x.md", clips.PAPERS_DIR) == f"{P}/x.md"


# ---------------------------------------------------------------- 阅读页、Mac App 的列表（apps/reader/server.py）

def test_reader_论文有自己的列表_不当节目(capture):
    from tests.test_reader import _client
    for folder, name in ((C, "20261101_c.md"), (P, "20261101_p.md")):
        (capture / folder).mkdir(parents=True, exist_ok=True)
        (capture / folder / name).write_text(_doc(name[:-3], "2026-11-01", ["t"]), encoding="utf-8")
    c = _client()
    papers = c.get("/read/api/items?place=papers").json["items"]
    clip_items = c.get("/read/api/items?place=clips").json["items"]
    assert [i["id"] for i in papers] == ["论文/20261101_p.md"] and [i["id"] for i in clip_items] == ["剪藏/20261101_c.md"]
    assert all(s["name"] != "论文" for s in c.get("/read/api/library").json["shows"])
    page = c.get("/read/f/%E8%AE%BA%E6%96%87/").get_data(as_text=True)
    assert "<h1>论文</h1>" in page and "20261101_p" in page and "20261101_c" not in page


def test_review_fixes_for_paper_folder(tmp_path):
    """审查指出的几处：图片键不收结尾带换行的；改名记录跨文件夹的一对不挪。"""
    from core import clips
    key = "Spark/论文/attachments/0123456789abcdef.png"
    assert clips.image_key_ok(key, "https://example.com/a.png")
    assert not clips.image_key_ok(key + "\n", "https://example.com/a.png")
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "Spark", "剪藏"))
    os.makedirs(os.path.join(root, "Spark", "论文"))
    clips.update_edits(root, lambda e: e.update({"Spark/剪藏/a.md": [{"x": 1}], "Spark/剪藏/b.md": [{"x": 2}]}))
    clips.move_edits(root, [("Spark/剪藏/a.md", "Spark/论文/a.md"), ("Spark/剪藏/b.md", "Spark/剪藏/c.md")])
    got = clips.load_edits(root)
    assert "Spark/剪藏/a.md" in got and "Spark/论文/a.md" not in got      # 跨文件夹的没挪
    assert "Spark/剪藏/c.md" in got and "Spark/剪藏/b.md" not in got      # 同文件夹的照常挪
