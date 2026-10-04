"""Mock backends, wired in through httpx's own transport layer, and a signed-in gateway.

The gateway is exercised as the real ASGI app, through a real httpx client, so
every test below runs the actual proxy code -- header filtering, streaming,
timeout mapping, the identity middleware and all. Only the socket is replaced:
`Router` dispatches by hostname to a mock backend, and a mock backend is a
plain ASGI callable that records what reached it.

That is why httpx was worth the dependency. A hand-rolled fake client would
have tested the gateway's idea of httpx rather than httpx.

**Every test gets its own gateway state** (the autouse fixture below): a
database, a keys volume and service volumes under tmp_path, a public origin of
https://gateway.test, no internal listener, and Argon2 parameters cheap enough
for a unit test. The app is reloaded per test because its configuration is read
at import and at start, exactly as in the container, where the process is the
unit of configuration.

**`gateway()` hands back a client that is already authenticated** with an
API key from the `admin` preset, which holds every scope a key may hold. A
test about routing or streaming then reads as it did before there was a
login, and a test about authentication passes `authenticate=False` and does
its own.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager

import httpx
import pytest
from voice_common import auth as removed
from voice_common import scopes as scope_rules

# The hostnames the reloaded app is pointed at. They resolve nowhere; every
# request for them is answered by Router below.
STT_URL = "http://stt.test"
TTS_URL = "http://tts.test"
LONG_URL = "http://long.test"
SATELLITES_URL = "http://satellites.test"
UI_URL = "http://ui.test"

PUBLIC_ORIGIN = "https://gateway.test"
# Strong enough for D18, so the bootstrap is armed rather than locked.
ADMIN_PASSWORD = "first access only, replaced at sign-in"
PASSWORD = "a long and unremarkable passphrase"


@pytest.fixture(autouse=True)
def gateway_environment(tmp_path, monkeypatch):
    """A private gateway-data, calliope-keys and /svc for each test, and fast hashing."""
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CALLIOPE_KEYS_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("CALLIOPE_SVC_DIR", str(tmp_path / "svc"))
    monkeypatch.setenv("CALLIOPE_PUBLIC_ORIGIN", PUBLIC_ORIGIN)
    monkeypatch.setenv("CALLIOPE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("GATEWAY_INTERNAL_PORT", "")
    for name in (*removed.REMOVED_VARIABLES, "CALLIOPE_TRUSTED_PROXIES",
                 "CALLIOPE_PROXY_PROTOCOL", "CALLIOPE_DEV_INSECURE_COOKIE",
                 "GATEWAY_BIND", "CALLIOPE_MASTER_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    from app import passwords
    monkeypatch.setattr(passwords, "PARAMS",
                        passwords.Params(iterations=1, memory_cost=8, lanes=1))
    return tmp_path


class MockBackend:
    """An ASGI app that records what it was sent and answers what it was told.

    Deliberately raw ASGI rather than a FastAPI app: half the assertions here
    are about bytes and framing -- was the upload forwarded in chunks, did the
    Authorization header survive, was content-length preserved -- and a
    framework in the way would answer those questions for us.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[dict] = []
        self.reply = self.default_reply

    def default_reply(self, record: dict) -> tuple[int, dict[str, str], bytes]:
        return (200, {"content-type": "application/json"},
                f'{{"backend":"{self.name}","path":"{record["path"]}"}}'.encode())

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message.get("body"):
                chunks.append(message["body"])
            if not message.get("more_body", False):
                break

        record = {
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "headers": {k.decode().lower(): v.decode() for k, v in scope["headers"]},
            # The same headers undeduplicated. The dict above cannot show a
            # field name arriving twice, which is exactly what the duplicate-
            # header tests have to assert on.
            "raw_headers": [(k.decode().lower(), v.decode())
                            for k, v in scope["headers"]],
            "body": b"".join(chunks),
            # One ASGI message per chunk the gateway forwarded, which is how
            # the streaming tests tell a pass-through from a buffer.
            "chunks": len(chunks),
        }
        self.seen.append(record)

        status, headers, body = self.reply(record)
        # A list of pairs is accepted as well as a dict, because a dict cannot
        # express the case the proxy has to get right: the same field name
        # twice. Real HTTP allows it (Set-Cookie above all) and a dict silently
        # loses one of them, so a dict-only mock could not have caught the
        # header collapsing that this fixture now tests for.
        pairs = headers.items() if isinstance(headers, dict) else headers
        await send({"type": "http.response.start", "status": status,
                    "headers": [(k.encode(), v.encode()) for k, v in pairs]})
        await send({"type": "http.response.body", "body": body})

    @property
    def last(self) -> dict:
        return self.seen[-1]


class Unreachable(httpx.AsyncBaseTransport):
    """A container that is down, restarting, or has no DNS entry yet."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)


class Slow(httpx.AsyncBaseTransport):
    """A container that accepted the connection and then never answered."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


