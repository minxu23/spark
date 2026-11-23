import SwiftUI

/// 书架各页签最上面那张深蓝卡片（会议、播客、笔记、剪藏、信息跟进共用）：左上小字、右上小字、标题、
/// 可选的一行副文、可选的摘要（最多 3 行）、可选的附加内容（笔记卡片上最近的三条）。
/// 样子照原来会议那张：底色 0x1f3a5f、圆角 16、内边距 16；放进列表用 shelfLeadRow()。
/// 卡片上的字合成一个无障碍元素，读出「最近更新 · 11月2日，标题，……」；附加内容里能点的各自单独读
struct ShelfLeadCard<Extra: View>: View {
    let leading: String
    var trailing = ""
    /// 标题原样显示：要换引号的（Markdown.latinQuotes）调用方先换好
    let title: String
    var detail = ""
    var summary = ""
    /// 标题字号：会议 21，别的 19
    var titleSize: CGFloat = 19
    /// 标题最多几行：会议不限（nil），别的 3 行
    var titleLines: Int? = 3
    @ViewBuilder var extra: () -> Extra

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    Text(leading)
                    Spacer(minLength: 0)
                    if !trailing.isEmpty { Text(trailing).lineLimit(1) }
                }
                .font(.system(size: 12)).foregroundStyle(.white.opacity(0.85))
                // 每段字都按自己要的高度排（fixedSize 竖向）：列表行测出来的高度比内容要的小时，
                // 不然没限行数的标题会被压成一行加省略号（会议卡片原来就这样）
                Text(title).font(.heading(titleSize)).foregroundStyle(.white).lineSpacing(2).lineLimit(titleLines)
                    .fixedSize(horizontal: false, vertical: true)
                if !detail.isEmpty {
                    Text(detail).font(.system(size: 12.5)).foregroundStyle(.white.opacity(0.85))
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !summary.isEmpty {
                    Text(summary).font(.system(size: 13)).foregroundStyle(.white.opacity(0.9)).lineSpacing(3).lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .combine)
            extra()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.hex(0x1f3a5f), in: RoundedRectangle(cornerRadius: 16))
    }
}

extension ShelfLeadCard where Extra == EmptyView {
    init(leading: String, trailing: String = "", title: String, detail: String = "", summary: String = "",
         titleSize: CGFloat = 19, titleLines: Int? = 3) {
        self.init(leading: leading, trailing: trailing, title: title, detail: detail, summary: summary,
                  titleSize: titleSize, titleLines: titleLines) { EmptyView() }
    }
}

extension View {
    /// 书架卡片在列表里那一行：透明底、不画分隔线、上 4 左 20 下 8 右 20
    func shelfLeadRow() -> some View {
        listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
    }
}
