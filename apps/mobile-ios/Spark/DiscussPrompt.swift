import Foundation

/// 复盘里的「讨论」：提检索词的提示词、回答的提示词、检索词的解析、存成复盘的文档格式。
/// 跟电脑 core/discuss.py 逐字一致：tests/test_review_discuss.py 用 swiftc 单独编这个文件，同样的输入两边比。
/// 只依赖 Foundation，所以出处（《标题》（来源，日期））、写法要求（style）靠调用的一方先算好再传进来。
enum DiscussPrompt {
    static let maxDocs = 12           // 材料最多几篇
    static let maxPerDoc = 2          // 同一篇最多留几个片段
    static let snippetChars = 600     // 每个片段截到多长
    static let maxTerms = 6           // 检索词最多几个
    static let maxTermChars = 20
    static let maxHistoryTurns = 6    // 提示词里带最近几轮问答
    static let historyAnswerChars = 800
    static let queryTurns = 3         // 提检索词时带最近几轮
    static let queryAnswerChars = 200
    static let tagMinDocs = 3         // 标签范围内命中少于这么多篇，用最近几篇的一句话补
    static let tagFillDocs = 6        // 补到这么多篇
    static let maxQuestionChars = 500
    static let maxSaveTurns = 30
    static let notFound = "笔记库里没找到相关内容，换个说法试试。"

    struct Turn: Codable, Hashable { let q: String; let a: String }

    /// 一篇材料：类型（剪藏、单集小结…）、出处、一句话、摘录（已截断）
    struct Material { let type: String; let source: String; let tldr: String; let snippets: [String] }

    /// 回答下面列的一条出处（n 是回答里的 [n]）
    struct Source: Codable, Hashable {
        let n: Int
        let title: String
        let path: String     // 相对笔记库
        let kind: String
        let date: String     // yyyy-MM-dd 或空
        let source: String
    }

    struct SavedTurn: Codable, Hashable { let q: String; let a: String; let sources: [Source] }

    // MARK: 小工具（同 core/review._cut 和 core/discuss）

    static func cut(_ text: String, _ n: Int) -> String {
        let t = text.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return t.count <= n ? t : String(t.prefix(n)).trimmingCharacters(in: .whitespacesAndNewlines) + "……"
    }

