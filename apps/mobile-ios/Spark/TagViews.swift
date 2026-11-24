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
        for s in lib.shows {
            if s.id == "Spark/论文" { s.docs.forEach { headed($0, "paper") }; continue }
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

private func typeLabel(_ type: String) -> String {
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

    /// tags 已按篇数从多到少。字号按篇数的平方根映射；从中心沿螺旋向外找不重叠的位置，大的先放；放不下的不放。
    /// 不用随机数：同样的数据和宽度，位置每次一样。
    static func place(_ tags: [CloudTag], in size: CGSize) -> [TagPlacement] {
        let share = min(1, (size.width / refWidth) * (size.width / refWidth))
        let words = Array(tags.prefix(max(1, Int(CGFloat(maxWords) * share))))
        guard let lo = words.last.map({ sqrt(Double($0.count)) }), let hi = words.first.map({ sqrt(Double($0.count)) }) else { return [] }
        let heavy = (words.count + 7) / 8
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        var placed: [TagPlacement] = []
        for (i, w) in words.enumerated() {
            let f = hi > lo ? sizeRange.lowerBound + (sizeRange.upperBound - sizeRange.lowerBound) * CGFloat((sqrt(Double(w.count)) - lo) / (hi - lo))
                            : (sizeRange.lowerBound + sizeRange.upperBound) / 2
            let medium = i < heavy
            let text = (w.tag as NSString).size(withAttributes: [.font: UIFont.systemFont(ofSize: f, weight: medium ? .medium : .regular)])
            let box = CGSize(width: ceil(text.width) + 2, height: ceil(f * 1.15))
            var theta: CGFloat = 0
            while true {
                let r = 2 * theta
                if r > max(size.width, size.height) { break }
                let at = CGPoint(x: center.x + 1.3 * r * cos(theta), y: center.y + r * sin(theta))
                let rect = CGRect(x: at.x - box.width / 2, y: at.y - box.height / 2, width: box.width, height: box.height)
                if rect.minX >= 0, rect.minY >= 0, rect.maxX <= size.width, rect.maxY <= size.height,
                   !placed.contains(where: { $0.rect.insetBy(dx: -4, dy: -1).intersects(rect) }) {
                    placed.append(TagPlacement(tag: w, font: f, medium: medium, rect: rect))
                    break
                }
                theta += 4 / max(r, 8)
            }
        }
        return placed
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

    var body: some View {
        Color.clear.aspectRatio(1 / TagCloudLayout.aspect, contentMode: .fit)
            .overlay {
                GeometryReader { geo in
                    let placed = TagCloudLayout.place(tags, in: geo.size)
                    ZStack(alignment: .topLeading) {
                        ForEach(Array(placed.enumerated()), id: \.element.tag.tag) { i, w in
                            Button { open(w.tag.tag) } label: {
                                Text(w.tag.tag)
                                    .font(.system(size: w.font, weight: w.medium ? .medium : .regular))
                                    .foregroundStyle(TagCloudLayout.color(kind: w.tag.kind, tier: i * 3 / placed.count, dark: p.isDark))
                                    .lineLimit(1).fixedSize()
                                    .frame(width: max(w.rect.width, 44), height: max(w.rect.height, 44))
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("\(w.tag.tag)，\(w.tag.count) 篇")
                            .position(x: w.rect.midX, y: w.rect.midY)
                        }
                    }
                }
            }
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

    var body: some View {
        let rows = model.index.items(for: tag, in: model.items)
        List(rows, id: \.ref) { it in
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
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("\(tag) · \(rows.count) 篇").navigationBarTitleDisplayMode(.inline)
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
    }
}

/// 复盘页的导航栈：词云里的词是按钮，点了要压一页进栈，所以这个栈带着 path
struct ReviewStack: View {
    @State private var path = NavigationPath()

    var body: some View {
        NavigationStack(path: $path) { ReviewView(openTag: { path.append(TagRoute(tag: $0)) }) }
    }
}
