import CryptoKit
import Foundation

/// 手机上出报告：按主题检索选材料，再走跟电脑端 apps/notes2insight/pipeline.py 一样的三段：
///   摘取（每篇一张摘要卡，长文分块再合卡，按内容缓存）→ 归纳（报告骨架）→ 成文（开篇、逐章、收尾）。
/// 选材料的规矩跟手机交给电脑的「出报告」任务一样（core/remote_tasks.py _report）：相关度 ≥3 的最多 15 篇，
/// 一篇都没有就取检索前 5 篇。深度用「标准」。
enum ReportRunner {
    static let chunkChars = 28_000
    static let frameworkBatchChars = 90_000
    static let composeContextChars = 70_000
    static let reportPick = 15
    static let concurrency = 3
    static let depth = (label: "标准", clusters: 6, words: "约 1.2-1.8 万字", chapterWords: "1200-1800 字")

    static let commonRules = """
    写作纪律：
    - 只使用给定材料中真实出现的内容，不要补充材料之外的知识，不要编造数字、日期或人名。
    - 公司、产品、芯片、模型、论文、人名等专有名词保留英文原名。
    - 材料中的数字要保留口径（单位、时间范围、谁说的）。
    - 材料本身可能来自语音转写或 AI 整理，含少量错误；遇到明显矛盾时指出矛盾，不要强行调和。
    - 中文正文使用中文全角标点（，。：；""），英文术语、数字与单位保持半角。
    - 直接输出 Markdown 正文，不要用 ```markdown 代码块把整段内容包起来。

    """

    static let digestPrompt = """
    你是一名技术情报分析师。下面是知识库中的一篇笔记（编号 [{idx}]），可能是播客整理稿、文章、论文或会议记录。

    请把它压缩成一张"可供跨笔记综合分析"的技术摘要卡。{focus_clause}

    {rules}
    输出格式（严格使用下列小标题；某一节没有内容就写"无"）：

    ## 一句话主旨
    ## 核心技术论点
    - 3-7 条，每条一句话，说清"主张是什么 + 依据是什么"
    ## 关键事实与数据
    - 具体数字、规格、价格、比例、时间；注明是谁说的
    ## 时间锚点
    - 事件 / 发布 / 里程碑 + 日期；笔记未注明日期就写"未注明"
    ## 涉及主体
    - 公司 / 产品 / 技术名词 / 人物，逗号分隔
    ## 判断与预测
    - 材料中对未来的判断，标明出自谁
    ## 分歧与不确定
    - 材料内部的争议、前提限制、反对意见
    ## 可引用原话
    - 最多 3 条短句，加引号

    笔记标题：{title}
    笔记路径：{path}
    笔记日期：{date}
    {chunk_note}
    --- 笔记正文开始 ---
    {body}
    --- 笔记正文结束 ---

    """

    static let mergeChunkPrompt = """
    下面是同一篇长笔记被切成 {n} 段后各自生成的摘要卡。请合并成一张完整摘要卡。

    要求：去重、按重要性排序、保留所有具体数字与日期、不要新增材料之外的内容。
    输出格式与输入保持一致（同样的小标题）。

    笔记标题：{title}

    {parts}

    """

    static let frameworkPrompt = """
    你是一名技术趋势分析师。下面是 {n} 篇笔记的技术摘要卡，每篇有编号 [N]。

    请据此设计一份**技术洞察报告**的骨架。{focus_clause}

    {rules}
    - 主题簇要按"技术议题"切分，不要按笔记来源或时间顺序切分。
    - 每个判断都要能追溯到具体编号。

    输出格式（严格遵守，编号格式不要改动）：

    ## 报告标题
    <一行，具体、有信息量，不要用"综述""汇总"这类空词做主语>

    ## 副标题
    <一行，点出这批材料真正的共同问题>

    ## 主题簇
    ### T1. <主题名>
    - 概括：<一句话>
    - 相关笔记：[1][3][7]
    - 核心张力：<这个主题里真正没有定论或正在变化的是什么>
    ### T2. ...
    （共 {clusters} 个左右，按重要性排序）

    ## 全局判断
    - J1. <一句话判断> —— 依据 [2][5]
    - （6-10 条，每条必须带依据编号）

    ## 主要分歧
    - D1. <A 方主张> vs <B 方主张> —— [4] vs [9]
    - （3-8 条）

    ## 时间线
    - <日期>｜<事件>｜[编号]
    - （按时间排序，只列材料中有明确日期的）

    --- 摘要卡开始 ---
    {cards}
    --- 摘要卡结束 ---

    """

