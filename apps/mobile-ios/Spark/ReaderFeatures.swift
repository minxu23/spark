import Foundation

/// 阅读页能做什么，只在这里定。阅读外壳（ReaderShell）按开关显示，不自己看路径。
/// 种类怎么从路径、页签推出来见 ReaderPlace（ReaderPlace.swift）。
/// apps/mobile-ios/阅读功能表.md 是同一张表的人读版，tests/test_reader_features.py 用 swiftc 把这个文件
/// 单独编出来逐格核对：加功能、改开关必须同时改那张表。

/// 单集的几份稿子（页签）；文档只有一页（doc）；论文是导读、原文、PDF 三页（guide、paperBody、pdf，见 ReaderPlace.paperTabs）
enum ReaderTab: String, CaseIterable, Identifiable {
    case note, speech, transcript, article, doc, guide, paperBody, pdf
    var id: String { rawValue }

    var label: String {
        switch self {
        case .note: "小结"
        case .speech: "整理稿"
        case .transcript: "文字记录"
        case .article: "原文"
        case .doc: "正文"
        case .guide: "导读"
        case .paperBody: "原文"
        case .pdf: "PDF"
        }
    }
}

/// 阅读页上打开的是哪种文档
enum ReaderKind: String, CaseIterable {
    case clip              // 剪藏
    case paper             // 论文（剪藏式文件夹 Spark/论文/，功能和剪藏一样，多一个追问）：导读、原文两个页签
    case paperPDF          // 论文的 PDF 页签（文件头 pdf: 指向的原件，本机有才有这一页）：不做笔记
    case article           // 信息文章（原文页签）
    case articleSummary    // 信息文章的小结页签（文件里「## 小结」那一节）
    case summary           // 单集小结（小结文件）
    case recordSummary     // 处理记录里的小结（没有文件、只读）
    case speech            // 整理稿
    case transcript        // 文字记录
    case brief             // 简报
    case report            // 报告（笔记洞察）
    case review            // 复盘
    case material          // 会议资料（节目、会议文件夹里没有处理记录的文章）
    case jotting           // 随手记
    case overview          // 总览页（节目 / 会议总结、索引、摘录这类生成的列表页）

    var label: String {
        switch self {
        case .clip: "剪藏"
        case .paper: "论文"
        case .paperPDF: "论文（PDF）"
        case .article: "信息文章（原文）"
        case .articleSummary: "信息文章（小结）"
        case .summary: "单集小结"
        case .recordSummary: "处理记录里的小结"
        case .speech: "整理稿"
        case .transcript: "文字记录"
        case .brief: "简报"
        case .report: "报告"
        case .review: "复盘"
        case .material: "会议资料"
        case .jotting: "随手记"
        case .overview: "总览页"
        }
    }
}

/// 原文链接放不放、叫什么
enum ReaderLink: String {
    case none, video, web
    /// 工具栏上的名字
    var label: String {
        switch self {
        case .none: ""
        case .video: "原视频"
        case .web: "原网页"
        }
    }
    var systemImage: String { self == .video ? "play" : "safari" }
}

/// 一种文档的功能开关
struct ReaderFeatures: Equatable {
    var notes = false              // 能做笔记（划线、批注、读后感）。「更多」里的「怎么划重点、批注」不看它，看这一页的笔记挂没挂上（ReaderShell 的 current != nil：文件读到了才挂）
    var star = false               // 星标
    var later = false              // 稍后再读：星标为有的种类都有（of 里照星标定）；按路径收窄时还要路径能做笔记（ReaderPlace.narrowed）
    var rename = false             // 改名称
    var delete = false             // 删除
    var languageSwitch = false     // 语言切换（中文 / 英文 / 对照）：内容确实是对照稿时才显示
    var translate = false          // 翻译成中文
    var resummarize = false        // 重写摘要
    var followUp = false           // 继续追问（复盘）
    var paperAsk = false           // 追问（论文：只针对这一篇多轮问答，「存进本文」写进文末「## 追问」一节）
    var editJotting = false        // 编辑随手记
    var pendingNotice = false      // 「手机这版没进电脑」提示
    var cellularConfirm = false    // 流量下载确认和「再试一次」
    var images = false             // 图片重下和删图
    var transcriptNote = false     // 文字记录的说明条
    var untranslatedNotice = false // 对照稿里没有译文的段数提示
    var fromRecord = false         // 显示处理记录里的小结（一句话总结卡片 + 正文，只读）
    var summaryFilter = false      // 小结文件里去掉文末指向别的稿子的链接、旧的「我的高亮」
    var clipTidy = false           // 剪藏正文显示前整理（ClipTidy.clean，只影响显示）
    var link: ReaderLink = .none   // 原文链接（工具栏）

