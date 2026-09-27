"""AirPlay into a satellite: music from a receiver, mixed with the hub's voice.

THE RECEIVER IS NOT HERE. Shairport Sync, in its own container (compose.yaml,
voice-airplay), is the AirPlay 2 target a phone sees. It writes what it plays,
as raw 48 kHz 16-bit mono (the satellite's speaker format, so nothing here
resamples), into a named pipe in a volume both containers mount. A pipe named
after a satellite, by its id or its name, is that satellite's music:

    /airplay/korvo  ->  the satellite called korvo

Shairport Sync paces the pipe against the phone's clock, on the same kernel
clock the hub paces by, so music arrives as fast as it is sent on, with two
exceptions its player.c makes for an output that cannot report its delay, as a
pipe cannot. At each start it writes the whole lead-in, up to the moment the
first sample is due, as silence and at once: a second or two of zeros in a
millisecond. That silence is the timing (the first real sample comes after it),
so it is kept and played like the rest, and MAX_BUFFER_S has room for it. And
it releases the audio audio_backend_buffer_desired_length_in_seconds early,
which compose.yaml sets to 0.5 s, so the hub always has the 0.3 s it keeps the
satellite ahead.

MUSIC AND VOICE SHARE ONE STREAM. A satellite plays a single stream from the
hub, so the speaker loop mixes: music, with the voice of a reply, a tone or
/say on top. While the satellite is ENGAGED (a conversation is open on it, or
voice is queued or playing) the music fades out, and it fades back when the
conversation is over. Out, not down: a conversation listens for its follow-up
only once the loopback has gone quiet (listening.FOLLOW_UP_GUARD_S), and music
still in the loopback would keep it from ever starting. The song goes on at
the phone; what the satellite missed is not played late.

ONLY ITS OWN FORMAT IS PLAYED. Raw PCM says nothing about itself. 32-bit
samples read as 16-bit put the low halves between the high ones: audio with
unrelated values between every two samples, which plays as a loud buzz. So
the first block that is not silence is checked for that: its even and odd
samples, each taken alone, must be the same kind of signal (interleaved()). Stereo read as mono
is the song at half speed, not noise, and shows only in the rate: more than
1.6 times 16-bit mono's, three seconds after the audio began. Either is
refused, and logged, until the music stops. Silence is never checked: zeros
are safe in any format, and the lead-in burst would read as a fast rate.

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
MAX_BUFFER_S = 4.0     # the lead-in silence is written at once; beyond it the loop stalled
IDLE_S = 1.0           # no music for this long and it has stopped: paused, or over
FADE_S = 0.3           # music in, or out, over this long
SILENT = 32            # a block no sample of which is louder than this is silence (dither)
RATE_WINDOW_S = 3.0    # audio this long before its rate is judged
RATE_MAX = 1.6         # stereo is 2x; the early release adds 0.5 s over the window
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
        self.audio_at: float | None = None  # the first block of this start that was not silence
        self.audio_bytes = 0       # since then, for the rate
        self.rate_checked = False
        self.refused = False       # this start is not 48 kHz 16-bit mono
        self.arrived = asyncio.Event()

    def feed(self, data: bytes, now: float | None = None) -> None:
        if not data:
            return
        now = time.monotonic() if now is None else now
        if now - self.last > IDLE_S:
            # A new start: check its format again, gather PREFILL_S, fade in.
            self.primed, self.refused, self.gain = False, False, 0.0
            self.audio_at, self.audio_bytes, self.rate_checked = None, 0, False
        self.last = now
        if self.refused:
            return
        problem = self._check(data, now)
        if problem:
            self.refused = True
            self.clear()
            log.warning("AirPlay: refused: %s. The receiver's pipe must be output_rate = %d, "
                        'output_format = "S16_LE", output_channels = 1.', problem, self.rate)
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

    def _check(self, data: bytes, now: float) -> str | None:
        """What is wrong with this start's format, once audio shows it."""
        if self.audio_at is None:
            x = np.frombuffer(data[:len(data) // 2 * 2], "<i2")
            if x.size == 0 or int(np.abs(x.astype(np.int32)).max()) <= SILENT:
                return None
            self.audio_at = now
            if interleaved(x):
                return "every other sample is unrelated to its neighbours (32-bit samples?)"
            return None
        self.audio_bytes += len(data)
        span = now - self.audio_at
        if not self.rate_checked and span >= RATE_WINDOW_S:
            self.rate_checked = True
            rate = self.audio_bytes / span
            if rate > RATE_MAX * self.rate * 2:
                return f"it arrives at {rate:.0f} bytes a second, {rate / (self.rate * 2):.1f} times mono's (stereo?)"
        return None

    def ready(self, nbytes: int) -> bool:
        """Music alone may send a chunk: a whole one is here, and PREFILL_S
        has been gathered since it started."""
        if not self.primed and self.size >= int(PREFILL_S * self.rate) * 2:
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


def interleaved(x: np.ndarray) -> bool:
    """Two unrelated streams taking turns, as 32-bit samples read as 16-bit
    are: the high halves are the audio, and the low halves between them are a
    decoder's noise, or zeros. So the even samples and the odd ones, each taken
    alone, are two different signals: one smooth, one not. In real audio both
    are the same signal at half the rate, and alike whatever it is; noise has
    both unsmooth, a tone at any frequency has both equally smooth."""
    if x.size < 256:
        return False

    def smooth(y: np.ndarray) -> float:
        y = y.astype(np.float64) - y.mean()
        a, b = y[:-1], y[1:]
        d = float(np.sqrt((a @ a) * (b @ b)))
        return float(a @ b) / d if d else 0.0
    return abs(smooth(x[0::2]) - smooth(x[1::2])) > 0.5


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
