// director — records a short Calliope reader demo: text selected on a page, then the player.
//
// Draws a 1600×900 stage at the bottom centre of the main screen (wallpaper and a document),
// makes sure the Kokoro server is warm first, selects text with the real cursor, launches the real
// calliope-player, opens the reader and changes speed through Accessibility, then selects a
// Portuguese paragraph and switches to it. Records the stage and the player's audio with
// ScreenCaptureKit at 1280×720; every other app is excluded. No captions, no end card.
//
// Usage: director <output.mov> [--switch-to 19:524288] [--return-to 18:524288]
import AppKit
import ApplicationServices
import CoreMedia
import ScreenCaptureKit

let arguments = CommandLine.arguments
let outputPath = arguments.dropFirst().first { !$0.hasPrefix("--") && !$0.contains(":") } ?? "demo-raw.mov"
let runtimeURL = URL(fileURLWithPath: NSString(string: "~/.local/share/calliope").expandingTildeInPath)
let playerSettings = UserDefaults(suiteName: "com.gabrielbelli.calliope-player")!

let screenFrame = NSScreen.screens[0].frame
let stageSize = NSSize(width: 1600, height: 900)
let outputSize = CGSize(width: 1280, height: 720)
let stageFrame = NSRect(x: screenFrame.midX - stageSize.width / 2, y: screenFrame.minY, width: stageSize.width, height: stageSize.height)

/// Private source: synthetic input never inherits held modifiers (a leaked ⌥ once turned the
/// selection drag into a rectangular block selection).
let eventSource = CGEventSource(stateID: .privateState)

func toGlobal(_ point: NSPoint) -> CGPoint { CGPoint(x: point.x, y: screenFrame.height - point.y) }
func pause(_ seconds: Double) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }

var recordingStart: Date?
var failures = 0

func log(_ message: String) {
    let t = recordingStart.map { String(format: "%5.2f", Date().timeIntervalSince($0)) } ?? "  pre"
    print("[\(t)] \(message)")
    fflush(stdout)
}

func check(_ passed: Bool, _ what: String) {
    if !passed { failures += 1 }
    log("\(passed ? "PASS" : "FAIL") \(what)")
}

func hotkeyArgument(_ name: String) -> (CGKeyCode, UInt64)? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    let parts = arguments[index + 1].split(separator: ":").compactMap { UInt64($0) }
    return parts.count == 2 ? (CGKeyCode(parts[0]), parts[1]) : nil
}
let switchTo = hotkeyArgument("--switch-to")
let returnTo = hotkeyArgument("--return-to")

func press(_ hotkey: (CGKeyCode, UInt64)) {
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: eventSource, virtualKey: hotkey.0, keyDown: down)
        event?.flags = CGEventFlags(rawValue: hotkey.1)
        event?.post(tap: .cghidEventTap)
    }
    for modifier: CGKeyCode in [58, 61, 55, 56, 59] {
        let release = CGEvent(keyboardEventSource: eventSource, virtualKey: modifier, keyDown: false)
        release?.flags = []
        release?.post(tap: .cghidEventTap)
    }
}

// MARK: - Page

let pageTitle = "How a phishing attack actually works"
let paragraphOne = "The link leads to a perfect clone of your login page. You type your password, and it is relayed to the real site in real time, so even a one-time code gets captured."
let paragraphTwo = "The fix is boring and effective. Use a password manager, which refuses to fill your credentials on the wrong domain, and turn on passkeys wherever you can."
let portugueseHeading = "Em português"
let portugueseParagraph = "Nunca reutilize a mesma senha. Ative a autenticação em dois fatores e desconfie de qualquer mensagem que peça urgência: é assim que a maioria dos golpes começa."
let pageText = [pageTitle, paragraphOne, paragraphTwo, portugueseHeading, portugueseParagraph].joined(separator: "\n\n")

final class WallpaperView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSGradient(colors: [NSColor(srgbRed: 0.04, green: 0.07, blue: 0.18, alpha: 1), NSColor(srgbRed: 0.09, green: 0.05, blue: 0.20, alpha: 1)])!
            .draw(in: bounds, angle: -35)
        let glows: [(NSColor, CGFloat, CGFloat, CGFloat)] = [
            (NSColor(srgbRed: 0.10, green: 0.78, blue: 0.74, alpha: 0.55), 0.12, 0.22, 0.40),
            (NSColor(srgbRed: 0.80, green: 0.25, blue: 0.62, alpha: 0.45), 0.90, 0.70, 0.44),
            (NSColor(srgbRed: 0.25, green: 0.45, blue: 0.98, alpha: 0.55), 0.52, 0.02, 0.48),
            (NSColor(srgbRed: 0.98, green: 0.55, blue: 0.30, alpha: 0.28), 0.30, 0.98, 0.30),
        ]
        for (colour, x, y, radius) in glows {
            let centre = NSPoint(x: bounds.width * x, y: bounds.height * y)
            NSGradient(colors: [colour, colour.withAlphaComponent(0)])!
                .draw(fromCenter: centre, radius: 0, toCenter: centre, radius: bounds.width * radius, options: [])
        }
    }
}

