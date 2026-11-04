import AppKit
import WebKit

/// 一块网页。WKWebView 默认不弹 alert/confirm、不开文件框、不下载，这里都接上，
/// 网页里的确认框和上传才能用。本机页面留在 App 里，外部链接和 obsidian:// 交给系统。
final class WebPane: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, ObservableObject {
    /// 页面自己的顶栏、工具切换和「回到 Spark」在 App 里由原生侧边栏和工具栏代替。
    /// data-speech 是中英对照稿显示哪一边，只有不是对照时才生效
    private static func chrome(speech: String) -> String {
        """
        document.documentElement.classList.add('in-app');
        document.documentElement.dataset.speech = '\(speech)';
        const style = document.createElement('style');
        style.textContent = `
          .in-app .bar, .in-app .ws-nav, .in-app .home-link, .in-app .back, .in-app .head-row, .in-app header > h1, .in-app .toc-top { display: none !important; }
          .in-app main { padding-top: 28px; }
          .in-app .toc-side { top: 24px; }
          .in-app #episodes, .in-app #fileInfo { display: none; }
          html.in-app:not(.reader) {
            --accent: #0a6ed1 !important; --accent-hover: #0859ad !important;
            --strong: #0a6ed1 !important; --link: #0a6ed1 !important;
          }
          @media (prefers-color-scheme: dark) {
            html.in-app:not(.reader) {
              --accent: #2f7fe0 !important; --accent-hover: #4a93ea !important;
              --strong: #6db0f5 !important; --link: #6db0f5 !important;
            }
          }
          .in-app:not(.reader) :is(.panel, section) { background: transparent; box-shadow: none; border-radius: 8px; }
          .in-app:not(.reader) :is(input:not([type=checkbox]):not([type=radio]), textarea, select) { border-radius: 6px; }
          .in-app:not(.reader) :is(button, a.btn-link) { border-radius: 6px; }
          .in-app:not(.reader) :is(button.primary, a.btn-link.primary) {
            background: var(--accent) !important; border-color: var(--accent) !important; color: #fff !important;
          }
          .in-app:not(.reader) button.primary:hover:not(:disabled) { background: var(--accent-hover) !important; }
          [data-speech="zh"] .pair .orig, [data-speech="en"] .pair .tr { display: none; }
          [data-speech="zh"] .doc:has(.pair) :is(ul, ol):has(+ blockquote) { display: none; }
          [data-speech="en"] .doc:has(.pair) :is(ul, ol) + blockquote { display: none; }
          [data-speech="zh"] .pair, [data-speech="en"] .pair { display: block; }
          [data-speech="zh"] .pair .tr { color: var(--ink); padding-left: 0; border: 0; }
          [data-speech="zh"] main.wide:not(.has-toc), [data-speech="en"] main.wide:not(.has-toc) { max-width: var(--measure); }
          [data-speech="zh"] main.wide.has-toc, [data-speech="en"] main.wide.has-toc { max-width: calc(var(--measure) + 560px); }
        `;
        document.documentElement.appendChild(style);
        """
    }

    static var speechLang: String { UserDefaults.standard.string(forKey: "speechLang") ?? "zh" }

    let webView: WKWebView
    @Published var canGoBack = false
    @Published var canGoForward = false
    @Published var hasDeck = false
    @Published var hasObsidian = false
    @Published var isBilingual = false
    @Published var toc: [TocEntry] = []
    @Published var speech = WebPane.speechLang
    var onFinish: (() -> Void)?
    private var watches: [NSKeyValueObservation] = []
    private var saveTargets: [ObjectIdentifier: URL] = [:]

    init(configuration: WKWebViewConfiguration? = nil) {
        let config = configuration ?? WKWebViewConfiguration()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        if configuration == nil {
            config.userContentController.addUserScript(WebPane.chromeScript())
        }
        webView = WKWebView(frame: .zero, configuration: config)
        webView.allowsBackForwardNavigationGestures = true
        webView.setValue(false, forKey: "drawsBackground")  // 页面没出来时是窗口底色，不闪白
        webView.isInspectable = true
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        watches = [
            webView.observe(\.canGoBack) { [weak self] view, _ in self?.canGoBack = view.canGoBack },
            webView.observe(\.canGoForward) { [weak self] view, _ in self?.canGoForward = view.canGoForward },
        ]
    }

    struct TocEntry: Decodable, Identifiable, Hashable {
        let id: String
        let text: String
        let sub: Bool
    }

