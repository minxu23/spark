import SwiftUI

struct MeView: View {
    @Environment(Vault.self) private var vault
    @Environment(Session.self) private var session
    @Environment(ReadPrefs.self) private var prefs
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @State private var showPrefs = false
    @State private var showModel = false
    @State private var reconnect = false
    @State private var confirmWipe = false
    @State private var storage = (total: 0, lazy: 0)

    var body: some View {
        @Bindable var session = session
        List {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Text(session.connection.map { "笔记库 \($0.slug)" } ?? "只用数据线同步").font(.heading(15))
                    Text(syncText).font(.system(size: 13.5)).foregroundStyle(p.ink2).lineSpacing(3)
                    Button {
                        Task { await vault.sync(); storage = vault.storageBytes }
                    } label: {
                        Text(vault.syncing ? "同步中…" : (session.connection == nil ? "看看有没有新拷来的" : "现在同步")).font(.system(size: 15, weight: .semibold))
                            .padding(.horizontal, 18).frame(minHeight: 44)
                            .background(vault.syncing ? p.rule : p.accent, in: RoundedRectangle(cornerRadius: 10))
                            .foregroundStyle(p.paper)
                    }
                    .buttonStyle(.plain).disabled(vault.syncing)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule))
                .listRowBackground(Color.clear).listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 0, leading: 20, bottom: 0, trailing: 20))
            }

            Section("同步") {
                Toggle(isOn: $session.autoSync) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("打开 App 时自动同步")
                        Text("只下载有变化的文件，通常几秒钟").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                }
                Toggle(isOn: Binding(get: { vault.wifiOnly }, set: { vault.wifiOnly = $0 })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("只在 Wi-Fi 下下载整理稿和文字记录")
                        Text("用流量时只同步小结和节目总结；打开整理稿会先问").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    }
                }
                #if DEBUG
                Toggle("开发用：假装在用流量", isOn: Binding(get: { NetMonitor.shared.pretendCellular },
                                                       set: { NetMonitor.shared.pretendCellular = $0 }))
                    .font(.system(size: 13)).foregroundStyle(p.ink2)
                #endif
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

            Section {
                Button { showModel = true } label: {
                    LabeledContent("模型和 API Key", value: jobs.config.map { $0.provider.label } ?? "还没填")
                }
                .foregroundStyle(p.ink)
            } header: { Text("在手机上生成复盘和报告") } footer: {
                Text(jobs.canRun ? "复盘和出报告直接在手机上生成，用的是 \(jobs.config?.label ?? "")。"
                                 : "填了 Key 之后，复盘和出报告可以直接在手机上生成，不用等电脑。")
            }
            .listRowBackground(Color.clear)

            Section("阅读") {
                Button { showPrefs = true } label: {
                    LabeledContent("阅读设置和字体", value: "\(prefs.bodyFont.label)，\(Int(prefs.size)) 号")
                }
                .foregroundStyle(p.ink)
            }
            .listRowBackground(Color.clear)

            Section("连接") {
                if session.connection != nil {
                    LabeledContent("访问令牌", value: "读笔记、交任务，在钥匙串里")
                }
                Button(session.connection == nil ? "改用 GitHub 同步" : "更换令牌或仓库") { reconnect = true }
                Button("断开连接并删除手机上的笔记", role: .destructive) { confirmWipe = true }
            }
            .listRowBackground(Color.clear)
        }
        .readableColumn()
        .listStyle(.insetGrouped)
        .pageStyle()
        .navigationTitle("我的")
        .onAppear { storage = vault.storageBytes }
        .sheet(isPresented: $showPrefs) { ReadPrefsSheet() }
        .sheet(isPresented: $showModel) { ModelSettingsView() }
        .sheet(isPresented: $reconnect) { ConnectView { reconnect = false } }
        .confirmationDialog("断开后手机上的笔记会全部删掉，令牌也从钥匙串里删掉。仓库里的内容不受影响。",
                            isPresented: $confirmWipe, titleVisibility: .visible) {
            Button("断开并删除", role: .destructive) {
                vault.wipe()
                Connection.delete()
                session.connection = nil
                session.cableOnly = false
            }
        }
    }

    private var syncText: String {
        if vault.syncing { return "正在同步：\(vault.progress.done)/\(vault.progress.total)" }
        if let e = vault.lastError { return "上次同步没成功：\(e)" }
        var parts: [String] = []
        if vault.deferred > 0 { parts.append("用的是流量，\(vault.deferred) 份整理稿和文字记录等连上 Wi-Fi 再下") }
        if let t = vault.lastSync {
            parts.append("上次同步 \(t.formatted(.relative(presentation: .named)))，来自\(vault.lastSource)")
        } else {
            parts.append(session.connection == nil ? "还没从电脑拷过来" : "还没同步过")
        }
        if session.connection == nil {
            parts.append("插上数据线后，在电脑上 Spark 的设置页点「同步到这台」")
        }
        return parts.joined(separator: "。") + "。"
    }
}
