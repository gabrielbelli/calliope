"""server.py's remote: an address and the key that goes with it, or none at all.

THE GATEWAY ANSWERS NOTHING WITHOUT A KEY, so these run server.py itself, not
its source text, against a stand-in gateway on 127.0.0.1 that refuses a
request without the key as the real one does. Nothing here loads the model:
only the forwarding path runs, and it never needs Kokoro.
"""
import importlib.util
import json
import pathlib
import re
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SERVER_PY = pathlib.Path(__file__).resolve().parent.parent / "server" / "server.py"
# Any string will do for a stand-in that only compares it; it is not a key.
KEY = "stand-in-key"
# A key the stand-in accepts but that may not list models, as a key made
# without models:read would be.
NARROW_KEY = "stand-in-key-without-models-read"


class Gateway(BaseHTTPRequestHandler):
    """The routes server.py asks the Calliope server for, behind the key."""

    seen = []

    def log_message(self, format, *args):
        pass

    def answer(self, status, body, content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def keyed(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length) if length else b""
        Gateway.seen.append({"method": self.command, "path": self.path,
                             "key": self.headers.get("Authorization"),
                             "type": self.headers.get("Content-Type"), "body": self.body})
        if self.path == "/health":
            self.answer(200, b"ok", "text/plain")       # liveness is the one thing without a key
            return False
        if self.headers.get("Authorization") not in ("Bearer " + KEY, "Bearer " + NARROW_KEY):
            self.answer(401, b'{"error": {"code": "unauthenticated"}}')
            return False
        return True

    def do_GET(self):
        if not self.keyed():
            return
        if self.path == "/v1/models":
            if self.headers.get("Authorization") == "Bearer " + NARROW_KEY:
                return self.answer(403, b'{"error": {"code": "forbidden"}}')
            return self.answer(200, json.dumps({"data": [{"id": "chatterbox"}, {"id": "kokoro"},
                                                         {"id": "chatterbox-long"}]}).encode())
        if self.path == "/voices":
            return self.answer(200, json.dumps({"voices": ["bm_george", "pf_dora"]}).encode())
        return self.jobs()

    def do_DELETE(self):
        if self.keyed():
            self.jobs()

    def jobs(self):
        self.answer(200, json.dumps({"method": self.command, "path": self.path}).encode())

    def do_POST(self):
        if not self.keyed():
            return
        if self.path == "/v1/audio/speech":
            model = json.loads(self.body)["model"]
            if model == "chatterbox-long":
                return self.answer(202, b'{"id": "j1"}', headers={
                    "Location": "/jobs/j1", "Retry-After": "5"})
            return self.answer(200, b"remote audio", "audio/pcm",
                               {"X-Word-Timings": "[[0,0.5]]", "X-Not-Passed": "no"})
        if self.path in ("/v1/audio/transcriptions", "/v1/audio/translations"):
            return self.answer(200, json.dumps({"text": "heard"}).encode())
        return self.jobs()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return server


@pytest.fixture
def gateway():
    Gateway.seen = []
    server = _serve(Gateway)
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxies(monkeypatch, tmp_path):
    """server.py loaded with an environment, serving on a free port."""
    models = tmp_path / "models"
    models.mkdir()
    with zipfile.ZipFile(models / "voices-v1.0.bin", "w") as voices:
        for name in ("af_heart", "pf_dora"):
            voices.writestr(name + ".npy", b"")
    started = []

    def start(**environment):
        monkeypatch.delenv("CALLIOPE_URL", raising=False)
        monkeypatch.delenv("CALLIOPE_KEY", raising=False)
        monkeypatch.delenv("CALLIOPE_LOCAL", raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        spec = importlib.util.spec_from_file_location("calliope_server_under_test", SERVER_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.MODELS = str(models)
        # Nothing here may speak on this Mac; a request that tried would fail loudly.
        module.ENGINE.command = ["/nonexistent/no-engine-in-these-tests"]
        server = _serve(module.Handler)
        started.append(server)
        return module, "http://127.0.0.1:%d" % server.server_address[1]

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _ask(url, body=None, method=None, headers=None):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _ask_with_headers(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.headers, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_every_request_to_the_calliope_server_carries_the_key(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    status, listing = _ask(proxy + "/v1/models")
    assert status == 200
    rows = json.loads(listing)["data"]
    assert {"id": "calliope/chatterbox", "object": "model", "owned_by": "calliope-remote"} in rows
    # The server's own Kokoro is a model of its own, not hidden behind this Mac's.
    assert {"id": "calliope/kokoro", "object": "model", "owned_by": "calliope-remote"} in rows
    assert {"id": "kokoro", "object": "model", "owned_by": "calliope-local"} in rows

    status, voices = _ask(proxy + "/voices")
    voices = json.loads(voices)
    assert voices["voices"] == ["af_heart", "bm_george", "pf_dora"]
    assert [(row["ref"], row["language"], row["model"]) for row in voices["detail"]] == [
        ("mac/af_heart", "en", "kokoro"), ("mac/pf_dora", "pt", "kokoro"),
        ("calliope/bm_george", "en", "calliope/kokoro"),
        ("calliope/pf_dora", "pt", "calliope/kokoro")]

    # Prefixed, and the unprefixed name clients used before the prefix existed.
    for model in ("calliope/chatterbox", "chatterbox"):
        status, headers, audio = _ask_with_headers(proxy + "/v1/audio/speech",
                                                   {"model": model, "input": "hello"})
        assert (status, audio) == (200, b"remote audio")
        assert headers["X-Word-Timings"] == "[[0,0.5]]"
        assert headers["Content-Type"] == "audio/pcm"
        assert "X-Not-Passed" not in headers
    sent = [json.loads(seen["body"]) for seen in Gateway.seen if seen["path"] == "/v1/audio/speech"]
    assert sent == [{"model": "chatterbox", "input": "hello"}] * 2, \
        "the server was asked for a name it does not have"

    assert {seen["path"] for seen in Gateway.seen} == {"/v1/models", "/voices", "/v1/audio/speech"}
    assert {seen["key"] for seen in Gateway.seen} == {"Bearer " + KEY}


def test_an_address_without_a_key_is_no_remote(proxies, gateway, capsys):
    """Said once at startup, and a remote model is refused here naming what is
    missing, without a request the gateway would only refuse."""
    module, proxy = proxies(CALLIOPE_URL=gateway)
    assert module.CALLIOPE_URL == ""
    assert "requires a key" in capsys.readouterr().out
    status, listing = _ask(proxy + "/v1/models")
    assert status == 200
    assert {row["owned_by"] for row in json.loads(listing)["data"]} == {"calliope-local"}
    status, refusal = _ask(proxy + "/v1/audio/speech", {"model": "chatterbox", "input": "hello"})
    assert status == 404
    assert json.loads(refusal)["error"]["code"] == "no_such_model"
    assert "an address with its key" in json.loads(refusal)["error"]["message"]
    for path, body in (("/v1/audio/speech", {"model": "calliope/kokoro", "input": "hello"}),
                       ("/v1/audio/transcriptions", b"--boundary--"),
                       ("/jobs", None)):
        status, refusal = _ask(proxy + path, body)
        assert status == 404, path
        assert json.loads(refusal)["error"]["code"] == "no_calliope_server"
        assert "an address with its key" in json.loads(refusal)["error"]["message"]
    status, report = _ask(proxy + "/calliope/test")
    assert status == 200
    report = json.loads(report)
    assert (report["configured"], report["ok"]) == (False, False) and report["message"]
    assert Gateway.seen == []


def test_an_unknown_model_is_refused_with_both_lists_and_never_sent(proxies, gateway):
    """The gateway answers a name it does not know with a default voice."""
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    for model in ("calliope/nonesuch", "nonesuch"):
        status, refusal = _ask(proxy + "/v1/audio/speech", {"model": model, "input": "hello"})
        assert status == 404
        error = json.loads(refusal)["error"]
        assert error["code"] == "no_such_model"
        assert "kokoro" in error["message"] and "calliope/chatterbox" in error["message"]
    assert [seen["path"] for seen in Gateway.seen if seen["method"] == "POST"] == []
    # Refreshed before each refusal, in case the name was added inside the cache's minute.
    assert [seen["path"] for seen in Gateway.seen].count("/v1/models") == 3


def test_a_transcription_is_carried_byte_for_byte(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    body = (b'--b0und\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\nRIFF\x00\x01\xff\r\n--b0und--\r\n")
    for path in ("/v1/audio/transcriptions", "/v1/audio/translations"):
        status, answer = _ask(proxy + path, body,
                              headers={"Content-Type": "multipart/form-data; boundary=b0und"})
        assert (status, json.loads(answer)) == (200, {"text": "heard"})
        seen = Gateway.seen[-1]
        assert (seen["path"], seen["body"], seen["type"], seen["key"]) == (
            path, body, "multipart/form-data; boundary=b0und", "Bearer " + KEY)


def test_a_long_voice_answers_with_a_job_and_the_job_routes_pass_through(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    status, headers, body = _ask_with_headers(
        proxy + "/v1/audio/speech", {"model": "calliope/chatterbox-long", "input": "a long text"})
    assert status == 202
    assert (headers["Location"], headers["Retry-After"]) == ("/jobs/j1", "5")

    for method, path in (("GET", "/jobs"), ("POST", "/jobs"), ("GET", "/jobs/j1?wait=0"),
                         ("DELETE", "/jobs/j1"), ("GET", "/jobs/j1/audio"),
                         ("DELETE", "/jobs/j1/audio")):
        body = b'{"model": "chatterbox-long", "input": "x"}' if method == "POST" else None
        status, answer = _ask(proxy + path, body, method=method,
                              headers={"Content-Type": "application/json"} if body else None)
        assert (status, json.loads(answer)) == (200, {"method": method, "path": path})
        assert Gateway.seen[-1]["body"] == (body or b"")
    assert {seen["key"] for seen in Gateway.seen} == {"Bearer " + KEY}
    # Anything that only looks like a job route is not passed on.
    assert _ask(proxy + "/jobs/j1/elsewhere")[0] == 404


def test_the_calliope_test_asks_every_time_and_says_what_it_found(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    for _ in range(2):
        status, report = _ask(proxy + "/calliope/test")
        assert status == 200
        report = json.loads(report)
        assert (report["configured"], report["ok"], report["url"]) == (True, True, gateway)
        assert [check["name"] for check in report["checks"]] == ["reach", "key", "voices", "speech"]
        assert all(check["ok"] and isinstance(check["ms"], int) for check in report["checks"])
        assert report["checks"][2]["detail"] == "2 voices"
        assert re.fullmatch(r"\d+\.\d s, \d+ KB", report["checks"][3]["detail"])
        assert "\n" not in report["message"] and gateway in report["message"]
    # Uncached: the second report asked again, and only liveness went without the key.
    assert [seen["path"] for seen in Gateway.seen] == [
        "/health", "/v1/models", "/voices", "/v1/audio/speech"] * 2
    assert [seen["key"] for seen in Gateway.seen if seen["path"] == "/health"] == [None, None]
    sample = json.loads(Gateway.seen[3]["body"])
    assert sample == {"model": "kokoro", "input": "Calliope.", "voice": "af_heart",
                      "response_format": "pcm"}


@pytest.mark.parametrize("key, failed, detail", [
    ("not-the-key", "key", "key not accepted"),
    (NARROW_KEY, "key", "key lacks models:read"),
])
def test_the_calliope_test_stops_at_the_first_failure(proxies, gateway, key, failed, detail):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=key)
    report = json.loads(_ask(proxy + "/calliope/test")[1])
    assert report["ok"] is False
    assert [(check["name"], check["ok"]) for check in report["checks"]] == [
        ("reach", True), (failed, False)]
    assert report["checks"][-1]["detail"] == detail
    assert detail in report["message"]


def test_the_calliope_test_names_a_server_that_is_not_there(proxies):
    with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as closed:
        address = "http://127.0.0.1:%d" % closed.server_address[1]
    module, proxy = proxies(CALLIOPE_URL=address, CALLIOPE_KEY=KEY)
    report = json.loads(_ask(proxy + "/calliope/test")[1])
    assert report["ok"] is False
    assert [check["name"] for check in report["checks"]] == ["reach"]
    assert report["message"].startswith(address + " did not answer")
