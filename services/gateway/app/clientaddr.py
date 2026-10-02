"""Which address a request came from, trusting a proxy only when told to (D67).

    proxies = parse_networks(os.environ.get("CALLIOPE_TRUSTED_PROXIES", ""))
    client_ip(peer="10.0.0.2", forwarded_for="198.51.100.7, 10.0.0.2", trusted=proxies)

**CALLIOPE_TRUSTED_PROXIES is the only way a forwarded address is believed.**
Without it the peer is the client, whatever headers say otherwise. With it,
`X-Forwarded-For` is read only when the peer itself is a trusted proxy, and
the client is the RIGHTMOST entry that is not a trusted proxy: every entry to
its left was written by someone this gateway does not trust, and the leftmost
is whatever the client chose to send.

It must never include the Docker bridge subnet (recheck L13): every LAN client
reaches the container through it, so each could then name its own address in
the header, dodge the per-IP login limit and write a false IP into the audit.

With PROXY protocol (CALLIOPE_PROXY_PROTOCOL=1) the address comes from the
PROXY header that proxyproto.py reads before TLS, and this header is ignored.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable

log = logging.getLogger("voice-gateway.clientaddr")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(text: str) -> tuple[Network, ...]:
    """CIDRs (or bare addresses), comma or space separated. A bad entry is logged and skipped."""
    networks: list[Network] = []
    for part in text.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.error("CALLIOPE_TRUSTED_PROXIES: %r is not an address or CIDR; "
                      "it is ignored", part[:64])
    return tuple(networks)


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = text.strip()
    if text.startswith("[") and "]" in text:      # [v6]:port
        text = text[1:text.index("]")]
    elif text.count(":") == 1:                    # v4:port
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    # An IPv4 client seen over an IPv6 socket is still that IPv4 client.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def trusted(peer: str | None, networks: Iterable[Network]) -> bool:
    address = _address(peer or "")
    return address is not None and any(address in network for network in networks)


def client_ip(*, peer: str | None, forwarded_for: str | None,
              networks: tuple[Network, ...]) -> str | None:
    """The client's address for throttling, audit and the forwarded header."""
    peer_address = _address(peer or "")
    if peer_address is None:
        return peer
    if not forwarded_for or not any(peer_address in n for n in networks):
        return str(peer_address)
    believed = peer_address
    for entry in reversed(forwarded_for.split(",")):
        address = _address(entry)
        if address is None:
            # Nothing left of an entry nobody can parse can be believed, so
            # the last hop that could be is the answer.
            return str(believed)
        if not any(address in n for n in networks):
            return str(address)
        believed = address
    # Every hop is a trusted proxy: the client is one of them, the first.
    return str(believed)
