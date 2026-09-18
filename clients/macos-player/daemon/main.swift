// calliope-daemon — the resident half of Calliope on macOS.
//
// WHAT IS RESIDENT AND WHAT IS NOT, because this split is the whole design.
// The DAEMON lives in the menu bar and owns everything that has to outlive a
// single utterance: the global hotkey, the Kokoro server, and later the HTTP
// API. The PLAYER stays exactly what it was -- a one-shot process that reads
// one file aloud, draws the capsule, and terminates. It is the user interface
// and it is cheap to start; the model is not, so the model is the daemon's.
//
// THE REASONS, RANKED BY HOW MUCH THEY ACTUALLY JUSTIFY THIS, because one of
// them is weaker than it sounds:
//
//   1. THE HOTKEY. A global hotkey needs a process that is already running.
//      There is no other mechanism. This is the reason.
//   2. THE API. A listener needs a process that is already listening. Same.
//   3. KEEPING KOKORO WARM. Measured at 1.69 s and 1.86 s from the server's own
//      log -- not the twenty seconds it is easy to assume. And the server
//      already survives between plays, exiting only after fifteen idle minutes.
//      So this buys about 1.8 s, on the first press after a long gap. Real, and
//      felt, because it lands between pressing a key and hearing anything --
//      but it would not on its own pay for a daemon.
//
// Build and install: ../install.sh
import AppKit
import Carbon.HIToolbox

let runtimeURL = URL(fileURLWithPath: NSString(string: "~/.local/share/calliope").expandingTildeInPath)
let serverURL = "http://127.0.0.1:47815"
let settings = UserDefaults(suiteName: "com.gabrielbelli.calliope-player")!

// MARK: - The Kokoro server

/// The model, held open for as long as this daemon runs.
///
/// SUPERVISED, NOT MERELY LAUNCHED. A child that dies -- OOM, a bad wheel after
/// an upgrade, somebody killing it by hand -- must not leave a menu bar icon
/// that looks fine and a hotkey that does nothing. It is restarted with a
/// backoff, and the backoff exists so a server that cannot start at all fails
/// visibly in the menu rather than spinning forever in the background.
final class ServerSupervisor {
    private var process: Process?
    private var restarts = 0
    private var lastStart = Date.distantPast
    private(set) var lastError: String?

    var isRunning: Bool { process?.isRunning == true }

    func start() {
        guard !isRunning else { return }

        // ".venv/bin/python", EXACTLY WHAT install.sh CREATES AND WHAT THE
        // PLAYER ALREADY USES. Written from memory as "venv/bin/python3" this
        // matched nothing, and the failure was quiet in the worst way: the menu
        // said "not installed: run install.sh" on a machine where install.sh
        // had just succeeded.
        let python = runtimeURL.appendingPathComponent(".venv/bin/python")
        let script = runtimeURL.appendingPathComponent("server.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            lastError = "not installed: run install.sh"
            return
        }

        let task = Process()
        task.executableURL = python
        task.arguments = [script.path]
        // 0 MEANS NEVER, AND THAT IS THE POINT OF THE DAEMON. Started by the
        // one-shot player the server times itself out, because nothing else
        // would ever close it. Started here, this process is the reason it is
        // open, so it closes when this one does and not before.
        var env = ProcessInfo.processInfo.environment
        env["CALLIOPE_IDLE_SECONDS"] = "0"
        task.environment = env

        let log = runtimeURL.appendingPathComponent("server.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: log) {
            handle.seekToEndOfFile()
            task.standardOutput = handle
            task.standardError = handle
        }

        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.childDied() }
        }

        do {
            try task.run()
            process = task
            lastStart = Date()
            lastError = nil
        } catch {
            lastError = "could not start: \(error.localizedDescription)"
        }
    }

    private func childDied() {
        process = nil
        // A server that ran for a good while and then stopped is a different
        // event from one that will not start: the first gets a fresh count.
        if Date().timeIntervalSince(lastStart) > 60 { restarts = 0 }
        restarts += 1
        guard restarts <= 5 else {
            lastError = "stopped \(restarts) times; not restarting"
            return
        }
        let delay = min(pow(2.0, Double(restarts - 1)), 30)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.start() }
    }

    /// SIGTERM, NEVER SIGKILL. phonemizer copies libespeak-ng into a temporary
    /// directory per process and removes it only on a normal exit; the server
    /// turns SIGTERM into sys.exit for exactly that reason. Killed outright it
    /// leaves the copy behind, every time, for ever.
    func stop() {
        guard let task = process, task.isRunning else { return }
        process = nil
        task.terminationHandler = nil
        task.terminate()
    }
}

