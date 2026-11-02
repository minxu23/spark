import SwiftUI

/// 单集阅读：小结（处理记录里就有，离线可看）、整理稿、文字记录三种稿子切换；信息跟进是小结 / 原文。
struct ReaderView: View {
    let episode: Episode
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p

    enum Tab: String, Identifiable { case note, speech, transcript, article; var id: String { rawValue } }
    @State private var tab: Tab = .note
    @State private var loaded: [Tab: [MDBlock]] = [:]
    @State private var loading = false
    @State private var error: String?
    @State private var onCellular = false
    @State private var showPrefs = false
    @State private var showToc = false
    @State private var jump: Int?

    private var tabs: [(Tab, String)] {
        var out: [(Tab, String)] = [(.note, "小结")]
        if episode.speechPath != nil { out.append((.speech, "整理稿")) }
        if episode.transcriptPath != nil { out.append((.transcript, "文字记录")) }
        if episode.articlePath != nil { out.append((.article, "原文")) }
        return out
    }

    private func path(_ t: Tab) -> String? {
        switch t {
        case .note: nil
        case .speech: episode.speechPath
        case .transcript: episode.transcriptPath
        case .article: episode.articlePath
        }
    }

    /// 小结：处理记录里的一句话总结 + 正文
    private var noteBlocks: [MDBlock] { Markdown.parse(episode.body) }
    private var rawBlocks: [MDBlock] { tab == .note ? noteBlocks : (loaded[tab] ?? []) }
    /// 当前稿子是不是中英对照的整理稿（是的话才显示语言切换）
    private var bilingual: Bool { tab == .speech && Markdown.isBilingual(rawBlocks) }
    private var blocks: [MDBlock] { bilingual ? Markdown.filter(rawBlocks, prefs.speechLang) : rawBlocks }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if tabs.count > 1 {
                        Picker("稿子", selection: $tab) {
                            ForEach(tabs, id: \.0) { Text($0.1).tag($0.0) }
                        }
                        .pickerStyle(.segmented)
                        .padding(.bottom, 14)
                    }
                    Text(Markdown.latinQuotes(episode.title)).font(.heading(21)).lineSpacing(5).foregroundStyle(p.ink)
                    Text(meta).font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3).padding(.top, 6).padding(.bottom, 18)

                    if tab == .note && !episode.tldr.isEmpty {
                        Text(Markdown.latinQuotes(Markdown.curlyQuotes(episode.tldr))).font(prefs.body(0.95)).lineSpacing(prefs.lineSpacing * 0.8)
                            .foregroundStyle(p.ink)
                            .padding(.horizontal, 14).padding(.vertical, 12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(p.chip, in: RoundedRectangle(cornerRadius: 10))
                            .padding(.bottom, 18)
                    }
                    if tab == .transcript {
                        Text("本地语音识别或字幕整理，可能有误差；发言人是 AI 推测的。点时间在原视频里从这一句开始播放。")
                            .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
                            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                            .background(p.chip, in: RoundedRectangle(cornerRadius: 10)).padding(.bottom, 16)
                    }

                    if bilingual && prefs.speechLang == .zh, case let n = Markdown.untranslated(rawBlocks), n > 0 {
                        Text("这一期有 \(n) 段整理稿没有译文，这些段落显示英文原文。")
                            .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
                            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                            .background(p.chip, in: RoundedRectangle(cornerRadius: 10)).padding(.bottom, 16)
                    }
                    if tab != .note && loaded[tab] == nil {
                        loadingView
                    } else {
                        MarkdownView(blocks: blocks)
                    }
                }
                .padding(.horizontal, 22).padding(.top, 8).padding(.bottom, 40)
            }
            .onChange(of: jump) { _, i in
                if let i { withAnimation { proxy.scrollTo(i, anchor: .top) }; jump = nil }
            }
        }
        .background(p.paper.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(p.paper, for: .navigationBar)
        .toolbar {
            ToolbarItemGroup(placement: .bottomBar) {
                Button { showPrefs = true } label: { Text("Aa").font(.system(size: 19, design: .serif)) }
                    .accessibilityLabel("阅读设置")
                Button { showToc = true } label: { Label("目录", systemImage: "list.bullet") }
                Spacer()
                if let url = URL(string: episode.url), !episode.url.isEmpty {
                    Link(destination: url) {
                        Label(episode.kind == .track ? "原网页" : "原视频", systemImage: episode.kind == .track ? "safari" : "play")
                    }
                }
            }
        }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet(showSpeechLang: bilingual).presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) {
            TocSheet(headings: Markdown.headings(blocks), notePath: episode.notePath) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .task(id: tab) {
            ReadLog.episode(episode, kind: tab == .note ? "note" : tab.rawValue)
            await load(tab)
        }
        .onAppear { readState.mark(episode.id) }
    }

    private var meta: String {
        var line = [episode.showName, Dates.label(episode.date), episode.durationLabel]
            .filter { !$0.isEmpty }.joined(separator: "　")
        // 嘉宾名单是模型给的，常带「（主持人）」「（科沃斯 CEO，嘉宾）」这类括注，也会写「无（本期为……）」：
        // 只留名字，没有真名就不显示
        let names = episode.guests
            .map { $0.replacingOccurrences(of: #"[（(].*$"#, with: "", options: .regularExpression).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("无") && $0.count <= 20 }
        if !names.isEmpty { line += "\n嘉宾：" + names.joined(separator: "、") }
        return line
    }

    @ViewBuilder
    private var loadingView: some View {
        VStack(spacing: 12) {
            if let error {
                Text(error).font(.system(size: 14)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
                if onCellular {
                    Button("这次用流量下载") { Task { await load(tab, allowCellular: true) } }
                        .buttonStyle(.bordered)
                    Text("小结不用下载，随时能看。").font(.system(size: 13)).foregroundStyle(p.ink2)
                } else {
                    Button("再试一次") { Task { await load(tab) } }
                }
            } else {
                ProgressView()
                Text("正在下载，下载过的以后没网也能看").font(.system(size: 13)).foregroundStyle(p.ink2)
            }
        }
        .frame(maxWidth: .infinity).padding(.vertical, 40)
    }

    private func load(_ t: Tab, allowCellular: Bool = false) async {
        guard t != .note, loaded[t] == nil, let path = path(t) else { return }
        error = nil
        onCellular = false
        loading = true
        defer { loading = false }
        do {
            let text = try await vault.text(path, allowCellular: allowCellular)
            loaded[t] = Markdown.stripPreamble(Markdown.parse(text))
        } catch {
            onCellular = error is Vault.OnCellular
            self.error = error.localizedDescription
        }
    }
}

/// 报告、简报、节目总结：一整篇 Markdown
struct DocReaderView: View {
    let doc: Doc
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var blocks: [MDBlock]?
    @State private var error: String?
    @State private var showPrefs = false
    @State private var showToc = false
    @State private var jump: Int?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text(Markdown.latinQuotes(doc.title)).font(.heading(21)).lineSpacing(5).foregroundStyle(p.ink)
                    Text([doc.source, Dates.label(doc.date)].filter { !$0.isEmpty }.joined(separator: "　"))
                        .font(.system(size: 13)).foregroundStyle(p.ink2).padding(.top, 6).padding(.bottom, 18)
                    if let blocks {
                        MarkdownView(blocks: blocks)
                    } else if let error {
                        Text(error).foregroundStyle(p.ink2)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.vertical, 40)
                    }
                }
                .padding(.horizontal, 22).padding(.vertical, 8)
            }
            .onChange(of: jump) { _, i in
                if let i { withAnimation { proxy.scrollTo(i, anchor: .top) }; jump = nil }
            }
        }
        .background(p.paper.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .bottomBar) {
                Button { showPrefs = true } label: { Text("Aa").font(.system(size: 19, design: .serif)) }
                    .accessibilityLabel("阅读设置")
                Button { showToc = true } label: { Label("目录", systemImage: "list.bullet") }
                Spacer()
            }
        }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet().presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) {
            TocSheet(headings: Markdown.headings(blocks ?? []), notePath: nil) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .task {
            do {
                blocks = Markdown.stripPreamble(Markdown.parse(try await vault.text(doc.path)))
            } catch { self.error = error.localizedDescription }
        }
        .onAppear {
            readState.mark(doc.id)
            ReadLog.doc(doc.path)
        }
    }
}

