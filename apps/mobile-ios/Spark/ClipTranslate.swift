import Foundation

/// 全英文的剪藏逐段翻译成中文：每段原文后面跟一段「> 译文」，跟电脑上中英对照的整理稿同一种写法，
/// 阅读时用 Markdown.filter 切换中文、英文、对照。图片、代码、表格、单独成行的公式不动。
enum ClipTranslate {
    static let batchChars = 6000
    static let prompt = """
    把下面编号的英文段落翻译成中文，语言自然、准确，像中文作者写的，不要逐词硬译。人名、公司名、产品名和业内通用术语可以保留英文。保留行内的粗体和链接。

    严格按这个格式输出，每段一行，不要合并、不要漏段、不要解释：
    [编号] 译文

    ===== 原文 =====
    {text}

    """

    /// 中文占比很低、字数够的才算全英文（公众号里夹英文的文章不算）
    static func needed(_ markdown: String) -> Bool {
        let t = RX.sub(#"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)"#, "", markdown)
        return t.count >= 200 && EpisodeRunner.cjkShare(t) < 0.1
    }

    /// 已经是对照稿了
    static func isBilingual(_ markdown: String) -> Bool {
        Markdown.isBilingual(Markdown.parse(markdown))
    }

    private struct Piece {
        var marker = ""        // 译文那行开头要带的标记：「## 」「- 」「1. 」
        var source = ""        // 给模型的文字
        var original = ""      // 文件里的原文
        var plain = false      // 图片、代码、表格：原样留着
        var translated: String?
    }

    private static func pieces(_ markdown: String) -> [Piece] {
        var out: [Piece] = [], code: [String]?
        // 连着的列表项在原文里只隔一个换行：拆开，一项一段译文
        let blocks = markdown.components(separatedBy: "\n\n").flatMap { block -> [String] in
            let lines = block.components(separatedBy: "\n")
            let isList = lines.count > 1 && lines.allSatisfy { $0.range(of: #"^([-*+] |\d{1,3}[.)] )"#, options: .regularExpression) != nil }
            return isList ? lines : [block]
        }
        for block in blocks {
            let b = block.trimmingCharacters(in: .whitespacesAndNewlines)
            if b.isEmpty { continue }
            if code != nil {
                code!.append(b)
                if b.hasSuffix("```") { out.append(Piece(original: code!.joined(separator: "\n\n"), plain: true)); code = nil }
                continue
            }
            if b.hasPrefix("```"), !b.dropFirst(3).contains("```") { code = [b]; continue }
            // 图片、代码、表格、单独成行的公式不翻译
            if b.hasPrefix("![") || b.hasPrefix("```") || b.hasPrefix("|") || b.hasPrefix("$$") || b.range(of: #"[A-Za-z]{2}"#, options: .regularExpression) == nil {
                out.append(Piece(original: b, plain: true))
                continue
            }
            // 原文里的引用照样是引用，译文另起一段「> 」跟在后面，阅读时按一对显示（Markdown.pairs）
            let line = b.hasPrefix("> ") ? String(b.dropFirst(2)) : b
            var marker = ""
            if let m = line.range(of: #"^(#{1,6} |[-*+] |\d{1,3}[.)] )"#, options: .regularExpression) { marker = String(line[m]) }
            out.append(Piece(marker: marker, source: String(line.dropFirst(marker.count)), original: b))
        }
        return out
    }

    private static func parse(_ raw: String) -> [Int: String] {
        var out: [Int: String] = [:]
        var cur: Int?
        for line in raw.components(separatedBy: "\n") {
            if let g = RX.groups(#"^\s*\[(\d+)\]\s*(.*)$"#, line), let n = Int(g[1]) {
                cur = n
                out[n] = g[2]
            } else if let c = cur, !line.trimmingCharacters(in: .whitespaces).isEmpty {
                out[c, default: ""] += " " + line.trimmingCharacters(in: .whitespaces)
            }
        }
        return out
    }

    /// 返回对照稿、没译成的段数。一批失败再试一次，还不行那几段留英文
    static func run(_ markdown: String, config: LLM.Config, progress: @escaping @Sendable (Int, Int) -> Void) async throws -> (text: String, missed: Int) {
        var items = pieces(markdown)
        var batches: [[Int]] = [[]], size = 0
        for (i, p) in items.enumerated() where !p.plain {
            if size + p.source.count > batchChars, !batches[batches.count - 1].isEmpty { batches.append([]); size = 0 }
            batches[batches.count - 1].append(i)
            size += p.source.count
        }
        for (n, batch) in batches.enumerated() where !batch.isEmpty {
            progress(n + 1, batches.count)
            let text = batch.map { "[\($0)] \(items[$0].source)" }.joined(separator: "\n\n")
            for _ in 0..<2 {
                try Task.checkCancellation()
                let left = batch.filter { items[$0].translated == nil }
                if left.isEmpty { break }
                guard let raw = try? await LLM.complete(prompt.replacingOccurrences(of: "{text}", with: text), config: config, maxTokens: 8000, timeout: 300) else { continue }
                let got = parse(raw)
                for i in left { if let t = got[i], !t.isEmpty { items[i].translated = t } }
            }
        }
        let body = items.map { p -> String in
            guard let t = p.translated else { return p.original }
            return "\(p.original)\n\n> \(p.marker)\(t)"
        }.joined(separator: "\n\n")
        return (body, items.filter { !$0.plain && $0.translated == nil }.count)
    }

    /// 已存的一篇：换掉「## 正文」到「[阅读原文]」之间
    static func replacingBody(_ text: String, _ body: String) -> String {
        guard let start = text.range(of: "## 正文\n\n") else { return text }
        let rest = text[start.upperBound...]
        let end = rest.range(of: "\n\n[阅读原文](", options: .backwards)?.lowerBound ?? rest.endIndex
        return String(text[..<start.upperBound]) + body + String(text[end...])
    }
}
