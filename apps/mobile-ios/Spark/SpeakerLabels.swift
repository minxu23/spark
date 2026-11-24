import Foundation

/// 手机上推测发言人，跟电脑的 pipeline.infer_speakers / apply_host_name / guess_single_speaker 同一套：
/// 分批让模型给每段标「编号: 发言人」，主持人姓名换进去，标题像「人名 - 标题」的单人视频直接取标题里的名字。
/// 提示词、规则和正则是从 pipeline.py、session_types.py 生成过来的，
/// tests/test_mobile_speaker_labels.py 检查两边一字不差，还拿同样的输入跑两边比结果。改了一边要改另一边。
/// 推测不出来（出错、格式不对、全是「未知发言人」）时单集照常存，只是不分人。
enum SpeakerLabels {
    struct Result: Sendable, Equatable {
        let mode: String          // single：标题里的单人演讲；multi：推测出来的多人对话
        let speakers: [String]    // 每段一个，跟段落一一对应
    }

    enum Outcome: Sendable, Equatable {
        case labeled(Result)
        case skipped              // 这期不用标（单集不是对谈、文章来源、简讯）
        case unknown              // 推测了，但每一段都是「未知发言人」：不写标签
        case failed(String)       // 模型出错或返回的格式认不出来

        /// 给用户看的「没推测出来」：只有真的试过而没成才算
        var missed: Bool {
            switch self {
            case .unknown, .failed: true
            case .labeled, .skipped: false
            }
        }
    }

    typealias Ask = SpeechScript.Ask

    static let batchChars = 100000     // SPEAKER_LABEL_BATCH_CHARS（手机只接云端模型，没有本地 Ollama 那一档）
    static let batchParas = 200        // SPEAKER_LABEL_BATCH_PARAS
    static let unknown = "未知发言人"
    static let multiTypes = ["panel", "fireside", "roundtable", "discussion", "qa"]   // session_types.MULTI_SPEAKER_TYPES

    // MARK: 提示词（pipeline.py 原文）

