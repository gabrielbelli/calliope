"""The runner's jobs: the store that holds them and the thread that runs them.

THE STATES, and `running` never goes back to `queued`:

    queued --dispatch--> running --> done | failed
       |                    |
       +--DELETE--> cancelled +--DELETE--> cancelling --> cancelled

A job is `queued` only while it waits for the GPU -- behind another job, or
behind the free-memory gate -- and tts-long counts that time against its
TTS_RUNNER_MAX_WAIT. From the moment it is handed to the worker, model load
included, it is `running`. offpeak sends a running job back to `queued` when
its owner takes the card; this runner has no owner, so it never does.

AN ORPHANED JOB IS CANCELLED. tts-long withdraws a job only when a person
cancels it or one of its own bounds runs out. A poll timeout, a restart or a
drain on that side leaves the job here, and every later job would wait behind
it until tts-long gave up and spoke on its CPU. A job nobody has polled for
ORPHAN_SECONDS is cancelled exactly as a DELETE would cancel it.

ONE WORKER, AND THE DISPATCHER THREAD IS ITS ONLY OWNER. It starts the worker
on demand and is the only thing that stops it: after IDLE_SECONDS without a
job, when the other engine is needed, after a CUDA error, or at shutdown. One
owner for the stop is what guarantees no job is ever sent to a worker that is
already exiting.
"""

from __future__ import annotations

import hashlib
import logging
import math
import multiprocessing
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import gpu, worker

log = logging.getLogger("tts-runner.jobs")


def _setting(name: str, default: float) -> float:
    """An operator setting. "0" is a value here, not "unset"."""
    raw = (os.environ.get(name) or "").strip()
    return float(raw) if raw else default


# ---------------------------------------------------------------- settings --
# Read once, at import. Every constant below is a module attribute that the
# tests patch, and is read at the moment it is used.

# The worker, and with it every byte of VRAM, is let go after this long without
# a job. 600 s is tts-long's own TTS_IDLE_TIMEOUT.
IDLE_SECONDS = _setting("RUNNER_IDLE_SECONDS", 600.0)
# With no engine loaded, refuse to start one while the card has less than this
# free. A placeholder until the measured peak of the larger engine plus 256 MiB
# replaces it (RUNNER.md); 0 turns the gate off.
MIN_FREE_MIB = _setting("RUNNER_MIN_FREE_MIB", 4608.0)

QUEUE_CAP = 4
KEEP_SECONDS = 3600.0
KEEP_JOBS = 64
ORPHAN_SECONDS = 60.0
CANCEL_GRACE_S = 30.0
SHUTDOWN_GRACE_S = 20.0
# How long a stopping worker is given to leave on its own before it is killed.
STOP_GRACE_S = 30.0
# How often the dispatcher looks at the world when nothing wakes it.
TICK_S = 1.0

JOB_ID = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")


class QueueFull(Exception):
    """QUEUE_CAP jobs are already waiting."""


class KeyConflict(Exception):
    """This Idempotency-Key already names a job for another service."""


@dataclass
class Job:
    id: str
    service: str
    engine: str
    dir: Path
    # The segments, language, controls and clip path. Dropped the moment the
    # job is handed to the worker, after which only the worker's message holds
    # the text.
    request: dict | None
    key: str | None
    created: float
    last_polled: float
    status: str = "queued"
    record: dict | None = None
    finished_at: float | None = None
    cancelling_since: float | None = None
    orphaned: bool = False

    @property
    def finished(self) -> bool:
        return self.status in {"done", "failed", "cancelled"}

    @property
    def active(self) -> bool:
        return self.status in {"running", "cancelling"}


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path, 0o700)
    return path


