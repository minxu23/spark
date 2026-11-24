import SwiftUI
import UIKit

/// 电脑上的清理工具（core/cleanup.py）写进笔记库的 output/.cleanup.json：上次结果、最近一次扫描、待确认几张。
/// 判断和执行都在电脑上；手机只发起扫描、看报告、交人的选择（任务 type=cleanup），电脑收到后每一张再查一遍。
struct CleanupInfo: Decodable {
    struct Item: Decodable, Hashable { let path: String; let size: Int? }
    struct Evidence: Decodable, Hashable { let doc: String; let kind: String; let snippets: [String]?; let docs: [String]? }
    struct Unsure: Decodable, Identifiable {
        let path: String; let size: Int?; let reasons: [String]?; let evidence: [Evidence]?
        var id: String { path }
    }
    struct Member: Decodable, Hashable { let path: String; let refs: Int? }
    struct Group: Decodable, Identifiable {
        let sha256: String; let size: Int?; let keeper: String; let note: String?; let members: [Member]
        var id: String { sha256 }
    }
    struct Conflicts: Decodable { let count: Int; let names: [String]? }
    struct Unreadable: Decodable, Hashable { let doc: String; let error: String? }
    struct Held: Decodable, Hashable { let doc: String; let count: Int? }
    struct Scan: Decodable {
        let id: String; let at: String; let mode: String?; let source: String?
        let total: Int?; let used: Int?; let certain: [Item]; let certain_bytes: Int?
        let unsure: [Unsure]; let dups: [Group]; let conflicts: Conflicts?; let unreadable: [Unreadable]?
        let held: [Held]?
    }
    struct Entry: Decodable, Hashable { let path: String; let size: Int?; let by: String?; let reason: String? }
    struct Merged: Decodable, Hashable { let keeper: String; let docs: [String]? }
    struct Report: Decodable {
        let at: String; let mode: String; let source: String?
        let cleaned: [Entry]; let kept: [Entry]; let merged: [Merged]?; let problems: [String]?
        let pending: Int?; let summary: String?
    }
    let schedule_label: String?
    let report: Report?
    let scan: Scan?
    let pending: Int?
    let paused: Bool?

    static let path = "output/.cleanup.json"
}

struct CleanupView: View {
    @Environment(Vault.self) private var vault
    @Environment(\.palette) private var p
    @State private var info: CleanupInfo?
    @State private var problem: String?
    @State private var note: String?
    @State private var choices: [String: String] = [:]   // 路径 → keep / clean / app
    @State private var merges: Set<String> = []
    @State private var certain = false
    @State private var scanID = ""

    var body: some View {
        List {
            Section {
                Text("清理电脑上笔记库 attachments/ 里没有文档在用的图片、一模一样的重复图片。电脑上只移进废纸篓，不永久删除。判断都在电脑上做，结果同步回来。")
                    .font(.system(size: 13)).foregroundStyle(p.ink2)
                if let note { Text(note).font(.system(size: 13)).foregroundStyle(p.accent) }
                if let problem { Text(problem).font(.system(size: 13)).foregroundStyle(p.ink2) }
                if info?.paused == true {
                    Text("电脑上的清理工具暂停了（记录坏过）：先在电脑上的清理工具页处理、确认，手机上交的清理才会做。")
                        .font(.system(size: 13)).foregroundStyle(p.accent)
                }
            }
            .listRowBackground(Color.clear)

            Section("定期清理") {
                row("多久一次", info?.schedule_label ?? "每周", hint: "在电脑上的清理工具页里改")
                if let r = info?.report {
                    NavigationLink { CleanupReportView(report: r) } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("上次清理：\(r.at)，\(r.mode)")
                            Text(r.summary ?? "移进废纸篓 \(r.cleaned.count) 张").font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        }
                    }
                } else {
                    Text("还没清理过").foregroundStyle(p.ink2)
                }
                row("待确认", "\(info?.pending ?? 0) 张", hint: nil)
            }

            Section {
                Button("开始深度扫描") { submit(title: "清理工具：深度扫描", task: ["type": "cleanup", "action": "scan"]) }
            } footer: {
                Text("只扫描、出报告，不动文件。电脑做完、同步回来后在下面选。")
            }

