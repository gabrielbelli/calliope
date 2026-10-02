"""The one place a credential is checked, on both listeners (D3, D14, D15, D60, D61).

    install_public(app)      :8080, sessions and API keys, CSRF, locked mode
    install_internal(app)    :8081, service keys and delegation only
    principal_of(request)    who is asking, for a route handler

**Pure ASGI and outermost**, for `http` and `websocket` alike: an
`@app.middleware("http")` never sees a socket, and a middleware added later
must not sit outside this one. Both guards wrap the finished middleware stack,
as voice_common.identity does for the backends.

**Order of the checks on :8080**, each one a refusal before any backend is
contacted:

1. a path with `.`/`..` segments, `//`, a backslash, `%2F` or `%5C`, or a
   decoded `?`, `#` or `%`, is a 400: what is matched is exactly what is
   forwarded, and `forwarded_path` writes it back for the backend (recheck M-1);
2. locked mode: 503 for everything but GET /health, GET /login, GET / and
   the device socket (D63);
3. the route's rule, found by the router's own match (routetable.resolve). A
   route that matched but carries no rule was added after routetable.bind(),
   and is a 500, never a route anyone may call;
4. the credential. A Bearer header wins over a cookie, and a Bearer that does
   not authenticate is a 401, never a fall-back to the cookie (D15). A
   service key is refused here: it works only on :8081 (D6). A cookie counts
   only on the public origin (D61);
5. CSRF for every unsafe request that is not Bearer, cookie or none at all
   (D14), and resource isolation for every cookie request (D15);
6. no credential: a page navigation goes to /login?next=, anything else 401;
7. session-only rows refuse a key with 403 session_required BEFORE the scope
   check, whatever the key holds (D60);
8. the scopes, then step-up.

The request then runs registered in the stream registry, so revoking the
credential ends it (D54).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose
from voice_common import errors, identity
from voice_common import scopes as scope_rules
from voice_common.errors import ApiError
from voice_common.scopes import KEY_ID

from . import apikeys, principals, proxyproto, routetable, runtime, sessions
from .audit import ANONYMOUS, Actor
from .clientaddr import client_ip, trusted
from .runtime import Runtime, normalise_origin
from .streams import SSE_CAP_SECONDS

log = logging.getLogger("voice-gateway.authn")

PRINCIPAL = "calliope_principal"
CLIENT_IP = "calliope_client_ip"

SAFE_METHODS = frozenset({"GET", "HEAD"})
DEVICE_SOCKETS = frozenset({"/satellites/ws", "/nodes/ws"})
# The one Origin a device sends: the Korvo's WebSockets library (links2004,
# 2.6.1) adds `Origin: file://` to every handshake unless told not to, and the
# firmware does not tell it (D53 says check, and drop the rule rather than
# change firmware). No web page can send it: a browser always writes its own
# origin, an http(s) one or "null".
DEVICE_ORIGINS = frozenset({"file://"})
# What a locked gateway still answers on :8080: liveness, and the two
# addresses that lead a person to the page that says why it is locked.
LOCK_EXEMPT = frozenset({"/health", "/login", "/"})
# The pages a top-level navigation may open from any site (D15). Each is the
# same static shell; a request that does something is never one of these.
PAGE_TABS = ("transcribe", "speak", "jobs", "vocabulary", "satellites", "account", "admin")
PAGE_PATH = re.compile(r"^/(login|ui(/(" + "|".join(PAGE_TABS) + r")(/.*)?)?)?$")
DELEGATION_ROUTE = ("POST", "/v1/audio/transcriptions")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Decoded into the path from `%3F`, `%23` and `%25`. Written back into a URL
# they end the path or start an escape, so a router and a backend would read
# two different paths; no route of this gateway has a use for them.
_URL_SYNTAX = re.compile(r"[?#%]")
# What forwarded_path leaves unescaped: RFC 3986's pchar, less `%`.
_PATH_SAFE = "/:@!$&'()*+,;="


@dataclass(frozen=True)
class Principal:
    """Who is asking, after authentication. `scopes` is already expanded."""

    kind: str                        # session | api_key | service | delegated
    sub: str                         # u_… or svc:<name>
    scopes: frozenset[str]
    user: Any = None                 # the users row, for a person
    session: sessions.Session | None = None
    key: Any = None                  # the api_keys row
    service: str | None = None       # the principal's name, or for a delegation the caller's
    delegated_cred: str | None = None
    # The registry ID of the session or key a delegation came from, so logout
    # or revoking that key also ends a long delegated transcription (D54).
    delegated_from: str | None = None

    @classmethod
    def for_session(cls, session: sessions.Session) -> Principal:
        return cls(kind="session", sub=session.user["id"], user=session.user,
                   session=session, scopes=scope_rules.session_scopes(session.user["role"]))

    @classmethod
    def for_key(cls, found: apikeys.KeyAuth) -> Principal:
        return cls(kind="api_key", sub=found.user["id"], user=found.user, key=found.row,
                   scopes=found.scopes)

    @classmethod
    def for_service(cls, name: str) -> Principal:
        return cls(kind="service", sub=scope_rules.principal(name), service=name,
                   scopes=scope_rules.SERVICE_PRINCIPALS[name])

    @property
    def restricted(self) -> bool:
        return self.session is not None and self.session.restricted

    @property
    def assertion_kind(self) -> str:
        return "service" if self.kind == "service" else "user"

    @property
    def assertion_cred(self) -> str:
        """`cred` in the identity assertion: session, a key ID, or the service itself."""
        if self.kind == "session":
            return "session"
        if self.kind == "api_key":
            return self.key["id"]
        if self.kind == "delegated":
            return self.delegated_cred if KEY_ID.fullmatch(self.delegated_cred or "") \
                else "session"
        return self.sub

    @property
    def delegation_cred(self) -> str:
        """`cred` in a delegation token: the session's public ref, or the key ID (D64)."""
        if self.kind == "session":
            return f"session:{self.session.ref}"
        if self.kind == "api_key":
            return self.key["id"]
        raise ValueError("only a person's session or key can delegate")

    @property
    def credential_id(self) -> str:
        """The key the stream registry and the 403 aggregation use."""
        if self.kind == "session":
            return f"session:{self.session.id_hash}"
        if self.kind == "api_key":
            return f"key:{self.key['id']}"
        if self.kind == "delegated" and self.delegated_from:
            return self.delegated_from
        return self.sub

    @property
    def actor(self) -> Actor:
        if self.kind == "service":
            return Actor("service", self.sub, self.sub)
        if self.kind == "api_key":
            return Actor("api_key", self.sub, f"key:{self.key['id']}")
        if self.kind == "delegated":
            return Actor("user", self.sub, f"delegated:{self.service}")
        return Actor("user", self.sub, "session")


