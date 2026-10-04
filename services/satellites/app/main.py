"""voice-satellites: the hub for thin audio devices.

A satellite is a microphone array, a speaker and a ring of lights on Wi-Fi. It
makes no decisions: it streams its microphones here and plays, lights and
reports whatever it is told. This service adopts satellites, holds their
settings, listens for wake words on their microphones, routes what follows to
an assistant, and is the one place audio and firmware reach them from.

    WS    /satellites/ws               the device connection (protocol: README)
    WS    /nodes/ws                    the same handler, for firmware from before
                                       the rename (see LEGACY_SOCKET)
    GET   /satellites                  every satellite seen since start, adopted or not
    GET   /satellites/events           server-sent events: buttons, wake words, routing
    GET   /satellites/firmware         uploaded images
    POST  /satellites/firmware?model&version&signature   raw body: a firmware image
    DELETE /satellites/firmware/{sha256}
    POST  /satellites/ota              {"satellite": id|name|"all", "sha256": ...}
    GET   /satellites/routing          the rules (router.py)
    PUT   /satellites/routing          replace them
    POST  /satellites/routing/test     a typed sentence through a rule, played nowhere
    POST  /satellites/ha/pipelines     Home Assistant's Assist pipelines, for the picker
    POST  /satellites/llm/models       a language model server's models, for the picker
    POST  /satellites/llm/test         one question to a draft llm destination
    GET   /satellites/wake-words       the wake words, which satellites hear each, and
                                       whether its model is ready
    PUT   /satellites/wake-words       replace them; live, no restart
    GET   /satellites/{id}
    PATCH /satellites/{id}             name, config and the button mapping
    POST  /satellites/{id}/adopt | forget | identify | reboot | lights | tone | say
                        | flush | set-hub | ptt
    POST  /satellites/{id}/listen?seconds=5   a WAV of the raw microphone channels
    POST  /satellites/{id}/inject?play=0      a 16 kHz mono WAV through the wake
                                       word, endpoint and routing path, as if heard
    POST  /satellites/{id}/media?announce=0   a WAV in the satellite's own format
                                       (its `media`), played; answered when it ends
    POST  /satellites/{id}/media/stop         end the media stream playing there
    POST  /satellites/{id}/airplay/{command}  play, pause, next... to the phone
                                       playing to its AirPlay receiver

EVERYTHING IS UNDER /satellites, INCLUDING THE SOCKET, because the gateway
mounts backend paths flat and never rewrites them. The gateway relays
/satellites/ws (and /nodes/ws) as a WebSocket and forwards the rest as
ordinary routes.

EVERY REQUEST CARRIES THE GATEWAY'S ASSERTION (D52). identity.install answers
anything without a valid X-Calliope-Identity for the audience "satellites" with
401, the socket included, and leaves /health alone. The gateway decides who may
call what; the hub only shapes what it answers by the caller's scopes:
`config.buttons` (button webhooks name secrets) and the wake words' actions go
only to satellites:admin (D62), and a PATCH that changes anything but the
controls Home Assistant drives needs satellites:admin too (CONTROL_FIELDS).

THE DEVICE SOCKET HAS NO LOGIN, AND THAT IS DELIBERATE. A satellite cannot hold
a credential it was never given, and one baked into firmware would be in every
flash dump. So the gateway relays the socket for anyone and adds its relay
assertion (svc:gateway-relay), which the hub requires on the upgrade, so only a
connection the gateway relayed reaches it (D53), and refuses it on every
HTTP route (RelayOnlyOnTheSocket). What a connection may DO is
decided by the adoption token. An unadopted connection can say hello and
receive "pending", nothing else: no microphone audio is accepted from it and
nothing is sent to it but that one word, until someone with satellites:admin
adopts it. Nor can it take the place of an adopted satellite that is connected:
a satellite's id is its MAC, and only the token proves the rest. Because anyone
on the internet can say hello, at most PENDING_MAX unadopted satellites are
kept (the oldest goes), and hellos that prove no adoption are taken at most
HELLOS_PER_MINUTE a minute from one address; a hello with a valid token is
never held back, so forty satellites reconnecting together after a restart,
all from the gateway's address, come straight back.

THE LISTENING PATH, per adopted satellite. The socket loop hands microphone
frames to a bounded queue (the oldest frame goes when it is full, and is
counted); one task per satellite drains it and runs listening.Ear (front-end,
wake words, endpointer, barge-in) in a thread pool, so the event loop never
does signal work. Each satellite listens only for the wake words assigned to
it, and each word says what it does (wake_words.json: see Voice and
wakewords_config.py). A word may be double-checked first: the audio that held
it is transcribed, and the hub answers only when the word is in the
transcript, so a TV saying something like it is ignored (Hub.on_wake,
verify.py). A trigger word publishes "triggered" and that is all
(Hub.trigger). Any other word starts a Conversation: the "wake" earcon if the
satellite holds it, the ring pointed at the talker, the satellite ducked,
then the command, streamed through dialogue.run_turn, and the reply on
whichever satellite the word's action names, sentence by sentence (Player).
A command is one turn; a conversation listens for the next one without the
wake word. While a reply plays, speech over it or the wake word interrupts
it. One conversation per satellite at a time.

A SATELLITE WITH LIGHTS OFF IS NEVER SENT "lights". Every lights message this
hub sends goes through Hub.send_lights, which reads the satellite's
lights_enabled at the moment of sending; POST /satellites/{id}/lights answers
409 for such a satellite rather than go around it. The ring stays dark in a
bedroom because the hub does not ask, not only because the firmware would
refuse.

A SATELLITE WITH ITS SPEAKER OFF IS SENT NOTHING AUDIBLE, on the same terms:
earcons and replies read speaker_enabled when they are sent, /tone and /say
answer 409, and turning the speaker off drops whatever was still playing.

TWO LANES PER SPEAKER. The voice lane (Session.speaker) carries replies,
earcons, Say, tones and announcements, one after another. The media lane
(Session.media) carries one stream of music from Home Assistant at a time,
already converted to the satellite's own format. A satellite that says it
has one (caps "media") takes it as frame kind 5 and ducks it under the voice
itself; any other (the Korvo) takes it as its usual speaker frames, only
while the voice lane is idle, so a reply pauses the music instead of queuing
behind it or flushing it (Hub.stream_media).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import statistics
import struct
import time
import uuid
import wave
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

import httpx
import base64
import binascii
import hashlib
import math
from datetime import UTC, datetime, timedelta

import ipaddress
from collections import OrderedDict

import numpy as np
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from starlette.requests import ClientDisconnect, HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from voice_common import errors, health, identity
from voice_common import logging as voice_logging
from voice_common.errors import ApiError

from . import (audio, dialogue, earcons, gateway, listening, secret_client, secret_import, signing,
               telemetry, verify, wakeword, wakewords_config)
from . import output as outputs
from . import language as lang
from . import router as routing
from . import tools as tooling
from .destinations import DestinationError, not_an_address, secret_url
from .mqtt import MqttBridge
from .store import (AIRPLAY_SETTINGS, AUDIO_SETTINGS, DEFAULT_CONFIG, DEVICE_ACTIONS, Store, device_actions,
                    firmware_older, keeps_a_mute, reported_config, satellite_config)

log = voice_logging.setup("voice-satellites", "SATELLITES")

DATA_DIR = Path(os.environ.get("SATELLITES_DATA_DIR", "/data"))
TTS_URL = os.environ.get("SATELLITES_TTS_URL", "").rstrip("/")
TTS_VOICE = os.environ.get("SATELLITES_TTS_VOICE", "bm_george")
# Only the seed of wake_words.json, read on the first start with a volume that
# has none (wakewords_config.py); after that the file decides. Unset means
# hey_jarvis at 0.5 on every satellite; "" means no wake words at all, which
# leaves push-to-talk and /inject?wake_word= working.
WAKE_WORDS = os.environ.get("SATELLITES_WAKE_WORDS", "hey_jarvis:0.5")
MODEL_DIR = Path(os.environ.get("SATELLITES_MODEL_DIR") or DATA_DIR / "models")
FRONTEND = os.environ.get("SATELLITES_FRONTEND", "1").strip() != "0"
# SATELLITES_DEBUG_AUDIO=1: keep the last DEBUG_KEEP commands' surroundings as
# WAV files under <data>/debug (processed and raw first microphone), so a
# command that came back empty can be listened to. It records the room; off
# by default, and nothing is kept beyond the last DEBUG_KEEP.
DEBUG_AUDIO = os.environ.get("SATELLITES_DEBUG_AUDIO", "0").strip() == "1"
DEBUG_AUDIO_S = 16.0 if DEBUG_AUDIO else 0.0
DEBUG_KEEP = 10
# Where the Containerfile put the default wake word at build time. Copied onto
# the volume at start-up (wakeword.ensure_models), so a first start needs no
# network and extra models still go to SATELLITES_MODEL_DIR.
BAKED_MODELS = Path(__file__).resolve().parent.parent / "models"
MAX_FIRMWARE = 4 * 1024 * 1024  # one OTA slot on the Korvo

FRAME_MIC, FRAME_SPEAKER, FRAME_FIRMWARE = 1, 2, 3
# Music on its own lane, for a satellite whose caps say "media" (the Pi
# agent): the speaker frame's header, then its interleaved channels.
FRAME_MEDIA = 5
HEADER = 16
# Under the satellite's own ceiling: the Arduino WebSockets library drops the
# whole connection on any frame over 15 KB (WEBSOCKETS_MAX_DATA_SIZE, not
# overridable on ESP32). 16 KB chunks killed the first real update 17 ms in.
OTA_CHUNK = 8 * 1024
# How far a satellite's Output is followed through other satellites.
OUTPUT_HOPS = 4
SPEAKER_CHUNK_MS = 20
SPEAKER_LEAD_S = 0.3  # how far ahead of real time playback is kept
# The media lane runs further ahead: music drops out on a Wi-Fi hiccup that
# speech rides over, and the Pi buffers it (its own media stream) where the
# Korvo's 300 ms of buffer is all it has, so a Korvo's media keeps the voice's.
MEDIA_LEAD_S = 1.0
# An announcement is read whole before it is queued, as one sentence of a reply
# is: this bounds what that holds in memory (10 MB at 44.1 kHz mono).
ANNOUNCE_MAX_S = 120
# How long POST /satellites/{id}/airplay/{command} waits for the satellite's
# answer. The Pi asks Shairport, which asks the phone (5 s at most), and
# watches for 1.5 s whether the phone did it.
AIRPLAY_WAIT_S = 6.0
AIRPLAY_COMMANDS = ("play", "pause", "play_pause", "next", "previous", "stop", "disconnect")
# A press that ran an action on the satellite is answered by its status in
# milliseconds; a status later than this is a heartbeat, not the answer.
# Current firmware marks that status (cause "button"); this is for firmware
# from before 2026-09-27, which does not.
VOLUME_PRESS_S = 2.0
# The settings a button can change on the satellite (actions.h there).
BUTTON_SETTINGS = ("volume", "lights_enabled", "brightness")

# One second of 20 ms frames. The listener drains the queue in batches, so it
# only fills when the thread pool falls a second behind; then the oldest audio
# goes, because a wake word from a second ago is worth less than one now.
MIC_QUEUE = 50
MIC_BATCH = 25
# How long a conversation waits for its command after the wake word: the
# endpointer's own ceiling is 10 s of audio, so this only trips when the audio
# stops arriving (a satellite muted or gone mid-command).
COMMAND_WAIT_S = 15.0
# Ducking lowers the hub's audio on the satellite while it listens, on the
# volume scale and never above the volume itself. The duck carries its own
# timeout, so a hub that dies mid-conversation cannot leave a satellite quiet
# for good; the firmware also lifts it on disconnect.
DUCK_LEVEL = 20
DUCK_MS = 60_000
EARCON_RETRY_S = 5.0   # the satellite formats its earcon storage in the background
EARCON_RETRIES = 24
LISTEN_COLOUR = (40, 110, 255)


def ring_colour(behaviour: "routing.Behaviour | None") -> list[int]:
    """The colour a word's turn shows on the ring, listening, thinking or a
    trigger's flash: the word's own (Behaviour.colour), or the listening blue."""
    hexa = behaviour.colour if behaviour is not None else None
    return [int(hexa[i:i + 2], 16) for i in (1, 3, 5)] if hexa else list(LISTEN_COLOUR)
LIGHTS_MIN_S = 0.15    # at most one direction update this often
MAX_INJECT_S = 60
WAKE_GRACE_S = 1.5
DEFAULT_EARCONS = earcons.defaults()
# A follow-up that has heard nothing for its follow_up_s ends on the Ear's own
# "no_speech". This much longer without any Command means the audio itself
# stopped arriving (a privacy mute, a satellite gone), and the conversation
# ends on that instead of waiting for ever.
FOLLOW_UP_SLACK_S = 3.0
# A reply's playback is waited for at most its own length and this, in case
# the speaker loop that would say it has finished is gone with its socket.
PLAYBACK_SLACK_S = 5.0
# How long an utterance that interrupted a reply may take to start: it already
# has, so this only ends one the endpointer lost. Then up to the endpointer's
# own 10 s of speech.
CAPTURE_START_S = 4.0
CAPTURE_WAIT_S = CAPTURE_START_S + 10.0 + FOLLOW_UP_SLACK_S
# Turns kept for the latency summary in GET /satellites/{id}.
LATENCY_TURNS = 20
TRIGGER_FLASH_S = 0.3
# What of the double-check's transcript a wake record and a wake_rejected
# event carry: two seconds of speech is well under it, and a runaway
# transcript of noise is cut.
VERIFY_HEARD_CHARS = 120

# Who relays the device socket: the gateway's own principal, in the assertion
# it adds to the upgrade (D53). No other caller may open the socket.
RELAY = "svc:gateway-relay"
# Anyone on the internet can say hello on the socket, so what that costs is
# bounded: at most PENDING_MAX unadopted satellites are kept, the newest, and
# hellos that prove no adoption are taken at most HELLOS_PER_MINUTE a minute
# from one address, counted for at most HELLO_ADDRESSES addresses (D53,
# recheck L10 and M-4).
PENDING_MAX = 32
HELLOS_PER_MINUTE = 10
HELLO_ADDRESSES = 4096
# The close code for a hello refused for now, and for a pending satellite
# evicted by a newer one: "try again later" (RFC 6455).
CLOSE_TRY_LATER = 1013
# What satellites:control may change with PATCH: the controls Home Assistant
# drives. Anything else (the name, the button mapping, how the ring is
# mounted) is the satellite's configuration and needs satellites:admin (§3.5).
CONTROL_FIELDS = frozenset({
    "volume", "mic_gain_db", "mic_enabled", "speaker_enabled", "lights_enabled", "brightness",
    "audio_sink", "audio_source", "echo_reference", "output_satellite", "airplay_enabled",
    "airplay_name"})

FIRMWARE_KEY: Any | None = None
EXECUTOR: ThreadPoolExecutor | None = None


def satellite_id(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


def client_address(conn: HTTPConnection) -> str | None:
    """The device's address: the last X-Forwarded-For entry, which the gateway
    writes from its own view of the client (D67), else the peer. Believed
    because every connection that reaches a handler carries the gateway's
    assertion (D53), and the gateway drops any X-Forwarded-For it was sent.
    Only an IP address is taken; anything else is the peer."""
    forwarded = conn.headers.get("x-forwarded-for")
    if forwarded:
        try:
            return str(ipaddress.ip_address(forwarded.split(",")[-1].strip()))
        except ValueError:
            pass
    return conn.client.host if conn.client else None


def is_admin(request: HTTPConnection) -> bool:
    """Does this request's assertion hold satellites:admin?"""
    return identity.has(identity.claims_of(request), "satellites:admin")


