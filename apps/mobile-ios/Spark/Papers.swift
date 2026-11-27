import Foundation
import SwiftUI
import UIKit
import UniformTypeIdentifiers

/// 论文在手机上收（电脑 core/papers.py 同一套规则，两端要一致的写在 Shared/Paper.swift）：
/// 手机自己抓、自己取字、自己调模型写摘要（沿用剪藏的 ClipProcessor.store），结果像剪藏一样交给电脑。
/// - arXiv：信息接口拿标题、作者、日期、摘要（不通退回摘要页）；再试网页版全文（Clipper.container + HTMLToMarkdown），
///   404 或正文太短就下 PDF 用 PDFKit 取字。PDF 原件交「库内路径 → 网址」，电脑按网址自己下；
///   手机上也留一份（keepPDF，存进手机上的笔记库，PDF 页签马上能看），这一份不进发件箱（Vault.linkPDFs）。
///   正文取自网页版的也把 PDF 下回来留着，下不成不算失败。
/// - 别的链接：内容是 PDF 的取字（原件同上）；网页照剪藏抓正文，存进论文文件夹。
/// - 本地 PDF（「选 PDF 文件」、分享进来的文件）：原件存进手机上的笔记库（Spark/论文/attachments/<内容哈希>.pdf），
///   文件头照样写 pdf: 那一行。Markdown 照常交给电脑；原件只走数据线，下次整个同步时交（PDFOutbox），不经 GitHub。
/// 网络请求一个接一个，不并发。
enum PaperFetch {
    struct Result {
        var article: Clipper.Article
        var localPDF: Data?       // 本地 PDF：原件存进手机上的笔记库
        var pdfURL: String?       // 来自链接的 PDF：交给电脑按这个网址下
        var keepPDF: Data?        // 来自链接的 PDF：手机上也留一份（文件名按网址算，不是内容哈希，不走发件箱）
        var notes: [String] = []
    }

    /// GET 一个网址。先看 Content-Length，超过 maxBytes 的不下；下的过程中超了也停
    static func get(_ s: String, timeout: TimeInterval = 30, maxBytes: Int = 20 << 20) async throws -> (Data, HTTPURLResponse?) {
        try NetGate.check()
        guard let u = URL(string: s) else { throw SparkError("链接不对") }
        var req = URLRequest(url: u, timeoutInterval: timeout)
        req.setValue(Clipper.userAgent, forHTTPHeaderField: "User-Agent")
        let (bytes, resp) = try await URLSession.shared.bytes(for: req)
        let http = resp as? HTTPURLResponse
        if let code = http?.statusCode, code >= 400 { throw SparkError("打不开（HTTP \(code)）") }
        let tooBig = SparkError(maxBytes >= Paper.maxPDFBytes ? Paper.tooBigMessage : "内容太大，没下")
        if resp.expectedContentLength > Int64(maxBytes) { throw tooBig }
        var data = Data()
        if resp.expectedContentLength > 0 { data.reserveCapacity(Int(resp.expectedContentLength)) }
        for try await b in bytes {
            data.append(b)
            if data.count > maxBytes { throw tooBig }
        }
        return (data, http)
    }

    private static func nonSpace(_ s: String) -> Int {
        s.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.count
    }

    /// 网页版全文的正文和图片（同 Clipper.fetch 的写法，不去公众号广告图）。公式先换成 TeX（Paper.texMath）
    static func htmlBody(_ html: String, base: URL, title: String) -> (String, [String: String]) {
        let page = Paper.texMath(html)
        var conv = HTMLToMarkdown(base: base)
        var md = ClipTidy.clean(Clipper.container(page).map { conv.convert($0) } ?? "", title: title)
        var images: [String: String] = [:]
        let best = ClipImages.canonical(conv.images)
        for (i, u) in best.enumerated() where md.contains("](IMG:\(i))") { images[Clipper.localName(u)] = u.absoluteString }
        for (i, u) in best.enumerated() { md = md.replacingOccurrences(of: "](IMG:\(i))", with: "](\(Clipper.localName(u)))") }
        return (md, images)
    }

    /// 信息接口 → 摘要页，都不通返回 nil（电脑 papers._arxiv_meta）
    static func arxivMeta(_ id: String) async -> Paper.Meta? {
        var meta: Paper.Meta?
        if let (d, _) = try? await get("https://export.arxiv.org/api/query?id_list=\(id)") { meta = Paper.parseAtom(d) }
        if meta == nil, let (d, _) = try? await get(Paper.absURL(id)) { meta = Paper.parseAbs(String(decoding: d, as: UTF8.self)) }
        return meta
    }

    /// 第一页有 arXiv 戳记的 PDF：用信息接口补 published、authors、arxiv（取不到就照原样，不报错）。
    /// 标题、来源、网址、原件名不动（电脑 papers._stamped）
    static func stamped(_ a: Clipper.Article, pages: [String]) async -> Clipper.Article {
        guard let id = Paper.stampID(pages), let meta = await arxivMeta(id) else { return a }
        var a = a
        a.arxiv = id
        a.published = meta.published
        a.authors = meta.authors
        return a
    }

