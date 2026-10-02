-- The roles become admin, user and user-jobs (ADR 0022). `speech` is renamed
-- `user-jobs` and holds exactly what it held; `user` is new, and is
-- user-jobs without the GPU lane and jobs. Every `speech` account becomes a
-- `user-jobs` account, so nobody loses anything they could do. Applied once,
-- in a transaction, by app/db.py, which then sets PRAGMA user_version = 2.
--
-- SQLITE CANNOT ALTER A CHECK, so the users table is rebuilt the way
-- sqlite.org/lang_altertable.html#otheralter describes: a new table with the
-- new CHECK, every row copied, the old one dropped and the new one renamed
-- into its place. app/db.py runs a migration with foreign keys off and runs
-- foreign_key_check before it commits, which are that procedure's first and
-- last steps; with them on, the DROP would cascade to every session and key.
-- Renaming users_new to users leaves the foreign keys of sessions and
-- api_keys as they are, since they name `users` and nothing else. The table
-- has no index, trigger or view of its own beyond what its columns declare,
-- and the CREATE below declares the same ones.

CREATE TABLE users_new (
  id TEXT PRIMARY KEY,                 -- 'u_' + 16 base32; stable, never the username
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT,
  role TEXT NOT NULL CHECK (role IN ('admin','user','user-jobs')),
  password_hash TEXT,                  -- PHC argon2id; NULL only for the bootstrap admin
  must_change INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  deleted_at TEXT,                     -- soft delete (D68); rows are never removed
  created_at TEXT NOT NULL, created_by TEXT,
  updated_at TEXT NOT NULL, password_changed_at TEXT,
  last_login_at TEXT, last_login_ip TEXT
);

-- updated_at is left alone: the account's rights did not change, only the
-- name of its role.
INSERT INTO users_new (id, username, display_name, role, password_hash,
                       must_change, disabled_at, deleted_at, created_at,
                       created_by, updated_at, password_changed_at,
                       last_login_at, last_login_ip)
SELECT id, username, display_name,
       CASE role WHEN 'speech' THEN 'user-jobs' ELSE role END,
       password_hash, must_change, disabled_at, deleted_at, created_at,
       created_by, updated_at, password_changed_at, last_login_at,
       last_login_ip
FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

-- A key's preset is informational, but Account and Admin › Keys show it
-- beside the key, and `speech` is no longer a preset anybody can choose.
-- The key's scopes are its own column and are not touched: the user-jobs
-- preset adds ingest:links, and a key made before keeps what it was given.
UPDATE api_keys SET preset = 'user-jobs' WHERE preset = 'speech';

-- The audit trail is left as it was written. A row from before this
-- migration that says `speech` is a true record of what the role was called.
