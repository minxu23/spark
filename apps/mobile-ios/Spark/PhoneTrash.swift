import Foundation

/// 手机上的废纸篓：手机上删掉的剪藏、复盘、随手记先放在这里（App 自己的 Application Support，不在笔记库文件夹里，同步不碰），
/// 30 天内能放回原处，从删掉那一刻起满 720 小时自动清掉，也能手工彻底删除、清空。这里只放规则，不碰文件、网络和界面
/// （tests/test_jottings.py 用 swiftc 编出来测）。电脑那边照旧把笔记库里那份移进 macOS 的废纸篓，不做自动清除。
///
/// 「满 30 天清掉」是用户明确要的按时间清除：整个 App 里判断不看时钟，只有这里例外，而且只管废纸篓里的条目。
/// 自动清除只认网络时间（手机联网时取，见 Vault 的 NetClock）：没联网不清；手机自己的钟不拿来判断清不清。
enum PhoneTrash {
    struct Item: Codable, Identifiable, Equatable {
        let id: String
        /// 原来在笔记库里的路径
        let path: String
        let title: String
        /// 剪藏 / 复盘 / 随手记
        let kind: String
        /// 删掉的时刻（秒）：删的时候手头有 10 分钟内的网络时间，用它校正过；没有就是手机当时的钟（clockUnverified）
        var deletedAt: Double
        /// 删掉时手机上的内容（有待确认的修改时是最新那一版）
        let content: String
        /// 交给电脑的删除任务
        var deleteTaskID: String = ""
        /// 删的时候还有没交到电脑的内容（待确认的修改、发件箱里撤下的存、手机上新建电脑还没有的）：放回时要交给电脑
        var unsent = false
        /// 删的时候这篇还没确认电脑收下的修改（bases、是不是手机上新建的……）：放回原处时原样装回去，电脑照常判同一版还是冲突
        var record: PendingEdits.Record?
        /// 没有待确认的修改时，删的时候手机上那份（就是电脑上那一版）的 SHA-256：放回时当 base
        var base: String?
        /// 删除任务里的标识：放回原处时交给电脑取消它（cancels），晚到的删除不再执行
        var deleteKey = ""
        /// 电脑没删成，文章还在书架上：放回原处只是把这一条拿掉，不写文件；彻底删除也不动图片
        var deleteFailed = false
        /// 删除时刻是手机自己的钟记的、没跟网络时间核对过。旧版存的条目没有这一项，当成没核对过
        var clockUnverified = true
        /// 在别的设备上删掉的：这台手机上新建的一篇，交到电脑以后在另一台设备上删了，电脑同步来的墓碑里有手机上这份
        /// （ElsewhereDeleted）。没有删除任务、没有 deleteKey；放回原处照常交带 restore 的存
        var elsewhere = false

        init(id: String, path: String, title: String, kind: String, deletedAt: Double, content: String,
             deleteTaskID: String = "", unsent: Bool = false) {
            (self.id, self.path, self.title, self.kind, self.deletedAt, self.content, self.deleteTaskID, self.unsent) =
                (id, path, title, kind, deletedAt, content, deleteTaskID, unsent)
        }