    static let premergePrompt = """
    下面是一批技术摘要卡（编号 [N]）。请压缩成一份"批次要点"，保留：核心论点、关键数字与日期、判断、分歧，并在每条后面保留来源编号。
    不要丢失编号，不要新增材料之外的内容。控制在 2500 字以内。

    {cards}

    """

    static let composeHeaderPrompt = """
    你正在撰写一份技术洞察报告的**开篇部分**。报告骨架与材料如下。

    请输出两节：

    ## 执行摘要
    用 400-700 字说清楚：这批材料共同回答了什么问题、最重要的三到五个变化是什么、读者应该带走什么。不要罗列笔记标题。

    ## 关键判断
    把骨架中的"全局判断"改写成 6-10 条，每条格式：
    **判断 N：<一句话结论>**
    <2-4 句展开：机制是什么、证据是什么、什么情况下这条判断会失效>（依据 [编号]）

    {rules}
    {focus_clause}

    --- 报告骨架 ---
    {framework}

    --- 摘要卡 ---
    {cards}

    """

    static let composeChapterPrompt = """
    你正在撰写一份技术洞察报告的**第 {n} 章**，主题是：{topic}

    本章篇幅 {chapter_words}。结构自拟，但必须包含：
    1. 这个主题当前的事实状态（谁在做什么，到了哪一步，有哪些具体数字）
    2. 技术机制层面的解释（为什么会这样，约束在哪里）
    3. 材料之间的相互印证或冲突
    4. 这个主题的"核心张力"——尚未有定论的是什么

    写作要求：
    - 段落式论述，不要通篇要点罗列；需要对比时可以用 Markdown 表格。
    - 每个具体主张后面用 [编号] 标注来源，一段里出现多个来源就并列标注。
    - 不要重复执行摘要已经说过的话，这一章要给出更细的机制与证据。
    - 只输出本章内容，以 `## 第 {n} 章 {topic}` 开头，不要写其他章节。

    {rules}
    {focus_clause}

    --- 报告骨架（供定位，不要整段复述）---
    {framework}

    --- 本章相关摘要卡 ---
    {cards}

    """

    static let composeTailPrompt = """
    你正在撰写一份技术洞察报告的**收尾部分**。请输出三节：

    ## 分歧与开放问题
    把骨架中的分歧展开成 4-8 组，每组格式：
    **分歧 N：<争论点>**
    - 一方：<主张与依据> [编号]
    - 另一方：<主张与依据> [编号]
    - 目前无法判定的原因：<一句话>

    ## 信号与展望
    - 先给出未来 6-24 个月的 3 个情景（基线 / 加速 / 受阻），每个情景 100-200 字，说明触发条件。
    - 再给出 8-12 个**可观测信号**，每条写清"看什么指标、出现什么读数意味着哪个情景在兑现"。

    ## 启示
    面向一位关注这些技术的从业者，给出 5-8 条可执行的启示，每条一句结论加一句理由。不要写成空泛建议。

    {rules}
    {focus_clause}

    --- 报告骨架 ---
    {framework}

    --- 摘要卡 ---
    {cards}

    """

    static func fill(_ template: String, _ values: [String: String]) -> String {
        var s = template
        for (k, v) in values { s = s.replacingOccurrences(of: "{\(k)}", with: v) }
        return s
    }

    final class Ref: @unchecked Sendable {
        let idx: Int, path: String, title: String, date: String, chars: Int
        var digest = "", error = ""
        init(idx: Int, path: String, title: String, date: String, chars: Int) {
            self.idx = idx; self.path = path; self.title = title; self.date = date; self.chars = chars
        }
    }

    struct Cluster { let no: Int; let topic: String; let summary: String; let tension: String; let noteIDs: [Int] }

    static func focusClause(_ focus: String) -> String {
        let f = focus.trimmingCharacters(in: .whitespacesAndNewlines)
        return f.isEmpty ? "" : "\n**本次分析的关注点：\(f)**\n与关注点相关的内容要保留细节；无关内容压缩成一两句带过，但不要完全丢弃。\n"
    }

