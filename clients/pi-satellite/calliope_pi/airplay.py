"""AirPlay: Shairport Sync as the calliope user, playing through PipeWire.

The satellite is an AirPlay receiver under its own name, on the output the
hub chose, beside everything else it plays: PipeWire mixes them. The hub
turns it on and off and names it (airplay_enabled, airplay_name); the agent
writes Shairport Sync's configuration and runs it as a user service
(calliope-airplay.service), so no root is needed after install.sh.

This is classic AirPlay, from Debian's shairport-sync (4.3.7 in trixie, built
without AirPlay 2): iPhones, iPads and Macs list it; multi-room and the Home
app need AirPlay 2.

DUCKING. While the satellite speaks, or while the hub holds a duck (someone
is talking to it, or to a satellite that plays through it), every other
stream (AirPlay, and later Bluetooth) goes down to a fraction of its volume,
and back when it is over. Through pipewire-pulse's pactl, which reads and
sets each stream's own volume."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path

log = logging.getLogger("calliope.airplay")

UNIT = "calliope-airplay.service"
CONF = Path.home() / ".config" / "calliope" / "shairport-sync.conf"
# Shairport Sync writes what it plays here (its metadata pipe), in calliope's
# own runtime directory, where only calliope can read it.
PIPE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "calliope-airplay-metadata"
BINARY = "shairport-sync"   # how its stream is found in pactl, whatever the backend
# Run with the phone's volume in dB; sets the output's own volume (bundle/bin).
VOLUME_HOOK = "/opt/calliope/current/bin/calliope-airplay-volume"
OURS = "Assistant"        # media.role of the agent's own streams (pipewire.Player)


def available() -> bool:
    return shutil.which("shairport-sync") is not None


def _quote(text: str) -> str:
    """A libconfig string: backslashes and quotes escaped, controls dropped."""
    clean = "".join(ch for ch in text if ch.isprintable())
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def start_db(percent: int) -> float:
    """A position of the phone's AirPlay slider, 0-100 %, as the AirPlay
    volume Shairport Sync takes: -30 dB (the bottom) to 0 dB (the top)."""
    return round(-30.0 + max(0, min(100, int(percent))) * 0.3, 1)


def config(name: str, pipe: Path = PIPE, start_volume: int = 70) -> str:
    """Shairport Sync's configuration for this satellite."""
    return "\n".join([
        "// Written by the Calliope satellite agent (calliope_pi/airplay.py).",
        "general = {",
        f"  name = {_quote(name[:50] or 'Calliope')};",
        # BIT-PERFECT. The samples leave Shairport Sync as the phone sent
        # them, 16-bit at 44.1 kHz: it applies no volume of its own
        # (ignore_volume_control), and hands the phone's volume, in dB, to
        # calliope-airplay-volume, which sets the output's own volume (in the
        # DAC's hardware where it has a control). Through ALSA's default
        # device, which is PipeWire (pipewire-alsa).
        '  output_backend = "alsa";',
        '  ignore_volume_control = "yes";',
        f'  run_this_when_volume_is_set = "{VOLUME_HOOK} ";',
        # Timing corrections resampled with SoX, the best of its choices.
        '  interpolation = "soxr";',
        # Its own D-Bus and MPRIS interfaces on calliope's session bus, where
        # the agent asks whether it is playing (mpris_status).
        '  dbus_service_bus = "session";',
        '  mpris_service_bus = "session";',
        # A session that starts after a minute with nothing playing starts at
        # the hub's starting volume, not wherever the phone left it: any
        # volume counts as "high" (-30 dB and up), and the idle timeout is one
        # minute. A pause shorter than that keeps the volume the phone set.
        f"  default_airplay_volume = {start_db(start_volume)};",
        "  high_threshold_airplay_volume = -30.0;",
        "  high_volume_idle_timeout_in_minutes = 1;",
        "};",
        "alsa = {",
        '  output_device = "default";',
        '  output_format = "S16";',
        "  output_rate = 44100;",
        "};",
        "sessioncontrol = {",
        "  session_timeout = 20;",
        "};",
        "metadata = {",
        '  enabled = "yes";',
        '  include_cover_art = "yes";',
        f"  pipe_name = {_quote(str(pipe))};",
        "  pipe_timeout = 5000;",
        "};",
        ""])


async def _run(*argv: str, timeout: float = 15.0) -> tuple[int, str]:
    if shutil.which(argv[0]) is None:
        return 127, f"{argv[0]} is not installed"
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"{argv[0]} did not answer within {timeout:g} s"
    return proc.returncode or 0, out.decode(errors="replace")


