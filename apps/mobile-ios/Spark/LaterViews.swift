import SwiftUI

/// 稍后再读：文章文件头里的一行 later:（写法见 LaterRule）。这里是界面：列表上的小标签、书架的「稍后再读」页签、
/// 列表长按菜单和阅读页「更多」用的加入、移出。纯逻辑都在 Later.swift，电脑端同一套（core/annotations.py）。

/// 列表行上的小标签「稍后再读」，样式跟「没进电脑」「有划线/批注」一样
struct LaterBadge: View {
    @Environment(\.palette) private var p
    var text = "稍后再读"

    var body: some View {
        Text(text).font(.system(size: 11, weight: .medium)).foregroundStyle(p.accent)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(p.accent.opacity(0.1), in: Capsule())
            .accessibilityLabel(text)
    }
}

/// 占位剪藏的小标签
struct StubBadge: View {
    @Environment(\.palette) private var p

    var body: some View {
        Text("没抓到正文").font(.system(size: 11, weight: .medium)).foregroundStyle(p.ink2)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(p.ink2.opacity(0.12), in: Capsule())
            .accessibilityLabel("正文还没抓到")
    }
}

extension FeedItem {
    /// 星标、稍后再读记在哪份文件的文件头上：文档是它自己；一期节目是第一份能做笔记的稿子（小结，没有小结的简讯是整理稿），
    /// 同阅读页的 ReaderPlace.starPath
    var flagPath: String? {
        switch self {
        case .doc(let d): d.path
        case .episode(let e): ReaderPlace.starPath([e.notePath, e.speechPath, e.transcriptPath, e.articlePath])
        }
    }

    var title: String {
        switch self {
        case .doc(let d): d.title
        case .episode(let e): e.title
        }
    }
}

/// 稍后再读列表里的一条
struct LaterItem: Identifiable, Hashable {
    let item: FeedItem
    let path: String
    /// 文件头 later: 的值（加入的时刻）
    let value: String
    let stub: Bool
    var id: String { item.id }
}

extension Vault {
    /// 所有在稍后再读里的文章：剪藏、随手记、报告、复盘、会议资料、信息跟进的文章，一期节目（记在小结或整理稿上）。
    /// 只读每个文件开头一段看文件头；按加入时刻从新到旧，「later: true」这种没有时刻的排在后面、按文件修改时间（LaterRule.order）
    func laterItems() async -> [LaterItem] {
        let lib = library, root = Self.root
        return await Task.detached(priority: .userInitiated) { () -> [LaterItem] in
            func read(_ path: String) -> (head: String, modified: Double)? {
                guard DocEdit.annotatable(path) else { return nil }
                let url = root.appending(path: path, directoryHint: .notDirectory)
                guard let h = try? FileHandle(forReadingFrom: url) else { return nil }
                defer { try? h.close() }
                guard let data = try? h.read(upToCount: 16384) else { return nil }
                let m = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate?.timeIntervalSince1970 ?? 0
                return (String(decoding: data, as: UTF8.self), m)
            }
            var rows: [(LaterItem, Double?, Double)] = []
            func add(_ item: FeedItem, _ path: String) {
                guard let r = read(path) else { return }
                let v = LaterRule.value(r.head)
                guard !v.isEmpty else { return }
                rows.append((LaterItem(item: item, path: path, value: v, stub: LaterRule.isStub(r.head)), LaterRule.instant(v), r.modified))
            }
            for e in lib.allEpisodes {
                if let p = ReaderPlace.starPath([e.notePath, e.speechPath, e.transcriptPath, e.articlePath]) { add(.episode(e), p) }
            }
            for d in lib.clips + lib.notes + lib.reports + lib.shows.flatMap(\.docs) { add(.doc(d), d.path) }
            // 同一份文件只列一次（一期节目和它的文件重名之类）
            var seen = Set<String>()
            return rows.filter { seen.insert($0.0.id).inserted }
                .sorted { LaterRule.order((at: $0.1, modified: $0.2), (at: $1.1, modified: $1.2)) }.map(\.0)
        }.value
    }

    /// 加进、移出稍后再读：改那份文件的文件头，存好交给电脑（已经在里面再加一次，文件不动）。返回没存上的原因，成功是 nil
    @discardableResult
    func setLater(path: String, on: Bool) async -> String? {
        do {
            let text = try await self.text(path)
            let new = LaterRule.with(text, on: on, value: LaterRule.now())
            guard new != text else { return nil }
            try await saveEdited(path: path, text: new)
            return nil
        } catch {
            return "没存上：\(error.localizedDescription)"
        }
    }
}

/// 列表长按菜单里的一项：加进或移出稍后再读
struct LaterMenuButton: View {
    let path: String
    let on: Bool
    var done: (String?) -> Void = { _ in }
    @Environment(Vault.self) private var vault

    var body: some View {
        Button(on ? "移出稍后再读" : "稍后再读", systemImage: on ? "clock.badge.xmark" : "clock") {
            Task { done(await vault.setLater(path: path, on: !on)) }
        }
    }
}

/// 书架「稍后再读」：所有模块里加了稍后再读的文章，按加入时刻从新到旧
struct LaterShelf: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var items: [LaterItem] = []
    @State private var loaded = false
    @State private var note: String?

    var body: some View {
        Group {
            if !loaded {
                ProgressView().frame(maxWidth: .infinity).listRowBackground(Color.clear).listRowSeparator(.hidden)
            } else if items.isEmpty {
                Text("还没有稍后再读。读文章时点右上角「更多」里的「稍后再读」，在列表里长按一篇也能加；也可以在「最新」页右上角的 + 里选「粘贴链接，稍后再读」。")
                    .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            } else {
                Text("\(items.count) 篇，新加的在前。读完后长按一篇，选「移出稍后再读」。")
                    .font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                    .accessibilityIdentifier("later-count")
            }
            if let note {
                Text(note).font(.system(size: 13)).foregroundStyle(Color.hex(0xb42318))
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            let _ = CrashLog.list("书架·稍后再读", items.map(\.id))
            ForEach(items) { it in
                switch it.item {
                case .episode(let e):
                    PlainLink(value: e) { line(it, [e.short ? "简讯" : e.kind.label, e.showName, Dates.label(e.date)]) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                        .contextMenu { menu(it) }
                case .doc(let d):
                    PlainLink(value: d) { line(it, [d.kind.label, d.source, Dates.label(d.date)]) }
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                        .contextMenu { menu(it) }
                }
            }
        }
        .task(id: vault.library.feed.count) { await load() }
        .onAppear { Task { await load() } }   // 从阅读页回来：刚加、刚移出的马上反映
    }

    @ViewBuilder
    private func menu(_ it: LaterItem) -> some View {
        LaterMenuButton(path: it.path, on: true) { err in
            note = err
            Task { await load() }
        }
    }

    private func load() async {
        items = await vault.laterItems()
        loaded = true
    }

    private func line(_ it: LaterItem, _ meta: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(meta.filter { !$0.isEmpty }.joined(separator: "　")).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                if it.stub { StubBadge() }
                PendingBadge(path: it.path)
                Spacer()
                if let at = LaterRule.instant(it.value) {
                    Text("加入于 \(Self.when(Date(timeIntervalSince1970: at)))").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                }
            }
            Text(Markdown.latinQuotes(it.item.title)).font(.heading(16.5)).foregroundStyle(p.ink).lineSpacing(3)
        }
        .padding(.vertical, 4)
    }

    private static func when(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.month, .day], from: d)
        return "\(c.month ?? 0)月\(c.day ?? 0)日"
    }
}