class RelayOnlyOnTheSocket:
    """The relay's assertion opens the device socket and nothing else: 403 on
    every HTTP request that carries it. It holds no scope, and the hub checks
    none on most routes (the gateway does), so without this an assertion
    minted for the relay, or replayed within its minute, would adopt, forget
    and read telemetry like an admin's (deny by default)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        claims = (scope.get("state") or {}).get(identity.STATE_CLAIMS)
        if scope["type"] == "http" and getattr(claims, "sub", None) == RELAY:
            refused = ApiError(403, "the gateway's relay may open the device socket and "
                                    "nothing else", code="relay_only")
            await errors.render(refused)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---- live connections -------------------------------------------------------


class Clip:
    """One sentence of a reply on its way to a speaker. The speaker loop marks
    it `started` when its first frame goes out, and resolves `done` when it
    ends: True when all of it went out, False when a flush or a setting
    dropped it. A conversation learns from these what was heard of a reply it
    interrupted, and when the reply is over."""

    __slots__ = ("pcm", "text", "started", "done")

    def __init__(self, pcm: bytes, text: str = ""):
        self.pcm, self.text, self.started = pcm, text, False
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def finish(self, played: bool) -> None:
        if not self.done.done():
            self.done.set_result(played)


class MediaStream:
    """One upload to POST /satellites/{id}/media playing on the media lane
    (Hub.stream_media), as Session.media of the satellite that plays it.
    `source` is the satellite it was sent to, which differs when that one's
    Output is another. `stopped` is why it was ended from outside (stopped,
    superseded, muted, unadopted, disconnected, cancelled), set once, and
    None while nothing has; `played_s` is how much of it has been sent."""

    __slots__ = ("id", "source", "since", "stopped", "played_s")

    def __init__(self, source: str):
        self.id = uuid.uuid4().hex
        self.source = source
        self.since = time.time()
        self.stopped: str | None = None
        self.played_s = 0.0

    def stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason

    def view(self) -> dict:
        return {"id": self.id, "source": self.source, "since": self.since}


class WakeCheck:
    """One wake word being double-checked (verify.py, Hub.on_wake): the Heard,
    the word's verify settings, and, once STT has answered, what it made of
    the word's audio.

    In mode "on" it is Session.checking from the wake until it is decided,
    and `held` keeps the commands the Ear ends meanwhile: they are the wake
    word's, and go wherever it does. `captures` is false for a trigger, which
    the Ear listens for nothing after.

    Its telemetry record says both what STT heard and what the hub did
    (`acted`, Hub.record_wake's decision), so it is written once both are
    known, in whichever order they come: in mode "on" the check decides
    first, in mode "log" the hub acts first."""

    __slots__ = ("heard", "mode", "spellings", "captures", "held", "decision", "transcript",
                 "matched", "ms", "clip", "acted", "unchecked")

    def __init__(self, heard: listening.Heard, settings: routing.VerifySettings, captures: bool):
        self.heard, self.mode, self.spellings = heard, settings.mode, list(settings.spellings)
        self.captures = captures
        self.held: list[listening.Command] = []
        # accepted, rejected (mode "on"), would_reject (mode "log"), or error:
        # STT failed or took too long, and the wake went ahead. None until
        # STT has answered. `unchecked`: rejected because STT could not
        # answer, not because it heard something else.
        self.decision: str | None = None
        self.unchecked = False
        self.transcript: str | None = None
        self.matched: str | None = None
        self.ms: float | None = None
        self.clip: str | None = None     # the kept clip's name (telemetry.py)
        self.acted: str | None = None

    def view(self) -> dict:
        """The wake record's `verify`."""
        return {"mode": self.mode, "decision": self.decision, "heard": self.heard_text(),
                "matched": self.matched, "ms": self.ms, "clip": self.clip}

    def heard_text(self) -> str | None:
        return None if self.transcript is None else self.transcript[:VERIFY_HEARD_CHARS]


class Session:
    """One connected device. Starlette sockets are not safe to send on from two
    coroutines at once, and the speaker loop, the OTA pump, conversations and
    API calls all send, so every send takes the lock."""

    def __init__(self, ws: WebSocket, hello: dict):
        self.ws = ws
        self.lock = asyncio.Lock()
        self.id = satellite_id(hello.get("id", ""))
        self.connected_at = time.time()
        # Behind the gateway every peer is the gateway; it passes the device's
        # own address along. Shown, and what tokenless hellos are counted by.
        self.address = client_address(ws)
        self.adopted = False
        # Closed to make room for a newer pending satellite: not remembered
        # as seen when its socket ends (Hub.limit_pending).
        self.evicted = False
        self.status: dict = {}
        self.taps: set[asyncio.Queue] = set()
        self.speaker: asyncio.Queue[bytes | Clip] = asyncio.Queue()
        self.speaker_gen = 0      # bumped by a flush; the loop drops the item in hand
        self.playing = False
        # The sequence number of the next speaker frame (kind 2). The
        # session's and not the speaker loop's, because a Korvo's media is
        # sent as speaker frames too, between two replies, and the firmware
        # counts one stream of them.
        self.spk_seq = 0
        # The media stream playing on this satellite's speaker, whichever
        # satellite it was sent to (Hub.stream_media).
        self.media: MediaStream | None = None
        # AirPlay commands sent and not answered yet, by their id: resolved
        # by the satellite's airplay_result, or by the socket closing.
        self.replies: dict[str, asyncio.Future] = {}
        # time.monotonic() at which what has been sent will have played out
        # on the satellite, as far as the pacing knows.
        self.play_until = 0.0
        self.ota: dict | None = None
        self.mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE)
        self.mic_dropped = 0
        self.ear: listening.Ear | None = None
        self.listener: asyncio.Task | None = None
        self.listen_error: str | None = None
        # The cover of what an AirPlay receiver plays ("artwork"): sha256,
        # format and the image, for GET /satellites/{id}/airplay/artwork.
        self.artwork: dict | None = None
        self.conversation: Conversation | None = None
        # A wake word whose double-check (mode "on") is under way, or was
        # rejected and still owns what the Ear captured after it (Hub.on_wake).
        self.checking: WakeCheck | None = None
        self.earcons: earcons.Sync | None = None
        self.earcon_asks = 0
        self.lit = False          # the hub's own layer is showing something
        self.duck_holds = 0       # conversations that want this satellite ducked
        # time.monotonic() until which a status is the answer to a press that
        # ran an action on the satellite (Hub.on_button), for older firmware.
        self.volume_press_until = 0.0
        # Set by the privacy mute (Hub.on_mute): the listener puts the Ear
        # back to wake words before it next hears anything.
        self.ear_reset = False
        self.update(hello)
        # Speaker or jack, from the loopback (output.py). Channel 0 is
        # the loopback on a satellite with more than one channel.
        self.sense = outputs.OutputSense(self.mic_channels) if self.mic_channels > 1 else None

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

    async def close(self, code: int) -> None:
        """Close from outside the socket's own loop: under the lock, so it
        never lands in the middle of another send."""
        async with self.lock:
            await self.ws.close(code=code)

    async def send_if(self, allowed: Callable[[], bool], obj: dict | None = None,
                      data: bytes | Callable[[], bytes] | None = None) -> bool:
        """Send `obj` as JSON, or `data` as a binary frame, only if allowed()
        still holds once the lock is ours. Checked inside the lock and not
        before it: the speaker loop holds the lock for every 20 ms frame, and a
        PATCH that turns the lights or the speaker off while a send waits for
        it must stop that send, not let it out just after the setting said
        no. `data` may be a function that makes the frame, which is then made
        inside the lock as well (speaker_frame)."""
        async with self.lock:
            if not allowed():
                return False
            if obj is not None:
                await self.ws.send_text(json.dumps(obj))
            else:
                await self.ws.send_bytes(data() if callable(data) else data)
            return True

    def speaker_frame(self, pcm: bytes) -> bytes:
        """The next speaker frame (kind 2) with `pcm`, taking the next
        sequence number. Made inside the socket's lock (send_if), so the voice
        lane and a Korvo's media, which share the numbers, never send one
        twice or out of order."""
        frame = struct.pack("<BBBBIQ", FRAME_SPEAKER, 0, 1, 0, self.spk_seq, 0) + pcm
        self.spk_seq = (self.spk_seq + 1) & 0xFFFFFFFF
        return frame

    def reported(self) -> dict:
        """The settings this satellite has said it has: its hello (firmware
        from 2026-09-25 on) with its latest status over it."""
        return reported_config(self.hello) | reported_config(self.status)

    @property
    def spk_rate(self) -> int:
        return self.caps.get("speaker", {}).get("rate", 48000)

    @property
    def media_format(self) -> tuple[int, int] | None:
        """(rate, channels) of the media lane, for a satellite that takes
        media as frame kind 5 (caps "media"), or None for one that takes it
        as speaker frames, mono at spk_rate."""
        m = self.caps.get("media")
        if not isinstance(m, dict):
            return None
        rate, channels = m.get("rate"), m.get("channels")
        ok = all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (rate, channels))
        return (rate, channels) if ok else None

    @property
    def mic_rate(self) -> int:
        return self.caps.get("mic", {}).get("rate", 16000)

    @property
    def mic_channels(self) -> int:
        return self.caps.get("mic", {}).get("channels", 4)

    @property
    def has_mic(self) -> bool:
        """A satellite whose caps name no microphone is a speaker only (a Pi
        with no input). Firmware from before caps said nothing about any of
        it, and had the Korvo's."""
        return "mic" in self.caps or not self.caps

    def sound(self, start: float, end: float, pcm: bytes) -> outputs.Sound:
        """A sound played on this satellite, for OutputSense: its level, and
        the volume it played at, which a duck lowers."""
        volume = self.status.get("volume")
        if isinstance(volume, int) and self.duck_holds > 0:
            volume = min(volume, DUCK_LEVEL)
        return outputs.Sound(start, end, outputs.level_dbfs(np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2")),
                             volume if isinstance(volume, int) else None)

    def offer_mic(self, pcm: bytes) -> None:
        """Queue one microphone frame for the listener, dropping the oldest
        when the queue is full. Never waits: this runs in the socket loop,
        and a socket loop that waits stops reading the satellite."""
        if self.mic.full():
            self.mic.get_nowait()
            self.mic_dropped += 1
            if self.mic_dropped in (1, 10, 100) or self.mic_dropped % 1000 == 0:
                log.warning("satellite %s: listening is behind, %d microphone frames dropped",
                            self.id, self.mic_dropped)
        self.mic.put_nowait(pcm)

    def flush_speaker(self) -> bool:
        """Drop queued audio and the rest of the item playing. True when
        there was any. The satellite's own buffer (300 ms) needs a "flush"
        message too."""
        had = self.playing or not self.speaker.empty()
        while not self.speaker.empty():
            item = self.speaker.get_nowait()
            if isinstance(item, Clip):
                item.finish(False)
        self.speaker_gen += 1
        self.play_until = 0.0
        return had

    async def speaker_loop(self, allowed: Callable[[], bool]) -> None:
        """Play the queue at real time. `allowed` is Hub.speaker_allowed for
        this satellite, and it is the last word: whatever queued audio -- a
        reply handed over just as the speaker was turned off, a tone playing
        when the satellite was forgotten -- goes nowhere once it says no. The
        callers check too, but each check before a queue is a moment before
        the audio plays.

        The item in hand is finished whatever the way out: cancelled with its
        socket, a Clip still resolves `done` (False), or an announcement
        waiting on it would wait for ever."""
        chunk = self.spk_rate * 2 * SPEAKER_CHUNK_MS // 1000
        while True:
            item = await self.speaker.get()
            clip = item if isinstance(item, Clip) else None
            pcm = clip.pcm if clip is not None else item
            gen = self.speaker_gen
            self.playing = True
            # A reply arrives a sentence at a time. Each one carries on from
            # the audio still buffered on the satellite rather than starting
            # the clock again, or every sentence would add another
            # SPEAKER_LEAD_S to what the satellite holds (300 ms of buffer).
            base, sent, played = max(time.monotonic(), self.play_until), 0.0, False
            first = time.monotonic()
            try:
                for off in range(0, len(pcm), chunk):
                    # Checked per chunk: a reply is one item, and a flush that
                    # only emptied the queue would let a two-minute answer
                    # play on.
                    piece = pcm[off:off + chunk]
                    if not await self.send_if(lambda: self.speaker_gen == gen and allowed(),
                                              data=lambda: self.speaker_frame(piece)):
                        break
                    if clip is not None:
                        clip.started = True
                    sent += len(piece) / 2 / self.spk_rate
                    self.play_until = base + sent
                    ahead = self.play_until - time.monotonic()
                    if ahead > SPEAKER_LEAD_S:
                        await asyncio.sleep(ahead - SPEAKER_LEAD_S)
                else:
                    played = True
            finally:
                self.playing = False
                if clip is not None:
                    clip.finish(played)
            if played and sent and self.sense is not None:
                self.sense.played(self.sound(first, self.play_until, pcm))


async def _quietly(coro) -> bool:
    """A send to a satellite that may have gone: a conversation cleaning up
    after a disconnect must not raise out of its finally block."""
    try:
        await coro
        return True
    except Exception as e:  # a closed socket raises several different things
        log.debug("send failed: %s", e)
        return False


async def _outcome(coro) -> Any:
    """What `coro` returns, or the exception it raised: for a task whose
    failure is an answer (Hub._check's STT call), not a crash to log."""
    try:
        return await coro
    except Exception as e:
        return e


async def _sent(coro) -> bool:
    """_quietly for Session.send_if: True only when the message went out,
    False when the setting refused it or the socket had gone."""
    try:
        return await coro
    except Exception as e:
        log.debug("send failed: %s", e)
        return False


class Voice:
    """The wake word models, and which satellite listens for which
    (wakewords_config.Assignment, wake_words.json).

    ONE SET OF SESSIONS, ONE STREAM PER SATELLITE ON ITS OWN WORDS. `base` is a
    WakeWords holding every word that is assigned anywhere; each satellite's
    Ear gets base.clone(<its words>), which shares the ONNX sessions and runs
    only those. plan() says which clone a satellite should have, and
    listen_loop swaps it in between two batches when that changes, so a new
    assignment reaches a listening satellite live, without a restart and
    without touching the others.

    What changes what:
      * a threshold: base.thresholds is updated in place, and every clone
        shares that dict, so no stream is rebuilt or reset;
      * an assignment: only the satellites whose words changed get a new
        clone (and 1.2 s of warm-up, see WakeWords);
      * a word named for the first time: fetched into the model directory in
        the background ("downloading"), then base is rebuilt with it and every
        satellite gets a clone of the new one ("ready");
      * a removed word: gone from plan() at once, and listen_loop also drops
        any detection of it still in flight. base keeps its session until the
        next rebuild, which costs one model's memory and resets nobody.

    Until a word is ready, and if it fails, satellites still stream, adopt and
    take push-to-talk; only that word waits."""

    def __init__(self, model_dir: Path, frontend: bool,
                 assignment: wakewords_config.Assignment | None = None,
                 on_change=None):
        self.model_dir = model_dir
        self.frontend = frontend
        self.assignment = assignment or wakewords_config.Assignment()
        self.on_change = on_change        # a word became ready or failed
        self.base: wakeword.WakeWords | None = None
        self.generation = 0               # bumped each time base is replaced
        self.failed: dict[str, str] = {}  # name -> why, until a PUT retries it
        self._lock = asyncio.Lock()

    # -- what is live ---------------------------------------------------------

    def word_state(self, name: str) -> tuple[str, str | None]:
        if self.base is not None and name in self.base.thresholds:
            return "ready", None
        if name in self.failed:
            return "error", self.failed[name]
        # Being fetched, or loaded from a file already on the volume: either
        # way it is not listened for yet.
        return "downloading", None

    @property
    def state(self) -> str:
        states = [self.word_state(w.name)[0] for w in self.assignment.words]
        if not states:
            return "off"
        if "downloading" in states:
            return "loading"
        return "failed" if all(s == "error" for s in states) else "ready"

    @property
    def error(self) -> str | None:
        problems = ([self.assignment.load_error] if self.assignment.load_error else []) + [
            f"{name}: {why}" for name, why in self.failed.items()
            if any(w.name == name for w in self.assignment.words)]
        return "; ".join(problems) or None

    def views(self) -> list[dict]:
        out = []
        for w in self.assignment.words:
            state, error = self.word_state(w.name)
            out.append(w.as_json() | {"state": state, "error": error})
        return out

    async def describe(self, *, admin: bool) -> dict:
        """GET /satellites/wake-words. Whole for satellites:admin; anyone else
        gets redacted_words(): Home Assistant needs a word's name and mode,
        and nothing of where its words go or which secret goes with them."""
        if not admin:
            return {"words": [redacted_word(v) for v in self.views()],
                    "ptt": {"mode": self.assignment.ptt.mode}}
        actions = wakewords_config.WordActions(self.assignment)
        return {"available": wakewords_config.available(self.model_dir), "words": self.views(),
                "ptt": self.assignment.ptt.model_dump(mode="json"),
                "custom": wakewords_config.custom(self.model_dir),
                # Which secrets the actions name have a value in the secret
                # store: names and booleans only, never a value. The page
                # stores one through PUT /admin/secrets/{name}.
                "env": await routing.env_status(actions.env_vars()),
                # Which of a language model's tools work on this hub: web
                # search only with SATELLITES_SEARXNG_URL.
                "tools": tooling.available(),
                "warnings": actions.warnings(lookup_satellite_safe),
                "load_error": self.assignment.load_error}

    def health(self) -> dict:
        return {"state": self.state, "wake_words": self.assignment.thresholds(),
                "error": self.error, "frontend": self.frontend,
                "model_dir": str(self.model_dir)}

    # -- per satellite --------------------------------------------------------

    def listens(self, nid: str, name: str) -> bool:
        return name in self.assignment.effective(nid)

    def plan(self, nid: str) -> tuple[tuple, wakeword.WakeWords | None, list[str]]:
        """(key, base, names): the loaded words this satellite listens for,
        the base to clone them from, and a key that changes exactly when that
        clone would. Read on the event loop; the clone itself is made in the
        thread pool from the base returned here, so a base replaced meanwhile
        cannot hand a satellite a stream the key does not describe."""
        base = self.base
        names = [n for n in self.assignment.effective(nid)
                 if base is not None and n in base.thresholds]
        return (self.generation, tuple(names)), base, names

    # -- changes --------------------------------------------------------------

    def replace(self, words: list[wakewords_config.Word],
                ptt: routing.Behaviour | None = None) -> None:
        """Save a new assignment and make what can be live, live: thresholds
        and actions now, plan() now. Words not loaded yet need reconcile()."""
        self.assignment.replace(words, ptt)
        # A save is also the retry for a word that failed to fetch: the network
        # may be back, or the model file may have been dropped into the volume.
        self.failed.clear()
        self._apply_thresholds()

    def forget(self, nid: str) -> None:
        try:
            if self.assignment.forget(nid):
                log.info("satellite %s taken out of every wake word it was assigned", nid)
        except OSError as e:
            # The satellite is forgotten either way; a stale id in the file
            # only names a satellite that is no longer adopted.
            log.error("could not save %s after forgetting %s: %s",
                      wakewords_config.FILE, nid, e)

    def _apply_thresholds(self) -> None:
        if self.base is None:
            return
        for name, threshold in self.assignment.thresholds().items():
            if name in self.base.thresholds:
                self.base.thresholds[name] = threshold

    async def reconcile(self) -> None:
        """Fetch and load every assigned word that is not loaded yet, and
        rebuild base with them. Run at start-up and after every PUT; runs one
        at a time, and goes round again when the words changed while it was
        fetching."""
        async with self._lock:
            while True:
                version = self.assignment.version
                wanted = self.assignment.thresholds()
                loaded = set(self.base.thresholds) if self.base is not None else set()
                new = [n for n in wanted if n not in loaded and n not in self.failed]
                if new:
                    await self._load(new, wanted, loaded, version)
                if self.assignment.version == version:
                    return

    async def _load(self, new: list[str], wanted: dict[str, float], loaded: set[str],
                    version: int) -> None:
        fetched = []
        for name in new:
            try:
                await asyncio.to_thread(wakeword.ensure_models, [name], self.model_dir,
                                        seeds=[BAKED_MODELS])
                fetched.append(name)
            except Exception as e:
                self._fail(name, e, version)
        if fetched:
            # Every word still assigned, not what base held: a word removed
            # since the last rebuild is dropped here.
            models = {n: t for n, t in wanted.items() if n in loaded or n in fetched}
            try:
                base, bad = await asyncio.to_thread(self._build, models)
            except Exception as e:
                for name in fetched:
                    self._fail(name, e, version)
            else:
                for name, why in bad.items():
                    self._fail(name, why, version)
                self.base = base
                self.generation += 1
                # Thresholds saved while the models were loading.
                self._apply_thresholds()
                log.info("wake words ready: %s", ", ".join(
                    f"{n} at {t:g}" for n, t in (base.thresholds if base else {}).items()))
        if self.on_change is not None:
            self.on_change()

    def _build(self, models: dict[str, float]) -> tuple[wakeword.WakeWords | None, dict[str, str]]:
        """A base for these words, in the thread pool, and the words that
        would not load. One bad model file must not keep the others off, so a
        failure is narrowed down to the words that cause it."""
        try:
            return wakeword.WakeWords(models, self.model_dir), {}
        except Exception:
            bad = {}
            for name, threshold in models.items():
                try:
                    wakeword.WakeWords({name: threshold}, self.model_dir)
                except Exception as e:
                    bad[name] = f"{type(e).__name__}: {e}"
            if not bad:
                raise  # not any one model's fault: the shared feature models
            good = {n: t for n, t in models.items() if n not in bad}
            return (wakeword.WakeWords(good, self.model_dir) if good else None), bad

    def _fail(self, name: str, e: Exception | str, version: int) -> None:
        why = e if isinstance(e, str) else f"{type(e).__name__}: {e}"
        if self.assignment.version != version:
            # Saved again while this attempt ran. A save is the retry
            # (replace() clears `failed`), so this failure is the attempt
            # before it: recorded, it would stand for the new save and keep
            # the word in "error" until yet another one. Left out, reconcile()
            # goes round and tries the word again.
            log.warning("wake word %s failed (%s), but was saved again meanwhile; "
                        "trying it again", name, why)
            return
        self.failed[name] = why
        log.error("wake word %s is off: %s", name, why)



# How long a finished update stays on a satellite's card. A refused image is
# worth seeing when it happens; hours later "update failed: bad signature"
# reads as a fault in the satellite, which was the satellite doing its job.
OTA_RESULT_S = 600


def redacted_word(view: dict) -> dict:
    """A wake word as satellites:read sees it (§3.5): its name, threshold,
    satellites, mode and state, and its action by kind alone ("ha_assist",
    "llm", ...). Nothing of where its words go or which secret goes with
    them; named fields, so one added to a word tomorrow is not shown here."""
    kind = ((view.get("action") or {}).get("destination") or {}).get("type")
    return {k: view.get(k) for k in ("name", "threshold", "satellites", "mode", "state")} | {
        "action": {"type": kind} if kind else None}


def shown_buttons(nid: str, buttons: dict) -> dict:
    """A button mapping as satellites:admin sees it. A raw webhook URL still
    waiting to be imported (secret_import.py) is shown under the secret it is
    being imported as, never as the URL: the URL is the secret (D62)."""
    return {button: {edge: (f"webhook:secret:{secret_import.button_secret(nid, button, edge)}"
                            if secret_import.RAW_WEBHOOK.match(str(action)) else action)
                     for edge, action in (edges or {}).items()}
            for button, edges in buttons.items()}


def shown_config(nid: str, config: dict | None, *, admin: bool) -> dict | None:
    """A satellite's config for a caller: `buttons` only with satellites:admin
    (D62). Home Assistant builds its button entities from caps.buttons."""
    if config is None:
        return None
    if not admin:
        return {k: v for k, v in config.items() if k != "buttons"}
    return config | {"buttons": shown_buttons(nid, config.get("buttons") or {})}


class HelloThrottle:
    """Hellos that prove no adoption, counted per address for a minute.

    Bounded however many addresses come (recheck M-4): the least recently
    heard is forgotten first, which costs it only its count."""

    def __init__(self, per_minute: int = HELLOS_PER_MINUTE, addresses: int = HELLO_ADDRESSES,
                 clock: Callable[[], float] = time.monotonic):
        self.per_minute, self.addresses, self.clock = per_minute, addresses, clock
        self._heard: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, address: str | None) -> bool:
        now, key = self.clock(), address or "unknown"
        heard = self._heard.pop(key, None) or deque()
        while heard and now - heard[0] >= 60.0:
            heard.popleft()
        allowed = len(heard) < self.per_minute
        if allowed:
            heard.append(now)
        self._heard[key] = heard
        while len(self._heard) > self.addresses:
            self._heard.popitem(last=False)
        return allowed


def _ota_view(s: "Session | None") -> dict | None:
    if s is None or not s.ota:
        return None
    done = s.ota.get("finished_at")
    if done is not None and time.time() - done > OTA_RESULT_S:
        return None
    return {k: v for k, v in s.ota.items() if k != "image"}

