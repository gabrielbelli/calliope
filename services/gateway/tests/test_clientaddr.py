"""Whose address a request is: X-Forwarded-For and PROXY protocol (D67, M4).

The client address keys the per-IP login limit and fills the audit's `ip`
column. Two ways to get it wrong are fenced here: believing a header the
client wrote (the leftmost X-Forwarded-For entry), and in TCP passthrough
seeing only HAProxy, which would turn "20 attempts per IP" into one limit for
the whole internet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest
from conftest import PASSWORD, SAME_ORIGIN, gateway, make_user, sign_in

from app import clientaddr, proxyproto

PROXIES = clientaddr.parse_networks("10.0.0.2/32, 172.18.0.0/16")


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("10.0.0.2", "198.51.100.7", "198.51.100.7"),
    # The leftmost entry is whatever the client chose to send.
    ("10.0.0.2", "203.0.113.66, 198.51.100.7", "198.51.100.7"),
    # A trusted hop in the middle is skipped over, never believed past.
    ("10.0.0.2", "203.0.113.66, 198.51.100.7, 172.18.0.9", "198.51.100.7"),
    # A peer that is not a trusted proxy: its header is ignored entirely.
    ("192.0.2.50", "198.51.100.7", "192.0.2.50"),
    # No header at all from the proxy: the proxy is the client.
    ("10.0.0.2", None, "10.0.0.2"),
    # Every hop trusted: the first of them is the client.
    ("10.0.0.2", "172.18.0.9", "172.18.0.9"),
    # An IPv4 client seen through an IPv6 socket.
    ("::ffff:192.0.2.50", None, "192.0.2.50"),
])
def test_the_client_is_the_rightmost_address_no_trusted_proxy_wrote(peer, forwarded,
                                                                     expected):
    assert clientaddr.client_ip(peer=peer, forwarded_for=forwarded,
                                networks=PROXIES) == expected


def test_without_trusted_proxies_no_header_is_believed():
    assert clientaddr.client_ip(peer="10.0.0.2", forwarded_for="198.51.100.7",
                                networks=()) == "10.0.0.2"


def test_a_bad_trusted_proxy_entry_is_skipped_not_fatal(caplog):
    assert clientaddr.parse_networks("10.0.0.0/8, not-a-cidr") == (
        ipaddress.ip_network("10.0.0.0/8"),)
    assert "not-a-cidr" in caplog.text


async def test_the_throttle_and_the_audit_see_the_forwarded_client(monkeypatch):
    monkeypatch.setenv("CALLIOPE_TRUSTED_PROXIES", "192.0.2.1/32")
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        await sign_in(client, "ana", "wrong but long enough",
                      **{"x-forwarded-for": "203.0.113.66, 198.51.100.7"})
        row = main.runtime.get().db.one(
            "SELECT ip FROM audit WHERE action = 'login_failed' AND aggregated = 0")

    assert row["ip"] == "198.51.100.7"


async def test_the_proxy_s_own_header_line_wins_over_one_the_client_sent(monkeypatch):
    """HAProxy's `option forwardfor` appends a line after the client's own, and
    reading only the first line once let the client choose its address."""
    monkeypatch.setenv("CALLIOPE_TRUSTED_PROXIES", "192.0.2.1/32")
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        await client.post("/auth/login", json={"username": "ana",
                                               "password": "wrong but long enough"},
                          headers=[*SAME_ORIGIN.items(),
                                   ("x-forwarded-for", "203.0.113.250"),
                                   ("x-forwarded-for", "198.51.100.7")])
        row = main.runtime.get().db.one(
            "SELECT ip FROM audit WHERE action = 'login_failed' AND aggregated = 0")

    assert row["ip"] == "198.51.100.7"


@pytest.mark.parametrize("lines,status", [(["https"], 200), (["https", "http"], 403)])
async def test_the_scheme_is_the_one_the_proxy_wrote_last(monkeypatch, lines, status):
    """A client's `https` line in front of the proxy's `http` must not make a
    cleartext request look like the public origin (D16)."""
    monkeypatch.setenv("CALLIOPE_TRUSTED_PROXIES", "192.0.2.1/32")
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        response = await client.post(
            "/auth/login", json={"username": "ana", "password": PASSWORD},
            headers=[*SAME_ORIGIN.items(), *(("x-forwarded-proto", line) for line in lines)])

    assert response.status_code == status


# ── PROXY protocol ────────────────────────────────────────────────────────────


def v2(client: str, port: int = 51000, *, local: bool = False) -> bytes:
    address = ipaddress.ip_address(client)
    family = 0x11 if address.version == 4 else 0x21
    body = (address.packed + ipaddress.ip_address("192.0.2.80" if address.version == 4
                                                  else "2001:db8::80").packed
            + port.to_bytes(2, "big") + (443).to_bytes(2, "big"))
    command = 0x20 if local else 0x21
    return (proxyproto.V2_SIGNATURE + bytes([command, family])
            + len(body).to_bytes(2, "big") + body)


@pytest.mark.parametrize("header,expected", [
    (b"PROXY TCP4 203.0.113.7 192.0.2.80 51000 443\r\n", "203.0.113.7"),
    (b"PROXY TCP6 2001:db8::7 2001:db8::80 51000 443\r\n", "2001:db8::7"),
    (b"PROXY UNKNOWN\r\n", None),
    (v2("203.0.113.7"), "203.0.113.7"),
    (v2("2001:db8::7"), "2001:db8::7"),
    (v2("203.0.113.7", local=True), None),
])
def test_a_proxy_header_is_read_to_its_exact_length(header, expected):
    length, client = proxyproto.parse(header + b"\x16\x03\x01TLS bytes")
    assert (length, client) == (len(header), expected)


@pytest.mark.parametrize("partial", [b"PRO", b"PROXY TCP4 203.0.113.7", v2("203.0.113.7")[:20]])
def test_half_a_header_asks_for_more(partial):
    assert proxyproto.parse(partial) is None


@pytest.mark.parametrize("junk", [b"GET / HTTP/1.1\r\n", b"\x16\x03\x01\x02\x00",
                                  b"PROXY TCP4 not-an-address x 1 2\r\n"])
def test_anything_else_is_not_a_header(junk):
    with pytest.raises(proxyproto.Malformed):
        proxyproto.parse(junk)


class Recorder(asyncio.Protocol):
    """Stands in for uvicorn's protocol: what arrived after the PROXY header."""

    received: list[bytes] = []

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        Recorder.received.append(data)
        self.transport.write(b"ok")


