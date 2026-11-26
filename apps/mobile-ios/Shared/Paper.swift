import CryptoKit
import Foundation
import PDFKit

/// 论文（Spark/论文/，格式同剪藏）里两端要一致的规则：arXiv 链接的认法、信息接口（Atom）和摘要页的解析、
/// 网页版全文里的公式、PDF 取字（上限、扫描版的认法、段落合并）、文件头多出来的几行（authors、arxiv、pdf）。
/// 电脑上是 core/papers.py、core/sources.py（PDF 的常量）、core/clip_compose.paper_head_lines，
/// tests/test_papers_capture.py 用 swiftc 把这个文件单独编出来，两边对同一张表。
/// 放在 Shared 里：分享扩展认 arXiv 链接也要用。抓取、写摘要在主 App（Spark/Papers.swift）。
enum Paper {
    static let maxPDFBytes = 50 * 1024 * 1024          // 电脑 sources.PDF_MAX_BYTES
    static let maxPDFPages = 1000                       // 电脑 sources.PDF_MAX_PAGES
    static let scannedCharsPerPage = 20                 // 电脑 sources.PDF_SCANNED_CHARS_PER_PAGE
    static let htmlMinChars = 2000                      // 电脑 papers.HTML_MIN_CHARS
    static let maxChars = 400000                        // 送给模型的正文上限（写摘要、追问）；电脑 clip_compose.PAPER_MAX_CHARS
    static let maxAuthors = 20                          // 电脑 clip_compose.MAX_AUTHORS
    static let authorsShown = 5                         // 电脑 clip_compose.AUTHORS_SHOWN
    static let scannedMessage = "这份 PDF 是扫描版，取不出文字"
    static let tooBigMessage = "PDF 超过 50MB，不处理"
    static let tooLongMessage = "PDF 超过 1000 页，不处理"
    static let notPDFMessage = "这不是 PDF 文件"
    static let arxivSource = "arXiv"
    static let fileSource = "PDF"                       // 本地 PDF 没有网址，来源写这个（电脑 papers.FILE_SOURCE）

    // MARK: arXiv 链接

    private static let arxivPattern = #"^(?i:https?://(?:www\.|export\.)?arxiv\.org)/(?:abs|pdf|html)/(\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$"#

