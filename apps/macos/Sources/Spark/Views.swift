import SwiftUI
import WebKit

struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        NavigationSplitView {
            Sidebar(model: model)
                .navigationSplitViewColumnWidth(min: 190, ideal: 220, max: 300)
        } detail: {
            if case .failed(let message) = model.state {
                FailureView(model: model, message: message)
            } else if case .tool(let tool) = model.place {
                if model.state == .running {
                    WebHost(pane: model.pane(tool)).id(tool)
                        .navigationTitle(tool.title)
                } else {
                    ProgressView("正在启动 Spark…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else {
                HSplitView {
                    ItemList(model: model)
                        .frame(minWidth: 260, idealWidth: 320, maxWidth: 460)
                    ReaderPane(model: model, pane: model.reader)
                        .frame(minWidth: 420)
                }
            }
        }
        .frame(minWidth: 960, minHeight: 600)
        .sheet(isPresented: $model.addClipVisible) {
            AddClipView(composer: model.clipComposer, model: model)
        }
        .alert(item: $model.clipNotice) { n in
            n.path.isEmpty
                ? Alert(title: Text(n.title), message: Text(n.message), dismissButton: .cancel(Text("好")))
                : Alert(title: Text(n.title), message: Text(n.message),
                        primaryButton: .default(Text("打开剪藏")) { model.openClip(n.path) },
                        secondaryButton: .cancel(Text("好")))
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.refresh()
        }
    }
}

private struct Sidebar: View {
    @ObservedObject var model: AppModel
    @AppStorage("open.series") private var seriesOpen = true
    @AppStorage("open.summit") private var summitOpen = false
    @AppStorage("open.track") private var trackOpen = true
    @AppStorage("sort.shows") private var sort = ShowSort.latest.rawValue

    var body: some View {
        List(selection: Binding(get: { model.place }, set: { if let value = $0 { model.place = value } })) {
            Section {
                Label("最新", systemImage: "tray").tag(Place.latest)
                Label("星标", systemImage: "star").tag(Place.starred)
                Label("稍后再读", systemImage: "clock").badge(model.laterCount).tag(Place.later)
                Label("报告", systemImage: "doc.richtext").tag(Place.reports)
                Label("剪藏", systemImage: "bookmark").tag(Place.clips)
            } header: {
                SectionTitle("阅读")
            }
            Section {
                ForEach(Tool.allCases) { tool in
                    Label(tool.title, systemImage: tool.symbol).tag(Place.tool(tool))
                }
            } header: {
                SectionTitle("工具")
            }
            shows("节目", kinds: ["series", "other"], open: $seriesOpen, key: "series")
            shows("会议", kinds: ["summit"], open: $summitOpen, key: "summit")
            shows("信息源", kinds: ["track"], open: $trackOpen, key: "track")
        }
        .listStyle(.sidebar)
        .onChange(of: model.shows, initial: true) { revealSelection() }
        .safeAreaInset(edge: .bottom) { footer }
    }

    /// 底部一条：排序对节目、会议、信息源一起生效，所以放在这里而不是每组一个
    private var footer: some View {
        VStack(alignment: .leading, spacing: 0) {
            Divider()
            if model.activity.jobs > 0 {
                Label("\(model.activity.jobs) 个任务在跑", systemImage: "arrow.triangle.2.circlepath")
                    .font(.footnote).foregroundStyle(.secondary)
                    .padding(.horizontal, 12).padding(.top, 8)
            }
            // 在同步什么写出来，点一下到设置里对应的那一节
            ForEach(model.activity.syncs ?? [], id: \.self) { s in
                let github = s.hasPrefix("推到 GitHub") || s == "正在暂停"
                HStack(spacing: 6) {
                    Button { model.openSettings(github ? "sync" : "cable") } label: {
                        Label(s == "正在暂停" ? "正在暂停 GitHub 同步" : "正在" + s,
                              systemImage: "arrow.triangle.2.circlepath").lineLimit(1)
                    }
                    .buttonStyle(.plain)
                    .help("打开设置里的同步状态")
                    Spacer(minLength: 0)
                    // 推 GitHub 慢时可以先停：推上去的批次留着，下次接着推剩下的
                    if github {
                        Button { model.stopSync() } label: { Image(systemName: "pause.circle") }
                            .buttonStyle(.borderless).disabled(s == "正在暂停")
                            .help("暂停 GitHub 同步；推上去的留着，下次接着推")
                            .accessibilityLabel("暂停 GitHub 同步")
                    }
                }
                .font(.footnote).foregroundStyle(.secondary)
                .padding(.horizontal, 12).padding(.top, 8)
            }
            if let phone = model.activity.phone { phoneRow(phone) }
            Menu {
                Picker("排序方式", selection: $sort) {
                    ForEach(ShowSort.allCases) { Text($0.title).tag($0.rawValue) }
                }
                .pickerStyle(.inline).labelsHidden()
            } label: {
                Text("排序：" + (ShowSort(rawValue: sort) ?? .latest).shortTitle)
            }
            .menuStyle(.borderlessButton)
            .font(.footnote)
            .fixedSize()
            .padding(.horizontal, 12).padding(.vertical, 8)
        }
        .background(.bar)
    }

