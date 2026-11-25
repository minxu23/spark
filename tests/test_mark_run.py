"""手机阅读页划线、批注、取消划线的定位链（apps/mobile-ios/Spark/MarkRun.swift、MarkAnchor.swift）。

用 swiftc 把 MarkRun.swift、MarkAnchor.swift、MarkRule.swift、ReaderPlace.swift（及它依赖的 PathRules、ReaderFeatures），
加上从工作区源码原样摘出来的 Markdown.parse / stripPreamble / filtered / inline（真的 inline，含链接、引号、批注号处理）、
MarkdownView.simpleQuote、ReaderShell.isLeadBlock 编成一个探针。探针照 ReaderShell.apply + MarkdownView + MarkableText 的路子：
按语言模式和信息文章的节排出页面 → 并段 → 每项的行键和整页序号 → 用 MarkRun.layoutText 拼出文本框里的字 →
按「从哪几个字选到哪几个字」模拟选区 → MarkRun.selections / segments → MarkAnchor.apply，断言文件的最终内容。

MarkableText.attributed 里设置字体、颜色这些属性的部分没法在 macOS 上单独编（UIKit），它拼出来的字和范围在 DEBUG
构建里用 assert 跟 MarkRun.layoutText 核对（DocEdits.swift）。"""

import json
import os
import random
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Spark")
SHARED = os.path.join(os.path.dirname(__file__), "..", "apps", "mobile-ios", "Shared")
FILES = [os.path.join(ROOT, n) for n in ("MarkRule.swift", "MarkRun.swift", "MarkAnchor.swift", "InlineSource.swift", "PathRules.swift",
                                          "ReaderFeatures.swift", "ReaderPlace.swift")]


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _chunk(src: str, start: str) -> str:
    i = src.index(start)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise ValueError(start)


def _harness():
    md = _read("Markdown.swift")
    mdblock = _chunk(md, "enum MDBlock: Hashable {")
    markdown = "\n".join(_chunk(md, s) for s in (
        "static func parse(", "static func captions(", "static func subheading(", "static func unmarked(", "static func cells(",
        "static func stripPreamble(", "static func curlyQuotes(", "static func latinQuotes(",
        "static func inline(", "static func displayed(",
        "enum SpeechLang: String", "private static func pairs(", "private static func hasHan(",
        "static func isBilingual(", "static func filter(", "static func filtered("))
    simple = _chunk(md, "static func simpleQuote(")
    shell = _read("ReaderShell.swift")
    lead = _chunk(shell, "static func isLeadBlock(") + "\n" + _chunk(shell, "static func summaryBody(")
    clipper = open(os.path.join(SHARED, "Clipper.swift"), encoding="utf-8").read()
    tidy = "enum HTMLToMarkdown {\n" + _chunk(clipper, "static func decodeEntities(") + "\n}\n" + _chunk(clipper, "enum ClipTidy {")
    return (HARNESS.replace("%MDBLOCK%", mdblock).replace("%MARKDOWN%", markdown).replace("%SIMPLE%", simple)
            .replace("%LEAD%", lead).replace("%TIDY%", tidy).replace("%OLDINLINE%", OLD_INLINE))


# 改之前（提交 ec60d57）Markdown.inline 交给系统排版前的那几步，原样抄来：InlineSource.prepare 必须和它一字不差
OLD_INLINE = r'''
enum OldInline {
    static let noteScheme = "spark-note"
static func pre(_ s: String) -> String {
        // ==划的重点==：先换成占位符，最后铺底色
        var t = latinQuotes(curlyQuotes(s)).replacingOccurrences(of: MarkRule.pattern, with: "\u{E002}$1\u{E003}", options: .regularExpression)
            .replacingOccurrences(of: "==", with: "")
            .replacingOccurrences(of: #"<br\s*/?>"#, with: "\n", options: .regularExpression)
        // Obsidian 的 [[链接|显示]] 只留显示的字
        t = t.replacingOccurrences(of: #"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]"#, with: "$1", options: .regularExpression)
        t = links(t)
        // 批注号「[^note-3]」：排成能点的 ✎，点了弹出批注
        t = t.replacingOccurrences(of: #"\[\^note-(\d+)\]"#, with: "[\("✎")](\(noteScheme):$1)", options: .regularExpression)
        // 「**拉姆：**你错了」这种：收尾的 ** 前面是标点、后面紧跟汉字，标准 Markdown 不认作加粗，先自己标出来
        t = t.replacingOccurrences(of: #"\*\*([^*\n]+?)\*\*"#, with: "\u{E000}$1\u{E001}", options: .regularExpression)
        return t
    }
static func links(_ s: String) -> String {
        guard s.contains("]("), let re = try? NSRegularExpression(pattern: #"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\((?:<([^>]*)>|([^)]*))\)"#) else { return s }
        let ns = s as NSString, out = NSMutableString(string: s)
        for m in re.matches(in: s, range: NSRange(location: 0, length: ns.length)).reversed() {
            let text = ns.substring(with: m.range(at: 1))
            let dest = ns.substring(with: m.range(at: 2).location != NSNotFound ? m.range(at: 2) : m.range(at: 3))
            if dest.range(of: #"^(https?|mailto):"#, options: .regularExpression) != nil {
                let escaped = text.replacingOccurrences(of: "[", with: "\\[").replacingOccurrences(of: "]", with: "\\]")
                out.replaceCharacters(in: m.range, with: "[\(escaped)](\(dest.replacingOccurrences(of: " ", with: "%20")))")
            } else {
                out.replaceCharacters(in: m.range, with: text)
            }
        }
        return out as String
    }
static func curlyQuotes(_ s: String) -> String {
        guard s.contains("\""), s.range(of: #"\p{Han}"#, options: .regularExpression) != nil,
              !s.contains("](") else { return s }
        var out = "", open = true
        for ch in s {
            if ch == "\"" { out.append(open ? "“" : "”"); open.toggle() } else { out.append(ch) }
        }
        return open ? out : s   // 引号不成对就不动
    }
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
'''