class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.sessions: dict[str, Session] = {}
        self.seen: dict[str, dict] = {}  # unadopted satellites, in memory only
        self.listeners: set[asyncio.Queue] = set()
        self.voice = Voice(MODEL_DIR, False)
        self.bridge: MqttBridge | None = None
        self.http: httpx.AsyncClient | None = None  # button webhooks
        self.tasks: set[asyncio.Task] = set()
        # The last LATENCY_TURNS turns' timelines per satellite, for GET
        # /satellites/{id}. Memory only: a restart starts the count again.
        self.latency: dict[str, deque] = {}
        # (satellite, trigger word) -> time.monotonic() before which it does
        # not fire again.
        self.cooldown: dict[tuple[str, str], float] = {}
        # Off until turned on (telemetry.py); None in a hub built without one.
        self.telemetry: telemetry.Recorder | None = None
        self.hellos = HelloThrottle()

    def record(self, record: dict) -> None:
        """One telemetry record, when telemetry is on."""
        if self.telemetry is not None:
            self.telemetry.write(record)

    def record_wake(self, s: Session, heard: listening.Heard, decision: str,
                    check: WakeCheck | None = None) -> None:
        """What the hub did with a wake word, for telemetry: `decision` is
        started, carried_on, interrupted, ignored_busy, trigger,
        trigger_cooldown, rejected (the double-check did not hear the word)
        or superseded (another wake word, the stop button, a mute, the
        satellite going away or its listener starting again after a fault
        came before its check was done).
        A word being double-checked is recorded once STT has answered too,
        with `verify` (WakeCheck)."""
        if check is not None:
            check.acted = decision
            if check.decision is None:
                return  # Hub._check writes it
        if self.telemetry is None or not self.telemetry.enabled:
            return
        self.record({"kind": "wake", "satellite": s.id, "word": heard.wake_word,
                     "score": heard.score,
                     "threshold": self.voice.assignment.thresholds().get(heard.wake_word),
                     "direction": heard.direction, "decision": decision,
                     "ptt": heard.score is None or None,
                     "verify": check.view() if check is not None else None})

    def publish(self, event: dict) -> None:
        event = {"at": time.time()} | event
        for q in list(self.listeners):
            if q.qsize() < 256:
                q.put_nowait(event)
        # An injected clip is a test: it must not fire the household's
        # automations through Home Assistant.
        if self.bridge is not None and not event.get("injected"):
            self.bridge.publish_event(event)

    def spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """A background task the hub keeps a reference to (asyncio holds only
        a weak one) and whose failure is logged rather than lost."""
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background task %s failed", t.get_name(), exc_info=t.exception())
        task.add_done_callback(done)
        return task

    def speaks_for(self, s: Session | None) -> bool:
        """Whether what this connection reports is its id's: it proved the
        adoption, or nobody has adopted that id. A satellite's id is its MAC,
        which is no secret, so a connection without the token of an adopted
        satellite that is offline is only pending under its id: its status,
        firmware and update are not the satellite's, on the page or in Home
        Assistant, and the satellite stays offline."""
        return s is not None and (s.adopted or s.id not in self.store.satellites)

    def describe(self, nid: str, *, admin: bool = False) -> dict:
        """One satellite, for the API (with `admin` from the caller's
        assertion), for MQTT and for the events: without satellites:admin,
        and by default, no `config.buttons` (D62)."""
        rec = self.store.satellites.get(nid)
        s = self.sessions.get(nid)
        if not self.speaks_for(s):
            s = None
        seen = self.seen.get(nid, {})
        return {
            "id": nid,
            "name": rec.name if rec else (s.hello.get("name") if s else "") or "",
            "adopted": rec is not None,
            "online": s is not None,
            "model": s.model if s else (rec.model if rec else seen.get("model")),
            "firmware": s.fw if s else seen.get("fw"),
            # Where a pending one said hello from, connected or not (recheck
            # L10): the address a stranger's hellos come from is worth seeing.
            "address": s.address if s else seen.get("address"),
            "connected_at": s.connected_at if s else None,
            "last_seen": seen.get("last_seen"),
            "config": shown_config(nid, rec.config if rec else None, admin=admin),
            "status": s.status if s else {},
            # Offline, the caps it last proved its adoption with: what it is
            # does not change when it is switched off. {} is never seen.
            "caps": s.caps if s else (rec.caps if rec else {}),
            # "speaker", "jack", or None: not known until something has
            # played since it connected (output.py).
            "output": s.sense.output if s and s.sense else None,
            "output_at": s.sense.at if s and s.sense else None,
            "boot": ({"reset_reason": s.hello.get("reset_reason"), **(s.hello.get("boot") or {})}
                     if s and s.hello.get("boot") is not None else None),
            "ota": _ota_view(s),
            "listening": self._listening(s),
            "earcons": self._earcons(s),
            # Assigned, whether or not the model has loaded yet: GET
            # /satellites/wake-words has each word's state.
            "wake_words": self.voice.assignment.effective(nid),
            "latency": self.latency_summary(nid),
            "media": self.media_view(nid),
            "update": self.available_update(nid),
        }

    def media_view(self, nid: str) -> dict | None:
        """What POST /satellites/{id}/media takes for this satellite, and what
        it plays: the satellite whose speaker plays it (`through`, its Output),
        the format of music and of an announcement there, and the stream
        playing there for this satellite. None when it cannot play media now:
        offline, not adopted, or playing through a satellite with no speaker.
        Home Assistant converts to exactly these, so the hub never decodes or
        resamples anything."""
        s = self.sessions.get(nid)
        if s is None or not s.adopted:
            return None
        t = self.speaker_for(s)
        if t.caps and "speaker" not in t.caps:
            return None
        rate, channels = t.media_format or (t.spk_rate, 1)
        playing = t.media is not None and (t is s or t.media.source == s.id)
        return {"through": t.id, "music": {"rate": rate, "channels": channels},
                "announce": {"rate": t.spk_rate, "channels": 1},
                "playing": t.media.view() if playing else None}

    def available_update(self, nid: str) -> dict | None:
        """The image POST /satellites/ota would install on this satellite now,
        when it would be an update: the newest for its model by the page's
        own rule (satNewest in ui.html), among those the satellite would take
        (signing.skip_reason), and neither what it runs, nor older, nor what
        an update in progress is installing. Home Assistant offers exactly
        this, because only the hub knows the keys, and git describe versions
        do not compare as Home Assistant compares versions."""
        rec = self.store.satellites.get(nid)
        if rec is None:
            return None
        s = self.sessions.get(nid)
        if not self.speaks_for(s):
            s = None
        model = s.model if s else rec.model
        caps = s.caps if s else rec.caps
        installed = s.fw if s else self.seen.get(nid, {}).get("fw")
        if not installed or installed == "unknown":
            return None
        going_to = (s.ota.get("version") if s is not None and s.ota
                    and s.ota.get("state") in ("requested", "started", "progress", "rebooting") else None)
        fw = self.store.newest_firmware(
            model, lambda f: signing.skip_reason(caps, f.signature, FIRMWARE_KEY) is None)
        if fw is None or fw.version in (installed, going_to) or firmware_older(fw.version, installed):
            return None
        return {"sha256": fw.sha256, "version": fw.version, "uploaded_at": fw.uploaded_at}

    def record_latency(self, nid: str, timeline: dict) -> None:
        if timeline.get("first_audio") is not None:
            self.latency.setdefault(nid, deque(maxlen=LATENCY_TURNS)).append(dict(timeline))

    def latency_summary(self, nid: str) -> dict | None:
        """Medians (and the 90th percentile of the first audio) over the last
        LATENCY_TURNS spoken replies, in ms from the end of speech."""
        rows = list(self.latency.get(nid, ()))
        if not rows:
            return None

        def pick(key: str, q: float = 0.5) -> float | None:
            values = sorted(r[key] for r in rows if r.get(key) is not None)
            if not values:
                return None
            if q == 0.5:
                return round(statistics.median(values))
            return round(values[min(len(values) - 1, int(q * len(values)))])
        return {"turns": len(rows), "p50_first_audio_ms": pick("first_audio"),
                "p90_first_audio_ms": pick("first_audio", 0.9),
                "p50_stt_done_ms": pick("stt_done"), "p50_first_token_ms": pick("first_token"),
                "p50_answer_done_ms": pick("answer_done"), "p50_reply_done_ms": pick("reply_done")}

    @staticmethod
    def _listening(s: Session | None) -> dict | None:
        # getattr throughout: tests stand a SimpleNamespace in for a Session.
        ear = getattr(s, "ear", None)
        if ear is None:
            err = getattr(s, "listen_error", None)
            return {"state": "off", "error": err} if err else None
        conv = getattr(s, "conversation", None)
        return ear.stats() | {"mic_dropped": s.mic_dropped,
                              "conversation": conv.phase if conv else None,
                              "session": conv.session_view() if conv else None}

    @staticmethod
    def _earcons(s: Session | None) -> dict | None:
        sync = getattr(s, "earcons", None)
        if sync is None:
            return None
        return {"ready": sync.ready, "have": sorted(e for e in sync.want if sync.has(e)),
                "failed": sync.failed}

    def find(self, ref: str) -> list[str]:
        """A satellite id (with or without colons), a satellite name, or
        "all"."""
        if ref == "all":
            return [n for n in self.sessions if n in self.store.satellites]
        nid = satellite_id(ref)
        if nid in self.store.satellites or nid in self.sessions or nid in self.seen:
            return [nid]
        return [n.id for n in self.store.satellites.values() if n.name == ref]

    def resolve(self, ref: str) -> str:
        """One satellite, by id or name, or a 404."""
        found = [n for n in self.find(ref) if n != "all"] if ref != "all" else []
        if len(found) != 1:
            raise ApiError(404, f"no single satellite matches {ref!r}", code="satellite_not_found")
        return found[0]

    def session(self, ref: str, *, adopted: bool = True) -> Session:
        nid = self.resolve(ref)
        s = self.sessions.get(nid)
        if s is None:
            raise ApiError(409, f"satellite {nid} is not connected", code="satellite_offline")
        if adopted and not s.adopted:
            raise ApiError(409, f"satellite {nid} is not adopted", code="satellite_not_adopted")
        return s

    def config(self, s: Session) -> dict:
        rec = self.store.satellites.get(s.id)
        return rec.config if rec else {}

    def imported_buttons(self, imported: dict[tuple[str, str, str], str]) -> None:
        """Button webhooks the secret store now holds: each mapping names its
        secret instead of the URL (D62), and satellites.json holds no URL."""
        changed = set()
        for (nid, button, edge), name in imported.items():
            rec = self.store.satellites.get(nid)
            edges = (rec.config.get("buttons") or {}).get(button) if rec else None
            if edges and secret_import.RAW_WEBHOOK.match(str(edges.get(edge, ""))):
                edges[edge] = f"webhook:secret:{name}"
                changed.add(nid)
        if not changed:
            return
        try:
            self.store.save_satellites()
        except OSError as e:
            log.error("could not save %s after importing button webhooks (%s); saved with the "
                      "next change", "satellites.json", type(e).__name__)
        for nid in sorted(changed):
            self.publish({"type": "config", "satellite": nid, "changed": ["buttons"]})
            _mqtt_satellite(nid)
        log.info("button webhooks now name their secrets on %s", ", ".join(sorted(changed)))

    def proves_adoption(self, s: Session) -> bool:
        rec = self.store.satellites.get(s.id)
        return bool(rec and rec.accepts(s.hello.get("token") or None))

    @staticmethod
    def button_actions(s: Session, config: dict) -> dict:
        """The part of the mapping the satellite runs itself, for firmware
        that says it can (caps "actions"); older firmware keeps its own."""
        if not isinstance(s.caps.get("actions"), list) or "buttons" not in config:
            return {}
        return {"button_actions": device_actions(config["buttons"])}

    def take_own(self, s: Session) -> None:
        """The settings in the status that answers a button's action on the
        satellite (volume, lights, brightness) are the ones it now has, so
        they are the hub's too: the page and Home Assistant show them, and
        the next welcome does not put the old ones back. Any other status is
        not believed on these: one already on its way when the page changed
        one carries the old value."""
        before = dict(self.config(s))
        mine = {k: v for k, v in reported_config(s.status).items() if k in BUTTON_SETTINGS}
        if mine and self.store.take_own(s.id, mine):
            changed = {k: v for k, v in mine.items() if before.get(k) != v}
            log.info("satellite %s set its own %s", s.id, changed)
            self.publish({"type": "settings", "satellite": s.id, "settings": changed})

    def take_report(self, s: Session, msg: dict) -> None:
        """Settings the hub had not had from this satellite, from its hello or
        status (Store.take_report). Home Assistant shows the record, so it is
        told: over MQTT, and by a "config" event naming what changed, never
        the values (it reads them back)."""
        before = dict(self.config(s))
        if self.store.take_report(s.id, msg):
            after = self.config(s)
            log.info("satellite %s reported %s", s.id, satellite_config(after))
            changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            if changed:
                self.publish({"type": "config", "satellite": s.id, "changed": changed})
            if self.bridge is not None:
                self.bridge.publish_satellite(self.describe(s.id))

    async def unadopt(self, s: Session) -> None:
        """A connected satellite becomes pending: forgotten, or a hello whose
        token does not match. `adopted` goes first, because the speaker loop
        and every earcon, light and duck read it at the moment of sending: the
        tone still playing and the conversation cancelled here stop at their
        next send instead of streaming on to a pending satellite.

        Then the satellite is told to drop what it has buffered, its media
        included, and to lift a duck the hub was holding. All of it goes out
        ahead of the "forget" or "pending" the caller sends next, which the
        satellite reads in order, so it takes them while it still counts
        itself adopted.

        The media stops before the first send. While one waited on the
        socket, the upload playing found the speaker no longer allowed and
        ended as speaker_off on its own, and with nothing left to stop, a Pi
        was never sent its media_flush."""
        was = s.adopted
        s.adopted = False
        had_audio = s.flush_speaker()
        ducked = s.duck_holds > 0 and s.caps.get("duck")
        s.duck_holds = 0
        self.stop_listening(s, "unadopted")
        s.earcons = None
        await self.stop_media(s, "unadopted")
        if was and had_audio:
            await _quietly(s.send_json({"type": "flush"}))
        if was and ducked:
            await _quietly(s.send_json({"type": "unduck"}))

    async def greet(self, s: Session) -> None:
        """Answer a hello: welcome with the config, or pending."""
        rec = self.store.satellites.get(s.id)
        token = s.hello.get("token") or None
        if self.proves_adoption(s):
            # Firmware from 2026-09-25 says its settings in the hello, so a
            # satellite adopted before its first status is welcomed with all
            # of them; older firmware keeps the ones it has not reported.
            self.take_report(s, s.hello)
            # Only from a hello that proves the adoption: anyone can say
            # anything in a hello under a MAC that is no secret.
            self.store.remember_caps(s.id, s.caps)
            s.adopted = True
            await s.send_json({"type": "welcome", "name": rec.name,
                               "config": satellite_config(rec.config, rec.unreported)
                               | self.button_actions(s, rec.config)})
            self.publish({"type": "online", "satellite": s.id, "name": rec.name, "firmware": s.fw})
            await self.sync_earcons(s)
            self.start_listening(s)
        else:
            await self.unadopt(s)
            if rec and token:
                log.warning("satellite %s presented a token that does not match its adoption", s.id)
            if self.speaks_for(s):
                self.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time(),
                                   "address": s.address}
            await s.send_json({"type": "pending"})
            self.publish({"type": "pending", "satellite": s.id, "address": s.address})
            await self.limit_pending()

    async def limit_pending(self) -> None:
        """Keep at most PENDING_MAX unadopted satellites, the newest. Anyone on
        the internet can say hello under any MAC, and refusing the newest
        would let them keep a real satellite off the list (recheck L10); an
        evicted one that is real says hello again and is back. A connected
        one is closed, and not remembered as seen when its socket ends."""
        when = {nid: info.get("last_seen", 0.0) for nid, info in self.seen.items()
                if nid not in self.store.satellites}
        for nid, s in self.sessions.items():
            if not s.adopted and nid not in self.store.satellites:
                when[nid] = max(when.get(nid, 0.0), s.connected_at)
        for nid in sorted(when, key=when.get)[:max(0, len(when) - PENDING_MAX)]:
            self.seen.pop(nid, None)
            s = self.sessions.get(nid)
            if s is not None and not s.adopted:
                s.evicted = True
                await _quietly(s.close(CLOSE_TRY_LATER))
            # DEBUG: a stranger's flood of hellos would otherwise flood the log.
            log.debug("satellite %s: pending for longest, forgotten to make room for a newer one",
                      nid)

    # -- listening ----------------------------------------------------------

    def may_listen(self, s: Session) -> bool:
        cfg = self.config(s)
        return s.adopted and cfg.get("mic_enabled", True) and not s.status.get("muted")

    def start_listening(self, s: Session) -> None:
        if s.listener is not None and not s.listener.done():
            return
        if not s.has_mic:
            s.listen_error = "it has no microphone"
            return
        try:
            s.ear = listening.Ear(debug_s=DEBUG_AUDIO_S, rate=s.mic_rate, channels=s.mic_channels,
                                  frontend=self.voice.frontend,
                                  silence_for=lambda word: silence_for(s.id, word))
        except ValueError as e:
            s.listen_error = str(e)
            log.warning("satellite %s will not be listened to: %s", s.id, e)
            return
        s.listen_error = None
        s.listener = asyncio.create_task(listen_loop(self, s), name=f"listen-{s.id}")

    def stop_listening(self, s: Session, reason: str = "stopped listening") -> None:
        if s.listener is not None:
            s.listener.cancel()
            s.listener = None
        if s.conversation is not None:
            s.conversation.cancel(reason)
        s.checking = None
        s.ear = None
        while not s.mic.empty():
            s.mic.get_nowait()

    # -- the double-check (verify.py) ---------------------------------------------

    def on_wake(self, s: Session, heard: listening.Heard) -> None:
        """Every wake word the listener reports, before heard() acts on it:
        the double-check, as the word's `verify` says (router.VerifySettings).

          off  heard() at once, the model's word alone;
          log  heard() at once, and the check alongside it, which only
               records what "on" would have done;
          on   nothing anyone can see or hear -- no earcon, duck, ring or
               conversation -- until STT has heard the word (_check). The Ear
               keeps capturing the command meanwhile, and the check holds
               what it captures (listen_loop).

        Push-to-talk has no clip and is never checked. A newer wake word on
        the same satellite supersedes a check still under way there: that
        one's result is recorded and not acted on, and what it held goes."""
        s.checking = None
        behaviour = self.voice.assignment.behaviour(heard.wake_word)
        settings = behaviour.verify if behaviour is not None else routing.VerifySettings()
        if heard.clip is None or settings.mode == "off" or not s.adopted:
            self.heard(s, heard)
            return
        check = WakeCheck(heard, settings, captures=behaviour is None or behaviour.mode != "trigger")
        if check.mode == "on":
            s.checking = check
        self.spawn(self._check(s, check), name=f"verify-{s.id}")
        if check.mode == "log":
            self.heard(s, heard, check)

    async def _check(self, s: Session, check: WakeCheck) -> None:
        """STT on the wake word's own audio, told to listen for the word
        (verify.vocabulary, the transcription's `boost`), the word looked for
        in what it heard (verify.matches), and then what the word's mode
        says. A check STT could not answer lets a TRIGGER through (`error`):
        the model has already fired, and a trigger sends nothing anywhere.
        A command or conversation word it drops instead (rejected,
        `unchecked`): what follows the wake is about to be transcribed and
        sent to the word's destination, which may be outside the house. On
        2 Oct 2026 a busy STT let a video's "hey_claude" through unchecked
        and a language model got the room's speech. A command whose STT is
        that slow would mostly have failed anyway.

        In a task, never on the listener: the Ear keeps processing the
        microphones while STT works. The STT call is a task of its own, and
        one the check stops waiting for is left to finish rather than
        cancelled: cut off while it asked stt-stack which engines it runs,
        it left the router believing it runs none, and the next command went
        to the default engine whatever its word's language (router.Router.
        _stt_health). Its transcription is bounded by VERIFY_TIMEOUT_S all
        the same, not the router's 30 s: every word is checked by default,
        and a slow STT given a TV's wakes to transcribe for nobody would
        make the commands wait behind them.

        A check in mode "on" that something superseded is recorded as such
        and nothing more, whatever STT said: nothing came of that wake, so
        nothing says it was ignored and no clip of it is kept."""
        heard = check.heard
        t0 = time.monotonic()
        stt = routing.current().transcribe(heard.clip, timeout=verify.VERIFY_TIMEOUT_S,
                                           boost=verify.vocabulary(heard.wake_word, check.spellings))
        call = self.spawn(_outcome(stt), name=f"verify-stt-{s.id}")
        try:
            said = await asyncio.wait_for(asyncio.shield(call), verify.VERIFY_TIMEOUT_S)
        except TimeoutError:
            said = TimeoutError(f"no answer within {verify.VERIFY_TIMEOUT_S:g} s")
        if isinstance(said, Exception):  # no answer in time, an STT error, no STT at all
            why = (str(said) if isinstance(said, (TimeoutError, routing.DestinationError))
                   else type(said).__name__)
            if check.mode == "on" and check.captures:
                check.decision, check.unchecked = "rejected", True
            else:
                check.decision = "error"
                log.info("satellite %s: %s let through unchecked: %s", s.id, heard.wake_word, why)
        else:
            check.transcript = said
            check.matched = verify.matches(check.transcript,
                                           verify.spellings(heard.wake_word, check.spellings))
            check.decision = ("accepted" if check.matched is not None
                              else "rejected" if check.mode == "on" else "would_reject")
        check.ms = round((time.monotonic() - t0) * 1000, 1)
        # Mode "on", and no newer wake word or stop has taken the satellite
        # since: this check's to act on. A check in mode "log" never is.
        current = s.checking is check
        if current and check.decision != "rejected":
            s.checking = None
            self.heard(s, heard, check)
            if s.conversation is not None:
                for command in check.held:
                    s.conversation.deliver(command)
            return
        if check.mode == "on" and not current:
            self.record_wake(s, heard, "superseded", check)
            return
        if check.decision in ("rejected", "would_reject"):
            if check.unchecked:
                log.info("satellite %s: %s (score %s) ignored: the double-check could not run (%s)",
                         s.id, heard.wake_word, heard.score, why)
            else:
                log.info("satellite %s: %s (score %s) %s: STT did not hear the word", s.id,
                         heard.wake_word, heard.score,
                         "ignored" if check.decision == "rejected" else "would have been ignored")
            log.debug("satellite %s: STT heard %r for %s", s.id, check.transcript, heard.wake_word)
            self.publish({"type": "wake_rejected", "satellite": s.id, "word": heard.wake_word,
                          "score": heard.score, "heard": check.heard_text(), "mode": check.mode,
                          "unchecked": check.unchecked})
            check.clip = await self._keep_clip(s, heard)
        if check.mode == "log":
            if check.acted is not None:
                self.record_wake(s, heard, check.acted, check)
        else:
            # Rejected, the check stays Session.checking until listen_loop
            # has undone what the wake word started (drop_rejected).
            self.record_wake(s, heard, "rejected", check)

    async def _keep_clip(self, s: Session, heard: listening.Heard) -> str | None:
        """The audio of a wake word STT did not hear, kept as a hard negative
        for retraining the word's model: at telemetry level full only, as
        what was said is. Its name, or None."""
        rec = self.telemetry
        if rec is None or not rec.enabled or rec.level != "full":
            return None
        try:
            return await asyncio.to_thread(rec.save_clip, s.id, heard.wake_word,
                                           audio.wav(heard.clip, listening.RATE, 1))
        except OSError as e:
            log.warning("telemetry: a clip of %s could not be kept: %s", heard.wake_word,
                        e.strerror or type(e).__name__)
            return None

    def drop_rejected(self, s: Session, ear: listening.Ear) -> None:
        """Undo what a wake word STT did not hear had the Ear start, from the
        listener, between two process() calls: the Ear goes back to wake
        words, and the command it captured is dropped. Except in a
        conversation waiting for its next turn, which was listening anyway:
        what was said is that turn, as if the word had never been detected.
        A trigger started nothing."""
        check, s.checking = s.checking, None
        conv = s.conversation
        if conv is not None and conv.phase == "listening":
            for command in check.held:
                conv.deliver(command)
        elif check.captures:
            ear.release()

    def heard(self, s: Session, heard: listening.Heard, check: WakeCheck | None = None) -> None:
        """A wake word (or push-to-talk), once on_wake lets it through. A
        trigger word fires and that is all. Otherwise it starts a
        conversation, one per satellite at a time: heard while one is
        listening for its command or routing it, it is dropped (the Ear
        already drops those unless told otherwise); heard while a reply
        plays, or a conversation waits for its next turn, it interrupts. The
        same wake word carries the conversation on; another one ends it and
        starts its own. `check` is the word's double-check, which its
        telemetry record waits for."""
        if not s.adopted:
            return
        behaviour = self.voice.assignment.behaviour(heard.wake_word)
        if behaviour is not None and behaviour.mode == "trigger":
            self.trigger(s, heard, behaviour, check)
            return
        conv = s.conversation
        if conv is not None:
            if conv.takes(heard):
                self.record_wake(s, heard, "carried_on", check)
                return
            if not conv.interruptible:
                self.record_wake(s, heard, "ignored_busy", check)
                return
            conv.supersede()
            self.record_wake(s, heard, "interrupted", check)
        else:
            self.record_wake(s, heard, "started", check)
        rec = self.store.satellites.get(s.id)
        conv = Conversation(self, s.id, rec.name if rec else "", heard, session=s)
        s.conversation = conv
        # The pause that ends this word's command, set before the next batch.
        if s.ear is not None and conv.route is not None:
            s.ear.set_silence(conv.route.behaviour.silence_ms)
        conv.start()

    def trigger(self, s: Session, heard: listening.Heard, behaviour: routing.Behaviour,
                check: WakeCheck | None = None) -> None:
        """A trigger word: the word is the command, and Home Assistant's
        automation decides what it does. The hub publishes "triggered" once
        per cooldown and, if asked, shows it heard: the "done" earcon and a
        flash of the ring, each only where the satellite may play or light."""
        key, now = (s.id, heard.wake_word), time.monotonic()
        if now < self.cooldown.get(key, 0.0):
            log.debug("satellite %s: trigger %s again within its cooldown; ignored",
                      s.id, heard.wake_word)
            self.record_wake(s, heard, "trigger_cooldown", check)
            return
        self.cooldown[key] = now + behaviour.trigger.cooldown_s
        self.record_wake(s, heard, "trigger", check)
        rec = self.store.satellites.get(s.id)
        self.publish({"type": "triggered", "satellite": s.id,
                      "satellite_name": rec.name if rec else "", "wake_word": heard.wake_word,
                      "score": heard.score, "direction": heard.direction})
        log.info("satellite %s: trigger %s (score %s)", s.id, heard.wake_word, heard.score)
        conv = s.conversation
        if behaviour.trigger.ends_conversation and conv is not None:
            conv.cancel("trigger")
            conv = None
        if behaviour.trigger.feedback == "earcon":
            self.spawn(self._trigger_feedback(s, flash=conv is None, colour=ring_colour(behaviour)),
                       name=f"trigger-{s.id}")

    async def _trigger_feedback(self, s: Session, flash: bool,
                                colour: list[int] | None = None) -> None:
        await self.earcon(s, "done")
        # A conversation owns the ring while it runs; a flash would put out
        # its "listening".
        leds = s.caps.get("lights")
        if flash and isinstance(leds, int) and leds > 0 and not s.lit:
            if await self.send_lights(s, {"mode": "solid", "color": colour or list(LISTEN_COLOUR),
                                          "brightness": 96}):
                await asyncio.sleep(TRIGGER_FLASH_S)
                if s.conversation is None:
                    await self.send_lights(s, {"mode": "off"})

    def ptt_refusal(self, s: Session) -> tuple[str, str] | None:
        """Why push-to-talk cannot start on this satellite now, as (code,
        sentence), or None when it can."""
        if s.conversation is not None:
            return "satellite_busy", f"satellite {s.id} is in a conversation already"
        if s.checking is not None:
            # The Ear is capturing what followed a wake word, and would take
            # no push-to-talk until it is done.
            return "satellite_busy", f"satellite {s.id} is checking a wake word it heard"
        if s.status.get("muted"):
            return ("satellite_muted", f"satellite {s.id} is muted at the device; only its REC "
                                       "button unmutes it")
        if not self.config(s).get("mic_enabled", True):
            return "mic_disabled", f"satellite {s.id} has its microphone turned off"
        if s.ear is None:
            return ("not_listening", f"satellite {s.id} is not being listened to"
                    + (f": {s.listen_error}" if s.listen_error else ""))
        return None

    async def push_to_talk(self, s: Session, word: str = listening.PTT) -> bool:
        """Listen as if `word` had been heard. False, and nothing starts, when
        ptt_refusal says why not; a satellite that would hear nothing (muted,
        microphone off) plays its error earcon, rather than open a
        conversation that can only time out."""
        why = self.ptt_refusal(s)
        if why is not None:
            if why[0] in ("satellite_muted", "mic_disabled"):
                log.info("satellite %s: push-to-talk while its microphone is off or muted", s.id)
                await self.earcon(s, "error")
            return False
        s.ear.push_to_talk(word)
        return True

    async def stop(self, s: Session) -> None:
        """What the "stop" button and POST /satellites/{id}/flush do:
        silence the satellite now and drop whatever it was in the middle of,
        music included, here and on the satellite that plays for it."""
        s.flush_speaker()
        await s.send_json({"type": "flush"})
        for t in dict.fromkeys((s, self.speaker_for(s))):
            await self.stop_media(t, "stopped")
        s.checking = None
        if s.conversation is not None:
            s.conversation.cancel("stop")

    async def on_mute(self, s: Session) -> None:
        """The privacy mute, the moment it goes on: a kill switch for the
        satellite, not only for its microphone. Whatever it was in the middle
        of ends -- its conversation and any other one answering or ducked on
        it, the audio queued and playing, the duck, the hub's lights, what
        the Ear was listening for -- so one press stops a satellite that is
        stuck (answering itself, say), and nothing of it comes back at unmute."""
        ended = 0
        for other in list(self.sessions.values()):
            c = other.conversation
            if c is not None and (other is s or s in c.ducked
                                  or (c.player is not None and c.player.session() is s)):
                c.cancel("muted")
                ended += 1
        played = s.flush_speaker()
        await _quietly(s.send_json({"type": "flush"}))
        await self.stop_media(s, "muted")
        # Every conversation that held a duck here was cancelled above, and
        # finds nothing to lift when it closes; a hold left over from anything
        # else goes too.
        if s.duck_holds > 0:
            s.duck_holds = 0
            if s.caps.get("duck"):
                await _sent(s.send_if(lambda: s.adopted, {"type": "unduck"}))
        if s.lit:
            await self.send_lights(s, {"mode": "off"})
        s.checking = None
        s.ear_reset = True
        s.volume_press_until = 0.0
        log.info("satellite %s muted: %d conversation(s) ended%s", s.id, ended,
                 ", its speaker flushed" if played else "")

    # -- what a satellite can see and hear -------------------------------------

    def lights_allowed(self, s: Session) -> bool:
        return bool(s.adopted and self.config(s).get("lights_enabled", True))

    async def send_lights(self, s: Session, msg: dict) -> bool:
        """THE ONLY PLACE THIS HUB SENDS "lights". lights_enabled is read here,
        at the moment of sending (inside the socket's lock, Session.send_if),
        never cached by a caller: a conversation that started lit must still go
        dark mid-way if the setting changes."""
        if not await _sent(s.send_if(lambda: self.lights_allowed(s), {"type": "lights", **msg})):
            return False
        s.lit = msg.get("mode") != "off"
        return True

    def speaker_allowed(self, s: Session) -> bool:
        return bool(s.adopted and self.config(s).get("speaker_enabled", True))

    def output_of(self, nid: str) -> str:
        """The satellite whose speaker plays what `nid` plays: `nid` itself,
        or the one its Output names, followed for up to OUTPUT_HOPS (a loop
        ends where it would come back). When that one is not connected and
        adopted, `nid` plays it itself: a reply is not lost to a speaker
        that is off."""
        seen, cur = {nid}, nid
        for _ in range(OUTPUT_HOPS):
            rec = self.store.satellites.get(cur)
            nxt = (rec.config.get("output_satellite") if rec else None) or None
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        if cur == nid:
            return nid
        s = self.sessions.get(cur)
        return cur if s is not None and s.adopted else nid

    def speaker_for(self, s: Session) -> Session:
        """The session that plays what `s` plays (output_of)."""
        other = self.sessions.get(self.output_of(s.id))
        return other if other is not None else s

    # -- the media lane ---------------------------------------------------------

    def replying_on(self, s: Session) -> bool:
        """A conversation's reply is under way on `s`, from its first sentence
        until it is over, with its next sentence perhaps still being
        synthesised and nothing of it queued."""
        return any(c is not None and c.phase == "replying" and c.player is not None
                   and c.player.session() is s
                   for c in (o.conversation for o in self.sessions.values()))

    async def stop_media(self, s: Session, reason: str) -> bool:
        """End the media stream playing on `s`, for `reason`, and have the
        satellite drop what it holds of it when it has a media lane of its
        own. True when there was one. The upload playing it sees the stream
        stopped at its next frame, and answers with the reason."""
        stream, s.media = s.media, None
        if stream is None:
            return False
        stream.stop(reason)
        if s.media_format is not None and self.sessions.get(s.id) is s:
            await _quietly(s.send_json({"type": "media_flush"}))
        return True

    async def stream_media(self, s: Session, stream: MediaStream, pcm: AsyncIterator[bytes],
                           rate: int, channels: int) -> str:
        """Play `pcm` (the upload's audio, `rate` and `channels` as the
        satellite takes them) on `s` as `stream`, at real time, and say why
        it ended: ended, or stream.stopped, or speaker_off, disconnected,
        cancelled (the upload broke off).

        A SATELLITE WITH A MEDIA LANE (caps "media") gets frame kind 5, kept
        MEDIA_LEAD_S ahead, and plays it on a stream of its own, which it
        ducks under the voice itself.

        ANY OTHER (the Korvo, an older Pi agent) gets its speaker frames, one
        stream of them shared with the voice, so media is sent only while
        the voice lane is idle: nothing queued, nothing playing, and no reply
        under way. A reply, an earcon, Say or an announcement that arrives
        pauses the music at the next frame, and it carries on after them.
        Its firmware is unchanged: to it this is a long reply.

        A reply is queued a sentence at a time, as each is synthesised, so
        its lane can be empty between two of them; music there would be
        heard in the middle of the answer (replying_on)."""
        lane = s.media_format is not None
        whole = 2 * channels                     # the bytes of one sample of every channel
        frame_bytes = max(1, rate * SPEAKER_CHUNK_MS // 1000) * whole
        seq, until = 0, 0.0

        def voice() -> bool:
            return s.playing or not s.speaker.empty() or self.replying_on(s)

        def ended() -> str | None:
            if self.sessions.get(s.id) is not s:
                return "disconnected"
            if stream.stopped or s.media is not stream:
                return stream.stopped or "stopped"
            if not self.speaker_allowed(s):
                return "speaker_off"
            return None

        async def send(frame: bytes) -> str | None:
            nonlocal seq, until
            if lane:
                header = struct.pack("<BBBBIQ", FRAME_MEDIA, 0, channels, 0, seq, 0)
                if not await _sent(s.send_if(lambda: self.speaker_allowed(s) and s.media is stream,
                                             data=header + frame)):
                    return ended() or "disconnected"
                seq = (seq + 1) & 0xFFFFFFFF
                # From now when the upload fell behind (Home Assistant
                # still reading the stream), rather than a burst to catch up.
                until = max(until, time.monotonic()) + len(frame) / whole / rate
                lead = MEDIA_LEAD_S
            else:
                while True:
                    while voice():
                        if why := ended():
                            return why
                        await asyncio.sleep(SPEAKER_CHUNK_MS / 1000)
                    if await _sent(s.send_if(
                            lambda: self.speaker_allowed(s) and s.media is stream and not voice(),
                            data=lambda: s.speaker_frame(frame))):
                        break
                    # Refused: the voice took the lane in between, or the
                    # stream is over. Only the second is an end.
                    if why := ended():
                        return why
                s.play_until = until = max(s.play_until, time.monotonic()) + len(frame) / whole / rate
                lead = SPEAKER_LEAD_S
            stream.played_s += len(frame) / whole / rate
            ahead = until - time.monotonic()
            if ahead > lead:
                await asyncio.sleep(ahead - lead)
            return None

        carry = b""
        while True:
            if why := ended():
                return why
            try:
                chunk = await anext(pcm)
            except StopAsyncIteration:
                break
            except ClientDisconnect:
                if s.media is stream:
                    await self.stop_media(s, "cancelled")
                stream.stop("cancelled")
                return "cancelled"
            carry += chunk
            end = len(carry) // frame_bytes * frame_bytes
            for off in range(0, end, frame_bytes):
                if why := ended() or await send(carry[off:off + frame_bytes]):
                    return why
            carry = carry[end:]
        tail = carry[:len(carry) // whole * whole]
        if tail and (why := ended() or await send(tail)):
            return why
        # Answered once it has been heard, not once it has been sent: a stop
        # in the last second still stops it, and Home Assistant's player does
        # not say idle while the music plays on.
        while (left := until - time.monotonic()) > 0:
            if why := ended():
                return why
            await asyncio.sleep(min(left, 0.1))
        return ended() or "ended"

    async def earcon(self, s: Session | None, eid: str) -> bool:
        if s is not None:
            s = self.speaker_for(s)
        if s is None or s.earcons is None or not s.earcons.has(eid):
            return False
        sent = await _sent(s.send_if(lambda: self.speaker_allowed(s), earcons.play_message(eid)))
        if sent and s.sense is not None:
            # Played from the satellite's own storage, at once.
            pcm = s.earcons.want[eid]
            now = time.monotonic()
            s.sense.played(s.sound(now, now + len(pcm) / 2 / earcons.RATE, pcm))
        return sent

    def on_output(self, s: Session) -> None:
        """The loopback settled a sound and said something new: speaker or
        jack. Home Assistant hears it through publish()."""
        log.info("satellite %s plays through its %s", s.id, s.sense.output)
        self.publish({"type": "output", "satellite": s.id, "output": s.sense.output})

    # A pending satellite is sent neither. unadopt() lifts a duck the hub held
    # itself, ahead of the "forget" or "pending", and the firmware lifts one on
    # disconnect; a conversation cancelled by either finds nothing to lift.
    async def hold_duck(self, s: Session) -> None:
        s.duck_holds += 1
        if s.duck_holds == 1 and s.caps.get("duck"):
            await _sent(s.send_if(lambda: s.adopted,
                                  {"type": "duck", "level": DUCK_LEVEL, "ms": DUCK_MS}))

    async def release_duck(self, s: Session) -> None:
        if s.duck_holds <= 0:
            return
        s.duck_holds -= 1
        if s.duck_holds == 0 and s.caps.get("duck"):
            await _sent(s.send_if(lambda: s.adopted, {"type": "unduck"}))

    # -- earcons --------------------------------------------------------------

    async def sync_earcons(self, s: Session) -> None:
        """Older firmware ignores earcon messages without a word, so the
        hello is asked first. The satellite answers earcon_list with what it
        holds; Sync uploads whatever is missing or different, one at a time."""
        if not earcons.supported(s.caps):
            s.earcons = None
            return
        s.earcons = earcons.Sync(DEFAULT_EARCONS)
        s.earcon_asks = 0
        await s.send_json({"type": "earcon_list"})

    async def on_earcons(self, s: Session, msg: dict) -> None:
        sync = s.earcons
        out = sync.handle(msg)
        if isinstance(out, bytes):
            await s.send_bytes(out)
        elif out is not None:
            await s.send_json(out)
        if msg.get("type") == "earcons" and not sync.ready:
            # First mount after a blank partition: the satellite formats it in
            # the background, which has not been timed on the board yet.
            if s.earcon_asks < EARCON_RETRIES:
                s.earcon_asks += 1
                self.spawn(self._ask_earcons_later(s, sync), name=f"earcons-{s.id}")
            else:
                log.warning("satellite %s: its earcon storage never became ready", s.id)
        if msg.get("type") == "earcon_failed":
            log.warning("satellite %s: earcon %s %s failed: %s", s.id, msg.get("op"),
                        msg.get("id"), msg.get("error"))
        if sync.done and msg.get("type") in ("earcons", "earcon_stored", "earcon_failed"):
            log.info("satellite %s earcons: %s", s.id,
                     ", ".join(e for e in sync.want if sync.has(e)) or "none")

    async def _ask_earcons_later(self, s: Session, sync: earcons.Sync) -> None:
        await asyncio.sleep(EARCON_RETRY_S)
        if self.sessions.get(s.id) is s and s.earcons is sync and not sync.ready:
            await _quietly(s.send_json({"type": "earcon_list"}))

    # -- buttons --------------------------------------------------------------

    async def on_button(self, s: Session, button: str, action: str, held_ms: int | None) -> None:
        mapping = self.config(s).get("buttons") or {}
        act = (mapping.get(button) or {}).get(action)
        # An action the satellite ran itself (DEVICE_ACTIONS): it has already
        # done it, and its next status says what changed. Older firmware
        # does not mark that status, so the press opens a window for it.
        if act in DEVICE_ACTIONS:
            if act != "mute":
                s.volume_press_until = time.monotonic() + VOLUME_PRESS_S
            return
        if not act or act == "none":
            return
        if act == "ptt":
            await self.push_to_talk(s)
        elif act == "stop":
            await self.stop(s)
        elif act.startswith("webhook:"):
            # webhook:secret:<NAME>, or a raw URL still waiting to be imported,
            # which is sent only as the secret it is being imported as: a raw
            # URL written into satellites.json by hand after the import window
            # closed has no such secret, and goes nowhere (recheck L5).
            named = secret_import.SECRET_WEBHOOK.match(act)
            name = named.group(1) if named else secret_import.button_secret(s.id, button, action)
            rec = self.store.satellites.get(s.id)
            self.spawn(self._button_webhook(name, {
                "satellite": rec.name if rec else s.id, "satellite_id": s.id, "button": button,
                "action": action, "held_ms": held_ms}), name=f"button-{s.id}")

    async def _button_webhook(self, name: str, body: dict) -> None:
        """POST to the address the secret `name` holds, if its host is one the
        secret names (D41, D62). Every line names the secret and never the
        address: a Home Assistant webhook's id is a credential (recheck M-2).
        Redirects are not followed (the client says so), and the whole call is
        bounded, as every call router.py makes is."""
        try:
            url = await secret_url(name)
            async with asyncio.timeout(10):
                r = await self.http.post(url, json=body)
            # A redirect is not followed, so a 3xx delivered nothing either.
            if r.status_code >= 300:
                log.warning("button webhook %s answered %d", name, r.status_code)
        except DestinationError as e:
            log.warning("button webhook %s: nothing was sent: %s", name, e)
        except Exception as e:
            log.warning("button webhook %s failed: %s", name, type(e).__name__)


hub: Hub


# ---- the listening path -------------------------------------------------------


async def listen_loop(h: Hub, s: Session) -> None:
    """Drain one satellite's microphone queue through its Ear, off the event
    loop.

    Never dies of a fault in the signal code: the Ear is rebuilt and the next
    frame is processed, because a listener that stopped would leave a satellite
    that looks fine and never answers."""
    loop = asyncio.get_running_loop()
    step = s.mic_channels * 2
    while True:
        chunks = [await s.mic.get()]
        while len(chunks) < MIC_BATCH and not s.mic.empty():
            chunks.append(s.mic.get_nowait())
        ear = s.ear
        if ear is None:
            return
        if s.ear_reset:
            s.ear_reset = False
            ear.cancel_ptt()
            ear.release()
        if s.checking is not None and s.checking.decision == "rejected":
            h.drop_rejected(s, ear)
        if not h.may_listen(s):
            ear.cancel_ptt()
            continue
        # Checked before every batch, and cheap when nothing changed: this is
        # how a word assigned, removed or loaded reaches a satellite that is
        # already listening. Swapped here, between two process() calls,
        # because the Ear is never in the thread pool at this point.
        key, base, names = h.voice.plan(s.id)
        if ear.wake_key != key:
            try:
                ear.wake = (await loop.run_in_executor(EXECUTOR, base.clone, names)
                            if names else None)
            except Exception:
                log.exception("satellite %s: its wake words could not be set up", s.id)
                ear.wake = None
            ear.wake_key = key
        ear.triggers = h.voice.assignment.triggers()
        # Nothing waits for what the Ear is capturing: its conversation is
        # over, or never started. A wake word being double-checked owns it
        # until it is decided.
        if ear.state != "idle" and s.conversation is None and s.checking is None:
            ear.release()
        data = b"".join(c for c in chunks if len(c) % step == 0)
        if not data:
            continue
        frames = np.frombuffer(data, dtype="<i2").reshape(-1, s.mic_channels)
        try:
            events = await loop.run_in_executor(EXECUTOR, ear.process, frames)
        except Exception:
            log.exception("satellite %s: listening failed; starting it again", s.id)
            s.ear = listening.Ear(debug_s=DEBUG_AUDIO_S, rate=s.mic_rate, channels=s.mic_channels,
                                  frontend=h.voice.frontend,
                                  silence_for=lambda word: silence_for(s.id, word))
            if s.conversation is not None:
                s.conversation.cancel("listening failed")
            # A wake word still being checked went with the old Ear and the
            # command it was capturing: heard now, it would start a
            # conversation with nothing to listen to.
            s.checking = None
            continue
        near = ear.near_misses()
        if near and h.telemetry is not None and h.telemetry.enabled:
            thresholds = h.voice.assignment.thresholds()
            for word, peak in near:
                h.telemetry.near_miss(s.id, word, peak, thresholds.get(word))
        dropped = False
        for ev in events:
            if isinstance(ev, listening.Heard):
                # A batch that was in the thread pool when a word was removed
                # or moved to another satellite ran on the old detector. What
                # it heard is dropped here, so a removal is final the moment
                # PUT /satellites/wake-words answers, not one batch later.
                # Push-to-talk has no score and is never assigned.
                if ev.score is not None and not h.voice.listens(s.id, ev.wake_word):
                    if ev.wake_word not in ear.triggers:
                        ear.release()
                        dropped = True
                    continue
                dropped = False
                h.on_wake(s, ev)
            elif isinstance(ev, listening.BargeIn):
                if s.conversation is not None:
                    s.conversation.barge_in(ev)
            elif dropped:
                continue  # the command after a dropped wake word
            else:
                # The command after a wake word still being checked, or
                # rejected, is that word's, whatever conversation is running.
                check = s.checking if s.checking is not None and s.checking.captures else None
                if check is None and s.conversation is None:
                    continue
                if isinstance(ev, listening.Command) and ev.debug:
                    loop.run_in_executor(EXECUTOR, _save_debug, s.id, ev)
                if check is not None:
                    check.held.append(ev)
                else:
                    s.conversation.deliver(ev)
        if s.conversation is not None and s.conversation.phase == "listening":
            await s.conversation.point(ear.direction)


def _save_debug(sid: str, command: "listening.Command") -> None:
    """Write one command's surroundings under <data>/debug and keep the last
    DEBUG_KEEP; runs in the thread pool."""
    d = DATA_DIR / "debug"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for kind in ("processed", "raw"):
        (d / f"{stamp}-{sid}-{kind}.wav").write_bytes(audio.wav(command.debug[kind], listening.RATE, 1))
    (d / f"{stamp}-{sid}-command.wav").write_bytes(audio.wav(command.audio, listening.RATE, 1))
    for old in sorted(d.glob("*.wav"))[:-DEBUG_KEEP * 3]:
        old.unlink(missing_ok=True)
    log.info("satellite %s: debug audio saved as %s-*", sid, stamp)

class Player:
    """dialogue.Sink for a live conversation: each sentence of the reply
    queued on the satellite it is for, as soon as it is synthesised.

    The reply is spoken over nothing: before its first sentence, audio still
    playing there from an earlier reply is dropped, and the duck is lifted,
    because the firmware ducks the hub's audio and the reply is the hub's
    audio. A satellite that may not play (speaker off, forgotten, gone) is
    handed nothing, and the conversation carries on in events alone."""

    def __init__(self, conv: Conversation, out: routing.Outcome):
        self.conv, self.hub, self.out = conv, conv.hub, out
        self.clips: list[Clip] = []
        self.began = False
        # How long the clips play, counted before each is resampled to the
        # satellite's rate: their bytes at that rate, read as 48 kHz, made a
        # 16 kHz satellite's reply a third as long as it is.
        self.seconds = 0.0

    def session(self) -> Session | None:
        target = self.out.reply_to
        s = self.hub.sessions.get(target) if target else None
        return s if s is not None and s.adopted else None

    async def play(self, pcm48k: bytes, text: str) -> bool:
        target = self.session()
        if target is None:
            self.conv.note = f"{self.out.reply_to} is not connected"
            if not self.began:
                self.began = True
                await self.hub.earcon(self.conv.s, "error")
            return False
        if not self.hub.speaker_allowed(target):
            self.conv.note = f"{self.out.reply_to} has its speaker off"
            return False
        if not self.began:
            self.began = True
            await self.conv.before_first_audio(target)
            # Asked again: the sends above wait on the socket, and a PATCH in
            # between flushed a queue that did not hold this reply yet.
            if not self.hub.speaker_allowed(target):
                self.conv.note = f"{self.out.reply_to} has its speaker off"
                return False
        self.seconds += len(pcm48k) / 2 / routing.SPEAKER_RATE
        if target.spk_rate != routing.SPEAKER_RATE:
            pcm48k = audio.resample(pcm48k, routing.SPEAKER_RATE, target.spk_rate)
        clip = Clip(pcm48k, text)
        self.clips.append(clip)
        target.speaker.put_nowait(clip)
        return True

    async def finish(self) -> None:
        """Until the last sentence has been sent and the satellite has played
        what it buffered."""
        if not self.clips:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.clips[-1].done),
                                   self.seconds + PLAYBACK_SLACK_S)
        except TimeoutError:
            return
        target = self.session()
        left = target.play_until - time.monotonic() if target is not None else 0.0
        if left > 0:
            await asyncio.sleep(left)

    def spoken(self) -> str:
        return " ".join(c.text for c in self.clips if c.started)

    def drop(self) -> bool:
        """Stop this reply, whatever of it is queued or playing. True when the
        satellite had anything to flush."""
        target = self.session()
        if target is None or not self.clips:
            return False
        for c in self.clips:
            c.finish(False)
        return target.flush_speaker()