final class DocumentView: NSView {
    let textView = NSTextView(usingTextLayoutManager: false)
    private static let titleBarHeight: CGFloat = 38
    static let selectionColour = NSColor(srgbRed: 0.70, green: 0.84, blue: 1.0, alpha: 1)

    override init(frame: NSRect) {
        super.init(frame: frame)
        // A light page on any system appearance, and the active light-mode selection colour even
        // when the window is not key (the inactive colour is a dark grey).
        appearance = NSAppearance(named: .aqua)
        wantsLayer = true
        layer?.cornerRadius = 14
        layer?.backgroundColor = NSColor(srgbRed: 0.985, green: 0.985, blue: 0.99, alpha: 1).cgColor
        layer?.shadowColor = NSColor.black.cgColor
        layer?.shadowOpacity = 0.45
        layer?.shadowRadius = 34
        layer?.shadowOffset = CGSize(width: 0, height: -14)

        let text = NSMutableAttributedString(string: pageText)
        let body = NSMutableParagraphStyle()
        body.lineSpacing = 5
        text.addAttributes([.font: NSFont.systemFont(ofSize: 19), .foregroundColor: NSColor(white: 0.18, alpha: 1), .paragraphStyle: body],
                           range: NSRange(location: 0, length: text.length))
        let nsText = pageText as NSString
        text.addAttributes([.font: NSFont.systemFont(ofSize: 29, weight: .bold), .foregroundColor: NSColor(white: 0.06, alpha: 1), .kern: -0.4],
                           range: nsText.range(of: pageTitle))
        text.addAttributes([.font: NSFont.systemFont(ofSize: 20, weight: .semibold), .foregroundColor: NSColor(white: 0.06, alpha: 1)],
                           range: nsText.range(of: portugueseHeading))
        textView.frame = NSRect(x: 56, y: 24, width: frame.width - 112, height: frame.height - Self.titleBarHeight - 50)
        textView.textContainer?.containerSize = NSSize(width: textView.frame.width, height: .greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.textStorage?.setAttributedString(text)
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.textContainerInset = .zero
        textView.textContainer?.lineFragmentPadding = 0
        textView.selectedTextAttributes = [.backgroundColor: Self.selectionColour]
        addSubview(textView)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        let bar = NSRect(x: 0, y: bounds.height - Self.titleBarHeight, width: bounds.width, height: Self.titleBarHeight)
        NSColor(white: 0.94, alpha: 1).setFill()
        NSBezierPath(roundedRect: bar, xRadius: 14, yRadius: 14).fill()
        NSRect(x: 0, y: bar.minY, width: bounds.width, height: 14).fill()
        NSColor(white: 0.86, alpha: 1).setFill()
        NSRect(x: 0, y: bar.minY, width: bounds.width, height: 1).fill()
        for (index, colour) in [NSColor.systemRed, NSColor.systemYellow, NSColor.systemGreen].enumerated() {
            colour.setFill()
            NSBezierPath(ovalIn: NSRect(x: 18 + CGFloat(index) * 21, y: bar.midY - 6.5, width: 13, height: 13)).fill()
        }
        let title = "Field Notes — Phishing" as NSString
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13.5, weight: .semibold), .foregroundColor: NSColor(white: 0.35, alpha: 1)]
        let size = title.size(withAttributes: attributes)
        title.draw(at: NSPoint(x: bar.midX - size.width / 2, y: bar.midY - size.height / 2), withAttributes: attributes)
    }
}

final class StageWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

@MainActor
final class Stage {
    let window = StageWindow(contentRect: stageFrame, styleMask: .borderless, backing: .buffered, defer: false)
    // Above the reader's reach: the capsule sits 90 pt up and the open reader ends near 360 pt.
    let document = DocumentView(frame: NSRect(x: 210, y: 400, width: 1180, height: 470))

