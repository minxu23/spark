import SwiftUI

struct MeView: View {
    @Environment(Vault.self) private var vault
    @Environment(Session.self) private var session
    @Environment(ReadPrefs.self) private var prefs
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var showPrefs = false
    @State private var showModel = false
    @State private var reconnect = false
    @State private var confirmWipe = false
    @State private var storage = (total: 0, lazy: 0)
    @State private var readingCount = 0
    @State private var noteCount = 0
    @State private var starCount = 0
    @State private var outboxCount = 0
    @State private var unsyncedCount = 0
    @State private var showOutbox = false
    @AppStorage(MainTab.key) private var mainTab = MainTab.settings
    @AppStorage("library.tab") private var libraryTab = ""
    @AppStorage("review.tab") private var reviewTab = 0
    @AppStorage("review.onlyNoted") private var onlyNoted = false
    @State private var pickingDrive = false
    @State private var driveOn = DriveSync.isOn
    @State private var driveNote: String?

    var body: some View {
        @Bindable var session = session
        // 整页都是卡片，跟上面的四个数一个样子（ScrollView，不用系统列表的通栏灰底）
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                // 跟另外三个页签的状态行对齐（那几页是列表，上面多一段行距）
                PageStatus(text: syncText, warning: vault.lastError != nil).padding(.top, 14)

                // 四个数都能点：读书笔记、星标去复盘、书架看是哪些；还没交给电脑的去任务页看、重试
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                    stat(readingCount, "篇笔记", hint: "打开复盘里的笔记") { reviewTab = 0; onlyNoted = false; mainTab = MainTab.review }
                    stat(starCount, "篇星标", hint: "打开书架的星标") { libraryTab = LibraryView.starred; mainTab = MainTab.library }
                    stat(noteCount, "条批注", hint: "打开有批注的笔记") { reviewTab = 0; onlyNoted = true; mainTab = MainTab.review }
                    stat(outboxCount, "待提交", hint: "看要电脑来做、还没交过去的任务", alert: outboxCount > 0) { showOutbox = true }
                }
                .navigationDestination(isPresented: $showOutbox) { TasksView() }

                // 常用的两项：模型、阅读样式
                card(nil) {
                    Button { showModel = true } label: {
                        row("sparkles", "模型", jobs.config.map { "\($0.provider.label)，Key 已填" } ?? "还没填")
                    }
                    .buttonStyle(.plain)
                    divider
                    Button { showPrefs = true } label: {
                        row("book", "阅读", "\(prefs.bodyFont.label)，\(Int(prefs.size)) 号")
                    }
                    .buttonStyle(.plain)
                }

                card("同步") {
                    Toggle(isOn: $session.autoSync) { line("打开 App 时自动同步", "只下有变化的文件") }
                        .padding(.vertical, 10)
                    divider
                    Toggle(isOn: Binding(get: { vault.wifiOnly }, set: { vault.wifiOnly = $0 })) { line("整理稿只在 Wi-Fi 下下载", "用流量时只同步小结") }
                        .padding(.vertical, 10)
                    #if DEBUG
                    divider
                    Toggle("开发用：假装在用流量", isOn: Binding(get: { NetMonitor.shared.pretendCellular },
                                                           set: { NetMonitor.shared.pretendCellular = $0 }))
                        .font(.system(size: 13)).foregroundStyle(p.ink2).padding(.vertical, 8)
                    #endif
                    divider
                    HStack {
                        Text("新文件下载")
                        Spacer()
                        Picker("新文件下载", selection: Binding(get: { vault.scope }, set: { vault.scope = $0 })) {
                            Text("小结和节目总结").tag(Vault.DownloadScope.light)
                            Text("全部（含文字记录）").tag(Vault.DownloadScope.all)
                        }
                        .labelsHidden().pickerStyle(.menu).tint(p.accent)
                    }
                    .frame(minHeight: 50)
                    divider
                    HStack {
                        line("存储", "\(bytes(storage.total))，整理稿占 \(bytes(storage.lazy))")
                        Spacer()
                        Button("清理") { vault.clearLazy(); storage = vault.storageBytes }
                            .font(.system(size: 14)).buttonStyle(.bordered).buttonBorderShape(.capsule).tint(p.accent)
                            .disabled(storage.lazy == 0)
                            .accessibilityHint("清掉整理稿和文字记录，小结留着，下次打开时再下载")
                    }
                    .padding(.vertical, 10)
                }

