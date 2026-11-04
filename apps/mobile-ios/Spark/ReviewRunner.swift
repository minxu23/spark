import Foundation

/// 手机上生成复盘：跟电脑端 core/review.py 同一套材料、提示词和文件名。
/// 写出来的文件放在 output/复盘/，下次同步时交给电脑存进笔记库。
enum ReviewRunner {
    static let maxReadItems = 60
    static let maxUnreadItems = 25
    static let maxTopicItems = 30
    static let maxOldItems = 25
    static let maxNewItems = 40

    /// 生成好的一份：相对笔记库的路径和全文
    struct Output: Sendable {
        let path: String
        let title: String
        let content: String
    }

    static let style = """
    写法要求：
    - 用平实的中文，像给自己写的读书笔记。不要开场白和收尾总结，不写「值得注意的是」「总的来说」「综上所述」「不难看出」这类套话，不用排比和感叹。
    - 每个判断后面括号注明出处：节目名和标题（材料里给的那个）。材料里没有的事实不要补，不确定的就说不确定。
    - 用 Markdown：二级标题分节，下面用短段落或列表。不要表格以外的花样格式，不要 emoji。
    """

    static func fmtDate(_ d: String) -> String {
        d.range(of: #"^\d{8}$"#, options: .regularExpression) != nil
            ? "\(d.prefix(4))-\(d.dropFirst(4).prefix(2))-\(d.suffix(2))" : "日期不详"
    }

    static func cut(_ text: String, _ n: Int) -> String {
        let t = RX.sub(#"\n{3,}"#, "\n\n", text).trimmingCharacters(in: .whitespacesAndNewlines)
        return t.count <= n ? t : String(t.prefix(n)).trimmingCharacters(in: .whitespaces) + "……"
    }

    static func source(_ it: LocalNotes.Item) -> String { "《\(it.title)》（\(it.show)，\(fmtDate(it.date))）" }

    static func safe(_ name: String) -> String {
        let s = String(RX.sub(#"[\\/:*?"<>|\s]+"#, "_", name).trimmingCharacters(in: CharacterSet(charactersIn: "_")).prefix(40))
        return s.isEmpty ? "未命名" : s
    }

    private static func llm(_ prompt: String, _ c: LLM.Config, maxTokens: Int = 8000) async throws -> String {
        try await LLM.complete(prompt, config: c, maxTokens: maxTokens, timeout: 1200)
    }

    private static func write(_ title: String, _ meta: [String], _ body: String, _ name: String) -> Output {
        let text = "# \(title)\n\n" + meta.map { "- \($0)" }.joined(separator: "\n") + "\n\n"
            + body.trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
        return Output(path: "output/复盘/\(name)", title: title, content: text)
    }

    private static func itemsForPaths(_ idx: LocalNotes.Index, _ paths: [String]) -> [LocalNotes.Item] {
        var seen = Set<String>(), out: [LocalNotes.Item] = []
        for p in paths {
            if let k = idx.byPath[p], !seen.contains(k), let it = idx.items[k] { seen.insert(k); out.append(it) }
        }
        return out
    }

    // MARK: 阅读复盘

    struct ReadEvent { let id: String; let at: Double; let source: String; let show: String; let entry: String; let path: String; let kind: String }

    /// 手机自己的阅读记录，加上数据线同步时电脑带过来的那份（里面有电脑上读的，也有之前交过去的手机记录，按 id 去重）
    static func readEvents(_ lib: LocalNotes, since: Double, until: Double) -> [ReadEvent] {
        var lines = ReadLog.lines()
        if let desk = lib.read(".spark-readlog.jsonl") { lines += desk.split(separator: "\n").map(String.init) }
        var seen = Set<String>(), out: [ReadEvent] = []
        for line in lines {
            guard let o = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  let at = o["at"] as? Double, at >= since, at < until else { continue }
            let id = o["id"] as? String ?? UUID().uuidString
            if !seen.insert(id).inserted { continue }
            out.append(ReadEvent(id: id, at: at, source: o["source"] as? String ?? "", show: o["show"] as? String ?? "",
                                 entry: o["entry"] as? String ?? "", path: o["path"] as? String ?? "", kind: o["kind"] as? String ?? ""))
        }
        return out.sorted { $0.at < $1.at }
    }

    struct Read { var item: LocalNotes.Item; var first: Double; var sources: Set<String>; var kinds: Set<String> }

    static func readingMaterial(_ lib: LocalNotes, _ idx: LocalNotes.Index, since: Double, until: Double)
        -> (reads: [Read], readTotal: Int, phone: Int, desktop: Int, unread: [LocalNotes.Item], unreadTotal: Int) {
        var reads: [String: Read] = [:]
        for ev in readEvents(lib, since: since, until: until) {
            let k = !ev.show.isEmpty && !ev.entry.isEmpty ? "\(ev.show)#\(ev.entry)" : (idx.byPath[ev.path] ?? "doc#\(ev.path)")
            var it = idx.items[k]
            if it == nil {
                guard ev.path.hasSuffix(".md") else { continue }
                let dir = (ev.path as NSString).deletingLastPathComponent
                it = LocalNotes.Item(show: dir.split(separator: "/").last.map(String.init) ?? "笔记库", folder: "", entry: ev.path,
                                     title: String((ev.path as NSString).lastPathComponent.dropLast(3)), date: "",
                                     tldr: "", body: "", note: "", updatedAt: "")
            }
            var r = reads[k] ?? Read(item: it!, first: ev.at, sources: [], kinds: [])
            r.sources.insert(ev.source)
            if !ev.kind.isEmpty { r.kinds.insert(ev.kind) }
            reads[k] = r
        }
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd"
        let lo = f.string(from: Date(timeIntervalSince1970: since)), hi = f.string(from: Date(timeIntervalSince1970: until))
        let unread = idx.items.filter { k, it in
            let d = it.date.isEmpty ? String(it.updatedAt.prefix(10)).replacingOccurrences(of: "-", with: "") : it.date
            return reads[k] == nil && lo <= d && d <= hi
        }.map(\.value).sorted { $0.date > $1.date }
        let list = reads.values.sorted { $0.first < $1.first }
        return (Array(list.prefix(maxReadItems)), list.count, list.filter { $0.sources.contains("phone") }.count,
                list.filter { $0.sources.contains("desktop") }.count, Array(unread.prefix(maxUnreadItems)), unread.count)
    }

    static func readingPrompt(_ lib: LocalNotes, _ m: (reads: [Read], readTotal: Int, phone: Int, desktop: Int, unread: [LocalNotes.Item], unreadTotal: Int),
                              period: String) -> String {
        let names = ["note": "小结", "speech": "整理稿", "transcript": "文字记录", "article": "原文", "doc": "全文"]
        let blocks = m.reads.enumerated().map { i, r -> String in
            let it = r.item
            let hl = lib.highlights(it.note)
            let how = r.kinds.map { names[$0] ?? $0 }.sorted().joined(separator: "、")
            var part = ["[\(i + 1)] \(source(it))　读了：\(how.isEmpty ? "打开过" : how)"]
            if !it.tldr.isEmpty { part.append("一句话：\(it.tldr)") }
            if !hl.isEmpty { part.append("我的高亮：\n" + hl.prefix(12).map { "- \($0)" }.joined(separator: "\n")) }
            if !it.body.isEmpty { part.append("小结摘录：\n" + cut(it.body, 900)) }
            return part.joined(separator: "\n")
        }
        let unread = m.unread.map { "- \(source($0))：\($0.tldr.isEmpty ? "（没有一句话总结）" : $0.tldr)" }.joined(separator: "\n")
        return """
        你在帮我做一次阅读复盘，时间范围是\(period)。下面是这段时间我读过的内容（按第一次打开的先后排），以及这段时间新进来但我还没读的。

        请写这几节：
        ## 这段时间读了什么
        按话题归成几组（不按节目），每组一两句话说清楚读的是什么，列出对应的篇目。
        ## 主要收获
        挑出最有分量的 5 到 8 条认识或事实，每条一两句，注明出处。我划过高亮的地方优先。
        ## 反复出现的话题和分歧
        哪些话题在不同节目里反复被提到，各方说法一致还是有分歧。
        ## 值得补看的
        从「没读的」里挑 3 到 6 篇跟我这段时间关注的话题最相关的，说明为什么值得看。没读的为空就写「这段时间新进来的都读过了」。

        \(style)

        ===== 读过的（共 \(m.readTotal) 篇，下面列了 \(m.reads.count) 篇）=====
        \(blocks.isEmpty ? "（这段时间没有阅读记录）" : blocks.joined(separator: "\n"))

        ===== 新进来但没读的（共 \(m.unreadTotal) 篇，下面列了 \(m.unread.count) 篇）=====
        \(unread.isEmpty ? "（无）" : unread)

        """
    }

    // MARK: 主题来龙去脉

    static func topicPrompt(_ topic: String, _ items: [LocalNotes.Item]) -> String {
        let sorted = items.sorted { ($0.date.isEmpty ? "99999999" : $0.date) < ($1.date.isEmpty ? "99999999" : $1.date) }
        let blocks = sorted.enumerated().map { i, it -> String in
            var part = ["[\(i + 1)] \(source(it))"]
            if !it.tldr.isEmpty { part.append("一句话：\(it.tldr)") }
            if !it.body.isEmpty { part.append(cut(it.body, 1000)) }
            return part.joined(separator: "\n")
        }
        return """
        下面是我笔记库里跟「\(topic)」相关的内容，按时间先后排列。请复盘这个主题的来龙去脉。

        请写这几节：
        ## 时间线
        按时间列出关键节点：谁在什么时候说了什么、发生了什么，一条一行，注明出处。
        ## 说法是怎么变的
        早期的主流看法是什么，后来哪里变了、因为什么变的。
        ## 分歧和转折点
        各方在哪些问题上意见不同；哪些事件或数据改变了讨论的方向。
        ## 现在的共识和悬而未决的问题
        到最近这批材料为止，哪些已经比较确定，哪些还在争。

        \(style)

        ===== 材料（共 \(items.count) 篇）=====
        \(blocks.map { "\n" + $0 }.joined(separator: "\n"))

        """
    }

    // MARK: 观点检验

    struct Claim { let claim: String; let who: String; let horizon: String; let source: LocalNotes.Item }

    static func extractClaims(_ old: [LocalNotes.Item], _ topic: String, _ c: LLM.Config, limit: Int = 30) async throws -> [Claim] {
        let blocks = old.enumerated().map { i, it in "[\(i + 1)] \(source(it))\n\(cut(it.body.isEmpty ? it.tldr : it.body, 1500))" }
            .joined(separator: "\n\n")
        let prompt = """
        下面是我笔记库里较早的一批节目和文章的小结。请从中摘出嘉宾或作者对未来的判断和预测：
        有明确指向、过一段时间能检验的（比如「X 会在两年内发生」「Y 路线会胜出」「Z 公司会如何」），不要泛泛的观点和事实陈述。

        每条输出一行 JSON，字段：
        {"claim": "判断内容（一句话，保留原意）", "who": "谁说的", "source": 材料编号（数字）, "horizon": "说的时间范围，没说就写空串"}
        只输出 JSON 行，不要别的文字。最多 \(limit) 条，挑最具体、最有分量的。
        \(topic.isEmpty ? "" : "只摘跟「\(topic)」有关的。\n")
        ===== 材料 =====
        \(blocks)

        """
        let raw = try await llm(prompt, c, maxTokens: 6000)
        var out: [Claim] = []
        for line in raw.split(separator: "\n") {
            let l = line.trimmingCharacters(in: CharacterSet(charactersIn: " \t,"))
            guard l.hasPrefix("{"), let o = try? JSONSerialization.jsonObject(with: Data(l.utf8)) as? [String: Any] else { continue }
            let src = (o["source"] as? Int) ?? Int("\(o["source"] ?? "")") ?? 0
            guard src >= 1, src <= old.count, let claim = o["claim"].map({ "\($0)" }), !claim.isEmpty else { continue }
            out.append(Claim(claim: claim, who: o["who"].map { "\($0)" } ?? "", horizon: o["horizon"].map { "\($0)" } ?? "", source: old[src - 1]))
        }
        return Array(out.prefix(limit))
    }

    static func claimsPrompt(_ claims: [Claim], _ new: [LocalNotes.Item], _ topic: String) -> String {
        let cl = claims.enumerated().map { i, c in
            "[C\(i + 1)] \(c.claim)（\(c.who.isEmpty ? "说话人不详" : c.who)，\(source(c.source))\(c.horizon.isEmpty ? "" : "，时间范围：" + c.horizon)）"
        }.joined(separator: "\n")
        let ev = new.enumerated().map { i, it in "[E\(i + 1)] \(source(it))\n\(it.tldr)\n\(cut(it.body, 700))" }.joined(separator: "\n\n")
        return """
        下面左边是较早时候嘉宾和作者做出的判断与预测，右边是之后进来的节目和文章的小结。\(topic.isEmpty ? "" : "主题是「\(topic)」。")
        请逐条检验这些判断。

        请写这几节：
        ## 检验结果
        一个 Markdown 表格，列：判断（含谁说的、出处）｜结论｜依据。结论只能是：应验、部分应验、被推翻、尚无定论。
        依据写清楚是后来哪篇材料（出处）说了什么；后来的材料里没提到的就写「尚无定论」，不要凭常识判断。
        ## 几点观察
        谁的判断更靠谱、哪类预测容易落空、有没有大家都没料到的变化。三到五条。

        \(style)

        ===== 较早的判断（共 \(claims.count) 条）=====
        \(cl)

        ===== 之后进来的材料（共 \(new.count) 篇）=====
        \(ev.isEmpty ? "（这段时间之后没有相关的新材料）" : ev)

        """
    }

    // MARK: 生成

    // MARK: 剪藏复盘

    static let maxClipItems = 60
    static let maxRelatedItems = 20

    struct ClipItem { let meta: ClipMeta; let excerpt: String }

    /// 剪藏里的一篇：文件头 + 摘要（没有摘要就取正文开头）
    static func clipItems(_ lib: LocalNotes) -> [ClipItem] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: lib.url(Clips.folder).path)) ?? []
        return names.map { "\(Clips.folder)/\($0)" }.filter(Clips.isClip).compactMap { path in
            guard let text = lib.read(path) else { return nil }
            let meta = Clips.meta(path: path, text: text)
            var excerpt = RX.first(#"(?s)\n## 摘要\n(.*?)(?=\n## |\z)"#, text) ?? ""
            if excerpt.isEmpty { excerpt = RX.first(#"(?s)\n## 正文\n(.*)"#, text) ?? "" }
            excerpt = RX.sub(#"!\[[^\]]*\]\([^)]*\)"#, "", excerpt)
            return ClipItem(meta: meta, excerpt: cut(excerpt, 600))
        }
    }

    static func clipsPrompt(period: String, clips: [ClipItem], earlierTags: [(String, Int)], related: [LocalNotes.Item]) -> String {
        let blocks = clips.enumerated().map { i, c in
            var part = ["[\(i + 1)] 《\(c.meta.title)》（\(c.meta.source.isEmpty ? "来源不详" : c.meta.source)，\(c.meta.clipped) 存）"]
            if !c.meta.tags.isEmpty { part.append("标签：\(c.meta.tags.joined(separator: "、"))") }
            if !c.meta.note.isEmpty { part.append("我存它时的备注：\(c.meta.note)") }
            if !c.excerpt.isEmpty { part.append(c.excerpt) }
            return part.joined(separator: "\n")
        }.joined(separator: "\n\n")
        let earlier = earlierTags.isEmpty ? "（这段时间以前没有剪藏，或者都没打标签）"
            : earlierTags.prefix(30).map { "\($0.0)（\($0.1) 篇）" }.joined(separator: "、")
        let rel = related.map { "- \(source($0))：\($0.tldr.isEmpty ? "（没有一句话总结）" : $0.tldr)" }.joined(separator: "\n")
        return """
        你在帮我做一次剪藏复盘，时间范围是\(period)。剪藏是我在手机上看到觉得值得留下、主动存下来的文章，最能说明我这段时间在关注什么。

        请写这几节：
        ## 这段时间在关注什么
        把这些剪藏按话题归成 3 到 6 组（不按来源），每组一两句话说清我在追的是什么问题，列出对应的篇目。
        ## 各个话题的要点
        每个话题挑最有分量的几条事实或观点，每条一两句，注明出处。我写了备注的优先。
        ## 关注点的变化
        跟这段时间以前的剪藏（下面给了以前各标签的篇数）比，哪些是新冒出来的关注点，哪些在持续，哪些淡了。以前没有剪藏就写「这是第一次剪藏复盘，还没有可比的」。
        ## 值得接着看的
        从「笔记库里相关的内容」里挑 3 到 6 篇跟这些话题最相关的，说明为什么值得看。列表为空就写「笔记库里暂时没有相关的新内容」。
        ## 可以继续追的问题
        3 到 5 个值得接着跟的问题，每个一句话，写成以后能用来检索或订阅的样子。

        \(style)

        ===== 这段时间的剪藏（共 \(clips.count) 篇）=====
        \(blocks)

        ===== 这段时间以前的剪藏，各标签的篇数 =====
        \(earlier)

        ===== 笔记库里相关的内容（这段时间进来的，按标签匹配，共 \(related.count) 篇）=====
        \(rel.isEmpty ? "（无）" : rel)

        """
    }

    // MARK: 剪藏综合小结

    static let maxPicked = 20
    static let pickedBudget = 60_000      // 所有文章加起来给模型多少字

    struct PickedItem { let meta: ClipMeta; let summary: String; let body: String }

    /// 挑出来的几篇剪藏：摘要和正文（去掉图片），正文按篇数平分字数
    static func pickedItems(_ lib: LocalNotes, _ paths: [String]) -> [PickedItem] {
        let each = max(1500, min(6000, pickedBudget / max(paths.count, 1)))
        var seen = Set<String>()
        return paths.filter { Clips.isClip($0) && seen.insert($0).inserted }.compactMap { path in
            guard let text = lib.read(path) else { return nil }
            let noImg = { (s: String) in RX.sub(#"!\[[^\]]*\]\([^)]*\)"#, "", s) }
            let summary = noImg(RX.first(#"(?s)\n## 摘要\n(.*?)(?=\n## |\z)"#, text) ?? "")
            let body = noImg(RX.first(#"(?s)\n## 正文\n(.*)"#, text) ?? "")
            return PickedItem(meta: Clips.meta(path: path, text: text), summary: cut(summary, 1200), body: cut(body, each))
        }.sorted { ($0.meta.clipped, $0.meta.path) < ($1.meta.clipped, $1.meta.path) }
    }

    static func pickedPrompt(_ items: [PickedItem]) -> String {
        let blocks = items.enumerated().map { i, c in
            var part = ["[\(i + 1)] 《\(c.meta.title)》（\(c.meta.source.isEmpty ? "来源不详" : c.meta.source)，\(c.meta.clipped) 存）"]
            if !c.meta.tags.isEmpty { part.append("标签：\(c.meta.tags.joined(separator: "、"))") }
            if !c.meta.note.isEmpty { part.append("我存它时的备注：\(c.meta.note)") }
            if !c.summary.isEmpty { part.append("摘要：\n\(c.summary)") }
            if !c.body.isEmpty { part.append("正文：\n\(c.body)") }
            return part.joined(separator: "\n")
        }.joined(separator: "\n\n")
        return """
        下面是我从剪藏里挑出来的 \(items.count) 篇文章，想放在一起看。请写一份综合小结。

        请写这几节：
        ## 这几篇在讲什么
        两三句话说清这组文章共同围绕的问题。如果其实是几个不相干的话题，就直说，并分组说明。
        ## 各篇要点
        每篇一个小标题（用文章标题），下面两到四条要点。我写了备注的，结合备注说。
        ## 共识和分歧
        几篇之间说法一致的地方、互相矛盾或角度不同的地方，各自的依据是什么，注明出处。
        ## 放在一起看出来的
        单看一篇看不出、合起来才明显的：互相印证的趋势、互补的信息、还缺哪一块。三到五条。
        ## 可以继续追的问题
        3 到 5 个值得接着跟的问题，每个一句话。

        \(style)

        ===== 文章（共 \(items.count) 篇，按存的先后排）=====
        \(blocks)

        """
    }

    static func pickedTitle(_ firstTitle: String, count: Int) -> String {
        let t = firstTitle.count > 24 ? String(firstTitle.prefix(24)) + "…" : firstTitle
        return "剪藏综合小结：《\(t)》等 \(count) 篇"
    }

    private static func search(_ lib: LocalNotes, _ topic: String, _ c: LLM.Config, _ progress: @escaping TopicSearch.Progress) async throws -> [String] {
        progress("按主题检索笔记")
        let r = try await TopicSearch.find(lib, topic: topic, config: c, progress: progress)
        return r.candidates.filter { $0.relevance >= 2 }.map(\.path)
    }

    static func run(kind: String, days: Int, months: Int, topic rawTopic: String, paths: [String] = [], lib: LocalNotes, config c: LLM.Config,
                    progress: @escaping TopicSearch.Progress) async throws -> Output {
        progress("读笔记库的处理记录")
        let idx = lib.buildIndex()
        let now = Date()
        let dayF = DateFormatter(); dayF.dateFormat = "yyyyMMdd"
        let today = dayF.string(from: now)
        let stampF = DateFormatter(); stampF.dateFormat = "yyyy-MM-dd HH:mm"
        let stamp = stampF.string(from: now)
        let topic = rawTopic.trimmingCharacters(in: .whitespacesAndNewlines)

        switch kind {
        case "reading":
            guard (1...366).contains(days) else { throw SparkError("天数应该在 1 到 366 之间") }
            let since = now.addingTimeInterval(-Double(days) * 86400)
            let mat = readingMaterial(lib, idx, since: since.timeIntervalSince1970, until: now.timeIntervalSince1970)
            if mat.readTotal == 0 && mat.unreadTotal == 0 { throw SparkError("近 \(days) 天既没有阅读记录，也没有新进来的内容") }
            let cal = Calendar.current
            let period = "\(cal.component(.month, from: since))月\(cal.component(.day, from: since))日到\(cal.component(.month, from: now))月\(cal.component(.day, from: now))日（近 \(days) 天）"
            progress("写阅读复盘")
            let body = try await llm(readingPrompt(lib, mat, period: period), c)
            var meta = ["生成时间：\(stamp)（在手机上生成，\(c.label)）",
                        "读了 \(mat.readTotal) 篇（电脑上 \(mat.desktop)，手机上 \(mat.phone)）；这段时间新进来但没读的 \(mat.unreadTotal) 篇"]
            if lib.read(".spark-readlog.jsonl") == nil { meta.append("电脑上的阅读记录还没通过数据线同步过来，这次只算了手机上读的") }
            return write("阅读复盘：\(period)", meta, body, "复盘_阅读_\(dayF.string(from: since))-\(today).md")

        case "topic":
            guard !topic.isEmpty else { throw SparkError("主题来龙去脉要填一个主题") }
            let items = Array(itemsForPaths(idx, try await search(lib, topic, c, progress)).prefix(maxTopicItems))
            if items.count < 3 { throw SparkError("笔记库里跟「\(topic)」相关、有处理记录的内容只有 \(items.count) 篇，不够复盘") }
            progress("用 \(items.count) 篇写来龙去脉")
            let body = try await llm(topicPrompt(topic, items), c)
            let dates = items.map(\.date).filter { !$0.isEmpty }.sorted()
            let span = dates.isEmpty ? "日期不详" : "\(fmtDate(dates.first!)) 到 \(fmtDate(dates.last!))"
            return write("主题来龙去脉：\(topic)", ["生成时间：\(stamp)（在手机上生成，\(c.label)）", "材料 \(items.count) 篇，时间跨度 \(span)"],
                         body, "复盘_主题_\(safe(topic))_\(today).md")

        case "picked":
            let items = pickedItems(lib, paths)
            if items.count < 2 { throw SparkError("综合小结至少要选 2 篇手机上有的剪藏，现在只找到 \(items.count) 篇") }
            if items.count > maxPicked { throw SparkError("一次最多选 \(maxPicked) 篇") }
            progress("用 \(items.count) 篇剪藏写综合小结")
            let body = try await llm(pickedPrompt(items), c)
            let names = items.map { "《\($0.meta.title)》" }.joined(separator: "、")
            return write(pickedTitle(items[0].meta.title, count: items.count),
                         ["生成时间：\(stamp)（在手机上生成，\(c.label)）", "选了 \(items.count) 篇剪藏：\(names)"],
                         body, "复盘_综合_\(safe(items[0].meta.title))等\(items.count)篇_\(stampF.string(from: now).filter(\.isNumber)).md")

        case "clips":
            guard (1...366).contains(days) else { throw SparkError("天数应该在 1 到 366 之间") }
            let since = now.addingTimeInterval(-Double(days) * 86400)
            let lo = ISO8601DateFormatter.day.string(from: since)
            let all = clipItems(lib)
            let inPeriod = all.filter { $0.meta.clipped >= lo }.sorted { $0.meta.clipped < $1.meta.clipped }
            if inPeriod.isEmpty { throw SparkError("近 \(days) 天没有剪藏") }
            var earlier: [String: Int] = [:]
            for c in all where c.meta.clipped < lo { for t in Set(c.meta.tags) { earlier[t, default: 0] += 1 } }
            // 笔记库里这段时间进来、标题或一句话里带着这些标签的
            let tags = Set(inPeriod.flatMap(\.meta.tags)).filter { $0.count >= 2 }
            let loCompact = dayF.string(from: since)
            let related = idx.items.values.filter { it in
                (it.date.isEmpty ? String(it.updatedAt.prefix(10)).replacingOccurrences(of: "-", with: "") : it.date) >= loCompact
                    && tags.contains { t in it.title.localizedCaseInsensitiveContains(t) || it.tldr.localizedCaseInsensitiveContains(t) }
            }.sorted { $0.date > $1.date }.prefix(maxRelatedItems)
            let cal = Calendar.current
            let period = "\(cal.component(.month, from: since))月\(cal.component(.day, from: since))日到\(cal.component(.month, from: now))月\(cal.component(.day, from: now))日（近 \(days) 天）"
            progress("用 \(inPeriod.count) 篇剪藏写复盘")
            let body = try await llm(clipsPrompt(period: period, clips: Array(inPeriod.suffix(maxClipItems)),
                                                 earlierTags: earlier.sorted { ($0.value, $1.key) > ($1.value, $0.key) }.map { ($0.key, $0.value) },
                                                 related: Array(related)), c)
            return write("剪藏复盘：\(period)", ["生成时间：\(stamp)（在手机上生成，\(c.label)）",
                                              "这段时间存了 \(inPeriod.count) 篇，更早的 \(all.count - inPeriod.count) 篇；笔记库里相关的新内容 \(related.count) 篇"],
                         body, "复盘_剪藏_\(loCompact)-\(today).md")

        default:   // claims
            guard (1...60).contains(months) else { throw SparkError("月数应该在 1 到 60 之间") }
            let cutoff = dayF.string(from: now.addingTimeInterval(-Double(months) * 30 * 86400))
            let pool = topic.isEmpty ? Array(idx.items.values) : itemsForPaths(idx, try await search(lib, topic, c, progress))
            let dated = pool.filter { !$0.date.isEmpty && !$0.body.isEmpty }
            let old = Array(dated.filter { $0.date < cutoff }.sorted { $0.date > $1.date }.prefix(maxOldItems))
            let new = Array(dated.filter { $0.date >= cutoff }.sorted { $0.date > $1.date }.prefix(maxNewItems))
            if old.isEmpty { throw SparkError("\(months) 个月以前\(topic.isEmpty ? "" : "跟「\(topic)」相关")的内容一篇都没有，没有可检验的判断") }
            progress("从 \(old.count) 篇较早的内容里摘判断")
            let claims = try await extractClaims(old, topic, c)
            if claims.isEmpty { throw SparkError("较早的内容里没有摘出能检验的判断或预测") }
            progress("对照之后的 \(new.count) 篇检验 \(claims.count) 条判断")
            let body = try await llm(claimsPrompt(claims, new, topic), c)
            let title = topic.isEmpty ? "观点检验：\(months) 个月前的判断" : "观点检验：\(topic)"
            return write(title, ["生成时间：\(stamp)（在手机上生成，\(c.label)）",
                                 "判断取自 \(fmtDate(cutoff)) 以前的 \(old.count) 篇，对照之后的 \(new.count) 篇，共 \(claims.count) 条"],
                         body, "复盘_观点检验_\(topic.isEmpty ? "\(months)个月" : safe(topic))_\(today).md")
        }
    }
}
