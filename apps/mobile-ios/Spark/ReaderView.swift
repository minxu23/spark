import SwiftUI

/// 单集阅读：小结、整理稿、文字记录三种稿子切换；信息跟进是小结 / 原文。
/// 都是笔记库里的文件，跟剪藏一样能划线、批注、写读后感（DocNotes）。小结先显示处理记录里的那份
/// （离线可看、只能读），notes/ 里的小结文件到手了换成文件，就能做笔记；重新生成小结时
/// 电脑上 core.annotations.carry_over 把笔记接回新小结。
struct ReaderView: View {
    let episode: Episode
    /// 从读书笔记点进来：打开哪一份稿子、滚到哪处划线
    var focusPath: String? = nil
    var focus: String? = nil
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
    @State private var notes: [Tab: DocNotes] = [:]
    @State private var focused = false
    @State private var markTip = false
    @State private var starred = false
    @State private var starNote: String?

    /// 一期的星标记在哪份稿子的文件头里：有小结记在小结上，没有小结的简讯记在整理稿上（电脑上 Mac 阅读器同一条）
    private var starPath: String? {
        [episode.notePath, episode.speechPath, episode.transcriptPath, episode.articlePath]
            .compactMap { $0 }.first(where: DocEdit.annotatable)
    }

    /// 加星标、取消星标：改那份稿子的文件头，存好交给电脑
    private func toggleStar() async {
        guard let sp = starPath else { return }
        guard let text = try? await vault.text(sp) else { starNote = "这份稿子还没下载下来，加不了星标"; return }
        starred.toggle()
        let new = DocEdit.starring(text, starred)
        if let n = notes.values.first(where: { $0.path == sp }) { n.save(new, vault); return }
        do { try await vault.saveEdited(path: sp, text: new) }
        catch { starNote = "没存上：\(error.localizedDescription)"; starred.toggle() }
    }

    /// 当前这份稿子上的笔记（没下载完的是 nil）
    private var current: DocNotes? { notes[tab] }
    private var edit: MDEdit? { current?.edit(vault) }

    private var tabs: [(Tab, String)] {
        // 简讯只有整理稿，没有小结：不给空的「小结」页签
        var out: [(Tab, String)] = episode.body.isEmpty && episode.tldr.isEmpty && episode.speechPath != nil ? [] : [(.note, "小结")]
        if episode.speechPath != nil { out.append((.speech, "整理稿")) }
        if episode.transcriptPath != nil { out.append((.transcript, "文字记录")) }
        if episode.articlePath != nil { out.append((.article, "原文")) }
        return out
    }

    private func path(_ t: Tab) -> String? {
        switch t {
        case .note: episode.notePath
        case .speech: episode.speechPath
        case .transcript: episode.transcriptPath
        case .article: episode.articlePath
        }
    }

