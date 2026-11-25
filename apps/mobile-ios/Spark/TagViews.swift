import SwiftUI
import UIKit

/// 复盘页「主题」：标签词云、全部标签、一个标签下的文章。数据规则在 TagIndex.swift（同电脑 core/tags.py）。

struct TagRoute: Hashable, Identifiable { let tag: String; var id: String { tag } }
struct AllTagsRoute: Hashable {}

enum TagTarget { case doc(Doc), episode(Episode) }

@Observable @MainActor
final class TagModel {
    var index = TagIndex()
    var items: [TagItem] = []
    var targets: [String: TagTarget] = [:]
    var loaded = false

    func reload(_ vault: Vault) async {
        let lib = vault.library, root = Vault.root
        let index = TagIndex(json: vault.localText("Spark/.tags.json"))
        let (items, targets) = await Task.detached(priority: .userInitiated) { Self.collect(lib, root: root) }.value
        guard !Task.isCancelled else { return }
        self.index = index; self.items = items; self.targets = targets; loaded = true
    }

    /// 书架上的剪藏、论文、节目单集、信息文章 → 条目。类型分法同电脑：series 播客、summit 演讲、信息跟进的文章
    nonisolated static func collect(_ lib: Library, root: URL) -> ([TagItem], [String: TagTarget]) {
        var items: [TagItem] = [], targets: [String: TagTarget] = [:]
        func rel(_ p: String) -> String { p.hasPrefix("Spark/") ? String(p.dropFirst(6)) : p }
        func day(_ s: String) -> String { s.count == 8 ? "\(s.prefix(4))-\(s.dropFirst(4).prefix(2))-\(s.suffix(2))" : "" }
        func headed(_ d: Doc, _ type: String) {
            guard let m = ClipCache.note(d.path, root: root)?.meta else { return }
            items.append(TagItem(ref: d.path, type: type, paths: [rel(d.path)], headerTags: m.tags, title: m.title, source: d.source,
                                 date: m.clipped.isEmpty ? m.published : m.clipped))
            targets[d.path] = .doc(d)
        }
        for d in lib.clips { headed(d, "clip") }
        for d in lib.papers { headed(d, "paper") }
        for s in lib.shows {
            for e in s.episodes {
                let paths = [e.notePath, e.speechPath, e.transcriptPath, e.articlePath].compactMap { $0 }.map(rel)
                guard !paths.isEmpty else { continue }
                items.append(TagItem(ref: e.id, type: e.kind == .track ? "track" : e.kind == .series ? "episode" : "talk", paths: paths,
                                     title: e.title, source: e.showName, date: day(e.date)))
                targets[e.id] = .episode(e)
            }
            if s.kind == .track && s.name != "简报" {   // 没有处理记录的文章
                for d in s.docs {
                    items.append(TagItem(ref: d.path, type: "track", paths: [rel(d.path)], title: d.title, source: s.name, date: day(d.date)))
                    targets[d.path] = .doc(d)
                }
            }
        }
        return (items, targets)
    }
}

func typeLabel(_ type: String) -> String {
    ["clip": "剪藏", "paper": "论文", "episode": "播客", "talk": "演讲", "track": "信息"][type] ?? ""
}

// MARK: 词云

/// 词云里的一个词
struct TagPlacement {
    let tag: CloudTag
    let font: CGFloat
    let medium: Bool
    let rect: CGRect     // 文字占的位置
}

enum TagCloudLayout {
    static let minCount = 3                          // 词云只放至少这么多篇的标签
    static let maxWords = 63
    static let sizeRange: ClosedRange<CGFloat> = 12...30
    static let refWidth: CGFloat = 350               // 窄过它就按面积减少词数
    static let aspect: CGFloat = 0.9                 // 高 / 宽