class Store:
    """Every job in memory, their audio and the reference clips on disk, one lock.

    THE JOBS DIRECTORY IS WIPED AT START, AND THE CLIPS ARE KEPT. A restart
    forgets every job, which tts-long cannot tell from a runner that is down:
    its next poll gets 404 and the job goes back to its CPU. The clips persist
    because tts-long remembers per process which clips it has uploaded, and a
    runner that forgot them would answer 400 to a job naming one.
    """

    def __init__(self, root: Path) -> None:
        self.root = _private_dir(Path(root))
        self.assets = _private_dir(self.root / "assets")
        self.jobs_dir = self.root / "jobs"
        shutil.rmtree(self.jobs_dir, ignore_errors=True)
        _private_dir(self.jobs_dir)
        # A clip that was being written when the process died. Its digest
        # name was never taken, so deleting it loses nothing.
        for leftover in self.assets.glob("*.tmp"):
            with suppress(OSError):
                leftover.unlink()
        self.lock = threading.Lock()
        # What the dispatcher waits on: a submit wakes it at once rather than
        # at its next tick.
        self.changed = threading.Condition(self.lock)
        self._jobs: dict[str, Job] = {}
        self._keys: dict[str, str] = {}
        self._queue: deque[str] = deque()

    # -- clips ----------------------------------------------------------------

    def has_asset(self, sha256: str) -> bool:
        return HEX64.fullmatch(sha256) is not None and (self.assets / sha256).is_file()

    def asset_path(self, sha256: str) -> Path:
        return self.assets / sha256

    def put_asset(self, raw: bytes) -> tuple[str, bool]:
        """Store a clip under its own SHA-256. (digest, whether it was new).

        Written under a temporary name, synced, then LINKED into place, which
        fails if the digest exists: an existing clip is never rewritten, and a
        crash never leaves a short file under a digest.
        """
        digest = hashlib.sha256(raw).hexdigest()
        final = self.assets / digest
        if final.is_file():
            return digest, False
        temporary = self.assets / f"{digest}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, final)
                created = True
            except FileExistsError:
                created = False
        finally:
            with suppress(OSError):
                temporary.unlink()
        return digest, created

    # -- jobs -----------------------------------------------------------------

    def lookup(self, key: str) -> Job | None:
        with self.lock:
            found = self._keys.get(key)
            return self._jobs.get(found) if found else None

    def create(self, service: str, engine: str, request: dict,
               key: str | None) -> tuple[Job, bool]:
        """A new queued job, or the one this key already names. (job, reused)."""
        with self.lock:
            if key is not None and key in self._keys:
                existing = self._jobs.get(self._keys[key])
                if existing is not None:
                    if existing.service != service:
                        raise KeyConflict(key)
                    return existing, True
            if sum(1 for j in self._jobs.values() if j.status == "queued") >= QUEUE_CAP:
                raise QueueFull()
            job_id = secrets.token_hex(16)
            directory = _private_dir(self.jobs_dir / job_id)
            now = time.monotonic()
            job = Job(id=job_id, service=service, engine=engine, dir=directory,
                      request=request, key=key, created=now, last_polled=now)
            self._jobs[job_id] = job
            if key is not None:
                self._keys[key] = job_id
            self._queue.append(job_id)
            self.changed.notify_all()
            return job, False

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self._jobs.get(job_id)

    def polled(self, job_id: str) -> Job | None:
        """The job, with its orphan clock reset: somebody is still asking."""
        with self.lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.last_polled = time.monotonic()
            return job

    def artefacts(self, job: Job) -> list[str]:
        """The complete segment files, in segment order.

        By listing the directory: a file exists under its name only after it
        has been renamed from `.part`, so tts-long never fetches half a
        segment.
        """
        pattern = re.compile(re.escape(job.id) + r"\.(\d{1,5})\.f32")
        try:
            names = os.listdir(job.dir)
        except OSError:
            return []
        found = [(int(m.group(1)), n) for n in names if (m := pattern.fullmatch(n))]
        return [n for _, n in sorted(found)]

    def queued_count(self, service: str | None = None) -> int:
        with self.lock:
            return sum(1 for j in self._jobs.values() if j.status == "queued"
                       and (service is None or j.service == service))

    def active_job(self) -> Job | None:
        with self.lock:
            return next((j for j in self._jobs.values() if j.active), None)

    def next_queued(self) -> Job | None:
        with self.lock:
            while self._queue:
                job = self._jobs.get(self._queue[0])
                if job is not None and job.status == "queued":
                    return job
                self._queue.popleft()
            return None

    def dispatch(self, job_id: str) -> dict | None:
        """Mark the job running and hand back the worker's message, or None.

        None when it stopped being queued meanwhile: a DELETE can land while
        the worker is being started.
        """
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued" or job.request is None:
                return None
            with suppress(ValueError):
                self._queue.remove(job_id)
            request, job.request = job.request, None
            job.status = "running"
            return {"id": job.id, "dir": str(job.dir), **request}

    def cancel(self, job_id: str, *, orphaned: bool = False) -> dict | None:
        """Cancel as a DELETE would. None for an unknown job.

        A queued job is finished at once. A running one gets the flag file the
        worker checks between segments, and the audio already written is kept.
        A finished job is not changed.
        """
        with self.lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            was = job.status
            if orphaned:
                job.orphaned = True
            if was == "queued":
                record = {"cancelled_before_start": True}
                if job.orphaned:
                    record["orphaned"] = True
                self._finish(job, "cancelled", record)
                with suppress(ValueError):
                    self._queue.remove(job_id)
                self.changed.notify_all()
                return {"job_id": job_id, "was": was, "cancelled_now": True,
                        "requested": True}
            if was == "running":
                with suppress(OSError):
                    (job.dir / "cancel").touch()
                job.status = "cancelling"
                job.cancelling_since = time.monotonic()
                self.changed.notify_all()
            return {"job_id": job_id, "was": was, "cancelled_now": False,
                    "requested": was in {"running", "cancelling"}}

    def finish(self, job: Job, status: str, record: dict | None) -> None:
        with self.lock:
            self._finish(job, status, record)

    def _finish(self, job: Job, status: str, record: dict | None) -> None:
        job.status = status
        job.record = record
        job.request = None
        job.finished_at = time.monotonic()
        job.cancelling_since = None

    def orphans(self, now: float) -> list[str]:
        with self.lock:
            return [j.id for j in self._jobs.values()
                    if j.status in {"queued", "running"}
                    and now - j.last_polled > ORPHAN_SECONDS]

    def sweep(self, now: float) -> None:
        """Forget finished jobs older than KEEP_SECONDS, and all but KEEP_JOBS."""
        with self.lock:
            finished = sorted((j for j in self._jobs.values() if j.finished),
                              key=lambda j: j.finished_at or 0.0, reverse=True)
            gone = [j for i, j in enumerate(finished)
                    if i >= KEEP_JOBS or now - (j.finished_at or now) > KEEP_SECONDS]
            for job in gone:
                del self._jobs[job.id]
                if job.key is not None and self._keys.get(job.key) == job.id:
                    del self._keys[job.key]
        for job in gone:
            shutil.rmtree(job.dir, ignore_errors=True)

    def has_text(self) -> bool:
        """Does any dispatched job's text still sit here? For the tests."""
        with self.lock:
            return any(j.request is not None for j in self._jobs.values()
                       if j.status != "queued")