    /// arXiv 的 abs、pdf、html 链接里的编号（不带版本号）；不是返回 nil。镜像（alphaxiv 之类）不认
    static func arxivID(_ url: String) -> String? {
        let s = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let re = try? NSRegularExpression(pattern: arxivPattern),
              let m = re.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) else { return nil }
        return (s as NSString).substring(with: m.range(at: 1))
    }

    static func absURL(_ id: String) -> String { "https://arxiv.org/abs/\(id)" }
    static func pdfURL(_ id: String) -> String { "https://arxiv.org/pdf/\(id)" }

    /// 贴进来的链接要不要直接存为论文：arXiv，或者网址以 .pdf 结尾（内容是 PDF 的抓到后才知道，见 Clipper.PDFLink）
    static func isPaperLink(_ url: String) -> Bool {
        if arxivID(url) != nil { return true }
        let bare = url.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false).first ?? ""
        return (bare.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false).first ?? "").lowercased().hasSuffix(".pdf")
    }

    static func isPDF(_ data: Data) -> Bool { data.starts(with: Array("%PDF-".utf8)) }

    /// PDF 原件在论文文件夹里的位置：attachments/<sha256 前 16 位>.pdf。来自链接的按网址算，本地文件按内容算
    static func pdfName(url: String) -> String { name(SHA256.hash(data: Data(url.utf8))) }
    static func pdfName(data: Data) -> String { name(SHA256.hash(data: data)) }
    private static func name(_ d: SHA256.Digest) -> String {
        "attachments/" + d.map { String(format: "%02x", $0) }.joined().prefix(16) + ".pdf"
    }

    // MARK: 信息接口、摘要页

    struct Meta: Equatable {
        var title: String
        var authors: [String]
        var published: String   // YYYY-MM-DD（首次提交）或空
        var summary: String
    }

    static func squash(_ s: String) -> String {
        s.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func day(_ s: String) -> String {
        s.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil ? s : ""
    }

    /// export.arxiv.org/api/query 的 Atom。没有条目、报错条目（id 里有 /api/errors）、XML 坏了都返回 nil（电脑 papers.parse_atom）
    static func parseAtom(_ data: Data) -> Meta? {
        let reader = AtomReader()
        let parser = XMLParser(data: data)
        parser.shouldProcessNamespaces = true
        parser.delegate = reader
        guard parser.parse(), reader.sawEntry, !reader.id.contains("/api/errors") else { return nil }
        let title = squash(reader.title)
        guard !title.isEmpty else { return nil }
        return Meta(title: title, authors: reader.authors.map(squash).filter { !$0.isEmpty },
                    published: day(String(reader.published.trimmingCharacters(in: .whitespacesAndNewlines).prefix(10))),
                    summary: squash(reader.summary))
    }

    /// 只读第一个 <entry> 直接下面的 id、title、published、summary 和 author/name
    private final class AtomReader: NSObject, XMLParserDelegate {
        static let ns = "http://www.w3.org/2005/Atom"
        var stack: [String] = []
        var sawEntry = false, done = false
        var id = "", title = "", published = "", summary = ""
        var authors: [String] = []

        func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName: String?, attributes: [String: String] = [:]) {
            stack.append(namespaceURI == Self.ns ? name : "?")
            if stack == ["feed", "entry"], !done { sawEntry = true }
            if stack == ["feed", "entry", "author", "name"], !done { authors.append("") }
        }

        func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName: String?) {
            if stack == ["feed", "entry"] { done = true }
            stack.removeLast()
        }

        func parser(_ parser: XMLParser, foundCharacters s: String) {
            guard sawEntry, !done, stack.count >= 3, stack[0] == "feed", stack[1] == "entry" else { return }
            switch Array(stack.dropFirst(2)) {
            case ["id"]: id += s
            case ["title"]: title += s
            case ["published"]: published += s
            case ["summary"]: summary += s
            case ["author", "name"]: if !authors.isEmpty { authors[authors.count - 1] += s }
            default: break
            }
        }
    }

    private static func unescape(_ s: String) -> String {
        guard s.contains("&") else { return s }
        var out = s
        for (k, v) in [("&lt;", "<"), ("&gt;", ">"), ("&quot;", "\""), ("&#39;", "'"), ("&apos;", "'")] {
            out = out.replacingOccurrences(of: k, with: v)
        }
        if let re = try? NSRegularExpression(pattern: "&#(x?)([0-9a-fA-F]+);") {
            let ns = out as NSString
            var result = "", last = 0
            for m in re.matches(in: out, range: NSRange(location: 0, length: ns.length)) {
                result += ns.substring(with: NSRange(location: last, length: m.range.location - last))
                let hex = m.range(at: 1).length > 0
                if let v = UInt32(ns.substring(with: m.range(at: 2)), radix: hex ? 16 : 10), let sc = Unicode.Scalar(v) {
                    result.unicodeScalars.append(sc)
                }
                last = m.range.location + m.range.length
            }
            out = result + ns.substring(from: last)
        }
        return out.replacingOccurrences(of: "&amp;", with: "&")
    }

    private static func metaValues(_ html: String, _ name: String) -> [String] {
        guard let tagRE = try? NSRegularExpression(pattern: "<meta\\b[^>]*>", options: .caseInsensitive) else { return [] }
        let ns = html as NSString
        var out: [String] = []
        for m in tagRE.matches(in: html, range: NSRange(location: 0, length: ns.length)) {
            let tag = ns.substring(with: m.range)
            guard tag.range(of: "\\bname\\s*=\\s*[\"']\(NSRegularExpression.escapedPattern(for: name))[\"']",
                            options: [.regularExpression, .caseInsensitive]) != nil else { continue }
            let t = tag as NSString
            guard let c = try? NSRegularExpression(pattern: #"\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*'([^']*)'"#, options: .caseInsensitive),
                  let cm = c.firstMatch(in: tag, range: NSRange(location: 0, length: t.length)) else { continue }
            let r = cm.range(at: 1).location != NSNotFound ? cm.range(at: 1) : cm.range(at: 2)
            let v = squash(unescape(t.substring(with: r)))
            if !v.isEmpty { out.append(v) }
        }
        return out
    }

    /// 信息接口不通时的退路：摘要页里的 citation_title、citation_author（「姓, 名」换成「名 姓」）、citation_date（电脑 papers.parse_abs）
    static func parseAbs(_ html: String) -> Meta? {
        guard let title = metaValues(html, "citation_title").first else { return nil }
        let authors = metaValues(html, "citation_author").map { a -> String in
            let parts = a.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            return parts.count == 2 && !parts[0].isEmpty && !parts[1].isEmpty ? "\(parts[1]) \(parts[0])" : a
        }
        return Meta(title: title, authors: authors,
                    published: day((metaValues(html, "citation_date").first ?? "").replacingOccurrences(of: "/", with: "-")),
                    summary: metaValues(html, "citation_abstract").first ?? "")
    }

    // MARK: 网页版全文里的公式

    /// MathML 换成它带的 TeX（alttext）：行内 $…$，单独成行的写成一段 $$…$$；没有 alttext 的不动（电脑 papers.tex_math）
    static func texMath(_ html: String) -> String {
        guard let re = try? NSRegularExpression(pattern: #"<math\b([^>]*)>.*?</math>"#, options: [.caseInsensitive, .dotMatchesLineSeparators]),
              let alt = try? NSRegularExpression(pattern: #"\balttext=(?:"([^"]*)"|'([^']*)')"#) else { return html }
        let ns = html as NSString
        var out = "", last = 0
        for m in re.matches(in: html, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let attrs = ns.substring(with: m.range(at: 1))
            if let a = alt.firstMatch(in: attrs, range: NSRange(location: 0, length: (attrs as NSString).length)) {
                let tex = (attrs as NSString).substring(with: a.range(at: a.range(at: 1).location != NSNotFound ? 1 : 2))
                out += attrs.range(of: #"\bdisplay=["']block["']"#, options: .regularExpression) != nil ? "<p>$$\(tex)$$</p>" : "$\(tex)$"
            } else {
                out += ns.substring(with: m.range)
            }
            last = m.range.location + m.range.length
        }
        return out + ns.substring(from: last)
    }

    // MARK: PDF

    /// 全文几乎没有文字：非空白字平均每页不到 scannedCharsPerPage 个（电脑 sources.pdf_scanned）
    static func scanned(_ pages: [String]) -> Bool {
        let chars = pages.reduce(0) { $0 + $1.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.count }
        return chars < scannedCharsPerPage * max(pages.count, 1)
    }

    /// PDF 每页的字和标题（元数据里的，没有是空串）。超过 50MB、1000 页、打不开、扫描版都抛 SparkError（文案同电脑）
    static func pdfText(_ data: Data) throws -> (pages: [String], title: String) {
        guard data.count <= maxPDFBytes else { throw SparkError(tooBigMessage) }
        guard isPDF(data), let doc = PDFDocument(data: data) else { throw SparkError("PDF 打不开") }
        guard doc.pageCount <= maxPDFPages else { throw SparkError(tooLongMessage) }
        let pages = (0..<doc.pageCount).map { (doc.page(at: $0)?.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines) }
        if scanned(pages) { throw SparkError(scannedMessage) }
        let title = squash(doc.documentAttributes?[PDFDocumentAttribute.titleAttribute] as? String ?? "")
        return (pages, title)
    }

    /// 没有元数据标题时用文件名：去掉 .pdf，下划线和连字符换成空格（电脑 papers.pdf_title）
    static func title(fileName: String) -> String {
        var base = fileName.components(separatedBy: "/").last ?? ""
        if base.lowercased().hasSuffix(".pdf") { base = String(base.dropLast(4)) }
        return squash(base.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " "))
    }

    private static func titleKey(_ s: String) -> String { squash(s).lowercased() }

    /// 网页版全文里第一个标题行（# 开头）去掉 # 和首尾空白后跟论文标题一样（不分大小写、连续空白当一个），就去掉这一行；
    /// 去掉后上下各有一个空行时再去掉一个空行。只看第一个标题行，别的不动（电脑 papers.drop_title_heading）
    static func dropTitleHeading(_ md: String, title: String) -> String {
        var lines = md.components(separatedBy: "\n")
        for i in lines.indices where lines[i].range(of: #"^#{1,6}\s"#, options: .regularExpression) != nil {
            if !titleKey(title).isEmpty, titleKey(String(lines[i].drop { $0 == "#" })) == titleKey(title) {
                lines.remove(at: i)
                let blank = { (j: Int) in lines[j].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
                if i < lines.count, blank(i), i == 0 || blank(i - 1) { lines.remove(at: i) }
            }
            break
        }
        return lines.joined(separator: "\n")
    }

    private static let endPunct = Set(".!?。！？:：".unicodeScalars)

    private static func cjk(_ c: Unicode.Scalar) -> Bool {
        (0x4E00...0x9FFF).contains(c.value) || (0x3000...0x303F).contains(c.value) || (0xFF00...0xFFEF).contains(c.value)
    }

    /// 按行断开的字合成段落（电脑 papers.paragraphs，同一套规则）：空行分段；行尾「字母-」接小写字母开头的下一行，
    /// 去掉连字符直接拼；两边都是汉字直接拼，别的加一个空格；以句末标点结尾、又不到这一页最长行 80% 的行算一段的结尾。每页另起一段
    static func paragraphs(_ pages: [String]) -> String {
        var out: [String] = []
        for page in pages {
            let lines = page.components(separatedBy: "\n").map { Array($0.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars) }
            let width = lines.map(\.count).max() ?? 0
            var para: [Unicode.Scalar] = []
            func flush() { if !para.isEmpty { out.append(String(String.UnicodeScalarView(para))) }; para = [] }
            for ln in lines {
                guard let first = ln.first, let lastCh = ln.last else { flush(); continue }
                if para.isEmpty {
                    para = ln
                } else if para.count >= 2, para[para.count - 1] == "-", Character(para[para.count - 2]).isLetter, Character(first).isLowercase {
                    para.removeLast()
                    para += ln
                } else if cjk(para[para.count - 1]), cjk(first) {
                    para += ln
                } else {
                    para.append(" ")
                    para += ln
                }
                if endPunct.contains(lastCh), Double(ln.count) < 0.8 * Double(width) { flush() }
            }
            flush()
        }
        return out.joined(separator: "\n\n")
    }

    // MARK: PDF 取字后的清理（电脑 papers.clean_pages、clip_compose.before_references，同一套规则，只用在 PDF 这条路）

    static let headerMinPages = 3        // 电脑 papers.HEADER_MIN_PAGES：少于这么多页不做页眉页脚去重
    static let headerMinCount = 3        // 电脑 papers.HEADER_MIN_COUNT
    static let headerZone = 2            // 电脑 papers.HEADER_ZONE：页首页尾各看前、后这么多个非空行
    static let headerMaxChars = 120      // 电脑 papers.HEADER_MAX_CHARS
    static let referencesMinShare = 5    // 电脑 clip_compose.REFERENCES_MIN_SHARE
    static let referencesTitle = #"(?:(?:[0-9]{1,2}|[ivx]{1,5})[.)]?\s+)?(?:references|bibliography|参考文献)"#   // 电脑 clip_compose.REFERENCES_TITLE
    private static let pageNoPattern = #"^(?:[-–—]\s*[0-9]{1,4}\s*[-–—]|[0-9]{1,4}|[0-9]{1,4}\s*/\s*[0-9]{1,4}|page\s*[0-9]{1,4}(?:\s*of\s*[0-9]{1,4})?)$"#

    private static func matches(_ s: String, _ pattern: String) -> Bool { s.range(of: pattern, options: .regularExpression) != nil }

    /// 比较页眉页脚用：空白合并、转小写、连续数字换成一个 0（电脑 papers._line_key）
    private static func lineKey(_ s: String) -> String {
        squash(s).lowercased().replacingOccurrences(of: "[0-9]+", with: "0", options: .regularExpression)
    }

    /// PDF 每页的字，去掉页眉页脚和页码，参考文献那一行变成「## 」标题行（前后各空一行）（电脑 papers.clean_pages）：
    /// 1. 页首页尾（各前 2、后 2 个非空行）里，归一后的同一行出现在一半以上的页、且至少 3 页，这些行去掉；少于 3 页的不做这一条；
    /// 2. 页首页尾里单独成行的页码去掉；3. 单独一行的 References / Bibliography / 参考文献变成「## 」标题行。行尾断词接上由 paragraphs 做
    static func cleanPages(_ pages: [String]) -> [String] {
        let split = pages.map { $0.components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } }
        let zones: [Set<Int>] = split.map { lines in
            let idx = lines.indices.filter { !lines[$0].isEmpty }
            return Set(idx.prefix(headerZone) + idx.suffix(headerZone))
        }
        var repeated = Set<String>()
        if pages.count >= headerMinPages {
            var count: [String: Int] = [:]
            for (lines, zone) in zip(split, zones) {
                for key in Set(zone.filter { lines[$0].unicodeScalars.count <= headerMaxChars }.map { lineKey(lines[$0]) }) { count[key, default: 0] += 1 }
            }
            let need = max(headerMinCount, (pages.count + 1) / 2)
            repeated = Set(count.filter { $0.value >= need }.keys)
        }
        return zip(split, zones).map { lines, zone in
            var kept: [String] = []
            for (i, ln) in lines.enumerated() {
                if zone.contains(i), !ln.isEmpty,
                   (ln.unicodeScalars.count <= headerMaxChars && repeated.contains(lineKey(ln))) || matches(ln.lowercased(), pageNoPattern) { continue }
                if !ln.isEmpty, matches(ln.lowercased(), "^" + referencesTitle + "$") {
                    kept += ["", "## " + ln, ""]
                } else {
                    kept.append(ln)
                }
            }
            return kept.joined(separator: "\n")
        }
    }

    /// PDF 取出来的每页字 → 正文：先清理，再合成段落（电脑 papers.pdf_pages_markdown）
    static func pdfMarkdown(_ pages: [String]) -> String { paragraphs(cleanPages(pages)) }

    /// 摘要只看参考文献标题（「## References」这类单独一行）之前的正文；标题前面的字（空白不算）不到全文 1/5 的不算，找下一个；
    /// 没有合适的标题就原样返回（电脑 clip_compose.before_references）
    static func beforeReferences(_ text: String) -> String {
        let total = text.unicodeScalars.count
        var pos = 0
        for line in text.components(separatedBy: "\n") {
            if matches(line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(), "^## " + referencesTitle + "$") {
                var head = String.UnicodeScalarView(text.unicodeScalars.prefix(pos))
                while let l = head.last, CharacterSet.whitespacesAndNewlines.contains(l) { head.removeLast() }
                if !head.isEmpty, head.count * referencesMinShare >= total { return String(head) }
            }
            pos += line.unicodeScalars.count + 1
        }
        return text
    }

    // MARK: arXiv 戳记（电脑 papers.STAMP_RE、papers.stamp_id）

    /// arXiv 的 PDF 第一页左边的戳记，取字后形如「arXiv:2506.09108v1  [cs.LG]  10 Jun 2025」（空白数量不定；旧编号 arXiv:hep-th/9901001v2）。
    /// 只在第一页的字里找，只认「编号 [分类] 日期」这个整体（正文里引用别人的 arXiv 编号不算）
    private static let stampCat = #"\[[A-Za-z][A-Za-z.\-]*\]"#
    private static let stampDate = #"\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}"#
    /// 新编号一定带 [分类]；旧编号的戳记常常没有 [分类]，分类可有可无（电脑 papers.STAMP_RE）
    private static let stampPattern = #"arXiv\s*:\s*(?:(\d{4}\.\d{4,5})(?:v\d+)?\s*"# + stampCat
        + #"|([a-z][a-z-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\s*"# + stampCat + #")?)\s*"# + stampDate

    /// PDF 第一页的 arXiv 戳记里的编号（不带版本号）；没有返回 nil
    static func stampID(_ pages: [String]) -> String? {
        guard let first = pages.first, let re = try? NSRegularExpression(pattern: stampPattern),
              let m = re.firstMatch(in: first, range: NSRange(location: 0, length: (first as NSString).length)) else { return nil }
        for i in 1...2 where m.range(at: i).location != NSNotFound { return (first as NSString).substring(with: m.range(at: i)) }
        return nil
    }

    // MARK: 模型顺带取的发表日期和作者（电脑 clip_compose.parse_published、parse_authors）

    /// 模型写的发表日期 → YYYY-MM-DD 或空串。只收 YYYY-MM-DD、YYYY-MM、YYYY，年份在 1900 到今年加 1 之间；
    /// YYYY-MM 补成当月 1 日，YYYY 补成 1 月 1 日。「无」和别的写法都当没有
    static func parsePublished(_ value: String) -> String {
        let v = value.trimmingCharacters(in: CharacterSet(charactersIn: " *`\"'"))
        guard let re = try? NSRegularExpression(pattern: #"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?$"#),
              let m = re.firstMatch(in: v, range: NSRange(location: 0, length: (v as NSString).length)) else { return "" }
        func part(_ i: Int) -> Int? { m.range(at: i).location == NSNotFound ? nil : Int((v as NSString).substring(with: m.range(at: i))) }
        let year = part(1) ?? 0, month = part(2) ?? 1, day = part(3) ?? 1
        guard (1900...(Calendar.current.component(.year, from: Date()) + 1)).contains(year) else { return "" }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        guard let d = cal.date(from: DateComponents(year: year, month: month, day: day)),
              cal.dateComponents([.year, .month, .day], from: d) == DateComponents(year: year, month: month, day: day) else { return "" }
        return String(format: "%04d-%02d-%02d", year, month, day)
    }

    static let noAuthors: Set<String> = ["无", "未知", "未找到", "没有", "n/a", "na", "none", "unknown"]   // 电脑 clip_compose.NO_AUTHORS
    static let maxAuthorName = 80                                                                        // 电脑 clip_compose.MAX_AUTHOR_NAME

    /// 模型写的作者 → 姓名列表：按分号（英文、中文都认）拆，去首尾空白，去掉空项；「无」「未知」「N/A」这类是空；
    /// 有哪一项超过 maxAuthorName 字（模型写了一句话，不是姓名）整行不要。最多 maxAuthors 位
    static func parseAuthors(_ value: String) -> [String] {
        let v = value.trimmingCharacters(in: CharacterSet(charactersIn: " *`\"'"))
        if noAuthors.contains(v.lowercased()) { return [] }
        let names = v.replacingOccurrences(of: "；", with: ";").split(separator: ";", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        if names.contains(where: { $0.unicodeScalars.count > maxAuthorName }) { return [] }
        return Array(names.prefix(maxAuthors))
    }

    // MARK: 文件头

    /// 文件头里的字符串：JSON 写法，斜杠不转义（同 Clips.yaml、电脑 clip_compose.yaml）
    static func yaml(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: s, options: [.fragmentsAllowed, .withoutEscapingSlashes])) ?? Data("\"\"".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    /// 论文文件头比剪藏多的几行，没有值的不写（电脑 clip_compose.paper_head_lines）
    static func headLines(authors: [String], arxiv: String, pdf: String) -> [String] {
        var out: [String] = []
        if !authors.isEmpty { out.append("authors: [" + authors.prefix(maxAuthors).map(yaml).joined(separator: ", ") + "]") }
        if !arxiv.isEmpty { out.append("arxiv: \(yaml(arxiv))") }
        if !pdf.isEmpty { out.append("pdf: \(yaml(pdf))") }
        return out
    }

    /// 「A、B」；超过 5 位：「A、B、C、D、E 等 N 人」（电脑 clip_compose.authors_line）
    static func authorsLine(_ authors: [String]) -> String {
        authors.count <= authorsShown ? authors.joined(separator: "、")
            : authors.prefix(authorsShown).joined(separator: "、") + " 等 \(authors.count) 人"
    }
}
