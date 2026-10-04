"""The gateway's SQLite database: one file, WAL, migrations by user_version (D1).

    db = Database.open(Path("/data/calliope.db"))
    with db.transaction():          # BEGIN IMMEDIATE ... COMMIT
        db.execute("UPDATE ...", (...))

**stdlib sqlite3 and nothing else.** One process writes (the gateway, plus the
odd `python -m app.admin` run), the rows are small, and WAL lets the CLI read
and write while the gateway serves. busy_timeout covers the moment both write.

**Autocommit unless a transaction is asked for.** A rule that spans two
statements (the last active admin, consuming the bootstrap) runs inside
`transaction()`, which takes the write lock up front with BEGIN IMMEDIATE, so
two requests cannot both read "one admin left" and both demote (recheck L6).

**Calls are synchronous.** Every query here is an indexed lookup on a local
file a few hundred kilobytes long; moving each to a thread would cost more than
it saves. Password hashing, which is slow on purpose, is the exception and
lives in passwords.py.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).with_name("migrations")


def iso(seconds: float) -> str:
    """UTC ISO 8601 to the second: the one time format every table stores.

    One format, so a string comparison in SQL is a time comparison.
    """
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._lock = threading.RLock()
        self._depth = 0

    @classmethod
    def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), check_same_thread=False,
                                     isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        # NORMAL is durable across an application crash in WAL mode and loses
        # at most the last commit on a power cut, which costs a login, not data.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        database = cls(connection)
        database.migrate()
        return database

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    # ── migrations ────────────────────────────────────────────────────────────

    def migrate(self) -> int:
        """Apply every migrations/NNNN_*.sql above PRAGMA user_version, in order.

        Each in one transaction that sets user_version as it commits, and
        rolls the whole script back if any statement in it fails.

        FOREIGN KEYS ARE OFF WHILE A MIGRATION RUNS, AND CHECKED BEFORE IT
        COMMITS. SQLite cannot alter a CHECK, so changing one means rebuilding
        the table (sqlite.org/lang_altertable.html#otheralter), and with foreign
        keys on, dropping the old `users` would cascade and take every session
        and key with it. The pragma does nothing inside a transaction, so it is
        set around it; any row foreign_key_check finds rolls the migration
        back.
        """
        with self._lock:
            current = self.connection.execute("PRAGMA user_version").fetchone()[0]
            pending = [(int(script.name[:4]), script) for script in
                       sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
                       if int(script.name[:4]) > current]
            if not pending:
                return current
            self.connection.execute("PRAGMA foreign_keys=OFF")
            try:
                for version, script in pending:
                    self._apply(script, version)
                    current = version
            finally:
                self.connection.execute("PRAGMA foreign_keys=ON")
            return current

    def _apply(self, script: Path, version: int) -> None:
        sql = script.read_text(encoding="utf-8")
        try:
            # executescript commits whatever is open first, so the transaction
            # is begun inside the script it runs, and ended here.
            self.connection.executescript(f"BEGIN IMMEDIATE;\n{sql}\n")
            broken = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(
                    f"{script.name} leaves {len(broken)} row(s) whose foreign key "
                    f"points nowhere, first in {broken[0][0]}")
            self.connection.execute(f"PRAGMA user_version = {version}")
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    # ── statements ────────────────────────────────────────────────────────────

    def execute(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
        """Positional parameters, or a mapping for `:name` placeholders."""
        bound = params if isinstance(params, Mapping) else tuple(params)
        with self._lock:
            return self.connection.execute(sql, bound)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.connection.executemany(sql, rows)

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(sql, tuple(params)).fetchall()

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """BEGIN IMMEDIATE: the write lock is taken before the first read.

        Nested use joins the outer transaction, so a helper that needs one can
        be called from inside another.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self.connection.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")
            finally:
                self._depth = 0

    # ── meta ──────────────────────────────────────────────────────────────────

    def meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    def delete_meta(self, key: str) -> None:
        self.execute("DELETE FROM meta WHERE key = ?", (key,))


def now() -> float:
    """The clock every module reads, so a test can move it."""
    return time.time()