    init() {
        let root = WallpaperView(frame: NSRect(origin: .zero, size: stageSize))
        root.wantsLayer = true
        window.contentView = root
        window.isOpaque = true
        window.backgroundColor = .black
        window.hasShadow = false
        root.addSubview(document)
    }

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(document.textView)
    }

    private var pinned: NSRange?

    /// Keeps a selection looking selected after the page loses focus to the player. AppKit greys
    /// out a real selection in an inactive window, so paint it into the text and drop the selection.
    func pinHighlight(_ range: NSRange) {
        clearHighlight()
        document.textView.textStorage?.addAttribute(.backgroundColor, value: DocumentView.selectionColour, range: range)
        document.textView.setSelectedRange(NSRange(location: NSMaxRange(range), length: 0))
        pinned = range
    }

    func clearHighlight() {
        if let pinned { document.textView.textStorage?.removeAttribute(.backgroundColor, range: pinned) }
        pinned = nil
    }

    /// Screen point (CG global) at the leading or trailing edge of a character range on the page.
    func point(for range: NSRange, trailing: Bool) -> CGPoint {
        let characterRange = NSRange(location: trailing ? NSMaxRange(range) - 1 : range.location, length: 1)
        let rect = document.textView.firstRect(forCharacterRange: characterRange, actualRange: nil)
        return toGlobal(NSPoint(x: trailing ? rect.maxX - 1 : rect.minX + 1, y: rect.midY))
    }
}

// MARK: - Mouse

enum Mouse {
    static var location: CGPoint { CGEvent(source: nil)?.location ?? .zero }

    private static func post(_ type: CGEventType, _ point: CGPoint) {
        let event = CGEvent(mouseEventSource: eventSource, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
        event?.flags = []
        event?.post(tap: .cghidEventTap)
    }

    static func glide(to target: CGPoint, duration: Double, dragging: Bool = false) async {
        let start = location
        let steps = max(1, Int(duration * 120))
        for step in 1...steps {
            let t = Double(step) / Double(steps)
            let eased = t < 0.5 ? 4 * t * t * t : 1 - pow(-2 * t + 2, 3) / 2
            post(dragging ? .leftMouseDragged : .mouseMoved,
                 CGPoint(x: start.x + (target.x - start.x) * eased, y: start.y + (target.y - start.y) * eased))
            await pause(1.0 / 120)
        }
    }

    static func click(_ target: CGPoint, travel: Double) async {
        await glide(to: target, duration: travel)
        await pause(0.08)
        post(.leftMouseDown, target)
        await pause(0.06)
        post(.leftMouseUp, target)
    }

    static func drag(from start: CGPoint, to end: CGPoint, travel: Double, duration: Double) async {
        await glide(to: start, duration: travel)
        await pause(0.12)
        post(.leftMouseDown, start)
        await glide(to: end, duration: duration, dragging: true)
        post(.leftMouseUp, end)
    }
}

// MARK: - Server and player

func serverIsWarm() async -> Bool {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:47815/health")!)
    request.timeoutInterval = 1
    guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
    return (response as? HTTPURLResponse)?.statusCode == 200
}

/// Starts the local server if needed and waits until it answers, so nothing loads on camera.
func warmServer() async -> Bool {
    if await serverIsWarm() { return true }
    let process = Process()
    process.executableURL = runtimeURL.appendingPathComponent(".venv/bin/python")
    process.arguments = [runtimeURL.appendingPathComponent("server.py").path]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    try? process.run()
    for _ in 0..<120 {
        await pause(0.25)
        if await serverIsWarm() { return true }
    }
    return false
}

@MainActor
final class PlayerRemote {
    private var process: Process?

    func launch(text: String) throws {
        let queue = runtimeURL.appendingPathComponent("queue")
        try FileManager.default.createDirectory(at: queue, withIntermediateDirectories: true)
        let file = queue.appendingPathComponent("demo-\(UUID().uuidString).txt")
        try text.write(to: file, atomically: true, encoding: .utf8)
        let process = Process()
        process.executableURL = runtimeURL.appendingPathComponent("calliope-player")
        process.arguments = [file.path]
        try process.run()
        self.process = process
    }

    func quit() {
        process?.terminate()
        process = nil
    }

    var isRunning: Bool { process?.isRunning == true }

    private var app: AXUIElement? {
        guard let pid = process?.processIdentifier, isRunning else { return nil }
        let element = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(element, 1.0)
        return element
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }

    private func walk(_ root: AXUIElement, depth: Int = 0, _ visit: (AXUIElement) -> Void) {
        visit(root)
        guard depth < 14, let children = attribute(root, kAXChildrenAttribute) as? [AXUIElement] else { return }
        for child in children { walk(child, depth: depth + 1, visit) }
    }

