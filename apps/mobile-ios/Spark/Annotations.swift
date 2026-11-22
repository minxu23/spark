import SwiftUI
import CryptoKit

/// 一篇文章上的笔记：划线、批注、读后感，划完、取消后底下留几秒「撤销」。
///
/// 剪藏、小结、整理稿、文字记录、信息跟进的原文、会议资料、报告、复盘都用这一套：能不能做笔记只看
/// DocEdit.annotatable(path)，阅读页只要
/// 1. 建一个 DocNotes(path:)，onChange 里按新内容重排正文（正文用 DocNotes.body 去掉读后感那一节）；
/// 2. 正文的 MarkdownView 带上 .environment(\.mdEdit, notes.edit(vault))；
/// 3. 正文下面放 DocNotesFooter(notes:)，整页挂 .docNotes(notes, title:)。
/// 文件的写法见 DocEdit（电脑上 core/annotations.py 同一种），改完由 Vault.saveEdited 存好、交给电脑。
@MainActor @Observable
final class DocNotes {
    let path: String
    var episode: Episode? = nil
    private(set) var reflection = ""
    private(set) var markCount = 0
    private(set) var noteCount = 0
    var noting: NoteDraft?
    var writingReflection = false
    var showNote = false
    /// 还没弹出来的提示。什么时候弹由页面定（弹窗开着时等它收起），几条一起到就连在一起
    var message: String?
    private(set) var undo: (label: String, before: String, id: UUID)?
    /// 文件改了：页面按新内容重排
    @ObservationIgnored var onChange: (String) -> Void = { _ in }

    init(path: String, episode: Episode? = nil) {
        self.path = path
        self.episode = episode
    }

    /// 正文里不重复画读后感：文末单画一张能改的卡
    static func body(_ text: String) -> String {
        var t = text
        if let r = DocEdit.reflectionRange(t) { t.removeSubrange(r) }
        return t
    }

    /// 某处划线在哪一块（回到原文那一处用）：比显示出来的前 24 个字
    static func blockIndex(of mark: String, in blocks: [MDBlock]) -> Int? {
        let want = String(String(Markdown.inline(mark).characters).prefix(24))
        guard !want.isEmpty else { return nil }
        return blocks.firstIndex { b in
            let raw: String
            switch b {
            case .paragraph(let t), .quote(let t), .caption(let t): raw = t
            case .bullet(let t, _, _): raw = t
            case .heading(_, let t): raw = t
            default: return false
            }
            return String(Markdown.inline(raw).characters).contains(want)
        }
    }

    func refresh(_ text: String) {
        reflection = DocEdit.reflection(text)
        markCount = DocEdit.marks(text).count
        noteCount = DocEdit.notes(text).count
    }

    func save(_ text: String, _ vault: Vault) {
        refresh(text)
        onChange(text)
        Task {
            do { try await vault.saveEdited(path: path, text: text) }
            catch { report("没存上：\(error.localizedDescription)") }
        }
    }

    /// 交给正文的划线、取消划线、批注。剪藏另外有重新下载、删图
    func edit(_ vault: Vault, retryImage: ((String) async -> String?)? = nil, removeImage: ((String) -> Void)? = nil) -> MDEdit {
        MDEdit(retryImage: retryImage, removeImage: removeImage, mark: { [weak self] pieces in
            // 跨了几段的一段一段划，改完一起存（一段一存的话，后一段读到的还是没改的文件）
            guard let self else { return }
            guard var text = vault.localText(self.path) else { self.report("这篇手机上已经没有了（同步时改了名或删了），没划上。回书架重新打开再划"); return }
            let before = text
            var done = 0
            for p in pieces { if let new = DocEdit.highlighted(text, block: p.block, selected: p.selected) { text = new; done += 1 } }
            if done > 0 { self.save(text, vault); self.offerUndo("已划线", before) }
            if done < pieces.count { self.report(done == 0 ? "这段在文件里没找到，划不上" : "有 \(pieces.count - done) 段在文件里没找到，没划上") }
        }, unmark: { [weak self] pieces in
            guard let self, var text = vault.localText(self.path) else { return }
            let before = text
            var done = 0
            for p in pieces { if let new = DocEdit.unhighlighted(text, block: p.block, marked: p.marked) { text = new; done += 1 } }
            if done > 0 { self.save(text, vault); self.offerUndo("已取消划线", before) }
        }, annotate: { [weak self] block, sel in
            self?.noting = NoteDraft(quote: sel, block: block)
        })
    }

