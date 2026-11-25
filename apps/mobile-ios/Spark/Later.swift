import Foundation

/// 「稍后再读」和占位剪藏的写法，手机上只在这里定义一次，只依赖 Foundation。
/// 电脑上 core/annotations.py 的 later_value、is_later、later_at、with_later、is_stub、
/// core/clip_compose.py 的 stub_title、stub_source、STUB_NOTE 是同一条；
/// tests/test_later_ios.py 用 swiftc 把这个文件单独编出来，同一张用例表两边输出必须逐字相同。
///
/// 写法：文件头一行「later: <加入的时刻>」（本机时区、到秒、带时差的 ISO 时刻，值不加引号）。
/// 判断在不在稍后再读里只看这一行（有 later:，值去掉两头的引号后不是空、不是 false），不看时钟；「later: true」也算。
/// 占位剪藏：文件头有「stub: true」。
enum LaterRule {
    static let stubNote = "正文还没抓到。可以在阅读页点「补抓正文」再抓一次，也可以先看原文。"

    // MARK: 文件头（照 core/annotations._FRONT_RE：\A---\n(.*?)\n?---[ \t]*(?:\n|\Z)，按码位）

    private static let dash = Unicode.Scalar("-"), newline = Unicode.Scalar("\n"), space = Unicode.Scalar(" "), tab = Unicode.Scalar("\t")

    /// 文件头匹配：head 是两道 --- 中间的字（不含最后那个换行），end 是整个匹配结束的位置
    private static func front(_ s: [Unicode.Scalar]) -> (head: [Unicode.Scalar], end: Int)? {
        guard s.count >= 4, s[0] == dash, s[1] == dash, s[2] == dash, s[3] == newline else { return nil }
        var i = 4
        while i <= s.count {
            var j = i
            if j < s.count, s[j] == newline { j += 1 }
            if j + 3 <= s.count, s[j] == dash, s[j + 1] == dash, s[j + 2] == dash {
                var k = j + 3
                while k < s.count, s[k] == space || s[k] == tab { k += 1 }
                if k == s.count { return (Array(s[4..<i]), k) }
                if s[k] == newline { return (Array(s[4..<i]), k + 1) }
            }
            i += 1
        }
        return nil
    }

    private static func string(_ s: ArraySlice<Unicode.Scalar>) -> String {
        var v = String.UnicodeScalarView(); v.append(contentsOf: s); return String(v)
    }

    /// 文件头按「\n」拆成行（Python 的 split("\n")）
    private static func lines(_ head: [Unicode.Scalar]) -> [String] {
        var out: [String] = [], cur: [Unicode.Scalar] = []
        for c in head {
            if c == newline { out.append(string(cur[...])); cur = [] } else { cur.append(c) }
        }
        out.append(string(cur[...]))
        return out
    }

    /// Python 的 str.strip()：空白字符含换行、\r、\x0b、\x0c、\x1c–\x1f、\x85、不换行空格等
    private static func isSpace(_ c: Unicode.Scalar) -> Bool {
        switch c.value {
        case 0x09...0x0D, 0x1C...0x20, 0x85, 0xA0, 0x1680, 0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000: return true
        default: return false
        }
    }

    private static func strip(_ s: String, _ drop: (Unicode.Scalar) -> Bool) -> String {
        var a = Array(s.unicodeScalars)[...]
        while let f = a.first, drop(f) { a = a.dropFirst() }
        while let l = a.last, drop(l) { a = a.dropLast() }
        return string(a)
    }

    // MARK: 稍后再读

    /// 文件头里 later: 后面的值（去掉两头的引号）；不在稍后再读里返回空串
    static func value(_ text: String) -> String {
        guard let f = front(Array(text.unicodeScalars)) else { return "" }
        for ln in lines(f.head) where ln.hasPrefix("later:") {
            var v = strip(String(ln.dropFirst("later:".count))) { $0 == space || $0 == tab }
            v = strip(v, isSpace)
            v = strip(v) { $0 == Unicode.Scalar("\"") }
            v = strip(v) { $0 == Unicode.Scalar("'") }
            v = strip(v, isSpace)
            if !v.isEmpty, v.lowercased() != "false" { return v }
        }
        return ""
    }

