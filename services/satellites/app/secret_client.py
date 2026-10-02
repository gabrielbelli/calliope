"""The secrets the hub sends, read from the gateway's store, and where each may go.

    secret_client.configure(Secrets(...))       a test's fake store; the default reads the gateway
    held = await secret_client.current().held("SATELLITES_HA_TOKEN")
    value = await secret_client.current().value_for("SATELLITES_HA_TOKEN", "https://ha.lan:8123")

ONE SOURCE OF TRUTH, ASKED BY NAME (D42). An action still names its secret
(token_env, api_key_env, url_secret) and never holds it. The value comes from
GET http://voice-gateway:8081/internal/secrets/{name}, asked with the hub's
service key, as {value, version, allowed_hosts, max_age}. Nothing is written
to disk: a value lives in this process's memory and nowhere else.

CACHED FOR A MINUTE, A MISS NEVER (D42). A value is kept for the answer's
max_age (60 s at most), so a change in Admin › Secrets reaches the hub within a
minute and a turn does not wait on the gateway. A 404 is not kept: a secret
someone has just stored is used at once, and a secret someone has just cleared
is dropped at once, its last value with it. Any other answer that is not a
value is a refusal and is treated the same way, said once at WARNING by name:
a 403 (the hub is not one of the secret's consumers), a 401 (the gateway does
not take this hub's key, even read again), a 500 or a body that is not a
secret. A definite answer from the gateway must not keep a secret alive.

STALE IF ERROR, AND ONLY THEN. While the gateway cannot be asked (unreachable,
service.key not written yet) or answers 503 (its keyring is unreadable, D63),
the last value it gave is used and a WARNING says so once, so a gateway in
trouble does not take the household's Home Assistant token from the
satellites. Pickers and Test ask afresh (invalidate), and so does a
destination whose provider answered 401 (again): the key may have been rotated
a moment ago.

BEFORE THE IMPORT, WHAT THE HUB HELD ITSELF (D45). From the hub's start until
the gateway answers its import (secret_import.py), the values the hub found in
its own configuration are kept here as `local`, each held to the hosts that
configuration named at start, and used when the store has none or cannot be
asked: a hub started beside a gateway that is not up yet keeps working. Every
one is dropped the moment the gateway answers the import, whatever it answers,
and once the import is done the hub never keeps them again.

A VALUE GOES ONLY WHERE ITS SECRET SAYS (D41). Every entry of allowed_hosts and
every target are reduced to scheme://host:port (IDNA, lower case, no trailing
dot, no userinfo, the scheme's default port written out) and compared exactly.
An entry without a scheme means https only, so a bearer goes over plain http
only when the entry says http. An empty list allows nothing. value_for raises
HostNotAllowed rather than return a value for a host the secret does not name;
the caller says so with the host and the secret's name, never the value.
Requests that carry a secret never follow a redirect: every client the hub
sends one with is built with follow_redirects=False.

NEVER A VALUE IN A LOG LINE. What is logged is a secret's name, a status code or
an exception's type.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx
from voice_common import identity
from voice_common.origins import normalise
from voice_common.scopes import SECRET_NAME

from . import gateway

log = logging.getLogger("voice-satellites.secrets")

PATH = "/internal/secrets/{name}"
MAX_AGE_S = 60.0
FETCH_TIMEOUT_S = 5.0
# A value no request header can carry is refused before it is sent: printable
# ASCII and no space. A key with a CR on the end (a .env saved on Windows) made
# h11 refuse the header in a sentence that quoted it whole.
SENDABLE = re.compile(r"[\x21-\x7e]{1,4096}")
KINDS = frozenset({"bearer", "password", "secret_url"})
# What a refusal from the gateway is said as, once per name.
REFUSALS = {
    401: "the gateway does not accept this hub's service key (401, even read again), so "
         "the hub does not use it",
    403: "the gateway refused it to this hub (403): the hub is not one of its consumers",
}
def origin(text: str | None, *, entry: bool = False) -> str | None:
    """`scheme://host:port` for a URL, or for an allowed_hosts entry, or None
    when it is not one. The gateway's store writes entries with this same
    function, so both sides spell a place one way (D41)."""
    if not isinstance(text, str):
        return None
    try:
        return normalise(text, entry=entry)
    except ValueError:
        return None


def origins(entries: Iterable[str]) -> frozenset[str]:
    """The entries of an allowed_hosts list that mean something, normalised."""
    return frozenset(o for o in (origin(e, entry=True) for e in entries) if o)


@dataclass(frozen=True)
class Held:
    """A secret as the store gave it, or as the hub held it before the import."""

    value: str
    hosts: frozenset[str]
    kind: str | None = None

    def allows(self, url: str) -> bool:
        target = origin(url)
        return target is not None and target in self.hosts


class HostNotAllowed(Exception):
    """A secret asked for on behalf of a host its allowed_hosts does not list."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.origin = origin(url) or "an address that is not a web address"
        super().__init__(f"{name} may not be sent to {self.origin}: that host is not one of "
                         "the secret's allowed hosts (Admin › Secrets)")


