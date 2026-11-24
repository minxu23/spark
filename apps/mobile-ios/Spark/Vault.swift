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
        Fixture.support(URL.applicationSupportDirectory).appending(path: "vault", directoryHint: .isDirectory)
    }
    private static var stateURL: URL { root.appending(path: ".spark-state.json") }

    init() {
        loadState()
        purgeTrash()
        refreshLegacyGone()
        rebuildLibrary()
        if let p = Self.trashProblem ?? Self.pendingProblem { lastError = p }
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
        if name == ".manifest.json" || path == "\(Clips.folder)/.renames.json" || path == "Spark/.tags.json" { return true }   // 改名记录、标签索引同步完就要用
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
        defer { syncing = false; Task { await runImagePurge() } }   // 图片等同步完再判断
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
            reconcilePending(&newTree, issues: await issueStatuses(), held: held != nil, cableTree: false)
            tree = newTree
            mergePendingRows()
            headSHA = head.sha
            headDate = head.date
            lastSync = .now
            lastSource = "GitHub"
            purgeTrash()
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
        defer { syncing = false; Task { await runImagePurge() } }
        do {
            let copied = try await DriveSync.run(into: Self.root) { n, total in
                Task { @MainActor [weak self] in self?.progress = (n, total) }
            }
            let before = Set(library.allEpisodes.map(\.id))
            importCable()
            lastSync = .now
            lastSource = "Google Drive"
            purgeTrash()
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
    /// 清单是新的一批才给 (generated_at, files)：读不到、不是 JSON、generated_at 跟上次收的一样（电脑推完文件、
    /// 清单还没到时手机上还是上一份清单）都是 nil，这时 importCable 不动 tree、local，不调 removeGone
    /// deleted：电脑带来的墓碑（{路径: [删掉的各版内容的 SHA-256]}），旧版电脑没有这一项，是空的
    nonisolated static func newCableManifest(_ data: Data?, lastCable: String?)
        -> (generated: String, files: [String: [Any]], deleted: [String: [String]])? {
        guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let generated = obj["generated_at"] as? String, generated != lastCable,
              let files = obj["files"] as? [String: [Any]] else { return nil }
        let deleted = (obj["deleted"] as? [String: Any] ?? [:]).compactMapValues { $0 as? [String] }
        return (generated, files, deleted)
    }

    @discardableResult
    func importCable() -> Bool {
        guard let (generated, files, deleted) = Self.newCableManifest(try? Data(contentsOf: Self.cableManifestURL), lastCable: lastCable) else {
            // 笔记库没重新拷，可能只推了任务结果（电脑看一眼就存下了手机的修改）：发件箱里做完的清掉，失败的标出来
            var t = tree
            // 只来了任务结果、清单没变：不放手（cableTree false），等新清单。放手以后到新清单之前要是做了一次 GitHub 同步，
            // GitHub 文件树里还没有这篇，removeGone 会把本机这篇新文档暂时删掉
            if reconcilePending(&t, cableTree: false) { tree = t; saveState(); rebuildLibrary() }
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
        settleElsewhereDeleted(newTree, deleted: deleted)   // 手机上新建、在别的设备上删掉的：移进手机废纸篓
        keepGenerated(&newTree)
        // 电脑上已经没有的文件（删了、移进了废纸篓）：手机上也删掉
        let held = removeGone(newTree, source: "电脑")
        pruneEmptyFolders()
        reconcilePending(&newTree, held: held != nil, cableTree: true)   // 还没确认的修改：收下了就放手，电脑拷进来的文件盖了就写回
        tree = newTree
        mergePendingRows()
        lastCable = generated
        lastSync = .now
        lastSource = "数据线"
        purgeTrash()
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

    /// 上次看到的清单、任务结果文件的修改时刻（只决定要不要重读，读了以后照内容判断）
    @ObservationIgnored private var cableStamp: [Date?] = []

    /// 旧版 App 交的「改已有的一篇」被电脑判了没有（gone），手机上又没有待确认记录的几篇（PendingEdits.legacyGone）：
    /// 列表上标「没进电脑」，阅读页上能「重新交给电脑」。每次对账前重算
    private(set) var legacyGonePaths: Set<String> = []

    private func refreshLegacyGone(results raw: [String: [String: Any]]? = nil) {
        let results = (raw ?? cableTaskResults()).mapValues {
            PendingEdits.Last(state: $0["state"] as? String, appliedSHA: $0["applied_sha"] as? String, kind: $0["kind"] as? String)
        }
        let outbox = Outbox.items().compactMap { item -> (id: String, path: String)? in
            guard item.task["type"] as? String == "save", let p = item.task["path"] as? String else { return nil }
            return (item.id, p)
        }
        let onPhone = Set(outbox.map(\.path).filter { local[$0] != nil && FileManager.default.fileExists(atPath: fileURL($0).path) })
        let found = PendingEdits.legacyGone(outbox: outbox, results: results, recorded: Set(pending.keys), onPhone: onPhone)
        if found != legacyGonePaths { legacyGonePaths = found }
    }

    /// App 开着的时候隔一会儿看一眼：电脑用数据线拷进来新的一批（清单变了）、或者推了任务结果，就收进来。
    /// 以前只在回到前台、点同步、启动时收：App 一直开着，电脑那边删掉的文件在列表里一直还在（打开也还读得到旧文件）。
    /// 电脑把清单放在最后单独推（core/cable_sync.sync_device），看到新清单时文件已经拷完
    @discardableResult
    func importCableIfChanged() -> Bool {
        guard !syncing else { return false }
        let stamp = [Self.cableManifestURL, Self.cableResultsURL].map {
            (try? $0.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
        }
        guard stamp != cableStamp else { return false }
        cableStamp = stamp
        return importCable()
    }

    /// 只在手机上有、电脑没有、也不在交的、电脑墓碑里又没有手机这一版的（ElsewhereDeleted 的 stranded）：
    /// 列表上标「没进电脑」，阅读页上说清楚（pendingNotice）。每收一份新清单重算
    private(set) var strandedPaths: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "vault.strandedPaths") ?? []) {
        didSet { UserDefaults.standard.set(Array(strandedPaths).sorted(), forKey: "vault.strandedPaths") }
    }

    /// 数据线清单带来的墓碑（见 ElsewhereDeleted）：手机上新建、在别的设备上删掉的同一份移进手机废纸篓（标「在别的设备上删掉的」，
    /// 不发删除任务）；手机上有电脑没见过的版本的留着、标出来。有待确认记录、发件箱里有它的任务、手机上删了还没交的一律不动
    private func settleElsewhereDeleted(_ newTree: [String: TreeItem], deleted: [String: [String]]) {
        let candidates = generatedPending.filter { newTree[$0] == nil && Self.canDelete($0) && FileManager.default.fileExists(atPath: fileURL($0).path) }
        var busy = Set(pending.keys).union(deletedPending)
        for item in Outbox.items() { if let p = item.task["path"] as? String { busy.insert(p) } }
        let onlyPhone = Dictionary(uniqueKeysWithValues: candidates.map { ($0, diskText($0).map(PendingEdits.sha256)) })
        let d = ElsewhereDeleted.decide(onlyPhone: onlyPhone, deleted: deleted, busy: busy)
        strandedPaths = Set(d.stranded)
        for path in d.trash {
            guard let content = diskText(path) else { continue }
            let title = (library.notes + library.clips + library.reports).first { $0.path == path }?.title
                ?? ((path as NSString).lastPathComponent as NSString).deletingPathExtension
            let base = PendingEdits.sha256(content)
            if !ElsewhereDeleted.alreadyTrashed(trash.map { ($0.path, $0.base, $0.elsewhere) }, path: path, base: base) {
                let stamp = PhoneTrash.deletionStamp(deviceNow: Date(), offset: NetClock.shared.offset())
                var item = PhoneTrash.Item(id: UUID().uuidString, path: path, title: title, kind: Self.kindLabel(path),
                                           deletedAt: stamp.at, content: content)
                item.clockUnverified = stamp.unverified
                item.base = base   // 电脑见过的那一版：放回时当 base
                item.elsewhere = true
                let next = PhoneTrash.adding(trash, item)
                guard (try? Self.writeTrash(next)) != nil else { continue }   // 放不进废纸篓就不动
                trash = next
            }
            try? FileManager.default.removeItem(at: fileURL(path))
            local[path] = nil
            tree[path] = nil
            generatedPending.remove(path)
        }
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
    private static var pendingRowsURL: URL { Fixture.support(URL.applicationSupportDirectory).appending(path: "pending-rows.json") }
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
    func submitTaskWithID(_ spec0: (title: String, task: [String: Any])) async throws -> (route: Route, id: String) {
        let spec = (title: spec0.title, task: noteSaveKey(spec0.task))
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

    // MARK: 手机上删掉的复盘、剪藏、随手记

    /// 手机上删了、电脑那边还没移进废纸篓的：{路径: 删除任务的 id}。同步回来的文件树里先不算它，免得又下下来。
    /// 旧版存的是路径数组（vault.deletedPending），读进来时任务 id 是空串（见 PendingEdits.deleteOutcome）
    private(set) var deletedTasks: [String: String] = Vault.loadDeleted() {
        didSet { UserDefaults.standard.set(deletedTasks, forKey: "vault.deletedTasks") }
    }

    nonisolated private static func loadDeleted() -> [String: String] {
        if let d = UserDefaults.standard.dictionary(forKey: "vault.deletedTasks") as? [String: String] { return d }
        let old = UserDefaults.standard.stringArray(forKey: "vault.deletedPending") ?? []
        return Dictionary(old.map { ($0, "") }, uniquingKeysWith: { a, _ in a })
    }

    var deletedPending: Set<String> { Set(deletedTasks.keys) }

    /// 手机上能删的：复盘、剪藏和随手记
    static func canDelete(_ path: String) -> Bool { PathRules.canDelete(path) }   // 规则在 PathRules.canDelete

    static func kindLabel(_ path: String) -> String {
        Clips.isClip(path) ? "剪藏" : Jottings.isJotting(path) ? "随手记" : "复盘"
    }

    /// 删掉一篇复盘、剪藏或随手记：先存进手机废纸篓（PhoneTrash，存不上就不删，告诉用户），再从书架拿掉；
    /// 再交个任务给电脑，电脑把笔记库里那份移进 macOS 的废纸篓。任务里带上删掉的那几版内容的指纹（shas）
    /// 和删之前交过的存的标识（keys）：电脑记成墓碑，删除先到、存后到时（比如刚剪藏还在交），晚到的那份不再建出来。
    /// 这篇还没确认的修改（待确认记录）一起存进废纸篓的这一条，放回原处时原样装回去
    func delete(_ doc: Doc) async throws {
        guard Self.canDelete(doc.path) else { return }
        let path = doc.path
        let latest = localText(path) ?? ""
        let record = pending[path]
        var shas = Set([diskText(path), record?.content].compactMap { $0 }.map(PendingEdits.sha256))
        shas.formUnion(record?.bases ?? [])
        let outboxSaves = Outbox.items().filter { $0.task["type"] as? String == "save" && $0.task["path"] as? String == path }
        for item in outboxSaves { if let c = item.task["content"] as? String { shas.insert(PendingEdits.sha256(c)) } }
        let onMac = tree[path].map { $0.sha != "local" } ?? false
        let unsent = record != nil || !outboxSaves.isEmpty || generatedPending.contains(path)
        let deleteKey = UUID().uuidString
        // 删除时刻：手头有 10 分钟内的网络时间就用它校正，没有就用手机的钟、标没核对过（之后第一次联网时核对）
        let stamp = PhoneTrash.deletionStamp(deviceNow: Date(), offset: NetClock.shared.offset())
        var trashed = PhoneTrash.Item(id: UUID().uuidString, path: path, title: doc.title, kind: Self.kindLabel(path),
                                      deletedAt: stamp.at, content: latest, unsent: unsent)
        trashed.clockUnverified = stamp.unverified
        trashed.record = record
        trashed.base = record == nil && onMac ? diskText(path).map(PendingEdits.sha256) : nil
        trashed.deleteKey = deleteKey
        if !latest.isEmpty {
            let next = PhoneTrash.adding(trash, trashed)
            do { try Self.writeTrash(next) } catch {
                throw SparkError("没能放进手机的废纸篓（\(error.localizedDescription)），这篇没删。")
            }
            trash = next
        }
        try? FileManager.default.removeItem(at: fileURL(path))
        local[path] = nil
        tree[path] = nil
        generatedPending.remove(path)
        deletedTasks[path] = ""
        // 还没确认的修改跟着作废：发件箱里这篇的修改拿掉，不然同步时又把它写回来（内容、待确认记录在手机废纸篓里）
        setPending(path, nil)
        for item in outboxSaves { Outbox.remove(item.id) }
        saveState()
        rebuildLibrary()
        if Clips.isClip(path) {
            try? addGenerated(path: Clips.indexPath, text: Clips.renderIndex(ClipProcessor.shared.clipMetas(self)))
        }
        // edited_at：电脑按手机上做的先后排修改和删除（core/cable_sync、remote_tasks）
        let (_, id) = try await submitTaskWithID((title: "删掉\(Self.kindLabel(path))：\(doc.title)",
                                                  task: ["type": "delete", "path": path, "shas": shas.sorted(), "key": deleteKey,
                                                         "keys": saveKeys[path] ?? [],
                                                         "edited_at": Int64(Date().timeIntervalSince1970 * 1000)]))
        if deletedTasks[path] != nil { deletedTasks[path] = id }
        trashed.deleteTaskID = id
        if let i = trash.firstIndex(where: { $0.id == trashed.id }) { trash[i] = trashed; persistTrash() }
    }

    /// 交给电脑的存（save）各带一个标识 key，按路径记着最近的几个：删这篇时一起交给电脑（墓碑），
    /// 这几个存晚到也不建回来；删了以后新做的一篇（新的 key）照常建
    private(set) var saveKeys: [String: [String]] = UserDefaults.standard.dictionary(forKey: "vault.saveKeys") as? [String: [String]] ?? [:] {
        didSet { UserDefaults.standard.set(saveKeys, forKey: "vault.saveKeys") }
    }

    func noteSaveKey(_ task: [String: Any]) -> [String: Any] {
        guard task["type"] as? String == "save", let path = task["path"] as? String, Self.canDelete(path) else { return task }
        var t = task
        let key = t["key"] as? String ?? UUID().uuidString
        t["key"] = key
        saveKeys[path] = Array(((saveKeys[path] ?? []) + [key]).suffix(16))
        return t
    }

    /// 一个任务在电脑上怎么样了：数据线带回的结果、查到的 Issue 状态；都没有时还在发件箱里、或者是 Issue 就当在等
    private func taskStatus(_ id: String) -> PendingEdits.Last {
        if let r = cableTaskResults()[id], let state = r["state"] as? String {
            return PendingEdits.Last(state: state == "running" ? "waiting" : state, appliedSHA: r["applied_sha"] as? String,
                                     kind: r["kind"] as? String)
        }
        if let s = lastIssueStatuses[id] { return s }
        return PendingEdits.Last(state: id.hasPrefix("gh:") || Outbox.items().contains { $0.id == id } ? "waiting" : nil)
    }

    /// 同步回来的文件树：手机上删了的先藏着，直到电脑删完、树里也没有了才放掉（PendingEdits.deleteOutcome）。
    /// 只凭「树里没有」放掉会出事：刚剪藏、电脑还没建出来的一篇删掉，下一次同步树里没有它，记号就清了，
    /// 电脑随后建出文件，它又回到手机。电脑没删成的不再藏着（任务页里标着没成功）
    private func dropDeleted(_ newTree: inout [String: TreeItem]) {
        for (p, id) in deletedTasks {
            switch PendingEdits.deleteOutcome(taskID: id, last: id.isEmpty ? nil : taskStatus(id), inTree: newTree[p] != nil) {
            case .keep: newTree[p] = nil
            case .clear: deletedTasks[p] = nil
            case .failed:
                deletedTasks[p] = nil
                // 文章回到书架；废纸篓里那一条标成「没删成，文章还在」，放回时不再写一份
                if let i = trash.firstIndex(where: { $0.path == p && $0.deleteTaskID == id }) {
                    trash[i].deleteFailed = true
                    persistTrash()
                }
            }
        }
    }

    // MARK: 手机上的废纸篓（PhoneTrash）

    nonisolated private static var trashURL: URL { Fixture.support(URL.applicationSupportDirectory).appending(path: "phone-trash.json") }

    /// 读废纸篓时出的问题（坏了的文件挪到了一边），设置页的废纸篓、同步状态里显示
    nonisolated(unsafe) static var trashProblem: String?

    private(set) var trash: [PhoneTrash.Item] = Vault.loadTrash()

    nonisolated private static func writeTrash(_ items: [PhoneTrash.Item]) throws {
        try FileManager.default.createDirectory(at: Fixture.support(URL.applicationSupportDirectory), withIntermediateDirectories: true)
        try JSONEncoder().encode(items).write(to: trashURL, options: .atomic)
    }

    private func persistTrash() {
        do { try Self.writeTrash(trash) } catch { lastError = "手机废纸篓存不上：\(error.localizedDescription)" }
    }

    /// 读不出来：不拿空表盖掉，挪到一边（里面是删掉的文章），同 pending-edits.json
    nonisolated private static func loadTrash() -> [PhoneTrash.Item] {
        guard let data = try? Data(contentsOf: trashURL) else { return [] }
        if let items = try? JSONDecoder().decode([PhoneTrash.Item].self, from: data) { return items }
        let bad = trashURL.deletingLastPathComponent().appending(path: "phone-trash.json.bad-\(Int(Date().timeIntervalSince1970))")
        try? FileManager.default.moveItem(at: trashURL, to: bad)
        trashProblem = "手机废纸篓的记录读不出来，已经挪到 \(bad.lastPathComponent)，里面的文章没有丢，但废纸篓里暂时看不到它们。"
        return []
    }

    /// 上一次要自动清除时没取到网络时间（没联网）：废纸篓页顶上说一句
    private(set) var trashOffline = false

    /// 删掉满 720 小时的彻底清掉（App 启动、每次同步后），顺手再查一遍还没清的图片。
    /// 先取一次网络时间（NetClock，10 分钟内取过就用缓存）：取不到不清，等下次联网；
    /// 没核对过删除时刻的条目先跟网络时间核对一遍（PhoneTrash.reconciled）
    func purgeTrash() {
        Task {
            let net = await NetClock.shared.now()
            trashOffline = net == nil
            if let net {
                let device = Date()
                let fixed = trash.map { PhoneTrash.reconciled($0, deviceNow: device, networkNow: net) }
                if fixed != trash { trash = fixed; persistTrash() }
                let old = trash.filter { PhoneTrash.purgeable($0, deviceNow: device, networkNow: net) }
                if !old.isEmpty { erase(old) }
            }
            await runImagePurge()
        }
    }

    /// 彻底删除一条；id 为 nil 是清空废纸篓
    func eraseTrash(_ id: String?) {
        erase(id.map { i in trash.filter { $0.id == i } } ?? trash)
        Task { await runImagePurge() }
    }

    /// 彻底删除的几条：从废纸篓去掉；它们引用的图片记进待清单（存着，重启不丢），由 runImagePurge 判断能不能删；
    /// 再交个任务给电脑，电脑自己查一遍引用，把没人用的图移进 macOS 的废纸篓（core/remote_tasks.purge_images）
    private func erase(_ items: [PhoneTrash.Item]) {
        let ids = Set(items.map(\.id))
        trash = trash.filter { !ids.contains($0.id) }
        persistTrash()
        // 没删成、文章还在的：图片不动；在别的设备上删掉的：手机上的图照常判断，电脑上的图由删掉它的那台设备释放
        for item in items where !item.deleteFailed {
            let images = Array(Set(Markdown.parse(item.content).compactMap { b -> String? in
                if case .image(_, let src) = b { return PhoneTrash.imagePath(src: src, docPath: item.path) }
                return nil
            })).sorted()
            guard !images.isEmpty else { continue }
            imagePurgeQueue = Array(Set(imagePurgeQueue + images)).sorted()
            guard ElsewhereDeleted.sendsPurge(deleteFailed: item.deleteFailed, elsewhere: item.elsewhere) else { continue }
            Task {
                _ = try? await submitTask((title: "彻底删除\(item.kind)的图片：\(item.title)",
                                           task: ["type": "purge_images", "path": item.path, "images": images,
                                                  "edited_at": Int64(Date().timeIntervalSince1970 * 1000)] as [String: Any]))
            }
        }
    }

    /// 等着判断能不能删的图片（手机本地的）
    private(set) var imagePurgeQueue: [String] = UserDefaults.standard.stringArray(forKey: "trash.purgeImages") ?? [] {
        didSet { UserDefaults.standard.set(imagePurgeQueue, forKey: "trash.purgeImages") }
    }

    /// 待清的图片里，确实没人用的删掉（PhoneTrash.orphans）：正在同步、或者有哪篇文档读不出来、没下载完，这一轮都不做，等下次
    func runImagePurge() async {
        guard !imagePurgeQueue.isEmpty, !syncing else { return }
        let queue = imagePurgeQueue, root = Self.root
        let docs = tree.keys.filter { $0.hasSuffix(".md") || $0.hasSuffix(".canvas") }.map { ($0, isDownloaded($0)) }
        var extra = trash.map(\.content) + pending.values.map(\.content)
        extra += Outbox.items().filter { $0.task["type"] as? String == "save" }.compactMap { $0.task["content"] as? String }
        // 不是 UTF-8 的按字节找；没下载、读不了的：在 Spark/、output/ 下的这一轮不删，别处的跳过（同电脑）
        let scan = await Task.detached(priority: .utility) { () -> (texts: [String], raw: [Data], unreadable: Bool) in
            var texts: [String] = [], raw: [Data] = [], unreadable = false
            for (path, downloaded) in docs {
                let inVault = path.hasPrefix(Library.sparkDir) || path.hasPrefix(Library.reportsDir)
                guard downloaded, let data = try? Data(contentsOf: root.appending(path: path, directoryHint: .notDirectory)) else {
                    if inVault { unreadable = true; break }
                    continue
                }
                // 带 BOM 的 UTF-16 文档按 UTF-16 读（同电脑）
                let utf16 = data.starts(with: [0xFF, 0xFE]) || data.starts(with: [0xFE, 0xFF])
                if let t = String(data: data, encoding: utf16 ? .utf16 : .utf8) { texts.append(t) } else { raw.append(data) }
            }
            return (texts, raw, unreadable)
        }.value
        guard let gone = PhoneTrash.orphans(candidates: queue, texts: scan.texts + extra, raw: scan.raw, unreadable: scan.unreadable,
                                            syncing: syncing) else { return }
        for p in gone {
            try? FileManager.default.removeItem(at: fileURL(p))
            local[p] = nil
        }
        // 还有人用的不删，也不再等（以后那篇也删了，会再记进来）
        imagePurgeQueue = imagePurgeQueue.filter { !queue.contains($0) }
        if !gone.isEmpty { saveState() }
    }

    /// 放回原处。原路径被占了就换个名字放回，返回实际放回的路径。
    /// 一律交一个带 restore 的存，带上删的时候的待确认记录（bases、new，电脑照常判同一版还是冲突，冲突时手机上照常提醒），
    /// 和要取消的删除（cancels：删除晚到也不执行；已经执行了，restore 的存把文件建回来）。删除还在发件箱里的顺手撤掉。
    /// 电脑没删成（文章还在）的：只把这一条拿掉，不写文件
    @discardableResult
    func restore(_ item: PhoneTrash.Item) async throws -> String {
        // 电脑没删成、文章还在：删的时候没有没交的修改，只把这一条拿掉；有的话照常装回去交给电脑（放回原处，不换名字）
        if item.deleteFailed && !PhoneTrash.hasUnsent(item) {
            trash = PhoneTrash.removing(trash, id: item.id)
            persistTrash()
            return item.path
        }
        // 电脑没删成的：书架上这篇后来又改过，就换个名字另存，不盖掉书架上那一版（PhoneTrash.restoreTarget）
        let target = PhoneTrash.restoreTarget(item, shelfText: item.deleteFailed ? localText(item.path) : nil,
                                              hasPending: item.deleteFailed && pending[item.path] != nil) { p in
            (p != item.path || self.deletedTasks[p] == nil) && self.exists(p)
        }.path
        if !item.deleteTaskID.isEmpty, !item.deleteTaskID.hasPrefix("gh:"), cableTaskResults()[item.deleteTaskID] == nil {
            Outbox.remove(item.deleteTaskID)
        }
        deletedTasks[item.path] = nil
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        let rec = PhoneTrash.restoredRecord(item, target: target, now: now)
        if rec.bases.isEmpty { try addGenerated(path: target, text: item.content) } else { try writeEdited(path: target, text: item.content) }
        setPending(target, rec)
        trash = PhoneTrash.removing(trash, id: item.id)
        persistTrash()
        if Clips.isClip(target) {
            try? addGenerated(path: Clips.indexPath, text: Clips.renderIndex(ClipProcessor.shared.clipMetas(self)))
        }
        var spec = pendingTask(target, rec)
        spec.title = "放回原处：\(item.title)"
        spec.task["restore"] = true
        if !item.deleteKey.isEmpty { spec.task["cancels"] = [item.deleteKey] }
        let (_, id) = try await submitTaskWithID(spec)
        if pending[target]?.editedAt == rec.editedAt { setPending(target, PendingEdits.submitted(rec, taskID: id)) }
        return target
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
            if let id = deletedTasks[old] {
                deletedTasks[old] = nil
                deletedTasks[new] = id
                newTree[new] = nil
            }
        }
        appliedRenames.formUnion(map.map { "\($0.key)\t\($0.value)" })
    }

    // MARK: 手机上改过、还没确认电脑收下的（规则见 PendingEdits）

    nonisolated private static var pendingURL: URL { Fixture.support(URL.applicationSupportDirectory).appending(path: "pending-edits.json") }

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
            try? FileManager.default.createDirectory(at: Fixture.support(URL.applicationSupportDirectory), withIntermediateDirectories: true)
            if let data = try? JSONEncoder().encode(pending) { try? data.write(to: Self.pendingURL, options: .atomic) }
        }
    }

    func setPending(_ path: String, _ record: PendingEdits.Record?) { pending[path] = record }

    /// 阅读页上的一行提醒：手机上这一版没进电脑。按电脑说的原因写
    func pendingNotice(_ path: String) -> String? {
        guard let r = pending[path] else {
            if strandedPaths.contains(path) {
                return "这篇在别的设备上删掉了，电脑上已经没有；手机上这一版电脑没见过，没交过去。要留着就复制一份，或者删掉。"
            }
            if legacyGonePaths.contains(path) {
                return "这篇是手机上新存的，旧版 App 交给电脑时把它当成了「改电脑上已有的一篇」，电脑上没有，就没存进去。现在只在手机上有。"
            }
            return nil
        }
        // 失败的是之前的一次修改，后来的修改已经交了：说清楚那一份去哪了
        if r.state == "conflict", r.failedEarlier {
            return ["conflict", "taken"].contains(r.failKind ?? "")
                ? "手机上之前的一次修改没被电脑收下（电脑上那时已经是别的版本），那一份另存在电脑上的 note-conflicts 文件夹里，需要的话从那里拷回来。后来的修改已经交给电脑。"
                : "手机上之前的一次修改没被电脑收下，电脑上没有另存那一份。后来的修改已经交给电脑。"
        }
        switch (r.state, r.failKind) {
        case ("conflict", "conflict"?), ("conflict", "taken"?):
            return "这篇在电脑上也改过，手机上这一版没有合并进去，电脑上另存了一份手机这一版（在 note-conflicts 文件夹里）。现在显示的是手机上这一版。"
        case ("conflict", "gone"?), ("gone", _):
            return "电脑上已经没有这篇了（删了或改了名），手机上这一版没存进去。现在显示的是手机上这一版；放弃的话这一版就没了，先拷贝一份。"
        case ("conflict", _):
            return "电脑收不了手机上这一版（比如文章太长、内容不合规），电脑上没有存这一版。现在显示的是手机上这一版；放弃的话这一版就没了，先拷贝一份。"
        default:
            return nil
        }
    }

    /// 冲突的提醒在阅读页上显示出来了：记下，用户再改时才不再带上报过失败的任务（PendingEdits.edited）
    func notePendingShown(_ path: String) {
        guard var r = pending[path], r.state == "conflict", !r.noticed else { return }
        r.noticed = true
        setPending(path, r)
    }

    /// 这篇为什么只在手机上（PendingEdits.Stuck）
    func stuck(_ path: String) -> PendingEdits.Stuck {
        PendingEdits.stuck(record: pending[path], stranded: strandedPaths.contains(path), legacyGone: legacyGonePaths.contains(path))
    }

    /// 阅读页上给不给「重新交给电脑」
    func canResend(_ path: String) -> Bool {
        PendingEdits.canResend(stuck(path), newDocPath: Clips.isClip(path) || Jottings.isJotting(path))
    }

    /// 重新交给电脑（只在用户点了才做，不自动重交：有的「电脑上没有」是用户在别的设备上故意删的）。
    /// 手机上这一版当成新的一篇交：不带 bases、带 new 和 restore（PendingEdits.resent）。电脑上这个路径有墓碑就清掉照建；
    /// 已经有内容不同的一篇不覆盖，判冲突，手机这一版存进电脑的 note-conflicts，手机上照常提醒。
    /// 交不出去就抛错：待确认记录、发件箱都不动，手机上这一版还在
    @discardableResult
    func resendToMac(_ path: String) async throws -> Route {
        let kind = stuck(path)
        guard PendingEdits.canResend(kind, newDocPath: Clips.isClip(path) || Jottings.isJotting(path)) else {
            throw SparkError("这篇现在不用重新交给电脑")
        }
        guard let content = localText(path) else { throw SparkError("手机上读不到这一版，没交") }
        let previous = pending[path]
        // 旧版交的那个失败的存（legacyGone）：原图地址接过来；交成以后从发件箱拿掉（电脑不会再跑它，留着这篇就一直算「没交出去」）
        let results = cableTaskResults()
        let failedSaves = Outbox.items().filter {
            $0.task["type"] as? String == "save" && $0.task["path"] as? String == path && results[$0.id]?["state"] as? String == "failed"
        }
        var images: [String: String] = [:]
        for item in failedSaves { for (k, v) in item.task["images"] as? [String: String] ?? [:] where images[k] == nil { images[k] = v } }
        let rec = PendingEdits.resent(content: content, previous: previous, images: Clips.isClip(path) ? images : [:],
                                      now: Int64(Date().timeIntervalSince1970 * 1000))
        var spec = pendingTask(path, rec)
        spec.title = "重新交给电脑：\((path as NSString).lastPathComponent)"
        let (route, id) = try await submitTaskWithID(spec)
        // 交出去了：换上新记录（之前报过失败的任务不再带），本地文件是这一版、按手机上新建的保护着
        if (try? addGenerated(path: path, text: content)) == nil { local[path] = "local" }
        setPending(path, PendingEdits.submitted(rec, taskID: id))
        for item in failedSaves { Outbox.remove(item.id) }
        strandedPaths.remove(path)
        legacyGonePaths.remove(path)
        saveState()
        rebuildLibrary()
        return route
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
        if Clips.isClip(path) { task["images"] = images.merging(r.images) { mine, _ in mine } }   // 记录里存着的原图地址也带上
        task.merge(PendingEdits.taskFields(r, review: path.hasPrefix(Library.reviewsDir))) { _, new in new }
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
        for r in pending.values.map(\.taskIDs) + [Array(deletedTasks.values)] {
            for id in r where id.hasPrefix("gh:") {
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
    /// cableTree：newTree 是刚收到的一份新的数据线（或同样流程的 Google Drive）清单。GitHub 文件树、
    /// 只来了任务结果而清单没变时都是 false（见 ElsewhereDeleted.released）
    private func reconcilePending(_ newTree: inout [String: TreeItem], issues: [String: PendingEdits.Last]? = nil, held: Bool = false,
                                  cableTree: Bool) -> Bool {
        if let issues { lastIssueStatuses.merge(issues) { _, new in new } }
        let results = cableTaskResults()
        let outbox = Outbox.items()
        for item in outbox where results[item.id]?["state"] as? String == "done" { Outbox.remove(item.id) }
        let inOutbox = Set(outbox.filter { results[$0.id]?["state"] as? String != "done" }.map(\.id))
        refreshLegacyGone(results: results)
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
        var renamed: [String] = []
        for (path, r0) in pending {
            let earlier = r0.taskIDs.dropLast().compactMap { status($0).state == "done" ? status($0).appliedSHA : nil }
            let treeSHA = newTree[path].flatMap { $0.sha == "local" ? nil : $0.sha }
            let applied = r0.taskIDs.compactMap { status($0).state == "done" ? status($0).appliedSHA : nil }
            let r = PendingEdits.settled(PendingEdits.absorb(r0, earlierApplied: Array(earlier)), treeSHA: treeSHA, applied: applied)
            records[path] = r
            let outcome = PendingEdits.reconcile(r, deletedLocally: deletedPending.contains(path), treeSHA: treeSHA,
                                                 last: r.taskIDs.last.map(status), held: held, disk: diskText(path),
                                                 earlier: r.taskIDs.dropLast().map(status))
            switch outcome {
            case .remove, .applied:
                records[path] = nil
                changed = true
                // 手机上新建的这篇电脑已经收下（任务 done），手机上这份就是交上去的那份（或电脑写进去的那版）：
                // 不再靠 generatedPending 保护，之后电脑文件树里没有它就按「电脑上删了」处理（ElsewhereDeleted）。
                // 手机上之后又改过、没交的不放手
                if outcome == .applied, generatedPending.contains(path),
                   ElsewhereDeleted.released(disk: diskText(path), submitted: r.content, appliedBlob: applied.last,
                                             blobOf: PendingEdits.blobSHA, treeHas: treeSHA != nil, cableTree: cableTree,
                                             cableTask: r.taskIDs.last.map { results[$0] != nil } ?? false) {
                    generatedPending.remove(path)
                    let sha = applied.last ?? PendingEdits.blobSHA(r.content)
                    local[path] = sha
                    if newTree[path]?.sha == "local" { newTree[path] = TreeItem(path: path, sha: sha, size: newTree[path]?.size ?? 0) }
                }
                continue
            case .conflict(let kind):
                records[path] = PendingEdits.markConflict(r, kind: kind,
                                                          statuses: Dictionary(r.taskIDs.map { ($0, status($0)) }, uniquingKeysWith: { a, _ in a }))
                // 手机上新写的随手记，电脑上那个名字已经是另一篇（另一台设备同一天、同一个开头写的，电脑回 kind=taken）：
                // 换个名字重交，不算冲突。同一篇两边都改过的是 kind=conflict，照常提醒
                if kind == "taken", Jottings.isJotting(path) || Clips.isClip(path) { renamed.append(path) }
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
        for old in renamed where moveNewFile(old, &newTree) { changed = true }
        return changed
    }

    /// 新写的随手记挪到下一个空着的名字（_2、_3……）重交：本地文件、待确认记录跟着挪；
    /// 原来那个名字是电脑上别的一条，本地这份删掉，下次同步下电脑那份。电脑那边冲突时存的 note-conflicts 副本不用管
    /// 剪藏同样（另一台设备同一天剪了同一篇）。图片的原地址没带上：电脑上缺图时在阅读页「重新下载」
    private func moveNewFile(_ old: String, _ newTree: inout [String: TreeItem]) -> Bool {
        guard let r = pending[old] else { return false }
        // 已经带着 _2 这类后缀的先去掉，不起 _2_2
        let stem = (old as NSString).deletingPathExtension.replacingOccurrences(of: #"_\d+$"#, with: "", options: .regularExpression)
        guard let new = (2..<100).lazy.map({ "\(stem)_\($0).md" })
                .first(where: { !self.exists($0) && newTree[$0] == nil && !self.deletedPending.contains($0) && self.pending[$0] == nil }),
              (try? write(Data(r.content.utf8), to: new)) != nil else { return false }
        try? FileManager.default.removeItem(at: fileURL(old))
        local[old] = nil
        if newTree[old]?.sha == "local" { newTree[old] = nil }
        generatedPending.remove(old)
        local[new] = "local"
        newTree[new] = TreeItem(path: new, sha: "local", size: r.content.utf8.count)
        // 新名字下就是手机上新建的一条：不带 bases，还没交（taskIDs 空），resubmit 交出去
        var moved = PendingEdits.Record(content: r.content, bases: [], editedAt: r.editedAt, taskIDs: [], created: true)
        moved.images = r.images   // 图片不跟着文章改名，原地址照带，电脑按它下图
        setPending(new, moved)
        setPending(old, nil)
        if Clips.isClip(new) { try? addGenerated(path: Clips.indexPath, text: Clips.renderIndex(ClipProcessor.shared.clipMetas(self))) }
        resubmit(new)
        return true
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

/// 网络时间：手机废纸篓自动清除只认它（PhoneTrash.purgeable）。iOS 没有公开接口知道「自动设置时间」开没开，所以不去判断，
/// 每次要自动清除前取一次：对 https://www.apple.com（失败换 https://www.cloudflare.com）发一个 HEAD 请求，读响应头的 Date。
/// 不带任何用户数据、Cookie、App 或用户标识（ephemeral 会话，User-Agent 换成通用的），超时 5 秒，
/// 只认 HTTPS、证书校验通过的响应（系统默认校验，跳到 http 的不认）。取到的值配合单调时钟（改手机时间不影响它）推算，
/// 10 分钟内不重复请求；只在内存里，App 重启后作废
@MainActor
final class NetClock {
    static let shared = NetClock()
    private var sample: (network: Date, mono: UInt64)?
    private static let sources = ["https://www.apple.com", "https://www.cloudflare.com"]
    private static let fresh: UInt64 = 600 * 1_000_000_000

    private static func mono() -> UInt64 { clock_gettime_nsec_np(CLOCK_MONOTONIC) }

    /// 10 分钟内取到过：按单调时钟推算的现在；没有是 nil
    func cached() -> Date? {
        guard let s = sample else { return nil }
        let elapsed = Self.mono() &- s.mono
        guard elapsed <= Self.fresh else { return nil }
        return s.network.addingTimeInterval(Double(elapsed) / 1e9)
    }

    /// 网络时间减手机时间（10 分钟内取过才有）
    func offset() -> TimeInterval? { cached().map { $0.timeIntervalSince(Date()) } }

    /// 现在的网络时间：10 分钟内取过用缓存，不然取一次；取不到是 nil
    func now() async -> Date? {
        if let c = cached() { return c }
        if NetGate.blocked { return nil }   // 样例库模式不联网：当作取不到，手机废纸篓不自动清
        let cfg = URLSessionConfiguration.ephemeral
        cfg.httpCookieAcceptPolicy = .never
        cfg.httpShouldSetCookies = false
        cfg.urlCache = nil
        cfg.timeoutIntervalForRequest = 5
        cfg.timeoutIntervalForResource = 5
        let session = URLSession(configuration: cfg)
        defer { session.finishTasksAndInvalidate() }
        for source in Self.sources {
            guard let url = URL(string: source) else { continue }
            var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 5)
            req.httpMethod = "HEAD"
            req.setValue("Mozilla/5.0", forHTTPHeaderField: "User-Agent")
            guard let (_, resp) = try? await session.data(for: req), let http = resp as? HTTPURLResponse,
                  http.url?.scheme == "https", let raw = http.value(forHTTPHeaderField: "Date"),
                  let date = PhoneTrash.httpDate(raw) else { continue }
            sample = (date, Self.mono())
            return date
        }
        return nil
    }
}
