import AppKit

final class Switch: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let info = NSMenuItem(title: "正在连接…", action: nil, keyEquivalent: "")
    let testItem = NSMenuItem(title: "测试纽约链路", action: #selector(testRoute), keyEquivalent: "")
    let latencyItem = NSMenuItem(title: "测量到纽约 VPS 的延迟", action: #selector(testLatency), keyEquivalent: "")
    let testInfo = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    var probe: Process?
    var tunnelPort: String?
    var worker: Process?
    var stopping = false
    var savedPhase = ""
    var timer: Timer?
    var exitRequested = false
    let project = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("workspace/Codex本地项目", isDirectory: true)

    @objc func openLocalProject() {
        var link = URLComponents()
        link.scheme = "codex"
        link.host = "threads"
        link.path = "/new"
        link.queryItems = [URLQueryItem(name: "path", value: project.path)]
        if let url = link.url { NSWorkspace.shared.open(url) }
    }

    @objc func showFolder() { NSWorkspace.shared.open(project) }
    var statusFile: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Codex NY Route/status.json")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item.button?.title = "NY…"
        item.button?.toolTip = "Codex 纽约线路 · Hysteria 2"
        let menu = NSMenu()
        menu.addItem(info)
        testItem.target = self
        menu.addItem(testItem)
        latencyItem.target = self
        menu.addItem(latencyItem)
        menu.addItem(testInfo)
        testInfo.isHidden = true
        menu.addItem(.separator())
        let local = menu.addItem(withTitle: "打开本地 Codex 项目", action: #selector(openLocalProject), keyEquivalent: "")
        local.target = self
        let finder = menu.addItem(withTitle: "在 Finder 打开成果目录", action: #selector(showFolder), keyEquivalent: "")
        finder.target = self
        menu.addItem(.separator())
        let quit = menu.addItem(withTitle: "关闭纽约线路，恢复 Clash", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        item.menu = menu
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-I", "-B", Bundle.main.url(forResource: "route", withExtension: "py")!.path]
        process.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                if p.terminationReason == .uncaughtSignal {
                    self?.recoverAfterInterruption()
                } else {
                    self?.finished(p.terminationStatus)
                }
            }
        }
        do {
            try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
            try process.run()
            worker = process
            let refresh = Timer(timeInterval: 0.6, repeats: true) { [weak self] _ in self?.updateStatus() }
            RunLoop.main.add(refresh, forMode: .common)
            timer = refresh
        } catch {
            alert(error.localizedDescription)
            NSApp.terminate(nil)
        }
    }

    func updateStatus() {
        guard let data = try? Data(contentsOf: statusFile),
              let fields = try? JSONDecoder().decode([String:String].self, from: data),
              fields["pid"] == worker.map({ String($0.processIdentifier) }) else { return }
        let phase = fields["phase"] ?? ""
        if phase != savedPhase {
            testInfo.isHidden = true
        }
        savedPhase = phase
        tunnelPort = phase == "on" ? fields["port"] : nil
        if stopping { return }
        info.title = fields["message"] ?? ""
        item.button?.title = savedPhase == "on" ? "Codex NY" : "NY…"
        item.button?.toolTip = info.title
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem === latencyItem {
            return !stopping && probe == nil
        }
        if menuItem === testItem {
            return !stopping && tunnelPort != nil && probe == nil
        }
        return true
    }

    @objc func testRoute() {
        runTest(latency: false)
    }

    @objc func testLatency() {
        runTest(latency: true)
    }

    func runTest(latency: Bool) {
        updateStatus()
        guard !stopping, probe == nil else { return }
        let port = tunnelPort
        let arguments: [String]
        if latency { arguments = ["--latency"] }
        else if let port = port { arguments = ["--test", port] }
        else { return }
        let sender = latency ? latencyItem : testItem
        let title = sender.title
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-I", "-B", Bundle.main.url(forResource: "route", withExtension: "py")!.path] + arguments
        process.standardOutput = output
        process.terminationHandler = { [weak self] p in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let message = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.probe = nil
                sender.title = title
                self.updateStatus()
                guard !self.stopping, self.worker?.isRunning == true else { return }
                let current = latency || self.tunnelPort == port
                self.showTestResult(latency: latency, ok: current && p.terminationStatus == 0,
                                    detail: current ? (message.isEmpty ? "测试未完成，请重试。" : message) : "线路状态已改变，请重新测试。")
            }
        }
        do {
            try process.run()
            probe = process
            sender.title = latency ? "正在测量到纽约 VPS 的延迟…" : "正在测试纽约链路…"
            testInfo.isHidden = true
        } catch {
            showTestResult(latency: latency, ok: false, detail: "无法启动测试。\(error.localizedDescription)")
        }
    }

    func showTestResult(latency: Bool, ok: Bool, detail: String) {
        let summary = latency ? (ok ? "纽约 VPS 往返延迟" : "纽约 VPS 延迟测量失败") : (ok ? "纽约链路正常" : "纽约链路测试未通过")
        let time = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        testInfo.title = "\(time) · \(summary)"
        if latency, ok, let value = detail.components(separatedBy: "（").first {
            testInfo.title += " " + value
        }
        testInfo.toolTip = detail
        testInfo.isHidden = false
        let dialog = NSAlert()
        dialog.messageText = summary
        dialog.informativeText = detail
        dialog.alertStyle = ok ? .informational : .warning
        dialog.addButton(withTitle: "好")
        if !latency && !ok { dialog.addButton(withTitle: "关闭纽约线路，恢复 Clash") }
        NSApp.activate(ignoringOtherApps: true)
        if dialog.runModal() == .alertSecondButtonReturn { quitApp() }
    }

    func alert(_ message: String) {
        let d = NSAlert()
        d.messageText = "Codex 纽约线路"
        d.informativeText = message
        d.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        d.runModal()
    }

    func recoverAfterInterruption() {
        timer?.invalidate()
        let recovery = Process()
        recovery.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        recovery.arguments = ["-I", "-B", Bundle.main.url(forResource: "route", withExtension: "py")!.path, "--recover"]
        item.button?.title = "恢复中…"
        worker = recovery
        recovery.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                self?.info.title = p.terminationStatus == 0
                    ? "纽约线路进程已中断，已恢复 Clash 原线路。"
                    : "纽约线路进程已中断，请在 Clash Verge 核对当前线路。"
                self?.worker = nil
                self?.finished(1)
            }
        }
        do {
            try recovery.run()
        } catch {
            info.title = "无法启动恢复程序，请在 Clash Verge 重新载入当前配置。"
            worker = nil
            finished(1)
        }
    }

    func finished(_ status: Int32) {
        timer?.invalidate()
        updateStatus()
        worker = nil
        if status != 0 {
            alert(status == 2 ? "线路开关已经在运行。" : (info.title.isEmpty ? "连接失败。" : info.title))
        }
        if exitRequested { NSApp.reply(toApplicationShouldTerminate: true) } else { NSApp.terminate(nil) }
    }

    @objc func quitApp() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let check = probe, check.isRunning { check.terminate() }
        guard let process = worker, process.isRunning else { return .terminateNow }
        exitRequested = true
        if !stopping {
            stopping = true
            item.button?.title = "恢复中…"
            info.title = "正在恢复 Clash…"
            process.terminate()
        }
        return .terminateLater
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        item.button?.performClick(nil)
        return false
    }
}

let app = NSApplication.shared
let controller = Switch()
app.delegate = controller
app.setActivationPolicy(.accessory)
app.run()
