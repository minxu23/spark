import Foundation

/// 笔记库里一个路径是什么、能做什么：只看路径字符串，不碰文件和界面。规则只写在这里，
/// Clips.isClip、Jottings.isJotting、DocEdit.annotatable、DocEdit.editable、Vault.canDelete 都转到这里。
/// tests/test_reader_features.py 用 swiftc 把它和 ReaderPlace、ReaderFeatures 一起单独编出来核对；
/// tests/test_jottings.py 编 Jottings.swift 时也带上它。
enum PathRules {
    // 文件夹。各处原来的常量（Clipper.folder、Clips.indexName、Jottings.folder、Library 的几个目录）
    // 跟这里的必须一样，test_reader_features.py 逐个核对
    static let clipFolder = "Spark/剪藏"
    static let paperFolder = "Spark/论文"
    /// 剪藏式文件夹：剪藏和论文，文件格式一样，各有自己的索引.md、attachments/、.renames.json（电脑 core/clips.CLIP_DIRS）
    static let clipFolders = [clipFolder, paperFolder]
    static let clipIndexName = "索引.md"
    static let jottingFolder = "Spark/笔记"
    static let sparkDir = "Spark/"
    static let trackDir = "Spark/信息跟进/"
    static let reportsDir = "output/"
    static let reviewsDir = "output/复盘/"

    /// 剪藏式文档所在的文件夹：Spark/剪藏/ 或 Spark/论文/ 下直接的一篇 .md（索引、点开头的除外）；不是返回 nil
    /// （电脑 core/clips.clip_folder 同一条，tests/test_clip_paths.py 两边对一张表）
    static func clipFolderOf(_ path: String) -> String? {
        guard path.hasSuffix(".md") else { return nil }
        for folder in clipFolders where path.hasPrefix(folder + "/") {
            let rest = path.dropFirst(folder.count + 1)
            return !rest.contains("/") && rest != clipIndexName && !rest.hasPrefix(".") ? folder : nil
        }
        return nil
    }

    /// 剪藏式文档：剪藏和论文。改名、删除、编辑、图片、待确认记录这些都按它
    static func isClip(_ path: String) -> Bool { clipFolderOf(path) != nil }

    /// 论文：Spark/论文/ 下直接的一篇 .md。只用在要分开的地方：书架归属、种类名、索引标题、搜索和标签的类型
    static func isPaper(_ path: String) -> Bool { clipFolderOf(path) == paperFolder }

    /// 剪藏式文件夹的种类名（就是文件夹名）：剪藏、论文
    static func kindName(_ folder: String) -> String { (folder as NSString).lastPathComponent }

    /// 剪藏式文件夹 attachments/ 下的一张图（电脑 core/clips._RE_IMAGE_KEY）
    static func isClipImageKey(_ key: String) -> Bool {
        clipFolders.contains { folder in
            guard key.hasPrefix(folder + "/attachments/") else { return false }
            let name = key.dropFirst(folder.count + "/attachments/".count).split(separator: ".", omittingEmptySubsequences: false)
            return name.count == 2 && name[0].count == 16 && name[0].allSatisfy { "0123456789abcdef".contains($0) }
                && ["jpg", "png", "gif", "webp"].contains(String(name[1]))
        }
    }

    /// 随手记：Spark/笔记/ 下直接的一篇 .md
    static func isJotting(_ path: String) -> Bool {
        guard path.hasPrefix(jottingFolder + "/"), path.hasSuffix(".md"), !path.contains("\\") else { return false }
        let rest = path.dropFirst(jottingFolder.count + 1)
        return !rest.isEmpty && !rest.contains("/") && !rest.hasPrefix(".")
    }

    /// 复盘（output/复盘/ 下的 .md，含更深的）
    static func isReview(_ path: String) -> Bool {
        path.hasPrefix(reviewsDir) && path.hasSuffix(".md")
    }

    /// 能在手机上改名、删的：剪藏（含论文）和复盘
    static func editable(_ path: String) -> Bool {
        isClip(path) || isReview(path)
    }

    /// 能做笔记（划线、批注、读后感）的：笔记库 Spark/ 和 output/ 下所有给人读的 Markdown——剪藏、整理稿、
    /// 文字记录、信息跟进的原文、小结、会议资料、报告、复盘。新加的资料类型放在这两个目录下就自动能做笔记，
    /// 不用逐类开。剪藏索引、摘录、节目 / 会议文件夹的总览页这类生成的列表页除外。电脑上 core/annotations.annotatable 同一条规则
    static func annotatable(_ path: String) -> Bool {
        let parts = path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard path.hasSuffix(".md"), parts.count >= 2, ["Spark", "output"].contains(parts[0]),
              !parts.contains(where: { $0.isEmpty || $0.hasPrefix(".") }) else { return false }
        let name = parts[parts.count - 1]
        if ["索引.md", "摘录.md"].contains(name) { return false }
        return !(parts.count >= 3 && name == parts[parts.count - 2] + ".md")
    }

    /// 手机上能删的：复盘（output/复盘/ 下直接的一篇）、剪藏（含论文）和随手记
    static func canDelete(_ path: String) -> Bool {
        if isClip(path) || isJotting(path) { return true }
        return path.hasPrefix(reviewsDir) && path.hasSuffix(".md") && !path.dropFirst(reviewsDir.count).contains("/")
    }
}
