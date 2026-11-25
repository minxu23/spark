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

/// 剪藏、简报、报告、复盘、会议资料、随手记：一整篇 Markdown。论文分导读、原文、PDF 三页（ReaderPlace.paperTabs）
struct DocReaderView: View {
    let doc: Doc
    /// 从读书笔记点进来：打开后滚到这处划线
    var focus: String? = nil
    @Environment(Vault.self) private var vault

    var body: some View {
        ReaderShell(input: .doc(doc, focus: focus, paper: paperInfo))
    }

    /// 论文：PDF 原件在不在这台手机上（文件头 pdf: 指向的、确实有这个文件）、正文是不是从 PDF 取的字。不是论文是 nil
    private var paperInfo: (pdf: URL?, fromPDF: Bool)? {
        guard ReaderPlace.doc(doc.path).kind == .paper else { return nil }
        let text = vault.localText(doc.path) ?? ""
        let key = PaperRead.pdfKey(text)
        let url = key.isEmpty ? nil : Vault.root.appending(path: key, directoryHint: .notDirectory)
        let here = url.map { FileManager.default.fileExists(atPath: $0.path) } ?? false
        return (here ? url : nil, PaperRead.fromPDF(text))
    }
}

/// 阅读外壳要的输入
struct ReaderInput {
    struct Page {
        let tab: ReaderTab
        let path: String?
        /// 信息文章：这个页签显示文件里的哪一节（小结、原文两个页签是同一个文件，见 ReaderPlace.splitsArticle）
        var section: ArticleSection? = nil
    }

    let id: String                 // 记「读过」用（ReadState.mark）
    let title: String
    let meta: ReaderMeta           // 标题下面的来源行，空的不显示
    let pages: [Page]              // 单集：几个页签；文档：一页（.doc）；论文：导读、原文、[PDF]
    var startTab: ReaderTab? = nil // 打开时停在哪个页签（nil：第一个）。论文从 PDF 取字的默认开 PDF
    var pdfURL: URL? = nil         // 论文 PDF 页签显示的原件（手机上确实有才给）
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
    func section(_ t: ReaderTab) -> ArticleSection? { pages.first { $0.tab == t }?.section }
    /// 跟这个页签是同一个文件的页签（包括它自己）
    func tabs(sharing t: ReaderTab) -> [ReaderTab] { ReaderPlace.tabsSharing(pages.map { ($0.tab, $0.path) }, t) }

    /// 这个页签是哪种文档、能做什么。fileLoaded：这个页签的文件读到了
    func place(_ t: ReaderTab, fileLoaded: Bool) -> ReaderPlace {
        if let episode {
            return .episodeTab(t, path: path(t), inFeed: episode.kind == .track, fileLoaded: fileLoaded, section: section(t))
        }
        return .doc(path(t) ?? "", tab: t)
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
        // 信息文章：小结页签显示文章文件里「## 小结」那一节（读不到时用处理记录里的），原文页签显示「## 原文」那一节
        let split = ReaderPlace.splitsArticle(notePath: e.notePath, speechPath: e.speechPath, articlePath: e.articlePath, inFeed: e.kind == .track)
        let note = split ? Page(tab: .note, path: e.articlePath, section: .summary) : Page(tab: .note, path: e.notePath)
        var pages: [Page] = e.body.isEmpty && e.tldr.isEmpty && e.speechPath != nil ? [] : [note]
        if let p = e.speechPath { pages.append(Page(tab: .speech, path: p)) }
        if let p = e.transcriptPath { pages.append(Page(tab: .transcript, path: p)) }
        if let p = e.articlePath { pages.append(Page(tab: .article, path: p, section: split ? .original : nil)) }
        return ReaderInput(id: e.id, title: e.title, meta: meta(e), pages: pages, focusPath: focusPath, focus: focus,
                           tldr: e.tldr, recordBody: e.body, episodeURL: e.url,
                           starPath: ReaderPlace.starPath([e.notePath, e.speechPath, e.transcriptPath, e.articlePath]),
                           legacyHighlightsPath: e.notePath, episode: e)
    }

    /// paper：论文的 PDF 原件（本机有才有）和正文是不是从 PDF 取的字；nil 是别的文档（一页）
    static func doc(_ d: Doc, focus: String?, paper: (pdf: URL?, fromPDF: Bool)? = nil) -> ReaderInput {
        var pages = [Page(tab: .doc, path: d.path)]
        var start: ReaderTab?
        if let paper {
            // 导读、原文是同一个文件的两节（同信息文章的小结、原文）；PDF 页签的 path 也是这篇 Markdown（星标、改名、删除按它）
            let t = ReaderPlace.paperTabs(pdfHere: paper.pdf != nil, fromPDF: paper.fromPDF)
            pages = t.tabs.map { Page(tab: $0, path: d.path, section: $0 == .guide ? .abstract : $0 == .paperBody ? .paperBody : nil) }
            start = focus == nil ? t.start : .guide   // 从读书笔记点进来要滚到划线：停在文字页签
        }
        return ReaderInput(id: d.id, title: d.title, meta: ReaderMeta(name: d.source, facts: [Dates.label(d.date)]),
                           pages: pages, startTab: start, pdfURL: paper?.pdf, focusPath: d.path, focus: focus, starPath: d.path, doc: d)
    }

    private static func meta(_ e: Episode) -> ReaderMeta {
        var line = ReaderMeta(name: e.showName, facts: [Dates.label(e.date), e.durationLabel])
        // 嘉宾名单是模型给的，常带「（主持人）」「（科沃斯 CEO，嘉宾）」这类括注，也会写「无（本期为……）」：
        // 只留名字，没有真名就不显示
        let names = e.guests
            .map { $0.replacingOccurrences(of: #"[（(].*$"#, with: "", options: .regularExpression).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("无") && $0.count <= 20 }
        if !names.isEmpty { line.guests = "嘉宾：" + names.joined(separator: "、") }
        return line
    }
}

/// 标题下面的来源行：名字（节目名、来源）、日期和时长、另起一行的嘉宾。
/// 排法（ReaderShell.header）：一行放得下就一行；放不下在名字后面断开，日期和时长整个放到下一行，不从中间拆开
struct ReaderMeta: Equatable {
    var name: String
    /// 日期、时长：空的不算
    var facts: [String]
    var guests: String? = nil

    init(name: String, facts: [String], guests: String? = nil) {
        self.name = name.trimmingCharacters(in: .whitespaces)
        self.facts = facts.filter { !$0.isEmpty }
        self.guests = guests
    }

    /// 日期和时长连成一截（中间全角空格）
    var factsText: String { facts.joined(separator: "　") }
    var isEmpty: Bool { name.isEmpty && facts.isEmpty && (guests ?? "").isEmpty }
    /// 整行连起来的字（辅助功能读这个）
    var text: String { ([[name, factsText].filter { !$0.isEmpty }.joined(separator: "　")] + [guests ?? ""]).filter { !$0.isEmpty }.joined(separator: "\n") }
}
