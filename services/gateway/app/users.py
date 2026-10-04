"""People: the users table, its three roles, and the rules that prevent lock-out.

**The ID is stable and is never the username** (§2.1). Jobs, glossaries and
clips are owned by `u_…`, so renaming nothing ever moves data, and a deleted
user's data keeps an owner that can never be reused (D68).

**The last active admin cannot be removed** (D25). Demoting, disabling and
deleting all check it inside one BEGIN IMMEDIATE transaction, so two admins
demoting each other at the same moment cannot both succeed (recheck L6).
Nobody can delete or demote themselves.

**Deleting is soft** (D68): the row stays, its username stays reserved, and
the audit trail keeps naming the person who acted.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

from voice_common import scopes as scope_rules

from . import db as dbmod
from .db import Database, iso
from .tokens import identifier

# The roles a person can have, from the one copy every service reads, in the
# order Admin › Users offers them. The CHECK in migration 0002 names the same
# three, so a fourth needs a migration as well as a line in scopes.py.
ROLES = tuple(scope_rules.ROLES)
ROLES_SAID = ", ".join(ROLES[:-1]) + " or " + ROLES[-1]
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DISPLAY_NAME = 128


class Refused(Exception):
    """A change the rules forbid: answered 409 with this code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def account_key(username: str) -> str:
    """The throttle's key for an attempted name, whether or not the account exists (D19)."""
    return unicodedata.normalize("NFKC", username).strip().casefold()[:128]


def clean_username(username: str) -> str | None:
    """The username as stored, or None if it is not one this service accepts."""
    text = unicodedata.normalize("NFKC", username).strip()
    return text if USERNAME.fullmatch(text) else None


def active(row: sqlite3.Row | None) -> bool:
    return row is not None and row["disabled_at"] is None and row["deleted_at"] is None


def get(database: Database, user_id: str) -> sqlite3.Row | None:
    return database.one("SELECT * FROM users WHERE id = ?", (user_id,))


def by_username(database: Database, username: str) -> sqlite3.Row | None:
    name = unicodedata.normalize("NFKC", username).strip()
    return database.one("SELECT * FROM users WHERE username = ?", (name,))


def listing(database: Database) -> list[sqlite3.Row]:
    return database.all("SELECT * FROM users ORDER BY username")


def count(database: Database) -> int:
    return int(database.one("SELECT COUNT(*) AS n FROM users")["n"])


def _active_admins(database: Database) -> int:
    return int(database.one(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' "
        "AND disabled_at IS NULL AND deleted_at IS NULL")["n"])


def create(database: Database, *, username: str, role: str, password_hash: str | None,
           must_change: bool, created_by: str | None,
           display_name: str | None = None) -> sqlite3.Row:
    name = clean_username(username)
    if name is None:
        raise Refused("invalid_username", "A username is 1 to 64 letters, digits, "
                                          "dots, dashes or underscores.")
    if role not in ROLES:
        raise Refused("invalid_role", f"The role is {ROLES_SAID}.")
    stamp = iso(dbmod.now())
    user_id = identifier("u_", 16)
    try:
        database.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, "
            "must_change, created_at, created_by, updated_at, password_changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, (display_name or None) and display_name[:MAX_DISPLAY_NAME],
             role, password_hash, int(must_change), stamp, created_by, stamp,
             stamp if password_hash else None))
    except sqlite3.IntegrityError:
        # Deleted users keep their name (D68), so this is also the answer for
        # a name that belonged to someone who left.
        raise Refused("username_taken", "That username is taken.") from None
    row = get(database, user_id)
    assert row is not None
    return row


def set_password(database: Database, user_id: str, password_hash: str, *,
                 must_change: bool = False) -> None:
    stamp = iso(dbmod.now())
    database.execute("UPDATE users SET password_hash = ?, must_change = ?, "
                     "password_changed_at = ?, updated_at = ? WHERE id = ?",
                     (password_hash, int(must_change), stamp, stamp, user_id))


def rehash(database: Database, user_id: str, password_hash: str) -> None:
    """New parameters, same password: nothing else about the account changes."""
    database.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, user_id))


def record_login(database: Database, user_id: str, ip: str | None) -> None:
    database.execute("UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
                     (iso(dbmod.now()), ip, user_id))


def update(database: Database, *, actor_id: str | None, user_id: str,
           role: str | None = None, disabled: bool | None = None,
           display_name: str | None = None) -> sqlite3.Row:
    """Change role, disabled or display name, under the last-admin and self rules."""
    with database.transaction():
        row = get(database, user_id)
        if row is None or row["deleted_at"] is not None:
            raise LookupError(user_id)
        if role is not None and role not in ROLES:
            raise Refused("invalid_role", f"The role is {ROLES_SAID}.")
        demoting = role is not None and row["role"] == "admin" and role != "admin"
        disabling = disabled is True and row["disabled_at"] is None
        if demoting and actor_id == user_id:
            raise Refused("self", "You cannot change your own role.")
        if disabling and actor_id == user_id:
            raise Refused("self", "You cannot disable your own account.")
        if (demoting or disabling) and row["role"] == "admin" \
                and row["disabled_at"] is None and _active_admins(database) <= 1:
            raise Refused("last_admin", "This is the last active admin.")
        stamp = iso(dbmod.now())
        if role is not None:
            database.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                             (role, stamp, user_id))
        if disabled is not None:
            database.execute("UPDATE users SET disabled_at = ?, updated_at = ? WHERE id = ?",
                             (stamp if disabled else None, stamp, user_id))
        if display_name is not None:
            database.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                             (display_name[:MAX_DISPLAY_NAME] or None, stamp, user_id))
        updated = get(database, user_id)
        assert updated is not None
        return updated


def soft_delete(database: Database, *, actor_id: str | None, user_id: str) -> None:
    """Disable and mark deleted; nothing is freed (D68). The caller revokes credentials."""
    with database.transaction():
        row = get(database, user_id)
        if row is None or row["deleted_at"] is not None:
            raise LookupError(user_id)
        if actor_id == user_id:
            raise Refused("self", "You cannot delete your own account.")
        if active(row) and row["role"] == "admin" and _active_admins(database) <= 1:
            raise Refused("last_admin", "This is the last active admin.")
        stamp = iso(dbmod.now())
        database.execute(
            "UPDATE users SET disabled_at = COALESCE(disabled_at, ?), deleted_at = ?, "
            "updated_at = ? WHERE id = ?", (stamp, stamp, stamp, user_id))


def public(row: sqlite3.Row) -> dict[str, object]:
    """A user as the API shows it: never the hash."""
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "must_change": bool(row["must_change"]),
            "disabled": row["disabled_at"] is not None,
            "deleted": row["deleted_at"] is not None,
            "created_at": row["created_at"], "last_login_at": row["last_login_at"]}
