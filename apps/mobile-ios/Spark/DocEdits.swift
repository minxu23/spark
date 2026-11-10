import SwiftUI
import UIKit

/// 手机上改一篇剪藏或复盘：改名、划重点、删图、写追问。文件内容的改法都在这里，改完由 Vault.saveEdited 存好并交给电脑。
enum DocEdit {
    /// 能在手机上改的：剪藏和复盘
    static func editable(_ path: String) -> Bool {
        Clips.isClip(path) || (path.hasPrefix(Library.reviewsDir) && path.hasSuffix(".md"))
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
        guard !sel.isEmpty, let b = blockRange(text, block), let s = sourceRange(sel, in: block) else { return nil }
        let inner = String(block[s]).replacingOccurrences(of: "==", with: "")
        return text.replacingCharacters(in: b, with: block.replacingCharacters(in: s, with: "==\(inner)=="))
    }

    /// 显示出来的一串字在原文里的位置
    static func sourceRange(_ shown: String, in raw: String) -> Range<String.Index>? {
        if let r = raw.range(of: shown) { return r }
        let gap = #"(?:\*\*|\*|__|_|`|==|~~|\]\([^)]*\)|\[)*"#
        let pattern = shown.map { NSRegularExpression.escapedPattern(for: String($0)) }.joined(separator: gap)
        return raw.range(of: pattern, options: .regularExpression)
    }

    /// 去掉这一段里某几处划线；没指明就整段去掉
    static func unhighlighted(_ text: String, block: String, marked: [String] = []) -> String? {
        guard let b = blockRange(text, block) else { return nil }
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
        let bodyStart = text.range(of: "\n## 正文\n")?.upperBound ?? text.startIndex
        return all.first { whole($0) && $0.lowerBound >= bodyStart } ?? all.first(where: whole) ?? all.first
    }

    /// 文章里划过的重点
    static func marks(_ text: String) -> [String] {
        let ns = text as NSString
        return RX.re(#"==([^=\n]+)=="#).matches(in: text, range: NSRange(location: 0, length: ns.length)).map { ns.substring(with: $0.range(at: 1)) }
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
        let section = "## 摘要\n\n**一句话**：\(s.tldr)\n\n" + (s.body.isEmpty ? "" : s.body + "\n\n")
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
    var retryImage: (String) async -> String?       // 返回失败原因，成功返回 nil
    var removeImage: (String) -> Void
    var mark: ((_ block: String, _ selected: String) -> Void)?      // 选中的字划上
    var unmark: ((_ block: String, _ marked: [String]) -> Void)?    // 去掉这几处划线
}

extension EnvironmentValues {
    @Entry var mdEdit: MDEdit? = nil
}

extension Vault {
    /// 手机上改过的一篇：存好，交电脑覆盖。发件箱里这篇还没交出去的旧版本拿掉，留着会把新改的盖回去
    @discardableResult
    func saveEdited(path: String, text: String, images: [String: String] = [:]) async throws -> Route {
        try addGenerated(path: path, text: text)
        var all = images
        for item in Outbox.items() where item.task["type"] as? String == "save" && item.task["path"] as? String == path {
            for (k, v) in item.task["images"] as? [String: String] ?? [:] where all[k] == nil { all[k] = v }
            Outbox.remove(item.id)
        }
        var task: [String: Any] = ["type": "save", "path": path, "content": text, "replace": true]
        if Clips.isClip(path) { task["images"] = all }
        return try await submitTask((title: "更新笔记库：\((path as NSString).lastPathComponent)", task: task))
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
/// 只在能改的剪藏里用，别处还是 SwiftUI 的 Text
struct MarkableText: UIViewRepresentable {
    let text: AttributedString
    let font: UIFont
    let color: UIColor
    let linkColor: UIColor
    let lineSpacing: Double
    let onMark: (String) -> Void          // 选中的字（显示出来的样子）
    let onUnmark: ([String]) -> Void      // 选中范围碰到的已划的字

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UITextView {
        let v = UITextView()
        v.isEditable = false
        v.isSelectable = true
        v.isScrollEnabled = false
        v.backgroundColor = .clear
        v.textContainerInset = .zero
        v.textContainer.lineFragmentPadding = 0
        v.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        v.delegate = context.coordinator
        return v
    }

    func updateUIView(_ v: UITextView, context: Context) {
        context.coordinator.parent = self
        let ns = attributed()
        if v.attributedText != ns { v.attributedText = ns }
        v.linkTextAttributes = [.foregroundColor: linkColor]
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UITextView, context: Context) -> CGSize? {
        let w = proposal.width ?? UIScreen.main.bounds.width
        let h = uiView.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height
        return CGSize(width: w, height: ceil(h))
    }

    static let markColor = UIColor.systemYellow.withAlphaComponent(0.35)

    /// SwiftUI 的样式（加粗、斜体、代码、链接、划线底色）换成 UIKit 的
    private func attributed() -> NSAttributedString {
        let para = NSMutableParagraphStyle()
        para.lineSpacing = lineSpacing
        let out = NSMutableAttributedString()
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
            if let url = run.link { attrs[.link] = url }
            if run.backgroundColor != nil { attrs[.backgroundColor] = Self.markColor }
            out.append(NSAttributedString(string: piece, attributes: attrs))
        }
        return out
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: MarkableText?

        func textView(_ v: UITextView, editMenuForTextIn range: NSRange, suggestedActions: [UIMenuElement]) -> UIMenu? {
            guard let parent, range.length > 0 else { return nil }
            let ns = v.attributedText.string as NSString
            // 选中范围碰到的已划的字
            var hit: [String] = []
            v.attributedText.enumerateAttribute(.backgroundColor, in: NSRange(location: 0, length: ns.length)) { val, r, _ in
                if val != nil, NSIntersectionRange(r, range).length > 0 { hit.append(ns.substring(with: r)) }
            }
            let action: UIAction
            if hit.isEmpty {
                let sel = ns.substring(with: range)
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
            return UIMenu(children: [action] + suggestedActions)
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
