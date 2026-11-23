"""手机书架上要算的几处字（apps/mobile-ios/Spark/ShelfText.swift）：用 swiftc 单独编出来跑。
按月分组、读后感一行上的小字和摘要、卡片上「近 7 天更新」和最近三条的排法、卡片摘要去掉引用编号、搜索匹配。"""

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
// 近 7 天更新：今天 11 月 23 日（上海），正好 7 天前的 11 月 16 日算，15 日不算；比今天晚的算；没日期、日期不对的不算
let recent = ["20261123", "20261116", "20261115", "20261125", "", "2026111", "20261120"]
print(ShelfText.recentCount(recent, now: d("2026-11-23T10:00:00+08:00"), calendar: cal))
// 上海 23 日 00:30 是 UTC 22 日：按 calendar 的时区算今天，还是 16 日起算
print(ShelfText.recentCount(recent, now: d("2026-11-23T00:30:00+08:00"), calendar: cal))
print(ShelfText.recentCount([], now: d("2026-11-23T10:00:00+08:00"), calendar: cal))
print(ShelfText.recentLine(3, unit: "期"), ShelfText.recentLine(0, unit: "篇"))
// 最近三条：同一天按时刻排，没时刻（0）的排在同一天有时刻的后面，没日期的最后；时刻也一样按名字排
let items: [(String, String, Int64)] = [("A", "20261123", 100), ("B", "20261123", 0), ("C", "20261123", 200),
                                        ("D", "20261122", 999), ("E", "", 0), ("Y", "20261120", 0), ("X", "20261120", 0)]
let key = { (t: (String, String, Int64)) in (date: t.1, instant: t.2) }
let tie = { (t: (String, String, Int64)) in t.0 }
print(ShelfText.newest(items, key: key, tie: tie).map(\.0).joined())
print(ShelfText.newest(items, limit: 10, key: key, tie: tie).map(\.0).joined())
print(ShelfText.newest([items[4], items[3]], key: key, tie: tie).map(\.0).joined())
print("[" + ShelfText.newest([(String, String, Int64)](), key: key, tie: tie).map(\.0).joined() + "]")
// tie 只在日期和时刻都一样时才取
var tieCalls = 0
_ = ShelfText.newest([("P", "20261101", 5), ("Q", "20261102", 0), ("R", "20261103", 0)], limit: 2, key: key,
                     tie: { tieCalls += 1; return $0.0 })
print("tie", tieCalls)
// 跟原来整串排序的写法比：随机数据（同日同刻、没时刻、没日期、条数少于 limit），结果一样
func reference(_ xs: [(String, String, Int64)], _ limit: Int) -> [String] {
    xs.sorted { a, b in (a.1, a.2, b.0) > (b.1, b.2, a.0) }.prefix(limit).map(\.0)
}
var seed: UInt64 = 20261123
func rnd(_ n: Int) -> Int { seed = seed &* 6364136223846793005 &+ 1442695040888963407; return Int((seed >> 33) % UInt64(n)) }
var mismatches = 0, cases = 0
for _ in 0..<3000 {
    let n = rnd(12)
    var xs: [(String, String, Int64)] = []
    for i in 0..<n {
        let date = rnd(6) == 0 ? "" : "2026112\(rnd(3))"           // 没日期，或 3 天里的一天（常同日）
        let inst: Int64 = rnd(3) == 0 ? 0 : Int64(100 * rnd(3))     // 没时刻，或少数几个时刻（常同刻）
        xs.append(("t\(rnd(5))-\(i)", date, inst))                   // tie 各不相同（加了序号）
    }
    for limit in [1, 3, 5, 20] {
        cases += 1
        if ShelfText.newest(xs, limit: limit, key: key, tie: tie).map(\.0) != reference(xs, limit) { mismatches += 1 }
    }
}
print("random", cases, mismatches)
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
        "4", "4", "0",
        "近 7 天更新 3 期 近 7 天没有更新",
        "CAB", "CABDXYE", "DE", "[]",
        "tie 0",
        "random 12000 0",
        "仍依赖人类。 开源模型发布，见 结尾",
        "数组 a[i] 和 [链接] 不动，[x1] 也不动",
        "开头",
        "苹果在 [2024] 年报中提到",
        "取值 a[0] 到 a[9]",
        "见 [1](https://x)",
        "true true false false true",
        "",
    ]
