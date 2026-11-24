import Foundation
#if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
import CryptoKit
import ObjectiveC
#endif

/// 样例库模式：只在开发时用。模拟器里用一套内置的假文档（apps/mobile-ios/dev/fixture-vault）启动 App，
/// 检查界面、截图时碰不到真实笔记库。说明和启动方法见 apps/mobile-ios/dev/README.md。
///
/// 三个条件同时成立才开：DEBUG 构建、运行在模拟器、启动时给了环境变量 SPARK_FIXTURE_DIR（样例库目录）。
/// DEBUG 构建在模拟器上默认也不许以平时模式运行：没给 SPARK_FIXTURE_DIR、也没显式给 SPARK_SIM_REAL=1 时
/// 拦下（Fixture.isBlocked），只显示说明页（SimulatorGuard），不建 Vault、不读存储和钥匙串、不联网。
/// 真机和 Release 构建里下面 #if 里的代码不编进去，isOn 恒为 false，也没有拦下这回事。进程启动时定一次，之后不变。
/// 别处只读 Fixture.isOn / Fixture.isBlocked，不自己看环境变量。
///
/// 开着时：
/// - 存储全部换到样例库专用的位置（App 容器里的 Library/SparkFixture/，App Group 容器里的 SparkFixture/，
///   单独的 UserDefaults suite），每次启动清掉上一次的，再把 SPARK_FIXTURE_DIR 整份拷进来；
/// - 钥匙串不读不写（GitHub 令牌、模型 Key 都当作没有）；
/// - 不联网：发请求的地方先过 NetGate，被拦下时报「样例库模式不联网」。
///   tests/test_fixture_network_scan.py 扫源码，新加的联网用法没过关口、也不在豁免清单里就失败。
enum Fixture {
    static let envKey = "SPARK_FIXTURE_DIR"
    static let offlineMessage = "样例库模式不联网"
    /// 界面上始终可见的标记（FixtureBadge）
    static let badge = "样例库"

    #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
    /// 模拟器上真要用这台模拟器里的真实数据时，启动时给 SPARK_SIM_REAL=1
    static let realEnvKey = "SPARK_SIM_REAL"

    /// 这次启动按哪种模式运行
    enum Mode: String { case fixture, real, blocked }

