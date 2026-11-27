"""手机样例库模式（apps/mobile-ios/Shared/Fixture.swift）：扫 App 源码，保证样例库模式下不联网、不碰真实存储。

联网：Spark/、Shared/、SparkShare/ 里每一处发请求、开连接、加载网页的用法，要么所在的函数里过了关口
（NetGate.check() / NetGate.blocked），要么在下面带理由的豁免清单里。新加一处没登记的用法，这个测试就失败。
存储：每一处取 Application Support、Caches、App Group 容器、App Group 的 UserDefaults、钥匙串的地方，
要么经过 Fixture 换位置，要么在函数里先看了 Fixture.isOn。
只读仓库里的源码，不读笔记库。"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "apps", "mobile-ios")
DIRS = ["Spark", "Shared", "SparkShare"]

# 发请求、开连接、加载网页的写法
NET = re.compile(
    r"URLSession\b|URLRequest\b|NWListener\b|NWConnection\b|NWBrowser\b|NWPathMonitor\b|WKWebView\b"
    r"|\.loadHTMLString\(|\.loadFileURL\(|\.load\(URLRequest|\.data\((?:for|from):|\.download\((?:for|from):"
    r"|\.upload\((?:for|from):|\.bytes\((?:for|from):|\b(?:data|download|upload|webSocket|stream)Task\("
    r"|CTFontDescriptorMatchFontDescriptorsWithProgressHandler|UIApplication\.shared\.open\b"
    r"|(?<![A-Za-z])Link\(|(?:Data|String)\(contentsOf:\s*URL\(string:|MultipeerConnectivity|MCSession\b|NetService\b"
    r"|CFStreamCreatePairWithSocket|\bsocket\(")

GATE = re.compile(r"NetGate\.(?:check\(\)|blocked)")

# 「文件:函数」→ 为什么不用过关口。函数是用法所在的最近一层 func / init / URLSession 类型的属性
EXEMPT = {
    "Shared/GitHub.swift:session": "只是存着会话的属性声明，请求都走 request()，那里过了关口",
    "Shared/GitHub.swift:init": "只建会话、不发请求；请求都走 request()，那里过了关口",
    "Shared/Captions.swift:session": "只建会话、不发请求；用它的 api()、playlistEntries()、captions()、publishDate()、FeedParser.fetch 都过了关口",
    "Spark/Network.swift:*": "NWPathMonitor 只看网络状态（Wi-Fi / 流量），不发任何数据",
    "Spark/MathArticle.swift:makeUIView": "公式页的 WKWebView：样例库模式下换成不落盘的数据存储；页面只读 App 里的 KaTeX 和笔记库文件（sparkdoc:），"
                                          "样例库模式下另加 Content-Security-Policy，网络图片不加载（见 test_math_page_offline）",
    "Spark/MathArticle.swift:updateUIView": "loadHTMLString 加载的是本地拼好的网页，baseURL 是 sparkdoc://vault/，不是网址",
    "Spark/MathArticle.swift:dismantleUIView": "只拆掉脚本消息处理，不加载东西",
    "Spark/MathArticle.swift:webView": "WKURLSchemeHandler / 导航回调：sparkdoc: 只读 App 包和笔记库里的文件；"
                                       "点正文链接交给系统浏览器打开，不是 App 发的请求",
}
# 某种写法在哪都不用过关口
EXEMPT_TOKEN = {
    "Link(": "SwiftUI 的 Link 交给系统浏览器打开网址，不是 App 发的请求（样例库里的链接都是 example.com）",
    "UIApplication.shared.open": "交给系统浏览器打开网址，不是 App 发的请求",
}

DECL = re.compile(r"\b(?:func\s+(\w+)|(init)[?!]?\s*[(<]|(?:let|var)\s+(\w+)\s*:\s*URLSession\b)")


def swift_files():
    for d in DIRS:
        base = os.path.join(IOS, d)
        for name in sorted(os.listdir(base)):
            if name.endswith(".swift"):
                yield f"{d}/{name}", os.path.join(base, name)


def code_part(line):
    """去掉行尾注释、整行注释和字符串里的内容（粗略：够认写法用）"""
    s = line.strip()
    if s.startswith("//") or s.startswith("///") or s.startswith("*"):
        return ""
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    i = s.find("//")
    return s[:i] if i >= 0 else s


def indent(line):
    return len(line) - len(line.lstrip(" "))


def contexts(lines, i):
    """用法外面的各层声明，从里到外：[(名字, 声明所在行号)]。用法这一行本身是声明就从它开始；
    往上找时只认缩进比途经各行都浅的 func / init / URLSession 属性（跳过已经结束的同级函数）"""
    out = []
    m = DECL.search(code_part(lines[i]))
    if m:
        out.append((next(g for g in m.groups() if g), i))
    lim = indent(lines[i])
    for j in range(i - 1, -1, -1):
        c = code_part(lines[j])
        if not c.strip():
            continue
        m = DECL.search(c)
        if m and indent(lines[j]) < lim:
            out.append((next(g for g in m.groups() if g), j))
        lim = min(lim, indent(lines[j]))
    return out or [("<top>", 0)]


def context(lines, i):
    return contexts(lines, i)[0]


def body_end(lines, j):
    """声明 j 的函数体到哪一行为止：下一行缩进不深于声明、且以 } 开头的地方"""
    base = indent(lines[j])
    for k in range(j + 1, len(lines)):
        s = lines[k]
        if s.strip() and indent(s) <= base and s.strip().startswith("}"):
            return k
    return len(lines) - 1


def net_uses():
    out = []
    for rel, path in swift_files():
        if rel == "Shared/Fixture.swift":
            continue   # 开关自己：只有兜底的 URLProtocol（不发请求，拦请求）
        lines = open(path, encoding="utf-8").read().split("\n")
        for i, line in enumerate(lines):
            c = code_part(line)
            for m in NET.finditer(c):
                out.append((rel, i, m.group(0), lines))
    return out


def test_every_network_use_is_gated_or_exempt():
    problems, used = [], set()
    for rel, i, tok, lines in net_uses():
        if tok in EXEMPT_TOKEN:
            continue
        chain = contexts(lines, i)
        name, j = chain[0]
        hit = next((k for k in (f"{rel}:{name}", f"{rel}:*") if k in EXEMPT), None)
        if hit:
            used.add(hit)
            continue
        # 外面任何一层函数在用法之前过了关口都算（比如局部函数、闭包里发的请求，关口在外层函数开头）
        if any(GATE.search("\n".join(lines[k:i + 1])) for _, k in chain):
            continue
        if i == j and GATE.search("\n".join(lines[j:body_end(lines, j) + 1])):
            continue   # 声明行本身带 URLSession 参数：函数体里过了关口就行
        problems.append(f"{rel}:{i + 1} {tok!r}（在 {name} 里）没过 NetGate，也不在豁免清单里")
    assert not problems, "\n".join(problems)
    assert used == set(EXEMPT), f"豁免清单里有用不上的条目：{sorted(set(EXEMPT) - used)}"


def test_scan_finds_known_entry_points():
    """扫描本身别失灵：已知的几处联网入口都要被认出来，并且都过了关口"""
    found = {(rel, tok) for rel, _, tok, _ in net_uses()}
    for rel in ["Shared/GitHub.swift", "Spark/LLM.swift", "Shared/Clipper.swift", "Shared/XHS.swift", "Shared/XPost.swift",
                "Shared/FeedParser.swift", "Shared/Captions.swift", "Spark/FeedRunner.swift", "Spark/Markdown.swift",
                "Spark/ReadPrefsSheet.swift", "Spark/Vault.swift", "Spark/MathArticle.swift", "Spark/Fonts.swift"]:
        assert any(r == rel for r, _ in found), f"{rel} 里的联网用法没扫到"
    gates = {rel: len(GATE.findall(open(p, encoding="utf-8").read())) for rel, p in swift_files()}
    for rel, n in {"Shared/GitHub.swift": 1, "Spark/LLM.swift": 1, "Shared/Clipper.swift": 2, "Shared/XHS.swift": 1,
                   "Shared/XPost.swift": 1, "Shared/FeedParser.swift": 1, "Shared/Captions.swift": 4,
                   "Spark/FeedRunner.swift": 2, "Spark/Markdown.swift": 1, "Spark/ReadPrefsSheet.swift": 1,
                   "Spark/Vault.swift": 1, "Spark/Fonts.swift": 2}.items():
        assert gates.get(rel, 0) >= n, f"{rel} 的关口少了：{gates.get(rel, 0)} < {n}"


def test_scan_catches_a_new_ungated_use():
    """新加一处没过关口的请求，测试要能抓到"""
    fake = ["import Foundation", "enum Probe {", "    static func ping() async throws {",
            '        let (_, _) = try await URLSession.shared.data(from: URL(string: "https://example.com")!)', "    }", "}"]
    name, j = context(fake, 3)
    assert name == "ping"
    assert not GATE.search("\n".join(fake[j:4]))
    gated = fake[:3] + ["        try NetGate.check()"] + fake[3:]
    name, j = context(gated, 4)
    assert name == "ping" and GATE.search("\n".join(gated[j:5]))


def test_math_page_offline():
    """公式页：样例库模式加内容安全策略，只许 sparkdoc: 和 data:；网页数据不落盘"""
    src = open(os.path.join(IOS, "Spark", "MathArticle.swift"), encoding="utf-8").read()
    assert "Fixture.isOn ? fixtureCSP" in src
    csp = re.search(r'static let fixtureCSP = (.+?)\n\n', src, re.S).group(1)
    assert "default-src sparkdoc: data:" in csp and not re.search(r"\bhttps?:", csp)
    assert "if Fixture.isOn { config.websiteDataStore = .nonPersistent() }" in src
    # KaTeX 是打包进 App 的本地文件，不从网上拿
    katex = os.path.join(IOS, "Spark", "KaTeX")
    for name in os.listdir(katex):
        if name.endswith((".css", ".js")):
            text = open(os.path.join(katex, name), encoding="utf-8", errors="replace").read()
            urls = set(re.findall(r"https?://[^\s\"')]+", text))
            assert urls <= {"http://www.w3.org/1998/Math/MathML", "http://www.w3.org/2000/svg"}, (name, urls)


# ---------------------------------------------------------------- 存储

STORE_RULES = [
    # (写法, 同一行必须带的换位置写法)
    (re.compile(r"URL\.applicationSupportDirectory"), "Fixture.support(URL.applicationSupportDirectory)"),
    (re.compile(r"URL\.cachesDirectory"), "Fixture.caches(URL.cachesDirectory)"),
    (re.compile(r"containerURL\(forSecurityApplicationGroupIdentifier"), "Fixture.groupContainer("),
    (re.compile(r"UserDefaults\(suiteName:\s*groupID\)"), "Fixture.groupUserDefaults("),
]
STORE_EXEMPT = {
    # CrashLog.swift 测试时单独编译（tests/test_crash_log.py），不引 Fixture；样例库模式下 SparkMain 设 folderOverride
    ("Spark/CrashLog.swift", "FileManager.default.urls(for: .applicationSupportDirectory"),
}

# 论文 PDF 拷进「文件」App 的 Documents/论文：入口一开头就 if Fixture.isOn { return }，样例库模式不写
DOCUMENTS_EXEMPT = "Spark/PaperFilesExport.swift"


def test_every_storage_root_goes_through_fixture():
    problems = []
    for rel, path in swift_files():
        if rel == "Shared/Fixture.swift":
            continue
        for n, line in enumerate(open(path, encoding="utf-8").read().split("\n"), 1):
            c = code_part(line)
            for rx, need in STORE_RULES:
                if rx.search(c) and need not in c:
                    problems.append(f"{rel}:{n} 没经过 {need}")
            if ".applicationSupportDirectory" in c and "URL.applicationSupportDirectory" not in c:
                if not any(rel == r and frag in c for r, frag in STORE_EXEMPT):
                    problems.append(f"{rel}:{n} 用 FileManager 取 Application Support，没经过 Fixture")
            if re.search(r"\.(?:documentDirectory|libraryDirectory|documentsDirectory)\b", c) and rel != DOCUMENTS_EXEMPT:
                problems.append(f"{rel}:{n} 新的存储位置，样例库模式没有顶替")
    assert not problems, "\n".join(problems)
    app = open(os.path.join(IOS, "Spark", "SparkApp.swift"), encoding="utf-8").read()
    assert "CrashLog.folderOverride = Fixture.support(" in app


def test_keychain_is_off_in_fixture_mode():
    """钥匙串：每个调 SecItem 的函数里先看 Fixture.isOn"""
    problems = []
    for rel, path in swift_files():
        lines = open(path, encoding="utf-8").read().split("\n")
        for i, line in enumerate(lines):
            if re.search(r"\bSecItem\w+\(", code_part(line)):
                name, j = context_func(lines, i)
                if "Fixture.isOn" not in "\n".join(lines[j:i + 1]):
                    problems.append(f"{rel}:{i + 1} {name} 碰钥匙串前没看 Fixture.isOn")
    assert not problems, "\n".join(problems)


def context_func(lines, i):
    """SecItem 所在的函数"""
    lim = indent(lines[i])
    for j in range(i, -1, -1):
        c = code_part(lines[j])
        if not c.strip():
            continue
        m = re.search(r"\bfunc\s+(\w+)", c)
        if m and (j == i or indent(lines[j]) < lim):
            return m.group(1), j
        lim = min(lim, indent(lines[j]))
    return "<top>", 0


def test_switch_has_one_reader():
    """开关只有一个读取处：环境变量只在 Fixture.swift 里看，模式只定一次（currentMode）；
    拦下（isBlocked）和说明页只在模拟器 DEBUG 的 #if 里用"""
    for rel, path in swift_files():
        text = open(path, encoding="utf-8").read()
        if rel == "Shared/Fixture.swift":
            assert len(re.findall(r"static let currentMode\b", text)) == 1
            assert "static var isOn: Bool { currentMode == .fixture }" in text
            assert len(re.findall(r"static let isOn = false", text)) == 1   # #else：真机、Release 恒为 false
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if re.search(r"Fixture\.isBlocked|SimulatorGuard(?:App|View)\b", code_part(line)):
                conds = [l.strip() for l in lines[:i] if l.strip().startswith(("#if", "#endif"))]
                assert conds and conds[-1] == "#if DEBUG && targetEnvironment(simulator)", f"{rel}:{i + 1} 不在模拟器 DEBUG 的 #if 里"
        assert "SPARK_FIXTURE_DIR" not in text and "processInfo.environment" not in text, rel
        assert "Fixture.prepare()" not in text or rel == "Spark/SparkApp.swift", rel


def test_badge_takes_no_layout_and_no_touches():
    """样例库标记：浮层窗口，不用 safeAreaInset（不挤动页面、不改安全区），不接点击，层级在 sheet 之上"""
    src = open(os.path.join(IOS, "Spark", "FixtureBadge.swift"), encoding="utf-8").read()
    code = "\n".join(code_part(l) for l in src.split("\n"))
    assert ".safeAreaInset(" not in code and ".overlay(" not in code
    assert "w.isUserInteractionEnabled = false" in code and ".allowsHitTesting(false)" in code
    assert "w.windowLevel = .alert + 1" in code
    assert "makeKey" not in code
