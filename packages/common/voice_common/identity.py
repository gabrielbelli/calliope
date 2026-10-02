"""Who is asking, as the gateway says it, and nothing a backend decides itself.

    X-Calliope-Identity: v1.<kid>.<b64url claims>.<b64url Ed25519 signature>

    identity.install(app, "stt")       every request needs a valid assertion
    identity.claims_of(request)        the verified Claims
    identity.has(claims, "jobs:read:all")

**The gateway is the only place a credential is checked** (D3). It turns a
session cookie or an API key into a 60-second assertion signed with Ed25519,
and a backend verifies it with the PUBLIC key only (D4). Neither voice-ui,
which runs yt-dlp on URLs users choose, nor the hub, which calls URLs an
admin configures, holds anything that can mint one.

**There is no `role` claim** (M1). A backend widens a listing to everyone only
when the scopes hold the `:all` form, so an admin's narrowed key (read-only,
home-assistant) is never widened by a backend testing `role == "admin"`. An
assertion that carries `role`, or any key not listed in `_IDENTITY_KEYS`, is
refused rather than tolerated.

**The headers never travel onward** (D65). install() deletes every
`X-Calliope-*` header from the request scope once it has read them, and only
the parsed claims (and, on the paths that delegate, the raw delegation token)
are left on `request.state`. A handler that proxies its request to MeTube, Home
Assistant or the GPU runner has nothing to forward by mistake; outbound
clients build their headers with `outbound_headers`, which takes explicit
values and no mapping.

**Deny by default.** Every path needs an assertion except GET and HEAD
`/health`, FastAPI's documentation routes are removed outright, and a
WebSocket gets no exemption at all. A refused request is logged as an aggregated `audit` line,
never with the header's value.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from . import audit, errors
from .auth import watch_removed_variables
from .health import IDENTITY_STATE, PATH as HEALTH_PATH
from .scopes import KEY_ID, SCOPE, SERVICE_SUB, USER_ID

__all__ = [
    "ASSERTION_HEADER", "DELEGATION_HEADER", "AUDIENCES", "DELEGATION_AUDIENCE",
    "ISSUER", "ASSERTION_LIFETIME", "DELEGATION_LIFETIME", "GATEWAY_INTERNAL",
    "RUN_DIR_ENV",
    "Claims", "InvalidAssertion", "Credentials", "Signer",
    "verify", "verify_delegation", "install", "claims_of", "delegation_of",
    "has", "require", "outbound_headers", "public_key_document",
    "write_public_keys",
]

log = logging.getLogger("voice_common.identity")

ASSERTION_HEADER = "X-Calliope-Identity"
DELEGATION_HEADER = "X-Calliope-Delegation"
VERSION = "v1"
ISSUER = "calliope-gateway"

# The backends an assertion can be addressed to (§3.8), and the one audience a
# delegation token carries: the gateway itself, which is never a backend, so a
# delegation token cannot be replayed as an identity assertion anywhere.
AUDIENCES = frozenset({"stt", "tts", "tts-long", "satellites", "ui"})
DELEGATION_AUDIENCE = "gateway"

ASSERTION_LIFETIME = 60          # D4
DELEGATION_LIFETIME = 30 * 60    # D64
# Every container reads the same host clock; this covers a request that was
# signed at the end of one second and checked at the start of the next.
LEEWAY = 5

# Far above a real assertion (about 1 KB with every scope an admin holds) and
# far below anything that costs a backend to look at.
MAX_TOKEN = 4096
MAX_SCOPES = 128

# The gateway's internal listener (D6): plain HTTP inside the compose network,
# never published, and the only place a backend's service key is ever sent.
# A constant, not a setting: a variable that named it could send the key
# anywhere.
GATEWAY_INTERNAL = "http://voice-gateway:8081"

RUN_DIR_ENV = "CALLIOPE_RUN_DIR"
DEFAULT_RUN_DIR = "/run/calliope"
PUBLIC_KEYS_FILE = "identity.pub"
SERVICE_KEY_FILE = "service.key"

# How often a backend looks at identity.pub when nothing forces it to. An
# unknown kid looks at once; this bounds how long a kid the gateway DROPPED
# (rotation, D69) keeps verifying here.
RECHECK_SECONDS = 30.0
# How often a credential file that is not there yet is looked for (§2.4).
POLL_SECONDS = 2.0

_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_KID = re.compile(r"^[0-9]{1,9}$")
_JTI = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SERVICE_KEY = re.compile(r"^calliope_svc_[0-9A-Za-z]{36}$")
# A delegation token must name the very session or key, because the gateway
# re-checks it live on every use (D64). `session:<ref>` is the gateway's own
# reference to a session row, never the session ID, which is a credential.
_SESSION_REF = re.compile(r"^session:[A-Za-z0-9_-]{16,64}$")
_SESSION = re.compile(r"^session$")

_IDENTITY_KEYS = frozenset({"iss", "aud", "sub", "kind", "scopes", "cred",
                            "iat", "exp", "jti"})
_DELEGATION_KEYS = frozenset({"iss", "aud", "sub", "kind", "cred", "iat", "exp",
                              "jti", "dlg"})

STATE_CLAIMS = "identity"
STATE_DELEGATION = "delegation"


class InvalidAssertion(Exception):
    """A token that does not verify. `reason` is a code for the audit line, never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Claims:
    """A verified assertion. No `role`: see the module docstring (M1).

    sub     `u_` + 16 base32 for a user, `svc:<name>` for a service
    kind    "user" or "service"
    scopes  the effective scopes, `:all` forms already expanded by the gateway
    cred    "session", an API key's ID, or for a service its own `svc:<name>`;
            in a delegation token, the session reference or key ID
    dlg     True only on a delegation token, which never reaches a backend
    """

    iss: str
    aud: str
    sub: str
    kind: str
    scopes: frozenset[str]
    cred: str
    iat: int
    exp: int
    jti: str
    dlg: bool = False

    def wire(self) -> dict[str, Any]:
        """The JSON object that is signed."""
        body: dict[str, Any] = {"iss": self.iss, "aud": self.aud, "sub": self.sub,
                                "kind": self.kind, "cred": self.cred,
                                "iat": self.iat, "exp": self.exp, "jti": self.jti}
        if self.dlg:
            body["dlg"] = True
        else:
            body["scopes"] = sorted(self.scopes)
        return body