    /// 手机那一行：上次什么时候看过手机、带回了什么；点右边的按钮马上再看一眼
    private func phoneRow(_ p: SparkServer.Phone) -> some View {
        let when = Date(timeIntervalSince1970: p.at).formatted(date: .omitted, time: .shortened)
        let text = p.checking ? "正在看\(p.device)…"
            : !p.ok ? "\(p.device)：没看成"
            : "\(p.device)：\(when) 看过" + (p.saved > 0 ? "，带回 \(p.saved) 篇" : p.tasks > 0 ? "，收到 \(p.tasks) 个任务" : "")
        return HStack(spacing: 6) {
            Button { model.openSettings("cable") } label: {
                Label(text, systemImage: "iphone").lineLimit(1)
            }
            .buttonStyle(.plain)
            .help(p.ok ? "数据线连着时每分钟看一眼手机上新加的剪藏、复盘和任务；点开看同步详情" : p.error)
            Spacer(minLength: 0)
            Button { model.checkPhone() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).disabled(p.checking).help("马上看手机上有没有新内容")
                .accessibilityLabel("检查手机")
        }
        .font(.footnote).foregroundStyle(p.ok ? Color.secondary : Color.red)
        .padding(.horizontal, 12).padding(.top, 8)
        .alert("没看成", isPresented: Binding(get: { model.phoneCheckError != nil }, set: { if !$0 { model.phoneCheckError = nil } })) {
            Button("好", role: .cancel) {}
        } message: { Text(model.phoneCheckError ?? "") }
    }

    /// 上次停在某个节目上时，把它所在的分组展开，不然看不出选中的是哪个
    private func revealSelection() {
        guard case .show(let rel) = model.place, let show = model.shows.first(where: { $0.rel == rel }) else { return }
        switch show.kind {
        case "summit": summitOpen = true
        case "track": trackOpen = true
        default: seriesOpen = true
        }
    }

    private func shows(_ title: String, kinds: [String], open: Binding<Bool>, key: String) -> some View {
        ShowSection(model: model, title: title, kinds: kinds, open: open, key: key)
    }
}

private struct SectionTitle: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text).font(.system(size: 12, weight: .semibold)).foregroundStyle(.primary.opacity(0.7)).padding(.top, 10)
    }
}

private struct ShowSection: View {
    @ObservedObject var model: AppModel
    let title: String
    let kinds: [String]
    @Binding var open: Bool
    let key: String
    @AppStorage private var sort: String
    @AppStorage private var order: String

    init(model: AppModel, title: String, kinds: [String], open: Binding<Bool>, key: String) {
        self.model = model
        self.title = title
        self.kinds = kinds
        _open = open
        self.key = key
        _sort = AppStorage(wrappedValue: ShowSort.latest.rawValue, "sort.shows")
        _order = AppStorage(wrappedValue: "", "order." + key)
    }

    private var mode: ShowSort { ShowSort(rawValue: sort) ?? .latest }
    private var rows: [ShowInfo] {
        mode.apply(model.shows.filter { kinds.contains($0.kind) }, order: order.components(separatedBy: "\n"))
    }

