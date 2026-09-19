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
import ServiceManagement

let runtimeURL = URL(fileURLWithPath: NSString(string: "~/.local/share/calliope").expandingTildeInPath)
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

    /// Something is already answering on 47815.
    ///
    /// A SECOND SERVER WOULD LOSE THE PORT AND DIE, and the daemon would keep
    /// restarting it into the same wall. The one already there is either an
    /// orphan of a previous daemon or one the one-shot player started; either
    /// way it is the same script serving the same model, so it is adopted. The
    /// cost is honest and worth naming: an adopted server keeps whatever idle
    /// timeout it was born with, so "kept warm" is only true of one this daemon
    /// started itself.
    private var somethingIsListening: Bool {
        let socket = socket(AF_INET, SOCK_STREAM, 0)
        guard socket >= 0 else { return false }
        defer { close(socket) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(47815).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(socket, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
        return connected
    }

    func start() {
        guard !isRunning else { return }
        if somethingIsListening {
            lastError = "adopted a server this daemon did not start"
            return
        }

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

// MARK: - Saying why nothing happened

/// A HOTKEY THAT DOES NOTHING IS UNDEBUGGABLE FROM THE OUTSIDE. There is no
/// window to show an error in and no exit code to read, so every attempt at a
/// selection says here which route it took and what it found.
enum Log {
    private static let url = runtimeURL.appendingPathComponent("daemon.log")

    static func write(_ line: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let entry = "\(stamp) \(line)\n"
        guard let data = entry.data(using: .utf8) else { return }
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url)
        }
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

    /// ASK THE ACCESSIBILITY API FIRST, AND ONLY THEN TOUCH THE CLIPBOARD.
    /// kAXSelectedText is what the focused element already knows; it costs
    /// nothing, disturbs nothing, and is instant. The synthetic Command-C is
    /// the fallback for applications that do not answer -- and a terminal
    /// running a full-screen program is exactly that case, because the text on
    /// screen belongs to the program rather than to a text field the system
    /// can read.
    ///
    /// Both need the same permission, so this is not a way to avoid the prompt.
    /// It is a way to avoid overwriting somebody's clipboard when there was
    /// never any need to.
    static func readViaAccessibility() -> String? {
        var focused: AnyObject?
        let system = AXUIElementCreateSystemWide()
        guard AXUIElementCopyAttributeValue(
                system, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let element = focused else { return nil }

        var selected: AnyObject?
        guard AXUIElementCopyAttributeValue(
                element as! AXUIElement, kAXSelectedTextAttribute as CFString,
                &selected) == .success,
              let text = selected as? String,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return nil }
        return text
    }

    static func read(completion: @escaping (String?) -> Void) {
        guard isPermitted else { completion(nil); return }

        if let text = readViaAccessibility() {
            Log.write("selection: accessibility, \(text.count) characters")
            completion(text)
            return
        }

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
                let copied = board.changeCount != before
                restore(saved)
                Log.write(copied
                    ? "selection: pasteboard, \(text?.count ?? 0) characters"
                    : "selection: nothing -- no accessible selection and Command-C copied nothing")
                completion(copied ? text : nil)
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
    private var loginItem: NSMenuItem?
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

        // NO SETTINGS WINDOW FOR TWO CONTROLS. A window is a thing to find,
        // open, and close; a menu that is already open is not. It earns one
        // when there is something in it that a menu cannot express -- a server
        // URL and a key, which is the Calliope section and does not exist yet.
        let login = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin),
                               keyEquivalent: "")
        login.target = self
        loginItem = login
        menu.addItem(login)

        menu.addItem(speedMenu())
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
        let front = NSWorkspace.shared.frontmostApplication?.localizedName ?? "unknown"
        Log.write("hotkey pressed, frontmost is \(front)")
        guard Selection.isPermitted else {
            Log.write("no Accessibility permission; asking")
            Selection.requestPermission()
            return
        }
        Selection.read { [weak self] text in
            guard let text, !text.isEmpty else { return }
            self?.speaker.speak(text)
        }
    }

    @objc private func stopSpeaking() { speaker.stop() }

    /// THE SAME KEY THE PLAYER ALREADY READS, in the same suite. The speed is
    /// the player's setting and always was; the daemon only offers a second
    /// place to change it, because the player is gone by the time you have an
    /// opinion about how fast it was going.
    private func speedMenu() -> NSMenuItem {
        let parent = NSMenuItem(title: "Speed", action: nil, keyEquivalent: "")
        let menu = NSMenu()
        let current = settings.object(forKey: "speed") as? Double ?? 1.0
        for step in [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0] {
            let item = NSMenuItem(title: step == 1.0 ? "1× (normal)" : "\(step)×",
                                  action: #selector(setSpeed(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = step
            item.state = abs(step - current) < 0.01 ? .on : .off
            menu.addItem(item)
        }
        parent.submenu = menu
        return parent
    }

    @objc private func setSpeed(_ sender: NSMenuItem) {
        guard let step = sender.representedObject as? Double else { return }
        settings.set(step, forKey: "speed")
        // The player reads this when it starts, so the next passage takes it.
        // Changing it mid-sentence deliberately does nothing to the one being
        // read: the capsule has its own control for that, and two controls
        // fighting over one utterance is worse than a change that waits.
    }

    /// SMAppService, NOT A LaunchAgent plist. The plist is a file to write, to
    /// keep in step with wherever the binary moved to, and to remember to
    /// remove; this is one call, and macOS shows it to the owner in the same
    /// list as everything else that opens at login.
    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
        } catch {
            Log.write("open at login: \(error.localizedDescription)")
        }
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

extension Daemon: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        loginItem?.state = SMAppService.mainApp.status == .enabled ? .on : .off
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
/// Held for the life of the process: a cancelled source stops delivering.
var signalSources: [DispatchSourceSignal] = []

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

// A BARE SIGTERM DOES NOT RUN applicationWillTerminate, AND THE SERVER IS THE
// ONE THING THAT MUST NOT BE LEFT BEHIND. This daemon starts it with
// CALLIOPE_IDLE_SECONDS=0, so a server orphaned by `pkill` or by a reinstall
// never times itself out: it holds the port, the next daemon adopts it instead
// of starting its own, and the model in memory is whichever build happened to
// be current when it started. Measured the first time this daemon was replaced
// on a running machine -- the daemon went, the server stayed.
//
// A DispatchSourceSignal, not signal(2): the handler runs on a real queue
// rather than in a signal context, so it may touch Process and Foundation.
// SIG_IGN first, because a source does not displace the default action.
for number in [SIGTERM, SIGINT] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { NSApp.terminate(nil) }
    source.resume()
    signalSources.append(source)
}

app.run()
