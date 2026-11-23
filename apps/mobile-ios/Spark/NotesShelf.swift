import SwiftUI

/// 书架「笔记」上点开的：一条随手记（文档页），或一篇读书笔记
enum NoteOpen: Hashable, Identifiable {
    case doc(Doc)
    case note(ReadingNoteRef)
    var id: String {
        switch self {
        case .doc(let d): "doc:" + d.path
        case .note(let r): "note:" + r.path
        }
    }
}

/// 书架「笔记」：最上面一张卡（最近更新、篇数、近 7 天、最近三条，只写数字不调模型），下面随手记，再下面读书笔记。
/// 读书笔记跟复盘页「笔记」是同一份（vault.readingNotes()、ReadingNoteRow）
struct NotesShelf: View {
    /// 卡片上的几条点了打开（一行里有好几处能点，交给书架页去开）
    let open: (NoteOpen) -> Void
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @AppStorage("notes.sort") private var sort = "updated"
    @State private var metas: [Jottings.Meta] = []
    /// 读书笔记：Spark/笔记/ 以外的（跟卡片上的数字一致；划了线的随手记在随手记列表里标出来）
    @State private var reading: [ReadingNote] = []
    /// 划了线、写了批注或读后感的随手记
    @State private var marked: Set<String> = []
    @State private var loaded = false
    @State private var editor: EditTarget?
    @State private var deleting: Doc?
    @State private var note: String?

    private struct EditTarget: Identifiable { let id: String }   // 随手记的路径；空串是新写一条

    static let sorts: [(value: String, label: String)] = [("updated", "按最近修改"), ("created", "按创建时间"), ("name", "按名称")]

    private var sorted: [Jottings.Meta] {
        switch sort {
        case "created": return metas.sorted { ($0.created, $1.path) > ($1.created, $0.path) }
        case "name": return metas.sorted { NameOrder.less($0.title, $1.title) }
        default: return metas.sorted { ($0.updated, $1.path) > ($1.updated, $0.path) }
        }
    }

    private var summary: Jottings.Summary {
        Jottings.summarize(jottings: metas.map { .init(path: $0.path, title: $0.title, created: $0.created, changed: $0.updated) },
                           reading: reading.map { .init(path: $0.meta.path, title: $0.meta.title, created: $0.edited, changed: $0.edited) },
                           now: .now)
    }

