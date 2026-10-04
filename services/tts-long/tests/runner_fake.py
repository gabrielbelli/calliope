"""A Synth for the runner's spawned worker, with no torch and no weights.

THE WORKER IS REAL AND THIS IS THE ONLY THING THAT IS NOT. The runner tests
start the actual worker process with multiprocessing's `spawn`, through the
actual pipe, writing the actual files; `factory="runner_fake:FakeSynth"` is
the one difference from production. Under spawn the child inherits the
parent's sys.path, which is how it finds this module beside the tests.

What a segment's text makes it do:

    ordinary text   0.1 for len(text) * 100 samples, input_tokens = word count
    __crash__       os._exit(3), the worker dying under a job
    __sleep:N__     sleeps N seconds, then speaks as ordinary text
    __raise__       ValueError("bad clip"): an ordinary failure
    __cuda__        RuntimeError naming CUDA: a failure that poisons the process

EVERY GENERATION IS WRITTEN TO $FAKE_SYNTH_LOG as `pid engine text`, and an
atexit hook writes `pid exit`, so a test can count generations and order
processes across the process boundary.
"""

from __future__ import annotations

import atexit
import os
import re
import time

import numpy as np

_SLEEP = re.compile(r"__sleep:(\d+(?:\.\d+)?)__")


def _log(line: str) -> None:
    path = os.environ.get("FAKE_SYNTH_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class Spoken:
    def __init__(self, audio, input_tokens):  # noqa: ANN001
        self.audio = audio
        self.input_tokens = input_tokens


class FakeSynth:
    """`Synth`'s constructor and `speak_segments` signature, and nothing else."""

    def __init__(self, idle_timeout: float = 600.0, threads: int = 8,
                 spec=None, device: str = "cpu") -> None:  # noqa: ANN001
        self.idle_timeout = idle_timeout
        self.threads = threads
        self.spec = spec
        self.device = device
        self.loads = 0
        self.load_seconds = 0.0
        pid = os.getpid()
        atexit.register(_log, f"{pid} exit")

    def speak_segments(self, segments, language, controls, reference=None,  # noqa: ANN001
                       on_chunk=None, cancelled=None) -> Spoken:  # noqa: ANN001
        parts = []
        tokens = 0
        for text, _pause in segments:
            if not text.strip():
                continue
            if self.loads == 0:
                self.loads, self.load_seconds = 1, 0.01
            _log(f"{os.getpid()} {self.spec.id} {text}")
            if text == "__crash__":
                os._exit(3)
            if text == "__raise__":
                raise ValueError("bad clip")
            if text == "__cuda__":
                raise RuntimeError("CUDA error: an illegal memory access was "
                                   "encountered")
            slept = _SLEEP.search(text)
            if slept:
                time.sleep(float(slept.group(1)))
            parts.append(np.full(len(text) * 100, 0.1, dtype=np.float32))
            tokens += len(text.split())
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return Spoken(audio, tokens)
