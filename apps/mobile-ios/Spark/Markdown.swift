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
            if line.hasPrefix("```") { flush(); code = []; codeLang = line.dropFirst(3).trimmingCharacters(in: .whitespaces).lowercased(); continue }
            if line.isEmpty { flush(); continue }
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
                blocks.append(.bullet(String(line[m.upperBound...]), ordered: number.map { String(line[$0].dropLast()) },
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
        return blocks
    }

    /// 「**为什么叫“帕西尼”**」这种整行加粗的短句：返回去掉星号的标题，不是就返回 nil
    static func subheading(_ line: String) -> String? {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.count <= 44, t.hasPrefix("**"), t.hasSuffix("**"),
              let m = t.range(of: #"^\*\*([^*]+)\*\*$"#, options: .regularExpression) else { return nil }
        let inner = String(t[m]).dropFirst(2).dropLast(2).trimmingCharacters(in: .whitespaces)
        guard !inner.isEmpty, !inner.hasSuffix("："), !inner.hasSuffix(":") else { return nil }
        return inner.hasSuffix("。") ? String(inner.dropLast()) : inner
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

    /// 中文句子里的直引号 "…" 换成弯引号“…”（模型写小结时常用英文引号，夹在宋体、楷体里很扎眼）
    static func curlyQuotes(_ s: String) -> String {
        guard s.contains("\""), s.range(of: #"\p{Han}"#, options: .regularExpression) != nil,
              !s.contains("](") else { return s }
        var out = "", open = true
        for ch in s {
            if ch == "\"" { out.append(open ? "“" : "”"); open.toggle() } else { out.append(ch) }
        }
        return open ? out : s   // 引号不成对就不动
    }

    /// 英文里的弯引号换成直引号。霞鹜文楷、思源黑体这类中文字体把 ’ ‘ “ ” 画成全角，
    /// 「Hip Hop’s」显示成「Hip Hop’ s」；直引号在中文字体里是半角的。
    /// 单词中间的撇号（Hop’s、don’t）一律换；整段没有汉字（英文原文、英文标题）的引号全换；
    /// 中文句子里的“…”保持全角。
    static func latinQuotes(_ s: String) -> String {
        guard s.contains(where: { "‘’“”".contains($0) }) else { return s }
        var t = s.replacingOccurrences(of: #"(?<=[A-Za-z0-9])[’‘](?=[A-Za-z])"#, with: "'", options: .regularExpression)
        if t.range(of: #"\p{Han}"#, options: .regularExpression) == nil {
            t = t.replacingOccurrences(of: #"[“”]"#, with: "\"", options: .regularExpression)
                .replacingOccurrences(of: #"[‘’]"#, with: "'", options: .regularExpression)
        }
        return t
    }

    static func inline(_ s: String) -> AttributedString {
        // ==划的重点==：先换成占位符，最后铺底色
        var t = latinQuotes(curlyQuotes(s)).replacingOccurrences(of: #"==([^=\n]+)=="#, with: "\u{E002}$1\u{E003}", options: .regularExpression)
            .replacingOccurrences(of: "==", with: "")
            .replacingOccurrences(of: #"<br\s*/?>"#, with: "\n", options: .regularExpression)
        // Obsidian 的 [[链接|显示]] 只留显示的字
        t = t.replacingOccurrences(of: #"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]"#, with: "$1", options: .regularExpression)
        t = links(t)
        // 「**拉姆：**你错了」这种：收尾的 ** 前面是标点、后面紧跟汉字，标准 Markdown 不认作加粗，先自己标出来
        t = t.replacingOccurrences(of: #"\*\*([^*\n]+?)\*\*"#, with: "\u{E000}$1\u{E001}", options: .regularExpression)
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

    /// 网址链接留着能点（地址里的空格换成 %20 系统才认）；指向笔记库里别的文件的相对链接手机上点不开，只留文字
    private static func links(_ s: String) -> String {
        guard s.contains("]("), let re = try? NSRegularExpression(pattern: #"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:<([^>]*)>|([^)]*))\)"#) else { return s }
        let ns = s as NSString, out = NSMutableString(string: s)
        for m in re.matches(in: s, range: NSRange(location: 0, length: ns.length)).reversed() {
            let text = ns.substring(with: m.range(at: 1))
            let dest = ns.substring(with: m.range(at: 2).location != NSNotFound ? m.range(at: 2) : m.range(at: 3))
            if dest.range(of: #"^(https?|mailto):"#, options: .regularExpression) != nil {
                let escaped = text.replacingOccurrences(of: "[", with: "\\[").replacingOccurrences(of: "]", with: "\\]")
                out.replaceCharacters(in: m.range, with: "[\(escaped)](\(dest.replacingOccurrences(of: " ", with: "%20")))")
            } else {
                out.replaceCharacters(in: m.range, with: text)
            }
        }
        return out as String
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
            default: break
            }
        }
        return out
    }

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
    static func filter(_ blocks: [MDBlock], _ lang: SpeechLang) -> [MDBlock] {
        guard lang != .both else { return blocks }
        let paired = pairs(blocks)
        var out: [MDBlock] = []
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
                    out += inner
                } else if case .bullet(_, let ordered, let depth) = blocks[i] {
                    out.append(.bullet(tr, ordered: ordered, depth: depth))
                } else {
                    out.append(.paragraph(tr))
                }
                i += 2
            } else {
                out.append(blocks[i])
                i += 1
            }
        }
        return out
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
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.mdEdit) private var edit

    var body: some View {
        // 带公式的文章整篇交给网页排（MathArticle）
        if MathHTML.hasMath(blocks) {
            MathArticle(blocks: blocks)
        } else {
            LazyVStack(alignment: .leading, spacing: prefs.paraSpacing) {
                ForEach(Array(blocks.enumerated()), id: \.offset) { i, block in
                    view(block).id(i)
                }
            }
            .foregroundStyle(p.ink)
        }
    }

    @ViewBuilder
    private func view(_ block: MDBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Markdown.inline(text))
                .font(prefs.heading(level))
                .foregroundStyle(level <= 3 ? prefs.headingTint(p) : p.ink)
                .lineSpacing(prefs.lineSpacing * 0.6)
                .padding(.top, level <= 2 ? prefs.size * 0.6 : prefs.size * 0.2)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let text):
            Text(Markdown.inline(text))
                .font(prefs.body())
                .lineSpacing(prefs.lineSpacing)
                .textSelection(.enabled)
                .modifier(Markable(text: text, tap: edit?.tapBlock))
        case .bullet(let text, let ordered, let depth):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(ordered.map { "\($0)." } ?? (depth == 0 ? "•" : "◦")).font(prefs.body()).foregroundStyle(p.ink2)
                Text(Markdown.inline(text)).font(prefs.body()).lineSpacing(prefs.lineSpacing).textSelection(.enabled)
            }
            .padding(.leading, Double(depth) * prefs.size * 1.2)
            .modifier(Markable(text: text, tap: edit?.tapBlock))
        case .quote(let text):
            Group {
                // 引用里本身有标题、列表（中英对照稿的译文常这样）：照样排，不露出 # 和 -
                let inner = Markdown.parse(text)
                if inner.count == 1, case .paragraph = inner[0] {
                    Text(Markdown.inline(text)).font(prefs.body(0.95)).lineSpacing(prefs.lineSpacing)
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
        }
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

/// 划重点模式下，点一段选这一段。平时不加手势，文字里的链接照常能点
struct Markable: ViewModifier {
    let text: String
    let tap: ((String) -> Void)?

    func body(content: Content) -> some View {
        if let tap {
            content.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle()).onTapGesture { tap(text) }
        } else {
            content
        }
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
                        if let edit {
                            Button("删除这张图", systemImage: "trash", role: .destructive) { edit.removeImage(src) }
                        }
                    }
            } else if failed {
                VStack(spacing: 8) {
                    Label(alt.isEmpty ? "图片没有下载下来" : alt, systemImage: "photo").font(.system(size: 13)).foregroundStyle(p.ink2)
                    if let edit {
                        HStack(spacing: 18) {
                            Button(retrying ? "正在下载…" : "重新下载") { Task { await retry(edit) } }.disabled(retrying)
                            Button("删除这张图", role: .destructive) { edit.removeImage(src) }
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

    private func retry(_ edit: MDEdit) async {
        retrying = true
        defer { retrying = false }
        note = await edit.retryImage(src)
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
            guard let u = URL(string: src), let (data, _) = try? await URLSession.shared.data(from: u), let img = UIImage(data: data)
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
