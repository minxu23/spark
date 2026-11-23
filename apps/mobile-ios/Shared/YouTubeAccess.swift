import Foundation

/// 手机上取 YouTube 视频信息时用哪几个 innertube 客户端、被要求「登录确认不是机器人」时怎么办。
/// 这里只放规则，不碰网络（tests/test_youtube_access.py 用 swiftc 编出来测）；发请求在 Captions.swift 的 YouTube.video。
///
/// 被要求登录验证（playabilityStatus 是 LOGIN_REQUIRED，原话 "Sign in to confirm you're not a bot"）多半是同一网络
/// 短时间请求太多被临时拦下，跟视频本身无关，换个客户端常能打开。几个客户端都被拦：这一期不算处理过，
/// 下次检查还列出来再试；同时提示用户可以交给电脑（电脑能借用浏览器里的登录状态，见 apps/summit2md/pipeline.py）。
enum YouTubeAccess {
    struct Client {
        let label: String
        /// innertube 请求体里的 context.client
        let context: [String: Any]
        /// 请求头 X-YouTube-Client-Name 的数字
        let number: Int
        let agent: String
        var version: String { context["clientVersion"] as? String ?? "" }
    }

    /// 依次试的客户端。第一个是平时用的 IOS，被要求登录验证才往后换。三个的参数都照 yt-dlp 的 INNERTUBE_CLIENTS
    /// （yt_dlp/extractor/youtube/_base.py；本机装的 2026.10.26 版，和 2026-11-19 的 master 7940e14e 里这三项一样）：
    /// IOS 从原来的 20.10.4 升到 21.26.4；visionos 是 yt-dlp 没有 JS 运行时时的默认客户端，android_vr 不要 JS 播放器。
    /// 2026-11-23 在电脑上对两个公开视频实测：IOS 21.26.4 都回 OK、有字幕
    static let clients: [Client] = [
        Client(label: "ios",
               context: ["clientName": "IOS", "clientVersion": "21.26.4", "deviceMake": "Apple", "deviceModel": "iPhone16,2",
                         "osName": "iPhone", "osVersion": "18.3.2.22D82"],
               number: 5,
               agent: "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"),
        Client(label: "visionos",
               context: ["clientName": "VISIONOS", "clientVersion": "1.02", "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
                         "osName": "visionOS", "osVersion": "26.5.23O471"],
               number: 101,
               agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
        Client(label: "android_vr",
               context: ["clientName": "ANDROID_VR", "clientVersion": "1.65.10", "deviceMake": "Oculus", "deviceModel": "Quest 3",
                         "androidSdkVersion": 32, "osName": "Android", "osVersion": "12L"],
               number: 28,
               agent: "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"),
    ]

    /// 是不是「要求登录验证」的回应：状态 LOGIN_REQUIRED，或者原话里有 not a bot / Sign in to confirm
    /// （电脑上 pipeline.is_youtube_bot_check 认的是同两句）。撇号可能是 ’，不按整句比
    static func isLoginCheck(status: String?, reason: String?) -> Bool {
        let r = (reason ?? "").lowercased()
        if r.contains("not a bot") || r.contains("sign in to confirm") { return true }
        return status == "LOGIN_REQUIRED" && (r.contains("sign in") || r.contains("bot"))
    }

    /// 一个客户端的回应怎么办：ok 用它；tryNext 换下一个；stop 照原样报（视频真的打不开：私享、删了、地区限制……）。
    /// 第一个客户端被拦之后，后面客户端别的出错（有的客户端看不了「儿童内容」）也换下一个，最后按被拦报。
    /// 后备客户端回 OK 但没有字幕轨道：不算「没有字幕」（可能只是这个客户端不给），接着换；都没有就按被拦报。
    /// 只有平时的客户端（primary）自己回 OK、没有字幕，才算这个视频没有字幕。needTracks 为 false（只要频道名）时有 OK 就行
    enum Step: Equatable { case ok, tryNext, stop }

    static func step(status: String?, reason: String?, blockedSoFar: Bool, primary: Bool = true, hasTracks: Bool = true,
                     needTracks: Bool = true) -> Step {
        if status == "OK" { return primary || hasTracks || !needTracks ? .ok : .tryNext }
        if isLoginCheck(status: status, reason: reason) || blockedSoFar { return .tryNext }
        return .stop
    }

    // MARK: 请求配置

    /// YouTube 专用会话的配置：ephemeral，不存也不带 cookies（没有 cookie 存储、不往请求里放、一律不收），
    /// 不用缓存、不用存着的凭据。手机上一旦被判成机器人，共享 cookie 存储里的访客身份就会跟着之后每次请求，
    /// 换客户端也没用（怀疑，电脑上没能复现）；这样每次都是干净的访客
    static func sessionConfiguration() -> URLSessionConfiguration {
        let c = URLSessionConfiguration.ephemeral
        c.httpCookieStorage = nil
        c.httpShouldSetCookies = false
        c.httpCookieAcceptPolicy = .never
        c.urlCredentialStorage = nil
        c.urlCache = nil
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        return c
    }

    /// 要从 App 共享 cookie 存储里清掉的：youtube.com、google.com 和它们的子域（cookie 的 domain 可能带开头的点）
    static func isYouTubeCookieDomain(_ domain: String) -> Bool {
        let d = (domain.hasPrefix(".") ? String(domain.dropFirst()) : domain).lowercased()
        return ["youtube.com", "google.com"].contains { d == $0 || d.hasSuffix("." + $0) }
    }

    // MARK: 一次检查里连着被拦

    /// 一次检查里连着这么多期被几个客户端全拦：这一次不再请求 YouTube 的 player 接口，剩下的 YouTube 期记进再试名单
    static let maxBlockedInRow = 3

    /// 处理完一期 YouTube 的节目后，连着被拦的期数：被拦加一；别的结果（写好、没字幕、跳过、出错——都已经打开了视频）清零。
    /// 不是 YouTube 的期不调这个，不影响计数
    static func blockedStreak(_ previous: Int, after s: Settle) -> Int {
        s == .blocked ? previous + 1 : 0
    }

    /// 这一期 YouTube 的节目还请不请求 player 接口
    static func shouldRequest(blockedInRow: Int) -> Bool { blockedInRow < maxBlockedInRow }

    /// 几个客户端都被要求登录验证
    struct LoginRequired: LocalizedError {
        /// YouTube 的原话
        let reason: String
        /// 是单独贴的链接（不是检查订阅）
        var link = false
        var errorDescription: String? { YouTubeAccess.message(reason: reason, link: link) }
    }

    /// 给用户看的话：先说发生了什么，再说接下来会怎样，YouTube 的原话放在最后
    static func message(reason: String, link: Bool) -> String {
        var s = "YouTube 要求登录验证（确认不是机器人），手机上换了几种方式都打不开这个视频。"
        s += link ? "这个链接没处理；可以点「交给电脑处理」，电脑能用浏览器里的登录状态。"
                  : "这一期没记成已处理，下次检查还会再试；也可以点「交给电脑处理」，电脑能用浏览器里的登录状态。"
        let r = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        if !r.isEmpty { s += "（YouTube 原话：\(r)）" }
        return s
    }

    // MARK: 一期处理完以后记不记、下次还列不列（LocalJobs.runFeeds 按 disposition 分）

    enum Settle: Equatable {
        case written        // 写好了
        case skippedShort   // 订阅设了跳过短视频
        case noText         // 没有字幕或文字稿：提示用户，要不要交给电脑由用户定
        case blocked        // YouTube 要求登录验证，几个客户端都被拦
        case failed         // 出错（模型、网络）
    }

    struct Disposition: Equatable {
        /// 记进处理记录或「不再列」的名单：以后检查不再列这一期
        var settled: Bool
        /// 下次检查即使比已处理的最新一期还旧也照样列（被拦的那期，后面更新的一期先写好了，它也不会漏掉）
        var retry: Bool
        /// 任务卡上给「交给电脑处理」
        var offerHandOff: Bool
    }

    static func disposition(_ s: Settle) -> Disposition {
        switch s {
        case .written: Disposition(settled: true, retry: false, offerHandOff: false)
        case .skippedShort: Disposition(settled: true, retry: false, offerHandOff: false)
        case .noText: Disposition(settled: false, retry: false, offerHandOff: true)
        case .blocked: Disposition(settled: false, retry: true, offerHandOff: true)
        case .failed: Disposition(settled: false, retry: false, offerHandOff: false)
        }
    }

    /// 「下次还要再试」的名单：这次被拦的加进去，这次有了结果的（写好了、跳过、没字幕、交给了电脑）拿掉。最多留 maxRetry 个
    static let maxRetry = 200

    static func nextRetry(_ current: [String], blocked: [String], resolved: [String]) -> [String] {
        let done = Set(resolved)
        var out = current.filter { !done.contains($0) }
        for id in blocked where !done.contains(id) && !out.contains(id) { out.append(id) }
        return Array(out.suffix(maxRetry))
    }

    /// 检查订阅时这一期列不列：没处理过、没在「不再列」里，并且比已处理的最新一期新、处理失败过、或者在再试名单里
    static func listed(processed: Bool, skipped: Bool, newer: Bool, failedBefore: Bool, retry: Bool) -> Bool {
        !processed && !skipped && (newer || failedBefore || retry)
    }
}
