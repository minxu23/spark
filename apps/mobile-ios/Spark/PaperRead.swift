import Foundation

/// 论文的阅读侧：三个页签（导读 / 原文 / PDF）要的判断，和针对单篇论文的「追问」——提示词、存进本文的写法。
/// 跟电脑 core/paper_read.py 逐字一致：tests/test_paper_read.py 用 swiftc 把这个文件和 DiscussPrompt.swift、Shared/Paper.swift 单独编出来，
/// 同样的输入两边比。只依赖 Foundation。字符串一律按 Unicode 标量（同 Python 的 str）切，不按字形簇，
/// 「\r\n」不会被当成一个字。
///
/// 「从 PDF 取字」：文件头没有专门的标记，按收录的走法推（同电脑）：有 pdf: 一行、来源不是 arXiv 的，正文一定是 PDF 取的字；
/// 来源是 arXiv 的，网页版全文转来的正文带「#」小标题，PDF 取的字只有段落（参考文献那一行写成「## References」，不算），
/// 正文里别的小标题一行都没有就算 PDF 取的字。
enum PaperRead {
    static let heading = "追问"
    static let notInPaper = "文中没有提到这一点。"
    static let maxPaperChars = Paper.maxChars   // 同论文写摘要的截断
    static let maxHistoryTurns = 6
    static let historyAnswerChars = 800
    static let arxivSource = "arXiv"
    static let paperFolder = "Spark/论文"

    private typealias U = [Unicode.Scalar]
    private static func u(_ s: String) -> U { Array(s.unicodeScalars) }
    private static func str<S: Sequence>(_ s: S) -> String where S.Element == Unicode.Scalar {
        var v = String.UnicodeScalarView(); v.append(contentsOf: s); return String(v)
    }

    private static func has(_ t: U, _ p: U, at i: Int) -> Bool {
        i >= 0 && i + p.count <= t.count && Array(t[i..<(i + p.count)]) == p
    }
    private static func find(_ t: U, _ p: U, from: Int = 0) -> Int? {
        guard p.count <= t.count else { return nil }
        var i = max(0, from)
        while i + p.count <= t.count { if has(t, p, at: i) { return i }; i += 1 }
        return nil
    }
    private static func rfind(_ t: U, _ p: U) -> Int? {
        guard p.count <= t.count else { return nil }
        var i = t.count - p.count
        while i >= 0 { if has(t, p, at: i) { return i }; i -= 1 }
        return nil
    }
    /// from 之后（不含 from 本身，除非它是第 0 个字或紧跟换行）每一行的起点和行尾（不含换行）
    private static func lines(_ t: U, after from: Int) -> [(start: Int, end: Int)] {
        var out: [(Int, Int)] = []
        var s = 0
        while s <= t.count {
            var e = s
            while e < t.count && t[e] != "\n" { e += 1 }
            if s >= from { out.append((s, e)) }
            if e >= t.count { break }
            s = e + 1
        }
        return out
    }
    private static func isBlank(_ c: Unicode.Scalar) -> Bool { c == " " || c == "\t" }

    // MARK: 「## 追问」一节

    /// 标题行（起, 行尾）：只认最后一个「[阅读原文](」后面的，取最后一个；行尾可以带 \r（同电脑 clip_compose.followup_head）
    private static func head(_ t: U) -> (start: Int, end: Int)? {
        let mark = u("\n[阅读原文](")
        let from = rfind(t, mark).map { $0 + mark.count } ?? 0
        let h = u("## " + heading)
        var hit: (Int, Int)?
        for (s, e) in lines(t, after: from) where has(t, h, at: s) {
            var rest = Array(t[(s + h.count)..<e])
            if rest.last == "\r" { rest.removeLast() }
            if rest.allSatisfy(isBlank) { hit = (s, e) }
        }
        return hit
    }

    /// 「## 追问」一节：(标题行起, 节尾)。节尾是下一个「## 」或第一条批注脚注，没有就是文末（UTF 标量下标）
    private static func range(_ t: U) -> (start: Int, end: Int)? {
        guard let h = head(t) else { return nil }
        for (s, e) in lines(t, after: h.end + 1) {
            if has(t, u("## "), at: s) { return (h.start, s) }
            let note = u("[^note-")
            if has(t, note, at: s) {
                var k = s + note.count, digits = 0
                while k < e, t[k].properties.numericType == .decimal { k += 1; digits += 1 }
                if digits > 0, has(t, u("]:"), at: k) { return (h.start, s) }
            }
        }
        return (h.start, t.count)
    }