    var body: some View {
        // 第一行总在：.task、弹出的编辑页、删除确认都挂在它上面（挂在整组上，列表里每行会各挂一份）
        top
            .task(id: "\(vault.library.notes.count)|\(vault.libraryVersion)") { await load() }
            .onAppear { if loaded { Task { await load() } } }   // 从文档页回来：刚改的、刚加的星标马上反映
            .sheet(item: $editor) { t in
                JottingEditor(path: t.id.isEmpty ? nil : t.id) { _ in
                    note = "已保存"
                    Task { await load() }
                }
            }
            .deleteReviewDialog($deleting) { why in
                note = why
                Task { await load() }
            }

        if let note {
            Text(note).font(.system(size: 13)).foregroundStyle(p.ink2)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        }

        if !metas.isEmpty || loaded {
            Section {
                if metas.isEmpty {
                    Text("还没有随手记。点右边的「写一条」，想到什么写什么。")
                        .font(.system(size: 13.5)).foregroundStyle(p.ink2)
                        .listRowBackground(Color.clear).listRowSeparator(.hidden)
                }
                ForEach(sorted) { m in jottingRow(m) }
            } header: {
                HStack(spacing: 8) {
                    Text("随手记 · \(metas.count)").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
                    Spacer(minLength: 0)
                    Button { editor = EditTarget(id: "") } label: {
                        Label("写一条", systemImage: "square.and.pencil").font(.system(size: 13.5, weight: .medium))
                    }
                    .buttonStyle(.borderless).foregroundStyle(p.accent)
                    if metas.count > 1 { SortMenu(options: Self.sorts, selection: $sort) }
                }
            }
        }

        if !reading.isEmpty {
            Section {
                ForEach(reading) { n in ReadingNoteRow(note: n) }
            } header: {
                Text("读书笔记 · \(reading.count)").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).textCase(nil)
            }
        }
    }

    // MARK: 最上面

    @ViewBuilder
    private var top: some View {
        if !loaded {
            ProgressView().frame(maxWidth: .infinity).padding(.vertical, 20)
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
        } else if metas.isEmpty && reading.isEmpty {
            invitation
        } else {
            card(summary)
        }
    }

    private var invitation: some View {
        VStack(spacing: 12) {
            Image(systemName: "square.and.pencil").font(.system(size: 26)).foregroundStyle(p.accent.opacity(0.7))
            Text("还没有笔记。想到什么随手记一条；读的时候划线、批注、写读后感，也会收在这里。")
                .font(.system(size: 13.5)).foregroundStyle(p.ink2).multilineTextAlignment(.center).lineSpacing(3)
            Button { editor = EditTarget(id: "") } label: {
                Text("写一条").font(.system(size: 15, weight: .semibold)).padding(.horizontal, 22).frame(minHeight: 40)
                    .foregroundStyle(p.paper).background(p.accent, in: Capsule())
            }
            .buttonStyle(.borderless)
        }
        .frame(maxWidth: .infinity).padding(.vertical, 36)
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
    }

    /// 跟会议「最近更新」那张卡一个样子
    private func card(_ s: Jottings.Summary) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("最近更新 · \(s.latest.map { $0.formatted(.relative(presentation: .named)) } ?? "—")")
                .font(.system(size: 12)).foregroundStyle(.white.opacity(0.85))
            Text("随手记 \(s.jottings) 篇 · 读书笔记 \(s.reading) 篇").font(.heading(19)).foregroundStyle(.white).lineSpacing(2)
            Text("近 7 天：新增 \(s.added)，修改 \(s.edited)").font(.system(size: 13)).foregroundStyle(.white.opacity(0.9))
            if !s.recent.isEmpty {
                VStack(spacing: 0) {
                    ForEach(s.recent, id: \.self) { r in
                        Rectangle().fill(.white.opacity(0.18)).frame(height: 0.5)
                        Button { openRecent(r) } label: {
                            HStack(spacing: 8) {
                                Text(Markdown.latinQuotes(r.entry.title)).font(.system(size: 14.5)).foregroundStyle(.white).lineLimit(1)
                                Spacer(minLength: 8)
                                Text(r.kind).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.75))
                                Text(r.entry.changed.formatted(.relative(presentation: .named)))
                                    .font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.75))
                            }
                            .frame(minHeight: 40).contentShape(Rectangle())
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("\(r.kind)：\(r.entry.title)")
                    }
                }
                .padding(.top, 2)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.hex(0x1f3a5f), in: RoundedRectangle(cornerRadius: 16))
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
    }

    private func openRecent(_ r: Jottings.Recent) {
        if r.kind == Jottings.kindJotting {
            open(.doc(doc(r.entry.path, title: r.entry.title)))
        } else {
            open(.note(ReadingNoteRef(path: r.entry.path, episode: reading.first { $0.meta.path == r.entry.path }?.episode)))
        }
    }

    // MARK: 随手记

    private func doc(_ path: String, title: String) -> Doc {
        vault.library.notes.first { $0.path == path } ?? Doc(path: path, kind: .note, source: Jottings.kindJotting, fullTitle: title)
    }

    private func jottingRow(_ m: Jottings.Meta) -> some View {
        PlainLink(value: doc(m.path, title: m.title)) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if m.starred {
                        Image(systemName: "star.fill").font(.system(size: 11)).foregroundStyle(.orange).accessibilityLabel("星标")
                    }
                    if marked.contains(m.path) {
                        Text("有划线/批注").font(.system(size: 11, weight: .medium)).foregroundStyle(p.accent)
                            .padding(.horizontal, 6).padding(.vertical, 1)
                            .background(p.accent.opacity(0.1), in: Capsule())
                    }
                    Spacer()
                    Text(sort == "created" ? "创建于 \(Dates.label(DocTime.day(.at(m.created)).replacingOccurrences(of: "-", with: "")))"
                                           : m.updated.formatted(.relative(presentation: .named)))
                        .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                }
                Text(Markdown.latinQuotes(m.title)).font(.heading(16.5)).foregroundStyle(p.ink).lineSpacing(3)
                if !m.excerpt.isEmpty, m.excerpt != m.title {
                    Text(m.excerpt).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineLimit(2)
                }
            }
            .padding(.vertical, 4)
        }
        .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
        // 跟剪藏一样：右滑星标；左滑删除、编辑
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            Button { Task { await star(m) } } label: {
                Label(m.starred ? "取消星标" : "星标", systemImage: m.starred ? "star.slash.fill" : "star.fill")
            }.tint(.yellow)
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button(role: .destructive) { deleting = doc(m.path, title: m.title) } label: { Label("删除", systemImage: "trash.fill") }
            Button { editor = EditTarget(id: m.path) } label: { Label("编辑", systemImage: "square.and.pencil") }.tint(.blue)
        }
        .contextMenu {
            Button("编辑", systemImage: "square.and.pencil") { editor = EditTarget(id: m.path) }
            Button(m.starred ? "取消星标" : "加星标", systemImage: m.starred ? "star.slash" : "star") { Task { await star(m) } }
            Button("删除", systemImage: "trash", role: .destructive) { deleting = doc(m.path, title: m.title) }
        }
    }

    private func star(_ m: Jottings.Meta) async {
        guard let text = vault.localText(m.path) else { return }
        do {
            try await vault.saveEdited(path: m.path, text: DocEdit.starring(text, !m.starred))
            await load()
        } catch {
            note = "没存上：\(error.localizedDescription)"
        }
    }

    private func load() async {
        let root = Vault.root
        metas = vault.library.notes.compactMap { d in
            vault.localText(d.path).map { text in
                let stamp = try? root.appending(path: d.path, directoryHint: .notDirectory).resourceValues(forKeys: [.contentModificationDateKey])
                return Jottings.meta(path: d.path, text: text, fallback: stamp?.contentModificationDate ?? .now)
            }
        }
        let all = await vault.readingNotes()
        reading = all.filter { !Jottings.isJotting($0.meta.path) }
        marked = Set(all.map(\.meta.path).filter(Jottings.isJotting))
        loaded = true
    }
}

