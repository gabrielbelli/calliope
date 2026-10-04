// The Settings window: General, Voices, Engines, Proxy, Integrations.
//
// SHAPED LIKE THE SYSTEM'S OWN WINDOWS ON macOS 26 AND 27. A transparent
// window over the blurred desktop, the pages listed on that backdrop, and the
// content on a rounded card inset from the edges, with grouped forms that
// scroll inside a window that keeps its size. The first version was toolbar tabs over panes
// that sized the window to their content, and content that arrives late (the
// voice list, a connection test, the model list) made the window grow under
// the pointer, or not grow and cut the last line off: seen in a screenshot and
// called unpolished, which it was. A fixed window cannot jump.
//
// GLASS WHERE THE SYSTEM PUTS IT, NOT BEHIND TEXT. Liquid Glass is the
// functional layer -- the page-wide action on the card's top bar, the window's
// own buttons -- and the rows sit on the card's denser, quieter surface,
// because glass behind text costs legibility for nothing. A first version used
// a stock split view in an opaque window and looked like an older macOS next
// to the apps around it: seen in a screenshot beside OpenClip's settings.
//
// A FILE OF ITS OWN SO IT CAN BE LOOKED AT. The daemon cannot be launched to
// test it -- it takes the menu bar, the hotkey and the port -- so this file
// depends on nothing of the daemon's but the SettingsHost protocol below, and
// tests/harness/settings/main.swift renders every pane to an image off screen
// with a stand-in host.
import AppKit
import SwiftUI

/// What the window needs from the daemon, and nothing else.
protocol SettingsHost: AnyObject {
    var loginEnabled: Bool { get }
    func setLogin(_ on: Bool)
    var accessibilityGranted: Bool { get }
    func requestAccessibility()
    func openLog()
    /// Why the proxy is not running, when it is not.
    var serverProblem: String? { get }
    /// Restart the proxy, which reads its settings only when it starts.
    func restartServer()
    var calliopeURL: String { get set }
    var calliopeKey: String { get set }
    var calliopeOn: Bool { get set }
    /// The switch is on and both the address and the key are there.
    var calliopeConfigured: Bool { get }
    /// Why the last key could not be saved, when it could not.
    var calliopeKeyProblem: String? { get }
}

// MARK: - Asking the proxy

/// The proxy on loopback, which is the only thing this window talks to.
///
/// THE ANSWER COMES FROM THE PROXY, NOT FROM THE CALLIOPE SERVER DIRECTLY. The
/// window could reach the server itself and get a faster, prettier yes -- and
/// it would be testing a path nothing uses. What matters is whether the thing
/// the player talks to can reach it, with the credential the daemon handed it.
enum Proxy {
    static func get(_ path: String, timeout: TimeInterval = 3,
                    completion: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: localServerURL + path) else { return completion(nil) }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        URLSession.shared.dataTask(with: request) { data, _, _ in
            let body = data.flatMap { (try? JSONSerialization.jsonObject(with: $0)) as? [String: Any] }
            DispatchQueue.main.async { completion(body) }
        }.resume()
    }

    /// Asked for a while rather than once: after a restart the proxy needs a
    /// moment to bind its port, and "no answer" a quarter-second after saving
    /// would be a lie about the setting.
    static func whenUp(within seconds: Double, _ completion: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(seconds)
        func ask() {
            guard let url = URL(string: localServerURL + "/health") else { return completion(false) }
            var request = URLRequest(url: url)
            request.timeoutInterval = 1
            URLSession.shared.dataTask(with: request) { _, response, _ in
                let up = (response as? HTTPURLResponse)?.statusCode == 200
                DispatchQueue.main.async {
                    if up { return completion(true) }
                    guard Date() < deadline else { return completion(false) }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: ask)
                }
            }.resume()
        }
        ask()
    }
}

/// A voice the proxy offers, and the side it runs on.
struct OfferedVoice: Hashable {
    let name: String
    let origin: VoiceOrigin
    let language: String?

    var ref: String { VoiceRef(origin: origin, name: name).ref }
}

/// "af_heart" -> "Heart (American, female)".
///
/// KOKORO'S NAMES ARE CODES, and a menu of codes asks somebody to know that the
/// second letter is a sex and the first is a country. The name itself stays in
/// the tooltip, because it is what the command line and the API take.
func voiceTitle(_ name: String) -> String {
    let parts = name.split(separator: "_", maxSplits: 1).map(String.init)
    guard parts.count == 2, parts[0].count == 2 else { return name }
    let tag = Array(parts[0])
    var notes: [String] = []
    if tag[0] == "a" { notes.append("American") }
    if tag[0] == "b" { notes.append("British") }
    if tag[1] == "f" { notes.append("female") }
    if tag[1] == "m" { notes.append("male") }
    let base = parts[1].replacingOccurrences(of: "_", with: " ").capitalized
    return notes.isEmpty ? base : "\(base) (\(notes.joined(separator: ", ")))"
}

/// THE SIDE IS IN THE TITLE, NOT ONLY IN THE MENU'S HEADINGS. The server's fast
/// voices are Kokoro's own, so "Dora (female)" exists on both sides -- and a
/// closed menu shows only the chosen item's title, where a section heading
/// never appears.
func voiceTitle(_ ref: VoiceRef) -> String {
    voiceTitle(ref.name) + (ref.origin == .mac ? " · This Mac" : " · Calliope server")
}

private var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }

/// One quick, critically damped settle for everything that changes in place:
/// a row arriving, a section opening. No bounce -- nothing here was thrown.
private var settle: Animation? { reduceMotion ? nil : .spring(response: 0.3, dampingFraction: 1) }

// MARK: - The panes

enum SettingsPane: String, CaseIterable, Identifiable {
    case general, voices, engines, api, integrations

    var id: Self { self }

    var title: String {
        switch self {
        case .general: "General"
        case .voices: "Voices"
        case .engines: "Engines"
        case .api: "Local API"
        case .integrations: "Integrations"
        }
    }

