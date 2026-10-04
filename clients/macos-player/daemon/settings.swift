// The Settings window: General, Voices, Engines, Proxy, Integrations.
//
// A FILE OF ITS OWN SO IT CAN BE LOOKED AT. The daemon cannot be launched to
// test it -- it takes the menu bar, the hotkey and the port -- so this file
// depends on nothing of the daemon's but the SettingsHost protocol below, and
// tests/harness/settings/main.swift renders every pane to an image off screen with
// a stand-in host. A window nobody can see in a test is a window whose layout
// is only ever checked by the person it was built for.
//
// WHAT THE WINDOW HOLDS IS WHAT THE MENU CANNOT SAY. Speed is in both; the
// rest -- which voice per language, which side runs it, the server's address
// and key, the proxy other programs use, the command line and the agent skill
// -- needs room, and a state that is read back rather than claimed.
import AppKit

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
struct OfferedVoice {
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

// MARK: - Building blocks

private let paneWidth: CGFloat = 560
private let inset: CGFloat = 24
private let labelColumn: CGFloat = 132
private var fieldWidth: CGFloat { paneWidth - inset * 2 - labelColumn - 10 }

private func label(_ text: String) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.alignment = .right
    return field
}

private func note(_ text: String, width: CGFloat = paneWidth - inset * 2) -> NSTextField {
    let field = NSTextField(wrappingLabelWithString: text)
    field.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
    field.textColor = .secondaryLabelColor
    field.preferredMaxLayoutWidth = width
    field.widthAnchor.constraint(equalToConstant: width).isActive = true
    return field
}

private func sectionTitle(_ text: String) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.font = .systemFont(ofSize: NSFont.systemFontSize + 1, weight: .semibold)
    return field
}

private func separator() -> NSBox {
    let box = NSBox()
    box.boxType = .separator
    box.widthAnchor.constraint(equalToConstant: paneWidth - inset * 2).isActive = true
    return box
}

private func row(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = .horizontal
    stack.alignment = .centerY
    stack.spacing = spacing
    return stack
}

private func button(_ title: String, _ target: AnyObject, _ action: Selector) -> NSButton {
    let button = NSButton(title: title, target: target, action: action)
    button.bezelStyle = .push
    return button
}

/// A label column and a control column, aligned on the first baseline -- the
/// shape every Settings window on this system has, so it reads at a glance.
private func form(_ rows: [[NSView]]) -> NSGridView {
    let grid = NSGridView(views: rows)
    grid.rowSpacing = 10
    grid.columnSpacing = 10
    grid.rowAlignment = .firstBaseline
    grid.column(at: 0).xPlacement = .trailing
    grid.column(at: 0).width = labelColumn
    grid.column(at: 1).xPlacement = .leading
    return grid
}

/// A titled section with its switch on the right: the switch is the section's
/// answer, and everything under it applies only while it is on.
private func switchHeader(_ heading: String, _ detail: String, _ toggle: NSSwitch) -> NSStackView {
    let words = NSStackView(views: [sectionTitle(heading), note(detail, width: paneWidth - inset * 2 - 60)])
    words.orientation = .vertical
    words.alignment = .leading
    words.spacing = 2
    let header = row([words, toggle])
    header.alignment = .top
    header.distribution = .equalSpacing
    header.widthAnchor.constraint(equalToConstant: paneWidth - inset * 2).isActive = true
    return header
}

/// One pane: a vertical stack at a fixed width, sized to what it holds.
class Pane: NSViewController {
    weak var host: SettingsHost?
    let stack = NSStackView()

    override func loadView() {
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 20, left: inset, bottom: 24, right: inset)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.widthAnchor.constraint(equalToConstant: paneWidth),
        ])
        view = root
        build()
        fit()
    }

    func build() {}
    /// Read everything back from where it is kept; called when the window opens.
    func refresh() {}
    /// The few things that change while the window is open (is the model
    /// loaded, is the proxy up), asked every two seconds while it is visible.
    func liveRefresh() {}

    /// The window takes its size from the pane, so a pane that grew or shrank
    /// -- a section switched on, a row added -- says so here.
    func fit() {
        stack.layoutSubtreeIfNeeded()
        let size = NSSize(width: paneWidth, height: ceil(stack.fittingSize.height))
        preferredContentSize = size
        view.setFrameSize(size)
    }
}

// MARK: - General

final class GeneralPane: Pane {
    private let login = NSButton(checkboxWithTitle: "Open Calliope at login", target: nil, action: nil)
    private let speed = NSPopUpButton()
    private let access = NSTextField(labelWithString: "")
    private let grant = NSButton()
    private static let steps = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

