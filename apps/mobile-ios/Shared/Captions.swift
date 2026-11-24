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
        /// 取到这份信息的客户端的 User-Agent：下字幕时照用
        var agent = YouTubeAccess.clients[0].agent
    }

    private static let webAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"

    /// YouTube 的所有请求（player、browse、resolve_url、订阅源、字幕、读网页）都走这个会话：不存也不带 cookies、
    /// 不用缓存和存着的凭据（YouTubeAccess.sessionConfiguration），每次都是干净的访客。
    /// 第一次用之前，把以前走 URLSession.shared 时 YouTube、Google 种在 App 共享 cookie 存储里的清一次
    static let session: URLSession = {
        clearOldSharedCookies()
        return URLSession(configuration: YouTubeAccess.sessionConfiguration())
    }()

    private static let clearedKey = "youtube.sharedCookiesCleared"

    /// App 里别处没有要用 youtube.com、google.com 这些 cookies 的（没有网页登录；Google Drive 同步走「文件」App，
    /// 公式页的 WKWebView 用自己的数据存储、只读本地文件），清掉不影响别的功能。只清一次
    private static func clearOldSharedCookies() {
        guard !Fixture.isOn else { return }   // 样例库模式：App 的 cookie 存储是真实的那份，不动
        guard !UserDefaults.standard.bool(forKey: clearedKey) else { return }
        let store = HTTPCookieStorage.shared
        for c in store.cookies ?? [] where YouTubeAccess.isYouTubeCookieDomain(c.domain) { store.deleteCookie(c) }
        UserDefaults.standard.set(true, forKey: clearedKey)
    }

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

    private static func api(_ path: String, _ body: [String: Any], client: YouTubeAccess.Client = YouTubeAccess.clients[0]) async throws -> [String: Any] {
        try NetGate.check()
        var req = URLRequest(url: URL(string: "https://www.youtube.com/youtubei/v1/\(path)?prettyPrint=false")!, timeoutInterval: 20)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(client.agent, forHTTPHeaderField: "User-Agent")
        // yt-dlp 也带这两个头（generate_api_headers）
        req.setValue("\(client.number)", forHTTPHeaderField: "X-YouTube-Client-Name")
        req.setValue(client.version, forHTTPHeaderField: "X-YouTube-Client-Version")
        var body = body
        body["context"] = ["client": client.context]
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await session.data(for: req)
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

    /// 播放列表的订阅地址有时候是 404（Training Data 就是）：改用网页版的列表接口，给得出视频 id 和标题，给不出日期。
    /// 没有标题的条目（YouTubeAccess.isUntitled：私享、已删除、还没公开的）不列，untitled 是跳过了几个
    static func playlistEntries(_ url: String) async throws -> (entries: [Feed.Entry], untitled: Int) {
        guard let list = URLComponents(string: url)?.queryItems?.first(where: { $0.name == "list" })?.value else { throw SparkError("不是播放列表") }
        try NetGate.check()
        var req = URLRequest(url: URL(string: "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false")!, timeoutInterval: 25)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(webAgent, forHTTPHeaderField: "User-Agent")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["browseId": "VL\(list)", "context": ["client": ["clientName": "WEB", "clientVersion": "2.20260101.00.00", "hl": "en"]]])
        let root = try JSONSerialization.jsonObject(with: try await session.data(for: req).0)
        var out: [Feed.Entry] = []
        var untitled: Set<String> = []
        func walk(_ o: Any) {
            if let d = o as? [String: Any] {
                if let v = d["lockupViewModel"] as? [String: Any], v["contentType"] as? String == "LOCKUP_CONTENT_TYPE_VIDEO",
                   let id = v["contentId"] as? String {
                    let title = (((v["metadata"] as? [String: Any])?["lockupMetadataViewModel"] as? [String: Any])?["title"] as? [String: Any])?["content"] as? String
                    if YouTubeAccess.isUntitled(title) {
                        untitled.insert(id)
                    } else if !out.contains(where: { $0.id == id }) {
                        out.append(Feed.Entry(id: id, title: title ?? id, url: "https://www.youtube.com/watch?v=\(id)", videoID: id))
                    }
                    return
                }
                d.values.forEach(walk)
            } else if let a = o as? [Any] { a.forEach(walk) }
        }
        walk(root)
        // 整个列表都是没有标题的：不算空列表报错，照「跳过了几个」说
        if out.isEmpty && untitled.isEmpty { throw SparkError("这个播放列表是空的") }
        return (out, untitled.count)
    }

    /// 视频信息和字幕轨道。被要求登录验证（「确认不是机器人」）时依次换 YouTubeAccess.clients 里别的客户端；
    /// 都被拦抛 YouTubeAccess.LoginRequired（调用方按它把这一期留着下次再试、提示可以交给电脑）。
    /// 视频本身打不开（私享、删了）照原样报。后备客户端回 OK 但没有字幕轨道时接着换（YouTubeAccess.step）；
    /// needTracks 为 false（只要频道名、时长）时有 OK 就用
    static func video(_ id: String, link: Bool = false, needTracks: Bool = true) async throws -> Video {
        var blocked = false, reason = ""
        for (i, client) in YouTubeAccess.clients.enumerated() {
            let d: [String: Any]
            do {
                d = try await api("player", ["videoId": id], client: client)
            } catch {
                // 第一个就出错（断网、超时）照原样报；被拦之后换的客户端出错，接着试下一个
                if !blocked || error is CancellationError { throw error }
                continue
            }
            let status = d["playabilityStatus"] as? [String: Any]
            let st = status?["status"] as? String, why = status?["reason"] as? String
            let video = parse(id, d, agent: client.agent)
            switch YouTubeAccess.step(status: st, reason: why, blockedSoFar: blocked, primary: i == 0,
                                      hasTracks: video.track != nil, needTracks: needTracks) {
            case .ok: return video
            case .tryNext:
                if YouTubeAccess.isLoginCheck(status: st, reason: why) {
                    blocked = true
                    if reason.isEmpty { reason = why ?? "" }
                }
            case .stop: throw SparkError("YouTube 打不开这个视频：\(why ?? "没说原因")")
            }
        }
        throw YouTubeAccess.LoginRequired(reason: reason, link: link)
    }

    private static func parse(_ id: String, _ d: [String: Any], agent: String) -> Video {
        let details = d["videoDetails"] as? [String: Any]
        let list = (d["captions"] as? [String: Any])?["playerCaptionsTracklistRenderer"] as? [String: Any]
        let tracks = list?["captionTracks"] as? [[String: Any]] ?? []
        return Video(id: id, title: details?["title"] as? String ?? id, author: details?["author"] as? String ?? "",
                     duration: Int(details?["lengthSeconds"] as? String ?? "") ?? 0, track: pick(tracks), agent: agent)
    }

    /// 选字幕，跟 pipeline._pick_subtitle_track 同一个顺序：上传者自己传的英文、中文字幕，再是英文、中文的
    /// 自动字幕，都没有才有什么用什么。开了 AI 配音的视频每种配音语言都有一条自动字幕，第一条可能是阿拉伯语
    static func pick(_ tracks: [[String: Any]]) -> (url: String, lang: String, kind: String)? {
        let usable = tracks.compactMap { t -> (url: String, lang: String, auto: Bool)? in
            guard let u = t["baseUrl"] as? String, let l = t["languageCode"] as? String else { return nil }
            return (u, l, t["kind"] as? String == "asr")
        }
        func preferred(_ list: [(url: String, lang: String, auto: Bool)]) -> (url: String, lang: String, auto: Bool)? {
            for p in ["en", "zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW"] {
                if let t = list.first(where: { $0.lang == p || $0.lang.hasPrefix(p + "-") }) { return t }
            }
            return nil
        }
        let manual = usable.filter { !$0.auto }, auto = usable.filter(\.auto)
        if let t = preferred(manual) { return (t.url, t.lang, "manual") }
        if let t = preferred(auto) ?? auto.first { return (t.url, t.lang, "auto") }
        return manual.first.map { ($0.url, $0.lang, "manual") }
    }

    static func captions(_ url: String, agent: String = YouTubeAccess.clients[0].agent) async throws -> [(Double, String)] {
        guard let u = URL(string: url + "&fmt=vtt") else { throw SparkError("字幕地址不对") }
        try NetGate.check()
        var req = URLRequest(url: u, timeoutInterval: 30)
        req.setValue(agent, forHTTPHeaderField: "User-Agent")
        return paragraphs(vtt: String(decoding: try await session.data(for: req).0, as: UTF8.self))
    }

    /// 单个视频的发布时间：player 接口不给，从网页里找（publishDate 带时差时连时刻一起记）。
    /// 返回 (yyyyMMdd，本机时区的那天；ISO 时刻，只知道哪天时是空串)
    static func publishDate(_ id: String) async -> (date: String, at: String) {
        guard let u = URL(string: "https://www.youtube.com/watch?v=\(id)"), !NetGate.blocked else { return ("", "") }
        var req = URLRequest(url: u, timeoutInterval: 20)
        req.setValue(webAgent, forHTTPHeaderField: "User-Agent")
        guard let data = try? await session.data(for: req).0 else { return ("", "") }
        return Feed.stamp(Clipper.first(#""publishDate":"([^"]+)""#, String(decoding: data, as: UTF8.self)) ?? "")
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
