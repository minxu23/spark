import Foundation

/// 管 spark.py 这个进程：端口上已经有 Spark（比如终端里开着的）就直接用，
/// 没有才自己启动。只有自己启动的才会在退出时停掉。
final class SparkServer {
    enum State: Equatable {
        case starting
        case running
        case failed(String)
    }

    struct Activity: Decodable {
        var jobs = 0
        var syncing = false
        init() {}
    }

    static let port = 8760
    static let baseURL = URL(string: "http://127.0.0.1:\(port)/")!
    static let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Spark/server.log")

    private(set) var state = State.starting { didSet { if state != oldValue { onChange?() } } }
    private(set) var activity = Activity() { didSet { onChange?() } }
    var onChange: (() -> Void)?
    var ownsProcess: Bool { process != nil }

    private var process: Process?
    private var timer: Timer?
    private let repo: String

    static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 10
        return URLSession(configuration: config)
    }()

    init() {
        repo = Bundle.main.object(forInfoDictionaryKey: "SparkRepo") as? String
            ?? NSHomeDirectory() + "/Developer/spark"
    }

    func start() {
        state = .starting
        probe { [weak self] alive in
            guard let self else { return }
            if alive {
                self.state = .running
            } else {
                self.spawn()
            }
            self.watch()
        }
    }

    func restart() {
        guard let process else { return }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.process = nil
                self?.start()
            }
        }
        state = .starting
        process.terminate()
    }

    func stop() {
        guard let process else { return }
        process.terminationHandler = nil
        process.terminate()
        process.waitUntilExit()
        self.process = nil
    }

    private func spawn() {
        guard FileManager.default.fileExists(atPath: repo + "/spark.py") else {
            state = .failed("找不到 \(repo)/spark.py。仓库搬过位置的话，重新运行 tools/make_app.sh。")
            return
        }
        let logs = SparkServer.logURL
        try? FileManager.default.createDirectory(at: logs.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logs.path) {
            FileManager.default.createFile(atPath: logs.path, contents: nil)
        }
        guard let log = try? FileHandle(forWritingTo: logs) else {
            state = .failed("打不开日志文件 \(logs.path)")
            return
        }
        log.seekToEndOfFile()

        // 从 Finder 打开的 App 只有系统默认的 PATH，经登录 shell 启动才找得到 python3、ffmpeg 这些
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-ilc", "exec python3 spark.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        var env = ProcessInfo.processInfo.environment
        env["SPARK_NO_BROWSER"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.process = nil
                self?.state = .failed(SparkServer.logTail())
            }
        }
        do {
            try p.run()
            process = p
        } catch {
            state = .failed("启动失败：\(error.localizedDescription)")
        }
    }

    /// 启动中等服务起来；起来之后每 5 秒问一次有没有任务在跑
    private func watch() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] t in
            guard let self else { return }
            switch self.state {
            case .starting:
                self.probe { alive in
                    if alive, self.state == .starting { self.state = .running }
                }
            case .running:
                t.invalidate()
                self.pollActivity()
                self.timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in self.pollActivity() }
            case .failed:
                t.invalidate()
            }
        }
    }

    private func probe(_ done: @escaping (Bool) -> Void) {
        let url = SparkServer.baseURL.appendingPathComponent("api/apps")
        SparkServer.session.dataTask(with: url) { _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    private func pollActivity() {
        let url = SparkServer.baseURL.appendingPathComponent("api/activity")
        SparkServer.session.dataTask(with: url) { [weak self] data, _, _ in
            let value = data.flatMap { try? JSONDecoder().decode(Activity.self, from: $0) }
            DispatchQueue.main.async { self?.activity = value ?? Activity() }
        }.resume()
    }

    private static func logTail() -> String {
        let text = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        let plain = text.replacingOccurrences(of: "\u{1B}\\[[0-9;]*m", with: "", options: .regularExpression)
        return plain.split(separator: "\n").suffix(12).joined(separator: "\n")
    }
}