            if let sc = info?.scan { scanSections(sc) }
        }
        .readableColumn()
        .pageStyle()
        .navigationTitle("清理工具")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .onChange(of: vault.syncing) { _, now in if !now { Task { await load() } } }
    }

    @ViewBuilder private func scanSections(_ sc: CleanupInfo.Scan) -> some View {
        Section {
            Text("\(sc.at) 扫描：共 \(sc.total ?? 0) 张，确定在用 \(sc.used ?? 0) 张").font(.system(size: 13)).foregroundStyle(p.ink2)
            Toggle("清掉确定没人用的 \(sc.certain.count) 张（\(bytes(sc.certain_bytes ?? 0))）", isOn: $certain)
                .disabled(sc.certain.isEmpty)
        } header: { Text("确定没人用") }

        Section {
            if sc.unsure.isEmpty { Text("没有拿不准的").foregroundStyle(p.ink2) }
            ForEach(sc.unsure) { u in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .top, spacing: 10) {
                        thumb(u.path)
                        VStack(alignment: .leading, spacing: 3) {
                            Text((u.path as NSString).lastPathComponent).font(.system(size: 14, weight: .medium)).lineLimit(1)
                            Text((u.reasons ?? []).prefix(2).joined(separator: "；")).font(.system(size: 12.5)).foregroundStyle(p.ink2)
                        }
                    }
                    DisclosureGroup("文件名出现在哪") {
                        ForEach(Array((u.evidence ?? []).enumerated()), id: \.offset) { _, e in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(e.kind == "unreadable" ? "读不出来的文档：\((e.docs ?? []).joined(separator: "、"))" : e.doc)
                                    .font(.system(size: 12)).foregroundStyle(p.ink2)
                                ForEach(Array((e.snippets ?? []).enumerated()), id: \.offset) { _, s in
                                    Text(s).font(.system(size: 12, design: .monospaced)).lineLimit(8)
                                }
                            }
                        }
                    }
                    .font(.system(size: 12.5))
                    Picker("怎么处理", selection: Binding(get: { choices[u.path] ?? "" }, set: { choices[u.path] = $0.isEmpty ? nil : $0 })) {
                        Text("不选").tag("")
                        Text("留").tag("keep")
                        Text("清").tag("clean")
                        Text("交给应用").tag("app")
                    }
                    .pickerStyle(.segmented)
                }
                .padding(.vertical, 4)
            }
        } header: {
            Text("拿不准（\(sc.unsure.count) 张）")
        } footer: {
            Text("交给应用：电脑先按规则判断，只有「文件名出现了、但不是图片引用」的才问模型，模型明确说没在用才清。有文档读不出来的不交给模型。")
        }

        Section {
            if sc.dups.isEmpty { Text("没有重复图片").foregroundStyle(p.ink2) }
            ForEach(sc.dups) { g in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) { ForEach(g.members.prefix(4), id: \.self) { m in thumb(m.path) } }
                    ForEach(g.members, id: \.self) { m in
                        Text("\(m.path == g.keeper ? "留" : "合并掉")：\((m.path as NSString).lastPathComponent)（\(m.refs ?? 0) 篇在用）")
                            .font(.system(size: 12.5))
                    }
                    if let n = g.note, !n.isEmpty { Text(n).font(.system(size: 12)).foregroundStyle(p.accent) }
                    Toggle("合并这一组", isOn: Binding(get: { merges.contains(g.sha256) },
                                                  set: { if $0 { merges.insert(g.sha256) } else { merges.remove(g.sha256) } }))
                }
                .padding(.vertical, 4)
            }
        } header: {
            Text("重复图片（\(sc.dups.count) 组）")
        } footer: {
            Text("合并：留引用最多的那张，文档里的引用换过去，别的几张移进电脑的废纸篓。重复图片不交给模型。")
        }

        if let held = sc.held, !held.isEmpty {
            Section {
                Text("\(held.count) 篇，共 \(held.reduce(0) { $0 + ($1.count ?? 0) }) 张图").font(.system(size: 13))
            } header: { Text("被废纸篓里的文章留着") } footer: {
                Text("这些文章删了、还在废纸篓里，可能放回，它们用过的图不清。手机上已经没有某一篇了，可以在电脑的清理工具页释放它的图。")
            }
        }
        if let c = sc.conflicts, c.count > 0 {
            Section {
                ForEach(Array((c.names ?? []).enumerated()), id: \.offset) { Text($0.element).font(.system(size: 12.5)) }
            } header: { Text("冲突副本（\(c.count) 份）") } footer: { Text("只列出来，清理工具不动它们。") }
        }
        if let un = sc.unreadable, !un.isEmpty {
            Section("读不出来的文档") {
                ForEach(Array(un.enumerated()), id: \.offset) { Text("\($0.element.doc)：\($0.element.error ?? "")").font(.system(size: 12.5)) }
            }
        }

        Section {
            Button("按选择清理") { apply(sc, delegateAll: false) }.disabled(!hasChoice)
            Button("拿不准的全部交给应用判断") { apply(sc, delegateAll: true) }.disabled(sc.unsure.isEmpty)
        } footer: {
            Text("电脑收到后每一张再查一遍：那时候有文档在用了就不清。")
        }
    }

    private var hasChoice: Bool { certain || !choices.isEmpty || !merges.isEmpty }

    private func apply(_ sc: CleanupInfo.Scan, delegateAll: Bool) {
        var task: [String: Any] = ["type": "cleanup", "action": "apply", "key": UUID().uuidString.lowercased(), "scan_id": sc.id]
        if delegateAll {
            task["delegate"] = "all"
        } else {
            task["certain"] = certain
            task["clean"] = choices.filter { $0.value == "clean" }.map(\.key).sorted()
            task["keep"] = choices.filter { $0.value == "keep" }.map(\.key).sorted()
            task["delegate"] = choices.filter { $0.value == "app" }.map(\.key).sorted()
            task["merge"] = merges.sorted()
        }
        submit(title: delegateAll ? "清理工具：交给应用判断" : "清理工具：按选择清理", task: task)
    }

    private func submit(title: String, task: [String: Any]) {
        Task {
            do {
                let route = try await vault.submitTask((title: title, task: task))
                note = vault.routeNote(route) + "电脑做完、同步回来后这里会更新。"
            } catch {
                note = "没交上：\(error.localizedDescription)"
            }
        }
    }

    private func load() async {
        guard let text = try? await vault.text(CleanupInfo.path), let data = text.data(using: .utf8) else {
            problem = info == nil ? "还没有电脑上的清理结果。电脑上的 Spark 做过一次清理或扫描、同步过来以后这里才有内容。" : nil
            return
        }
        do {
            let got = try JSONDecoder().decode(CleanupInfo.self, from: data)
            if got.scan?.id != scanID { choices = [:]; merges = []; certain = false; scanID = got.scan?.id ?? "" }
            info = got
            problem = nil
        } catch {
            problem = "电脑上的清理结果读不出来：\(error.localizedDescription)"
        }
    }

    /// 缩略图只用手机上已经有的图片，没有就放个占位（不为看缩略图去下载）
    @ViewBuilder private func thumb(_ path: String) -> some View {
        let url = Vault.root.appending(path: path, directoryHint: .notDirectory)
        if let img = UIImage(contentsOfFile: url.path) {
            Image(uiImage: img).resizable().scaledToFill().frame(width: 56, height: 56).clipShape(RoundedRectangle(cornerRadius: 6))
        } else {
            RoundedRectangle(cornerRadius: 6).fill(p.rule.opacity(0.4)).frame(width: 56, height: 56)
                .overlay(Image(systemName: "photo").foregroundStyle(p.ink2))
                .accessibilityLabel("手机上没有这张图")
        }
    }

    private func row(_ title: String, _ value: String, hint: String?) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if let hint { Text(hint).font(.system(size: 12)).foregroundStyle(p.ink2) }
            }
            Spacer()
            Text(value).foregroundStyle(p.ink2)
        }
    }

    private func bytes(_ n: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file) }
}