class AirPlay:
    def __init__(self, conf: Path = CONF, pipe: Path = PIPE):
        self.conf, self.pipe = conf, pipe
        self.error: str | None = None

    async def apply(self, enabled: bool, name: str, start_volume: int = 70) -> None:
        """Running under `name` when enabled, stopped when not. Restarted
        only when its configuration changed, so a settings message that
        changes nothing else does not cut the music."""
        if not available():
            self.error = "shairport-sync is not installed"
            return
        if not enabled:
            await _run("systemctl", "--user", "disable", "--now", UNIT)
            self.error = None
            return
        text = config(name, self.pipe, start_volume)
        make_fifo(self.pipe)
        changed = not self.conf.exists() or self.conf.read_text() != text
        if changed:
            self.conf.parent.mkdir(parents=True, exist_ok=True)
            self.conf.write_text(text)
        code, out = await _run("systemctl", "--user", "enable", UNIT)
        verb = "restart" if changed else "start"
        code, out = await _run("systemctl", "--user", verb, UNIT)
        self.error = None if code == 0 else out.strip()[-200:] or f"systemctl {verb} failed"
        if changed:
            log.info("AirPlay as %r (%s)", name, "running" if code == 0 else self.error)

    async def running(self) -> bool:
        code, out = await _run("systemctl", "--user", "is-active", UNIT)
        return out.strip() == "active"


# ---- what is playing ---------------------------------------------------------------


def make_fifo(path: Path) -> None:
    """The metadata pipe, made before Shairport Sync starts so both agree on it."""
    try:
        if path.exists() and stat.S_ISFIFO(path.stat().st_mode):
            return
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path, 0o600)
    except OSError as e:
        log.warning("AirPlay metadata pipe %s: %s", path, e)


ITEM = re.compile(rb"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
                  rb"(?:\s*<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*)</data>)?\s*</item>")


def _word(hexed: bytes) -> str:
    try:
        return bytes.fromhex(hexed.decode()).decode("ascii", "replace")
    except ValueError:
        return "????"


# DMAP items (type "core") as text, and as big-endian integers.
CORE_TEXT = {"minm": "title", "asar": "artist", "asal": "album", "asaa": "album_artist", "asgn": "genre",
             "ascp": "composer", "asdt": "kind", "ascm": "comment", "asct": "category", "asfm": "format"}
CORE_INT = {"astm": "duration_ms", "astn": "track_number", "astc": "track_count", "asdn": "disc_number",
            "asdc": "disc_count", "asyr": "year", "asbr": "bitrate_kbps", "assr": "sample_rate",
            "caps": "play_status", "mper": "persistent_id", "asri": "artist_id", "asai": "album_id"}
# Shairport Sync's own items (type "ssnc") as text.
SSNC_TEXT = {"snam": "client_name", "snua": "client_agent", "clip": "client_ip", "svip": "server_ip",
             "cmod": "client_model", "cdid": "client_device_id", "cmac": "client_mac", "styp": "stream_type",
             "daid": "dacp_id", "ofmt": "output_format", "ofps": "output_rate", "sdsc": "stream_description"}
# Kept on the Pi only: with it, anyone on the network could control the phone.
PRIVATE = frozenset({"acre"})
RTP_RATE = 44100


def _decoded(kind: str, code: str, data: bytes):
    """An item's value as JSON can carry it: an integer, text, or for
    anything else its size and a base64 of its first bytes."""
    if kind == "core" and code in CORE_INT and 0 < len(data) <= 8:
        return int.from_bytes(data, "big")
    try:
        text = data.decode("utf-8")
        if text.isprintable() or not text:
            return text
    except UnicodeDecodeError:
        pass
    return {"bytes": len(data), "base64": base64.b64encode(data[:48]).decode()}


