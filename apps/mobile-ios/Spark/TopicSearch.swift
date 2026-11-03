import Foundation

/// 主题检索的手机版：跟电脑端 apps/notes2insight/search.py 同一套做法和提示词。
///   1. 看主题词在库里怎么用，一次模型调用判断含义、扩成带权重的检索词
///   2. 本地全文打分（BM25 变体，中文子串计数，英文带词边界）
///   3. 打分靠前的 60 篇交给模型判相关度
/// 主题有几种含义、电脑上会请人选的时候，手机上按第一种接着检索（跟手机交给电脑的任务一样）。
enum TopicSearch {
    static let readCap = 200_000
    static let defaultCandidates = 60
    static let bm25K1 = 1.4
    static let bm25B = 0.72
    static let contextSnippets = 14
    static let contextWidth = 160
    static let recentNoteDays = 90
    static let recentWeight = 3.0
    static let autoShare = 0.8
    static let screenBatch = 15
    static let minGood = 3

    static let expandPrompt = """
    你是检索助手。用户要在自己的中英文混排技术笔记库（科技播客整理稿、行业文章、论文、大会记录）里，找出与下面主题相关的笔记。

    主题：{topic}
    {contexts}{activity}
    ## 第一步：判断主题指什么
    - 主题是缩写或多义词时，列出它在这个库里实际出现的各种含义（依据上面的原文片段）；
      用户近期关注明显指向某个片段里没有的含义时，也把它列出来；这个词广为人知的其他含义
      （比如 RSI 在金融里是相对强弱指标）即使片段里没有，也列出来，片段编号写 -，方便用户改选。
      真没有歧义的（比如"推理成本"）就只列一个。
    - 最多 4 个含义，用 A、B、C、D 编号，每个一行：
      `含义|编号|简短名称（不超过 12 个字）|一句话说明|属于这个含义的片段编号，逗号分隔，没有就写 -`
    - 每个片段都归到某一个含义里。
    - 再写一行 `近期|编号`：用户近期的检索、报告、订阅明显指向哪个含义；看不出来就写 `近期|-`。
    - 示例（行首的「含义」「近期」不能省）：
      含义|A|混合专家模型|大模型里的 Mixture of Experts 架构|1,2,4
      含义|B|人名|片段里人名的一部分|3
      近期|A

    ## 第二步：给每个含义写检索词（用于全文关键词匹配）
    - 一行一个，格式严格为 `编号|权重|检索词`，权重只能是 3、2、1。
      3 = 这个含义的核心概念（命中基本就相关）；2 = 强相关术语、代表性公司/产品/技术名；1 = 弱相关线索词。
    - 中英文都要给：同一个概念的中文说法和英文说法**各占一行**，不要写在同一行里。
    - 给**具体**的词：公司名、产品名、芯片名、模型名、协议名、指标名。不要给"技术""趋势""发展""影响"这类泛词。
    - 不要给单个汉字，不要给长句：中文每个词不超过 12 个字，英文不超过 4 个单词。
    - 缩写要同时给出中英文全称和常见的其他写法。
    - 片段只用来判断含义：片段里顺带出现的人名、节目名、公司名不要当检索词，除非它本身就是这个含义公认的代表。
    - 每个含义 15-25 个词。

    只输出上面这些行，不要解释。

    """

    static let screenPrompt = """
    你是检索筛选助手。当前主题是：

    **{topic}**{meaning}

    下面是若干候选笔记的标题与全文命中片段。请逐条判断它与这个主题的相关度。

    相关度定义：
    5 = 核心材料，整篇都在讲这个主题
    4 = 高度相关，有大段直接讨论
    3 = 相关，有明确且可用的内容
    2 = 部分相关，只是顺带提到
    1 = 边缘，只有词面命中
    0 = 无关（命中的是同形词或完全不同的语境）

    输出格式严格为每行一条：`编号|相关度|不超过25字的理由`
    不要输出表头、解释或多余空行。编号必须与输入一致，每个候选都要有一行。

    {items}

    """

