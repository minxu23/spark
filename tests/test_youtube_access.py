"""手机上 YouTube 要求登录验证（「Sign in to confirm you're not a bot」）时的规则（apps/mobile-ios/Shared/YouTubeAccess.swift）：
用 swiftc 单独编出来跑。认不认得出这类回应、换不换客户端、这一期记不记成已处理、下次还列不列、给用户的话。"""

import json
import os
import shutil
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Shared", "YouTubeAccess.swift")

MAIN = r'''
import Foundation
var out: [String: Any] = [:]
// 认得出：直撇号、弯撇号、只有状态没有原话的不算；年龄限制的 Sign in to confirm your age 也算（要登录）
out["check"] = [
    YouTubeAccess.isLoginCheck(status: "LOGIN_REQUIRED", reason: "Sign in to confirm you're not a bot"),
    YouTubeAccess.isLoginCheck(status: "LOGIN_REQUIRED", reason: "Sign in to confirm you’re not a bot"),
    YouTubeAccess.isLoginCheck(status: "UNPLAYABLE", reason: "Sign in to confirm you’re not a bot"),
    YouTubeAccess.isLoginCheck(status: "LOGIN_REQUIRED", reason: "Sign in to confirm your age"),
    YouTubeAccess.isLoginCheck(status: "LOGIN_REQUIRED", reason: nil),
    YouTubeAccess.isLoginCheck(status: "UNPLAYABLE", reason: "Video unavailable"),
    YouTubeAccess.isLoginCheck(status: "ERROR", reason: "This video is private"),
    YouTubeAccess.isLoginCheck(status: "OK", reason: nil),
]
func st(_ s: YouTubeAccess.Step) -> String { "\(s)" }
out["step"] = [
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: false)),
    st(YouTubeAccess.step(status: "LOGIN_REQUIRED", reason: "Sign in to confirm you’re not a bot", blockedSoFar: false)),
    st(YouTubeAccess.step(status: "ERROR", reason: "This video is private", blockedSoFar: false)),
    // 第一个被拦之后，别的客户端看不了（儿童内容、要刷新页面）：接着换，最后按被拦报
    st(YouTubeAccess.step(status: "UNPLAYABLE", reason: "The page needs to be reloaded.", blockedSoFar: true)),
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: true)),
    // 平时的客户端回 OK 没字幕：就是没有字幕；后备客户端回 OK 没字幕：接着换；有字幕：用；只要频道名时有 OK 就用
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: false, primary: true, hasTracks: false)),
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: true, primary: false, hasTracks: false)),
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: true, primary: false, hasTracks: true)),
    st(YouTubeAccess.step(status: "OK", reason: nil, blockedSoFar: true, primary: false, hasTracks: false, needTracks: false)),
]
// 连着被拦：被拦加一，别的结果清零；到 maxBlockedInRow 就不再请求
var streak = 0, trace: [String] = []
for s in [YouTubeAccess.Settle.blocked, .blocked, .written, .blocked, .blocked, .blocked, .noText, .blocked, .failed] {
    streak = YouTubeAccess.blockedStreak(streak, after: s)
    trace.append("\(streak):\(YouTubeAccess.shouldRequest(blockedInRow: streak))")
}
out["streak"] = trace
out["maxBlockedInRow"] = YouTubeAccess.maxBlockedInRow
out["clients"] = YouTubeAccess.clients.map { "\($0.label)|\($0.context["clientName"] as? String ?? "")|\($0.version)|\($0.number)" }
out["agents"] = YouTubeAccess.clients.map(\.agent)
func d(_ s: YouTubeAccess.Settle) -> String {
    let x = YouTubeAccess.disposition(s)
    return "\(x.settled)|\(x.retry)|\(x.offerHandOff)"
}
out["disposition"] = [d(.written), d(.skippedShort), d(.noText), d(.blocked), d(.failed)]
// 再试名单：被拦的加进去；写好了、跳过、交给电脑的拿掉；同一次里又被拦又有了结果的算有了结果；不重复；最多 200 个
out["retry1"] = YouTubeAccess.nextRetry([], blocked: ["a", "b"], resolved: [])
out["retry2"] = YouTubeAccess.nextRetry(["a", "b"], blocked: ["c", "a"], resolved: ["b"])
out["retry3"] = YouTubeAccess.nextRetry(["a"], blocked: ["x"], resolved: ["x", "a"])
let many = (0..<250).map { "v\($0)" }
let capped = YouTubeAccess.nextRetry([], blocked: many, resolved: [])
out["retryCap"] = [capped.count, capped.first ?? "", capped.last ?? ""] as [Any]
// 列不列：比已处理的最新一期旧，但在再试名单里：照列；处理过的、在「不再列」里的：不列
func l(_ p: Bool, _ s: Bool, _ n: Bool, _ f: Bool, _ r: Bool) -> Bool {
    YouTubeAccess.listed(processed: p, skipped: s, newer: n, failedBefore: f, retry: r)
}
out["listed"] = [l(false, false, true, false, false), l(false, false, false, false, false), l(false, false, false, false, true),
                 l(false, false, false, true, false), l(true, false, true, false, true), l(false, true, true, false, true)]
let cfg = YouTubeAccess.sessionConfiguration()
out["session"] = [cfg.httpCookieStorage == nil ? "nil" : "store", "\(cfg.httpShouldSetCookies)",
                  cfg.httpCookieAcceptPolicy == .never ? "never" : "accepts", cfg.urlCredentialStorage == nil ? "nil" : "store",
                  cfg.urlCache == nil ? "nil" : "cache", cfg.requestCachePolicy == .reloadIgnoringLocalCacheData ? "reloadIgnoringLocalCacheData" : "other"]
out["cookieDomain"] = [".youtube.com", "www.youtube.com", "youtube.com", ".google.com", "accounts.google.com",
                       "notyoutube.com", ".google.co.uk", "api.github.com"].map(YouTubeAccess.isYouTubeCookieDomain)
out["msgFeed"] = YouTubeAccess.LoginRequired(reason: "Sign in to confirm you’re not a bot").localizedDescription
out["msgLink"] = YouTubeAccess.LoginRequired(reason: "", link: true).localizedDescription
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


@pytest.mark.skipif(not shutil.which("swiftc"), reason="没有 swiftc")
def test_youtube_access_swift(tmp_path):
    main = tmp_path / "main.swift"
    main.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), SRC, str(main)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    got = json.loads(subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout)
    assert got["check"] == [True, True, True, True, False, False, False, False]
    assert got["step"] == ["ok", "tryNext", "stop", "tryNext", "ok", "ok", "tryNext", "ok", "ok"]
    assert got["maxBlockedInRow"] == 3
    assert got["streak"] == ["1:true", "2:true", "0:true", "1:true", "2:true", "3:false", "0:true", "1:true", "0:true"]
    # 平时用 IOS 21.26.4；被拦才换 visionos、android_vr（参数都照 yt-dlp 2026.10.26 的 INNERTUBE_CLIENTS）
    assert got["clients"] == ["ios|IOS|21.26.4|5", "visionos|VISIONOS|1.02|101", "android_vr|ANDROID_VR|1.65.10|28"]
    assert got["agents"][0] == "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"
    assert "oculus/1.65.10" in got["agents"][2]
    # 写好了、跳过：记下；没字幕：不记、不再试、给「交给电脑」；被拦：不记、下次再试、给「交给电脑」；出错：不记、不再试
    assert got["disposition"] == ["true|false|false", "true|false|false", "false|false|true", "false|true|true", "false|false|false"]
    # YouTube 专用会话：不存、不带、不收 cookies，不用缓存和存着的凭据
    assert got["session"] == ["nil", "false", "never", "nil", "nil", "reloadIgnoringLocalCacheData"]
    assert got["cookieDomain"] == [True, True, True, True, True, False, False, False]
    assert got["retry1"] == ["a", "b"]
    assert got["retry2"] == ["a", "c"]
    assert got["retry3"] == []
    assert got["retryCap"] == [200, "v50", "v249"], "最多留 200 个，留最近的"
    assert got["listed"] == [True, False, True, True, False, False]
    # 中文说清楚发生了什么、接下来会怎样，YouTube 原话放在最后
    feed = got["msgFeed"]
    assert feed.startswith("YouTube 要求登录验证") and "没记成已处理，下次检查还会再试" in feed and "交给电脑处理" in feed
    assert feed.endswith("（YouTube 原话：Sign in to confirm you’re not a bot）")
    link = got["msgLink"]
    assert "这个链接没处理" in link and "下次检查" not in link and "原话" not in link