class Metadata:
    """Everything Shairport Sync says through its metadata pipe, kept for the
    page and for what comes later (Home Assistant, remote control): every
    item raw (`raw`, by "type/code"), and the parts a person reads decoded
    (`state`): the session, play and pause, the track and its source file,
    progress, the phone (name, model, address, app), its volume, and the
    cover art (kept on the Pi, named by its hash). The phone's remote-control
    token stays in `remote` and never leaves the Pi. `on_change` is called
    from the reading thread when something a person would see changed."""

    def __init__(self, pipe: Path = PIPE, on_change=None, art_dir: Path | None = None):
        self.pipe, self.on_change = pipe, on_change
        self.art_dir = art_dir or pipe.parent
        self.state: dict = {"session": False, "playing": False, "client": None, "title": None,
                            "artist": None, "album": None, "volume": None, "since": None,
                            "track": {}, "client_info": {}, "progress": None, "artwork": None}
        self.raw: dict = {}
        self.remote: dict = {}
        self._thread: threading.Thread | None = None
        self._picture: bytearray | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._read, name="airplay-metadata", daemon=True)
            self._thread.start()

    def _read(self) -> None:
        while True:
            try:
                with open(self.pipe, "rb", buffering=0) as f:   # waits for Shairport Sync
                    buf = b""
                    while chunk := f.read(65536):
                        buf = self.feed(buf + chunk)
            except OSError:
                time.sleep(2)

    def feed(self, buf: bytes) -> bytes:
        """Take every whole item in `buf`; what is left of it."""
        end = 0
        changed = False
        for m in ITEM.finditer(buf):
            end = m.end()
            data = base64.b64decode(b"".join(m.group(4).split())) if m.group(4) else b""
            changed |= self.take(_word(m.group(1)), _word(m.group(2)), data)
        if changed and self.on_change is not None:
            self.on_change()
        rest = buf[end:].lstrip()
        return rest[-8 * 1024 * 1024:]   # a cover picture can be a few hundred KB

    def take(self, kind: str, code: str, data: bytes) -> bool:
        s = self.state
        before = json.dumps(s, sort_keys=True, default=str)
        key = f"{kind}/{code}"
        if code in PRIVATE:
            self.remote[code] = data.decode("ascii", "replace")
        elif code != "PICT":
            self.raw[key] = {"value": _decoded(kind, code, data), "at": round(time.time(), 3)}
        text = data.decode("utf-8", "replace").strip() or None
        if kind == "ssnc":
            if code in ("abeg", "pbeg", "prsm"):
                s["session"], s["playing"] = True, True
                s["since"] = s["since"] or time.time()
            elif code == "pfls":
                s["playing"] = False
            elif code == "prgr" and text:
                # "start/current/end" RTP frames: sent as play starts, resumes or seeks.
                try:
                    a, b, c = (int(x) for x in text.split("/"))
                    s["progress"] = {"position_s": round(((b - a) % 2 ** 32) / RTP_RATE, 1),
                                     "duration_s": round(((c - a) % 2 ** 32) / RTP_RATE, 1),
                                     "at": time.time()}
                    s["session"], s["playing"] = True, True
                except ValueError:
                    pass
            elif code in ("pend", "aend"):
                s.update(session=code == "pend" and s["session"], playing=False, since=None, progress=None)
                if code == "aend":
                    s.update(client=None, title=None, artist=None, album=None, volume=None,
                             track={}, client_info={}, artwork=None)
                    self.remote.clear()
            elif code == "pvol" and text:
                try:
                    db = float(text.split(",")[0])
                    s["volume"] = 0 if db <= -30 else round((db + 30) / 30 * 100)
                except ValueError:
                    pass
            elif code == "mdst":
                s.update(title=None, artist=None, album=None, track={})
            elif code == "pcst":
                self._picture = bytearray()
            elif code == "PICT":
                self._artwork(data)
            elif code in SSNC_TEXT and text:
                s["client_info"] = s["client_info"] | {SSNC_TEXT[code]: text}
                if code == "snam":
                    s["client"] = text
        elif kind == "core":
            if code in CORE_TEXT:
                s["track"] = s["track"] | {CORE_TEXT[code]: text}
            elif code in CORE_INT and 0 < len(data) <= 8:
                value = int.from_bytes(data, "big")
                s["track"] = s["track"] | {CORE_INT[code]: value}
                if code == "caps":            # DAAP play status: 3 paused, 4 playing
                    s["playing"] = value == 4 if value in (3, 4) else s["playing"]
            s["title"], s["artist"], s["album"] = (s["track"].get("title"), s["track"].get("artist"),
                                                   s["track"].get("album"))
        return json.dumps(s, sort_keys=True, default=str) != before

    def _artwork(self, data: bytes) -> None:
        """The cover, saved beside the pipe by its hash, and named in the state."""
        if not data:
            self.state["artwork"] = None
            return
        kind = "jpeg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "bin"
        digest = __import__("hashlib").sha256(data).hexdigest()
        try:
            self.art_dir.mkdir(parents=True, exist_ok=True)
            (self.art_dir / "calliope-airplay-cover").write_bytes(data)
        except OSError:
            pass
        self.state["artwork"] = {"sha256": digest, "bytes": len(data), "type": kind}

    def view(self) -> dict:
        """What the status carries: the decoded state, and every raw item."""
        return {"raw": dict(self.raw), "remote_control": bool(self.remote)}


