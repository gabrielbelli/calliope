"""The page, the reference-clip store and link ingestion, for one signed-in person at a time.

    GET    /ui  /ui/<tab>/...      the page: one HTML file, no build step
    GET    /ui/config              the flags and limits the page starts with
    GET    /ui/clips  POST  DELETE the reference-clip store (voice cloning)
    POST   /ui/resolve /commit /abandon /fetch /captions
    GET    /ui/progress /media     link ingestion: yt-dlp in a guarded child
                                   process, app/fetcher.py -- see app/ingest.py
    GET    /health                 this container's own probe

WHY THIS IS A FIFTH CONTAINER AND NOT ROUTES ON THE GATEWAY. Ingestion needs
yt-dlp, and yt-dlp in the gateway would make the process that checks every
credential also the process that spawns a subprocess on a URL a browser chose.
Separate images keep that blast radius where it is: this one holds nothing
that can act as anybody, because an identity is something it is told and can
only verify (D4).

WHO IS ASKING IS THE GATEWAY'S ANSWER, NOT THIS SERVICE'S. The gateway is the
only door and the only thing that checks a session or a key (D3). It forwards
each request here with a signed identity assertion, and identity.install below
refuses anything without a valid one for the audience `ui`, except GET /health
(D52). The gateway's route table has already decided whether this person may
use the route, so nothing here decides it again.

WHAT THIS SERVICE DOES DECIDE IS WHOSE DATA A REQUEST TOUCHES. A clip is saved
into the caller's own directory and listed and deleted from there; a holder of
voices:write:all may list and delete anyone's, by ?owner= (D35). A link answers
only the person who resolved it (D36, app/ingest.py).

THE PAGE CALLS THE GATEWAY ITSELF. It is same-origin with the gateway, so its
session cookie goes with every call and the gateway answers /v1, /jobs,
/glossaries, /satellites and /health directly. The forwarding table this
service used to keep, its /ui/api mount and the container key it added on the
way past are gone, and with them the arrangement in which everyone who could
reach this port acted as one shared credential.

ONE OUTBOUND CALL CARRIES A CREDENTIAL: /ui/fetch, which sends a finished
download to the gateway's internal listener with this service's key and the
person's delegation token (D64). Every outbound request is built from named
headers and never from the inbound request's (D65).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from voice_common import identity
from voice_common import logging as voice_logging
from voice_common.errors import (ApiError, error_response, http_error_response,
                                 install_errors, insufficient_scope)
from voice_common.health import install_health
from voice_common.scopes import check_owner_filter, valid_scope

from . import clips, config, downloads, ingest

# The shared setup, not a fourth basicConfig. The format string here was
# byte-identical to voice_common.logging.FORMAT, and using the shared one also
# brings UI_LOG_LEVEL: without it there was no way to get DEBUG output of this
# service without editing the source and rebuilding the image.
log = voice_logging.setup("voice-ui", "UI")
# One access line per request is this service's whole observability budget, as
# it is the gateway's. httpx logging one of its own would double every line and
# say less.
logging.getLogger("httpx").setLevel(logging.WARNING)

PAGE = Path(__file__).with_name("static") / "ui.html"

# The scope that lets ?owner= name somebody else's clips (D35).
ALL_VOICES = "voices:write:all"


def new_client() -> httpx.AsyncClient:
    """The one connection pool, kept open for the process lifetime.

    A module-level function rather than an inline constructor for the same
    reason services/gateway has one: it is the seam the tests hand a transport
    through, so every outbound path runs for real against a mock gateway with
    no socket anywhere.

    It carries no default header of its own: each request names the headers
    it sends (D65).
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        # A redirect means something about the far end's routing, and
        # following one here would hide it -- and on /ui/fetch it would carry
        # this service's key to wherever it pointed.
        follow_redirects=False)


