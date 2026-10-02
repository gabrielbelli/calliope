"""Server-side sessions: an opaque cookie, its SHA-256 in the table (D11, D12).

    session_id, ref = create(db, user_id=..., ip=..., user_agent=..., restricted=False)
    found = lookup(db, session_id)      # None when revoked, expired or the user is not active

**Only the hash is stored**, so a database backup holds no live session, and
logout, a password change, a role change or a disabled account takes effect on
the next request rather than at some expiry.

**A year absolute, 30 days idle** (D12), so a browser used for everyday
speech work stays signed in; step-up still guards the sensitive actions.
`last_seen_at` is written at most
once a minute: every request reads the row, and writing it on every one would
make a page of thirty requests thirty writes.

**A restricted session lives 15 minutes** and reaches only /auth/me,
/auth/password, /auth/logout and /login (D21). Every login of an account with
`must_change` set gets one, not only the bootstrap login (recheck M-7): a
temporary password an admin read, or the CLI printed, must not be able to
create keys that outlive the change.

**`ref` is the row's public name.** The account page lists sessions by it and
a delegation token names the session by it (`session:<ref>`, D64); the ID
itself never leaves the cookie.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

from . import db as dbmod
from .db import Database, iso
from .tokens import digest

ABSOLUTE_SECONDS = 365 * 24 * 3600
IDLE_SECONDS = 30 * 24 * 3600
RESTRICTED_SECONDS = 15 * 60
STEP_UP_SECONDS = 10 * 60
TOUCH_SECONDS = 60
MAX_USER_AGENT = 256


@dataclass(frozen=True)
class Session:
    id_hash: str
    ref: str
    user: sqlite3.Row
    restricted: bool
    stepup_until: str | None
    expires_at: str

    @property
    def stepped_up(self) -> bool:
        return self.stepup_until is not None and self.stepup_until > iso(dbmod.now())


def create(database: Database, *, user_id: str, ip: str | None, user_agent: str | None,
           restricted: bool) -> tuple[str, str]:
    """A new session. Returns (the cookie value, the row's public ref)."""
    session_id = secrets.token_urlsafe(32)
    ref = secrets.token_urlsafe(18)
    at = dbmod.now()
    lifetime = RESTRICTED_SECONDS if restricted else ABSOLUTE_SECONDS
    database.execute(
        "INSERT INTO sessions (id_hash, ref, user_id, created_at, last_seen_at, "
        "expires_at, idle_expires_at, restricted, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (digest(session_id), ref, user_id, iso(at), iso(at), iso(at + lifetime),
         iso(at + min(lifetime, IDLE_SECONDS)), int(restricted), ip,
         (user_agent or "")[:MAX_USER_AGENT] or None))
    return session_id, ref


def _live(database: Database, row: sqlite3.Row | None) -> Session | None:
    if row is None or row["revoked_at"] is not None:
        return None
    stamp = iso(dbmod.now())
    if row["expires_at"] <= stamp or row["idle_expires_at"] <= stamp:
        return None
    user = database.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if user is None or user["disabled_at"] is not None or user["deleted_at"] is not None:
        return None
    return Session(id_hash=row["id_hash"], ref=row["ref"], user=user,
                   restricted=bool(row["restricted"]), stepup_until=row["stepup_until"],
                   expires_at=row["expires_at"])


def lookup(database: Database, session_id: str) -> Session | None:
    """The live session this cookie names, touched; None for anything else."""
    if not session_id or len(session_id) > 128:
        return None
    row = database.one("SELECT * FROM sessions WHERE id_hash = ?", (digest(session_id),))
    found = _live(database, row)
    if found is not None and row is not None:
        at = dbmod.now()
        if at - dbmod.parse_iso(row["last_seen_at"]) >= TOUCH_SECONDS:
            idle = min(at + IDLE_SECONDS, dbmod.parse_iso(row["expires_at"]))
            database.execute("UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? "
                             "WHERE id_hash = ?", (iso(at), iso(idle), row["id_hash"]))
    return found


def by_ref(database: Database, ref: str) -> Session | None:
    """The live session with this public ref, untouched: the delegation re-check (D64)."""
    return _live(database, database.one("SELECT * FROM sessions WHERE ref = ?", (ref,)))


def step_up(database: Database, id_hash: str) -> str:
    until = iso(dbmod.now() + STEP_UP_SECONDS)
    database.execute("UPDATE sessions SET stepup_until = ? WHERE id_hash = ?",
                     (until, id_hash))
    return until


def revoke(database: Database, id_hash: str) -> None:
    database.execute("UPDATE sessions SET revoked_at = ? WHERE id_hash = ? "
                     "AND revoked_at IS NULL", (iso(dbmod.now()), id_hash))


def revoke_hash_of(database: Database, session_id: str) -> None:
    """Revoke whatever row this cookie value names, live or not."""
    revoke(database, digest(session_id))


def revoke_user(database: Database, user_id: str, *, keep: str | None = None) -> list[str]:
    """Revoke every live session of a user, except `keep`. Returns the revoked refs."""
    rows = database.all(
        "SELECT id_hash, ref FROM sessions WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,))
    refs = []
    for row in rows:
        if row["id_hash"] != keep:
            revoke(database, row["id_hash"])
            refs.append(row["ref"])
    return refs


def listing(database: Database, user_id: str) -> list[sqlite3.Row]:
    stamp = iso(dbmod.now())
    return database.all(
        "SELECT * FROM sessions WHERE user_id = ? AND revoked_at IS NULL "
        "AND expires_at > ? AND idle_expires_at > ? ORDER BY last_seen_at DESC",
        (user_id, stamp, stamp))


def recent_ips(database: Database, user_id: str) -> set[str]:
    """Addresses this user's live sessions came from: exempt from the account ceiling (D19)."""
    return {row["ip"] for row in listing(database, user_id) if row["ip"]}
