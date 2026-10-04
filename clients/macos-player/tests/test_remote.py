"""server.py's remote: an address and the key that goes with it, or none at all.

THE GATEWAY ANSWERS NOTHING WITHOUT A KEY, so these run server.py itself, not
its source text, against a stand-in gateway on 127.0.0.1 that refuses a
request without the key as the real one does. Nothing here loads the model:
only the forwarding path runs, and it never needs Kokoro.
"""
import http.client
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

    def answer(self, status, body, content_type="application/json", headers=None, length=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length:
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
            return self.answer(200, json.dumps({"data": [
                {"id": "chatterbox", "owned_by": "tts-long"},
                {"id": "kokoro", "owned_by": "tts-stack"},
                {"id": "chatterbox-long"}]}).encode())
        if self.path == "/voices":
            return self.answer(200, json.dumps({"voices": ["bm_george", "pf_dora"]}).encode())
        if self.path.startswith("/ui/media"):
            # A file, with its length, as Starlette's FileResponse sends one.
            return self.answer(200, MEDIA, "audio/webm")
        if self.path == "/jobs/j2/audio":
            # A job's audio, streamed by tts-long with no length: the end is the close.
            return self.answer(200, MEDIA[:300000], "audio/mpeg", length=False,
                               headers={"Content-Disposition": 'attachment; filename="j2.mp3"'})
        if self.path == "/glossaries/moved":
            return self.answer(302, b"", headers={"Location": ELSEWHERE[0] + "/collect"})
        return self.jobs()

    def do_DELETE(self):
        if self.keyed():
            self.jobs()

    def do_PUT(self):
        if self.keyed():
            self.jobs()

    def jobs(self):
        self.answer(200, json.dumps({"method": self.command, "path": self.path,
                                     "bytes": len(self.body)}).encode())

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
            return self.answer(200, json.dumps({"text": "heard"}).encode(), headers={
                "X-Glossary-Repaired": "Calliope, S%C3%A3o Paulo", "X-Stt-Engine": "parakeet"})
        return self.jobs()


# Several megabytes, so a proxy that held answers whole would at least have to try.
MEDIA = bytes(range(256)) * (3 * 4096)
# Where a redirect points, set by the test that needs it.
ELSEWHERE = [""]


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
    # served_by is the server's own owned_by, which says what a model is for; a row the server
    # gave none keeps none.
    assert {"id": "calliope/chatterbox", "object": "model", "owned_by": "calliope-remote",
            "served_by": "tts-long"} in rows
    # The server's own Kokoro is a model of its own, not hidden behind this Mac's.
    assert {"id": "calliope/kokoro", "object": "model", "owned_by": "calliope-remote",
            "served_by": "tts-stack"} in rows
    assert {"id": "calliope/chatterbox-long", "object": "model",
            "owned_by": "calliope-remote"} in rows
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
        assert (status, json.loads(answer)) == (200, {"method": method, "path": path,
                                                      "bytes": len(body or b"")})
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


# ---------------------------------------------------------------- what is forwarded, and not --
#
# FORWARDED in server.py is an allowlist by method and path. Every route the `calliope` command
# uses is here, passed on with the key; everything else -- session-only, admin, the page --
# stops at the proxy and never reaches the server.

FORWARDED = [
    # (method, path with its query, body, content type)
    ("GET", "/glossaries", None, None),
    ("GET", "/glossaries/Tech", None, None),
    ("PUT", "/glossaries/mine.v2?force=true", b"Calliope\ncloud code = Claude Code\n",
     "text/plain; charset=utf-8"),
    ("DELETE", "/glossaries/mine_old", None, None),
    ("POST", "/ui/resolve", b'{"url": "https://example.com/watch?v=1"}', "application/json"),
    ("POST", "/ui/commit", b'{"token": "https://example.com/watch?v=1", "clip_start": 90.0}',
     "application/json"),
    ("GET", "/ui/progress?token=https%3A%2F%2Fexample.com%2Fwatch%3Fv%3D1", None, None),
    ("POST", "/ui/fetch?response_format=srt&glossary=tech", b'{"token": "t"}', "application/json"),
    ("POST", "/ui/captions", b'{"token": "t"}', "application/json"),
    ("POST", "/ui/abandon", b'{"token": "t"}', "application/json"),
    ("GET", "/ui/clips", None, None),
    ("POST", "/ui/clips", b"--b\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\nme\r\n--b--\r\n",
     "multipart/form-data; boundary=b"),
    ("DELETE", "/ui/clips/my-voice_2", None, None),
    ("GET", "/jobs?limit=20&kind=clone", None, None),
    ("GET", "/jobs/0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab", None, None),
    ("DELETE", "/jobs/0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab/audio", None, None),
]


@pytest.mark.parametrize("method, path, body, content_type", FORWARDED)
def test_every_route_the_command_uses_reaches_the_server_with_the_key(
        proxies, gateway, method, path, body, content_type):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    status, answer = _ask(proxy + path, body, method=method,
                          headers={"Content-Type": content_type} if content_type else None)

    assert (status, json.loads(answer)) == (200, {"method": method, "path": path,
                                                  "bytes": len(body or b"")})
    seen = Gateway.seen[-1]
    assert (seen["method"], seen["path"], seen["key"], seen["body"]) == (
        method, path, "Bearer " + KEY, body or b"")
    assert seen["type"] == content_type


REFUSED = [
    # The page, and its flags: session-only at the gateway, and nothing a command needs.
    ("GET", "/ui"), ("GET", "/ui/transcribe"), ("GET", "/ui/config"),
    # Keys, sessions and administration: session-only or admin, never a key's.
    ("GET", "/auth/me"), ("GET", "/auth/keys"), ("POST", "/auth/keys"),
    ("DELETE", "/auth/keys/k_abcdefghijkl"), ("GET", "/admin/users"), ("GET", "/admin/audit"),
    ("PUT", "/admin/secrets/X"), ("GET", "/satellites"), ("POST", "/satellites/n1/say"),
    # Routes a key could call but nothing here uses.
    ("POST", "/v1/chat/completions"), ("POST", "/transcribe"), ("POST", "/speak"),
    # The right path with the wrong method, or the wrong shape of name.
    ("PUT", "/jobs/j1"), ("POST", "/jobs/j1"), ("PUT", "/jobs/j1/audio"), ("POST", "/ui/media"),
    ("GET", "/ui/fetch"), ("GET", "/ui/resolve"), ("DELETE", "/glossaries"),
    ("POST", "/glossaries/mine"), ("GET", "/glossaries/.hidden"),
    ("GET", "/glossaries/a%2F..%2Fb"), ("GET", "/glossaries/" + "a" * 65),
    ("GET", "/glossaries/mine/extra"), ("GET", "/ui/clips/me"), ("DELETE", "/ui/clips/a.b"),
    ("DELETE", "/ui/clips/" + "a" * 65), ("GET", "/jobs/j1/../../admin/users"),
    ("GET", "/jobs/j1.json"), ("GET", "/jobs/j1/elsewhere"),
]


@pytest.mark.parametrize("method, path", REFUSED)
def test_nothing_outside_the_allowlist_reaches_the_server(proxies, gateway, method, path):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    status, refusal = _ask(proxy + path, b"{}" if method in ("POST", "PUT") else None,
                           method=method)

    assert status == 404
    assert json.loads(refusal)["error"]["code"] == "not_found"
    assert Gateway.seen == []


def test_the_path_sent_is_the_path_checked(proxies, gateway):
    """`GET //elsewhere/jobs` is never pasted after the server's address as it arrived.

    Newer Pythons collapse the leading slashes before the handler sees the path, which makes it
    /elsewhere/jobs and refused; older ones leave it, and urlsplit reads it as /jobs. Either is
    safe, so long as what goes out is the path that was checked."""
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    connection = http.client.HTTPConnection(proxy[len("http://"):], timeout=10)
    try:
        connection.request("GET", "//elsewhere/jobs?limit=1")
        answer = connection.getresponse()
        status = answer.status
        answer.read()
    finally:
        connection.close()
    sent = [seen["path"] for seen in Gateway.seen]
    assert (status, sent) in ((404, []), (200, ["/jobs?limit=1"]))


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/glossaries", None), ("PUT", "/glossaries/mine", b"Calliope\n"),
    ("POST", "/ui/resolve", b'{"url": "https://example.com/a"}'),
    ("GET", "/ui/media?token=t", None), ("GET", "/ui/clips", None),
    ("DELETE", "/ui/clips/me", None), ("POST", "/v1/audio/translations", b"--b--"),
])
def test_without_a_server_the_new_routes_say_so(proxies, gateway, method, path, body):
    module, proxy = proxies(CALLIOPE_URL=gateway)
    status, refusal = _ask(proxy + path, body, method=method)
    assert status == 404
    assert json.loads(refusal)["error"]["code"] == "no_calliope_server"
    assert Gateway.seen == []


