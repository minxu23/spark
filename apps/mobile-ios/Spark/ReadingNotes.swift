import SwiftUI

/// 读书笔记：一篇剪藏读完留下的东西——划线（==…==）、批注（划线后面的脚注）、读后感（文件末尾「## 读后感」）。
/// 都存在剪藏自己的文件里，这里只是按「一篇文章一篇笔记」的样子读出来、摆出来。
struct ReadingNote: Identifiable {
    struct Item: Identifiable {
        let id: Int
        let section: String          // 所在的小标题（正文开头、没有小标题的是空串）
        let mark: String             // 划的那段（原文写法）
        let anchor: String           // 第一处划线的原文：接起来的几处中间少了标点，回原文时拿这一段去找
        let notes: [DocEdit.Note]
    }

    let meta: ClipMeta
    let reflection: String
    let items: [Item]
    let looseNotes: [DocEdit.Note]   // 没挂在划线上的批注
    let edited: Date                 // 文件最后改动的时间：划线、写批注、写读后感都会更新

    var id: String { meta.path }
    var noteCount: Int { items.reduce(0) { $0 + $1.notes.count } + looseNotes.count }
    var isEmpty: Bool { items.isEmpty && looseNotes.isEmpty && reflection.isEmpty }

    /// 按小标题分组，保持在文章里的先后顺序
    var sections: [(title: String, items: [Item])] {
        var out: [(String, [Item])] = []
        for it in items {
            if out.last?.0 == it.section { out[out.count - 1].1.append(it) } else { out.append((it.section, [it])) }
        }
        return out
    }

    /// 列表上的一句：读后感的第一句，没写就用第一处划线
    var teaser: String {
        if !reflection.isEmpty { return reflection.split(separator: "\n").first.map(String.init) ?? reflection }
        return items.first.map { String(Markdown.inline($0.mark).characters) } ?? ""
    }

    /// 划线只框住了加粗的一半（以前留下的「==…**半句==」）：单出来的 ** 去掉，免得露出符号
    static func cleanBold(_ mark: String) -> String {
        mark.components(separatedBy: "**").count % 2 == 0 ? mark.replacingOccurrences(of: "**", with: "") : mark
    }

