import SwiftUI

/// 目录 + 我的高亮（电脑上阅读页划的，存在单集笔记的「我的高亮」一节，手机上只看）
struct TocSheet: View {
    let headings: [(index: Int, level: Int, text: String)]
    let notePath: String?
    let inlineMarks: [String]?     // 剪藏：文章里 ==划的重点==
    var notes: [DocEdit.Note] = [] // 剪藏：划的字上写的批注
    var onMark: ((String) -> Void)? = nil      // 剪藏：点一处划线，回到正文那一段
    var onOpenNote: (() -> Void)? = nil        // 剪藏：打开这篇的读书笔记
    let onJump: (Int) -> Void
    @Environment(Vault.self) private var vault
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    @State private var marks: [String]?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if notePath != nil || inlineMarks != nil {
                    Picker("", selection: $tab) {
                        Text("目录").tag(0)
                        Text(marks.map { "划线 \($0.count)" + (notes.isEmpty ? "" : "・批注 \(notes.count)") } ?? "划线").tag(1)
                    }
                    .pickerStyle(.segmented).padding(.horizontal, 20).padding(.bottom, 8)
                }
                List {
                    if tab == 0 {
                        if headings.isEmpty {
                            Text("这一篇没有小标题").foregroundStyle(p.ink2)
                        }
                        ForEach(headings, id: \.index) { h in
                            Button {
                                onJump(h.index)
                                dismiss()
                            } label: {
                                Text(h.text).font(.system(size: 15)).foregroundStyle(p.ink)
                                    .padding(.leading, Double(max(0, h.level - 2)) * 14)
                                    .frame(minHeight: 36, alignment: .leading)
                            }
                        }
                    } else {
                        if inlineMarks == nil {
                            Text("小结文件还没下载到手机上，下载好以后小结里也能直接划线、批注。这里是以前在电脑上划、记在小结末尾的句子。")
                                .font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        if let marks {
                            if marks.isEmpty { Text("还没有划线").foregroundStyle(p.ink2) }
                            if let onOpenNote, !marks.isEmpty {
                                Button { dismiss(); onOpenNote() } label: {
                                    Label("打开这篇的笔记", systemImage: "note.text").font(.system(size: 15)).foregroundStyle(p.accent)
                                        .frame(minHeight: 36, alignment: .leading)
                                }
                            }
                            // 同样的字可能划了两处：按第几处当 id，不按字（字当 id 会重复）
                            ForEach(Array(marks.enumerated()), id: \.offset) { _, m in
                                Button { if let onMark { onMark(m); dismiss() } } label: {
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(Markdown.inline(m)).font(prefs.body(0.95)).lineSpacing(4).foregroundStyle(p.ink)
                                            .multilineTextAlignment(.leading)
                                        ForEach(notes.filter { $0.quote == m }) { n in
                                            Label { Text(n.text).multilineTextAlignment(.leading) } icon: { Image(systemName: "square.and.pencil") }
                                                .font(.system(size: 14)).foregroundStyle(p.accent)
                                        }
                                    }
                                    .padding(.vertical, 4)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                .buttonStyle(.plain).disabled(onMark == nil)
                            }
                        } else {
                            ProgressView()
                        }
                    }
                }
                .readableColumn()
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
            }
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("这一期").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
        .task(id: tab) {
            if let inlineMarks { marks = inlineMarks; return }
            guard tab == 1, marks == nil, let notePath else { return }
            let text = (try? await vault.text(notePath)) ?? ""
            marks = Self.highlights(text)
        }
    }

    /// 笔记里「## 我的高亮」这一节的每一条
    static func highlights(_ note: String) -> [String] {
        guard let start = note.range(of: "\n## 我的高亮") else { return [] }
        var section = note[start.upperBound...]
        if let next = section.range(of: "\n## ") { section = section[..<next.lowerBound] }
        return section.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { $0.hasPrefix("- ") || $0.hasPrefix("> ") }
            .map { String($0.dropFirst(2)) }
    }
}

/// 要写或要改的一条批注
struct NoteDraft: Identifiable {
    let quote: String       // 批注的那段字（显示出来的样子）
    var targets: [MarkAnchor.Target] = []   // 新批注：选中的几项（前面几项划上，批注挂在最后一项）
    var scope = MarkAnchor.Scope()           // 新批注：在文件里哪一段找
    var text = ""
    var noteID: Int? = nil  // 已有批注的编号
    var id: String { "\(noteID ?? -1)|\(quote)" }
}

/// 写批注、看批注：上面是批注的那段字，下面写想法
struct NoteSheet: View {
    let draft: NoteDraft
    let onSave: (String) -> Void
    let onDelete: (() -> Void)?
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                // 选中的那段字照正文里划重点的样子铺浅黄底，不用引用竖条
                Text({ var q = AttributedString(draft.quote); q.backgroundColor = Color.yellow.opacity(0.32); return q }())
                    .font(prefs.body(0.9)).lineSpacing(prefs.size * 0.45).foregroundStyle(p.ink).lineLimit(6)
                TextField("写下你的想法", text: $text, axis: .vertical)
                    .font(.system(size: 17)).lineLimit(4...12)
                    .focused($focused)
                if let onDelete {
                    Button("删除批注", role: .destructive) { onDelete(); dismiss() }
                        .font(.system(size: 15))
                }
                Spacer()
            }
            .padding(20)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle(draft.noteID == nil ? "批注" : "我的批注").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { onSave(text); dismiss() }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || text == draft.text)
                }
            }
        }
        .onAppear { text = draft.text; focused = draft.noteID == nil }
    }
}
