import SwiftUI
import UIKit

/// 在手机上跑的复盘和出报告：排成一队，一次跑一件。跑完写进手机上的笔记库（书架、复盘页马上能看），
/// 再交一个「存进笔记库」的任务给电脑（type: save，见 core/remote_tasks.py）：连了 GitHub 走 Issue，
/// 没连、太长或者一时交不上就放发件箱，下次接数据线同步时电脑存好，同步回来就是同一份。
///
/// 队列存在手机上（Application Support/local-jobs.json）：App 被系统关掉、切到后台被打断，
/// 回到前台接着跑——已经摘取过的摘要卡有缓存，不会重复花钱。
@MainActor
@Observable
final class LocalJobs {
    struct Job: Codable, Identifiable {
        enum State: String, Codable { case queued, running, done, failed }

        let id: String
        let title: String
        let kind: String                 // review / report
        let params: [String: String]     // review: kind days topic months；report: topic scope
        var state: State = .queued
        var status = "排队中"
        var path: String?                // 写好的文件在笔记库里的路径
        var outbox: String?              // 交给电脑时放进了发件箱：那个任务的 id（等接数据线）
        var viaGitHub = false
        var deliverError: String?
        var created = Date.now
        var updated = Date.now

        var running: Bool { state == .running }
    }

    private(set) var jobs: [Job] = []
    private var task: Task<Void, Never>?
    private var bgTask: UIBackgroundTaskIdentifier = .invalid
    private weak var vault: Vault?

    private static var fileURL: URL {
        URL.applicationSupportDirectory.appending(path: "local-jobs.json")
    }
    private static let keepDone = 20

    init() {
        if let data = try? Data(contentsOf: Self.fileURL),
           let saved = try? JSONDecoder().decode([Job].self, from: data) {
            // 上次跑到一半 App 被关了：回到排队，下次打开接着跑
            jobs = saved.map { j in
                var j = j
                if j.state == .running { j.state = .queued; j.status = "上次被打断，接着跑" }
                return j
            }
        }
    }

    /// 设置里的开关：填了 Key 之后复盘和出报告默认在手机上生成
    var runOnPhone: Bool = UserDefaults.standard.object(forKey: "llm.runOnPhone") as? Bool ?? true {
        didSet { UserDefaults.standard.set(runOnPhone, forKey: "llm.runOnPhone") }
    }
    /// 设置页保存后改一下，让依赖它的界面刷新
    var configVersion = 0

    var config: LLM.Config? {
        _ = configVersion
        return LLM.Config.load()
    }

    var canRun: Bool { runOnPhone && config != nil }
    var busy: Bool { jobs.contains { $0.state == .running } }
    /// 还没跑完的（排队的和正在跑的）
    var pending: Int { jobs.filter { $0.state == .queued || $0.state == .running }.count }

    // MARK: 加任务

    func review(kind: String, days: Int, topic: String, months: Int, vault: Vault) {
        let spec = SparkTask.review(kind, days: days, topic: topic, months: months)
        add(title: spec.title, kind: "review",
            params: ["kind": kind, "days": "\(days)", "topic": topic, "months": "\(months)"], vault: vault)
    }

    func picked(paths: [String], title: String, vault: Vault) {
        add(title: title, kind: "review", params: ["kind": "picked", "paths": paths.joined(separator: "\n")], vault: vault)
    }

    func report(topic: String, scope: String, vault: Vault) {
        add(title: "出报告：\(topic)", kind: "report", params: ["topic": topic, "scope": scope], vault: vault)
    }

    private func add(title: String, kind: String, params: [String: String], vault: Vault) {
        // 同一件事已经在排队或在跑，就不重复加
        guard !jobs.contains(where: { $0.kind == kind && $0.params == params && ($0.state == .queued || $0.state == .running) })
        else { return }
        jobs.insert(Job(id: UUID().uuidString.lowercased(), title: title, kind: kind, params: params), at: 0)
        save()
        resume(vault: vault)
    }

    // MARK: 管理

    /// 停掉正在跑的，或者从队列里拿掉
    func remove(_ id: String) {
        if jobs.first(where: { $0.id == id })?.running == true { task?.cancel() }
        jobs.removeAll { $0.id == id }
        save()
    }

