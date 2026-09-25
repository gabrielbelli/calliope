"""voice-nodes: the hub for thin audio devices.

A node is a microphone array, a speaker and a ring of lights on Wi-Fi. It makes
no decisions: it streams its microphones here and plays, lights and reports
whatever it is told. This service adopts nodes, holds their settings, and is
the one place audio and firmware reach them from.

    WS    /nodes/ws                      the device connection (protocol: README)
    GET   /nodes                         every node seen since start, adopted or not
    GET   /nodes/events                  server-sent events: buttons, status, updates
    GET   /nodes/firmware                uploaded images
    POST  /nodes/firmware?model&version  raw body: a firmware image
    DELETE /nodes/firmware/{sha256}
    POST  /nodes/ota                     {"node": id|name|"all", "sha256": ...}
    GET   /nodes/{id}
    PATCH /nodes/{id}                    name and config
    POST  /nodes/{id}/adopt | forget | identify | reboot | lights | tone | say
                   | flush | set-hub
    GET   /nodes/{id}/listen?seconds=5   a WAV of the raw microphone channels

EVERYTHING IS UNDER /nodes, INCLUDING THE SOCKET, because the gateway mounts
backend paths flat and never rewrites them. The gateway relays /nodes/ws as a
WebSocket and forwards the rest as ordinary routes.

THE DEVICE SOCKET IS NOT BEHIND AN API KEY, AND THAT IS DELIBERATE. A node
cannot hold a gateway key it was never given, and a key baked into firmware
would be a key in every flash dump. The socket is answered for anyone; what a
connection may DO is decided by the adoption token. An unadopted connection
can say hello and receive "pending", nothing else: no microphone audio is
accepted from it and nothing is sent to it but that one word, until someone
with access to the API adopts it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import numpy as np
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from voice_common import auth, errors, health
from voice_common import logging as voice_logging
from voice_common.errors import ApiError

from . import audio
from .store import DEFAULT_CONFIG, Store

log = voice_logging.setup("voice-nodes", "NODES")

DATA_DIR = Path(os.environ.get("NODES_DATA_DIR", "/data"))
TTS_URL = os.environ.get("NODES_TTS_URL", "").rstrip("/")
TTS_VOICE = os.environ.get("NODES_TTS_VOICE", "bm_george")
MAX_FIRMWARE = 4 * 1024 * 1024  # one OTA slot on the Korvo

FRAME_MIC, FRAME_SPEAKER, FRAME_FIRMWARE = 1, 2, 3
HEADER = 16
# Under the node's own ceiling: the Arduino WebSockets library drops the whole
# connection on any frame over 15 KB (WEBSOCKETS_MAX_DATA_SIZE, not
# overridable on ESP32). 16 KB chunks killed the first real update 17 ms in.
OTA_CHUNK = 8 * 1024
SPEAKER_CHUNK_MS = 20
SPEAKER_LEAD_S = 0.3  # how far ahead of real time playback is kept


def node_id(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


# ---- live connections -------------------------------------------------------


class Session:
    """One connected device. Starlette sockets are not safe to send on from two
    coroutines at once, and the speaker loop, the OTA pump and API calls all
    send, so every send takes the lock."""

    def __init__(self, ws: WebSocket, hello: dict):
        self.ws = ws
        self.lock = asyncio.Lock()
        self.id = node_id(hello.get("id", ""))
        self.connected_at = time.time()
        # Behind the gateway every peer is the gateway; it passes the device's
        # own address along. Display only -- nothing is decided on it.
        self.address = ws.headers.get("x-forwarded-for") or (ws.client.host if ws.client else None)
        self.adopted = False
        self.status: dict = {}
        self.taps: set[asyncio.Queue] = set()
        self.speaker: asyncio.Queue[bytes] = asyncio.Queue()
        self.ota: dict | None = None
        self.update(hello)

    def update(self, hello: dict) -> None:
        self.hello = hello
        self.model = hello.get("model", "unknown")
        self.fw = hello.get("fw", "unknown")
        self.caps = hello.get("caps", {})

    async def send_json(self, obj: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(obj))

    async def send_bytes(self, data: bytes) -> None:
        async with self.lock:
            await self.ws.send_bytes(data)

    @property
    def spk_rate(self) -> int:
        return self.caps.get("speaker", {}).get("rate", 48000)

    @property
    def mic_rate(self) -> int:
        return self.caps.get("mic", {}).get("rate", 16000)

    @property
    def mic_channels(self) -> int:
        return self.caps.get("mic", {}).get("channels", 4)

    async def speaker_loop(self) -> None:
        seq = 0
        chunk = self.spk_rate * 2 * SPEAKER_CHUNK_MS // 1000
        while True:
            pcm = await self.speaker.get()
            start, sent = time.monotonic(), 0.0
            for off in range(0, len(pcm), chunk):
                piece = pcm[off:off + chunk]
                await self.send_bytes(struct.pack("<BBBBIQ", FRAME_SPEAKER, 0, 1, 0, seq, 0) + piece)
                seq = (seq + 1) & 0xFFFFFFFF
                sent += len(piece) / 2 / self.spk_rate
                ahead = sent - (time.monotonic() - start)
                if ahead > SPEAKER_LEAD_S:
                    await asyncio.sleep(ahead - SPEAKER_LEAD_S)


class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.sessions: dict[str, Session] = {}
        self.seen: dict[str, dict] = {}  # unadopted nodes, in memory only
        self.listeners: set[asyncio.Queue] = set()

    def publish(self, event: dict) -> None:
        event = {"at": time.time()} | event
        for q in list(self.listeners):
            if q.qsize() < 256:
                q.put_nowait(event)

    def describe(self, nid: str) -> dict:
        rec = self.store.nodes.get(nid)
        s = self.sessions.get(nid)
        seen = self.seen.get(nid, {})
        return {
            "id": nid,
            "name": rec.name if rec else (s.hello.get("name") if s else "") or "",
            "adopted": rec is not None,
            "online": s is not None,
            "model": s.model if s else (rec.model if rec else seen.get("model")),
            "firmware": s.fw if s else seen.get("fw"),
            "address": s.address if s else None,
            "connected_at": s.connected_at if s else None,
            "last_seen": seen.get("last_seen"),
            "config": rec.config if rec else None,
            "status": s.status if s else {},
            "caps": s.caps if s else {},
            "ota": {k: v for k, v in s.ota.items() if k != "image"} if s and s.ota else None,
        }

    def find(self, ref: str) -> list[str]:
        """A node id (with or without colons), a node name, or "all"."""
        if ref == "all":
            return [n for n in self.sessions if n in self.store.nodes]
        nid = node_id(ref)
        if nid in self.store.nodes or nid in self.sessions or nid in self.seen:
            return [nid]
        return [n.id for n in self.store.nodes.values() if n.name == ref]

    def resolve(self, ref: str) -> str:
        """One node, by id or name, or a 404."""
        found = [n for n in self.find(ref) if n != "all"] if ref != "all" else []
        if len(found) != 1:
            raise ApiError(404, f"no single node matches {ref!r}", code="node_not_found")
        return found[0]

    def session(self, ref: str, *, adopted: bool = True) -> Session:
        nid = self.resolve(ref)
        s = self.sessions.get(nid)
        if s is None:
            raise ApiError(409, f"node {nid} is not connected", code="node_offline")
        if adopted and not s.adopted:
            raise ApiError(409, f"node {nid} is not adopted", code="node_not_adopted")
        return s

    async def greet(self, s: Session) -> None:
        """Answer a hello: welcome with the config, or pending."""
        rec = self.store.nodes.get(s.id)
        token = s.hello.get("token") or None
        if rec and rec.accepts(token):
            s.adopted = True
            await s.send_json({"type": "welcome", "name": rec.name, "config": rec.config})
            self.publish({"type": "online", "node": s.id, "name": rec.name, "firmware": s.fw})
        else:
            s.adopted = False
            if rec and token:
                log.warning("node %s presented a token that does not match its adoption", s.id)
            self.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time()}
            await s.send_json({"type": "pending"})
            self.publish({"type": "pending", "node": s.id, "address": s.address})


hub: Hub


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global hub
    hub = Hub(Store(DATA_DIR))
    log.info("%d adopted nodes, %d firmware images in %s",
             len(hub.store.nodes), len(hub.store.firmware), DATA_DIR)
    yield


app = FastAPI(title="voice-nodes", lifespan=lifespan)
errors.install_errors(app)
health.install_health(app, details=lambda: {
    "nodes": {"online": len(hub.sessions), "adopted": len(hub.store.nodes),
              "pending": sum(1 for s in hub.sessions.values() if not s.adopted)},
    "tts": TTS_URL or None,
})
auth.install(app, "NODES_API_KEYS")


# ---- the device socket -------------------------------------------------------


@app.websocket("/nodes/ws")
async def node_socket(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=1008)
        return
    if first.get("type") != "hello" or not node_id(first.get("id", "")):
        await ws.close(code=1008)
        return

    s = Session(ws, first)
    old = hub.sessions.get(s.id)
    if old is not None:  # the device reconnected before the old socket timed out
        await old.ws.close(code=1012)
    hub.sessions[s.id] = s
    player = asyncio.create_task(s.speaker_loop())
    log.info("node %s connected from %s (%s, firmware %s)", s.id, s.address, s.model, s.fw)
    try:
        await hub.greet(s)
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                data = msg["bytes"]
                if s.adopted and data and data[0] == FRAME_MIC:
                    for q in list(s.taps):
                        q.put_nowait(data[HEADER:])
            elif msg.get("text") is not None:
                await on_message(s, json.loads(msg["text"]))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        player.cancel()
        if s.ota and s.ota.get("state") in ("requested", "started", "progress"):
            s.ota.update(state="failed", error="disconnected mid-transfer")
            hub.publish({"type": "ota", "node": s.id, "state": "failed",
                         "error": "disconnected mid-transfer"})
            log.warning("node %s disconnected during an update", s.id)
        if hub.sessions.get(s.id) is s:
            del hub.sessions[s.id]
        hub.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time()}
        hub.publish({"type": "offline", "node": s.id})
        log.info("node %s disconnected", s.id)


async def on_message(s: Session, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "hello":  # sent again after adoption, with the new token
        s.update(msg)
        await hub.greet(s)
    elif kind == "status":
        s.status = {k: v for k, v in msg.items() if k != "type"}
        hub.publish({"type": "status", "node": s.id, "status": s.status})
    elif kind == "button" and s.adopted:
        hub.publish({"type": "button", "node": s.id, "button": msg.get("button"),
                     "action": msg.get("action"), "held_ms": msg.get("held_ms")})
    elif kind == "ota_next" and s.ota:
        off = int(msg.get("offset", 0))
        image = s.ota["image"]
        await s.send_bytes(struct.pack("<BBBBI", FRAME_FIRMWARE, 0, 0, 0, off)
                           + image[off:off + OTA_CHUNK])
    elif kind == "ota":
        state = msg.get("state")
        if s.ota is None:
            s.ota = {"version": msg.get("version")}
        s.ota.update({"state": state, "pct": msg.get("pct"), "error": msg.get("error")})
        if state in ("failed", "verified"):
            s.ota.pop("image", None)
        hub.publish({"type": "ota", "node": s.id, "state": state, "pct": msg.get("pct"),
                     "version": msg.get("version"), "error": msg.get("error")})
        log.info("node %s ota %s %s", s.id, state, msg.get("error") or "")


# ---- nodes -------------------------------------------------------------------


class AdoptBody(BaseModel):
    name: str = Field(default="", max_length=64)


class ConfigBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    volume: int | None = Field(default=None, ge=0, le=100)
    mic_gain_db: float | None = Field(default=None, ge=0, le=37.5)
    mic_enabled: bool | None = None
    speaker_enabled: bool | None = None
    local_volume_buttons: bool | None = None


class LightsBody(BaseModel):
    mode: str = Field(pattern="^(off|solid|pulse|spin|pixels)$")
    color: tuple[int, int, int] = (255, 255, 255)
    brightness: int = Field(default=64, ge=0, le=255)
    pixels: list[tuple[int, int, int]] | None = None


class ToneBody(BaseModel):
    frequency: float = Field(default=440, ge=50, le=8000)
    seconds: float = Field(default=1.0, gt=0, le=10)


class SayBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = None


class HubBody(BaseModel):
    url: str = Field(pattern=r"^wss?://[^/\s]+$", max_length=120)


class OtaBody(BaseModel):
    node: str
    sha256: str = Field(pattern="^[0-9a-f]{64}$")


@app.get("/nodes")
async def list_nodes() -> dict:
    ids = set(hub.store.nodes) | set(hub.sessions) | set(hub.seen)
    return {"nodes": sorted((hub.describe(n) for n in ids),
                            key=lambda d: (not d["adopted"], d["name"], d["id"]))}


@app.get("/nodes/events")
async def events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    hub.listeners.add(q)

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            hub.listeners.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/nodes/firmware")
async def list_firmware() -> dict:
    return {"firmware": sorted((vars(f) for f in hub.store.firmware.values()),
                               key=lambda f: -f["uploaded_at"])}


@app.post("/nodes/firmware")
async def upload_firmware(request: Request, model: str = Query(..., max_length=64),
                          version: str = Query("unknown", max_length=64)) -> dict:
    image = await request.body()
    if not image:
        raise ApiError(400, "empty body: send the .bin as the request body")
    if len(image) > MAX_FIRMWARE:
        raise ApiError(413, f"image is {len(image)} bytes; an OTA slot holds {MAX_FIRMWARE}")
    if image[0] != 0xE9:  # every ESP32 app image starts with this magic byte
        raise ApiError(400, "not an ESP32 application image (first byte is not 0xE9)")
    fw = hub.store.add_firmware(image, model, version)
    log.info("firmware %s stored: %s %s, %d bytes", fw.sha256[:12], model, version, fw.size)
    return vars(fw)


@app.delete("/nodes/firmware/{sha256}")
async def delete_firmware(sha256: str) -> Response:
    if not hub.store.delete_firmware(sha256):
        raise ApiError(404, "no such firmware")
    return Response(status_code=204)


@app.post("/nodes/ota")
async def start_ota(body: OtaBody) -> dict:
    fw = hub.store.firmware.get(body.sha256)
    if fw is None:
        raise ApiError(404, "no such firmware")
    targets = hub.find(body.node)
    if not targets:
        raise ApiError(404, f"no node matches {body.node!r}")
    image = hub.store.firmware_bytes(fw.sha256)
    started, skipped = [], {}
    for nid in targets:
        s = hub.sessions.get(nid)
        if s is None or not s.adopted:
            skipped[nid] = "offline or not adopted"
        elif s.model != fw.model:
            skipped[nid] = f"model {s.model}, image is for {fw.model}"
        elif s.ota and s.ota.get("state") in ("requested", "started", "progress"):
            skipped[nid] = "already updating"
        else:
            s.ota = {"image": image, "sha256": fw.sha256, "version": fw.version, "state": "requested"}
            await s.send_json({"type": "ota", "size": fw.size, "sha256": fw.sha256,
                               "version": fw.version})
            started.append(nid)
    return {"started": started, "skipped": skipped}


@app.get("/nodes/{nid}")
async def get_node(nid: str) -> dict:
    return hub.describe(hub.resolve(nid))


@app.post("/nodes/{nid}/adopt")
async def adopt(nid: str, body: AdoptBody) -> dict:
    s = hub.session(nid, adopted=False)
    name = body.name or f"node-{s.id[-4:]}"
    token = hub.store.adopt(s.id, name, s.model)
    hub.seen.pop(s.id, None)
    await s.send_json({"type": "adopt", "token": token, "name": name})
    log.info("node %s adopted as %r", s.id, name)
    return hub.describe(s.id)


@app.post("/nodes/{nid}/forget")
async def forget(nid: str) -> Response:
    nid = hub.resolve(nid)
    s = hub.sessions.get(nid)
    hub.store.forget(nid)
    if s is not None:
        s.adopted = False
        await s.send_json({"type": "forget"})
    return Response(status_code=204)


@app.patch("/nodes/{nid}")
async def configure(nid: str, body: ConfigBody) -> dict:
    nid = hub.resolve(nid)
    rec = hub.store.nodes.get(nid)
    if rec is None:
        raise ApiError(404, "no adopted node with that id")
    change = body.model_dump(exclude_none=True)
    if "name" in change:
        rec.name = change["name"]
    cfg = {k: v for k, v in change.items() if k in DEFAULT_CONFIG}
    rec.config.update(cfg)
    hub.store.save_nodes()
    s = hub.sessions.get(nid)
    if s is not None and s.adopted:
        await s.send_json({"type": "config", **cfg, **({"name": rec.name} if "name" in change else {})})
    return hub.describe(nid)


@app.post("/nodes/{nid}/identify")
async def identify(nid: str) -> Response:
    # Allowed before adoption: telling identical boxes apart is what it is for.
    await hub.session(nid, adopted=False).send_json({"type": "identify", "seconds": 5})
    return Response(status_code=204)


@app.post("/nodes/{nid}/reboot")
async def reboot(nid: str) -> Response:
    await hub.session(nid).send_json({"type": "reboot"})
    return Response(status_code=204)


@app.post("/nodes/{nid}/set-hub")
async def set_hub(nid: str, body: HubBody) -> Response:
    """Point the node at another hub. It saves the address and reboots; the
    other hub sees it as pending, with no adoption carried over."""
    await hub.session(nid).send_json({"type": "set_hub", "url": body.url})
    return Response(status_code=204)


@app.post("/nodes/{nid}/lights")
async def lights(nid: str, body: LightsBody) -> Response:
    await hub.session(nid).send_json({"type": "lights", **body.model_dump(exclude_none=True)})
    return Response(status_code=204)


@app.post("/nodes/{nid}/tone")
async def tone(nid: str, body: ToneBody) -> Response:
    s = hub.session(nid)
    s.speaker.put_nowait(audio.tone(body.frequency, body.seconds, s.spk_rate))
    return Response(status_code=204)


@app.post("/nodes/{nid}/say")
async def say(nid: str, body: SayBody) -> Response:
    s = hub.session(nid)
    if not TTS_URL:
        raise ApiError(503, "NODES_TTS_URL is not set, so there is no voice to speak with")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{TTS_URL}/v1/audio/speech", json={
            "model": "kokoro", "voice": body.voice or TTS_VOICE, "input": body.text,
            "response_format": "pcm"})
    if r.status_code != 200:
        raise ApiError(502, f"tts answered {r.status_code}: {r.text[:200]}")
    # Kokoro's pcm is 24 kHz mono s16le (voice_common.audio.SAMPLE_RATE).
    s.speaker.put_nowait(audio.resample(r.content, 24000, s.spk_rate))
    return Response(status_code=204)


@app.post("/nodes/{nid}/flush")
async def flush(nid: str) -> Response:
    s = hub.session(nid)
    while not s.speaker.empty():
        s.speaker.get_nowait()
    await s.send_json({"type": "flush"})
    return Response(status_code=204)


@app.get("/nodes/{nid}/listen")
async def listen(nid: str, seconds: float = Query(5, gt=0, le=60),
                 channel: int | None = Query(None, ge=0, le=7)) -> Response:
    s = hub.session(nid)
    if s.status.get("muted"):
        raise ApiError(409, "the node is muted at the device; only its REC button unmutes it")
    rate, ch = s.mic_rate, s.mic_channels
    want = int(seconds * rate) * ch * 2
    q: asyncio.Queue = asyncio.Queue()
    s.taps.add(q)
    buf = bytearray()
    try:
        deadline = time.monotonic() + seconds + 5
        while len(buf) < want:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ApiError(504, "the node stopped sending audio (mic disabled or muted?)")
            buf += await asyncio.wait_for(q.get(), timeout=left)
    except asyncio.TimeoutError:
        raise ApiError(504, "the node sent no audio in time (mic disabled or muted?)")
    finally:
        s.taps.discard(q)
    pcm = bytes(buf[:want])
    if channel is not None:
        if channel >= ch:
            raise ApiError(400, f"the node has {ch} channels")
        pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, ch)[:, channel].tobytes()
        ch = 1
    return Response(audio.wav(pcm, rate, ch), media_type="audio/wav",
                    headers={"Content-Disposition": f'attachment; filename="{s.id}.wav"'})
