import SwiftUI
import UniformTypeIdentifiers

struct ReadPrefsSheet: View {
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var fonts = Fonts.shared

    var body: some View {
        @Bindable var prefs = prefs
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    section("配色")
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 3), spacing: 8) {
                        ForEach(ReadTheme.allCases) { t in
                            let pal = t.resolved(.light)
                            Button { prefs.theme = t } label: {
                                Text(t.label).font(.system(size: 14)).frame(maxWidth: .infinity, minHeight: 44)
                                    .foregroundStyle(pal.ink)
                                    .background {
                                        if t == .auto {
                                            HStack(spacing: 0) { Color.hex(0xf5f4ed); Color.hex(0x1c1b18).frame(width: 14) }
                                        } else { pal.paper }
                                    }
                                    .clipShape(RoundedRectangle(cornerRadius: 10))
                                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(prefs.theme == t ? p.accent : p.rule,
                                                                                       lineWidth: prefs.theme == t ? 2 : 1))
                            }
                            .buttonStyle(.plain)
                            .accessibilityAddTraits(prefs.theme == t ? .isSelected : [])
                        }
                    }

                    HStack(alignment: .firstTextBaseline) {
                        section("正文字体")
                        Text("标题固定用思源黑体").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        Spacer()
                        NavigationLink { AddFontView() } label: { Label("添加字体", systemImage: "plus").font(.system(size: 14)) }
                    }
                    .padding(.top, 8)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(prefs.allFonts) { f in
                                let on = prefs.bodyFontID == f.id
                                Button { prefs.bodyFontID = f.id } label: {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text("永和九年").font(Fonts.font(f.fontName, size: 18))
                                        Text(f.label + (Fonts.isAvailable(f.fontName) ? "" : "（未安装）"))
                                            .font(.system(size: 11.5)).opacity(0.85)
                                    }
                                    .padding(.horizontal, 12).padding(.vertical, 8).frame(minWidth: 92, minHeight: 64, alignment: .leading)
                                    .foregroundStyle(on ? p.paper : p.ink)
                                    .background(on ? p.accent : .clear, in: RoundedRectangle(cornerRadius: 10))
                                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(on ? p.accent : p.rule))
                                }
                                .buttonStyle(.plain)
                                .accessibilityAddTraits(on ? .isSelected : [])
                            }
                        }
                    }

                    section("字号").padding(.top, 8)
                    HStack(spacing: 10) {
                        Button { prefs.size = max(ReadPrefs.sizes.lowerBound, prefs.size - 1) } label: {
                            Text("A").font(.system(size: 15)).frame(width: 52, height: 44)
                                .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.rule))
                        }
                        .accessibilityLabel("字号调小")
                        Slider(value: $prefs.size, in: ReadPrefs.sizes, step: 1)
                        Button { prefs.size = min(ReadPrefs.sizes.upperBound, prefs.size + 1) } label: {
                            Text("A").font(.system(size: 22)).frame(width: 52, height: 44)
                                .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.rule))
                        }
                        .accessibilityLabel("字号调大")
                    }
                    .buttonStyle(.plain)
                    Text("正文 \(Int(prefs.size)) 号。和电脑上阅读页的设置互不影响。")
                        .font(.system(size: 12.5)).foregroundStyle(p.ink2).padding(.top, 8)

                    Text("钱程把自己的人生分为深圳、加拿大、国内三段，各约十一年。The quick brown fox — 0123456789")
                        .font(prefs.body()).lineSpacing(prefs.lineSpacing)
                        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                        .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                        .padding(.top, 16)
                }
                .padding(.horizontal, 20).padding(.bottom, 30)
            }
            .foregroundStyle(p.ink)
            .background(p.paper.ignoresSafeArea())
            .navigationTitle("阅读设置").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }

    private func section(_ t: String) -> some View {
        Text(t).font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2).padding(.top, 10).padding(.bottom, 8)
    }
}

