import Foundation
import SwiftUI

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
    var id: String { path }
    var doc: Doc { Doc(path: path, kind: .clip, source: source.isEmpty ? "剪藏" : source, fullTitle: title, tldr: tldr) }
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

    static func meta(path: String, text: String) -> ClipMeta {
        let f = frontmatter(text)
        let stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        let fallbackDate = stem.count >= 8 ? String(stem.prefix(8)) : ""
        let clipped = f["clipped"] ?? (fallbackDate.count == 8 ? "\(fallbackDate.prefix(4))-\(fallbackDate.dropFirst(4).prefix(2))-\(fallbackDate.suffix(2))" : "")
        return ClipMeta(path: path, title: f["title"] ?? stem, source: f["source"] ?? "", url: f["url"] ?? "",
                        published: f["published"] ?? "", clipped: clipped, tags: tags(f["tags"] ?? ""),
                        tldr: f["tldr"] ?? "", note: f["note"] ?? "")
    }

    // MARK: 写一篇

    static func fileName(title: String, clipped: Date) -> String {
        var t = title.replacingOccurrences(of: #"[\\/:*?"<>|\n\r\t#^\[\]]+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        if t.count > 60 { t = String(t.prefix(60)) }
        return "\(Clipper.day.string(from: clipped).replacingOccurrences(of: "-", with: ""))_\(t.isEmpty ? "未命名" : t).md"
    }

    struct Summary { var tldr: String; var tags: [String]; var body: String }

    static func render(_ a: Clipper.Article, note: String, summary: Summary?, clipped: Date) -> String {
        let day = Clipper.day.string(from: clipped)
        var fm = ["---", "title: \(yaml(a.title))", "source: \(yaml(a.source))", "url: \(yaml(a.url))"]
        if !a.published.isEmpty { fm.append("published: \(a.published)") }
        fm.append("clipped: \(day)")
        if let s = summary {
            fm.append("tags: [" + s.tags.map(yaml).joined(separator: ", ") + "]")
            fm.append("tldr: \(yaml(s.tldr))")
        }
        if !note.isEmpty { fm.append("note: \(yaml(note))") }
        fm.append("---")
        var lines = fm + ["", "# \(a.title)", "", "- 来源：\(a.source)"]
        if !a.published.isEmpty { lines.append("- 发布：\(a.published)") }
        lines.append("- 剪藏：\(day)")
        if !note.isEmpty { lines.append("- 备注：\(note.replacingOccurrences(of: "\n", with: " "))") }
        lines.append("")
        if let s = summary {
            lines += ["## 摘要", "", "**一句话**：\(s.tldr)", ""]
            if !s.body.isEmpty { lines += [s.body, ""] }
        }
        lines += ["## 正文", "", a.markdown.trimmingCharacters(in: .whitespacesAndNewlines), "", "[阅读原文](\(a.url))", ""]
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

    static func summarize(_ a: Clipper.Article, existingTags: [String], config: LLM.Config) async throws -> Summary {
        var text = a.markdown.replacingOccurrences(of: #"!\[[^\]]*\]\([^)]*\)"#, with: "", options: .regularExpression)
        text = text.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
        if text.count > 30000 { text = String(text.prefix(30000)) }
        let existing = existingTags.isEmpty ? "" : "\n已经用过的标签（合适就沿用同一个写法，不合适再起新的）：\(existingTags.prefix(40).joined(separator: "、"))\n"
        let prompt = summaryPrompt.replacingOccurrences(of: "{source}", with: a.source)
            .replacingOccurrences(of: "{title}", with: a.title).replacingOccurrences(of: "{existing}", with: existing)
            .replacingOccurrences(of: "{text}", with: text)
        let raw = try await LLM.complete(prompt, config: config, maxTokens: 1500, timeout: 180)
        var tldr = "", tags: [String] = [], body: [String] = []
        for line in raw.split(separator: "\n").map({ $0.trimmingCharacters(in: .whitespaces) }) {
            if let r = line.range(of: #"^\**TLDR\**\s*[:：]\s*"#, options: .regularExpression) { tldr = String(line[r.upperBound...]); continue }
            if let r = line.range(of: #"^\**TAGS\**\s*[:：]\s*"#, options: .regularExpression) {
                tags = line[r.upperBound...].split(whereSeparator: { "，,、;；".contains($0) })
                    .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " #`\"")) }.filter { !$0.isEmpty && $0.count <= 12 }
                continue
            }
            if line.hasPrefix("- ") || line.hasPrefix("* ") { body.append("- " + line.dropFirst(2)) }
        }
        guard !tldr.isEmpty else { throw SparkError("摘要格式不对") }
        return Summary(tldr: tldr, tags: Array(tags.prefix(4)), body: body.joined(separator: "\n"))
    }

    // MARK: 索引

    /// 按标签归组：两篇以上用到的标签成一组（一篇可能在几组里），剩下的放「其他」；每组里新存的在前
    static func renderIndex(_ clips: [ClipMeta]) -> String {
        let sorted = clips.sorted { ($0.clipped, $0.path) > ($1.clipped, $1.path) }
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
            guard let a = e.article, let dir = ClipInbox.folder(e.id) else {
                failed.append("\(e.url)：\(e.error ?? "没抓到正文")")
                // 抓了三天都不行的就不留了
                if e.created < Date().addingTimeInterval(-3 * 86400) { ClipInbox.remove(e.id) }
                continue
            }
            var summary: Clips.Summary?
            if let config {
                status = "正在写摘要：\(a.title.prefix(20))"
                let used = Array(Set(clipMetas(vault).flatMap(\.tags))).sorted()
                summary = try? await Clips.summarize(a, existingTags: used, config: config)
                if summary == nil { notes.append("《\(a.title)》的摘要没写成，正文已经存了") }
            }
            var name = Clips.fileName(title: a.title, clipped: e.created)
            for n in 2..<100 where vault.exists("\(Clips.folder)/\(name)") {
                name = (name as NSString).deletingPathExtension.replacingOccurrences(of: #"_\d+$"#, with: "", options: .regularExpression) + "_\(n).md"
            }
            let path = "\(Clips.folder)/\(name)"
            let text = Clips.render(a, note: e.note, summary: summary, clipped: e.created)
            var images: [String: String] = [:]
            do {
                for (local, remote) in a.images {
                    images["\(Clips.folder)/\(local)"] = remote
                    if let data = try? Data(contentsOf: dir.appending(path: local)) {
                        try vault.addGenerated(path: "\(Clips.folder)/\(local)", data: data, rebuild: false)
                    }
                }
                try vault.addGenerated(path: path, text: text)
            } catch {
                failed.append("《\(a.title)》：\(error.localizedDescription)")
                continue
            }
            if let msg = await deliver(path: path, content: text, images: images, title: a.title, vault: vault) { notes.append(msg) }
            ClipInbox.remove(e.id)
            saved.append(a.title)
        }
        if !saved.isEmpty { try? vault.addGenerated(path: Clips.indexPath, text: Clips.renderIndex(clipMetas(vault))) }
        var parts: [String] = []
        if !saved.isEmpty {
            parts.append("存进剪藏 \(saved.count) 篇" + (config == nil ? "（没填模型 Key，没写摘要）" : ""))
        }
        if !failed.isEmpty { parts.append("没存成 \(failed.count) 篇：" + failed.joined(separator: "；")) }
        status = (parts + notes).joined(separator: "。")
    }

    func setStatus(_ s: String?) { status = s }

    func clipMetas(_ vault: Vault) -> [ClipMeta] {
        vault.library.clips.compactMap { d in vault.localText(d.path).map { Clips.meta(path: d.path, text: $0) } }
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

    private var tags: [(String, Int)] {
        var c: [String: Int] = [:]
        for m in metas { for t in Set(m.tags) { c[t, default: 0] += 1 } }
        return c.sorted { ($0.value, $1.key) > ($1.value, $0.key) }
    }

    var body: some View {
        Group {
            HStack(spacing: 10) {
                // 系统的粘贴按钮：点了直接拿到剪贴板里的文字，不弹「允许粘贴」
                PasteButton(payloadType: String.self) { texts in
                    Task { await clip(texts.first ?? "") }
                }
                .tint(p.accent).buttonBorderShape(.roundedRectangle(radius: 10))
                .disabled(adding || processor.running)
                Text("复制了文章链接后点「粘贴」存下来").font(.system(size: 13)).foregroundStyle(p.ink2)
                if processor.running { ProgressView() }
                Spacer()
                if metas.count >= 2, !picking {
                    Button("综合小结") { picking = true; picked = []; pickNote = nil }
                        .font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
                        .accessibilityHint("挑几篇剪藏，放在一起写一份小结")
                }
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)

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
                Text("在微信、Safari 里看到想留的文章：分享 → Spark → 「存到剪藏」。全文和图片都存下来，填了模型 Key 的话还会写摘要、打标签。也可以复制链接后点上面的「粘贴」。")
                    .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            } else {
                if !tags.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            chip(nil, "全部 \(metas.count)")
                            ForEach(tags, id: \.0) { t, n in chip(t, "\(t) \(n)") }
                        }
                    }
                    .contentMargins(.horizontal, 0, for: .scrollContent)   // 外面那栏的边距不要再叠一层
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                ForEach(metas.filter { tag == nil || $0.tags.contains(tag!) }) { m in
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
                    }
                }
            }
        }
        .task(id: vault.library.clips.count) { metas = processor.clipMetas(vault).sorted { ($0.clipped, $0.path) > ($1.clipped, $1.path) } }
        .task { await processor.run(vault: vault) }
    }

    private func clipRow(_ m: ClipMeta) -> some View {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(m.source.isEmpty ? "剪藏" : m.source).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                                Spacer()
                                Text(Dates.label(m.clipped.replacingOccurrences(of: "-", with: ""))).font(.system(size: 12.5)).foregroundStyle(p.ink2)
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
        let firstTitle = metas.filter { picked.contains($0.path) }.min { ($0.clipped, $0.path) < ($1.clipped, $1.path) }?.title ?? ""
        let title = ReviewRunner.pickedTitle(firstTitle, count: picked.count)
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