    /// 小结文件还没到手时：处理记录里的一句话总结 + 正文（只能读）
    private var noteBlocks: [MDBlock] { Markdown.parse(episode.body) }
    private var rawBlocks: [MDBlock] { tab == .note ? (loaded[.note] ?? noteBlocks) : (loaded[tab] ?? []) }
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
                    Text(Markdown.latinQuotes(episode.title)).font(prefs.titleFont).lineSpacing(5).foregroundStyle(p.ink)
                    Text(meta).font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3).padding(.top, 6).padding(.bottom, 18)

                    if tab == .note && loaded[.note] == nil && !episode.tldr.isEmpty {
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
                            .environment(\.mdBaseDir, path(tab).map { ($0 as NSString).deletingLastPathComponent })
                            .environment(\.mdEdit, edit)
                        if let current { DocNotesFooter(notes: current) }
                    }
                }
                .padding(.horizontal, prefs.margin.points).padding(.top, 8).padding(.bottom, 40)
            }
            .readableColumn()
            .onChange(of: jump) { _, i in
                guard let i else { return }
                // 能做笔记的稿子里连着的几段在同一个文本框里：先滚到那一行，再让文本框自己滚到里面的那一段
                let row = MarkdownView.rowStart(i, blocks, grouped: edit?.mark != nil)
                withAnimation { proxy.scrollTo(row, anchor: .top) }
                if row != i {
                    Task { try? await Task.sleep(for: .milliseconds(450)); NotificationCenter.default.post(name: MarkableText.scrollToBlock, object: i) }
                }
                jump = nil
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
                if current != nil {
                    Button { markTip = true } label: { Label("怎么划重点、批注", systemImage: "highlighter") }
                }
                if starPath != nil {
                    Button { Task { await toggleStar() } } label: { Label(starred ? "取消星标" : "加星标", systemImage: starred ? "star.fill" : "star") }
                        .tint(starred ? .orange : nil)
                }
                if let url = URL(string: episode.url), !episode.url.isEmpty {
                    Link(destination: url) {
                        Label(episode.kind == .track ? "原网页" : "原视频", systemImage: episode.kind == .track ? "safari" : "play")
                    }
                }
            }
        }
        .docNotes(current, title: episode.title)
        .alert(NotesTip.title, isPresented: $markTip) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(NotesTip.message)
        }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet(showSpeechLang: bilingual).presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) {
            let text = current.flatMap { vault.localText($0.path) }
            TocSheet(headings: Markdown.headings(blocks), notePath: episode.notePath,
                     inlineMarks: text.map(DocEdit.marks), notes: text.map(DocEdit.notes) ?? [],
                     onMark: { scroll(to: $0) }, onOpenNote: current.map { n in { n.showNote = true } }) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .task(id: tab) { await load(tab) }
        .task {
            // 小结用处理记录里那份显示时文件可能还没下：读不到就当没加星标，不报错
            if let sp = starPath, let t = try? await vault.text(sp) { starred = DocEdit.isStarred(t) }
        }
        .alert("星标", isPresented: Binding(get: { starNote != nil }, set: { if !$0 { starNote = nil } })) {
            Button("好", role: .cancel) {}
        } message: { Text(starNote ?? "") }
        .onAppear {
            // 从读书笔记点进来：先切到划线所在的那份稿子
            if !tabs.contains(where: { $0.0 == tab }), let first = tabs.first { tab = first.0 }
            if !focused, let focusPath, let t = tabs.map(\.0).first(where: { path($0) == focusPath }) { tab = t }
        }
        .task(id: tab) {
            // 在前台停留够久才算读过：只点开看一眼不进阅读记录，复盘才准
            if await ReadLog.waitDwell() { ReadLog.episode(episode, kind: tab == .note ? "note" : tab.rawValue) }
        }
        .onAppear { readState.mark(episode.id) }
    }

    /// 滚到某处划线所在的那一段
    private func scroll(to mark: String) {
        guard let i = DocNotes.blockIndex(of: mark, in: blocks) else { return }
        Task { try? await Task.sleep(for: .milliseconds(350)); jump = i }
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
        guard loaded[t] == nil, let path = path(t) else { return }
        // 小结先显示处理记录里的那份，文件拿不到（没网、用流量）就一直用它，不报错
        let quiet = t == .note
        if !quiet { error = nil; onCellular = false; loading = true }
        defer { if !quiet { loading = false } }
        do {
            let text = try await vault.text(path, allowCellular: allowCellular)
            func shown(_ s: String) -> [MDBlock] {
                let b = Markdown.stripPreamble(Markdown.parse(s))
                return t == .note ? Self.summaryBody(b) : b
            }
            if DocEdit.annotatable(path) {
                let n = notes[t] ?? DocNotes(path: path, episode: episode)
                n.onChange = { loaded[t] = shown(DocNotes.body($0)) }
                n.refresh(text)
                notes[t] = n
                loaded[t] = shown(DocNotes.body(text))
            } else {
                loaded[t] = shown(text)
            }
            if !focused, let focus, path == focusPath { focused = true; scroll(to: focus) }
        } catch {
            guard !quiet else { return }
            onCellular = error is Vault.OnCellular
            self.error = error.localizedDescription
        }
    }

    /// 小结文件里页面上已经有的东西不再画：文末指向整理稿、文字记录的那行链接（上面切换就是），
    /// 以前电脑上记的「我的高亮」汇总（目录里看）
    static func summaryBody(_ blocks: [MDBlock]) -> [MDBlock] {
        var out: [MDBlock] = []
        var i = 0, skipping = false
        while i < blocks.count {
            let b = blocks[i]
            if case .heading(let level, let t) = b {
                if skipping && level <= 2 { skipping = false }
                if t == "我的高亮" { skipping = true; i += 1; continue }
            }
            if skipping { i += 1; continue }
            if case .rule = b, i + 1 < blocks.count, case .paragraph(let next) = blocks[i + 1],
               next.contains("[原链接](") || next.contains("](<../") || next.contains("](../") {
                i += 2; continue
            }
            out.append(b)
            i += 1
        }
        return out
    }
}

