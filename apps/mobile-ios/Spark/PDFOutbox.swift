import CryptoKit
import Foundation

/// 手机本地收录的论文 PDF 原件（选文件、从别的 App 分享进来的）交给电脑：只走数据线，不经 GitHub。
/// 原件在手机笔记库的 Spark/论文/attachments/<内容 SHA-256 前 16 位>.pdf，记在 Vault.generatedPending 里。
/// 还在 generatedPending 里的，拷一份副本到 App Group 容器的 Library/Spark/pdfs/<名字>.pdf
/// （真机上 devicectl 只能读 App Group 的 Library/），电脑整个同步时拉走、校验后收下（core/cable_sync.receive_pdfs）。
/// 电脑带回的清单里出现这个文件，Vault.keepGenerated 把它从 generatedPending 放手，下一次对账就清掉副本。
/// 传了一半、电脑没收、同步断了：generatedPending 不变，副本留着，下次同步再交。手机笔记库里的原件这里从不动。
/// 论文 attachments/ 下的 PDF 电脑也不推到 GitHub（core/github_sync.EXCLUDE）：手机经 GitHub 同步时文件树里看不到它们，
/// 按 cableOnly 保留上次知道的（keepCableOnly），不当成「电脑上删了」。
/// 手机上原件没了、副本还在（副本是唯一一份）：keepGenerated 先把副本拷回原处，不放手（release 给 .restore）。
/// 电脑拒收（同步结果 pdfs 里 state 是 failed）：原件内容哈希对得上文件名的，删掉副本重拷一次；同一路径只重拷一次（recopyCandidates）。
/// 判断写成纯函数，方便单独编译测试（tests/test_cable_sync.py）。
enum PDFOutbox {
    /// 论文文件夹的 attachments/（同 PathRules.paperFolder、电脑 core/clips._RE_PDF_KEY）
    static let prefix = "Spark/论文/attachments/"

    /// 发件箱 pdfs/ 里一份副本的文件名；不是本地论文 PDF 的路径给 nil。只认 16 位小写十六进制加 .pdf（同电脑的校验）
    static func outboxName(_ path: String) -> String? {
        guard path.hasPrefix(prefix) else { return nil }
        let name = String(path.dropFirst(prefix.count))
        guard name.hasSuffix(".pdf") else { return nil }
        let stem = name.dropLast(4)
        guard stem.count == 16, stem.allSatisfy({ "0123456789abcdef".contains($0) }) else { return nil }
        return name
    }

    /// GitHub 上没有的路径：论文 attachments/ 下直接的 .pdf（电脑 github_sync.EXCLUDE 里的 /Spark/论文/attachments/*.pdf）。
    /// 扩展名不分大小写：Mac 上的 git 仓库默认不分大小写（core.ignorecase），.PDF 也排除了；多留不会删错
    static func cableOnly(_ path: String) -> Bool {
        guard path.hasPrefix(prefix) else { return false }
        let name = path.dropFirst(prefix.count)
        return !name.contains("/") && name.lowercased().hasSuffix(".pdf") && name.count > 4
    }

    /// 经 GitHub 同步时：上次文件树里有、这次 GitHub 文件树里没有的 cableOnly 路径照旧留着（GitHub 本来就看不到它们）。
    /// 在 keepGenerated 之后调：还在 generatedPending 里的已经放回去了，这里不会让它们被当成「电脑上有了」而放手
    static func keepCableOnly<T>(old: [String: T], into new: inout [String: T]) {
        for (path, item) in old where new[path] == nil && cableOnly(path) { new[path] = item }
    }

    enum Release: Equatable { case release, keep, restore }

    /// keepGenerated 里 generatedPending 的一个路径怎么办：
    /// - 电脑的文件树里有了：放手（.release）；
    /// - 手机上有原件：留着（.keep）；
    /// - 原件没了、是待交的论文 PDF、发件箱里有副本：副本是唯一一份，拷回原处、不放手（.restore）；
    /// - 都没有：照旧放手。
    static func release(path: String, inTree: Bool, onPhone: Bool, hasCopy: Bool) -> Release {
        if inTree { return .release }
        if onPhone { return .keep }
        return outboxName(path) != nil && hasCopy ? .restore : .release
    }

    /// 电脑拒收的路径里，这次该算一下原件哈希、对得上就重拷副本的：还在 generatedPending、是待交的论文 PDF、
    /// 还没重拷过（tried 里没有）。同一路径连续被拒只重拷一次：内容没变，再拷也一样
    static func recopyCandidates(rejected: [String], pending: Set<String>, tried: Set<String>) -> [String] {
        Array(Set(rejected.filter { pending.contains($0) && outboxName($0) != nil && !tried.contains($0) })).sorted()
    }

    /// 原件内容的 SHA-256（十六进制）前 16 位跟文件名对得上没有。对不上说明原件本身不对，重拷副本也没用
    static func nameMatches(_ path: String, sha256 hex: String) -> Bool {
        guard let name = outboxName(path) else { return false }
        return hex.count >= 16 && hex.lowercased().prefix(16) == name.prefix(16)
    }

    /// 文件内容的 SHA-256（十六进制），一块一块读，50MB 也不整份进内存。读不了给 nil
    static func sha256File(_ url: URL) -> String? {
        guard let h = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? h.close() }
        var hasher = SHA256()
        while true {
            do {
                // 读到头时给 nil 或空的 Data（两种都是读完了）；读出错才算读不了
                guard let chunk = try h.read(upToCount: 1 << 20), !chunk.isEmpty else { break }
                hasher.update(data: chunk)
            } catch {
                return nil
            }
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// 这一轮对账：copy 要拷进发件箱的（库内路径），drop 要从发件箱去掉的（发件箱里的文件名）。
    /// - 该留在发件箱里的：还在 generatedPending（电脑清单里还没有）的本地论文 PDF。手机上原件没了也留着副本：那时副本是唯一一份；
    /// - 要拷的：该留的里面发件箱还没有、手机上有原件的；
    /// - 别的都去掉：离开了 generatedPending 的（电脑收下了、彻底删了、断开连接），以及拷到一半留下的临时文件（.<名字>.partial）。
    /// 只看 generatedPending 和文件在不在，不看时钟。
    static func plan(pending: Set<String>, onPhone: (String) -> Bool, inOutbox: Set<String>) -> (copy: [String], drop: [String]) {
        var wanted: [String: String] = [:]   // 文件名 → 库内路径
        for path in pending {
            if let name = outboxName(path) { wanted[name] = path }
        }
        let copy = wanted.filter { !inOutbox.contains($0.key) && onPhone($0.value) }.map(\.value).sorted()
        let drop = inOutbox.filter { wanted[$0] == nil }.sorted()
        return (copy, drop)
    }
}
