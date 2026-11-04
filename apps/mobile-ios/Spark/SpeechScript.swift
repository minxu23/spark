import Foundation

/// 手机上写整理稿，跟电脑的 pipeline.generate_speech_script 同一套做法：
/// 外语节目先分块整理原文、再逐段翻译成中英对照；中文节目直接整理成中文；RSS 文章只配译文。
/// 手机上没有发言人标注，按没有发言人处理。提示词和正则是从 pipeline.py 生成过来的，
/// tests/test_mobile_speech_parity.py 检查两边一字不差。改了一边要改另一边。
enum SpeechScript {
    struct Result: Sendable {
        let text: String
        let mode: String      // bilingual / zh / original
    }

    struct Failure: LocalizedError {
        let errorDescription: String?
        init(_ message: String) { errorDescription = message }
    }

    typealias Ask = @Sendable (_ prompt: String, _ maxTokens: Int, _ timeout: TimeInterval) async throws -> String

    static let chunkOriginal = 20000, chunkZhFromZh = 7000      // SPEECH_CHUNK_CHARS
    static let translateBatch = 10000                            // TRANSLATE_BATCH_CHARS
    static let retryBatches = [2500, 0]                          // TRANSLATE_RETRY_BATCH_CHARS
    static let parallel = 4                                      // SPEECH_PARALLEL

    // MARK: 提示词（pipeline.py 原文）

