import SwiftUI

/// 复盘：底部单独一个标签。阅读复盘一点就交；来龙去脉、观点检验只填一个主题。
/// 复盘在电脑上生成（core/review.py），写好后同步回来，列在下面。
struct ReviewView: View {
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var sheet: String?          // topic / claims
    @State private var sending = false
    @State private var note: String?
    @State private var pending: [(title: String, text: String)] = []
    @State private var deleting: Doc?
    @State private var renaming: Doc?

    private var reviews: [Doc] {
        vault.library.reports.filter { $0.source == "复盘" }.sorted { ($0.date, $0.path) > ($1.date, $1.path) }
    }

    /// 手机上近 7 天读了几篇（同一期按一篇算）
    @State private var phoneReads = 0
    nonisolated private static func countReads() -> Int {
        let since = Date().addingTimeInterval(-7 * 86400).timeIntervalSince1970
        var keys = Set<String>()
        for line in ReadLog.lines() {
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  (obj["at"] as? Double ?? 0) >= since else { continue }
            keys.insert("\(obj["show"] ?? "")#\(obj["entry"] ?? obj["path"] ?? "")")
        }
        return keys.count
    }

    /// 近 30 天存的剪藏（文件名前面是存的日期）
    private var recentClips: Int {
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd"
        let lo = f.string(from: Date().addingTimeInterval(-30 * 86400))
        return vault.library.clips.filter { $0.date >= lo }.count
    }

    /// 笔记：一篇剪藏一篇读书笔记（划线、批注、读后感），最近改过的在前
    @State private var notes: [ReadingNote] = []
    /// 随手翻一条：从所有划线里随便挑一处
    @State private var pick: (note: ReadingNote, item: ReadingNote.Item)?
    @State private var choosing: String?           // reading / clips：选时间段
    @AppStorage("review.tab") private var tab = 0  // 0 笔记，1 复盘报告
    /// 笔记列表只看有批注的（设置页点「条批注」进来就是这样）
    @AppStorage("review.onlyNoted") private var onlyNoted = false

    /// 读文件放后台（文件没变的走缓存）；随手翻的那条还在就不换，免得每次回到这页都跳
    private func loadNotes() async {
        let fresh = await vault.readingNotes()
        let reads = await Task.detached { Self.countReads() }.value
        guard !Task.isCancelled else { return }
        notes = fresh
        phoneReads = reads
        if let cur = pick, let n = fresh.first(where: { $0.id == cur.note.id }), let it = n.items.first(where: { $0.mark == cur.item.mark }) {
            pick = (note: n, item: it)
        } else { shuffle() }
    }

    private func shuffle() {
        let all = notes.flatMap { n in n.items.map { (n, $0) } }
        pick = all.randomElement().map { (note: $0.0, item: $0.1) }
    }

