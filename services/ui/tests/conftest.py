"""A signing gateway, the gateway's internal listener and MeTube, all without a socket.

NO SERVER IS STARTED BY ANY TEST HERE, and none may be: the app is exercised
as the real ASGI app through fastapi.testclient, and the two things it talks to
are replaced at the httpx transport rather than at a socket. So the identity
check, the ownership checks, the outbound headers, the streaming and the error
mapping are the real ones -- only the wire is fake.

WHO IS ASKING comes from voice_common.conformance's FakeGateway, which writes
identity.pub and service.key where this service reads them and signs
assertions with the matching key, exactly as the real gateway does. Every
request a test sends carries one; the default is ALICE, a user-jobs user, so a
test that needs more than a user-jobs user says so.

The app is reloaded per test because its configuration is read at import,
exactly as it is in the container, where the process is the unit of
configuration.
"""

from __future__ import annotations

import contextlib
import importlib
import ipaddress
import json
import socket
from collections.abc import Callable, Iterable

import httpx
from urllib.parse import unquote
import pytest
from fastapi.testclient import TestClient
from voice_common.identity import ASSERTION_HEADER, DELEGATION_HEADER
from voice_common.scopes import session_scopes

# The `calliope_gateway` fixture: a FakeGateway whose credential directory
# this process reads, as the real gateway writes one into each service's
# volume.
pytest_plugins = ("voice_common.conformance",)

METUBE = "http://metube.test"
INTERNAL_HOST = "voice-gateway"

ALICE = "u_aaaaaaaaaaaaaaaa"
BOB = "u_bbbbbbbbbbbbbbbb"
USER_JOBS = session_scopes("user-jobs")
USER = session_scopes("user")
ADMIN = session_scopes("admin")


