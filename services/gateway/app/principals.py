"""Service principals: one key per service, minted onto its own volume (D6, D7).

    ensure(db, svc_dir)                 at start: mint what is missing, record the hashes
    authenticate(db, "calliope_svc_…")  -> "satellites", or None

**The gateway writes the file and the service reads it.** Each service has a
`calliope-svc-<name>` volume, mounted read-write here at /svc/<name> and
read-only in the service at /run/calliope. A key that is missing is minted at
start, so there is no operator step and no first-use race (D7).

**The scopes come from code, every start** (D2): the row's `scopes` column is
rewritten from voice_common.scopes.SERVICE_PRINCIPALS, and authentication
reads the code, so a row edited by hand grants nothing extra.

**A service key works only on the internal listener** (D6). The public
listener refuses the prefix outright, so a key that leaked from a container
is no use from outside the compose network.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from voice_common.identity import SERVICE_KEY_FILE
from voice_common.scopes import SERVICE_PRINCIPALS

from . import db as dbmod
from .db import Database, iso
from .tokens import SERVICE_KEY_PREFIX, digest, mint, well_formed

log = logging.getLogger("voice-gateway.principals")


def _write_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 0444: the volume holds one service's key and nothing else, and the
        # service runs as its own uid, which only needs to read it.
        os.chmod(temporary, 0o444)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _record(database: Database, name: str, key: str, *, rotated: bool) -> None:
    stamp = iso(dbmod.now())
    database.execute(
        "INSERT INTO service_principals (name, key_hash, scopes, created_at, rotated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET key_hash = excluded.key_hash, "
        "scopes = excluded.scopes, rotated_at = COALESCE(excluded.rotated_at, rotated_at)",
        (name, digest(key), json.dumps(sorted(SERVICE_PRINCIPALS[name])), stamp,
         stamp if rotated else None))


def _read_key(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key if well_formed(key, SERVICE_KEY_PREFIX) else None


def ensure(database: Database, svc_dir: Path) -> None:
    """Every principal has a key file and a row that matches it."""
    for name in SERVICE_PRINCIPALS:
        path = svc_dir / name / SERVICE_KEY_FILE
        key = _read_key(path)
        if key is None:
            key = mint(SERVICE_KEY_PREFIX)
            _write_key(path, key)
            log.info("minted the service key for %s", name)
        _record(database, name, key, rotated=False)


def rotate(database: Database, svc_dir: Path, names: list[str] | None = None) -> list[str]:
    """New keys for these principals (all by default). The old ones stop working at once.

    A service re-reads service.key after a 401 from the internal listener, so
    the next call it makes carries the new key.
    """
    chosen = list(SERVICE_PRINCIPALS) if not names else names
    for name in chosen:
        if name not in SERVICE_PRINCIPALS:
            raise ValueError(f"{name!r} is not a service principal")
        key = mint(SERVICE_KEY_PREFIX)
        _write_key(svc_dir / name / SERVICE_KEY_FILE, key)
        _record(database, name, key, rotated=True)
    return chosen


def authenticate(database: Database, token: str) -> str | None:
    """The principal this key belongs to, or None."""
    if not well_formed(token, SERVICE_KEY_PREFIX):
        return None
    row = database.one("SELECT name FROM service_principals WHERE key_hash = ?",
                       (digest(token),))
    if row is None or row["name"] not in SERVICE_PRINCIPALS:
        return None
    return row["name"]
