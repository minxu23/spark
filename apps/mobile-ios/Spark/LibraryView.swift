import SwiftUI

struct LibraryView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    /// 上次看的是哪一类，下次打开还在这一类；第一次打开是剪藏
    @AppStorage("library.tab") private var tabRaw = Category.clip.rawValue
    private var tab: Category { Category(rawValue: tabRaw) ?? .clip }
    /// 「星标」不是一类内容，是加了星标的那些（剪藏、单集、会议、信息、报告都算），排在最前
    static let starred = "starred"
    /// 书架上分类的顺序：剪藏放第一
    private static let order = LatestView.order
    @State private var query = ""
    @State private var pickClips = false
    @State private var hasStarredOthers = false
    /// 书架「笔记」卡片上点开的随手记、读后感（一张卡里几处能点，不能用一行一个链接）
    @State private var noteOpen: NoteOpen?
    /// 播客、信息跟进卡片上点开的一期、一篇或一份简报（同上，交给书架页去开）
    @State private var leadOpen: LeadOpen?
    private enum LeadOpen: Hashable, Identifiable {
        case episode(Episode)
        case doc(Doc)
        var id: String {
            switch self {
            case .episode(let e): "episode:" + e.id
            case .doc(let d): "doc:" + d.path
            }
        }
    }
    /// 各分类怎么排，分别记住（剪藏的在 ClipShelf 里，键是 clips.sort）
    @AppStorage("library.sort.series") private var seriesSort = "updated"
    @AppStorage("library.sort.summit") private var summitSort = "updated"
    @AppStorage("library.sort.track") private var trackSort = "updated"
    @AppStorage("library.sort.report") private var reportSort = "updated"
    private static let showSorts: [(value: String, label: String)] = [("updated", "按最近更新"), ("name", "按名称"), ("unread", "未读多的在前")]
    /// 会议多一种：按会议时间（会议真正开的那几天，新的在前；不知道会议时间的排最后）
    private static let summitSorts = showSorts + [("held", "按会议时间")]
    private static let reportSorts: [(value: String, label: String)] = [("updated", "按生成时间"), ("name", "按名称")]

    private func sortShows(_ shows: [Show], _ by: String) -> [Show] {
        switch by {
        case "name": return shows.sorted { NameOrder.less($0.name, $1.name) }
        case "unread": return shows.sorted { (unread($0), $0.latest, $0.id) > (unread($1), $1.latest, $1.id) }
        case "held": return shows.sorted { ($0.eventKey, $0.latest, $0.name, $0.id) > ($1.eventKey, $1.latest, $1.name, $1.id) }
        default: return shows.sorted { ($0.latest, $0.id) > ($1.latest, $1.id) }
        }
    }
    /// 搜索用的正文：随手记的全文、读后感那几篇。开始搜索（或书架内容变了）时读一次放在内存里，
    /// 每按一个键只在这里面找，不再从磁盘重读
    @State private var corpus = SearchCorpus()
    private struct SearchCorpus {
        var version = ""
        var jottings: [String: String] = [:]     // 路径 → 全文
        var reflections: [ReadingNote] = []
    }

    private func loadCorpus() async {
        let version = "\(vault.libraryVersion)"
        guard corpus.version != version else { return }
        var texts: [String: String] = [:]
        for d in vault.library.notes { texts[d.path] = vault.localText(d.path) ?? "" }
        let notes = await vault.readingNotes()   // 读书笔记的缓存（ClipCache），没变的文件不重新解析
        guard !Task.isCancelled else { return }   // 等的时候 .task(id:) 换了：新的那次会写，旧的别盖掉它
        corpus = SearchCorpus(version: version, jottings: texts,
                              reflections: notes.filter { !Jottings.isJotting($0.meta.path) && !$0.reflection.isEmpty })
    }

    /// 右上角「任务」上的数字：还没跑完的（进行中、等电脑接手）和没成功的
    @State private var badge = (count: 0, failed: false)

    private func loadBadge() async {
        var count = 0, failed = false
        if let api = vault.api, let issues = try? await api.tasks() {
            for i in issues where i.state == "open" {
                count += 1
                if i.labelNames.contains("失败") { failed = true }
            }
        }
        let results = vault.cableTaskResults()
        for item in Outbox.items() {
            let state = results[item.id]?["state"] as? String
            if state == "failed" { failed = true }
            // 手机上已经做好、只是还没同步到电脑的内容不算待办
            if state != "done" && !item.isSync { count += 1 }
        }
        badge = (count, failed)
    }

    var body: some View {
        List {
            if query.isEmpty {
                PageStatus(text: shelfNote, warning: badge.failed)
                SectionTabs(items: [(Self.starred, "星标")] + Self.order.map { ($0.rawValue, $0.label) }, selection: $tabRaw)
            }

            RunningTaskCards()

            if !query.isEmpty {
                searchResults
            } else if tabRaw == Self.starred {
                StarredOthers(hasOthers: $hasStarredOthers)
                ClipShelf(onlyStarred: true, othersStarred: hasStarredOthers)
            } else if tab == .note {
                NotesShelf(open: { noteOpen = $0 })
            } else if tab == .clip {
                ClipShelf(pickRequest: $pickClips)
            } else if tab == .report {
                let reports = vault.library.reports
                if !reports.isEmpty { SortBar(count: "\(reports.count) 份", options: Self.reportSorts, selection: $reportSort) }
                let listed = reportSort == "name" ? reports.sorted { NameOrder.less($0.title, $1.title) } : reports.sorted { ($0.date, $0.path) > ($1.date, $1.path) }
                let _ = CrashLog.list("书架·报告", listed.map(\.id))
                ForEach(listed) { d in
                    PlainLink(value: d) { row(d.title, Dates.label(d.date), unread: 0) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } else if tab == .summit {
                summitShelf
            } else if tab == .track {
                trackShelf
            } else {
                let shows = vault.library.shows(tab)
                // 播客最上面一张卡列最近更新的三期（跟笔记卡片一个样子），跟怎么排无关；下面的列表总是完整的。
                // 卡片在最上面，排序那一行紧挨着下面的列表（各页签一样）
                if tab == .series { podcastCard(shows) }
                if !shows.isEmpty { SortBar(count: "\(shows.count) 档", options: Self.showSorts, selection: $seriesSort) }
                let listed = sortShows(shows, seriesSort)
                let _ = CrashLog.list("书架·\(tab.label)", listed.map(\.id))
                ForEach(listed) { s in
                    PlainLink(value: s) {
                        row(s.name, [s.countLabel, s.latest.isEmpty ? "" : "\(Dates.label(s.latest))更新"]
                                .filter { !$0.isEmpty }.joined(separator: "，"),
                            unread: s.episodes.filter { !readState.isRead($0.id) }.count)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            }
        }
        .readableColumn()
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("书架")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                AddMenu(onSummary: { tabRaw = Category.clip.rawValue; pickClips = true })
            }
            // 任务（交给电脑处理的链接、订阅检查、报告）：结果都落在书架上，入口放这里
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink { TasksView() } label: {
                    // 跟别的页签一样只放图标；有没跑完的任务角上挂个数字（状态那一行也写着），有没成功的变红
                    Image(systemName: "checklist")
                        .overlay(alignment: .topTrailing) {
                            if badge.count > 0 {
                                Text("\(badge.count)").font(.system(size: 10.5, weight: .bold)).foregroundStyle(.white)
                                    .padding(.horizontal, 4).frame(minWidth: 16, minHeight: 16)
                                    .background(badge.failed ? Color.hex(0xb42318) : p.accent, in: Capsule())
                                    .offset(x: 8, y: -6)
                            }
                        }
                }
                .accessibilityLabel(badge.count > 0 ? "任务，\(badge.count) 个\(badge.failed ? "有失败的" : "进行中")" : "任务")
            }
        }
        .task { await loadBadge() }
        .task(id: query.isEmpty ? "" : "search|\(vault.libraryVersion)") { if !query.isEmpty { await loadCorpus() } }
        .searchable(text: $query, prompt: "搜索笔记、节目、文章、剪藏和报告")
        .navigationDestination(for: Show.self) { ShowView(show: $0) }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
        .navigationDestination(for: ReadingNoteRef.self) { ReadingNoteView(path: $0.path, episode: $0.episode) }
        .navigationDestination(for: DocFocus.self) { DocReaderView(doc: $0.doc, focus: $0.focus) }
        .navigationDestination(for: EpisodeFocus.self) { ReaderView(episode: $0.episode, focusPath: $0.path, focus: $0.focus.isEmpty ? nil : $0.focus) }
        .navigationDestination(item: $noteOpen) { o in
            switch o {
            case .doc(let d): DocReaderView(doc: d)
            case .note(let r): ReadingNoteView(path: r.path, episode: r.episode)
            }
        }
        .navigationDestination(item: $leadOpen) { o in
            switch o {
            case .episode(let e): ReaderView(episode: e)
            case .doc(let d): DocReaderView(doc: d)
            }
        }
    }

    // MARK: 播客

    /// 卡片上列几条、按什么排（ShelfText.newest）：（日期、时刻）新的在前，同一天没有时刻的在后
    private static let leadLines = 3

    /// 播客卡片：最近更新、几档几期（同 Show.countLabel，简讯也算）、近 7 天更新几期，下面最近更新的三期能点
    /// （跨所有节目取，同一档可以占几行）。一期都没有不放
    @ViewBuilder
    private func podcastCard(_ shows: [Show]) -> some View {
        let all = shows.flatMap(\.episodes)
        let top = ShelfText.newest(all, limit: Self.leadLines, key: { ($0.date, $0.instant) }, tie: { $0.title + $0.id })
        if let first = top.first {
            ShelfLeadCard(leading: "最近更新 · \(first.date.isEmpty ? "—" : Dates.label(first.date))",
                          title: "\(shows.count) 档播客 · \(all.count) 期",
                          detail: ShelfText.recentLine(ShelfText.recentCount(all.map(\.date), now: .now), unit: "期")) {
                ShelfLeadLinks(lines: top.map { e in
                    .init(id: e.id, title: Markdown.latinQuotes(e.title), trailing: [e.showName, Dates.label(e.date)],
                          label: "\(e.showName)：\(e.title)", open: { leadOpen = .episode(e) })
                })
            }
            .shelfLeadRow()
        }
    }

    /// 大标题下的一句话：书架上各有多少，有没有任务在跑
    private var shelfNote: String {
        let lib = vault.library
        var s = "\(lib.notes.count) 篇随手记，\(lib.clips.count) 篇剪藏，\(lib.shows(.summit).count) 场会议，\(lib.shows(.series).count) 档播客，\(lib.shows(.track).filter { $0.name != "简报" }.count) 个订阅"
        if badge.count > 0 { s += badge.failed ? "；有任务没成功，点右上角看" : "；\(badge.count) 个任务在跑" }
        return s
    }

    @ViewBuilder
    private var searchResults: some View {
        let q = query.lowercased()
        let shows = vault.library.shows.filter { $0.name.lowercased().contains(q) }
        let eps = vault.library.allEpisodes.filter {
            $0.title.lowercased().contains(q) || $0.tldr.lowercased().contains(q)
        }.sorted { ($0.date, $0.id) > ($1.date, $1.id) }.prefix(80)   // 先按日期、同一天按 id 排定，再取前 80 条
        // 剪藏按标题、来源、一句话、标签找；报告和复盘按标题找
        let clips = ClipProcessor.shared.clipMetas(vault).filter { m in
            ([m.title, m.source, m.tldr, m.note] + m.tags).contains { $0.lowercased().contains(q) }
        }.sorted { ($0.clipped, $0.path) > ($1.clipped, $1.path) }
        let reports = vault.library.reports.filter { $0.title.lowercased().contains(q) }.sorted { ($0.date, $0.path) > ($1.date, $1.path) }
        // 随手记按标题和正文找；读后感按出处标题和读后感找（正文在 corpus 里，见 loadCorpus）
        let notes = vault.library.notes.filter { ShelfText.matches(query, [$0.title, corpus.jottings[$0.path] ?? ""]) }
            .sorted { ($0.date, $0.path) > ($1.date, $1.path) }
        let refl = corpus.reflections.filter { ShelfText.matches(query, [$0.meta.title, $0.reflection]) }
        ForEach(notes) { d in
            PlainLink(value: d) { row(d.title, "随手记　\(Dates.label(d.date))", unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(refl) { n in
            PlainLink(value: ReadingNoteRef(path: n.meta.path, episode: n.episode)) {
                row(n.meta.title, ["读后感", n.meta.source, Dates.label(DocTime.day(.at(n.edited)).replacingOccurrences(of: "-", with: ""))]
                        .filter { !$0.isEmpty }.joined(separator: "　"), unread: 0)
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(clips) { m in
            PlainLink(value: m.doc) { row(m.title, "剪藏　\(m.source)　\(Dates.label(m.clipped.replacingOccurrences(of: "-", with: "")))", unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(reports) { d in
            PlainLink(value: d) { row(d.title, "\(d.source)　\(Dates.label(d.date))", unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(shows) { s in
            PlainLink(value: s) { row(s.name, s.countLabel, unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(Array(eps)) { e in
            PlainLink(value: e) { row(e.title, "\(e.showName)　\(Dates.label(e.date))", unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        if shows.isEmpty && eps.isEmpty && clips.isEmpty && reports.isEmpty && notes.isEmpty && refl.isEmpty {
            Text("没找到「\(query)」").foregroundStyle(p.ink2).listRowBackground(Color.clear)
        }
    }

    // MARK: 会议

    private func unread(_ s: Show) -> Int { s.episodes.filter { !readState.isRead($0.id) }.count }

    /// 总结的头一两句，去掉 Markdown 符号，放在卡片上
    private func excerpt(_ s: Show) -> String {
        let raw = s.overallSummary.isEmpty ? (s.summaryPath.flatMap { vault.localText($0) } ?? "") : s.overallSummary
        return Self.leadText(Markdown.parse(raw))
    }

    /// 卡片摘要的取法：段落和列表项的文字（去掉 Markdown 符号）连起来，最多 90 个字
    static func leadText(_ blocks: [MDBlock]) -> String {
        let body = blocks.compactMap { b -> String? in
            switch b { case .paragraph(let t), .bullet(let t, _, _): return String(Markdown.inline(t).characters); default: return nil }
        }.joined(separator: " ")
        return String(ShelfText.stripCitations(body).prefix(90))   // 去掉 [1]、[^1] 这类引用编号
    }

    /// 会议：最近更新的一场放成大卡片（跟怎么排无关），下面是排序那一行和按选的排法排的列表，每场前面标年月
    @ViewBuilder
    private var summitShelf: some View {
        let shows = vault.library.shows(.summit)
        let byUpdated = sortShows(shows, "updated")
        let all = summitSort == "updated" ? byUpdated : sortShows(shows, summitSort)
        // 最近更新的一场放成大卡片，跟怎么排无关（按 latest 取，不是取当前排法的第一场）。
        // 按最近更新排时这一场不在下面列表里重复；别的排法列表是完整的，这一场按排法排在它该在的位置。
        // 卡片在最上面，排序那一行紧挨着下面的列表
        let top = byUpdated.first
        if let top {
            let topUnread = unread(top)
            PlainLink(value: top) {
                ShelfLeadCard(leading: "最近更新 · \(Dates.label(top.latest))",
                              trailing: top.countLabel + (topUnread > 0 ? " · \(topUnread) 未读" : ""),
                              title: top.name, detail: top.eventText, summary: excerpt(top), titleSize: 21, titleLines: nil)
            }
            .shelfLeadRow()
        }
        if !all.isEmpty { SortBar(count: "\(all.count) 场", options: Self.summitSorts, selection: $summitSort) }
        let listed = summitSort == "updated" ? all.filter { $0.id != top?.id } : all
        let _ = CrashLog.list("书架·会议", listed.map(\.id))
        ForEach(listed) { s in
            PlainLink(value: s) {
                HStack(spacing: 14) {
                    // 前面的年月：按会议时间排时标会议开始那个月，别的排法标最近更新的月份
                    let ym = summitSort == "held" ? s.eventKey.replacingOccurrences(of: "-", with: "") : s.latest
                    VStack(spacing: 0) {
                        Text(String(ym.prefix(4))).font(.system(size: 10.5)).foregroundStyle(p.ink2)
                        Text(ym.count >= 6 ? "\(Int(ym.dropFirst(4).prefix(2)) ?? 0)月" : "—").font(.system(size: 15, weight: .bold)).foregroundStyle(p.ink)
                    }
                    .frame(width: 48, height: 46)
                    .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityHidden(true)
                    row(s.name, s.countLabel + (s.topics.isEmpty ? "" : " · \(s.topics.count) 个主题")
                            + (s.eventText.isEmpty ? "" : "\n\(s.eventText)"), unread: unread(s))
                }
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
    }

    // MARK: 信息跟进

    /// 信息跟进：最上面一张卡（最近更新的三篇，简报也算；跟怎么排无关），下面「简报」一行，
    /// 再下面排序那一行和订阅（按分类归组，每个标上来源类型）
    @ViewBuilder
    private var trackShelf: some View {
        let shows = vault.library.shows(.track)
        let briefs = shows.first { $0.name == "简报" }
        let feeds = shows.filter { $0.name != "简报" }
        trackCard(feeds, briefs: briefs?.docs ?? [])
        // 简报只在这一行进（订阅列表里不放）：点开是简报这个 Show 的页面，跟点订阅一样
        if let briefs, !briefs.docs.isEmpty {
            PlainLink(value: briefs) {
                // 左边的徽章跟订阅行同尺寸，左边对齐
                HStack(spacing: 12) {
                    sourceBadge("brief")
                    row("简报", [briefs.countLabel, briefs.latest.isEmpty ? "" : "\(Dates.label(briefs.latest))更新"]
                            .filter { !$0.isEmpty }.joined(separator: "，"), unread: 0)
                }
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        let subs = Dictionary(FeedRunner.subscriptions(LocalNotes(root: Vault.root)).map { ($0.folder, $0) }, uniquingKeysWith: { a, _ in a })
        let groups = Dictionary(grouping: feeds) { subs[$0.id]?.category ?? "其他" }
        if !feeds.isEmpty { SortBar(count: "\(feeds.count) 个订阅", options: Self.showSorts, selection: $trackSort) }
        // 分类按最近更新排（按名称排时分类也按名称），「其他」放最后
        let newest = { (c: String) in groups[c]?.map(\.latest).max() ?? "" }
        let order = groups.keys.sorted { a, b in
            if (a == "其他") != (b == "其他") { return b == "其他" }
            if trackSort == "name" { return NameOrder.less(a, b) }
            return (newest(a), b) > (newest(b), a)
        }
        let sortedGroups = Dictionary(uniqueKeysWithValues: order.map { ($0, sortShows(groups[$0] ?? [], trackSort)) })
        let _ = CrashLog.list("书架·信息", order.map { (sortedGroups[$0] ?? []).map(\.id) }, keys: order)
        ForEach(order, id: \.self) { cat in
            Section {
                ForEach(sortedGroups[cat] ?? []) { s in
                    PlainLink(value: s) {
                        HStack(spacing: 12) {
                            sourceBadge(subs[s.id]?.source_type)
                            row(s.name, [s.countLabel, s.latest.isEmpty ? "" : "\(Dates.label(s.latest))更新"].filter { !$0.isEmpty }.joined(separator: "，"),
                                unread: unread(s))
                        }
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                Text("\(cat) · \((groups[cat] ?? []).count)").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    /// 卡片上一行：订阅里的一篇，或一份简报
    private enum TrackLine {
        case episode(Episode)
        case brief(Doc)
    }

    /// 信息跟进卡片：最近更新、几个订阅（简报不算）几篇、近 7 天更新几篇（篇数和近 7 天同一口径：订阅的单集加简报，
    /// 近 7 天不会比总数多；简报排进下面三条时，副文不会写「没有更新」），
    /// 下面最近更新的三篇能点（跨订阅取，简报也排进来，右边写「简报」）。没有订阅（只有简报）、一篇都没有都不放
    @ViewBuilder
    private func trackCard(_ feeds: [Show], briefs: [Doc]) -> some View {
        let eps = feeds.flatMap(\.episodes)
        let lines = eps.map(TrackLine.episode) + briefs.map(TrackLine.brief)
        let top = ShelfText.newest(lines, limit: Self.leadLines, key: { l in
            switch l {
            case .episode(let e): (e.date, e.instant)
            case .brief(let d): (d.date, d.feedInstant)
            }
        }, tie: { l in
            switch l {
            case .episode(let e): e.title + e.id
            case .brief(let d): d.title + d.path
            }
        })
        if !feeds.isEmpty, let first = top.first {
            let day = switch first { case .episode(let e): e.date; case .brief(let d): d.date }
            ShelfLeadCard(leading: "最近更新 · \(day.isEmpty ? "—" : Dates.label(day))",
                          title: "\(feeds.count) 个订阅 · \(eps.count + briefs.count) 篇",
                          detail: ShelfText.recentLine(ShelfText.recentCount(eps.map(\.date) + briefs.map(\.date), now: .now), unit: "篇")) {
                ShelfLeadLinks(lines: top.map { l in
                    switch l {
                    case .episode(let e):
                        .init(id: e.id, title: Markdown.latinQuotes(e.title), trailing: [e.showName, Dates.label(e.date)],
                              label: "\(e.showName)：\(e.title)", open: { leadOpen = .episode(e) })
                    case .brief(let d):
                        .init(id: d.path, title: Markdown.latinQuotes(d.title), trailing: ["简报", Dates.label(d.date)],
                              label: "简报：\(d.title)", open: { leadOpen = .doc(d) })
                    }
                })
            }
            .shelfLeadRow()
        }
    }

    private func sourceBadge(_ type: String?) -> some View {
        let (label, color): (String, Color) = switch type ?? "" {
        case "rss": ("RSS", Color.hex(0x8b4a1c))
        case "wechat": ("公众号", Color.hex(0x2f7a5b))
        case "youtube": ("YT", Color.hex(0x8a3a52))
        case "article": ("网页", p.accent)
        case "brief": ("简报", p.accent)
        default: ("订阅", p.ink2)
        }
        return Text(label).font(.system(size: 10.5, weight: .bold)).foregroundStyle(color)
            .frame(width: 40, height: 30)
            .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
            .accessibilityLabel("来源类型：\(label)")
    }

    private func row(_ title: String, _ meta: String, unread: Int) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.heading(17)).foregroundStyle(p.ink).lineSpacing(2)
                if !meta.isEmpty { Text(meta).font(.system(size: 13)).foregroundStyle(p.ink2) }
            }
            Spacer(minLength: 0)
            if unread > 0 {
                Text("\(unread)").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.paper)
                    .padding(.horizontal, 7).frame(minWidth: 22, minHeight: 22)
                    .background(p.accent, in: Capsule())
                    .accessibilityLabel("\(unread) 篇未读")
            }
        }
        .padding(.vertical, 6)
    }
}

struct ShowView: View {
    let show: Show
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var expanded = false
    @State private var topic: String?
    /// 看单集还是简讯：有简讯的节目，标题下面一个切换，不用翻到底
    @State private var showFlashes = false
    /// 单集怎么排：会议默认按议程，节目和信息跟进默认新的在前；分别记住
    @AppStorage("show.order.summit") private var summitOrder = "agenda"
    @AppStorage("show.order.series") private var seriesOrder = "newest"
    @AppStorage("show.order.track") private var trackOrder = "newest"
    private var orderBinding: Binding<String> {
        switch show.kind {
        case .summit: $summitOrder
        case .track: $trackOrder
        default: $seriesOrder
        }
    }
    private var orderOptions: [(value: String, label: String)] {
        (agendaAvailable ? [("agenda", "按议程")] : []) + [("newest", "新的在前"), ("oldest", "旧的在前")]
    }
    /// 现在用的排法：没有议程的会议选了「按议程」就当新的在前
    private var order: String {
        let o = orderBinding.wrappedValue
        return o == "agenda" && !agendaAvailable ? "newest" : o
    }

    private var summary: String {
        if !show.overallSummary.isEmpty { return show.overallSummary }
        if let path = show.summaryPath, let t = vault.localText(path) { return t }
        return ""
    }

    /// 会议有议程（处理记录里的顺序）才能按议程排；按议程排时单集前面标序号
    private var agendaAvailable: Bool { show.kind == .summit && show.episodes.contains { $0.rank > 0 } }
    private var agenda: Bool { agendaAvailable && order == "agenda" }

    /// 简讯（频道里的短视频）跟单集分开看：「按主题看」也只算单集，数字跟上面「单集 N」对得上
    private var splitFlashes: Bool { show.kind == .series }
    private var flashIDs: Set<String> { Set(show.episodes.filter(\.short).map(\.entryID)) }
    /// 主题里有几期单集（简讯不算）
    private func topicCount(_ ids: [String]) -> Int { ids.filter { !flashIDs.contains($0) }.count }
    private var flashes: [Episode] { splitFlashes ? episodes.filter(\.short) : [] }
    private var flashCount: Int { show.kind == .series ? show.episodes.filter(\.short).count : 0 }
    /// 列表里显示的：切到简讯就只列简讯，不然列单集（简讯不混进来）
    private var listed: [Episode] {
        guard splitFlashes else { return episodes }
        return showFlashes ? flashes : episodes.filter { !$0.short }
    }

    private var episodes: [Episode] {
        var list = show.episodes
        switch order {
        case "agenda" where agenda: list.sort { ($0.rank == 0 ? Int.max : $0.rank, $0.date, $0.id) < ($1.rank == 0 ? Int.max : $1.rank, $1.date, $1.id) }
        // 同一天的按发表时刻（只知道哪天的排在有时刻的后面），再按处理顺序、标题
        case "oldest": list.sort { ($0.date, $0.instant, $0.rank, $0.title, $0.id) < ($1.date, $1.instant, $1.rank, $1.title, $1.id) }
        default: list.sort { ($0.date, $0.instant, $0.rank, $0.title, $0.id) > ($1.date, $1.instant, $1.rank, $1.title, $1.id) }
        }
        guard let topic, let ids = show.topics.first(where: { $0.name == topic })?.ids else { return list }
        let set = Set(ids)
        return list.filter { set.contains($0.entryID) }
    }

    /// 会议的聚焦总结、信息跟进的简报：没有处理记录的那几篇
    private var extraDocs: [Doc] { show.docs.sorted { ($0.order, $0.path) > ($1.order, $1.path) } }
    /// 信息跟进的「简报」：一份一份的，每行标上日期（没主题的都叫「信息简报」，不标分不清），头上写「最近一份」
    private var isBriefs: Bool { show.kind == .track && show.name == "简报" }

    var body: some View {
        List {
            VStack(alignment: .leading, spacing: 6) {
                Text(show.name).font(.heading(26)).foregroundStyle(p.ink)
                Text(meta).font(.system(size: 13.5)).foregroundStyle(p.ink2)
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)

            if flashCount > 0 {
                Picker("看哪些", selection: $showFlashes) {
                    Text("单集 \(show.episodes.count - flashCount)").tag(false)
                    Text("简讯 \(flashCount)").tag(true)
                }
                .pickerStyle(.segmented)
                .onChange(of: showFlashes) { _, on in if on { topic = nil } }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if !summary.isEmpty && !showFlashes {
                VStack(alignment: .leading, spacing: 8) {
                    Text(show.kind == .summit ? "大会总结" : "节目总结").font(.heading(14))
                    MarkdownView(blocks: Markdown.stripPreamble(Markdown.parse(summary)))
                        .frame(maxHeight: expanded ? nil : prefs.size * 8, alignment: .top)
                        .clipped()
                        // 收起时最后一行渐隐，不从一行字中间硬切
                        .mask {
                            LinearGradient(stops: expanded ? [.init(color: .black, location: 0), .init(color: .black, location: 1)]
                                           : [.init(color: .black, location: 0.55), .init(color: .clear, location: 1)],
                                           startPoint: .top, endPoint: .bottom)
                        }
                    Button(expanded ? "收起" : "展开全文") { withAnimation { expanded.toggle() } }
                        .font(.system(size: 14, weight: .medium)).foregroundStyle(p.accent).frame(minHeight: 36)
                        .buttonStyle(.plain)
                }
                .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 6)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 4, trailing: 20))
            }

            if show.kind == .summit, !extraDocs.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("聚焦总结").font(.heading(14))
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(extraDocs) { d in
                                PlainLink(value: d) {
                                    Text(Markdown.latinQuotes(d.title)).font(.system(size: 13.5)).foregroundStyle(p.ink).lineLimit(1)
                                        .padding(.horizontal, 12).frame(minHeight: 36)
                                        .background(p.paper2, in: Capsule()).overlay(Capsule().stroke(p.rule))
                                }
                            }
                        }
                    }
                    .contentMargins(.horizontal, 0, for: .scrollContent)
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if !show.topics.isEmpty && !showFlashes {
                VStack(alignment: .leading, spacing: 8) {
                    Text("按主题看").font(.heading(14))
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            topicChip(nil, "全部 \(show.episodes.count - flashCount)")
                            ForEach(show.topics.filter { topicCount($0.ids) > 0 }, id: \.name) { topicChip($0.name, "\($0.name) \(topicCount($0.ids))") }
                        }
                    }
                    .contentMargins(.horizontal, 0, for: .scrollContent)   // 外面那栏的边距不要再叠一层
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            Section {
                if showFlashes && splitFlashes {
                    Text("频道里不到 3 分钟的短视频，只有整理稿，没有小结。")
                        .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                let rows = listed
                let docs = show.kind == .summit || showFlashes ? [] : extraDocs
                let _ = CrashLog.list("节目单集", [rows.map(\.id) + docs.map(\.id)])
                ForEach(Array(rows.enumerated()), id: \.element.id) { i, e in
                    episodeRow(i, e)
                }
                ForEach(docs) { d in
                    PlainLink(value: d) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(Markdown.latinQuotes(d.title)).font(.heading(16)).foregroundStyle(p.ink)
                            if isBriefs, !d.date.isEmpty {
                                Text(Dates.label(d.date)).font(.system(size: 13)).foregroundStyle(p.ink2)
                            }
                        }
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                HStack(spacing: 8) {
                    Text(showFlashes && splitFlashes ? "全部简讯" : topic.map { "「\($0)」里的\(show.kind == .summit ? "议题" : "单集")" } ?? (agenda ? "议程 · 按官网顺序" : show.kind == .summit ? "全部议题" : show.kind == .track ? "全部文章" : "全部单集"))
                        .font(.heading(14)).foregroundStyle(p.ink).textCase(nil)
                    Spacer(minLength: 0)
                    if listed.count > 1 { SortMenu(options: orderOptions, selection: orderBinding) }
                }
            }

        }
        .readableColumn()
        .listStyle(.plain)
        .listSectionSpacing(4)
        .pageStyle()
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if let url = URL(string: show.sourceURL), !show.sourceURL.isEmpty {
                ToolbarItem(placement: .topBarTrailing) { Link("原节目", destination: url) }
            }
        }
    }

    private func episodeRow(_ i: Int, _ e: Episode) -> some View {
        PlainLink(value: e) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if agenda {
                        Text(String(format: "%02d", i + 1)).font(.system(size: 13, weight: .semibold).monospacedDigit())
                            .foregroundStyle(p.ink2).accessibilityLabel("第 \(i + 1) 个")
                    }
                    if !readState.isRead(e.id) {
                        Circle().fill(p.accent).frame(width: 7, height: 7).accessibilityLabel("未读")
                    }
                    Text(Markdown.latinQuotes(e.title)).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(3)
                }
                if show.kind == .track, !e.tldr.isEmpty {
                    Text(e.tldr).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(2).lineLimit(2)
                } else if show.kind == .summit, !e.guests.isEmpty {
                    Text(e.guests.prefix(3).joined(separator: "、")).font(.system(size: 13)).foregroundStyle(p.ink2).lineLimit(1)
                }
                HStack(spacing: 10) {
                    // 会议：只标议程上这一场是哪天开的（session_date），多天的会议才标；视频发布那天不标
                    Text(show.kind == .summit ? (show.multiDay ? Dates.label(e.sessionDay) : "") : Dates.label(e.date))
                    Text(e.durationLabel)
                    Spacer()
                    Text(offline(e))
                }
                .font(.system(size: 12.5)).foregroundStyle(p.ink2)
            }
            .padding(.vertical, 4)
        }
        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        .saveToClips(e)
    }

    private var meta: String {
        var parts: [String] = []
        if !show.hostName.isEmpty { parts.append("主持人 \(show.hostName)") }
        parts.append(show.countLabel)
        if show.kind == .summit && !show.eventText.isEmpty {
            parts.append(show.eventText)
        } else if !show.latest.isEmpty {
            parts.append("\(isBriefs ? "最近一份" : "最近一期") \(Dates.label(show.latest))")
        }
        return parts.joined(separator: "　")
    }

    private func offline(_ e: Episode) -> String {
        let paths = [e.speechPath, e.transcriptPath, e.articlePath].compactMap { $0 }
        if paths.isEmpty || paths.allSatisfy(vault.isDownloaded) { return "已离线" }
        return "只有小结"
    }

    private func topicChip(_ t: String?, _ label: String) -> some View {
        let on = topic == t
        return Button { topic = t } label: {
            Text(label).font(.system(size: 13.5)).padding(.horizontal, 12).frame(minHeight: 36)
                .foregroundStyle(on ? p.paper : p.ink)
                .background(on ? p.accent : .clear, in: Capsule())
                .overlay(Capsule().stroke(on ? p.accent : p.rule))
        }
        .buttonStyle(.plain)
    }
}


/// 书架「星标」里剪藏以外的：单集、会议议题、信息跟进的文章、简报、报告，新的在前，列在星标剪藏上面
private struct StarredOthers: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @Binding var hasOthers: Bool
    @State private var items: [FeedItem] = []

    var body: some View {
        Group {
            if !items.isEmpty {
                Text("节目、会议、信息和报告").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                ForEach(items) { item in
                    switch item {
                    case .episode(let e):
                        PlainLink(value: e) { line(e.title, [e.short ? "简讯" : e.kind.label, e.showName, Dates.label(e.date)]) }
                            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    case .doc(let d):
                        PlainLink(value: d) { line(d.title, [d.kind.label, d.source, Dates.label(d.date)]) }
                            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    }
                }
            }
        }
        .task(id: vault.library.feed.count) { await load() }
        .onAppear { Task { await load() } }   // 从阅读页回来：刚加、刚取消的星标马上反映
    }

    private func load() async {
        items = await vault.starredOthers()
        hasOthers = !items.isEmpty
    }

    private func line(_ title: String, _ meta: [String]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(Markdown.latinQuotes(title)).font(.heading(17)).foregroundStyle(p.ink).lineSpacing(2)
            Text(meta.filter { !$0.isEmpty }.joined(separator: "　")).font(.system(size: 13)).foregroundStyle(p.ink2)
        }
        .padding(.vertical, 6)
    }
}