    private static func chromeScript() -> WKUserScript {
        WKUserScript(source: chrome(speech: speechLang), injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }

    func setSpeech(_ lang: String) {
        speech = lang
        UserDefaults.standard.set(lang, forKey: "speechLang")
        let scripts = webView.configuration.userContentController
        scripts.removeAllUserScripts()
        scripts.addUserScript(WebPane.chromeScript())
        webView.evaluateJavaScript("document.documentElement.dataset.speech = '\(lang)'")
    }

    func load(_ url: URL) {
        webView.load(URLRequest(url: url))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.evaluateJavaScript("[!!document.querySelector('a.btn[aria-label=\"看演示\"]'), !!document.querySelector('a.btn[href^=\"obsidian:\"]'), document.querySelectorAll('.pair').length >= 3]") { [weak self] result, _ in
            let flags = result as? [Bool] ?? [false, false, false]
            self?.hasDeck = flags[0]
            self?.hasObsidian = flags[1]
            self?.isBilingual = flags[2]
        }
        let headings = """
            JSON.stringify([...document.querySelectorAll('.toc-side .toc-list a')].map(a => ({
              id: decodeURIComponent(a.getAttribute('href').slice(1)), text: a.firstChild.textContent.trim(),
              sub: a.closest('li').className !== 'd0' })))
            """
        webView.evaluateJavaScript(headings) { [weak self] result, _ in
            let data = (result as? String).map { Data($0.utf8) } ?? Data()
            let entries = (try? JSONDecoder().decode([TocEntry].self, from: data)) ?? []
            self?.toc = entries.filter { $0.text.contains(where: \.isLetter) }
        }
        onFinish?()
    }

    func scroll(to id: String) {
        let quoted = (try? JSONEncoder().encode(id)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
        webView.evaluateJavaScript("document.getElementById(\(quoted))?.scrollIntoView()")
    }

    func clickAction(_ selector: String) {
        webView.evaluateJavaScript("document.querySelector('\(selector)')?.click()")
    }

    // MARK: 导航

    private static func isLocal(_ url: URL) -> Bool {
        (url.scheme == "http" || url.scheme == "https") && (url.host == "127.0.0.1" || url.host == "localhost")
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if action.shouldPerformDownload { return decisionHandler(.download) }
        if WebPane.isLocal(url) || ["about", "blob", "data"].contains(url.scheme ?? "") {
            return decisionHandler(.allow)
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        let attachment = disposition.lowercased().hasPrefix("attachment")
        decisionHandler(attachment || !response.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    // MARK: 下载

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.begin { [weak self] result in
            let url = result == .OK ? panel.url : nil
            if let url {
                // 保存框已经问过要不要替换，WebKit 自己不覆盖已有文件，得先挪走
                if FileManager.default.fileExists(atPath: url.path) {
                    try? FileManager.default.trashItem(at: url, resultingItemURL: nil)
                }
                self?.saveTargets[ObjectIdentifier(download)] = url
            }
            completionHandler(url)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = saveTargets.removeValue(forKey: ObjectIdentifier(download)) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        saveTargets[ObjectIdentifier(download)] = nil
        let alert = NSAlert()
        alert.messageText = "下载失败"
        alert.informativeText = error.localizedDescription
        present(alert) { _ in }
    }

    // MARK: 网页弹窗、新窗口、选文件

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, !url.absoluteString.isEmpty, url.absoluteString != "about:blank",
           !WebPane.isLocal(url) {
            NSWorkspace.shared.open(url)
            return nil
        }
        return PopupWindow(configuration: configuration).pane.webView
    }

    func webViewDidClose(_ webView: WKWebView) {
        webView.window?.close()
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        present(alert) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.addButton(withTitle: "取消")
        present(alert) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "好")
        alert.addButton(withTitle: "取消")
        let field = NSTextField(string: defaultText ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 280, height: 24)
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        present(alert) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.begin { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    private func present(_ alert: NSAlert, done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let window = webView.window {
            alert.beginSheetModal(for: window, completionHandler: done)
        } else {
            done(alert.runModal())
        }
    }
}

/// 网页用 window.open 开的新窗口（比如生成报告时先开好的标签页）
final class PopupWindow: NSWindowController, NSWindowDelegate {
    private static var open: [PopupWindow] = []
    let pane: WebPane

    init(configuration: WKWebViewConfiguration) {
        pane = WebPane(configuration: configuration)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 760),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "Spark"
        window.contentView = pane.webView
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self
        PopupWindow.open.append(self)
        showWindow(nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func windowWillClose(_ notification: Notification) {
        PopupWindow.open.removeAll { $0 === self }
    }
}
