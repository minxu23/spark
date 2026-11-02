import SwiftUI

/// 复盘：底部单独一个标签。阅读复盘一点就交；来龙去脉、观点检验只填一个主题。
/// 复盘在电脑上生成（core/review.py），写好后同步回来，列在下面。
struct ReviewView: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var sheet: String?          // topic / claims
    @State private var sending = false
    @State private var note: String?
    @State private var pending: [(title: String, text: String)] = []

    private var reviews: [Doc] {
        vault.library.reports.filter { $0.source == "复盘" }.sorted { $0.date > $1.date }
    }

    /// 手机上近 7 天读了几篇（同一期按一篇算）
    private var phoneReads: Int {
        let since = Date().addingTimeInterval(-7 * 86400).timeIntervalSince1970
        var keys = Set<String>()
        for line in ReadLog.lines() {
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  (obj["at"] as? Double ?? 0) >= since else { continue }
            keys.insert("\(obj["show"] ?? "")#\(obj["entry"] ?? obj["path"] ?? "")")
        }
        return keys.count
    }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    Text("阅读复盘").font(.heading(18))
                    Text("这段时间在电脑和手机上读了什么、主要收获、反复出现的话题、没读的里面哪些值得补看。手机上近 7 天读了 \(phoneReads) 篇。")
                        .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    HStack(spacing: 10) {
                        readingButton(7, "近 7 天", filled: true)
                        readingButton(30, "近 30 天", filled: false)
                    }
                    .padding(.top, 2)
                }
                .padding(16)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 6, trailing: 20))

                HStack(spacing: 10) {
                    tile("来龙去脉", "一个主题按时间线：说法怎么变的、分歧在哪", "point.topleft.down.to.point.bottomright.curvepath") { sheet = "topic" }
                    tile("观点检验", "当初的判断和预测，后来应验了没有", "checkmark.seal") { sheet = "claims" }
                }
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 6, trailing: 20))

                if let note {
                    Text(note).font(.system(size: 13.5)).foregroundStyle(p.ink2)
                        .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
            }

            if !pending.isEmpty {
                Section {
                    ForEach(Array(pending.enumerated()), id: \.offset) { _, t in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(t.title).font(.heading(15)).foregroundStyle(p.ink)
                            Text(t.text).font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        .padding(.vertical, 4)
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    }
                } header: { header("生成中") }
            }

            Section {
                if reviews.isEmpty {
                    Text("还没有复盘。上面点一下，电脑上的 Spark 生成好后同步回来就在这里。")
                        .font(.system(size: 13.5)).foregroundStyle(p.ink2)
                        .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                ForEach(reviews) { d in
                    PlainLink(value: d) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(d.title).font(.heading(16)).foregroundStyle(p.ink)
                            Text(Dates.label(d.date)).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        }
                        .padding(.vertical, 4)
                    }
                    .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                }
            } header: { header("生成好的复盘") }

            Text(vault.api == nil
                 ? "复盘在电脑上生成：交的请求先放在手机上，下次接数据线同步时交给电脑，写好后再同步回来。"
                 : "复盘在电脑上生成：电脑上的 Spark 开着、并打开了「手机上添加的任务」时接手，写好后同步回来。")
                .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("复盘")
        .refreshable {
            await vault.sync()
            await loadPending()
        }
        .task { await loadPending() }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
        .sheet(item: Binding(get: { sheet.map(SheetKind.init) }, set: { sheet = $0?.id })) { k in
            TopicReviewSheet(kind: k.id) { spec in
                await send(spec)
            }
            .presentationDetents([.medium])
        }
    }

    private struct SheetKind: Identifiable { let id: String }

    private func header(_ t: String) -> some View {
        Text(t).font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
    }

    private func readingButton(_ days: Int, _ label: String, filled: Bool) -> some View {
        Button {
            Task { await send(SparkTask.review("reading", days: days, topic: "", months: 6)) }
        } label: {
            Text(label).font(.system(size: 15, weight: .semibold))
                .frame(maxWidth: .infinity, minHeight: 44)
                .foregroundStyle(filled ? p.paper : p.accent)
                .background(filled ? p.accent : .clear, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.accent))
        }
        .buttonStyle(.plain)
        .disabled(sending)
        .accessibilityLabel("生成\(label)的阅读复盘")
    }

    private func tile(_ title: String, _ desc: String, _ icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: icon).font(.system(size: 20)).foregroundStyle(p.accent)
                    .frame(width: 26, height: 26, alignment: .leading)
                Text(title).font(.heading(16)).foregroundStyle(p.ink)
                Text(desc).font(.system(size: 12.5)).foregroundStyle(p.ink2).lineSpacing(2)
                    .multilineTextAlignment(.leading).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(14)
            .frame(maxWidth: .infinity, minHeight: 128, alignment: .topLeading)
            .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
        }
        .buttonStyle(.plain)
    }

    private func send(_ spec: (title: String, task: [String: Any])) async {
        sending = true
        defer { sending = false }
        do {
            try await vault.submitTask(spec)
            note = vault.api == nil ? "已放进发件箱：\(spec.title)，下次接数据线同步时交给电脑。"
                                    : "已交给电脑：\(spec.title)。"
            sheet = nil
            await loadPending()
        } catch {
            note = "没交出去：\(error.localizedDescription)"
        }
    }

    /// 还没生成好的复盘请求：GitHub 上没关的 Issue，或者发件箱里 / 电脑还在跑的
    private func loadPending() async {
        var out: [(String, String)] = []
        if let api = vault.api, let issues = try? await api.tasks() {
            for issue in issues where issue.state == "open" {
                let t = TaskItem.parse(issue, [])
                guard t.task["type"] as? String == "review" else { continue }
                let state = issue.labelNames.contains("失败") ? "没成功，去「任务」里看原因"
                    : issue.labelNames.contains("进行中") ? "电脑正在生成" : "等电脑接手"
                out.append((issue.title, state))
            }
        }
        let results = vault.cableTaskResults()
        for item in Outbox.items() where item.task["type"] as? String == "review" {
            out.append((item.title, (results[item.id]?["text"] as? String) ?? "等下次接数据线同步时交给电脑"))
        }
        pending = out
    }
}

/// 来龙去脉 / 观点检验：只填一个主题
struct TopicReviewSheet: View {
    let kind: String                      // topic / claims
    let submit: ((title: String, task: [String: Any])) async -> Void
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var topic = ""
    @State private var months = 6
    @State private var sending = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("比如：具身智能、RSI、AI 芯片", text: $topic).focused($focused)
                } header: { Text(kind == "claims" ? "主题（可以不填）" : "主题") } footer: {
                    Text(kind == "claims" ? "不填就检验那段时间以前全部内容里的判断。" : "按主题检索相关笔记，按时间排开写。")
                }
                if kind == "claims" {
                    Picker("检验多久以前的判断", selection: $months) {
                        Text("3 个月以前").tag(3); Text("半年以前").tag(6); Text("一年以前").tag(12)
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle(kind == "claims" ? "观点检验" : "主题来龙去脉").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(sending ? "提交中…" : "生成") {
                        sending = true
                        Task {
                            await submit(SparkTask.review(kind, days: 7, topic: topic.trimmingCharacters(in: .whitespaces),
                                                          months: months))
                            sending = false
                        }
                    }
                    .disabled(sending || (kind == "topic" && topic.trimmingCharacters(in: .whitespaces).isEmpty))
                }
            }
            .onAppear { focused = true }
        }
    }
}
