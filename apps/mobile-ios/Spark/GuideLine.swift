import Foundation

/// 导读（长文摘要）里的一行怎么留：段落和「### 小标题」原样留着；分隔线丢掉；「#」「##」降成「###」（下游按「\n## 」切节）；
/// 小标题叫「正文」「摘要」的改成加粗行：重写摘要时按「## 正文」找边界，「### 正文」也会被认成它。
/// 电脑端同一规则：core/clip_compose.parse_summary(guide=True)，tests/test_clip_compose.py 用同一张表对照。
/// 只依赖 Foundation，能用 swiftc 单独编。
enum GuideLine {
    /// line 已去掉首尾空白；返回 nil 表示这一行不要
    static func keep(_ line: String) -> String? {
        if line.range(of: #"^(-{3,}|\*{3,}|_{3,})$"#, options: .regularExpression) != nil { return nil }
        let line = line.replacingOccurrences(of: #"^#+\s*(正文|摘要)\s*$"#, with: "**$1**", options: .regularExpression)
        return line.range(of: #"^#{1,2}\s+"#, options: .regularExpression).map { "### " + line[$0.upperBound...] } ?? line
    }
}
