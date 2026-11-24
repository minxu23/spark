import Foundation

/// 书架：从仓库的文件树和各文件夹的处理记录（.manifest.json）拼出来的节目、会议、订阅和报告。
/// 跟电脑上阅读页（apps/reader/server.py）同一套认法：一个文件夹一个节目/会议，
/// 「信息跟进」下面每个子文件夹是一个订阅，output/ 是笔记洞察的报告。
struct Library {
    let shows: [Show]
    let reports: [Doc]
    /// 剪藏（Spark/剪藏/ 下一篇一个 .md），新存的在前
    let clips: [Doc]
    /// 随手记（Spark/笔记/ 下一条一个 .md，Jottings.swift），新建的在前
    let notes: [Doc]

    /// 「最新」：所有单集、文章和报告按日期混排。建书架时排一次存着——「最新」页每画一次要用三回，不能回回重排几千条
    let feed: [FeedItem]

    init(shows: [Show], reports: [Doc], clips: [Doc] = [], notes: [Doc] = []) {
        self.shows = shows; self.reports = reports; self.clips = clips; self.notes = notes
        let eps = shows.flatMap(\.episodes).filter { !$0.date.isEmpty }.map(FeedItem.episode)
        let docs = (reports + clips + notes + shows.flatMap(\.docs)).filter { !$0.date.isEmpty }.map(FeedItem.doc)
        // 同一时刻的几条按 id 排定先后（决胜键），不然顺序跟着字典遍历走，每次建书架都可能变
        feed = (eps + docs).sorted { ($0.order, $0.id) > ($1.order, $1.id) }.prefix(300).map { $0 }
    }

    static let empty = Library(shows: [], reports: [])

    var allEpisodes: [Episode] { shows.flatMap(\.episodes) }

    func shows(_ kind: Category) -> [Show] {
        shows.filter { $0.kind == kind }.sorted { ($0.latest, $0.name, $0.id) > ($1.latest, $1.name, $1.id) }
    }

    func show(_ id: String) -> Show? { shows.first { $0.id == id } }

