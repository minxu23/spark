import SwiftUI

/// 够用的 Markdown：标题、段落、列表、引用、代码块、表格、分隔线。
/// 行内的粗体、斜体、链接、行内代码交给系统的 AttributedString。
enum MDBlock: Hashable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bullet(String, ordered: String?, depth: Int)
    case quote(String)
    case code(String)
    case table(header: [String], rows: [[String]])
    case rule
    /// 单独一行的图片（剪藏的文章）：地址相对这篇文章所在的文件夹，或者网址
    case image(alt: String, src: String)
    /// 图注：紧跟在图片后面的一行短字，或「……丨来源」这种写法
    case caption(String)
}

enum Markdown {
    static func parse(_ source: String) -> [MDBlock] {
        var text = source.replacingOccurrences(of: "\r\n", with: "\n")
        // frontmatter
        if text.hasPrefix("---\n"), let end = text.range(of: "\n---\n", range: text.index(text.startIndex, offsetBy: 4)..<text.endIndex) {
            text = String(text[end.upperBound...])
        }
        // <!-- spark-topic-summary --> 这类给电脑端定位用的注释，Obsidian 里看不见，手机上也不显示
        text = text.replacingOccurrences(of: #"<!--[\s\S]*?-->"#, with: "", options: .regularExpression)
        var blocks: [MDBlock] = []
        var listIndents: [Int] = []   // 当前这串列表每一层的缩进宽度，用来算嵌套深度
        var para: [String] = []
        var quote: [String] = []
        var code: [String]? = nil
        var codeLang = ""
        var table: [String] = []

        func flush() {
            if !para.isEmpty {
                // 单独一行、整行加粗的短句是章节小标题（整理稿里常这样写）；冒号结尾的是说话人、引出下文的标签，不算
                if para.count == 1, let t = subheading(para[0]) { blocks.append(.heading(level: 3, text: t)) }
                else { blocks.append(.paragraph(para.joined(separator: "\n"))) }
                para = []
            }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))); quote = [] }
            if !table.isEmpty {
                // 第二行是 |---|---| 才算表格，不然原样当段落
                if table.count >= 2, table[1].range(of: #"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$"#, options: .regularExpression) != nil {
                    let rows = table.dropFirst(2).map(cells).filter { $0.contains { !$0.isEmpty } }
                    blocks.append(.table(header: cells(table[0]), rows: Array(rows)))
                } else {
                    blocks.append(.paragraph(table.joined(separator: "\n")))
                }
                table = []
            }
        }

        for raw in text.components(separatedBy: "\n") {
            if code != nil {
                if raw.hasPrefix("```") {
                    // 报告附录里「```markdown」包着的成稿：照 Markdown 排出来，不当代码
                    if codeLang == "markdown" || codeLang == "md" { blocks += parse(code!.joined(separator: "\n")) }
                    else { blocks.append(.code(code!.joined(separator: "\n"))) }
                    code = nil
                }
                else { code!.append(raw) }
                continue
            }
            let line = raw.trimmingCharacters(in: .whitespaces)
            // 文末的批注（脚注定义）不排进正文，点 ✎ 时再看
            if line.hasPrefix("[^note-"), line.range(of: #"^\[\^note-\d+\]:"#, options: .regularExpression) != nil { flush(); continue }
            if line.hasPrefix("```") { flush(); code = []; codeLang = line.dropFirst(3).trimmingCharacters(in: .whitespaces).lowercased(); continue }
            // 空行的判断跟划线定位（MarkAnchor.scan）用同一个函数
            if MarkAnchor.blank(raw) { flush(); continue }
            if line.hasPrefix("|") {
                if table.isEmpty { flush() }
                table.append(line)
                continue
            }
            if !table.isEmpty { flush() }
            if line == "---" || line == "***" { flush(); blocks.append(.rule); continue }
            if line.hasPrefix("!["), let m = line.range(of: #"^!\[([^\]]*)\]\(<?([^)>\s]+)>?\)$"#, options: .regularExpression) {
                flush()
                let s = String(line[m])
                let alt = s.dropFirst(2).prefix { $0 != "]" }
                let src = s[s.index(s.firstIndex(of: "(")!, offsetBy: 1)...].dropLast().trimmingCharacters(in: CharacterSet(charactersIn: "<>"))
                blocks.append(.image(alt: String(alt), src: src))
                continue
            }
            // 报告末尾折叠起来的「<details><summary>…</summary>」：手机上不折叠，标签去掉、标题留下。
            // 单独一行的 <br>、模型漏出来的 <think></think> 也去掉
            if line.range(of: #"^</?(details|think)>$|^<br\s*/?>$"#, options: .regularExpression) != nil { flush(); continue }
            if let m = line.range(of: #"^<summary>(.*)</summary>$"#, options: .regularExpression) {
                flush()
                blocks.append(.heading(level: 3, text: String(line[m]).replacingOccurrences(of: #"</?summary>"#, with: "", options: .regularExpression)))
                continue
            }
            if let m = line.range(of: #"^#{1,6} "#, options: .regularExpression) {
                flush()
                blocks.append(.heading(level: line[m].count - 1, text: String(line[m.upperBound...])))
                continue
            }
            if line.hasPrefix(">") {
                if !para.isEmpty { flush() }
                listIndents = []
                var q = String(line.dropFirst()).trimmingCharacters(in: .whitespaces)
                // Obsidian 的提示框「> [!abstract] 标题」：类型标记去掉，标题加粗
                if quote.isEmpty, let m = q.range(of: #"^\[![A-Za-z-]+\][+-]?\s*"#, options: .regularExpression) {
                    let title = q[m.upperBound...]
                    if title.isEmpty { continue }
                    q = "**\(title)**"
                }
                quote.append(q)
                continue
            }
            let bullet = line.range(of: #"^[-*+] "#, options: .regularExpression)
            let number = bullet == nil ? line.range(of: #"^\d{1,3}[.)] "#, options: .regularExpression) : nil
            if let m = bullet ?? number {
                flush()
                // 缩进（Tab 算 4 格）比上一层深就进一层，回到哪层的缩进就回到哪层
                let indent = raw.prefix { $0 == " " || $0 == "\t" }.reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
                while let last = listIndents.last, last > indent { listIndents.removeLast() }
                if listIndents.last != indent { listIndents.append(indent) }
                blocks.append(.bullet(String(line[m.upperBound...]), ordered: number.map { String(line[$0].dropLast(2)) },
                                      depth: min(listIndents.count - 1, 3)))
                continue
            }
            listIndents = []
            if !quote.isEmpty { flush() }
            // 行尾两个空格的硬换行、普通换行都当换行（中文不靠空格分词）
            para.append(raw.hasSuffix("  ") ? String(raw.dropLast(2)) : raw)
        }
        if let code { blocks.append(.code(code.joined(separator: "\n"))) }
        flush()
        return captions(blocks)
    }

    /// 图片下面的短句排成图注。中英对照稿里后面跟着译文的不算（那是要对照的一段）
    static func captions(_ blocks: [MDBlock]) -> [MDBlock] {
        var out = blocks
        for i in out.indices {
            // 划线、批注号不算：划过线的一句和没划的排法一样（不然划一下，正文段会变成图注）
            guard case .paragraph(let raw) = out[i], case let t = unmarked(raw), !t.contains("\n"), t.count <= 80 else { continue }
            if i + 1 < out.count, case .quote = out[i + 1] { continue }
            let afterImage = i > 0 && { if case .image = out[i - 1] { return true }; return false }()
            let credited = t.range(of: #"[丨|｜][^丨|｜]{1,24}$"#, options: .regularExpression) != nil
                && t.range(of: #"\]\("#, options: .regularExpression) == nil
            let short = t.count <= 50 && t.range(of: #"[。！？!?：:]$"#, options: .regularExpression) == nil
            if credited || (afterImage && short) { out[i] = .caption(t) }
        }
        return out
    }

    /// 「**为什么叫“帕西尼”**」这种整行加粗的短句：返回去掉星号的标题，不是就返回 nil
    static func subheading(_ line: String) -> String? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.count <= 44, t.hasPrefix("**"), t.hasSuffix("**"),
              let m = t.range(of: #"^\*\*([^*]+)\*\*$"#, options: .regularExpression) else { return nil }
        let inner = String(t[m]).dropFirst(2).dropLast(2).trimmingCharacters(in: .whitespaces)
        // 冒号结尾的看划线去掉以后（「**==说话人：==**」划过线也还是说话人标签，不变成小标题）
        let plain = unmarked(inner)
        guard !plain.isEmpty, !plain.hasSuffix("："), !plain.hasSuffix(":") else { return nil }
        return inner.hasSuffix("。") ? String(inner.dropLast()) : inner
    }

    /// 去掉划线记号和批注号后的字：判断一块是什么样子（图注、小标题）时不看这些，划了线不会改变排法
    static func unmarked(_ s: String) -> String {
        s.replacingOccurrences(of: MarkRule.pattern, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"\[\^note-\d+\]"#, with: "", options: .regularExpression)
    }

    /// 表格一行拆成格子。Obsidian 的 [[笔记|显示名]] 里的竖线、转义的 \| 不算分隔
    static func cells(_ line: String) -> [String] {
        var out: [String] = [], cur = "", depth = 0
        var chars = Array(line.trimmingCharacters(in: .whitespaces))
        if chars.first == "|" { chars.removeFirst() }
        if chars.last == "|", chars.dropLast().last != "\\" { chars.removeLast() }
        var i = 0
        while i < chars.count {
            let c = chars[i]
            if c == "\\", i + 1 < chars.count, chars[i + 1] == "|" { cur.append("|"); i += 2; continue }
            if c == "[", i + 1 < chars.count, chars[i + 1] == "[" { depth += 1 }
            if c == "]", i + 1 < chars.count, chars[i + 1] == "]", depth > 0 { depth -= 1 }
            if c == "|", depth == 0 { out.append(cur.trimmingCharacters(in: .whitespaces)); cur = "" } else { cur.append(c) }
            i += 1
        }
        out.append(cur.trimmingCharacters(in: .whitespaces))
        return out
    }

    /// 整理稿、文字记录、单集笔记开头的「# 标题」和「- 所属节目：…」那一串说明：页面上已经有了，去掉
    static func stripPreamble(_ blocks: [MDBlock]) -> [MDBlock] {
        var out = blocks
        if case .heading(1, _) = out.first { out.removeFirst() }
        while let first = out.first, case .bullet(let t, nil, _) = first,
              t.range(of: #"^[^：:]{1,12}[：:]"#, options: .regularExpression) != nil {
            out.removeFirst()
        }
        // 紧跟着的「## 演讲稿」「## 文字记录」「### 内容总结」这类总标题：页面上的切换已经说明了是哪种稿子
        if case .heading(_, let t) = out.first, ["演讲稿", "文字记录", "整理稿", "正文", "内容总结", "大会总结", "节目总结"].contains(t) {
            out.removeFirst()
        }
        return out
    }

    /// 中文句子里的直引号换成弯引号（规则在 InlineSource.curlyQuotes，划线定位也用它）
    static func curlyQuotes(_ s: String) -> String { InlineSource.curlyQuotes(s) }

    /// 英文里的弯引号换成直引号（规则在 InlineSource.latinQuotes）
    static func latinQuotes(_ s: String) -> String { InlineSource.latinQuotes(s) }

    /// 批注号的链接：spark-note:3
    static let noteScheme = "spark-note"

    /// 一行 Markdown 排成带样式的字。交给系统排版之前的几步改写（引号、划线、<br>、双链、链接、批注号、加粗）在
    /// InlineSource.prepare：划线定位（MarkAnchor）用同一份代码求「每个显示的字是原文第几个字」，两边不会改岔
    static func inline(_ s: String) -> AttributedString {
        let t = InlineSource.prepare(.init(s)).string
        var a = (try? AttributedString(markdown: t, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(t)
        while let open = a.range(of: "\u{E000}") {
            if let close = a[open.upperBound...].range(of: "\u{E001}") {
                a[open.upperBound..<close.lowerBound].inlinePresentationIntent = .stronglyEmphasized
                a.removeSubrange(close)
            }
            if let open = a.range(of: "\u{E000}") { a.removeSubrange(open) }
        }
        while let open = a.range(of: "\u{E002}") {
            if let close = a[open.upperBound...].range(of: "\u{E003}") {
                a[open.upperBound..<close.lowerBound].backgroundColor = Color.yellow.opacity(0.35)
                a.removeSubrange(close)
            }
            if let open = a.range(of: "\u{E002}") { a.removeSubrange(open) }
        }
        return a
    }

    /// 页面上一行显示出来的字（批注号两边垫细空格，跟 MarkableText 一样，拼法在 MarkRun.layoutText）和每个字有没有划线底色。
    /// 划线定位用它核对「文件里那一行排出来的字」和「页面上这一行的字」一样，再把选中的字换回原文
    static func displayed(_ s: String) -> MarkAnchor.Rendered {
        let a = inline(s)
        let runs = a.runs.map { r -> (text: String, note: Bool, mark: Bool) in
            (String(a[r.range].characters), r.link?.scheme == noteScheme, r.backgroundColor != nil)
        }
        let lay = MarkRun.layoutText([(marker: nil, runs: runs.map { (text: $0.text, note: $0.note) })], thin: MarkRun.thin)
        var marked: [Bool] = []
        for r in runs {
            let n = r.text.utf16.count + (r.note ? 2 : 0)
            marked += Array(repeating: r.mark && !r.note, count: n)
        }
        return MarkAnchor.Rendered(text: lay.text, marked: marked)
    }

    /// 整理稿的语言：中英对照的整理稿里每段是原文、后面紧跟一段「> 译文」（电脑端 bilingual 模式）
    enum SpeechLang: String, CaseIterable, Identifiable {
        case zh, en, both
        var id: String { rawValue }
        var label: String {
            switch self {
            case .zh: "中文"
            case .en: "英文"
            case .both: "对照"
            }
        }
    }

    /// 原文段落（或列表项）后面紧跟一段引用（译文）的位置
    private static func pairs(_ blocks: [MDBlock]) -> Set<Int> {
        var out = Set<Int>()
        for i in blocks.indices.dropLast() {
            guard case .quote(let tr) = blocks[i + 1] else { continue }
            switch blocks[i] {
            case .paragraph, .bullet: out.insert(i)
            // 「## Defining…」后面跟「> ## 定义…」：标题的译文
            case .heading where tr.hasPrefix("#") || subheading(tr) != nil: out.insert(i)
            // 英文原文本身是引用：后面那段带汉字的引用是它的译文（剪藏逐段翻译）
            case .quote(let orig) where hasHan(tr) && !hasHan(orig): out.insert(i)
            default: break
            }
        }
        return out
    }

    private static func hasHan(_ s: String) -> Bool { s.range(of: #"\p{Han}"#, options: .regularExpression) != nil }

    /// 是不是中英对照的整理稿：至少 3 段配成对，并且占了正文段落的三成以上
    static func isBilingual(_ blocks: [MDBlock]) -> Bool {
        let n = pairs(blocks).count
        let paras = blocks.filter { if case .paragraph = $0 { return true }; return false }.count
        return n >= 3 && Double(n) >= Double(paras) * 0.3
    }

    /// 没有译文的原文段落有几段（电脑上生成整理稿时个别段落会漏翻）：主要是英文字母、后面没跟译文的
    static func untranslated(_ blocks: [MDBlock]) -> Int {
        let paired = pairs(blocks)
        var n = 0
        for (i, b) in blocks.enumerated() where !paired.contains(i) {
            guard case .paragraph(let t) = b, t.count > 40, !t.hasPrefix("$$") else { continue }   // 公式块不用译
            let latin = t.unicodeScalars.filter { $0.isASCII && CharacterSet.letters.contains($0) }.count
            if Double(latin) > Double(t.count) * 0.6 { n += 1 }
        }
        return n
    }

    /// 按选的语言只留一边：中文把译文当正文显示，英文去掉译文，对照原样
    static func filter(_ blocks: [MDBlock], _ lang: SpeechLang) -> [MDBlock] { filtered(blocks, lang).blocks }

    /// 同 filter，另外记下只看中文时当正文段、列表项显示的译文在结果里的下标（文件里这些行带「> 」，划线时要知道）；
    /// listed 是其中文件里那一行本身带列表符号的（「> - 译文」）
    static func filtered(_ blocks: [MDBlock], _ lang: SpeechLang) -> (blocks: [MDBlock], translated: Set<Int>, listed: Set<Int>) {
        guard lang != .both else { return (blocks, [], []) }
        let paired = pairs(blocks)
        var out: [MDBlock] = []
        var translated = Set<Int>(), listed = Set<Int>()
        var i = 0
        while i < blocks.count {
            if paired.contains(i), case .quote(let tr) = blocks[i + 1] {
                let inner = parse(tr)
                if lang == .en {
                    out.append(blocks[i])
                } else if inner.count > 1 || !(inner.first.map { if case .paragraph = $0 { true } else { false } } ?? true) {
                    // 译文里本身是标题、列表：一串英文列表项后面跟一整段「> - …」译文时，前面没配上对的英文项也换掉
                    let items = inner.filter { if case .bullet = $0 { true } else { false } }.count
                    var drop = items - 1
                    while drop > 0, case .bullet = out.last { out.removeLast(); drop -= 1 }
                    for b in inner {
                        if case .paragraph = b { translated.insert(out.count) }
                        else if case .bullet = b { translated.insert(out.count); listed.insert(out.count) }
                        out.append(b)
                    }
                } else if case .bullet(_, let ordered, let depth) = blocks[i] {
                    translated.insert(out.count)
                    out.append(.bullet(tr, ordered: ordered, depth: depth))
                } else if case .quote = blocks[i] {
                    out.append(.quote(tr))   // 原文是引用，只看中文时译文也按引用排
                } else {
                    translated.insert(out.count)
                    out.append(.paragraph(tr))
                }
                i += 2
            } else {
                out.append(blocks[i])
                i += 1
            }
        }
        return (out, translated, listed)
    }

    /// 剪藏的「## 摘要」一节是长文导读（里面有「### 」小标题）时的范围：从「摘要」标题那一块到下一个一二级标题前。
    /// 短文的摘要（一句话加几条要点）没有小标题，返回 nil，照常显示
    static func guideRange(_ blocks: [MDBlock]) -> Range<Int>? {
        guard let s = blocks.firstIndex(where: { if case .heading(2, "摘要") = $0 { return true } else { return false } }) else { return nil }
        let e = blocks[(s + 1)...].firstIndex(where: { if case .heading(let l, _) = $0, l <= 2 { return true } else { return false } }) ?? blocks.count
        let hasSub = blocks[(s + 1)..<e].contains { if case .heading(3, _) = $0 { return true } else { return false } }
        return hasSub ? s..<e : nil
    }

    static func headings(_ blocks: [MDBlock]) -> [(index: Int, level: Int, text: String)] {
        blocks.enumerated().compactMap { i, b in
            if case .heading(let l, let t) = b, l >= 2 { return (i, l, String(inline(t).characters)) }
            return nil
        }
    }
}

struct MarkdownView: View {
    let blocks: [MDBlock]
    /// 只看中文时当正文显示的译文（文件里每行带「> 」）：划线时按引用行到文件里找（见 MarkRun.units）
    var translated: Set<Int> = []
    /// 其中文件里本身是列表项的译文（「> - 译文」）
    var listed: Set<Int> = []
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.mdEdit) private var edit
    @Environment(\.openURL) private var openURL
    /// 长文导读折起来了（只在这一屏记着，不写文件）
    @State private var guideFolded = false

    var body: some View {
        // 带公式的文章整篇交给网页排（MathArticle）
        if MathHTML.hasMath(blocks) {
            MathArticle(blocks: blocks)
        } else {
            LazyVStack(alignment: .leading, spacing: prefs.paraSpacing) {
                let grouped = edit?.mark != nil
                let rows = Self.rows(blocks, grouped: grouped)
                // 每项在整页同样的项里是第几个：文件里同样的字有几处时按它定是哪一处（MarkAnchor 规则 5）
                let ord = grouped ? MarkRun.ordinals(blocks, rows: rows, translated: translated, listed: listed, simpleQuote: Self.simpleQuote) : [:]
                let guide = Markdown.guideRange(blocks)
                let shown = guide.map { g in rows.filter { !guideFolded || !((g.lowerBound + 1)..<g.upperBound).contains($0.lowerBound) } } ?? rows
                ForEach(shown, id: \.lowerBound) { r in
                    Group {
                        if let g = guide, r.lowerBound == g.lowerBound, case .heading(_, let t) = blocks[g.lowerBound] { guideHeading(t) }
                        else if MarkRun.isRun(r, blocks, grouped: grouped, simpleQuote: Self.simpleQuote) { runText(r, ord) }
                        else { view(blocks[r.lowerBound], first: r.lowerBound == 0) }
                    }
                    .id(r.lowerBound)
                }
            }
            .foregroundStyle(p.ink)
        }
    }

    /// 导读的「摘要」标题：点一下折起或展开
    private func guideHeading(_ text: String) -> some View {
        Button { guideFolded.toggle() } label: {
            HStack(spacing: 6) {
                view(.heading(level: 2, text: text), first: false)
                Image(systemName: guideFolded ? "chevron.right" : "chevron.down").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.ink2)
                Spacer(minLength: 0)
            }
        }
        .buttonStyle(.plain)
        .accessibilityHint(guideFolded ? "展开导读" : "折叠导读")
    }

    @ViewBuilder
    private func view(_ block: MDBlock, first: Bool = false) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Markdown.inline(text))
                .font(prefs.heading(level))
                .foregroundStyle(level <= 3 ? prefs.headingTint(p) : p.ink)
                .lineSpacing(prefs.lineSpacing * 0.6)
                // 上方明显比下方宽，标题跟它下面那段贴近（数值见 ReaderSpacing.heading，按正文一行的高度算）
                .padding(.top, ReaderSpacing.heading(level: level, line: prefs.lineStep, para: prefs.paraSpacing, first: first).top)
                .padding(.bottom, ReaderSpacing.heading(level: level, line: prefs.lineStep, para: prefs.paraSpacing, first: first).bottom)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let text):
            bodyText(text, raw: text)
        case .bullet(let text, let ordered, let depth):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(ordered.map { "\($0)." } ?? (depth == 0 ? "•" : "◦")).font(prefs.body()).foregroundStyle(p.ink2)
                bodyText(text, raw: text)
            }
            .padding(.leading, Double(depth) * prefs.size * 1.2)
        case .quote(let text):
            Group {
                // 引用里本身有标题、列表（中英对照稿的译文常这样）：照样排，不露出 # 和 -
                let inner = Markdown.parse(text)
                if inner.count == 1, case .paragraph = inner[0] {
                    // 文件里这一行是「> 译文」，带上前缀才能在文件里找准位置
                    bodyText(text, raw: text.contains("\n") ? text : "> " + text, scale: 0.95, color: p.ink2)
                } else {
                    VStack(alignment: .leading, spacing: prefs.paraSpacing * 0.7) {
                        ForEach(Array(inner.enumerated()), id: \.offset) { _, b in AnyView(view(b)) }
                    }
                }
            }
            .foregroundStyle(p.ink2)
            .padding(.leading, 14)
            .overlay(alignment: .leading) { Rectangle().fill(p.rule).frame(width: 2) }
        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text).font(.system(size: prefs.size * 0.78, design: .monospaced)).padding(12)
            }
            .contentMargins(.horizontal, 0, for: .scrollContent)   // 外面那栏的边距不要再叠一层
            .background(p.paper2, in: RoundedRectangle(cornerRadius: 8))
        case .table(let header, let rows):
            table(header, rows)
        case .rule:
            Rectangle().fill(p.rule).frame(height: 1)
        case .image(let alt, let src):
            MDImage(alt: alt, src: src)
        case .caption(let text):
            Text(Markdown.inline(text))
                .font(prefs.body(0.78))
                .foregroundStyle(p.ink2)
                .lineSpacing(prefs.lineSpacing * 0.5)
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.top, -prefs.paraSpacing * 0.5)
                .textSelection(.enabled)
        }
    }

    /// 一屏一屏排的单位：一般一块一行；能做笔记的文章里，连着的正文段、列表项、简单的引用并成一行
    /// （放进同一个文本框，选字才能跨块）。规则在 MarkRun.rows
    static func rows(_ blocks: [MDBlock], grouped: Bool) -> [Range<Int>] {
        MarkRun.rows(blocks, grouped: grouped, simpleQuote: simpleQuote)
    }

    /// 第 i 块排在哪一行（那一行第一块的下标，也是那一行的 id）
    static func rowStart(_ i: Int, _ blocks: [MDBlock], grouped: Bool) -> Int {
        MarkRun.rowStart(i, blocks, grouped: grouped, simpleQuote: simpleQuote)
    }

    /// 引用里面只有一段话（不是标题、列表这些）：照正文段那样排，能并进文本框
    static func simpleQuote(_ text: String) -> Bool {
        let inner = Markdown.parse(text)
        if inner.count == 1, case .paragraph = inner[0] { return true }
        return false
    }

    /// 连着的几块放进一个文本框：每块的样子（列表符号、缩进、引用的竖线和浅色）由 MarkableText 按 MarkRun.Unit.Style 排。
    /// 回调里是第几项和选中的范围，换成「这一项对应文件里的哪几行（行键）、整页里第几个、每行选中的范围」交给划线、批注、取消划线
    @ViewBuilder
    private func runText(_ r: Range<Int>, _ ord: [MarkRun.Unit.ID: (ordinal: Int, total: Int)]) -> some View {
        let units = MarkRun.units(blocks, r, translated: translated, listed: listed)
        if let edit, let mark = edit.mark {
            let target = { (piece: MarkableText.Piece) -> MarkAnchor.Target in
                MarkRun.target(units[piece.para], ord, display: piece.display, selection: piece.range, render: Markdown.displayed)
            }
            MarkableText(blocks: units.map { .init(text: Markdown.inline($0.text), style: $0.style, block: $0.block, joinsNext: $0.joinsNext) },
                         font: prefs.bodyUIFont,
                         color: UIColor(p.ink), softColor: UIColor(p.ink2), ruleColor: UIColor(p.rule),
                         linkColor: UIColor(p.accent), lineSpacing: prefs.lineSpacing,
                         paraSpacing: max(0, prefs.paraSpacing - prefs.lineSpacing),
                         onMark: { mark($0.map(target)) },
                         onUnmark: { edit.unmark?($0.map(target)) },
                         onAnnotate: edit.annotate.map { f in { f($0.map(target)) } },
                         onOpenURL: { openURL($0) })
        }
    }

    /// 正文一段字（不并进文本框的：里面有标题或列表的引用里的段落、列表项）。这几处在文件里没法按整行定位，只读、能选字拷贝
    @ViewBuilder
    private func bodyText(_ text: String, raw: String, scale: Double = 1, color: Color? = nil) -> some View {
        Text(Markdown.inline(text)).font(prefs.body(scale)).lineSpacing(prefs.lineSpacing).textSelection(.enabled)
    }

    /// 手机屏幕窄，报告里的表格格子又常是一整段话，排成网格挤不下：
    /// 每行排成一张卡片，第一格当小标题，其余各格前面标上表头
    private func table(_ header: [String], _ rows: [[String]]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { r, row in
                VStack(alignment: .leading, spacing: prefs.size * 0.7) {
                    if let first = row.first, !first.isEmpty {
                        Text(Markdown.inline(first)).font(prefs.headingFont(prefs.size * 0.95)).lineSpacing(prefs.lineSpacing * 0.6)
                    }
                    ForEach(Array(row.enumerated().dropFirst()), id: \.offset) { c, cell in
                        if !cell.isEmpty {
                            VStack(alignment: .leading, spacing: prefs.size * 0.25) {
                                if c < header.count, !header[c].isEmpty {
                                    Text(Markdown.inline(header[c])).font(.heading(prefs.size * 0.72, weight: .regular)).foregroundStyle(p.ink2)
                                }
                                Text(Markdown.inline(cell)).font(prefs.body(0.92)).lineSpacing(prefs.lineSpacing)
                            }
                        }
                    }
                }
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, prefs.size * 0.9)
                .overlay(alignment: .top) { if r > 0 { Rectangle().fill(p.rule).frame(height: 1) } }
            }
        }
        .padding(.horizontal, 16)
        .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
    }
}

extension EnvironmentValues {
    /// 正在读的文章所在的文件夹（相对笔记库），图片的相对地址按它找
    @Entry var mdBaseDir: String? = nil
}

/// 文章里的一张图：笔记库里的文件（没下载的先下载），或者网址
struct MDImage: View {
    let alt: String
    let src: String
    @Environment(\.mdBaseDir) private var base
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @Environment(\.mdEdit) private var edit
    @State private var image: UIImage?
    @State private var failed = false
    @State private var retrying = false
    @State private var note: String?

    var body: some View {
        Group {
            if let image, isBanner(image) {
                EmptyView()
            } else if let image {
                Image(uiImage: image).resizable().scaledToFit()
                    .frame(maxWidth: min(image.size.width, .infinity))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .accessibilityLabel(alt.isEmpty ? "图片" : alt)
                    .contextMenu {
                        if let remove = edit?.removeImage {
                            Button("删除这张图", systemImage: "trash", role: .destructive) { remove(src) }
                        }
                    }
            } else if failed {
                VStack(spacing: 8) {
                    Label(alt.isEmpty ? "图片没有下载下来" : alt, systemImage: "photo").font(.system(size: 13)).foregroundStyle(p.ink2)
                    if let retryImage = edit?.retryImage, let remove = edit?.removeImage {
                        HStack(spacing: 18) {
                            Button(retrying ? "正在下载…" : "重新下载") { Task { await retry(retryImage) } }.disabled(retrying)
                            Button("删除这张图", role: .destructive) { remove(src) }
                        }
                        .font(.system(size: 14)).buttonStyle(.borderless)
                    }
                    if let note { Text(note).font(.system(size: 12.5)).foregroundStyle(.red) }
                }
                .padding(.vertical, 10).frame(maxWidth: .infinity)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 6))
            } else {
                RoundedRectangle(cornerRadius: 6).fill(p.paper2).frame(height: 160)
            }
        }
        .frame(maxWidth: .infinity)
        .task(id: src) { await load() }
    }

    private func retry(_ retryImage: (String) async -> String?) async {
        retrying = true
        defer { retrying = false }
        note = await retryImage(src)
        if note == nil {
            failed = false
            await load()
        }
    }

    /// 剪藏里加这个功能之前存下的：引导关注一类的扁长条横幅，读的时候不显示（新存的剪藏抓的时候就去掉了，见 Clipper.dropAds）
    private func isBanner(_ img: UIImage) -> Bool {
        (base ?? "").hasPrefix("Spark/剪藏") && img.size.height > 0 && img.size.width / img.size.height > 3.4
    }

    private func load() async {
        if src.hasPrefix("http://") || src.hasPrefix("https://") {
            guard !NetGate.blocked, let u = URL(string: src), let (data, _) = try? await URLSession.shared.data(from: u), let img = UIImage(data: data)
            else { failed = true; return }
            image = img
            return
        }
        guard let base else { failed = true; return }
        // 相对路径：拼上文章所在的文件夹，处理 ../
        var parts = base.split(separator: "/").map(String.init)
        for piece in src.removingPercentEncoding?.split(separator: "/") ?? [] {
            if piece == ".." { _ = parts.popLast() } else if piece != "." { parts.append(String(piece)) }
        }
        guard let url = await vault.fileURL(forDownloading: parts.joined(separator: "/")),
              let img = UIImage(contentsOfFile: url.path) else { failed = true; return }
        image = img
    }
}
