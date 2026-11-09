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

    /// 手机自己能处理的：RSS 的信息跟进，YouTube、Substack、RSS 的节目（没有字幕或文字稿的那几期会提示用户）。
    /// 节目文件夹只认 Spark/ 下一层，电脑收的时候也这么要求
    var onPhone: Bool {
        if kind == "track" { return source_type == "rss" && !folder.isEmpty }
        return ["youtube", "substack", "rss"].contains(source_type ?? "") && folder.split(separator: "/").count == 2
    }
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

    /// 已经成功处理过的：id，链接（站点改了 guid 格式时靠它认出来），最新一期的日期；还有处理失败过的 id
    static func processed(_ lib: LocalNotes, folder: String) -> (ids: Set<String>, urls: Set<String>, newest: String, failed: Set<String>) {
        guard let text = lib.read("\(folder)/.manifest.json"),
              let m = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let rows = m["entries"] as? [String: [String: Any]] else { return ([], [], "", []) }
        let done = rows.filter { $0.value["ok"] as? Bool == true }
        let dates = done.values.compactMap { r -> String? in
            let d = (r["entry"] as? [String: Any])?["publish_date"] as? String ?? ""
            return d.count == 8 ? d : RX.first(#"(?:^|/)(\d{8})_"#, r["relative_path"] as? String ?? "")
        }
        return (Set(done.keys), Set(done.values.compactMap { ($0["entry"] as? [String: Any])?["url"] as? String }), dates.max() ?? "",
                Set(rows.keys).subtracting(done.keys))
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

/// 手机上写好的一期节目
struct EpisodeItem: Sendable {
    let folder: String
    let id: String
    let title: String
    let url: String
    let duration: Int
    let date: String              // yyyyMMdd
    let sourceType: String?       // substack / rss；YouTube 是空
    let subKind: String?          // manual / auto
    let lang: String
    let paragraphs: [(Double, String)]
    let summary: EpisodeRunner.Summary
    var speech: SpeechScript.Result? = nil
    var speechError: String? = nil

    private var fileTitle: String { FeedRunner.sanitize(title, maxLength: 80) }
    var transcriptPath: String { "transcripts/\(date)_\(fileTitle).md" }
    var notePath: String { "notes/\(date) \(fileTitle).md" }
    var speechPath: String { "speech/\(date)_\(fileTitle).md" }

    private var entryRow: [String: Any] {
        var e: [String: Any] = ["id": id, "title": title, "url": url, "duration": duration, "publish_date": date, "is_raw_session": false]
        if let sourceType { e["source_type"] = sourceType }
        if let subKind { e["sub_kind"] = subKind }
        return e
    }

    private var summaryRow: [String: Any] {
        ["tldr": summary.tldr, "body": summary.body, "guests": summary.guests, "topics": summary.topics]
    }

    var row: [String: Any] {
        var r: [String: Any] = ["entry": entryRow, "ok": true, "relative_path": transcriptPath, "note_relative_path": notePath,
                                "summary": summaryRow, "truncated": false]
        if let speech {
            r["speech_relative_path"] = speechPath
            r["speech_lang_mode"] = speech.mode
        }
        return r
    }

    /// 交给电脑的是结构化的内容，文字记录和笔记由电脑自己的模板写（tracking.save_episode），同步回来就是电脑那一份
    var task: (title: String, task: [String: Any]) {
        var t: [String: Any] = ["type": "episode_item", "folder": folder, "entry": entryRow, "summary": summaryRow,
                                "paragraphs": paragraphs.map { [$0.0, $0.1] as [Any] }, "lang": lang]
        if let speech { t["speech"] = ["text": speech.text, "mode": speech.mode] }
        return ("存进笔记库：\(title)", t)
    }

    /// 要存的文件：文字记录、笔记，有整理稿再加上整理稿
    var files: [(name: String, text: String)] {
        var out = [(transcriptPath, transcriptText), (notePath, noteText)]
        if let speechText { out.append((speechPath, speechText)) }
        return out
    }

    /// 手机上先看的整理稿，跟 pipeline.render_speech_md 一样的结构（同步回来就是电脑写的那一份）
    var speechText: String? {
        guard let speech else { return nil }
        let isText = sourceType == "rss"
        let source = sourceType == "substack" ? "官方转写" : isText ? "RSS 文章正文" : subKind == "manual" ? "上传者提供的字幕" : "自动字幕"
        let zhSource = lang.split(separator: "-").first?.lowercased() == "zh"
        let mode = speech.mode == "zh" ? (zhSource ? "中文整理" : "中文翻译") : speech.mode == "original" ? "原文" : "原文/中文对照"
        var note = isText ? "原文为\(source)，中文为 AI 逐段翻译（\(mode)），可能存在翻译误差"
                          : "本文由 AI 基于\(source)整理为流畅演讲稿（\(mode)），已去除口语填充词并合理分段，力求保留原意但可能存在改写/翻译误差"
        note += "，请以视频原声及 [原始文字记录](../\(transcriptPath)) 为准"
        var lines = ["# \(title)", "", "- 所属节目：\((folder as NSString).lastPathComponent)", "- 链接：\(url)",
                     "- 时长：约 \(EpisodeRunner.duration(duration))",
                     "- 播出时间：\(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.suffix(2))", "- 说明：\(note)", "",
                     "## 单集小结", ""]
        if !summary.tldr.isEmpty { lines += ["**\(summary.tldr)**", ""] }
        lines += [EpisodeRunner.linkTimes(summary.body, url: url), "", "## 演讲稿", "", speech.text.trimmingCharacters(in: .whitespacesAndNewlines), ""]
        return lines.joined(separator: "\n")
    }

    private var hasTimes: Bool { paragraphs.contains { $0.0 > 0 } }

    /// 手机上先看的文字记录，跟 pipeline.render_transcript_md 一样的结构
    var transcriptText: String {
        var lines = ["# \(title)", "", "- 所属节目：\((folder as NSString).lastPathComponent)", "- 链接：\(url)",
                     "- 时长：约 \(EpisodeRunner.duration(duration))",
                     "- 播出时间：\(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.suffix(2))"]
        switch sourceType {
        case "substack": lines.append("- 文字记录来源：节目官方发布的正文内容（非语音识别）")
        case "rss": lines.append("- 文字记录来源：RSS/Atom 订阅源抓取的文章正文（非语音识别）")
        default:
            lines.append(subKind == "manual" ? "- 字幕来源：YouTube 上传者提供的字幕（\(lang)），已去重整理"
                                             : "- 字幕来源：YouTube 自动生成字幕（\(lang)），已去重整理，可能存在识别误差")
        }
        lines += ["- 单集笔记：[打开笔记](<../\(notePath)>)", "", "## 文字记录", ""]
        let sep = url.contains("?") ? "&" : "?"
        for (t, text) in paragraphs {
            if hasTimes { lines.append("**[\(EpisodeRunner.duration(Int(t)))](\(url)\(sep)t=\(Int(t))s)**  ") }
            lines += [text, ""]
        }
        return lines.joined(separator: "\n")
    }

    /// 手机上先看的笔记，跟 pipeline.render_episode_note 一样的结构
    var noteText: String {
        func list(_ v: [String]) -> String { "[" + v.map { "\"\($0.replacingOccurrences(of: "\"", with: "\\\""))\"" }.joined(separator: ", ") + "]" }
        var lines = ["---", "节目: \"[[\((folder as NSString).lastPathComponent)]]\"",
                     "播出: \(date.prefix(4))-\(date.dropFirst(4).prefix(2))-\(date.suffix(2))"]
        if duration > 0 { lines.append("时长: \"\(EpisodeRunner.duration(duration))\"") }
        if !summary.guests.isEmpty { lines.append("嘉宾: \(list(summary.guests))") }
        if !summary.topics.isEmpty { lines.append("话题: \(list(summary.topics))") }
        lines += ["链接: \"\(url)\"", "---", "", "# \(title)", ""]
        if !summary.tldr.isEmpty { lines += ["> \(summary.tldr)", ""] }
        let script = speech == nil ? "" : "[整理稿](<../\(speechPath)>) · "
        lines += [EpisodeRunner.linkTimes(summary.body, url: url), "", "---",
                  "\(script)[完整文字记录](<../\(transcriptPath)>) · [原链接](\(url))", ""]
        return lines.joined(separator: "\n")
    }
}