/// 目录 + 我的高亮（电脑上阅读页划的，存在单集笔记的「我的高亮」一节，手机上只看）
struct TocSheet: View {
    let headings: [(index: Int, level: Int, text: String)]
    let notePath: String?
    let onJump: (Int) -> Void
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    @State private var marks: [String]?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if notePath != nil {
                    Picker("", selection: $tab) {
                        Text("目录").tag(0)
                        Text(marks.map { "我的高亮 \($0.count)" } ?? "我的高亮").tag(1)
                    }
                    .pickerStyle(.segmented).padding(.horizontal, 20).padding(.bottom, 8)
                }
                List {
                    if tab == 0 {
                        if headings.isEmpty {
                            Text("这一篇没有小标题").foregroundStyle(p.ink2)
                        }
                        ForEach(headings, id: \.index) { h in
                            Button {
                                onJump(h.index)
                                dismiss()
                            } label: {
                                Text(h.text).font(.system(size: 15)).foregroundStyle(p.ink)
                                    .padding(.leading, Double(max(0, h.level - 2)) * 14)
                                    .frame(minHeight: 36, alignment: .leading)
                            }
                        }
                    } else {
                        Text("在电脑上阅读页里划的句子，存在笔记的「我的高亮」一节，同步过来在这里看。手机上暂时只能看，不能新划。")
                            .font(.system(size: 13)).foregroundStyle(p.ink2)
                        if let marks {
                            if marks.isEmpty { Text("还没有高亮").foregroundStyle(p.ink2) }
                            ForEach(marks, id: \.self) { m in
                                Text(Markdown.inline(m)).font(prefs.body(0.95)).lineSpacing(4).foregroundStyle(p.ink)
                                    .padding(.vertical, 4)
                            }
                        } else {
                            ProgressView()
                        }
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("这一期").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
        .task(id: tab) {
            guard tab == 1, marks == nil, let notePath else { return }
            let text = (try? await vault.text(notePath)) ?? ""
            marks = Self.highlights(text)
        }
    }

    /// 笔记里「## 我的高亮」这一节的每一条
    static func highlights(_ note: String) -> [String] {
        guard let start = note.range(of: "\n## 我的高亮") else { return [] }
        var section = note[start.upperBound...]
        if let next = section.range(of: "\n## ") { section = section[..<next.lowerBound] }
        return section.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { $0.hasPrefix("- ") || $0.hasPrefix("> ") }
            .map { String($0.dropFirst(2)) }
    }
}
