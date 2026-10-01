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

THE SATELLITES ARE SCRIPTED, NOT MOCKED. A Korvo, a Raspberry Pi and a second
Korvo that nobody has adopted connect to the real hub's WebSocket, say hello
with the caps their firmware sends, are adopted through the hub's own route,
and then behave: they send status on a clock, answer airplay_command, store
earcons, take an OTA image chunk by chunk, reboot and come back. So a control on
the Satellites tab goes all the way to a device and its answer comes all the
way back, which is what "the page updates without a refresh" has to be tested
against.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import functools
import hashlib
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
from typing import Any

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile
from voice_common.engines import CATALOGUE
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

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.seq = 0
        self.log: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []
        self.health_overrides: dict[str, dict[str, Any]] = {}
        self.transcript = DEFAULT_TRANSCRIPT
        self.jobs: dict[str, dict[str, Any]] = {}
        self.glossaries: dict[str, dict[str, Any]] = {}
        self.metube: dict[str, dict[str, Any]] = {}
        self.satellites: dict[str, Satellite] = {}
        self.hub: str | None = None
        self.reset()

    def reset(self) -> None:
        self.log.clear()
        self.rules.clear()
        self.health_overrides.clear()
        self.transcript = DEFAULT_TRANSCRIPT
        self.jobs.clear()
        self.metube.clear()
        self.glossaries = {name: g for name, g in self.glossaries.items() if g["source"] == "builtin"}
        if not self.glossaries:
            for path in sorted((self.repo / "services/stt/glossaries").glob("*.txt")):
                self.glossaries[path.stem] = glossary(path.stem, path.read_text("utf-8"), "builtin")
        seed_jobs(self)

    def record(self, entry: dict[str, Any]) -> None:
        self.seq += 1
        entry["seq"] = self.seq
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


class Observed:
    """ASGI middleware: log the request, and answer with an injected failure
    (or after an injected delay) before the fake itself is reached.

    The body is captured on its way to the app rather than read ahead of it,
    so a streamed upload still streams, and the app sees exactly the bytes it
    would have seen without this."""

    def __init__(self, app: Any, backend: str, world: World) -> None:
        self.app = app
        self.backend = backend
        self.world = world

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method, path = scope["method"], scope["path"]
        entry: dict[str, Any] = {
            "t": time.time(), "backend": self.backend, "method": method, "path": path,
            "query": scope.get("query_string", b"").decode("latin-1"),
            "headers": {k: v for k, v in headers.items()
                        if k in ("content-type", "authorization", "accept", "range")
                        or k.startswith("x-")}}
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
            await self.app(scope, tapped, watched)
        finally:
            body = b"".join(chunks)
            entry["status"] = status.get("code")
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


def glossary(name: str, text: str, source: str) -> dict[str, Any]:
    """services/stt/app/profiles.py's reading of a file, at the depth the page
    sees: `heard = intended` is a replacement, a bare term a hotword, and a
    replacement missing either side is refused with its line number."""
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
            replacements[heard] = intended
        else:
            hotwords.append(line)
    return {"name": name, "source": source, "text": text, "replacements": replacements,
            "hotwords": hotwords, "rejected": rejected, "writable": source != "builtin"}


def glossary_summary(g: dict[str, Any]) -> dict[str, Any]:
    return {"name": g["name"], "source": g["source"],
            "terms": len(g["replacements"]) + len(g["hotwords"]),
            "replacements": len(g["replacements"]), "hotwords": len(g["hotwords"]),
            "writable": g["writable"]}