    /// 有没有存进来的「## 追问」一节
    static func hasFollowUp(_ text: String) -> Bool { head(u(text)) != nil }

    /// s 里「## 追问」一节前面的部分（没有这一节原样返回）。正文、翻译、重写摘要都先过这里，不把追问当正文
    static func beforeFollowUp(_ s: String) -> String {
        let t = u(s)
        guard let h = head(t) else { return s }
        return str(t[..<h.start])
    }

    // MARK: 文件头、原文、从 PDF 取字

    /// 文件头里的几行 key: value（值是 JSON 字符串的解开）
    static func front(_ text: String) -> [String: String] {
        var out: [String: String] = [:]
        let ls = text.components(separatedBy: "\n")
        guard ls.first == "---" else { return out }
        for ln in ls.dropFirst() {
            if ln == "---" { break }
            guard let colon = ln.firstIndex(of: ":") else { continue }
            let k = String(ln[..<colon])
            guard !k.isEmpty, k.unicodeScalars.allSatisfy({ ($0.value >= 65 && $0.value <= 90) || ($0.value >= 97 && $0.value <= 122) || $0 == "_" }) else { continue }
            var v = String(ln[ln.index(after: colon)...]).trimmingCharacters(in: .whitespacesAndNewlines)
            let vs = u(v)
            if vs.count >= 2, vs.first == "\"", vs.last == "\"" {
                guard let d = v.data(using: .utf8),
                      let s = try? JSONSerialization.jsonObject(with: d, options: .fragmentsAllowed) as? String else { continue }
                v = s
            }
            out[k] = v
        }
        return out
    }

    /// 文件头 pdf: 指向的原件在笔记库里的路径（Spark/论文/attachments/<16 位 hex>.pdf）；没有或不合规是空串
    static func pdfKey(_ text: String) -> String {
        let v = front(text)["pdf"] ?? ""
        let prefix = paperFolder + "/"
        let key = v.hasPrefix(prefix) ? v : prefix + v
        let dir = prefix + "attachments/"
        guard !v.isEmpty, key.hasPrefix(dir), key.hasSuffix(".pdf") else { return "" }
        let name = u(key).dropFirst(u(dir).count).dropLast(4)
        guard name.count == 16, name.allSatisfy({ "0123456789abcdef".unicodeScalars.contains($0) }) else { return "" }
        return key
    }

    /// 原文：「## 正文」后面、「[阅读原文]」或「## 追问」前面（同电脑 clip_compose.body_of）
    static func body(_ text: String) -> String {
        let t = u(text)
        let mark = u("\n## 正文\n")
        guard let i = find(t, mark) else { return "" }
        var b = u(beforeFollowUp(str(t[(i + mark.count)...])).trimmingCharacters(in: .whitespacesAndNewlines))
        if let cut = rfind(b, u("\n\n[阅读原文](")) { b = Array(b[..<cut]) }
        else if has(b, u("[阅读原文]("), at: 0) { b = [] }
        return str(b)
    }

    /// 原文里参考文献一节在哪：写摘要时截断用的那个标题（Paper.beforeReferences）是原文里第几个这样的标题（从 0 数），原文里一共几个。
    /// 没有合适的标题返回 nil（电脑 server._fold_references 同一条）
    static func referencesPick(_ text: String) -> (skip: Int, total: Int)? {
        let b = body(text)
        let before = Paper.beforeReferences(b)
        guard before.unicodeScalars.count < b.unicodeScalars.count else { return nil }
        func count(_ s: String) -> Int {
            s.components(separatedBy: "\n").filter {
                $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased().range(of: "^## " + Paper.referencesTitle + "$", options: .regularExpression) != nil
            }.count
        }
        return (count(before), count(b))
    }

    /// 正文是不是从 PDF 取的字（见上面的说明）
    static func fromPDF(_ text: String) -> Bool {
        guard !pdfKey(text).isEmpty else { return false }
        if (front(text)["source"] ?? "") != arxivSource { return true }
        let b = u(body(text))
        return !lines(b, after: 0).contains { s, e in
            var k = s
            while k < e, b[k] == "#" { k += 1 }
            guard k - s >= 1 && k - s <= 6 && k < e && isBlank(b[k]) else { return false }
            // 收录时 PDF 取字写的「## References」这一行不算小标题（规则同 Paper.referencesTitle）
            let line = str(b[s..<e]).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            return line.range(of: "^## " + Paper.referencesTitle + "$", options: .regularExpression) == nil
        }
    }