class FakeInternalListener:
    """The gateway's :8081, as /ui/fetch meets it: a service key and a delegation token.

    It refuses a request that does not carry this service's own key, as the
    real listener does, so a test can see the key was sent -- and a test can
    hand it a different key to stand for a rotation the service has not read.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.seen: list[httpx.Request] = []
        self.reply: tuple[int, dict[str, str], bytes] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {
                "message": "Incorrect API key provided.",
                "type": "invalid_request_error", "param": None,
                "code": "invalid_api_key"}})
        if DELEGATION_HEADER.lower() not in request.headers:
            return httpx.Response(403, json={"error": {
                "message": "no delegation", "type": "invalid_request_error",
                "param": None, "code": "insufficient_scope"}})
        if self.reply is not None:
            status, headers, body = self.reply
            return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(200, json={"text": "transcribed",
                                         "path": request.url.path})

    def transcriptions(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.url.path == "/v1/audio/transcriptions"]


class FakeMeTube:
    """MeTube's three lists and the four routes this service uses.

    Modelled on the real thing's observed behaviour rather than its intent, so
    the tests can assert the two traps: an auto_start:false item lands in
    `pending` and not `queue`, and /delete answers {"status":"ok"} whether or
    not it deleted anything.
    """

    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self.queue: dict[str, dict] = {}
        self.done: dict[str, dict] = {}
        self.refuse: str | None = None
        # What finish() last wrote, so the static route can serve exactly it.
        self.filename: str = ""
        self.folder: str = ""
        # What the static route hands back, so a test can serve a real WebVTT
        # body rather than the audio placeholder.
        self.content: bytes = b"RIFFfake-audio-bytes"
        # What aiohttp guesses off the suffix, and what a <video> element
        # refuses to play when it is application/octet-stream instead.
        self.content_type: str = "audio/ogg"
        # The two If-Range is compared against. Present because /ui/media
        # relays the request header, and relaying it while dropping these makes
        # every conditional range unconditional.
        self.etag: str = '"fake-etag"'
        self.last_modified: str = "Wed, 03 Sep 2026 10:00:00 GMT"
        # WHICH OF MeTube'S TWO STATIC ROUTES HOLDS THE FILE. Both resolve to
        # one directory on this deployment -- AUDIO_DOWNLOAD_DIR defaults to
        # "%%DOWNLOAD_DIR" and is unset -- so the default is "both", which is
        # the deployed shape. "video" stands in for a deployment that does set
        # them apart, where a captions download is written beside the video and
        # /audio_download/ 404s for it.
        self.served_from: str = "both"
        # An OUTAGE rather than a refusal: MeTube unreachable, which must stay
        # a 502 now that a refusal is a 400. See
        # test_an_unreachable_metube_is_still_a_502.
        self.down: bool = False
        self.calls: list[tuple[str, dict]] = []
        # Every request MeTube received, headers and all, so a test can assert
        # what this service told it about the caller: nothing.
        self.requests: list[httpx.Request] = []
        # A finished file whose body is still being produced, for the tests
        # that tell a relay from a buffer. Served as it is, never read first.
        self.paced: httpx.AsyncByteStream | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if request.method == "GET":
            if path == "/history":
                return httpx.Response(200, json={
                    "pending": list(self.pending.values()),
                    "queue": list(self.queue.values()),
                    "done": list(self.done.values())})
            for route, offered in (("audio_download", ("both", "audio")),
                                   ("download", ("both", "video"))):
                if not path.startswith(f"/{route}/"):
                    continue
                # SERVES ONE EXACT PATH, and 404s on anything else, because the
                # real one does. This used to answer 200 to any path under
                # /audio_download/, which meant the suite could not tell a
                # correct URL from one missing its folder segment -- and that
                # is exactly the bug that shipped: every real download 404'd
                # while 82 tests passed. A fixture more permissive than the
                # thing it stands in for tests nothing.
                want = f"/{route}/" + "/".join(
                    part for part in (self.folder, self.filename) if part)
                if unquote(path) == want and self.served_from in offered:
                    return self.static(request)
                return httpx.Response(404, text=f"not here; the file is at {want}")
            return httpx.Response(404)

        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if path == "/add":
            if self.refuse:
                return httpx.Response(200, json={"status": "error",
                                                 "msg": self.refuse})
            url = body["url"]
            record = {"id": "short-id", "url": url, "title": "A Title",
                      "status": "pending", "size": None, "percent": None,
                      "speed": None, "eta": None, "live_status": "not_live",
                      "filename": None}
            # auto_start defaults TRUE when the field is None, which is why the
            # client under test always sends it explicitly.
            if body.get("auto_start", True):
                self.queue[url] = dict(record, status="downloading", percent=10)
            else:
                self.pending[url] = record
            return httpx.Response(200, json={"status": "ok"})
        if path == "/start":
            for url in body["ids"]:
                record = self.pending.pop(url, None)
                if record:
                    self.queue[url] = dict(record, status="downloading")
            return httpx.Response(200, json={"status": "ok"})
        if path == "/delete":
            where = body["where"]
            target = self.queue if where == "queue" else self.done
            for url in body["ids"]:
                # Both queues are consulted for a "queue" delete because
                # MeTube's cancel() handles pending explicitly.
                target.pop(url, None)
                if where == "queue":
                    self.pending.pop(url, None)
            # ALWAYS ok, even when nothing matched. This is the real
            # behaviour and it is why abandon() verifies afterwards.
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    def static(self, request: httpx.Request) -> httpx.Response:
        """One finished file, served the way aiohttp's static route serves it.

        RANGES ARE NOT OPTIONAL IN THIS FIXTURE, because they are the whole
        subject of /ui/media. Verified against the live MeTube on this NAS
        before it was written down here: `Range: bytes=0-1023` answered
        `206 Partial Content` with `Content-Range: bytes 0-1023/533915`,
        `Accept-Ranges: bytes` and `Content-Type: video/mp4`. A stub that
        answered 200 to everything would let a relay that drops Range pass,
        and a player served that way ignores every scrub.
        """
        body = self.content
        common = {"accept-ranges": "bytes", "content-type": self.content_type,
                  "etag": self.etag, "last-modified": self.last_modified}

        wanted = request.headers.get("range")
        # A stale If-Range means "send me the whole thing instead", and getting
        # that backwards splices two different files together with no error
        # anywhere. aiohttp compares against the ETag it would have sent.
        condition = request.headers.get("if-range")
        if condition is not None and condition != self.etag:
            wanted = None
        if not wanted or not wanted.startswith("bytes="):
            return httpx.Response(200, content=body,
                                  headers={**common,
                                           "content-length": str(len(body))})

        first, _, last = wanted[len("bytes="):].partition("-")
        start = int(first) if first else 0
        stop = int(last) + 1 if last else len(body)
        stop = min(stop, len(body))
        if start >= len(body) or start >= stop:
            # What a range past the end really answers, and it carries the
            # total so the client can correct itself.
            return httpx.Response(416, headers={
                **common, "content-range": f"bytes */{len(body)}"})
        chunk = body[start:stop]
        return httpx.Response(206, content=chunk, headers={
            **common,
            "content-length": str(len(chunk)),
            "content-range": f"bytes {start}-{stop - 1}/{len(body)}"})

    def finish(self, url: str, filename: str = "A Title.opus",
               folder: str = "stt-ingest") -> None:
        """Finish a download.

        `folder` defaults to the value compose.yaml actually deploys rather
        than to "", because the default should be the deployed shape: MeTube
        writes into that subdirectory and records it, and a fixture that
        defaulted to no folder is why the missing path segment went unnoticed.
        """
        self.queue.pop(url, None)
        self.pending.pop(url, None)
        self.filename = filename
        self.folder = folder
        self.done[url] = {"id": "short-id", "url": url, "title": "A Title",
                          "status": "finished", "filename": filename,
                          "folder": folder, "size": 1234, "percent": 100}


class Bytes(httpx.AsyncByteStream):
    """One chunk, as a stream that has NOT been consumed yet.

    httpx.Response(json=...) arrives with its content already loaded, and
    aiter_raw() on such a response raises StreamConsumed. The service under
    test streams what it relays, so a mock that hands back a pre-read body
    tests the wrong thing and fails for the wrong reason.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data


