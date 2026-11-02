import Foundation
import Network

/// 现在用的是不是流量：蜂窝网络、个人热点（系统标成 expensive），或打开了「低数据模式」（constrained）。
/// 「只在 Wi-Fi 下下载整理稿和文字记录」靠它判断。
@MainActor
@Observable
final class NetMonitor {
    static let shared = NetMonitor()

    private(set) var expensive = false
    private let monitor = NWPathMonitor()

    private init() {
        monitor.pathUpdateHandler = { path in
            let costly = path.isExpensive || path.isConstrained
            Task { @MainActor in NetMonitor.shared.expensive = costly }
        }
        monitor.start(queue: DispatchQueue(label: "spark.net"))
    }

    #if DEBUG
    /// 模拟器一直是「Wi-Fi」，开发时在「我的」里打开这个假装在用流量
    var pretendCellular = UserDefaults.standard.bool(forKey: "debug.pretendCellular") {
        didSet { UserDefaults.standard.set(pretendCellular, forKey: "debug.pretendCellular") }
    }
    #endif

    var onCellular: Bool {
        #if DEBUG
        if pretendCellular { return true }
        #endif
        return expensive
    }
}
