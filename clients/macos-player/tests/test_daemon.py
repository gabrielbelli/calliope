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
    paths = (HERE / "shared" / "paths.swift").read_text()
    for path in (".venv/bin/python", "server.py", "calliope-player"):
        assert path in paths, f"shared/paths.swift no longer names {path}"
    assert ".venv/bin/python" in INSTALL, "install.sh no longer creates the interpreter"

    # THE FIX IS THAT THERE IS ONE SPELLING, so the thing to hold is that both
    # halves are built from it. A swiftc line that forgets this file does not
    # fail to compile -- it fails to link, or worse, compiles against a copy
    # somebody reintroduced.
    for target in ('"$contents/MacOS/calliope-daemon"', '"$helper/MacOS/calliope-player"'):
        line = [l for l in INSTALL.replace("\\\n", " ").splitlines()
                if target in l and l.strip().startswith("swiftc")]
        assert line, f"install.sh does not build {target}"
        assert "shared/paths.swift" in line[0], f"{target} is built without the shared paths"
    for source, who in ((DAEMON_CODE, "the daemon"), (code(PLAYER), "the player")):
        assert "runtimeURL = URL(" not in source, f"{who} defines its own copy again"
    # And nothing looks for the pre-rename layout.
    assert "kokoro-tts" not in DAEMON_CODE, "the daemon points at the old runtime"


def test_the_menu_only_offers_settings_that_change_something():
    """THE RULE THIS FILE EXISTS TO HOLD, and it is about what is ABSENT. A
    switch that toggles nothing is worse than no switch: it tells somebody the
    feature is there, and the bug report is "I turned it on and nothing
    happened".

    There is no "local API" toggle for the same reason: the loopback server is
    not an optional extra, it is how speech happens at all -- the player fetches
    from it -- so turning it off would turn the hotkey off with it, which is not
    what anybody would expect that switch to mean.

    The Calliope connection waited for this rule and now satisfies it: the
    fields are in the window because server.py forwards, and the test below
    holds them to reaching it."""
    assert "localAPI" not in DAEMON_CODE and "enableAPI" not in DAEMON_CODE, \
        "a switch for the loopback server, which cannot be switched off without "\
        "switching off speech"
    # And what IS there reaches something real.
    assert "SMAppService" in DAEMON_CODE, "Open at Login does not use the API that owns it"
    assert 'settings.set(step, forKey: "speed")' in DAEMON_CODE, \
        "the speed menu writes somewhere the player does not read"
    assert '"speed"' in (HERE / "player" / "defaults.swift").read_text(), \
        "the player no longer carries the key the daemon writes"


def test_the_settings_window_says_what_a_menu_cannot():
    """The two controls are in the menu as well, and on their own they would
    not have earned a window. What earns it is the rest of the panel: whether
    the permission is granted, whether the model is warm, and where the log is
    -- because the answer to "the hotkey did nothing" is in that file and
    nobody should have to be told its path."""
    assert "NSWindow(" in DAEMON_CODE
    assert "Accessibility" in DAEMON_CODE and "daemon.log" in DAEMON_CODE, \
        "the window does not say why the hotkey might be silent"
    # AND IT REFRESHES WHEN OPENED. A permission granted in System Settings
    # happens outside this process, so a panel that reads its state once is
    # wrong from the moment somebody acts on it.
    assert "refreshSettings()" in DAEMON_CODE


def test_the_daemon_owns_its_server_rather_than_adopting_one():
    """A REAL DEFECT, SEEN IN THE MENU. The first version adopted whatever was
    already listening on 47815 and said so for as long as it ran. Adopting
    reads well and behaves badly: the daemon cannot hold open a process it does
    not own, cannot restart it when it dies, and cannot tell it to skip the
    idle timeout -- so "Kokoro is warm" became a claim about somebody else's
    server, permanently, with no route back.

    Measured on the machine: the listener's parent was launchd, which is what a
    process looks like once the daemon that started it has gone. Every crash
    and every reinstall over a running binary leaves one, so this is the common
    case."""
    assert "reclaimPort()" in DAEMON_CODE, "the port is not reclaimed"
    assert "adopted a server" not in DAEMON_CODE, "it still adopts"
    # Signalled by script path, never by port: something else listening there
    # is a conflict to report, not a process to kill.
    assert '"-TERM", "-f", script' in DAEMON_CODE, \
        "it kills by port, so it could signal a process that is not ours"


