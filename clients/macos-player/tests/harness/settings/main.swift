// Renders every Settings pane to a PNG, off screen, with a stand-in daemon.
//
// THE WINDOW IS NEVER SHOWN. It is laid out and drawn into a bitmap, so a test
// can look at the layout without taking over the screen of whoever runs it --
// and without the daemon, which would take the menu bar, the hotkey and the
// port. Run by tests/test_settings_render.py with CALLIOPE_TEST_DEFAULTS
// pointing at a throwaway suite and the proxy port at a stand-in proxy.
//
// Usage: settings-render <output-dir>
import AppKit

final class StandInHost: SettingsHost {
    var loginEnabled = true
    func setLogin(_ on: Bool) { loginEnabled = on }
    var accessibilityGranted = true
    func requestAccessibility() {}
    func openLog() {}
    var serverProblem: String? = nil
    func restartServer() {}
    var calliopeURL = "https://calliope.example.com"
    var calliopeKey = "stand-in"
    var calliopeOn = true
    var calliopeConfigured: Bool { calliopeOn && !calliopeURL.isEmpty && !calliopeKey.isEmpty }
}

func settle(_ seconds: Double) {
    RunLoop.main.run(until: Date().addingTimeInterval(seconds))
}

/// The pane over the window's own background, in the pane's appearance. A pane
/// has no background of its own -- the window supplies it -- so drawn alone its
/// dark-mode text would land on a transparent PNG and read as a blank image.
func render(_ view: NSView, to url: URL) {
    view.layoutSubtreeIfNeeded()
    guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
    view.cacheDisplay(in: view.bounds, to: bitmap)
    let size = view.bounds.size
    guard let canvas = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: bitmap.pixelsWide,
                                        pixelsHigh: bitmap.pixelsHigh, bitsPerSample: 8,
                                        samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
    else { return }
    canvas.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: canvas)
    (view.appearance ?? NSAppearance.currentDrawing()).performAsCurrentDrawingAppearance {
        NSColor.windowBackgroundColor.setFill()
        NSRect(origin: .zero, size: size).fill()
    }
    bitmap.draw(in: NSRect(origin: .zero, size: size), from: .zero, operation: .sourceOver,
                fraction: 1, respectFlipped: true, hints: nil)
    NSGraphicsContext.restoreGraphicsState()
    try? canvas.representation(using: .png, properties: [:])?.write(to: url)
}

let output = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? ".")
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)

let host = StandInHost()
let settings = SettingsController(host: host)
let names = ["general", "voices", "engines", "proxy", "integrations"]
for (appearance, suffix) in [(NSAppearance.Name.aqua, "light"), (.darkAqua, "dark")] {
    settings.window.appearance = NSAppearance(named: appearance)
    settings.refresh()
    for (index, name) in names.enumerated() {
        settings.select(pane: index)
        settle(0.6)                       // the proxy's answers arrive on the main queue
        if name == "engines", suffix == "light" {
            // The Test button's result is part of the layout worth seeing.
            (settings.window.contentViewController as? NSTabViewController)?
                .tabViewItems[index].viewController?.perform(Selector(("testConnection")))
            settle(1.5)
        }
        // The pane at its own full height: the window's size follows the pane
        // only once it is on screen, and this one never is.
        let tabs = settings.window.contentViewController as? NSTabViewController
        if let pane = tabs?.tabViewItems[index].viewController as? Pane {
            pane.view.appearance = NSAppearance(named: appearance)
            pane.fit()
            pane.view.setFrameSize(pane.preferredContentSize)
            render(pane.view, to: output.appendingPathComponent("\(name)-\(suffix).png"))
        }
    }
}
print("rendered \(names.count * 2) panes into \(output.path)")