    var symbol: String {
        switch self {
        case .general: "gearshape.fill"
        case .voices: "waveform"
        case .engines: "cpu.fill"
        case .api: "curlybraces"
        case .integrations: "terminal.fill"
        }
    }

    /// The tile behind each symbol, as System Settings draws them: one colour
    /// per place, so the list is found by colour before it is read.
    var tint: Color {
        switch self {
        case .general: .gray
        case .voices: .orange
        case .engines: .blue
        case .api: .green
        case .integrations: .indigo
        }
    }
}

/// One step of the connection test, as the proxy reported it.
struct Check: Identifiable, Equatable {
    let id: String
    let ok: Bool
    let detail: String
    let ms: Int?

    var title: String {
        ["reach": "Server", "key": "Key", "voices": "Voices", "speech": "Speech"][id] ?? id
    }
}

// MARK: - What the window shows, and what it changes

/// Every value the panes show, read back from where it is kept.
///
/// SETTING A VALUE HERE CHANGES NOTHING; THE ACTIONS DO. refresh() assigns
/// every field when the window opens, and a field whose assignment restarted
/// the proxy would restart it five times on the way in. So the switches write
/// through the methods below, which save and restart deliberately.
@MainActor
final class SettingsModel: ObservableObject {
    weak var host: SettingsHost?

    @Published var pane: SettingsPane? = .general

    // General
    @Published var openAtLogin = false
    @Published var speed = 1.0
    @Published var accessibility = false

    // Voices
    @Published var chosen: [String: String] = [:]
    @Published var offered: [OfferedVoice] = []
    @Published var voicesLoaded = false
    @Published var voicesAnswered = false

    // Engines
    @Published var macOn = true
    @Published var keepLoaded = false
    @Published var macLoaded: Bool?
    @Published var calliopeOn = false
    @Published var url = ""
    @Published var key = ""
    @Published var testing = false
    @Published var testMessage = ""
    @Published var testPassed: Bool?
    @Published var checks: [Check] = []

    // Proxy
    @Published var port = Preference.defaultPort
    @Published var proxyUp: Bool?
    @Published var localModels: [String] = []
    @Published var remoteModels: [String] = []

    // Integrations
    @Published var cliPath: String?
    @Published var cliProblem: String?
    @Published var skillPresent = false
    @Published var openClipPresent = false
    @Published var openClipInstalled = false
    @Published var openClipNote: String?

    static let speeds = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
    static let example = "calliope speak --reader -f explanation.md"

    private let home = FileManager.default.homeDirectoryForCurrentUser
    private var cliLink: URL { home.appendingPathComponent(".local/bin/calliope") }
    private var bundledCLI: URL { appURL.appendingPathComponent("Contents/Resources/cli/calliope") }
    var bundledSkill: URL {
        appURL.appendingPathComponent("Contents/Resources/skill/calliope-voice/SKILL.md")
    }
    private var openClip: URL { home.appendingPathComponent(".openclip") }
    private var extensionDir: URL { openClip.appendingPathComponent("extensions/calliope.openclipext") }
    private var bundledExtension: URL { appURL.appendingPathComponent("Contents/Resources/openclip") }

    var proxyAddress: String { localServerURL }
    var bothOff: Bool { !macOn && !calliopeOn }
    var appVersion: String? {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    }

    /// Everything read back, because a permission granted in System Settings
    /// or a server restarted happened outside this window.
    func refresh() {
        guard let host else { return }
        openAtLogin = host.loginEnabled
        speed = preferences.object(forKey: "speed") as? Double ?? 1.0
        accessibility = host.accessibilityGranted
        chosen = Preference.voices
        macOn = Preference.macOn
        keepLoaded = Preference.keepLoaded
        calliopeOn = host.calliopeOn
        url = host.calliopeURL
        key = host.calliopeKey
        port = Preference.port
        readIntegrations()
        loadVoices()
        liveRefresh()
    }

    /// The few things that change while the window is open, asked every two
    /// seconds while it is visible and never while it is closed.
    func liveRefresh() {
        accessibility = host?.accessibilityGranted ?? false
        Proxy.get("/status", timeout: 1) { [weak self] body in
            guard let self else { return }
            let mac = body?["mac"] as? [String: Any]
            self.macLoaded = mac?["loaded"] as? Bool
            self.proxyUp = self.host?.serverProblem == nil && body != nil
        }
        Proxy.get("/v1/models", timeout: 1) { [weak self] body in
            guard let self, let rows = body?["data"] as? [[String: Any]] else { return }
            let local = rows.filter { $0["owned_by"] as? String == "calliope-local" }
                .compactMap { $0["id"] as? String }
            let remote = rows.filter { $0["owned_by"] as? String == "calliope-remote" }
                .compactMap { $0["id"] as? String }
                .map { $0.hasPrefix("calliope/") ? String($0.dropFirst(9)) : $0 }
            if local != self.localModels { self.localModels = local }
            if remote != self.remoteModels { self.remoteModels = remote }
        }
    }

    var proxyProblem: String? { host?.serverProblem }

    // MARK: General

    func setLogin(_ on: Bool) {
        host?.setLogin(on)
        openAtLogin = host?.loginEnabled ?? on
    }

    /// THE SAME KEY THE MENU WRITES AND THE PLAYER READS: the next passage
    /// takes it, and the capsule has its own control for the one being read.
    func setSpeed(_ step: Double) {
        speed = step
        preferences.set(step, forKey: "speed")
    }

    func askForAccess() {
        host?.requestAccessibility()
        // Granted in System Settings, in their own time: noticed, not assumed.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.liveRefresh() }
    }

    func openLog() { host?.openLog() }

    // MARK: Voices