    override func build() {
        // THE SAME MARK AS THE PAGE, THE MENU BAR AND THE ICON, in colour here
        // because this is the one place it is shown at a size that can carry it.
        let mark = NSImageView(image: NSImage(size: NSSize(width: 56, height: 56), flipped: false) { rect in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            Mark.draw(in: context, size: rect.width, monochrome: false)
            return true
        })
        mark.widthAnchor.constraint(equalToConstant: 56).isActive = true
        mark.heightAnchor.constraint(equalToConstant: 56).isActive = true
        let name = NSTextField(labelWithString: "Calliope")
        name.font = .systemFont(ofSize: 20, weight: .semibold)
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let words = NSStackView(views: [
            name,
            note("Reads text aloud, on this Mac or with your own Calliope server.",
                 width: paneWidth - inset * 2 - 72),
        ])
        if let version { words.addArrangedSubview(note("Version \(version)", width: 200)) }
        words.orientation = .vertical
        words.alignment = .leading
        words.spacing = 2
        stack.addArrangedSubview(row([mark, words], spacing: 14))
        stack.addArrangedSubview(separator())

        login.target = self
        login.action = #selector(toggleLogin)
        for step in Self.steps {
            speed.addItem(withTitle: step == 1.0 ? "1× (normal)" : "\(step)×")
            speed.lastItem?.representedObject = step
        }
        speed.target = self
        speed.action = #selector(setSpeed)
        grant.title = "Grant Access…"
        grant.bezelStyle = .push
        grant.target = self
        grant.action = #selector(askForAccess)
        let logs = button("Open Log", self, #selector(openLog))

        let grid = form([
            [label("Startup:"), login],
            [label("Reading speed:"), speed],
            [label("Shortcut:"), NSTextField(labelWithString: "⌥⌘S reads the selected text aloud")],
            [label("Accessibility:"), row([access, grant])],
            [NSGridCell.emptyContentView, note("Needed to read another app's selection. macOS asks again "
                + "after an update, because it grants this to one copy of a program.", width: fieldWidth)],
            [label("Troubleshooting:"), logs],
        ])
        stack.addArrangedSubview(grid)
    }

    override func refresh() {
        login.state = host?.loginEnabled == true ? .on : .off
        let current = preferences.object(forKey: "speed") as? Double ?? 1.0
        speed.selectItem(at: Self.steps.firstIndex { abs($0 - current) < 0.01 } ?? 1)
        let granted = host?.accessibilityGranted == true
        access.stringValue = granted ? "Granted" : "Not granted — the shortcut cannot read a selection"
        access.textColor = granted ? .labelColor : .systemOrange
        grant.isHidden = granted
    }

    override func liveRefresh() { refresh() }

    @objc private func toggleLogin() {
        host?.setLogin(login.state == .on)
        refresh()
    }

    /// THE SAME KEY THE MENU WRITES AND THE PLAYER READS: the next passage
    /// takes it, and the capsule has its own control for the one being read.
    @objc private func setSpeed() {
        guard let step = speed.selectedItem?.representedObject as? Double else { return }
        preferences.set(step, forKey: "speed")
    }

    @objc private func askForAccess() {
        host?.requestAccessibility()
        // Granted in System Settings, in their own time: noticed, not assumed.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in self?.refresh() }
    }

    @objc private func openLog() { host?.openLog() }
}

// MARK: - Voices

/// A voice per language, for the languages somebody actually reads.
///
/// ROWS ARE ADDED, NOT PRE-FILLED. Eight rows for eight languages makes
/// somebody who reads English and Portuguese answer six questions about
/// languages they never read; a language without a row is read with its
/// standard voice, which is what happened before there was a choice at all.
final class VoicesPane: Pane {
    private let rows = NSStackView()
    private let empty = NSTextField(labelWithString: "")
    private let add = NSPopUpButton(frame: .zero, pullsDown: true)
    private let status = NSTextField(labelWithString: "")
    private var offered: [OfferedVoice] = []

    override func build() {
        stack.addArrangedSubview(note("Calliope detects the language of what it reads. Choose a voice "
            + "for the languages you read; any other language uses its standard voice. A voice from "
            + "the Calliope server is spoken there, so it needs the server on and reachable."))

        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 8
        stack.addArrangedSubview(rows)

        empty.textColor = .secondaryLabelColor
        stack.addArrangedSubview(empty)

        add.target = self
        add.action = #selector(addLanguage)
        stack.addArrangedSubview(add)

        status.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        status.textColor = .secondaryLabelColor
        stack.addArrangedSubview(status)
    }

