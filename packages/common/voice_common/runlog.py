"""Post a finished run to tts-long, or don't. Never on the caller's clock.

    /speak or /transcribe -> record(**fields) -> a bounded queue
        -> POST http://voice-gateway:8081/runs, the gateway's internal listener
        -> tts-long

tts-long already keeps a record of every cloned voice job; Kokoro and Parakeet
kept none, so two of the three engines in this stack were invisible the moment
their response left. This is the sender half of fixing that. The receiver is
tts-long's POST /runs, and the shape of the body is pinned by
packages/common/tests/fixtures/run_records.json, which both halves read.

THROUGH THE GATEWAY, AS THIS SERVICE (D6). Every POST carries this service's
key from service.key on its credential volume, so it goes to the gateway's
internal listener, http://voice-gateway:8081, and nowhere else. RUNLOG_URL
switches the log on and must name exactly that address. Any other value turns
the log off with an ERROR: a POST straight to tts-long, the old setting, would
hand the key to a peer that is not the gateway and be refused anyway. For the
same reason a redirect is never followed and the proxy variables are never
read. After a 401 the key is read again, once, in case the gateway rotated it.

WHOSE RUN IT WAS travels as `owner` and `credential`, taken by the caller from
the assertion its own request carried. A record without an owner is a system
record, visible only to holders of jobs:read:all, so a forgotten owner hides a
run from its user rather than showing it to anybody else.

THE RESPONSE MUST NOT WAIT ON THIS. /speak and /transcribe are sync `def`
handlers on AnyIO's forty-thread pool, so a synchronous POST from inside one is
added to the reply of somebody who is sitting in front of it, and a tts-long
that is merely slow to answer would become a tts-stack that is slow to speak.
One daemon thread, a bounded queue, and a full queue is a DROPPED record: a log
that can stall the service it logs is worse than no log at all.

THE STACK MUST RUN COMPLETELY WITH tts-long STOPPED, so RUNLOG_URL unset is the
default and `record` is then a no-op that starts no thread. Nothing in either
service's behaviour may depend on a record having been accepted, or on there
being anywhere to send one.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .identity import GATEWAY_INTERNAL, Credentials, outbound_headers
from .scopes import CREDENTIAL, SERVICE_SUB, USER_ID

__all__ = ["RunLog", "host_label", "SERVER_ONLY", "TEXT_LIMIT"]

log = logging.getLogger(__name__)

# Fields tts-long derives or owns. A sender that sets one of these is REJECTED
# with 400 rather than tolerated -- `path` in particular would let a sender
# choose the argument to open() -- so the guard in `record` drops them loudly
# instead of posting a body that cannot be accepted.
SERVER_ONLY = frozenset({
    "path", "bytes", "reference", "segments", "stream", "parts", "recovered",
    "text_preview", "text_length", "audio",
})

# Who ran it, checked here because the receiver trusts a service principal's
# word for it: a value that is not an owner or a credential is dropped, and the
# run becomes a system record rather than somebody else's.
_OWNER = (USER_ID, SERVICE_SUB)

# The receiver's own cap on `text`. Truncated by the sender, never rejected for
# length: a transcript of an hour-long recording is a valid run and losing the
# whole record because the text was long would be the worst reading of a cap.
TEXT_LIMIT = 4096

# How many records may be waiting before new ones are dropped. Small on
# purpose. This is a log, not a spool: the alternative to dropping is holding
# memory for a receiver that may never come back, and a queue that grows is how
# a logging path takes down the service it was added to.
MAX_QUEUED = 64

# One warning per this many seconds, however many records are lost in between.
# A drop is a real event and a drop storm is one event repeated; logging each
# would put the noisiest line in the file exactly when the disk or the network
# is already in trouble.
DROP_WARN_EVERY = 60.0


def host_label() -> str:
    """WHICH MACHINE THIS IS, and it is a value rather than a lookup.

    Every record carries it, including the two kinds that have only ever run
    in one place. That is the point: a second machine appearing in the listing
    is then a data change, not a change to every reader that has to learn a new
    field. platform.node() is the honest default -- inside a container it is
    the container id unless compose says otherwise, which is why compose.yaml
    sets AIV_HOST_LABEL.
    """
    return os.getenv("AIV_HOST_LABEL", "").strip() or platform.node()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A 30x ends the request as an HTTPError instead of repeating it elsewhere.

    urllib repeats a redirected request with its Authorization header, so a
    receiver answering 302 would hand this service's key to whatever host its
    Location names.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def urlopen(request: urllib.request.Request, *, timeout: float):  # noqa: ANN201
    """urllib's urlopen with no proxy and no redirect: the key reaches the URL named.

    The default opener reads HTTP_PROXY and its relatives, and follows 301,
    302, 303 and 307. Either sends the key to a host that is not the gateway.
    A module function, so a test replaces this one name to stand in for the
    receiver.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         _RefuseRedirects())
    return opener.open(request, timeout=timeout)


