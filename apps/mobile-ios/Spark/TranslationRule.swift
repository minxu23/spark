import Foundation

/// 逐段翻译的译文合理性检查，同电脑上 core/clip_compose.translation_plausible（常量、判断逐字一致）。
/// 模型漏写某段的「[编号]」时，那段译文会被接到上一段后面：这样的译文当没译成，留给重试。
/// 只依赖 Foundation，测试里用 swiftc 单独编出来和 Python 比。
enum TranslationRule {
    static let headingFactor = 2, headingSlack = 20
    static let bodyPercent = 115, bodySlack = 25

    /// 标题：译文不能有「。」，长度不超过原文字符数的 2 倍加 20；其他段：不超过 1.15 倍加 25
    static func plausible(source: String, translated: String, heading: Bool) -> Bool {
        let s = source.unicodeScalars.count, t = translated.unicodeScalars.count   // 按码点数，跟 Python 的 len 一致
        if heading { return !translated.contains("。") && t <= s * headingFactor + headingSlack }
        return t * 100 <= s * bodyPercent + bodySlack * 100
    }

    private static let cueSpeaker = try! NSRegularExpression(pattern: "^\\*\\*[^*\\n]+\\*\\*[ \\t]*[:：][ \\t]*")
    private static let cue = try! NSRegularExpression(pattern: "^(?:\\[[^\\[\\]()]{1,40}\\][ \\t]*)+$")

    /// 整段只有方括号舞台提示（「[applause]」，前面可以有「**发言人**：」）：不用翻译，不送去模型。
    /// 同电脑上 core/clip_compose.is_stage_cue。
    static func isStageCue(_ paragraph: String) -> Bool {
        var p = paragraph.trimmingCharacters(in: .whitespacesAndNewlines)
        if let m = cueSpeaker.firstMatch(in: p, range: NSRange(p.startIndex..., in: p)), let r = Range(m.range, in: p) { p = String(p[r.upperBound...]) }
        return cue.firstMatch(in: p, range: NSRange(p.startIndex..., in: p)) != nil
    }

    /// 一批的回复里的编号正好是 1..count，否则整批不收（模型跳过一段后顺着编号，后面的译文会整体错位）。
    /// 同电脑上 core/clip_compose.numbers_complete。
    static func numbersComplete(_ got: [Int], count: Int) -> Bool {
        Set(got) == Set(Array(0..<count).map { $0 + 1 })
    }
}
