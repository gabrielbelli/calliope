"""The internal listener, :8081: service keys only, never published (D6, §3.6).

    hub -> stt and tts        POST /v1/audio/transcriptions, /v1/audio/speech, …
    stt, tts -> tts-long      POST /runs                         runs:write
    hub -> health             GET /health                        health:read
    voice-ui -> stt           POST /v1/audio/transcriptions + X-Calliope-Delegation
    hub, tts-long -> secrets  /internal/secrets/*                (routes_secrets.py)

**Why a second listener rather than letting backends call each other.** A
backend then verifies exactly one kind of credential, the gateway's
assertion, whoever is calling, and a service key is useless on the public
port. The route table and the audit are the same ones :8080 uses: every row
of §3.3-3.5 is here with the same scope, checked against the service
principal's scopes instead of a person's.

**It runs inside the public listener's process** (main.lifespan), on the same
event loop, so a delegation token issued for /ui/fetch on :8080 is counted
and checked here against the same in-memory record (D64). It is plain HTTP on
the compose network; compose never publishes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

from voice_common.errors import install_errors

from . import authn, main, routes_secrets, routetable
from .routetable import Reserved, Rule, rule
from .runtime import Settings

log = logging.getLogger("voice-gateway.internal")

app = FastAPI(title="voice-gateway-internal", openapi_url=None, docs_url=None,
              redoc_url=None)
authn.install_internal(app)
install_errors(app)
app.add_exception_handler(main.StarletteHTTPException, main._http_error)


async def runs(request: Request) -> Response:
    """A finished run record from stt or tts, to tts-long's history (D31).

    Only here, and only with runs:write: the browser must never be able to
    write a row into the history it reads.
    """
    return await main._proxy(request, main.LONG, content=request.stream())


INTERNAL_ONLY: tuple[tuple[str, str, object, Rule], ...] = (
    ("POST", "/runs", runs, rule("runs:write")),
    # The hub picks its STT engine and glossary from stt's part of this body,
    # and reaches stt only through here; the same tiers as :8080 (D50).
    ("GET", "/health", main.health, rule("health:read")),
)

for _method, _path, _endpoint, _rule in (*main.SHARED, *INTERNAL_ONLY):
    app.add_api_route(_path, _endpoint, methods=[_method], include_in_schema=False)

app.include_router(routes_secrets.internal_router)

# The hub holds glossaries:ha to SELECT the home-assistant profile for Assist
# commands; changing the household's vocabulary is a person's act, so on this
# listener, which only services reach, the reserved profile takes :all to write.
SERVICE_GLOSSARY_WRITE = rule("glossaries:write:own", reserved=Reserved(
    "name", "home-assistant", frozenset({"glossaries:write:all"})))

RULES = {**{(method, path): requirement for method, path, _, requirement in main.SHARED},
         **{(method, path): requirement for method, path, _, requirement in INTERNAL_ONLY},
         **{(method, "/glossaries/{name}"): SERVICE_GLOSSARY_WRITE
            for method in ("PUT", "DELETE")},
         # What routes_secrets.internal_router serves (D42, D66).
         ("GET", "/internal/secrets/{name}"): rule("secrets:fetch"),
         ("POST", "/internal/secrets/import"): rule("secrets:import")}
routetable.bind(app, RULES)


class _Server(uvicorn.Server):
    """uvicorn without its signal handlers: the public listener's own handle them."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


async def serve(settings: Settings) -> tuple[_Server, asyncio.Task] | None:
    """Start :8081 on this event loop, or nothing when GATEWAY_INTERNAL_PORT is empty."""
    if settings.internal_port is None:
        return None
    # proxy_headers off: the caller is the peer, never what a header says (D67).
    server = _Server(uvicorn.Config(app, lifespan="off", log_level="info",
                                    access_log=False, proxy_headers=False))
    # A socket of its own, handed over bound: uvicorn then creates the server
    # from it, and proxyproto.EventLoop (which takes over only host-and-port
    # requests) leaves this plain-HTTP listener alone.
    family = socket.AF_INET6 if ":" in settings.internal_bind else socket.AF_INET
    sock = socket.create_server((settings.internal_bind, settings.internal_port),
                                family=family)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    log.info("internal listener on %s:%d (service keys only)", settings.internal_bind,
             settings.internal_port)
    return server, task


async def shutdown(listener: tuple[_Server, asyncio.Task] | None) -> None:
    if listener is None:
        return
    server, task = listener
    server.should_exit = True
    with contextlib.suppress(Exception):
        await task