async def _serve(trusted: str):
    loop = asyncio.get_running_loop()
    listening = socket.create_server(("127.0.0.1", 0))
    addresses = proxyproto.Addresses()
    listener = proxyproto.Listener(loop, Recorder, listening, ssl=None,
                                   networks=clientaddr.parse_networks(trusted),
                                   addresses=addresses).start()
    return listener, addresses, listening.getsockname()[1]


async def _exchange(port: int, payload: bytes) -> tuple[bytes, int]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    local_port = writer.get_extra_info("sockname")[1]
    writer.write(payload)
    await writer.drain()
    try:
        answer = await asyncio.wait_for(reader.read(10), 2)
    except ConnectionResetError:
        # Closed with our bytes still unread, which the kernel turns into a
        # reset: the same refusal as a clean close.
        answer = b""
    finally:
        writer.close()
    return answer, local_port


async def test_proxy_v2_from_a_trusted_peer_sets_the_client_address():
    Recorder.received = []
    listener, addresses, port = await _serve("127.0.0.1/32")
    try:
        answer, local_port = await _exchange(port, v2("203.0.113.7") + b"hello")
    finally:
        listener.close()

    assert answer == b"ok"
    assert b"".join(Recorder.received) == b"hello", "the header reached the HTTP layer"
    assert addresses.get(("127.0.0.1", local_port)) == "203.0.113.7"


