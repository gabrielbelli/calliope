"""Admin › Users, Roles, Keys and Audit (§3.2, D20, D25, D30, D68).

Every route here is session-only except GET /admin/audit, which a `monitor`
key may read, and every change to a user needs a step-up: the route table says
so and the middleware enforces it before these handlers run.

**A temporary password is shown once** (D25), with `Cache-Control: no-store`
(recheck L7), and the account must choose its own at first sign-in, from a
restricted session that cannot create keys (recheck M-7).

**Resets end the account's sessions** (D20): a reset is what an admin does
when an account is suspected. "Also revoke this user's API keys" defaults to
yes.
"""

from __future__ import annotations

import json
import secrets
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from voice_common import scopes as scope_rules
from voice_common.errors import ApiError

from . import apikeys, runtime, sessions, users
from .authn import client_ip_of, no_store, principal_of
from .routes_auth import _Body, json_body

router = APIRouter()

AUDIT_PAGE = 100
AUDIT_PAGE_MAX = 500


class NewUser(_Body):
    username: str = Field(min_length=1, max_length=64)
    role: Literal["admin", "speech"]
    display_name: str | None = Field(default=None, max_length=users.MAX_DISPLAY_NAME)


class UserChange(_Body):
    role: Literal["admin", "speech"] | None = None
    disabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=users.MAX_DISPLAY_NAME)


class Reset(_Body):
    revoke_keys: bool = True


def _refused(exc: users.Refused) -> ApiError:
    return ApiError(409, exc.message, code=exc.code)


def _not_found(what: str) -> ApiError:
    return ApiError(404, f"No such {what}.", code="not_found")


def _temporary_password() -> str:
    # 24 characters of token_urlsafe: past every rule in D18 by construction.
    return secrets.token_urlsafe(18)


# ── users ─────────────────────────────────────────────────────────────────────


@router.get("/admin/users")
async def list_users(request: Request) -> Response:
    rt = runtime.get()
    return no_store(JSONResponse({"users": [users.public(row) for row in
                                            users.listing(rt.db)]}))


@router.post("/admin/users")
async def create_user(request: Request) -> Response:
    rt = runtime.get()
    principal = principal_of(request)
    body: NewUser = await json_body(request, NewUser)
    temporary = _temporary_password()
    try:
        row = users.create(rt.db, username=body.username, role=body.role,
                           password_hash=await rt.hasher.hash(temporary),
                           must_change=True, created_by=principal.sub,
                           display_name=body.display_name)
    except users.Refused as exc:
        raise _refused(exc) from None
    rt.trail.record(action="user_created", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=row["id"],
                    detail={"username": row["username"], "role": row["role"]})
    return no_store(JSONResponse({"user": users.public(row),
                                  "temporary_password": temporary}, status_code=201))


@router.patch("/admin/users/{user_id}")
async def change_user(request: Request, user_id: str) -> Response:
    rt = runtime.get()
    principal = principal_of(request)
    body: UserChange = await json_body(request, UserChange)
    before = users.get(rt.db, user_id)
    try:
        row = users.update(rt.db, actor_id=principal.sub, user_id=user_id, role=body.role,
                           disabled=body.disabled, display_name=body.display_name)
    except LookupError:
        raise _not_found("user") from None
    except users.Refused as exc:
        raise _refused(exc) from None
    changed = {field: getattr(body, field) for field in ("role", "disabled", "display_name")
               if getattr(body, field) is not None}
    just_disabled = before is not None and row["disabled_at"] and not before["disabled_at"]
    revoked: dict[str, int] = {}
    if just_disabled:
        # Disabling is the incident response: enabling again must not quietly
        # bring back a stolen cookie or key, so both die here as on delete.
        revoked = {"sessions_revoked": len(sessions.revoke_user(rt.db, user_id)),
                   "keys_revoked": len(apikeys.revoke_user(rt.db, user_id,
                                                           by=principal.sub))}
    if just_disabled or (before is not None and row["role"] != before["role"]):
        # What a stream may see was decided when it opened (D54).
        rt.streams.close(user=user_id)
    rt.trail.record(action="user_changed", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=user_id,
                    detail={"fields": sorted(changed),
                            **({"role": body.role} if body.role else {}),
                            **({"disabled": body.disabled}
                               if body.disabled is not None else {}),
                            **revoked})
    return JSONResponse({"user": users.public(row)})


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(request: Request, user_id: str) -> Response:
    rt = runtime.get()
    principal = principal_of(request)
    body: Reset = await json_body(request, Reset)
    row = users.get(rt.db, user_id)
    if row is None or row["deleted_at"] is not None:
        raise _not_found("user")
    temporary = _temporary_password()
    users.set_password(rt.db, user_id, await rt.hasher.hash(temporary), must_change=True)
    revoked = sessions.revoke_user(rt.db, user_id)
    keys = apikeys.revoke_user(rt.db, user_id, by=principal.sub) if body.revoke_keys else []
    rt.streams.close(user=user_id)
    rt.trail.record(action="password_reset", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=user_id,
                    detail={"sessions_revoked": len(revoked), "keys_revoked": len(keys)})
    return no_store(JSONResponse({"user": users.public(users.get(rt.db, user_id)),
                                  "temporary_password": temporary,
                                  "keys_revoked": keys}))


