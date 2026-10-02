"""Everything the page reaches that is not the page server, the gateway or the
hub: stt-stack, tts-stack, tts-long, MeTube, and the satellites themselves.

One process, one event loop, one server per port:

    stt       stt-stack       transcriptions, /transcribe, glossaries, /health
    tts       tts-stack       /v1/audio/speech (audio or SSE), /speak, /voices
    long      tts-long        /jobs and their audio, /v1/audio/speech, /health
    metube    MeTube          /add /start /delete /history and the two file routes
    control   this harness    /__fake/... : the request log, failures, satellites

It is started by stack.py through launch.py, so it runs behind the same
loopback-only network guard as the real services.

WHY THE BACKENDS ARE FAKE AND THE GATEWAY IS NOT. The gateway's allowlist, its
/ui/api passthrough and its route-by-model are exactly the seams the page has
broken on before (a PUT, a DELETE, a PATCH that one table carried and the next
did not), so the browser reaches the real gateway, which reaches the real page
server and the real hub. Only the three model servers are replaced: they need
gigabytes of weights and a GPU, and what the page depends on is their wire
shape, which is copied here from services/stt, services/tts and
services/tts-long rather than invented.

EVERY REQUEST IS RECORDED, with its JSON body or its form fields, so a test can
assert what the page actually sent after the gateway and the page server had
their say. The browser-side log (conftest.py) records what left the page; this
one records what arrived.

THE BACKENDS BELIEVE ONLY THE GATEWAY, as the real ones do. stt, tts and
tts-long each install voice_common.identity for their audience, with the
public key the session's gateway wrote to its service volume, so a request
that did not come through the gateway is a 401 here too. Whose request it was
is recorded beside it (`identity`: sub, kind, cred, scopes), and the raw
assertion never is. What each one keeps is partitioned the way the real
service partitions it: tts-long's jobs by owner (D31, D32), stt's profiles by
namespace (D33, D34). The harness itself reads and changes them through the
control port, never by speaking to a backend.

THE SATELLITES ARE SCRIPTED, NOT MOCKED. A Korvo, a Raspberry Pi and a second
Korvo that nobody has adopted connect to the gateway's device socket, which
relays them to the real hub, say hello with the caps their firmware sends, are
adopted through the gateway with the harness's admin key, and then behave:
they send status on a clock, answer airplay_command, store earcons, take an
OTA image chunk by chunk, reboot and come back. So a control on the Satellites
tab goes all the way to a device and its answer comes all the way back, which
is what "the page updates without a refresh" has to be tested against.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import functools
import hashlib
import html
import io
import json
import math
import re
import struct
import time
import uuid
import wave
import zlib
from pathlib import Path
from urllib.parse import quote
from typing import Any

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile
from voice_common import identity
from voice_common.engines import CATALOGUE
from voice_common.errors import insufficient_scope, render
from voice_common.scopes import check_owner_filter, is_system_owner
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

# ---- sound -------------------------------------------------------------------------
#
# Small, deterministic and audible if anybody ever unmuted it: a vowel-like
# stack of harmonics under a syllable-rate envelope, so a waveform drawn by the
# page looks like speech rather than a flat line or a square wave.


@functools.lru_cache(maxsize=64)
def pcm(seconds: float, rate: int = 24000, channels: int = 1, pitch: float = 140.0) -> bytes:
    t = np.arange(max(1, int(seconds * rate))) / rate
    syllable = 0.55 + 0.45 * np.sin(2 * np.pi * 3.7 * t) ** 2
    voice = sum(np.sin(2 * np.pi * pitch * k * t) / k for k in (1, 2, 3, 5))
    mono = (np.clip(0.18 * syllable * voice, -1.0, 1.0) * 32767).astype("<i2")
    return np.repeat(mono, channels).tobytes()


@functools.lru_cache(maxsize=64)
def wav(seconds: float, rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm(seconds, rate, channels))
    return buf.getvalue()


def wav_seconds(data: bytes) -> float | None:
    start = data.find(b"RIFF")
    if start < 0:
        return None
    try:
        with wave.open(io.BytesIO(data[start:]), "rb") as w:
            return w.getnframes() / w.getframerate() if w.getframerate() else None
    except (wave.Error, EOFError):
        return None


def png(size: int = 96, hue: int = 0) -> bytes:
    """A small cover picture, drawn rather than shipped: a diagonal gradient
    whose colour moves with `hue`, so a track change changes the picture."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            v = (x + y) / (2 * size)
            row += bytes((int(60 + 160 * v + hue) % 256, int(40 + 120 * (1 - v)) % 256,
                          int(120 + hue * 2) % 256))
        rows.append(bytes(row))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def error(status: int, message: str, code: str | None = None, param: str | None = None,
          type_: str = "invalid_request_error") -> JSONResponse:
    """OpenAI's four-field envelope, which every service in this stack answers with."""
    return JSONResponse({"error": {"message": message, "type": type_, "param": param, "code": code}},
                        status_code=status)


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return parts or [text.strip() or "…"]


# ---- the world ---------------------------------------------------------------------


DEFAULT_TRANSCRIPT = ("Calliope is listening. The quick brown fox jumps over the lazy dog, "
                      "and the transcript follows the audio word by word. "
                      "This sentence exists so the karaoke has something to follow.")


class World:
    """Every piece of state the fakes share, and what the control API changes."""

    def __init__(self, repo: Path, svc: Path) -> None:
        self.repo = repo
        # Where the gateway writes each service's identity.pub (CALLIOPE_SVC_DIR).
        self.svc = svc
        self.seq = 0
        self.log: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []
        self.health_overrides: dict[str, dict[str, Any]] = {}
        self.transcript = DEFAULT_TRANSCRIPT
        self.jobs: dict[str, dict[str, Any]] = {}
        # (owner, name) -> a profile: the built-ins and the system's under
        # "system", a person's under their user ID (D33).
        self.glossaries: dict[tuple[str, str], dict[str, Any]] = {}
        self.gloss_writable = True
        self.gloss_reason: str | None = None
        self.gloss_strict = False
        self.metube: dict[str, dict[str, Any]] = {}
        self.satellites: dict[str, Satellite] = {}
        # Where the satellites connect (the gateway's device socket) and the
        # admin key they are adopted with, both told by the stack.
        self.gateway: str | None = None
        self.adopt_key: str | None = None
        # Whose the seeded history is: the admin the session signed in as, so
        # the Jobs tab, which lists the reader's own runs, opens on it.
        self.admin: str | None = None
        self.reset()

    def reset(self) -> None:
        self.log.clear()
        self.rules.clear()
        self.health_overrides.clear()
        self.transcript = DEFAULT_TRANSCRIPT
        self.jobs.clear()
        self.metube.clear()
        self.glossaries = {key: g for key, g in self.glossaries.items() if g["source"] == "builtin"}
        self.gloss_writable, self.gloss_reason, self.gloss_strict = True, None, False
        if not self.glossaries:
            for path in sorted((self.repo / "services/stt/glossaries").glob("*.txt")):
                self.glossaries[(SYSTEM, path.stem)] = glossary(path.stem, path.read_text("utf-8"),
                                                                "builtin", owner=SYSTEM)
        seed_jobs(self)

    def tick(self) -> int:
        """The log's clock, moved on when a request arrives and again when it
        is logged, so a test can tell a request that began after a moment
        from one that only finished after it (FakeControl.fresh_health)."""
        self.seq += 1
        return self.seq

    def record(self, entry: dict[str, Any]) -> None:
        entry["seq"] = self.tick()
        self.log.append(entry)
        del self.log[:-5000]

    def rule_for(self, backend: str, method: str, path: str) -> dict[str, Any] | None:
        for rule in self.rules:
            if rule.get("backend") not in (None, backend):
                continue
            if rule.get("method") not in (None, method):
                continue
            if not re.search(rule.get("path") or "", path):
                continue
            if rule.get("times") is not None:
                if rule["times"] <= 0:
                    continue
                rule["times"] -= 1
            return rule
        return None


# ---- recording and failure injection -----------------------------------------------


