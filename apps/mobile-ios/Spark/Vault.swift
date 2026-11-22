import Foundation
import SwiftUI

/// 手机上的那份笔记库：GitHub 仓库的文件树 + 下载下来的文件。
///
/// 同步时先拿整棵树（一次请求），再按 blob 的 sha 比对，只下载变了的。第一次同步下哪些：
/// - 轻量（默认）：处理记录 .manifest.json（里面就有每期的标题、日期、一句话总结和小结）、
///   各节目的总结、信息跟进的文章和简报、笔记洞察的报告。小结全部能离线看。
/// - 全部：再加整理稿和文字记录。
/// 没下的稿子打开时再下载，下过的留在手机上。
@MainActor
@Observable
final class Vault {
    enum DownloadScope: String { case light, all }

    private(set) var tree: [String: TreeItem] = [:]
    private(set) var library = Library.empty { didSet { libraryVersion &+= 1 } }
    /// 书架每重建一次加一：检索拿它判断上次组好的条目还能不能用
    @ObservationIgnored private(set) var libraryVersion = 0
    private(set) var syncing = false
    private(set) var progress: (done: Int, total: Int) = (0, 0)
    private(set) var lastSync: Date?
    private(set) var headSHA = ""
    private(set) var headDate: Date?
    private(set) var lastError: String?
    /// 上一次同步多出来的单集（给「最新」顶上那句提示用）
    private(set) var lastAdded = 0
    private(set) var lastUpdated = 0
    /// 上一次同步是从哪来的：GitHub 或数据线
    private(set) var lastSource = "GitHub"
    private var lastCable: String?

    /// 用流量时不下整理稿和文字记录（同步时跳过，打开时先问）
    var wifiOnly = UserDefaults.standard.object(forKey: "sync.wifiOnly") as? Bool ?? true {
        didSet { UserDefaults.standard.set(wifiOnly, forKey: "sync.wifiOnly") }
    }
    /// 上一次同步因为在用流量而没下的整理稿和文字记录份数（连上 Wi-Fi 再下）
    private(set) var deferred = 0

    /// 现在不该下整理稿和文字记录
    var holdLazy: Bool { wifiOnly && NetMonitor.shared.onCellular }

    func isLazy(_ path: String) -> Bool {
        let parts = path.split(separator: "/")
        return parts.contains("transcripts") || parts.contains("speech")
    }