HARNESS = r'''
import Foundation
import SwiftUI
%MDBLOCK%
enum RX { static func re(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p) } }
enum DocEdit { struct Note { static let glyph: Character = "✎" } }
enum Markdown {
%MARKDOWN%
    static let noteScheme = "spark-note"
}
enum MDView {
%SIMPLE%
}
enum Shell {
%LEAD%
}
%TIDY%
%OLDINLINE%
let thin = "\u{200A}"

/// 照 ReaderShell.apply 排出一页：去掉读后感，剪藏先 ClipTidy，解析，信息文章按节截取（一句话总结画成卡片，正文里那块不画），
/// 单集小结去掉「我的高亮」，按语言过滤
func page(_ text: String, mode: String, section: String?, kind: String = "doc") -> (blocks: [MDBlock], translated: Set<Int>, listed: Set<Int>, scope: MarkAnchor.Scope) {
    var body = text
    if let r = MarkAnchor.reflectionRange(body) { body.removeSubrange(r) }
    let tidy = kind == "clip"
    var b = Markdown.stripPreamble(Markdown.parse(tidy ? ClipTidy.clean(body) : body))
    var scope = MarkAnchor.Scope(section: nil, tidy: tidy, skipHighlights: kind == "summary")
    if let section {
        let sec: ArticleSection = section == "小结" ? .summary : .original
        let heads = b.enumerated().compactMap { i, x -> (index: Int, text: String)? in
            if case .heading(2, let h) = x { return (i, h.trimmingCharacters(in: .whitespaces)) }
            return nil
        }
        let v = ReaderPlace.sectionView(heads, count: b.count, section: sec)
        var shownBlocks = v.range.map { Array(b[$0]) } ?? []
        if v.split, sec == .summary, ReaderPlace.sectionLead(text) != nil, let f = shownBlocks.first, Shell.isLeadBlock(f) { shownBlocks.removeFirst() }
        b = shownBlocks
        if v.split { scope.section = section }
    } else if kind == "summary" {
        b = Shell.summaryBody(b)
    }
    if let lang = Markdown.SpeechLang(rawValue: mode) {
        let f = Markdown.filtered(b, lang)
        return (f.blocks, f.translated, f.listed, scope)
    }
    return (b, [], [], scope)
}

struct Run { let units: [MarkRun.Unit]; let text: String; let ranges: [NSRange]; let excluded: [NSRange] }

/// 照 MarkdownView.runText + MarkableText：每个文本框里的项和拼出来的字
func runs(_ blocks: [MDBlock], _ translated: Set<Int>, _ listed: Set<Int>) -> (runs: [Run], ord: [MarkRun.Unit.ID: (ordinal: Int, total: Int)]) {
    let rows = MarkRun.rows(blocks, grouped: true, simpleQuote: MDView.simpleQuote)
    let ord = MarkRun.ordinals(blocks, rows: rows, translated: translated, listed: listed, simpleQuote: MDView.simpleQuote)
    var out: [Run] = []
    for r in rows where MarkRun.isRun(r, blocks, grouped: true, simpleQuote: MDView.simpleQuote) {
        let units = MarkRun.units(blocks, r, translated: translated, listed: listed)
        let lay = MarkRun.layoutText(units.map { u in
            var marker: String? = nil
            if case .bullet(let m, _, false) = u.style { marker = m }
            let a = Markdown.inline(u.text)
            return (marker: marker, runs: a.runs.map { (text: String(a[$0.range].characters), note: $0.link?.scheme == Markdown.noteScheme) })
        }, thin: thin)
        out.append(Run(units: units, text: lay.text, ranges: lay.ranges, excluded: lay.excluded))
    }
    return (out, ord)
}

func nth(_ ns: NSString, _ needle: String, _ n: Int, from: Int = 0) -> NSRange {
    var at = from, r = NSRange(location: NSNotFound, length: 0)
    for _ in 0..<n {
        r = ns.range(of: needle, range: NSRange(location: at, length: ns.length - at))
        if r.location == NSNotFound { return r }
        at = r.location + 1
    }
    return r
}

/// 照 MarkdownView.runText：选中的每一项换成 Target（MarkRun.target），交给 MarkAnchor.apply（照 DocNotes / saveNote）
func perform(_ text: String, _ run: Run, _ ord: [MarkRun.Unit.ID: (ordinal: Int, total: Int)], _ sel: NSRange, _ op: String, _ note: String,
             _ scope: MarkAnchor.Scope) -> [String: Any] {
    let ns = run.text as NSString
    var targets: [MarkAnchor.Target] = []
    var blocks: [Int] = []
    for s in MarkRun.selections(run.text, units: run.ranges, selection: sel, drop: thin) {
        let display = ns.substring(with: run.ranges[s.unit])
        targets.append(MarkRun.target(run.units[s.unit], ord, display: display, selection: s.range, render: Markdown.displayed))
        blocks.append(run.units[s.unit].block)
    }
    let o: MarkAnchor.Op = op == "mark" ? .mark : op == "unmark" ? .unmark : .annotate(note)
    let r = MarkAnchor.apply(text, o, targets, scope: scope, render: Markdown.displayed)
    return ["text": r.text ?? text, "changed": r.text != nil, "done": r.done, "blocks": blocks,
            "failures": r.failures.map { "\($0)" }]
}

let input = try! JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
var out: [String: Any] = [:]

// 页面保留旧的重复次数、文件已增删同文项：不能猜位置，也不能写入。
out["countMismatch"] = [1, 3].map { (expectedCount: Int) -> [String: Any] in
    let text = "# T\n\n- 重复项\n- 重复项\n"
    let lines = MarkAnchor.scan(text, scope: .init())
    let target = MarkAnchor.Target(keys: [lines[0].key], ordinal: 0, total: expectedCount,
        parts: [.init(line: 0, display: "重复项", range: NSRange(location: 0, length: 3))])
    let result = MarkAnchor.apply(text, .mark, [target], scope: .init(), render: Markdown.displayed)
    return ["unchanged": result.text == nil, "done": result.done,
            "failures": result.failures.map { "\($0)" }]
}

out["edits"] = (input["edits"] as! [[String: Any]]).map { c -> Any in
    let text = c["text"] as! String
    let p = page(text, mode: c["mode"] as! String, section: c["section"] as? String, kind: c["kind"] as? String ?? "doc")
    let (rs, ord) = runs(p.blocks, p.translated, p.listed)
    let sn = c["startN"] as! Int, en = c["endN"] as! Int
    var seen = 0
    for run in rs {
        let ns = run.text as NSString
        var cnt = 0, at = 0
        while true {
            let x = ns.range(of: c["start"] as! String, range: NSRange(location: at, length: ns.length - at))
            if x.location == NSNotFound { break }
            cnt += 1; at = x.location + 1
        }
        if seen + cnt < sn { seen += cnt; continue }
        let a = nth(ns, c["start"] as! String, sn - seen)
        let b = nth(ns, c["end"] as! String, en, from: a.location)
        guard b.location != NSNotFound else { return ["err": "end not found", "box": run.text] }
        return perform(text, run, ord, NSRange(location: a.location, length: NSMaxRange(b) - a.location), c["op"] as! String,
                       c["note"] as! String, p.scope)
    }
    return ["err": "start not found"]
}

/// 随机：整篇一个文本框，选区按比例给。划完重排一遍核对：每项显示的字没变；选中的字都有底色；
/// 新多出来的底色所在的那一截连着的底色都碰到了选区（不会落到别处）；链接地址、批注号、双链目标里没有「==」。再用同一个选区取消
func check(_ before: Run, _ after: Run, _ sel: NSRange, partial: Bool) -> String? {
    guard before.units.count == after.units.count else { return "项数变了" }
    let skip: Set<UInt16> = MarkRule.trimChars.union([0x200A, 0x270E])
    for i in before.units.indices {
        let a = Markdown.displayed(before.units[i].text), b = Markdown.displayed(after.units[i].text)
        let ta = a.text.replacingOccurrences(of: thin, with: ""), tb = b.text.replacingOccurrences(of: thin, with: "")
        guard ta == tb else { return "第 \(i) 项显示的字变了：\(ta) → \(tb)" }
        // 去掉细空格后的位置
        func strip(_ r: MarkAnchor.Rendered) -> (u: [UInt16], m: [Bool], pos: [Int]) {
            var u: [UInt16] = [], m: [Bool] = [], pos: [Int] = []
            for (k, c) in r.text.utf16.enumerated() where c != 0x200A { u.append(c); m.append(r.marked[k]); pos.append(k) }
            return (u, m, pos)
        }
        let sa = strip(a), sb = strip(b)
        let ur = before.ranges[i]
        let local = NSIntersectionRange(ur, sel)
        var selected = Set<Int>()
        if local.length > 0, local.location >= ur.location {
            for (x, k) in sa.pos.enumerated() where k >= local.location - ur.location && k < NSMaxRange(local) - ur.location && !skip.contains(sa.u[x]) {
                if sa.u[x] != 0x0A { selected.insert(x) }
            }
        }
        // 有几项没划成（MarkAnchor 报了原因）：没变的那几项不查
        if partial, sa.m == sb.m, before.units[i].text == after.units[i].text { continue }
        // 选中的字（同一段 <br> 换行里的空白、等号除外）都有底色
        for x in selected where !sb.m[x] { return "第 \(i) 项选中的字「\(String(utf16CodeUnits: [sa.u[x]], count: 1))」没划上" }
        // 新多出来的底色：所在的连着的一截要碰到选区
        var x = 0
        while x < sb.m.count {
            guard sb.m[x] else { x += 1; continue }
            var y = x
            while y < sb.m.count, sb.m[y] { y += 1 }
            let fresh = (x..<y).contains { !sa.m[$0] }
            if fresh, !(x..<y).contains(where: { selected.contains($0) }) { return "第 \(i) 项多出一截底色" }
            x = y
        }
    }
    return nil
}

/// 链接地址、批注号、双链目标、自动链接里有没有「==」（逐个记号看，不跨记号）
let tokenREs = [#"\]\((?:<[^>\n]*>|[^)\s]*)\)"#, #"\[\^note-[^\]\n]*\]"#, #"\[\[[^\]|\n]*\|"#, #"<https?://[^>\s]*>"#].map { try! NSRegularExpression(pattern: $0) }
func markInToken(_ s: String) -> Bool {
    let ns = s as NSString
    return tokenREs.contains { re in re.matches(in: s, range: NSRange(location: 0, length: ns.length)).contains { ns.substring(with: $0.range).contains("==") } }
}

out["random"] = (input["random"] as! [[String: Any]]).map { c -> Any in
    let text = c["text"] as! String
    let p = page(text, mode: "none", section: nil)
    let (rs, ord) = runs(p.blocks, p.translated, p.listed)
    guard let run = rs.first else { return ["err": "no run"] }
    let n = (run.text as NSString).length
    let fr = c["sel"] as! [Double]
    var a = min(n - 1, Int(fr[0] * Double(n))), b = max(a + 1, min(n, Int(fr[1] * Double(n))))
    let rns = run.text as NSString
    a = rns.rangeOfComposedCharacterSequence(at: a).location
    if b < n { b = rns.rangeOfComposedCharacterSequence(at: b).location }
    let sel = NSRange(location: a, length: max(1, b - a))
    var m = perform(text, run, ord, sel, c["op"] as! String, "随机批注", p.scope)
    guard m["changed"] as! Bool else { return m }
    let t2 = m["text"] as! String
    let p2 = page(t2, mode: "none", section: nil)
    let (rs2, ord2) = runs(p2.blocks, p2.translated, p2.listed)
    guard let run2 = rs2.first else { m["bad"] = "改完没有文本框了"; return m }
    if let why = check(run, run2, sel, partial: !(m["failures"] as! [String]).isEmpty) { m["bad"] = why }
    if markInToken(t2) && !markInToken(text) { m["bad"] = "记号里出现了 ==" }
    // 用同一个选区取消
    if c["op"] as! String == "mark" { m["undo"] = perform(t2, run2, ord2, sel, "unmark", "", p2.scope)["text"] }
    return m
}

out["selections"] = (input["selections"] as! [[String: Any]]).map { c -> [[Int]] in
    let r = { (a: [Int]) in NSRange(location: a[0], length: a[1]) }
    return MarkRun.selections(c["text"] as! String, units: (c["units"] as! [[Int]]).map(r), selection: r(c["sel"] as! [Int]))
        .map { [$0.unit, $0.range.location, $0.range.length] }
}
/// InlineSource.prepare 和改之前的 Markdown.inline 改写一字不差；对照表对得上；displayed 去掉细空格就是 inline 的纯文字
out["inline"] = (input["inline"] as! [String]).map { line -> [Any] in
    let t = InlineSource.prepare(.init(line))
    let shown = Markdown.displayed(line)
    let plain = String(Markdown.inline(line).characters)
    return [t.string == OldInline.pre(line), MarkAnchor.align(t, shown.text) != nil, shown.text.replacingOccurrences(of: thin, with: "") == plain]
}
/// 一行里一长串下划线、星号整行选中：要很快返回（不能有指数级回溯的正则）
out["perf"] = (input["perf"] as! [String]).map { line -> Double in
    let text = "# T\n\n## 正文\n\n" + line + "\n"
    let t0 = Date()
    let p = page(text, mode: "none", section: nil)
    let (rs, ord) = runs(p.blocks, p.translated, p.listed)
    if let run = rs.first { _ = perform(text, run, ord, NSRange(location: 0, length: (run.text as NSString).length), "mark", "", p.scope) }
    return Date().timeIntervalSince(t0)
}
out["lineParts"] = (input["lineParts"] as! [[String: Any]]).map { c -> Any in
    guard let ps = MarkRun.lineParts(c["unit"] as! String, lines: c["lines"] as! [String],
                                     selection: NSRange(location: (c["sel"] as! [Int])[0], length: (c["sel"] as! [Int])[1])) else { return NSNull() }
    return ps.map { [$0.line, $0.range.location, $0.range.length] }
}
out["layout"] = (input["layout"] as! [[String]]).map { units -> [Any] in
    let lay = MarkRun.layoutText(units.map { t in
        let a = Markdown.inline(t)
        return (marker: t.hasPrefix("·") ? "•" : nil, runs: a.runs.map { (text: String(a[$0.range].characters), note: $0.link?.scheme == Markdown.noteScheme) })
    }, thin: thin)
    return [lay.text, lay.ranges.map { [$0.location, $0.length] }, lay.excluded.map { [$0.location, $0.length] }]
}
out["rows"] = (input["rows"] as! [[String: Any]]).map { c -> [[Int]] in
    MarkRun.rows(Markdown.parse(c["text"] as! String), grouped: c["grouped"] as! Bool, simpleQuote: MDView.simpleQuote).map { [$0.lowerBound, $0.upperBound] }
}
out["rowsBig"] = (input["rowsBig"] as! [[Int]]).map { c -> [[Int]] in
    let blocks = (0..<c[0]).map { _ in MDBlock.paragraph(String(repeating: "字", count: c[1])) }
    return MarkRun.rows(blocks, grouped: true, simpleQuote: MDView.simpleQuote).map { [$0.lowerBound, $0.upperBound] }
}
out["units"] = (input["units"] as! [[String: Any]]).map { c -> [String] in
    let p = page(c["text"] as! String, mode: c["mode"] as! String, section: nil)
    return MarkRun.units(p.blocks, 0..<p.blocks.count, translated: p.translated, listed: p.listed).map { u in
        u.keys.map { "\($0.quoted ? ">" : "")\($0.listed ? "-" : "")\($0.content)" }.joined(separator: "|") + (u.joinsNext ? " +" : "")
    }
}
out["scan"] = (input["scan"] as! [[String: Any]]).map { c -> [String] in
    MarkAnchor.scan(c["text"] as! String, scope: .init(section: c["section"] as? String, tidy: c["tidy"] as? Bool ?? false)).map {
        "\($0.index):\($0.key.quoted ? ">" : "")\($0.key.listed ? "-" : "")\($0.key.content)"
    }
}
print(String(data: try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
'''


