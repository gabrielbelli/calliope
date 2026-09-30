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
sets each stream's own volume.

REMOTE CONTROL. Play, pause, next and the rest go to the phone as DACP
requests, through Shairport Sync's own D-Bus call RemoteCommand: the only one
that hands back the phone's answer (the MPRIS methods throw it away). The
phone's app decides whether it acts on a request it accepted, so the agent
watches MPRIS afterwards to say whether it did (agent._confirm). Disconnect
is Shairport Sync's DropSession, which needs nothing from the phone.

THE COVER comes through the metadata pipe; a picture that went by while no
agent read the pipe (the agent restarted mid-track) is found again in
Shairport Sync's own cover cache (cached_cover), private to calliope."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("calliope.airplay")

UNIT = "calliope-airplay.service"
CONF = Path.home() / ".config" / "calliope" / "shairport-sync.conf"
# Shairport Sync writes what it plays here (its metadata pipe), in calliope's
# own runtime directory, where only calliope can read it.
PIPE = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "calliope-airplay-metadata"
BINARY = "shairport-sync"   # how its stream is found in pactl, whatever the backend
# Run with the phone's volume in dB; sets the output's own volume (bundle/bin).
VOLUME_HOOK = "/opt/calliope/current/bin/calliope-airplay-volume"
# The phone's 30 dB slider spread over this many dB of the output's volume,
# as Shairport Sync's own volume control does: its first step is quiet.
VOLUME_RANGE_DB = 60
OURS = "Assistant"        # media.role of the agent's own streams (pipewire.Player)
ARTWORK_MAX = 2 * 1024 * 1024   # the largest cover sent to the hub, or taken from the cache
# Shairport Sync keeps each cover here too, as cover-<md5>.<jpg|png>: beside
# the pipe, private to calliope (0700), and gone at reboot with the rest of
# the runtime directory.
COVERS = PIPE.parent / "calliope-airplay-covers"

# The hub's commands (airplay_command), as DACP names them.
COMMANDS = {"play": "play", "pause": "pause", "play_pause": "playpause", "next": "nextitem",
            "previous": "previtem", "stop": "stop"}
CONTROLS = (*COMMANDS, "disconnect")
# RemoteCommand's own answers where the phone gave none: what each means.
SHAIRPORT_CODES = {490: "no DACP port known yet", 491: "the phone refused the connection",
                   492: "argument out of range", 493: "failed to send", 494: "busy",
                   495: "receive error", 496: "cannot connect", 497: "cannot open a socket",
                   498: "bad address"}
BUSY = 494        # the request met Shairport Sync's own once-a-second probe of the phone
DBUS_NAME = "org.gnome.ShairportSync"
DBUS_PATH = "/org/gnome/ShairportSync"


def available() -> bool:
    return shutil.which("shairport-sync") is not None


def _quote(text: str) -> str:
    """A libconfig string: backslashes and quotes escaped, controls dropped."""
    clean = "".join(ch for ch in text if ch.isprintable())
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config(name: str, pipe: Path = PIPE, covers: Path = COVERS) -> str:
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
        f'  run_this_when_volume_is_set = "{VOLUME_HOOK} {VOLUME_RANGE_DB} ";',
        # Timing corrections resampled with SoX, the best of its choices.
        '  interpolation = "soxr";',
        # Its own D-Bus and MPRIS interfaces on calliope's session bus, where
        # the agent asks whether it is playing (mpris_status).
        '  dbus_service_bus = "session";',
        '  mpris_service_bus = "session";',
        "};",
        "alsa = {",
        '  output_device = "default";',
        # The phone's own samples, as it sent them: 16 bits.
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
        # Shairport Sync names each picture cover-<md5>.<jpg|png> here, and
        # MPRIS's artUrl points at it: where the agent finds the cover again
        # after a restart (cached_cover). Private (0700, AirPlay.apply) and
        # emptied at reboot, unlike its default in /tmp, which any user can
        # write to. Adding the line changes the configuration's text, so
        # Shairport Sync restarts once on the release that adds it.
        f"  cover_art_cache_directory = {_quote(str(covers))};",
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
    except asyncio.CancelledError:
        # The caller stopped waiting (agent._confirm's deadline): nothing is
        # left running behind it.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace")


