"""install.sh, the OpenClip extension, the player and the demo must agree on the names.

A rename half done is worse than no rename: install.sh builds calliope-player while the
extension still launches kokoro-player, and Speak does nothing at all. Each of these reads the
name out of two files that have to match.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "install.sh").read_text()
ACTION = (ROOT / "openclip/calliope.py").read_text()
MAIN = (ROOT / "player/main.swift").read_text()
DEFAULTS = (ROOT / "player/defaults.swift").read_text()
DIRECTOR = (ROOT / "demo/director.swift").read_text()
PATHS = (ROOT / "shared/paths.swift").read_text()


def shell(name):
    return re.search(rf'^{name}="([^"]+)"', INSTALL, re.M).group(1).replace("$HOME", "~")


def test_the_extension_launches_the_player_where_the_installer_puts_it():
    """The player moved into Calliope.app, so there are now three spellings of
    one path -- the installer's, the extension's and the Swift that spawns it --
    and Speak does nothing at all if any of them drifts. The relative part is
    compared verbatim; the app's own location is substituted by install.sh, so
    an install somewhere else cannot leave the extension pointing at
    /Applications."""
    inside = "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player"
    assert inside in ACTION, "the extension does not look inside the app bundle"
    assert inside in PATHS, "the Swift and the extension disagree about the player"
    assert '"$helper/MacOS/calliope-player"' in INSTALL, \
        "the installer no longer builds the player where the others look for it"
    assert '"s|__APP__|$app|"' in INSTALL and "__APP__" in ACTION, \
        "the extension hardcodes a location the installer can be told to change"


def test_everything_agrees_on_the_runtime_directory():
    runtime = shell("runtime")
    assert runtime == "~/.local/share/calliope"
    assert re.search(r'RUNTIME = os\.path\.expanduser\("([^"]+)"\)', ACTION).group(1) == runtime
    # One Swift definition now, in shared/paths.swift, compiled into all three.
    assert re.search(r'runtimeURL = URL\(fileURLWithPath: NSString\(string: "([^"]+)"\)',
                     PATHS).group(1) == runtime
    for source, who in ((MAIN, "the player"), (DIRECTOR, "the demo")):
        assert "runtimeURL = URL(" not in source, f"{who} still defines its own runtime path"
    # AND THE SERVER, WHICH IS NOT SWIFT AND WAS THEREFORE MISSED. It used to
    # load the model from beside itself; inside the bundle that is a directory
    # with no model in it, and the whole stack failed to start with
    # "NO_SUCHFILE ... Contents/Resources/kokoro-v1.0.onnx".
    server = (ROOT / "server/server.py").read_text()
    assert re.search(r'RUNTIME = os\.path\.expanduser\("([^"]+)"\)', server).group(1) == runtime
    assert "os.path.dirname(os.path.abspath(__file__))" not in server, \
        "the server locates the model relative to itself again"


def test_the_installed_script_is_the_one_the_manifest_names():
    manifest = json.loads((ROOT / "openclip/openclip.json").read_text())
    named = {action["script"] for action in manifest["actions"]}
    # Copied verbatim, or rewritten on the way in -- calliope.py is sed'd so the
    # app's location is substituted. Either way it has to land in $extension.
    installed = set(re.findall(r'"\$here/openclip/([\w.]+)"[^\n]*"\$extension/[\w.]+"', INSTALL))
    installed |= set(re.findall(r'"\$here/openclip/([\w.]+)" > "\$extension/[\w.]+"', INSTALL))

    assert named == {"calliope.py"}
    assert named <= installed
    for script in named:
        assert (ROOT / "openclip" / script).exists()


def test_the_manifest_carries_the_product_name():
    manifest = json.loads((ROOT / "openclip/openclip.json").read_text())

    assert manifest["identifier"] == "com.gabrielbelli.calliope"
    assert all(action["id"].startswith("com.gabrielbelli.calliope.") for action in manifest["actions"])


def test_the_demo_reads_the_suite_the_player_writes():
    """The demo saves and restores the user's speed and reader state around a recording. Against
    the wrong suite it saves nothing and restores nothing, and the settings it did change stay
    changed."""
    suite = re.search(r'defaultsSuiteName = "([\w.-]+)"', DEFAULTS).group(1)

    assert suite == "com.gabrielbelli.calliope-player"
    assert re.search(r'UserDefaults\(suiteName: "([\w.-]+)"\)', DIRECTOR).group(1) == suite


def test_the_player_opens_its_settings_only_through_playerDefaults():
    """One way in, because the way in is what runs the carry. A UserDefaults built anywhere in
    main.swift reads the new suite straight, skips the carry and hands back an empty store, and
    that is exactly the silent reset — with the whole of defaults.swift still present and its
    own tests still green."""
    assert re.search(r"^let settings = playerDefaults\(\)$", MAIN, re.M)
    assert "UserDefaults(" not in MAIN


def test_the_old_install_is_named_where_it_is_retired():
    assert shell("old_runtime") == "~/.local/share/kokoro-tts"
    assert shell("old_extension") == "~/.openclip/extensions/kokoro.openclipext"
    assert re.search(r'legacyDefaultsSuiteName = "([\w.-]+)"', DEFAULTS).group(1) == "com.gabrielbelli.kokoro-player"
