import Foundation

/// 划线、批注、取消划线落到文件里的哪一行、哪几个字。只用 Foundation，不碰视图：
/// tests/test_mark_run.py 用 swiftc 把它和 MarkRule.swift、MarkRun.swift 编在一起测。
///
/// 规则（电脑端要做同样的事时照这几条实现）：
/// 1. 只认整行。文件按行切开，每行先认种类、去掉行首前缀，得到「行键」：
///    引用行（行首「>」，前缀是缩进 +「>」+ 后面的空白）、列表行（前缀是缩进 + 列表符号 + 空白；
///    引用里的列表「> - 项」两样都有）、正文行（前缀是缩进）。键 = (是不是引用, 是不是列表, 去掉前缀和首尾空白后的内容)。
/// 2. 永远不是候选的行：文件头、代码块里的行（「```markdown」包着的成稿除外，照正文认）、标题行、
///    表格行、分隔线、单独一行的图片、「[^note-N]:」批注定义、「## 读后感」那一节、碰到 HTML 注释的行、空行。
///    剪藏（scope.tidy）另外照 ClipTidy 把只有「/」「-」「01.」的行当空行、「①…」短行当标题、「•文字」当列表行。
/// 3. 查找范围：信息文章的小结页签只在第一个「## 小结」到它后面第一个「## 原文」之间；原文页签只在那个「## 原文」之后；
///    别的文档是整篇（去掉文件头）。
/// 4. 页面上的一项（MarkRun.Unit）带着它对应的几行的键。候选 = 范围里连续几行、逐行键相同；
///    正文段还要正好是一整段（前后不再接正文行）。
/// 5. 同样的键在页面上出现 total 次、这一项是第 ordinal 个（0 起）：范围里的候选也必须正好 total 个，取第 ordinal 个；
///    个数不等不猜，这一项不改（Failure.ambiguous）。
/// 6. 一次操作（跨项划线、取消、批注）先用没改过的文件把每一项都定好位，再从后往前一次写完。两项定到同一行也不改。
/// 7. 一行里落在哪几个字上，不查找、不数「第几处」，用对照表换算：文件里这一行（去掉前缀的内容）先过 InlineSource
///    （Markdown.inline 用的同一份改写，带着每个字的来处），再和系统排出来的字逐字对上（align），得到「显示的每个字是原文第几个字」。
///    这样排出来的字必须和页面上这一行的字一样（只允许两头空白不同），不一样就不改（Failure.layout）。
///    看不见的：链接的方括号和地址、双链的目标和竖线、批注号、「**」「==」、反引号、转义的反斜杠、行首前缀、剪藏里的零宽字符和「****」；
///    换过的：直引号、弯引号（一个字换一个字）；凭空加的（没有来处）：批注号两边的细空格、批注气泡、<br> 换成的换行。
///    选区在显示字上的范围直接换成原文范围；在 <br> 换成的换行处断开。
/// 8. 选区碰到加粗、行内代码、链接、[[双链]] 的一半时，扩到整个记号（只落在加粗或链接文字里面的不扩）；
///    碰到批注号「[^note-N]」、<br> 时断开，分几处划，原来的批注号仍紧跟在它那处划线后面；断开后会切在加粗、链接中间的不划（refused）。
///    划线只在内容部分里做，行首前缀永远不进划线、也不会被当成「=」外面那个字。
/// 9. 选区跨了几行（硬换行、软换行、拆开的多行引用），每行各划一处；批注号挂在最后一处后面。任何时候都不写出跨行的「==」。
/// 10. 每改一行都重排一遍核对（verify）：显示的字没变、选中的字都有了底色、新多出来的底色只在改动的那一截里；不过就不改这一行（layout）。
///     取消划线同样换算：只去掉和选区（原文范围）相交的划线。批注整次成功或整次不改；新批注号取正文和文末用过的最大号 + 1。
enum MarkAnchor {
    static let glyph: Character = "✎"

    // MARK: 行键、范围、要改的项

    struct LineKey: Hashable {
        var quoted: Bool
        var listed: Bool
        var content: String

        static func normalized(_ s: String) -> String { s.trimmingCharacters(in: .whitespaces) }
        var isParagraph: Bool { !quoted && !listed }
    }

    struct Scope: Equatable {
        /// 信息文章分两节时：「小结」或「原文」。nil 是整篇
        var section: String? = nil
        /// 剪藏：显示前按 ClipTidy 整理过，行级的几条照着认
        var tidy = false
        /// 单集小结：页面上不画旧的「## 我的高亮」一节（ReaderShell.summaryBody），这一节的行也不算候选
        var skipHighlights = false
    }

    // MARK: 文件里的行

    struct SourceLine: Equatable {
        let index: Int
        let key: LineKey
        let content: NSRange       // 内容部分在全文里的范围（UTF-16）
        var runStart = true        // 正文行：前一行不是正文行
        var runEnd = true          // 正文行：后一行不是正文行
    }


