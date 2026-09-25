"""What one satellite's microphones go through on the hub, between the socket
and the router:

    4-channel frames ──FrontEnd──▶ mono 16 kHz ──WakeWords──▶ "hey_jarvis"
                                        └──────Endpointer──▶ the command

    ear = Ear(rate=16000, channels=4, frontend=True, wake=ww)
    for ev in ear.process(frames):     Heard (a wake word, or push-to-talk)
        ...                            Command (the audio after it, ended)

An Ear is plain synchronous CPU work with state, one per satellite, and it
never talks to anything: main.py runs process() in a thread pool, one call at a
time per satellite, and does everything a satellite can see or hear (lights,
earcons, ducking, the reply) on the event loop. That split is also what keeps
the lights promise checkable in one place: nothing in this file can send a
satellite anything.

THREE STATES. "idle" listens for a wake word; "listening" feeds the endpointer
after one; "busy" is the conversation after the endpoint, while the router
works. The wake word model is fed in every state, so its stream never has a
hole in it (openWakeWord scores a sliding window, and a window straddling a gap
scores audio that never happened); a detection outside "idle" is dropped.
main.py moves "busy" back to "idle" when the conversation ends, between two
process() calls, never during one.

WITHOUT THE FRONT-END (SATELLITES_FRONTEND=0) the mono stream is the first
microphone, raw: the channel after the loopback reference.

Measured with the two "hey jarvis" fixtures played to three simulated
microphones under white noise, through FrontEnd then WakeWords and Endpointer
(darwin, 2026-09-25): detected on both at every floor from -60 to -27 dBFS, the
command ended on "silence" 0.4 to 0.7 s after the speech did; on the raw first
microphone at -27 dBFS the endpointer never ended at all, because webrtcvad
takes that much noise for speech.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from .frontend import FrontEnd
from .wakeword import Endpointer, WakeWords

log = logging.getLogger("voice-satellites.listening")

RATE = 16000  # what the wake word models and the router take
PTT = "ptt"   # the wake word name push-to-talk stands in for


@dataclass(frozen=True)
class Heard:
    wake_word: str
    score: float | None       # None for push-to-talk
    direction: float | None   # FrontEnd.direction at the time, degrees, or None
    at_s: float               # stream position of the end of the wake word, s


@dataclass(frozen=True)
class Command:
    audio: bytes              # mono s16le 16 kHz; b"" when nothing was said
    reason: str               # the Endpointer's: silence | max_length | no_speech
    had_speech: bool

    @property
    def seconds(self) -> float:
        return len(self.audio) / 2 / RATE


# Enough to rewind by any detector's latency (wakeword.WakeWords.latency_s).
HISTORY_S = 2.0

class Ear:
    def __init__(self, *, rate: int = RATE, channels: int = 4, frontend: bool = True,
                 wake: WakeWords | None = None):
        if rate != RATE:
            # The wake word models and the router are 16 kHz only; resampling
            # a microphone here would hide a satellite that is misconfigured.
            raise ValueError(f"the microphones run at {rate} Hz; listening needs {RATE}")
        self.rate = rate
        self.channels = channels
        # Channel 0 is the speaker loopback on every satellite that has more
        # than one channel; with one channel there is nothing to cancel
        # against.
        self.first_mic = 1 if channels > 1 else 0
        self.frontend = (FrontEnd(rate=rate, ref_channel=0, mic_channels=tuple(range(1, channels)))
                         if frontend and channels > 1 else None)
        self.wake = wake
        # Which detector `wake` is, in main.Voice.plan's terms. The hub swaps
        # the detector between two process() calls when the words assigned
        # to this satellite change, and compares this to know when to; an
        # Ear rebuilt after a fault starts at None, so it gets one at once.
        self.wake_key: object = None
        self.state = "idle"
        self.endpointer: Endpointer | None = None
        self.samples = 0          # mono samples produced since this Ear was made
        # The last HISTORY_S of output, for rewinding to where a late
        # detector's word actually ended (WakeWords.latency_s).
        self._history = np.zeros(0, np.int16)
        self._ptt: str | None = None

    # ---- called from the event loop, between process() calls ------------------

    def push_to_talk(self, word: str = PTT) -> None:
        """Start listening at the next process() as if `word` had been heard.
        A flag rather than a state change, because process() may be running
        in its thread right now; it reads and clears the flag itself."""
        self._ptt = word

    def cancel_ptt(self) -> None:
        self._ptt = None

    def release(self) -> None:
        """The conversation is over (or was cancelled): back to wake words."""
        self.state = "idle"
        self.endpointer = None

    @property
    def direction(self) -> float | None:
        return self.frontend.direction if self.frontend else None

    def stats(self) -> dict:
        """For GET /satellites. Read on the event loop while process() may be
        running in its thread, so it takes only scalars, and a read that
        catches the front-end mid-reset reports nothing rather than failing
        the whole listing."""
        s = {"state": self.state, "seconds": round(self.samples / self.rate, 1),
             "frontend": self.frontend is not None, "wake_words": self.wake is not None}
        if self.frontend:
            try:
                fe = self.frontend.stats
                s |= {k: fe[k] for k in ("rtf", "erle_db", "beamformer", "far_end")}
                s["direction"] = self.direction
            except Exception:
                pass
        return s

    # ---- in the thread pool --------------------------------------------------

    def process(self, frames: np.ndarray) -> list[Heard | Command]:
        """int16 (n, channels) in; what happened in them out, in order."""
        if self.frontend:
            mono = self.frontend.process(frames)
        else:
            mono = np.ascontiguousarray(frames[:, self.first_mic], dtype=np.int16)
        start = self.samples
        self.samples += len(mono)
        events: list[Heard | Command] = []

        word, self._ptt = self._ptt, None
        if word:
            if self.state == "idle":
                events.append(self._listen(word, None, start))
                # The whole chunk is the command: the button was pressed
                # before these samples reached the hub.
                self._endpoint(mono, events)
                self._feed_wake(mono)
                return events

        found = self._feed_wake(mono)
        if self.state == "idle" and found:
            d = found[0]
            # Everything after the frame that fired belongs to the command, and
            # so does the detector's own latency before it: a real model fires
            # well after the word ends, by which time the command has begun.
            after = max(0, min(len(mono), self.wake.position - d.sample))
            rewind = int(getattr(self.wake, "latency_s", 0.0) * self.rate)
            before = np.concatenate((self._history, mono[:len(mono) - after]))[-rewind:] \
                if rewind else np.zeros(0, np.int16)
            events.append(self._listen(d.name, d.score, start + len(mono) - after))
            if len(before) and self.endpointer is not None:
                self.endpointer.preroll(before)
            self._endpoint(mono[len(mono) - after:], events)
        elif self.state == "listening":
            self._endpoint(mono, events)
        self._history = np.concatenate((self._history, mono))[-int(HISTORY_S * self.rate):]
        return events

    def _feed_wake(self, mono: np.ndarray) -> list:
        return self.wake.feed(mono) if self.wake is not None else []

    def _listen(self, word: str, score: float | None, at: int) -> Heard:
        self.state = "listening"
        self.endpointer = Endpointer(rate=self.rate)
        return Heard(word, None if score is None else round(score, 3),
                     None if self.direction is None else round(self.direction, 1),
                     round(at / self.rate, 3))

    def _endpoint(self, mono: np.ndarray, events: list) -> None:
        ep = self.endpointer
        if ep is not None and len(mono) and ep.feed(mono):
            events.append(Command(ep.audio, ep.reason or "silence", ep.had_speech))
            self.state = "busy"
            self.endpointer = None


def room_floor(seconds: float, dbfs: float = -60.0, seed: int = 0) -> np.ndarray:
    """A microphone's noise floor, to follow a clip that ends before its
    command has. Never digital zeros: webrtcvad holds "speech" for 3.5 s on
    exact silence after speech (wakeword.Endpointer)."""
    rng = np.random.default_rng(seed)
    return np.clip(rng.standard_normal(int(seconds * RATE)) * 32768 * 10 ** (dbfs / 20),
                   -32768, 32767).astype(np.int16)


def parse_wake_words(spec: str) -> dict[str, float]:
    """SATELLITES_WAKE_WORDS: "hey_jarvis:0.5,alexa:0.6", which seeds
    wake_words.json on a first start (wakewords_config.py). A name with no
    threshold gets 0.5, and an empty value means no wake words at all
    (push-to-talk and /inject with ?wake_word= still work). Raises
    ValueError saying what is wrong, for the health check to repeat."""
    out: dict[str, float] = {}
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        name, _, threshold = part.partition(":")
        name = name.strip()
        if not name:
            raise ValueError(f"{part!r} has no wake word name")
        if name == PTT:
            raise ValueError(f"{PTT!r} is push-to-talk's name, not a wake word model")
        try:
            value = float(threshold) if threshold.strip() else 0.5
        except ValueError:
            raise ValueError(f"the threshold in {part!r} is not a number") from None
        if not 0 < value <= 1:
            raise ValueError(f"the threshold in {part!r} must be in (0, 1]")
        out[name] = value
    return out
