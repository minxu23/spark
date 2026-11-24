import Foundation

/// 阅读页上打开的这一份是哪种文档、能做什么：种类由路径和所在页签推出，推导规则只写在这里。
/// 开关表在 ReaderFeatures；这里按路径再收窄（只关不开）：路径不能做笔记就关掉笔记，不能删的关掉删除。
/// 只看路径字符串（PathRules），不碰文件和界面：tests/test_reader_features.py 把它和 PathRules、ReaderFeatures
/// 一起单独编出来，按一张「路径 → 种类 → 收窄后的开关」的边界表核对。
/// 阅读外壳不直接调 Clips.isClip、DocEdit.annotatable 这些路径判断，都从这里拿。
struct ReaderPlace: Equatable {
    let kind: ReaderKind
    let features: ReaderFeatures

    /// 文档（DocReaderView 打开的一篇）。按顺序认：剪藏、随手记、复盘、不能做笔记的（总览页）、报告、简报，剩下的是会议资料
    static func doc(_ path: String) -> ReaderPlace {
        let kind: ReaderKind
        if PathRules.isClip(path) { kind = .clip }
        else if PathRules.isJotting(path) { kind = .jotting }
        else if PathRules.isReview(path) { kind = .review }
        else if !PathRules.annotatable(path) { kind = .overview }
        else if path.hasPrefix(PathRules.reportsDir) { kind = .report }
        else if path.hasPrefix(PathRules.trackDir) { kind = .brief }
        else { kind = .material }
        return ReaderPlace(kind: kind, features: narrowed(ReaderFeatures.of(kind), path: path, keepStar: kind == .clip))
    }

    /// 单集的一个页签。fileLoaded：这个页签的文件读到了（小结页签读到之前显示处理记录里的那份）
    static func episodeTab(_ tab: ReaderTab, path: String?, inFeed: Bool, fileLoaded: Bool) -> ReaderPlace {
        let kind: ReaderKind
        switch tab {
        case .note: kind = fileLoaded && path != nil ? .summary : .recordSummary
        case .speech: kind = .speech
        case .transcript: kind = .transcript
        case .article, .doc: kind = .article
        }
        var f = ReaderFeatures.of(kind, inFeed: inFeed)
        if let path { f = narrowed(f, path: path, keepStar: true) } else { f.notes = false; f.pendingNotice = false }
        return ReaderPlace(kind: kind, features: f)
    }

    /// 按路径收窄：不能做笔记的路径关笔记和图片操作；文档的星标跟着笔记走（剪藏例外，照旧）；
    /// 改名称、继续追问要 editable；删除要 canDelete（复盘只有 output/复盘/ 下直接的一篇）
    private static func narrowed(_ f: ReaderFeatures, path: String, keepStar: Bool) -> ReaderFeatures {
        var f = f
        // 图片重下和删图挂在笔记上（MDEdit 跟着 DocNotes），不能做笔记就没有
        if !PathRules.annotatable(path) { f.notes = false; f.images = false; if !keepStar { f.star = false } }
        if !PathRules.editable(path) { f.rename = false; f.followUp = false }
        if !PathRules.canDelete(path) { f.delete = false }
        return f
    }

    /// 单集的星标记在哪份稿子的文件头里：有小结记在小结上，没有小结的简讯记在整理稿上（电脑上 Mac 阅读器同一条）
    static func starPath(_ paths: [String?]) -> String? {
        paths.compactMap { $0 }.first(where: PathRules.annotatable)
    }
}