GLOSS_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def stt_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return health_body(world, "stt", {
            "status": "ok", "model": "parakeet",
            "models": [{"id": "parakeet", "family": "parakeet", "default": True,
                        "languages": PARAKEET_LANGUAGES, "accepts_language": False,
                        "accepts_boost": True, "can_translate": False, "can_stream": False}],
            "model_id": "nvidia/parakeet-tdt-0.6b-v3", "accepts_vocabulary": True,
            "translations": False, "streaming": False, "hotwords": True,
            "glossaries": sorted(world.glossaries), "vad": True, "threads": 4,
            "max_concurrent": 1, "host_label": "e2e-fake"})

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
        text = world.transcript
        segments, words = timed(text, seconds)
        headers = {"x-stt-engine": "parakeet-tdt-0.6b-v3", "x-realtime-factor": "8.8",
                   "x-audio-seconds": f"{seconds:.2f}"}
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
    async def transcribe(request: Request) -> dict[str, Any]:
        _, seconds = await heard(request)
        return {"text": world.transcript, "raw": world.transcript.lower(), "repaired": [],
                "model": "parakeet", "audio_seconds": round(seconds, 2),
                "speech_seconds": round(seconds * 0.86, 2), "compute_seconds": round(seconds / 8.8, 2),
                "realtime_factor": 8.8}

    @app.get("/glossaries")
    async def list_glossaries() -> dict[str, Any]:
        return {"glossaries": [glossary_summary(g) for _, g in sorted(world.glossaries.items())],
                "writable": True, "default": [], "builtin_dir": "/app/glossaries",
                "custom_dir": "/glossaries"}

    @app.get("/glossaries/{name}")
    async def get_glossary(name: str) -> Response:
        g = world.glossaries.get(name.strip().lower())
        if g is None:
            return JSONResponse({"detail": f"no glossary profile called {name!r}"}, status_code=404)
        return JSONResponse(glossary_summary(g) | {"replacements": g["replacements"],
                                                   "hotwords": g["hotwords"], "text": g["text"],
                                                   "path": f"/glossaries/{g['name']}.txt"})

    @app.put("/glossaries/{name}")
    async def put_glossary(name: str, request: Request) -> Response:
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
        existing = world.glossaries.get(name)
        if existing is not None and existing["source"] == "builtin":
            return JSONResponse({"detail": f"{name!r} is built in and cannot be written. Copy it to a "
                                           "new name and edit that."}, status_code=409)
        if len(text.encode()) > 65536:
            return JSONResponse({"detail": "that profile is over 64 KB"}, status_code=413)
        g = glossary(name, text, "custom")
        if g["rejected"] and not force:
            accepted = len(g["replacements"]) + len(g["hotwords"])
            return JSONResponse({"detail": {
                "message": f"{len(g['rejected'])} line(s) rejected; nothing was written. "
                           f"{accepted} term(s) would have been accepted.",
                "accepted": accepted, "rejected": g["rejected"]}}, status_code=400)
        world.glossaries[name] = g
        return JSONResponse(glossary_summary(g) | {"path": f"/glossaries/{name}.txt", "forced": force,
                                                   "created": existing is None},
                            status_code=200 if existing else 201)

    @app.delete("/glossaries/{name}")
    async def delete_glossary(name: str) -> Response:
        g = world.glossaries.get(name.strip().lower())
        if g is None:
            return JSONResponse({"detail": f"no glossary profile called {name!r}"}, status_code=404)
        if g["source"] == "builtin":
            return JSONResponse({"detail": f"{name!r} is built in and cannot be deleted"}, status_code=409)
        del world.glossaries[g["name"]]
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
        async def frames():
            data = pcm(seconds) if fmt == "pcm" else wav(seconds)
            step = 24000  # half a second of 24 kHz s16le mono per delta
            for i in range(0, len(data), step):
                chunk = base64.b64encode(data[i:i + step]).decode()
                yield f"data: {json.dumps({'type': 'speech.audio.delta', 'audio': chunk})}\n\n"
                await asyncio.sleep(0.12)
            done = {"type": "speech.audio.done",
                    "usage": {"input_tokens": len(text), "output_tokens": len(data) // 2,
                              "total_tokens": len(text) + len(data) // 2}}
            yield f"data: {json.dumps(done)}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})
    return audio_answer(fmt, seconds, rtf)


def tts_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return health_body(world, "tts", {
            "status": "ok", "voices": len(KOKORO_VOICES), "default_voice": "af_heart", "threads": 4,
            "host_label": "e2e-fake", "runlog": {"written": 0, "dropped": 0, "last_error": None},
            "realtime_factor": 2.79, "realtime_factor_samples": 12})

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


def seed_jobs(world: World) -> None:
    """A history the Jobs tab can draw at once: one of each audio state, a
    failure, and the two kinds that keep no audio here."""
    now = time.time()
    rows = [
        {"kind": "clone", "status": "done", "age": 7200, "audio_seconds": 42.4, "voice": "narrator"},
        {"kind": "clone", "status": "done", "age": 86400, "audio_seconds": 18.1, "voice": "narrator",
         "audio_deleted": True},
        {"kind": "clone", "status": "failed", "age": 10800, "voice": "narrator",
         "error": "the runner went away in the middle of segment 3"},
        {"kind": "speech", "status": "done", "age": 3600, "audio_seconds": 6.2, "voice": "af_heart",
         "engine": "kokoro", "service": "tts-stack"},
        {"kind": "transcribe", "status": "done", "age": 1800, "audio_seconds": 312.0, "voice": None,
         "engine": "parakeet", "service": "stt-stack"},
    ]
    for row in rows:
        created = now - row.pop("age")
        job = new_job(world, text=SEED_TEXT, voice=row.pop("voice"), engine=row.pop("engine", "chatterbox"),
                      created=created, scripted=False)
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
            scripted: bool = True, language: str | None = "en") -> dict[str, Any]:
    parts = sentences(text)
    job_id = str(uuid.uuid4())
    kind = "clone" if engine in CATALOGUE else ("transcribe" if engine == "parakeet" else "speech")
    job = {"id": job_id, "status": "queued", "created_at": created or time.time(), "kind": kind,
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


def long_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
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

    @app.post("/jobs", status_code=202)
    async def create_job(request: Request) -> Response:
        body = await request.json()
        text = str(body.get("text") or "")
        if body.get("segments"):
            text = " ".join(str(s.get("text") if isinstance(s, dict) else s) for s in body["segments"])
        if not text.strip():
            return JSONResponse({"detail": "provide either text or segments"}, status_code=400)
        engine = (body.get("model") or "chatterbox").strip().lower()
        if engine in ("tts-long", ""):
            engine = "chatterbox"
        job = new_job(world, text=text, voice=body.get("voice"), engine=engine,
                      language=body.get("language"))
        return JSONResponse({"id": job["id"], "status": "queued", "queued_ahead": 0,
                             "chunks": job["chunks"], "engine": engine,
                             "estimated_seconds": job["estimated_seconds"]}, status_code=202)

    @app.get("/jobs")
    async def list_jobs(request: Request) -> Response:
        q = request.query_params

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
        return JSONResponse({"jobs": keep[:limit], "counts": counts, "truncated": len(keep) > limit})

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> Response:
        job = world.jobs.get(job_id)
        if job is None:
            return JSONResponse({"detail": "no such job"}, status_code=404)
        return JSONResponse(public(job) | {"text": job.get("text"), "segments": job.get("segments")})

    @app.delete("/jobs/{job_id}")
    async def delete_job(job_id: str) -> Response:
        job = world.jobs.get(job_id)
        if job is None:
            return JSONResponse({"detail": "no such job"}, status_code=404)
        advance(job)
        if job["status"] in ("done", "failed", "cancelled"):
            del world.jobs[job_id]
            return JSONResponse({"id": job_id, "status": "deleted"})
        job.update(cancelled=True, status="cancelled", finished_at=time.time())
        if job["offsets"]:
            job.update(path=f"/out/{job_id}.wav", audio_seconds=job["offsets"][-1] + 1.0)
        return JSONResponse({"id": job_id, "status": "cancelling"})

    @app.get("/jobs/{job_id}/audio")
    async def job_audio(job_id: str) -> Response:
        job = world.jobs.get(job_id)
        if job is None:
            return JSONResponse({"detail": "no such job"}, status_code=404)
        advance(job)
        if job["status"] not in ("done", "cancelled") or not job.get("path"):
            return JSONResponse({"detail": f"job is {job['status']}"}, status_code=409)
        return Response(wav(min(30.0, job.get("audio_seconds") or 2.0)), media_type="audio/wav",
                        headers={"Content-Disposition": f'attachment; filename="{job_id}.wav"'})

    @app.delete("/jobs/{job_id}/audio")
    async def delete_job_audio(job_id: str) -> Response:
        job = world.jobs.get(job_id)
        if job is None:
            return JSONResponse({"detail": "no such job"}, status_code=404)
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
        if elapsed >= DOWNLOAD_SECONDS:
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
        """Connect, and reconnect after a reboot or a dropped socket, until stopped."""
        await asyncio.sleep(delay)
        while self.world.hub:
            ws_url = self.world.hub.replace("http", "ws", 1) + "/satellites/ws"
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
        async with httpx.AsyncClient(base_url=self.world.hub, timeout=10) as http:
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

    async def mic(self, seconds: float) -> None:
        """Microphone frames at real time: 20 ms, a quiet tone on every channel."""
        channels = self.caps["mic"]["channels"]
        tone = pcm(seconds, 16000, channels, pitch=220.0)
        step = 320 * channels * 2
        t0 = time.monotonic()
        for i, offset in enumerate(range(0, len(tone) - step + 1, step)):
            await self.ws.send(struct.pack("<BBBBIQ", 1, 0, channels, 0, i, 0) + tone[offset:offset + step])
            await asyncio.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.monotonic()))


