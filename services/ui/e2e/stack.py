"""The local stack the browser tests run against, and the process hygiene that
keeps it from outliving them.

    browser -> gateway (real) -> page server (real) -> gateway -> backends (fakes.py)
                                                              -> hub (real) <- scripted satellites

Everything listens on 127.0.0.1, on ports the kernel picked, and nothing in it
can reach anything else (launch.py). The browser is pointed at the GATEWAY,
not at the page server, because that is how the page is deployed: it is served
from the gateway's origin and every call it makes crosses the gateway's /ui
allowlist twice. A route the page needs and the gateway does not carry is a
404 here, as it would be at home -- which is the class of bug a deep link to a
tab or a satellite is most likely to introduce.

MANUAL USE, for poking at the stack with curl while writing a test:

    <venv>/bin/python stack.py

takes the same machine-wide lock the tests take, prints the URLs, and stops
everything on Ctrl-C or after 20 minutes. It starts no browser.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SERVICES = REPO / "services"

# THE HARNESS'S OWN DIRECTORY: the lock, the per-run logs and data, the browser
# profiles and the screenshots. Outside the repository, so nothing a run writes
# can be committed by accident. One directory per user, in the home directory
# and not under TMPDIR, because the lock in it is what makes "one browser at a
# time" true across every shell and agent of that user, and TMPDIR can differ
# between them; CALLIOPE_E2E_DIR moves all of it at once.
WEBUI = Path(os.environ.get("CALLIOPE_E2E_DIR", str(Path.home() / ".cache" / "calliope-e2e")))
LOCK = WEBUI / "browser.lock"
RUNS = WEBUI / "runs"
PROFILES = WEBUI / "profiles"
SHOTS = WEBUI / "shots"

# What `ps` is searched for. Every stack process carries MARKER_PREFIX<pid>-...
# on its command line (launch.py --marker); the browser carries its profile
# directory, PROFILES/<pid>/...
MARKER_PREFIX = "calliope-e2e-"
SESSION_SECONDS = 20 * 60
KEEP_RUNS = 5

# The wake word models, pinned by hash in services/satellites/app/wakeword.py.
# The hub fetches a missing one from GitHub at start-up, which this stack
# cannot do (launch.py refuses the connection), so they are copied into the
# hub's model directory first. Kept beside the venv rather than in the repo:
# 5 MB of binaries, and the same SATELLITES_TEST_WAKEWORD_DIR the satellites'
# own tests read wins when it is set.
WAKEWORD_CACHE = Path(os.environ.get("SATELLITES_TEST_WAKEWORD_DIR")
                      or Path(sys.prefix) / "share" / "calliope-e2e" / "wakewords")


# A well-formed DER ECDSA signature that proves nothing. The hub runs without
# SATELLITES_FIRMWARE_PUBKEY, so it checks a signature's shape and not its
# maths, and the scripted satellites report caps.ota_key as the real firmware
# does, so the hub skips an UNSIGNED image for them with a reason (which is a
# flow worth testing too). An upload carrying this one goes all the way:
# POST /satellites/firmware?model=...&version=...&signature=UNCHECKED_SIGNATURE,
# with a body whose first byte is 0xE9 (an ESP32 application image).
UNCHECKED_SIGNATURE = "MAYCAQECAQE"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- processes ---------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    rss_kb: int
    command: str


def processes() -> list[Proc]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,rss=,command="], capture_output=True,
                         text=True, timeout=15, check=False).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit():
            found.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4]))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# MATCHED ON THE SHAPE OF THE COMMAND, NEVER ON A SUBSTRING OF IT. A substring
# test once matched the shell that had run `ps | grep calliope-e2e-` and
# killed it: any process whose command line merely MENTIONS the marker -- a
# grep, an editor, an agent's shell -- would be swept. A stack process is
# python running launch.py with --marker as its first option; a browser is
# the headless shell binary with its profile under PROFILES.


def is_stack(proc: Proc, owner: int | None = None) -> bool:
    argv = proc.command.split()
    prefix = MARKER_PREFIX + (f"{owner}-" if owner is not None else "")
    return (len(argv) >= 4 and argv[1] == str(HERE / "launch.py") and argv[2] == "--marker"
            and argv[3].startswith(prefix))


def is_browser(proc: Proc, owner: int | None = None) -> bool:
    argv = proc.command.split()
    prefix = f"--user-data-dir={PROFILES}/" + (f"{owner}/" if owner is not None else "")
    return bool(argv) and argv[0].endswith("/chrome-headless-shell") and any(a.startswith(prefix) for a in argv)


def untouchable() -> tuple[set[int], set[int]]:
    """This process, its ancestors (the shell, the terminal, the agent) and
    its group: nothing here may ever be killed, whatever it matched."""
    by_pid = {p.pid: p for p in processes()}
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        pid = by_pid[pid].ppid if pid in by_pid else 1
    return pids, {os.getpgid(0), 0, 1}


def harness_processes(owner: int | None = None) -> list[Proc]:
    """Every process this harness started (all runs, or the one whose pytest
    is `owner`), and every process in the group of one: Chrome's helpers carry
    no profile path, but they share its group."""
    everything = processes()
    pids, pgids = untouchable()
    marked = [p for p in everything if (is_stack(p, owner) or is_browser(p, owner)) and p.pid not in pids]
    groups = {p.pgid for p in marked} - pgids
    return sorted({p for p in everything if p.pid not in pids and (p in marked or p.pgid in groups)},
                  key=lambda p: p.pid)


def kill_group(pgid: int, grace: float = 5.0) -> None:
    """SIGTERM a process group, then SIGKILL whatever is left of it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    ends = time.monotonic() + grace
    while time.monotonic() < ends:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def kill_all(procs: list[Proc]) -> None:
    """Kill these processes and their groups, except anything untouchable()."""
    pids, pgids = untouchable()
    for pgid in {p.pgid for p in procs} - pgids:
        kill_group(pgid, grace=2.0)
    for p in procs:
        if p.pid in pids:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(p.pid, signal.SIGKILL)


