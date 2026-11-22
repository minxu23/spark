import Foundation
import SwiftUI
import UIKit

/// 剪藏：笔记库 Spark/剪藏/ 下一篇文章一个 .md（文件头里记来源、链接、标签、一句话），图片在 attachments/，
/// 另有一篇「索引.md」按标签归组。电脑端存进笔记库时也按同一套规则重写索引（core/clips.py）。
struct ClipMeta: Identifiable, Hashable {
    let path: String
    let title: String
    let source: String
    let url: String
    let published: String     // YYYY-MM-DD 或空
    let clipped: String       // YYYY-MM-DD
    let tags: [String]
    let tldr: String
    let note: String
    var starred = false
    var pinned = false
    /// 发表、收录的时刻：ISO 8601 带时差（文件头 published_at、clipped_at；老文件没有，是空串）
    var publishedAt = ""
    var clippedAt = ""
    var id: String { path }
    var doc: Doc {
        var d = Doc(path: path, kind: .clip, source: source.isEmpty ? "剪藏" : source, fullTitle: title, tldr: tldr)
        (d.day, d.instant) = Doc.clipDay(clipped: clipped, clippedAt: clippedAt)   // 跟书架上那份一样（Library.build）
        return d
    }
}

enum Clips {
    static let folder = Clipper.folder
    static let indexName = "索引.md"
    static var indexPath: String { "\(folder)/\(indexName)" }

    static func isClip(_ path: String) -> Bool {
        guard path.hasPrefix(folder + "/"), path.hasSuffix(".md") else { return false }
        let rest = path.dropFirst(folder.count + 1)
        return !rest.contains("/") && rest != indexName
    }

    // MARK: 文件头

