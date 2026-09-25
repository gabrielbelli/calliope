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
works and the reply plays. The wake word model is fed in every state, so its
stream never has a hole in it (openWakeWord scores a sliding window, and a
window straddling a gap scores audio that never happened); a detection outside
"idle" is dropped unless main.py has made the Ear `interruptible`. main.py
moves "busy" back to "idle" when the conversation ends, between two process()
calls, never during one.

A CONVERSATION ASKS FOR MORE, all by flags that process() reads at its start,
because main.py sets them from the event loop while process() may be running
in its thread:

  * follow_up(): listen again with no wake word, once the satellite's own
    voice has left its loopback channel for FOLLOW_UP_GUARD_S; a turn that
    does not start within the timeout comes back as Command "no_speech";
  * watch(voice=True): barge-in. While the reply plays, sustained speech on
    the echo-cancelled output (BargeInDetector) is reported as BargeIn and,
    with `capture`, the Ear starts collecting what is being said from BARGE_
    PREROLL_S before the detection, so its first syllables are kept;
  * interruptible: a wake word heard in any state is reported, and starts a
    new command after it;
  * triggers: words that are the whole command. Heard is reported and the
    Ear listens for nothing after them.

Barge-in by voice needs the front-end: without echo cancellation there is no
telling the satellite's own voice from the talker's, so with
SATELLITES_FRONTEND=0, or one channel, only a wake word interrupts.

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
from collections import deque
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
    # Only with debug_s: what the Ear heard around this command, processed and
    # raw (first microphone), as mono s16le 16 kHz.
    debug: dict | None = None

    @property
    def seconds(self) -> float:
        return len(self.audio) / 2 / RATE


@dataclass(frozen=True)
class BargeIn:
    """Someone spoke over the reply. `at_s` is the stream position where it
    was detected; `capturing` says the Ear is collecting the utterance."""

    at_s: float
    capturing: bool


# Enough to rewind by any detector's latency (wakeword.WakeWords.latency_s).
HISTORY_S = 2.0

# BARGE-IN, on the front-end's own decision for each 16 ms block (FrontEnd.
# blocks): voiced, whether the satellite's speaker is playing, and the
# block's a posteriori SNR against the tracked noise PLUS the canceller's
# model of the echo it left behind.
#
# Measured on synthetic scenes (tests/test_listening.py, and the notes in the
# README): the canceller removes a linear echo (27-38 dB ERLE) and the
# satellite's own voice never raised a voiced block once it had converged.
# A loudspeaker that distorts is another matter: with its peaks clipped (tanh
# at -14 to -24 dBFS) the linear model leaves 4-12 dB of ERLE, and the
# front-end's usual bar (SNR 2, 3 dB) was cleared by 32-42 % of the playback's
# blocks. The same blocks never reached 9 dB, while a talker at -36 dBFS over
# the same playback had a median of 13.7-19 dB. So while the satellite plays,
# a block counts only at BARGE_SNR_FAR; with nothing playing, at the front-end's
# own bar.
BARGE_SNR_FAR = 10.0     # 10 dB, linear, as FrontEnd reports it
# Sustained speech, not a cough or a clatter: BARGE_VOICED of the last
# BARGE_WINDOW blocks, 288 ms voiced within 480 ms. Measured: a talker at -30
# to -42 dBFS over the reply was caught 0.30-0.37 s after they began; the
# clipped self-echo reached at most 4 of the 18 needed.
BARGE_WINDOW = 30
BARGE_VOICED = 18
# Kept from before the detection: the window it took to decide, and a margin.
BARGE_PREROLL_S = 0.6
# How long the loopback must have been quiet before a follow-up is listened
# to, so the reply's last syllable and the room's echo of it are not taken for
# the talker's next turn: without the front-end nothing else removes them.
FOLLOW_UP_GUARD_S = 0.25
# The loopback is the satellite playing when it is louder than this (the
# Korvo's idle loopback measured -89 dBFS; frontend.FAR_END_DBFS).
LOOPBACK_DBFS = -60.0


