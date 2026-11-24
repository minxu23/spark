import Foundation

/// 占位剪藏（存链接时正文没抓到，文件头 stub: true）补抓正文。电脑上 core/clip_capture.start_refetch 同一套：
/// 抓到了：照新剪藏的做法写摘要、翻译，换掉占位的正文，去掉 stub；星标、置顶、稍后再读、划线、批注、读后感、
/// 备注、收录时间、文件名都留着。抓不到：文件不动，返回原因。
extension ClipProcessor {
    /// 补抓一篇占位剪藏。成功返回 nil，失败返回给人看的原因（文件不动）
    func refetch(path: String, vault: Vault) async -> String? {
        guard !running else { return "正在收别的剪藏，等一下再补抓" }
        guard let old = vault.localText(path), LaterRule.isStub(old) else { return "这篇的正文已经有了，不用补抓" }
        running = true
        defer { running = false }
        let meta = Clips.meta(path: path, text: old)
        guard let url = URL(string: meta.url), !meta.url.isEmpty else { return "这篇没有记原文地址，补抓不了" }
        setStatus("正在补抓正文：\(meta.title.prefix(20))")
        var article: Clipper.Article
        do { article = try await Clipper.fetch(url) } catch {
            setStatus(nil)
            return "没抓到正文：\(error.localizedDescription)"
        }
        // 占位时的标题是网址凑的（或者没改过）：换成抓到的；自己改过的标题留着
        if !meta.title.isEmpty, meta.title != LaterRule.stubTitle(meta.url) {
            article.title = meta.title
            article.titleGuessed = false
        } else if article.title.trimmingCharacters(in: .whitespaces).isEmpty {
            article.title = LaterRule.stubTitle(meta.url)
            article.titleGuessed = true
        }
        // 图片先下到临时文件夹，再放进笔记库（跟 retryClipImage 一样）
        let tmp = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }
        _ = await Clipper.downloadImages(article.images, into: tmp)
        article = Clipper.mergeDownloaded(article, dir: tmp)
        var notes: [String] = []
        let summary = await enrich(&article, dir: tmp, vault: vault, config: LLM.Config.load(), notes: &notes)
        setStatus("正在保存")
        // 补抓的时候这篇可能已经被补好了、或者被删了：以现在手机上的为准
        guard let latest = vault.localText(path), LaterRule.isStub(latest) else {
            setStatus(nil)
            return "这篇在补抓时已经有正文了，没覆盖"
        }
        let f = Clips.frontmatter(latest)
        let clipped = DocTime.date(DocTime.best(at: f["clipped_at"] ?? "", day: f["clipped"] ?? "")) ?? Date()
        var new = Clips.render(article, note: f["note"] ?? "", summary: summary, clipped: clipped)
        new = Clips.carryNotes(old: latest, new: new)         // 划线、批注、读后感
        new = LaterRule.keepingFlags(old: latest, new: new)   // 星标、置顶、稍后再读：原样、原来的先后
        var images: [String: String] = [:]
        do {
            for (local, remote) in article.images {
                images["\(Clips.folder)/\(local)"] = remote
                if let data = try? Data(contentsOf: tmp.appending(path: local)) {
                    try vault.addGenerated(path: "\(Clips.folder)/\(local)", data: data, rebuild: false)
                }
            }
            try await vault.saveEdited(path: path, text: new, images: images)
            try? vault.addGenerated(path: Clips.indexPath, text: Clips.renderIndex(clipMetas(vault)))
        } catch {
            setStatus(nil)
            return "没存上：\(error.localizedDescription)"
        }
        setStatus(notes.isEmpty ? "已补抓正文：《\(article.title)》" : "已补抓正文：《\(article.title)》。" + notes.joined(separator: "。"),
                  sticky: !notes.isEmpty)
        return nil
    }
}

extension Clips {
    /// 补抓后把旧文件里的笔记接到新内容上（电脑 core/annotations._carry_notes 的简化版，占位剪藏正文只有一句说明，
    /// 能划的只有那一句）：划线的字在新正文里还找得到就原处划上，批注跟着挂上；找不到的放进文末「以前的划线」，批注不丢；
    /// 读后感整节接回去；批注脚注都留着
    static func carryNotes(old: String, new: String) -> String {
        var out = new
        var oldBody = old
        if let r = DocEdit.reflectionRange(oldBody) { oldBody.removeSubrange(r) }
        if let r = oldBody.range(of: "\n## 正文\n") { oldBody = String(oldBody[r.upperBound...]) }
        var missed: [String] = []
        let ns = oldBody as NSString
        for m in RX.re(MarkRule.withNotes).matches(in: oldBody, range: NSRange(location: 0, length: ns.length)) {
            let text = ns.substring(with: m.range(at: 1)), refs = ns.substring(with: m.range(at: 2))
            var from = out.range(of: "\n## 正文\n")?.upperBound ?? out.startIndex
            var placed = false
            while let r = out.range(of: text, range: from..<out.endIndex) {
                // 已经在某处划线里的不再划
                let line = out.lineRange(for: r)
                let at = out[line.lowerBound..<r.lowerBound].utf16.count
                let inMark = MarkRule.spans(String(out[line])).contains { $0.whole.location <= at && at < $0.whole.location + $0.whole.length }
                if inMark { from = r.upperBound; continue }
                out.replaceSubrange(r, with: "==\(text)==\(refs)")
                placed = true
                break
            }
            if !placed { missed.append("- ==\(text)==\(refs)") }
        }
        if !missed.isEmpty {
            out = out.trimmingCharacters(in: .newlines) + "\n\n## 以前的划线\n\n重新生成后原文里找不到这几处了，批注还在。\n\n" + missed.joined(separator: "\n") + "\n"
        }
        let reflection = DocEdit.reflection(old)
        if !reflection.isEmpty, DocEdit.reflection(out).isEmpty { out = DocEdit.withReflection(out, reflection) }
        let have = DocEdit.noteDefs(out)
        let extra = DocEdit.noteDefs(old).filter { have[$0.key] == nil }.sorted { $0.key < $1.key }.map { "[^note-\($0.key)]: \($0.value)" }
        if !extra.isEmpty { out = out.trimmingCharacters(in: .newlines) + "\n\n" + extra.joined(separator: "\n") + "\n" }
        return out
    }
}
