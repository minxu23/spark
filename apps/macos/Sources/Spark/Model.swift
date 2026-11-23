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
    /// 剪藏才有：标签、发表日期（YYYY-MM-DD 或空）
    var tags: [String]?
    var published: String?
    /// 剪藏才有：发表、收录的时刻（ISO 8601 带时差，老文件没有）
    var publishedAt: String?
    var clippedAt: String?
    /// 剪藏缺摘要或者英文没译完
    var needs: Bool?
    /// 星标（能做笔记的都能加：一期节目记在小结上）、置顶（只有剪藏）：文件头 starred / pinned，跟手机同一份
    var starred: Bool?
    var pinned: Bool?
    /// 信息跟进的文章才有：原文链接、是不是已经存进剪藏
    var origin: String?
    var saved: Bool?
}

/// 剪藏列表的排法，跟手机（ClipSort）一样三种
enum ClipSort: String, CaseIterable, Identifiable {
    case clipped, published, name
    var id: String { rawValue }

    var title: String {
        switch self {
        case .clipped: return "收录时间"
        case .published: return "发布时间"
        case .name: return "名称"
        }
    }

    func apply(_ items: [Item]) -> [Item] {
        switch self {
        // 同手机 ClipShelf、电脑 core/clips.order_clipped / order_published：同一天里有时刻的按时刻排，
        // 只知道哪天的排在有时刻的后面；一样的再按收录时刻、路径
        case .clipped: return items.sorted { Self.clippedKey($0) > Self.clippedKey($1) }
        case .published: return items.sorted { Self.publishedKey($0) > Self.publishedKey($1) }
        case .name: return items.sorted { $0.title.lowercased().localizedStandardCompare($1.title.lowercased()) == .orderedAscending }
        }
    }
}

extension ClipSort {
    static func clippedKey(_ i: Item) -> String {
        DocTime.sortKey(at: i.clippedAt ?? "", day: i.date) + "|" + i.id
    }

    static func publishedKey(_ i: Item) -> String {
        let pub = (i.published ?? "").isEmpty && (i.publishedAt ?? "").isEmpty
            ? DocTime.sortKey(at: i.clippedAt ?? "", day: i.date)
            : DocTime.sortKey(at: i.publishedAt ?? "", day: i.published ?? "")
        return pub + "|" + clippedKey(i)
    }
}

enum Tool: String, CaseIterable, Identifiable {
    case summit, podcast, track, notes, review, readingNotes, jottings, cleanup, settings
    var id: String { rawValue }

    var title: String {
        switch self {
        case .summit: return "Summit 总结"
        case .podcast: return "Podcast 跟进"
        case .track: return "信息跟进"
        case .notes: return "Notes2Insight"
        case .review: return "复盘"
        case .readingNotes: return "读书笔记"
        case .jottings: return "笔记"
        case .cleanup: return "清理工具"
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
        case .readingNotes: return "highlighter"
        case .jottings: return "note.text"
        case .cleanup: return "trash"
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
        case .readingNotes: return "read/notes"
        case .jottings: return "read/jottings"
        case .cleanup: return "cleanup"
        case .settings: return "settings"
        }
    }
}

/// 侧边栏选中的位置。存成字符串，下次打开回到这里
enum Place: Hashable {
    case latest, starred, clips, reports
    case show(String)
    case tool(Tool)

