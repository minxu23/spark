import PDFKit
import SwiftUI

/// 论文的「追问」：只针对这一篇多轮问答（提示词在 PaperRead，跟电脑 core/paper_read.py 逐字一致）。
/// 对话界面是讨论的那个（ChatView）；「存进本文」把这次新问的几组交给阅读页去存（onSave：写进文件末尾的「## 追问」一节，
/// 走阅读页的保存，跟划线、改名一样交给电脑）。用模型的条件同重写摘要、翻译：填了模型 Key 就行
struct PaperAskView: View {
    let path: String
    let onSave: @MainActor ([DiscussPrompt.Turn]) throws -> Void
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            Group {
                if let config = jobs.config {
                    ChatView(
                        hint: "只根据这篇论文回答，文中没有的会直接说没有。问完点「存进本文」，问答写进这篇末尾的「追问」一节，在「导读」页签里看。",
                        ask: { q, history, progress in
                            guard let text = vault.localText(path) else { throw SparkError("手机上没有这篇的文件") }
                            return try await PaperAskRunner.ask(text: text, question: q, history: history, config: config, progress: progress)
                        },
                        save: { turns in
                            try onSave(turns.map { DiscussPrompt.Turn(q: $0.q, a: $0.a) })
                            return "已存进本文：在「导读」页签末尾的「追问」一节。"
                        },
                        saveLabel: "存进本文", incremental: true)
                } else {
                    VStack(spacing: 14) {
                        Text("追问要调用模型：先在设置里填好模型的 Key。")
                            .font(.system(size: 14)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
                        Button("去设置") { showSettings = true }.buttonStyle(.borderedProminent).tint(p.accent)
                    }
                    .padding(28).frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .pageStyle()
            .navigationTitle("追问").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            .sheet(isPresented: $showSettings) { ModelSettingsView() }
        }
    }
}

enum PaperAskRunner {
    /// 问一个问题：这篇的正文（参考文献前面、截断过的）连同之前几轮交给模型，只根据这篇回答
    static func ask(text: String, question: String, history: [DiscussPrompt.Turn], config c: LLM.Config,
                    progress: @escaping @Sendable (String) -> Void) async throws -> DiscussRunner.Answer {
        let paper = PaperRead.paperText(text)
        guard !paper.isEmpty else { throw SparkError("这篇论文还没有正文，没法追问") }
        let title = PaperRead.front(text)["title"] ?? ""
        progress("读这篇论文，写回答")
        let a = try await LLM.complete(PaperRead.answerPrompt(title: title, paper: paper, question: question, history: history),
                                       config: c, maxTokens: 3000, timeout: 1200)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return DiscussRunner.Answer(text: a.isEmpty ? PaperRead.notInPaper : a, sources: [])
    }
}

/// 分享出去的论文 PDF：文件名用论文标题（笔记库里的名字是一串哈希），系统拷一份给对方
struct PaperPDFFile: Transferable {
    let url: URL
    let name: String

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: .pdf) { SentTransferredFile($0.url) }
            .suggestedFileName { $0.fileName }
    }

    /// 文件名里不能有的字符换成空格，太长截到 80 字；空的用「论文」
    var fileName: String {
        let clean = name.components(separatedBy: CharacterSet(charactersIn: "/\\:*?\"<>|\n\r")).joined(separator: " ")
            .trimmingCharacters(in: .whitespaces)
        return (clean.isEmpty ? "论文" : String(clean.prefix(80))) + ".pdf"
    }
}

/// 论文 PDF 页签：PDFKit 的 PDFView 显示原件（手机上确实有的那份），连续往下翻、按宽度缩放
struct PDFPage: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.displayMode = .singlePageContinuous
        v.displayDirection = .vertical
        v.document = PDFDocument(url: url)
        return v
    }

    func updateUIView(_ v: PDFView, context: Context) {
        if v.document?.documentURL != url { v.document = PDFDocument(url: url) }
    }
}