    var body: some View {
        let rows = rows
        if !rows.isEmpty {
            Section(isExpanded: $open) {
                ForEach(rows) { show in
                    Text(show.name).lineLimit(1).padding(.leading, 8).help(show.count).tag(Place.show(show.rel))
                }
                .onMove(perform: mode == .manual ? move : nil)
            } header: {
                SectionTitle(title)
            }
        }
    }

    private func move(from source: IndexSet, to destination: Int) {
        var list = rows.map(\.rel)
        list.move(fromOffsets: source, toOffset: destination)
        order = list.joined(separator: "\n")
    }
}

private struct ItemList: View {
    @ObservedObject var model: AppModel
    @FocusState private var searching: Bool
    @AppStorage("clips.sort") private var clipSort = ClipSort.clipped.rawValue
    @State private var clipTag: String?

    /// 剪藏：按标签筛、按选的方式排；搜索结果和别的位置原样
    private var clipMode: Bool { model.place == .clips && model.search.isEmpty }
    private var shown: [Item] {
        guard clipMode else { return model.items }
        let sort = ClipSort(rawValue: clipSort) ?? .clipped
        let picked = model.items.filter {
            clipTag == nil || (clipTag == Self.starTag ? $0.starred == true : ($0.tags ?? []).contains(clipTag!))
        }
        // 置顶的排最前（同手机）
        let sorted = sort.apply(picked)
        return sorted.filter { $0.pinned == true } + sorted.filter { $0.pinned != true }
    }
    /// 筛选里的「星标」：只看加了星标的剪藏（同手机书架的「星标」分类）
    static let starTag = "\u{1}starred"
    private var starCount: Int { model.items.filter { $0.starred == true }.count }
    /// 标签按篇数从多到少
    private var clipTags: [(String, Int)] {
        var count: [String: Int] = [:]
        for item in model.items { for t in Set(item.tags ?? []) { count[t, default: 0] += 1 } }
        return count.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索全部笔记", text: $model.search)
                    .textFieldStyle(.plain)
                    .focused($searching)
                    .onChange(of: model.search) { model.runSearch() }
                if !model.search.isEmpty {
                    Button { model.search = "" } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .padding(8)
            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 7))
            .padding(10)
            .background(Button("") { searching = true }.keyboardShortcut("f", modifiers: [.command, .shift]).hidden())

            if clipMode, !model.items.isEmpty { clipBar }
            if !model.homeURL.isEmpty, model.search.isEmpty {
                Button { model.openHome() } label: {
                    Label("节目总结", systemImage: "text.book.closed").frame(maxWidth: .infinity, alignment: .leading)
                }
                .buttonStyle(.plain).foregroundStyle(.tint)
                .padding(.horizontal, 16).padding(.bottom, 8)
            }
            Divider()

            if model.items.isEmpty {
                Group {
                    if model.loadingItems {
                        ProgressView()
                    } else {
                        Text(model.search.isEmpty ? "这里还没有内容" : "没有找到").foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(shown, selection: $model.picked) { item in
                    ItemRow(item: item, source: item.show == model.placeTitle ? "" : item.show,
                            published: clipMode && clipSort == ClipSort.published.rawValue).tag(item.id)
                        .contextMenu {
                            // 右键：星标（剪藏、单集、会议、信息、报告都能加）；剪藏还能置顶（同手机列表右滑、长按）
                            if item.id.hasPrefix("剪藏/") {
                                Button(item.pinned == true ? "取消置顶" : "置顶") { setFlag(item, "pinned", item.pinned != true) }
                            }
                            Button(item.starred == true ? "取消星标" : "加星标") { setFlag(item, "starred", item.starred != true) }
                            Button(item.later == true ? "移出稍后再读" : "稍后再读") { setFlag(item, "later", item.later != true) }
                            // 信息跟进的文章右键：存进剪藏（同手机右滑）
                            if let origin = item.origin {
                                Button(item.saved == true ? "已在剪藏里" : model.clipSaving.contains(origin) ? "正在存进剪藏…" : "存进剪藏") {
                                    model.saveToClips(item)
                                }
                                .disabled(item.saved == true || model.clipSaving.contains(origin))
                            }
                        }
                }
                .listStyle(.inset)
            }
        }
        .onChange(of: model.place) { clipTag = nil }
        .alert("没改成", isPresented: Binding(get: { flagError != nil }, set: { if !$0 { flagError = nil } })) {
            Button("好", role: .cancel) {}
        } message: {
            Text(flagError ?? "")
        }
    }

