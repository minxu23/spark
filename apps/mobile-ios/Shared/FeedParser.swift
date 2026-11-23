import CryptoKit
import Foundation

/// RSS / Atom 订阅源：手机自己检查信息跟进用。条目 id、发布日期的算法跟电脑端 core/sources.py 的
/// fetch_rss_playlist 一样（guid 或链接取 SHA-1 前 16 位），两边认的是同一条，不会重复处理。
/// 日期：带时区的时刻换算到本机时区，取那天（电脑上一样取电脑本机的那天），时刻另记在 at。
enum Feed {
    struct Entry: Sendable {
        var id = ""
        var title = ""
        var url = ""
        var date = ""        // yyyyMMdd（本机时区的那天），没有就是空串
        var at = ""          // 知道发表时刻时：ISO 8601 带时差（DocTime.iso）；只知道哪天时是空串
        var html = ""        // 全文或摘要
        var videoID = ""     // YouTube 的源：处理记录里用视频 id
        var duration = 0     // 秒，Substack 的节目才有
    }

    static func stableID(_ s: String) -> String {
        Insecure.SHA1.hash(data: Data(s.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16).description
    }

    /// session：YouTube 的订阅源传 YouTube.session（不带 cookies 的专用会话），别的照用 URLSession.shared
    static func fetch(_ url: URL, session: URLSession = .shared) async throws -> [Entry] {
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
                     forHTTPHeaderField: "User-Agent")
        let (data, resp) = try await session.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard status < 400 else { throw SparkError("打不开（HTTP \(status)）") }
        let out = parse(data)
        if out.isEmpty { throw SparkError("里面没有找到条目") }
        return out
    }

    /// 解析不完整的源（中途 XML 出错）也把已经读到的条目交出去
    static func parse(_ data: Data) -> [Entry] {
        let reader = Reader()
        let p = XMLParser(data: data)
        p.delegate = reader
        p.shouldProcessNamespaces = false
        p.parse()
        return reader.entries
    }

    private static func formatter(_ f: String) -> DateFormatter {
        let d = DateFormatter()
        d.locale = Locale(identifier: "en_US_POSIX")
        d.timeZone = TimeZone(identifier: "UTC")
        d.dateFormat = f
        return d
    }

    /// 带时区的写法：能确定到时刻
    nonisolated(unsafe) private static let zoned = [
        "EEE, dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss zzz", "EEE, dd MMM yyyy HH:mm Z", "EEE, dd MMM yyyy HH:mm zzz",
        "dd MMM yyyy HH:mm:ss Z", "dd MMM yyyy HH:mm:ss zzz", "yyyy-MM-dd'T'HH:mm:ssXXXXX", "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX",
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX", "yyyy-MM-dd'T'HH:mmXXXXX", "yyyy-MM-dd HH:mm:ss Z",
    ].map(formatter)
    /// 不带时区的：不知道是哪个时区的时刻，只取写着的那天
    nonisolated(unsafe) private static let plain = [
        "EEE, dd MMM yyyy HH:mm:ss", "EEE, dd MMM yyyy HH:mm", "dd MMM yyyy HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd",
    ].map(formatter)
    nonisolated(unsafe) private static let output = formatter("yyyyMMdd")

    /// RFC 822 或 ISO 8601 → 发表时间：带时区的是时刻，不带的只有那天（同电脑 sources._rss_published）。
    /// 正好是 0 时差的 00:00:00：多半是只有日期的源补出来的，也只算那天
    static func when(_ raw: String) -> DocTime.When? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        let hasZone = s.range(of: #"(?:[+-]\d{2}:?\d{2}|\b(?:GMT|UTC|UT|[ECMP][SD]T)|Z)\s*$"#, options: .regularExpression) != nil
        if hasZone, let d = zoned.lazy.compactMap({ $0.date(from: s) }).first {
            guard DocTime.fromEpoch(d.timeIntervalSince1970) != nil else { return nil }
            let zero = s.range(of: #"(?:\b(?:GMT|UTC|UT)|Z|[+-]00:?00)\s*$"#, options: .regularExpression) != nil
            if zero, s.range(of: #"\b00:00(?::00)?\b"#, options: .regularExpression) != nil {
                return DocTime.parse(output.string(from: d))
            }
            return .at(d)
        }
        guard let d = plain.lazy.compactMap({ $0.date(from: s) }).first else { return nil }
        return DocTime.parse(output.string(from: d))    // 按 UTC 读进来、按 UTC 写出去，就是写着的那天（不在合理范围的不要）
    }

    /// (yyyyMMdd，本机时区的那天；ISO 时刻，只知道哪天时是空串)
    static func stamp(_ raw: String) -> (date: String, at: String) {
        guard let w = when(raw) else { return ("", "") }
        return (DocTime.day(w).replacingOccurrences(of: "-", with: ""), DocTime.date(w).map { DocTime.iso($0) } ?? "")
    }

    /// RFC 822 或 ISO 8601 → yyyyMMdd（本机时区的那天）
    static func date(_ raw: String) -> String { stamp(raw).date }

    private final class Reader: NSObject, XMLParserDelegate {
        var entries: [Entry] = []
        private var cur: [String: String]?
        private var alternate = ""             // Atom 的 <link href>
        private var path: [String] = []        // 条目里面的元素
        private var buf = ""

        func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName: String?,
                    attributes: [String: String] = [:]) {
            let n = name.lowercased()
            if cur == nil {
                if n == "item" || n == "entry" { cur = [:]; alternate = ""; path = [] }
                return
            }
            if path.isEmpty {
                buf = ""
                if n == "link", let href = attributes["href"], ["alternate", ""].contains(attributes["rel"] ?? ""), alternate.isEmpty {
                    alternate = href
                }
            } else {
                buf += "<\(n)>"                // Atom 的 xhtml 正文：标签留着，后面当 HTML 转
            }
            path.append(n)
        }

        func parser(_ parser: XMLParser, foundCharacters s: String) { if cur != nil { buf += s } }

        func parser(_ parser: XMLParser, foundCDATA data: Data) {
            if cur != nil { buf += String(decoding: data, as: UTF8.self) }
        }

        func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName: String?) {
            let n = name.lowercased()
            guard cur != nil else { return }
            if path.isEmpty {
                if n == "item" || n == "entry" { finish() }
                return
            }
            path.removeLast()
            if path.isEmpty {
                if cur?[n] == nil || cur?[n]?.isEmpty == true { cur?[n] = buf.trimmingCharacters(in: .whitespacesAndNewlines) }
            } else {
                buf += "</\(n)>"
            }
        }

        private func finish() {
            defer { cur = nil }
            guard let c = cur else { return }
            let link = alternate.isEmpty ? c["link"] ?? "" : alternate
            let guid = [c["guid"], c["id"], link].compactMap { $0 }.first { !$0.isEmpty } ?? ""
            guard !guid.isEmpty else { return }
            let body = [c["content:encoded"], c["content"], c["summary"], c["description"]].compactMap { $0 }.first { !$0.isEmpty } ?? ""
            let when = [c["pubdate"], c["published"], c["dc:date"], c["issued"], c["updated"], c["modified"]].compactMap { $0 }
                .first { !$0.isEmpty } ?? ""
            let title = c["title"] ?? ""
            let stamp = stamp(when)
            entries.append(Entry(id: stableID(guid), title: title.isEmpty ? "Untitled-\(entries.count + 1)" : title,
                                 url: link.isEmpty ? guid : link, date: stamp.date, at: stamp.at, html: body, videoID: c["yt:videoid"] ?? ""))
        }
    }
}