    func labels() -> [String] {
        guard let app else { return [] }
        var values: [String] = []
        walk(app) { element in
            if (self.attribute(element, kAXRoleAttribute) as? String) == kAXStaticTextRole,
               let value = self.attribute(element, kAXValueAttribute) as? String { values.append(value) }
        }
        return values
    }

    var windowHeight: CGFloat {
        guard let app, let windows = attribute(app, kAXWindowsAttribute) as? [AXUIElement], let window = windows.first,
              let size = attribute(window, kAXSizeAttribute) else { return 0 }
        var extent = CGSize.zero
        AXValueGetValue(size as! AXValue, .cgSize, &extent)
        return extent.height
    }

    func button(_ tip: String, timeout: Double = 6) async -> CGPoint? {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            var found: CGRect?
            if let app {
                walk(app) { element in
                    guard found == nil, (self.attribute(element, kAXRoleAttribute) as? String) == kAXButtonRole,
                          (self.attribute(element, kAXHelpAttribute) as? String) == tip,
                          let position = self.attribute(element, kAXPositionAttribute),
                          let size = self.attribute(element, kAXSizeAttribute) else { return }
                    var point = CGPoint.zero
                    var extent = CGSize.zero
                    AXValueGetValue(position as! AXValue, .cgPoint, &point)
                    AXValueGetValue(size as! AXValue, .cgSize, &extent)
                    found = CGRect(origin: point, size: extent)
                }
            }
            if let found, found.width > 0 { return CGPoint(x: found.midX, y: found.midY) }
            await pause(0.08)
        }
        return nil
    }
}

// MARK: - Recording

@available(macOS 15.0, *)
@MainActor
final class Recorder: NSObject, SCStreamDelegate, SCRecordingOutputDelegate {
    private var stream: SCStream?
    private var finished: CheckedContinuation<Void, Never>?
    private var didFinish = false

    func start(to url: URL) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw NSError(domain: "director", code: 1) }
        // Everything already running except this stage is excluded. The player starts later, so it
        // and its audio are captured.
        let excluded = content.applications.filter { $0.processID != getpid() }
        let filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.width = Int(outputSize.width)
        config.height = Int(outputSize.height)
        config.sourceRect = CGRect(x: stageFrame.minX, y: screenFrame.height - stageFrame.maxY, width: stageSize.width, height: stageSize.height)
        config.minimumFrameInterval = CMTime(value: 1, timescale: 60)
        config.showsCursor = true
        config.capturesAudio = true
        config.sampleRate = 48_000
        config.channelCount = 2
        config.excludesCurrentProcessAudio = true
        config.queueDepth = 8

        let recording = SCRecordingOutputConfiguration()
        recording.outputURL = url
        recording.outputFileType = .mov
        recording.videoCodecType = .h264
        let output = SCRecordingOutput(configuration: recording, delegate: self)
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addRecordingOutput(output)
        try await stream.startCapture()
        self.stream = stream
    }

    func stop() async {
        try? await stream?.stopCapture()
        if !didFinish { await withCheckedContinuation { finished = $0 } }
    }

    private func markFinished() {
        didFinish = true
        finished?.resume()
        finished = nil
    }

    nonisolated func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        Task { @MainActor in self.markFinished() }
    }

    nonisolated func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: any Error) {
        print("recording failed: \(error.localizedDescription)")
        Task { @MainActor in self.markFinished() }
    }

    nonisolated func stream(_ stream: SCStream, didStopWithError error: any Error) {
        print("stream stopped: \(error.localizedDescription)")
    }
}

// MARK: - The demo (≈17 s)

