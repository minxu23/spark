import SwiftUI

/// 正在跑的一件事，不管在手机上、数据线交给电脑的，还是 GitHub 上交给电脑的
struct RunningTask: Identifiable {
    let id: String
    let onPhone: Bool
    let title: String
    let step: String          // 这一步在做什么
    let item: String          // 在做哪一条
    var current = 0
    var total = 0
    var started: Date?
    var updated: Date?        // 只有数据线任务有：进度是电脑推过来的，可能已经旧了
    var localID: String?      // 手机上跑的：可以停

    /// 手机拔了线，或者电脑上的 Spark 关了：进度不再更新
    func staleMinutes(now: Date) -> Int? {
        guard let updated else { return nil }
        let m = Int(now.timeIntervalSince(updated) / 60)
        return m >= 3 ? m : nil
    }

    /// 已经做完的占多少：第 10 条正在做，算做完 9 条
    var fraction: Double? {
        total > 0 ? Double(max(0, min(current, total) - 1)) / Double(total) : nil
    }
}

/// 最新、书架、任务三页共用的一份「现在在跑什么」。隔几秒看一眼手机上的队列和电脑推过来的进度文件，
/// 都是本地读，不花流量；GitHub 上的任务隔一分钟问一次。
@MainActor
@Observable
final class RunningTasks {
    private(set) var items: [RunningTask] = []
    private(set) var queued = 0
    private var github: [RunningTask] = []
    private var githubQueued = 0

    func watch(vault: Vault, jobs: LocalJobs) async {
        var tick = 0
        while !Task.isCancelled {
            refresh(vault: vault, jobs: jobs)     // 先显示本地的，不等网络
            if tick % 20 == 0 {
                await refreshGitHub(vault)
                refresh(vault: vault, jobs: jobs)
            }
            tick += 1
            try? await Task.sleep(for: .seconds(3))
        }
    }

    func refresh(vault: Vault, jobs: LocalJobs) {
        var out: [RunningTask] = jobs.jobs.filter(\.running).map {
            RunningTask(id: $0.id, onPhone: true, title: $0.title, step: $0.status, item: "", localID: $0.id)
        }
        var waiting = jobs.jobs.filter { $0.state == .queued }.count + githubQueued
        for (id, r) in vault.cableTaskResults() {
            switch r["state"] as? String {
            case "waiting": waiting += 1
            case "running":
                let subject = r["subject"] as? String ?? ""
                let step = r["step"] as? String ?? ""
                out.append(RunningTask(id: id, onPhone: false, title: subject.isEmpty ? r["title"] as? String ?? "任务" : subject,
                                       step: step.isEmpty ? r["text"] as? String ?? "" : step, item: r["item"] as? String ?? "",
                                       current: r["current"] as? Int ?? 0, total: r["total"] as? Int ?? 0,
                                       started: (r["started_at"] as? Double).map { Date(timeIntervalSince1970: $0) },
                                       updated: (r["updated_at"] as? Double).map { Date(timeIntervalSince1970: $0) }))
            default: break
            }
        }
        items = out + github
        queued = waiting
    }

    private func refreshGitHub(_ vault: Vault) async {
        guard let api = vault.api, let issues = try? await api.tasks() else { return }
        let open = issues.filter { $0.state == "open" }
        var out: [RunningTask] = []
        for issue in open where issue.labelNames.contains("进行中") {
            guard let comments = try? await api.comments(issue.number) else { continue }
            let t = TaskItem.parse(issue, comments)
            let subject = t.status["subject"] as? String ?? ""
            let step = t.status["step"] as? String ?? ""
            out.append(RunningTask(id: "issue-\(issue.number)", onPhone: false, title: subject.isEmpty ? t.subject : subject,
                                   step: step.isEmpty ? t.text : step, item: t.status["item"] as? String ?? "",
                                   current: t.status["current"] as? Int ?? 0, total: t.status["total"] as? Int ?? 0))
        }
        github = out
        githubQueued = open.filter { !$0.labelNames.contains("进行中") && !$0.labelNames.contains("失败") }.count
    }
}

/// 正在跑的任务进展卡片。link：点了去任务页（最新、书架上用）；任务页自己用时带「停止」
struct RunningTaskCards: View {
    @Environment(RunningTasks.self) private var running
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    var link = true

    var body: some View {
        ForEach(Array(running.items.enumerated()), id: \.element.id) { i, t in
            Group {
                if link {
                    ZStack {
                        NavigationLink { TasksView() } label: { EmptyView() }.opacity(0)
                        card(t, last: i == running.items.count - 1)
                    }
                } else {
                    card(t, last: i == running.items.count - 1)
                }
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
        }
    }

    private func card(_ t: RunningTask, last: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(t.onPhone ? "手机上正在跑" : "电脑上正在跑").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                Spacer()
                if t.total > 0 {
                    Text("第 \(t.current) 条，共 \(t.total) 条").font(.system(size: 12.5)).monospacedDigit().foregroundStyle(p.ink2)
                }
                if !link, let id = t.localID {
                    Button("停止") { jobs.remove(id) }.font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
                }
            }
            Text(Markdown.latinQuotes(t.title)).font(.heading(16.5)).foregroundStyle(p.ink).lineLimit(2).truncationMode(.middle)
            if !t.step.isEmpty { Text(t.step).font(.system(size: 14)).foregroundStyle(p.ink) }
            if !t.item.isEmpty {
                Text(Markdown.latinQuotes(t.item)).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineLimit(2)
            }
            if let v = t.fraction {
                ProgressView(value: v).tint(p.accent).padding(.top, 4)
            }
            let foot = footer(t, last: last)
            if !foot.isEmpty { Text(foot).font(.system(size: 12)).foregroundStyle(p.ink2) }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
    }

    private func footer(_ t: RunningTask, last: Bool) -> String {
        var parts: [String] = []
        let now = Date.now
        if let m = t.staleMinutes(now: now) {
            parts.append("这是 \(m) 分钟前的进度，接上电脑后会更新")
        } else if let s = t.started {
            parts.append("已跑 \(max(1, Int(now.timeIntervalSince(s) / 60))) 分钟")
        }
        if last, running.queued > 0 { parts.append("后面还有 \(running.queued) 个") }
        return parts.joined(separator: "，")
    }
}