def principal_of(request: Request) -> Principal:
    """The authenticated principal. Only public routes run without one."""
    found = request.scope.get("state", {}).get(PRINCIPAL)
    if not isinstance(found, Principal):
        raise errors.unauthenticated()
    return found


def optional_principal(request: Request) -> Principal | None:
    found = request.scope.get("state", {}).get(PRINCIPAL)
    return found if isinstance(found, Principal) else None


def client_ip_of(request: Request) -> str | None:
    return request.scope.get("state", {}).get(CLIENT_IP)


# ── request facts ─────────────────────────────────────────────────────────────


def odd_path(scope: Scope) -> bool:
    """A path the router and a backend might read differently (recheck M-1)."""
    raw = (scope.get("raw_path") or scope["path"].encode("latin-1", "replace")).lower()
    if b"%2f" in raw or b"%5c" in raw:
        return True
    path = scope["path"]
    if "\\" in path or "//" in path or _CONTROL.search(path) or _URL_SYNTAX.search(path):
        return True
    return any(segment in (".", "..") for segment in path.split("/"))


def forwarded_path(scope: Scope) -> str:
    """The path the guard authorised, escaped for the backend's URL (recheck M-1).

    Never `request.url.path`: Starlette rebuilds that by putting the decoded
    path into a URL string and parsing it again, so a decoded `?` or `#` ends
    it early and a decoded `%` becomes an escape the backend decodes a second
    time. odd_path refuses all three; escaping here as well means the backend
    decodes exactly the path whose rule was checked whatever odd_path misses.
    """
    return quote(scope["path"], safe=_PATH_SAFE)


