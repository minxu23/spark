import Foundation

/// YouTube：手机自己列新视频、取字幕，不用 yt-dlp。列表走公开的 RSS（只有最近 15 条），字幕走 App 用的 player 接口。
/// 没有字幕的视频手机上处理不了（下不了音频），提示用户，要不要交给电脑由用户定。
enum YouTube {
    struct Video: Sendable {
        let id: String
        let title: String
        let author: String
        let duration: Int
        /// nil：这个视频没有字幕
        let track: (url: String, lang: String, kind: String)?
    }

    private static let iosClient: [String: Any] = ["clientName": "IOS", "clientVersion": "20.10.4", "deviceModel": "iPhone16,2"]
    private static let iosAgent = "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3 like Mac OS X)"
    private static let webAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"

    static func videoID(_ url: String) -> String? {
        guard let c = URLComponents(string: url), let host = c.host?.lowercased() else { return nil }
        if host == "youtu.be" { return c.path.split(separator: "/").first.map(String.init) }
        guard host == "youtube.com" || host.hasSuffix(".youtube.com") else { return nil }
        if let v = c.queryItems?.first(where: { $0.name == "v" })?.value, !v.isEmpty { return v }
        let parts = c.path.split(separator: "/").map(String.init)
        return parts.count >= 2 && ["shorts", "live", "embed"].contains(parts[0]) ? parts[1] : nil
    }

    static func isYouTube(_ url: String) -> Bool {
        guard let host = URLComponents(string: url)?.host?.lowercased() else { return false }
        return host == "youtu.be" || host == "youtube.com" || host.hasSuffix(".youtube.com")
    }

