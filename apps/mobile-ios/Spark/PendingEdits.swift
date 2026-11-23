import CryptoKit
import Foundation

/// 手机上改过、还没看到电脑收下的文章：规则（不碰文件，tests/test_doctime.py 用 swiftc 编出来测）。
///
/// 从改的那一刻起，到手机看到电脑那边已经是这一版（同步过来的文件树里这篇的 blob sha 等于手机这份，
/// 或者等于电脑说它写进去的那一版 applied_sha）为止，这篇是「待确认」的：不管走发件箱（数据线）还是 GitHub Issue，
/// 书架、阅读页、下一次改的起点都用手机这份，电脑同步过来的文件盖不掉它。
/// 每篇只留一条记录，再改就换掉：bases 记着电脑上可能还是的那几版（最早改之前那版 + 之后每次手机这份），
/// 电脑那篇是其中任何一版就照收（core/remote_tasks._same_version）。
enum PendingEdits {
    struct Record: Codable, Equatable {
        var content: String
        /// 电脑上可能是的那几版的 SHA-256（第一个是最早改之前那版）；手机上新建、电脑上还没有的是空的
        var bases: [String]
        /// 手机上改的时刻（毫秒）；同一篇每次严格变大
        var editedAt: Int64
        /// 交出去的任务：发件箱的 id，或者 gh:<Issue 号>；最后一个是这份内容的
        var taskIDs: [String]
        /// pending：等电脑收下；conflict：电脑没收（failKind 说是哪种）；gone：电脑上已经没有这篇了。每次同步重新判断
        var state: String = "pending"
        /// 这份内容之前的那几版的 blob sha（改的起点、之前每次手机这份、之前任务写进电脑的那版）：
        /// 同步过来的文件树是其中一版，说明电脑那边还没到这次（不是电脑后来又改了）
        var olderBlobs: [String] = []
        /// 电脑说的失败原因：conflict 两边都改过（手机这一版存进了 note-conflicts）/ gone 电脑上没有了 / rejected 电脑收不了
        var failKind: String? = nil
        /// 手机上新建、电脑上还没有的一篇（交的第一份不带 bases）
        var created = false
        /// 标成冲突时失败的那几个任务（阅读页上已经提醒过）：用户再改一次，新记录不再带上它们
        var failedIDs: [String] = []
        /// 失败的不是最后那个任务，是更早的一次修改（提醒的说法不一样）
        var failedEarlier = false
        /// 这次冲突的提醒真的在阅读页上显示过（Vault.notePendingShown）。显示过了用户再改，才不再带上报过失败的任务
        var noticed = false
        /// 剪藏的图片：笔记库里的路径 → 原图地址（换名字重交、再交一次时带上，电脑按它下图）
        var images: [String: String] = [:]
        /// 用户点了「重新交给电脑」：任务带 restore，电脑上这个路径有墓碑也清掉、照建（同手机废纸篓的放回原处）。
        /// 电脑收下以后（settled）不再带
        var restore = false

        init(content: String, bases: [String], editedAt: Int64, taskIDs: [String], state: String = "pending",
             olderBlobs: [String] = [], failKind: String? = nil, created: Bool = false) {
            (self.content, self.bases, self.editedAt, self.taskIDs, self.state, self.olderBlobs, self.failKind, self.created) =
                (content, bases, editedAt, taskIDs, state, olderBlobs, failKind, created)
        }

