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
        var note: String?                // 检查订阅：做完了说一句结果
        var current: Int?                // 检查订阅：第几条
        var total: Int?
        var item: String?                // 正在写哪一篇
        var skipped: [Skipped]?          // 手机上处理不了的节目（没有字幕）：提示用户，要不要交给电脑
        var created = Date.now
        var updated = Date.now

        var running: Bool { state == .running }
    }

    /// 手机上处理不了的一期
    struct Skipped: Codable, Identifiable {
        let id: String
        let title: String
        let url: String
        let folder: String
        let reason: String
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

    /// 检查信息跟进：看这些订阅有没有新内容，每个最多写 maxNew 条小结
    func feeds(_ subs: [Subscription], maxNew: Int, vault: Vault) {
        add(title: subs.count == 1 ? "检查订阅：\(subs[0].name)" : "检查订阅：\(subs.count) 个来源", kind: "feeds",
            params: ["subs": subs.map(\.id).joined(separator: "\n"), "max": "\(maxNew)"], vault: vault)
    }

    /// 处理 YouTube 链接：folder 是放到哪个节目文件夹，空的就按频道名新建
    func link(_ url: String, folder: String, length: String, vault: Vault) {
        add(title: "处理链接：\(url)", kind: "feeds", params: ["link": url, "folder": folder, "length": length, "max": "\(FeedRunner.maxPerRun)"], vault: vault)
    }

    /// 没有字幕的节目用户决定交给电脑：一期一个任务，以后检查时不再提示这几期
    func handOff(_ id: String, vault: Vault) async {
        guard let job = jobs.first(where: { $0.id == id }), let skipped = job.skipped else { return }
        var left: [Skipped] = []
        var handed = EpisodeRunner.handed
        for s in skipped {
            let name = (s.folder as NSString).lastPathComponent
            let exists = FileManager.default.fileExists(atPath: Vault.root.appending(path: s.folder).path)
            if (try? await vault.submitTask(SparkTask.link(s.url, dest: exists ? .show(name) : .newSeries, length: "medium"))) == nil {
                left.append(s)
            } else {
                handed.insert(s.id)
            }
        }
        UserDefaults.standard.set(Array(handed).sorted(), forKey: EpisodeRunner.handedKey)
        update(id) { j in
            j.skipped = left.isEmpty ? nil : left
            if left.isEmpty { j.note = (j.note ?? "") + "；没字幕的已经交给电脑" }
        }
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
        if job.kind == "feeds" {
            task = Task { [weak self] in
                await self?.runFeeds(job, lib: lib, config: config, vault: vault)
                self?.finish(vault: vault)
            }
            return
        }
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

    /// 检查信息跟进：同时看几个源，新内容一篇篇写。写好一篇存一篇，中途被打断回来接着跑时已经写好的会跳过
    private func runFeeds(_ job: Job, lib: LocalNotes, config: LLM.Config, vault: Vault) async {
        let id = job.id
        let wanted = Set((job.params["subs"] ?? "").split(separator: "\n").map(String.init))
        let maxNew = Int(job.params["max"] ?? "") ?? 5
        let length = job.params["length"] ?? "medium"
        var subs = FeedRunner.subscriptions(lib).filter { wanted.contains($0.id) && $0.onPhone }
        update(id, save: false) { $0.status = "看 \(subs.count) 个来源有没有新内容"; $0.total = nil; $0.current = nil; $0.item = nil }
        var fromLink: [(Subscription, Feed.Entry)] = []
        if let link = job.params["link"] {
            do {
                let (sub, entries) = try await EpisodeRunner.linkTarget(link, folder: job.params["folder"] ?? "")
                let done = FeedRunner.processed(lib, folder: sub.folder)
                fromLink = entries.filter { !done.ids.contains($0.id) }.map { (sub, $0) }
                subs = []
            } catch {
                update(id) { $0.state = .failed; $0.status = error.localizedDescription }
                return
            }
        }
        var unreachable: [String] = []
        var todo: [(Subscription, Feed.Entry)] = []
        await withTaskGroup(of: (Subscription, Result<[Feed.Entry], Error>).self) { group in
            var next = subs.makeIterator()
            func add() {
                guard let s = next.next() else { return }
                group.addTask {
                    do {
                        return (s, .success(s.kind == "track" ? try await FeedRunner.newEntries(s, lib: lib)
                                                              : try await EpisodeRunner.newEntries(s, lib: lib)))
                    }
                    catch { return (s, .failure(error)) }
                }
            }
            for _ in 0..<8 { add() }
            while let (s, r) = await group.next() {
                switch r {
                case .success(let new): todo += new.prefix(maxNew).map { (s, $0) }
                case .failure: unreachable.append(s.name)
                }
                add()
            }
        }
        todo += fromLink
        // 大多数订阅从没处理过，全选一次能有几百篇：每次只写最新的这些，剩下的再检查一次接着写
        // 公众号的源没有日期，排在最前面，不然总被有日期的挤掉（每个源最多 maxNew 条，不会一下子占满）
        todo.sort { ($0.1.date.isEmpty ? "99999999" : $0.1.date) > ($1.1.date.isEmpty ? "99999999" : $1.1.date) }
        let left = max(0, todo.count - FeedRunner.maxPerRun)
        todo = Array(todo.prefix(FeedRunner.maxPerRun))
        var wrote = 0, outbox = 0, failed = 0, inRow = 0, scripts = 0, noScript = 0
        var skipped: [Skipped] = []
        var lastError = ""
        for (i, (sub, entry)) in todo.enumerated() {
            if Task.isCancelled { break }
            update(id, save: false) { $0.status = sub.kind == "track" ? "写小结" : sub.source_type == "youtube" ? "取字幕、写小结和整理稿" : "写小结和整理稿"; $0.current = i + 1; $0.total = todo.count; $0.item = "\(sub.name)：\(entry.title)" }
            do {
                let rebuild = (i + 1) % 5 == 0
                let spec: (title: String, task: [String: Any])
                if sub.kind == "track" {
                    let item = try await Task.detached(priority: .userInitiated) { try await FeedRunner.process(sub, entry, config: config) }.value
                    try vault.addProcessed(folder: item.folder, files: [(item.name, item.text)], id: item.entry.id, row: item.row, series: false, rebuild: rebuild)
                    spec = item.task
                } else {
                    let outcome = try await Task.detached(priority: .userInitiated) { try await EpisodeRunner.process(sub, entry, config: config, length: length) }.value
                    guard case .written(let item) = outcome else {
                        if case .noText(let title, let why) = outcome {
                            skipped.append(Skipped(id: entry.id, title: title, url: entry.url, folder: sub.folder, reason: why))
                        }
                        continue
                    }
                    try vault.addProcessed(folder: item.folder, files: item.files, id: item.id, row: item.row, series: true, rebuild: rebuild)
                    spec = item.task
                    if item.speech != nil { scripts += 1 }
                    if let why = item.speechError { noScript += 1; lastError = why }
                }
                wrote += 1
                inRow = 0
                if case .outbox = try await vault.submitTask(spec) { outbox += 1 }
            } catch {
                if Task.isCancelled { break }
                failed += 1
                inRow += 1
                lastError = error.localizedDescription
                // 接连失败多半是模型 Key 或网络的问题，不用一条条试下去
                if inRow >= 3 { break }
            }
        }
        vault.saveFeedItems()
        if Task.isCancelled { return }
        if wrote == 0, inRow >= 3 {
            update(id) { $0.state = .failed; $0.status = lastError; $0.item = nil }
            return
        }
        var parts: [String] = []
        if wrote > 0 { parts.append("写了 \(wrote) 篇小结，在书架上") }
        if scripts > 0 { parts.append("\(scripts) 期有整理稿") }
        if noScript > 0 { parts.append("\(noScript) 期的整理稿没写成：\(lastError)") }
        if !skipped.isEmpty { parts.append("\(skipped.count) 期没有字幕或文字稿，手机上处理不了") }
        if todo.isEmpty { parts.append("没有新内容") }
        if outbox > 0 { parts.append("\(outbox) 篇等下次接数据线时存进电脑") }
        if left > 0 { parts.append("还有 \(left) 篇没写，再检查一次接着写") }
        if failed > 0 { parts.append("\(failed) 篇没写成：\(lastError)") }
        if !unreachable.isEmpty { parts.append("\(unreachable.count) 个来源打不开：\(unreachable.prefix(3).joined(separator: "、"))") }
        update(id) { $0.state = .done; $0.note = parts.joined(separator: "；"); $0.item = nil; $0.current = nil; $0.total = nil
            $0.skipped = skipped.isEmpty ? nil : skipped }
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
            if let n = j.note { return (n, false) }
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
                if let skipped = job.skipped, !skipped.isEmpty {
                    Text(skipped.prefix(3).map { "《\($0.title)》" }.joined(separator: "、") + (skipped.count > 3 ? " 等 \(skipped.count) 期" : ""))
                        .font(.system(size: 13)).foregroundStyle(p.ink2).lineLimit(3)
                    Button("交给电脑处理") { Task { await jobs.handOff(job.id, vault: vault) } }
                        .font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
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
