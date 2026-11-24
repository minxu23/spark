import Foundation

/// 交给电脑的一个任务：拼成 Issue 的标题和正文（正文里一段 ```spark-task JSON，格式见电脑端 core/remote_tasks.py）。
/// 主 App 的「添加任务」和分享扩展共用。
enum SparkTask {
    /// 处理链接放到哪：新建节目、当作会议，或 Spark/ 下已有的文件夹
    enum Dest: Hashable {
        case newSeries
        case summit
        case show(String)

        var payload: [String: Any] {
            switch self {
            case .newSeries: ["kind": "new_series"]
            case .summit: ["kind": "summit"]
            case .show(let folder): ["kind": "show", "folder": folder]
            }
        }

        var label: String {
            switch self {
            case .newSeries: "新建一个节目文件夹"
            case .summit: "作为一场会议处理"
            case .show(let folder): folder
            }
        }

        /// 记住上次选的（分享扩展里默认选它）
        var key: String {
            switch self {
            case .newSeries: "new_series"
            case .summit: "summit"
            case .show(let folder): "show:" + folder
            }
        }

        init?(key: String) {
            if key == "new_series" { self = .newSeries }
            else if key == "summit" { self = .summit }
            else if key.hasPrefix("show:") { self = .show(String(key.dropFirst(5))) }
            else { return nil }
        }
    }

    static func link(_ url: String, dest: Dest, length: String) -> (title: String, task: [String: Any]) {
        ("处理链接：\(url)", ["type": "link", "url": url, "dest": dest.payload, "summary_length": length])
    }

    static func check(_ subs: [String], maxNew: Int) -> (title: String, task: [String: Any]) {
        ("检查订阅：\(subs.joined(separator: "、"))", ["type": "check", "subscriptions": subs, "max_new": maxNew])
    }

    /// 复盘（电脑端 core/review.py）：reading 阅读复盘 / topic 主题来龙去脉 / claims 观点检验
    static func review(_ kind: String, days: Int, topic: String, months: Int) -> (title: String, task: [String: Any]) {
        var t: [String: Any] = ["type": "review", "kind": kind, "days": days, "months": months]
        if !topic.isEmpty { t["topic"] = topic }
        let title: String
        switch kind {
        case "reading": title = "阅读复盘：近 \(days) 天"
        case "clips": title = "剪藏复盘：近 \(days) 天"
        case "topic": title = "主题来龙去脉：\(topic)"
        default: title = topic.isEmpty ? "观点检验：\(months) 个月以前的判断" : "观点检验：\(topic)"
        }
        return (title, t)
    }

    /// 剪藏综合小结：挑出来的几篇放在一起写（kind: picked，paths 是 Spark/剪藏/ 下的文件）
    static func picked(_ paths: [String], title: String) -> (title: String, task: [String: Any]) {
        (title, ["type": "review", "kind": "picked", "paths": paths])
    }

    static func report(_ topic: String, scope: String) -> (title: String, task: [String: Any]) {
        var t: [String: Any] = ["type": "report", "topic": topic]
        if !scope.isEmpty { t["scope"] = scope }
        return ("出报告：\(topic)", t)
    }

    /// 只用数据线同步（不连 GitHub）、或 GitHub 交不上时：任务放进发件箱，下次接数据线同步时电脑拉走去跑（core/cable_sync.py）。
    /// 返回发件箱里的任务 id，电脑带回的结果按它对上
    @discardableResult
    static func enqueue(_ spec: (title: String, task: [String: Any])) throws -> String {
        try Outbox.add(title: spec.title, task: spec.task)
    }

    /// Issue 正文的长度上限是 65536 字，留点余量：任务 JSON 超过这么长就只能走数据线
    static let issueLimit = 60_000

    static func fitsIssue(_ task: [String: Any]) -> Bool {
        ((try? JSONSerialization.data(withJSONObject: task).count) ?? .max) <= issueLimit
    }

    /// 返回 Issue 号（手机上待确认的修改拿它查电脑做完没有）
    @discardableResult
    static func submit(_ spec: (title: String, task: [String: Any]), via api: GitHub, from: String) async throws -> Int {
        let json = try JSONSerialization.data(withJSONObject: spec.task, options: [.sortedKeys, .withoutEscapingSlashes])
        let body = "从 Spark \(from)添加。电脑上的 Spark 接手后会在下面写进度。\n\n```spark-task\n\(String(decoding: json, as: UTF8.self))\n```\n"
        return try await api.createTask(title: String(spec.title.prefix(120)), body: body).number
    }

    /// 从分享过来的一段文字里找出第一个 http(s) 链接（YouTube、小红书等 App 分享的常常是「标题 + 链接 + 一句提示」）。
    /// 跟 Mac 应用「添加剪藏」用同一套（ClipLink.firstURL）：系统认链接，链接后面紧跟的中文标点、引号不算进去
    static func firstURL(in text: String) -> String? {
        ClipLink.firstURL(in: text)
    }
}

/// 主 App 和分享扩展共用的一点数据（App Group）：书架上的文件夹名（分享时选「放到哪」）、上次选的位置。
enum SharedStore {
    static let groupID = "group.com.minxu.spark"

