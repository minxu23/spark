import Foundation

/// 主题综述（复盘 kind "tag"）的提示词。跟电脑 core/review.py 的 tag_prompt 逐字一致：
/// tests/test_review_tag.py 用 swiftc 单独编这个文件，同样的输入两边比。只依赖 Foundation，所以出处、截断这些
/// 靠 ReviewRunner 的 source / cut 先算好再传进来（style 也由调用的一方传）。
enum TagReviewPrompt {
    static let maxItems = 40

    /// 一篇：类型（剪藏、播客…）、出处（《标题》（来源，日期））、一句话、小结（已截断，可以是空串）
    struct Row { let type: String; let source: String; let tldr: String; let body: String }

    static func titleRule(_ tag: String) -> String {
        "第一行写：TITLE: <一个短语点出「\(tag)」这批内容合起来讲的是什么，必须带上「\(tag)」，不超过 24 字，比如「\(tag) 的几条主线和分歧」；不带书名号和句号>"
    }

    /// 超过 maxItems 篇时写在材料说明和成文开头的那句
    static func limitNote(_ total: Int) -> String { "这个主题共 \(total) 篇，这里只读了最近 \(maxItems) 篇" }

    static func prompt(tag: String, rows: [Row], total: Int, style: String) -> String {
        let blocks = rows.enumerated().map { i, r -> String in
            var part = ["[\(i + 1)] \(r.type)　\(r.source)"]
            if !r.tldr.isEmpty { part.append("一句话：\(r.tldr)") }
            if !r.body.isEmpty { part.append(r.body) }
            return part.joined(separator: "\n")
        }
        let cut = total > rows.count
        let lead = cut ? "第二行单独写一句：「\(limitNote(total))。」然后再写这几节：" : "从第二行开始写这几节："
        let head = cut ? limitNote(total) : "共 \(rows.count) 篇"
        return """
        下面是我笔记库里带着「\(tag)」这个标签的内容，按日期从新到旧排列。请把它们放在一起，写一份主题综述。

        \(titleRule(tag))
        \(lead)
        ## 这些内容合起来在讲什么
        两三段，说清这批内容围绕的是什么问题、各自从什么角度谈。
        ## 互相印证的观点
        几条，每条一两句，写明哪几篇都支持这个看法，注明出处。
        ## 互相矛盾或有分歧的地方
        各方在哪些问题上说法不同，各自的依据是什么，注明出处。材料里没有分歧就写「没有」，不要硬凑。
        ## 值得接着追问的问题
        两三个，每个一句话。

        \(style)

        ===== 材料（\(head)）=====
        \(blocks.map { "\n" + $0 }.joined(separator: "\n"))

        """
    }
}