/// 一次清理的报告：移进废纸篓的、没清的（为什么、谁判断的）、合并的、问题
struct CleanupReportView: View {
    let report: CleanupInfo.Report
    @Environment(\.palette) private var p

    var body: some View {
        List {
            Section {
                Text("\(report.at)，\(report.mode)（\(report.source ?? "")发起）").font(.system(size: 13)).foregroundStyle(p.ink2)
            }
            if !report.cleaned.isEmpty {
                Section("移进了废纸篓（\(report.cleaned.count) 张）") {
                    ForEach(Array(report.cleaned.enumerated()), id: \.offset) { entry($0.element) }
                }
            }
            if let m = report.merged, !m.isEmpty {
                Section("合并的重复图片") {
                    ForEach(Array(m.enumerated()), id: \.offset) { Text("留 \(($0.element.keeper as NSString).lastPathComponent)，改了 \($0.element.docs?.count ?? 0) 篇文档的引用").font(.system(size: 13)) }
                }
            }
            if !report.kept.isEmpty {
                Section("没清的（\(report.kept.count) 张）") {
                    ForEach(Array(report.kept.enumerated()), id: \.offset) { entry($0.element) }
                }
            }
            if let probs = report.problems, !probs.isEmpty {
                Section("问题") { ForEach(Array(probs.enumerated()), id: \.offset) { Text($0.element).font(.system(size: 13)) } }
            }
        }
        .readableColumn()
        .pageStyle()
        .navigationTitle("清理报告")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func entry(_ e: CleanupInfo.Entry) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text((e.path as NSString).lastPathComponent).font(.system(size: 14))
            Text("\(e.reason ?? "")（\(e.by ?? "")）").font(.system(size: 12.5)).foregroundStyle(p.ink2)
        }
    }
}
