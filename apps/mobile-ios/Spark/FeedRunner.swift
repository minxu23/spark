import Foundation

/// 电脑同步过来的订阅列表（core/cable_sync.py 的 export_subscriptions，存在笔记库根目录）
struct Subscription: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let url: String
    let kind: String          // track 信息跟进 / podcast
    let source_type: String?
    let category: String?
    let folder: String        // 相对笔记库；在笔记库外面的是空串
    let ignored_ids: [String]

    /// 手机自己能跑到底的：RSS 的信息跟进。节目要转写，只能交给电脑
    var onPhone: Bool { kind == "track" && source_type == "rss" && !folder.isEmpty }
}

/// 手机上检查信息跟进：跟电脑端 apps/summit2md/tracking.py 同一套流程、提示词和笔记格式。
/// 写好的一条存进手机上的笔记库，再交一个 track_item 任务给电脑记进处理记录，电脑以后不会再处理这一条。
enum FeedRunner {
    static let maxChars = 120_000        // pipeline.DEFAULT_MAX_TRANSCRIPT_CHARS
    static let minBody = 120             // core/sources.py 的 _MIN_PLAIN_BODY_LEN：比这短算只有摘要，去抓原文
    static let maxPerRun = 30            // 检查一次最多写几篇，每篇调一次模型

    /// PER_TOPIC_LENGTH_INSTRUCTIONS["medium"]
    static let lengthInstruction = """
    - <要点1：背景/问题>
    - <要点2：核心方法或观点>
    - <要点3：关键数据、案例或产品名（如有，保留英文原名）>
    - <要点4，如有必要>
    - <要点5，如有必要>
    写 3-5 条要点，每条一句话即可，不用展开。
    """

    static func subscriptions(_ lib: LocalNotes) -> [Subscription] {
        guard let text = lib.read(".spark-subscriptions.json"),
              let obj = try? JSONDecoder().decode([String: [Subscription]].self, from: Data(text.utf8)) else { return [] }
        return obj["subscriptions"] ?? []
    }

    /// 已经成功处理过的：id，还有链接（站点改了 guid 格式时靠它认出来）
    static func processed(_ lib: LocalNotes, folder: String) -> (ids: Set<String>, urls: Set<String>) {
        guard let text = lib.read("\(folder)/.manifest.json"),
              let m = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let rows = m["entries"] as? [String: [String: Any]] else { return ([], []) }
        let done = rows.filter { $0.value["ok"] as? Bool == true }
        return (Set(done.keys), Set(done.values.compactMap { ($0["entry"] as? [String: Any])?["url"] as? String }))
    }

    /// 还没处理过、也没被忽略的，新的在前
    static func newEntries(_ sub: Subscription, lib: LocalNotes) async throws -> [Feed.Entry] {
        guard let url = URL(string: sub.url) else { throw SparkError("地址不对") }
        let entries = try await Feed.fetch(url)
        let done = processed(lib, folder: sub.folder)
        let ignored = Set(sub.ignored_ids)
        return entries.filter { !done.ids.contains($0.id) && !done.urls.contains($0.url) && !ignored.contains($0.id) }
            .sorted { $0.date > $1.date }
    }

    static func paragraphs(html: String, base: URL) -> [String] {
        var conv = HTMLToMarkdown(base: base)
        return plain(conv.convert(html))
    }

