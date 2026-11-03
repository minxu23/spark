import SwiftUI
import UIKit

/// 在手机上跑的复盘和出报告：一次只跑一件。跑完写进手机上的笔记库（书架、复盘页马上能看），
/// 再交一个「存进笔记库」的任务给电脑（type: save，见 core/remote_tasks.py），电脑存好后同步回来就是同一份。
///
/// 跑的时候屏幕不自动锁；切到后台系统只多给几十秒，回来时要是被中断了就重新点一次——
/// 已经摘取过的摘要卡有缓存，不会重复花钱。
@MainActor
@Observable
final class LocalJobs {
    struct Job {
        let title: String
        let kind: String           // review / report
        var status: String
        var running = true
        var error: String?
        var output: ReviewRunner.Output?
        var delivered: String?
    }

    private(set) var job: Job?
    private var task: Task<Void, Never>?
    private var bgTask: UIBackgroundTaskIdentifier = .invalid

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
    var busy: Bool { job?.running == true }

    func review(kind: String, days: Int, topic: String, months: Int, vault: Vault) {
        let spec = SparkTask.review(kind, days: days, topic: topic, months: months)
        start(title: spec.title, kind: "review", vault: vault) { lib, c, progress in
            try await ReviewRunner.run(kind: kind, days: days, months: months, topic: topic, lib: lib, config: c, progress: progress)
        }
    }

    func report(topic: String, scope: String, vault: Vault) {
        let folder = scope.isEmpty ? "" : "Spark/\(scope)"    // 跟电脑端一样：Spark/ 下的一层文件夹
        let taken = Set(vault.tree.keys)
        start(title: "出报告：\(topic)", kind: "report", vault: vault) { lib, c, progress in
            try await ReportRunner.run(topic: topic, folder: folder, lib: lib, config: c,
                                       exists: { taken.contains($0) || FileManager.default.fileExists(atPath: lib.url($0).path) },
                                       progress: progress)
        }
    }

    func cancel() {
        task?.cancel()
    }

    func dismiss() {
        if job?.running != true { job = nil }
    }

    private func start(title: String, kind: String, vault: Vault,
                       work: @escaping @Sendable (LocalNotes, LLM.Config, @escaping TopicSearch.Progress) async throws -> ReviewRunner.Output) {
        guard !busy, let config else { return }
        job = Job(title: title, kind: kind, status: "开始")
        UIApplication.shared.isIdleTimerDisabled = true
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "spark-local-job") { [weak self] in
            Task { @MainActor in self?.endBackground() }
        }
        let lib = LocalNotes(root: Vault.root)
        task = Task { [weak self] in
            let progress: TopicSearch.Progress = { text in
                Task { @MainActor in
                    if self?.job?.running == true { self?.job?.status = text }
                }
            }
            do {
                let out = try await Task.detached(priority: .userInitiated) { try await work(lib, config, progress) }.value
                guard let self else { return }
                try vault.addGenerated(path: out.path, text: out.content)
                self.job?.output = out
                self.job?.status = "写好了"
                self.job?.delivered = await Self.deliver(out, vault: vault)
            } catch is CancellationError {
                self?.job?.error = "已停止"
            } catch {
                self?.job?.error = error.localizedDescription
            }
            self?.job?.running = false
            self?.finish()
        }
    }

    /// 交给电脑存进笔记库。GitHub 的 Issue 正文有长度上限，太长的放发件箱，等接数据线时交
    private static func deliver(_ out: ReviewRunner.Output, vault: Vault) async -> String {
        let spec = (title: "存进笔记库：\(out.title)", task: ["type": "save", "path": out.path, "content": out.content] as [String: Any])
        do {
            if vault.api != nil, out.content.count > 60_000 {
                try SparkTask.enqueue(spec)
                return "篇幅太长，走 GitHub 交不过去：已放进发件箱，下次接数据线同步时存进电脑上的笔记库。"
            }
            try await vault.submitTask(spec)
            return vault.api == nil ? "下次接数据线同步时存进电脑上的笔记库。" : "已交给电脑，存进笔记库后会同步回来。"
        } catch {
            return "手机上已经存好；交给电脑时出错：\(error.localizedDescription)"
        }
    }

    private func finish() {
        task = nil
        UIApplication.shared.isIdleTimerDisabled = false
        endBackground()
    }

    private func endBackground() {
        if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask); bgTask = .invalid }
    }
}

/// 复盘页、任务页上显示正在手机上跑的那件事
struct LocalJobRow: View {
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p

    var body: some View {
        if let job = jobs.job {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(Markdown.latinQuotes(job.title)).font(.heading(15)).foregroundStyle(p.ink)
                    Spacer()
                    if job.running {
                        Button("停止") { jobs.cancel() }.font(.system(size: 14)).foregroundStyle(p.accent)
                    } else {
                        Button("知道了") { jobs.dismiss() }.font(.system(size: 14)).foregroundStyle(p.accent)
                    }
                }
                HStack(spacing: 8) {
                    if job.running { ProgressView().controlSize(.small) }
                    Text(job.error.map { "没成功：\($0)" } ?? job.status)
                        .font(.system(size: 13)).foregroundStyle(job.error == nil ? p.ink2 : .red)
                }
                if let d = job.delivered { Text(d).font(.system(size: 12.5)).foregroundStyle(p.ink2) }
                if job.running {
                    Text("在手机上生成，要几分钟。别切走 App：切到后台久了会被系统打断，回来重新点一次就行，读过的材料不会重复花钱。")
                        .font(.system(size: 12)).foregroundStyle(p.ink2)
                }
            }
            .padding(.vertical, 4)
        }
    }
}
