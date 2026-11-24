import Foundation

/// 一行 Markdown 交给系统排版（AttributedString(markdown:)）之前的那几步改写：引号、划线、<br>、双链、链接、批注号、加粗。
/// Markdown.inline 和划线定位（MarkAnchor）用的是这同一份代码：这里每一步都带着「这个字是原文第几个字」一起改，
/// Markdown.inline 只取改完的字，MarkAnchor 还要用每个字的来处，把页面上选中的字换回原文的位置。
/// 只用 Foundation，tests/test_mark_run.py 编出来核对。
enum InlineSource {
    /// 来处：>= 0 是原文（这一行）的 UTF-16 下标；visible 是改写时加上、排出来看得见的字（批注气泡、<br> 换成的换行）；
    /// hidden 是改写时加上、交给系统排版时一定会被吃掉的字（链接的方括号和地址、转义的反斜杠、占位符）
    static let visible = -1
    static let hidden = -2

    struct Traced: Equatable {
        var u: [UInt16]
        var o: [Int]
        init(_ s: String) { u = Array(s.utf16); o = Array(0..<u.count) }
        init(u: [UInt16], o: [Int]) { self.u = u; self.o = o }
        var string: String { String(utf16CodeUnits: u, count: u.count) }
        var ns: NSString { string as NSString }

        func slice(_ r: NSRange) -> Traced { Traced(u: Array(u[r.location..<NSMaxRange(r)]), o: Array(o[r.location..<NSMaxRange(r)])) }
        static func synth(_ s: String, _ tag: Int) -> Traced { let u = Array(s.utf16); return Traced(u: u, o: Array(repeating: tag, count: u.count)) }
        static func + (a: Traced, b: Traced) -> Traced { Traced(u: a.u + b.u, o: a.o + b.o) }
    }

    /// 把每处匹配换成 f 给的几段
    static func replace(_ t: Traced, _ re: NSRegularExpression, _ f: (NSTextCheckingResult, Traced) -> Traced) -> Traced {
        let ms = re.matches(in: t.string, range: NSRange(location: 0, length: t.u.count))
        guard !ms.isEmpty else { return t }
        var out = Traced(u: [], o: []), at = 0
        for m in ms {
            out = out + t.slice(NSRange(location: at, length: m.range.location - at)) + f(m, t)
            at = NSMaxRange(m.range)
        }
        return out + t.slice(NSRange(location: at, length: t.u.count - at))
    }

