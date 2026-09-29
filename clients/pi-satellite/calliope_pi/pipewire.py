"""PipeWire, through its own command-line tools: pw-dump to list the devices,
wpctl to choose and set them, pw-cat to play and record.

WHY THE COMMAND-LINE TOOLS and not a binding: they are in every PipeWire
install (pipewire-bin), they are what an operator would type to check the
same thing, and a player that dies takes nothing of the agent with it.

OUTPUT is one device for everything the board plays: the hub's replies here,
and AirPlay and Bluetooth later. Choosing it sets PipeWire's default sink.
The agent's own stream carries media.role "Assistant", which WirePlumber's
role policy can duck other streams for.

INPUT is the chosen microphone, recorded together with the output's monitor,
what the speaker is playing, as channel 0: the hub's front-end cancels the
satellite's own voice (and any music) from the microphone with it, as it does
with the Korvo's loopback channel."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from array import array
from dataclasses import dataclass

log = logging.getLogger("calliope.pipewire")

MIC_RATE = 16000
SPEAKER_RATE = 48000
FRAME_MS = 20
IDLE_S = 0.6          # after the last sample has played, the player is closed


@dataclass(frozen=True)
class Device:
    name: str             # node.name: stable across reboots for the same device
    description: str      # what a person reads
    api: str | None       # alsa, bluez5, ...
    id: int               # object.id: what wpctl takes, valid until it goes away

    def view(self) -> dict:
        return {"name": self.name, "description": self.description, "api": self.api}


@dataclass(frozen=True)
class Devices:
    sinks: tuple[Device, ...]
    sources: tuple[Device, ...]
    default_sink: str | None
    default_source: str | None

    def view(self) -> dict:
        return {"sinks": [d.view() for d in self.sinks], "sources": [d.view() for d in self.sources],
                "default_sink": self.default_sink, "default_source": self.default_source}

    def sink(self, name: str | None) -> Device | None:
        return next((d for d in self.sinks if d.name == name), None)

    def source(self, name: str | None) -> Device | None:
        return next((d for d in self.sources if d.name == name), None)


NONE = Devices((), (), None, None)


def parse_dump(objects: list) -> Devices:
    """The audio sinks and sources of a pw-dump, and the defaults. Streams,
    video and MIDI nodes are left out, and so is a source that is a sink's
    monitor or a filter's virtual one."""
    sinks, sources = [], []
    default_sink = default_source = None
    for o in objects if isinstance(objects, list) else []:
        if not isinstance(o, dict):
            continue
        if o.get("type") == "PipeWire:Interface:Metadata" and \
                (o.get("props") or {}).get("metadata.name") == "default":
            for item in o.get("metadata") or []:
                value = item.get("value")
                name = value.get("name") if isinstance(value, dict) else None
                if item.get("key") == "default.audio.sink":
                    default_sink = name
                elif item.get("key") == "default.audio.source":
                    default_source = name
            continue
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        props = (o.get("info") or {}).get("props") or {}
        media = props.get("media.class")
        name = props.get("node.name")
        if not name or media not in ("Audio/Sink", "Audio/Source"):
            continue
        dev = Device(name=name,
                     description=props.get("node.description") or props.get("node.nick") or name,
                     api=props.get("device.api"), id=int(o.get("id", -1)))
        (sinks if media == "Audio/Sink" else sources).append(dev)
    return Devices(tuple(sinks), tuple(sources), default_sink, default_source)


async def _run(*argv: str, timeout: float = 5.0) -> tuple[int, str]:
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


async def devices() -> Devices:
    code, out = await _run("pw-dump", timeout=8.0)
    if code != 0:
        log.debug("pw-dump failed: %s", out[:200])
        return NONE
    try:
        return parse_dump(json.loads(out))
    except ValueError:
        return NONE


async def sinks() -> list:
    """pactl's view of the outputs: what each is open at now."""
    code, out = await _run("pactl", "-f", "json", "list", "sinks")
    if code:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def output_format(sinks_: list, name: str | None) -> dict | None:
    """The output as the card is driven now: sample format, channels and
    rate (\"s32le 2ch 44100Hz\"), and whether it is running, idle or
    suspended. The default output when `name` is None."""
    for s in sinks_ if isinstance(sinks_, list) else []:
        if name is None or s.get("name") == name:
            return {"name": s.get("name"), "format": s.get("sample_specification"),
                    "state": str(s.get("state") or "").lower() or None}
    return None


async def set_default(dev: Device) -> bool:
    code, out = await _run("wpctl", "set-default", str(dev.id))
    if code:
        log.warning("wpctl set-default %s: %s", dev.name, out.strip()[:200])
    return code == 0


async def set_volume(dev: Device | None, fraction: float) -> bool:
    """The device's own volume, 0.0-1.0 (the default sink when `dev` is None)."""
    target = str(dev.id) if dev is not None else "@DEFAULT_AUDIO_SINK@"
    code, _ = await _run("wpctl", "set-volume", target, f"{max(0.0, min(1.0, fraction)):.3f}")
    return code == 0


