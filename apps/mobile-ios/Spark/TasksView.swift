import SwiftUI

/// 一个任务 = 同步仓库里带 spark-task 标签的 Issue（电脑端见 core/remote_tasks.py）。
/// 状态看标签和电脑写回的那条评论：评论第一行是 <!-- spark-status {...} -->。
struct TaskItem: Identifiable {
    let issue: Issue
    let status: [String: Any]
    let text: String
    var id: Int { issue.number }

    enum State { case waiting, running, failed, done }

    var state: State {
        if issue.state == "closed" { return .done }
        if issue.labelNames.contains("失败") { return .failed }
        if issue.labelNames.contains("进行中") { return .running }
        return .waiting
    }

    var kind: String {
        switch task["type"] as? String {
        case "link": "处理链接"
        case "check": "检查订阅"
        case "report": "笔记洞察报告"
        case "review": "复盘"
        case "save": "存进笔记库"
        default: "任务"
        }
    }

    var task: [String: Any] {
        guard let body = issue.body,
              let r = body.range(of: #"```spark-task\s*\n([\s\S]*?)\n```"#, options: .regularExpression) else { return [:] }
        let block = body[r].split(separator: "\n").dropFirst().dropLast().joined(separator: "\n")
        return (try? JSONSerialization.jsonObject(with: Data(block.utf8)) as? [String: Any]) ?? [:]
    }

    var subject: String {
        let t = task
        if let url = t["url"] as? String { return url }
        if let subs = t["subscriptions"] as? [String] { return subs.joined(separator: "、") }
        if t["type"] as? String == "review" { return issue.title.replacingOccurrences(of: "复盘：", with: "") }
        if let topic = t["topic"] as? String { return "主题：\(topic)" }
        return issue.title
    }

    var progress: Double? {
        guard let cur = status["current"] as? Int, let total = status["total"] as? Int, total > 0 else { return nil }
        return Double(cur) / Double(total)
    }

    static func parse(_ issue: Issue, _ comments: [IssueComment]) -> TaskItem {
        let prefix = "<!-- spark-status "
        for c in comments.reversed() where c.body.hasPrefix(prefix) {
            let first = c.body.split(separator: "\n", maxSplits: 1)
            let json = first[0].dropFirst(prefix.count).replacingOccurrences(of: " -->", with: "")
            let meta = (try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]) ?? [:]
            return TaskItem(issue: issue, status: meta, text: first.count > 1 ? String(first[1]) : "")
        }
        return TaskItem(issue: issue, status: [:], text: "")
    }
}

struct TasksView: View {
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(RunningTasks.self) private var running
    @Environment(\.palette) private var p
    @State private var items: [TaskItem] = []
    @State private var loading = false
    @State private var error: String?
    @State private var adding = false