class Dispatcher:
    """One thread, one worker, one job at a time, first in first out."""

    def __init__(self, store: Store, *, services: dict[str, str], device: str,
                 factory: str = "app.synth:Synth", probe=None,  # noqa: ANN001
                 sampler=None) -> None:  # noqa: ANN001
        self.store = store
        # {runner service id: engine id}
        self.services = dict(services)
        self.device = device
        self.factory = factory
        self.probe = probe if probe is not None else gpu.Probe(device)
        self.sampler = sampler if sampler is not None else (
            gpu.Sampler() if device.startswith("cuda") else gpu.NoSampler())
        self._context = multiprocessing.get_context("spawn")
        self._proc = None
        self._conn = None
        self.engine: str | None = None
        # When the last worker was gone, its memory with it. -inf: none yet.
        self._released = -math.inf
        self._last_end = time.monotonic()
        self._stopping = threading.Event()
        self._stop_at = 0.0
        self._grace = SHUTDOWN_GRACE_S
        self._thread: threading.Thread | None = None

    # -- what the routes read --------------------------------------------------

    def worker_alive(self) -> bool:
        """A worker handle is held. Dropped within a tick of the process dying."""
        return self._proc is not None

    def gate(self) -> tuple[bool, int | None]:
        """(closed, free MiB). Closed only with no worker, a minimum and a reading."""
        if self.worker_alive() or MIN_FREE_MIB <= 0:
            return False, None
        # NOT A READING TAKEN WHILE OUR OWN WORKER HELD THE CARD. One taken up
        # to SAMPLE_EVERY_S before an idle stop counted the worker's memory as
        # somebody else's, the gate closed on it, and tts-long spoke the next
        # job on its CPU. Until a newer one arrives there is no reading, and
        # with no reading the gate is open, as it is without nvidia-smi.
        reading = self.sampler.latest(since=self._released) or {}
        free = reading.get("memory_free_mib")
        return (free is not None and free < MIN_FREE_MIB), free

    def available(self, service: str) -> tuple[bool, str | None]:
        """Will this runner take a job for `service` now, and if not why not."""
        if self.probe.state == gpu.CHECKING:
            return False, "checking the GPU"
        if self.probe.state != gpu.OK:
            return False, f"no usable GPU: {self.probe.reason}"
        closed, free = self.gate()
        if closed:
            return False, (f"the GPU has {free} MiB free and an engine needs "
                           f"about {int(MIN_FREE_MIB)}")
        return True, None

    def machine_state(self) -> str:
        if self.probe.state == gpu.FAILED:
            return "nogpu"
        if self.gate()[0]:
            return "busy"
        return "free"

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.probe.start()
        self.sampler.start()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="dispatcher")
        self._thread.start()

    def wake(self) -> None:
        with self.store.changed:
            self.store.changed.notify_all()

    def close(self, grace: float | None = None) -> None:
        """Stop at shutdown: cancel the running job, stop the worker, in `grace`.

        Called from the FastAPI lifespan when uvicorn receives SIGTERM. Compose
        gives the container 30 s, so this must be done well inside that.
        """
        self._grace = SHUTDOWN_GRACE_S if grace is None else grace
        self._stop_at = time.monotonic()
        self._stopping.set()
        running = self.store.active_job()
        if running is not None:
            self.store.cancel(running.id)
        self.wake()
        if self._thread is not None:
            self._thread.join(self._grace + 5.0)
        proc = self._proc
        if proc is not None and proc.is_alive():
            proc.kill()
        self.probe.stop()
        self.sampler.stop()

    # -- the thread ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                ran = self._tick()
            except Exception:  # noqa: BLE001 - the thread must outlive one bad tick
                log.exception("the dispatcher tick failed")
                ran = False
            if ran:
                continue
            with self.store.changed:
                if not self._stopping.is_set():
                    self.store.changed.wait(TICK_S)
        self._stop_worker("shutdown", grace=self._remaining())

    def _remaining(self) -> float:
        return max(0.0, self._grace - (time.monotonic() - self._stop_at))

    def _housekeep(self, now: float) -> None:
        self.store.sweep(now)
        for job_id in self.store.orphans(now):
            said = self.store.cancel(job_id, orphaned=True)
            if said is not None:
                log.info("job %s cancelled: not polled for %.0f s",
                         job_id[:8], ORPHAN_SECONDS)

    def _tick(self) -> bool:
        now = time.monotonic()
        self._housekeep(now)
        if self._proc is not None and not self._proc.is_alive():
            # DIED BETWEEN JOBS, which nothing was waiting on to notice: the
            # kernel's OOM killer, or a driver reset. Without this the next job
            # would be sent down a pipe nobody reads and fail for it.
            log.warning("the %s worker exited with code %s between jobs",
                        self.engine, self._proc.exitcode)
            self._drop()
            self.probe.again()
        if (self._proc is not None and now - self._last_end > IDLE_SECONDS):
            self._stop_worker(f"idle for {IDLE_SECONDS:.0f} s")
        job = self.store.next_queued()
        if job is None or self._stopping.is_set():
            return False
        if self.gate()[0]:
            # LEFT QUEUED, and tried again next tick. tts-long's own wait bound
            # decides how long it is worth waiting for the card.
            return False
        if self._proc is not None and self.engine != job.engine:
            self._stop_worker(f"switching to {job.engine}")
        if self._proc is None:
            self._start_worker(job.engine)
        message = self.store.dispatch(job.id)
        if message is None:
            return True
        try:
            self._conn.send(message)
        except (BrokenPipeError, OSError):
            self._died(job)
            return True
        self._wait(job)
        self._last_end = time.monotonic()
        return True

    def _wait(self, job: Job) -> None:
        """Until the job ends: the worker answers, dies, or overruns a cancel."""
        while True:
            try:
                ready = self._conn.poll(TICK_S)
            except (EOFError, OSError):
                ready = True
            if ready:
                try:
                    end = self._conn.recv()
                except (EOFError, OSError):
                    self._died(job)
                    return
                self._ended(job, end)
                return
            if not self._proc.is_alive():
                self._died(job)
                return
            now = time.monotonic()
            self._housekeep(now)
            if self._stopping.is_set() and job.status == "running":
                self.store.cancel(job.id)
            if job.status == "cancelling":
                limit = CANCEL_GRACE_S
                if self._stopping.is_set():
                    limit = min(limit, self._remaining())
                if now - (job.cancelling_since or now) > limit:
                    # generate() cannot be interrupted, and a segment can be
                    # forty seconds of speech. Past the grace, the process goes.
                    log.info("job %s did not stop within %.0f s of its cancel; "
                             "stopping the worker", job.id[:8], limit)
                    self._kill()
                    record = {"segments_done": len(self.store.artefacts(job))}
                    if job.orphaned:
                        record["orphaned"] = True
                    self.store.finish(job, "cancelled", record)
                    return

    def _ended(self, job: Job, end: dict) -> None:
        status = end.get("status") or "failed"
        record = dict(end.get("record") or {})
        if status == "cancelled" and job.orphaned:
            record["orphaned"] = True
        self.store.finish(job, status, record)
        if status == "done":
            audio = float(record.get("audio_seconds") or 0.0)
            compute = float(record.get("compute_seconds") or 0.0)
            peak = record.get("peak_vram_mib")
            log.info("job %s %s: %d segments, %.1f s of audio in %.1f s "
                     "(%.2fx), load %.1f s%s", job.id[:8], job.service,
                     record.get("segments") or 0, audio, compute,
                     audio / compute if compute else 0.0,
                     float(record.get("load_seconds") or 0.0),
                     f", peak {peak} MiB" if peak is not None else "")
        else:
            log.info("job %s %s: %s", job.id[:8], job.service, status)
        if end.get("fatal"):
            # The worker said its CUDA context is gone and is exiting. Wait for
            # it here, so the next job never meets a process on its way out.
            self._stop_worker("a CUDA error", send=False)
            self.probe.again()

    def _died(self, job: Job) -> None:
        proc = self._proc
        code = None
        if proc is not None:
            proc.join(5)
            if proc.is_alive():
                # The pipe closed and the process did not follow: nothing it
                # says can be trusted, and it may still hold the card.
                proc.kill()
                proc.join(5)
            code = proc.exitcode
        said = (f"the GPU worker was killed by signal {-code}"
                if isinstance(code, int) and code < 0
                else f"the GPU worker exited with code {code}")
        log.warning("job %s failed: %s", job.id[:8], said)
        self._drop()
        self.store.finish(job, "failed", {"error": f"WorkerExited: {said}"})
        self.probe.again()

    # -- the worker process ------------------------------------------------------

    def _start_worker(self, engine: str) -> None:
        parent, child = self._context.Pipe()
        proc = self._context.Process(
            target=worker.main, args=(child, engine, self.device, self.factory),
            daemon=False, name=f"worker-{engine}")
        proc.start()
        child.close()
        self._proc, self._conn, self.engine = proc, parent, engine
        # The idle clock starts now, so a worker started for a job that was
        # cancelled before it could be sent is not stopped the instant after.
        self._last_end = time.monotonic()
        log.info("started a %s worker on %s", engine, self.device)

    def _stop_worker(self, why: str, *, grace: float | None = None,
                     send: bool = True) -> None:
        proc = self._proc
        if proc is None:
            return
        if send:
            with suppress(BrokenPipeError, OSError):
                self._conn.send(None)
        proc.join(STOP_GRACE_S if grace is None else grace)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        log.info("stopped the %s worker: %s", self.engine, why)
        self._drop()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.is_alive():
            proc.kill()
            proc.join(5)
        self._drop()

    def _drop(self) -> None:
        """Forget a worker that has exited, and have the card read again."""
        if self._conn is not None:
            with suppress(OSError):
                self._conn.close()
        # BEFORE THE HANDLE GOES: a handler that sees no worker must also see
        # the time after which a reading counts.
        self._released = time.monotonic()
        self._proc, self._conn, self.engine = None, None, None
        self.sampler.again()
