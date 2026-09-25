"""One door in front of three speech services.

    POST /v1/audio/transcriptions  ──────────────────────►  stt-stack:8000
    POST /v1/audio/translations    ──────────────────────►  stt-stack:8000
    POST /transcribe               ──────────────────────►  stt-stack:8000

    POST /v1/audio/speech   model=kokoro|tts-1|…|absent  ►  tts-stack:8001
    POST /speak                    ──────────────────────►  tts-stack:8001
    GET  /voices                   ──────────────────────►  tts-stack:8001

    POST /v1/audio/speech   model in GATEWAY_LONG_MODELS ►  tts-long:8002
    POST /jobs  GET /jobs  GET /jobs/{id}[/audio]        ►  tts-long:8002
    DELETE /jobs/{id}  DELETE /jobs/{id}/audio           ►  tts-long:8002

    POST /v1/audio/speech   a long-form name not enabled ►  404 model_not_found

    POST /v1/chat/completions  input_audio part  ────────►  stt-stack:8000
    POST /v1/chat/completions  no audio  ───────────────►  answered here, and
                                                           NEVER by a model

    GET  /v1/models        answered here, from its own table, no backend call
    GET  /v1/models/{id}   the same table, one row, so the two cannot disagree
    GET  /health           all three, fanned out, unauthenticated
    everything else        404 in the OpenAI envelope

This is a router, not a framework. It exists for two reasons and no others.

ONE AUTH BOUNDARY. The three backends used to carry a copy each of an auth
module, and those three copies diverged and duplicated bugs between them; they
share voice_common.auth now, which closed that gap but not this one — three
processes each deciding for themselves is still three places a key can be
misconfigured. Here the backends run open, only :8080 is published, and one
file checks a token. That file is this service's own auth.py rather than the
shared module: a different env var, and no health route of its own to exempt.

ONE HEALTH ANSWER. Today, knowing whether the stack is up means polling three
ports. GET /health here fans out and returns all three, unauthenticated.

THE ROUTING KEY IS THE `model` STRING AND NOTHING ELSE. Not input length: that
is a proxy for a quality decision, and escalating a 400-character paragraph on
length turns a ~17 s call into a ~10 min job with nothing in the request that
asked for it. (The other half of this argument has expired and is recorded
rather than relied on: tts-long carried no ffmpeg, so an mp3 request to it was
a hard 400 while mp3 is exactly what response_format defaults to. It carries
ffmpeg now and answers all six formats, so the two backends no longer differ
there — the timing asymmetry is what still rules length out.) Not a header
either: Open WebUI and every other OpenAI-shaped client has a `model` field in
its settings and no custom-header field, and a routing key nobody can set is
not a routing key.

AN UNKNOWN MODEL GOES FAST, A KNOWN-BUT-DISABLED ONE DOES NOT. The two wrong
answers used to be asymmetric in one direction only — sending a long-form
request to Kokoro costs some quality, sending an ordinary one to Chatterbox
turns 17 seconds into a job the caller did not ask for — so an unrecognised
name defaulted to the recoverable mistake, and still does.

That rule breaks the moment there is more than one long-form engine. A caller
who types a name this gateway KNOWS is a tts-long engine has said which engine
they want; if this deployment has not enabled it, falling through hands them
Kokoro — a different engine, a different voice, no error anywhere. "Some
quality" was an honest description when the fallback was one long-form model
being downgraded to the fast one. It is not a description of getting audio from
a model you named against and cannot hear the difference from until you listen.
So: enabled here goes long, an unrecognised string goes fast, and a name the
shared catalogue says TTS-LONG OWNS that this deployment has not enabled is a
404 that says which variable enables it. The owner is read off the catalogue
row rather than assumed, so a row for a checkpoint the FAST backend owns keeps
going fast instead of being refused by a service that never held it. See
LONG_MODELS and LONG_KNOWN below.

NATIVE ROUTES MOUNT FLAT AND NOTHING IS REWRITTEN. The proxy below forwards
`request.url.path` verbatim, which is why /jobs works: tts-long's own 202
answers with `Location: /jobs/{id}` and `audio_url: /jobs/{id}/audio`, both
backend-relative. Mounted at the same path here they stay correct with no
header rewriting at all. A prefixed design (/tts-long/jobs/…) would need a
rewriting rule for a Location header and a JSON body field, and that rule rots
the first time the backend adds a field.

WHAT THE GATEWAY NEVER DOES: retry (both TTS routes are non-idempotent and
openai-python already retries 5xx twice), cache, rate-limit, load-balance,
trip a circuit breaker (it cannot tell tts-long's minutes-long cold start from
an outage, and would lock the service out of its own recovery path), pre-flight
a health check before each request, or rewrite a body beyond reading `model`.
The budget that decides all of those: a 2-second dictation clip is 190-240 ms
of recognition at the measured 8.5-10.4x, and 20 ms of gateway is 10% of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, NamedTuple

import httpx
from websockets.asyncio.client import connect as ws_connect
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from voice_common.engines import CATALOGUE
from voice_common.errors import (ApiError, error_response, http_error_response,
                                 install_errors, v1_path)

from . import auth, chat
from .openai_api import model_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-gateway")

# httpx logs a line of its own for every request it sends, which in a proxy is
# precisely one duplicate per request — carrying neither the model string, the
# chosen backend, nor the duration. Ours is the line with the information in
# it, and doubling the access log to say the same thing twice is not free on a
# box that also holds 6.5 GB of Chatterbox.
logging.getLogger("httpx").setLevel(logging.WARNING)


class Backend(NamedTuple):
    """A backend, its clock, and what to tell the caller when the clock wins."""

    name: str            # what the log line and the error message call it
    url: str             # no trailing slash; paths are appended verbatim
    read_timeout: float
    timeout_help: str    # the way out, quoted in the 504 body


# Connect is 2 s everywhere: a container on the same host either accepts
# immediately or is not there. Read timeouts are per route and derived from the
# two rates measured on orko, not from one global number — a single 300 s would
# break every long recording, and a single 900 s would hide a wedged TTS.
CONNECT_TIMEOUT = float(os.getenv("GATEWAY_CONNECT_TIMEOUT", "2"))
HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "5"))

# The whole routing criterion. An unrecognised name — and no `model` field at
# all — goes fast. Compared lowercased and stripped: a client that sends
# "Chatterbox" means chatterbox, and the alternative is a nine-minute
# difference decided by a capital letter.
#
# CONFIGURED RATHER THAN LITERAL, because the engine is now the model string.
# Which long-form names exist is a property of the deployment: a box that has
# not installed a second engine must not advertise it, and a box that has must
# not need a code change to say so. The default is the two names this service
# has always routed, so an unset variable is today's behaviour byte for byte.
LONG_MODELS = frozenset(
    m.strip().lower()
    for m in os.getenv("GATEWAY_LONG_MODELS", "chatterbox,tts-long").split(",")
    if m.strip())

# The way out quoted in the fast backend's 504, built rather than written. An
# empty GATEWAY_LONG_MODELS is a legal deployment — a box with no card and no
# local engine — and on that box "send one of ()" is worse than saying nothing,
# so the clause is dropped instead of rendered blank.
_LONG_WAY_OUT = (
    " Or send one of the long-form models (" + ", ".join(sorted(LONG_MODELS))
    + ") and collect the audio from /jobs/{id}/audio." if LONG_MODELS else "")

STT = Backend(
    name="stt-stack",
    url=os.getenv("GATEWAY_STT_URL", "http://stt-stack:8000").rstrip("/"),
    # 900 s: at the measured 8.5-10.4x realtime, two hours of audio is ~847 s
    # of compute. openai-python's own 600 s default gives up first past about
    # 85 minutes of audio, which is the client's call to make and not ours.
    read_timeout=float(os.getenv("GATEWAY_STT_TIMEOUT", "900")),
    timeout_help="It runs at 8.5-10.4x realtime on this host, so roughly two "
                 "hours of audio is the practical ceiling for one request. "
                 "Split the recording.",
)
TTS = Backend(
    name="tts-stack",
    url=os.getenv("GATEWAY_TTS_URL", "http://tts-stack:8001").rstrip("/"),
    # 300 s: at the orko-measured 1.2x realtime that is ~360 s of speech,
    # about 900 words. This is a guard-rail, not a router — long input on the
    # fast path stays on the fast path and is bounded by this timeout rather
    # than by an invented length cap the backend does not have.
    read_timeout=float(os.getenv("GATEWAY_TTS_TIMEOUT", "300")),
    # The way out names LONG_MODELS rather than the string "chatterbox". There
    # is more than one long-form name now and a deployment chooses which of
    # them exist, so a hard-coded one is advice that can be wrong on the box
    # reading it — pointing a caller at a model that 404s here.
    timeout_help="It runs at 1.2-1.5x realtime on this host, so ~900 words is "
                 "the practical ceiling for a synchronous request. Split the "
                 "input." + _LONG_WAY_OUT,
)
LONG = Backend(
    name="tts-long",
    url=os.getenv("GATEWAY_TTS_LONG_URL", "http://tts-long:8002").rstrip("/"),
    # 240 s, chosen ONLY to sit above tts-long's own SYNC_TIMEOUT of 180 s so
    # the backend's honest 202 always wins the race. A gateway timing out at
    # 120 s would return 504 for a job that is still running and will produce
    # audio, and would throw away the job id — a lie plus a leak.
    read_timeout=float(os.getenv("GATEWAY_TTS_LONG_TIMEOUT", "240")),
    timeout_help="Its own synchronous window is TTS_OPENAI_SYNC_TIMEOUT "
                 "(180 s by default), so a timeout here means it is not "
                 "answering at all rather than merely slow. Any job it did "
                 "accept is still queued: see GET /jobs.",
)

# EVERY NAME tts-long COULD OWN, enabled here or not. LONG_MODELS' documented
# rule — "everything else goes fast, including an unrecognised name" — is right
# today and becomes a trap the day a second engine ships: someone types
# chatterbox-turbo at a deployment that has not enabled it, falls through, and
# gets KOKORO. A different engine, a different voice, no error.
#
# The catalogue is the fact table, not this deployment's choices, which is
# exactly what makes it the right source: knowing the name exists is what lets
# this service tell "you meant an engine I have not been given" apart from "you
# sent a string nobody has ever heard of". `tts-long` is added because it is
# the service alias rather than a checkpoint, so it is not in the catalogue and
# never will be.
#
# READ OFF `owned_by` AND NOT OFF THE WHOLE CATALOGUE, BECAUSE THE OTHER
# SPELLING SCHEDULES AN OUTAGE ON THE STACK'S DEFAULT VOICE. `CATALOGUE_IDS |
# {"tts-long"}` says "every checkpoint anybody ever writes a row for is a
# tts-long engine". That is true of every row written so far and false of the
# next one: the fast path's own `kokoro` is a catalogue row waiting to be
# written, and on the day it lands this branch refuses `model="kokoro"` — the
# one string an unconfigured OpenAI client sends — with a 404 telling the
# caller to add Kokoro to GATEWAY_LONG_MODELS, which would then route it to a
# backend that has never held it. EngineFacts.owned_by is documented in the
# catalogue as "the gateway's routing key: which service in this stack owns the
# name", so reading it is not a new convention, it is using the one already
# there. A row this gateway's LONG backend does not own falls through to the
# fast path, which is where it belongs and where it was going before anybody
# wrote it a row.
#
# This is why it sits BELOW `LONG` rather than beside LONG_MODELS: the owner is
# compared against the backend's own name, so there is no second literal
# "tts-long" to keep in step with the first. The alias below is a literal
# because it is a MODEL STRING that means "whatever this deployment defaults
# to", not a backend name that happens to match.
LONG_KNOWN = frozenset(
    engine for engine, facts in CATALOGUE.items()
    if facts.owned_by == LONG.name) | {"tts-long"}

# `GET /v1/models`, built once from the set that actually routes rather than
# written out beside it. One read of GATEWAY_LONG_MODELS feeds both the branch
# in `speech` and the advertised list, which is what stops the two drifting.
MODEL_LIST = model_list(LONG_MODELS)

# `GET /v1/models/{id}`, DERIVED FROM THE PUBLISHED LIST RATHER THAN BUILT
# BESIDE IT. Retrieve-model and list-models are two endpoints over one table in
# OpenAI's API, and the failure worth ruling out is the one this estate keeps
# producing: two tables written by different hands that agree until the day a
# row moves. Indexing the list that was already built makes a row that lists
# but does not retrieve unrepresentable.
#
# KEYED LOWERCASED AND STRIPPED, BECAUSE THAT IS THE KEY `speech` ROUTES ON.
# `model` is compared `.strip().lower()` twenty lines below, so a client that
# sends "Chatterbox" gets audio from the long backend; a retrieve that answered
# 404 for the same string would be this service disagreeing with itself about
# what a model name is, and the whole reason /v1/models exists is to be the one
# place a client learns the names. The row handed back carries the canonical
# spelling, so a client that stores what it retrieved stores the id the list
# published.
MODEL_ROWS = {str(row["id"]).lower(): row for row in MODEL_LIST["data"]}

# Hop-by-hop headers, per RFC 9110 §7.6.1. They describe a single connection
# and must not be copied onto the next one; forwarding `transfer-encoding`
# in particular would describe a framing the next hop is not using.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})

# `host` because httpx must set the backend's own. `authorization` because the
# client's key is stripped and NOT replaced: forwarding it to three services
# that run with their keys unset achieves nothing except copying the secret
# into three more log streams.
DROP_FROM_REQUEST = HOP_BY_HOP | {"host", "authorization"}

state: dict[str, object] = {}


def new_client() -> httpx.AsyncClient:
    """The one connection pool, kept open for the process lifetime.

    A module-level function rather than an inline constructor so the tests can
    hand the app a transport that speaks to mock backends instead of a socket.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT),
        # Redirects are the client's business. A 307 from a backend means
        # something about that backend's routing, and following it here would
        # hide it and re-send the body.
        follow_redirects=False,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["client"] = new_client()
    log.info("ready: stt=%s tts=%s tts-long=%s", STT.url, TTS.url, LONG.url)
    yield
    await state["client"].aclose()  # type: ignore[attr-defined]
    state.clear()