    /// 点了批注气泡：弹出这条批注
    func openNote(_ url: URL, _ vault: Vault) -> OpenURLAction.Result {
        guard url.scheme == Markdown.noteScheme else { return .systemAction }
        let id = Int(url.absoluteString.dropFirst(Markdown.noteScheme.count + 1)) ?? -1
        if let text = vault.localText(path), let n = DocEdit.notes(text).first(where: { $0.id == id }) {
            noting = NoteDraft(quote: String(Markdown.inline(n.quote).characters), text: n.text, noteID: n.id)
        }
        return .handled
    }

    func saveNote(_ d: NoteDraft, _ body: String, _ vault: Vault) {
        guard let text = vault.localText(path) else {
            // 写好的批注别悄悄丢：这篇同步时改了名或删了
            report("批注没加上：这篇手机上已经没有了（同步时改了名或删了）。回书架重新打开再加。\n\n你写的批注：\(body)")
            return
        }
        let new: String?
        if let id = d.noteID { new = DocEdit.renoted(text, id: id, note: body) }
        else { new = DocEdit.annotated(text, block: d.block, selected: d.quote, note: body) }
        if let new { save(new, vault); dropUndo() }
        // 写好的批注别跟着丢：原话放进提示里（页面等批注弹窗收起再弹）
        else if d.noteID == nil {
            report("批注没加上：选中的字跨了已有的批注（点批注气泡修改），或者这段在文件里没找到。\n\n你写的批注：\(body)")
        } else {
            report("批注没改上：这条批注在文件里没找到。\n\n你写的批注：\(body)")
        }
    }

    func deleteNote(_ id: Int, _ vault: Vault) {
        if let text = vault.localText(path) { save(DocEdit.unnoted(text, id: id), vault); dropUndo() }
    }

    func saveReflection(_ body: String, _ vault: Vault) {
        if let text = vault.localText(path) { save(DocEdit.withReflection(text, body), vault); dropUndo() }
    }

    /// 有一条没弹的就接在后面，不盖掉
    func report(_ text: String) {
        message = message.map { "\($0)\n\n\(text)" } ?? text
    }

    /// 批注、读后感存上了：撤销条回到的是这之前的文件，点了会把刚写的一起丢掉，收起来
    private func dropUndo() {
        if undo != nil { withAnimation(.easeIn(duration: 0.15)) { undo = nil } }
    }

    func offerUndo(_ label: String, _ before: String) {
        let id = UUID()
        withAnimation(.easeOut(duration: 0.15)) { undo = (label, before, id) }
        Task {
            try? await Task.sleep(for: .seconds(5))
            if undo?.id == id { withAnimation(.easeIn(duration: 0.2)) { undo = nil } }
        }
    }

    func undoNow(_ vault: Vault) {
        guard let u = undo else { return }
        save(u.before, vault)
        withAnimation(.easeIn(duration: 0.15)) { undo = nil }
    }
}

extension View {
    /// 挂上做笔记要的东西：点批注气泡、写批注和读后感的弹窗、这篇的笔记、撤销条。notes 是 nil（这篇不能做笔记）就什么都不加
    func docNotes(_ notes: DocNotes?, title: String) -> some View {
        modifier(DocNotesModifier(notes: notes, title: title))
    }
}

private struct DocNotesModifier: ViewModifier {
    let notes: DocNotes?
    let title: String

    func body(content: Content) -> some View {
        if let notes { content.modifier(Attached(notes: notes, title: title)) } else { content }
    }

    struct Attached: ViewModifier {
        @Bindable var notes: DocNotes
        let title: String
        @Environment(Vault.self) private var vault
        /// 正弹着的提示。notes.message 是还没弹的
        @State private var alertText: String?

        /// 这几样一变就重新看要不要弹
        private var alertKey: String {
            "\(notes.message ?? "")|\(notes.noting?.id ?? "")|\(notes.writingReflection)|\(alertText != nil)"
        }

