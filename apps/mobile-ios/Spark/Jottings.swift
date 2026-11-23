import Foundation

/// 随手记：自己手写的笔记，一条一个 .md，放在笔记库 Spark/笔记/ 下。书架「笔记」里放随手记和读后感（文章末尾「## 读后感」写了字的）；
/// 只有划线、批注的文章在复盘的「读书笔记」里。
/// 这里只放规则（写法、改正文、卡片上的数字），不碰界面和文件，tests/test_jottings.py 用 swiftc 编出来跟电脑 core/jottings.py 对照，
/// 写出来的文件要一字不差：
///
///     ---
///     title: "标题"
///     created_at: "2026-11-22T21:30:05+08:00"
///     updated_at: "2026-11-22T21:42:10+08:00"
///     tags: […]            用户自己填了才有
///     starred: true        加了星标才有，跟剪藏同一个键
///     ---
///
///     # 标题
///
///     - 收录时间：2026年11月22日，21:30（UTC+8）
///     - 修改时间：2026年11月22日，21:42（UTC+8）   改过以后才有，每改一次换掉
///
///     正文…
///
/// 文件名 YYYYMMDD_<标题>.md（新建那天，起名规矩同剪藏 Clips.fileName），之后不再改名：改标题只改文件头和第一行。
/// 改正文只改文档头和「## 读后感」、批注脚注之间的那一段；正文里删掉的批注号，它的脚注一起删。
enum Jottings {
    static let folder = "Spark/笔记"
    static let titleFromBody = 30
    static let recentDays = 7
    static let recentItems = 3
    static let kindJotting = "随手记"
    static let kindReflection = "读后感"

    /// Spark/笔记/ 下直接的一篇 .md
    static func isJotting(_ path: String) -> Bool {
        guard path.hasPrefix(folder + "/"), path.hasSuffix(".md"), !path.contains("\\") else { return false }
        let rest = path.dropFirst(folder.count + 1)
        return !rest.isEmpty && !rest.contains("/") && !rest.hasPrefix(".")
    }

    /// 两头去掉的空白：Unicode 的 Zs 类空格、Tab、各种换行、零宽空格。电脑 core/jottings._WS 同一张表
    /// （逐个写出来，不用两边各自的「空白」定义：Swift 的 whitespaces 认 U+200B，Python 不认）
    static let spaces = CharacterSet(charactersIn: "\t\n\u{0B}\u{0C}\r\u{85}\u{2028}\u{2029}\u{200B}\u{180E} \u{A0}\u{1680}"
        + "\u{2000}\u{2001}\u{2002}\u{2003}\u{2004}\u{2005}\u{2006}\u{2007}\u{2008}\u{2009}\u{200A}\u{202F}\u{205F}\u{3000}")

    /// 按码位去掉两头（leading: false 只去末尾）
    static func trim(_ s: String, leading: Bool = true) -> String {
        let u = s.unicodeScalars
        var lo = u.startIndex, hi = u.endIndex
        while hi > lo, spaces.contains(u[u.index(before: hi)]) { hi = u.index(before: hi) }
        if leading { while lo < hi, spaces.contains(u[lo]) { lo = u.index(after: lo) } }
        return String(String.UnicodeScalarView(u[lo..<hi]))
    }

    private static func isBlank(_ s: String) -> Bool { trim(s).isEmpty }

