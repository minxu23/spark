import SwiftUI

struct LatestView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var filter: Category?
    @State private var query = ""
    @State private var scope: Category?
    @State private var hits: [LibrarySearch.Hit] = []
    @State private var searching = false

    private var groups: [(day: String, items: [FeedItem])] {
        var out: [(String, [FeedItem])] = []
        for item in vault.library.feed where filter == nil || item.kind == filter {
            if out.last?.0 == item.date { out[out.count - 1].1.append(item) } else { out.append((item.date, [item])) }
        }
        return out.prefix(60).map { $0 }
    }

    var body: some View {
        List {
            if !query.trimmingCharacters(in: .whitespaces).isEmpty {
                searchResults
            } else {
                feedContent
            }
        }
        .readableColumn()
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("最新")
        .searchable(text: $query, prompt: "搜标题、正文、标签和批注")
        .searchScopes($scope) {
            Text("全部").tag(Category?.none)
            ForEach([Category.note, .clip, .summit, .series, .track, .report]) { Text($0.label).tag(Optional($0)) }
        }
        .task(id: "\(query)|\(scope?.rawValue ?? "")") { await search() }
        .navigationDestination(for: Show.self) { ShowView(show: $0) }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { SyncButton() }
            ToolbarItem(placement: .topBarTrailing) { AddMenu() }
        }
        .refreshable { await vault.sync() }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
        .navigationDestination(for: ReadingNoteRef.self) { ReadingNoteView(path: $0.path, episode: $0.episode) }
        .navigationDestination(for: DocFocus.self) { DocReaderView(doc: $0.doc, focus: $0.focus) }
        .navigationDestination(for: EpisodeFocus.self) { ReaderView(episode: $0.episode, focusPath: $0.path, focus: $0.focus.isEmpty ? nil : $0.focus) }
    }

    @ViewBuilder
    private var searchResults: some View {
        if searching && hits.isEmpty {
            HStack(spacing: 8) { ProgressView(); Text("正在搜").foregroundStyle(p.ink2) }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } else if hits.isEmpty {
            Text("没找到「\(query)」。整理稿和文字记录不在搜索范围里，可以换个说法，或者去书架里按节目找。")
                .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } else {
            Text("找到 \(hits.count) 条\(hits.count >= 150 ? "（只列前 150 条）" : "")").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            ForEach(hits) { h in
                Group {
                    switch h.target {
                    case .episode(let e): PlainLink(value: e) { SearchHitRow(hit: h) }
                    case .doc(let d): PlainLink(value: d) { SearchHitRow(hit: h) }
                    case .show(let sh): PlainLink(value: sh) { SearchHitRow(hit: h) }
                    }
                }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
            }
        }
    }

    /// 停手 0.3 秒再搜；读文件、找词放在后台
    private func search() async {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { hits = []; searching = false; return }
        try? await Task.sleep(for: .milliseconds(300))
        guard !Task.isCancelled else { return }
        searching = true
        let (all, targets) = vault.searchItems()
        let items = scope.map { c in all.filter { $0.kind == c } } ?? all
        let root = Vault.root
        // 又打了字：前一次的扫描停掉，不让几遍全库扫描同时跑；「正在搜」留给新的那次去关
        let scan = Task.detached(priority: .userInitiated) { LibrarySearch.run(q, items: items, root: root) }
        let found = await withTaskCancellationHandler { await scan.value } onCancel: { scan.cancel() }
        guard !Task.isCancelled else { return }
        searching = false
        hits = found.map { f in
            let it = all[f.index]
            let snippet = f.snippet.map { LibrarySearch.highlight((f.fromNote ? "批注：" : "") + $0, q, tint: p.accent) }
            return LibrarySearch.Hit(id: "\(f.index)", kind: it.kind, title: it.title, meta: it.meta, snippet: snippet, missing: f.missing, target: targets[f.index])
        }
    }

    @ViewBuilder
    private var feedContent: some View {
            PageStatus(text: syncNote, warning: vault.lastError != nil)
            SectionTabs(items: [(Category?.none, "全部")] + Self.order.map { (Optional($0), $0.label) }, selection: $filter)

            RunningTaskCards()

            if vault.library.feed.isEmpty {
                EmptyHint(syncing: vault.syncing, cable: vault.api == nil)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            let _ = CrashLog.list("最新", groups.map { $0.items.map(\.id) }, keys: groups.map(\.day))
            ForEach(groups, id: \.day) { group in
                Section {
                    ForEach(group.items) { item in
                        FeedRow(item: item, unread: !readState.isRead(item.id))
                            .listRowBackground(Color.clear)
                            .listRowSeparatorTint(p.rule)
                            .listRowInsets(EdgeInsets(top: 12, leading: 20, bottom: 14, trailing: 20))
                    }
                } header: {
                    Text(Dates.label(group.day, weekday: true))
                        .font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2)
                        .textCase(nil)
                }
            }
    }

    /// 来源的顺序：笔记（随手记）、剪藏放最前，跟书架一致
    static let order: [Category] = [.note, .clip, .summit, .series, .track, .report]

    /// 大标题下的一句话：同步到哪了、今天新到几篇
    private var syncNote: String {
        if let e = vault.lastError { return "同步没成功：\(e)" }
        if vault.syncing { return "正在同步：\(vault.progress.done)/\(vault.progress.total)" }
        var parts: [String] = []
        if let t = vault.lastSync { parts.append("同步于 \(t.formatted(date: .omitted, time: .shortened))") } else { parts.append("还没同步过") }
        if vault.lastAdded > 0 { parts.append("新增 \(vault.lastAdded) 篇，新的标着小圆点") }
        else {
            let today = Dates.todayKey
            let n = vault.library.feed.filter { $0.date == today }.count
            parts.append(n > 0 ? "今天新到 \(n) 篇" : "今天还没有新内容")
        }
        if vault.deferred > 0 { parts.append("\(vault.deferred) 份整理稿等连上 Wi-Fi 再下") }
        return parts.joined(separator: "，")
    }
}