        /// 旧版存的记录少几个字段：照样读（缺的用默认值）
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            content = try c.decode(String.self, forKey: .content)
            bases = try c.decodeIfPresent([String].self, forKey: .bases) ?? []
            editedAt = try c.decodeIfPresent(Int64.self, forKey: .editedAt) ?? 0
            taskIDs = try c.decodeIfPresent([String].self, forKey: .taskIDs) ?? []
            state = try c.decodeIfPresent(String.self, forKey: .state) ?? "pending"
            olderBlobs = try c.decodeIfPresent([String].self, forKey: .olderBlobs) ?? []
            failKind = try c.decodeIfPresent(String.self, forKey: .failKind)
            created = try c.decodeIfPresent(Bool.self, forKey: .created) ?? false
            failedIDs = try c.decodeIfPresent([String].self, forKey: .failedIDs) ?? []
            failedEarlier = try c.decodeIfPresent(Bool.self, forKey: .failedEarlier) ?? false
            noticed = try c.decodeIfPresent(Bool.self, forKey: .noticed) ?? false
            images = try c.decodeIfPresent([String: String].self, forKey: .images) ?? [:]
            restore = try c.decodeIfPresent(Bool.self, forKey: .restore) ?? false
        }
    }

    /// 最后那个任务（这份内容的）在电脑上怎么样了。state：done / failed / waiting；nil 是不知道（没结果也不在发件箱）
    struct Last: Equatable {
        var state: String?
        var appliedSHA: String? = nil
        var kind: String? = nil
    }

    /// bases 最多留几个：最早的 keepOldest 个（新建时的那几版，可能走 GitHub 早到了电脑）加最近的。电脑 MAX_BASES 一样
    static let maxBases = 32
    static let keepOldest = 8
    static let maxTasks = 8
    static let maxOlder = 16

    static func sha256(_ s: String) -> String {
        SHA256.hash(data: Data(s.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// 跟 git / GitHub 文件树一样的 blob sha
    static func blobSHA(_ s: String) -> String {
        let data = Data(s.utf8)
        return Insecure.SHA1.hash(data: Data("blob \(data.count)\0".utf8) + data).map { String(format: "%02x", $0) }.joined()
    }

    /// 又改了一次：换掉记录。startText：这次改的起点（手机上看到的那份）；onMac：电脑上有这篇。
    /// chainNew：手机上新建的一篇也带 bases——之前每一份手机上的内容（包括新建时交出去、没有记录的那份起点，比如刚存的剪藏）
    /// 都可能已经到了电脑，bases 把它们都列上，电脑上是其中哪一版都照收。复盘是手机上那一份说了算，传 false，一直不带。
    /// 起点不是记录里这份（编辑页打开得早，期间又划了线、加了星标）：bases 只写起点那一版，电脑上已经是后来那份就判冲突，
    /// 两份都留着，不悄悄盖掉后来那份
    static func edited(previous: Record?, startText: String?, onMac: Bool, content: String, now: Int64, chainNew: Bool = true) -> Record {
        var bases: [String]
        if let previous, let startText, startText != previous.content, onMac || chainNew {
            bases = [sha256(startText)]
        } else if let previous {
            bases = previous.bases.isEmpty ? (chainNew ? [sha256(previous.content)] : []) : previous.bases + [sha256(previous.content)]
        } else {
            bases = onMac || chainNew ? startText.map { [sha256($0)] } ?? [] : []
        }
        var seen = Set<String>()
        bases = bases.filter { seen.insert($0).inserted }
        if bases.count > maxBases { bases = Array(bases.prefix(keepOldest)) + bases.suffix(maxBases - keepOldest) }   // 最早那几版一直留着
        let at = max(now, (previous?.editedAt ?? 0) + 1)
        var older = previous.map { $0.olderBlobs + [blobSHA($0.content)] } ?? []
        if let startText { older.append(blobSHA(startText)) }
        var seenOlder = Set<String>()
        older = Array(older.filter { seenOlder.insert($0).inserted }.suffix(maxOlder))
        // 上一份已经标成冲突、提醒过了（用户看到提醒后接着改）：报过失败的那几个任务不再带上，
        // 不然它们的结果每次对账都会重新判成冲突，后来的修改收下了提醒也不消失。没提醒过的失败照样带着
        var tasks = previous?.taskIDs ?? []
        if let previous, previous.state == "conflict", previous.noticed { tasks.removeAll { previous.failedIDs.contains($0) } }
        var out = Record(content: content, bases: bases, editedAt: at, taskIDs: Array(tasks.suffix(maxTasks - 1)),
                      olderBlobs: older, created: previous.map { wasCreated($0, chainNew: chainNew) } ?? !onMac)
        out.images = previous?.images ?? [:]
        out.restore = previous?.restore ?? false
        return out
    }

    /// 上一条记录是不是手机上新建、电脑还没收下的一篇。旧版（4340d72，记录里还没有 created 这一项）给新建的一篇记的是
    /// 空 bases：读进来 created 是 false。不认出来的话，下一次改会带上 bases、不带 new，第一份还没到电脑时电脑判「没有了」（gone）。
    /// 新版里 bases 空、又不是新建的记录只有复盘（chainNew 为 false，任务不带 bases 和 new，不受影响）
    static func wasCreated(_ previous: Record, chainNew: Bool) -> Bool {
        previous.created || (chainNew && previous.bases.isEmpty)
    }

    /// 之前的任务（不是最后那个）写进电脑的那几版也是「更早的」：文件树是它们，说明电脑还没到这次
    static func absorb(_ r: Record, earlierApplied: [String]) -> Record {
        var r = r
        for a in earlierApplied where !r.olderBlobs.contains(a) { r.olderBlobs.append(a) }
        r.olderBlobs = Array(r.olderBlobs.suffix(maxOlder))
        return r
    }

    /// 对账判成冲突：记下是哪几个任务失败（statuses：任务 id → 结果）、是不是更早那次失败的
    static func markConflict(_ r: Record, kind: String, statuses: [String: Last]) -> Record {
        var out = r
        out.state = "conflict"
        out.failKind = kind
        out.failedIDs = r.taskIDs.filter { statuses[$0]?.state == "failed" }
        // 更早的失败了、最后那个没失败：「后来的修改已经交给电脑」才说得通
        out.failedEarlier = out.failedIDs.contains { $0 != r.taskIDs.last } && !out.failedIDs.contains { $0 == r.taskIDs.last }
        // 失败的跟上次提醒的不一样了：还没提醒过这一次
        if out.failedIDs != r.failedIDs || r.state != "conflict" { out.noticed = false }
        return out
    }

    // MARK: 手机上删掉、电脑还没删的

    enum DeleteOutcome: Equatable {
        case keep      // 还藏着：删除还没做完，或者做完了文件树还没跟上
        case clear     // 电脑删了、文件树里也没有了：不用再记
        case failed    // 电脑没删成：不再藏着，任务页里标着没成功
    }

    /// 书架上先藏起来的一篇（手机上删了）什么时候不用再藏。taskID 是删除任务的 id，旧版记的没有（空串）：
    /// 旧版没记任务，只能照旧看文件树。新版只在删除任务做完、文件树里也没有这篇时才放掉——
    /// 只凭「文件树里没有」会把刚剪藏、电脑还没建出来的一篇当成已经删了，电脑建出来以后它又回到手机
    static func deleteOutcome(taskID: String, last: Last?, inTree: Bool) -> DeleteOutcome {
        if taskID.isEmpty { return inTree ? .keep : .clear }
        if last?.state == "failed" { return .failed }
        if last?.state == "done", !inTree { return .clear }
        return .keep
    }

    /// 交给电脑的任务里跟这条记录有关的几项：base/bases（复盘不带），手机上新建、还没确认电脑收下过的带 new
    /// （电脑上没有这个文件时照写，不算「没有了」：第一份可能在发件箱里被后来的修改替掉了，根本没到电脑）
    static func taskFields(_ r: Record, review: Bool) -> [String: Any] {
        var out: [String: Any] = [:]
        guard !review else { return out }
        if !r.bases.isEmpty { out["base"] = r.bases[0]; out["bases"] = r.bases }
        if r.created { out["new"] = true }
        if r.restore { out["restore"] = true }
        return out
    }

    /// 手机上新建的一篇，同步过来的文件树里这篇已经是手机交过的某一版（applied：之前任务写进电脑的那几版）：
    /// 电脑收下过了，之后就是普通的修改，不再带 new
    static func settled(_ r: Record, treeSHA: String?, applied: [String]) -> Record {
        guard r.created, let t = treeSHA, applied.contains(t) || r.olderBlobs.contains(t) || t == blobSHA(r.content) else { return r }
        var out = r
        out.created = false
        out.restore = false
        return out
    }

    // MARK: 手机上这一版电脑上没有：用户点「重新交给电脑」

    /// 这篇为什么只在手机上（没有就是 none）：
    /// - gone：待确认记录说电脑上已经没有这篇了（state gone，或者最后那个任务失败、kind 是 gone）；
    /// - stranded：别的设备删掉了，手机上这一版电脑没见过，没有待确认记录（Vault.strandedPaths，ElsewhereDeleted）；
    /// - legacyGone：旧版 App（4340d72 之前）把手机上新存的一篇当成「改电脑上已有的」交了出去（带 base），电脑上没有、判了 gone。
    ///   旧版没有待确认记录，只剩发件箱里那个失败的任务（legacyGonePaths 找出来的）
    enum Stuck: Equatable { case none, gone, stranded, legacyGone }

    static func stuck(record: Record?, stranded: Bool, legacyGone: Bool) -> Stuck {
        if let r = record {
            if r.state == "gone" || (r.state == "conflict" && r.failKind == "gone" && !r.failedEarlier) { return .gone }
            return .none   // 还在等、冲突（电脑上有另一版）、收不了：不是「电脑上没有」
        }
        if stranded { return .stranded }
        if legacyGone { return .legacyGone }
        return .none
    }

    /// 阅读页上给不给「重新交给电脑」：只有电脑能当成新的一篇收下的路径（剪藏、随手记，newDocPath）。
    /// 别的文章（整理稿、文字记录……）电脑只收带 base 的修改，交过去也收不了，不给
    static func canResend(_ s: Stuck, newDocPath: Bool) -> Bool { newDocPath && s != .none }

    /// 重新交给电脑的那条待确认记录：手机上这一版当成新的一篇（不带 bases、created，任务带 new），带 restore
    /// （电脑上这个路径有墓碑就清掉照建，同放回原处）。电脑上这个路径已经有内容不同的一篇：不覆盖，判冲突，
    /// 手机这一版存进 note-conflicts（core/remote_tasks._save_clip / _save_jotting 对 restore 的处理）。
    /// 之前报过失败的任务不再带上（不然对账时又判成没有了）；原图地址照带
    static func resent(content: String, previous: Record?, images: [String: String], now: Int64) -> Record {
        var r = Record(content: content, bases: [], editedAt: max(now, (previous?.editedAt ?? 0) + 1), taskIDs: [], created: true)
        r.images = (previous?.images ?? [:]).merging(images) { mine, _ in mine }
        r.restore = true
        return r
    }

    /// 旧版交的「改已有的一篇」被电脑判 gone、手机上又没有待确认记录的那几篇：发件箱里还留着那个失败的存，
    /// 电脑的结果是 failed、kind gone，手机上这篇还在（onPhone）。有待确认记录的归 reconcile 管，这里不算
    static func legacyGone(outbox: [(id: String, path: String)], results: [String: Last], recorded: Set<String>,
                           onPhone: Set<String>) -> Set<String> {
        var out = Set<String>()
        for item in outbox where !recorded.contains(item.path) && onPhone.contains(item.path) {
            if let r = results[item.id], r.state == "failed", r.kind == "gone" { out.insert(item.path) }
        }
        return out
    }

    /// 交出去之后记下任务
    static func submitted(_ r: Record, taskID: String) -> Record {
        var r = r
        r.taskIDs = Array((r.taskIDs + [taskID]).suffix(maxTasks))
        return r
    }

    /// 显示、下一次改的起点：有记录用记录里的
    static func display(record: Record?, disk: String?) -> String? {
        record?.content ?? disk
    }

    enum Outcome: Equatable {
        case remove            // 手机上删了
        case applied           // 电脑已经有这次的改动（是这一版、或者从这一版又往后改了）：放手
        case conflict(String)  // 电脑没收：conflict / gone / rejected
        case gone              // 电脑上没有这篇了，也没有等着的任务
        case resubmit          // 根本没交出去（交任务时出错）：再交一次
        case pending(rewrite: Bool)   // 还在等；rewrite：本地文件被盖了，要写回
    }

    /// 同步之后对一条记录。只按内容判断，不看时间：
    /// treeSHA 是同步过来的文件树里这篇的 blob sha（电脑上没有是 nil）；last 是最后那个任务的结果；
    /// held：这次同步发现电脑上少了一大批文件、先没删（可能是电脑上笔记库没挂上），这次不判「没有了」
    /// earlier：这条记录更早的那几个任务的结果。其中有失败的（比如编辑页起点过期、两份都交，先交的那份晚到被判冲突，
    /// 存进了 note-conflicts）：标成冲突、提醒，不因为后一份收下了就放手。还有在等结果的：先不放手，等都有了结果再判
    /// （state 是 nil 的是查不到结果的老任务，不等）
    static func reconcile(_ r: Record, deletedLocally: Bool, treeSHA: String?, last: Last?, held: Bool, disk: String?,
                          earlier: [Last] = []) -> Outcome {
        if deletedLocally { return .remove }
        if let f = earlier.first(where: { $0.state == "failed" }) { return .conflict(f.kind ?? "rejected") }
        let outcome = decide(r, treeSHA: treeSHA, last: last, held: held, disk: disk)
        if outcome == .applied, earlier.contains(where: { $0.state == "waiting" }) { return .pending(rewrite: disk != r.content) }
        return outcome
    }

    private static func decide(_ r: Record, treeSHA: String?, last: Last?, held: Bool, disk: String?) -> Outcome {
        if let treeSHA, treeSHA == blobSHA(r.content) { return .applied }
        if r.taskIDs.isEmpty { return .resubmit }
        if last?.state == "failed" { return .conflict(last?.kind ?? "rejected") }   // 先看失败：失败的绝不当成收下了放手
        if last?.state == "done" {
            if let a = last?.appliedSHA, treeSHA == a { return .applied }
            guard let treeSHA else { return held ? .pending(rewrite: false) : .applied }   // 收下之后电脑上又删了
            if r.olderBlobs.contains(treeSHA) { return .pending(rewrite: disk != r.content) }   // 文件树还是之前那版：还没同步到
            return .applied   // 电脑收下之后又往后改了（Mac 上划线、重新生成）：改动在它的历史里，用电脑的
        }
        let waiting = last?.state == "waiting"
        // 手机上新建的一篇电脑上本来就没有：不算「没有了」
        if treeSHA == nil, !r.bases.isEmpty, !r.created, !waiting, !held { return .gone }
        return .pending(rewrite: disk != r.content)
    }

    /// 剪藏改了名：记录挪到新名字下（每次同步都对一遍，改名记录早就见过也照样挪）。
    /// 新名字下已经有一条、内容又不一样：不按时间丢掉哪条——挪不过去的那条留在旧名字下，
    /// 等它的任务有结果（收下了就放手，失败了就提醒），不合并。内容一样的才合成一条。
    static func retarget(_ records: [String: Record], renames: [String: String]) -> [String: Record] {
        var out: [String: Record] = [:]
        for (path, r) in records where renames[path] == nil { out[path] = r }
        for path in records.keys.filter({ renames[$0] != nil }).sorted() {   // 按路径排：结果跟字典顺序无关
            let r = records[path]!, dest = renames[path]!
            if let have = out[dest] {
                if have.content == r.content {
                    var merged = have
                    merged.taskIDs = Array((have.taskIDs + r.taskIDs.filter { !have.taskIDs.contains($0) }).suffix(maxTasks))
                    out[dest] = merged
                } else {
                    out[path] = r
                }
            } else {
                out[dest] = r
            }
        }
        return out
    }

    /// 这次改完交出去时，发件箱里同一篇旧的那份能不能拿掉：这次改的起点就是它的内容才能拿掉
    static func mayDrop(pendingContent: String?, editedFrom: String?) -> Bool {
        guard let pendingContent, let editedFrom else { return false }
        return pendingContent == editedFrom
    }

    /// 电脑写在 Issue 里的状态评论：<!-- spark-status {"state": …, "applied_sha": …, "kind": …} -->，取最后一条
    static func issueStatus(_ comments: [String]) -> Last? {
        let prefix = "<!-- spark-status "
        for body in comments.reversed() where body.hasPrefix(prefix) {
            guard let end = body.range(of: " -->"),
                  let obj = try? JSONSerialization.jsonObject(with: Data(body[body.index(body.startIndex, offsetBy: prefix.count)..<end.lowerBound].utf8))
                    as? [String: Any], let state = obj["state"] as? String else { continue }
            return Last(state: state == "running" ? "waiting" : state, appliedSHA: obj["applied_sha"] as? String, kind: obj["kind"] as? String)
        }
        return nil
    }
}
