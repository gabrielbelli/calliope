"""AirPlay into a satellite: music from a receiver, mixed with the hub's voice.

THE RECEIVER IS NOT HERE. Shairport Sync, in its own container (compose.yaml,
voice-airplay), is the AirPlay 2 target a phone sees. It writes what it plays,
as raw 48 kHz 16-bit mono (the satellite's speaker format, so nothing here
resamples), into a named pipe in a volume both containers mount. A pipe named
after a satellite, by its id or its name, is that satellite's music:

    /airplay/korvo  ->  the satellite called korvo

Shairport Sync paces the pipe in real time against the phone's clock, on the
same kernel clock the hub paces by, so music arrives as fast as it is sent on.

MUSIC AND VOICE SHARE ONE STREAM. A satellite plays a single stream from the
hub, so the speaker loop mixes: music, with the voice of a reply, a tone or
/say on top. While the satellite is ENGAGED (a conversation is open on it, or
voice is queued or playing) the music fades out, and it fades back when the
conversation is over. Out, not down: a conversation listens for its follow-up
only once the loopback has gone quiet (listening.FOLLOW_UP_GUARD_S), and music
still in the loopback would keep it from ever starting. The song goes on at
the phone; what the satellite missed is not played late.

ONLY ITS OWN FORMAT IS PLAYED. Raw PCM says nothing about itself, and a pipe
set up for stereo or 32-bit samples would play as full-scale noise. So the
rate music arrives at, over the PREFILL_S it waits for anyway, has to be that
of 48 kHz 16-bit mono (2 bytes x 48000 a second, within RATE_TOLERANCE); a
start that is not is refused, and logged, until the music stops.

THE SATELLITE'S CLOCK. Its DAC runs on its own crystal, a few tens of ppm off
the hub's, which over an hour of music is enough to empty or overflow its
one-second buffer. Its status reports what it holds (spk_buffered_ms) every
ten seconds; outside BUFFER_LOW_MS..BUFFER_HIGH_MS the next chunk of music is
dropped, or repeated, moving the buffer 20 ms back towards the middle. At
100 ppm that is one correction every three minutes or so.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger("voice-satellites.airplay")

PREFILL_S = 0.3        # gathered before music starts, so the satellite holds as
                       # much as the voice path keeps it ahead (SPEAKER_LEAD_S)
MAX_BUFFER_S = 1.0     # more than this means the loop stalled: the oldest goes
IDLE_S = 1.0           # no music for this long and it has stopped: paused, or over
FADE_S = 0.3           # music in, or out, over this long
RATE_TOLERANCE = 0.5   # 0.5x to 1.5x of the expected byte rate: stereo, or 32-bit, is 2x
BUFFER_LOW_MS = 120
BUFFER_HIGH_MS = 600
READ_BYTES = 4096
SCAN_S = 5.0           # how often the directory is looked at for new pipes


class Music:
    """One satellite's music, on the event loop: the pipe's reader hands it
    in through call_soon_threadsafe, and the speaker loop takes it 20 ms at a
    time."""

    def __init__(self, rate: int = 48000):
        self.rate = rate
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.last = -1e9           # time.monotonic() of the last music in
        self.primed = False        # PREFILL_S gathered since it last started
        self.gain = 0.0            # where the fade is
        self.adjust = 0            # the clock servo: -1 drop a chunk, +1 repeat one
        self.previous = b""
        self.playing = False       # as last published
        self.first = 0.0           # the first block of this start, when it came
        self.after_first = 0       # bytes since it, for the rate check
        self.refused = False       # this start is not 48 kHz 16-bit mono
        self.arrived = asyncio.Event()

    def feed(self, data: bytes, now: float | None = None) -> None:
        if not data:
            return
        now = time.monotonic() if now is None else now
        if now - self.last > IDLE_S:
            # A new start: check its rate, gather PREFILL_S, and fade in.
            self.primed, self.refused, self.gain = False, False, 0.0
            self.first, self.after_first = now, 0
        else:
            self.after_first += len(data)
        self.last = now
        if self.refused:
            return
        self.chunks.append(data)
        self.size += len(data)
        cap = int(MAX_BUFFER_S * self.rate) * 2
        while self.size > cap:
            self.size -= len(self.chunks.popleft())
        self.arrived.set()

    def on(self, now: float | None = None) -> bool:
        """Music came in the last IDLE_S, and it was not refused. What is left
        after that, less than a chunk at the end of a song, is cleared when it
        stops."""
        now = time.monotonic() if now is None else now
        return now - self.last <= IDLE_S and not self.refused

    def ready(self, nbytes: int, now: float | None = None) -> bool:
        """Music alone may send a chunk: a whole one is here, and since it
        started PREFILL_S has been gathered at the rate of its format."""
        if not self.primed and self.size >= int(PREFILL_S * self.rate) * 2:
            now = time.monotonic() if now is None else now
            # Counted from after the first block, so a first write larger than
            # the rest does not read as a fast rate.
            rate = self.after_first / max(now - self.first, 1e-3)
            expected = self.rate * 2
            if abs(rate / expected - 1) > RATE_TOLERANCE:
                self.refused = True
                self.clear()
                log.warning("AirPlay: refused: music arrives at %d bytes a second, and 48 kHz "
                            "16-bit mono is %d. Is the receiver's pipe set to output_rate = %d, "
                            'output_format = "S16_LE", output_channels = 1?', rate, expected, self.rate)
                return False
            self.primed = True
        return self.primed and self.size >= nbytes

    def take(self, nbytes: int) -> bytes:
        """nbytes of music, padded with silence when short (under a voice, or
        at the end of a song). The clock servo acts here."""
        if self.adjust < 0 and self.size >= 2 * nbytes:
            self._pop(nbytes)      # dropped: the satellite holds too much
            self.adjust = 0
        elif self.adjust > 0 and self.previous:
            self.adjust = 0        # repeated: it holds too little
            return self.previous
        out = self._pop(nbytes)
        out += bytes(nbytes - len(out))
        self.previous = out
        return out

    def _pop(self, nbytes: int) -> bytes:
        parts, need = [], nbytes
        while need > 0 and self.chunks:
            head = self.chunks[0]
            if len(head) <= need:
                parts.append(self.chunks.popleft())
                need -= len(head)
            else:
                parts.append(head[:need])
                self.chunks[0] = head[need:]
                need = 0
        out = b"".join(parts)
        self.size -= len(out)
        return out

    def buffered(self, buffered_ms: object) -> None:
        """The satellite's own count of what it holds, from its status."""
        if not isinstance(buffered_ms, (int, float)) or isinstance(buffered_ms, bool):
            return
        if buffered_ms > BUFFER_HIGH_MS:
            self.adjust = -1
        elif buffered_ms < BUFFER_LOW_MS:
            self.adjust = +1

    def clear(self) -> None:
        self.chunks.clear()
        self.size = 0
        self.primed = False