/// 手机上处理节目：有字幕或文字稿的跟电脑同样写小结（pipeline.build_series_episode_prompt，提示词一字不差）和
/// 整理稿（SpeechScript），没有的不处理，告诉用户。节目总结手机上不做。
enum EpisodeRunner {
    struct Summary: Sendable {
        var tldr = "", body = ""
        var guests: [String] = [], topics: [String] = []
    }

    enum Outcome: Sendable {
        case written(EpisodeItem)
        case noText(title: String, reason: String)     // 手机上处理不了
    }

    static let handedKey = "feeds.handed"
    static var handed: Set<String> { Set(UserDefaults.standard.stringArray(forKey: handedKey) ?? []) }

    static let roleContext = "你是播客/视频节目内容编辑。下面是该节目其中一期内容（访谈/对谈/独白等）的自动语音转写文字记录，可能包含少量识别错误，请结合上下文合理理解。"
    static let tiers: [(String, String)] = [
        ("3-5 条，每条一句话", "2-4"),
        ("6-9 条，每条 1-2 句话，写出具体的论据、数据或例子", "3-6"),
        ("10-15 条，每条 2-3 句话展开：具体说了什么、依据是什么（数据/案例/人名/产品名保留原文）、为什么重要；要覆盖全期所有主要话题，不要只写前半段", "4-8"),
    ]