async def parsed_body(headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """What a test wants to assert about a body: its JSON, its form fields (a
    file as its name, type and size), or its size and nothing else."""
    kind = headers.get("content-type", "")
    out: dict[str, Any] = {"bytes": len(body)}
    if "json" in kind:
        with contextlib.suppress(ValueError):
            out["json"] = json.loads(body or b"null")
    elif "multipart/form-data" in kind or "x-www-form-urlencoded" in kind:
        scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"",
                 "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        form: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            async with Request(scope, receive).form() as fields:
                for key, value in fields.multi_items():
                    if isinstance(value, UploadFile):
                        size = len(await value.read())
                        value = {"filename": value.filename, "content_type": value.content_type,
                                 "bytes": size}
                    if key in form:
                        form[key] = (form[key] if isinstance(form[key], list) else [form[key]]) + [value]
                    else:
                        form[key] = value
        out["form"] = form
    elif kind.startswith("text/"):
        out["text"] = body[:65536].decode("utf-8", "replace")
    return out


# The services a wake word's action or a button reaches, outside the stack:
# Home Assistant, a language model server, a webhook receiver.
OUTSIDE = frozenset({"ha", "llm", "hook"})


class Observed:
    """ASGI middleware: log the request, and answer with an injected failure
    (or after an injected delay) before the fake itself is reached.

    The body is captured on its way to the app rather than read ahead of it,
    so a streamed upload still streams, and the app sees exactly the bytes it
    would have seen without this.

    WHO ASKED is logged as the claims the app verified, under `identity`, and
    None when it verified none. The assertion itself is not logged: an
    X-Calliope-* header is recorded only by name, under `calliope_headers`,
    and a cookie or an Authorization header only as the fact of one, so
    test_ownership.py can say that no person's credential reached a backend
    and that the gateway's assertion did (D51, D65). The services outside
    (OUTSIDE) are the exception for Authorization: what reaches them is a
    token the test made up for them, and the test reads it back.

    WHEN is logged twice on the World's clock: `began` as the request
    arrives, `seq` once it has been answered."""

    def __init__(self, app: Any, backend: str, world: World) -> None:
        self.app = app
        self.backend = backend
        self.world = world

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method, path = scope["method"], scope["path"]
        kept = ("content-type", "accept", "range") + (("authorization",) if self.backend in OUTSIDE else ())
        entry: dict[str, Any] = {
            "t": time.time(), "began": self.world.tick(), "backend": self.backend, "method": method,
            "path": path, "query": scope.get("query_string", b"").decode("latin-1"),
            "headers": {k: v for k, v in headers.items()
                        if k in kept or (k.startswith("x-") and not k.startswith("x-calliope-"))},
            "calliope_headers": sorted(k for k in headers if k.startswith("x-calliope-")),
            "cookie": "cookie" in headers,
            "authorization": "authorization" in headers,
            "identity": None}
        # The guard copies the scope but keeps this dict, so what it verified
        # is readable here once the app has answered.
        state = scope.setdefault("state", {})
        chunks: list[bytes] = []

        async def tapped() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "http.request" and sum(map(len, chunks)) < 8 * 1024 * 1024:
                chunks.append(message.get("body", b""))
            return message

        status: dict[str, int] = {}

        async def watched(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        rule = self.world.rule_for(self.backend, method, path)
        try:
            if rule and rule.get("delay"):
                await asyncio.sleep(float(rule["delay"]))
            if rule and rule.get("status"):
                while True:  # drain the upload, as a real server would before answering
                    message = await tapped()
                    if message["type"] != "http.request" or not message.get("more_body"):
                        break
                reply = rule.get("json")
                response = Response(
                    json.dumps(reply).encode() if reply is not None else (rule.get("body") or "").encode(),
                    status_code=int(rule["status"]),
                    media_type="application/json" if reply is not None else "text/plain",
                    headers=rule.get("headers") or {})
                entry["injected"] = True
                await response(scope, receive, watched)
                return
            # A RULE THAT ANSWERS NOTHING ITSELF is handed to the fake, which
            # may still act on it: speech() reads cut_after and the rule's
            # headers to break a stream half-way, as only the fake that is
            # writing the stream can.
            if rule:
                scope["fake.rule"] = rule
            await self.app(scope, tapped, watched)
        finally:
            body = b"".join(chunks)
            entry["status"] = status.get("code")
            claims = state.get(identity.STATE_CLAIMS)
            if isinstance(claims, identity.Claims):
                entry["identity"] = {"sub": claims.sub, "kind": claims.kind, "cred": claims.cred,
                                     "scopes": sorted(claims.scopes)}
            entry.update(await parsed_body(headers, body))
            self.world.record(entry)


def health_body(world: World, backend: str, body: dict[str, Any]) -> dict[str, Any]:
    """A backend's /health with whatever a test merged over it (null removes)."""
    merged = dict(body)
    for key, value in world.health_overrides.get(backend, {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# ---- stt-stack ---------------------------------------------------------------------

PARAKEET_LANGUAGES = ["bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hr", "hu", "it",
                      "lt", "lv", "mt", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "uk"]


# The namespaces, as services/stt/app/profiles.py names them (D33, D34): the
# built-ins and the deployment's own profiles are the system's, a person's are
# under their user ID, and `home-assistant` is the system's and reserved.
SYSTEM = "system"
RESERVED = "home-assistant"
RESERVED_SCOPES = ("glossaries:ha", "glossaries:read:all", "glossaries:write:all")


def glossary(name: str, text: str, source: str, strict: bool = False,
             owner: str = SYSTEM) -> dict[str, Any]:
    """services/stt/app/profiles.py's reading of a file, at the depth the page
    sees: `heard = intended` is a replacement, a bare term a hotword, and a
    replacement missing either side is refused with its line number.

    `strict` is the service's single-word rule for a PUT without force: a
    one-word left-hand side (`belly = Belli`) is refused, and its reason
    says "send force", which is the only thing the page reads to offer Save
    anyway. It is a switch rather than the default because the other files
    write `alpha = Alpha` as shorthand for any profile; the built-ins never
    get it, as the service reads a file already on disk with force."""
    replacements: dict[str, str] = {}
    hotwords: list[str] = []
    rejected: list[dict[str, Any]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            heard, _, intended = (part.strip() for part in line.partition("="))
            if not heard or not intended:
                rejected.append({"line": number, "text": raw,
                                 "reason": "a replacement needs text on both sides of '='"})
                continue
            if strict and len(heard.split()) == 1:
                rejected.append({"line": number, "text": line, "reason": (
                    f"{heard.lower()!r} is a single word, so this rule would rewrite any sentence "
                    f"that says it correctly. Use the bare form ({intended!r} on a line of its own) "
                    "to bias the decoder without rewriting, or send force to accept it.")})
                continue
            replacements[heard] = intended
        else:
            hotwords.append(line)
    return {"name": name, "owner": owner, "source": source, "text": text,
            "replacements": replacements, "hotwords": hotwords, "rejected": rejected,
            "writable": source != "builtin"}


def glossary_summary(g: dict[str, Any]) -> dict[str, Any]:
    # No path, as the service gives none: it would name the volume's layout
    # and the directory a person's ID is in.
    return {"name": g["name"], "owner": g["owner"], "source": g["source"],
            "terms": len(g["replacements"]) + len(g["hotwords"]),
            "replacements": len(g["replacements"]), "hotwords": len(g["hotwords"]),
            "writable": g["writable"]}


GLOSS_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def claims(request: Request) -> identity.Claims:
    return identity.claims_of(request)


def own_namespace(who: identity.Claims) -> str:
    """profiles.view_of: a service and a holder of glossaries:read:all name the
    system's profiles; anyone else names their own."""
    if who.kind == "service" or identity.has(who, "glossaries:read:all"):
        return SYSTEM
    return who.sub


def sees_reserved(who: identity.Claims) -> bool:
    return any(identity.has(who, scope) for scope in RESERVED_SCOPES)


def visible(world: World, namespace: str, reserved: bool) -> dict[str, dict[str, Any]]:
    """Registry.visible: the namespace, the built-ins, and `home-assistant`
    from the system's only when the view may see it."""
    found = {name: g for (owner, name), g in world.glossaries.items()
             if owner == namespace and g["source"] != "builtin"}
    system = world.glossaries.get((SYSTEM, RESERVED))
    if reserved and system is not None:
        found[RESERVED] = system
    found.update({name: g for (_, name), g in world.glossaries.items() if g["source"] == "builtin"})
    if not reserved:
        found.pop(RESERVED, None)
    return found


def owner_param(request: Request) -> str | None | Response:
    """?owner=, checked before it is used anywhere (D32); a Response is the 400."""
    raw = request.query_params.get("owner")
    if raw is None:
        return None
    try:
        return check_owner_filter(raw)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


def gloss_view(request: Request, name: str | None, *, write: bool) -> tuple[str, bool] | Response:
    """main._view in services/stt: which namespace one route acts on for this
    caller, and whether `home-assistant` is in it; or the refusal."""
    who = claims(request)
    owner = owner_param(request)
    if isinstance(owner, Response):
        return owner
    wide = "glossaries:write:all" if write else "glossaries:read:all"
    if name is not None and name.strip().lower() == RESERVED:
        if not (identity.has(who, "glossaries:ha") or identity.has(who, wide)):
            return render(insufficient_scope(["glossaries:ha", wide]))
        if owner not in (None, SYSTEM):
            return JSONResponse({"detail": f"{RESERVED!r} is reserved and always the system's "
                                           "profile; send no owner"}, status_code=400)
        return SYSTEM, True
    own = who.sub if who.kind == "user" else None
    if owner is None:
        namespace = own_namespace(who)
    elif owner == "me":
        if own is None:
            return JSONResponse({"detail": "owner=me names a user's profiles, and this caller is "
                                           "a service"}, status_code=400)
        namespace = own
    elif owner == "all":
        return JSONResponse({"detail": "owner=all lists every profile; it names no single one"},
                            status_code=400)
    else:
        namespace = owner
    if namespace != own and not identity.has(who, wide):
        return render(insufficient_scope([wide]))
    return namespace, sees_reserved(who) and namespace in (SYSTEM, own)


def timed(text: str, seconds: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Segments and words spread evenly over the audio, as Parakeet's would be."""
    parts = sentences(text)
    words_total = max(1, sum(len(p.split()) for p in parts))
    per_word = max(0.05, seconds / words_total)
    segments, words, clock = [], [], 0.0
    for index, part in enumerate(parts):
        start = clock
        for word in part.split():
            words.append({"word": word, "start": round(clock, 2), "end": round(clock + per_word * 0.9, 2)})
            clock += per_word
        segments.append({"id": index, "seek": 0, "start": round(start, 2), "end": round(clock, 2),
                         "text": part, "tokens": [], "temperature": 0.0, "avg_logprob": -0.12,
                         "compression_ratio": 1.4, "no_speech_prob": 0.01})
    return segments, words


def subtitles(segments: list[dict[str, Any]], vtt: bool) -> str:
    def stamp(s: float) -> str:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        mark = "." if vtt else ","
        return f"{int(h):02d}:{int(m):02d}:{int(sec):02d}{mark}{int(round((sec % 1) * 1000)):03d}"

    lines = ["WEBVTT", ""] if vtt else []
    for i, seg in enumerate(segments, 1):
        if not vtt:
            lines.append(str(i))
        lines += [f"{stamp(seg['start'])} --> {stamp(seg['end'])}", seg["text"], ""]
    return "\n".join(lines)


class UnknownProfile(LookupError):
    """services/stt/app/profiles.UnknownProfile: a name the caller's view does
    not hold, with the names it does."""

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name, self.known = name, known


def repair(world: World, who: identity.Claims, chosen: Any, text: str) -> tuple[str, list[str]]:
    """services/stt/app/glossary.apply over the profiles a request named in its
    `glossary` field, as the caller's view resolves them: each `heard =
    intended` rewrites whole words whatever their case, and a rule is listed
    as fired only when it changed the text. Both routes run it; /transcribe
    returns the list as `repaired`, and /v1 sends it as the
    `x-glossary-repaired` header, percent-encoded, as the service does.

    A NAME OUTSIDE THE VIEW IS REFUSED, as the service refuses it, before
    anything is applied (UnknownProfile): another person's profile, or
    `home-assistant` without its scope, is the same "unknown profile" as one
    that does not exist (D33). Ignoring it here would let a page that sends
    one pass a test and fail at home."""
    view = visible(world, own_namespace(who), sees_reserved(who))
    wanted = [name.strip().lower() for name in str(chosen or "").split(",") if name.strip()]
    for name in wanted:
        if name not in view:
            raise UnknownProfile(name, sorted(view))
    fired: list[str] = []
    for name in wanted:
        for heard, intended in view[name]["replacements"].items():
            changed = re.sub(rf"\b{re.escape(heard)}\b", lambda _: intended, text,
                             flags=re.IGNORECASE)
            if changed != text:
                fired.append(intended)
            text = changed
    return text, fired


def stt_health(world: World) -> dict[str, Any]:
    # The system's names and the built-ins, never a person's (D50).
    shared = sorted({name for (owner, name) in world.glossaries if owner == SYSTEM})
    return health_body(world, "stt", {
        "status": "ok", "model": "parakeet",
        "models": [{"id": "parakeet", "family": "parakeet", "default": True,
                    "languages": PARAKEET_LANGUAGES, "accepts_language": False,
                    "accepts_boost": True, "can_translate": False, "can_stream": False}],
        "model_id": "nvidia/parakeet-tdt-0.6b-v3", "accepts_vocabulary": True,
        "translations": False, "streaming": False, "hotwords": True,
        "glossaries": shared, "vad": True, "threads": 4,
        "max_concurrent": 1, "host_label": "e2e-fake"})


def stt_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)
    identity.install(app, "stt", credentials=identity.Credentials(world.svc / "stt"))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return stt_health(world)

    async def heard(request: Request) -> tuple[dict[str, Any], float]:
        form = await request.form()
        fields: dict[str, Any] = {}
        seconds = None
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                seconds = wav_seconds(await value.read())
            else:
                fields.setdefault(key, []).append(value)
        return {k: v[0] if len(v) == 1 else v for k, v in fields.items()}, seconds or 6.0

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request) -> Response:
        fields, seconds = await heard(request)
        fmt = fields.get("response_format", "json")
        try:
            text, fired = repair(world, claims(request), fields.get("glossary"), world.transcript)
        except UnknownProfile as unknown:
            return error(400, f"Unknown glossary profile {unknown.name!r}. You can use: "
                              f"{', '.join(unknown.known) or 'none'}. See GET /glossaries.",
                         code="invalid_value", param="glossary")
        segments, words = timed(text, seconds)
        headers = {"x-stt-engine": "parakeet-tdt-0.6b-v3", "x-realtime-factor": "8.8",
                   "x-audio-seconds": f"{seconds:.2f}"}
        if fired:
            headers["x-glossary-repaired"] = ", ".join(quote(term, safe=" ") for term in fired)
        if fmt == "text":
            return PlainTextResponse(text, headers=headers)
        if fmt in ("srt", "vtt"):
            return PlainTextResponse(subtitles(segments, fmt == "vtt"), headers=headers,
                                     media_type="text/vtt" if fmt == "vtt" else "application/x-subrip")
        if fmt == "verbose_json":
            grains = fields.get("timestamp_granularities[]") or ["segment"]
            grains = [grains] if isinstance(grains, str) else grains
            body: dict[str, Any] = {"task": "transcribe", "language": "english", "duration": seconds,
                                    "text": text, "segments": segments}
            if "word" in grains:
                body["words"] = words
            if "segment" not in grains:
                body.pop("segments")
            return JSONResponse(body, headers=headers)
        return JSONResponse({"text": text, "usage": {"type": "duration", "seconds": math.ceil(seconds)}},
                            headers=headers)

    @app.post("/v1/audio/translations")
    async def translations(request: Request) -> Response:
        await heard(request)
        return error(400, "parakeet cannot translate: it has no translation task. Run stt-stack "
                          "with STT_MODEL=whisper to translate.", code="unsupported_task", param="model")

    @app.post("/transcribe")
    async def transcribe(request: Request) -> Any:
        fields, seconds = await heard(request)
        try:
            text, fired = repair(world, claims(request), fields.get("glossary"), world.transcript)
        except UnknownProfile as unknown:
            # The native route keeps its {"detail": ...} body, as the service's does.
            return JSONResponse({"detail": f"unknown glossary profile {unknown.name!r}; you can use: "
                                           f"{', '.join(unknown.known) or 'none'}"}, status_code=400)
        return {"text": text, "raw": world.transcript.lower(), "repaired": fired,
                "model": "parakeet", "audio_seconds": round(seconds, 2),
                "speech_seconds": round(seconds * 0.86, 2), "compute_seconds": round(seconds / 8.8, 2),
                "realtime_factor": 8.8}

    @app.get("/glossaries")
    async def list_glossaries(request: Request) -> Response:
        owner = owner_param(request)
        if isinstance(owner, Response):
            return owner
        if owner == "all":
            # Every namespace at once, each entry naming its owner: the
            # built-ins, the system's, then each person's.
            if not identity.has(claims(request), "glossaries:read:all"):
                return render(insufficient_scope(["glossaries:read:all"]))
            listed = sorted(world.glossaries.values(),
                            key=lambda g: (g["source"] != "builtin", g["owner"] != SYSTEM,
                                           g["owner"], g["name"]))
        else:
            view = gloss_view(request, None, write=False)
            if isinstance(view, Response):
                return view
            shown = visible(world, *view)
            listed = [shown[name] for name in sorted(shown)]
        body = {"glossaries": [glossary_summary(g) for g in listed],
                "writable": world.gloss_writable, "default": []}
        # As the service: `reason` only when it cannot write, and only if it
        # has one (fake.glossaries(reason=None) is a server that gave none).
        if not world.gloss_writable and world.gloss_reason:
            body["reason"] = world.gloss_reason
        return JSONResponse(body)

    def read_only() -> Response | None:
        """The service's 503 for a write on a deployment with no volume."""
        if world.gloss_writable:
            return None
        return JSONResponse({"detail": "glossary profiles are read-only here: "
                                       + (world.gloss_reason or "no custom glossary directory")},
                            status_code=503)

    @app.get("/glossaries/{name}")
    async def get_glossary(name: str, request: Request) -> Response:
        view = gloss_view(request, name, write=False)
        if isinstance(view, Response):
            return view
        g = visible(world, *view).get(name.strip().lower())
        if g is None:
            return JSONResponse({"detail": f"no glossary profile named {name!r}"}, status_code=404)
        return JSONResponse(glossary_summary(g) | {"replacements": g["replacements"],
                                                   "hotwords": g["hotwords"], "text": g["text"]})

    @app.put("/glossaries/{name}")
    async def put_glossary(name: str, request: Request) -> Response:
        view = gloss_view(request, name, write=True)
        if isinstance(view, Response):
            return view
        namespace = view[0]
        raw = await request.body()
        force = request.query_params.get("force") in ("1", "true")
        if "json" in request.headers.get("content-type", ""):
            payload = json.loads(raw or b"{}")
            text, force = str(payload.get("text") or ""), bool(payload.get("force", force))
        else:
            text = raw.decode("utf-8", "replace")
        name = name.strip().lower()
        if not GLOSS_NAME.match(name):
            return JSONResponse({"detail": f"{name!r} is not a usable profile name"}, status_code=400)
        # A built-in is everyone's, so nobody's own profile may hide it.
        builtin = world.glossaries.get((SYSTEM, name))
        if builtin is not None and builtin["source"] == "builtin":
            existing = builtin
        else:
            existing = world.glossaries.get((namespace, name))
        if existing is not None and existing["source"] == "builtin":
            return JSONResponse({"detail": f"{name!r} is built in and cannot be written. Copy it to a "
                                           "new name and edit that."}, status_code=409)
        if (refused := read_only()) is not None:
            return refused
        if len(text.encode()) > 65536:
            return JSONResponse({"detail": "that profile is over 64 KB"}, status_code=413)
        g = glossary(name, text, "custom", strict=world.gloss_strict and not force, owner=namespace)
        if g["rejected"] and not force:
            accepted = len(g["replacements"]) + len(g["hotwords"])
            return JSONResponse({"detail": {
                "message": f"{len(g['rejected'])} line(s) rejected; nothing was written. "
                           f"{accepted} term(s) would have been accepted.",
                "accepted": accepted, "rejected": g["rejected"]}}, status_code=400)
        world.glossaries[(namespace, name)] = g
        return JSONResponse(glossary_summary(g) | {"forced": force, "created": existing is None},
                            status_code=200 if existing else 201)

    @app.delete("/glossaries/{name}")
    async def delete_glossary(name: str, request: Request) -> Response:
        view = gloss_view(request, name, write=True)
        if isinstance(view, Response):
            return view
        g = visible(world, *view).get(name.strip().lower())
        if g is None:
            return JSONResponse({"detail": f"no glossary profile named {name!r}"}, status_code=404)
        if g["source"] == "builtin":
            return JSONResponse({"detail": f"{name!r} is built in and cannot be deleted"}, status_code=409)
        if (refused := read_only()) is not None:
            return refused
        del world.glossaries[(g["owner"], g["name"])]
        return JSONResponse({"name": g["name"], "deleted": True})

    return app


# ---- tts-stack ---------------------------------------------------------------------

KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis", "ef_dora", "em_alex", "em_santa", "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi", "if_sara", "im_nicola", "jf_alpha", "jf_gongitsune",
    "jm_kumo", "pf_dora", "pm_alex", "pm_santa", "zf_xiaobei", "zf_xiaoni", "zm_yunjian", "zm_yunxi"]
VOICE_ALIASES = {"alloy": "af_alloy", "ash": "am_fenrir", "ballad": "bm_lewis", "coral": "af_sarah",
                 "echo": "am_echo", "fable": "bm_fable", "onyx": "am_onyx", "nova": "af_nova",
                 "sage": "af_heart", "shimmer": "af_bella"}
MEDIA_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac",
               "wav": "audio/wav", "pcm": "audio/pcm"}
CHARS_PER_SECOND = 15.0


def spoken_seconds(text: str) -> float:
    return round(min(20.0, max(0.8, len(text) / CHARS_PER_SECOND)), 1)


def audio_answer(fmt: str, seconds: float, rtf: float, extra: dict[str, str] | None = None) -> Response:
    """The audio in the format asked for. Only pcm and wav are real; mp3, opus,
    aac and flac are answered with WAV bytes under their own content type,
    which every browser sniffs and plays -- an encoder here would be a
    dependency for a difference no test looks at."""
    body = pcm(seconds) if fmt == "pcm" else wav(seconds)
    headers = {"X-Audio-Seconds": f"{seconds:.2f}", "X-Compute-Seconds": f"{seconds / rtf:.2f}",
               "X-Realtime-Factor": f"{rtf:.1f}"} | (extra or {})
    return Response(body, media_type=MEDIA_TYPES.get(fmt, "audio/wav"), headers=headers)


async def speech(world: World, request: Request, rtf: float) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return error(400, "the body is not JSON")
    text = str(body.get("input") or "")
    if not text.strip():
        return error(400, "input must not be empty", param="input")
    fmt = body.get("response_format") or "mp3"
    if fmt not in MEDIA_TYPES:
        return error(400, f"response_format {fmt!r} is not one of {', '.join(MEDIA_TYPES)}",
                     param="response_format")
    seconds = round(spoken_seconds(text) / float(body.get("speed") or 1.0), 1)
    if body.get("stream_format") == "sse":
        # A STREAM THAT BREAKS HALF-WAY, on a fake.fail(..., status=None,
        # cut_after=n) rule: n deltas, then the in-band error frame
        # services/tts writes when synthesis fails after the 200 has gone
        # (_sse_body), which is the only way a failure can reach a client
        # through the gateway -- a connection dropped under it is relayed as
        # a stream that simply ended. The rule's headers go on the response,
        # so a test can give it tts-long's X-Job-Id.
        rule = request.scope.get("fake.rule") or {}
        cut = rule.get("cut_after")

        async def frames():
            data = pcm(seconds) if fmt == "pcm" else wav(seconds)
            step = 24000  # half a second of 24 kHz s16le mono per delta
            for n, i in enumerate(range(0, len(data), step)):
                if cut is not None and n >= int(cut):
                    failed = {"error": {"message": "synthesis failed: the fake was told to stop here",
                                        "type": "server_error", "param": None,
                                        "code": "synthesis_failed"}}
                    yield f"data: {json.dumps(failed)}\n\n"
                    return
                chunk = base64.b64encode(data[i:i + step]).decode()
                yield f"data: {json.dumps({'type': 'speech.audio.delta', 'audio': chunk})}\n\n"
                await asyncio.sleep(0.12)
            done = {"type": "speech.audio.done",
                    "usage": {"input_tokens": len(text), "output_tokens": len(data) // 2,
                              "total_tokens": len(text) + len(data) // 2}}
            yield f"data: {json.dumps(done)}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"} | (rule.get("headers") or {}))
    return audio_answer(fmt, seconds, rtf)


def tts_health(world: World) -> dict[str, Any]:
    return health_body(world, "tts", {
        "status": "ok", "voices": len(KOKORO_VOICES), "default_voice": "af_heart", "threads": 4,
        "host_label": "e2e-fake", "runlog": {"written": 0, "dropped": 0, "last_error": None},
        "realtime_factor": 2.79, "realtime_factor_samples": 12})


def tts_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)
    identity.install(app, "tts", credentials=identity.Credentials(world.svc / "tts"))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return tts_health(world)

    @app.get("/voices")
    async def voices() -> dict[str, Any]:
        return {"voices": KOKORO_VOICES,
                "pt_br": [v for v in KOKORO_VOICES if v.startswith(("pf_", "pm_"))],
                "en_us": [v for v in KOKORO_VOICES if v.startswith(("af_", "am_"))],
                "en_gb": [v for v in KOKORO_VOICES if v.startswith(("bf_", "bm_"))],
                "openai_aliases": VOICE_ALIASES}

    @app.post("/v1/audio/speech")
    async def v1_speech(request: Request) -> Response:
        return await speech(world, request, 2.79)

    @app.post("/speak")
    async def speak(request: Request) -> Response:
        body = await request.json()
        parts = body.get("segments")
        if parts:
            texts = [str(p.get("text") if isinstance(p, dict) else p) for p in parts]
        else:
            texts = [str(body.get("text") or "")]
        if not any(t.strip() for t in texts):
            return JSONResponse({"detail": "provide text or segments"}, status_code=422)
        offsets, clock = [], 0.0
        for t in texts:
            offsets.append(round(clock, 3))
            clock += spoken_seconds(t)
        return audio_answer(body.get("format") or "wav", clock, 2.79,
                            {"X-Segment-Offsets": ",".join(str(o) for o in offsets)})

    return app


# ---- tts-long ----------------------------------------------------------------------

SEED_TEXT = ("It was the best of times, it was the worst of times. The chapter is read aloud "
             "in a cloned voice, one segment at a time, and the Jobs tab follows it.")
# What a satellite heard: a run of the hub's, which no person's Jobs tab lists.
SATELLITE_TEXT = "Hey Jarvis, turn on the kitchen lights."


# Whose a satellite's run is: the hub's, never a person's (D31).
HUB = "svc:satellites"


def seed_jobs(world: World) -> None:
    """A history the Jobs tab can draw at once: one of each audio state, a
    failure, and the two kinds that keep no audio here, all the admin's; and
    a satellite's transcription, which is the hub's and so a system record
    that no person's own listing shows."""
    now = time.time()
    mine = {"owner": world.admin, "credential": "session"}
    rows = [
        {"kind": "clone", "status": "done", "age": 7200, "audio_seconds": 42.4, "voice": "narrator"} | mine,
        {"kind": "clone", "status": "done", "age": 86400, "audio_seconds": 18.1, "voice": "narrator",
         "audio_deleted": True} | mine,
        {"kind": "clone", "status": "failed", "age": 10800, "voice": "narrator",
         "error": "the runner went away in the middle of segment 3"} | mine,
        {"kind": "speech", "status": "done", "age": 3600, "audio_seconds": 6.2, "voice": "af_heart",
         "engine": "kokoro", "service": "tts-stack"} | mine,
        {"kind": "transcribe", "status": "done", "age": 1800, "audio_seconds": 312.0, "voice": None,
         "engine": "parakeet", "service": "stt-stack"} | mine,
        {"kind": "transcribe", "status": "done", "age": 900, "audio_seconds": 2.4, "voice": None,
         "engine": "parakeet", "service": "stt-stack", "route": "/v1/audio/transcriptions",
         "text": SATELLITE_TEXT, "owner": HUB, "credential": HUB},
    ]
    for row in rows:
        created = now - row.pop("age")
        job = new_job(world, text=row.pop("text", SEED_TEXT), voice=row.pop("voice"),
                      engine=row.pop("engine", "chatterbox"), created=created, scripted=False,
                      owner=row.pop("owner"), credential=row.pop("credential"))
        job.update(row)
        seconds = job.get("audio_seconds") or 0
        job.update(started_at=created + 1, finished_at=created + 1 + seconds / 0.7,
                   compute_seconds=round(seconds / 0.7, 1),
                   realtime_factor=0.7 if seconds else None, speech_seconds=seconds,
                   offsets=[round(seconds * i / job["chunks"], 2) for i in range(job["chunks"])]
                   if job["status"] == "done" else [])
        if job["status"] == "done" and job["kind"] == "clone" and not job.get("audio_deleted"):
            job["path"] = f"/out/{job['id']}.wav"
            job["bytes"] = int(seconds * 48000)


def new_job(world: World, *, text: str, voice: str | None, engine: str, created: float | None = None,
            scripted: bool = True, language: str | None = "en", owner: str | None = None,
            credential: str | None = None) -> dict[str, Any]:
    parts = sentences(text)
    job_id = str(uuid.uuid4())
    kind = "clone" if engine in CATALOGUE else ("transcribe" if engine == "parakeet" else "speech")
    # WHOSE IT IS, as tts-long writes it (D31): absent is system.
    whose = {k: v for k, v in (("owner", owner), ("credential", credential)) if v is not None}
    job = whose | {"id": job_id, "status": "queued", "created_at": created or time.time(), "kind": kind,
           "service": "tts-long", "engine": engine, "engine_reason": "default", "host": "e2e-fake",
           "route": "/jobs", "language": language, "voice": voice, "format": "wav",
           "sample_rate": 24000, "chunks": len(parts), "chars": len(text), "text": text,
           "segments": parts, "offsets": [], "backend": "runner", "runner_host": "e2e-gpu",
           "runner_service": "chatterbox-runner", "queued_ahead": 0,
           "estimated_seconds": round(len(parts) * 1.2 + 1.0)}
    if scripted:
        # The clock a new job runs to: queued for a second, then one segment
        # every 1.2 s, so a test can watch a row go queued -> running -> done
        # (and the page redraw it) inside its 60 s budget.
        job["_script"] = {"start": job["created_at"] + 1.0, "per": 1.2,
                          "seconds": [spoken_seconds(p) for p in parts]}
    world.jobs[job_id] = job
    return job


def advance(job: dict[str, Any]) -> None:
    """Move a scripted job along its clock, as the worker thread would have."""
    script = job.get("_script")
    if not script or job["status"] not in ("queued", "running") or job.get("cancelled"):
        return
    now = time.time()
    if now < script["start"]:
        return
    job.setdefault("started_at", script["start"])
    done = min(len(script["seconds"]), int((now - script["start"]) / script["per"]))
    job["status"] = "running"
    job["offsets"] = [round(sum(script["seconds"][:i]), 2) for i in range(done)]
    if done >= len(script["seconds"]):
        seconds = sum(script["seconds"])
        job.update(status="done", finished_at=script["start"] + done * script["per"],
                   audio_seconds=round(seconds, 2), speech_seconds=round(seconds, 2),
                   compute_seconds=round(done * script["per"], 2),
                   realtime_factor=round(seconds / (done * script["per"]), 2),
                   path=f"/out/{job['id']}.wav", bytes=int(seconds * 48000))


def audio_state(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("audio_deleted"):
        state = "deleted"
    elif job.get("audio_expired"):
        state = "expired"
    elif job.get("path"):
        state = "present"
    elif job["status"] in ("queued", "running"):
        state = "pending"
    elif job["kind"] != "clone":
        state = "never"
    else:
        state = "expired"
    if state != "present":
        return {"state": state}
    return {"state": "present", "format": job["format"], "bytes": job.get("bytes") or 0,
            "url": f"/jobs/{job['id']}/audio"}


def public(job: dict[str, Any]) -> dict[str, Any]:
    advance(job)
    out = {k: v for k, v in job.items() if k not in ("segments", "text", "_script")}
    out["audio"] = audio_state(job)
    source = " ".join((job.get("text") or "").split())
    if source:
        out["text_preview"] = source[:140] + ("…" if len(source) > 140 else "")
        out["text_length"] = len(source)
    return out


def engine_rows() -> dict[str, dict[str, Any]]:
    rows = {}
    for engine, facts in CATALOGUE.items():
        if facts.owned_by != "tts-long":
            continue
        local = engine != "voxtral"
        rows[engine] = {
            "label": facts.label, "default": engine == "chatterbox", "languages": list(facts.languages),
            "controls": sorted(facts.controls), "min_reference_seconds": facts.min_reference_seconds,
            "cold_load_seconds": facts.cold_load_seconds, "reference_audio": facts.reference_audio,
            "language_from_voice": facts.language_from_voice,
            "voices": ([{"name": v.name, "language": v.language} for v in facts.voices]
                       if facts.voices is not None else None),
            "native_sample_rate": facts.native_sample_rate,
            "local": {"ready": local, "why": "" if local else "not in TTS_LOCAL_ENGINES",
                      "resident": engine == "chatterbox"},
            "runner": {"ready": True, "why": "", "service": facts.runner_service, "settings": None}}
    return rows


def owned_by(raw: str | None, who: identity.Claims, everyone: str) -> Any:
    """tts-long's _owned_by: which jobs `?owner=` asks for, as a test on one
    job, or the refusal as a Response (D32). Absent means `me`, admins
    included; anybody else's needs `everyone`, the route's `:all` scope."""
    try:
        value = check_owner_filter(raw or "me")
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if value in ("me", who.sub):
        return lambda job: job.get("owner") == who.sub
    if not identity.has(who, everyone):
        return render(insufficient_scope([everyone]))
    if value == "all":
        return lambda job: True
    if value == "system":
        return lambda job: is_system_owner(job.get("owner"))
    return lambda job: job.get("owner") == value


def listing(world: World, q: Any, covers: Any = lambda job: True) -> dict[str, Any]:
    """GET /jobs's answer over the jobs `covers` admits: the owner filter
    first of all, then the counts, then the other filters and the limit, as
    tts-long orders them, so a cap never falls on somebody else's rows."""
    def wanted(name: str) -> set[str] | None:
        raw = q.get(name)
        return {v.strip() for v in raw.split(",") if v.strip()} if raw else None

    kinds, audio, statuses = wanted("kind"), wanted("audio"), wanted("status")
    if statuses and "live" in statuses:
        statuses = (statuses - {"live"}) | {"queued", "running"}
    counts = {k: 0 for k in ("all", "clone", "speech", "transcribe", "present", "deleted", "expired",
                             "never", "pending", "failed", "cancelled", "live")}
    keep = []
    for job in sorted(world.jobs.values(), key=lambda j: -j["created_at"]):
        if not covers(job):
            continue
        shape = public(job)
        live = shape["status"] in ("queued", "running")
        counts["all"] += 1
        counts[shape["kind"]] += 1
        counts[shape["audio"]["state"]] += 1
        if live:
            counts["live"] += 1
        elif shape["status"] in ("failed", "cancelled"):
            counts[shape["status"]] += 1
        if kinds and shape["kind"] not in kinds:
            continue
        if statuses and shape["status"] not in statuses and not live:
            continue
        if audio and shape["audio"]["state"] not in audio and not (live or shape["status"] == "failed"):
            continue
        keep.append(shape)
    limit = max(1, min(int(q.get("limit") or 50), 200))
    return {"jobs": keep[:limit], "counts": counts, "truncated": len(keep) > limit}


def long_health(world: World) -> dict[str, Any]:
    live = [j for j in world.jobs.values() if public(j)["status"] in ("queued", "running")]
    rows = engine_rows()
    return health_body(world, "tts_long", {
        "status": "ok", "model_loaded": True, "threads": 4,
        "queued": sum(1 for j in live if j["status"] == "queued"), "queue_capacity": 32,
        "running": sum(1 for j in live if j["status"] == "running"), "realtime_factor": 0.46,
        "realtime_factor_by_backend": {"local": 0.23, "runner": 0.7},
        "backend_observations": {"local": 3, "runner": 17},
        "realtime_factor_by_engine": {f"{lane}/{e}": rate for e in rows
                                      for lane, rate in (("local", 0.23), ("runner", 0.7))},
        "engine_observations": {f"{lane}/{e}": 5 for e in rows for lane in ("local", "runner")},
        "engines": rows, "default_engine": "chatterbox", "backend_order": ["runner", "local"],
        "runner": {"reachable": True, "can_run": True, "state": "idle", "machine_state": "idle",
                   "mode": "auto", "job_running": False, "seconds_until_available": 0,
                   "error": None,
                   "gpu": {"name": "NVIDIA GeForce RTX 4070", "util_gpu": 3, "mem_used_mib": 2150,
                           "mem_total_mib": 12282, "temperature_c": 41},
                   "services": [{"id": "chatterbox-runner", "running": True}]},
        "dispatch": {"runner": {"open": True, "why": ""}, "local": {"open": True, "why": ""}},
        "host_label": "e2e-fake"})


def long_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)
    identity.install(app, "tts-long", credentials=identity.Credentials(world.svc / "tts-long"))

    def job_for(request: Request, job_id: str, everyone: str) -> dict[str, Any] | Response:
        """tts-long's _job_for: one job, if the listing with the same `?owner=`
        would show it; 404 otherwise, as if it did not exist."""
        covers = owned_by(request.query_params.get("owner"), claims(request), everyone)
        if isinstance(covers, Response):
            return covers
        job = world.jobs.get(job_id)
        if job is None or not covers(job):
            return JSONResponse({"detail": "no such job"}, status_code=404)
        return job

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return long_health(world)

    @app.post("/jobs", status_code=202)
    async def create_job(request: Request) -> Response:
        body = await request.json()
        text = str(body.get("text") or "")
        if body.get("segments"):
            # EACH SEGMENT AS voice_common.models.Segment READS IT: `text` is
            # required, `pause_after` optional, and nothing else is allowed. This
            # took a segment without text and spoke the word "None", so a page
            # that sent one was answered with a job where tts-long answers 422.
            wrong = []
            for i, s in enumerate(body["segments"]):
                s = s if isinstance(s, dict) else {}
                if not isinstance(s.get("text"), str):
                    wrong.append({"type": "missing", "loc": ["body", "segments", i, "text"],
                                  "msg": "Field required"})
                wrong += [{"type": "extra_forbidden", "loc": ["body", "segments", i, key],
                           "msg": "Extra inputs are not permitted"} for key in set(s) - {"text", "pause_after"}]
            if wrong:
                return JSONResponse({"detail": wrong}, status_code=422)
            text = " ".join(s["text"] for s in body["segments"])
        if not text.strip():
            return JSONResponse({"detail": "provide either text or segments"}, status_code=400)
        engine = (body.get("model") or "chatterbox").strip().lower()
        if engine in ("tts-long", ""):
            engine = "chatterbox"
        who = claims(request)
        job = new_job(world, text=text, voice=body.get("voice"), engine=engine,
                      language=body.get("language"), owner=who.sub, credential=who.cred)
        return JSONResponse({"id": job["id"], "status": "queued", "queued_ahead": 0,
                             "chunks": job["chunks"], "engine": engine,
                             "estimated_seconds": job["estimated_seconds"]}, status_code=202)

    @app.get("/jobs")
    async def list_jobs(request: Request) -> Response:
        covers = owned_by(request.query_params.get("owner"), claims(request), "jobs:read:all")
        if isinstance(covers, Response):
            return covers
        return JSONResponse(listing(world, request.query_params, covers))

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> Response:
        job = job_for(request, job_id, "jobs:read:all")
        if isinstance(job, Response):
            return job
        # THE SEGMENTS AS THE WORKER HOLDS THEM, (text, pause_after) pairs, which
        # is what tts-long's GET /jobs/{id} returns: main.py's _segments() builds
        # them and get_job() hands them over as they are. This answered a list of
        # bare strings, a shape no service sends, so a page reading the record
        # was tested against a contract that does not exist. Pauses are not
        # modelled here, so each is 0.0. A record with no text keeps none, as a
        # job recovered from disk does.
        pairs = [[piece, 0.0] for piece in job.get("segments") or []] if job.get("text") else []
        return JSONResponse(public(job) | {"text": job.get("text"), "segments": pairs})

    @app.delete("/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request) -> Response:
        job = job_for(request, job_id, "jobs:delete:all")
        if isinstance(job, Response):
            return job
        advance(job)
        if job["status"] in ("done", "failed", "cancelled"):
            del world.jobs[job_id]
            return JSONResponse({"id": job_id, "status": "deleted"})
        job.update(cancelled=True, status="cancelled", finished_at=time.time())
        if job["offsets"]:
            job.update(path=f"/out/{job_id}.wav", audio_seconds=job["offsets"][-1] + 1.0)
        return JSONResponse({"id": job_id, "status": "cancelling"})

    @app.get("/jobs/{job_id}/audio")
    async def job_audio(job_id: str, request: Request) -> Response:
        job = job_for(request, job_id, "jobs:read:all")
        if isinstance(job, Response):
            return job
        advance(job)
        if job["status"] not in ("done", "cancelled") or not job.get("path"):
            return JSONResponse({"detail": f"job is {job['status']}"}, status_code=409)
        return Response(wav(min(30.0, job.get("audio_seconds") or 2.0)), media_type="audio/wav",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}.wav"'})

    @app.delete("/jobs/{job_id}/audio")
    async def delete_job_audio(job_id: str, request: Request) -> Response:
        job = job_for(request, job_id, "jobs:delete:all")
        if isinstance(job, Response):
            return job
        advance(job)
        if job["status"] not in ("done", "failed", "cancelled"):
            return JSONResponse({"detail": "that job has not finished; cancel it instead"}, status_code=409)
        if audio_state(job)["state"] == "never":
            return JSONResponse({"detail": f"a {job['kind']} run keeps no audio on this service, so "
                                           "there is nothing here to delete"}, status_code=409)
        job.update(path=None, bytes=0, audio_deleted=True)
        return JSONResponse({"id": job_id, "audio": audio_state(job)})

    @app.post("/v1/audio/speech")
    async def v1_speech(request: Request) -> Response:
        return await speech(world, request, 0.7)

    return app


# ---- MeTube ------------------------------------------------------------------------

DOWNLOAD_SECONDS = 3.0


def metube_entry(world: World, url: str) -> tuple[str, dict[str, Any]] | None:
    entry = world.metube.get(url)
    if entry is None:
        return None
    if entry["where"] == "queue":
        elapsed = time.time() - entry["started"]
        if elapsed >= DOWNLOAD_SECONDS and "broken" in url:
            # A LINK THAT RESOLVES AND THEN FAILS TO DOWNLOAD, as a video taken
            # down between the two does: MeTube files it under done with an
            # error status and its message, and no file.
            entry.update(where="done", status="error", percent=None, speed=None, eta=None,
                         msg="ERROR: [generic] Unable to download webpage: HTTP Error 403: Forbidden")
        elif elapsed >= DOWNLOAD_SECONDS:
            entry.update(where="done", status="finished", percent=100, speed=None, eta=None,
                         filename=entry["_filename"])
        else:
            entry.update(percent=round(100 * elapsed / DOWNLOAD_SECONDS, 1), speed=2.5e6,
                         eta=round(DOWNLOAD_SECONDS - elapsed, 1))
    return entry["where"], {k: v for k, v in entry.items() if not k.startswith("_") and k != "where"}


def metube_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.post("/add")
    async def add(request: Request) -> dict[str, Any]:
        body = await request.json()
        url = body["url"]
        if "unsupported" in url:
            return {"status": "error", "msg": "ERROR: Unsupported URL: " + url}
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower().rstrip("/").rsplit("/", 1)[-1]).strip("-") or "talk"
        kind = body.get("download_type") or "audio"
        suffix = {"captions": "en.vtt", "video": "mp4"}.get(kind, body.get("format") or "wav")
        world.metube[url] = {
            "id": uuid.uuid4().hex[:11], "url": url, "title": "Example talk: " + slug.replace("-", " "),
            "status": "pending", "size": None, "percent": None, "speed": None, "eta": None,
            "live_status": "not_live", "filename": None, "folder": body.get("folder") or "",
            "download_type": kind, "where": "pending", "_filename": f"{slug}.{suffix}",
            "started": 0.0}
        if body.get("auto_start", True):
            world.metube[url].update(where="queue", status="downloading", percent=0, started=time.time())
        return {"status": "ok"}

    @app.post("/start")
    async def start(request: Request) -> dict[str, Any]:
        for url in (await request.json()).get("ids") or []:
            entry = world.metube.get(url)
            if entry and entry["where"] == "pending":
                entry.update(where="queue", status="downloading", percent=0, started=time.time())
        return {"status": "ok"}

    @app.post("/delete")
    async def delete(request: Request) -> dict[str, Any]:
        body = await request.json()
        for url in body.get("ids") or []:
            found = metube_entry(world, url)
            if found and (found[0] != "done") == (body.get("where") == "queue"):
                del world.metube[url]
        return {"status": "ok"}  # always ok, as the real one answers, deleted or not

    @app.get("/history")
    async def history() -> dict[str, Any]:
        out: dict[str, list] = {"pending": [], "queue": [], "done": []}
        for url in list(world.metube):
            found = metube_entry(world, url)
            if found:
                out[found[0]].append(found[1])
        return out

    def serve(path: str, request: Request) -> Response:
        # ONE EXACT PATH PER FINISHED DOWNLOAD, and a 404 for anything else,
        # because the real MeTube is that strict: a fixture that served any
        # path under the route is how a URL missing its folder once passed
        # every test and failed every real download (services/ui/tests).
        for url in list(world.metube):
            metube_entry(world, url)
        wanted_path = "/".join(p for p in path.split("/") if p)
        entry = next((e for e in world.metube.values() if e.get("filename") and wanted_path ==
                      "/".join(p for p in (e["folder"], e["filename"]) if p)), None)
        if entry is None:
            return PlainTextResponse("not found", status_code=404)
        name = entry["filename"]
        if name.endswith(".vtt"):
            segments, _ = timed(world.transcript, 12.0)
            body, kind = subtitles(segments, True).encode(), "text/vtt"
        else:
            body, kind = wav(12.0), ("video/mp4" if name.endswith(".mp4") else "audio/wav")
        wanted = request.headers.get("range", "")
        common = {"accept-ranges": "bytes", "etag": '"e2e-fake"'}
        match = re.match(r"bytes=(\d*)-(\d*)$", wanted)
        if match:
            first = int(match.group(1) or 0)
            last = min(int(match.group(2)) if match.group(2) else len(body) - 1, len(body) - 1)
            return Response(body[first:last + 1], status_code=206, media_type=kind,
                            headers=common | {"content-range": f"bytes {first}-{last}/{len(body)}"})
        return Response(body, media_type=kind, headers=common)

    @app.get("/audio_download/{path:path}")
    async def audio_download(path: str, request: Request) -> Response:
        return serve(path, request)

    @app.get("/download/{path:path}")
    async def download(path: str, request: Request) -> Response:
        return serve(path, request)

    return app


# ---- the satellites ----------------------------------------------------------------

KORVO_CAPS: dict[str, Any] = {  # clients/home-assistant/tests/fake_calliope.py, as the firmware sends them
    "mic": {"rate": 16000, "channels": 4, "format": "s16le"},
    "speaker": {"rate": 48000, "channels": 1, "format": "s16le"},
    "lights": 12,
    "light_modes": ["off", "solid", "pulse", "spin", "pixels", "listen"],
    "buttons": ["vol_up", "vol_down", "set", "play", "mode", "rec", "key1"],
    "actions": ["mute", "volume_up", "volume_down", "lights", "dimmer", "brighter"],
    "ota_key": "k1",
    "earcons": {"max": 16, "max_bytes": 524288, "rate": 48000},
    "duck": True,
}
PI_CAPS: dict[str, Any] = {  # clients/pi-satellite/calliope_pi/agent.py caps(), with AirPlay
    "speaker": {"rate": 44100, "channels": 1, "format": "s16le"},
    "media": {"rate": 44100, "channels": 2, "format": "s16le"},
    "mic": {"rate": 16000, "channels": 2, "format": "s16le", "reference": True, "max_gain_db": 3.5},
    "earcons": {"max": 16, "max_bytes": 524288, "rate": 48000},
    "duck": True, "audio_devices": True, "bundle": "tar.gz", "ota_key": "k1",
    "airplay": {"version": 2, "controls": True},
    "health": ["temp_c", "throttled", "under_voltage", "load"],
}
PI_SINKS = [
    {"name": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo", "description": "USB Audio Analog Stereo",
     "api": "alsa", "quality": {"kind": "usb", "dac": True, "bits": [16, 24], "rates": [44100, 48000, 96000]},
     "jack": None},
    {"name": "alsa_output.platform-bcm2835_audio.stereo-fallback", "description": "Built-in Audio Stereo",
     "api": "alsa", "quality": {"kind": "pwm", "dac": False, "bits": [16], "rates": [48000]},
     "jack": "unplugged"},
]
PI_SOURCES = [
    {"name": "alsa_input.usb-Generic_USB_Audio-00.analog-stereo", "description": "USB Audio Analog Stereo",
     "api": "alsa", "quality": {"kind": "usb", "dac": True, "bits": [16], "rates": [16000, 48000]},
     "jack": None},
]
AIRPLAY_CONTROLS = ["play", "pause", "play_pause", "next", "previous", "stop", "disconnect"]
PLAYLIST = [("So What", "Miles Davis", "Kind of Blue", 562.0),
            ("Blue in Green", "Miles Davis", "Kind of Blue", 337.0),
            ("Take Five", "The Dave Brubeck Quartet", "Time Out", 324.0)]

SCRIPTED = {
    # key: (MAC, model, caps, adopt as, settings)
    "kitchen": ("02:00:00:00:00:01", "esp32-korvo-v1.1", KORVO_CAPS, "Kitchen",
                {"volume": 55, "mic_gain_db": 0.0, "mic_enabled": True, "speaker_enabled": True,
                 "lights_enabled": True, "brightness": 60}),
    "lounge": ("02:00:00:00:00:02", "raspberry-pi", PI_CAPS, "Lounge",
               {"volume": 40, "mic_gain_db": 0.0, "mic_enabled": True, "speaker_enabled": True,
                "audio_sink": PI_SINKS[0]["name"], "audio_source": "", "echo_reference": True,
                "airplay_enabled": True, "airplay_name": None}),
    "hallway": ("02:00:00:00:00:03", "esp32-korvo-v1.1", KORVO_CAPS, None,
                {"volume": 60, "mic_gain_db": 0.0, "mic_enabled": True, "speaker_enabled": True,
                 "lights_enabled": True}),
}


class Satellite:
    """One scripted device on the hub's socket. See the module docstring."""

    def __init__(self, world: World, key: str) -> None:
        mac, model, caps, adopt_as, settings = SCRIPTED[key]
        self.world, self.key, self.mac, self.model = world, key, mac, model
        self.nid = mac.replace(":", "").lower()
        self.caps = json.loads(json.dumps(caps))
        self.adopt_as = adopt_as
        self.settings = dict(settings)
        self.fw = "v0.2.0" if model.startswith("esp32") else "v0.1.2-193-g0ae5ed7"
        self.token: str | None = None
        self.name: str | None = None
        self.state = "disconnected"
        self.ws: ClientConnection | None = None
        self.received: list[dict[str, Any]] = []
        self.audio_frames: dict[int, int] = {}
        self.earcons: dict[str, dict[str, Any]] = {}
        self.upload: dict[str, Any] | None = None
        self.ota: dict[str, Any] | None = None
        self.extra: dict[str, Any] = {}
        self.status_every = 10.0
        self.airplay_mode = "answer"  # or "refuse", "ignore"
        self.track = 0
        self.airplay = self._airplay_playing() if "airplay" in caps else None
        self.born = time.time()
        self.tasks: set[asyncio.Task] = set()
        self.run_task: asyncio.Task | None = None
        self.welcomed = asyncio.Event()
        self.pending = asyncio.Event()

    # -- what it says --

    def _airplay_idle(self) -> dict[str, Any]:
        return {"enabled": bool(self.settings.get("airplay_enabled", True)),
                "name": self.settings.get("airplay_name") or (self.name or "lounge").lower(),
                "running": True, "error": None, "player": None, "playing": False, "session": False,
                "client": None, "title": None, "artist": None, "album": None, "volume": None,
                "since": None, "track": None, "client_info": None, "progress": None, "artwork": None,
                "stream": None, "raw": {}, "remote": {"available": None, "controls": [], "last": None}}

    def _cover(self) -> bytes:
        return png(96, hue=self.track * 70)

    def _airplay_playing(self, paused: bool = False) -> dict[str, Any]:
        title, artist, album, duration = PLAYLIST[self.track % len(PLAYLIST)]
        cover = self._cover()
        return self._airplay_idle() | {
            "player": "Paused" if paused else "Playing", "playing": not paused, "session": True,
            "client": "Gabriel's iPhone", "title": title, "artist": artist, "album": album,
            "volume": self.settings.get("volume"), "since": time.time() - 42,
            "track": {"persistent_id": f"track-{self.track}", "title": title},
            "client_info": {"client_ip": "fe80::1", "dacp_id": "D1"},
            "progress": {"position_s": 42.0, "duration_s": duration},
            "artwork": {"sha256": hashlib.sha256(cover).hexdigest(), "bytes": len(cover), "type": "image/png"},
            "raw": {}, "remote": {"available": True, "controls": AIRPLAY_CONTROLS, "last": None}}

    def hello(self) -> dict[str, Any]:
        msg = {"type": "hello", "id": self.mac, "model": self.model, "fw": self.fw, "caps": self.caps,
               "token": self.token, "name": self.name} | self.settings
        if "audio_devices" in self.caps:
            msg["audio"] = self.audio()
            msg["board"] = "Raspberry Pi 4 Model B Rev 1.5"
        return msg

    def audio(self) -> dict[str, Any]:
        return {"sinks": PI_SINKS, "sources": PI_SOURCES, "default_sink": PI_SINKS[0]["name"],
                "default_source": PI_SOURCES[0]["name"], "playing_at": None}

    def status(self, cause: str | None = None) -> dict[str, Any]:
        up = int(time.time() - self.born) + 3600
        msg: dict[str, Any] = {"type": "status", "uptime_s": up, "rssi": -58, "muted": False,
                               "mic_dropped": 0, "spk_dropped": 0, "spk_buffered_ms": 0}
        if "audio_devices" in self.caps:
            msg |= {"rssi": -61, "heap": 2_900_000_000, "temp_c": 48.5, "throttled": 0,
                    "under_voltage": False, "load": 0.31, "media_buffered_ms": 0, "media_dropped": 0,
                    "duck": None, "earcons_ready": True, "audio": self.audio()}
            if self.airplay is not None:
                air = dict(self.airplay)
                if air.get("progress") and air.get("playing"):
                    position = air["progress"]["position_s"] + (time.time() - air["since"] - 42)
                    air["progress"] = dict(air["progress"], position_s=round(
                        position % air["progress"]["duration_s"], 1))
                msg["airplay"] = air
        else:
            msg["heap"] = 90000
        msg |= self.settings | self.extra
        if cause:
            msg["cause"] = cause
        return msg

    def view(self) -> dict[str, Any]:
        return {"key": self.key, "id": self.nid, "mac": self.mac, "model": self.model, "state": self.state,
                "name": self.name, "settings": self.settings, "earcons": sorted(self.earcons),
                "audio_frames": self.audio_frames, "received": len(self.received),
                "airplay": self.airplay and {k: self.airplay.get(k) for k in ("playing", "title", "session")}}

    # -- the connection --

    async def send(self, msg: dict[str, Any]) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(msg))

    async def send_status(self, cause: str | None = None) -> None:
        if self.airplay and self.airplay.get("artwork"):
            cover = self._cover()
            await self.send({"type": "artwork", "sha256": hashlib.sha256(cover).hexdigest(),
                             "format": "png", "data": base64.b64encode(cover).decode()})
        await self.send(self.status(cause))

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def start(self) -> None:
        if self.run_task is None or self.run_task.done():
            self.run_task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        for task in [self.run_task, *self.tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in [self.run_task, *self.tasks] if t is not None),
                             return_exceptions=True)
        self.run_task = None
        self.ws = None
        self.state = "disconnected"
        self.welcomed.clear()
        self.pending.clear()

    async def run(self, delay: float = 0.0) -> None:
        """Connect, and reconnect after a reboot or a dropped socket, until stopped.

        Through the gateway's device socket, as a satellite at home connects:
        the hub answers only connections the gateway relayed (D53). No Origin
        header, as neither firmware sends a web page's."""
        await asyncio.sleep(delay)
        while self.world.gateway:
            ws_url = self.world.gateway.replace("http", "ws", 1) + "/satellites/ws"
            try:
                async with connect(ws_url, max_size=None, proxy=None, open_timeout=10) as ws:
                    self.ws = ws
                    self.state = "hello"
                    self.welcomed.clear()
                    self.pending.clear()
                    await self.send(self.hello())
                    ticker = self.spawn(self._tick())
                    try:
                        async for message in ws:
                            if isinstance(message, bytes):
                                await self.on_binary(message)
                            else:
                                await self.on_message(json.loads(message))
                    finally:
                        ticker.cancel()
            except (OSError, ConnectionClosed, asyncio.TimeoutError):
                pass
            except _Reconnect as again:
                self.ws = None
                self.state = "rebooting"
                await asyncio.sleep(again.after)
                continue
            self.ws = None
            self.state = "disconnected"
            self.welcomed.clear()
            await asyncio.sleep(2.0)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.status_every)
            if self.state == "online":
                await self.send_status()

    async def adopt(self) -> None:
        """Adopted as an admin adopts one, through the gateway with a key that
        holds satellites:admin; the hub takes no other word for it."""
        async with httpx.AsyncClient(base_url=self.world.gateway, timeout=10, headers={
                "Authorization": f"Bearer {self.world.adopt_key}"}) as http:
            r = await http.post(f"/satellites/{self.nid}/adopt", json={"name": self.adopt_as})
            r.raise_for_status()

    # -- what it hears --

    async def on_message(self, msg: dict[str, Any]) -> None:
        self.received.append({"t": time.time(), **msg})
        del self.received[:-500]
        kind = msg.get("type")
        if kind == "pending":
            # A hub that answers a token with "pending" has disowned it (a
            # fresh hub after restart_hub, or a forget), so it is dropped.
            self.token = None
            self.state = "pending"
            self.pending.set()
            if self.adopt_as:
                self.spawn(self.adopt())
        elif kind == "adopt":
            self.token, self.name = msg.get("token"), msg.get("name")
            await self.send(self.hello())
        elif kind == "welcome":
            self.name = msg.get("name") or self.name
            config = msg.get("config") or {}
            self.settings.update({k: v for k, v in config.items() if k in self.settings or k == "brightness"})
            self.state = "online"
            self.welcomed.set()
            await self.send_status()
        elif kind == "config":
            changed = {k: v for k, v in msg.items() if k != "type"}
            if "name" in changed:
                self.name = changed.pop("name")
            self.settings.update(changed)
            if self.airplay is not None:
                self.airplay.update(enabled=bool(self.settings.get("airplay_enabled", True)))
            await self.send_status()
        elif kind == "forget":
            self.token, self.name, self.adopt_as = None, None, None
            raise _Reconnect(1.0)
        elif kind == "reboot":
            raise _Reconnect(2.0)
        elif kind == "airplay_command":
            await self.on_airplay(msg)
        elif kind == "earcon_list":
            await self.send_earcons()
        elif kind == "earcon_put":
            self.upload = {"id": msg["id"], "size": int(msg["size"]), "sha256": msg["sha256"],
                           "data": bytearray()}
            await self.send({"type": "earcon_next", "id": msg["id"], "offset": 0})
        elif kind == "earcon_delete":
            self.earcons.pop(str(msg.get("id")), None)
            await self.send_earcons()
        elif kind == "ota":
            self.ota = {"size": int(msg["size"]), "sha256": msg["sha256"], "version": msg.get("version"),
                        "data": bytearray(), "said": 0}
            await self.send({"type": "ota", "state": "started", "pct": 0, "version": msg.get("version")})
            await self.send({"type": "ota_next", "offset": 0})

    async def send_earcons(self) -> None:
        await self.send({"type": "earcons", "ready": True, "last_load_us": 1800,
                         "items": [{"id": k, "size": v["size"], "sha256": v["sha256"]}
                                   for k, v in sorted(self.earcons.items())]})

    async def on_binary(self, frame: bytes) -> None:
        kind = frame[0]
        self.audio_frames[kind] = self.audio_frames.get(kind, 0) + 1
        if kind == 4 and self.upload is not None:  # an earcon chunk: kind, 0, 0, 0, offset u32
            offset = struct.unpack("<I", frame[4:8])[0]
            data = self.upload["data"]
            if offset == len(data):
                data += frame[8:]
            if len(data) >= self.upload["size"]:
                ok = hashlib.sha256(bytes(data)).hexdigest() == self.upload["sha256"]
                eid = self.upload["id"]
                self.upload = None
                if ok:
                    self.earcons[eid] = {"size": len(data), "sha256": hashlib.sha256(bytes(data)).hexdigest()}
                    await self.send({"type": "earcon_stored", "id": eid, "size": len(data),
                                     "sha256": self.earcons[eid]["sha256"]})
                else:
                    await self.send({"type": "earcon_failed", "op": "put", "id": eid, "error": "sha256"})
            else:
                await self.send({"type": "earcon_next", "id": self.upload["id"], "offset": len(data)})
        elif kind == 3 and self.ota is not None:  # firmware: kind, 0, 0, 0, offset u32
            offset = struct.unpack("<I", frame[4:8])[0]
            data = self.ota["data"]
            if offset == len(data):
                data += frame[8:]
            pct = int(100 * len(data) / max(1, self.ota["size"]))
            if pct >= self.ota["said"] + 20 and len(data) < self.ota["size"]:
                self.ota["said"] = pct
                await self.send({"type": "ota", "state": "progress", "pct": pct, "version": self.ota["version"]})
            if len(data) < self.ota["size"]:
                await self.send({"type": "ota_next", "offset": len(data)})
                return
            version, ok = self.ota["version"], hashlib.sha256(bytes(data)).hexdigest() == self.ota["sha256"]
            self.ota = None
            if not ok:
                await self.send({"type": "ota", "state": "failed", "error": "sha256 mismatch", "version": version})
                return
            await self.send({"type": "ota", "state": "verified", "pct": 100, "version": version})
            self.fw = version or self.fw
            raise _Reconnect(1.5)

    async def on_airplay(self, msg: dict[str, Any]) -> None:
        command, rid = msg.get("command"), msg.get("id")
        if self.airplay_mode == "ignore":
            return
        if self.airplay_mode == "refuse":
            await self.send({"type": "airplay_result", "id": rid, "command": command, "ok": False,
                             "status": 403, "confirmed": False, "error": "the phone answered 403"})
            return
        playing = bool(self.airplay and self.airplay.get("playing"))
        if command in ("stop", "disconnect"):
            self.airplay = self._airplay_idle()
        elif command in ("next", "previous"):
            self.track += 1 if command == "next" else -1
            self.airplay = self._airplay_playing()
        elif command in ("play", "pause", "play_pause"):
            want = {"play": True, "pause": False, "play_pause": not playing}[command]
            if not (self.airplay or {}).get("session"):
                self.airplay = self._airplay_playing(paused=not want)
            self.airplay.update(playing=want, player="Playing" if want else "Paused")
        await self.send({"type": "airplay_result", "id": rid, "command": command, "ok": True,
                         "status": 204, "confirmed": True, "error": None})
        await self.send_status()

    async def mic(self, seconds: float, clip: bytes | None = None) -> None:
        """Microphone frames at real time: 20 ms, a quiet tone on every channel,
        or `clip` (a 16 kHz mono WAV) as the room heard it (heard_in_room)."""
        channels = self.caps["mic"]["channels"]
        tone = pcm(seconds, 16000, channels, pitch=220.0) if clip is None \
            else heard_in_room(clip, seconds, channels)
        step = 320 * channels * 2
        t0 = time.monotonic()
        for i, offset in enumerate(range(0, len(tone) - step + 1, step)):
            await self.ws.send(struct.pack("<BBBBIQ", 1, 0, channels, 0, i, 0) + tone[offset:offset + step])
            await asyncio.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.monotonic()))


