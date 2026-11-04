import Combine
import SwiftUI

@main
struct SparkApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        Window("Spark", id: "main") {
            RootView(model: model)
        }
        .defaultSize(width: 1320, height: 860)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .appSettings) {
                Button("设置…") { model.place = .tool(.settings) }.keyboardShortcut(",")
            }
            CommandGroup(after: .textEditing) {
                Button("在这一篇里查找") { model.findVisible = true }.keyboardShortcut("f")
            }
            CommandGroup(after: .toolbar) {
                Button("重新载入") { model.currentPane.webView.reload() }.keyboardShortcut("r")
                Button("放大") { model.stepZoom(0.1) }.keyboardShortcut("=")
                Button("缩小") { model.stepZoom(-0.1) }.keyboardShortcut("-")
                Button("实际大小") { model.stepZoom(0) }.keyboardShortcut("0")
            }
            CommandMenu("前往") {
                Button("最新") { model.place = .latest }.keyboardShortcut("1")
                Button("报告") { model.place = .reports }.keyboardShortcut("2")
                Button("剪藏") { model.place = .clips }.keyboardShortcut("3")
                Divider()
                ForEach(Array(Tool.allCases.dropLast().enumerated()), id: \.element) { index, tool in
                    Button(tool.title) { model.place = .tool(tool) }
                        .keyboardShortcut(KeyEquivalent(Character(String(index + 4))))
                }
                Divider()
                Button("下一篇") { model.go(1) }.keyboardShortcut(.downArrow, modifiers: [.command, .option])
                Button("上一篇") { model.go(-1) }.keyboardShortcut(.upArrow, modifiers: [.command, .option])
                Button("后退") { model.currentPane.webView.goBack() }.keyboardShortcut("[")
                Button("前进") { model.currentPane.webView.goForward() }.keyboardShortcut("]")
            }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let model = AppModel.shared
    private var statusItem: NSStatusItem!
    private var statusLine: NSMenuItem!
    private var observer: AnyObject?

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeStatusItem()
        model.start()
    }

    // 关掉窗口后服务继续跑，任务不会被打断；从 Dock 或菜单栏再打开
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let running = model.server.activity.jobs
        if !model.server.ownsProcess || running == 0 { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "还有 \(running) 个任务在跑"
        alert.informativeText = "退出 Spark 会中断这些任务。关掉窗口的话，任务会在后台继续跑。"
        alert.addButton(withTitle: "继续后台运行")
        alert.addButton(withTitle: "仍然退出")
        if alert.runModal() == .alertSecondButtonReturn { return .terminateNow }
        NSApp.windows.first { $0.isVisible }?.close()
        return .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.server.stop()
    }

    func menuWillOpen(_ menu: NSMenu) {
        let activity = model.server.activity
        switch model.server.state {
        case .starting: statusLine.title = "正在启动…"
        case .failed: statusLine.title = "没有启动"
        case .running:
            statusLine.title = activity.jobs > 0 ? "\(activity.jobs) 个任务在跑" : activity.syncing ? "正在同步" : "没有任务在跑"
        }
    }

    @objc private func showMain() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.windows.first { $0.canBecomeMain }?.makeKeyAndOrderFront(nil)
    }

    @objc private func restartServer() {
        if model.server.ownsProcess { model.server.restart() } else { model.server.start() }
    }

    @objc private func showLog() {
        NSWorkspace.shared.open(SparkServer.logURL)
    }

    private func makeStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "sparkle", accessibilityDescription: "Spark")
        statusItem.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.delegate = self
        statusLine = NSMenuItem(title: "正在启动…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        menu.addItem(withTitle: "打开 Spark", action: #selector(showMain), keyEquivalent: "").target = self
        menu.addItem(withTitle: "重启服务", action: #selector(restartServer), keyEquivalent: "").target = self
        menu.addItem(withTitle: "查看日志", action: #selector(showLog), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Spark", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        statusItem.menu = menu
        observer = model.$activity.sink { [weak self] activity in
            self?.statusItem.button?.title = activity.jobs > 0 ? " \(activity.jobs)" : ""
        }
    }
}