    /// pipeline.strip_title_hashtags
    private static func hasChinese(_ s: String) -> Bool { s.unicodeScalars.contains { (0x4e00...0x9fff).contains($0.value) } }

    static func stripHashtags(_ title: String) -> String {
        let out = RX.sub(#"(?:\s+#[^\s#\d][^\s#]*)+\s*$"#, "", title).trimmingCharacters(in: .whitespacesAndNewlines)
        return out.isEmpty || RX.first(#"^(#\S+)$"#, out) != nil ? title : out
    }

    /// pipeline.format_timestamp
    static func duration(_ seconds: Int) -> String {
        let h = seconds / 3600, m = seconds % 3600 / 60, s = seconds % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }

    static func cjkShare(_ text: String) -> Double {
        var cjk = 0, all = 0
        for u in text.unicodeScalars {
            if (0x4e00...0x9fff).contains(u.value) { cjk += 1; all += 1 }
            else if (0x41...0x5a).contains(u.value) || (0x61...0x7a).contains(u.value) { all += 1 }
        }
        return all == 0 ? 0 : Double(cjk) / Double(all)
    }

    /// pipeline.series_points_tier
    static func tier(duration seconds: Int, text: String, length: String) -> Int {
        var minutes = Double(seconds) / 60
        if minutes == 0, !text.isEmpty {
            let latin = text.prefix(5000).filter(\.isASCII).count
            minutes = Double(text.count) / (latin > 2500 ? 900 : 250)
        }
        var t = minutes <= 35 ? 0 : minutes <= 75 ? 1 : 2
        if length == "short" { t -= 1 } else if length == "long" { t += 1 }
        return max(0, min(2, t))
    }

    /// pipeline.build_series_episode_prompt
    static func prompt(title: String, duration seconds: Int, paragraphs: [(Double, String)], length: String) -> String {
        let plain = paragraphs.map(\.1).joined(separator: "\n")
        let zh = cjkShare(String(plain.prefix(5000))) > 0.5
        let hasTimes = paragraphs.contains { $0.0 > 0 }
        let text = hasTimes ? paragraphs.map { "[\(duration(Int($0.0)))] \($0.1)" }.joined(separator: "\n") : plain
        let (points, segments) = tiers[tier(duration: seconds, text: plain, length: length)]
        let cap = FeedRunner.maxChars + (hasTimes ? 10 * paragraphs.count : 0)
        var p = "{role_context}\n\n单集标题：{title}\n时长：{duration}\n\n请用中文输出，严格按下面的格式（不要开场白和解释；各节标题照抄）：{zh_rule}\n\nTLDR: <一句话核心结论，不超过40字，不要markdown>\n嘉宾: <出场的主持人和嘉宾，逗号分隔，名字保留原文，看得出身份就在括号里注明；看不出来写\"无\">\n话题: <3-6 个关键话题，逗号分隔，每个 2-8 个字，可以直接当标签用>\n\n### 本期要点\n<{points}>\n\n### 分段\n<按内容自然分成 {segments} 段。每段一个四级标题 \"#### {segment_heading}\"，下面 2-4 条要点。>\n\n### 观点与分歧\n<多人对谈：谁持什么立场、分歧在哪里，每人一条；单人独白或访谈没有明显分歧，就写嘉宾的核心主张 2-3 条>\n\n### 值得记下的话\n<2-4 句原话，{quote_rule}，句末注明说话人{quote_time}；没有值得引用的就省略这一节（连标题一起）>\n\n### 预测与判断\n<节目里对未来的具体预测或可以日后验证的判断，每条注明是谁说的；没有就省略这一节（连标题一起）>\n\n文字记录{transcript_note}：\n\"\"\"\n{transcript}\n\"\"\"\n"
        let values: [(String, String)] = [
            ("{role_context}", roleContext), ("{title}", title), ("{duration}", duration(seconds)), ("{points}", points), ("{segments}", segments),
            ("{segment_heading}", hasTimes ? "[这一段开始的时间，照抄文字记录里的时间戳] 段落标题" : "段落标题"),
            ("{quote_time}", hasTimes ? "和时间 [m:ss]" : ""),
            ("{zh_rule}", zh ? "\n（这是中文节目：除了人名、公司名、产品名和业内通用的术语，不要夹英文词句。）" : ""),
            ("{quote_rule}", zh ? "用中文写，嘉宾说的是英文就只写中文译文" : "保留原文，非中文的在后面括号里给出中文翻译"),
            ("{transcript_note}", hasTimes ? "（每段开头方括号里是时间戳）" : ""),
        ]
        for (k, v) in values { p = p.replacingOccurrences(of: k, with: v) }
        return p.replacingOccurrences(of: "{transcript}", with: String(text.prefix(cap)))
    }

    /// pipeline.parse_series_summary
    static func parse(_ raw: String) -> Summary {
        var head = ["TLDR": "", "嘉宾": "", "话题": ""], body: [String] = [], inHead = true
        for line in raw.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: "\n") {
            let clean = String(line.trimmingCharacters(in: .whitespaces).drop { "-*#> ".contains($0) })
                .replacingOccurrences(of: "**", with: "").trimmingCharacters(in: .whitespaces)
            if inHead, let c = clean.firstIndex(where: { $0 == ":" || $0 == "：" }) {
                let key = clean[..<c].trimmingCharacters(in: .whitespaces).uppercased()
                if head[key] != nil {
                    head[key] = clean[clean.index(after: c)...].trimmingCharacters(in: .whitespaces)
                    continue
                }
            }
            if !line.trimmingCharacters(in: .whitespaces).isEmpty { inHead = false }
            body.append(line)
        }
        func split(_ v: String) -> [String] {
            var out: [String] = [], buf = "", depth = 0
            for ch in v {
                if "(（[【".contains(ch) { depth += 1 }
                if ")）]】".contains(ch), depth > 0 { depth -= 1 }
                if ",，、".contains(ch), depth == 0 { out.append(buf); buf = "" } else { buf.append(ch) }
            }
            out.append(buf)
            return out.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty && $0 != "无" }
        }
        let text = body.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        let first = body.first { !$0.trimmingCharacters(in: .whitespaces).isEmpty && !$0.trimmingCharacters(in: .whitespaces).hasPrefix("#") }
            .map { String($0.drop { "#-* ".contains($0) }).trimmingCharacters(in: .whitespaces) } ?? ""
        return Summary(tldr: head["TLDR"]!.isEmpty ? String(first.prefix(60)) : head["TLDR"]!,
                       body: text.isEmpty ? raw.trimmingCharacters(in: .whitespacesAndNewlines) : text,
                       guests: split(head["嘉宾"]!), topics: split(head["话题"]!))
    }

