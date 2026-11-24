import Foundation

/// 小红书笔记。分享出来的是 http://xhslink.com/o/xxx 短链，打开后跳到 www.rednote.com（或 xiaohongshu.com）的笔记页；
/// 页面上看得见的字只有导航栏，笔记在脚本 window.__INITIAL_STATE__ 的 JSON 里：标题、正文、图片、作者、发布时间。
/// 笔记页要带分享链接里的 xsec_token 才打得开，不带会跳到 404。不登录、不带 Cookie，碰到要登录或验证的页面就报打不开。
enum XHS {
    static let deadMessage = "这条小红书笔记打不开：可能已删除、链接过期，或者需要登录"
    static let videoLine = "（视频笔记，视频没有存）"

    /// 确定打不开：404、跳到首页、要登录、不是笔记的页面。收件箱里报一次错就不再自动重试（ClipInbox.Entry.gone）；
    /// 断网、超时这类可能过一会儿就好的不算
    struct Gone: LocalizedError {
        var errorDescription: String? { XHS.deadMessage }
    }

    static func isXHS(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return ["xhslink.com", "xiaohongshu.com", "rednote.com"].contains { host == $0 || host.hasSuffix("." + $0) }
    }

    /// 笔记编号（24 位十六进制）：/explore/<id>、/discovery/item/<id>、/item/<id>
    static func noteID(_ url: URL) -> String? {
        guard isXHS(url), url.host?.lowercased().hasSuffix("xhslink.com") == false else { return nil }
        return Clipper.first(#"/(?:explore|discovery/item|item|notes?)/([0-9a-fA-F]{24})(?:[/?#]|$)"#, url.path + "/")?.lowercased()
    }

    static func fetch(_ url: URL) async throws -> Clipper.Article {
        try NetGate.check()
        var req = URLRequest(url: ClipLink.secure(url), timeoutInterval: 20)
        req.setValue(Clipper.userAgent, forHTTPHeaderField: "User-Agent")
        req.setValue("zh-CN,zh;q=0.9,en;q=0.8", forHTTPHeaderField: "Accept-Language")
        let (data, resp) = try await URLSession.shared.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard status < 400 else { throw status == 404 || status == 410 ? Gone() as Error : SparkError(deadMessage) }
        return try article(String(decoding: data, as: UTF8.self), final: resp.url ?? url)
    }

    struct Note {
        var id = ""
        var title = ""
        var desc = ""
        var author = ""
        var time: Double?
        var video = false
        var images: [URL] = []
    }

    /// 笔记页 → 剪藏文章（单独拿出来方便测）。final：跳转之后的地址
    static func article(_ html: String, final: URL) throws -> Clipper.Article {
        guard let n = note(html, final: final) else { throw Gone() }
        let body = text(n.desc)
        var title = n.title.trimmingCharacters(in: .whitespacesAndNewlines)
        var guessed = false
        if title.isEmpty {
            title = String((body.split(separator: "\n").first.map(String.init) ?? "").replacingOccurrences(of: #"#\S+"#, with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces).prefix(30))
            guessed = true
        }
        if title.isEmpty { title = "小红书笔记" }
        var images: [String: String] = [:]
        var parts = body.isEmpty ? [] : [body]
        if n.video { parts.append(videoLine) }
        for u in ClipImages.unique(n.images).prefix(Clipper.maxImages) {
            // 图地址带每次都变的签名：文件名按文件编号起，重新打开笔记拿到新地址时对得上
            let name = ClipImages.stableName(u)
            guard images[name] == nil else { continue }
            images[name] = u.absoluteString
            parts.append("![](\(name))")
        }
        guard !parts.isEmpty else { throw Gone() }
        let stamp = n.time.flatMap(DocTime.fromEpoch)
        // 正文字少（只有一两句配图的话）的才算纯图片：写摘要时把图给模型看
        let fewWords = body.replacingOccurrences(of: #"#\S+"#, with: "", options: .regularExpression).filter { !$0.isWhitespace }.count < 50
        return Clipper.Article(url: canonical(id: n.id, final: final, html: html), title: title, source: n.author.isEmpty ? "小红书" : n.author,
                               published: stamp.map { Clipper.day.string(from: $0) } ?? "", markdown: parts.joined(separator: "\n\n"),
                               images: images, titleGuessed: guessed ? true : nil, imagesOnly: fewWords && !images.isEmpty ? true : nil,
                               publishedAt: stamp.map { DocTime.iso($0) })
    }

    /// 存下来的地址：https://www.xiaohongshu.com/explore/<编号>?xsec_token=…。
    /// 不带 xsec_token 打开是 404（2026-11 试过），所以 token 留着，别的分享参数（来源、分享人、时间）去掉
    static func canonical(id: String, final: URL, html: String) -> String {
        let fromURL = URLComponents(url: final, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "xsec_token" }?.value
        let fromPage = Clipper.first(#"<meta[^>]+og:url[^>]+content="[^"]*[?&](?:amp;)?xsec_token=([^&"]+)"#, html)
        guard !id.isEmpty else { return ClipLink.secure(final).absoluteString }
        var c = URLComponents(string: "https://www.xiaohongshu.com/explore/\(id)")!
        if let t = (fromURL ?? fromPage.map(HTMLToMarkdown.decodeEntities)), !t.isEmpty {
            c.queryItems = [URLQueryItem(name: "xsec_token", value: t)]
        }
        return c.url?.absoluteString ?? "https://www.xiaohongshu.com/explore/\(id)"
    }

    /// 页面里的笔记：先找 JSON，找不到退回 og:title / og:description / og:image；都没有（首页、404、要登录）返回 nil
    static func note(_ html: String, final: URL) -> Note? {
        let path = final.path.lowercased()
        if path.hasPrefix("/404") || path.contains("login") || path.contains("captcha") { return nil }
        if let state = initialState(html), let raw = noteObject(state) {
            var n = Note()
            n.id = (raw["noteId"] as? String) ?? (raw["id"] as? String) ?? noteID(final) ?? ""
            n.title = raw["title"] as? String ?? ""
            n.desc = raw["desc"] as? String ?? ""
            let user = raw["user"] as? [String: Any] ?? [:]
            n.author = (user["nickName"] as? String) ?? (user["nickname"] as? String) ?? ""
            n.time = number(raw["time"]) ?? number(raw["lastUpdateTime"])
            n.video = (raw["type"] as? String) == "video"
            n.images = ((raw["imageList"] as? [[String: Any]]) ?? []).compactMap(bestImage)
            if n.images.isEmpty, n.video, let cover = raw["cover"] as? [String: Any], let u = bestImage(cover) { n.images = [u] }
            if !n.title.isEmpty || !n.desc.isEmpty || !n.images.isEmpty { return n }
        }
        // 没有 JSON：用页面头里的 og 信息（不是笔记的页面没有 og:title）
        guard var t = metaAll(html, "og:title").first, !t.isEmpty,
              t.range(of: "你的生活指南|生活兴趣社区|global lifestyle|登录|验证", options: [.regularExpression, .caseInsensitive]) == nil else { return nil }
        t = t.replacingOccurrences(of: #"\s*[-|]\s*(rednote|小红书)\s*$"#, with: "", options: [.regularExpression, .caseInsensitive])
        // 认不出笔记编号的（个人主页、搜索页）不是笔记，不存
        guard let id = noteID(final) ?? metaAll(html, "og:url").first.flatMap(URL.init(string:)).flatMap(noteID) else { return nil }
        var n = Note()
        n.id = id
        n.title = t
        n.desc = metaAll(html, "og:description").first ?? metaAll(html, "description").first ?? ""
        n.video = metaAll(html, "og:type").first == "video"
        n.images = metaAll(html, "og:image").compactMap { URL(string: ClipImages.clean($0)).map(ClipLink.secure) }
        return n
    }

    /// 笔记对象：手机版页面在 noteData.data.noteData，电脑版在 note.noteDetailMap.<编号>.note
    static func noteObject(_ state: [String: Any]) -> [String: Any]? {
        if let nd = ((state["noteData"] as? [String: Any])?["data"] as? [String: Any])?["noteData"] as? [String: Any], !nd.isEmpty {
            return nd
        }
        if let map = (state["note"] as? [String: Any])?["noteDetailMap"] as? [String: Any] {
            for v in map.values {
                if let n = (v as? [String: Any])?["note"] as? [String: Any], !n.isEmpty { return n }
            }
        }
        return nil
    }

    /// 一张图最好的地址：url，没有就用 infoList 里默认尺寸（imageScene 带 DFT）的，再没有用 urlDefault；http 换成 https
    static func bestImage(_ img: [String: Any]) -> URL? {
        let info = (img["infoList"] as? [[String: Any]]) ?? []
        let dft = info.first { (($0["imageScene"] as? String) ?? "").range(of: "DFT|default", options: [.regularExpression, .caseInsensitive]) != nil }
        let candidates = [img["url"] as? String, dft?["url"] as? String, img["urlDefault"] as? String, info.first?["url"] as? String]
        guard let s = candidates.compactMap({ $0 }).first(where: { !$0.isEmpty }) else { return nil }
        let t = ClipImages.clean(s)
        return URL(string: t.hasPrefix("//") ? "https:" + t : t).map(ClipLink.secure)
    }

    /// 正文：「#话题[话题]#」写成「#话题」，「[笑哭R]」这种表情代码不动，制表符去掉；每行一段
    static func text(_ desc: String) -> String {
        var t = desc.replacingOccurrences(of: "\t", with: "").replacingOccurrences(of: "\r", with: "")
        t = t.replacingOccurrences(of: #"#([^#\[\]\n]+?)\[话题\]#"#, with: "#$1 ", options: .regularExpression)
        // 没有名字的话题「#[话题]#」、落单的「[话题]」去掉
        t = t.replacingOccurrences(of: #"#?\[话题\]#?"#, with: "", options: .regularExpression)
        t = t.replacingOccurrences(of: #" {2,}"#, with: " ", options: .regularExpression)
        return t.components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            .joined(separator: "\n\n")
    }

    /// window.__INITIAL_STATE__ = {...}</script> → 字典。里面有不加引号的 undefined，先换成 null 再解析
    static func initialState(_ html: String) -> [String: Any]? {
        guard let start = html.range(of: "window.__INITIAL_STATE__") else { return nil }
        let rest = html[start.upperBound...]
        guard let brace = rest.firstIndex(of: "{"), let end = rest.range(of: "</script>", range: brace..<rest.endIndex) else { return nil }
        var json = String(rest[brace..<end.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
        while json.hasSuffix(";") { json.removeLast(); json = json.trimmingCharacters(in: .whitespacesAndNewlines) }
        let data = Data(jsonSafe(json).utf8)
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    /// 把字符串外面的 undefined 换成 null（字符串里面的不动）
    static func jsonSafe(_ s: String) -> String {
        var out = String.UnicodeScalarView()
        let chars = Array(s.unicodeScalars)
        let word = Array("undefined".unicodeScalars)
        var i = 0, inString = false, escaped = false
        func isIdent(_ c: Unicode.Scalar) -> Bool { CharacterSet.alphanumerics.contains(c) || c == "_" || c == "$" }
        while i < chars.count {
            let c = chars[i]
            if inString {
                out.append(c)
                if escaped { escaped = false } else if c == "\\" { escaped = true } else if c == "\"" { inString = false }
                i += 1
                continue
            }
            if c == "\"" { inString = true; out.append(c); i += 1; continue }
            if c == "u", i + word.count <= chars.count, Array(chars[i..<i + word.count]) == word,
               i == 0 || !isIdent(chars[i - 1]), i + word.count == chars.count || !isIdent(chars[i + word.count]) {
                out.append(contentsOf: "null".unicodeScalars)
                i += word.count
                continue
            }
            out.append(c)
            i += 1
        }
        return String(out)
    }

    private static func number(_ v: Any?) -> Double? {
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s.trimmingCharacters(in: .whitespaces)) }
        return nil
    }

    /// 同名的 meta 可能有好几个（og:image 一张图一个），按出现顺序都取
    static func metaAll(_ html: String, _ key: String) -> [String] {
        let k = NSRegularExpression.escapedPattern(for: key)
        var out: [String] = []
        let ns = html as NSString
        for p in ["<meta[^>]+(?:property|name)=[\"']\(k)[\"'][^>]*content=[\"']([^\"']*)[\"']",
                  "<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']\(k)[\"']"] {
            guard let re = try? NSRegularExpression(pattern: p, options: .caseInsensitive) else { continue }
            out += re.matches(in: html, range: NSRange(location: 0, length: ns.length)).map {
                HTMLToMarkdown.decodeEntities(ns.substring(with: $0.range(at: 1))).trimmingCharacters(in: .whitespacesAndNewlines)
            }
            if !out.isEmpty { break }
        }
        return out
    }
}
