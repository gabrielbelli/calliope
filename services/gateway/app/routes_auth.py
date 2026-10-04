"""Signing in, the account's own sessions and keys (§3.1, D11-D30, D60, D61).

    GET  /login  /              the page, and where / sends you
    POST /auth/login            JSON only, public origin only, throttled
    GET  /auth/me               who you are and what you may do
    POST /auth/password         change it (no current password in a must-change session)
    POST /auth/logout  /auth/step-up
    GET|DELETE /auth/sessions[/{ref}]
    GET|POST /auth/keys, DELETE /auth/keys/{id}

The middleware has already decided who may reach each route (routetable);
these handlers enforce what depends on the request body: the password rules,
which scopes a new key may hold, and the step-up that some of them need.

**Login answers one sentence for every failure**, "Incorrect username or
password", and spends one Argon2 verify on every path, the unknown user's and
the bootstrap's included, so neither the words nor the time say whether an
account exists (D19, D21).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from voice_common import errors
from voice_common import scopes as scope_rules
from voice_common.errors import ApiError

from . import apikeys, passwords, runtime, sessions, users
from . import db as dbmod
from .audit import UNKNOWN_USERNAME, Actor
from .authn import (Principal, client_ip_of, cookie_header, no_store, on_public_origin,
                    optional_principal, principal_of, safe_next, session_cookie,
                    step_up_required)
from .db import iso
from .runtime import Runtime

router = APIRouter()

LOGIN_PAGE = Path(__file__).with_name("static") / "login.html"
KNOWN_IP_SECONDS = 30 * 24 * 3600
MAX_FIELD = 1024
# Every body these routes take is a handful of short fields.
MAX_BODY = 64 * 1024


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=MAX_FIELD)
    next: str | None = Field(default=None, max_length=2048)


class PasswordBody(_Body):
    current_password: str | None = Field(default=None, max_length=MAX_FIELD)
    new_password: str = Field(min_length=1, max_length=MAX_FIELD)
    next: str | None = Field(default=None, max_length=2048)


class StepUpBody(_Body):
    password: str = Field(min_length=1, max_length=MAX_FIELD)


class KeyBody(_Body):
    name: str = Field(min_length=1, max_length=apikeys.MAX_NAME)
    scopes: list[str] | None = Field(default=None, max_length=128)
    preset: str | None = Field(default=None, max_length=64)
    expires_days: int | None = scope_rules.DEFAULT_KEY_EXPIRY_DAYS


async def _bounded(request: Request) -> bytes:
    """The body, refused past MAX_BODY: POST /auth/login is public."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise ApiError(413, "This body is too large.", code="upload_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise ApiError(413, "This body is too large.", code="upload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def json_body(request: Request, model: type[_Body]) -> Any:
    """The body as `model`, or 415 when it is not JSON and a quiet 422 when it is wrong."""
    media = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json":
        raise errors.json_required()
    raw = await _bounded(request)
    try:
        return model.model_validate(json.loads(raw))
    except ValueError as exc:
        # ValidationError is a ValueError; so is a body that is not JSON.
        details = exc.errors() if isinstance(exc, ValidationError) else [
            {"type": "json_invalid", "loc": ("body",), "msg": "The body is not JSON."}]
        raise RequestValidationError(details) from None


def _incorrect() -> ApiError:
    return errors.unauthenticated("Incorrect username or password.")


def _too_many(wait: float) -> ApiError:
    seconds = max(1, int(wait + 0.999))
    return ApiError(429, f"Too many attempts. Try again in {seconds} s.",
                    code="rate_limited", headers={"Retry-After": str(seconds)})


def _known_ips(rt: Runtime, user: Any) -> set[str]:
    """Addresses exempt from the per-account ceiling: this account's recent sign-ins."""
    if user is None:
        return set()
    known = sessions.recent_ips(rt.db, user["id"])
    if user["last_login_ip"] and user["last_login_at"] and \
            user["last_login_at"] > iso(dbmod.now() - KNOWN_IP_SECONDS):
        known.add(user["last_login_ip"])
    return known


def _digest(text: str) -> bytes:
    return hashlib.sha256(passwords.normalise(text).encode("utf-8")).digest()


async def _password_matches(rt: Runtime, user: Any, password: str) -> bool:
    """One Argon2 verify whatever the account, so timing tells nothing (D19, D21)."""
    if user is not None and user["password_hash"] is None:
        value = rt.settings.admin_password
        armed = value is not None and user["role"] == "admin" and rt.bootstrap_armed()
        # Fixed-length digests, compared in constant time, then the same
        # verify every other login pays for.
        matched = armed and hmac.compare_digest(_digest(password), _digest(value or ""))
        await rt.hasher.dummy_verify(password)
        return bool(matched)
    if user is None:
        await rt.hasher.dummy_verify(password)
        return False
    return await rt.hasher.verify(password, user["password_hash"])


def _failed(rt: Runtime, request: Request, account: str, user: Any, ip: str | None,
            action: str) -> None:
    """Count a failure into the throttle and the audit's two tiers (§2.1, recheck M-3)."""
    started = rt.throttle.failed(account, ip or "-")
    # Never the name as typed: an attempted username is attacker input, and
    # one that matches nobody is recorded as <unknown> (§2.1).
    shown = user["username"] if user is not None else UNKNOWN_USERNAME
    first = rt.trail.count("login", f"{shown}|{ip}", path=request.url.path, action=action,
                           ip=ip, target=shown)
    # Only an existing account's failures reach the security tier: an
    # internet client can invent usernames faster than it can be written down.
    if user is not None and (first or started):
        rt.trail.record(action=action if not started else "login_throttled",
                        outcome="denied", ip=ip, target=user["id"],
                        detail={"username": shown})


def _session_response(rt: Runtime, body: dict[str, Any], session_id: str, *,
                      restricted: bool, status: int = 200) -> Response:
    response = JSONResponse(body, status_code=status)
    lifetime = sessions.RESTRICTED_SECONDS if restricted else sessions.ABSOLUTE_SECONDS
    response.headers.append("Set-Cookie", cookie_header(rt, session_id, max_age=lifetime))
    return no_store(response)


# ── the page and its two redirects ────────────────────────────────────────────


@cache
def _login_page() -> tuple[str, str]:
    """login.html and a CSP that allows its own inline script and style, by hash.

    Self-contained on purpose (recheck L8): with no asset of its own there is
    no static path to make public, and with hashes rather than
    'unsafe-inline' a script injected into the page would not run. The hashes
    are computed from the file as served, so they cannot go stale.
    """
    html = LOGIN_PAGE.read_text(encoding="utf-8")

    def hashes(tag: str) -> str:
        found = re.findall(rf"<{tag}>(.*?)</{tag}>", html, flags=re.S)
        return " ".join("'sha256-" + base64.b64encode(
            hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"
                        for text in found)

    policy = ("default-src 'none'; "
              f"script-src {hashes('script')}; style-src {hashes('style')}; "
              "connect-src 'self'; img-src 'self' data:; font-src data:; base-uri 'none'; "
              "form-action 'none'; frame-ancestors 'none'")
    return html, policy



@router.get("/login")
async def login_page(request: Request) -> Response:
    rt = runtime.get()
    origin = rt.settings.public_origin
    if origin is not None and not on_public_origin(rt, request.scope, request.headers):
        # Cookies are not isolated by port, so a session is only ever made
        # on the host name that serves nothing else (D16, D61).
        target = f"{origin}/login"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=303)
    html, policy = _login_page()
    return no_store(Response(html, media_type="text/html; charset=utf-8",
                             headers={"Content-Security-Policy": policy,
                                      "X-Content-Type-Options": "nosniff",
                                      "Referrer-Policy": "no-referrer"}))


@router.get("/")
async def root(request: Request) -> Response:
    principal = optional_principal(request)
    signed_in = principal is not None and principal.kind == "session" \
        and not principal.restricted
    return RedirectResponse("/ui" if signed_in else "/login", status_code=303)


# ── login ─────────────────────────────────────────────────────────────────────


@router.post("/auth/login")
async def login(request: Request) -> Response:
    rt = runtime.get()
    ip = client_ip_of(request)
    body: LoginBody = await json_body(request, LoginBody)
    if not on_public_origin(rt, request.scope, request.headers):
        rt.trail.count("403", ip or "-", path=request.url.path, action="wrong_host", ip=ip)
        raise errors.wrong_host()

    account = users.account_key(body.username)
    user = users.by_username(rt.db, body.username) if users.clean_username(body.username) \
        else None
    wait = rt.throttle.check(account, ip or "-", lambda: _known_ips(rt, user))
    if wait is not None:
        rt.trail.count("login", f"{account}|{ip}", path=request.url.path,
                       action="login_throttled", ip=ip)
        raise _too_many(wait)

    if not await _password_matches(rt, user, body.password) or not users.active(user):
        _failed(rt, request, account, user, ip, "login_failed")
        raise _incorrect()

    # Never upgrade a session ID the browser arrived with: a sibling on the
    # same host name may have planted it (D61).
    old = session_cookie(rt, request.headers)
    if old:
        sessions.revoke_hash_of(rt.db, old)
    restricted = bool(user["must_change"])
    session_id, _ = sessions.create(rt.db, user_id=user["id"], ip=ip,
                                    user_agent=request.headers.get("user-agent"),
                                    restricted=restricted)
    rt.throttle.succeeded(account, ip or "-")
    if user["password_hash"] and rt.hasher.needs_rehash(user["password_hash"]):
        users.rehash(rt.db, user["id"], await rt.hasher.hash(body.password))
    users.record_login(rt.db, user["id"], ip)
    rt.trail.record(action="login", outcome="ok", actor=Actor("user", user["id"], "session"),
                    ip=ip, detail={"restricted": restricted})
    return _session_response(rt, {"user": users.public(user), "must_change": restricted,
                                  "next": safe_next(body.next)}, session_id,
                             restricted=restricted)


# ── the signed-in account ─────────────────────────────────────────────────────


def _session_principal(request: Request) -> Principal:
    principal = principal_of(request)
    if principal.kind != "session":
        raise errors.session_required()
    return principal


@router.get("/auth/me")
async def me(request: Request) -> Response:
    principal = _session_principal(request)
    user = principal.user
    return no_store(JSONResponse({
        "user": users.public(user), "role": user["role"],
        "scopes": sorted(principal.scopes), "must_change": principal.restricted,
        "step_up_until": principal.session.stepup_until,
        "presets": list(scope_rules.presets_for(user["role"]))}))


@router.post("/auth/password")
async def change_password(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    user, ip = principal.user, client_ip_of(request)
    body: PasswordBody = await json_body(request, PasswordBody)
    account = users.account_key(user["username"])

    if not principal.restricted:
        # The current password is a password check like any other, and
        # shares the login throttle (D13, D19).
        wait = rt.throttle.check(account, ip or "-", lambda: _known_ips(rt, user))
        if wait is not None:
            raise _too_many(wait)
        if not body.current_password or not await rt.hasher.verify(
                body.current_password, user["password_hash"] or ""):
            _failed(rt, request, account, user, ip, "password_change_failed")
            raise ApiError(403, "The current password is not correct.",
                           code="wrong_password", param="current_password")
    bootstrap = user["password_hash"] is None and rt.bootstrap_armed()
    problem = passwords.problem(body.new_password, username=user["username"],
                                bootstrap=rt.settings.admin_password if bootstrap else None)
    if problem is not None:
        raise ApiError(400, passwords.PROBLEMS[problem], code="weak_password",
                       param="new_password")
    password_hash = await rt.hasher.hash(body.new_password)
    if not bootstrap:
        users.set_password(rt.db, user["id"], password_hash)
    elif not rt.consume_bootstrap(user["id"], password_hash):
        raise ApiError(409, "The first-access password was already replaced. Sign in "
                            "again.", code="bootstrap_consumed")
    # A new ID for this browser, and every other session of the account ends:
    # a password change is what someone does when they suspect one (D20).
    sessions.revoke_user(rt.db, user["id"])
    rt.streams.close(user=user["id"])
    session_id, _ = sessions.create(rt.db, user_id=user["id"], ip=ip,
                                    user_agent=request.headers.get("user-agent"),
                                    restricted=False)
    actor = Actor("user", user["id"], "session")
    if bootstrap:
        rt.trail.record(action="bootstrap_consumed", outcome="ok", actor=actor, ip=ip)
    rt.trail.record(action="password_changed", outcome="ok", actor=actor, ip=ip,
                    target=user["id"])
    return _session_response(rt, {"next": safe_next(body.next)}, session_id,
                             restricted=False)


@router.post("/auth/logout")
async def logout(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    sessions.revoke(rt.db, principal.session.id_hash)
    rt.streams.close(credential=principal.credential_id)
    rt.trail.record(action="logout", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request))
    response = no_store(Response(status_code=204))
    response.headers.append("Set-Cookie", cookie_header(rt, "", max_age=0))
    return response


@router.post("/auth/step-up")
async def step_up(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    user, ip = principal.user, client_ip_of(request)
    body: StepUpBody = await json_body(request, StepUpBody)
    account = users.account_key(user["username"])
    wait = rt.throttle.check(account, ip or "-", lambda: _known_ips(rt, user))
    if wait is not None:
        raise _too_many(wait)
    if not await rt.hasher.verify(body.password, user["password_hash"] or ""):
        _failed(rt, request, account, user, ip, "step_up_failed")
        rt.trail.record(action="step_up", outcome="denied", actor=principal.actor, ip=ip)
        raise ApiError(403, "That password is not correct.", code="wrong_password",
                       param="password")
    rt.throttle.succeeded(account, ip or "-")
    until = sessions.step_up(rt.db, principal.session.id_hash)
    rt.trail.record(action="step_up", outcome="ok", actor=principal.actor, ip=ip)
    return no_store(JSONResponse({"step_up_until": until}))


# ── sessions ──────────────────────────────────────────────────────────────────


@router.get("/auth/sessions")
async def list_sessions(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    rows = sessions.listing(rt.db, principal.sub)
    return no_store(JSONResponse({"sessions": [
        {"id": row["ref"], "created_at": row["created_at"],
         "last_seen_at": row["last_seen_at"], "ip": row["ip"],
         "user_agent": row["user_agent"],
         "current": row["id_hash"] == principal.session.id_hash} for row in rows]}))


@router.delete("/auth/sessions")
async def sign_out_others(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    refs = sessions.revoke_user(rt.db, principal.sub, keep=principal.session.id_hash)
    rt.streams.close(user=principal.sub, keep=principal.credential_id)
    rt.trail.record(action="sessions_revoked", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), detail={"count": len(refs)})
    return Response(status_code=204)


@router.delete("/auth/sessions/{ref}")
async def sign_out_one(request: Request, ref: str) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    row = rt.db.one("SELECT id_hash FROM sessions WHERE ref = ? AND user_id = ? "
                    "AND revoked_at IS NULL", (ref, principal.sub))
    if row is None:
        raise ApiError(404, "No such session.", code="not_found")
    sessions.revoke(rt.db, row["id_hash"])
    rt.streams.close(credential=f"session:{row['id_hash']}")
    rt.trail.record(action="session_revoked", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request))
    return Response(status_code=204)


# ── keys ──────────────────────────────────────────────────────────────────────


@router.get("/auth/keys")
async def list_keys(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    return no_store(JSONResponse({"keys": [apikeys.public(row) for row in
                                           apikeys.listing(rt.db, principal.sub)]}))


@router.post("/auth/keys")
async def create_key(request: Request) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    body: KeyBody = await json_body(request, KeyBody)
    role = principal.user["role"]
    if body.preset is not None and body.preset not in scope_rules.presets_for(role):
        raise ApiError(400, "That preset is not one your role can use.",
                       code="invalid_value", param="preset")
    requested = body.scopes if body.scopes is not None else (
        sorted(scope_rules.PRESETS[body.preset].scopes) if body.preset else [])
    if not requested:
        raise ApiError(400, "Choose at least one scope.", code="invalid_value",
                       param="scopes")
    refused = scope_rules.ungrantable(requested, role)
    if refused:
        # Session-only scopes, service scopes, scopes outside the role and
        # strings that are not scopes at all, all refused the same way (D60).
        raise errors.scope_not_grantable([s for s in requested if s in refused])
    if scope_rules.needs_step_up(requested) and not principal.session.stepped_up:
        raise step_up_required()
    if not scope_rules.expiry_allowed(requested, body.expires_days):
        cap = scope_rules.max_expiry_days(requested)
        raise ApiError(400, "Choose 30, 90 or 365 days, or never"
                       + (f"; these scopes allow at most {cap} days." if cap else "."),
                       code="invalid_value", param="expires_days")
    row, plaintext = apikeys.create(rt.db, user=principal.user, name=body.name.strip(),
                                    scopes=frozenset(requested), preset=body.preset,
                                    days=body.expires_days, created_by=principal.sub)
    rt.trail.record(action="key_created", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=row["id"],
                    detail={"preset": body.preset, "scopes": sorted(requested),
                            "expires_at": row["expires_at"]})
    return no_store(JSONResponse({"key": apikeys.public(row), "plaintext": plaintext},
                                 status_code=201))


@router.delete("/auth/keys/{key_id}")
async def revoke_key(request: Request, key_id: str) -> Response:
    rt = runtime.get()
    principal = _session_principal(request)
    row = apikeys.get(rt.db, key_id)
    if row is None or row["user_id"] != principal.sub:
        raise ApiError(404, "No such key.", code="not_found")
    apikeys.revoke(rt.db, key_id, by=principal.sub)
    rt.streams.close(credential=f"key:{key_id}")
    rt.trail.record(action="key_revoked", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=key_id)
    return Response(status_code=204)