def test_the_daemon_is_local_only_like_the_player():
    """GAB-635 removed the remote path from the reader, and the proxy did not
    put it back HERE. One process makes the remote call -- server.py -- and the
    daemon's job is to hand it the address and the credential, so the daemon
    still speaks to 127.0.0.1 and nothing else.

    That is why the Connect button asks the proxy rather than the Calliope
    server: testing the address directly would prove a path nothing uses, and
    would pass while the thing the player actually talks to could not reach it."""
    hosts = {u.split("/")[2] for u in re.findall(r'"(https?://[^"]+)"', DAEMON_CODE)}
    assert hosts <= {"127.0.0.1:47815"}, f"the daemon reaches elsewhere: {hosts}"
    assert "Authorization" not in DAEMON_CODE, \
        "the daemon authenticates to the remote itself instead of handing the key on"


def test_the_calliope_fields_reach_the_process_that_reads_them():
    """THE DEFECT THIS PREVENTS IS A SETTING THAT SAVES AND DOES NOTHING. Three
    things have to line up and none of them is visible from the others:

    server.py reads the address from the ENVIRONMENT, which is read once at
    startup -- so a URL saved into a running server changes nothing until it is
    restarted. The daemon is the only process that knows both halves, so it is
    the one that passes them. And the key is a credential, so it lives in the
    Keychain rather than in a plist that rides in every backup."""
    for name in ("CALLIOPE_URL", "CALLIOPE_KEY"):
        assert name in SERVER, f"server.py no longer reads {name}"
        assert f'env["{name}"]' in DAEMON_CODE, f"the daemon never passes {name}"
    assert "server.restart()" in DAEMON_CODE, \
        "the settings save without restarting the server that only reads them at startup"
    assert "kSecClassGenericPassword" in DAEMON_CODE, "the key is not in the Keychain"
    assert "kSecAttrAccessibleWhenUnlockedThisDeviceOnly" in DAEMON_CODE, \
        "a speech key syncs to every other device on the account"
    # Every UserDefaults key, literal or named. Two are expected; a third is
    # something new stored in a plist that rides in every backup, and the only
    # value here that must never do that is the key.
    keys = set(re.findall(r'forKey:\s*("?\w+"?)', DAEMON_CODE))
    assert keys <= {'"speed"', "urlKey", "onKey"}, \
        f"something new is in UserDefaults, and a credential must not be: {keys}"
    # And the round trip is proven rather than claimed: the button reads back
    # what the proxy says it can reach.
    assert "calliope-remote" in DAEMON_CODE and "calliope-remote" in SERVER, \
        "the two sides disagree about how a forwarded model is labelled"


def test_the_key_is_not_handed_to_an_unverified_connection():
    """A CREDENTIAL TRAVELS ON THIS CONNECTION, which is what makes certificate
    verification load-bearing rather than tidy: an unverified TLS session is one
    anything on the path can sit in the middle of, and it would be given the
    Authorization header on the way past.

    An earlier draft here disabled verification for every request, reasoning
    that a home server presents a certificate for a name it is not reached by.
    Measured against the real deployment that was false -- it answers on its own
    hostname with a certificate that verifies. Only a server typed in as a bare
    IP has no name for a certificate to match, and that is the one case left."""
    assert "context=UNVERIFIED" not in SERVER, \
        "a request skips verification unconditionally, and one of them carries the key"
    assert "def tls_for(" in SERVER and SERVER.count("tls_for(CALLIOPE_URL)") == 2, \
        "not every outbound request decides verification the same way"
    assert "ipaddress.ip_address(host)" in SERVER, \
        "the bare-IP exception is decided by something other than what an address is"