class Conversation:
    """One wake word, or one push of a button, through to its last reply.

    A COMMAND is one turn: the words after the wake word, routed, answered,
    and over when the answer has finished playing. A CONVERSATION (the wake
    word's mode, or a command that handed over to its `fallback`) goes on:
    after each reply the satellite listens again without the wake word for
    the conversation's follow_up_s, and the next thing said is the next turn,
    with the turns so far passed to the destination (dialogue.Memory). It
    ends on silence, on an ending phrase, on an error, on the stop button, or
    when the satellite stops sending audio.

    WHILE A REPLY PLAYS, the satellite can be interrupted. Speech over it
    (listening.BargeIn, which needs the front-end) stops the reply at once:
    the speaker is flushed, the destination and TTS still working on the
    rest are cancelled, and in a conversation what was said becomes the next
    turn, from its first syllable. In a command it only stops the reply. The
    wake word interrupts too: the same one carries the conversation on,
    another ends it and starts its own. Barge-in by voice is armed only when
    the reply plays on the satellite that heard, whose own loopback is what
    the echo canceller subtracts: a reply played in another room is not.

    Created by the listener (or by /inject) and run as its own task, so the
    listener keeps draining audio while the router works. Commands arrive
    through deliver() from the listener; /inject delivers one before
    starting, and an injected conversation is always one turn.

    quiet: send nothing to any satellite. That is /inject without ?play=1,
    which is how the pipeline is verified with nobody hearing or seeing
    anything.
    """

    def __init__(self, h: Hub, nid: str, name: str, heard: listening.Heard, *,
                 session: Session | None = None, quiet: bool = False, injected: bool = False):
        self.hub, self.nid, self.name, self.heard = h, nid, name, heard
        self.s = session
        self.quiet = quiet
        self.injected = injected
        self.commands: asyncio.Queue[listening.Command] = asyncio.Queue()
        self.phase = "listening"
        self.ducked: list[Session] = []
        self.outcome: routing.Outcome | None = None
        self.task: asyncio.Task | None = None
        self._lit_key: int | None | str = "unset"
        self._lit_at = 0.0
        self.route = routing.current().find(nid, name, heard.wake_word)
        self.memory: dialogue.Memory | None = None
        self.turns = 0
        self.started_at = time.monotonic()
        self.reason: str | None = None       # why it ended, for conversation_ended
        self.note: str | None = None
        self.interruptible = False
        self.superseded = False
        self.player: Player | None = None
        self._interrupt: asyncio.Future | None = None
        self._delivered_at = time.monotonic()
        self._silence_ms = self.route.behaviour.silence_ms if self.route else 800
        self._command: listening.Command | None = None
        self._published = False
        self._n = 0  # turns begun, for telemetry
        self._cancelled = False
        self._closing = False

    @property
    def live(self) -> bool:
        return not self.quiet and self.s is not None

    def start(self) -> None:
        self.task = self.hub.spawn(self.run(), name=f"conversation-{self.nid}")

    def deliver(self, command: listening.Command) -> None:
        self._delivered_at = time.monotonic()
        self.commands.put_nowait(command)

    def cancel(self, reason: str = "cancelled") -> None:
        """End it now, for `reason`. Not once it is closing: _close is
        already ending it, and a second cancel (stop pressed twice, stop and
        a flush) landing in one of its sends cut it short, before the
        satellite was released: the duck and the ring stayed on, and the hub
        took the satellite for busy and ignored its wake words until it
        reconnected."""
        self.reason = self.reason or reason
        if self.task is not None and not self._closing:
            self.task.cancel()

    def session_view(self) -> dict | None:
        if self.memory is None:
            return None
        return {"rule_id": self.route.id if self.route else None, "turns": self.turns,
                "seconds": round(time.monotonic() - self.started_at, 1),
                "language": self.memory.language}

    # -- interruptions, from the listener ---------------------------------------

    def takes(self, heard: listening.Heard) -> bool:
        """A wake word heard during this conversation that carries it on: the
        one it is a conversation of, while it may be interrupted. What follows
        the word is the next turn."""
        if self.memory is None or not self.interruptible or self.route is None:
            return False
        found = routing.current().find(self.nid, self.name, heard.wake_word)
        if found is None or found.id != self.route.id:
            return False
        if self.s is not None and self.s.ear is not None:
            self.s.ear.set_silence(self.route.behaviour.silence_ms)
        self._silence_ms = self.route.behaviour.silence_ms
        self._interrupted("wake_word")
        return True

    def barge_in(self, ev: listening.BargeIn) -> None:
        if self.interruptible:
            self._interrupted("voice" if ev.capturing else "voice_stop")

    def _interrupted(self, kind: str) -> None:
        if self._interrupt is not None and not self._interrupt.done():
            self._interrupt.set_result(kind)

    def supersede(self) -> None:
        """Another wake word: this conversation ends now, its reply with it,
        and the new one owns the ring."""
        self.superseded = True
        if self.player is not None and self.player.drop():
            target = self.player.session()
            if target is not None:
                self.hub.spawn(_quietly(target.send_json({"type": "flush"})), name="flush")
        self.cancel("wake_word")

    # -- the satellite's lights, duck and Ear -----------------------------------

    async def point(self, direction: float | None, *, force: bool = False) -> None:
        """The ring, while listening.

        Firmware that draws the `listen` mode animates it on the board: a
        breathing glow, and an arc that glides towards the talker. It is sent
        the direction whenever that moves by half an LED or more.

        Older firmware draws only the frame it is sent. It used to be sent a
        pointer at the talker, which sat still whenever the talker did and
        looked like a stalled frame, so it now gets the breathing pulse, once."""
        s = self.s
        leds = s.caps.get("lights") if s is not None else None
        if not self.live or not isinstance(leds, int) or leds <= 0:
            return
        animates = "listen" in (s.caps.get("light_modes") or ())
        key = (round(direction / 360.0 * leds * 2) % (leds * 2)
               if animates and direction is not None else None)
        now = time.monotonic()
        # The listener calls this after every batch; until run() has lit the
        # ring for the first time (after the wake earcon), it waits its turn.
        if not force and (self._lit_key == "unset" or key == self._lit_key
                          or now - self._lit_at < LIGHTS_MIN_S):
            return
        self._lit_key, self._lit_at = key, now
        if animates:
            msg = {"mode": "listen", "color": ring_colour(self.route and self.route.behaviour),
                   "brightness": 96,
                   "direction": None if direction is None else round(direction % 360.0, 1)}
        else:
            msg = {"mode": "pulse", "color": ring_colour(self.route and self.route.behaviour),
                   "brightness": 48}
        await self.hub.send_lights(s, msg)

    def _reply_satellite(self) -> Session | None:
        """The other satellite this word answers on (its Reply on, or this
        satellite's Output), so it can be ducked while the question is asked:
        the music there goes down, not only here."""
        if self.route is None or self.route.behaviour.action is None:
            return None
        reply_to = self.route.behaviour.action.reply_to
        if reply_to == "none":
            return None
        found = (self.nid,) if reply_to == "same" else lookup_satellite(reply_to)
        if found is None:
            return None
        target = self.hub.output_of(found[0])
        if target == self.nid:
            return None
        other = self.hub.sessions.get(target)
        return other if other is not None and other.adopted else None

    async def _duck(self, s: Session) -> None:
        if s not in self.ducked:
            self.ducked.append(s)
            await self.hub.hold_duck(s)

    async def _unduck(self, s: Session) -> None:
        if s in self.ducked:
            self.ducked.remove(s)
            await self.hub.release_duck(s)

    async def before_first_audio(self, target: Session) -> None:
        """The moment a reply starts: whatever was playing there goes, the
        duck is lifted, the ring (spinning while it thought) goes out, and
        the satellite may be interrupted until the reply is over."""
        if target.flush_speaker():
            await _quietly(target.send_json({"type": "flush"}))
        await self._unduck(target)
        if self.live and self.s.lit:
            await self.hub.send_lights(self.s, {"mode": "off"})
        self.phase = "replying"
        ear = self.s.ear if self.live else None
        if ear is None:
            return
        self.interruptible = ear.interruptible = True
        if target is self.s:
            capture = None
            if self.memory is not None:
                capture = (self.route.behaviour.conversation.silence_ms, CAPTURE_START_S)
            ear.watch(voice=True, capture=capture)

    def _hold_still(self) -> None:
        """Nothing interrupts while a command is routed: the Ear reports no
        wake word and listens for no barge-in."""
        self.interruptible = False
        ear = self.s.ear if self.live else None
        if ear is not None:
            ear.unwatch()

    # -- the conversation ---------------------------------------------------------

    def _begin(self, route: routing.Route, reason: str,
               from_rule: str | None = None) -> dialogue.Memory | None:
        """This becomes a conversation, with a memory of its own."""
        if self.quiet or self.injected or self.s is None or not self.s.adopted:
            return None
        self.route = route
        self.memory = dialogue.Memory()
        self.hub.publish({"type": "conversation_started", "satellite": self.nid,
                          "wake_word": self.heard.wake_word, "rule_id": route.id,
                          "reason": reason, "from_rule": from_rule,
                          "follow_up_s": route.behaviour.conversation.follow_up_s})
        return self.memory

    def _handover(self, target: routing.Route) -> dialogue.Memory | None:
        return self._begin(target, "fallback", from_rule=self.route.id if self.route else None)

    async def _next(self, timeout: float) -> listening.Command | None:
        try:
            return await asyncio.wait_for(self.commands.get(), timeout)
        except TimeoutError:
            return None

    async def run(self) -> dict:
        event: dict = {}
        try:
            self.hub.publish({"type": "wake", "satellite": self.nid,
                              "wake_word": self.heard.wake_word,
                              "score": self.heard.score, "direction": self.heard.direction}
                             | ({"injected": True} if self.injected else {}))
            if self.live:
                await self.hub.earcon(self.s, "wake")
                await self.point(self.heard.direction, force=True)
                await self._duck(self.s)
                other = self._reply_satellite()
                if other is not None:
                    await self._duck(other)
            if self.route is not None and self.route.behaviour.mode == "conversation":
                self._begin(self.route, "wake_word")
            command = await self._next(COMMAND_WAIT_S)
            first = True
            while True:
                out, following = await self._turn(command, first)
                await self._feedback(out)
                event = self._publish(out, command)
                if self.memory is None or self.injected:
                    break
                if out.ended:
                    self.reason = "phrase"
                    if self.live:
                        await self.hub.earcon(self.s, "done")
                    break
                if command is None:
                    self.reason = "no_audio"
                    break
                nothing = out.error is not None and (out.error.startswith("nothing was said")
                                                     or out.error.startswith("stt: nothing"))
                if out.error and (first or not nothing):
                    self.reason = "silence" if nothing else "error"
                    break
                first = False
                if following is None or not following.had_speech:
                    following = await self._follow_up()
                    if following is None:
                        self.reason = "no_audio"
                        break
                    if not following.had_speech:
                        self.reason = "silence"
                        break
                command = following
        except asyncio.CancelledError:
            self._cancelled = True
            if self.outcome is None:
                self.outcome = routing.Outcome(rule_id=self.route.id if self.route else None)
            if not self._published:
                if self.outcome.error is None:
                    self.outcome.error = "cancelled"
                event = self._publish(self.outcome, self._command, note="cancelled")
        finally:
            await self._close()
        return event

    async def _follow_up(self) -> listening.Command | None:
        """Listen for the next turn without the wake word. None when no
        Command came at all (the audio stopped arriving)."""
        b = self.route.behaviour.conversation
        self.phase = "listening"
        ear = self.s.ear if self.live else None
        if ear is None or not self.hub.may_listen(self.s):
            return None
        self._silence_ms = b.silence_ms
        ear.follow_up(b.silence_ms, b.follow_up_s)
        self.interruptible = ear.interruptible = True
        await self.point(None, force=True)
        return await self._next(b.follow_up_s + FOLLOW_UP_SLACK_S)

    async def _turn(self, command: listening.Command | None,
                    first: bool) -> tuple[routing.Outcome, listening.Command | None]:
        """One utterance through to its reply, and the next command if a
        barge-in or the wake word already brought one."""
        out = routing.Outcome(rule_id=self.route.id if self.route else None,
                              mode=self.route.behaviour.mode if self.route else "command")
        self.outcome, self._command, self._published = out, command, False
        self._n += 1
        self.phase = "routing"
        self._hold_still()
        if command is None:
            out.error = "no command: the microphone stopped sending audio"
            return out, None
        if not command.had_speech:
            # Nothing to transcribe, and silence given to Whisper-style models
            # comes back as invented text.
            out.error = "nothing was said after the wake word" if first else "nothing was said"
            return out, None
        if self.live and self.s.lit:
            await self.hub.send_lights(self.s, {"mode": "spin", "brightness": 40,
                                                "color": ring_colour(self.route and self.route.behaviour)})
        speech_end = self._delivered_at - self._silence_ms / 1000
        sink = Player(self, out) if self.live else dialogue.Collect()
        self.player = sink if isinstance(sink, Player) else None
        self._interrupt = asyncio.get_running_loop().create_future()
        work = asyncio.create_task(dialogue.run_turn(
            routing.current(), self.route, satellite_id=self.nid, satellite_name=self.name,
            wake_word=self.heard.wake_word, audio=command.audio, sink=sink, memory=self.memory,
            out=out, speech_end=speech_end,
            on_handover=None if (self.quiet or self.injected) else self._handover))
        following = None
        try:
            await asyncio.wait({work, self._interrupt}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                kind = self._interrupt.result()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                if self.player is not None and self.player.drop():
                    target = self.player.session()
                    if target is not None:
                        await _quietly(target.send_json({"type": "flush"}))
                out.interrupted = True
                out.spoken_text = sink.spoken() or None
                self.note = "interrupted by " + ("the wake word" if kind == "wake_word" else "voice")
                log.info("satellite %s: reply interrupted by %s", self.nid, kind)
                if kind in ("voice", "wake_word") and self.memory is not None:
                    # The Ear is already collecting what is being said.
                    if kind == "voice":
                        self._silence_ms = self.route.behaviour.conversation.silence_ms
                    self.phase = "listening"
                    following = await self._next(CAPTURE_WAIT_S)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            self._hold_still()
            self._interrupt = None
        if self.memory is not None and out.transcript and not out.error and not out.ended:
            self.turns += 1
            self.memory.add(out.transcript, out.spoken_text or out.reply_text or "")
        if self.live:
            self.hub.record_latency(self.nid, out.timeline_ms)
        return out, following

    async def _feedback(self, out: routing.Outcome) -> None:
        """The earcon a turn ends with: `error` when it failed, `done` when it
        succeeded with nothing to say, or answered on another satellite. A
        reply heard here says it is done by itself."""
        if not self.live or out.interrupted or out.ended:
            return
        if out.error:
            await self.hub.earcon(self.s, "error")
            return
        target = self.player.session() if self.player is not None else None
        if not out.audio_bytes:
            if self.note is None:  # not a speaker that is off, or a satellite gone
                await self.hub.earcon(self.s, "done")
        elif target is not None and target is not self.s:
            await self.hub.earcon(self.s, "done")

    def _publish(self, o: routing.Outcome, command: listening.Command | None,
                 note: str | None = None) -> dict:
        played = bool(self.player is not None and self.player.clips)
        if self.quiet:
            note = note or ("not played: ?play=1 was not given" if self.injected else None)
        common = {"satellite": self.nid, "wake_word": self.heard.wake_word,
                  "rule_id": o.rule_id, "mode": o.mode, "reply_to": o.reply_to,
                  "error": o.error, "transcript": o.transcript, "language": o.language,
                  "language_source": o.language_source, "reply_language": o.reply_language,
                  "voice": o.voice, "reply_text": o.reply_text, "spoken_text": o.spoken_text,
                  "interrupted": o.interrupted, "timings_ms": o.timings_ms,
                  "timeline_ms": o.timeline_ms,
                  "endpoint": command.reason if command else None,
                  "command_s": round(command.seconds, 2) if command else None,
                  "played": played, "note": note or self.note}
        if self.memory is not None and not self.injected:
            event = {"type": "turn", "turn": self.turns, "ended": o.ended,
                     "handed_over_to": o.handed_over_to} | common
        else:
            event = {"type": "routed"} | common | ({"injected": True} if self.injected else {})
        self._record(o, command, played, note or self.note)
        self.note = None
        self._published = True
        self.hub.publish(event)
        return event

    def _record(self, o: routing.Outcome, command: listening.Command | None, played: bool,
                note: str | None) -> None:
        """The turn's telemetry record (telemetry.py), when telemetry is on."""
        rec = self.hub.telemetry
        if rec is None or not rec.enabled:
            return
        b = self.route.behaviour if self.route else None
        d = b.action.destination if b is not None and b.action is not None else None
        s = self.s
        word = self.heard.wake_word
        trigger = ("inject" if self.injected else "follow_up" if self._n > 1
                   else "ptt" if self.heard.score is None else "wake")
        # Ear.stats never raises: a front-end caught mid-reset reports nothing.
        ear = ({k: v for k, v in s.ear.stats().items() if k in ("erle_db", "far_end", "beamformer", "rtf")}
               if s is not None and s.ear is not None else {})
        rec.write({
            "kind": "turn", "satellite": self.nid, "satellite_name": self.name,
            "firmware": s.fw if s is not None else None, "word": word, "rule_id": o.rule_id,
            "mode": o.mode, "trigger": trigger, "turn": self._n,
            "action": getattr(d, "type", None), "destination": _destination_view(d),
            "wake": ({"score": self.heard.score, "direction": self.heard.direction,
                      "threshold": self.hub.voice.assignment.thresholds().get(word)}
                     if trigger == "wake" else None),
            "command": _command_view(command, self._silence_ms),
            "language": o.language, "language_source": o.language_source,
            "reply_language": o.reply_language, "voice": o.voice,
            "transcript": o.transcript, "reply_text": o.reply_text, "spoken_text": o.spoken_text,
            "error": o.error, "handed_over_to": o.handed_over_to, "interrupted": o.interrupted,
            "ended": o.ended, "played": played, "note": note, "reply_to": o.reply_to,
            "reply_audio_s": round(o.audio_bytes / 2 / routing.SPEAKER_RATE, 2) if o.audio_bytes else None,
            "timings_ms": o.timings_ms, "timeline_ms": o.timeline_ms, "events": o.events,
            "device": ({k: s.status[k] for k in TELEMETRY_STATUS if k in s.status}
                       if s is not None else None) or None,
            "ear": ear or None, "quiet": self.quiet or None})

    async def _close(self) -> None:
        """Whatever the way out: a reply cut short stops, the duck is
        lifted, the ring goes out, the Ear goes back to wake words (the
        listener sees no conversation), and a conversation says it ended.

        cancel() leaves it alone from here on; the bookkeeping is in a
        finally all the same, so a task cancelled some other way (the hub
        shutting down) still lets the satellite go."""
        self._closing = True
        self.interruptible = False
        try:
            if self._cancelled and self.player is not None and self.player.drop():
                target = self.player.session()
                if target is not None:
                    await _quietly(target.send_json({"type": "flush"}))
            for s in list(self.ducked):
                await self._unduck(s)
            if self.live and self.s.lit and not self.superseded:
                await self.hub.send_lights(self.s, {"mode": "off"})
        finally:
            if self.s is not None and self.s.conversation is self:
                self.s.conversation = None
            if self.memory is not None:
                self.hub.publish({"type": "conversation_ended", "satellite": self.nid,
                                  "rule_id": self.route.id if self.route else None,
                                  "turns": self.turns, "reason": self.reason or "cancelled",
                                  "seconds": round(time.monotonic() - self.started_at, 1)})
            self.phase = "done"


# ---- AirPlay artwork -----------------------------------------------------------------

ARTWORK_MAX = 2 * 1024 * 1024
ARTWORK_TYPES = {"jpeg": "image/jpeg", "png": "image/png"}


def _artwork(msg: dict) -> dict | None:
    """The cover an AirPlay receiver sent, checked: a JPEG or PNG of at most
    ARTWORK_MAX whose SHA-256 is the one it names. Anything else is dropped."""
    fmt, digest = msg.get("format"), str(msg.get("sha256") or "")
    if fmt not in ARTWORK_TYPES:
        return None
    try:
        data = base64.b64decode(str(msg.get("data") or ""), validate=True)
    except (ValueError, binascii.Error):
        return None
    if not data or len(data) > ARTWORK_MAX or hashlib.sha256(data).hexdigest() != digest:
        return None
    return {"sha256": digest, "type": ARTWORK_TYPES[fmt], "data": data}


# ---- jacks ------------------------------------------------------------------------


def _jacks(status: dict) -> dict:
    audio = status.get("audio") if isinstance(status.get("audio"), dict) else {}
    out = {}
    for direction, key in (("output", "sinks"), ("input", "sources")):
        for d in audio.get(key) or []:
            if isinstance(d, dict) and d.get("name") and d.get("jack") in ("plugged", "unplugged"):
                out[d["name"]] = (direction, d.get("description") or d["name"], d["jack"])
    return out


def jack_changes(before: dict, after: dict) -> list[dict]:
    """A plug that went in or out on a satellite whose card can tell
    (a Linux satellite's `audio`, calliope_pi pipewire.jacks), as events:
    {device, name, direction, plugged}. A device that came or went with its
    card is not a plug; nor is the first status after connecting."""
    if not before:
        return []
    old, new = _jacks(before), _jacks(after)
    return [{"device": desc, "name": name, "direction": direction, "plugged": state == "plugged"}
            for name, (direction, desc, state) in new.items()
            if name in old and old[name][2] != state]


# ---- telemetry ----------------------------------------------------------------------

# A satellite's status fields that go into a turn's telemetry record.
TELEMETRY_STATUS = ("rssi", "heap", "psram", "uptime_s", "mic_dropped", "spk_dropped",
                    "spk_buffered_ms", "volume", "mic_gain_db")


def _destination_view(d) -> dict | None:
    """What a turn's destination is, without its address or key names."""
    if d is None:
        return None
    view = {"type": d.type}
    for key in ("model", "pipeline", "agent_id", "tools", "stream", "timeout", "max_tokens"):
        value = getattr(d, key, None)
        if value not in (None, [], ""):
            view[key] = value
    return view


def _command_view(command: listening.Command | None, silence_ms: int | None) -> dict | None:
    """How long the command was, why the endpointer ended it, and how loud it
    was (dBFS): a quiet command is a far or turned-away talker, a clipped
    one a gain set too high."""
    if command is None:
        return None
    view = {"seconds": round(command.seconds, 2), "endpoint": command.reason,
            "had_speech": command.had_speech, "silence_ms": silence_ms}
    pcm = command.audio[:len(command.audio) & ~1]
    if pcm:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x)))
        view |= {"rms_dbfs": round(20 * math.log10(rms / 32768), 1) if rms > 0 else -120.0,
                 "peak_dbfs": round(20 * math.log10(peak / 32768), 1) if peak > 0 else -120.0,
                 "clipped_pct": round(float(np.mean(np.abs(x) >= 32000)) * 100, 3)}
    return view


