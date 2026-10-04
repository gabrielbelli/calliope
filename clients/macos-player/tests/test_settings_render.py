"""The Settings window, drawn: every pane, light and dark, off screen.

THE ONLY WAY TO LOOK AT IT WITHOUT THE DAEMON. The daemon takes the menu bar,
the hotkey and the port, so it cannot be started in a test; the window lives in
daemon/settings.swift precisely so tests/harness/settings/main.swift can build
it with a stand-in host and draw each pane into a bitmap. Nothing is shown on
screen: the window is never ordered in, and the app is .prohibited, so no Dock
icon appears either.

The stand-in proxy answers the routes the window asks for, so the panes are
drawn with voices, models and a passed connection test in them -- the states
somebody will actually see -- rather than empty.
"""
import json
import os
import pathlib
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANES = ("general", "voices", "engines", "api", "integrations")

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit")


class StandInProxy(BaseHTTPRequestHandler):
    MAC = ["af_heart", "am_michael", "bf_emma", "pf_dora", "pm_alex", "ef_dora"]
    REMOTE = ["af_heart", "af_nova", "pf_dora", "pm_santa"]

    def log_message(self, format, *args):
        pass

    def answer(self, body, status=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        language = {"a": "en", "b": "en", "p": "pt", "e": "es"}
        if self.path == "/health":
            return self.answer(b"ok")
        if self.path == "/status":
            return self.answer({"port": 0, "mac": {"on": True, "loaded": False,
                                                   "keep_loaded": False, "idle_seconds": 600},
                                "calliope": {"on": True, "url": "https://calliope.example.com"}})
        if self.path == "/voices":
            detail = [{"name": n, "origin": "mac", "ref": "mac/" + n, "language": language[n[0]],
                       "model": "kokoro"} for n in self.MAC]
            detail += [{"name": n, "origin": "calliope", "ref": "calliope/" + n,
                        "language": language[n[0]], "model": "calliope/kokoro"} for n in self.REMOTE]
            return self.answer({"voices": sorted(set(self.MAC + self.REMOTE)), "detail": detail})
        if self.path == "/v1/models":
            local = [{"id": m, "owned_by": "calliope-local"} for m in ("kokoro", "tts-1", "tts-1-hd")]
            remote = [{"id": "calliope/" + m, "owned_by": "calliope-remote"}
                      for m in ("kokoro", "chatterbox", "whisper-1")]
            return self.answer({"object": "list", "data": local + remote})
        if self.path == "/calliope/test":
            return self.answer({"configured": True, "ok": True, "message": "Connected.",
                                "checks": [{"name": "reach", "ok": True, "ms": 80, "detail": ""},
                                           {"name": "key", "ok": True, "ms": 90, "detail": ""}]})
        return self.answer({"error": {"code": "not_found", "message": self.path}}, 404)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    build = tmp_path_factory.mktemp("render")
    binary = build / "settings-render"
    subprocess.run(
        ["swiftc", "-swift-version", "5",
         *(str(ROOT / name) for name in ("shared/paths.swift", "shared/mark.swift",
                                         "shared/preferences.swift", "daemon/settings.swift",
                                         "tests/harness/settings/main.swift")),
         "-o", str(binary)],
        check=True, capture_output=True, text=True)

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), StandInProxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    # A throwaway suite, never the owner's: the window reads and writes its
    # preferences, and a test has no business in the real ones.
    suite = f"com.gabrielbelli.calliope-player-render-test.{os.getpid()}"
    subprocess.run(["defaults", "write", suite, "proxyPort", "-int",
                    str(proxy.server_address[1])], check=True)
    subprocess.run(["defaults", "write", suite, "voices", "-dict",
                    "en", "mac/af_heart", "pt", "calliope/pf_dora"], check=True)
    subprocess.run(["defaults", "write", suite, "calliopeOn", "-bool", "true"], check=True)
    out = build / "panes"
    out.mkdir()
    try:
        run = subprocess.run([str(binary), str(out)], check=True, capture_output=True, text=True,
                             timeout=120, env={**os.environ, "CALLIOPE_TEST_DEFAULTS": suite})
        (out / "log.txt").write_text(run.stdout)
    finally:
        proxy.shutdown()
        subprocess.run(["defaults", "delete", suite], capture_output=True)
        (pathlib.Path.home() / "Library/Preferences" / f"{suite}.plist").unlink(missing_ok=True)
    return out


def size_of(png: pathlib.Path):
    """Width and height in points, from the PNG header (the bitmap is 2x on Retina)."""
    with png.open("rb") as handle:
        handle.read(16)
        return struct.unpack(">II", handle.read(8))


@pytest.mark.parametrize("pane", PANES)
@pytest.mark.parametrize("appearance", ("light", "dark"))
def test_every_pane_is_drawn(rendered, pane, appearance):
    png = rendered / f"{pane}-{appearance}.png"
    assert png.exists(), f"{png.name} was not drawn"
    width, height = size_of(png)
    scale = width / 760
    assert scale in (1, 2), f"{png.name} is {width} wide; the window is 760 points"
    assert height / scale == 560, f"{png.name} is {height / scale:.0f} points tall, not 560"
    # And it is not blank: a transparent or one-colour image compresses to almost nothing.
    assert png.stat().st_size > 8_000, f"{png.name} looks empty"


def test_nothing_that_arrives_late_moves_the_window(rendered):
    """THE DEFECT THIS EXISTS FOR WAS SEEN IN A SCREENSHOT. Panes used to size
    the window to their content, and content that arrives late -- the voice
    list, a connection test, the model list -- made the window grow under the
    pointer, or not grow and cut the last line off. The window has one size now
    and the forms scroll inside it; the harness reports the size after every
    pane has had its answers, a test included."""
    log = (rendered / "log.txt").read_text()
    assert log.count("kept its size") == 2 * len(PANES), log
    assert "CHANGED SIZE" not in log, log
