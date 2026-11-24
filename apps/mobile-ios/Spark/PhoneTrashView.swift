import SwiftUI

/// 设置 → 废纸篓：手机上删掉的剪藏、复盘、随手记。每条能放回原处、彻底删除；右上角清空废纸篓。30 天后自动清掉
struct PhoneTrashView: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var erasing: PhoneTrash.Item?
    @State private var emptying = false
    @State private var note: String?

    var body: some View {
        List {
            if let problem = Vault.trashProblem {
                Text(problem).font(.system(size: 13)).foregroundStyle(Color.hex(0xb42318))
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            if vault.trashOffline && !vault.trash.isEmpty {
                Text("没联网时不会自动清除，联网后再检查。").font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            if let note {
                Text(note).font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            if vault.trash.isEmpty {
                Text("废纸篓是空的。手机上删掉的剪藏、复盘、随手记会先放在这里，30 天内可以放回原处，过了 30 天自动清掉。")
                    .font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            } else {
                Text("删掉的东西放在这里，30 天后自动清掉。电脑上那份在电脑的废纸篓里。")
                    .font(.system(size: 13)).foregroundStyle(p.ink2)
                    .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }
            let _ = CrashLog.list("手机废纸篓", vault.trash.map { "\($0.id)" })
            ForEach(vault.trash) { item in
                VStack(alignment: .leading, spacing: 4) {
                    Text(Markdown.latinQuotes(item.title)).font(.heading(16.5)).foregroundStyle(p.ink).lineSpacing(3)
                    if item.elsewhere {
                        Text("在别的设备上删掉的。这台手机上还留着一份，放回原处会重新交给电脑。")
                            .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                    if item.deleteFailed {
                        Text("电脑上没删成，文章还在书架上。点放回原处只是把这一条从废纸篓拿掉。")
                            .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                    HStack(spacing: 10) {
                        Text(item.kind)
                        Text("\(Date(timeIntervalSince1970: item.deletedAt).formatted(date: .abbreviated, time: .omitted))删除")
                        Spacer()
                        // 只是显示：有 10 分钟内的网络时间用它，没有用手机的钟（清不清只看网络时间）
                        Text("还剩 \(PhoneTrash.daysLeft(item, now: NetClock.shared.cached() ?? .now)) 天")
                    }
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2)
                }
                .padding(.vertical, 4)
                .listRowBackground(Color.clear).listRowSeparatorTint(p.rule)
                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                    Button { restore(item) } label: { Label("放回原处", systemImage: "arrow.uturn.backward") }.tint(p.accent)
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    Button(role: .destructive) { erasing = item } label: { Label("彻底删除", systemImage: "trash.slash") }
                }
                .contextMenu {
                    Button("放回原处", systemImage: "arrow.uturn.backward") { restore(item) }
                    Button("彻底删除", systemImage: "trash.slash", role: .destructive) { erasing = item }
                }
            }
        }
        .readableColumn()
        .listStyle(.plain)
        .pageStyle()
        .navigationTitle("废纸篓")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("清空废纸篓", role: .destructive) { emptying = true }.disabled(vault.trash.isEmpty)
            }
        }
        .onAppear { vault.purgeTrash() }
        .confirmationDialog("彻底删除这一篇？", isPresented: Binding(get: { erasing != nil }, set: { if !$0 { erasing = nil } }),
                            titleVisibility: .visible, presenting: erasing) { item in
            Button("彻底删除", role: .destructive) { vault.eraseTrash(item.id) }
        } message: { _ in
            Text("从手机的废纸篓里删掉，不能恢复。只有这篇用到的图片会一起删掉（别的文章也在用的留着）；电脑上这些图片移进电脑的废纸篓。")
        }
        .confirmationDialog("清空废纸篓？", isPresented: $emptying, titleVisibility: .visible) {
            Button("清空废纸篓", role: .destructive) { vault.eraseTrash(nil) }
        } message: {
            Text("手机废纸篓里的 \(vault.trash.count) 篇都会删掉，不能恢复。只有这些文章用到的图片会一起删掉（别的文章也在用的留着）；电脑上这些图片移进电脑的废纸篓。")
        }
    }

    private func restore(_ item: PhoneTrash.Item) {
        Task {
            do {
                let path = try await vault.restore(item)
                note = item.deleteFailed && path != item.path
                    ? "书架上这篇已经改过，废纸篓里的这一版另存为：\((path as NSString).lastPathComponent)"
                    : item.deleteFailed ? "《\(item.title)》本来就在书架上，已从废纸篓拿掉"
                    : path == item.path ? "放回原处了：《\(item.title)》"
                    : "原来的位置已经有别的文件，《\(item.title)》换了个名字放回：\((path as NSString).lastPathComponent)"
            } catch {
                note = "放回去了，交给电脑时出错：\(error.localizedDescription)"
            }
        }
    }
}
