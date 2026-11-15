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
                    Text(Markdown.latinQuotes(episode.title)).font(prefs.titleFont).lineSpacing(5).foregroundStyle(p.ink)
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
                .padding(.horizontal, prefs.margin.points).padding(.top, 8).padding(.bottom, 40)
            }
            .readableColumn()
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
            TocSheet(headings: Markdown.headings(blocks), notePath: episode.notePath, inlineMarks: nil) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .task(id: tab) { await load(tab) }
        .task(id: tab) {
            // 在前台停留够久才算读过：只点开看一眼不进阅读记录，复盘才准
            if await ReadLog.waitDwell() { ReadLog.episode(episode, kind: tab == .note ? "note" : tab.rawValue) }
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
    @State private var noting: NoteDraft?
    @State private var starred = false
    @State private var reflection = ""         // 剪藏的读后感：不当正文画，放在文末那张卡里
    @State private var marks = (count: 0, notes: 0)
    @State private var writingReflection = false
    @State private var showNote = false
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
        guard isClip else { return nil }
        return MDEdit(retryImage: { src in
            let why = await vault.retryClipImage(path: doc.path, src: src)
            return why
        }, removeImage: { src in
            guard let text = vault.localText(doc.path) else { return }
            save(DocEdit.removingImage(text, src))
        }, mark: { block, sel in
            guard let text = vault.localText(doc.path) else { return }
            if let new = DocEdit.highlighted(text, block: block, selected: sel) { save(new) }
            else { note = "这段在文件里没找到，划不上" }
        }, unmark: { block, marked in
            guard let text = vault.localText(doc.path), let new = DocEdit.unhighlighted(text, block: block, marked: marked) else { return }
            save(new)
        }, annotate: { block, sel in
            noting = NoteDraft(quote: sel, block: block)
        })
    }

    /// 点了 ✎：弹出这条批注
    private func openNote(_ url: URL) -> OpenURLAction.Result {
        guard url.scheme == Markdown.noteScheme else { return .systemAction }
        let id = Int(url.absoluteString.dropFirst(Markdown.noteScheme.count + 1)) ?? -1
        if let text = vault.localText(doc.path), let n = DocEdit.notes(text).first(where: { $0.id == id }) {
            noting = NoteDraft(quote: String(Markdown.inline(n.quote).characters), text: n.text, noteID: n.id)
        }
        return .handled
    }

    private func saveNote(_ d: NoteDraft, _ body: String) {
        guard let text = vault.localText(doc.path) else { return }
        let new: String?
        if let id = d.noteID { new = DocEdit.renoted(text, id: id, note: body) }
        else { new = DocEdit.annotated(text, block: d.block, selected: d.quote, note: body) }
        if let new { save(new) } else if d.noteID == nil { note = "批注没加上：选中的字跨了已有的批注（点 ✎ 修改），或者这段在文件里没找到" }
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
        let want = String(String(Markdown.inline(mark).characters).prefix(24))
        guard !want.isEmpty, let i = shown.firstIndex(where: { b in
            let raw: String
            switch b {
            case .paragraph(let t), .quote(let t), .caption(let t): raw = t
            case .bullet(let t, _, _): raw = t
            case .heading(_, let t): raw = t
            default: return false
            }
            return String(Markdown.inline(raw).characters).contains(want)
        }) else { return }
        Task { try? await Task.sleep(for: .milliseconds(350)); jump = i }
    }

    /// 文末：读完顺手写读后感；划过线的，从这儿去看这篇的笔记
    private var noteFooter: some View {
        VStack(alignment: .leading, spacing: 12) {
            Rectangle().fill(p.rule).frame(height: 0.5).padding(.bottom, 6)
            if reflection.isEmpty {
                Button { writingReflection = true } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "square.and.pencil").font(.system(size: 16, weight: .medium)).foregroundStyle(p.accent)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("写读后感").font(.system(size: 15, weight: .semibold)).foregroundStyle(p.ink)
                            Text("这篇读完，最想记住的是什么").font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        Spacer()
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 16).strokeBorder(p.accent.opacity(0.45), style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("读后感").font(.system(size: 13, weight: .medium)).foregroundStyle(p.ink2)
                        Spacer()
                        Button("改") { writingReflection = true }.font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
                    }
                    Text(reflection).font(prefs.body(0.95)).lineSpacing(prefs.size * 0.55).foregroundStyle(p.ink).textSelection(.enabled)
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(p.rule.opacity(0.6)))
            }
            if marks.count + marks.notes > 0 {
                Button { showNote = true } label: {
                    HStack {
                        Text("这篇的笔记").foregroundStyle(p.ink)
                        Spacer()
                        Text("\(marks.count) 处划线，\(marks.notes) 条批注").foregroundStyle(p.ink2)
                        Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.ink2.opacity(0.6))
                    }
                    .font(.system(size: 14)).frame(minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.top, 28).padding(.bottom, 16)
    }

    private func reload(_ text: String) {
        guard isClip else { blocks = Markdown.stripPreamble(Markdown.parse(text)); return }
        starred = Clips.frontmatter(text)["starred"] == "true"
        reflection = DocEdit.reflection(text)
        marks = (DocEdit.marks(text).count, DocEdit.notes(text).count)
        // 读后感在文末单画一张卡（能改），正文里那一节不重复画
        var body = text
        if let r = DocEdit.reflectionRange(body) { body.removeSubrange(r) }
        blocks = Markdown.stripPreamble(Markdown.parse(ClipTidy.clean(body)))
    }

    private func save(_ text: String) {
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
                            .environment(\.openURL, OpenURLAction(handler: openNote))
                        if isClip { noteFooter }
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
                if isClip {
                    Button { toggleStar() } label: { Label(starred ? "取消星标" : "加星标", systemImage: starred ? "star.fill" : "star") }
                        .tint(starred ? .orange : nil)
                }
                if editable { moreMenu } else if Vault.canDelete(doc.path) {
                    Button { deleting = doc } label: { Label("删除", systemImage: "trash") }
                }
            }
        }
        .deleteReviewDialog($deleting) { _ in dismiss() }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet(showSpeechLang: bilingual).presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) {
            TocSheet(headings: Markdown.headings(shown), notePath: nil,
                     inlineMarks: isClip ? DocEdit.marks(vault.localText(doc.path) ?? "") : nil,
                     notes: isClip ? DocEdit.notes(vault.localText(doc.path) ?? "") : [],
                     onMark: { scroll(to: $0) }, onOpenNote: isClip ? { showNote = true } : nil) { jump = $0 }
                .presentationDetents([.medium, .large])
        }
        .sheet(item: $noting) { d in
            NoteSheet(draft: d, onSave: { saveNote(d, $0) }, onDelete: d.noteID.map { id in {
                if let text = vault.localText(doc.path) { save(DocEdit.unnoted(text, id: id)) }
            } })
            .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $writingReflection) {
            ReflectionSheet(title: title ?? doc.title, initial: reflection, source: vault.readingNote(doc.path)?.draftSource ?? "") { body in
                if let text = vault.localText(doc.path) { save(DocEdit.withReflection(text, body)) }
            }
        }
        .navigationDestination(isPresented: $showNote) { ReadingNoteView(path: doc.path) }
        .sheet(isPresented: $asking) {
            FollowUpSheet(path: doc.path) { if let t = vault.localText(doc.path) { reload(t) } }
        }
        .alert("划重点、批注", isPresented: $markTip) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text("在正文里长按，拖动选中文字，点弹出菜单里的「划重点」或「批注」。选中已划的字，可以取消划线。批注过的地方后面有个 ✎，点它看批注、修改或删除。")
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
            Button("改名称", systemImage: "pencil") { newTitle = title ?? doc.title; renaming = true }
            if isClip {
                Button("怎么划重点、批注", systemImage: "highlighter") { markTip = true }
                Button(summarizing ? "正在写摘要…" : "重写摘要", systemImage: "text.badge.star") { Task { await resummarize() } }
                    .disabled(summarizing || jobs.config == nil)
                if canTranslate {
                    Button(translating ? "正在翻译…" : "翻译成中文", systemImage: "character.book.closed") { Task { await translate() } }
                        .disabled(translating || jobs.config == nil)
                }
                if let text = vault.localText(doc.path), let url = URL(string: Clips.meta(path: doc.path, text: text).url), url.scheme != nil {
                    Link(destination: url) { Label("打开原文", systemImage: "safari") }
                }
            } else {
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
                            Text("在电脑上阅读页里划的句子，同步过来在这里看，也列在「复盘」的笔记里。手机上暂时只能看，不能新划。")
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
