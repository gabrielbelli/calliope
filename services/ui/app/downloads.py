"""Link downloads: one job per person and link, one child per run, and a small cache.

    probe(sub, url)      app/fetcher.py probe, for the confirm card's facts
    replace(sub, url)    a pending job, the person's own
    start(job, kind)     a cache hit, or a download child in the background
    drop(sub, url)       the job goes, its child is killed, a big file deleted

EVERY JOB IS ONE PERSON'S. Jobs are keyed by (sub, url): two people with one
link get two jobs and two cache entries, and a stranger's token finds nothing,
which is the 404 of D36. There is no shared queue and no owner map, so there
is nothing one person can hold that locks another out. In memory only: a
restart empties it, and resolving the link again finds the cached file.

THE CHILD IS TOLD A URL, A KIND, A CAP AND A LANGUAGE, and nothing about the
person: no sub, no assertion, no delegation, no key path. It runs as
`python -I app/fetcher.py`, with the four variables in ENV and nothing this
service was started with, in a session of its own so a timeout kills its
whole process group. Its stdout is read one JSON line at a time, at most
LINE_LIMIT bytes each, into named fields; anything else is ignored.

THE CACHE ONLY AVOIDS DOWNLOADING THE SAME THING TWICE.

    /cache/jobs/<16 hex>/   a download in progress; emptied at start-up
    /cache/<sha256>.<ext>   a finished file; the directory listing is the index

No index file, no database, no timer. Last use is the file's atime, which
touch() moves and nothing else does on purpose; mtime never changes, so the
ETag /ui/media sends stays the same. A file of ITEM_BYTES or less is kept
SMALL_KEEP after its last use, and all of them together stay under
UI_CACHE_BYTES, least recently used out first. A bigger file is not cached:
it goes when its job goes, when the same person finishes another big file, or
BIG_KEEP after its last use. UI_CACHE_BYTES=0 makes every file big. sweep()
runs at start-up, after every download, on abandon and on every resolve, and
it deletes only names it wrote.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("voice-ui.downloads")

DOWNLOADS_AT_ONCE = 3        # whole server, above PER_PERSON so nobody holds every slot
PER_PERSON = 2               # queued or downloading, per person
PROBES_AT_ONCE = 2           # whole server, and one per person at a time
DOWNLOAD_SECONDS = 1800
JOBS_PER_PERSON = 64
JOB_LIMIT = 4096
JOB_IDLE = 24 * 3600
ITEM_BYTES = 128 * 2**20     # a bigger file is not cached
SMALL_KEEP = 24 * 3600       # a cached file, after last use
BIG_KEEP = 3600              # a file over ITEM_BYTES, or any file with the cache off
HEADROOM = 64 * 2**20
LINE_LIMIT = 64 * 1024
PROBE_LINES = 16
NAME = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{2,5}")

# What /ui/media sends each suffix as. Every one of ingest.MEDIA_SUFFIXES has
# a key here, which a test checks, so the route never guesses.
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
               ".mkv": "video/x-matroska", ".mov": "video/quicktime",
               ".m4a": "audio/mp4", ".weba": "audio/webm", ".mp3": "audio/mpeg",
               ".opus": "audio/ogg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
               ".wav": "audio/wav", ".flac": "audio/flac", ".aac": "audio/aac"}

# The child's whole environment. -I already ignores every PYTHON* variable and
# the user site, which uid 1000 can write.
ENV = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": "/nonexistent",
       "YTDLP_NO_PLUGINS": "1"}

EXPIRED = "That download is no longer kept here. Resolve the link again."
NO_SPACE = "There is not enough free disk space on the server for this download."
TOO_LONG = (f"The download took longer than {DOWNLOAD_SECONDS // 60} minutes "
            f"and was stopped.")
UNREADABLE = "The downloader sent something this server could not read."

_HAVE_YT_DLP = importlib.util.find_spec("yt_dlp") is not None
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


class Busy(Exception):
    """The person already has as much running as they may."""


class Refused(Exception):
    """The probe's own answer about the link: refused, live, playlist or failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class CollectError(Exception):
    """The child left something other than one acceptable file."""


@dataclass(slots=True)
class Job:
    sub: str                     # whose it is
    url: str                     # the token, as the page sends it
    facts: dict | None           # None if the probe timed out
    seen: float                  # time.monotonic() of the last touch
    kind: str | None = None      # audio | clip | video | captions, set by start
    clip: tuple[float | None, float | None] = (None, None)
    state: str = "pending"       # pending | queued | downloading | finished | error
    done: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    path: Path | None = None     # the cache file once finished
    error: str | None = None
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "downloading")