    static func yaml(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: s, options: [.fragmentsAllowed, .withoutEscapingSlashes])) ?? Data("\"\"".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    static func frontmatter(_ text: String) -> [String: String] {
        guard text.hasPrefix("---\n"), let end = text.range(of: "\n---\n", range: text.index(text.startIndex, offsetBy: 4)..<text.endIndex)
        else { return [:] }
        var out: [String: String] = [:]
        for line in text[text.index(text.startIndex, offsetBy: 4)..<end.lowerBound].split(separator: "\n") {
            guard let c = line.firstIndex(of: ":") else { continue }
            let k = line[..<c].trimmingCharacters(in: .whitespaces)
            var v = line[line.index(after: c)...].trimmingCharacters(in: .whitespaces)
            if v.hasPrefix("\""), let data = v.data(using: .utf8),
               let s = try? JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed) as? String { v = s }
            out[k] = v
        }
        return out
    }

    static func tags(_ raw: String) -> [String] {
        if let data = raw.data(using: .utf8), let arr = try? JSONSerialization.jsonObject(with: data) as? [String] { return arr }
        return raw.trimmingCharacters(in: CharacterSet(charactersIn: "[] ")).split(separator: ",")
            .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " \"'")) }.filter { !$0.isEmpty }
    }

    /// 收录那天（YYYY-MM-DD）：以文件头的 clipped 为准；文件名前面的日期现在是发表那天，只在没有文件头的老文件里当收录日期用
    static func clippedDay(path: String, front f: [String: String]) -> String {
        let stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        let fallbackDate = stem.count >= 8 ? String(stem.prefix(8)) : ""
        return f["clipped"] ?? (fallbackDate.count == 8 ? "\(fallbackDate.prefix(4))-\(fallbackDate.dropFirst(4).prefix(2))-\(fallbackDate.suffix(2))" : "")
    }

    static func meta(path: String, text: String) -> ClipMeta {
        let f = frontmatter(text)
        let stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        let clipped = clippedDay(path: path, front: f)
        return ClipMeta(path: path, title: f["title"] ?? stem, source: f["source"] ?? "", url: f["url"] ?? "",
                        published: f["published"] ?? "", clipped: clipped, tags: tags(f["tags"] ?? ""),
                        tldr: f["tldr"] ?? "", note: f["note"] ?? "", starred: f["starred"] == "true", pinned: f["pinned"] == "true",
                        publishedAt: f["published_at"] ?? "", clippedAt: f["clipped_at"] ?? "")
    }

    // MARK: 写一篇

    /// day：发表那天（YYYY-MM-DD，本机时区）；不知道发表时间时用收录那天（nameDay）
    static func fileName(title: String, day: String) -> String {
        var t = title.replacingOccurrences(of: #"[\\/:*?"<>|\n\r\t#^\[\]]+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        // 按 Unicode 码位截（电脑 clip_compose.file_name 按码位，一样）
        if t.unicodeScalars.count > 60 { t = String(String.UnicodeScalarView(t.unicodeScalars.prefix(60))) }
        return "\(day.replacingOccurrences(of: "-", with: ""))_\(t.isEmpty ? "未命名" : t).md"
    }

    /// 文章的发表时间：publishedAt（ISO 带时差）优先，没有用 published（YYYY-MM-DD）
    static func published(_ a: Clipper.Article) -> DocTime.When? {
        DocTime.best(at: a.publishedAt ?? "", day: a.published)
    }

    /// 文件名前面的日期：发表那天，不知道就用收录那天
    static func nameDay(_ a: Clipper.Article, clipped: Date) -> String {
        DocTime.day(published(a) ?? .at(clipped))
    }

    struct Summary { var tldr: String; var tags: [String]; var body: String; var title = "" }

    /// clipped：收录的那一刻
    static func render(_ a: Clipper.Article, note: String, summary: Summary?, clipped: Date) -> String {
        let pub = published(a)
        var fm = ["---", "title: \(yaml(a.title))", "source: \(yaml(a.source))", "url: \(yaml(a.url))"]
        if let pub {
            fm.append("published: \(DocTime.day(pub))")
            if let t = DocTime.date(pub) { fm.append("published_at: \(yaml(DocTime.iso(t)))") }
        }
        fm.append("clipped: \(DocTime.day(.at(clipped)))")
        fm.append("clipped_at: \(yaml(DocTime.iso(clipped)))")
        if let s = summary {
            fm.append("tags: [" + s.tags.map(yaml).joined(separator: ", ") + "]")
            fm.append("tldr: \(yaml(s.tldr))")
        }
        if !note.isEmpty { fm.append("note: \(yaml(note))") }
        fm.append("---")
        var lines = fm + ["", "# \(a.title)", "", "- 来源：\(a.source)"]
        lines += DocTime.lines(published: pub, captured: .at(clipped))
        if !note.isEmpty { lines.append("- 备注：\(note.replacingOccurrences(of: "\n", with: " "))") }
        lines.append("")
        if let s = summary {
            lines += ["## 摘要", "", s.tldr, ""]   // 一句话放在最前面，不再加「一句话：」的标签
            if !s.body.isEmpty { lines += [s.body, ""] }
        }
        lines += ["## 正文", "", a.markdown.trimmingCharacters(in: .whitespacesAndNewlines), ""]
        if !a.url.isEmpty { lines += ["[阅读原文](\(a.url))", ""] }
        return lines.joined(separator: "\n")
    }

    // MARK: 摘要

    static let summaryPrompt = """
    你是资讯编辑。下面是我收藏的一篇文章的正文（网页抓取，可能夹杂少量噪音）。只根据正文归纳，不要补充正文里没有的信息。

    来源：{source}
    标题：{title}

    请用中文输出，严格按以下格式（不要输出多余的开场白或解释）：

    TLDR: <一句话核心结论，不超过40字，不要markdown>
    TAGS: <2 到 4 个主题标签，用中文逗号分隔，每个 2 到 8 个字，用具体的领域、技术或公司名（比如 具身智能、AI芯片、OpenAI），不要用「科技」「行业动态」这类泛词>
    - <要点1：背景/问题>
    - <要点2：核心方法或观点>
    - <要点3：关键数据、案例或产品名（如有，保留英文原名）>
    - <要点4，如有必要>
    - <要点5，如有必要>
    写 3-5 条要点，每条一句话即可，不用展开。
    {existing}
    正文：
    \"\"\"
    {text}
    \"\"\"

    """

    /// images：纯图片的文章（小绿书、小红书），摘要要看图写
    static func summarize(_ a: Clipper.Article, images: [Data] = [], existingTags: [String], config: LLM.Config) async throws -> Summary {
        var text = a.markdown.replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)"#, with: "", options: .regularExpression)
        text = text.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
        if text.count > 30000 { text = String(text.prefix(30000)) }
        let existing = existingTags.isEmpty ? "" : "\n已经用过的标签（合适就沿用同一个写法，不合适再起新的）：\(existingTags.prefix(40).joined(separator: "、"))\n"
        var prompt = summaryPrompt.replacingOccurrences(of: "{source}", with: a.source)
            .replacingOccurrences(of: "{title}", with: a.title).replacingOccurrences(of: "{existing}", with: existing)
            .replacingOccurrences(of: "{text}", with: text)
        if a.titleGuessed == true { prompt = "这篇没有标题。第一行先写：TITLE: <给它起的标题，不超过 24 字，说清内容，不要写作者名>\n\n" + prompt }
        if !images.isEmpty { prompt = "这篇文章的内容主要在图片里，附上了图片，请看图归纳。\n\n" + prompt }
        let raw = try await LLM.complete(prompt, images: images, config: config, maxTokens: 1500, timeout: 180)
        var tldr = "", tags: [String] = [], body: [String] = [], title = ""
        for line in raw.split(separator: "\n").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            if let r = line.range(of: #"^\**TITLE\**\s*[:：]\s*"#, options: .regularExpression) {
                title = String(line[r.upperBound...]).trimmingCharacters(in: CharacterSet(charactersIn: " *《》\"「」"))
                continue
            }
            if let r = line.range(of: #"^\**TLDR\**\s*[:：]\s*"#, options: .regularExpression) { tldr = String(line[r.upperBound...]); continue }
            if let r = line.range(of: #"^\**TAGS\**\s*[:：]\s*"#, options: .regularExpression) {
                tags = line[r.upperBound...].split(whereSeparator: { "，,、;；".contains($0) })
                    .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " #`\"")) }.filter { !$0.isEmpty && $0.count <= 12 }
                continue
            }
            if line.hasPrefix("- ") || line.hasPrefix("* ") { body.append("- " + line.dropFirst(2)) }
        }
        // 模型没按格式写「TLDR:」时，用第一条要点顶上，别让整篇没有摘要
        if tldr.isEmpty, let first = body.first { tldr = String(first.dropFirst(2).prefix(40)) }
        guard !tldr.isEmpty else { throw SparkError("摘要格式不对：\(raw.prefix(60))") }
        return Summary(tldr: tldr, tags: Array(tags.prefix(4)), body: body.joined(separator: "\n"), title: title)
    }

    // MARK: 索引

    /// 按标签归组：两篇以上用到的标签成一组（一篇可能在几组里），剩下的放「其他」；每组里新存的在前
    static func renderIndex(_ clips: [ClipMeta]) -> String {
        let sorted = clips.sorted { ClipSort.clippedKey($0) > ClipSort.clippedKey($1) }
        var count: [String: Int] = [:]
        for c in sorted { for t in Set(c.tags) { count[t, default: 0] += 1 } }
        let groups = count.filter { $0.value >= 2 }.sorted { ($0.value, $1.key) > ($1.value, $0.key) }.map(\.key)
        var lines = ["# 剪藏索引", "", "共 \(clips.count) 篇。按主题归组，一篇可能出现在几个主题下；每组里新存的在前。", ""]
        func row(_ c: ClipMeta) -> String {
            let name = (c.path as NSString).lastPathComponent
            let src = [c.source, c.clipped.isEmpty ? "" : "\(c.clipped) 存"].filter { !$0.isEmpty }.joined(separator: "，")
            return "- [\(c.title.replacingOccurrences(of: "]", with: "］"))](<\(name)>)（\(src)）" + (c.tldr.isEmpty ? "" : "：\(c.tldr)")
        }
        for g in groups {
            let members = sorted.filter { $0.tags.contains(g) }
            lines += ["## \(g)（\(members.count)）", ""] + members.map(row) + [""]
        }
        let rest = sorted.filter { !$0.tags.contains(where: groups.contains) }
        if !rest.isEmpty {
            lines += ["## \(groups.isEmpty ? "全部" : "其他")（\(rest.count)）", ""] + rest.map(row) + [""]
        }
        return lines.joined(separator: "\n")
    }
}

