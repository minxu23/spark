import Foundation

/// 剪藏的链接：从分享文字里找出链接、http 换成 https、判断两个链接是不是同一篇。
/// 手机 App、分享扩展和 Mac 应用共用（Mac 那边是指向这里的链接）。都是纯函数，tests/test_xhs_clip.py 编译了直接测。
enum ClipLink {
    /// 链接里不会原样出现的字：空白、中文标点、中文引号。系统认出的链接到这里为止
    private static let stops = CharacterSet.whitespacesAndNewlines
        .union(CharacterSet(charactersIn: "“”‘’「」『』（）【】《》〈〉，。！？；：、…　"))

    /// 从一段文字里找出第一个 http(s) 链接（小红书、YouTube 分享的是「标题… 链接 一句提示」）。
    /// 先用系统的 NSDataDetector（认得路径里的中文、中文域名），再把它多带上的尾巴去掉：中文引号、括号后面的字，
    /// 末尾的英文标点、Markdown 的 **、多出来的右括号。系统一个都没认出来时，退回只认 ASCII 的 extract。
    static func firstURL(in text: String) -> String? {
        if let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) {
            let ns = text as NSString
            for m in detector.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
                guard let u = m.url, ["http", "https"].contains(u.scheme?.lowercased() ?? "") else { continue }
                let raw = ns.substring(with: m.range)
                var s = String(raw.unicodeScalars.prefix { !stops.contains($0) }.map(Character.init))
                s = trimTail(s)
                if s == raw { return u.absoluteString }
                // 去过尾巴的再让系统认一遍（中文路径、中文域名照样编码）
                let again = detector.matches(in: s, range: NSRange(location: 0, length: (s as NSString).length)).first?.url
                if let v = again, ["http", "https"].contains(v.scheme?.lowercased() ?? "") { return v.absoluteString }
            }
        }
        return extract(text)
    }

    /// 末尾的英文标点、Markdown 的 *、多出来的右括号去掉
    private static func trimTail(_ str: String) -> String {
        var s = str
        while let last = s.last {
            if ".,;:!?'*\"".contains(last) { s.removeLast(); continue }
            if last == ")" && s.filter({ $0 == "(" }).count < s.filter({ $0 == ")" }).count { s.removeLast(); continue }
            if last == "]" && s.filter({ $0 == "[" }).count < s.filter({ $0 == "]" }).count { s.removeLast(); continue }
            break
        }
        return s
    }

    /// 只认 ASCII 字符的链接（firstURL 的后备）：碰到空白、中文就断开
    static func extract(_ text: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: #"(?i)https?://[!#-;=?-~]+"#) else { return nil }
        let ns = text as NSString
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let s = trimTail(ns.substring(with: m.range))
            guard let u = URL(string: s), ["http", "https"].contains(u.scheme?.lowercased() ?? ""),
                  let host = u.host, !host.isEmpty else { continue }
            return s
        }
        return nil
    }

    /// 本机、局域网的地址（localhost、*.local、内网和回环 IP）：不换 https，照旧走 http（Info.plist 允许局域网）
    static func isLocal(_ host: String) -> Bool {
        let h = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h == "localhost" || h.hasSuffix(".localhost") || h.hasSuffix(".local") { return true }
        let v4 = h.split(separator: ".").compactMap { Int($0) }
        if v4.count == 4, h.split(separator: ".").count == 4, v4.allSatisfy({ (0...255).contains($0) }) {
            return v4[0] == 10 || v4[0] == 127 || (v4[0] == 172 && (16...31).contains(v4[1]))
                || (v4[0] == 192 && v4[1] == 168) || (v4[0] == 169 && v4[1] == 254) || v4[0] == 0
        }
        if h.contains(":") {   // IPv6
            return h == "::1" || h.hasPrefix("fe80:") || h.hasPrefix("fc") || h.hasPrefix("fd")
        }
        return false
    }

    /// http 换成 https：iOS 默认不让 App 走不加密的连接（ATS），小红书分享出来的却是 http:// 短链。
    /// 本机、局域网的地址不换；别的不动
    static func secure(_ u: URL) -> URL {
        guard u.scheme?.lowercased() == "http", !isLocal(u.host ?? ""),
              var c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return u }
        c.scheme = "https"
        if c.port == 80 { c.port = nil }
        return c.url ?? u
    }

    static func secure(_ s: String) -> String {
        guard let u = URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines)) else { return s }
        return secure(u).absoluteString
    }

    /// 判断「已经在剪藏里了」用的标识（电脑 core/clips.link_identity 是同一套，逐字一样）：
    /// 小红书是笔记编号（同一条笔记每次分享的短链不一样，存的是笔记地址）；
    /// 别的网站按字面：http、https 当一样，主机名小写，去掉 :80、:443；# 后面的去掉，
    /// 但「#/」「#!」开头的留着（单页网站靠它区分文章）；参数原样留着、次序不变，只去掉 utm_*、fbclid、gclid、spm；
    /// 不是 ASCII 的字按 UTF-8 写成 %XX。
    static func identity(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if let u = URL(string: t), let id = XHS.noteID(u) { return "xhs:" + id }
        guard let re = try? NSRegularExpression(pattern: #"^(?i:https?)://([^/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$"#),
              let m = re.firstMatch(in: t, range: NSRange(location: 0, length: (t as NSString).length)) else { return t }
        let ns = t as NSString
        func part(_ i: Int) -> String { m.range(at: i).location == NSNotFound ? "" : ns.substring(with: m.range(at: i)) }
        var host = part(1).lowercased()
        for p in [":80", ":443"] where host.hasSuffix(p) { host.removeLast(p.count) }
        let query = part(3).dropFirst().split(separator: "&", omittingEmptySubsequences: true).filter { q in
            let k = q.split(separator: "=", maxSplits: 1).first.map { $0.lowercased() } ?? ""
            return !k.hasPrefix("utm_") && !["fbclid", "gclid", "spm"].contains(k)
        }
        let frag = part(4)
        var out = "https://" + host + part(2) + (query.isEmpty ? "" : "?" + query.joined(separator: "&"))
        if frag.hasPrefix("#/") || frag.hasPrefix("#!") { out += frag }
        return percentNonASCII(out)
    }

    private static func percentNonASCII(_ s: String) -> String {
        var out = ""
        for sc in s.unicodeScalars {
            if sc.isASCII { out.unicodeScalars.append(sc) } else {
                for b in String(sc).utf8 { out += String(format: "%%%02X", b) }
            }
        }
        return out
    }

    static func same(_ a: String, _ b: String) -> Bool {
        !a.isEmpty && !b.isEmpty && identity(a) == identity(b)
    }

    /// 原链接是 http、换成 https 以后连不上：证书、加密握手出错，或者 443 端口根本不通。
    /// 这时说「网站只支持不加密的连接」；原链接本来就是 https 的（网站只是暂时打不开）不算
    static func insecureOnly(_ error: Error, original: URL, tried: URL) -> Bool {
        guard original.scheme?.lowercased() == "http", tried.scheme?.lowercased() == "https",
              let e = error as? URLError else { return false }
        let tls: [URLError.Code] = [.secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
                                    .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot, .clientCertificateRejected,
                                    .clientCertificateRequired, .appTransportSecurityRequiresSecureConnection]
        if tls.contains(e.code) { return true }
        return e.code == .cannotConnectToHost && (tried.port == nil || tried.port == 443)
    }
    static let insecureOnlyMessage = "这个网站只支持不加密的连接，手机上打不开"
}
