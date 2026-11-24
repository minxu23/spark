import Foundation

/// 能做笔记的正文里，连着的几块（正文段、列表项、里面只有一段话的引用）放进同一个文本框（MarkableText），
/// 选字、划线才能跨块。这里只放纯逻辑，不碰视图：
/// - 哪几块并成一行（rows）；
/// - 一行拆成文本框里的哪几项、每项对应文件里什么样的几行（units，行键见 MarkAnchor.LineKey）；
/// - 每项在整页同样的项里是第几个（ordinals），文件里按它定是哪一处；
/// - 文本框里的字怎么拼（layoutText），选中的范围落在哪几项（selections / pieces）、每项拆回文件的哪几行（lineParts）。
/// tests/test_mark_run.py 用 swiftc 把这个文件和 MarkRule.swift、MarkAnchor.swift 编在一起核对。
enum MarkRun {
    /// 一行最多几块、几个字：整篇塞进一个文本框，长文会卡
    static let maxBlocks = 40
    static let maxChars = 8000
    /// 批注号前后垫的细空格（只为了排版，原文里没有）
    static let thin = "\u{200A}"

    /// 文本框里的一项：一块字，或者多行引用（对照稿的多行译文）里的一行
    struct Unit: Equatable {
        enum Style: Equatable {
            case body
            /// 列表项。continued：同一个列表项拆出来的后几行，不画项目符号，跟第一行的字对齐
            case bullet(marker: String, depth: Int, continued: Bool)
            case quote
        }
        /// 文章里的第几块（目录、读书笔记跳到某一块时用）
        let block: Int
        /// 一块拆成几行时是第几行（不拆是 0）
        var part = 0
        /// 显示的字（Markdown 写法，交给 Markdown.inline 排）
        let text: String
        let style: Style
        /// 下一项是同一块的下一行：中间不空段距，引用的竖线接着画
        let joinsNext: Bool
        /// 文件里这几行行首有「>」（引用、只看中文时当正文显示的译文）
        var quoted = false
        /// 文件里第一行行首有列表符号
        var listed = false

        /// 对应文件里那几行的行键（一行显示的字对应文件里的一行）
        var keys: [MarkAnchor.LineKey] {
            text.components(separatedBy: "\n").enumerated().map { k, l in
                MarkAnchor.LineKey(quoted: quoted, listed: listed && k == 0, content: MarkAnchor.LineKey.normalized(l))
            }
        }
        struct ID: Hashable { let block: Int; let part: Int }
        var id: ID { ID(block: block, part: part) }
    }

