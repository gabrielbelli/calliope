#!/usr/bin/env bash
# Builds calliope-player and installs it, its local server and the OpenClip extension.
# Safe to re-run after changing anything in this folder.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
runtime="$HOME/.local/share/calliope"
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
    [ -f "$runtime/$file" ] || curl -fL --progress-bar -o "$runtime/$file" "$models/$file"
done

echo "==> Server"
install -m 0644 "$here/server/server.py" "$runtime/server.py"

echo "==> Player"
swiftc -O -swift-version 5 "$here/player/main.swift" "$here/player/defaults.swift" -o "$runtime/calliope-player"

echo "==> Daemon"
swiftc -O -swift-version 5 "$here/daemon/main.swift" -o "$runtime/calliope-daemon"

echo "==> OpenClip extension"
mkdir -p "$extension"
install -m 0644 "$here/openclip/openclip.json" "$extension/openclip.json"
install -m 0755 "$here/openclip/calliope.py" "$extension/calliope.py"

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

retire_old_install

echo
echo "Installed."
echo
echo "  One-shot, as before:  select text, click Speak in OpenClip."
echo
echo "  Resident, new:        $runtime/calliope-daemon"
echo "                        a menu bar icon, the model kept warm, and"
echo "                        Option-Command-S to speak the selection."
echo
echo "  The hotkey needs Accessibility permission -- there is no way to read"
echo "  another application's selection without it. The daemon asks the first"
echo "  time you press it, and does nothing until you agree."
echo
echo "  To start it at login, add it in System Settings > General > Login Items."
