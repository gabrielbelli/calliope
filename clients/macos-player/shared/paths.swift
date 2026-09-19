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

/// The interpreter that runs it, in the runtime, because a venv is 125 MB of
/// files that pip rewrites.
let pythonURL = runtimeURL.appendingPathComponent(".venv/bin/python")

/// The one-shot player, a nested application with its own identity.
///
/// ITS OWN BUNDLE ID, NOT THE DAEMON'S. Two NSApplications sharing one
/// identifier is the shape of a bug this project has already paid for once in
/// another form -- two processes answering to the same name, and the window
/// server activating whichever it pleases.
let playerURL = appURL.appendingPathComponent(
    "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player")