def E(name, text, start, end=None, op="mark", mode="none", section=None, startN=1, endN=1, want=None, fails=None, kind="doc"):
    """want：文件的最终内容；fails：MarkAnchor 报的没成的几处（None 表示应当全成）；kind：clip 按剪藏整理（ClipTidy），summary 去掉「我的高亮」"""
    return dict(name=name, text=text, start=start, end=end or start, op=op, mode=mode, section=section,
                startN=startN, endN=endN, note="一条批注", want=want, fails=fails or [], kind=kind)


def sub(text, *pairs, last=False):
    for a, b in pairs:
        assert a in text, a
        if last:
            i = text.rindex(a)
            text = text[:i] + b + text[i + len(a):]
        else:
            text = text.replace(a, b, 1)
    return text


def noted(text, *pairs, n=1, body="一条批注"):
    return sub(text, *pairs).rstrip("\n") + f"\n\n[^note-{n}]: {body}\n"


H = "# T\n\n## 正文\n\n"

# ---------------------------------------------------------------- 审查的危险用例

DUP_BULLET = "# T\n\n## 正文\n\n- 好的\n- 不行\n- 好的\n"
SUB_BULLET = "---\ntitle: 时钟的历史\n---\n\n# 时钟的历史\n\n## 正文\n\n这一段讲了时钟的历史。\n\n- 时钟\n- 别的\n"
SUB_BULLET_MARKED = "# T\n\n## 正文\n\n这一段讲了==时钟的历史==。\n\n- 时钟\n- 别的\n"
FM_BULLET = "---\ntitle: 时钟\ntags: [时钟]\n---\n\n# 关于钟\n\n- 时钟\n- 别的\n"
HEAD_BULLET = "# T\n\n## 结论\n\n- 结论\n- 别的\n"
SPEECH_DUP = """# 一期

## 演讲稿

He said yes and I agreed with him.

> 他说：是的。我同意他。

Yes.

> 是的。

Third paragraph here is fine.

> 第三段在这里。

Yes.

> 是的。
"""
SPEECH_ML = """# 一期

## 演讲稿

First paragraph about the conclusion.

> 结论很重要，大家都同意。

Second paragraph that is long.

> 第一行译文
> 结论

Third paragraph here.

> 第三段的译文。
"""
SPEECH_ML_MARKED = SPEECH_ML.replace("> 结论很重要，大家都同意。", "> ==结论很重要==，大家都同意。")
ARTICLE = """# 文章

- 来源：博客

## 小结

**一句话总结这篇文章讲了什么，写得比较长所以不会被当成小标题，继续写到四十四个字以上。**

- OpenAI 发布新模型
- 要点二

模型能力提升明显。

## 原文

OpenAI 发布新模型，引起关注。

模型能力提升明显。

- OpenAI 发布新模型
- 另一项
"""
INLINE = H + "这是**重要的**观点，见[文档](https://x.com/a)和`code`，还有[[笔记|别名]]。\n\n- 列表里有**粗体**和[链接](https://y.com)字样\n"
QUOTES = H + '他说"你好"然后走了，中文句子。\n\nHip Hop’s culture is “big” today.\n\n- 她说"再见"就走\n'
NOTE = H + "这是==重点==[^note-1]后面的字，还有别的。\n\n- 项目==划过==[^note-2]尾巴\n\n[^note-1]: 旧批注\n[^note-2]: 旧批注二\n"
BR = H + "- 第一行<br>第二行\n- 另一项\n"
QVAR = H + "前一段。\n\n>引用无空格\n\n后一段。\n"
QEQ = H + "前一段。\n\n> =SUM 公式说明\n\n> ROI=收益 很高\n\n> 甲行\n> =乙行\n"
QTRAIL = H + "> 第一行动计划\n\n中间。\n\n> 第一行<HB>\n> 第二行\n".replace("<HB>", "  ")
LISTS = H + "- 甲乙丙\n- 乙\n\n1. 步骤\n2. 步骤\n"
HARD = H + "结尾一段正文，<HB>\n带一个硬换行。\n\n下一段。\n".replace("<HB>", "  ")
SOFT = H + "English paragraph line one\nline two continues here.\n\n> 译文第一行\n> 译文第二行\n"
SAMEUNIT = H + "今天天气好，明天天气好，后天也好。\n"
NESTED = H + "- 外层\n    - 内层一\n    - 内层二\n        1. 最里层\n- 外层二\n"
TASK = H + "- [ ] 待办事项\n- [x] 已办事项\n"
EQB = H + "- x=收益\n- =收益\n- a=b=c\n"
EQSUB = H + "公式是 ROI=收益 除以成本。\n\n- 收益\n- 成本\n"
ZHLIST = "# 一期\n\n## 演讲稿\n\nIntro paragraph one is here.\n\n> 开场第一段。\n\n- Item one in English\n- Item two in English\n\n> - 第一项译文\n> - 第二项译文\n\n1. Step one text\n\n> 步骤一的译文\n> 步骤一译文第二行\n\nThird paragraph here.\n\n> 第三段的译文。\n\nFourth paragraph here.\n\n> 第四段的译文。\n"
QQ = "---\ntitle: x\n---\n\n# x\n\n## 正文\n\n> English quoted original one.\n\n> 引用的译文一。\n\nPlain para two.\n\n> 译文二。\n\nPlain para three.\n\n> 译文三。\n\nPlain para four.\n\n> 译文四。\n"
CAPTION = H + "![](a.png)\n\n短句子\n\n后面一段。\n\n短句子\n"
REFLECT = H + "正文一段。\n\n[阅读原文](https://example.com/a)\n\n## 读后感\n\n正文一段。\n"
LONGQ = H + "> 多行引用第一行\n> =等号开头的第二行\n"

