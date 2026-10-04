"""Earcons: short feedback sounds the hub uploads once and each satellite keeps
in flash, so that "I heard you", "done" and "that failed" play from the
satellite itself, with no audio crossing the network when they are needed.

On the wire (the satellite's side is clients/korvo-satellite/src/earcons.cpp):

    hub       -> satellite  {"type": "earcon_put", "id": "wake", "size": N,
                             "sha256": "..."}
    satellite -> hub        {"type": "earcon_next", "id": "wake", "offset": 0}
    hub       -> satellite  binary: kind 4, 0, 0, 0, offset u32 LE, then up to 8 KB
               ... one chunk per earcon_next, as for firmware ...
    satellite -> hub        {"type": "earcon_stored", "id", "size", "sha256"}
                            or {"type": "earcon_failed", "op": "put", "id", "error"}
    hub       -> satellite  {"type": "earcon", "id": "wake"}        play it now
    hub       -> satellite  {"type": "earcon_list"}
    satellite -> hub        {"type": "earcons", "ready": bool,
                             "items": [{id, size, sha256}], "last_load_us": N}
    hub       -> satellite  {"type": "earcon_delete", "id": "wake"}  answered with
                            "earcons"

Files are raw mono s16le at 48 kHz, at most 2 s (192 000 bytes), at most 16 per
satellite, with ids of [a-z0-9_-]{1,24}. An earcon plays over whatever the hub
is sending and is not ducked.

The default set is synthesised here with numpy rather than shipped as audio
files: a few lines of arithmetic are easier to review, and to change, than a
binary.
"""

from __future__ import annotations

import hashlib
import re
import struct
from typing import Iterator

import numpy as np

RATE = 48000  # the satellite's speaker rate; earcons are played as stored, never resampled
FRAME_EARCON = 4
HEADER = 8  # kind, 0, 0, 0, offset u32 little-endian
# The Arduino WebSockets library drops the connection on any frame over 15 KB,
# so chunks match the 8 KB firmware chunks that are known to work.
CHUNK = 8 * 1024
MAX_BYTES = 2 * RATE * 2  # 2 s
MAX_EARCONS = 16
ID_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
# Amplitude before the envelope. The default set peaks at -13 to -12 dBFS,
# which leaves room to be mixed over speech on the satellite without clipping
# much.
LEVEL = 0.3


class EarconError(ValueError):
    pass


# ---- the default set --------------------------------------------------------


def _note(freq: float, ms: float, level: float = LEVEL) -> np.ndarray:
    """A soft struck tone: a sine with a little second and third harmonic, so a
    small speaker that cannot reproduce the fundamental still carries the
    pitch, decaying to silence."""
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    y = (np.sin(2 * np.pi * freq * t)
         + 0.25 * np.sin(2 * np.pi * 2 * freq * t)
         + 0.08 * np.sin(2 * np.pi * 3 * freq * t)) / 1.33
    env = np.exp(-3 * t / (ms / 1000))  # about 5 % left at the end
    # Raised-cosine edges that start and end on exactly zero: a step at either
    # end is an audible click.
    a, r = int(0.004 * RATE), int(0.010 * RATE)
    env[:a] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, a))
    env[-r:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, r))
    return level * y * env


def _place(parts: list[tuple[float, np.ndarray]]) -> bytes:
    """Mixes (start ms, samples) pairs into one s16le buffer."""
    starts = [int(RATE * ms / 1000) for ms, _ in parts]
    out = np.zeros(max(s + len(p) for s, (_, p) in zip(starts, parts)))
    for s, (_, p) in zip(starts, parts):
        out[s:s + len(p)] += p
    return np.clip(np.round(out * 32767), -32768, 32767).astype("<i2").tobytes()


# E5 and A5, a rising fourth: the note after the pause sounds like an answer.
E5, A5, D4 = 659.26, 880.0, 293.66


def wake() -> bytes:
    """Rising two-tone, 150 ms: "I heard you"."""
    return _place([(0, _note(E5, 90)), (60, _note(A5, 90))])


def done() -> bytes:
    """The same two notes falling, 150 ms: "finished"."""
    return _place([(0, _note(A5, 90)), (60, _note(E5, 90))])


def error() -> bytes:
    """Two low, short pulses with a gap, 210 ms: "that did not work"."""
    return _place([(0, _note(D4, 80, LEVEL * 1.2)), (130, _note(D4, 80, LEVEL * 1.2))])


def defaults() -> dict[str, bytes]:
    return {"wake": wake(), "done": done(), "error": error()}


# ---- the transfer -------------------------------------------------------------