    func loadVoices() {
        Proxy.get("/voices") { [weak self] body in
            guard let self else { return }
            let detail = body?["detail"] as? [[String: Any]] ?? []
            let voices = detail.compactMap { row -> OfferedVoice? in
                guard let name = row["name"] as? String,
                      let origin = (row["origin"] as? String).flatMap(VoiceOrigin.init) else { return nil }
                return OfferedVoice(name: name, origin: origin, language: row["language"] as? String)
            }
            withAnimation(settle) {
                self.offered = voices
                self.voicesAnswered = body != nil
                self.voicesLoaded = true
            }
        }
    }

    /// A language's menu offers only that language's voices: the letter is
    /// what the server derives the phonemiser from.
    func voices(for spoken: Language, on origin: VoiceOrigin) -> [OfferedVoice] {
        offered.filter { $0.origin == origin && $0.language == spoken.code }
            .sorted { $0.name < $1.name }
    }

    var configured: [Language] { languages.filter { chosen[$0.code] != nil } }
    var remaining: [Language] { languages.filter { chosen[$0.code] == nil } }

    func choose(_ ref: String, for spoken: Language) {
        chosen[spoken.code] = ref
        Preference.voices = chosen
    }

    func remove(_ spoken: Language) {
        withAnimation(settle) { chosen[spoken.code] = nil }
        Preference.voices = chosen
    }

    /// A new row starts on the standard voice, on whichever side is on: a row
    /// that starts empty is a setting that does nothing until it is touched.
    func add(_ spoken: Language) {
        let origin: VoiceOrigin = macOn || !calliopeOn ? .mac : .calliope
        withAnimation(settle) {
            chosen[spoken.code] = VoiceRef(origin: origin, name: spoken.standardVoice).ref
        }
        Preference.voices = chosen
    }

    // MARK: Engines

    func setMac(_ on: Bool) {
        withAnimation(settle) { macOn = on }
        Preference.macOn = on
        host?.restartServer()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.loadVoices()
            self?.liveRefresh()
        }
    }

    func setKeepLoaded(_ on: Bool) {
        keepLoaded = on
        Preference.keepLoaded = on
        host?.restartServer()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.liveRefresh() }
    }

    func setCalliope(_ on: Bool) {
        withAnimation(settle) { calliopeOn = on }
        host?.calliopeOn = on
        commitCalliope(force: true)
    }

    /// Save both, restart the proxy that reads them, then test the round trip.
    ///
    /// ONLY WHEN SOMETHING CHANGED. Both fields commit on leaving them, so
    /// tabbing from the address to the key fired this twice -- and a restart
    /// takes the proxy away from whatever is being read aloud.
    func commitCalliope(force: Bool = false) {
        guard let host else { return }
        url = url.trimmingCharacters(in: .whitespaces)
        key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        let changed = url != host.calliopeURL || key != host.calliopeKey
        host.calliopeURL = url
        host.calliopeKey = key
        guard changed || force else { return }
        host.restartServer()
        withAnimation(settle) { checks = [] }
        testPassed = nil
        if let problem = host.calliopeKeyProblem {
            testMessage = problem
            testPassed = false
            return
        }
        guard host.calliopeConfigured else {
            testPassed = false
            if !host.calliopeOn {
                testMessage = ""
            } else if url.isEmpty {
                testMessage = "Where is it? Enter the server's address."
            } else {
                testMessage = "A key is required. The server answers nothing without one."
            }
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.testConnection()
            self?.loadVoices()
        }
    }

    /// The Test button, and what every save that completes the address runs.
    ///
    /// FOUR STEPS, STOPPING AT THE FIRST THAT FAILS, because each one is only
    /// meaningful if the one before it worked: reachable, then the key, then the
    /// voices it offers, then a word actually spoken. "It did not work" is not
    /// something anybody can act on; "the key was not accepted" is.
    func testConnection() {
        guard host?.calliopeConfigured == true else {
            testMessage = host?.calliopeOn == true
                ? "Fill in the address and the key first." : "The Calliope server is off."
            return
        }
        testing = true
        testPassed = nil
        testMessage = "Testing…"
        withAnimation(settle) { checks = [] }
        Proxy.whenUp(within: 10) { [weak self] up in
            guard let self else { return }
            guard up else {
                self.finishTest(false, "Calliope's local service did not start, so nothing could be tested.", [])
                return
            }
            Proxy.get("/calliope/test", timeout: 40) { body in
                let steps = (body?["checks"] as? [[String: Any]] ?? []).map { step in
                    Check(id: step["name"] as? String ?? "?", ok: step["ok"] as? Bool == true,
                          detail: step["detail"] as? String ?? "", ms: step["ms"] as? Int)
                }
                self.finishTest(body?["ok"] as? Bool == true,
                                body?["message"] as? String ?? "No answer from Calliope's local service.", steps)
            }
        }
    }

    private func finishTest(_ passed: Bool, _ message: String, _ steps: [Check]) {
        testing = false
        testPassed = passed
        testMessage = message
        withAnimation(settle) { checks = steps }
    }

    func openCalliope() {
        guard let target = URL(string: url), target.scheme?.hasPrefix("http") == true else { return }
        NSWorkspace.shared.open(target)
    }

    // MARK: Proxy

    func copyAddress() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(localServerURL, forType: .string)
    }

    func savePort() {
        guard (1024...65535).contains(port) else { port = Preference.port; return }
        guard port != Preference.port else { return }
        Preference.port = port
        host?.restartServer()
        proxyUp = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.liveRefresh() }
    }

    // MARK: Integrations

    private func readIntegrations() {
        let files = FileManager.default
        let brewed = ["/opt/homebrew/bin/calliope", "/usr/local/bin/calliope"]
            .first { files.fileExists(atPath: $0) }
        if files.fileExists(atPath: cliLink.path) {
            cliPath = "~/.local/bin/calliope"
        } else {
            cliPath = brewed
        }
        skillPresent = files.fileExists(atPath: bundledSkill.path)
        openClipPresent = files.fileExists(atPath: openClip.path)
        openClipInstalled = files.fileExists(atPath: extensionDir.appendingPathComponent("openclip.json").path)
    }

    var canInstallCLI: Bool { FileManager.default.fileExists(atPath: bundledCLI.path) }
    var canInstallOpenClip: Bool { FileManager.default.fileExists(atPath: bundledExtension.path) }

    func installCommand() {
        let files = FileManager.default
        do {
            try files.createDirectory(at: cliLink.deletingLastPathComponent(),
                                      withIntermediateDirectories: true)
            try? files.removeItem(at: cliLink)
            try files.createSymbolicLink(at: cliLink, withDestinationURL: bundledCLI)
            cliProblem = nil
        } catch {
            cliProblem = "Could not install: \(error.localizedDescription)"
        }
        withAnimation(settle) { readIntegrations() }
    }

    func copyExample() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(Self.example, forType: .string)
    }

    func revealSkill() {
        NSWorkspace.shared.activateFileViewerSelecting([bundledSkill])
    }

    /// The same three files install.sh writes, from the copies inside this app,
    /// with the app's own location written into the script -- so an extension
    /// put back from here launches this Calliope, wherever it was installed.
    ///
    /// REINSTALL, NOT ONLY INSTALL. An extension that is there can still be
    /// wrong -- an older copy, a file edited by hand -- and putting the bundled
    /// one back is the repair for all of them.
    func installOpenClip() {
        let files = FileManager.default
        do {
            try files.createDirectory(at: extensionDir, withIntermediateDirectories: true)
            for name in ["openclip.json", "icon.svg"] {
                let target = extensionDir.appendingPathComponent(name)
                try? files.removeItem(at: target)
                try files.copyItem(at: bundledExtension.appendingPathComponent(name), to: target)
            }
            let script = try String(contentsOf: bundledExtension.appendingPathComponent("calliope.py"),
                                    encoding: .utf8)
                .replacingOccurrences(of: "__APP__", with: appURL.path)
            let target = extensionDir.appendingPathComponent("calliope.py")
            try script.write(to: target, atomically: true, encoding: .utf8)
            try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.path)
            // OPENCLIP REMEMBERS AN ICON IT COULD NOT READ for as long as it
            // runs: its LocalIconCache stores the failure by path, so a fixed
            // file at the same path still shows "?" until OpenClip restarts.
            // Seen, then read in its source; a restart is the only way to clear
            // it from outside. It also trusts an extension by a hash of its
            // files, so it asks once more.
            openClipNote = "Reinstalled. Quit and reopen OpenClip to see the new icon, then "
                + "trust the extension when it asks."
        } catch {
            openClipNote = "Could not install: \(error.localizedDescription)"
        }
        withAnimation(settle) { readIntegrations() }
    }
}

