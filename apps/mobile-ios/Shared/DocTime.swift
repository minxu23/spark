import Foundation

/// 文档头里的两行时间：「- 发布时间：…」「- 收录时间：…」，以及跟着写进文件头、处理记录的机器读的字段。
/// 跟电脑 core/doctime.py 一字不差（tests/test_doctime.py 对照）。
/// - 知道时刻：2026年11月19日，21:12（UTC+8）——换算到写文件这台设备此刻的时区，时区标在括号里；
/// - 只知道哪天：2026年11月19日——不带时刻、不带时区，不编 00:00；
/// - 不知道发布时间：不写这一行。收录时间总有时刻。
/// 机器读的：ISO 8601 带时差（2026-11-19T21:12:00+08:00）。旧写法（- 发布：、- 剪藏：、- 播出时间：、- 发布日期：）读的时候都认。
enum DocTime {
    /// 一个时间：确定到时刻，或只知道哪天（YYYY-MM-DD）
    enum When: Equatable {
        case at(Date)
        case day(String)
    }

    static let publishedLabel = "发布时间"
    static let capturedLabel = "收录时间"
    /// 随手记改过以后才有（Jottings.swift），每改一次换掉
    static let modifiedLabel = "修改时间"
    /// 文档头里的发布时间、收录时间（新旧写法都认：上一版的「发表时间」、更早的「发布」「播出时间」「发布日期」），第 1 组是值
    static let publishedLine = #"(?m)^- (?:发布时间|发表时间|发布|播出时间|发布日期)：(.+?)\s*$"#
    static let capturedLine = #"(?m)^- (?:收录时间|剪藏)：(.+?)\s*$"#

