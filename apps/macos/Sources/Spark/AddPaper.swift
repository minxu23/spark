import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// 添加论文（论文页的工具栏）：贴链接或选一个 PDF 文件，交给本机服务（core/papers.py）。
/// 链接直接交给电脑抓（/read/api/papers），文件传过去（/read/api/papers/upload，multipart）；进度跟剪藏一样查 /read/api/clips/<id>。
@MainActor
final class PaperComposer: ObservableObject {
    @Published var url = ""
    @Published var busy = false
    @Published var phase = ""
    @Published var error = ""
    @Published var warning = ""
    @Published var title = ""
    @Published var savedPath = ""
    @Published var started: Date?

    func prefill() {
        guard url.isEmpty, let text = NSPasteboard.general.string(forType: .string), let u = ClipComposer.webURL(text) else { return }
        url = u.absoluteString
    }

    func addLink() async {
        guard let u = ClipComposer.webURL(url), !busy else { return }
        url = u.absoluteString
        var req = URLRequest(url: URL(string: "read/api/papers", relativeTo: SparkServer.baseURL)!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["url": u.absoluteString])
        await run(req)
    }

    func pickFile() async {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let file = panel.url else { return }
        let size = (try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size <= Paper.maxPDFBytes else { error = Paper.tooBigMessage; return }
        guard let data = try? Data(contentsOf: file) else { error = "PDF 读不出来"; return }
        let boundary = "spark-" + UUID().uuidString
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(file.lastPathComponent.replacingOccurrences(of: "\"", with: ""))\"\r\nContent-Type: application/pdf\r\n\r\n".utf8)
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: URL(string: "read/api/papers/upload", relativeTo: SparkServer.baseURL)!)
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        await run(req)
    }

    private func run(_ req: URLRequest) async {
        busy = true; started = .now; error = ""; warning = ""; phase = "正在提交"
        defer { busy = false }
        do {
            var job = try await request(req)
            while job.done != true, let id = job.id {
                phase = job.phase ?? "正在处理"
                try await Task.sleep(for: .seconds(1))
                job = try await request(URLRequest(url: URL(string: "read/api/clips/" + id, relativeTo: SparkServer.baseURL)!))
            }
            if let message = job.error, !message.isEmpty { throw SparkError(message) }
            warning = job.warning ?? ""
            title = job.title ?? ""
            savedPath = job.path ?? ""
        } catch { self.error = error.localizedDescription }
    }

    private func request(_ req: URLRequest) async throws -> ClipJob {
        let (data, response) = try await SparkServer.session.data(for: req)
        let job = try JSONDecoder().decode(ClipJob.self, from: data)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            throw SparkError(job.error ?? "保存失败，请重试")
        }
        return job
    }
}

struct AddPaperView: View {
    @ObservedObject var composer: PaperComposer
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("添加论文").font(.title2.weight(.semibold))
            if !composer.savedPath.isEmpty {
                Label(composer.warning.isEmpty ? "已存为论文" : composer.warning, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Text(composer.title).font(.headline)
                HStack {
                    Spacer()
                    Button("完成") { dismiss() }
                    Button("打开论文") {
                        model.openClip(composer.savedPath)
                        dismiss()
                    }.keyboardShortcut(.defaultAction)
                }
            } else {
                Text("粘贴链接（arXiv、PDF 或网页），或选一个 PDF 文件。电脑取内容、写摘要后存进论文。").foregroundStyle(.secondary)
                TextField("https://arxiv.org/abs/…", text: $composer.url).textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await composer.addLink(); if !composer.savedPath.isEmpty { model.refresh() } } }
                    .disabled(composer.busy)
                if composer.busy {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(composer.phase)
                        Spacer()
                        if let date = composer.started { Text(date, style: .timer).monospacedDigit() }
                    }.font(.callout)
                }
                if !composer.error.isEmpty { Text(composer.error).foregroundStyle(.red).textSelection(.enabled) }
                HStack {
                    Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(composer.busy)
                    Spacer()
                    Button("选 PDF 文件…") { Task { await composer.pickFile(); if !composer.savedPath.isEmpty { model.refresh() } } }
                        .disabled(composer.busy)
                    Button("粘贴链接") { Task { await composer.addLink(); if !composer.savedPath.isEmpty { model.refresh() } } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(composer.busy || ClipComposer.webURL(composer.url) == nil)
                }
            }
        }
        .padding(24).frame(width: 540)
        .interactiveDismissDisabled(composer.busy)
        .onAppear { composer.prefill() }
    }
}
