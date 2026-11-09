import AppKit
import SwiftUI

/// 与 iOS 共用 Clipper 和 XPost；正文和图片交给本机服务保存。
@MainActor
final class ClipComposer: ObservableObject {
    @Published var url = ""
    @Published var note = ""
    @Published var title = ""
    @Published var article: Clipper.Article?
    @Published var busy = false
    @Published var phase = ""
    @Published var error = ""
    @Published var warning = ""
    @Published var savedPath = ""
    @Published var started: Date?
    private var jobID: String?
    var submitted: Bool { jobID != nil }

    static func webURL(_ text: String) -> URL? {
        guard let u = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["https", "http"].contains(u.scheme?.lowercased() ?? ""),
              u.host != nil, u.user == nil, u.password == nil else { return nil }
        return u
    }

    func prefill() {
        guard url.isEmpty, let text = NSPasteboard.general.string(forType: .string), Self.webURL(text) != nil else { return }
        url = text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func fetch() async {
        guard let u = Self.webURL(url), !busy else { return }
        busy = true; started = .now; error = ""; phase = "正在抓取正文"
        defer { busy = false }
        do {
            var a = try await Clipper.fetch(u)
            // 页面上没标题：先用网站名占位，存的时候让模型起一个（同手机）
            if a.title.isEmpty { a.title = u.host ?? "未命名"; a.titleGuessed = true }
            article = a
            title = a.title
            phase = "正文已就绪"
        } catch { self.error = error.localizedDescription; phase = "抓取失败" }
    }

    func save() async {
        guard var a = article, !busy else { return }
        busy = true; started = .now; error = ""; phase = "正在保存"
        defer { busy = false }
        do {
            // 已提交的任务只恢复查询，不重复提交。
            if jobID == nil {
                // 文件名、摘要、翻译都由电脑按手机的写法做（core/clip_capture.py），这里只交抓好的正文
                let edited = title.trimmingCharacters(in: .whitespacesAndNewlines)
                if edited != a.title { a.title = edited; a.titleGuessed = false }
                var article: [String: Any] = ["url": a.url, "title": a.title, "source": a.source, "published": a.published,
                                              "markdown": a.markdown, "images": a.images]
                if a.titleGuessed == true { article["titleGuessed"] = true }
                if a.imagesOnly == true { article["imagesOnly"] = true }
                let payload: [String: Any] = ["article": article, "note": note]
                var req = URLRequest(url: URL(string: "read/api/clips", relativeTo: SparkServer.baseURL)!)
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try JSONSerialization.data(withJSONObject: payload)
                let job = try await request(req)
                jobID = job.id
            }
            while let id = jobID {
                let job = try await request(URLRequest(url: URL(string: "read/api/clips/" + id, relativeTo: SparkServer.baseURL)!))
                phase = job.phase ?? "正在保存"
                if job.done == true {
                    jobID = nil
                    if let message = job.error, !message.isEmpty { throw SparkError(message) }
                    warning = job.warning ?? ""
                    if let t = job.title, !t.isEmpty { title = t }
                    savedPath = job.path ?? ""
                    return
                }
                try await Task.sleep(for: .seconds(1))
            }
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

private struct ClipJob: Decodable {
    var id: String?
    var done: Bool?
    var phase: String?
    var path: String?
    var title: String?
    var error: String?
    var warning: String?
}

struct AddClipView: View {
    @ObservedObject var composer: ClipComposer
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("添加剪藏").font(.title2.weight(.semibold))
            if !composer.savedPath.isEmpty {
                Label("已存入剪藏", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Text(composer.title).font(.headline)
                if !composer.warning.isEmpty { Text(composer.warning).foregroundStyle(.secondary) }
                HStack {
                    Spacer()
                    Button("完成") { dismiss() }
                    Button("打开剪藏") {
                        model.openClip(composer.savedPath)
                        dismiss()
                    }.keyboardShortcut(.defaultAction)
                }
            } else {
                if composer.article == nil {
                    Text("粘贴文章链接，抓取正文后存入笔记库。") .foregroundStyle(.secondary)
                    TextField("https://…", text: $composer.url).textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await composer.fetch() } }
                        .disabled(composer.busy)
                } else {
                    LabeledContent("标题") { TextField("文章标题", text: $composer.title).textFieldStyle(.roundedBorder).disabled(composer.busy || composer.submitted) }
                    if let a = composer.article {
                        Text("\(a.source) · \(a.markdown.count) 字符 · \(a.images.count) 张图片")
                            .font(.caption).foregroundStyle(.secondary)
                        ScrollView { Text(a.markdown).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            .padding(10).frame(height: 180)
                            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
                    }
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("备注（可选）").font(.callout)
                    TextEditor(text: $composer.note).font(.body).frame(height: 65)
                        .border(.quaternary)
                }.disabled(composer.busy || composer.submitted)
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
                    if composer.article != nil {
                        Button("更换链接") { composer.article = nil; composer.error = "" }.disabled(composer.busy || composer.submitted)
                        Button("存入剪藏") { Task { await composer.save(); if !composer.savedPath.isEmpty { model.refresh() } } }
                            .keyboardShortcut(.defaultAction)
                            .disabled(composer.busy || composer.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    } else {
                        Button("抓取正文") { Task { await composer.fetch() } }.keyboardShortcut(.defaultAction)
                            .disabled(composer.busy || ClipComposer.webURL(composer.url) == nil)
                    }
                }
            }
        }
        .padding(24).frame(width: 540)
        .interactiveDismissDisabled(composer.busy)
        .onAppear { composer.prefill() }
    }
}