app = FastAPI(
    title="voice-gateway",
    description="One port, one key, three speech services.",
    lifespan=lifespan,
    # No /docs, /redoc or /openapi.json, and none proxied either. stt-stack
    # deliberately put its own behind a key because FastAPI's default
    # registrations were handing out a free map of the service; publishing an
    # unauthenticated map of all three here would quietly undo that decision.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
auth.install(app)

# The shared /v1 handlers, which this service had none of. It was the only one
# of the four with no `param` on any error and no envelope at all on an
# unhandled 500 — and it is the one openai-python actually talks to, which is
# the entire reason it exists. install_errors also registers a validation
# handler that nothing here reaches today: no route below takes a pydantic
# body. That is the point. The next /v1 route someone adds gets the envelope
# without having to know this paragraph exists.
install_errors(app)

# What a 404 says when the path is not in the table. The gateway is the one
# service whose 404 has something useful to add: it routes a FIXED set of
# paths, and it can name where that set is published.
UNKNOWN_URL_HINT = ("This gateway routes a fixed set of paths; GET /v1/models "
                    "lists the models it accepts.")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Anything not in the route table, in OpenAI's envelope.

    There is no catch-all pass-through on purpose: a wildcard route would
    proxy /docs and /openapi.json to a backend that put them behind a key.

    Registered after install_errors, which replaces the shared handler for
    this one exception class. It has to be, because this service answers its
    NATIVE routes in the envelope too — an older decision of its own, see
    openai_api.py — and the shared handler deliberately hands the native side
    back to FastAPI, which is right for the three backends and wrong here.

    Under /v1 it defers to the shared renderer, so a 404 and a 405 from this
    gateway read exactly as they do from the three services behind it. The
    native branch is this service's existing wording, kept byte for byte:
    those routes have clients — bench/bench.py, the integration suite, Open
    WebUI — and `method_not_supported` is a string one of them may already
    branch on. Its only change is the `param` key the schema requires, which
    comes from sharing error_response.
    """
    if v1_path(request.url.path):
        return http_error_response(request, exc,
                                   unknown_url_hint=UNKNOWN_URL_HINT)

    if exc.status_code == 404:
        return error_response(
            404,
            f"Invalid URL ({request.method} {request.url.path}). "
            f"{UNKNOWN_URL_HINT}",
            code="unknown_url")
    if exc.status_code == 405:
        return error_response(
            405, f"Not allowed: {request.method} {request.url.path}.",
            code="method_not_supported")
    return error_response(exc.status_code, str(exc.detail),
                          code="invalid_request_error")


# ------------------------------------------------------------------ proxy --


# Both directions carry a LIST of pairs, never a dict. HTTP allows a field
# name to repeat, and collapsing the repeats corrupts the message in two
# different ways depending on which side you are on:
#
#   dict(httpx_headers.items())  -> {"set-cookie": "a=1, b=2"}   two cookies
#                                   comma-joined into one invalid one, because
#                                   httpx's Mapping view joins duplicates
#   dict(starlette_headers)      -> the LAST duplicate silently wins, because
#                                   Starlette's view yields every pair and the
#                                   comprehension overwrites
#
# Measured, not assumed: httpx.Headers([("set-cookie","a=1"),("set-cookie",
# "b=2")]).items() gives 'a=1, b=2'. Neither backend sends a duplicate header
# today — no Set-Cookie, no repeated Vary or WWW-Authenticate anywhere in the
# three — so this was harmless in practice, and it is fixed rather than
# documented because a proxy that mangles a header the day a backend starts
# sending one is a bug that surfaces as someone else's broken login.
#
# multi_items() and Starlette's MutableHeaders are the duplicate-preserving
# views on each side; Response.init_headers calls .items(), which on
# MutableHeaders returns every pair rather than a deduplicated mapping.


def _request_headers(request: Request) -> list[tuple[str, str]]:
    # content-length is kept deliberately. httpx only adds
    # `transfer-encoding: chunked` for an iterator body when content-length is
    # absent, so keeping it lets a streamed upload keep its known length.
    #
    # httpx replaces its own defaults (user-agent, accept-encoding) with what
    # is passed rather than appending to them, so forwarding a list does not
    # produce a doubled header.
    return [(k, v) for k, v in request.headers.items()
            if k.lower() not in DROP_FROM_REQUEST]


def _response_headers(upstream: httpx.Response) -> MutableHeaders:
    # content-length and content-encoding survive because the body is
    # forwarded raw and undecoded — see aiter_raw below — so both stay true.
    #
    # MutableHeaders rather than a plain list so the Retry-After branch below
    # can still ask `in` and assign by name.
    return MutableHeaders(raw=[
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in upstream.headers.multi_items()
        if k.lower() not in HOP_BY_HOP])


def _log(*, request: Request, backend: str, model: str | None, status: object,
         started: float, rtf: str | None = None) -> None:
    """The entire observability budget: one line per request.

    Route, backend, the model string as the client sent it, upstream status,
    gateway-observed duration, and the backend's own realtime factor where it
    arrives in a header. Prometheus, OpenTelemetry and a sidecar are all
    rejected for three containers and one user; grep answers every question
    asked so far.
    """
    log.info("route=%s %s backend=%s model=%s status=%s duration=%.3f rtf=%s",
             request.url.path, request.method, backend, model or "-", status,
             time.monotonic() - started, rtf or "-")


async def _body(upstream: httpx.Response, *, request: Request, backend: Backend,
                model: str | None, started: float) -> AsyncIterator[bytes]:
    """Forward the upstream body as it arrives, and log once it is done.

    aiter_raw, not aiter_bytes: the bytes go out exactly as they came in,
    which is what keeps content-encoding and content-length honest.
    """
    rtf = upstream.headers.get("x-realtime-factor")
    status: object = upstream.status_code
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    except httpx.TimeoutException:
        # The headers are already sent, so there is no status left to change.
        # The client sees a truncated body; the log says why.
        status = f"{upstream.status_code}+read-timeout"
    except httpx.RequestError as exc:
        status = f"{upstream.status_code}+{type(exc).__name__}"
    except (GeneratorExit, asyncio.CancelledError):
        # The client went away mid-response. Closing the upstream connection
        # is all that is owed: a tts-long job is NOT cancelled, because it
        # runs to completion, the audio lands on disk, and the id was handed
        # over in the 202. Throwing away half-finished 6.5 GB of work to save
        # disk would be the worse trade.
        status = f"{upstream.status_code}+client-disconnect"
        raise
    finally:
        await upstream.aclose()
        _log(request=request, backend=backend.name, model=model, status=status,
             started=started, rtf=rtf)


# The two answers a failed hop gets, written once. `_proxy` streams and the
# chat route below buffers, so they cannot share a code path -- but a caller
# who sees "stt-stack is not reachable" from one and something else worded
# differently from the other is being told two things about one container.
# 503, not 502, on an unreachable backend: 502 claims the upstream answered
# badly and it did not answer at all. openai-python retries 5xx twice by
# default, which for a container mid-restart is exactly right and costs nothing
# -- a refused connection fails in microseconds. The service is named because
# with four backends behind one URL "upstream failed" is unactionable.


def _unreachable(backend: Backend) -> Response:
    return error_response(
        503,
        f"{backend.name} is not reachable from the gateway; the container "
        "may be restarting.",
        type_="server_error", code="backend_unavailable",
        headers={"Retry-After": "30"})


def _failed(backend: Backend, exc: Exception) -> Response:
    return error_response(
        503, f"{backend.name} could not be reached: {type(exc).__name__}.",
        type_="server_error", code="backend_unavailable",
        headers={"Retry-After": "30"})


def _too_slow(backend: Backend) -> Response:
    return error_response(
        504,
        f"{backend.name} did not finish within {backend.read_timeout:.0f} s. "
        f"{backend.timeout_help}",
        type_="server_error", code="backend_timeout")


async def _proxy(request: Request, backend: Backend, *,
                 content: bytes | AsyncIterator[bytes] | None,
                 model: str | None = None) -> Response:
    """Forward this request to `backend` and stream the answer back.

    The path is `request.url.path` verbatim — no prefixing and no rewriting
    anywhere, which is the property that keeps tts-long's own /jobs URLs valid
    through the gateway.
    """
    client: httpx.AsyncClient = state["client"]  # type: ignore[assignment]
    url = backend.url + request.url.path
    if request.url.query:
        url = f"{url}?{request.url.query}"
    started = time.monotonic()

    upstream_request = client.build_request(
        request.method, url,
        headers=_request_headers(request),
        content=content,
        timeout=httpx.Timeout(backend.read_timeout, connect=CONNECT_TIMEOUT),
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # See _unreachable above for why this is 503 rather than 502.
        _log(request=request, backend=backend.name, model=model,
             status=f"unreachable:{type(exc).__name__}", started=started)
        return _unreachable(backend)
    except httpx.TimeoutException:
        _log(request=request, backend=backend.name, model=model,
             status="timeout", started=started)
        return _too_slow(backend)
    except ClientDisconnect:
        # The client hung up while we were still reading its upload. Nothing
        # can be delivered; 499 is nginx's code for it and never leaves here.
        _log(request=request, backend=backend.name, model=model,
             status="client-disconnect", started=started)
        return Response(status_code=499)
    except httpx.RequestError as exc:
        _log(request=request, backend=backend.name, model=model,
             status=f"failed:{type(exc).__name__}", started=started)
        return _failed(backend, exc)

    content_type = upstream.headers.get("content-type", "")

    if upstream.status_code >= 500 and "json" not in content_type:
        # An ffmpeg subprocess crash escaping a handler, a uvicorn-level 500,
        # a proxy error page: not an envelope, so it is wrapped in one. The
        # quote is truncated because a stack trace reaches a client that
        # cannot use it, and 200 bytes is enough to tell an HTML error page
        # from a Python exception. The whole body goes to the log.
        raw = await upstream.aread()
        await upstream.aclose()
        _log(request=request, backend=backend.name, model=model,
             status=f"{upstream.status_code}-nonjson", started=started)
        log.warning("%s returned %d with a non-JSON body: %r",
                    backend.name, upstream.status_code, raw[:4096])
        return error_response(
            502,
            f"{backend.name} answered {upstream.status_code} with a body that "
            f"is not an error envelope: {raw[:200]!r}",
            type_="server_error", code="backend_error")

    headers = _response_headers(upstream)
    if upstream.status_code == 503 and "retry-after" not in headers:
        # tts-stack answers 503 with code `model_loading` from /speak, /voices
        # and /v1/audio/speech while the synthesiser is still loading. That is
        # the backend's own answer and it passes through untouched; the header
        # is the only thing added. Kokoro is 330 MB and always resident, so
        # this window is seconds at container start, not minutes.
        headers["Retry-After"] = "10"

    return StreamingResponse(
        _body(upstream, request=request, backend=backend, model=model,
              started=started),
        status_code=upstream.status_code,
        headers=headers,
    )


# ------------------------------------------------------------ speech-to-text --
#
# Both routes stream the request body straight through. An hour of wav is
# 100 MB+, and buffering it here would double resident memory on a host that
# already keeps 6.5 GB of Chatterbox around. There is no routing decision to
# make: stt-stack is the only STT backend.


#: The largest upload this gateway will pass to stt-stack.
#:
#: "STREAMED, SO IT COSTS NOTHING HERE" WAS ONLY TRUE OF HERE. The gateway
#: hands the body straight through, so its own memory is flat whatever arrives
#: -- and stt-stack at the other end reads the clip to decode it, inside a
#: container with 6 GB. An unauthenticated POST on the only published port
#: could take that container down, and with it every transcription in flight.
#:
#: 512 MB is about five hours of 16-bit mono wav, which is past any clip this
#: is for and short of what hurts. Raise it with GATEWAY_UPLOAD_MAX_BYTES if a
#: real job needs more; the number is a guess about audio, not a law.
UPLOAD_MAX_BYTES = int(os.getenv("GATEWAY_UPLOAD_MAX_BYTES", str(512 * 1024 * 1024)))


class UploadTooLarge(Exception):
    """Raised from inside the forwarded stream once the cap is passed."""


async def _capped(request: Request) -> AsyncIterator[bytes]:
    """The request body, counted, and abandoned if it runs over.

    COUNTED RATHER THAN TRUSTED. content-length is checked first because it is
    cheap and it is what every real client sends, but it is a claim: omit it,
    send chunked, and the declared size is no size at all. So the bytes are
    counted as they pass, and the forward is abandoned mid-flight if they run
    over -- which costs the caller their upload and costs this stack nothing.
    """
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > UPLOAD_MAX_BYTES:
            raise UploadTooLarge
        yield chunk


async def _upload_to_stt(request: Request) -> Response:
    started = time.monotonic()
    declared = request.headers.get("content-length")
    over = declared and declared.isdigit() and int(declared) > UPLOAD_MAX_BYTES
    if not over:
        try:
            return await _proxy(request, STT, content=_capped(request))
        except UploadTooLarge:
            over = True
    # `upload_too_large` is the vocabulary services/stt already answers for an
    # oversized glossary and voice-ui for an oversized upload, so a client
    # branching on `code` learns the same thing everywhere in the estate.
    _log(request=request, backend="-", model=None, status="413-too-large", started=started)
    return error_response(
        413,
        f"this gateway passes an upload straight to the transcription service, "
        f"which reads it to decode it, so it is capped at "
        f"{UPLOAD_MAX_BYTES / 1024**2:.0f} MB and this one is larger. Split the "
        "audio, or raise GATEWAY_UPLOAD_MAX_BYTES on the gateway.",
        code="upload_too_large")


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request) -> Response:
    return await _upload_to_stt(request)


@app.post("/v1/audio/translations")
async def translations(request: Request) -> Response:
    """Speech in any language, English text out.

    THE DELETE /jobs/{id}/audio DEFECT FOR THE THIRD TIME, AND ON THE SURFACE
    THIS SERVICE EXISTS FOR. stt-stack has answered this route all along
    (`@router.post("/audio/translations")`, openai_api.py:1061) with the full
    field validation the transcription route has -- response_format, prompt,
    temperature, glossary, and a refusal by name for everything else. The one
    thing missing was a line in this table, so a request through the only
    published port met Starlette's 404 `unknown_url` and an aggregator's
    provider check read the whole /v1/audio surface as half-implemented.

    THE REASON IT WAS LEFT OUT HAS EXPIRED, and it is worth recording because
    it was a good reason. The exemption said "Parakeet refuses translation, so
    the route exists only to say so; routing it would publish a 400". That
    argument makes this gateway's route table depend on which checkpoint the
    STT container happens to have loaded -- and a deployment with
    STT_MODEL=whisper answers this route for real. Not routing it there hides a
    working feature; routing it under Parakeet publishes stt-stack's own 400,
    which names the engine, names the variable that changes it, and points at
    /v1/audio/transcriptions. A refusal a caller can act on is a better answer
    than a 404 that says the route does not exist.

    Streamed like its sibling above: an hour of wav is 100 MB+ and there is no
    routing decision in it -- stt-stack is the only STT backend. Capped like it
    too; see UPLOAD_MAX_BYTES for why streaming was not enough on its own.
    """
    return await _upload_to_stt(request)


@app.post("/transcribe")
async def transcribe(request: Request) -> Response:
    return await _upload_to_stt(request)


# ------------------------------------------------------------ text-to-speech --


@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    """The one route with a decision in it.

    The request body is buffered — not streamed — because `model` has to be
    read out of it to route. That body is text measured in kilobytes, unlike
    the audio uploads above. The response streams either way.

    When the model is `chatterbox`, this route MAY ANSWER 202 WITH JSON rather
    than audio. The gateway neither invents nor re-implements that: tts-long
    already made the call (short input is waited on and returned as audio,
    anything longer or any wait that expires returns 202 with a job id). The
    only job here is not to break it, which is why the long read timeout sits
    above the backend's own SYNC_TIMEOUT. Be honest about the cost:
    openai-python does not raise on a 2xx, so it hands that JSON to the caller
    as if it were audio and stream_to_file will write it into a .wav. Two
    things blunt it — the deviation is reachable only through an opt-in model
    name, so no unmodified client meets it by accident, and the Content-Type
    is application/json rather than audio/*, so a client that checks can tell.
    """
    # Started here rather than inside _proxy because the two ways out below
    # never reach a backend, and their duration is still the client's wait.
    # Passing time.monotonic() at the point of logging — as this route used to
    # — reports duration=0.000 for a body read that may have taken a minute.
    started = time.monotonic()

    try:
        raw = await request.body()
    except ClientDisconnect:
        # The client hung up while we were still reading its upload; nothing
        # can be delivered. This is the same case _proxy handles, and it logs
        # here for the same reason: the README promises one line per request,
        # and this was the one path that answered without writing one — the
        # 499s were invisible to grep, which is the whole observability budget.
        _log(request=request, backend="-", model=None,
             status="client-disconnect", started=started)
        return Response(status_code=499)

    try:
        body = json.loads(raw)
    except ValueError as exc:
        # The only body validation this gateway performs, and only because it
        # cannot route what it cannot parse. Everything else — empty input, a
        # bad voice, an unsupported response_format or speed — belongs to the
        # backend, which has better messages for all of them.
        _log(request=request, backend="-", model=None, status="400-badjson",
             started=started)
        return error_response(400, f"request body is not valid JSON: {exc}",
                              code="invalid_value")

    # A body that is valid JSON but not an object (a list, a bare string) is
    # forwarded rather than rejected: it has no `model`, so it goes fast, and
    # the backend's own validation says something more useful than this could.
    model = body.get("model") if isinstance(body, dict) else None
    key = model.strip().lower() if isinstance(model, str) else ""

    if key in LONG_MODELS:
        backend = LONG
    elif key in LONG_KNOWN:
        # A LONG-FORM NAME THIS DEPLOYMENT HAS NOT ENABLED IS A 404, NEVER
        # KOKORO. Falling through here would answer 200 with audio from the
        # fast backend: a different engine, a different voice, in whatever
        # language Kokoro guessed, and nothing in the response saying so. The
        # caller typed an engine name — the one thing they cannot have meant is
        # "surprise me". This is the only place the gateway rejects a model
        # string, and it rejects only names it can prove are ours.
        _log(request=request, backend="-", model=model, status="404-model",
             started=started)
        return error_response(
            404,
            f"model '{model}' is a long-form model this gateway knows but "
            f"this deployment has not enabled. Add it to GATEWAY_LONG_MODELS "
            f"(and to TTS_ENGINES on tts-long). Enabled: "
            f"{', '.join(sorted(LONG_MODELS))}.",
            code="model_not_found", param="model")
    else:
        backend = TTS

    return await _proxy(request, backend, content=raw,
                        model=model if isinstance(model, str) else None)


# ------------------------------------------------------------ chat, honestly --
#
# The route an aggregator probes before it will list this stack at all, and the
# one place in the estate where a plausible answer would be worse than an
# error. app/chat.py holds the whole argument and the two strings that can
# reach a caller; everything here is the hop to stt-stack.


# THE ONE BODY THIS PROCESS HOLDS THAT CARRIES AUDIO, AND THEREFORE THE ONE
# PLACE IT CAN BE OOM-KILLED. compose.yaml gives this container 512 MB, and the
# comment beside that number is the reason it is that small: "this process
# moves bytes between two sockets and never holds them: uploads and audio
# responses stream through, and the single buffered body is /v1/audio/speech's
# JSON, which is kilobytes". Every other audio route here is `content=
# request.stream()`. This one cannot be — the clip is base64 INSIDE a JSON
# object and the transcript has to be wrapped before anything is sent — so
# without a ceiling the sentence above stops being true and the memory limit
# stops being a budget.
#
# MEASURED, NOT GUESSED: 3.8x the body, linear over 5.3 / 10.7 / 21.3 / 32.0 MB
# bodies (tracemalloc peak over baseline, mock backend, one request in flight).
# The base64 string, the bytes it arrived as, the decoded clip and the
# multipart body built for the next hop are all resident at once. So 16 MiB
# costs about 61 MB of a 512 MB container and several can be in flight without
# touching the limit, where an uncapped 400 MB body is 1.5 GB and the container
# dies -- taking every other request through the only published port with it.
#
# 16 MiB of base64 is 12 MB of audio: roughly six minutes of 16 kHz mono wav or
# twenty-five of a 64 kbps mp3. Anything longer belongs on
# POST /v1/audio/transcriptions, which streams and has no ceiling here at all,
# and the refusal below says so rather than leaving a caller to guess.
CHAT_MAX_BYTES = int(os.getenv("GATEWAY_CHAT_MAX_BYTES", str(16 * 1024 * 1024)))


class _Transcript(NamedTuple):
    """Either the text, or the backend's own answer to hand straight back."""

    text: str | None
    engine: str | None
    failure: Response | None


async def _transcribe(audio: chat.Audio, *, model: str, request: Request,
                      started: float) -> _Transcript:
    """One clip to stt-stack's own OpenAI route, buffered.

    NOT `_proxy`, AND IT CANNOT BE. _proxy forwards the caller's request and
    streams the backend's answer out untouched; here the request is SYNTHESISED
    from a JSON body and the answer has to be READ before anything can be sent,
    because the transcript goes inside a chat envelope this service builds.
    What keeps that from being the 100 MB wav problem the transcription route
    above is streamed to avoid is CHAT_MAX_BYTES, enforced on the way in — not
    the shape of the request, which bounds nothing.

    `model` is forwarded rather than replaced. stt-stack requires the field and
    cannot choose an engine with it, so passing the caller's string keeps the
    run record honest: its listing shows what was asked for beside what ran.
    """
    client: httpx.AsyncClient = state["client"]  # type: ignore[assignment]
    try:
        upstream = await client.post(
            STT.url + "/v1/audio/transcriptions",
            files={"file": (audio.filename, audio.data, audio.content_type)},
            data={"model": model, "response_format": "json"},
            timeout=httpx.Timeout(STT.read_timeout, connect=CONNECT_TIMEOUT))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        _log(request=request, backend=STT.name, model=model,
             status=f"unreachable:{type(exc).__name__}", started=started)
        return _Transcript(None, None, _unreachable(STT))
    except httpx.TimeoutException:
        _log(request=request, backend=STT.name, model=model, status="timeout",
             started=started)
        return _Transcript(None, None, _too_slow(STT))
    except httpx.RequestError as exc:
        _log(request=request, backend=STT.name, model=model,
             status=f"failed:{type(exc).__name__}", started=started)
        return _Transcript(None, None, _failed(STT, exc))

    engine = upstream.headers.get("x-stt-engine")
    if upstream.status_code != 200:
        # THE BACKEND'S OWN ENVELOPE, FORWARDED RATHER THAN REWORDED. stt-stack
        # says which field was wrong, which engine is loaded and which variable
        # changes it; anything this service wrote instead would be a worse
        # version of that with a chat envelope round it. Status and body go
        # back exactly as they arrived, which is the same promise
        # test_a_backend_envelope_is_never_rewrapped holds _proxy to.
        _log(request=request, backend=STT.name, model=model,
             status=upstream.status_code, started=started)
        return _Transcript(None, engine, Response(
            content=upstream.content, status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type",
                                            "application/json")))

    try:
        text = upstream.json()["text"]
    except (ValueError, KeyError, TypeError):
        _log(request=request, backend=STT.name, model=model,
             status="200-unreadable", started=started)
        log.warning("%s answered 200 with no `text` field: %r", STT.name,
                    upstream.content[:4096])
        return _Transcript(None, engine, error_response(
            502,
            f"{STT.name} answered 200 to a transcription with a body that "
            "carries no `text` field, so there is nothing to put in the "
            "assistant message.",
            type_="server_error", code="backend_error"))

    _log(request=request, backend=STT.name, model=model,
         status=upstream.status_code, started=started,
         rtf=upstream.headers.get("x-realtime-factor"))
    return _Transcript(str(text), engine, None)


async def _chat_body(request: Request) -> bytes | None:
    """The whole request body, or None when it is over CHAT_MAX_BYTES.

    COUNTED WHILE READING RATHER THAN TRUSTED FROM Content-Length, which is the
    difference between a ceiling and a request to please stay under one. The
    header is checked first because it lets an honest client be refused before
    it uploads anything, but a chunked request carries no Content-Length at all
    and `curl -H 'Transfer-Encoding: chunked'` is one flag away -- so a check
    that stopped at the header would be bypassed by the one caller who meant
    to. voice-ui's own cap stops at the header and is right to: it forwards a
    stream it never holds, so the bytes it does not count cost it nothing. This
    route holds every byte it reads.

    Returning None rather than raising, because the refusal has to be logged
    with the duration of the read that led to it, and that is the caller's
    wait: a 400 MB upload refused at the end still took as long as a 400 MB
    upload.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > CHAT_MAX_BYTES:
        return None

    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > CHAT_MAX_BYTES:
            # Stop reading. What has arrived is dropped rather than parsed:
            # half a JSON body is not a smaller request, it is a different one.
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Transcribe through the chat surface, or say what this stack is.

    THE ANSWER IS NEVER COMPOSED HERE. See app/chat.py: the assistant message
    is a transcript stt-stack produced or the constant NO_MODEL_REPLY, and the
    reason that matters is not politeness -- it is that a tool added later must
    not be able to conclude from a plausible reply that the NAS runs a language
    model and start routing real traffic to it.
    """
    started = time.monotonic()

    try:
        raw = await _chat_body(request)
    except ClientDisconnect:
        # The same case /v1/audio/speech handles, logged for the same reason:
        # one line per request, or the 499s are invisible to grep.
        _log(request=request, backend="-", model=None,
             status="client-disconnect", started=started)
        return Response(status_code=499)

    if raw is None:
        # THE ONE REFUSAL THIS ROUTE MAKES BEFORE READING ANYTHING. See
        # CHAT_MAX_BYTES: 413 rather than 400 because the payload is the
        # problem, which is the code services/stt already answers for an
        # oversized glossary and the one voice-ui answers for an oversized
        # upload -- `upload_too_large` is that same vocabulary, so a client
        # branching on `code` learns the same thing everywhere in the estate.
        _log(request=request, backend="-", model=None, status="413-too-large",
             started=started)
        return error_response(
            413,
            f"a chat body is buffered whole here to find the audio inside it, "
            f"so it is capped at {CHAT_MAX_BYTES / 1024**2:.0f} MB and this "
            "one is larger. POST /v1/audio/transcriptions takes the clip as a "
            "file upload and streams it through rather than holding it, so its "
            f"ceiling is far higher at {UPLOAD_MAX_BYTES / 1024**2:.0f} MB.",
            code="upload_too_large")

    try:
        body = json.loads(raw)
    except ValueError as exc:
        _log(request=request, backend="-", model=None, status="400-badjson",
             started=started)
        return error_response(400, f"request body is not valid JSON: {exc}",
                              code="invalid_value")

    try:
        ask = chat.read(body)
    except ApiError as exc:
        # Raised rather than returned so install_errors renders it, and caught
        # on the way past ONLY to write the line. A refusal that leaves no log
        # entry is the one shape of failure this service cannot be asked about
        # afterwards, and a body full of unhonourable fields is exactly what an
        # unfamiliar client sends first.
        _log(request=request, backend="-",
             model=body.get("model") if isinstance(body, dict) else None,
             status=f"{exc.status}-{exc.code}", started=started)
        raise

    engine: str | None = None
    if ask.audio is None:
        text = chat.NO_MODEL_REPLY
        _log(request=request, backend="-", model=ask.model, status="200-noaudio",
             started=started)
    else:
        result = await _transcribe(ask.audio, model=ask.model, request=request,
                                   started=started)
        if result.failure is not None:
            return result.failure
        text, engine = str(result.text), result.engine

    # The engine that actually ran, on the response, exactly as stt-stack puts
    # it on its own. `model` in the body echoes what the caller sent because the
    # specification says it is their model string; the header is where this
    # stack is honest about which checkpoint produced the words.
    headers = {"x-stt-engine": engine} if engine else {}
    ident = chat.identifier()
    if ask.stream:
        return StreamingResponse(
            chat.stream(model=ask.model, text=text, ident=ident),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     **headers})
    return Response(
        content=json.dumps(chat.completion(model=ask.model, text=text,
                                           ident=ident)),
        media_type="application/json", headers=headers)


@app.post("/speak")
async def speak(request: Request) -> Response:
    # Native tts-stack route. No decision to make, so the body streams.
    return await _proxy(request, TTS, content=request.stream())


@app.get("/voices")
async def voices(request: Request) -> Response:
    return await _proxy(request, TTS, content=None)


# ---------------------------------------------------------- glossaries --
#
# Native, not /v1: OpenAI has no concept of a glossary profile, so there is
# nothing to be 1:1 with and claiming /v1/glossaries would take spec territory
# that does not exist. See docs/adr/0003.
#
# The PUT streams its body and carries the query string -- _proxy appends
# request.url.query already, which matters here more than anywhere else on this
# service: ?force=true is what lets a single-word left-hand side through, and
# dropping it silently would make a `belly = Belli` rule unenterable through
# the front door while appearing to work.


@app.get("/glossaries")
async def list_glossaries(request: Request) -> Response:
    return await _proxy(request, STT, content=None)


@app.get("/glossaries/{name}")
async def get_glossary(request: Request, name: str) -> Response:
    return await _proxy(request, STT, content=None)


@app.put("/glossaries/{name}")
async def put_glossary(request: Request, name: str) -> Response:
    return await _proxy(request, STT, content=request.stream())


@app.delete("/glossaries/{name}")
async def delete_glossary(request: Request, name: str) -> Response:
    return await _proxy(request, STT, content=None)


# ------------------------------------------------------------------- the page --
#
# ONE PUBLISHED PORT. voice-ui used to publish 30081 of its own, so the stack
# had two doors and the sentence in compose.yaml about there being one was only
# true of the backends. The page is now reached through this service, which is
# what "the gateway is the only door" was supposed to mean all along.
#
# EXPLICITLY LISTED, NOT A WILDCARD, for the reason _http_error already gives:
# a catch-all would proxy /docs and /openapi.json to a service that deliberately
# does not publish them. These are exactly voice-ui's own routes -- `/`, the
# page, and the /ui/* family -- and nothing else reaches it.
#
# The UI's PROXIED table is NOT among them and must not be. It forwards /v1 and
# the native routes, which this service already answers itself; routing them to
# voice-ui would send a request out to the UI so it could send it back here.
# The page reaches them under /ui/api/, which voice-ui strips before forwarding
# -- one origin for the browser, and the container's key still applied.
UI = Backend(
    name="voice-ui",
    url=os.getenv("GATEWAY_UI_URL", "http://voice-ui:8090").rstrip("/"),
    # 900 s because /ui/fetch is on this path: it streams a finished download
    # from MeTube into the transcription route, and a two-hour podcast at the
    # measured 8.5-10.4x realtime is ~847 s of compute inside that one request.
    # Anything shorter would 504 a transcription that is still working.
    read_timeout=float(os.getenv("GATEWAY_UI_TIMEOUT", "900")),
    timeout_help="The page's own routes are quick; /ui/fetch is not, because "
                 "it transcribes. Its ceiling is the same as /v1/audio/"
                 "transcriptions -- roughly two hours of audio.",
)

UI_PATHS = (
    ("GET", "/"),
    ("GET", "/ui"),
    ("GET", "/ui/health"),
    ("GET", "/ui/config"),
    ("GET", "/ui/clips"),
    ("POST", "/ui/clips"),
    ("DELETE", "/ui/clips/{name}"),
    # Cloning from a link. Listed before the {name} route above would match it
    # -- Starlette takes the first match, and /ui/clips/from-link is a valid
    # {name} -- but that one is DELETE and this is POST, so they cannot
    # collide. Named here anyway rather than relying on that.
    ("POST", "/ui/clips/from-link"),
    ("POST", "/ui/resolve"),
    ("POST", "/ui/commit"),
    ("POST", "/ui/abandon"),
    ("GET", "/ui/progress"),
    ("POST", "/ui/fetch"),
    # A captions download is already a transcript, so it never reaches stt.
    # Absent here, the page 404s on it when served from the published port --
    # which is how DELETE /jobs/{id} stayed unreachable while tts-long had
    # implemented it all along.
    ("POST", "/ui/captions"),
    # The media relay. Without it playback 404s from the published port, which
    # is the DELETE /jobs/{id} failure again -- implemented behind the gateway
    # and unreachable through it. _proxy already relays Range and
    # Content-Range: only hop-by-hop headers, host and authorization are
    # dropped, so a byte range survives the hop untouched.
    ("GET", "/ui/media"),
    # The prefixed mount of voice-ui's own proxy. Everything under it is
    # forwarded verbatim and voice-ui strips /ui/api before sending it back
    # here with UI_GATEWAY_API_KEY attached. A path parameter rather than a
    # list because the set it covers is voice-ui's PROXIED table, which is
    # already an allowlist on that side; duplicating it here would be two
    # lists to keep in step.
    # THE PAGE'S OWN MOUNT, and every method it can arrive with. PUT was
    # missing and nothing noticed until a profile was written against the
    # deployed stack: the UI's allowlist had been extended for PUT and so had
    # this service's own /glossaries routes, but the /ui/api PASSTHROUGH in
    # between had not, so the page's save died here with 405
    # method_not_supported before it reached either.
    #
    # The seam is easy to miss because it belongs to neither side. Whoever adds
    # a method to the UI's PROXIED table has to add it here as well, and the
    # test named after this defect is what says so.
    ("POST", "/ui/api/{rest:path}"),
    ("GET", "/ui/api/{rest:path}"),
    ("PUT", "/ui/api/{rest:path}"),
    ("DELETE", "/ui/api/{rest:path}"),
    # PATCH arrived with the Satellites tab (PATCH /satellites/{id}, a
    # satellite's settings).
    ("PATCH", "/ui/api/{rest:path}"),
)


async def _to_ui(request: Request) -> Response:
    """Everything the page needs, streamed from voice-ui.

    An upload body is streamed rather than read: POST /ui/clips carries a
    reference clip and /ui/api/v1/audio/transcriptions carries whatever the
    browser is transcribing, and buffering either here would put a file this
    process has no reason to hold into a container limited to 512 MB.
    """
    streaming = request.method in ("POST", "PUT", "PATCH")
    return await _proxy(request, UI,
                        content=request.stream() if streaming else None)


for _method, _path in UI_PATHS:
    app.add_api_route(_path, _to_ui, methods=[_method],
                      include_in_schema=False)


# -------------------------------------------------------------- satellites --
#
# voice-satellites, the hub for thin audio devices (clients/korvo-satellite).
# Optional: GATEWAY_SATELLITES_URL="" leaves every route below answering 503
# and keeps it out of /health, so a deployment without the hub is unchanged.
#
# Like everything else here the paths are flat and explicit, and the device
# socket is the one WebSocket this gateway relays.
SATELLITES = Backend(
    name="voice-satellites",
    url=os.getenv("GATEWAY_SATELLITES_URL", "http://voice-satellites:8003").rstrip("/"),
    # 120 s: the slowest routes are /satellites/{id}/say, which waits for
    # Kokoro, /satellites/{id}/listen, which records for up to 60 s by design,
    # and the two that run a sentence through STT, an assistant and TTS
    # (/satellites/routing/test and /satellites/{id}/inject), each stage under
    # its own ceiling of 15-30 s.
    read_timeout=float(os.getenv("GATEWAY_SATELLITES_TIMEOUT", "120")),
    timeout_help="A listen records for as long as it was asked to, up to 60 s; "
                 "a say waits for the whole sentence to be synthesised, and a "
                 "routing test for the assistant. Ask for less.",
)

SATELLITES_PATHS = (
    ("GET", "/satellites"),
    ("GET", "/satellites/events"),
    ("GET", "/satellites/firmware"),
    ("POST", "/satellites/firmware"),
    ("DELETE", "/satellites/firmware/{sha256}"),
    ("POST", "/satellites/ota"),
    # Above /satellites/{nid}, as in voice-satellites itself: here both would
    # reach the same backend path, but PUT and the POST below have no
    # /satellites/{nid} twin and would answer 405 if they were left to it.
    ("GET", "/satellites/routing"),
    ("PUT", "/satellites/routing"),
    ("POST", "/satellites/routing/test"),
    # The wake words and the satellites each is assigned to: above
    # /satellites/{nid} for the same reason, PUT again having no twin there.
    ("GET", "/satellites/wake-words"),
    ("PUT", "/satellites/wake-words"),
    ("POST", "/satellites/wake-words/models"),
    ("DELETE", "/satellites/wake-words/models/{name}"),
    ("GET", "/satellites/{nid}"),
    ("PATCH", "/satellites/{nid}"),
    ("GET", "/satellites/{nid}/listen"),
    # inject is routed for scripts that verify the listening path with a
    # recorded clip, behind the same keys as everything else here; the page
    # never calls it (see NOT_ON_PAGE in tests/test_gateway.py).
    *(("POST", f"/satellites/{{nid}}/{action}") for action in (
        "adopt", "forget", "identify", "reboot", "lights", "tone", "say",
        "flush", "set-hub", "inject")),
)


async def _to_satellites(request: Request) -> Response:
    if not SATELLITES.url:
        return _unreachable(SATELLITES)
    streaming = request.method in ("POST", "PUT", "PATCH")
    return await _proxy(request, SATELLITES,
                        content=request.stream() if streaming else None)


for _method, _path in SATELLITES_PATHS:
    app.add_api_route(_path, _to_satellites, methods=[_method],
                      include_in_schema=False)


# THE DEVICE SOCKET HAS TWO PATHS AND ONE HANDLER. The feature was called
# "nodes" until 2026-09-25, and a board in the field runs firmware that
# connects to /nodes/ws. Its next firmware arrives over that same socket, so a
# gateway that stopped answering the old path would strand the board on the
# old image with USB as the only way back. Both paths relay to the hub's
# /satellites/ws; the hub answers the old one too, for the same reason.
SATELLITES_SOCKET = "/satellites/ws"
LEGACY_SATELLITES_SOCKET = "/nodes/ws"


@app.websocket(SATELLITES_SOCKET)
@app.websocket(LEGACY_SATELLITES_SOCKET)
async def satellites_socket(client: WebSocket) -> None:
    """Relay one device connection to voice-satellites, frame for frame.

    NOT BEHIND GATEWAY_API_KEYS, deliberately, and the key middleware could not
    see it anyway: it is an http middleware and this is a websocket scope. A
    device is never given a gateway key -- one baked into firmware would be in
    every flash dump -- so what a connection may do is decided by
    voice-satellites, by the token it issued on adoption. An unadopted device
    can say hello and be told "pending"; nothing else passes until someone
    adopts it through the authenticated routes above.
    """
    if not SATELLITES.url:
        await client.close(code=1013)
        return
    await client.accept()
    peer = client.client.host if client.client else ""
    target = SATELLITES.url.replace("http", "ws", 1) + SATELLITES_SOCKET
    try:
        upstream = await ws_connect(
            target, additional_headers={"X-Forwarded-For": peer},
            open_timeout=CONNECT_TIMEOUT, max_size=2**20,
            # The device pings this socket and uvicorn answers; the hop to
            # voice-satellites is a container on the same network.
            ping_interval=None)
    except (OSError, TimeoutError) as exc:
        log.warning("satellites: cannot reach %s for %s: %s", target, peer, exc)
        await client.close(code=1013)
        return

    async def up() -> None:
        while True:
            msg = await client.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def down() -> None:
        async for msg in upstream:
            if isinstance(msg, bytes):
                await client.send_bytes(msg)
            else:
                await client.send_text(msg)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await client.close()
        except RuntimeError:
            pass  # already closed by the device


# --------------------------------------------------------------- long jobs --
#
# Flat and unprefixed, which is load-bearing: tts-long's 202 carries
# `Location: /jobs/{id}` and `audio_url: /jobs/{id}/audio`, both relative to
# its own root. Mounted here at the same paths they remain correct with zero
# rewriting. There is deliberately no unified job abstraction over the two TTS
# backends: it would mean the gateway holds job state, and then needs storage,
# restart survival, its own /jobs endpoints and an answer for in-flight jobs
# when it redeploys. tts-long already has all of that.


@app.post("/jobs")
async def create_job(request: Request) -> Response:
    # A cold tts-long reports model_loaded:false for minutes (6.5 GB, lazy,
    # ~3 GB downloaded on the very first job ever) and accepts work anyway —
    # the queue absorbs it and the worker loads the model. Nothing here gates
    # on that: a gateway that synthesised a 503 from model_loaded:false would
    # reject the request that was about to warm the model, for the whole
    # cold-start window, turning a working design into an outage.
    return await _proxy(request, LONG, content=request.stream())


@app.get("/jobs")
async def list_jobs(request: Request) -> Response:
    return await _proxy(request, LONG, content=None)


@app.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> Response:
    return await _proxy(request, LONG, content=None)


@app.delete("/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str) -> Response:
    """Cancel a queued job, or discard a finished one.

    tts-long has had this route all along (`@app.delete("/jobs/{job_id}")`,
    main.py:580); this table simply never carried it, so a DELETE through the
    gateway met Starlette's 405 and `method_not_supported`. The consequence was
    that a Chatterbox job could be started through the front door and not
    called off through it — and at 0.138x realtime the jobs that most need
    calling off are the ones measured in tens of minutes.

    Not routing it was never a decision, and the asymmetry says so: POST /jobs
    and both GETs were here from the start. Adding it is three lines and closes
    the one thing a UI could not offer.
    """
    return await _proxy(request, LONG, content=None)


@app.get("/jobs/{job_id}/audio")
async def get_job_audio(request: Request, job_id: str) -> Response:
    return await _proxy(request, LONG, content=None)


@app.delete("/jobs/{job_id}/audio")
async def delete_job_audio(request: Request, job_id: str) -> Response:
    """Throw away the audio and keep the record of the job that made it.

    THE DEFECT ABOVE, REPEATED ONE METHOD LATER. tts-long has answered
    `DELETE /jobs/{job_id}/audio` all along and neither route table carried it,
    so the Jobs tab's "delete the audio" button met Starlette's 405 and
    `method_not_supported` -- reproduced against the deployed stack. The GET on
    the line above is what makes it easy to miss: the path is plainly here, and
    the allowlist is matched on the PAIR.

    NOT THE SAME BUTTON AS DELETE /jobs/{id}. That one discards the whole job.
    This one is the only way to reclaim the disk a finished clone is holding
    while keeping the row that says it ran, which is the entire reason a
    record outlives its audio.
    """
    return await _proxy(request, LONG, content=None)


# -------------------------------------------------------------- meta routes --


@app.get("/v1/models")
async def models() -> Response:
    """The routing table, as OpenAI's model list. No backend is contacted.

    The names are a property of this gateway's routing contract rather than of
    any backend's state, so asking a backend would be asking the wrong process
    — and would fail while one was restarting, which is precisely when a
    client most wants to know what it can send.
    """
    return Response(content=json.dumps(MODEL_LIST),
                    media_type="application/json")


@app.get("/v1/models/{model_id:path}")
async def model(model_id: str) -> Response:
    """Retrieve one model, off the same rows GET /v1/models publishes.

    THE STANDARD CALL THAT WAS A 404, AND THE CLIENTS THAT DEPEND ON IT are not
    hypothetical: an aggregator walks the list and then retrieves each row to
    confirm it is really there, so a 404 here reads as a provider advertising
    models it does not have. MODEL_ROWS is an index OF the published list rather
    than a second table, so the two cannot come to disagree.

    `{model_id:path}` rather than a plain parameter because OpenAI's own ids
    carry slashes -- `ft:gpt-4o:acme::abc` does not, but a HuggingFace-style
    `owner/name` does, and this estate's catalogue is checkpoint names that may
    yet grow one. A bare parameter would answer 404 for the front half of such
    a name, which is the confusing failure rather than the clear one.

    A LONG-FORM NAME THIS DEPLOYMENT HAS NOT ENABLED GETS THE SAME SENTENCE THE
    SPEECH ROUTE GIVES IT. Two endpoints saying "no such model" and "add it to
    GATEWAY_LONG_MODELS" about one string would send an operator looking for
    two different faults.
    """
    key = model_id.strip().lower()
    row = MODEL_ROWS.get(key)
    if row is not None:
        return Response(content=json.dumps(row),
                        media_type="application/json")
    if key in LONG_KNOWN:
        return error_response(
            404,
            f"model '{model_id}' is a long-form model this gateway knows but "
            f"this deployment has not enabled. Add it to GATEWAY_LONG_MODELS "
            f"(and to TTS_ENGINES on tts-long). Enabled: "
            f"{', '.join(sorted(LONG_MODELS))}.",
            code="model_not_found", param="model")
    return error_response(
        404,
        f"model '{model_id}' does not exist. GET /v1/models lists the names "
        "this gateway routes.",
        code="model_not_found", param="model")


async def _probe(backend: Backend) -> dict[str, object]:
    client: httpx.AsyncClient = state["client"]  # type: ignore[assignment]
    result: dict[str, object] = {"url": backend.url}
    try:
        # No Authorization header, deliberately: the backends' /health are
        # unauthenticated for the same reason this one is, and a probe that
        # needed a key would be a probe that stops working the day one is set.
        response = await client.get(
            backend.url + "/health",
            timeout=httpx.Timeout(HEALTH_TIMEOUT, connect=CONNECT_TIMEOUT))
    except httpx.RequestError as exc:
        result["reachable"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["reachable"] = response.status_code == 200
    result["http_status"] = response.status_code
    try:
        # The backend's own body, inlined rather than summarised. tts-stack
        # says how many voices loaded, stt-stack names the model, tts-long
        # reports model_loaded and the queue depth — all three are the answer
        # to a different question an operator is about to ask.
        result["health"] = response.json()
    except ValueError:
        result["health"] = {"body": response.text[:200]}
    return result


@app.get("/health")
async def health() -> Response:
    """Every backend's health in one unauthenticated call.

    This is the gateway's main justification beyond routing: today, knowing
    the state of the stack means polling three ports.

    It always answers 200, even when a backend is down. The TrueNAS healthcheck
    for THIS container calls THIS endpoint, and a 503 here because a sibling is
    restarting would have the orchestrator restart the gateway — a container
    must not be killed for a sibling's fault. Read `status`, not the code.
    Each container keeps its own healthcheck pointed at its own localhost
    /health for exactly the same reason.
    """
    stt, tts, long = await asyncio.gather(_probe(STT), _probe(TTS), _probe(LONG))
    backends = {"stt": stt, "tts": tts, "tts_long": long}
    # The hub is optional; configured, it counts like any other backend.
    if SATELLITES.url:
        backends["satellites"] = await _probe(SATELLITES)
    # `ok` only if all three answered. A backend that answered 200 while still
    # loading its model is still `ok` here — it answered, and its own body
    # says "loading" for anyone reading past the first field.
    everything = all(b["reachable"] for b in backends.values())
    return Response(
        content=json.dumps({"status": "ok" if everything else "degraded",
                            "gateway": "ok",
                            "backends": backends}),
        media_type="application/json")