    static let sparkDir = "Spark/"
    static let trackDir = "Spark/信息跟进/"
    static let reportsDir = "output/"
    static let reviewsDir = "output/复盘/"     // 电脑上生成的复盘（core/review.py）

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
                && dir != Clips.folder && dir != Jottings.folder   // 随手记不是节目
            guard isTrack || isTop else { continue }
            if let s = Show.load(folder: dir, track: isTrack, files: folders, read: read) { shows.append(s) }
        }
        shows.sort { $0.id < $1.id }   // 来自字典遍历：按文件夹路径排好，顺序每次一样
        // output/ 下的报告，和按主题分的一层子文件夹里的（电脑上现在写进 output/<主题>/技术洞察报告_….md）
        let reports = tree.keys.filter { path in
            guard path.hasPrefix(reportsDir), path.hasSuffix(".md"), !path.hasPrefix(reviewsDir) else { return false }
            let rest = path.dropFirst(reportsDir.count).split(separator: "/")
            return rest.count == 1 || (rest.count == 2 && rest[1].hasPrefix("技术洞察报告_") && !rest[0].hasPrefix("."))
        }
            .map { Doc(path: $0, kind: .report, source: "笔记洞察报告") }
            + tree.keys.filter { $0.hasPrefix(reviewsDir) && $0.hasSuffix(".md") && !$0.dropFirst(reviewsDir.count).contains("/") }
            .map { path in
                // 复盘的标题写在文件第一行，手机上改名改的也是它
                let head = read(path).flatMap { RX.first(#"(?m)^# +(.+)$"#, $0) }?.trimmingCharacters(in: .whitespaces) ?? ""
                return Doc(path: path, kind: .report, source: "复盘", fullTitle: head)
            }
        let clips = tree.keys.filter(Clips.isClip).sorted(by: >).map { path in
            // 来源（公众号名）、原标题、一句话写在文件头里
            let f = read(path).map(Clips.frontmatter) ?? [:]
            let source = f["source"] ?? ""
            var d = Doc(path: path, kind: .clip, source: source.isEmpty ? "剪藏" : source, fullTitle: f["title"] ?? "", tldr: f["tldr"] ?? "")
            (d.day, d.instant) = Doc.clipDay(clipped: Clips.clippedDay(path: path, front: f), clippedAt: f["clipped_at"] ?? "")
            return d
        }
        // 随手记：标题、新建的时刻在文件头里；「最新」里按新建那天排
        let notes = tree.keys.filter(Jottings.isJotting).sorted(by: >).map { path in
            let f = read(path).map(Clips.frontmatter) ?? [:]
            var d = Doc(path: path, kind: .note, source: Jottings.kindJotting, fullTitle: f["title"] ?? "")
            (d.day, d.instant) = Doc.clipDay(clipped: "", clippedAt: f["created_at"] ?? "")
            return d
        }
        return Library(shows: shows, reports: reports.sorted { $0.path < $1.path }, clips: clips, notes: notes)
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
    /// 会议的日期（SummitDates.resolve）：手动填的、议程的会议时间，没有就是视频发布日期（source=sessions）；
    /// 只有会议有，不知道是 nil
    var event: SummitDates.Range? = nil
    /// 给人看的一整句：「会议时间：2026年10月13日—16日」或「视频发布：2026年9月14日—23日」；不知道是空串（建书架时算好）
    var eventText = ""
    /// 各场议程日期（session_date）不在同一天：多天的会议才在每场旁边标日期（建书架时算好，不在每一行里算）
    var multiDay = false

    /// 按会议时间排用：开始那天 YYYY-MM-DD（手动填的、议程的优先，没有用视频发布的），不知道是空串
    var eventKey: String { event?.start ?? "" }

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

    static func == (a: Show, b: Show) -> Bool { a.id == b.id && a.episodes.count == b.episodes.count && a.event == b.event
        && a.multiDay == b.multiDay && a.eventText == b.eventText }
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
        let docs = mine.filter { $0.hasSuffix(".md") && !used.contains($0) && $0 != summaryPath }.sorted()
            .map { path in
                // 信息简报的文件名只到日期，同一天几份按正文里的「生成时间：2026-10-30 21:16」排先后
                var d = Doc(path: path, kind: kind, source: name)
                if track { d.time = read(path).flatMap { Doc.generatedTime($0) } ?? "" }
                return d
            }
        if episodes.isEmpty && docs.isEmpty && summaryPath == nil { return nil }
        episodes.sort { ($0.date, $0.instant, $0.rank, $0.title, $0.id) > ($1.date, $1.instant, $1.rank, $1.title, $1.id) }
        let event = kind == .summit ? SummitDates.resolve(m) : nil
        var topics: [(String, [String])] = []
        if let groups = m["topic_groups"] as? [String: [String]] {
            topics = groups.map { ($0.key, $0.value) }.sorted { ($0.1.count, $1.0) > ($1.1.count, $0.0) }
        }
        return Show(id: folder, name: name, kind: kind, hostName: m["host_name"] as? String ?? "",
                    overallSummary: m["overall_summary"] as? String ?? "", summaryPath: summaryPath,
                    sourceURL: m["source_url"] as? String ?? "", topics: topics, episodes: episodes, docs: docs,
                    event: event, eventText: SummitDates.display(event),
                    multiDay: kind == .summit && Set(episodes.map(\.sessionDay).filter { !$0.isEmpty }).count > 1)
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
    /// 会议里这一场是哪天开的（yyyyMMdd）：只认议程上的 session_date，没有是空串（视频发布那天不当这一场的日期）
    let sessionDay: String
    /// 发表的时刻：处理记录里的 publish_at（ISO 带时差，老记录没有）；instant 是它的绝对秒数（没有是 0），排同一天的先后用。
    /// 哪天以处理记录里写着的 publish_date 为准，跟手机在哪个时区无关（同电脑 reader _episodes）
    let publishAt: String
    let instant: Int64
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
    /// 简讯：频道里的短视频（检查订阅时标了 short 的只有整理稿；以前处理过的按时长认，同电脑 pipeline.is_short_row）
    let short: Bool

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
        sessionDay = SummitDates.sessionDay(entry).replacingOccurrences(of: "-", with: "")
        publishAt = entry["publish_at"] as? String ?? ""
        if case .at(let t)? = DocTime.entryPublished(at: publishAt, date: date) {
            instant = Int64(t.timeIntervalSince1970.rounded(.down))
        } else {
            instant = 0
        }
        short = entry["short"] as? Bool == true
            || (kind == .series && entry["source_type"] == nil && duration > 0 && duration <= EpisodeRunner.shortSeconds)
    }

    var durationLabel: String {
        guard duration > 0 else { return kind == .track ? "文章" : short ? "简讯" : "" }
        let h = duration / 3600, m = (duration % 3600) / 60
        if short { return "简讯・\(max(m, 1)) 分钟" }
        if h == 0 { return "\(max(m, 1)) 分钟" }
        return m == 0 ? "\(h) 小时" : "\(h) 小时 \(m) 分"
    }
}

/// 没有处理记录的一篇 Markdown：报告、简报
struct Doc: Identifiable, Hashable {
    let path: String
    let kind: Category
    let source: String
    /// 剪藏：文件头里的原标题、一句话
    var fullTitle = ""
    var tldr = ""
    /// 同一天里的先后：信息简报正文里的生成时刻，写成 HHmm（别的文档是空的）
    var time = ""
    /// 哪天（yyyyMMdd）：剪藏取文件头里写着的收录日期；空的话看文件名
    var day = ""
    /// 剪藏收录的时刻（绝对秒数，没有是 0）
    var instant: Int64 = 0
    var id: String { path }

