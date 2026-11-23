import SwiftUI
import UIKit

/// 手机上改一篇剪藏或复盘：改名、划重点、删图、写追问。文件内容的改法都在这里，改完由 Vault.saveEdited 存好并交给电脑。
enum DocEdit {
    /// 能在手机上改名、删的：剪藏和复盘
    static func editable(_ path: String) -> Bool {
        Clips.isClip(path) || (path.hasPrefix(Library.reviewsDir) && path.hasSuffix(".md"))
    }

    /// 能做笔记（划线、批注、读后感）的：笔记库 Spark/ 和 output/ 下所有给人读的 Markdown——剪藏、整理稿、
    /// 文字记录、信息跟进的原文、小结、会议资料、报告、复盘。新加的资料类型放在这两个目录下就自动能做笔记，
    /// 不用逐类开。剪藏索引、摘录、节目 / 会议文件夹的总览页这类生成的列表页除外。电脑上 core/annotations.annotatable 同一条规则
    static func annotatable(_ path: String) -> Bool {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard path.hasSuffix(".md"), parts.count >= 2, ["Spark", "output"].contains(parts[0]),
              !parts.contains(where: { $0.isEmpty || $0.hasPrefix(".") }) else { return false }
        let name = parts[parts.count - 1]
        if ["索引.md", "摘录.md"].contains(name) { return false }
        return !(parts.count >= 3 && name == parts[parts.count - 2] + ".md")
    }

    /// 改标题：文件头的 title 和第一行「# 标题」。文件名不动，别的地方的链接不会断
    static func retitled(_ text: String, _ title: String) -> String {
        var lines = text.components(separatedBy: "\n")
        var inHead = lines.first == "---"
        var didHead = false
        for i in lines.indices {
            if inHead {
                if i > 0, lines[i] == "---" { inHead = false; continue }
                if i > 0, lines[i].hasPrefix("title:") { lines[i] = "title: \(Clips.yaml(title))"; didHead = true }
                continue
            }
            if lines[i].hasPrefix("# ") {
                lines[i] = "# \(title)"
                break
            }
        }
        _ = didHead
        return lines.joined(separator: "\n")
    }