    /// tags 已按篇数从多到少。字号按篇数的平方根映射；从中心沿螺旋向外找不重叠的位置，大的先放。
    /// 不用随机数：同样的数据和宽度，位置每次一样。
    static func place(_ tags: [CloudTag], in size: CGSize) -> [TagPlacement] {
        let share = min(1, (size.width / refWidth) * (size.width / refWidth))
        let words = Array(tags.prefix(max(1, Int(CGFloat(maxWords) * share))))
        guard let lo = words.last.map({ sqrt(Double($0.count)) }), let hi = words.first.map({ sqrt(Double($0.count)) }) else { return [] }
        let heavy = (words.count + 7) / 8
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        // 宽屏（折叠屏内屏、平板）：字跟着放大一些，横向拉得更开，把宽度用起来；手机宽度下和原来一样
        let scale = min(maxScale, max(1, size.width / scaleRefWidth))
        let stretch: CGFloat = size.width > scaleRefWidth * 1.25 ? wideStretch : 1.3
        var placed: [TagPlacement] = []
        for (i, w) in words.enumerated() {
            let f = scale * (hi > lo ? sizeRange.lowerBound + (sizeRange.upperBound - sizeRange.lowerBound) * CGFloat((sqrt(Double(w.count)) - lo) / (hi - lo))
                                     : (sizeRange.lowerBound + sizeRange.upperBound) / 2)
            let medium = i < heavy
            let text = (w.tag as NSString).size(withAttributes: [.font: UIFont.systemFont(ofSize: f, weight: medium ? .medium : .regular)])
            let box = CGSize(width: ceil(text.width) + 2, height: ceil(f * 1.15))
            guard box.width <= size.width else { continue }   // 比整个宽度还宽的词放不下，不用去找
            var theta: CGFloat = 0
            while true {
                let r = 2 * theta
                if r > 6 * max(size.width, size.height) { break }   // 只当保险：正常情况下往下总有地方
                let at = CGPoint(x: center.x + stretch * r * cos(theta), y: center.y + r * sin(theta))
                let rect = CGRect(x: at.x - box.width / 2, y: at.y - box.height / 2, width: box.width, height: box.height)
                // 只限左右不出界，上下不限：篇数排前面的词一个都不能因为放不下被丢掉（高度由 fitted 按实际占的地方算）
                if rect.minX >= 0, rect.maxX <= size.width,
                   !placed.contains(where: { $0.rect.insetBy(dx: -4, dy: -1).intersects(rect) }) {
                    placed.append(TagPlacement(tag: w, font: f, medium: medium, rect: rect))
                    break
                }
                theta += 4 / max(r, 8)
            }
        }
        return placed
    }

    static let scaleRefWidth: CGFloat = 400          // 比这宽的屏，字按宽度的比例放大
    static let maxScale: CGFloat = 1.6               // 最多放大到这么多倍
    static let wideStretch: CGFloat = 2.2            // 宽屏上横向比纵向拉开多少倍
    static let pad: CGFloat = 12                     // 词云上下各留这么多

    /// 给定宽度排好，再把上下的空白裁掉：返回的位置从 pad 开始，height 是词实际占的高度加上下的 pad。
    /// 宽屏上不这样做的话，词只占画布中间一块，上下各空一大截，横屏还会被截掉。
    static func fitted(_ tags: [CloudTag], width: CGFloat) -> (placed: [TagPlacement], height: CGFloat) {
        guard width > 0 else { return ([], 0) }
        let raw = place(tags, in: CGSize(width: width, height: width * aspect))
        guard let top = raw.map(\.rect.minY).min(), let bottom = raw.map(\.rect.maxY).max() else { return ([], 0) }
        return (raw.map { TagPlacement(tag: $0.tag, font: $0.font, medium: $0.medium, rect: $0.rect.offsetBy(dx: 0, dy: pad - top)) },
                bottom - top + 2 * pad)
    }

    /// 类别三种色相，每种三档（前 1/3 最醒目）。浅色底用深色，深色底用浅色；对比度在样例库的几种底色上都 ≥ 4.5
    static func color(kind: String, tier: Int, dark: Bool) -> Color {
        // 宝石蓝 1D4ED8、祖母绿 047857、玫瑰绯红 BE123C 各配三档（同电脑 apps/review/static/index.html 的 --c-*）
        let light: [String: [UInt32]] = ["tech": [0x1e3a8a, 0x1d4ed8, 0x2e6ff0], "org": [0x065f46, 0x047857, 0x058a61],
                                         "biz": [0x9f1239, 0xbe123c, 0xe11d48]]
        let night: [String: [UInt32]] = ["tech": [0xbfdbfe, 0x93c5fd, 0x60a5fa], "org": [0xa7f3d0, 0x6ee7b7, 0x34d399],
                                         "biz": [0xfecdd3, 0xfda4af, 0xfb7185]]
        return .hex((dark ? night : light)[kind, default: (dark ? night : light)["tech"]!][tier])
    }
}

