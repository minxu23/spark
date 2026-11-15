import Foundation

/// 用 Google Drive 文件夹同步（电脑端见 core/drive_sync.py）。
/// 电脑把笔记库写进 Google Drive 里的「Spark 同步/vault/」（跟数据线拷进手机的那份一样，带 .spark-cable.json 清单）；
/// 手机在「设置」里选一次「Spark 同步」文件夹（Google Drive App 在「文件」里提供），之后：
/// - 按清单把变了的文件拷进手机上的笔记库，再走数据线同步那套收进来（Vault.importCable）；
/// - 手机的发件箱、阅读记录写回文件夹的 outbox/、readlog/，电脑下次同步时读走。
enum DriveSync {
    private static let key = "drive.bookmark"
    private static let nameKey = "drive.folderName"
    private static let d = UserDefaults.standard

    /// 暂时冻结（2026-11-15）：设置里不显示入口，也不走这条同步；要重新打开改成 true
    static let available = false

    static var isOn: Bool { available && d.data(forKey: key) != nil }
    static var folderName: String { d.string(forKey: nameKey) ?? "Spark 同步" }

    /// 记下选的文件夹（安全书签，App 重开后还能访问）
    static func choose(_ url: URL) throws {
        let ok = url.startAccessingSecurityScopedResource()
        defer { if ok { url.stopAccessingSecurityScopedResource() } }
        let data = try url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
        d.set(data, forKey: key)
        d.set(url.lastPathComponent, forKey: nameKey)
    }

    static func turnOff() {
        d.removeObject(forKey: key)
        d.removeObject(forKey: nameKey)
    }

    private static func folder() throws -> URL {
        guard let data = d.data(forKey: key) else { throw SparkError("还没选 Google Drive 里的文件夹") }
        var stale = false
        let url = try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
        if stale, let fresh = try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
            d.set(fresh, forKey: key)
        }
        return url
    }

    /// 带回来的几个附带文件（任务结果、订阅、电脑的阅读记录、阅读设置）
    private static let extras = [".spark-cable-tasks.json", ".spark-subscriptions.json", ".spark-readlog.jsonl", ".spark-prefs.json"]

    /// 同步一次：拉变了的文件，写回发件箱和阅读记录。返回拷进来几个文件。progress：(拷完几个, 一共几个)
    static func run(into root: URL, progress: @escaping @Sendable (Int, Int) -> Void) async throws -> Int {
        let dir = try folder()
        let outbox = SharedStore.groupDir("outbox"), readlog = SharedStore.groupDir("readlog")
        return try await Task.detached(priority: .userInitiated) {
            try sync(dir: dir, root: root, outbox: outbox, readlog: readlog, progress: progress)
        }.value
    }

    private static func sync(dir: URL, root: URL, outbox: URL?, readlog: URL?, progress: (Int, Int) -> Void) throws -> Int {
        guard dir.startAccessingSecurityScopedResource() else {
            throw SparkError("打不开 Google Drive 里的「\(folderName)」，到「设置」里重新选一次")
        }
        defer { dir.stopAccessingSecurityScopedResource() }
        let fm = FileManager.default
        let remote = dir.appending(path: "vault", directoryHint: .isDirectory)
        let manifestName = ".spark-cable.json"

        // 先写回手机这边的：电脑下次同步就能收到
        if let outbox { try push(outbox, to: dir.appending(path: "outbox", directoryHint: .isDirectory), prune: true) }
        if let readlog { try push(readlog, to: dir.appending(path: "readlog", directoryHint: .isDirectory), prune: false) }

        guard let manData = try? read(remote.appending(path: manifestName)),
              let man = try? JSONSerialization.jsonObject(with: manData) as? [String: Any],
              let files = man["files"] as? [String: [Any]] else {
            throw SparkError("「\(folderName)」里还没有电脑同步过来的笔记库：电脑上的 Spark 打开 Google Drive 同步后等它传完")
        }
        // 上次拷进来的清单：sha 没变、文件也在的跳过
        var old: [String: String] = [:]
        if let data = try? Data(contentsOf: root.appending(path: manifestName)),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let f = obj["files"] as? [String: [Any]] {
            for (p, v) in f { if let s = v.first as? String { old[p] = s } }
        }
        let todo = files.compactMap { path, v -> String? in
            guard let sha = v.first as? String else { return nil }
            let exists = fm.fileExists(atPath: root.appending(path: path).path)
            return old[path] == sha && exists ? nil : path
        }.sorted()
        var done = 0
        progress(0, todo.count)
        for path in todo {
            // 清单是从共享的文件夹里读来的：带 .. 或绝对路径的不写，免得写到笔记库外面
            let parts = path.split(separator: "/", omittingEmptySubsequences: false)
            guard !path.hasPrefix("/"), !parts.contains(where: { $0 == ".." || $0 == "." || $0.isEmpty }) else { continue }
            let data = try read(remote.appending(path: path))
            let dst = root.appending(path: path)
            try fm.createDirectory(at: dst.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: dst, options: .atomic)
            done += 1
            if done % 20 == 0 || done == todo.count { progress(done, todo.count) }
        }
        for name in extras {
            if let data = try? read(remote.appending(path: name)) { try? data.write(to: root.appending(path: name), options: .atomic) }
        }
        // 清单最后写：Vault.importCable 看到新清单时文件都已经到位了
        try manData.write(to: root.appending(path: manifestName), options: .atomic)
        return done
    }

    /// 读 Google Drive 里的一个文件：还没下到手机上的，读的时候由 Google Drive App 下载
    private static func read(_ url: URL) throws -> Data {
        var err: NSError?, out: Result<Data, Error> = .failure(SparkError("读不到 \(url.lastPathComponent)"))
        NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &err) { u in
            out = Result { try Data(contentsOf: u) }
        }
        if let err { throw err }
        return try out.get()
    }

    /// 手机上的一个文件夹写到 Google Drive：内容一样的跳过；prune 时那边多出来的删掉（发件箱里交完的任务）
    private static func push(_ local: URL, to remote: URL, prune: Bool) throws {
        let fm = FileManager.default
        try? fm.createDirectory(at: remote, withIntermediateDirectories: true)
        let names = (try? fm.contentsOfDirectory(atPath: local.path))?.filter { !$0.hasPrefix(".") } ?? []
        for name in names {
            let src = local.appending(path: name), dst = remote.appending(path: name)
            guard let data = try? Data(contentsOf: src) else { continue }
            if let existing = try? Data(contentsOf: dst), existing == data { continue }
            var err: NSError?, failure: Error?
            NSFileCoordinator().coordinate(writingItemAt: dst, options: .forReplacing, error: &err) { u in
                do { try data.write(to: u, options: .atomic) } catch { failure = error }
            }
            if let e = err ?? failure { throw e }
        }
        if prune {
            for name in (try? fm.contentsOfDirectory(atPath: remote.path)) ?? [] where !names.contains(name) && !name.hasPrefix(".") {
                var err: NSError?
                NSFileCoordinator().coordinate(writingItemAt: remote.appending(path: name), options: .forDeleting, error: &err) { u in
                    try? fm.removeItem(at: u)
                }
            }
        }
    }
}
