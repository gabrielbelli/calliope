"""Wake words and the end of the command that follows one.

    ensure_models(names, model_dir)          fetch missing ONNX files, never twice
    ww = WakeWords({"hey_jarvis": 0.5, "alexa": 0.5}, model_dir)
    ww.feed(pcm) -> [Detection(name, score, sample)]
    ep = Endpointer()
    ep.feed(pcm) -> True when the command is over; ep.audio, ep.had_speech

Everything here takes mono int16 at 16 kHz, in chunks of any length. Both
classes hold per-stream state, so a hub with several nodes makes one of each
per node. Nothing here talks to a node: no lights, no speaker.

WAKE WORDS ARE openWakeWord (openwakeword==0.6.0) ON ONNX RUNTIME. That
release still declares tflite-runtime as a Linux dependency, and tflite-runtime
has never published a wheel newer than CPython 3.11, so a plain
`pip install openwakeword` fails on this image's Python 3.13. Install it with
--no-deps and pin what it really imports: onnxruntime, scipy, scikit-learn,
tqdm and requests. Inference needs only onnxruntime; the other four serve its
training helpers and its own downloader, but `import openwakeword` imports all
of them.

THE MODELS CARRY A LICENCE OF THEIR OWN. openWakeWord's code is Apache-2.0,
but every pre-trained model in its releases, the two feature models included,
is CC BY-NC-SA 4.0: non-commercial use only, with attribution. The hub's image
is built with the default wake word's files in it (the Containerfile, which
also writes the attribution beside them), so that a first start needs no
network; ensure_models() copies them onto the data volume and fetches any
other name there. An image that carries them may be shared only on those
terms, which THIRD-PARTY-NOTICES.md spells out.

Every file ensure_models() fetches is checked against a SHA-256 pinned below
before it is renamed into place, so a truncated or substituted download never
becomes a model, and a file that is present is never fetched again.

THE ENDPOINTER USES webrtcvad (webrtcvad-wheels==2.0.14), NOT A BARE ENERGY
GATE AND NOT openWakeWord's SILERO. Measured on the two "hey jarvis" fixtures
in tests/fixtures, scaled to -17 to -51 dBFS RMS with no added noise, mode 2
marked 80-100% of the frames within 35 dB of the loudest as voiced. On
synthetic white, pink and brown noise it marked none to 2% up to -33 dBFS RMS,
up to 45% at -30 dBFS, and nearly all from -27 dBFS. So in a loud room a
command ends at max_seconds instead of at the pause. Silero ignored the same
noise at every level, but needs a model file passed in, where webrtcvad needs
nothing but the audio.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
import numpy as np
import webrtcvad

log = logging.getLogger("voice-nodes.wakeword")

# ONNX RUNTIME 1.30 REPORTS TO MICROSOFT FROM LINUX UNLESS TOLD NOT TO. Its
# Linux wheel carries the 1DS telemetry client, posting to
# mobile.events.data.microsoft.com and keeping a device id under
# ~/.cache/Microsoft. Measured in this image with that host pointed at a
# listener on the container's own loopback: two connections within 15 s of a
# session, and the id written; with ORT_DISABLE_TELEMETRY=1, none and none.
# ONNX Runtime reads the variable when its environment is created, so it is
# set here, before anything imports it, and the Containerfile sets it as well.
# setdefault, so an operator who wants to send it can still say so.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

RATE = 16000
FRAME = 1280  # openWakeWord scores one 80 ms frame at a time
REFRACTORY_S = 1.5

# The release assets openwakeword 0.6.0 names in openwakeword.MODELS and
# FEATURE_MODELS (as .tflite; the .onnx twins sit beside them). The hashes are
# of the files downloaded on 2026-09-25. "timer" is left out on purpose: it is
# a six-way intent classifier, not a wake word, and its scores are keyed by
# class labels ("5_minute_timer") rather than by its own name.
RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
FEATURES = {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
}
MODELS = {
    "alexa": ("alexa_v0.1.onnx",
              "6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e"),
    "hey_jarvis": ("hey_jarvis_v0.1.onnx",
                   "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
    "hey_mycroft": ("hey_mycroft_v0.1.onnx",
                    "c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18"),
    "hey_rhasspy": ("hey_rhasspy_v0.1.onnx",
                    "5a9b3ed3be2910e35780e097905aa9f35a9c10038df47914cf2b3ec4d670f6ea"),
    "weather": ("weather_v0.1.onnx",
                "8441da8e746899e8d969528d5bad5651cdd563079c05962788f77753041f60e7"),
}

# A model name becomes a file name, and names arrive from configuration, so no
# dots and no slashes: nothing can point outside model_dir.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _mono16(pcm: np.ndarray) -> np.ndarray:
    # Refuse rather than convert: a float array in [-1, 1] cast to int16 is
    # silence, and a stereo array flattened is the wrong rate. Both would fail
    # quietly as "never hears the wake word".
    if not isinstance(pcm, np.ndarray) or pcm.dtype != np.int16 or pcm.ndim != 1:
        raise TypeError("expected mono int16 audio as a 1-D numpy array, got "
                        f"{getattr(pcm, 'dtype', type(pcm).__name__)} "
                        f"with shape {getattr(pcm, 'shape', '?')}")
    return pcm


def _model_file(name: str, model_dir: Path) -> Path:
    """A built-in name's pinned file, or <model_dir>/<name>.onnx for a model
    someone trained and dropped into the volume."""
    if not NAME.match(name):
        raise ValueError(f"{name!r} is not a model name")
    return model_dir / (MODELS[name][0] if name in MODELS else f"{name}.onnx")


def _fetch(url: str) -> bytes:
    # GitHub answers release assets with a redirect to its object store.
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


def _seeded(file: str, sha256: str, seeds: Iterable[Path]) -> bytes | None:
    """The file from the first seed directory holding it with the pinned hash.
    A copy with another hash is passed over rather than trusted: a seed is a
    directory in an image, and an image can be older than this table."""
    for seed in seeds:
        path = Path(seed) / file
        if path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == sha256:
                return data
            log.warning("wake word model %s ignored: its SHA-256 is not the pinned one", path)
    return None


def ensure_models(names: Iterable[str], model_dir: str | Path, *,
                  seeds: Iterable[str | Path] = ()) -> None:
    """Make sure the named models, and the two feature models every wake word
    shares, are in model_dir. A file that is present is not fetched again, so
    this is cheap at every start-up and needs no network once the volume is
    populated. A missing file is copied from the first of `seeds` that has it
    (the hub passes the directory its image was built with), and only fetched
    when none has. Safe to run at image build time as well:

        python -m app.wakeword /data/wakewords hey_jarvis alexa hey_mycroft
    """
    model_dir = Path(model_dir)
    seeds = [Path(s) for s in seeds if Path(s) != model_dir]
    wanted = dict(FEATURES)
    for name in names:
        path = _model_file(name, model_dir)
        if name in MODELS:
            wanted[path.name] = MODELS[name][1]
        elif not path.is_file():
            raise ValueError(f"{name!r} is not a built-in wake word "
                             f"({', '.join(sorted(MODELS))}) and {path} does not exist")
    model_dir.mkdir(parents=True, exist_ok=True)
    for file, sha256 in wanted.items():
        dest = model_dir / file
        if dest.is_file():
            continue
        data = _seeded(file, sha256, seeds)
        if data is None:
            data = _fetch(RELEASE + file)
            got = hashlib.sha256(data).hexdigest()
            if got != sha256:
                raise RuntimeError(f"{RELEASE + file} has SHA-256 {got}, expected {sha256}; "
                                   "not installed")
        # Written beside the target and renamed, so an interrupted download
        # leaves no file for the is_file() check above to mistake for a model.
        part = dest.with_name(f".{file}.{os.getpid()}.part")
        try:
            part.write_bytes(data)
            os.replace(part, dest)
        finally:
            part.unlink(missing_ok=True)
        log.info("wake word model %s: %d bytes into %s", file, len(data), model_dir)


@dataclass(frozen=True)
class Detection:
    name: str       # the model name as given to WakeWords
    score: float    # of the frame that fired, not the peak
    sample: int     # stream position at the end of that frame, in samples
                    # since construction or reset(); audio after it is the command


class WakeWords:
    """Several openWakeWord models on one stream, each with its own threshold.

    A model fires on the first 80 ms frame at or above its threshold, then
    stays quiet until its score has dropped below the threshold AND
    refractory_s of audio has passed. On both fixtures hey_jarvis stayed at or
    above 0.5 for seven frames in a row (560 ms), so something has to hold it
    off. The refractory window alone would let a run that outlasts it fire
    again; the re-arm rule alone would let a one-frame dip inside the run fire
    again.

    Time is counted in samples fed, not by the clock, so a stalled or bursty
    connection neither shortens nor stretches the window.

    Measured on darwin (Apple silicon, onnxruntime 1.30.0): 2.7 ms of one core
    per 80 ms frame (3.4%) with one model or three, because the shared feature
    models are nearly all of it; about 50 MB of memory per instance; 160 ms to
    construct once the first import (about 1 s) has been paid.
    """

    def __init__(self, models: dict[str, float], model_dir: str | Path, *,
                 refractory_s: float = REFRACTORY_S):
        if not models:
            raise ValueError("no wake word models given")
        for name, threshold in models.items():
            if not 0 < threshold <= 1:
                raise ValueError(f"threshold for {name!r} is {threshold}; it must be in (0, 1]")
        model_dir = Path(model_dir)
        paths = {name: _model_file(name, model_dir) for name in models}
        missing = [str(p) for p in [*(model_dir / f for f in FEATURES), *paths.values()]
                   if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"missing wake word model files {missing}; "
                                    "run ensure_models() for these names first")

        # Imported here, not at the top: it pulls in scipy and scikit-learn and
        # takes about a second, which nothing but a WakeWords should pay.
        from openwakeword.model import Model

        self._model = Model(wakeword_models=[str(p) for p in paths.values()],
                            inference_framework="onnx",
                            melspec_model_path=str(model_dir / "melspectrogram.onnx"),
                            embedding_model_path=str(model_dir / "embedding_model.onnx"))
        # openWakeWord keys scores by file stem ("hey_jarvis_v0.1").
        self._keys = {name: p.stem for name, p in paths.items()}
        for name, key in self._keys.items():
            if self._model.model_outputs[key] != 1:
                raise ValueError(f"{name!r} has {self._model.model_outputs[key]} outputs; "
                                 "only single-output wake word models are supported")
        self.thresholds = dict(models)
        self._refractory = int(refractory_s * RATE)
        # openWakeWord fills its window with embeddings of random noise from
        # numpy's global generator on construction and on every reset. Until
        # real audio has pushed all of them out, scores are random too: with
        # hey_jarvis, alexa and hey_mycroft loaded and a -60 dBFS noise floor
        # fed after each of 600 resets, alexa scored over 0.5 eight times, all
        # in the first 1.2 s. From 1.2 s on, the same audio gave identical
        # scores after every reset.
        self._warmup = (max(self._model.model_inputs[k] for k in self._keys.values()) - 1) * FRAME
        self.reset()

    @staticmethod
    def available() -> list[str]:
        """The built-in names ensure_models() can fetch and WakeWords can load.
        "ptt" is not among them and never can be: it is the name push-to-talk
        gives the wake word a button stands in for."""
        return sorted(MODELS)

    def clone(self) -> WakeWords:
        """A second stream on the models this one loaded, for another node.

        openWakeWord keeps a stream's audio buffers inside the same Model that
        holds its ONNX sessions, so it has no way to share models between
        streams. The copy shares the sessions (ONNX Runtime runs one session
        from several threads at once) and gets buffers of its own. Measured on
        darwin with two wake words: 57 MB for the first stream, 55 MB for each
        further one built from scratch, 3.6 MB for each clone.

        The thresholds dict is shared as well, so a change reaches every node.
        This leans on openwakeword 0.6.0's internals (Model.preprocessor and
        its raw_data_buffer); the version is pinned, and a test feeds a clone
        and a fresh instance the same audio and compares every score.
        """
        twin = copy.copy(self)
        model = copy.copy(self._model)
        features = copy.copy(self._model.preprocessor)
        # reset() clears this deque in place rather than replacing it, so a
        # shared one would let one node's reset wipe another node's audio.
        features.raw_data_buffer = deque(maxlen=features.raw_data_buffer.maxlen)
        model.preprocessor = features
        twin._model = model
        twin.reset()
        return twin

    def reset(self) -> None:
        """Forget the stream. Nothing fires in the first 1.2 s of audio after
        this (the warm-up above), so reset between streams, not between
        utterances."""
        self._model.reset()
        self._pending = np.empty(0, dtype=np.int16)
        self.position = 0     # samples given to feed()
        self._scored = 0      # samples openWakeWord has scored, a multiple of FRAME
        self._armed = dict.fromkeys(self._keys, True)
        self._quiet_until = dict.fromkeys(self._keys, 0)

    def feed(self, pcm: np.ndarray) -> list[Detection]:
        """The detections this chunk completed, in stream order."""
        x = _mono16(pcm)
        self.position += len(x)
        if self._pending.size:
            x = np.concatenate((self._pending, x))
        # Whole frames only. openWakeWord would buffer a remainder itself, but
        # then scores the max over however many frames a call completes, and
        # the debounce and Detection.sample both need one score per frame.
        whole = len(x) - len(x) % FRAME
        found = []
        for off in range(0, whole, FRAME):
            scores = self._model.predict(x[off:off + FRAME])
            self._scored += FRAME
            if self._scored <= self._warmup:
                continue
            for name, key in self._keys.items():
                score = float(scores[key])
                if score < self.thresholds[name]:
                    self._armed[name] = True
                elif self._armed[name] and self._scored >= self._quiet_until[name]:
                    self._armed[name] = False
                    self._quiet_until[name] = self._scored + self._refractory
                    found.append(Detection(name, score, self._scored))
        self._pending = x[whole:].copy()
        return found


class Endpointer:
    """Collects the command after a wake word and says when it is over.

    feed() returns True once, and on every call after, when one of these has
    happened (reason says which):

        "silence"     speech, then silence_ms without it
        "max_length"  max_seconds of audio fed, counted from the first feed()
        "no_speech"   start_timeout_s of audio fed and speech never started

    There is speech while at least 200 ms of the last 300 ms is voiced, and
    silence_ms is counted from the last voiced frame of such a stretch. One rule
    serves both ends because of two things measured on webrtcvad in mode 2: a
    fresh instance marked the first 80 ms of every noise floor tried (-60 to
    -36 dBFS) voiced while it adapted, and it holds every voiced decision for
    about four more frames, so a 20 ms click in a pause comes out as 80-100 ms
    voiced. Both stay under 200 ms,
    so neither starts a command, and a click in the pause does not restart the
    silence count unless it starts within 120 ms of the last voiced frame.

    webrtcvad learns the noise floor only from frames it calls unvoiced, so a
    floor that rises mid-command reads as speech until it catches up: 2.3 s
    for a step from -60 to -45 dBFS, about 5 s for -60 to -40, and 3.5 s for
    a -60 dBFS floor straight after speech that faded to digital silence. The
    command then ends that much late, or at max_seconds. Feed it the room as
    the microphone heard it, never audio with exact zeros spliced in.

    audio is the speech with 300 ms either side, trimmed from what was fed,
    and empty when had_speech is False: there is nothing to transcribe, and
    silence given to Whisper-style models comes back as invented text.
    """

    FRAME_MS = 20        # the node's own frame length
    ONSET_WINDOW = 15    # frames: 300 ms
    ONSET_VOICED = 10    # frames: 200 ms
    PAD_MS = 300

    def __init__(self, rate: int = RATE, max_seconds: float = 10.0, silence_ms: int = 800,
                 start_timeout_s: float = 4.0, *, vad_mode: int = 2):
        if rate not in (8000, 16000, 32000, 48000):
            raise ValueError(f"webrtcvad takes 8, 16, 32 or 48 kHz, not {rate} Hz")
        # Mode 2, not 3: 3 rejects more loud noise, but with the fixtures at
        # -47 to -51 dBFS it missed 44-46% of the frames mode 2 is scored on
        # above, where mode 2 missed 4-20%. A command cut off is worse than
        # one that runs long.
        self._vad = webrtcvad.Vad(vad_mode)
        self.rate = rate
        self._n = rate * self.FRAME_MS // 1000
        self._max_frames = math.ceil(max_seconds * 1000 / self.FRAME_MS)
        self._timeout_frames = math.ceil(start_timeout_s * 1000 / self.FRAME_MS)
        self._silence_frames = max(1, math.ceil(silence_ms / self.FRAME_MS))
        self._pad = self.PAD_MS // self.FRAME_MS
        self._pending = np.empty(0, dtype=np.int16)
        self._pcm = bytearray()
        self._frames = 0
        self._recent: deque[bool] = deque(maxlen=self.ONSET_WINDOW)
        self._voiced = -1                   # last frame webrtcvad called voiced
        self._start: int | None = None      # first voiced frame of the first speech
        self._last_speech = -1              # last voiced frame inside speech
        self.reason: str | None = None

    @property
    def had_speech(self) -> bool:
        return self._start is not None

    @property
    def audio(self) -> bytes:
        if self._start is None:
            return b""
        first = max(0, self._start - self._pad)
        last = min(self._frames, self._last_speech + 1 + self._pad)
        return bytes(self._pcm[first * self._n * 2:last * self._n * 2])

    def feed(self, pcm: np.ndarray) -> bool:
        x = _mono16(pcm)
        if self.reason is not None:
            return True
        if self._pending.size:
            x = np.concatenate((self._pending, x))
        n = self._n
        whole = len(x) - len(x) % n
        for off in range(0, whole, n):
            frame = x[off:off + n].tobytes()
            self._pcm += frame
            i = self._frames
            self._frames += 1
            voiced = self._vad.is_speech(frame, self.rate)
            self._recent.append(voiced)
            if voiced:
                self._voiced = i
            if sum(self._recent) >= self.ONSET_VOICED:
                if self._start is None:
                    self._start = i - len(self._recent) + 1 + self._recent.index(True)
                self._last_speech = self._voiced
            if self._start is not None and i - self._last_speech >= self._silence_frames:
                self.reason = "silence"
            elif self._frames >= self._max_frames:
                self.reason = "max_length"
            elif self._start is None and self._frames >= self._timeout_frames:
                self.reason = "no_speech"
            if self.reason is not None:
                self._pending = np.empty(0, dtype=np.int16)
                return True
        self._pending = x[whole:].copy()
        return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        sys.exit("usage: python -m app.wakeword MODEL_DIR NAME [NAME ...]\n"
                 f"built-in names: {', '.join(WakeWords.available())}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ensure_models(sys.argv[2:], sys.argv[1])
