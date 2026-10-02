"""The local stack the browser tests run against, and the process hygiene that
keeps it from outliving them.

    browser -> gateway :8080 (real) -> page server, hub (real), stt, tts, tts-long (fakes.py)
    hub, page server -> gateway :8081 (internal) -> stt, tts (fakes.py)
    scripted satellites -> gateway device socket -> hub

Everything listens on 127.0.0.1, on ports the kernel picked, and nothing in it
can reach anything else (launch.py). The browser is pointed at the GATEWAY,
not at the page server, because that is how the page is deployed: it is served
from the gateway's origin, signed in there, and every call it makes crosses
the gateway's route table. A route the page needs and the gateway does not
carry is a 404 here, as it would be at home -- which is the class of bug a deep
link to a tab or a satellite is most likely to introduce.

THE GATEWAY IS THE DEPLOYMENT'S, CONFIGURED AS A DEVELOPER'S MACHINE MAY BE
(D16): bound to loopback, so CALLIOPE_DEV_INSECURE_COOKIE drops `__Host-` and
`Secure` and the session cookie works over plain http; its public origin this
session's own address; its database, keys and service volumes in this run's
directory, new every session. So the first sign-in is the deployment's first
sign-in: admin, with the CALLIOPE_ADMIN_PASSWORD this session made up, and a
password of the session's choosing straight after (D21). Nothing here is a
test-only door: every backend believes the gateway's signature and nothing
else, and the harness reaches them through the gateway with a key or a
session like anyone else, or through the fakes' control port.

MANUAL USE, for poking at the stack with curl while writing a test:

    <venv>/bin/python stack.py

takes the same machine-wide lock the tests take, signs the admin in, prints
the URLs and the path of a file only you can read that holds the admin's
password and an admin key (never the values themselves; the file goes with
the run), and stops everything on Ctrl-C or after 20 minutes. It starts no
browser.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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

# The session cookie's name on a loopback bind with CALLIOPE_DEV_INSECURE_COOKIE
# (D16); at home it is __Host-calliope_session.
COOKIE = "calliope_session"
# What a page's own fetch() says about itself (D14, D15). The harness sends
# these when it acts with a person's session, as the page would; a cookie
# request without them is refused.
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}

# THE SIGN-IN BUDGET. The gateway lets one address make twenty sign-in
# attempts in ten minutes (D19; IP_ATTEMPTS in services/gateway/app/throttle.py),
# successes and step-ups included, and every attempt here comes from
# 127.0.0.1. Past that it answers 429, and the tests that meet it fail for a
# reason that has nothing to do with them. So the harness counts its own and
# holds four back: the test that spends the seventeenth fails, naming the
# budget, while the gateway would still have let it through.
SIGN_IN_LIMIT = 20
SIGN_IN_BUDGET = SIGN_IN_LIMIT - 4
SIGN_IN_WINDOW = 10 * 60


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
        # The gateway, with a key that may read /health in full: set once the
        # stack has one, so a change to a backend's health can wait until the
        # gateway has looked again (fresh_health).
        self.gateway: httpx.Client | None = None

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
        """The request log's clock now: a request logged after this has a
        greater `seq`, and one that arrived after it a greater `began`."""
        return self._ok(self.http.get("/__fake/seq"))["seq"]

    def clear_requests(self) -> None:
        self._ok(self.http.delete("/__fake/requests"))

    def reset(self) -> None:
        """Requests, failures, health overrides, transcript, jobs, glossaries and
        MeTube back to how the session started. The hub is real and is not
        reset here; see Stack.restart_hub."""
        if self._ok(self.http.post("/__fake/reset"))["health_changed"]:
            self.fresh_health()

    def fail(self, path: str, *, status: int | None = 500, method: str | None = None,
             backend: str | None = None, json_body: Any = None, body: str | None = None,
             times: int | None = None, delay: float = 0.0, headers: dict[str, str] | None = None,
             cut_after: int | None = None) -> None:
        """Answer requests matching `path` (a regular expression) with `status`
        instead of the fake's answer, `times` times or for ever; or, with
        status=None, only hold them for `delay` seconds first. backend is one
        of stt, tts, tts_long, metube. With status=None, cut_after=n breaks a
        streamed /v1/audio/speech after n deltas with the service's in-band
        error frame, and `headers` go on that stream's response."""
        if json_body is None and body is None and status and status >= 400:
            json_body = {"error": {"message": f"injected {status}", "type": "server_error",
                                   "param": None, "code": "injected"}}
        self._ok(self.http.post("/__fake/fail", json={
            "path": path, "status": status, "method": method, "backend": backend, "json": json_body,
            "body": body, "times": times, "delay": delay, "headers": headers, "cut_after": cut_after}))

    def clear_failures(self) -> None:
        self._ok(self.http.delete("/__fake/fail"))

    def health(self, backend: str, **fields: Any) -> None:
        """Merge fields over a backend's /health (stt, tts, tts_long); None
        removes one. Returns once the gateway has read the backends again, so
        the next page load is told."""
        self._ok(self.http.post(f"/__fake/health/{backend}", json=fields))
        self.fresh_health()

    def fresh_health(self, seconds: float = 15.0) -> None:
        """Until the gateway's /health comes from a probe made after now. The
        gateway keeps each probe for 5 s (D50), so a page loaded straight
        after a change to a backend's health would otherwise be told the old
        one. Its own GET /health of the backends is in the request log.

        A probe counts only if it ARRIVED after now (`began`): one that was
        already being answered when the change was made carries the old
        health, even when it is logged after."""
        if self.gateway is None:
            return
        since = self.last_seq()
        ends = time.monotonic() + seconds
        while True:
            self.gateway.get("/health").raise_for_status()
            if any(probe["began"] > since
                   for probe in self.requests(method="GET", path=r"^/health$", since=since)):
                return
            if time.monotonic() > ends:
                raise TimeoutError("the gateway did not probe the backends again")
            time.sleep(0.5)

    def transcript(self, text: str) -> None:
        self._ok(self.http.put("/__fake/transcript", json={"text": text}))

    def glossaries(self, writable: bool = True, reason: str | None = None, strict: bool = False) -> None:
        """How the fake stt-stack treats profile writes until reset():
        writable=False lists `writable: false` with `reason` (when given) and
        answers a PUT or DELETE 503, as a deployment with no volume does;
        strict=True refuses a one-word left-hand side unless the PUT sends
        force, with a reason that says so, as the real service does."""
        self._ok(self.http.post("/__fake/glossaries",
                                json={"writable": writable, "reason": reason, "strict": strict}))

    def add_job(self, **fields: Any) -> dict[str, Any]:
        """A tts-long job, the session admin's unless `owner` says whose (None
        is system). Scripted by default: queued for 1 s, then one segment
        every 1.2 s. scripted=False keeps whatever status is given."""
        return self._ok(self.http.post("/__fake/jobs", json=fields))

    def jobs(self, **params: Any) -> dict[str, Any]:
        """tts-long's GET /jobs answer over every job, whoever owns it: the
        same filters (kind, status, audio, limit) and the same counts."""
        return self._ok(self.http.get("/__fake/jobs", params=params))

    def job(self, job_id: str) -> dict[str, Any] | None:
        """One job's whole record as tts-long keeps it, or None once it is gone."""
        response = self.http.get(f"/__fake/jobs/{job_id}")
        return None if response.status_code == 404 else self._ok(response)

    def backend_health(self, backend: str) -> dict[str, Any]:
        """What a backend's /health answers now (stt, tts, tts_long), overrides included."""
        return self._ok(self.http.get(f"/__fake/health/{backend}"))

    def owners(self, admin: str) -> None:
        """Whose the seeded history is. The fakes start over with it."""
        self._ok(self.http.post("/__fake/owners", json={"admin": admin}))

    def elsewhere(self, to: str, username: str, password: str, *, host: str = "127.0.0.1") -> str:
        """The address of another site's page that signs its visitor in to `to`
        as `username` by a form it submits itself. 127.0.0.1 is the gateway's
        site with another origin; localhost is another site."""
        port = self.base.rsplit(":", 1)[1]
        query = urlencode({"to": to, "username": username, "password": password})
        return f"http://{host}:{port}/__fake/elsewhere/sign-in?{query}"

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

    def satellite_mic(self, key: str, seconds: float = 6.0, clip: str | None = None) -> None:
        """Microphone frames at real time, for the Listen button: a quiet
        tone, or `clip`, one of services/satellites/tests/fixtures by file name
        (hey_jarvis_en_gb.wav), as the room heard it."""
        self._ok(self.http.post(f"/__fake/satellites/{key}/mic", json={"seconds": seconds, "clip": clip}))

    def satellite_caps(self, key: str, **caps: Any) -> dict[str, Any]:
        """Change what a satellite says it is (merged into its caps; None
        takes a key out) and connect it again, since caps are said only in its
        hello. A restart_hub() puts it back as it started."""
        return self._ok(self.http.post(f"/__fake/satellites/{key}/caps", json=caps))

    # -- what a wake word's action or a button's webhook reaches (fakes.py) --

    @property
    def ha_url(self) -> str:
        """Home Assistant's address, as an action's Address field takes it."""
        return f"{self.base}/__ha"

    @property
    def llm_url(self) -> str:
        """A language model server's base URL, as an action's Base URL takes it."""
        return f"{self.base}/__llm/v1"

    def hook_url(self, name: str) -> str:
        """A webhook receiver's address; what reaches it is under backend "hook"."""
        return f"{self.base}/__hook/{name}"

    def satellite_button(self, key: str, button: str, action: str = "press") -> None:
        self._ok(self.http.post(f"/__fake/satellites/{key}/button", json={"button": button, "action": action}))

    def satellite_drop(self, key: str) -> None:
        """Take a satellite offline until satellite_start."""
        self._ok(self.http.post(f"/__fake/satellites/{key}/drop"))

    def satellite_start(self, key: str) -> None:
        self._ok(self.http.post(f"/__fake/satellites/{key}/start"))

    def close(self) -> None:
        self.http.close()