    static func arxiv(_ id: String) async throws -> Result {
        var notes: [String] = []
        let meta = await arxivMeta(id)
        if meta == nil { notes.append("arXiv 的信息接口和摘要页都没打开，标题和作者可能不全") }
        var md = "", images: [String: String] = [:], why = "", pdfTitle = ""
        var pdfData: Data?
        if let (d, resp) = try? await get("https://arxiv.org/html/\(id)") {
            // 图片地址是相对这一页的（x1.png）：按「…/html/<id>v<n>/」解析
            var base = resp?.url ?? URL(string: "https://arxiv.org/html/\(id)")!
            if !base.absoluteString.hasSuffix("/"), let b = URL(string: base.absoluteString + "/") { base = b }
            (md, images) = htmlBody(String(decoding: d, as: UTF8.self), base: base, title: meta?.title ?? "")
            md = Paper.dropTitleHeading(md, title: meta?.title ?? "")
        }
        if nonSpace(md) < Paper.htmlMinChars {
            md = ""; images = [:]
            do {
                let (d, _) = try await get(Paper.pdfURL(id), timeout: 90, maxBytes: Paper.maxPDFBytes)
                let t = try Paper.pdfText(d)
                md = Paper.pdfMarkdown(t.pages)
                pdfTitle = t.title
                pdfData = d
            } catch { why = error.localizedDescription }
        } else if let (d, _) = try? await get(Paper.pdfURL(id), timeout: 90, maxBytes: Paper.maxPDFBytes), Paper.isPDF(d) {
            pdfData = d   // 正文用网页版的：PDF 原件也下回来留在手机上看
        } else {
            notes.append("PDF 原件这次没下到手机上，电脑收下后接数据线同步过来")
        }
        let title = meta?.title ?? pdfTitle
        if md.isEmpty {
            guard let summary = meta?.summary, !summary.isEmpty else {
                throw SparkError("没取到这篇论文的正文（\(why.isEmpty ? "网页版和 PDF 都打不开" : why)）")
            }
            md = summary
            notes.append("正文没取到（\(why.isEmpty ? "网页版和 PDF 都打不开" : why)），先存了 arXiv 上的摘要")
        }
        let a = Clipper.Article(url: Paper.absURL(id), title: title.isEmpty ? "arXiv \(id)" : title, source: Paper.arxivSource,
                                published: meta?.published ?? "", markdown: md, images: images, titleGuessed: title.isEmpty ? true : nil,
                                authors: meta?.authors, arxiv: id, pdf: Paper.pdfName(url: Paper.pdfURL(id)))
        return Result(article: a, localPDF: nil, pdfURL: Paper.pdfURL(id), keepPDF: pdfData, notes: notes)
    }

    /// 存为论文的链接。prefetched：收件箱条目文件夹里已有的 PDF 字节（旧版存过的）
    static func link(_ s: String, prefetched: Data?) async throws -> Result {
        if let id = Paper.arxivID(s) { return try await arxiv(id) }
        guard let u = URL(string: s) else { throw SparkError("链接不对") }
        var data = prefetched
        if data == nil {
            do {
                return Result(article: try await Clipper.fetch(u), localPDF: nil, pdfURL: nil)   // 网页：照剪藏抓正文
            } catch is Clipper.PDFLink {   // 是一份 PDF：按带上限的下载取回来
                data = try await get(s, timeout: 90, maxBytes: Paper.maxPDFBytes).0
            }
        }
        let t = try Paper.pdfText(data ?? Data())
        let title = t.title.isEmpty ? Paper.title(fileName: u.path) : t.title
        var a = Clipper.Article(url: s, title: title.isEmpty ? LaterRule.stubTitle(s) : title, source: LaterRule.stubSource(s),
                                published: "", markdown: Paper.pdfMarkdown(t.pages), images: [:], titleGuessed: t.title.isEmpty ? true : nil,
                                pdf: Paper.pdfName(url: s))
        a = await stamped(a, pages: t.pages)
        return Result(article: a, localPDF: nil, pdfURL: s, keepPDF: data)
    }

    /// 本地的一份 PDF。没有元数据标题、只能用文件名时 titleGuessed（写摘要时顺带起标题，链接 PDF 同样）
    static func file(_ data: Data, name: String) async throws -> Result {
        guard Paper.isPDF(data) else { throw SparkError(Paper.notPDFMessage) }
        let t = try Paper.pdfText(data)
        let title = t.title.isEmpty ? Paper.title(fileName: name) : t.title
        var a = Clipper.Article(url: "", title: title.isEmpty ? "未命名" : title, source: Paper.fileSource, published: "",
                                markdown: Paper.pdfMarkdown(t.pages), images: [:], titleGuessed: t.title.isEmpty ? true : nil,
                                pdf: Paper.pdfName(data: data))
        a = await stamped(a, pages: t.pages)
        return Result(article: a, localPDF: data, pdfURL: nil)
    }
}