def test_an_unknown_model_is_refused_here_rather_than_guessed_at_there():
    """MEASURED, AND IT RETURNED AUDIO. A request for model "nonesuch" came back
    200 with eighteen kilobytes of MP3, because the proxy forwarded any name it
    did not recognise and the gateway answers an unknown model with a default
    instead of refusing. A typo produced speech in a voice nobody chose, and the
    proxy is what hid it -- the same substitution forward()'s own docstring
    calls a defect noticed only after the audio has been sent to somebody.

    The listing is refreshed once before refusing, because the cache is a minute
    old and a model added on the server inside that minute is a real name."""
    assert "def remote_model_names(fresh=False)" in SERVER, "the cache cannot be bypassed"
    assert "remote_model_names(fresh=True)" in SERVER, \
        "a name added in the last minute is refused as a typo"
    assert SERVER.count('"no_such_model"') == 2, \
        "the unknown-model refusal is gone, or there is a third path that differs"


def test_an_upgrade_stops_the_running_daemon():
    """A DAEMON STARTED AS ./calliope-daemon HAS EXACTLY THAT ON ITS COMMAND
    LINE, so a pkill pattern built from the runtime path matched nothing. The
    old daemon survived install.sh, and its own single-instance guard then
    turned the new one away -- an install that reports success while the old
    binary keeps running and holds the old server open. Seen, not imagined."""
    assert re.search(r"pkill -TERM -f calliope-daemon", INSTALL), \
        "the installer matches the daemon by a path it may not have been started with"


def test_the_calliope_section_is_one_line_until_it_is_wanted():
    """SEEN IN A SCREENSHOT AND CALLED BAD AND UNINTUITIVE, which it was: two
    fields and a Connect button, always visible, made the common case -- one
    Mac, nothing else -- look like something left half-configured.

    A switch says what the section is for in one line, and the fields belong to
    it rather than standing alongside. Three consequences, each of which was a
    decision:

    Off must not erase the address, or the only way back is to type it again.
    The URL therefore decides nothing on its own; the switch does.

    The fields commit themselves, because a switch that then needs a button
    pressed is one step more than it promised.

    And the secure field must not take focus on open: macOS anchors its
    Passwords popover to whatever is focused, and it landed on top of the
    button that used to be there."""
    assert "NSSwitch()" in DAEMON_CODE, "the section is not behind a switch"
    assert 'NSButton(title: "Connect"' not in DAEMON_CODE, \
        "the button the switch replaced is still there"
    assert "isOn && !url.isEmpty" in DAEMON_CODE, \
        "a saved address is used whether or not the switch is on"
    assert "sendsActionOnEndEditing = true" in DAEMON_CODE, \
        "the fields need a second thing pressed to take effect"
    assert "window.initialFirstResponder = urlField" in DAEMON_CODE, \
        "opening the window summons the Passwords popover over the section"


def test_two_hotkey_presses_cannot_read_the_clipboard_aloud():
    """A PRIVACY DEFECT, AND THE PATH TO IT IS THE ORDINARY ONE. Every piece of
    Selection.read's state was function-local, so two reads interleaved. Press
    the hotkey twice with nothing selected: the first Command-C copies nothing
    so it polls the full 0.6 s; the second starts and records the same
    changeCount; the first then restores, and clearContents() bumps the counter;
    the second sees the change, concludes its copy worked, and speaks whatever
    it finds -- the person's own clipboard. A password out of a password
    manager, read out loud.

    Pressing again is the natural reaction to a hotkey that has not made a sound
    yet, so this is the common path, not a contrived one.

    The flag must be released in the RESTORE, not at completion: the restore is
    what moves the changeCount, so a read starting between the two would see
    that move and mistake it for a copy."""
    assert "isReading" in DAEMON_CODE, "overlapping selection reads are possible again"
    # "static func read(" -- readViaAccessibility comes first and is not it.
    body = DAEMON_CODE.split("static func read(")[1].split("private static func postCommandC")[0]
    assert "guard !isReading" in body, "a second read is not turned away"
    restore = DAEMON_CODE.split("private static func restore")[1]
    assert "isReading = false" in restore.split("board.clearContents()")[0], \
        "the flag is cleared somewhere other than before the restore's own clipboard write"