def mix(voice: bytes | None, music: bytes | None, gain_from: float, gain_to: float,
        samples: int) -> bytes:
    """One chunk of `samples`: the music ramped from gain_from to gain_to
    across it, the voice at full level on top, clipped to int16."""
    out = np.zeros(samples, np.float32)
    if music is not None and (gain_from > 0 or gain_to > 0):
        m = np.frombuffer(music[:samples * 2], "<i2").astype(np.float32)
        ramp = (np.linspace(gain_from, gain_to, len(m), endpoint=False, dtype=np.float32)
                if gain_from != gain_to else np.float32(gain_to))
        out[:len(m)] += m * ramp
    if voice is not None:
        v = np.frombuffer(voice[:min(len(voice), samples * 2) // 2 * 2], "<i2").astype(np.float32)
        out[:len(v)] += v
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()


def fade_step(gain: float, target: float, chunk_s: float) -> float:
    """Where the fade is one chunk later."""
    step = chunk_s / FADE_S
    moved = gain + step if gain < target else gain - step
    # Within a rounding error of the target is the target: fifteen steps of
    # 1/15 add up to 0.9999999.
    return target if abs(moved - target) < 1e-6 or (moved > target) == (gain < target) else moved


# ---- the pipes -----------------------------------------------------------------


class Pipes:
    """Watches a directory for named pipes and reads each in a thread of its
    own, handing every block to deliver(name, data) from that thread.

    Opened read-only and non-blocking, so opening never waits for a writer.
    poll() then waits for music; with no writer at all a pipe polls as hung
    up at once, so the thread sleeps between looks instead of spinning."""

    def __init__(self, directory: Path, deliver: Callable[[str, bytes], None]):
        self.dir = directory
        self.deliver = deliver
        self.threads: dict[str, threading.Thread] = {}
        self.stopping = threading.Event()

    def scan(self) -> list[str]:
        """A reader for every pipe not yet read. The names started."""
        started = []
        try:
            entries = sorted(self.dir.iterdir())
        except OSError:
            return started
        for p in entries:
            try:
                if not stat.S_ISFIFO(p.stat().st_mode) or p.name in self.threads:
                    continue
            except OSError:
                continue
            t = threading.Thread(target=self._read, args=(p,), name=f"airplay-{p.name}", daemon=True)
            self.threads[p.name] = t
            t.start()
            started.append(p.name)
            log.info("AirPlay: reading %s, for the satellite of that name", p)
        return started

    def stop(self) -> None:
        self.stopping.set()

    def _read(self, path: Path) -> None:
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            while not self.stopping.is_set():
                events = poller.poll(200)
                if not events:
                    continue
                if events[0][1] & select.POLLIN:
                    try:
                        data = os.read(fd, READ_BYTES)
                    except BlockingIOError:
                        continue
                    if data:
                        self.deliver(path.name, data)
                        continue
                # Hung up: no writer. Look again shortly.
                self.stopping.wait(0.1)
        except Exception as e:  # logged; the next scan starts it again
            log.warning("AirPlay: reading %s failed: %s", path, e or type(e).__name__)
        finally:
            if fd is not None:
                os.close(fd)
            self.threads.pop(path.name, None)
