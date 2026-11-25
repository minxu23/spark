import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// 分享扩展：在微信、Safari、YouTube、播客 App 里点「分享 → Spark」。两种用法：
/// - 存到剪藏：当场抓下全文和图片，存在手机上（摘要在打开 Spark 时写），见 Shared/Clipper.swift；
/// - 交给电脑处理：开成同步仓库里的一个 spark-task Issue，跟主 App「添加任务 → 处理链接」一样。
/// arXiv 链接、PDF 文件存为论文：扩展里只放进收件箱（PDF 只拷贝，不取字），取内容、写摘要留给主 App（Spark/Papers.swift）。
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        #if DEBUG && targetEnvironment(simulator)
        // 模拟器 DEBUG 上主 App 没以样例库或真实模式启动过（App Group 里两种标记都没有）：只说明，不读钥匙串、不写发件箱
        if Fixture.isBlocked {
            show(SimulatorGuardView(onClose: { [weak self] in self?.extensionContext?.completeRequest(returningItems: nil) }))
            return
        }
        #endif
        let model = ShareModel(close: { [weak self] in
            self?.extensionContext?.completeRequest(returningItems: nil)
        })
        let host = UIHostingController(rootView: ShareView(model: model))
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
        Task { await model.load(from: extensionContext?.inputItems as? [NSExtensionItem] ?? []) }
    }

    #if DEBUG && targetEnvironment(simulator)
    private func show(_ root: some View) {
        let host = UIHostingController(rootView: root)
        addChild(host)
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.didMove(toParent: self)
    }
    #endif
}

@MainActor
@Observable
final class ShareModel {
    enum Phase: Equatable { case loading, ready, sending, sent, failed(String) }
    enum Mode: String { case clip, task }

    var url = ""
    /// 分享进来的是一份 PDF 文件：存为论文
    var pdf: NSItemProvider?
    var pdfName = ""
    /// arXiv 链接、以 .pdf 结尾的链接：存为论文（扩展里只记一条论文条目，不下载，交给主 App）
    var isPaper: Bool { pdf != nil || Paper.isPaperLink(url) }
    var mode = Mode.clip
    var note = ""
    /// 存好后放进稍后再读（正文没抓到时 Spark 里先存一篇占位剪藏）
    var later = false
    var progress = ""
    var clipped = ""
    var dest = SharedStore.lastDest
    var length = SharedStore.lastLength
    var phase = Phase.loading
    let connection = Connection.load()
    /// 没连 GitHub、只用数据线：任务放进发件箱
    let cableOnly = SharedStore.cableOnly
    var canSend: Bool { connection != nil || cableOnly }
    let folders = SharedStore.folders
    let close: () -> Void

    init(close: @escaping () -> Void) { self.close = close }

