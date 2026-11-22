import CryptoKit
import Foundation

/// 剪藏里同一张图只存一份。公众号图片消息的页面里，一张图的地址会出现好几次，尺寸、格式、参数各不一样
/// （…/640?wx_fmt=jpeg&from=appmsg、…/0?wx_fmt=jpeg、&tp=webp、脚本里的 \x26amp;）；小红书的图地址前面带一段每次都变的签名。
/// 先认出是不是同一张图（key），挑最好的版本；下载以后字节一样的、确实是同一张的再合并一次（mergeIdentical）。
/// 电脑上的同一套在 core/clips.py（image_key、stable_name、merge_identical）。都是纯函数，tests/test_xhs_clip.py 编译了直接测。
enum ClipImages {
    /// 页面脚本里的地址还原：HTML 实体、\x26amp;、\x26、/、\/
    static func clean(_ s: String) -> String {
        var t = s
        for _ in 0..<2 {
            t = t.replacingOccurrences(of: #"\x26amp;"#, with: "&").replacingOccurrences(of: #"\x26"#, with: "&")
                .replacingOccurrences(of: #"&"#, with: "&").replacingOccurrences(of: #"/"#, with: "/")
                .replacingOccurrences(of: #"\/"#, with: "/")
            t = HTMLToMarkdown.decodeEntities(t)
        }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func isWeChat(_ host: String) -> Bool {
        host == "mmbiz.qpic.cn" || host.hasSuffix(".mmbiz.qpic.cn") || host == "mmbiz.qlogo.cn" || host.hasSuffix(".mmbiz.qlogo.cn")
    }

    static func isXHS(_ host: String) -> Bool {
        host.hasSuffix("rednotecdn.com") || host.hasSuffix("xhscdn.com")
    }

    /// 只管格式、尺寸、来源的参数（只在公众号、小红书的图床上当成无关参数）
    private static func formatParam(_ name: String) -> Bool {
        let k = name.lowercased()
        return ["wx_fmt", "tp", "wxfrom", "from", "format", "wx_lazy", "wx_co"].contains(k)
            || k.hasPrefix("imageview2") || k.hasPrefix("imagemogr2")
    }

    /// 小红书图片的文件编号：最后一段「!」前面的部分，至少 20 个字母数字（短的是网站自己的图标、静态图，不算）
    static func xhsFileID(_ u: URL) -> String? {
        guard isXHS((u.host ?? "").lowercased()), let last = u.path.split(separator: "/").last,
              let id = last.split(separator: "!").first.map(String.init),
              id.count >= 20, id.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil else { return nil }
        return id
    }

    /// 同一张图的标识。
    /// 公众号（mmbiz.qpic.cn、mmbiz.qlogo.cn）：主机类别 + 路径到图片编号为止（后面的 /0、/640 是尺寸，参数不管）；
    /// 小红书图床：文件编号（≥20 位）；编号太短的用主机 + 路径 + 去掉格式参数的参数；
    /// 别的网站：整个地址只去掉开头的 http:/https: 和 # 后面的部分，别的一个字都不动。
    static func key(_ u: URL) -> String {
        let host = (u.host ?? "").lowercased()
        let parts = u.path.split(separator: "/").map(String.init)
        if isWeChat(host), parts.count >= 2 {
            return (host.hasSuffix("qlogo.cn") ? "qlogo" : "qpic") + "/" + parts[0] + "/" + parts[1]
        }
        if isXHS(host) {
            if let id = xhsFileID(u) { return "xhs/" + id }
            var c = URLComponents(url: u, resolvingAgainstBaseURL: false)
            let items = (c?.queryItems ?? []).filter { !formatParam($0.name) }
            c?.queryItems = items.isEmpty ? nil : items
            return "//" + host + (c?.percentEncodedPath ?? u.path) + ((c?.percentEncodedQuery).map { "?" + $0 } ?? "")
        }
        return plainKey(u.absoluteString)
    }

    /// 去掉开头的协议名和 # 后面的部分（电脑 clips.image_key 逐字一样）
    static func plainKey(_ s: String) -> String {
        var t = s.replacingOccurrences(of: #"^[A-Za-z][A-Za-z0-9+.-]*:"#, with: "", options: .regularExpression)
        if let i = t.firstIndex(of: "#") { t = String(t[..<i]) }
        return t
    }

    /// 同一张图几个版本里的好坏：原图（公众号的 /0）、不转 webp、不缩略的分高
    static func score(_ u: URL) -> Int {
        let host = (u.host ?? "").lowercased()
        let q = (u.query ?? "").lowercased()
        var s = 0
        if isWeChat(host) {
            let parts = u.path.split(separator: "/")
            if parts.count >= 3, parts[2] == "0" { s += 2 }
            if parts.count == 2 { s += 1 }
        }
        if q.contains("tp=webp") || q.contains("wx_fmt=webp") || q.contains("format/webp") || q.contains("format=webp") { s -= 2 }
        if q.contains("imageview2") || q.contains("imagemogr2") || q.contains("thumbnail") { s -= 3 }
        if u.scheme?.lowercased() == "https" { s += 1 }
        return s
    }

    /// 去重：每张图留第一次出现的位置，换成几个版本里最好的那个
    static func unique(_ urls: [URL]) -> [URL] {
        var order: [String] = []
        var best: [String: URL] = [:]
        for u in urls {
            let k = key(u)
            if let b = best[k] {
                if score(u) > score(b) { best[k] = u }
            } else {
                order.append(k)
                best[k] = u
            }
        }
        return order.compactMap { best[$0] }
    }

    /// 每个地址换成它那张图最好的版本（正文里的图用：位置、引用次数都不变）
    static func canonical(_ urls: [URL]) -> [URL] {
        let best = Dictionary(unique(urls).map { (key($0), $0) }, uniquingKeysWith: { a, _ in a })
        return urls.map { best[key($0)] ?? $0 }
    }

    /// 换成更好的版本时记下页面上原来写的那个：好版本（原图）下不来或者太大时退回去下它。
    /// pairs：(用的地址, 页面上的地址)；返回 本地文件名 → 退回用的地址
    static func fallbacks(_ pairs: [(URL, URL)], name: (URL) -> String) -> [String: String] {
        var out: [String: String] = [:]
        for (used, listed) in pairs where used != listed && out[name(used)] == nil { out[name(used)] = listed.absoluteString }
        return out
    }

    /// 地址里每次都变的图（小红书的签名地址）和页面数据里列出来的图用的本地文件名：按 key 算，不按地址算，
    /// 重新打开原文拿到新地址时还对得上。attachments/<key 的 sha1 前 16 位>.<扩展名>（电脑 clips.stable_name 一样）
    static func stableName(_ u: URL) -> String {
        let hash = Insecure.SHA1.hash(data: Data(key(u).utf8)).map { String(format: "%02x", $0) }.joined().prefix(16)
        return "\(Clipper.attachments)/\(hash).\(Clipper.localName(u).split(separator: ".").last ?? "jpg")"
    }

    /// 下载以后：字节一样（digests 里 sha256 相同）的图在下面两种情况下才当成同一张，只留正文里先出现的那张、引用改过去：
    /// 1. 两张图原地址的 key 一样（同一张图的两个版本）；
    /// 2. imagePost（纯图片的文章）里，两张图的那两行紧挨着（中间没有字，也没有别的图），
    ///    并且这份字节只出现在这两个 key 上（三个以上不同 key 的图字节一样，多半是占位图，不按这条合并）。
    /// 别的字节一样的图不合并：几张不同的图可能都下成了同一张占位图（「图片已删除」、限流），合并了就再也分不开。
    /// 纯图片的文章里合并以后同一张图出现第二次的那行去掉；普通文章两处引用都留着（指向同一个文件）。没下下来的图不动。
    static func mergeIdentical(markdown: String, images: [String: String], digests: [String: String],
                               imagePost: Bool) -> (markdown: String, images: [String: String]) {
        let blocks = markdown.components(separatedBy: "\n\n")
        func imageTarget(_ b: String) -> String? {
            let t = b.trimmingCharacters(in: .whitespacesAndNewlines)
            guard t.hasPrefix("!["), t.hasSuffix(")"), !t.contains("\n"), let open = t.range(of: "](") else { return nil }
            return String(t[open.upperBound..<t.index(before: t.endIndex)])
        }
        // 每张图出现在第几段
        var positions: [String: [Int]] = [:]
        for (i, b) in blocks.enumerated() { if let t = imageTarget(b) { positions[t, default: []].append(i) } }
        func firstUse(_ n: String) -> Int { positions[n]?.first ?? Int.max }
        func keyOf(_ n: String) -> String? { images[n].flatMap(URL.init(string:)).map(key) }
        func adjacent(_ a: String, _ b: String) -> Bool {
            for i in positions[a] ?? [] { for j in positions[b] ?? [] where abs(i - j) == 1 { return true } }
            return false
        }
        func related(_ a: String, _ b: String, distinctKeys: Int) -> Bool {
            if let ka = keyOf(a), let kb = keyOf(b), ka == kb { return true }
            return imagePost && distinctKeys == 2 && adjacent(a, b)
        }
        var groups: [String: [String]] = [:]
        for (name, digest) in digests where images[name] != nil && !digest.isEmpty { groups[digest, default: []].append(name) }
        var md = markdown, imgs = images
        for names in groups.values where names.count > 1 {
            // 同一组字节里，按「有关系」连成几块（并查集），每块留最先出现的那张
            let sorted = names.sorted { (firstUse($0), $0) < (firstUse($1), $1) }
            let distinctKeys = Set(sorted.map { keyOf($0) ?? $0 }).count
            var parent = Array(sorted.indices)
            func root(_ i: Int) -> Int { var i = i; while parent[i] != i { i = parent[i] }; return i }
            for i in sorted.indices { for j in sorted.indices where j > i && related(sorted[i], sorted[j], distinctKeys: distinctKeys) {
                let a = root(i), b = root(j)
                parent[max(a, b)] = min(a, b)     // 根总是块里最先出现的那张
            } }
            for i in sorted.indices where root(i) != i {
                let keep = sorted[root(i)], other = sorted[i]
                md = md.replacingOccurrences(of: "](\(other))", with: "](\(keep))")
                imgs[other] = nil
            }
        }
        if imagePost { md = dropRepeatedImages(md) }
        return (md, imgs)
    }

    /// 单独一行的 ![](同一张图) 第二次出现时去掉
    static func dropRepeatedImages(_ md: String) -> String {
        var seen = Set<String>()
        var blocks: [String] = []
        for b in md.components(separatedBy: "\n\n") {
            let t = b.trimmingCharacters(in: .whitespacesAndNewlines)
            if t.hasPrefix("!["), t.hasSuffix(")"), !t.contains("\n"), let open = t.range(of: "](") {
                let target = String(t[open.upperBound..<t.index(before: t.endIndex)])
                if !seen.insert(target).inserted { continue }
            }
            blocks.append(b)
        }
        return blocks.joined(separator: "\n\n")
    }
}