/// 把收件箱里的剪藏收进笔记库：抓正文（扩展里没抓到的）、下完图片、写摘要（填了模型 Key 时）、更新索引、交给电脑
@MainActor
@Observable
final class ClipProcessor {
    static let shared = ClipProcessor()
    private(set) var running = false
    private(set) var status: String?

    var pending: Int { ClipInbox.entries().count }

    func run(vault: Vault) async {
        guard !running else { return }
        let entries = ClipInbox.entries()
        guard !entries.isEmpty else { return }
        running = true
        defer { running = false }
        var saved: [String] = [], failed: [String] = [], notes: [String] = []
        let config = LLM.Config.load()
        for (i, entry) in entries.enumerated() {
            var e = entry
            status = "正在收进剪藏 \(i + 1)/\(entries.count)"
            await ClipInbox.capture(&e, deadline: .distantFuture)
            guard var a = e.article, let dir = ClipInbox.folder(e.id) else {
                failed.append("\(e.url)：\(e.error ?? "没抓到正文")")
                // 抓了三天都不行的就不留了；确定打不开的（笔记删了、链接失效）这次报过错就不再重试
                if e.gone == true || e.created < Date().addingTimeInterval(-3 * 86400) { ClipInbox.remove(e.id) }
                continue
            }
            // 抓下来才知道是哪一篇（小红书每次分享的短链不一样，抓完才有笔记编号）：已经存过的不再存
            // 提示里写明是剪藏里的哪一篇，在「书架 → 剪藏」列表顶上的状态行里看得到
            if let old = clipMetas(vault).first(where: { ClipLink.same($0.url, a.url) }) {
                notes.append("《\(a.title)》跟剪藏里的《\(old.title)》是同一篇，没有再存")
                ClipInbox.remove(e.id)
                continue
            }
            do {
                let title = try await store(&a, note: e.note, created: e.created, dir: dir, vault: vault, config: config, notes: &notes)
                saved.append(title)
            } catch {
                failed.append("《\(a.title)》：\(error.localizedDescription)")
                continue
            }
            ClipInbox.remove(e.id)
        }
        if !saved.isEmpty { try? vault.addGenerated(path: Clips.indexPath, text: Clips.renderIndex(clipMetas(vault))) }
        var parts: [String] = []
        if !saved.isEmpty {
            parts.append("存进剪藏 \(saved.count) 篇" + (config == nil ? "（没填模型 Key，没写摘要）" : ""))
        }
        if !failed.isEmpty { parts.append("没存成 \(failed.count) 篇：" + failed.joined(separator: "；")) }
        status = (parts + notes).joined(separator: "。")
    }