@MainActor
func runDemo(stage: Stage) async {
    let savedSpeed = playerSettings.object(forKey: "speed")
    let savedReader = playerSettings.object(forKey: "karaoke")
    playerSettings.set(Float(1.0), forKey: "speed")
    playerSettings.set(false, forKey: "karaoke")
    let player = PlayerRemote()
    func finish() {
        player.quit()
        if let savedSpeed { playerSettings.set(savedSpeed, forKey: "speed") } else { playerSettings.removeObject(forKey: "speed") }
        if let savedReader { playerSettings.set(savedReader, forKey: "karaoke") } else { playerSettings.removeObject(forKey: "karaoke") }
        stage.window.orderOut(nil)
        if let returnTo { press(returnTo) }
        log("DONE with \(failures) failure(s)")
        exit(failures == 0 ? 0 : 2)
    }
    guard #available(macOS 15.0, *) else { return finish() }

    // Off camera: a warm server, so the first word follows the selection without a spinner.
    let warm = await warmServer()
    check(warm, "Kokoro server warm before recording")
    guard warm else { return finish() }

    if let switchTo {
        press(switchTo)
        await pause(1.4)
    }
    stage.show()
    let page = pageText as NSString
    let textView = stage.document.textView
    await Mouse.glide(to: toGlobal(NSPoint(x: stageFrame.midX + 380, y: stageFrame.minY + 300)), duration: 0.3)
    await pause(0.4)

    let recorder = Recorder()
    do {
        try await recorder.start(to: URL(fileURLWithPath: outputPath))
        recordingStart = Date()
    } catch {
        check(false, "recording started: \(error.localizedDescription)")
        return finish()
    }
    await pause(0.25)

    // Select two paragraphs on the page
    log("SCENE Select")
    let english = NSUnionRange(page.range(of: paragraphOne), page.range(of: paragraphTwo))
    await Mouse.drag(from: stage.point(for: page.range(of: paragraphOne), trailing: false),
                     to: stage.point(for: page.range(of: paragraphTwo), trailing: true), travel: 0.35, duration: 0.9)
    let selected = textView.selectedRange()
    check(textView.selectedRanges.count == 1 && abs(selected.location - english.location) <= 1 && abs(NSMaxRange(selected) - NSMaxRange(english)) <= 1,
          "English selection is one range over both paragraphs (got \(selected.location)–\(NSMaxRange(selected)))")
    check(stage.window.isKeyWindow, "page window is key, so the selection draws as active")
    // Let the text view's drag-tracking loop see the mouse-up first; pinning earlier gets undone
    // when the loop re-applies the real selection, which then greys out behind the player.
    await pause(0.25)
    stage.pinHighlight(selected)
    await pause(0.15)

    // The capsule appears and reads
    log("SCENE Speak")
    do { try player.launch(text: page.substring(with: english)) } catch { check(false, "player launched: \(error)") }
    guard let readerButton = await player.button("Follow the text") else { check(false, "capsule appeared"); return finish() }
    await Mouse.glide(to: CGPoint(x: readerButton.x + 170, y: readerButton.y - 60), duration: 0.4)
    await pause(0.7)

    // The reader
    log("SCENE Reader")
    await Mouse.click(readerButton, travel: 0.6)
    await pause(0.4)
    check(player.windowHeight > 150, "reader opened (height \(Int(player.windowHeight)) pt)")
    await Mouse.glide(to: CGPoint(x: readerButton.x + 250, y: readerButton.y - 40), duration: 0.5)
    await pause(2.3)

    // Speed
    log("SCENE Speed")
    if let faster = await player.button("Faster") {
        await Mouse.click(faster, travel: 0.5)
        await pause(0.6)
        await Mouse.click(faster, travel: 0.1)
    }
    await pause(0.15)
    check(player.labels().contains("1.5×"), "speed shows 1.5× (labels: \(player.labels()))")
    await pause(1.4)

    // Select the Portuguese paragraph and switch
    log("SCENE Language")
    // Clicking the capsule leaves the page window inactive, and a first click on an inactive
    // window only activates it — the drag would select nothing.
    stage.show()
    await pause(0.15)
    let portuguese = page.range(of: portugueseParagraph)
    let portugueseStart = stage.point(for: portuguese, trailing: false)
    await Mouse.glide(to: portugueseStart, duration: 0.5)
    stage.clearHighlight()  // as a real click would, at the moment the new drag begins
    await Mouse.drag(from: portugueseStart, to: stage.point(for: portuguese, trailing: true), travel: 0.01, duration: 0.9)
    let selectedPortuguese = textView.selectedRange()
    check(textView.selectedRanges.count == 1 && abs(selectedPortuguese.location - portuguese.location) <= 1 && abs(NSMaxRange(selectedPortuguese) - NSMaxRange(portuguese)) <= 1,
          "Portuguese selection covers its paragraph (got \(selectedPortuguese.location)–\(NSMaxRange(selectedPortuguese)))")
    await pause(0.25)
    stage.pinHighlight(selectedPortuguese)
    await pause(0.3)
    player.quit()
    do { try player.launch(text: portugueseParagraph) } catch { check(false, "Portuguese player launched: \(error)") }
    _ = await player.button("Follow the text")
    await pause(0.3)
    check(player.labels().contains("PT"), "badge says PT (labels: \(player.labels()))")
    await Mouse.glide(to: CGPoint(x: readerButton.x + 340, y: readerButton.y - 110), duration: 0.5)
    await pause(2.8)

    await recorder.stop()
    log("recorded \(outputPath)")
    finish()
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
Task { @MainActor in await runDemo(stage: Stage()) }
app.run()
