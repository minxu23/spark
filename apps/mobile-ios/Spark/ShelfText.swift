import Foundation

/// 书架上几处要算的字：按月分组、读后感行上的小字、卡片上「近 7 天更新」和最近几条、搜索匹配。
/// 都是纯函数（只用 Foundation），tests/test_shelf_text.py 用 swiftc 单独编出来跑
enum ShelfText {
    struct MonthGroup<T> {
        let key: String      // yyyy-MM
        let label: String    // 2026年11月 · 8
        let items: [T]
    }

    /// 已经按时间排好（新的在前）的一串，按月份连着归组：组的顺序跟原来的顺序一致，不重新排
    static func monthGroups<T>(_ items: [T], calendar: Calendar = .current, date: (T) -> Date) -> [MonthGroup<T>] {
        var out: [(key: String, y: Int, m: Int, items: [T])] = []
        for it in items {
            let c = calendar.dateComponents([.year, .month], from: date(it))
            let y = c.year ?? 0, m = c.month ?? 0
            let key = String(format: "%04d-%02d", y, m)
            if out.last?.key == key { out[out.count - 1].items.append(it) } else { out.append((key, y, m, [it])) }
        }
        // 同一个月要是不连着（排法跟分组的日期不一样时），后面那段并进前面那组
        var merged: [(key: String, y: Int, m: Int, items: [T])] = []
        for g in out {
            if let i = merged.firstIndex(where: { $0.key == g.key }) { merged[i].items += g.items } else { merged.append(g) }
        }
        return merged.map { MonthGroup(key: $0.key, label: "\($0.y)年\($0.m)月 · \($0.items.count)", items: $0.items) }
    }

    /// 读后感一行上面那行小字的左边：出处，几处划线，几条批注（是 0 的不写）
    static func reflectionMeta(source: String, marks: Int, notes: Int) -> String {
        var parts: [String] = []
        if !source.isEmpty { parts.append(source) }
        if marks > 0 { parts.append("\(marks) 处划线") }
        if notes > 0 { parts.append("\(notes) 条批注") }
        return parts.joined(separator: "，")
    }

    /// 读后感一行的两行摘要：读后感的开头，换行换成空格
    static func reflectionTeaser(_ reflection: String) -> String {
        reflection.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// 「近 7 天更新」有几条：日期（yyyyMMdd）不早于今天往前数 7 天那天的都算（含正好 7 天前那天，
    /// 同 Jottings.summarize「含正好 7 天前」）；比今天晚的也算；没有日期的不算。今天按 calendar 的时区算
    static func recentCount(_ dates: [String], now: Date, calendar: Calendar = .current) -> Int {
        guard let start = calendar.date(byAdding: .day, value: -recentDays, to: now) else { return 0 }
        let c = calendar.dateComponents([.year, .month, .day], from: start)
        let cutoff = String(format: "%04d%02d%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
        return dates.filter { $0.count == 8 && $0 >= cutoff }.count
    }
    static let recentDays = 7

    /// 「近 7 天」那行副文：K 为 0 时写「近 7 天没有更新」
    static func recentLine(_ count: Int, unit: String) -> String {
        count > 0 ? "近 \(recentDays) 天更新 \(count) \(unit)" : "近 \(recentDays) 天没有更新"
    }

    /// 卡片上列的最近几条：按（日期、时刻）新的在前，取前 limit 条。同一天没有时刻（0）的排在有时刻的后面，
    /// 没有日期的排最后；日期和时刻都一样时才看 tie（标题等），小的在前；再一样的保持原来的先后。
    /// 一趟扫过去，只留前 limit 条（插进一个按顺序排好的小数组），不整串排序
    static func newest<T>(_ items: [T], limit: Int = 3, key: (T) -> (date: String, instant: Int64),
                          tie: (T) -> String) -> [T] {
        guard limit > 0 else { return [] }
        var top: [(item: T, date: String, instant: Int64)] = []
        top.reserveCapacity(limit + 1)
        // a 排在 b 前面：日期、时刻新的在前；都一样时 tie 小的在前
        func before(_ a: (item: T, date: String, instant: Int64), _ b: (item: T, date: String, instant: Int64)) -> Bool {
            if a.date != b.date { return a.date > b.date }
            if a.instant != b.instant { return a.instant > b.instant }
            return tie(a.item) < tie(b.item)
        }
        for item in items {
            let k = key(item)
            let c = (item: item, date: k.date, instant: k.instant)
            if top.count == limit, let last = top.last, !before(c, last) { continue }
            // 插在第一条排在它后面的前面（一样的排在已有的后面）
            let i = top.firstIndex { before(c, $0) } ?? top.count
            top.insert(c, at: i)
            if top.count > limit { top.removeLast() }
        }
        return top.map(\.item)
    }

    /// 卡片摘要里去掉引用编号（连同前面的空白），多出来的空白并成一个：
    /// - [^1] 这类脚注编号都去掉；
    /// - [1]、[12]、[123]：1 到 3 位数字才算；后面紧跟「(」的是链接（[1](https://…)），不动；
    ///   前面紧挨着 ASCII 字母或数字的是下标（a[0]），不动。[2024] 这类 4 位以上的数字不动
    static func stripCitations(_ s: String) -> String {
        let removed = s.replacingOccurrences(of: #"\s*\[\^\d+\]|\s*(?<![A-Za-z0-9])\[\d{1,3}\](?!\()"#, with: "",
                                             options: .regularExpression)
        return removed.replacingOccurrences(of: #"[ \t]{2,}"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    /// 搜索：几段文字里有一段包含要找的字（不分大小写）。要找的字是空白的不算匹配
    static func matches(_ query: String, _ fields: [String]) -> Bool {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return false }
        return fields.contains { $0.lowercased().contains(q) }
    }
}