# ---- the people ---------------------------------------------------------------------


@dataclass
class Account:
    """A person of this session's gateway: who, the password they chose, and
    the browser session they signed in with, which tests reuse.

    `password` is the temporary one an admin was shown until the person has
    chosen their own at first sign-in, which is the only way out of a
    must-change session (D21, D25). Every password is made up per session.
    """

    username: str
    role: str
    password: str
    id: str = ""
    # Playwright's storage state of the browser that signed in: its cookie.
    state: dict[str, Any] | None = None
    keys: dict[str, str] = field(default_factory=dict)

    @property
    def cookie(self) -> str:
        return next(c["value"] for c in (self.state or {}).get("cookies", []) if c["name"] == COOKIE)


def new_password() -> str:
    """Past every rule in D18 by construction, and never the same twice."""
    return "e2e-" + secrets.token_urlsafe(18)


class Attempts:
    """Every password this session has asked the gateway to check, by when:
    a sign-in or a step-up, from a page or from the harness (SIGN_IN_BUDGET).

    note() is told of each answer. An answer the gateway gives before its
    throttle is reached (a CSRF or wrong-host refusal, a body that is not
    JSON) is no attempt; a wrong password and a 429 are. A password change
    from Account would count as well, but nothing here makes one: the forced
    change after a first sign-in asks for no current password, and the
    gateway does not count it. What went over the budget, or was throttled,
    is kept in `over` until conftest.sign_ins_within_budget fails the test
    with it."""

    # Besides a success: the wrong password (401 at sign-in, 403 at step-up)
    # and the throttle's own refusal.
    COUNTED = {"/auth/login": (401, 429), "/auth/step-up": (403, 429)}

    def __init__(self) -> None:
        self.times: list[float] = []
        self.over: list[str] = []

    def note(self, path: str, status: int, by: str) -> None:
        refusals = self.COUNTED.get(path)
        if refusals is None or not (status < 300 or status in refusals):
            return
        now = time.monotonic()
        self.times = [t for t in self.times if t > now - SIGN_IN_WINDOW] + [now]
        if status == 429:
            self.over.append(f"the gateway throttled {path} ({by}) after {len(self.times)} attempts "
                             f"in ten minutes: it allows {SIGN_IN_LIMIT} from one address (D19)")
        elif len(self.times) > SIGN_IN_BUDGET:
            self.over.append(f"{len(self.times)} sign-in attempts in ten minutes, the latest {path} "
                             f"({by}): the harness's budget is {SIGN_IN_BUDGET} of the gateway's "
                             f"{SIGN_IN_LIMIT} per address (D19). Reuse a signed-in person "
                             "rather than signing in again")

    def take(self) -> list[str]:
        over, self.over = self.over, []
        return over


