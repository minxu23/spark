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

    static func report(_ topic: String, scope: String) -> (title: String, task: [String: Any]) {
        var t: [String: Any] = ["type": "report", "topic": topic]
        if !scope.isEmpty { t["scope"] = scope }
        return ("出报告：\(topic)", t)
    }

    static func submit(_ spec: (title: String, task: [String: Any]), via api: GitHub, from: String) async throws {
        let json = try JSONSerialization.data(withJSONObject: spec.task, options: [.sortedKeys, .withoutEscapingSlashes])
        let body = "从 Spark \(from)添加。电脑上的 Spark 接手后会在下面写进度。\n\n```spark-task\n\(String(decoding: json, as: UTF8.self))\n```\n"
        _ = try await api.createTask(title: String(spec.title.prefix(120)), body: body)
    }

    /// 从分享过来的一段文字里找出第一个 http(s) 链接（YouTube 等 App 分享的常常是「标题 + 链接」）
    static func firstURL(in text: String) -> String? {
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return nil }
        let range = NSRange(text.startIndex..., in: text)
        return detector.matches(in: text, range: range).compactMap(\.url)
            .first { ["http", "https"].contains($0.scheme?.lowercased() ?? "") }?.absoluteString
    }
}

/// 主 App 和分享扩展共用的一点数据（App Group）：书架上的文件夹名（分享时选「放到哪」）、上次选的位置。
enum SharedStore {
    static let groupID = "group.com.minxu.spark"
    static var defaults: UserDefaults { UserDefaults(suiteName: groupID) ?? .standard }

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

    static var lastLength: String {
        get { defaults.string(forKey: "lastLength") ?? "medium" }
        set { defaults.set(newValue, forKey: "lastLength") }
    }
}
