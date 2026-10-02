"""API keys: several per user, each with its own scopes and expiry (D26-D30).

    row, plaintext = create(db, user=..., name="laptop", scopes=..., days=90, ...)
    found = authenticate(db, "calliope_…")   # KeyAuth, or raises Refused

**What a key may do is decided on every request** (D29): its stored scopes
intersected with its owner's CURRENT role, minus every session-only scope, and
nothing at all once the owner is disabled. A demotion narrows existing keys at
once, and a row written outside the API still cannot carry `users:manage`.

**The plaintext exists once**, in the response that created it (D27). The
table holds its SHA-256 and a display form.

**`last_used_at` is written at most once a minute.** A key in a loop would
otherwise turn every request into a write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from voice_common import scopes as scope_rules

from . import db as dbmod
from .db import Database, iso
from .tokens import USER_KEY_PREFIX, digest, display, identifier, mint, well_formed

TOUCH_SECONDS = 60
MAX_NAME = 64


class Refused(Exception):
    """A key that does not authenticate: `invalid` or `expired`. Never says which row."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class KeyAuth:
    row: sqlite3.Row
    user: sqlite3.Row
    scopes: frozenset[str]

    @property
    def id(self) -> str:
        return self.row["id"]


def _owner_scopes(row: sqlite3.Row, user: sqlite3.Row) -> frozenset[str]:
    try:
        stored = json.loads(row["scopes"])
    except ValueError:
        return frozenset()
    if not isinstance(stored, list) or not all(isinstance(s, str) for s in stored):
        return frozenset()
    return scope_rules.effective(stored, user["role"])


def _check(database: Database, row: sqlite3.Row | None) -> KeyAuth:
    if row is None or row["revoked_at"] is not None:
        raise Refused("invalid")
    user = database.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if user is None or user["disabled_at"] is not None or user["deleted_at"] is not None:
        raise Refused("invalid")
    if row["expires_at"] is not None and row["expires_at"] <= iso(dbmod.now()):
        raise Refused("expired")
    return KeyAuth(row=row, user=user, scopes=_owner_scopes(row, user))


def authenticate(database: Database, token: str) -> KeyAuth:
    # Shape and checksum first: a typo is refused without a lookup.
    if not well_formed(token, USER_KEY_PREFIX):
        raise Refused("invalid")
    return _check(database, database.one("SELECT * FROM api_keys WHERE hash = ?",
                                         (digest(token),)))


def live(database: Database, key_id: str) -> KeyAuth | None:
    """The key with this ID if it still authenticates: the delegation re-check (D64)."""
    try:
        return _check(database, database.one("SELECT * FROM api_keys WHERE id = ?",
                                             (key_id,)))
    except Refused:
        return None


def touch(database: Database, row: sqlite3.Row, ip: str | None) -> None:
    at = dbmod.now()
    last = row["last_used_at"]
    if last is None or at - dbmod.parse_iso(last) >= TOUCH_SECONDS:
        database.execute("UPDATE api_keys SET last_used_at = ?, last_used_ip = ? "
                         "WHERE id = ?", (iso(at), ip, row["id"]))


def create(database: Database, *, user: sqlite3.Row, name: str, scopes: frozenset[str],
           preset: str | None, days: int | None, created_by: str) -> tuple[sqlite3.Row, str]:
    """Store a key the caller has already checked against the rules. Returns (row, plaintext)."""
    plaintext = mint(USER_KEY_PREFIX)
    key_id = identifier("k_", 12)
    at = dbmod.now()
    database.execute(
        "INSERT INTO api_keys (id, user_id, name, hash, display, scopes, preset, "
        "created_at, created_by, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key_id, user["id"], name[:MAX_NAME], digest(plaintext), display(plaintext),
         json.dumps(sorted(scope_rules.expand(scopes))), preset, iso(at), created_by,
         None if days is None else iso(at + days * 24 * 3600)))
    row = database.one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    assert row is not None
    return row, plaintext


def get(database: Database, key_id: str) -> sqlite3.Row | None:
    return database.one("SELECT * FROM api_keys WHERE id = ?", (key_id,))


def revoke(database: Database, key_id: str, *, by: str | None) -> bool:
    cursor = database.execute(
        "UPDATE api_keys SET revoked_at = ?, revoked_by = ? WHERE id = ? "
        "AND revoked_at IS NULL", (iso(dbmod.now()), by, key_id))
    return cursor.rowcount > 0


def revoke_user(database: Database, user_id: str, *, by: str | None) -> list[str]:
    rows = database.all("SELECT id FROM api_keys WHERE user_id = ? AND revoked_at IS NULL",
                        (user_id,))
    for row in rows:
        revoke(database, row["id"], by=by)
    return [row["id"] for row in rows]


def listing(database: Database, user_id: str | None = None) -> list[sqlite3.Row]:
    if user_id is None:
        return database.all("SELECT * FROM api_keys ORDER BY created_at DESC")
    return database.all("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                        (user_id,))


def public(row: sqlite3.Row) -> dict[str, object]:
    """A key as the API lists it: the display form, never the hash."""
    return {"id": row["id"], "user_id": row["user_id"], "name": row["name"],
            "display": row["display"], "preset": row["preset"],
            "scopes": json.loads(row["scopes"]), "created_at": row["created_at"],
            "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
            "last_used_ip": row["last_used_ip"], "revoked_at": row["revoked_at"]}