def sweep_orphans() -> list[Proc]:
    """Kill what an earlier run left behind. Called with the lock held, so
    nothing marked as ours can belong to a session that is still running."""
    left = harness_processes()
    if left:
        kill_all(left)
    for profile in PROFILES.glob("*") if PROFILES.is_dir() else ():
        if profile.name.isdigit() and not alive(int(profile.name)):
            shutil.rmtree(profile, ignore_errors=True)
    return left


# ---- the lock ----------------------------------------------------------------------


class MachineLock:
    """At most one stack and one browser on this machine at any moment, across
    every agent and every checkout. fcntl.flock, so a holder that dies -- even
    SIGKILLed -- releases it with its file descriptor."""

    def __init__(self, wait: float = 25 * 60) -> None:
        self.wait = wait
        self.fd: int | None = None

    def acquire(self) -> None:
        WEBUI.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        ends = time.monotonic() + self.wait
        said = False
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
            if time.monotonic() > ends:
                os.close(self.fd)
                self.fd = None
                raise TimeoutError(f"another browser session has held {LOCK} for {self.wait:.0f} s")
            if not said:
                holder = LOCK.read_text(errors="replace").strip() if LOCK.exists() else "?"
                sys.stderr.write(f"e2e: waiting for {LOCK} (held by {holder or 'an unknown process'})\n")
                said = True
            time.sleep(2.0)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"pid {os.getpid()} since {time.strftime('%H:%M:%S')}\n".encode())

    def release(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self.fd, 0)
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


# ---- the fakes' control API --------------------------------------------------------