    static let tidyMono = #"""
你是文字编辑。下面是一段技术峰会内容（演讲/圆桌/工作坊）的自动语音转写文字记录，
可能带有识别错误、口语化填充词（"um"、"you know"、"like"、重复词、说到一半改口等）。

议题标题：{title}
{speaker_note}

【语言规则，最高优先级】原始文字记录的语言是：{lang_name}。你输出的正文必须逐句使用这同一种语言改写，
禁止翻译成中文或任何其他语言——哪怕本提示词、发言人角色标签（如"主持人""未知发言人"）是中文写的，
发言人实际说出的内容也必须保持 {lang_name} 原文改写，不允许出现翻译后的版本。

请把这段文字记录整理成一篇流畅、可读的演讲稿/文章：
- 正文语言见上方【语言规则】，绝对不要翻译。
- 保留全部实质内容、观点、数据、案例，不要总结、不要删减信息量，只是把口语转成书面语。
- 去掉口语填充词（um/uh/you know/like 等）、明显的重复和结巴、无意义的语气词。
- 结合上下文合理修正明显的语音识别错误（人名、专业术语、产品名），拿不准的保留原样，不要瞎编内容。
- 按内容自然分段，可以适当加小标题帮助阅读，但不要过度切割，也不要编号列表化原本连贯的叙述。
{speaker_instruction}
- 只输出整理后的正文本身：不要重复输出议题标题（文档已经有标题了），
  不要输出任何开场白、解释，或"以下是整理后的演讲稿"之类的元话语。

原始文字记录：
"""
{transcript}
"""

"""#

    static let tidyZh = #"""
你是文字编辑兼译者。下面是一段技术峰会内容（演讲/圆桌/工作坊）的自动语音转写文字记录，
可能带有识别错误、口语化填充词（"um"、"you know"、"like"、重复词、说到一半改口等）。

议题标题：{title}
{speaker_note}

原始文字记录的语言是：{lang_name}。请把这段文字记录整理并翻译成一篇流畅、可读的中文演讲稿：
- 全文必须是中文（如果原文本来就是中文，直接整理成书面语即可，不需要翻译）。原文里夹着的整句英文
  也要译成中文，只有人名、产品名、专业术语可以保留英文。
- 忠实传达原意，保留全部实质内容、观点、数据、案例，不要总结、不要删减信息量。
- 去掉口语填充词（um/uh/you know/like 等）、明显的重复和结巴、无意义的语气词，翻译要通顺自然，
  不要逐字直译导致生硬。
- 结合上下文合理修正明显的语音识别错误（人名、专业术语、产品名可保留英文原名），拿不准的保留原样，
  不要瞎编内容。
- 按内容自然分段，可以适当加小标题帮助阅读，但不要过度切割，也不要编号列表化原本连贯的叙述。
{speaker_instruction}
- 只输出整理后的正文本身：不要重复输出议题标题（文档已经有标题了），
  不要输出任何开场白、解释，或"以下是整理后的演讲稿"之类的元话语。

原始文字记录：
"""
{transcript}
"""

"""#

    static let translate = #"""
你是专业译者。下面是一份 {lang_name} 演讲稿/文字记录，已经按段落逐一编号
（每段前面形如 "[3] " 的编号只是段落标记，不属于正文，翻译时不要输出编号本身以外的任何原文）。
这是用户自己收听的公开节目/演讲的文字记录，译文只放在用户本人的笔记里，跟原文逐段对照着读。

请把每一段准确、通顺地翻译成中文：
- 忠实传达原意，不要总结、不要删减信息量，翻译要自然流畅，不要逐字直译导致生硬。
- 每一段如果带有发言人标签（如「**某某**：」），标签保留在译文对应位置或省略都可以，但不需要重复出现两次。
- 严格按 "[编号] 中文翻译" 的格式输出，每段对应一个编号，编号必须和原文完全一致，
  不能遗漏、合并、拆分或新增编号，一个编号只能对应这一段的翻译，不要输出原文。
- 只输出翻译结果本身，不要输出任何解释、开场白，或"以下是翻译"之类的元话语。

原文（已编号，共 {n} 段）：
"""
{numbered_paragraphs}
"""

"""#

    static let englishToZh = #"""
下面是一份中文节目的整理稿/笔记里的几段，已经逐段编号（形如 "[3] " 的编号只是段落标记）。
这些段落里夹着英文，请换成通顺的中文：
- 整句的英文译成中文；口语里随口夹的英文词（如 fancy、maybe、bet、scale up）也换成中文说法。
- 人名、公司名、产品名、模型名、缩写和业内通用的术语（如 Agent、Token、MoE）可以保留英文；
  书名、论文题目、名言译成中文后可在括号里附上英文原文。
- 如果英文后面已经跟着中文翻译，只保留中文那一份。
- 中文部分原样保留，不要改写、删减或总结；Markdown 标记（#、**、列表符号、链接、时间戳）和
  段首的发言人标签（如「**某某**：」）原样保留。
- 严格按 "[编号] 改好的段落" 输出，编号和原文一一对应，不要输出解释。

共 {n} 段：
"""
{numbered_paragraphs}
"""

"""#

    static let refusalReminder = #"（这是用户自己收听的公开节目/演讲的文字记录，整理后只放在用户本人的笔记里，代替听原声来阅读。请直接输出整理后的正文。）"#

    static let keepLanguageReminder = #"IMPORTANT: The transcript above is not in Chinese. Rewrite it in the SAME language as the transcript. Do NOT translate it into Chinese; the output must contain no Chinese."#

    // MARK: 正则（pipeline.py 原文）

    static let refusalPatterns: [(pattern: String, ignoreCase: Bool)] = [
        (#"(?:sorry|can(?:'|’)?t|cannot|unable|not able|won(?:'|’)?t)\b[^.\n]{0,80}?\b(?:produce|provide|translate|help with|complete|generate|rewrite|go ahead|work from|reproduce|render)\b[^.\n]{0,60}?\b(?:this|the|that|these)\b[^.\n]{0,40}?\b(?:transcript|clean-?up|cleaned|request|text|article|version|segment|translation|document|excerpt)"#, true),
        (#"^\W*(?:I'?m sorry|Sorry)[,.]?\s*(?:but\s*)?I (?:can(?:'|’)?t|cannot|won(?:'|’)?t) (?:help with|fulfill|assist with) (?:this|that)\b"#, true),
        (#"^\W*I (?:can(?:'|’)?t|cannot|won(?:'|’)?t|am not able to|'m not able to|'m unable to) (?:produce|provide|translate|complete|generate|rewrite|reproduce|render|clean up) (?:this|that|it|the)\b"#, true),
        (#"(?:抱歉|无法|不能)[^。\n]{0,20}?(?:翻译|整理|生成|提供|完成|复现|处理)[^。\n]{0,20}?(?:这份|这段|整理|全文|文字|转录|内容)"#, false),
    ]
    static let edgeNotePattern = #"[*_]*[（(]\s*(?:注[:：]\s*)?(?:原文|记录|文字记录|此处记录)[^）)\n]{0,12}(?:截断|中断)[^）)\n]{0,6}[）)][*_]*"#
    static let englishSentencePattern = #"(?:[A-Za-z][A-Za-z0-9'’\-]*[ ,]+){5,}[A-Za-z][A-Za-z0-9'’\-]*"#
    static let englishWordPattern = #"(?<![A-Za-z0-9'’\-])[a-z][a-z'’\-]{2,}(?![A-Za-z0-9])"#
    static let linkTargetPattern = #"\]\(([^)]*)\)|https?://\S+"#
    static let labelColonPattern = #"^((?:>\s*)?\*\*[^*\n]+\*\*)[ \t]*:[ \t]*"#
    static let halfPunctPattern = #"(?<=[\u4e00-\u9fff”」』）])[ \t]*([,:;?!])[ \t]*|([,:;?!])[ \t]*(?=[\u4e00-\u9fff“「『（])"#
    static let speakerLabelPattern = #"^\*\*([^*\n]+)\*\*\s*[：:]"#

    private static func rx(_ pattern: String, _ options: NSRegularExpression.Options = []) -> NSRegularExpression {
        // 上面的正则都是写死的，并且由测试编译过
        try! NSRegularExpression(pattern: pattern, options: options)
    }
    private static let refusals = refusalPatterns.map { rx($0.pattern, $0.ignoreCase ? [.caseInsensitive] : []) }
    private static let edgeNote = rx(edgeNotePattern)
    private static let englishSentence = rx(englishSentencePattern)
    private static let englishWord = rx(englishWordPattern)
    private static let linkTarget = rx(linkTargetPattern)
    private static let labelColon = rx(labelColonPattern, [.anchorsMatchLines])
    private static let halfPunct = rx(halfPunctPattern)
    private static let speakerLabel = rx(speakerLabelPattern)
    private static let numberMark = rx(#"^[ \t]*\[(\d+)\][ \t]*"#, [.anchorsMatchLines])
    private static let blankLines = rx(#"\n\s*\n+"#)
    private static let speakerLine = rx(#"^\[[^\]\n]+\]$"#)
    private static let fullWidth: [String: String] = [",": "，", ":": "：", ";": "；", "?": "？", "!": "！"]

    // MARK: 入口

    /// 返回 nil：这一期不用写（比如中文的 RSS 文章，或者正文是空的）
    static func generate(title: String, paragraphs: [(Double, String)], lang: String, sourceType: String?, ask: @escaping Ask) async throws -> Result? {
        let name = langName(lang)
        let zh = lang.split(separator: "-").first?.lowercased() == "zh"
        if sourceType == "rss" {
            if zh { return nil }
            let paras = paragraphs.map { trim($0.1) }.filter { !$0.isEmpty }
            if paras.isEmpty { return nil }
            return Result(text: interleave(paras, try await translate(paras, langName: name, ask: ask)), mode: "bilingual")
        }
        if zh {
            let chunks = buildChunks(paragraphs, maxChars: chunkZhFromZh)
            var text = try await script(title: title, chunks: chunks, template: tidyZh, langName: name, maxTokens: 10000, timeout: 700,
                                        checkLanguage: false, minRatio: 0.6, ask: ask)
            text = try await translateEnglishStretches(text, ask: ask)
            let out = zhPunctuation(text)
            return trim(out).isEmpty ? nil : Result(text: out, mode: "zh")
        }
        let chunks = buildChunks(paragraphs, maxChars: chunkOriginal)
        let original = try await script(title: title, chunks: chunks, template: tidyMono, langName: name, maxTokens: 8000, timeout: 600,
                                        checkLanguage: true, minRatio: 0, ask: ask)
        let paras = splitParagraphs(original)
        if paras.isEmpty { return nil }
        do {
            return Result(text: interleave(paras, try await translate(paras, langName: name, ask: ask)), mode: "bilingual")
        } catch {
            if Task.isCancelled || error is CancellationError { throw error }
            return Result(text: original, mode: "original")      // 翻译失败也不丢掉整理好的原文
        }
    }

    // MARK: 整理（pipeline._script_from_chunks）

    /// pipeline.build_transcript_chunks（没有发言人时）
    static func buildChunks(_ paragraphs: [(Double, String)], maxChars: Int) -> [String] {
        var chunks: [String] = [], cur: [String] = [], size = 0
        for (_, text) in paragraphs {
            let add = len(text) + 1
            if !cur.isEmpty, maxChars > 0, size + add > maxChars {
                chunks.append(cur.joined(separator: "\n"))
                cur = []; size = 0
            }
            cur.append(text)
            size += add
        }
        if !cur.isEmpty { chunks.append(cur.joined(separator: "\n")) }
        return chunks
    }

    private static func script(title: String, chunks: [String], template: String, langName: String, maxTokens: Int, timeout: TimeInterval,
                               checkLanguage: Bool, minRatio: Double, ask: @escaping Ask) async throws -> String {
        let n = chunks.count
        let parts = try await mapParallel(Array(chunks.enumerated())) { k, chunk -> String in
            let note = n > 1 ? "（这是整篇文字记录的第 \(k + 1)/\(n) 部分，前后还有别的部分另外整理、最后拼在一起：直接从这部分的第一句整理起，不要加开场白、过渡语或总结。这部分可能从半句话开始、在半句话结束，照原样整理即可，不要加「原文在此处截断」之类的说明。）" : ""
            var (prompt, out) = try await tidy(chunk, title: title, note: note, template: template, langName: langName,
                                               maxTokens: maxTokens, timeout: timeout, split: true, ask: ask)
            if checkLanguage, lostLanguage(chunk, out) {
                out = trim(try await ask(prompt + "\n" + keepLanguageReminder, maxTokens, timeout))
                if lostLanguage(chunk, out) { throw Failure("整理出来的原文语言不对（重试了一次还是被翻译成了中文）") }
            }
            let source = cjkCount(chunk)
            if minRatio > 0, source > 0, Double(cjkCount(out)) < Double(source) * minRatio {
                let again = trim(try await ask(prompt + "\n（上一次整理只剩原文的一小部分，删得太多了。这是整理不是摘要：原文约 \(source) 字，整理后应该在 \(Int(Double(source) * 0.8)) 字以上，逐段保留每一句有内容的话。）", maxTokens, timeout))
                if cjkCount(again) > cjkCount(out) { out = again }
            }
            prompt = ""
            return trim(replace(edgeNote, in: out, with: ""))
        }
        return parts.filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    /// 模型拒绝，或者一大块只回几个字：带上说明再来一次，还不行拆成两半，哪一半还不行就留下原文
    private static func tidy(_ chunk: String, title: String, note: String, template: String, langName: String, maxTokens: Int, timeout: TimeInterval,
                             split: Bool, ask: @escaping Ask) async throws -> (prompt: String, text: String) {
        func bad(_ out: String) -> Bool {
            looksLikeRefusal(out) || (len(chunk) > 2000 && Double(len(out)) < Double(len(chunk)) * 0.05)
        }
        let prompt = fill(template, ["title": title, "speaker_note": note, "lang_name": langName, "speaker_instruction": "", "transcript": chunk])
        var out = trim(try await ask(prompt, maxTokens, timeout))
        if !bad(out) { return (prompt, out) }
        out = trim(try await ask(prompt + "\n" + refusalReminder, maxTokens, timeout))
        if !bad(out) { return (prompt, out) }
        let lines = chunk.components(separatedBy: "\n")
        if !split || lines.count < 2 { return (prompt, rawChunkText(chunk)) }
        let half = lines.count / 2
        let head = try await tidy(lines[..<half].joined(separator: "\n"), title: title, note: note, template: template, langName: langName,
                                  maxTokens: maxTokens, timeout: timeout, split: false, ask: ask)
        let tail = try await tidy(lines[half...].joined(separator: "\n"), title: title, note: note, template: template, langName: langName,
                                  maxTokens: maxTokens, timeout: timeout, split: false, ask: ask)
        return (prompt, [head.text, tail.text].filter { !$0.isEmpty }.joined(separator: "\n\n"))
    }

    /// pipeline._raw_chunk_text
    static func rawChunkText(_ chunk: String) -> String {
        var out: [String] = [], speaker = ""
        for line in chunk.components(separatedBy: "\n") {
            let t = trim(line)
            if first(speakerLine, t) != nil {
                speaker = String(t.dropFirst().dropLast())
            } else if !t.isEmpty {
                out.append(speaker.isEmpty ? t : "**\(speaker)**: \(t)")
                speaker = ""
            }
        }
        return out.joined(separator: "\n\n")
    }

    /// pipeline.looks_like_refusal：只看开头，正文里引用别人说「我不能……」不算
    static func looksLikeRefusal(_ text: String) -> Bool {
        let head = String(trim(text).prefix(200))
        return refusals.contains { r in
            guard let m = first(r, head), let range = Range(m.range, in: head) else { return false }
            return head.distance(from: head.startIndex, to: range.lowerBound) < 60
        }
    }

    private static func lostLanguage(_ source: String, _ out: String) -> Bool { cjkShare(source) < 0.1 && cjkShare(out) > 0.3 }

    // MARK: 翻译（pipeline._translate_speech_paragraphs）

    /// {段落序号(从 1 起): 译文}。漏掉的段落拆成小批补译，补完还缺的不在结果里
    static func translate(_ paragraphs: [String], langName: String, ask: @escaping Ask) async throws -> [Int: String] {
        @Sendable func run(_ batch: [(Int, String)]) async throws -> ([Int: String], Error?) {
            let numbered = batch.enumerated().map { "[\($0.offset + 1)] \($0.element.1)" }.joined(separator: "\n\n")
            let prompt = fill(translate, ["lang_name": langName, "n": String(batch.count), "numbered_paragraphs": numbered])
            do {
                let got = parseNumbered(try await ask(prompt, 12000, 700))
                var out: [Int: String] = [:]
                for (j, t) in got where j >= 1 && j <= batch.count && !t.isEmpty {
                    // 拒绝话前面也可能带着编号，不能当译文。原文本身就是这句话时才算翻译
                    if !looksLikeRefusal(t) || looksLikeRefusal(batch[j - 1].1) { out[batch[j - 1].0] = t }
                }
                return (out, nil)
            } catch {
                if Task.isCancelled || error is CancellationError { throw error }
                return ([:], error)      // 一批报错不连累别的批次，后面拆小了再试
            }
        }
        let items = paragraphs.enumerated().map { ($0.offset + 1, $0.element) }
        var result: [Int: String] = [:]
        var lastError: Error?
        for (part, error) in try await mapParallel(batches(items, maxChars: translateBatch), run) {
            result.merge(part) { $1 }
            if let error { lastError = error }
        }
        if let lastError, result.isEmpty { throw lastError }    // 第一轮一批都没成，多半是额度、密钥的问题
        for maxChars in retryBatches {
            let missing = items.filter { result[$0.0] == nil }
            if missing.isEmpty { break }
            for (part, _) in try await mapParallel(batches(missing, maxChars: maxChars), run) { result.merge(part) { $1 } }
        }
        return result
    }

    static func batches(_ items: [(Int, String)], maxChars: Int) -> [[(Int, String)]] {
        var out: [[(Int, String)]] = [], cur: [(Int, String)] = [], size = 0
        for item in items {
            if !cur.isEmpty, size + len(item.1) > maxChars {
                out.append(cur)
                cur = []; size = 0
            }
            cur.append(item)
            size += len(item.1)
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    /// pipeline.translate_english_stretches：中文稿里夹着的英文换成中文，其余段落不动
    static func translateEnglishStretches(_ text: String, ask: @escaping Ask) async throws -> String {
        var paras = splitParagraphs(text)
        let targets = paras.enumerated().filter { !trim($0.element).isEmpty && !$0.element.hasPrefix("# ") && hasEnglish($0.element) }
            .map { ($0.offset + 1, $0.element) }
        if targets.isEmpty { return text }
        let parts = try await mapParallel(batches(targets, maxChars: translateBatch)) { batch -> [Int: String] in
            let numbered = batch.enumerated().map { "[\($0.offset + 1)] \($0.element.1)" }.joined(separator: "\n\n")
            let got = parseNumbered(try await ask(fill(englishToZh, ["n": String(batch.count), "numbered_paragraphs": numbered]), 8000, 700))
            var out: [Int: String] = [:]
            for (j, t) in got where j >= 1 && j <= batch.count && !t.isEmpty { out[batch[j - 1].0] = t }
            return out
        }
        for part in parts {
            for (i, fixed) in part {
                let old = paras[i - 1], new = trim(fixed)
                if cjkCount(new) >= cjkCount(old), linkTargets(new) == linkTargets(old) {
                    paras[i - 1] = zhPunctuation(keepSpeakerLabel(trim(old), new))
                }
            }
        }
        return paras.joined(separator: "\n\n")
    }

    private static func hasEnglish(_ text: String) -> Bool {
        let t = replace(linkTarget, in: text, with: "")
        return first(englishSentence, t) != nil || first(englishWord, t) != nil
    }

    private static func linkTargets(_ text: String) -> [String] {
        let range = NSRange(text.startIndex..., in: text)
        return linkTarget.matches(in: text, range: range).map { m in
            Range(m.range(at: 1), in: text).map { String(text[$0]) } ?? ""
        }
    }

    // MARK: 文字处理（跟 pipeline 里同名的函数一样）

    static func splitParagraphs(_ text: String) -> [String] {
        let t = trim(text)
        let range = NSRange(t.startIndex..., in: t)
        var out: [String] = [], start = t.startIndex
        for m in blankLines.matches(in: t, range: range) {
            guard let r = Range(m.range, in: t) else { continue }
            out.append(trim(String(t[start..<r.lowerBound])))
            start = r.upperBound
        }
        out.append(trim(String(t[start...])))
        return out.filter { !$0.isEmpty }
    }

    /// 只认行首的编号，而且编号必须递增：正文里的脚注 "[2]" 不是段落标记
    static func parseNumbered(_ raw: String) -> [Int: String] {
        let range = NSRange(raw.startIndex..., in: raw)
        var marks: [(n: Int, start: String.Index, end: String.Index)] = [], last = 0
        for m in numberMark.matches(in: raw, range: range) {
            guard let whole = Range(m.range, in: raw), let g = Range(m.range(at: 1), in: raw), let n = Int(raw[g]), n > last else { continue }
            marks.append((n, whole.lowerBound, whole.upperBound))
            last = n
        }
        var out: [Int: String] = [:]
        for (i, m) in marks.enumerated() {
            let end = i + 1 < marks.count ? marks[i + 1].start : raw.endIndex
            out[m.n] = trim(String(raw[m.end..<end]))
        }
        return out
    }

    static func zhPunctuation(_ text: String) -> String {
        let t = labelColon.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "$1：")
        var out = "", tail = t.startIndex
        for m in halfPunct.matches(in: t, range: NSRange(t.startIndex..., in: t)) {
            guard let whole = Range(m.range, in: t) else { continue }
            let g = Range(m.range(at: 1), in: t) ?? Range(m.range(at: 2), in: t)
            out += t[tail..<whole.lowerBound]
            out += g.flatMap { fullWidth[String(t[$0])] } ?? String(t[whole])
            tail = whole.upperBound
        }
        return out + t[tail...]
    }

    static func keepSpeakerLabel(_ original: String, _ translation: String) -> String {
        guard let src = first(speakerLabel, original), let dst = first(speakerLabel, translation),
              let a = Range(src.range(at: 1), in: original), let b = Range(dst.range(at: 1), in: translation),
              let end = Range(dst.range, in: translation)?.upperBound, original[a] != translation[b] else { return translation }
        let name = String(original[a])
        var bare = name
        while let c = bare.last, c.isWhitespace { bare.removeLast() }
        if len(name) > 40 || (bare.last.map { ".?!。？！".contains($0) } ?? false) { return translation }
        return "**\(name)**：" + translation[end...]
    }

    static func translationBlock(_ original: String, _ translation: String) -> String {
        let t = keepSpeakerLabel(original, zhPunctuation(translation))
        return t.isEmpty ? "" : t.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n").map { "> \($0)" }.joined(separator: "\n")
    }

    static func interleave(_ paras: [String], _ translations: [Int: String]) -> String {
        var out: [String] = []
        for (i, p) in paras.enumerated() {
            out.append(p)
            let block = translationBlock(p, translations[i + 1] ?? "")
            if !block.isEmpty { out.append(block) }
        }
        return out.joined(separator: "\n\n")
    }

    static func langName(_ lang: String) -> String {
        let names = ["en": "英语", "zh": "中文", "ja": "日语", "ko": "韩语", "es": "西班牙语", "fr": "法语", "de": "德语", "pt": "葡萄牙语",
                     "ru": "俄语", "it": "意大利语", "nl": "荷兰语", "sv": "瑞典语", "pl": "波兰语", "tr": "土耳其语", "vi": "越南语",
                     "th": "泰语", "id": "印尼语", "hi": "印地语", "ar": "阿拉伯语", "he": "希伯来语", "uk": "乌克兰语"]
        let base = lang.split(separator: "-").first.map { $0.lowercased() } ?? ""
        return names[base] ?? (lang.isEmpty ? "原字幕语言" : lang)
    }

    // MARK: 小工具

    /// 按字符数算，跟 Python 的 len 一样（emoji 算一个，组合字符分开算）
    private static func len(_ s: String) -> Int { s.unicodeScalars.count }
    private static func trim(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }
    private static func first(_ r: NSRegularExpression, _ s: String) -> NSTextCheckingResult? {
        r.firstMatch(in: s, range: NSRange(s.startIndex..., in: s))
    }
    private static func replace(_ r: NSRegularExpression, in s: String, with template: String) -> String {
        r.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: template)
    }
    private static func isCJK(_ u: Unicode.Scalar) -> Bool { (0x4e00...0x9fff).contains(u.value) }
    private static func cjkCount(_ s: String) -> Int { s.unicodeScalars.reduce(0) { $0 + (isCJK($1) ? 1 : 0) } }
    private static func cjkShare(_ s: String) -> Double {
        let latin = s.unicodeScalars.reduce(0) { $0 + (((0x41...0x5a).contains($1.value) || (0x61...0x7a).contains($1.value)) ? 1 : 0) }
        let cjk = cjkCount(s)
        return cjk + latin == 0 ? 0 : Double(cjk) / Double(cjk + latin)
    }

    /// 一遍替换完 {名字}，字幕里碰巧写着 {title} 也不会被再替换一次
    private static func fill(_ template: String, _ values: [String: String]) -> String {
        var out = "", rest = Substring(template)
        while let open = rest.firstIndex(of: "{"), let close = rest[open...].firstIndex(of: "}") {
            out += rest[..<open]
            let key = String(rest[rest.index(after: open)..<close])
            out += values[key] ?? String(rest[open...close])
            rest = rest[rest.index(after: close)...]
        }
        return out + rest
    }

    private static func mapParallel<T: Sendable, R: Sendable>(_ items: [T], _ fn: @escaping @Sendable (T) async throws -> R) async throws -> [R] {
        var out = [R?](repeating: nil, count: items.count)
        try await withThrowingTaskGroup(of: (Int, R).self) { group in
            var next = 0
            func add() {
                guard next < items.count else { return }
                let i = next, item = items[i]
                next += 1
                group.addTask { (i, try await fn(item)) }
            }
            for _ in 0..<parallel { add() }
            while let (i, r) = try await group.next() {
                out[i] = r
                add()
            }
        }
        return out.compactMap { $0 }
    }
}
