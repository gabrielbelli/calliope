// Where the two halves of Calliope live, agreed on once instead of twice.
//
// THE DAEMON AND THE PLAYER HAVE DISAGREED ABOUT A PATH BEFORE, and the suite
// was green through it: the daemon looked for "venv/bin/python3" where the
// installer had made ".venv/bin/python", so the menu said "not installed" on a
// machine where install.sh had just succeeded. Every test read one file. This
// file is compiled into both, so there is one spelling to be wrong.
//
// The split is what a signed application requires rather than a preference.
// Code is inside the bundle and must not be written to; the model, the venv and
// the logs change constantly and must not be inside it, or every write would
// break the signature.

import Foundation

/// The changing half: a 310 MB model, a Python environment, logs, the queue and
/// the pid file. Outside the bundle, and the same directory it has always been.
let runtimeURL = URL(fileURLWithPath: NSString(string: "~/.local/share/calliope")
    .expandingTildeInPath)

/// Calliope.app itself, found by walking up from whichever executable is asking.
///
/// The daemon is the bundle's own executable, so `Bundle.main` is already the
/// app. The player is a nested bundle two directories down, so its `Bundle.main`
/// is the helper -- hence the walk rather than a constant. Unbundled, during
/// development, there is nothing to find and the paths below simply do not
/// exist, which the daemon already reports as "not installed: run install.sh".
let appURL: URL = {
    var url = Bundle.main.bundleURL
    while url.path != "/" {
        if url.lastPathComponent == "Calliope.app" { return url }
        url = url.deletingLastPathComponent()
    }
    return Bundle.main.bundleURL
}()

/// The local server, in the bundle's Resources.
let serverScriptURL = appURL.appendingPathComponent("Contents/Resources/server.py")

/// The interpreter that runs it.
///
/// INSIDE THE BUNDLE WHEN THERE IS ONE, and that is what makes a packaged
/// Calliope work at all. install.sh builds the environment in the runtime
/// directory, because it is a script the owner runs and $HOME is theirs to
/// write to. A Homebrew formula cannot do that -- its install step is
/// sandboxed to the formula's own prefix -- so it puts the environment and the
/// model inside Calliope.app instead, where they are built once, sealed by the
/// signature, and never written to again.
///
/// Bundle first, runtime second: an app that carries its own is self-contained,
/// and one that does not falls back to the directory install.sh fills.
private func preferBundle(_ inside: String, _ outside: String) -> URL {
    let bundled = appURL.appendingPathComponent("Contents/Resources/" + inside)
    return FileManager.default.fileExists(atPath: bundled.path)
        ? bundled
        : runtimeURL.appendingPathComponent(outside)
}

let pythonURL = preferBundle("python/bin/python3", ".venv/bin/python")

/// The one-shot player, a nested application with its own identity.
///
/// ITS OWN BUNDLE ID, NOT THE DAEMON'S. Two NSApplications sharing one
/// identifier is the shape of a bug this project has already paid for once in
/// another form -- two processes answering to the same name, and the window
/// server activating whichever it pleases.
let playerURL = appURL.appendingPathComponent(
    "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player")
