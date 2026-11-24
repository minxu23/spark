import Foundation

/// 划线的写法，手机上只在这里定义一次（电脑上 core/annotations.py 的 MARK_PATTERN、add_mark 是同一条，
/// tests/test_mark_rule.py 用同一张用例表核对两边）：
/// 「==」+ 内容 +「==」。内容不换行、不含连着的「==」、不以「=」开头或结尾，可以有单个「=」（「Gemini = 沟通」）；
/// 两头的「==」外面不能再紧挨「=」，所以「====x====」「==x===」都不算划线。
enum MarkRule {
    static let inner = #"[^=\n](?:=?[^=\n])*"#
    /// 第 1 组是划的字
    static let pattern = #"(?<!=)==("# + inner + #")==(?!=)"#
    /// 再加第 2 组：后面跟着的批注号
    static let withNotes = pattern + #"((?:\[\^note-\d+\])*)"#
    static let regex = try! NSRegularExpression(pattern: pattern)
    /// 选区两头要去掉的：等号和紧挨的空白
    static let trimChars: Set<unichar> = [0x3D, 0x20, 0x09, 0x0D, 0x0A, 0x3000, 0x00A0]

    /// 每处划线（整段、内容），UTF-16 范围
    static func spans(_ s: String) -> [(whole: NSRange, inner: NSRange)] {
        regex.matches(in: s, range: NSRange(location: 0, length: (s as NSString).length)).map { ($0.range, $0.range(at: 1)) }
    }

    /// 划过的字
    static func texts(_ s: String) -> [String] {
        let ns = s as NSString
        return spans(s).map { ns.substring(with: $0.inner) }
    }

    /// 选中的字两头的「=」和紧挨的空白去掉
    static func trimmed(_ s: String) -> String {
        let u = Array(s.utf16)
        var a = 0, b = u.count
        while a < b, trimChars.contains(u[a]) { a += 1 }
        while b > a, trimChars.contains(u[b - 1]) { b -= 1 }
        return String(utf16CodeUnits: Array(u[a..<b]), count: b - a)
    }

    /// 把 s 里 r 这一段划上，返回新文字；不用改（已经整个在一处划线里、去掉两头的等号后是空的、
    /// 外面紧挨的「=」扩不了）返回 nil，文字不变：
    /// 两头的「=」和紧挨的空白先去掉；碰到（重叠或紧挨着）的已有划线并成一处，中间原来的「==」去掉；
    /// 外面紧挨着一个字面的「=」时往外扩，把「=」和它外面一个字包进来（「=」在行首行尾、连着两个以上的不划）；
    /// 跨了几行的每行各划一处。任何顺序划几次都不会出现「===」
    static func wrap(_ s: String, _ r: NSRange) -> String? {
        let ns = s as NSString
        var a = r.location, b = r.location + r.length
        while a < b, trimChars.contains(ns.character(at: a)) { a += 1 }
        while b > a, trimChars.contains(ns.character(at: b - 1)) { b -= 1 }
        guard a < b else { return nil }
        if ns.range(of: "\n", range: NSRange(location: a, length: b - a)).location != NSNotFound {
            var starts = [a]
            for i in a..<b where ns.character(at: i) == 0x0A { starts.append(i + 1) }
            var out = s, changed = false
            for st in starts.reversed() {
                let nl = ns.range(of: "\n", range: NSRange(location: st, length: b - st)).location
                let end = nl == NSNotFound ? b : nl
                if let got = wrap(out, NSRange(location: st, length: end - st)) { out = got; changed = true }
            }
            return changed ? out : nil
        }
        let all = spans(s)
        if all.contains(where: { $0.inner.location <= a && b <= NSMaxRange($0.inner) }) { return nil }
        var grown = true
        while grown {
            grown = false
            var moved = true
            while moved {
                moved = false
                for m in all {
                    let ms = m.whole.location, me = NSMaxRange(m.whole)
                    if ms <= b, a <= me, ms < a || me > b { a = min(a, ms); b = max(b, me); moved = true }
                }
            }
            // 外面紧挨着一个字面的「=」（「ROI=收益」只选「收益」）：把这个「=」和它外面一个字一起包进来，
            // 扩完再并一次碰到的划线，直到外面不再紧挨「=」。「=」在行首行尾、连着两个以上的，扩不了
            if a > 0, ns.character(at: a - 1) == 0x3D {
                guard a >= 2, ![0x3D, 0x0A].contains(ns.character(at: a - 2)) else { return nil }
                a -= 2; grown = true
            }
            if b < ns.length, ns.character(at: b) == 0x3D {
                guard b + 1 < ns.length, ![0x3D, 0x0A].contains(ns.character(at: b + 1)) else { return nil }
                b += 2; grown = true
            }
        }
        let inner = ns.substring(with: NSRange(location: a, length: b - a)).replacingOccurrences(of: "==", with: "")
        guard let first = inner.utf16.first, let last = inner.utf16.last, first != 0x3D, last != 0x3D, !inner.contains("\n") else { return nil }
        if a > 0, ns.character(at: a - 1) == 0x3D { return nil }
        if b < ns.length, ns.character(at: b) == 0x3D { return nil }
        let out = ns.substring(to: a) + "==" + inner + "==" + ns.substring(from: b)
        return out == s ? nil : out
    }
}