EDITS = [
    # 一、两项原文相同、互相包含（跨项）
    E("两个一样的有序项：跨两项划", LISTS, "步骤", "步骤", endN=2, want=sub(LISTS, ("1. 步骤", "1. ==步骤=="), ("2. 步骤", "2. ==步骤=="))),
    E("批注跨两项：后一项的字是前一项的一部分", LISTS, "乙丙", "乙", op="annotate", endN=2,
      want=noted(LISTS, ("- 甲乙丙", "- 甲==乙丙=="), ("- 乙\n", "- ==乙==[^note-1]\n"))),
    E("中文：两段一样的短译文和中间一段一起选", SPEECH_DUP, "是的。", "是的。", mode="zh", startN=2, endN=2,
      want=SPEECH_DUP.replace("\n> 是的。\n", "\n> ==是的。==\n").replace("> 第三段在这里。", "> ==第三段在这里。==")),
    # 二、单项落到更早的同样文字上
    E("两个列表项文字相同：选第 2 个", DUP_BULLET, "好的", startN=2, want=sub(DUP_BULLET, ("- 好的\n", "- ==好的==\n"), last=True)),
    E("列表项的字在文件头、标题、前面段落里出现过", SUB_BULLET, "时钟", startN=2, want=sub(SUB_BULLET, ("- 时钟", "- ==时钟=="))),
    E("列表项的字在前面一处已有划线里", SUB_BULLET_MARKED, "时钟", startN=2, want=sub(SUB_BULLET_MARKED, ("- 时钟", "- ==时钟=="))),
    E("列表项的字只在文件头里出现过", FM_BULLET, "时钟", want=sub(FM_BULLET, ("- 时钟", "- ==时钟=="))),
    E("列表项的字跟前面的标题一样", HEAD_BULLET, "结论", want=sub(HEAD_BULLET, ("- 结论", "- ==结论=="))),
    E("列表项的字在前面「ROI=收益」里", EQSUB, "收益", startN=2, want=sub(EQSUB, ("- 收益", "- ==收益=="))),
    E("中文：短译文是前面译文的一部分（选第 2 处）", SPEECH_DUP, "是的。", mode="zh", startN=2,
      want=sub(SPEECH_DUP, ("\n> 是的。\n", "\n> ==是的。==\n"))),
    E("中文：重复的译文选第 3 处（最后一段）", SPEECH_DUP, "是的。", mode="zh", startN=3,
      want=sub(SPEECH_DUP, ("\n> 是的。\n", "\n> ==是的。==\n"), last=True)),
    E("对照：两行一样的译文选第 2 行", SPEECH_DUP, "是的。", mode="both", startN=3,
      want=sub(SPEECH_DUP, ("\n> 是的。\n", "\n> ==是的。==\n"), last=True)),
    E("英文：两段一样的原文选第 2 段", SPEECH_DUP, "Yes.", mode="en", startN=2, want=sub(SPEECH_DUP, ("\nYes.\n", "\n==Yes.==\n"), last=True)),
    E("中文：多行译文的第 2 行是前面译文的一部分", SPEECH_ML, "结论", mode="zh", startN=2, want=sub(SPEECH_ML, ("> 结论\n", "> ==结论==\n"))),
    E("中文：同上，前面那处已有划线", SPEECH_ML_MARKED, "结论", mode="zh", startN=2, want=sub(SPEECH_ML_MARKED, ("> 结论\n", "> ==结论==\n"))),
    E("对照：同上", SPEECH_ML, "结论", mode="both", startN=2, want=sub(SPEECH_ML, ("> 结论\n", "> ==结论==\n"))),
    E("信息文章原文页签：列表项和小结里的一样", ARTICLE, "OpenAI 发布新模型", section="原文", startN=2,
      want=sub(ARTICLE, ("- OpenAI 发布新模型\n- 另一项", "- ==OpenAI 发布新模型==\n- 另一项"))),
    E("信息文章原文页签：段落和小结里的一样", ARTICLE, "模型能力提升明显。", section="原文",
      want=sub(ARTICLE, ("\n模型能力提升明显。\n", "\n==模型能力提升明显。==\n"), last=True)),
    E("信息文章小结页签：列表项和原文里的一样", ARTICLE, "OpenAI 发布新模型", section="小结",
      want=sub(ARTICLE, ("- OpenAI 发布新模型", "- ==OpenAI 发布新模型=="))),
    E("信息文章小结页签：段落和原文里的一样", ARTICLE, "模型能力提升明显。", section="小结",
      want=sub(ARTICLE, ("\n模型能力提升明显。\n", "\n==模型能力提升明显。==\n"))),
    E("同一项里同样的字出现两次：选第 2 处", SAMEUNIT, "天气好", startN=2, want=sub(SAMEUNIT, ("明天天气好", "明天==天气好=="))),
    E("引用行尾有空格、前面有以它开头的引用", QTRAIL, "第一行", startN=2, want=sub(QTRAIL, ("> 第一行  ", "> ==第一行==  "))),
    E("中文：译文本身是列表", ZHLIST, "第一项译文", "第二项", mode="zh",
      want=sub(ZHLIST, ("> - 第一项译文", "> - ==第一项译文=="), ("> - 第二项译文", "> - ==第二项==译文"))),
    E("中文：列表项的多行译文跨行", ZHLIST, "步骤一的", "第二行", mode="zh",
      want=sub(ZHLIST, ("> 步骤一的译文", "> ==步骤一的译文=="), ("> 步骤一译文第二行", "> ==步骤一译文第二行=="))),
    E("中文：原文是引用，译文按引用排", QQ, "引用的译文", mode="zh", want=sub(QQ, ("> 引用的译文一。", "> ==引用的译文==一。"))),
    E("对照：引用原文 → 引用译文", QQ, "quoted", "引用的", mode="both",
      want=sub(QQ, ("quoted original one.", "==quoted original one.=="), ("> 引用的译文一。", "> ==引用的==译文一。"))),
    E("读后感里有同样的字：不算候选", REFLECT, "正文一段。", want=sub(REFLECT, ("\n正文一段。\n", "\n==正文一段。==\n"))),
    # 三、跨行批注
    E("硬换行段落：跨行批注", HARD, "正文，\n", "带一个", op="annotate",
      want=noted(HARD, ("结尾一段正文，  \n带一个", "结尾一段==正文，==  \n==带一个==[^note-1]"))),
    E("软换行英文段：跨行批注", SOFT, "line one", "line two", op="annotate",
      want=noted(SOFT, ("line one\nline two", "==line one==\n==line two==[^note-1]"))),
    E("软换行英文段 → 译文：跨项批注", SOFT, "continues", "译文第一", op="annotate",
      want=noted(SOFT, ("continues here.", "==continues here.=="), ("> 译文第一行", "> ==译文第一==[^note-1]行"))),
    # 四、引用行以「=」开头、「>」后没有空格
    E("引用行以 = 开头：不划", QEQ, "SUM", want=QEQ, fails=["refused"]),
    E("多行引用第 2 行以 = 开头：不划", LONGQ, "等号开头", want=LONGQ, fails=["refused"]),
    E("多行引用跨到以 = 开头的那行：第一行照划", LONGQ, "第一行", "等号开头", want=sub(LONGQ, ("引用第一行", "引用==第一行==")), fails=["refused"]),
    E("引用里 ROI=收益", QEQ, "收益", want=sub(QEQ, ("> ROI=收益", "> RO==I=收益=="))),
    E("引用：> 后无空格", QVAR, "引用无空格", want=sub(QVAR, (">引用无空格", ">==引用无空格=="))),
    # 五、选区带上批注号
    E("选区含批注号", NOTE, "重点", "后面", want=sub(NOTE, ("==重点==[^note-1]后面", "==重点==[^note-1]==后面=="))),
    E("列表项：含批注号", NOTE, "划过", "尾巴", want=sub(NOTE, ("==划过==[^note-2]尾巴", "==划过==[^note-2]==尾巴=="))),
    E("批注号后面的字", NOTE, "后面的字", want=sub(NOTE, ("[^note-1]后面的字", "[^note-1]==后面的字=="))),
    E("跨批注号写批注：新批注号挂在最后一处后面", NOTE, "重点", "后面", op="annotate",
      want=sub(NOTE, ("==重点==[^note-1]后面", "==重点==[^note-1]==后面==[^note-3]")).rstrip("\n") + "\n[^note-3]: 一条批注\n"),
    # 粗体、链接、行内代码
    E("粗体 + 后文", INLINE, "重要的", "观点", want=sub(INLINE, ("**重要的**观点", "==**重要的**观点=="))),
    E("跨链接尾和行内代码", INLINE, "文档", "和code", want=sub(INLINE, ("[文档](https://x.com/a)和`code`", "==[文档](https://x.com/a)和`code`=="))),
    E("只选半个粗体", INLINE, "要的观", want=sub(INLINE, ("**重要的**观", "==**重要的**观=="))),
    E("链接文字里面", INLINE, "文", "文", want=sub(INLINE, ("[文档]", "[==文==档]"))),
    E("双链别名", INLINE, "别名", want=sub(INLINE, ("[[笔记|别名]]", "==[[笔记|别名]]=="))),
    E("列表里跨粗体和链接", INLINE, "粗体", "链接字", want=sub(INLINE, ("**粗体**和[链接](https://y.com)字", "==**粗体**和[链接](https://y.com)字=="))),
    E("弯引号（文件里是直引号）", QUOTES, "说“你好”然", want=sub(QUOTES, ('说"你好"然', '==说"你好"然=='))),
    E("直撇号（文件里是弯撇号）", QUOTES, "Hop's culture", want=sub(QUOTES, ("Hop’s culture", "==Hop’s culture=="))),
    E("列表 <br> 跨行：<br> 两边各划一处", BR, "第一行", "第二行", want=sub(BR, ("- 第一行<br>第二行", "- ==第一行==<br>==第二行=="))),
    E("嵌套列表跨层", NESTED, "层一", "最里",
      want=sub(NESTED, ("- 内层一", "- 内==层一=="), ("- 内层二", "- ==内层二=="), ("1. 最里层", "1. ==最里==层"))),
    E("任务列表", TASK, "待办", want=sub(TASK, ("待办事项", "==待办==事项"))),
    E("列表 x=收益", EQB, "收益", want=sub(EQB, ("- x=收益", "- ==x=收益=="))),
    E("列表 =收益：不划", EQB, "收益", startN=2, want=EQB, fails=["refused"]),
    E("列表 a=b=c 选 b", EQB, "b", want=sub(EQB, ("- a=b=c", "- ==a=b=c=="))),
    # 数量不等：不猜
    E("文件里两处、其中一处是图注：图注不算候选，落到正文那一处", CAPTION, "短句子", startN=1,
      want=sub(CAPTION, ("\n短句子\n", "\n==短句子==\n"), last=True)),
]