    /// Markdown 正文 → 段落，图片不要
    static func plain(_ md: String) -> [String] {
        RX.sub(#"!\[[^\]]*\]\([^)]*\)"#, "", md).components(separatedBy: "\n\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    /// 源里给了全文就用，只有摘要就去抓文章页
    static func body(_ e: Feed.Entry) async throws -> [String] {
        guard let url = URL(string: e.url) else { throw SparkError("链接不对") }
        let inFeed = paragraphs(html: e.html, base: url)
        if inFeed.reduce(0, { $0 + $1.count }) >= minBody { return inFeed }
        return plain(try await Clipper.fetch(url).markdown)
    }

    static func prompt(source: String, title: String, text: String) -> String {
        """
        你是资讯编辑。下面是一篇资讯/博客文章或一期节目的正文（来自网页抓取或语音转写，可能夹杂少量噪音）。只根据正文归纳，不要补充正文里没有的信息。

        来源：\(source)
        标题：\(title)

        请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

        TLDR: <一句话核心结论，不超过40字，不要markdown>
        \(lengthInstruction)

        正文：
        \"\"\"
        \(String(text.prefix(maxChars)))
        \"\"\"

        """
    }

    /// pipeline.parse_topic_summary
    static func parseSummary(_ raw: String) -> (tldr: String, body: String) {
        var tldr = "", lines: [String] = []
        for line in raw.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).uppercased().hasPrefix("TLDR:"), let colon = line.firstIndex(of: ":") {
                tldr = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            } else {
                lines.append(line)
            }
        }
        let body = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        if tldr.isEmpty, let first = lines.first {
            tldr = String(first.trimmingCharacters(in: CharacterSet(charactersIn: "- ")).prefix(60))
        }
        return (tldr, body.isEmpty ? raw.trimmingCharacters(in: .whitespacesAndNewlines) : body)
    }

    /// pipeline.sanitize_filename
    static func sanitize(_ name: String, maxLength: Int = 100) -> String {
        var s = RX.sub(#"[\\/:*?"<>|]"#, " ", name.isEmpty ? "untitled" : name)
        s = RX.sub(#"\s+"#, " ", s).trimmingCharacters(in: .whitespacesAndNewlines)
        s = String(s.drop { $0 == "." || $0 == " " })
        if s.isEmpty { s = "untitled" }
        return String(String(s.prefix(maxLength)).reversed().drop { $0 == " " }.reversed())
    }

    /// tracking.render_note
    static func render(_ e: Feed.Entry, source: String, tldr: String, body: String, paragraphs: [String]) -> String {
        var lines = ["# \(e.title)", "", "- 来源：\(source)"]
        if e.date.count == 8 {
            lines.append("- 发布日期：\(e.date.prefix(4))-\(e.date.dropFirst(4).prefix(2))-\(e.date.suffix(2))")
        }
        if !e.url.isEmpty { lines.append("- 原文链接：\(e.url)") }
        lines += ["", "## 小结", ""]
        if !tldr.isEmpty { lines += ["**\(tldr)**", ""] }
        lines += [body, "", "## 原文", ""]
        lines += paragraphs.map { $0 + "\n" }
        return lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
    }

    struct Item: Sendable {
        let folder: String
        let name: String
        let text: String
        let entry: Feed.Entry
        let tldr: String
        let body: String

        var row: [String: Any] {
            var e: [String: Any] = ["id": entry.id, "title": entry.title, "url": entry.url, "duration": 0,
                                    "is_raw_session": false, "source_type": "rss"]
            if !entry.date.isEmpty { e["publish_date"] = entry.date }
            return ["entry": e, "ok": true, "relative_path": name, "summary": ["tldr": tldr, "body": body], "truncated": false]
        }

        var task: (title: String, task: [String: Any]) {
            var e: [String: Any] = ["id": entry.id, "title": entry.title, "url": entry.url]
            if !entry.date.isEmpty { e["publish_date"] = entry.date }
            return ("存进笔记库：\(entry.title)", ["type": "track_item", "folder": folder, "name": name, "content": text,
                                              "entry": e, "summary": ["tldr": tldr, "body": body]])
        }
    }

    /// 处理一条：抓正文 → 写小结 → 排好笔记
    static func process(_ sub: Subscription, _ e: Feed.Entry, config: LLM.Config) async throws -> Item {
        let paras = try await body(e)
        guard !paras.isEmpty else { throw SparkError("没有抓到正文") }
        let raw = try await LLM.complete(prompt(source: sub.name, title: e.title, text: paras.joined(separator: "\n")), config: config)
        let s = parseSummary(raw)
        let title = sanitize(e.title)
        return Item(folder: sub.folder, name: e.date.isEmpty ? "\(title).md" : "\(e.date)_\(title).md",
                    text: render(e, source: sub.name, tldr: s.tldr, body: s.body, paragraphs: paras), entry: e, tldr: s.tldr, body: s.body)
    }
}