    static func stripNoise(_ text: String) -> String {
        RX.sub(#"\n{3,}"#, "\n\n", RX.sub(#"!\[[^\]]*\]\([^)]*\)"#, "", text)).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 按长度切块，尽量切在段落边界（同 core/digest.py 的 chunks）
    static func chunks(_ text: String, _ size: Int) -> [String] {
        let chars = Array(text)
        guard chars.count > size else { return [text] }
        var out: [String] = [], start = 0
        while start < chars.count {
            var end = min(start + size, chars.count)
            if end < chars.count {
                var i = end - 2
                while i >= start + size / 2 {
                    if chars[i] == "\n" && chars[i + 1] == "\n" { end = i; break }
                    i -= 1
                }
            }
            out.append(String(chars[start..<end]))
            start = end
        }
        return out
    }

    // MARK: 摘要卡缓存（手机自己的，在 Caches 里，系统空间紧张时可以清掉）

    private static var cacheDir: URL {
        let d = URL.cachesDirectory.appending(path: "digests", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    private static func cacheKey(_ c: LLM.Config, _ focus: String, _ ref: Ref, _ text: String) -> String {
        let s = ["v1", c.provider.rawValue, c.model, focus, ref.title, ref.date, text].joined(separator: "\u{1F}")
        return SHA256.hash(data: Data(s.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    // MARK: 摘取

    private static func digest(_ ref: Ref, lib: LocalNotes, focus: String, c: LLM.Config) async throws -> String {
        guard let raw = lib.read(ref.path) else { throw SparkError("手机上没有这篇笔记") }
        let text = stripNoise(raw)
        if text.isEmpty { throw SparkError("笔记为空") }
        let key = cacheKey(c, focus, ref, text)
        let cacheURL = cacheDir.appending(path: key + ".md")
        if let hit = try? String(contentsOf: cacheURL, encoding: .utf8), !hit.isEmpty { return hit }
        let parts = chunks(text, chunkChars)
        var cards: [String] = []
        for (i, part) in parts.enumerated() {
            let note = parts.count == 1 ? "" : "（这是全文的第 \(i + 1)/\(parts.count) 段，只针对本段做卡片）\n"
            cards.append(try await LLM.complete(fill(digestPrompt, [
                "idx": "\(ref.idx)", "title": ref.title, "path": ref.path, "date": ref.date.isEmpty ? "未注明" : ref.date,
                "focus_clause": focusClause(focus), "rules": commonRules, "chunk_note": note, "body": part,
            ]), config: c, maxTokens: 4000))
        }
        let result: String
        if cards.count == 1 {
            result = cards[0]
        } else {
            let merged = cards.enumerated().map { "--- 第 \($0.offset + 1) 段的卡片 ---\n\($0.element)" }.joined(separator: "\n\n")
            result = try await LLM.complete(fill(mergeChunkPrompt, ["n": "\(cards.count)", "title": ref.title, "parts": merged]),
                                            config: c, maxTokens: 4000)
        }
        try? result.write(to: cacheURL, atomically: true, encoding: .utf8)
        return result
    }

    // MARK: 归纳

    static func cardBlock(_ r: Ref) -> String {
        "\n===== [\(r.idx)] \(r.title)｜\(r.date.isEmpty ? "未注明日期" : r.date)｜\(r.path) =====\n\(r.digest.trimmingCharacters(in: .whitespacesAndNewlines))\n"
    }

    static func cardsText(_ refs: [Ref], ids: Set<Int>? = nil, budget: Int = composeContextChars) -> String {
        var out: [String] = [], used = 0
        let pool = refs.filter { !$0.digest.isEmpty && (ids == nil || ids!.contains($0.idx)) }
        for r in pool {
            let block = cardBlock(r)
            if used + block.count > budget {
                out.append("\n（余下摘要卡因长度限制未展开，共省略 \(pool.count - out.count) 张）\n")
                break
            }
            out.append(block)
            used += block.count
        }
        return out.joined()
    }

    static func frameworkBatches(_ ok: [Ref]) -> [[Ref]] {
        var batches: [[Ref]] = [], cur: [Ref] = [], len = 0
        for r in ok {
            let b = cardBlock(r).count
            if !cur.isEmpty, len + b > frameworkBatchChars { batches.append(cur); cur = []; len = 0 }
            cur.append(r); len += b
        }
        if !cur.isEmpty { batches.append(cur) }
        return batches
    }

    static func parseFramework(_ fw: String) -> (title: String, subtitle: String, clusters: [Cluster]) {
        let title = (RX.first(#"(?m)^##\s*报告标题\s*\n+(.+?)\s*$"#, fw) ?? "")
            .trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "#")).trimmingCharacters(in: .whitespaces)
        let subtitle = (RX.first(#"(?m)^##\s*副标题\s*\n+(.+?)\s*$"#, fw) ?? "").trimmingCharacters(in: .whitespaces)
        let ns = fw as NSString
        let ms = RX.re(#"(?m)^(?:#{2,5}\s*)?\**\s*T(\d+)[.、．:：]?\s*(.+?)\s*\**\s*$"#).matches(in: fw, range: NSRange(location: 0, length: ns.length))
        var clusters: [Cluster] = []
        for (i, m) in ms.enumerated() {
            let start = m.range.location + m.range.length
            let end = i + 1 < ms.count ? ms[i + 1].range.location : ns.length
            var body = ns.substring(with: NSRange(location: start, length: end - start))
            if let cut = body.range(of: #"(?m)^##\s+"#, options: .regularExpression) { body = String(body[..<cut.lowerBound]) }
            let ids = RX.first(#"相关笔记\s*[:：]\s*(.+)"#, body).map { RX.all(#"\[(\d+)\]"#, $0).compactMap { Int($0[1]) } } ?? []
            clusters.append(Cluster(no: Int(ns.substring(with: m.range(at: 1))) ?? i + 1,
                                    topic: ns.substring(with: m.range(at: 2)).trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "*")).trimmingCharacters(in: .whitespaces),
                                    summary: (RX.first(#"概括\s*[:：]\s*(.+)"#, body) ?? "").trimmingCharacters(in: .whitespaces),
                                    tension: (RX.first(#"核心张力\s*[:：]\s*(.+)"#, body) ?? "").trimmingCharacters(in: .whitespaces),
                                    noteIDs: ids))
        }
        return (title, subtitle, clusters)
    }

    // MARK: 组装

    static func cleanSection(_ raw: String) -> String {
        var md = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let inner = RX.first(#"^\s*```(?:markdown|md)?\s*\n([\s\S]*)\n\s*```\s*$"#, md) { md = inner.trimmingCharacters(in: .whitespacesAndNewlines) }
        let levels = RX.all(#"(?m)^(#{1,6})\s+\S"#, md).map { $0[1].count }
        guard let lo = levels.min(), lo != 2 else { return md }
        let shift = 2 - lo
        return md.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            guard let g = RX.groups(#"^(#{1,6})\s+"#, String(line)) else { return String(line) }
            let lvl = max(1, min(6, g[1].count + shift))
            return String(repeating: "#", count: lvl) + " " + line.dropFirst(g[0].count)
        }.joined(separator: "\n")
    }

    static func slug(_ text: String, limit: Int = 28) -> String {
        var t = RX.sub(#"["'“”‘’「」『』《》〈〉]"#, "", text.trimmingCharacters(in: .whitespaces))
        t = RX.sub(#"[\s/\\:：，,。.、|｜&]+"#, "_", t)
        t = RX.sub(#"_{2,}"#, "_", t).trimmingCharacters(in: CharacterSet(charactersIn: "_"))
        if t.count > limit {
            let head = String(t.prefix(limit + 1))
            let ends = RX.re(#"_|(?<=[的与和及])"#).matches(in: head, range: NSRange(location: 0, length: (head as NSString).length))
                .map { (head as NSString).substring(to: $0.range.location + $0.range.length).count }
                .filter { Double($0) >= Double(limit) * 0.6 }
            t = String(t.prefix(ends.max() ?? limit))
        }
        let r = t.trimmingCharacters(in: CharacterSet(charactersIn: "_→-—–~·"))
        return r.isEmpty ? "技术洞察报告" : r
    }

    static func shortTitle(_ title: String) -> String {
        let t = RX.sub(#"^技术洞察报告[：:]\s*"#, "", title).trimmingCharacters(in: .whitespaces)
        var head = RX.split(#"[：:]|——|—"#, t)[0].trimmingCharacters(in: .whitespaces)
        if head.count > 20 { head = RX.split(#"[，,；;。]"#, head)[0].trimmingCharacters(in: .whitespaces) }
        if head.count < 4 { head = t }
        return slug(head, limit: 60)
    }

    static func dateRange(_ refs: [Ref]) -> String {
        let ds = refs.map(\.date).filter { !$0.isEmpty }.sorted()
        guard let a = ds.first, let b = ds.last else { return "未注明" }
        return a == b ? a : "\(a) → \(b)"
    }

    static func yamlStr(_ v: String) -> String {
        let s = RX.sub(#"\s*\n+\s*"#, " ", v.trimmingCharacters(in: .whitespacesAndNewlines))
        if s.isEmpty || RX.groups(#"[:#"'\[\]{}&*!|>%@`,\n\r\t]|^[\s-]|\s$"#, s) != nil || ["true", "false", "null", "~"].contains(s.lowercased()) {
            return "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
        }
        return s
    }

    /// 文件夹名：同 apps/notes2insight/output_paths.py 的 topic_directory
    static func topicDirectory(_ topic: String) -> String {
        var name = RX.sub(#"[\x00-\x1f<>:"/\\|?*]"#, "_", topic.trimmingCharacters(in: .whitespaces)).trimmingCharacters(in: CharacterSet(charactersIn: " ."))
        while name.utf8.count > 150 { name.removeLast() }
        return name.isEmpty ? "综合分析" : name
    }

    struct Retrieval { let terms: [(String, Int)]; let scanned: Int; let matched: Int; let candidates: Int; let picked: Int; let dropped: [TopicSearch.Candidate] }

    static func assemble(topic: String, refs: [Ref], framework: String, title rawTitle: String, subtitle: String,
                         clusters: [Cluster], sections: [String], elapsed: TimeInterval, retrieval r: Retrieval, c: LLM.Config) -> (String, String) {
        let ok = refs.filter { !$0.digest.isEmpty }, failed = refs.filter { $0.digest.isEmpty }
        let todayF = DateFormatter(); todayF.dateFormat = "yyyy-MM-dd"
        let today = todayF.string(from: .now)
        let title = rawTitle.isEmpty ? "技术洞察报告：\(topic)" : rawTitle
        let backend = ["anthropic": "Anthropic API", "openrouter": "OpenRouter", "openai_compatible": "第三方 API"][c.provider.rawValue] ?? ""
        let modelLabel = c.model.isEmpty ? "" : "（\(c.model)）"
        let esc = { (s: String) in s.replacingOccurrences(of: "|", with: "\\|") }
        var toc = ["| 章 | 主题 | 核心张力 |", "|---|---|---|"]
        for (i, cl) in clusters.enumerated() {
            toc.append("| 第 \(i + 1) 章 | \(esc(cl.topic)) | \(esc(cl.tension.isEmpty ? (cl.summary.isEmpty ? "—" : cl.summary) : cl.tension)) |")
        }
        var front = ["---", "title: \(yamlStr(title))", "date: \(today)", "range: \(dateRange(refs))",
                     "sources: \(ok.count) 篇笔记" + (failed.isEmpty ? "" : "（\(failed.count) 篇未成功）"),
                     "focus: \(yamlStr(topic))", "depth: \(yamlStr(depth.label))", "topic: \(yamlStr(topic))",
                     "tags: [技术洞察, notes2insight]", "---", "", "# \(title)"]
        if !subtitle.isEmpty { front.append("## \(subtitle)") }
        front += ["", "> 本报告由 notes2insight（手机上生成）基于知识库中 **\(ok.count) 篇**勾选笔记生成，"
                  + "覆盖 \(dateRange(refs))，模型后端 \(backend)\(modelLabel)，生成耗时 \(Int(elapsed) / 60) 分 \(Int(elapsed) % 60) 秒。"
                  + "正文中的 `[N]` 对应文末来源索引；结论均来自笔记材料本身，未引入外部知识。",
                  "", "## 阅读地图", "", toc.joined(separator: "\n"), "", "---", ""]
        let body = sections.map(cleanSection).joined(separator: "\n\n---\n\n")

        var index = ["| 编号 | 笔记 | 日期 | 位置 | 状态 |", "|---|---|---|---|---|"]
        for ref in refs {
            let stem = ((ref.path as NSString).lastPathComponent as NSString).deletingPathExtension
            let t = String(esc(ref.title).prefix(60))
            let cell = t.trimmingCharacters(in: .whitespaces) == stem.trimmingCharacters(in: .whitespaces) ? "[[\(stem)]]" : "[[\(stem)]] \(t)"
            let folder = esc((ref.path as NSString).deletingLastPathComponent.isEmpty ? "." : (ref.path as NSString).deletingLastPathComponent)
            let status = ref.digest.isEmpty ? "⚠️ \(ref.error.isEmpty ? "未生成" : String(ref.error.prefix(40)))" : "✅"
            index.append("| [\(ref.idx)] | \(cell) | \(ref.date.isEmpty ? "—" : ref.date) | \(folder) | \(status) |")
        }
        var detail = ["**检索词**（权重越高越核心）：", ""]
        for w in [3, 2, 1] {
            let got = r.terms.filter { $0.1 == w }.map(\.0)
            if !got.isEmpty { detail.append("- 权重 \(w)：\(got.joined(separator: "、"))") }
        }
        if !r.dropped.isEmpty {
            detail += ["", "**打分靠前但被判定为不相关、未纳入的笔记**：", ""]
            detail += r.dropped.prefix(15).map { "- \($0.title)（相关度 \($0.relevance)：\($0.reason)）" }
        }
        let tail = ["", "---", "", "## 方法、来源与局限", "",
                    "**方法**：按主题「\(topic)」检索：扩展 \(r.terms.count) 个检索词，全文扫描 \(r.scanned) 篇、命中 \(r.matched) 篇，"
                    + "取打分最高的 \(r.candidates) 篇做相关度判定，选入 \(r.picked) 篇 → "
                    + "逐篇笔记生成结构化技术摘要卡 → 跨卡归纳主题簇与全局判断 → 按骨架分章成文。"
                    + "共 \(refs.count) 篇输入，\(ok.count) 篇成功，\(failed.count) 篇失败。深度档位：\(depth.label)（目标篇幅 \(depth.words)）。",
                    "", "**局限**：",
                    "- 结论只反映所选笔记的覆盖面，不代表领域全貌；样本偏向知识库自身的采集偏好。",
                    "- 主题模式下的笔记是关键词检索加模型判定选出来的：换一批关键词可能选出不同材料，词面上没命中但实质相关的笔记会被漏掉。附录里列出了实际用到的检索词，可据此判断召回是否够全。",
                    "- 在手机上生成：只检索了手机上已经下载的笔记，没下载的整理稿和文字记录不在检索范围里。",
                    "- 播客与视频类笔记来自语音转写与 AI 整理，数字与人名可能有误，引用前请回溯原始材料。",
                    "- 摘要卡是有损压缩，细节以原笔记为准；正文 `[N]` 标注用于回溯，不等于逐句引用。",
                    "", "### 来源索引", "", index.joined(separator: "\n"), "",
                    "<details>", "<summary>报告骨架与检索过程（中间结果，供核对）</summary>", "",
                    detail.joined(separator: "\n") + "\n", "", "```markdown",
                    framework.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "```", with: "``\u{200B}`"),
                    "```", "", "</details>", ""]
        let content = front.joined(separator: "\n") + body + "\n" + tail.joined(separator: "\n")
        return (content, "技术洞察报告_\(shortTitle(title))_\(today.replacingOccurrences(of: "-", with: "")).md")
    }

    // MARK: 编排

    /// folder：只在这个文件夹里找（相对笔记库，比如 Spark/硅谷101）。exists：这个相对路径手机上已经有文件了没有
    static func run(topic rawTopic: String, folder: String, lib: LocalNotes, config c: LLM.Config,
                    exists: @Sendable (String) -> Bool, progress: @escaping TopicSearch.Progress) async throws -> ReviewRunner.Output {
        let started = Date()
        let topic = rawTopic.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !topic.isEmpty else { throw SparkError("要填一个主题") }
        let result = try await TopicSearch.find(lib, topic: topic, folder: folder, config: c, progress: progress)
        if !result.insufficient.isEmpty { throw SparkError(result.insufficient) }
        let cands = result.candidates
        var picked = Array(cands.filter { $0.relevance >= 3 }.prefix(reportPick))
        if picked.isEmpty { picked = Array(cands.prefix(5)) }
        guard !picked.isEmpty else { throw SparkError("没有找到相关的笔记") }
        let pickedPaths = Set(picked.map(\.path))
        let refs = picked.enumerated().map { Ref(idx: $0.offset + 1, path: $0.element.path, title: $0.element.title,
                                                 date: $0.element.date, chars: $0.element.chars) }

        // 摘取：同时跑 3 篇；单篇失败只记下来，不中断整份报告
        progress("摘取 0/\(refs.count)")
        try await withThrowingTaskGroup(of: Void.self) { group in
            var next = 0, done = 0
            func add() {
                guard next < refs.count else { return }
                let ref = refs[next]; next += 1
                group.addTask {
                    do { ref.digest = try await digest(ref, lib: lib, focus: topic, c: c) }
                    catch is CancellationError { throw CancellationError() }
                    catch { ref.error = String(error.localizedDescription.prefix(300)) }
                }
            }
            for _ in 0..<concurrency { add() }
            while try await group.next() != nil {
                done += 1
                progress("摘取 \(done)/\(refs.count)")
                add()
            }
        }
        let ok = refs.filter { !$0.digest.isEmpty }
        if ok.isEmpty { throw SparkError("所有笔记的摘要卡都生成失败：\(refs.first?.error ?? "")") }

        // 归纳
        let all = cardsText(ok, budget: Int.max / 2)
        let cards: String
        if all.count > frameworkBatchChars {
            let batches = frameworkBatches(ok)
            var sums: [String] = []
            for (i, b) in batches.enumerated() {
                progress("摘要卡较多，预归并 \(i + 1)/\(batches.count)")
                sums.append(try await LLM.complete(fill(premergePrompt, ["cards": cardsText(b, budget: frameworkBatchChars)]), config: c, maxTokens: 6000))
            }
            cards = sums.enumerated().map { "--- 第 \($0.offset + 1) 批要点 ---\n\($0.element)" }.joined(separator: "\n\n")
        } else {
            cards = all
        }
        progress("归纳报告骨架")
        let framework = try await LLM.complete(fill(frameworkPrompt, ["n": "\(ok.count)", "clusters": "\(depth.clusters)", "cards": cards,
                                                                       "focus_clause": focusClause(topic), "rules": commonRules]),
                                               config: c, maxTokens: 8000)
        var (title, subtitle, clusters) = parseFramework(framework)
        if clusters.isEmpty { clusters = [Cluster(no: 1, topic: topic, summary: "", tension: "", noteIDs: [])] }

        // 成文
        let total = clusters.count + 2
        var sections: [String] = []
        let base = ["rules": commonRules, "focus_clause": focusClause(topic), "framework": framework]
        progress("写报告 1/\(total)：执行摘要与关键判断")
        sections.append(try await LLM.complete(fill(composeHeaderPrompt, base.merging(["cards": cardsText(ok)]) { $1 }), config: c, maxTokens: 8000))
        for (i, cl) in clusters.enumerated() {
            progress("写报告 \(i + 2)/\(total)：第 \(i + 1) 章 \(cl.topic.prefix(20))")
            var chapterCards = cardsText(ok, ids: cl.noteIDs.isEmpty ? nil : Set(cl.noteIDs))
            if chapterCards.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { chapterCards = cardsText(ok) }
            sections.append(try await LLM.complete(fill(composeChapterPrompt, base.merging([
                "n": "\(i + 1)", "topic": cl.topic, "chapter_words": depth.chapterWords, "cards": chapterCards]) { $1 }), config: c, maxTokens: 12000))
        }
        progress("写报告 \(total)/\(total)：分歧、展望与启示")
        sections.append(try await LLM.complete(fill(composeTailPrompt, base.merging(["cards": cardsText(ok)]) { $1 }), config: c, maxTokens: 10000))

        let retrieval = Retrieval(terms: result.terms, scanned: result.scanned, matched: result.matched, candidates: cands.count,
                                  picked: picked.count, dropped: cands.filter { !pickedPaths.contains($0.path) && $0.relevance < 3 })
        let (content, fname) = assemble(topic: topic, refs: refs, framework: framework, title: title, subtitle: subtitle,
                                        clusters: clusters, sections: sections, elapsed: Date().timeIntervalSince(started),
                                        retrieval: retrieval, c: c)
        let dir = "output/\(topicDirectory(topic))"
        var path = "\(dir)/\(fname)"
        if exists(path) {
            let stem = (fname as NSString).deletingPathExtension
            for i in 2..<100 where !exists("\(dir)/\(stem)_\(i).md") { path = "\(dir)/\(stem)_\(i).md"; break }
        }
        progress("报告写好了")
        return ReviewRunner.Output(path: path, title: title.isEmpty ? "技术洞察报告：\(topic)" : title, content: content)
    }
}