/// 添加字体：从下载链接，或从「文件」里选
struct AddFontView: View {
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.palette) private var p
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    @State private var url = ""
    @State private var working = false
    @State private var error: String?
    @State private var added: BodyFont?
    @State private var picking = false

    /// 常用的开源中文字体：打开它们的发布页，找到字体文件复制链接贴回来
    private let presets: [(String, String, String)] = [
        ("霞鹜文楷", "楷体风格，适合长文阅读（已内置）", "https://github.com/lxgw/LxgwWenKai/releases"),
        ("霞鹜文楷 屏幕阅读版", "笔画加粗，小字号更清楚", "https://github.com/lxgw/LxgwWenKai-Screen/releases"),
        ("思源宋体", "比系统宋体字重更全", "https://github.com/adobe-fonts/source-han-serif/releases"),
        ("思源黑体", "黑体风格，屏幕上清晰，字重齐全（已内置）", "https://github.com/adobe-fonts/source-han-sans/releases"),
        ("朱雀仿宋", "仿宋风格", "https://github.com/TrionesType/zhuque/releases"),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Picker("从哪里添加", selection: $tab) {
                    Text("从链接").tag(0)
                    Text("从文件").tag(1)
                }
                .pickerStyle(.segmented)

                if tab == 0 {
                    Text("字体文件的下载链接").font(.system(size: 14, weight: .semibold)).padding(.top, 16)
                    TextField("https://…/xxx.ttf", text: $url)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        .padding(.horizontal, 12).frame(minHeight: 46)
                        .background(p.paper2, in: RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(p.rule))
                        .padding(.vertical, 6)
                    Text("直接指向 .ttf 或 .otf 文件的链接，比如 GitHub Releases 里的字体文件。")
                        .font(.system(size: 13)).foregroundStyle(p.ink2)
                    Button(working ? "下载中…" : "下载并预览") { Task { await download() } }
                        .buttonStyle(.bordered).disabled(working || url.isEmpty).padding(.top, 10)

                    Text("常用的开源中文字体").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2)
                        .padding(.top, 24).padding(.bottom, 4)
                    ForEach(presets, id: \.0) { name, desc, page in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(name).font(.system(size: 15))
                                Text(desc).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                            }
                            Spacer()
                            Link("发布页", destination: URL(string: page)!).font(.system(size: 14))
                        }
                        .frame(minHeight: 56)
                        Divider().overlay(p.rule)
                    }
                } else {
                    Button { picking = true } label: {
                        VStack(spacing: 8) {
                            Image(systemName: "doc.badge.plus").font(.system(size: 28)).foregroundStyle(p.accent)
                            Text("从「文件」里选字体").font(.system(size: 15, weight: .semibold))
                            Text(".ttf　.otf，可以是 iCloud 里的").font(.system(size: 13)).foregroundStyle(p.ink2)
                        }
                        .frame(maxWidth: .infinity, minHeight: 150)
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(p.rule, style: StrokeStyle(lineWidth: 1.5, dash: [6])))
                    }
                    .buttonStyle(.plain).padding(.top, 16)
                }

                if let error { Text(error).font(.system(size: 14)).foregroundStyle(.red).padding(.top, 14) }

                if let added {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("预览：\(added.label)").font(.system(size: 13, weight: .semibold)).foregroundStyle(p.ink2)
                        Text("钱程把自己的人生分为深圳、加拿大、国内三段，各约十一年。The quick brown fox — 0123456789")
                            .font(Fonts.font(added.fontName, size: 18)).lineSpacing(8)
                        Button {
                            prefs.bodyFontID = added.id
                            dismiss()
                        } label: {
                            Text("用于阅读").font(.system(size: 15, weight: .semibold))
                                .frame(maxWidth: .infinity, minHeight: 46)
                                .background(p.accent, in: RoundedRectangle(cornerRadius: 10)).foregroundStyle(p.paper)
                        }
                    }
                    .padding(16).background(p.paper2, in: RoundedRectangle(cornerRadius: 12)).padding(.top, 22)
                }

                Text("添加的字体只存在这台手机上，只用于 Spark 里的正文。字体里没有的字会退回系统字体显示。")
                    .font(.system(size: 12.5)).foregroundStyle(p.ink2).padding(.top, 20)
            }
            .padding(20)
        }
        .foregroundStyle(p.ink)
        .background(p.paper.ignoresSafeArea())
        .navigationTitle("添加字体").navigationBarTitleDisplayMode(.inline)
        .fileImporter(isPresented: $picking, allowedContentTypes: [.font, UTType(filenameExtension: "ttf")!, UTType(filenameExtension: "otf")!]) { result in
            switch result {
            case .success(let file):
                let ok = file.startAccessingSecurityScopedResource()
                defer { if ok { file.stopAccessingSecurityScopedResource() } }
                install(file)
            case .failure(let e):
                error = e.localizedDescription
            }
        }
    }

    private func download() async {
        guard let u = URL(string: url.trimmingCharacters(in: .whitespaces)), ["https", "http"].contains(u.scheme ?? "") else {
            error = "链接应该是 https:// 开头"
            return
        }
        working = true
        error = nil
        defer { working = false }
        do {
            let (tmp, resp) = try await URLSession.shared.download(from: u)
            guard (resp as? HTTPURLResponse)?.statusCode ?? 0 < 400 else { throw SparkError("下载失败") }
            let ext = u.pathExtension.isEmpty ? "ttf" : u.pathExtension
            let named = tmp.deletingLastPathComponent().appending(path: UUID().uuidString + "." + ext)
            try FileManager.default.moveItem(at: tmp, to: named)
            install(named)
        } catch {
            self.error = "下载失败：\(error.localizedDescription)"
        }
    }

    private func install(_ file: URL) {
        do {
            added = try Fonts.shared.install(from: file)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}