// MARK: - Reading the selection

/// The selected text in whatever application is frontmost.
///
/// THERE IS NO WAY TO DO THIS WITHOUT ACCESSIBILITY, and pretending otherwise
/// would only move the prompt somewhere less expected. Posting a synthetic ⌘C
/// is gated on the same permission the accessibility API is, so the honest
/// version asks once, explains why, and does nothing until it is granted.
///
/// THE PASTEBOARD IS PUT BACK. Taking a copy of somebody's clipboard and
/// leaving the selection in it is a side effect nobody asked for -- the next
/// ⌘V would paste the wrong thing, and they would not connect it to this.
enum Selection {
    static var isPermitted: Bool { AXIsProcessTrusted() }

    static func requestPermission() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    static func read(completion: @escaping (String?) -> Void) {
        guard isPermitted else { completion(nil); return }
        let board = NSPasteboard.general
        let saved = board.pasteboardItems?.compactMap { item -> [NSPasteboard.PasteboardType: Data] in
            var copy: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types { copy[type] = item.data(forType: type) }
            return copy
        } ?? []
        let before = board.changeCount

        postCommandC()

        // The copy is asynchronous in the other application, so the pasteboard
        // is polled rather than read once. 0.6 s is generous; a slow editor is
        // the common case, not the rare one.
        var waited = 0.0
        func poll() {
            waited += 0.04
            if board.changeCount != before || waited >= 0.6 {
                let text = board.string(forType: .string)
                restore(saved)
                completion(board.changeCount != before ? text : nil)
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.04, execute: poll)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.04, execute: poll)
    }

    private static func postCommandC() {
        // A PRIVATE EVENT SOURCE, not .hidSystemState. The demo director hit
        // this and it cost a round to find: events posted from the shared
        // source leak their modifier flags into whatever the user does next, so
        // a later drag arrives with ⌘ still held.
        guard let source = CGEventSource(stateID: .privateState) else { return }
        let c = CGKeyCode(kVK_ANSI_C)
        let down = CGEvent(keyboardEventSource: source, virtualKey: c, keyDown: true)
        let up = CGEvent(keyboardEventSource: source, virtualKey: c, keyDown: false)
        down?.flags = .maskCommand
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
    }

    private static func restore(_ saved: [[NSPasteboard.PasteboardType: Data]]) {
        // Put it back on the next turn of the loop: restoring immediately can
        // land before the copy that is still in flight.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            let board = NSPasteboard.general
            board.clearContents()
            guard !saved.isEmpty else { return }
            let items = saved.map { entry -> NSPasteboardItem in
                let item = NSPasteboardItem()
                for (type, data) in entry { item.setData(data, forType: type) }
                return item
            }
            board.writeObjects(items)
        }
    }
}

// MARK: - Speaking

/// Hands one passage to a fresh player.
///
/// THE PLAYER STAYS ONE-SHOT AND THAT IS DELIBERATE. It is the window, the
/// capsule and the underline; none of that is expensive to create and all of it
/// is simpler when it cannot outlive the thing it is showing. What was
/// expensive -- the model -- is not its any more.
///
/// The previous player is stopped first, because two of them would talk over
/// each other. That coordination used to live in the OpenClip script, which
/// read a pid file and signalled a process group from the outside; here it is
/// just a reference this process already holds.
final class Speaker {
    private var current: Process?

    var isSpeaking: Bool { current?.isRunning == true }

    func speak(_ text: String) {
        stop()
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        let queue = runtimeURL.appendingPathComponent("queue")
        try? FileManager.default.createDirectory(at: queue, withIntermediateDirectories: true)
        let file = queue.appendingPathComponent(UUID().uuidString + ".txt")
        guard (try? trimmed.write(to: file, atomically: true, encoding: .utf8)) != nil else { return }

        let player = runtimeURL.appendingPathComponent("calliope-player")
        guard FileManager.default.isExecutableFile(atPath: player.path) else { return }

        let task = Process()
        task.executableURL = player
        task.arguments = [file.path]      // the player deletes it once read
        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.current = nil }
        }
        try? task.run()
        current = task

        // THE SAME PID FILE THE OPENCLIP SCRIPT WRITES, so the two ways of
        // starting a player can stop each other's. Without it, pressing Speak
        // in OpenClip while the hotkey's player is talking gives you two voices
        // at once -- the script looks for a pid, finds the one it wrote last
        // time, and signals a process that is long gone.
        let pidFile = runtimeURL.appendingPathComponent("player.pid")
        try? String(task.processIdentifier).write(to: pidFile, atomically: true, encoding: .utf8)
    }

    func stop() {
        guard let task = current, task.isRunning else { return }
        current = nil
        task.terminationHandler = nil
        task.terminate()
    }
}