/// 报告、简报、节目总结：一整篇 Markdown
struct DocReaderView: View {
    let doc: Doc
    /// 从读书笔记点进来：打开后滚到这处划线
    var focus: String? = nil
    @State private var focused = false
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(ReadState.self) private var readState
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var blocks: [MDBlock]?
    @State private var error: String?
    @State private var discardPhone = false   // 放弃手机上这一版：确认之后才做
    @State private var showPrefs = false
    @State private var showToc = false
    @State private var jump: Int?
    @State private var deleting: Doc?
    @State private var title: String?
    @State private var renaming = false
    @State private var newTitle = ""
    @State private var markTip = false
    @State private var asking = false
    @State private var summarizing = false
    @State private var translating = false
    @State private var note: String?
    @State private var starred = false
    /// 划线、批注、读后感（剪藏、会议资料、报告……能做笔记的都有，见 DocEdit.annotatable）
    @State private var notes: DocNotes?
    @Environment(\.dismiss) private var dismiss

    private var isClip: Bool { Clips.isClip(doc.path) }
    /// 中英对照的剪藏才有语言切换，整理稿同一套
    private var bilingual: Bool { isClip && Markdown.isBilingual(blocks ?? []) }
    private var shown: [MDBlock] { bilingual ? Markdown.filter(blocks ?? [], prefs.speechLang) : (blocks ?? []) }
    private var language: Binding<Markdown.SpeechLang> {
        Binding(get: { prefs.speechLang }, set: { prefs.speechLang = $0 })
    }
    private var editable: Bool { DocEdit.editable(doc.path) }

    private var edit: MDEdit? {
        guard let notes else { return nil }
        guard isClip else { return notes.edit(vault) }
        return notes.edit(vault, retryImage: { src in
            await vault.retryClipImage(path: doc.path, src: src)
        }, removeImage: { src in
            guard let text = vault.localText(doc.path) else { return }
            save(DocEdit.removingImage(text, src))
        })
    }

    private func toggleStar() {
        guard let text = vault.localText(doc.path) else { return }
        starred.toggle()
        save(DocEdit.starring(text, starred))
    }

    /// 读书笔记里点的那处划线在哪一段
    private func scrollToFocus() {
        guard let focus, !focused else { return }
        focused = true
        scroll(to: focus)
    }

    /// 滚到某处划线所在的那一段
    private func scroll(to mark: String) {
        guard let i = DocNotes.blockIndex(of: mark, in: shown) else { return }
        Task { try? await Task.sleep(for: .milliseconds(350)); jump = i }
    }

    private func reload(_ text: String) {
        // 读后感在文末单画一张卡（能改），正文里那一节不重复画
        notes?.refresh(text)
        let body = notes == nil ? text : DocNotes.body(text)
        starred = DocEdit.isStarred(text)
        blocks = Markdown.stripPreamble(Markdown.parse(isClip ? ClipTidy.clean(body) : body))
    }

