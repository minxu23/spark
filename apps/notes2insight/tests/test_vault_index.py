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
