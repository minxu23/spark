import SwiftUI

struct LatestView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(\.palette) private var p
    @State private var filter: Category?

    private var groups: [(day: String, items: [FeedItem])] {
        var out: [(String, [FeedItem])] = []
        for item in vault.library.feed where filter == nil || item.kind == filter {
            if out.last?.0 == item.date { out[out.count - 1].1.append(item) } else { out.append((item.date, [item])) }
        }
        return out.prefix(60).map { $0 }
    }

    var body: some View {
        List {
            Section {
                if let note = syncNote {
                    Text(note).font(.system(size: 13)).padding(.vertical, 9).padding(.horizontal, 12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(p.chip, in: RoundedRectangle(cornerRadius: 8))
                        .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 6, trailing: 20))
                }
                ChipBar(selection: $filter)
                    .listRowInsets(EdgeInsets(top: 0, leading: 0, bottom: 6, trailing: 0))
            }
            .listRowBackground(Color.clear)
            .listRowSeparator(.hidden)

            if vault.library.feed.isEmpty {
                EmptyHint(syncing: vault.syncing, cable: vault.api == nil)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

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
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("最新")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { SyncButton() }
        }
        .refreshable { await vault.sync() }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
    }

    private var syncNote: String? {
        if let e = vault.lastError { return "同步没成功：\(e)" }
        if vault.syncing { return "正在同步：\(vault.progress.done)/\(vault.progress.total)" }
        if vault.deferred > 0 {
            return "用的是流量，\(vault.deferred) 份整理稿和文字记录等连上 Wi-Fi 再下。小结都已同步。"
        }
        if vault.lastAdded > 0 || vault.lastUpdated > 0 {
            return "刚同步完：新增 \(vault.lastAdded) 篇，更新 \(vault.lastUpdated) 个文件。新的标着小圆点。"
        }
        return nil
    }
}

struct SyncButton: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    var body: some View {
        Button { Task { await vault.sync() } } label: {
            HStack(spacing: 6) {
                if let t = vault.lastSync, !vault.syncing {
                    Text("同步于 \(t.formatted(date: .omitted, time: .shortened))").font(.system(size: 13)).foregroundStyle(p.ink2)
                }
                if vault.syncing { ProgressView() } else { Image(systemName: "arrow.triangle.2.circlepath") }
            }
        }
        .disabled(vault.syncing)
        .accessibilityLabel("现在同步")
    }
}

struct ChipBar: View {
    @Binding var selection: Category?
    @Environment(\.palette) private var p
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                chip(nil, "全部")
                ForEach(Category.allCases) { chip($0, $0.label) }
            }
            .padding(.horizontal, 20)
        }
    }
    private func chip(_ c: Category?, _ label: String) -> some View {
        let on = selection == c
        return Button { selection = c } label: {
            Text(label).font(.system(size: 14)).padding(.horizontal, 14).frame(minHeight: 36)
                .foregroundStyle(on ? p.paper : p.ink)
                .background(on ? p.accent : .clear, in: Capsule())
                .overlay(Capsule().stroke(on ? p.accent : p.rule))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
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
        case .doc(let d):
            PlainLink(value: d) {
                content(source: d.source, meta: d.kind == .report ? "报告" : "简报", title: d.title, tldr: "", kind: d.kind)
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