class Router(httpx.AsyncBaseTransport):
    """Every outbound request this service makes, to whichever fake it was addressed to.

    Anything else is refused as unreachable, so an outbound request to an
    address nobody configured fails the test that made it.
    """

    def __init__(self, internal: FakeInternalListener, tube: FakeMeTube) -> None:
        self.internal, self.tube = internal, tube

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Read the body here, once, so the fakes below can be plain synchronous
        # functions and can still assert on it. Uploads through this service
        # are STREAMED -- a 131 MB ingest must not be buffered -- so
        # `request.content` raises RequestNotRead until it is.
        if request.method in {"POST", "PUT", "PATCH"}:
            await request.aread()
        host = request.url.host
        if (host == "metube.test" and self.tube.paced is not None
                and request.url.path.startswith("/audio_download/")):
            self.tube.requests.append(request)
            return httpx.Response(200, stream=self.tube.paced, headers={
                "content-type": self.tube.content_type, "accept-ranges": "bytes"})
        if host == INTERNAL_HOST and request.url.port == 8081:
            answer = self.internal.handle(request)
        elif host == "metube.test":
            answer = self.tube.handle(request)
        else:
            raise httpx.ConnectError(f"nothing is listening on {host}")
        return httpx.Response(answer.status_code, headers=answer.headers,
                              stream=Bytes(answer.content))


def fake_getaddrinfo(host, port, **kwargs):
    """DNS, without DNS.

    The guard resolves before it decides, which is the point of it -- so a test
    suite that let it use the real resolver would depend on the network, and
    `media.example` does not resolve anywhere. A literal address answers as
    itself, exactly as getaddrinfo does, so the private-range rules are still
    exercised by app/guard.py's own tests; anything else answers with a public
    address so the ingestion tests get past the guard and on to what they are
    about.
    """
    try:
        ipaddress.ip_address(host)
        address = host
    except ValueError:
        address = "93.184.216.34"
    family = (socket.AF_INET6 if ":" in address else socket.AF_INET)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


@pytest.fixture
def sign(calliope_gateway) -> Callable[..., dict[str, str]]:
    """The headers the gateway forwards for one person: their assertion and a delegation.

    The delegation token rides on every request because the gateway attaches
    one only to /ui/fetch and this service reads it only there; carrying it
    everywhere proves the second half.
    """
    def headers(sub: str = ALICE, scopes: Iterable[str] = USER_JOBS,
                **assertion: object) -> dict[str, str]:
        return {ASSERTION_HEADER: calliope_gateway.assertion(
                    "ui", sub=sub, scopes=scopes, **assertion),
                DELEGATION_HEADER: calliope_gateway.delegation(sub=sub)}
    return headers


@pytest.fixture
def build(monkeypatch, tmp_path, calliope_gateway):
    """Reload the app with an environment, and hand back its pieces."""
    def make(**environment):
        environment.setdefault("UI_METUBE_URL", METUBE)
        environment.setdefault("UI_VOICE_DIR", str(tmp_path / "voices"))
        environment.setdefault("UI_PROBE", "0")
        for name, value in environment.items():
            monkeypatch.setenv(name, value)

        from app import clips, config, guard, ingest, main, metube, owners, probe
        for module in (config, guard, probe, metube, clips, owners, ingest, main):
            importlib.reload(module)

        monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)

        internal, tube = FakeInternalListener(calliope_gateway.service_key), FakeMeTube()
        transport = Router(internal, tube)
        # The one thing replaced: the factory, not httpx.AsyncClient itself.
        # Patching the class means the replacement's own call to it recurses,
        # which is a stack overflow inside the lifespan and a confusing one.
        monkeypatch.setattr(main, "new_client",
                            lambda: httpx.AsyncClient(transport=transport))
        return main, internal, tube
    return make


@pytest.fixture
def client(build, sign):
    """A TestClient with the LIFESPAN RUN, signed as ALICE unless a test says otherwise.

    TestClient only runs startup and shutdown when it is used as a context
    manager, and this app builds its one httpx client in the lifespan. Without
    this, every test sees `'State' object has no attribute 'client'` -- which is
    also exactly what a production process would do if the lifespan were
    skipped, so it is worth failing loudly rather than lazily constructing one.
    A request that passes its own identity headers replaces the default ones.
    """
    stack = contextlib.ExitStack()

    def make(**environment):
        main, internal, tube = build(**environment)
        api = stack.enter_context(TestClient(main.app, headers=sign()))
        return api, internal, tube

    yield make
    stack.close()