class RunLog:
    """One sender, one daemon thread, one bounded queue. Never raises.

    Built once per process from the environment (`from_env`) and swapped
    wholesale in tests. `record` is called from the request thread and returns
    immediately; everything after it happens on the sender thread, where a
    failure costs the record and nothing else.
    """

    def __init__(self, url: str | None, host: str, service: str, engine: str,
                 *, credentials: Credentials | None = None, timeout: float = 2.0,
                 send_text: bool = True) -> None:
        self.url = (url or "").strip().rstrip("/") or None
        self.host = host
        self.service = service
        self.engine = engine
        self.credentials = credentials or Credentials()
        self.timeout = timeout
        self.send_text = send_text
        self.sent = 0
        self.dropped = 0
        self.last_error: str | None = None
        if self.url is not None and self.url != GATEWAY_INTERNAL:
            # Off, not redirected: whoever set the variable meant somewhere,
            # and quietly posting to the gateway instead would hide that. The
            # value is never logged, since a URL can carry a password.
            self.url = None
            self.last_error = f"RUNLOG_URL must be {GATEWAY_INTERNAL}"
            log.error("runlog: RUNLOG_URL is not %s, the gateway's internal "
                      "listener, so no run record is sent: each one carries "
                      "this service's key", GATEWAY_INTERNAL)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=MAX_QUEUED)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._warned_at = 0.0

    @classmethod
    def from_env(cls, service: str, engine: str) -> RunLog:
        """The two services read the SAME environment, spelled once.

        Three hand-vendored copies of one wire contract is what this package
        exists to stop. A second service reading RUNLOG_URL with a different
        default is the same failure in miniature. The key is not in the
        environment at all any more: it is the service key file (D7), and a
        leftover RUNLOG_KEY is reported by voice_common.auth and ignored.
        """
        return cls(
            url=os.getenv("RUNLOG_URL"),
            host=host_label(),
            service=service,
            engine=engine,
            timeout=float(os.getenv("RUNLOG_TIMEOUT", "2.0")),
            # WHAT WAS SAID, OR WHAT WAS HEARD, LEAVES THIS CONTAINER unless
            # this is turned off. A transcript is the most sensitive thing
            # either service handles, and an operator who wants the timings
            # without the content must be able to say so without losing the
            # record. `chars` still goes, so the length survives the switch.
            send_text=os.getenv("RUNLOG_TEXT", "1") not in {"0", "false", "no"},
        )

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def record(self, **fields: Any) -> None:
        """Enqueue one finished run. Never blocks, never raises, may drop.

        Callable from anywhere, including a generator's `finally` while a
        GeneratorExit is propagating, which is the one place in tts-stack where
        a streamed run's numbers are final. So it must not yield, must not
        await, and must not let anything out.
        """
        try:
            if not self.enabled:
                return
            body = self._body(fields)
            try:
                self._queue.put_nowait(body)
            except queue.Full:
                # UNDER THE LOCK, unlike `sent`, and the asymmetry is the
                # reason: `sent` is only ever touched by the one sender thread,
                # while this runs on whichever request threads found the queue
                # full — and a `+= 1` that loses a race would under-report the
                # only number that says records are being lost. It costs
                # nothing, because reaching here already means the log is
                # behind.
                with self._lock:
                    self.dropped += 1
                self._warn_dropped()
                return
            self._ensure_thread()
        except Exception as exc:  # noqa: BLE001 - a log must never be the fault
            self.last_error = f"{type(exc).__name__}: {exc}"

    def stats(self) -> dict[str, Any]:
        """What /health reports, and the only reason a drop is honest.

        A dropped record is invisible as an absence -- the row simply is not
        there and nothing anywhere says why. Reported here it is a number that
        went up, which is the difference between "nothing ran" and "the log
        could not keep up".
        """
        return {"url": self.url,
                "queued": self._queue.qsize(),
                "sent": self.sent,
                "dropped": self.dropped,
                "last_error": self.last_error}

    # ---------------------------------------------------------- internals --

    def _body(self, fields: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"host": self.host,
                                "service": self.service,
                                "engine": self.engine}
        for key, value in fields.items():
            if value is None:
                # ABSENT, NOT NULL. The contract reads an absent field as "this
                # run has no such number"; a null would have to be special-cased
                # by every reader, and `finished_at: null` is how a finished run
                # would come to look live.
                continue
            if key in SERVER_ONLY:
                # Loud rather than silent: tts-long answers 400 to this, so a
                # sender that grows one of these fields would start losing every
                # record with nothing but a rejection count to show for it.
                log.warning("runlog: %r is set by tts-long and was dropped from "
                            "this record; sending it would be a 400", key)
                continue
            if key == "owner" and not (isinstance(value, str) and any(
                    pattern.fullmatch(value) for pattern in _OWNER)):
                log.warning("runlog: an owner that is not a user or a service "
                            "was dropped; the run is recorded as a system run")
                continue
            if key == "credential" and not (isinstance(value, str)
                                            and CREDENTIAL.fullmatch(value)):
                log.warning("runlog: a credential that is not a session, a key "
                            "ID or a service was dropped from this record")
                continue
            body[key] = value
        text = body.get("text")
        if isinstance(text, str):
            if not self.send_text:
                del body["text"]
            elif len(text) > TEXT_LIMIT:
                body["text"] = text[:TEXT_LIMIT]
        return body

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # STARTED ON THE FIRST RECORD, not at import. A service with
            # RUNLOG_URL unset never reaches here, so the disabled case costs a
            # None check and no thread at all -- which is what makes "unset is
            # the default" free rather than merely cheap.
            self._thread = threading.Thread(target=self._drain,
                                            name="runlog", daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        while True:
            body = self._queue.get()
            try:
                self._post(body)
                self.sent += 1
            except Exception as exc:  # noqa: BLE001 - see _post
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def _post(self, body: dict[str, Any]) -> None:
        """One POST, one timeout, NO RETRY -- except once after a 401.

        A failed record is a lost record and that is the correct trade. A retry
        queue behind a receiver that is down is a growing queue, and the run it
        describes has already been answered -- there is nobody left to serve by
        trying again. A 401 is the one exception: it says this service's key
        was rotated under it, which the next read of service.key fixes, so the
        record is sent once more with the new key and never more than that.
        """
        key = self.credentials.service_key()
        if key is None:
            raise RuntimeError(f"no service key yet in {self.credentials.directory}")
        data = json.dumps(body).encode("utf-8")
        try:
            self._send(data, key)
        except urllib.error.HTTPError as exc:
            fresh = self.credentials.reload_service_key() if exc.code == 401 else None
            if fresh is None or fresh == key:
                # The status is the useful half: a 404 means TTS_RUNLOG_ACCEPT=0
                # or an older tts-long, a 429 means the receiver's rate limit,
                # and a 400 means this sender is sending something the contract
                # forbids. Reported through /health, where an operator sees it.
                raise RuntimeError(f"POST /runs -> {exc.code}") from exc
            try:
                self._send(data, fresh)
            except urllib.error.HTTPError as again:
                raise RuntimeError(f"POST /runs -> {again.code}") from again

    def _send(self, data: bytes, key: str) -> None:
        request = urllib.request.Request(
            f"{self.url}/runs", data=data, method="POST",
            headers=outbound_headers(content_type="application/json",
                                     authorization=f"Bearer {key}"))
        with urlopen(request, timeout=self.timeout):
            pass

    def _warn_dropped(self) -> None:
        now = time.monotonic()
        if now - self._warned_at < DROP_WARN_EVERY:
            return
        self._warned_at = now
        log.warning("runlog: the queue is full and %d record(s) have been "
                    "dropped; the runs are unaffected, their history is not",
                    self.dropped)
