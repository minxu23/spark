import Foundation

/// 手机上那份笔记库（Vault.root 下的 Spark/、output/）的扫描和读取，给手机上跑的复盘、出报告用。
/// 跟电脑端 apps/notes2insight/vault.py、core/review.py 的 build_index 同一套认法。
/// 只看已经下载到手机上的文件：GitHub 同步的「轻量」模式下整理稿和文字记录没下，检索就只在小结、报告里找。
struct LocalNotes: Sendable {
    let root: URL

    struct Note: Sendable {
        let path: String      // 相对笔记库
        let folder: String
        let name: String
        let title: String
        let date: String      // YYYY-MM-DD 或空
        let chars: Int
        let mtime: Date
    }

    private static let excludeDirs: Set<String> = ["images", "image", "assets", "attachments", "_resources", "__pycache__", "logs"]
    private static let excludeNames: Set<String> = ["README.md", "Welcome.md"]

    func url(_ rel: String) -> URL { root.appending(path: rel, directoryHint: .notDirectory) }

    func scan() -> [Note] {
        let fm = FileManager.default
        guard let walker = fm.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey, .fileSizeKey, .contentModificationDateKey],
                                         options: [.skipsHiddenFiles]) else { return [] }
        var out: [Note] = []
        let base = root.standardizedFileURL.path
        for case let u as URL in walker {
            let v = try? u.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey, .contentModificationDateKey])
            if v?.isDirectory == true {
                if Self.excludeDirs.contains(u.lastPathComponent) { walker.skipDescendants() }
                continue
            }
            let name = u.lastPathComponent
            guard name.lowercased().hasSuffix(".md"), !Self.excludeNames.contains(name) else { continue }
            let full = u.standardizedFileURL.path
            guard full.hasPrefix(base + "/") else { continue }
            let rel = String(full.dropFirst(base.count + 1))
            let head = Self.head(u)
            let (title, date) = Self.parseHead(head.text, name: name)
            let size = v?.fileSize ?? 0
            out.append(Note(path: rel, folder: (rel as NSString).deletingLastPathComponent.isEmpty ? "." : (rel as NSString).deletingLastPathComponent,
                            name: name, title: title, date: date, chars: Int(Double(size) * head.ratio),
                            mtime: v?.contentModificationDate ?? .distantPast))
        }
        return out.sorted { ($0.folder, $0.name) < ($1.folder, $1.name) }
    }

    private static func head(_ u: URL) -> (text: String, ratio: Double) {
        guard let h = try? FileHandle(forReadingFrom: u) else { return ("", 1.0 / 3) }
        defer { try? h.close() }
        let data = (try? h.read(upToCount: 4096)) ?? Data()
        let text = String(decoding: data, as: UTF8.self)
        return (text, data.isEmpty ? 1.0 / 3 : Double(text.count) / Double(data.count))
    }

    static func parseHead(_ head: String, name: String) -> (String, String) {
        var title = RX.first(#"(?m)^\s*title\s*[:：]\s*(.+)$"#, head)?.trimmingCharacters(in: CharacterSet(charactersIn: " \"'")) ?? ""
        if title.isEmpty { title = RX.first(#"(?m)^#\s+(.+)$"#, head)?.trimmingCharacters(in: .whitespaces) ?? "" }
        if title.isEmpty { title = (name as NSString).deletingPathExtension }
        var date = RX.first(#"(?m)^\s*(?:date|created|发布日期)\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})"#, head)
            ?? RX.first(#"(?m)^-\s*(?:发布日期|日期)\s*[:：]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})"#, head) ?? ""
        if date.isEmpty, let g = RX.groups(#"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})"#, name) {
            date = "\(g[1])-\(g[2])-\(g[3])"
        }
        return (String(title.prefix(200)), date)
    }

    func read(_ rel: String, maxChars: Int = 0) -> String? {
        guard !rel.contains(".."), let data = try? Data(contentsOf: url(rel)) else { return nil }
        let t = String(decoding: data, as: UTF8.self)
        return maxChars > 0 && t.count > maxChars ? String(t.prefix(maxChars)) : t
    }

    // MARK: 处理记录索引（core/review.py 的 build_index）

    struct Item: Sendable {
        let show: String, folder: String, entry: String, title: String, date: String   // date 是 YYYYMMDD
        let tldr: String, body: String, note: String, updatedAt: String
    }

    struct Index: Sendable {
        var items: [String: Item] = [:]          // "folder#entry" -> 条目
        var byPath: [String: String] = [:]       // 文件相对路径 -> "folder#entry"
    }

    func buildIndex() -> Index {
        var idx = Index()
        let fm = FileManager.default
        var folders: [String] = []
        for parent in ["Spark", "Spark/信息跟进"] {
            for name in (try? fm.contentsOfDirectory(atPath: url(parent).path)) ?? [] where !name.hasPrefix(".") {
                folders.append("\(parent)/\(name)")
            }
        }
        for folder in folders {
            guard let data = try? Data(contentsOf: url("\(folder)/.manifest.json")),
                  let m = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let entries = m["entries"] as? [String: Any] else { continue }
            for (key, rowAny) in entries {
                guard let row = rowAny as? [String: Any], row["ok"] as? Bool == true else { continue }
                let entry = row["entry"] as? [String: Any] ?? [:]
                let eid = (entry["id"] as? String) ?? (entry["id"].map { "\($0)" }) ?? key
                let files = ["note_relative_path", "speech_relative_path", "relative_path"]
                    .compactMap { row[$0] as? String }.filter { !$0.isEmpty }.map { "\(folder)/\($0)" }
                var date = entry["publish_date"].map { "\($0)" } ?? ""
                if date.range(of: #"^\d{8}$"#, options: .regularExpression) == nil {
                    date = files.lazy.compactMap { RX.first(#"^(\d{8})"#, ($0 as NSString).lastPathComponent) }.first ?? ""
                }
                let summary = row["summary"] as? [String: Any] ?? [:]
                let k = "\(folder)#\(eid)"
                idx.items[k] = Item(show: (folder as NSString).lastPathComponent, folder: folder, entry: eid,
                                    title: (entry["title"] as? String) ?? eid, date: date,
                                    tldr: summary["tldr"] as? String ?? "", body: summary["body"] as? String ?? "",
                                    note: (row["note_relative_path"] as? String).map { "\(folder)/\($0)" } ?? "",
                                    updatedAt: row["updated_at"] as? String ?? "")
                for f in files { idx.byPath[f] = k }
            }
        }
        return idx
    }

    /// 笔记里「我的高亮」那一节的条目
    func highlights(_ noteRel: String) -> [String] {
        guard !noteRel.isEmpty, let text = read(noteRel),
              let sec = RX.first(#"(?ms)^## 我的高亮[ \t]*\n(.*?)(?=^## |\z)"#, text) else { return [] }
        return sec.split(separator: "\n").filter { $0.hasPrefix("- ") }.map { String($0.dropFirst(2)).trimmingCharacters(in: .whitespaces) }
    }
}

/// 正则的几个小工具（NSRegularExpression 写起来太啰嗦）
enum RX {
    nonisolated(unsafe) private static var cache: [String: NSRegularExpression] = [:]
    private static let lock = NSLock()

    static func re(_ p: String) -> NSRegularExpression {
        lock.withLock {
            if let r = cache[p] { return r }
            let r = try! NSRegularExpression(pattern: p)
            cache[p] = r
            return r
        }
    }

    /// 第一处匹配的各组（0 是整段），没匹配到的组是空串
    static func groups(_ p: String, _ s: String) -> [String]? {
        let ns = s as NSString
        guard let m = re(p).firstMatch(in: s, range: NSRange(location: 0, length: ns.length)) else { return nil }
        return (0..<m.numberOfRanges).map { m.range(at: $0).location == NSNotFound ? "" : ns.substring(with: m.range(at: $0)) }
    }

    /// 第一处匹配的第 1 组（没有组就是整段）
    static func first(_ p: String, _ s: String) -> String? {
        guard let g = groups(p, s) else { return nil }
        return g.count > 1 ? g[1] : g[0]
    }

    static func all(_ p: String, _ s: String) -> [[String]] {
        let ns = s as NSString
        return re(p).matches(in: s, range: NSRange(location: 0, length: ns.length)).map { m in
            (0..<m.numberOfRanges).map { m.range(at: $0).location == NSNotFound ? "" : ns.substring(with: m.range(at: $0)) }
        }
    }

    static func sub(_ p: String, _ template: String, _ s: String) -> String {
        re(p).stringByReplacingMatches(in: s, range: NSRange(location: 0, length: (s as NSString).length), withTemplate: template)
    }

    static func split(_ p: String, _ s: String) -> [String] {
        let ns = s as NSString
        var out: [String] = [], last = 0
        for m in re(p).matches(in: s, range: NSRange(location: 0, length: ns.length)) {
            out.append(ns.substring(with: NSRange(location: last, length: m.range.location - last)))
            last = m.range.location + m.range.length
        }
        out.append(ns.substring(from: last))
        return out
    }
}
