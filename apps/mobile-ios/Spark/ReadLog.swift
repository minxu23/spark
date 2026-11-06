import Foundation

/// 手机上的阅读记录：打开一期的哪种稿子、一篇报告，记一条，给电脑上的复盘用（core/readlog.py）。
/// 存在 App Group 容器的 Library/Spark/readlog/phone.jsonl（一行一条 JSON），走数据线时电脑直接拉这个文件；
/// 走 GitHub 时同步完把还没交过的几行发到仓库里带 spark-readlog 标签的 Issue 下面。
enum ReadLog {
    private static let dedupSeconds: TimeInterval = 30 * 60
    /// 打开停留这么久才算读过
    static let dwellSeconds = 20
    private static var recent: [String: Date] = [:]

    static var fileURL: URL? {
        SharedStore.groupDir("readlog")?.appending(path: "phone.jsonl")
    }

    /// 读一期：节目文件夹 + 条目 id + 哪种稿子（note / speech / transcript / article）
    static func episode(_ e: Episode, kind: String) {
        record(key: "\(e.id)#\(kind)", fields: ["show": e.showID, "entry": e.entryID, "kind": kind])
    }

    /// 读一篇没有处理记录的文档（报告、简报、复盘）
    static func doc(_ path: String) {
        record(key: path, fields: ["path": path, "kind": "doc"])
    }

    private static func record(key: String, fields: [String: String]) {
        let now = Date()
        if let last = recent[key], now.timeIntervalSince(last) < dedupSeconds { return }
        recent[key] = now
        var obj: [String: Any] = fields
        obj["id"] = UUID().uuidString.lowercased()
        obj["at"] = now.timeIntervalSince1970
        obj["source"] = "phone"
        guard let url = fileURL, let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]) else { return }
        let line = data + Data("\n".utf8)
        if let h = try? FileHandle(forWritingTo: url) {
            defer { try? h.close() }
            _ = try? h.seekToEnd()
            try? h.write(contentsOf: line)
        } else {
            try? line.write(to: url, options: .atomic)
        }
    }

    static func lines() -> [String] {
        guard let url = fileURL, let text = try? String(contentsOf: url, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
    }

    /// 走 GitHub 时：已经交过多少行
    static var uploaded: Int {
        get { SharedStore.defaults.integer(forKey: "readlog.uploaded") }
        set { SharedStore.defaults.set(newValue, forKey: "readlog.uploaded") }
    }

    /// 把还没交过的记录发到 spark-readlog Issue 下（没有这个 Issue 就开一个）。失败了下次同步再试。
    static func upload(via api: GitHub) async {
        let all = lines()
        guard all.count > uploaded else { return }
        let pending = Array(all[uploaded...].prefix(500))
        do {
            let number = try await api.readlogIssue()
            let body = "手机上的阅读记录（\(pending.count) 条），电脑上的 Spark 做复盘时会读。\n\n```spark-readlog\n"
                + pending.joined(separator: "\n") + "\n```\n"
            try await api.comment(number, body: body)
            uploaded += pending.count
        } catch {
            // 没网、令牌没有 Issues 权限：留着下次
        }
    }
}