    override func refresh() {
        rebuild()
        status.stringValue = "Asking for the voices…"
        Proxy.get("/voices") { [weak self] body in
            guard let self else { return }
            let detail = body?["detail"] as? [[String: Any]] ?? []
            self.offered = detail.compactMap { row in
                guard let name = row["name"] as? String,
                      let origin = (row["origin"] as? String).flatMap(VoiceOrigin.init) else { return nil }
                return OfferedVoice(name: name, origin: origin, language: row["language"] as? String)
            }
            if body == nil {
                self.status.stringValue = "The proxy is not answering, so only your saved choices are shown."
            } else if self.offered.isEmpty {
                self.status.stringValue = "No voices: both engines are off. Turn one on in Engines."
            } else {
                let mac = self.offered.filter { $0.origin == .mac }.count
                let remote = self.offered.count - mac
                self.status.stringValue = "\(mac) voices on this Mac"
                    + (remote > 0 ? ", \(remote) on the Calliope server." : ".")
            }
            self.rebuild()
        }
    }

    private func rebuild() {
        rows.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let saved = Preference.voices
        let configured = languages.filter { saved[$0.code] != nil }
        for spoken in configured {
            rows.addArrangedSubview(voiceRow(spoken, chosen: saved[spoken.code]!))
        }
        empty.stringValue = "No languages added. Every language uses its standard voice."
        empty.isHidden = !configured.isEmpty

        add.removeAllItems()
        add.addItem(withTitle: "Add Language")
        let remaining = languages.filter { saved[$0.code] == nil }
        for spoken in remaining {
            add.addItem(withTitle: spoken.name)
            add.lastItem?.representedObject = spoken.code
        }
        add.isEnabled = !remaining.isEmpty
        fit()
    }

    private func voiceRow(_ spoken: Language, chosen: String) -> NSView {
        let name = NSTextField(labelWithString: spoken.name)
        name.widthAnchor.constraint(equalToConstant: 104).isActive = true

        let popup = NSPopUpButton()
        popup.widthAnchor.constraint(equalToConstant: 330).isActive = true
        let menu = NSMenu()
        var selected: NSMenuItem?
        for (origin, heading) in [(VoiceOrigin.mac, "This Mac"), (.calliope, "Calliope server")] {
            let voices = offered.filter { $0.origin == origin && $0.language == spoken.code }
                .sorted { $0.name < $1.name }
            guard !voices.isEmpty else { continue }
            menu.addItem(.sectionHeader(title: heading))
            for voice in voices {
                let item = NSMenuItem(title: voiceTitle(VoiceRef(origin: voice.origin, name: voice.name)),
                                      action: nil, keyEquivalent: "")
                item.representedObject = voice.ref
                item.toolTip = voice.ref
                menu.addItem(item)
                if voice.ref == chosen { selected = item }
            }
        }
        // A SAVED CHOICE IS SHOWN EVEN WHEN IT IS NOT ON OFFER RIGHT NOW. The
        // server may be down, or switched off for the afternoon; quietly
        // replacing the choice with whatever happens to be listed would lose it.
        if selected == nil, let ref = VoiceRef(chosen) {
            let item = NSMenuItem(title: voiceTitle(ref) + " — not available now",
                                  action: nil, keyEquivalent: "")
            item.representedObject = chosen
            menu.insertItem(item, at: 0)
            selected = item
        }
        popup.menu = menu
        if let selected { popup.select(selected) }
        popup.identifier = NSUserInterfaceItemIdentifier(spoken.code)
        popup.target = self
        popup.action = #selector(choose(_:))

        let remove = NSButton(image: NSImage(systemSymbolName: "minus.circle",
                                             accessibilityDescription: "Remove \(spoken.name)")!,
                              target: self, action: #selector(remove(_:)))
        remove.isBordered = false
        remove.identifier = NSUserInterfaceItemIdentifier(spoken.code)
        remove.toolTip = "Use the standard voice for \(spoken.name)"
        return row([name, popup, remove])
    }

    @objc private func choose(_ sender: NSPopUpButton) {
        guard let code = sender.identifier?.rawValue,
              let ref = sender.selectedItem?.representedObject as? String else { return }
        var saved = Preference.voices
        saved[code] = ref
        Preference.voices = saved
    }

    @objc private func remove(_ sender: NSButton) {
        guard let code = sender.identifier?.rawValue else { return }
        var saved = Preference.voices
        saved[code] = nil
        Preference.voices = saved
        rebuild()
    }

    /// A new row starts on the standard voice, on whichever side is on: a row
    /// that starts empty is a setting that does nothing until it is touched.
    @objc private func addLanguage() {
        guard let code = add.selectedItem?.representedObject as? String,
              let spoken = language(code: code) else { return }
        let origin: VoiceOrigin = Preference.macOn || !Preference.calliopeOn ? .mac : .calliope
        var saved = Preference.voices
        saved[code] = VoiceRef(origin: origin, name: spoken.standardVoice).ref
        Preference.voices = saved
        rebuild()
    }
}

// MARK: - Engines

/// The two sides that can speak, each behind its own switch.
///
/// THIS MAC IS THE WHOLE OF IT UNLESS SOMEBODY SAYS OTHERWISE. With the Calliope
/// switch off nothing here touches the network, and with "Keep loaded" off the
/// 450 MB model is in memory only while it is being used: Calliope's footprint
/// at rest is the menu bar item and a small proxy.
final class EnginesPane: Pane {
    private let macSwitch = NSSwitch()
    private let macStatus = NSTextField(labelWithString: "")
    private let keepLoaded = NSButton(checkboxWithTitle: "Keep the voice loaded", target: nil, action: nil)
    private let macDetail = NSStackView()

