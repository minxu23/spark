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
    @State private var later = false
    /// 占位剪藏（文件头 stub: true）：显示「补抓正文」
    @State private var stub = false
    @State private var refetching = false
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
    /// 信息文章：分成了两节的页签（划线只在那一节里找）
    @State private var splitTabs: Set<ReaderTab> = []
    /// 信息文章：小结一节开头整行加粗的一句话总结（照处理记录那一版画成卡片，不当小标题）
    @State private var sectionLeads: [ReaderTab: String] = [:]
    /// 信息文章分不成两节的文件（路径 → 当时文件内容的哈希）：内容没变就不再重读重排小结页签
    @State private var noSplit: [String: Int] = [:]

    init(input: ReaderInput) {
        self.input = input
        _tab = State(initialValue: input.pages.first?.tab ?? .doc)
    }

    // MARK: 当前页签

    private var place: ReaderPlace { input.place(tab, fileLoaded: loaded[tab] != nil) }
    private var features: ReaderFeatures { place.features }
    private var path: String? { input.path(tab) }
    /// 当前这份稿子上的笔记（没读到、不能做笔记的是 nil）。信息文章的小结页签文件里没有「## 小结」时
    /// 显示的是处理记录里的小结（不能做笔记），笔记虽然挂着（跟原文页签共用）也不给
    private var current: DocNotes? { features.notes ? notes[tab] : nil }

    private var rawBlocks: [MDBlock] {
        if let b = loaded[tab] { return b }
        return features.fromRecord ? Markdown.parse(input.recordBody) : []
    }
    /// 开关里有语言切换、内容又确实是中英对照稿，才显示语言切换
    private var bilingual: Bool { features.languageSwitch && Markdown.isBilingual(rawBlocks) }
    /// 按语言过滤后显示的块；translated：只看中文时当正文显示的译文（划线时要知道它们在文件里带「> 」）
    private var shown: (blocks: [MDBlock], translated: Set<Int>, listed: Set<Int>) {
        bilingual ? Markdown.filtered(rawBlocks, prefs.speechLang) : (rawBlocks, [], [])
    }
    private var blocks: [MDBlock] { shown.blocks }
    private var language: Binding<Markdown.SpeechLang> {
        Binding(get: { prefs.speechLang }, set: { prefs.speechLang = $0 })
    }

    private var edit: MDEdit? {
        guard let current else { return nil }
        guard features.images, let path else { return current.edit(vault, scope: scope) }
        let t = tab
        return current.edit(vault, scope: scope, retryImage: { src in
            await vault.retryClipImage(path: path, src: src)
        }, removeImage: { src in
            guard let text = vault.localText(path) else { return }
            save(DocEdit.removingImage(text, src), tab: t)
        })
    }

    /// 划线定位只在这一页对应的那段文件里找：信息文章分成两节时只在这一节里；剪藏按显示整理过的样子认（MarkAnchor.Scope）
    private var scope: MarkAnchor.Scope {
        let sec = input.section(tab).flatMap { splitTabs.contains(tab) ? ($0 == .summary ? "小结" : "原文") : nil }
        return MarkAnchor.Scope(section: sec, tidy: features.clipTidy, skipHighlights: features.summaryFilter)
    }

    private var showStar: Bool { features.star && input.starPath != nil }
    private var showLater: Bool { features.later && input.starPath != nil }
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
                    if features.clipTidy, stub { stubBox }
                    if features.fromRecord && !input.tldr.isEmpty || sectionLeads[tab] != nil {
                        // 处理记录里的一句话是纯文字；信息文章小结一节开头那行是文件里的 Markdown（电脑上可能划过线、批注过）：照 inline 排
                        Group {
                            if features.fromRecord { Text(Markdown.latinQuotes(Markdown.curlyQuotes(input.tldr))) }
                            else { Text(Markdown.inline(sectionLeads[tab] ?? "")) }
                        }.font(prefs.body(0.95)).lineSpacing(prefs.lineSpacing * 0.8)
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
                        let s = shown
                        MarkdownView(blocks: s.blocks, translated: s.translated, listed: s.listed)
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
        .alert("星标和稍后再读", isPresented: Binding(get: { starNote != nil }, set: { if !$0 { starNote = nil } })) {
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
                    do {
                        // 同一个文件的页签（信息文章的小结、原文）都换成电脑上那一版
                        let text = try await vault.text(path, allowCellular: true)
                        for t2 in input.tabs(sharing: t) { apply(text, t2) }
                    }
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
            if input.isEpisode, let sp = input.starPath, let t = try? await vault.text(sp) { starred = DocEdit.isStarred(t); later = LaterRule.isLater(t) }
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
            MetaLines(meta: input.meta).padding(.top, sp.toMeta).padding(.bottom, sp.toBody)
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

    /// 占位剪藏：正文还没抓到。下面正文里那句说明照常显示；这里放「补抓正文」
    private var stubBox: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("正文还没抓到").font(.system(size: 14, weight: .semibold)).foregroundStyle(p.ink)
                Text("有网的时候点右边的按钮再抓一次，也可以先看原文。").font(.system(size: 12.5)).foregroundStyle(p.ink2)
            }
            Spacer(minLength: 8)
            Button { Task { await refetchStub() } } label: {
                if refetching { ProgressView() } else { Text("补抓正文").font(.system(size: 14, weight: .semibold)) }
            }
            .buttonStyle(.borderedProminent).disabled(refetching).tint(p.accent)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(p.chip, in: RoundedRectangle(cornerRadius: 10))
        .padding(.bottom, spacing.box)
        .accessibilityElement(children: .contain)
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
        showLater || features.rename || current != nil || features.resummarize || (features.translate && canTranslate) || features.followUp
    }

    private var moreMenu: some View {
        Menu {
            if showLater {
                Button(later ? "移出稍后再读" : "稍后再读", systemImage: later ? "clock.badge.xmark" : "clock") { Task { await toggleLater() } }
            }
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

    /// 滚到某处划线所在的那一段。信息文章的小结、原文两个页签是同一个文件：这一页上没有，就切到另一页去找
    private func scroll(to mark: String) {
        if let i = DocNotes.blockIndex(of: mark, in: blocks) {
            Task { try? await Task.sleep(for: .milliseconds(350)); jump = i }
            return
        }
        for t in input.tabs(sharing: tab) where t != tab {
            // 信息文章不切语言，loaded 就是显示的块
            guard let b = loaded[t], let i = DocNotes.blockIndex(of: mark, in: b) else { continue }
            tab = t
            Task { try? await Task.sleep(for: .milliseconds(500)); jump = i }
            return
        }
    }

    // MARK: 读文件、存文件

    /// 能做笔记的页签挂上笔记（已经挂了就不动）
    /// 同一个文件只建一份笔记：信息文章的小结、原文两个页签共用，任一页签上改了，两个页签都按新内容重排
    private func attachNotes(_ t: ReaderTab) {
        guard notes[t] == nil, let path = input.path(t), input.place(t, fileLoaded: true).features.notes else { return }
        if let shared = notes.values.first(where: { $0.path == path }) { notes[t] = shared; return }
        let n = DocNotes(path: path, episode: input.episode)
        let tabs = input.tabs(sharing: t)
        n.onChange = { text in
            for t2 in tabs {
                attachNotes(t2)
                apply(text, t2)
            }
        }
        notes[t] = n
    }

    /// 读一个页签的文件。again：读过也重读
    private func load(_ t: ReaderTab, allowCellular: Bool = false, again: Bool = false) async {
        guard again || loaded[t] == nil, let path = input.path(t) else { return }
        // 信息文章分不成两节、文件也没变：小结页签照旧显示处理记录里的，不再重读重排
        if !again, input.section(t) == .summary, let h = noSplit[path], vault.localText(path)?.hashValue == h { return }
        // 小结先显示处理记录里的那份，文件拿不到（没网、用流量）就一直用它，不报错
        let quiet = input.place(t, fileLoaded: loaded[t] != nil).features.fromRecord
        if !quiet { error = nil; onCellular = false; loading = true }
        defer { if !quiet { loading = false } }
        do {
            let text = try await vault.text(path, allowCellular: allowCellular)
            // 同一个文件的页签（信息文章的小结、原文）一起排好
            for t2 in input.tabs(sharing: t) {
                attachNotes(t2)
                apply(text, t2)
            }
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
        if input.path(t) == input.starPath { starred = DocEdit.isStarred(text); later = LaterRule.isLater(text) }
        if t == tab, !input.isEpisode { stub = LaterRule.isStub(text) }
        let f = input.place(t, fileLoaded: true).features
        let body = n == nil ? text : DocNotes.body(text)
        let b = Markdown.stripPreamble(Markdown.parse(f.clipTidy ? ClipTidy.clean(body) : body))
        if let section = input.section(t) {
            // 信息文章：只排这一页的那一节。缺「## 小结」或「## 原文」：小结页签不用文件（显示处理记录里的），原文页签排整个文件
            let heads = b.enumerated().compactMap { i, x -> (index: Int, text: String)? in
                if case .heading(2, let h) = x { return (i, h.trimmingCharacters(in: .whitespaces)) }
                return nil
            }
            let v = ReaderPlace.sectionView(heads, count: b.count, section: section)
            var shown = v.range.map { Array(b[$0]) }
            let lead = v.split && section == .summary ? ReaderPlace.sectionLead(text) : nil
            // 一句话总结画成卡片：正文里那一块（解析成的小标题或加粗段落）不再画
            if lead != nil, let first = shown?.first, Self.isLeadBlock(first) { shown?.removeFirst() }
            loaded[t] = shown
            sectionLeads[t] = lead
            if v.split { splitTabs.insert(t) } else { splitTabs.remove(t) }
            if let path = input.path(t) { if v.split { noSplit[path] = nil } else { noSplit[path] = text.hashValue } }
            return
        }
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

    /// 加进、移出稍后再读：改那份文件的文件头（同星标那份），存好交给电脑。已经在里面再加一次，文件不动
    private func toggleLater() async {
        guard let sp = input.starPath else { return }
        let on = !later
        if input.isEpisode {
            guard let text = try? await vault.text(sp) else { starNote = "这份稿子还没下载下来，加不了稍后再读"; return }
            let new = LaterRule.with(text, on: on, value: LaterRule.now())
            later = on
            guard new != text else { return }
            if let n = notes.values.first(where: { $0.path == sp }) { n.save(new, vault); return }
            do { try await vault.saveEdited(path: sp, text: new) }
            catch { starNote = "没存上：\(error.localizedDescription)"; later = !on }
        } else {
            guard let text = vault.localText(sp) else { return }
            let new = LaterRule.with(text, on: on, value: LaterRule.now())
            later = on
            if new != text { save(new, tab: tab) }
        }
    }

    /// 占位剪藏补抓正文：用手机上的剪藏抓取重新抓一遍，成功后换掉占位正文（保留稍后再读、星标、划线、读后感）
    private func refetchStub() async {
        guard let path, !refetching else { return }
        refetching = true
        defer { refetching = false }
        note = nil
        if let why = await ClipProcessor.shared.refetch(path: path, vault: vault) {
            note = why
        } else {
            note = "已补抓正文。"
            reloadLocal(tab)
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
        // 参考这篇所在文件夹里已经用过的标签（论文看论文的，同电脑 clip_capture）
        let used = Array(Set(ClipProcessor.shared.clipMetas(vault, folder: PathRules.clipFolderOf(path) ?? Clips.folder).flatMap(\.tags))).sorted()
        do {
            let s = try await Clips.summarize(article, existingTags: used, config: config)
            save(DocEdit.withSummary(text, s), tab: t)
            note = nil
        } catch {
            note = "摘要没写成：\(error.localizedDescription)"
        }
    }

    /// 信息文章小结一节开头那行整行加粗的一句话总结解析成的块（字少的成了小标题，字多的是加粗段落）
    static func isLeadBlock(_ b: MDBlock) -> Bool {
        switch b {
        case .heading(3, _): return true
        case .paragraph(let t): return t.trimmingCharacters(in: .whitespaces).hasPrefix("**") && !t.contains("\n")
        default: return false
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

/// 标题下面的来源行（ReaderMeta）。名字和「日期　时长」一行放得下就一行；放不下在名字后面断开：
/// 名字占第一行（名字本身太长可以折行），日期和时长整个在下一行，不从中间拆开。嘉宾另起一行。行与行之间 3pt（同以前的 lineSpacing）
struct MetaLines: View {
    let meta: ReaderMeta
    @Environment(\.palette) private var p
    private let gap: CGFloat = 3

    var body: some View {
        VStack(alignment: .leading, spacing: gap) {
            if meta.name.isEmpty || meta.facts.isEmpty {
                let one = meta.name.isEmpty ? meta.factsText : meta.name
                if !one.isEmpty { Text(one).lineSpacing(gap) }
            } else {
                ViewThatFits(in: .horizontal) {
                    Text(meta.name + "　" + meta.factsText).lineLimit(1).fixedSize(horizontal: true, vertical: false)
                    VStack(alignment: .leading, spacing: gap) {
                        Text(meta.name).lineSpacing(gap).fixedSize(horizontal: false, vertical: true)
                        Text(meta.factsText).lineLimit(1)
                    }
                }
            }
            if let g = meta.guests, !g.isEmpty { Text(g).lineSpacing(gap) }
        }
        .font(.system(size: 13)).foregroundStyle(p.ink2)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(meta.text)
    }
}

/// 窄屏导航栏里的稿子页签：跟这一屏别的悬浮按钮（返回、底栏按钮、标签栏）同一种外观——系统的玻璃胶囊
/// （iOS 26 的 glassEffect，同样的底色、细边和阴影；更早的系统用 regularMaterial 加一圈细边）。
/// 选中的那个在胶囊里垫一块灰底（同标签栏选中项的系统填充色）、加粗、正文深色；没选中的次一级的灰。
/// 字 13pt；每个页签点按区域高 44。辅助功能里是按钮，选中的标「已选中」
struct CompactTabs: View {
    let tabs: [ReaderTab]
    @Binding var selection: ReaderTab
    @Environment(\.palette) private var p
    static let height: CGFloat = 44
    private let inset: CGFloat = 4

    var body: some View {
        HStack(spacing: 0) {
            ForEach(tabs, id: \.self) { t in
                let on = t == selection
                Button { selection = t } label: {
                    // 按加粗的宽度占位：切换选中时页签不跟着变宽变窄
                    Text(t.label).font(.system(size: 13, weight: .semibold)).hidden()
                        .overlay { Text(t.label).font(.system(size: 13, weight: on ? .semibold : .regular)) }
                        .foregroundStyle(on ? p.ink : p.ink2)
                        .lineLimit(1)
                        .fixedSize()
                        .padding(.horizontal, 11)
                        .frame(maxHeight: .infinity)
                        .background { if on { Capsule().fill(Color(uiColor: .tertiarySystemFill)) } }
                        .padding(.vertical, inset)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
        .padding(.horizontal, inset)
        .frame(height: Self.height)
        .modifier(FloatingCapsule())
        .fixedSize()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("稿子")
    }
}

/// 悬浮按钮的胶囊底：iOS 26 起用系统玻璃（跟导航栏、底栏里的按钮一样），更早的用材质加细边
struct FloatingCapsule: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.glassEffect(.regular, in: Capsule())
        } else {
            content
                .background(.regularMaterial, in: Capsule())
                .overlay(Capsule().strokeBorder(Color(uiColor: .separator), lineWidth: 0.5))
                .shadow(color: .black.opacity(0.08), radius: 6, y: 2)
        }
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
        // 没给宽度（要的是理想尺寸）：按 UILabel 自己排一行算出的宽度，不报一个 10000 的假宽度
        guard let w = proposal.width else {
            let s = uiView.sizeThatFits(CGSize(width: CGFloat.greatestFiniteMagnitude, height: .greatestFiniteMagnitude))
            return CGSize(width: ceil(s.width), height: ceil(s.height))
        }
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