        func body(content: Content) -> some View {
            content
                .environment(\.openURL, OpenURLAction { notes.openNote($0, vault) })
                .overlay(alignment: .bottom) { UndoBar(notes: notes) }
                // 没存上、划不上、批注没加上：弹出来说，不放在文末（正读着的地方看不到）
                .alert("笔记", isPresented: Binding(get: { alertText != nil }, set: { if !$0 { alertText = nil } })) {
                    Button("好") { alertText = nil }
                } message: { Text(alertText ?? "") }
                // 批注、读后感弹窗开着时提示弹不出来，还可能挡掉弹窗：等弹窗都收起、收起的动画走完再弹
                .task(id: alertKey) {
                    guard canShowAlert else { return }
                    try? await Task.sleep(for: .milliseconds(600))
                    guard !Task.isCancelled, canShowAlert, let m = notes.message else { return }
                    notes.message = nil
                    alertText = m
                }
                .sheet(item: $notes.noting) { d in
                    NoteSheet(draft: d, onSave: { notes.saveNote(d, $0, vault) },
                              onDelete: d.noteID.map { id in { notes.deleteNote(id, vault) } })
                    .presentationDetents([.medium, .large])
                }
                .sheet(isPresented: $notes.writingReflection) {
                    ReflectionSheet(title: title, initial: notes.reflection, source: vault.readingNote(notes.path)?.draftSource ?? "") {
                        notes.saveReflection($0, vault)
                    }
                }
                .navigationDestination(isPresented: $notes.showNote) { ReadingNoteView(path: notes.path, episode: notes.episode) }
        }

        private var canShowAlert: Bool {
            notes.message != nil && alertText == nil && notes.noting == nil && !notes.writingReflection
        }
    }
}

/// 刚划上或刚取消的那一下：底下留几秒「撤销」，点了回到改之前的样子
private struct UndoBar: View {
    let notes: DocNotes
    @Environment(Vault.self) private var vault

    var body: some View {
        if let u = notes.undo {
            HStack(spacing: 14) {
                Text(u.label).foregroundStyle(.white)
                Button("撤销") { notes.undoNow(vault) }.fontWeight(.semibold).foregroundStyle(Color.yellow)
            }
            .font(.system(size: 15))
            .padding(.horizontal, 18).frame(minHeight: 44)
            .background(Color.black.opacity(0.82), in: Capsule())
            .padding(.bottom, 12)
            .transition(.opacity.combined(with: .move(edge: .bottom)))
        }
    }
}

/// 文末：读完顺手写读后感；划过线的，从这儿去看这篇的笔记
struct DocNotesFooter: View {
    let notes: DocNotes
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Rectangle().fill(p.rule).frame(height: 0.5).padding(.bottom, 6)
            if notes.reflection.isEmpty {
                Button { notes.writingReflection = true } label: {
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
                        Button("改") { notes.writingReflection = true }.font(.system(size: 14)).foregroundStyle(p.accent).buttonStyle(.borderless)
                    }
                    Text(notes.reflection).font(prefs.body(0.95)).lineSpacing(prefs.size * 0.55).foregroundStyle(p.ink).textSelection(.enabled)
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(p.rule.opacity(0.6)))
            }
            if notes.markCount + notes.noteCount > 0 {
                Button { notes.showNote = true } label: {
                    HStack {
                        Text("这篇的笔记").foregroundStyle(p.ink)
                        Spacer()
                        Text("\(notes.markCount) 处划线，\(notes.noteCount) 条批注").foregroundStyle(p.ink2)
                        Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold)).foregroundStyle(p.ink2.opacity(0.6))
                    }
                    .font(.system(size: 14)).frame(minHeight: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.top, 28).padding(.bottom, 16)
    }
}

/// 怎么划重点、批注：阅读页「更多」里的说明
enum NotesTip {
    static let title = "划重点、批注"
    static let message = "在正文里长按，拖动选中文字，点弹出菜单里的「划重点」或「批注」。点已划的字，可以批注、取消划线或拷贝。批注过的地方后面有个小气泡，点它看批注、修改或删除。读完在文末写读后感。\n\niPad 上用笔：横着在字上一划就是划重点，在已划的字上再划一次是取消。\n\n剪藏、播客和会议的小结、整理稿、文字记录、信息跟进、报告都能这样做笔记，笔记存在文件自己里，电脑上也看得到。小结、专题重新生成时，笔记会接到新内容上。"
}

extension Vault {
    /// 手机上改之前那一版的指纹：发给电脑核对，电脑上那篇还是这一版才覆盖（core/remote_tasks._save_noted）
    static func fingerprint(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