# ── encoding ──────────────────────────────────────────────────────────────────


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not _B64.fullmatch(text):
        raise InvalidAssertion("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise InvalidAssertion("malformed") from None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Two `sub` keys are read as the last one by Python and as the first by
    # some other parser. Nobody signs that on purpose, so nobody verifies it.
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InvalidAssertion("bad_claims")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise InvalidAssertion("bad_claims")


def _integer(value: object) -> int:
    # bool is an int to Python and `true` is not a timestamp.
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidAssertion("bad_claims")
    return value


def _string(value: object, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or (pattern is not None and not pattern.fullmatch(value)):
        raise InvalidAssertion("bad_claims")
    return value


def _claims(body: object) -> Claims:
    """The claims, checked for shape and consistency. Signature and time are checked elsewhere."""
    if not isinstance(body, dict):
        raise InvalidAssertion("bad_claims")
    delegation = body.get("dlg") is True
    expected = _DELEGATION_KEYS if delegation else _IDENTITY_KEYS
    if set(body) != expected:
        raise InvalidAssertion("bad_claims")

    kind = _string(body["kind"])
    sub = _string(body["sub"])
    cred = _string(body["cred"])
    if kind == "user":
        session = _SESSION_REF if delegation else _SESSION
        if not USER_ID.fullmatch(sub) or not (session.fullmatch(cred)
                                              or KEY_ID.fullmatch(cred)):
            raise InvalidAssertion("bad_claims")
    elif kind == "service" and not delegation:
        # A service is its own credential, so `cred` cannot claim to be a
        # user's session while `sub` says it is a service.
        if not SERVICE_SUB.fullmatch(sub) or cred != sub:
            raise InvalidAssertion("bad_claims")
    else:
        raise InvalidAssertion("bad_claims")

    scopes: frozenset[str] = frozenset()
    if not delegation:
        raw = body["scopes"]
        if (not isinstance(raw, list) or len(raw) > MAX_SCOPES
                or not all(isinstance(s, str) and SCOPE.fullmatch(s) for s in raw)):
            raise InvalidAssertion("bad_claims")
        scopes = frozenset(raw)

    return Claims(iss=_string(body["iss"]), aud=_string(body["aud"]), sub=sub,
                  kind=kind, scopes=scopes, cred=cred, iat=_integer(body["iat"]),
                  exp=_integer(body["exp"]), jti=_string(body["jti"], _JTI),
                  dlg=delegation)


# ── verification ──────────────────────────────────────────────────────────────

KeyLookup = Callable[[str], Ed25519PublicKey | None]


def _lookup(keys: Mapping[str, Ed25519PublicKey] | Credentials) -> KeyLookup:
    return keys.public_key if isinstance(keys, Credentials) else keys.get


def _decode(token: str, keys: Mapping[str, Ed25519PublicKey] | Credentials) -> Claims:
    """Signature first, then the claims: nothing unauthenticated is ever parsed."""
    if not token or len(token) > MAX_TOKEN:
        raise InvalidAssertion("missing" if not token else "malformed")
    parts = token.split(".")
    if (len(parts) != 4 or parts[0] != VERSION or not _KID.fullmatch(parts[1])
            or not all(_B64.fullmatch(part) for part in parts[2:])):
        raise InvalidAssertion("malformed")
    _, kid, payload, signature = parts
    key = _lookup(keys)(kid)
    if key is None:
        raise InvalidAssertion("unknown_key")
    raw_signature = _b64decode(signature)
    try:
        key.verify(raw_signature, f"{VERSION}.{kid}.{payload}".encode("ascii"))
    except InvalidSignature:
        raise InvalidAssertion("bad_signature") from None
    try:
        body = json.loads(_b64decode(payload), object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidAssertion("bad_claims") from None
    return _claims(body)


def _check_time(claims: Claims, lifetime: int, now: float | None) -> None:
    at = time.time() if now is None else now
    if claims.exp <= claims.iat or claims.exp - claims.iat > lifetime:
        # A token signed to live longer than its kind may is refused even
        # though it verifies: the lifetime is part of the contract, not a hint.
        raise InvalidAssertion("lifetime")
    if claims.iat > at + LEEWAY:
        raise InvalidAssertion("not_yet_valid")
    if claims.exp + LEEWAY <= at:
        raise InvalidAssertion("expired")


def verify(header: str | None, audience: str,
           keys: Mapping[str, Ed25519PublicKey] | Credentials, *,
           now: float | None = None) -> Claims:
    """The claims of an identity assertion addressed to `audience`, or InvalidAssertion.

    `keys` is a kid → public key mapping, or a Credentials, which re-reads
    identity.pub when it meets a kid it does not know.
    """
    claims = _decode(header or "", keys)
    if claims.dlg:
        raise InvalidAssertion("delegation")
    if claims.iss != ISSUER:
        raise InvalidAssertion("wrong_issuer")
    if claims.aud != audience:
        raise InvalidAssertion("wrong_audience")
    _check_time(claims, ASSERTION_LIFETIME, now)
    return claims


def verify_delegation(token: str | None,
                      keys: Mapping[str, Ed25519PublicKey] | Credentials, *,
                      now: float | None = None) -> Claims:
    """The claims of a delegation token (D64), for the gateway's own check.

    This proves only that the gateway signed it and that it is current. The
    use count and the live check of `cred` are the gateway's: a token is never
    sufficient on its own.
    """
    claims = _decode(token or "", keys)
    if not claims.dlg:
        raise InvalidAssertion("not_delegation")
    if claims.iss != ISSUER:
        raise InvalidAssertion("wrong_issuer")
    if claims.aud != DELEGATION_AUDIENCE:
        raise InvalidAssertion("wrong_audience")
    _check_time(claims, DELEGATION_LIFETIME, now)
    return claims


# ── signing (the gateway, and tests that stand in for it) ─────────────────────


class Signer:
    """Mints assertions and delegation tokens with one Ed25519 key.

    The private key lives only on the gateway's own volume. This class is here
    rather than in the gateway so that the format is written and read in one
    file, and so a backend's tests can stand in for the gateway without a
    second copy of it.
    """

    def __init__(self, private_key: Ed25519PrivateKey, kid: str) -> None:
        if not _KID.fullmatch(kid):
            raise ValueError("kid is 1 to 9 decimal digits")
        self.private_key = private_key
        self.kid = kid

    @classmethod
    def generate(cls, kid: str = "1") -> Signer:
        return cls(Ed25519PrivateKey.generate(), kid)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def sign(self, claims: Claims) -> str:
        """The token for `claims`, checked against the same rules verify applies."""
        body = claims.wire()
        _claims(json.loads(json.dumps(body)))  # refuse to mint what nobody accepts
        payload = _b64encode(json.dumps(body, separators=(",", ":"),
                                        sort_keys=True).encode("utf-8"))
        signing_input = f"{VERSION}.{self.kid}.{payload}"
        signature = _b64encode(self.private_key.sign(signing_input.encode("ascii")))
        return f"{signing_input}.{signature}"

    def assertion(self, *, audience: str, sub: str, kind: str,
                  scopes: Iterable[str], cred: str, now: float | None = None,
                  lifetime: int = ASSERTION_LIFETIME, jti: str | None = None) -> str:
        """An identity assertion for one forwarded request."""
        if audience not in AUDIENCES:
            raise ValueError(f"{audience!r} is not a backend audience")
        iat = int(time.time() if now is None else now)
        return self.sign(Claims(iss=ISSUER, aud=audience, sub=sub, kind=kind,
                                scopes=frozenset(scopes), cred=cred, iat=iat,
                                exp=iat + lifetime,
                                jti=jti or secrets.token_urlsafe(16)))

    def delegation(self, *, sub: str, cred: str, now: float | None = None,
                   jti: str | None = None) -> str:
        """A delegation token for POST /ui/fetch (D64): audience gateway, 30 minutes."""
        iat = int(time.time() if now is None else now)
        return self.sign(Claims(iss=ISSUER, aud=DELEGATION_AUDIENCE, sub=sub,
                                kind="user", scopes=frozenset(), cred=cred,
                                iat=iat, exp=iat + DELEGATION_LIFETIME,
                                jti=jti or secrets.token_urlsafe(16), dlg=True))


def public_key_document(keys: Mapping[str, Ed25519PublicKey]) -> str:
    """identity.pub's contents: a JWK set (RFC 8037), current and previous kid."""
    entries = []
    for kid, key in sorted(keys.items(), key=lambda item: int(item[0])):
        if not _KID.fullmatch(kid):
            raise ValueError("kid is 1 to 9 decimal digits")
        raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        entries.append({"kty": "OKP", "crv": "Ed25519", "kid": kid,
                        "x": _b64encode(raw)})
    return json.dumps({"keys": entries}, indent=2) + "\n"


def write_public_keys(path: str | Path, keys: Mapping[str, Ed25519PublicKey]) -> None:
    """Write identity.pub atomically: a reader sees the old file or the new one, never half.

    A backend that read a torn file would keep its previous keys and look
    again, but a rename costs nothing and removes the question.
    """
    target = Path(path)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(public_key_document(keys))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parse_public_keys(text: str) -> dict[str, Ed25519PublicKey]:
    document = json.loads(text)
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("not a JWK set")
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in entries:
        if not isinstance(entry, dict) or (entry.get("kty"), entry.get("crv")) != ("OKP", "Ed25519"):
            raise ValueError("not an Ed25519 key")
        kid = entry["kid"]
        if not isinstance(kid, str) or not _KID.fullmatch(kid):
            raise ValueError("bad kid")
        raw = _b64decode(entry["x"])
        keys[kid] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


# ── the credential files on /run/calliope ─────────────────────────────────────


class Credentials:
    """identity.pub and service.key, read lazily and re-read when they change.

    The directory is CALLIOPE_RUN_DIR, or /run/calliope where compose mounts
    this service's `calliope-svc-<name>` volume read-only (D7). It is looked up
    on every access rather than fixed at construction, so a test can point an
    app that was built at import at a temporary directory.

    Nothing here waits or polls in the background. An unknown kid stats the
    file at once (a cheap call, and the parse runs only if the file changed);
    a known kid looks again every RECHECK_SECONDS so a dropped kid stops
    verifying; a file that is not there yet is looked for at most every
    POLL_SECONDS.
    """

    def __init__(self, directory: str | Path | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fixed = Path(directory) if directory is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded_from: Path | None = None
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._keys_stat: tuple[int, int, int] | None = None
        self._broken_stat: tuple[int, int, int] | None = None
        self._keys_checked = float("-inf")
        self._service_key: str | None = None
        self._service_key_tried = float("-inf")

    @property
    def directory(self) -> Path:
        return self._fixed or Path(os.environ.get(RUN_DIR_ENV) or DEFAULT_RUN_DIR)

    def _reset_if_moved(self) -> Path:
        directory = self.directory
        if directory != self._loaded_from:
            self._loaded_from = directory
            self._keys, self._keys_stat, self._broken_stat = {}, None, None
            self._keys_checked = float("-inf")
            self._service_key, self._service_key_tried = None, float("-inf")
        return directory

    def _refresh_keys(self, *, force: bool) -> None:
        with self._lock:
            directory = self._reset_if_moved()
            now = self._clock()
            interval = RECHECK_SECONDS if self._keys else POLL_SECONDS
            if not force and now - self._keys_checked < interval:
                return
            self._keys_checked = now
            path = directory / PUBLIC_KEYS_FILE
            try:
                info = path.stat()
            except OSError:
                # Gone is gone: a key the gateway can no longer vouch for
                # must not keep verifying from a cache.
                self._keys, self._keys_stat = {}, None
                return
            stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
            # A file already parsed, or already found broken, is not read
            # again: otherwise every made-up kid would cost a parse and a
            # broken file would cost a WARNING per request.
            if stamp in (self._keys_stat, self._broken_stat):
                return
            try:
                self._keys = _parse_public_keys(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError, InvalidAssertion):
                self._broken_stat = stamp
                log.warning("%s could not be read; keeping the keys already "
                            "loaded until it changes", path)
                return
            self._keys_stat = stamp

    def public_key(self, kid: str) -> Ed25519PublicKey | None:
        self._refresh_keys(force=False)
        if kid not in self._keys:
            self._refresh_keys(force=True)
        return self._keys.get(kid)

    def public_keys(self) -> dict[str, Ed25519PublicKey]:
        self._refresh_keys(force=False)
        return dict(self._keys)

    def _read_service_key(self, directory: Path) -> str | None:
        try:
            value = (directory / SERVICE_KEY_FILE).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not _SERVICE_KEY.fullmatch(value):
            log.warning("%s does not hold a service key", directory / SERVICE_KEY_FILE)
            return None
        return value

    def service_key(self) -> str | None:
        """This service's key for the gateway's internal listener, or None until it exists."""
        with self._lock:
            directory = self._reset_if_moved()
            if self._service_key is None:
                now = self._clock()
                if now - self._service_key_tried >= POLL_SECONDS:
                    self._service_key_tried = now
                    self._service_key = self._read_service_key(directory)
            return self._service_key

    def reload_service_key(self) -> str | None:
        """Read service.key again now: the gateway answered 401, so it may have been rotated."""
        with self._lock:
            directory = self._reset_if_moved()
            self._service_key_tried = self._clock()
            self._service_key = self._read_service_key(directory)
            return self._service_key

    @property
    def ready(self) -> bool:
        """Both files are here and readable, so this service can verify and be verified."""
        return bool(self.public_keys()) and self.service_key() is not None


# ── the middleware ────────────────────────────────────────────────────────────

# The two headers this module reads, and the prefix it strips. Every
# X-Calliope-* header is removed, not only these two: none is meant for an
# application, so none should reach one.
_PREFIX = b"x-calliope-"
_ASSERTION = ASSERTION_HEADER.lower().encode("latin-1")
_DELEGATION = DELEGATION_HEADER.lower().encode("latin-1")

AGGREGATE_SECONDS = 60
MAX_FAILURE_KEYS = 256
MAX_PATHS = 5
MAX_PATH_LENGTH = 200


# The two spellings of the one open path. The check runs before routing, so
# FastAPI's redirect from /health/ never happens: matched as an exact string,
# the slash was a 401 and a probe written that way went permanently unhealthy.
# Exactly one slash, and only these two: stripping every trailing slash let
# /health// through, which a catch-all route would then have served.
_HEALTH_SPELLINGS = frozenset({HEALTH_PATH, HEALTH_PATH + "/"})


class _Failures:
    """Refused assertions, counted per (reason, peer) per minute, one audit line each.

    Aggregated because the internet can reach the gateway and the gateway can
    reach this service: one line per refusal would let anyone choose how big
    this container's log gets. Bounded for the same reason (recheck M-4).
    """

    def __init__(self, audience: str, *, clock: Callable[[], float] = time.time) -> None:
        self.audience = audience
        self.clock = clock
        self.windows: dict[tuple[str, str], dict[str, Any]] = {}
        self.scheduled_on: asyncio.AbstractEventLoop | None = None

    def record(self, reason: str, ip: str, path: str) -> None:
        now = self.clock()
        window = int(now // AGGREGATE_SECONDS)
        if any(entry["window"] != window for entry in self.windows.values()):
            self.flush(before=window)
        key = (reason, ip)
        if key not in self.windows and len(self.windows) >= MAX_FAILURE_KEYS:
            key = (reason, "*")
        entry = self.windows.setdefault(key, {"window": window, "count": 0,
                                              "first": now, "last": now,
                                              "paths": []})
        entry["count"] += 1
        entry["last"] = now
        trimmed = path[:MAX_PATH_LENGTH]
        if trimmed not in entry["paths"] and len(entry["paths"]) < MAX_PATHS:
            entry["paths"].append(trimmed)
        self._schedule(now)

    def _schedule(self, now: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # A flush already waits on this loop. One that waited on a loop since
        # closed never ran, so it does not count.
        if self.scheduled_on is loop:
            return
        self.scheduled_on = loop
        delay = AGGREGATE_SECONDS - (now % AGGREGATE_SECONDS) + 0.01

        def due() -> None:
            self.scheduled_on = None
            self.flush()

        loop.call_later(delay, due)

    def flush(self, before: int | None = None) -> None:
        """Emit every window that has closed (or every window, when `before` is None)."""
        for key in [k for k, entry in self.windows.items()
                    if before is None or entry["window"] < before]:
            entry = self.windows.pop(key)
            reason, ip = key
            audit.emit({
                "ts": audit.timestamp(entry["first"]),
                "actor_kind": "anonymous", "ip": None if ip == "*" else ip,
                "action": "assertion_rejected", "target": self.audience,
                "outcome": "denied", "aggregated": 1,
                "detail": {"reason": reason, "count": entry["count"],
                           "first": audit.timestamp(entry["first"]),
                           "last": audit.timestamp(entry["last"]),
                           "paths": entry["paths"]},
            })


class _Guard:
    """Pure ASGI, outermost, for `http` and `websocket` alike."""

    def __init__(self, *, audience: str, credentials: Credentials,
                 delegation_paths: frozenset[str]) -> None:
        self.app: ASGIApp | None = None
        self.audience = audience
        self.credentials = credentials
        self.delegation_paths = delegation_paths
        self.failures = _Failures(audience)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert self.app is not None
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        assertions: list[bytes] = []
        delegations: list[bytes] = []
        kept = []
        for name, value in scope["headers"]:
            lowered = name.lower()
            if lowered.startswith(_PREFIX):
                if lowered == _ASSERTION:
                    assertions.append(value)
                elif lowered == _DELEGATION:
                    delegations.append(value)
                continue
            kept.append((name, value))
        scope = {**scope, "headers": kept}

        if (scope["type"] == "http" and scope["method"] in ("GET", "HEAD")
                and scope["path"] in _HEALTH_SPELLINGS):
            # Routed as /health whichever spelling arrived, so the exemption
            # reaches the health route and nothing else: a catch-all of the
            # service's own would otherwise answer /health/ with no assertion.
            health = {**scope, "path": HEALTH_PATH,
                      "raw_path": HEALTH_PATH.encode("ascii")}
            await self.app(health, receive, send)
            return

        try:
            if len(assertions) != 1:
                raise InvalidAssertion("missing" if not assertions else "duplicate")
            claims = verify(assertions[0].decode("latin-1"), self.audience,
                            self.credentials)
        except InvalidAssertion as exc:
            peer = (scope.get("client") or ("unknown",))[0]
            self.failures.record(exc.reason, str(peer), scope["path"])
            await self._refuse(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state[STATE_CLAIMS] = claims
        if scope["path"] in self.delegation_paths:
            state[STATE_DELEGATION] = self._delegation(delegations, claims)
        await self.app(scope, receive, send)

    def _delegation(self, values: list[bytes], claims: Claims) -> str | None:
        """The delegation token, only if the gateway signed it for THIS request's user.

        The gateway checks it again, live, when voice-ui uses it (D64). Checked
        here too so a handler is never handed a token it would only be passing
        on for somebody else.
        """
        if len(values) != 1:
            return None
        token = values[0].decode("latin-1")
        try:
            delegated = verify_delegation(token, self.credentials)
        except InvalidAssertion:
            return None
        return token if delegated.sub == claims.sub else None

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Closed before accept, which the server turns into an HTTP 403
            # on the upgrade: the socket never opens.
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        refusal = errors.unauthenticated(
            "No valid identity assertion. This service answers only requests "
            "the Calliope gateway forwards.")
        await errors.render(refusal)(scope, receive, send)


def _switch_off_docs(app: FastAPI) -> None:
    # FastAPI adds these routes in its constructor, so setting the URLs to None
    # now is not enough on its own: the routes already exist. A schema is a
    # free map of the service, and with a valid assertion the middleware
    # would otherwise serve it.
    doc_paths = {app.openapi_url, app.docs_url, app.redoc_url,
                 app.swagger_ui_oauth2_redirect_url if app.docs_url else None}
    doc_paths.discard(None)
    app.router.routes[:] = [route for route in app.router.routes
                            if isinstance(route, APIRoute)
                            or getattr(route, "path", None) not in doc_paths]
    app.openapi_url = app.docs_url = app.redoc_url = None


def install(app: FastAPI, audience: str, *, credentials: Credentials | None = None,
            delegation_paths: Iterable[str] = ()) -> None:
    """Require a valid assertion for `audience` on every request but GET and HEAD /health.

    Call once, at import, before the app serves. The middleware is placed
    OUTERMOST whatever else is added before or after this call, so no other
    middleware ever sees the assertion headers (D65).

    `delegation_paths` are the exact paths where the raw delegation token is
    left on request.state for the handler to pass on: voice-ui's /ui/fetch and
    nothing else (§3.7).

    Also reports any removed variable of the old key scheme, by name, every
    minute (voice_common.auth). It never stops the service.
    """
    if audience not in AUDIENCES:
        raise ValueError(f"{audience!r} is not one of {sorted(AUDIENCES)}")
    if app.middleware_stack is not None:
        raise RuntimeError("identity.install must run before the app serves")
    if getattr(app.state, IDENTITY_STATE, None) is not None:
        raise RuntimeError("identity.install was already called on this app")

    _switch_off_docs(app)
    guard = _Guard(audience=audience, credentials=credentials or Credentials(),
                   delegation_paths=frozenset(delegation_paths))
    setattr(app.state, IDENTITY_STATE, guard)

    build = app.build_middleware_stack

    def outermost() -> ASGIApp:
        guard.app = build()
        return guard

    # Not add_middleware: that puts the guard wherever this call happened to
    # fall in the module, and a middleware added afterwards would sit outside
    # it and see the headers. Wrapping the finished stack cannot be outflanked.
    app.build_middleware_stack = outermost  # type: ignore[method-assign]
    watch_removed_variables()


# ── what a handler uses ───────────────────────────────────────────────────────


def claims_of(request: Request) -> Claims:
    """The verified claims of this request. 401 if there are none (only /health has none)."""
    claims = getattr(request.state, STATE_CLAIMS, None)
    if not isinstance(claims, Claims):
        raise errors.unauthenticated()
    return claims


def delegation_of(request: Request) -> str | None:
    """The raw delegation token on a delegating path, to pass on and never to log."""
    return getattr(request.state, STATE_DELEGATION, None)


def has(claims: Claims, scope: str) -> bool:
    """Do these claims hold `scope`? `X:all` satisfies a check for `X:own`."""
    if scope in claims.scopes:
        return True
    return scope.endswith(":own") and scope[:-len("own")] + "all" in claims.scopes


def require(request: Request, scope: str) -> Claims:
    """The claims, or 403 insufficient_scope. For the few checks a backend keeps (runs:write)."""
    claims = claims_of(request)
    if not has(claims, scope):
        raise errors.insufficient_scope([scope])
    return claims


_NEVER_SENT = frozenset({ASSERTION_HEADER.lower(), "cookie"})


def outbound_headers(**explicit: str | None) -> dict[str, str]:
    """Headers for an outbound request, built from named values only (D65).

        outbound_headers(authorization=f"Bearer {key}", content_type="application/json")
        -> {"Authorization": "Bearer …", "Content-Type": "application/json"}

    Python names only, so `**request.headers` cannot pass an inbound request
    through: its `user-agent`, `content-type` and the rest are refused. A None
    value is left out. The identity assertion and cookies are refused by
    name: only the gateway mints the one, and no backend has a reason to send
    the other onward. A value with CR or LF is refused, so a value cannot
    become a second header.

    For the backends' clients (MeTube, Home Assistant, LLMs, webhooks, the
    GPU runner, the gateway's internal listener). The gateway's own forwarder
    sets the assertion itself and does not use this.
    """
    headers: dict[str, str] = {}
    for name, value in explicit.items():
        if not name.isidentifier():
            raise ValueError(f"{name!r} is not a header written as a Python name")
        if value is None:
            continue
        header = "-".join(part.capitalize() for part in name.split("_"))
        if header.lower() in _NEVER_SENT:
            raise ValueError(f"{header} is never sent onward")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError(f"{header} must be a single-line string")
        headers[header] = value
    return headers
