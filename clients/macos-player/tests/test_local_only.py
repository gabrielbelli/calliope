"""The player talks to 127.0.0.1 and to nothing else, and it holds no credential.

A configurable server came back once already. These fail the moment the player can be pointed
somewhere else: a URL read from UserDefaults, an Authorization header, any host that is not
loopback. Reading the source is the only way to check it without launching the player, which
opens a window and starts speaking.
"""
import pathlib
import re

PLAYER = pathlib.Path(__file__).resolve().parent.parent / "player"
SOURCES = sorted(PLAYER.glob("*.swift"))


def test_the_only_server_is_loopback():
    urls = set()
    for source in SOURCES:
        urls.update(re.findall(r'"(https?://[^"]+)"', source.read_text()))

    assert urls == {"http://127.0.0.1:47815"}


def test_no_setting_can_redirect_the_player():
    """The saved settings are the speed step and the reader state. A serverURL or an apiKey
    read from the same suite is how the remote path existed before."""
    keys = set()
    for source in SOURCES:
        keys.update(re.findall(r'forKey: "([^"]+)"', source.read_text()))

    assert keys == {"speed", "karaoke"}


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
