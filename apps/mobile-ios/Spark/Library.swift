import Foundation

/// 书架：从仓库的文件树和各文件夹的处理记录（.manifest.json）拼出来的节目、会议、订阅和报告。
/// 跟电脑上阅读页（apps/reader/server.py）同一套认法：一个文件夹一个节目/会议，
/// 「信息跟进」下面每个子文件夹是一个订阅，output/ 是笔记洞察的报告。
struct Library {
    var shows: [Show]
    var reports: [Doc]

    static let empty = Library(shows: [], reports: [])

    var allEpisodes: [Episode] { shows.flatMap(\.episodes) }

    func shows(_ kind: Category) -> [Show] {
        shows.filter { $0.kind == kind }.sorted { ($0.latest, $0.name) > ($1.latest, $1.name) }
    }

    func show(_ id: String) -> Show? { shows.first { $0.id == id } }

    /// 「最新」：所有单集、文章和报告按日期混排
    var feed: [FeedItem] {
        let eps = allEpisodes.filter { !$0.date.isEmpty }.map(FeedItem.episode)
        let docs = (reports + shows.flatMap(\.docs)).filter { !$0.date.isEmpty }.map(FeedItem.doc)
        return (eps + docs).sorted { $0.date > $1.date }.prefix(300).map { $0 }
    }

    static let sparkDir = "Spark/"
    static let trackDir = "Spark/信息跟进/"
    static let reportsDir = "output/"

    static func build(tree: [String: TreeItem], read: (String) -> String?) -> Library {
        // 每个文件夹里有哪些文件
        var folders: [String: [String]] = [:]
        for path in tree.keys {
            let dir = (path as NSString).deletingLastPathComponent
            folders[dir, default: []].append(path)
        }
        var shows: [Show] = []
        for dir in folders.keys {
            let isTrack = dir.hasPrefix(trackDir) && !dir.dropFirst(trackDir.count).contains("/")
            let isTop = dir.hasPrefix(sparkDir) && !dir.dropFirst(sparkDir.count).contains("/") && dir != "Spark/信息跟进"
            guard isTrack || isTop else { continue }
            if let s = Show.load(folder: dir, track: isTrack, files: folders, read: read) { shows.append(s) }
        }
        let reports = tree.keys.filter { $0.hasPrefix(reportsDir) && $0.hasSuffix(".md") && !$0.dropFirst(reportsDir.count).contains("/") }
            .map { Doc(path: $0, kind: .report, source: "笔记洞察报告") }
        return Library(shows: shows, reports: reports)
    }
}

struct Show: Identifiable, Hashable {
    let id: String          // 文件夹在仓库里的路径，如 Spark/Dwarkesh
    let name: String
    let kind: Category
    let hostName: String
    let overallSummary: String
    let summaryPath: String?   // 节目/会议总结的 .md
    let sourceURL: String
    let topics: [(name: String, ids: [String])]
    let episodes: [Episode]
    let docs: [Doc]            // 没有处理记录的文章（比如信息跟进的「简报」）

    var latest: String { max(episodes.map(\.date).max() ?? "", docs.map(\.date).max() ?? "") }
    var countLabel: String {
        if !episodes.isEmpty {
            switch kind {
            case .summit: return "\(episodes.count) 个议题"
            case .series: return "\(episodes.count) 期"
            default: return "\(episodes.count) 篇"
            }
        }
        return "\(docs.count) 份"
    }

    static func == (a: Show, b: Show) -> Bool { a.id == b.id && a.episodes.count == b.episodes.count }
    func hash(into h: inout Hasher) { h.combine(id) }

    static func load(folder: String, track: Bool, files: [String: [String]], read: (String) -> String?) -> Show? {
        let name = (folder as NSString).lastPathComponent
        let manifestPath = "\(folder)/.manifest.json"
        var m: [String: Any] = [:]
        if let text = read(manifestPath), let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] {
            m = obj
        }
        let mine = files[folder] ?? []
        let kind: Category = track ? .track : ((m["content_type"] as? String) == "series" ? .series : .summit)
        var episodes: [Episode] = []
        for (key, raw) in (m["entries"] as? [String: Any]) ?? [:] {
            guard let row = raw as? [String: Any], row["ok"] as? Bool == true else { continue }
            episodes.append(Episode(row: row, key: key, folder: folder, showName: name, kind: kind))
        }
        // 没有处理记录的 .md（简报、零散文章）
        let used = Set(episodes.flatMap { [$0.notePath, $0.speechPath, $0.transcriptPath, $0.articlePath].compactMap { $0 } })
        let summaryPath = mine.first { ($0 as NSString).lastPathComponent == "\(name).md" }
        let docs = mine.filter { $0.hasSuffix(".md") && !used.contains($0) && $0 != summaryPath }
            .map { Doc(path: $0, kind: kind, source: name) }
        if episodes.isEmpty && docs.isEmpty && summaryPath == nil { return nil }
        episodes.sort { ($0.date, $0.rank) > ($1.date, $1.rank) }
        var topics: [(String, [String])] = []
        if let groups = m["topic_groups"] as? [String: [String]] {
            topics = groups.map { ($0.key, $0.value) }.sorted { $0.1.count > $1.1.count }
        }
        return Show(id: folder, name: name, kind: kind, hostName: m["host_name"] as? String ?? "",
                    overallSummary: m["overall_summary"] as? String ?? "", summaryPath: summaryPath,
                    sourceURL: m["source_url"] as? String ?? "", topics: topics, episodes: episodes, docs: docs)
    }
}

