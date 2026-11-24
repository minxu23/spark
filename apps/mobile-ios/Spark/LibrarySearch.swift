import SwiftUI

/// 「最新」上的检索：标题、一句话、标签、小结和剪藏正文、批注一起搜。
/// 整理稿和文字记录很长，不在搜索范围里；文件还没下载到手机的只按标题和一句话找，结果上标「未下载」。
enum LibrarySearch {
    enum Target: Hashable {
        case episode(Episode)
        case doc(Doc)
        case show(Show)
    }

    struct Hit: Identifiable {
        let id: String
        let kind: Category
        let title: String
        let meta: String
        let snippet: AttributedString?
        let missing: Bool          // 正文没下载，只按标题找到的
        let target: Target
    }

    /// 交给后台线程的一条：只带字符串，不带界面里的模型
    struct Item: Sendable {
        let index: Int
        let kind: Category
        let title: String
        let meta: String
        let date: String
        let fields: [String]       // 标题以外直接比的：一句话、标签、来源、嘉宾
        let paths: [String]        // 要读正文的文件（相对笔记库）
    }

    struct Found: Sendable {
        let index: Int
        let titleHit: Bool
        let snippet: String?
        let fromNote: Bool
        let missing: Bool
        let date: String
    }

    /// 在后台跑：读文件、找词、截一小段
    static func run(_ query: String, items: [Item], root: URL) -> [Found] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return [] }
        var out: [Found] = []
        // 没有英文字母的词按字节预筛；有英文字母的大小写写法太多（openai / OpenAI），交给 NSString 不分大小写找
        let needles = q.contains(where: { $0.isASCII && $0.isLetter }) ? [] : [Data(q.utf8)]
        for it in items {
            if Task.isCancelled { return [] }
            let titleHit = it.title.range(of: q, options: [.caseInsensitive, .diacriticInsensitive]) != nil
            var snippet: String?, fromNote = false, missing = false
            for f in it.fields where snippet == nil {
                if let r = f.range(of: q, options: .caseInsensitive) { snippet = cut(f, r) }
            }
            if snippet == nil {
                for p in it.paths {
                    let url = root.appending(path: p, directoryHint: .notDirectory)
                    guard let data = FileManager.default.contents(atPath: url.path) else { missing = true; continue }
                    // 没有英文字母的词先按字节找（快），没有就跳过，不用解码整篇
                    if !needles.isEmpty, !needles.contains(where: { data.range(of: $0) != nil }) { continue }
                    // 直接从字节建的 NSString 找起来快；没有这个词的文件（绝大多数）到这儿就跳过，不再走 Swift 的慢路径
                    if needles.isEmpty, let ns = NSString(data: data, encoding: String.Encoding.utf8.rawValue),
                       ns.range(of: q, options: .caseInsensitive).location == NSNotFound { continue }
                    let text = String(decoding: data, as: UTF8.self)
                    let body = text.hasPrefix("---\n") ? String(text[(text.range(of: "\n---\n", range: text.index(text.startIndex, offsetBy: 4)..<text.endIndex)?.upperBound ?? text.startIndex)...]) : text
                    if let r = body.range(of: q, options: .caseInsensitive) {
                        let lineStart = body[..<r.lowerBound].lastIndex(of: "\n").map { body.index(after: $0) } ?? body.startIndex
                        fromNote = body[lineStart...].hasPrefix("[^note-")
                        snippet = cut(body, r)
                        break
                    }
                }
            }
            if titleHit || snippet != nil {
                out.append(Found(index: it.index, titleHit: titleHit, snippet: snippet, fromNote: fromNote, missing: missing && snippet == nil, date: it.date))
            }
        }
        // 标题里有的排前面，其余按日期新的在前
        return Array(out.sorted { ($0.titleHit ? 1 : 0, $0.date, $1.index) > ($1.titleHit ? 1 : 0, $1.date, $0.index) }.prefix(150))
    }

    /// 命中处前后各截一段，去掉 Markdown 符号
    private static func cut(_ text: String, _ r: Range<String.Index>) -> String {
        let lo = text.index(r.lowerBound, offsetBy: -30, limitedBy: text.startIndex) ?? text.startIndex
        let hi = text.index(r.upperBound, offsetBy: 50, limitedBy: text.endIndex) ?? text.endIndex
        var s = String(text[lo..<hi])
        s = s.replacingOccurrences(of: #"\[\^note-\d+\]:?"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)|[#*=>\[\]`_]"#, with: "", options: .regularExpression)
        s = s.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        return (lo > text.startIndex ? "…" : "") + s + (hi < text.endIndex ? "…" : "")
    }

    /// 截出来的一段里把搜的词标出来
    static func highlight(_ s: String, _ q: String, tint: Color) -> AttributedString {
        var a = AttributedString(s)
        var from = a.startIndex
        while let r = a[from...].range(of: q, options: .caseInsensitive) {
            a[r].backgroundColor = tint.opacity(0.18)
            a[r].foregroundColor = .primary
            from = r.upperBound
        }
        return a
    }
}