async def set_source_volume(dev: Device | None, fraction: float) -> bool:
    target = str(dev.id) if dev is not None else "@DEFAULT_AUDIO_SOURCE@"
    code, _ = await _run("wpctl", "set-volume", target, f"{max(0.0, min(1.5, fraction)):.3f}")
    return code == 0


def _cat(mode: str, rate: int, channels: int, target: str | None, role: str | None,
         capture_sink: bool = False, latency_ms: int = 60) -> list[str]:
    argv = ["pw-cat", f"--{mode}", "--raw", "--format", "s16", "--rate", str(rate),
            "--channels", str(channels), "--latency", f"{latency_ms}ms"]
    if target:
        argv += ["--target", target]
    props = {}
    if role:
        props["media.role"] = role
    if capture_sink:
        props["stream.capture.sink"] = True
    if props:
        argv += ["--properties", json.dumps(props)]
    return argv + ["-"]


class Player:
    """The hub's audio, played to the output as it arrives. A pw-cat is
    started with the first samples and closed IDLE_S after the last has
    played, so the Assistant stream exists only while there is a voice to
    duck other streams for."""

    def __init__(self, rate: int = SPEAKER_RATE, target: str | None = None, role: str = "Assistant"):
        self.rate, self.target, self.role = rate, target, role
        self.proc: asyncio.subprocess.Process | None = None
        self.until = 0.0          # time.monotonic() when what was written has played
        self.dropped = 0          # writes lost to a player that failed
        self._closer: asyncio.Task | None = None
        # Told True when a voice starts playing and False when it has ended,
        # so the agent can turn other streams down meanwhile (airplay.Ducker).
        self.on_active = None

    def buffered_ms(self) -> int:
        return max(0, int((self.until - time.monotonic()) * 1000))

    async def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self.proc is None or self.proc.returncode is not None:
            self.proc = await asyncio.create_subprocess_exec(
                *_cat("playback", self.rate, 1, self.target, self.role),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            if self.on_active is not None:
                await self.on_active(True)
        try:
            self.proc.stdin.write(pcm)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.dropped += 1
            self.proc = None
            return
        now = time.monotonic()
        self.until = max(self.until, now) + len(pcm) / 2 / self.rate
        if self._closer is None or self._closer.done():
            self._closer = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        while True:
            left = self.until + IDLE_S - time.monotonic()
            if left <= 0:
                break
            await asyncio.sleep(left)
        await self._stop(drain=True)

    async def _stop(self, drain: bool) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and self.on_active is not None:
            await self.on_active(False)
        if proc is None or proc.returncode is not None:
            return
        if drain:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
                return
            except TimeoutError:
                pass
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

    async def flush(self) -> None:
        """Stop now, dropping whatever is queued."""
        self.until = time.monotonic()
        if self._closer is not None:
            self._closer.cancel()
        await self._stop(drain=False)

    async def play_once(self, pcm: bytes, rate: int) -> None:
        """A whole sound (an earcon), on its own short-lived stream."""
        proc = await asyncio.create_subprocess_exec(
            *_cat("playback", rate, 1, self.target, self.role),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(pcm)
            await proc.stdin.drain()
            proc.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), len(pcm) / 2 / rate + 3)


class Recorder:
    """The microphone, with the output's monitor as channel 0 when
    `reference` is set: 20 ms frames of interleaved s16le, [monitor, mic]."""

    def __init__(self, source: str | None, sink: str | None, reference: bool = True,
                 rate: int = MIC_RATE):
        self.source, self.sink, self.reference, self.rate = source, sink, reference, rate
        self.channels = 2 if reference else 1
        self.dropped = 0
        self._procs: list[asyncio.subprocess.Process] = []

    async def frames(self):
        """Yield one frame at a time until stopped or a recorder dies."""
        n = self.rate * FRAME_MS // 1000
        size = n * 2
        mic = await asyncio.create_subprocess_exec(
            *_cat("record", self.rate, 1, self.source, None),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self._procs = [mic]
        ref = None
        if self.reference:
            ref = await asyncio.create_subprocess_exec(
                *_cat("record", self.rate, 1, self.sink, None, capture_sink=True),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self._procs.append(ref)
        try:
            while True:
                try:
                    m = await mic.stdout.readexactly(size)
                    r = await ref.stdout.readexactly(size) if ref is not None else None
                except asyncio.IncompleteReadError:
                    return
                yield interleave(r, m) if r is not None else m
        finally:
            await self.stop()

    async def stop(self) -> None:
        procs, self._procs = self._procs, []
        for p in procs:
            if p.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
                await p.wait()


def interleave(first: bytes, second: bytes) -> bytes:
    """Two mono s16le blocks of one length as one stereo block, first on
    channel 0."""
    a, b = array("h"), array("h")
    a.frombytes(first)
    b.frombytes(second)
    out = array("h", bytes(len(first) * 2))
    out[0::2] = a
    out[1::2] = b
    return out.tobytes()
