import SwiftUI
import UIKit

/// 阅读配色：跟电脑上阅读页（static/common/read-prefs.css）同一套，整个 App 的底色也跟着它走。
enum ReadTheme: String, CaseIterable, Identifiable {
    case auto, parchment, white, sepia, gray, night
    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: "跟随系统"
        case .parchment: "羊皮纸"
        case .white: "白"
        case .sepia: "米黄"
        case .gray: "灰"
        case .night: "夜间"
        }
    }

    /// 跟随系统时，深色模式下用的那一套
    func resolved(_ scheme: ColorScheme) -> Palette {
        switch self {
        case .auto: scheme == .dark ? .parchmentDark : .parchment
        case .parchment: .parchment
        case .white: .white
        case .sepia: .sepia
        case .gray: .gray
        case .night: .night
        }
    }
}

struct Palette: Hashable {
    var paper: Color, paper2: Color, ink: Color, ink2: Color, accent: Color, rule: Color, chip: Color
    var isDark: Bool

    static let parchment = Palette(paper: .hex(0xf5f4ed), paper2: .hex(0xefeee5), ink: .hex(0x1f1d18), ink2: .hex(0x6b665b),
                                   accent: .hex(0x1e4a8c), rule: .hex(0xd4d1c5), chip: .hex(0xe6e3d7), isDark: false)
    static let parchmentDark = Palette(paper: .hex(0x1c1b18), paper2: .hex(0x24231f), ink: .hex(0xe8e4d8), ink2: .hex(0xa39e90),
                                       accent: .hex(0x9fb6d8), rule: .hex(0x3a3831), chip: .hex(0x2e2c27), isDark: true)
    static let white = Palette(paper: .hex(0xffffff), paper2: .hex(0xf4f4f2), ink: .hex(0x1b1b1b), ink2: .hex(0x5f5f5f),
                               accent: .hex(0x1a56b0), rule: .hex(0xdcdcdc), chip: .hex(0xececea), isDark: false)
    static let sepia = Palette(paper: .hex(0xf8f0e0), paper2: .hex(0xefe4cc), ink: .hex(0x4b3a2a), ink2: .hex(0x7a6650),
                               accent: .hex(0x8b4a1c), rule: .hex(0xdccdb0), chip: .hex(0xe9dcc0), isDark: false)
    static let gray = Palette(paper: .hex(0x4a4a4f), paper2: .hex(0x55555b), ink: .hex(0xececee), ink2: .hex(0xbdbdc3),
                              accent: .hex(0xb3cdf7), rule: .hex(0x66666d), chip: .hex(0x5d5d63), isDark: true)
    static let night = Palette(paper: .hex(0x121212), paper2: .hex(0x1c1c1c), ink: .hex(0xd2d2d2), ink2: .hex(0x8f8f8f),
                               accent: .hex(0x8ab4f8), rule: .hex(0x2c2c2c), chip: .hex(0x262626), isDark: true)
}

extension Color {
    static func hex(_ v: UInt32) -> Color {
        Color(red: Double((v >> 16) & 0xff) / 255, green: Double((v >> 8) & 0xff) / 255, blue: Double(v & 0xff) / 255)
    }
}

/// 类别的小圆点：跟网页版各工具的主题色一致
enum Category: String, CaseIterable, Identifiable {
    case summit, series, track, clip, report
    var id: String { rawValue }
    var label: String {
        switch self {
        case .summit: "会议"
        case .series: "播客"
        case .track: "信息跟进"
        case .clip: "剪藏"
        case .report: "报告"
        }
    }
    var color: Color {
        switch self {
        case .summit: .hex(0x6a4bb5)
        case .series: .hex(0x2563a8)
        case .track: .hex(0xb4531c)
        case .clip: .hex(0xa8436b)
        case .report: .hex(0x2f7d4f)
        }
    }
}

// MARK: - 阅读偏好

/// 正文、标题可选的字体（标题默认思源黑体，没装就用系统黑体）。
struct BodyFont: Identifiable, Hashable {
    let id: String          // wenkai / sans / pingfang / songti … / user:<PostScript 名>
    let label: String
    let fontName: String?   // nil = 系统字体
    /// iOS 的「可下载字体」：系统有授权，第一次用时由系统从苹果服务器下载，不占 App 的空间
    var downloadable = false

    static let builtins: [BodyFont] = [
        BodyFont(id: "wenkai", label: "霞鹜文楷", fontName: Fonts.wenkai),
        BodyFont(id: "sans", label: "思源黑体", fontName: Fonts.heading),
        BodyFont(id: "pingfang", label: "苹方（系统黑体）", fontName: "PingFangSC-Regular"),
        BodyFont(id: "songti", label: "宋体", fontName: "STSongti-SC-Regular", downloadable: true),
        BodyFont(id: "kaiti", label: "楷体", fontName: "STKaitiSC-Regular", downloadable: true),
        BodyFont(id: "fangsong", label: "仿宋", fontName: "STFangsong", downloadable: true),
        BodyFont(id: "yuanti", label: "圆体", fontName: "STYuanti-SC-Regular", downloadable: true),
        BodyFont(id: "lantinghei", label: "兰亭黑", fontName: "FZLTXHK--GBK1-0", downloadable: true),
        BodyFont(id: "libian", label: "隶变", fontName: "STLibianSC-Regular", downloadable: true),
        BodyFont(id: "weibei", label: "魏碑", fontName: "Weibei-SC-Bold", downloadable: true),
        BodyFont(id: "xingkai", label: "行楷", fontName: "STXingkaiSC-Light", downloadable: true),
    ]
}