// MARK: - The window's content

/// The window's backdrop: the desktop, blurred, behind everything.
///
/// THE SAME MATERIAL THE SYSTEM'S OWN SIDEBARS ARE MADE OF, blended behind the
/// window rather than within it, so the sidebar sits on what is behind the
/// window the way Finder's and System Settings' do on this system. A dim layer
/// over it keeps the text legible on a bright wallpaper; with Reduce
/// Transparency on, it is the plain window colour instead.
private struct Backdrop: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .sidebar
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

/// Translucent fills that follow light and dark, defined once.
private enum Tone {
    static func dynamic(dark: NSColor, light: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        })
    }

    /// Over the backdrop, under everything: enough to hold contrast on any wallpaper.
    static let scrim = dynamic(dark: NSColor(white: 0, alpha: 0.14), light: NSColor(white: 1, alpha: 0.18))
    /// The content card: a smoked pane a step denser than the sidebar around it.
    static let card = dynamic(dark: NSColor(white: 0.07, alpha: 0.34), light: NSColor(white: 1, alpha: 0.42))
    /// A panel of rows on the card: a light veil, not a slab, so the wallpaper
    /// still shows through it.
    static let panel = dynamic(dark: NSColor(white: 1, alpha: 0.045), light: NSColor(white: 1, alpha: 0.38))
    static let panelEdge = dynamic(dark: NSColor(white: 1, alpha: 0.07), light: NSColor(white: 0, alpha: 0.07))
    static let divider = dynamic(dark: NSColor(white: 1, alpha: 0.07), light: NSColor(white: 0, alpha: 0.08))
    static let cardEdge = dynamic(dark: NSColor(white: 1, alpha: 0.09), light: NSColor(white: 0, alpha: 0.09))
    /// The chosen row, as a quiet highlight: the accent colour is for controls.
    static let selected = dynamic(dark: NSColor(white: 1, alpha: 0.12), light: NSColor(white: 0, alpha: 0.08))
    static let hovered = dynamic(dark: NSColor(white: 1, alpha: 0.05), light: NSColor(white: 0, alpha: 0.04))
}

private enum Metric {
    static let sidebarWidth: CGFloat = 208
    /// Room at the top of the sidebar for the window's own buttons.
    static let trafficLights: CGFloat = 52
    static let cardInset: CGFloat = 8
    static let cardRadius: CGFloat = 22
    static let tile: CGFloat = 20
}

struct SettingsView: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        HStack(spacing: 0) {
            SettingsSidebar(model: model)
                .frame(width: Metric.sidebarWidth)
            detail
        }
        .background {
            if NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency {
                Color(nsColor: .windowBackgroundColor)
            } else {
                ZStack { Backdrop(); Tone.scrim }
            }
        }
        .ignoresSafeArea()
    }

    private var pane: SettingsPane { model.pane ?? .general }

    /// The content, on a rounded card inset from the window's edges, with the
    /// pane's name and its one page-wide action on a bar across the top.
    private var detail: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text(pane.title)
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                PaneAction(model: model, pane: pane)
            }
            .padding(.horizontal, 20)
            .frame(height: 48)

            Group {
                switch pane {
                case .general: GeneralPane(model: model)
                case .voices: VoicesPane(model: model)
                case .engines: EnginesPane(model: model)
                case .api: LocalAPIPane(model: model)
                case .integrations: IntegrationsPane(model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Tone.card, in: .rect(cornerRadius: Metric.cardRadius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Metric.cardRadius, style: .continuous)
            .strokeBorder(Tone.cardEdge, lineWidth: 0.5))
        .padding([.top, .trailing, .bottom], Metric.cardInset)
    }
}

