"""The player talks to 127.0.0.1 and to nothing else, and it holds no credential.

A configurable server came back once already. These fail the moment the player can be pointed
somewhere else: a URL read from UserDefaults, an Authorization header, any host that is not
loopback. Reading the source is the only way to check it without launching the player, which
opens a window and starts speaking.
"""
import pathlib
import re

PLAYER = pathlib.Path(__file__).resolve().parent.parent / "player"
SHARED = pathlib.Path(__file__).resolve().parent.parent / "shared" / "preferences.swift"
# The player's own files and the preferences it shares with the daemon, which
# is where the address is built now that the port is a setting.
SOURCES = sorted(PLAYER.glob("*.swift")) + [SHARED]


def test_the_only_server_is_loopback():
    """The port is a setting; the host is not. Any other address in these
    files -- a URL read back from a setting, a host typed into the source -- is
    how the player would come to talk to something that is not this Mac."""
    urls = set()
    for source in SOURCES:
        urls.update(re.findall(r'"(https?://[^"]+)"', source.read_text()))

    assert urls == {"http://127.0.0.1:\\(Preference.port)"}


def test_no_setting_can_redirect_the_player():
    """The saved settings are the speed step and the reader state. A serverURL or an apiKey
    read from the same suite is how the remote path existed before."""
    keys = set()
    for source in SOURCES:
        text = source.read_text()
        keys.update(re.findall(r'forKey: "([^"]+)"', text))
        keys.update(re.findall(r'static let \w+Key = "([^"]+)"', text))

    # None of these is an address. "voices" maps a language to a voice and the
    # side that speaks it -- this Mac or the Calliope server, both reached
    # through the one loopback proxy; "proxyPort" is that proxy's port, not a
    # host. The rule this test holds is about redirection, so the list grows
    # when something is added that cannot redirect, and the second assertion is
    # what enforces it.
    assert keys == {"speed", "karaoke", "voice", "voices", "macOn", "keepLoaded",
                    "proxyPort", "calliopeOn"}
    assert not [k for k in keys if any(word in k.lower()
                                       for word in ("url", "host", "server", "key", "token"))], \
        "a setting is shaped like an address or a credential"


def test_the_player_sends_no_credential():
    for source in SOURCES:
        text = source.read_text()
        assert "Authorization" not in text
        assert "Bearer" not in text


def test_the_local_server_is_started_unconditionally():
    """There is no remote host left to blame, so an unhealthy server means start ours. The
    old guard returned an error instead whenever the URL was not the local one."""
    start = (PLAYER / "main.swift").read_text()
    health = start.index("checkHealth(timeout: 0.5)")
    launch = start.index("launchLocalServer()", health)

    assert "guard" not in start[health:launch]