class Secrets:
    """The hub's view of the secret store. One per process (configure/current)."""

    def __init__(self, *, base_url: str = identity.GATEWAY_INTERNAL,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Held]] = {}
        self._good: dict[str, Held] = {}
        self._local: dict[str, Held] = {}
        # (name, why) already said at WARNING: once per name until the store
        # answers again, or every turn would say it.
        self._warned: set[tuple[str, str]] = set()

    def client(self) -> httpx.AsyncClient:
        """Made on first use, so it belongs to the event loop that uses it."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                             timeout=FETCH_TIMEOUT_S)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- what the hub held itself, until the store has it ------------------------

    def keep_local(self, values: dict[str, Held]) -> None:
        self._local = dict(values)

    def drop_local(self) -> None:
        self._local.clear()

    # -- asking the store ------------------------------------------------------

    async def _fetch(self, name: str) -> tuple[str, Held | None]:
        """("ok", held); ("missing", None) for a 404; ("error", None) when the
        gateway cannot be asked or answers 503, the one case a stale value
        may stand in; ("refused", None) for any other answer."""
        try:
            answer = await gateway.request(self.client(), "GET",
                                           self.base_url + PATH.format(name=name))
        except gateway.NotReady:
            return "error", None
        except httpx.HTTPError as e:
            log.debug("secret %s: the gateway was not reached (%s)", name, type(e).__name__)
            return "error", None
        status = answer.status_code
        if status == 503:
            log.debug("secret %s: the gateway answered 503", name)
            return "error", None
        if status == 404:
            return "missing", None
        if status != 200:
            self._warn(name, f"refused {status}", REFUSALS.get(
                status, f"the gateway answered {status}, so the hub does not use it"))
            return "refused", None
        try:
            body = answer.json()
            value, hosts, kind = body["value"], body.get("allowed_hosts") or [], body.get("kind")
            if not isinstance(value, str) or not isinstance(hosts, list):
                raise ValueError("shape")
            age = body.get("max_age", MAX_AGE_S)
            age = min(float(age), MAX_AGE_S) if isinstance(age, (int, float)) else MAX_AGE_S
        except (ValueError, KeyError, TypeError, AttributeError):
            self._warn(name, "shape", "the gateway's answer was not a secret, so the hub does "
                                      "not use it")
            return "refused", None
        held = Held(value, origins(h for h in hosts if isinstance(h, str)),
                    kind if kind in KINDS else None)
        self._cache[name] = (self._clock() + max(age, 0.0), held)
        return "ok", held

    async def held(self, name: str | None) -> Held | None:
        """The secret called `name`, or None when it has no value the hub may use."""
        if not name or not SECRET_NAME.fullmatch(name):
            return None
        hit = self._cache.get(name)
        if hit is not None and self._clock() < hit[0]:
            return hit[1]
        outcome, held = await self._fetch(name)
        if outcome == "ok":
            self._good[name] = held
            self._warned = {w for w in self._warned if w[0] != name}
            return held
        self._cache.pop(name, None)
        if outcome != "error":
            # Cleared means cleared, and refused means refused: the value it
            # had must not outlive the answer. The hub's own copy, which
            # exists only until the gateway has answered the import, may.
            self._good.pop(name, None)
            return self._local.get(name)
        stale = self._good.get(name)
        if stale is not None:
            self._warn(name, "stale", "the gateway did not answer, so the value it last gave is "
                                      "used until it does")
            return stale
        return self._local.get(name)

    async def stored(self, name: str) -> bool | None:
        """Does the store hold `name` for this hub? None when the gateway could
        not be asked. The import's confirmation (secret_import.py)."""
        self.invalidate(name)
        outcome, held = await self._fetch(name)
        if outcome == "ok":
            self._good[name] = held
            return True
        return None if outcome == "error" else False

    def _warn(self, name: str, why: str, sentence: str) -> None:
        if (name, why) not in self._warned:
            self._warned.add((name, why))
            log.warning("secret %s: %s", name, sentence)

    def invalidate(self, name: str | None) -> None:
        """The next held() asks the store: for a picker, a Test, or after a 401."""
        if name:
            self._cache.pop(name, None)

    async def value_for(self, name: str | None, url: str) -> str | None:
        """The value of `name` for a request to `url`, or None when it has
        none. HostNotAllowed when `url`'s host is not one the secret names."""
        held = await self.held(name)
        if held is None:
            return None
        if not held.allows(url):
            raise HostNotAllowed(name, url)
        return held.value

    async def again(self, name: str | None, used: str | None, url: str) -> str | None:
        """After a provider refused `used` (401): the value the store holds now,
        when it is another one, else None. The caller tries once more with it."""
        if not name or used is None:
            return None
        self.invalidate(name)
        fresh = await self.value_for(name, url)
        return fresh if fresh is not None and fresh != used else None

    async def status(self, names: Iterable[str]) -> dict[str, bool]:
        """Whether each name has a value the hub can use: names and booleans only."""
        return {name: await self.held(name) is not None for name in sorted(set(names))}

    def known(self, names: Iterable[str | None]) -> list[str]:
        """The values in memory for these names, to scrub from an error's text.
        Never sent anywhere and never logged."""
        found = []
        for name in names:
            for source in (self._cache.get(name or "", (0.0, None))[1], self._good.get(name or ""),
                           self._local.get(name or "")):
                if source is not None:
                    found.append(source.value)
        return found


_current = Secrets()


def configure(secrets: Secrets) -> Secrets:
    """The Secrets every lookup uses: a test's fake store, or the gateway's."""
    global _current
    _current = secrets
    return secrets


def current() -> Secrets:
    return _current
