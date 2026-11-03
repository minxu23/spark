import SwiftUI

/// 「我的」里的模型设置：选哪家、填 Key、模型名。Key 存进钥匙串，存过的不再显示出来。
struct ModelSettingsView: View {
    @Environment(LocalJobs.self) private var jobs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var provider: LLM.Provider = LLM.Config.load()?.provider ?? .anthropic
    @State private var key = ""
    @State private var model = ""
    @State private var apiBase = ""
    @State private var hasKey = false
    @State private var testing = false
    @State private var note: String?
    @State private var confirmDelete = false

    var body: some View {
        @Bindable var jobs = jobs
        NavigationStack {
            Form {
                Section {
                    Picker("用哪家", selection: $provider) {
                        ForEach(LLM.Provider.allCases) { Text($0.label).tag($0) }
                    }
                } footer: {
                    Text("跟电脑上 Spark 设置页的模型后端一样。Key 只存在这台设备的钥匙串里，不会同步到电脑或 GitHub。")
                }

                Section {
                    SecureField(hasKey ? "已保存（要换就重新填）" : "粘贴 API Key", text: $key)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    if hasKey {
                        Button("删除这个 Key", role: .destructive) { confirmDelete = true }
                    }
                } header: { Text("API Key") }

                Section {
                    TextField(provider == .openaiCompatible ? "比如 deepseek-chat" : provider.defaultModel, text: $model)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    if !provider.suggestedModels.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(provider.suggestedModels, id: \.self) { m in
                                    Button(m) { model = m }
                                        .font(.system(size: 12.5)).padding(.horizontal, 10).frame(minHeight: 32)
                                        .foregroundStyle(model == m ? p.paper : p.accent)
                                        .background(model == m ? p.accent : .clear, in: Capsule())
                                        .overlay(Capsule().stroke(p.accent))
                                        .buttonStyle(.plain)
                                }
                            }
                        }
                        .contentMargins(.horizontal, 0, for: .scrollContent)
                    }
                } header: { Text("模型") } footer: {
                    Text("出一份报告要调二三十次模型，摘取材料最费。Sonnet 一类均衡的模型就够用。")
                }

                if provider != .anthropic {
                    Section {
                        TextField(provider == .openrouter ? LLM.Config.openrouterBase : "https://api.deepseek.com/v1", text: $apiBase)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    } header: { Text("接口地址") } footer: {
                        Text(provider == .openrouter ? "一般不用改，留空就是 OpenRouter 官方地址。" : "OpenAI Chat Completions 协议的地址，到 /v1 为止。")
                    }
                }

                Section {
                    Toggle("复盘和出报告在手机上生成", isOn: $jobs.runOnPhone)
                    Button(testing ? "正在试…" : "测试一下") { Task { await test() } }
                        .disabled(testing)
                    if let note { Text(note).font(.system(size: 13)).foregroundStyle(p.ink2) }
                } footer: {
                    Text("关掉就还像原来那样交给电脑生成。在手机上生成的复盘和报告先存在手机上，下次同步时也存进电脑上的笔记库。")
                }
            }
            .scrollContentBackground(.hidden)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("模型").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { if save() { dismiss() } }
                        .disabled(!hasKey && key.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear(perform: load)
            .onChange(of: provider) { _, _ in load() }
            .confirmationDialog("删除 \(provider.label) 的 Key？", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("删除", role: .destructive) {
                    LLM.Config.deleteKey(provider)
                    jobs.configVersion += 1
                    load()
                }
            }
        }
    }

    private func load() {
        let s = LLM.Config.stored(provider)
        model = s.model
        apiBase = s.apiBase
        hasKey = s.hasKey
        key = ""
        note = nil
    }

    @discardableResult
    private func save() -> Bool {
        do {
            try LLM.Config.save(provider: provider, model: model, apiBase: apiBase, key: key)
            jobs.configVersion += 1
            hasKey = LLM.Config.stored(provider).hasKey
            key = ""
            return true
        } catch {
            note = error.localizedDescription
            return false
        }
    }

    private func test() async {
        guard save() else { return }
        guard let c = LLM.Config.load() else { note = "先填 Key"; return }
        testing = true
        defer { testing = false }
        do {
            let reply = try await LLM.complete("只回答两个字：可以", config: c, maxTokens: 50, timeout: 60)
            note = "连上了，\(c.label) 回答：\(reply.prefix(20))"
        } catch {
            note = "没连上：\(error.localizedDescription)"
        }
    }
}
