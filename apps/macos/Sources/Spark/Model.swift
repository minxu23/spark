import AppKit
import Foundation

struct ShowInfo: Codable, Identifiable, Hashable {
    let rel: String
    let name: String
    let kind: String
    let count: String
    let latest: Double
    var id: String { rel }
    /// 「41 期」「12 个议题」「30 篇」前面的数字
    var number: Int { Int(count.prefix { $0.isNumber }) ?? 0 }
}

enum ShowSort: String, CaseIterable, Identifiable {
    case latest, name, count, manual
    var id: String { rawValue }

    var title: String {
        switch self {
        case .latest: return "最近更新"
        case .name: return "名称"
        case .count: return "数量"
        case .manual: return "手动（拖动排序）"
        }
    }

    var shortTitle: String { self == .manual ? "手动" : title }

    func apply(_ shows: [ShowInfo], order: [String]) -> [ShowInfo] {
        switch self {
        case .latest: return shows.sorted { $0.latest > $1.latest }
        case .name: return shows.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        case .count: return shows.sorted { $0.number > $1.number }
        case .manual:
            // 新出现的节目还没排过，放在最前面
            let rank = Dictionary(zip(order, order.indices)) { first, _ in first }
            return shows.sorted { (rank[$0.rel] ?? -1, $1.latest) < (rank[$1.rel] ?? -1, $0.latest) }
        }
    }
}

struct ItemFile: Codable, Hashable {
    let label: String
    let url: String
}

struct Item: Codable, Identifiable, Hashable {
    let id: String
    let title: String
    let date: String
    let show: String
    let files: [ItemFile]
    var read: Bool
    var snippet: String?
}

enum Tool: String, CaseIterable, Identifiable {
    case summit, podcast, track, notes, review, settings
    var id: String { rawValue }

    var title: String {
        switch self {
        case .summit: return "Summit 总结"
        case .podcast: return "Podcast 跟进"
        case .track: return "信息跟进"
        case .notes: return "Notes2Insight"
        case .review: return "复盘"
        case .settings: return "设置"
        }
    }

    var symbol: String {
        switch self {
        case .summit: return "calendar"
        case .podcast: return "mic"
        case .track: return "dot.radiowaves.up.forward"
        case .notes: return "doc.text.magnifyingglass"
        case .review: return "arrow.counterclockwise"
        case .settings: return "gearshape"
        }
    }

    var path: String {
        switch self {
        case .summit: return "summit/?mode=summit"
        case .podcast: return "summit/?mode=series"
        case .track: return "summit/?mode=track"
        case .notes: return "notes/"
        case .review: return "review/"
        case .settings: return "settings"
        }
    }
}

/// 侧边栏选中的位置。存成字符串，下次打开回到这里
enum Place: Hashable {
    case latest, clips, reports
    case show(String)
    case tool(Tool)

    var key: String {
        switch self {
        case .latest: return "latest"
        case .clips: return "clips"
        case .reports: return "reports"
        case .show(let rel): return "show:" + rel
        case .tool(let tool): return "tool:" + tool.rawValue
        }
    }

    init(key: String) {
        if key.hasPrefix("show:") {
            self = .show(String(key.dropFirst(5)))
        } else if key.hasPrefix("tool:"), let tool = Tool(rawValue: String(key.dropFirst(5))) {
            self = .tool(tool)
        } else {
            self = key == "clips" ? .clips : key == "reports" ? .reports : .latest
        }
    }

    var query: String {
        if case .show(let rel) = self { return rel }
        return key
    }
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    let server = SparkServer()
    let reader = WebPane()
    private var toolPanes: [Tool: WebPane] = [:]

    @Published var state = SparkServer.State.starting
    @Published var activity = SparkServer.Activity()
    @Published var shows: [ShowInfo] = []
    @Published var items: [Item] = []
    @Published var homeURL = ""
    @Published var loadingItems = false
    @Published var search = ""
    @Published var findVisible = false
    @Published var addClipVisible = false
    @Published var clipComposer = ClipComposer()

    func addClip() {
        if !clipComposer.savedPath.isEmpty { clipComposer = ClipComposer() }
        addClipVisible = true
    }
    @Published var place: Place {
        didSet {
            UserDefaults.standard.set(place.key, forKey: "place")
            if place != oldValue { placeChanged() }
        }
    }
    @Published var picked: Item.ID? {
        didSet {
            UserDefaults.standard.set(picked, forKey: "picked")
            if picked != oldValue { pickChanged() }
        }
    }
    @Published var fileIndex = 0 {
        didSet { if fileIndex != oldValue { openPicked() } }
    }

    private var listTask: Task<Void, Never>?
    private var searchTask: Task<Void, Never>?
    private var keepAwake: NSObjectProtocol?
    private var wasBusy = false
    private var needsFirstPage = true

    init() {
        place = Place(key: UserDefaults.standard.string(forKey: "place") ?? "latest")
        picked = UserDefaults.standard.string(forKey: "picked")
        if let library: Library = readCache("shows") { shows = library.shows }
        showCachedList()
        server.onChange = { [weak self] in
            Task { @MainActor in self?.serverChanged() }
        }
    }

    var pickedItem: Item? { items.first { $0.id == picked } }
    var placeTitle: String {
        switch place {
        case .latest: return "最新"
        case .clips: return "剪藏"
        case .reports: return "报告"
        case .show(let rel): return (rel as NSString).lastPathComponent
        case .tool(let tool): return tool.title
        }
    }
    var currentPane: WebPane {
        if case .tool(let tool) = place { return pane(tool) }
        return reader
    }

