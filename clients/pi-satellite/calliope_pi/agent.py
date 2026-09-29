"""The satellite: one WebSocket to the hub, for as long as the board runs.

What it does, in the protocol's terms (services/satellites/README.md):

  hello      id (MAC), model "raspberry-pi", fw (its release), token, name,
             caps, settings, and `audio`: the outputs and inputs PipeWire has
  pending /  waits to be adopted; keeps the token it is given, says hello
  adopt      again with it
  welcome /  applies the settings: output, microphone, volumes; confirms a
  config     new release (bundle.commit) once the hub has welcomed it
  speaker    plays the frames to the chosen output (pipewire.Player)
  mic        streams [output monitor, microphone] at 16 kHz while it has a
             microphone and it is enabled (pipewire.Recorder)
  earcons    keeps them under /var/lib/calliope/earcons and plays them
  ota        receives a signed bundle and has calliope-root install it
  status     every 10 s: uptime, Wi-Fi, free memory, temperature, throttling,
             dropped audio, the devices

A microphone plugged in or pulled out changes what the satellite can do, so
the agent reconnects and says hello with the new caps."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import airplay, bundle, frames, paths, pipewire, system, version
from .state import State, load, save

log = logging.getLogger("calliope.agent")

PATH = "/satellites/ws"
STATUS_S = 10.0
DEVICES_S = 10.0
BACKOFF = (1, 2, 5, 10, 20, 30)
EARCON_MAX, EARCON_MAX_BYTES, EARCON_RATE = 16, 512 * 1024, 48000
ROOT = ["sudo", "-n", "/usr/local/sbin/calliope-root"]
SPEAK_DUCK = 0.2       # other streams, while the satellite itself speaks
ARTWORK_MAX = 2 * 1024 * 1024


def socket_url(hub: str) -> str:
    """wss://host (or with a path already) as the socket's URL."""
    parts = urlsplit(hub.strip())
    path = parts.path if parts.path not in ("", "/") else PATH
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _ssl_for(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    extra = Path("/etc/calliope/hub-ca.pem")
    if extra.exists():
        ctx.load_verify_locations(extra)
    return ctx


async def _connect(url: str):
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # websockets before 13
        from websockets import connect
    return await connect(url, ssl=_ssl_for(url), max_size=2 ** 22, open_timeout=15,
                         ping_interval=20, ping_timeout=20)


class Earcons:
    """Sounds the hub stores here, one raw file each, with an index."""

    def __init__(self) -> None:
        self.dir = paths.earcons()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.dir / "index.json"
        try:
            self.index = json.loads(self.index_file.read_text())
        except (OSError, ValueError):
            self.index = {}
        self.receiving: dict | None = None

    def items(self) -> list[dict]:
        return [{"id": k, "size": v["size"], "sha256": v["sha256"]} for k, v in sorted(self.index.items())
                if (self.dir / f"{k}.pcm").exists()]

    def begin(self, eid: str, size: int, sha256: str) -> str | None:
        if not eid.isidentifier() or not 0 < size <= EARCON_MAX_BYTES:
            return "bad id or size"
        if eid not in self.index and len(self.index) >= EARCON_MAX:
            return "no room"
        self.receiving = {"id": eid, "size": size, "sha256": sha256, "data": bytearray()}
        return None

    def take(self, offset: int, chunk: bytes) -> tuple[str, dict] | None:
        """A chunk; what to send next: ("next", msg), ("stored", msg) or ("failed", msg)."""
        r = self.receiving
        if r is None or offset != len(r["data"]):
            return None
        r["data"] += chunk[:r["size"] - len(r["data"])]
        if len(r["data"]) < r["size"]:
            return "next", {"type": "earcon_next", "id": r["id"], "offset": len(r["data"])}
        self.receiving = None
        digest = hashlib.sha256(r["data"]).hexdigest()
        if digest != r["sha256"]:
            return "failed", {"type": "earcon_failed", "op": "put", "id": r["id"], "error": "sha256 mismatch"}
        (self.dir / f"{r['id']}.pcm").write_bytes(r["data"])
        self.index[r["id"]] = {"size": r["size"], "sha256": digest}
        self.index_file.write_text(json.dumps(self.index))
        return "stored", {"type": "earcon_stored", "id": r["id"], "size": r["size"], "sha256": digest}

    def pcm(self, eid: str) -> bytes | None:
        try:
            return (self.dir / f"{eid}.pcm").read_bytes() if eid in self.index else None
        except OSError:
            return None


class Update:
    """One bundle arriving over the socket, a chunk per ota_next."""

    def __init__(self, msg: dict):
        self.size = int(msg.get("size") or 0)
        self.sha256 = str(msg.get("sha256") or "")
        self.version = str(msg.get("version") or "")
        self.signature = msg.get("signature")
        self.data = bytearray()
        self.hash = hashlib.sha256()
        self.last_pct = -1

    def take(self, offset: int, chunk: bytes) -> bool:
        if offset != len(self.data):
            return False
        chunk = chunk[:self.size - len(self.data)]
        self.data += chunk
        self.hash.update(chunk)
        return True

    @property
    def done(self) -> bool:
        return len(self.data) >= self.size

    def pct(self) -> int:
        return int(len(self.data) * 100 / self.size) if self.size else 100


class Agent:
    def __init__(self, st: State | None = None):
        self.st = st or load()
        self.id = system.satellite_id()
        self.clock = system.Clock()
        self.devices = pipewire.NONE
        self.player = pipewire.Player()
        self.earcons = Earcons()
        self.ws = None
        self._send_lock = asyncio.Lock()
        self.mic_caps: tuple[int, ...] | None = None   # what the last hello said about the mic
        self.recording: asyncio.Task | None = None
        self.recorder: pipewire.Recorder | None = None
        self.update: Update | None = None
        self.mic_dropped = 0
        self.seq = 0
        self.duck: int | None = None
        self.welcomed = False
        self.airplay = airplay.AirPlay()
        self.ducker = airplay.Ducker()
        self.metadata = airplay.Metadata(on_change=self._airplay_changed)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._pushing: asyncio.Task | None = None
        self.speaking = False
        self.player.on_active = self._speaking
        self._art_sent: str | None = None
        self.airplay_state: dict | None = None
        self.playing_at: dict | None = None

    # -- sending --

    async def send(self, msg: dict | bytes) -> None:
        ws = self.ws
        if ws is None:
            return
        async with self._send_lock:
            await ws.send(msg if isinstance(msg, bytes) else json.dumps(msg))

    def mic_channels(self) -> int | None:
        """What the microphone stream would be: None without one."""
        if not self.devices.sources:
            return None
        return 2 if self.st.config.get("echo_reference", True) else 1

    def caps(self) -> dict:
        caps: dict = {"speaker": {"rate": pipewire.SPEAKER_RATE, "channels": 1, "format": "s16le"},
                      "earcons": {"max": EARCON_MAX, "max_bytes": EARCON_MAX_BYTES, "rate": EARCON_RATE},
                      "duck": True, "audio_devices": True, "bundle": "tar.gz"}
        ch = self.mic_channels()
        if ch:
            caps["mic"] = {"rate": pipewire.MIC_RATE, "channels": ch, "format": "s16le",
                           "reference": ch == 2}
        key = bundle.key_id()
        if key:
            caps["ota_key"] = key
        if airplay.available():
            caps["airplay"] = {"version": 1}
        return caps

    def settings(self) -> dict:
        c = self.st.config
        return {k: c.get(k) for k in ("volume", "mic_gain_db", "mic_enabled", "speaker_enabled",
                                      "audio_sink", "audio_source", "echo_reference",
                                      "airplay_enabled", "airplay_name", "airplay_volume")}

    def airplay_name(self) -> str:
        return self.st.config.get("airplay_name") or self.st.name or f"calliope-sat-{self.id[-4:]}"

    def hello(self) -> dict:
        rolled = None
        with contextlib.suppress(OSError, ValueError):
            rolled = json.loads((paths.STATE / "rolled_back.json").read_text())
        return {"type": "hello", "id": self.id, "model": bundle.MODEL, "fw": version(),
                "token": self.st.token, "name": self.st.name, "board": system.board(),
                "ota_pending": bundle.pending() is not None, "rolled_back": rolled,
                "caps": self.caps(), "audio": self.devices.view(), **self.settings()}

    def status(self) -> dict:
        return {"type": "status", "uptime_s": system.uptime_s(), "rssi": system.rssi(),
                "heap": system.memory_available(), "temp_c": system.temperature_c(),
                "throttled": system.throttled(), "under_voltage": system.under_voltage(),
                "load": round(os.getloadavg()[0], 2),
                "muted": False, "mic_dropped": self.mic_dropped, "spk_dropped": self.player.dropped,
                "spk_buffered_ms": self.player.buffered_ms(), "duck": self.duck, "earcons_ready": True,
                "audio": self.devices.view() | {"playing_at": self.playing_at},
                "airplay": self.airplay_state, **self.settings()}

    # -- settings --

    async def apply(self) -> None:
        """Make the output, the microphone and their volumes what the
        settings say, where the devices exist."""
        c = self.st.config
        sink = self.devices.sink(c.get("audio_sink"))
        if sink is not None and self.devices.default_sink != sink.name:
            await pipewire.set_default(sink)
        await pipewire.set_volume(sink, (c.get("volume") or 0) / 100)
        source = self.devices.source(c.get("audio_source"))
        if source is not None and self.devices.default_source != source.name:
            await pipewire.set_default(source)
        if self.devices.sources:
            await pipewire.set_source_volume(source, 10 ** (float(c.get("mic_gain_db") or 0) / 20))
        self.player.target = sink.name if sink else None
        await self.airplay.apply(bool(c.get("airplay_enabled", True)), self.airplay_name(),
                                 int(c.get("airplay_volume") if c.get("airplay_volume") is not None else 70))
        await self.refresh_recording()

    # -- ducking: other streams go down while the satellite speaks, or while the hub asks --

    async def _speaking(self, active: bool) -> None:
        self.speaking = active
        await self._duck_now()

    async def _duck_now(self) -> None:
        level = SPEAK_DUCK if self.speaking else (self.duck / 100 if self.duck is not None else None)
        await self.ducker.set(level)

    async def refresh_recording(self) -> None:
        c = self.st.config
        want = (self.st.adopted() and self.welcomed and bool(self.devices.sources)
                and c.get("mic_enabled", True) and self.mic_caps is not None)
        source = c.get("audio_source") if self.devices.source(c.get("audio_source")) else None
        sink = c.get("audio_sink") if self.devices.sink(c.get("audio_sink")) else None
        running = self.recording is not None and not self.recording.done()
        same = running and self.recorder is not None and \
            (self.recorder.source, self.recorder.sink) == (source, sink)
        if want and same:
            return
        if running:
            self.recording.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.recording
            self.recording = None
        if want:
            self.recorder = pipewire.Recorder(source, sink, reference=self.mic_caps[0] == 2)
            self.recording = asyncio.create_task(self._stream_mic(self.recorder))

    async def _stream_mic(self, rec: pipewire.Recorder) -> None:
        async for pcm in rec.frames():
            self.seq += 1
            try:
                await self.send(frames.mic(self.seq, rec.channels, self.clock.us(), pcm))
            except Exception:
                self.mic_dropped += 1
                return

    # -- the hub's messages --

    async def on_text(self, msg: dict) -> str | None:
        """Handle one message; "reconnect" when the connection must start again."""
        kind = msg.get("type")
        if kind == "pending":
            log.info("waiting to be adopted on the Satellites tab (id %s)", self.id)
        elif kind == "adopt":
            self.st.token, self.st.name = str(msg.get("token") or ""), str(msg.get("name") or "")
            save(self.st)
            log.info("adopted as %s", self.st.name)
            await self.send(self.hello())
        elif kind == "welcome":
            self.welcomed = True
            if msg.get("name"):
                self.st.name = str(msg["name"])
            self.st.apply(msg.get("config") or {})
            save(self.st)
            committed = bundle.commit()
            if committed:
                log.info("release %s met the hub: it stays", committed)
                await self.send({"type": "ota", "state": "verified", "version": committed})
            with contextlib.suppress(FileNotFoundError):
                (paths.STATE / "rolled_back.json").unlink()
            await self.apply()
            await self.send(self.status())
            # The hub keeps a cover in memory only: a new connection sends it again.
            self._art_sent = None
            await self._send_artwork()
        elif kind == "config":
            if self.st.apply(msg):
                save(self.st)
            await self.apply()
            await self.send(self.status())
        elif kind == "flush":
            await self.player.flush()
        elif kind == "identify":
            pcm = self.earcons.pcm("wake")
            if pcm and self.st.config.get("speaker_enabled", True):
                for _ in range(3):
                    await self.player.play_once(pcm, EARCON_RATE)
        elif kind == "reboot":
            await self._root("reboot")
        elif kind == "forget":
            self.st.token, self.st.name = "", ""
            save(self.st)
            return "reconnect"
        elif kind == "set_hub":
            url = str(msg.get("url") or "")
            if url.startswith(("ws://", "wss://")):
                self.st.hub = url
                save(self.st)
                return "reconnect"
        elif kind == "earcon_list":
            await self.send({"type": "earcons", "ready": True, "items": self.earcons.items(),
                             "last_load_us": 0})
        elif kind == "earcon_put":
            why = self.earcons.begin(str(msg.get("id") or ""), int(msg.get("size") or 0),
                                     str(msg.get("sha256") or ""))
            if why:
                await self.send({"type": "earcon_failed", "op": "put", "id": msg.get("id"), "error": why})
            else:
                await self.send({"type": "earcon_next", "id": msg.get("id"), "offset": 0})
        elif kind == "earcon":
            pcm = self.earcons.pcm(str(msg.get("id") or ""))
            if pcm and self.st.config.get("speaker_enabled", True):
                asyncio.create_task(self.player.play_once(pcm, EARCON_RATE))
        elif kind == "duck":
            self.duck = max(0, min(100, int(msg.get("level") or 0)))
            await self._duck_now()
        elif kind == "unduck":
            self.duck = None
            await self._duck_now()
        elif kind == "ota":
            await self.begin_update(msg)
        return None

    async def on_binary(self, data: bytes) -> None:
        f = frames.parse(data)
        if f is None:
            return
        if f.kind == frames.SPEAKER:
            if self.st.config.get("speaker_enabled", True):
                await self.player.write(f.payload)
        elif f.kind == frames.EARCON:
            out = self.earcons.take(f.offset, f.payload)
            if out is not None:
                await self.send(out[1])
        elif f.kind == frames.FIRMWARE and self.update is not None:
            await self.take_update(f.offset, f.payload)

    # -- updates --

    async def begin_update(self, msg: dict) -> None:
        up = Update(msg)
        if not bundle.VERSION.match(up.version) or not 0 < up.size <= bundle.MAX_BYTES:
            await self.send({"type": "ota", "state": "failed", "version": up.version,
                             "error": "bad version or size"})
            return
        if not up.signature:
            await self.send({"type": "ota", "state": "failed", "version": up.version,
                             "error": "unsigned image"})
            return
        self.update = up
        await self.send({"type": "ota", "state": "started", "version": up.version, "pct": 0})
        await self.send({"type": "ota_next", "offset": 0})

    async def take_update(self, offset: int, chunk: bytes) -> None:
        up = self.update
        if not up.take(offset, chunk):
            return
        if not up.done:
            if up.pct() // 10 != up.last_pct // 10:
                up.last_pct = up.pct()
                await self.send({"type": "ota", "state": "progress", "version": up.version, "pct": up.pct()})
            await self.send({"type": "ota_next", "offset": len(up.data)})
            return
        self.update = None
        if up.hash.hexdigest() != up.sha256:
            await self.send({"type": "ota", "state": "failed", "version": up.version,
                             "error": "sha256 mismatch"})
            return
        # The agent checks too, so a bad signature is said at once; calliope-root
        # checks again for itself before it installs anything.
        try:
            bundle.verify(bytes(up.data), up.signature)
            bundle.manifest(bytes(up.data))
        except bundle.BundleError as e:
            await self.send({"type": "ota", "state": "failed", "version": up.version, "error": str(e)})
            return
        paths.incoming().mkdir(parents=True, exist_ok=True)
        file = paths.incoming() / f"{up.sha256}.bundle"
        file.write_bytes(up.data)
        await self.send({"type": "ota", "state": "progress", "version": up.version, "pct": 100})
        # install.sh may install packages for minutes: the socket keeps being read meanwhile.
        asyncio.create_task(self._install(up, file))

    async def _install(self, up: Update, file: Path) -> None:
        code, out = await self._root("install", str(file), str(up.signature), timeout=1800)
        with contextlib.suppress(OSError):
            file.unlink()
        if code != 0:
            try:
                why = json.loads(out.strip().splitlines()[-1]).get("error")
            except (ValueError, IndexError, AttributeError):
                why = out.strip()[-200:] or f"calliope-root exited {code}"
            await self.send({"type": "ota", "state": "failed", "version": up.version, "error": why})
            return
        await self.send({"type": "ota", "state": "rebooting", "version": up.version})
        await self._root("restart-agent")

    async def _root(self, *args: str, timeout: float = 60) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(*ROOT, *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            return 124, "calliope-root did not finish in time"
        return proc.returncode or 0, out.decode(errors="replace")

    # -- the connection --

    async def session(self, url: str) -> None:
        self.welcomed = False
        self.devices = await pipewire.devices()
        ch = self.mic_channels()
        self.mic_caps = (ch,) if ch else None
        self.ws = await _connect(url)
        log.info("connected to %s", url)
        ticker = asyncio.create_task(self._ticker())
        try:
            await self.send(self.hello())
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    await self.on_binary(msg)
                    continue
                try:
                    parsed = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and await self.on_text(parsed) == "reconnect":
                    return
        finally:
            ticker.cancel()
            if self.recording is not None:
                self.recording.cancel()
                self.recording = None
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None
            self.update = None

    async def _ticker(self) -> None:
        """Status every STATUS_S; a microphone that came or went reconnects."""
        while True:
            await asyncio.sleep(STATUS_S)
            self.devices = await pipewire.devices()
            ch = self.mic_channels()
            if ((ch,) if ch else None) != self.mic_caps:
                log.info("the microphone %s: saying hello again", "appeared" if ch else "went away")
                with contextlib.suppress(Exception):
                    await self.ws.close()
                return
            self.airplay_state = await self._airplay_state()
            self.playing_at = pipewire.output_format(await pipewire.sinks(), self.devices.default_sink)
            with contextlib.suppress(Exception):
                await self.send(self.status())
            if self.welcomed:
                await self.refresh_recording()

    async def _airplay_state(self) -> dict | None:
        """What the page shows under AirPlay: on or off, its name, whether it
        runs, what it plays and from whom (its metadata), and the stream as
        PipeWire has it (format, rate, bit rate, paused)."""
        if not airplay.available():
            return None
        meta = dict(self.metadata.state)
        stream = airplay.stream(await airplay.sink_inputs())
        if meta["session"] and not meta["title"]:
            # Started in the middle of a track: MPRIS has what the pipe said before.
            md = await airplay.mpris_metadata()
            meta.update({k: md[k] for k in ("title", "artist", "album") if md.get(k)})
        running = await self.airplay.running()
        mpris = await airplay.mpris_status() if running else None
        # The player's own word first; the metadata's events where it has none.
        playing = mpris == "Playing" if mpris else bool(meta["playing"] and stream and not stream.get("corked"))
        progress = dict(meta["progress"]) if meta.get("progress") else None
        if progress and playing:   # carried on from when the phone last said
            progress["position_s"] = round(min(progress["duration_s"] or 1e9,
                                               progress["position_s"] + time.time() - progress.pop("at")), 1)
        elif progress:
            progress.pop("at", None)
        return {"enabled": bool(self.st.config.get("airplay_enabled", True)), "name": self.airplay_name(),
                "running": running, "error": self.airplay.error, "player": mpris,
                "playing": playing, "session": meta["session"] or mpris in ("Playing", "Paused"),
                "client": meta["client"], "title": meta["title"], "artist": meta["artist"],
                "album": meta["album"], "volume": meta["volume"], "since": meta["since"],
                "track": meta["track"], "client_info": meta["client_info"], "progress": progress,
                "artwork": meta["artwork"], "stream": stream, **self.metadata.view()}

    def _airplay_changed(self) -> None:
        """From the metadata thread: send a status now, not at the next tick,
        so the page says Playing when the music starts."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._push_status)

    def _push_status(self) -> None:
        if self._pushing is not None and not self._pushing.done():
            return

        async def push() -> None:
            await asyncio.sleep(0.4)   # a burst of items (a new track) is one status
            if self.metadata.new_session:
                self.metadata.new_session = False
                await self._starting_volume()
            self.airplay_state = await self._airplay_state()
            with contextlib.suppress(Exception):
                await self.send(self.status())
                await self._send_artwork()
        self._pushing = asyncio.create_task(push())

    async def _starting_volume(self) -> None:
        """A phone just connected: its slider goes to the starting volume
        (DACP, through Shairport Sync), and it sends that volume back. A
        phone that takes no remote control gets the output set directly."""
        percent = self.st.config.get("airplay_volume")
        percent = 70 if percent is None else int(percent)
        await asyncio.sleep(1.0)   # let the phone finish connecting
        if await airplay.set_phone_volume(percent):
            log.info("AirPlay: the phone's volume set to %d%%", percent)
            return
        proc = await asyncio.create_subprocess_exec(
            airplay.VOLUME_HOOK, str(airplay.VOLUME_RANGE_DB), f"{airplay.start_db(percent):.1f}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

    async def _send_artwork(self) -> None:
        """The cover of what plays, to the hub, once per picture: the page
        shows it (GET /satellites/{id}/airplay/artwork)."""
        art = (self.airplay_state or {}).get("artwork")
        if not art or art.get("sha256") == self._art_sent:
            return
        try:
            data = self.metadata.art_file.read_bytes()
        except OSError:
            return
        if len(data) > ARTWORK_MAX:
            return
        await self.send({"type": "artwork", "sha256": art["sha256"], "format": art.get("type"),
                         "data": base64.b64encode(data).decode()})
        self._art_sent = art["sha256"]

    async def _devices_changed(self) -> None:
        """A plug went in or out, or a card came or went: the page is told
        now (debounced, as pactl reports one change as several events)."""
        if self._pushing is not None and not self._pushing.done():
            return

        async def push() -> None:
            await asyncio.sleep(0.5)
            before = {d["name"]: d.get("jack") for d in self.devices.view()["sinks"] + self.devices.view()["sources"]}
            self.devices = await pipewire.devices()
            after = {d["name"]: d.get("jack") for d in self.devices.view()["sinks"] + self.devices.view()["sources"]}
            if after != before:
                self.playing_at = pipewire.output_format(await pipewire.sinks(), self.devices.default_sink)
                with contextlib.suppress(Exception):
                    await self.send(self.status())
        self._pushing = asyncio.create_task(push())

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        if airplay.available():
            self.metadata.start()
        self._watcher = asyncio.create_task(pipewire.watch(self._devices_changed))
        tries = 0
        while True:
            if not self.st.hub:
                log.error("no hub address: set it on the Wi-Fi setup page; checking again in 30 s")
                await asyncio.sleep(30)
                self.st = load()
                continue
            url = socket_url(self.st.hub)
            started = time.monotonic()
            try:
                await self.session(url)
            except Exception as e:  # any failure is a reconnect, never an exit
                log.warning("hub %s: %s: %s", url, type(e).__name__, str(e)[:200])
            tries = 0 if time.monotonic() - started > 60 else tries + 1
            await asyncio.sleep(BACKOFF[min(tries, len(BACKOFF) - 1)])


def main() -> int:
    logging.basicConfig(level=os.environ.get("CALLIOPE_LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    log.info("calliope satellite %s starting", version())
    try:
        asyncio.run(Agent().run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