/// 没保存的字：每打一个字记一次（App 被关、不小心划掉编辑页都还在）。键是随手记的路径，新写的是 new
enum JottingDrafts {
    private static let key = "jotting.drafts"

    /// start：草稿是在哪一版上写的（编辑页打开时那份全文；新写的是空串）。保存时照它算 bases
    struct Draft { var title: String; var body: String; var start: String }

    static func get(_ target: String) -> Draft? {
        guard let v = UserDefaults.standard.dictionary(forKey: key)?[target] as? [String], v.count == 3 else { return nil }
        return Draft(title: v[0], body: v[1], start: v[2])
    }

    static func set(_ target: String, _ d: Draft?) {
        var all = UserDefaults.standard.dictionary(forKey: key) ?? [:]
        all[target] = d.map { [$0.title, $0.body, $0.start] }
        UserDefaults.standard.set(all, forKey: key)
    }
}

/// 写一条、改一条随手记：标题（可不填）+ 正文。全屏弹出，「保存」存好交给电脑；「取消」时有没保存的字先问一句
struct JottingEditor: View {
    /// nil：新写一条
    let path: String?
    let onSaved: (String) -> Void
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var text = ""
    @State private var original = (title: "", text: "")
    /// 改的起点：打开时那一版的全文（换上草稿时是草稿写在的那一版）。存的时候照它算 bases，
    /// 编辑页开着的时候同步进来电脑上的改动、或者草稿写在旧版本上，电脑都认得出两边都改过，不会悄悄盖掉
    @State private var start: String?
    @State private var restored: String?
    @State private var confirmDiscard = false
    @State private var saving = false
    @State private var error: String?
    @State private var ready = false
    @FocusState private var focus: Field?

