"""server.py's remote: an address and the key that goes with it, or none at all.

THE GATEWAY ANSWERS NOTHING WITHOUT A KEY, so these run server.py itself, not
its source text, against a stand-in gateway on 127.0.0.1 that refuses a
request without the key as the real one does. Nothing here loads the model:
only the forwarding path runs, and it never needs Kokoro.
"""
import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SERVER_PY = pathlib.Path(__file__).resolve().parent.parent / "server" / "server.py"
# Any string will do for a stand-in that only compares it; it is not a key.
KEY = "stand-in-key"


class Gateway(BaseHTTPRequestHandler):
    """The three routes server.py asks the Calliope server for, behind the key."""

    seen = []

    def log_message(self, format, *args):
        pass

    def answer(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def keyed(self):
        Gateway.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + KEY:
            self.answer(401, b'{"error": {"code": "unauthenticated"}}')
            return False
        return True

    def do_GET(self):
        if not self.keyed():
            return
        if self.path == "/v1/models":
            return self.answer(200, json.dumps({"data": [{"id": "chatterbox"}]}).encode())
        return self.answer(200, json.dumps({"voices": ["bm_george"]}).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.keyed():
            self.answer(200, b"remote audio", "audio/pcm")


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def gateway():
    Gateway.seen = []
    server = _serve(Gateway)
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def _proxy(monkeypatch, **environment):
    """server.py loaded with this environment, serving on a free port."""
    monkeypatch.delenv("CALLIOPE_URL", raising=False)
    monkeypatch.delenv("CALLIOPE_KEY", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("calliope_server_under_test", SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = _serve(module.Handler)
    return module, server, "http://127.0.0.1:%d" % server.server_address[1]


def _ask(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_every_request_to_the_calliope_server_carries_the_key(monkeypatch, gateway):
    module, server, proxy = _proxy(monkeypatch, CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    try:
        status, listing = _ask(proxy + "/v1/models")
        assert status == 200
        assert {"id": "chatterbox", "object": "model", "owned_by": "calliope-remote"} \
            in json.loads(listing)["data"]
        assert _ask(proxy + "/voices") == (200, b'{"voices": ["bm_george"]}')
        assert _ask(proxy + "/v1/audio/speech",
                    {"model": "chatterbox", "input": "hello"}) == (200, b"remote audio")
    finally:
        server.shutdown()
        server.server_close()
    assert {path for _, path, _ in Gateway.seen} == {"/v1/models", "/voices", "/v1/audio/speech"}
    assert {sent for _, _, sent in Gateway.seen} == {"Bearer " + KEY}


def test_an_address_without_a_key_is_no_remote(monkeypatch, gateway, capsys):
    """Said once at startup, and a remote model is refused here naming what is
    missing, without a request the gateway would only refuse."""
    module, server, proxy = _proxy(monkeypatch, CALLIOPE_URL=gateway)
    try:
        assert module.CALLIOPE_URL == ""
        assert "requires a key" in capsys.readouterr().out
        status, listing = _ask(proxy + "/v1/models")
        assert status == 200
        assert {row["owned_by"] for row in json.loads(listing)["data"]} == {"calliope-local"}
        status, refusal = _ask(proxy + "/v1/audio/speech", {"model": "chatterbox", "input": "hello"})
        assert status == 404
        assert "an address with its key" in json.loads(refusal)["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()
    assert Gateway.seen == []
