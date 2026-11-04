import SwiftUI

struct LibraryView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var tab: Category = .series
    @State private var query = ""
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
            if state != "done" { count += 1 }
        }
        badge = (count, failed)
    }

    var body: some View {
        List {
            Picker("类别", selection: $tab) {
                ForEach(Category.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear).listRowSeparator(.hidden)

            RunningTaskCards()

            if !query.isEmpty {
                searchResults
            } else if tab == .clip {
                ClipShelf()
            } else if tab == .report {
                ForEach(vault.library.reports.sorted { $0.date > $1.date }) { d in
                    PlainLink(value: d) { row(d.title, Dates.label(d.date), unread: 0) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
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
            // 任务（交给电脑处理的链接、订阅检查、报告）：结果都落在书架上，入口放这里
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink { TasksView() } label: {
                    // 只放图标；有没跑完的任务时角上挂个数字，有没成功的就变红
                    Image(systemName: "checklist")
                        .overlay(alignment: .topTrailing) {
                            if badge.count > 0 {
                                Text("\(badge.count)").font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                                    .padding(.horizontal, 5).frame(minWidth: 18, minHeight: 18)
                                    .background(badge.failed ? Color.hex(0xb42318) : p.accent, in: Capsule())
                                    .overlay(Capsule().stroke(p.paper, lineWidth: 2))
                                    .offset(x: 9, y: -6)
                            }
                        }
                }
                .accessibilityLabel(badge.count > 0 ? "任务，\(badge.count) 个\(badge.failed ? "有失败的" : "进行中")" : "任务")
            }
        }
        .task { await loadBadge() }
        .searchable(text: $query, prompt: "搜索节目、标题和一句话总结")
        .navigationDestination(for: Show.self) { ShowView(show: $0) }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
    }

    @ViewBuilder
    private var searchResults: some View {
        let q = query.lowercased()
        let shows = vault.library.shows.filter { $0.name.lowercased().contains(q) }
        let eps = vault.library.allEpisodes.filter {
            $0.title.lowercased().contains(q) || $0.tldr.lowercased().contains(q)
        }.sorted { $0.date > $1.date }.prefix(80)
        ForEach(shows) { s in
            PlainLink(value: s) { row(s.name, s.countLabel, unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(Array(eps)) { e in
            PlainLink(value: e) { row(e.title, "\(e.showName)　\(Dates.label(e.date))", unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        if shows.isEmpty && eps.isEmpty {
            Text("没找到「\(query)」").foregroundStyle(p.ink2).listRowBackground(Color.clear)
        }
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

    private var summary: String {
        if !show.overallSummary.isEmpty { return show.overallSummary }
        if let path = show.summaryPath, let t = vault.localText(path) { return t }
        return ""
    }

    private var episodes: [Episode] {
        guard let topic, let ids = show.topics.first(where: { $0.name == topic })?.ids else { return show.episodes }
        let set = Set(ids)
        return show.episodes.filter { set.contains($0.entryID) }
    }

    var body: some View {
        List {
            VStack(alignment: .leading, spacing: 6) {
                Text(show.name).font(.heading(26)).foregroundStyle(p.ink)
                Text(meta).font(.system(size: 13.5)).foregroundStyle(p.ink2)
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)

            if !summary.isEmpty {
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

            if !show.topics.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("按主题看").font(.heading(14))
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            topicChip(nil, "全部 \(show.episodes.count)")
                            ForEach(show.topics, id: \.name) { topicChip($0.name, "\($0.name) \($0.ids.count)") }
                        }
                    }
                    .contentMargins(.horizontal, 0, for: .scrollContent)   // 外面那栏的边距不要再叠一层
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            Section {
                ForEach(episodes) { e in
                    PlainLink(value: e) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                if !readState.isRead(e.id) {
                                    Circle().fill(p.accent).frame(width: 7, height: 7).accessibilityLabel("未读")
                                }
                                Text(Markdown.latinQuotes(e.title)).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(3)
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
                }
                ForEach(show.docs.sorted { $0.date > $1.date }) { d in
                    PlainLink(value: d) {
                        Text(Markdown.latinQuotes(d.title)).font(.heading(16)).foregroundStyle(p.ink)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                Text(topic.map { "「\($0)」里的单集" } ?? (show.kind == .summit ? "全部议题" : "全部单集"))
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