# The variables of the release that fetched links through MeTube. Named in one
# warning if any is still set, never with its value. One string, split, so no
# name is a quoted literal: docs/tests reads a quoted name as a setting the
# code still reads, and a compose file that sets one must keep failing there.
RETIRED = tuple("UI_METUBE_URL UI_METUBE_FOLDER UI_METUBE_FORMAT "
                "UI_METUBE_VIDEO_FORMAT UI_PROBE UI_MAX_MEDIA_BYTES".split())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.client = new_client()
    downloads.startup()
    log.info("ready: gateway=%s links=%s cache=%s voices=%s",
             config.GATEWAY_INTERNAL_URL, "on" if downloads.available() else "off",
             config.CACHE_DIR, config.VOICE_DIR)
    if config.IGNORED_INTERNAL_URL:
        # The name and the rule, never the value: a URL can carry a password.
        log.error("UI_GATEWAY_INTERNAL_URL is ignored and %s is used instead: "
                  "/ui/fetch sends this service's key there, so it may name "
                  "only the gateway's internal listener or a loopback address",
                  identity.GATEWAY_INTERNAL)
    if retired := [name for name in RETIRED if name in os.environ]:
        log.warning("%s is set and no longer read: links are fetched in this "
                    "container now. Remove it.", ", ".join(retired))
    if config.FETCHER:
        log.warning("UI_FETCHER is set: links are fetched by a test stand-in.")
    yield
    await downloads.shutdown()
    await app.state.client.aclose()


app = FastAPI(
    title="voice-ui",
    description="The page and its own routes, behind the gateway.",
    lifespan=lifespan,
    # identity.install removes these too; saying so here as well keeps the
    # constructor honest about what this service publishes, which is nothing.
    openapi_url=None, docs_url=None, redoc_url=None,
)
install_errors(app)
# Every request but GET /health needs the gateway's assertion for `ui`. The
# delegation token is kept for /ui/fetch alone, the one route that spends it.
identity.install(app, "ui", credentials=ingest.CREDENTIALS,
                 delegation_paths=("/ui/fetch",))


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """Every 404 and 405 in the envelope the page reads `error.message` from.

    Registered after install_errors and so in place of its handler, which
    keeps FastAPI's `{"detail": ...}` off /v1: nothing calls this service but
    the page, and the page reads the envelope everywhere.
    """
    return http_error_response(
        request, exc, unknown_url_hint="This service answers the page and its "
                                       "own /ui routes, and nothing else.")


# ------------------------------------------------------------------- page --

# An inline <script> without a src, as the HTML tokenizer reads one: from the
# end of the opening tag to the first </script, whatever the script contains.
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script",
                            re.IGNORECASE | re.DOTALL)
# What script-src says of a page with no inline script: nothing may run.
NO_SCRIPT = "'none'"


