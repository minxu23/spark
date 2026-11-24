import SwiftUI

/// 「改名称」面板：剪藏、复盘的阅读页「更多」里和列表长按菜单里都用这一个。
/// 从底部弹出，输入框能折行，长标题整句看得到；只改显示的名称（文件头 title 和第一行「# 标题」，
/// 见 DocEdit.retitled），文件名不变。空的、没改的不能保存。
struct RenameSheet: View {
    let current: String
    let onSave: (String) -> Void
    @State private var text: String
    @State private var height: CGFloat = 220
    @FocusState private var focused: Bool
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss

    init(current: String, onSave: @escaping (String) -> Void) {
        self.current = current
        self.onSave = onSave
        _text = State(initialValue: current)
    }

    /// 名称只能是一行：换行当成空格
    static func cleaned(_ s: String) -> String {
        s.replacingOccurrences(of: #"\s*\n\s*"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSave: Bool {
        let t = Self.cleaned(text)
        return !t.isEmpty && t != Self.cleaned(current)
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 10) {
                TextField("名称", text: $text, axis: .vertical)
                    .lineLimit(1...8)
                    .font(.system(size: 17))
                    .foregroundStyle(p.ink)
                    .focused($focused)
                    .submitLabel(.done)
                    .padding(.horizontal, 12).padding(.vertical, 10)
                    .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.rule))
                    // 折行的输入框里按「完成」会输进一个换行：去掉，能保存就当是点了保存
                    .onChange(of: text) { _, new in
                        guard new.contains("\n") else { return }
                        text = new.replacingOccurrences(of: "\n", with: "")
                        if canSave { save() }
                    }
                Text("只改显示的名称，文件名不变")
                    .font(.system(size: 13)).foregroundStyle(p.ink2)
            }
            .padding(.horizontal, 20).padding(.top, 8).padding(.bottom, 16)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
            .frame(maxHeight: .infinity, alignment: .top)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("改名称").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("保存") { save() }.disabled(!canSave) }
            }
        }
        // 高度跟着内容：输入框折几行就多高（加上导航栏那一条），最多到半屏以上
        .presentationDetents([.height(min(max(height + 70, 180), 460))])
        .presentationDragIndicator(.visible)
        .onAppear { focused = true }
    }

    private func save() {
        let t = Self.cleaned(text)
        guard !t.isEmpty, t != Self.cleaned(current) else { return }
        onSave(t)
        dismiss()
    }
}