# ---- the rename from "nodes" ------------------------------------------------------

LEGACY_PREFIX, PREFIX = "NODES_", "SATELLITES_"


def legacy_settings(environ: dict[str, str], rules: list) -> list[str]:
    """A sentence for each setting that the rename (2026-09-25) left behind.

    Every NODES_* variable became SATELLITES_*, and the hub reads only the new
    names. A secret set in the app's settings under the old name is ignored
    without a word otherwise: MQTT simply switches off. So each one still set
    is named at start, with the name read now.

    Except the ones an action names (`rules`: anything with an id and a
    destination: a wake word's action, or a rule). rules.json stored every
    field, and a wake word migrated from it carries them on, so an action
    saved before the rename says "token_env": "NODES_HA_TOKEN" and reads
    exactly that; calling it unread would send the operator off to rename the
    one variable that works. What is worth saying about those is the opposite
    case: an action naming a NODES_* variable that is not set, which is what
    renaming the secret and not the action looks like."""
    named: dict[str, list[str]] = {}
    for rule in rules:
        for var in rule.destination.env_vars():
            named.setdefault(var, []).append(rule.id)
    out = []
    for var in sorted(v for v in environ if v.startswith(LEGACY_PREFIX)):
        if var in named:
            continue
        new = PREFIX + var.removeprefix(LEGACY_PREFIX)
        out.append(f"{var} is set but is not read since the feature was renamed: the hub reads "
                   f"{new}" + ("" if environ.get(new) else ", which is not set"))
    for var, ids in sorted(named.items()):
        if var.startswith(LEGACY_PREFIX) and not environ.get(var):
            new = PREFIX + var.removeprefix(LEGACY_PREFIX)
            out.append(f"the action of {', '.join(repr(i) for i in ids)} reads {var}, which is not "
                       f"set: a secret renamed to {new} has to be renamed in the action as well")
    return out