    /// 改名、星标、翻译这些不是笔记的改动：一样存好、交给电脑
    private func save(_ text: String) {
        if let notes { notes.save(text, vault); return }
        reload(text)
        Task {
            do { try await vault.saveEdited(path: doc.path, text: text) }
            catch { note = "没存上：\(error.localizedDescription)" }
        }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text(Markdown.latinQuotes(title ?? doc.title)).font(prefs.titleFont).lineSpacing(5).foregroundStyle(p.ink)
                    Text([doc.source, Dates.label(doc.date)].filter { !$0.isEmpty }.joined(separator: "　"))
                        .font(.system(size: 13)).foregroundStyle(p.ink2).padding(.top, 6).padding(.bottom, 18)
                    if let note {
                        Text(note).font(.system(size: 13)).foregroundStyle(p.ink2).padding(.bottom, 12)
                    }
                    // 手机上这一版没进电脑（电脑上也改过、或者已经没有这篇了）：说清楚，能放弃手机这一版
                    if let notice = vault.pendingNotice(doc.path) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(notice).font(.system(size: 13)).foregroundStyle(p.ink)
                            HStack(spacing: 16) {
                                Button("拷贝手机上这一版") { UIPasteboard.general.string = vault.localText(doc.path) ?? "" }
                                Button("不要手机上这一版，看电脑上的", role: .destructive) { discardPhone = true }
                            }
                            .font(.system(size: 13))
                        }
                        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(p.paper2, in: RoundedRectangle(cornerRadius: 8)).padding(.bottom, 12)
                    }
                    if bilingual {
                        Picker("语言", selection: language) {
                            ForEach(Markdown.SpeechLang.allCases) { Text($0.label).tag($0) }
                        }
                        .pickerStyle(.segmented).padding(.bottom, 16)
                    }
                    if blocks != nil {
                        MarkdownView(blocks: shown)
                            .environment(\.mdBaseDir, (doc.path as NSString).deletingLastPathComponent)
                            .environment(\.mdEdit, edit)
                        if let notes { DocNotesFooter(notes: notes) }
                    } else if let error {
                        Text(error).foregroundStyle(p.ink2)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.vertical, 40)
                    }
                }
                .padding(.horizontal, prefs.margin.points).padding(.vertical, 8)
            }
            .readableColumn()
            .onChange(of: jump) { _, i in
                guard let i else { return }
                // 连着的几段在同一个文本框里：先滚到那一行，再让文本框自己滚到里面的那一段
                let row = MarkdownView.rowStart(i, shown, grouped: edit?.mark != nil)
                withAnimation { proxy.scrollTo(row, anchor: .top) }
                if row != i {
                    Task { try? await Task.sleep(for: .milliseconds(450)); NotificationCenter.default.post(name: MarkableText.scrollToBlock, object: i) }
                }
                jump = nil
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
                // 剪藏、报告、会议资料、简报……能做笔记的都能加星标，收进书架的「星标」
                if isClip || notes != nil {
                    Button { toggleStar() } label: { Label(starred ? "取消星标" : "加星标", systemImage: starred ? "star.fill" : "star") }
                        .tint(starred ? .orange : nil)
                }
                if editable || notes != nil { moreMenu } else if Vault.canDelete(doc.path) {
                    Button { deleting = doc } label: { Label("删除", systemImage: "trash") }
                }
            }
        }
        .docNotes(notes, title: title ?? doc.title)
        .confirmationDialog("放弃手机上这一版？", isPresented: $discardPhone, titleVisibility: .visible) {
            Button("放弃，看电脑上的", role: .destructive) {
                vault.dismissPending(doc.path)
                Task { if let t = try? await vault.text(doc.path) { reload(t) } }
            }
        } message: { Text("手机上这一版的改动会没有（电脑上没有合并它）。要留着就先点「拷贝手机上这一版」。") }
        .deleteReviewDialog($deleting) { _ in dismiss() }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet(showSpeechLang: bilingual).presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) {
            TocSheet(headings: Markdown.headings(shown), notePath: nil,
                     inlineMarks: notes == nil ? nil : DocEdit.marks(vault.localText(doc.path) ?? ""),
                     notes: notes == nil ? [] : DocEdit.notes(vault.localText(doc.path) ?? ""),
                     onMark: { scroll(to: $0) }, onOpenNote: notes.map { n in { n.showNote = true } }) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $asking) {
            FollowUpSheet(path: doc.path) { if let t = vault.localText(doc.path) { reload(t) } }
        }
        .alert(NotesTip.title, isPresented: $markTip) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(NotesTip.message)
        }
        .alert("改名称", isPresented: $renaming) {
            TextField("名称", text: $newTitle)
            Button("保存") {
                let t = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty, let text = vault.localText(doc.path) else { return }
                title = t
                save(DocEdit.retitled(text, t))
            }
            Button("取消", role: .cancel) {}
        }
        .task {
            if notes == nil, DocEdit.annotatable(doc.path) {
                let n = DocNotes(path: doc.path)
                n.onChange = { reload($0) }
                notes = n
            }
            do {
                reload(try await vault.text(doc.path))
                scrollToFocus()
            } catch { self.error = error.localizedDescription }
        }
        .task {
            // 在前台停留够久才算读过：只点开看一眼不进阅读记录，复盘才准
            if await ReadLog.waitDwell() { ReadLog.doc(doc.path) }
        }
        .onAppear { readState.mark(doc.id) }
    }

    private var moreMenu: some View {
        Menu {
            if editable {
                Button("改名称", systemImage: "pencil") { newTitle = title ?? doc.title; renaming = true }
            }
            if notes != nil {
                Button("怎么划重点、批注", systemImage: "highlighter") { markTip = true }
            }
            if isClip {
                Button(summarizing ? "正在写摘要…" : "重写摘要", systemImage: "text.badge.star") { Task { await resummarize() } }
                    .disabled(summarizing || jobs.config == nil)
                if canTranslate {
                    Button(translating ? "正在翻译…" : "翻译成中文", systemImage: "character.book.closed") { Task { await translate() } }
                        .disabled(translating || jobs.config == nil)
                }
                if let text = vault.localText(doc.path), let url = URL(string: Clips.meta(path: doc.path, text: text).url), url.scheme != nil {
                    Link(destination: url) { Label("打开原文", systemImage: "safari") }
                }
            } else if editable {
                Button("继续追问", systemImage: "bubble.left.and.text.bubble.right") { asking = true }
            }
            if Vault.canDelete(doc.path) {
                Button("删除", systemImage: "trash", role: .destructive) { deleting = doc }
            }
        } label: { Label("更多", systemImage: "ellipsis.circle") }
    }

    /// 全英文、还没译完的剪藏
    private var canTranslate: Bool {
        guard let text = vault.localText(doc.path), let body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) else { return false }
        if bilingual { return Markdown.untranslated(blocks ?? []) > 0 }
        return ClipTranslate.needed(body)
    }

    private func translate() async {
        guard let config = jobs.config, let text = vault.localText(doc.path),
              var body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) else { return }
        translating = true
        defer { translating = false }
        body = body.trimmingCharacters(in: .whitespacesAndNewlines)
        if let r = body.range(of: "\n\n[阅读原文](", options: .backwards) { body = String(body[..<r.lowerBound]) }
        // 补译：已经配好译文的段落留着，ClipTranslate 只译没译的
        do {
            let r = try await ClipTranslate.run(body, config: config) { n, total in
                Task { @MainActor in note = "正在翻译（\(n)/\(total)）" }
            }
            save(ClipTranslate.replacingBody(text, r.text))
            note = r.missed > 0 ? "有 \(r.missed) 段没译成，可以再点一次" : nil
        } catch {
            note = "没翻译成：\(error.localizedDescription)"
        }
    }

    private func resummarize() async {
        guard let config = jobs.config, let text = vault.localText(doc.path) else { return }
        summarizing = true
        defer { summarizing = false }
        note = "正在写摘要"
        let meta = Clips.meta(path: doc.path, text: text)
        let body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) ?? text
        let article = Clipper.Article(url: meta.url, title: meta.title, source: meta.source, published: meta.published, markdown: body, images: [:])
        let used = Array(Set(ClipProcessor.shared.clipMetas(vault).flatMap(\.tags))).sorted()
        do {
            let s = try await Clips.summarize(article, existingTags: used, config: config)
            save(DocEdit.withSummary(text, s))
            note = nil
        } catch {
            note = "摘要没写成：\(error.localizedDescription)"
        }
    }
}

