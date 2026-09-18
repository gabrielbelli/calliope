"""The resident half: what it owns, what it must not own, and the two contracts
it shares with the one-shot half.

The daemon cannot be launched to test it -- it takes a menu bar slot, registers
a system-wide hotkey and asks for Accessibility permission. So these read the
source, which is the same bargain the rest of this suite makes and the reason it
is worth being strict about what the source has to say.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent.parent
DAEMON = (HERE / "daemon" / "main.swift").read_text()
PLAYER = (HERE / "player" / "main.swift").read_text()
SERVER = (HERE / "server" / "server.py").read_text()
INSTALL = (HERE / "install.sh").read_text()
OPENCLIP = (HERE / "openclip" / "calliope.py").read_text()


def code(source: str) -> str:
    r"""The same Swift with its comments gone.

    EVERY NEGATIVE ASSERTION IN THIS FILE HAS TO USE THIS, and the first draft
    did not. Comments here name the defect they prevent -- "SIGTERM, NEVER
    SIGKILL", "a private event source, not .hidSystemState" -- so they quote the
    very string the assertion forbids, and `"SIGKILL" not in DAEMON` failed
    against the sentence explaining why SIGKILL is not used.

    A test that matches its own documentation is worse than no test: it fails
    when the rule is written down and passes when it is merely obeyed silently.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


DAEMON_CODE = code(DAEMON)


def test_the_daemon_is_the_only_half_that_is_resident():
    """The split is the design: the model is expensive and lives in the daemon,
    the window is cheap and dies with each utterance. A daemon that also drew
    the capsule, or a player that kept the model, would be one process doing two
    jobs with two lifetimes."""
    assert "NSStatusItem" in DAEMON, "the daemon has no menu bar presence"
    assert "setActivationPolicy(.accessory)" in DAEMON, \
        "without .accessory it takes a Dock icon and steals focus from the reader"
    # And the player still terminates. If this ever stops being true, the split
    # has quietly collapsed into one long-running process.
    assert "NSApp.terminate" in PLAYER, "the player is no longer one-shot"


def test_the_daemon_holds_the_server_open_and_the_player_does_not():
    """A server that shuts itself down underneath the daemon makes "kept warm" a
    claim rather than a behaviour -- the next press pays the 1.7-1.9 s load
    again. Started by the one-shot player, though, the timeout is the only thing
    that ever closes it, so the default must stay."""
    assert 'CALLIOPE_IDLE_SECONDS' in SERVER, "the idle exit is not owner-decided"
    assert 'os.environ.get("CALLIOPE_IDLE_SECONDS", 15 * 60)' in SERVER, \
        "the default stopped being fifteen minutes, which is the one-shot case"
    assert "if IDLE_SECONDS <= 0:" in SERVER, "0 no longer means never"
    assert 'env["CALLIOPE_IDLE_SECONDS"] = "0"' in DAEMON, \
        "the daemon does not actually hold the server open"


def test_the_server_is_asked_to_stop_and_never_killed():
    """phonemizer copies libespeak-ng into a temporary directory per process and
    removes it only on a normal exit, which is why server.py turns SIGTERM into
    sys.exit. Killed outright it leaves that copy behind, every time."""
    assert ".terminate()" in DAEMON
    assert "SIGKILL" not in DAEMON_CODE, "a killed server leaks its espeak copy"


def test_both_ways_of_starting_a_player_can_stop_each_other():
    """THE DEFECT THIS EXISTS TO PREVENT IS TWO VOICES AT ONCE. There are two
    coordinators now -- the OpenClip script and the daemon -- and each spawns
    players. The script stops the previous one with killpg(pid), which only
    reaches the process if it is the group LEADER. That used to be arranged by
    the caller (start_new_session=True), which the daemon's Process has no
    equivalent for, so a hotkey-started player was not a leader and the script's
    stop silently found nothing to signal.

    Two halves, and both are needed: the player makes itself a leader whoever
    started it, and the daemon writes the same pid file the script reads."""
    assert "setsid()" in PLAYER, \
        "the player is not a group leader unless its caller remembers to arrange it"
    assert "player.pid" in DAEMON, "the daemon's player cannot be stopped from outside"
    assert "killpg" in OPENCLIP and "player.pid" in OPENCLIP, \
        "the contract this test is about no longer exists on the other side"