    func start() {
        server.start()
    }

    func pane(_ tool: Tool) -> WebPane {
        if let pane = toolPanes[tool] { return pane }
        let pane = WebPane()
        pane.load(URL(string: tool.path, relativeTo: SparkServer.baseURL)!)
        toolPanes[tool] = pane
        return pane
    }

    func refresh() {
        guard state == .running else { return }
        Task {
            if let library: Library = await fetch("read/api/library") {
                shows = library.shows
                writeCache("shows", library)
            }
        }
        loadList()
    }

    func runSearch() {
        searchTask?.cancel()
        let q = search.trimmingCharacters(in: .whitespaces)
        if q.isEmpty {
            loadList()
            return
        }
        searchTask = Task {
            try? await Task.sleep(for: .milliseconds(220))
            guard !Task.isCancelled else { return }
            let path = "read/api/search?q=" + (q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
            if let found: ItemList = await fetch(path), !Task.isCancelled {
                items = found.items
                homeURL = ""
            }
        }
    }

    func openClip(_ path: String) {
        place = .clips
        search = ""
        listTask?.cancel()
        listTask = Task {
            guard let list: ItemList = await fetch("read/api/items?place=clips"), !Task.isCancelled else { return }
            items = list.items
            homeURL = list.home ?? ""
            needsFirstPage = false
            loadingItems = false
            picked = String(path.dropFirst("Spark/".count))
            openPicked()
        }
    }

    func openHome() {
        guard !homeURL.isEmpty else { return }
        picked = nil
        reader.load(URL(string: String(homeURL.dropFirst()), relativeTo: SparkServer.baseURL)!)
    }

    func go(_ step: Int) {
        let visible = items
        guard let at = visible.firstIndex(where: { $0.id == picked }) else { return }
        let next = at + step
        if visible.indices.contains(next) { picked = visible[next].id }
    }

    func stepZoom(_ delta: CGFloat) {
        let view = currentPane.webView
        view.pageZoom = delta == 0 ? 1 : min(max(view.pageZoom + delta, 0.5), 2.5)
    }

    private func loadList() {
        guard state == .running, case let place = place else { return }
        if case .tool = place { return }
        listTask?.cancel()
        loadingItems = items.isEmpty
        let query = place.query.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        let cacheName = "items-" + query
        listTask = Task {
            let list: ItemList? = await fetch("read/api/items?place=" + query)
            guard !Task.isCancelled, let list else { return }
            if search.isEmpty {
                items = list.items
                homeURL = list.home ?? ""
                writeCache(cacheName, list)
            }
            loadingItems = false
            guard needsFirstPage else { return }
            needsFirstPage = false
            if pickedItem != nil {
                openPicked()
            } else if !homeURL.isEmpty {
                openHome()
            } else {
                picked = items.first?.id
            }
        }
    }

    private func placeChanged() {
        search = ""
        if case .tool = place { return }
        items = []
        homeURL = ""
        picked = nil
        needsFirstPage = true
        showCachedList()
        loadList()
    }

    private func pickChanged() {
        if fileIndex != 0 { fileIndex = 0 } else { openPicked() }
    }

    private func openPicked() {
        guard let item = pickedItem, item.files.indices.contains(fileIndex) else { return }
        reader.load(URL(string: String(item.files[fileIndex].url.dropFirst()), relativeTo: SparkServer.baseURL)!)
        if let at = items.firstIndex(where: { $0.id == picked }) { items[at].read = true }
    }

    private func serverChanged() {
        let old = state
        state = server.state
        activity = server.activity
        if state == .running, old != .running { refresh() }

        let busy = activity.jobs > 0 || activity.syncing
        // 和原来启动脚本里的 caffeinate -i 一样：只防闲置休眠，合上盖子照常睡
        if busy, keepAwake == nil {
            keepAwake = ProcessInfo.processInfo.beginActivity(options: .idleSystemSleepDisabled, reason: "Spark 任务进行中")
        } else if !busy, let token = keepAwake {
            ProcessInfo.processInfo.endActivity(token)
            keepAwake = nil
        }
        // 任务跑完，书架上可能多了新内容
        if wasBusy, !busy { refresh() }
        wasBusy = busy
    }

    private func fetch<T: Decodable>(_ path: String) async -> T? {
        guard let url = URL(string: path, relativeTo: SparkServer.baseURL),
              let (data, _) = try? await SparkServer.session.data(from: url) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    // 上次的书架存一份：打开时不用等服务，服务起来再换成新的

    private func showCachedList() {
        if case .tool = place { return }
        let query = place.query.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        if let list: ItemList = readCache("items-" + query) {
            items = list.items
            homeURL = list.home ?? ""
        }
    }

    private func cacheFile(_ name: String) -> URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("Spark")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent(String(name.prefix(200)) + ".json")
    }

    private func readCache<T: Decodable>(_ name: String) -> T? {
        guard let data = try? Data(contentsOf: cacheFile(name)) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }

    private func writeCache<T: Encodable>(_ name: String, _ value: T) {
        if let data = try? JSONEncoder().encode(value) { try? data.write(to: cacheFile(name), options: .atomic) }
    }
}

private struct Library: Codable { let shows: [ShowInfo] }
private struct ItemList: Codable {
    let items: [Item]
    let home: String?
}