/// 目录 + 我的高亮（电脑上阅读页划的，存在单集笔记的「我的高亮」一节，手机上只看）
struct TocSheet: View {
    let headings: [(index: Int, level: Int, text: String)]
    let notePath: String?
    let inlineMarks: [String]?     // 剪藏：文章里 ==划的重点==
    var notes: [DocEdit.Note] = [] // 剪藏：划的字上写的批注
    var onMark: ((String) -> Void)? = nil      // 剪藏：点一处划线，回到正文那一段
    var onOpenNote: (() -> Void)? = nil        // 剪藏：打开这篇的读书笔记
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
                if notePath != nil || inlineMarks != nil {
                    Picker("", selection: $tab) {
                        Text("目录").tag(0)
                        Text(marks.map { "划线 \($0.count)" + (notes.isEmpty ? "" : "・批注 \(notes.count)") } ?? "划线").tag(1)
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
                        if inlineMarks == nil {
                            Text("小结文件还没下载到手机上，下载好以后小结里也能直接划线、批注。这里是以前在电脑上划、记在小结末尾的句子。")
                                .font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        if let marks {
                            if marks.isEmpty { Text("还没有划线").foregroundStyle(p.ink2) }
                            if let onOpenNote, !marks.isEmpty {
                                Button { dismiss(); onOpenNote() } label: {
                                    Label("打开这篇的笔记", systemImage: "note.text").font(.system(size: 15)).foregroundStyle(p.accent)
                                        .frame(minHeight: 36, alignment: .leading)
                                }
                            }
                            ForEach(marks, id: \.self) { m in
                                Button { if let onMark { onMark(m); dismiss() } } label: {
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(Markdown.inline(m)).font(prefs.body(0.95)).lineSpacing(4).foregroundStyle(p.ink)
                                            .multilineTextAlignment(.leading)
                                        ForEach(notes.filter { $0.quote == m }) { n in
                                            Label { Text(n.text).multilineTextAlignment(.leading) } icon: { Image(systemName: "square.and.pencil") }
                                                .font(.system(size: 14)).foregroundStyle(p.accent)
                                        }
                                    }
                                    .padding(.vertical, 4)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .buttonStyle(.plain).disabled(onMark == nil)
                            }
                        } else {
                            ProgressView()
                        }
                    }
                }
                .readableColumn()
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("这一期").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
        .task(id: tab) {
            if let inlineMarks { marks = inlineMarks; return }
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

/// 要写或要改的一条批注
struct NoteDraft: Identifiable {
    let quote: String       // 批注的那段字（显示出来的样子）
    var block = ""          // 新批注：选中的字所在的那一段（原文）
    var text = ""
    var noteID: Int? = nil  // 已有批注的编号
    var id: String { "\(noteID ?? -1)|\(quote)" }
}

/// 写批注、看批注：上面是批注的那段字，下面写想法
struct NoteSheet: View {
    let draft: NoteDraft
    let onSave: (String) -> Void
    let onDelete: (() -> Void)?
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                // 选中的那段字照正文里划重点的样子铺浅黄底，不用引用竖条
                Text({ var q = AttributedString(draft.quote); q.backgroundColor = Color.yellow.opacity(0.32); return q }())
                    .font(prefs.body(0.9)).lineSpacing(prefs.size * 0.45).foregroundStyle(p.ink).lineLimit(6)
                TextField("写下你的想法", text: $text, axis: .vertical)
                    .font(.system(size: 17)).lineLimit(4...12)
                    .focused($focused)
                if let onDelete {
                    Button("删除批注", role: .destructive) { onDelete(); dismiss() }
                        .font(.system(size: 15))
                }
                Spacer()
            }
            .padding(20)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle(draft.noteID == nil ? "批注" : "我的批注").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { onSave(text); dismiss() }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text == draft.text)
                }
            }
        }
        .onAppear { text = draft.text; focused = draft.noteID == nil }
    }
}