    @State private var flagError: String?
    private func setFlag(_ item: Item, _ key: String, _ on: Bool) {
        Task { flagError = await model.flag(item, key, on) }
    }

    /// 剪藏列表上面一行：标签筛选、排序
    private var clipBar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Picker("标签", selection: $clipTag) {
                    Text("全部 \(model.items.count)").tag(String?.none)
                    if starCount > 0 { Text("星标 \(starCount)").tag(String?.some(Self.starTag)) }
                    ForEach(clipTags, id: \.0) { t, n in Text("\(t) \(n)").tag(String?.some(t)) }
                }
                .labelsHidden().fixedSize()
                .help("按标签筛")
                Spacer()
                Menu {
                    Picker("排序", selection: $clipSort) {
                        ForEach(ClipSort.allCases) { Text($0.title).tag($0.rawValue) }
                    }
                    .pickerStyle(.inline).labelsHidden()
                } label: {
                    Label((ClipSort(rawValue: clipSort) ?? .clipped).title, systemImage: "arrow.up.arrow.down")
                }
                .menuStyle(.borderlessButton).fixedSize()
                .help("排序")
            }
            .font(.callout)
            .padding(.horizontal, 12).padding(.bottom, model.fillStatus == nil && missing.isEmpty ? 8 : 4)
            fillRow
        }
    }

    /// 缺摘要或者英文没译完的剪藏
    private var missing: [String] { model.items.filter { $0.needs == true }.map(\.id) }

    /// 补全按钮和进度
    @ViewBuilder private var fillRow: some View {
        if model.filling || model.fillStatus != nil || !missing.isEmpty {
            HStack(spacing: 6) {
                if model.filling { ProgressView().controlSize(.small) }
                if let status = model.fillStatus {
                    Text(status).foregroundStyle(.secondary).lineLimit(2).textSelection(.enabled)
                }
                Spacer()
                if !model.filling, !missing.isEmpty {
                    Button("补全 \(missing.count) 篇") { model.fill(missing) }
                        .help("给缺摘要、英文没译完的剪藏补上，会调用模型")
                }
            }
            .font(.callout)
            .padding(.horizontal, 12).padding(.bottom, 8)
        }
    }
}

