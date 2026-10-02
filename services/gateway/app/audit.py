"""The audit trail: one table, two tiers, and the same row on stdout (§2.1).

    trail.record(action="login", outcome="ok", actor=principal.actor, ip=ip)
    trail.refused(ip=ip, path="/v1/models", code="")     # a 401, counted per minute

**Two tiers, so noise cannot push out evidence** (M6). A security event
(`aggregated=0`: a login, a key created, a microphone opened) is kept for a
year and never dropped for a row cap. Refusals an internet client can produce
at will (401s, 403s, failed logins) are counted per minute into one
`aggregated=1` row each, and that tier is capped at 100k rows, oldest first.

**The security tier has a ceiling too** (recheck M-3). Only events about an
existing account reach it from an anonymous request, but a ceiling is what
makes "never grows without bound" true rather than likely: at a million rows
one `audit_overflow` event is written, later events go to stdout only, and the
Admin › Audit page shows a banner until the yearly sweep makes room.

**Values never go in a row.** Not a password, a key, a token or a body: the
columns have no slot for one, and `detail` is built by this module's callers
from names and counts only. Each row is also printed as one `audit {...}`
line by voice_common.audit, the format the backends use for their own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from voice_common import audit as audit_line

from . import db as dbmod
from .db import Database, iso

log = logging.getLogger("voice-gateway.audit")

WINDOW_SECONDS = 60
MAX_PATHS = 5
MAX_PATH_LENGTH = 200
# How many distinct (kind, who) windows one minute may hold. Past it, new
# keys fold into one "*" bucket per kind, so a flood costs a counter, not
# memory (recheck M-4).
MAX_WINDOWS = 4096

SECURITY_RETENTION_SECONDS = 365 * 24 * 3600
NOISE_CAP = 100_000
SECURITY_CEILING = 1_000_000

UNKNOWN_USERNAME = "<unknown>"


@dataclass(frozen=True)
class Actor:
    """Who acted, in the audit table's three columns."""

    kind: str                     # user | api_key | service | cli | anonymous
    id: str | None = None         # a user ID, or svc:<name>
    auth_method: str | None = None  # session | key:k_… | svc:<name>


ANONYMOUS = Actor("anonymous")
CLI = Actor("cli", auth_method="cli")



