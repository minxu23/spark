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

struct Palette: Equatable {
    var paper: Color, paper2: Color, ink: Color, ink2: Color, accent: Color, rule: Color, chip: Color
    var isDark: Bool

    static let parchment = Palette(paper: .hex(0xf5f4ed), paper2: .hex(0xefeee5), ink: .hex(0x1f1d18), ink2: .hex(0x6b665b),
                                   accent: .hex(0x1b365d), rule: .hex(0xd4d1c5), chip: .hex(0xe6e3d7), isDark: false)
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
    case summit, series, track, report
    var id: String { rawValue }
    var label: String {
        switch self {
        case .summit: "会议"
        case .series: "播客"
        case .track: "信息跟进"
        case .report: "报告"
        }
    }
    var color: Color {
        switch self {
        case .summit: .hex(0x6a4bb5)
        case .series: .hex(0x2563a8)
        case .track: .hex(0xb4531c)
        case .report: .hex(0x2f7d4f)
        }
    }
}

// MARK: - 阅读偏好

/// 正文字体。标题、小标题固定用思源黑体（没装就用系统黑体）。
struct BodyFont: Identifiable, Hashable {
    let id: String          // wenkai / sans / songti / kaiti / fangsong / user:<PostScript 名>
    let label: String
    let fontName: String?   // nil = 系统字体

    static let builtins: [BodyFont] = [
        BodyFont(id: "wenkai", label: "霞鹜文楷", fontName: Fonts.wenkai),
        BodyFont(id: "sans", label: "思源黑体", fontName: Fonts.heading),
        BodyFont(id: "songti", label: "宋体", fontName: "STSongti-SC-Regular"),
        BodyFont(id: "kaiti", label: "楷体", fontName: "STKaitiSC-Regular"),
        BodyFont(id: "fangsong", label: "仿宋", fontName: "STFangsong"),
    ]
}

@Observable
final class ReadPrefs {
    var theme: ReadTheme { didSet { save() } }
    var bodyFontID: String { didSet { save() } }
    var size: Double { didSet { save() } }
    static let sizes: ClosedRange<Double> = 15...23

    init() {
        let d = UserDefaults.standard
        theme = ReadTheme(rawValue: d.string(forKey: "read.theme") ?? "") ?? .auto
        bodyFontID = d.string(forKey: "read.font") ?? "wenkai"
        let s = d.double(forKey: "read.size")
        size = s == 0 ? 17 : min(max(s, Self.sizes.lowerBound), Self.sizes.upperBound)
    }

    private func save() {
        let d = UserDefaults.standard
        d.set(theme.rawValue, forKey: "read.theme")
        d.set(bodyFontID, forKey: "read.font")
        d.set(size, forKey: "read.size")
    }

    /// iOS 默认不带宋体、楷体、仿宋（要在系统里另外下载），没装的不列出来；
    /// 自带的两款即使这次没打包进来也列着，标「未安装」
    var allFonts: [BodyFont] {
        BodyFont.builtins.filter { ["wenkai", "sans"].contains($0.id) || Fonts.isAvailable($0.fontName) }
            + Fonts.shared.userFonts
    }

    var bodyFont: BodyFont { allFonts.first { $0.id == bodyFontID } ?? BodyFont.builtins[0] }

    func body(_ scale: Double = 1) -> Font { Fonts.font(bodyFont.fontName, size: size * scale) }
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