    func retry(_ id: String, vault: Vault) {
        guard let i = jobs.firstIndex(where: { $0.id == id }), jobs[i].state == .failed else { return }
        if jobs[i].path != nil {
            // 手机上已经写好了，只是没交出去：重新交
            Task { await deliver(id, vault: vault) }
            return
        }
        update(id) { $0.state = .queued; $0.status = "排队中" }
        resume(vault: vault)
    }

    /// 把跑完的（做好的和没成功的）从列表里清掉；文件还在书架上
    func clearFinished() {
        jobs.removeAll { $0.state == .done || ($0.state == .failed && $0.path == nil) }
        save()
    }

    /// 回到前台、加了任务时调：没在跑就接着跑队列里最早的那件
    func resume(vault: Vault) {
        self.vault = vault
        guard task == nil, canRun, let next = jobs.last(where: { $0.state == .queued }), let config else { return }
        run(next, config: config, vault: vault)
    }

    // MARK: 跑

    private func run(_ job: Job, config: LLM.Config, vault: Vault) {
        update(job.id) { $0.state = .running; $0.status = "开始" }
        UIApplication.shared.isIdleTimerDisabled = true
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "spark-local-job") { [weak self] in
            Task { @MainActor in self?.endBackground() }
        }
        let lib = LocalNotes(root: Vault.root)
        let work = Self.work(job, taken: Set(vault.tree.keys))
        let id = job.id
        task = Task { [weak self] in
            let progress: TopicSearch.Progress = { text in
                Task { @MainActor in
                    self?.update(id, save: false) { if $0.running { $0.status = text } }
                }
            }
            do {
                let out = try await Task.detached(priority: .userInitiated) { try await work(lib, config, progress) }.value
                try vault.addGenerated(path: out.path, text: out.content)
                self?.update(id) { $0.path = out.path; $0.status = "写好了，交给电脑存进笔记库" }
                await self?.deliver(id, content: out.content, title: out.title, vault: vault)
            } catch is CancellationError {
                // 用户点了停止：remove 已经把它拿掉了
            } catch {
                self?.update(id) { $0.state = .failed; $0.status = error.localizedDescription }
            }
            self?.finish(vault: vault)
        }
    }

    private static func work(_ job: Job, taken: Set<String>)
        -> @Sendable (LocalNotes, LLM.Config, @escaping TopicSearch.Progress) async throws -> ReviewRunner.Output {
        let p = job.params
        if job.kind == "report" {
            let scope = p["scope"] ?? ""
            let folder = scope.isEmpty ? "" : "Spark/\(scope)"    // 跟电脑端一样：Spark/ 下的一层文件夹
            return { lib, c, progress in
                try await ReportRunner.run(topic: p["topic"] ?? "", folder: folder, lib: lib, config: c,
                                           exists: { taken.contains($0) || FileManager.default.fileExists(atPath: lib.url($0).path) },
                                           progress: progress)
            }
        }
        return { lib, c, progress in
            try await ReviewRunner.run(kind: p["kind"] ?? "reading", days: Int(p["days"] ?? "") ?? 7,
                                       months: Int(p["months"] ?? "") ?? 6, topic: p["topic"] ?? "",
                                       paths: (p["paths"] ?? "").split(separator: "\n").map(String.init),
                                       lib: lib, config: c, progress: progress)
        }
    }

    /// 交给电脑存进笔记库（Vault.submitTask：GitHub 交不上就放发件箱）
    private func deliver(_ id: String, content: String? = nil, title: String? = nil, vault: Vault) async {
        guard let job = jobs.first(where: { $0.id == id }), let path = job.path else { return }
        guard let text = content ?? vault.localText(path) else {
            update(id) { $0.state = .failed; $0.deliverError = "手机上的文件找不到了" }
            return
        }
        let name = title ?? (path as NSString).lastPathComponent
        do {
            let route = try await vault.submitTask((title: "存进笔记库：\(name)", task: ["type": "save", "path": path, "content": text]))
            update(id) { j in
                j.state = .done
                j.deliverError = nil
                if case .outbox(let oid) = route { j.outbox = oid } else { j.viaGitHub = true }
            }
        } catch {
            update(id) { $0.state = .failed; $0.deliverError = error.localizedDescription }
        }
    }

    private func finish(vault: Vault) {
        task = nil
        UIApplication.shared.isIdleTimerDisabled = false
        endBackground()
        resume(vault: vault)
    }

    private func endBackground() {
        if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask); bgTask = .invalid }
    }

    private func update(_ id: String, save persist: Bool = true, _ change: (inout Job) -> Void) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        change(&jobs[i])
        jobs[i].updated = .now
        if persist { save() }
    }

    private func save() {
        // 做好的只留最近 20 件
        var done = 0
        jobs = jobs.filter { j in
            guard j.state == .done else { return true }
            done += 1
            return done <= Self.keepDone
        }
        try? FileManager.default.createDirectory(at: Self.fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(jobs) { try? data.write(to: Self.fileURL, options: .atomic) }
    }

    // MARK: 给人看的

    /// 一件事现在到哪了：在手机上生成的进度，或者生成好后交没交到电脑上
    func line(_ j: Job, results: [String: [String: Any]]) -> (text: String, error: Bool) {
        switch j.state {
        case .queued: return (j.status, false)
        case .running: return (j.status, false)
        case .failed:
            if let e = j.deliverError { return ("手机上已经写好；交给电脑时出错：\(e)", true) }
            return ("没成功：\(j.status)", true)
        case .done:
            if let oid = j.outbox {
                guard let r = results[oid] else { return ("手机上已经写好，下次接数据线同步时存进电脑", false) }
                switch r["state"] as? String {
                case "done": return ("已存进电脑上的笔记库", false)
                case "failed": return ("存进电脑时出错：\(r["text"] as? String ?? "")", true)
                default: return ("电脑收到了，正在存", false)
                }
            }
            return ("手机上已经写好，已交给电脑（经 GitHub），存好后同步回来", false)
        }
    }
}