class AirPlay:
    def __init__(self, conf: Path = CONF, pipe: Path = PIPE, covers: Path = COVERS):
        self.conf, self.pipe, self.covers = conf, pipe, covers
        self.error: str | None = None

    async def apply(self, enabled: bool, name: str) -> None:
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
        text = config(name, self.pipe, self.covers)
        make_fifo(self.pipe)
        # Made before Shairport Sync starts, which would otherwise make it
        # itself with its umask cleared, open to every user.
        try:
            self.covers.mkdir(parents=True, exist_ok=True)
            self.covers.chmod(0o700)
        except OSError as e:
            log.warning("AirPlay cover cache %s: %s", self.covers, e)
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


COVER_NAME = re.compile(r"^cover-([0-9a-f]{32})\.(jpg|png)$")


def cached_cover(art_url: str, cache_dir: Path = COVERS) -> bytes | None:
    """The picture MPRIS's artUrl names, from Shairport Sync's cover cache,
    or None. Only a file:// URL straight into the cache, named as Shairport
    Sync names a cover, a regular file no larger than ARTWORK_MAX, whose MD5
    is its name: a file half written, or put there by anything else, is
    refused."""
    try:
        url = urlsplit(art_url)
    except ValueError:
        return None
    if url.scheme != "file":
        return None
    path = Path(unquote(url.path))
    m = COVER_NAME.match(path.name)
    if m is None:
        return None
    try:
        if path.parent.resolve() != cache_dir.resolve():
            return None
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > ARTWORK_MAX:
            return None
        data = path.read_bytes()
    except (OSError, ValueError):   # ValueError: a NUL in the path, which no file has
        return None
    if len(data) > ARTWORK_MAX or hashlib.md5(data, usedforsecurity=False).hexdigest() != m.group(1):
        return None
    return data


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
# Never kept, not even on the Pi: with it, anyone on the network could
# control the phone. Shairport Sync holds its own copy, which RemoteCommand uses.
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
    page and Home Assistant: every item raw (`raw`, by "type/code"), and the
    parts a person reads decoded (`state`): the session, play and pause, the
    track and its source file, progress, the phone (name, model, address,
    app), its volume, and the cover art (kept on the Pi, named by its hash).
    The phone's remote-control token is never kept at all (PRIVATE).
    `pictured` says whether this session has sent a picture, even an empty
    one, so a cover from the cache never replaces the pipe's word.
    `on_change` is called from the reading thread when something a person
    would see changed."""

    def __init__(self, pipe: Path = PIPE, on_change=None, art_dir: Path | None = None):
        self.pipe, self.on_change = pipe, on_change
        self.art_dir = art_dir or pipe.parent
        self.state: dict = {"session": False, "playing": False, "client": None, "title": None,
                            "artist": None, "album": None, "volume": None, "since": None,
                            "track": {}, "client_info": {}, "progress": None, "artwork": None}
        self.raw: dict = {}
        self.pictured = False
        self.art_file = self.art_dir / "calliope-airplay-cover"
        # The pipe's thread and the agent's cover recovery both write the
        # cover: one at a time, so the file and the state always name the
        # same picture, and a recovery never lands on one the pipe just sent.
        self._art_lock = threading.RLock()
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
            return False
        if code != "PICT":
            self.raw[key] = {"value": _decoded(kind, code, data), "at": round(time.time(), 3)}
        text = data.decode("utf-8", "replace").strip() or None
        if kind == "ssnc":
            if code in ("abeg", "pbeg", "prsm"):
                s["session"], s["playing"] = True, True
                s["since"] = s["since"] or time.time()
                if code == "abeg":
                    self.pictured = False
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
        with self._art_lock:
            self.pictured = True
            if not data:
                self.state["artwork"] = None
                return
            kind = "jpeg" if data[:3] == b"\xff\xd8\xff" else "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "bin"
            digest = hashlib.sha256(data).hexdigest()
            try:
                self.art_dir.mkdir(parents=True, exist_ok=True)
                self.art_file.write_bytes(data)
            except OSError:
                pass
            self.state["artwork"] = {"sha256": digest, "bytes": len(data), "type": kind}

    def recover_artwork(self, data: bytes) -> bool:
        """A cover found again in Shairport Sync's cache (cached_cover), kept
        exactly as the same picture from the pipe would have been; whether it
        was. Not when the pipe has sent a picture since the agent looked:
        reading MPRIS and the cache takes a while, and the pipe's word is
        this track's. Asked again under the lock for that reason."""
        with self._art_lock:
            if self.pictured:
                return False
            self._artwork(data)
            return True

    def view(self) -> dict:
        """What the status carries beside the decoded state: every raw item."""
        return {"raw": dict(self.raw)}


