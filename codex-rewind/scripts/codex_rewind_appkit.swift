import AppKit
import Foundation

struct Target: Decodable {
    let index: Int
    let preview: String?
    let text: String?
    let checkpoint_id: String?
}

struct Input: Decodable {
    let targets: [Target]
}

final class RewindButton: NSButton {
    private let normalColor = NSColor(calibratedWhite: 0.19, alpha: 1.0)
    private let hoverColor = NSColor(calibratedRed: 0.22, green: 0.26, blue: 0.24, alpha: 1.0)
    private var trackingAreaRef: NSTrackingArea?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        commonInit()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        commonInit()
    }

    private func commonInit() {
        isBordered = false
        wantsLayer = true
        layer?.cornerRadius = 7
        layer?.backgroundColor = normalColor.cgColor
        contentTintColor = NSColor(calibratedWhite: 0.94, alpha: 1.0)
        setButtonType(.momentaryChange)
        cell?.lineBreakMode = .byTruncatingTail
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let trackingAreaRef {
            removeTrackingArea(trackingAreaRef)
        }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        trackingAreaRef = area
        addTrackingArea(area)
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = hoverColor.cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = normalColor.cgColor
    }
}

final class RewindController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let targets: [Target]
    var selectedTarget: Target?
    var window: NSWindow!
    var rootView: NSView!
    var keyMonitor: Any?
    var digitBuffer = ""
    var digitTimer: Timer?
    var isFinishing = false

    let bgColor = NSColor(calibratedRed: 0.12, green: 0.14, blue: 0.13, alpha: 1.0)
    let textColor = NSColor(calibratedWhite: 0.94, alpha: 1.0)
    let mutedColor = NSColor(calibratedWhite: 0.68, alpha: 1.0)
    let contentWidth: CGFloat = 560
    let margin: CGFloat = 16

    init(targets: [Target]) {
        self.targets = targets
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        createWindow()
        showTargets()
        if let raw = ProcessInfo.processInfo.environment["CODEX_REWIND_APPKIT_INITIAL_TARGET"],
           let index = Int(raw),
           let target = targets.first(where: { $0.index == index }) {
            selectedTarget = target
            showModes()
            if let mode = ProcessInfo.processInfo.environment["CODEX_REWIND_APPKIT_AUTO_MODE"] {
                finish([
                    "status": "selected",
                    "target": target.index,
                    "mode": mode
                ])
            }
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            return self?.handleKey(event) ?? event
        }
    }

    func createWindow() {
        let visibleRows = min(max(targets.count, 3), 7)
        let height = CGFloat(min(430, max(300, 74 + visibleRows * 44)))
        let frame = NSRect(x: 0, y: 0, width: contentWidth, height: height)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Codex Rewind"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.backgroundColor = bgColor
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        rootView = NSView(frame: frame)
        rootView.wantsLayer = true
        rootView.layer?.backgroundColor = bgColor.cgColor
        window.contentView = rootView
    }

    func windowWillClose(_ notification: Notification) {
        if !isFinishing {
            finish(["status": "dismissed"])
        }
    }

    func finish(_ result: [String: Any]) {
        if isFinishing {
            return
        }
        isFinishing = true
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        if let data = try? JSONSerialization.data(withJSONObject: result, options: []),
           let text = String(data: data, encoding: .utf8) {
            FileHandle.standardOutput.write(text.data(using: .utf8)!)
            FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        }
        NSApp.terminate(nil)
    }

    func clearRoot() {
        digitBuffer = ""
        digitTimer?.invalidate()
        digitTimer = nil
        rootView.subviews.forEach { $0.removeFromSuperview() }
    }

    func oneLine(_ text: String, limit: Int) -> String {
        let normalized = text.split(whereSeparator: { $0 == "\n" || $0 == "\t" || $0 == " " }).joined(separator: " ")
        if normalized.count <= limit {
            return normalized
        }
        let end = normalized.index(normalized.startIndex, offsetBy: limit)
        return String(normalized[..<end]) + "..."
    }

    func label(_ text: String, size: CGFloat, weight: NSFont.Weight = .regular, color: NSColor? = nil) -> NSTextField {
        let view = NSTextField(labelWithString: text)
        view.textColor = color ?? textColor
        view.font = NSFont.systemFont(ofSize: size, weight: weight)
        view.lineBreakMode = .byTruncatingTail
        return view
    }

    func makeButton(_ title: String, action: Selector, tag: Int = 0, height: CGFloat = 36) -> NSButton {
        let button = RewindButton(frame: NSRect(x: 0, y: 0, width: contentWidth - margin * 2, height: height))
        button.title = title
        button.target = self
        button.action = action
        button.tag = tag
        button.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        button.alignment = .left
        return button
    }

    func showTargets() {
        selectedTarget = nil
        clearRoot()
        let buttonWidth = rootView.bounds.width - margin * 2
        var y = rootView.bounds.height - 86

        let title = label("选择回退目标", size: 15, weight: .bold)
        title.frame = NSRect(x: margin, y: rootView.bounds.height - 42, width: buttonWidth, height: 20)
        rootView.addSubview(title)

        let visibleTargets = targets.prefix(8)
        if visibleTargets.isEmpty {
            let empty = label("当前线程没有可回退的用户对话。", size: 13, color: mutedColor)
            empty.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 22)
            rootView.addSubview(empty)
            return
        }

        for target in visibleTargets {
            let raw = target.text ?? target.preview ?? ""
            let preview = oneLine(raw, limit: 42)
            let code = target.checkpoint_id ?? "-"
            let title = "\(target.index). \(preview)    code:\(code)"
            let button = makeButton(title, action: #selector(selectTarget(_:)), tag: target.index, height: 38)
            button.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 38)
            rootView.addSubview(button)
            y -= 46
        }

        if targets.count > 8 {
            let more = label("仅显示前 8 个目标；可用数字键选择完整列表中的编号。", size: 11, color: mutedColor)
            more.frame = NSRect(x: margin, y: max(12, y), width: buttonWidth, height: 18)
            rootView.addSubview(more)
        }
    }

    func showModes() {
        clearRoot()
        let buttonWidth = rootView.bounds.width - margin * 2
        var y = rootView.bounds.height - 46

        let back = makeButton("< 返回", action: #selector(backToTargets), height: 30)
        back.alignment = .center
        back.frame = NSRect(x: margin, y: y, width: 72, height: 30)
        rootView.addSubview(back)

        let title = label("选择回退内容", size: 15, weight: .bold)
        title.frame = NSRect(x: margin + 84, y: y + 5, width: buttonWidth - 84, height: 20)
        rootView.addSubview(title)
        y -= 42

        if let target = selectedTarget {
            let preview = oneLine(target.text ?? target.preview ?? "", limit: 68)
            let selected = label("\(target.index). \(preview)", size: 12, color: mutedColor)
            selected.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 20)
            rootView.addSubview(selected)
            y -= 36
        }

        let session = makeButton("1. 仅对话", action: #selector(selectMode(_:)), tag: 1, height: 42)
        session.alignment = .center
        session.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 42)
        rootView.addSubview(session)
        y -= 52

        let code = makeButton("2. 仅代码", action: #selector(selectMode(_:)), tag: 2, height: 42)
        code.alignment = .center
        code.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 42)
        rootView.addSubview(code)
        y -= 52

        let both = makeButton("3. 对话和代码", action: #selector(selectMode(_:)), tag: 3, height: 42)
        both.alignment = .center
        both.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 42)
        rootView.addSubview(both)
        y -= 34

        let hint = label("按 1/2/3 选择；Esc 或返回键回到目标选择。", size: 11, color: mutedColor)
        hint.frame = NSRect(x: margin, y: y, width: buttonWidth, height: 18)
        rootView.addSubview(hint)
    }

    @objc func selectTarget(_ sender: NSButton) {
        guard let target = targets.first(where: { $0.index == sender.tag }) else { return }
        selectedTarget = target
        showModes()
    }

    @objc func backToTargets() {
        showTargets()
    }

    @objc func selectMode(_ sender: NSButton) {
        let mode: String
        switch sender.tag {
        case 1:
            mode = "session"
        case 2:
            mode = "code"
        default:
            mode = "both"
        }
        finish([
            "status": "selected",
            "target": selectedTarget?.index ?? 0,
            "mode": mode
        ])
    }

    func handleKey(_ event: NSEvent) -> NSEvent? {
        if event.keyCode == 53 {
            if selectedTarget == nil {
                finish(["status": "dismissed"])
            } else {
                showTargets()
            }
            return nil
        }

        guard let chars = event.charactersIgnoringModifiers, let digit = Int(chars) else {
            return event
        }
        if selectedTarget == nil {
            digitBuffer += String(digit)
            digitTimer?.invalidate()
            if targets.count < 10 {
                chooseBufferedTarget()
            } else {
                digitTimer = Timer.scheduledTimer(withTimeInterval: 0.35, repeats: false) { [weak self] _ in
                    self?.chooseBufferedTarget()
                }
            }
            return nil
        }

        if digit >= 1 && digit <= 3 {
            let button = NSButton()
            button.tag = digit
            selectMode(button)
            return nil
        }
        return event
    }

    func chooseBufferedTarget() {
        let raw = digitBuffer
        digitBuffer = ""
        guard let index = Int(raw), let target = targets.first(where: { $0.index == index }) else {
            return
        }
        selectedTarget = target
        showModes()
    }
}

if CommandLine.arguments.count < 2 {
    FileHandle.standardOutput.write("{\"status\":\"dismissed\"}\n".data(using: .utf8)!)
    exit(0)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let data = try Data(contentsOf: inputURL)
let input = try JSONDecoder().decode(Input.self, from: data)
let app = NSApplication.shared
let controller = RewindController(targets: input.targets)
app.delegate = controller
app.setActivationPolicy(.regular)
app.run()
