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

    /// 划重点：这一段里选中的文字用 ==…== 标出来，跟电脑上阅读页、Obsidian 同一种写法
    static func highlighted(_ text: String, block: String, selected: String) -> String? {
        let sel = selected.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sel.isEmpty, let b = text.range(of: block), let s = block.range(of: sel) else { return nil }
        return text.replacingCharacters(in: b, with: block.replacingCharacters(in: s, with: "==\(sel)=="))
    }

    static func unhighlighted(_ text: String, block: String) -> String? {
        guard let b = text.range(of: block) else { return nil }
        return text.replacingCharacters(in: b, with: block.replacingOccurrences(of: "==", with: ""))
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
    var tapBlock: ((String) -> Void)?               // 划重点模式：点一段
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

/// 选中一段里要划的文字
struct SelectableText: UIViewRepresentable {
    let text: String
    let font: UIFont
    let color: UIColor
    @Binding var selected: String

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> UITextView {
        let v = UITextView()
        v.isEditable = false
        v.isSelectable = true
        v.backgroundColor = .clear
        v.delegate = context.coordinator
        return v
    }

    func updateUIView(_ v: UITextView, context: Context) {
        if v.text != text {
            v.text = text
            v.font = font
            v.textColor = color
        }
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        let parent: SelectableText
        init(_ p: SelectableText) { parent = p }
        func textViewDidChangeSelection(_ v: UITextView) {
            let ns = v.text as NSString
            let r = v.selectedRange
            let s = r.length > 0 && r.location + r.length <= ns.length ? ns.substring(with: r) : ""
            DispatchQueue.main.async { self.parent.selected = s }
        }
    }
}

/// 划重点：长按拖选一段里的文字，或者整段都划
struct HighlightSheet: View {
    let block: String
    let onMark: (String) -> Void
    let onClear: () -> Void
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var selected = ""

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                Text("长按文字，拖动选出要划的部分").font(.system(size: 13)).foregroundStyle(p.ink2)
                SelectableText(text: block, font: .systemFont(ofSize: 17), color: UIColor(p.ink), selected: $selected)
                HStack(spacing: 10) {
                    Button(selected.isEmpty ? "整段划重点" : "划选中的") {
                        onMark(selected.isEmpty ? block.replacingOccurrences(of: "==", with: "") : selected)
                        dismiss()
                    }
                    .buttonStyle(.borderedProminent).tint(p.accent)
                    if block.contains("==") {
                        Button("取消这段的划线") { onClear(); dismiss() }.buttonStyle(.bordered)
                    }
                }
            }
            .padding(20)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("划重点").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } } }
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