def peer_of(scope: Scope) -> str | None:
    client = scope.get("client")
    return str(client[0]) if client else None


def joined(headers: Headers, name: str) -> str:
    """Every line of a list header, in order, as one value.

    `headers.get` returns the first line only, and a proxy that appends
    (HAProxy's `option forwardfor`) writes its line AFTER any the client sent:
    the first line is the client's own choice.
    """
    return ",".join(headers.getlist(name))


def resolve_client_ip(rt: Runtime, scope: Scope, headers: Headers) -> str | None:
    if rt.settings.proxy_protocol:
        return proxyproto.ADDRESSES.get(scope.get("client")) or peer_of(scope)
    return client_ip(peer=peer_of(scope), forwarded_for=joined(headers, "x-forwarded-for"),
                     networks=rt.settings.trusted_networks)


def request_origin(rt: Runtime, scope: Scope, headers: Headers) -> str | None:
    """The origin this request was made to, as the browser saw it.

    The scheme comes from X-Forwarded-Proto only when a trusted proxy sent
    it: HAProxy terminating TLS forwards plain HTTP.
    """
    scheme = {"ws": "http", "wss": "https"}.get(scope.get("scheme", "http"),
                                                scope.get("scheme", "http"))
    forwarded = joined(headers, "x-forwarded-proto")
    if (forwarded and not rt.settings.proxy_protocol
            and trusted(peer_of(scope), rt.settings.trusted_networks)):
        scheme = forwarded.split(",")[-1].strip().lower()
    host = headers.get("host")
    return normalise_origin(f"{scheme}://{host}") if host else None


def on_public_origin(rt: Runtime, scope: Scope, headers: Headers) -> bool:
    """Is this request for CALLIOPE_PUBLIC_ORIGIN? Sessions exist only there (D61)."""
    s = rt.settings
    if s.public_origin is None or request_origin(rt, scope, headers) != s.public_origin:
        return False
    if s.insecure_cookie or not s.public_origin.startswith("https://"):
        # Plain HTTP (the dev cookie, or the http:// origin a loopback
        # GATEWAY_BIND allows) is honoured only when the request reached a
        # loopback address: GATEWAY_BIND is a variable that need not match the
        # socket, and the Host header is the client's to choose (D16).
        server = scope.get("server")
        return bool(server) and runtime._loopback(str(server[0]))
    return True


def page_navigation(method: str, path: str, headers: Headers) -> bool:
    """A top-level navigation to one of the page's addresses (D15, D55).

    A request with no Fetch Metadata at all counts too, but only as a GET to
    a page path: every supported browser sends it, so this is curl or an old
    client, and the page shell is all such a request can ever receive.
    """
    if method not in SAFE_METHODS or not PAGE_PATH.match(path):
        return False
    mode, dest = headers.get("sec-fetch-mode"), headers.get("sec-fetch-dest")
    if mode is None and dest is None and headers.get("sec-fetch-site") is None:
        return True
    return mode == "navigate" and dest in (None, "document")