    /// 每行的范围（不含换行）
    static func lineRanges(_ ns: NSString) -> [NSRange] {
        var out: [NSRange] = []
        var start = 0
        while true {
            let nl = ns.range(of: "\n", range: NSRange(location: start, length: ns.length - start))
            let end = nl.location == NSNotFound ? ns.length : nl.location
            out.append(NSRange(location: start, length: end - start))
            if nl.location == NSNotFound { break }
            start = end + 1
        }
        return out
    }

    private static func re(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p) }
    private static let headingRE = re(#"^#{1,6} "#)
    private static let imageRE = re(#"^!\[([^\]]*)\]\(<?([^)>\s]+)>?\)$"#)
    private static let htmlRE = re(#"^</?(details|think)>$|^<br\s*/?>$|^<summary>(.*)</summary>$"#)
    private static let noteDefRE = re(#"^\[\^note-\d+\]:"#)
    private static let listRE = re(#"^(?:[-*+]|\d{1,3}[.)]) "#)
    private static let tidyBlankRE = re(#"^(/|[-*+]|\d{1,3}[.)]|\d{1,2}\s*/\s*\d{1,2})$"#)
    private static let tidyHeadRE = re(#"^[①-⑳]\s*\S.{0,28}$"#)

    private static func has(_ r: NSRegularExpression, _ s: String) -> Bool {
        r.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) != nil
    }

    /// 空行：同 Markdown.parse（去掉首尾空白后没有字；空白含全角空格、不间断空格）。
    /// 剪藏（tidy）先照 ClipTidy 去掉零宽字符和「****」再看
    static func blank(_ raw: String, tidy: Bool = false) -> Bool {
        let t = tidy ? InlineSource.tidied(.init(raw)).string : raw
        return t.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private static let creditRE = re(#"[丨|｜][^丨|｜]{1,24}$"#)
    private static let sentenceEndRE = re(#"[。！？!?：:]$"#)
    private static let preambleRE = re(#"^[-*+] [^：:]{1,12}[：:]"#)
    private static let tableSepRE = re(#"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$"#)

    /// 范围里能做候选的行。每行先去掉行尾的「\r」（CRLF 文件；写回时只换内容部分，换行符不动）
    static func scan(_ text: String, scope: Scope) -> [SourceLine] {
        let ns = text as NSString
        var ranges = lineRanges(ns)
        for k in ranges.indices where ranges[k].length > 0 && ns.character(at: NSMaxRange(ranges[k]) - 1) == 0x0D { ranges[k].length -= 1 }
        let raws = ranges.map { ns.substring(with: $0) }
        // 文件头：同 Markdown.parse，第一行是「---」，到后面第一个单独的「---」（它后面还要有换行）
        var first = 0
        if raws.first == "---", let end = raws.indices.dropFirst().first(where: { raws[$0] == "---" && $0 + 1 < raws.count }) { first = end + 1 }
        // 碰到 HTML 注释、读后感一节的行
        var blocked = IndexSet()
        func block(_ r: NSRange) {
            for (i, lr) in ranges.enumerated() where NSIntersectionRange(lr, r).length > 0 || (lr.length == 0 && NSLocationInRange(lr.location, r)) {
                blocked.insert(i)
            }
        }
        for m in re(#"<!--[\s\S]*?-->"#).matches(in: text, range: NSRange(location: 0, length: ns.length)) { block(m.range) }
        if let r = reflectionRange(text) { block(NSRange(r, in: text)) }

        // 先过一遍：代码块、两节的标题、表格（连着的「|」行，第二行是 |---| 才算表格，不然照正文，同 parse）
        var fenced = [Bool](repeating: false, count: ranges.count)   // 代码块、表格里（含栅栏行）
        var inFence = false, mdFence = false
        var summaryAt: Int?, originalAt: Int?
        func checkHeading(_ t: String, _ i: Int) {
            if t == "## 小结", summaryAt == nil { summaryAt = i }
            else if t == "## 原文", let s = summaryAt, i > s, originalAt == nil { originalAt = i }
        }
        var tableStart: Int?
        var fenceStart = 0
        func endTable(_ i: Int) {
            if let s = tableStart, i - s >= 2, has(tableSepRE, raws[s + 1].trimmingCharacters(in: .whitespaces)) {
                for k in s..<i { fenced[k] = true }
            }
            tableStart = nil
        }
        for (i, raw) in raws.enumerated() where i >= first {
            let t = raw.trimmingCharacters(in: .whitespaces)
            if inFence {
                if raw.hasPrefix("```") { inFence = false; fenced[i] = true; continue }
                fenced[i] = !mdFence
                if mdFence { checkHeading(t, i) }
                continue
            }
            if t.hasPrefix("|") { if tableStart == nil { tableStart = i }; continue }
            endTable(i)
            if t.hasPrefix("```") {
                inFence = true
                fenceStart = i
                let lang = t.dropFirst(3).trimmingCharacters(in: .whitespaces).lowercased()
                mdFence = lang == "markdown" || lang == "md"
                fenced[i] = true
                continue
            }
            checkHeading(t, i)
        }
        endTable(raws.count)
        // 没关上的栅栏：parse 把后面全当代码块（「```markdown」也一样），都不是候选
        if inFence { for k in fenceStart..<raws.count { fenced[k] = true } }
        // 开头的「# 标题」和紧跟的「- 来源：…」这串说明：页面上去掉了（Markdown.stripPreamble），也不算候选
        do {
            var k = first
            func skipBlank() { while k < raws.count, blank(raws[k]) { k += 1 } }
            skipBlank()
            if k < raws.count, raws[k].trimmingCharacters(in: .whitespaces).hasPrefix("# ") { fenced[k] = true; k += 1 }
            skipBlank()
            while k < raws.count, has(preambleRE, raws[k].trimmingCharacters(in: .whitespaces)) { fenced[k] = true; k += 1; skipBlank() }
        }
        // 单集小结的「## 我的高亮」：到下一个一、二级标题为止
        if scope.skipHighlights {
            var on = false
            for (k, raw) in raws.enumerated() where k >= first {
                let t = raw.trimmingCharacters(in: .whitespaces)
                if let m = headingRE.firstMatch(in: t, range: NSRange(location: 0, length: (t as NSString).length)) {
                    let level = m.range.length - 1
                    if on && level <= 2 { on = false }
                    if (t as NSString).substring(from: m.range.length) == "我的高亮" { on = true }
                }
                if on { fenced[k] = true }
            }
        }
        var lo = first, hi = ranges.count
        if let section = scope.section {
            guard let s = summaryAt, let o = originalAt else { return [] }
            if section == "小结" { lo = s + 1; hi = o } else { lo = o + 1 }
        }

        var out: [SourceLine] = []
        for i in lo..<max(lo, hi) where !fenced[i] && !blocked.contains(i) {
            if let l = classify(raws[i], lineStart: ranges[i].location, index: i, tidy: scope.tidy) { out.append(l) }
        }
        // 正文段的头尾：前后一行（按行号紧挨着）也是正文行就不是头 / 尾
        func runs() {
            for k in out.indices where out[k].key.isParagraph {
                out[k].runStart = !(k > 0 && out[k - 1].key.isParagraph && out[k - 1].index == out[k].index - 1)
                out[k].runEnd = !(k + 1 < out.count && out[k + 1].key.isParagraph && out[k + 1].index == out[k].index + 1)
            }
        }
        runs()
        // 图注（同 Markdown.captions）：单独成段的一行短句，在图片后面，或者「……丨来源」；后面紧跟引用（对照稿的译文）的不算。
        // 页面上排成图注，不能划线，也不算候选（不然别处同样的字会落到这一行上）
        out.removeAll { l in
            guard l.key.isParagraph, l.runStart, l.runEnd else { return false }
            // 同 Markdown.captions：划线、批注号不算
            let t = l.key.content.replacingOccurrences(of: MarkRule.pattern, with: "$1", options: .regularExpression)
                .replacingOccurrences(of: #"\[\^note-\d+\]"#, with: "", options: .regularExpression)
            guard t.count <= 80 else { return false }
            func neighbor(_ step: Int) -> String? {
                var k = l.index + step
                while k >= 0, k < raws.count, blank(raws[k], tidy: scope.tidy) { k += step }
                return k >= 0 && k < raws.count ? raws[k].trimmingCharacters(in: .whitespaces) : nil
            }
            if neighbor(1)?.hasPrefix(">") == true { return false }
            let credited = has(creditRE, t) && !t.contains("](")
            let afterImage = neighbor(-1).map { has(imageRE, $0) } ?? false
            let short = t.count <= 50 && !has(sentenceEndRE, t)
            return credited || (afterImage && short)
        }
        runs()
        return out
    }

    /// 一行的种类和内容；不能做候选的返回 nil。行首、行尾的空白按 CharacterSet.whitespaces 算（含全角空格），同 parse
    static func classify(_ raw: String, lineStart: Int, index: Int, tidy: Bool) -> SourceLine? {
        if blank(raw, tidy: tidy) { return nil }
        let u = Array(raw.utf16)
        func isSpace(_ c: UInt16) -> Bool { c < 0xD800 && CharacterSet.whitespaces.contains(Unicode.Scalar(c)!) }
        func isSkip(_ c: UInt16) -> Bool { isSpace(c) || (tidy && [0x2060, 0x200B, 0x200C, 0x200D, 0xFEFF].contains(c)) }
        var a = 0
        while a < u.count, isSkip(u[a]) { a += 1 }
        var b = u.count
        while b > a, isSkip(u[b - 1]) { b -= 1 }
        guard a < b else { return nil }
        var t = String(utf16CodeUnits: Array(u[a..<b]), count: b - a)
        if tidy { t = InlineSource.tidied(.init(t)).string.trimmingCharacters(in: .whitespaces) }
        if tidy, has(tidyBlankRE, t) { return nil }
        if has(noteDefRE, t) || t == "---" || t == "***" || has(imageRE, t) || has(htmlRE, t) || has(headingRE, t) { return nil }
        if tidy, has(tidyHeadRE, t), !t.hasSuffix("。") { return nil }
        var quoted = false, listed = false
        if u[a] == 0x3E {   // >
            quoted = true
            a += 1
            while a < b, isSkip(u[a]) { a += 1 }
        }
        if a < b {
            let rest = String(utf16CodeUnits: Array(u[a..<b]), count: b - a)
            if let m = listRE.firstMatch(in: rest, range: NSRange(location: 0, length: (rest as NSString).length)) {
                listed = true
                a += m.range.length
                while a < b, isSkip(u[a]) { a += 1 }
            } else if tidy, !quoted, let f = rest.first, "•●▪︎·".contains(f), rest.count > 1 {
                listed = true
                a += String(f).utf16.count
                while a < b, isSkip(u[a]) { a += 1 }
            }
        }
        guard a < b else { return nil }
        var content = String(utf16CodeUnits: Array(u[a..<b]), count: b - a)
        if tidy { content = InlineSource.tidied(.init(content)).string }
        return SourceLine(index: index, key: LineKey(quoted: quoted, listed: listed, content: LineKey.normalized(content)),
                          content: NSRange(location: lineStart + a, length: b - a))
    }

    // MARK: 定位

    static func locate(_ lines: [SourceLine], _ t: Target) -> Result<[SourceLine], LocateError> {
        let k = t.keys.count
        guard k > 0, lines.count >= k else { return .failure(.init(.notFound)) }
        var found: [[SourceLine]] = []
        for i in 0...(lines.count - k) {
            var ok = true
            for j in 0..<k {
                let l = lines[i + j]
                if l.key != t.keys[j] || (j > 0 && l.index != lines[i + j - 1].index + 1) { ok = false; break }
            }
            guard ok else { continue }
            if t.keys[0].isParagraph, !(lines[i].runStart && lines[i + k - 1].runEnd) { continue }
            found.append(Array(lines[i..<(i + k)]))
        }
        if found.isEmpty { return .failure(.init(.notFound)) }
        guard found.count == t.total, t.ordinal < found.count else { return .failure(.init(.ambiguous)) }
        return .success(found[t.ordinal])
    }

    struct LocateError: Error { let f: Failure; init(_ f: Failure) { self.f = f } }

    // MARK: 一行里选中的字：显示字 → 原文的对照表

    /// 一行排出来的样子（同页面：批注号两边垫细空格）和每个 UTF-16 位置有没有划线底色。由 Markdown.displayed 给
    struct Rendered: Equatable {
        var text: String
        var marked: [Bool]
    }

    /// 交给系统排版时可能被吃掉的原文字：强调、行内代码、删除线的记号，转义的反斜杠，自动链接的尖括号，图片前的感叹号
    static let markdownSyntax: Set<UInt16> = Set("*_`~\\<>!".utf16)

    /// 对照表：页面上这一行显示的每个字（UTF-16）是原文（这一行的内容）第几个字；凭空加的（细空格、批注气泡、<br> 换成的换行）是 nil。
    /// 原文先过一遍 InlineSource（跟 Markdown.inline 是同一份代码，带着每个字的来处），剩下的交给系统排版的那一步只会吃掉字、
    /// 不会加字：一个字一个字往下对，对得上就记下来处，对不上的只能是 InlineSource 加的语法字或 markdownSyntax 里的记号，
    /// 否则对不上（返回 nil）。最后两边都要正好走完
    static func align(_ m: InlineSource.Traced, _ shown: String) -> [Int?]? {
        let f = Array(shown.utf16)
        let thin = MarkRun.thin.utf16.first!
        var map = [Int?](repeating: nil, count: f.count)
        var i = 0, j = 0
        while i < m.u.count {
            let c = m.u[i], o = m.o[i]
            if o == InlineSource.hidden { i += 1; continue }
            if j < f.count, f[j] == c { map[j] = o >= 0 ? o : nil; i += 1; j += 1; continue }
            if j < f.count, f[j] == thin { j += 1; continue }
            if o >= 0, markdownSyntax.contains(c) { i += 1; continue }
            return nil
        }
        while j < f.count, f[j] == thin { j += 1 }
        return j == f.count ? map : nil
    }

    /// 文件里这一行（内容部分）排出来的字和对照表。tidy：剪藏，先按 ClipTidy 去掉零宽字符和「****」
    static func traced(_ c: String, tidy: Bool, render: (String) -> Rendered) -> (shown: Rendered, map: [Int?])? {
        var t = InlineSource.Traced(c)
        if tidy { t = InlineSource.tidied(t) }
        let shown = render(t.string)
        guard let map = align(InlineSource.prepare(t), shown.text) else { return nil }
        return (shown, map)
    }

    private static let boldRE = re(#"\*\*[^*\n]+?\*\*"#)
    private static let codeRE = re(#"`[^`\n]+`"#)
    private static let linkRE = re(#"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:<[^>]*>|[^)]*)\)"#)
    private static let wikiRE = re(#"\[\[[^\]\n]*\]\]"#)
    static let noteRefRE = re(#"\[\^note-\d+\]"#)
    private static let brRE = re(#"<br\s*/?>"#)

    /// 选区只碰到记号的一半：扩到整个记号。落在加粗里面、链接文字里面的不扩；行内代码、双链碰到就扩；转义的字连反斜杠一起
    static func expanded(_ r: NSRange, in c: String) -> NSRange {
        let all = NSRange(location: 0, length: (c as NSString).length)
        var r = r
        var moved = true
        while moved {
            moved = false
            func grow(_ whole: NSRange, inner: NSRange?) {
                guard NSIntersectionRange(whole, r).length > 0 else { return }
                if let inner, inner.location <= r.location, NSMaxRange(r) <= NSMaxRange(inner) { return }
                let u = NSUnionRange(whole, r)
                if !NSEqualRanges(u, r) { r = u; moved = true }
            }
            for m in boldRE.matches(in: c, range: all) {
                grow(m.range, inner: NSRange(location: m.range.location + 2, length: m.range.length - 4))
            }
            for m in linkRE.matches(in: c, range: all) { grow(m.range, inner: m.range(at: 1)) }
            for m in codeRE.matches(in: c, range: all) { grow(m.range, inner: nil) }
            for m in wikiRE.matches(in: c, range: all) { grow(m.range, inner: nil) }
            // 转义的字（\*、\[）：反斜杠一起包进来，不然「\==*」会把反斜杠露出来
            let ns = c as NSString
            if r.location > 0, ns.character(at: r.location - 1) == 0x5C, r.location < ns.length,
               let sc = Unicode.Scalar(ns.character(at: r.location)), sc.isASCII, CharacterSet.punctuationCharacters.union(.symbols).contains(sc) {
                r = NSRange(location: r.location - 1, length: r.length + 1); moved = true
            }
        }
        return r
    }

    /// 在批注号和 <br> 处断开（批注号不进划线；<br> 两边各划一处）
    static func splitAtNotes(_ r: NSRange, in c: String) -> [NSRange] {
        var cuts = noteRefRE.matches(in: c, range: r).map(\.range) + brRE.matches(in: c, range: r).map(\.range)
        cuts.sort { $0.location < $1.location }
        var out: [NSRange] = []
        var at = r.location
        for m in cuts where m.location >= at {
            if m.location > at { out.append(NSRange(location: at, length: m.location - at)) }
            at = NSMaxRange(m)
        }
        if at < NSMaxRange(r) { out.append(NSRange(location: at, length: NSMaxRange(r) - at)) }
        let ns = c as NSString
        return out.filter { !ns.substring(with: $0).trimmingCharacters(in: CharacterSet(charactersIn: "= \t\u{3000}\u{00A0}")).isEmpty }
    }

    /// 断开后的一段切在加粗、链接、行内代码、双链中间：划上会和它们交叉（「==**重点==…**」），不划
    static func cutsMarkup(_ r: NSRange, in c: String) -> Bool {
        let all = NSRange(location: 0, length: (c as NSString).length)
        for re in [boldRE, linkRE, codeRE, wikiRE] {
            for m in re.matches(in: c, range: all) {
                let w = m.range
                let inter = NSIntersectionRange(w, r)
                guard inter.length > 0, !(r.location <= w.location && NSMaxRange(w) <= NSMaxRange(r)) else { continue }
                // 整段落在加粗里面、链接文字里面是可以的
                if re === boldRE, w.location + 2 <= r.location, NSMaxRange(r) <= NSMaxRange(w) - 2 { continue }
                if re === linkRE, NSLocationInRange(r.location, m.range(at: 1)) || m.range(at: 1).location == r.location,
                   NSMaxRange(r) <= NSMaxRange(m.range(at: 1)) { continue }
                return true
            }
        }
        return false
    }

    /// 去掉两头的「=」和空白后，已经整个落在一处划线里
    static func insideMark(_ c: String, _ r: NSRange) -> Bool {
        let ns = c as NSString
        var a = r.location, b = NSMaxRange(r)
        while a < b, MarkRule.trimChars.contains(ns.character(at: a)) { a += 1 }
        while b > a, MarkRule.trimChars.contains(ns.character(at: b - 1)) { b -= 1 }
        if a >= b { return true }
        return MarkRule.spans(c).contains { $0.inner.location <= a && b <= NSMaxRange($0.inner) }
    }

    /// 页面上一行里选中的范围（显示字的下标）换成原文里的几段：凭空加的字不算，在显示出来的换行（<br>）处断开
    static func sourceRanges(_ sel: NSRange, shown: String, map: [Int?]) -> [NSRange] {
        let f = Array(shown.utf16)
        var out: [NSRange] = []
        var lo: Int?, hi = 0
        func flush() { if let l = lo { out.append(NSRange(location: l, length: hi - l)) }; lo = nil }
        for k in max(0, sel.location)..<min(f.count, NSMaxRange(sel)) {
            guard let o = map[k] else { if f[k] == 0x0A { flush() }; continue }
            if lo == nil { lo = o }
            lo = min(lo!, o)
            hi = max(hi, o + 1)
        }
        flush()
        return out
    }

    /// 一行里要改的几段（原文下标），已经扩好、断开
    static func pieces(_ c: String, _ ranges: [NSRange]) -> (pieces: [NSRange], refused: Bool) {
        var out: [NSRange] = [], refused = false
        for r in ranges {
            let subs = splitAtNotes(expanded(r, in: c), in: c)
            if subs.contains(where: { cutsMarkup($0, in: c) }) { refused = true; continue }
            out += subs
        }
        return (out.sorted { $0.location < $1.location }, refused)
    }

    /// 划这几段（从右往左）。note：最后一段（最右边）后面挂批注号 [^note-note]，挂在盖住它的那处划线和已有批注号后面
    static func wrapLine(_ c: String, _ ps: [NSRange], note: Int? = nil) -> (text: String, refused: Bool)? {
        var out = c, refused = false
        for (k, s) in ps.enumerated().reversed() {
            if k == ps.count - 1, let id = note {
                // 在它开头插一个记号字，划完按记号找到盖住它的那处划线
                let mark = "\u{E010}"
                let ns = out as NSString
                var a = s.location
                while a < NSMaxRange(s), MarkRule.trimChars.contains(ns.character(at: a)) { a += 1 }
                let withMark = ns.replacingCharacters(in: NSRange(location: a, length: 0), with: mark)
                let wrapped = MarkRule.wrap(withMark, NSRange(location: s.location, length: s.length + 1)) ?? withMark
                let wns = wrapped as NSString
                let at = wns.range(of: mark).location
                guard at != NSNotFound, let span = MarkRule.spans(wrapped).first(where: { NSLocationInRange(at, $0.inner) }) else { return nil }
                var end = NSMaxRange(span.whole)
                while let m = noteRefRE.firstMatch(in: wrapped, options: .anchored, range: NSRange(location: end, length: wns.length - end)) {
                    end = NSMaxRange(m.range)
                }
                out = wns.replacingCharacters(in: NSRange(location: end, length: 0), with: "[^note-\(id)]").replacingOccurrences(of: mark, with: "")
                continue
            }
            if let w = MarkRule.wrap(out, s) { out = w } else if !insideMark(out, s) { refused = true }
        }
        return (out, refused)
    }

    /// 去掉和这几段（原文下标）相交的划线
    static func unmarkLine(_ c: String, _ ranges: [NSRange]) -> String? {
        let ns = c as NSString
        var out = c
        for m in MarkRule.spans(c).reversed() where ranges.contains(where: { NSIntersectionRange($0, m.whole).length > 0 || NSLocationInRange($0.location, m.whole) }) {
            out = (out as NSString).replacingCharacters(in: m.whole, with: ns.substring(with: m.inner))
        }
        return out == c ? nil : out
    }

    // MARK: 一次操作

    /// 页面上选中的一项里，某一行（文件里的一行）选中的部分
    struct Part: Equatable {
        var line: Int          // 这一项里的第几行
        var display: String    // 页面上这一行显示的字（Markdown.displayed 排的，含批注号旁的细空格）
        var range: NSRange     // 选中的范围（在 display 里）
    }

    /// 一次操作里的一项
    struct Target: Equatable {
        var keys: [LineKey]
        var ordinal: Int
        var total: Int
        var parts: [Part] = []
        /// 页面上这一项的字拆不回文件的几行（MarkRun.lineParts 对不上）：不改，报「排版对不上」
        var layoutMismatch = false
    }

    enum Failure: Equatable {
        case notFound       // 文件里没有这一项（文件刚被改过）
        case ambiguous      // 同样的字出现了不止一次，定不了是哪一处
        case refused        // 找到了但不能划（紧挨着「=」、会跟加粗交叉等）
        case layout         // 这一行排出来的字和页面上的对不上，没法换回原文
        case notMarkable    // 这一行不能划（提示框标题这类：页面上的字是改写过的）
    }

    enum Op: Equatable { case mark, unmark, annotate(String) }

    struct Outcome: Equatable {
        var text: String?        // 改后的文件；没改是 nil
        var done = 0             // 改成的处数
        var failures: [Failure] = []
        var noteID: Int? = nil
    }

    private static let calloutRE = re(#"^\[![A-Za-z-]+\][+-]?\s*"#)

    /// 找不到时分清是不是提示框标题这类不能划的行
    private static func whyNotFound(_ text: String, _ t: Target, scope: Scope) -> Failure {
        guard let k = t.keys.first, k.quoted, k.content.hasPrefix("**"), k.content.hasSuffix("**") else { return .notFound }
        let title = String(k.content.dropFirst(2).dropLast(2))
        for raw in text.components(separatedBy: "\n") {
            var l = raw.trimmingCharacters(in: .whitespaces)
            guard l.hasPrefix(">") else { continue }
            l = String(l.dropFirst()).trimmingCharacters(in: .whitespaces)
            if let m = calloutRE.firstMatch(in: l, range: NSRange(location: 0, length: (l as NSString).length)),
               (l as NSString).substring(from: m.range.length).trimmingCharacters(in: .whitespacesAndNewlines) == title { return .notMarkable }
        }
        return .notFound
    }

    /// 页面上一行的 display 和文件里那一行排出来的字核对，选区换到后者的下标上。只允许两头空白不同（段落行首的缩进）
    static func shift(_ p: Part, to shown: String) -> NSRange? {
        func lead(_ s: String) -> Int { s.utf16.prefix { [0x20, 0x09, 0x3000, 0xA0].contains($0) }.count }
        let a = p.display.trimmingCharacters(in: .whitespaces), b = shown.trimmingCharacters(in: .whitespaces)
        guard a == b else { return nil }
        let la = lead(p.display), lb = lead(shown)
        let lo = max(p.range.location - la, 0) + lb
        let hi = min(NSMaxRange(p.range) - la, (a as NSString).length) + lb
        return hi > lo ? NSRange(location: lo, length: hi - lo) : nil
    }

    /// 先全部定位、算好每行的新内容，再从后往前一次写完。
    /// 批注整次成功或整次不改：有一处没成就什么都不写（Outcome.text 为 nil），写好的批注由调用方留着。
    /// render：一行 Markdown 排出来的样子（Markdown.displayed）。每改一行都用它核对：显示的字没变、选中的字都有了底色、
    /// 新多出来的底色只在改动的那一截里；核对不过这一行不改（.layout）
    static func apply(_ text: String, _ op: Op, _ targets: [Target], scope: Scope, render: (String) -> Rendered) -> Outcome {
        let lines = scan(text, scope: scope)
        let ns = text as NSString
        var out = Outcome()
        var located: [[SourceLine]?] = []
        var owner: [Int: Int] = [:]     // 文件行号 → 第几项
        for (ti, t) in targets.enumerated() {
            if t.layoutMismatch { located.append(nil); out.failures.append(.layout); continue }
            switch locate(lines, t) {
            case .failure(let e):
                located.append(nil)
                out.failures.append(e.f == .notFound ? whyNotFound(text, t, scope: scope) : e.f)
            case .success(let ls):
                if ls.contains(where: { owner[$0.index] != nil }) { located.append(nil); out.failures.append(.ambiguous); continue }
                ls.forEach { owner[$0.index] = ti }
                located.append(ls)
            }
        }
        var noteID: Int?
        if case .annotate = op {
            noteID = (DocNoteDefs.ids(text) + DocNoteDefs.refs(text)).max().map { $0 + 1 } ?? 1
            if !out.failures.isEmpty { return Outcome(text: nil, done: 0, failures: out.failures) }
        }
        // 批注号挂在最后一项的最后一处
        let notePart: (Int, Int)? = {
            guard noteID != nil, let ti = targets.indices.last, let pi = targets[ti].parts.indices.last else { return nil }
            return (ti, pi)
        }()
        if noteID != nil, notePart == nil { return Outcome(text: nil, done: 0, failures: [.notFound]) }

        var edits: [(range: NSRange, text: String)] = []
        for (ti, t) in targets.enumerated() {
            guard let ls = located[ti] else { continue }
            // 同一个文件行的几段合在一起改
            var byLine: [Int: [Part]] = [:]
            for p in t.parts { byLine[p.line, default: []].append(p) }
            var changed = false, failed = false
            for (li, ps) in byLine.sorted(by: { $0.key < $1.key }) {
                guard li < ls.count else { out.failures.append(.layout); failed = true; continue }
                let l = ls[li]
                let c = ns.substring(with: l.content)
                guard let tr = traced(c, tidy: scope.tidy, render: render) else { out.failures.append(.layout); failed = true; continue }
                var src: [NSRange] = [], sels: [NSRange] = [], bad = false
                for p in ps {
                    guard let sel = shift(p, to: tr.shown.text) else { bad = true; break }
                    sels.append(sel)
                    src += sourceRanges(sel, shown: tr.shown.text, map: tr.map)
                }
                if bad { out.failures.append(.layout); failed = true; continue }
                if src.isEmpty { continue }
                let new: String?
                if op == .unmark {
                    new = unmarkLine(c, src)
                    if new == nil { continue }
                } else {
                    let pc = pieces(c, src)
                    if pc.refused || pc.pieces.isEmpty { out.failures.append(.refused); failed = true; continue }
                    let isNote = notePart.map { $0.0 == ti && ps.contains(t.parts[$0.1]) } ?? false
                    guard let w = wrapLine(c, pc.pieces, note: isNote ? noteID : nil), !(w.refused && w.text == c) else {
                        out.failures.append(.refused); failed = true; continue
                    }
                    new = w.text == c ? nil : w.text
                }
                guard let new else { changed = true; continue }   // 已经整个划过：算成
                guard verify(c, new, sels: sels, before: tr, tidy: scope.tidy, unmark: op == .unmark, render: render) else {
                    out.failures.append(.layout); failed = true; continue
                }
                edits.append((l.content, new)); changed = true
            }
            if changed && !failed { out.done += 1 }
            else if op == .unmark, !changed, !failed { out.failures.append(.notFound) }
        }
        if case .annotate = op, !out.failures.isEmpty { return Outcome(text: nil, done: 0, failures: out.failures) }
        guard !edits.isEmpty else { return out }
        var s = text
        for e in edits.sorted(by: { $0.range.location > $1.range.location }) {
            s = (s as NSString).replacingCharacters(in: e.range, with: e.text)
        }
        if case .annotate(let note) = op, let id = noteID {
            guard s.contains("[^note-\(id)]") else { return Outcome(text: nil, done: 0, failures: [.refused]) }
            let body = note.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: #"\s*\n\s*"#, with: " ", options: .regularExpression)
            let nl = text.contains("\r\n") ? "\r\n" : "\n"
            s = s.trimmingCharacters(in: .newlines) + nl
            if DocNoteDefs.ids(s).isEmpty { s += nl }
            s += "[^note-\(id)]: \(body)" + nl
            out.noteID = id
        }
        out.text = s
        return out
    }

    /// 改完的一行再排一遍核对：显示的字（去掉细空格、批注气泡）没变；划线时选中的字都有底色（两头的「=」、空白、批注气泡除外），
    /// 新多出来的底色只落在改动的那一截原文里；取消时选中的字都没了底色
    static func verify(_ old: String, _ new: String, sels: [NSRange], before: (shown: Rendered, map: [Int?]), tidy: Bool, unmark: Bool,
                       render: (String) -> Rendered) -> Bool {
        guard let after = traced(new, tidy: tidy, render: render) else { return false }
        let thin = MarkRun.thin.utf16.first!
        let fb = Array(before.shown.text.utf16), fa = Array(after.shown.text.utf16)
        // 去掉细空格和批注气泡（批注会多出一个）后逐字比较，记下对应位置
        let glyph = InlineSource.noteGlyph.utf16.first!
        var ib: [Int] = [], ia: [Int] = []
        for (k, c) in fb.enumerated() where c != thin && c != glyph { ib.append(k) }
        for (k, c) in fa.enumerated() where c != thin && c != glyph { ia.append(k) }
        guard ib.count == ia.count, zip(ib, ia).allSatisfy({ fb[$0] == fa[$1] }) else { return false }
        var posA: [Int: Int] = [:]
        for (x, y) in zip(ib, ia) { posA[x] = y }
        // 改动的那一截（原文下标，按旧行算）
        let ou = Array(old.utf16), nu = Array(new.utf16)
        var p = 0
        while p < ou.count, p < nu.count, ou[p] == nu[p] { p += 1 }
        var q = 0
        while q < ou.count - p, q < nu.count - p, ou[ou.count - 1 - q] == nu[nu.count - 1 - q] { q += 1 }
        let changed = NSRange(location: p, length: max(0, ou.count - q - p))
        let skip: Set<UInt16> = MarkRule.trimChars.union([thin, InlineSource.noteGlyph.utf16.first!])
        for sel in sels {
            for k in max(0, sel.location)..<min(fb.count, NSMaxRange(sel)) where !skip.contains(fb[k]) {
                guard let y = posA[k] else { continue }
                if unmark ? after.shown.marked[y] : !after.shown.marked[y] { return false }
            }
        }
        if !unmark {
            for (x, y) in posA where after.shown.marked[y] && !before.shown.marked[x] {
                guard let o = before.map[x], o >= changed.location - 2, o < NSMaxRange(changed) + 2 else { return false }
            }
        }
        return true
    }

    // MARK: 读后感一节（DocEdit.reflectionRange 转到这里）

    /// 「## 读后感」这一节在文件里的范围：到下一个「## 」或者第一条批注脚注为止。
    /// 文章自己也可能有叫「读后感」的小标题：只认「阅读原文」后面的最后一个，免得把正文当成读后感、保存时删掉
    static func reflectionRange(_ text: String) -> Range<String.Index>? {
        let from = text.range(of: "\n[阅读原文](", options: .backwards)?.upperBound ?? text.startIndex
        var start: Range<String.Index>?
        var at = from
        while let r = text.range(of: #"(?m)^## 读后感[ \t]*$"#, options: .regularExpression, range: at..<text.endIndex) {
            start = r; at = r.upperBound
        }
        guard let start else { return nil }
        let rest = start.upperBound..<text.endIndex
        let next = text.range(of: #"(?m)^## "#, options: .regularExpression, range: rest)?.lowerBound
        let def = text.range(of: #"(?m)^\[\^note-\d+\]:"#, options: .regularExpression, range: rest)?.lowerBound
        let end = [next, def].compactMap { $0 }.min() ?? text.endIndex
        return start.lowerBound..<end
    }
}

/// 文末批注定义的编号（「[^note-3]: …」），新批注取最大的 + 1
enum DocNoteDefs {
    /// 正文里用到的批注号（「[^note-3]」，不含定义行）
    static func refs(_ text: String) -> [Int] {
        let ns = text as NSString
        return (try! NSRegularExpression(pattern: #"\[\^note-(\d+)\](?!:)"#))
            .matches(in: text, range: NSRange(location: 0, length: ns.length))
            .compactMap { Int(ns.substring(with: $0.range(at: 1))) }
    }

    static func ids(_ text: String) -> [Int] {
        let ns = text as NSString
        return (try! NSRegularExpression(pattern: #"(?m)^\[\^note-(\d+)\]:"#))
            .matches(in: text, range: NSRange(location: 0, length: ns.length))
            .compactMap { Int(ns.substring(with: $0.range(at: 1))) }
    }
}