class Trail:
    def __init__(self, database: Database, *, security_ceiling: int = SECURITY_CEILING,
                 noise_cap: int = NOISE_CAP) -> None:
        self.db = database
        self.security_ceiling = security_ceiling
        self.noise_cap = noise_cap
        self._windows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._security_rows = self._count_security()
        self._overflowed = database.meta("audit_overflow") is not None

    # ── rows ──────────────────────────────────────────────────────────────────

    def _count_security(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM audit WHERE aggregated = 0")
        return int(row["n"]) if row else 0

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def record(self, *, action: str, outcome: str, actor: Actor = ANONYMOUS,
               ip: str | None = None, target: str | None = None,
               detail: dict[str, Any] | None = None, request_id: str | None = None,
               at: float | None = None) -> None:
        """One security event: stored and printed, or printed only past the ceiling."""
        row = {"ts": iso(dbmod.now() if at is None else at), "actor_kind": actor.kind,
               "actor_id": actor.id, "auth_method": actor.auth_method, "ip": ip,
               "action": action, "target": target, "outcome": outcome,
               "aggregated": 0, "request_id": request_id, "detail": detail}
        if self._security_rows >= self.security_ceiling:
            if not self._overflowed:
                self._overflowed = True
                self.db.set_meta("audit_overflow", row["ts"])
                self._insert({**row, "action": "audit_overflow", "actor_kind": "anonymous",
                              "actor_id": None, "auth_method": None, "ip": None,
                              "target": None, "outcome": "failed",
                              "detail": {"ceiling": self.security_ceiling}})
                log.error("the audit table holds %d security events, its ceiling; "
                          "further events are written to stdout only until the "
                          "yearly sweep makes room", self.security_ceiling)
            audit_line.emit(_printable(row))
            return
        self._insert(row)

    def _insert(self, row: dict[str, Any]) -> None:
        stored = {**row, "detail": json.dumps(row["detail"], separators=(",", ":"),
                                              ensure_ascii=True)
                  if row["detail"] is not None else None}
        self.db.execute(
            "INSERT INTO audit (ts, actor_kind, actor_id, auth_method, ip, action, "
            "target, outcome, aggregated, request_id, detail) "
            "VALUES (:ts, :actor_kind, :actor_id, :auth_method, :ip, :action, "
            ":target, :outcome, :aggregated, :request_id, :detail)", stored)
        if not row["aggregated"]:
            self._security_rows += 1
        audit_line.emit(_printable(row))

    # ── the noise tier ────────────────────────────────────────────────────────

    def count(self, kind: str, who: str, *, path: str, action: str, ip: str | None,
              actor: Actor = ANONYMOUS, target: str | None = None,
              detail: dict[str, Any] | None = None, outcome: str = "denied") -> bool:
        """Count one request into its minute. True if it is the first of its window.

        `kind` names the aggregation rule (401 per IP, 403 per credential,
        failed login per username and IP, an `:all` read per credential) and
        `who` is its key. Most counted requests are refusals; an `:all` read
        that was allowed is counted with outcome "ok".
        """
        at = dbmod.now()
        window = int(at // WINDOW_SECONDS)
        self.flush(before=window)
        key = (kind, who, action)
        if key not in self._windows and len(self._windows) >= MAX_WINDOWS:
            key = (kind, "*", action)
            ip, actor, target = None, ANONYMOUS, None
        entry = self._windows.get(key)
        first = entry is None
        if first:
            entry = self._windows[key] = {
                "window": window, "count": 0, "first": at, "last": at, "paths": [],
                "action": action, "ip": ip, "actor": actor, "target": target,
                "outcome": outcome, "detail": detail or {}}
        entry["count"] += 1
        entry["last"] = at
        trimmed = path[:MAX_PATH_LENGTH]
        if trimmed not in entry["paths"] and len(entry["paths"]) < MAX_PATHS:
            entry["paths"].append(trimmed)
        return first

    def flush(self, before: int | None = None) -> None:
        """Write every window that has closed; every window when `before` is None."""
        for key in [k for k, e in self._windows.items()
                    if before is None or e["window"] < before]:
            entry = self._windows.pop(key)
            actor: Actor = entry["actor"]
            self._insert({
                "ts": iso(entry["first"]), "actor_kind": actor.kind,
                "actor_id": actor.id, "auth_method": actor.auth_method,
                "ip": entry["ip"], "action": entry["action"], "target": entry["target"],
                "outcome": entry["outcome"], "aggregated": 1, "request_id": None,
                "detail": {**entry["detail"], "count": entry["count"],
                           "first": iso(entry["first"]), "last": iso(entry["last"]),
                           "paths": entry["paths"]}})

    def refused(self, *, ip: str | None, path: str, code: str) -> None:
        """A 401, counted per IP per minute. The guard counts its 403s per credential."""
        self.count("401", ip or "-", path=path, action="request_refused", ip=ip,
                   detail={"status": 401, "code": code})

    # ── retention ─────────────────────────────────────────────────────────────

    def sweep(self) -> None:
        """Hourly: security events older than a year, and the noise tier past its cap."""
        cutoff = iso(dbmod.now() - SECURITY_RETENTION_SECONDS)
        self.db.execute("DELETE FROM audit WHERE aggregated = 0 AND ts < ?", (cutoff,))
        self.db.execute(
            "DELETE FROM audit WHERE aggregated = 1 AND id NOT IN "
            "(SELECT id FROM audit WHERE aggregated = 1 ORDER BY id DESC LIMIT ?)",
            (self.noise_cap,))
        self._security_rows = self._count_security()
        if self._overflowed and self._security_rows < self.security_ceiling:
            self._overflowed = False
            self.db.delete_meta("audit_overflow")


def _printable(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in audit_line.FIELDS}
