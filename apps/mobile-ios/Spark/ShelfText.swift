import Foundation

/// 书架上几处要算的字：按月分组、读后感行上的小字、简报卡片上「N 个订阅有更新」、搜索匹配。
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

    /// 简报卡片右上角：订阅里最近更新那天（yyyyMMdd）跟简报同一天的有几个。简报日期不对、一个都没有是 0（卡片上不放）
    static func briefUpdates(briefDate: String, subscriptionLatest: [String]) -> Int {
        guard briefDate.count == 8, Int(briefDate) != nil else { return 0 }
        return subscriptionLatest.filter { $0 == briefDate }.count
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
