import SwiftUI

/// 阅读页的两个入口：只把 Episode / Doc 变成阅读外壳（ReaderShell）要的输入（ReaderInput），别的都在外壳里。
/// 能做什么看 ReaderFeatures（开关表）和 ReaderPlace（种类怎么推出来）。

/// 单集阅读：小结、整理稿、文字记录三种稿子切换；信息里是小结 / 原文。
/// 都是笔记库里的文件，跟剪藏一样能划线、批注、写读后感（DocNotes）。小结先显示处理记录里的那份
/// （离线可看、只能读），notes/ 里的小结文件到手了换成文件，就能做笔记；重新生成小结时
/// 电脑上 core.annotations.carry_over 把笔记接回新小结。
struct ReaderView: View {
    let episode: Episode
    /// 从读书笔记点进来：打开哪一份稿子、滚到哪处划线
    var focusPath: String? = nil
    var focus: String? = nil

    var body: some View {
        ReaderShell(input: .episode(episode, focusPath: focusPath, focus: focus))
    }
}

/// 剪藏、简报、报告、复盘、会议资料、随手记：一整篇 Markdown
struct DocReaderView: View {
    let doc: Doc
    /// 从读书笔记点进来：打开后滚到这处划线
    var focus: String? = nil

    var body: some View {
        ReaderShell(input: .doc(doc, focus: focus))
    }
}

/// 阅读外壳要的输入
struct ReaderInput {
    struct Page {
        let tab: ReaderTab
        let path: String?
    }

    let id: String                 // 记「读过」用（ReadState.mark）
    let title: String
    let meta: String               // 标题下面的来源行，空的不显示
    let pages: [Page]              // 单集：几个页签；文档：一页（.doc）
    var focusPath: String? = nil   // 从读书笔记点进来：划线在哪份文件里
    var focus: String? = nil       // 滚到哪处划线
    var tldr = ""                  // 处理记录里的一句话总结
    var recordBody = ""            // 处理记录里的小结正文
    var episodeURL: String? = nil  // 单集的原文链接；文档是 nil（剪藏的读文件头）
    var starPath: String? = nil    // 星标记在哪份文件上
    var legacyHighlightsPath: String? = nil   // 目录里「我的高亮」的老写法从哪份小结读（只有单集）
    var episode: Episode? = nil
    var doc: Doc? = nil

    var isEpisode: Bool { episode != nil }

    func path(_ t: ReaderTab) -> String? { pages.first { $0.tab == t }?.path }

    /// 这个页签是哪种文档、能做什么。fileLoaded：这个页签的文件读到了
    func place(_ t: ReaderTab, fileLoaded: Bool) -> ReaderPlace {
        if let episode {
            return .episodeTab(t, path: path(t), inFeed: episode.kind == .track, fileLoaded: fileLoaded)
        }
        return .doc(path(t) ?? "")
    }

    /// 原文链接的网址。单集用处理记录里的网址，不读文件；没有单集网址、链接是网页的（剪藏）才调 text 读文件头里的 url（要有 scheme）
    func linkURL(_ f: ReaderFeatures, text: () -> String?) -> URL? {
        guard f.link != .none else { return nil }
        if let episodeURL { return episodeURL.isEmpty ? nil : URL(string: episodeURL) }
        guard f.link == .web, let path = pages.first?.path, let t = text(),
              let url = URL(string: Clips.meta(path: path, text: t).url), url.scheme != nil else { return nil }
        return url
    }

    /// 在前台停留够久了：记进阅读记录
    func logRead(_ t: ReaderTab) {
        if let episode { ReadLog.episode(episode, kind: t == .note ? "note" : t.rawValue) }
        else if let doc { ReadLog.doc(doc.path) }
    }

    static func episode(_ e: Episode, focusPath: String?, focus: String?) -> ReaderInput {
        // 简讯只有整理稿，没有小结：不给空的「小结」页签
        var pages: [Page] = e.body.isEmpty && e.tldr.isEmpty && e.speechPath != nil ? [] : [Page(tab: .note, path: e.notePath)]
        if let p = e.speechPath { pages.append(Page(tab: .speech, path: p)) }
        if let p = e.transcriptPath { pages.append(Page(tab: .transcript, path: p)) }
        if let p = e.articlePath { pages.append(Page(tab: .article, path: p)) }
        return ReaderInput(id: e.id, title: e.title, meta: meta(e), pages: pages, focusPath: focusPath, focus: focus,
                           tldr: e.tldr, recordBody: e.body, episodeURL: e.url,
                           starPath: ReaderPlace.starPath([e.notePath, e.speechPath, e.transcriptPath, e.articlePath]),
                           legacyHighlightsPath: e.notePath, episode: e)
    }

    static func doc(_ d: Doc, focus: String?) -> ReaderInput {
        ReaderInput(id: d.id, title: d.title, meta: [d.source, Dates.label(d.date)].filter { !$0.isEmpty }.joined(separator: "　"),
                    pages: [Page(tab: .doc, path: d.path)], focusPath: d.path, focus: focus, starPath: d.path, doc: d)
    }

    private static func meta(_ e: Episode) -> String {
        var line = [e.showName, Dates.label(e.date), e.durationLabel]
            .filter { !$0.isEmpty }.joined(separator: "　")
        // 嘉宾名单是模型给的，常带「（主持人）」「（科沃斯 CEO，嘉宾）」这类括注，也会写「无（本期为……）」：
        // 只留名字，没有真名就不显示
        let names = e.guests
            .map { $0.replacingOccurrences(of: #"[（(].*$"#, with: "", options: .regularExpression).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("无") && $0.count <= 20 }
        if !names.isEmpty { line += "\n嘉宾：" + names.joined(separator: "、") }
        return line
    }
}
