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
    GET   /satellites/wake-words       the wake words, which satellites hear each, and
                                       whether its model is ready
    PUT   /satellites/wake-words       replace them; live, no restart
    GET   /satellites/{id}
    PATCH /satellites/{id}             name, config and the button mapping
    POST  /satellites/{id}/adopt | forget | identify | reboot | lights | tone | say
                        | flush | set-hub
    GET   /satellites/{id}/listen?seconds=5   a WAV of the raw microphone channels
    POST  /satellites/{id}/inject?play=0      a 16 kHz mono WAV through the wake
                                       word, endpoint and routing path, as if heard

EVERYTHING IS UNDER /satellites, INCLUDING THE SOCKET, because the gateway
mounts backend paths flat and never rewrites them. The gateway relays
/satellites/ws (and /nodes/ws) as a WebSocket and forwards the rest as
ordinary routes.

THE DEVICE SOCKET IS NOT BEHIND AN API KEY, AND THAT IS DELIBERATE. A
satellite cannot hold a gateway key it was never given, and a key baked into
firmware would be a key in every flash dump. The socket is answered for anyone;
what a connection may DO is decided by the adoption token. An unadopted
connection can say hello and receive "pending", nothing else: no microphone
audio is accepted from it and nothing is sent to it but that one word, until
someone with access to the API adopts it. Nor can it take the place of an
adopted satellite that is connected: a satellite's id is its MAC, and only the
token proves the rest.

THE LISTENING PATH, per adopted satellite. The socket loop hands microphone
frames to a bounded queue (the oldest frame goes when it is full, and is
counted); one task per satellite drains it and runs listening.Ear (front-end,
wake words, endpointer) in a thread pool, so the event loop never does signal
work. Each satellite listens only for the wake words assigned to it
(wake_words.json, see Voice and wakewords_config.py). A wake word starts a
Conversation: the "wake" earcon if the satellite holds it, the ring pointed at
the talker, the satellite ducked, then the command, the router, and the reply
on whichever satellite the rule names. One conversation per satellite at a
time.

A SATELLITE WITH LIGHTS OFF IS NEVER SENT "lights". Every lights message this
hub sends goes through Hub.send_lights, which reads the satellite's
lights_enabled at the moment of sending; POST /satellites/{id}/lights answers
409 for such a satellite rather than go around it. The ring stays dark in a
bedroom because the hub does not ask, not only because the firmware would
refuse.

A SATELLITE WITH ITS SPEAKER OFF IS SENT NOTHING AUDIBLE, on the same terms:
earcons and replies read speaker_enabled when they are sent, /tone and /say
answer 409, and turning the speaker off drops whatever was still playing.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import struct
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Literal

import httpx
import numpy as np
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator
from voice_common import auth, errors, health
from voice_common import logging as voice_logging
from voice_common.errors import ApiError

from . import audio, earcons, listening, signing, wakeword, wakewords_config
from . import router as routing
from .mqtt import MqttBridge
from .store import DEFAULT_CONFIG, Store, reported_config, satellite_config

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
HEADER = 16
# Under the satellite's own ceiling: the Arduino WebSockets library drops the
# whole connection on any frame over 15 KB (WEBSOCKETS_MAX_DATA_SIZE, not
# overridable on ESP32). 16 KB chunks killed the first real update 17 ms in.
OTA_CHUNK = 8 * 1024
SPEAKER_CHUNK_MS = 20
SPEAKER_LEAD_S = 0.3  # how far ahead of real time playback is kept

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
LIGHTS_MIN_S = 0.15    # at most one direction update this often
MAX_INJECT_S = 60
WAKE_GRACE_S = 1.5
DEFAULT_EARCONS = earcons.defaults()

FIRMWARE_KEY: Any | None = None
EXECUTOR: ThreadPoolExecutor | None = None


