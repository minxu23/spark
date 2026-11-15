import SwiftUI

/// 四个页签共用的页头：系统大标题下面一行状态（一句话），再下面一条分区栏。
/// 分区栏是这一页的第一层切换（最新的来源、书架的分类、复盘的两块），用文字加下划线；
/// 分区里面再筛（剪藏的标签）才用胶囊按钮，两层看起来不一样。

/// 大标题下的一句话：同步到哪了、书架上有多少、这几天读了多少
struct PageStatus: View {
    let text: String
    var warning = false
    @Environment(\.palette) private var p

    var body: some View {
        Text(text)
            .font(.system(size: 13.5))
            .foregroundStyle(warning ? Color.hex(0xb42318) : p.ink2)
            .lineLimit(1).minimumScaleFactor(0.85)
            .frame(maxWidth: .infinity, alignment: .leading)
            .listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 2, trailing: 20))
    }
}

/// 分区栏：选中的字加粗、下面一道强调色短线；放不下时左右滑
struct SectionTabs<T: Hashable>: View {
    let items: [(value: T, label: String)]
    @Binding var selection: T
    @Environment(\.palette) private var p

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 22) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    let on = item.value == selection
                    Button { withAnimation(.easeOut(duration: 0.18)) { selection = item.value } } label: {
                        VStack(spacing: 7) {
                            Text(item.label)
                                .font(.system(size: 15.5, weight: on ? .semibold : .regular))
                                .foregroundStyle(on ? p.ink : p.ink2)
                            Capsule().fill(on ? p.accent : .clear).frame(height: 2.5)
                        }
                        .fixedSize()
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(on ? [.isSelected, .isButton] : .isButton)
                }
            }
            .padding(.horizontal, 20)
        }
        .contentMargins(.horizontal, 0, for: .scrollContent)
        .overlay(alignment: .bottom) { Rectangle().fill(p.rule).frame(height: 0.5) }
        .listRowBackground(Color.clear).listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 8, trailing: 0))
    }
}