    var scope: DownloadScope {
        get { DownloadScope(rawValue: UserDefaults.standard.string(forKey: "vault.scope") ?? "") ?? .light }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "vault.scope") }
    }

    private var local: [String: String] = [:]   // path -> 已下载的 sha
    private var github: GitHub?

    nonisolated static var root: URL {
        URL.applicationSupportDirectory.appending(path: "vault", directoryHint: .isDirectory)
    }
    private static var stateURL: URL { root.appending(path: ".spark-state.json") }

    init() {
        loadState()
        rebuildLibrary()
        if let p = Self.pendingProblem { lastError = p }
    }

    func connect(_ conn: Connection?) {
        github = conn.map(GitHub.init)
    }

    var api: GitHub? { github }

    // MARK: 状态文件

    private struct State: Codable {
        var tree: [TreeItem]
        var local: [String: String]
        var lastSync: Date?
        var headSHA: String
        var headDate: Date?
        var lastCable: String?
        var lastSource: String?
    }

    private func loadState() {
        guard let data = try? Data(contentsOf: Self.stateURL),
              let s = try? JSONDecoder().decode(State.self, from: data) else { return }
        tree = Dictionary(uniqueKeysWithValues: s.tree.map { ($0.path, $0) })
        local = s.local
        lastSync = s.lastSync
        headSHA = s.headSHA
        headDate = s.headDate
        lastCable = s.lastCable
        lastSource = s.lastSource ?? "GitHub"
    }

    private func saveState() {
        let s = State(tree: Array(tree.values), local: local, lastSync: lastSync, headSHA: headSHA, headDate: headDate,
                      lastCable: lastCable, lastSource: lastSource)
        try? FileManager.default.createDirectory(at: Self.root, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: Self.stateURL, options: .atomic) }
    }

    // MARK: 同步

    /// 第一次同步要下的文件
    func isEager(_ path: String) -> Bool {
        let name = (path as NSString).lastPathComponent
        if name == ".manifest.json" || path == "\(Clips.folder)/.renames.json" { return true }   // 改名记录同步完就要用
        guard name.hasSuffix(".md") else { return false }
        let parts = path.split(separator: "/").map(String.init)
        if parts.contains("transcripts") || parts.contains("speech") {
            return scope == .all
        }
        return true
    }

    func sync() async {
        guard !syncing else { return }
        // 选了 Google Drive 文件夹：从那里拉（电脑写进去的跟数据线拷的一样），不走 GitHub
        if DriveSync.isOn { await syncDrive(); return }
        importCable()
        guard let github else { return }
        syncing = true
        lastError = nil
        defer { syncing = false }
        do {
            let head = try await github.head()
            let items = try await github.tree(commit: head.sha)
            let before = Set(library.allEpisodes.map(\.id))
            var newTree = Dictionary(uniqueKeysWithValues: items.map { ($0.path, $0) })
            keepGenerated(&newTree)
            // 要下的：轻量范围里的、之前下过但变了的
            var wanted = items.filter { item in
                newTree[item.path] != nil && (isEager(item.path) || local[item.path] != nil) && local[item.path] != item.sha
            }
            // 用流量时整理稿和文字记录先不下（包括下过但电脑上改过的），等连上 Wi-Fi
            let hold = holdLazy
            deferred = hold ? wanted.filter { isLazy($0.path) }.count : 0
            if hold { wanted.removeAll { isLazy($0.path) } }
            let updated = wanted.filter { local[$0.path] != nil }.count
            progress = (0, wanted.count)
            try await download(wanted)
            applyClipRenames(&newTree, computer: Set(items.map(\.path)))   // 电脑上改过名的剪藏（刚下的改名记录）
            // 仓库里已经没有的文件：手机上也删掉（removeGone 防着一次删太多、删掉还没同步出去的）
            let held = removeGone(newTree, source: "GitHub")
            pruneEmptyFolders()
            // 还没确认的修改：收下了就放手，盖了就写回
            reconcilePending(&newTree, issues: await issueStatuses(), held: held != nil)
            tree = newTree
            mergePendingRows()
            headSHA = head.sha
            headDate = head.date
            lastSync = .now
            lastSource = "GitHub"
            lastError = held ?? Self.pendingProblem
            saveState()
            rebuildLibrary()
            // 分享扩展里「放到哪」要列的文件夹
            SharedStore.folders = (library.shows(.series) + library.shows(.summit))
                .map { SharedStore.Folder(name: $0.name, kind: $0.kind.rawValue) }
            lastAdded = Set(library.allEpisodes.map(\.id)).subtracting(before).count
            await ReadLog.upload(via: github)
            lastUpdated = before.isEmpty ? 0 : updated
            if before.isEmpty { lastAdded = 0 }   // 第一次同步不算「新增」
        } catch {
            lastError = error.localizedDescription
        }
    }

    private func download(_ items: [TreeItem]) async throws {
        guard let github else { return }
        var iterator = items.makeIterator()
        try await withThrowingTaskGroup(of: (TreeItem, Data).self) { group in
            func addNext() -> Bool {
                guard let item = iterator.next() else { return false }
                group.addTask { (item, try await github.blob(item.sha)) }
                return true
            }
            for _ in 0..<8 where addNext() {}
            while let (item, data) = try await group.next() {
                try write(data, to: item.path)
                local[item.path] = item.sha
                progress.done += 1
                if progress.done % 50 == 0 { saveState() }
                _ = addNext()
            }
        }
    }

    /// 删完文件后留下的空文件夹（比如电脑上整理时的备份文件夹不再同步了）一起删掉
    private func pruneEmptyFolders() {
        let fm = FileManager.default
        guard let walker = fm.enumerator(at: Self.root, includingPropertiesForKeys: [.isDirectoryKey]) else { return }
        // 从最深的往上删，删掉子文件夹后父文件夹可能也空了
        let dirs = walker.compactMap { $0 as? URL }
            .filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
            .sorted { $0.pathComponents.count > $1.pathComponents.count }
        for d in dirs where (try? fm.contentsOfDirectory(atPath: d.path).isEmpty) == true {
            try? fm.removeItem(at: d)
        }
    }

    private func fileURL(_ path: String) -> URL {
        Self.root.appending(path: path, directoryHint: .notDirectory)
    }

    private func write(_ data: Data, to path: String) throws {
        let url = fileURL(path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }

    // MARK: 读文件

    func isDownloaded(_ path: String) -> Bool {
        guard let item = tree[path] else { return local[path] != nil }
        return local[path] == item.sha
    }

    /// 手机上看到的这篇：有还没确认电脑收下的修改，用那份修改（电脑同步过来的文件可能已经把本地文件盖了，见 PendingEdits）
    func localText(_ path: String) -> String? {
        if let r = pending[path] { return r.content }
        return diskText(path)
    }

    /// 本地文件本身
    func diskText(_ path: String) -> String? {
        guard local[path] != nil, let data = try? Data(contentsOf: fileURL(path)) else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    /// 打开整理稿或文字记录时正在用流量、又设了只在 Wi-Fi 下载
    struct OnCellular: LocalizedError {
        let size: Int
        var errorDescription: String? {
            let s = size > 0 ? "（\(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))）" : ""
            return "现在用的是流量。按设置，整理稿和文字记录只在 Wi-Fi 下下载\(s)。"
        }
    }

    /// 读一个文件：手机上有最新的就直接读，没有就下载
    func text(_ path: String, allowCellular: Bool = false) async throws -> String {
        if let r = pending[path] { return r.content }
        if isDownloaded(path), let t = localText(path) { return t }
        if isLazy(path), holdLazy, !allowCellular {
            // 手机上有旧版本就先看旧的，不为这个花流量
            if let t = localText(path) { return t }
            throw OnCellular(size: tree[path]?.size ?? 0)
        }
        guard let item = tree[path] else {
            if let t = localText(path) { return t }
            throw SparkError("仓库里没有这个文件，可能电脑上还没同步上来")
        }
        guard let github else { throw SparkError("还没连接笔记库") }
        let data = try await github.blob(item.sha)
        try write(data, to: path)
        local[path] = item.sha
        saveState()
        return String(decoding: data, as: UTF8.self)
    }

    // MARK: 存储

    var storageBytes: (total: Int, lazy: Int) {
        var total = 0, lazy = 0
        for path in local.keys {
            let size = tree[path]?.size ?? 0
            total += size
            if isLazy(path) { lazy += size }
        }
        return (total, lazy)
    }

    /// 清掉整理稿和文字记录（小结、节目总结在处理记录里，留着）
    func clearLazy() {
        for path in local.keys where isLazy(path) {
            try? FileManager.default.removeItem(at: fileURL(path))
            local[path] = nil
        }
        saveState()
    }

    /// 从 Google Drive 文件夹同步一次（DriveSync），拷进来的再按数据线那套收进笔记库
    private func syncDrive() async {
        syncing = true
        lastError = nil
        defer { syncing = false }
        do {
            let copied = try await DriveSync.run(into: Self.root) { n, total in
                Task { @MainActor [weak self] in self?.progress = (n, total) }
            }
            let before = Set(library.allEpisodes.map(\.id))
            importCable()
            lastSync = .now
            lastSource = "Google Drive"
            lastUpdated = before.isEmpty ? 0 : copied
            saveState()
        } catch {
            lastError = error.localizedDescription
        }
    }

    /// 断开连接：删掉手机上的整份笔记库
    // MARK: 数据线同步（电脑端见 core/cable_sync.py）

    private static var cableManifestURL: URL { root.appending(path: ".spark-cable.json") }
    private static var cableResultsURL: URL { root.appending(path: ".spark-cable-tasks.json") }

    /// 电脑用数据线拷进来的一批文件：按附带的清单（每个文件的 git blob sha，跟 GitHub 上一样）
    /// 记成「已下载」，之后再从 GitHub 同步时没变的就不用重下。清单没变（同一次拷贝）就什么都不做。
    @discardableResult
    func importCable() -> Bool {
        guard let data = try? Data(contentsOf: Self.cableManifestURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let generated = obj["generated_at"] as? String, generated != lastCable,
              let files = obj["files"] as? [String: [Any]] else {
            // 笔记库没重新拷，可能只推了任务结果（电脑看一眼就存下了手机的修改）：发件箱里做完的清掉，失败的标出来
            var t = tree
            if reconcilePending(&t) { tree = t; saveState(); rebuildLibrary() }
            return false
        }
        let before = Set(library.allEpisodes.map(\.id))
        var newTree: [String: TreeItem] = [:]
        for (path, v) in files {
            guard let sha = v.first as? String else { continue }
            newTree[path] = TreeItem(path: path, sha: sha, size: v.count > 1 ? (v[1] as? Int ?? 0) : 0)
            if FileManager.default.fileExists(atPath: fileURL(path).path) { local[path] = sha }
        }
        applyClipRenames(&newTree, computer: Set(files.keys))   // 电脑上改过名的剪藏
        keepGenerated(&newTree)
        // 电脑上已经没有的文件（删了、移进了废纸篓）：手机上也删掉
        let held = removeGone(newTree, source: "电脑")
        pruneEmptyFolders()
        reconcilePending(&newTree, held: held != nil)   // 还没确认的修改：收下了就放手，电脑拷进来的文件盖了就写回
        tree = newTree
        mergePendingRows()
        lastCable = generated
        lastSync = .now
        lastSource = "数据线"
        headDate = ISO8601DateFormatter().date(from: generated) ?? headDate
        lastError = held ?? Self.pendingProblem
        saveState()
        rebuildLibrary()
        SharedStore.folders = (library.shows(.series) + library.shows(.summit))
            .map { SharedStore.Folder(name: $0.name, kind: $0.kind.rawValue) }
        lastAdded = before.isEmpty ? 0 : Set(library.allEpisodes.map(\.id)).subtracting(before).count
        lastUpdated = 0
        deferred = 0
        return true
    }

    // MARK: 同步时不误删、不丢手机上做的

    /// 上一次同步时「电脑上没有、手机上要删」的那批：这次还是同一批才真删（见 removeGone）
    private var heldRemovals: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "vault.heldRemovals") ?? []) {
        didSet { UserDefaults.standard.set(Array(heldRemovals).sorted(), forKey: "vault.heldRemovals") }
    }
    /// 一次最多直接删几个；再多就先留着，下次同步还是要删同一批才删
    static let removeAtOnce = 200

    /// 电脑（或仓库）上已经没有的文件，手机上也删掉。两道保险：
    /// 1. 手机上新做的、改过还没同步出去的（发件箱里还有它）不删；
    /// 2. 一次要删很多（超过 removeAtOnce 个，或者手机上文件的五分之一）多半是电脑那边笔记库没找全、
    ///    硬盘没挂上：这次先不删，下次同步还是这一批才删。返回给人看的提醒
    @discardableResult
    private func removeGone(_ newTree: [String: TreeItem], source: String) -> String? {
        let unsent = unsentPaths()
        let gone = Set(local.keys.filter { newTree[$0] == nil && !unsent.contains($0) })
        guard !gone.isEmpty else { heldRemovals = []; return nil }
        if gone.count > Self.removeAtOnce || gone.count * 5 > max(local.count, 1), gone != heldRemovals {
            heldRemovals = gone
            return "\(source)上少了 \(gone.count) 个文件，像是\(source)上的笔记库没找全。这次先没删手机上的；下次同步还是少这些才删。"
        }
        heldRemovals = []
        for path in gone {
            try? FileManager.default.removeItem(at: fileURL(path))
            local[path] = nil
        }
        return nil
    }

    /// 发件箱里还没同步到电脑的文件：手机上新存的、改过的
    private func unsentPaths() -> Set<String> {
        var out = generatedPending.union(pending.keys)   // 还没确认电脑收下的修改
        for item in Outbox.items() where item.isSync {
            if let p = item.task["path"] as? String { out.insert(p) }
        }
        return out
    }

    /// 手机上写好、电脑还没记进处理记录的条目：{文件夹: {条目 id: 记录}}。
    /// 电脑同步过来的 .manifest.json 里还没有这些条目时，再给手机上这份补回去，书架上一直看得到；
    /// 电脑那边记上（处理成功）了就不再管，以电脑那份为准，不会出两条
    private static var pendingRowsURL: URL { URL.applicationSupportDirectory.appending(path: "pending-rows.json") }
    private var pendingRows: [String: [String: Any]] {
        get {
            guard let data = try? Data(contentsOf: Self.pendingRowsURL),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] else { return [:] }
            return obj
        }
        set {
            if newValue.isEmpty { try? FileManager.default.removeItem(at: Self.pendingRowsURL); return }
            if let data = try? JSONSerialization.data(withJSONObject: newValue, options: [.withoutEscapingSlashes]) {
                try? data.write(to: Self.pendingRowsURL, options: .atomic)
            }
        }
    }

    private func rememberRow(folder: String, id: String, row: [String: Any], series: Bool) {
        var all = pendingRows
        var rows = all[folder] as? [String: Any] ?? [:]
        rows[id] = ["row": row, "series": series]
        all[folder] = rows
        pendingRows = all
    }

    /// 同步之后：电脑那份处理记录里还没有的手机条目补回手机上这份
    func mergePendingRows() {
        var all = pendingRows
        guard !all.isEmpty else { return }
        for (folder, value) in all {
            guard var rows = value as? [String: Any] else { all[folder] = nil; continue }
            let path = "\(folder)/.manifest.json"
            var m: [String: Any] = [:]
            if let data = try? Data(contentsOf: fileURL(path)), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { m = obj }
            var entries = m["entries"] as? [String: [String: Any]] ?? [:]
            var changed = false
            for (id, v) in rows {
                guard let v = v as? [String: Any], var row = v["row"] as? [String: Any] else { rows[id] = nil; continue }
                if let have = entries[id], have["ok"] as? Bool == true {
                    rows[id] = nil         // 电脑上记上了：以电脑那份为准
                    continue
                }
                // 手机上这一期的文件都没了（用户删了、清掉了）：不再补
                let files = ["relative_path", "note_relative_path", "speech_relative_path"].compactMap { row[$0] as? String }
                if !files.isEmpty, !files.contains(where: { FileManager.default.fileExists(atPath: fileURL("\(folder)/\($0)").path) }) {
                    rows[id] = nil
                    continue
                }
                row["rank"] = (entries.values.compactMap { $0["rank"] as? Int }.max() ?? 0) + 1
                entries[id] = row
                if v["series"] as? Bool == true, m["content_type"] == nil { m["content_type"] = "series" }
                changed = true
            }
            all[folder] = rows.isEmpty ? nil : rows
            if changed {
                m["entries"] = entries
                if let data = try? JSONSerialization.data(withJSONObject: m, options: [.withoutEscapingSlashes]) {
                    try? write(data, to: path)
                    if local[path] == nil { local[path] = "local" }
                }
            }
        }
        pendingRows = all
    }

    /// 发件箱里的任务在电脑上跑到哪了（上一次数据线同步带回来的）
    func cableTaskResults() -> [String: [String: Any]] {
        guard let data = try? Data(contentsOf: Self.cableResultsURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj["tasks"] as? [String: [String: Any]] ?? [:]
    }

    // MARK: 交任务

    /// 任务怎么交给电脑的
    enum Route {
        case github
        case outbox(String)      // 发件箱里的任务 id，电脑同步时带回的结果按它对上
        /// 连了 GitHub 却放进了发件箱（太长、或者一时交不上）
        var fellBack: Bool { if case .outbox = self { true } else { false } }
    }

    /// 交一个任务给电脑：连了 GitHub 就开 Issue；只用数据线、任务太长、或者 GitHub 一时交不上（没网、令牌过期），
    /// 就放进发件箱，下次接数据线同步时交——手机上发起的事，不会因为一时连不上就丢了
    @discardableResult
    func submitTask(_ spec: (title: String, task: [String: Any])) async throws -> Route {
        try await submitTaskWithID(spec).route
    }

    /// 同上，再返回任务的 id：发件箱的 id，或者 gh:<Issue 号>（待确认的修改拿它查电脑做完没有）
    func submitTaskWithID(_ spec: (title: String, task: [String: Any])) async throws -> (route: Route, id: String) {
        if let github, SparkTask.fitsIssue(spec.task) {
            do {
                let number = try await SparkTask.submit(spec, via: github, from: "手机客户端")
                return (.github, "gh:\(number)")
            } catch {}
        }
        let id = try SparkTask.enqueue(spec)
        return (.outbox(id), id)
    }

    /// 交出去之后给人看的一句话
    func routeNote(_ route: Route) -> String {
        switch route {
        case .github: "已交给电脑。"
        case .outbox: github == nil ? "已放进发件箱，下次接数据线同步时交给电脑。"
                                    : "GitHub 这会儿交不上，已放进发件箱，下次接数据线同步时交给电脑。"
        }
    }

    // MARK: 手机上生成的复盘和报告

    /// 手机上生成、还没出现在电脑同步过来的文件树里的：同步时别当成「电脑上删了」删掉
    private(set) var generatedPending: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "vault.generatedPending") ?? []) {
        didSet { UserDefaults.standard.set(Array(generatedPending).sorted(), forKey: "vault.generatedPending") }
    }

    func exists(_ path: String) -> Bool {
        tree[path] != nil || local[path] != nil || FileManager.default.fileExists(atPath: fileURL(path).path)
    }

    /// 写进手机上的笔记库，书架里马上能看到；电脑那边由任务（type: save）存进笔记库，之后同步回来就是同一份
    func addGenerated(path: String, text: String) throws {
        try addGenerated(path: path, data: Data(text.utf8))
    }

    /// 剪藏的图片也走这里；一次存好几个文件时 rebuild 传 false，最后一个再重建书架
    func addGenerated(path: String, data: Data, rebuild: Bool = true) throws {
        try write(data, to: path)
        local[path] = "local"
        tree[path] = TreeItem(path: path, sha: "local", size: data.count)
        generatedPending.insert(path)
        if rebuild {
            saveState()
            rebuildLibrary()
        }
    }

    /// 手机上写好的一条信息跟进或一期节目：存文件，记进手机上这份处理记录（书架靠它列条目）。
    /// 电脑收到任务后记进自己的处理记录，同步回来就是同一份。rebuild 传 false 时攒着，最后再重建书架
    func addProcessed(folder: String, files: [(name: String, text: String)], id: String, row: [String: Any], series: Bool, rebuild: Bool) throws {
        for f in files { try addGenerated(path: "\(folder)/\(f.name)", data: Data(f.text.utf8), rebuild: false) }
        let path = "\(folder)/.manifest.json"
        var m: [String: Any] = [:]
        if let data = try? Data(contentsOf: fileURL(path)), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] { m = obj }
        var rows = m["entries"] as? [String: [String: Any]] ?? [:]
        var row = row
        row["rank"] = (rows.values.compactMap { $0["rank"] as? Int }.max() ?? 0) + 1
        rows[id] = row
        m["entries"] = rows
        if series, m["content_type"] == nil { m["content_type"] = "series" }
        try write(try JSONSerialization.data(withJSONObject: m, options: [.withoutEscapingSlashes]), to: path)
        if local[path] == nil { local[path] = "local" }
        // 电脑同步过来的处理记录会盖掉手机上这份：记着这一条，电脑记上之前每次同步完都补回去
        rememberRow(folder: folder, id: id, row: row, series: series)
        if tree[path] == nil { generatedPending.insert(path) }
        if rebuild {
            saveState()
            rebuildLibrary()
        }
    }

    func saveFeedItems() {
        saveState()
        rebuildLibrary()
    }

    /// 笔记库里的一个文件（剪藏的图片）在手机上的位置；没下载过的先下载
    func fileURL(forDownloading path: String) async -> URL? {
        let url = fileURL(path)
        if FileManager.default.fileExists(atPath: url.path) { return url }
        guard let item = tree[path], let github, let data = try? await github.blob(item.sha) else { return nil }
        try? write(data, to: path)
        local[path] = item.sha
        return url
    }

    // MARK: 手机上删掉的复盘

    /// 手机上删了、电脑那边还没移进废纸篓的：同步回来的文件树里先不算它，免得又下下来
    private(set) var deletedPending: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "vault.deletedPending") ?? []) {
        didSet { UserDefaults.standard.set(Array(deletedPending).sorted(), forKey: "vault.deletedPending") }
    }

    /// 手机上能删的：复盘和剪藏
    static func canDelete(_ path: String) -> Bool {
        if Clips.isClip(path) { return true }
        return path.hasPrefix(Library.reviewsDir) && path.hasSuffix(".md") && !path.dropFirst(Library.reviewsDir.count).contains("/")
    }

    /// 删掉一篇复盘或剪藏：手机上马上删；再交个任务给电脑，电脑把笔记库里那份移进废纸篓
    func delete(_ doc: Doc) async throws {
        guard Self.canDelete(doc.path) else { return }
        let path = doc.path
        try? FileManager.default.removeItem(at: fileURL(path))
        local[path] = nil
        tree[path] = nil
        generatedPending.remove(path)
        deletedPending.insert(path)
        // 还没确认的修改跟着作废：发件箱里这篇的修改拿掉，不然同步时又把它写回来
        setPending(path, nil)
        for item in Outbox.items() where item.task["type"] as? String == "save" && item.task["path"] as? String == path {
            Outbox.remove(item.id)
        }
        saveState()
        rebuildLibrary()
        let isClip = Clips.isClip(path)
        if isClip {
            try? addGenerated(path: Clips.indexPath, text: Clips.renderIndex(ClipProcessor.shared.clipMetas(self)))
        }
        // edited_at：电脑按手机上做的先后排修改和删除（core/cable_sync、remote_tasks）
        try await submitTask((title: "删掉\(isClip ? "剪藏" : "复盘")：\(doc.title)",
                              task: ["type": "delete", "path": path, "edited_at": Int64(Date().timeIntervalSince1970 * 1000)]))
    }

    private func dropDeleted(_ newTree: inout [String: TreeItem]) {
        for p in deletedPending {
            if newTree[p] == nil { deletedPending.remove(p) } else { newTree[p] = nil }
        }
    }

    private func keepGenerated(_ newTree: inout [String: TreeItem]) {
        dropDeleted(&newTree)
        for p in generatedPending {
            if newTree[p] != nil || !FileManager.default.fileExists(atPath: fileURL(p).path) {
                generatedPending.remove(p)      // 电脑上已经有了（或者手机上的已经没了）
            } else {
                newTree[p] = TreeItem(path: p, sha: local[p] ?? "local", size: 0)
            }
        }
    }

    // MARK: 电脑上改过名的剪藏（tools/migrate_times.py 按发表日期改名）

    /// Spark/剪藏/.renames.json 里的 renames（旧路径 → 新路径），a→b→c 顺着找到最后的名字。早先的版本整个文件就是 {旧: 新}
    nonisolated static func clipRenames() -> [String: String] {
        guard let data = try? Data(contentsOf: root.appending(path: "\(Clips.folder)/.renames.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        let raw = obj["renames"] as? [String: Any] ?? (obj["rewritten"] == nil ? obj : [:])
        let table = raw.compactMapValues { $0 as? String }
        var out: [String: String] = [:]
        for old in table.keys {
            var cur = old, seen: Set<String> = [old]
            while let next = table[cur], !seen.contains(next) { cur = next; seen.insert(next) }
            if cur != old { out[old] = cur }
        }
        return out
    }

    /// 已经换过的改名记录（「旧\t新」），每条只换一次
    private var appliedRenames: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "vault.renamesApplied") ?? []) {
        didSet { UserDefaults.standard.set(Array(appliedRenames).sorted(), forKey: "vault.renamesApplied") }
    }

    /// 同步之后：手机上按路径记的东西跟着换成新名字——发件箱里还没交的存、删，手机上删了还没到电脑的。
    /// 手机上生成、还没到电脑的旧路径不再留着（电脑上已经是新名字那篇，留着书架上就出两份）。computer：电脑那边有的文件
    private func applyClipRenames(_ newTree: inout [String: TreeItem], computer: Set<String>) {
        let all = Self.clipRenames()
        guard !all.isEmpty else { return }
        // 还没确认的修改、发件箱里的存和删：每次都对一遍（改名记录早就见过，之后才放进来的旧名字也换掉）
        let moved = PendingEdits.retarget(pending, renames: all)
        if moved != pending { pending = moved }
        Self.retargetOutbox(all)
        let map = all.filter { !appliedRenames.contains("\($0.key)\t\($0.value)") }
        guard !map.isEmpty else { return }
        for (old, new) in map {
            if generatedPending.contains(old) {
                generatedPending.remove(old)
                if !computer.contains(old) { newTree[old] = nil }
            }
            if deletedPending.contains(old) {
                deletedPending.remove(old)
                deletedPending.insert(new)
                newTree[new] = nil
            }
        }
        appliedRenames.formUnion(map.map { "\($0.key)\t\($0.value)" })
    }

    // MARK: 手机上改过、还没确认电脑收下的（规则见 PendingEdits）

    nonisolated private static var pendingURL: URL { URL.applicationSupportDirectory.appending(path: "pending-edits.json") }

    /// 读待确认记录时出的问题（坏了的文件挪到了一边），同步状态里显示
    nonisolated(unsafe) static var pendingProblem: String?

    nonisolated private static func loadPending() -> [String: PendingEdits.Record] {
        guard let data = try? Data(contentsOf: pendingURL) else { return [:] }
        if let obj = try? JSONDecoder().decode([String: PendingEdits.Record].self, from: data) { return obj }
        // 读不出来：不拿空表盖掉，挪到一边（里面是手机上还没确认的修改）
        let bad = pendingURL.deletingLastPathComponent()
            .appending(path: "pending-edits.json.bad-\(Int(Date().timeIntervalSince1970))")
        try? FileManager.default.moveItem(at: pendingURL, to: bad)
        pendingProblem = "手机上待确认的修改记录读不出来，已经挪到 \(bad.lastPathComponent)，里面的修改没有丢，但这次不再自动保护。"
        return [:]
    }

    /// 每篇一条，存在 App 自己的 Application Support 里（不在电脑会整个盖掉的笔记库文件夹里），整个文件原子写
    private(set) var pending: [String: PendingEdits.Record] = Vault.loadPending() {
        didSet {
            try? FileManager.default.createDirectory(at: URL.applicationSupportDirectory, withIntermediateDirectories: true)
            if let data = try? JSONEncoder().encode(pending) { try? data.write(to: Self.pendingURL, options: .atomic) }
        }
    }

    func setPending(_ path: String, _ record: PendingEdits.Record?) { pending[path] = record }

    /// 阅读页上的一行提醒：手机上这一版没进电脑。按电脑说的原因写
    func pendingNotice(_ path: String) -> String? {
        guard let r = pending[path] else { return nil }
        switch (r.state, r.failKind) {
        case ("conflict", "conflict"?):
            return "这篇在电脑上也改过，手机上这一版没有合并进去，电脑上另存了一份手机这一版（在 note-conflicts 文件夹里）。现在显示的是手机上这一版。"
        case ("conflict", "gone"?), ("gone", _):
            return "电脑上已经没有这篇了（删了或改了名），手机上这一版没存进去。现在显示的是手机上这一版；放弃的话这一版就没了，先拷贝一份。"
        case ("conflict", _):
            return "电脑收不了手机上这一版（比如文章太长、内容不合规），电脑上没有存这一版。现在显示的是手机上这一版；放弃的话这一版就没了，先拷贝一份。"
        default:
            return nil
        }
    }

    /// 不要手机上这一版了：看电脑上的（只在用户点了才做）
    func dismissPending(_ path: String) {
        setPending(path, nil)
        rebuildLibrary()
    }

    /// 改电脑上已有的一篇：写本地文件，书架马上看到；不算「手机上新建的」（保护靠待确认记录，见 unsentPaths）
    func writeEdited(path: String, text: String) throws {
        try write(Data(text.utf8), to: path)
        local[path] = "local"
        if tree[path] == nil { tree[path] = TreeItem(path: path, sha: "local", size: text.utf8.count) }
        saveState()
        rebuildLibrary()
    }

    /// 待确认的修改交给电脑的任务（saveEdited、再交一次都用）
    func pendingTask(_ path: String, _ r: PendingEdits.Record, images: [String: String] = [:]) -> (title: String, task: [String: Any]) {
        var task: [String: Any] = ["type": "save", "path": path, "content": r.content, "replace": true, "edited_at": r.editedAt]
        if Clips.isClip(path) { task["images"] = images }
        if !path.hasPrefix(Library.reviewsDir), !r.bases.isEmpty { task["base"] = r.bases[0]; task["bases"] = r.bases }
        return (title: "更新笔记库：\((path as NSString).lastPathComponent)", task: task)
    }

    /// 正在再交的（同步时常常对账，别重复交）
    private var resubmitting: Set<String> = []

    /// 根本没交出去的修改（交任务时出错）：bases、editedAt 不变，再交一次
    private func resubmit(_ path: String) {
        guard let r = pending[path], r.taskIDs.isEmpty, !resubmitting.contains(path) else { return }
        resubmitting.insert(path)
        Task {
            defer { resubmitting.remove(path) }
            guard let (_, id) = try? await submitTaskWithID(pendingTask(path, r)) else { return }
            if pending[path]?.editedAt == r.editedAt { setPending(path, PendingEdits.submitted(r, taskID: id)) }
        }
    }

    /// 走 GitHub Issue 交的修改：电脑写回的状态（做完了没有、写进去那一版的 applied_sha、失败的原因）
    private func issueStatuses() async -> [String: PendingEdits.Last] {
        guard let github else { return [:] }
        var out: [String: PendingEdits.Last] = [:]
        // 只查要用的：每条的最后那个任务，加上 absorb 要的更早任务里还不知道结果的；已经有结果（做完、失败）的不再查
        var wanted = Set<String>()
        for r in pending.values {
            for id in r.taskIDs where id.hasPrefix("gh:") {
                let known = lastIssueStatuses[id].map { $0.state == "done" || $0.state == "failed" } ?? false
                if !known { wanted.insert(id) }
            }
        }
        for id in wanted {
            guard let n = Int(id.dropFirst(3)), let comments = try? await github.comments(n),
                  let s = PendingEdits.issueStatus(comments.map(\.body)) else { continue }
            out[id] = s
        }
        return out
    }

    private var lastIssueStatuses: [String: PendingEdits.Last] = [:]

    /// 同步之后对每条待确认的修改（PendingEdits.reconcile，只按内容判断）：电脑已经有这次的改动就放手；
    /// 电脑没收（冲突、没有了、收不了）就标出来，阅读页上说清楚，不再写回；还在等就保证本地文件是手机这一版。
    /// 发件箱里电脑已经做完的顺手清掉。held：这次同步先没删电脑上少了的那批。返回有没有变
    @discardableResult
    private func reconcilePending(_ newTree: inout [String: TreeItem], issues: [String: PendingEdits.Last]? = nil, held: Bool = false) -> Bool {
        if let issues { lastIssueStatuses.merge(issues) { _, new in new } }
        let results = cableTaskResults()
        let outbox = Outbox.items()
        for item in outbox where results[item.id]?["state"] as? String == "done" { Outbox.remove(item.id) }
        let inOutbox = Set(outbox.filter { results[$0.id]?["state"] as? String != "done" }.map(\.id))
        guard !pending.isEmpty else { return false }
        func status(_ id: String) -> PendingEdits.Last {
            if let r = results[id], let state = r["state"] as? String {
                return PendingEdits.Last(state: state == "running" ? "waiting" : state, appliedSHA: r["applied_sha"] as? String,
                                         kind: r["kind"] as? String)
            }
            if let s = lastIssueStatuses[id] { return s }
            // 没结果：还在发件箱里、或者是 Issue（没查到状态）就当还在等
            return PendingEdits.Last(state: id.hasPrefix("gh:") || inOutbox.contains(id) ? "waiting" : nil)
        }
        var records = pending
        var changed = false
        for (path, r0) in pending {
            let earlier = r0.taskIDs.dropLast().compactMap { status($0).state == "done" ? status($0).appliedSHA : nil }
            let r = PendingEdits.absorb(r0, earlierApplied: Array(earlier))
            records[path] = r
            let treeSHA = newTree[path].flatMap { $0.sha == "local" ? nil : $0.sha }
            let outcome = PendingEdits.reconcile(r, deletedLocally: deletedPending.contains(path), treeSHA: treeSHA,
                                                 last: r.taskIDs.last.map(status), held: held, disk: diskText(path))
            switch outcome {
            case .remove, .applied:
                records[path] = nil
                changed = true
                continue
            case .conflict(let kind):
                records[path]?.state = "conflict"
                records[path]?.failKind = kind
            case .gone:
                records[path]?.state = "gone"
            case .resubmit:
                records[path]?.state = "pending"
                resubmit(path)
            case .pending(let rewrite):
                records[path]?.state = "pending"
                records[path]?.failKind = nil
                if rewrite, (try? write(Data(r.content.utf8), to: path)) != nil { changed = true }
                local[path] = "local"
            }
            // 书架上还看得到这篇（内容取记录里的，见 localText）
            if newTree[path] == nil { newTree[path] = TreeItem(path: path, sha: "local", size: r.content.utf8.count) }
            if local[path] == nil { local[path] = "local" }
        }
        if records != pending { pending = records; changed = true }
        return changed
    }

    /// 发件箱里还按旧名字的存、删换成新名字（电脑那边也认旧名字，这里换是为了手机上别把旧路径当成「还没同步出去」留着）
    private static func retargetOutbox(_ map: [String: String]) {
        guard let dir = Outbox.dir, let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        for url in files where url.pathExtension == "json" {
            guard let data = try? Data(contentsOf: url),
                  var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  var task = obj["task"] as? [String: Any], ["save", "delete"].contains(task["type"] as? String ?? ""),
                  let path = task["path"] as? String, let new = map[path] else { continue }
            task["path"] = new
            obj["task"] = task
            if let out = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]) {
                try? out.write(to: url, options: .atomic)
            }
        }
    }

    func wipe() {
        try? FileManager.default.removeItem(at: Self.root)
        tree = [:]
        local = [:]
        generatedPending = []
        lastSync = nil
        headSHA = ""
        library = .empty
        github = nil
    }

    // MARK: 书架

    func rebuildLibrary() {
        library = Library.build(tree: tree.isEmpty ? Dictionary(uniqueKeysWithValues: local.keys.map {
            ($0, TreeItem(path: $0, sha: local[$0]!, size: 0)) }) : tree, read: { self.localText($0) })
    }
}
