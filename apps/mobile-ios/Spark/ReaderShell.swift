import SwiftUI

/// 阅读外壳：单集和文档共用一个。标题和来源行、页签、正文、笔记卡、滚动跳转、工具栏、「更多」菜单、
/// 阅读设置、目录、各种弹窗只写在这里，显示什么按当前页签的开关（ReaderPlace.features）定，不看路径。
/// 单集和文档本来就不一样、这一步没统一的几处用 input.isEpisode 分开，每处写了原因。
struct ReaderShell: View {
    let input: ReaderInput
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(ReadState.self) private var readState
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss

    @State private var tab: ReaderTab
    /// 各页签读到的正文（已经去掉读后感、整理过）；没读到是 nil
    @State private var loaded: [ReaderTab: [MDBlock]] = [:]
    /// 各页签上的笔记（能做笔记、文件读到了才有）
    @State private var notes: [ReaderTab: DocNotes] = [:]
    @State private var loading = false
    @State private var error: String?
    @State private var onCellular = false
    @State private var showPrefs = false
    @State private var showToc = false
    @State private var jump: Int?
    @State private var focused = false
    @State private var markTip = false
    @State private var starred = false
    @State private var starNote: String?
    @State private var discardPhone = false   // 放弃手机上这一版：确认之后才做
    @State private var resendPhone = false    // 重新交给电脑：确认之后才做
    @State private var resending = false
    @State private var deleting: Doc?
    @State private var title: String?
    @State private var renaming = false
    @State private var asking = false
    @State private var summarizing = false
    @State private var translating = false
    @State private var editingJotting = false
    /// 标题下面的一行状态（已保存、正在翻译……）
    @State private var note: String?
    /// 阅读页的宽度（导航栏里的页签按它选宽窄两种）
    @State private var pageWidth: CGFloat = 0

    init(input: ReaderInput) {
        self.input = input
        _tab = State(initialValue: input.pages.first?.tab ?? .doc)
    }

    // MARK: 当前页签

    private var place: ReaderPlace { input.place(tab, fileLoaded: loaded[tab] != nil) }
    private var features: ReaderFeatures { place.features }
    private var path: String? { input.path(tab) }
    /// 当前这份稿子上的笔记（没读到、不能做笔记的是 nil）
    private var current: DocNotes? { notes[tab] }

    private var rawBlocks: [MDBlock] {
        if let b = loaded[tab] { return b }
        return features.fromRecord ? Markdown.parse(input.recordBody) : []
    }
    /// 开关里有语言切换、内容又确实是中英对照稿，才显示语言切换
    private var bilingual: Bool { features.languageSwitch && Markdown.isBilingual(rawBlocks) }
    private var blocks: [MDBlock] { bilingual ? Markdown.filter(rawBlocks, prefs.speechLang) : rawBlocks }
    private var language: Binding<Markdown.SpeechLang> {
        Binding(get: { prefs.speechLang }, set: { prefs.speechLang = $0 })
    }

    private var edit: MDEdit? {
        guard let current else { return nil }
        guard features.images, let path else { return current.edit(vault) }
        let t = tab
        return current.edit(vault, retryImage: { src in
            await vault.retryClipImage(path: path, src: src)
        }, removeImage: { src in
            guard let text = vault.localText(path) else { return }
            save(DocEdit.removingImage(text, src), tab: t)
        })
    }

    private var showStar: Bool { features.star && input.starPath != nil }
    /// 只有剪藏要读文件取网址：单集直接用处理记录里的网址，body 不读 vault.local
    private var linkURL: URL? {
        let p = path
        return input.linkURL(features) { p.flatMap { vault.localText($0) } }
    }

