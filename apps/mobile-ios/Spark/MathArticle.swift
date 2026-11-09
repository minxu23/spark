import SwiftUI
import WebKit

/// 带公式的文章：原生的 Text 排不了公式，整篇正文换成网页，用打包进 App 的 KaTeX 排版（跟电脑阅读页同一套）。
/// 认公式的规则跟 apps/reader/server.py 的 _take_math 一字不差（tests/test_mobile_math_parity.py 对比）。
enum MathHTML {
    // MARK: 认公式

    static let pattern = #"(?<code>^```.*?^```[ \t]*$|`[^`\n]+`)|\$\$(?<block>[^$]+?)\$\$|(?<![\w.$\\])\$(?<inline>[^\s$](?:[^$\n]{0,298}[^\s$\\])?)\$(?![\w$])"#
    private static let re = try! NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators, .anchorsMatchLines])
    private static let mathy = #"[\\^_={]"#
    private static let shortVar = #"^[A-Za-z0-9 ,+\-*/<>()|.']{1,10}$"#

    static func looksMath(_ tex: String) -> Bool {
        if tex.range(of: #"[\x{4e00}-\x{9fff}]"#, options: .regularExpression) != nil { return false }
        if tex.range(of: mathy, options: .regularExpression) != nil { return true }
        return tex.range(of: shortVar, options: .regularExpression) != nil && tex.range(of: "[A-Za-z]{3}", options: .regularExpression) == nil
    }

    /// 公式换成占位符 SPARKMATH0X……，返回换好的文字和公式（是否单独成块、公式内容）
    static func take(_ body: String) -> (String, [(display: Bool, tex: String)]) {
        let ns = body as NSString
        var out = "", maths: [(display: Bool, tex: String)] = [], last = 0
        for m in re.matches(in: body, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            last = m.range.location + m.range.length
            let whole = ns.substring(with: m.range)
            func group(_ name: String) -> String? {
                let r = m.range(withName: name)
                return r.location == NSNotFound ? nil : ns.substring(with: r)
            }
            if group("code") != nil { out += whole; continue }
            let block = group("block")
            let tex = block ?? group("inline") ?? ""
            if tex.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || tex.contains("\n\n") || (block == nil && !looksMath(tex)) {
                out += whole
                continue
            }
            maths.append((block != nil, tex.trimmingCharacters(in: .whitespacesAndNewlines)))
            let key = "SPARKMATH\(maths.count - 1)X"
            if block != nil, alone(ns, m.range) { out += "\n\n\(key)\n\n" } else { out += key }
        }
        out += ns.substring(from: last)
        return (out, maths)
    }

    private static func alone(_ s: NSString, _ r: NSRange) -> Bool {
        let before = s.range(of: "\n", options: .backwards, range: NSRange(location: 0, length: r.location))
        let start = before.location == NSNotFound ? 0 : before.location + 1
        let afterStart = r.location + r.length
        let after = s.range(of: "\n", range: NSRange(location: afterStart, length: s.length - afterStart))
        let end = after.location == NSNotFound ? s.length : after.location
        let head = s.substring(with: NSRange(location: start, length: r.location - start))
        let tail = s.substring(with: NSRange(location: afterStart, length: end - afterStart))
        return head.trimmingCharacters(in: .whitespaces).isEmpty && tail.trimmingCharacters(in: .whitespaces).isEmpty
    }

    static func hasMath(_ blocks: [MDBlock]) -> Bool {
        blocks.contains { b in
            switch b {
            case .heading(_, let t), .paragraph(let t), .bullet(let t, _, _), .quote(let t): !take(t).1.isEmpty
            case .table(let h, let rows): !take((h + rows.flatMap { $0 }).joined(separator: "\n")).1.isEmpty
            default: false
            }
        }
    }

    // MARK: 拼网页

    private static func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }

    private static func mathHTML(_ m: (display: Bool, tex: String)) -> String {
        m.display ? "<div class=\"math\">$$\(esc(m.tex))$$</div>" : "<span class=\"math\">\\(\(esc(m.tex))\\)</span>"
    }

    /// 行内的粗体、斜体、链接、图片、行内代码、高亮；公式和行内代码先换成占位符，里面的 * _ 不动
    static func inline(_ s: String) -> String {
        let (text, maths) = take(s)
        var codes: [String] = []
        var t = RX.sub(#"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]"#, "$2", text)   // [[笔记|别名]] 只留字
        t = (try! NSRegularExpression(pattern: "`([^`\n]+)`")).matches(in: t, range: NSRange(location: 0, length: (t as NSString).length))
            .reversed().reduce(t) { acc, m in
                let ns = acc as NSString
                codes.append(ns.substring(with: m.range(at: 1)))
                return ns.replacingCharacters(in: m.range, with: "SPARKCODE\(codes.count - 1)X")
            }
        t = esc(t)
        t = RX.sub(#"!\[([^\]]*)\]\(&lt;?([^)\s]+?)(?:&gt;)?(?:\s+&quot;[^)]*&quot;)?\)"#, #"<img alt="$1" src="$2">"#, t)
        t = RX.sub(#"\[([^\]]+)\]\(&lt;?([^)\s]+?)(?:&gt;)?(?:\s+&quot;[^)]*&quot;)?\)"#, #"<a href="$2">$1</a>"#, t)
        t = RX.sub(#"\*\*(?=\S)(.+?)(?<=\S)\*\*"#, "<strong>$1</strong>", t)
        t = RX.sub(#"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])"#, "<em>$1</em>", t)
        t = RX.sub("~~(.+?)~~", "<del>$1</del>", t)
        t = RX.sub("==(.+?)==", "<mark>$1</mark>", t)
        for (i, c) in codes.enumerated() { t = t.replacingOccurrences(of: "SPARKCODE\(i)X", with: "<code>\(esc(c))</code>") }
        for (i, m) in maths.enumerated() { t = t.replacingOccurrences(of: "SPARKMATH\(i)X", with: mathHTML(m)) }
        return t
    }

    static func body(_ blocks: [MDBlock]) -> String {
        blocks.map { b -> String in
            switch b {
            case .heading(let level, let text):
                return "<h\(level)>\(inline(text))</h\(level)>"
            case .paragraph(let text):
                let html = inline(text)
                return html.hasPrefix("<div class=\"math\">") && html.hasSuffix("</div>") ? html : "<p>\(html)</p>"
            case .bullet(let text, let ordered, let depth):
                let mark = ordered.map { "\(esc($0))." } ?? (depth == 0 ? "•" : "◦")
                return "<div class=\"li\" style=\"margin-left:\(Double(depth) * 1.2)em\"><span class=\"mk\">\(mark)</span><div>\(inline(text))</div></div>"
            case .quote(let text):
                return "<blockquote>\(body(Markdown.parse(text)))</blockquote>"
            case .code(let text):
                return "<pre><code>\(esc(text))</code></pre>"
            case .table(let header, let rows):
                let head = header.map { "<th>\(inline($0))</th>" }.joined()
                let trs = rows.map { "<tr>" + $0.map { "<td>\(inline($0))</td>" }.joined() + "</tr>" }.joined()
                return "<div class=\"table\"><table><thead><tr>\(head)</tr></thead><tbody>\(trs)</tbody></table></div>"
            case .rule:
                return "<hr>"
            case .image(let alt, let src):
                return "<p><img alt=\"\(esc(alt))\" src=\"\(esc(src))\"></p>"
            }
        }.joined(separator: "\n")
    }

    static func page(_ blocks: [MDBlock], prefs: ReadPrefs, palette p: Palette) -> String {
        func css(_ c: Color) -> String {
            var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            UIColor(c).getRed(&r, green: &g, blue: &b, alpha: &a)
            return String(format: "#%02x%02x%02x", Int(r * 255), Int(g * 255), Int(b * 255))
        }
        let size = prefs.size, line = (prefs.size + prefs.lineSpacing) / prefs.size
        let s = prefs.headingSize.scales
        return """
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
        <link rel="stylesheet" href="sparkdoc://katex/katex.min.css">
        <style>
        html, body { margin: 0; padding: 0; background: transparent; -webkit-text-size-adjust: 100%; }
        body { color: \(css(p.ink)); font: \(size)px/\(line) "Songti SC", "PingFang SC", -apple-system, serif; overflow-wrap: anywhere; }
        body > *:first-child { margin-top: 0; }
        p, .li, blockquote, pre, .table, div.math { margin: 0 0 \(prefs.paraSpacing)px; }
        h1, h2, h3, h4, h5, h6 { font-family: "PingFang SC", -apple-system, sans-serif; font-weight: 600; line-height: 1.35;
          color: \(css(prefs.headingTint(p))); margin: \(size * 1.1)px 0 \(prefs.paraSpacing)px; }
        h1, h2 { font-size: \(size * s.section)px; } h3, h4, h5, h6 { font-size: \(size * s.sub)px; color: \(css(p.ink)); }
        a { color: \(css(p.accent)); text-decoration: none; }
        .li { display: flex; gap: 8px; } .li .mk { color: \(css(p.ink2)); flex: none; }
        blockquote { color: \(css(p.ink2)); border-left: 2px solid \(css(p.rule)); padding-left: 14px; margin-left: 0; font-size: 0.95em; }
        blockquote > :last-child { margin-bottom: 0; }
        code { font: 0.85em ui-monospace, Menlo, monospace; background: \(css(p.paper2)); padding: 1px 4px; border-radius: 4px; }
        pre { background: \(css(p.paper2)); padding: 12px; border-radius: 8px; overflow-x: auto; }
        pre code { background: none; padding: 0; font-size: 0.78em; }
        img { max-width: 100%; height: auto; border-radius: 6px; }
        hr { border: 0; border-top: 1px solid \(css(p.rule)); }
        mark { background: \(css(p.chip)); color: inherit; }
        .table { overflow-x: auto; } table { border-collapse: collapse; font-size: 0.9em; }
        th, td { border: 1px solid \(css(p.rule)); padding: 6px 8px; text-align: left; vertical-align: top; }
        div.math { overflow-x: auto; overflow-y: hidden; }
        .katex { font-size: 1.08em; } .katex-display { margin: 0.4em 0; }
        </style>
        <script src="sparkdoc://katex/katex.min.js"></script>
        </head><body>
        \(body(blocks))
        <script src="sparkdoc://katex/math.js"></script>
        <script>
        (() => {
          const post = () => webkit.messageHandlers.height.postMessage(document.documentElement.scrollHeight);
          new ResizeObserver(post).observe(document.body);
          addEventListener("load", post);
          document.querySelectorAll("img").forEach(i => i.addEventListener("load", post));
          post();
        })();
        </script>
        </body></html>
        """
    }
}

/// 正文网页：高度跟着内容变，外面的 ScrollView 负责滚动
struct MathArticle: View {
    let blocks: [MDBlock]
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.mdBaseDir) private var base
    @Environment(Vault.self) private var vault
    @State private var height: CGFloat = 600

    var body: some View {
        MathWebView(html: MathHTML.page(blocks, prefs: prefs, palette: p), base: base, vault: vault, height: $height)
            .frame(height: height)
    }
}

private struct MathWebView: UIViewRepresentable {
    let html: String
    let base: String?
    let vault: Vault
    @Binding var height: CGFloat

    func makeCoordinator() -> Coordinator { Coordinator(height: $height) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(DocFiles(vault: vault), forURLScheme: "sparkdoc")
        config.userContentController.add(context.coordinator, name: "height")
        let web = WKWebView(frame: .zero, configuration: config)
        web.isOpaque = false
        web.backgroundColor = .clear
        web.scrollView.isScrollEnabled = false
        web.scrollView.bounces = false
        web.navigationDelegate = context.coordinator
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        guard context.coordinator.loaded != html else { return }
        context.coordinator.loaded = html
        // 图片的相对地址按文章所在的文件夹找：sparkdoc://vault/<文件夹>/
        let dir = (base ?? "").split(separator: "/").map { String($0).addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? "" }
        web.loadHTMLString(html, baseURL: URL(string: "sparkdoc://vault/" + dir.map { $0 + "/" }.joined()))
    }

    static func dismantleUIView(_ web: WKWebView, coordinator: Coordinator) {
        web.configuration.userContentController.removeScriptMessageHandler(forName: "height")
    }

    final class Coordinator: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
        var height: Binding<CGFloat>
        var loaded = ""
        init(height: Binding<CGFloat>) { self.height = height }

        func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
            if let h = message.body as? Double, h > 0, abs(CGFloat(h) - height.wrappedValue) > 1 { height.wrappedValue = CGFloat(h) }
        }

        func webView(_ web: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
            // 正文里的链接在系统浏览器里开，不在这块网页里跳走
            if action.navigationType == .linkActivated, let url = action.request.url {
                await UIApplication.shared.open(url)
                return .cancel
            }
            return .allow
        }
    }
}

/// sparkdoc://katex/<文件> 读 App 里的 KaTeX；sparkdoc://vault/<路径> 读笔记库里的图片（没下载的先下载）
private final class DocFiles: NSObject, WKURLSchemeHandler {
    let vault: Vault
    private var stopped = Set<ObjectIdentifier>()
    init(vault: Vault) { self.vault = vault }

    func webView(_ web: WKWebView, start task: WKURLSchemeTask) {
        guard let url = task.request.url else { return }
        let id = ObjectIdentifier(task)
        Task { @MainActor in
            var file: URL?
            if url.host == "katex" {
                let name = url.lastPathComponent as NSString
                file = Bundle.main.url(forResource: name.deletingPathExtension, withExtension: name.pathExtension)
            } else if url.host == "vault" {
                let path = url.path.removingPercentEncoding?.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
                if !path.isEmpty, !path.split(separator: "/").contains("..") { file = await vault.fileURL(forDownloading: path) }
            }
            guard !stopped.contains(id) else { stopped.remove(id); return }
            guard let file, let data = try? Data(contentsOf: file) else {
                task.didFailWithError(URLError(.fileDoesNotExist))
                return
            }
            let types = ["js": "text/javascript", "css": "text/css", "woff2": "font/woff2", "png": "image/png",
                         "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"]
            let mime = types[file.pathExtension.lowercased()] ?? "application/octet-stream"
            task.didReceive(URLResponse(url: url, mimeType: mime, expectedContentLength: data.count, textEncodingName: nil))
            task.didReceive(data)
            task.didFinish()
        }
    }

    func webView(_ web: WKWebView, stop task: WKURLSchemeTask) {
        stopped.insert(ObjectIdentifier(task))
    }
}