private struct ItemRow: View {
    let item: Item
    let source: String
    /// 按发表日期排时，日期写发表日期
    var published = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Circle().fill(item.read ? Color.clear : Color.accentColor).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    if item.pinned == true {
                        Image(systemName: "pin.fill").font(.system(size: 10)).foregroundStyle(.orange).accessibilityLabel("置顶")
                    }
                    Text(item.title).font(.system(size: 13, weight: item.read ? .regular : .medium)).lineLimit(2)
                    if item.starred == true {
                        Image(systemName: "star.fill").font(.system(size: 10)).foregroundStyle(.yellow).accessibilityLabel("星标")
                    }
                    if item.later == true {
                        Text("稍后再读").font(.system(size: 10)).foregroundStyle(.tint)
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .overlay(RoundedRectangle(cornerRadius: 3).stroke(.tint.opacity(0.6), lineWidth: 0.5))
                    }
                    if item.stub == true {
                        Text("正文还没抓到").font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                }
                let day = published && !(item.published ?? "").isEmpty ? "发布于 " + item.published! : item.date
                let meta = [source, day].filter { !$0.isEmpty }.joined(separator: "  ")
                if !meta.isEmpty {
                    Text(meta).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if let snippet = item.snippet, !snippet.isEmpty {
                    Text(snippet).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

private struct ReaderPane: View {
    @ObservedObject var model: AppModel
    @ObservedObject var pane: WebPane
    @State private var prefsOpen = false
    @State private var tocOpen = false
    @State private var renaming = false
    @State private var newTitle = ""
    @State private var renameError: String?
    @State private var deleting: Item?
    @State private var errorTitle = "没改成"

    var body: some View {
        WebHost(pane: pane)
            .overlay(alignment: .topTrailing) {
                if model.findVisible { FindBar(model: model, pane: pane) }
            }
            .alert("改名称", isPresented: $renaming) {
                TextField("名称", text: $newTitle)
                Button("改名称") {
                    let title = newTitle.trimmingCharacters(in: .whitespacesAndNewlines)
                    Task { errorTitle = "没改成"; renameError = title.isEmpty ? "名称不能是空的" : await model.retitle(title) }
                }
                Button("取消", role: .cancel) {}
            } message: {
                Text("只改显示的名称，文件名不变。")
            }
            .confirmationDialog(deleting.map { $0.id.hasPrefix("剪藏/") ? "删掉这篇剪藏？" : "删掉这篇复盘？" } ?? "删掉？",
                                isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                                presenting: deleting) { item in
                Button("移到废纸篓", role: .destructive) {
                    Task { errorTitle = "没删成"; renameError = await model.delete(item) }
                }
                Button("取消", role: .cancel) {}
            } message: { _ in
                Text("笔记库里这篇移进废纸篓，想要回来可以从废纸篓拖出来。")
            }
            .alert(errorTitle, isPresented: Binding(get: { renameError != nil }, set: { if !$0 { renameError = nil } })) {
                Button("好") {}
            } message: {
                Text(renameError ?? "")
            }
            .navigationTitle(model.placeTitle)
            .toolbar {
                ToolbarItem(placement: .navigation) {
                    ControlGroup {
                        Button { pane.webView.goBack() } label: { Image(systemName: "chevron.left") }
                            .disabled(!pane.canGoBack).help("后退")
                        Button { pane.webView.goForward() } label: { Image(systemName: "chevron.right") }
                            .disabled(!pane.canGoForward).help("前进")
                    }
                }
                if let item = model.pickedItem, item.files.count > 1 {
                    ToolbarItem(placement: .principal) {
                        Picker("版本", selection: $model.fileIndex) {
                            ForEach(item.files.indices, id: \.self) { Text(item.files[$0].label).tag($0) }
                        }
                        .pickerStyle(.segmented).labelsHidden()
                    }
                }
                ToolbarItemGroup(placement: .primaryAction) {
                    if model.place == .clips {
                        Button { model.addClip() } label: { Label("添加剪藏", systemImage: "plus") }
                            .help("添加剪藏（⌘N）").disabled(model.state != .running)
                    }
                    // 星标：正在看的这篇（一期节目记在小结上），收进左边的「星标」
                    if let item = model.pickedItem {
                        Button {
                            Task { errorTitle = "没改成"; renameError = await model.flag(item, "starred", item.starred != true) }
                        } label: {
                            Label(item.starred == true ? "取消星标" : "加星标", systemImage: item.starred == true ? "star.fill" : "star")
                        }
                        .help(item.starred == true ? "取消星标" : "加星标，收进左边的「星标」")
                        .disabled(model.state != .running)
                        // 稍后再读：紧挨星标，收进左边的「稍后再读」；读完移出，文章留在原来的地方
                        Button {
                            Task { errorTitle = "没改成"; renameError = await model.flag(item, "later", item.later != true) }
                        } label: {
                            Label(item.later == true ? "移出稍后再读" : "稍后再读",
                                  systemImage: item.later == true ? "clock.fill" : "clock")
                        }
                        .help(item.later == true ? "移出稍后再读" : "稍后再读，收进左边的「稍后再读」")
                        .disabled(model.state != .running)
                    }
                    // 信息跟进的文章：存进剪藏
                    if let item = model.pickedItem, let origin = item.origin {
                        let saving = model.clipSaving.contains(origin)
                        Button { model.saveToClips(item) } label: {
                            Label(item.saved == true ? "已在剪藏里" : "存进剪藏",
                                  systemImage: item.saved == true ? "bookmark.fill" : saving ? "hourglass" : "bookmark")
                        }
                        .help(item.saved == true ? "这篇已经在剪藏里了" : saving ? "正在抓全文和图片" : "存进剪藏：按原文链接抓全文和图片")
                        .disabled(item.saved == true || saving || model.state != .running)
                    }
                    // 剪藏和复盘的「更多」：改名称、补摘要（剪藏）、删除，跟手机文章里的「更多」一样
                    if model.canRetitle, let item = model.pickedItem {
                        let isClip = item.id.hasPrefix("剪藏/")
                        Menu {
                            Button("改名称…") { newTitle = item.title; renaming = true }
                            if isClip {
                                Button(item.pinned == true ? "取消置顶" : "置顶") {
                                    Task { errorTitle = "没改成"; renameError = await model.flag(item, "pinned", item.pinned != true) }
                                }
                                Divider()
                                Button("补摘要和翻译") { model.fill([item.id]) }.disabled(item.needs != true || model.filling)
                                Button("重写摘要") { model.fill([item.id], mode: "resummarize") }.disabled(model.filling)
                            }
                            Divider()
                            Button(isClip ? "删除剪藏…" : "删除复盘…", role: .destructive) { deleting = item }
                        } label: {
                            Image(systemName: model.filling ? "hourglass" : "ellipsis.circle")
                        }
                        .help(model.filling ? (model.fillStatus ?? "正在补摘要") : "改名称、删除" + (isClip ? "、补摘要和翻译" : ""))
                    }
                    if pane.isBilingual {
                        Picker("语言", selection: Binding(get: { pane.speech }, set: { pane.setSpeech($0) })) {
                            Text("中文").tag("zh")
                            Text("英文").tag("en")
                            Text("对照").tag("both")
                        }
                        .pickerStyle(.segmented).labelsHidden().help("中文、英文或中英对照")
                    }
                    if pane.hasDeck {
                        Button { pane.clickAction("a.btn[aria-label=\\\"看演示\\\"]") } label: {
                            Image(systemName: "play.rectangle")
                        }.help("看演示")
                    }
                    if pane.hasObsidian {
                        Button { pane.clickAction("a.btn[href^=\\\"obsidian:\\\"]") } label: {
                            Image(systemName: "arrow.up.forward.app")
                        }.help("在 Obsidian 打开")
                    }
                    if pane.toc.count > 1 {
                        Button { tocOpen.toggle() } label: { Image(systemName: "list.bullet") }
                            .help("目录")
                            .popover(isPresented: $tocOpen, arrowEdge: .bottom) {
                                TocList(pane: pane) { tocOpen = false }
                            }
                    }
                    Button { prefsOpen.toggle() } label: { Text("Aa").font(.system(size: 14, weight: .medium, design: .serif)) }
                        .help("阅读设置")
                        .popover(isPresented: $prefsOpen, arrowEdge: .bottom) { ReadPrefs(pane: pane) }
                }
            }
    }
}

private struct TocList: View {
    @ObservedObject var pane: WebPane
    let done: () -> Void

    var body: some View {
        List(pane.toc) { entry in
            Button {
                pane.scroll(to: entry.id)
                done()
            } label: {
                Text(entry.text).lineLimit(2).padding(.leading, entry.sub ? 14 : 0)
                    .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .listStyle(.plain)
        .frame(width: 300, height: min(CGFloat(pane.toc.count) * 34 + 16, 460))
    }
}

private struct FindBar: View {
    @ObservedObject var model: AppModel
    let pane: WebPane
    @State private var text = ""
    @State private var missing = false
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 6) {
            TextField("在这一篇里查找", text: $text)
                .textFieldStyle(.plain).frame(width: 180).focused($focused)
                .foregroundStyle(missing ? Color.red : Color.primary)
                .onSubmit { find(backwards: NSEvent.modifierFlags.contains(.shift)) }
            Button { find(backwards: true) } label: { Image(systemName: "chevron.up") }.buttonStyle(.plain)
            Button { find(backwards: false) } label: { Image(systemName: "chevron.down") }.buttonStyle(.plain)
            Button { model.findVisible = false } label: { Image(systemName: "xmark") }
                .buttonStyle(.plain).keyboardShortcut(.cancelAction)
        }
        .padding(8)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .padding(12)
        .onAppear { focused = true }
    }

    private func find(backwards: Bool) {
        guard !text.isEmpty else { return }
        let config = WKFindConfiguration()
        config.backwards = backwards
        config.wraps = true
        pane.webView.find(text, configuration: config) { missing = !$0.matchFound }
    }
}

/// 改的是网页阅读页同一份偏好（localStorage），设置页和所有文章都跟着变
private struct ReadPrefs: View {
    private struct Prefs: Decodable {
        var theme: String
        var font: String
        var width: String
        var size: Int?
        var themes: [[String]]
        var fonts: [[String]]
        var widths: [[String]]
        var sizes: [Int]
        var lineGap: String
        var paraGap: String
        var lineGaps: [[String]]
        var paraGaps: [[String]]
    }

    let pane: WebPane
    @State private var prefs: Prefs?

    var body: some View {
        Form {
            if let prefs {
                picker("配色", prefs.theme, prefs.themes, "theme")
                picker("字体", prefs.font, prefs.fonts, "font")
                picker("栏宽", prefs.width, prefs.widths, "width")
                Stepper("字号 \(prefs.sizes[prefs.size ?? 2])",
                        onIncrement: { setSize(1) }, onDecrement: { setSize(-1) })
                picker("行间距", prefs.lineGap, prefs.lineGaps, "lineGap")
                picker("段间距", prefs.paraGap, prefs.paraGaps, "paraGap")
            }
        }
        .frame(width: 280).padding(14)
        .onAppear(perform: reload)
    }

    private func picker(_ title: String, _ value: String, _ options: [[String]], _ key: String) -> some View {
        Picker(title, selection: Binding(get: { value }, set: { set("\(key): '\($0)'") })) {
            ForEach(options, id: \.self) { Text($0[1]).tag($0[0]) }
        }
    }

    private func setSize(_ step: Int) {
        guard let prefs else { return }
        let at = min(max((prefs.size ?? 2) + step, 0), prefs.sizes.count - 1)
        set("size: \(at)")
    }

    private func set(_ change: String) {
        let script = "(() => { const r = window.SparkRead; const p = { ...r.load(), \(change) }; r.save(p); r.apply(p); })()"
        pane.webView.evaluateJavaScript(script) { _, _ in reload() }
    }

    private func reload() {
        let script = """
            (() => { const r = window.SparkRead; const p = r.load();
              return JSON.stringify({ theme: p.theme, font: p.font, width: p.width, size: p.size, themes: r.THEMES.map(t => t.slice(0, 2)),
                fonts: r.CORE.map(k => [k, r.FONTS.find(f => f.key === k).label]), widths: r.WIDTHS.map(w => w.slice(0, 2)), sizes: r.SIZES,
                lineGap: p.lineGap, paraGap: p.paraGap, lineGaps: r.LINE_GAPS.map(g => g.slice(0, 2)), paraGaps: r.PARA_GAPS.map(g => g.slice(0, 2)) }); })()
            """
        pane.webView.evaluateJavaScript(script) { result, _ in
            if let text = result as? String {
                prefs = try? JSONDecoder().decode(Prefs.self, from: Data(text.utf8))
            }
        }
    }
}

private struct FailureView: View {
    let model: AppModel
    let message: String

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Spark 没有启动").font(.title2)
            ScrollView {
                Text(message).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 260)
            HStack {
                Button("重新启动") { model.server.start() }.keyboardShortcut(.defaultAction)
                Button("查看日志") { NSWorkspace.shared.open(SparkServer.logURL) }
            }
        }
        .padding(40).frame(maxWidth: 640, maxHeight: .infinity)
    }
}

struct WebHost: NSViewRepresentable {
    let pane: WebPane
    func makeNSView(context: Context) -> WKWebView { pane.webView }
    func updateNSView(_ view: WKWebView, context: Context) {}
}
