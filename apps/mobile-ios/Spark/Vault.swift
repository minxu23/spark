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
    private(set) var library = Library.empty
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

    static var root: URL {
        URL.applicationSupportDirectory.appending(path: "vault", directoryHint: .isDirectory)
    }
    private static var stateURL: URL { root.appending(path: ".spark-state.json") }

    init() {
        loadState()
        rebuildLibrary()
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
        if name == ".manifest.json" { return true }
        guard name.hasSuffix(".md") else { return false }
        let parts = path.split(separator: "/").map(String.init)
        if parts.contains("transcripts") || parts.contains("speech") {
            return scope == .all
        }
        return true
    }

    func sync() async {
        guard !syncing else { return }
        importCable()
        guard let github else { return }
        syncing = true
        lastError = nil
        defer { syncing = false }
        do {
            let head = try await github.head()
            let items = try await github.tree(commit: head.sha)
            let before = Set(library.allEpisodes.map(\.id))
            let newTree = Dictionary(uniqueKeysWithValues: items.map { ($0.path, $0) })
            // 要下的：轻量范围里的、之前下过但变了的
            var wanted = items.filter { item in
                (isEager(item.path) || local[item.path] != nil) && local[item.path] != item.sha
            }
            // 用流量时整理稿和文字记录先不下（包括下过但电脑上改过的），等连上 Wi-Fi
            let hold = holdLazy
            deferred = hold ? wanted.filter { isLazy($0.path) }.count : 0
            if hold { wanted.removeAll { isLazy($0.path) } }
            let updated = wanted.filter { local[$0.path] != nil }.count
            progress = (0, wanted.count)
            try await download(wanted)
            // 仓库里已经没有的文件：手机上也删掉
            for path in local.keys where newTree[path] == nil {
                try? FileManager.default.removeItem(at: fileURL(path))
                local[path] = nil
            }
            tree = newTree
            headSHA = head.sha
            headDate = head.date
            lastSync = .now
            lastSource = "GitHub"
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

    func localText(_ path: String) -> String? {
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
              let files = obj["files"] as? [String: [Any]] else { return false }
        let before = Set(library.allEpisodes.map(\.id))
        var newTree: [String: TreeItem] = [:]
        for (path, v) in files {
            guard let sha = v.first as? String else { continue }
            newTree[path] = TreeItem(path: path, sha: sha, size: v.count > 1 ? (v[1] as? Int ?? 0) : 0)
            if FileManager.default.fileExists(atPath: fileURL(path).path) { local[path] = sha }
        }
        // 电脑上已经没有的文件（删了、移进了废纸篓）：手机上也删掉
        for path in local.keys where newTree[path] == nil {
            try? FileManager.default.removeItem(at: fileURL(path))
            local[path] = nil
        }
        tree = newTree
        lastCable = generated
        lastSync = .now
        lastSource = "数据线"
        headDate = ISO8601DateFormatter().date(from: generated) ?? headDate
        lastError = nil
        saveState()
        rebuildLibrary()
        SharedStore.folders = (library.shows(.series) + library.shows(.summit))
            .map { SharedStore.Folder(name: $0.name, kind: $0.kind.rawValue) }
        lastAdded = before.isEmpty ? 0 : Set(library.allEpisodes.map(\.id)).subtracting(before).count
        lastUpdated = 0
        deferred = 0
        return true
    }

    /// 发件箱里的任务在电脑上跑到哪了（上一次数据线同步带回来的）
    func cableTaskResults() -> [String: [String: Any]] {
        guard let data = try? Data(contentsOf: Self.cableResultsURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj["tasks"] as? [String: [String: Any]] ?? [:]
    }

    // MARK: 交任务

    /// 交一个任务给电脑：连了 GitHub 就开 Issue，只用数据线就放进发件箱
    func submitTask(_ spec: (title: String, task: [String: Any])) async throws {
        if let github {
            try await SparkTask.submit(spec, via: github, from: "手机客户端")
        } else {
            try SparkTask.enqueue(spec)
        }
    }

    func wipe() {
        try? FileManager.default.removeItem(at: Self.root)
        tree = [:]
        local = [:]
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