    /// App Group 容器里给电脑用数据线拉的文件夹（outbox、readlog）：放在 Library/Spark/ 下。
    /// 真机上 devicectl 只让读 App Group 的 Library/，放在容器根目录拉不到（模拟器上没这个限制，之前没发现）。
    /// 之前放在根目录的，第一次用时挪过来。
    static func groupDir(_ name: String) -> URL? {
        guard let base = Fixture.groupContainer(FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: groupID)) else { return nil }
        let fm = FileManager.default
        let d = base.appending(path: "Library/Spark/\(name)", directoryHint: .isDirectory)
        try? fm.createDirectory(at: d, withIntermediateDirectories: true)
        let old = base.appending(path: name, directoryHint: .isDirectory)
        if let files = try? fm.contentsOfDirectory(atPath: old.path) {
            for f in files where !fm.fileExists(atPath: d.appending(path: f).path) {
                try? fm.moveItem(at: old.appending(path: f), to: d.appending(path: f))
            }
            if (try? fm.contentsOfDirectory(atPath: old.path))?.isEmpty == true { try? fm.removeItem(at: old) }
        }
        return d
    }
    static var defaults: UserDefaults { Fixture.groupUserDefaults(UserDefaults(suiteName: groupID) ?? .standard) }

    struct Folder: Codable, Hashable {
        let name: String
        let kind: String     // series / summit
    }

    static var folders: [Folder] {
        get {
            guard let data = defaults.data(forKey: "folders") else { return [] }
            return (try? JSONDecoder().decode([Folder].self, from: data)) ?? []
        }
        set { defaults.set(try? JSONEncoder().encode(newValue), forKey: "folders") }
    }

    static var lastDest: SparkTask.Dest {
        get { SparkTask.Dest(key: defaults.string(forKey: "lastDest") ?? "") ?? .newSeries }
        set { defaults.set(newValue.key, forKey: "lastDest") }
    }

    /// 只用数据线同步、不连 GitHub（分享扩展据此决定任务放发件箱）
    static var cableOnly: Bool {
        get { defaults.bool(forKey: "cableOnly") }
        set { defaults.set(newValue, forKey: "cableOnly") }
    }

    static var lastLength: String {
        get { defaults.string(forKey: "lastLength") ?? "medium" }
        set { defaults.set(newValue, forKey: "lastLength") }
    }
}

/// 发件箱：App Group 容器里的 Library/Spark/outbox/，一个任务一个 JSON 文件。主 App 和分享扩展都往里写，
/// 电脑同步时用 devicectl 拉走（appGroupDataContainer），结果写回手机那份笔记库的 .spark-cable-tasks.json。
/// 发件箱里有两种东西：
/// - 任务：要电脑来做的（处理视频链接、检查节目订阅、出报告），做完才有结果；
/// - 同步：手机上已经做好、手机上已经能看的内容（存文件、删文件、写好的一条信息跟进或一期节目），
///   只是还没到电脑上。这种不算「待提交的任务」，界面上只说「还没同步到电脑」。
/// 同步的文件名带 .sync（「<id>.sync.json」），数个数时不用把每件读出来；电脑那边照样按 .json 收
enum Outbox {
    struct Item: Identifiable {
        let id: String
        let title: String
        let task: [String: Any]
        let created: Date
        /// 手机上已经有了、只是还没同步到电脑的内容
        var isSync: Bool { Outbox.syncTypes.contains(task["type"] as? String ?? "") }
    }

    static let syncTypes: Set<String> = ["save", "delete", "purge_images", "track_item", "episode_item"]

    static var dir: URL? { SharedStore.groupDir("outbox") }

    @discardableResult
    static func add(title: String, task: [String: Any]) throws -> String {
        guard let dir else { throw SparkError("发件箱打不开（App Group 没配好）") }
        let id = UUID().uuidString.lowercased()
        let obj: [String: Any] = ["id": id, "title": title, "task": task,
                                  "created_at": ISO8601DateFormatter().string(from: .now)]
        let data = try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes])
        let sync = syncTypes.contains(task["type"] as? String ?? "")
        try data.write(to: dir.appending(path: sync ? "\(id).sync.json" : "\(id).json"), options: .atomic)
        return id
    }

    /// 还没交的：任务几件、还没同步到电脑的内容几件。只看文件名，不把每件（剪藏正文可能上百万字）读出来解一遍
    static var counts: (tasks: Int, sync: Int) {
        _ = migrated
        guard let dir, let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return (0, 0) }
        let json = files.filter { $0.pathExtension == "json" }
        let sync = json.filter { $0.lastPathComponent.hasSuffix(".sync.json") }.count
        return (json.count - sync, sync)
    }

    static var count: Int { counts.tasks + counts.sync }

    /// 旧版放进来的同步内容文件名没带 .sync：读一遍改名（每次启动只做一次）
    private static let migrated: Void = {
        guard let dir, let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else { return }
        for url in files where url.pathExtension == "json" && !url.lastPathComponent.hasSuffix(".sync.json") {
            guard let data = try? Data(contentsOf: url),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = obj["id"] as? String, url.lastPathComponent == "\(id).json",
                  syncTypes.contains((obj["task"] as? [String: Any])?["type"] as? String ?? "") else { continue }
            try? FileManager.default.moveItem(at: url, to: dir.appending(path: "\(id).sync.json"))
        }
    }()

    static func items() -> [Item] {
        guard let dir, let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        else { return [] }
        return files.filter { $0.pathExtension == "json" }.compactMap { url in
            guard let data = try? Data(contentsOf: url),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = obj["id"] as? String else { return nil }
            let created = (obj["created_at"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) } ?? .distantPast
            return Item(id: id, title: obj["title"] as? String ?? "", task: obj["task"] as? [String: Any] ?? [:], created: created)
        }.sorted { $0.created > $1.created }
    }

    static func remove(_ id: String) {
        guard let dir else { return }
        try? FileManager.default.removeItem(at: dir.appending(path: "\(id).json"))
        try? FileManager.default.removeItem(at: dir.appending(path: "\(id).sync.json"))
    }
}
