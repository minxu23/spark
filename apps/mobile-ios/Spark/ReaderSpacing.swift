import Foundation

/// 阅读页的间距，两套阅读外壳（单集：播客、会议、信息的文章；文档：剪藏、简报、报告、复盘、随手记）都用这里的。
/// 都跟着阅读设置里的正文字号（ReadPrefs.size）等比变，默认 17 号字时就是下面写的数。
/// tests/test_reader_spacing.py 用 swiftc 单独编出来核对数值
enum ReaderSpacing {
    static let baseSize = 17.0

    /// 篇名四周：上方 24；到来源 / 日期那一行 12；来源那一行到正文 28；没有来源那一行时篇名到正文 28
    struct Title: Equatable {
        let top: Double
        let toMeta: Double
        let toBody: Double
    }

    static func title(size: Double) -> Title {
        let k = size / baseSize
        return Title(top: (24 * k).rounded(), toMeta: (12 * k).rounded(), toBody: (28 * k).rounded())
    }

    /// 正文里的小标题在段间距（ReadPrefs.paraSpacing，上下都有）之外再加的：
    /// 章节标题（## 及以上）上方多 0.9 倍字号，小标题（### 及以下）多 0.6 倍；下方都收回 0.35 倍字号，
    /// 让标题贴近它下面的那段。默认设置（17 号字、标准段间距 22.1、标准行距）下，小标题上方共 32.3，
    /// 正好一行正文的高度（字号 × 1.2 + 行距 11.9），下方 16.1。文章开头第一块是标题时上方不另加
    static func heading(level: Int, size: Double, first: Bool) -> (top: Double, bottom: Double) {
        let top = first ? 0 : size * (level <= 2 ? 0.9 : 0.6)
        return (top, -size * 0.35)
    }
}