    // MARK: 提示词

    /// 划线的写法（同 MarkRule.pattern、电脑 annotations.MARK_PATTERN，测试核对）
    static let markPattern = #"(?<!=)==([^=\n](?:=?[^=\n])*)==(?!=)"#

    /// 交给模型的正文：原文一节，参考文献标题起去掉（Paper.beforeReferences，同写摘要），去掉图片，
    /// 用户的划线记号去成纯文字、批注号删掉，连着的空行并成一个，截到 maxPaperChars 字
    static func paperText(_ text: String) -> String {
        let b = Paper.beforeReferences(body(text))
            .replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: markPattern, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"\[\^note-\d+\]"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return str(b.unicodeScalars.prefix(maxPaperChars))
    }

    private static func turns(_ history: [DiscussPrompt.Turn]) -> String {
        history.suffix(maxHistoryTurns).map { "问：\($0.q)\n答：\(DiscussPrompt.cut($0.a, historyAnswerChars))" }.joined(separator: "\n\n")
    }

    static func answerPrompt(title: String, paper: String, question: String, history: [DiscussPrompt.Turn]) -> String {
        let past = history.isEmpty ? "" : "\n===== 之前的问答 =====\n\(turns(history))\n"
        return """
        下面是一篇论文的正文（从网页或 PDF 取出的文字，可能夹杂少量噪音，参考文献已经去掉），以及我们之前关于这篇论文的问答。请只根据这篇论文，回答我最后的问题。

        要求：
        - 只用论文里写的。论文里没有的，直接回答「\(notInPaper)」，不要用常识补，不要编。
        - 用中文回答。术语、方法名、模型名、数据集名可以保留英文。
        - 能说出依据在论文哪一部分（章节、图表编号）的，顺带写一句。
        - 不加标题，用短段落或列表。先给结论，再说依据。
        \(past)
        ===== 论文：\(title) =====
        \(paper)

        ===== 我最后的问题 =====
        \(question)

        """
    }

    // MARK: 存进本文

    /// 回答写进文件前：「#」「##」「###」开头的行降成「####」，「==」转义成「\=\=」，「[^」「[阅读原文](」开头的行前面加反斜杠，
    /// 代码栅栏行（去掉行首空格后以「```」开头）是奇数个时末尾补一行「```」（同电脑 paper_read.answer_lines）
    static func answerLines(_ a: String) -> String {
        var out = a.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n").map { line -> String in
            var t = u(line)
            var s = 0
            while s < t.count, t[s] == " " { s += 1 }
            var h = s
            while h < t.count, t[h] == "#" { h += 1 }
            if s <= 3, h - s >= 1, h - s <= 3, h == t.count || isBlank(t[h]) { t = u("####") + t[h...] }
            var out = str(t).replacingOccurrences(of: "==", with: "\\=\\=")
            if out.hasPrefix("[^") || out.hasPrefix("[阅读原文](") { out = "\\" + out }
            return out
        }
        if out.filter({ String($0.unicodeScalars.drop { $0 == " " }).hasPrefix("```") }).count % 2 == 1 { out.append("```") }
        return out.joined(separator: "\n")
    }

    static func block(q: String, a: String) -> String { "### \(DiscussPrompt.flat(q))\n\n\(answerLines(a))" }

    private static func trimNL(_ t: U) -> U {
        var e = t.count
        while e > 0, t[e - 1] == "\n" { e -= 1 }
        return Array(t[..<e])
    }

    /// 几组问答加进「## 追问」一节：没有这一节就文末另起一节，有就接在这一节最后。原有的字一个不动，只规整接口处的换行
    static func appending(_ text: String, turns: [DiscussPrompt.Turn]) -> String {
        let blocks = turns.map { block(q: $0.q, a: $0.a) }.joined(separator: "\n\n")
        let t = u(text)
        guard let r = range(t) else { return str(trimNL(t)) + "\n\n## \(heading)\n\n" + blocks + "\n" }
        let tail = str(t[r.end...])
        return str(trimNL(Array(t[..<r.end]))) + "\n\n" + blocks + "\n" + (tail.isEmpty ? "" : "\n" + tail)
    }
}
