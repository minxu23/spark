import Foundation

/// 会议是哪几天开的（会议时间）、视频是哪几天发布的。处理记录顶层的 event_dates = {start, end, source}，
/// 只存 manual（在电脑阅读页上手动填的）、agenda（官网议程给了日期）；老版本存的 sessions 当没存。
/// 显示：存着的 → 现推议程日期（session_date，source=agenda）→ 现推视频发布日期（publish_date，source=sessions，
/// 写成「视频发布：…」，不是会议时间；跨了 maxDerivedSpanDays 天以上不显示）。两种日期不混在一起算。
/// 现推都要日期够多：有日期的场数至少一半，而且至少 2 场（整个会议只有 1 场的除外）。
/// 跟电脑 core/summit_dates.py 一字不差（tests/test_summit_dates.py 对照）。
enum SummitDates {
    struct Range: Equatable {
        let start: String   // YYYY-MM-DD
        let end: String
        let source: String
        var label: String { SummitDates.label(start, end) }
    }

    static let sources = ["manual", "agenda", "sessions"]
    static let maxDerivedSpanDays = 10
    static let dash = "—"

    private static func leap(_ y: Int) -> Bool { y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) }

    private static func valid(_ y: Int, _ m: Int, _ d: Int) -> Bool {
        guard (1990...2100).contains(y), (1...12).contains(m) else { return false }
        let days = [31, leap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
        return (1...days).contains(d)
    }

    /// YYYY-MM-DD 或 YYYYMMDD → YYYY-MM-DD；不是真日期返回空串
    static func isoDay(_ value: Any?) -> String {
        guard let raw = value as? String else { return "" }
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let digits: String
        if s.count == 10, s.wholeMatch(of: #/[0-9]{4}-[0-9]{2}-[0-9]{2}/#) != nil {
            digits = s.replacingOccurrences(of: "-", with: "")
        } else if s.count == 8, s.wholeMatch(of: #/[0-9]{8}/#) != nil {
            digits = s
        } else {
            return ""
        }
        guard let y = Int(digits.prefix(4)), let m = Int(digits.dropFirst(4).prefix(2)), let d = Int(digits.suffix(2)),
              valid(y, m, d) else { return "" }
        return String(format: "%04d-%02d-%02d", y, m, d)
    }

    private static func parts(_ day: String) -> (Int, Int, Int) {
        (Int(day.prefix(4)) ?? 0, Int(day.dropFirst(5).prefix(2)) ?? 0, Int(day.suffix(2)) ?? 0)
    }

    /// 从 1970-01-01 算起第几天（days_from_civil，整数运算，同电脑）
    private static func ordinal(_ day: String) -> Int {
        var (y, m, d) = parts(day)
        if m <= 2 { y -= 1 }
        let era = (y >= 0 ? y : y - 399) / 400
        let yoe = y - era * 400
        let doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        return era * 146097 + doe - 719468
    }

    static func spanDays(_ start: String, _ end: String) -> Int { ordinal(end) - ordinal(start) }

    /// 2026年10月13日 / 2026年10月13日—16日 / 2026年9月30日—10月2日 / 2026年12月30日—2027年1月2日
    static func label(_ start: String, _ end: String = "") -> String {
        let a = isoDay(start)
        guard !a.isEmpty else { return "" }
        var b = isoDay(end)
        if b.isEmpty || b < a { b = a }
        let (y1, m1, d1) = parts(a), (y2, m2, d2) = parts(b)
        let head = "\(y1)年\(m1)月\(d1)日"
        if a == b { return head }
        if y1 == y2 && m1 == m2 { return "\(head)\(dash)\(d2)日" }
        if y1 == y2 { return "\(head)\(dash)\(m2)月\(d2)日" }
        return "\(head)\(dash)\(y2)年\(m2)月\(d2)日"
    }

    /// Python 的真假：true、非 0 的数、非空的字符串/数组/字典算真
    private static func truthy(_ v: Any?) -> Bool {
        switch v {
        case let n as NSNumber: return n != 0
        case let s as String: return !s.isEmpty
        case let a as [Any]: return !a.isEmpty
        case let d as [String: Any]: return !d.isEmpty
        default: return false
        }
    }

    /// 一场在议程上是哪天开的（session_date）；视频发布那天不算
    static func sessionDay(_ entry: [String: Any]) -> String { isoDay(entry["session_date"]) }

    /// 一场的视频是哪天发布的（publish_date）；手机上猜的（publish_guessed）不算
    static func publishDay(_ entry: [String: Any]) -> String {
        truthy(entry["publish_guessed"]) ? "" : isoDay(entry["publish_date"])
    }

    /// 「会议时间：…」（manual、agenda）/「视频发布：…」（sessions）
    static func display(_ r: Range?) -> String {
        guard let r, !r.label.isEmpty else { return "" }
        return (r.source == "sessions" ? "视频发布：" : "会议时间：") + r.label
    }

    static func stored(_ manifest: [String: Any]) -> Range? {
        guard let ed = manifest["event_dates"] as? [String: Any] else { return nil }
        let start = isoDay(ed["start"])
        var end = isoDay(ed["end"])
        if end.isEmpty { end = start }
        guard !start.isEmpty, end >= start, let source = ed["source"] as? String, ["manual", "agenda"].contains(source) else { return nil }
        return Range(start: start, end: end, source: source)
    }

    /// 日期够不够多：至少一半的场有日期，而且至少 2 场（整个会议只有 1 场的除外）
    static func enough(_ dated: Int, _ total: Int) -> Bool {
        total > 0 && dated * 2 >= total && (dated >= 2 || total == 1)
    }

    private static func entries(_ manifest: [String: Any]) -> [[String: Any]] {
        ((manifest["entries"] as? [String: Any])?.values.compactMap { $0 as? [String: Any] } ?? [])
            .compactMap { $0["entry"] as? [String: Any] }
    }

    private static func range(_ manifest: [String: Any], _ pick: ([String: Any]) -> String, _ source: String) -> Range? {
        let all = entries(manifest)
        let days = all.map(pick).filter { !$0.isEmpty }.sorted()
        guard let lo = days.first, let hi = days.last, enough(days.count, all.count) else { return nil }
        return Range(start: lo, end: hi, source: source)
    }

    static func agendaRange(_ manifest: [String: Any]) -> Range? { range(manifest, sessionDay, "agenda") }

    static func derived(_ manifest: [String: Any]) -> Range? {
        guard let r = range(manifest, publishDay, "sessions"), spanDays(r.start, r.end) <= maxDerivedSpanDays else { return nil }
        return r
    }

    /// 显示用的：存着的 manual / agenda，没有就现推议程日期，再没有现推视频发布日期
    static func resolve(_ manifest: [String: Any]) -> Range? { stored(manifest) ?? agendaRange(manifest) ?? derived(manifest) }
}