    struct Candidate: Sendable {
        var path: String
        var title: String
        var date: String
        var chars: Int
        var score: Double = 0
        var relevance = -1
        var reason = ""
        var snippet = ""
    }

    struct Sense: Sendable {
        var key: String
        var label: String
        var meaning: String
        var snippets: [Int]
        var estRecent = 0
        var estOlder = 0
        var terms: [(String, Int)] = []
    }

    struct Result: Sendable {
        var topic: String
        var meaning: String
        var label: String
        var terms: [(String, Int)]
        var scanned: Int
        var matched: Int
        var candidates: [Candidate]
        var insufficient: String
        var askedButPicked: String     // 主题有歧义时按第一种含义检索了：说明一句
    }

    typealias Progress = @Sendable (String) -> Void

    // MARK: 第一步：关键词扩展

    static func topicTokens(_ topic: String) -> [String] {
        RX.split(#"[\s,，、;；/]+"#, topic).map { $0.trimmingCharacters(in: .whitespaces) }.filter { $0.count > 1 }
    }

    static func isASCII(_ t: String) -> Bool { t.unicodeScalars.allSatisfy { $0.value < 128 } }

    private static func topicPatterns(_ topic: String) -> [NSRegularExpression] {
        let toks = topicTokens(topic)
        return (toks.isEmpty ? [topic.trimmingCharacters(in: .whitespaces)] : toks).compactMap { t in
            let e = NSRegularExpression.escapedPattern(for: t)
            return try? NSRegularExpression(pattern: isASCII(t) ? "(?<![A-Za-z0-9])\(e)(?![A-Za-z0-9])" : e, options: .caseInsensitive)
        }
    }

    static func noteDay(_ n: LocalNotes.Note) -> String {
        if !n.date.isEmpty { return n.date }
        return n.mtime == .distantPast ? "" : ISO8601DateFormatter.day.string(from: n.mtime)
    }

    struct Sample { var text: String; var date: String; var folder: String; var recent: Bool }
    struct Contexts { var samples: [Sample]; var recentHits: Int; var olderHits: Int }

    /// 主题词原样出现的笔记，分近期 / 较早两层各抽一半片段（同 collect_contexts）
    static func collectContexts(_ lib: LocalNotes, _ topic: String, _ notes: [LocalNotes.Note]) -> Contexts {
        let pats = topicPatterns(topic)
        let cut = ISO8601DateFormatter.day.string(from: Date().addingTimeInterval(-Double(recentNoteDays) * 86400))
        var recent: [(Sample, String)] = [], older: [(Sample, String)] = []
        for n in notes {
            guard let text = lib.read(n.path, maxChars: readCap) else { continue }
            let ns = text as NSString
            var hit: NSRange?
            for p in pats {
                if let m = p.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)) { hit = m.range; break }
            }
            guard let m = hit else { continue }
            let day = noteDay(n)
            let start = max(0, m.location - contextWidth / 2)
            let piece = ns.substring(with: NSRange(location: start, length: min(contextWidth, ns.length - start)))
            let s = Sample(text: RX.sub(#"\s+"#, " ", piece).trimmingCharacters(in: .whitespaces), date: day, folder: n.folder, recent: false)
            let key = RX.sub(#"\s+"#, "", n.title).lowercased()
            if !day.isEmpty && day >= cut { recent.append((s, key)) } else { older.append((s, key)) }
        }

        func sample(_ rows: [(Sample, String)], _ k: Int) -> [Sample] {
            var groups: [String: [(Sample, String)]] = [:], order: [String] = []
            for r in rows.sorted(by: { $0.0.date > $1.0.date }) {
                // 按节目 / 来源分组：Spark/Dwarkesh/notes 和 Spark/Dwarkesh/speech 算同一个来源
                let src = r.0.folder.split(separator: "/").prefix(2).joined(separator: "/")
                if groups[src] == nil { order.append(src) }
                groups[src, default: []].append(r)
            }
            var out: [Sample] = [], seen = Set<String>()
            while out.count < k, groups.values.contains(where: { !$0.isEmpty }) {
                for src in order {
                    while var g = groups[src], !g.isEmpty {
                        let r = g.removeFirst()
                        groups[src] = g
                        if !seen.contains(r.1) { seen.insert(r.1); out.append(r.0); break }
                    }
                    if out.count >= k { break }
                }
            }
            return out
        }

        let half = max(1, contextSnippets / 2)
        var rec = sample(recent, half)
        let old = sample(older, contextSnippets - rec.count)
        if old.count < contextSnippets - half { rec = sample(recent, contextSnippets - old.count) }
        let samples = rec.map { var s = $0; s.recent = true; return s } + old
        return Contexts(samples: samples, recentHits: recent.count, olderHits: older.count)
    }

    /// 认出含义行，返回去掉含义行之后的文本（同 _take_sense_lines）
    private static func takeSenseLines(_ raw: String, _ senses: inout [String: Sense], _ nSamples: Int) -> String {
        var rest: [String] = []
        for line in raw.split(separator: "\n", omittingEmptySubsequences: false).map(String.init) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            var parts = RX.split(#"[|｜]"#, trimmed).map { $0.trimmingCharacters(in: .whitespaces) }
            if parts.first == "含义" { parts.removeFirst() }
            let isSense = parts.count >= 4 && parts[0].range(of: #"^[A-D]$"#, options: .regularExpression) != nil
                && (parts.last ?? "").range(of: #"^[\d,，、\s\-–—无]*$"#, options: .regularExpression) != nil
                && !trimmed.hasPrefix("近期")
            guard isSense else { rest.append(line); continue }
            let key = parts[0]
            var fields = Array(parts[1..<(parts.count - 1)])
            if fields.count >= 3, fields[0].allSatisfy(\.isNumber) { fields.removeFirst() }
            let label = fields.first ?? ""
            if label.isEmpty || label.allSatisfy(\.isNumber) { rest.append(line); continue }
            if senses[key] != nil { continue }
            let ids = RX.all(#"\d+"#, parts.last ?? "").compactMap { Int($0[0]) }.filter { 1 <= $0 && $0 <= nSamples }
            senses[key] = Sense(key: key, label: String(label.prefix(30)), meaning: String(fields.dropFirst().joined(separator: "｜").prefix(200)), snippets: ids)
        }
        return rest.joined(separator: "\n")
    }

    private static func cleanTerms(_ pairs: [(String, Int)], _ topic: String) -> [(String, Int)] {
        var best: [String: Int] = [:]
        for (t, w) in pairs + topicTokens(topic).map({ ($0, 3) }) { best[t.lowercased()] = max(best[t.lowercased()] ?? 0, w) }
        return best.sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }.prefix(30).map { ($0.key, $0.value) }
    }

    /// 自动用哪个含义，还是要人选（同 _decide）。返回 (要不要问, 选的编号, 原因)
    private static func decide(_ senses: [Sense], _ recentPick: String) -> (ask: Bool, chosen: String, reason: String) {
        if senses.count == 1 { return (false, senses[0].key, "") }
        let by = Dictionary(uniqueKeysWithValues: senses.map { ($0.key, $0) })
        let score = by.mapValues { recentWeight * Double($0.estRecent) + Double($0.estOlder) }
        let total = score.values.reduce(0, +)
        let keys = senses.map(\.key)
        if total <= 0 {
            if let s = by[recentPick] { return (false, recentPick, "库里没有直接出现这个词，按你近期的关注理解为「\(s.label)」") }
            return (true, "", "库里没有直接出现这个词，它有几种常见含义")
        }
        let top = keys.max { score[$0]! < score[$1]! }!
        let recTop = keys.max { by[$0]!.estRecent < by[$1]!.estRecent }!
        let oldTop = keys.max { by[$0]!.estOlder < by[$1]!.estOlder }!
        if by[recentPick] != nil, recentPick != top {
            return (true, "", "库里多数按「\(by[top]!.label)」用，但你近期更像在关注「\(by[recentPick]!.label)」")
        }
        if recTop != oldTop, by[recTop]!.estRecent >= 2, by[oldTop]!.estOlder >= 2 {
            return (true, "", "最近 \(recentNoteDays) 天的笔记以「\(by[recTop]!.label)」为主，更早的以「\(by[oldTop]!.label)」为主")
        }
        return score[top]! / total >= autoShare ? (false, top, "") : (true, "", "库里几种含义都有不少")
    }

    static func analyze(_ topic: String, ctx: Contexts, config: LLM.Config) async throws -> (senses: [Sense], ask: Bool, chosen: String, reason: String) {
        let samples = ctx.samples
        let ctxText = samples.isEmpty ? "\n库里没有找到这个词原样出现的地方，按常识判断含义。\n"
            : "\n库里出现这个词的原文片段（编号、日期）：\n" + samples.enumerated().map { i, r in
                "[\(i + 1)] (\(r.date.isEmpty ? "日期不详" : r.date)\(r.recent ? "，近期" : "")) \(r.text)" }.joined(separator: "\n") + "\n"
        var senses: [String: Sense] = [:]
        var terms: [String: [(String, Int)]] = [:]
        var recentPick = ""
        do {
            var raw = try await LLM.complete(expandPrompt.replacingOccurrences(of: "{topic}", with: topic)
                .replacingOccurrences(of: "{contexts}", with: ctxText).replacingOccurrences(of: "{activity}", with: ""),
                                             config: config, maxTokens: 2500, timeout: 180)
            raw = takeSenseLines(raw, &senses, samples.count)
            if let r = RX.first(#"(?m)^\s*近期\s*[|｜]\s*([A-D-])"#, raw), r != "-" { recentPick = r }
            for g in RX.all(#"(?m)^\s*(?:([A-D])\s*[|｜]\s*)?([321])\s*[|｜]\s*(.+?)\s*$"#, raw) {
                let key = g[1].isEmpty ? "A" : g[1], weight = Int(g[2]) ?? 1
                // 模型有时会把「英文|中文」塞在同一行，一律拆开
                for piece in RX.split(#"[|｜/、,，;；]+"#, g[3]) {
                    let term = piece.trimmingCharacters(in: CharacterSet(charactersIn: " `\"'（）()"))
                    if term.count > 1, term.count <= 40, !term.allSatisfy(\.isNumber) { terms[key, default: []].append((term, weight)) }
                }
            }
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            senses = [:]; terms = [:]   // 模型不可用：退回主题本身的切词
        }
        for key in terms.keys where senses[key] == nil {
            senses[key] = Sense(key: key, label: String(topic.prefix(30)), meaning: "", snippets: [])
        }
        senses = senses.filter { terms[$0.key]?.isEmpty == false }
        if senses.isEmpty { senses = ["A": Sense(key: "A", label: String(topic.prefix(30)), meaning: "", snippets: [])] }

        let nRec = samples.filter(\.recent).count, nOld = samples.count - nRec
        var out: [Sense] = []
        for key in senses.keys.sorted() {
            var s = senses[key]!
            let rec = s.snippets.filter { samples[$0 - 1].recent }.count
            let old = s.snippets.count - rec
            s.estRecent = nRec > 0 ? Int((Double(ctx.recentHits * rec) / Double(nRec)).rounded()) : 0
            s.estOlder = nOld > 0 ? Int((Double(ctx.olderHits * old) / Double(nOld)).rounded()) : 0
            s.terms = cleanTerms(terms[key] ?? [], topic)
            out.append(s)
        }
        let d = decide(out, recentPick)
        return (out, d.ask, d.chosen, d.reason)
    }

    // MARK: 第二步：本地全文打分

    private static func asciiRegex(_ terms: [String]) -> NSRegularExpression? {
        guard !terms.isEmpty else { return nil }
        let parts = terms.map { t -> String in
            let pieces = RX.split(#"[\s\-]+"#, t.trimmingCharacters(in: .whitespaces)).filter { !$0.isEmpty }.map(NSRegularExpression.escapedPattern)
            let body = pieces.count > 1 ? pieces.joined(separator: #"[\s\-]?"#) : (pieces.first ?? NSRegularExpression.escapedPattern(for: t))
            return "(\(body)(?:e?s)?)"
        }
        return try? NSRegularExpression(pattern: "(?<![A-Za-z0-9])(?:" + parts.joined(separator: "|") + ")(?![A-Za-z0-9])", options: .caseInsensitive)
    }

    private static func snippet(_ text: String, _ terms: [String], width: Int = 240) -> String {
        let low = text.lowercased() as NSString
        var pos = NSNotFound
        for t in terms {
            let r = low.range(of: t.lowercased())
            if r.location != NSNotFound, pos == NSNotFound || r.location < pos { pos = r.location }
        }
        let ns = text as NSString
        let start = pos == NSNotFound ? 0 : max(0, pos - width / 3)
        let piece = ns.substring(with: NSRange(location: start, length: min(width, ns.length - start)))
        return RX.sub(#"\s+"#, " ", piece).trimmingCharacters(in: .whitespaces)
    }

    private static func count(_ needle: String, in hay: String) -> Int {
        guard !needle.isEmpty else { return 0 }
        var n = 0, range = hay.startIndex..<hay.endIndex
        while let r = hay.range(of: needle, range: range) { n += 1; range = r.upperBound..<hay.endIndex }
        return n
    }

    static func scoreNotes(_ lib: LocalNotes, _ notes: [LocalNotes.Note], _ terms: [(String, Int)], progress: Progress) -> [Candidate] {
        guard !terms.isEmpty else { return [] }
        let words = terms.map(\.0)
        var weights: [String: Int] = [:]
        for (t, w) in terms { weights[t.lowercased()] = w }
        let asciiTerms = words.filter(isASCII), cjkTerms = words.filter { !isASCII($0) }
        let asciiRe = asciiRegex(asciiTerms)
        var rows: [(LocalNotes.Note, [String: Int], Int, String)] = []
        var df: [String: Int] = [:]
        var totalLen = 0
        for (i, n) in notes.enumerated() {
            if (i + 1) % 300 == 0 || i + 1 == notes.count { progress("全文检索 \(i + 1)/\(notes.count)") }
            guard let text = lib.read(n.path, maxChars: readCap) else { continue }
            let low = text.lowercased(), titleLow = n.title.lowercased()
            var tf: [String: Int] = [:]
            for t in cjkTerms { let c = count(t.lowercased(), in: low); if c > 0 { tf[t.lowercased()] = c } }
            if let asciiRe {
                for m in asciiRe.matches(in: low, range: NSRange(location: 0, length: (low as NSString).length)) {
                    guard let gi = (1..<m.numberOfRanges).first(where: { m.range(at: $0).location != NSNotFound }) else { continue }
                    tf[asciiTerms[gi - 1].lowercased(), default: 0] += 1
                }
            }
            if tf.isEmpty { continue }
            // 标题命中是很强的信号，等价于正文里多出现 5 次
            for t in words { let tl = t.lowercased(); if titleLow.contains(tl), tf[tl] != nil { tf[tl]! += 5 } }
            let length = max(1, text.count)
            totalLen += length
            for k in tf.keys { df[k, default: 0] += 1 }
            rows.append((n, tf, length, snippet(text, words)))
        }
        guard !rows.isEmpty else { return [] }
        let nDocs = Double(max(notes.count, rows.count))
        let avgLen = Double(totalLen) / Double(rows.count)
        return rows.map { n, tf, length, snip in
            var score = 0.0
            for (term, freq) in tf {
                let d = Double(df[term]!)
                let idf = log(1 + (nDocs - d + 0.5) / (d + 0.5))
                let f = Double(freq)
                score += Double(weights[term] ?? 1) * idf * f * (bm25K1 + 1) / (f + bm25K1 * (1 - bm25B + bm25B * Double(length) / avgLen))
            }
            return Candidate(path: n.path, title: n.title, date: n.date, chars: n.chars, score: (score * 1000).rounded() / 1000, snippet: snip)
        }.sorted { $0.score > $1.score }
    }

    // MARK: 第三步：相关度筛选

    static func screen(_ topic: String, _ cands: inout [Candidate], meaning: String, config: LLM.Config, progress: Progress) async throws {
        let batches = stride(from: 0, to: cands.count, by: screenBatch).map { $0..<min($0 + screenBatch, cands.count) }
        let snapshot = cands
        progress("判定相关度 0/\(batches.count)")
        // 几批同时问，手机上等得短一些
        let results = try await withThrowingTaskGroup(of: (Range<Int>, String?).self) { group in
            for r in batches {
                let items = snapshot[r].enumerated().map { i, c in
                    let folder = (c.path as NSString).deletingLastPathComponent
                    return "[\(i + 1)] 标题：\(c.title)\n    日期：\(c.date.isEmpty ? "未注明" : c.date)｜位置：\(folder.isEmpty ? "." : folder)\n    片段：\(c.snippet.prefix(240))"
                }.joined(separator: "\n\n")
                let prompt = screenPrompt.replacingOccurrences(of: "{topic}", with: topic)
                    .replacingOccurrences(of: "{meaning}", with: meaning.isEmpty ? "" : "（指：\(meaning)）")
                    .replacingOccurrences(of: "{items}", with: items)
                group.addTask {
                    do { return (r, try await LLM.complete(prompt, config: config, maxTokens: 2000, timeout: 300)) }
                    catch is CancellationError { throw CancellationError() }
                    catch { return (r, nil) }      // 这一批失败：保持未判定
                }
            }
            var out: [(Range<Int>, String?)] = []
            for try await x in group { out.append(x); progress("判定相关度 \(out.count)/\(batches.count)") }
            return out
        }
        for (r, raw) in results {
            guard let raw else { continue }
            for g in RX.all(#"(?m)^\s*\[?(\d+)\]?\s*[|｜]\s*([0-5])\s*[|｜]\s*(.*?)\s*$"#, raw) {
                guard let i = Int(g[1]), i >= 1, i <= r.count else { continue }
                cands[r.lowerBound + i - 1].relevance = Int(g[2]) ?? -1
                cands[r.lowerBound + i - 1].reason = String(g[3].prefix(60))
            }
        }
    }

    // MARK: 编排

    /// 单集笔记 <节目>/notes/YYYYMMDD 标题.md 对应的整理稿 <节目>/speech/YYYYMMDD_标题.md
    static func speechForEpisodeNote(_ path: String) -> String {
        var parts = path.split(separator: "/").map(String.init)
        guard parts.count >= 2, parts[parts.count - 2] == "notes" else { return "" }
        let name = RX.sub(#"^(\d{8}) "#, "$1_", parts.removeLast())
        parts.removeLast()
        return (parts + ["speech", name]).joined(separator: "/")
    }

    /// 同一期有几份时留哪份：整理稿 > 其它整理过的 > 逐字稿 > 单集笔记
    static func versionRank(_ path: String) -> Int {
        let parts = path.split(separator: "/").dropLast().map(String.init)
        if parts.contains("speech") { return 0 }
        if parts.contains("transcripts") || parts.contains("_transcripts") { return 2 }
        if parts.contains("notes") { return 3 }
        return 1
    }

    /// folder：只在这个文件夹（相对笔记库）里找；excludeOutput：报告、复盘不当素材（免得越滚越自我引用）
    static func find(_ lib: LocalNotes, topic: String, folder: String = "", excludeOutput: Bool = true,
                     config: LLM.Config, progress: Progress) async throws -> Result {
        let topic = topic.trimmingCharacters(in: .whitespacesAndNewlines)
        progress("扫描手机上的笔记")
        var notes = lib.scan()
        if !folder.isEmpty { notes = notes.filter { $0.folder == folder || $0.folder.hasPrefix(folder + "/") } }
        if excludeOutput { notes = notes.filter { $0.folder != "output" && !$0.folder.hasPrefix("output/") } }
        guard !notes.isEmpty else {
            return Result(topic: topic, meaning: "", label: topic, terms: [], scanned: 0, matched: 0, candidates: [],
                          insufficient: "手机上还没有可检索的笔记", askedButPicked: "")
        }
        try Task.checkCancellation()
        progress("看主题词在库里怎么用")
        let ctx = collectContexts(lib, topic, notes)
        try Task.checkCancellation()
        progress("判断主题含义、扩展检索词")
        let a = try await analyze(topic, ctx: ctx, config: config)
        let chosen = a.senses.first { $0.key == a.chosen } ?? a.senses[0]
        let picked = a.ask && a.senses.count > 1
            ? "「\(topic)」有几种含义（\(a.senses.map(\.label).joined(separator: "、"))），按「\(chosen.label)」检索。\(a.reason)" : ""
        let terms = chosen.terms
        progress("检索词：\(terms.prefix(10).map(\.0).joined(separator: ", "))…")
        try Task.checkCancellation()

        let scored = scoreNotes(lib, notes, terms, progress: progress)
        try Task.checkCancellation()

        // 同一期常存好几份（整理稿 / 逐字稿 / 单集笔记），按标题 + 日期合并成一篇，留哪份按版本优先级定
        var groups: [String: [Candidate]] = [:], order: [String] = []
        for c in scored {
            let k = RX.sub(#"\s+"#, "", c.title).lowercased() + "|" + c.date
            if groups[k] == nil { order.append(k) }
            groups[k, default: []].append(c)
        }
        let byPath = Dictionary(notes.map { ($0.path, $0) }, uniquingKeysWith: { a, _ in a })
        var deduped: [Candidate] = order.map { k in
            let members = groups[k]!
            var best = members.min { (versionRank($0.path), -$0.score) < (versionRank($1.path), -$1.score) }!
            best.score = members.map(\.score).max()!
            if versionRank(best.path) == 3, let sib = byPath[speechForEpisodeNote(best.path)] {
                best.path = sib.path; best.chars = sib.chars
            }
            return best
        }
        deduped.sort { $0.score > $1.score }
        var top = Array(deduped.prefix(defaultCandidates))

        if !top.isEmpty {
            try await screen(topic, &top, meaning: chosen.meaning, config: config, progress: progress)
            for i in top.indices where top[i].relevance < 0 { top[i].relevance = 2 }
            top.sort { ($0.relevance, $0.score) > ($1.relevance, $1.score) }
        }
        let good = top.filter { $0.relevance >= 3 }.count
        let insufficient = good < minGood
            ? "库里按「\(chosen.label)」相关的资料只有 \(good) 篇，不够写一份像样的报告，可以先用「信息跟进」收集一批再来" : ""
        return Result(topic: topic, meaning: chosen.meaning, label: chosen.label, terms: terms, scanned: notes.count,
                      matched: deduped.count, candidates: top, insufficient: insufficient, askedButPicked: picked)
    }
}

extension ISO8601DateFormatter {
    /// 本地时区的 YYYY-MM-DD
    nonisolated(unsafe) static let day: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate, .withDashSeparatorInDate]
        f.timeZone = .current
        return f
    }()
}
