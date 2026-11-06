import Foundation

/// X（推特）的文章：网页里长文不出现，普通推文也没有标题，混着头像、点赞数。
/// 用 FxTwitter 的公开接口取全文、作者、日期和图片（只发推文编号，不带任何账号信息）；取不到再退回网页。
enum XPost {
    static func isX(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return ["x.com", "twitter.com", "m.x.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"].contains(host)
    }

    static func statusID(_ url: URL) -> String? {
        Clipper.first(#"/status(?:es)?/(\d+)"#, url.path)
    }

    static func fetch(_ url: URL) async throws -> Clipper.Article {
        guard let id = statusID(url), let api = URL(string: "https://api.fxtwitter.com/i/status/\(id)") else {
            throw SparkError("不是一条推文的链接")
        }
        var req = URLRequest(url: api, timeoutInterval: 20)
        req.setValue("Spark", forHTTPHeaderField: "User-Agent")
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode ?? 0 < 400,
              let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let t = obj["tweet"] as? [String: Any] else { throw SparkError("取不到这条推文") }
        return try article(t, url: url)
    }

    /// 接口返回的一条推文 → 剪藏文章（单独拿出来方便测）
    static func article(_ t: [String: Any], url: URL) throws -> Clipper.Article {
        let author = (t["author"] as? [String: Any])?["name"] as? String ?? ""
        let stamp = (t["created_timestamp"] as? Double) ?? (t["created_timestamp"] as? Int).map(Double.init)
        let date = stamp.map { Clipper.day.string(from: Date(timeIntervalSince1970: $0)) } ?? ""
        var images: [String: String] = [:]
        func image(_ remote: String?) -> String? {
            guard let remote, let u = URL(string: remote) else { return nil }
            let name = Clipper.localName(u)
            images[name] = remote
            return "![](\(name))"
        }
        if let a = t["article"] as? [String: Any], let md = articleMarkdown(a, image: image), !md.isEmpty {
            let cover = image(((a["cover_media"] as? [String: Any])?["media_info"] as? [String: Any])?["original_img_url"] as? String)
            return Clipper.Article(url: url.absoluteString, title: a["title"] as? String ?? "", source: author, published: date,
                                   markdown: [cover, md].compactMap { $0 }.joined(separator: "\n\n"), images: images)
        }
        let text = (t["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        var parts = text.components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        if let q = t["quote"] as? [String: Any], let qt = q["text"] as? String, !qt.isEmpty {
            parts.append("> " + qt.replacingOccurrences(of: "\n", with: "\n> "))
        }
        for m in ((t["media"] as? [String: Any])?["all"] as? [[String: Any]]) ?? [] {
            let remote = m["type"] as? String == "photo" ? m["url"] as? String : m["thumbnail_url"] as? String
            if let md = image(remote) { parts.append(md) }
        }
        guard !parts.isEmpty else { throw SparkError("这条推文没有内容") }
        return Clipper.Article(url: url.absoluteString, title: guessTitle(text), source: author, published: date,
                               markdown: parts.joined(separator: "\n\n"), images: images, titleGuessed: true)
    }

    /// X 长文的段落（Draft.js 的 blocks）→ Markdown
    private static func articleMarkdown(_ a: [String: Any], image: (String?) -> String?) -> String? {
        guard let content = a["content"] as? [String: Any], let blocks = content["blocks"] as? [[String: Any]] else { return nil }
        var mediaURL: [String: String] = [:]
        for m in a["media_entities"] as? [[String: Any]] ?? [] {
            if let id = m["media_id"] as? String, let u = (m["media_info"] as? [String: Any])?["original_img_url"] as? String { mediaURL[id] = u }
        }
        var entityMedia: [String: String] = [:]
        for e in content["entityMap"] as? [[String: Any]] ?? [] {
            guard let key = e["key"] as? String ?? (e["key"] as? Int).map(String.init),
                  let items = ((e["value"] as? [String: Any])?["data"] as? [String: Any])?["mediaItems"] as? [[String: Any]],
                  let id = items.first?["mediaId"] as? String else { continue }
            entityMedia[key] = id
        }
        var out: [String] = [], number = 0
        for b in blocks {
            let text = (b["text"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            let type = b["type"] as? String ?? ""
            if type != "ordered-list-item" { number = 0 }
            switch type {
            case "atomic":
                let key = (b["entityRanges"] as? [[String: Any]])?.first.flatMap { $0["key"] as? Int }.map(String.init)
                if let key, let id = entityMedia[key], let md = image(mediaURL[id]) { out.append(md) }
            case "header-one": out.append("# \(text)")
            case "header-two": out.append("## \(text)")
            case "header-three": out.append("### \(text)")
            case "blockquote": out.append("> \(text)")
            case "unordered-list-item": out.append("- \(text)")
            case "ordered-list-item":
                number += 1
                out.append("\(number). \(text)")
            default: if !text.isEmpty { out.append(text) }
            }
        }
        return out.joined(separator: "\n\n")
    }

    /// 推文没有标题：取第一句。填了模型的话，模型会再起一个（Clips.summarize）
    static func guessTitle(_ text: String) -> String {
        let line = text.components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.first { !$0.isEmpty } ?? ""
        let cut = line.firstIndex { "。！？!?；".contains($0) }.map { String(line[...$0]) } ?? line
        let s = cut.count >= 6 ? cut : line
        return s.count > 40 ? String(s.prefix(40)) + "…" : s
    }

    // MARK: 接口取不到时，收拾网页版

    /// 「X 上的 DAN KOE：“标题” / X」「DAN KOE on X: "标题" / X」：拆出标题和作者。没有标题的返回 nil
    static func parseTitle(_ raw: String) -> (title: String?, author: String?) {
        let t = raw.trimmingCharacters(in: .whitespaces)
        for p in [#"^X 上的 (.+?)[：:]\s*[“"](.+)[”"]\s*(?:/ X)?$"#, #"^(.+?) on X:\s*[“"](.+)[”"]\s*(?:/ X)?$"#] {
            let g = RXLite.groups(p, t)
            if g.count == 3 { return (g[2], g[1]) }
        }
        for p in [#"^X 上的 (.+?)(?:\s*\(@\w+\))?\s*(?:/ X)?$"#, #"^(.+?)(?:\s*\(@\w+\))? on X\s*(?:/ X)?$"#] {
            let g = RXLite.groups(p, t)
            if g.count == 2 { return (nil, g[1]) }
        }
        return (nil, nil)
    }

    /// 去掉页面元素：头像、账号、发布时间、浏览量、点赞数
    static func clean(_ md: String) -> String {
        md.components(separatedBy: "\n\n").filter { block in
            let b = block.trimmingCharacters(in: .whitespacesAndNewlines)
            if b.isEmpty || b.contains("launch_app_store=") || b == "自 x.com" || b.hasPrefix("X. It") { return false }
            if b == "浏览量" || b.hasSuffix("分钟阅读") { return false }
            if b.range(of: #"^\[@\w+\]\(https://x\.com/\w+\)$"#, options: .regularExpression) != nil { return false }
            return b.range(of: #"^[\d.,]+[万亿KM]?$"#, options: .regularExpression) == nil
        }.joined(separator: "\n\n")
    }
}

/// Shared 里用的一点正则（主 App 的 RX 分享扩展里没有）
enum RXLite {
    static func groups(_ pattern: String, _ s: String) -> [String] {
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) else { return [] }
        return (0..<m.numberOfRanges).map { m.range(at: $0).location == NSNotFound ? "" : (s as NSString).substring(with: m.range(at: $0)) }
    }
}