    static func load(_ meta: ClipMeta, text: String, edited: Date) -> ReadingNote {
        let defs = DocEdit.noteDefs(text)
        var body = RX.first(#"(?s)\n## 正文\n(.*)"#, text) ?? text
        if let r = DocEdit.reflectionRange(body) { body.removeSubrange(r) }
        var section = "", used = Set<Int>()
        // 先逐处记下：在第几行、是不是从段首开始、是不是一直划到段尾——好把跨段划的接回一段
        struct Raw { var section: String; var mark: String; var anchor = ""; var notes: [DocEdit.Note]; let line: Int; let atStart: Bool; let atEnd: Bool
                     var start = 0; var end = 0 }
        var raws: [Raw] = []
        let markRE = RX.re(#"==([^=\n]+)==((?:\[\^note-\d+\])*)"#), refRE = RX.re(#"\[\^note-(\d+)\]"#)
        let lines = body.components(separatedBy: "\n")
        for (li, line) in lines.enumerated() {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("[^note-") { continue }
            if t.hasPrefix("#"), let m = t.range(of: #"^#{1,6}\s+"#, options: .regularExpression) {
                section = String(Markdown.inline(String(t[m.upperBound...])).characters)
                continue
            }
            guard line.contains("==") else { continue }     // 绝大多数行没划线，不用跑正则
            let ns = line as NSString
            for m in markRE.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
                let mark = ns.substring(with: m.range(at: 1)), refs = ns.substring(with: m.range(at: 2)) as NSString
                let ids = refRE.matches(in: refs as String, range: NSRange(location: 0, length: refs.length))
                    .compactMap { Int(refs.substring(with: $0.range(at: 1))) }
                used.formUnion(ids)
                let notes = ids.compactMap { id in defs[id].map { DocEdit.Note(id: id, quote: mark, text: $0) } }
                let before = ns.substring(to: m.range.location).replacingOccurrences(of: #"^[\s>*\-+]*(\d+[.)]\s*)?|\*\*"#, with: "", options: .regularExpression)
                let after = ns.substring(from: m.range.location + m.range.length).replacingOccurrences(of: #"\*\*|\[\^note-\d+\]|\s"#, with: "", options: .regularExpression)
                raws.append(Raw(section: section, mark: cleanBold(mark), anchor: cleanBold(mark), notes: notes, line: li, atStart: before.trimmingCharacters(in: .whitespaces).isEmpty,
                                atEnd: after.isEmpty, start: m.range.location, end: m.range.location + m.range.length))
            }
        }
        // 接起来的两种：同一段里挨着划的两处（中间只有标点、空格），合成一句；
        // 跨段划的（前一处划到段尾、后一处从下一段段首开始，中间只隔空行或没划的译文），合成一条、保留分段
        func sameParagraph(_ a: Raw, _ b: Raw) -> Bool {
            guard a.line == b.line, b.start >= a.end else { return false }
            let gap = (lines[a.line] as NSString).substring(with: NSRange(location: a.end, length: b.start - a.end))
            return gap.replacingOccurrences(of: #"[\s\p{P}*]"#, with: "", options: .regularExpression).isEmpty
        }
        func nextParagraph(_ a: Raw, _ b: Raw) -> Bool {
            guard a.atEnd, b.atStart, b.line > a.line else { return false }
            return lines[(a.line + 1)..<b.line].allSatisfy { l in
                let t = l.trimmingCharacters(in: .whitespaces)
                return t.isEmpty || (t.hasPrefix(">") && !t.contains("=="))
            }
        }
        var items: [Item] = []
        var i = 0
        while i < raws.count {
            var cur = raws[i]
            while i + 1 < raws.count, raws[i + 1].section == cur.section,
                  sameParagraph(cur, raws[i + 1]) || nextParagraph(cur, raws[i + 1]) {
                let next = raws[i + 1]
                let glue = next.line == cur.line ? "" : "\n"
                cur = Raw(section: cur.section, mark: cur.mark + glue + next.mark, anchor: cur.anchor, notes: cur.notes + next.notes,
                          line: next.line, atStart: cur.atStart, atEnd: next.atEnd, start: cur.start, end: next.end)
                i += 1
            }
            items.append(Item(id: items.count, section: cur.section, mark: cur.mark, anchor: cur.anchor, notes: cur.notes))
            i += 1
        }
        let loose = defs.keys.sorted().filter { !used.contains($0) }.map { DocEdit.Note(id: $0, quote: "", text: defs[$0] ?? "") }
        return ReadingNote(meta: meta, reflection: DocEdit.reflection(text), items: items, looseNotes: loose, edited: edited)
    }
}

/// 每篇剪藏读出来的结果（一句话、标签、划线、批注）按文件的改动时间记着：文件没变就不再读、不再拆。
/// 复盘、设置、检索都要过一遍全部剪藏，没有这层就得每次把每个文件读一遍
enum ClipCache {
    private static let lock = NSLock()
    private static var entries: [String: (stamp: Date, size: Int, note: ReadingNote)] = [:]

    /// 一篇剪藏的读书笔记（没有划线、批注的也给，里面带着这篇的信息）；文件不在手机上是 nil
    static func note(_ path: String, root: URL) -> ReadingNote? {
        let url = root.appending(path: path, directoryHint: .notDirectory)
        guard let v = try? url.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey]),
              let stamp = v.contentModificationDate, let size = v.fileSize else { return nil }
        lock.lock()
        let hit = entries[path]
        lock.unlock()
        if let hit, hit.stamp == stamp, hit.size == size { return hit.note }
        guard let data = try? Data(contentsOf: url) else { return nil }
        let text = String(decoding: data, as: UTF8.self)
        let note = ReadingNote.load(Clips.meta(path: path, text: text), text: text, edited: stamp)
        lock.lock()
        entries[path] = (stamp, size, note)
        lock.unlock()
        return note
    }

    static func notes(_ paths: [String], root: URL) -> [ReadingNote] { paths.compactMap { note($0, root: root) } }
}

extension Vault {
    /// 全部剪藏（含没写笔记的），在后台读：第一次要把每个文件读一遍，放主线程上会卡住界面
    func clipNotes() async -> [ReadingNote] {
        let paths = library.clips.map(\.path), root = Self.root
        return await Task.detached(priority: .userInitiated) { ClipCache.notes(paths, root: root) }.value
    }

    /// 所有有笔记的剪藏（有划线、批注或读后感的），最近改过的在前
    func readingNotes() async -> [ReadingNote] {
        await clipNotes().filter { !$0.isEmpty }.sorted { $0.edited > $1.edited }
    }

    /// 一篇剪藏的读书笔记
    func readingNote(_ path: String) -> ReadingNote? { ClipCache.note(path, root: Self.root) }
}

/// 打开一篇读书笔记
struct ReadingNoteRef: Hashable { let path: String }

/// 打开原文并滚到某一处划线
struct DocFocus: Hashable { let doc: Doc; let focus: String }

/// 一篇读书笔记：读后感在最上面，下面按小标题列划线和批注；点划线回到原文那一处
struct ReadingNoteView: View {
    let path: String
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @State private var note: ReadingNote?
    @State private var writing = false
    @State private var onlyNoted = false
    @State private var error: String?

    var body: some View {
        ScrollView {
            if let note {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(Markdown.latinQuotes(note.meta.title)).font(.heading(22)).foregroundStyle(p.ink).lineSpacing(3)
                        Text(meta(note)).font(.system(size: 13)).foregroundStyle(p.ink2).lineLimit(1)
                    }
                    reflectionCard(note)
                    if let error { Text(error).font(.system(size: 13)).foregroundStyle(Color.hex(0xb42318)) }
                    if !note.items.isEmpty {
                        HStack {
                            Text("划线和批注").font(.system(size: 13, weight: .medium)).foregroundStyle(p.ink2)
                            Spacer()
                            Picker("看哪些", selection: $onlyNoted) {
                                Text("全部").tag(false)
                                Text("有批注").tag(true)
                            }
                            .pickerStyle(.segmented).frame(width: 150)
                        }
                        ForEach(Array(note.sections.enumerated()), id: \.offset) { _, s in
                            let items = s.items.filter { !onlyNoted || !$0.notes.isEmpty }
                            if !items.isEmpty {
                                VStack(alignment: .leading, spacing: 14) {
                                    if !s.title.isEmpty {
                                        Text(s.title).font(.system(size: 14, weight: .semibold)).foregroundStyle(p.accent).lineLimit(2)
                                    }
                                    ForEach(Array(items.enumerated()), id: \.element.id) { i, it in
                                        if i > 0 { Rectangle().fill(p.rule.opacity(0.6)).frame(height: 0.5) }
                                        item(it, doc: note.meta.doc)
                                    }
                                }
                            }
                        }
                    }
                    if !note.looseNotes.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("其他批注").font(.system(size: 14, weight: .semibold)).foregroundStyle(p.accent)
                            ForEach(note.looseNotes) { noteBox($0) }
                        }
                    }
                }
                .padding(.horizontal, 20).padding(.top, 8).padding(.bottom, 32)
            } else {
                ProgressView().padding(.top, 60)
            }
        }
        .readableColumn()
        .background(p.paper.ignoresSafeArea())
        .navigationBarTitleDisplayMode(.inline)
        .navigationTitle("读书笔记")
        .toolbar {
            if let note {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink(value: note.meta.doc) { Label("打开原文", systemImage: "doc.text") }
                }
            }
        }
        .sheet(isPresented: $writing) {
            ReflectionSheet(title: note?.meta.title ?? "", initial: note?.reflection ?? "", source: note.map(draftSource) ?? "") { save($0) }
        }
        .onAppear(perform: reload)
    }

    /// 给模型起草读后感用的材料：标题、一句话摘要、按顺序的划线和批注
    private func draftSource(_ n: ReadingNote) -> String {
        var lines = ["文章：\(n.meta.title)"]
        if !n.meta.tldr.isEmpty { lines.append("摘要：\(n.meta.tldr)") }
        lines.append("")
        for (i, it) in n.items.enumerated() {
            lines.append("划线\(i + 1)：\(String(Markdown.inline(it.mark).characters))")
            for note in it.notes { lines.append("  我的批注：\(note.text)") }
        }
        for note in n.looseNotes { lines.append("我的批注：\(note.text)") }
        return lines.joined(separator: "\n")
    }

    private func meta(_ n: ReadingNote) -> String {
        var parts = [n.meta.source.isEmpty ? "剪藏" : n.meta.source, "存于 \(Dates.label(n.meta.clipped.replacingOccurrences(of: "-", with: "")))"]
        parts.append("\(n.items.count) 处划线，\(n.noteCount) 条批注")
        return parts.joined(separator: "，")
    }

    @ViewBuilder
    private func reflectionCard(_ n: ReadingNote) -> some View {
        if n.reflection.isEmpty {
            Button { writing = true } label: {
                HStack(spacing: 10) {
                    Image(systemName: "square.and.pencil").font(.system(size: 16, weight: .medium)).foregroundStyle(p.accent)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("写读后感").font(.system(size: 15, weight: .semibold)).foregroundStyle(p.ink)
                        Text("这篇读完，最想记住的是什么").font(.system(size: 13)).foregroundStyle(p.ink2)
                    }
                    Spacer()
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 16).strokeBorder(p.accent.opacity(0.45), style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("读后感").font(.system(size: 13, weight: .medium)).foregroundStyle(p.ink2)
                    Spacer()
                    Button("改") { writing = true }.font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
                }
                Text(n.reflection).font(prefs.body(0.95)).lineSpacing(prefs.size * 0.55).foregroundStyle(p.ink).textSelection(.enabled)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(p.paper2, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(p.rule.opacity(0.6)))
        }
    }

    private func item(_ it: ReadingNote.Item, doc: Doc) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            NavigationLink(value: DocFocus(doc: doc, focus: it.anchor)) {
                // 这里列的全是划线，不再铺黄底；批注的浅蓝框是唯一要区分的东西
                Text(Markdown.inline(it.mark))
                    .font(prefs.body(0.92)).lineSpacing(prefs.size * 0.55).foregroundStyle(p.ink)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("回到原文这一处")
            ForEach(it.notes) { noteBox($0) }
        }
    }

    private func noteBox(_ n: DocEdit.Note) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "pencil.line").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.accent)
            Text(n.text).font(.system(size: 14)).lineSpacing(6).foregroundStyle(p.ink).textSelection(.enabled)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityLabel("批注：\(n.text)")
    }

    private func reload() {
        guard let fresh = vault.readingNote(path) else { error = "这篇还没下载到手机上"; return }
        note = fresh
    }

    private func save(_ body: String) {
        guard let text = vault.localText(path) else { return }
        let new = DocEdit.withReflection(text, body)
        Task {
            do { try await vault.saveEdited(path: path, text: new); error = nil }
            catch { self.error = "没存上：\(error.localizedDescription)" }
            reload()
        }
    }
}

