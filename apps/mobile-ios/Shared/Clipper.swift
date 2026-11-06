import CryptoKit
import Foundation

/// 剪藏：把一篇网页文章（公众号、博客、新闻）存下来，正文转成 Markdown，图片下载到本地。
/// 分享扩展里当场抓（几秒），抓好的先放在 App Group 的 clips/ 里；主 App 回到前台时收进笔记库、写摘要（ClipInbox）。
/// 扩展里时间不够、没下完的图片，主 App 接着下。
enum Clipper {
    static let folder = "Spark/剪藏"
    static let attachments = "attachments"
    static let userAgent = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    static let maxImages = 60
    static let maxImageBytes = 8 << 20

    struct Article: Codable {
        var url: String
        var title: String
        var source: String          // 公众号名、站点名
        var published: String       // YYYY-MM-DD 或空
        var markdown: String        // 正文，图片写成 ![](attachments/xxx.jpg)
        var images: [String: String] // attachments/xxx.jpg -> 原图地址
        var titleGuessed: Bool? = nil   // 页面上没有真标题，是从正文猜的，写摘要时让模型起一个
        var imagesOnly: Bool? = nil     // 纯图片的文章（小绿书、小红书）：写摘要时把图给模型看
    }

    // MARK: 抓取

    static func fetch(_ url: URL) async throws -> Article {
        if XPost.isX(url), let a = try? await XPost.fetch(url) { return a }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        req.setValue("zh-CN,zh;q=0.9,en;q=0.8", forHTTPHeaderField: "Accept-Language")
        let (data, resp) = try await URLSession.shared.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard status < 400 else { throw SparkError("网页打不开（HTTP \(status)）") }
        let html = String(decoding: data, as: UTF8.self)
        let final = resp.url ?? url
        if html.contains("环境异常") && html.contains("完成验证") {
            throw SparkError("公众号要求验证，暂时抓不到正文，过一会儿再试")
        }
        var meta = metadata(html, url: final)
        var conv = HTMLToMarkdown(base: final)
        var md = container(html).map { conv.convert($0) } ?? ""
        var guessed = false
        if XPost.isX(url) {
            md = XPost.clean(md.replacingOccurrences(of: #"!\[\]\(IMG:\d+\)\n\n(?=[^\n]+\n\n\[@)"#, with: "", options: .regularExpression))
            let parsed = XPost.parseTitle(meta.title)
            if let a = parsed.author { meta.source = a }
            if let t = parsed.title { meta.title = t } else if parsed.author != nil {
                meta.title = XPost.guessTitle(metaContent(html, "og:description") ?? md)
                guessed = true
            }
        }
        // 纯图片的文章（公众号小绿书、小红书）：正文几乎没有字，图在页面的数据里。图存下来，文字用页面的简介
        if md.filter({ !$0.isWhitespace }).count < 50 {
            let pictures = pictureURLs(html, base: final)
            guard !pictures.isEmpty || !conv.images.isEmpty else {
                throw SparkError("没抓到正文（可能需要登录、付费，或者正文是视频）")
            }
            var parts = [metaContent(html, "og:description") ?? "", md].filter { !$0.isEmpty }
            var images: [String: String] = Dictionary(conv.images.map { (localName($0), $0.absoluteString) }, uniquingKeysWith: { a, _ in a })
            for (i, u) in conv.images.enumerated() { parts = parts.map { $0.replacingOccurrences(of: "](IMG:\(i))", with: "](\(localName(u)))") } }
            for u in pictures where images[localName(u)] == nil {
                images[localName(u)] = u.absoluteString
                parts.append("![](\(localName(u)))")
            }
            return Article(url: url.absoluteString, title: meta.title, source: meta.source, published: meta.date,
                           markdown: parts.joined(separator: "\n\n"), images: images, imagesOnly: true)
        }
        var out = dropAds(md, shapes: conv.shapes)
        var images: [String: String] = [:]
        for (i, u) in conv.images.enumerated() where out.contains("](IMG:\(i))") { images[localName(u)] = u.absoluteString }
        for (i, u) in conv.images.enumerated() {
            out = out.replacingOccurrences(of: "](IMG:\(i))", with: "](\(localName(u)))")
        }
        return Article(url: url.absoluteString, title: meta.title, source: meta.source, published: meta.date, markdown: out, images: images,
                       titleGuessed: guessed ? true : nil)
    }

    /// 页面数据里的图片地址：公众号图片消息的 cdn_url、小红书的 urlDefault，都没有就用 og:image
    static func pictureURLs(_ html: String, base: URL) -> [URL] {
        var raw: [String] = []
        let ns = html as NSString
        for p in [#"cdn_url["']?\s*[:=]\s*["']([^"']+)["']"#, #""urlDefault"\s*:\s*"([^"]+)""#,
                  #"<meta[^>]+(?:property|name)=["']og:image["'][^>]*content=["']([^"']+)["']"#] {
            guard let re = try? NSRegularExpression(pattern: p, options: .caseInsensitive) else { continue }
            raw += re.matches(in: html, range: NSRange(location: 0, length: ns.length)).map { ns.substring(with: $0.range(at: 1)) }
            if !raw.isEmpty { break }
        }
        var seen = Set<String>()
        return raw.compactMap { s -> URL? in
            let u = HTMLToMarkdown.decodeEntities(s).replacingOccurrences(of: #"\x26amp;"#, with: "&").replacingOccurrences(of: #"\x26"#, with: "&")
                .replacingOccurrences(of: #"\u002F"#, with: "/").replacingOccurrences(of: #"\/"#, with: "/")
            guard seen.insert(u).inserted else { return nil }
            return URL(string: u.hasPrefix("//") ? "https:" + u : u, relativeTo: base)?.absoluteURL
        }.prefix(maxImages).map { $0 }
    }

    // MARK: 广告图

    /// 公众号文章里常见的不是正文的图：引导关注的横幅、动图，二维码；文末「往期推荐」一节整个不要。认出来就不收。
    /// shapes：每张图的高宽比（0 = 不知道）和是不是动图，来自公众号 img 上的 data-ratio、wx_fmt
    static func dropAds(_ md: String, shapes: [Int: HTMLToMarkdown.Shape]) -> String {
        var blocks = md.components(separatedBy: "\n\n")
        func imageIndex(_ b: String) -> Int? {
            Clipper.first(#"^!\[\]\(IMG:(\d+)\)$"#, b.trimmingCharacters(in: .whitespacesAndNewlines)).flatMap(Int.init)
        }
        func isPromo(_ b: String) -> Bool {
            let t = b.replacingOccurrences(of: "*", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
            return !t.isEmpty && t.count <= 40 && t.range(of: promo, options: .regularExpression) != nil
        }
        let textIdx = blocks.indices.filter { imageIndex(blocks[$0]) == nil && !blocks[$0].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let firstText = textIdx.first ?? 0, lastText = textIdx.last ?? blocks.count
        // 文末「往期推荐」「推荐阅读」之后的图都是别的文章的配图
        let tail = blocks.indices.first { i in
            let t = blocks[i].replacingOccurrences(of: #"[*#\s\-—]"#, with: "", options: .regularExpression)
            return t.range(of: #"^(往期推荐|往期回顾|推荐阅读|相关阅读|热门推荐|精选推荐|END)$"#, options: .regularExpression) != nil
        } ?? blocks.count
        var drop = Set<Int>()
        for (i, b) in blocks.enumerated() {
            guard let n = imageIndex(b) else { continue }
            let s = shapes[n] ?? .init(ratio: 0, gif: false)
            let edge = i < firstText || i > lastText            // 正文开始之前、结束之后
            let wide = s.ratio > 0 && s.ratio < (edge ? 0.45 : 0.3)   // 扁长条
            let gif = s.gif && (s.ratio == 0 || s.ratio < 0.8)  // 「点击关注」一类的动图
            let qr = i > lastText && s.ratio > 0.8 && s.ratio < 1.25   // 正文后面的方图：多半是二维码
            let near = [i - 1, i + 1].filter { blocks.indices.contains($0) && imageIndex(blocks[$0]) == nil }
            let promoNear = near.filter { isPromo(blocks[$0]) }
            if wide || gif || qr || !promoNear.isEmpty {
                drop.insert(i)
                drop.formUnion(promoNear)                        // 紧挨着的「长按识别二维码关注」一起去掉
            }
        }
        // 「往期推荐」那一节（标题、别的文章的链接和配图）整个不要
        drop.formUnion(tail..<blocks.count)
        blocks = blocks.enumerated().filter { !drop.contains($0.offset) }.map(\.element)
        return blocks.joined(separator: "\n\n")
    }

    /// 引导关注、转发的话。只认这些说法，图注里出现「值得关注」这类词不算
    private static let promo = "点击上方|点击下方|点击蓝字|蓝字关注|关注我们|关注公众号|加个?关注|扫码|扫描二维码|长按识别|长按二维码|识别二维码|识别图中|设为星标|标星|置顶公众号|点个在看|点亮在看|点赞在看|分享给朋友|商务合作|投稿邮箱|赞赏|加群|进群|添加微信|微信号"

    /// 图片的本地文件名：attachments/<原地址的 sha1 前 16 位>.<扩展名>
    static func localName(_ u: URL) -> String {
        let hash = Insecure.SHA1.hash(data: Data(u.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16)
        var ext = (URLComponents(url: u, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "wx_fmt" }?.value ?? u.pathExtension).lowercased()
        if ext == "jpeg" { ext = "jpg" }
        if !["jpg", "png", "gif", "webp"].contains(ext) { ext = "jpg" }
        return "\(attachments)/\(hash).\(ext)"
    }

    /// 把还没下的图片下到 dir 下（dir/attachments/xxx.jpg）。deadline 到了就停，没下完的下次接着下。
    @discardableResult
    static func downloadImages(_ images: [String: String], into dir: URL, deadline: Date = .distantFuture) async -> Int {
        let fm = FileManager.default
        try? fm.createDirectory(at: dir.appending(path: attachments), withIntermediateDirectories: true)
        let todo = images.filter { !fm.fileExists(atPath: dir.appending(path: $0.key).path) }.sorted { $0.key < $1.key }
        var done = 0
        await withTaskGroup(of: Bool.self) { group in
            var it = todo.makeIterator()
            func add() -> Bool {
                guard Date() < deadline, let (name, remote) = it.next(), let u = URL(string: remote) else { return false }
                group.addTask {
                    var req = URLRequest(url: u, timeoutInterval: 20)
                    req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
                    guard let (data, resp) = try? await URLSession.shared.data(for: req),
                          (resp as? HTTPURLResponse)?.statusCode ?? 0 < 400, !data.isEmpty, data.count <= maxImageBytes,
                          (resp.mimeType ?? "image/").hasPrefix("image/") else { return false }
                    return (try? data.write(to: dir.appending(path: name), options: .atomic)) != nil
                }
                return true
            }
            for _ in 0..<4 where add() {}
            while let ok = await group.next() {
                if ok { done += 1 }
                _ = add()
            }
        }
        return done
    }

    // MARK: 页面信息

    private static func metaContent(_ html: String, _ key: String) -> String? {
        let k = NSRegularExpression.escapedPattern(for: key)
        for p in ["<meta[^>]+(?:property|name)=[\"']\(k)[\"'][^>]*content=[\"']([^\"']*)[\"']",
                  "<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']\(k)[\"']"] {
            if let v = first(p, html), !v.isEmpty { return HTMLToMarkdown.decodeEntities(v) }
        }
        return nil
    }

    static func first(_ pattern: String, _ s: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)),
              m.numberOfRanges > 1, m.range(at: 1).location != NSNotFound else { return nil }
        return (s as NSString).substring(with: m.range(at: 1))
    }

    static func metadata(_ html: String, url: URL) -> (title: String, source: String, date: String) {
        var title = metaContent(html, "og:title") ?? metaContent(html, "twitter:title")
            ?? first("<title[^>]*>([^<]+)</title>", html).map(HTMLToMarkdown.decodeEntities) ?? ""
        title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        // 公众号：作者在 js_name 里，发布时间是 var ct = "秒数"
        var source = first(#"id="js_name"[^>]*>\s*([^<]+?)\s*<"#, html).map(HTMLToMarkdown.decodeEntities)
            ?? first(#"var nickname\s*=\s*htmlDecode\("([^"]+)"\)"#, html)
            ?? metaContent(html, "og:site_name") ?? metaContent(html, "author") ?? ""
        source = source.trimmingCharacters(in: .whitespacesAndNewlines)
        if source.isEmpty || source == "微信公众平台" { source = url.host()?.replacingOccurrences(of: "www.", with: "") ?? "" }
        var date = ""
        if let ct = first(#"var ct\s*=\s*"(\d{9,11})""#, html) ?? first(#""create_time"\s*:\s*"?(\d{9,11})"#, html),
           let secs = TimeInterval(ct) {
            date = day.string(from: Date(timeIntervalSince1970: secs))
        } else if let iso = metaContent(html, "article:published_time") ?? first(#"<time[^>]+datetime="([^"]+)""#, html) {
            date = String(iso.prefix(10))
            if date.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) == nil { date = "" }
        }
        return (title.isEmpty ? (url.host() ?? "未命名") : title, source, date)
    }

    nonisolated(unsafe) static let day: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate, .withDashSeparatorInDate]
        f.timeZone = .current
        return f
    }()

    /// 正文所在的那段 HTML：公众号是 #js_content，别的网站找 <article>、<main>，都没有就整个 <body>
    static func container(_ html: String) -> String? {
        let ns = html as NSString
        for p in [#"<div[^>]+id="js_content"[^>]*>"#, #"<div[^>]+id="img-content"[^>]*>"#, "<article[\\s>]", "<main[\\s>]", "<body[\\s>]"] {
            guard let re = try? NSRegularExpression(pattern: p, options: .caseInsensitive),
                  let m = re.firstMatch(in: html, range: NSRange(location: 0, length: ns.length)) else { continue }
            let tag = first("^<([a-zA-Z]+)", ns.substring(with: m.range))?.lowercased() ?? "div"
            if let end = matchingClose(ns, tag: tag, from: m.range.location + m.range.length) {
                return ns.substring(with: NSRange(location: m.range.location, length: end - m.range.location))
            }
            return ns.substring(from: m.range.location)
        }
        return nil
    }

    /// 从 from 开始数同名标签的开合，找到对应的结束标签，返回它结束的位置
    private static func matchingClose(_ ns: NSString, tag: String, from: Int) -> Int? {
        guard let re = try? NSRegularExpression(pattern: "<(/?)\(tag)(?=[\\s>/])[^>]*>", options: .caseInsensitive) else { return nil }
        var depth = 1
        for m in re.matches(in: ns as String, range: NSRange(location: from, length: ns.length - from)) {
            let closing = m.range(at: 1).length > 0
            let selfClosing = ns.substring(with: m.range).hasSuffix("/>")
            if closing { depth -= 1 } else if !selfClosing { depth += 1 }
            if depth == 0 { return m.range.location + m.range.length }
        }
        return nil
    }
}

/// 够用的 HTML → Markdown：段落、标题、加粗、列表、引用、代码、链接、图片。
/// 样式、脚本、表单一概丢掉；图片先写成 ![](IMG:序号)，地址收在 images 里。
struct HTMLToMarkdown {
    let base: URL
    private(set) var images: [URL] = []
    /// 每张图的形状（序号 → 高宽比、是不是动图），认广告图用
    private(set) var shapes: [Int: Shape] = [:]
    struct Shape { let ratio: Double; let gif: Bool }
    private var out = ""
    private var skipDepth = 0
    private var skipTag = ""
    private var pre = 0
    private var listStack: [String] = []
    private var linkStarts: [(Int, String)] = []
    private var quoteStarts: [Int] = []

    init(base: URL) { self.base = base }

    private static let blockTags: Set<String> = ["p", "div", "section", "article", "header", "footer", "figure", "figcaption",
                                                 "table", "tr", "ul", "ol", "dl", "dd", "dt", "center", "main", "aside", "h1", "h2",
                                                 "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "li"]
    private static let skipTags: Set<String> = ["script", "style", "noscript", "svg", "iframe", "form", "button", "select",
                                                "textarea", "template", "nav", "mpprofile", "mp-common-profile", "qqmusic", "mpvoice"]

    mutating func convert(_ html: String) -> String {
        let ns = html as NSString
        guard let re = try? NSRegularExpression(pattern: "<!--[\\s\\S]*?-->|<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>") else { return "" }
        var last = 0
        for m in re.matches(in: html, range: NSRange(location: 0, length: ns.length)) {
            text(ns.substring(with: NSRange(location: last, length: m.range.location - last)))
            last = m.range.location + m.range.length
            guard m.range(at: 2).location != NSNotFound else { continue }   // 注释
            let closing = m.range(at: 1).length > 0
            let name = ns.substring(with: m.range(at: 2)).lowercased()
            let attrs = ns.substring(with: m.range(at: 3))
            tag(name, closing: closing, attrs: attrs)
        }
        text(ns.substring(from: last))
        return Self.tidy(out)
    }

    private mutating func text(_ raw: String) {
        guard skipDepth == 0, !raw.isEmpty else { return }
        let t = Self.decodeEntities(raw)
        if pre > 0 { out += t; return }
        var s = t.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        if out.hasSuffix("\n") || out.hasSuffix(" ") || out.isEmpty { s = String(s.drop { $0 == " " }) }
        out += s
    }

    private mutating func block() {
        guard !out.isEmpty else { return }
        while out.hasSuffix(" ") { out.removeLast() }
        if !out.hasSuffix("\n\n") { out += out.hasSuffix("\n") ? "\n" : "\n\n" }
    }

    private static func attr(_ name: String, _ attrs: String) -> String? {
        Clipper.first("\(name)\\s*=\\s*\"([^\"]*)\"", attrs) ?? Clipper.first("\(name)\\s*=\\s*'([^']*)'", attrs)
    }

    private mutating func tag(_ name: String, closing: Bool, attrs: String) {
        if skipDepth > 0 {
            if name == skipTag { skipDepth += closing ? -1 : 1 }
            return
        }
        if Self.skipTags.contains(name), !closing, !attrs.hasSuffix("/") {
            skipTag = name; skipDepth = 1
            return
        }
        // 公众号里隐藏的元素（比如原文链接的占位）
        if !closing, attrs.contains("display: none") || attrs.contains("display:none"), Self.blockTags.contains(name) || name == "span" {
            // 只跳过不含图片的隐藏块太麻烦，整块跳过
            skipTag = name; skipDepth = 1
            return
        }
        switch name {
        case "br":
            out += pre > 0 ? "\n" : "  \n"
        case "hr":
            block(); out += "---"; block()
        case "img":
            guard !closing, let src = Self.attr("data-src", attrs) ?? Self.attr("src", attrs),
                  !src.hasPrefix("data:"), let u = URL(string: Self.decodeEntities(src), relativeTo: base)?.absoluteURL,
                  ["http", "https"].contains(u.scheme ?? "") else { return }
            // 很小的图（分隔线、图标）不要
            if let w = Self.attr("data-w", attrs) ?? Self.attr("width", attrs), let n = Int(w), n < 60 { return }
            guard images.count < Clipper.maxImages else { return }
            if let i = images.firstIndex(of: u) { block(); out += "![](IMG:\(i))"; block(); return }
            images.append(u)
            var ratio = Self.attr("data-ratio", attrs).flatMap(Double.init) ?? 0
            if ratio == 0, let w = Self.attr("data-w", attrs).flatMap(Double.init), let h = Self.attr("data-h", attrs).flatMap(Double.init), w > 0 {
                ratio = h / w
            }
            let fmt = (URLComponents(url: u, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "wx_fmt" }?.value ?? u.pathExtension).lowercased()
            shapes[images.count - 1] = Shape(ratio: ratio, gif: fmt == "gif" || Self.attr("data-type", attrs) == "gif")
            block(); out += "![](IMG:\(images.count - 1))"; block()
        case "h1", "h2", "h3", "h4", "h5", "h6":
            block()
            if !closing { out += name <= "h2" ? "## " : "### " }
        case "strong", "b":
            out += "**"
        case "pre":
            block()
            if closing { pre = max(0, pre - 1); out += "\n```"; block() } else { pre += 1; out += "```\n" }
        case "ul", "ol":
            if closing { _ = listStack.popLast() } else { listStack.append(name) }
            block()
        case "li":
            if closing { return }
            if !out.hasSuffix("\n") && !out.isEmpty { out += "\n" }
            out += String(repeating: "  ", count: max(0, listStack.count - 1)) + (listStack.last == "ol" ? "1. " : "- ")
        case "blockquote":
            block()
            if closing, let start = quoteStarts.popLast(), start <= out.count {
                let idx = out.index(out.startIndex, offsetBy: start)
                let inner = out[idx...].trimmingCharacters(in: .whitespacesAndNewlines)
                out = String(out[..<idx]) + inner.split(separator: "\n", omittingEmptySubsequences: false).map { "> " + $0 }.joined(separator: "\n")
                block()
            } else if !closing {
                quoteStarts.append(out.count)
            }
        case "a":
            if closing, let (start, href) = linkStarts.popLast(), start <= out.count {
                let idx = out.index(out.startIndex, offsetBy: start)
                let label = out[idx...].trimmingCharacters(in: .whitespaces)
                if !label.isEmpty, !label.contains("!["), !label.contains("\n"), label != href {
                    out = String(out[..<idx]) + "[\(label)](\(href))"
                }
            } else if !closing {
                let href = Self.attr("href", attrs).map(Self.decodeEntities) ?? ""
                if let u = URL(string: href, relativeTo: base)?.absoluteURL, ["http", "https"].contains(u.scheme ?? "") {
                    linkStarts.append((out.count, u.absoluteString))
                } else {
                    linkStarts.append((Int.max, ""))
                }
            }
        case "td", "th":
            if !closing, !out.hasSuffix("\n") { out += "　" }
        default:
            if Self.blockTags.contains(name) { block() }
        }
    }

    /// 收拾一下：去掉空的加粗、行首行尾空白、多余空行
    static func tidy(_ s: String) -> String {
        var t = s.replacingOccurrences(of: #"\*\*(\s*)\*\*"#, with: "$1", options: .regularExpression)
        // 「** 文字 **」→「**文字**」
        t = t.replacingOccurrences(of: #"\*\*[ \t]+([^*\n]+?)[ \t]*\*\*"#, with: "**$1**", options: .regularExpression)
        t = t.replacingOccurrences(of: #"\*\*([^*\n]+?)[ \t]+\*\*"#, with: "**$1**", options: .regularExpression)
        var lines: [String] = []
        var inCode = false
        for raw in t.split(separator: "\n", omittingEmptySubsequences: false) {
            var line = String(raw)
            if line.hasPrefix("```") { inCode.toggle() }
            if !inCode {
                let hard = line.hasSuffix("  ")
                line = line.trimmingCharacters(in: .whitespaces)
                if line == "**" || line == ">" { line = "" }
                if hard, !line.isEmpty { line += "  " }
            }
            lines.append(line)
        }
        t = lines.joined(separator: "\n")
        // 空的标题行；跨了换行的加粗（「**上半句\n\n下半句**」）接成一行
        t = t.replacingOccurrences(of: #"(?m)^#{1,6}[ \t]*$"#, with: "", options: .regularExpression)
        for _ in 0..<3 {
            t = t.replacingOccurrences(of: #"\*\*([^*\n]+)\n+([^*\n]+)\*\*"#, with: "**$1 $2**", options: .regularExpression)
        }
        t = t.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
        return t.trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
    }

    static func decodeEntities(_ s: String) -> String {
        guard s.contains("&") else { return s }
        let named = ["&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": "\"", "&#39;": "'", "&apos;": "'",
                     "&ldquo;": "“", "&rdquo;": "”", "&lsquo;": "‘", "&rsquo;": "’", "&mdash;": "—", "&hellip;": "…", "&middot;": "·"]
        var out = s
        for (k, v) in named where out.contains(k) { out = out.replacingOccurrences(of: k, with: v) }
        guard out.contains("&#") , let re = try? NSRegularExpression(pattern: "&#(x?)([0-9a-fA-F]+);") else { return out }
        let ns = out as NSString
        var result = "", last = 0
        for m in re.matches(in: out, range: NSRange(location: 0, length: ns.length)) {
            result += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let hex = m.range(at: 1).length > 0
            let num = ns.substring(with: m.range(at: 2))
            if let v = UInt32(num, radix: hex ? 16 : 10), let sc = Unicode.Scalar(v) { result.append(Character(sc)) }
            last = m.range.location + m.range.length
        }
        return result + ns.substring(from: last)
    }
}

/// 剪藏的收件箱：App Group 容器里的 clips/<id>/（meta.json + attachments/）。
/// 分享扩展往里放，主 App 收进笔记库后删掉。
enum ClipInbox {
    struct Entry: Codable, Identifiable {
        var id: String
        var url: String
        var note: String
        var created: Date
        var article: Clipper.Article?
        var error: String?
    }

    static var dir: URL? {
        guard let base = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: SharedStore.groupID) else { return nil }
        let d = base.appending(path: "clips", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    static func folder(_ id: String) -> URL? { dir?.appending(path: id, directoryHint: .isDirectory) }

    static func add(url: String, note: String) throws -> Entry {
        guard let d = dir else { throw SparkError("收件箱打不开（App Group 没配好）") }
        let e = Entry(id: UUID().uuidString.lowercased(), url: url, note: note, created: .now)
        try FileManager.default.createDirectory(at: d.appending(path: e.id), withIntermediateDirectories: true)
        try save(e)
        return e
    }

    static func save(_ e: Entry) throws {
        guard let f = folder(e.id) else { return }
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .iso8601
        try enc.encode(e).write(to: f.appending(path: "meta.json"), options: .atomic)
    }

    static func entries() -> [Entry] {
        guard let d = dir, let ids = try? FileManager.default.contentsOfDirectory(atPath: d.path) else { return [] }
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        return ids.compactMap { id in
            guard let data = try? Data(contentsOf: d.appending(path: id).appending(path: "meta.json")) else { return nil }
            return try? dec.decode(Entry.self, from: data)
        }.sorted { $0.created < $1.created }
    }

    static func remove(_ id: String) {
        if let f = folder(id) { try? FileManager.default.removeItem(at: f) }
    }

    /// 抓正文、在 deadline 前尽量把图片下完。抓不到就把原因记下，主 App 里再试。返回下好的图片张数
    @discardableResult
    static func capture(_ e: inout Entry, deadline: Date) async -> Int {
        guard let u = URL(string: e.url), let f = folder(e.id) else { return 0 }
        if e.article == nil {
            do {
                e.article = try await Clipper.fetch(u)
                e.error = nil
            } catch {
                e.error = error.localizedDescription
            }
            try? save(e)
        }
        guard let a = e.article else { return 0 }
        return await Clipper.downloadImages(a.images, into: f, deadline: deadline)
    }
}