    var key: String {
        switch self {
        case .latest: return "latest"
        case .starred: return "starred"
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
            self = key == "clips" ? .clips : key == "reports" ? .reports : key == "starred" ? .starred : .latest
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
    /// 补摘要和翻译：正在做时显示进度，做完显示结果
    @Published var fillStatus: String?
    @Published var filling = false
    @Published var clipComposer = ClipComposer()

    func addClip() {
        if !clipComposer.savedPath.isEmpty { clipComposer = ClipComposer() }
        addClipVisible = true
    }
    @Published var place: Place {
        didSet {
            UserDefaults.standard.set(place.key, forKey: "place")
            if place != oldValue { placeChanged() }
            // 读书笔记每次点开都重新读一遍：刚划的线、写的批注要能看到
            if place == .tool(.readingNotes), place != oldValue { pane(.readingNotes).webView.reload() }
            // 笔记页（随手记 + 读书笔记）同样：手机上刚写的、刚改的要能看到
            if place == .tool(.jottings), place != oldValue { pane(.jottings).webView.reload() }
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
        reader.openLink = { [weak self] path in ["/read/notes", "/read/jottings"].contains(path) && (self?.follow(path) ?? false) }
        server.onChange = { [weak self] in
            Task { @MainActor in self?.serverChanged() }
        }
    }

    var pickedItem: Item? { items.first { $0.id == picked } }
    var placeTitle: String {
        switch place {
        case .latest: return "最新"
        case .starred: return "星标"
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
        // 读书笔记、笔记页里点到剪藏、别的工具页：换到对应的位置打开（见 follow）
        if tool == .readingNotes || tool == .jottings { pane.openLink = { [weak self] in self?.follow($0) ?? false } }
        toolPanes[tool] = pane
        return pane
    }

    /// 读书笔记页点标题 → 剪藏列表里打开那篇；剪藏读后感卡下面的「读书笔记」→ 换到读书笔记
    func follow(_ path: String) -> Bool {
        if path.hasPrefix("/read/f/剪藏/") {
            openClip("Spark/" + path.dropFirst("/read/f/".count))
            return true
        }
        if path == "/read/notes" {
            place = .tool(.readingNotes)
            return true
        }
        if path == "/read/jottings" {
            place = .tool(.jottings)
            return true
        }
        return false
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

    /// 能改名称的：剪藏和复盘（同手机 DocEdit.editable），按阅读页的路径认
    var canRetitle: Bool {
        guard let id = pickedItem?.id, currentPane === reader else { return false }
        return id.hasPrefix("剪藏/") || id.hasPrefix("@r/复盘/")
    }

    /// 改名称：只改文件头的 title 和第一行标题，文件名不动。返回出错的说明，成功返回 nil
    func retitle(_ title: String) async -> String? {
        guard let item = pickedItem else { return "没有选中文章" }
        // 页面打开时的修改时间：在别处（Obsidian）改过的话服务会拒绝，免得覆盖
        let mtime = (try? await reader.webView.evaluateJavaScript("document.querySelector('article.doc')?.dataset.mtime || ''")) as? String ?? ""
        var req = URLRequest(url: URL(string: "read/api/retitle", relativeTo: SparkServer.baseURL)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["path": item.id, "title": title, "mtime": mtime])
        guard let (data, response) = try? await SparkServer.session.data(for: req) else { return "连不上 Spark 服务" }
        if (response as? HTTPURLResponse)?.statusCode != 200 {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            return body?["error"] as? String ?? "改名称没成功"
        }
        reader.webView.reload()
        loadList()
        return nil
    }

    /// 删一篇剪藏或复盘：服务把笔记库里那份移进废纸篓（跟手机删了交给电脑一样）。返回出错的说明，成功返回 nil
    func delete(_ item: Item) async -> String? {
        var req = URLRequest(url: URL(string: "read/api/delete", relativeTo: SparkServer.baseURL)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["path": item.id])
        guard let (data, response) = try? await SparkServer.session.data(for: req) else { return "连不上 Spark 服务" }
        if (response as? HTTPURLResponse)?.statusCode != 200 {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            return body?["error"] as? String ?? "没删成"
        }
        // 列表里挪到下一篇；删的是最后一篇就回到上一篇
        let visible = items
        if let at = visible.firstIndex(where: { $0.id == item.id }) {
            let rest = visible.filter { $0.id != item.id }
            picked = rest.isEmpty ? nil : rest[min(at, rest.count - 1)].id
        }
        if picked == nil { openHome() }
        loadList()
        return nil
    }

    /// 加星标、置顶（或取消）：写进剪藏的文件头，手机上同步看到。返回出错的说明，成功返回 nil
    func flag(_ item: Item, _ key: String, _ on: Bool) async -> String? {
        var req = URLRequest(url: URL(string: "read/api/flag", relativeTo: SparkServer.baseURL)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["path": item.id, "key": key, "on": on])
        guard let (data, response) = try? await SparkServer.session.data(for: req) else { return "连不上 Spark 服务" }
        if (response as? HTTPURLResponse)?.statusCode != 200 {
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            return body?["error"] as? String ?? "没改成"
        }
        if let i = items.firstIndex(where: { $0.id == item.id }) {
            if key == "starred" { items[i].starred = on } else { items[i].pinned = on }
        }
        // 在「星标」里取消了：从列表里拿掉
        if place == .starred, key == "starred", !on { items.removeAll { $0.id == item.id } }
        // 正开着这篇的话，页面上的修改时间变了，重新载入免得接着划线时被当成「在别处改过」
        if picked == item.id, currentPane === reader { reader.webView.reload() }
        return nil
    }

    /// 正在存进剪藏的原文链接；存完的结果（标题、说明、存下的路径）给界面弹提示
    @Published var clipSaving: Set<String> = []
    @Published var clipNotice: ClipNotice?
    struct ClipNotice: Identifiable {
        let id = UUID()
        let title: String
        let message: String
        var path = ""
    }

    /// 信息跟进里的一篇存进剪藏：按原文链接重新抓全文和图片，跟「添加剪藏」走同一套（同手机右滑「存进剪藏」）
    func saveToClips(_ item: Item) {
        guard let origin = item.origin, item.saved != true, !clipSaving.contains(origin) else { return }
        clipSaving.insert(origin)
        Task {
            defer { clipSaving.remove(origin) }
            let c = ClipComposer()
            c.url = origin
            await c.fetch()
            if c.article != nil { await c.save() }
            guard c.error.isEmpty, !c.savedPath.isEmpty else {
                clipNotice = ClipNotice(title: "没存进剪藏", message: c.error.isEmpty ? "没抓到这篇的正文" : c.error)
                return
            }
            for i in items.indices where items[i].origin == origin { items[i].saved = true }
            let note = c.warning.isEmpty ? "" : "\n" + c.warning
            clipNotice = ClipNotice(title: "存进剪藏了", message: "《\(c.title)》在「剪藏」里，摘要和翻译在后台写。" + note,
                                    path: c.savedPath)
        }
    }

    /// 已存的剪藏补摘要和翻译（core/clip_capture.py 的 start_fill）。paths 是阅读页的路径（剪藏/…）
    func fill(_ paths: [String], mode: String = "fill") {
        guard !filling, !paths.isEmpty else { return }
        filling = true
        fillStatus = "准备中"
        Task {
            defer { filling = false }
            var req = URLRequest(url: URL(string: "read/api/clips/fill", relativeTo: SparkServer.baseURL)!)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["paths": paths, "mode": mode])
            guard var job = await post(req) else { fillStatus = "连不上 Spark 服务"; return }
            if let error = job["error"] as? String, job["id"] == nil { fillStatus = error; return }
            while job["done"] as? Bool != true, let id = job["id"] as? String {
                fillStatus = job["phase"] as? String
                try? await Task.sleep(for: .seconds(1))
                guard let next = await post(URLRequest(url: URL(string: "read/api/clips/" + id, relativeTo: SparkServer.baseURL)!)) else { break }
                job = next
            }
            let warning = job["warning"] as? String ?? ""
            fillStatus = (job["error"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? (warning.isEmpty ? "补好了" : "补好了。" + warning)
            if let id = picked, paths.contains(id) { reader.webView.reload() }
            loadList()
        }
    }

    private var lastPhoneAt: Double = 0
    @Published var phoneCheckError: String?

    /// 马上看一眼手机：读发件箱、存下手机上生成好的内容（一两秒，不拷笔记库）
    func checkPhone() {
        guard let id = activity.phone?.device_id, !id.isEmpty else { return }
        Task {
            var req = URLRequest(url: URL(string: "api/cable", relativeTo: SparkServer.baseURL)!)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["device": id, "quick": true])
            guard let d = await post(req) else { phoneCheckError = "连不上 Spark 服务"; return }
            if let e = d["error"] as? String, !e.isEmpty { phoneCheckError = e }
        }
    }

    /// 暂停 GitHub 同步（停掉正在推的 git，推上去的批次留着）
    func stopSync() {
        Task {
            var req = URLRequest(url: URL(string: "api/sync/stop", relativeTo: SparkServer.baseURL)!)
            req.httpMethod = "POST"
            _ = await post(req)
        }
    }

    /// 打开设置里的某一节（sync：GitHub 同步，cable：数据线同步）
    func openSettings(_ anchor: String) {
        place = .tool(.settings)
        pane(.settings).load(URL(string: "settings#" + anchor, relativeTo: SparkServer.baseURL)!)
    }

    private func post(_ req: URLRequest) async -> [String: Any]? {
        guard let (data, _) = try? await SparkServer.session.data(for: req) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
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

        // 看手机时带回了新内容（手机上存的剪藏、复盘）：书架刷新一下
        if let p = activity.phone, p.at != lastPhoneAt {
            if lastPhoneAt != 0, p.saved > 0 { refresh() }
            lastPhoneAt = p.at
        }
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