    /// pipeline.linkify_timestamps
    static func linkTimes(_ text: String, url: String) -> String {
        guard let id = YouTube.videoID(url) else { return text }
        var out = text
        for m in RX.re(#"\[(\d{1,2}:\d{2}(?::\d{2})?)\](?!\()"#).matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let whole = Range(m.range, in: out), let g = Range(m.range(at: 1), in: out) else { continue }
            let label = String(out[g])
            let p = label.split(separator: ":").compactMap { Int($0) }
            let sec = p.count == 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]
            out.replaceSubrange(whole, with: "[\(label)](https://www.youtube.com/watch?v=\(id)&t=\(sec)s)")
        }
        return out
    }

    // MARK: 列新内容

    /// Substack 节目的最近几期：id 是文章的 slug，跟电脑的处理记录一样
    static func substackEntries(_ sub: Subscription) async throws -> [Feed.Entry] {
        guard let c = URLComponents(string: sub.url), let host = c.host,
              let api = URL(string: "https://\(host)/api/v1/archive?sort=new&limit=12") else { throw SparkError("地址不对") }
        let (data, _) = try await URLSession.shared.data(from: api)
        guard let posts = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { throw SparkError("列不出文章") }
        let podcastOnly = c.path.contains("podcast")
        return posts.filter { !podcastOnly || $0["type"] as? String == "podcast" }.compactMap { p in
            guard let slug = p["slug"] as? String else { return nil }
            return Feed.Entry(id: slug, title: p["title"] as? String ?? slug, url: p["canonical_url"] as? String ?? "",
                              date: Feed.date(p["post_date"] as? String ?? ""), duration: Int(p["podcast_duration"] as? Double ?? 0))
        }
    }

    /// 频道把同一个视频传了两遍时只留一条（tracking.collapse_reuploads）：列表里挨着、标题和时长都一样。
    /// RSS 里没有时长，碰到挨着的同名视频才去查；查不到就都留着，不藏新内容
    static func collapseReuploads(_ entries: [Feed.Entry], done: Set<String>, skip: Set<String>) async -> [Feed.Entry] {
        func rank(_ e: Feed.Entry) -> Int { done.contains(e.id) ? 0 : skip.contains(e.id) ? 1 : 2 }
        func key(_ e: Feed.Entry) -> String {
            e.title.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased()
        }
        func length(_ e: Feed.Entry) async -> Int { (try? await YouTube.video(e.id))?.duration ?? 0 }
        var out: [Feed.Entry] = []
        var lengths: [String: Int] = [:]
        for e in entries {
            guard let last = out.last, !key(e).isEmpty, key(last) == key(e) else { out.append(e); continue }
            if lengths[last.id] == nil { lengths[last.id] = await length(last) }
            let seconds = await length(e)
            lengths[e.id] = seconds
            guard seconds > 0, lengths[last.id] == seconds else { out.append(e); continue }
            if rank(e) < rank(last) { out[out.count - 1] = e }
        }
        return out
    }

    /// 比已处理的最新一期还新的（tracking.newer_than_processed）：更早的往期不算新内容
    static func newEntries(_ sub: Subscription, lib: LocalNotes) async throws -> [Feed.Entry] {
        var entries: [Feed.Entry]
        switch sub.source_type {
        case "youtube":
            do {
                entries = try await Feed.fetch(try await YouTube.feedURL(sub.url)).compactMap { e in
                    guard !e.videoID.isEmpty else { return nil }
                    var e = e
                    e.id = e.videoID
                    e.title = stripHashtags(e.title)
                    e.url = "https://www.youtube.com/watch?v=\(e.videoID)"
                    return e
                }
            } catch let e where !(e is URLError) && (URLComponents(string: sub.url)?.queryItems ?? []).contains(where: { $0.name == "list" }) {
                // 有的播放列表没有订阅地址（订阅地址 404、是空的）：换列表接口。它不给日期，只看没处理过的前 15 条，逐条问日期。
                // 断网、超时（URLError）和频道地址的出错照原样报，不换接口，免得把真正的原因盖掉
                let done = FeedRunner.processed(lib, folder: sub.folder)
                entries = Array(try await YouTube.playlistEntries(sub.url).filter { !done.ids.contains($0.id) }.prefix(15))
                for i in entries.indices {
                    entries[i].date = await YouTube.publishDate(entries[i].id)
                    entries[i].title = stripHashtags(entries[i].title)
                }
            }
        case "substack": entries = try await substackEntries(sub)
        default: entries = try await Feed.fetch(URL(string: sub.url) ?? URL(fileURLWithPath: "/"))
        }
        let done = FeedRunner.processed(lib, folder: sub.folder)
        let skip = Set(sub.ignored_ids).union(handed)
        if sub.source_type == "youtube" { entries = await collapseReuploads(entries, done: done.ids, skip: skip) }
        // 处理失败过的照样列（tracking.find_new）：那是想要、没做成的，日期再早也可以重试
        return entries.filter { !done.ids.contains($0.id) && !done.urls.contains($0.url) && !skip.contains($0.id)
            && (done.newest.isEmpty || $0.date.isEmpty || $0.date >= done.newest || done.failed.contains($0.id)) }.sorted { $0.date > $1.date }
    }

    // MARK: 处理一期

    static func process(_ sub: Subscription, _ e: Feed.Entry, config: LLM.Config, length: String) async throws -> Outcome {
        var paragraphs: [(Double, String)] = []
        var title = e.title, duration = e.duration, date = e.date
        var subKind: String?, lang = "", sourceType: String?
        switch sub.source_type {
        case "youtube":
            let v = try await YouTube.video(e.id)
            title = stripHashtags(v.title.isEmpty ? e.title : v.title)
            // RSS 里是原标题，视频详情可能是 YouTube 译好的（pipeline.original_title）
            if hasChinese(e.title), !hasChinese(title) { title = e.title }
            guard let track = v.track else { return .noText(title: title, reason: "没有字幕") }
            paragraphs = try await YouTube.captions(track.url)
            guard !paragraphs.isEmpty else { return .noText(title: title, reason: "字幕是空的") }
            duration = v.duration
            (lang, subKind) = (track.lang, track.kind)
            if date.isEmpty { date = await YouTube.publishDate(e.id) }
        case "substack":
            guard let u = URLComponents(string: e.url), let host = u.host,
                  let api = URL(string: "https://\(host)/api/v1/posts/\(e.id)") else { return .noText(title: title, reason: "链接不对") }
            let (data, _) = try await URLSession.shared.data(from: api)
            let post = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let base = URL(string: e.url) ?? URL(fileURLWithPath: "/")
            paragraphs = FeedRunner.paragraphs(html: post?["body_html"] as? String ?? "", base: base).map { (0, $0) }
            guard paragraphs.reduce(0, { $0 + $1.1.count }) >= FeedRunner.minBody else { return .noText(title: title, reason: "没有公开的文字稿") }
            lang = post?["language"] as? String ?? "en"
            sourceType = "substack"
        default:
            guard let text = try? await FeedRunner.body(e), !text.isEmpty else { return .noText(title: title, reason: "没有文字稿") }
            paragraphs = text.map { (0, $0) }
            sourceType = "rss"
        }
        if date.isEmpty {
            let f = DateFormatter(); f.dateFormat = "yyyyMMdd"; f.timeZone = TimeZone(identifier: "UTC")
            date = f.string(from: .now)
        }
        let raw = try await LLM.complete(prompt(title: title, duration: duration, paragraphs: paragraphs, length: length),
                                         config: config, maxTokens: 8000, timeout: 600)
        var item = EpisodeItem(folder: sub.folder, id: e.id, title: title, url: e.url, duration: duration, date: date,
                               sourceType: sourceType, subKind: subKind, lang: lang, paragraphs: paragraphs, summary: parse(raw))
        do {
            // 整理稿写不成也不丢小结：这一期照样存，结束时告诉用户
            item.speech = try await SpeechScript.generate(title: title, paragraphs: paragraphs, lang: lang, sourceType: sourceType) { prompt, maxTokens, timeout in
                try await LLM.complete(prompt, config: config, maxTokens: maxTokens, timeout: timeout)
            }
        } catch {
            if Task.isCancelled || error is CancellationError { throw error }
            item.speechError = error.localizedDescription
        }
        return .written(item)
    }

    /// 处理链接：单个视频，或者播放列表、频道。folder 为空就用频道名新建一个节目文件夹
    static func linkTarget(_ link: String, folder: String) async throws -> (Subscription, [Feed.Entry]) {
        let hasList = URLComponents(string: link)?.queryItems?.contains { $0.name == "list" } ?? false
        var entries: [Feed.Entry]
        if let id = YouTube.videoID(link), !hasList {
            entries = [Feed.Entry(id: id, title: id, url: "https://www.youtube.com/watch?v=\(id)", videoID: id)]
        } else {
            entries = try await Feed.fetch(try await YouTube.feedURL(link)).filter { !$0.videoID.isEmpty }.map { e in
                var e = e
                e.id = e.videoID
                e.title = stripHashtags(e.title)
                e.url = "https://www.youtube.com/watch?v=\(e.videoID)"
                return e
            }
        }
        guard let first = entries.first else { throw SparkError("这个链接里没有找到视频") }
        let name = folder.isEmpty ? try await YouTube.video(first.id).author : folder
        let sub = Subscription(id: "link", name: name, url: link, kind: "podcast", source_type: "youtube", category: nil,
                               folder: "Spark/\(FeedRunner.sanitize(name))", ignored_ids: [])
        return (sub, entries)
    }
}