@router.delete("/admin/users/{user_id}")
async def delete_user(request: Request, user_id: str) -> Response:
    rt = runtime.get()
    principal = principal_of(request)
    try:
        users.soft_delete(rt.db, actor_id=principal.sub, user_id=user_id)
    except LookupError:
        raise _not_found("user") from None
    except users.Refused as exc:
        raise _refused(exc) from None
    sessions.revoke_user(rt.db, user_id)
    apikeys.revoke_user(rt.db, user_id, by=principal.sub)
    rt.streams.close(user=user_id)
    rt.trail.record(action="user_deleted", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=user_id)
    return Response(status_code=204)


# ── roles ─────────────────────────────────────────────────────────────────────


@router.get("/admin/roles")
async def roles(request: Request) -> Response:
    """The code constants, read-only (D2)."""
    return JSONResponse({
        "scopes": dict(scope_rules.SCOPES),
        "session_only": sorted(scope_rules.SESSION_ONLY),
        "service_only": sorted(scope_rules.SERVICE_ONLY),
        "roles": {name: sorted(scope_rules.session_scopes(name))
                  for name in scope_rules.ROLES},
        "presets": {name: {"scopes": sorted(preset.scopes),
                           "description": preset.description,
                           "step_up": scope_rules.needs_step_up(preset.scopes),
                           "max_expiry_days": scope_rules.max_expiry_days(preset.scopes),
                           "roles": [role for role in scope_rules.ROLES
                                     if name in scope_rules.presets_for(role)]}
                    for name, preset in scope_rules.PRESETS.items()},
        "services": {name: sorted(granted)
                     for name, granted in scope_rules.SERVICE_PRINCIPALS.items()},
    })


# ── everyone's keys ───────────────────────────────────────────────────────────


@router.get("/admin/keys")
async def all_keys(request: Request) -> Response:
    rt = runtime.get()
    wanted = request.query_params.get("user")
    if wanted is not None and not scope_rules.USER_ID.fullmatch(wanted):
        raise ApiError(400, "user must be a user ID.", code="invalid_value", param="user")
    names = {row["id"]: row["username"] for row in users.listing(rt.db)}
    return no_store(JSONResponse({"keys": [
        {**apikeys.public(row), "username": names.get(row["user_id"])}
        for row in apikeys.listing(rt.db, wanted)]}))


@router.delete("/admin/keys/{key_id}")
async def revoke_any_key(request: Request, key_id: str) -> Response:
    rt = runtime.get()
    principal = principal_of(request)
    if apikeys.get(rt.db, key_id) is None:
        raise _not_found("key")
    apikeys.revoke(rt.db, key_id, by=principal.sub)
    rt.streams.close(credential=f"key:{key_id}")
    rt.trail.record(action="key_revoked", outcome="ok", actor=principal.actor,
                    ip=client_ip_of(request), target=key_id)
    return Response(status_code=204)


# ── audit ─────────────────────────────────────────────────────────────────────


@router.get("/admin/audit")
async def audit(request: Request) -> Response:
    """Newest first, paged by `before` (an id); aggregated rows only when asked."""
    rt = runtime.get()
    query = request.query_params
    clauses, params = [], []
    if query.get("aggregated") not in ("1", "true"):
        clauses.append("aggregated = 0")
    for field in ("actor", "action", "outcome"):
        value = query.get(field)
        if value:
            clauses.append(f"{'actor_id' if field == 'actor' else field} = ?")
            params.append(value)
    before = query.get("before")
    if before:
        if not before.isdigit():
            raise ApiError(400, "before must be a row id.", code="invalid_value",
                           param="before")
        clauses.append("id < ?")
        params.append(int(before))
    limit = query.get("limit", str(AUDIT_PAGE))
    if not limit.isdigit():
        raise ApiError(400, "limit must be a number.", code="invalid_value", param="limit")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    rows = rt.db.all(f"SELECT * FROM audit {where}ORDER BY id DESC LIMIT ?",
                     (*params, min(int(limit), AUDIT_PAGE_MAX)))
    return no_store(JSONResponse({
        "rows": [{**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else None,
                  "aggregated": bool(row["aggregated"])} for row in rows],
        "overflow": rt.trail.overflowed}))
