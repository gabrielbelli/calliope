"""A fake Calliope gateway: the routes the integration uses, on 127.0.0.1.

Shapes are copied from the real services (services/satellites/app/main.py,
services/gateway/README.md, services/tts and services/stt READMEs), including
the OpenAI error envelope and the event stream's ": connected" and keepalive
comments.

It holds a key as the gateway does: every route but /health answers 401
without it, and 403 insufficient_scope, with the RFC 6750 challenge, for a
route whose scope the key lacks. The key starts with the home-assistant
preset's scopes, so the whole suite proves the integration needs nothing
outside that preset.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
import string
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.calliope.api import KEY_PREFIX, _checksum

KITCHEN_ID = "020000000001"  # an ESP32-Korvo
LOUNGE_ID = "020000000002"  # a Raspberry Pi
BEDROOM_ID = "0a1b2c3d4e5f"
PENDING_ID = "aabbccddeeff"

KORVO_BUTTONS = ["vol_up", "vol_down", "set", "play", "mode", "rec", "key1"]

# hello.caps as the Korvo firmware sends them (clients/korvo-satellite,
# hub.cpp), with a signing key.
KORVO_CAPS: dict[str, Any] = {
    "mic": {"rate": 16000, "channels": 4, "format": "s16le"},
    "speaker": {"rate": 48000, "channels": 1, "format": "s16le"},
    "lights": 12,
    "light_modes": ["off", "solid", "pulse", "spin", "pixels", "listen"],
    "buttons": KORVO_BUTTONS,
    "actions": ["mute", "volume_up", "volume_down", "lights", "dimmer", "brighter"],
    "ota_key": "k1",
    "earcons": {"max": 16, "max_bytes": 524288, "rate": 48000},
    "duck": True,
}
# The Pi agent's, as the spec's section 2.1 has them.
PI_CAPS: dict[str, Any] = {
    "speaker": {"rate": 44100, "channels": 1, "format": "s16le"},
    "media": {"rate": 44100, "channels": 2, "format": "s16le"},
    "mic": {
        "rate": 16000,
        "channels": 2,
        "format": "s16le",
        "reference": True,
        "max_gain_db": 3.5,
    },
    "earcons": {"max": 16, "max_bytes": 524288, "rate": 48000},
    "duck": True,
    "audio_devices": True,
    "bundle": "tar.gz",
    "ota_key": "k1",
    "airplay": {"version": 2, "controls": True},
    "health": ["temp_c", "throttled", "under_voltage", "load"],
}
PI_SINKS = [
    {
        "name": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
        "description": "USB Audio Analog Stereo",
        "api": "alsa",
        "quality": None,
        "jack": None,
    },
    {
        "name": "alsa_output.platform-bcm2835_audio.stereo-fallback",
        "description": "Built-in Audio Stereo",
        "api": "alsa",
        "quality": None,
        "jack": "unplugged",
    },
]
PI_SOURCES = [
    {
        "name": "alsa_input.usb-Generic_USB_Audio-00.analog-stereo",
        "description": "USB Audio Analog Stereo",
        "api": "alsa",
        "quality": None,
        "jack": None,
    }
]
COVER = b"\xff\xd8\xff\xe0 a jpeg"
COVER_SHA = hashlib.sha256(COVER).hexdigest()

# The home-assistant key preset, as packages/common/voice_common/scopes.py has
# it (test_key.py holds the two together).
HOME_ASSISTANT_PRESET = frozenset(
    {
        "models:read",
        "health:read",
        "speech:transcribe",
        "speech:speak",
        "glossaries:ha",
        "satellites:read",
        "satellites:control",
        "satellites:update",
    }
)

# What each route needs, as the gateway's route table has it: any one scope of
# the set is enough. A route missing here answers 500, so a route added to the
# fake without its scope fails every test that uses it. The satellite actions
# are one row each, as the gateway has them, so an action the integration
# starts to use is checked against its own scope rather than a broad one.
SATELLITE_CONTROL = frozenset({"satellites:control"})
ROUTE_SCOPES: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/v1/models"): frozenset({"models:read"}),
    ("GET", "/voices"): frozenset({"speech:speak"}),
    ("POST", "/v1/audio/transcriptions"): frozenset({"speech:transcribe"}),
    ("POST", "/v1/audio/speech"): frozenset({"speech:speak"}),
    ("GET", "/glossaries/{name}"): frozenset({"glossaries:read:own"}),
    ("PUT", "/glossaries/{name}"): frozenset({"glossaries:write:own"}),
    ("GET", "/satellites"): frozenset({"satellites:read"}),
    ("GET", "/satellites/events"): frozenset({"satellites:read"}),
    ("GET", "/satellites/wake-words"): frozenset({"satellites:read"}),
    ("GET", "/satellites/firmware"): frozenset({"satellites:read"}),
    ("POST", "/satellites/ota"): frozenset({"satellites:update"}),
    ("GET", "/satellites/{sid}"): frozenset({"satellites:read"}),
    # Control fields only: anything else needs satellites:admin as well,
    # which the hub checks (FakeCalliope._patch).
    ("PATCH", "/satellites/{sid}"): SATELLITE_CONTROL,
    ("GET", "/satellites/{sid}/airplay/artwork"): frozenset({"satellites:read"}),
    ("POST", "/satellites/{sid}/airplay/{command}"): SATELLITE_CONTROL,
    ("POST", "/satellites/{sid}/media/stop"): SATELLITE_CONTROL,
    ("POST", "/satellites/{sid}/media"): SATELLITE_CONTROL,
    **{
        ("POST", f"/satellites/{{sid}}/{action}"): SATELLITE_CONTROL
        for action in ("identify", "reboot", "lights", "tone", "say", "flush", "ptt")
    },
    ("POST", "/satellites/{sid}/listen"): frozenset({"satellites:listen"}),
    ("POST", "/satellites/{sid}/inject"): frozenset({"satellites:listen"}),
    **{
        ("POST", f"/satellites/{{sid}}/{action}"): frozenset({"satellites:admin"})
        for action in ("adopt", "forget", "set-hub")
    },
}
# The reserved profile: reached with these, never with :own (the gateway's
# Reserved rule).
HOME_ASSISTANT_GLOSSARY: dict[str, frozenset[str]] = {
    "GET": frozenset({"glossaries:ha", "glossaries:read:all"}),
    "PUT": frozenset({"glossaries:ha", "glossaries:write:all"}),
}
# What PATCH /satellites/{id} takes with satellites:control alone, as the hub
# has it (services/satellites/app/main.py; test_key.py holds the two together).
CONTROL_FIELDS = frozenset(
    {
        "volume",
        "mic_gain_db",
        "mic_enabled",
        "speaker_enabled",
        "lights_enabled",
        "brightness",
        "audio_sink",
        "audio_source",
        "echo_reference",
        "output_satellite",
        "airplay_enabled",
        "airplay_name",
    }
)


def new_key() -> str:
    """A key in the gateway's format, as Account › API keys hands one out."""
    body = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(30)
    )
    return KEY_PREFIX + body + _checksum(body)