def satellite_id(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


# ---- live connections -------------------------------------------------------


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
        # own address along. Display only -- nothing is decided on it.
        self.address = ws.headers.get("x-forwarded-for") or (ws.client.host if ws.client else None)
        self.adopted = False
        self.status: dict = {}
        self.taps: set[asyncio.Queue] = set()
        self.speaker: asyncio.Queue[bytes] = asyncio.Queue()
        self.speaker_gen = 0      # bumped by a flush; the loop drops the item in hand
        self.playing = False
        self.ota: dict | None = None
        self.mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MIC_QUEUE)
        self.mic_dropped = 0
        self.ear: listening.Ear | None = None
        self.listener: asyncio.Task | None = None
        self.listen_error: str | None = None
        self.conversation: Conversation | None = None
        self.earcons: earcons.Sync | None = None
        self.earcon_asks = 0
        self.lit = False          # the hub's own layer is showing something
        self.duck_holds = 0       # conversations that want this satellite ducked
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

    async def send_if(self, allowed: Callable[[], bool], obj: dict | None = None,
                      data: bytes | None = None) -> bool:
        """Send `obj` as JSON, or `data` as a binary frame, only if allowed()
        still holds once the lock is ours. Checked inside the lock and not
        before it: the speaker loop holds the lock for every 20 ms frame, and a
        PATCH that turns the lights or the speaker off while a send waits for
        it must stop that send, not let it out just after the setting said
        no."""
        async with self.lock:
            if not allowed():
                return False
            if obj is not None:
                await self.ws.send_text(json.dumps(obj))
            else:
                await self.ws.send_bytes(data)
            return True

    def reported(self) -> dict:
        """The settings this satellite has said it has: its hello (firmware
        from 2026-09-25 on) with its latest status over it."""
        return reported_config(self.hello) | reported_config(self.status)

    @property
    def spk_rate(self) -> int:
        return self.caps.get("speaker", {}).get("rate", 48000)

    @property
    def mic_rate(self) -> int:
        return self.caps.get("mic", {}).get("rate", 16000)

    @property
    def mic_channels(self) -> int:
        return self.caps.get("mic", {}).get("channels", 4)

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
            self.speaker.get_nowait()
        self.speaker_gen += 1
        return had

    async def speaker_loop(self, allowed: Callable[[], bool]) -> None:
        """Play the queue at real time. `allowed` is Hub.speaker_allowed for
        this satellite, and it is the last word: whatever queued audio -- a
        reply handed over just as the speaker was turned off, a tone playing
        when the satellite was forgotten -- goes nowhere once it says no. The
        callers check too, but each check before a queue is a moment before
        the audio plays."""
        seq = 0
        chunk = self.spk_rate * 2 * SPEAKER_CHUNK_MS // 1000
        while True:
            pcm = await self.speaker.get()
            gen = self.speaker_gen
            self.playing = True
            start, sent = time.monotonic(), 0.0
            for off in range(0, len(pcm), chunk):
                # Checked per chunk: a reply is one item, and a flush that only
                # emptied the queue would let a two-minute answer play on.
                piece = pcm[off:off + chunk]
                frame = struct.pack("<BBBBIQ", FRAME_SPEAKER, 0, 1, 0, seq, 0) + piece
                if not await self.send_if(lambda: self.speaker_gen == gen and allowed(),
                                          data=frame):
                    break
                seq = (seq + 1) & 0xFFFFFFFF
                sent += len(piece) / 2 / self.spk_rate
                ahead = sent - (time.monotonic() - start)
                if ahead > SPEAKER_LEAD_S:
                    await asyncio.sleep(ahead - SPEAKER_LEAD_S)
            self.playing = False


