import SwiftUI

/// 复盘页「+」→「讨论」、主题页某个标签的文章列表上的「讨论」：从复盘页的导航栈压进来。tag 为空是整个笔记库
struct DiscussRoute: Hashable { let tag: String }

/// 讨论页：基于手机上的笔记库和云端模型多轮问答。能不能用同「在手机上生成复盘」：填了 Key 且开着「在手机上生成」
struct DiscussView: View {
    let tag: String
    var model: TagModel?
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var showSettings = false

    var body: some View {
        Group {
            if jobs.canRun, let config = jobs.config {
                ChatView(
                    hint: tag.isEmpty ? "在手机上的笔记里找相关内容，再回答。回答里的 [编号] 对应下面列出的出处，点出处打开原文。"
                                      : "只在「\(tag)」这个主题下的文章里找。回答里的 [编号] 对应下面列出的出处，点出处打开原文。",
                    ask: { q, history, progress in
                        let picks = model.map { DiscussRunner.tagPicks($0.index.items(for: tag, in: $0.items)) } ?? []
                        return try await DiscussRunner.ask(question: q, history: history, tag: tag, picks: picks,
                                                           lib: LocalNotes(root: Vault.root), config: config, progress: progress)
                    },
                    save: { turns in try await DiscussRunner.save(turns: turns, tag: tag, config: config, vault: vault, jobs: jobs) })
            } else {
                VStack(spacing: 14) {
                    Text("讨论要在手机上调用模型：先在设置里填好模型的 Key，并打开「复盘和出报告在手机上生成」。")
                        .font(.system(size: 14)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
                    Button("去设置") { showSettings = true }.buttonStyle(.borderedProminent).tint(p.accent)
                }
                .padding(28).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .pageStyle()
        .navigationTitle(tag.isEmpty ? "讨论" : "讨论：\(tag)").navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showSettings) { ModelSettingsView() }
    }
}

/// 对话界面：给一个「怎么回答一个问题」和一个「怎么保存」就能用（讨论、论文的「追问」共用，换一套闭包）。
/// 对话只在这个页面里，不存；点保存按钮（讨论「存成复盘」，追问「存进本文」）才写进文件
struct ChatView: View {
    var hint = ""
    let ask: @MainActor (_ question: String, _ history: [DiscussPrompt.Turn], _ progress: @escaping @Sendable (String) -> Void) async throws -> DiscussRunner.Answer
    let save: @MainActor (_ turns: [DiscussPrompt.SavedTurn]) async throws -> String
    var saveLabel = "存成复盘"
    /// 只交上次存过以后新问的几组（追问往同一篇里接，不重复写）
    var incremental = false

    struct Turn: Identifiable { let id = UUID(); let q: String; let a: String; let sources: [DiscussPrompt.Source] }

    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var turns: [Turn] = []
    @State private var input = ""
    @State private var busy = false
    @State private var asking: String?
    @State private var status = ""
    @State private var failed = false
    @State private var saved = 0   // incremental：前几组已经存过
    @FocusState private var focused: Bool

    private var trimmed: String { input.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    if turns.isEmpty && asking == nil && !hint.isEmpty {
                        Text(hint).font(.system(size: 13.5)).foregroundStyle(p.ink2)
                    }
                    ForEach(turns) { t in turn(t).id(t.id) }
                    if let asking {
                        question(asking)
                    }
                    if !status.isEmpty {
                        Text(status).font(.system(size: 13.5)).foregroundStyle(failed ? Color.red : p.ink2).id("status")
                    }
                }
                .padding(.horizontal, 20).padding(.vertical, 14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: turns.count) { withAnimation { proxy.scrollTo(turns.last?.id, anchor: .top) } }
            .onChange(of: asking) { withAnimation { proxy.scrollTo("status", anchor: .bottom) } }
        }
        .scrollDismissesKeyboard(.interactively)
        .safeAreaInset(edge: .bottom) { inputBar }
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button(saveLabel) { saveAll() }.disabled(busy || turns.count <= (incremental ? saved : 0))
                Button("重新开始") { turns = []; saved = 0; input = ""; status = ""; failed = false }.disabled(busy || (turns.isEmpty && input.isEmpty))
            }
        }
    }

    private func question(_ q: String) -> some View {
        Text(q).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(2).textSelection(.enabled)
    }

    private func turn(_ t: Turn) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            question(t.q)
            Text(t.a).font(.system(size: 15)).foregroundStyle(p.ink).lineSpacing(4).textSelection(.enabled)
            if !t.sources.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("出处").font(.system(size: 12, weight: .medium)).foregroundStyle(p.ink2)
                    ForEach(t.sources, id: \.n) { s in SourceLink(source: s) }
                }
                .padding(.top, 2)
            }
        }
        .padding(.bottom, 4)
        .overlay(alignment: .bottom) { Rectangle().fill(p.rule).frame(height: 0.5).offset(y: 12) }
    }

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("输入问题", text: $input, axis: .vertical)
                .lineLimit(1...4).focused($focused).disabled(busy)
                .padding(.horizontal, 12).padding(.vertical, 9)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
            Button("发送") { send() }
                .buttonStyle(.borderedProminent).tint(p.accent)
                .disabled(busy || trimmed.isEmpty)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(p.paper)
    }

    private func send() {
        let q = trimmed
        guard !q.isEmpty, !busy else { return }
        guard q.count <= DiscussPrompt.maxQuestionChars else {
            status = "问题不要超过 \(DiscussPrompt.maxQuestionChars) 字"; failed = true; return
        }
        busy = true; failed = false; asking = q; status = "正在回答…"
        let history = turns.map { DiscussPrompt.Turn(q: $0.q, a: $0.a) }
        Task {
            do {
                let a = try await ask(q, history) { text in Task { @MainActor in status = "正在回答：\(text)" } }
                turns.append(Turn(q: q, a: a.text, sources: a.sources))
                input = ""; status = ""
            } catch {
                status = error.localizedDescription; failed = true   // 问题留在输入框里，可以改了再发
            }
            asking = nil; busy = false
        }
    }

    private func saveAll() {
        busy = true; failed = false; status = "正在存…"
        let from = incremental ? saved : 0, upTo = turns.count
        let picked = turns[from...].map { DiscussPrompt.SavedTurn(q: $0.q, a: $0.a, sources: $0.sources) }
        Task {
            do {
                status = try await save(picked)
                if incremental { saved = upTo }
            } catch { status = "没存上：\(error.localizedDescription)"; failed = true }
            busy = false
        }
    }
}