    private static func calendar(_ tz: TimeZone) -> Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = tz
        return c
    }

    /// UTC+8、UTC-7、UTC+5:30、UTC+0
    static func zoneLabel(_ d: Date, tz: TimeZone = .current) -> String {
        let mins = tz.secondsFromGMT(for: d) / 60
        let h = abs(mins) / 60, m = abs(mins) % 60
        return "UTC\(mins < 0 ? "-" : "+")\(h)" + (m == 0 ? "" : String(format: ":%02d", m))
    }

    /// 给人看的写法
    static func cn(_ w: When, tz: TimeZone = .current) -> String {
        switch w {
        case .at(let d):
            let c = calendar(tz).dateComponents([.year, .month, .day, .hour, .minute], from: d)
            return String(format: "%04d年%02d月%02d日，%02d:%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0, c.hour ?? 0, c.minute ?? 0)
                + "（\(zoneLabel(d, tz: tz))）"
        case .day(let s):
            let p = s.split(separator: "-").compactMap { Int($0) }
            return p.count == 3 ? String(format: "%04d年%02d月%02d日", p[0], p[1], p[2]) : s
        }
    }

    /// 机器读的：本机时区、到秒、带时差（+00:00 不写成 Z）
    static func iso(_ d: Date, tz: TimeZone = .current) -> String {
        let c = calendar(tz).dateComponents([.year, .month, .day, .hour, .minute, .second], from: d)
        let mins = tz.secondsFromGMT(for: d) / 60
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d%@%02d:%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0,
                      c.hour ?? 0, c.minute ?? 0, c.second ?? 0, mins < 0 ? "-" : "+", abs(mins) / 60, abs(mins) % 60)
    }

    /// 哪天（YYYY-MM-DD）：知道时刻的取这一刻在本机时区的那天
    static func day(_ w: When, tz: TimeZone = .current) -> String {
        switch w {
        case .at(let d):
            let c = calendar(tz).dateComponents([.year, .month, .day], from: d)
            return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
        case .day(let s): return s
        }
    }

    /// 时刻（同上）
    static func date(_ w: When?) -> Date? {
        if case .at(let d) = w { return d }
        return nil
    }

    private static func validDay(_ y: Int, _ m: Int, _ d: Int) -> String? {
        let c = DateComponents(year: y, month: m, day: d)
        guard c.isValidDate(in: calendar(TimeZone(identifier: "UTC")!)) else { return nil }
        let s = String(format: "%04d-%02d-%02d", y, m, d)
        // 1990-01-01 到「现在 + 2 天」之外的不认（坏数据）
        return s >= "1990-01-01" && s <= utcDay(Date().addingTimeInterval(slack)) ? s : nil
    }

    static let earliest = Date(timeIntervalSince1970: 631_152_000)   // 1990-01-01T00:00:00Z
    static let slack: TimeInterval = 2 * 86400

    private static func utcDay(_ d: Date) -> String { day(.at(d), tz: TimeZone(identifier: "UTC")!) }

    private static func okInstant(_ d: Date) -> Bool { d >= earliest && d <= Date().addingTimeInterval(slack) }

    /// 秒数（13 位的当毫秒）→ 时刻；不在合理范围里返回 nil
    static func fromEpoch(_ seconds: Double) -> Date? {
        guard seconds.isFinite else { return nil }
        let d = Date(timeIntervalSince1970: seconds >= 1e11 ? seconds / 1000 : seconds)
        return okInstant(d) ? d : nil
    }

    private static func instant(_ day: String, _ hh: Int, _ mm: Int, _ ss: Int, offset: Int) -> (When, Int)? {
        guard hh <= 23, mm <= 59, ss <= 59, abs(offset) <= 14 * 60, let tz = TimeZone(secondsFromGMT: offset * 60) else { return nil }
        let p = day.split(separator: "-").compactMap { Int($0) }
        guard let d = calendar(tz).date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: hh, minute: mm, second: ss)),
              okInstant(d) else { return nil }
        return (.at(d), offset)
    }

    /// 认的写法（跟电脑 core/doctime.parse 一模一样）：
    /// - 日期：YYYY-MM-DD、YYYYMMDD、2026年11月19日；
    /// - 时刻：YYYY-MM-DD[T 空格]HH:MM[:SS[.小数]] 后面紧跟时差 Z / ±HH / ±HHMM / ±HH:MM，可以再跟一个 [时区名]（不看）；
    ///   2026年11月19日，21:12（UTC+8）。秒的小数不要。
    /// - 有日期有时刻、但没有时差（或时差写法不认识）：只取日期，不猜时区。
    /// - 不在 1990-01-01 到「现在 + 2 天」之间的不认。认不出返回 nil
    static func parse(_ raw: String) -> When? { parseFull(raw)?.0 }

    // 正则只编一次。写在函数里的 #/…/# 每次执行都重新编译（一次几百微秒到几毫秒）：
    // 剪藏书架排序时每比较一次都要解析日期，几百篇文章就是几千次，会卡到被系统杀掉
    nonisolated(unsafe) private static let cnRegex = #/([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日(?:，([0-9]{1,2}):([0-9]{2})（UTC([+-])([0-9]{1,2})(?::([0-9]{2}))?）)?/#
    nonisolated(unsafe) private static let day8Regex = #/[0-9]{8}/#
    nonisolated(unsafe) private static let isoRegex = #/([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,][0-9]+)?)?(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)?(?:\[[^\]\s]*\])?)?/#
    nonisolated(unsafe) private static let isoLooseRegex = #/([0-9]{4})-([0-9]{2})-([0-9]{2})[T ][0-9]{2}:[0-9]{2}/#

    /// 同 parse，时刻再带上它自己写的时差（分钟）
    static func parseFull(_ raw: String) -> (When, Int?)? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if let m = s.wholeMatch(of: cnRegex) {
            guard let day = validDay(Int(m.1)!, Int(m.2)!, Int(m.3)!) else { return nil }
            guard let hh = m.4, let mm = m.5, let sign = m.6, let oh = m.7 else { return (.day(day), nil) }
            let om = m.8.flatMap { Int($0) } ?? 0
            guard om <= 59 else { return nil }
            return instant(day, Int(hh)!, Int(mm)!, 0, offset: (Int(oh)! * 60 + om) * (sign == "-" ? -1 : 1)).map { ($0.0, $0.1) }
        }
        if s.wholeMatch(of: day8Regex) != nil {
            return validDay(Int(s.prefix(4))!, Int(s.dropFirst(4).prefix(2))!, Int(s.suffix(2))!).map { (.day($0), nil) }
        }
        if let m = s.wholeMatch(of: isoRegex) {
            guard let day = validDay(Int(m.1)!, Int(m.2)!, Int(m.3)!) else { return nil }
            guard let hh = m.4.flatMap({ Int($0) }), let mm = m.5.flatMap({ Int($0) }) else { return (.day(day), nil) }
            let ss = m.6.flatMap { Int($0) } ?? 0
            guard let off = m.7 else { return hh < 24 && mm < 60 && ss < 60 ? (.day(day), nil) : nil }
            var minutes = 0
            if off != "Z" && off != "z" {
                let digits = off.dropFirst().replacingOccurrences(of: ":", with: "")
                let oh = Int(digits.prefix(2))!, om = Int(digits.dropFirst(2)) ?? 0
                guard om <= 59 else { return nil }
                minutes = (oh * 60 + om) * (off.hasPrefix("-") ? -1 : 1)
            }
            return instant(day, hh, mm, ss, offset: minutes).map { ($0.0, $0.1) }
        }
        if let m = s.prefixMatch(of: isoLooseRegex) {
            // 有时刻但时差写法不认识：只取日期
            return validDay(Int(m.1)!, Int(m.2)!, Int(m.3)!).map { (.day($0), nil) }
        }
        return nil
    }

    /// 时刻照它自己写的时差是哪天（跟看的人在哪个时区无关）
    static func writtenDay(_ raw: String) -> String? {
        guard let (w, off) = parseFull(raw) else { return nil }
        if case .at(let d) = w, let off, let tz = TimeZone(secondsFromGMT: off * 60) { return day(.at(d), tz: tz) }
        return day(w)
    }

    /// 有时刻用时刻，没有用日期（文件头 published_at + published、处理记录 publish_at + publish_date 这样成对的）
    static func best(at: String, day: String) -> When? {
        let v = parse(at)
        if case .at = v { return v }
        return parse(day) ?? v
    }

    /// 文档头的两行；不知道发布时间就只有收录时间
    static func lines(published: When?, captured: When?) -> [String] {
        var out: [String] = []
        if let published { out.append("- \(publishedLabel)：\(cn(published))") }
        if let captured { out.append("- \(capturedLabel)：\(cn(captured))") }
        return out
    }

    /// 「- 修改时间：2026年11月22日，21:42（UTC+8）」：随手记改过以后写在收录时间下面（同 core/doctime.modified_line）
    static func modifiedLine(_ d: Date, tz: TimeZone = .current) -> String {
        "- \(modifiedLabel)：\(cn(.at(d), tz: tz))"
    }

    /// 处理记录条目的发表时间：publish_at 优先，没有用 publish_date（YYYYMMDD）；publish_at 照它自己的时差跟日期不是同一天时信日期
    static func entryPublished(at: String, date: String) -> When? {
        let v = best(at: at, day: date)
        if case .at = v, case .day(let d)? = parse(date), let own = writtenDay(at), own != d { return .day(d) }
        return v
    }

    /// 排先后用（倒序时新的在前）：写着的那天|有没有时刻|时刻的秒数。
    /// 哪天以文件、处理记录里写着的日期为准，跟看的人在哪个时区无关；没写日期只有时刻的，取时刻照它自己时差的那天。
    /// 同一天里有时刻的排在只有日期的前面，时刻比绝对秒数。跟 core/doctime.sort_key 排出来的先后一样
    static func sortKey(at: String, day d: String) -> String {
        var inst: Date?
        if case .at(let t)? = parse(at) { inst = t }
        guard let written = parse(d).map({ w -> String in
            if case .at = w { return writtenDay(d) ?? day(w) }
            return day(w)
        }) ?? (inst != nil ? writtenDay(at) : nil) else {
            return "0000-00-00|0|" + String(repeating: "0", count: 12)   // 没有日期的排在最后
        }
        guard let t = inst else { return written + "|0|" + String(repeating: "0", count: 12) }
        return written + "|1|" + String(format: "%012lld", Int64(t.timeIntervalSince1970.rounded(.down)))
    }
}
