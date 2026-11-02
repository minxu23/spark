import SwiftUI

/// 够用的 Markdown：标题、段落、列表、引用、代码块、表格（按等宽文字显示）、分隔线。
/// 行内的粗体、斜体、链接、行内代码交给系统的 AttributedString。
enum MDBlock: Hashable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bullet(String, ordered: String?)
    case quote(String)
    case code(String)
    case rule
}

enum Markdown {
    static func parse(_ source: String) -> [MDBlock] {
        var text = source.replacingOccurrences(of: "\r\n", with: "\n")
        // frontmatter
        if text.hasPrefix("---\n"), let end = text.range(of: "\n---\n", range: text.index(text.startIndex, offsetBy: 4)..<text.endIndex) {
            text = String(text[end.upperBound...])
        }
        var blocks: [MDBlock] = []
        var para: [String] = []
        var quote: [String] = []
        var code: [String]? = nil

        func flush() {
            if !para.isEmpty { blocks.append(.paragraph(para.joined(separator: "\n"))); para = [] }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))); quote = [] }
        }

        for raw in text.components(separatedBy: "\n") {
            if code != nil {
                if raw.hasPrefix("```") { blocks.append(.code(code!.joined(separator: "\n"))); code = nil }
                else { code!.append(raw) }
                continue
            }
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") { flush(); code = []; continue }
            if line.isEmpty { flush(); continue }
            if line == "---" || line == "***" { flush(); blocks.append(.rule); continue }
            if let m = line.range(of: #"^#{1,6} "#, options: .regularExpression) {
                flush()
                blocks.append(.heading(level: line[m].count - 1, text: String(line[m.upperBound...])))
                continue
            }
            if line.hasPrefix(">") {
                if !para.isEmpty { flush() }
                quote.append(String(line.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            if let m = line.range(of: #"^[-*+] "#, options: .regularExpression) {
                flush()
                blocks.append(.bullet(String(line[m.upperBound...]), ordered: nil))
                continue
            }
            if let m = line.range(of: #"^\d{1,3}[.)] "#, options: .regularExpression) {
                flush()
                blocks.append(.bullet(String(line[m.upperBound...]), ordered: String(line[m].dropLast())))
                continue
            }
            if !quote.isEmpty { flush() }
            // 行尾两个空格的硬换行、普通换行都当换行（中文不靠空格分词）
            para.append(raw.hasSuffix("  ") ? String(raw.dropLast(2)) : raw)
        }
        if let code { blocks.append(.code(code.joined(separator: "\n"))) }
        flush()
        return blocks
    }

    /// 整理稿、文字记录、单集笔记开头的「# 标题」和「- 所属节目：…」那一串说明：页面上已经有了，去掉
    static func stripPreamble(_ blocks: [MDBlock]) -> [MDBlock] {
        var out = blocks
        if case .heading(1, _) = out.first { out.removeFirst() }
        while let first = out.first, case .bullet(let t, nil) = first,
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

    static func inline(_ s: String) -> AttributedString {
        var t = curlyQuotes(s).replacingOccurrences(of: "==", with: "")
        // Obsidian 的 [[链接|显示]] 只留显示的字
        t = t.replacingOccurrences(of: #"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]"#, with: "$1", options: .regularExpression)
        // 链接地址里有空格时系统不认，换成 %20
        t = t.replacingOccurrences(of: #"\]\(<([^>]*)>\)"#, with: "]($1)", options: .regularExpression)
        if let a = try? AttributedString(markdown: t, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) {
            return a
        }
        return AttributedString(t)
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

    var body: some View {
        LazyVStack(alignment: .leading, spacing: prefs.size * 0.9) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { i, block in
                view(block).id(i)
            }
        }
        .foregroundStyle(p.ink)
    }

    @ViewBuilder
    private func view(_ block: MDBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Markdown.inline(text))
                .font(.heading(level <= 2 ? prefs.size * 1.15 : prefs.size * 1.0))
                .padding(.top, level <= 2 ? prefs.size * 0.6 : prefs.size * 0.2)
                .accessibilityAddTraits(.isHeader)
        case .paragraph(let text):
            Text(Markdown.inline(text))
                .font(prefs.body())
                .lineSpacing(prefs.lineSpacing)
                .textSelection(.enabled)
        case .bullet(let text, let ordered):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(ordered.map { "\($0)." } ?? "•").font(prefs.body()).foregroundStyle(p.ink2)
                Text(Markdown.inline(text)).font(prefs.body()).lineSpacing(prefs.lineSpacing).textSelection(.enabled)
            }
        case .quote(let text):
            Text(Markdown.inline(text))
                .font(prefs.body(0.95))
                .lineSpacing(prefs.lineSpacing)
                .foregroundStyle(p.ink2)
                .padding(.leading, 14)
                .overlay(alignment: .leading) { Rectangle().fill(p.rule).frame(width: 2) }
        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text).font(.system(size: prefs.size * 0.78, design: .monospaced)).padding(12)
            }
            .background(p.paper2, in: RoundedRectangle(cornerRadius: 8))
        case .rule:
            Rectangle().fill(p.rule).frame(height: 1)
        }
    }
}