    static let labelPrompt = #"""
你是转写编辑，需要给一段多人对话（圆桌/炉边谈话/工作坊/路演）的文字记录标注每一段发言可能是谁说的。

议题标题：{title}{show_line}

视频简介（可能列出了嘉宾名单，若为空可忽略）：
"""
{description}
"""

下面是按顺序编号的文字记录段落。请结合视频简介里的嘉宾名单、以及文字记录里的上下文线索
（自我介绍、被主持人点名、"谢谢xx，那xx怎么看"这类过渡句等）判断每一段最可能是谁在说话。

规则：
- 能确定具体姓名就写姓名（优先用视频简介里给出的全名）。
- 文字记录是语音识别出来的，人名常被听成同音字（比如主持人自我介绍"我是小X"里的名字）。写姓名时以
  上面给出的主持人姓名、议题标题、视频简介里的写法为准，不要照抄识别错的同音字。
- 只能确定角色但不确定姓名，就写角色，例如"主持人"。
- 实在无法判断，写"未知发言人"。
- 相邻段落如果明显是同一个人接着说，就标注同一个名字/角色，不要每段都换人。
- 这是基于文字线索的推测，不是精确的说话人识别，给出你最合理的判断即可，不用每段都非常确信。

下面给的是第 {lo} 到第 {hi} 段（共 {count} 段）。每段输出一行，编号照抄段落前方括号里的编号，
一段都不要漏，不要输出其他任何内容（不要开场白、不要解释）：
{lo}: <发言人>
{lo_next}: <发言人>
...
{hi}: <发言人>

文字记录段落：
{numbered_paragraphs}

"""#

    static let hostLabelPrompt = #"""
下面是一期播客文字记录里标出的几个发言人，以及每人开头说的几段话。文字记录是语音识别出来的，
主持人的名字可能被听成了同音字（比如把"小珺"听成"小俊"）。

节目：{show_title}
主持人：{host_name}

{samples}

哪个标签是主持人 {host_name}？只回答标签本身（照抄上面的写法）；都不是就回答"无"。
"""#

    /// 整理稿提示词里的发言人规则（pipeline._speech_speaker_rule），{lang_name} 换成原文语言
    static let ruleZh = #"- 这是多人对话，请在发言人切换时用「**发言人名**：」的格式另起一段标出（可参考原文字记录里方括号标出的发言人，这些角色标签本身可能已经是中文，直接沿用即可）。"#
    static let ruleOriginal = #"- 这是多人对话，请在发言人切换时用「**发言人名**：」的格式另起一段标出，方便阅读（可参考原文字记录里方括号标出的发言人；这些角色标签本身可能是中文，直接沿用即可，但标签后面引用的发言内容仍必须是 {lang_name} 原文，不要翻译）。"#

    // MARK: 正则、规则（pipeline.py、session_types.py 原文）

    static let singleNamePattern = #"^([A-Z][^\-–:]{1,60}?)\s*[-–:]\s+\S"#
    static let hostSplitPattern = #"\s*[,，、;；/]\s*"#
    static let labelLinePattern = #"^(\s*)\*\*([^*\n]{1,40})\*\*(\s*[：:])"#
    static let sessionRules: [(kind: String, pattern: String)] = [
        ("fireside", #"\bfireside(?:\s+chat)?\b|炉边(?:对话|谈话|访谈)"#),
        ("panel", #"(^|[:：–—])\s*panel\b(?![-\s]+(?:level|packaging|manufacturing)\b)|\bpanel\s+(?:discussion|of\s+experts|on)\b|\bpanel\s*$|专家讨论|小组讨论"#),
        ("roundtable", #"\bround[\s-]*table\b|圆桌"#),
        ("qa", #"\bq\s*&\s*a\b|\bquestions\s+and\s+answers\b|问答"#),
        ("discussion", #"\b(?:open\s+discussion|in\s+conversation|conversation\s+with|dialogue|interview)\b|对谈|对话|访谈"#),
        ("workshop", #"^\s*workshop\s*[:：–—-]|工作坊"#),
        ("spotlight", #"^\s*startup\s+spotlight\s*[:：–—-]"#),
    ]

    private static func rx(_ pattern: String, _ options: NSRegularExpression.Options = []) -> NSRegularExpression {
        // 上面的正则都是写死的，并且由测试编译过
        try! NSRegularExpression(pattern: pattern, options: options)
    }
    private static let singleName = rx(singleNamePattern)
    private static let hostSplit = rx(hostSplitPattern)
    private static let labelLine = rx(labelLinePattern, [.anchorsMatchLines])
    private static let rules = sessionRules.map { ($0.kind, rx($0.pattern, [.caseInsensitive])) }
    private static let titlePrefix = rx(#"^\s*(?:\[\d+\]|\d{4,6})\s*"#)
    private static let parseLine = rx(#"\s*(\d+)\s*[:：]\s*(.+?)\s*$"#)
    private static let parenTail = rx(#"\s*[（(][^）)]*[）)]$"#)
    private static let bracketed = rx(#"^[\[【](.+)[\]】]$"#)
    private static let rolePrefix = rx(#"^发言人\s*[：:]?\s*(?=.)"#)

    // MARK: 这一期要不要标、怎么标

    enum Plan: Equatable {
        case single(String)       // 标题里就有名字
        case multi                // 让模型推测
        case none                 // 不标
    }

    /// session_types.from_title：议题类型，认不出来是 talk
    static func sessionKind(_ title: String) -> String {
        let t = replace(titlePrefix, in: title, with: "")
        return rules.first { first($0.1, t) != nil }?.0 ?? "talk"
    }

    /// pipeline.guess_single_speaker：标题是「人名 - 标题」的单人演讲，直接取名字
    static func guessSingleSpeaker(_ title: String) -> String? {
        let t = trim(title)
        if t.isEmpty || sessionKind(t) != "talk" { return nil }
        guard let m = first(singleName, t), let r = Range(m.range(at: 1), in: t) else { return nil }
        let name = trim(String(t[r]))
        let words = name.split(whereSeparator: \.isWhitespace).count
        return (1...5).contains(words) ? name : nil
    }

    /// dialogue：节目按对谈处理（订阅的默认；电脑上 dialogue_series），每集都当多人对话，不看标题。
    /// 不是对谈：先看标题是不是单人演讲，再看标题像不像 Panel、访谈这类（电脑上 session_types.is_multi_speaker）
    static func plan(title: String, dialogue: Bool) -> Plan {
        if dialogue { return .multi }
        if let name = guessSingleSpeaker(title) { return .single(name) }
        return multiTypes.contains(sessionKind(title)) ? .multi : .none
    }

    // MARK: 主持人姓名

    /// pipeline.parse_host_names
    static func parseHostNames(_ hostName: String) -> [String] {
        var names: [String] = []
        var start = hostName.startIndex
        var parts: [String] = []
        for m in hostSplit.matches(in: hostName, range: NSRange(hostName.startIndex..., in: hostName)) {
            guard let r = Range(m.range, in: hostName) else { continue }
            parts.append(String(hostName[start..<r.lowerBound]))
            start = r.upperBound
        }
        parts.append(String(hostName[start...]))
        for p in parts {
            let n = trim(p)
            if !n.isEmpty, !names.contains(n) { names.append(n) }
        }
        return names
    }

    /// pipeline.host_prompt_line
    static func hostPromptLine(_ hostName: String) -> String {
        let hosts = parseHostNames(hostName)
        if hosts.isEmpty { return "" }
        if hosts.count == 1 { return "\n主持人：\(hosts[0])（主持人的发言一律标成这个名字）" }
        return "\n固定主持人：\(hosts.joined(separator: "、"))（这几位每期都在；他们的发言标成对应的名字，照抄这里的写法，不要只写「主持人」）"
    }

    // MARK: 推测（pipeline.infer_speakers）

    /// {段落编号(从 1 起): 发言人}。分批：每批最多 batchChars 字、batchParas 段，后一批带上前面认出来的名字。
    /// 模型出错就抛错；返回的格式认不出来就是空
    static func infer(paragraphs: [(Double, String)], title: String, description: String, showTitle: String,
                      hostName: String, ask: @escaping Ask) async throws -> [Int: String] {
        if paragraphs.isEmpty { return [:] }
        var batches: [[(Int, String)]] = [], cur: [(Int, String)] = [], size = 0
        for (k, p) in paragraphs.enumerated() {
            let line = "[\(k + 1)] \(p.1)"
            if !cur.isEmpty, size + len(line) + 2 > batchChars || cur.count >= batchParas {
                batches.append(cur)
                cur = []; size = 0
            }
            cur.append((k + 1, line))
            size += len(line) + 2
        }
        if !cur.isEmpty { batches.append(cur) }

        var labels: [Int: String] = [:], order: [Int] = []
        let hosts = parseHostNames(hostName)
        var known = hosts
        var showLine = !showTitle.isEmpty && title.range(of: showTitle, options: .literal) == nil ? "\n所属节目/会议：\(showTitle)" : ""
        showLine += hostPromptLine(hostName)
        // 只有一位主持人时，模型写的「主持人」能直接换成姓名；几位的话分不清是谁，留给模型按名字标
        let sole = hosts.count == 1 ? hosts[0] : ""
        for batch in batches {
            var desc = String(String.UnicodeScalarView(description.unicodeScalars.prefix(3000)))
            if !known.isEmpty {
                desc += "\n\n（这是同一场对话的后半部分，前面已经识别出这些发言人，请沿用同样的写法：" + known.joined(separator: "、") + "）"
            }
            let lo = batch[0].0, hi = batch[batch.count - 1].0
            let prompt = SpeechScript.fill(labelPrompt, [
                "title": title, "description": desc, "lo": String(lo), "hi": String(hi), "lo_next": String(lo + 1),
                "count": String(batch.count), "show_line": showLine,
                "numbered_paragraphs": batch.map(\.1).joined(separator: "\n\n")])
            var got = parse(try await ask(prompt, 4000, 300), lo: lo, hi: hi)
            if Double(got.count) < 0.8 * Double(batch.count) {
                // 回答缺了一大截（先写了一段解释、只答了一部分）：重问一次，取答得全的那次
                let retry = parse(try await ask(prompt + "\n\n（注意：从 \(lo) 到 \(hi) 每段一行，直接从「\(lo):」开始写，不要任何说明。）", 4000, 300), lo: lo, hi: hi)
                if retry.count > got.count { got = retry }
            }
            for (n, name) in got {
                if labels[n] == nil { order.append(n) }
                labels[n] = !sole.isEmpty && name.hasPrefix("主持人") ? sole : name
            }
            for n in order {
                let name = labels[n] ?? ""
                if !name.isEmpty, name != unknown, !known.contains(name) { known.append(name) }
            }
        }
        return labels
    }

    /// 模型的回答 → [(编号, 发言人)]，按第一次出现的顺序，同一个编号写了几次取最后一次。格式不对的行丢掉
    static func parse(_ raw: String, lo: Int, hi: Int) -> [(Int, String)] {
        var pairs: [(Int, String)] = []
        for line in raw.components(separatedBy: .newlines) {
            guard let m = first(parseLine, line), let a = Range(m.range(at: 1), in: line), let b = Range(m.range(at: 2), in: line),
                  let n = Int(line[a]) else { continue }
            pairs.append((n, trim(String(line[b]))))
        }
        var keep: [(Int, String)] = []
        func put(_ n: Int, _ name: String) {
            if let i = keep.firstIndex(where: { $0.0 == n }) { keep[i].1 = name } else { keep.append((n, name)) }
        }
        if lo > 1, pairs.first?.0 == 1 {
            // 模型把这一批从 1 重新编号了：按顺序换算回全文编号
            for (n, name) in pairs where n >= 1 && n <= hi - lo + 1 { put(lo - 1 + n, name) }
        } else {
            for (n, name) in pairs where n >= lo && n <= hi { put(n, name) }
        }
        return keep
    }

    /// pipeline._speech_speaker_names：推测出来的发言人（按出场顺序去重，不含「未知发言人」）
    static func names(_ speakers: [String]) -> [String] {
        var out: [String] = []
        for n in speakers where !n.isEmpty && n != unknown && !out.contains(n) { out.append(n) }
        return out
    }

    /// pipeline.apply_host_name：「主持人」「主持人（小俊）」换成节目设置里的主持人姓名；
    /// 没有这类标签时（主持人被标成了听错的名字），问一次模型哪个标签是主持人
    static func applyHostName(_ speakers: [String], paragraphs: [(Double, String)], hostName: String, showTitle: String,
                              ask: @escaping Ask) async throws -> [String] {
        let all = names(speakers)
        let hosts = parseHostNames(hostName)
        if hosts.count != 1 { return speakers }   // 几位主持人时推测那一步已经按名字标了
        let host = hosts[0]
        if all.isEmpty || all.contains(host) { return speakers }
        var labels = Set(all.filter { $0.hasPrefix("主持人") })
        if labels.isEmpty {
            var samples: [String] = []
            for name in all {
                var said: [String] = []
                for (k, p) in paragraphs.enumerated() where k < speakers.count && speakers[k] == name {
                    said.append(String(String.UnicodeScalarView(p.1.unicodeScalars.prefix(150))))
                    if said.count == 3 { break }
                }
                samples.append("【\(name)】\n" + said.joined(separator: "\n"))
            }
            let prompt = SpeechScript.fill(hostLabelPrompt, ["show_title": showTitle, "host_name": host, "samples": samples.joined(separator: "\n\n")])
            let answer = trim(try await ask(prompt, 50, 120)).trimmingCharacters(in: CharacterSet(charactersIn: "「」【】[]\"' "))
            if all.contains(answer) { labels = [answer] }
        }
        return speakers.map { labels.contains($0) ? host : $0 }
    }

    // MARK: 一期的流程

    /// 这一期的发言人。出错、格式不对、全是未知都不抛错（照常存，不分人）；只有用户点了停止才抛
    static func label(title: String, paragraphs: [(Double, String)], description: String, showTitle: String, hostName: String,
                      dialogue: Bool, ask: @escaping Ask) async throws -> Outcome {
        if paragraphs.isEmpty { return .skipped }
        switch plan(title: title, dialogue: dialogue) {
        case .none: return .skipped
        case .single(let name): return .labeled(Result(mode: "single", speakers: Array(repeating: name, count: paragraphs.count)))
        case .multi: break
        }
        var speakers: [String]
        do {
            let labels = try await infer(paragraphs: paragraphs, title: title, description: description, showTitle: showTitle,
                                         hostName: hostName, ask: ask)
            if labels.isEmpty { return .failed("模型回的格式认不出来") }
            speakers = (1...paragraphs.count).map { labels[$0] ?? unknown }
        } catch {
            if Task.isCancelled || error is CancellationError { throw error }
            return .failed(error.localizedDescription)
        }
        if !hostName.isEmpty {
            do {
                speakers = try await applyHostName(speakers, paragraphs: paragraphs, hostName: hostName, showTitle: showTitle, ask: ask)
            } catch {
                if Task.isCancelled || error is CancellationError { throw error }
                // 没能认出哪个是主持人：沿用推测出来的写法
            }
        }
        // 全是未知：写出来比不标还难看
        if names(speakers).isEmpty { return .unknown }
        return .labeled(Result(mode: "multi", speakers: speakers))
    }

    // MARK: 写进文件（pipeline.render_transcript_md / render_speech_md）

    /// 文件头那一行
    static func headerLine(_ r: Result?) -> String? {
        guard let r else { return nil }
        if r.mode == "single", let name = r.speakers.first { return "- 发言人：\(name)（根据标题解析，单人演讲）" }
        if r.mode == "multi" { return "- 发言人：AI 基于上下文推测标注，可能不准确，仅供参考" }
        return nil
    }

    /// 「## 文字记录」下面的正文：多人对话换人时先写一行「### 🗣️ 名字」
    static func transcriptBody(paragraphs: [(Double, String)], result: Result?, url: String) -> [String] {
        let hasTimes = paragraphs.contains { $0.0 > 0 }
        let multi = result?.mode == "multi" && !(result?.speakers.isEmpty ?? true)
        let sep = url.contains("?") ? "&" : "?"
        var lines: [String] = [], last: String?
        for (k, (t, text)) in paragraphs.enumerated() {
            if multi, let r = result, k < r.speakers.count, !r.speakers[k].isEmpty, r.speakers[k] != last {
                lines += ["### 🗣️ \(r.speakers[k])", ""]
                last = r.speakers[k]
            }
            if hasTimes { lines.append("**[\(stamp(t))](\(url)\(sep)t=\(Int(t))s)**  ") }
            lines += [text, ""]
        }
        return lines
    }

    /// pipeline.format_timestamp
    static func stamp(_ seconds: Double) -> String {
        let s = Int(seconds), h = s / 3600, m = s % 3600 / 60, r = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, r) : String(format: "%d:%02d", m, r)
    }

    // MARK: 整理稿用到的（pipeline._speech_speaker_instruction / normalize_speaker_labels）

    /// mode：zh（中文节目整理成中文）或 original（外语原文整理）
    static func instruction(mode: String, langName: String, names: [String]) -> String {
        var rule = (mode == "zh" ? ruleZh : ruleOriginal).replacingOccurrences(of: "{lang_name}", with: langName)
        if names.count >= 2 {
            // 文字记录里的发言人是按大段推测的，一段里主持人的提问和嘉宾的回答常被算给同一个人；
            // 模型看出来以后不给名单就会自己起「发言人A」「提问者」这类名字，前后还不一致
            rule += "\n- 本期发言人只有：" + names.joined(separator: "、") + "。标签只用这几个名字，不要另起名单以外的称呼"
                + "（比如「发言人A」「提问者」）；方括号里的发言人是自动推测的，一段里的提问和回答有时被"
                + "算给了同一个人，请按内容判断是谁在说。"
        }
        return rule
    }

    /// 段首的发言人标签能认出是名单里哪个人的，统一写成名单里的名字；认不出的不动
    static func normalize(_ text: String, names: [String]) -> String {
        if names.isEmpty { return text }
        var out = "", tail = text.startIndex
        for m in labelLine.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let whole = Range(m.range, in: text), let g1 = Range(m.range(at: 1), in: text),
                  let g2 = Range(m.range(at: 2), in: text), let g3 = Range(m.range(at: 3), in: text) else { continue }
            let label = trim(String(text[g2]))
            if names.contains(label) { continue }
            // [张三]、【张三】、发言人：张三、发言人张三、张三（嘉宾）→ 张三
            var bare = replace(bracketed, in: label, with: "$1")
            bare = replace(rolePrefix, in: bare, with: "")
            guard let name = [bare, replace(parenTail, in: bare, with: "")].first(where: { names.contains($0) }) else { continue }
            out += text[tail..<whole.lowerBound]
            out += "\(text[g1])**\(name)**\(text[g3])"
            tail = whole.upperBound
        }
        return out + text[tail...]
    }

    // MARK: 小工具

    private static func len(_ s: String) -> Int { s.unicodeScalars.count }
    private static func trim(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }
    private static func first(_ r: NSRegularExpression, _ s: String) -> NSTextCheckingResult? {
        r.firstMatch(in: s, range: NSRange(s.startIndex..., in: s))
    }
    private static func replace(_ r: NSRegularExpression, in s: String, with template: String) -> String {
        r.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: template)
    }
}
