import SwiftUI

extension View {
    /// 样例库模式（Fixture）：状态栏区域里一个很小的「样例库」标记，截图里认得出来，不参与布局、不挡内容、不接点击。
    /// @AppStorage 也明确落到样例库的 suite。平时（以及真机、Release 构建）原样返回
    @ViewBuilder
    func fixtureChrome() -> some View {
        #if DEBUG && targetEnvironment(simulator)
        if Fixture.isOn { modifier(FixtureChrome()) } else { self }
        #else
        self
        #endif
    }
}

#if DEBUG && targetEnvironment(simulator)
private struct FixtureChrome: ViewModifier {
    func body(content: Content) -> some View {
        content
            .defaultAppStorage(Fixture.defaults)
            // 零大小、隐藏的一个视图：拿到所在的窗口场景后，在场景上另开一个浮层窗口画标记（见 FixtureOverlay）
            .background(FixtureOverlayInstaller().frame(width: 0, height: 0).allowsHitTesting(false))
    }
}

/// 找到 App 窗口所在的场景，给它装一次浮层窗口
private struct FixtureOverlayInstaller: UIViewRepresentable {
    func makeUIView(context: Context) -> SceneProbe { SceneProbe() }
    func updateUIView(_ view: SceneProbe, context: Context) {}

    final class SceneProbe: UIView {
        override func didMoveToWindow() {
            super.didMoveToWindow()
            isHidden = true
            isUserInteractionEnabled = false
            if let window, let scene = window.windowScene { FixtureOverlay.install(on: scene, below: window) }
        }
    }
}

/// 样例库标记的浮层：场景上单独一个窗口，层级在弹出的 sheet、全屏页面之上，所以这些页面上也看得到。
/// 窗口不接点击（isUserInteractionEnabled = false，点击落到下面的 App 窗口），不参与 App 的布局、不改任何安全区。
/// 只画在状态栏区域里：
/// - 屏幕最上沿一条 2pt 的橙色细线；
/// - 一行很小的橙色「样例库」：有刘海或灵动岛的 iPhone 放在左边「耳朵」的下半截（时间下面、状态栏底边之上），
///   没有的（iPad 等）放在状态栏正中（那里没有系统图标）；状态栏太矮放不下字时只留细线。
/// 没有状态栏（iPhone 横屏）时：细线竖着贴在刘海那一侧的屏幕边上（那一侧的安全区里没有内容）；两侧都没有安全区就只在最上沿留细线
@MainActor
enum FixtureOverlay {
    private static var windows: [ObjectIdentifier: UIWindow] = [:]

    static func install(on scene: UIWindowScene, below appWindow: UIWindow) {
        let key = ObjectIdentifier(scene)
        guard windows[key] == nil else { return }
        let w = UIWindow(windowScene: scene)
        w.windowLevel = .alert + 1
        w.isUserInteractionEnabled = false
        w.backgroundColor = .clear
        let vc = OverlayController()
        vc.below = appWindow
        w.rootViewController = vc
        w.isHidden = false   // 不 makeKey：键盘、焦点留在 App 窗口
        windows[key] = w
    }

    final class OverlayController: UIViewController {
        weak var below: UIWindow?
        override func loadView() { view = OverlayView() }
        // 状态栏的样式、显示与否交给下面的 App 窗口决定，这个浮层不插手
        override var childForStatusBarStyle: UIViewController? { below?.rootViewController }
        override var childForStatusBarHidden: UIViewController? { below?.rootViewController }
        override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
            below?.rootViewController?.supportedInterfaceOrientations ?? .all
        }
        override func viewWillTransition(to size: CGSize, with coordinator: any UIViewControllerTransitionCoordinator) {
            super.viewWillTransition(to: size, with: coordinator)
            coordinator.animate(alongsideTransition: nil) { [weak self] _ in self?.view.setNeedsLayout() }
        }
    }

    final class OverlayView: UIView {
        private let line = UIView()
        private let label = UILabel()
        private static let orange = UIColor(red: 0.85, green: 0.38, blue: 0.0, alpha: 1)

        override init(frame: CGRect) {
            super.init(frame: frame)
            backgroundColor = .clear
            isUserInteractionEnabled = false
            line.backgroundColor = Self.orange
            label.text = Fixture.problem == nil ? Fixture.badge : "\(Fixture.badge)·拷贝失败"
            label.font = .systemFont(ofSize: 9, weight: .bold)
            label.textColor = Self.orange
            label.accessibilityIdentifier = "fixture-badge"
            label.accessibilityLabel = Fixture.problem.map { "\(Fixture.badge)：\($0)" } ?? Fixture.badge
            addSubview(line)
            addSubview(label)
        }

        required init?(coder: NSCoder) { fatalError() }

        override func safeAreaInsetsDidChange() {
            super.safeAreaInsetsDidChange()
            setNeedsLayout()
        }

        override func layoutSubviews() {
            super.layoutSubviews()
            let w = bounds.width
            let bar = window?.windowScene?.statusBarManager?.statusBarFrame.height ?? 0
            let inset = safeAreaInsets
            label.sizeToFit()
            let lh = label.bounds.height, lw = label.bounds.width
            if bar > 0 {
                line.frame = CGRect(x: 0, y: 0, width: w, height: 2)
                let y = bar - lh - 1
                // 有刘海、灵动岛的 iPhone：竖屏时顶部安全区明显比普通状态栏高
                let cutout = inset.top >= 40 && traitCollection.userInterfaceIdiom == .phone
                let x = cutout ? w * 0.17 : w / 2
                label.center = CGPoint(x: x, y: y + lh / 2)
                label.isHidden = y < 2 || lw > w * 0.3   // 状态栏太矮或屏幕太窄：只留细线
            } else if max(inset.left, inset.right) >= 4 {
                line.frame = CGRect(x: inset.left >= inset.right ? 0 : w - 2, y: 0, width: 2, height: bounds.height)
                label.isHidden = true
            } else {
                line.frame = CGRect(x: 0, y: 0, width: w, height: 2)
                label.isHidden = true
            }
        }
    }
}

/// 模拟器 DEBUG 上拦下时的最小 App：只有说明页，不建 Vault、不读存储和钥匙串、不联网（SparkMain 分流过来）
struct SimulatorGuardApp: App {
    var body: some Scene {
        WindowGroup { SimulatorGuardView() }
    }
}
#endif