class _Reconnect(Exception):
    def __init__(self, after: float) -> None:
        self.after = after


# The recordings a scripted microphone can play: the hub's own test fixtures,
# a person saying a wake word, by file name and nothing else.
FIXTURES = Path(__file__).resolve().parents[2] / "satellites" / "tests" / "fixtures"
FIXTURE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.wav$")


def heard_in_room(clip: bytes, seconds: float, channels: int) -> bytes:
    """A recorded voice as a satellite's microphones hear it, for `seconds`:
    half a second of a quiet room, the clip, then the room again. Every
    microphone hears it; the loopback (channel 0 on a satellite with more
    than one) is silent, because nothing is playing. Interleaved s16le, as
    the satellite sends its frames.

    WHY A RECORDING AND NOT /inject. The hub's double-check runs on a wake
    word heard live, from a satellite's microphones, and nowhere else; a clip
    sent to /inject goes through a detector of its own and is never checked,
    and the page marks its events as a test and lights nothing for them. So
    the one way to hear a wake word as a satellite hears it is through its
    microphones."""
    with wave.open(io.BytesIO(clip)) as w:
        voice = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    rate = 16000
    total = int(seconds * rate)
    rng = np.random.default_rng(7)
    room = rng.normal(0.0, 30.0, total).astype(np.float32)
    start = rate // 2
    end = min(total, start + len(voice))
    room[start:end] += voice[:end - start]
    mono = np.clip(room, -32768, 32767).astype("<i2")
    frames = np.repeat(mono[:, None], channels, axis=1)
    if channels > 1:
        frames[:, 0] = 0
    return frames.tobytes()


