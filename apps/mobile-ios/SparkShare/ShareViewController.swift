import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// 分享扩展：在 YouTube、Safari、播客 App 里点「分享 → Spark」，把链接交给电脑处理
/// （开成同步仓库里的一个 spark-task Issue，跟主 App「添加任务 → 处理链接」一样）。
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
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
}

@MainActor
@Observable
final class ShareModel {
    enum Phase: Equatable { case loading, ready, sending, sent, failed(String) }

    var url = ""
    var dest = SharedStore.lastDest
    var length = SharedStore.lastLength
    var phase = Phase.loading
    let connection = Connection.load()
    let folders = SharedStore.folders
    let close: () -> Void

    init(close: @escaping () -> Void) { self.close = close }

    /// 分享过来的可能是链接，也可能是一段带链接的文字
    func load(from items: [NSExtensionItem]) async {
        for provider in items.flatMap({ $0.attachments ?? [] }) {
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
        if case .show(let name) = dest, !folders.contains(where: { $0.name == name }) { dest = .newSeries }
        phase = .ready
    }

    func send() async {
        guard let connection else { return }
        phase = .sending
        do {
            try await SparkTask.submit(SparkTask.link(url, dest: dest, length: length),
                                       via: GitHub(connection), from: "分享")
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
    private var accent: Color { scheme == .dark ? Color(red: 0.624, green: 0.714, blue: 0.847) : Color(red: 0.106, green: 0.212, blue: 0.365) }

    var body: some View {
        NavigationStack {
            Form {
                if model.connection == nil {
                    Section {
                        Text("还没连接笔记库。先打开 Spark，填好仓库和令牌，再回来分享。")
                    }
                } else if model.phase == .loading {
                    ProgressView()
                } else if model.url.isEmpty {
                    Section {
                        Text("分享的内容里没有找到链接。Spark 能处理 YouTube 视频或播放列表、播客单集、Substack 或公众号文章的链接。")
                    }
                } else {
                    Section("链接") {
                        Text(model.url).font(.system(size: 14)).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
                    }
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
                        Text("电脑上的 Spark 开着、并打开了「手机上添加的任务」时接手处理。进度在 Spark 的「任务」里看。")
                    }
                    if case .failed(let message) = model.phase {
                        Section { Text(message).foregroundStyle(.red) }
                    }
                    if model.phase == .sent {
                        Section { Label("已交给电脑", systemImage: "checkmark.circle.fill").foregroundStyle(accent) }
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(paper.ignoresSafeArea())
            .navigationTitle("交给 Spark 处理")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { model.close() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(model.phase == .sending ? "提交中…" : "交给电脑") { Task { await model.send() } }
                        .disabled(model.connection == nil || model.url.isEmpty || model.phase == .sending || model.phase == .sent)
                }
            }
        }
        .tint(accent)
    }
}