async def _quietly(coro) -> bool:
    """A send to a satellite that may have gone: a conversation cleaning up
    after a disconnect must not raise out of its finally block."""
    try:
        await coro
        return True
    except Exception as e:  # a closed socket raises several different things
        log.debug("send failed: %s", e)
        return False


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
            out.append({"name": w.name, "threshold": w.threshold,
                        "satellites": list(w.satellites), "state": state, "error": error})
        return out

    def describe(self) -> dict:
        return {"available": wakewords_config.available(self.model_dir), "words": self.views(),
                "custom": wakewords_config.custom(self.model_dir),
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

    def replace(self, words: list[wakewords_config.Word]) -> None:
        """Save a new assignment and make what can be live, live: thresholds
        now, plan() now. Words not loaded yet need reconcile()."""
        self.assignment.replace(words)
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

    def describe(self, nid: str) -> dict:
        rec = self.store.satellites.get(nid)
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
            "ota": _ota_view(s),
            "listening": self._listening(s),
            "earcons": self._earcons(s),
            # Assigned, whether or not the model has loaded yet: GET
            # /satellites/wake-words has each word's state.
            "wake_words": self.voice.assignment.effective(nid),
        }

    @staticmethod
    def _listening(s: Session | None) -> dict | None:
        # getattr throughout: tests stand a SimpleNamespace in for a Session.
        ear = getattr(s, "ear", None)
        if ear is None:
            err = getattr(s, "listen_error", None)
            return {"state": "off", "error": err} if err else None
        conv = getattr(s, "conversation", None)
        return ear.stats() | {"mic_dropped": s.mic_dropped,
                              "conversation": conv.phase if conv else None}

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

    def proves_adoption(self, s: Session) -> bool:
        rec = self.store.satellites.get(s.id)
        return bool(rec and rec.accepts(s.hello.get("token") or None))

    def take_report(self, s: Session, msg: dict) -> None:
        """Settings the hub had not had from this satellite, from its hello or
        status (Store.take_report). Home Assistant shows the record, so it is
        told."""
        if self.store.take_report(s.id, msg):
            log.info("satellite %s reported %s", s.id, satellite_config(self.config(s)))
            if self.bridge is not None:
                self.bridge.publish_satellite(self.describe(s.id))

    async def unadopt(self, s: Session) -> None:
        """A connected satellite becomes pending: forgotten, or a hello whose
        token does not match. `adopted` goes first, because the speaker loop
        and every earcon, light and duck read it at the moment of sending: the
        tone still playing and the conversation cancelled here stop at their
        next send instead of streaming on to a pending satellite.

        Then the satellite is told to drop what it has buffered and to lift a
        duck the hub was holding. Both go out ahead of the "forget" or
        "pending" the caller sends next, which the satellite reads in order,
        so it takes them while it still counts itself adopted."""
        was = s.adopted
        s.adopted = False
        had_audio = s.flush_speaker()
        ducked = s.duck_holds > 0 and s.caps.get("duck")
        s.duck_holds = 0
        self.stop_listening(s)
        s.earcons = None
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
            s.adopted = True
            await s.send_json({"type": "welcome", "name": rec.name,
                               "config": satellite_config(rec.config, rec.unreported)})
            self.publish({"type": "online", "satellite": s.id, "name": rec.name, "firmware": s.fw})
            await self.sync_earcons(s)
            self.start_listening(s)
        else:
            await self.unadopt(s)
            if rec and token:
                log.warning("satellite %s presented a token that does not match its adoption", s.id)
            self.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time()}
            await s.send_json({"type": "pending"})
            self.publish({"type": "pending", "satellite": s.id, "address": s.address})

    # -- listening ----------------------------------------------------------

    def may_listen(self, s: Session) -> bool:
        cfg = self.config(s)
        return s.adopted and cfg.get("mic_enabled", True) and not s.status.get("muted")

    def start_listening(self, s: Session) -> None:
        if s.listener is not None and not s.listener.done():
            return
        try:
            s.ear = listening.Ear(debug_s=DEBUG_AUDIO_S, rate=s.mic_rate, channels=s.mic_channels,
                                  frontend=self.voice.frontend)
        except ValueError as e:
            s.listen_error = str(e)
            log.warning("satellite %s will not be listened to: %s", s.id, e)
            return
        s.listen_error = None
        s.listener = asyncio.create_task(listen_loop(self, s), name=f"listen-{s.id}")

    def stop_listening(self, s: Session) -> None:
        if s.listener is not None:
            s.listener.cancel()
            s.listener = None
        if s.conversation is not None:
            s.conversation.cancel()
        s.ear = None
        while not s.mic.empty():
            s.mic.get_nowait()

    def heard(self, s: Session, heard: listening.Heard) -> None:
        if s.conversation is not None:
            return  # one conversation per satellite; the Ear already drops these
        rec = self.store.satellites.get(s.id)
        conv = Conversation(self, s.id, rec.name if rec else "", heard, session=s)
        s.conversation = conv
        conv.start()

    async def push_to_talk(self, s: Session, word: str = listening.PTT) -> None:
        if s.conversation is not None or s.ear is None:
            return
        if not self.may_listen(s):
            # Nothing would arrive to be heard. Say so, rather than open a
            # conversation that can only time out.
            log.info("satellite %s: push-to-talk while its microphone is off or muted", s.id)
            await self.earcon(s, "error")
            return
        s.ear.push_to_talk(word)

    async def stop(self, s: Session) -> None:
        """What the "stop" button and POST /satellites/{id}/flush do:
        silence the satellite now and drop whatever it was in the middle of."""
        s.flush_speaker()
        await s.send_json({"type": "flush"})
        if s.conversation is not None:
            s.conversation.cancel()

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

    async def earcon(self, s: Session | None, eid: str) -> bool:
        if s is None or s.earcons is None or not s.earcons.has(eid):
            return False
        return await _sent(s.send_if(lambda: self.speaker_allowed(s), earcons.play_message(eid)))

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
        if not act or act == "none":
            return
        if act == "ptt":
            await self.push_to_talk(s)
        elif act == "stop":
            await self.stop(s)
        elif act.startswith("webhook:"):
            rec = self.store.satellites.get(s.id)
            self.spawn(self._button_webhook(act.removeprefix("webhook:"), {
                "satellite": rec.name if rec else s.id, "satellite_id": s.id, "button": button,
                "action": action, "held_ms": held_ms}), name=f"button-{s.id}")

    async def _button_webhook(self, url: str, body: dict) -> None:
        # Redirects are not followed (the client says so), and the whole call
        # is bounded, as every call router.py makes is.
        try:
            async with asyncio.timeout(10):
                r = await self.http.post(url, json=body)
            if r.status_code >= 400:
                log.warning("button webhook %s answered %d", url, r.status_code)
        except Exception as e:
            log.warning("button webhook %s failed: %s", url, e or type(e).__name__)


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
        if ear.state != "idle" and s.conversation is None:
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
                                  frontend=h.voice.frontend)
            if s.conversation is not None:
                s.conversation.cancel()
            continue
        dropped = False
        for ev in events:
            if isinstance(ev, listening.Heard):
                # A batch that was in the thread pool when a word was removed
                # or moved to another satellite ran on the old detector. What
                # it heard is dropped here, so a removal is final the moment
                # PUT /satellites/wake-words answers, not one batch later.
                # Push-to-talk has no score and is never assigned.
                if ev.score is not None and not h.voice.listens(s.id, ev.wake_word):
                    ear.release()
                    dropped = True
                    continue
                dropped = False
                h.heard(s, ev)
            elif dropped:
                continue  # the command after a dropped wake word
            elif s.conversation is not None:
                if isinstance(ev, listening.Command) and ev.debug:
                    loop.run_in_executor(EXECUTOR, _save_debug, s.id, ev)
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

