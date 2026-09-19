#!/usr/bin/env bash
# Builds calliope-player and installs it, its local server and the OpenClip extension.
# Safe to re-run after changing anything in this folder.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
runtime="$HOME/.local/share/calliope"

# THE APPLICATION, WHICH IS NEW AND IS NOT A STYLE CHOICE. As two bare
# executables this could not open at login -- SMAppService.mainApp reports
# notFound without a bundle identifier, register() throws, and the checkbox in
# Settings was a control that did nothing and said nothing. An app also has one
# identity for macOS to grant Accessibility to and for Gatekeeper to check,
# which is what shipping this anywhere but this machine requires.
app="${CALLIOPE_APP_DIR:-/Applications}/Calliope.app"
version="${CALLIOPE_VERSION:-0.1.0}"
# Ad-hoc by default, which is enough to run here and not enough to run
# elsewhere. Set this to a "Developer ID Application: ..." identity to produce
# something notarisable.
identity="${CALLIOPE_SIGN_IDENTITY:--}"
extension="$HOME/.openclip/extensions/calliope.openclipext"
old_runtime="$HOME/.local/share/kokoro-tts"
old_extension="$HOME/.openclip/extensions/kokoro.openclipext"
models="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

# AN OLD INSTALL LEFT IN PLACE PUTS TWO SPEAK ACTIONS IN THE OPENCLIP MENU, and only one of them
# is the one this script updates. They are indistinguishable in the menu, so the user picks the
# wrong one about half the time and gets whatever the player was months ago, from a runtime this
# script no longer touches. The old server is stopped before its directory goes: it holds the
# model open and would otherwise run on for up to fifteen idle minutes against deleted files.
#
# This runs only after a successful install, never before, because it removes the only copy of
# the old runtime. Anything worth keeping out of it — the model files are 330 MB — must already
# have been copied into the new runtime by the time this is called.
retire_old_install() {
    if [ "$old_runtime" != "$runtime" ] && [ -d "$old_runtime" ]; then
        pkill -TERM -f "$old_runtime/server.py" || true
        rm -rf "$old_runtime"
    fi
    if [ "$old_extension" != "$extension" ] && [ -d "$old_extension" ]; then
        rm -rf "$old_extension"
    fi
}

mkdir -p "$runtime"

echo "==> Python runtime"
[ -x "$runtime/.venv/bin/python" ] || uv venv --python 3.12 "$runtime/.venv"
VIRTUAL_ENV="$runtime/.venv" uv pip install --quiet -r "$here/server/requirements.txt"

echo "==> Model files"
for file in kokoro-v1.0.onnx voices-v1.0.bin; do
    # The rename moved the runtime directory, not the model: 330 MB already on this disk under
    # the old name would otherwise be downloaded again. Copied, not moved, so a failure further
    # down leaves the old install whole and re-runnable.
    [ -f "$runtime/$file" ] || [ ! -f "$old_runtime/$file" ] || cp "$old_runtime/$file" "$runtime/$file"
    # DOWNLOADED BESIDE ITS NAME AND RENAMED, because [ -f ] cannot tell 310 MB
    # from 3 MB. A Ctrl-C or a dropped connection used to leave truncated bytes
    # at the final name, every later run then skipped the download, and the
    # failure surfaced far away as an onnxruntime parse error on a corrupt
    # model. The rename is atomic on one filesystem, so the name exists only
    # once the bytes are all there.
    if [ ! -f "$runtime/$file" ]; then
        curl -fL --progress-bar -o "$runtime/$file.part" "$models/$file"
        mv "$runtime/$file.part" "$runtime/$file"
    fi
done

# BUILT INTO A STAGING DIRECTORY, MOVED IN ONE STEP. Compiling straight into
# /Applications would leave a half-written app there if swiftc failed, and that
# half-written app is what the login item and Gatekeeper would then be pointing
# at. The move at the end is the only moment the installed app is not whole.
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
contents="$staging/Calliope.app/Contents"
helper="$contents/Helpers/CalliopePlayer.app/Contents"
mkdir -p "$contents/MacOS" "$contents/Resources" "$helper/MacOS"

# -target, AND IT IS NOT OPTIONAL. Without it swiftc stamps the Mach-O with
# whatever minimum it infers from the host -- measured here as minos 28.0 on a
# machine running 27.0 -- and LaunchServices refuses to open the app at all:
# "_LSOpenURLsWithCompletionHandler() failed with error -10825", which is
# kLSIncompatibleSystemVersionErr. Running the binary directly bypasses
# LaunchServices and works, so this is invisible until somebody double-clicks
# the app, which is what everybody but the person building it does.
#
# It must agree with LSMinimumSystemVersion in the Info.plist; 26.0 is where
# NSGlassEffectView arrives, which is what the capsule is made of.
# The architecture is the HOST's, not a constant: hardcoding arm64 on an Intel
# Mac running macOS 26 cross-builds a thin binary that machine cannot execute,
# and the installer would print "Installed." over it.
target="$(uname -m)-apple-macos26.0"