extension Vault {
    /// 检索用的全部条目和它们对应的目标（下标一一对应）
    func searchItems() -> (items: [LibrarySearch.Item], targets: [LibrarySearch.Target]) {
        // 书架没重建、剪藏的标题标签也没变，就用上次组好的，不用每搜一次把几千条重组一遍
        let metas = ClipProcessor.shared.clipMetas(self) + ClipProcessor.shared.clipMetas(self, folder: PathRules.paperFolder)
        let key = SearchIndexCache.Key(version: libraryVersion, clips: metas.hashValue)
        if let c = SearchIndexCache.last, c.key == key { return (c.items, c.targets) }
        var items: [LibrarySearch.Item] = [], targets: [LibrarySearch.Target] = []
        func add(_ t: LibrarySearch.Target, kind: Category, title: String, meta: String, date: String, fields: [String], paths: [String]) {
            items.append(.init(index: items.count, kind: kind, title: title, meta: meta, date: date, fields: fields, paths: paths))
            targets.append(t)
        }
        for m in metas {
            add(.doc(m.doc), kind: m.doc.kind, title: m.title, meta: [m.source, Dates.label(m.clipped.replacingOccurrences(of: "-", with: ""))].filter { !$0.isEmpty }.joined(separator: " · "),
                date: m.clipped.replacingOccurrences(of: "-", with: ""), fields: [m.tldr, m.tags.joined(separator: " "), m.source, m.note], paths: [m.path])
        }
        for e in library.allEpisodes {
            add(.episode(e), kind: e.kind, title: e.title, meta: [e.showName, Dates.label(e.date)].filter { !$0.isEmpty }.joined(separator: " · "),
                date: e.date, fields: [e.tldr, e.body, e.guests.joined(separator: " ")], paths: [e.notePath, e.articlePath].compactMap { $0 })
        }
        for d in library.notes {   // 随手记：标题、正文都搜
            add(.doc(d), kind: .note, title: d.title, meta: Dates.label(d.date), date: d.date, fields: [], paths: [d.path])
        }
        for d in library.reports {
            add(.doc(d), kind: .report, title: d.title, meta: [d.source, Dates.label(d.date)].filter { !$0.isEmpty }.joined(separator: " · "),
                date: d.date, fields: [d.tldr], paths: [d.path])
        }
        for s in library.shows {
            add(.show(s), kind: s.kind, title: s.name, meta: "\(s.kind.label) · \(s.countLabel)", date: s.latest, fields: [], paths: [])
        }
        SearchIndexCache.last = (key, items, targets)
        return (items, targets)
    }
}

/// 检索结果的一行
struct SearchHitRow: View {
    let hit: LibrarySearch.Hit
    @Environment(\.palette) private var p

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 7) {
                Circle().fill(hit.kind.color).frame(width: 8, height: 8).accessibilityHidden(true)
                Text(hit.kind.label + (hit.meta.isEmpty ? "" : " · \(hit.meta)")).lineLimit(1)
                Spacer()
                if hit.missing { Text("未下载") }
            }
            .font(.system(size: 12.5)).foregroundStyle(p.ink2)
            Text(Markdown.latinQuotes(hit.title)).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(2)
            if let s = hit.snippet {
                Text(s).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(2).lineLimit(3)
            }
        }
        .padding(.vertical, 2)
    }
}

@MainActor
private enum SearchIndexCache {
    struct Key: Equatable { let version: Int; let clips: Int }
    static var last: (key: Key, items: [LibrarySearch.Item], targets: [LibrarySearch.Target])?
}
