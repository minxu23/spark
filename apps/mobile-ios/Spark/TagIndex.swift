import Foundation

/// 标签索引（手机只读）：笔记库里的 Spark/.tags.json，由电脑写。规则同电脑 core/tags.py
/// （_read、resolve、main_file、cloud、items_for）；tests/test_tags_ios.py 用同一份输入逐项对照。
/// 只依赖 Foundation，能用 swiftc 单独编。
///
/// 路径都是相对 Spark/ 的。剪藏、论文的标签在文件头里（headerTags），播客、演讲、信息文章的标签在 docs 里，
/// 键是这一期的主文件；主文件没有键就看同一期别的文件的键。

struct TagItem: Hashable {
    var ref: String                 // 手机上找回这一条用的键（界面用）
    var type: String                // clip / paper / episode / talk / track
    var paths: [String]             // 剪藏、论文一个；一期节目、演讲几个文件
    var headerTags: [String] = []   // 剪藏、论文文件头里的 tags（还没过别名）
    var title = "", source = ""
    var date = ""                   // yyyy-MM-dd，排先后用
}

struct CloudTag: Hashable {
    let tag: String
    let count: Int
    let kind: String
}

struct TagIndex {
    var aliases: [String: String] = [:]
    var kinds: [String: String] = [:]
    var docs: [String: [String]] = [:]

    static let kindNames: Set<String> = ["tech", "org", "biz"]
    static let defaultKind = "tech"

    init() {}

    /// 坏文件、顶层不是对象、version 不是整数 1：当空索引。合法的对象里类型不对的那一项丢掉，其余照用。
    init(json text: String?) {
        guard let text, text.unicodeScalars.first != "\u{feff}",   // Python 的 json 不收 BOM
              let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { return }
        if let v = obj["version"] {
            guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), !CFNumberIsFloatType(n), n.intValue == 1 else { return }
        }
        if let a = obj["aliases"] as? [String: Any] {
            for (k, v) in a { if let s = v as? String { aliases[k] = s } }
        }
        if let k = obj["kinds"] as? [String: Any] {
            for (key, v) in k { if let s = v as? String, Self.kindNames.contains(s) { kinds[key] = s } }
        }
        if let d = obj["docs"] as? [String: Any] {
            for (key, v) in d { if let list = v as? [Any], list.allSatisfy({ $0 is String }) { docs[key] = list.map { $0 as! String } } }
        }
    }

    // MARK: 别名

    /// 跟 Python str.strip() 一样的空白（多出 \x1c–\x1f）
    private static let space = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\u{1c}\u{1d}\u{1e}\u{1f}"))
    private static func norm(_ s: String) -> String { s.trimmingCharacters(in: space) }

    /// 顺着 a→b→c 找到最后的正式标签；表里有环时停在回到见过的那一个之前
    func resolve(_ tag: String) -> String {
        var cur = Self.norm(tag), seen = Set<String>()
        while let next = aliases[cur], !seen.contains(cur) {
            seen.insert(cur)
            let n = Self.norm(next)
            if n.isEmpty || seen.contains(n) { break }
            cur = n
        }
        return cur
    }

    func cleaned(_ raw: [String]) -> [String] {
        var out: [String] = []
        for t in raw { let r = resolve(t); if !r.isEmpty && !out.contains(r) { out.append(r) } }
        return out
    }

    // MARK: 条目

    /// 按码位比（同 Python 字符串比较）
    static func less(_ a: String, _ b: String) -> Bool { a.utf8.lexicographicallyPrecedes(b.utf8) }

    /// 一期的主文件：notes/ → speech/ → transcripts/ 下排序后第一个；都没有取排序后的第一个
    static func mainFile(_ paths: [String]) -> String {
        for sub in ["notes", "speech", "transcripts"] {
            let hit = paths.filter { p in
                let c = p.split(separator: "/", omittingEmptySubsequences: false)
                return c.count >= 2 && c[c.count - 2] == sub
            }
            if let f = hit.min(by: less) { return f }
        }
        return paths.min(by: less) ?? ""
    }

    func tags(of it: TagItem) -> [String] {
        if it.type == "clip" || it.type == "paper" { return cleaned(it.headerTags) }
        let main = Self.mainFile(it.paths)
        let keys = [main] + it.paths.filter { $0 != main }.sorted(by: Self.less)
        guard let hit = keys.first(where: { docs[$0] != nil }) else { return [] }
        return cleaned(docs[hit] ?? [])
    }

    /// 有这个标签的条目数（一篇里两个写法指向同一个只算一次），≥ minCount 的；按篇数降序，同数按标签升序
    func cloud(_ items: [TagItem], minCount: Int) -> [CloudTag] {
        var count: [String: Int] = [:]
        for it in items { for t in Set(tags(of: it)) { count[t, default: 0] += 1 } }
        return count.filter { $0.value >= minCount }
            .sorted { $0.value != $1.value ? $0.value > $1.value : Self.less($0.key, $1.key) }
            .map { CloudTag(tag: $0.key, count: $0.value, kind: kinds[$0.key] ?? Self.defaultKind) }
    }

    /// 这个标签（先过别名）下的条目，按日期新到旧，同一天按主文件路径
    func items(for tag: String, in items: [TagItem]) -> [TagItem] {
        let want = resolve(tag)
        return items.filter { tags(of: $0).contains(want) }
            .map { ($0, Self.mainFile($0.paths)) }
            .sorted { $0.0.date != $1.0.date ? Self.less($1.0.date, $0.0.date) : Self.less($0.1, $1.1) }
            .map(\.0)
    }
}
