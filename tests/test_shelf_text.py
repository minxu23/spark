"""手机书架上要算的几处字（apps/mobile-ios/Spark/ShelfText.swift）：用 swiftc 单独编出来跑。
按月分组、读后感一行上的小字和摘要、简报卡片「N 个订阅有更新」、卡片摘要去掉引用编号、搜索匹配。"""

import os
import shutil
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark", "ShelfText.swift")

MAIN = r'''
import Foundation
var cal = Calendar(identifier: .gregorian)
cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
func d(_ s: String) -> Date { ISO8601DateFormatter().date(from: s)! }
// 新的在前排好的一串；11 月 1 日 00:30（上海）还算 11 月，UTC 是 10 月 31 日
let dates = ["2026-11-23T10:00:00+08:00", "2026-11-01T00:30:00+08:00", "2026-10-31T23:00:00+08:00",
             "2026-01-05T09:00:00+08:00", "2025-12-31T09:00:00+08:00"]
for g in ShelfText.monthGroups(dates, calendar: cal, date: { d($0) }) { print(g.key, g.label, g.items.count) }
print(ShelfText.monthGroups([String](), calendar: cal, date: { d($0) }).count)
// 排法跟分组的日期不一致、同一个月不连着：并进前面那组，组的顺序不变
let mixed = ["2026-11-02T10:00:00+08:00", "2026-10-02T10:00:00+08:00", "2026-11-20T10:00:00+08:00"]
for g in ShelfText.monthGroups(mixed, calendar: cal, date: { d($0) }) { print(g.label) }
print(ShelfText.reflectionMeta(source: "晚点", marks: 3, notes: 2))
print(ShelfText.reflectionMeta(source: "", marks: 0, notes: 1))
print(ShelfText.reflectionMeta(source: "节目・整理稿", marks: 1, notes: 0))
print("[" + ShelfText.reflectionMeta(source: "", marks: 0, notes: 0) + "]")
print(ShelfText.reflectionTeaser("第一句。\n\n  第二段  \n第三行"))
print(ShelfText.briefUpdates(briefDate: "20261123", subscriptionLatest: ["20261123", "20261122", "20261123", ""]))
print(ShelfText.briefUpdates(briefDate: "", subscriptionLatest: ["", ""]))
print(ShelfText.briefUpdates(briefDate: "2026112", subscriptionLatest: ["2026112"]))
print(ShelfText.briefUpdates(briefDate: "20261123", subscriptionLatest: ["20261120"]))
// 摘要去掉引用编号
print(ShelfText.stripCitations("仍依赖人类 [1]。 开源模型[12]发布[^1]，见[^23] 结尾"))
print(ShelfText.stripCitations("数组 a[i] 和 [链接] 不动，[x1] 也不动"))
print(ShelfText.stripCitations("[3] 开头"))
print(ShelfText.stripCitations("苹果在 [2024] 年报中提到"))
print(ShelfText.stripCitations("取值 a[0] 到 a[9]"))
print(ShelfText.stripCitations("见 [1](https://x)"))
print(ShelfText.matches("ARIA", ["英国的AI芯片初创与aria"]), ShelfText.matches("芯片", ["标题", "正文里写着芯片"]),
      ShelfText.matches("  ", ["任何"]), ShelfText.matches("没有", ["标题", ""]), ShelfText.matches(" aria ", ["Aria"]))
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_shelf_text_swift(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "TZ": "UTC"}).stdout.split("\n")
    assert out == [
        "2026-11 2026年11月 · 2 2", "2026-10 2026年10月 · 1 1", "2026-01 2026年1月 · 1 1", "2025-12 2025年12月 · 1 1",
        "0",
        "2026年11月 · 2", "2026年10月 · 1",
        "晚点，3 处划线，2 条批注", "1 条批注", "节目・整理稿，1 处划线", "[]",
        "第一句。 第二段 第三行",
        "2", "0", "0", "0",
        "仍依赖人类。 开源模型发布，见 结尾",
        "数组 a[i] 和 [链接] 不动，[x1] 也不动",
        "开头",
        "苹果在 [2024] 年报中提到",
        "取值 a[0] 到 a[9]",
        "见 [1](https://x)",
        "true true false false true",
        "",
    ]
