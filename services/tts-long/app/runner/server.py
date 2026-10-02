"""uvicorn for the runner: h11, TLS 1.3 only, and a deadline on every request head.

THREE THINGS uvicorn DOES BY DEFAULT THAT A LAN SERVER WITH ONE KEY CANNOT
AFFORD, each measured against the pinned fastapi 0.121.2 and uvicorn 0.38.0:

  * httptools, which `uvicorn[standard]` installs and `http="auto"` picks,
    buffered 190 MiB of request headers from a client with no key. h11 caps an
    incomplete head at 16 KiB and answers 400. Passing a protocol CLASS, as
    below, is also what makes uvicorn use h11 rather than auto.
  * `limit_concurrency` counts idle TLS sockets, so sixteen silent connections
    turned every later request into a 503 for as long as they stayed open. It
    is off; the guard in api.py caps AUTHENTICATED requests instead, and
    `DeadlineH11` closes a connection that sends no request head.
  * uvicorn's default cipher string ("TLSv1") limits TLS 1.2 to old CBC suites.
    TLS 1.3 only avoids the question: tts-long's client speaks it.

`DeadlineH11` USES uvicorn INTERNALS (`cycle`, `handle_events`,
`on_response_complete`). requirements.txt pins uvicorn, and
tests/test_runner_tls.py drives this class over real TLS, so an upgrade that
breaks it fails there first.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

PORT = 47600
# How long a connection may go without a complete request head, from the
# moment it connects or its previous response ends. Read at call time, so a
# test can patch it as a module attribute.
HEAD_DEADLINE_S = 10.0


class DeadlineH11(H11Protocol):
    """Close a connection that has not sent a complete request head in time."""

    _deadline = None

    def connection_made(self, transport):  # noqa: ANN001 - asyncio's signature
        super().connection_made(transport)
        self._arm()

    def _arm(self) -> None:
        self._disarm()
        self._deadline = self.loop.call_later(HEAD_DEADLINE_S, self.transport.close)

    def _disarm(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def handle_events(self) -> None:
        super().handle_events()
        if self.cycle is not None and not self.cycle.response_complete:
            # A request head has arrived. The body that follows is the route's
            # business and the guard's: it is bounded by Content-Length, and
            # nobody without the key gets as far as sending one.
            self._disarm()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        # NOT WHILE A PIPELINED REQUEST IS ALREADY RUNNING. The parent method
        # handles any buffered events, which may have started the next cycle;
        # arming then would close a connection in the middle of a request.
        if self.transport.is_closing():
            return
        if self.cycle is not None and not self.cycle.response_complete:
            return
        self._arm()

    def connection_lost(self, exc):  # noqa: ANN001 - asyncio's signature
        self._disarm()
        super().connection_lost(exc)


def build_config(app, cert: Path, key: Path, *, host: str = "0.0.0.0",
                 port: int = PORT, log_level: str = "info") -> uvicorn.Config:
    """The one uvicorn configuration the runner serves with, loaded.

    LOADED HERE AND NOT IN `Server.run`, because the TLS floor is set on the
    context `load()` builds and `Server.run` does not load a config twice.
    """
    config = uvicorn.Config(
        app, host=host, port=port, ssl_certfile=str(cert), ssl_keyfile=str(key),
        http=DeadlineH11, ws="none", limit_concurrency=None,
        timeout_keep_alive=5, server_header=False, date_header=True,
        access_log=False, proxy_headers=False, lifespan="on",
        log_level=log_level)
    config.load()
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_3
    return config


def serve(config: uvicorn.Config) -> None:
    uvicorn.Server(config).run()
