"""The secret store: one encrypted table, written by admins, read by the services (D38-D47, D66).

    store = secret_store.start(rt)        # step 1 of §2.4; locks on keyring_unreadable
    store.put("SATELLITES_HA_TOKEN", value=..., kind="bearer", consumers=["satellites"], ...)
    store.reveal(row)                     # for GET /internal/secrets/{name} only
    store.import_batch("satellites", items, declared, final=True)

**The key never sits beside the ciphertext** (D8). The keyring comes from
CALLIOPE_MASTER_KEY_FILE, a host file with one Fernet key per line, the first
one current. Without it the gateway generates `master.keys` on calliope-keys,
a volume only the gateway mounts and never the one gateway-data is on, and
says so at every start: that file needs a backup of its own.

**The keyring's source is sticky.** Once a file has been used, the gateway
never generates a key again: a variable that is set and unreadable, or that
was set before and is not now, is locked mode `keyring_unreadable` (D63).
Generating instead would encrypt new secrets under a key the operator does
not hold, and every stored secret would stay undecryptable. The other way is
allowed: a gateway that generated its keyring may be given a file later, and
the start-up pass moves every row the generated keys can open under the
file's first key.

**Each value is bound to its name** (D39). The plaintext is
`{"n": name, "v": value}`, so a ciphertext copied from one row into another
decrypts to the wrong name and is refused, not served.

**Rotation logs nobody out** (D43). Passwords, keys and sessions are hashes;
only these rows are encrypted. At every start, any row not under the
keyring's first key is re-encrypted under it in one transaction and the count
is logged. For a generated keyring the Rotate button does the three steps the
operator does by hand for a file: add a key in front, re-encrypt, drop the old
line. A suspected leak of the key itself also needs the secret VALUES
rotated: old database backups were encrypted under the old key (recheck L14).

**Write-only** (D40). Nothing here returns a value except `reveal`, which
only the internal fetch route calls, for a listed consumer. The listing says
whether a value is set and whether it still decrypts, and nothing about the
value: no prefix, suffix, length or hash.
"""

from __future__ import annotations

import binascii
import json
import logging
import os
import re
import tempfile
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from voice_common import scopes as scope_rules
from voice_common.origins import normalise as normalise_host
from voice_common.scopes import SECRET_NAME, SERVICE_PRINCIPALS

from . import db as dbmod
from .audit import CLI, WINDOW_SECONDS, Trail
from .db import Database, iso

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger("voice-gateway.secrets")

MASTER_KEY_VARIABLE = "CALLIOPE_MASTER_KEY_FILE"
GENERATED_KEYRING = "master.keys"
KINDS = ("bearer", "password", "secret_url")
# What a person may store. A webhook URL or an API key is well under this;
# the cap only stops a pasted file from becoming a row.
MAX_VALUE = 8192
MAX_DESCRIPTION = 200
MAX_HOSTS = 16
# The whole table. A service may import names under a prefix during its
# window (SATELLITES_BUTTON_*), so without a ceiling a compromised one could
# grow the table, and the audit with it, without bound.
MAX_SECRETS = 1000
# D42: consumers cache a value this long and never cache a miss.
MAX_AGE = 60
# A fetch is a consumer's cache miss, about once a minute per secret while it
# is in use, and a consumer refused an undecryptable row asks as often. One
# security event per secret, version and consumer per hour says who was given
# which value, or failed to be, without filling the year-long tier that M-3
# caps; last_read_at and last_read_by record every fetch.
REPEAT_AUDIT_SECONDS = 3600
# Distinct names refused to one service, for one action and reason, that are
# each a security event in a minute (§2.1). The names are the forensic signal;
# past this, a flood of invented ones is counted in the minute's aggregated
# row and not listed (recheck M-3).
NAMED_REFUSALS = 20
CERTIFICATE_WARNING_SECONDS = 14 * 24 * 3600

# Only a principal that can fetch is a consumer worth naming: listing any
# other would read as access it does not have.
CONSUMERS = frozenset(name for name, granted in SERVICE_PRINCIPALS.items()
                      if "secrets:fetch" in granted)

