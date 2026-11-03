import Foundation

/// iPhone、iPad 之间同步阅读设置和已读状态（免费开发者账号开不了 iCloud，走跟笔记库一样的两条路）。
///
/// 已读：不另外上传，用现成的阅读记录（ReadLog）。每台设备打开一期、一篇都会记一条，
/// - 连了 GitHub：都发在 spark-readlog 那个 Issue 下，同步时把所有设备发的读回来；
/// - 数据线：电脑把汇总好的阅读记录（含电脑上读的）放在笔记库根目录的 .spark-readlog.jsonl 带过来。
/// 记录里读过的都标成已读。第一次同步时整批标成「读过」的不在记录里，不会把别的设备上的小圆点抹掉。
///
/// 阅读设置：谁后改用谁的。
/// - GitHub：改了就在同一个 Issue 下发一条 ```spark-prefs 代码块（电脑做复盘时只认 spark-readlog 代码块，不受影响）；
/// - 数据线：写进 App Group 的 readlog/prefs.json，电脑同步时拉走、留最新的一份，
///   推回来是笔记库根目录的 .spark-prefs.json（core/cable_sync.py）。
@MainActor
enum DeviceSync {
    private static let d = UserDefaults.standard
    private static let prefsBlock = #/```spark-prefs[ \t]*\n(.*?)\n```/#.dotMatchesNewlines()
    private static let readlogBlock = #/```spark-readlog[ \t]*\n(.*?)\n```/#.dotMatchesNewlines()

    /// 同步完（GitHub 或数据线）调一次
    static func run(vault: Vault, prefs: ReadPrefs, readState: ReadState) async {
        var events: [[String: Any]] = []
        var remotePrefs: [(prefs: [String: Any], at: Double)] = []

        // 数据线带来的
        if let text = try? String(contentsOf: Vault.root.appending(path: ".spark-readlog.jsonl"), encoding: .utf8) {
            events += jsonLines(text)
        }
        if let data = try? Data(contentsOf: Vault.root.appending(path: ".spark-prefs.json")),
           let p = parsePrefs(data) {
            remotePrefs.append(p)
        }

        // GitHub 上的：只拉上次之后的评论，读过的 id 攒在本地
        if let api = vault.api, let number = try? await api.readlogIssue() {
            let since = d.object(forKey: "devsync.since") as? Date
            if let comments = try? await api.allComments(number, since: since) {
                var ids = Set(d.stringArray(forKey: "devsync.githubRead") ?? [])
                for c in comments {
                    for m in c.body.matches(of: readlogBlock) {
                        ids.formUnion(jsonLines(String(m.1)).compactMap { readID($0, paths: [:]) })
                    }
                    for m in c.body.matches(of: prefsBlock) {
                        if let p = parsePrefs(Data(m.1.utf8)) { remotePrefs.append(p) }
                    }
                }
                d.set(Array(ids), forKey: "devsync.githubRead")
                if let last = comments.map(\.updated_at).max() { d.set(last, forKey: "devsync.since") }
                if let p = d.dictionary(forKey: "devsync.githubPrefs"), let at = p["at"] as? Double,
                   let body = p["prefs"] as? [String: Any] {
                    remotePrefs.append((body, at))
                }
                if let newest = remotePrefs.max(by: { $0.at < $1.at }) {
                    d.set(["prefs": newest.prefs, "at": newest.at], forKey: "devsync.githubPrefs")
                }
                readState.merge(ids)
            }
        }

        // 已读：电脑的记录里只有文件路径，换成单集的 id
        if !events.isEmpty {
            var paths: [String: String] = [:]
            for e in vault.library.allEpisodes {
                for p in [e.notePath, e.speechPath, e.transcriptPath, e.articlePath].compactMap({ $0 }) { paths[p] = e.id }
            }
            readState.merge(events.compactMap { readID($0, paths: paths) })
        }

        if let newest = remotePrefs.max(by: { $0.at < $1.at }) {
            prefs.apply(newest.prefs, at: newest.at)
        }
        await uploadPrefs(vault: vault, prefs: prefs)
    }

    /// 这台设备上改过、还没发到 GitHub 的设置：发一条
    static func uploadPrefs(vault: Vault, prefs: ReadPrefs) async {
        guard let api = vault.api, prefs.changedAt > 0,
              prefs.changedAt > d.double(forKey: "devsync.uploadedPrefsAt") else { return }
        let obj: [String: Any] = ["prefs": prefs.snapshot, "at": prefs.changedAt]
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
              let number = try? await api.readlogIssue() else { return }
        let body = "阅读设置（各设备之间同步用）\n\n```spark-prefs\n" + String(decoding: data, as: UTF8.self) + "\n```\n"
        do {
            try await api.comment(number, body: body)
            d.set(prefs.changedAt, forKey: "devsync.uploadedPrefsAt")
        } catch {
            // 下次同步再发
        }
    }

    /// 数据线那条路：设置一改就写进 App Group，电脑下次同步时拉走
    nonisolated static func writePrefs(_ prefs: ReadPrefs) {
        guard let dir = ReadLog.fileURL?.deletingLastPathComponent() else { return }
        let obj: [String: Any] = ["prefs": prefs.snapshot, "at": prefs.changedAt]
        if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) {
            try? data.write(to: dir.appending(path: "prefs.json"), options: .atomic)
        }
    }

    // MARK: 解析

    private static func jsonLines(_ text: String) -> [[String: Any]] {
        text.split(separator: "\n").compactMap {
            try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
        }
    }

    private static func parsePrefs(_ data: Data) -> (prefs: [String: Any], at: Double)? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let p = obj["prefs"] as? [String: Any], let at = obj["at"] as? Double else { return nil }
        return (p, at)
    }

    /// 一条阅读记录 → 书架上的 id：单集是「节目文件夹#条目 id」，文档就是路径
    private static func readID(_ e: [String: Any], paths: [String: String]) -> String? {
        if let show = e["show"] as? String, let entry = e["entry"] as? String { return "\(show)#\(entry)" }
        guard let path = e["path"] as? String else { return nil }
        return paths[path] ?? path
    }
}
