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