    private static func re(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p) }
    static let markRE = re(MarkRule.pattern)
    static let brRE = re(#"<br\s*/?>"#)
    static let wikiRE = re(#"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]"#)
    static let linkRE = re(#"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:<([^>]*)>|([^)]*))\)"#)
    static let noteRE = re(#"\[\^note-(\d+)\]"#)
    static let boldRE = re(#"\*\*([^*\n]+?)\*\*"#)
    static let schemeRE = re(#"^(https?|mailto):"#)

    /// 改写，顺序同原来的 Markdown.inline：
    /// 1. 引号（curlyQuotes、latinQuotes，一个字换一个字）；2. ==划线== 换成占位符，剩下的「==」去掉；3. <br> 换成换行；
    /// 4. [[笔记|显示]] 只留显示的字；5. 链接：网址的留成 Markdown 链接（文字里的方括号转义），别的只留文字；
    /// 6. [^note-3] 换成 [✎](spark-note:3)；7. **加粗** 换成占位符
    static func prepare(_ src: Traced) -> Traced {
        var t = src
        // 1
        let q = latinQuotes(curlyQuotes(t.string))
        if q.utf16.count == t.u.count { t.u = Array(q.utf16) }
        // 2
        t = replace(t, markRE) { m, t in .synth("\u{E002}", hidden) + t.slice(m.range(at: 1)) + .synth("\u{E003}", hidden) }
        var u2: [UInt16] = [], o2: [Int] = [], i = 0
        while i < t.u.count {
            if i + 1 < t.u.count, t.u[i] == 0x3D, t.u[i + 1] == 0x3D { i += 2; continue }
            u2.append(t.u[i]); o2.append(t.o[i]); i += 1
        }
        t = Traced(u: u2, o: o2)
        // 3
        t = replace(t, brRE) { _, _ in .synth("\n", visible) }
        // 4
        t = replace(t, wikiRE) { m, t in t.slice(m.range(at: 1)) }
        // 5
        if t.string.contains("](") {
            t = replace(t, linkRE) { m, t in
                let text = t.slice(m.range(at: 1))
                let dr = m.range(at: 2).location != NSNotFound ? m.range(at: 2) : m.range(at: 3)
                let dest = t.ns.substring(with: dr)
                guard schemeRE.firstMatch(in: dest, range: NSRange(location: 0, length: (dest as NSString).length)) != nil else { return text }
                var esc = Traced(u: [], o: [])
                for k in 0..<text.u.count {
                    if text.u[k] == 0x5B || text.u[k] == 0x5D { esc = esc + .synth("\\", hidden) }
                    esc = esc + text.slice(NSRange(location: k, length: 1))
                }
                return .synth("[", hidden) + esc + .synth("](" + dest.replacingOccurrences(of: " ", with: "%20") + ")", hidden)
            }
        }
        // 6
        t = replace(t, noteRE) { m, t in .synth("[", hidden) + .synth(String(noteGlyph), visible) + .synth("](\(noteScheme):" + t.ns.substring(with: m.range(at: 1)) + ")", hidden) }
        // 7
        t = replace(t, boldRE) { m, t in .synth("\u{E000}", hidden) + t.slice(m.range(at: 1)) + .synth("\u{E001}", hidden) }
        return t
    }

    static let noteGlyph: Character = "✎"
    static let noteScheme = "spark-note"

    /// 剪藏显示前按 ClipTidy 改过的（只看一行之内的那几样）：零宽字符去掉、「****」去掉
    static func tidied(_ t: Traced) -> Traced {
        let invisible: Set<UInt16> = [0x2060, 0x200B, 0x200C, 0x200D, 0xFEFF]
        var out = Traced(u: [], o: [])
        for k in 0..<t.u.count where !invisible.contains(t.u[k]) { out.u.append(t.u[k]); out.o.append(t.o[k]) }
        var u2: [UInt16] = [], o2: [Int] = [], i = 0
        while i < out.u.count {
            if i + 3 < out.u.count, out.u[i...(i + 3)].allSatisfy({ $0 == 0x2A }) { i += 4; continue }
            u2.append(out.u[i]); o2.append(out.o[i]); i += 1
        }
        return Traced(u: u2, o: o2)
    }

    /// 中文句子里的直引号 "…" 换成弯引号“…”（模型写小结时常用英文引号，夹在宋体、楷体里很扎眼）
    static func curlyQuotes(_ s: String) -> String {
        guard s.contains("\""), s.range(of: #"\p{Han}"#, options: .regularExpression) != nil,
              !s.contains("](") else { return s }
        var out = "", open = true
        for ch in s {
            if ch == "\"" { out.append(open ? "“" : "”"); open.toggle() } else { out.append(ch) }
        }
        return open ? out : s   // 引号不成对就不动
    }

    /// 英文里的弯引号换成直引号。霞鹜文楷、思源黑体这类中文字体把 ’ ‘ “ ” 画成全角，
    /// 「Hip Hop’s」显示成「Hip Hop’ s」；直引号在中文字体里是半角的。
    /// 单词中间的撇号（Hop’s、don’t）一律换；整段没有汉字（英文原文、英文标题）的引号全换；
    /// 中文句子里的“…”保持全角。
    static func latinQuotes(_ s: String) -> String {
        guard s.contains(where: { "‘’“”".contains($0) }) else { return s }
        var t = s.replacingOccurrences(of: #"(?<=[A-Za-z0-9])[’‘](?=[A-Za-z])"#, with: "'", options: .regularExpression)
        if t.range(of: #"\p{Han}"#, options: .regularExpression) == nil {
            t = t.replacingOccurrences(of: #"[“”]"#, with: "\"", options: .regularExpression)
                .replacingOccurrences(of: #"[‘’]"#, with: "'", options: .regularExpression)
        }
        return t
    }
}