def _script_hash(body: str) -> str:
    # The browser hashes the script after normalising its newlines, so a file
    # saved with CRLF must be hashed the way it will be read, not as stored.
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def policy(page: str) -> str:
    """The page's Content-Security-Policy, with every inline script named by its hash (§4.7).

    No 'unsafe-inline' for scripts, and no 'self' either: the page is one file
    and loads no script from anywhere, so a script that is not one of these
    exact bytes does not run -- including one a page title smuggled into the
    DOM, and one served from this origin by some other route. Inline event
    handler attributes are not covered by a hash and are blocked with it; the
    page attaches its handlers with addEventListener.
    """
    scripts = " ".join(_script_hash(body) for body in _INLINE_SCRIPT.findall(page))
    return ("default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            # THE DISPLAY FACE IS A data: URI IN THE STYLESHEET. This service
            # serves exactly one file, so a sibling .woff2 would need a route of
            # its own; inlined, the page stays one thing to copy. Without this
            # line fonts fall back to default-src 'self' and the face is
            # silently blocked -- the page still renders, in system-ui.
            "font-src 'self' data:; "
            f"script-src {scripts or NO_SCRIPT}; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            # Never framed, so no other site can dress it up and click it; no
            # form posts anywhere else; and no <base> that would re-point every
            # relative call the page makes.
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")


@lru_cache(maxsize=1)
def _load(stamp: tuple[int, int]) -> tuple[str, str]:
    """The page and its policy, for one version of the file on disk."""
    del stamp  # the cache key: a new mtime or size reads the file again
    text = PAGE.read_text(encoding="utf-8")
    return text, policy(text)


def _with_scopes(text: str, request: Request) -> str:
    """The page with the session's scopes on <html>, for the dock's first paint.

    The page draws only the tabs a session may open, and without this it
    learnt which from /auth/me after the first paint, so an admin's bar grew
    by two tabs under the reader. The scopes are the caller's own, as
    /auth/me gives them, and each matches the scope grammar, so nothing
    written here can close the attribute. The script's bytes are untouched,
    so its hash in the policy still holds.
    """
    claims = identity.claims_of(request)
    held = " ".join(sorted(s for s in claims.scopes if valid_scope(s)))
    return text.replace("<html lang=\"en\">", f'<html lang="en" data-scopes="{held}">', 1)


@app.get("/ui", include_in_schema=False)
async def page(request: Request) -> Response:
    """The whole UI: one file, inline CSS and JS, no build step.

    Read again whenever the file changes rather than once at import, so it can
    still be edited in a running container and reloaded; the script hashes are
    worked out once per version of the file rather than once per request.
    """
    info = PAGE.stat()
    text, csp = _load((info.st_mtime_ns, info.st_size))
    return HTMLResponse(_with_scopes(text, request), headers={
        # NEVER CACHED. The page carried no Cache-Control at all, so browsers
        # applied their own heuristic and served a stale copy -- a control
        # added and deployed was reported as missing, and the diagnosis went
        # through the markup, the boot order and the route table before
        # reaching the cache.
        #
        # There is nothing to gain by caching it. It is one file from a local
        # disk on a LAN, and its whole content changes on every deploy.
        # must-revalidate rather than no-store so a reload can still be a 304
        # if that is ever added.
        "cache-control": "no-cache, must-revalidate",
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


# THE PAGE'S OWN ADDRESSES: one per tab and everything under each. Each answers
# the page and nothing else; the tab reads its own location and opens what the
# path names. Which tabs a person may open is the gateway's route table
# (§3.4), and a navigation to one their role lacks never arrives here. Written
# out one decorator per path, not added in a loop, because services/gateway's
# tests read voice-ui's routes from literal decorators, and a route they cannot
# see is an allowlist entry they would report as pointing at nothing.
@app.get("/ui/transcribe", include_in_schema=False)
@app.get("/ui/transcribe/{rest:path}", include_in_schema=False)
@app.get("/ui/speak", include_in_schema=False)
@app.get("/ui/speak/{rest:path}", include_in_schema=False)
@app.get("/ui/jobs", include_in_schema=False)
@app.get("/ui/jobs/{rest:path}", include_in_schema=False)
@app.get("/ui/vocabulary", include_in_schema=False)
@app.get("/ui/vocabulary/{rest:path}", include_in_schema=False)
@app.get("/ui/satellites", include_in_schema=False)
@app.get("/ui/satellites/{rest:path}", include_in_schema=False)
@app.get("/ui/account", include_in_schema=False)
@app.get("/ui/account/{rest:path}", include_in_schema=False)
@app.get("/ui/admin", include_in_schema=False)
@app.get("/ui/admin/{rest:path}", include_in_schema=False)
async def page_at(request: Request, rest: str = "") -> Response:
    """The same document with the same headers; the path is the page's to read."""
    return await page(request)


@app.get("/ui/config", include_in_schema=False)
async def ui_config() -> Response:
    """The flags and limits the page draws itself with, and nothing else.

    Any signed-in session may read it, so it carries no address, nothing about
    who is asking and nothing per person: which features this deployment has,
    and the ceilings the page checks a file against before it sends one.
    Whether a link needs confirming, and whether it was probed, come back with
    each link from /ui/resolve.
    """
    return Response(media_type="application/json", content=json.dumps({
        "ingestion": downloads.available(),
        "cloning": clips.writable(),
        "max_upload_bytes": config.MAX_UPLOAD_BYTES,
        "max_clip_seconds": config.MAX_CLIP_SECONDS,
        # The seed only. The page keeps its own EMA in localStorage from the
        # realtime_factor every native transcription returns, because the
        # repository states three different rates for Parakeet -- 47-63x in the
        # root README, 8.5-10.4x in the gateway (which is what its 900 s
        # timeout and 504 text were built on) and about 5x in the stt README.
        # A measured number beats all three.
        "stt_rtf_seed": config.STT_RTF_SEED,
        "stt_budget_seconds": config.STT_BUDGET_SECONDS,
    }))


# ------------------------------------------------------------------ clips --


def _owner_filter(request: Request, owner: str | None) -> str:
    """`?owner=`, checked before it is used anywhere: "me" unless the caller may name others.

    "me", "all", "system" or a user ID (D32); anything else is a 400, so `../x`
    never reaches a path. Naming anyone but "me" needs voices:write:all (D35),
    and asking without it is refused rather than quietly answered with the
    caller's own clips, which would look like an empty store.
    """
    if owner is None or owner == "me":
        return "me"
    try:
        wanted = check_owner_filter(owner)
    except ValueError:
        raise ApiError(400, "owner must be me, all, system or a user ID",
                       code="invalid_owner", param="owner") from None
    if not identity.has(identity.claims_of(request), ALL_VOICES):
        raise insufficient_scope([ALL_VOICES])
    return wanted


def _namespace(request: Request, wanted: str) -> str | None:
    """The one namespace a checked filter names: the caller's own, the system's or a person's."""
    if wanted == "me":
        return clips.owner_of(identity.claims_of(request))
    return None if wanted == "system" else wanted


@app.get("/ui/clips", include_in_schema=False)
async def list_clips(request: Request, owner: str | None = None) -> Response:
    wanted = _owner_filter(request, owner)
    voices = clips.listing_all() if wanted == "all" else clips.listing(
        _namespace(request, wanted))
    return Response(media_type="application/json", content=json.dumps({
        "voices": voices, "writable": clips.writable(),
        "max_seconds": config.MAX_CLIP_SECONDS}))


@app.post("/ui/clips", include_in_schema=False)
async def add_clip(request: Request, name: str = Form(...),
                   replace: bool = Form(default=False),
                   file: UploadFile = File(...)) -> Response:
    """Save a clip into the caller's own namespace. Nobody saves into anyone else's (D35)."""
    # THE SAME CHECK THE GATEWAY MAKES ON AN AUDIO UPLOAD, and it was missing
    # here. The cap in clips.save is applied AFTER the body is in memory, so a
    # 200 MB POST grew peak RSS by about 800 MB before returning "that clip is
    # 200.0 MB" -- and compose.yaml gives this service mem_limit: 384m, so the
    # real outcome was an OOM kill, not the message. Declared length first, and
    # only then read; clips.save still bounds the undeclared case.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.MAX_CLIP_BYTES:
        return error_response(
            413,
            f"that clip is {int(declared) / 1024**2:.1f} MB; the ceiling is "
            f"{config.MAX_CLIP_BYTES / 1024**2:.0f} MB. Chatterbox wants ten "
            f"to thirty seconds of clean speech, which is about 1.4 MB.",
            code="clip_too_large", param="file")
    owner = clips.owner_of(identity.claims_of(request))
    data = await file.read()
    # The browser converts to WAV when it can decode the file at all. When it
    # cannot -- an ogg/opus voice note with no duration in its container is the
    # case that prompted this -- it uploads the original bytes instead, and the
    # suffix tells this route which of the two arrived.
    suffix = Path(file.filename or "").suffix.lower() or clips.SUFFIX
    try:
        saved = clips.save(name, data, owner=owner, replace=replace, suffix=suffix)
    except clips.ClipError as exc:
        return error_response(400, str(exc), code="invalid_clip", param="file")
    return Response(media_type="application/json", status_code=201,
                    content=json.dumps({"voice": saved,
                                        "voices": clips.listing(owner)}))


@app.delete("/ui/clips/{name}", include_in_schema=False)
async def delete_clip(request: Request, name: str,
                      owner: str | None = None) -> Response:
    wanted = _owner_filter(request, owner)
    if wanted == "all":
        return error_response(
            400, "a clip is deleted from one owner's voices: name them with "
                 "owner=system or a user ID",
            code="invalid_owner", param="owner")
    namespace = _namespace(request, wanted)
    try:
        gone = clips.remove(name, owner=namespace)
    except clips.ClipError as exc:
        return error_response(400, str(exc), code="invalid_clip", param="name")
    if not gone:
        return error_response(404, f"no voice called {name!r}",
                              code="unknown_voice", param="name")
    return Response(media_type="application/json",
                    content=json.dumps({"deleted": name,
                                        "voices": clips.listing(namespace)}))


# -------------------------------------------------------------- ingestion --


app.include_router(ingest.router)


# ----------------------------------------------------------------- health --


def _health() -> dict[str, object]:
    """This container's own state. Never the gateway's: the page reads that from the gateway.

    Open inside the compose network, like every backend's (D50), so it says
    which features are configured and nothing about who uses them. `status`
    is `not_ready` until the gateway has written this service's credential
    files, and any removed variable still set is named in `ignored_variables`
    -- both added by voice_common.health.
    """
    return {"ui": "ok",
            "features": {"ingestion": downloads.available(),
                         "cloning": clips.writable()},
            # What the cache holds, and the yt-dlp a failing link should be
            # checked against first. Read from package metadata: this process
            # never imports yt_dlp.
            "cache": downloads.stats(),
            "yt_dlp": _yt_dlp_version()}


def _yt_dlp_version() -> str | None:
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


install_health(app, _health)
