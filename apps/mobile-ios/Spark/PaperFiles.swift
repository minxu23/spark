import Foundation

/// 论文 PDF 自动拷一份到「文件」App（我的 iPhone → Spark → 论文）的规则。
/// Spark 里这篇论文没了（手机上删了、电脑上删了同步过来），拷过去的那一份也删掉——只删认得出是自己拷的、没被改过的：
/// 记着拷过去时的文件名和内容的 SHA-256，文件还叫那个名字、内容还是那份才删；改了名、挪走了、做过批注的都不动。
/// 论文在手机废纸篓里放回原处，下一轮又拷一份。判断只看这篇在不在、文件对不对得上，不看时钟。
/// 这个文件只放纯规则，不依赖 UIKit / SwiftUI / Vault，可以单独编译测试。
enum PaperFiles {
    static let folderName = "论文"
    static let exportedKey = "files.exportedPaperPDFs"

    /// 拷过去的一份：在「文件」里的文件名、拷的时候内容的 SHA-256（十六进制）
    struct Record: Codable, Equatable {
        var name: String
        var sha: String
    }

    /// 记着拷过、现在没有哪篇论文再用的 PDF（库内路径）：对应的那一份该收走，记录也去掉
    /// 一篇带 PDF 的论文都没有了：不收（断开连接、书架还没建好时就是这样，分不清；宁可留着）
    static func gone(records: [String: Record], papers: Set<String>) -> [String] {
        guard !papers.isEmpty else { return [] }
        return records.keys.filter { !papers.contains($0) }.sorted()
    }

    /// 「文件」里这一份能不能删：记录里的名字是论文文件夹下直接的一个 .pdf（不带路径、不是隐藏文件），
    /// 而且文件现在的内容哈希跟拷的时候一样。读不出哈希（文件没了、读不了）或者对不上：不删
    static func mayDelete(_ r: Record, fileSHA: String?) -> Bool {
        guard !r.name.contains("/"), !r.name.contains("\\"), !r.name.hasPrefix("."), r.name.hasSuffix(".pdf"), r.name.count > 4 else { return false }
        guard let fileSHA, !r.sha.isEmpty else { return false }
        return fileSHA == r.sha
    }

    /// 文件名：不能有的字符（/ \ : * ? " < > | 和换行）换成空格，连续空格合成一个，去首尾空白，最多 80 字，空的用「论文」，加 .pdf
    static func fileName(title: String) -> String {
        let bad = CharacterSet(charactersIn: "/\\:*?\"<>|\n\r")
        var clean = title.components(separatedBy: bad).joined(separator: " ")
        while clean.contains("  ") { clean = clean.replacingOccurrences(of: "  ", with: " ") }
        clean = clean.trimmingCharacters(in: .whitespacesAndNewlines)
        clean = String(clean.prefix(80)).trimmingCharacters(in: .whitespacesAndNewlines)
        return (clean.isEmpty ? "论文" : clean) + ".pdf"
    }

    /// 重名的加 _2、_3 …… 最多到 999；都被占了就返回原名（调用方会发现它已存在而跳过）
    static func freeName(_ name: String, taken: (String) -> Bool) -> String {
        guard taken(name) else { return name }
        let stem = name.hasSuffix(".pdf") ? String(name.dropLast(4)) : name
        for n in 2...999 {
            let c = "\(stem)_\(n).pdf"
            if !taken(c) { return c }
        }
        return name
    }

    /// 要拷的：pdf 不在已导出集合里、原件在手机上的；同一个 pdf 只留第一条
    static func plan(papers: [(pdf: String, title: String)], exported: Set<String>,
                     onPhone: (String) -> Bool) -> [(pdf: String, title: String)] {
        var seen = Set<String>()
        var out: [(pdf: String, title: String)] = []
        for p in papers where !p.pdf.isEmpty && !exported.contains(p.pdf) && !seen.contains(p.pdf) {
            seen.insert(p.pdf)
            if onPhone(p.pdf) { out.append(p) }
        }
        return out
    }
}
