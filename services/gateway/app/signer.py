"""The gateway's Ed25519 identity key, its rotation, and identity.pub (D4, D69).

    keys = IdentityKeys.load(keys_dir, svc_dir)
    keys.signer().assertion(audience="stt", ...)
    keys.publish()                        # identity.pub on every calliope-svc-* volume

**The private key lives in one file on `calliope-keys`**, `identity.json`
(0400): every key the gateway has held in the last half hour, PEM, with the
time each was retired. Only the gateway mounts that volume.

**Rotation never breaks a request** (D69). `python -m app.admin
rotate-identity-key` adds a key with the next kid and writes identity.pub with
both kids BEFORE the keyring, so a backend already knows the new kid when the
gateway first signs with it; backends re-read identity.pub on an unknown kid
anyway. The running gateway notices the keyring changed within a second and
signs with the new key. The previous kid leaves identity.pub two minutes later
(an assertion lives 60 s), and stops verifying the gateway's own delegation
tokens 30 minutes later, which is how long one lives (recheck L15).

**An unreadable keyring is the one fault that stops the gateway** (recheck
L12). Without the key nothing can be signed: not a user's request and not the
device relay, so locked mode could not keep the satellites up anyway. Raising
names the file; generating a fresh key over it would invalidate nothing and
hide the fault.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, load_pem_private_key)
from voice_common import identity
from voice_common.scopes import SERVICE_PRINCIPALS

from . import db as dbmod

log = logging.getLogger("voice-gateway.signer")

KEYRING_FILE = "identity.json"
PUBLISH_SECONDS = 120          # D69: the previous kid stays in identity.pub this long
DELEGATION_SECONDS = identity.DELEGATION_LIFETIME   # L15: and verifies delegations this long
RELOAD_SECONDS = 1.0


class Unreadable(Exception):
    """The keyring exists and cannot be used. The message names the file, never its contents."""


@dataclass(frozen=True)
class Entry:
    kid: str
    key: Ed25519PrivateKey
    retired_at: float | None


def _atomic_write(path: Path, text: str, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _encode(entries: list[Entry]) -> str:
    return json.dumps({"keys": [
        {"kid": e.kid, "retired_at": e.retired_at,
         "pem": e.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                    NoEncryption()).decode("ascii")}
        for e in entries]}, indent=2) + "\n"


def _decode(text: str) -> list[Entry]:
    document = json.loads(text)
    entries = []
    for item in document["keys"]:
        key = load_pem_private_key(item["pem"].encode("ascii"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an Ed25519 key")
        kid = str(item["kid"])
        identity.Signer(key, kid)   # validates the kid's shape
        retired = item.get("retired_at")
        entries.append(Entry(kid, key, None if retired is None else float(retired)))
    if sum(1 for e in entries if e.retired_at is None) != 1:
        raise ValueError("exactly one key must be current")
    return entries


class IdentityKeys:
    def __init__(self, keys_dir: Path, svc_dir: Path) -> None:
        self.path = keys_dir / KEYRING_FILE
        self.svc_dir = svc_dir
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._stamp: tuple[int, int, int] | None = None
        self._checked = float("-inf")
        self._published: dict[str, str] = {}

    @classmethod
    def load(cls, keys_dir: Path, svc_dir: Path) -> IdentityKeys:
        """Read the keyring, or create kid 1 when there is none. Raises Unreadable."""
        keys = cls(keys_dir, svc_dir)
        if not keys.path.exists():
            keys_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(keys.path, _encode([Entry("1", Ed25519PrivateKey.generate(),
                                                    None)]), 0o400)
            log.info("generated the identity signing key, kid 1")
        keys._reload(force=True)
        return keys

    def _reload(self, *, force: bool = False) -> None:
        with self._lock:
            at = dbmod.now()
            if not force and at - self._checked < RELOAD_SECONDS:
                return
            self._checked = at
            try:
                info = self.path.stat()
                stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
                if stamp == self._stamp:
                    return
                entries = _decode(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if force or not self._entries:
                    raise Unreadable(f"{self.path} cannot be read as the identity "
                                     f"keyring ({type(exc).__name__}); restore it from "
                                     "the calliope-keys backup") from None
                log.error("%s changed and cannot be read (%s); still signing with "
                          "kid %s", self.path, type(exc).__name__, self.current.kid)
                return
            self._entries, self._stamp = entries, stamp

    @property
    def current(self) -> Entry:
        return next(e for e in self._entries if e.retired_at is None)

    def signer(self) -> identity.Signer:
        self._reload()
        entry = self.current
        return identity.Signer(entry.key, entry.kid)

    def _within(self, seconds: float) -> dict[str, Ed25519PublicKey]:
        self._reload()
        at = dbmod.now()
        return {e.kid: e.key.public_key() for e in self._entries
                if e.retired_at is None or at - e.retired_at < seconds}

    def published_keys(self) -> dict[str, Ed25519PublicKey]:
        """What identity.pub holds now: the current kid, and the previous one for 2 minutes."""
        return self._within(PUBLISH_SECONDS)

    def delegation_keys(self) -> dict[str, Ed25519PublicKey]:
        """What the gateway's own delegation check accepts: previous kids for 30 minutes."""
        return self._within(DELEGATION_SECONDS)

    def publish(self, *, force: bool = False) -> None:
        """Write identity.pub to every service volume whose copy is out of date."""
        document = identity.public_key_document(self.published_keys())
        for name in SERVICE_PRINCIPALS:
            directory = self.svc_dir / name
            if not force and self._published.get(name) == document:
                continue
            directory.mkdir(parents=True, exist_ok=True)
            identity.write_public_keys(directory / identity.PUBLIC_KEYS_FILE,
                                       self.published_keys())
            self._published[name] = document

    def rotate(self) -> str:
        """Add a key with the next kid and retire the current one. Returns the new kid.

        identity.pub is written first, with both kids, so a backend knows the
        new kid before the gateway signs with it. Keys retired more than 30
        minutes ago are dropped from the keyring here, and only here: the
        running gateway never writes the file, so it cannot race this.
        """
        self._reload(force=True)
        at = dbmod.now()
        new = Entry(str(int(self.current.kid) + 1), Ed25519PrivateKey.generate(), None)
        kept = [Entry(e.kid, e.key, at if e.retired_at is None else e.retired_at)
                for e in self._entries
                if e.retired_at is None or at - e.retired_at < DELEGATION_SECONDS]
        published = {new.kid: new.key.public_key(),
                     **{e.kid: e.key.public_key() for e in kept
                        if at - (e.retired_at or at) < PUBLISH_SECONDS}}
        for name in SERVICE_PRINCIPALS:
            directory = self.svc_dir / name
            directory.mkdir(parents=True, exist_ok=True)
            identity.write_public_keys(directory / identity.PUBLIC_KEYS_FILE, published)
        _atomic_write(self.path, _encode([new, *kept]), 0o400)
        self._reload(force=True)
        return new.kid