# ---- the stack ---------------------------------------------------------------------


class Stack:
    """Start and stop the fakes, the gateway, the page server and the hub.

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
                      ("stt", "tts", "long", "metube", "control", "hub", "gateway", "internal", "ui")}
        self.url = f"http://127.0.0.1:{self.ports['gateway']}"
        self.ui_direct = f"http://127.0.0.1:{self.ports['ui']}"
        self.hub = f"http://127.0.0.1:{self.ports['hub']}"
        self.fake = FakeControl(f"http://127.0.0.1:{self.ports['control']}")
        self.wake_words: str = ""
        # The gateway's volumes (D7): gateway-data, calliope-keys and every
        # calliope-svc-<name>, which each service reads as its /run/calliope.
        self.svc = self.run / "svc"
        # CALLIOPE_ADMIN_PASSWORD, the first-access value: made up here, used
        # once by the first sign-in, and never a password anyone keeps.
        self.bootstrap = new_password()
        self.admin = Account("admin", "admin", self.bootstrap)
        self.people: dict[str, Account] = {"admin": self.admin}
        # The harness's own admin key, minted once the admin has signed in:
        # what adopts the scripted satellites and what `api` sends.
        self.key: str | None = None
        self.attempts = Attempts()
        # The names store_secret has put in the gateway's store since the
        # store was last cleared (clear_secrets).
        self.stored: set[str] = set()
        self._api: httpx.Client | None = None
        self._stopped = False

    # -- lifecycle --

    def start(self) -> Stack:
        """Everything up, nobody signed in, no satellite connected yet: those
        need the admin, whose first sign-in is the caller's (conftest signs in
        through the page; main() through the API). Then provision()."""
        self._prune_runs()
        if self.run.exists():
            shutil.rmtree(self.run)
        (self.run / "tmp").mkdir(parents=True)
        (self.run / "voices").mkdir()
        atexit.register(self.stop)
        try:
            self._spawn("fakes", ["--fakes", "--repo", str(REPO), "--svc-dir", str(self.svc),
                                  *(f"--{n}-port={self.ports[n]}" for n in ("stt", "tts", "long",
                                                                          "metube", "control"))],
                        cwd=HERE, env={})
            self._wait("fakes", f"http://127.0.0.1:{self.ports['control']}/__fake/health")
            self._spawn("gateway", ["--app", "app.main:app", "--app-dir", str(SERVICES / "gateway"),
                                    "--port", str(self.ports["gateway"])],
                        cwd=SERVICES / "gateway", env={
                            "CALLIOPE_DATA_DIR": str(self.run / "gateway-data"),
                            "CALLIOPE_KEYS_DIR": str(self.run / "gateway-keys"),
                            "CALLIOPE_SVC_DIR": str(self.svc),
                            "CALLIOPE_ADMIN_PASSWORD": self.bootstrap,
                            "CALLIOPE_PUBLIC_ORIGIN": self.url,
                            # Honoured only because the bind is loopback (D16);
                            # GATEWAY_BIND says what uvicorn was given.
                            "CALLIOPE_DEV_INSECURE_COOKIE": "1",
                            "GATEWAY_BIND": "127.0.0.1",
                            "GATEWAY_INTERNAL_BIND": "127.0.0.1",
                            "GATEWAY_INTERNAL_PORT": str(self.ports["internal"]),
                            "GATEWAY_STT_URL": f"http://127.0.0.1:{self.ports['stt']}",
                            "GATEWAY_TTS_URL": f"http://127.0.0.1:{self.ports['tts']}",
                            "GATEWAY_TTS_LONG_URL": f"http://127.0.0.1:{self.ports['long']}",
                            "GATEWAY_UI_URL": self.ui_direct,
                            "GATEWAY_SATELLITES_URL": self.hub,
                            "GATEWAY_LOG_LEVEL": "WARNING"})
            # The gateway mints every service's key and identity.pub before it
            # answers, so the services after it start with their credentials.
            self._wait("gateway", f"{self.url}/health")
            self._start_hub()
            self._spawn("ui", ["--app", "app.main:app", "--app-dir", str(SERVICES / "ui"),
                               "--port", str(self.ports["ui"])],
                        cwd=SERVICES / "ui", routed=True, env={
                            "CALLIOPE_RUN_DIR": str(self.svc / "ui"),
                            "UI_METUBE_URL": f"http://127.0.0.1:{self.ports['metube']}",
                            # WAV, not the production opus: the fake MeTube
                            # serves a WAV, and a name that says so keeps the
                            # content type honest all the way to <audio>.
                            "UI_METUBE_FORMAT": "wav",
                            # THE PROBE ON, AND THE REAL yt-dlp OUT OF REACH. The
                            # venv holds a real one, and it would reach for the
                            # network from a subprocess launch.py does not wall
                            # in, so the venv is left off this PATH altogether:
                            # the only yt-dlp the page server can find is
                            # e2e/bin/yt-dlp, which prints an info-dict chosen by
                            # a word in the link (live, subs, long, unprobed) and
                            # lets the confirm card's every branch be reached.
                            # The page server itself is started by absolute path
                            # and spawns nothing else.
                            "UI_PROBE": "1",
                            "PATH": f"{HERE / 'bin'}:/usr/bin:/bin",
                            # Twelve a minute is the deployment's rate limit for
                            # one person, and a file of link tests passes it in
                            # under a minute as the one admin, so it would read
                            # 429s it did not cause.
                            "UI_RESOLVE_PER_MINUTE": "600",
                            "UI_VOICE_DIR": str(self.run / "voices"),
                            "UI_LOG_LEVEL": "WARNING"})
            self._wait("ui", f"{self.ui_direct}/health")
        except BaseException:
            self.stop()
            raise
        return self

    def first_sign_in(self) -> None:
        """The admin's first sign-in over the API, for main(): the bootstrap
        value, then a password of the session's own. The tests sign in
        through the page instead (conftest)."""
        with self.person(None) as http:
            self._ok(http.post("/auth/login", json={"username": "admin", "password": self.bootstrap}))
            self.admin.password = new_password()
            self._ok(http.post("/auth/password", json={"new_password": self.admin.password}))
            self.admin.state = {"cookies": [{"name": COOKIE, "value": http.cookies[COOKIE]}]}

    def provision(self) -> None:
        """Once the admin has signed in (self.admin.state holds the session):
        their ID, which the fakes seed the history under; an admin key for the
        harness, minted as any admin mints one, with the password again (D13);
        and the satellites, adopted with it."""
        with self.person(self.admin) as http:
            self.admin.id = self._ok(http.get("/auth/me"))["user"]["id"]
            self._ok(http.post("/auth/step-up", json={"password": self.admin.password}))
            self.key = self._ok(http.post("/auth/keys", json={
                "name": "e2e harness", "preset": "admin", "expires_days": 30}))["plaintext"]
        self.fake.owners(self.admin.id)
        self.fake.gateway = self.api
        self._seed_voices(self.admin.id)
        self._connect_satellites()

    def create_person(self, username: str, role: str = "user-jobs") -> Account:
        """A person an admin created, with the temporary password they were
        shown (D25). The first sign-in is the caller's, through the page."""
        with self.person(self.admin) as http:
            created = self._ok(self._stepped_up(http, self.admin, lambda: http.post(
                "/admin/users", json={"username": username, "role": role})))
        person = Account(username, role, created["temporary_password"], id=created["user"]["id"])
        self.people[username] = person
        return person

    def mint_key(self, person: Account, preset: str, *, name: str | None = None,
                 expires_days: int = 30) -> str:
        """A key of `preset`, made with the person's own session as the Account
        tab makes one. A preset that needs the password again gets it."""
        body = {"name": name or f"e2e {preset}", "preset": preset, "expires_days": expires_days}
        with self.person(person) as http:
            return self._ok(self._stepped_up(http, person, lambda: http.post("/auth/keys", json=body)))[
                "plaintext"]

    def store_secret(self, name: str, value: str | None, *, hosts: list[str],
                     kind: str = "bearer", consumers: tuple[str, ...] = ("satellites",)) -> None:
        """A secret in the gateway's store, as Admin › Secrets stores one: the
        admin's session, the password again when it is asked for (D13), the
        services that may read it and the hosts it may go to (D41). None
        clears it."""
        path = f"/admin/secrets/{name}"
        with self.person(self.admin) as http:
            def send() -> httpx.Response:
                if value is None:
                    return http.delete(path)
                return http.put(path, json={"value": value, "kind": kind, "consumers": list(consumers),
                                            "allowed_hosts": hosts})
            answer = self._stepped_up(http, self.admin, send)
            if value is None and answer.status_code == 404:
                return  # nothing was stored, which is what clearing asks for
            if answer.is_error:
                self._ok(answer)
            if value is not None:
                self.stored.add(name)

    def clear_secrets(self) -> None:
        """Every value in the gateway's store cleared. The store outlives the
        hub, which restart_hub starts afresh, and every test, so a key one
        test stored would otherwise be what the next test's hub, or its
        Admin › Secrets, finds (conftest.no_secret_left)."""
        # Forgotten even when clearing fails: the test it failed after says
        # so, and every test after it would otherwise fail the same way.
        self.stored.clear()
        with self.person(self.admin) as http:
            for row in self._ok(http.get("/admin/secrets"))["secrets"]:
                if row["set"]:
                    self.store_secret(row["name"], None, hosts=[])

    def person(self, who: Account | None) -> httpx.Client:
        """The gateway as `who`'s page calls it: their session cookie and the
        fetch metadata a same-origin fetch() carries. None is nobody. Every
        sign-in and step-up it sends is counted against SIGN_IN_BUDGET."""
        cookies = {COOKIE: who.cookie} if who is not None and who.state else None
        return httpx.Client(base_url=self.url, timeout=30, cookies=cookies,
                            headers=SAME_ORIGIN | {"Origin": self.url},
                            event_hooks={"response": [self._attempted]})

    def _attempted(self, response: httpx.Response) -> None:
        if response.request.method == "POST":
            self.attempts.note(response.request.url.path, response.status_code, "by the harness")

    @property
    def api(self) -> httpx.Client:
        """The gateway with the harness's admin key, for what a test does
        behind the page's back: what another device sends, a direct read of
        the hub. A Bearer request, so no CSRF check applies to it."""
        if self._api is None:
            if self.key is None:
                raise RuntimeError("the stack has no admin key until provision() has run")
            self._api = httpx.Client(base_url=self.url, timeout=60,
                                     headers={"Authorization": f"Bearer {self.key}"})
        return self._api

    def client(self, key: str | None = None, **options: Any) -> httpx.Client:
        """A new client of the gateway with `key` (the harness's by default),
        for a test that closes it itself."""
        return httpx.Client(base_url=self.url, timeout=options.pop("timeout", 10),
                            headers={"Authorization": f"Bearer {key or self.key}"}, **options)

    def _stepped_up(self, http: httpx.Client, person: Account,
                    send: Callable[[], httpx.Response]) -> httpx.Response:
        """send(), and once more after the password again if the gateway asks
        for it (D13). Each step-up is a sign-in attempt to the throttle (D19),
        and one lasts ten minutes, so the harness asks only when told to."""
        answer = send()
        if answer.status_code == 403 and answer.json()["error"]["code"] == "step_up_required":
            self._ok(http.post("/auth/step-up", json={"password": person.password}))
            answer = send()
        return answer

    @staticmethod
    def _ok(response: httpx.Response) -> Any:
        if response.is_error:
            raise RuntimeError(f"{response.request.method} {response.request.url.path} answered "
                               f"{response.status_code}: {response.text[:300]}")
        return response.json()

    def _start_hub(self, fresh: bool = False) -> None:
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
        # Speech through the gateway's internal listener with the hub's own
        # key, as compose sets it (D6); launch.py answers the name.
        internal = "http://voice-gateway:8081"
        self._spawn("hub", ["--app", "app.main:app", "--app-dir", str(SERVICES / "satellites"),
                            "--port", str(self.ports["hub"])],
                    cwd=SERVICES / "satellites", routed=True, env={
                        "CALLIOPE_RUN_DIR": str(self.svc / "satellites"),
                        "SATELLITES_DATA_DIR": str(data),
                        "SATELLITES_MODEL_DIR": str(models),
                        "SATELLITES_WAKE_WORDS": self.wake_words,
                        "SATELLITES_STT_URL": internal,
                        "SATELLITES_TTS_URL": internal,
                        "SATELLITES_TIMEZONE": "Europe/London",
                        "SATELLITES_LOG_LEVEL": "WARNING",
                        "ORT_DISABLE_TELEMETRY": "1"})
        self._wait("hub", f"{self.hub}/health")
        if self.key is not None:
            self._connect_satellites(fresh)

    def _connect_satellites(self, fresh: bool = False) -> None:
        self.fake._ok(self.fake.http.post("/__fake/satellites/connect", json={
            "gateway": self.url, "key": self.key, "fresh": fresh}))

    def restart_hub(self, wipe: bool = True) -> None:
        """A fresh hub (and a fresh adoption of the satellites) for a test that
        needs one. About a second, and every open page loses its event stream
        for that second, so only where the hub's state matters. Wiped, the
        scripted satellites start over too, as they were when the stack
        started: a hub with nothing on it meeting a Kitchen that an earlier
        test had forgotten is not the hub the test asked for."""
        self.stop_hub()
        if wipe:
            shutil.rmtree(self.run / "hub-data", ignore_errors=True)
        self._start_hub(fresh=wipe)

    def inject(self, key: str, wav_bytes: bytes, **params: Any) -> dict[str, Any]:
        """A recorded clip through a satellite's listening path, as POST
        /satellites/{id}/inject runs it, with play=0: nothing is sent to any
        satellite. `params` are the route's own (wake_word=...). The hub
        marks what it publishes as injected. Sent through the gateway with
        the harness's key, which holds satellites:listen."""
        nid = next(s["id"] for s in self.fake.satellites() if s["key"] == key)
        r = self.api.post(f"/satellites/{nid}/inject", params={"play": 0} | params,
                          content=wav_bytes, headers={"Content-Type": "audio/wav"}, timeout=60)
        r.raise_for_status()
        return r.json()

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
        if self._api is not None:
            with contextlib.suppress(Exception):
                self._api.close()
        for name in reversed(list(self.children)):
            self._kill(name)
        # Anything that escaped its group -- it should be nothing -- by marker.
        stray = self.survivors()
        if stray:
            kill_all(stray)
        # The logs stay; the data and every key the gateway minted do not.
        for sub in ("tmp", "hub-data", "voices", "gateway-data", "gateway-keys", "svc"):
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

    def _spawn(self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str],
               routed: bool = False) -> None:
        # routed: the service reaches the gateway's internal listener by its
        # compose name, which launch.py answers with this session's port.
        route = ["--route", f"voice-gateway:8081={self.ports['internal']}"] if routed else []
        # The child has its own copy of the descriptor, so ours closes at once.
        with open(self.run / f"{name}.log", "ab") as log:
            self.children[name] = subprocess.Popen(
                [sys.executable, str(HERE / "launch.py"), "--marker", self.marker,
                 "--parent", str(self.owner), "--deadline", str(self.deadline + 60),
                 "--violations", str(self.run / "network-violations.log"), *route, *argv],
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
        child = self.children.get(name)
        ends = time.monotonic() + timeout
        while time.monotonic() < ends:
            if child is not None and child.poll() is not None:
                break
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(url, timeout=2).status_code == 200:
                    return
            time.sleep(0.2)
        log = self.run / f"{name}.log"
        tail = log.read_text(errors="replace")[-3000:] if log.exists() else ""
        raise RuntimeError(f"{name} did not come up at {url}:\n{tail}")

    def _seed_voices(self, owner: str) -> None:
        """One cloned voice, so the Speak tab's clone group is not empty: the
        admin's own, as the tab lists the reader's own voices (D35)."""
        import wave

        voices = self.run / "voices" / "users" / owner
        voices.mkdir(parents=True)
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
                "wake_words": self.wake_words, "ports": self.ports,
                "people": {name: {"id": p.id, "role": p.role} for name, p in self.people.items()}}


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
        stack.first_sign_in()
        stack.provision()
        print(json.dumps(stack.describe(), indent=2))
        # Made up for this session and gone with it, but still a password and
        # a key: written to a file only this user can read, never printed.
        access = stack.run / "tmp" / "access.json"
        with os.fdopen(os.open(access, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            json.dump({"username": "admin", "password": stack.admin.password, "key": stack.key}, out)
        print(f"the admin's password and an admin key are in {access}")
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
