"""One spelling for a place a secret may go: `scheme://host:port` (D41).

    normalise("https://Faß.de/x", entry=False)  -> "https://xn--fa-hia.de:443"
    normalise("ha.lan")                         -> "https://ha.lan:443"

The gateway stores `allowed_hosts` in this form and the hub and tts-long
compare their targets in it. **One copy, because three drifted**: two
consumers encoded names with Python's IDNA 2003 codec while the store used
IDNA 2008, so an entry for one domain admitted another that anyone may
register.

Lower case, IDNA 2008 the way httpx encodes a name, no trailing dot, no
userinfo, the default port written out. An entry without a scheme means
https only, so a bearer goes over plain http only when an entry says so. An
entry is a host, never a path: `https://ha.lan/api` is refused rather than
read as narrower than it is. Every failure raises ValueError and names no
part of the input.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import idna

__all__ = ["DEFAULT_PORTS", "normalise"]

# mqtt and mqtts are here because the hub's broker password is a secret too,
# bound to its broker like any other (D41, D70).
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "mqtt": 1883, "mqtts": 8883}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Labels as DNS has them, plus `_`, which Docker container names use.
_HOST_LABELS = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
                          r"(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*$")
# A last label a resolver reads as a number: decimal, or hex after 0x.
_NUMERIC_LABEL = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")


def normalise(text: str, *, entry: bool = True) -> str:
    """`scheme://host:port` for an allowed_hosts entry, or for a target URL with `entry=False`.

    Raises ValueError.
    """
    raw = text.strip()
    if not raw or any(c.isspace() or c == "\\" for c in raw) or _CONTROL.search(raw):
        raise ValueError("not a host")
    if "://" not in raw:
        if not entry:
            raise ValueError("not an absolute URL")
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError("not a scheme a secret may go to")
    if entry and (parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("an allowed host has no path, query or fragment")
    host = (parts.hostname or "").rstrip(".")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("not a port") from None
    if port is None:
        port = DEFAULT_PORTS[scheme]
    if not host or not 0 < port < 65536:
        raise ValueError("not a host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = _host_name(host)
    else:
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{scheme}://{host}:{port}"


def _host_name(host: str) -> str:
    """A host name as the client that sends the value will connect to it. Raises ValueError.

    A non-ASCII name is encoded with IDNA 2008, the call httpx makes on the
    name urlsplit has already lowered. Python's "idna" codec is IDNA 2003: it
    folds `faß.de` into `fass.de` while httpx connects to `xn--fa-hia.de`. A
    name httpx refuses is refused here too.
    """
    if not host.isascii():
        try:
            host = idna.encode(host).decode("ascii")
        except UnicodeError:      # idna.IDNAError is one
            raise ValueError("not a host name") from None
    if not _HOST_LABELS.fullmatch(host):
        raise ValueError("not a host name")
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):
        # 010.0.0.1, 0x7f.0.0.1 and 127.1 are not addresses to ipaddress, but a
        # resolver reads them as octal, hex or short forms of one: the entry
        # would not say which machine it admits.
        raise ValueError("a numeric host that is not an IP address")
    return host