    var body: some View {
        List {
            Text(vault.api == nil
                 ? "现在只用数据线同步：添加的任务先存在手机上，下次用数据线接上开着 Spark 的电脑时自动交过去。"
                 : "电脑上的 Spark 开着、并在设置里打开了「手机上添加的任务」时才会接手：手动检查、或按设的间隔定时检查。电脑关着时任务会排着。")
                .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
                .padding(12).background(p.chip, in: RoundedRectangle(cornerRadius: 10))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            if let error {
                Text(error).font(.system(size: 14)).foregroundStyle(.red).listRowBackground(Color.clear)
            }
            RunningTaskCards(link: false)
            if jobs.jobs.contains(where: { !$0.running }) {
                Section { LocalJobRows(hideRunning: true) } header: {
                    Text("在手机上生成").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
                }
            }
            if items.isEmpty && cable.isEmpty && jobs.jobs.isEmpty && unsynced == 0 && !loading && error == nil {
                Text("还没有任务。点右上角「添加」：文章、信息跟进和有字幕的节目在手机上就能做。").foregroundStyle(p.ink2).listRowBackground(Color.clear)
            }
            if unsynced > 0 {
                Text("手机上新做的 \(unsynced) 份内容（剪藏、小结、复盘、笔记）已经在手机上，还没同步到电脑。下次接数据线同步时电脑会收下，已有的不会重复存。")
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            cableSection
            group("等电脑接手", .waiting)
            group("没成功", .failed)
            group("完成", .done)
            Text(vault.api == nil ? "" : "每个任务是仓库里的一个 Issue，在 GitHub 网页上也能看到。")
                .font(.system(size: 12.5)).foregroundStyle(p.ink2).listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
        .readableColumn()
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("任务")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { adding = true } label: { Label("添加", systemImage: "plus") }
            }
        }
        .refreshable { await load() }
        .task { await load() }
        // 电脑在跑数据线任务时，它推过来的进度只是本地文件：隔几秒重读一下，不花流量
        .task(id: cable.contains { $0.state == "running" || $0.state == "waiting" }) {
            guard cable.contains(where: { $0.state == "running" || $0.state == "waiting" }) else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                loadCable()
            }
        }
        .sheet(isPresented: $adding, onDismiss: { Task { await load() } }) { NewTaskView() }
    }

    // MARK: 数据线任务（发件箱，电脑端见 core/cable_sync.py）

    struct CableTask: Identifiable {
        let id: String
        let title: String
        let state: String     // queued（还在手机上）/ waiting / running / done / failed
        let text: String
        let when: Date
        var retryable = false   // 手机发件箱里还留着这个任务，能再交一次
    }

    @State private var cable: [CableTask] = []
    /// 手机上已经做好、还没同步到电脑的内容有几份（不算任务，不一条条列）
    @State private var unsynced = 0
    /// 删掉的数据线任务：电脑的记录里还有，手机上不再显示
    private static let hiddenKey = "tasks.hidden"
    private var hidden: Set<String> { Set(UserDefaults.standard.stringArray(forKey: Self.hiddenKey) ?? []) }

    private func hide(_ id: String) {
        UserDefaults.standard.set(Array(hidden.union([id])).sorted(), forKey: Self.hiddenKey)
    }

    private func deleteCable(_ t: CableTask) {
        Outbox.remove(t.id)
        hide(t.id)
        loadCable()
    }

    /// 重试：发件箱里换个新 id 再放一份，电脑按新任务收
    private func retryCable(_ t: CableTask) {
        guard let item = Outbox.items().first(where: { $0.id == t.id }), (try? Outbox.add(title: item.title, task: item.task)) != nil else { return }
        deleteCable(t)
    }

    private func loadCable() {
        let results = vault.cableTaskResults()
        let hidden = hidden
        var out: [CableTask] = []
        let items = Outbox.items()
        let syncIDs = Set(items.filter(\.isSync).map(\.id))
        var waiting = 0
        for item in items where !hidden.contains(item.id) {
            if let r = results[item.id], r["state"] as? String == "done" {
                Outbox.remove(item.id)   // 电脑已经收下并跑完了，手机上不用再留；没成功的留着，能重试
                continue
            }
            if item.isSync {
                // 手机上已经能看，只是电脑上还没有：不当任务列，数一下就行（同步出了错的下面照样列出来）
                if results[item.id]?["state"] as? String != "failed" { waiting += 1 }
                continue
            }
            if results[item.id] == nil {
                out.append(CableTask(id: item.id, title: item.title, state: "queued",
                                     text: "等下次接数据线同步时交给电脑", when: item.created))
            }
        }
        unsynced = waiting
        for (id, r) in results where !hidden.contains(id) {
            // 同步内容：做完了的不列（手机上本来就有），只列出错的
            let isSync = syncIDs.contains(id) || Outbox.syncTypes.contains(r["type"] as? String ?? "")
            if isSync && r["state"] as? String != "failed" { continue }
            let when = Date(timeIntervalSince1970: r["updated_at"] as? Double ?? 0)
            out.append(CableTask(id: id, title: r["title"] as? String ?? "任务", state: r["state"] as? String ?? "",
                                 text: r["text"] as? String ?? "", when: when, retryable: items.contains { $0.id == id }))
        }
        // 没成功、没跑完的全都列出来（书架右上角的数字也是这么算的）；跑完的只留最近 20 个
        let order = ["failed": 0, "running": 1, "waiting": 2, "queued": 3]
        let open = out.filter { $0.state != "done" }.sorted { (order[$0.state] ?? 4, $1.when) < (order[$1.state] ?? 4, $0.when) }
        let done = out.filter { $0.state == "done" }.sorted { $0.when > $1.when }.prefix(20)
        cable = open + done
    }

    @ViewBuilder
    private var cableSection: some View {
        // 正在跑的在最上面的卡片里
        let rows = cable.filter { $0.state != "running" }
        if !rows.isEmpty {
            Section {
                Text("手机用数据线连着电脑时，进度每半分钟更新一次，跑完马上推回结果；拔了就等下次同步带回来。")
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                ForEach(rows) { t in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(Markdown.latinQuotes(t.title)).font(.heading(16)).foregroundStyle(p.ink).lineLimit(2)
                        Text(t.text).font(.system(size: 13.5)).lineSpacing(3)
                            .foregroundStyle(t.state == "failed" ? .red : p.ink2)
                        if t.state == "failed" || t.state == "queued" {
                            HStack(spacing: 18) {
                                if t.retryable { Button("重试") { retryCable(t) } }
                                Button("删除", role: .destructive) { deleteCable(t) }
                            }
                            .font(.system(size: 14)).buttonStyle(.borderless).padding(.top, 2)
                        }
                    }
                    .padding(.vertical, 6)
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    .swipeActions { Button("删除", role: .destructive) { deleteCable(t) } }
                }
            } header: {
                Text("走数据线的任务").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    @ViewBuilder
    private func group(_ title: String, _ state: TaskItem.State) -> some View {
        let rows = items.filter { $0.state == state }
        if !rows.isEmpty {
            Section {
                ForEach(rows) { row($0) }
            } header: {
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    private func row(_ t: TaskItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(t.kind).font(.system(size: 12.5)).foregroundStyle(p.ink2)
            Text(t.subject).font(.heading(16)).foregroundStyle(p.ink).lineLimit(2)
            Text(line(t)).font(.system(size: 13.5)).lineSpacing(3)
                .foregroundStyle(t.state == .failed ? .red : p.ink2)
            if t.state == .running, let v = t.progress {
                ProgressView(value: v).tint(p.accent)
            }
            if t.state == .failed {
                HStack(spacing: 10) {
                    Button("重试") { Task { await retry(t) } }.buttonStyle(.bordered)
                    Button("删除", role: .destructive) { Task { await close(t) } }.buttonStyle(.bordered)
                }
                .padding(.top, 4)
            }
        }
        .padding(.vertical, 6)
        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        .swipeActions {
            if t.state != .done { Button("删除", role: .destructive) { Task { await close(t) } } }
        }
    }

    private func line(_ t: TaskItem) -> String {
        if !t.text.isEmpty { return t.text }
        let when = t.issue.created_at.formatted(date: .abbreviated, time: .shortened)
        return t.state == .waiting ? "\(when) 添加，等电脑接手" : when
    }

    private func load() async {
        loadCable()
        running.refresh(vault: vault, jobs: jobs)
        guard let api = vault.api else { return }
        loading = true
        defer { loading = false }
        do {
            let issues = try await api.tasks()
            items = try await withThrowingTaskGroup(of: TaskItem.self) { group in
                for issue in issues {
                    group.addTask { TaskItem.parse(issue, try await api.comments(issue.number)) }
                }
                var out: [TaskItem] = []
                for try await t in group { out.append(t) }
                return out.sorted { $0.issue.created_at > $1.issue.created_at }
            }
            error = nil
        } catch {
            self.error = "任务列表没拿到：\(error.localizedDescription)"
        }
    }

    /// 删除 GitHub 上的任务：关掉那个 Issue，电脑就不再接手
    private func close(_ t: TaskItem) async {
        guard let api = vault.api else { return }
        do {
            try await api.close(t.issue.number)
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func retry(_ t: TaskItem) async {
        guard let api = vault.api else { return }
        do {
            try await api.removeLabel(t.issue.number, "失败")
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct NewTaskView: View {
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var kind: Int
    /// initialKind：打开时选中的类型（0 处理链接，1 检查订阅，2 出报告）
    init(initialKind: Int = 0) { _kind = State(initialValue: initialKind) }
    @State private var url = ""
    @State private var dest = "new_series"
    @State private var length = "medium"
    @State private var subs: Set<String> = []
    @State private var maxNew = 5
    @State private var topic = ""
    @State private var scope = ""
    @State private var sending = false
    @State private var error: String?
    /// 检查订阅：只看哪类（all / podcast / track）、搜索、展开了哪些分类
    @AppStorage("newTask.subKind") private var subKind = "all"
    @State private var subQuery = ""
    @State private var toggledGroups: Set<String> = []

    private var folders: [Show] {
        (vault.library.shows(.series) + vault.library.shows(.summit))
    }
    private var subscriptions: [Show] {
        vault.library.shows(.track).filter { !$0.episodes.isEmpty } + vault.library.shows(.series)
    }
    /// 电脑同步过来的订阅列表（还没同步过就是空的，退回到书架上的文件夹，全交给电脑）
    private var feeds: [Subscription] { FeedRunner.subscriptions(LocalNotes(root: Vault.root)) }
    /// 手机自己能检查的：RSS 信息跟进
    private var phoneFeeds: [Subscription] { jobs.canRun ? feeds.filter(\.onPhone) : [] }
    /// 这篇链接手机自己能收：不是视频、播客，是文章
    private var linkIsArticle: Bool {
        guard let host = URL(string: url.trimmingCharacters(in: .whitespacesAndNewlines))?.host?.lowercased() else { return false }
        let media = ["youtube.com", "youtu.be", "podcasts.apple.com", "xiaoyuzhoufm.com", "spotify.com", "bilibili.com"]
        return !media.contains { host == $0 || host.hasSuffix("." + $0) }
    }
    /// YouTube 链接手机上取字幕写小结；放进「一场会议」要电脑的整套流程
    private var linkOnPhone: Bool { jobs.canRun && YouTube.isYouTube(url.trimmingCharacters(in: .whitespacesAndNewlines)) && dest != "summit" }
    private var runsHere: Bool {
        switch kind {
        case 0: jobs.canRun && linkIsArticle || linkOnPhone
        case 1: subs.contains { s in phoneFeeds.contains { $0.name == s } }
        default: jobs.canRun
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Picker("任务类型", selection: $kind) {
                    Text("处理链接").tag(0)
                    Text("检查订阅").tag(1)
                    Text("出报告").tag(2)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)

                switch kind {
                case 0: linkForm
                case 1: checkForm
                default: reportForm
                }

                if let error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("添加任务").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "提交中…" : (runsHere ? "在手机上做" : (vault.api == nil ? "放进发件箱" : "交给电脑"))) {
                        Task { await submit() }
                    }
                    .disabled(sending || !valid)
                }
            }
            .onAppear {
                if url.isEmpty, let s = UIPasteboard.general.string, s.hasPrefix("http"), s.count < 2000 { url = s }
            }
        }
    }

    @ViewBuilder private var linkForm: some View {
        Section {
            TextField("https://…", text: $url)
                .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
        } header: { Text("链接") } footer: {
            Text("YouTube 视频或播放列表、播客单集、Substack 或公众号文章。")
        }
        if jobs.canRun && linkIsArticle {
            Section {} footer: {
                Text("这是文章，直接在手机上存进剪藏：全文、图片、摘要和标签都有，不用等电脑。视频和播客才交给电脑。")
            }
        } else {
            Section("放到") {
                Picker("文件夹", selection: $dest) {
                    Text("新建一个节目文件夹").tag("new_series")
                    Text("作为一场会议处理").tag("summit")
                    ForEach(folders) { Text($0.name).tag("show:" + $0.name) }
                }
                .pickerStyle(.navigationLink)
            }
            Section {
                Picker("小结篇幅", selection: $length) {
                    Text("简洁").tag("short"); Text("标准").tag("medium"); Text("详细").tag("long")
                }
                .pickerStyle(.segmented)
            } header: { Text("小结篇幅") } footer: {
                Text(linkOnPhone ? "在手机上取字幕、写小结，模型用「我的」里填的。没有字幕的手机上处理不了，到时候会提示。整理稿要电脑来做。"
                     : jobs.canRun && !url.isEmpty ? "这个平台没有字幕，手机上处理不了，只能交给电脑。" : "模型和整理稿语言用电脑上设置页里的默认值。")
            }
        }
    }

    /// 订阅按分类分组（电脑上订阅管理里的分类）：节目在前，信息跟进在后；同一类里按名字排
    private struct FeedGroup: Identifiable {
        let name: String
        let kind: String
        let items: [Subscription]
        var id: String { kind + "/" + name }
    }

    private var feedGroups: [FeedGroup] {
        let q = subQuery.trimmingCharacters(in: .whitespaces).lowercased()
        let shown = feeds.filter { s in
            (subKind == "all" || s.kind == subKind)
                && (q.isEmpty || s.name.lowercased().contains(q) || (s.category ?? "").lowercased().contains(q))
        }
        let grouped = Dictionary(grouping: shown) { s in
            "\(s.kind)\u{0}" + ((s.category ?? "").isEmpty ? (s.kind == "track" ? "信息跟进" : "节目") : s.category!)
        }
        return grouped.map { key, items in
            let parts = key.split(separator: "\u{0}", maxSplits: 1).map(String.init)
            return FeedGroup(name: parts.count > 1 ? parts[1] : "", kind: parts[0],
                             items: items.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending })
        }
        .sorted { ($0.kind == "track" ? 1 : 0, $0.name) < ($1.kind == "track" ? 1 : 0, $1.name) }
    }

    /// 短的分类默认展开，长的（几十个博客）默认收起，点标题切换；搜索时全部展开
    private func isOpen(_ g: FeedGroup) -> Bool {
        !subQuery.trimmingCharacters(in: .whitespaces).isEmpty || ((g.items.count <= 8) != toggledGroups.contains(g.id))
    }

    @ViewBuilder private var checkForm: some View {
        if feeds.isEmpty {
            Section {
                ForEach(subscriptions) { s in checkRow(s.name, s.kind.label) }
            } header: { Text("检查哪些订阅有新内容") }
        } else {
            Section {
                Picker("类型", selection: $subKind) {
                    Text("全部").tag("all")
                    Text("节目 \(feeds.filter { $0.kind != "track" }.count)").tag("podcast")
                    Text("信息跟进 \(feeds.filter { $0.kind == "track" }.count)").tag("track")
                }
                .pickerStyle(.segmented)
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(p.ink2)
                    TextField("搜索名称或分类", text: $subQuery).textInputAutocapitalization(.never).autocorrectionDisabled()
                    if !subQuery.isEmpty {
                        Button { subQuery = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(p.ink2) }
                            .buttonStyle(.borderless).accessibilityLabel("清空搜索")
                    }
                }
                if !subs.isEmpty {
                    HStack {
                        Text("已选 \(subs.count) 个").foregroundStyle(p.ink)
                        Spacer()
                        Button("清空") { subs = [] }.buttonStyle(.borderless)
                    }
                }
            } header: { Text("检查哪些订阅有新内容") }
            if feedGroups.isEmpty {
                Section { Text("没有符合的订阅").foregroundStyle(p.ink2) }
            }
            ForEach(feedGroups) { g in
                Section {
                    if isOpen(g) {
                        ForEach(g.items) { s in
                            checkRow(s.name, jobs.canRun && !s.onPhone ? "交给电脑" : "")
                        }
                    }
                } header: { groupHeader(g) }
            }
        }
        Section {
            Stepper("每个订阅最多处理 \(maxNew) 条", value: $maxNew, in: 1...20)
        } footer: {
            Text(phoneFeeds.isEmpty
                 ? "找到的新内容按新到旧处理，多出来的写在结果里。" + (feeds.isEmpty ? "接一次数据线同步后，手机上就有订阅列表，填了模型 Key 就能自己检查信息跟进。" : "")
                 : "在手机上检查、写小结，写好的马上在书架里，下次同步时电脑也有一份，不会重复处理。标着「交给电脑」的手机上做不了。频道里不到 3 分钟的短视频当简讯，只写整理稿（电脑上订阅设了跳过的不处理）。每篇调一次模型，一次最多写最新的 \(FeedRunner.maxPerRun) 篇，随时能停。")
        }
    }

    private func groupHeader(_ g: FeedGroup) -> some View {
        let names = Set(g.items.map(\.name))
        let picked = names.intersection(subs).count
        let open = isOpen(g)
        return HStack(spacing: 8) {
            Button {
                if toggledGroups.contains(g.id) { toggledGroups.remove(g.id) } else { toggledGroups.insert(g.id) }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                    Text(g.name).foregroundStyle(p.ink)
                    Text(picked > 0 ? "\(picked)/\(g.items.count)" : "\(g.items.count)").foregroundStyle(picked > 0 ? p.accent : p.ink2)
                }
                .frame(minHeight: 32).contentShape(Rectangle())
            }
            .accessibilityLabel("\(g.name)，\(g.items.count) 个，已选 \(picked) 个")
            .accessibilityHint(open ? "收起" : "展开")
            Spacer()
            Button(picked == names.count ? "取消全选" : "全选") {
                if picked == names.count { subs.subtract(names) } else { subs.formUnion(names) }
            }
            .foregroundStyle(p.accent)
        }
        .font(.system(size: 14, weight: .medium))
        .textCase(nil)
        .buttonStyle(.borderless)
    }

    private func checkRow(_ name: String, _ note: String) -> some View {
        Button {
            if subs.contains(name) { subs.remove(name) } else { subs.insert(name) }
        } label: {
            HStack {
                Image(systemName: subs.contains(name) ? "checkmark.square.fill" : "square").foregroundStyle(p.accent)
                Text(name).foregroundStyle(p.ink)
                Spacer()
                if !note.isEmpty { Text(note).font(.system(size: 12.5)).foregroundStyle(p.ink2) }
            }
        }
    }

    @ViewBuilder private var reportForm: some View {
        Section("主题") {
            TextField("比如：机器人触觉与灵巧手", text: $topic)
        }
        Section {
            Picker("从哪些笔记里找", selection: $scope) {
                Text("整个笔记库，按主题自动选").tag("")
                ForEach(folders) { Text("只看「\($0.name)」").tag($0.name) }
            }
            .pickerStyle(.navigationLink)
        } footer: {
            Text(jobs.canRun
                 ? ((jobs.busy ? "手机上正在生成另一份，这份排在后面。" : "") + "在手机上生成（\(jobs.config?.label ?? "")），选材料和写法跟电脑上 Notes2Insight 的「按主题自动选」一样，要调二三十次模型、几分钟。报告出来后在书架的「报告」里，也会存进电脑上的笔记库：连着 GitHub 马上交，不然等下次接数据线同步。")
                 : "和电脑上 Notes2Insight 的「按主题自动选」一样，报告出来后在书架的「报告」里。")
        }
    }

    private var valid: Bool {
        switch kind {
        case 0: url.hasPrefix("http")
        case 1: !subs.isEmpty
        default: !topic.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    private func submit() async {
        let spec: (title: String, task: [String: Any])
        switch kind {
        case 0:
            let link = url.trimmingCharacters(in: .whitespacesAndNewlines)
            if jobs.canRun && linkIsArticle {
                do {
                    _ = try ClipInbox.add(url: link, note: "")
                    Task { await ClipProcessor.shared.run(vault: vault) }
                    dismiss()
                } catch {
                    self.error = error.localizedDescription
                }
                return
            }
            let d = SparkTask.Dest(key: dest) ?? .newSeries
            if linkOnPhone {
                var folder = ""
                if case .show(let name) = d { folder = name }
                jobs.link(link, folder: folder, length: length, vault: vault)
                dismiss()
                return
            }
            spec = SparkTask.link(link, dest: d, length: length)
        case 1:
            // 手机能检查的在手机上跑，剩下的（节目）交给电脑
            let here = phoneFeeds.filter { subs.contains($0.name) }
            if !here.isEmpty { jobs.feeds(here, maxNew: maxNew, vault: vault) }
            let rest = subs.subtracting(here.map(\.name))
            if rest.isEmpty { dismiss(); return }
            spec = SparkTask.check(Array(rest).sorted(), maxNew: maxNew)
        default:
            if jobs.canRun {
                jobs.report(topic: topic.trimmingCharacters(in: .whitespaces), scope: scope, vault: vault)
                dismiss()
                return
            }
            spec = SparkTask.report(topic.trimmingCharacters(in: .whitespaces), scope: scope)
        }
        sending = true
        defer { sending = false }
        do {
            try await vault.submitTask(spec)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