def csrf_ok(rt: Runtime, method: str, headers: Headers) -> bool:
    """D14: an unsafe request that is not Bearer must come from the page itself."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        if site == "same-origin":
            return True
        return site == "none" and headers.get("sec-fetch-mode") == "navigate"
    origin = headers.get("origin")
    return (origin is not None and rt.settings.public_origin is not None
            and normalise_origin(origin) == rt.settings.public_origin)


def isolated_ok(method: str, path: str, headers: Headers) -> bool:
    """D15: a cookie request is same-origin, or a top-level navigation to a page."""
    if headers.get("sec-fetch-site") == "same-origin":
        return True
    return page_navigation(method, path, headers)


def safe_next(value: str | None) -> str:
    """Where to go after login: a relative path, or /ui (D55)."""
    if not value:
        return "/ui"
    decoded = unquote(value)
    for text in (value, decoded):
        if (not text.startswith("/") or text.startswith("//") or "//" in text
                or "\\" in text or _CONTROL.search(text)):
            return "/ui"
    return value


def session_cookie(rt: Runtime, headers: Headers) -> str | None:
    raw = headers.get("cookie")
    return cookie_parser(raw).get(rt.settings.cookie_name) if raw else None


def cookie_header(rt: Runtime, value: str, *, max_age: int) -> str:
    """Set-Cookie for the session: __Host-, Secure, HttpOnly, Lax, Path=/ (D12)."""
    attributes = [f"{rt.settings.cookie_name}={value}", "Path=/", f"Max-Age={max_age}",
                  "HttpOnly", "SameSite=Lax"]
    if not rt.settings.insecure_cookie:
        attributes.insert(3, "Secure")
    return "; ".join(attributes)


# ── the guards ────────────────────────────────────────────────────────────────


def step_up_required() -> ApiError:
    return ApiError(403, "Enter your password again to do this.", code="step_up_required")


def _restricted() -> ApiError:
    return errors.unauthenticated("Choose a new password before doing anything else.")


def _unruled(route: Any) -> ApiError:
    """A route added after routetable.bind(): refused, because nothing says who may call it."""
    log.error("%s matched a request but has no rule: add it to the route table "
              "before routetable.bind() runs (D48)", getattr(route, "path", route))
    return ApiError(500, "This route has no access rule, so it is refused.",
                    type_="server_error", code="internal_error")


class _Guard:
    def __init__(self, app: FastAPI) -> None:
        self.routes = app
        self.app: ASGIApp | None = None

    async def _send_error(self, rt: Runtime, scope: Scope, receive: Receive, send: Send,
                          exc: ApiError, *, ip: str | None,
                          principal: Principal | None = None,
                          action: str | None = None) -> None:
        if exc.status == 401:
            rt.trail.refused(ip=ip, path=scope["path"], code=exc.code or "")
        elif exc.status == 403 or action:
            rt.trail.count("403", principal.credential_id if principal else (ip or "-"),
                           path=scope["path"], action=action or "request_refused", ip=ip,
                           actor=principal.actor if principal else ANONYMOUS,
                           detail={"status": exc.status, "code": exc.code})
        await errors.render(exc)(scope, receive, send)

    async def _run(self, rt: Runtime, principal: Principal | None, scope: Scope,
                   receive: Receive, send: Send) -> None:
        assert self.app is not None
        scope.setdefault("state", {})[PRINCIPAL] = principal
        if principal is None:
            await self.app(scope, receive, send)
            return

        loop = asyncio.get_running_loop()
        started = finished = False
        timer: asyncio.TimerHandle | None = None
        inner: asyncio.Future

        async def watched(message: dict) -> None:
            nonlocal started, finished, timer
            if message["type"] == "http.response.start":
                started = True
                if any(name.lower() == b"content-type"
                       and value.lower().startswith(b"text/event-stream")
                       for name, value in message.get("headers", ())):
                    timer = loop.call_later(SSE_CAP_SECONDS, inner.cancel)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                finished = True
            await send(message)

        inner = asyncio.ensure_future(self.app(scope, receive, watched))
        handle = rt.streams.open(principal.sub, principal.credential_id, inner)
        try:
            await asyncio.wait({inner})
        except asyncio.CancelledError:
            inner.cancel()
            raise
        finally:
            handle.release()
            if timer is not None:
                timer.cancel()
        if inner.cancelled():
            # Revoked, or past the event-stream cap: end what was started.
            if not started:
                await errors.render(errors.unauthenticated(
                    "This credential is no longer valid."))(scope, receive, send)
            elif not finished:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        inner.result()


class PublicGuard(_Guard):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert self.app is not None
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rt = runtime.get()
        headers = Headers(scope=scope)
        ip = resolve_client_ip(rt, scope, headers)
        scope.setdefault("state", {})[CLIENT_IP] = ip

        if scope["type"] == "websocket":
            await self._socket(rt, scope, receive, send, headers, ip)
            return
        if odd_path(scope):
            await errors.render(ApiError(400, "This path is not one this gateway "
                                              "routes.", code="invalid_path"))(
                scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        if method in SAFE_METHODS and path == "/health/":
            # Routed as /health: this check runs before routing, so the
            # router's redirect from /health/ never happens, and a probe
            # written with the slash would otherwise be a 401 for ever.
            scope = {**scope, "path": "/health", "raw_path": b"/health"}
            path = "/health"
        if method == "OPTIONS":
            # No CORS here, ever (D14): the Bearer skip of the CSRF check
            # depends on no browser being allowed to send one cross-site.
            await errors.render(ApiError(405, f"Not allowed: OPTIONS {path}.",
                                         code="method_not_supported"))(scope, receive, send)
            return

        route, rule, params = routetable.resolve(self.routes, scope)
        exempt = method in SAFE_METHODS and path in LOCK_EXEMPT
        if rt.lock.active and not exempt:
            await errors.render(errors.locked(*rt.lock.primary))(scope, receive, send)
            return

        if rule is not None and rule.public:
            principal = None
            if method not in SAFE_METHODS:
                if not csrf_ok(rt, method, headers):
                    await self._send_error(rt, scope, receive, send, errors.csrf(), ip=ip,
                                           action="csrf_refused")
                    return
            else:
                principal = self._optional(rt, scope, headers, method, path, ip)
            await self._run_public(principal, scope, receive, send)
            return

        principal = None
        authorization = headers.getlist("authorization")
        if authorization:
            found = self._bearer(rt, authorization, ip)
            if isinstance(found, ApiError):
                await self._send_error(rt, scope, receive, send, found, ip=ip)
                return
            principal = found
        else:
            # A request with no credential at all is refused below whatever
            # its origin, so the CSRF checks matter only once there is a
            # cookie; on the public rows (login) they ran above. Answering
            # 401 first also tells an API client that forgot its key the
            # truth, rather than "reload the page".
            session = self._session(rt, scope, headers)
            if session is not None:
                principal = Principal.for_session(session)
                if (method not in SAFE_METHODS and not csrf_ok(rt, method, headers)) \
                        or not isolated_ok(method, path, headers):
                    await self._send_error(rt, scope, receive, send, errors.csrf(), ip=ip,
                                           principal=principal, action="csrf_refused")
                    return

        navigation = page_navigation(method, path, headers)
        if principal is None:
            if navigation:
                await RedirectResponse(f"/login?next={quote(path)}", status_code=303)(
                    scope, receive, send)
                return
            await self._send_error(rt, scope, receive, send, errors.unauthenticated(), ip=ip)
            return
        if rule is None:
            if route is not None:
                await self._send_error(rt, scope, receive, send, _unruled(route), ip=ip)
                return
            # Nothing matched: the router answers 404 or 405.
            await self._run(rt, principal, scope, receive, send)
            return
        if principal.restricted and not rule.restricted:
            if navigation:
                await RedirectResponse("/login", status_code=303)(scope, receive, send)
                return
            await self._send_error(rt, scope, receive, send, _restricted(), ip=ip)
            return
        if rule.session_only and principal.kind != "session":
            await self._send_error(rt, scope, receive, send, errors.session_required(),
                                   ip=ip, principal=principal, action="session_required")
            return
        required, any_of = rule.required(params)
        missing = required - principal.scopes
        if missing or (any_of and not any_of & principal.scopes):
            if navigation:
                await RedirectResponse("/ui", status_code=303)(scope, receive, send)
                return
            await self._send_error(rt, scope, receive, send,
                                   errors.insufficient_scope(missing or any_of),
                                   ip=ip, principal=principal)
            return
        if rule.step_up and not principal.session.stepped_up:
            await self._send_error(rt, scope, receive, send, step_up_required(), ip=ip,
                                   principal=principal)
            return
        await self._run(rt, principal, scope, receive, send)

    async def _run_public(self, principal: Principal | None, scope: Scope,
                          receive: Receive, send: Send) -> None:
        assert self.app is not None
        scope.setdefault("state", {})[PRINCIPAL] = principal
        await self.app(scope, receive, send)

    def _bearer(self, rt: Runtime, values: list[str], ip: str | None) -> Principal | ApiError:
        if len(values) != 1:
            return errors.invalid_api_key()
        scheme, _, token = values[0].partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return errors.invalid_api_key()
        try:
            found = apikeys.authenticate(rt.db, token)
        except apikeys.Refused as refused:
            return errors.api_key_expired() if refused.reason == "expired" \
                else errors.invalid_api_key()
        apikeys.touch(rt.db, found.row, ip)
        return Principal.for_key(found)

    def _session(self, rt: Runtime, scope: Scope, headers: Headers) -> sessions.Session | None:
        value = session_cookie(rt, headers)
        if not value or not on_public_origin(rt, scope, headers):
            return None
        return sessions.lookup(rt.db, value)

    def _optional(self, rt: Runtime, scope: Scope, headers: Headers, method: str,
                  path: str, ip: str | None) -> Principal | None:
        """Who is asking a public GET (/health's tiers, / and /login), if anyone."""
        authorization = headers.getlist("authorization")
        if authorization:
            found = self._bearer(rt, authorization, ip)
            return found if isinstance(found, Principal) else None
        session = self._session(rt, scope, headers)
        # A must-change session sees only liveness, as an anonymous caller does.
        if session is None or session.restricted or not isolated_ok(method, path, headers):
            return None
        return Principal.for_session(session)

    async def _socket(self, rt: Runtime, scope: Scope, receive: Receive, send: Send,
                      headers: Headers, ip: str | None) -> None:
        assert self.app is not None
        if scope["path"] not in DEVICE_SOCKETS or odd_path(scope):
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        if "origin" in headers and headers["origin"] not in DEVICE_ORIGINS:
            # A browser always sends Origin, and never the device's. Refused
            # before accept, so the upgrade is a 403 and a page cannot open
            # the device door (D53, CSWSH).
            rt.trail.count("403", ip or "-", path=scope["path"], action="socket_refused",
                           ip=ip, detail={"code": "origin"})
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class InternalGuard(_Guard):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert self.app is not None
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        rt = runtime.get()
        headers = Headers(scope=scope)
        ip = peer_of(scope)
        scope.setdefault("state", {})[CLIENT_IP] = ip
        if odd_path(scope):
            await errors.render(ApiError(400, "This path is not one this gateway "
                                              "routes.", code="invalid_path"))(
                scope, receive, send)
            return

        values = headers.getlist("authorization")
        scheme, _, token = (values[0] if len(values) == 1 else "").partition(" ")
        name = principals.authenticate(rt.db, token.strip()) \
            if scheme.lower() == "bearer" else None
        if name is None:
            refusal = errors.invalid_api_key() if values else errors.unauthenticated(
                "The internal listener takes a service key as 'Authorization: Bearer'.")
            await self._send_error(rt, scope, receive, send, refusal, ip=ip)
            return
        principal = Principal.for_service(name)
        route, rule, params = routetable.resolve(self.routes, scope)

        delegation = headers.getlist(identity.DELEGATION_HEADER.lower())
        if delegation:
            key = (scope["method"], getattr(route, "path", None))
            if key != DELEGATION_ROUTE or "speech:delegate" not in principal.scopes:
                # Honoured on one row, from one scope; anyone else sending it
                # is a bug or an attempt, never a switch of identity (L3).
                rt.trail.record(action="delegation_refused", outcome="denied",
                                actor=principal.actor, ip=ip, target=scope["path"],
                                detail={"reason": "not_allowed"})
                await errors.render(ApiError(400, "X-Calliope-Delegation is not accepted "
                                                  "here.", code="delegation_not_allowed"))(
                    scope, receive, send)
                return
            delegated = self._delegate(rt, principal, delegation, ip)
            if isinstance(delegated, ApiError):
                await errors.render(delegated)(scope, receive, send)
                return
            principal = delegated

        if rule is None:
            if route is not None:
                await self._send_error(rt, scope, receive, send, _unruled(route), ip=ip)
                return
            await self._run(rt, principal, scope, receive, send)
            return
        if rule.session_only:
            await self._send_error(rt, scope, receive, send, errors.session_required(),
                                   ip=ip, principal=principal, action="session_required")
            return
        required, any_of = rule.required(params)
        missing = required - principal.scopes
        if missing or (any_of and not any_of & principal.scopes):
            await self._send_error(rt, scope, receive, send,
                                   errors.insufficient_scope(missing or any_of),
                                   ip=ip, principal=principal)
            return
        await self._run(rt, principal, scope, receive, send)

    def _delegate(self, rt: Runtime, caller: Principal, values: list[str],
                  ip: str | None) -> Principal | ApiError:
        """D64: signature, audience, use count, and the credential re-checked now."""
        reason: str | None = None
        claims = None
        if len(values) != 1:
            reason = "duplicate"
        else:
            try:
                claims = identity.verify_delegation(values[0], rt.keys.delegation_keys())
            except identity.InvalidAssertion as exc:
                reason = exc.reason
        user = None
        origin: str | None = None
        scopes: frozenset[str] = frozenset()
        if claims is not None:
            reason = rt.delegations.claim(claims)
        if claims is not None and reason is None:
            if claims.cred.startswith("session:"):
                found = sessions.by_ref(rt.db, claims.cred.removeprefix("session:"))
                if found is not None and not found.restricted:
                    user, origin = found.user, f"session:{found.id_hash}"
                    scopes = scope_rules.session_scopes(user["role"])
            else:
                key = apikeys.live(rt.db, claims.cred)
                if key is not None:
                    user, scopes, origin = key.user, key.scopes, f"key:{claims.cred}"
            if user is None or user["id"] != claims.sub:
                reason = "credential_revoked"
            elif "speech:transcribe" not in scopes:
                reason = "insufficient_scope"
        target = claims.sub if claims is not None else None
        if reason is not None:
            rt.trail.record(action="delegation_refused", outcome="denied",
                            actor=caller.actor, ip=ip, target=target,
                            detail={"reason": reason})
            return ApiError(403, "This delegation is not valid any more.",
                            code="delegation_refused")
        rt.trail.record(action="delegated_call", outcome="ok", actor=caller.actor, ip=ip,
                        target=target, detail={"cred": "session" if claims.cred.startswith(
                            "session:") else claims.cred})
        return Principal(kind="delegated", sub=claims.sub, user=user,
                         scopes=scopes & {"speech:transcribe"}, service=caller.service,
                         delegated_cred=claims.cred, delegated_from=origin)


def _install(app: FastAPI, guard: _Guard) -> None:
    if app.middleware_stack is not None:
        raise RuntimeError("install the guard before the app serves")
    build = app.build_middleware_stack

    def outermost() -> ASGIApp:
        guard.app = build()
        return guard

    app.build_middleware_stack = outermost  # type: ignore[method-assign]


def install_public(app: FastAPI) -> None:
    _install(app, PublicGuard(app))


def install_internal(app: FastAPI) -> None:
    _install(app, InternalGuard(app))


def no_store(response: Response) -> Response:
    """For anything carrying a secret once: a key, a temporary password (D27, L7)."""
    response.headers["Cache-Control"] = "no-store"
    return response