    /// 新建那天（本机时区）+ 标题。先按电脑 core/jottings._BAD_NAME 那张表换掉、去掉两头空白，剪藏那一步就不会两边不一样
    static func fileName(title: String, created: Date) -> String {
        let t = title.replacingOccurrences(of: #"[\\/:*?"<>|\n\r\t#^\[\]\x00-\x1F\x7F\x{200B}]+"#, with: " ", options: .regularExpression)
        return Clips.fileName(title: trim(t), day: DocTime.day(.at(created)))
    }

    // MARK: 写法

    private static func oneLine(_ s: String) -> String {
        trim(s.replacingOccurrences(of: "[\r\n]+", with: " ", options: .regularExpression))
    }

    /// 换行统一成 \n；去掉开头的空行和末尾的空白。第一行开头的缩进（「　　」）留着（同 core/jottings.normalize_body）
    static func normalize(_ body: String) -> String {
        var lines = body.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        while let f = lines.first, isBlank(f) { lines.removeFirst() }
        return trim(lines.joined(separator: "\n"), leading: false)
    }

    private static func stripMarks(_ line: String) -> String {
        trim(line.replacingOccurrences(of: #"^[#>*\- \t]+"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\[\^note-\d+\]|==|\*\*"#, with: "", options: .regularExpression))
    }

    /// 标题：填了用填的（换行换成空格）；空着用正文第一行，去掉开头的 #、>、- 和划线、批注号，最多 30 个字
    static func titleOf(_ title: String, _ body: String) -> String {
        let t = oneLine(title)
        if !t.isEmpty { return t }
        let first = normalize(body).components(separatedBy: "\n").first { !isBlank($0) } ?? ""
        let s = stripMarks(first)
        let cut = trim(String(String.UnicodeScalarView(s.unicodeScalars.prefix(titleFromBody))))
        return cut.isEmpty ? "未命名" : cut
    }

    private static func compose(_ header: [String], _ body: String, _ tail: [String]) -> String {
        var out = header.joined(separator: "\n") + "\n"
        if !body.isEmpty { out += "\n" + body + "\n" }
        let t = tail.joined(separator: "\n").trimmingCharacters(in: CharacterSet(charactersIn: "\n"))
        if !t.isEmpty { out += "\n" + t + "\n" }
        return out
    }

    /// 新建的一条
    static func render(title: String, body: String, created: Date, tags: [String] = [], starred: Bool = false) -> String {
        let b = normalize(body)
        let t = titleOf(title, b)
        let stamp = Clips.yaml(DocTime.iso(created))
        var fm = ["---", "title: \(Clips.yaml(t))", "created_at: \(stamp)", "updated_at: \(stamp)"]
        if !tags.isEmpty { fm.append("tags: [" + tags.map(Clips.yaml).joined(separator: ", ") + "]") }
        if starred { fm.append("starred: true") }
        fm.append("---")
        let header = fm + ["", "# \(t)", "", "- \(DocTime.capturedLabel)：\(DocTime.cn(.at(created)))"]
        return compose(header, b, [])
    }

    private struct Parts {
        let lines: [String]
        var front: Int?
        var h1: Int?
        var items: [Int] = []
        var start = 0
        var end = 0
    }

    private static func matches(_ line: String, _ pattern: String) -> Bool {
        line.range(of: pattern, options: .regularExpression) != nil
    }

    private static let headItem = #"^- [^：\n]{1,16}："#
    private static let reflectionHead = #"^## 读后感[ \t]*\z"#
    private static let noteDef = #"^\[\^note-(\d+)\]:"#
    private static let fence = #"^ {0,3}(?:```|~~~)"#

    private static func lineIndex(_ text: String, _ i: String.Index) -> Int {
        text[..<i].components(separatedBy: "\n").count - 1
    }

    /// 按行拆开：文件头、标题行、标题下面那串「- 键：值」、正文的起止行（同 core/jottings._split）。
    /// 正文到读后感或第一条批注脚注为止，哪个在前算哪个：
    /// - 读后感按 DocEdit.reflectionRange 认（同电脑 core/annotations.reflection_range），而且它后面只剩脚注、空行，
    ///   标题那一行就是「## 读后感」本身。正文中间自己写的一行「## 读后感」、后面还有别的小节的，不算；
    /// - 代码块（``` 或 ~~~ 围起来的）里的「[^note-3]: …」不算脚注
    private static func split(_ text: String) -> Parts {
        let lines = text.components(separatedBy: "\n")
        let n = lines.count
        var p = Parts(lines: lines)
        var i = 0
        if lines.first == "---", let j = lines.indices.dropFirst().first(where: { lines[$0] == "---" }) {
            p.front = j
            i = j + 1
        }
        while i < n, lines[i].isEmpty { i += 1 }
        if i < n, lines[i].hasPrefix("# ") {
            p.h1 = i
            i += 1
            while i < n, lines[i].isEmpty { i += 1 }
            while i < n, matches(lines[i], headItem) { p.items.append(i); i += 1 }
        }
        p.start = i
        var end = n
        if let r = DocEdit.reflectionRange(text) {
            let head = lineIndex(text, r.lowerBound), after = lineIndex(text, r.upperBound)
            if head >= i, head < n, matches(lines[head], reflectionHead),
               lines[min(after, n)...].allSatisfy({ $0.isEmpty || matches($0, noteDef) }) { end = head }
        }
        var fenced = false
        for j in i..<max(i, n) {
            if matches(lines[j], fence) { fenced.toggle() } else if !fenced, matches(lines[j], noteDef) { end = min(end, j); break }
        }
        p.end = end
        return p
    }

    /// 编辑框里放的正文：文档头和读后感、批注脚注之间的那一段
    static func editableBody(_ text: String) -> String {
        let p = split(text)
        return normalize(p.lines[p.start..<p.end].joined(separator: "\n"))
    }

    /// 改文件头里的一项（同 DocEdit.setting）：原来在哪就放哪；没有的放在 after 那项后面，再没有放最后
    private static func setKey(_ head: [String], _ key: String, _ value: String, after: String? = nil) -> [String] {
        var at = head.firstIndex { $0.hasPrefix("\(key):") }
        var out = head.filter { !$0.hasPrefix("\(key):") }
        if at == nil, let after, let a = out.firstIndex(where: { $0.hasPrefix("\(after):") }) { at = a + 1 }
        out.insert("\(key): \(value)", at: at.map { min($0, out.count) } ?? out.count)
        return out
    }

    private static func refs(_ s: String) -> Set<Int> {
        let ns = s as NSString
        let re = try! NSRegularExpression(pattern: #"\[\^note-(\d+)\](?!:)"#)
        return Set(re.matches(in: s, range: NSRange(location: 0, length: ns.length)).compactMap { Int(ns.substring(with: $0.range(at: 1))) })
    }

    private static func defID(_ line: String) -> Int? {
        guard let r = line.range(of: noteDef, options: .regularExpression) else { return nil }
        return Int(line[r].dropFirst("[^note-".count).dropLast("]:".count))
    }

    /// 改过的一条：标题、正文换掉，updated_at 和「修改时间」换成 now；文件头别的项、读后感、脚注照旧。
    /// 正文里删掉的批注号，脚注一起删（原来就没挂在划线上的批注不动）
    static func updated(_ text: String, title: String, body: String, now: Date) -> String {
        let p = split(text)
        let lines = p.lines
        let oldBody = normalize(lines[p.start..<p.end].joined(separator: "\n"))
        let b = normalize(body)
        let t = titleOf(title, b)
        var head = p.front.map { Array(lines[1..<$0]) } ?? []
        head = setKey(head, "title", Clips.yaml(t))
        head = setKey(head, "updated_at", Clips.yaml(DocTime.iso(now)), after: "created_at")
        let modified = "- \(DocTime.modifiedLabel)："
        var items = p.items.map { lines[$0] }.filter { !$0.hasPrefix(modified) }
        items.append(DocTime.modifiedLine(now))
        let header = ["---"] + head + ["---", "", "# \(t)", ""] + items
        // 正文里删掉的批注号；读后感里还引着的不算（脚注留着，免得读后感里的引用落空）
        let kept = lines[p.end...].filter { defID($0) == nil }.joined(separator: "\n")
        let gone = refs(oldBody).subtracting(refs(b + "\n" + kept))
        let tail = lines[p.end...].filter { ln in defID(ln).map { !gone.contains($0) } ?? true }
        return compose(header, b, Array(tail))
    }

    // MARK: 读

    /// 列表上的一条
    struct Meta: Identifiable, Hashable {
        let path: String
        let title: String
        let created: Date
        let updated: Date
        let excerpt: String
        let starred: Bool
        var id: String { path }
    }

    private static func date(_ w: DocTime.When?) -> Date? {
        switch w {
        case .at(let d)?: return d
        case .day(let s)?:
            let p = s.split(separator: "-").compactMap { Int($0) }
            guard p.count == 3 else { return nil }
            return Calendar(identifier: .gregorian).date(from: DateComponents(year: p[0], month: p[1], day: p[2]))
        case nil: return nil
        }
    }

    /// 列表上的一句：正文第一行（去掉开头的 #、>、- 和划线、批注号）
    static func excerpt(_ body: String, limit: Int = 80) -> String {
        for line in body.components(separatedBy: "\n") {
            let t = stripMarks(line)
            // 按码位截（同电脑，跟剪藏文件名一个单位）
            if !t.isEmpty { return t.unicodeScalars.count <= limit ? t : String(String.UnicodeScalarView(t.unicodeScalars.prefix(limit))) + "…" }
        }
        return ""
    }

    /// 一条的信息。fallback：文件头、收录时间、文件名都看不出新建时间时用（文件的改动时间）
    static func meta(path: String, text: String, fallback: Date) -> Meta {
        let f = Clips.frontmatter(text)
        let p = split(text)
        let h1 = p.h1.map { trim(String(p.lines[$0].dropFirst(2))) } ?? ""
        let name = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        let title = [f["title"] ?? "", h1, name].first { !$0.isEmpty } ?? "未命名"
        var created = date(DocTime.parse(f["created_at"] ?? ""))
        if created == nil, let r = text.range(of: DocTime.capturedLine, options: .regularExpression) {
            let line = String(text[r])
            if let c = line.firstIndex(of: "：") { created = date(DocTime.parse(String(line[line.index(after: c)...]))) }
        }
        if created == nil, name.range(of: #"^\d{8}_"#, options: .regularExpression) != nil { created = date(DocTime.parse(String(name.prefix(8)))) }
        let made = created ?? fallback
        let changed = date(DocTime.parse(f["updated_at"] ?? "")) ?? made
        let body = normalize(p.lines[p.start..<p.end].joined(separator: "\n"))
        return Meta(path: path, title: title, created: made, updated: changed, excerpt: excerpt(body), starred: f["starred"] == "true")
    }

    // MARK: 笔记页最上面那张卡

    struct Entry: Hashable {
        let path: String
        let title: String
        let created: Date
        let changed: Date
    }

    struct Recent: Hashable {
        let entry: Entry
        let kind: String     // 随手记 / 读后感
    }

    struct Summary: Equatable {
        var jottings = 0
        var reflections = 0
        var latest: Date?
        var added = 0
        var edited = 0
        var recent: [Recent] = []
    }

    /// 卡片上的数字（同 core/jottings.summarize）。不调模型。「笔记」里放两样：随手记，和写了读后感的文档。
    /// - 读后感只算 Spark/笔记/ 以外的：随手记自己写了读后感，只在随手记里算，不重复算；
    /// - 近 7 天（含正好 7 天前那一刻）新增：这几天新建的随手记；修改：之前就有、这几天改过的随手记，加上这几天改过的读后感文档；
    /// - 最近的三条：随手记按修改时刻、读后感按文件改动时刻，新的在前
    static func summarize(jottings: [Entry], reflections all: [Entry], now: Date) -> Summary {
        let reading = all.filter { !$0.path.hasPrefix(folder + "/") }
        let cutoff = now.timeIntervalSince1970 - Double(recentDays * 86400)
        var s = Summary(jottings: jottings.count, reflections: reading.count)
        s.added = jottings.filter { $0.created.timeIntervalSince1970 >= cutoff }.count
        s.edited = jottings.filter {
            $0.changed.timeIntervalSince1970 >= cutoff && $0.created.timeIntervalSince1970 < cutoff && $0.changed > $0.created
        }.count + reading.filter { $0.changed.timeIntervalSince1970 >= cutoff }.count
        let rows = (jottings.map { Recent(entry: $0, kind: kindJotting) } + reading.map { Recent(entry: $0, kind: kindReflection) })
            .sorted { ($0.entry.changed, $1.entry.path) > ($1.entry.changed, $0.entry.path) }
        s.latest = rows.first?.entry.changed
        s.recent = Array(rows.prefix(recentItems))
        return s
    }
}