    /// 纯逻辑（tests/test_fixture_switch.py）。真机、Release 永远是「真实」；
    /// 模拟器 DEBUG：给了样例库目录（不全是空白）是样例库，否则显式给了 SPARK_SIM_REAL=1 是真实，都没有就拦下
    static func mode(debug: Bool, simulator: Bool, sourceDir: String?, simReal: String?) -> Mode {
        guard debug, simulator else { return .real }
        if let dir = sourceDir, !dir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return .fixture }
        return simReal == "1" ? .real : .blocked
    }

    /// 分享扩展拿不到环境变量，看主 App 留在 App Group 里的标记。两种标记互斥；万一都在，按样例库（不碰真实数据的那边）
    static func extensionMode(debug: Bool, simulator: Bool, fixtureMarker: Bool, realMarker: Bool) -> Mode {
        guard debug, simulator else { return .real }
        if fixtureMarker { return .fixture }
        return realMarker ? .real : .blocked
    }

    static let currentMode: Mode = {
        if isExtension {
            return extensionMode(debug: true, simulator: true, fixtureMarker: groupMarker(markerName), realMarker: groupMarker(realMarkerName))
        }
        let env = ProcessInfo.processInfo.environment
        return mode(debug: true, simulator: true, sourceDir: env[envKey], simReal: env[realEnvKey])
    }()

    /// 唯一的开关
    static var isOn: Bool { currentMode == .fixture }
    /// 模拟器 DEBUG 上没说用哪种数据：只显示说明页
    static var isBlocked: Bool { currentMode == .blocked }

    /// 说明页的话（主 App 和分享扩展共用）
    static let guardTitle = "模拟器上只能用样例库模式启动"
    static let guardHowTo = "在电脑上运行 apps/mobile-ios/dev/run_fixture.sh \"<模拟器名>\"，用内置的假文档启动。"
    static let guardReal = "确实要用这台模拟器里的真实数据时，启动时加环境变量 SPARK_SIM_REAL=1"
        + "（xcrun simctl launch 要写成 SIMCTL_CHILD_SPARK_SIM_REAL=1）。"

    /// 测试时换到临时目录（tests/test_fixture_switch.py）；App 里不设
    nonisolated(unsafe) static var testRoot: URL?

    /// 拷样例库、生成清单时出的问题：标记上显示出来
    nonisolated(unsafe) private(set) static var problem: String?

    static let defaultsSuite = "com.minxu.spark.fixture"
    static let groupDefaultsSuite = "com.minxu.spark.fixture.group"
    private static let markerName = "active"
    /// 用 SPARK_SIM_REAL=1 启动过主 App：分享扩展也按平时模式走。跟 markerName 互斥
    private static let realMarkerName = "real"
    /// 同 SharedStore.groupID（这里单写一份，测试单独编这个文件时不用带上 SparkTask.swift；
    /// tests/test_fixture_switch.py 核对两处一样）
    static let groupID = "group.com.minxu.spark"

    private static var isExtension: Bool { Bundle.main.bundleURL.pathExtension == "appex" }

    /// App 自己容器里样例库的根：support/ 顶替 Application Support，caches/ 顶替 Caches
    static var appRoot: URL {
        if let testRoot { return testRoot.appending(path: "app", directoryHint: .isDirectory) }
        return URL.libraryDirectory.appending(path: "SparkFixture", directoryHint: .isDirectory)
    }

    /// App Group 容器里样例库的根（发件箱、阅读记录、剪藏收件箱）。App Group 没配好时是 nil，跟平时一样
    static var groupRoot: URL? {
        if let testRoot { return testRoot.appending(path: "group", directoryHint: .isDirectory) }
        return FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: groupID)?
            .appending(path: "SparkFixture", directoryHint: .isDirectory)
    }

    private static func groupMarker(_ name: String) -> Bool {
        guard let g = groupRoot else { return false }
        return FileManager.default.fileExists(atPath: g.appending(path: name).path)
    }

    /// 每次都是同一个对象：swizzle 后 UserDefaults.standard 返回它
    static let defaults: UserDefaults = UserDefaults(suiteName: defaultsSuite)!
    static let groupDefaults: UserDefaults = UserDefaults(suiteName: groupDefaultsSuite)!
    #else
    static let isOn = false
    static var problem: String? { nil }
    #endif

    // MARK: 存储位置（开关关着时原样返回传进来的表达式，走原来的代码路径）

    /// 顶替 URL.applicationSupportDirectory
    static func support(_ real: @autoclosure () -> URL) -> URL {
        #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
        if isOn { return appRoot.appending(path: "support", directoryHint: .isDirectory) }
        #endif
        return real()
    }

    /// 顶替 URL.cachesDirectory
    static func caches(_ real: @autoclosure () -> URL) -> URL {
        #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
        if isOn { return appRoot.appending(path: "caches", directoryHint: .isDirectory) }
        #endif
        return real()
    }

    /// 顶替 App Group 容器的根
    static func groupContainer(_ real: @autoclosure () -> URL?) -> URL? {
        #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
        if isOn { return groupRoot }
        #endif
        return real()
    }

    /// 顶替 App Group 的 UserDefaults
    static func groupUserDefaults(_ real: @autoclosure () -> UserDefaults) -> UserDefaults {
        #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
        if isOn { return groupDefaults }
        #endif
        return real()
    }

    // MARK: 启动

    /// 主 App 进程最早调一次（SparkMain，在 SparkApp 建任何状态之前）。开关关着时什么都不做
    static func prepare() {
        #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
        guard !isExtension else { return }
        if currentMode == .real { markRealMode(groupRoot: groupRoot); return }
        guard isOn else { return }
        // 先换 UserDefaults：拷文件出错也不能退回去读写真实的设置
        defaults.removePersistentDomain(forName: defaultsSuite)
        pinStandardDefaults(to: defaults)
        groupDefaults.removePersistentDomain(forName: groupDefaultsSuite)
        // 不连 GitHub、只用「数据线」：启动时按清单把拷进来的样例库收进书架（Vault.importCable）
        groupDefaults.set(true, forKey: "cableOnly")
        URLProtocol.registerClass(NoNetwork.self)
        let source = URL(fileURLWithPath: ProcessInfo.processInfo.environment[envKey] ?? "", isDirectory: true)
        do {
            try installFiles(source: source, appRoot: appRoot, groupRoot: groupRoot)
        } catch {
            problem = "样例库没拷进来：\(error.localizedDescription)"
        }
        #endif
    }

    #if DEBUG && (targetEnvironment(simulator) || SPARK_FIXTURE_TEST)
    /// 清掉上一次的样例库位置，把 source 整份拷进 support/vault（Vault.root），按拷进来的文件写一份数据线清单，
    /// 再在 App Group 里留标记给分享扩展看
    static func installFiles(source: URL, appRoot: URL, groupRoot: URL?) throws {
        let fm = FileManager.default
        try? fm.removeItem(at: appRoot)
        if let groupRoot { try? fm.removeItem(at: groupRoot) }
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: source.path, isDirectory: &isDir), isDir.boolValue else {
            throw SparkError("找不到样例库目录 \(source.path)")
        }
        let support = appRoot.appending(path: "support", directoryHint: .isDirectory)
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        try fm.createDirectory(at: appRoot.appending(path: "caches", directoryHint: .isDirectory), withIntermediateDirectories: true)
        let vault = support.appending(path: "vault", directoryHint: .isDirectory)
        try fm.copyItem(at: source, to: vault)
        try cableManifest(root: vault).write(to: vault.appending(path: ".spark-cable.json"), options: .atomic)
        if let groupRoot {
            try fm.createDirectory(at: groupRoot, withIntermediateDirectories: true)
            try Data(ISO8601DateFormatter().string(from: .now).utf8).write(to: groupRoot.appending(path: markerName))
        }
    }

    /// 用 SPARK_SIM_REAL=1 启动（只在模拟器 DEBUG 上会走到）：留「真实模式」标记、清掉样例库标记，分享扩展跟着按平时模式走
    static func markRealMode(groupRoot: URL?) {
        guard let groupRoot else { return }
        let fm = FileManager.default
        try? fm.removeItem(at: groupRoot.appending(path: markerName))
        try? fm.createDirectory(at: groupRoot, withIntermediateDirectories: true)
        try? Data(ISO8601DateFormatter().string(from: .now).utf8).write(to: groupRoot.appending(path: realMarkerName))
    }

    /// 数据线清单（格式同电脑 core/cable_sync.py）：{generated_at, files: {路径: [git blob sha, 大小]}}。
    /// 顺手删掉 .DS_Store
    static func cableManifest(root: URL, now: Date = .now) throws -> Data {
        let fm = FileManager.default
        var files: [String: [Any]] = [:]
        let base = root.standardizedFileURL.path.hasSuffix("/") ? root.standardizedFileURL.path : root.standardizedFileURL.path + "/"
        let walker = fm.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey])
        while let url = walker?.nextObject() as? URL {
            guard (try? url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true else { continue }
            if url.lastPathComponent == ".DS_Store" { try? fm.removeItem(at: url); continue }
            let full = url.standardizedFileURL.path
            guard full.hasPrefix(base) else { continue }
            let rel = String(full.dropFirst(base.count))
            if rel == ".spark-cable.json" { continue }
            let data = try Data(contentsOf: url)
            files[rel] = [blobSHA(data), data.count]
        }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let obj: [String: Any] = ["generated_at": f.string(from: now), "files": files]
        return try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes])
    }

    /// git 的 blob sha：sha1("blob <字节数>\0" + 内容)，跟 GitHub、数据线清单里的一样
    static func blobSHA(_ data: Data) -> String {
        var h = Insecure.SHA1()
        h.update(data: Data("blob \(data.count)\0".utf8))
        h.update(data: data)
        return h.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// App 里直接用 UserDefaults.standard 和 @AppStorage 的地方有六十多处。不逐个改，
    /// 把 +[NSUserDefaults standardUserDefaults] 换成返回样例库的 suite：一处改动全部生效，开关关着时一行不动。
    /// 换完立刻核对，没换成就停下（宁可起不来，也不读写真实的设置）
    static func pinStandardDefaults(to suite: UserDefaults) {
        let sel = NSSelectorFromString("standardUserDefaults")
        guard let method = class_getClassMethod(UserDefaults.self, sel) else {
            fatalError("样例库模式：找不到 +[NSUserDefaults standardUserDefaults]")
        }
        let block: @convention(block) (AnyObject) -> UserDefaults = { _ in suite }
        method_setImplementation(method, imp_implementationWithBlock(block))
        precondition(UserDefaults.standard === suite, "样例库模式：UserDefaults.standard 没换成样例库的 suite")
    }

    /// 兜底：漏网的 URLSession.shared 请求（NetGate 之外的）一律失败
    final class NoNetwork: URLProtocol {
        override class func canInit(with request: URLRequest) -> Bool {
            ["http", "https", "ws", "wss"].contains(request.url?.scheme?.lowercased() ?? "")
        }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            client?.urlProtocol(self, didFailWithError: SparkError(Fixture.offlineMessage))
        }
        override func stopLoading() {}
    }
    #endif
}

/// 联网的关口。发请求、开连接之前先过这里：样例库模式下抛「样例库模式不联网」，平时什么都不做
enum NetGate {
    static func check() throws {
        if Fixture.isOn { throw SparkError(Fixture.offlineMessage) }
    }

    /// 不能抛错的地方（图片、网络时间、字体）用这个
    static var blocked: Bool { Fixture.isOn }
}
