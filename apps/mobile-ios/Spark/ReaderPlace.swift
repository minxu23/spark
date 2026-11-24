import Foundation

/// 阅读页上打开的这一份是哪种文档、能做什么：种类由路径和所在页签推出，推导规则只写在这里。
/// 开关表在 ReaderFeatures；这里按路径再收窄（只关不开）：路径不能做笔记就关掉笔记，不能删的关掉删除。
/// 只看路径字符串（PathRules），不碰文件和界面：tests/test_reader_features.py 把它和 PathRules、ReaderFeatures
/// 一起单独编出来，按一张「路径 → 种类 → 收窄后的开关」的边界表核对。
/// 阅读外壳不直接调 Clips.isClip、DocEdit.annotatable 这些路径判断，都从这里拿。
/// 信息文章的一个页签显示文件里的哪一节
enum ArticleSection: String { case summary, original }

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

    /// 单集的一个页签。fileLoaded：这个页签的文件读到了（小结页签读到之前显示处理记录里的那份）。
    /// section：信息文章的两个页签显示同一个文件的哪一节（见 splitsArticle）；小结页签读到「## 小结」那一节才算 fileLoaded
    static func episodeTab(_ tab: ReaderTab, path: String?, inFeed: Bool, fileLoaded: Bool,
                           section: ArticleSection? = nil) -> ReaderPlace {
        let kind: ReaderKind
        switch tab {
        case .note where section == .summary: kind = fileLoaded && path != nil ? .articleSummary : .recordSummary
        case .note: kind = fileLoaded && path != nil ? .summary : .recordSummary
        case .speech: kind = .speech
        case .transcript: kind = .transcript
        case .article, .doc: kind = .article
        }
        var f = ReaderFeatures.of(kind, inFeed: inFeed)
        if let path { f = narrowed(f, path: path, keepStar: true) } else { f.notes = false; f.pendingNotice = false }
        return ReaderPlace(kind: kind, features: f)
    }

    /// 按路径收窄：不能做笔记的路径关笔记和图片操作；文档的星标跟着笔记走（剪藏例外，照旧）；稍后再读要星标为有、路径又能做笔记；
    /// 改名称、继续追问要 editable；删除要 canDelete（复盘只有 output/复盘/ 下直接的一篇）
    private static func narrowed(_ f: ReaderFeatures, path: String, keepStar: Bool) -> ReaderFeatures {
        var f = f
        // 图片重下和删图挂在笔记上（MDEdit 跟着 DocNotes），不能做笔记就没有
        if !PathRules.annotatable(path) { f.notes = false; f.images = false; if !keepStar { f.star = false } }
        f.later = f.star && PathRules.annotatable(path)   // 稍后再读：星标为有、而且路径能做笔记（电脑上 annotatable 同一条）
        if !PathRules.editable(path) { f.rename = false; f.followUp = false }
        if !PathRules.canDelete(path) { f.delete = false }
        return f
    }

    /// 信息文章（信息订阅里按文章处理的一期：只有原文文件，没有小结文件、整理稿）：
    /// 文件里「## 小结」「## 原文」两节分给「小结」「原文」两个页签，两个页签是同一个文件。
    /// 按节目流程处理的订阅（有小结文件或整理稿）不分
    static func splitsArticle(notePath: String?, speechPath: String?, articlePath: String?, inFeed: Bool) -> Bool {
        inFeed && notePath == nil && speechPath == nil && articlePath != nil
    }

    /// 信息文章的正文里两节的范围（块的下标）：第一个二级标题「小结」到第一个在它后面的「原文」之前是小结，
    /// 「原文」后面到文末是原文。headings：正文里二级标题的（块下标, 标题字）；count：一共几块。
    /// 缺一节、顺序反了、哪一节是空的，返回 nil（照旧：小结用处理记录里的，原文页签显示整个文件）
    static func articleSections(_ headings: [(index: Int, text: String)], count: Int) -> (summary: Range<Int>, original: Range<Int>)? {
        guard let s = headings.first(where: { $0.text == "小结" })?.index,
              let o = headings.first(where: { $0.text == "原文" && $0.index > s })?.index,
              o > s + 1, o + 1 < count else { return nil }
        return ((s + 1)..<o, (o + 1)..<count)
    }

    /// 信息文章一个页签显示哪些块：range 是要排的块（nil：小结页签不用文件，显示处理记录里的），split：分成了两节。
    /// 分不成两节时原文页签排整个文件
    static func sectionView(_ headings: [(index: Int, text: String)], count: Int, section: ArticleSection) -> (range: Range<Int>?, split: Bool) {
        if let r = articleSections(headings, count: count) { return (section == .summary ? r.summary : r.original, true) }
        return (section == .summary ? nil : 0..<count, false)
    }

    /// 信息文章文件里「## 小结」后第一行非空的字是整行加粗的一句话总结：返回去掉星号的字（句末标点留着）。
    /// 这一行照处理记录那一版画成卡片，不当小标题、不进目录；只读（不在 MarkAnchor 的候选里配上页面上的项）
    static func sectionLead(_ text: String) -> String? {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        guard let s = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "## 小结" }),
              lines[(s + 1)...].contains(where: { $0.trimmingCharacters(in: .whitespaces) == "## 原文" }),
              let first = lines[(s + 1)...].first(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) else { return nil }
        let t = first.trimmingCharacters(in: .whitespaces)
        guard t.count > 4, t.hasPrefix("**"), t.hasSuffix("**") else { return nil }
        let inner = String(t.dropFirst(2).dropLast(2))
        guard !inner.contains("**"), !inner.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return inner.trimmingCharacters(in: .whitespaces)
    }

    /// 跟这个页签是同一个文件的页签（包括它自己）：信息文章的小结、原文。改了、放弃了、读到了都一起重排
    static func tabsSharing(_ pages: [(tab: ReaderTab, path: String?)], _ t: ReaderTab) -> [ReaderTab] {
        guard let p = pages.first(where: { $0.tab == t })?.path else { return [t] }
        return pages.filter { $0.path == p }.map(\.tab)
    }

    /// 单集的星标记在哪份稿子的文件头里：有小结记在小结上，没有小结的简讯记在整理稿上（电脑上 Mac 阅读器同一条）
    static func starPath(_ paths: [String?]) -> String? {
        paths.compactMap { $0 }.first(where: PathRules.annotatable)
    }
}