def airplay_idle() -> dict[str, Any]:
    """status.airplay with no phone connected."""
    return {
        "enabled": True,
        "name": "lounge",
        "running": True,
        "error": None,
        "player": None,
        "playing": False,
        "session": False,
        "client": None,
        "title": None,
        "artist": None,
        "album": None,
        "volume": None,
        "since": None,
        "track": None,
        "client_info": None,
        "progress": None,
        "artwork": None,
        "stream": None,
        "raw": {},
        "remote": {"available": None, "controls": [], "last": None},
    }


def airplay_playing(
    *, controls: list[str] | None = None, position: float = 42.0, paused: bool = False
) -> dict[str, Any]:
    """status.airplay while a phone plays, with its cover."""
    if controls is None:
        controls = [
            "play",
            "pause",
            "play_pause",
            "next",
            "previous",
            "stop",
            "disconnect",
        ]
    return airplay_idle() | {
        "player": "Paused" if paused else "Playing",
        "playing": not paused,
        "session": True,
        "client": "Someone's iPhone",
        "title": "So What",
        "artist": "Miles Davis",
        "album": "Kind of Blue",
        "volume": 60,
        "since": 1_700_000_000.0,
        "track": {"persistent_id": "abc", "title": "So What"},
        "client_info": {"client_ip": "fe80::1", "dacp_id": "D1"},
        "progress": {"position_s": position, "duration_s": 562.0},
        "artwork": {"sha256": COVER_SHA, "bytes": len(COVER), "type": "image/jpeg"},
        "raw": {"ssnc/clip": "fe80::1"},
        "remote": {"available": True, "controls": controls, "last": None},
    }