def _named_actions() -> list:
    """Each wake word's action (and push-to-talk's), as legacy_settings reads them."""
    from types import SimpleNamespace
    a = hub.voice.assignment
    return [SimpleNamespace(id=name, destination=b.action.destination)
            for name, b in [(w.name, w.behaviour) for w in a.words] + [("ptt", a.ptt)]
            if b is not None and b.action is not None]


def lookup_satellite_safe(ref: str) -> tuple[str, str] | None:
    """lookup_satellite for a Voice that may exist before the hub does."""
    try:
        return lookup_satellite(ref)
    except NameError:
        return None


def lookup_satellite(ref: str) -> tuple[str, str] | None:
    """router.py's view of the satellites: one adopted satellite by id or
    name, or None."""
    if ref == "all":
        return None
    found = [n for n in hub.find(ref) if n in hub.store.satellites]
    if len(found) != 1:
        return None
    return found[0], hub.store.satellites[found[0]].name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global hub, FIRMWARE_KEY, EXECUTOR
    # A bad SATELLITES_FIRMWARE_PUBKEY stops the service here, not at the first
    # upload an hour later.
    FIRMWARE_KEY = signing.load_public_key()
    EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ear")
    hub = Hub(Store(DATA_DIR))
    hub.telemetry = telemetry.Recorder(DATA_DIR, os.environ.get("SATELLITES_TELEMETRY"))
    telemetry.install(hub.telemetry)
    log.info("telemetry %s", f"on ({hub.telemetry.level})" if hub.telemetry.enabled else "off")
    # The page learns that a word finished downloading (or failed) from the
    # event stream, rather than by polling GET /satellites/wake-words.
    hub.voice = Voice(MODEL_DIR, FRONTEND, wakewords_config.Assignment.open(DATA_DIR, WAKE_WORDS),
                      on_change=lambda: hub.publish({"type": "wake_words",
                                                     "words": hub.voice.views()}))
    hub.http = httpx.AsyncClient(follow_redirects=False, timeout=10)
    # Routing is by each wake word's own entry (wakewords_config.WordActions);
    # rules.json was read once, above, by Assignment.open's migration.
    routing.configure(routing.Router(wakewords_config.WordActions(hub.voice.assignment),
                                     output_of=hub.output_of,
                                     lookup=lookup_satellite))
    # STT's engines learnt now, so the first double-check waits for nothing.
    routing.current().warm()
    for problem in legacy_settings(dict(os.environ), _named_actions()) + lang.household_problems():
        log.warning("%s", problem)
    log.info("household languages: %s", ", ".join(lang.household()))
    # py3langid takes 0.4 s to load; paid now, off the event loop, rather than
    # by the first utterance.
    hub.spawn(asyncio.to_thread(lang.detector.load), name="language")
    # What the hub held itself (secrets.json, the environment, button URLs,
    # the broker's password) goes into the secret store, and is used from
    # there; until the gateway answers, from here (secret_import.py). Made
    # before the MQTT bridge asks for the broker's password.
    importing = secret_import.Import(
        data_dir=DATA_DIR, targets=wakewords_config.WordActions(hub.voice.assignment).targets(),
        satellites={nid: rec.config for nid, rec in hub.store.satellites.items()},
        on_buttons=hub.imported_buttons)
    hub.spawn(importing.run(), name="secret-import")
    hub.importing = importing
    hub.bridge = MqttBridge.from_env(password=mqtt_password)
    await hub.bridge.start(hub, on_command=mqtt_command)
    hub.spawn(hub.voice.reconcile(), name="wake-words")
    log.info("%d adopted satellites, %d firmware images in %s",
             len(hub.store.satellites), len(hub.store.firmware), DATA_DIR)
    try:
        yield
    finally:
        for s in list(hub.sessions.values()):
            hub.stop_listening(s)
        for t in list(hub.tasks):
            t.cancel()
        await asyncio.gather(*hub.tasks, return_exceptions=True)
        await hub.bridge.stop()
        await routing.current().aclose()
        await hub.http.aclose()
        await secret_client.current().aclose()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _health() -> dict:
    """The hub's part of /health, once the lifespan has built the hub."""
    h = globals().get("hub")
    if h is None:
        return {}
    return {
        "satellites": {"online": len(h.sessions), "adopted": len(h.store.satellites),
                       "pending": sum(1 for s in h.sessions.values() if not s.adopted)},
        "tts": TTS_URL or None,
        "voice": h.voice.health(),
        "routing": {"rules": sum(1 for w in h.voice.assignment.words if w.behaviour),
                    "stt": routing.current().stt_url or None,
                    "stt_engine": routing.current().stt_engine,
                    "load_error": routing.current().rules.load_error},
        "mqtt": h.bridge.health() if h.bridge else None,
        # WHICH KEY FIRMWARE MUST BE SIGNED WITH, for Admin > Secrets: the
        # public key's short id, or None with no SATELLITES_FIRMWARE_PUBKEY.
        # A public name, never key material; without it the page said "unknown".
        "firmware_key_id": signing.key_id(FIRMWARE_KEY) if FIRMWARE_KEY is not None else None,
        # Credentials the secret store now holds, still set in this
        # environment: Admin asks for each to be removed (secret_import).
        "ignored_variables": (h.importing.leftover_variables()
                              if getattr(h, "importing", None) else []),
    }


app = FastAPI(title="voice-satellites", lifespan=lifespan)
# Its 422 on a native route says what was wrong and never repeats what was
# sent: a token pasted into token_env stays out of the answer.
errors.install_errors(app)
health.install_health(app, details=_health)
identity.install(app, "satellites", credentials=gateway.CREDENTIALS)
# Inside identity's guard, which is always outermost and has put the claims
# on the scope by then.
app.add_middleware(RelayOnlyOnTheSocket)
# Before every /satellites/{nid} route below: FastAPI matches in registration
# order, and GET /satellites/{nid} would otherwise take "routing" for a
# satellite id.
app.include_router(routing.routes)


# ---- the device socket -------------------------------------------------------

# TWO PATHS, ONE HANDLER. The feature was called "nodes" in pre-release builds
# until 2026-09-25, and a board flashed from one connects to /nodes/ws. Its next
# firmware arrives over that same socket, so a hub that stopped answering the
# old path would strand the board on the old image with USB as the only way
# back. Nothing about a connection depends on the path it came in on. The
# alias can go once no board reports firmware from before the rename.
SOCKET = "/satellites/ws"
LEGACY_SOCKET = "/nodes/ws"


@app.websocket(SOCKET)
@app.websocket(LEGACY_SOCKET)
async def satellite_socket(ws: WebSocket) -> None:
    # The gateway's relay and nothing else (D53): identity.install has
    # verified the assertion, and this says whose it must be. Closed before
    # the upgrade is accepted, so the socket never opens.
    if identity.claims_of(ws).sub != RELAY:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=1008)
        return
    if (not isinstance(first, dict) or first.get("type") != "hello"
            or not isinstance(first.get("id"), str) or not satellite_id(first["id"])):
        await ws.close(code=1008)
        return

    s = Session(ws, first)
    if not hub.proves_adoption(s) and not hub.hellos.allow(s.address):
        log.debug("satellite %s: a hello without its token from %s, over %d a minute; refused",
                  s.id, s.address, HELLOS_PER_MINUTE)
        await ws.close(code=CLOSE_TRY_LATER)
        return
    old = hub.sessions.get(s.id)
    if old is not None and old.adopted and not hub.proves_adoption(s):
        # A satellite's id is its MAC, which is no secret. Only a connection
        # holding the adoption token may take an adopted satellite's place:
        # otherwise anyone could keep it offline and have their own status
        # shown, and sent to Home Assistant, as its. A board that lost its
        # token while its old socket is still open waits the half-minute or so
        # it takes the hub to notice that socket is dead, then comes in as
        # pending.
        log.warning("satellite %s: a connection from %s without its token tried to take its "
                    "place; refused", s.id, s.address)
        await ws.close(code=1008)
        return
    if old is not None:  # the device reconnected before the old socket timed out
        await old.ws.close(code=1012)
    hub.sessions[s.id] = s
    player = asyncio.create_task(s.speaker_loop(lambda: hub.speaker_allowed(s)))
    log.info("satellite %s connected from %s (%s, firmware %s)", s.id, s.address, s.model, s.fw)
    boot = s.hello.get("boot") or {}
    connected_at, close_code = time.monotonic(), None
    hub.record({"kind": "session", "satellite": s.id, "event": "connected", "firmware": s.fw,
                "model": s.model, "reset_reason": s.hello.get("reset_reason"),
                "boot_ms": boot.get("stages_ms"), "stalled_in": boot.get("stalled_in"),
                "replaced": old is not None or None})
    if boot:
        # The satellite's own account of its start-up: how long each step took,
        # and whether a previous start-up stalled (see clients/korvo-satellite
        # src/boot.h for why this exists).
        log.info("satellite %s boot: reset reason %s, stages %s%s", s.id, s.hello.get("reset_reason"),
                 boot.get("stages_ms"), f", STALLED in {boot['stalled_in']} "
                 f"({boot.get('stall_restarts')} restarts)" if boot.get("stalled_in") else "")
    try:
        await hub.greet(s)
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                close_code = msg.get("code")
                break
            if msg.get("bytes") is not None:
                data = msg["bytes"]
                if s.adopted and data and data[0] == FRAME_MIC:
                    pcm = data[HEADER:]
                    for q in list(s.taps):
                        q.put_nowait(pcm)
                    if s.listener is not None:
                        s.offer_mic(pcm)
                    # Music playing is not the silence the loopback's idle
                    # level is learnt from.
                    if s.sense is not None and s.sense.feed(pcm, time.monotonic(),
                                                            s.playing or s.media is not None):
                        hub.on_output(s)
            elif msg.get("text") is not None:
                await on_message(s, json.loads(msg["text"]))
    except WebSocketDisconnect as e:
        close_code = e.code
    except RuntimeError:
        pass
    finally:
        hub.record({"kind": "session", "satellite": s.id, "event": "disconnected",
                    "close_code": close_code,
                    "seconds": round(time.monotonic() - connected_at),
                    "superseded": hub.sessions.get(s.id) is not s or None})
        player.cancel()
        hub.stop_listening(s)
        # Nothing that waits on this satellite waits for ever: the upload
        # playing here ends "disconnected", an AirPlay command is answered
        # offline, and an announcement still queued is dropped.
        if s.media is not None:
            s.media.stop("disconnected")
            s.media = None
        for reply in s.replies.values():
            if not reply.done():
                reply.set_result({"ok": False, "error": "disconnected"})
        s.replies.clear()
        s.flush_speaker()
        if s.ota and s.ota.get("state") in ("requested", "started", "progress"):
            s.ota.update(state="failed", error="disconnected mid-transfer")
            hub.publish({"type": "ota", "satellite": s.id, "state": "failed",
                         "error": "disconnected mid-transfer"})
            log.warning("satellite %s disconnected during an update", s.id)
        if hub.sessions.get(s.id) is s:
            del hub.sessions[s.id]
        if hub.speaks_for(s) and not s.evicted:
            hub.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time(),
                              "address": s.address}
        hub.publish({"type": "offline", "satellite": s.id})
        log.info("satellite %s disconnected", s.id)


EARCON_MESSAGES = frozenset({"earcons", "earcon_next", "earcon_stored", "earcon_failed"})


async def on_message(s: Session, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "hello":  # sent again after adoption, with the new token
        s.update(msg)
        if not hub.proves_adoption(s) and not hub.hellos.allow(s.address):
            await s.close(CLOSE_TRY_LATER)
            return
        await hub.greet(s)
    elif kind == "status":
        was_muted = bool(s.status.get("muted"))
        before = s.status
        s.status = {k: v for k, v in msg.items() if k != "type"}
        if s.adopted:
            for change in jack_changes(before, s.status):
                hub.publish({"type": "jack", "satellite": s.id} | change)
        if s.adopted:
            # After a welcome that left out settings the hub did not know,
            # the firmware applies the rest and reports at once: this is where
            # the record gets them.
            hub.take_report(s, s.status)
            # "local": the satellite changed it itself, as a button does (a
            # phone's AirPlay slider moving the Pi's output volume).
            if msg.get("cause") in ("button", "local") or time.monotonic() <= s.volume_press_until:
                s.volume_press_until = 0.0
                hub.take_own(s)
        # The cover goes with the music: a status that no longer names the
        # picture held (the session ended, the track has another) drops it,
        # so neither the page nor Home Assistant shows the cover of what has
        # stopped. The satellite sends a picture before the status that names
        # it. A satellite that is no AirPlay receiver says nothing of it.
        air = s.status.get("airplay")
        if isinstance(air, dict) and s.artwork is not None:
            art = air.get("artwork")
            if not isinstance(art, dict) or art.get("sha256") != s.artwork["sha256"]:
                s.artwork = None
        if hub.speaks_for(s):
            hub.publish({"type": "status", "satellite": s.id, "status": s.status})
            if hub.telemetry is not None:
                hub.telemetry.device(s.id, s.status)
        # The privacy mute stops everything: nothing more will arrive to be
        # heard, and a conversation that is waiting for it, or answering it,
        # ends now rather than when its follow-up times out. Going on, it
        # clears the rest of what the satellite was doing too (Hub.on_mute).
        if s.status.get("muted") and s.adopted and not was_muted:
            await hub.on_mute(s)
        elif s.status.get("muted") and s.conversation is not None:
            s.conversation.cancel("muted")
    elif kind == "button" and s.adopted:
        hub.publish({"type": "button", "satellite": s.id, "button": msg.get("button"),
                     "action": msg.get("action"), "held_ms": msg.get("held_ms")})
        if isinstance(msg.get("button"), str) and msg.get("action") in ("press", "release"):
            await hub.on_button(s, msg["button"], msg["action"], msg.get("held_ms"))
    elif kind == "artwork" and s.adopted:
        s.artwork = _artwork(msg) or s.artwork
    elif kind == "airplay_result" and s.adopted:
        reply = s.replies.pop(str(msg.get("id")), None)
        if reply is not None and not reply.done():
            reply.set_result(msg)
    elif kind in EARCON_MESSAGES and s.adopted and s.earcons is not None:
        await hub.on_earcons(s, msg)
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
            s.ota["finished_at"] = time.time()
        if hub.speaks_for(s):
            hub.publish({"type": "ota", "satellite": s.id, "state": state, "pct": msg.get("pct"),
                         "version": msg.get("version"), "error": msg.get("error")})
        log.info("satellite %s ota %s %s", s.id, state, msg.get("error") or "")


# ---- satellites --------------------------------------------------------------


class AdoptBody(BaseModel):
    name: str = Field(default="", max_length=64)


# A button as the satellite names it, and what the hub does when it is pressed
# or released. A webhook is webhook:secret:<NAME>, a secret_url secret in the
# store (D62): its URL is a credential, and the mapping is answered by GET
# /satellites. Any other webhook: action passes the pattern only so that
# configure() can refuse it with the code the page acts on (use_secret).
ButtonName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]
ButtonAction = Annotated[str, StringConstraints(
    pattern=r"^(ptt|stop|none|mute|volume_up|volume_down|lights|dimmer|brighter|webhook:\S+)$",
    max_length=500)]


class ConfigBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    volume: int | None = Field(default=None, ge=0, le=100)
    mic_gain_db: float | None = Field(default=None, ge=0, le=37.5)
    mic_enabled: bool | None = None
    speaker_enabled: bool | None = None
    local_volume_buttons: bool | None = None
    lights_enabled: bool | None = None
    brightness: int | None = Field(default=None, ge=1, le=100)
    ring_top: int | None = Field(default=None, ge=0, le=11)
    ring_upside_down: bool | None = None
    # A satellite with caps "audio_devices": a PipeWire node name from its
    # `audio` list, or "" for PipeWire's own default.
    audio_sink: str | None = Field(default=None, max_length=256, pattern=r"^[\w.:@+-]*$")
    audio_source: str | None = Field(default=None, max_length=256, pattern=r"^[\w.:@+-]*$")
    echo_reference: bool | None = None
    # Another adopted satellite that plays what this one plays, or "" for
    # its own speaker (Hub.output_of).
    output_satellite: str | None = Field(default=None, max_length=64, pattern=r"^[0-9a-f]*$")
    # A satellite with caps "airplay": whether it is an AirPlay receiver, and
    # the name phones list it as ("" for the satellite's own name).
    airplay_enabled: bool | None = None
    airplay_name: str | None = Field(default=None, max_length=64, pattern=r"^[^\x00-\x1f\x7f]*$")
    # Replaces the whole mapping; the default is in store.py.
    buttons: dict[ButtonName, dict[Literal["press", "release"], ButtonAction]] | None = Field(
        default=None, max_length=16)

    @field_validator("buttons")
    @classmethod
    def _keeps_a_mute(cls, v: dict | None) -> dict | None:
        # Only a button can undo the privacy mute, so a mapping without one
        # would leave a muted satellite muted for good. A mute on key1 alone
        # does not count (store.MUTE_DOES_NOT_COUNT): a stock board does not
        # wire it, and the firmware would quietly make Rec the mute while the
        # page showed Rec doing something else.
        if v is not None and not keeps_a_mute(v):
            raise ValueError("one button other than key1 must stay the privacy mute (mute), "
                             "or a muted satellite could never be unmuted")
        return v


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
    satellite: str
    sha256: str = Field(pattern="^[0-9a-f]{64}$")