@Observable
final class ReadPrefs {
    var theme: ReadTheme { didSet { save() } }
    var bodyFontID: String { didSet { save() } }
    var size: Double { didSet { save() } }
    /// 中英对照整理稿显示哪一边
    var speechLang: Markdown.SpeechLang { didSet { save() } }
    /// 标题（篇名、章节标题、小标题）用的字体、颜色、大小
    var headingFontID: String { didSet { save() } }
    var headingColor: HeadingColor { didSet { save() } }
    var headingSize: HeadingSize { didSet { save() } }
    static let sizes: ClosedRange<Double> = 15...23

    enum HeadingColor: String, CaseIterable, Identifiable {
        case accent, ink
        var id: String { rawValue }
        var label: String { self == .accent ? "强调色" : "和正文同色" }
    }

    enum HeadingSize: String, CaseIterable, Identifiable {
        case small, standard, large
        var id: String { rawValue }
        var label: String { ["small": "小", "standard": "标准", "large": "大"][rawValue]! }
        /// 篇名、章节标题（##）、小标题（### 和整行加粗的）相对正文字号的倍数
        var scales: (title: Double, section: Double, sub: Double) {
            switch self {
            case .small: (1.15, 1.08, 1.0)
            case .standard: (1.3, 1.2, 1.08)
            case .large: (1.5, 1.35, 1.18)
            }
        }
    }

    init() {
        let d = UserDefaults.standard
        theme = ReadTheme(rawValue: d.string(forKey: "read.theme") ?? "") ?? .auto
        bodyFontID = d.string(forKey: "read.font") ?? "wenkai"
        let s = d.double(forKey: "read.size")
        size = s == 0 ? 17 : min(max(s, Self.sizes.lowerBound), Self.sizes.upperBound)
        speechLang = Markdown.SpeechLang(rawValue: d.string(forKey: "read.speechLang") ?? "") ?? .zh
        headingFontID = d.string(forKey: "read.headingFont") ?? "sans"
        headingColor = HeadingColor(rawValue: d.string(forKey: "read.headingColor") ?? "") ?? .accent
        headingSize = HeadingSize(rawValue: d.string(forKey: "read.headingSize") ?? "") ?? .standard
    }

    private func save() {
        let d = UserDefaults.standard
        d.set(theme.rawValue, forKey: "read.theme")
        d.set(bodyFontID, forKey: "read.font")
        d.set(size, forKey: "read.size")
        d.set(speechLang.rawValue, forKey: "read.speechLang")
        d.set(headingFontID, forKey: "read.headingFont")
        d.set(headingColor.rawValue, forKey: "read.headingColor")
        d.set(headingSize.rawValue, forKey: "read.headingSize")
    }

    /// iOS 默认不带宋体、楷体、仿宋（要在系统里另外下载），没装的不列出来；
    /// 自带的两款即使这次没打包进来也列着，标「未安装」
    /// 自带的两款、苹方，加上系统可下载的宋体、楷体、仿宋……（没下载的点了会下载）和自己添加的
    var allFonts: [BodyFont] {
        // 读一下下载状态，下载完了列表跟着刷新
        _ = Fonts.shared.downloadTick
        return BodyFont.builtins + Fonts.shared.userFonts
    }

    var bodyFont: BodyFont { allFonts.first { $0.id == bodyFontID } ?? BodyFont.builtins[0] }

    func body(_ scale: Double = 1) -> Font { Fonts.font(bodyFont.fontName, size: size * scale) }

    var headingFontChoice: BodyFont { allFonts.first { $0.id == headingFontID } ?? BodyFont.builtins[1] }
    func headingFont(_ size: Double) -> Font { Fonts.font(headingFontChoice.fontName, size: size).weight(.semibold) }
    /// 正文里的标题：## 及以上算章节标题，### 及以下（含整行加粗的小标题）算小标题
    func heading(_ level: Int) -> Font {
        let s = headingSize.scales
        return headingFont(size * (level <= 2 ? s.section : s.sub))
    }
    /// 阅读页顶上的篇名
    var titleFont: Font { headingFont(size * headingSize.scales.title) }
    func headingTint(_ p: Palette) -> Color { headingColor == .accent ? p.accent : p.ink }
    var lineSpacing: Double { size * 0.5 }
}

// MARK: - 排版辅助

extension Font {
    static func heading(_ size: Double, weight: Font.Weight = .semibold) -> Font {
        Fonts.font(Fonts.heading, size: size).weight(weight)
    }
}

struct PaletteKey: EnvironmentKey { static let defaultValue = Palette.parchment }
extension EnvironmentValues {
    var palette: Palette {
        get { self[PaletteKey.self] }
        set { self[PaletteKey.self] = newValue }
    }
}

/// 根视图套一层：按阅读配色 + 系统深浅色算出当前的一套颜色，往下传
struct Themed: ViewModifier {
    @Environment(ReadPrefs.self) private var prefs
    @Environment(\.colorScheme) private var scheme
    func body(content: Content) -> some View {
        let p = prefs.theme.resolved(scheme)
        content
            .environment(\.palette, p)
            .tint(p.accent)
            .preferredColorScheme(prefs.theme == .auto ? nil : (p.isDark ? .dark : .light))
    }
}

extension View {
    /// iPad 上把滚动内容排成居中的一栏：铺满宽屏一行太长，读着累。窄屏（iPhone、分屏）不受影响
    func readableColumn(_ maxWidth: CGFloat = 720) -> some View { modifier(ReadableColumn(maxWidth: maxWidth)) }
}

private struct ReadableColumn: ViewModifier {
    let maxWidth: CGFloat
    @State private var width: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .contentMargins(.horizontal, max(0, (width - maxWidth) / 2), for: .scrollContent)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }
}
