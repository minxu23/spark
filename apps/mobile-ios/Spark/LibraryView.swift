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
            } else if tab == .clip {
                ClipShelf(pickRequest: $pickClips)
            } else if tab == .report {
                ForEach(vault.library.reports.sorted { $0.date > $1.date }) { d in
                    PlainLink(value: d) { row(d.title, Dates.label(d.date), unread: 0) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } else if tab == .summit {
                summitShelf
            } else if tab == .track {
                trackShelf
            } else {
                ForEach(vault.library.shows(tab)) { s in
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
        .searchable(text: $query, prompt: "搜索节目、文章、剪藏和报告")
        .navigationDestination(for: Show.self) { ShowView(show: $0) }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
        .navigationDestination(for: ReadingNoteRef.self) { ReadingNoteView(path: $0.path, episode: $0.episode) }
        .navigationDestination(for: DocFocus.self) { DocReaderView(doc: $0.doc, focus: $0.focus) }
        .navigationDestination(for: EpisodeFocus.self) { ReaderView(episode: $0.episode, focusPath: $0.path, focus: $0.focus.isEmpty ? nil : $0.focus) }
    }

    /// 大标题下的一句话：书架上各有多少，有没有任务在跑
    private var shelfNote: String {
        let lib = vault.library
        var s = "\(lib.clips.count) 篇剪藏，\(lib.shows(.summit).count) 场会议，\(lib.shows(.series).count) 档播客，\(lib.shows(.track).filter { $0.name != "简报" }.count) 个订阅"
        if badge.count > 0 { s += badge.failed ? "；有任务没成功，点右上角看" : "；\(badge.count) 个任务在跑" }
        return s
    }

    @ViewBuilder
    private var searchResults: some View {
        let q = query.lowercased()
        let shows = vault.library.shows.filter { $0.name.lowercased().contains(q) }
        let eps = vault.library.allEpisodes.filter {
            $0.title.lowercased().contains(q) || $0.tldr.lowercased().contains(q)
        }.sorted { $0.date > $1.date }.prefix(80)
        // 剪藏按标题、来源、一句话、标签找；报告和复盘按标题找
        let clips = ClipProcessor.shared.clipMetas(vault).filter { m in
            ([m.title, m.source, m.tldr, m.note] + m.tags).contains { $0.lowercased().contains(q) }
        }.sorted { $0.clipped > $1.clipped }
        let reports = vault.library.reports.filter { $0.title.lowercased().contains(q) }.sorted { $0.date > $1.date }
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
        if shows.isEmpty && eps.isEmpty && clips.isEmpty && reports.isEmpty {
            Text("没找到「\(query)」").foregroundStyle(p.ink2).listRowBackground(Color.clear)
        }
    }

    // MARK: 会议

    private func unread(_ s: Show) -> Int { s.episodes.filter { !readState.isRead($0.id) }.count }

    /// 总结的头一两句，去掉 Markdown 符号，放在卡片上
    private func excerpt(_ s: Show) -> String {
        let raw = s.overallSummary.isEmpty ? (s.summaryPath.flatMap { vault.localText($0) } ?? "") : s.overallSummary
        let body = Markdown.parse(raw).compactMap { b -> String? in
            switch b { case .paragraph(let t), .bullet(let t, _, _): return String(Markdown.inline(t).characters); default: return nil }
        }.joined(separator: " ")
        return String(body.prefix(90))
    }

    /// 会议：最近更新的一场放成大卡片，其余按时间排，前面标年月
    @ViewBuilder
    private var summitShelf: some View {
        let all = vault.library.shows(.summit).sorted { $0.latest > $1.latest }
        if let top = all.first {
            PlainLink(value: top) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("最近更新 · \(Dates.label(top.latest))")
                        Spacer()
                        Text(top.countLabel + (unread(top) > 0 ? " · \(unread(top)) 未读" : ""))
                    }
                    .font(.system(size: 12)).foregroundStyle(.white.opacity(0.85))
                    Text(top.name).font(.heading(21)).foregroundStyle(.white).lineSpacing(2)
                    let ex = excerpt(top)
                    if !ex.isEmpty {
                        Text(ex).font(.system(size: 13)).foregroundStyle(.white.opacity(0.9)).lineSpacing(3).lineLimit(3)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.hex(0x1f3a5f), in: RoundedRectangle(cornerRadius: 16))
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
        }
        ForEach(all.dropFirst()) { s in
            PlainLink(value: s) {
                HStack(spacing: 14) {
                    VStack(spacing: 0) {
                        Text(String(s.latest.prefix(4))).font(.system(size: 10.5)).foregroundStyle(p.ink2)
                        Text(s.latest.count >= 6 ? "\(Int(s.latest.dropFirst(4).prefix(2)) ?? 0)月" : "—").font(.system(size: 15, weight: .bold)).foregroundStyle(p.ink)
                    }
                    .frame(width: 48, height: 46)
                    .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                    .accessibilityHidden(true)
                    row(s.name, s.countLabel + (s.topics.isEmpty ? "" : " · \(s.topics.count) 个主题"), unread: unread(s))
                }
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
    }

    // MARK: 信息跟进

    /// 信息跟进：最新的一份简报放最上面，订阅按分类归组，每个标上来源类型
    @ViewBuilder
    private var trackShelf: some View {
        let shows = vault.library.shows(.track)
        let briefs = shows.first { $0.name == "简报" }
        if let b = briefs?.docs.max(by: { $0.order < $1.order }) {
            PlainLink(value: b) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("最新简报").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.accent)
                        Spacer()
                        Text(Dates.label(b.date)).font(.system(size: 12)).foregroundStyle(p.ink2)
                    }
                    Text(Markdown.latinQuotes(b.title)).font(.heading(17)).foregroundStyle(p.ink)
                    Text("把各个订阅的新内容合成一篇，读完再挑感兴趣的看。").font(.system(size: 13)).foregroundStyle(p.ink2)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(p.rule))
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
        }
        let subs = Dictionary(FeedRunner.subscriptions(LocalNotes(root: Vault.root)).map { ($0.folder, $0) }, uniquingKeysWith: { a, _ in a })
        let groups = Dictionary(grouping: shows.filter { $0.name != "简报" }) { subs[$0.id]?.category ?? "其他" }
        // 分类按最近更新排，「其他」放最后
        let newest = { (c: String) in groups[c]?.map(\.latest).max() ?? "" }
        let order = groups.keys.sorted { a, b in
            if (a == "其他") != (b == "其他") { return b == "其他" }
            return (newest(a), b) > (newest(b), a)
        }
        ForEach(order, id: \.self) { cat in
            Section {
                ForEach((groups[cat] ?? []).sorted { $0.latest > $1.latest }) { s in
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

    private func sourceBadge(_ type: String?) -> some View {
        let (label, color): (String, Color) = switch type ?? "" {
        case "rss": ("RSS", Color.hex(0x8b4a1c))
        case "wechat": ("公众号", Color.hex(0x2f7a5b))
        case "youtube": ("YT", Color.hex(0x8a3a52))
        case "article": ("网页", p.accent)
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

    private var summary: String {
        if !show.overallSummary.isEmpty { return show.overallSummary }
        if let path = show.summaryPath, let t = vault.localText(path) { return t }
        return ""
    }

    /// 会议按议程（处理记录里的顺序）排，节目和信息跟进按日期新的在前
    private var agenda: Bool { show.kind == .summit && show.episodes.contains { $0.rank > 0 } }

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
        if agenda { list.sort { ($0.rank == 0 ? Int.max : $0.rank, $0.date) < ($1.rank == 0 ? Int.max : $1.rank, $1.date) } }
        guard let topic, let ids = show.topics.first(where: { $0.name == topic })?.ids else { return list }
        let set = Set(ids)
        return list.filter { set.contains($0.entryID) }
    }

    /// 会议的聚焦总结、信息跟进的简报：没有处理记录的那几篇
    private var extraDocs: [Doc] { show.docs.sorted { $0.order > $1.order } }

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
                ForEach(Array(listed.enumerated()), id: \.element.id) { i, e in
                    episodeRow(i, e)
                }
                ForEach(show.kind == .summit || showFlashes ? [] : extraDocs) { d in
                    PlainLink(value: d) {
                        Text(Markdown.latinQuotes(d.title)).font(.heading(16)).foregroundStyle(p.ink)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                Text(showFlashes && splitFlashes ? "全部简讯" : topic.map { "「\($0)」里的\(show.kind == .summit ? "议题" : "单集")" } ?? (agenda ? "议程 · 按官网顺序" : show.kind == .summit ? "全部议题" : show.kind == .track ? "全部文章" : "全部单集"))
                    .font(.heading(14)).foregroundStyle(p.ink).textCase(nil)
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
                    Text(Dates.label(e.date))
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
        if !show.latest.isEmpty { parts.append("最近一期 \(Dates.label(show.latest))") }
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