struct Episode: Identifiable, Hashable {
    let id: String            // 文件夹 + 条目 id
    let entryID: String
    let showID: String
    let showName: String
    let kind: Category
    let title: String
    let date: String          // yyyyMMdd，没有就是空串
    let rank: Int
    let duration: Int
    let url: String
    let tldr: String
    let body: String
    let guests: [String]
    let notePath: String?
    let speechPath: String?
    let transcriptPath: String?
    let articlePath: String?  // 信息跟进：文章本身

    init(row: [String: Any], key: String, folder: String, showName: String, kind: Category) {
        let entry = row["entry"] as? [String: Any] ?? [:]
        entryID = entry["id"] as? String ?? key
        id = "\(folder)#\(entryID)"
        showID = folder
        self.showName = showName
        self.kind = kind
        title = entry["title"] as? String ?? key
        rank = row["rank"] as? Int ?? 0
        duration = entry["duration"] as? Int ?? 0
        url = entry["url"] as? String ?? ""
        let summary = row["summary"] as? [String: Any]
        tldr = summary?["tldr"] as? String ?? ""
        body = summary?["body"] as? String ?? ""
        guests = summary?["guests"] as? [String] ?? []
        func path(_ k: String) -> String? {
            (row[k] as? String).flatMap { $0.isEmpty ? nil : "\(folder)/\($0)" }
        }
        let rel = path("relative_path")
        if kind == .track {
            articlePath = rel
            transcriptPath = nil
        } else {
            articlePath = nil
            transcriptPath = rel
        }
        speechPath = path("speech_relative_path")
        notePath = path("note_relative_path")
        // 日期：处理记录里的播出日期，没有就看文件名前面的 yyyyMMdd
        var d = entry["publish_date"] as? String ?? ""
        if d.count != 8, let file = rel ?? speechPath ?? notePath {
            let name = (file as NSString).lastPathComponent
            if let m = name.range(of: #"^\d{8}"#, options: .regularExpression) { d = String(name[m]) }
        }
        date = d.count == 8 ? d : ""
    }

    var durationLabel: String {
        guard duration > 0 else { return kind == .track ? "文章" : "" }
        let h = duration / 3600, m = (duration % 3600) / 60
        if h == 0 { return "\(max(m, 1)) 分钟" }
        return m == 0 ? "\(h) 小时" : "\(h) 小时 \(m) 分"
    }
}

/// 没有处理记录的一篇 Markdown：报告、简报
struct Doc: Identifiable, Hashable {
    let path: String
    let kind: Category
    let source: String
    var id: String { path }

    var title: String {
        var stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        if let r = stem.range(of: #"^\d{8}[ _]"#, options: .regularExpression) { stem.removeSubrange(r) }
        stem = stem.replacingOccurrences(of: "技术洞察报告_", with: "")
        if let r = stem.range(of: #"_\d{8}$"#, options: .regularExpression) { stem.removeSubrange(r) }
        return stem.replacingOccurrences(of: "_", with: " ")
    }

    var date: String {
        let name = (path as NSString).lastPathComponent
        if let r = name.range(of: #"\d{8}"#, options: .regularExpression) { return String(name[r]) }
        return ""
    }
}

enum FeedItem: Identifiable, Hashable {
    case episode(Episode)
    case doc(Doc)
    var id: String {
        switch self {
        case .episode(let e): e.id
        case .doc(let d): d.id
        }
    }
    var date: String {
        switch self {
        case .episode(let e): e.date
        case .doc(let d): d.date
        }
    }
    var kind: Category {
        switch self {
        case .episode(let e): e.kind
        case .doc(let d): d.kind
        }
    }
}

enum Dates {
    static func label(_ yyyymmdd: String, weekday: Bool = false) -> String {
        guard yyyymmdd.count == 8, let y = Int(yyyymmdd.prefix(4)),
              let m = Int(yyyymmdd.dropFirst(4).prefix(2)), let d = Int(yyyymmdd.suffix(2)) else { return "" }
        let cal = Calendar(identifier: .gregorian)
        let date = cal.date(from: DateComponents(year: y, month: m, day: d)) ?? .now
        var s = cal.component(.year, from: .now) == y ? "\(m)月\(d)日" : "\(y)年\(m)月\(d)日"
        if weekday {
            let names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            s += " " + names[cal.component(.weekday, from: date) - 1]
        }
        return s
    }
}

/// 读过的单集：存在手机本地
@Observable
final class ReadState {
    private(set) var read: Set<String>
    init() { read = Set(UserDefaults.standard.stringArray(forKey: "read.ids") ?? []) }
    func mark(_ id: String) {
        guard !read.contains(id) else { return }
        read.insert(id)
        UserDefaults.standard.set(Array(read), forKey: "read.ids")
    }
    func isRead(_ id: String) -> Bool { read.contains(id) }
}