async def mpris_metadata() -> dict:
    """The track as Shairport Sync's MPRIS interface has it: what fills the
    title, artist and album when the metadata pipe has not said them (the
    agent restarted in the middle of a track)."""
    code, out = await _run("busctl", "--user", "--json=short", "get-property",
                           "org.mpris.MediaPlayer2.ShairportSync", "/org/mpris/MediaPlayer2",
                           "org.mpris.MediaPlayer2.Player", "Metadata", timeout=3.0)
    if code:
        return {}
    try:
        data = json.loads(out).get("data") or {}
    except ValueError:
        return {}

    def value(key):
        v = (data.get(key) or {}).get("data")
        return ", ".join(v) if isinstance(v, list) else v
    return {k: v for k, v in (("title", value("xesam:title")), ("artist", value("xesam:artist")),
                              ("album", value("xesam:album")), ("art_url", value("mpris:artUrl")),
                              ("trackid", value("mpris:trackid"))) if v}


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


async def remote_available() -> bool | None:
    """Whether Shairport Sync can reach the phone's remote control now
    (RemoteControl.Available: it asks the phone every second, and five
    failures in a row make it false); None when it cannot be asked."""
    code, out = await _run("busctl", "--user", "--json=short", "get-property", DBUS_NAME, DBUS_PATH,
                           f"{DBUS_NAME}.RemoteControl", "Available", timeout=3.0)
    if code:
        return None
    try:
        data = json.loads(out).get("data")
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, bool) else None


async def remote_command(command: str) -> tuple[bool, int | None, str | None]:
    """One of CONTROLS, sent to the phone: (ok, status, error). `status` is
    the phone's own HTTP status, or Shairport Sync's 490-498 when it could not
    ask (SHAIRPORT_CODES), and None for disconnect, which asks the phone
    nothing. A busy answer is asked once more."""
    if command == "disconnect":
        code, out = await _run("busctl", "--user", "call", DBUS_NAME, DBUS_PATH, DBUS_NAME, "DropSession",
                               timeout=8.0)
        if code == 0:
            return True, None, None
        return False, None, out.strip()[-200:] or f"busctl exited {code}"
    if command not in COMMANDS:
        return False, None, "unknown command"
    for attempt in range(2):
        code, out = await _run("busctl", "--user", "--json=short", "--timeout=5", "call", DBUS_NAME, DBUS_PATH,
                               DBUS_NAME, "RemoteCommand", "s", COMMANDS[command], timeout=8.0)
        status = _remote_status(out) if code == 0 else None
        if status != BUSY or attempt:
            break
        await asyncio.sleep(0.3)
    if status is not None and 200 <= status < 300:
        return True, status, None
    if status in SHAIRPORT_CODES:
        return False, status, SHAIRPORT_CODES[status]
    if status is not None:
        return False, status, f"the phone answered {status}"
    return False, None, out.strip()[-200:] or f"busctl exited {code}"


def _remote_status(out: str) -> int | None:
    """The status in RemoteCommand's answer, {"type": "is", "data": [status, body]}."""
    try:
        return int(json.loads(out)["data"][0])
    except (ValueError, TypeError, KeyError, IndexError):
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
