#!/usr/bin/env bash
# Builds the artefact a Homebrew cask installs: a self-contained Calliope.app,
# tarred, with its checksum.
#
# BUILT HERE RATHER THAN IN CI, and that is a constraint rather than a choice.
# The capsule is NSGlassEffectView, which needs the macOS 26 SDK, and GitHub's
# runners are behind. The machine that can build this is the one that uses it.
#
#   clients/macos-player/release.sh 0.1.0
#   gh release create v0.1.0 dist/calliope-0.1.0-arm64.tar.gz --generate-notes
set -euo pipefail

version="${1:?usage: release.sh <version>}"
here="$(cd "$(dirname "$0")" && pwd)"
dist="$here/dist"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

contents="$staging/Calliope.app/Contents"
helper="$contents/Helpers/CalliopePlayer.app/Contents"
mkdir -p "$contents/MacOS" "$contents/Resources" "$helper/MacOS" "$dist"

# Must agree with LSMinimumSystemVersion; see install.sh for what happens when
# it is left to swiftc to infer.
target="$(uname -m)-apple-macos26.0"

echo "==> Code"
swiftc -O -swift-version 5 -target "$target" "$here/shared/paths.swift" \
    "$here/daemon/main.swift" -o "$contents/MacOS/calliope-daemon"
swiftc -O -swift-version 5 -target "$target" "$here/shared/paths.swift" \
    "$here/player/main.swift" "$here/player/defaults.swift" \
    -o "$helper/MacOS/calliope-player"
install -m 0644 "$here/server/server.py" "$contents/Resources/server.py"
sed "s/__VERSION__/$version/g" "$here/bundle/Calliope-Info.plist" > "$contents/Info.plist"
sed "s/__VERSION__/$version/g" "$here/bundle/CalliopePlayer-Info.plist" > "$helper/Info.plist"

# SEALED IN, NEVER WRITTEN TO. A cask installs the .app and nothing else, so
# anything the app needs at runtime has to be inside it: an interpreter it does
# not have is an app that installs cleanly and can never speak. Built once here
# and covered by the signature.
# AN INTERPRETER, COPIED IN -- NOT A VENV. `uv venv` writes bin/python as a
# symlink to uv's own managed Python, which is an absolute path into the build
# machine's home directory. codesign refuses it outright ("invalid destination
# for symbolic link in bundle") and it would have been worse if it had not:
# an app that installs cleanly on somebody else's Mac and points at an
# interpreter that is not there.
#
# python-build-standalone, which is what uv manages, derives sys.prefix from
# its own executable path at run time. Measured: copied elsewhere it reports
# the copy, and no symlink inside it is absolute. 72 MB.
echo "==> Python"
uv python install 3.12 >/dev/null 2>&1 || true
# `uv python find` prints the interpreter; its prefix is two directories up,
# and -P resolves the version symlink so the copy is of real files.
source_python="$(cd "$(dirname "$(uv python find 3.12)")/.." && pwd -P)"
cp -R "$source_python" "$contents/Resources/python"
uv pip install --quiet --python "$contents/Resources/python/bin/python3" \
    --break-system-packages -r "$here/server/requirements.txt"

echo "==> Model"
models="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
cache="${CALLIOPE_MODEL_CACHE:-$HOME/.local/share/calliope}"
for file in kokoro-v1.0.onnx voices-v1.0.bin; do
    if [ -f "$cache/$file" ]; then
        cp "$cache/$file" "$contents/Resources/$file"
    else
        curl -fL --progress-bar -o "$contents/Resources/$file.part" "$models/$file"
        mv "$contents/Resources/$file.part" "$contents/Resources/$file"
    fi
done

# INSIDE OUT: codesign seals what it finds, so a nested bundle signed after its
# container invalidates the container's seal. Ad-hoc, because the cask strips
# the quarantine attribute that would otherwise make Gatekeeper ask about it.
echo "==> Signature"
codesign --force --sign - "$contents/Helpers/CalliopePlayer.app"
codesign --force --sign - "$staging/Calliope.app"
codesign --verify --strict --deep "$staging/Calliope.app"

echo "==> Archive"
archive="$dist/calliope-$version-$(uname -m).tar.gz"
# --no-xattrs: a tarball carrying the build machine's extended attributes is a
# tarball carrying its quarantine flags.
tar --no-xattrs -czf "$archive" -C "$staging" Calliope.app
shasum -a 256 "$archive" | tee "$archive.sha256"
echo
echo "  $(du -h "$archive" | cut -f1)  $archive"