    static func isLater(_ text: String) -> Bool { !value(text).isEmpty }

    /// 加入的时刻（1970 年起的秒，带小数）；值是 true 或认不出的返回 nil
    static func at(_ text: String) -> Double? { instant(value(text)) }

    /// 加进或移出稍后再读。value：要写的时刻（ISO 带时差）；已经在里面再加一次，文件一字不变
    static func with(_ text: String, on: Bool, value v: String) -> String {
        if on, isLater(text) { return text }
        let s = Array(text.unicodeScalars)
        guard let f = front(s) else {
            guard on else { return text }
            var rest = s[...]
            while rest.first == newline { rest = rest.dropFirst() }
            return "---\nlater: \(v)\n---\n\n" + string(rest)
        }
        var head = lines(f.head).filter { !strip($0, isSpace).isEmpty && !$0.hasPrefix("later:") }
        if on { head.append("later: \(v)") }
        let rest = string(s[f.end...])
        if head.isEmpty {
            return String(rest.drop { $0 == "\n" })
        }
        return "---\n" + head.joined(separator: "\n") + "\n---\n" + rest
    }

    /// 此刻：本机时区、到秒、带时差（跟 DocTime.iso 一样，+00:00 不写成 Z）
    static func now(_ d: Date = Date(), tz: TimeZone = .current) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = tz
        let c = cal.dateComponents([.year, .month, .day, .hour, .minute, .second], from: d)
        let mins = tz.secondsFromGMT(for: d) / 60
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d%@%02d:%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0,
                      c.hour ?? 0, c.minute ?? 0, c.second ?? 0, mins < 0 ? "-" : "+", abs(mins) / 60, abs(mins) % 60)
    }

    // MARK: 时刻（照 core/doctime.parse 的 ISO 一支：YYYY-MM-DD[T ]HH:MM[:SS[.fff]][Z|±HH[:MM]]，只认带时差的时刻）

    static func instant(_ raw: String) -> Double? {
        let s = strip(strip(strip(raw, isSpace)) { $0 == Unicode.Scalar("\"") }, isSpace)
        guard let m = Pattern.wholeMatch, let r = m.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) else { return nil }
        let ns = s as NSString
        func g(_ i: Int) -> String? { r.range(at: i).location == NSNotFound ? nil : ns.substring(with: r.range(at: i)) }
        guard let y = Int(g(1)!), let mo = Int(g(2)!), let d = Int(g(3)!), let hh = g(4).flatMap({ Int($0) }), let mm = g(5).flatMap({ Int($0) }),
              let off = g(8) else { return nil }
        let ss = g(6).flatMap { Int($0) } ?? 0
        var minutes = 0
        if off != "Z", off != "z" {
            let digits = String(off.dropFirst()).replacingOccurrences(of: ":", with: "")
            let oh = Int(digits.prefix(2)) ?? 0, om = Int(digits.dropFirst(2)) ?? 0
            guard om <= 59 else { return nil }
            minutes = (oh * 60 + om) * (off.hasPrefix("-") ? -1 : 1)
        }
        guard hh <= 23, mm <= 59, ss <= 59, abs(minutes) <= 14 * 60 else { return nil }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let parts = DateComponents(year: y, month: mo, day: d, hour: hh, minute: mm, second: ss)
        guard parts.isValidDate(in: cal), let date = cal.date(from: parts) else { return nil }
        let secs = date.timeIntervalSince1970 - Double(minutes * 60)
        // 合理范围：1990 年起、不晚于此刻后两天（同 core/doctime._ok_instant）
        guard secs >= 631_152_000, secs <= Date().timeIntervalSince1970 + 2 * 86400 else { return nil }
        return secs
    }

    private enum Pattern {
        static let wholeMatch = try? NSRegularExpression(
            pattern: #"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})([.,][0-9]+)?)?(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)(?:\[[^\]\s]*\])?$"#)
    }

    /// 稍后再读列表的先后（从新到旧排，大的在前）：带时刻的在前、按加入时刻；
    /// 「later: true」这类认不出时刻的在后、按文件修改时间。同电脑 core/search_index.later_docs
    static func order(_ a: (at: Double?, modified: Double), _ b: (at: Double?, modified: Double)) -> Bool {
        func key(_ x: (at: Double?, modified: Double)) -> (Int, Double, Double) { x.at.map { (1, $0, x.modified) } ?? (0, 0, x.modified) }
        let ka = key(a), kb = key(b)
        return (ka.0, ka.1, ka.2) > (kb.0, kb.1, kb.2)
    }

    // MARK: 占位剪藏

    /// 占位剪藏：文件头有 stub: true（只看这一行）
    static func isStub(_ text: String) -> Bool {
        guard let f = front(Array(text.unicodeScalars)) else { return false }
        return lines(f.head).contains { ln in
            guard ln.hasPrefix("stub:") else { return false }
            return strip(String(ln.dropFirst("stub:".count))) { $0 == space || $0 == tab } == "true"
        }
    }

    /// 没有标题时用的：网址的主机名加路径（去掉 www.、查询串和末尾的斜杠），例如 example.com/blog/post
    static func stubTitle(_ url: String) -> String {
        let host = stubSource(url)
        var path = ""
        if let c = URLComponents(string: url) { path = c.percentEncodedPath.removingPercentEncoding ?? c.percentEncodedPath }
        while path.hasSuffix("/") { path.removeLast() }
        let t = String(String.UnicodeScalarView((host + path).unicodeScalars.prefix(200)))
        return t.isEmpty ? String(String.UnicodeScalarView(url.unicodeScalars.prefix(200))) : t
    }

    static func stubSource(_ url: String) -> String {
        let host = (URLComponents(string: url)?.host ?? "").lowercased()
        return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    }

    /// 占位剪藏的文件头要多写的一行：放在文件头最后（在稍后再读那一行之前）
    static func addingStub(_ text: String) -> String {
        let s = Array(text.unicodeScalars)
        guard let f = front(s) else { return text }
        var head = lines(f.head).filter { !strip($0, isSpace).isEmpty && !$0.hasPrefix("stub:") }
        head.append("stub: true")
        return "---\n" + head.joined(separator: "\n") + "\n---\n" + string(s[f.end...])
    }

    /// 补抓到正文后（新文件是重新写的，没有 stub 行）：旧文件头里的星标、置顶、稍后再读几行原样搬到最后（按旧文件里的先后），
    /// 新文件头里原有的这几行去掉。同电脑 core/clip_capture._keep_flags
    static func keepingFlags(old: String, new: String) -> String {
        let keys = ["starred:", "pinned:", "later:"]
        // 论文的作者、arXiv 编号、PDF 原件：旧文件里有这一行才用旧的盖掉新渲染的（同电脑 _PAPER_KEYS）
        let paperKeys = ["authors:", "arxiv:", "pdf:"]
        func headEnd(_ s: [Unicode.Scalar]) -> Int? {
            guard s.count >= 4, s[0] == dash, s[1] == dash, s[2] == dash, s[3] == newline else { return nil }
            let pat: [Unicode.Scalar] = [newline, dash, dash, dash, newline]
            var i = 4
            while i + 5 <= s.count { if Array(s[i..<(i + 5)]) == pat { return i }; i += 1 }
            return nil
        }
        let o = Array(old.unicodeScalars), n = Array(new.unicodeScalars)
        guard let ne = headEnd(n) else { return new }
        let olds = headEnd(o).map { lines(Array(o[4..<$0])) } ?? []
        let flags = olds.filter { ln in (keys + paperKeys).contains { ln.hasPrefix($0) } }
        let drop = keys + paperKeys.filter { k in olds.contains { $0.hasPrefix(k) } }
        let kept = lines(Array(n[4..<ne])).filter { ln in !drop.contains { ln.hasPrefix($0) } }
        return "---\n" + (kept + flags).joined(separator: "\n") + string(n[ne...])
    }
}
