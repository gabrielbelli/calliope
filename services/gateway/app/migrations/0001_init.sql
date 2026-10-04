-- The gateway's one database (D1): identity, sessions, keys, service
-- principals, secrets and the audit trail. Applied once, in a transaction,
-- by app/db.py, which then sets PRAGMA user_version = 1.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema facts and flags: bootstrap_consumed ('0'|'1'), installation_id,
-- keyring_source ('file'|'generated'), import_done.<service> ('1'),
-- unlock.<account> (a throttle unlock written by app.admin, D19)

CREATE TABLE users (
  id TEXT PRIMARY KEY,                 -- 'u_' + 16 base32; stable, never the username
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT,
  role TEXT NOT NULL CHECK (role IN ('admin','speech')),
  password_hash TEXT,                  -- PHC argon2id; NULL only for the bootstrap admin
  must_change INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  deleted_at TEXT,                     -- soft delete (D68); rows are never removed
  created_at TEXT NOT NULL, created_by TEXT,
  updated_at TEXT NOT NULL, password_changed_at TEXT,
  last_login_at TEXT, last_login_ip TEXT
);

CREATE TABLE sessions (
  id_hash TEXT PRIMARY KEY,            -- sha256(session id); the id itself is never stored
  ref TEXT NOT NULL UNIQUE,            -- a public name for the row: the account page and
                                       -- delegation tokens use it, never the id (D64)
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,            -- created + 30 d (+ 15 min when restricted)
  idle_expires_at TEXT NOT NULL,       -- last_seen + 7 d (written at most once a minute)
  stepup_until TEXT,                   -- +10 min after /auth/step-up
  restricted INTEGER NOT NULL DEFAULT 0, -- 1 = must-change session (D21)
  ip TEXT, user_agent TEXT, revoked_at TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE api_keys (
  id TEXT PRIMARY KEY,                 -- 'k_' + 12 base32 (shown in audit and Jobs)
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE,           -- sha256(full key)
  display TEXT NOT NULL,               -- calliope_AbCd…wxyz
  scopes TEXT NOT NULL,                -- JSON array, expanded; never a SESSION_ONLY scope (D60)
  preset TEXT,                         -- informational
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  expires_at TEXT,                     -- NULL = never (not allowed with capped scopes)
  last_used_at TEXT, last_used_ip TEXT,
  revoked_at TEXT, revoked_by TEXT
);
CREATE INDEX api_keys_user ON api_keys(user_id);

CREATE TABLE service_principals (
  name TEXT PRIMARY KEY,               -- satellites, ui, stt, tts, tts-long
  key_hash TEXT NOT NULL,
  scopes TEXT NOT NULL,                -- JSON, from code; rewritten at each start
  created_at TEXT NOT NULL, rotated_at TEXT
);

CREATE TABLE secrets (
  name TEXT PRIMARY KEY CHECK (name GLOB '[A-Z]*'),
  kind TEXT NOT NULL CHECK (kind IN ('bearer','password','secret_url')),
  description TEXT,
  ciphertext BLOB,                     -- NULL = declared but not set
  version INTEGER NOT NULL DEFAULT 0,
  consumers TEXT NOT NULL,             -- JSON ["satellites"]
  allowed_hosts TEXT NOT NULL,         -- JSON ["https://ha.lan:8123"], normalised (D41)
  imported_from TEXT,                  -- e.g. "env SATELLITES_HA_TOKEN @ satellites"
  unreviewed INTEGER NOT NULL DEFAULT 0, -- 1 = imported, not yet confirmed by an admin (D66)
  created_at TEXT, created_by TEXT,
  updated_at TEXT, updated_by TEXT,
  last_read_at TEXT, last_read_by TEXT
);

CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                    -- UTC ISO 8601
  actor_kind TEXT NOT NULL,            -- user|api_key|service|cli|anonymous
  actor_id TEXT, auth_method TEXT,     -- session|key:k_…|svc:…
  ip TEXT, action TEXT NOT NULL, target TEXT,
  outcome TEXT NOT NULL,               -- ok|denied|failed
  aggregated INTEGER NOT NULL DEFAULT 0, -- 1 = a per-minute count row (noise tier)
  request_id TEXT, detail TEXT         -- JSON; never values, bodies or tokens
);
CREATE INDEX audit_ts ON audit(ts);
CREATE INDEX audit_aggregated_ts ON audit(aggregated, ts);