echo "==> Daemon"
swiftc -O -swift-version 5 -target "$target" "$here/shared/paths.swift" \
    "$here/daemon/main.swift" -o "$contents/MacOS/calliope-daemon"

echo "==> Player"
swiftc -O -swift-version 5 -target "$target" "$here/shared/paths.swift" \
    "$here/player/main.swift" "$here/player/defaults.swift" \
    -o "$helper/MacOS/calliope-player"

echo "==> Server"
install -m 0644 "$here/server/server.py" "$contents/Resources/server.py"

echo "==> Bundle"
sed "s/__VERSION__/$version/g" "$here/bundle/Calliope-Info.plist" > "$contents/Info.plist"
sed "s/__VERSION__/$version/g" "$here/bundle/CalliopePlayer-Info.plist" > "$helper/Info.plist"

# INSIDE OUT. codesign seals what it finds, so a nested bundle signed after its
# container invalidates the container's seal. --deep does this order for you and
# Apple has deprecated it for notarisation, so it is done by hand.
sign_options=()
[ "$identity" = "-" ] || sign_options=(--options runtime --timestamp)
codesign --force --sign "$identity" "${sign_options[@]+"${sign_options[@]}"}" \
    "$staging/Calliope.app/Contents/Helpers/CalliopePlayer.app"
codesign --force --sign "$identity" "${sign_options[@]+"${sign_options[@]}"}" \
    "$staging/Calliope.app"
codesign --verify --strict --deep "$staging/Calliope.app"

echo "==> OpenClip extension"
mkdir -p "$extension"
install -m 0644 "$here/openclip/openclip.json" "$extension/openclip.json"
sed "s|__APP__|$app|" "$here/openclip/calliope.py" > "$extension/calliope.py"
chmod 0755 "$extension/calliope.py"

# A running server keeps the old code until it idles out; stop it so the next
# Speak starts the new one. The daemon goes with it: it holds the server open
# now (CALLIOPE_IDLE_SECONDS=0), so a daemon left running would keep the OLD
# server alive for ever and the upgrade would appear not to have happened.
# BY NAME, NOT BY FULL PATH, AND THIS WAS WRONG AND MEASURED. A daemon started
# as ./calliope-daemon has exactly that on its command line, so a pattern built
# from $runtime matched nothing -- the old one survived the upgrade, and its
# single-instance guard then turned the new one away on sight. The symptom is
# an install that succeeds while the old binary keeps running, holding the old
# server open, which is the exact failure this line exists to prevent.
pkill -TERM -f calliope-daemon || true
pkill -TERM -f "$runtime/server.py" || true
pkill -TERM -f "$app/Contents/Resources/server.py" || true

echo "==> Installing $app"
rm -rf "$app"
mkdir -p "$(dirname "$app")"
mv "$staging/Calliope.app" "$app"

# The two loose binaries this replaces. Left behind they are a second, older
# copy that nothing updates, and the OpenClip action used to point at one.
rm -f "$runtime/calliope-daemon" "$runtime/calliope-player" "$runtime/server.py"

# TELL LAUNCHSERVICES THE APP EXISTS. Measured: until it knows, SMAppService
# reports notFound for an app sitting in /Applications, so the Open at Login
# checkbox reads as off and cannot be turned on. Moving a bundle into place is
# not something it notices on its own.
lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$lsregister" ] && "$lsregister" -f "$app" || true

retire_old_install

# PUT THE MENU BAR BACK. This script stops the running daemon so the upgrade
# takes effect, and used to leave it stopped -- so every re-run silently killed
# the hotkey until somebody noticed and opened the app by hand, while the text
# below read like first-run instructions. -g keeps it in the background, and
# the app is LSUIElement, so nothing appears but the status item.
open -g "$app" || true

echo
echo "Installed."
echo
echo "  One-shot, as before:  select text, click Speak in OpenClip."
echo
echo "  Resident:             open $app"
echo "                        a menu bar icon, the model kept warm, and"
echo "                        Option-Command-S to speak the selection."
echo
echo "  The hotkey needs Accessibility permission -- there is no way to read"
echo "  another application's selection without it. The daemon asks the first"
echo "  time you press it, and does nothing until you agree."
echo
echo "  macOS grants that permission to a particular copy of a program, so"
echo "  this install asks again: it is an application now, where it used to be"
echo "  a loose binary, and that is a different thing as far as macOS is"
echo "  concerned. Once. Open at login is in Settings and now works."