class FakeControl:
    """The test's handle on fakes.py: what arrived, what to break, and the
    scripted satellites. Every call is to 127.0.0.1."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.http = httpx.Client(base_url=base, timeout=30)

    def _ok(self, response: httpx.Response) -> Any:
        response.raise_for_status()
        return response.json()

    def requests(self, *, backend: str | None = None, method: str | None = None,
                 path: str | None = None, since: int = 0) -> list[dict[str, Any]]:
        """What reached the fake backends, oldest first. `path` is a regular
        expression searched in the path; `since` a `seq` from an earlier entry."""
        params = {k: v for k, v in (("backend", backend), ("method", method), ("path", path),
                                    ("since", since)) if v}
        return self._ok(self.http.get("/__fake/requests", params=params))

    def last_seq(self) -> int:
        entries = self.requests()
        return entries[-1]["seq"] if entries else 0

    def clear_requests(self) -> None:
        self._ok(self.http.delete("/__fake/requests"))

    def reset(self) -> None:
        """Requests, failures, health overrides, transcript, jobs, glossaries and
        MeTube back to how the session started. The hub is real and is not
        reset here; see Stack.restart_hub."""
        self._ok(self.http.post("/__fake/reset"))

    def fail(self, path: str, *, status: int | None = 500, method: str | None = None,
             backend: str | None = None, json_body: Any = None, body: str | None = None,
             times: int | None = None, delay: float = 0.0, headers: dict[str, str] | None = None) -> None:
        """Answer requests matching `path` (a regular expression) with `status`
        instead of the fake's answer, `times` times or for ever; or, with
        status=None, only hold them for `delay` seconds first. backend is one
        of stt, tts, tts_long, metube."""
        if json_body is None and body is None and status and status >= 400:
            json_body = {"error": {"message": f"injected {status}", "type": "server_error",
                                   "param": None, "code": "injected"}}
        self._ok(self.http.post("/__fake/fail", json={
            "path": path, "status": status, "method": method, "backend": backend, "json": json_body,
            "body": body, "times": times, "delay": delay, "headers": headers}))

    def clear_failures(self) -> None:
        self._ok(self.http.delete("/__fake/fail"))

    def health(self, backend: str, **fields: Any) -> None:
        """Merge fields over a backend's /health (stt, tts, tts_long); None removes one."""
        self._ok(self.http.post(f"/__fake/health/{backend}", json=fields))

    def transcript(self, text: str) -> None:
        self._ok(self.http.put("/__fake/transcript", json={"text": text}))

    def add_job(self, **fields: Any) -> dict[str, Any]:
        """A tts-long job. Scripted by default: queued for 1 s, then one segment
        every 1.2 s. scripted=False keeps whatever status is given."""
        return self._ok(self.http.post("/__fake/jobs", json=fields))

    def metube(self) -> dict[str, Any]:
        return self._ok(self.http.get("/__fake/metube"))

    # -- satellites: kitchen (Korvo), lounge (Pi with AirPlay), hallway (Korvo, not adopted)

    def satellites(self) -> list[dict[str, Any]]:
        return self._ok(self.http.get("/__fake/satellites"))

    def satellite_received(self, key: str, type: str | None = None, since: float = 0) -> list[dict]:
        """The JSON messages the hub sent a satellite, oldest first."""
        params = {k: v for k, v in (("type", type), ("since", since)) if v}
        return self._ok(self.http.get(f"/__fake/satellites/{key}/received", params=params))

    def satellite_status(self, key: str, cause: str | None = None, **fields: Any) -> dict[str, Any]:
        """Change what a satellite reports and send a status now. cause="local"
        is a change made on the device (a phone's AirPlay slider)."""
        return self._ok(self.http.post(f"/__fake/satellites/{key}/status",
                                       json=fields | ({"cause": cause} if cause else {})))

    def satellite_send(self, key: str, message: dict[str, Any]) -> None:
        self._ok(self.http.post(f"/__fake/satellites/{key}/send", json=message))

    def satellite_airplay(self, key: str, state: str | None = None, on_command: str | None = None) -> None:
        """state: playing, paused or idle. on_command: answer, refuse or ignore."""
        self._ok(self.http.post(f"/__fake/satellites/{key}/airplay",
                                json={k: v for k, v in (("state", state), ("on_command", on_command)) if v}))

    def satellite_mic(self, key: str, seconds: float = 6.0) -> None:
        """Microphone frames at real time, for the Listen button."""
        self._ok(self.http.post(f"/__fake/satellites/{key}/mic", json={"seconds": seconds}))

    def satellite_button(self, key: str, button: str, action: str = "press") -> None:
        self._ok(self.http.post(f"/__fake/satellites/{key}/button", json={"button": button, "action": action}))

    def satellite_drop(self, key: str) -> None:
        """Take a satellite offline until satellite_start."""
        self._ok(self.http.post(f"/__fake/satellites/{key}/drop"))

    def satellite_start(self, key: str) -> None:
        self._ok(self.http.post(f"/__fake/satellites/{key}/start"))

    def close(self) -> None:
        self.http.close()