def test_the_pasteboard_is_put_back():
    """Reading a selection means posting a synthetic Command-C, which overwrites
    whatever the person had copied. Leaving it there means their next paste is
    wrong and they will not connect it to this."""
    assert "restore(" in DAEMON, "the clipboard is taken and not returned"
    assert "changeCount" in DAEMON, \
        "the pasteboard is read once rather than waited on, so a slow app gives stale text"


def test_the_synthetic_keystroke_uses_a_private_event_source():
    """Measured once already, in the demo director: events posted from
    .hidSystemState leak their modifier flags into whatever the user does next,
    so a later drag arrives with Command still held."""
    assert "CGEventSource(stateID: .privateState)" in DAEMON
    assert ".hidSystemState" not in DAEMON_CODE


def test_the_daemon_asks_for_permission_rather_than_failing_silently():
    """There is no way to read another application's selection without
    Accessibility. A hotkey that does nothing, with no explanation, is the worst
    version of that."""
    assert "AXIsProcessTrusted()" in DAEMON
    assert "kAXTrustedCheckOptionPrompt" in DAEMON, "it never asks"
    assert "Accessibility" in DAEMON, "the menu never says why nothing happens"


def test_a_second_daemon_cannot_start():
    """Two would fight over the hotkey and, later, the port. The symptom is a
    hotkey that silently stops working rather than an error, which is the kind
    of thing that gets diagnosed as "the app is broken"."""
    assert "calliope-daemon" in DAEMON and "runningApplications" in DAEMON


def test_the_daemon_is_built_and_explained_by_the_installer():
    """An installer that builds one half and not the other leaves a menu bar app
    that is whatever version happened to be there."""
    assert "daemon/main.swift" in INSTALL, "install.sh does not build the daemon"
    assert re.search(r"pkill.*calliope-daemon", INSTALL), \
        "an old daemon survives the upgrade and holds the OLD server open for ever"
    assert "Accessibility" in INSTALL, \
        "nothing tells the user the hotkey needs a permission"


def test_the_daemon_and_the_player_agree_on_where_things_are():
    """A REAL DEFECT, AND IT SURVIVED A GREEN SUITE. The daemon was written with
    ".venv/bin/python" as "venv/bin/python3" -- from memory rather than from the
    installer -- so it matched nothing, and the menu reported "not installed:
    run install.sh" on a machine where install.sh had just succeeded. Every
    other test passed, because they all read the source and none of them
    compared one file's idea of a path with another's.

    Three files name these paths and all three must agree: install.sh puts them
    there, the player finds the interpreter, the daemon finds the interpreter
    and the player.
    """
    for path in (".venv/bin/python", "server.py"):
        assert path in INSTALL, f"install.sh no longer creates {path}"
        assert path in DAEMON_CODE, f"the daemon does not look for {path}"
    assert ".venv/bin/python" in PLAYER, "the player and the daemon disagree"
    assert "calliope-player" in INSTALL and "calliope-player" in DAEMON_CODE, \
        "the daemon cannot find the binary the installer builds"
    # And nothing looks for the pre-rename layout.
    assert "kokoro-tts" not in DAEMON_CODE, "the daemon points at the old runtime"


def test_the_daemon_is_local_only_like_the_player():
    """GAB-635 removed the remote path from the reader. The daemon is a new
    surface on the same app and must not reintroduce it: the proxy to a Calliope
    server is a later, opt-in step, and until it exists there is one host here."""
    urls = set(re.findall(r'"(https?://[^"]+)"', DAEMON_CODE))
    assert urls <= {"http://127.0.0.1:47815"}, f"the daemon reaches elsewhere: {urls}"
    assert "Authorization" not in DAEMON_CODE and "apiKey" not in DAEMON_CODE
