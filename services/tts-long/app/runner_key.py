"""The GPU runner's bearer key, from the gateway's secret store (D38-D45).

    GET  http://voice-gateway:8081/internal/secrets/TTS_RUNNER_API_KEY
         -> {"value", "version", "allowed_hosts", "max_age"}       secrets:fetch
    POST http://voice-gateway:8081/internal/secrets/import          secrets:import
         <- {"final": true, "secrets": [{"name", "value", "kind",
                                         "allowed_hosts", "source"}],
             "declared": {"TTS_RUNNER_API_KEY": [<the runner's origin>]}}

**The store wins** (D44). The key used to be a file this service read at start
(TTS_RUNNER_API_KEY_FILE) or a variable (TTS_RUNNER_API_KEY), and rotating it
meant a restart of a container holding 6.5 GB of model. Now the value lives in
Admin › Secrets and is read from the gateway, cached for at most 60 s, so a
rotation is picked up within a minute and the value never touches this
service's disk.

**The old setting is imported once, then kept only as a fallback** (D45). At
start the value it names is posted to the import route with `final: true`,
which also closes this service's import window (D66): tts-long has exactly one
secret, so its first batch is its last. The import never overwrites a value an
admin has set. After that the file or variable is read only while the gateway
cannot be reached and nothing better is cached, with a WARNING each minute.

**A failure keeps the last good value** (D42, stale-if-error). A gateway that
is restarting, unreachable or in locked mode (503) must not take the runner
away from jobs already promised to it. A 404 is different: it is an admin
clearing the secret, and a cleared secret stops at once. A miss is never
cached, so setting it again works on the next request.

**The key goes only where the secret says** (D41). The runner's own origin,
`https://<TTS_RUNNER_HOST>:<TTS_RUNNER_PORT>`, must be in the secret's
`allowed_hosts`, compared after both sides are normalised. Otherwise the
request goes without it and the runner answers 401, which fails the lane and
not the job.

**Every other runner has a secret of its own**, named for its settings:
TTS_RUNNER2_API_KEY for the runner at TTS_RUNNER2_HOST, and so on. Two runners
never share a key, so a compromise of one machine cannot call the other, and
each secret's `allowed_hosts` names its own runner. Those are set in
Admin › Secrets and nowhere else: there is no file or variable to import them
from, because the store has been where keys live since D44, and the import
above stays the one-off migration of the first runner's old setting.

Nothing here is ever logged but the secret's NAME.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from voice_common.identity import GATEWAY_INTERNAL, Credentials, outbound_headers
from voice_common.origins import normalise
from voice_common.runlog import urlopen

__all__ = ["NAME", "RunnerKey", "origin"]

log = logging.getLogger("tts-long.runner_key")

NAME = "TTS_RUNNER_API_KEY"
FILE_VARIABLE = "TTS_RUNNER_API_KEY_FILE"

# The longest a value is trusted without asking again (D42). The gateway's
# own `max_age` can only shorten it.
MAX_AGE = 60.0
# How long a failed fetch is not repeated. The probe asks the runner every ten
# seconds and a job polls it every two; without this, a gateway that hangs
# would add its timeout to every one of those calls.
RETRY_AFTER_ERROR = 10.0
# The internal network is one hop inside the compose project. A gateway that
# has not answered in two seconds is not going to, and /health waits on this.
TIMEOUT = 2.0
# How often the import is tried again while the gateway cannot be reached.
IMPORT_RETRY = 60.0
# One warning a minute per cause, however many runner requests go by.
WARN_EVERY = 60.0

def origin(value: str) -> str | None:
    """`scheme://host:port` the way D41 compares hosts, or None if it is not one.

    The gateway's store writes allowed_hosts with this same function, so both
    sides spell a place one way. A value with no scheme means https and only
    https, so a bearer key goes over plain HTTP only to an entry that says so.
    """
    try:
        return normalise(value)
    except ValueError:
        return None


class _Unreachable(Exception):
    """The gateway gave no answer worth acting on. Never carries a value."""


class RunnerKey:
    """The current runner key, or None when the request must go without one.

    Thread-safe: the probe thread, the lane threads and /health all ask.
    """

    def __init__(self, *, target: str, fallback: str = "",
                 fallback_from: str | None = None,
                 credentials: Credentials | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 name: str = NAME) -> None:
        normalised = origin(target)
        if normalised is None:
            raise ValueError(f"{target!r} is not an origin a key can be sent to")
        self.target = normalised
        # WHICH SECRET. TTS_RUNNER_API_KEY for the first runner; a second
        # runner has its own (TTS_RUNNER2_API_KEY), with its own allowed hosts,
        # so a compromise of one machine never hands it the other's key.
        self.name = name
        self._fallback = fallback
        self._fallback_from = fallback_from
        self._credentials = credentials or Credentials()
        self._clock = clock
        self._lock = threading.Lock()
        self._value: str | None = None
        self._allowed: frozenset[str] = frozenset()
        self._fresh_until = float("-inf")
        self._retry_at = float("-inf")
        # When a runner 401 may next empty the cache; see refused().
        self._refetch_after = float("-inf")
        self._warned: dict[str, float] = {}

    @classmethod
    def from_env(cls, target: str,
                 env: Mapping[str, str] | None = None) -> RunnerKey:
        """The key the old settings name, kept as the import source and the fallback.

        TTS_RUNNER_API_KEY wins over the file, as it always did.
        """
        e = os.environ if env is None else env
        value = (e.get(NAME) or "").strip()
        source = NAME if value else None
        path = (e.get(FILE_VARIABLE) or "").strip()
        if not value and path:
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
                source = FILE_VARIABLE
            except OSError as exc:
                # Named, not swallowed: an unreadable file means a runner
                # that answers 401 whenever the gateway is away.
                log.warning("cannot read %s: %s", FILE_VARIABLE, type(exc).__name__)
        return cls(target=target, fallback=value, fallback_from=source)

    # -- what the runner client calls ---------------------------------------

    def get(self) -> str | None:
        """The key to send now. Cached for up to a minute, never cached when absent."""
        with self._lock:
            now = self._clock()
            if self._value is not None and now < self._fresh_until:
                return self._for_target(self._value)
            if now < self._retry_at:
                return self._stale("the gateway did not answer a moment ago")
            try:
                status, document = self._fetch()
            except _Unreachable as exc:
                self._retry_at = now + RETRY_AFTER_ERROR
                return self._stale(str(exc))
            if status == 200:
                return self._accept(document, now)
            if status in (403, 404):
                # Cleared by an admin, never set, or this service is not one
                # of its consumers. Each of those is the store's answer, so
                # nothing older is used in its place.
                self._value, self._allowed = None, frozenset()
                self._fresh_until = float("-inf")
                self._warn(f"status-{status}",
                           "%s is %s in the gateway's secret store; runner "
                           "requests go without a key", self.name,
                           "not set" if status == 404
                           else "not readable by tts-long")
                return None
            self._retry_at = now + RETRY_AFTER_ERROR
            return self._stale(f"the gateway answered {status}")

    def refused(self, key: str) -> str | None:
        """The runner answered 401 to `key`. A different key to retry with, or None.

        The store may hold a rotated value the cache has not caught up with
        (D42), so a refusal empties the cache -- AT MOST ONCE PER
        RETRY_AFTER_ERROR. A runner that refuses every key (a wrong value in
        Admin › Secrets, or a runner rotated ahead of the store) otherwise
        turned every probe, poll and /health snapshot into a gateway fetch:
        each one an audited secret read, and each one holding `_lock`, with
        every lane thread queued behind it, while it waited on the gateway. In
        between, the cached value is the answer, and the back-off after a
        failed fetch is left alone. The same key again is not worth a retry.
        """
        with self._lock:
            now = self._clock()
            if now >= self._refetch_after:
                self._refetch_after = now + RETRY_AFTER_ERROR
                self._fresh_until = float("-inf")
        fresh = self.get()
        return fresh if fresh is not None and fresh != key else None

    # -- the import ---------------------------------------------------------

    def import_fallback(self) -> bool:
        """Post the old setting's value to the store, once. True when nothing is left to do.

        `final: true` even with no value, because closing the import window
        is the point as much as the value is: a window left open is a way to
        plant a key nobody chose (D66).
        """
        entries = []
        if self._fallback:
            # `source` in the store's words ("env X" or "file X"), which the
            # Secrets banner turns into what to remove (D44). `declared` is
            # where this service's configuration sends the key: the store
            # narrows the imported hosts to it, and with none declared the
            # row would allow no host at all.
            kind = "file" if self._fallback_from == FILE_VARIABLE else "env"
            entries.append({"name": self.name, "value": self._fallback,
                            "kind": "bearer", "allowed_hosts": [self.target],
                            "source": f"{kind} {self._fallback_from}"})
        body = json.dumps({"final": True, "secrets": entries,
                           "declared": {self.name: [self.target]}}).encode("utf-8")
        try:
            status = self._call("POST", "/internal/secrets/import", body)[0]
        except _Unreachable as exc:
            log.info("the import of %s will be tried again in %.0f s: %s",
                     self.name, IMPORT_RETRY, exc)
            return False
        if 200 <= status < 300:
            if self._fallback_from:
                log.warning("%s is set and now only a fallback: %s lives in "
                            "Admin › Secrets, and this value is used only "
                            "while the gateway cannot be reached. Remove it "
                            "after the next release.", self._fallback_from, self.name)
            return True
        if status == 410:
            # The window is closed: imported before, or closed by an admin.
            # Either way the store decides now, not this service.
            return True
        if status == 401 or status >= 500:
            # A key the gateway has not finished minting, or a gateway that
            # is restarting or locked (503): both pass on their own.
            log.info("the gateway answered %d to the import of %s; trying "
                     "again in %.0f s", status, self.name, IMPORT_RETRY)
            return False
        log.warning("the gateway refused the import of %s with %d; set it in "
                    "Admin › Secrets instead", self.name, status)
        return True

    def start_import(self) -> None:
        """Run the import on a daemon thread until the gateway has answered it."""

        def attempt() -> None:
            while not self.import_fallback():
                time.sleep(IMPORT_RETRY)

        threading.Thread(target=attempt, name="runner-key-import",
                         daemon=True).start()

    # -- inside ---------------------------------------------------------------

    def _accept(self, document: object, now: float) -> str | None:
        value = document.get("value") if isinstance(document, dict) else None
        hosts = document.get("allowed_hosts") if isinstance(document, dict) else None
        if (not isinstance(value, str) or not value or "\r" in value
                or "\n" in value or not isinstance(hosts, list)):
            self._retry_at = now + RETRY_AFTER_ERROR
            return self._stale("the gateway's answer was not a secret")
        max_age = document.get("max_age")
        age = (min(float(max_age), MAX_AGE)
               if isinstance(max_age, (int, float)) and not isinstance(max_age, bool)
               and max_age >= 0 else MAX_AGE)
        self._value = value
        self._allowed = frozenset(filter(None, (origin(h) for h in hosts
                                                if isinstance(h, str))))
        self._fresh_until = now + age
        self._retry_at = float("-inf")
        return self._for_target(value)

    def _for_target(self, value: str) -> str | None:
        if self.target in self._allowed:
            return value
        self._warn("host", "%s is not allowed for %s; add that host to the "
                   "secret in Admin › Secrets", self.name, self.target)
        return None

    def _stale(self, why: str) -> str | None:
        if self._value is not None:
            self._warn("stale", "using the last value of %s the gateway gave: %s",
                       self.name, why)
            return self._for_target(self._value)
        if self._fallback:
            self._warn("fallback", "using %s for %s: %s", self._fallback_from,
                       self.name, why)
            return self._fallback
        self._warn("none", "no %s to send to the runner: %s", self.name, why)
        return None

    def _warn(self, cause: str, message: str, *args: object) -> None:
        now = self._clock()
        if now - self._warned.get(cause, float("-inf")) < WARN_EVERY:
            return
        self._warned[cause] = now
        log.warning(message, *args)

    def _fetch(self) -> tuple[int, object]:
        status, data = self._call("GET", f"/internal/secrets/{self.name}")
        if status != 200:
            return status, None
        try:
            return status, json.loads(data)
        except ValueError:
            raise _Unreachable("the gateway's answer was not JSON") from None

    def _call(self, method: str, path: str,
              body: bytes | None = None) -> tuple[int, bytes]:
        """One request with this service's key, read again once after a 401."""
        key = self._credentials.service_key()
        if key is None:
            raise _Unreachable(f"no service key yet in {self._credentials.directory}")
        status, data = self._send(method, path, body, key)
        if status == 401:
            fresh = self._credentials.reload_service_key()
            if fresh is not None and fresh != key:
                status, data = self._send(method, path, body, fresh)
        return status, data

    @staticmethod
    def _send(method: str, path: str, body: bytes | None,
              key: str) -> tuple[int, bytes]:
        # Built from named values only, so nothing this service received can
        # ride along (D65), and through runlog's opener: no proxy variables
        # and no redirects, so the service key reaches the gateway and nothing
        # else.
        headers = outbound_headers(
            authorization=f"Bearer {key}", accept="application/json",
            content_type="application/json" if body is not None else None)
        request = urllib.request.Request(f"{GATEWAY_INTERNAL}{path}", data=body,
                                         method=method, headers=headers)
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except (urllib.error.URLError, OSError) as exc:
            raise _Unreachable(type(exc).__name__) from None
