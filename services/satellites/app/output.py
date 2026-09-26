"""Which output a satellite plays through: its speaker, or headphones.

THE KORVO HAS NO JACK-DETECT INPUT. Its headphone jack (J1, a PJ-393-A,
schematic sheet 4, "EarPhone" and "AEC") switches in hardware. With no plug,
the codec's output runs through the jack's normally-closed contacts on to the
speaker amplifier and to the loopback, ES7210 channel 0. A plug opens those
contacts: the codec then drives the headphones alone, and the loopback hears
nothing. The jack's detect pin only gates the amplifier (Q16, U19); no GPIO
reads it.

So the loopback says which, but only while something plays. After each sound
the hub played (a reply, a tone, an earcon), the loopback over that sound is
compared with its level when nothing was playing:

- risen by RISE_DB or more: the loopback heard it, so there is no plug;
- risen by FLAT_DB or less, for a sound that was not quiet, at a volume that
  was not low: the codec's output went where the loopback cannot hear, so a
  plug is in;
- anything between: nothing is concluded, and the last answer stands.

Until something has played the answer is None: not known. A plug put in or
pulled out while nothing plays is seen at the next sound.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

SPEAKER, HEADPHONES = "speaker", "headphones"

RISE_DB = 10.0      # the Korvo's loopback idles at -89 dBFS and carries a reply
                    # at -40 or so at volume 60; the volume's 0.5 dB steps leave
                    # 25 dB of that even at volume 10
FLAT_DB = 3.0
AUDIBLE_DBFS = -45.0  # a sound quieter than this over its length proves nothing
MIN_VOLUME = 10       # nor does one played at a volume below this
# Where in time a sound's loopback is: the hub sends SPEAKER_LEAD_S (0.3 s)
# ahead of real time and the satellite buffers about as much, so the sound
# starts on the loopback some 0.3 s after its first frame left, and ends
# around play_until.
LAG_S = 0.3
TAIL_S = 0.2
SETTLE_S = 0.6        # judged once a microphone frame this long after the end has come
MIN_FRAMES = 10       # 200 ms of loopback over the sound
MIN_IDLE = 25         # and 500 ms of it with nothing playing
RECENT = 3000         # 60 s of 20 ms frames: the longest sound /say can queue
IDLE = 250            # the last 5 s of quiet
PENDING = 8


def level_dbfs(x: np.ndarray) -> float:
    """Mean power of int16 samples, in dB below full scale. Digital silence
    is -120, not minus infinity, so a floor of it still subtracts."""
    power = float(np.mean(np.square(x, dtype=np.float64))) / (32768.0 ** 2) if x.size else 0.0
    return 10 * math.log10(power + 1e-12)


@dataclass
class Sound:
    start: float        # time.monotonic() its first frame left, or it was asked for
    end: float          # when it will have played out
    level_dbfs: float   # of the sound itself, over its length
    volume: int | None  # the satellite's volume as it played


class OutputSense:
    """Fed every microphone frame of one satellite and told of every sound
    played on it; says the output when it can."""

    def __init__(self, channels: int, ref_channel: int = 0):
        self.channels = channels
        self.ref_channel = ref_channel
        self.recent: deque[tuple[float, float]] = deque(maxlen=RECENT)
        self.idle: deque[float] = deque(maxlen=IDLE)
        self.pending: deque[Sound] = deque(maxlen=PENDING)
        self.busy_until = 0.0
        self.output: str | None = None
        self.at: float | None = None   # wall clock of the sound that last said so

    def played(self, sound: Sound) -> None:
        """A sound that played in full. Its loopback is judged once it has
        been heard out."""
        self.pending.append(sound)
        self.busy_until = max(self.busy_until, sound.end + TAIL_S)

    def feed(self, pcm: bytes, now: float, playing: bool) -> str | None:
        """One microphone frame, interleaved int16. `playing` is the hub's
        own speaker loop at work. Returns the output when this frame settled
        a sound and the answer changed, else None."""
        a = np.frombuffer(pcm, "<i2")
        n = a.size // self.channels
        if n == 0:
            return None
        db = level_dbfs(a[:n * self.channels].reshape(n, self.channels)[:, self.ref_channel])
        self.recent.append((now, db))
        if not playing and now > self.busy_until:
            self.idle.append(db)
        changed = None
        while self.pending and now >= self.pending[0].end + SETTLE_S:
            said = self.judge(self.pending.popleft())
            if said is not None:
                self.at = time.time()
                if said != self.output:
                    self.output = changed = said
        return changed

    def judge(self, sound: Sound) -> str | None:
        window = [db for t, db in self.recent if sound.start + LAG_S <= t <= sound.end + TAIL_S]
        if len(window) < MIN_FRAMES or len(self.idle) < MIN_IDLE:
            return None
        floor = float(np.median(np.fromiter(self.idle, float)))
        rise = float(np.percentile(np.asarray(window), 90)) - floor
        if rise >= RISE_DB:
            return SPEAKER
        if (rise <= FLAT_DB and sound.level_dbfs >= AUDIBLE_DBFS
                and (sound.volume is None or sound.volume >= MIN_VOLUME)):
            return HEADPHONES
        return None