# 划了线不能改变这一块的排法（不然改完页面上的项对不上，复审的真实库统计里出现过）
IMG_Q = H + "![](a.png)\n\n你最喜欢的是哪一个？\n\n后面一段正文写得长一些。\n"
LABEL = H + "**原始代码（从01至14）：**\n\n一段正文。\n"
CRLF_NOTE = "# T\r\n\r\n## 正文\r\n\r\n第一段正文。\r\n"
EDITS += [
    E("图片后面的问句划线后照旧是正文段", IMG_Q, "最喜欢", "？", want=sub(IMG_Q, ("你最喜欢的是哪一个？", "你==最喜欢的是哪一个？=="))),
    E("加粗的冒号标签划线后照旧是正文段", LABEL, "原始代码", "：", want=sub(LABEL, ("**原始代码（从01至14）：**", "**==原始代码（从01至14）：==**"))),
    E("CRLF 文件批注：定义行也用 CRLF", CRLF_NOTE, "第一段", op="annotate",
      want="# T\r\n\r\n## 正文\r\n\r\n==第一段==[^note-1]正文。\r\n\r\n[^note-1]: 一条批注\r\n"),
]

UNMARK = [
    E("跨列表项和引用取消划线", sub(H + "- 列表第二项的内容\n\n> 单行引用的一句话\n", ("第二项", "==第二项=="), ("单行引用", "==单行引用==")),
      "第二项", "单行引用", op="unmark", want=H + "- 列表第二项的内容\n\n> 单行引用的一句话\n"),
    E("一样的两项只取消第 2 个", sub(LISTS, ("1. 步骤", "1. ==步骤=="), ("2. 步骤", "2. ==步骤==")), "步骤", startN=2, op="unmark",
      want=sub(LISTS, ("1. 步骤", "1. ==步骤=="))),
    E("没有划线可取消：报没成", LISTS, "步骤", op="unmark", want=LISTS, fails=[]),
]

ROUNDTRIP = [
    ("正文→列表→嵌套→单行引用→多行引用→硬换行段",
     H + "开头一段正文，讲了第一件事。\n\n- 列表第一项的内容\n- 列表第二项的内容\n    1. 嵌套的有序项\n\n> 单行引用的一句话\n\n> 多行引用第一行\n> 多行引用第二行\n\n结尾一段正文，<HB>\n带一个硬换行。\n".replace("<HB>", "  "),
     "讲了", "带一个", "none"),
    ("对照：原文→译文→原文→多行译文", "# 一期\n\n## 演讲稿\n\nFirst original paragraph about clocks.\n\n> 第一段的译文，讲时钟。\n\nSecond original paragraph that is long.\n\n> 第二段译文的第一行\n> 第二段译文的第二行\n\nThird original paragraph here.\n\n> 第三段的译文。\n",
     "about", "译文的第二", "both"),
    ("中文：两段一样的短译文", SPEECH_DUP, "是的。", "第三段", "zh"),
    ("英文：跨三段", SPEECH_DUP, "Yes.", "Third", "en"),
]


# ---------------------------------------------------------------- 纯函数用例

TEXT = "正文一\n•\t列表项二\n•\t列表项三\n引用四"
UNITS = [[0, 3], [6, 4], [13, 4], [18, 3]]
SELECTION_CASES = [
    ([1, 1], [[0, 1, 1]]),
    ([1, 7], [[0, 1, 2], [1, 0, 2]]),
    ([0, 21], [[0, 0, 3], [1, 0, 4], [2, 0, 4], [3, 0, 3]]),
    ([4, 3], [[1, 0, 1]]),                 # 起点落在项目符号上
    ([8, 4], [[1, 2, 2]]),                 # 终点落在下一项的项目符号上
    ([4, 2], []),                          # 只选了项目符号
    ([3, 3], []),                          # 只选了块之间的换行和符号
    ([10, 1], []),
    ([2, 2], [[0, 2, 1]]),                 # 带上块后面的换行
]
# 一项按文件行拆开：(页面上这一项的字, 每个文件行排出来的字, 选区) → [行, 起, 长] 或 None（拆不回）
LINEPART_CASES = [
    ({"unit": "第一行\n第二行", "lines": ["第一行", "第二行"], "sel": [1, 5]}, [[0, 1, 2], [1, 0, 2]]),
    ({"unit": "甲\n乙\n丙", "lines": ["甲\n乙", "丙"], "sel": [2, 3]}, [[0, 2, 1], [1, 0, 1]]),   # 第一行里有 <br> 换成的换行
    ({"unit": "甲乙", "lines": ["甲", "乙"], "sel": [0, 2]}, None),                                  # 接不回去
    ({"unit": "a\n\u200a✎\u200a", "lines": ["a", "\u200a✎\u200a"], "sel": [2, 3]}, [[1, 0, 3]]),
    ({"unit": "a\nb", "lines": ["a", "b"], "sel": [1, 1]}, []),                                       # 只选了行界
]
LAYOUT_CASES = [
    (["正文", "·项", "有批注[^note-1]的"],
     ["正文\n•\t·项\n有批注 ✎ 的", [[0, 2], [5, 2], [8, 7]], [[3, 2]]]),
]
ROW_CASES = [
    ("一段\n\n- 甲\n- 乙\n\n> 引用\n\n## 标题\n\n二段", True, [[0, 4], [4, 5], [5, 6]]),
    ("一段\n\n- 甲\n- 乙\n\n> 引用\n\n## 标题\n\n二段", False, [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6]]),
    ("甲\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n乙\n\n```\ncode\n```\n\n丙\n\n---\n\n丁\n\n![](a.png)\n\n戊", True,
     [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9]]),
    ("甲\n\n> ## 标题\n> 正文\n\n乙\n\n> - 项\n\n丙\n\n> 第一行\n> 第二行\n\n丁", True, [[0, 1], [1, 2], [2, 3], [3, 4], [4, 7]]),
    ("![](a.png)\n\n图：一张图\n\n正文段落。", True, [[0, 1], [1, 2], [2, 3]]),
]
BIG_CASES = [([45, 10], [[0, 40], [40, 45]]), ([5, 3000], [[0, 2], [2, 4], [4, 5]])]
UNIT_CASES = [
    ("正文\n\n- 甲\n    1. 嵌套\n\n> 单行\n\n> 第一行\n> 第二行\n\n软换行\n第二行", "none",
     ["正文", "-甲", "-嵌套", ">单行", ">第一行 +", ">第二行", "软换行|第二行"]),
    (ZHLIST, "zh", [">开场第一段。", ">-第一项译文", ">-第二项译文", ">步骤一的译文 +", ">步骤一译文第二行", ">第三段的译文。", ">第四段的译文。"]),
]
SCAN_CASES = [
    ("---\ntitle: 甲\n---\n# 标\n\n甲\n\n- 乙\n> 丙\n> - 丁\n>戊\n\n```\n代码\n```\n\n| a |\n\n[^note-1]: 注\n<!-- c -->\n## 小结\n己\n",
     None, False, ["5:甲", "7:-乙", "8:>丙", "9:>-丁", "10:>戊", "16:| a |", "21:己"]),   # 只有一行的「|」行不成表格，照正文（同 parse）
    ("## 小结\n甲\n## 原文\n乙\n## 小节\n丙\n", "小结", False, ["1:甲"]),
    ("## 小结\n甲\n## 原文\n乙\n## 小节\n丙\n", "原文", False, ["3:乙", "5:丙"]),
    ("## 原文\n乙\n", "原文", False, []),                            # 没有小结一节：没有候选
    ("```markdown\n成稿一段\n```\n", None, False, ["1:成稿一段"]),       # markdown 栅栏里的照正文认
    ("•文字\n01.\n① 小标题\n正文​内容\n", None, True, ["0:-文字", "3:正文内容"]),
    # 论文：导读页是「摘要」一节加文末存进来的「追问」一节，原文页是「正文」到「追问」之前
    ("## 摘要\n甲\n## 正文\n乙\n[阅读原文](u)\n## 追问\n### 问\n丙\n", "摘要", False, ["1:甲", "7:丙"]),
    ("## 摘要\n甲\n## 正文\n乙\n[阅读原文](u)\n## 追问\n### 问\n丙\n", "正文", False, ["3:乙", "4:[阅读原文](u)"]),
    ("## 摘要\n甲\n## 正文\n乙\n## 追问\n文章自己的\n[阅读原文](u)\n", "正文", False, ["3:乙", "5:文章自己的", "6:[阅读原文](u)"]),
    ("## 摘要\n甲\n## 正文\n乙\n## 追问\n文章自己的\n[阅读原文](u)\n", "摘要", False, ["1:甲"]),
]


