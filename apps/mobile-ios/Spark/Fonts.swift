import CoreText
import SwiftUI
import UIKit

/// 字体：App 自带的（Fonts/ 目录里的霞鹜文楷、思源黑体）启动时注册；用户自己添加的
/// （从链接或文件）存在 Application Support/Fonts，下次启动也注册。没装上的一律退回系统字体。
@Observable
final class Fonts {
    static let shared = Fonts()

    /// 自带字体的 PostScript 名。文件没打包进来时这两个名字找不到，自动退回系统字体。
    static let wenkai = "LXGWWenKai-Regular"
    static let heading = "SourceHanSansCN-Medium"

    private(set) var userFonts: [BodyFont] = []
    /// 正在下载的系统字体（PostScript 名 → 进度 0…1）
    private(set) var downloading: [String: Double] = [:]
    private(set) var downloadError: [String: String] = [:]
    private(set) var downloadTick = 0

    static var userDir: URL {
        let dir = URL.applicationSupportDirectory.appending(path: "Fonts", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    func registerAll() {
        if let bundled = Bundle.main.urls(forResourcesWithExtension: nil, subdirectory: "Fonts") {
            bundled.forEach { _ = register($0) }
        }
        for name in [Self.wenkai, Self.heading] where UIFont(name: name, size: 12) == nil {
            // 打包时文件放在资源根目录（同步文件夹会拍平目录）也认
            for ext in ["ttf", "otf"] {
                if let url = Bundle.main.url(forResource: name, withExtension: ext) { _ = register(url) }
            }
        }
        loadUserFonts()
    }

    /// 注册一个字体文件，返回里面的字体（PostScript 名，家族名）
    @discardableResult
    func register(_ url: URL) -> [(postscript: String, family: String)] {
        var error: Unmanaged<CFError>?
        CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error)
        guard let descs = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL) as? [CTFontDescriptor] else { return [] }
        return descs.compactMap { d in
            guard let ps = CTFontDescriptorCopyAttribute(d, kCTFontNameAttribute) as? String else { return nil }
            let family = CTFontDescriptorCopyAttribute(d, kCTFontDisplayNameAttribute) as? String ?? ps
            return (ps, family)
        }
    }

    private func loadUserFonts() {
        let files = (try? FileManager.default.contentsOfDirectory(at: Self.userDir, includingPropertiesForKeys: nil)) ?? []
        var out: [BodyFont] = []
        for f in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            if let first = register(f).first {
                out.append(BodyFont(id: "user:\(first.postscript)", label: first.family, fontName: first.postscript))
            }
        }
        userFonts = out
    }

    /// 把一个字体文件收进来：检查能不能用，复制到 Fonts 目录，注册。返回它的字体项。
    func install(from source: URL) throws -> BodyFont {
        guard let descs = CTFontManagerCreateFontDescriptorsFromURL(source as CFURL) as? [CTFontDescriptor],
              let first = descs.first,
              let ps = CTFontDescriptorCopyAttribute(first, kCTFontNameAttribute) as? String else {
            throw SparkError("这不是能用的字体文件（支持 .ttf、.otf）")
        }
        let dest = Self.userDir.appending(path: "\(ps).\(source.pathExtension.isEmpty ? "ttf" : source.pathExtension)")
        if !FileManager.default.fileExists(atPath: dest.path) {
            try FileManager.default.copyItem(at: source, to: dest)
        }
        register(dest)
        loadUserFonts()
        return userFonts.first { $0.fontName == ps } ?? BodyFont(id: "user:\(ps)", label: ps, fontName: ps)
    }

    func remove(_ font: BodyFont) {
        guard let name = font.fontName else { return }
        let files = (try? FileManager.default.contentsOfDirectory(at: Self.userDir, includingPropertiesForKeys: nil)) ?? []
        for f in files where f.deletingPathExtension().lastPathComponent == name {
            CTFontManagerUnregisterFontsForURL(f as CFURL, .process, nil)
            try? FileManager.default.removeItem(at: f)
        }
        loadUserFonts()
    }

    /// 让系统下载一款 iOS 可下载字体（宋体、楷体、仿宋……）。下好之后本 App 就能用，
    /// 系统会记住，App 重启后不用再下（registerAll 里会再请求一次，已下载的立刻返回）。
    func downloadSystemFont(_ name: String) {
        guard downloading[name] == nil, !Self.isAvailable(name) else { return }
        downloading[name] = 0
        downloadError[name] = nil
        let desc = CTFontDescriptorCreateWithNameAndSize(name as CFString, 0)
        CTFontDescriptorMatchFontDescriptorsWithProgressHandler([desc] as CFArray, nil) { state, info in
            let dict = info as NSDictionary
            let progress = (dict[kCTFontDescriptorMatchingPercentage] as? Double ?? 0) / 100
            let failed = state == .didFailWithError
            let done = state == .didFinish
            let err = (dict[kCTFontDescriptorMatchingError] as? NSError)?.localizedDescription
            Task { @MainActor in
                if failed {
                    Fonts.shared.downloading[name] = nil
                    Fonts.shared.downloadError[name] = err ?? "下载失败，检查一下网络"
                } else if done {
                    Fonts.shared.downloading[name] = nil
                    Fonts.shared.downloadTick += 1
                } else {
                    Fonts.shared.downloading[name] = progress
                }
            }
            return true
        }
    }

    /// 上次下好的系统字体：系统存着，但每次启动要再请求一次才能用
    func restoreSystemFonts(_ names: [String]) {
        for name in names where !Self.isAvailable(name) {
            let desc = CTFontDescriptorCreateWithNameAndSize(name as CFString, 0)
            CTFontDescriptorMatchFontDescriptorsWithProgressHandler([desc] as CFArray, nil) { state, _ in
                if state == .didFinish {
                    Task { @MainActor in Fonts.shared.downloadTick += 1 }
                }
                return true
            }
        }
    }

    static func font(_ name: String?, size: Double) -> Font {
        if let name, UIFont(name: name, size: size) != nil {
            return .custom(name, fixedSize: size)
        }
        return .system(size: size)
    }

    static func isAvailable(_ name: String?) -> Bool {
        guard let name else { return true }
        return UIFont(name: name, size: 12) != nil
    }
}