                card("连接", footer: driveNote) {
                    HStack {
                        Text("笔记库")
                        Spacer()
                        Text(driveOn ? "Google Drive：\(DriveSync.folderName)" : (session.connection.map(\.slug) ?? "只用数据线同步"))
                            .foregroundStyle(p.ink2).lineLimit(1)
                    }
                    .frame(minHeight: 48)
                    if session.connection != nil, !driveOn {
                        divider
                        HStack { Text("访问令牌"); Spacer(); Text("在钥匙串里").foregroundStyle(p.ink2) }
                            .frame(minHeight: 48)
                    }
                    if DriveSync.available {
                        divider
                        action(driveOn ? "换一个 Google Drive 文件夹" : "改用 Google Drive 同步") { pickingDrive = true }
                    }
                    if driveOn {
                        divider
                        action("不用 Google Drive 同步") {
                            DriveSync.turnOff(); driveOn = false
                            driveNote = session.connection == nil ? "改回只用数据线同步" : "改回 GitHub 同步"
                        }
                    }
                    divider
                    action(session.connection == nil ? "改用 GitHub 同步" : "更换令牌或仓库") { reconnect = true }
                    divider
                    action("断开连接并删除手机上的笔记", destructive: true) { confirmWipe = true }
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
            .foregroundStyle(p.ink)
        }
        .readableColumn()
        .pageStyle()
        .navigationTitle("设置")
        .onAppear { storage = vault.storageBytes; countNotes() }
        .toolbar { ToolbarItem(placement: .topBarTrailing) { SyncButton() } }
        .onChange(of: vault.syncing) { _, now in if !now { storage = vault.storageBytes; countNotes() } }
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet() }
        .sheet(isPresented: $showModel) { ModelSettingsView() }
        .sheet(isPresented: $reconnect) { ConnectView { reconnect = false } }
        .fileImporter(isPresented: $pickingDrive, allowedContentTypes: [.folder]) { result in
            switch result {
            case .success(let url):
                do {
                    try DriveSync.choose(url)
                    driveOn = true
                    if session.connection == nil { session.cableOnly = true }
                    driveNote = "已选「\(url.lastPathComponent)」，正在同步"
                    Task { await vault.sync(); storage = vault.storageBytes; countNotes()
                        driveNote = vault.lastError.map { "没同步成：\($0)" } ?? "同步好了" }
                } catch {
                    driveNote = "没选上：\(error.localizedDescription)"
                }
            case .failure(let error):
                driveNote = "没选上：\(error.localizedDescription)"
            }
        }
        .confirmationDialog("断开后手机上的笔记会全部删掉，令牌也从钥匙串里删掉。仓库里的内容不受影响。",
                            isPresented: $confirmWipe, titleVisibility: .visible) {
            Button("断开并删除", role: .destructive) {
                vault.wipe()
                Connection.delete()
                session.connection = nil
                session.cableOnly = false
            }
        }
    }

    /// 一张数字卡：点了去看是哪些。alert：要留意的（还有没交给电脑的）数字用强调色。数是 0 也能点，进去是空状态和说明
    private func stat(_ value: Int, _ label: String, hint: String, alert: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(String(value)).font(.system(size: 30, weight: .bold, design: .rounded).monospacedDigit())
                        .foregroundStyle(alert ? p.accent : p.ink).lineLimit(1).minimumScaleFactor(0.6)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2.opacity(0.55))
                }
                Text(label).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineLimit(1)
            }
            .padding(.horizontal, 16).padding(.vertical, 14)
            .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
            .background(p.paper2, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(p.rule.opacity(0.6)))
            .contentShape(RoundedRectangle(cornerRadius: 16))
        }
        .buttonStyle(StatPress())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityHint(hint)
    }

    /// 读书笔记几篇、批注几条（剪藏、整理稿、会议资料、报告……都算）；顺便数一下加了星标的剪藏
    private func countNotes() {
        let c = Outbox.counts
        outboxCount = c.tasks       // 要电脑来做的任务；手机上已经做好的内容不算，只在上面的状态行里说还没同步
        unsyncedCount = c.sync
        Task {
            starCount = await vault.clipNotes().filter(\.meta.starred).count   // 后台读，文件没变的走缓存
            let notes = await vault.readingNotes()
            readingCount = notes.count
            noteCount = notes.reduce(0) { $0 + $1.noteCount }
        }
    }

    /// 一张卡片：跟上面四个数同样的底色、圆角、描边。title 是卡片上方的组名（小号灰字，跟系统设置一样放在外面），
    /// footer 是卡片下面一行小字。组名到卡片 8，组与组之间 24（外面的 VStack）
    private func card<C: View>(_ title: String?, footer: String? = nil, @ViewBuilder content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title).font(.system(size: 13, weight: .medium)).foregroundStyle(p.ink2).padding(.leading, 4)
            }
            VStack(alignment: .leading, spacing: 0) { content() }
                .font(.system(size: 16))
                .padding(.horizontal, 16).padding(.vertical, 4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(p.rule.opacity(0.6)))
            if let footer {
                Text(footer).font(.system(size: 12.5)).foregroundStyle(p.ink2).lineLimit(2).padding(.horizontal, 4)
            }
        }
    }

    private var divider: some View { Rectangle().fill(p.rule.opacity(0.7)).frame(height: 0.5) }

    /// 开关、存储那一行左边的两行字
    private func line(_ title: String, _ sub: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
            Text(sub).font(.system(size: 12.5)).foregroundStyle(p.ink2).lineLimit(1)
        }
    }

    /// 卡片里的一行操作（换令牌、断开）
    private func action(_ title: String, destructive: Bool = false, _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            Text(title).foregroundStyle(destructive ? Color.hex(0xb42318) : p.accent)
                .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func row(_ icon: String, _ title: String, _ value: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.system(size: 15, weight: .medium)).foregroundStyle(p.accent)
                .frame(width: 32, height: 32).background(p.accent.opacity(0.1), in: Circle())
            Text(title).foregroundStyle(p.ink)
            Spacer()
            Text(value).font(.system(size: 14)).foregroundStyle(p.ink2).lineLimit(1)
            Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.ink2.opacity(0.6))
        }
        .frame(minHeight: 56)
        .contentShape(Rectangle())
    }

    private func bytes(_ n: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file) }

    /// 大标题下的一句话（别的页签都是一行）；数字和按钮在下面的卡片里
    private var syncText: String {
        if vault.syncing { return "正在同步：\(vault.progress.done)/\(vault.progress.total)" }
        if let e = vault.lastError { return "上次同步没成功：\(e)" }
        if vault.deferred > 0 { return "用的是流量，\(vault.deferred) 份整理稿等连上 Wi-Fi 再下" }
        guard let t = vault.lastSync else {
            return session.connection == nil ? "用数据线接上开着 Spark 的电脑，会自动同步" : "还没同步过"
        }
        let last = "上次同步 \(t.formatted(.relative(presentation: .named)))，来自\(vault.lastSource)"
        return unsyncedCount > 0 ? last + "。手机上新做的 \(unsyncedCount) 份还没同步到电脑" : last
    }
}

/// 数字卡按下去暗一点
private struct StatPress: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.6 : 1)
    }
}