# ---- the stack ---------------------------------------------------------------------


class Stack:
    """Start and stop the fakes, the hub, the gateway and the page server.

    `stop()` is idempotent and safe from a signal handler or atexit, which is
    how it is also installed: every child is in a process group of its own, and
    the group is what is killed."""

    def __init__(self, *, deadline: float = SESSION_SECONDS) -> None:
        self.owner = os.getpid()
        self.marker = f"{MARKER_PREFIX}{self.owner}-{int(time.time())}"
        self.deadline = deadline
        self.run = RUNS / str(self.owner)
        self.children: dict[str, subprocess.Popen] = {}
        self.ports = {name: free_port() for name in
                      ("stt", "tts", "long", "metube", "control", "hub", "gateway", "ui")}
        self.url = f"http://127.0.0.1:{self.ports['gateway']}"
        self.ui_direct = f"http://127.0.0.1:{self.ports['ui']}"
        self.hub = f"http://127.0.0.1:{self.ports['hub']}"
        self.fake = FakeControl(f"http://127.0.0.1:{self.ports['control']}")
        self.wake_words: str = ""
        self._stopped = False

    # -- lifecycle --

    def start(self) -> Stack:
        self._prune_runs()
        if self.run.exists():
            shutil.rmtree(self.run)
        (self.run / "tmp").mkdir(parents=True)
        self._seed_voices()
        atexit.register(self.stop)
        try:
            self._spawn("fakes", ["--fakes", "--repo", str(REPO),
                                  *(f"--{n}-port={self.ports[n]}" for n in ("stt", "tts", "long",
                                                                          "metube", "control"))],
                        cwd=HERE, env={})
            self._wait("fakes", f"http://127.0.0.1:{self.ports['control']}/__fake/health")
            self._start_hub()
            self._spawn("gateway", ["--app", "app.main:app", "--app-dir", str(SERVICES / "gateway"),
                                    "--port", str(self.ports["gateway"])],
                        cwd=SERVICES / "gateway", env={
                            "GATEWAY_STT_URL": f"http://127.0.0.1:{self.ports['stt']}",
                            "GATEWAY_TTS_URL": f"http://127.0.0.1:{self.ports['tts']}",
                            "GATEWAY_TTS_LONG_URL": f"http://127.0.0.1:{self.ports['long']}",
                            "GATEWAY_UI_URL": self.ui_direct,
                            "GATEWAY_SATELLITES_URL": self.hub,
                            "GATEWAY_LOG_LEVEL": "WARNING"})
            self._spawn("ui", ["--app", "app.main:app", "--app-dir", str(SERVICES / "ui"),
                               "--port", str(self.ports["ui"])],
                        cwd=SERVICES / "ui", env={
                            "UI_GATEWAY_URL": self.url,
                            "UI_METUBE_URL": f"http://127.0.0.1:{self.ports['metube']}",
                            # WAV, not the production opus: the fake MeTube
                            # serves a WAV, and a name that says so keeps the
                            # content type honest all the way to <audio>.
                            "UI_METUBE_FORMAT": "wav",
                            # No yt-dlp: it would reach for the network, and the
                            # confirm card is drawn from MeTube's title without it.
                            "UI_PROBE": "0",
                            "UI_VOICE_DIR": str(self.run / "voices"),
                            "UI_LOG_LEVEL": "WARNING"})
            self._wait("gateway", f"{self.url}/health")
            self._wait("ui", f"{self.ui_direct}/health")
            self._wait("page", f"{self.url}/ui")
        except BaseException:
            self.stop()
            raise
        return self

    def _start_hub(self) -> None:
        data = self.run / "hub-data"
        models = data / "models"
        models.mkdir(parents=True, exist_ok=True)
        present = sorted(p.name for p in WAKEWORD_CACHE.glob("*.onnx")) if WAKEWORD_CACHE.is_dir() else []
        for name in present:
            shutil.copy2(WAKEWORD_CACHE / name, models / name)
        # Only the words whose model is here: anything else would be fetched.
        words = [w for w, f in (("hey_jarvis:0.5", "hey_jarvis_v0.1.onnx"), ("alexa:0.6", "alexa_v0.1.onnx"))
                 if f in present and {"melspectrogram.onnx", "embedding_model.onnx"} <= set(present)]
        self.wake_words = ",".join(words)
        self._spawn("hub", ["--app", "app.main:app", "--app-dir", str(SERVICES / "satellites"),
                            "--port", str(self.ports["hub"])],
                    cwd=SERVICES / "satellites", env={
                        "SATELLITES_DATA_DIR": str(data),
                        "SATELLITES_MODEL_DIR": str(models),
                        "SATELLITES_WAKE_WORDS": self.wake_words,
                        "SATELLITES_STT_URL": f"http://127.0.0.1:{self.ports['stt']}",
                        "SATELLITES_TTS_URL": f"http://127.0.0.1:{self.ports['tts']}",
                        "SATELLITES_TIMEZONE": "Europe/London",
                        "SATELLITES_LOG_LEVEL": "WARNING",
                        "ORT_DISABLE_TELEMETRY": "1"})
        self._wait("hub", f"{self.hub}/health")
        self.fake._ok(self.fake.http.post("/__fake/satellites/connect", json={"hub": self.hub}))

    def restart_hub(self, wipe: bool = True) -> None:
        """A fresh hub (and a fresh adoption of the satellites) for a test that
        needs one. About a second, and every open page loses its event stream
        for that second, so only where the hub's state matters."""
        self.stop_hub()
        if wipe:
            shutil.rmtree(self.run / "hub-data", ignore_errors=True)
        self.start_hub()

    def stop_hub(self) -> None:
        """The hub gone, as a restart or a crash looks from the page: the
        satellites are disconnected first, so none of them is left dialling a
        port nothing listens on, then the hub's process group is killed."""
        self.fake._ok(self.fake.http.post("/__fake/satellites/disconnect"))
        self._kill("hub")

    def start_hub(self) -> None:
        """The hub back, on the same port and with whatever data it had, and
        the satellites connected to it again."""
        self._start_hub()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        with contextlib.suppress(Exception):
            self.fake.close()
        for name in reversed(list(self.children)):
            self._kill(name)
        # Anything that escaped its group -- it should be nothing -- by marker.
        stray = self.survivors()
        if stray:
            kill_all(stray)
        for sub in ("tmp", "hub-data", "voices"):
            shutil.rmtree(self.run / sub, ignore_errors=True)

    def survivors(self) -> list[Proc]:
        return [p for p in processes() if is_stack(p, self.owner)]

    # -- helpers --

    def _env(self, extra: dict[str, str]) -> dict[str, str]:
        # BUILT FROM NOTHING, not copied from this shell: a proxy variable, an
        # API key or a SATELLITES_MQTT_URL in the caller's environment must not
        # reach a service under test.
        return {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                "HOME": os.environ.get("HOME", "/tmp"), "LANG": "en_GB.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                "TMPDIR": str(self.run / "tmp")} | extra

    def _spawn(self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        # The child has its own copy of the descriptor, so ours closes at once.
        with open(self.run / f"{name}.log", "ab") as log:
            self.children[name] = subprocess.Popen(
                [sys.executable, str(HERE / "launch.py"), "--marker", self.marker,
                 "--parent", str(self.owner), "--deadline", str(self.deadline + 60),
                 "--violations", str(self.run / "network-violations.log"), *argv],
                cwd=cwd, env=self._env(env), stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)

    def _kill(self, name: str) -> None:
        child = self.children.pop(name, None)
        if child is None:
            return
        kill_group(child.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(5)

    def _wait(self, name: str, url: str, timeout: float = 90.0) -> None:
        child = self.children.get(name if name != "page" else "ui")
        ends = time.monotonic() + timeout
        while time.monotonic() < ends:
            if child is not None and child.poll() is not None:
                break
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(url, timeout=2).status_code == 200:
                    return
            time.sleep(0.2)
        log = self.run / f"{name if name != 'page' else 'ui'}.log"
        tail = log.read_text(errors="replace")[-3000:] if log.exists() else ""
        raise RuntimeError(f"{name} did not come up at {url}:\n{tail}")

    def _seed_voices(self) -> None:
        """One cloned voice, so the Speak tab's clone group is not empty."""
        import wave

        voices = self.run / "voices"
        voices.mkdir()
        with wave.open(str(voices / "narrator.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000 * 12)

    def _prune_runs(self) -> None:
        if not RUNS.is_dir():
            return
        runs = sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in runs[:-KEEP_RUNS]:
            shutil.rmtree(old, ignore_errors=True)

    def violations(self) -> list[str]:
        path = self.run / "network-violations.log"
        return path.read_text().splitlines() if path.exists() else []

    def describe(self) -> dict[str, Any]:
        return {"page": f"{self.url}/ui", "gateway": self.url, "ui_direct": self.ui_direct,
                "hub": self.hub, "fakes": self.fake.base, "run": str(self.run),
                "wake_words": self.wake_words, "ports": self.ports}


# ---- emergency exits ---------------------------------------------------------------

_CLEANUPS: list[Callable[[], None]] = []
_INSTALLED = False


def on_emergency(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` on SIGTERM, SIGHUP, at exit, and at the session deadline.

    SIGINT is left to pytest, which turns it into KeyboardInterrupt and runs
    the fixtures' finalisers; atexit catches whatever that misses."""
    global _INSTALLED
    _CLEANUPS.append(cleanup)
    if _INSTALLED:
        return
    _INSTALLED = True
    atexit.register(run_cleanups)

    def handler(signum, frame) -> None:
        run_cleanups()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def run_cleanups() -> None:
    while _CLEANUPS:
        with contextlib.suppress(Exception):
            _CLEANUPS.pop()()


def watchdog(seconds: float, what: str) -> threading.Timer:
    """The session's hard limit: clean up, interrupt the main thread, and if
    that does not end the process within 15 s, end it."""
    def fire() -> None:
        sys.stderr.write(f"\ne2e: {what} passed its {seconds:.0f} s limit; stopping everything\n")
        run_cleanups()
        import _thread
        _thread.interrupt_main()
        time.sleep(15)
        os._exit(124)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


def main() -> None:
    lock = MachineLock()
    lock.acquire()
    stack = Stack()
    on_emergency(stack.stop)
    on_emergency(lock.release)
    try:
        swept = sweep_orphans()
        if swept:
            print(f"swept {len(swept)} orphaned processes from an earlier run")
        stack.start()
        print(json.dumps(stack.describe(), indent=2))
        print("Ctrl-C stops it; it stops by itself after 20 minutes.")
        ends = time.monotonic() + SESSION_SECONDS
        while time.monotonic() < ends:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()
        left = stack.survivors()
        lock.release()
        print("leaked:", [p.command[:120] for p in left] if left else "nothing")


if __name__ == "__main__":
    main()
