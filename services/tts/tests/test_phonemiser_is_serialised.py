"""One espeak backend, one thread at a time.

MEASURED BEFORE IT WAS FIXED, in this service's own environment: 320 calls to
`Tokenizer.phonemize` from 8 threads, against a map built single-threaded, gave
13 correct, 195 that returned ANOTHER TEXT'S PHONEMES, and 112 that raised
"number of lines in input and output must be equal". Reproduced three times.

The cause is not subtle once seen. `kokoro_onnx.Tokenizer` holds one
process-wide `EspeakBackend` over a ctypes CDLL that releases the GIL, and
phonemizer says what that means in its own source -- backend/espeak/api.py: the
library "is not designed to be wrapped nor to be used in multithreaded or
multiprocess contexts (massive use of global variables)".

Both `/speak` and `/v1/audio/speech` are `def` rather than `async def`, on
purpose, so FastAPI runs them on AnyIO's 40-thread pool. Ordinary overlap -- the
page streaming while a shortcut or a script speaks -- was all it took for a
reader to be handed audio of a document they never sent. 1,307 tests passed
while that was true, because nothing here covered concurrency at all.

This test does not load the model: it replaces the tokenizer with one that
reports being entered twice. That is the property the lock exists for, and it
fails in milliseconds rather than minutes.
"""
import threading
import time

from app.synth import Synth


class ReentrancyDetector:
    """Records whether two threads were ever inside phonemize together."""

    def __init__(self) -> None:
        self.inside = 0
        self.overlapped = False
        self._count = threading.Lock()

    def phonemize(self, text: str, language: str) -> str:
        with self._count:
            self.inside += 1
            if self.inside > 1:
                self.overlapped = True
        # Long enough that unsynchronised callers are certain to collide, short
        # enough that 64 of them finish in well under a second.
        time.sleep(0.002)
        with self._count:
            self.inside -= 1
        return "p" * max(1, len(text))


def test_two_threads_are_never_inside_the_phonemiser_together():
    detector = ReentrancyDetector()
    synth = object.__new__(Synth)          # no model, no 310 MB, no espeak
    synth._k = type("K", (), {"tokenizer": detector})()

    threads = [threading.Thread(target=synth.plan,
                                args=(f"sentence number {n}", "en-us"),
                                kwargs={"target": 400})
               for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not detector.overlapped, (
        "two threads entered the phonemiser at once; espeak's global state means "
        "one of them gets the other's phonemes"
    )


def test_the_lock_is_shared_by_every_instance():
    """A per-instance lock would not help: the backend is process-wide, and
    nothing stops a second Synth being constructed -- a reload, a test, a future
    second voice pack."""
    assert Synth._espeak is not None
    first = object.__new__(Synth)
    second = object.__new__(Synth)
    assert first._espeak is second._espeak, \
        "each Synth has its own lock, so two of them would still collide in espeak"
