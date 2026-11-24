import Foundation

/// 崩溃记录。真机上出过两次「section 里的行数不对」的崩溃（UICollectionView，由 SwiftUI List 更新触发），
/// 没定位到是哪个列表。这里留三样东西，下次再崩能对上：
/// 1. 内存里最近 60 次主要列表的形状（列表名、几组、每组几行、有没有重复 id），形状没变不重复记；
/// 2. 未捕获的异常：异常名、原因、调用栈、App 版本、时间连同上面的形状，同步写成一份 JSON；
/// 3. 列表里出现重复 id（就算没崩）：写一份记录，同一个列表同一组 id 只写一次。
/// 都写在 App 自己容器的 Library/Application Support/Diagnostics/ 下，每种最多留 5 份。不加界面、不上传。
/// 只记 id（路径、编号）和数字，不记正文。
/// 纯逻辑（环形记录、查重、留几份）在 CrashLog.Ring、duplicates、prune，tests/test_crash_log.py 用 swiftc 单独编出来测
enum CrashLog {
    /// 一次列表形状
    struct Shape: Equatable {
        let list: String
        let rows: [Int]            // 每组几行
        let duplicates: [String]   // 重复的 id（没有就是空）
        var at = Date()

        static func == (a: Shape, b: Shape) -> Bool { a.list == b.list && a.rows == b.rows && a.duplicates == b.duplicates }

        var json: [String: Any] {
            ["list": list, "sections": rows.count, "rows": rows, "duplicates": duplicates, "at": CrashLog.stamp(at)]
        }
    }

    /// 最多留 capacity 条的环形记录；同一个列表形状跟上次一样就不记
    struct Ring {
        let capacity: Int
        private(set) var items: [Shape] = []
        private var start = 0
        private var last: [String: Shape] = [:]

        init(capacity: Int = 60) { self.capacity = max(1, capacity) }

        /// 记一条；形状没变返回 false
        @discardableResult
        mutating func add(_ s: Shape) -> Bool {
            if last[s.list] == s { return false }
            last[s.list] = s
            if items.count < capacity { items.append(s) } else { items[start] = s; start = (start + 1) % capacity }
            return true
        }

        /// 从旧到新
        var ordered: [Shape] { Array(items[start...] + items[..<start]) }
    }

    /// 一串 id 里重复的那些（按第一次重复出现的先后，每个只列一次）。O(n)
    static func duplicates(_ ids: [String]) -> [String] {
        var seen = Set<String>(), dup = Set<String>(), out: [String] = []
        seen.reserveCapacity(ids.count)
        for id in ids where !seen.insert(id).inserted {
            if dup.insert(id).inserted { out.append(id) }
        }
        return out
    }

    /// 同一种记录文件超过 keep 份时要删掉的（文件名带时间，按名字排就是按时间排，删最旧的）
    static func prune(_ names: [String], prefix: String, keep: Int) -> [String] {
        let mine = names.filter { $0.hasPrefix(prefix) && $0.hasSuffix(".json") }.sorted()
        return Array(mine.prefix(max(0, mine.count - keep)))
    }

    /// 文件名和记录里用的时间：UTC，按名字排就是按时间排
    static func stamp(_ d: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyyMMdd'T'HHmmss.SSS'Z'"
        return f.string(from: d)
    }

    static let keepFiles = 5
    static let crashPrefix = "crash-"
    static let duplicatePrefix = "duplicate-"

    // MARK: 进程里的那一份

    nonisolated(unsafe) private static var ring = Ring()
    nonisolated(unsafe) private static var reported = Set<String>()
    nonisolated(unsafe) private static var previous: (@convention(c) (NSException) -> Void)?
    private static let lock = NSLock()

    /// 记一次列表形状。sections：每组的行 id；keys：组自己的 id（ForEach 分组用的，可以不给）。
    /// 在列表算出要显示的数据之后调，开销 O(行数)，不读磁盘；发现重复 id 时才在后台写一份记录
    static func list(_ name: String, _ sections: [[String]], keys: [String] = []) {
        var dups: [String] = []
        for s in sections { dups += duplicates(s) }
        dups += duplicates(keys).map { "组:\($0)" }
        let shape = Shape(list: name, rows: sections.map(\.count), duplicates: dups)
        let isNew = lock.withLock { ring.add(shape) }
        guard isNew, !dups.isEmpty else { return }
        let key = name + "\u{1}" + dups.joined(separator: "\u{1}")
        guard lock.withLock({ reported.insert(key).inserted }) else { return }
        let payload: [String: Any] = ["kind": "duplicate-ids", "version": appVersion, "shape": shape.json]
        DispatchQueue.global(qos: .utility).async { write(payload, prefix: duplicatePrefix) }
    }

    /// 只有一组的列表
    static func list(_ name: String, _ ids: [String]) { list(name, [ids]) }

    /// App 启动时装一次：未捕获的异常先写记录，再交给原来的处理函数
    static func install() {
        lock.withLock {
            guard previous == nil else { return }
            previous = NSGetUncaughtExceptionHandler()
            NSSetUncaughtExceptionHandler { e in
                CrashLog.crashed(e)
                CrashLog.previousHandler()?(e)
            }
        }
    }

    private static func previousHandler() -> (@convention(c) (NSException) -> Void)? { previous }

    /// 崩溃时同步写完再返回（进程马上就没了）
    private static func crashed(_ e: NSException) {
        let shapes = lock.withLock { ring.ordered }
        let payload: [String: Any] = [
            "kind": "uncaught-exception",
            "name": e.name.rawValue,
            "reason": e.reason ?? "",
            "stack": e.callStackSymbols,
            "version": appVersion,
            "at": stamp(Date()),
            "lists": shapes.map(\.json),
        ]
        write(payload, prefix: crashPrefix)
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "\(info["CFBundleShortVersionString"] as? String ?? "?") (\(info["CFBundleVersion"] as? String ?? "?"))"
    }

    /// 测试时换到临时目录（tests/test_crash_log.py）；App 里只有样例库模式设（SparkMain，换到样例库专用的位置）
    nonisolated(unsafe) static var folderOverride: URL?

    static var folder: URL {
        if let folderOverride { return folderOverride }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appending(path: "Diagnostics", directoryHint: .isDirectory)
    }

    /// 写一份 JSON，同一种只留最新的 keepFiles 份（手机 App 自己容器里的诊断文件）
    static func write(_ payload: [String: Any], prefix: String, in dir: URL = folder, now: Date = Date()) {
        let fm = FileManager.default
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) else { return }
        let url = dir.appending(path: "\(prefix)\(stamp(now))-\(UUID().uuidString.prefix(4)).json")
        try? data.write(to: url)
        let names = (try? fm.contentsOfDirectory(atPath: dir.path)) ?? []
        for name in prune(names, prefix: prefix, keep: keepFiles) { try? fm.removeItem(at: dir.appending(path: name)) }
    }
}
