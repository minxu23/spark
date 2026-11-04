import CryptoKit
import Foundation

/// RSS / Atom 订阅源：手机自己检查信息跟进用。条目 id、发布日期的算法跟电脑端 core/sources.py 的
/// fetch_rss_playlist 一样（guid 或链接取 SHA-1 前 16 位，日期按 UTC），两边认的是同一条，不会重复处理。
enum Feed {
    struct Entry: Sendable {
        var id = ""
        var title = ""
        var url = ""
        var date = ""        // yyyyMMdd，没有就是空串
        var html = ""        // 全文或摘要
        var videoID = ""     // YouTube 的源：处理记录里用视频 id
        var duration = 0     // 秒，Substack 的节目才有
    }

    static func stableID(_ s: String) -> String {
        Insecure.SHA1.hash(data: Data(s.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16).description
    }

    static func fetch(_ url: URL) async throws -> [Entry] {
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
                     forHTTPHeaderField: "User-Agent")
        let (data, resp) = try await URLSession.shared.data(for: req)
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

    nonisolated(unsafe) private static let inputs = [
        "EEE, dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss zzz", "EEE, dd MMM yyyy HH:mm Z", "EEE, dd MMM yyyy HH:mm zzz",
        "dd MMM yyyy HH:mm:ss Z", "dd MMM yyyy HH:mm:ss zzz", "yyyy-MM-dd'T'HH:mm:ssXXXXX", "yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX",
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXXXX", "yyyy-MM-dd'T'HH:mmXXXXX", "yyyy-MM-dd HH:mm:ss Z", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd",
    ].map(formatter)
    nonisolated(unsafe) private static let output = formatter("yyyyMMdd")

    /// RFC 822 或 ISO 8601 → UTC 的 yyyyMMdd（feedparser 的 published_parsed 也是 UTC）
    static func date(_ raw: String) -> String {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return "" }
        return inputs.lazy.compactMap { $0.date(from: s) }.first.map { output.string(from: $0) } ?? ""
    }

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
            entries.append(Entry(id: stableID(guid), title: title.isEmpty ? "Untitled-\(entries.count + 1)" : title,
                                 url: link.isEmpty ? guid : link, date: date(when), html: body, videoID: c["yt:videoid"] ?? ""))
        }
    }
}
