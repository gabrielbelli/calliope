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


def shell(name):
    return re.search(rf'^{name}="([^"]+)"', INSTALL, re.M).group(1).replace("$HOME", "~")


def test_the_extension_launches_the_binary_install_builds():
    built = re.search(r'swiftc .* -o "\$runtime/([\w-]+)"', INSTALL).group(1)
    launched = re.search(r'PLAYER = os\.path\.join\(RUNTIME, "([\w-]+)"\)', ACTION).group(1)

    assert built == launched == "calliope-player"


def test_everything_agrees_on_the_runtime_directory():
    runtime = shell("runtime")
    assert runtime == "~/.local/share/calliope"
    assert re.search(r'RUNTIME = os\.path\.expanduser\("([^"]+)"\)', ACTION).group(1) == runtime
    assert re.search(r'runtimeURL = URL\(fileURLWithPath: NSString\(string: "([^"]+)"\)', MAIN).group(1) == runtime
    assert re.search(r'runtimeURL = URL\(fileURLWithPath: NSString\(string: "([^"]+)"\)', DIRECTOR).group(1) == runtime


def test_the_installed_script_is_the_one_the_manifest_names():
    manifest = json.loads((ROOT / "openclip/openclip.json").read_text())
    named = {action["script"] for action in manifest["actions"]}
    installed = set(re.findall(r'install -m \d+ "\$here/openclip/([\w.]+)" "\$extension/[\w.]+"', INSTALL))

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