def _rand_doc(rnd):
    pool = ["甲乙丙", "乙", "甲乙", "丙丁=戊", "好的", "好的好的", "同样的一句话", "同样的", "哈哈哈哈", "第 3 步和 3 个数",
            "见[官网](https://example.com/claude)，后面说 claude 很好", "见[笔记](../剪藏/时钟.md)说时钟很准",
            "引用 [\\[1\\]](<../示例 研究/20261030_Claude 和时钟.md>) 里说 Claude 的时钟",
            "见[[时钟的历史|这篇]]，时钟很准", "这是==重点==[^note-1]后面有 1 个步骤", "有**粗体的字**和粗体",
            "用`git status`看 status", "转义 \\* 星号和 \\_ 下划线", '他说"你好"然后走了，中文句子', "数字 2026 和 20 26",
            "foo_bar 和 foobar", "a = b 和 a=b", "**加粗**[^note-1]后面"]
    blocks, lines = [], ["## 正文", ""]
    for _ in range(rnd.randint(3, 12)):
        kind = rnd.choice("pbq")
        t = rnd.choice(pool)
        blocks.append((kind, t))
        lines.append({"p": t, "b": "- " + t, "q": "> " + t}[kind])
        lines.append("")
    lines.append("[^note-1]: 旧批注")
    return "\n".join(lines) + "\n", blocks


def _random_jobs():
    jobs = []
    for seed in range(400):
        rnd = random.Random(seed)
        text, blocks = _rand_doc(rnd)
        a, b = sorted((rnd.random(), rnd.random()))
        jobs.append({"text": text, "sel": [a, b], "op": "mark", "blocks": blocks})
    return jobs