/// The page-wide action, as a Liquid Glass capsule on the card's top bar --
/// where the system puts the controls that act on the page as a whole. Only
/// panes that have one show it; the rest leave the bar to their title.
private struct PaneAction: View {
    @ObservedObject var model: SettingsModel
    let pane: SettingsPane

    var body: some View {
        switch pane {
        case .voices:
            Menu {
                ForEach(model.remaining, id: \.code) { spoken in
                    Button(spoken.name) { model.add(spoken) }
                }
            } label: {
                Label("Add Language", systemImage: "plus")
                    .font(.system(size: 12, weight: .medium))
            }
            .menuStyle(.button)
            .buttonStyle(.glass)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(model.remaining.isEmpty)
        case .api:
            Button {
                model.copyAddress()
            } label: {
                Label("Copy Address", systemImage: "doc.on.doc")
                    .font(.system(size: 12, weight: .medium))
            }
            .buttonStyle(.glass)
        default:
            EmptyView()
        }
    }
}

/// The pages, as rows on the window's backdrop: a coloured tile and a name,
/// with a quiet highlight for the one on screen.
private struct SettingsSidebar: View {
    @ObservedObject var model: SettingsModel
    @State private var hovered: SettingsPane?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(SettingsPane.allCases) { pane in
                row(pane)
            }
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                MarkImage(size: 18)
                Text("Calliope" + (model.appVersion.map { " " + $0 } ?? ""))
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 10)
            .padding(.bottom, 14)
        }
        .padding(.top, Metric.trafficLights)
        .padding(.horizontal, 10)
    }

    private func row(_ pane: SettingsPane) -> some View {
        let chosen = (model.pane ?? .general) == pane
        return Button {
            withAnimation(settle) { model.pane = pane }
        } label: {
            HStack(spacing: 9) {
                Image(systemName: pane.symbol)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: Metric.tile, height: Metric.tile)
                    .background(pane.tint.gradient, in: .rect(cornerRadius: 5, style: .continuous))
                Text(pane.title)
                    .font(.system(size: 13, weight: chosen ? .semibold : .medium))
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .frame(height: 30)
            .background(chosen ? Tone.selected : (hovered == pane ? Tone.hovered : .clear),
                        in: .rect(cornerRadius: 8, style: .continuous))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .onHover { inside in hovered = inside ? pane : (hovered == pane ? nil : hovered) }
        .accessibilityAddTraits(chosen ? .isSelected : [])
    }
}

// MARK: - Panels

/// A titled group of rows on a translucent panel, with a note underneath.
///
/// OUR OWN, NOT THE STOCK GROUPED FORM, AND FOR ONE REASON: THE FILL. The
/// grouped form draws each section on a near-opaque surface, so the wallpaper
/// the window is built to show through stopped at the first section -- seen
/// beside OpenClip's settings, which let it through and looked of a piece with
/// the desktop. These panels are a light veil over the card instead, with a
/// hairline between rows.
private struct Panel<Content: View>: View {
    var title: String?
    var footer: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .padding(.leading, 4)
                    .accessibilityAddTraits(.isHeader)
            }
            VStack(spacing: 0) {
                Group(subviews: content) { rows in
                    ForEach(rows) { row in
                        if row.id != rows.first?.id {
                            Tone.divider.frame(height: 0.5).padding(.leading, 14)
                        }
                        row
                    }
                }
            }
            .background(Tone.panel, in: .rect(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Tone.panelEdge, lineWidth: 0.5))
            if let footer {
                Text(footer)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 4)
            }
        }
    }
}

/// One row: a name, an optional quieter line under it, and its control on the right.
private struct Row<Trailing: View>: View {
    let title: String
    var detail: String?
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13))
                if let detail {
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 12)
            trailing
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .frame(minHeight: 40)
    }
}

/// A pane's content: panels in a column that scrolls inside the card.
private struct PaneScroll<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) { content }
                .padding(.horizontal, 20)
                .padding(.top, 4)
                .padding(.bottom, 20)
        }
        .scrollIndicators(.automatic)
    }
}

private struct Switch: View {
    let isOn: Binding<Bool>

    var body: some View {
        Toggle("", isOn: isOn).labelsHidden().toggleStyle(.switch)
    }
}

private struct MarkImage: View {
    let size: CGFloat

    var body: some View {
        Image(nsImage: NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            Mark.draw(in: context, size: rect.width, monochrome: false)
            return true
        })
        .accessibilityHidden(true)
    }
}

// MARK: General

