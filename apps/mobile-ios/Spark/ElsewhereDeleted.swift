import Foundation

/// 手机上新建、交到电脑以后在别的设备上删掉的文档。手机自己一直等不到「电脑上有了」（generatedPending 靠这个放手），
/// 就一直留在书架上。两层：
/// 1. 交上去的存电脑已经收下（任务 done、手机上这份就是交上去的那份或电脑写进去的那版）：不再靠 generatedPending 保护，
///    之后电脑文件树里没有它，就按「电脑上删了」处理（released）。
/// 2. 已经卡住、证据在手机上没了的：电脑在数据线清单里带上墓碑（.spark-cable.json 的 deleted：{路径: [删掉的各版内容的 SHA-256]}，
///    core/cable_sync.deleted_versions）。只在手机上有、电脑文件树里没有的文件，指纹在墓碑里、没有待确认记录、发件箱里没有它的任务，
///    就是别的设备删掉的同一份：移进手机废纸篓（decide 的 trash）。指纹不在里面（手机上有电脑没见过的版本）：不动，标「没进电脑」（stranded）。
/// 只看内容和任务结果，不看时间。清单里没有 deleted（旧版电脑）：什么都不判，跟以前一样
enum ElsewhereDeleted {
    /// 电脑确认收下之后，手机上这份是不是就是交上去的（或电脑写进去的那版，blob sha 对得上）。
    /// 文件树里没有这篇（treeHas 为 false）时，只在刚收到一份新的数据线清单时放手：cableTree（这份文件树就是新清单）且
    /// cableTask（任务结果是数据线带回来的，不是 GitHub Issue）。数据线上任何一份新清单都在电脑处理完这个存之后才生成，
    /// 任务 done 而新清单里没有它，就是收下后又没了。只来了任务结果、清单没变时不放手，等新清单；GitHub 的任务状态
    /// 可能先于文件树更新，GitHub 那条路也不放手（等文件树里出现它，keepGenerated 照旧放手）。不然放手之后的一次
    /// GitHub 同步会把还没推上去的新文档从手机上暂时删掉
    static func released(disk: String?, submitted: String, appliedBlob: String?, blobOf: (String) -> String,
                         treeHas: Bool = false, cableTree: Bool = true, cableTask: Bool = true) -> Bool {
        guard treeHas || (cableTree && cableTask) else { return false }
        guard let disk else { return false }
        if disk == submitted { return true }
        if let appliedBlob, blobOf(disk) == appliedBlob { return true }
        return false
    }

    /// 彻底删除（点删除、清空废纸篓、到期自动清）时要不要交 purge_images 给电脑释放图片：
    /// 电脑没删成的不交（文章还在）；在别的设备上删掉的也不交——墓碑里的图片记录按路径只有一份，
    /// 只有删掉这篇的那台设备负责释放，这台先交会把那台废纸篓里文章的图片保护提前放掉
    static func sendsPurge(deleteFailed: Bool, elsewhere: Bool) -> Bool { !deleteFailed && !elsewhere }

    /// 废纸篓里已经有同一份（路径相同、base 即内容的 SHA-256 相同、在别的设备上删掉的）：不再加一条。
    /// 写完废纸篓、删本地文件之前 App 被杀，下次收清单时只把剩下的删文件、清状态做完
    static func alreadyTrashed(_ items: [(path: String, base: String?, elsewhere: Bool)], path: String, base: String) -> Bool {
        items.contains { $0.elsewhere && $0.path == path && $0.base == base }
    }

    struct Decision: Equatable {
        var trash: [String] = []
        var stranded: [String] = []
    }

    /// onlyPhone：只在手机上有、电脑文件树里没有的文件 → 手机上那份内容的 SHA-256（读不出来是 nil）。
    /// busy：有待确认记录、发件箱里有它的任务、手机上删了还没交的——这些一律不动
    static func decide(onlyPhone: [String: String?], deleted: [String: [String]], busy: Set<String>) -> Decision {
        var out = Decision()
        for path in onlyPhone.keys.sorted() where !busy.contains(path) {
            guard let tomb = deleted[path] else { continue }   // 电脑上没有墓碑：不知道，照旧
            if let sha = onlyPhone[path] ?? nil, tomb.contains(sha) { out.trash.append(path) } else { out.stranded.append(path) }
        }
        return out
    }
}