    private let calliopeSwitch = NSSwitch()
    private let calliopeFields = NSStackView()
    private let urlField = NSTextField(string: "")
    private let keyField = NSSecureTextField(string: "")
    private let test = NSButton()
    private let open = NSButton()
    private let spinner = NSProgressIndicator()
    private let message = NSTextField(wrappingLabelWithString: "")
    private let checks = NSStackView()
    private let bothOff = NSTextField(labelWithString: "")

    var firstField: NSView { urlField }

    override func build() {
        macSwitch.target = self
        macSwitch.action = #selector(toggleMac)
        stack.addArrangedSubview(switchHeader("This Mac",
            "Kokoro on this Mac's processor. Private, and it works offline.", macSwitch))

        keepLoaded.target = self
        keepLoaded.action = #selector(toggleKeepLoaded)
        macDetail.orientation = .vertical
        macDetail.alignment = .leading
        macDetail.spacing = 6
        macDetail.addArrangedSubview(form([[label("Status:"), macStatus]]))
        macDetail.addArrangedSubview(form([
            [NSGridCell.emptyContentView, keepLoaded],
            [NSGridCell.emptyContentView, note("Loaded, the first word comes at once, and it holds about "
                + "450 MB of memory. Otherwise it loads when needed (about 2 seconds) and is released "
                + "after 10 minutes unused.", width: fieldWidth)],
        ]))
        stack.addArrangedSubview(macDetail)
        stack.addArrangedSubview(separator())

        calliopeSwitch.target = self
        calliopeSwitch.action = #selector(toggleCalliope)
        stack.addArrangedSubview(switchHeader("Calliope server",
            "Your own server: more voices, transcription and long documents. "
                + "Nothing leaves this Mac while this is off.", calliopeSwitch))

        for field in [urlField, keyField] as [NSTextField] {
            field.widthAnchor.constraint(equalToConstant: fieldWidth).isActive = true
            // NO SAVE BUTTON. The fields commit themselves, on Return and on
            // leaving them -- typing an address and then pressing a second
            // thing is a step more than the switch promised.
            field.target = self
            field.action = #selector(commitCalliope)
            (field.cell as? NSTextFieldCell)?.sendsActionOnEndEditing = true
        }
        urlField.placeholderString = "https://calliope.example.com"
        keyField.placeholderString = "Paste an API key"

        test.title = "Test Connection"
        test.bezelStyle = .push
        test.target = self
        test.action = #selector(testConnection)
        open.title = "Open Calliope"
        open.bezelStyle = .push
        open.target = self
        open.action = #selector(openCalliope)
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false

        message.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        message.preferredMaxLayoutWidth = fieldWidth
        message.widthAnchor.constraint(equalToConstant: fieldWidth).isActive = true
        checks.orientation = .vertical
        checks.alignment = .leading
        checks.spacing = 4

        calliopeFields.orientation = .vertical
        calliopeFields.alignment = .leading
        calliopeFields.spacing = 10
        calliopeFields.addArrangedSubview(form([
            [label("Address:"), urlField],
            [label("API key:"), keyField],
            [NSGridCell.emptyContentView, note("Make one in Calliope under Account › API keys › New key. "
                + "Preset user covers speech and transcription; user-jobs adds long documents.",
                width: fieldWidth)],
            [NSGridCell.emptyContentView, row([test, open, spinner])],
            [NSGridCell.emptyContentView, message],
            [NSGridCell.emptyContentView, checks],
        ]))
        stack.addArrangedSubview(calliopeFields)

        bothOff.stringValue = "Both engines are off, so Calliope cannot speak."
        bothOff.textColor = .systemOrange
        stack.addArrangedSubview(bothOff)
    }