// MARK: - The hotkey

/// ⌥⌘S by default, registered with Carbon.
///
/// CARBON, IN 2026, ON PURPOSE. RegisterEventHotKey is the only API that gives
/// a true system-wide hotkey without the accessibility permission an NSEvent
/// global monitor needs, and it still works. The permission is needed anyway to
/// READ the selection -- but a hotkey that registers before permission is
/// granted can at least tell the user why nothing happened.
final class Hotkey {
    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private let action: () -> Void

    init(action: @escaping () -> Void) {
        self.action = action
        register()
    }

    private func register() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let context else { return noErr }
            Unmanaged<Hotkey>.fromOpaque(context).takeUnretainedValue().action()
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), &handler)

        let id = EventHotKeyID(signature: OSType(0x43414C4C), id: 1)   // 'CALL'
        RegisterEventHotKey(UInt32(kVK_ANSI_S),
                            UInt32(optionKey | cmdKey),
                            id, GetApplicationEventTarget(), 0, &ref)
    }
}

// MARK: - The menu bar

final class Daemon: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private let server = ServerSupervisor()
    private let speaker = Speaker()
    private var hotkey: Hotkey?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // NO DOCK ICON AND NO MENU BAR OF ITS OWN. This is a background app with
        // a status item; .accessory is what says so, and it is why the reader
        // can take focus without this stealing it back.
        NSApp.setActivationPolicy(.accessory)

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "waveform",
                                     accessibilityDescription: "Calliope")
        item.menu = buildMenu()

        server.start()
        hotkey = Hotkey { [weak self] in self?.speakSelection() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        speaker.stop()
        server.stop()
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self

        let speak = NSMenuItem(title: "Speak Selection", action: #selector(speakSelection),
                               keyEquivalent: "s")
        speak.keyEquivalentModifierMask = [.option, .command]
        speak.target = self
        menu.addItem(speak)

        let stop = NSMenuItem(title: "Stop", action: #selector(stopSpeaking), keyEquivalent: ".")
        stop.target = self
        menu.addItem(stop)

        menu.addItem(.separator())
        menu.addItem(statusLine)
        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit Calliope", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    /// One line that says what is actually true, refreshed when the menu opens.
    /// A status that is only correct at launch is worse than none, because it
    /// is believed.
    private lazy var statusLine: NSMenuItem = {
        let line = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        line.isEnabled = false
        return line
    }()

    @objc private func speakSelection() {
        guard Selection.isPermitted else {
            Selection.requestPermission()
            return
        }
        Selection.read { [weak self] text in
            guard let text, !text.isEmpty else { return }
            self?.speaker.speak(text)
        }
    }

    @objc private func stopSpeaking() { speaker.stop() }

    @objc private func quit() { NSApp.terminate(nil) }
}

extension Daemon: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        if !Selection.isPermitted {
            statusLine.title = "Needs Accessibility permission to read a selection"
        } else if let error = server.lastError {
            statusLine.title = "Kokoro: \(error)"
        } else if server.isRunning {
            statusLine.title = "Kokoro is warm"
        } else {
            statusLine.title = "Kokoro is starting…"
        }
    }
}

// MARK: - One of us, not several

// A SECOND DAEMON WOULD FIGHT THE FIRST for the hotkey and the port, and the
// symptom is a hotkey that silently stops working rather than an error. The
// check is by bundle-free process name because this is a bare executable rather
// than an app bundle, so NSRunningApplication's bundle identifier is nil.
let mine = ProcessInfo.processInfo.processIdentifier
let others = NSWorkspace.shared.runningApplications.filter {
    $0.processIdentifier != mine
        && $0.executableURL?.lastPathComponent == "calliope-daemon"
}
if !others.isEmpty {
    FileHandle.standardError.write("calliope-daemon is already running\n".data(using: .utf8)!)
    exit(0)
}

let app = NSApplication.shared
let daemon = Daemon()
app.delegate = daemon
app.run()