    var body: some View {
        List {
            PageStatus(text: reviewNote)
            SectionTabs(items: [(0, "笔记"), (1, "报告")], selection: $tab)

            if let note {
                Text(note).font(.system(size: 13.5)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            if jobs.jobs.contains(where: { $0.kind == "review" }) {
                Section {
                    LocalJobRows(kind: "review")
                } header: { header("在手机上生成") }
            }

            if !pending.isEmpty {
                Section {
                    ForEach(Array(pending.enumerated()), id: \.offset) { _, t in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(Markdown.latinQuotes(t.title)).font(.heading(15)).foregroundStyle(p.ink)
                            Text(t.text).font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        .padding(.vertical, 4)
                        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                    }
                } header: { header("生成中") }
            }

            if tab == 0 {
                notesSection
            } else {
                generators
                reportsSection
            }

            if tab == 1 {
                Text(jobs.canRun ? "在手机上生成，同步时存进电脑"
                     : vault.api == nil ? "交给电脑生成，接数据线时带回" : "交给电脑生成，写好后同步回来")
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2).lineLimit(1)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
        }
        .confirmationDialog(choosing == "clips" ? "剪藏复盘：看多长时间" : "阅读复盘：看多长时间",
                            isPresented: Binding(get: { choosing != nil }, set: { if !$0 { choosing = nil } }), titleVisibility: .visible) {
            if choosing == "clips" {
                Button("近 30 天") { generate("clips", 30) }
                Button("近 90 天") { generate("clips", 90) }
            } else {
                Button("近 7 天") { generate("reading", 7) }
                Button("近 30 天") { generate("reading", 30) }
            }
        } message: {
            Text(choosing == "clips" ? "存下来的文章归成几个话题、各自要点、关注点怎么变的。"
                                     : "手机上近 7 天读了 \(phoneReads) 篇。读了什么、主要收获、反复出现的话题。")
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Section("阅读复盘") {
                        Button("近 7 天") { generate("reading", 7) }
                        Button("近 30 天") { generate("reading", 30) }
                    }
                    Section("剪藏复盘") {
                        Button("近 30 天") { generate("clips", 30) }
                        Button("近 90 天") { generate("clips", 90) }
                    }
                    Button("主题来龙去脉…", systemImage: "point.topleft.down.to.point.bottomright.curvepath") { sheet = "topic" }
                    Button("观点检验…", systemImage: "checkmark.seal") { sheet = "claims" }
                } label: { Image(systemName: "plus").font(.system(size: 17, weight: .semibold)) }
                .accessibilityLabel("生成复盘")
            }
        }
        .task(id: vault.library.clips.count) { await loadNotes() }
        .onAppear { if !notes.isEmpty { Task { await loadNotes() } } }
        .readableColumn()
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("复盘")
        .refreshable {
            await vault.sync()
            await loadPending()
        }
        .task { await loadPending() }
        .navigationDestination(for: Doc.self) { DocReaderView(doc: $0) }
        .navigationDestination(for: ReadingNoteRef.self) { ReadingNoteView(path: $0.path, episode: $0.episode) }
        .navigationDestination(for: Episode.self) { ReaderView(episode: $0) }
        .navigationDestination(for: DocFocus.self) { DocReaderView(doc: $0.doc, focus: $0.focus) }
        .navigationDestination(for: EpisodeFocus.self) { ReaderView(episode: $0.episode, focusPath: $0.path, focus: $0.focus.isEmpty ? nil : $0.focus) }
        .deleteReviewDialog($deleting) { note = $0 }
        .sheet(item: $renaming) { d in
            RenameSheet(current: d.title) { t in Task { await rename(d, t) } }
        }
        .sheet(item: Binding(get: { sheet.map(SheetKind.init) }, set: { sheet = $0?.id })) { k in
            TopicReviewSheet(kind: k.id, local: jobs.canRun) { spec in
                if jobs.canRun {
                    let t = spec.task
                    queueNote()
                    jobs.review(kind: k.id, days: 7, topic: t["topic"] as? String ?? "", months: t["months"] as? Int ?? 6, vault: vault)
                    sheet = nil
                } else {
                    await send(spec)
                }
            }
            .presentationDetents([.medium])
        }
    }

    private struct SheetKind: Identifiable { let id: String }

    private func header(_ t: String) -> some View {
        Text(t).font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
    }


    /// 大标题下的一句话：这几天读了多少、留下了多少笔记
    private var reviewNote: String {
        let marks = notes.reduce(0) { $0 + $1.items.count }, n = notes.reduce(0) { $0 + $1.noteCount }
        return "近 7 天读 \(phoneReads) 篇，\(notes.count) 篇笔记，划线 \(marks) 处，批注 \(n) 条"
    }