    override func refresh() {
        macSwitch.state = Preference.macOn ? .on : .off
        keepLoaded.state = Preference.keepLoaded ? .on : .off
        calliopeSwitch.state = host?.calliopeOn == true ? .on : .off
        urlField.stringValue = host?.calliopeURL ?? ""
        keyField.stringValue = host?.calliopeKey ?? ""
        layoutSwitches()
        liveRefresh()
    }

    private func layoutSwitches() {
        macDetail.isHidden = !Preference.macOn
        calliopeFields.isHidden = host?.calliopeOn != true
        bothOff.isHidden = Preference.macOn || host?.calliopeOn == true
        open.isEnabled = !(host?.calliopeURL ?? "").isEmpty
        fit()
        view.window?.setContentSize(preferredContentSize)
    }

    override func liveRefresh() {
        Proxy.get("/status", timeout: 1) { [weak self] body in
            guard let self else { return }
            let mac = body?["mac"] as? [String: Any]
            if let problem = self.host?.serverProblem {
                self.macStatus.stringValue = "The proxy is not running: \(problem)"
            } else if body == nil {
                self.macStatus.stringValue = "Starting…"
            } else if mac?["loaded"] as? Bool == true {
                self.macStatus.stringValue = "Loaded and ready"
            } else {
                self.macStatus.stringValue = "Not loaded — loads on first use"
            }
        }
    }

    @objc private func toggleMac() {
        Preference.macOn = macSwitch.state == .on
        host?.restartServer()
        layoutSwitches()
    }

    @objc private func toggleKeepLoaded() {
        Preference.keepLoaded = keepLoaded.state == .on
        host?.restartServer()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.liveRefresh() }
    }

    @objc private func toggleCalliope() {
        host?.calliopeOn = calliopeSwitch.state == .on
        layoutSwitches()
        saveCalliope(force: true)
        if host?.calliopeOn == true, urlField.stringValue.isEmpty {
            message.stringValue = "Where is it?"
            view.window?.makeFirstResponder(urlField)
        }
    }

    @objc private func commitCalliope() { saveCalliope(force: false) }

    /// Save both, restart the proxy that reads them, then test the round trip.
    ///
    /// ONLY WHEN SOMETHING CHANGED. Both fields commit on leaving them, so
    /// tabbing from the address to the key fired this twice -- and a restart
    /// takes the proxy away from whatever is being read aloud.
    private func saveCalliope(force: Bool) {
        guard let host else { return }
        let url = urlField.stringValue.trimmingCharacters(in: .whitespaces)
        let key = keyField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let changed = url != host.calliopeURL || key != host.calliopeKey
        host.calliopeURL = url
        host.calliopeKey = key
        urlField.stringValue = url
        open.isEnabled = !url.isEmpty
        guard changed || force else { return }
        host.restartServer()
        clearChecks()
        guard host.calliopeConfigured else {
            if !host.calliopeOn {
                message.stringValue = "Off. Nothing is sent anywhere."
            } else if !url.isEmpty {
                message.stringValue = "A key is required. The server answers nothing without one."
            } else {
                message.stringValue = ""
            }
            fit()
            return
        }
        testConnection()
    }

    /// The Test button, and what every save that completes the address runs.
    ///
    /// FOUR STEPS, STOPPING AT THE FIRST THAT FAILS, because each one is only
    /// meaningful if the one before it worked: reachable, then the key, then the
    /// voices it offers, then a word actually spoken. "It did not work" is not
    /// something anybody can act on; "the key was not accepted" is.
    @objc private func testConnection() {
        view.window?.makeFirstResponder(nil)        // commits a field still being edited
        guard host?.calliopeConfigured == true else {
            message.stringValue = host?.calliopeOn == true
                ? "Fill in the address and the key first." : "The Calliope server is off."
            return fit()
        }
        clearChecks()
        message.stringValue = "Testing…"
        test.isEnabled = false
        spinner.startAnimation(nil)
        Proxy.whenUp(within: 10) { [weak self] up in
            guard let self else { return }
            guard up else {
                return self.finishTest("The proxy did not start, so nothing could be tested.", [])
            }
            Proxy.get("/calliope/test", timeout: 40) { body in
                let steps = body?["checks"] as? [[String: Any]] ?? []
                self.finishTest(body?["message"] as? String ?? "No answer from the proxy.", steps)
            }
        }
    }