    /// 加星标、取消星标：文件头里的 starred: true（Obsidian 里也看得到、能按它筛）。
    /// 能做笔记的文件都能加：没有文件头的（整理稿、文字记录、报告）补一段；取消后文件头空了就整段去掉。
    /// 电脑上 core.annotations.with_star 同一套
    static func starring(_ text: String, _ on: Bool) -> String {
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "---", let end = lines.dropFirst().firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "---" }) else {
            guard on else { return text }
            var rest = Substring(text)
            while rest.first == "\n" { rest = rest.dropFirst() }
            return "---\nstarred: true\n---\n\n" + rest
        }
        var head = lines[1..<end].filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty && !$0.hasPrefix("starred:") }
        if on { head.append("starred: true") }
        let rest = lines[(end + 1)...].joined(separator: "\n")
        if head.isEmpty { return String(rest.drop { $0 == "\n" }) }
        return (["---"] + head + ["---"]).joined(separator: "\n") + "\n" + rest
    }

    /// 文件头里有没有 starred: true（只看开头那段文件头）
    static func isStarred(_ text: String) -> Bool {
        guard text.hasPrefix("---\n") else { return false }
        let lines = text.prefix(4096).components(separatedBy: "\n").dropFirst()
        for line in lines {
            if line.trimmingCharacters(in: .whitespaces) == "---" { return false }
            if line.range(of: #"^starred:[ \t]*true[ \t]*$"#, options: .regularExpression) != nil { return true }
        }
        return false
    }

    /// 文件头里的开关（starred、pinned）：开就写「key: true」，关就去掉这一行
    static func flagged(_ text: String, _ key: String, _ on: Bool) -> String {
        setting(text, key, on ? "true" : nil)
    }

    /// 改文件头里的一项：value 为 nil 就去掉
    static func setting(_ text: String, _ key: String, _ value: String?) -> String {
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "---", let end = lines.dropFirst().firstIndex(of: "---") else { return text }
        var head = Array(lines[1..<end])
        let at = head.firstIndex { $0.hasPrefix("\(key):") }
        head.removeAll { $0.hasPrefix("\(key):") }
        if let value { head.insert("\(key): \(value)", at: min(at ?? head.count, head.count)) }
        return (["---"] + head + lines[end...]).joined(separator: "\n")
    }

    /// 移到别的主题：换掉文件头里的标签
    static func retagged(_ text: String, _ tags: [String]) -> String {
        setting(text, "tags", tags.isEmpty ? nil : "[" + tags.map(Clips.yaml).joined(separator: ", ") + "]")
    }

    /// 删掉一张图：单独一行的 ![](地址)
    static func removingImage(_ text: String, _ src: String) -> String {
        text.components(separatedBy: "\n")
            .filter { !($0.trimmingCharacters(in: .whitespaces).hasPrefix("![") && $0.contains("](\(src))")) }
            .joined(separator: "\n")
            .replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
    }

    /// 划重点：这一段里选中的文字用 ==…== 标出来，跟电脑上阅读页、Obsidian 同一种写法。
    /// 选中的是显示出来的字；原文里中间可能夹着 **、链接地址，按字逐个对上
    static func highlighted(_ text: String, block: String, selected: String) -> String? {
        let sel = selected.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sel.isEmpty, let b = blockRange(text, block) else { return nil }
        let block = String(text[b])   // 文件里的原样（行尾的两个空格这类显示时去掉的也在）
        guard let found = sourceRange(sel, in: block) else { return nil }
        let s = balanced(found, in: block)
        let inner = String(block[s]).replacingOccurrences(of: "==", with: "")
        return text.replacingCharacters(in: b, with: block.replacingCharacters(in: s, with: "==\(inner)=="))
    }

    /// 选中范围只框住了一半加粗（「**重要的**观点」从「重」开始）：把落在外面的 ** 也框进来，
    /// 不然划线和加粗交叉，排出来、Obsidian 里都会乱
    static func balanced(_ r: Range<String.Index>, in raw: String) -> Range<String.Index> {
        var r = r
        for _ in 0..<2 where raw[r].components(separatedBy: "**").count % 2 == 0 {
            if raw[..<r.lowerBound].hasSuffix("**") { r = raw.index(r.lowerBound, offsetBy: -2)..<r.upperBound }
            else if raw[r.upperBound...].hasPrefix("**") { r = r.lowerBound..<raw.index(r.upperBound, offsetBy: 2) }
            else { break }
        }
        return r
    }

    /// 显示出来的一串字在原文里的位置
    static func sourceRange(_ shown: String, in raw: String) -> Range<String.Index>? {
        if let r = raw.range(of: shown) { return r }
        // 显示时去掉的标记、行尾硬换行的空格，都可以夹在两个字中间
        let gap = #"(?:\*\*|\*|__|_|`|==|~~|\]\([^)]*\)|\[\^note-\d+\]|\[|[ \t]+(?=\n))*"#
        let pattern = shown.map { ch -> String in
            if ch == Note.glyph { return #"(?:\[\^note-\d+\])?"# }
            // 显示时换过引号（Markdown.curlyQuotes、latinQuotes）：文件里可能还是另一种
            if "\"“”".contains(ch) { return #"["“”]"# }
            if "'‘’".contains(ch) { return #"['‘’]"# }
            return NSRegularExpression.escapedPattern(for: String(ch))
        }.joined(separator: gap)
        return raw.range(of: pattern, options: .regularExpression)
    }

    /// 去掉这一段里某几处划线；没指明就整段去掉
    static func unhighlighted(_ text: String, block: String, marked: [String] = []) -> String? {
        guard let b = blockRange(text, block) else { return nil }
        let block = String(text[b])
        var out = block
        if marked.isEmpty {
            out = block.replacingOccurrences(of: "==", with: "")
        } else {
            let ns = block as NSString
            for m in RX.re(#"==([^=\n]+)=="#).matches(in: block, range: NSRange(location: 0, length: ns.length)).reversed() {
                let inner = ns.substring(with: m.range(at: 1))
                let shown = String(Markdown.inline(inner).characters)
                guard marked.contains(where: { shown.contains($0) || $0.contains(shown) }) else { continue }
                out = (out as NSString).replacingCharacters(in: m.range, with: inner)
            }
        }
        guard out != block else { return nil }
        return text.replacingCharacters(in: b, with: out)
    }

    /// 点的那一段在文件里的位置。同样的话可能也出现在摘要、文件头里：
    /// 先找正文（「## 正文」后面）里单独成段的，再找全文里单独成段的，最后才退到第一处
    static func blockRange(_ text: String, _ block: String) -> Range<String.Index>? {
        var all: [Range<String.Index>] = []
        var from = text.startIndex
        while let r = text.range(of: block, range: from..<text.endIndex) {
            all.append(r)
            from = r.upperBound
        }
        func whole(_ r: Range<String.Index>) -> Bool {
            (r.lowerBound == text.startIndex || text[text.index(before: r.lowerBound)] == "\n")
                && (r.upperBound == text.endIndex || text[r.upperBound] == "\n")
        }
        // 显示时每行去掉了首尾空格（行尾两个空格是 Markdown 的硬换行）：原样找不到就放宽这些空格再找
        if all.isEmpty {
            let lines = block.components(separatedBy: "\n").map { NSRegularExpression.escapedPattern(for: $0) }
            let pattern = #"[ \t]*"# + lines.joined(separator: #"[ \t]*\n[ \t]*"#) + #"[ \t]*"#
            if let re = try? NSRegularExpression(pattern: pattern) {
                all = re.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap { Range($0.range, in: text) }
            }
        }
        let bodyStart = text.range(of: "\n## 正文\n")?.upperBound ?? text.startIndex
        return all.first { whole($0) && $0.lowerBound >= bodyStart } ?? all.first(where: whole) ?? all.first
    }

    /// 文章里划过的重点
    static func marks(_ text: String) -> [String] {
        let ns = text as NSString
        return RX.re(#"==([^=\n]+)=="#).matches(in: text, range: NSRange(location: 0, length: ns.length)).map { ns.substring(with: $0.range(at: 1)) }
    }

    // MARK: 批注

    /// 批注用 Obsidian 的脚注存在文章里：「==划的字==[^note-3]」，文末「[^note-3]: 批注」。
    /// Obsidian 里照样能看，电脑和手机同步的还是这一个文件
    struct Note: Identifiable, Hashable {
        let id: Int
        let quote: String     // 批注的那段字（原文写法）
        let text: String
        static let glyph: Character = "✎"
    }

    static func notes(_ text: String) -> [Note] {
        let defs = noteDefs(text)
        let ns = text as NSString
        var seen = Set<Int>()
        return RX.re(#"(?:==([^=\n]+)==)?\[\^note-(\d+)\](?!:)"#).matches(in: text, range: NSRange(location: 0, length: ns.length)).compactMap { m in
            guard let id = Int(ns.substring(with: m.range(at: 2))), let body = defs[id], seen.insert(id).inserted else { return nil }
            let quote = m.range(at: 1).location == NSNotFound ? "" : ns.substring(with: m.range(at: 1))
            return Note(id: id, quote: quote, text: body)
        }
    }

    static func noteDefs(_ text: String) -> [Int: String] {
        var out: [Int: String] = [:]
        let ns = text as NSString
        for m in RX.re(#"(?m)^\[\^note-(\d+)\]:[ \t]?(.*)$"#).matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            if let id = Int(ns.substring(with: m.range(at: 1))) { out[id] = ns.substring(with: m.range(at: 2)) }
        }
        return out
    }

    /// 给选中的字加批注：选中的字划上（碰到已划的就并进去），后面跟脚注号，文末写批注
    static func annotated(_ text: String, block: String, selected: String, note: String) -> String? {
        let sel = selected.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = oneLine(note)
        guard !sel.isEmpty, !body.isEmpty, let b = blockRange(text, block) else { return nil }
        let block = String(text[b])
        guard let found = sourceRange(sel, in: block) else { return nil }
        var s = balanced(found, in: block)
        // 选中范围碰到的已有划线，整段并进来
        for r in block.ranges(of: #/==[^=\n]+==/#) where r.overlaps(s) || r.contains(s.lowerBound) {
            s = min(r.lowerBound, s.lowerBound)..<max(r.upperBound, s.upperBound)
        }
        s = balanced(s, in: block)
        // 跨了已有的批注号：并进来会弄丢原来那条批注对应的字，不加
        guard !block[s].contains("[^note-") else { return nil }
        // 已经跟着的批注号挪到新批注号前面，不拆开
        var tail = block[s.upperBound...]
        var keep = ""
        while let m = tail.prefixMatch(of: #/\[\^note-\d+\]/#) { keep += m.output; tail = tail[m.range.upperBound...] }
        let id = (noteDefs(text).keys.max() ?? 0) + 1
        let inner = String(block[s]).replacingOccurrences(of: "==", with: "")
        let newBlock = String(block[..<s.lowerBound]) + "==\(inner)==" + keep + "[^note-\(id)]" + tail
        var out = text.replacingCharacters(in: b, with: newBlock)
        out = out.trimmingCharacters(in: .newlines) + "\n"
        if noteDefs(out).isEmpty { out += "\n" }
        return out + "[^note-\(id)]: \(body)\n"
    }

    // MARK: 读后感

    /// 读后感：文件末尾「## 读后感」一节（在「阅读原文」后面、批注脚注前面），Obsidian 里也看得到
    static func reflection(_ text: String) -> String {
        guard let r = reflectionRange(text) else { return "" }
        let body = String(text[r]).split(separator: "\n", omittingEmptySubsequences: false).dropFirst()
            .map { $0.hasPrefix("\\#") || $0.hasPrefix("\\[^") ? String($0.dropFirst()) : String($0) }
        return body.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 写上（或改掉、清空）读后感
    static func withReflection(_ text: String, _ body: String) -> String {
        var out = text
        if let r = reflectionRange(out) {
            // 只收拾删掉的这一节前后的空行，正文别处（比如代码块里）的空行不动
            let head = String(out[..<r.lowerBound]).trimmingTrailingNewlines()
            let tail = String(out[r.upperBound...].drop(while: \.isNewline))
            out = tail.isEmpty ? head + "\n" : head + "\n\n" + tail
        }
        // 自己写的一行要是以 # 或 [^ 开头，会被当成下一节或批注脚注，前面加个反斜杠（Markdown 里照样显示原字）
        let b = body.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: "\n", omittingEmptySubsequences: false)
            .map { $0.hasPrefix("#") || $0.hasPrefix("[^") ? "\\" + $0 : String($0) }.joined(separator: "\n")
        guard !b.isEmpty else { return out.trimmingCharacters(in: .newlines) + "\n" }
        let section = "## 读后感\n\n\(b)\n\n"
        // 放在批注的脚注前面；没有脚注就放在最后
        if let def = out.range(of: #"(?m)^\[\^note-\d+\]:"#, options: .regularExpression) {
            out.insert(contentsOf: section, at: def.lowerBound)
            return out
        }
        return out.trimmingCharacters(in: .newlines) + "\n\n" + section.trimmingCharacters(in: .newlines) + "\n"
    }

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

    /// 改一条批注
    static func renoted(_ text: String, id: Int, note: String) -> String? {
        let body = oneLine(note)
        guard !body.isEmpty, let r = text.range(of: #"(?m)^\[\^note-\#(id)\]:.*$"#, options: .regularExpression) else { return nil }
        return text.replacingCharacters(in: r, with: "[^note-\(id)]: \(body)")
    }

    /// 删一条批注：脚注号和文末那行去掉，划线留着
    static func unnoted(_ text: String, id: Int) -> String {
        text.replacingOccurrences(of: #"(?m)^\[\^note-\#(id)\]:.*\n?"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: "[^note-\(id)]", with: "")
            .trimmingCharacters(in: .newlines) + "\n"
    }

    /// 脚注只能写一行：换行换成空格
    private static func oneLine(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: #"\s*\n\s*"#, with: " ", options: .regularExpression)
    }

    /// 把摘要写进一篇剪藏：换掉「## 摘要」，文件头的 tldr、tags 跟着换
    static func withSummary(_ text: String, _ s: Clips.Summary) -> String {
        var lines = text.components(separatedBy: "\n")
        if lines.first == "---", let end = lines.dropFirst().firstIndex(of: "---") {
            var head = Array(lines[1..<end]).filter { !$0.hasPrefix("tags:") && !$0.hasPrefix("tldr:") }
            head.append("tags: [" + s.tags.map(Clips.yaml).joined(separator: ", ") + "]")
            head.append("tldr: \(Clips.yaml(s.tldr))")
            lines.replaceSubrange(1..<end, with: head)
        }
        var out = lines.joined(separator: "\n")
        let section = "## 摘要\n\n\(s.tldr)\n\n" + (s.body.isEmpty ? "" : s.body + "\n\n")
        if let r = out.range(of: #"(?s)## 摘要\n.*?(?=## 正文)"#, options: .regularExpression) {
            out.replaceSubrange(r, with: section)
        } else if let r = out.range(of: "## 正文") {
            out.insert(contentsOf: section, at: r.lowerBound)
        }
        return out
    }

    /// 复盘末尾接一段追问
    static func withFollowUp(_ text: String, question: String, answer: String) -> String {
        let marker = "<!-- sources:"
        let piece = "## 追问：\(question)\n\n\(answer.trimmingCharacters(in: .whitespacesAndNewlines))\n\n"
        if let r = text.range(of: marker) { return text.replacingCharacters(in: r.lowerBound..<r.lowerBound, with: piece) }
        return text.trimmingCharacters(in: .newlines) + "\n\n" + piece
    }
}

/// 文章页交给 Markdown 显示层的操作
struct MDEdit {
    var retryImage: ((String) async -> String?)? = nil   // 剪藏才有：返回失败原因，成功返回 nil
    var removeImage: ((String) -> Void)? = nil
    /// 选中的字划上。一次选中可以跨几段：每段一项（这一段在文件里的原文，这一段里选中的字），一起改、存一次
    var mark: ((_ pieces: [(block: String, selected: String)]) -> Void)?
    /// 去掉这几处划线，同样可以跨几段
    var unmark: ((_ pieces: [(block: String, marked: [String])]) -> Void)?
    var annotate: ((_ block: String, _ selected: String) -> Void)?  // 给选中的字写批注
}

extension EnvironmentValues {
    @Entry var mdEdit: MDEdit? = nil
}

extension Vault {
    /// 手机上改过的一篇：存好，交电脑覆盖。发件箱里这篇还没交出去的旧版本拿掉，留着会把新改的盖回去
    @discardableResult
    /// start：改的起点（编辑页打开时看到的那份）。不给就用手机上现在这份：编辑页开着的时候同步进来电脑上的改动，
    /// 起点仍是打开时那份，bases 照它写，电脑那边才认得出两边都改过
    func saveEdited(path: String, text: String, images: [String: String] = [:], from start: String? = nil) async throws -> Route {
        // 起点：手机上看到的那份（还有没确认的修改时是那份修改，见 PendingEdits）
        let from = start ?? localText(path)
        let review = path.hasPrefix(Library.reviewsDir)   // 复盘是手机上那一份说了算，不带 bases
        let onMac = tree[path].map { $0.sha != "local" } ?? false
        let previous = pending[path]
        // 先记下这次修改（交不出去、App 被关、同步把文件盖了，手机上这一版都还在）。
        // bases：电脑上可能还是的那几版——最早改之前那版，加上之前每次手机这份（电脑可能已经收下了前一次）。
        // 复盘是手机上那一份说了算，不带
        let rec0 = PendingEdits.edited(previous: previous, startText: from, onMac: onMac && !review, content: text,
                                      now: Int64(Date().timeIntervalSince1970 * 1000), chainNew: !review)
        setPending(path, rec0)
        if onMac || !(previous?.bases.isEmpty ?? true) { try writeEdited(path: path, text: text) } else { try addGenerated(path: path, text: text) }
        var all = images
        for item in Outbox.items() where item.task["type"] as? String == "save" && item.task["path"] as? String == path {
            // 发件箱里同一篇旧的那份：这次是在它上面改的才拿掉（它的改动已经在这次里）；不是的两份都交，不悄悄丢
            guard PendingEdits.mayDrop(pendingContent: item.task["content"] as? String, editedFrom: from) else { continue }
            for (k, v) in item.task["images"] as? [String: String] ?? [:] where all[k] == nil { all[k] = v }
            Outbox.remove(item.id)
        }
        // 改的是电脑上也有的一篇：带上 bases（base 是第一个，给旧版电脑），电脑上那篇是其中一版才覆盖，
        // 电脑上后来也改过（重新生成、在 Mac 上划了线）就两边都留着，不互相盖掉（core/remote_tasks.save_file）
        var rec = rec0
        if Clips.isClip(path), !all.isEmpty {   // 原图地址记进待确认记录：再交、换名字重交时照带
            rec.images = rec.images.merging(all) { _, new in new }
            setPending(path, rec)
        }
        let (route, id) = try await submitTaskWithID(pendingTask(path, rec, images: all))
        if pending[path]?.editedAt == rec.editedAt { setPending(path, PendingEdits.submitted(rec, taskID: id)) }
        return route
    }

    /// 重新下载剪藏里的一张图：打开原文找到这张图的地址再下。手机上下好后，电脑也按这个地址下一份
    func retryClipImage(path: String, src: String) async -> String? {
        guard let text = localText(path), let url = URL(string: Clips.meta(path: path, text: text).url), !url.absoluteString.isEmpty else {
            return "这篇没有记原文地址"
        }
        guard let article = try? await Clipper.fetch(url) else { return "原文打不开，过一会儿再试" }
        guard let remote = article.images[src] else { return "原文里找不到这张图" }
        let tmp = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString, directoryHint: .isDirectory)
        defer { try? FileManager.default.removeItem(at: tmp) }
        guard await Clipper.downloadImages([src: remote], into: tmp) == 1, let data = try? Data(contentsOf: tmp.appending(path: src)) else {
            return "图片还是下不下来"
        }
        let key = "\(Clips.folder)/\(src)"
        do {
            try addGenerated(path: key, data: data)
            try await saveEdited(path: path, text: text, images: [key: remote])
            return nil
        } catch {
            return error.localizedDescription
        }
    }
}

/// 可以直接选字划重点的一段正文。长按选中文字，系统菜单里多一项「划重点」；选中已划的字，变成「取消划线」。
/// iPad 上用笔横着在字上一划，直接划上；在已划的字上再划一次是取消。手指照旧：滚动、长按选字。
/// 只在能改的剪藏里用，别处还是 SwiftUI 的 Text。
/// 连着的几段正文放在同一个文本框里（paragraphs 有好几项），选字、用笔划线才能跨段；回调里的下标是第几段
struct MarkableText: UIViewRepresentable {
    typealias Piece = (para: Int, text: String)
    let paragraphs: [AttributedString]
    var firstIndex = 0                    // 第一段是文章的第几块（目录、笔记跳到某一段时用）
    let font: UIFont
    let color: UIColor
    let linkColor: UIColor
    let lineSpacing: Double
    var paraSpacing: Double = 0
    let onMark: ([Piece]) -> Void                       // 每段里选中的字（显示出来的样子）
    let onUnmark: ([(para: Int, marked: [String])]) -> Void   // 选中范围碰到的已划的字
    var onAnnotate: (([Piece]) -> Void)? = nil          // 给选中的字写批注
    var onOpenURL: ((URL) -> Void)? = nil               // 点了批注号这类站内链接

    /// 文章页要滚到某一块（object 是块的下标）：落在一个多段文本框中间的，由这个文本框自己滚到那一段
    static let scrollToBlock = Notification.Name("spark.scrollToBlock")

    func makeCoordinator() -> Coordinator { Coordinator() }

    /// 笔常从页边起笔、划出页边才抬：左右各多认一截，落在页边的笔也归这段字
    final class PenTextView: UITextView {
        override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
            bounds.insetBy(dx: -32, dy: 0).contains(point)
        }

        var bubbleColor = UIColor.systemBlue

        /// 主题的强调色偏深（深蓝、深棕），画成小图标近乎黑色：色相不变，提亮、提饱和度
        static func vivid(_ c: UIColor) -> UIColor {
            var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            guard c.getHue(&h, saturation: &s, brightness: &b, alpha: &a) else { return c }
            return UIColor(hue: h, saturation: max(s, 0.7), brightness: max(b, 0.9), alpha: a)
        }
        private var bubbles: [CALayer] = []

        /// 批注号画成一个强调色的小对话气泡（原来那个笔的字符留着占位，不显示）
        override func layoutSubviews() {
            super.layoutSubviews()
            bubbles.forEach { $0.removeFromSuperlayer() }
            bubbles = []
            guard let text = attributedText, text.length > 0 else { return }
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            text.enumerateAttribute(MarkableText.noteKey, in: NSRange(location: 0, length: text.length)) { val, r, _ in
                guard val != nil, let from = position(from: beginningOfDocument, offset: r.location),
                      let to = position(from: from, offset: r.length), let tr = textRange(from: from, to: to) else { return }
                let rect = firstRect(for: tr)
                guard rect.width > 0, rect.height > 0, !rect.isInfinite, !rect.isNull else { return }
                let cfg = UIImage.SymbolConfiguration(pointSize: (font?.pointSize ?? 17) * 0.68, weight: .medium)
                // 样子照 💬（带三个点的气泡）；用系统图标画，颜色才能跟着主题走（表情符号的颜色是固定的灰白）
                guard let img = UIImage(systemName: "ellipsis.bubble.fill", withConfiguration: cfg) else { return }
                // 系统图标取出来的位图只有形状、没有颜色（直接当内容画出来是黑的）：拿它当蒙版，底下铺要的颜色
                let shape = CALayer()
                shape.contents = img.cgImage
                shape.contentsScale = img.scale
                shape.contentsGravity = .resizeAspect
                shape.frame = CGRect(origin: .zero, size: img.size)
                let l = CALayer()
                l.backgroundColor = Self.vivid(bubbleColor).cgColor
                l.mask = shape
                // 略往上提一点，像角标，不压着字的基线
                l.frame = CGRect(x: rect.midX - img.size.width / 2, y: rect.midY - img.size.height / 2 - rect.height * 0.08,
                                 width: img.size.width, height: img.size.height)
                layer.addSublayer(l)
                bubbles.append(l)
            }
            CATransaction.commit()
        }
    }

    /// 批注号：值是「spark-note:编号」。不走系统的链接（链接的颜色统一、藏不掉那个字符），点按由这里自己接
    static let noteKey = NSAttributedString.Key("spark.note")
    /// 批注号前后垫的细空格（只为了排版，原文里没有，取选中的字时要去掉）
    static let thin = "\u{200A}"

    func makeUIView(context: Context) -> UITextView {
        let v = PenTextView()
        v.isEditable = false
        v.isSelectable = true
        v.isScrollEnabled = false
        v.backgroundColor = .clear
        v.textContainerInset = .zero
        v.textContainer.lineFragmentPadding = 0
        v.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        v.delegate = context.coordinator
        // 只收笔：手指的触摸这个手势根本不接，滚动、长按选字都不受影响
        let pen = UIPanGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.penStroke(_:)))
        pen.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.pencil.rawValue)]
        pen.maximumNumberOfTouches = 1
        pen.delegate = context.coordinator
        v.addGestureRecognizer(pen)
        // 点一下已划的字：弹出小菜单（批注、取消划线、拷贝），手指和笔都行
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapMark(_:)))
        tap.delegate = context.coordinator
        v.addGestureRecognizer(tap)
        let menu = UIEditMenuInteraction(delegate: context.coordinator)
        v.addInteraction(menu)
        context.coordinator.markMenu = menu
        context.coordinator.listen(v)
        return v
    }

    static func dismantleUIView(_ v: UITextView, coordinator: Coordinator) { coordinator.stopListening() }

    func updateUIView(_ v: UITextView, context: Context) {
        context.coordinator.parent = self
        let (ns, ranges) = attributed()
        context.coordinator.paraRanges = ranges
        (v as? PenTextView)?.bubbleColor = linkColor
        if v.attributedText != ns { v.attributedText = ns; v.setNeedsLayout() }
        v.linkTextAttributes = [.foregroundColor: linkColor]
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UITextView, context: Context) -> CGSize? {
        let w = proposal.width ?? UIScreen.main.bounds.width
        let h = uiView.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height
        return CGSize(width: w, height: ceil(h))
    }

    static let markColor = UIColor.systemYellow.withAlphaComponent(0.35)

    /// SwiftUI 的样式（加粗、斜体、代码、链接、划线底色）换成 UIKit 的；几段之间用换行隔开，顺带记下每段的范围
    private func attributed() -> (NSAttributedString, [NSRange]) {
        let para = NSMutableParagraphStyle()
        para.lineSpacing = lineSpacing
        para.paragraphSpacing = paraSpacing
        let out = NSMutableAttributedString()
        var ranges: [NSRange] = []
        for (i, text) in paragraphs.enumerated() {
            if i > 0 { out.append(NSAttributedString(string: "\n", attributes: [.font: font, .paragraphStyle: para])) }
            let start = out.length
            append(text, to: out, para: para)
            ranges.append(NSRange(location: start, length: out.length - start))
        }
        return (out, ranges)
    }

    private func append(_ text: AttributedString, to out: NSMutableAttributedString, para: NSParagraphStyle) {
        for run in text.runs {
            let piece = String(text[run.range].characters)
            var f = font
            if let intent = run.inlinePresentationIntent {
                var traits: UIFontDescriptor.SymbolicTraits = []
                if intent.contains(.stronglyEmphasized) { traits.insert(.traitBold) }
                if intent.contains(.emphasized) { traits.insert(.traitItalic) }
                if intent.contains(.code) {
                    f = .monospacedSystemFont(ofSize: font.pointSize * 0.9, weight: .regular)
                } else if !traits.isEmpty, let d = font.fontDescriptor.withSymbolicTraits(traits) {
                    f = UIFont(descriptor: d, size: font.pointSize)
                } else if traits.contains(.traitBold) {
                    f = .systemFont(ofSize: font.pointSize, weight: .semibold)
                }
            }
            var attrs: [NSAttributedString.Key: Any] = [.font: f, .foregroundColor: color, .paragraphStyle: para]
            if let url = run.link, url.scheme == Markdown.noteScheme {
                // 批注号：字符本身不显示，只占位置；气泡在 PenTextView 里画。前后留一点空，不跟字挤在一起
                attrs[Self.noteKey] = url
                attrs[.font] = UIFont.systemFont(ofSize: font.pointSize * 0.7)
                attrs[.foregroundColor] = UIColor.clear
                let gap = NSAttributedString(string: Self.thin, attributes: [.font: f, .paragraphStyle: para, .kern: 2])
                out.append(gap)
                out.append(NSAttributedString(string: piece, attributes: attrs))
                out.append(gap)
                continue
            }
            if let url = run.link { attrs[.link] = url }
            if run.backgroundColor != nil { attrs[.backgroundColor] = Self.markColor }
            out.append(NSAttributedString(string: piece, attributes: attrs))
        }
    }

    final class Coordinator: NSObject, UITextViewDelegate, UIGestureRecognizerDelegate, UIEditMenuInteractionDelegate {
        var markMenu: UIEditMenuInteraction?
        private var tapped: (para: Int, range: NSRange, text: String)?

        /// 点的地方是已划的字（不是批注号、链接）就弹菜单；点在别处什么都不做，系统自己的点按照旧
        @objc func tapMark(_ g: UITapGestureRecognizer) {
            guard let v = g.view as? UITextView, v.selectedRange.length == 0 else { return }
            let pt = g.location(in: v)
            guard let tr = v.characterRange(at: pt) else { return }
            let i = v.offset(from: v.beginningOfDocument, to: tr.start)
            let text = v.attributedText!
            // 点在气泡上（它小，左右各让两个字符的位置）：打开这条批注
            for j in [i, i - 1, i + 1, i - 2, i + 2] where j >= 0 && j < text.length {
                if let url = text.attribute(MarkableText.noteKey, at: j, effectiveRange: nil) as? URL { parent?.onOpenURL?(url); return }
            }
            guard i >= 0, i < text.length, text.attribute(.link, at: i, effectiveRange: nil) == nil,
                  let p = paraRanges.firstIndex(where: { NSLocationInRange(i, $0) }) else { return }
            // 这一处划线在这一段里的整个范围（中间的加粗、批注号把底色切成了几截，接起来）
            var hit: NSRange?
            text.enumerateAttribute(.backgroundColor, in: paraRanges[p]) { val, r, _ in
                guard val != nil else { return }
                if let h = hit, NSMaxRange(h) == r.location { hit = NSUnionRange(h, r) }
                else if hit == nil || !NSLocationInRange(i, hit!) { hit = r }
            }
            guard let range = hit, NSLocationInRange(i, range) else { return }
            tapped = (p, range, (text.string as NSString).substring(with: range).replacingOccurrences(of: MarkableText.thin, with: ""))
            markMenu?.presentEditMenu(with: UIEditMenuConfiguration(identifier: "spark.mark", sourcePoint: pt))
        }

        func editMenuInteraction(_ interaction: UIEditMenuInteraction, menuFor configuration: UIEditMenuConfiguration,
                                 suggestedActions: [UIMenuElement]) -> UIMenu? {
            guard let parent, let t = tapped else { return nil }
            var items: [UIMenuElement] = []
            if let annotate = parent.onAnnotate {
                items.append(UIAction(title: "批注", image: UIImage(systemName: "square.and.pencil")) { _ in annotate([(t.para, t.text)]) })
            }
            items.append(UIAction(title: "取消划线", image: UIImage(systemName: "eraser")) { _ in parent.onUnmark([(t.para, [t.text])]) })
            items.append(UIAction(title: "拷贝", image: UIImage(systemName: "doc.on.doc")) { _ in
                UIPasteboard.general.string = t.text.replacingOccurrences(of: String(DocEdit.Note.glyph), with: "")
            })
            return UIMenu(children: items)
        }

        func editMenuInteraction(_ interaction: UIEditMenuInteraction, targetRectFor configuration: UIEditMenuConfiguration) -> CGRect {
            guard let v = interaction.view as? UITextView, let t = tapped,
                  let from = v.position(from: v.beginningOfDocument, offset: t.range.location),
                  let to = v.position(from: from, offset: t.range.length), let tr = v.textRange(from: from, to: to) else { return .null }
            // 菜单贴着点的那一行，不贴着整处划线的外框（多行时外框太大，菜单会跑远）
            let rects = v.selectionRects(for: tr).map(\.rect).filter { $0.width > 0 }
            return rects.first { $0.insetBy(dx: -4, dy: -6).contains(configuration.sourcePoint) } ?? rects.first ?? .null
        }

        func gestureRecognizer(_ g: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
            g is UITapGestureRecognizer
        }

        var parent: MarkableText?
        var paraRanges: [NSRange] = []
        private weak var view: UITextView?
        private var token: NSObjectProtocol?

        func listen(_ v: UITextView) {
            view = v
            token = NotificationCenter.default.addObserver(forName: MarkableText.scrollToBlock, object: nil, queue: .main) { [weak self] n in
                guard let i = n.object as? Int else { return }
                MainActor.assumeIsolated { self?.scroll(toBlock: i) }
            }
        }

        func stopListening() { if let token { NotificationCenter.default.removeObserver(token) }; token = nil }

        /// 要去的那一块是这个文本框里的第几段（不是第一段）：把外面的滚动视图滚到那一段的位置
        private func scroll(toBlock i: Int) {
            guard let parent, let v = view, i > parent.firstIndex, i - parent.firstIndex < paraRanges.count else { return }
            let r = paraRanges[i - parent.firstIndex]
            guard let from = v.position(from: v.beginningOfDocument, offset: r.location),
                  let to = v.position(from: from, offset: min(1, r.length)), let tr = v.textRange(from: from, to: to) else { return }
            var s: UIView? = v.superview
            while let cur = s, !(cur is UIScrollView) { s = cur.superview }
            guard let scroll = s as? UIScrollView else { return }
            let y = v.convert(v.firstRect(for: tr), to: scroll).minY - 12
            let top = -scroll.adjustedContentInset.top
            let bottom = max(top, scroll.contentSize.height + scroll.adjustedContentInset.bottom - scroll.bounds.height)
            scroll.setContentOffset(CGPoint(x: scroll.contentOffset.x, y: min(max(y + top, top), bottom)), animated: true)
        }

        /// 选中的范围落在每一段里的字
        private func pieces(_ v: UITextView, _ range: NSRange) -> [Piece] {
            let ns = v.attributedText.string as NSString
            return paraRanges.enumerated().compactMap { i, pr in
                let r = NSIntersectionRange(pr, range)
                guard r.length > 0 else { return nil }
                let t = ns.substring(with: r).replacingOccurrences(of: MarkableText.thin, with: "")
                return t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : (i, t)
            }
        }

        /// 选中的范围碰到的已划的字，按段分；covered 是其中落在选中范围里的字数
        private func marked(_ v: UITextView, _ range: NSRange) -> (hits: [(para: Int, marked: [String])], covered: Int) {
            let ns = v.attributedText.string as NSString
            var out: [(para: Int, marked: [String])] = [], covered = 0
            for (i, pr) in paraRanges.enumerated() {
                var hit: [String] = []
                v.attributedText.enumerateAttribute(.backgroundColor, in: pr) { val, r, _ in
                    let n = NSIntersectionRange(r, range).length
                    if val != nil, n > 0 { hit.append(ns.substring(with: r).replacingOccurrences(of: MarkableText.thin, with: "")); covered += n }
                }
                if !hit.isEmpty { out.append((i, hit)) }
            }
            return (out, covered)
        }

        private var penStart: CGPoint?
        private var penTrail: [(at: CFTimeInterval, point: CGPoint)] = []
        private var penLayer: CAShapeLayer?

        /// 笔横着起笔才算划线；竖着拖还是滚动页面
        func gestureRecognizerShouldBegin(_ g: UIGestureRecognizer) -> Bool {
            guard let pan = g as? UIPanGestureRecognizer, let v = pan.view else { return true }
            let d = pan.velocity(in: v)
            return abs(d.x) > abs(d.y)
        }

        /// 笔在划线时，系统自己的选字、外面的滚动先等着，免得一边划一边选字或滚走
        func gestureRecognizer(_ g: UIGestureRecognizer, shouldBeRequiredToFailBy other: UIGestureRecognizer) -> Bool {
            g is UIPanGestureRecognizer
        }

        @objc func penStroke(_ g: UIPanGestureRecognizer) {
            guard let v = g.view as? UITextView else { return }
            let now = g.location(in: v)
            switch g.state {
            case .began:
                // 起点按落笔的位置算，不按手势认出来时已经挪到的位置
                let t = g.translation(in: v)
                penStart = CGPoint(x: now.x - t.x, y: now.y - t.y)
                showPen(v, to: now)
            case .changed:
                penTrail.append((CACurrentMediaTime(), now))
                if penTrail.count > 40 { penTrail.removeFirst(penTrail.count - 40) }
                showPen(v, to: now)
            case .ended:
                // 抬笔那一下常带个小勾：用抬笔前 0.06 秒的位置
                let t = CACurrentMediaTime() - 0.06
                let range = penRange(v, to: penTrail.last { $0.at <= t }?.point ?? penTrail.first?.point ?? now)
                clearPen()
                if let range { commitPen(v, range) }
            default:
                clearPen()
            }
        }

        /// 笔划过的字。碰到的字都算进去（按笔尖底下的那个字算，不按离哪条字缝近）；
        /// 上下没偏出大半行的当成同一行，免得在行尾、页边手一抖就跳到上一行或下一行；划出页边的按行首、行尾算
        private func penRange(_ v: UITextView, to end: CGPoint) -> NSRange? {
            guard let a = penStart else { return nil }
            var b = end
            let line = (v.font?.lineHeight ?? 20) + (parent?.lineSpacing ?? 0)
            if abs(b.y - a.y) < line * 0.7 { b.y = a.y }
            guard abs(b.x - a.x) > 6 || b.y != a.y else { return nil }
            let forward = b.y > a.y || (b.y == a.y && b.x >= a.x)
            let (p, q) = forward ? (a, b) : (b, a)
            func clamp(_ pt: CGPoint) -> CGPoint { CGPoint(x: min(max(pt.x, 1), v.bounds.width - 1), y: min(max(pt.y, 1), v.bounds.height - 1)) }
            guard let from = v.characterRange(at: clamp(p))?.start ?? v.closestPosition(to: clamp(p)),
                  let to = v.characterRange(at: clamp(q))?.end ?? v.closestPosition(to: clamp(q)) else { return nil }
            let i = v.offset(from: v.beginningOfDocument, to: from), j = v.offset(from: v.beginningOfDocument, to: to)
            return j > i ? NSRange(location: i, length: j - i) : nil
        }

        /// 划的过程中铺一层黄底，松开才真的写进文件
        private func showPen(_ v: UITextView, to end: CGPoint) {
            guard let r = penRange(v, to: end), let from = v.position(from: v.beginningOfDocument, offset: r.location),
                  let to = v.position(from: from, offset: r.length), let tr = v.textRange(from: from, to: to) else { penLayer?.path = nil; return }
            let layer = penLayer ?? {
                let l = CAShapeLayer()
                l.fillColor = UIColor.systemYellow.withAlphaComponent(0.4).cgColor
                v.layer.addSublayer(l)
                penLayer = l
                return l
            }()
            let path = UIBezierPath()
            for s in v.selectionRects(for: tr) where s.rect.width > 0 { path.append(UIBezierPath(roundedRect: s.rect, cornerRadius: 2)) }
            layer.path = path.cgPath
        }

        private func clearPen() {
            penLayer?.removeFromSuperlayer()
            penLayer = nil
            penStart = nil
            penTrail = []
        }

        /// 划过的地方全是已划的字：取消；否则划上
        private func commitPen(_ v: UITextView, _ range: NSRange) {
            guard let parent else { return }
            let range = NSIntersectionRange(range, NSRange(location: 0, length: v.attributedText.length))
            let sel = pieces(v, range)
            guard !sel.isEmpty else { return }
            let (hits, covered) = marked(v, range)
            // 段与段之间的换行不算字
            var chars = 0
            for pr in paraRanges {
                let r = NSIntersectionRange(pr, range)
                guard r.length > 0 else { continue }
                // 批注号和它前后的细空格不是划的字，不算
                v.attributedText.enumerateAttribute(MarkableText.noteKey, in: r) { val, sub, _ in
                    if val == nil { chars += (v.attributedText.string as NSString).substring(with: sub).replacingOccurrences(of: MarkableText.thin, with: "").utf16.count }
                }
            }
            if covered == chars { parent.onUnmark(hits) } else { parent.onMark(sel) }
        }

        func textView(_ v: UITextView, editMenuForTextIn range: NSRange, suggestedActions: [UIMenuElement]) -> UIMenu? {
            guard let parent, range.length > 0 else { return nil }
            // 选中范围碰到的已划的字
            let hit = marked(v, range).hits
            let sel = pieces(v, range)
            let action: UIAction
            if hit.isEmpty {
                action = UIAction(title: "划重点", image: UIImage(systemName: "highlighter")) { [weak v] _ in
                    v?.selectedRange = NSRange(location: range.location, length: 0)
                    parent.onMark(sel)
                }
            } else {
                action = UIAction(title: "取消划线", image: UIImage(systemName: "eraser")) { [weak v] _ in
                    v?.selectedRange = NSRange(location: range.location, length: 0)
                    parent.onUnmark(hit)
                }
            }
            var mine: [UIMenuElement] = [action]
            if let annotate = parent.onAnnotate, !sel.isEmpty {
                mine.append(UIAction(title: "批注", image: UIImage(systemName: "square.and.pencil")) { [weak v] _ in
                    v?.selectedRange = NSRange(location: range.location, length: 0)
                    annotate(sel)
                })
            }
            return UIMenu(children: mine + suggestedActions)
        }

        /// 批注号（spark-note:3）不交给系统打开，交给文章页弹出批注
        func textView(_ v: UITextView, primaryActionFor item: UITextItem, defaultAction: UIAction) -> UIAction? {
            guard case .link(let url) = item.content, url.scheme == Markdown.noteScheme, let open = parent?.onOpenURL else { return defaultAction }
            return UIAction { _ in open(url) }
        }
    }
}

