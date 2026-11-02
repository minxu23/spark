import SwiftUI

/// 一个任务 = 同步仓库里带 spark-task 标签的 Issue（电脑端见 core/remote_tasks.py）。
/// 状态看标签和电脑写回的那条评论：评论第一行是 <!-- spark-status {...} -->。
struct TaskItem: Identifiable {
    let issue: Issue
    let status: [String: Any]
    let text: String
    var id: Int { issue.number }

    enum State { case waiting, running, failed, done }

    var state: State {
        if issue.state == "closed" { return .done }
        if issue.labelNames.contains("失败") { return .failed }
        if issue.labelNames.contains("进行中") { return .running }
        return .waiting
    }

    var kind: String {
        switch task["type"] as? String {
        case "link": "处理链接"
        case "check": "检查订阅"
        case "report": "笔记洞察报告"
        case "review": "复盘"
        default: "任务"
        }
    }

    var task: [String: Any] {
        guard let body = issue.body,
              let r = body.range(of: #"```spark-task\s*\n([\s\S]*?)\n```"#, options: .regularExpression) else { return [:] }
        let block = body[r].split(separator: "\n").dropFirst().dropLast().joined(separator: "\n")
        return (try? JSONSerialization.jsonObject(with: Data(block.utf8)) as? [String: Any]) ?? [:]
    }

    var subject: String {
        let t = task
        if let url = t["url"] as? String { return url }
        if let subs = t["subscriptions"] as? [String] { return subs.joined(separator: "、") }
        if t["type"] as? String == "review" { return issue.title.replacingOccurrences(of: "复盘：", with: "") }
        if let topic = t["topic"] as? String { return "主题：\(topic)" }
        return issue.title
    }

    var progress: Double? {
        guard let cur = status["current"] as? Int, let total = status["total"] as? Int, total > 0 else { return nil }
        return Double(cur) / Double(total)
    }

    static func parse(_ issue: Issue, _ comments: [IssueComment]) -> TaskItem {
        let prefix = "<!-- spark-status "
        for c in comments.reversed() where c.body.hasPrefix(prefix) {
            let first = c.body.split(separator: "\n", maxSplits: 1)
            let json = first[0].dropFirst(prefix.count).replacingOccurrences(of: " -->", with: "")
            let meta = (try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]) ?? [:]
            return TaskItem(issue: issue, status: meta, text: first.count > 1 ? String(first[1]) : "")
        }
        return TaskItem(issue: issue, status: [:], text: "")
    }
}