struct TagCloudView: View {
    let open: (String) -> Void
    let tags: [CloudTag]
    @Environment(\.palette) private var p
    @State private var width: CGFloat = 0

    var body: some View {
        let w = width   // 用满这一行的宽度
        let fit = TagCloudLayout.fitted(tags, width: w)
        // 占位的一条只定高度、宽度跟着这一行走，词云盖在它上面：量宽度量的是这一条，不受词云内容影响。
        // 直接量词云的话，屏幕转窄时词云还撑着旧的宽度，量到的不会变小，就溢出屏幕了
        Color.clear.frame(height: fit.height).frame(maxWidth: .infinity)
            .background(GeometryReader { geo in
                Color.clear.onAppear { width = geo.size.width }.onChange(of: geo.size.width) { _, new in width = new }
            })
            .overlay(alignment: .top) { cloud(fit, width: w) }
    }

    private func cloud(_ fit: (placed: [TagPlacement], height: CGFloat), width w: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(Array(fit.placed.enumerated()), id: \.element.tag.tag) { i, t in
                Button { open(t.tag.tag) } label: {
                    Text(t.tag.tag)
                        .font(.system(size: t.font, weight: t.medium ? .medium : .regular))
                        .foregroundStyle(TagCloudLayout.color(kind: t.tag.kind, tier: i * 3 / fit.placed.count, dark: p.isDark))
                        .lineLimit(1).fixedSize()
                        .frame(width: max(t.rect.width, 44), height: max(t.rect.height, 44))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)
                .accessibilityLabel("\(t.tag.tag)，\(t.tag.count) 篇")
                .position(x: t.rect.midX, y: t.rect.midY)
            }
        }
        .frame(width: w, height: fit.height)   // 高度跟着词实际占的地方走
    }
}

// MARK: 页面

/// 「主题」那一段：词云，下面是「全部标签」入口
struct TopicsSection: View {
    let open: (String) -> Void
    let model: TagModel
    @Environment(\.palette) private var p

    var body: some View {
        let cloud = model.index.cloud(model.items, minCount: TagCloudLayout.minCount)
        let all = model.index.cloud(model.items, minCount: 1)
        if model.loaded && cloud.isEmpty {
            Text("还没有主题。电脑上补过标签、同步之后会出现在这里。")
                .font(.system(size: 13.5)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity).padding(.vertical, 40)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
        if !cloud.isEmpty {
            TagCloudView(open: open, tags: cloud)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 16, leading: 20, bottom: 16, trailing: 20))
        }
        if !all.isEmpty {
            PlainLink(value: AllTagsRoute()) {
                HStack {
                    Text("全部标签").font(.system(size: 15.5)).foregroundStyle(p.ink)
                    Spacer()
                    Text("\(all.count) 个").font(.system(size: 13)).foregroundStyle(p.ink2)
                    Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.ink2)
                }
                .frame(minHeight: 44)
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
            .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 0, trailing: 20))
        }
    }
}

/// 全部标签：标签、篇数，可搜索，按篇数降序
struct AllTagsView: View {
    let model: TagModel
    @Environment(\.palette) private var p
    @State private var query = ""

    var body: some View {
        let q = query.trimmingCharacters(in: .whitespaces)
        let rows = model.index.cloud(model.items, minCount: 1).filter { q.isEmpty || $0.tag.localizedCaseInsensitiveContains(q) }
        List(rows, id: \.tag) { t in
            PlainLink(value: TagRoute(tag: t.tag)) {
                HStack {
                    Text(t.tag).font(.system(size: 15.5)).foregroundStyle(p.ink)
                    Spacer()
                    Text("\(t.count) 篇").font(.system(size: 13)).foregroundStyle(p.ink2)
                }
                .frame(minHeight: 44).contentShape(Rectangle())
            }
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
            .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 0, trailing: 20))
            .accessibilityLabel("\(t.tag)，\(t.count) 篇")
        }
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "搜标签")
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("全部标签").navigationBarTitleDisplayMode(.inline)
    }
}