async def mpris_status() -> str | None:
    """Playing, Paused or Stopped, from Shairport Sync's own MPRIS interface
    on calliope's session bus: what the player itself says, where the
    metadata's events leave it unsure (a resume that sends no event)."""
    code, out = await _run("busctl", "--user", "--json=short", "get-property",
                           "org.mpris.MediaPlayer2.ShairportSync", "/org/mpris/MediaPlayer2",
                           "org.mpris.MediaPlayer2.Player", "PlaybackStatus", timeout=3.0)
    if code:
        return None
    try:
        return json.loads(out).get("data")
    except ValueError:
        return None


FORMAT = re.compile(r"^(\w+?)(\d+)?(le|be|ne)?\s+(\d+)ch\s+(\d+)Hz")


def stream(sink_inputs: list) -> dict | None:
    """The AirPlay stream as PipeWire has it: sample format, rate, channels,
    the PCM bit rate they make, whether it is paused (corked), and PipeWire's
    own delay. None while nothing plays."""
    for si in sink_inputs if isinstance(sink_inputs, list) else []:
        props = si.get("properties") or {}
        # Through the PulseAudio server it carries its process name; through
        # ALSA's PipeWire plugin only a node name made from it.
        if (props.get("application.process.binary") != BINARY
                and props.get("node.name") != f"alsa_playback.{BINARY}"
                and f"[{BINARY}]" not in str(props.get("application.name") or "")):
            continue
        spec = str(si.get("sample_specification") or "")
        m = FORMAT.match(spec)
        out = {"format": spec or None, "corked": bool(si.get("corked"))}
        if m:
            bits = int(m.group(2)) if m.group(2) else (32 if "float" in m.group(1) else 16)
            channels, rate = int(m.group(4)), int(m.group(5))
            out.update(bits=bits, channels=channels, rate=rate,
                       bitrate_kbps=round(rate * bits * channels / 1000))
        latency = (si.get("buffer_latency_usec") or 0) + (si.get("sink_latency_usec") or 0)
        if latency:
            out["latency_ms"] = round(latency / 1000)
        return out
    return None


# ---- ducking --------------------------------------------------------------------


def others(sink_inputs: list) -> list[dict]:
    """The streams that are not the agent's own: {index, volume (0-1 of the
    first channel), app}."""
    out = []
    for si in sink_inputs if isinstance(sink_inputs, list) else []:
        props = si.get("properties") or {}
        if props.get("media.role") == OURS:
            continue
        vol = (si.get("volume") or {})
        first = next(iter(vol.values()), {}) if isinstance(vol, dict) else {}
        try:
            fraction = int(first.get("value")) / 65536
        except (TypeError, ValueError, AttributeError):
            continue
        out.append({"index": si.get("index"), "volume": fraction,
                    "app": props.get("application.name") or props.get("media.name")})
    return out


async def sink_inputs() -> list:
    code, out = await _run("pactl", "-f", "json", "list", "sink-inputs")
    if code:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


class Ducker:
    """Lowers every other stream while `want` is set, and gives each back the
    volume it had. A stream that starts while ducked is lowered too."""

    def __init__(self) -> None:
        self.saved: dict[int, float] = {}
        self.level: float | None = None
        self._task: asyncio.Task | None = None

    async def _inputs(self) -> list:
        return await sink_inputs()

    async def set(self, level: float | None) -> None:
        """Duck to `level` (0-1 of each stream's own volume), or None to lift."""
        if level == self.level:
            return
        self.level = level
        if level is None:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            await self._restore()
        elif self._task is None or self._task.done():
            self._task = asyncio.create_task(self._hold())

    async def _hold(self) -> None:
        while self.level is not None:
            for s in others(await self._inputs()):
                idx = s["index"]
                if idx not in self.saved:
                    self.saved[idx] = s["volume"]
                    target = self.saved[idx] * self.level
                    await _run("pactl", "set-sink-input-volume", str(idx), f"{target:.3f}")
            await asyncio.sleep(0.5)

    async def _restore(self) -> None:
        saved, self.saved = self.saved, {}
        present = {s["index"] for s in others(await self._inputs())}
        for idx, volume in saved.items():
            if idx in present:
                await _run("pactl", "set-sink-input-volume", str(idx), f"{volume:.3f}")