class Router(httpx.AsyncBaseTransport):
    """Dispatch by hostname to one of the mock backends."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = self.mapping[request.url.host]
        if not isinstance(target, httpx.AsyncBaseTransport):
            target = httpx.ASGITransport(app=target)
        return await target.handle_async_request(request)


def reload_gateway(monkeypatch, *, long_models: str | None = None):
    """Import a fresh copy of the app under the given environment.

    `long_models` is GATEWAY_LONG_MODELS, and it is DELETED rather than left
    alone when unset. The set decides both which names route long and which
    names GET /v1/models advertises, so a value inherited from the shell that
    ran pytest would silently change what half these tests assert.
    """
    monkeypatch.setenv("GATEWAY_STT_URL", STT_URL)
    monkeypatch.setenv("GATEWAY_TTS_URL", TTS_URL)
    monkeypatch.setenv("GATEWAY_TTS_LONG_URL", LONG_URL)
    monkeypatch.setenv("GATEWAY_SATELLITES_URL", SATELLITES_URL)
    monkeypatch.setenv("GATEWAY_UI_URL", UI_URL)
    if long_models is None:
        monkeypatch.delenv("GATEWAY_LONG_MODELS", raising=False)
    else:
        monkeypatch.setenv("GATEWAY_LONG_MODELS", long_models)
    return importlib.reload(importlib.import_module("app.main"))


def make_user(username: str = "ana", *, role: str = "admin",
              password: str | None = PASSWORD, must_change: bool = False):
    """A user written straight into the running gateway's database."""
    from app import runtime, users
    rt = runtime.get()
    return users.create(rt.db, username=username, role=role,
                        password_hash=rt.hasher.hash_sync(password) if password else None,
                        must_change=must_change, created_by="test")


def make_key(user, *, scopes=None, days: int | None = None) -> str:
    """An API key for `user`, by default the admin preset. Returns the plaintext."""
    from app import apikeys, runtime
    chosen = scope_rules.PRESETS["admin"].scopes if scopes is None else frozenset(scopes)
    _, plaintext = apikeys.create(runtime.get().db, user=user, name="test", scopes=chosen,
                                  preset=None, days=days, created_by=user["id"])
    return plaintext


def admin_key() -> str:
    """An admin-preset key for the user `root`, made if it is not there yet."""
    from app import runtime, users
    root = users.by_username(runtime.get().db, "root") or make_user("root")
    return make_key(root)


def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


# What a browser sends with a request the page itself makes.
SAME_ORIGIN = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors",
               "sec-fetch-dest": "empty"}


async def sign_in(client: httpx.AsyncClient, username: str = "ana",
                  password: str = PASSWORD, **headers: str) -> httpx.Response:
    """Log in through POST /auth/login, as the page does; the cookie lands in the jar."""
    return await client.post("/auth/login", json={"username": username,
                                                  "password": password},
                             headers={**SAME_ORIGIN, **headers})


@asynccontextmanager
async def gateway(monkeypatch, *, stt=None, tts=None, long=None, satellites=None, ui=None,
                  long_models: str | None = None, authenticate: bool = True):
    """A client speaking to the real app, which speaks to the mock backends.

    With `authenticate` the client carries an admin-preset key for a user
    called `root`; without it the client carries nothing.
    """
    main = reload_gateway(monkeypatch, long_models=long_models)
    router = Router({"stt.test": stt or MockBackend("stt-stack"),
                     "tts.test": tts or MockBackend("tts-stack"),
                     "long.test": long or MockBackend("tts-long"),
                     "satellites.test": satellites or MockBackend("voice-satellites"),
                     "ui.test": ui or MockBackend("voice-ui")})
    monkeypatch.setattr(main, "new_client",
                        lambda: httpx.AsyncClient(transport=router,
                                                  follow_redirects=False))
    # The lifespan is what opens the client and the database, so it is run
    # rather than skipped: a test against an app whose startup never ran would
    # not be testing this app. ASGITransport does not run it, so it is entered
    # by hand.
    async with main.app.router.lifespan_context(main.app):
        headers = bearer(admin_key()) if authenticate else {}
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app, client=("192.0.2.1", 50000)),
                base_url=PUBLIC_ORIGIN, headers=headers) as client:
            yield client, main


def service_key(name: str) -> str:
    """The key the gateway minted onto a service's volume, as the service reads it."""
    import os
    from pathlib import Path
    return (Path(os.environ["CALLIOPE_SVC_DIR"]) / name / "service.key").read_text().strip()


def internal_client() -> httpx.AsyncClient:
    """A client for the :8081 app, sharing the running gateway's runtime."""
    from app import internal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=internal.app, client=("172.18.0.5", 40000)),
        base_url="http://voice-gateway:8081")


class Clock:
    """The gateway's clock (app.db.now), moved by hand."""

    def __init__(self, monkeypatch) -> None:
        import time
        # From now, so a token signed on this clock is current to a verifier
        # that reads the real one.
        self.at = time.time()
        from app import db
        monkeypatch.setattr(db, "now", lambda: self.at)

    def advance(self, seconds: float) -> None:
        self.at += seconds


@pytest.fixture
def backends():
    """The three mock backends, fresh for each test."""
    return (MockBackend("stt-stack"), MockBackend("tts-stack"),
            MockBackend("tts-long"))
