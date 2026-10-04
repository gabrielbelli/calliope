"""Admin › Secrets on :8080, and the two routes services use on :8081 (§3.2, §3.6, D40-D47, D66).

    admin_router      GET /admin/secrets                     list, never a value
                      PUT|PATCH|DELETE /admin/secrets/{name}  set; bindings or review; clear
                      POST /admin/secrets/rotate-master       generated keyring only
    internal_router   GET /internal/secrets/{name}           secrets:fetch, a listed consumer
                      POST /internal/secrets/import          secrets:import, inside the window

The route tables in main.py and internal.py decide who reaches each route:
every admin row is session-only and every write needs a step-up (D13, D60),
so no API key can set, clear or rebind a secret, and the guard has refused it
before these handlers run. What is left here depends on the row: whether the
calling service is one of its consumers, and whether that service's import
window is still open.

**Nothing here answers with a value except the internal fetch.** Errors name
the field and never repeat what was in it, and audit rows hold names,
versions and counts, never values (D40).

**Refusals a service can repeat are counted, and named while they are new**
(§2.1, recheck M-3). Every one goes into its minute's aggregated row. Which
names a service asked for is the forensic signal, so each distinct name
refused is also a security event, up to secret_store.NAMED_REFUSALS a minute
per action and reason. A confused or compromised service cannot fill the
year-long tier, and a hub that tries to plant three names leaves all three
in it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field
from voice_common import errors
from voice_common.errors import ApiError

from . import runtime, secret_store
from .authn import Principal, client_ip_of, no_store, principal_of
from .routes_auth import _Body, json_body
from .secret_store import (MASTER_KEY_VARIABLE, MAX_AGE, MAX_DESCRIPTION, MAX_HOSTS,
                           MAX_VALUE, ImportItem, Invalid, SecretStore, Undecryptable,
                           WindowClosed)

admin_router = APIRouter()
internal_router = APIRouter()

# json_body bounds a batch at 64 KiB anyway; a service with more sends more
# batches and marks the last one final (D66).
MAX_BATCH = 100
MAX_CONSUMERS = 8


class SecretPut(_Body):
    value: str = Field(min_length=1, max_length=MAX_VALUE)
    kind: Literal["bearer", "password", "secret_url"] | None = None
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    consumers: list[str] | None = Field(default=None, max_length=MAX_CONSUMERS)
    allowed_hosts: list[str] | None = Field(default=None, max_length=MAX_HOSTS)


class SecretPatch(_Body):
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    consumers: list[str] | None = Field(default=None, max_length=MAX_CONSUMERS)
    allowed_hosts: list[str] | None = Field(default=None, max_length=MAX_HOSTS)
    # Confirming an imported row (D66). There is no un-confirming: a row an
    # admin doubts is set again or cleared.
    reviewed: Literal[True] | None = None


class ImportEntry(_Body):
    # Checked per entry by the store, not here: one odd name in secrets.json
    # must cost that name, not the whole batch.
    name: str = Field(max_length=128)
    kind: str = Field(max_length=32)
    value: str
    allowed_hosts: list[str] = Field(default_factory=list, max_length=4 * MAX_HOSTS)
    # "env SATELLITES_HA_TOKEN", "file TTS_RUNNER_API_KEY_FILE", "secrets.json" or "config".
    source: str | None = Field(default=None, max_length=128)


class ImportBatch(_Body):
    secrets: list[ImportEntry] = Field(default_factory=list, max_length=MAX_BATCH)
    # Every secret name the service's own configuration refers to, with the
    # hosts of the actions that refer to it (D66).
    declared: dict[str, list[str]] = Field(default_factory=dict)
    final: bool = False


# ── shared ────────────────────────────────────────────────────────────────────


def _store() -> SecretStore:
    store = secret_store.get()
    if not store.available:
        # :8080 is locked as a whole before a request gets here (D63); :8081
        # stays up, so its secrets routes say why, and a consumer keeps its
        # last good value on a 503 (D42).
        raise errors.locked("keyring_unreadable", MASTER_KEY_VARIABLE)
    return store


def _refused(exc: Invalid, status: int = 400) -> ApiError:
    return ApiError(status, exc.message, code=exc.code, param=exc.param)


def _not_found() -> ApiError:
    return ApiError(404, "No such secret.", code="not_found", param="name")


def _row(store: SecretStore, name: str) -> Any:
    try:
        return store.row(name)
    except Invalid as exc:
        raise _refused(exc) from None


def _audit(request: Request, principal: Principal, **row: Any) -> None:
    runtime.get().trail.record(actor=principal.actor, ip=client_ip_of(request), **row)


def _audit_refusal(request: Request, principal: Principal, store: SecretStore, *,
                   action: str, reason: str, target: str | None) -> None:
    trail = runtime.get().trail
    ip = client_ip_of(request)
    trail.count("secrets", f"{action}|{principal.sub}|{reason}", path=request.scope["path"],
                action=action, ip=ip, actor=principal.actor, target=target,
                detail={"reason": reason})
    if store.worth_naming((action, principal.sub, reason), target):
        trail.record(action=action, outcome="denied", actor=principal.actor, ip=ip,
                     target=target, detail={"reason": reason})


# ── Admin › Secrets (:8080) ───────────────────────────────────────────────────


async def _health_probes() -> dict[str, Any]:
    """The backends' own /health bodies, from the gateway's 5-second probe cache (D50)."""
    from . import main   # main includes these routers, so it cannot be imported at the top
    cache = main.state.get("health")
    return await cache.get() if cache is not None else {}


@admin_router.get("/admin/secrets")
async def list_secrets(request: Request) -> Response:
    store = _store()
    rows = store.listing()
    return no_store(JSONResponse({
        "secrets": rows,
        "keyring": store.keyring_status(),
        "status": secret_store.status_rows(await _health_probes(), os.environ),
        "counts": {"unreviewed": sum(row["unreviewed"] for row in rows),
                   "undecryptable": sum(row["undecryptable"] for row in rows)}}))


@admin_router.put("/admin/secrets/{name}")
async def put_secret(request: Request, name: str) -> Response:
    store = _store()
    principal = principal_of(request)
    body: SecretPut = await json_body(request, SecretPut)
    try:
        row, created, version = store.put(
            name, value=body.value, kind=body.kind, description=body.description,
            consumers=body.consumers, allowed_hosts=body.allowed_hosts, by=principal.sub)
    except Invalid as exc:
        raise _refused(exc, 409 if exc.code == "store_full" else 400) from None
    _audit(request, principal, action="secret_set", outcome="ok", target=name,
           detail={"created": created, "kind": row["kind"], "version": version,
                   "fields": sorted(field for field in ("kind", "description", "consumers",
                                                        "allowed_hosts")
                                    if getattr(body, field) is not None)})
    return no_store(JSONResponse({"secret": row}, status_code=201 if created else 200))


@admin_router.patch("/admin/secrets/{name}")
async def change_secret(request: Request, name: str) -> Response:
    store = _store()
    principal = principal_of(request)
    body: SecretPatch = await json_body(request, SecretPatch)
    changed = sorted(field for field in ("description", "consumers", "allowed_hosts")
                     if getattr(body, field) is not None)
    if not changed and not body.reviewed:
        raise ApiError(400, "Nothing to change: send description, consumers, allowed_hosts "
                            "or reviewed.", code="nothing_to_change")
    try:
        row = store.change(name, description=body.description, consumers=body.consumers,
                           allowed_hosts=body.allowed_hosts, reviewed=bool(body.reviewed),
                           by=principal.sub)
    except Invalid as exc:
        raise _refused(exc) from None
    if row is None:
        raise _not_found()
    if changed:
        _audit(request, principal, action="secret_bindings_changed", outcome="ok",
               target=name, detail={"fields": changed, "consumers": row["consumers"],
                                    "allowed_hosts": row["allowed_hosts"]})
    if body.reviewed:
        _audit(request, principal, action="secret_reviewed", outcome="ok", target=name)
    return no_store(JSONResponse({"secret": row}))


@admin_router.delete("/admin/secrets/{name}")
async def clear_secret(request: Request, name: str) -> Response:
    store = _store()
    principal = principal_of(request)
    try:
        found = store.clear(name, by=principal.sub)
    except Invalid as exc:
        raise _refused(exc) from None
    if not found:
        raise _not_found()
    _audit(request, principal, action="secret_cleared", outcome="ok", target=name)
    return Response(status_code=204)


@admin_router.post("/admin/secrets/rotate-master")
async def rotate_master(request: Request) -> Response:
    store = _store()
    principal = principal_of(request)
    try:
        rotated, unreadable = store.rotate_master()
    except Invalid as exc:
        raise _refused(exc, 409) from None
    _audit(request, principal, action="master_key_rotated", outcome="ok",
           detail={"rotated": rotated, "undecryptable": len(unreadable)})
    return no_store(JSONResponse({"rotated": rotated, "undecryptable": unreadable}))


# ── services (:8081) ──────────────────────────────────────────────────────────


@internal_router.get("/internal/secrets/{name}")
async def fetch_secret(request: Request, name: str) -> Response:
    """A consumer's cache miss (D42): the value, its version and where it may be sent."""
    store = _store()
    principal = principal_of(request)
    row = _row(store, name)
    if row is None:
        raise _not_found()
    if principal.service not in json.loads(row["consumers"]):
        _audit_refusal(request, principal, store, action="secret_fetch_denied",
                       reason="not_a_consumer", target=name)
        # 403, not 404, although it tells the service that the name exists.
        # Names are not secret: they are written in the services' own
        # configuration. Only a principal holding secrets:fetch gets here, and
        # the hub turns this answer into "add this hub as a consumer", the one
        # mistake an operator makes with a row that exists.
        raise ApiError(403, "This service is not a consumer of this secret.",
                       code="not_a_consumer", param="name")
    if row["ciphertext"] is None:
        # Cleared: the consumer stops at once, because a miss is never cached.
        raise _not_found()
    try:
        value = store.reveal(row)
    except Undecryptable:
        # Not a refusal the service provoked: the row is broken on this side.
        # So it is never folded into a `denied` aggregate, and is audited as a
        # failure once an hour per version and consumer, like a fetch, however
        # often the consumer asks again.
        if store.first_this_hour("undecryptable", name, row["version"], principal.sub):
            _audit(request, principal, action="secret_decrypt_failed", outcome="failed",
                   target=name, detail={"version": row["version"]})
        # 503, not 404: the fault is the gateway's, so the consumer keeps its
        # last good value until an admin stores this one again (D42, D43).
        raise ApiError(503, "This secret cannot be decrypted; an admin must store it "
                            "again.", type_="server_error", code="undecryptable",
                       param="name") from None
    if store.mark_read(name, row["version"], principal.sub):
        _audit(request, principal, action="secret_fetched", outcome="ok", target=name,
               detail={"version": row["version"]})
    return no_store(JSONResponse({
        "value": value, "version": row["version"], "kind": row["kind"],
        "allowed_hosts": json.loads(row["allowed_hosts"]), "max_age": MAX_AGE}))


