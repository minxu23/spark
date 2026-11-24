"""手机阅读页的间距（apps/mobile-ios/Spark/ReaderSpacing.swift）：用 swiftc 单独编出来核对数值和层级。

单位 L = 正文一行的高度 = 正文字体行高 + 行距（ReadPrefs.lineStep）。App 里字体行高从 UIFont 取，
这里用自带霞鹜文楷的行高（字号 × 1.184，ascent 0.928 + descent 0.256，leading 0）代入；ReaderSpacing 本身只吃 L，
跟字体无关。

目标：篇名上方 1.25 L；篇名行距 = 篇名字号 × 1.3；篇名到来源行 0.5 L（不小于 12）；来源行到正文 1.25 L；
章节标题上 1.4 L / 下 0.6 L，小标题上 1.1 L / 下 0.5 L（都含段间距）；第一块是标题时上方不另加。
层级：篇名上方是篇名区最大的空隙；组内（篇名到来源行）明显小于组外（来源行到正文）；小标题上方大于下方两倍。"""

import itertools
import os
import shutil
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "ReaderSpacing.swift")

WENKAI_LINE = 1.184                       # 霞鹜文楷行高 / 字号
SIZES = [15.0, 17.0, 20.0, 23.0]          # ReadPrefs.sizes 15...23
LINE_GAPS = [0.5, 0.7, 0.9, 1.1]          # ReadPrefs.LineGap.factor：紧凑、标准、舒展、宽松
PARA_GAPS = [0.9, 1.3, 1.7]               # ReadPrefs.ParaGap.factor
TITLE_SCALES = [1.15, 1.3, 1.5]           # ReadPrefs.HeadingSize.scales.title

COMBOS = list(itertools.product(SIZES, LINE_GAPS, PARA_GAPS, TITLE_SCALES))


def line_of(size, gap):
    return size * WENKAI_LINE + size * gap


def probe_main():
    rows = []
    for i, (size, gap, para, ts) in enumerate(COMBOS):
        rows.append(f"run({i}, {line_of(size, gap)!r}, {size * para!r}, {size * ts!r})")
    return r'''
import Foundation
func f(_ x: Double) -> String { String(format: "%.4f", x) }
func run(_ i: Int, _ line: Double, _ para: Double, _ titleSize: Double) {
    let t = ReaderSpacing.title(line: line, titleSize: titleSize)
    let h2 = ReaderSpacing.heading(level: 2, line: line, para: para, first: false)
    let h3 = ReaderSpacing.heading(level: 3, line: line, para: para, first: false)
    let h4 = ReaderSpacing.heading(level: 4, line: line, para: para, first: false)
    let first = ReaderSpacing.heading(level: 2, line: line, para: para, first: true)
    print(i, f(t.top), f(t.line), f(t.toMeta), f(t.toBody), f(t.small), f(t.box),
          f(h2.top), f(h2.bottom), f(h3.top), f(h3.bottom), f(h4.top), f(h4.bottom), f(first.top), f(first.bottom))
}
''' + "\n".join(rows) + "\n"


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("reader_spacing")
    main = d / "main.swift"
    main.write_text(probe_main(), encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.split("\n")
    keys = ["top", "line", "toMeta", "toBody", "small", "box", "h2top", "h2bottom", "h3top", "h3bottom",
            "h4top", "h4bottom", "firsttop", "firstbottom"]
    got = {}
    for ln in out:
        if not ln.strip():
            continue
        parts = ln.split()
        got[COMBOS[int(parts[0])]] = dict(zip(keys, map(float, parts[1:])))
    assert len(got) == len(COMBOS)
    return got


# 三组必须核对的设置：最小字号 + 最紧行距、默认、最大字号 + 最松行距（段间距、标题大小取对应的一头）
NAMED = {
    "最小字号+最紧行距": (15.0, 0.5, 0.9, 1.15),
    "默认": (17.0, 0.7, 1.3, 1.3),
    "最大字号+最松行距": (23.0, 1.1, 1.7, 1.5),
}


@pytest.mark.parametrize("name", list(NAMED))
def test_目标数值(results, name):
    size, gap, para_k, ts = NAMED[name]
    r = results[NAMED[name]]
    L = line_of(size, gap)
    para = size * para_k
    near = lambda a, b: abs(a - b) < 0.01
    assert near(r["top"], 1.25 * L), "内容区顶部到篇名 1.25 L"
    assert 1.28 <= r["line"] / (size * ts) <= 1.35 and near(r["line"], size * ts * 1.3), "篇名行距 = 篇名字号 × 1.3"
    assert near(r["toMeta"], max(12, 0.5 * L)), "篇名到来源行 0.5 L，不小于 12"
    assert near(r["toBody"], 1.25 * L), "来源行到正文 / 没有来源行时篇名到正文 1.25 L"
    # 小标题：段间距 + 加减 = 到相邻块的总间距
    assert near(para + r["h2top"], 1.4 * L) and near(para + r["h2bottom"], 0.6 * L), "章节标题上 1.4 L / 下 0.6 L"
    assert near(para + r["h3top"], 1.1 * L) and near(para + r["h3bottom"], 0.5 * L), "小标题上 1.1 L / 下 0.5 L"
    assert near(para + r["h4top"], 1.1 * L) and near(para + r["h4bottom"], 0.5 * L), "四级标题同三级"
    assert r["firsttop"] == 0, "文章第一块就是标题时上方不另加"


def test_默认设置的实际数值(results):
    r = results[NAMED["默认"]]
    # 默认 L = 17 × 1.184 + 11.9 = 32.03
    assert round(r["top"], 1) == 40.0 and round(r["toMeta"], 1) == 16.0 and round(r["toBody"], 1) == 40.0
    assert round(r["line"], 2) == 28.73
    assert round(r["box"], 1) == 24.0 and r["small"] == r["toMeta"]


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "size%g-line%g-para%g-title%g" % c)
def test_层级规则(results, combo):
    size, gap, para_k, ts = combo
    r = results[combo]
    para = size * para_k
    # 1. 篇名上方是篇名区里最大的空隙（来源行到正文同为 1.25 L，可以相等）
    assert r["top"] >= max(r["toMeta"], r["toBody"], r["small"], r["box"])
    assert r["top"] > r["toMeta"] and r["top"] > r["box"]
    # 2. 组内空隙（篇名到来源行）明显小于组外空隙（来源行到正文）：至少差一倍
    assert r["toBody"] >= 2 * r["toMeta"]
    # 3. 小标题上方大于下方的两倍（总间距，含段间距）
    for lv in ("h2", "h3", "h4"):
        above, below = para + r[lv + "top"], para + r[lv + "bottom"]
        assert below > 0 and above > 2 * below, (lv, above, below)
    # 章节标题上方比小标题上方宽
    assert r["h2top"] > r["h3top"]