/// 一个标签下的文章：新到旧；点一行打开阅读页
struct TagItemsView: View {
    let tag: String
    let model: TagModel
    @Environment(\.palette) private var p
    @Environment(Vault.self) private var vault
    @Environment(LocalJobs.self) private var jobs
    @State private var note: String?
    @State private var sending = false
    static let reviewMin = 3   // 少于这么多篇不写综述

    /// 写综述：手机上能生成就在手机上跑（材料取自这一页的前 40 篇），否则交给电脑（电脑自己从标签索引里取）
    private func writeReview(_ rows: [TagItem]) {
        note = nil
        if jobs.canRun {
            let picks = rows.prefix(TagReviewPrompt.maxItems).map {
                ReviewRunner.TagPick(type: $0.type, path: TagIndex.mainFile($0.paths), title: $0.title, source: $0.source, date: $0.date)
            }
            note = jobs.busy ? "手机上正在生成另一份，这份排在后面，跑完接着跑。" : nil
            jobs.tagReview(tag: tag, picks: Array(picks), total: rows.count, vault: vault)
        } else {
            sending = true
            Task {
                defer { sending = false }
                do {
                    let route = try await vault.submitTask(SparkTask.tagReview(tag))
                    note = "主题综述：\(vault.routeNote(route))"
                } catch { note = "没交出去：\(error.localizedDescription)" }
            }
        }
    }

    var body: some View {
        let rows = model.index.items(for: tag, in: model.items)
        List {
            if let note {
                Text(note).font(.system(size: 13.5)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            if jobs.jobs.contains(where: { $0.kind == "review" }) {
                Section {
                    LocalJobRows(kind: "review")
                } header: { Text("在手机上生成").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil) }
            }
            ForEach(rows, id: \.ref) { it in
                link(it) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(Markdown.latinQuotes(it.title)).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(2).lineLimit(2)
                        HStack(spacing: 8) {
                            Text(typeLabel(it.type)).font(.system(size: 11, weight: .medium)).foregroundStyle(p.ink2)
                                .padding(.horizontal, 6).padding(.vertical, 2).background(p.chip, in: Capsule())
                            Text(it.source).font(.system(size: 12.5)).foregroundStyle(p.ink2).lineLimit(1)
                            Spacer(minLength: 4)
                            Text(Dates.label(it.date.replacingOccurrences(of: "-", with: ""))).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                                .fixedSize()
                        }
                    }
                    .padding(.vertical, 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
            }
        }
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("\(tag) · \(rows.count) 篇").navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                if rows.count >= Self.reviewMin { Button("写综述") { writeReview(rows) }.disabled(sending) }
                if !rows.isEmpty { NavigationLink("讨论", value: DiscussRoute(tag: tag)) }
            }
        }
    }

    @ViewBuilder
    private func link<L: View>(_ it: TagItem, @ViewBuilder _ label: @escaping () -> L) -> some View {
        switch model.targets[it.ref] {
        case .doc(let d): PlainLink(value: d, label: label)
        case .episode(let e): PlainLink(value: e, label: label)
        case nil: label()
        }
    }
}

extension View {
    /// 复盘页挂上主题：开着「主题」页签时读数据；全部标签、一个标签下的文章两个落点
    func topicsPages(_ model: TagModel, active: Bool) -> some View { modifier(TopicsPages(model: model, active: active)) }
}

private struct TopicsPages: ViewModifier {
    let model: TagModel
    let active: Bool
    @Environment(Vault.self) private var vault

    func body(content: Content) -> some View {
        content
            .task(id: "\(active)|\(vault.library.clips.count)|\(vault.library.shows.count)|\(vault.lastSync?.timeIntervalSince1970 ?? 0)") {
                if active { await model.reload(vault) }
            }
            .navigationDestination(for: AllTagsRoute.self) { _ in AllTagsView(model: model) }
            .navigationDestination(for: TagRoute.self) { TagItemsView(tag: $0.tag, model: model) }
            .navigationDestination(for: DiscussRoute.self) { DiscussView(tag: $0.tag, model: model) }
    }
}

/// 复盘页的导航栈：词云里的词是按钮，点了要压一页进栈，所以这个栈带着 path
struct ReviewStack: View {
    @State private var path = NavigationPath()

    var body: some View {
        NavigationStack(path: $path) {
            ReviewView(openTag: { path.append(TagRoute(tag: $0)) }, openDiscuss: { path.append(DiscussRoute(tag: "")) })
        }
    }
}
