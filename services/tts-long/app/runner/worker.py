"""The child process that holds one engine and speaks one job at a time.

ITS EXIT IS THE UNLOAD. Dropping a model inside a process does not give back
the CUDA context or the allocator's reserve; ending the process does. So the
worker never decides to stop: it blocks on `recv()` until the dispatcher sends
None (idle, an engine switch, shutdown), the pipe closes (the server has gone),
or a CUDA-class error poisons it. The dispatcher in jobs.py is the one owner of
every stop, which is what keeps a job from being sent to a worker that is
already on its way out.

ONE SEGMENT PER CALL, through `Synth.speak_segments`, so the empty-text, token
count and watermark behaviour are the CPU lane's exactly, and the cancel flag
is checked between segments -- as fine as cancellation gets, because
generate() has no interruption point.
"""

from __future__ import annotations

import importlib
import os
import re
import signal
import time
from pathlib import Path

import numpy as np

# Threads for the CPU-side work around the card (tokenising, the vocoder's
# host half). Not a knob: the card does the work that matters.
THREADS = 4
# A failure message is cut here, as offpeak cuts its own.
MAX_ERROR = 300
# A CUDA error leaves the context in a state nothing in this process can trust;
# anything else (a clip that will not decode, say) keeps the loaded model.
_CUDA_CLASS = re.compile(r"CUDA|cuDNN|CUDNN|cuBLAS|CUBLAS")
# An absolute path or a URL inside a message, so the record never names one.
_PATH = re.compile(r"(?:/[\w.@+-]+){2,}")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)


def _load(factory: str):
    module_name, _, name = factory.partition(":")
    return getattr(importlib.import_module(module_name), name)


def main(conn, engine: str, device: str,  # noqa: ANN001 - a multiprocessing Connection
         factory: str = "app.synth:Synth") -> None:
    """Load `engine` lazily on `device`, then run whatever the dispatcher sends."""
    # The parent decides when this process stops. A Ctrl-C in a terminal
    # reaches the whole process group, and this must not die under a job the
    # parent is about to cancel cleanly.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from app.engines import ENGINES

    synth = _load(factory)(idle_timeout=float("inf"), threads=THREADS,
                           spec=ENGINES[engine], device=device)
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        end = run(synth, message, device)
        try:
            conn.send(end)
        except (BrokenPipeError, OSError):
            return
        if end.get("fatal"):
            return


def run(synth, job: dict, device: str) -> dict:  # noqa: ANN001 - Synth or a fake
    """Speak every segment of one job into its directory, and say how it ended."""
    started = time.monotonic()
    cuda = device.startswith("cuda")
    if cuda:
        import torch

        torch.cuda.reset_peak_memory_stats()
    loads = getattr(synth, "loads", 0)
    directory = Path(job["dir"])
    flag = directory / "cancel"
    segments = job["segments"]
    tokens = frames = 0
    try:
        for n, text in enumerate(segments):
            if flag.exists():
                return {"id": job["id"], "status": "cancelled",
                        "record": {"segments_done": n}}
            spoken = synth.speak_segments([(text, 0.0)], job.get("language"),
                                          job.get("controls") or {},
                                          job.get("reference"))
            audio = np.asarray(spoken.audio, dtype="<f4")
            # WRITTEN EVEN WHEN EMPTY. An empty segment is still a segment:
            # tts-long splices that segment's pause from its index, and a
            # missing file would shift every pause after it.
            name = f"{job['id']}.{n}.f32"
            partial = directory / f"{name}.part"
            partial.write_bytes(audio.tobytes())
            os.replace(partial, directory / name)
            tokens += int(spoken.input_tokens)
            frames += int(audio.size)
    except Exception as exc:  # noqa: BLE001 - every failure is the job's, named
        return {"id": job["id"], "status": "failed",
                "record": {"error": _said(exc, job)}, "fatal": _fatal(exc)}
    rate = int(synth.spec.facts.native_sample_rate)
    compute = time.monotonic() - started
    return {"id": job["id"], "status": "done", "record": {
        "input_tokens": tokens,
        # THE TOTAL SAMPLES WRITTEN AT THE ENGINE'S OWN RATE, so tts-long's
        # frames / frame_rate check against the audio it collected holds
        # exactly rather than within a tolerance.
        "frames": frames,
        "frame_rate": rate,
        "segments": len(segments),
        "audio_seconds": round(frames / rate, 3),
        "compute_seconds": round(compute, 3),
        "load_seconds": (round(float(synth.load_seconds), 1)
                         if getattr(synth, "loads", 0) > loads else 0.0),
        "device": device,
        "peak_vram_mib": _peak_mib() if cuda else None}}


def _peak_mib() -> int:
    import torch

    return int(torch.cuda.max_memory_reserved()) >> 20


def _fatal(exc: BaseException) -> bool:
    """Does this error poison the process, so the next job needs a fresh one."""
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    return isinstance(exc, RuntimeError) and bool(_CUDA_CLASS.search(str(exc)))


def _said(exc: BaseException, job: dict) -> str:
    """The failure as "<Class>: <message>", with no path, host or segment text.

    The record is read by tts-long and written onto a job row a person sees,
    and it crosses a network. librosa names the clip's path; a tokeniser error
    can quote the text it choked on.
    """
    message = str(exc)
    for text in sorted((t for t in job.get("segments") or () if len(t) >= 4),
                       key=len, reverse=True):
        message = message.replace(text, "<text>")
    message = _PATH.sub("<path>", _URL.sub("<address>", message))
    message = " ".join(message.split())[:MAX_ERROR]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