JOBS: OrderedDict[tuple[str, str], Job] = OrderedDict()   # least recently touched first
PROBING: set[str] = set()                                  # people with a probe running
_DOWNLOADS = asyncio.Semaphore(DOWNLOADS_AT_ONCE)
_PROBES = asyncio.Semaphore(PROBES_AT_ONCE)


def _fetcher() -> Path:
    return Path(config.FETCHER) if config.FETCHER else Path(__file__).with_name("fetcher.py")


def available() -> bool:
    """Links are on, the cache is a writable directory, and there is a downloader to run."""
    directory = config.CACHE_DIR
    return (config.LINKS and (bool(config.FETCHER) or _HAVE_YT_DLP)
            and directory.is_dir() and os.access(directory, os.W_OK | os.X_OK))


def startup() -> None:
    """Empty jobs/ of whatever a previous process left half-written, then sweep."""
    global _DOWNLOADS, _PROBES
    _DOWNLOADS = asyncio.Semaphore(DOWNLOADS_AT_ONCE)
    _PROBES = asyncio.Semaphore(PROBES_AT_ONCE)
    work = config.CACHE_DIR / "jobs"
    try:
        work.mkdir(parents=True, exist_ok=True)
        for entry in os.scandir(work):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(entry.path)
        sweep()
    except OSError as exc:
        log.warning("the link cache is not writable (%s): link ingestion is off",
                    type(exc).__name__)


