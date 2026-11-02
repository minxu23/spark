import Foundation
import Security

struct SparkError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// 存在钥匙串里的连接信息：仓库和令牌。令牌只在这里，不写进 UserDefaults 或日志。
/// 主 App 和分享扩展的 entitlements 里第一个 keychain-access-groups 是同一个共享组，
/// 查询和保存都不指定组，就落在那个组里，两边都读得到。
struct Connection: Equatable {
    var owner: String
    var repo: String
    var token: String
    var apiBase: String = "https://api.github.com"

    var slug: String { "\(owner)/\(repo)" }

    private static let service = "com.minxu.spark.github"

    static func load() -> Connection? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let owner = obj["owner"], let repo = obj["repo"], let token = obj["token"] else { return nil }
        return Connection(owner: owner, repo: repo, token: token, apiBase: obj["apiBase"] ?? "https://api.github.com")
    }

    func save() throws {
        Self.delete()
        let data = try JSONSerialization.data(withJSONObject: ["owner": owner, "repo": repo, "token": token, "apiBase": apiBase])
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: Self.service,
                                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                kSecValueData as String: data]
        let status = SecItemAdd(q as CFDictionary, nil)
        if status != errSecSuccess { throw SparkError("令牌存不进钥匙串（\(status)）") }
    }

    static func delete() {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }

    /// 「minxu23/spark-vault」或 GitHub 网址都认
    static func parseSlug(_ text: String) -> (String, String)? {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        for p in ["https://github.com/", "http://github.com/", "github.com/", "git@github.com:"] where s.hasPrefix(p) {
            s = String(s.dropFirst(p.count))
        }
        if s.hasSuffix(".git") { s = String(s.dropLast(4)) }
        let parts = s.split(separator: "/").map(String.init)
        guard parts.count == 2, parts.allSatisfy({ !$0.isEmpty && $0.range(of: #"^[\w.-]+$"#, options: .regularExpression) != nil })
        else { return nil }
        return (parts[0], parts[1])
    }
}

struct TreeItem: Codable, Hashable {
    let path: String
    let sha: String
    let size: Int
}

struct Issue: Identifiable, Decodable, Hashable {
    let number: Int
    let title: String
    let body: String?
    let state: String
    let labels: [Label]
    let created_at: Date
    let closed_at: Date?
    let user: User?
    var id: Int { number }

    struct Label: Decodable, Hashable { let name: String }
    struct User: Decodable, Hashable { let login: String }

    var labelNames: Set<String> { Set(labels.map(\.name)) }
}

struct IssueComment: Decodable, Hashable {
    let id: Int
    let body: String
    let updated_at: Date
}

/// GitHub REST 接口里用到的那几个。
actor GitHub {
    let conn: Connection
    private let session: URLSession

    init(_ conn: Connection) {
        self.conn = conn
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 60
        session = URLSession(configuration: cfg)
    }

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    private func request(_ path: String, method: String = "GET", body: Any? = nil,
                         accept: String = "application/vnd.github+json") async throws -> Data {
        guard let url = URL(string: conn.apiBase + path) else { throw SparkError("地址不对：\(path)") }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("Bearer \(conn.token)", forHTTPHeaderField: "Authorization")
        req.setValue(accept, forHTTPHeaderField: "Accept")
        req.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw SparkError("连不上 GitHub：\(error.localizedDescription)")
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["message"] as? String
            switch code {
            case 401: throw SparkError("令牌不对或已过期（401）")
            case 403: throw SparkError("令牌没有这个权限，或者请求太频繁了（403）\(msg.map { "：\($0)" } ?? "")")
            case 404: throw SparkError("找不到仓库或文件（404）：检查仓库名，以及令牌是否选了这个仓库")
            default: throw SparkError("GitHub 返回 \(code)\(msg.map { "：\($0)" } ?? "")")
            }
        }
        return data
    }

    private var repoPath: String { "/repos/\(conn.owner)/\(conn.repo)" }

    /// main 分支最新的提交（sha，时间）
    func head() async throws -> (sha: String, date: Date?) {
        let data = try await request("\(repoPath)/commits/main")
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let sha = obj?["sha"] as? String ?? ""
        let dateStr = ((obj?["commit"] as? [String: Any])?["committer"] as? [String: Any])?["date"] as? String
        return (sha, dateStr.flatMap { ISO8601DateFormatter().date(from: $0) })
    }

    /// 整棵文件树（只要文件，不要目录）
    func tree(commit: String) async throws -> [TreeItem] {
        let data = try await request("\(repoPath)/git/trees/\(commit)?recursive=1")
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        if obj?["truncated"] as? Bool == true {
            throw SparkError("仓库文件太多，GitHub 一次列不全")
        }
        let items = obj?["tree"] as? [[String: Any]] ?? []
        return items.compactMap { i in
            guard i["type"] as? String == "blob", let path = i["path"] as? String, let sha = i["sha"] as? String else { return nil }
            return TreeItem(path: path, sha: sha, size: i["size"] as? Int ?? 0)
        }
    }

    func blob(_ sha: String) async throws -> Data {
        try await request("\(repoPath)/git/blobs/\(sha)", accept: "application/vnd.github.raw+json")
    }

    // MARK: Issue 任务

    func tasks() async throws -> [Issue] {
        let data = try await request("\(repoPath)/issues?labels=spark-task&state=all&per_page=30&sort=created&direction=desc")
        return try Self.decoder.decode([Issue].self, from: data)
    }

    func comments(_ number: Int) async throws -> [IssueComment] {
        let data = try await request("\(repoPath)/issues/\(number)/comments?per_page=30")
        return try Self.decoder.decode([IssueComment].self, from: data)
    }

    func createTask(title: String, body: String) async throws -> Issue {
        let data = try await request("\(repoPath)/issues", method: "POST",
                                     body: ["title": title, "body": body, "labels": ["spark-task"]])
        return try Self.decoder.decode(Issue.self, from: data)
    }

    /// 放手机阅读记录的那个 Issue（带 spark-readlog 标签），没有就开一个
    func readlogIssue() async throws -> Int {
        let data = try await request("\(repoPath)/issues?labels=spark-readlog&state=all&per_page=1")
        if let first = try Self.decoder.decode([Issue].self, from: data).first { return first.number }
        let made = try await request("\(repoPath)/issues", method: "POST", body: [
            "title": "Spark 手机阅读记录",
            "body": "手机客户端把阅读记录发在这个 Issue 的评论里，电脑上的 Spark 做复盘时会读。不要关掉也不要删。",
            "labels": ["spark-readlog"]])
        return try Self.decoder.decode(Issue.self, from: made).number
    }

    func comment(_ number: Int, body: String) async throws {
        _ = try await request("\(repoPath)/issues/\(number)/comments", method: "POST", body: ["body": body])
    }

    func removeLabel(_ number: Int, _ label: String) async throws {
        let enc = label.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? label
        _ = try await request("\(repoPath)/issues/\(number)/labels/\(enc)", method: "DELETE")
    }

    func reopen(_ number: Int) async throws {
        _ = try await request("\(repoPath)/issues/\(number)", method: "PATCH", body: ["state": "open"])
    }
}
