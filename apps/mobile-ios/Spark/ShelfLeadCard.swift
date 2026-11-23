import SwiftUI

/// 书架各页签最上面那张深蓝卡片（会议、播客、笔记、信息跟进共用）：左上小字、右上小字、标题、
/// 可选的一行副文、可选的摘要（最多 3 行）、可选的附加内容（笔记、播客、信息跟进卡片上最近的三条，用 ShelfLeadLinks）。
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

/// 卡片里最近的几条（笔记、播客、信息跟进共用）：每条上面一条细线，左边标题（一行，截断），右边几段小字
/// （标题优先：右边最后一段是日期，不截断；前面的名字最多占行宽三分之一，放不下先截断，见 ShelfLeadLineLayout），
/// 最小高 40，整行能点，各自单独读（读 label）。点了调 open：一张卡里几处能点，交给书架页去开，不在列表行里嵌链接
struct ShelfLeadLinks: View {
    struct Line: Identifiable {
        let id: String
        /// 原样显示：要换引号的调用方先换好
        let title: String
        let trailing: [String]
        /// 无障碍读的
        let label: String
        let open: () -> Void
    }
    let lines: [Line]

    var body: some View {
        if !lines.isEmpty {
            VStack(spacing: 0) {
                ForEach(lines) { l in
                    Rectangle().fill(.white.opacity(0.18)).frame(height: 0.5)
                    Button(action: l.open) {
                        ShelfLeadLineLayout {
                            Text(l.title).font(.system(size: 14.5)).foregroundStyle(.white).lineLimit(1)
                            ForEach(Array(l.trailing.enumerated()), id: \.offset) { _, t in
                                Text(t).font(.system(size: 11.5)).foregroundStyle(.white.opacity(0.75)).lineLimit(1)
                            }
                        }
                        .frame(minHeight: 40).contentShape(Rectangle())
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel(l.label)
                }
            }
            .padding(.top, 2)
        }
    }
}

/// 卡片里一行的排法：第一个是标题，靠左；后面几段靠右，段间隔 8，跟标题至少隔 24
/// （原来 HStack(spacing: 8) 里标题、Spacer(minLength: 8)、右边几段的间距）。
/// 右边最后一段（日期）照它要的宽度放，不截断；中间的（节目名、订阅名）最多占行宽三分之一，超出截断；
/// 剩下的宽度都给标题。都竖向居中
struct ShelfLeadLineLayout: Layout {
    private let spacing: CGFloat = 8
    private let titleGap: CGFloat = 24

    private func sizes(_ width: CGFloat, _ subviews: Subviews) -> [CGSize] {
        guard let title = subviews.first else { return [] }
        let trailing = Array(subviews.dropFirst())
        var out: [CGSize] = []
        for (i, v) in trailing.enumerated() {
            if i == trailing.count - 1 {
                out.append(v.sizeThatFits(.unspecified))
            } else {
                let ideal = v.sizeThatFits(.unspecified).width
                out.append(v.sizeThatFits(ProposedViewSize(width: min(ideal, width / 3), height: nil)))
            }
        }
        let used = out.reduce(0) { $0 + $1.width } + spacing * CGFloat(max(trailing.count - 1, 0))
            + (trailing.isEmpty ? 0 : titleGap)
        let titleSize = title.sizeThatFits(ProposedViewSize(width: max(0, width - used), height: nil))
        return [titleSize] + out
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let ideal = sizes(.infinity, subviews)
        let idealWidth = ideal.reduce(0) { $0 + $1.width } + spacing * CGFloat(max(subviews.count - 2, 0))
            + (subviews.count > 1 ? titleGap : 0)
        let width = proposal.width ?? idealWidth
        return CGSize(width: width, height: sizes(width, subviews).map(\.height).max() ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let s = sizes(bounds.width, subviews)
        guard !s.isEmpty else { return }
        subviews[0].place(at: CGPoint(x: bounds.minX, y: bounds.midY), anchor: .leading,
                          proposal: ProposedViewSize(s[0]))
        var x = bounds.maxX
        for i in stride(from: subviews.count - 1, through: 1, by: -1) {
            subviews[i].place(at: CGPoint(x: x, y: bounds.midY), anchor: .trailing, proposal: ProposedViewSize(s[i]))
            x -= s[i].width + spacing
        }
    }
}

extension View {
    /// 书架卡片在列表里那一行：透明底、不画分隔线、上 4 左 20 下 8 右 20
    func shelfLeadRow() -> some View {
        listRowBackground(Color.clear).listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 8, trailing: 20))
    }
}
