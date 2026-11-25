import Foundation

/// 手机上的「讨论」：跟电脑 core/discuss.py 同一套规则，提示词在 DiscussPrompt.swift（两端逐字一致）。
/// 手机没有电脑那份全文索引，检索走本地扫描：扫手机上的 Markdown，按检索词「任一词命中」给每个段落块打分
/// （命中几个不同的词，再看出现几次），每篇留得分最高的 2 块、最多 12 篇。跟电脑的差别：电脑是 FTS5 trigram + bm25，
/// 手机是逐篇读文件数子串；标题、来源、一句话取手机上现成的（处理记录、剪藏文件头），读不到的就空着。
enum DiscussRunner {
    struct Answer: Sendable { let text: String; let sources: [DiscussPrompt.Source] }
    typealias Progress = @Sendable (String) -> Void

    /// 范围限定在标签时：这个标签下的文章（新的在前），取自主题页（TagModel）；用主文件的路径当范围
    static func tagPicks(_ items: [TagItem]) -> [ReviewRunner.TagPick] {
        items.map { ReviewRunner.TagPick(type: $0.type, path: TagIndex.mainFile($0.paths), title: $0.title, source: $0.source, date: $0.date) }
    }

    private static func llm(_ prompt: String, _ c: LLM.Config, maxTokens: Int) async throws -> String {
        try await LLM.complete(prompt, config: c, maxTokens: maxTokens, timeout: 1200)
    }

    // MARK: 一轮问答

    static func ask(question: String, history: [DiscussPrompt.Turn], tag: String, picks: [ReviewRunner.TagPick],
                    lib: LocalNotes, config c: LLM.Config, progress: @escaping Progress) async throws -> Answer {
        progress("想检索词")
        let raw = try await llm(DiscussPrompt.queryPrompt(question: question, history: history), c, maxTokens: 300)
        let terms = DiscussPrompt.parseTerms(raw, question: question)
        progress("在手机上的笔记里找相关内容")
        let allowed: Set<String>? = tag.isEmpty ? nil : Set(picks.map { "Spark/\($0.path)" })
        var found = await Task.detached(priority: .userInitiated) { search(lib, terms, allowed: allowed) }.value
        try Task.checkCancellation()
        if !tag.isEmpty && found.count < DiscussPrompt.tagMinDocs {
            let have = Set(found.map(\.source.path))
            let idx = lib.buildIndex()
            for (pick, row) in zip(picks.prefix(TagReviewPrompt.maxItems), ReviewRunner.tagRows(Array(picks.prefix(TagReviewPrompt.maxItems)), idx, lib)) where found.count < DiscussPrompt.tagFillDocs {
                let path = "Spark/\(pick.path)"
                if have.contains(path) || row.tldr.isEmpty { continue }
                found.append(Found(material: .init(type: row.type, source: row.source, tldr: row.tldr, snippets: []),
                                   source: .init(n: 0, title: pick.title, path: path, kind: row.type, date: pick.date, source: pick.source)))
            }
        }
        if found.isEmpty { return Answer(text: DiscussPrompt.notFound, sources: []) }
        progress("用 \(found.count) 篇材料写回答")
        let text = try await llm(DiscussPrompt.answerPrompt(question: question, history: history, materials: found.map(\.material),
                                                             tag: tag, style: ReviewRunner.style), c, maxTokens: 4000)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let sources = DiscussPrompt.cited(text, count: found.count).map { n -> DiscussPrompt.Source in
            let s = found[n - 1].source
            return .init(n: n, title: s.title, path: s.path, kind: s.kind, date: s.date, source: s.source)
        }
        return Answer(text: text, sources: sources)
    }

    // MARK: 检索

    struct Found: Sendable { let material: DiscussPrompt.Material; let source: DiscussPrompt.Source }

    private static let readCap = 200_000   // 一篇最多读多少字（同 TopicSearch.readCap）