    private func finishTest(_ text: String, _ steps: [[String: Any]]) {
        spinner.stopAnimation(nil)
        test.isEnabled = true
        message.stringValue = text
        clearChecks()
        let names = ["reach": "Server", "key": "Key", "voices": "Voices", "speech": "Speech"]
        for step in steps {
            let ok = step["ok"] as? Bool == true
            let symbol = NSImage(systemSymbolName: ok ? "checkmark.circle.fill" : "xmark.circle.fill",
                                 accessibilityDescription: ok ? "passed" : "failed")
            let icon = NSImageView(image: symbol ?? NSImage())
            icon.contentTintColor = ok ? .systemGreen : .systemRed
            var words = names[step["name"] as? String ?? ""] ?? (step["name"] as? String ?? "")
            if let detail = step["detail"] as? String, !detail.isEmpty { words += " — \(detail)" }
            if let ms = step["ms"] as? Int { words += " (\(ms) ms)" }
            let line = NSTextField(labelWithString: words)
            line.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            checks.addArrangedSubview(row([icon, line], spacing: 6))
        }
        fit()
        view.window?.setContentSize(preferredContentSize)
    }

    private func clearChecks() {
        checks.arrangedSubviews.forEach { $0.removeFromSuperview() }
    }

    @objc private func openCalliope() {
        guard let url = URL(string: host?.calliopeURL ?? ""), url.scheme?.hasPrefix("http") == true
        else { return }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - Proxy

/// The address other programs on this Mac use.
///
/// ONE ADDRESS FOR EVERY ENGINE, AND NO KEY IN ANY OF THE PROGRAMS. A script, an
/// editor plugin or an agent asks 127.0.0.1 in OpenAI's shape; this Mac's voices
/// answer here and calliope/… models go to the Calliope server with the key the
/// daemon handed the proxy -- so the key is in one process, not in every tool.
final class ProxyPane: Pane {
    private let address = NSTextField(labelWithString: "")
    private let port = NSTextField(string: "")
    private let state = NSTextField(labelWithString: "")
    private let models = NSTextField(wrappingLabelWithString: "")

    override func build() {
        stack.addArrangedSubview(note("Apps and scripts on this Mac can use Calliope at one "
            + "OpenAI-compatible address: speech, transcription and the model list. This Mac's voices "
            + "answer here; models named calliope/… go to your Calliope server with your key, so no "
            + "app needs the key itself."))

        address.font = .monospacedSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        address.isSelectable = true
        port.widthAnchor.constraint(equalToConstant: 72).isActive = true
        port.alignment = .right
        port.target = self
        port.action = #selector(savePort)
        (port.cell as? NSTextFieldCell)?.sendsActionOnEndEditing = true
        port.formatter = {
            let formatter = NumberFormatter()
            formatter.allowsFloats = false
            formatter.usesGroupingSeparator = false
            formatter.minimum = 1024
            formatter.maximum = 65535
            return formatter
        }()
        models.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        models.preferredMaxLayoutWidth = fieldWidth
        models.widthAnchor.constraint(equalToConstant: fieldWidth).isActive = true

        stack.addArrangedSubview(form([
            [label("Address:"), row([address, button("Copy", self, #selector(copyAddress))])],
            [label("Port:"), row([port, note("Changing it restarts the proxy.", width: 220)])],
            [label("Status:"), state],
            [label("Models:"), models],
        ]))
        stack.addArrangedSubview(note("Web pages cannot use it: a request a browser sends is refused, "
            + "so a site you visit cannot spend your voices or your key."))
    }

    override func refresh() {
        address.stringValue = localServerURL
        port.stringValue = String(Preference.port)
        liveRefresh()
    }

    override func liveRefresh() {
        address.stringValue = localServerURL
        Proxy.get("/v1/models", timeout: 1) { [weak self] body in
            guard let self else { return }
            if let problem = self.host?.serverProblem {
                self.state.stringValue = "Not running: \(problem)"
            } else {
                self.state.stringValue = body == nil ? "Starting…" : "Running"
            }
            let rows = body?["data"] as? [[String: Any]] ?? []
            let local = rows.filter { $0["owned_by"] as? String == "calliope-local" }
                .compactMap { $0["id"] as? String }
            let remote = rows.filter { $0["owned_by"] as? String == "calliope-remote" }
                .compactMap { $0["id"] as? String }
            var lines: [String] = []
            if !local.isEmpty { lines.append("This Mac: " + local.joined(separator: ", ")) }
            if !remote.isEmpty {
                let names = remote.map { $0.hasPrefix("calliope/") ? String($0.dropFirst(9)) : $0 }
                lines.append("Calliope server, as calliope/…: " + names.joined(separator: ", "))
            }
            self.models.stringValue = lines.isEmpty ? "None yet." : lines.joined(separator: "\n")
            self.fit()
        }
    }

    @objc private func copyAddress() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(localServerURL, forType: .string)
    }

    @objc private func savePort() {
        guard let value = Int(port.stringValue), (1024...65535).contains(value) else {
            port.stringValue = String(Preference.port)
            return
        }
        guard value != Preference.port else { return }
        Preference.port = value
        host?.restartServer()
        refresh()
    }
}

// MARK: - Integrations

/// The command line, the agent skill and OpenClip: how other programs reach Calliope.
///
/// THE SKILL IS A FILE, NOT AN INSTALLATION. It is a SKILL.md for whoever wants
/// it, to use with whichever agent they like, in whatever way they keep their
/// own setup -- so this shows where it is and installs nothing. The command
/// goes onto the PATH only when somebody presses the button.
final class IntegrationsPane: Pane {
    private let cliState: NSTextField = {
        let field = NSTextField(wrappingLabelWithString: "")
        field.preferredMaxLayoutWidth = fieldWidth
        return field
    }()
    private let installCLI = NSButton()
    private let skillState = NSTextField(labelWithString: "")
    private let showSkill = NSButton()
    private let openClipState: NSTextField = {
        let field = NSTextField(wrappingLabelWithString: "")
        field.preferredMaxLayoutWidth = fieldWidth
        return field
    }()
    private let reinstallOpenClip = NSButton()

    private let home = FileManager.default.homeDirectoryForCurrentUser
    private var cliLink: URL { home.appendingPathComponent(".local/bin/calliope") }
    private var bundledCLI: URL { appURL.appendingPathComponent("Contents/Resources/cli/calliope") }
    private var bundledSkill: URL {
        appURL.appendingPathComponent("Contents/Resources/skill/calliope-voice/SKILL.md")
    }
    private var openClip: URL { home.appendingPathComponent(".openclip") }
    private var extensionDir: URL { openClip.appendingPathComponent("extensions/calliope.openclipext") }
    private var bundledExtension: URL { appURL.appendingPathComponent("Contents/Resources/openclip") }
    static let example = "calliope speak --reader -f explanation.md"

    override func build() {
        stack.addArrangedSubview(sectionTitle("Command line"))
        stack.addArrangedSubview(note("The calliope command speaks, saves audio and transcribes from a "
            + "terminal or a script, through the same proxy and voices as everything else."))
        let example = NSTextField(labelWithString: Self.example)
        example.font = .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        example.isSelectable = true
        configure(installCLI, "Install Command", #selector(installCommand))
        stack.addArrangedSubview(form([
            [label("Status:"), cliState],
            [label("Example:"), example],
            [NSGridCell.emptyContentView, row([installCLI, button("Copy Example", self, #selector(copyExample))])],
        ]))
        stack.addArrangedSubview(separator())

        stack.addArrangedSubview(sectionTitle("Agent skill"))
        stack.addArrangedSubview(note("calliope-voice is a SKILL.md that teaches an AI agent to write "
            + "an explanation for the ear and read it to you with the calliope command. Use it with any "
            + "agent, however you keep your own setup."))
        configure(showSkill, "Show Skill File", #selector(revealSkill))
        stack.addArrangedSubview(form([
            [label("File:"), skillState],
            [NSGridCell.emptyContentView, showSkill],
        ]))
        stack.addArrangedSubview(separator())

        stack.addArrangedSubview(sectionTitle("OpenClip"))
        configure(reinstallOpenClip, "Reinstall Speak Action", #selector(installOpenClip))
        stack.addArrangedSubview(form([
            [label("Status:"), openClipState],
            [NSGridCell.emptyContentView, reinstallOpenClip],
        ]))
    }

    private func configure(_ button: NSButton, _ title: String, _ action: Selector) {
        button.title = title
        button.bezelStyle = .push
        button.target = self
        button.action = action
    }

    override func refresh() {
        let files = FileManager.default
        let brewed = ["/opt/homebrew/bin/calliope", "/usr/local/bin/calliope"]
            .first { files.fileExists(atPath: $0) }
        if files.fileExists(atPath: cliLink.path) {
            cliState.stringValue = "Installed at ~/.local/bin/calliope"
        } else if let brewed {
            cliState.stringValue = "Installed at \(brewed)"
        } else {
            cliState.stringValue = "Not installed"
        }
        installCLI.isHidden = files.fileExists(atPath: cliLink.path) || brewed != nil
        installCLI.isEnabled = files.fileExists(atPath: bundledCLI.path)

        let skill = files.fileExists(atPath: bundledSkill.path)
        skillState.stringValue = skill ? "calliope-voice/SKILL.md, inside Calliope.app" : "Not in this build"
        showSkill.isEnabled = skill

        // REINSTALL, NOT ONLY INSTALL. An extension that is there can still be
        // wrong -- an older copy, a file edited by hand, an icon OpenClip could
        // not load -- and putting the bundled one back is the repair for all of
        // them. OpenClip trusts an extension by a hash of its files, so it asks
        // once more after this, which is the price of the files having changed.
        let present = files.fileExists(atPath: extensionDir.appendingPathComponent("openclip.json").path)
        if !files.fileExists(atPath: openClip.path) {
            openClipState.stringValue = "OpenClip is not installed. It is optional."
            reinstallOpenClip.isHidden = true
        } else {
            openClipState.stringValue = present
                ? "The Speak action is installed."
                : "OpenClip is here, without the Speak action."
            reinstallOpenClip.title = present ? "Reinstall Speak Action" : "Install Speak Action"
            reinstallOpenClip.isHidden = false
            reinstallOpenClip.isEnabled = files.fileExists(atPath: bundledExtension.path)
        }
        fit()
    }

    @objc private func installCommand() {
        let files = FileManager.default
        do {
            try files.createDirectory(at: cliLink.deletingLastPathComponent(),
                                      withIntermediateDirectories: true)
            try? files.removeItem(at: cliLink)
            try files.createSymbolicLink(at: cliLink, withDestinationURL: bundledCLI)
        } catch {
            cliState.stringValue = "Could not install: \(error.localizedDescription)"
            return
        }
        refresh()
        cliState.stringValue += " — if your shell cannot find it, add ~/.local/bin to PATH"
    }

    @objc private func copyExample() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(Self.example, forType: .string)
    }

    @objc private func revealSkill() {
        NSWorkspace.shared.activateFileViewerSelecting([bundledSkill])
    }

    /// The same three files install.sh writes, from the copies inside this app,
    /// with the app's own location written into the script -- so an extension
    /// put back from here launches this Calliope, wherever it was installed.
    @objc private func installOpenClip() {
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
        } catch {
            openClipState.stringValue = "Could not install: \(error.localizedDescription)"
            return
        }
        refresh()
        openClipState.stringValue = "Reinstalled. OpenClip asks you to trust it again, because its "
            + "files changed."
        fit()
    }
}

// MARK: - The window

final class SettingsController: NSObject, NSWindowDelegate {
    let window: NSWindow
    private let tabs = NSTabViewController()
    private let panes: [Pane]
    private let engines = EnginesPane()
    private var timer: Timer?

    init(host: SettingsHost) {
        let general = GeneralPane(), voices = VoicesPane()
        let proxy = ProxyPane(), integrations = IntegrationsPane()
        panes = [general, voices, engines, proxy, integrations]
        for pane in panes { pane.host = host }

        tabs.tabStyle = .toolbar
        // The window follows each pane's own size; animating that is what
        // System Settings-era panes do, and it keeps a short pane short.
        tabs.transitionOptions = [.crossfade, .allowUserInteraction]
        for (pane, name, symbol) in [
            (general as Pane, "General", "gearshape"),
            (voices, "Voices", "person.wave.2"),
            (engines, "Engines", "cpu"),
            (proxy, "Proxy", "point.3.connected.trianglepath.dotted"),
            (integrations, "Integrations", "terminal"),
        ] {
            let item = NSTabViewItem(viewController: pane)
            pane.title = name            // the window's title follows the chosen pane
            item.label = name
            item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: name)
            tabs.addTabViewItem(item)
        }

        window = NSWindow(contentViewController: tabs)
        window.styleMask = [.titled, .closable]
        window.toolbarStyle = .preference
        window.isReleasedWhenClosed = false
        super.init()
        window.delegate = self
        // SEEN IN A SCREENSHOT BEFORE: the secure field took focus on open, so
        // macOS anchored its Passwords popover to it, over the buttons. Opening
        // Settings should not summon a password manager.
        window.initialFirstResponder = nil
    }

    func refresh() { panes.forEach { $0.refresh() } }

    /// Opened from the menu: everything read back, because a permission
    /// granted or a server restarted happened outside this window.
    func show() {
        refresh()
        window.center()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        startTimer()
    }

    func select(pane index: Int) { tabs.selectedTabViewItemIndex = index }

    private func startTimer() {
        timer?.invalidate()
        let timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            guard let self, self.window.isVisible else { return }
            let index = self.tabs.selectedTabViewItemIndex
            if self.panes.indices.contains(index) { self.panes[index].liveRefresh() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    /// Nothing is polled while the window is closed: a loopback request every
    /// two seconds is cheap, but cheap times for ever is not free.
    func windowWillClose(_ notification: Notification) {
        window.makeFirstResponder(nil)   // a field being edited commits
        timer?.invalidate()
        timer = nil
    }
}