async def test_a_trusted_peer_without_a_header_is_closed():
    """In this mode every connection from the proxy must say whose it is."""
    Recorder.received = []
    listener, _, port = await _serve("127.0.0.1/32")
    try:
        answer, _ = await _exchange(port, b"GET / HTTP/1.1\r\n\r\n")
    finally:
        listener.close()
    assert answer == b"" and not Recorder.received


async def test_a_proxy_header_from_an_untrusted_peer_is_refused():
    """Only a trusted proxy may say where a connection came from."""
    Recorder.received = []
    listener, addresses, port = await _serve("10.0.0.2/32")
    try:
        refused, _ = await _exchange(port, v2("203.0.113.7") + b"hello")
        plain, _ = await _exchange(port, b"hello")
    finally:
        listener.close()

    assert refused == b""
    assert plain == b"ok" and Recorder.received == [b"hello"]
    assert not addresses._map


@pytest.mark.parametrize("trusted,partial", [
    ("10.0.0.2/32", b"P"),                           # could be PROXY, could be POST
    ("10.0.0.2/32", b"\r\n\r\n"),                     # could be PROXY v2
    ("127.0.0.1/32", b"PROXY TCP4 203.0.113.7"),      # a header with no end
], ids=["untrusted-v1", "untrusted-v2", "trusted"])
async def test_a_peer_that_stops_part_way_through_a_header_is_closed_in_time(
        monkeypatch, trusted, partial):
    """One byte and then silence once kept a task waking every 10 ms for ever,
    on the loop that also relays the satellites."""
    monkeypatch.setattr(proxyproto, "HEADER_TIMEOUT", 0.5)
    Recorder.received = []
    listener, _, port = await _serve(trusted)
    loop = asyncio.get_running_loop()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(partial)
        await writer.drain()
        started = loop.time()
        try:
            answer = await asyncio.wait_for(reader.read(10), 3)
        except ConnectionResetError:
            answer = b""
        waited = loop.time() - started
        writer.close()
        await asyncio.sleep(0)
        handlers = len(listener._tasks)
    finally:
        listener.close()

    assert answer == b"" and not Recorder.received
    assert waited < 1.5
    assert handlers == 0, "the handler outlived its deadline"


def test_the_event_loop_swaps_in_the_listener_only_when_asked(monkeypatch):
    async def bound():
        loop = asyncio.get_running_loop()
        server = await loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
        kind = type(server).__name__
        server.close()
        return kind

    monkeypatch.delenv("CALLIOPE_PROXY_PROTOCOL", raising=False)
    with asyncio.Runner(loop_factory=proxyproto.EventLoop) as runner:
        plain = runner.run(bound())
    monkeypatch.setenv("CALLIOPE_PROXY_PROTOCOL", "1")
    monkeypatch.setenv("CALLIOPE_TRUSTED_PROXIES", "127.0.0.1/32")
    with asyncio.Runner(loop_factory=proxyproto.EventLoop) as runner:
        proxied = runner.run(bound())

    assert (plain, proxied) == ("Server", "Listener")


async def test_the_middleware_reads_the_address_proxy_protocol_registered(monkeypatch):
    monkeypatch.setenv("CALLIOPE_PROXY_PROTOCOL", "1")
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        proxyproto.ADDRESSES.put(("192.0.2.1", 50000), "203.0.113.7")
        make_user("ana")
        await client.post("/auth/login", headers={
            **SAME_ORIGIN, "x-forwarded-for": "198.51.100.99"},
            json={"username": "ana", "password": "wrong but long enough"})
        row = main.runtime.get().db.one(
            "SELECT ip FROM audit WHERE action = 'login_failed' AND aggregated = 0")

    assert row["ip"] == "203.0.113.7"