    /// 一种文档的开关。inFeed：单集在「信息」的订阅里（原文链接叫「原网页」，不叫「原视频」）
    static func of(_ kind: ReaderKind, inFeed: Bool = false) -> ReaderFeatures {
        var f = ReaderFeatures()
        let episodeLink: ReaderLink = inFeed ? .web : .video
        switch kind {
        case .clip, .paper:
            f.notes = true; f.star = true; f.rename = true; f.delete = true
            f.languageSwitch = true; f.translate = true; f.resummarize = true
            f.pendingNotice = true; f.images = true; f.clipTidy = true
            f.paperAsk = kind == .paper
            f.link = .web
        case .paperPDF:
            // 显示的是 PDF 原件，不是文件里的字：笔记、翻译、写摘要、图片这些都在文字页签上做
            f.star = true; f.rename = true; f.delete = true; f.paperAsk = true
            f.link = .web
        case .article:
            f.notes = true; f.star = true; f.pendingNotice = true; f.cellularConfirm = true
            f.link = .web
        case .articleSummary:
            // 文件拿不到时显示处理记录里的小结（.recordSummary），不报错：没有流量下载确认
            f.notes = true; f.star = true; f.pendingNotice = true
            f.link = .web
        case .summary:
            f.notes = true; f.star = true; f.pendingNotice = true; f.summaryFilter = true
            f.link = episodeLink
        case .recordSummary:
            f.star = true; f.fromRecord = true
            f.link = episodeLink
        case .speech:
            f.notes = true; f.star = true; f.languageSwitch = true; f.untranslatedNotice = true
            f.pendingNotice = true; f.cellularConfirm = true
            f.link = episodeLink
        case .transcript:
            f.notes = true; f.star = true; f.pendingNotice = true; f.cellularConfirm = true; f.transcriptNote = true
            f.link = episodeLink
        case .brief, .report, .material:
            f.notes = true; f.star = true; f.pendingNotice = true
        case .review:
            f.notes = true; f.star = true; f.rename = true; f.delete = true; f.followUp = true; f.pendingNotice = true
        case .jotting:
            f.notes = true; f.star = true; f.delete = true; f.editJotting = true; f.pendingNotice = true
        case .overview:
            f.pendingNotice = true
        }
        f.later = f.star
        return f
    }

    /// 功能表的列：（表头, 取值）。test_reader_features.py 按这个顺序打印
    static let columns: [(String, (ReaderFeatures) -> String)] = [
        ("做笔记", { yn($0.notes) }),
        ("星标", { yn($0.star) }),
        ("稍后再读", { yn($0.later) }),
        ("改名称", { yn($0.rename) }),
        ("删除", { yn($0.delete) }),
        ("语言切换", { yn($0.languageSwitch) }),
        ("翻译成中文", { yn($0.translate) }),
        ("重写摘要", { yn($0.resummarize) }),
        ("继续追问", { yn($0.followUp) }),
        ("追问", { yn($0.paperAsk) }),
        ("编辑随手记", { yn($0.editJotting) }),
        ("没进电脑提示", { yn($0.pendingNotice) }),
        ("流量下载确认", { yn($0.cellularConfirm) }),
        ("图片重下和删图", { yn($0.images) }),
        ("文字记录说明条", { yn($0.transcriptNote) }),
        ("未译段数提示", { yn($0.untranslatedNotice) }),
        ("处理记录小结", { yn($0.fromRecord) }),
        ("小结过滤", { yn($0.summaryFilter) }),
        ("剪藏整理", { yn($0.clipTidy) }),
        ("原文链接", { yn($0.link != .none) }),
    ]

    /// 功能表最后一列「链接叫法」：不在信息里、在信息里叫法不同时两个都写
    static func linkCell(_ kind: ReaderKind) -> String {
        let a = of(kind, inFeed: false).link, b = of(kind, inFeed: true).link
        if a == .none && b == .none { return "—" }
        return a == b ? a.label : "\(a.label)；在信息里：\(b.label)"
    }

    private static func yn(_ b: Bool) -> String { b ? "有" : "没有" }
}