@internal_router.post("/internal/secrets/import")
async def import_secrets(request: Request) -> Response:
    """Copy what a service still holds in its environment or files, once (D44, D66).

    One result per entry, in the order sent: `imported`, `exists` (the store
    already has the name, set or cleared, and wins) or `refused` with a
    reason. `imported` and `exists` both mean the service may stop holding
    the value itself.
    """
    store = _store()
    principal = principal_of(request)
    batch: ImportBatch = await json_body(request, ImportBatch)
    service = principal.service
    items = [ImportItem(entry.name, entry.kind, entry.value, tuple(entry.allowed_hosts),
                        entry.source) for entry in batch.secrets]
    try:
        outcomes = store.import_batch(service, items, batch.declared, final=batch.final)
    except WindowClosed:
        _audit_refusal(request, principal, store, action="secret_import_refused",
                       reason="import_closed", target=None)
        raise ApiError(410, "This service's import window is closed. An operator reopens "
                            "it with `python -m app.admin reopen-import`.",
                       code="import_closed") from None
    for outcome in outcomes:
        if outcome.outcome == "imported":
            _audit(request, principal, action="secret_imported", outcome="ok",
                   target=outcome.name, detail={"hosts_dropped": outcome.hosts_dropped})
        elif outcome.outcome == "refused":
            _audit_refusal(request, principal, store, action="secret_import_refused",
                           reason=outcome.reason or "refused", target=outcome.name)
    if batch.final:
        _audit(request, principal, action="secret_import_closed", outcome="ok",
               target=service)
    return JSONResponse({
        "results": [{"name": outcome.name, "outcome": outcome.outcome,
                     **({"reason": outcome.reason} if outcome.reason else {})}
                    for outcome in outcomes],
        "closed": batch.final})