def test_the_daemon_can_stop_a_player_it_did_not_start():
    """HALF THE CONTRACT WAS MISSING AND THE SUITE PASSED ON THE OTHER HALF --
    test_both_ways_of_starting_a_player_can_stop_each_other asserted only that
    the string "player.pid" appeared in the daemon, and it appeared in a write
    that nothing ever read back.

    So: click Speak in OpenClip, then press the hotkey. The pre-emptive stop
    found nothing, a second player started, and the two read over each other --
    with the upper capsule covering the lower one's close button. README.md
    says "one player at a time", and it was true only when both came from the
    same side."""
    # THE CALL SITE, NOT THE DEFINITION. Asserting the name appears anywhere
    # passed with the call deleted, because the function that is never called
    # still contains its own name -- the same shape as the player.pid write
    # that nothing read.
    # Scoped to Speaker: ServerSupervisor has a stop() too, and it comes first.
    speaker = DAEMON_CODE.split("class Speaker")[1]
    stop = speaker.split("func stop() {")[1].split("private func stopForeignPlayer")[0]
    assert "stopForeignPlayer()" in stop, "stop() no longer reaches a player it did not start"
    assert "killpg(pid, SIGTERM)" in DAEMON_CODE, \
        "it signals a process rather than a group, so a player that forked is left speaking"
    assert "proc_pidpath" in DAEMON_CODE, \
        "a stale pid file whose number has been reused would have somebody else's program killed"
    # ONE WRITER, and it is the one that can be signalled: killpg reaches a
    # group leader, and setsid() is what makes the player one.
    assert "player.pid" in PLAYER and "setsid()" in PLAYER, "the player does not name itself"
    assert re.search(r'try\? String\(getpid\)?\(\)', PLAYER) or "String(getpid())" in PLAYER, \
        "the player writes somebody else's pid"
    assert "player.pid" not in code(OPENCLIP).split("def main")[1], \
        "the OpenClip action writes the pid file too, so there are two writers again"


def test_a_client_mistake_is_not_reported_as_a_server_error():
    """Every malformed request came back 500 with a bare text/plain body: {}
    gave "'input'", a truncated body gave "Expecting value", an unknown voice
    gave a KeyError. A caller cannot tell "I sent that wrong" from "the server
    is broken", and neither can anybody reading the bug report."""
    assert 'self.refuse(400, "invalid_request"' in SERVER, "client mistakes are still 500s"
    assert "json.JSONDecodeError" in SERVER and "KeyError" in SERVER, \
        "the 400 does not cover the ways a body is actually wrong"
    assert 'self.reply(500, str(error).encode(), "text/plain")' not in SERVER, \
        "the bare text/plain 500 is still there"


def test_a_web_page_cannot_spend_the_owners_machine():
    """Loopback is not a boundary a browser respects: any site the owner visits
    can POST JSON to 127.0.0.1:47815, and with a Calliope server configured that
    is their GPU, their gateway key and their OpenAI credit.

    Browsers attach Origin to every cross-site POST; the real clients -- the
    player, curl, a script -- attach none. So the header's presence is the whole
    test, and no CORS header is sent anywhere, so nothing could read a reply."""
    assert 'self.headers.get("Origin")' in SERVER, "a web page can still POST here"
    assert '"browser_not_allowed"' in SERVER
    assert "Access-Control-Allow-Origin" not in SERVER, \
        "a CORS header would hand a reply back to the page that asked"


def test_a_passage_that_lost_its_server_does_not_end_as_if_it_finished():
    """A chunk that failed to synthesise was blacklisted and then skipped in
    silence, so walking off the end of a half-failed passage looked exactly like
    finishing one: the capsule vanished mid-article and nothing said why.

    Three ordinary things kill the server under a live passage -- changing a
    setting in Settings (the daemon restarts it), opening Calliope.app while an
    OpenClip player is speaking (the port is reclaimed), and the idle timeout
    after a long pause. So this is the common case, and the retry is aimed at
    it: the server is usually back a second later."""
    finish = PLAYER.split("private func finish()")[1].split("\n    }")[0]
    assert "failed.isEmpty" in finish, "it can still end cleanly with parts missing"
    assert "fail(" in finish, "it ends quietly rather than saying what happened"
    assert "retried" in PLAYER, "a chunk is given up on the first refusal"
    # And the message has to be legible without hovering: `detail` is a tooltip.
    fail = PLAYER.split("private func fail(")[1].split("\n    }")[0]
    assert "status: message" in fail, "the only visible text is still the word \"error\""