struct SyncButton: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    var body: some View {
        Button { Task { await vault.sync() } } label: {
            if vault.syncing { ProgressView() } else { Image(systemName: "arrow.triangle.2.circlepath") }
        }
        .disabled(vault.syncing)
        .accessibilityLabel("现在同步")
    }
}
struct FeedRow: View {
    let item: FeedItem
    let unread: Bool
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p

    var body: some View {
        switch item {
        case .episode(let e):
            PlainLink(value: e) {
                content(source: e.showName, meta: e.durationLabel, title: e.title, tldr: e.tldr, kind: e.kind)
            }
            .saveToClips(e)
        case .doc(let d):
            PlainLink(value: d) {
                content(source: d.source, meta: d.kind == .report ? "报告" : d.kind == .clip ? "剪藏" : d.kind == .note ? "" : "简报",
                        title: d.title, tldr: d.tldr, kind: d.kind)
            }
        }
    }

    private func content(source: String, meta: String, title: String, tldr: String, kind: Category) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 7) {
                Circle().fill(kind.color).frame(width: 8, height: 8).accessibilityHidden(true)
                Text(source).lineLimit(1)
                Spacer()
                Text(meta)
            }
            .font(.system(size: 12.5)).foregroundStyle(p.ink2)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if unread {
                    Circle().fill(p.accent).frame(width: 7, height: 7).accessibilityLabel("未读")
                }
                Text(Markdown.latinQuotes(title)).font(.heading(17)).lineSpacing(3).foregroundStyle(p.ink)
            }
            if !tldr.isEmpty {
                Text(tldr).font(Fonts.font(prefs.bodyFont.fontName, size: 15)).lineSpacing(4)
                    .foregroundStyle(p.ink2).lineLimit(2)
            }
        }
    }
}

struct EmptyHint: View {
    let syncing: Bool
    var cable = false
    @Environment(\.palette) private var p
    var body: some View {
        VStack(spacing: 10) {
            if syncing { ProgressView() }
            Text(syncing ? "第一次同步，正在下载处理记录和节目总结…"
                 : cable ? "还没有内容。插上数据线，在电脑上 Spark 的设置页「数据线同步到手机」里点「同步到这台」，回到这里下拉一下。"
                 : "还没有内容。下拉同步一次，或者看看电脑上的 Spark 有没有把笔记推上来。")
                .font(.system(size: 14)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 60)
    }
}