class _Reconnect(Exception):
    def __init__(self, after: float) -> None:
        self.after = after


# ---- the control API ---------------------------------------------------------------


def control_app(world: World) -> FastAPI:
    app = FastAPI(openapi_url=None)

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
        world.reset()
        return {"ok": True}

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

    @app.post("/__fake/jobs")
    async def seed_job(request: Request) -> dict[str, Any]:
        body = await request.json()
        job = new_job(world, text=body.pop("text", SEED_TEXT), voice=body.pop("voice", "narrator"),
                      engine=body.pop("engine", "chatterbox"), scripted=body.pop("scripted", True))
        job.update(body)
        return public(job)

    @app.get("/__fake/metube")
    async def metube() -> dict[str, Any]:
        return {url: {k: v for k, v in e.items() if not k.startswith("_")} for url, e in world.metube.items()}

    @app.post("/__fake/satellites/connect")
    async def connect_all(request: Request) -> dict[str, Any]:
        body = await request.json()
        world.hub = body["hub"].rstrip("/")
        keys = body.get("satellites") or list(SCRIPTED)
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
        s.spawn(s.mic(float((await request.json()).get("seconds", 6.0))))
        return {"ok": True}

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
    for name in ("stt", "tts", "long", "metube", "control"):
        parser.add_argument(f"--{name}-port", type=int, required=True)
    args = parser.parse_args(argv)
    world = World(args.repo)
    asyncio.run(serve(world, {name: getattr(args, f"{name}_port")
                              for name in ("stt", "tts", "long", "metube", "control")}))