def check(eid: str, pcm: bytes) -> None:
    if not ID_RE.match(eid or ""):
        raise EarconError(f"earcon id {eid!r} must match {ID_RE.pattern}")
    if not pcm or len(pcm) % 2:
        raise EarconError(f"earcon {eid!r} must be whole s16le samples, and not empty")
    if len(pcm) > MAX_BYTES:
        raise EarconError(f"earcon {eid!r} is {len(pcm)} bytes; a satellite keeps at most "
                          f"{MAX_BYTES} (2 s at {RATE} Hz)")


def sha256(pcm: bytes) -> str:
    return hashlib.sha256(pcm).hexdigest()


def put_message(eid: str, pcm: bytes) -> dict:
    check(eid, pcm)
    return {"type": "earcon_put", "id": eid, "size": len(pcm), "sha256": sha256(pcm)}


def play_message(eid: str) -> dict:
    return {"type": "earcon", "id": eid}


def frame(pcm: bytes, offset: int) -> bytes:
    return struct.pack("<BBBBI", FRAME_EARCON, 0, 0, 0, offset) + pcm[offset:offset + CHUNK]


def frames(pcm: bytes) -> Iterator[bytes]:
    for off in range(0, len(pcm), CHUNK):
        yield frame(pcm, off)


def supported(caps: dict) -> bool:
    """Firmware older than earcons ignores these messages without a word, so
    the hub asks the hello first."""
    return bool((caps or {}).get("earcons"))


class Sync:
    """Brings one satellite's earcons in line with `want`, one upload at a
    time, the only way a satellite takes them. It does no I/O: hand it the
    satellite's messages through `handle` and send whatever it returns (a dict
    as JSON, bytes as a binary frame). One per connected satellite; start it by
    sending earcon_list.
    """

    def __init__(self, want: dict[str, bytes]):
        if len(want) > MAX_EARCONS:
            raise EarconError(f"{len(want)} earcons; a satellite keeps at most {MAX_EARCONS}")
        for eid, pcm in want.items():
            check(eid, pcm)
        self.want = dict(want)
        self.queue: list[str] = []
        self.current: str | None = None
        self.stored: list[str] = []
        self.failed: dict[str, str] = {}
        # False until the satellite answers earcon_list with its storage
        # mounted. The first mount after a blank partition formats it in the
        # background, so ask again a few seconds later.
        self.ready = False

    @property
    def done(self) -> bool:
        return self.ready and self.current is None and not self.queue

    def has(self, eid: str) -> bool:
        """The satellite holds `eid` with the content this hub wants: it
        listed it already, or stored it since. Anything else, a play would
        only come back as earcon_failed, so the hub plays nothing instead."""
        return (self.ready and eid in self.want and eid != self.current
                and eid not in self.queue and eid not in self.failed)

    def plan(self, items: list[dict]) -> list[str]:
        """The ids the satellite lacks, or holds with different content."""
        have = {i.get("id"): i.get("sha256") for i in items if isinstance(i, dict)}
        return [eid for eid, pcm in self.want.items() if have.get(eid) != sha256(pcm)]

    def handle(self, msg: dict) -> dict | bytes | None:
        kind = msg.get("type")
        if kind == "earcons":
            return self.on_list(msg)
        if kind == "earcon_next":
            return self.on_next(msg)
        if kind == "earcon_stored":
            return self.on_stored(msg)
        if kind == "earcon_failed":
            return self.on_failed(msg)
        return None

    def on_list(self, msg: dict) -> dict | None:
        if self.current is not None:  # a list that crossed an upload in flight
            return None
        self.ready = bool(msg.get("ready"))
        self.queue = self.plan(msg.get("items") or []) if self.ready else []
        return self._next_put()

    def on_next(self, msg: dict) -> bytes | None:
        if self.current is None or msg.get("id") != self.current:
            return None
        pcm = self.want[self.current]
        off = msg.get("offset")
        if not isinstance(off, int) or not 0 <= off < len(pcm):
            return None
        return frame(pcm, off)

    def on_stored(self, msg: dict) -> dict | None:
        if msg.get("id") == self.current:
            self.stored.append(self.current)
            self.current = None
        return self._next_put()

    def on_failed(self, msg: dict) -> dict | None:
        # A failed play or delete says nothing about the upload in progress,
        # even for the same id.
        if msg.get("op") == "put" and msg.get("id") == self.current:
            self.failed[self.current] = str(msg.get("error"))
            self.current = None
        return self._next_put()

    def _next_put(self) -> dict | None:
        if self.current is not None or not self.queue:
            return None
        self.current = self.queue.pop(0)
        return put_message(self.current, self.want[self.current])