        /// 少了后加的字段也照样读
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(String.self, forKey: .id)
            path = try c.decode(String.self, forKey: .path)
            title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
            kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
            deletedAt = try c.decode(Double.self, forKey: .deletedAt)
            content = try c.decode(String.self, forKey: .content)
            deleteTaskID = try c.decodeIfPresent(String.self, forKey: .deleteTaskID) ?? ""
            unsent = try c.decodeIfPresent(Bool.self, forKey: .unsent) ?? false
            record = try c.decodeIfPresent(PendingEdits.Record.self, forKey: .record)
            base = try c.decodeIfPresent(String.self, forKey: .base)
            deleteKey = try c.decodeIfPresent(String.self, forKey: .deleteKey) ?? ""
            deleteFailed = try c.decodeIfPresent(Bool.self, forKey: .deleteFailed) ?? false
            clockUnverified = try c.decodeIfPresent(Bool.self, forKey: .clockUnverified) ?? true
            elsewhere = try c.decodeIfPresent(Bool.self, forKey: .elsewhere) ?? false
        }
    }

    static let keepDays = 30
    /// 从删掉那一刻起流逝满这么久自动清除（720 小时）
    static let keepSeconds: Double = 720 * 3600
    /// 删除时刻跟网络时间对得上的容差
    static let clockTolerance: Double = 5 * 60

    /// 能不能自动清：只用网络时间减删除时刻（手机当前的钟不参与）。
    /// - 没有网络时间（没联网、请求失败、Date 解析不了）：不清，等下次联网；
    /// - 删除时刻晚于网络时间（删的时候手机的钟是错的）：不清；
    /// - 满 720 小时：清；
    /// - 电脑没删成、删的时候还有没交到电脑的修改：不清（清了那些修改就没了），等用户放回
    static func purgeable(_ item: Item, deviceNow: Date, networkNow: Date?) -> Bool {
        guard let net = networkNow?.timeIntervalSince1970 else { return false }
        guard item.deletedAt <= net, !(item.deleteFailed && hasUnsent(item)) else { return false }
        return net - item.deletedAt >= keepSeconds
    }

    /// 没核对过删除时刻的条目，第一次取到网络时间时核对：删除时刻一律改成这次的网络时间，反悔期从这时重新算。
    /// 核对那一刻手机的钟准不准，说明不了删除那一刻准不准（删的时候钟慢了 10 天、后来改回来了，原时刻就早了 10 天），
    /// 所以不保留原时刻；原时刻在将来的（删的时候钟快了）也拉回到这次的网络时间
    static func reconciled(_ item: Item, deviceNow: Date, networkNow: Date?) -> Item {
        guard item.clockUnverified, let net = networkNow else { return item }
        var out = item
        out.deletedAt = net.timeIntervalSince1970
        out.clockUnverified = false
        return out
    }

    /// 放回到哪：电脑没删成、文章还在书架上的条目，书架上这篇后来又改过（有新的待确认修改、或者内容跟废纸篓里这一版不同）：
    /// 不写原路径（会盖掉书架上那一版），换个名字另存（当新建的一篇），两边都留着，renamed 为真。
    /// 别的照 restorePath：原路径空着放回原处，被占了换名字
    static func restoreTarget(_ item: Item, shelfText: String?, hasPending: Bool, taken: (String) -> Bool) -> (path: String, renamed: Bool) {
        if item.deleteFailed {
            guard hasPending || (shelfText != nil && shelfText != item.content) else { return (item.path, false) }
            return (restorePath(item.path) { $0 == item.path || taken($0) }, true)
        }
        let p = restorePath(item.path, taken: taken)
        return (p, p != item.path)
    }

    /// 删掉的时刻：offset 是 10 分钟内取到的网络时间减手机时间；有就用校正后的时刻，没有就用手机的钟并标没核对过
    static func deletionStamp(deviceNow: Date, offset: TimeInterval?) -> (at: Double, unverified: Bool) {
        guard let offset else { return (deviceNow.timeIntervalSince1970, true) }
        return (deviceNow.timeIntervalSince1970 + offset, false)
    }

    /// 响应头里的 Date：RFC 1123（Sun, 06 Nov 1994 08:49:37 GMT）、RFC 850（Sunday, 06-Nov-94 08:49:37 GMT）、
    /// asctime（Sun Nov  6 08:49:37 1994）。认不出返回 nil
    static func httpDate(_ raw: String) -> Date? {
        let s = raw.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        for format in ["EEE, dd MMM yyyy HH:mm:ss zzz", "EEEE, dd-MMM-yy HH:mm:ss zzz", "EEE MMM d HH:mm:ss yyyy"] {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = TimeZone(identifier: "GMT")
            f.dateFormat = format
            f.isLenient = false
            if let d = f.date(from: s) { return d }
        }
        return nil
    }

    /// 删的时候有没交到电脑的内容（待确认的修改、发件箱里撤下的存）
    static func hasUnsent(_ item: Item) -> Bool { item.record != nil || item.unsent }

    /// 还剩几天（向上取整，最少 0）。只用来显示：now 有 10 分钟内的网络时间就用它，没有就用手机的钟
    static func daysLeft(_ item: Item, now: Date) -> Int {
        let left = keepSeconds - max(0, now.timeIntervalSince1970 - item.deletedAt)
        return max(0, Int((left / 86400).rounded(.up)))
    }

    /// 放回哪里：原路径空着就放回原处；被别的文件占了就换个名字（去掉已有的 _N 再起 _2、_3……），不覆盖
    static func restorePath(_ path: String, taken: (String) -> Bool) -> String {
        guard taken(path) else { return path }
        let ext = (path as NSString).pathExtension
        let stem = (path as NSString).deletingPathExtension.replacingOccurrences(of: #"_\d+$"#, with: "", options: .regularExpression)
        for n in 2..<1000 {
            let p = "\(stem)_\(n)" + (ext.isEmpty ? "" : ".\(ext)")
            if !taken(p) { return p }
        }
        return "\(stem)_\(UUID().uuidString.prefix(8))" + (ext.isEmpty ? "" : ".\(ext)")
    }

    // MARK: 彻底删除时一起删的图片

    /// 文章里一处图片引用（Markdown.parse 认出的 src）落在这篇所在文件夹 attachments/ 下的哪张图（相对笔记库）。
    /// 解码 %xx，拒绝 ..、绝对路径、网址、子文件夹；不在 attachments/ 下的不管（不是这篇存下的图）
    static func imagePath(src: String, docPath: String) -> String? {
        let s = (src.removingPercentEncoding ?? src).trimmingCharacters(in: .whitespaces)
        guard !s.hasPrefix("/"), !s.contains("://"), !s.contains("\\") else { return nil }
        let parts = s.split(separator: "/", omittingEmptySubsequences: false).map(String.init).filter { $0 != "." }
        guard parts.count == 2, parts[0] == "attachments", let name = parts.last, !name.isEmpty, !name.hasPrefix("."),
              !parts.contains("..") else { return nil }
        let dir = (docPath as NSString).deletingLastPathComponent
        return dir.isEmpty ? "attachments/\(name)" : "\(dir)/attachments/\(name)"
    }

    /// 哪些图可以删：所有文档都读成功（unreadable 为假）、不在同步，而且没有任何一处文字里出现这张图的文件名——
    /// 不只看认得出的引用写法（![](…)、![[…]]、带相对路径的），代码块、普通文字里出现也算在用；
    /// 别的文件夹里有同名图、文档只写了文件名的，也按在用算。texts：笔记库里别的文档、废纸篓里别的条目、
    /// 待确认的修改、发件箱里没交的存。返回 nil 表示这一轮一张都不删（等下次）
    /// raw：不是 UTF-8 的文档的原始字节（按字节找：文件名的 UTF-8 字节，NFC、NFD 两种写法，ASCII 不分大小写，同电脑）
    static func orphans(candidates: [String], texts: [String], raw: [Data] = [], unreadable: Bool, syncing: Bool) -> [String]? {
        guard !unreadable, !syncing else { return nil }
        var scan = OrphanScan(candidates: candidates)
        for d in raw { scan.see(raw: d) }
        for t in texts { scan.see(text: t) }
        return scan.orphans
    }

    /// 上面这条规则的逐篇做法：一篇一篇地看，看完一篇就放掉，不把整个笔记库的文字同时留在内存里；
    /// 按字节找（memmem），不用 String.contains——几千篇文档乘上几十张图，String.contains 要跑几十秒，
    /// 在主线程上就是卡死、被系统杀掉。Vault.runImagePurge 在后台线程上用它。
    /// 按字节找比按字找只会多认出「在用」（比如文件名后面紧跟一个组合符号），不会少认，所以只会少删、不会多删
    struct OrphanScan {
        /// 还没发现有人用的图：路径 → 要找的几种写法（小写 NFC 的名字、%xx 写法、空格写成 + 的）和不是 UTF-8 的文档里按字节找的写法
        private var left: [String: (text: [[UInt8]], raw: [[UInt8]])] = [:]
        private let order: [String]
        /// 有 BOM 却按 UTF-16 解不开的文档：拿不准，当成每张图都在用（同电脑）
        private(set) var unsure = false

        init(candidates: [String]) {
            order = candidates
            for c in candidates {
                let base = (c as NSString).lastPathComponent
                let name = base.precomposedStringWithCanonicalMapping.lowercased()
                let forms = [name, name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name,
                             name.replacingOccurrences(of: " ", with: "+")]
                let bytes = [base.precomposedStringWithCanonicalMapping, base.decomposedStringWithCanonicalMapping]
                    .map { Array(PhoneTrash.asciiLower(Data($0.utf8))) }
                // 同一种写法只找一遍（纯英文数字的名字，三种写法是同一个）
                left[c] = (Array(Set(forms)).map { Array($0.utf8) }, Array(Set(bytes)))
            }
        }

        /// 不用再看了：每张图都找到有人用，或者遇到了拿不准的文档
        var done: Bool { unsure || left.isEmpty }

        /// 没人用的图，按传进来的顺序
        var orphans: [String] { unsure ? [] : order.filter { left[$0] != nil } }

        /// 一篇文档的文字（或者废纸篓里一条、一份待确认的修改）
        mutating func see(text: String) {
            guard !done else { return }
            var low = text.precomposedStringWithCanonicalMapping.lowercased()
            low.withUTF8 { hay in drop { $0.text.contains { PhoneTrash.find($0, in: UnsafeRawBufferPointer(hay)) } } }
        }

        /// 一篇不是 UTF-8 的文档的原始字节
        mutating func see(raw: Data) {
            guard !done else { return }
            if raw.starts(with: [0xFF, 0xFE]) || raw.starts(with: [0xFE, 0xFF]) { unsure = true; return }
            PhoneTrash.asciiLower(raw).withUnsafeBytes { hay in drop { $0.raw.contains { PhoneTrash.find($0, in: hay) } } }
        }

        /// 一个文件的内容：带 BOM 的 UTF-16 按 UTF-16 读，别的按 UTF-8；读不成文字的按字节找（同电脑）
        mutating func see(file data: Data) {
            guard !done else { return }
            let utf16 = data.starts(with: [0xFF, 0xFE]) || data.starts(with: [0xFE, 0xFF])
            if let t = String(data: data, encoding: utf16 ? .utf16 : .utf8) { see(text: t) } else { see(raw: data) }
        }

        private mutating func drop(_ used: ((text: [[UInt8]], raw: [[UInt8]])) -> Bool) {
            for (c, forms) in left where used(forms) { left[c] = nil }
        }
    }

    /// needle 在不在 hay 里（按字节）。空的 needle 当成在：拿不准就算在用
    private static func find(_ needle: [UInt8], in hay: UnsafeRawBufferPointer) -> Bool {
        guard !needle.isEmpty, let base = hay.baseAddress else { return needle.isEmpty }
        return needle.withUnsafeBytes { memmem(base, hay.count, $0.baseAddress, $0.count) != nil }
    }

    private static func asciiLower(_ d: Data) -> Data {
        var out = d
        out.withUnsafeMutableBytes { buf in
            for i in buf.indices where buf[i] >= 0x41 && buf[i] <= 0x5A { buf[i] += 32 }
        }
        return out
    }

    /// 放回原处时装回去的待确认记录：原路径放回、删的时候有待确认的修改 → 原样（bases、是不是新建），任务重新交；
    /// 没有待确认的修改、删的时候电脑上有这篇 → base 是那时的电脑版本；换了名字放回、或者电脑上本来没有 → 当成新建的一篇
    static func restoredRecord(_ item: Item, target: String, now: Int64) -> PendingEdits.Record {
        if target == item.path, let r = item.record {
            var rec = PendingEdits.Record(content: item.content, bases: r.bases, editedAt: max(now, r.editedAt + 1), taskIDs: [],
                                          olderBlobs: r.olderBlobs, created: r.created)
            rec.images = r.images
            return rec
        }
        if target == item.path, let b = item.base {
            return PendingEdits.Record(content: item.content, bases: [b], editedAt: now, taskIDs: [])
        }
        var rec = PendingEdits.Record(content: item.content, bases: [], editedAt: now, taskIDs: [], created: true)
        rec.images = item.record?.images ?? [:]   // 换名字放回：图片不跟着改名，原地址照带
        return rec
    }

    /// 移进废纸篓
    static func adding(_ items: [Item], _ item: Item) -> [Item] {
        [item] + items.filter { $0.id != item.id }
    }

    /// 拿出来（放回原处、彻底删除都用）
    static func removing(_ items: [Item], id: String) -> [Item] {
        items.filter { $0.id != id }
    }
}