    /// 一篇文章存进剪藏：写摘要、翻译（填了模型时）、写文件、交给电脑。返回存下的标题
    private func store(_ a: inout Clipper.Article, note: String, created: Date, dir: URL?, vault: Vault,
                       config: LLM.Config?, notes: inout [String]) async throws -> String {
        do {
            var summary: Clips.Summary?
            if let config {
                status = "正在写摘要：\(a.title.prefix(20))"
                let used = Array(Set(clipMetas(vault).flatMap(\.tags))).sorted()
                let shots = a.imagesOnly == true && dir != nil ? Self.shots(a, dir: dir!) : []
                var why = ""
                for _ in 0..<2 where summary == nil {     // 模型偶尔不按格式写或者超时，再试一次
                    do { summary = try await Clips.summarize(a, images: shots, existingTags: used, config: config) }
                    catch { why = error.localizedDescription }
                }
                if summary == nil { notes.append("《\(a.title)》的摘要没写成（\(why)），正文已经存了，打开文章点「更多」里的「重写摘要」再试") }
                if let t = summary?.title, !t.isEmpty, a.titleGuessed == true { a.title = t }
                // 全英文的文章逐段译成中文，阅读时能切换中文、英文、对照
                if ClipTranslate.needed(a.markdown) {
                    let name = String(a.title.prefix(20))
                    do {
                        let r = try await ClipTranslate.run(a.markdown, config: config) { n, total in
                            Task { @MainActor in ClipProcessor.shared.setStatus("正在翻译：\(name)（\(n)/\(total)）") }
                        }
                        a.markdown = r.text
                        if r.missed > 0 { notes.append("《\(a.title)》有 \(r.missed) 段没译成，打开文章点「更多」里的「翻译成中文」补上") }
                    } catch {
                        notes.append("《\(a.title)》没翻译：\(error.localizedDescription)")
                    }
                }
            }
            // 文件名用发表那天（不知道发表时间用收录那天），文件头记收录的那一刻
            var name = Clips.fileName(title: a.title, day: Clips.nameDay(a, clipped: created))
            for n in 2..<100 where vault.exists("\(Clips.folder)/\(name)") {
                name = (name as NSString).deletingPathExtension.replacingOccurrences(of: #"_\d+$"#, with: "", options: .regularExpression) + "_\(n).md"
            }
            let path = "\(Clips.folder)/\(name)"
            let text = Clips.render(a, note: note, summary: summary, clipped: created)
            var images: [String: String] = [:]
            do {
                for (local, remote) in a.images {
                    images["\(Clips.folder)/\(local)"] = remote
                    if let dir, let data = try? Data(contentsOf: dir.appending(path: local)) {
                        try vault.addGenerated(path: "\(Clips.folder)/\(local)", data: data, rebuild: false)
                    }
                }
                try vault.addGenerated(path: path, text: text)
            }
            if let msg = await deliver(path: path, content: text, images: images, title: a.title, vault: vault) { notes.append(msg) }
        }
        return a.title
    }

    /// 手写的一段文字存进剪藏：没有原文链接，来源记「手写」（或填的出处）
    func saveText(title: String, text: String, source: String, vault: Vault) async -> Bool {
        guard !running else { status = "正在收别的剪藏，等一下再存"; return false }
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return false }
        running = true
        defer { running = false }
        var t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let guessed = t.isEmpty
        if guessed { t = String(body.split(separator: "\n").first.map(String.init)?.prefix(30) ?? "未命名") }
        let src = source.trimmingCharacters(in: .whitespacesAndNewlines)
        var a = Clipper.Article(url: "", title: t, source: src.isEmpty ? "手写" : src, published: "", markdown: body, images: [:],
                                titleGuessed: guessed ? true : nil)
        var notes: [String] = []
        let config = LLM.Config.load()
        status = config == nil ? "正在存" : "正在写摘要：\(t.prefix(20))"
        do {
            let saved = try await store(&a, note: "", created: Date(), dir: nil, vault: vault, config: config, notes: &notes)
            try? vault.addGenerated(path: Clips.indexPath, text: Clips.renderIndex(clipMetas(vault)))
            status = (["存进剪藏：《\(saved)》" + (config == nil ? "（没填模型 Key，没写摘要）" : "")] + notes).joined(separator: "。")
            return true
        } catch {
            status = "没存成：\(error.localizedDescription)"
            return false
        }
    }

    /// 纯图片的文章给模型看的图：最多 6 张，缩到长边 1280，太大了模型接口不收
    private static func shots(_ a: Clipper.Article, dir: URL) -> [Data] {
        a.images.keys.sorted().prefix(6).compactMap { name in
            guard let img = UIImage(contentsOfFile: dir.appending(path: name).path) else { return nil }
            let scale = min(1, 1280 / max(img.size.width, img.size.height))
            let size = CGSize(width: img.size.width * scale, height: img.size.height * scale)
            return UIGraphicsImageRenderer(size: size).jpegData(withCompressionQuality: 0.7) { _ in img.draw(in: CGRect(origin: .zero, size: size)) }
        }
    }

    func setStatus(_ s: String?) { status = s }

    func clipMetas(_ vault: Vault) -> [ClipMeta] {
        vault.library.clips.compactMap { ClipCache.note($0.path, root: Vault.root)?.meta }
    }

    /// 交给电脑存进笔记库（图片电脑按原地址自己下）。连不上 GitHub 或太长的放发件箱，接数据线时交
    private func deliver(path: String, content: String, images: [String: String], title: String, vault: Vault) async -> String? {
        let task: [String: Any] = ["type": "save", "path": path, "content": content, "images": images, "replace": true]
        do {
            let route = try await vault.submitTask((title: "存进笔记库：\(title)", task: task))
            return vault.api != nil && route.fellBack ? "《\(title)》GitHub 交不过去，已放进发件箱，下次接数据线同步时存进电脑" : nil
        } catch {
            return "《\(title)》手机上已经存好；交给电脑时出错：\(error.localizedDescription)"
        }
    }
}

enum ClipSort: String, CaseIterable, Identifiable {
    case clipped, published, name
    var id: String { rawValue }
    var label: String {
        switch self {
        case .clipped: "按收录时间"
        case .published: "按发表时间"
        case .name: "按文章名称"
        }
    }

