import SwiftUI

struct LibraryView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var tab: Category = .series
    @State private var query = ""

    var body: some View {
        List {
            Picker("类别", selection: $tab) {
                ForEach(Category.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear).listRowSeparator(.hidden)

            if !query.isEmpty {
                searchResults
            } else if tab == .report {
                ForEach(vault.library.reports.sorted { $0.date > $1.date }) { d in
                    NavigationLink(value: d) { row(d.title, Dates.label(d.date), unread: 0) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } else {
                ForEach(vault.library.shows(tab)) { s in
                    NavigationLink(value: s) {
                        row(s.name, [s.countLabel, s.latest.isEmpty ? "" : "\(Dates.label(s.latest))更新"]
                                .filter { !$0.isEmpty }.joined(separator: "，"),
                            unread: s.episodes.filter { !readState.isRead($0.id) }.count)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            }
        }
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("书架")
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
            NavigationLink(value: s) { row(s.name, s.countLabel, unread: 0) }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        }
        ForEach(Array(eps)) { e in
            NavigationLink(value: e) { row(e.title, "\(e.showName)　\(Dates.label(e.date))", unread: 0) }
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
                        .frame(maxHeight: expanded ? nil : prefs.size * 7.5, alignment: .top)
                        .clipped()
                    Button(expanded ? "收起" : "展开全文") { withAnimation { expanded.toggle() } }
                        .font(.system(size: 14)).foregroundStyle(p.accent).frame(minHeight: 40)
                        .buttonStyle(.plain)
                }
                .padding(16)
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
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            Section {
                ForEach(episodes) { e in
                    NavigationLink(value: e) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                if !readState.isRead(e.id) {
                                    Circle().fill(p.accent).frame(width: 7, height: 7).accessibilityLabel("未读")
                                }
                                Text(e.title).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(3)
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
                    NavigationLink(value: d) {
                        Text(d.title).font(.heading(16)).foregroundStyle(p.ink)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                Text(topic.map { "「\($0)」里的单集" } ?? (show.kind == .summit ? "全部议题" : "全部单集"))
                    .font(.heading(14)).foregroundStyle(p.ink).textCase(nil)
            }
        }
        .listStyle(.plain)
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