extension ClipProcessor {
    /// 论文里已有的同一篇：同一个 arXiv 编号（不分版本）、同一个网址、同一份 PDF（文件名里的哈希）
    func existingPaper(_ vault: Vault, arxiv: String?, url: String?, pdf: String?) -> ClipMeta? {
        clipMetas(vault, folder: PathRules.paperFolder).first { m in
            if let arxiv, m.arxiv == arxiv || Paper.arxivID(m.url) == arxiv { return true }
            if let url, !url.isEmpty, !m.url.isEmpty, ClipLink.same(m.url, url) { return true }
            if let pdf, m.pdf == pdf { return true }
            return false
        }
    }

    /// 收件箱里的一条论文：查重、取内容、下图片，交给 store（摘要、写文件、交给电脑都同剪藏）。
    /// 结果记进 saved / failed / notes；已经有的、存好的、确定存不了的（本地 PDF 取不出字、超上限）从收件箱拿掉
    func storePaper(_ e: ClipInbox.Entry, vault: Vault, config: LLM.Config?, saved: inout [String], failed: inout [String],
                    notes: inout [String]) async {
        guard let dir = ClipInbox.folder(e.id) else { return }
        let local = try? Data(contentsOf: dir.appending(path: ClipInbox.fileName))
        let label = e.file ?? e.url
        let id = e.file == nil ? Paper.arxivID(e.url) : nil
        if let old = existingPaper(vault, arxiv: id, url: e.file == nil && id == nil ? e.url : nil,
                                   pdf: e.file != nil ? local.map(Paper.pdfName(data:)) : nil) {
            notes.append("《\(old.title)》已经在论文里了，没有再存")
            ClipInbox.remove(e.id)
            return
        }
        setStatus("正在取论文内容：\(label.prefix(40))")
        do {
            let r: PaperFetch.Result
            if let name = e.file {
                guard let local else { throw SparkError("收件箱里的 PDF 不见了") }
                r = try await PaperFetch.file(local, name: name)
            } else {
                r = try await PaperFetch.link(e.url, prefetched: local)
            }
            var a = r.article
            // PDF 带 arXiv 戳记的，取到编号后再按编号查一次重（电脑 papers._run）
            if id == nil, let stamp = a.arxiv, !stamp.isEmpty, let old = existingPaper(vault, arxiv: stamp, url: nil, pdf: nil) {
                notes.append("《\(old.title)》已经在论文里了，没有再存")
                ClipInbox.remove(e.id)
                return
            }
            notes += r.notes
            // PDF 原件放进条目文件夹的 attachments/：store 从那里拷进手机上的笔记库。
            // 本地 PDF 之后走数据线交给电脑（PDFOutbox）；来自链接的电脑按网址自己下，手机这份只是留着看
            if let data = r.localPDF, let pdf = a.pdf {
                try FileManager.default.createDirectory(at: dir.appending(path: Clipper.attachments), withIntermediateDirectories: true)
                try data.write(to: dir.appending(path: pdf), options: .atomic)
            } else if let data = r.keepPDF, let pdf = a.pdf {
                // 留着看的这份写不下（手机空间不够）：不算收录失败，电脑照旧按网址下，之后同步过来
                try? FileManager.default.createDirectory(at: dir.appending(path: Clipper.attachments), withIntermediateDirectories: true)
                if (try? data.write(to: dir.appending(path: pdf), options: .atomic)) == nil {
                    notes.append("PDF 原件这次没存到手机上，电脑收下后接数据线同步过来")
                }
            }
            if !a.images.isEmpty {
                await Clipper.downloadImages(a.images, into: dir)
                a = Clipper.mergeDownloaded(a, dir: dir)
            }
            let title = try await store(&a, note: e.note, created: e.created, dir: dir, vault: vault, config: config, notes: &notes,
                                        later: e.later == true, folder: PathRules.paperFolder, pdfURL: r.pdfURL)
            saved.append(title)
            ClipInbox.remove(e.id)
        } catch {
            let why = error.localizedDescription
            failed.append("\(label)：\(why)")
            // 本地 PDF、扫描版、超上限的再试也一样；链接抓了三天都不行的也不留了（同剪藏）
            let final = e.file != nil || [Paper.scannedMessage, Paper.tooBigMessage, Paper.tooLongMessage, Paper.notPDFMessage].contains(why)
            if final || e.created < Date().addingTimeInterval(-3 * 86400) { ClipInbox.remove(e.id) }
        }
    }
}

/// 系统的文件选择器，只选 PDF。拿到的是一份拷贝（asCopy），不用申请访问权限。
/// 不用 SwiftUI 的 .fileImporter：挂在工具栏的 Menu 上时点了不弹出来（模拟器上试过），改用 sheet 包一层
struct PDFPicker: UIViewControllerRepresentable {
    let onPick: (URL) -> Void

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.pdf], asCopy: true)
        picker.allowsMultipleSelection = false
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick) }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL) -> Void
        init(onPick: @escaping (URL) -> Void) { self.onPick = onPick }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let u = urls.first { onPick(u) }
        }
    }
}