    private enum Field { case title, body }
    private var target: String { path ?? "new" }
    private var changed: Bool { title != original.title || text != original.text }
    private var empty: Bool {
        title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 0) {
                if let restored {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text(restored).font(.system(size: 13)).foregroundStyle(p.ink2)
                        Spacer(minLength: 0)
                        Button("丢掉草稿") {
                            title = original.title
                            text = original.text
                            start = path.flatMap { vault.localText($0) }
                            JottingDrafts.set(target, nil)
                            self.restored = nil
                        }
                        .font(.system(size: 13)).buttonStyle(.borderless)
                    }
                    .padding(.horizontal, 20).padding(.vertical, 8)
                    .background(p.paper2)
                }
                if let error {
                    Text(error).font(.system(size: 13)).foregroundStyle(Color.hex(0xb42318))
                        .padding(.horizontal, 20).padding(.top, 8)
                }
                TextField("标题（可不填，用正文第一行）", text: $title)
                    .font(.heading(20)).foregroundStyle(p.ink)
                    .submitLabel(.next).onSubmit { focus = .body }
                    .focused($focus, equals: .title)
                    .padding(.horizontal, 20).padding(.vertical, 12)
                Rectangle().fill(p.rule).frame(height: 0.5).padding(.horizontal, 20)
                TextEditor(text: $text)
                    .font(prefs.body()).foregroundStyle(p.ink).lineSpacing(prefs.size * 0.4)
                    .scrollContentBackground(.hidden)
                    .focused($focus, equals: .body)
                    .padding(.horizontal, 15).padding(.top, 4)
                    .overlay(alignment: .topLeading) {
                        if text.isEmpty {
                            Text("想到什么写什么，可以用 Markdown").font(prefs.body()).foregroundStyle(p.ink2.opacity(0.7))
                                .padding(.horizontal, 20).padding(.top, 12).allowsHitTesting(false)
                        }
                    }
                if path != nil, text.contains("[^note-") || text.contains("==") {
                    Text("==…== 是划线，[^note-1] 是批注号；删掉批注号，那条批注跟着删。读后感和批注的内容不在这里，不会被改动。")
                        .font(.system(size: 12)).foregroundStyle(p.ink2).lineSpacing(2)
                        .padding(.horizontal, 20).padding(.vertical, 8)
                }
            }
            .background(p.paper.ignoresSafeArea())
            .navigationTitle(path == nil ? "写一条笔记" : "改随手记").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") {
                        if changed { confirmDiscard = true } else { JottingDrafts.set(target, nil); dismiss() }
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "正在保存" : "保存") { Task { await save() } }
                        .fontWeight(.semibold)
                        .disabled(saving || empty || (path != nil && !changed))
                }
            }
            .confirmationDialog("放弃这次修改？", isPresented: $confirmDiscard, titleVisibility: .visible) {
                Button("放弃", role: .destructive) { JottingDrafts.set(target, nil); dismiss() }
                Button("继续写", role: .cancel) {}
            } message: {
                Text("没保存的字会删掉。")
            }
        }
        // 有没保存的字时不让往下一划就关掉；万一关了（或者 App 被关），草稿也还在，下次打开换上
        .interactiveDismissDisabled(changed)
        .onAppear(perform: setup)
        .onChange(of: title) { keepDraft() }
        .onChange(of: text) { keepDraft() }
    }

    private func setup() {
        guard !ready else { return }
        if let path, let current = vault.localText(path) {
            original = (Jottings.meta(path: path, text: current, fallback: .now).title, Jottings.editableBody(current))
            start = current
        }
        title = original.title
        text = original.text
        if let d = JottingDrafts.get(target), d.title != original.title || d.body != original.text {
            title = d.title
            text = d.body
            let moved = path != nil && !d.start.isEmpty && d.start != start
            if path != nil, !d.start.isEmpty { start = d.start }
            restored = moved ? "换上了上次没保存的草稿。这条后来在别处改过，保存时电脑会两份都留着（手机这份存进 note-conflicts）。"
                : "换上了上次没保存的草稿。"
        }
        ready = true
        focus = .body
    }

    private func keepDraft() {
        guard ready else { return }
        JottingDrafts.set(target, changed ? .init(title: title, body: text, start: start ?? "") : nil)
    }

    private func save() async {
        saving = true
        error = nil
        do {
            let saved = try await vault.saveJotting(path: path, title: title, body: text, start: start)
            JottingDrafts.set(target, nil)
            ready = false
            onSaved(saved)
            dismiss()
        } catch {
            self.error = "没保存上：\(error.localizedDescription)"
            saving = false
        }
    }
}

extension Vault {
    /// 存一条随手记：path 是 nil 就新建（文件名按今天和标题，同名的另起 _2），不然改那一条。
    /// 都走 saveEdited：手机上马上看到，待确认的修改记着，交给电脑（新建的不带 base，电脑写在同一个名字下）
    @discardableResult
    /// start：改的起点（编辑页打开时那份全文）：新正文换进它里面，bases 也照它算（saveEdited 的 from）
    func saveJotting(path: String?, title: String, body: String, start: String? = nil, now: Date = .now) async throws -> String {
        if let path {
            guard let text = start ?? localText(path) else { throw SparkError("这条随手记还没下载到手机上，同步之后再改") }
            try await saveEdited(path: path, text: Jottings.updated(text, title: title, body: body, now: now), from: text)
            return path
        }
        let stem = (Jottings.fileName(title: Jottings.titleOf(title, Jottings.normalize(body)), created: now) as NSString).deletingPathExtension
        var name = stem + ".md"
        // 手机上删了、电脑还没移走的名字，还在等电脑收下的名字，都不用（不然电脑先存后删，新的一条就进了废纸篓）
        func taken(_ n: String) -> Bool {
            let p = "\(Jottings.folder)/\(n)"
            return exists(p) || deletedPending.contains(p) || pending[p] != nil
        }
        for n in 2..<100 where taken(name) { name = "\(stem)_\(n).md" }
        let newPath = "\(Jottings.folder)/\(name)"
        try await saveEdited(path: newPath, text: Jottings.render(title: title, body: body, created: now))
        return newPath
    }
}