/// 回答下面的一条出处：点开打开原文（手机上有这篇才能点）
private struct SourceLink: View {
    let source: DiscussPrompt.Source
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p

    private enum Target { case doc(Doc), episode(Episode) }

    private var target: Target? {
        let lib = vault.library
        let path = source.path
        if let e = lib.allEpisodes.first(where: { [$0.notePath, $0.speechPath, $0.transcriptPath, $0.articlePath].contains(path) }) { return .episode(e) }
        if let d = (lib.clips + lib.papers + lib.notes + lib.reports + lib.shows.flatMap(\.docs)).first(where: { $0.path == path }) { return .doc(d) }
        return nil
    }

    private var label: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("[\(source.n)]").font(.system(size: 13)).foregroundStyle(p.ink2)
            VStack(alignment: .leading, spacing: 1) {
                Text(Markdown.latinQuotes(source.title)).font(.system(size: 14)).foregroundStyle(target == nil ? p.ink2 : p.accent).lineLimit(2)
                Text([source.source, source.kind, source.date].filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 12)).foregroundStyle(p.ink2).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 3)
    }

    @ViewBuilder var body: some View {
        switch target {
        case .doc(let d): PlainLink(value: d) { label }
        case .episode(let e): PlainLink(value: e) { label }
        case nil: label
        }
    }
}