private struct GeneralPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        PaneScroll {
            // THE SAME MARK AS THE PAGE, THE MENU BAR AND THE ICON, in colour
            // here because this is the one place it is shown at a size that can
            // carry it -- where System Settings puts the account it belongs to.
            Panel {
                HStack(spacing: 14) {
                    MarkImage(size: 52)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Calliope").font(.title2.weight(.semibold))
                        Text("Reads text aloud, on this Mac or with your own Calliope server.")
                            .foregroundStyle(.secondary)
                        if let version = model.appVersion {
                            Text("Version \(version)").font(.caption).foregroundStyle(.tertiary)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(14)
            }

            Panel {
                Row(title: "Open Calliope at login") {
                    Switch(isOn: Binding(get: { model.openAtLogin }, set: { model.setLogin($0) }))
                }
                Row(title: "Reading speed") {
                    Picker("Reading speed", selection: Binding(
                        get: { model.speed }, set: { model.setSpeed($0) })) {
                        ForEach(SettingsModel.speeds, id: \.self) { step in
                            Text(step == 1.0 ? "1× (normal)" : "\(step.formatted())×").tag(step)
                        }
                    }
                    .labelsHidden()
                    .fixedSize()
                }
                Row(title: "Shortcut", detail: "Reads the selected text aloud.") {
                    Text("⌥⌘S").font(.body.monospaced()).foregroundStyle(.secondary)
                }
            }

            Panel {
                Row(title: "Accessibility",
                    detail: "Needed to read another app's selection. macOS asks again if the app "
                        + "is signed differently.") {
                    if model.accessibility {
                        Label("Granted", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    } else {
                        Button("Grant Access…") { model.askForAccess() }
                    }
                }
                Row(title: "Troubleshooting", detail: "What the shortcut did each time, and why.") {
                    Button("Open Log") { model.openLog() }
                }
            }
        }
    }
}

// MARK: Voices

/// A voice per language, for the languages somebody actually reads.
///
/// ROWS ARE ADDED, NOT PRE-FILLED. Eight rows for eight languages makes
/// somebody who reads English and Portuguese answer six questions about
/// languages they never read; a language without a row is read with its
/// standard voice, which is what happened before there was a choice at all.
private struct VoicesPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        PaneScroll {
            Panel(title: "Languages",
                  footer: "Calliope detects the language of what it reads. Choose a voice for the "
                      + "languages you read; any other language uses its standard voice. A voice from "
                      + "the Calliope server is spoken there, so it needs the server on and reachable.") {
                if model.configured.isEmpty {
                    Row(title: "No languages yet",
                        detail: "Every language is read with its standard voice. Add Language chooses "
                            + "one for a language you read.") { EmptyView() }
                }
                ForEach(model.configured, id: \.code) { spoken in
                    Row(title: spoken.name) {
                        HStack(spacing: 6) {
                            voicePicker(spoken)
                            Button {
                                model.remove(spoken)
                            } label: {
                                Image(systemName: "minus.circle.fill")
                                    .symbolRenderingMode(.hierarchical)
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.borderless)
                            .help("Use the standard voice for \(spoken.name)")
                            .accessibilityLabel("Remove \(spoken.name)")
                        }
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            Text(availability)
                .font(.callout)
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 4)
        }
    }

    private var availability: String {
        guard model.voicesLoaded else { return "Asking for the voices…" }
        guard model.voicesAnswered else {
            return "Calliope's local service is not answering, so only your saved choices are shown."
        }
        let mac = model.offered.filter { $0.origin == .mac }.count
        let remote = model.offered.count - mac
        if model.offered.isEmpty { return "No voices: both engines are off. Turn one on in Engines." }
        return "\(mac) voices on this Mac" + (remote > 0 ? ", \(remote) on the Calliope server." : ".")
    }

    @ViewBuilder
    private func voicePicker(_ spoken: Language) -> some View {
        let chosen = model.chosen[spoken.code] ?? ""
        let mac = model.voices(for: spoken, on: .mac)
        let remote = model.voices(for: spoken, on: .calliope)
        // A SAVED CHOICE IS SHOWN EVEN WHEN IT IS NOT ON OFFER RIGHT NOW. The
        // server may be down, or switched off for the afternoon; quietly
        // replacing the choice with whatever happens to be listed would lose it.
        let offeredNow = (mac + remote).contains { $0.ref == chosen }
        Picker(spoken.name, selection: Binding(
            get: { chosen }, set: { model.choose($0, for: spoken) })) {
            if !offeredNow, let ref = VoiceRef(chosen) {
                Text(voiceTitle(ref) + " — not available now").tag(chosen)
            }
            if !mac.isEmpty {
                Section("This Mac") {
                    ForEach(mac, id: \.ref) { voice in
                        Text(voiceTitle(VoiceRef(origin: .mac, name: voice.name))).tag(voice.ref)
                    }
                }
            }
            if !remote.isEmpty {
                Section("Calliope server") {
                    ForEach(remote, id: \.ref) { voice in
                        Text(voiceTitle(VoiceRef(origin: .calliope, name: voice.name))).tag(voice.ref)
                    }
                }
            }
        }
        .labelsHidden()
        .fixedSize()
        .help(chosen)
    }
}

// MARK: Engines

/// The two sides that can speak, each behind its own switch.
///
/// THIS MAC IS THE WHOLE OF IT UNLESS SOMEBODY SAYS OTHERWISE. With the Calliope
/// switch off nothing here touches the network, and with "Keep loaded" off the
/// 450 MB model is in memory only while it is being used: Calliope's footprint
/// at rest is the menu bar item and a small local service.
private struct EnginesPane: View {
    @ObservedObject var model: SettingsModel
    @FocusState private var focus: Field?

    private enum Field { case url, key }