# ---- what a wake word's action reaches -----------------------------------------------
#
# HOME ASSISTANT, A LANGUAGE MODEL SERVER AND A WEBHOOK RECEIVER, under /__ha,
# /__llm and /__hook on the control port. The hub calls them for a wake word's
# action (Try a word, the model picker and its Test, the pipeline picker) and
# for a button set to Webhook, and launch.py lets it reach nothing but
# loopback, so these are what a test points an action at; an address anywhere
# else would be a refused connection and a line in network-violations.log.
# Each is wrapped in Observed, so what the hub sent is in the request log
# under backend "ha", "llm" or "hook" with its body and its Authorization
# header, and fake.fail(path, backend="llm", status=401) breaks one as a
# provider would. The wire shapes are the ones services/satellites/app/
# destinations.py reads: Home Assistant's POST /api/conversation/process and
# its websocket's auth handshake and assist_pipeline/pipeline/list; an
# OpenAI-compatible GET /models that pages as Anthropic's does (has_more,
# last_id, after_id) and a POST /chat/completions that streams; a webhook that
# answers 200 with nothing to say.

HA_REPLY = "Turned on the kitchen lights."
HA_PIPELINES = {
    "preferred_pipeline": "01home",
    "pipelines": [
        {"id": "01home", "name": "Home", "language": "en", "conversation_language": "en",
         "stt_engine": "stt.faster_whisper", "stt_language": "en",
         "tts_engine": "tts.piper", "tts_language": "en-GB", "tts_voice": "en_GB-alba-medium"},
        {"id": "02kitchen", "name": "Kitchen pipeline", "language": "pt", "conversation_language": "*",
         "stt_engine": None, "stt_language": None,
         "tts_engine": "tts.google_translate", "tts_language": "pt", "tts_voice": None}]}
