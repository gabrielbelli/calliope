"""The database's migrations, applied as app/db.py applies them (D1).

The one that matters here is 0002: `speech` became `user-jobs`, and SQLite
cannot alter the CHECK that named it, so the users table is rebuilt. Every
test starts from a database the gateway itself made at version 1, by running
the migration runner over 0001 alone, so what is migrated is exactly what a
deployed gateway holds.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app import db as dbmod

SAM = "u_aaaaaaaaaaaaaaaa"   # speech, with a session and two keys
ANA = "u_bbbbbbbbbbbbbbbb"   # admin
BEN = "u_cccccccccccccccc"   # speech, disabled and deleted


@pytest.fixture
def at_version_1(tmp_path, monkeypatch):
    """A database at user_version 1 holding the people, sessions, keys and
    audit rows a deployment had before 0002, and its path."""
    only_first = tmp_path / "migrations-0001"
    only_first.mkdir()
    shutil.copy(dbmod.MIGRATIONS / "0001_init.sql", only_first)
    path = tmp_path / "data" / "calliope.db"
    with monkeypatch.context() as patched:
        patched.setattr(dbmod, "MIGRATIONS", only_first)
        database = dbmod.Database.open(path)
    assert database.one("PRAGMA user_version")[0] == 1
    people = [
        (SAM, "sam", "Sam", "speech", "$argon2id$sam", 0, None, None, "2026-01-01T00:00:00Z",
         ANA, "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", "10.0.0.2"),
        (ANA, "ana", None, "admin", "$argon2id$ana", 0, None, None, "2026-01-01T00:00:00Z",
         None, "2026-01-01T00:00:00Z", None, "2026-03-02T00:00:00Z", "10.0.0.1"),
        (BEN, "ben", "Ben", "speech", None, 1, "2026-02-03T00:00:00Z", "2026-02-04T00:00:00Z",
         "2026-01-02T00:00:00Z", ANA, "2026-02-04T00:00:00Z", None, None, None),
    ]
    database.executemany(
        "INSERT INTO users (id, username, display_name, role, password_hash, must_change, "
        "disabled_at, deleted_at, created_at, created_by, updated_at, password_changed_at, "
        "last_login_at, last_login_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", people)
    database.execute(
        "INSERT INTO sessions (id_hash, ref, user_id, created_at, last_seen_at, expires_at, "
        "idle_expires_at, restricted) VALUES ('hash-sam', 'ref-sam', ?, 't0', 't1', 't2', 't3', 0)",
        (SAM,))
    keys = [("k_aaaaaaaaaaaa", SAM, "script", "h1", "calliope_Ab…yz", '["models:read"]', "speech"),
            ("k_bbbbbbbbbbbb", SAM, "player", "h2", "calliope_Cd…wx", '["speech:speak"]', "speak-only"),
            ("k_cccccccccccc", ANA, "ha", "h3", "calliope_Ef…uv", '["satellites:read"]', "home-assistant"),
            ("k_dddddddddddd", ANA, "mine", "h4", "calliope_Gh…st", '["models:read"]', None)]
    database.executemany(
        "INSERT INTO api_keys (id, user_id, name, hash, display, scopes, preset, created_at, "
        "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-05T00:00:00Z', 'test')", keys)
    database.execute(
        "INSERT INTO audit (ts, actor_kind, actor_id, action, target, outcome, detail) VALUES "
        "('2026-01-01T00:00:00Z', 'user', ?, 'user_created', ?, 'ok', "
        "'{\"username\": \"sam\", \"role\": \"speech\"}')", (ANA, SAM))
    database.set_meta("installation_id", "abc")
    database.close()
    return path


def snapshot(path) -> dict:
    """Every row of every table, and the schema, read without the runner."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        tables = [row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name")]
        return {
            "rows": {table: [dict(row) for row in connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables},
            "schema": {row["name"]: (row["type"], row["tbl_name"], row["sql"]) for row in
                       connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema")},
            "columns": {table: [tuple(row) for row in connection.execute(
                f'PRAGMA table_info("{table}")')] for table in tables},
            "foreign_keys": {table: [tuple(row) for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")')] for table in tables},
            "indexes": {table: sorted((row["name"], row["unique"], row["origin"]) for row in
                                      connection.execute(f'PRAGMA index_list("{table}")'))
                        for table in tables},
        }
    finally:
        connection.close()


def test_a_speech_user_and_a_speech_key_come_out_as_user_jobs_and_nothing_else_moves(at_version_1):
    before = snapshot(at_version_1)
    database = dbmod.Database.open(at_version_1)
    try:
        assert database.one("PRAGMA user_version")[0] >= 2
    finally:
        database.close()
    after = snapshot(at_version_1)

    roles = {row["id"]: row["role"] for row in after["rows"]["users"]}
    assert roles == {SAM: "user-jobs", ANA: "admin", BEN: "user-jobs"}
    # Every other column of every user exactly as it was, updated_at included:
    # their rights did not change, only the name of the role that holds them.
    renamed = [{**row, "role": "user-jobs" if row["role"] == "speech" else row["role"]}
               for row in before["rows"]["users"]]
    assert after["rows"]["users"] == renamed

    presets = {row["id"]: row["preset"] for row in after["rows"]["api_keys"]}
    assert presets == {"k_aaaaaaaaaaaa": "user-jobs", "k_bbbbbbbbbbbb": "speak-only",
                       "k_cccccccccccc": "home-assistant", "k_dddddddddddd": None}
    # A key's scopes are its own: the preset's new ingest:links is not added.
    assert [{**row, "preset": "user-jobs" if row["preset"] == "speech" else row["preset"]}
            for row in before["rows"]["api_keys"]] == after["rows"]["api_keys"]

    # The sessions, the audit trail (which keeps saying what the role was
    # called when it was written), meta and every other table: untouched.
    for table in before["rows"]:
        if table not in ("users", "api_keys"):
            assert after["rows"][table] == before["rows"][table], table


def test_the_rebuilt_table_keeps_every_column_index_and_foreign_key(at_version_1):
    before = snapshot(at_version_1)
    dbmod.Database.open(at_version_1).close()
    after = snapshot(at_version_1)

    assert after["columns"] == before["columns"]
    assert after["foreign_keys"] == before["foreign_keys"]
    assert after["foreign_keys"]["sessions"] and after["foreign_keys"]["api_keys"]
    assert all(fk[2] == "users" and fk[6] == "CASCADE"
               for table in ("sessions", "api_keys") for fk in after["foreign_keys"][table])
    assert after["indexes"] == before["indexes"]
    # Nothing in the schema moved but the users table's own statement, and
    # that only in its CHECK (and the quoted name a rename writes).
    assert {name for name in after["schema"] if after["schema"][name] != before["schema"][name]} \
        == {"users"}
    assert "CHECK (role IN ('admin','user','user-jobs'))" in after["schema"]["users"][2]
    assert after["schema"]["users"][2].replace('"users"', "users").replace(
        "'admin','user','user-jobs'", "'admin','speech'") == before["schema"]["users"][2]
    assert not any("users_new" in (sql or "") for _, _, sql in after["schema"].values())


def test_the_check_takes_the_three_roles_and_refuses_speech(at_version_1):
    database = dbmod.Database.open(at_version_1)
    try:
        for n, role in enumerate(("admin", "user", "user-jobs")):
            database.execute("INSERT INTO users (id, username, role, created_at, updated_at) "
                             "VALUES (?, ?, ?, 't', 't')", (f"u_{n}", f"new{n}", role))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            database.execute("INSERT INTO users (id, username, role, created_at, updated_at) "
                             "VALUES ('u_x', 'old', 'speech', 't', 't')")
    finally:
        database.close()


def test_foreign_keys_are_on_again_and_still_cascade(at_version_1):
    """The runner turns them off for a migration and back on after it: a
    deleted user's sessions and keys still go with the row."""
    database = dbmod.Database.open(at_version_1)
    try:
        assert database.one("PRAGMA foreign_keys")[0] == 1
        database.execute("DELETE FROM users WHERE id = ?", (SAM,))
        assert database.one("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (SAM,))[0] == 0
        assert database.one("SELECT COUNT(*) FROM api_keys WHERE user_id = ?", (SAM,))[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            database.execute("INSERT INTO sessions (id_hash, ref, user_id, created_at, "
                             "last_seen_at, expires_at, idle_expires_at) VALUES "
                             "('x', 'y', 'u_nobody', 't', 't', 't', 't')")
    finally:
        database.close()


def test_a_new_database_ends_with_the_same_schema_as_a_migrated_one(at_version_1, tmp_path):
    dbmod.Database.open(at_version_1).close()
    dbmod.Database.open(tmp_path / "fresh.db").close()
    migrated, fresh = snapshot(at_version_1), snapshot(tmp_path / "fresh.db")
    assert fresh["schema"] == migrated["schema"]
    assert fresh["columns"] == migrated["columns"]


def test_opening_a_migrated_database_again_changes_nothing(at_version_1):
    dbmod.Database.open(at_version_1).close()
    once = snapshot(at_version_1)
    dbmod.Database.open(at_version_1).close()
    assert snapshot(at_version_1) == once


# ── the runner ────────────────────────────────────────────────────────────────


def user_version(path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def runner_over(monkeypatch, tmp_path, extra: str) -> object:
    """MIGRATIONS as shipped plus one more script, 0999, holding `extra`."""
    folder = tmp_path / "migrations-extra"
    shutil.copytree(dbmod.MIGRATIONS, folder)
    (folder / "0999_test.sql").write_text(extra, encoding="utf-8")
    monkeypatch.setattr(dbmod, "MIGRATIONS", folder)
    return folder


def test_a_migration_that_fails_part_way_is_rolled_back_whole(at_version_1, monkeypatch, tmp_path):
    dbmod.Database.open(at_version_1).close()
    shipped = snapshot(at_version_1)
    version = user_version(at_version_1)
    runner_over(monkeypatch, tmp_path,
                "UPDATE users SET role = 'user' WHERE id = 'u_aaaaaaaaaaaaaaaa';\n"
                "INSERT INTO nowhere VALUES (1);\n")
    with pytest.raises(sqlite3.OperationalError, match="nowhere"):
        dbmod.Database.open(at_version_1)
    assert snapshot(at_version_1) == shipped
    assert user_version(at_version_1) == version


def test_a_migration_that_leaves_a_dangling_foreign_key_is_rolled_back(
        at_version_1, monkeypatch, tmp_path):
    """Foreign keys are off while a script runs, so the runner checks them
    itself before it commits."""
    dbmod.Database.open(at_version_1).close()
    shipped = snapshot(at_version_1)
    runner_over(monkeypatch, tmp_path, "DELETE FROM users WHERE id = 'u_aaaaaaaaaaaaaaaa';\n")
    with pytest.raises(sqlite3.IntegrityError, match="foreign key"):
        dbmod.Database.open(at_version_1)
    assert snapshot(at_version_1) == shipped
