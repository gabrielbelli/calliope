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
    var calliopeKeyProblem: String? = nil
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
    // A STAND-IN WALLPAPER, NOT THE WINDOW COLOUR. The window is transparent
    // over the desktop; drawn over a flat fill, a pane that lets nothing through
    // and one that lets everything through would look the same. A dusk sky over
    // orange rock shows how much of the desktop survives each layer.
    NSGradient(colors: [NSColor(srgbRed: 0.10, green: 0.12, blue: 0.20, alpha: 1),
                        NSColor(srgbRed: 0.78, green: 0.42, blue: 0.20, alpha: 1)])?
        .draw(in: NSRect(origin: .zero, size: size), angle: -90)
    (view.appearance ?? NSAppearance.currentDrawing()).performAsCurrentDrawingAppearance {
        // What the system's sidebar material does to it, roughly: a dim veil.
        NSColor(white: view.appearance?.name == .darkAqua ? 0.1 : 0.95, alpha: 0.55).setFill()
        NSRect(origin: .zero, size: size).fill(using: .sourceOver)
    }
    bitmap.draw(in: NSRect(origin: .zero, size: size), from: .zero, operation: .sourceOver,
                fraction: 1, respectFlipped: true, hints: nil)
    NSGraphicsContext.restoreGraphicsState()
    try? canvas.representation(using: .png, properties: [:])?.write(to: url)
}

let output = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? ".")
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)

MainActor.assumeIsolated {
    let host = StandInHost()
    let settings = SettingsController(host: host)
    // Laid out in a window that is never ordered in: its size is the one the
    // controller gives it, which is the point -- nothing that arrives later may
    // change it.
    let size = settings.window.frame.size
    for (appearance, suffix) in [(NSAppearance.Name.aqua, "light"), (.darkAqua, "dark")] {
        settings.window.appearance = NSAppearance(named: appearance)
        settings.refresh()
        for pane in SettingsPane.allCases {
            settings.select(pane: pane)
            settle(0.8)                   // the proxy's answers arrive on the main queue
            if pane == .engines, suffix == "light" {
                settings.model.testConnection()
                settle(1.5)
            }
            let fixed = settings.window.frame.size == size
            print("window \(fixed ? "kept its size" : "CHANGED SIZE") on \(pane.rawValue)-\(suffix)")
            settings.window.layoutIfNeeded()
            settings.window.displayIfNeeded()
            if let frame = settings.window.contentView?.superview {
                render(frame, to: output.appendingPathComponent("\(pane.rawValue)-\(suffix).png"))
            }
        }
    }
    print("rendered \(SettingsPane.allCases.count * 2) panes into \(output.path)")
}