    var body: some View {
        PaneScroll {
            if model.bothOff {
                Panel {
                    Row(title: "Both engines are off, so Calliope cannot speak.") {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                }
            }

            Panel(title: "This Mac") {
                Row(title: "Speak on this Mac",
                    detail: "Kokoro on this Mac's processor. Private, and it works offline.") {
                    Switch(isOn: Binding(get: { model.macOn }, set: { model.setMac($0) }))
                }
                if model.macOn {
                    Row(title: "Status") { Text(macStatus).foregroundStyle(.secondary) }
                    Row(title: "Keep the voice loaded",
                        detail: "The first word comes at once, and it holds about 450 MB of memory. "
                            + "Otherwise it loads when needed, in about 2 seconds, and is released "
                            + "after 10 minutes unused.") {
                        Switch(isOn: Binding(get: { model.keepLoaded }, set: { model.setKeepLoaded($0) }))
                    }
                }
            }

            Panel(title: "Calliope server",
                  footer: model.calliopeOn
                      ? "Make a key in Calliope under Account › API keys › New key. Preset user covers "
                          + "speech and transcription; user-jobs adds long documents."
                      : nil) {
                Row(title: "Use a Calliope server",
                    detail: "Your own server: more voices, transcription and long documents. "
                        + "Nothing leaves this Mac while this is off.") {
                    Switch(isOn: Binding(get: { model.calliopeOn }, set: { model.setCalliope($0) }))
                }
                if model.calliopeOn {
                    // NO SAVE BUTTON. The fields commit themselves, on Return
                    // and on leaving them -- typing an address and then
                    // pressing a second thing is a step more than the switch
                    // promised.
                    Row(title: "Address") {
                        TextField("Address", text: $model.url,
                                  prompt: Text(verbatim: "https://calliope.example.com"))
                            .labelsHidden()
                            .textFieldStyle(.plain)
                            .multilineTextAlignment(.trailing)
                            .frame(maxWidth: 300)
                            .focused($focus, equals: .url)
                            .onSubmit { model.commitCalliope() }
                    }
                    Row(title: "API key") {
                        SecureField("API key", text: $model.key, prompt: Text("Paste an API key"))
                            .labelsHidden()
                            .textFieldStyle(.plain)
                            .multilineTextAlignment(.trailing)
                            .frame(maxWidth: 300)
                            .focused($focus, equals: .key)
                            .onSubmit { model.commitCalliope() }
                    }
                    // THE TEST SITS WITH THE FIELDS IT TESTS. On the card's top
                    // bar it was out of sight from the key field, and the user
                    // read that as no button at all.
                    Row(title: "Connection") {
                        HStack(spacing: 8) {
                            if model.testing { ProgressView().controlSize(.small) }
                            Button("Open Calliope") { model.openCalliope() }
                                .disabled(model.url.isEmpty)
                            Button("Test Connection") {
                                focus = nil
                                model.commitCalliope()
                                model.testConnection()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.testing)
                        }
                    }
                    if !model.testMessage.isEmpty {
                        Row(title: model.testMessage) { verdict }
                            .transition(.opacity)
                    }
                    ForEach(model.checks) { check in
                        Row(title: check.title, detail: check.detail) {
                            HStack(spacing: 8) {
                                Text(check.ms.map { "\($0) ms" } ?? "")
                                    .monospacedDigit()
                                    .foregroundStyle(.tertiary)
                                Image(systemName: check.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundStyle(check.ok ? .green : .red)
                            }
                        }
                        .transition(.opacity)
                    }
                }
            }
        }
        // Leaving a field commits it, as Return does.
        .onChange(of: focus) { previous, _ in
            if previous != nil { model.commitCalliope() }
        }
    }

    private var macStatus: String {
        if let problem = model.proxyProblem { return "Calliope's local service is not running: \(problem)" }
        switch model.macLoaded {
        case .some(true): return "Loaded and ready"
        case .some(false): return "Not loaded — loads on first use"
        case .none: return "Starting…"
        }
    }

    /// A SPINNER ONLY WHILE SOMETHING IS RUNNING. It used to stand for "no
    /// verdict yet", so a message that was not a test result -- a key that is
    /// missing -- spun for ever beside it.
    @ViewBuilder
    private var verdict: some View {
        if model.testing {
            ProgressView().controlSize(.small)
        } else if model.testPassed == true {
            Image(systemName: "checkmark.seal.fill").foregroundStyle(.green)
        } else {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
        }
    }
}

// MARK: Local API

/// Calliope's own API on this Mac, for other programs to call.
///
/// NAMED FOR WHAT IT IS TO SOMEBODY READING SETTINGS. Inside, it is a proxy --
/// it answers for this Mac's engine and passes calliope/… requests on to the
/// server -- but a pane called "Proxy" read as a setting for the app's own
/// outbound connection, which it is not: the user said so. To them it is an
/// address their other apps and scripts can use.
///
/// ONE ADDRESS FOR EVERY ENGINE, AND NO KEY IN ANY OF THE PROGRAMS. The key the
/// daemon handed over stays in Calliope; a script on this Mac sends no credential.
private struct LocalAPIPane: View {
    @ObservedObject var model: SettingsModel
    @FocusState private var portFocused: Bool

    var body: some View {
        PaneScroll {
            Panel(footer: "Apps and scripts on this Mac can use Calliope at this address, in "
                    + "OpenAI's shape: speech, transcription and the model list. Voices on this Mac "
                    + "answer here; models named calliope/… are passed to your Calliope server with "
                    + "your key, so no app needs the key itself. Web pages are refused.") {
                Row(title: "Address") {
                    Text(model.proxyAddress)
                        .font(.body.monospaced())
                        .textSelection(.enabled)
                        .fixedSize()
                }
                Row(title: "Port", detail: "Changing it restarts Calliope's local service.") {
                    TextField("Port", value: $model.port, format: .number.grouping(.never))
                        .labelsHidden()
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 72)
                        .focused($portFocused)
                        .onSubmit { model.savePort() }
                }
                Row(title: "Status") {
                    Label(status, systemImage: "circle.fill")
                        .labelStyle(StatusLabel(colour: statusColour))
                }
            }

            Panel(title: "Models") {
                Row(title: "This Mac") {
                    Text(model.localModels.isEmpty ? "None" : model.localModels.joined(separator: ", "))
                        .foregroundStyle(.secondary)
                }
                Row(title: "Calliope server", detail: "Asked for as calliope/<name>") {
                    Text(model.remoteModels.isEmpty ? "None" : model.remoteModels.joined(separator: ", "))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.trailing)
                        .frame(maxWidth: 300, alignment: .trailing)
                }
            }
        }
        .onChange(of: portFocused) { wasFocused, _ in
            if wasFocused { model.savePort() }
        }
    }

    private var status: String {
        if let problem = model.proxyProblem { return "Not running: \(problem)" }
        switch model.proxyUp {
        case .some(true): return "Running"
        case .some(false): return "Not answering"
        case .none: return "Starting…"
        }
    }