    /// 能并进同一个文本框的块的字数；不能并的（标题、表格、代码、图片、分隔线、图注、里面有标题或列表的引用）是 nil。
    /// simpleQuote：引用里面是不是只有一段话（Markdown.parse 一遍看，由调用方给）
    static func groupLength(_ b: MDBlock, simpleQuote: (String) -> Bool) -> Int? {
        // 字数不算划线记号和批注号：划一下、批注一下，并段的边界不跟着变（页面上的项不变）
        func n(_ t: String) -> Int {
            t.replacingOccurrences(of: MarkRule.pattern, with: "$1", options: .regularExpression)
                .replacingOccurrences(of: #"\[\^note-\d+\]"#, with: "", options: .regularExpression).count
        }
        switch b {
        case .paragraph(let t): return n(t)
        case .bullet(let t, _, _): return n(t)
        case .quote(let t): return simpleQuote(t) ? n(t) : nil
        default: return nil
        }
    }

    /// 一屏一屏排的单位：一般一块一行；grouped（能做笔记）时连着的正文段、列表项、简单引用并成一行，
    /// 一行最多 maxBlocks 块、maxChars 字
    static func rows(_ blocks: [MDBlock], grouped: Bool, simpleQuote: (String) -> Bool) -> [Range<Int>] {
        var out: [Range<Int>] = []
        var chars = 0
        var open = false   // 上一行是能接着并的
        for (i, b) in blocks.enumerated() {
            if grouped, let n = groupLength(b, simpleQuote: simpleQuote) {
                if open, let last = out.last, last.count < maxBlocks, chars + n <= maxChars {
                    out[out.count - 1] = last.lowerBound..<(i + 1)
                    chars += n
                } else {
                    out.append(i..<(i + 1))
                    chars = n
                    open = true
                }
                continue
            }
            out.append(i..<(i + 1))
            open = false
        }
        return out
    }

    /// 这一行是不是排进文本框的（第一块能并）
    static func isRun(_ r: Range<Int>, _ blocks: [MDBlock], grouped: Bool, simpleQuote: (String) -> Bool) -> Bool {
        grouped && groupLength(blocks[r.lowerBound], simpleQuote: simpleQuote) != nil
    }

    /// 第 i 块排在哪一行（那一行第一块的下标，也是那一行的 id）
    static func rowStart(_ i: Int, _ blocks: [MDBlock], grouped: Bool, simpleQuote: (String) -> Bool) -> Int {
        rows(blocks, grouped: grouped, simpleQuote: simpleQuote).first { $0.contains(i) }?.lowerBound ?? i
    }

    /// 列表项前面的符号：有序的「3.」，无序的第一层「•」、更深的「◦」
    static func marker(ordered: String?, depth: Int) -> String {
        ordered.map { "\($0)." } ?? (depth == 0 ? "•" : "◦")
    }

    /// 一行拆成文本框里的几项。多行的块里，文件中每行都带「> 」的（多行引用；只看中文时当正文显示的多行译文，
    /// translated 里记着）一行拆成一项：每行单独定位，划线不会跨行、不会把「> 」包进去。
    /// listed：只看中文时显示成列表项、文件里那一行本身带列表符号的译文（「> - 译文」）
    static func units(_ blocks: [MDBlock], _ r: Range<Int>, translated: Set<Int> = [], listed: Set<Int> = []) -> [Unit] {
        var out: [Unit] = []
        func lines(_ i: Int, _ t: String, quoted: Bool, listed: Bool, style: (Bool) -> Unit.Style) {
            let ls = t.components(separatedBy: "\n")
            for (k, l) in ls.enumerated() {
                out.append(Unit(block: i, part: k, text: l, style: style(k > 0), joinsNext: k < ls.count - 1,
                                quoted: quoted, listed: listed && k == 0))
            }
        }
        for i in r {
            let tr = translated.contains(i)
            switch blocks[i] {
            case .paragraph(let t):
                if tr { lines(i, t, quoted: true, listed: false, style: { _ in .body }) }
                else { out.append(Unit(block: i, text: t, style: .body, joinsNext: false)) }
            case .bullet(let t, let ordered, let depth):
                let m = marker(ordered: ordered, depth: depth)
                if tr {
                    lines(i, t, quoted: true, listed: listed.contains(i), style: { .bullet(marker: m, depth: depth, continued: $0) })
                } else {
                    out.append(Unit(block: i, text: t, style: .bullet(marker: m, depth: depth, continued: false), joinsNext: false, listed: true))
                }
            case .quote(let t):
                lines(i, t, quoted: true, listed: false, style: { _ in .quote })
            default:
                break
            }
        }
        return out
    }

    /// 整页（能做笔记时排进文本框的各行）的项；每项在同样行键的项里是第几个、一共几个
    static func ordinals(_ blocks: [MDBlock], rows: [Range<Int>], translated: Set<Int> = [], listed: Set<Int> = [],
                         simpleQuote: (String) -> Bool) -> [Unit.ID: (ordinal: Int, total: Int)] {
        var all: [Unit] = []
        for r in rows where isRun(r, blocks, grouped: true, simpleQuote: simpleQuote) {
            all += units(blocks, r, translated: translated, listed: listed)
        }
        var seen: [[MarkAnchor.LineKey]: Int] = [:]
        var order: [(Unit.ID, [MarkAnchor.LineKey], Int)] = []
        for u in all {
            let k = u.keys
            let n = seen[k, default: 0]
            seen[k] = n + 1
            order.append((u.id, k, n))
        }
        var out: [Unit.ID: (ordinal: Int, total: Int)] = [:]
        for (id, k, n) in order { out[id] = (n, seen[k] ?? 1) }
        return out
    }

    /// 文本框里的字（同 MarkableText.attributed）：项与项之间一个换行；列表项（不是续行）前面是「符号 + 制表符」，不属于正文；
    /// 批注号（note 为真的那段）前后各垫一个细空格。返回整段字、每项的范围（不含符号）、不属于正文的范围
    static func layoutText(_ items: [(marker: String?, runs: [(text: String, note: Bool)])],
                           thin: String = "\u{200A}") -> (text: String, ranges: [NSRange], excluded: [NSRange]) {
        var s = "", ranges: [NSRange] = [], excluded: [NSRange] = []
        var len = 0
        func add(_ t: String) { s += t; len += t.utf16.count }
        for (i, it) in items.enumerated() {
            if i > 0 { add("\n") }
            if let m = it.marker {
                let at = len
                add(m + "\t")
                excluded.append(NSRange(location: at, length: len - at))
            }
            let at = len
            for r in it.runs { if r.note { add(thin + r.text + thin) } else { add(r.text) } }
            ranges.append(NSRange(location: at, length: len - at))
        }
        return (s, ranges, excluded)
    }

    /// 选中的范围落在每一项里的部分：（第几项, 在这一项字里的范围）。只有空白、细空格的不算
    static func selections(_ text: String, units: [NSRange], selection: NSRange, drop: String = "\u{200A}") -> [(unit: Int, range: NSRange)] {
        let ns = text as NSString
        return units.enumerated().compactMap { i, ur in
            let r = NSIntersectionRange(ur, selection)
            guard r.length > 0, r.location >= ur.location else { return nil }
            let t = ns.substring(with: r).replacingOccurrences(of: drop, with: "")
            guard !t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return (i, NSRange(location: r.location - ur.location, length: r.length))
        }
    }

    /// 选中的一项换成交给 MarkAnchor 的 Target：行键、整页序号、每个文件行选中的范围。
    /// display：页面上这一项的字；render：一行 Markdown 排出来的样子（Markdown.displayed）
    static func target(_ u: Unit, _ ord: [Unit.ID: (ordinal: Int, total: Int)], display: String, selection: NSRange,
                       render: (String) -> MarkAnchor.Rendered) -> MarkAnchor.Target {
        let o = ord[u.id] ?? (0, 1)
        let lines = u.text.components(separatedBy: "\n").map { render($0).text }
        let parts = lineParts(display, lines: lines, selection: selection)
        return MarkAnchor.Target(keys: u.keys, ordinal: o.ordinal, total: o.total, parts: parts ?? [], layoutMismatch: parts == nil)
    }

    /// 一项里选中的部分按文件行拆开。unit：页面上这一项显示的字；lines：这一项每个文件行单独排出来的字（Markdown.displayed）。
    /// lines 用换行接起来必须正好是 unit（不然这一项的字拆不回文件的几行，返回 nil）。<br> 换成的换行在一行之内，不算行界
    static func lineParts(_ unit: String, lines: [String], selection: NSRange) -> [MarkAnchor.Part]? {
        guard lines.joined(separator: "\n") == unit else { return nil }
        var out: [MarkAnchor.Part] = [], at = 0
        for (k, l) in lines.enumerated() {
            let n = l.utf16.count
            let r = NSIntersectionRange(NSRange(location: at, length: n), selection)
            if r.length > 0, r.location >= at {
                let local = NSRange(location: r.location - at, length: r.length)
                let piece = (l as NSString).substring(with: local).replacingOccurrences(of: thin, with: "")
                if !piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { out.append(MarkAnchor.Part(line: k, display: l, range: local)) }
            }
            at += n + 1
        }
        return out
    }

    /// 选中的范围落在每一项里的字。text：文本框里的全部字；units：每项的字在 text 里的范围（不含项目符号）；
    /// excluded：不属于正文的范围（项目符号和它后面的制表符），落在里面的字去掉；drop：排版用的细空格，去掉。
    /// 项与项之间的换行不在任何一项里，自然不算。只有空白的项不算
    static func pieces(_ text: String, units: [NSRange], excluded: [NSRange], selection: NSRange,
                       drop: String = "\u{200A}") -> [(unit: Int, text: String)] {
        let ns = text as NSString
        let cuts = excluded.sorted { $0.location < $1.location }
        var out: [(unit: Int, text: String)] = []
        for (i, ur) in units.enumerated() {
            let r = NSIntersectionRange(ur, selection)
            guard r.length > 0, r.location >= ur.location else { continue }
            var s = "", at = r.location
            let end = NSMaxRange(r)
            for c in cuts where NSMaxRange(c) > at && c.location < end {
                if c.location > at { s += ns.substring(with: NSRange(location: at, length: c.location - at)) }
                at = max(at, NSMaxRange(c))
            }
            if at < end { s += ns.substring(with: NSRange(location: at, length: end - at)) }
            s = s.replacingOccurrences(of: drop, with: "")
            if !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { out.append((i, s)) }
        }
        return out
    }
}