def satellite(
    sid: str,
    name: str,
    *,
    model: str,
    caps: dict[str, Any],
    status: dict[str, Any],
    adopted: bool = True,
    online: bool = True,
    **config: Any,
) -> dict[str, Any]:
    """A satellite as GET /satellites describes it, less "media", which the
    fake works out when it answers (FakeCalliope.describe)."""
    cfg = {
        "volume": 60,
        "mic_gain_db": 30.0,
        "mic_enabled": True,
        "speaker_enabled": True,
        "lights_enabled": True,
        "brightness": 55,
        "output_satellite": None,
        "buttons": {
            "play": {"press": "ptt"},
            "set": {"press": "stop"},
            "rec": {"press": "webhook:https://hooks.example.com/secret-token"},
        },
    } | config
    return {
        "id": sid,
        "name": name,
        "adopted": adopted,
        "online": online,
        "model": model,
        "firmware": "v0.1.2-163-gf60932d",
        "address": "192.0.2.50" if online else None,
        "connected_at": 1_700_000_000.0 if online else None,
        "last_seen": None,
        "config": cfg if adopted else None,
        "status": ({**status, **{k: cfg.get(k) for k in _REPORTED}} if online else {}),
        "caps": caps,
        "update": None,
        "ota": None,
        "listening": None,
        "earcons": None,
        "wake_words": ["hey_jarvis", "lumos"] if adopted else [],
    }


# The settings a satellite echoes in its status.
_REPORTED = ("volume", "mic_enabled", "speaker_enabled", "mic_gain_db")


def korvo(sid: str, name: str, **kwargs: Any) -> dict[str, Any]:
    """An ESP32-Korvo, with the live board's caps and status."""
    return satellite(
        sid,
        name,
        model="esp32-korvo-v1.1",
        # A hub keeps an offline satellite's last caps.
        caps=dict(KORVO_CAPS),
        status={"uptime_s": 3600, "rssi": -58, "muted": False, "heap": 90000},
        **kwargs,
    )


def pi(sid: str, name: str, *, mic: bool = True, **kwargs: Any) -> dict[str, Any]:
    """A Raspberry Pi running the satellite agent, with its sound cards and
    AirPlay receiver."""
    caps = dict(PI_CAPS)
    if not mic:
        del caps["mic"]
    sat = satellite(
        sid,
        name,
        model="raspberry-pi",
        caps=caps,
        status={
            "uptime_s": 7200,
            "rssi": -61,
            "temp_c": 48.5,
            "throttled": 0,
            "under_voltage": False,
            "load": 0.3,
            "muted": False,
            "audio_sink": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
            "audio_source": "",
            "echo_reference": True,
            "airplay_enabled": True,
            "airplay_name": None,
            "audio": {
                "sinks": PI_SINKS,
                "sources": PI_SOURCES,
                "default_sink": PI_SINKS[0]["name"],
                "default_source": PI_SOURCES[0]["name"],
                "playing_at": None,
            },
            "airplay": airplay_idle(),
        },
        **(
            {
                "mic_gain_db": 0.0,
                "mic_enabled": False,
                "audio_sink": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
                "audio_source": "",
                "echo_reference": True,
            }
            | kwargs
        ),
    )
    sat["firmware"] = "v0.1.2-193-g0ae5ed7"
    return sat