    /// 复盘报告上面的四个生成入口（右上角 + 里也有）
    private var generators: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
            tile("阅读复盘", "这段时间读了什么、收获", "book", filled: true) { choosing = "reading" }
            tile("剪藏复盘", "近 30 天存了 \(recentClips) 篇", "bookmark") { choosing = "clips" }
            tile("来龙去脉", "一个主题怎么演变", "point.topleft.down.to.point.bottomright.curvepath") { sheet = "topic" }
            tile("观点检验", "当初的判断应验了没", "checkmark.seal") { sheet = "claims" }
        }
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 10, trailing: 20))
    }

    /// 卡片左上角的类别：看标题开头
    private func reviewKind(_ title: String) -> String {
        // 看开头：标题后半截是模型写的主题，里面可能正好带着「综合」「复盘」这些字
        for (k, v) in [("阅读复盘", "阅读复盘"), ("剪藏复盘", "剪藏复盘"), ("来龙去脉", "来龙去脉"), ("主题来龙去脉", "来龙去脉"),
                       ("观点检验", "观点检验"), ("综合小结", "综合小结"), ("剪藏综合小结", "综合小结")] where title.hasPrefix(k) { return v }
        return "复盘"
    }

    private func generate(_ kind: String, _ days: Int) {
        if jobs.canRun {
            queueNote()
            jobs.review(kind: kind, days: days, topic: "", months: 6, vault: vault)
        } else {
            Task { await send(SparkTask.review(kind, days: days, topic: "", months: 6)) }
        }
    }

    private func tile(_ title: String, _ desc: String, _ icon: String, filled: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                Image(systemName: icon).font(.system(size: 17)).foregroundStyle(filled ? p.paper : p.accent)
                    .frame(height: 22, alignment: .leading)
                Text(title).font(.heading(15.5)).foregroundStyle(filled ? p.paper : p.ink)
                Text(desc).font(.system(size: 12)).foregroundStyle(filled ? p.paper.opacity(0.85) : p.ink2).lineLimit(1)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(filled ? p.accent : p.paper2, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(filled ? p.accent : p.rule))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(sending)
        .accessibilityHint(desc)
    }

    // MARK: 笔记

    @ViewBuilder
    private var notesSection: some View {
        if notes.isEmpty {
            VStack(spacing: 10) {
                Image(systemName: "highlighter").font(.system(size: 26)).foregroundStyle(p.accent.opacity(0.7))
                Text("读剪藏、整理稿、会议资料、报告时长按选字，点「划重点」或「批注」，读完写几句读后感，就成了这篇的笔记。电脑上划的也会列在这里")
                    .font(.system(size: 13.5)).foregroundStyle(p.ink2).multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 40)
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } else {
            if let pick, !onlyNoted { randomCard(pick.note, pick.item) }
            if notes.contains(where: { $0.noteCount > 0 }) || onlyNoted {
                Picker("看哪些", selection: $onlyNoted) {
                    Text("全部 \(notes.count)").tag(false)
                    Text("有批注 \(notes.filter { $0.noteCount > 0 }.count)").tag(true)
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 4, trailing: 20))
            }
            let shown = onlyNoted ? notes.filter { $0.noteCount > 0 } : notes
            if shown.isEmpty {
                Text("还没有批注。读的时候长按选字，点「批注」写下想法。").font(.system(size: 13.5)).foregroundStyle(p.ink2)
                    .frame(maxWidth: .infinity).padding(.vertical, 30)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            let _ = CrashLog.list("复盘页·笔记", shown.map(\.id))
            ForEach(shown) { n in ReadingNoteRow(note: n) }
        }
    }

    /// 随手翻一条：读过的东西隔一阵再看一眼
    private func randomCard(_ n: ReadingNote, _ it: ReadingNote.Item) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("随手翻一条").font(.system(size: 12.5, weight: .medium)).foregroundStyle(p.ink2)
                Spacer()
                Button { withAnimation(.easeOut(duration: 0.2)) { shuffle() } } label: {
                    Label("换一条", systemImage: "arrow.triangle.2.circlepath").font(.system(size: 12.5))
                }
                .buttonStyle(.borderless).foregroundStyle(p.accent)
            }
            let card = VStack(alignment: .leading, spacing: 8) {
                Text(String(Markdown.inline(it.mark).characters)).font(prefs.body(0.95)).lineSpacing(prefs.size * 0.5)
                    .foregroundStyle(p.ink).lineLimit(5)
                Text(Markdown.latinQuotes(n.meta.title)).font(.system(size: 12.5)).foregroundStyle(p.ink2).lineLimit(1)
            }
            if let f = n.focus(it.anchor) { PlainLink(value: f) { card } } else { PlainLink(value: DocFocus(doc: n.target, focus: it.anchor)) { card } }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.yellow.opacity(0.13), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.yellow.opacity(0.35)))
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 6, leading: 20, bottom: 8, trailing: 20))
    }

    // MARK: 复盘报告

    @ViewBuilder
    private var reportsSection: some View {
        if reviews.isEmpty {
            Text(jobs.canRun ? "还没有复盘。上面点一下，在手机上生成好就在这里。"
                             : "还没有复盘。上面点一下，电脑上的 Spark 生成好后同步回来就在这里。")
                .font(.system(size: 13.5)).foregroundStyle(p.ink2)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        }
        let rows = reviews
        let _ = CrashLog.list("复盘页·报告", rows.map(\.id))
        ForEach(rows) { d in
            // 跟上面的生成入口一样做成卡片，整页一个样子
            PlainLink(value: d) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(reviewKind(d.title)).font(.system(size: 12, weight: .medium)).foregroundStyle(p.accent)
                        PendingBadge(path: d.path)   // 复盘也能在手机上划线、批注，可能没进电脑
                        Spacer()
                        Text(Dates.label(d.date)).font(.system(size: 12)).foregroundStyle(p.ink2)
                    }
                    Text(Markdown.latinQuotes(d.title)).font(.heading(16)).foregroundStyle(p.ink).lineSpacing(2).lineLimit(2)
                    if !d.tldr.isEmpty {
                        Text(d.tldr).font(.system(size: 13)).foregroundStyle(p.ink2).lineLimit(2)
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
            }
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 4, trailing: 20))
            .swipeActions { Button("删除", role: .destructive) { deleting = d } }
            .contextMenu {
                if DocEdit.editable(d.path) {
                    Button("改名称", systemImage: "pencil") { renaming = d }
                }
                Button("删除", systemImage: "trash", role: .destructive) { deleting = d }
            }
        }
    }

    /// 改名称：跟阅读页里的一样，只改文件头 title 和第一行「# 标题」
    private func rename(_ d: Doc, _ t: String) async {
        guard let text = vault.localText(d.path) else { return }
        do { try await vault.saveEdited(path: d.path, text: DocEdit.retitled(text, t)) }
        catch { note = "没存上：\(error.localizedDescription)" }
    }

    private func send(_ spec: (title: String, task: [String: Any])) async {
        sending = true
        defer { sending = false }
        do {
            let route = try await vault.submitTask(spec)
            note = "\(spec.title)：\(vault.routeNote(route))"
            sheet = nil
            await loadPending()
        } catch {
            note = "没交出去：\(error.localizedDescription)"
        }
    }

    /// 手机上正在跑别的：这一份排在后面
    private func queueNote() {
        note = jobs.busy ? "手机上正在生成另一份，这份排在后面，跑完接着跑。" : nil
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
    var local = false                     // 在手机上生成
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
                    Button(sending ? "提交中…" : (local ? "在手机上生成" : "生成")) {
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


extension View {
    /// 删一篇复盘前问一下；删完把结果（或出错）交给 done
    func deleteReviewDialog(_ doc: Binding<Doc?>, done: @escaping (String?) -> Void = { _ in }) -> some View {
        modifier(DeleteReviewDialog(doc: doc, done: done))
    }
}

private struct DeleteReviewDialog: ViewModifier {
    @Binding var doc: Doc?
    let done: (String?) -> Void
    @Environment(Vault.self) private var vault

    func body(content: Content) -> some View {
        content.confirmationDialog(doc.map { Clips.isClip($0.path) ? "删掉这篇剪藏？" : Jottings.isJotting($0.path) ? "删掉这条随手记？" : "删掉这篇复盘？" } ?? "删掉？", isPresented: Binding(get: { doc != nil }, set: { if !$0 { doc = nil } }),
                                   titleVisibility: .visible, presenting: doc) { d in
            Button("删除", role: .destructive) {
                Task {
                    do {
                        try await vault.delete(d)
                        done(nil)
                    } catch {
                        done("手机上删了；告诉电脑时出错：\(error.localizedDescription)")
                    }
                }
            }
        } message: { _ in
            Text(vault.api == nil ? "移进手机的废纸篓，30 天内可以在「设置 → 废纸篓」里放回原处；下次接数据线同步时，电脑上那份移进电脑的废纸篓。"
                                  : "移进手机的废纸篓，30 天内可以在「设置 → 废纸篓」里放回原处；电脑上的 Spark 收到后，把笔记库里那份移进电脑的废纸篓。")
        }
    }
}
