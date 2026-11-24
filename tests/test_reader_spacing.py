"""手机阅读页的间距（apps/mobile-ios/Spark/ReaderSpacing.swift）：用 swiftc 单独编出来核对数值。
篇名四周默认 24 / 12 / 28，随正文字号等比变；正文小标题上方明显比下方宽。"""

import os
import shutil
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "ReaderSpacing.swift")

MAIN = r'''
for size in [15.0, 17.0, 20.0, 23.0] {
    let t = ReaderSpacing.title(size: size)
    print("title", Int(size), Int(t.top), Int(t.toMeta), Int(t.toBody))
}
for level in [2, 3] {
    let h = ReaderSpacing.heading(level: level, size: 17, first: false)
    print("heading", level, String(format: "%.2f %.2f", h.top, h.bottom))
}
let f = ReaderSpacing.heading(level: 2, size: 17, first: true)
print("first", String(format: "%.2f %.2f", f.top, f.bottom))
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_阅读页间距(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.split("\n")
    assert "title 17 24 12 28" in out, "默认字号：上方 24，到来源行 12，来源行到正文 28"
    assert "title 15 21 11 25" in out and "title 23 32 16 38" in out, "随字号等比变"
    # 正文小标题：段间距（标准 1.3 倍字号 = 22.1）之外的加减
    para = 17 * 1.3
    line = 17 * 1.2 + 17 * 0.7
    got = {ln.split()[1]: tuple(map(float, ln.split()[2:])) for ln in out if ln.startswith("heading")}
    for level, (top, bottom) in got.items():
        above, below = para + top, para + bottom
        assert above > below * 1.8, (level, above, below)
        assert below > 0
    assert abs(para + got["3"][0] - line) < 0.5, "小标题上方约一行正文高"
    assert "first 0.00 -5.95" in out, "开头第一块是标题时上方不另加"