    private var statusColour: Color {
        model.proxyProblem != nil ? .red : (model.proxyUp == true ? .green : .secondary)
    }
}

/// A small status dot before the words, as System Settings marks a service.
private struct StatusLabel: LabelStyle {
    let colour: Color

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.font(.system(size: 7)).foregroundStyle(colour)
            configuration.title
        }
    }
}

// MARK: Integrations

/// The command line, the agent skill and OpenClip: how other programs reach Calliope.
///
/// THE SKILL IS A FILE, NOT AN INSTALLATION. It is a SKILL.md for whoever wants
/// it, to use with whichever agent they like, in whatever way they keep their
/// own setup -- so this shows where it is and installs nothing. The command
/// goes onto the PATH only when somebody presses the button.
private struct IntegrationsPane: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        PaneScroll {
            Panel(title: "Command line", footer: commandNote) {
                Row(title: "Status") {
                    if let path = model.cliPath {
                        Text("Installed at \(path)").foregroundStyle(.secondary)
                    } else {
                        Button("Install Command") { model.installCommand() }
                            .disabled(!model.canInstallCLI)
                    }
                }
                Row(title: "Example", detail: SettingsModel.example) {
                    Button("Copy") { model.copyExample() }
                }
            }

            Panel(title: "Agent skill",
                  footer: "A SKILL.md that teaches an AI agent to write an explanation for the ear "
                      + "and read it to you with the calliope command. Use it with any agent, "
                      + "however you keep your own setup.") {
                Row(title: "File", detail: model.skillPresent ? "calliope-voice/SKILL.md" : "Not in this build") {
                    Button("Show in Finder") { model.revealSkill() }
                        .disabled(!model.skillPresent)
                }
            }

            Panel(title: "OpenClip", footer: openClipNote) {
                Row(title: "Speak action") {
                    if !model.openClipPresent {
                        Text("OpenClip is not installed").foregroundStyle(.secondary)
                    } else {
                        Button(model.openClipInstalled ? "Reinstall" : "Install") {
                            model.installOpenClip()
                        }
                        .disabled(!model.canInstallOpenClip)
                    }
                }
            }
        }
    }

    private var commandNote: String {
        var lines = ["The calliope command speaks, saves audio, transcribes files and links, and "
            + "manages glossaries and jobs, through the same local API and voices as everything else."]
        if let problem = model.cliProblem { lines.append(problem) }
        if model.cliPath == "~/.local/bin/calliope" {
            lines.append("If your shell cannot find it, add ~/.local/bin to your PATH.")
        }
        return lines.joined(separator: "\n")
    }

    private var openClipNote: String {
        model.openClipNote ?? (!model.openClipPresent
            ? "Optional: OpenClip adds Speak to a menu over any selection."
            : model.openClipInstalled
                ? "Reinstalling puts back the copy inside Calliope: the repair for a missing, stale "
                    + "or hand-edited extension."
                : "Adds Speak to OpenClip's menu.")
    }
}

// MARK: - The window

final class SettingsController: NSObject, NSWindowDelegate {
    let window: NSWindow
    let model: SettingsModel
    private var timer: Timer?

    @MainActor
    init(host: SettingsHost) {
        model = SettingsModel()
        model.host = host
        let content = NSHostingController(rootView: SettingsView(model: model))
        content.sizingOptions = [.minSize]

        // A TRANSPARENT WINDOW WITH AN EMPTY TOOLBAR, AND BOTH ARE THE POINT.
        // The toolbar is what tells this system to give the window its current
        // chrome -- the larger corners, the buttons sitting over the sidebar --
        // and the transparency is what lets the backdrop show the desktop
        // through. The title is hidden because the card's own bar carries the
        // pane's name; a unified toolbar would otherwise draw it a second time.
        window = NSWindow(contentRect: NSRect(origin: .zero, size: NSSize(width: 760, height: 560)),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Calliope Settings"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        let toolbar = NSToolbar(identifier: "CalliopeSettings")
        toolbar.showsBaselineSeparator = false
        toolbar.displayMode = .iconOnly
        window.toolbar = toolbar
        window.toolbarStyle = .unified
        window.isOpaque = false
        window.backgroundColor = .clear
        window.contentViewController = content
        // ONE SIZE, SET HERE, AND THE CONTENT SCROLLS INSIDE IT. Nothing that
        // arrives later -- a voice list, a test result -- may move the window.
        window.setContentSize(NSSize(width: 760, height: 560))
        window.contentMinSize = NSSize(width: 680, height: 460)
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("CalliopeSettings")
        super.init()
        window.delegate = self
    }

    @MainActor func refresh() { model.refresh() }

    /// Opened from the menu: everything read back, because a permission
    /// granted or a server restarted happened outside this window.
    ///
    /// AN ORDINARY APP WHILE THE WINDOW IS OPEN. Calliope lives in the menu
    /// bar with no Dock icon; a settings window of such an app cannot be found
    /// with Command-Tab and loses its place behind other windows. So it takes a
    /// Dock icon while the window is open and gives it back when it closes.
    @MainActor func show() {
        model.refresh()
        if !window.isVisible {
            NSApp.setActivationPolicy(.regular)
            window.center()
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        startTimer()
    }

    @MainActor func select(pane: SettingsPane) { model.pane = pane }

    private func startTimer() {
        timer?.invalidate()
        let timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.window.isVisible else { return }
                self.model.liveRefresh()
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    /// Nothing is polled while the window is closed: a loopback request every
    /// two seconds is cheap, but cheap times for ever is not free.
    func windowWillClose(_ notification: Notification) {
        MainActor.assumeIsolated {
            window.makeFirstResponder(nil)
            model.commitCalliope()          // a field being edited commits
            NSApp.setActivationPolicy(.accessory)
        }
        timer?.invalidate()
        timer = nil
    }
}