/// 复盘页、任务页上列出手机上生成的事：排队的、在跑的、做好的交没交到电脑上
struct LocalJobRows: View {
    @Environment(LocalJobs.self) private var jobs
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    var kind: String?                 // 只列一种（复盘页只列复盘）
    var hideRunning = false           // 任务页上在跑的那件已经在进展卡片里了

    var body: some View {
        let results = vault.cableTaskResults()
        let rows = jobs.jobs.filter { (kind == nil || $0.kind == kind) && !(hideRunning && $0.running) }
        ForEach(rows) { job in
            let line = jobs.line(job, results: results)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(Markdown.latinQuotes(job.title)).font(.heading(15)).foregroundStyle(p.ink)
                    Spacer()
                    if job.state == .failed {
                        Button("重试") { jobs.retry(job.id, vault: vault) }.font(.system(size: 14)).foregroundStyle(p.accent)
                    }
                    if job.state == .queued || job.running {
                        Button(job.running ? "停止" : "不跑了") { jobs.remove(job.id) }
                            .font(.system(size: 14)).foregroundStyle(p.accent)
                    }
                }
                .buttonStyle(.borderless)
                HStack(spacing: 8) {
                    if job.running { ProgressView().controlSize(.small) }
                    Text(line.text).font(.system(size: 13)).foregroundStyle(line.error ? .red : p.ink2)
                }
                if job.running {
                    Text("在手机上生成，要几分钟。切到后台久了会被系统打断，回到 App 会接着跑，读过的材料不会重复花钱。")
                        .font(.system(size: 12)).foregroundStyle(p.ink2)
                } else if job.state == .queued, !jobs.canRun {
                    Text("要在「我的」里填好模型 Key、打开「在手机上生成」才会跑。").font(.system(size: 12)).foregroundStyle(p.ink2)
                }
            }
            .padding(.vertical, 4)
            .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
            .swipeActions {
                if !job.running { Button("移除", role: .destructive) { jobs.remove(job.id) } }
            }
        }
        if rows.contains(where: { $0.state == .done }) {
            Button("清掉做好的") { jobs.clearFinished() }
                .font(.system(size: 13)).foregroundStyle(p.accent).buttonStyle(.borderless)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
    }
}
