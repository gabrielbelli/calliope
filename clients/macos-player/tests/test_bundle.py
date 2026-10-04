"""Calliope.app: what makes it an application rather than two loose binaries.

It became one because it had to. As bare executables there was no bundle
identifier, so SMAppService.mainApp reported notFound, register() threw, and the
Open at Login checkbox was a control that did nothing and said nothing about it.
There was also no identity for macOS to grant Accessibility to, and nothing for
Gatekeeper to check, which is what shipping it anywhere else requires.

These read the installer and the plists. The one exception compiles, because the
defect it guards was invisible in every source file.
"""
import platform
import plistlib
import re
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent.parent


def expand(target):
    """install.sh builds for the host, so the literal carries a substitution."""
    return target.replace("$(uname -m)", platform.machine())
INSTALL = (HERE / "install.sh").read_text()
APP_PLIST = plistlib.loads((HERE / "bundle" / "Calliope-Info.plist").read_bytes())
PLAYER_PLIST = plistlib.loads((HERE / "bundle" / "CalliopePlayer-Info.plist").read_bytes())


def test_the_two_halves_have_different_identities():
    """One identifier for two NSApplications leaves the window server to choose
    between them. The daemon is resident and the player is spawned per passage,
    so they are running at the same time by design, not by accident."""
    assert APP_PLIST["CFBundleIdentifier"] == "com.gabrielbelli.calliope"
    assert PLAYER_PLIST["CFBundleIdentifier"] == "com.gabrielbelli.calliope.player"
    assert APP_PLIST["CFBundleIdentifier"] != PLAYER_PLIST["CFBundleIdentifier"]


def test_neither_half_takes_a_dock_icon():
    """A reader that steals focus from what is being read is the one thing it
    must never do. setActivationPolicy(.accessory) says this at runtime;
    LSUIElement says it before the process exists, which is what the login-item
    machinery reads."""
    assert APP_PLIST["LSUIElement"] is True
    assert PLAYER_PLIST["LSUIElement"] is True


def test_the_deployment_target_is_stated_and_agrees_with_the_plist():
    """THE DEFECT THAT NO SOURCE FILE SHOWED. Without -target, swiftc stamps the
    Mach-O with a minimum it infers from the host -- measured as minos 28.0 on a
    machine running 27.0 -- and LaunchServices then refuses to open the app at
    all: kLSIncompatibleSystemVersionErr, -10825.

    Running the binary directly bypasses LaunchServices and works perfectly,
    which is how it was started all through development. Every person who
    double-clicks the app, or installs it from a cask, does the other thing.
    """
    target = re.search(r'^target="([^"]+)"', INSTALL, re.M)
    assert target, "install.sh does not state a deployment target"
    stated = target.group(1).split("macos")[-1]
    assert "$(uname -m)" in target.group(1), \
        "the architecture is hardcoded, so an Intel Mac gets a thin app it cannot run"
    assert APP_PLIST["LSMinimumSystemVersion"] == stated, \
        "the compiler and the Info.plist disagree about the oldest macOS this runs on"
    assert PLAYER_PLIST["LSMinimumSystemVersion"] == stated

    builds = [l for l in INSTALL.replace("\\\n", " ").splitlines()
              if l.strip().startswith("swiftc")]
    assert builds, "nothing is compiled"
    for line in builds:
        assert '-target "$target"' in line, f"compiled without a target: {line.strip()[:60]}"


def test_the_compiler_honours_that_target(tmp_path):
    """The agreement above is between two files; this is against the compiler.
    A -target that swiftc silently ignored would satisfy every assertion here
    and still produce the binary LaunchServices refuses."""
    target = expand(re.search(r'^target="([^"]+)"', INSTALL, re.M).group(1))
    source = tmp_path / "t.swift"
    source.write_text("print(1)\n")
    subprocess.run(["swiftc", "-swift-version", "5", "-target", target,
                    str(source), "-o", str(tmp_path / "t")], check=True,
                   capture_output=True)
    load = subprocess.run(["otool", "-l", str(tmp_path / "t")],
                          capture_output=True, text=True).stdout
    minos = re.search(r"minos (\S+)", load).group(1)

    assert minos == APP_PLIST["LSMinimumSystemVersion"], \
        f"swiftc stamped minos {minos}, the app claims {APP_PLIST['LSMinimumSystemVersion']}"


def test_the_nested_bundle_is_signed_before_its_container():
    """codesign seals what it finds, so signing the helper after the app
    invalidates the app's seal -- and the failure is at launch on somebody
    else's machine, not at build time here. --deep does this order for you and
    Apple has deprecated it for notarisation, so it is done by hand."""
    order = [m.start() for m in re.finditer(r"^codesign --force", INSTALL, re.M)]
    assert len(order) >= 2, "only one thing is signed"
    helper = INSTALL.index("CalliopePlayer.app\"", order[0])
    outer = INSTALL.index("Calliope.app\"", order[1])
    assert helper < outer, "the container is signed before the bundle inside it"
    assert "codesign --verify --strict --deep" in INSTALL, \
        "nothing checks that the signature it just made is valid"


def test_the_app_is_installed_whole_or_not_at_all():
    """Compiling straight into /Applications leaves a half-written app there
    when swiftc fails, and that is what the login item and Gatekeeper would then
    point at."""
    assert 'staging="$(mktemp -d)"' in INSTALL, "it builds in place"
    assert re.search(r'mv "\$staging/Calliope\.app" "\$app"', INSTALL), \
        "the finished app is not moved into place in one step"
    # The first swiftc that is a command, not the comment explaining -target.
    first = re.search(r"^swiftc ", INSTALL, re.M).start()
    assert INSTALL.index("mktemp -d") < first, \
        "something is compiled before there is a staging directory to compile into"