class WakeWordBody(BaseModel):
    # Only the shape here; what an entry may say is wakewords_config.check's
    # (and router.Behaviour's), which a loaded file goes through as well.
    # Fields GET adds ("state", "error") are ignored rather than refused, so
    # the page can send back what it was given. A field left out keeps what
    # was saved for that name (wakewords_config: a save merges), which is why
    # none has a default here: the route reads only the fields that were sent.
    name: str = Field(max_length=64)
    threshold: float | None = None
    satellites: list[Annotated[str, StringConstraints(max_length=64)]] | None = Field(
        default=None, max_length=256)
    mode: str | None = None
    language: str | None = None
    action: dict | None = None
    silence_ms: int | None = None
    colour: str | None = None
    conversation: dict | None = None
    trigger: dict | None = None
    verify: dict | None = None


class WakeWordsBody(BaseModel):
    words: list[WakeWordBody] = Field(max_length=wakewords_config.MAX_WORDS)
    # Push-to-talk's behaviour. Left out, it stays as saved.
    ptt: dict | None = None


def _mqtt_satellite(nid: str) -> None:
    # describe() without satellites:admin: MQTT never carries `buttons` (D62).
    if hub.bridge is not None:
        hub.bridge.publish_satellite(hub.describe(nid))


async def mqtt_password() -> str | None:
    """The broker's password: the secret SATELLITES_MQTT_PASSWORD, sent only
    to the broker its allowed hosts name (D41, D46)."""
    broker = secret_client.origin(os.environ.get("SATELLITES_MQTT_URL")) or ""
    try:
        return await secret_client.current().value_for(secret_import.MQTT_PASSWORD, broker)
    except secret_client.HostNotAllowed as e:
        log.warning("MQTT: %s; connecting without a password", e)
        return None


async def mqtt_command(nid: str, change: dict) -> None:
    """A Home Assistant switch or slider: the same path as PATCH, so it is
    validated, saved and sent to the satellite in exactly one place. Only
    the controls (mqtt.parse_command), so not as satellites:admin."""
    await update_satellite(nid, ConfigBody(**change), admin=False)


@app.get("/satellites")
async def list_satellites(request: Request) -> dict:
    ids = set(hub.store.satellites) | set(hub.sessions) | set(hub.seen)
    admin = is_admin(request)
    return {"satellites": sorted((hub.describe(n, admin=admin) for n in ids),
                                 key=lambda d: (not d["adopted"], d["name"], d["id"]))}


# What was said in the house, and what was said back. The same speech in Jobs
# is system-owned and needs jobs:read:all (D32), so a satellites:read key made
# for firmware uploads or monitoring must not be a live feed of it.
# `error` goes too, because an LLM's or a webhook's error can quote the command.
SPOKEN = ("transcript", "reply_text", "spoken_text", "error", "heard")
SPOKEN_IN = ("turn", "routed", "wake_rejected")


def for_subscriber(event: dict, *, admin: bool, hears: bool) -> dict:
    """An event as one subscriber of /satellites/events sees it. The wake
    words' actions go only to satellites:admin, as GET /satellites/wake-words
    gives them (§3.5); what a turn heard and said goes only to a subscriber
    that `hears` (satellites:control or satellites:admin, which Home Assistant
    holds). A `config` event names what changed, never a value."""
    if not admin and event.get("type") == "wake_words":
        return event | {"words": [redacted_word(w) for w in event.get("words") or ()]}
    if not hears and event.get("type") in SPOKEN_IN:
        return {k: v for k, v in event.items() if k not in SPOKEN}
    return event


@app.get("/satellites/events")
async def events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    hub.listeners.add(q)
    admin = is_admin(request)
    hears = admin or identity.has(identity.claims_of(request), "satellites:control")

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield b": connected\n\n"
            while not await request.is_disconnected():
                try:
                    ev = for_subscriber(await asyncio.wait_for(q.get(), timeout=15),
                                        admin=admin, hears=hears)
                    yield f"data: {json.dumps(ev)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            hub.listeners.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# Above GET /satellites/{nid}, which would otherwise take "wake-words" for a
# satellite id and answer 404.
@app.get("/satellites/wake-words")
async def get_wake_words(request: Request) -> dict:
    return await hub.voice.describe(admin=is_admin(request))


@app.put("/satellites/wake-words")
async def put_wake_words(body: WakeWordsBody, request: Request) -> dict:
    """Replace every wake word: its model, threshold, satellites and what it
    does. Live at once: a threshold reaches every detector, a word taken off
    a satellite is no longer heard there, a word's action applies to the
    next time it is heard, and a word named for the first time is fetched in
    the background and listened for when it is ready."""
    known = (set(hub.store.satellites) | set(hub.sessions) | set(hub.seen)
             | hub.voice.assignment.satellite_ids())
    try:
        words, ptt = wakewords_config.check(
            [w.model_dump(exclude_unset=True) for w in body.words],
            available=wakewords_config.available(hub.voice.model_dir), known=known,
            saved=hub.voice.assignment, ptt=body.ptt)
    except ValueError as e:
        raise ApiError(422, str(e), code="invalid_wake_words") from None
    await refuse_non_addresses(
        (t.name for b in [w.behaviour for w in words] + [ptt] if b is not None and b.action
         for t in b.action.destination.targets() if t.holds_url), "words")
    try:
        hub.voice.replace(words, ptt)
    except OSError as e:
        raise ApiError(500, f"could not write {wakewords_config.FILE}: {e}",
                       type_="server_error") from None
    log.info("wake words saved: %s", ", ".join(
        f"{w.name} ({w.mode or 'no action'}) at {w.threshold:g} on "
        f"{'every satellite' if w.satellites == ['*'] else w.satellites}"
        for w in words) or "none")
    hub.spawn(hub.voice.reconcile(), name="wake-words")
    return await hub.voice.describe(admin=is_admin(request))


class TelemetryBody(BaseModel):
    """Telemetry's settings (telemetry.py); a field left out keeps its value."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    level: Literal["timings", "full"] | None = None
    retention_days: int | None = None
    max_mb: int | None = None


def _telemetry() -> telemetry.Recorder:
    if hub.telemetry is None:
        raise ApiError(503, "telemetry is not set up on this hub", code="telemetry_unavailable")
    return hub.telemetry


def _when(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(422, f"{name} must be an ISO 8601 time, like 2026-09-29T14:00:00Z",
                       code="invalid_time", param=name) from None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


@app.get("/satellites/telemetry")
async def get_telemetry() -> dict:
    """Whether telemetry is on, at which level, how long it keeps what it
    records, and the day files it holds."""
    return await asyncio.to_thread(_telemetry().status)


@app.put("/satellites/telemetry")
async def put_telemetry(body: TelemetryBody) -> dict:
    """Turn telemetry on or off, or change its level, retention or size cap.
    Saved on the hub and in force at once, for every satellite."""
    rec = _telemetry()
    try:
        rec.update(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise ApiError(422, str(e), code="invalid_telemetry") from None
    except OSError as e:
        raise ApiError(500, f"could not write telemetry.json: {e.strerror or type(e).__name__}",
                       type_="server_error") from None
    return await asyncio.to_thread(rec.status)


@app.delete("/satellites/telemetry")
async def delete_telemetry() -> dict:
    """Delete every telemetry record, and every clip kept with them. The
    settings stay as they are."""
    rec = _telemetry()
    n = await asyncio.to_thread(rec.wipe)
    log.info("telemetry: %d files deleted", n)
    return await asyncio.to_thread(rec.status) | {"deleted": n}


@app.get("/satellites/telemetry/clips/{name}")
async def telemetry_clip(name: str) -> Response:
    """The audio of a wake word the double-check did not hear, as its wake
    record's verify.clip names it: the hard negatives a wake word model is
    retrained on. Kept at telemetry level full only (telemetry.py). A name
    that is not a clip's (telemetry.CLIP) is a 404 like a clip that is gone,
    and never reaches the file system."""
    wav = await asyncio.to_thread(_telemetry().clip, name)
    if wav is None:
        raise ApiError(404, f"no clip {name}", code="clip_not_found")
    return Response(wav, media_type="audio/wav")


@app.get("/satellites/telemetry/records")
async def telemetry_records(since: str | None = None, until: str | None = None,
                            hours: float | None = Query(None, gt=0, le=24 * 366),
                            kind: str | None = None, satellite: str | None = None,
                            word: str | None = None,
                            limit: int = Query(500, ge=1, le=20000)) -> dict:
    """The newest `limit` records that match, oldest first: from `since`
    (or the last `hours`) to `until`, of the kinds named (turn, wake,
    near_miss, session, device; comma-separated), for one satellite or one
    wake word."""
    start = _when(since, "since")
    if start is None and hours is not None:
        start = datetime.now(UTC) - timedelta(hours=hours)
    kinds = {k.strip() for k in kind.split(",") if k.strip()} if kind else None
    records = await asyncio.to_thread(_telemetry().read, since=start, until=_when(until, "until"),
                                      kinds=kinds, satellite=satellite, word=word, limit=limit)
    return {"count": len(records), "records": records}


@app.get("/satellites/telemetry/summary")
async def telemetry_summary(hours: float = Query(24, gt=0, le=24 * 366),
                            satellite: str | None = None) -> dict:
    """What the last `hours` of records say (telemetry.summarise): per wake
    word, how often it worked and how long each stage took; each engine,
    model and tool; and the wake words and satellites between turns."""
    rec = _telemetry()
    records = await asyncio.to_thread(rec.read, since=datetime.now(UTC) - timedelta(hours=hours),
                                      satellite=satellite, limit=500_000)
    return await asyncio.to_thread(telemetry.summarise, records, hours) | {"telemetry": rec.settings()}


@app.post("/satellites/wake-words/models")
async def upload_wake_word_model(request: Request,
                                 name: str = Query(..., max_length=64)) -> dict:
    """Add (or replace) a custom wake word: the .onnx as the raw body. It is
    checked to be an openWakeWord classifier before it is written, then
    offered in `available` like a built-in and assigned the same way. A
    replaced model is picked up by every satellite that listens for it."""
    data = await _body_within(request, wakewords_config.MAX_MODEL_BYTES,
                              "a wake word model is under")
    try:
        wakewords_config.check_model(name, data)
    except ValueError as e:
        raise ApiError(422, str(e), code="invalid_wake_word_model") from None
    d = hub.voice.model_dir
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.onnx.upload"
    tmp.write_bytes(data)
    os.replace(tmp, d / f"{name}.onnx")
    log.info("custom wake word model %s stored (%d bytes)", name, len(data))
    hub.spawn(hub.voice.reconcile(), name="wake-words")
    return await hub.voice.describe(admin=is_admin(request))


@app.delete("/satellites/wake-words/models/{name}")
async def delete_wake_word_model(name: str) -> Response:
    if name not in wakewords_config.custom(hub.voice.model_dir):
        raise ApiError(404, f"no custom wake word model named {name!r}", code="not_found")
    if name in hub.voice.assignment.thresholds():
        raise ApiError(409, f"{name!r} is still a wake word; remove it from the list first",
                       code="wake_word_in_use")
    (hub.voice.model_dir / f"{name}.onnx").unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/satellites/firmware")
async def list_firmware() -> dict:
    return {"firmware": sorted((vars(f) for f in hub.store.firmware.values()),
                               key=lambda f: -f["uploaded_at"])}


@app.post("/satellites/firmware")
async def upload_firmware(request: Request, model: str = Query(..., max_length=64),
                          version: str = Query("unknown", max_length=64),
                          signature: str | None = Query(None, max_length=200)) -> dict:
    image = await _body_within(request, MAX_FIRMWARE, "an OTA slot holds")
    if not image:
        raise ApiError(400, "empty body: send the .bin as the request body")
    if len(image) > MAX_FIRMWARE:
        raise ApiError(413, f"image is {len(image)} bytes; an OTA slot holds {MAX_FIRMWARE}")
    # Every ESP32 app image starts with 0xE9; a Linux satellite's release
    # bundle is a gzipped tar (clients/pi-satellite/scripts/build_bundle.py).
    if image[0] != 0xE9 and image[:2] != b"\x1f\x8b":
        raise ApiError(400, "neither an ESP32 application image (first byte 0xE9) nor a "
                            "satellite release bundle (.tar.gz)")
    try:
        sig = signing.accept_upload(image, signature, FIRMWARE_KEY)
    except signing.SignatureError as e:
        raise ApiError(400, str(e), code="bad_signature") from None
    fw = hub.store.add_firmware(image, model, version, sig)
    log.info("firmware %s stored: %s %s, %d bytes, %s", fw.sha256[:12], model, version, fw.size,
             "signed" if sig else "unsigned")
    # Every satellite of that model may have an update now (`update`).
    hub.publish({"type": "firmware", "action": "added", "sha256": fw.sha256, "model": fw.model,
                 "version": fw.version})
    return vars(fw)


async def _body_within(request: Request, limit: int, says: str) -> bytes:
    """The request body, refused with a 413 once it passes `limit`: by its
    Content-Length before a byte is read, or as it is read. request.body()
    held an upload of any size whole in memory before it could be refused."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, f"the upload is {declared} bytes; {says} {limit}",
                       code="upload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ApiError(413, f"the upload is over {limit} bytes; {says} {limit}",
                           code="upload_too_large")
    return bytes(body)


@app.delete("/satellites/firmware/{sha256}")
async def delete_firmware(sha256: str) -> Response:
    if not hub.store.delete_firmware(sha256):
        raise ApiError(404, "no such firmware")
    hub.publish({"type": "firmware", "action": "deleted", "sha256": sha256})
    return Response(status_code=204)


@app.post("/satellites/ota")
async def start_ota(body: OtaBody) -> dict:
    fw = hub.store.firmware.get(body.sha256)
    if fw is None:
        raise ApiError(404, "no such firmware")
    targets = hub.find(body.satellite)
    if not targets:
        raise ApiError(404, f"no satellite matches {body.satellite!r}")
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
        elif reason := signing.skip_reason(s.caps, fw.signature, FIRMWARE_KEY):
            # The satellite would refuse it at the end of a 15 s transfer; say
            # why now.
            skipped[nid] = reason
        else:
            s.ota = {"image": image, "sha256": fw.sha256, "version": fw.version, "state": "requested"}
            await s.send_json({"type": "ota", "size": fw.size, "sha256": fw.sha256,
                               "version": fw.version}
                              | ({"signature": fw.signature} if fw.signature else {}))
            started.append(nid)
    return {"started": started, "skipped": skipped}


@app.get("/satellites/{nid}")
async def get_satellite(nid: str, request: Request) -> dict:
    return hub.describe(hub.resolve(nid), admin=is_admin(request))


@app.post("/satellites/{nid}/adopt")
async def adopt(nid: str, body: AdoptBody, request: Request) -> dict:
    s = hub.session(nid, adopted=False)
    name = body.name or f"satellite-{s.id[-4:]}"
    token = hub.store.adopt(s.id, name, s.model, reported=s.reported())
    hub.seen.pop(s.id, None)
    await s.send_json({"type": "adopt", "token": token, "name": name})
    log.info("satellite %s adopted as %r", s.id, name)
    _mqtt_satellite(s.id)
    return hub.describe(s.id, admin=is_admin(request))


@app.post("/satellites/{nid}/forget")
async def forget(nid: str) -> Response:
    nid = hub.resolve(nid)
    s = hub.sessions.get(nid)
    hub.store.forget(nid)
    # Out of every wake word that names it, so a PUT that sends back what GET
    # returned is not refused for a satellite that no longer exists. Adopted
    # again, it hears the words for every satellite until it is given more.
    hub.voice.forget(nid)
    # A satellite that is not connected leaves no trace. Without this, a
    # satellite that was only ever seen stayed on the Satellites tab as "seen,
    # not adopted" until the hub restarted, and Forget -- the one control it
    # had -- did nothing.
    if s is None:
        hub.seen.pop(nid, None)
    if s is not None:
        await hub.unadopt(s)
        await s.send_json({"type": "forget"})
    # After store.forget: this is the only way a satellite forgotten while
    # offline is removed from Home Assistant.
    _mqtt_satellite(nid)
    return Response(status_code=204)


def checked_buttons(nid: str, buttons: dict, saved: dict) -> dict:
    """A PATCH's button mapping, with every webhook naming a secret (D62).

    A raw URL is refused with use_secret, and never repeated: it is a
    credential, and the page opens its secret picker on that code. A raw URL
    the hub still holds while its import is pending is shown under the
    secret it is being imported as (shown_buttons); sent back as shown, it is
    kept as it is, so a Save before the gateway confirms loses nothing."""
    out: dict = {}
    for button, edges in buttons.items():
        out[button] = {}
        for edge, action in edges.items():
            if action.startswith("webhook:") and not secret_import.SECRET_WEBHOOK.match(action):
                raise ApiError(422, "a button's webhook names a secret_url secret, as "
                                    "webhook:secret:<NAME>: its address is a credential and is "
                                    "kept in the secret store (Admin › Secrets), never in the "
                                    "mapping", code="use_secret", param="buttons")
            old = (saved.get(button) or {}).get(edge)
            if (isinstance(old, str) and secret_import.RAW_WEBHOOK.match(old)
                    and action == f"webhook:secret:{secret_import.button_secret(nid, button, edge)}"):
                action = old
            out[button][edge] = action
    return out


async def refuse_non_addresses(names: Iterable[str], param: str) -> None:
    """422 not_a_secret_url when a webhook is about to name, as its address,
    a secret the store holds as another kind (recheck L5). A name the store
    does not hold yet passes: it may be stored after it is named, and the
    press or the wake word is refused then if it is still no address."""
    for name in sorted(set(names)):
        problem = await not_an_address(name)
        if problem is not None:
            raise ApiError(422, problem, code="not_a_secret_url", param=param)


@app.patch("/satellites/{nid}")
async def configure(nid: str, body: ConfigBody, request: Request) -> dict:
    admin = is_admin(request)
    return hub.describe(await update_satellite(nid, body, admin=admin), admin=admin)


async def update_satellite(nid: str, body: ConfigBody, *, admin: bool) -> str:
    """PATCH /satellites/{id}, and Home Assistant's switches over MQTT. The
    controls (CONTROL_FIELDS) need satellites:control, which the gateway
    checked; anything else needs satellites:admin as well (§3.5). The id."""
    nid = hub.resolve(nid)
    rec = hub.store.satellites.get(nid)
    if rec is None:
        raise ApiError(404, "no adopted satellite with that id")
    change = body.model_dump(exclude_none=True)
    if not admin and any(k not in CONTROL_FIELDS for k in change):
        raise errors.insufficient_scope(["satellites:admin"])
    if "buttons" in change:
        change["buttons"] = checked_buttons(nid, change["buttons"], rec.config.get("buttons") or {})
        await refuse_non_addresses(
            (named.group(1) for edges in change["buttons"].values() for action in edges.values()
             if (named := secret_import.SECRET_WEBHOOK.match(action))), "buttons")
    s = hub.sessions.get(nid)
    # Offline, the caps it last proved its adoption with: a Korvo that is
    # switched off still has no audio devices. Refused only on what is known,
    # so a satellite the hub has not seen since saving caps can still be set
    # up before it comes back.
    caps = s.caps if s is not None else (rec.caps or {})
    known = s is not None or bool(caps)
    audio = [k for k in change if k in AUDIO_SETTINGS]
    if audio and known and not caps.get("audio_devices"):
        raise ApiError(409, f"satellite {nid} has no audio devices to choose from "
                            f"({', '.join(audio)})", code="no_audio_devices", param=audio[0])
    air = [k for k in change if k in AIRPLAY_SETTINGS]
    if air and known and not caps.get("airplay"):
        raise ApiError(409, f"satellite {nid} is not an AirPlay receiver", code="no_airplay",
                       param=air[0])
    if change.get("airplay_name") == "":
        change["airplay_name"] = None
    if "output_satellite" in change:
        change["output_satellite"] = _output_satellite(nid, change["output_satellite"])
    if "name" in change:
        rec.name = change["name"]
    cfg = {k: v for k, v in change.items()
           if k in DEFAULT_CONFIG or k in AUDIO_SETTINGS or k in AIRPLAY_SETTINGS or k == "output_satellite"}
    was_dark = not rec.config.get("lights_enabled", True)
    rec.config.update(cfg)
    # Set here, so the hub has it now: sent to the satellite below, and not
    # taken from its next status.
    rec.unreported = [k for k in rec.unreported if k not in cfg]
    hub.store.save_satellites()
    if s is not None and s.adopted:
        # Off means off now, not after the reply in hand has played out: the
        # rest of it would still be streamed to a satellite being told to be
        # silent. The music too, and before anything here waits on the
        # socket: meanwhile its upload found the speaker off, ended on its
        # own, and left a Pi playing the second of it that it holds.
        if cfg.get("speaker_enabled") is False:
            had_audio = s.flush_speaker()
            await hub.stop_media(s, "speaker_off")
            if had_audio:
                await s.send_json({"type": "flush"})
        to_satellite = (satellite_config(cfg) | ({"name": rec.name} if "name" in change else {})
                        | (hub.button_actions(s, cfg) if "buttons" in cfg else {}))
        if to_satellite:
            await s.send_json({"type": "config", **to_satellite})
        # A ring lit by a conversation that went dark mid-way was never put
        # out, because nothing may be sent to a dark satellite. Now it may.
        if was_dark and cfg.get("lights_enabled") and s.lit and s.conversation is None:
            await hub.send_lights(s, {"mode": "off"})
        # Nothing more will be heard: a conversation waiting for its next turn
        # ends, and a command already said still gets its answer.
        conv = s.conversation
        if cfg.get("mic_enabled") is False and conv is not None and (
                conv.phase == "listening" or conv.memory is not None):
            conv.cancel("mic_off")
    # What changed, by name: Home Assistant reads the values back, and a
    # webhook in a button mapping is not for the event stream.
    if change:
        hub.publish({"type": "config", "satellite": nid, "changed": sorted(change)})
    _mqtt_satellite(nid)
    return nid


