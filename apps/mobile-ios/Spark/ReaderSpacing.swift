import Foundation

/// 阅读页的间距：阅读外壳 ReaderShell（单集：播客、会议、信息的文章；文档：剪藏、简报、报告、复盘、随手记）用这里的。
/// 单位是「正文一行的高度」line（L）：正文字体在当前字号下的行高 + 行距（ReadPrefs.lineSpacing），也就是相邻两行正文基线的距离
/// （ReadPrefs.lineStep 算，MarkableText 和 SwiftUI Text 两条路排正文都是这个数）。
/// 默认设置（霞鹜文楷 17 号、标准行距 11.9）下 L ≈ 32.0。
/// tests/test_reader_spacing.py 用 swiftc 单独编出来核对数值和层级
enum ReaderSpacing {
    /// 篇名四周：
    /// - top：内容区顶部到篇名 1.25 L（篇名区里最大的空隙）
    /// - line：篇名自己的行距（基线到基线）= 篇名字号 × 1.3
    /// - toMeta：篇名到来源 / 日期那一行 0.5 L，不小于 12（同一组，明显比组外紧）
    /// - toBody：来源行到正文第一块 1.25 L；没有来源行时篇名到正文也是这个数
    /// - small：篇名区里状态行、待确认提示框下面，同 toMeta
    /// - box：一句话总结卡片、说明条、语言切换下面到正文 0.75 L
    struct Title: Equatable {
        let top: Double
        let line: Double
        let toMeta: Double
        let toBody: Double
        let small: Double
        let box: Double
    }

    static let titleLineRatio = 1.3

    static func title(line: Double, titleSize: Double) -> Title {
        let toMeta = max(12, 0.5 * line)
        return Title(top: 1.25 * line, line: titleSize * titleLineRatio, toMeta: toMeta,
                     toBody: 1.25 * line, small: toMeta, box: 0.75 * line)
    }

    /// 正文里的小标题：返回在段间距（para = ReadPrefs.paraSpacing，正文各块之间都有）之外再加减的上、下边距，
    /// 让小标题到相邻块的总间距是：章节标题（## 及以上）上方 1.4 L、下方 0.6 L；小标题（### 及以下）上方 1.1 L、下方 0.5 L。
    /// 上方都大于下方的两倍，标题贴近它下面那段。文章开头第一块是标题时上方不另加（0）
    static func heading(level: Int, line: Double, para: Double, first: Bool) -> (top: Double, bottom: Double) {
        let (above, below) = level <= 2 ? (1.4, 0.6) : (1.1, 0.5)
        return (first ? 0 : above * line - para, below * line - para)
    }
}
