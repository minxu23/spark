import SwiftUI

struct MeView: View {
    @Environment(Vault.self) private var vault
    @Environment(Session.self) private var session
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @State private var showPrefs = false
    @State private var reconnect = false
    @State private var confirmWipe = false
    @State private var storage = (total: 0, lazy: 0)

    var body: some View {
        @Bindable var session = session
        List {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Text("笔记库 \(session.connection?.slug ?? "")").font(.heading(15))
                    Text(syncText).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    Button {
                        Task { await vault.sync(); storage = vault.storageBytes }
                    } label: {
                        Text(vault.syncing ? "同步中…" : "现在同步").font(.system(size: 15, weight: .semibold))
                            .padding(.horizontal, 18).frame(minHeight: 44)
                            .background(vault.syncing ? p.rule : p.accent, in: RoundedRectangle(cornerRadius: 10))
                            .foregroundStyle(p.paper)
                    }
                    .buttonStyle(.plain).disabled(vault.syncing)
                }
                .padding(14)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
            }

            Section("同步") {
                Toggle(isOn: $session.autoSync) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("打开 App 时自动同步")
                        Text("只下载有变化的文件，通常几秒钟").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                }
                Picker("新文件下载", selection: Binding(get: { vault.scope }, set: { vault.scope = $0 })) {
                    Text("小结和节目总结").tag(Vault.DownloadScope.light)
                    Text("全部（含文字记录）").tag(Vault.DownloadScope.all)
                }
            }
            .listRowBackground(Color.clear)

            Section {
                LabeledContent("已下载", value: ByteCountFormatter.string(fromByteCount: Int64(storage.total), countStyle: .file))
                Button("清除已下载的整理稿和文字记录（\(ByteCountFormatter.string(fromByteCount: Int64(storage.lazy), countStyle: .file))）") {
                    vault.clearLazy()
                    storage = vault.storageBytes
                }
                .disabled(storage.lazy == 0)
            } header: { Text("存储") } footer: { Text("小结和节目总结会留着；清掉的稿子下次打开时再下载。") }
            .listRowBackground(Color.clear)

            Section("阅读") {
                Button { showPrefs = true } label: {
                    LabeledContent("阅读设置和字体", value: "\(prefs.bodyFont.label)，\(Int(prefs.size)) 号")
                }
                .foregroundStyle(p.ink)
            }
            .listRowBackground(Color.clear)

            Section("连接") {
                LabeledContent("访问令牌", value: "读笔记、交任务，在钥匙串里")
                Button("更换令牌或仓库") { reconnect = true }
                Button("断开连接并删除手机上的笔记", role: .destructive) { confirmWipe = true }
            }
            .listRowBackground(Color.clear)
        }
        .listStyle(.insetGrouped)
        .pageStyle()
        .navigationTitle("我的")
        .onAppear { storage = vault.storageBytes }
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet() }
        .sheet(isPresented: $reconnect) { ConnectView { reconnect = false } }
        .confirmationDialog("断开后手机上的笔记会全部删掉，令牌也从钥匙串里删掉。仓库里的内容不受影响。",
                            isPresented: $confirmWipe, titleVisibility: .visible) {
            Button("断开并删除", role: .destructive) {
                vault.wipe()
                Connection.delete()
                session.connection = nil
            }
        }
    }

    private var syncText: String {
        if vault.syncing { return "正在同步：\(vault.progress.done)/\(vault.progress.total)" }
        if let e = vault.lastError { return "上次同步没成功：\(e)" }
        var parts: [String] = []
        if let t = vault.lastSync {
            parts.append("上次同步：\(t.formatted(date: .abbreviated, time: .shortened))")
        } else {
            parts.append("还没同步过")
        }
        if let d = vault.headDate {
            parts.append("仓库最新的一次推送来自电脑，\(d.formatted(date: .abbreviated, time: .shortened))")
        }
        return parts.joined(separator: "。") + "。"
    }
}