LLM_REPLY = ["Hello", " there,", " friend."]
# Two pages: the first says there is more after its last id, as Anthropic's does.
LLM_PAGES = [["fake-large", "fake-small"], ["fake-tiny"]]


def bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


def ha_app(world: World) -> FastAPI:
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocketDisconnect

    app = FastAPI(openapi_url=None)

    @app.post("/api/conversation/process")
    async def conversation(request: Request) -> Response:
        # Read, as HA reads it: Observed records only the body the app took.
        await request.body()
        if not bearer(request):
            return JSONResponse({"message": "Invalid authentication"}, status_code=401)
        return JSONResponse({
            "conversation_id": "01e2e", "response": {
                "response_type": "action_done", "language": "en",
                "data": {"targets": [], "success": [{"id": "light.kitchen", "name": "Kitchen"}],
                         "failed": []},
                "speech": {"plain": {"speech": HA_REPLY, "extra_data": None}}}})

    async def websocket(ws) -> None:
        """Home Assistant's handshake, then the one command the picker sends.
        Each message the hub sends is recorded, the token as the fact of one.
        A Starlette route and not FastAPI's decorator: this module's
        annotations are strings, and FastAPI, unable to resolve a WebSocket
        imported here, took the parameter for a query and refused the
        handshake with a 403."""
        await ws.accept()
        try:
            await ws.send_json({"type": "auth_required", "ha_version": "2026.9.0"})
            auth = await ws.receive_json()
            token = str(auth.get("access_token") or "")
            world.record({"t": time.time(), "backend": "ha", "method": "WS", "path": "/__ha/api/websocket",
                          "query": "", "headers": {}, "status": 101,
                          "json": {"type": auth.get("type"), "token": bool(token)}})
            if not token:
                await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
                return
            await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.0"})
            while True:
                msg = await ws.receive_json()
                world.record({"t": time.time(), "backend": "ha", "method": "WS",
                              "path": "/__ha/api/websocket", "query": "", "headers": {}, "status": 101,
                              "json": msg})
                if msg.get("type") == "assist_pipeline/pipeline/list":
                    await ws.send_json({"id": msg.get("id"), "type": "result", "success": True,
                                        "result": HA_PIPELINES})
                else:
                    await ws.send_json({"id": msg.get("id"), "type": "result", "success": False,
                                        "error": {"code": "unknown_command", "message": "Unknown command."}})
        except WebSocketDisconnect:
            return

    app.router.routes.append(WebSocketRoute("/api/websocket", websocket))
    return app