/// 写读后感：一段话，存进剪藏文件末尾的「## 读后感」。填了模型的话，可以先让模型按划线和批注起个草稿再改
struct ReflectionSheet: View {
    let title: String
    let initial: String
    var source: String = ""
    let onSave: (String) -> Void
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var drafting = false
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                Text(Markdown.latinQuotes(title)).font(.system(size: 13)).foregroundStyle(p.ink2).lineLimit(2)
                TextField("这篇读完，最想记住的是什么？跟之前读过的哪篇有关？", text: $text, axis: .vertical)
                    .font(.system(size: 17)).lineSpacing(6).lineLimit(6...20).focused($focused)
                if !source.isEmpty, jobs.config != nil {
                    Button { Task { await draft() } } label: {
                        HStack(spacing: 6) {
                            if drafting { ProgressView().controlSize(.small) } else { Image(systemName: "sparkles") }
                            Text(drafting ? "正在起草…" : (text.isEmpty ? "让模型按划线和批注起个草稿" : "重新起草（会替换上面的字）"))
                        }
                        .font(.system(size: 14, weight: .medium)).foregroundStyle(p.accent)
                        .padding(.horizontal, 12).frame(minHeight: 36)
                        .background(p.accent.opacity(0.1), in: Capsule())
                    }
                    .buttonStyle(.plain).disabled(drafting)
                }
                if let error { Text(error).font(.system(size: 13)).foregroundStyle(Color.hex(0xb42318)) }
                Spacer()
            }
            .padding(20)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("读后感").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { onSave(text); dismiss() }.disabled(text == initial || drafting)
                }
            }
        }
        .onAppear { text = initial; focused = initial.isEmpty }
    }

    /// 只根据我划的和写的起草，口吻平实，不要套话；起出来的只是初稿，要自己改
    private func draft() async {
        guard let config = jobs.config else { return }
        drafting = true
        error = nil
        defer { drafting = false }
        let prompt = """
        下面是我读一篇文章时划的重点和写的批注。请替我起草读后感：只写我关注的点，每一点直接说内容本身。
        - 1 到 3 点，每点一两句，合起来不超过 150 字；一段话，不要标题、不要分点符号。
        - 关注点从划线和批注里来；批注里我自己的判断要保留原意。
        - 不要写「我学到了」「让我意识到」「我觉得」「我的感想是」「这篇文章」「作者认为」这类引导语和感想，也不要总结、评价或夸文章。
        - 只根据下面的内容，不要编造文章里没有的观点和数字。

        \(source)
        """
        do {
            let out = try await LLM.complete(prompt, config: config, maxTokens: 600, timeout: 120)
            text = out.trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            self.error = "没起草成：\(error.localizedDescription)"
        }
    }
}
