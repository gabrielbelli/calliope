"""PROXY protocol v1 and v2 on the public listener, read before TLS (D67).

    uvicorn app.main:app --loop app.proxyproto:EventLoop ...
    CALLIOPE_PROXY_PROTOCOL=1 CALLIOPE_TRUSTED_PROXIES=192.0.2.10/32

**Why this exists.** With HAProxy in TCP passthrough the gateway terminates TLS
itself, and every client arrives from HAProxy's address: "20 login attempts
per 10 minutes per IP" would become one limit for the whole internet, and the
audit would name HAProxy for everything. HAProxy's `send-proxy-v2` writes the
real client address in front of the TLS bytes, and uvicorn cannot read it.

**How.** uvicorn builds its listener with `loop.create_server(...)`. EventLoop
replaces that one call, when CALLIOPE_PROXY_PROTOCOL=1 and uvicorn asks by
host and port, with a Listener that accepts each connection itself:

1. A peer inside CALLIOPE_TRUSTED_PROXIES must send a PROXY header; it is
   read with MSG_PEEK and then consumed to its exact length, so the TLS bytes
   behind it stay in the kernel buffer untouched.
2. Any other peer that sends one is disconnected: only a trusted proxy may
   say where a connection came from.
   Either way the first bytes must settle the question within HEADER_TIMEOUT,
   or the connection is closed: a peer that sends half a header and stops
   costs a few dozen wake-ups, never a task for ever.
3. The socket is handed to `loop.connect_accepted_socket` with uvicorn's own
   protocol factory and its own SSL context, so TLS and HTTP are exactly what
   uvicorn would have done.

The client address is kept in ADDRESSES under the proxy-side (address, port)
of the connection, which is what the ASGI scope's `client` holds; the
middleware looks it up there. Each connection from a trusted peer registers
its header before its first byte reaches uvicorn, so a reused source port can
never be read with a stale address.

Everything else (no PROXY variable, a socket passed in, the internal
listener) goes to asyncio's own create_server unchanged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import ssl as ssl_module
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .clientaddr import Network, parse_networks, trusted

log = logging.getLogger("voice-gateway.proxyproto")

V1_PREFIX = b"PROXY "
V1_MAX = 107
V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
V2_MAX = 16 + 1024          # the addresses plus any TLVs HAProxy is set to add
HEADER_TIMEOUT = 10.0
# Between two looks at a header that has not grown: from 10 ms, doubling.
FIRST_PAUSE = 0.01
LONGEST_PAUSE = 0.25
MAX_ADDRESSES = 65_536


class Malformed(Exception):
    """Not a PROXY header, or one this listener refuses."""


def parse(data: bytes) -> tuple[int, str | None] | None:
    """(header length, client address) from the start of `data`.

    None means more bytes are needed. The address is None for a LOCAL (v2) or
    UNKNOWN (v1) header: the proxy's own health check, which is the peer.
    """
    if data.startswith(V2_SIGNATURE[:len(data)]) and len(data) < 16:
        return None
    if data.startswith(V2_SIGNATURE):
        version, command = data[12] >> 4, data[12] & 0x0F
        family, length = data[13], int.from_bytes(data[14:16], "big")
        if version != 2 or command not in (0, 1) or 16 + length > V2_MAX:
            raise Malformed("unsupported PROXY v2 header")
        if len(data) < 16 + length:
            return None
        body = data[16:16 + length]
        if command == 0:
            return 16 + length, None
        if family == 0x11 and length >= 12:          # TCP over IPv4
            return 16 + length, str(ipaddress.IPv4Address(body[0:4]))
        if family == 0x21 and length >= 36:          # TCP over IPv6
            address = ipaddress.IPv6Address(body[0:16])
            return 16 + length, str(address.ipv4_mapped or address)
        raise Malformed("unsupported PROXY v2 address family")
    if data.startswith(V1_PREFIX[:len(data)]) and len(data) < len(V1_PREFIX):
        return None
    if data.startswith(V1_PREFIX):
        end = data.find(b"\r\n", 0, V1_MAX + 1)
        if end < 0:
            if len(data) >= V1_MAX:
                raise Malformed("PROXY v1 header too long")
            return None
        fields = data[:end].decode("ascii", "replace").split(" ")
        if fields[1:2] == ["UNKNOWN"]:
            return end + 2, None
        if len(fields) != 6 or fields[1] not in ("TCP4", "TCP6"):
            raise Malformed("malformed PROXY v1 header")
        try:
            address = ipaddress.ip_address(fields[2])
        except ValueError:
            raise Malformed("malformed PROXY v1 address") from None
        return end + 2, str(address)
    raise Malformed("no PROXY header")


def announces_proxy(data: bytes) -> bool:
    """Does this start (or could it start) a PROXY header?"""
    return any(data[:len(prefix)] == prefix[:len(data)]
               for prefix in (V1_PREFIX, V2_SIGNATURE)) and bool(data)


class Addresses:
    """(proxy-side address, port) -> client address, for the connections open now."""

    def __init__(self, limit: int = MAX_ADDRESSES) -> None:
        self.limit = limit
        self._map: OrderedDict[tuple[str, int], str] = OrderedDict()

    def put(self, peer: tuple[str, int], client: str) -> None:
        self._map[peer] = client
        self._map.move_to_end(peer)
        while len(self._map) > self.limit:
            self._map.popitem(last=False)

    def get(self, peer: tuple[str, int] | list | None) -> str | None:
        if not peer:
            return None
        return self._map.get((str(peer[0]), int(peer[1])))


ADDRESSES = Addresses()


def enabled() -> bool:
    return os.environ.get("CALLIOPE_PROXY_PROTOCOL", "").strip() == "1"


class Listener:
    """What uvicorn needs of an asyncio.Server: `sockets`, `close()` and `wait_closed()`."""

    def __init__(self, loop: asyncio.AbstractEventLoop, factory: Callable[[], Any],
                 sock: socket.socket, *, ssl: ssl_module.SSLContext | None,
                 networks: tuple[Network, ...], addresses: Addresses = ADDRESSES) -> None:
        self.loop = loop
        self.factory = factory
        self.sockets = [sock]
        self.ssl = ssl
        self.networks = networks
        self.addresses = addresses
        self._tasks: set[asyncio.Task] = set()
        self._accepting: asyncio.Task | None = None

    def start(self) -> Listener:
        self.sockets[0].setblocking(False)
        self._accepting = self.loop.create_task(self._accept())
        return self

    def close(self) -> None:
        if self._accepting is not None:
            self._accepting.cancel()
        for task in list(self._tasks):
            task.cancel()
        self.sockets[0].close()

    async def wait_closed(self) -> None:
        if self._accepting is not None:
            await asyncio.gather(self._accepting, *self._tasks, return_exceptions=True)

    async def _accept(self) -> None:
        while True:
            conn, peer = await self.loop.sock_accept(self.sockets[0])
            task = self.loop.create_task(self._handle(conn, peer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _readable(self, conn: socket.socket, deadline: float) -> None:
        ready = self.loop.create_future()
        self.loop.add_reader(conn.fileno(), lambda: ready.done() or ready.set_result(None))
        try:
            await asyncio.wait_for(ready, max(0.0, deadline - self.loop.time()))
        finally:
            self.loop.remove_reader(conn.fileno())

    async def _peek(self, conn: socket.socket, size: int, deadline: float, *,
                    longer_than: int = 0) -> bytes:
        """Up to `size` queued bytes, left queued, once more than `longer_than` are there.

        b"" is a peer that closed. TimeoutError at the deadline, however the
        peer behaves: one byte of a header and then silence must not hold a
        task, and a slot on the loop that relays the satellites, for ever.

        Readability can wait for the first byte, but not for one more: a
        peeked byte stays queued, so the socket stays readable and add_reader
        would fire at once, every time. So a header that has not grown is
        looked at again after a pause that doubles to a quarter of a second,
        a few dozen looks before the deadline rather than a thousand.
        """
        pause = FIRST_PAUSE
        while True:
            if self.loop.time() >= deadline:
                raise TimeoutError("no complete PROXY header in time")
            try:
                data = conn.recv(size, socket.MSG_PEEK)
            except BlockingIOError:
                await self._readable(conn, deadline)
                continue
            if not data or len(data) > longer_than:
                return data
            await asyncio.sleep(min(pause, max(0.0, deadline - self.loop.time())))
            pause = min(pause * 2, LONGEST_PAUSE)

    async def _header(self, conn: socket.socket, deadline: float) -> str | None:
        data = b""
        while True:
            data = await self._peek(conn, V2_MAX, deadline, longer_than=len(data))
            if not data:
                raise Malformed("closed before a PROXY header")
            parsed = parse(data)
            if parsed is not None:
                length, client = parsed
                conn.recv(length)          # exactly the header; TLS bytes stay queued
                return client
            if len(data) >= V2_MAX:
                raise Malformed("PROXY header too long")

    async def _handle(self, conn: socket.socket, peer: Any) -> None:
        deadline = self.loop.time() + HEADER_TIMEOUT
        try:
            conn.setblocking(False)
            if trusted(peer[0], self.networks):
                client = await self._header(conn, deadline)
                self.addresses.put((str(peer[0]), int(peer[1])), client or str(peer[0]))
            else:
                first = await self._peek(conn, 16, deadline)
                while first and announces_proxy(first) and len(first) < len(V2_SIGNATURE):
                    first = await self._peek(conn, 16, deadline, longer_than=len(first))
                if announces_proxy(first):
                    raise Malformed(f"PROXY header from untrusted peer {peer[0]}")
            await self.loop.connect_accepted_socket(self.factory, conn, ssl=self.ssl)
        except (Malformed, TimeoutError, OSError, ssl_module.SSLError) as exc:
            log.warning("closed a connection before HTTP: %s", exc)
            conn.close()
        except asyncio.CancelledError:
            conn.close()
            raise


class EventLoop(asyncio.SelectorEventLoop):
    """asyncio's loop, with create_server replaced for the PROXY listener only."""

    async def create_server(self, protocol_factory, host=None, port=None, *,  # type: ignore[override]
                            sock=None, ssl=None, backlog=100, **kwargs):
        if not enabled() or sock is not None:
            return await super().create_server(protocol_factory, host, port, sock=sock,
                                               ssl=ssl, backlog=backlog, **kwargs)
        networks = parse_networks(os.environ.get("CALLIOPE_TRUSTED_PROXIES", ""))
        if not networks:
            log.error("CALLIOPE_PROXY_PROTOCOL=1 with no CALLIOPE_TRUSTED_PROXIES: "
                      "no peer may send a PROXY header, so HAProxy's connections "
                      "will be closed")
        family = socket.AF_INET6 if host and ":" in str(host) else socket.AF_INET
        listening = socket.create_server((host or "0.0.0.0", port or 0), family=family,
                                         backlog=backlog)
        log.info("PROXY protocol on %s:%s, trusted peers: %s", host, port,
                 ", ".join(str(n) for n in networks) or "none")
        return Listener(self, protocol_factory, listening, ssl=ssl,
                        networks=networks).start()