    /// 剪藏按收录那天归组（「最新」、日期标签）：文件名前面现在是发表那天，收录日期读文件头里写着的 clipped
    /// （不按手机时区重算），时刻读 clipped_at；老文件没有就是空的，还看文件名。返回 (yyyyMMdd, 绝对秒数)
    static func clipDay(clipped: String, clippedAt: String) -> (String, Int64) {
        var day = clipped.wholeMatch(of: #/\d{4}-\d{2}-\d{2}/#) != nil ? clipped.replacingOccurrences(of: "-", with: "") : ""
        var instant: Int64 = 0
        if case .at(let t)? = DocTime.parse(clippedAt) {
            instant = Int64(t.timeIntervalSince1970.rounded(.down))
            if day.isEmpty { day = (DocTime.writtenDay(clippedAt) ?? "").replacingOccurrences(of: "-", with: "") }
        }
        return (day, instant)
    }

    /// 「最新」里排同一天的先后用的绝对秒数：剪藏收录的时刻；简报按生成时刻（日期 + HHmm，按本机时区算）
    var feedInstant: Int64 {
        if instant > 0 { return instant }
        guard time.count == 4, date.count == 8, let y = Int(date.prefix(4)), let m = Int(date.dropFirst(4).prefix(2)),
              let d = Int(date.suffix(2)), let hh = Int(time.prefix(2)), let mm = Int(time.suffix(2)),
              let t = Calendar(identifier: .gregorian).date(from: DateComponents(year: y, month: m, day: d, hour: hh, minute: mm))
        else { return 0 }
        return Int64(t.timeIntervalSince1970)
    }
    /// 排先后用：日期 + 生成时刻
    var order: String { date + time }

    /// 简报正文开头的「生成时间：2026-10-30 21:16」（老简报写在标题里「# 信息简报 · 2026-10-30 21:16」）→ 2116
    static func generatedTime(_ text: String) -> String? {
        guard let t = RX.first(#"(?m)^(?:生成时间：|# 信息简报 · )\d{4}-\d{2}-\d{2} (\d{2}:\d{2})"#, String(text.prefix(600))) else { return nil }
        return t.replacingOccurrences(of: ":", with: "")
    }

    var title: String {
        if !fullTitle.isEmpty { return fullTitle }
        var stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        if let r = stem.range(of: #"^\d{8}[ _]"#, options: .regularExpression) { stem.removeSubrange(r) }
        stem = stem.replacingOccurrences(of: "技术洞察报告_", with: "")
        // 复盘_阅读_20261026-20261102 → 阅读复盘 20261026-20261102
        for (prefix, label) in [("复盘_阅读_", "阅读复盘 "), ("复盘_剪藏_", "剪藏复盘 "), ("复盘_主题_", "主题来龙去脉："), ("复盘_观点检验_", "观点检验：")]
            where stem.hasPrefix(prefix) {
            stem = label + stem.dropFirst(prefix.count)
        }
        if let r = stem.range(of: #"_\d{8}$"#, options: .regularExpression) { stem.removeSubrange(r) }
        return stem.replacingOccurrences(of: "_", with: " ")
    }

    var date: String {
        if !day.isEmpty { return day }
        let name = (path as NSString).lastPathComponent
        // 复盘的文件名里可能有起止两个日期，取后一个（生成的那天）
        let found = name.ranges(of: #/\d{8}/#).map { String(name[$0]) }
        return found.last ?? ""
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
    /// 排先后用：同一天的简报按生成时刻，单集按发表时刻，剪藏按收录时刻
    var order: String {
        switch self {
        case .episode(let e): e.date + "|" + String(format: "%012lld", e.instant)
        case .doc(let d): d.date + "|" + String(format: "%012lld", d.feedInstant)
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
    /// 今天，写成 yyyyMMdd
    static var todayKey: String {
        let c = Calendar(identifier: .gregorian).dateComponents([.year, .month, .day], from: .now)
        return String(format: "%04d%02d%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

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
    /// 别的设备上读过的（DeviceSync）
    func merge(_ ids: some Sequence<String>) {
        // 别的设备、电脑的阅读记录里可能还是剪藏改名前的路径：换成新名字再记（Vault.clipRenames）
        let renames = Vault.clipRenames()
        let new = Set(ids.map { renames[$0] ?? $0 }).subtracting(read)
        guard !new.isEmpty else { return }
        read.formUnion(new)
        UserDefaults.standard.set(Array(read), forKey: "read.ids")
    }
    func isRead(_ id: String) -> Bool { read.contains(id) }

    /// 电脑上改过名的剪藏（Vault.clipRenames）：读过的旧路径换成新路径。每条只换一次
    func applyRenames(_ map: [String: String]) {
        var done = Set(UserDefaults.standard.stringArray(forKey: "read.renamesApplied") ?? [])
        let todo = map.filter { !done.contains("\($0.key)\t\($0.value)") }
        guard !todo.isEmpty else { return }
        for (old, new) in todo where read.contains(old) {
            read.remove(old)
            read.insert(new)
        }
        done.formUnion(todo.map { "\($0.key)\t\($0.value)" })
        UserDefaults.standard.set(Array(read), forKey: "read.ids")
        UserDefaults.standard.set(Array(done).sorted(), forKey: "read.renamesApplied")
    }
}
