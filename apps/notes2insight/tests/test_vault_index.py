"""笔记库索引：上传/导入的临时目录清掉之后，索引里不留它们的记录。"""

import json
import os

from apps.notes2insight import vault


def test_扫描后索引只保留还存在的目录(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.md").write_text("# A\n", encoding="utf-8")
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "b.md").write_text("# B\n", encoding="utf-8")

    vault.scan(str(gone))
    gone_root = os.path.abspath(str(gone))
    (gone / "b.md").unlink()
    gone.rmdir()
    vault.scan(str(live))

    with open(vault.INDEX_PATH, encoding="utf-8") as f:
        index = json.load(f)
    assert os.path.abspath(str(live)) in index
    assert gone_root not in index


def test_同时扫描不同目录_索引互不覆盖(tmp_path):
    import threading
    roots = []
    for i in range(8):
        d = tmp_path / f"r{i}"
        d.mkdir()
        (d / "a.md").write_text(f"# {i}\n", encoding="utf-8")
        roots.append(os.path.abspath(str(d)))
    threads = [threading.Thread(target=vault.scan, args=(r,)) for r in roots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(vault.INDEX_PATH, encoding="utf-8") as f:
        index = json.load(f)
    assert set(roots) <= set(index)


def test_指到库外的软链不列出来(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "真笔记.md").write_text("# 真\n", encoding="utf-8")
    outside = tmp_path / "外面.md"
    outside.write_text("# 外\n", encoding="utf-8")
    os.symlink(str(outside), str(root / "软链.md"))
    names = {n["name"] for n in vault.scan(str(root), use_cache=False)}
    assert names == {"真笔记.md"}


def test_字数按开头采样的字符字节比估_英文不再低估三倍(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    en = "word " * 2000            # 1 万字符 ≈ 1 万字节
    zh = "汉字" * 5000              # 1 万字符 ≈ 3 万字节
    (root / "en.md").write_text("# EN\n" + en, encoding="utf-8")
    (root / "zh.md").write_text("# ZH\n" + zh, encoding="utf-8")
    notes = {n["name"]: n for n in vault.scan(str(root), use_cache=False)}
    assert abs(notes["en.md"]["chars"] - len(en)) < len(en) * 0.1
    assert abs(notes["zh.md"]["chars"] - len(zh)) < len(zh) * 0.1
    # 走缓存再扫一遍，比例也从索引里取，结果一样
    again = {n["name"]: n for n in vault.scan(str(root), use_cache=True)}
    assert again["en.md"]["chars"] == notes["en.md"]["chars"]