@app.get("/satellites/{nid}/airplay/artwork")
async def airplay_artwork(nid: str, v: str | None = Query(None, pattern="^[0-9a-f]{64}$")) -> Response:
    """The cover of what the satellite's AirPlay receiver plays, as it sent
    it. The page and Home Assistant ask with ?v=<sha256>, so an answer can be
    kept: and one for another picture is a 404, so that a picture that has
    changed since is never kept under a SHA that is not its own."""
    s = hub.sessions.get(hub.resolve(nid))
    if s is None or not s.artwork or (v is not None and v != s.artwork["sha256"]):
        raise ApiError(404, f"satellite {nid} has no AirPlay artwork now"
                            + (" with that SHA-256" if v and s is not None and s.artwork else ""),
                       code="no_artwork")
    return Response(s.artwork["data"], media_type=s.artwork["type"],
                    headers={"ETag": f'"{s.artwork["sha256"]}"', "Cache-Control": "private, max-age=86400"})


@app.post("/satellites/{nid}/identify")
async def identify(nid: str) -> Response:
    # Allowed before adoption: telling identical boxes apart is what it is for.
    await hub.session(nid, adopted=False).send_json({"type": "identify", "seconds": 5})
    return Response(status_code=204)


@app.post("/satellites/{nid}/reboot")
async def reboot(nid: str) -> Response:
    await hub.session(nid).send_json({"type": "reboot"})
    return Response(status_code=204)


@app.post("/satellites/{nid}/set-hub")
async def set_hub(nid: str, body: HubBody) -> Response:
    """Point the satellite at another hub. It saves the address and reboots;
    the other hub sees it as pending, with no adoption carried over."""
    await hub.session(nid).send_json({"type": "set_hub", "url": body.url})
    return Response(status_code=204)


@app.post("/satellites/{nid}/lights")
async def lights(nid: str, body: LightsBody) -> Response:
    s = hub.session(nid)
    if not await hub.send_lights(s, body.model_dump(exclude_none=True)):
        raise ApiError(409, f"satellite {s.id} has its lights turned off (lights_enabled is false)",
                       code="lights_disabled")
    return Response(status_code=204)


def _output_satellite(nid: str, target: str) -> str | None:
    """A satellite's Output, checked: "" is its own speaker (None); another
    must be adopted, not this one, have a speaker when it is connected, and
    not play back into this one."""
    if not target:
        return None
    if target == nid:
        raise ApiError(422, "a satellite's output is its own speaker; name another satellite, or \"\"",
                       code="invalid_output", param="output_satellite")
    if target not in hub.store.satellites:
        raise ApiError(422, f"no adopted satellite {target}", code="invalid_output",
                       param="output_satellite")
    other = hub.sessions.get(target)
    if other is not None and "speaker" not in other.caps and other.caps:
        raise ApiError(409, f"satellite {target} has no speaker", code="no_speaker",
                       param="output_satellite")
    seen, cur = {nid}, target
    for _ in range(OUTPUT_HOPS):
        rec = hub.store.satellites.get(cur)
        nxt = (rec.config.get("output_satellite") if rec else None) or None
        if nxt is None:
            break
        if nxt in seen or nxt == nid:
            raise ApiError(422, f"satellite {target} plays through {nid} already, so this would "
                                "go round in a loop", code="output_loop", param="output_satellite")
        seen.add(nxt)
        cur = nxt
    return target


def _speaker_on(s: Session) -> Session:
    """A satellite with its speaker off is sent no audio at all, as one with
    its lights off is sent no "lights". The firmware keeps its amplifier off
    too, but the promise is the hub's: it must not depend on every board's
    firmware having taken the setting."""
    if not hub.speaker_allowed(s):
        raise ApiError(409, f"satellite {s.id} has its speaker turned off "
                            "(speaker_enabled is false)", code="speaker_disabled")
    return s


@app.post("/satellites/{nid}/tone")
async def tone(nid: str, body: ToneBody) -> Response:
    s = _speaker_on(hub.speaker_for(hub.session(nid)))
    s.speaker.put_nowait(audio.tone(body.frequency, body.seconds, s.spk_rate))
    return Response(status_code=204)


@app.post("/satellites/{nid}/say")
async def say(nid: str, body: SayBody) -> Response:
    s = _speaker_on(hub.speaker_for(hub.session(nid)))
    if not TTS_URL:
        raise ApiError(503, "SATELLITES_TTS_URL is not set, so there is no voice to speak with")
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as c:
        try:
            r = await gateway.request(c, "POST", f"{TTS_URL}/v1/audio/speech", json={
                "model": "kokoro", "voice": body.voice or TTS_VOICE, "input": body.text,
                "response_format": "pcm"})
        except gateway.NotReady as e:
            raise ApiError(503, f"tts: {e}", code="not_ready") from None
    if r.status_code != 200:
        raise ApiError(502, f"tts answered {r.status_code}: {r.text[:200]}")
    # Asked again: synthesis takes seconds, and the speaker may have been
    # turned off while it ran.
    _speaker_on(s)
    # Kokoro's pcm is 24 kHz mono s16le (voice_common.audio.SAMPLE_RATE).
    s.speaker.put_nowait(audio.resample(r.content, 24000, s.spk_rate))
    return Response(status_code=204)


class PttBody(BaseModel):
    wake_word: str | None = Field(default=None, max_length=64)


@app.post("/satellites/{nid}/ptt")
async def ptt(nid: str, body: PttBody | None = None) -> Response:
    """Listen on a satellite as if its push-to-talk button had been pressed:
    Home Assistant's way to start a command from an automation or a dashboard
    (clients/home-assistant). What follows is handled by `wake_word`'s entry
    when one is named, else by push-to-talk's. 409 when the satellite cannot
    listen now, and why."""
    s = hub.session(nid)
    word = (body.wake_word if body else None) or listening.PTT
    if word != listening.PTT:
        behaviour = hub.voice.assignment.behaviour(word)
        if behaviour is None:
            raise ApiError(404, f"no wake word {word!r} with an action", code="wake_word_not_found")
        if behaviour.mode == "trigger":
            raise ApiError(409, f"{word!r} is a trigger word: it is the whole command, so there "
                                "is nothing to listen for after it", code="trigger_word")
    why = hub.ptt_refusal(s)
    if why is not None:
        raise ApiError(409, why[1], code=why[0])
    await hub.push_to_talk(s, word)
    return Response(status_code=204)


@app.post("/satellites/{nid}/flush")
async def flush(nid: str) -> Response:
    """Drop the speaker audio queued and playing, the music, and the
    conversation in progress: what the "stop" button does."""
    await hub.stop(hub.session(nid))
    return Response(status_code=204)


def _media_answer(reason: str, played_s: float | None) -> dict:
    return {"played_s": None if played_s is None else round(played_s, 2),
            "stopped": reason != "ended", "reason": reason}


def _dropped(s: Session) -> str:
    """Why an announcement queued on `s` did not play to its end, as the
    reasons of a media stream say it."""
    if hub.sessions.get(s.id) is not s:
        return "disconnected"
    if not s.adopted:
        return "unadopted"
    if not hub.speaker_allowed(s):
        return "speaker_off"
    return "muted" if s.status.get("muted") else "stopped"


@app.post("/satellites/{nid}/media")
async def media(nid: str, request: Request, announce: bool = Query(False)) -> dict:
    """Play a WAV that is still arriving: Home Assistant's play_media, its
    TTS and its announcements (clients/home-assistant), converted by its own
    ffmpeg to exactly the format GET /satellites/{id} names under `media`.
    The hub reads the header, refuses any other format, and relays the PCM;
    it never decodes or resamples.

    Music (?announce=0) plays on the media lane of the satellite that plays
    for this one (Hub.stream_media), in place of any stream already playing
    there, which ends "superseded". An announcement (?announce=1) is read
    whole and played as one sentence on the voice lane, after whatever is
    queued there, with the music paused or ducked under it. Either way the
    answer comes when it has played, or has been stopped: {played_s,
    stopped, reason}. Errors come before any audio plays."""
    src = hub.session(nid)
    s = _speaker_on(hub.speaker_for(src))
    if s.caps and "speaker" not in s.caps:
        raise ApiError(409, f"satellite {s.id} has no speaker", code="no_speaker")
    want = hub.media_view(src.id)["announce" if announce else "music"]
    try:
        (rate, channels), pcm = await audio.wav_stream(request.stream())
    except ValueError as e:
        raise ApiError(415, f"not a 16-bit PCM WAV: {e}", code="unsupported_audio",
                       param="body") from None
    except ClientDisconnect:
        return _media_answer("cancelled", None if announce else 0.0)
    if (rate, channels) != (want["rate"], want["channels"]):
        raise ApiError(415, f"satellite {s.id} plays {'an announcement' if announce else 'music'} "
                            f"as {want['rate']} Hz, {want['channels']} channel(s); this WAV is "
                            f"{rate} Hz, {channels} channel(s)", code="format_mismatch", param="body")
    stream = MediaStream(src.id)
    event = {"type": "media", "satellite": s.id, "source": src.id, "id": stream.id,
             "announce": announce}

    if announce:
        limit = ANNOUNCE_MAX_S * rate * 2 * channels
        body = bytearray()
        try:
            async for chunk in pcm:
                body += chunk
                if len(body) > limit:
                    raise ApiError(413, f"an announcement is at most {ANNOUNCE_MAX_S} s",
                                   code="announce_too_long", param="body")
        except ClientDisconnect:
            return _media_answer("cancelled", None)
        # Asked again: the upload took as long as Home Assistant took to
        # convert it, and the speaker may have been turned off meanwhile.
        _speaker_on(s)
        if hub.sessions.get(s.id) is not s:
            return _media_answer("disconnected", None)
        clip = Clip(bytes(body[:len(body) // 2 * 2]), "announcement")
        s.speaker.put_nowait(clip)
        hub.publish(event | {"state": "playing", "reason": None, "played_s": None})
        # Ended in a finally, as the music is below: a handler cancelled at
        # shutdown must not leave the events saying it plays for good.
        reason, played_s = "cancelled", None
        try:
            played = await clip.done
            # Sent is not heard: the satellite still holds what was sent ahead.
            if played and (left := s.play_until - time.monotonic()) > 0:
                await asyncio.sleep(left)
            reason = "ended" if played else _dropped(s)
            played_s = len(clip.pcm) / 2 / rate if played else None
        finally:
            hub.publish(event | {"state": "ended", "reason": reason, "played_s": played_s})
        return _media_answer(reason, played_s)

    while s.media is not None:
        await hub.stop_media(s, "superseded")
    s.media = stream
    hub.publish(event | {"state": "playing", "reason": None, "played_s": 0.0})
    reason = None
    try:
        reason = await hub.stream_media(s, stream, pcm, rate, channels)
    finally:
        if s.media is stream:
            s.media = None
        # In the finally: a body that broke off some other way, or the hub
        # shutting down, still ends what the page and Home Assistant were
        # told is playing, or they show it playing for good.
        reason = reason or stream.stopped or "cancelled"
        played_s = round(stream.played_s, 2)
        hub.publish(event | {"state": "ended", "reason": reason, "played_s": played_s})
    log.info("satellite %s: media for %s %s after %.1f s", s.id, src.id, reason, played_s)
    return _media_answer(reason, played_s)


@app.post("/satellites/{nid}/media/stop")
async def media_stop(nid: str) -> Response:
    """End the media stream playing for this satellite, and have a satellite
    with a media lane drop what it holds of it. 204 whether or not anything
    was playing: Home Assistant's media_stop."""
    await hub.stop_media(hub.speaker_for(hub.session(nid)), "stopped")
    return Response(status_code=204)


@app.post("/satellites/{nid}/airplay/{command}")
async def airplay_command(nid: str, command: Literal[AIRPLAY_COMMANDS]) -> dict:
    """Ask the phone playing to the satellite's AirPlay receiver to play,
    pause, skip or stop, or end its session (disconnect). The satellite asks
    Shairport Sync, which asks the phone, and answers with the phone's own
    answer and whether it saw the change happen (`confirmed`); the phone
    decides, and may take a command and not act on it.

    Refused at once (409) for what cannot work: a satellite that is no
    AirPlay receiver, an agent too old to take commands (it would never
    answer), no phone connected, or a command the phone does not take now."""
    s = hub.session(nid)
    caps = s.caps.get("airplay")
    if not caps:
        raise ApiError(409, f"satellite {s.id} is not an AirPlay receiver", code="no_airplay")
    if not (isinstance(caps, dict) and caps.get("controls") is True):
        raise ApiError(409, f"satellite {s.id} runs an agent that takes no AirPlay commands; "
                            "update it", code="airplay_no_controls")
    air = s.status.get("airplay") if isinstance(s.status.get("airplay"), dict) else {}
    if air.get("session") is not True:
        raise ApiError(409, f"no phone is playing to satellite {s.id}", code="airplay_idle")
    remote = air.get("remote") if isinstance(air.get("remote"), dict) else {}
    controls = remote.get("controls") if isinstance(remote.get("controls"), list) else []
    if command not in controls:
        raise ApiError(409, f"the phone playing to satellite {s.id} does not take {command} now",
                       code="airplay_no_remote")
    rid = uuid.uuid4().hex
    reply = asyncio.get_running_loop().create_future()
    s.replies[rid] = reply
    result: dict = {"ok": False}
    try:
        if not await _quietly(s.send_json({"type": "airplay_command", "id": rid, "command": command})):
            raise ApiError(409, f"satellite {s.id} is not connected", code="satellite_offline")
        try:
            result = await asyncio.wait_for(reply, AIRPLAY_WAIT_S)
        except TimeoutError:
            raise ApiError(504, f"satellite {s.id} did not answer {command} within "
                                f"{AIRPLAY_WAIT_S:g} s", code="satellite_timeout") from None
        if hub.sessions.get(s.id) is not s:
            raise ApiError(409, f"satellite {s.id} went away before it answered",
                           code="satellite_offline")
    finally:
        s.replies.pop(rid, None)
        hub.publish({"type": "airplay_command", "satellite": s.id, "command": command,
                     "ok": result.get("ok") is True, "status": result.get("status"),
                     "confirmed": result.get("confirmed")})
    if result.get("ok") is not True:
        raise ApiError(502, f"the phone did not take {command} ({result.get('status')}: "
                            f"{result.get('error')})", code="airplay_refused")
    return {"command": command, "status": result.get("status"), "confirmed": result.get("confirmed")}


@app.post("/satellites/{nid}/listen")
async def listen(nid: str, seconds: float = Query(5, gt=0, le=60),
                 channel: int | None = Query(None, ge=0, le=7)) -> Response:
    """A recording of the raw microphone channels. A POST, not a GET: it
    opens a microphone, which a link or an <img> on another site must never
    do (D15, H3); the gateway audits each one."""
    s = hub.session(nid)
    if s.status.get("muted"):
        raise ApiError(409, "the satellite is muted at the device; only its REC button unmutes it")
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
                raise ApiError(504, "the satellite stopped sending audio (mic disabled or muted?)")
            buf += await asyncio.wait_for(q.get(), timeout=left)
    except asyncio.TimeoutError:
        raise ApiError(504, "the satellite sent no audio in time (mic disabled or muted?)")
    finally:
        s.taps.discard(q)
    pcm = bytes(buf[:want])
    if channel is not None:
        if channel >= ch:
            raise ApiError(400, f"the satellite has {ch} channels")
        pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, ch)[:, channel].tobytes()
        ch = 1
    return Response(audio.wav(pcm, rate, ch), media_type="audio/wav",
                    headers={"Content-Disposition": f'attachment; filename="{s.id}.wav"'})


# ---- verifying the pipeline without a voice ---------------------------------------


def _read_clip(body: bytes) -> np.ndarray:
    if not body:
        raise ApiError(400, "empty body: send a 16 kHz mono 16-bit WAV as the request body")
    try:
        with wave.open(io.BytesIO(body)) as w:
            shape = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            n = w.getnframes()
            pcm = w.readframes(n)
    except (wave.Error, EOFError):
        raise ApiError(400, "the body is not a WAV file") from None
    if shape != (listening.RATE, 1, 2):
        rate, ch, width = shape
        raise ApiError(400, f"the clip is {rate} Hz, {ch} channel(s), {8 * width}-bit; "
                            "inject takes 16 kHz mono 16-bit")
    if n > MAX_INJECT_S * listening.RATE:
        raise ApiError(413, f"the clip is {n / listening.RATE:.0f} s; inject takes at most {MAX_INJECT_S} s")
    return np.frombuffer(pcm[:len(pcm) & ~1], dtype="<i2").astype(np.int16)


def silence_for(nid: str, word: str) -> int | None:
    """The pause that ends `word`'s command on this satellite (its
    silence_ms), for the Ear to open the command with."""
    rec = hub.store.satellites.get(nid)
    route = routing.current().find(nid, rec.name if rec else "", word)
    return route.behaviour.silence_ms if route is not None else None


def _hear_clip(clip: np.ndarray, wake: wakeword.WakeWords | None, wake_word: str | None,
               nid: str = "") -> tuple[listening.Heard | None, listening.Command | None]:
    """A clip through a fresh Ear, in the satellite's own 20 ms frames, then
    room noise until the endpointer ends the command, which ends on the
    word's own pause as it would live. Runs in the thread pool."""
    ear = listening.Ear(channels=1, frontend=False, wake=wake,
                        silence_for=lambda word: silence_for(nid, word) if nid else None)
    if wake_word:
        ear.push_to_talk(wake_word)
    heard = command = None
    frame = listening.RATE * 20 // 1000
    # openWakeWord fires up to about a second after the word ends (0.8 s on the
    # fixtures), so a clip that stops right after it still gets that long.
    grace = int(WAKE_GRACE_S * listening.RATE)
    x = np.concatenate((clip, listening.room_floor(COMMAND_WAIT_S)))
    for off in range(0, len(x), frame):
        for ev in ear.process(x[off:off + frame].reshape(-1, 1)):
            if isinstance(ev, listening.Heard) and heard is None:
                heard = ev
            elif isinstance(ev, listening.Command) and command is None:
                command = ev
        if command is not None or (heard is None and off >= len(clip) + grace):
            break
    return heard, command


@app.post("/satellites/{nid}/inject")
async def inject(nid: str, request: Request, play: bool = Query(False),
                 wake_word: str | None = Query(
                     None, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")) -> dict:
    """Run a recorded clip through the listening path as if the satellite
    had heard it: wake word, endpoint, rule, STT, destination, TTS. The
    reply is played only with ?play=1; without it nothing at all is sent to
    any satellite, which is how the whole path is checked with nobody in
    earshot. Detection listens for the satellite's own wake words, as it
    would live. ?wake_word= skips detection and treats the whole clip as the
    command after that word, as push-to-talk does. Events are published
    marked "injected" and are not forwarded to MQTT."""
    nid = hub.resolve(nid)
    rec = hub.store.satellites.get(nid)
    if rec is None:
        raise ApiError(409, f"satellite {nid} is not adopted", code="satellite_not_adopted")
    s = None
    if play:
        s = hub.session(nid)
        if s.conversation is not None:
            raise ApiError(409, f"satellite {nid} is in a conversation already",
                           code="satellite_busy")
    clip = _read_clip(await request.body())
    loop = asyncio.get_running_loop()
    wake = None
    if wake_word is None:
        # The satellite's own words, as it would listen live: a clip with a
        # word assigned only to the kitchen is not heard through the bedroom.
        _, base, ready = hub.voice.plan(nid)
        assigned = hub.voice.assignment.effective(nid)
        if not assigned and hub.voice.assignment.load_error is None:
            raise ApiError(409, f"satellite {nid} is assigned no wake word; assign one with "
                                "PUT /satellites/wake-words, or pass ?wake_word= to skip "
                                "detection", code="no_wake_words")
        if not ready:
            raise ApiError(503, f"none of satellite {nid}'s wake words "
                                f"({', '.join(assigned) or 'none'}) is loaded (state "
                                f"{hub.voice.state}"
                                f"{': ' + hub.voice.error if hub.voice.error else ''}); pass "
                                "?wake_word= to skip detection", code="wake_words_unavailable")
        wake = await loop.run_in_executor(EXECUTOR, base.clone, ready)
    heard, command = await loop.run_in_executor(EXECUTOR, _hear_clip, clip, wake, wake_word, nid)
    if heard is None:
        return {"satellite": nid, "heard": None, "command": None, "outcome": None, "played": False}
    behaviour = hub.voice.assignment.behaviour(heard.wake_word)
    if behaviour is not None and behaviour.mode == "trigger":
        # A trigger's whole effect is its event; marked injected, it reaches
        # neither MQTT nor, by the integration's own filter, Home Assistant.
        hub.publish({"type": "triggered", "satellite": nid, "satellite_name": rec.name,
                     "wake_word": heard.wake_word, "score": heard.score,
                     "direction": heard.direction, "injected": True})
        return {"satellite": nid,
                "heard": {"wake_word": heard.wake_word, "score": heard.score, "at_s": heard.at_s},
                "command": None, "outcome": None, "triggered": True, "played": False}
    if play and s.conversation is not None:
        raise ApiError(409, f"satellite {nid} is in a conversation already", code="satellite_busy")
    conv = Conversation(hub, nid, rec.name, heard, session=s, quiet=not play, injected=True)
    if s is not None:
        s.conversation = conv
    conv.deliver(command)
    # A task, as a live conversation is, so the satellite's stop button cancels
    # this one too; run() ends with the routed event even when cancelled.
    conv.start()
    event = await conv.task
    return {"satellite": nid,
            "heard": {"wake_word": heard.wake_word, "score": heard.score, "at_s": heard.at_s},
            "command": {"reason": command.reason, "seconds": round(command.seconds, 2),
                        "had_speech": command.had_speech},
            "outcome": conv.outcome.as_json(), "played": event["played"], "note": event["note"]}