def test_the_bundle_holds_no_state():
    """A signed bundle that is written to is a bundle whose signature is broken,
    and the model is 310 MB that a re-install must not download again. So the
    code is inside and everything that changes stays in the runtime."""
    assert '"$contents/Resources/server.py"' in INSTALL, "the server is not in the bundle"
    assert re.search(r'uv venv[^\n]*"\$runtime/\.venv"', INSTALL), \
        "the Python environment is built somewhere other than the runtime"
    assert re.search(r'-o "\$runtime/\$file\.part"', INSTALL), \
        "the model is downloaded somewhere other than the runtime"


def test_launchservices_is_told_the_app_exists():
    """Measured: until it knows, SMAppService reports notFound for an app
    sitting in /Applications, so Open at Login reads as off and cannot be turned
    on. Moving a bundle into place is not something it notices."""
    assert "lsregister" in INSTALL and re.search(r'"\$lsregister" -f "\$app"', INSTALL), \
        "nothing registers the installed app with LaunchServices"


def test_the_installer_puts_the_menu_bar_back():
    """It stops the running daemon so the upgrade takes effect, and used to
    leave it stopped -- so every re-run silently killed the hotkey until
    somebody noticed, while the closing text read like first-run instructions.
    -g keeps it in the background; the app is LSUIElement, so nothing appears
    but the status item."""
    assert re.search(r'^open -g "\$app"', INSTALL, re.M), \
        "the daemon is stopped by the installer and never restarted"
    assert INSTALL.index("pkill -TERM -f calliope-daemon") < INSTALL.index('open -g "$app"'), \
        "it is restarted before it is stopped"


def test_the_app_wears_the_mark_the_page_already_wears():
    """It shipped with no icon at all and an SF Symbol waveform in the menu bar
    -- a stock glyph with no relation to anything else Calliope wears. The web
    UI carries this mark inline in its <head> and its masthead, and that page's
    own comment says of it: "it will be the app icon"."""
    mark = (HERE / "shared" / "mark.swift").read_text()
    for colour in ("1B / 255", "EB / 255", "FF / 255"):
        assert colour in mark, f"the mark no longer uses {colour}, which the page does"
    assert APP_PLIST["CFBundleIconFile"] == "Calliope"
    assert "make-icon.swift" in INSTALL and "Calliope.icns" in INSTALL, \
        "the installer does not build the icon"

    daemon = (HERE / "daemon" / "main.swift").read_text()
    assert "systemSymbolName" not in daemon, "the menu bar wears somebody else's glyph again"
    assert "Mark.draw(in: context, size: rect.width, monochrome: true)" in daemon
    assert "isTemplate = true" in daemon, \
        "a coloured status item ignores light, dark and being clicked"

    # And the OpenClip action, which was the same stock waveform.
    import json
    manifest = json.loads((HERE / "openclip" / "openclip.json").read_text())
    assert manifest["actions"][0]["icon"] == "icon.svg"
    svg = (HERE / "openclip" / "icon.svg").read_text()
    assert "currentColor" in svg, "the menu cannot tint it"
    assert "icon.svg" in INSTALL, "the installer does not ship it"


def test_every_size_of_the_icon_is_drawn_rather_than_scaled():
    """A 2.2-unit stroke does not survive 1024 downsampled to 16. iconutil
    takes an iconset of real bitmaps, so each one is rendered at its own size."""
    renderer = (HERE / "bundle" / "make-icon.swift").read_text()
    assert "icon_16x16" in renderer and "icon_512x512@2x" in renderer
    assert renderer.count("CGContext(data: nil") == 1, "more than one drawing path"
    assert "for (name, size) in wanted" in renderer, "the sizes are not iterated"


def test_the_monochrome_mark_fits_its_canvas():
    """WITH THE PLATE DROPPED THE INK IS ONLY THE MIDDLE OF THE SQUARE. The
    colour icon's rounded plate fills its canvas; a template image has no plate,
    so drawing from the same numbers left the swell and the lamp occupying about
    60 per cent of the height with a ring of empty space no other status item
    has — a menu bar icon that reads as small and adrift.

    So monochrome scales by the ink's own bounds rather than by the 32-unit
    grid, and the same refit is what makes openclip/icon.svg line up with the
    other extensions' icons."""
    mark = (HERE / "shared" / "mark.swift").read_text()
    assert "private static let ink" in mark, "the ink's bounds are not recorded"
    assert "min(size / ink.width, size / ink.height)" in mark, \
        "monochrome scales by the grid again, so the art floats in empty space"
    # And the colour path still fills the canvas, plate and all.
    assert ": size / grid" in mark, "the app icon stopped filling its square"


def test_the_openclip_action_is_only_installed_where_openclip_is():
    """It used to mkdir -p its way in regardless, so a Mac that had never had
    OpenClip ended up with a ~/.openclip/extensions tree holding one extension
    for an application that is not there. OpenClip is optional — the hotkey,
    the menu bar item and the `calliope` command all work without it — and an
    optional integration should leave no trace when it is not taken."""
    assert re.search(r'if \[ -d "\$HOME/\.openclip" \]', INSTALL), \
        "the extension is written whether or not OpenClip exists"
    guarded = INSTALL.split('if [ -d "$HOME/.openclip" ]')[1].split("\nfi")[0]
    assert "$extension/openclip.json" in guarded and "$extension/icon.svg" in guarded, \
        "part of the extension is written outside the guard"