async def shutdown() -> None:
    """Cancel every running download; each task kills its own child on the way out."""
    tasks = [job.task for job in JOBS.values() if job.task is not None and not job.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def get(sub: str, url: str) -> Job | None:
    job = JOBS.get((sub, url))
    if job is not None:
        job.seen = time.monotonic()
        JOBS.move_to_end((sub, url))
    return job


async def _spawn(args: list[str], cwd: Path | str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(_fetcher()), *args, cwd=cwd, env=ENV,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=LINE_LIMIT)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """The child's whole process group, then wait for it."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        # PermissionError is macOS's answer when the group's leader has
        # exited and not yet been reaped; there is nothing left to kill.
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _message(line: bytes) -> dict | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _number(value: object, kind: type) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return kind(value)


def _facts(raw: dict) -> dict:
    """The child's facts, field by field: anything else it sent is dropped."""
    def text(name: str, size: int) -> str | None:
        value = raw.get(name)
        return value[:size] if isinstance(value, str) and value else None
    duration = _number(raw.get("duration"), float)
    size = _number(raw.get("bytes"), int)
    lang = raw.get("subtitles_lang")
    return {"title": text("title", 300), "uploader": text("uploader", 200),
            "duration": duration if duration is not None and duration >= 0 else None,
            "bytes": size if size is not None and size >= 0 else None,
            "has_subtitles": raw.get("has_subtitles") is True,
            "subtitles_lang": lang if isinstance(lang, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", lang) else None,
            "video": raw.get("video") is True}


async def probe(sub: str, url: str) -> dict | None:
    """The facts about `url`, None if the probe could not say, or Refused.

    One probe per person at a time (Busy), PROBES_AT_ONCE for the server, and
    PROBE_TIMEOUT covers the wait for a slot as well as the run.
    """
    if sub in PROBING:
        raise Busy
    PROBING.add(sub)
    proc = None
    try:
        async with asyncio.timeout(config.PROBE_TIMEOUT):
            async with _PROBES:
                proc = await _spawn(["probe", "--", url], cwd="/")
                for _ in range(PROBE_LINES):
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    message = _message(line)
                    if message is None:
                        continue
                    if isinstance(message.get("facts"), dict):
                        return _facts(message["facts"])
                    if "error" in message:
                        raise Refused(str(message.get("code") or "failed"),
                                      str(message["error"]).strip()[:300])
        return None
    except (TimeoutError, ValueError, OSError) as exc:
        # A timeout, a line over LINE_LIMIT, or a child that would not start.
        log.info("probe gave no answer: %s", type(exc).__name__)
        return None
    finally:
        PROBING.discard(sub)
        if proc is not None:
            await _kill(proc)


def _discard(job: Job) -> None:
    """Forget a job: its task cancelled, and its file deleted if it is big."""
    JOBS.pop((job.sub, job.url), None)
    if job.task is not None and not job.task.done():
        job.task.cancel()
    if job.path is not None:
        with contextlib.suppress(OSError):
            if big(job.path.stat().st_size):
                job.path.unlink()


def replace(sub: str, url: str, facts: dict | None) -> Job:
    """A new pending job for `sub`, in place of any they had for `url`."""
    drop(sub, url)
    now = time.monotonic()
    for job in [j for j in JOBS.values() if not j.active and now - j.seen > JOB_IDLE]:
        _discard(job)
    mine = [j for j in JOBS.values() if j.sub == sub]
    idle = [j for j in mine if not j.active]
    while len(mine) >= JOBS_PER_PERSON and idle:
        oldest = idle.pop(0)
        mine.remove(oldest)
        _discard(oldest)
    idle = [j for j in JOBS.values() if not j.active]
    while len(JOBS) >= JOB_LIMIT and idle:
        _discard(idle.pop(0))
    job = Job(sub=sub, url=url, facts=facts, seen=now)
    JOBS[(sub, url)] = job
    return job


def _key(job: Job, kind: str) -> str:
    return hashlib.sha256(f"{job.sub}\0{job.url}\0{kind}".encode()).hexdigest()


def start(job: Job, kind: str) -> None:
    """A cache hit finishes now; anything else queues a download, or raises Busy."""
    key = _key(job, kind)
    hit = next((p for p in config.CACHE_DIR.glob(key + ".*") if NAME.fullmatch(p.name)), None)
    if hit is None and sum(1 for j in JOBS.values()
                           if j.sub == job.sub and j.active) >= PER_PERSON:
        raise Busy
    job.kind, job.error, job.path = kind, None, None
    job.done, job.total, job.speed, job.eta = 0, None, None, None
    if hit is not None:
        touch(hit)
        job.path, job.state = hit, "finished"
        return
    job.state = "queued"
    job.task = asyncio.create_task(_run(job, key))


def drop(sub: str, url: str) -> None:
    job = JOBS.get((sub, url))
    if job is not None:
        _discard(job)
    with contextlib.suppress(OSError):
        sweep()


def _cap(job: Job) -> int:
    return config.MAX_CAPTION_BYTES if job.kind == "captions" else config.MAX_DOWNLOAD_BYTES


def _fail(job: Job, message: str) -> None:
    job.state, job.error, job.path = "error", message, None


async def _run(job: Job, key: str) -> None:
    workdir: Path | None = None
    try:
        async with _DOWNLOADS:
            if JOBS.get((job.sub, job.url)) is not job:
                return                        # dropped while it waited
            cap = _cap(job)
            reserved = sum(_cap(j) for j in JOBS.values() if j.state == "downloading")
            sweep(need=cap + HEADROOM + reserved)
            if shutil.disk_usage(config.CACHE_DIR).free - reserved < cap + HEADROOM:
                _fail(job, NO_SPACE)
                return
            # Nothing awaits between the check above and this line, so two
            # starts cannot both count the same free space.
            workdir = config.CACHE_DIR / "jobs" / secrets.token_hex(8)
            workdir.mkdir()
            job.state = "downloading"
            lang = "-"
            if job.kind == "captions":
                lang = (job.facts or {}).get("subtitles_lang") or "en"
            job.proc = proc = await _spawn(
                ["fetch", str(job.kind), str(cap), lang, "--", job.url], cwd=workdir)
            final: dict = {}
            try:
                async with asyncio.timeout(DOWNLOAD_SECONDS):
                    while line := await proc.stdout.readline():
                        message = _message(line)
                        if message is None:
                            continue
                        if "ok" in message or "error" in message:
                            final = message
                            continue
                        job.done = _number(message.get("done"), int) or job.done
                        job.total = _number(message.get("total"), int)
                        job.speed = _number(message.get("speed"), float)
                        job.eta = _number(message.get("eta"), float)
                    await proc.wait()
            except TimeoutError:
                await _kill(proc)
                _fail(job, TOO_LONG)
                return
            except ValueError:
                await _kill(proc)
                _fail(job, UNREADABLE)
                return
            if proc.returncode != 0 or final.get("ok") is not True:
                said = final.get("error")
                log.info("a download failed: code=%s exit=%s", final.get("code"), proc.returncode)
                _fail(job, str(said).strip()[:300] if said else
                      f"The downloader stopped without saying why (exit {proc.returncode}).")
                return
            video = final.get("video") if isinstance(final.get("video"), bool) else None
            source, suffix = _collect(workdir, job.kind, cap, video)
            target = config.CACHE_DIR / f"{key}{suffix}"
            os.replace(source, target)
            touch(target)
            job.path, job.state = target, "finished"
            if big(target.stat().st_size):
                # Each person keeps at most one big file.
                for other in JOBS.values():
                    if (other is not job and other.sub == job.sub
                            and other.state == "finished" and other.path is not None):
                        with contextlib.suppress(OSError):
                            if big(other.path.stat().st_size):
                                other.path.unlink()
    except CollectError as exc:
        _fail(job, str(exc))
    except asyncio.CancelledError:
        if job.proc is not None:
            await asyncio.shield(_kill(job.proc))
        raise
    except Exception as exc:  # noqa: BLE001 - the job says so, the server goes on
        log.warning("a download could not be run: %s", type(exc).__name__)
        _fail(job, "The download could not be run on this server.")
    finally:
        job.proc = None
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        with contextlib.suppress(OSError):
            sweep()


def _collect(workdir: Path, kind: str | None, cap: int,
             video: bool | None) -> tuple[Path, str]:
    """THE ONE-FILE RULE: one regular file called media.<allowed suffix>, 0 < size <= cap."""
    # Here rather than at the top: ingest imports this module.
    from .ingest import CAPTION_SUFFIXES, MEDIA_SUFFIXES  # noqa: PLC0415

    entries = list(os.scandir(workdir))
    if len(entries) != 1 or not entries[0].is_file(follow_symlinks=False):
        raise CollectError("The download left something other than one file.")
    name = entries[0].name
    suffix = Path(name).suffix.lower()
    allowed = CAPTION_SUFFIXES if kind == "captions" else MEDIA_SUFFIXES
    if not name.startswith("media.") or suffix not in allowed:
        raise CollectError(
            f"The download is a {suffix or 'nameless'} file, which this page does not handle.")
    size = entries[0].stat(follow_symlinks=False).st_size
    if not 0 < size <= cap:
        raise CollectError("The download is empty or over the size limit.")
    if video is False:                         # audio-only, so <audio> and not <video>
        suffix = {".webm": ".weba", ".mp4": ".m4a"}.get(suffix, suffix)
    return Path(entries[0].path), suffix


def file_of(job: Job, *, use: bool = True) -> Path | None:
    """A finished job's file, touched as a use; None, and the job an error, if it has gone."""
    if job.state != "finished" or job.path is None:
        return None
    try:
        if use:
            touch(job.path)
        elif not job.path.is_file():
            raise FileNotFoundError(job.path)
    except FileNotFoundError:
        _fail(job, EXPIRED)
        return None
    return job.path


def filename(job: Job) -> str:
    """What the person would call the file: the title, else the host, plus the suffix."""
    title = ((job.facts or {}).get("title") or urlsplit(job.url).hostname or "link")
    name = _UNSAFE.sub("_", str(title)).strip(" ._")[:120] or "link"
    return name + (job.path.suffix if job.path is not None else "")


def touch(path: Path) -> None:
    """Move atime, the last use, and keep mtime, so the ETag never changes."""
    st = path.stat()
    os.utime(path, ns=(time.time_ns(), st.st_mtime_ns))


def big(size: int) -> bool:
    return config.CACHE_BYTES == 0 or size > ITEM_BYTES


def sweep(need: int = 0) -> None:
    """Expire, then keep the small files under UI_CACHE_BYTES and `need` bytes free.

    Least recently used first, and only names this module wrote. There is no
    grace period: the file used last goes last, and a reader that has a file
    open when it is unlinked reads it to the end.
    """
    now, on = time.time(), config.CACHE_BYTES > 0
    found = []
    for entry in os.scandir(config.CACHE_DIR):
        if NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
            with contextlib.suppress(FileNotFoundError):
                st = entry.stat(follow_symlinks=False)
                found.append((st.st_atime, st.st_size, entry.path))
    found.sort()                                            # least recently used first
    small = sum(size for _, size, _ in found if on and size <= ITEM_BYTES)
    free = shutil.disk_usage(config.CACHE_DIR).free
    for used, size, path in found:
        is_small = on and size <= ITEM_BYTES
        if now - used > (SMALL_KEEP if is_small else BIG_KEEP) or (
                is_small and (small > config.CACHE_BYTES or free < need)):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            free += size
            small -= size if is_small else 0


def stats() -> dict[str, int]:
    """How many finished files the cache holds, and their bytes."""
    items = total = 0
    with contextlib.suppress(OSError):
        for entry in os.scandir(config.CACHE_DIR):
            if NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                with contextlib.suppress(FileNotFoundError):
                    total += entry.stat(follow_symlinks=False).st_size
                    items += 1
    return {"items": items, "bytes": total}