    // MARK: 页面

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    if let note {
                        Text(note).font(.system(size: 13)).foregroundStyle(p.ink2).padding(.bottom, spacing.small)
                    }
                    if features.pendingNotice, let path, let notice = vault.pendingNotice(path) {
                        pendingBox(notice, path: path).id(path)
                    }
                    if features.fromRecord && !input.tldr.isEmpty {
                        Text(Markdown.latinQuotes(Markdown.curlyQuotes(input.tldr))).font(prefs.body(0.95)).lineSpacing(prefs.lineSpacing * 0.8)
                            .foregroundStyle(p.ink)
                            .padding(.horizontal, 14).padding(.vertical, 12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(p.chip, in: RoundedRectangle(cornerRadius: 10))
                            .padding(.bottom, spacing.box)
                    }
                    if features.transcriptNote {
                        infoStrip("本地语音识别或字幕整理，可能有误差；发言人是 AI 推测的。点时间在原视频里从这一句开始播放。")
                    }
                    if bilingual {
                        Picker("语言", selection: language) {
                            ForEach(Markdown.SpeechLang.allCases) { Text($0.label).tag($0) }
                        }
                        .pickerStyle(.segmented).padding(.bottom, spacing.box)
                    }
                    if features.untranslatedNotice && bilingual && prefs.speechLang == .zh, case let n = Markdown.untranslated(rawBlocks), n > 0 {
                        infoStrip("这一期有 \(n) 段整理稿没有译文，这些段落显示英文原文。")
                    }
                    if loaded[tab] != nil || features.fromRecord {
                        MarkdownView(blocks: blocks)
                            .environment(\.mdBaseDir, path.map { ($0 as NSString).deletingLastPathComponent })
                            .environment(\.mdEdit, edit)
                        if let current { DocNotesFooter(notes: current) }
                    } else if features.cellularConfirm {
                        downloadView
                    } else if let error {
                        Text(error).foregroundStyle(p.ink2)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.vertical, 40)
                    }
                }
                .padding(.horizontal, prefs.margin.points).padding(.top, spacing.top).padding(.bottom, 40)
            }
            .readableColumn()
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { pageWidth = $0 }
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
        .toolbar {
            // 几份稿子的页签放在导航栏正中：是操作控件，不压在篇名上；往下滚也一直在。只有一份时正中不放东西
            if input.pages.count > 1 {
                ToolbarItem(placement: .principal) { tabPicker }
            }
            ToolbarItemGroup(placement: .bottomBar) { toolbarItems }
        }
        // 导航栏底色：各种文档都用纸色（以前只有单集是）
        .toolbarBackground(p.paper, for: .navigationBar)
        .docNotes(current, title: title ?? input.title)
        .alert(NotesTip.title, isPresented: $markTip) {
            Button("知道了", role: .cancel) {}
        } message: {
            Text(NotesTip.message)
        }
        .alert("星标", isPresented: Binding(get: { starNote != nil }, set: { if !$0 { starNote = nil } })) {
            Button("好", role: .cancel) {}
        } message: { Text(starNote ?? "") }
        .confirmationDialog("放弃手机上这一版？", isPresented: $discardPhone, titleVisibility: .visible) {
            Button("放弃，看电脑上的", role: .destructive) {
                guard let path else { return }
                let t = tab
                vault.dismissPending(path)
                // 读电脑上的那一版：整理稿、文字记录是懒下载的，只在 Wi-Fi 下载时不放开流量就读回手机这一版。
                // 读不到就说清楚，别让人以为已经换过来了
                Task {
                    do { apply(try await vault.text(path, allowCellular: true), t) }
                    catch { note = "没读到电脑上的那一版，现在显示的还是手机上的：\(error.localizedDescription)" }
                }
            }
        } message: { Text("手机上这一版的改动会没有（电脑上没有合并它）。要留着就先点「拷贝手机上这一版」。") }
        .confirmationDialog("把手机上这一版交给电脑？", isPresented: $resendPhone, titleVisibility: .visible) {
            Button("交给电脑") {
                guard let path else { return }
                resending = true
                Task {
                    defer { resending = false }
                    do {
                        let route = try await vault.resendToMac(path)
                        note = "已经重新交给电脑：\(vault.routeNote(route))"
                    } catch {
                        note = "没交成，手机上这一版还在：\(error.localizedDescription)"
                    }
                }
            }
        } message: { Text("电脑上会新建这一篇。要是它是在别的设备上故意删掉的，交过去会再出现在电脑上。电脑上这个名字已经有另一篇时不会覆盖，手机这一版另存在电脑的 note-conflicts 文件夹里。") }
        .deleteReviewDialog($deleting) { _ in dismiss() }
        .toolbarBackground(p.paper2, for: .bottomBar)
        .toolbarBackground(.visible, for: .bottomBar)
        .toolbar(.hidden, for: .tabBar)
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet(showSpeechLang: bilingual).presentationDetents([.medium, .large]) }
        .sheet(isPresented: $showToc) { tocSheet.presentationDetents([.medium, .large]) }
        .sheet(isPresented: $asking) {
            if let path {
                let t = tab
                FollowUpSheet(path: path) { reloadLocal(t) }
            }
        }
        .sheet(isPresented: $editingJotting) {
            if let path {
                let t0 = tab
                JottingEditor(path: path) { _ in
                    guard let t = vault.localText(path) else { return }
                    apply(t, t0)
                    title = Jottings.meta(path: path, text: t, fallback: .now).title
                    note = "已保存"
                }
            }
        }
        .sheet(isPresented: $renaming) {
            RenameSheet(current: title ?? input.title) { [tab] t in
                guard let path = input.path(tab), let text = vault.localText(path) else { return }
                title = t
                save(DocEdit.retitled(text, t), tab: tab)
            }
        }
        .onAppear {
            // 从读书笔记点进来：先切到划线所在的那份稿子
            if !input.pages.contains(where: { $0.tab == tab }), let first = input.pages.first { tab = first.tab }
            if !focused, let fp = input.focusPath, let t = input.pages.first(where: { $0.path == fp })?.tab { tab = t }
        }
        .task(id: tab) {
            // 文档一打开就挂上笔记（读文件之前），每次回到这一页都重读一遍文件；单集的稿子读到了才挂、读过不再读（照旧）
            if !input.isEpisode { attachNotes(tab) }
            await load(tab, again: !input.isEpisode)
        }
        .task {
            // 单集的星标可能记在别的稿子上：先读那份。小结用处理记录里那份显示时文件可能还没下：读不到就当没加星标，不报错
            if input.isEpisode, let sp = input.starPath, let t = try? await vault.text(sp) { starred = DocEdit.isStarred(t) }
        }
        .task(id: tab) {
            // 在前台停留够久才算读过：只点开看一眼不进阅读记录，复盘才准
            if await ReadLog.waitDwell() { input.logRead(tab) }
        }
        .onAppear { readState.mark(input.id) }
    }

    /// 篇名区的间距（按正文一行的高度算，见 ReaderSpacing）
    private var spacing: ReaderSpacing.Title { ReaderSpacing.title(line: prefs.lineStep, titleSize: prefs.titleSize) }

    @ViewBuilder
    private var header: some View {
        let sp = spacing
        // 篇名的行距定死为字号 × 1.3：SwiftUI 的 lineSpacing 压不到字体自带的行高（约 1.45 倍）以下，交给 UIKit 排
        TitleText(text: Markdown.latinQuotes(title ?? input.title), font: prefs.titleUIFont, color: UIColor(p.ink), lineHeight: sp.line)
            .padding(.bottom, input.meta.isEmpty ? sp.toBody : 0)
        if !input.meta.isEmpty {
            Text(input.meta).font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3).padding(.top, sp.toMeta).padding(.bottom, sp.toBody)
        }
    }

    /// 导航栏正中的稿子页签：宽度按内容，不撑满。
    /// 系统分段控件在导航栏里约 234pt 宽（三个页签，iPhone 17 上量的）；返回按钮占左边 60pt，正中的东西两边各要让出 68pt。
    /// 屏幕窄于 390pt（iPhone SE、iPhone Duo 合上）换成字小一号、边距小的一排按钮（三个页签约 170pt），不挤掉返回按钮、不截断
    @ViewBuilder
    private var tabPicker: some View {
        if pageWidth > 0 && pageWidth < 390 {
            CompactTabs(tabs: input.pages.map(\.tab), selection: $tab)
        } else {
            Picker("稿子", selection: $tab) {
                ForEach(input.pages, id: \.tab) { Text($0.tab.label).tag($0.tab) }
            }
            .pickerStyle(.segmented)
            .fixedSize()
        }
    }

    private func infoStrip(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(p.chip, in: RoundedRectangle(cornerRadius: 10)).padding(.bottom, spacing.box)
    }

    /// 手机上这一版没进电脑（电脑上也改过、或者已经没有这篇了）：说清楚，能放弃手机这一版
    private func pendingBox(_ notice: String, path: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(notice).font(.system(size: 13)).foregroundStyle(p.ink)
            HStack(spacing: 16) {
                Button("拷贝手机上这一版") { UIPasteboard.general.string = vault.localText(path) ?? "" }
                // 电脑上没有这篇（删了、别的设备删的、旧版交错了）：把手机这一版当新的一篇交给电脑，用户点了才交
                if vault.canResend(path) {
                    Button(resending ? "正在交…" : "重新交给电脑") { resendPhone = true }.disabled(resending)
                }
                // 别的设备删掉的、旧版交错了的：没有待确认记录，放弃什么都不做，不给这个按钮
                if ![.stranded, .legacyGone].contains(vault.stuck(path)) {
                    Button("不要手机上这一版，看电脑上的", role: .destructive) { discardPhone = true }
                }
            }
            .font(.system(size: 13))
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(p.paper2, in: RoundedRectangle(cornerRadius: 8)).padding(.bottom, spacing.small)
        .task(id: path) { vault.notePendingShown(path) }   // 提醒真的显示过了：每个路径显示时都记一次（切页签也算）
    }

    /// 要下载的稿子：下载中、出错、用流量时问一下
    @ViewBuilder
    private var downloadView: some View {
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

    // MARK: 工具栏、更多

    /// 阅读设置、目录、（空）、[编辑]、[星标]、[原文链接]、[更多]
    @ViewBuilder
    private var toolbarItems: some View {
        Button { showPrefs = true } label: { Text("Aa").font(.system(size: 19, design: .serif)) }
            .accessibilityLabel("阅读设置")
        Button { showToc = true } label: { Label("目录", systemImage: "list.bullet") }
        Spacer()
        // 随手记：改标题和正文
        if features.editJotting {
            Button { editingJotting = true } label: { Label("编辑", systemImage: "square.and.pencil") }
        }
        if showStar {
            Button { Task { await toggleStar() } } label: { Label(starred ? "取消星标" : "加星标", systemImage: starred ? "star.fill" : "star") }
                .tint(starred ? .orange : nil)
        }
        if let url = linkURL {
            Link(destination: url) { Label(features.link.label, systemImage: features.link.systemImage) }
        }
        // 「更多」里只有「删除」一项时照旧直接放删除按钮
        if hasMoreItems { moreMenu } else if features.delete {
            Button { deleting = input.doc } label: { Label("删除", systemImage: "trash") }
        }
    }

    /// 「更多」里除了删除还有没有别的
    private var hasMoreItems: Bool {
        features.rename || current != nil || features.resummarize || (features.translate && canTranslate) || features.followUp
    }

    private var moreMenu: some View {
        Menu {
            if features.rename {
                Button("改名称", systemImage: "pencil") { renaming = true }
            }
            if current != nil {
                Button("怎么划重点、批注", systemImage: "highlighter") { markTip = true }
            }
            if features.resummarize {
                Button(summarizing ? "正在写摘要…" : "重写摘要", systemImage: "text.badge.star") { Task { await resummarize() } }
                    .disabled(summarizing || jobs.config == nil)
            }
            if features.translate && canTranslate {
                Button(translating ? "正在翻译…" : "翻译成中文", systemImage: "character.book.closed") { Task { await translate() } }
                    .disabled(translating || jobs.config == nil)
            }
            if features.followUp {
                Button("继续追问", systemImage: "bubble.left.and.text.bubble.right") { asking = true }
            }
            if features.delete {
                Button("删除", systemImage: "trash", role: .destructive) { deleting = input.doc }
            }
        } label: { Label("更多", systemImage: "ellipsis.circle") }
    }

    // MARK: 目录

    private var tocSheet: some View {
        let text = current.flatMap { vault.localText($0.path) }
        // 文件读不到时：单集退回去看小结里老的「我的高亮」，文档当没有划线（照旧）
        let marks: [String]? = current == nil ? nil : (text.map(DocEdit.marks) ?? (input.isEpisode ? nil : DocEdit.marks("")))
        return TocSheet(headings: Markdown.headings(blocks), notePath: input.legacyHighlightsPath,
                        inlineMarks: marks, notes: text.map(DocEdit.notes) ?? [],
                        onMark: { scroll(to: $0) }, onOpenNote: current.map { n in { n.showNote = true } }) { jump = $0 }
    }

    /// 滚到某处划线所在的那一段
    private func scroll(to mark: String) {
        guard let i = DocNotes.blockIndex(of: mark, in: blocks) else { return }
        Task { try? await Task.sleep(for: .milliseconds(350)); jump = i }
    }

    // MARK: 读文件、存文件

    /// 能做笔记的页签挂上笔记（已经挂了就不动）
    private func attachNotes(_ t: ReaderTab) {
        guard notes[t] == nil, let path = input.path(t), input.place(t, fileLoaded: true).features.notes else { return }
        let n = DocNotes(path: path, episode: input.episode)
        n.onChange = { apply($0, t) }
        notes[t] = n
    }

    /// 读一个页签的文件。again：读过也重读
    private func load(_ t: ReaderTab, allowCellular: Bool = false, again: Bool = false) async {
        guard again || loaded[t] == nil, let path = input.path(t) else { return }
        // 小结先显示处理记录里的那份，文件拿不到（没网、用流量）就一直用它，不报错
        let quiet = input.place(t, fileLoaded: loaded[t] != nil).features.fromRecord
        if !quiet { error = nil; onCellular = false; loading = true }
        defer { if !quiet { loading = false } }
        do {
            let text = try await vault.text(path, allowCellular: allowCellular)
            attachNotes(t)
            apply(text, t)
            if !focused, let focus = input.focus, path == input.focusPath { focused = true; scroll(to: focus) }
        } catch {
            guard !quiet else { return }
            onCellular = error is Vault.OnCellular
            self.error = error.localizedDescription
        }
    }

    /// 文件内容到手（读到、改了）：重排正文。读后感在文末单画一张卡（能改），正文里那一节不重复画
    private func apply(_ text: String, _ t: ReaderTab) {
        let n = notes[t]
        n?.refresh(text)
        if input.path(t) == input.starPath { starred = DocEdit.isStarred(text) }
        let f = input.place(t, fileLoaded: true).features
        let body = n == nil ? text : DocNotes.body(text)
        let b = Markdown.stripPreamble(Markdown.parse(f.clipTidy ? ClipTidy.clean(body) : body))
        loaded[t] = f.summaryFilter ? Self.summaryBody(b) : b
    }

    /// 从手机上的文件重排某个页签（继续追问写回来之后）
    private func reloadLocal(_ t: ReaderTab) {
        if let path = input.path(t), let text = vault.localText(path) { apply(text, t) }
    }

    /// 改名、星标、翻译、删图这些不是笔记的改动：一样存好、交给电脑。
    /// t 是动手那一刻的页签（翻译、写摘要要等很久，存的时候可能已经切走了）：写回那个页签的文件，只刷新那个页签
    private func save(_ text: String, tab t: ReaderTab) {
        if let n = notes[t] { n.save(text, vault); return }
        guard let path = input.path(t) else { return }
        apply(text, t)
        Task {
            do { try await vault.saveEdited(path: path, text: text) }
            catch { note = "没存上：\(error.localizedDescription)" }
        }
    }

    /// 加星标、取消星标：改那份文件的文件头，存好交给电脑
    private func toggleStar() async {
        guard let sp = input.starPath else { return }
        if input.isEpisode {
            // 单集：星标可能记在没打开的那份稿子上，要先下载；存不上就退回去，弹出来说（照旧）
            guard let text = try? await vault.text(sp) else { starNote = "这份稿子还没下载下来，加不了星标"; return }
            starred.toggle()
            let new = DocEdit.starring(text, starred)
            if let n = notes.values.first(where: { $0.path == sp }) { n.save(new, vault); return }
            do { try await vault.saveEdited(path: sp, text: new) }
            catch { starNote = "没存上：\(error.localizedDescription)"; starred.toggle() }
        } else {
            // 文档：就是打开的这一篇，按手机上的文件改（照旧）
            guard let text = vault.localText(sp) else { return }
            starred.toggle()
            save(DocEdit.starring(text, starred), tab: tab)
        }
    }

    // MARK: 剪藏：翻译、重写摘要

    /// 全英文、还没译完的剪藏
    private var canTranslate: Bool {
        guard let path, let text = vault.localText(path), let body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) else { return false }
        if bilingual { return Markdown.untranslated(rawBlocks) > 0 }
        return ClipTranslate.needed(body)
    }

    private func translate() async {
        let t = tab   // 开头记下页签和路径：译完写回这一份
        guard let config = jobs.config, let path = input.path(t), let text = vault.localText(path),
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
            save(ClipTranslate.replacingBody(text, r.text), tab: t)
            note = r.missed > 0 ? "有 \(r.missed) 段没译成，可以再点一次" : nil
        } catch {
            note = "没翻译成：\(error.localizedDescription)"
        }
    }

    private func resummarize() async {
        let t = tab   // 开头记下页签和路径：写完存回这一份
        guard let config = jobs.config, let path = input.path(t), let text = vault.localText(path) else { return }
        summarizing = true
        defer { summarizing = false }
        note = "正在写摘要"
        let meta = Clips.meta(path: path, text: text)
        let body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) ?? text
        let article = Clipper.Article(url: meta.url, title: meta.title, source: meta.source, published: meta.published, markdown: body, images: [:])
        let used = Array(Set(ClipProcessor.shared.clipMetas(vault).flatMap(\.tags))).sorted()
        do {
            let s = try await Clips.summarize(article, existingTags: used, config: config)
            save(DocEdit.withSummary(text, s), tab: t)
            note = nil
        } catch {
            note = "摘要没写成：\(error.localizedDescription)"
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

/// 窄屏导航栏里的稿子页签：一排按钮，字 13pt，选中的那个加粗、垫底色。辅助功能里是按钮，选中的标「已选中」
struct CompactTabs: View {
    let tabs: [ReaderTab]
    @Binding var selection: ReaderTab
    @Environment(\.palette) private var p

    var body: some View {
        HStack(spacing: 0) {
            ForEach(tabs, id: \.self) { t in
                let on = t == selection
                Button { selection = t } label: {
                    // 按加粗的宽度占位：切换选中时页签不跟着变宽变窄
                    Text(t.label).font(.system(size: 13, weight: .semibold)).hidden()
                        .overlay { Text(t.label).font(.system(size: 13, weight: on ? .semibold : .regular)) }
                        .foregroundStyle(p.ink)
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 9).padding(.vertical, 6)
                        .background(on ? p.chip : .clear, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
        .fixedSize()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("稿子")
    }
}

/// 阅读页顶上的篇名：行高定死（UIKit 段落样式 minimumLineHeight = maximumLineHeight），基线校正到行框正中，字形不裁切。
/// 辅助功能里是标题；字体、颜色跟着阅读设置和配色走
struct TitleText: UIViewRepresentable {
    let text: String
    let font: UIFont
    let color: UIColor
    let lineHeight: Double

    func makeUIView(context: Context) -> UILabel {
        let l = UILabel()
        l.numberOfLines = 0
        l.clipsToBounds = false
        l.accessibilityTraits = .header
        l.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        l.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return l
    }

    func updateUIView(_ l: UILabel, context: Context) {
        l.attributedText = Self.attributed(text, font: font, color: color, lineHeight: lineHeight)
        l.accessibilityLabel = text
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UILabel, context: Context) -> CGSize? {
        let w = proposal.width ?? 10_000
        let h = uiView.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude)).height
        return CGSize(width: w, height: ceil(h))
    }

    static func attributed(_ text: String, font: UIFont, color: UIColor, lineHeight: Double) -> NSAttributedString {
        let para = NSMutableParagraphStyle()
        para.minimumLineHeight = lineHeight
        para.maximumLineHeight = lineHeight
        para.lineBreakMode = .byWordWrapping
        // 行框比字体自带的行高矮时，UIKit 把字贴着行框底边排：往下挪一半差值，字形落在行框正中
        let offset = (lineHeight - font.lineHeight) / 2
        return NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color, .paragraphStyle: para, .baselineOffset: offset])
    }
}