    /// 分享过来的可能是链接，也可能是一段带链接的文字
    func load(from items: [NSExtensionItem]) async {
        let providers = items.flatMap { $0.attachments ?? [] }
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
               let u = try? await provider.loadItem(forTypeIdentifier: UTType.url.identifier) as? URL,
               ["http", "https"].contains(u.scheme?.lowercased() ?? "") {
                url = u.absoluteString
                break
            }
            if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
               let text = try? await provider.loadItem(forTypeIdentifier: UTType.plainText.identifier) as? String,
               let found = SparkTask.firstURL(in: text) {
                url = found
                break
            }
        }
        // 同时有 PDF 文件和网址时用网址：原件由电脑按网址下，不会变成只在这台手机上。只有文件时才走本地文件
        if url.isEmpty, let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) }) {
            pdf = provider
            let name = provider.suggestedName ?? "文档"
            pdfName = name.lowercased().hasSuffix(".pdf") ? name : name + ".pdf"
        }
        if case .show(let name) = dest, !folders.contains(where: { $0.name == name }) { dest = .newSeries }
        // 视频、播客默认交给电脑（要转写）；文章默认存到剪藏
        let media = ["youtube.com", "youtu.be", "bilibili.com", "podcasts.apple.com", "xiaoyuzhoufm.com", "spotify.com"]
        let host = URL(string: url)?.host()?.lowercased() ?? ""
        mode = media.contains { host == $0 || host.hasSuffix("." + $0) } ? .task : .clip
        phase = .ready
    }

    /// 论文：arXiv 链接只记下链接，PDF 只拷进收件箱（大文件不在扩展里取字，免得分享面板卡住）
    func savePaper() async {
        phase = .sending
        progress = pdf != nil ? "正在拷贝 PDF…" : ""
        do {
            if let pdf {
                let name = pdfName
                _ = try await withCheckedThrowingContinuation { (done: CheckedContinuation<ClipInbox.Entry, Error>) in
                    // 给的临时文件只在回调里有效：在回调里拷进收件箱
                    _ = pdf.loadFileRepresentation(forTypeIdentifier: UTType.pdf.identifier) { file, error in
                        guard let file else { done.resume(throwing: error ?? SparkError("PDF 读不出来")); return }
                        done.resume(with: Result { try ClipInbox.addFile(file, name: name) })
                    }
                }
                clipped = "《\(pdfName)》，打开 Spark 时取字、写摘要。原件只留在这台手机上"
            } else {
                _ = try ClipInbox.addPaper(url: url, later: later)
                clipped = "，打开 Spark 时取内容、写摘要"
            }
            phase = .sent
            try? await Task.sleep(for: .milliseconds(1600))
            close()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// 存到剪藏：抓全文、下图片，十几秒内做完；没下完的图片打开 Spark 时接着下
    func clip() async {
        if isPaper { await savePaper(); return }
        phase = .sending
        progress = "正在保存全文…"
        do {
            var e = try ClipInbox.add(url: url, note: note.trimmingCharacters(in: .whitespacesAndNewlines), later: later)
            let deadline = Date().addingTimeInterval(15)
            if e.article == nil, let u = URL(string: url) {
                do { e.article = try await Clipper.fetch(u) } catch is Clipper.PDFLink {
                    e.paper = true   // 打开才知道是一份 PDF：标为论文，不在扩展里下载，主 App 处理
                    e.error = nil
                } catch {
                    e.error = error.localizedDescription
                    if error is XHS.Gone { e.gone = true }   // 确定打不开的，主 App 里报一次错就不再重试
                }
                try? ClipInbox.save(e)
            }
            if e.paper == true {   // 链接打开是一份 PDF（Clipper.PDFLink）：存为论文，取字留给主 App
                clipped = "（已存为论文），打开 Spark 后处理"
            } else if let a = e.article {
                progress = a.images.isEmpty ? "" : "正在保存图片…"
                let n = await ClipInbox.capture(&e, deadline: deadline)
                let rest = a.images.count - n
                clipped = (later ? "（稍后再读）" : "") + "《\(a.title)》" + (a.images.isEmpty ? "" : rest > 0 ? "，图片还有 \(rest) 张打开 Spark 时接着存" : "，\(n) 张图片")
            } else {
                clipped = "全文这次没抓到（\(e.error ?? "原因不明")），打开 Spark 时" + (later ? "先存一篇占位，之后可以补抓" : "再试")
            }
            phase = .sent
            try? await Task.sleep(for: .milliseconds(1600))
            close()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    func send() async {
        guard canSend else { return }
        phase = .sending
        do {
            let spec = SparkTask.link(url, dest: dest, length: length)
            if let connection {
                do {
                    try await SparkTask.submit(spec, via: GitHub(connection), from: "分享")
                } catch {
                    // GitHub 一时交不上（没网、令牌过期）：先放发件箱，下次接数据线同步时交给电脑
                    try SparkTask.enqueue(spec)
                }
            } else {
                try SparkTask.enqueue(spec)
            }
            SharedStore.lastDest = dest
            SharedStore.lastLength = length
            phase = .sent
            try? await Task.sleep(for: .milliseconds(900))
            close()
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }
}

struct ShareView: View {
    @Bindable var model: ShareModel
    @Environment(\.colorScheme) private var scheme

    // 跟主 App 的羊皮纸配色一致（扩展里不带主 App 的阅读设置，跟随系统深浅色）
    private var paper: Color { scheme == .dark ? Color(red: 0.11, green: 0.106, blue: 0.094) : Color(red: 0.961, green: 0.957, blue: 0.929) }
    private var accent: Color { scheme == .dark ? Color(red: 0.624, green: 0.714, blue: 0.847) : Color(red: 0.118, green: 0.290, blue: 0.549) }

    var body: some View {
        NavigationStack {
            Form {
                if model.phase == .loading {
                    ProgressView()
                } else if model.pdf != nil {
                    Section {
                        Text(model.pdfName).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(3)
                    } header: { Text("PDF") } footer: {
                        Text("存为论文。打开 Spark 时取字、写摘要。原件只留在这台手机上，不传给电脑，电脑上只有文字。")
                    }
                    paperStatus
                } else if model.url.isEmpty {
                    Section {
                        Text("分享的内容里没有找到链接。Spark 能存公众号、网页文章，处理 YouTube 视频或播放列表、播客单集。")
                    }
                } else {
                    Section("链接") {
                        Text(model.url).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
                    }
                    Section {
                        Picker("怎么处理", selection: $model.mode) {
                            Text(model.isPaper ? "存为论文" : "存到剪藏").tag(ShareModel.Mode.clip)
                            Text("交给电脑处理").tag(ShareModel.Mode.task)
                        }
                        .pickerStyle(.segmented)
                    } footer: {
                        Text(model.mode == .clip ? (model.isPaper ? "arXiv 论文存进「论文」，打开 Spark 时取内容、写摘要，之后同步到电脑。"
                                                    : "存下全文和图片，打开 Spark 时写摘要、更新剪藏索引，之后同步到电脑。")
                             : "电脑上抓取、转写、写小结，适合视频和播客。")
                    }
                    if model.mode == .clip {
                        Section("备注（可以不填）") {
                            TextField("为什么存它", text: $model.note, axis: .vertical).lineLimit(1...4)
                        }
                        Section {
                            Toggle("稍后再读", isOn: $model.later)
                        } footer: {
                            Text("存好后放进 Spark 书架的「稍后再读」。全文没抓到时先存一篇占位，之后在阅读页点「补抓正文」。")
                        }
                    } else if !model.canSend {
                        Section { Text("还没连接笔记库。先打开 Spark，填好仓库和令牌，再回来分享。") }
                    } else {
                        Section {
                            Picker("文件夹", selection: $model.dest) {
                                Text(SparkTask.Dest.newSeries.label).tag(SparkTask.Dest.newSeries)
                                Text(SparkTask.Dest.summit.label).tag(SparkTask.Dest.summit)
                                ForEach(model.folders, id: \.self) { f in
                                    Text(f.name).tag(SparkTask.Dest.show(f.name))
                                }
                            }
                            .pickerStyle(.navigationLink)
                        } header: { Text("放到") } footer: {
                            Text(model.folders.isEmpty ? "打开 Spark 同步一次后，这里能选书架上已有的节目。" : "选已有的节目，这一期会加进那个节目文件夹。")
                        }
                        Section {
                            Picker("小结篇幅", selection: $model.length) {
                                Text("简洁").tag("short"); Text("标准").tag("medium"); Text("详细").tag("long")
                            }
                            .pickerStyle(.segmented)
                        } header: { Text("小结篇幅") } footer: {
                            Text(model.connection == nil
                                 ? "先存在手机上，下次接数据线同步时交给电脑。进度在 Spark 的「任务」里看。"
                                 : "电脑上的 Spark 开着、并打开了「手机上添加的任务」时接手处理。进度在 Spark 的「任务」里看。")
                        }
                    }
                    if case .failed(let message) = model.phase {
                        Section { Text(message).foregroundStyle(.red) }
                    }
                    if model.phase == .sending, !model.progress.isEmpty {
                        Section { Label(model.progress, systemImage: "arrow.down.circle") }
                    }
                    if model.phase == .sent {
                        Section {
                            if model.mode == .clip {
                                Label((model.isPaper ? "已存为论文" : "已存进剪藏") + model.clipped, systemImage: "checkmark.circle.fill").foregroundStyle(accent)
                            } else {
                                Label(model.connection == nil ? "已放进发件箱" : "已交给电脑", systemImage: "checkmark.circle.fill")
                                    .foregroundStyle(accent)
                            }
                        }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(paper.ignoresSafeArea())
            .navigationTitle("交给 Spark")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { model.close() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(model.phase == .sending ? (model.mode == .clip ? "保存中…" : "提交中…") : (model.mode == .clip ? "存下来" : "交给电脑")) {
                        Task { model.mode == .clip ? await model.clip() : await model.send() }
                    }
                    .disabled((model.mode == .task && !model.canSend) || (model.url.isEmpty && model.pdf == nil) || model.phase == .sending || model.phase == .sent)
                }
            }
        }
        .tint(accent)
    }

    /// PDF 存为论文时的出错、进度、结果
    @ViewBuilder private var paperStatus: some View {
        if case .failed(let message) = model.phase {
            Section { Text(message).foregroundStyle(.red) }
        }
        if model.phase == .sending, !model.progress.isEmpty {
            Section { Label(model.progress, systemImage: "arrow.down.circle") }
        }
        if model.phase == .sent {
            Section { Label("已存为论文" + model.clipped, systemImage: "checkmark.circle.fill").foregroundStyle(accent) }
        }
    }
}