def test_a_file_streams_through_with_its_length_or_to_the_close(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    with urllib.request.urlopen(proxy + "/ui/media?token=t", timeout=10) as answer:
        assert answer.headers["Content-Length"] == str(len(MEDIA))
        assert answer.headers["Content-Type"] == "audio/webm"
        assert answer.read() == MEDIA
    # tts-long sends a job's audio with no length; through the proxy it ends at the close.
    with urllib.request.urlopen(proxy + "/jobs/j2/audio", timeout=10) as answer:
        assert answer.headers["Content-Length"] is None
        assert answer.headers["Content-Disposition"] == 'attachment; filename="j2.mp3"'
        assert answer.read() == MEDIA[:300000]


def test_an_upload_streams_through_byte_for_byte(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    status, answer = _ask(proxy + "/v1/audio/transcriptions", MEDIA,
                          headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert status == 200
    assert Gateway.seen[-1]["body"] == MEDIA


def test_a_chunked_upload_is_refused_rather_than_sent_empty(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    connection = http.client.HTTPConnection(proxy[len("http://"):], timeout=10)
    try:
        connection.request("PUT", "/glossaries/mine", body=iter([b"Calliope\n"]),
                           encode_chunked=True)
        answer = connection.getresponse()
        status, refusal = answer.status, answer.read()
    finally:
        connection.close()
    assert status == 411 and json.loads(refusal)["error"]["code"] == "length_required"
    assert Gateway.seen == []


def test_the_transcription_headers_come_back(proxies, gateway):
    module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
    request = urllib.request.Request(proxy + "/v1/audio/transcriptions", data=b"--b--",
                                     headers={"Content-Type": "multipart/form-data; boundary=b"})
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.headers["X-Glossary-Repaired"] == "Calliope, S%C3%A3o Paulo"
        assert answer.headers["X-Stt-Engine"] == "parakeet"


def test_a_redirect_is_answered_and_never_followed_with_the_key(proxies, gateway):
    """urllib follows a redirect with the Authorization header on it, to any host."""
    collector = _serve(Gateway)
    ELSEWHERE[0] = "http://127.0.0.1:%d" % collector.server_address[1]
    try:
        module, proxy = proxies(CALLIOPE_URL=gateway, CALLIOPE_KEY=KEY)
        connection = http.client.HTTPConnection(proxy[len("http://"):], timeout=10)
        try:
            connection.request("GET", "/glossaries/moved")
            answer = connection.getresponse()
            assert answer.status == 302
            assert answer.headers["Location"] == ELSEWHERE[0] + "/collect"
            answer.read()
        finally:
            connection.close()
    finally:
        collector.shutdown()
        collector.server_close()
    assert [seen["path"] for seen in Gateway.seen] == ["/glossaries/moved"], \
        "the redirect was followed"


def test_only_the_routes_that_transcribe_wait_long(proxies):
    module, _ = proxies()
    for method, path in (("POST", "/ui/fetch"), ("POST", "/v1/audio/transcriptions"),
                         ("POST", "/v1/audio/translations")):
        assert module.forwarded(method, path) >= 900, path
    for method, path in (("GET", "/glossaries"), ("POST", "/ui/resolve"), ("GET", "/ui/progress"),
                         ("GET", "/jobs/j1"), ("DELETE", "/ui/clips/me")):
        assert module.forwarded(method, path) <= 120, path
    assert module.forwarded("GET", "/ui/config") is None