    /// 「按收录时间」排（倒序用）：收录那一刻，只知道哪天的排在同一天有时刻的后面，再按路径。
    /// 跟电脑 core/clips.order_clipped、Mac 应用 ClipSort 一样
    static func clippedKey(_ m: ClipMeta) -> String {
        DocTime.sortKey(at: m.clippedAt, day: m.clipped) + "|" + (m.path as NSString).lastPathComponent
    }

    /// 「按发表时间」排（倒序用）：发表那一刻；只知道哪天的排在同一天有时刻的后面；一样的再按收录时间、路径。
    /// 不知道发表时间的按收录时间算
    static func publishedKey(_ m: ClipMeta) -> String {
        let first = m.published.isEmpty && m.publishedAt.isEmpty
            ? DocTime.sortKey(at: m.clippedAt, day: m.clipped) : DocTime.sortKey(at: m.publishedAt, day: m.published)
        return first + "|" + clippedKey(m)
    }
}

/// 综合小结里加入剪藏以外的文章：节目的小结、信息跟进、报告
struct OtherArticlesPicker: View {
    @Binding var picked: [String]
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var rows: [(path: String, title: String, meta: String)] {
        let q = query.lowercased()
        let eps = vault.library.allEpisodes.filter { q.isEmpty || $0.title.lowercased().contains(q) || $0.showName.lowercased().contains(q) }
            .sorted { $0.date > $1.date }.compactMap { e -> (String, String, String)? in
                (e.notePath ?? e.articlePath ?? e.speechPath ?? e.transcriptPath).map { ($0, e.title, "\(e.showName)　\(Dates.label(e.date))") }
            }
        let docs = vault.library.reports.filter { q.isEmpty || $0.title.lowercased().contains(q) }.map { ($0.path, $0.title, $0.source) }
        return Array((eps + docs).prefix(120))
    }

    var body: some View {
        NavigationStack {
            List(rows, id: \.path) { r in
                Button {
                    if let i = picked.firstIndex(of: r.path) { picked.remove(at: i) }
                    else if picked.count < ReviewRunner.maxPicked { picked.append(r.path) }
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: picked.contains(r.path) ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(picked.contains(r.path) ? p.accent : p.ink2)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(Markdown.latinQuotes(r.title)).font(.system(size: 15)).foregroundStyle(p.ink).lineLimit(2)
                            Text(r.meta).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        }
                    }
                }
                .listRowBackground(Color.clear)
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "搜标题或节目")
            .navigationTitle("加入其他文章").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }
}

