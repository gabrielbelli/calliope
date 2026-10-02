"""A signing gateway and the gateway's internal listener without a socket, and a downloader without a network.

NO SERVER IS STARTED BY ANY TEST HERE, and none may be: the app is exercised
as the real ASGI app through fastapi.testclient, and the one thing it talks to
is replaced at the httpx transport rather than at a socket. So the identity
check, the ownership checks, the outbound headers, the streaming and the error
mapping are the real ones -- only the wire is fake.

THE DOWNLOADER IS A REAL CHILD PROCESS, and a fake one: UI_FETCHER points at
tests/fake_fetcher.py, which speaks app/fetcher.py's protocol, chooses what to
do by a word in the link and never opens a socket. So the spawning, the line
reading, the time limits, the one-file rule and the cache all run for real.
Every fetch it runs is appended to fetches.log in the cache directory, which
`fetches.calls()` reads back.

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
import hashlib
import importlib
import ipaddress
import json
import socket
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from voice_common.identity import ASSERTION_HEADER, DELEGATION_HEADER
from voice_common.scopes import session_scopes

# The `calliope_gateway` fixture: a FakeGateway whose credential directory
# this process reads, as the real gateway writes one into each service's
# volume.
pytest_plugins = ("voice_common.conformance",)

INTERNAL_HOST = "voice-gateway"
FAKE_FETCHER = Path(__file__).with_name("fake_fetcher.py")

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


class Fetches:
    """What the stand-in downloader was asked to fetch, read from its own log."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def calls(self) -> list[dict]:
        log = self.dir / "fetches.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line]


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

    def __init__(self, internal: FakeInternalListener) -> None:
        self.internal = internal

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Read the body here, once, so the fakes below can be plain synchronous
        # functions and can still assert on it. Uploads through this service
        # are STREAMED -- a 131 MB ingest must not be buffered -- so
        # `request.content` raises RequestNotRead until it is.
        if request.method in {"POST", "PUT", "PATCH"}:
            await request.aread()
        host = request.url.host
        if host == INTERNAL_HOST and request.url.port == 8081:
            answer = self.internal.handle(request)
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
        environment.setdefault("UI_VOICE_DIR", str(tmp_path / "voices"))
        environment.setdefault("UI_FETCHER", str(FAKE_FETCHER))
        environment.setdefault("UI_CACHE_DIR", str(tmp_path / "cache"))
        environment.setdefault("UI_PROBE_TIMEOUT", "2")
        for name, value in environment.items():
            monkeypatch.setenv(name, value)

        from app import clips, config, downloads, guard, ingest, main
        for module in (config, guard, clips, downloads, ingest, main):
            importlib.reload(module)

        monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)

        internal = FakeInternalListener(calliope_gateway.service_key)
        transport = Router(internal)
        # The one thing replaced: the factory, not httpx.AsyncClient itself.
        # Patching the class means the replacement's own call to it recurses,
        # which is a stack overflow inside the lifespan and a confusing one.
        monkeypatch.setattr(main, "new_client",
                            lambda: httpx.AsyncClient(transport=transport))
        return main, internal, Fetches(Path(environment["UI_CACHE_DIR"]))
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
        main, internal, fetches = build(**environment)
        api = stack.enter_context(TestClient(main.app, headers=sign()))
        return api, internal, fetches

    yield make
    stack.close()


def wait_for(api, token: str, *, headers: dict[str, str] | None = None,
             seconds: float = 10.0) -> dict:
    """/ui/progress until the download has finished or failed, and its last answer."""
    ends = time.monotonic() + seconds
    while True:
        state = api.get("/ui/progress", params={"token": token}, headers=headers).json()
        if state.get("status") in ("finished", "error") or time.monotonic() > ends:
            return state
        time.sleep(0.05)


def fetched(api, url: str, *, headers: dict[str, str] | None = None, **commit) -> dict:
    """Resolve, commit and wait: a finished download of `url`, as the caller."""
    resolved = api.post("/ui/resolve", json={"url": url}, headers=headers)
    assert resolved.status_code == 200, resolved.text
    started = api.post("/ui/commit", json={"token": url, **commit}, headers=headers)
    assert started.status_code == 200, started.text
    return wait_for(api, url, headers=headers)


def finished_job(main, url: str, data: bytes, suffix: str, *, sub: str = ALICE,
                 facts: dict | None = None):
    """A finished download of `sub`'s, as a run would leave it, without running one.

    For the tests that need exact bytes or a suffix the stand-in never writes:
    the file is written under a name sweep() owns, and the job points at it.
    """
    downloads = main.downloads
    downloads.config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    job = downloads.replace(sub, url, facts if facts is not None else {
        "title": "A Title", "uploader": None, "duration": 20.0, "bytes": len(data),
        "has_subtitles": False, "subtitles_lang": None, "video": False})
    path = downloads.config.CACHE_DIR / (
        hashlib.sha256(f"{sub}\0{url}".encode()).hexdigest() + suffix)
    path.write_bytes(data)
    job.kind, job.path, job.state = "audio", path, "finished"
    return job