/// 综合小结继续追问：答案接在这份小结末尾，以后打开还在
struct FollowUpSheet: View {
    let path: String
    let onSaved: () -> Void
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var question = ""
    @State private var answer: String?
    @State private var asking = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("想接着问什么", text: $question, axis: .vertical).lineLimit(2...5)
                } footer: {
                    Text(jobs.config == nil ? "要先在「我的」里填模型 Key。" : "根据这份小结和当初选的文章回答，答案接在小结末尾。")
                }
                if let error { Text(error).foregroundStyle(.red) }
                if asking {
                    HStack(spacing: 8) { ProgressView(); Text("正在想").foregroundStyle(p.ink2) }
                }
                if let answer {
                    Section("回答") { Text(Markdown.inline(answer)).textSelection(.enabled) }
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("继续追问").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("提问") { Task { await ask() } }
                        .disabled(asking || question.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || jobs.config == nil)
                }
            }
        }
    }

    private func ask() async {
        guard let config = jobs.config, let text = vault.localText(path) else { return }
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        asking = true
        error = nil
        defer { asking = false }
        do {
            let a = try await ReviewRunner.followUp(doc: text, question: q, lib: LocalNotes(root: Vault.root), config: config)
            try await vault.saveEdited(path: path, text: DocEdit.withFollowUp(text, question: q, answer: a))
            answer = a
            question = ""
            onSaved()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private extension String {
    func trimmingTrailingNewlines() -> String {
        var s = self[...]
        while let c = s.last, c.isNewline { s = s.dropLast() }
        return String(s)
    }
}