# Where an imported value came from, as a service reports it: the page turns
# `env X @ service` into "imported from env X on service: remove it" (D44).
SOURCE = re.compile(r"^(?:(?:env|file) [A-Z][A-Z0-9_]{0,63}|secrets\.json|config)$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_KEY_ID = re.compile(r"^[0-9a-f]{8,64}$")


class KeyringUnreadable(Exception):
    """The keyring cannot be used. The message names the file, never what is in it."""


class KeyringMissing(KeyringUnreadable):
    """The file is not there at all: the one case in which a generated keyring may be made."""


class Undecryptable(Exception):
    """A row that no key in the keyring opens, or that opens to another row's name."""


class WindowClosed(Exception):
    """The service sent its final batch; only `app.admin reopen-import` reopens it (D66)."""


class Invalid(ValueError):
    """A request field the store refuses. `code` is the API error code; no value is echoed."""

    def __init__(self, code: str, message: str, param: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param


# ── allowed hosts (D41) ───────────────────────────────────────────────────────


def normalise_hosts(entries: Iterable[str]) -> list[str]:
    """Every entry normalised, de-duplicated and sorted. Raises Invalid naming the bad index."""
    found: set[str] = set()
    for index, entry in enumerate(entries):
        try:
            found.add(normalise_host(entry))
        except ValueError:
            raise Invalid("invalid_host", f"allowed_hosts[{index}] is not a host: use "
                          "scheme://host:port, or host[:port] for https.",
                          "allowed_hosts") from None
    if len(found) > MAX_HOSTS:
        raise Invalid("invalid_host", f"At most {MAX_HOSTS} allowed hosts.", "allowed_hosts")
    return sorted(found)


def _valid_hosts(entries: Iterable[str]) -> set[str]:
    """The entries that normalise; the rest are dropped. For imports, which never fail on one."""
    kept = set()
    for entry in entries:
        try:
            kept.add(normalise_host(entry))
        except ValueError:
            continue
    return kept


# ── values ────────────────────────────────────────────────────────────────────


def value_problem(kind: str, value: str) -> str | None:
    """Why a value cannot be stored, as a phrase that never repeats any of it."""
    if not value:
        return "The value is empty."
    if len(value) > MAX_VALUE:
        return f"The value is longer than {MAX_VALUE} characters."
    if _CONTROL.search(value):
        # A header value with CR or LF in it is a second header at the consumer.
        return "The value contains a control character."
    if kind == "secret_url":
        # A webhook is posted over HTTP, so a broker or socket address is not one.
        try:
            place = normalise_host(value, entry=False)
        except ValueError:
            place = ""
        if not place.startswith(("http://", "https://")):
            return "A secret_url value is an absolute http or https URL."
    return None


def _clean_text(text: str | None, limit: int) -> str | None:
    """A description: shown in the admin view, so one line and bounded (recheck L9)."""
    if text is None:
        return None
    cleaned = _CONTROL.sub(" ", text).strip()[:limit]
    return cleaned or None


def _consumers(names: Iterable[str]) -> list[str]:
    chosen = sorted(set(names))
    unknown = [name for name in chosen if name not in CONSUMERS]
    if unknown:
        raise Invalid("invalid_consumer", "consumers must be among: "
                      + ", ".join(sorted(CONSUMERS)) + ".", "consumers")
    return chosen


# ── the keyring file ──────────────────────────────────────────────────────────


def read_keyring(path: Path, label: str) -> list[bytes]:
    """Every key in a keyring file, current first. Raises KeyringUnreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyringMissing(f"{label} names {path}, which does not exist") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyringUnreadable(f"{label} names {path}, which cannot be read "
                                f"({type(exc).__name__})") from None
    keys = []
    for number, line in enumerate(text.splitlines(), 1):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            Fernet(candidate.encode("ascii"))
        except (ValueError, binascii.Error):
            # The line number and nothing of the line: it may be a key with
            # one character wrong.
            raise KeyringUnreadable(f"line {number} of {path} ({label}) is not a Fernet "
                                    "key: 32 bytes, url-safe base64") from None
        keys.append(candidate.encode("ascii"))
    if not keys:
        raise KeyringUnreadable(f"{label} names {path}, which holds no key")
    return keys


def write_keyring(path: Path, keys: Iterable[bytes]) -> None:
    """Replace the generated keyring in one step: temp file, fsync, rename, fsync the directory.

    A crash at any point leaves either the old file or the new one, never a
    half-written keyring that would lock the gateway (recheck L14).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"".join(key + b"\n" for key in keys))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# ── the store ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportItem:
    name: str
    kind: str
    value: str
    allowed_hosts: tuple[str, ...] = ()
    source: str | None = None


@dataclass(frozen=True)
class Imported:
    """One name's outcome. `name` is None when what was sent was not a name at all."""

    name: str | None
    outcome: str                  # imported | exists | refused
    reason: str | None = None     # for refused: invalid_name, not_allowed, invalid_kind, …
    hosts_dropped: int = 0


class SecretStore:
    def __init__(self, database: Database, trail: Trail, keys_dir: Path) -> None:
        self.db = database
        self.trail = trail
        self.keys_dir = keys_dir
        self.source: str | None = None      # file | generated; None while unavailable
        self._keys: tuple[bytes, ...] = ()
        self._multi: MultiFernet | None = None
        # What this process has already written a security event about, so a
        # consumer asking again and again is audited by the rules above, not
        # per request. Both are bounded: by the rows and consumers, and by the
        # few (action, service, reason) triples, each holding NAMED_REFUSALS.
        self._audited: dict[tuple[Any, ...], float] = {}
        self._named: dict[tuple[str, str, str], tuple[int, set[str | None]]] = {}

    @property
    def available(self) -> bool:
        return self._multi is not None

    @property
    def generated_path(self) -> Path:
        return self.keys_dir / GENERATED_KEYRING

    # ── the keyring ───────────────────────────────────────────────────────────

    def _use(self, keys: Iterable[bytes]) -> None:
        self._keys = tuple(keys)
        self._multi = MultiFernet([Fernet(key) for key in self._keys])

    def open(self, env: Mapping[str, str]) -> None:
        """Load the keyring by the sticky rule (D8) and re-encrypt what needs it.

        Raises KeyringUnreadable.
        """
        variable = env.get(MASTER_KEY_VARIABLE)
        recorded = self.db.meta("keyring_source")
        moving: list[bytes] = []
        if variable is not None:
            if not variable.strip():
                # Present and blank, as an unset host variable interpolated
                # by compose or an empty form field leaves it: the operator
                # meant a file, so this is the file failing (D8), never a
                # reason to generate a key they do not hold.
                raise KeyringUnreadable(f"{MASTER_KEY_VARIABLE} is set and empty")
            keys = read_keyring(Path(variable.strip()), MASTER_KEY_VARIABLE)
            moving = self._generated_keys_to_move()
            self.source = "file"
        elif recorded == "file":
            raise KeyringUnreadable(
                f"{MASTER_KEY_VARIABLE} was used before and is not set now; set it again. "
                "The gateway never generates a key in its place: secrets stored under the "
                "file's keys would stay unreadable")
        else:
            keys = self._generated_keys()
            self.source = "generated"
            log.warning("the secret keyring was generated on calliope-keys (%s). Back that "
                        "volume up separately from gateway-data, or set %s to a host file",
                        GENERATED_KEYRING, MASTER_KEY_VARIABLE)
        self._use(keys)
        self._reencrypt(fallback=moving, at_start=True)
        if recorded != self.source:
            self.db.set_meta("keyring_source", self.source)
            if recorded is not None:
                self.trail.record(action="keyring_source_changed", outcome="ok", actor=CLI,
                                  detail={"from": recorded, "to": self.source})

    def _generated_keys(self) -> list[bytes]:
        """The generated keyring, created only when there is none: never over one that fails."""
        try:
            return read_keyring(self.generated_path, GENERATED_KEYRING)
        except KeyringMissing:
            pass
        stored = self.db.one("SELECT COUNT(*) AS n FROM secrets WHERE ciphertext IS NOT NULL")
        if stored and stored["n"]:
            # calliope-keys was lost and gateway-data was not. No key exists
            # anywhere for these rows; a new one lets an admin store them
            # again, where a lock would leave no page to do it from.
            log.error("the generated secret keyring is missing from calliope-keys: %d stored "
                      "secrets cannot be decrypted and must be stored again", stored["n"])
        key = Fernet.generate_key()
        write_keyring(self.generated_path, [key])
        return [key]

    def _generated_keys_to_move(self) -> list[bytes]:
        """A generated keyring left from before the file was set: decrypt-only, to move its rows."""
        try:
            return read_keyring(self.generated_path, GENERATED_KEYRING)
        except KeyringMissing:
            return []
        except KeyringUnreadable as exc:
            log.error("%s; rows it encrypted stay unreadable until it is fixed", exc)
            return []

    def _reencrypt(self, *, fallback: Iterable[bytes] = (), at_start: bool = False
                   ) -> tuple[int, list[str]]:
        """Every row not under the current key, re-encrypted under it in one transaction.

        A row the current key already opens is left alone, so the count logged
        is the rows that moved. `fallback` keys decrypt only.
        """
        primary = Fernet(self._keys[0])
        opener = MultiFernet([Fernet(key) for key in (*self._keys, *fallback)])
        rotated = 0
        with self.db.transaction():
            for row in self.db.all("SELECT name, ciphertext FROM secrets "
                                   "WHERE ciphertext IS NOT NULL"):
                token = bytes(row["ciphertext"])
                try:
                    primary.decrypt(token)
                    continue
                except InvalidToken:
                    pass
                try:
                    renewed = opener.rotate(token)
                except InvalidToken:
                    continue
                self.db.execute("UPDATE secrets SET ciphertext = ? WHERE name = ?",
                                (renewed, row["name"]))
                rotated += 1
        unreadable = self.undecryptable()
        if rotated:
            log.info("re-encrypted %d stored secret(s) under the keyring's first key", rotated)
            if at_start:
                self.trail.record(action="secrets_reencrypted", outcome="ok", actor=CLI,
                                  detail={"count": rotated, "source": self.source})
        if unreadable:
            log.warning("%d stored secret(s) cannot be decrypted and must be stored again: %s",
                        len(unreadable), ", ".join(unreadable))
        return rotated, unreadable

    def rotate_master(self) -> tuple[int, list[str]]:
        """Admin › Secrets › Rotate master key, for a generated keyring (D43).

        The new key goes in front first, so a crash before the re-encryption
        commits leaves a keyring that still opens every row; the old line is
        dropped only after.
        """
        if self.source != "generated":
            raise Invalid("keyring_from_file", f"The keyring is {MASTER_KEY_VARIABLE}: put a "
                          "new key on its first line, restart, then remove the old line.")
        # No await from here to the end, so on the one event loop two clicks
        # cannot interleave.
        current = Fernet.generate_key()
        write_keyring(self.generated_path, [current, *self._keys])
        self._use([current, *self._keys])
        rotated, unreadable = self._reencrypt()
        write_keyring(self.generated_path, [current])
        self._use([current])
        return rotated, unreadable

    def keyring_status(self) -> dict[str, Any]:
        """For the Secrets banners: where the keyring is, and how many lines it still holds."""
        return {"source": self.source, "variable": MASTER_KEY_VARIABLE,
                "keys": len(self._keys), "rotatable": self.source == "generated"}

    # ── sealing ───────────────────────────────────────────────────────────────

    def _seal(self, name: str, value: str) -> bytes:
        assert self._multi is not None
        return self._multi.encrypt(json.dumps({"n": name, "v": value},
                                              separators=(",", ":")).encode("utf-8"))

    def _unseal(self, name: str, token: bytes) -> str:
        assert self._multi is not None
        try:
            payload = json.loads(self._multi.decrypt(token))
        except (InvalidToken, ValueError):
            raise Undecryptable(name) from None
        if not isinstance(payload, dict) or payload.get("n") != name \
                or not isinstance(payload.get("v"), str):
            # Another row's ciphertext, copied here (D39).
            raise Undecryptable(name)
        return payload["v"]

    def reveal(self, row: Mapping[str, Any]) -> str:
        """The plaintext. Only GET /internal/secrets/{name} calls this, for a listed consumer."""
        return self._unseal(row["name"], bytes(row["ciphertext"]))

    def undecryptable(self) -> list[str]:
        names = []
        for row in self.db.all("SELECT name, ciphertext FROM secrets "
                               "WHERE ciphertext IS NOT NULL ORDER BY name"):
            try:
                self.reveal(row)
            except Undecryptable:
                names.append(row["name"])
        return names

    # ── rows ──────────────────────────────────────────────────────────────────

    def row(self, name: str) -> Any:
        """The row, or None. Raises Invalid for a name no row could have."""
        if not SECRET_NAME.fullmatch(name):
            raise Invalid("invalid_name", "A secret's name is upper case letters, digits and "
                          "underscores, starting with a letter.", "name")
        return self.db.one("SELECT * FROM secrets WHERE name = ?", (name,))

    def public(self, row: Mapping[str, Any], *, broken: Collection[str] = ()) -> dict[str, Any]:
        """A row as Admin › Secrets shows it: all but the value, and nothing derived from it."""
        return {"name": row["name"], "kind": row["kind"], "description": row["description"],
                "set": row["ciphertext"] is not None,
                "undecryptable": row["name"] in broken,
                "consumers": json.loads(row["consumers"]),
                "allowed_hosts": json.loads(row["allowed_hosts"]),
                "imported_from": row["imported_from"],
                "unreviewed": bool(row["unreviewed"]),
                "created_at": row["created_at"], "created_by": row["created_by"],
                "updated_at": row["updated_at"], "updated_by": row["updated_by"],
                "last_read_at": row["last_read_at"], "last_read_by": row["last_read_by"]}

    def listing(self) -> list[dict[str, Any]]:
        broken = set(self.undecryptable())
        return [self.public(row, broken=broken)
                for row in self.db.all("SELECT * FROM secrets ORDER BY name")]

    def _count(self) -> int:
        found = self.db.one("SELECT COUNT(*) AS n FROM secrets")
        return int(found["n"]) if found else 0

    def put(self, name: str, *, value: str, kind: str | None, description: str | None,
            consumers: Iterable[str] | None, allowed_hosts: Iterable[str] | None,
            by: str) -> tuple[dict[str, Any], bool, int]:
        """Set or replace a value: (the public row, True if created, the new version).

        Fields left out keep what the row had. `unreviewed` is left alone: an
        imported row's bindings came from the service, and only PATCH
        `reviewed` says an admin has looked at them (D66).
        """
        with self.db.transaction():
            existing = self.row(name)
            chosen_kind = kind or (existing["kind"] if existing else "bearer")
            problem = value_problem(chosen_kind, value)
            if problem:
                raise Invalid("invalid_value", problem, "value")
            hosts = normalise_hosts(allowed_hosts) if allowed_hosts is not None else None
            readers = _consumers(consumers) if consumers is not None else None
            stamp = iso(dbmod.now())
            token = self._seal(name, value)
            if existing is None:
                if self._count() >= MAX_SECRETS:
                    raise Invalid("store_full", f"The store holds {MAX_SECRETS} secrets, "
                                  "its limit.")
                self.db.execute(
                    "INSERT INTO secrets (name, kind, description, ciphertext, version, "
                    "consumers, allowed_hosts, created_at, created_by, updated_at, "
                    "updated_by) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (name, chosen_kind, _clean_text(description, MAX_DESCRIPTION), token,
                     json.dumps(readers or []), json.dumps(hosts or []), stamp, by,
                     stamp, by))
            else:
                self.db.execute(
                    "UPDATE secrets SET kind = ?, ciphertext = ?, version = version + 1, "
                    "description = COALESCE(?, description), "
                    "consumers = COALESCE(?, consumers), "
                    "allowed_hosts = COALESCE(?, allowed_hosts), "
                    "updated_at = ?, updated_by = ? WHERE name = ?",
                    (chosen_kind, token, _clean_text(description, MAX_DESCRIPTION),
                     json.dumps(readers) if readers is not None else None,
                     json.dumps(hosts) if hosts is not None else None, stamp, by, name))
            stored = self.row(name)
            return self.public(stored), existing is None, int(stored["version"])

    def clear(self, name: str, *, by: str) -> bool:
        """Forget the value and keep the row: its bindings, and who set it, stay. False if no row.

        The version moves, so a consumer that sees the value again later
        knows it is a new one; a fetch answers 404 at once, and consumers
        never cache a miss (D42).
        """
        if self.row(name) is None:
            return False
        self.db.execute("UPDATE secrets SET ciphertext = NULL, version = version + 1, "
                        "updated_at = ?, updated_by = ? WHERE name = ?",
                        (iso(dbmod.now()), by, name))
        return True

    def change(self, name: str, *, description: str | None, consumers: Iterable[str] | None,
               allowed_hosts: Iterable[str] | None, reviewed: bool, by: str
               ) -> dict[str, Any] | None:
        """Bindings and description, or confirming an imported row (D66). None if no row."""
        with self.db.transaction():
            if self.row(name) is None:
                return None
            hosts = normalise_hosts(allowed_hosts) if allowed_hosts is not None else None
            readers = _consumers(consumers) if consumers is not None else None
            self.db.execute(
                "UPDATE secrets SET description = CASE WHEN ? THEN ? ELSE description END, "
                "consumers = COALESCE(?, consumers), "
                "allowed_hosts = COALESCE(?, allowed_hosts), "
                "unreviewed = CASE WHEN ? THEN 0 ELSE unreviewed END, "
                "updated_at = ?, updated_by = ? WHERE name = ?",
                (description is not None, _clean_text(description, MAX_DESCRIPTION),
                 json.dumps(readers) if readers is not None else None,
                 json.dumps(hosts) if hosts is not None else None, reviewed,
                 iso(dbmod.now()), by, name))
            return self.public(self.row(name))

    # ── reads, and what is worth auditing ─────────────────────────────────────

    def mark_read(self, name: str, version: int, by: str) -> bool:
        """Record a fetch on the row. True when it is worth a security event."""
        self.db.execute("UPDATE secrets SET last_read_at = ?, last_read_by = ? WHERE name = ?",
                        (iso(dbmod.now()), by, name))
        return self.first_this_hour("fetched", name, version, by)

    def first_this_hour(self, *about: Any) -> bool:
        """True the first time `about` is seen in REPEAT_AUDIT_SECONDS."""
        at = dbmod.now()
        for stale in [key for key, seen in self._audited.items()
                      if at - seen >= REPEAT_AUDIT_SECONDS]:
            del self._audited[stale]
        if about in self._audited:
            return False
        self._audited[about] = at
        return True

    def worth_naming(self, refusal: tuple[str, str, str], target: str | None) -> bool:
        """Is this refusal a security event of its own? `refusal` is (action, principal, reason).

        True for each distinct target the first time it is refused in a
        minute, up to NAMED_REFUSALS of them (§2.1, recheck M-3).
        """
        window = int(dbmod.now() // WINDOW_SECONDS)
        seen = self._named.get(refusal)
        if seen is None or seen[0] != window:
            seen = self._named[refusal] = (window, set())
        names = seen[1]
        if target in names or len(names) >= NAMED_REFUSALS:
            return False
        names.add(target)
        return True

    # ── the import window (D44, D66) ──────────────────────────────────────────

    def import_batch(self, service: str, items: Iterable[ImportItem],
                     declared: Mapping[str, Iterable[str]], *, final: bool) -> list[Imported]:
        """Store each name that is allowed and not there yet; never touch one that is.

        `declared` is what the service found in its own configuration: each
        secret name an action or setting refers to, with the hosts of those
        actions. A row's allowed_hosts is what was asked for, within those
        hosts, and for a secret_url within the URL's own host. All of it comes
        from the service, so this is a consistency check, not a defence
        against a compromised one: the controls that hold then are the window
        closing and every imported row staying `unreviewed` until an admin
        confirms it (recheck L18).
        """
        outcomes: list[Imported] = []
        with self.db.transaction():
            if self.db.meta(f"import_done.{service}") == "1":
                raise WindowClosed(service)
            room = MAX_SECRETS - self._count()
            stamp = iso(dbmod.now())
            for item in items:
                refused = self._import_refusal(service, item, declared)
                if refused is not None:
                    outcomes.append(refused)
                    continue
                if room <= 0:
                    outcomes.append(Imported(item.name, "refused", "store_full"))
                    continue
                hosts = _valid_hosts(item.allowed_hosts) \
                    & _valid_hosts(declared.get(item.name, ()))
                if item.kind == "secret_url":
                    hosts &= {normalise_host(item.value, entry=False)}
                stored = sorted(hosts)[:MAX_HOSTS]
                source = item.source if item.source and SOURCE.fullmatch(item.source) \
                    else "import"
                cursor = self.db.execute(
                    "INSERT INTO secrets (name, kind, ciphertext, version, consumers, "
                    "allowed_hosts, imported_from, unreviewed, created_at, created_by, "
                    "updated_at, updated_by) VALUES (?, ?, ?, 1, ?, ?, ?, 1, ?, ?, ?, ?) "
                    "ON CONFLICT(name) DO NOTHING",
                    (item.name, item.kind, self._seal(item.name, item.value),
                     json.dumps([service]), json.dumps(stored),
                     f"{source} @ {service}", stamp, scope_rules.principal(service),
                     stamp, scope_rules.principal(service)))
                if cursor.rowcount == 1:
                    room -= 1
                    outcomes.append(Imported(item.name, "imported",
                                             hosts_dropped=len(set(item.allowed_hosts))
                                             - len(stored)))
                else:
                    # Set, cleared or declared by an admin: the store wins (D44).
                    outcomes.append(Imported(item.name, "exists"))
            if final:
                self.db.set_meta(f"import_done.{service}", "1")
        return outcomes

    @staticmethod
    def _import_refusal(service: str, item: ImportItem,
                        declared: Mapping[str, Iterable[str]]) -> Imported | None:
        if not SECRET_NAME.fullmatch(item.name):
            return Imported(None, "refused", "invalid_name")
        if not scope_rules.importable(service, item.name, declared):
            return Imported(item.name, "refused", "not_allowed")
        if item.kind not in KINDS:
            return Imported(item.name, "refused", "invalid_kind")
        if value_problem(item.kind, item.value) is not None:
            return Imported(item.name, "refused", "invalid_value")
        return None


# ── status rows (D46) ─────────────────────────────────────────────────────────


def status_rows(probes: Mapping[str, Any], env: Mapping[str, str]) -> list[dict[str, Any]]:
    """What stays out of the store, shown read-only: rotated elsewhere, or must not sign.

    The certificate is the gateway's own file. The runner key file and the
    firmware key are another service's, so they come from that service's
    /health body (the raw probe, never shown elsewhere): `runner_key_file`
    from tts-long, `firmware_key_id` from the hub. A body without the field
    gives `unknown`, not a guess.
    """
    return [_certificate(env.get("GATEWAY_TLS_CERT", "").strip()),
            _runner_key_file(_health(probes, "tts_long")),
            _firmware_key(_health(probes, "satellites"))]


def _health(probes: Mapping[str, Any], backend: str) -> Mapping[str, Any]:
    probe = probes.get(backend)
    body = probe.get("health") if isinstance(probe, Mapping) else None
    return body if isinstance(body, Mapping) else {}


def _certificate(path: str) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "tls_certificate", "label": "TLS certificate"}
    if not path:
        return {**row, "state": "not_configured"}
    try:
        certificate = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError):
        return {**row, "state": "unreadable"}
    expires = certificate.not_valid_after_utc.timestamp()
    left = expires - dbmod.now()
    state = "expired" if left <= 0 else \
        "expiring" if left < CERTIFICATE_WARNING_SECONDS else "ok"
    return {**row, "state": state, "expires_at": iso(expires)}