class BargeInDetector:
    """BARGE_VOICED voiced blocks in the last BARGE_WINDOW, counting a block
    of playback only at BARGE_SNR_FAR. feed() returns the index, in the
    blocks given, of the block that completed the count, or None."""

    def __init__(self) -> None:
        self.recent: deque[bool] = deque(maxlen=BARGE_WINDOW)

    def reset(self) -> None:
        self.recent.clear()

    def feed(self, blocks: list[tuple[bool, bool, float]]) -> int | None:
        for k, (voiced, far, snr) in enumerate(blocks):
            self.recent.append(voiced and (snr >= BARGE_SNR_FAR if far else True))
            if sum(self.recent) >= BARGE_VOICED:
                self.recent.clear()
                return k
        return None


class Ear:
    def __init__(self, *, rate: int = RATE, channels: int = 4, frontend: bool = True,
                 wake: WakeWords | None = None, debug_s: float = 0.0):
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
        # SATELLITES_DEBUG_AUDIO: the last debug_s of output and of the first
        # microphone, attached to each Command so a missed command can be
        # listened to instead of guessed at. Off (0) unless asked for.
        self.debug_s = debug_s
        self._dbg_proc = np.zeros(0, np.int16)
        self._dbg_raw = np.zeros(0, np.int16)
        # Set by main.py for a conversation (see the module docstring).
        self.interruptible = False
        self.triggers: frozenset[str] = frozenset()
        self._voice = False
        self._capture: tuple[int, float] | None = None
        self._follow: tuple[int, float] | None = None
        self._guard = 0           # samples of quiet loopback a follow-up still waits for
        self._turn_settings: tuple[int, float] | None = None
        self._barge = BargeInDetector()
        self._loop_floor = rate * 0.02 * (32768.0 * 10 ** (LOOPBACK_DBFS / 20)) ** 2

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
        self.unwatch()
        self._follow = None
        self._guard = 0

    def follow_up(self, silence_ms: int, timeout_s: float) -> None:
        """Listen for the next turn, with no wake word before it, at the next
        process(): a pause of silence_ms ends it, and timeout_s without it
        starting is Command "no_speech"."""
        self._follow = (silence_ms, timeout_s)

    def watch(self, *, voice: bool, capture: tuple[int, float] | None = None) -> None:
        """Barge-in by voice while a reply plays. `capture` (silence_ms,
        timeout_s) collects what the talker says as the next command; None
        only reports it."""
        if voice and not self._voice:
            self._barge.reset()
        self._voice, self._capture = voice, capture

    def unwatch(self) -> None:
        self._voice, self._capture = False, None
        self.interruptible = False

    def set_silence(self, silence_ms: int) -> None:
        ep = self.endpointer
        if ep is not None:
            ep.set_silence(silence_ms)

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

    def process(self, frames: np.ndarray) -> list[Heard | Command | BargeIn]:
        """int16 (n, channels) in; what happened in them out, in order."""
        if self.frontend:
            mono = self.frontend.process(frames)
        else:
            mono = np.ascontiguousarray(frames[:, self.first_mic], dtype=np.int16)
        start = self.samples
        self.samples += len(mono)
        mono_all = mono
        events: list[Heard | Command | BargeIn] = []
        if self.debug_s:
            keep = int(self.debug_s * self.rate)
            self._dbg_proc = np.concatenate((self._dbg_proc, mono))[-keep:]
            raw = np.ascontiguousarray(frames[:, self.first_mic], dtype=np.int16)
            self._dbg_raw = np.concatenate((self._dbg_raw, raw))[-keep:]

        word, self._ptt = self._ptt, None
        if word:
            if self.state == "idle":
                events.append(self._listen(word, None, start))
                # The whole chunk is the command: the button was pressed
                # before these samples reached the hub.
                self._endpoint(mono, events)
                self._feed_wake(mono)
                return events

        follow, self._follow = self._follow, None
        if follow is not None and self.state in ("busy", "idle"):
            self.state = "listening"
            self.endpointer = self._turn(follow)
            self._guard = int(FOLLOW_UP_GUARD_S * self.rate) if self.channels > 1 else 0
        interruptible = self.interruptible
        voice, capture = self._voice and self.frontend is not None, self._capture

        found = self._feed_wake(mono)
        if found and (self.state == "idle" or interruptible):
            d = found[0]
            if d.name in self.triggers:
                # The word is the whole command. A follow-up that was
                # listening starts again, so the word is not its next turn.
                events.append(Heard(d.name, round(d.score, 3), self._direction(),
                                    round((start + len(mono)) / self.rate, 3)))
                if self.state == "listening" and self._turn_settings is not None:
                    self.endpointer = self._turn(self._turn_settings)
            else:
                # Everything after the frame that fired belongs to the
                # command, and so does the detector's own latency before it:
                # a real model fires well after the word ends, by which time
                # the command has begun.
                self._voice = False
                after = max(0, min(len(mono), self.wake.position - d.sample))
                rewind = int(getattr(self.wake, "latency_s", 0.0) * self.rate)
                before = np.concatenate((self._history, mono[:len(mono) - after]))[-rewind:] \
                    if rewind else np.zeros(0, np.int16)
                events.append(self._listen(d.name, d.score, start + len(mono) - after))
                if len(before) and self.endpointer is not None:
                    self.endpointer.preroll(before)
                self._endpoint(mono[len(mono) - after:], events)
        elif voice and self.state == "busy":
            k = self._barge.feed(self.frontend.blocks)
            if k is not None:
                cut = min(len(mono), (k + 1) * self.frontend.hop)
                self._voice = False  # once per reply
                events.append(BargeIn(round((start + cut) / self.rate, 3), capture is not None))
                if capture is not None:
                    self.state = "listening"
                    self.endpointer = self._turn(capture)
                    keep = int(BARGE_PREROLL_S * self.rate)
                    self._endpoint(np.concatenate((self._history, mono[:cut]))[-keep:], events)
                    self._endpoint(mono[cut:], events)
        elif self.state == "listening":
            if self._guard > 0:
                mono = self._after_guard(frames, mono)
            if len(mono):
                self._endpoint(mono, events)
        self._history = np.concatenate((self._history, mono_all))[-int(HISTORY_S * self.rate):]
        return events

    def _after_guard(self, frames: np.ndarray, mono: np.ndarray) -> np.ndarray:
        """What of this chunk comes after the loopback has been quiet for the
        guard: nothing until then. With one channel there is no loopback, and
        no guard (follow_up sets none)."""
        ref = frames[:, 0].astype(np.float64)
        n = self.rate // 50  # the satellite's own 20 ms
        loud = [i for i in range(0, len(ref) - n + 1, n)
                if float(ref[i:i + n] @ ref[i:i + n]) > self._loop_floor]
        quiet_tail = len(ref) - (loud[-1] + n) if loud else None
        if quiet_tail is None:
            self._guard -= len(ref)
        else:
            self._guard = int(FOLLOW_UP_GUARD_S * self.rate) - quiet_tail
        if self._guard > 0:
            return mono[:0]
        take = min(len(mono), -self._guard)
        self._guard = 0
        return mono[len(mono) - take:] if take else mono[:0]

    def _direction(self) -> float | None:
        return None if self.direction is None else round(self.direction, 1)

    def _turn(self, settings: tuple[int, float]) -> Endpointer:
        """An endpointer for a turn with no wake word before it."""
        self._turn_settings = settings
        return Endpointer(rate=self.rate, silence_ms=settings[0], start_timeout_s=settings[1],
                          reopen=False)

    def _feed_wake(self, mono: np.ndarray) -> list:
        return self.wake.feed(mono) if self.wake is not None else []

    def _listen(self, word: str, score: float | None, at: int) -> Heard:
        self.state = "listening"
        self._turn_settings = None
        self.endpointer = Endpointer(rate=self.rate)
        return Heard(word, None if score is None else round(score, 3),
                     None if self.direction is None else round(self.direction, 1),
                     round(at / self.rate, 3))

    def _endpoint(self, mono: np.ndarray, events: list) -> None:
        ep = self.endpointer
        if ep is not None and len(mono) and ep.feed(mono):
            debug = ({"processed": self._dbg_proc.tobytes(), "raw": self._dbg_raw.tobytes()}
                     if self.debug_s else None)
            events.append(Command(ep.audio, ep.reason or "silence", ep.had_speech, debug))
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