def llm_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.get("/v1/models")
    async def models(request: Request) -> dict[str, Any]:
        after = request.query_params.get("after_id")
        page = 1 if after == LLM_PAGES[0][-1] else 0
        ids = LLM_PAGES[page]
        return {"data": [{"id": i, "object": "model", "created": 1767225600} for i in ids],
                "has_more": page + 1 < len(LLM_PAGES), "first_id": ids[0], "last_id": ids[-1]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Response:
        body = await request.json()
        model = body.get("model") or "fake-small"
        if not body.get("stream"):
            return JSONResponse({"id": "chatcmpl-e2e", "object": "chat.completion", "model": model,
                                 "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": "".join(LLM_REPLY)}}],
                                 "usage": {"prompt_tokens": 12, "completion_tokens": 4}})

        async def chunks():
            for piece in LLM_REPLY:
                delta = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                yield f"data: {json.dumps(delta)}\n\n"
                await asyncio.sleep(0.05)
            end = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(end)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app


def hook_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.post("/{name}")
    async def hook(name: str, request: Request) -> dict[str, Any]:
        # Read, as a receiver does: Observed records only the body the app took.
        await request.body()
        return {"ok": True}

    return app


# ---- the control API ---------------------------------------------------------------


def control_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)
    for prefix, backend, make in (("/__ha", "ha", ha_app), ("/__llm", "llm", llm_app),
                                  ("/__hook", "hook", hook_app)):
        app.mount(prefix, Observed(make(world), backend, world))

    def sat(key: str) -> Satellite:
        found = world.satellites.get(key) or next(
            (s for s in world.satellites.values() if key in (s.nid, s.mac)), None)
        if found is None:
            raise KeyError(key)
        return found

    @app.exception_handler(KeyError)
    async def unknown(request: Request, exc: KeyError) -> Response:
        return JSONResponse({"error": f"no such thing: {exc}"}, status_code=404)

    @app.get("/__fake/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "requests": len(world.log)}

    @app.post("/__fake/reset")
    async def reset() -> dict[str, Any]:
        changed = any(world.health_overrides.values())
        world.reset()
        return {"ok": True, "health_changed": changed}

    @app.get("/__fake/seq")
    async def now() -> dict[str, Any]:
        return {"seq": world.seq}

    @app.get("/__fake/requests")
    async def requests(backend: str | None = None, method: str | None = None, path: str | None = None,
                       since: int = 0) -> list[dict[str, Any]]:
        return [e for e in world.log if e["seq"] > since
                and (backend is None or e["backend"] == backend)
                and (method is None or e["method"] == method.upper())
                and (path is None or re.search(path, e["path"]))]

    @app.delete("/__fake/requests")
    async def forget_requests() -> dict[str, Any]:
        world.log.clear()
        return {"ok": True, "seq": world.seq}

    @app.post("/__fake/fail")
    async def fail(request: Request) -> dict[str, Any]:
        rule = await request.json()
        world.rules.insert(0, rule)
        return {"ok": True, "rules": len(world.rules)}

    @app.delete("/__fake/fail")
    async def unfail() -> dict[str, Any]:
        world.rules.clear()
        return {"ok": True}

    @app.post("/__fake/health/{backend}")
    async def health_override(backend: str, request: Request) -> dict[str, Any]:
        world.health_overrides.setdefault(backend, {}).update(await request.json())
        return {"ok": True}

    @app.put("/__fake/transcript")
    async def transcript(request: Request) -> dict[str, Any]:
        world.transcript = str((await request.json())["text"])
        return {"ok": True}

    @app.post("/__fake/glossaries")
    async def glossary_rules(request: Request) -> dict[str, Any]:
        body = await request.json()
        world.gloss_writable = bool(body.get("writable", True))
        world.gloss_reason = body.get("reason")
        world.gloss_strict = bool(body.get("strict", False))
        return {"ok": True}

    @app.post("/__fake/owners")
    async def owners(request: Request) -> dict[str, Any]:
        """Who the session's admin is, so the seeded history is theirs; the
        fakes start over with it."""
        world.admin = (await request.json())["admin"]
        world.reset()
        return {"ok": True}

    @app.post("/__fake/jobs")
    async def seed_job(request: Request) -> dict[str, Any]:
        """A job, the admin's unless `owner` says whose (null is system)."""
        body = await request.json()
        owner = body.pop("owner", world.admin)
        job = new_job(world, text=body.pop("text", SEED_TEXT), voice=body.pop("voice", "narrator"),
                      engine=body.pop("engine", "chatterbox"), scripted=body.pop("scripted", True),
                      owner=owner, credential=body.pop("credential", "session" if owner else None))
        job.update(body)
        return public(job)

    # WHAT tts-long HOLDS, for the harness, whoever owns it: GET /jobs's
    # answer over every job (the same filters and limit), and one job's whole
    # record. A test reads these here rather than asking the service, which
    # answers only what the gateway forwards.
    @app.get("/__fake/jobs")
    async def jobs(request: Request) -> dict[str, Any]:
        return listing(world, request.query_params)

    @app.get("/__fake/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        found = world.jobs[job_id]
        return public(found) | {"text": found.get("text")}

    @app.get("/__fake/health/{backend}")
    async def backend_health(backend: str) -> dict[str, Any]:
        """A backend's /health as it answers now, overrides included."""
        return {"stt": stt_health, "tts": tts_health, "tts_long": long_health}[backend](world)

    @app.get("/__fake/elsewhere/sign-in")
    async def elsewhere(to: str, username: str, password: str) -> Response:
        """ANOTHER SITE'S PAGE that signs its visitor in to `to` as somebody
        else, by a form it submits itself (login CSRF, D14, D61). Served on
        this port, so it is another origin than the gateway's; reached as
        127.0.0.1 it is the same site, as a sibling on the NAS is, and as
        localhost it is a different site altogether. A text/plain form is
        the closest a form can come to the JSON the login takes."""
        field = html.escape(json.dumps({"username": username, "password": password})[:-1] + ', "x": "',
                            quote=True)
        page = (f'<!doctype html><title>Elsewhere</title><form id="f" method="post" '
                f'action="{html.escape(to, quote=True)}" enctype="text/plain">'
                f'<input type="hidden" name="{field}" value=\'"}}\'></form>'
                '<script>document.getElementById("f").submit()</script>')
        return Response(page, media_type="text/html")

    @app.get("/__fake/metube")
    async def metube() -> dict[str, Any]:
        return {url: {k: v for k, v in e.items() if not k.startswith("_")} for url, e in world.metube.items()}

    @app.post("/__fake/satellites/connect")
    async def connect_all(request: Request) -> dict[str, Any]:
        body = await request.json()
        world.gateway = body["gateway"].rstrip("/")
        world.adopt_key = body["key"]
        keys = body.get("satellites") or list(SCRIPTED)
        # AS THEY START, for a hub that starts with nothing on it
        # (stack.restart_hub). A device keeps what it was told across a
        # reconnect, as a real one does: a volume, a muted mic, the track its
        # phone skipped to, and a forget, after which it never adopts itself
        # again. So a fresh hub met the devices the last test left, and
        # Kitchen forgotten by one test stayed waiting for every test after.
        if body.get("fresh"):
            for key in keys:
                old = world.satellites.pop(key, None)
                if old is not None:
                    await old.stop()
        for key in keys:
            world.satellites.setdefault(key, Satellite(world, key)).start()
        deadline = time.monotonic() + float(body.get("timeout", 20))
        for key in keys:
            s = world.satellites[key]
            event = s.welcomed if s.adopt_as else s.pending
            await asyncio.wait_for(event.wait(), max(0.1, deadline - time.monotonic()))
        return {"satellites": [world.satellites[k].view() for k in keys]}

    @app.post("/__fake/satellites/disconnect")
    async def disconnect_all() -> dict[str, Any]:
        for s in world.satellites.values():
            await s.stop()
        return {"ok": True}

    @app.get("/__fake/satellites")
    async def satellites() -> list[dict[str, Any]]:
        return [s.view() for s in world.satellites.values()]

    @app.get("/__fake/satellites/{key}/received")
    async def received(key: str, type: str | None = None, since: float = 0) -> list[dict[str, Any]]:
        return [m for m in sat(key).received if m["t"] > since and (type is None or m.get("type") == type)]

    @app.delete("/__fake/satellites/{key}/received")
    async def forget_received(key: str) -> dict[str, Any]:
        sat(key).received.clear()
        return {"ok": True}

    @app.post("/__fake/satellites/{key}/status")
    async def push_status(key: str, request: Request) -> dict[str, Any]:
        """Change what the device reports and say so now: settings go into its
        settings, anything else rides along on every later status too."""
        s = sat(key)
        body = await request.json()
        cause = body.pop("cause", None)
        if "status_every" in body:
            s.status_every = float(body.pop("status_every"))
        for k, v in body.items():
            (s.settings if k in s.settings else s.extra)[k] = v
        await s.send_status(cause)
        return s.view()

    @app.post("/__fake/satellites/{key}/send")
    async def send(key: str, request: Request) -> dict[str, Any]:
        await sat(key).send(await request.json())
        return {"ok": True}

    @app.post("/__fake/satellites/{key}/airplay")
    async def airplay(key: str, request: Request) -> dict[str, Any]:
        s = sat(key)
        body = await request.json()
        s.airplay_mode = body.get("on_command", s.airplay_mode)
        state = body.get("state")
        if state == "idle":
            s.airplay = s._airplay_idle()
        elif state in ("playing", "paused"):
            s.airplay = s._airplay_playing(paused=state == "paused")
        await s.send_status()
        return s.view()

    @app.post("/__fake/satellites/{key}/mic")
    async def mic(key: str, request: Request) -> dict[str, Any]:
        s = sat(key)
        body = await request.json()
        clip = body.get("clip")
        if clip is not None and not FIXTURE_NAME.match(str(clip)):
            return JSONResponse({"error": f"not a fixture's name: {clip!r}"}, status_code=400)
        data = (FIXTURES / clip).read_bytes() if clip else None
        s.spawn(s.mic(float(body.get("seconds", 6.0)), data))
        return {"ok": True}

    @app.post("/__fake/satellites/{key}/caps")
    async def caps(key: str, request: Request) -> dict[str, Any]:
        """Change what the device says it is, and connect it again: caps are
        said once, in its hello. A key given null is taken out."""
        s = sat(key)
        for name, value in (await request.json()).items():
            if value is None:
                s.caps.pop(name, None)
            else:
                s.caps[name] = value
        await s.stop()
        s.start()
        return s.view()

    @app.post("/__fake/satellites/{key}/button")
    async def button(key: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        await sat(key).send({"type": "button", "button": body["button"], "action": body.get("action", "press"),
                             "held_ms": body.get("held_ms")})
        return {"ok": True}

    @app.post("/__fake/satellites/{key}/drop")
    async def drop(key: str) -> dict[str, Any]:
        """Stop the device: its socket closes and it stays away until /start."""
        await sat(key).stop()
        return {"ok": True}

    @app.post("/__fake/satellites/{key}/start")
    async def start(key: str) -> dict[str, Any]:
        s = world.satellites.setdefault(key, Satellite(world, key)) if key in SCRIPTED else sat(key)
        s.start()
        return {"ok": True}

    return app


# ---- the process -------------------------------------------------------------------


class _Server(uvicorn.Server):
    """uvicorn's server without its own signal handling: five of them share one
    process, and each would otherwise take SIGTERM for itself alone."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def serve(world: World, ports: dict[str, int]) -> None:
    import signal

    apps = {"stt": Observed(stt_app(world), "stt", world),
            "tts": Observed(tts_app(world), "tts", world),
            "long": Observed(long_app(world), "tts_long", world),
            "metube": Observed(metube_app(world), "metube", world),
            "control": control_app(world)}
    servers = [_Server(uvicorn.Config(apps[name], host="127.0.0.1", port=port, loop="asyncio",
                                      log_level="warning", lifespan="off", timeout_graceful_shutdown=2))
               for name, port in ports.items()]

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for s in world.satellites.values():
            await s.stop()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="fakes")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--svc-dir", required=True, type=Path)
    for name in ("stt", "tts", "long", "metube", "control"):
        parser.add_argument(f"--{name}-port", type=int, required=True)
    args = parser.parse_args(argv)
    world = World(args.repo, args.svc_dir)
    asyncio.run(serve(world, {name: getattr(args, f"{name}_port")
                              for name in ("stt", "tts", "long", "metube", "control")}))
