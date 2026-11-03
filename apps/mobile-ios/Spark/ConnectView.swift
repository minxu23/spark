import SwiftUI

/// 第一次打开：填仓库和令牌，选先下载多少。「我的」里「更换令牌或仓库」也用这一页。
struct ConnectView: View {
    var onDone: (() -> Void)? = nil
    @Environment(Session.self) private var session
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p

    @State private var repo = "minxu23/spark-vault"
    @State private var token = ""
    @State private var scope: Vault.DownloadScope = .light
    @State private var apiBase = "https://api.github.com"
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("连接你的笔记库")
                    .font(.heading(28))
                    .padding(.top, 40)
                Text("电脑上的 Spark 会把笔记推到你的 GitHub 私有仓库，手机从那里读，不改动里面的笔记。在手机上添加的任务记成这个仓库的 Issue，电脑上的 Spark 看到后去处理。")
                    .font(.system(size: 15)).foregroundStyle(p.ink2).lineSpacing(4)
                    .padding(.top, 10).padding(.bottom, 22)

                field("仓库") {
                    TextField("用户名/仓库名", text: $repo)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                field("访问令牌") {
                    SecureField("github_pat_…", text: $token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .textContentType(.password)
                }
                Text("在 GitHub 建一个细粒度令牌：只选这个仓库，权限给「Contents：只读」和「Issues：读写」（交任务用）。令牌只存在这台手机的钥匙串里。")
                    .font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(3)
                Link("去 GitHub 建令牌", destination: URL(string: "https://github.com/settings/personal-access-tokens/new")!)
                    .font(.system(size: 14)).foregroundStyle(p.accent).frame(minHeight: 40)

                Text("先下载哪些").font(.system(size: 14, weight: .semibold)).padding(.top, 14).padding(.bottom, 8)
                option(.light, "小结和节目总结", "整理稿和文字记录打开时再下载，读过的留在手机上。推荐。")
                option(.all, "全部", "没网也能看文字记录，有 200 多 MB。建议连着 Wi-Fi 下载。")

                #if DEBUG
                DisclosureGroup("开发用") {
                    field("API 地址") {
                        TextField("https://api.github.com", text: $apiBase)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    }
                }
                .font(.system(size: 13)).foregroundStyle(p.ink2).padding(.top, 8)
                #endif

                if let error {
                    Text(error).font(.system(size: 14)).foregroundStyle(.red).padding(.top, 12)
                }

                Button(action: connect) {
                    HStack {
                        if checking { ProgressView().tint(p.paper) }
                        Text(checking ? "正在连接…" : "连接并下载").font(.system(size: 16, weight: .semibold))
                    }
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .background(p.accent, in: RoundedRectangle(cornerRadius: 12))
                    .foregroundStyle(p.paper)
                }
                .disabled(checking || token.isEmpty)
                .padding(.top, 24)
                Text("之后随时可以在「我的」里改").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                    .frame(maxWidth: .infinity).padding(.top, 8)

                if session.connection == nil {
                    Button {
                        session.cableOnly = true
                        onDone?()
                    } label: {
                        Text("不用 GitHub，只用数据线同步").font(.system(size: 15)).foregroundStyle(p.accent)
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .padding(.top, 18)
                    Text("插上数据线，在电脑上 Spark 的设置页点「同步到这台」，笔记就直接拷进来，手机上添加的任务也一起带回电脑。手机上的 Spark 要是从 Xcode 装的。")
                        .font(.system(size: 12.5)).foregroundStyle(p.ink2).lineSpacing(3).multilineTextAlignment(.center)
                }
            }
            .padding(.horizontal, 24).padding(.bottom, 40)
        }
        .readableColumn()
        .foregroundStyle(p.ink)
        .background(p.paper.ignoresSafeArea())
        .onAppear {
            if let c = session.connection { repo = c.slug; apiBase = c.apiBase }
            scope = vault.scope
        }
    }

    private func field<V: View>(_ label: String, @ViewBuilder _ input: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 14, weight: .semibold))
            input()
                .font(.system(size: 16))
                .padding(.horizontal, 12).frame(minHeight: 46)
                .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.rule))
        }
        .padding(.bottom, 14)
    }

    private func option(_ value: Vault.DownloadScope, _ title: String, _ desc: String) -> some View {
        Button { scope = value } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: scope == value ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(p.accent).font(.system(size: 20))
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.system(size: 15, weight: .semibold))
                    Text(desc).font(.system(size: 13)).foregroundStyle(p.ink2).lineSpacing(2)
                }
                Spacer(minLength: 0)
            }
            .padding(14)
            .background(scope == value ? p.paper2 : .clear, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(scope == value ? p.accent : p.rule, lineWidth: scope == value ? 2 : 1))
        }
        .buttonStyle(.plain)
        .padding(.bottom, 8)
        .accessibilityAddTraits(scope == value ? .isSelected : [])
    }

    private func connect() {
        guard let (owner, name) = Connection.parseSlug(repo) else {
            error = "仓库请填「用户名/仓库名」，比如 minxu23/spark-vault"
            return
        }
        let base = apiBase.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let conn = Connection(owner: owner, repo: name, token: token.trimmingCharacters(in: .whitespacesAndNewlines),
                              apiBase: base.isEmpty ? "https://api.github.com" : base)
        checking = true
        error = nil
        Task {
            defer { checking = false }
            do {
                _ = try await GitHub(conn).head()   // 能读到最新提交，说明仓库和令牌都对
                try conn.save()
                vault.scope = scope
                session.connection = conn
                session.cableOnly = false
                onDone?()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
