"""A fake Calliope gateway: the routes the integration uses, on 127.0.0.1.

Shapes are copied from the real services (services/satellites/app/main.py,
services/gateway/README.md, services/tts and services/stt READMEs), including
the OpenAI error envelope and the event stream's ": connected" and keepalive
comments.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

KITCHEN_ID = "94b97e7b8be8"
BEDROOM_ID = "0a1b2c3d4e5f"
PENDING_ID = "aabbccddeeff"

KORVO_BUTTONS = ["vol_up", "vol_down", "set", "play", "mode", "rec"]


def satellite(
    sid: str, name: str, *, adopted: bool = True, online: bool = True, **config: Any
) -> dict[str, Any]:
    """A satellite as GET /satellites describes it."""
    cfg = {
        "volume": 60,
        "mic_gain_db": 30.0,
        "mic_enabled": True,
        "speaker_enabled": True,
        "local_volume_buttons": True,
        "lights_enabled": True,
        "buttons": {"play": {"press": "ptt"}, "set": {"press": "stop"}},
    } | config
    return {
        "id": sid,
        "name": name,
        "adopted": adopted,
        "online": online,
        "model": "korvo-v1.1",
        "firmware": "0.4.2",
        "address": "192.168.1.50" if online else None,
        "connected_at": 1_700_000_000.0 if online else None,
        "last_seen": None,
        "config": cfg if adopted else None,
        "status": (
            {
                "rssi": -58,
                "muted": False,
                **{
                    k: cfg[k]
                    for k in (
                        "volume",
                        "mic_enabled",
                        "speaker_enabled",
                        "lights_enabled",
                    )
                },
            }
            if online
            else {}
        ),
        "caps": {"buttons": KORVO_BUTTONS, "lights": 12} if online else {},
        "ota": None,
        "listening": None,
        "earcons": None,
        "wake_words": ["hey_jarvis", "lumos"] if adopted else [],
    }


def envelope(status: int, message: str, code: str | None = None) -> web.Response:
    """The OpenAI-shaped error every Calliope route answers with."""
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": code,
            }
        },
        status=status,
    )


class FakeCalliope:
    """State the tests set and read, behind real HTTP."""

    def __init__(self) -> None:
        """A kitchen satellite (adopted), and one waiting to be adopted."""
        self.api_key: str | None = None
        self.satellites: dict[str, dict[str, Any]] = {
            KITCHEN_ID: satellite(KITCHEN_ID, "kitchen", lights_enabled=False),
            PENDING_ID: satellite(PENDING_ID, "", adopted=False),
        }
        self.words: list[dict[str, Any]] = [
            {
                "name": "hey_jarvis",
                "threshold": 0.5,
                "satellites": ["*"],
                "state": "ready",
                "error": None,
                "mode": "command",
            },
            {
                "name": "lumos",
                "threshold": 0.6,
                "satellites": [KITCHEN_ID],
                "state": "ready",
                "error": None,
                "mode": "trigger",
            },
        ]
        self.voices = [
            "af_heart",
            "am_onyx",
            "bf_emma",
            "bm_george",
            "ef_dora",
            "pf_dora",
            "pm_alex",
            "pm_santa",
            "zf_xiaobei",
        ]
        self.stt_model = "parakeet"
        self.health_body: dict[str, Any] | None = None
        self.transcript = "turn on the kitchen lights"
        self.ptt_route = False
        self.events_status: int | None = None  # answer /satellites/events with this
        self.requests: list[tuple[str, str, Any]] = []
        self.stream_count = 0
        self._queues: set[asyncio.Queue] = set()
        self._opened = asyncio.Condition()
        self._server: TestServer | None = None
        self.url = ""

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Listen on a free port."""
        app = web.Application(middlewares=[self._auth])
        r = app.router
        r.add_get("/health", self._health)
        r.add_get("/v1/models", self._models)
        r.add_get("/voices", self._voices)
        r.add_post("/v1/audio/transcriptions", self._transcribe)
        r.add_post("/v1/audio/speech", self._speech)
        r.add_get("/satellites", self._list)
        r.add_get("/satellites/events", self._events)
        r.add_get("/satellites/wake-words", self._wake_words)
        r.add_get("/satellites/{sid}", self._get)
        r.add_patch("/satellites/{sid}", self._patch)
        r.add_post("/satellites/{sid}/{action}", self._action)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        self.url = str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        """End every stream, then close."""
        self.drop_streams()
        await asyncio.sleep(0)
        if self._server is not None:
            await self._server.close()

    # -- what tests do -----------------------------------------------------

    def push(self, event: dict[str, Any]) -> None:
        """Publish on every open event stream, as Hub.publish does."""
        event = {"at": 1_700_000_100.0} | event
        for q in list(self._queues):
            q.put_nowait(event)

    def drop_streams(self) -> None:
        """End every open event stream, as a hub restart would."""
        for q in list(self._queues):
            q.put_nowait(None)

    async def wait_streams(self, count: int, timeout: float = 5) -> None:
        """Until `count` event streams have been opened in all."""
        async with asyncio.timeout(timeout), self._opened:
            await self._opened.wait_for(lambda: self.stream_count >= count)

    def calls(self, method: str, path: str) -> list[Any]:
        """The bodies of every request to one route."""
        return [body for m, p, body in self.requests if m == method and p == path]

    # -- routes --------------------------------------------------------------

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if (
            self.api_key
            and request.path != "/health"
            and request.headers.get("Authorization") != f"Bearer {self.api_key}"
        ):
            return envelope(401, "Invalid API key", "invalid_api_key")
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        if self.health_body is not None:
            return web.json_response(self.health_body)
        return web.json_response(
            {
                "status": "ok",
                "gateway": "ok",
                "backends": {
                    "stt": {
                        "url": "http://stt-stack:8000",
                        "reachable": True,
                        "http_status": 200,
                        "health": {
                            "status": "ok",
                            "model": self.stt_model,
                            "threads": 8,
                        },
                    },
                    "tts": {
                        "url": "http://tts-stack:8001",
                        "reachable": True,
                        "http_status": 200,
                        "health": {
                            "status": "ok",
                            "voices": len(self.voices),
                            "default_voice": "bm_george",
                        },
                    },
                },
            }
        )

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"object": "list", "data": [{"id": "parakeet"}, {"id": "kokoro"}]}
        )

    async def _voices(self, request: web.Request) -> web.Response:
        return web.json_response({"voices": self.voices, "openai_aliases": {}})

    async def _transcribe(self, request: web.Request) -> web.Response:
        form = await request.post()
        fields = {
            k: (v if isinstance(v, str) else v.file.read()) for k, v in form.items()
        }
        self.requests.append(("POST", "/v1/audio/transcriptions", fields))
        if "language" in fields and self.stt_model == "parakeet":
            return envelope(
                400,
                "Unsupported parameter: 'language' is not supported by the "
                "'parakeet' engine",
                "unsupported_parameter",
            )
        return web.json_response(
            {"text": self.transcript}, headers={"x-stt-engine": self.stt_model}
        )

    async def _speech(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(("POST", "/v1/audio/speech", body))
        if body.get("voice") not in self.voices:
            return envelope(
                400, f"Unknown voice {body.get('voice')!r}", "invalid_value"
            )
        # 0.1 s of 24 kHz s16le silence per request.
        return web.Response(body=b"\x00\x00" * 2400, content_type="audio/pcm")

    async def _list(self, request: web.Request) -> web.Response:
        self.requests.append(("GET", "/satellites", None))
        return web.json_response(
            {"satellites": list(copy.deepcopy(self.satellites).values())}
        )

    async def _wake_words(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "available": ["alexa", "hey_jarvis", "lumos"],
                "words": self.words,
                "custom": ["lumos"],
                "load_error": None,
            }
        )

    async def _events(self, request: web.Request) -> web.StreamResponse:
        if self.events_status is not None:
            return envelope(self.events_status, "unavailable")
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        await resp.write(b": connected\n\n")
        q: asyncio.Queue = asyncio.Queue()
        self._queues.add(q)
        async with self._opened:
            self.stream_count += 1
            self._opened.notify_all()
        try:
            while (event := await q.get()) is not None:
                await resp.write(f"data: {json.dumps(event)}\n\n".encode())
                await resp.write(b": keepalive\n\n")
        finally:
            self._queues.discard(q)
        return resp

    def _find(self, sid: str) -> dict[str, Any] | None:
        if sid in self.satellites:
            return self.satellites[sid]
        return next((s for s in self.satellites.values() if s["name"] == sid), None)

    async def _get(self, request: web.Request) -> web.Response:
        sat = self._find(request.match_info["sid"])
        self.requests.append(("GET", f"/satellites/{request.match_info['sid']}", None))
        if sat is None:
            return envelope(404, "no single satellite matches", "satellite_not_found")
        return web.json_response(sat)

    async def _patch(self, request: web.Request) -> web.Response:
        body = await request.json()
        sid = request.match_info["sid"]
        self.requests.append(("PATCH", f"/satellites/{sid}", body))
        sat = self._find(sid)
        if sat is None or not sat["adopted"]:
            return envelope(404, "no adopted satellite with that id")
        if "name" in body:
            sat["name"] = body.pop("name")
        sat["config"].update(body)
        return web.json_response(sat)

    async def _action(self, request: web.Request) -> web.Response:
        sid, action = request.match_info["sid"], request.match_info["action"]
        body = await request.json() if request.can_read_body else None
        self.requests.append(("POST", f"/satellites/{sid}/{action}", body))
        if action == "ptt" and not self.ptt_route:
            # The gateway routes only what it lists (SATELLITES_PATHS).
            return envelope(
                404, f"Invalid URL (POST /satellites/{sid}/ptt).", "unknown_url"
            )
        if action not in ("identify", "say", "tone", "flush", "ptt"):
            return envelope(404, "Invalid URL", "unknown_url")
        sat = self._find(sid)
        if sat is None:
            return envelope(404, "no single satellite matches", "satellite_not_found")
        if not sat["online"]:
            return envelope(
                409, f"satellite {sid} is not connected", "satellite_offline"
            )
        if action in ("say", "tone") and not sat["config"]["speaker_enabled"]:
            return envelope(
                409,
                f"satellite {sid} has its speaker turned off "
                "(speaker_enabled is false)",
                "speaker_disabled",
            )
        return web.Response(status=204)
