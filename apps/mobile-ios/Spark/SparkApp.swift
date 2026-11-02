import SwiftUI

@main
struct SparkApp: App {
    @State private var prefs = ReadPrefs()
    @State private var vault = Vault()
    @State private var readState = ReadState()
    @State private var session = Session()

    init() {
        Fonts.shared.registerAll()
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
        }
    }
}

/// 连接状态：钥匙串里有没有仓库和令牌
@Observable
final class Session {
    var connection: Connection? = Connection.load()
    var autoSync: Bool {
        get { UserDefaults.standard.object(forKey: "sync.auto") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "sync.auto") }
    }
}

struct RootView: View {
    @Environment(Session.self) private var session
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @Environment(\.scenePhase) private var phase

    var body: some View {
        Group {
            if session.connection == nil {
                ConnectView()
            } else {
                MainTabs()
            }
        }
        .background(p.paper.ignoresSafeArea())
        .task(id: session.connection?.slug) {
            vault.connect(session.connection)
            if session.connection != nil, session.autoSync { await syncAndSeed() }
        }
        .onChange(of: phase) { _, now in
            if now == .active, session.connection != nil, session.autoSync, !vault.syncing,
               (vault.lastSync.map { Date.now.timeIntervalSince($0) > 300 } ?? true) {
                Task { await syncAndSeed() }
            }
        }
    }

    private func syncAndSeed() async {
        let first = vault.lastSync == nil
        await vault.sync()
        // 第一次同步下来的全部算「读过」，之后新来的才标小圆点
        if first, vault.lastError == nil { readState.markAll(vault.library.allEpisodes.map(\.id)) }
    }
}

struct MainTabs: View {
    @Environment(\.palette) private var p

    var body: some View {
        TabView {
            NavigationStack { LatestView() }
                .tabItem { Label("最新", systemImage: "doc.text") }
            NavigationStack { LibraryView() }
                .tabItem { Label("书架", systemImage: "books.vertical") }
            NavigationStack { TasksView() }
                .tabItem { Label("任务", systemImage: "clock") }
            NavigationStack { MeView() }
                .tabItem { Label("我的", systemImage: "person.crop.circle") }
        }
        .toolbarBackground(p.paper2, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
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
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .toolbarBackground(p.paper, for: .navigationBar)
    }
}

extension View {
    func pageStyle() -> some View { modifier(PageStyle()) }
}
