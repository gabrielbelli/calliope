"""What a pasted URL has to survive before anything is done with it.

READ THIS BEFORE RELAXING ANYTHING BELOW.

This box hosts the owner's other services: the NAS's own pages, other apps on
high ports, and a handful of things bound to 127.0.0.1 that were never meant
to leave the machine. A route that takes a URL from a browser and makes an
outbound request with it is, unmodified, a way to ask this container to fetch
any of those and tell the caller what came back. That is the whole of SSRF,
and on a NAS the interesting targets are all on the near side of the firewall.

TWO LAYERS, AND THEY APPLY THE SAME RULES.

  1. This module, in the server. Stdlib only, no network, ~40 lines: scheme,
     userinfo, port, then getaddrinfo and a check of EVERY address the name
     resolves to. It runs on /ui/resolve and /ui/commit, before any child is
     spawned, so a URL it hates never reaches yt-dlp at all.
  2. The same rules at every connection, inside app/fetcher.py. The child
     replaces socket.getaddrinfo and socket.socket's connect, connect_ex and
     sendto before it imports yt_dlp, so every answer a name resolves to and
     every peer a socket is opened to goes through _forbidden() and the port
     rule below. That covers what layer 1 cannot see: redirects, URLs found
     inside a page, DASH fragments and DNS rebinding, for the probe and the
     download alike.

Layer 1 exists even though layer 2 is complete, because it answers in the
server with a message about the link, costs no process, and is what the rate
limit and the log see.

WHAT IS BLOCKED, AND WHY EACH ONE.

  scheme other than http/https   file:, gopher:, dict: and ftp: are all
                                 fetchable by some client in this chain and
                                 none of them is a media URL.
  userinfo (user:pass@host)      the classic parser split: several URL
                                 libraries disagree about where the host
                                 starts when an @ is present, and the whole
                                 attack is making two parsers disagree.
  ports other than 80 and 443    an internal service is almost never on 80 or
                                 443 on a home server; the apps beside this
                                 one listen on high ports. This single rule
                                 removes most of the LAN as a target.
  loopback         127/8, ::1    the container's own listeners
  private          10/8, 172.16/12, 192.168/16, fc00::/7   the LAN, the NAS,
                                 and every other container on it
  link-local       169.254/16, fe80::/10   which includes 169.254.169.254,
                                 the cloud metadata address -- not applicable
                                 on a NAS today, and the day this moves it is
                                 the first thing anyone tries
  CGNAT            100.64/10      carrier NAT, and Tailscale's range
  multicast, reserved, unspecified, and IPv4-mapped (::ffff:0:0/96) or
  NAT64-wrapped (64:ff9b::/96) IPv6 of any of the above

  by name          localhost, *.localhost, metadata.google.internal, and the
                   .local mDNS suffix -- belt and braces over the address
                   check, which already covers them, for the case where
                   resolution is the thing that is lying.

WHAT IS STILL OPEN, stated rather than hidden. A native network stack --
ffmpeg, aria2c, curl_cffi -- resolves and connects in C and never passes
through Python's socket module, so layer 2 cannot see it. The image carries
none of them, and its build fails if one arrives (services/ui/Containerfile).
The guard is not a sandbox either: code running inside the child can undo the
patch. The control against that is the deployment's egress rule, which is
written down in this service's README.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

__all__ = ["GuardError", "check"]

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
# The well-known NAT64 prefix (RFC 6052). On a network with NAT64,
# 64:ff9b::7f00:1 is 127.0.0.1 by another name, and Python calls the wrapper
# global.
NAT64 = ipaddress.ip_network("64:ff9b::/96")
BLOCKED_NAMES = ("localhost", "metadata.google.internal")
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


class GuardError(ValueError):
    """The URL is refused. The message is shown to the user verbatim."""


def _forbidden(address: str) -> str | None:
    """Why this literal address may not be fetched, or None if it may."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:  # pragma: no cover - getaddrinfo returns literals
        return "it did not resolve to an IP address"

    # An IPv4 address wrapped in IPv6 (::ffff:127.0.0.1) is the same host and
    # is_private on the wrapper is not the same question as is_private on the
    # address inside it. Unwrap first, then ask once.
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped  # type: ignore[assignment]
    # The same for NAT64: the last 32 bits are the IPv4 host it reaches.
    if ip.version == 6 and ip in NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)

    if ip.is_loopback:
        return f"{ip} is loopback"
    if ip.is_link_local:
        # Covers 169.254.169.254, the cloud metadata address.
        return f"{ip} is link-local"
    if ip.is_private:
        # ipaddress folds ULA (fc00::/7), 10/8, 172.16/12 and 192.168/16 into
        # this one predicate, and also 100.64/10 since Python 3.13.
        return f"{ip} is a private address"
    if ip.is_multicast:
        return f"{ip} is multicast"
    if ip.is_reserved or ip.is_unspecified:
        return f"{ip} is reserved"
    if not ip.is_global:
        # The catch-all, and the reason it is last rather than first: the
        # named predicates above produce a message that says WHICH rule fired,
        # which is what someone debugging a refused link needs. This one
        # closes what they miss -- 100.64/10 in particular, whose is_private
        # answer has changed between CPython releases, plus 192.0.0.0/24,
        # 198.18/15, the documentation ranges and 2001:db8::/32.
        return f"{ip} is not a globally routable address"
    return None


def check(url: str, *, resolve=None) -> str:
    """Return the URL if it may be handed onward, or raise GuardError.

    `resolve` is injectable so the tests can assert the address rules without
    depending on what DNS says today, and so a test can prove that a name
    resolving to a private address is refused without needing such a name.

    It defaults to None and is looked up below rather than being written as
    `resolve=socket.getaddrinfo`: a default argument is evaluated once, at
    definition, so the latter captures the original function and no later
    patch of socket.getaddrinfo — a test's, or a runtime's — is ever seen.
    """
    if resolve is None:
        resolve = socket.getaddrinfo
    url = (url or "").strip()
    if not url:
        raise GuardError("no URL given")
    if len(url) > 2048:
        # Not a security boundary, a sanity one: nothing legitimate is longer,
        # and the value ends up in a subprocess argument list.
        raise GuardError("that URL is implausibly long")

    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise GuardError(
            f"only http and https links can be fetched, not {parts.scheme or 'that'}:")
    if parts.username or parts.password:
        raise GuardError(
            "links carrying a username or password are refused: URL parsers "
            "disagree about where the host starts once an @ is present")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise GuardError("that URL has no host in it")
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
        raise GuardError(f"refusing to fetch the internal host {host!r}")

    try:
        port = parts.port
    except ValueError as exc:
        raise GuardError(f"that URL has an unusable port: {exc}") from None
    port = port if port is not None else (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise GuardError(
            f"only ports 80 and 443 are fetched, not {port}. Every service on "
            "this host that is not meant to be reachable from here is on some "
            "other port, and this rule is what keeps them that way.")

    # Resolved BEFORE anything fetches it, and every answer is checked rather
    # than the first: a name with one public A record and one 10.x A record is
    # a rebinding attack with the work already done for it.
    try:
        infos = resolve(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GuardError(f"{host} does not resolve ({exc.strerror or exc})") from None
    if not infos:
        raise GuardError(f"{host} does not resolve to any address")

    for info in infos:
        address = info[4][0]
        why = _forbidden(address)
        if why is not None:
            raise GuardError(f"refusing to fetch {host}: {why}")

    return url