    private static func api(_ path: String, _ body: [String: Any]) async throws -> [String: Any] {
        var req = URLRequest(url: URL(string: "https://www.youtube.com/youtubei/v1/\(path)?prettyPrint=false")!, timeoutInterval: 20)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(iosAgent, forHTTPHeaderField: "User-Agent")
        var body = body
        body["context"] = ["client": iosClient]
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode ?? 0 < 400,
              let d = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw SparkError("YouTube 没有回应") }
        return d
    }

    /// 播放列表或频道的 RSS 地址。@频道名要先换成频道 id
    static func feedURL(_ url: String) async throws -> URL {
        let c = URLComponents(string: url)
        if let list = c?.queryItems?.first(where: { $0.name == "list" })?.value, !list.isEmpty {
            return URL(string: "https://www.youtube.com/feeds/videos.xml?playlist_id=\(list)")!
        }
        let parts = (c?.path ?? "").split(separator: "/").map(String.init)
        var channel = parts.count >= 2 && parts[0] == "channel" ? parts[1] : ""
        if channel.isEmpty, let name = parts.first {
            let d = try await api("navigation/resolve_url", ["url": "https://www.youtube.com/\(name)"])
            channel = ((d["endpoint"] as? [String: Any])?["browseEndpoint"] as? [String: Any])?["browseId"] as? String ?? ""
        }
        guard !channel.isEmpty else { throw SparkError("找不到这个频道") }
        return URL(string: "https://www.youtube.com/feeds/videos.xml?channel_id=\(channel)")!
    }

    static func video(_ id: String) async throws -> Video {
        let d = try await api("player", ["videoId": id])
        let status = d["playabilityStatus"] as? [String: Any]
        guard status?["status"] as? String == "OK" else {
            throw SparkError("YouTube 打不开这个视频：\(status?["reason"] as? String ?? "没说原因")")
        }
        let details = d["videoDetails"] as? [String: Any]
        let list = (d["captions"] as? [String: Any])?["playerCaptionsTracklistRenderer"] as? [String: Any]
        let tracks = list?["captionTracks"] as? [[String: Any]] ?? []
        return Video(id: id, title: details?["title"] as? String ?? id, author: details?["author"] as? String ?? "",
                     duration: Int(details?["lengthSeconds"] as? String ?? "") ?? 0, track: pick(tracks))
    }

    /// 上传者自己传的字幕优先（pipeline._pick_subtitle_track：质量最好），没有英文、中文的就用对原声做的自动字幕，再不然有什么用什么
    static func pick(_ tracks: [[String: Any]]) -> (url: String, lang: String, kind: String)? {
        let usable = tracks.compactMap { t -> (url: String, lang: String, auto: Bool)? in
            guard let u = t["baseUrl"] as? String, let l = t["languageCode"] as? String else { return nil }
            return (u, l, t["kind"] as? String == "asr")
        }
        let manual = usable.filter { !$0.auto }
        for p in ["en", "zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW"] {
            if let t = manual.first(where: { $0.lang == p || $0.lang.hasPrefix(p + "-") }) { return (t.url, t.lang, "manual") }
        }
        if let t = usable.first(where: \.auto) { return (t.url, t.lang, "auto") }
        return manual.first.map { ($0.url, $0.lang, "manual") }
    }

    static func captions(_ url: String) async throws -> [(Double, String)] {
        guard let u = URL(string: url + "&fmt=vtt") else { throw SparkError("字幕地址不对") }
        var req = URLRequest(url: u, timeoutInterval: 30)
        req.setValue(iosAgent, forHTTPHeaderField: "User-Agent")
        return paragraphs(vtt: String(decoding: try await URLSession.shared.data(for: req).0, as: UTF8.self))
    }

    /// 单个视频的发布日期：player 接口不给，从网页里找
    static func publishDate(_ id: String) async -> String {
        guard let u = URL(string: "https://www.youtube.com/watch?v=\(id)") else { return "" }
        var req = URLRequest(url: u, timeoutInterval: 20)
        req.setValue(webAgent, forHTTPHeaderField: "User-Agent")
        guard let data = try? await URLSession.shared.data(for: req).0 else { return "" }
        return Feed.date(Clipper.first(#""publishDate":"([^"]+)""#, String(decoding: data, as: UTF8.self)) ?? "")
    }

    // MARK: 字幕 → 段落（pipeline.vtt_to_paragraphs）

    private static func isCJK(_ c: Character?) -> Bool {
        guard let v = c?.unicodeScalars.first?.value else { return false }
        return (0x3400...0x9fff).contains(v) || (0xf900...0xfaff).contains(v) || (0x3000...0x303f).contains(v) || (0xff00...0xffef).contains(v)
    }

    private static func clean(_ raw: String) -> (String, Bool) {
        var s = HTMLToMarkdown.decodeEntities(raw.replacingOccurrences(of: #"<[^>]*>"#, with: "", options: .regularExpression))
        s = s.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        let speaker = s.hasPrefix(">>")
        if speaker { s = String(s.drop { $0 == ">" || $0 == " " }) }
        return (s, speaker)
    }

    private static func seconds(_ line: String) -> Double? {
        guard let g = line.range(of: #"(\d+):(\d{2}):(\d{2})[.,](\d{3})"#, options: .regularExpression) else {
            guard let s = line.range(of: #"(\d{2}):(\d{2})[.,](\d{3})"#, options: .regularExpression) else { return nil }
            let p = line[s].split(whereSeparator: { ":.,".contains($0) }).compactMap { Double($0) }
            return p.count == 3 ? p[0] * 60 + p[1] + p[2] / 1000 : nil
        }
        let p = line[g].split(whereSeparator: { ":.,".contains($0) }).compactMap { Double($0) }
        return p.count == 4 ? p[0] * 3600 + p[1] * 60 + p[2] + p[3] / 1000 : nil
    }

    static func paragraphs(vtt: String, maxChars: Int = 480, gap: Double = 4) -> [(Double, String)] {
        let lines = vtt.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var segments: [(Double, String, Bool)] = []
        var last: String?, i = 0
        while i < lines.count {
            guard lines[i].contains("-->"), let start = seconds(lines[i]) else { i += 1; continue }
            i += 1
            // 自动字幕是两行滚动：新一条的第一行就是上一条的第二行，跟上一行一样的跳过
            while i < lines.count, !lines[i].isEmpty {
                let (text, speaker) = clean(lines[i])
                if !text.isEmpty, text != last {
                    segments.append((start, text, speaker))
                    last = text
                }
                i += 1
            }
        }
        func join(_ parts: [String]) -> String {
            var out = ""
            for p in parts {
                out += out.isEmpty || (isCJK(out.last) && isCJK(p.first)) ? p : " " + p
            }
            return out
        }
        var out: [(Double, String)] = []
        var start = segments.first?.0 ?? 0, parts: [String] = [], len = 0
        var prev: Double?
        for (s, text, speaker) in segments {
            if !parts.isEmpty, speaker || (prev.map { s - $0 > gap } ?? false) || len > maxChars {
                out.append((start, join(parts)))
                start = s; parts = []; len = 0
            }
            parts.append(text)
            len += text.count + 1
            prev = s
        }
        if !parts.isEmpty { out.append((start, join(parts))) }
        return out
    }
}