# 复审第二轮（cases5）的用例：(说明, 文字, 从, 到, 操作, 种类, 节, 第几处, 期望的文件, 没成的原因)
CASES5 = [
    ('A 制表符缩进的列表', '# T\n\n## 正文\n\n- 外\n\t- 内层项\n', '内层项', '内层项', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n- 外\n\t- ==内层项==\n', []),
    ('A 1) 有序项', '# T\n\n## 正文\n\n1) 第一步骤\n2) 第二步骤\n', '第二步', '第二步', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n1) 第一步骤\n2) ==第二步==骤\n', []),
    ('A 全角空格缩进的正文段', '# T\n\n## 正文\n\n\u3000\u3000缩进的正文段落。\n\n下一段。\n', '正文段落', '正文段落', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n\u3000\u3000缩进的==正文段落==。\n\n下一段。\n', []),
    ('A 全角空格缩进的列表项', '# T\n\n## 正文\n\n正文。\n\n\u3000- 全角缩进项\n', '全角缩进项', '全角缩进项', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n正文。\n\n\u3000- ==全角缩进项==\n', []),
    ('A 文件末尾没有换行', '# T\n\n## 正文\n\n最后一段没有换行', '没有换行', '没有换行', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n最后一段==没有换行==', []),
    ('A BOM 开头+文件头', '\ufeff---\ntitle: x\n---\n\n# T\n\n正文一段在这里。\n', '正文一段', '正文一段', 'mark', 'doc', None, 1,
     '---\ntitle: x\n---\n\n# T\n\n==正文一段==在这里。\n', []),
    ('A CRLF 文件', '---\r\ntitle: x\r\n---\r\n\r\n# T\r\n\r\n第一段正文。\r\n\r\n- 列表项甲\r\n\r\n软换行一\r\n软换行二\r\n', '第一段', '换行二', 'mark', 'doc', None, 1,
     '---\r\ntitle: x\r\n---\r\n\r\n# T\r\n\r\n==第一段正文。==\r\n\r\n- ==列表项甲==\r\n\r\n==软换行一==\r\n==软换行二==\r\n', []),
    ('A CRLF 信息文章 原文页签', '# T\r\n\r\n## 小结\r\n\r\n小结一段。\r\n\r\n## 原文\r\n\r\n原文一段。\r\n', '原文一段', '原文一段', 'mark', 'article', '原文', 1,
     '# T\r\n\r\n## 小结\r\n\r\n小结一段。\r\n\r\n## 原文\r\n\r\n==原文一段==。\r\n', []),
    ('A CRLF markdown 栅栏', '# T\r\n\r\n```markdown\r\n成稿一段话。\r\n```\r\n', '成稿一段', '成稿一段', 'mark', 'doc', None, 1,
     '# T\r\n\r\n```markdown\r\n==成稿一段==话。\r\n```\r\n', []),
    ('A 两段之间一行只有全角空格（没有真空行）', '# T\n\n## 正文\n\n甲段文字。\n\u3000\n乙段文字。\n', '甲段', '甲段', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==甲段==文字。\n\u3000\n乙段文字。\n', []),
    ('A 两段之间一行只有不间断空格', '# T\n\n## 正文\n\n甲段文字。\n\xa0\n乙段文字。\n', '乙段', '乙段', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n甲段文字。\n\xa0\n==乙段==文字。\n', []),
    ('A 剪藏：两段之间一行只有零宽字符', '# T\n\n## 正文\n\n甲段文字。\n\u200b\n乙段文字。\n', '甲段', '甲段', 'mark', 'clip', None, 1,
     '# T\n\n## 正文\n\n==甲段==文字。\n\u200b\n乙段文字。\n', []),
    ('A 剪藏：两段之间一行只有 ****', '# T\n\n## 正文\n\n甲段文字。\n****\n乙段文字。\n', '甲段', '甲段', 'mark', 'clip', None, 1,
     '# T\n\n## 正文\n\n==甲段==文字。\n****\n乙段文字。\n', []),
    ('A 剪藏：行首零宽字符的列表项', '# T\n\n## 正文\n\n正文。\n\n\u200b- 零宽开头的项\n', '零宽开头', '零宽开头', 'mark', 'clip', None, 1,
     '# T\n\n## 正文\n\n正文。\n\n\u200b- ==零宽开头==的项\n', []),
    # A 剪藏：行中零宽字符：页面上选不到（start not found），不收
    ('A 剪藏：行中零宽字符(clip)', '# T\n\n## 正文\n\n正文里有\u200b零宽字符的句子。\n', '有零宽', '有零宽', 'mark', 'clip', None, 1,
     '# T\n\n## 正文\n\n正文里==有\u200b零宽==字符的句子。\n', []),
    ('B 段里夹一行数字加点开头', '# T\n\n## 正文\n\n第一行文字\n2. 第二行其实是列表\n第三行文字\n', '第一行', '第三行', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==第一行文字==\n2. ==第二行其实是列表==\n==第三行==文字\n', []),
    ('B 段后紧跟列表没有空行', '# T\n\n## 正文\n\n一段正文\n- 紧跟的项\n', '一段', '紧跟', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==一段正文==\n- ==紧跟==的项\n', []),
    ('B setext ===', '# T\n\n## 正文\n\n像标题的一行\n===\n\n正文。\n', '像标题', '像标题', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==像标题==的一行\n===\n\n正文。\n', []),
    # B 只有一行竖线开头（不成表格）：页面上选不到（start not found），不收
    ('B 正文行里有 HTML 注释', '# T\n\n## 正文\n\n前半句 <!-- 注释 --> 后半句。\n\n另一段。\n', '前半句', '前半句', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n前半句 <!-- 注释 --> 后半句。\n\n另一段。\n', ['notFound']),
    ('C 注释行 + 图注里同样的字：落到图注行？', '# T\n\n## 正文\n\n好的 <!-- x -->\n\n![](a.png)\n\n好的\n\n末段在这里，比较长的一段正文。\n', '好的', '好的', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n好的 <!-- x -->\n\n![](a.png)\n\n好的\n\n末段在这里，比较长的一段正文。\n', ['notFound']),
    ('C 代码块里同样的行', '# T\n\n## 正文\n\n```\n同样的一行\n```\n\n同样的一行\n', '同样的一行', '同样的一行', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n```\n同样的一行\n```\n\n==同样的一行==\n', []),
    ('C 小结：我的高亮一节里有同样的列表项', '# T\n\n- 所属节目：X\n\n## 要点\n\n- 同样的要点\n- 另一个\n\n## 我的高亮\n\n- 同样的要点\n', '同样的要点', '同样的要点', 'mark', 'summary', None, 1,
     '# T\n\n- 所属节目：X\n\n## 要点\n\n- ==同样的要点==\n- 另一个\n\n## 我的高亮\n\n- 同样的要点\n', []),
    ('C 开头说明行在正文里又出现', '# T\n\n- 来源：博客\n\n## 正文\n\n- 来源：博客\n- 别的\n', '来源：博客', '来源：博客', 'mark', 'doc', None, 1,
     '# T\n\n- 来源：博客\n\n## 正文\n\n- ==来源：博客==\n- 别的\n', []),
    ('C 剪藏：•项 和 - 项 一样', '# T\n\n## 正文\n\n•好的\n\n- 好的\n', '好的', '好的', 'mark', 'clip', None, 2,
     '# T\n\n## 正文\n\n•好的\n\n- ==好的==\n', []),
    ('C 没关的 markdown 栅栏', '# T\n\n## 正文\n\n同样一句\n\n```markdown\n同样一句\n', '同样一句', '同样一句', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==同样一句==\n\n```markdown\n同样一句\n', []),
    ('D 链接地址里有同样的词', '# T\n\n## 正文\n\n见[官网](https://example.com/claude)，后面说 claude 很好。\n', 'claude', 'claude', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n见[官网](https://example.com/claude)，后面说 ==claude== 很好。\n', []),
    ('D 批注号里的数字', '# T\n\n## 正文\n\n这是==重点==[^note-3]后面有 3 个步骤。\n\n[^note-3]: 旧批注\n', '3', '3', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是==重点==[^note-3]后面有 ==3== 个步骤。\n\n[^note-3]: 旧批注\n', []),
    ('D 批注号里的字母 note', '# T\n\n## 正文\n\n这是==重点==[^note-3]后面写 note 一词。\n\n[^note-3]: 旧批注\n', 'note', 'note', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是==重点==[^note-3]后面写 ==note== 一词。\n\n[^note-3]: 旧批注\n', []),
    # D 双链目标里有同样的词：页面上选不到（start not found），不收
    ('D 重复字 哈哈哈哈 选中间两个', '# T\n\n## 正文\n\n他说哈哈哈哈。\n', '哈哈', '哈哈', 'mark', 'doc', None, 2,
     '# T\n\n## 正文\n\n他说哈==哈哈==哈。\n', []),
    ('D foo_bar 和 foobar', '# T\n\n## 正文\n\n变量 foo_bar 和 foobar 不同。\n', 'foobar', 'foobar', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n变量 foo_bar 和 ==foobar== 不同。\n', []),
    ('D 两行段第一行有 <br>：选 <br> 后那截', '# T\n\n## 正文\n\n第一行<br>甲字\n第二行有甲字\n', '甲字', '甲字', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n第一行<br>==甲字==\n第二行有甲字\n', []),
    ('D 相对链接地址里有同样的词', '# T\n\n## 正文\n\n见[笔记](../剪藏/时钟.md)，时钟很准。\n', '时钟', '时钟', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n见[笔记](../剪藏/时钟.md)，==时钟==很准。\n', []),
    ('E 粗体里有批注号，再跨过去划', '# T\n\n## 正文\n\n这是**==重点==[^note-1]**后面的字。\n\n[^note-1]: 旧\n', '点', '后面', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是**==重点==[^note-1]**后面的字。\n\n[^note-1]: 旧\n', ['refused']),
    ('E 跨两个链接', '# T\n\n## 正文\n\n看[甲文](https://a.com)和[乙文](https://b.com)两篇。\n', '文和乙', '文和乙', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n看==[甲文](https://a.com)和[乙文](https://b.com)==两篇。\n', []),
    ('E 斜体交叉', '# T\n\n## 正文\n\n这是*斜体字*后文内容。\n', '体字后', '体字后', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是*斜==体字*后==文内容。\n', []),
    ('E 行内代码一半', '# T\n\n## 正文\n\n用`git status`看状态。\n', 'status', 'status', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n用==`git status`==看状态。\n', []),
    ('E 行内代码一半+后文', '# T\n\n## 正文\n\n用`git status`看状态。\n', 'status看', 'status看', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n用==`git status`看==状态。\n', []),
    ('E 删除线交叉', '# T\n\n## 正文\n\n这是~~删掉的~~后文。\n', '掉的后', '掉的后', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是~~删==掉的~~后==文。\n', []),
    ('E 粗体里的链接', '# T\n\n## 正文\n\n**见[文档](https://x.com)说明**后文。\n', '见文', '见文', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n**==见[文档](https://x.com)==说明**后文。\n', []),
    ('E 链接地址带括号', '# T\n\n## 正文\n\n见[词条](https://zh.wikipedia.org/wiki/钟_(消歧义))后文字。\n', '词条后', '词条后', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n见[词条](https://zh.wikipedia.org/wiki/钟_(消歧义))后文字。\n', ['layout']),
    ('E 行内图片', '# T\n\n## 正文\n\n图标![小图](a.png)后面的字。\n', '后面', '后面', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n图标![小图](a.png)==后面==的字。\n', []),
    ('E 两个粗体之间', '# T\n\n## 正文\n\n**甲方**和**乙方**签约。\n', '方和乙', '方和乙', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==**甲方**和**乙方**==签约。\n', []),
    ('E 反引号不成对', '# T\n\n## 正文\n\n命令 a`b`c`d 结束。\n', 'c', 'c', 'mark', 'doc', None, 1,
     '# T\n\n## 正文\n\n命令 a`b`==c==`d 结束。\n', []),
    ('H <br> 跨行批注', '# T\n\n## 正文\n\n- 第一行<br>第二行\n- 另一项\n', '第一行', '第二行', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n- ==第一行==<br>==第二行==[^note-1]\n- 另一项\n\n[^note-1]: 一条批注\n', []),
    ('H 批注：最后一项定不了位（提示框标题）', '# T\n\n## 正文\n\n前一段正文。\n\n> [!note] 提示标题\n> 提示内容\n', '正文', '提示标', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n前一段正文。\n\n> [!note] 提示标题\n> 提示内容\n', ['notMarkable']),
    ('H 批注：正文里有没有定义的批注号', '# T\n\n## 正文\n\n旧的==字==[^note-5]在这。\n\n新的一段要批注。\n\n[^note-2]: 只有二号有定义\n', '新的一段', '新的一段', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n旧的==字==[^note-5]在这。\n\n==新的一段==[^note-6]要批注。\n\n[^note-2]: 只有二号有定义\n[^note-6]: 一条批注\n', []),
    ('H 批注：选区最后落在 = 开头的引用行', '# T\n\n## 正文\n\n前一段正文。\n\n> =SUM 公式\n', '正文', 'SUM', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n前一段正文。\n\n> =SUM 公式\n', ['refused']),
    ('H 批注已划过的字', '# T\n\n## 正文\n\n这里==已经划过==的字。\n', '经划', '经划', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n这里==已经划过==[^note-1]的字。\n\n[^note-1]: 一条批注\n', []),
    ('H 批注粗体里的字', '# T\n\n## 正文\n\n这是**重点内容**后面。\n', '重点内容', '重点内容', 'annotate', 'doc', None, 1,
     '# T\n\n## 正文\n\n这是**==重点内容==[^note-1]**后面。\n\n[^note-1]: 一条批注\n', []),
    ('G 同段两处划线，一处的字包含另一处', '# T\n\n## 正文\n\n==时钟==很准，==时钟的历史==很长。\n', '时钟的历史', '时钟的历史', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n==时钟==很准，时钟的历史很长。\n', []),
    ('G 同段两处划线，取消短的那处', '# T\n\n## 正文\n\n==时钟==很准，==时钟的历史==很长。\n', '时钟', '时钟', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n时钟很准，==时钟的历史==很长。\n', []),
    ('G 划线被粗体切成两截', '# T\n\n## 正文\n\n==**重要的**观点==在前，==别的观点==在后。\n', '重要', '重要', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n**重要的**观点在前，==别的观点==在后。\n', []),
    ('G 只碰到划线的一半', '# T\n\n## 正文\n\n前面==划过的字==后面。\n', '面划', '面划', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n前面划过的字后面。\n', []),
    ('G 跨行硬换行的两处', '# T\n\n## 正文\n\n==第一行==  \n==第二行==尾\n', '一行', '第二', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n第一行  \n第二行尾\n', []),
    ('G 两个一样的项只取消第 1 个', '# T\n\n## 正文\n\n- ==好的==\n- ==好的==\n', '好的', '好的', 'unmark', 'doc', None, 1,
     '# T\n\n## 正文\n\n- 好的\n- ==好的==\n', []),
]

# 复审第二轮点名的几种（必须改的第 1 条），改写成不会排成图注的样子
CASES5 += [
    ("双链目标里有同样的词（不是图注）", H + "见[[时钟的历史|这篇]]，时钟很准，这一段写得长一些，超过二十四个字就不会被当成图注排。\n", "时钟", "时钟", "mark", "doc", None, 1,
     H + "见[[时钟的历史|这篇]]，==时钟==很准，这一段写得长一些，超过二十四个字就不会被当成图注排。\n", []),
    ("句末引用链接的文件名里有同样的词", H + "第一句讲时钟。[\\[1\\]](<../示例 研究/20261030_Claude 和时钟.md>)。第二句说 Claude 做了实验。\n",
     "Claude", "Claude", "mark", "doc", None, 1,
     H + "第一句讲时钟。[\\[1\\]](<../示例 研究/20261030_Claude 和时钟.md>)。第二句说 ==Claude== 做了实验。\n", []),
    ("整行选中带引用标记的简报条目", H + "- 小卫星的时钟要校准 [\\[1\\]](<../示例 研究/20261030_时钟.md>)\n", "小卫星", "]", "mark", "doc", None, 1,
     H + "- ==小卫星的时钟要校准 [\\[1\\]](<../示例 研究/20261030_时钟.md>)==\n", []),
    ("选区落在链接文字里的引用标记上", H + "说法见 [\\[2\\]](<x.md>) 一文\n", "[2", "[2", "mark", "doc", None, 1,
     H + "说法见 [==\\[2==\\]](<x.md>) 一文\n", []),   # 落在链接文字里面：不扩，转义的反斜杠一起包进来
]


def _inline_lines():
    """核对 InlineSource 用的一批行：所有用例文字里的行，加随机文档的字池，加几种刁钻写法"""
    out = set()
    for c in EDITS + UNMARK:
        out.update(c["text"].replace("\r", "").split("\n"))
    for c in CASES5:
        out.update(c[1].replace("\r", "").split("\n"))
    for j in _random_jobs()[:60]:
        out.update(j["text"].split("\n"))
    out.update(["a==b==c===d", "[[a]] [[b|c]] [d](e) [f](https://g.com/h i)", "![图](https://x.com/a.png) 后", "<br>开头<br/>中间<br />",
                "&amp; &lt;b&gt;", "`a`b``c``", "~~s~~ *i* _j_ __k__ ***l***", "**a**b**c**", "\\[1\\] \\* \\_ \\\\",
                '中文"引号"和“弯引号”', "Hop’s “quote” here", "<https://x.com>", "a [^note-12] b [^note-x]", "=="])
    return sorted(x for x in out if x.strip())


INLINE_LINES = _inline_lines()
PERF_LINES = ["开头" + "_" * 40 + "结尾", "开头" + "*" * 40 + "结尾", "a" + "_a" * 40, "x" + "**" * 30 + "y", "_" * 20 + "字" + "_" * 20]


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    if not shutil.which("swiftc"):
        pytest.skip("没有 swiftc")
    d = tmp_path_factory.mktemp("mark_run")
    main = d / "main.swift"
    main.write_text(_harness(), encoding="utf-8")
    exe = d / "probe"
    r = subprocess.run(["swiftc", "-o", str(exe), *FILES, str(main)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-4000:]
    edits = EDITS + UNMARK + [E(c[0], c[1], c[2], c[3], op=c[4], kind=c[5], section=c[6], startN=c[7]) for c in CASES5]
    rt = []
    for name, text, s, e, mode in ROUNDTRIP:
        rt.append(E(name, text, s, e, mode=mode))
    payload = {
        "edits": [{k: v for k, v in c.items() if k not in ("want", "fails")} for c in edits + rt],
        "random": [{k: v for k, v in j.items() if k != "blocks"} for j in _random_jobs()],
        "selections": [{"text": TEXT, "units": UNITS, "sel": s} for s, _ in SELECTION_CASES],
        "lineParts": [c for c, _ in LINEPART_CASES],
        "inline": INLINE_LINES,
        "perf": PERF_LINES,
        "layout": [c for c, _ in LAYOUT_CASES],
        "rows": [{"text": t, "grouped": g} for t, g, _ in ROW_CASES],
        "rowsBig": [c for c, _ in BIG_CASES],
        "units": [{"text": t, "mode": m} for t, m, _ in UNIT_CASES],
        "scan": [{"text": t, "section": s, "tidy": td} for t, s, td, _ in SCAN_CASES],
    }
    p = subprocess.run([str(exe)], input=json.dumps(payload), capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-4000:]
    out = json.loads(p.stdout)
    out["_rt"] = out["edits"][len(edits):]
    out["edits"] = out["edits"][:len(edits)]
    return out


@pytest.mark.parametrize("i", range(len(CASES5)), ids=[c[0] for c in CASES5])
def test_复审第二轮的用例(probe, i):
    c = CASES5[i]
    got = probe["edits"][len(EDITS) + len(UNMARK) + i]
    assert "err" not in got, got
    assert got["text"] == c[8], (c[0], got)
    assert got["failures"] == c[9], (c[0], got)


def test_重复次数不一致时拒绝且不改文件(probe):
    assert probe["countMismatch"] == [
        {"unchanged": True, "done": 0, "failures": ["ambiguous"]},
        {"unchanged": True, "done": 0, "failures": ["ambiguous"]},
    ]


@pytest.mark.parametrize("i", range(len(EDITS) + len(UNMARK)), ids=[c["name"] for c in EDITS + UNMARK])
def test_定位和落盘(probe, i):
    c = (EDITS + UNMARK)[i]
    got = probe["edits"][i]
    assert "err" not in got, got
    assert got["text"] == c["want"], (c["name"], got)
    assert "===" not in got["text"]
    if c["op"] != "unmark":
        assert got["failures"] == c["fails"], (c["name"], got)


def test_没有划线可取消时报没成(probe):
    got = probe["edits"][len(EDITS) + 2]
    assert got["text"] == LISTS and not got["changed"]


@pytest.mark.parametrize("i", range(len(ROUNDTRIP)), ids=[c[0] for c in ROUNDTRIP])
def test_划了再取消回到原样(probe, i, tmp_path):
    got = probe["_rt"][i]
    assert "err" not in got and got["changed"] and not got["failures"], got
    assert "===" not in got["text"]
    assert got["text"].replace("==", "") == ROUNDTRIP[i][1]


MARK_RE = re.compile(r"(?<!=)==([^=\n](?:=?[^=\n])*)==(?!=)")


def test_随机文档_落点和底色_不出现三个等号_取消不留新划线(probe):
    jobs = _random_jobs()
    changed_n = 0
    for job, got in zip(jobs, probe["random"]):
        assert "err" not in got, got
        text, new = job["text"], got["text"]
        assert "bad" not in got, (job, got)
        assert "===" not in new, (job, new)
        assert new.replace("==", "") == text.replace("==", ""), (job, new)
        # 改了的行都在选中的那几项对应的行里（第 i 块在文件第 2 + 2i 行）
        allowed = {2 + 2 * b for b in got["blocks"]}
        changed = {i for i, (x, y) in enumerate(zip(text.split("\n"), new.split("\n"))) if x != y}
        assert changed <= allowed, (job, got)
        if got["changed"]:
            changed_n += 1
            undo = got["undo"]
            assert undo.replace("==", "") == text.replace("==", ""), (job, undo)
            assert set(MARK_RE.findall(undo)) <= set(MARK_RE.findall(text)), (job, undo)
    assert changed_n >= len(jobs) * 0.7, changed_n


def test_对照表和_Markdown_inline_一致(probe):
    # 对不上的只允许这两种（划线时拒绝，报「排版对不上」）：HTML 实体（&amp; 排成一个字）、链接地址里带括号（系统把括号后面的当正文）
    known = {"&amp; &lt;b&gt;", "见[词条](https://zh.wikipedia.org/wiki/钟_(消歧义))后文字。"}
    bad = [(line, r) for line, r in zip(INLINE_LINES, probe["inline"]) if not (r[0] and r[2] and (r[1] or line in known))]
    assert not bad, bad[:5]
    assert all(not r[1] for line, r in zip(INLINE_LINES, probe["inline"]) if line in known)


def test_长串下划线星号很快返回(probe):
    assert all(t < 0.5 for t in probe["perf"]), list(zip(PERF_LINES, probe["perf"]))


def test_选区映射(probe):
    assert probe["selections"] == [w for _, w in SELECTION_CASES]
    assert probe["lineParts"] == [w for _, w in LINEPART_CASES]


def test_文本框里的字和范围(probe):
    assert probe["layout"] == [w for _, w in LAYOUT_CASES]


def test_并段规则(probe):
    assert probe["rows"] == [w for _, _, w in ROW_CASES]
    assert probe["rowsBig"] == [w for _, w in BIG_CASES]


def test_每项的行键(probe):
    assert probe["units"] == [w for _, _, w in UNIT_CASES]


def test_文件里的候选行(probe):
    assert probe["scan"] == [w for *_, w in SCAN_CASES]


def test_源码里用的是这条链():
    md = _read("Markdown.swift")
    assert "MarkRun.ordinals(blocks, rows: rows, translated: translated, listed: listed" in md
    assert "MarkRun.target(units[piece.para], ord, display: piece.display, selection: piece.range, render: Markdown.displayed)" in md
    assert "InlineSource.prepare(.init(s)).string" in md
    assert "MarkRun.selections(ns as String, units: paraRanges" in _read("DocEdits.swift")
    assert "MarkRun.layoutText(" in _read("DocEdits.swift")
    an = _read("Annotations.swift")
    assert an.count("MarkAnchor.apply(") == 3
    for gone in ("static func highlighted(", "static func blockRange(", "static func annotated("):
        assert gone not in _read("DocEdits.swift")


def test_读后感一节的范围两份一样():
    # DocEdit.reflectionRange（tests/test_jottings.py 摘出来用）和 MarkAnchor.reflectionRange（定位时跳过读后感）必须一字不差
    a = _chunk(_read("DocEdits.swift"), "static func reflectionRange(")
    b = _chunk(_read("MarkAnchor.swift"), "static func reflectionRange(")
    assert a == b