def _runner_key_file(health: Mapping[str, Any]) -> dict[str, Any]:
    present = health.get("runner_key_file")
    state = ("present" if present else "absent") if isinstance(present, bool) else "unknown"
    return {"id": "runner_key_file", "label": "GPU runner key file (tts-long)",
            "state": state}


def _firmware_key(health: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "firmware_signing_key", "label": "Firmware signing key"}
    if "firmware_key_id" not in health:
        return {**row, "state": "unknown"}
    key_id = health["firmware_key_id"]
    if key_id is None:
        return {**row, "state": "not_configured"}
    if isinstance(key_id, str) and _KEY_ID.fullmatch(key_id):
        return {**row, "state": "configured", "key_id": key_id}
    return {**row, "state": "unknown"}


# ── the running store ─────────────────────────────────────────────────────────

current: SecretStore | None = None


def start(rt: Runtime) -> SecretStore:
    """Step 1 of §2.4: open the keyring, or lock with keyring_unreadable; never raise for it.

    Locked, the store stays closed: /internal/secrets/* answers 503 and
    consumers keep their last good value (D42), while :8081 goes on serving
    everything else.
    """
    global current
    store = SecretStore(rt.db, rt.trail, rt.settings.keys_dir)
    try:
        store.open(os.environ)
    except KeyringUnreadable as exc:
        log.error("%s. The secret store is closed until it is fixed and the gateway "
                  "restarted", exc)
        rt.lock.add("keyring_unreadable", MASTER_KEY_VARIABLE)
    current = store
    return store


def get() -> SecretStore:
    if current is None:
        raise RuntimeError("the secret store has not started")
    return current