struct TasksView: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var items: [TaskItem] = []
    @State private var loading = false
    @State private var error: String?
    @State private var adding = false

    var body: some View {
        List {
            Text(vault.api == nil
                 ? "现在只用数据线同步：添加的任务先存在手机上，下次接数据线、在电脑上点「同步到这台」时交给电脑。"
                 : "电脑上的 Spark 开着、并在设置里打开了「手机上添加的任务」时才会接手：手动检查、或按设的间隔定时检查。电脑关着时任务会排着。")
                .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
                .padding(12).background(p.chip, in: RoundedRectangle(cornerRadius: 10))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            if let error {
                Text(error).font(.system(size: 14)).foregroundStyle(.red).listRowBackground(Color.clear)
            }
            if items.isEmpty && cable.isEmpty && !loading && error == nil {
                Text("还没有任务。点右上角「添加」交一个给电脑处理。").foregroundStyle(p.ink2).listRowBackground(Color.clear)
            }
            cableSection
            group("进行中", .running)
            group("等电脑接手", .waiting)
            group("没成功", .failed)
            group("完成", .done)
            Text(vault.api == nil ? "" : "每个任务是仓库里的一个 Issue，在 GitHub 网页上也能看到。")
                .font(.system(size: 12.5)).foregroundStyle(p.ink2).listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("任务")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { adding = true } label: { Label("添加", systemImage: "plus") }
            }
        }
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $adding, onDismiss: { Task { await load() } }) { NewTaskView() }
    }

    // MARK: 数据线任务（发件箱，电脑端见 core/cable_sync.py）

    struct CableTask: Identifiable {
        let id: String
        let title: String
        let state: String     // queued（还在手机上）/ waiting / running / done / failed
        let text: String
        let when: Date
    }

    @State private var cable: [CableTask] = []

    private func loadCable() {
        let results = vault.cableTaskResults()
        var out: [CableTask] = []
        for item in Outbox.items() {
            if let r = results[item.id], let state = r["state"] as? String, state == "done" || state == "failed" {
                Outbox.remove(item.id)   // 电脑已经收下并跑完了，手机上不用再留
                continue
            }
            if results[item.id] == nil {
                out.append(CableTask(id: item.id, title: item.title, state: "queued",
                                     text: "等下次接数据线同步时交给电脑", when: item.created))
            }
        }
        for (id, r) in results {
            let when = Date(timeIntervalSince1970: r["updated_at"] as? Double ?? 0)
            out.append(CableTask(id: id, title: r["title"] as? String ?? "任务", state: r["state"] as? String ?? "",
                                 text: r["text"] as? String ?? "", when: when))
        }
        cable = Array(out.sorted { $0.when > $1.when }.prefix(20))
    }

    @ViewBuilder
    private var cableSection: some View {
        if !cable.isEmpty {
            Section {
                Text("电脑跑完一个就推回结果（手机还连着的话），不然下次同步带回来。")
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
                ForEach(cable) { t in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(Markdown.latinQuotes(t.title)).font(.heading(16)).foregroundStyle(p.ink).lineLimit(2)
                        Text(t.text).font(.system(size: 13.5)).lineSpacing(3)
                            .foregroundStyle(t.state == "failed" ? .red : p.ink2)
                    }
                    .padding(.vertical, 6)
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: {
                Text("走数据线的任务").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    @ViewBuilder
    private func group(_ title: String, _ state: TaskItem.State) -> some View {
        let rows = items.filter { $0.state == state }
        if !rows.isEmpty {
            Section {
                ForEach(rows) { row($0) }
            } header: {
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    private func row(_ t: TaskItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(t.kind).font(.system(size: 12.5)).foregroundStyle(p.ink2)
            Text(t.subject).font(.heading(16)).foregroundStyle(p.ink).lineLimit(2)
            Text(line(t)).font(.system(size: 13.5)).lineSpacing(3)
                .foregroundStyle(t.state == .failed ? .red : p.ink2)
            if t.state == .running, let v = t.progress {
                ProgressView(value: v).tint(p.accent)
            }
            if t.state == .failed {
                Button("重试") { Task { await retry(t) } }.buttonStyle(.bordered).padding(.top, 4)
            }
        }
        .padding(.vertical, 6)
        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
    }

    private func line(_ t: TaskItem) -> String {
        if !t.text.isEmpty { return t.text }
        let when = t.issue.created_at.formatted(date: .abbreviated, time: .shortened)
        return t.state == .waiting ? "\(when) 添加，等电脑接手" : when
    }

    private func load() async {
        loadCable()
        guard let api = vault.api else { return }
        loading = true
        defer { loading = false }
        do {
            let issues = try await api.tasks()
            items = try await withThrowingTaskGroup(of: TaskItem.self) { group in
                for issue in issues {
                    group.addTask { TaskItem.parse(issue, try await api.comments(issue.number)) }
                }
                var out: [TaskItem] = []
                for try await t in group { out.append(t) }
                return out.sorted { $0.issue.created_at > $1.issue.created_at }
            }
            error = nil
        } catch {
            self.error = "任务列表没拿到：\(error.localizedDescription)"
        }
    }

    private func retry(_ t: TaskItem) async {
        guard let api = vault.api else { return }
        do {
            try await api.removeLabel(t.issue.number, "失败")
            await load()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct NewTaskView: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var kind = 0
    @State private var url = ""
    @State private var dest = "new_series"
    @State private var length = "medium"
    @State private var subs: Set<String> = []
    @State private var maxNew = 5
    @State private var topic = ""
    @State private var scope = ""
    @State private var sending = false
    @State private var error: String?

    private var folders: [Show] {
        (vault.library.shows(.series) + vault.library.shows(.summit))
    }
    private var subscriptions: [Show] {
        vault.library.shows(.track).filter { !$0.episodes.isEmpty } + vault.library.shows(.series)
    }

    var body: some View {
        NavigationStack {
            Form {
                Picker("任务类型", selection: $kind) {
                    Text("处理链接").tag(0)
                    Text("检查订阅").tag(1)
                    Text("出报告").tag(2)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)

                switch kind {
                case 0: linkForm
                case 1: checkForm
                default: reportForm
                }

                if let error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("添加任务").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "提交中…" : (vault.api == nil ? "放进发件箱" : "交给电脑")) { Task { await submit() } }
                        .disabled(sending || !valid)
                }
            }
            .onAppear {
                if url.isEmpty, let s = UIPasteboard.general.string, s.hasPrefix("http"), s.count < 2000 { url = s }
            }
        }
    }

    @ViewBuilder private var linkForm: some View {
        Section {
            TextField("https://…", text: $url)
                .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
        } header: { Text("链接") } footer: {
            Text("YouTube 视频或播放列表、播客单集、Substack 或公众号文章。")
        }
        Section("放到") {
            Picker("文件夹", selection: $dest) {
                Text("新建一个节目文件夹").tag("new_series")
                Text("作为一场会议处理").tag("summit")
                ForEach(folders) { Text($0.name).tag("show:" + $0.name) }
            }
            .pickerStyle(.navigationLink)
        }
        Section {
            Picker("小结篇幅", selection: $length) {
                Text("简洁").tag("short"); Text("标准").tag("medium"); Text("详细").tag("long")
            }
            .pickerStyle(.segmented)
        } header: { Text("小结篇幅") } footer: { Text("模型和整理稿语言用电脑上设置页里的默认值。") }
    }

    @ViewBuilder private var checkForm: some View {
        Section {
            ForEach(subscriptions) { s in
                Button {
                    if subs.contains(s.name) { subs.remove(s.name) } else { subs.insert(s.name) }
                } label: {
                    HStack {
                        Image(systemName: subs.contains(s.name) ? "checkmark.square.fill" : "square").foregroundStyle(p.accent)
                        Text(s.name).foregroundStyle(p.ink)
                        Spacer()
                        Text(s.kind.label).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                }
            }
        } header: { Text("检查哪些订阅有新内容") }
        Section {
            Stepper("每个订阅最多处理 \(maxNew) 条", value: $maxNew, in: 1...20)
        } footer: { Text("找到的新内容按新到旧处理，多出来的写在结果里。") }
    }

    @ViewBuilder private var reportForm: some View {
        Section("主题") {
            TextField("比如：机器人触觉与灵巧手", text: $topic)
        }
        Section {
            Picker("从哪些笔记里找", selection: $scope) {
                Text("整个笔记库，按主题自动选").tag("")
                ForEach(folders) { Text("只看「\($0.name)」").tag($0.name) }
            }
            .pickerStyle(.navigationLink)
        } footer: { Text("和电脑上 Notes2Insight 的「按主题自动选」一样，报告出来后在书架的「报告」里。") }
    }

    private var valid: Bool {
        switch kind {
        case 0: url.hasPrefix("http")
        case 1: !subs.isEmpty
        default: !topic.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    private func submit() async {
        let spec: (title: String, task: [String: Any])
        switch kind {
        case 0:
            let d = SparkTask.Dest(key: dest) ?? .newSeries
            spec = SparkTask.link(url.trimmingCharacters(in: .whitespacesAndNewlines), dest: d, length: length)
        case 1:
            spec = SparkTask.check(Array(subs).sorted(), maxNew: maxNew)
        default:
            spec = SparkTask.report(topic.trimmingCharacters(in: .whitespaces), scope: scope)
        }
        sending = true
        defer { sending = false }
        do {
            try await vault.submitTask(spec)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