    /// 历史回答里的 [n] 是上一轮材料的编号，这一轮重新排，带进提示词只会混淆
    static func stripCites(_ text: String) -> String {
        text.replacingOccurrences(of: #"\[\d+\]"#, with: "", options: .regularExpression)
    }

    static func flat(_ text: String) -> String {
        text.components(separatedBy: .whitespacesAndNewlines).filter { !$0.isEmpty }.joined(separator: " ")
    }

    static func safe(_ name: String) -> String {
        let s = String(name.replacingOccurrences(of: #"[\\/:*?"<>|\s]+"#, with: "_", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "_")).prefix(40))
        return s.isEmpty ? "未命名" : s
    }

    // MARK: 提示词

    private static func turns(_ history: [Turn], _ keep: Int, _ chars: Int) -> String {
        history.suffix(keep).map { "问：\($0.q)\n答：\(cut(stripCites($0.a), chars))" }.joined(separator: "\n\n")
    }

    static func queryPrompt(question: String, history: [Turn]) -> String {
        let past = history.isEmpty ? "" : "\n===== 之前的对话（要点） =====\n\(turns(history, queryTurns, queryAnswerChars))\n"
        return """
        我在笔记库里提问。请把我最新的问题变成 \(maxTerms - 3) 到 \(maxTerms) 个检索词，用来在笔记库里全文搜索。

        规则：
        - 检索词是名词性的短词，每个 2 到 8 个字：人名、公司、产品、技术、概念。英文词和缩写原样写。
        - 不写「什么」「怎么样」「为什么」这类疑问词，不写整句话。
        - 问题里的「它」「这个」「那家公司」，用之前对话里说到的具体名词代替。
        - 同一个意思有常见的另一种写法（中英文、简称全称）时，补一两个。

        只输出一行：检索词之间用一个空格分开。不写标点，不写解释，不写编号。
        \(past)
        ===== 我最新的问题 =====
        \(question)

        """
    }

    static func block(_ i: Int, _ m: Material) -> String {
        var part = ["[\(i)] \(m.type)　\(m.source)"]
        if !m.tldr.isEmpty { part.append("一句话：\(m.tldr)") }
        part += m.snippets.map { "摘录：\n\($0)" }
        return part.joined(separator: "\n")
    }

    static func answerPrompt(question: String, history: [Turn], materials: [Material], tag: String, style: String) -> String {
        let scope = tag.isEmpty ? "" : "（范围限定在「\(tag)」这个主题下的文章）"
        let past = history.isEmpty ? "" : "\n===== 之前的对话 =====\n\(turns(history, maxHistoryTurns, historyAnswerChars))\n"
        let blocks = materials.enumerated().map { "\n" + block($0.offset + 1, $0.element) }.joined(separator: "\n")
        return """
        下面是我笔记库里的材料\(scope)，以及我们之前的对话。请只根据这些材料，回答我最后的问题。

        \(style)

        这次回答的补充要求（和上面冲突时，以这里为准）：
        - 只用材料里写的。材料里没有的，直接说笔记库里没有这方面的内容，不要用常识补，不要编。
        - 出处只写材料编号：每条结论后面写 [编号]，比如 [2]；一条结论用了几篇就连着写几个，比如 [1][3]。编号只能用下面材料里有的，不要自己造。
        - 不分节，不加标题，用短段落或列表。先给结论，再说依据。
        - 材料之间说法不一致的，直接指出来，各自写明出处。
        \(past)
        ===== 材料（共 \(materials.count) 篇）=====
        \(blocks)

        ===== 我最后的问题 =====
        \(question)

        """
    }

    // MARK: 检索词

    private static let splitChars: Set<Unicode.Scalar> = Set(" \t\r\n，,、;；。！!？?：:（）()【】「」《》\"“”".unicodeScalars)

    private static func pieces(_ text: String) -> [[Unicode.Scalar]] {
        var out: [[Unicode.Scalar]] = [], cur: [Unicode.Scalar] = []
        for ch in text.unicodeScalars {
            if splitChars.contains(ch) { out.append(cur); cur = [] } else { cur.append(ch) }
        }
        return out + [cur]
    }

    private static func str(_ s: [Unicode.Scalar]) -> String { var v = String.UnicodeScalarView(); v.append(contentsOf: s); return String(v) }

    private static func cleanTerms(_ ps: [[Unicode.Scalar]]) -> [String] {
        var seen = Set<String>(), out: [String] = []
        for p in ps {
            let t = str(p).trimmingCharacters(in: .whitespacesAndNewlines)
            let n = t.unicodeScalars.count
            if n < 2 || n > maxTermChars || !seen.insert(t.lowercased()).inserted { continue }
            out.append(t)
        }
        return Array(out.prefix(maxTerms))
    }

    /// 模型输出的第一个非空行按空格和标点拆成检索词；拆不出来就用问题本身拆（长的一段每 4 个字切一个）
    static func parseTerms(_ raw: String, question: String) -> [String] {
        let line = raw.components(separatedBy: .newlines).first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? ""
        let terms = cleanTerms(pieces(line))
        if !terms.isEmpty { return terms }
        var out: [[Unicode.Scalar]] = []
        for p in pieces(question) {
            if p.count > 8 { stride(from: 0, to: p.count, by: 4).forEach { out.append(Array(p[$0..<min($0 + 4, p.count)])) } } else { out.append(p) }
        }
        return cleanTerms(out)
    }

    /// 回答里写到的材料编号（按第一次出现的先后，去重，只留 1 到 count 的）
    static func cited(_ answer: String, count: Int) -> [Int] {
        guard let re = try? NSRegularExpression(pattern: #"\[(\d+)\]"#) else { return [] }
        let ns = answer as NSString
        var out: [Int] = []
        for m in re.matches(in: answer, range: NSRange(location: 0, length: ns.length)) {
            if let n = Int(ns.substring(with: m.range(at: 1))), n >= 1, n <= count, !out.contains(n) { out.append(n) }
        }
        return out
    }

    // MARK: 存成复盘

    static func shortQuestion(_ q: String, _ n: Int = 30) -> String {
        let f = flat(q)
        return f.count <= n ? f : String(f.prefix(n)) + "…"
    }

    static func sourceLabel(_ s: Source) -> String {
        let day = s.date.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil ? s.date : "日期不详"
        return "《\(flat(s.title))》（\(flat(s.source.isEmpty ? day : "\(s.source)，\(day)"))）"
    }

    static func sourceLine(_ s: Source) -> String {
        let label = sourceLabel(s)
        if s.path.contains(where: { "|[]\n\r".contains($0) }) || !s.path.hasSuffix(".md") { return "- [\(s.n)] \(label)" }
        let target = String(s.path.dropLast(3))
        return "- [\(s.n)] [[\(target)|\(label.replacingOccurrences(of: "|", with: "｜").replacingOccurrences(of: "]", with: "］"))]]"
    }

    /// 标题（第一问）、正文（每一轮「## 问：…」加回答加出处）、元信息。generated 是「生成时间：」后面的整段
    static func document(turns: [SavedTurn], tag: String, generated: String) -> (title: String, body: String, meta: [String]) {
        let meta = ["生成时间：\(generated)", tag.isEmpty ? "范围：整个笔记库" : "范围：主题「\(tag)」", "共 \(turns.count) 轮"]
        let blocks = turns.map { t -> String in
            var part = ["## 问：\(flat(t.q))", t.a.trimmingCharacters(in: .whitespacesAndNewlines)]
            if !t.sources.isEmpty { part.append("出处：\n" + t.sources.map(sourceLine).joined(separator: "\n")) }
            return part.joined(separator: "\n\n")
        }
        return ("讨论：\(shortQuestion(turns[0].q))", blocks.joined(separator: "\n\n"), meta)
    }

    /// 整篇文字（同 core/review._write）
    static func render(title: String, meta: [String], body: String) -> String {
        "# \(title)\n\n" + meta.map { "- \($0)" }.joined(separator: "\n") + "\n\n" + body.trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
    }

    /// stamp：yyyyMMddHHmm。日期放最后：书架按文件名里最后一个 8 位数字认日期
    static func fileName(firstQuestion: String, stamp: String) -> String {
        "复盘_讨论_\(safe(shortQuestion(firstQuestion)))_\(stamp).md"
    }
}