/// 书架上的「剪藏」：粘贴链接存一篇、正在收进来的进度、按标签筛、列表
struct ClipShelf: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadState.self) private var readState
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var processor = ClipProcessor.shared
    @State private var tag: String?
    @State private var metas: [ClipMeta] = []
    @State private var adding = false
    /// 挑几篇写综合小结：挑的时候点一篇是勾选，不是打开
    @State private var picking = false
    @State private var picked: [String] = []
    @State private var pickNote: String?
    @State private var addingOthers = false
    @State private var deleting: Doc?
    @State private var writing = false
    @State private var moving: ClipMeta?
    /// 书架右上角菜单点了「挑几篇写综合小结」
    var pickRequest: Binding<Bool> = .constant(false)
    /// 书架的「星标」分类：只列加了星标的，标签和篇数也只算这些
    var onlyStarred = false
    /// 星标里还有剪藏以外的（列在上面）：没有星标剪藏时不再提示「还没有星标」
    var othersStarred = false
    private var pool: [ClipMeta] { onlyStarred ? metas.filter(\.starred) : metas }
    @AppStorage("clips.sort") private var sort = ClipSort.clipped.rawValue

    /// 置顶的排在最前，其余按选的排序
    private var sorted: [ClipMeta] {
        let all = sortedByChoice
        return all.filter(\.pinned) + all.filter { !$0.pinned }
    }

    private var sortedByChoice: [ClipMeta] {
        let name = { (m: ClipMeta) in m.title.lowercased() }
        switch ClipSort(rawValue: sort) ?? .clipped {
        case .clipped: return metas.sorted { ClipSort.clippedKey($0) > ClipSort.clippedKey($1) }
        case .published: return metas.sorted { ClipSort.publishedKey($0) > ClipSort.publishedKey($1) }
        case .name: return metas.sorted { NameOrder.less(name($0), name($1)) }
        }
    }

    private var tags: [(String, Int)] {
        var c: [String: Int] = [:]
        for m in pool { for t in Set(m.tags) { c[t, default: 0] += 1 } }
        return c.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
    }

    var body: some View {
        Group {
            // 粘贴、手写、综合小结都在书架右上角的 + 里；这里只在收东西时显示进度
            if processor.running {
                HStack(spacing: 8) { ProgressView(); Text("正在收进剪藏").font(.system(size: 13)).foregroundStyle(p.ink2) }
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if picking { pickBar }
            if let pickNote {
                Text(pickNote).font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if let s = processor.status {
                Text(s).font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if metas.isEmpty {
                Text("在微信、Safari 里看到想留的文章：分享 → Spark → 「存到剪藏」。也可以复制链接后点右上角的 +。")
                    .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            } else if pool.isEmpty {
                if !othersStarred {
                    Text("还没有星标。读文章、单集时点底下的星，或者在剪藏列表里把一篇往右滑，就会收到这里。")
                        .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                        .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
            } else {
                // 标签和排序放一行：排序只是个小图标，不单独占一行
                HStack(spacing: 8) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            chip(nil, "全部 \(pool.count)")
                            ForEach(tags, id: \.0) { t, n in chip(t, "\(t) \(n)") }
                        }
                    }
                    .contentMargins(.horizontal, 0, for: .scrollContent)   // 外面那栏的边距不要再叠一层
                    SortMenu(options: ClipSort.allCases.map { ($0.rawValue, $0.label) }, selection: $sort)
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .deleteReviewDialog($deleting) { pickNote = $0 }   // 挂在一行上：挂在整组上会每行各弹一次
                ForEach(sorted.filter { (tag == nil || $0.tags.contains(tag!)) && (!onlyStarred || $0.starred) }) { m in
                    if picking {
                        Button { toggle(m.path) } label: {
                            HStack(alignment: .top, spacing: 12) {
                                Image(systemName: picked.contains(m.path) ? "checkmark.circle.fill" : "circle")
                                    .font(.system(size: 21)).foregroundStyle(picked.contains(m.path) ? p.accent : p.ink2)
                                    .padding(.top, 2)
                                clipRow(m)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(picked.contains(m.path) ? .isSelected : [])
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    } else {
                        PlainLink(value: m.doc) { clipRow(m) }
                            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                            // 跟「备忘录」一样：右滑置顶、星标；左滑删除、移到主题、分享
                            .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                Button { Task { await flip(m, "pinned") } } label: {
                                    Label(m.pinned ? "取消置顶" : "置顶", systemImage: m.pinned ? "pin.slash.fill" : "pin.fill")
                                }.tint(.orange)
                                Button { Task { await flip(m, "starred") } } label: {
                                    Label(m.starred ? "取消星标" : "星标", systemImage: m.starred ? "star.slash.fill" : "star.fill")
                                }.tint(.yellow)
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                Button(role: .destructive) { deleting = m.doc } label: { Label("删除", systemImage: "trash.fill") }
                                Button { moving = m } label: { Label("移到", systemImage: "folder.fill") }.tint(.purple)
                                share(m) { Label("分享", systemImage: "square.and.arrow.up.fill") }.tint(.blue)
                            }
                            .contextMenu {
                                Button(m.pinned ? "取消置顶" : "置顶", systemImage: m.pinned ? "pin.slash" : "pin") { Task { await flip(m, "pinned") } }
                                Button(m.starred ? "取消星标" : "加星标", systemImage: m.starred ? "star.slash" : "star") { Task { await flip(m, "starred") } }
                                Button("移到主题…", systemImage: "folder") { moving = m }
                                share(m) { Label("分享", systemImage: "square.and.arrow.up") }
                                Button("删除", systemImage: "trash", role: .destructive) { deleting = m.doc }
                            }
                    }
                }
            }
        }
        .task(id: vault.library.clips.count) { metas = processor.clipMetas(vault) }
        .onChange(of: pickRequest.wrappedValue) { _, want in
            guard want else { return }
            picking = true; picked = []; pickNote = nil
            pickRequest.wrappedValue = false
        }
        .sheet(isPresented: $addingOthers) { OtherArticlesPicker(picked: $picked) }
        .sheet(item: $moving) { m in
            MoveToTopicSheet(meta: m, allTags: tags.map(\.0)) { new in Task { await retag(m, new) } }
                .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $writing) {
            TextClipSheet { title, text, source in
                Task {
                    if await processor.saveText(title: title, text: text, source: source, vault: vault) { metas = processor.clipMetas(vault) }
                }
            }
        }
        .task { await processor.run(vault: vault) }
    }

    private func clipRow(_ m: ClipMeta) -> some View {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(m.source.isEmpty ? "剪藏" : m.source).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                                if m.pinned {
                                    Image(systemName: "pin.fill").font(.system(size: 11)).foregroundStyle(.orange).accessibilityLabel("置顶")
                                }
                                if m.starred {
                                    Image(systemName: "star.fill").font(.system(size: 11)).foregroundStyle(.orange).accessibilityLabel("星标")
                                }
                                Spacer()
                                Text(sort == ClipSort.published.rawValue && !m.published.isEmpty
                                     ? "发表于 \(Dates.label(m.published.replacingOccurrences(of: "-", with: "")))"
                                     : Dates.label(m.clipped.replacingOccurrences(of: "-", with: ""))).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                            }
                            Text(Markdown.latinQuotes(m.title)).font(.heading(16.5)).foregroundStyle(p.ink).lineSpacing(3)
                            if !m.tldr.isEmpty {
                                Text(m.tldr).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineLimit(2)
                            }
                        }
                        .padding(.vertical, 4)
    }

    // MARK: 综合小结

    private var pickBar: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(picked.isEmpty ? "点文章勾选，挑 2 到 \(ReviewRunner.maxPicked) 篇放在一起写一份小结。可以先用上面的标签筛。"
                                : "已选 \(picked.count) 篇" + (picked.count < 2 ? "，再挑至少 1 篇" : ""))
                .font(.system(size: 13.5)).foregroundStyle(p.ink2)
            Button("加入节目、信息跟进、报告（已选 \(picked.filter { !Clips.isClip($0) }.count) 篇）") { addingOthers = true }
                .font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
            HStack(spacing: 10) {
                Button { Task { await summarize() } } label: {
                    Text(jobs.canRun ? "在手机上写" : (vault.api == nil ? "放进发件箱" : "交给电脑写"))
                        .font(.system(size: 15, weight: .semibold)).frame(maxWidth: .infinity, minHeight: 44)
                        .foregroundStyle(p.paper).background(p.accent, in: RoundedRectangle(cornerRadius: 10))
                }
                .buttonStyle(.plain)
                .disabled(picked.count < 2)
                .opacity(picked.count < 2 ? 0.4 : 1)
                Button { picking = false; picked = [] } label: {
                    Text("取消").font(.system(size: 15)).frame(minWidth: 72, minHeight: 44)
                        .foregroundStyle(p.accent).overlay(RoundedRectangle(cornerRadius: 10).stroke(p.accent))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(14)
        .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
    }

    private func toggle(_ path: String) {
        if let i = picked.firstIndex(of: path) {
            picked.remove(at: i)
        } else if picked.count < ReviewRunner.maxPicked {
            picked.append(path)
        } else {
            pickNote = "一次最多 \(ReviewRunner.maxPicked) 篇。"
        }
    }

    /// 手机上填了模型就在手机上写（排进「在手机上生成」的队列），不然交给电脑
    private func summarize() async {
        let title = "综合小结：\(picked.count) 篇"
        if jobs.canRun {
            pickNote = (jobs.busy ? "手机上正在生成别的，这份排在后面。" : "开始写了。") + "进度和写好的小结在「复盘」页。"
            jobs.picked(paths: picked, title: title, vault: vault)
        } else {
            do {
                let route = try await vault.submitTask(SparkTask.picked(picked, title: title))
                pickNote = vault.routeNote(route) + "写好后在「复盘」页。"
            } catch {
                pickNote = "没交出去：\(error.localizedDescription)"
                return
            }
        }
        picking = false
        picked = []
    }

    /// 置顶、星标：改文件头里的开关，存好交给电脑
    private func flip(_ m: ClipMeta, _ key: String) async {
        let on = key == "pinned" ? !m.pinned : !m.starred
        await edit(m) { DocEdit.flagged($0, key, on) }
    }

    /// 移到别的主题：换标签，索引跟着重写
    private func retag(_ m: ClipMeta, _ tags: [String]) async {
        await edit(m) { DocEdit.retagged($0, tags) }
        try? vault.addGenerated(path: Clips.indexPath, text: Clips.renderIndex(metas))
    }

    private func edit(_ m: ClipMeta, _ change: (String) -> String) async {
        guard let text = vault.localText(m.path) else { return }
        do {
            try await vault.saveEdited(path: m.path, text: change(text))
            metas = processor.clipMetas(vault)
        } catch {
            pickNote = "没存上：\(error.localizedDescription)"
        }
    }

    /// 分享：有原文链接分享链接，手写的分享正文
    @ViewBuilder
    private func share<L: View>(_ m: ClipMeta, @ViewBuilder label: () -> L) -> some View {
        if let u = URL(string: m.url), u.scheme?.hasPrefix("http") == true {
            ShareLink(item: u, subject: Text(m.title), message: Text(m.tldr), label: label)
        } else {
            let body = vault.localText(m.path).flatMap { RX.first(#"(?s)\n## 正文\n+(.*)"#, $0) } ?? m.tldr
            ShareLink(item: "\(m.title)\n\n\(body.trimmingCharacters(in: .whitespacesAndNewlines))", subject: Text(m.title), label: label)
        }
    }

    private func chip(_ t: String?, _ label: String) -> some View {
        let on = tag == t
        return Button { tag = t } label: {
            Text(label).font(.system(size: 13.5)).padding(.horizontal, 12).frame(minHeight: 34)
                .foregroundStyle(on ? p.paper : p.ink)
                .background(on ? p.accent : .clear, in: Capsule())
                .overlay(Capsule().stroke(on ? p.accent : p.rule))
        }
        .buttonStyle(.plain)
    }

    private func clip(_ text: String) async {
        guard let url = SparkTask.firstURL(in: text) else {
            processor.setStatus("剪贴板里没有链接。先在微信或浏览器里复制文章链接。")
            return
        }
        adding = true
        defer { adding = false }
        do {
            _ = try ClipInbox.add(url: url, note: "")
            await processor.run(vault: vault)
        } catch {
            processor.setStatus(error.localizedDescription)
        }
    }
}

/// 手写一段存进剪藏：标题可空（空着用第一行），出处可空（空着记「手写」）
struct TextClipSheet: View {
    let onSave: (_ title: String, _ text: String, _ source: String) -> Void
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var text = ""
    @State private var source = ""
    @FocusState private var focus: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("标题（可不填，用第一行）", text: $title)
                    TextField("出处（可不填，比如书名、谁说的）", text: $source)
                }
                Section {
                    TextField("想存下来的一段文字", text: $text, axis: .vertical)
                        .lineLimit(8...30).focused($focus)
                } footer: {
                    Text("存成一篇剪藏，跟别的剪藏一样能划重点、批注、加星标。填了模型 Key 会写摘要、打标签。")
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("手写一段").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("存进剪藏") { onSave(title, text, source); dismiss() }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .onAppear { focus = true }
    }
}

/// 移到主题：勾选这篇归在哪几个主题下（就是标签），也可以新建一个
struct MoveToTopicSheet: View {
    let meta: ClipMeta
    let allTags: [String]
    let onSave: ([String]) -> Void
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var picked: [String] = []
    @State private var newTag = ""

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack {
                        TextField("新主题", text: $newTag).submitLabel(.done).onSubmit(addNew)
                        Button("添加", action: addNew).disabled(newTag.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } footer: {
                    Text("一篇可以归在几个主题下。剪藏列表上方按主题筛，索引也按主题归组。")
                }
                Section("主题") {
                    ForEach(Array(Set(allTags + picked)).sorted { a, b in
                        (allTags.firstIndex(of: a) ?? Int.max, a) < (allTags.firstIndex(of: b) ?? Int.max, b)
                    }, id: \.self) { t in
                        Button {
                            if let i = picked.firstIndex(of: t) { picked.remove(at: i) } else { picked.append(t) }
                        } label: {
                            HStack {
                                Text(t).foregroundStyle(p.ink)
                                Spacer()
                                if picked.contains(t) { Image(systemName: "checkmark").foregroundStyle(p.accent) }
                            }
                            .contentShape(Rectangle())
                        }
                        .accessibilityAddTraits(picked.contains(t) ? .isSelected : [])
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("移到主题").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { onSave(picked); dismiss() }.disabled(picked == meta.tags)
                }
            }
        }
        .onAppear { picked = meta.tags }
    }

    private func addNew() {
        let t = newTag.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        if !picked.contains(t) { picked.append(t) }
        newTag = ""
    }
}

/// 信息跟进里的一条右滑「存进剪藏」：按原文链接重新抓全文和图片，跟分享进来的剪藏走同一套
struct SaveToClips: ViewModifier {
    let episode: Episode
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var message: String?

    /// 只有信息跟进的文章（网页、公众号）能存；播客、视频不是文章
    private var url: String? {
        guard episode.kind == .track, let u = URL(string: episode.url), ["http", "https"].contains(u.scheme ?? "") else { return nil }
        return episode.url
    }

    func body(content: Content) -> some View {
        if let url {
            content
                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                    Button { save(url) } label: { Label("存进剪藏", systemImage: "bookmark.fill") }.tint(p.accent)
                }
                .contextMenu {
                    Button("存进剪藏", systemImage: "bookmark") { save(url) }
                }
                .alert(message ?? "", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
                    Button("好", role: .cancel) {}
                }
        } else {
            content
        }
    }

    private func save(_ url: String) {
        let processor = ClipProcessor.shared
        if processor.clipMetas(vault).contains(where: { ClipLink.same($0.url, url) }) {
            message = "这篇已经在剪藏里了"
            return
        }
        do {
            _ = try ClipInbox.add(url: url, note: "")
            message = "已加入剪藏，正在抓全文和图片。进度在「剪藏」里。"
            Task { await processor.run(vault: vault) }
        } catch {
            message = "没存上：\(error.localizedDescription)"
        }
    }
}