    /// 按段落攒块：攒到 300 字以上就收一块，一段超过 800 字照样单独一块（电脑端的块是 300 到 800 字）
    static func chunks(_ body: String) -> [String] {
        var out: [String] = [], cur = ""
        for para in body.components(separatedBy: "\n\n") {
            let p = para.trimmingCharacters(in: .whitespacesAndNewlines)
            if p.isEmpty { continue }
            cur += (cur.isEmpty ? "" : "\n\n") + p
            if cur.count >= 300 { out.append(cur); cur = "" }
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    static func kindLabel(_ path: String) -> String {
        if PathRules.isPaper(path) { return "论文" }
        if PathRules.isClip(path) { return "剪藏" }
        if PathRules.isJotting(path) { return "随手记" }
        let parts = path.split(separator: "/").map(String.init)
        if parts.count >= 3, let k = ["notes": "单集小结", "speech": "整理稿", "transcripts": "文字记录", "topics": "专题"][parts[parts.count - 2]] { return k }
        if path.hasPrefix(PathRules.trackDir) { return parts.count >= 3 && parts[2] == "简报" ? "简报" : "信息文章" }
        return "会议资料"
    }

    static func origin(_ path: String, clipSource: String) -> String {
        if !clipSource.isEmpty { return clipSource }
        let parts = path.split(separator: "/").map(String.init)
        if parts.first == "Spark", parts.count > 2 { return parts[1] == "信息跟进" && parts.count > 3 ? parts[2] : parts[1] }
        return parts.first == "output" ? "报告" : "笔记库"
    }

    /// output/ 下自己写的报告和复盘不当材料（同电脑端，免得越滚越自我引用）
    static func search(_ lib: LocalNotes, _ terms: [String], allowed: Set<String>?) -> [Found] {
        let low = terms.map { $0.lowercased() }
        guard !low.isEmpty else { return [] }
        let idx = lib.buildIndex()
        var ranked: [(score: (Int, Int, Int, Int), found: Found)] = []
        for n in lib.scan() {
            if Task.isCancelled { return [] }
            if n.folder == "output" || n.folder.hasPrefix("output/") { continue }
            if let allowed, !allowed.contains(n.path) { continue }
            guard let text = lib.read(n.path, maxChars: readCap) else { continue }
            let body = text.hasPrefix("---\n") ? String(text[(text.range(of: "\n---\n", range: text.index(text.startIndex, offsetBy: 4)..<text.endIndex)?.upperBound ?? text.startIndex)...]) : text
            // 块的得分：命中几个不同的词，再看一共出现几次
            var best: [(Int, Int, String)] = []
            for ch in chunks(body) {
                let l = ch.lowercased()
                let counts = low.map { l.components(separatedBy: $0).count - 1 }
                let distinct = counts.filter { $0 > 0 }.count
                if distinct > 0 { best.append((distinct, counts.reduce(0, +), ch)) }
            }
            let titleLow = n.title.lowercased()
            let titleHits = low.filter { titleLow.contains($0) }.count
            if best.isEmpty && titleHits == 0 { continue }
            best.sort { ($0.0, $0.1) > ($1.0, $1.1) }
            let top = Array(best.prefix(DiscussPrompt.maxPerDoc))
            let meta: ClipMeta? = PathRules.isClip(n.path) ? Clips.meta(path: n.path, text: text) : nil
            let tldr = meta?.tldr ?? idx.byPath[n.path].flatMap { idx.items[$0] }?.tldr ?? ""
            let from = origin(n.path, clipSource: meta?.source ?? "")
            let kind = kindLabel(n.path)
            let it = LocalNotes.Item(show: from, folder: "", entry: "", title: n.title, date: n.date.replacingOccurrences(of: "-", with: ""),
                                     tldr: "", body: "", note: "", updatedAt: "")
            let found = Found(material: .init(type: kind, source: ReviewRunner.source(it), tldr: tldr,
                                              snippets: top.map { DiscussPrompt.cut($0.2, DiscussPrompt.snippetChars) }),
                              source: .init(n: 0, title: n.title, path: n.path, kind: kind, date: n.date, source: from))
            ranked.append(((top.first?.0 ?? 0, titleHits, top.reduce(0) { $0 + $1.1 }, n.date.isEmpty ? 0 : 1), found))
        }
        ranked.sort { $0.score > $1.score }
        return ranked.prefix(DiscussPrompt.maxDocs).map(\.found)
    }

    // MARK: 存成复盘

    /// 写进手机笔记库（output/复盘/），再交给电脑存档。返回给人看的一句话
    @MainActor
    static func save(turns: [DiscussPrompt.SavedTurn], tag: String, config c: LLM.Config, vault: Vault, jobs: LocalJobs) async throws -> String {
        guard let first = turns.first else { throw SparkError("还没有可以存的问答") }
        let now = Date()
        let stampF = DateFormatter(); stampF.dateFormat = "yyyy-MM-dd HH:mm"
        let nameF = DateFormatter(); nameF.dateFormat = "yyyyMMddHHmm"
        let d = DiscussPrompt.document(turns: turns, tag: tag, generated: "\(stampF.string(from: now))（在手机上生成，\(c.label)）")
        var path = "output/复盘/" + DiscussPrompt.fileName(firstQuestion: first.q, stamp: nameF.string(from: now))
        let base = String(path.dropLast(3))
        var n = 1
        while vault.localText(path) != nil { n += 1; path = "\(base)_\(n).md" }   // 同一分钟存两次：另起一个名字，不盖掉前一份
        let text = DiscussPrompt.render(title: d.title, meta: d.meta, body: d.body)
        return try await jobs.saveGenerated(path: path, title: d.title, content: text, vault: vault)
    }
}