def lacks_scope(listed: str, message: str | None = None) -> web.Response:
    """The gateway's 403 for a key without the scopes `listed` names, with
    the RFC 6750 challenge that names them."""
    return envelope(
        403,
        message or f"This credential lacks the scope it needs: {listed}.",
        "insufficient_scope",
        {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{listed}"'},
    )


def envelope(
    status: int,
    message: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> web.Response:
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
        headers=headers,
    )


class FakeCalliope:
    """State the tests set and read, behind real HTTP."""

    def __init__(self) -> None:
        """A Korvo in the kitchen and a Pi in the lounge (both adopted), and
        one waiting to be adopted."""
        self.api_key = new_key()
        # What the key holds; a test takes scopes away to see a 403.
        self.scopes: set[str] = set(HOME_ASSISTANT_PRESET)
        # (scope list, message) a 403 names instead of the route's scopes, to
        # play a server that is not the gateway.
        self.hostile_refusal: tuple[str, str] | None = None
        # Every request, (method, path), whatever it was answered.
        self.hits: list[tuple[str, str]] = []
        self.satellites: dict[str, dict[str, Any]] = {
            KITCHEN_ID: korvo(KITCHEN_ID, "kitchen", lights_enabled=False),
            LOUNGE_ID: pi(LOUNGE_ID, "lounge"),
            PENDING_ID: korvo(PENDING_ID, "", adopted=False),
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
        # GET /health's backends.stt.health.models, or None for a stack older
        # than the list; stt_status is that health's status.
        self.stt_models: list[dict[str, Any]] | None = None
        self.stt_status = "ok"
        # /health's hotwords: False is a stack with STT_HOTWORDS=0, which
        # refuses `boost`.
        self.stt_hotwords = True
        self.health_body: dict[str, Any] | None = None
        self.transcript = "turn on the kitchen lights"
        # Glossary profiles by name, as PUT /glossaries/{name} stored them.
        self.glossaries: dict[str, str] = {}
        self.glossary_status: int | None = None  # answer PUT with this
        self.events_status: int | None = None  # answer /satellites/events with this
        self.list_status: int | None = None  # answer GET /satellites with this
        # False is a hub from before media: no "media" block, no route.
        self.media_route = True
        # Cleared, POST .../media holds its answer until it is set again, as
        # the hub holds it until the stream ends.
        self.media_hold = asyncio.Event()
        self.media_hold.set()
        self.media_answer: dict[str, Any] = {
            "played_s": 0.1,
            "stopped": False,
            "reason": "ended",
        }
        # (status, message, code) to refuse media with instead.
        self.media_refusal: tuple[int, str, str] | None = None
        # POST .../airplay/{command} answers this, or with this refusal.
        self.airplay_refusal: tuple[int, str, str] | None = None
        self.artwork = COVER
        self.firmware: list[dict[str, Any]] = []
        # POST /satellites/ota: satellite id to the reason it is skipped.
        self.ota_skip: dict[str, str] = {}
        # (status, message, code) to refuse every PATCH with.
        self.patch_refusal: tuple[int, str, str] | None = None
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
        r.add_get("/glossaries/{name}", self._get_glossary)
        r.add_put("/glossaries/{name}", self._put_glossary)
        r.add_get("/satellites", self._list)
        r.add_get("/satellites/events", self._events)
        r.add_get("/satellites/wake-words", self._wake_words)
        r.add_get("/satellites/firmware", self._list_firmware)
        r.add_post("/satellites/ota", self._ota)
        r.add_get("/satellites/{sid}", self._get)
        r.add_patch("/satellites/{sid}", self._patch)
        r.add_get("/satellites/{sid}/airplay/artwork", self._artwork)
        r.add_post("/satellites/{sid}/airplay/{command}", self._airplay)
        r.add_post("/satellites/{sid}/media/stop", self._media_stop)
        r.add_post("/satellites/{sid}/media", self._media)
        r.add_post("/satellites/{sid}/{action}", self._action)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        self.url = str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        """End every stream and answer every held upload, then close."""
        self.drop_streams()
        self.media_hold.set()
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
        """End every open event stream cleanly, as a hub restart would, or
        the gateway's 15-minute limit on an event stream."""
        for q in list(self._queues):
            q.put_nowait(None)

    async def wait_streams(self, count: int, timeout: float = 5) -> None:
        """Until `count` event streams have been opened in all."""
        async with asyncio.timeout(timeout), self._opened:
            await self._opened.wait_for(lambda: self.stream_count >= count)

    def calls(self, method: str, path: str) -> list[Any]:
        """The bodies of every request to one route."""
        return [body for m, p, body in self.requests if m == method and p == path]

    def media_view(self, sid: str) -> dict[str, Any] | None:
        """The "media" block as the hub works it out (spec 2.4): through the
        satellite whose speaker plays, in its formats; None while offline."""
        sat = self.satellites[sid]
        if not sat["online"] or not sat["adopted"]:
            return None
        through = (sat["config"] or {}).get("output_satellite") or sid
        caps = self.satellites[through]["caps"]
        if caps and "speaker" not in caps:
            return None
        # Empty caps: the hub's default speaker rate.
        rate = caps.get("speaker", {}).get("rate", 48000)
        music = caps.get("media") or {"rate": rate, "channels": 1}
        return {
            "through": through,
            "music": {"rate": music["rate"], "channels": music["channels"]},
            "announce": {"rate": rate, "channels": 1},
            "playing": sat.get("playing"),
        }

    def describe(self, sat: dict[str, Any]) -> dict[str, Any]:
        """A satellite as the hub answers for it: config.buttons only to a key
        holding satellites:admin, because a webhook action names a secret."""
        out = copy.deepcopy(sat)
        out.pop("playing", None)
        if "satellites:admin" not in self.scopes:
            (out.get("config") or {}).pop("buttons", None)
        if self.media_route:
            out["media"] = self.media_view(sat["id"])
        return out

    # -- routes --------------------------------------------------------------

    def _keyed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.api_key}"

    def _holds(self, request: web.Request, scope: str) -> bool:
        return self._keyed(request) and scope in self.scopes

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        self.hits.append((request.method, request.path))
        if request.path == "/health":
            return await handler(request)
        if "Authorization" not in request.headers:
            return envelope(
                401,
                "Authentication required.",
                "unauthenticated",
                {"WWW-Authenticate": "Bearer"},
            )
        if not self._keyed(request):
            return envelope(
                401,
                "Incorrect API key provided.",
                "invalid_api_key",
                {"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        method = "GET" if request.method == "HEAD" else request.method
        resource = request.match_info.route.resource
        route = resource.canonical if resource is not None else request.path
        if "action" in request.match_info:
            route = route.replace("{action}", request.match_info["action"])
        needed = ROUTE_SCOPES.get((method, route))
        if needed is None:
            return envelope(500, f"no scope declared for {method} {request.path}")
        if request.match_info.get("name") == "home-assistant":
            needed = HOME_ASSISTANT_GLOSSARY[method]
        if not needed & self.scopes:
            return lacks_scope(*(self.hostile_refusal or (" ".join(sorted(needed)),)))
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        """Liveness for anyone; the backends for a key with health:read, and
        their addresses and threads only with health:detail."""
        if self.health_body is not None:
            return web.json_response(self.health_body)
        if not self._holds(request, "health:read"):
            return web.json_response({"status": "ok"})
        detail = self._holds(request, "health:detail")
        stt = {
            "status": self.stt_status,
            "model": self.stt_model,
            "hotwords": self.stt_hotwords,
        } | ({} if self.stt_models is None else {"models": self.stt_models})
        backends = {
            "stt": {"reachable": True, "health": stt},
            "tts": {
                "reachable": True,
                "health": {
                    "status": "ok",
                    "voices": len(self.voices),
                    "default_voice": "bm_george",
                },
            },
        }
        if detail:
            stt["threads"] = 8
            backends["stt"] |= {"url": "http://stt-stack:8000", "http_status": 200}
            backends["tts"] |= {"url": "http://tts-stack:8001", "http_status": 200}
        return web.json_response({"status": "ok", "backends": backends})

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
        if "file" not in fields:
            # The stack's refusal, which the config flow's scope check meets.
            return envelope(400, "file: Field required", "missing_required_parameter")
        self.requests.append(("POST", "/v1/audio/transcriptions", fields))
        name = fields.get("glossary")
        # The reserved profile resolves only for a key that may select it.
        hidden = (
            set()
            if {"glossaries:ha", "glossaries:read:all"} & self.scopes
            else {"home-assistant"}
        )
        if name and (name not in self.glossaries or name in hidden):
            return envelope(
                400,
                f"Unknown glossary profile {name!r}. This deployment has: "
                f"{', '.join(sorted(set(self.glossaries) - hidden)) or 'none'}. "
                "See GET /glossaries.",
                "invalid_value",
            )
        unspellable = sorted(
            {
                ch
                for ch in self.glossaries.get(name or "", "")
                if ch in "\u2019\U0001f4a1"
            }
        )
        if "boost" in fields and unspellable:
            # stt-stack's refusal for a term the model's vocabulary cannot spell.
            return envelope(
                400,
                f"'boost' cannot be honoured for 1 term(s): at {unspellable[0]!r}. "
                "This model's vocabulary has no piece for those characters.",
                "invalid_value",
            )
        if "boost" in fields and not self.stt_hotwords:
            return envelope(
                400,
                "Unsupported parameter: 'boost' cannot be honoured: this "
                "deployment has STT_HOTWORDS=0.",
                "unsupported_parameter",
            )
        if "boost" in fields and self.stt_model == "whisper":
            return envelope(
                400,
                "Unsupported parameter: 'boost' is not supported by the "
                "'whisper' engine",
                "unsupported_parameter",
            )
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

    async def _get_glossary(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.glossaries:
            return envelope(404, f"Unknown glossary profile {name!r}.", "glossary_not_found")
        return web.Response(text=self.glossaries[name])

    async def _put_glossary(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await request.json()
        self.requests.append(("PUT", f"/glossaries/{name}", body))
        if self.glossary_status is not None:
            return envelope(self.glossary_status, "glossary store unavailable")
        text = body["text"]
        lines = [ln.strip() for ln in text.splitlines()]
        entries = [ln for ln in lines if ln and not ln.startswith("#")]
        rules = [ln.split("=", 1)[0].strip().lower() for ln in entries if "=" in ln]
        terms = [ln for ln in entries if "=" not in ln]
        # The stack's own refusals, for what this integration must never send:
        # a duplicate, and a single-word left-hand side without `force`.
        assert len({term.lower() for term in terms}) == len(terms), terms
        assert len(set(rules)) == len(rules), rules
        assert all(" " in heard for heard in rules), rules
        self.glossaries[name] = text
        return web.json_response(
            {"name": name, "terms": len(entries), "replacements": len(rules)}
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
        if self.list_status is not None:
            return envelope(self.list_status, "the hub is not ready")
        return web.json_response(
            {"satellites": [self.describe(s) for s in self.satellites.values()]}
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
        return web.json_response(self.describe(sat))

    async def _patch(self, request: web.Request) -> web.Response:
        body = await request.json()
        sid = request.match_info["sid"]
        self.requests.append(("PATCH", f"/satellites/{sid}", body))
        sat = self._find(sid)
        if sat is None or not sat["adopted"]:
            return envelope(404, "no adopted satellite with that id")
        if "satellites:admin" not in self.scopes and set(body) - CONTROL_FIELDS:
            return lacks_scope("satellites:admin")
        if self.patch_refusal is not None:
            return envelope(*self.patch_refusal)
        if "name" in body:
            sat["name"] = body.pop("name")
        sat["config"].update(body)
        return web.json_response(self.describe(sat))

    def _playable(self, sid: str) -> tuple[dict[str, Any] | None, web.Response | None]:
        """The satellite, or the hub's refusal, for a route that needs it
        online and adopted."""
        sat = self._find(sid)
        if sat is None:
            return None, envelope(
                404, "no single satellite matches", "satellite_not_found"
            )
        if not sat["online"]:
            return None, envelope(
                409, f"satellite {sid} is not connected", "satellite_offline"
            )
        return sat, None

    async def _media(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        body = await request.read()
        sent: dict[str, Any] = {
            "announce": request.query.get("announce"),
            "content_type": request.headers.get("Content-Type"),
            "bytes": len(body),
        }
        self.requests.append(("POST", f"/satellites/{sid}/media", sent))
        if not self.media_route:
            return envelope(
                404, f"Invalid URL (POST /satellites/{sid}/media).", "unknown_url"
            )
        sat, refused = self._playable(sid)
        if refused is not None:
            return refused
        if self.media_refusal is not None:
            return envelope(*self.media_refusal)
        try:
            await self.media_hold.wait()
        except asyncio.CancelledError:
            # The upload went away before its answer (TestServer cancels the
            # handler), which is how Home Assistant stops its own stream: the
            # hub ends it "cancelled".
            sent["reason"] = "cancelled"
            raise
        sent["reason"] = self.media_answer["reason"]
        return web.json_response(self.media_answer)

    async def _media_stop(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        self.requests.append(("POST", f"/satellites/{sid}/media/stop", None))
        _, refused = self._playable(sid)
        return refused or web.Response(status=204)

    async def _airplay(self, request: web.Request) -> web.Response:
        sid, command = request.match_info["sid"], request.match_info["command"]
        self.requests.append(("POST", f"/satellites/{sid}/airplay/{command}", None))
        _, refused = self._playable(sid)
        if refused is not None:
            return refused
        if self.airplay_refusal is not None:
            return envelope(*self.airplay_refusal)
        return web.json_response({"command": command, "status": 204, "confirmed": True})

    async def _artwork(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        self.requests.append(
            (
                "GET",
                f"/satellites/{sid}/airplay/artwork",
                {
                    "v": request.query.get("v"),
                    "authorization": request.headers.get("Authorization"),
                },
            )
        )
        sha = hashlib.sha256(self.artwork).hexdigest()
        if request.query.get("v") not in (None, sha):
            return envelope(404, "no artwork", "no_artwork")
        return web.Response(body=self.artwork, content_type="image/jpeg")

    async def _list_firmware(self, request: web.Request) -> web.Response:
        return web.json_response({"firmware": self.firmware})

    async def _ota(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(("POST", "/satellites/ota", body))
        sid = body["satellite"]
        if sid in self.ota_skip:
            return web.json_response(
                {"started": [], "skipped": {sid: self.ota_skip[sid]}}
            )
        sat = self.satellites[sid]
        version = (sat.get("update") or {}).get("version")
        sat["ota"] = {
            "sha256": body["sha256"],
            "version": version,
            "state": "requested",
        }
        # The hub offers no update of the version an update is installing
        # (Hub.available_update).
        sat["update"] = None
        return web.json_response({"started": [sid], "skipped": {}})

    async def _action(self, request: web.Request) -> web.Response:
        sid, action = request.match_info["sid"], request.match_info["action"]
        body = await request.json() if request.can_read_body else None
        self.requests.append(("POST", f"/satellites/{sid}/{action}", body))
        if action not in ("identify", "say", "tone", "flush", "ptt", "reboot"):
            # Scoped, but not something this fake plays.
            return envelope(500, f"the fake does not answer {action}")
        sat, refused = self._playable(sid)
        if refused is not None:
            return refused
        if action in ("say", "tone") and not sat["config"]["speaker_enabled"]:
            return envelope(
                409,
                f"satellite {sid} has its speaker turned off "
                "(speaker_enabled is false)",
                "speaker_disabled",
            )
        if action == "ptt" and not sat["config"]["mic_enabled"]:
            return envelope(
                409,
                f"satellite {sid} has its microphone turned off (mic_enabled is false)",
                "mic_disabled",
            )
        return web.Response(status=204)