extension View {
    func saveToClips(_ e: Episode) -> some View { modifier(SaveToClips(episode: e)) }
}

/// 「最新」右上角的 +：粘贴链接存进剪藏、手写一段、处理链接、检查订阅
struct AddMenu: View {
    /// 书架上多一项「挑几篇写综合小结」
    var onSummary: (() -> Void)? = nil
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var writing = false
    @State private var newTask: Int?
    @State private var message: String?

    var body: some View {
        Menu {
            Button("粘贴链接，存进剪藏", systemImage: "doc.on.clipboard") { pasteLink() }
            Button("手写一段，存进剪藏", systemImage: "square.and.pencil") { writing = true }
            if let onSummary {
                Button("挑几篇写综合小结", systemImage: "checklist.checked") { onSummary() }
            }
            Divider()
            Button("处理视频或播客…", systemImage: "link") { newTask = 0 }
            Button("检查订阅更新…", systemImage: "arrow.clockwise") { newTask = 1 }
        } label: {
            Image(systemName: "plus").font(.system(size: 17, weight: .semibold))
        }
        .accessibilityLabel("添加")
        .sheet(isPresented: $writing) {
            TextClipSheet { title, text, source in
                Task {
                    let ok = await ClipProcessor.shared.saveText(title: title, text: text, source: source, vault: vault)
                    message = ok ? "存进剪藏了，在「书架 → 剪藏」里。" : (ClipProcessor.shared.status ?? "没存成")
                }
            }
        }
        .sheet(item: Binding(get: { newTask.map(TaskKind.init) }, set: { newTask = $0?.id })) { k in
            NewTaskView(initialKind: k.id)
        }
        .alert(message ?? "", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("好", role: .cancel) {}
        }
    }

    private struct TaskKind: Identifiable { let id: Int }

    private func pasteLink() {
        guard let url = SparkTask.firstURL(in: UIPasteboard.general.string ?? "") else {
            message = "剪贴板里没有链接。先在微信或浏览器里复制文章链接。"
            return
        }
        if ClipProcessor.shared.clipMetas(vault).contains(where: { ClipLink.same($0.url, url) }) {
            message = "这篇已经在剪藏里了"
            return
        }
        do {
            _ = try ClipInbox.add(url: url, note: "")
            message = "已加入剪藏，正在抓全文和图片。进度在「书架 → 剪藏」里。"
            Task { await ClipProcessor.shared.run(vault: vault) }
        } catch {
            message = "没存上：\(error.localizedDescription)"
        }
    }
}
