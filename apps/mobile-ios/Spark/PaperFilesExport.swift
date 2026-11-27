import Foundation

// MARK: - 依赖 Vault 的部分（纯规则在 PaperFiles.swift）

/// 一次只跑一轮；跑的时候又被要求跑，等这轮完了再跑一次
@MainActor
enum PaperFilesExporter {
    private static var running = false
    private static var again = false

    private static func load() -> [String: PaperFiles.Record] {
        guard let data = UserDefaults.standard.data(forKey: PaperFiles.exportedKey) else { return [:] }
        return (try? JSONDecoder().decode([String: PaperFiles.Record].self, from: data)) ?? [:]
    }

    private static func save(_ records: [String: PaperFiles.Record]) {
        if let data = try? JSONEncoder().encode(records) { UserDefaults.standard.set(data, forKey: PaperFiles.exportedKey) }
    }

    static func run(_ vault: Vault) {
        if Fixture.isOn { return }
        if running { again = true; return }
        running = true
        // 论文列表和记录在主线程取，文件操作交给后台
        let papers: [(pdf: String, title: String)] = ClipProcessor.shared.clipMetas(vault, folder: PathRules.paperFolder).compactMap { m in
            guard !m.pdf.isEmpty else { return nil }
            let key = (m.pdf.hasPrefix(PathRules.paperFolder + "/") ? "" : PathRules.paperFolder + "/") + m.pdf
            return (key, m.title)
        }
        let records = load()
        // 书架不可靠的时候这一轮只拷不收：正在同步（同步完 Vault 会再跑一轮）；同步来的文件树少了一大批、还没确认（holdingRemovals）
        let gone = vault.syncing || vault.holdingRemovals ? [] : PaperFiles.gone(records: records, papers: Set(papers.map(\.pdf)))
        Task.detached(priority: .utility) {
            removeGone(gone.compactMap { p in records[p].map { (p, $0) } })
            let done = copyAll(papers: papers, exported: Set(records.keys).subtracting(gone))
            await MainActor.run {
                var all = load()
                for p in gone where all[p] == records[p] { all[p] = nil }   // 收走的（或者找不到、改过的）：记录去掉，放回原处时再拷
                for (p, r) in done { all[p] = r }
                if all != load() { save(all) }
                running = false
                if again { again = false; run(vault) }
            }
        }
    }

    private nonisolated static var folder: URL {
        URL.documentsDirectory.appending(path: PaperFiles.folderName, directoryHint: .isDirectory)
    }

    /// 后台：论文没了的那几份，文件还叫记着的名字、内容还是拷的时候那份才删（PaperFiles.mayDelete）
    nonisolated private static func removeGone(_ items: [(pdf: String, record: PaperFiles.Record)]) {
        let fm = FileManager.default
        for (_, r) in items {
            guard !r.name.contains("/"), !r.name.hasPrefix(".") else { continue }   // 名字不对的不拼路径（mayDelete 里还会再验）
            let url = folder.appending(path: r.name, directoryHint: .notDirectory)
            guard fm.fileExists(atPath: url.path), PaperFiles.mayDelete(r, fileSHA: PDFOutbox.sha256File(url)) else { continue }
            try? fm.removeItem(at: url)
        }
    }

    /// 后台：拷到「文档/论文/」，返回拷成功的（库内路径 → 文件名和内容哈希）。先拷成 .<名字>.partial 再改名；不盖、不改名已有的文件
    nonisolated private static func copyAll(papers: [(pdf: String, title: String)], exported: Set<String>) -> [String: PaperFiles.Record] {
        let fm = FileManager.default
        let root = Vault.root
        let plan = PaperFiles.plan(papers: papers, exported: exported) {
            fm.fileExists(atPath: root.appending(path: $0, directoryHint: .notDirectory).path)
        }
        guard !plan.isEmpty else { return [:] }
        let dir = folder
        do { try fm.createDirectory(at: dir, withIntermediateDirectories: true) } catch { return [:] }
        var done: [String: PaperFiles.Record] = [:]
        for item in plan {
            let src = root.appending(path: item.pdf, directoryHint: .notDirectory)
            let name = PaperFiles.freeName(PaperFiles.fileName(title: item.title)) {
                fm.fileExists(atPath: dir.appending(path: $0).path)
            }
            let final = dir.appending(path: name, directoryHint: .notDirectory)
            if fm.fileExists(atPath: final.path) { continue }
            let tmp = dir.appending(path: ".\(name).partial", directoryHint: .notDirectory)
            do {
                if fm.fileExists(atPath: tmp.path) { try fm.removeItem(at: tmp) }   // 上次中断留下的半成品（自己的临时文件）
                try fm.copyItem(at: src, to: tmp)
                // 记下拷过去的这一份的内容哈希：以后论文没了，内容还是这份才删
                guard let sha = PDFOutbox.sha256File(tmp) else { try? fm.removeItem(at: tmp); continue }
                try fm.moveItem(at: tmp, to: final)
                done[item.pdf] = PaperFiles.Record(name: name, sha: sha)
            } catch {
                try? fm.removeItem(at: tmp)
            }
        }
        return done
    }
}

extension Vault {
    /// 论文 PDF 在「文件」App（Spark → 论文）里的那一份跟着书架走：有 PDF 的论文拷一份（每个 PDF 只拷一次），
    /// 论文没了的收走。入口：启动、每次同步后、收下论文后、删掉和放回原处后
    func exportPaperPDFs() { PaperFilesExporter.run(self) }
}