def ring(direction: float, leds: int, colour: tuple[int, int, int]) -> list[list[int]]:
    """A pixels frame pointing at `direction`: full at the nearest LED, soft on
    its neighbours. LED 0 is taken to sit at 0 degrees (towards microphone 1)
    and to count the same way as the microphones. Neither has been checked on
    a board, so the pointer may be rotated or mirrored; see listening.py."""
    pos = direction / 360.0 * leds
    out = []
    for i in range(leds):
        d = abs(i - pos) % leds
        d = min(d, leds - d)
        w = max(0.0, 1.0 - d / 2.0) ** 2
        out.append([round(c * w) for c in colour])
    return out


class Conversation:
    """One wake word, or one push of a button, through to its reply.

    Created by the listener (or by /inject) and run as its own task, so the
    listener keeps draining audio while the router waits on STT, the
    assistant and TTS. The command arrives through deliver() from the
    listener; /inject delivers it before starting.

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
        self.command: asyncio.Future = asyncio.get_running_loop().create_future()
        self.phase = "listening"
        self.ducked: list[Session] = []
        self.outcome: routing.Outcome | None = None
        self.task: asyncio.Task | None = None
        self._lit_key: int | None | str = "unset"
        self._lit_at = 0.0

    @property
    def live(self) -> bool:
        return not self.quiet and self.s is not None

    def start(self) -> None:
        self.task = self.hub.spawn(self.run(), name=f"conversation-{self.nid}")

    def deliver(self, command: listening.Command) -> None:
        if not self.command.done():
            self.command.set_result(command)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
        elif not self.command.done():
            self.command.cancel()

    async def point(self, direction: float | None, *, force: bool = False) -> None:
        """The ring, while listening: pointed at the talker, or a soft pulse
        until there is a direction. Sent only when the pointed LED changes."""
        s = self.s
        leds = s.caps.get("lights") if s is not None else None
        if not self.live or not isinstance(leds, int) or leds <= 0:
            return
        key = None if direction is None else round(direction / 360.0 * leds) % leds
        now = time.monotonic()
        # The listener calls this after every batch; until run() has lit the
        # ring for the first time (after the wake earcon), it waits its turn.
        if not force and (self._lit_key == "unset" or key == self._lit_key
                          or now - self._lit_at < LIGHTS_MIN_S):
            return
        self._lit_key, self._lit_at = key, now
        if direction is None:
            msg = {"mode": "pulse", "color": list(LISTEN_COLOUR), "brightness": 48}
        else:
            msg = {"mode": "pixels", "brightness": 96,
                   "pixels": ring(direction, leds, LISTEN_COLOUR)}
        await self.hub.send_lights(s, msg)

    def _reply_satellite(self) -> Session | None:
        """The other satellite a matching rule would answer on, so it can be
        ducked while the question is asked."""
        rule = routing.current().rules.match(self.nid, self.name, self.heard.wake_word)
        if rule is None or rule.reply_to in ("same", "none"):
            return None
        found = lookup_satellite(rule.reply_to)
        if found is None or found[0] == self.nid:
            return None
        other = self.hub.sessions.get(found[0])
        return other if other is not None and other.adopted else None

    async def _duck(self, s: Session) -> None:
        if s not in self.ducked:
            self.ducked.append(s)
            await self.hub.hold_duck(s)

    async def _unduck(self, s: Session) -> None:
        if s in self.ducked:
            self.ducked.remove(s)
            await self.hub.release_duck(s)

    async def run(self) -> dict:
        command: listening.Command | None = None
        played, note = False, None
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
            try:
                command = await asyncio.wait_for(self.command, COMMAND_WAIT_S)
            except TimeoutError:
                self.outcome = routing.Outcome(
                    error="no command: the microphone stopped sending audio")
            self.phase = "routing"
            if self.outcome is None:
                if not command.had_speech:
                    # Nothing to transcribe, and silence given to Whisper-style
                    # models comes back as invented text.
                    self.outcome = routing.Outcome(error="nothing was said after the wake word")
                else:
                    if self.live and self.s.lit:
                        await self.hub.send_lights(self.s, {"mode": "spin", "brightness": 40,
                                                            "color": list(LISTEN_COLOUR)})
                    self.outcome = await routing.current().handle(
                        self.nid, self.name, self.heard.wake_word, command.audio)
            self.phase = "replying"
            played, note = await self._reply(self.outcome)
        except asyncio.CancelledError:
            if self.outcome is None:
                self.outcome = routing.Outcome(error="cancelled")
            note = "cancelled"
        finally:
            for s in list(self.ducked):
                await self._unduck(s)
            if self.live and self.s.lit:
                await self.hub.send_lights(self.s, {"mode": "off"})
            if self.s is not None and self.s.conversation is self:
                self.s.conversation = None
            self.phase = "done"
        o = self.outcome
        event = {"type": "routed", "satellite": self.nid, "wake_word": self.heard.wake_word,
                 "rule_id": o.rule_id, "reply_to": o.reply_to, "error": o.error,
                 "transcript": o.transcript, "reply_text": o.reply_text,
                 "timings_ms": o.timings_ms, "endpoint": command.reason if command else None,
                 "command_s": round(command.seconds, 2) if command else None,
                 "played": played, "note": note} | ({"injected": True} if self.injected else {})
        self.hub.publish(event)
        return event

    async def _reply(self, o: routing.Outcome) -> tuple[bool, str | None]:
        if self.quiet:
            return False, "not played: ?play=1 was not given" if self.injected else None
        origin = self.s
        if o.error:
            await self.hub.earcon(origin, "error")
            return False, None
        if not o.reply_pcm48k or not o.reply_to:
            await self.hub.earcon(origin, "done")  # done, with nothing to say
            return False, None
        target = self.hub.sessions.get(o.reply_to)
        if target is None or not target.adopted:
            await self.hub.earcon(origin, "error")
            return False, f"{o.reply_to} is not connected"
        if not self.hub.config(target).get("speaker_enabled", True):
            return False, f"{o.reply_to} has its speaker off"
        # The answer is spoken over nothing: a reply still playing from an
        # earlier conversation is dropped (this is where a barge-in lands),
        # and the duck is lifted first, because it lowers hub audio and the
        # reply is hub audio.
        if target.flush_speaker():
            await _quietly(target.send_json({"type": "flush"}))
        await self._unduck(target)
        # Asked again: the two sends above wait on the socket, and a PATCH in
        # between flushed a queue that did not hold this reply yet. The
        # speaker loop would drop it anyway; this keeps the routed event from
        # saying it played.
        if not self.hub.speaker_allowed(target):
            return False, f"{o.reply_to} has its speaker off"
        pcm = o.reply_pcm48k
        if target.spk_rate != routing.SPEAKER_RATE:
            pcm = audio.resample(pcm, routing.SPEAKER_RATE, target.spk_rate)
        target.speaker.put_nowait(pcm)
        if origin is not None and target is not origin:
            await self.hub.earcon(origin, "done")
        return True, None


# ---- the rename from "nodes" ------------------------------------------------------

LEGACY_PREFIX, PREFIX = "NODES_", "SATELLITES_"


def legacy_settings(environ: dict[str, str], rules: list[routing.Rule]) -> list[str]:
    """A sentence for each setting that the rename (2026-09-25) left behind.

    Every NODES_* variable became SATELLITES_*, and the hub reads only the new
    names. A secret set in the app's settings under the old name is ignored
    without a word otherwise: MQTT simply switches off. So each one still set
    is named at start, with the name read now.

    Except the ones a routing rule names. rules.json stores every field, so a
    rule saved before the rename says "token_env": "NODES_HA_TOKEN" and reads
    exactly that; calling it unread would send the operator off to rename the
    one variable that works. What is worth saying about those is the opposite
    case: a rule naming a NODES_* variable that is not set, which is what
    renaming the secret and not the rule looks like."""
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
            out.append(f"routing rule {', '.join(repr(i) for i in ids)} reads {var}, which is not "
                       f"set: a secret renamed to {new} has to be renamed in the rule as well")
    return out


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
    # The page learns that a word finished downloading (or failed) from the
    # event stream, rather than by polling GET /satellites/wake-words.
    hub.voice = Voice(MODEL_DIR, FRONTEND, wakewords_config.Assignment.open(DATA_DIR, WAKE_WORDS),
                      on_change=lambda: hub.publish({"type": "wake_words",
                                                     "words": hub.voice.views()}))
    hub.http = httpx.AsyncClient(follow_redirects=False, timeout=10)
    routing.configure(routing.Router(routing.Rules(DATA_DIR), lookup=lookup_satellite))
    for problem in legacy_settings(dict(os.environ), routing.current().rules.rules):
        log.warning("%s", problem)
    hub.bridge = MqttBridge.from_env()
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
        EXECUTOR.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="voice-satellites", lifespan=lifespan)
errors.install_errors(app)
health.install_health(app, details=lambda: {
    "satellites": {"online": len(hub.sessions), "adopted": len(hub.store.satellites),
              "pending": sum(1 for s in hub.sessions.values() if not s.adopted)},
    "tts": TTS_URL or None,
    "voice": hub.voice.health(),
    "routing": {"rules": len(routing.current().rules.rules),
                "stt": routing.current().stt_url or None,
                "load_error": routing.current().rules.load_error},
    "mqtt": hub.bridge.health() if hub.bridge else None,
})
auth.install(app, "SATELLITES_API_KEYS")
# Before every /satellites/{nid} route below: FastAPI matches in registration
# order, and GET /satellites/{nid} would otherwise take "routing" for a
# satellite id.
app.include_router(routing.routes)


# ---- the device socket -------------------------------------------------------

# TWO PATHS, ONE HANDLER. The feature was called "nodes" until 2026-09-25, and
# a board in the field runs firmware that connects to /nodes/ws. Its next
# firmware arrives over that same socket, so a hub that stopped answering the
# old path would strand the board on the old image with USB as the only way
# back. Nothing about a connection depends on the path it came in on. The
# alias can go once no board reports firmware from before the rename.
SOCKET = "/satellites/ws"
LEGACY_SOCKET = "/nodes/ws"


@app.websocket(SOCKET)
@app.websocket(LEGACY_SOCKET)
async def satellite_socket(ws: WebSocket) -> None:
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        await ws.close(code=1008)
        return
    if first.get("type") != "hello" or not satellite_id(first.get("id", "")):
        await ws.close(code=1008)
        return

    s = Session(ws, first)
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
    try:
        await hub.greet(s)
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                data = msg["bytes"]
                if s.adopted and data and data[0] == FRAME_MIC:
                    pcm = data[HEADER:]
                    for q in list(s.taps):
                        q.put_nowait(pcm)
                    if s.listener is not None:
                        s.offer_mic(pcm)
            elif msg.get("text") is not None:
                await on_message(s, json.loads(msg["text"]))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        player.cancel()
        hub.stop_listening(s)
        if s.ota and s.ota.get("state") in ("requested", "started", "progress"):
            s.ota.update(state="failed", error="disconnected mid-transfer")
            hub.publish({"type": "ota", "satellite": s.id, "state": "failed",
                         "error": "disconnected mid-transfer"})
            log.warning("satellite %s disconnected during an update", s.id)
        if hub.sessions.get(s.id) is s:
            del hub.sessions[s.id]
        hub.seen[s.id] = {"model": s.model, "fw": s.fw, "last_seen": time.time()}
        hub.publish({"type": "offline", "satellite": s.id})
        log.info("satellite %s disconnected", s.id)


EARCON_MESSAGES = frozenset({"earcons", "earcon_next", "earcon_stored", "earcon_failed"})


async def on_message(s: Session, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "hello":  # sent again after adoption, with the new token
        s.update(msg)
        await hub.greet(s)
    elif kind == "status":
        s.status = {k: v for k, v in msg.items() if k != "type"}
        if s.adopted:
            # After a welcome that left out settings the hub did not know,
            # the firmware applies the rest and reports at once: this is where
            # the record gets them.
            hub.take_report(s, s.status)
        hub.publish({"type": "status", "satellite": s.id, "status": s.status})
        # Muted mid-command: nothing more will arrive, so stop waiting for it.
        if s.status.get("muted") and s.conversation and s.conversation.phase == "listening":
            s.conversation.cancel()
    elif kind == "button" and s.adopted:
        hub.publish({"type": "button", "satellite": s.id, "button": msg.get("button"),
                     "action": msg.get("action"), "held_ms": msg.get("held_ms")})
        if isinstance(msg.get("button"), str) and msg.get("action") in ("press", "release"):
            await hub.on_button(s, msg["button"], msg["action"], msg.get("held_ms"))
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
        hub.publish({"type": "ota", "satellite": s.id, "state": state, "pct": msg.get("pct"),
                     "version": msg.get("version"), "error": msg.get("error")})
        log.info("satellite %s ota %s %s", s.id, state, msg.get("error") or "")


# ---- satellites --------------------------------------------------------------


class AdoptBody(BaseModel):
    name: str = Field(default="", max_length=64)


# A button as the satellite names it, and what the hub does when it is pressed
# or released. A webhook URL may not carry a user and password: the mapping is
# returned by GET /satellites, as a rule is by GET /satellites/routing.
ButtonName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]
ButtonAction = Annotated[str, StringConstraints(
    pattern=r"^(ptt|stop|none|webhook:https?://[^\s/?#@]+(/\S*)?)$", max_length=500)]


class ConfigBody(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    volume: int | None = Field(default=None, ge=0, le=100)
    mic_gain_db: float | None = Field(default=None, ge=0, le=37.5)
    mic_enabled: bool | None = None
    speaker_enabled: bool | None = None
    local_volume_buttons: bool | None = None
    lights_enabled: bool | None = None
    # Replaces the whole mapping. {} maps nothing; the default is in store.py.
    buttons: dict[ButtonName, dict[Literal["press", "release"], ButtonAction]] | None = Field(
        default=None, max_length=16)

    @field_validator("buttons")
    @classmethod
    def _rec_is_the_mute(cls, v: dict | None) -> dict | None:
        # The firmware mutes on REC by itself, before the hub hears of it, so
        # anything mapped there would start with the microphone cut.
        if v and any(a != "none" for a in (v.get("rec") or {}).values()):
            raise ValueError("rec is the satellite's own privacy mute and cannot be mapped")
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
    # Only the shape here; what the names, thresholds and satellites may be is
    # wakewords_config.check's, which a loaded file goes through as well.
    # Fields GET adds ("state", "error") are ignored rather than refused, so
    # the page can send back what it was given.
    name: str = Field(max_length=64)
    threshold: float = wakewords_config.DEFAULT_THRESHOLD
    satellites: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        default_factory=lambda: [wakewords_config.EVERY], max_length=256)


class WakeWordsBody(BaseModel):
    words: list[WakeWordBody] = Field(max_length=wakewords_config.MAX_WORDS)


def _mqtt_satellite(nid: str) -> None:
    if hub.bridge is not None:
        hub.bridge.publish_satellite(hub.describe(nid))


async def mqtt_command(nid: str, change: dict) -> None:
    """A Home Assistant switch or slider: the same path as PATCH, so it is
    validated, saved and sent to the satellite in exactly one place."""
    await configure(nid, ConfigBody(**change))


@app.get("/satellites")
async def list_satellites() -> dict:
    ids = set(hub.store.satellites) | set(hub.sessions) | set(hub.seen)
    return {"satellites": sorted((hub.describe(n) for n in ids),
                            key=lambda d: (not d["adopted"], d["name"], d["id"]))}


@app.get("/satellites/events")
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


# Above GET /satellites/{nid}, which would otherwise take "wake-words" for a
# satellite id and answer 404.
@app.get("/satellites/wake-words")
async def get_wake_words() -> dict:
    return hub.voice.describe()


@app.put("/satellites/wake-words")
async def put_wake_words(body: WakeWordsBody) -> dict:
    """Replace every wake word and its satellites. Live at once: a threshold
    reaches every detector, a word taken off a satellite is no longer heard
    there, and a word named for the first time is fetched in the background
    and listened for when it is ready."""
    known = (set(hub.store.satellites) | set(hub.sessions) | set(hub.seen)
             | hub.voice.assignment.satellite_ids())
    try:
        words = wakewords_config.check([w.model_dump() for w in body.words],
                                       available=wakewords_config.available(hub.voice.model_dir),
                                       known=known)
    except ValueError as e:
        raise ApiError(422, str(e), code="invalid_wake_words") from None
    try:
        hub.voice.replace(words)
    except OSError as e:
        raise ApiError(500, f"could not write {wakewords_config.FILE}: {e}",
                       type_="server_error") from None
    log.info("wake words saved: %s", ", ".join(
        f"{w.name} at {w.threshold:g} on {'every satellite' if w.satellites == ['*'] else w.satellites}"
        for w in words) or "none")
    hub.spawn(hub.voice.reconcile(), name="wake-words")
    return hub.voice.describe()


@app.post("/satellites/wake-words/models")
async def upload_wake_word_model(request: Request,
                                 name: str = Query(..., max_length=64)) -> dict:
    """Add (or replace) a custom wake word: the .onnx as the raw body. It is
    checked to be an openWakeWord classifier before it is written, then
    offered in `available` like a built-in and assigned the same way. A
    replaced model is picked up by every satellite that listens for it."""
    data = await request.body()
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
    return hub.voice.describe()


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
    image = await request.body()
    if not image:
        raise ApiError(400, "empty body: send the .bin as the request body")
    if len(image) > MAX_FIRMWARE:
        raise ApiError(413, f"image is {len(image)} bytes; an OTA slot holds {MAX_FIRMWARE}")
    if image[0] != 0xE9:  # every ESP32 app image starts with this magic byte
        raise ApiError(400, "not an ESP32 application image (first byte is not 0xE9)")
    try:
        sig = signing.accept_upload(image, signature, FIRMWARE_KEY)
    except signing.SignatureError as e:
        raise ApiError(400, str(e), code="bad_signature") from None
    fw = hub.store.add_firmware(image, model, version, sig)
    log.info("firmware %s stored: %s %s, %d bytes, %s", fw.sha256[:12], model, version, fw.size,
             "signed" if sig else "unsigned")
    return vars(fw)


@app.delete("/satellites/firmware/{sha256}")
async def delete_firmware(sha256: str) -> Response:
    if not hub.store.delete_firmware(sha256):
        raise ApiError(404, "no such firmware")
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
async def get_satellite(nid: str) -> dict:
    return hub.describe(hub.resolve(nid))


@app.post("/satellites/{nid}/adopt")
async def adopt(nid: str, body: AdoptBody) -> dict:
    s = hub.session(nid, adopted=False)
    name = body.name or f"satellite-{s.id[-4:]}"
    token = hub.store.adopt(s.id, name, s.model, reported=s.reported())
    hub.seen.pop(s.id, None)
    await s.send_json({"type": "adopt", "token": token, "name": name})
    log.info("satellite %s adopted as %r", s.id, name)
    _mqtt_satellite(s.id)
    return hub.describe(s.id)


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


@app.patch("/satellites/{nid}")
async def configure(nid: str, body: ConfigBody) -> dict:
    nid = hub.resolve(nid)
    rec = hub.store.satellites.get(nid)
    if rec is None:
        raise ApiError(404, "no adopted satellite with that id")
    change = body.model_dump(exclude_none=True)
    if "name" in change:
        rec.name = change["name"]
    cfg = {k: v for k, v in change.items() if k in DEFAULT_CONFIG}
    was_dark = not rec.config.get("lights_enabled", True)
    rec.config.update(cfg)
    # Set here, so the hub has it now: sent to the satellite below, and not
    # taken from its next status.
    rec.unreported = [k for k in rec.unreported if k not in cfg]
    hub.store.save_satellites()
    s = hub.sessions.get(nid)
    if s is not None and s.adopted:
        to_satellite = satellite_config(cfg) | ({"name": rec.name} if "name" in change else {})
        if to_satellite:
            await s.send_json({"type": "config", **to_satellite})
        # A ring lit by a conversation that went dark mid-way was never put
        # out, because nothing may be sent to a dark satellite. Now it may.
        if was_dark and cfg.get("lights_enabled") and s.lit and s.conversation is None:
            await hub.send_lights(s, {"mode": "off"})
        # Off means off now, not after the reply in hand has played out: the
        # rest of it would still be streamed to a satellite that was just
        # told to be silent.
        if cfg.get("speaker_enabled") is False and s.flush_speaker():
            await s.send_json({"type": "flush"})
        if cfg.get("mic_enabled") is False and s.conversation and s.conversation.phase == "listening":
            s.conversation.cancel()
    _mqtt_satellite(nid)
    return hub.describe(nid)


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
    s = _speaker_on(hub.session(nid))
    s.speaker.put_nowait(audio.tone(body.frequency, body.seconds, s.spk_rate))
    return Response(status_code=204)


@app.post("/satellites/{nid}/say")
async def say(nid: str, body: SayBody) -> Response:
    s = _speaker_on(hub.session(nid))
    if not TTS_URL:
        raise ApiError(503, "SATELLITES_TTS_URL is not set, so there is no voice to speak with")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{TTS_URL}/v1/audio/speech", json={
            "model": "kokoro", "voice": body.voice or TTS_VOICE, "input": body.text,
            "response_format": "pcm"})
    if r.status_code != 200:
        raise ApiError(502, f"tts answered {r.status_code}: {r.text[:200]}")
    # Asked again: synthesis takes seconds, and the speaker may have been
    # turned off while it ran.
    _speaker_on(s)
    # Kokoro's pcm is 24 kHz mono s16le (voice_common.audio.SAMPLE_RATE).
    s.speaker.put_nowait(audio.resample(r.content, 24000, s.spk_rate))
    return Response(status_code=204)


@app.post("/satellites/{nid}/flush")
async def flush(nid: str) -> Response:
    """Drop the speaker audio queued and playing, and the conversation in
    progress: what the "stop" button does."""
    await hub.stop(hub.session(nid))
    return Response(status_code=204)


@app.get("/satellites/{nid}/listen")
async def listen(nid: str, seconds: float = Query(5, gt=0, le=60),
                 channel: int | None = Query(None, ge=0, le=7)) -> Response:
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


def _hear_clip(clip: np.ndarray, wake: wakeword.WakeWords | None,
               wake_word: str | None) -> tuple[listening.Heard | None, listening.Command | None]:
    """A clip through a fresh Ear, in the satellite's own 20 ms frames, then
    room noise until the endpointer ends the command. Runs in the thread
    pool."""
    ear = listening.Ear(channels=1, frontend=False, wake=wake)
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
    heard, command = await loop.run_in_executor(EXECUTOR, _hear_clip, clip, wake, wake_word)
    if heard is None:
        return {"satellite": nid, "heard": None, "command": None, "outcome": None, "played": False}
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
