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

        init(content: String, bases: [String], editedAt: Int64, taskIDs: [String], state: String = "pending",
             olderBlobs: [String] = [], failKind: String? = nil) {
            (self.content, self.bases, self.editedAt, self.taskIDs, self.state, self.olderBlobs, self.failKind) =
                (content, bases, editedAt, taskIDs, state, olderBlobs, failKind)
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
        }
    }

    /// 最后那个任务（这份内容的）在电脑上怎么样了。state：done / failed / waiting；nil 是不知道（没结果也不在发件箱）
    struct Last: Equatable {
        var state: String?
        var appliedSHA: String? = nil
        var kind: String? = nil
    }

    static let maxBases = 8
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

    /// 又改了一次：换掉记录。startText：这次改的起点（手机上看到的那份）；onMac：电脑上有这篇
    static func edited(previous: Record?, startText: String?, onMac: Bool, content: String, now: Int64) -> Record {
        var bases: [String]
        if let previous {
            bases = previous.bases.isEmpty ? [] : previous.bases + [sha256(previous.content)]
        } else {
            bases = onMac ? startText.map { [sha256($0)] } ?? [] : []
        }
        var seen = Set<String>()
        bases = bases.filter { seen.insert($0).inserted }
        if bases.count > maxBases { bases = [bases[0]] + bases.suffix(maxBases - 1) }   // 最早那版一直留着
        let at = max(now, (previous?.editedAt ?? 0) + 1)
        var older = previous.map { $0.olderBlobs + [blobSHA($0.content)] } ?? []
        if let startText { older.append(blobSHA(startText)) }
        var seenOlder = Set<String>()
        older = Array(older.filter { seenOlder.insert($0).inserted }.suffix(maxOlder))
        return Record(content: content, bases: bases, editedAt: at, taskIDs: Array((previous?.taskIDs ?? []).suffix(maxTasks - 1)),
                      olderBlobs: older)
    }

    /// 之前的任务（不是最后那个）写进电脑的那几版也是「更早的」：文件树是它们，说明电脑还没到这次
    static func absorb(_ r: Record, earlierApplied: [String]) -> Record {
        var r = r
        for a in earlierApplied where !r.olderBlobs.contains(a) { r.olderBlobs.append(a) }
        r.olderBlobs = Array(r.olderBlobs.suffix(maxOlder))
        return r
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
    static func reconcile(_ r: Record, deletedLocally: Bool, treeSHA: String?, last: Last?, held: Bool, disk: String?) -> Outcome {
        if deletedLocally { return .remove }
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
        if treeSHA == nil, !r.bases.isEmpty, !waiting, !held { return .gone }
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
