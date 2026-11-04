import SwiftUI

@main
struct SparkApp: App {
    @State private var prefs = ReadPrefs()
    @State private var vault = Vault()
    @State private var readState = ReadState()
    @State private var session = Session()
    @State private var jobs = LocalJobs()
    @State private var running = RunningTasks()

    init() {
        Fonts.shared.registerAll()
        // 下好过的系统字体（宋体、楷体……）每次启动要再请求一次才能用
        Fonts.shared.restoreSystemFonts(BodyFont.builtins.filter(\.downloadable).compactMap(\.fontName))
        NavTitleFont.apply()
    }

    var body: some Scene {
        WindowGroup {
            // Themed 要读阅读偏好，所以放在 .environment 里面一层
            RootView()
                .modifier(Themed())
                .environment(prefs)
                .environment(vault)
                .environment(readState)
                .environment(session)
                .environment(jobs)
                .environment(running)
        }
    }
}

/// 连接状态：钥匙串里有没有仓库和令牌
@Observable
final class Session {
    var connection: Connection? = Connection.load()
    /// 不连 GitHub，只用数据线跟电脑同步（电脑设置页「数据线同步到手机」）
    var cableOnly = SharedStore.cableOnly {
        didSet { SharedStore.cableOnly = cableOnly }
    }
    var ready: Bool { connection != nil || cableOnly }

    init() {
        // 加分享扩展之前存的令牌在 App 自己的钥匙串组里：重存一次，挪进和扩展共用的组
        if let c = connection { try? c.save() }
    }
    var autoSync: Bool {
        get { UserDefaults.standard.object(forKey: "sync.auto") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "sync.auto") }
    }
}

struct RootView: View {
    @Environment(Session.self) private var session
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(ReadPrefs.self) private var prefs
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.scenePhase) private var phase

    var body: some View {
        Group {
            if !session.ready {
                ConnectView()
            } else {
                MainTabs()
            }
        }
        .background(p.paper.ignoresSafeArea())
        .task(id: "\(session.connection?.slug ?? "")|\(session.cableOnly)") {
            vault.connect(session.connection)
            if session.ready, session.autoSync || session.connection == nil { await syncAndSeed() }
            // 上次没跑完的复盘、报告（App 被关了、被系统打断）接着跑
            if session.ready { jobs.resume(vault: vault) }
            if session.ready { await ClipProcessor.shared.run(vault: vault) }
        }
        // 用流量时攒下没下的整理稿和文字记录：一连上 Wi-Fi 就补
        .onChange(of: NetMonitor.shared.onCellular) { _, cellular in
            if !cellular, vault.deferred > 0, session.connection != nil, !vault.syncing {
                Task { await vault.sync() }
            }
        }
        // 第一次有内容（GitHub 第一次同步，或第一次从数据线拷进来）时全部算「读过」，之后新来的才标小圆点
        .onChange(of: vault.lastSync) { _, _ in
            if readState.read.isEmpty, !vault.library.allEpisodes.isEmpty {
                readState.markAll(vault.library.allEpisodes.map(\.id))
            }
        }
        .onChange(of: phase) { _, now in
            // 数据线拷来的东西只是本地文件，回到前台就看一眼，不花流量
            if now == .active, session.ready {
                if vault.importCable() { Task { await syncDevices() } }
                jobs.resume(vault: vault)
                // 分享扩展存进收件箱的剪藏：收进笔记库、写摘要
                Task { await ClipProcessor.shared.run(vault: vault) }
            }
            if now == .active, session.connection != nil, session.autoSync, !vault.syncing,
               (vault.lastSync.map { Date.now.timeIntervalSince($0) > 300 } ?? true) {
                Task { await syncAndSeed() }
            }
            // 刚改过阅读设置就切走：趁这会儿发出去，另一台设备打开时就能用上
            if now == .background, session.connection != nil {
                Task { await DeviceSync.uploadPrefs(vault: vault, prefs: prefs) }
            }
        }
    }

    private func syncAndSeed() async {
        await vault.sync()
        await syncDevices()
    }

    /// 阅读设置、已读状态跟别的设备对一下（DeviceSync）
    private func syncDevices() async {
        // 第一次有内容时整批算「读过」要在合并别的设备的记录之前做，不然 read 已经不空了
        if readState.read.isEmpty, !vault.library.allEpisodes.isEmpty {
            readState.markAll(vault.library.allEpisodes.map(\.id))
        }
        DeviceSync.writePrefs(prefs)
        await DeviceSync.run(vault: vault, prefs: prefs, readState: readState)
    }
}

struct MainTabs: View {
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @Environment(RunningTasks.self) private var running
    @Environment(\.palette) private var p

    var body: some View {
        TabView {
            NavigationStack { LatestView() }
                .tabItem { Label("最新", systemImage: "doc.text") }
            NavigationStack { LibraryView() }
                .tabItem { Label("书架", systemImage: "books.vertical") }
            NavigationStack { ReviewView() }
                .tabItem { Label("复盘", systemImage: "clock.arrow.circlepath") }
            NavigationStack { MeView() }
                .tabItem { Label("我的", systemImage: "person.crop.circle") }
        }
        .toolbarBackground(p.paper2, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .task { await running.watch(vault: vault, jobs: jobs) }
    }
}

extension ReadState {
    func markAll(_ ids: [String]) {
        ids.forEach(mark)
    }
}

/// 列表页通用的底色和导航栏
struct PageStyle: ViewModifier {
    @Environment(\.palette) private var p
    func body(content: Content) -> some View {
        content
            // 系统画的分隔线在切换深浅色时不跟着变色（浅色下会留一条黑线），配色变了就整页重建
            .id(p)
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
    }
}

extension View {
    func pageStyle() -> some View { modifier(PageStyle()) }
}

/// 列表里点进下一页的一行，不带系统的「›」：读东西的列表里那个箭头只是噪音，还把右边的日期、时长往里挤
struct PlainLink<Value: Hashable, Label: View>: View {
    let value: Value
    @ViewBuilder let label: () -> Label

    init(value: Value, @ViewBuilder label: @escaping () -> Label) {
        self.value = value
        self.label = label
    }

    var body: some View {
        ZStack(alignment: .leading) {
            NavigationLink(value: value) { EmptyView() }.opacity(0)
            label()
        }
        .contentShape(Rectangle())
    }
}

/// 导航栏标题（「最新」「书架」这些大标题）用思源黑体，跟页面里的标题一套
enum NavTitleFont {
    static func apply() {
        guard let large = UIFont(name: Fonts.heading, size: 32), let inline = UIFont(name: Fonts.heading, size: 17) else { return }
        let a = UINavigationBarAppearance()
        a.configureWithTransparentBackground()
        a.largeTitleTextAttributes = [.font: large]
        a.titleTextAttributes = [.font: inline]
        UINavigationBar.appearance().standardAppearance = a
        UINavigationBar.appearance().scrollEdgeAppearance = a
        UINavigationBar.appearance().compactAppearance = a
    }
}
