"""What the gateway holds while it runs, and the start-up order that builds it (§2.4).

    rt = await start(Settings.from_env())
    rt.db, rt.trail, rt.lock, rt.keys, rt.throttle, rt.delegations, rt.streams
    await stop()

Both listeners share one Runtime: the internal listener checks delegation
tokens the public one issued, and a key revoked on one is revoked on both.
Nothing here touches the disk at import, so a test can import the app, walk
its routes, and never create a database.

Start-up, in the spec's order, and it never exits for a configuration fault:
every one of them is a locked-mode reason (D63):

1. open and migrate the database; load or create the identity key (an
   unreadable keyring is the one exception and raises: nothing could be
   signed, the device relay included, recheck L12);
2. mint missing service keys, write identity.pub and service_principals;
3. the Argon2id self-test;
4. removed variables, CALLIOPE_PUBLIC_ORIGIN and CALLIOPE_DEV_INSECURE_COOKIE
   against the bound address (D16), and the bootstrap value's strength while
   the bootstrap is armed (D18, recheck M-6);
5. the bootstrap check (D21-D24);
6. lock with every reason found, audit it, and remind every minute.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

from voice_common import auth as removed

from . import db as dbmod
from . import passwords, principals, secret_store, users
from .audit import CLI, Trail
from .clientaddr import Network, parse_networks
from .db import Database, iso
from .delegation import Delegations
from .lockmode import BOOTSTRAP_REASONS, Lock
from .signer import IdentityKeys
from .streams import Streams
from .throttle import Throttle

log = logging.getLogger("voice-gateway.runtime")

BOOTSTRAP_MARKER = "bootstrap_consumed"
DATABASE_FILE = "calliope.db"
SECURE_COOKIE = "__Host-calliope_session"
DEV_COOKIE = "calliope_session"


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host == "localhost"


def normalise_origin(text: str) -> str | None:
    """`scheme://host[:port]`, lower case, default port dropped; None if it is not an origin."""
    try:
        parts = urlsplit(text.strip())
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.path not in ("", "/") or parts.query
            or parts.fragment):
        return None
    host = parts.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    keys_dir: Path
    svc_dir: Path
    admin_password: str | None
    public_origin: str | None        # normalised; None when unset or not an origin
    public_origin_set: bool
    dev_insecure_cookie: bool
    bind: str
    trusted_proxies: str
    proxy_protocol: bool
    internal_port: int | None
    internal_bind: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        origin = e.get("CALLIOPE_PUBLIC_ORIGIN", "").strip()
        internal = e.get("GATEWAY_INTERNAL_PORT", "8081").strip()
        return cls(
            data_dir=Path(e.get("CALLIOPE_DATA_DIR", "/data")),
            keys_dir=Path(e.get("CALLIOPE_KEYS_DIR", "/keys")),
            svc_dir=Path(e.get("CALLIOPE_SVC_DIR", "/svc")),
            admin_password=e.get("CALLIOPE_ADMIN_PASSWORD") or None,
            public_origin=normalise_origin(origin) if origin else None,
            public_origin_set=bool(origin),
            dev_insecure_cookie=e.get("CALLIOPE_DEV_INSECURE_COOKIE", "").strip() == "1",
            bind=e.get("GATEWAY_BIND", "0.0.0.0").strip(),
            trusted_proxies=e.get("CALLIOPE_TRUSTED_PROXIES", ""),
            proxy_protocol=e.get("CALLIOPE_PROXY_PROTOCOL", "").strip() == "1",
            internal_port=int(internal) if internal else None,
            internal_bind=e.get("GATEWAY_INTERNAL_BIND", "0.0.0.0").strip())

    @cached_property
    def trusted_networks(self) -> tuple[Network, ...]:
        """CALLIOPE_TRUSTED_PROXIES, parsed once: a bad entry is logged once, not per request."""
        return parse_networks(self.trusted_proxies)

    @property
    def loopback_bind(self) -> bool:
        return _loopback(self.bind)

    @property
    def insecure_cookie(self) -> bool:
        """The dev cookie, honoured only on a loopback bind (D16); otherwise locked."""
        return self.dev_insecure_cookie and self.loopback_bind

    @property
    def cookie_name(self) -> str:
        return DEV_COOKIE if self.insecure_cookie else SECURE_COOKIE


@dataclass
class Runtime:
    settings: Settings
    db: Database
    trail: Trail
    keys: IdentityKeys
    hasher: passwords.Hasher
    lock: Lock = field(default_factory=Lock)
    throttle: Throttle = field(default_factory=Throttle)
    delegations: Delegations = field(default_factory=Delegations)
    streams: Streams = field(default_factory=Streams)
    tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def marker(self) -> Path:
        return self.settings.keys_dir / BOOTSTRAP_MARKER

    def bootstrap_armed(self) -> bool:
        """The first-access value still works: an admin with no hash, not yet consumed."""
        return (self.db.meta("bootstrap_consumed") == "0"
                and self.db.one("SELECT 1 FROM users WHERE password_hash IS NULL "
                                "AND role = 'admin' AND deleted_at IS NULL") is not None)

    def check_bootstrap(self, *, at_start: bool = False) -> None:
        """Steps 4 (the value's strength) and 5 of §2.4; also re-run every minute."""
        self.lock.clear(BOOTSTRAP_REASONS | {"admin_password_weak"})
        value = self.settings.admin_password
        if users.count(self.db) == 0:
            if self.marker.exists():
                # gateway-data was lost but calliope-keys was not: re-arming
                # the bootstrap would reopen admin to anyone who ever saw the
                # variable in docker inspect (D24).
                self.lock.add("bootstrap_data_lost")
                return
            if value is None:
                self.lock.add("bootstrap_required", "CALLIOPE_ADMIN_PASSWORD")
                return
            if passwords.problem(value, username="admin") is not None:
                self.lock.add("admin_password_weak", "CALLIOPE_ADMIN_PASSWORD")
                return
            with self.db.transaction():
                users.create(self.db, username="admin", role="admin", password_hash=None,
                             must_change=True, created_by="bootstrap")
                self.db.set_meta("bootstrap_consumed", "0")
            log.info("created the bootstrap admin; sign in as admin with "
                     "CALLIOPE_ADMIN_PASSWORD and choose a new password")
            return
        if self.bootstrap_armed():
            if value is None:
                self.lock.add("bootstrap_required", "CALLIOPE_ADMIN_PASSWORD")
            elif passwords.problem(value, username="admin") is not None:
                self.lock.add("admin_password_weak", "CALLIOPE_ADMIN_PASSWORD")
        elif value is not None and at_start:
            log.warning("CALLIOPE_ADMIN_PASSWORD is set and ignored: the first-access "
                        "password was replaced at first login. Remove it.")

    def consume_bootstrap(self, user_id: str, password_hash: str) -> bool:
        """Store the admin's first password and mark the bootstrap used, once (D22).

        Both in one transaction, after the hash is computed: consumed with no
        password stored would leave the admin with neither the bootstrap value
        nor a password of their own, and only the CLI to get back in. False
        if another session got there first (L6).
        """
        with self.db.transaction():
            cursor = self.db.execute("UPDATE meta SET value = '1' WHERE "
                                     "key = 'bootstrap_consumed' AND value = '0'")
            if cursor.rowcount != 1:
                return False
            users.set_password(self.db, user_id, password_hash)
        self.settings.keys_dir.mkdir(parents=True, exist_ok=True)
        self.marker.touch()
        return True


current: Runtime | None = None


def get() -> Runtime:
    if current is None:
        raise RuntimeError("the gateway runtime has not started")
    return current


def _check_settings(rt: Runtime) -> None:
    s = rt.settings
    for name in removed.ignored_variables():
        rt.lock.add("removed_variable", name)
    if s.public_origin_set and s.public_origin is None:
        rt.lock.add("public_origin_required", "CALLIOPE_PUBLIC_ORIGIN")
    elif not s.loopback_bind and not (s.public_origin or "").startswith("https://"):
        # Missing, or plain HTTP: cookie login is refused over plain HTTP
        # (D16), and an http:// origin would match a cleartext request and
        # hand it a session. A browser drops a Secure cookie there; curl does not.
        rt.lock.add("public_origin_required", "CALLIOPE_PUBLIC_ORIGIN")
    if s.dev_insecure_cookie and not s.loopback_bind:
        rt.lock.add("dev_insecure_cookie", "CALLIOPE_DEV_INSECURE_COOKIE")


async def _every_minute(rt: Runtime) -> None:
    ticks = 0
    while True:
        await asyncio.sleep(10)
        ticks += 1
        try:
            rt.trail.flush(before=int(dbmod.now() // 60))
            rt.keys.publish()
            if ticks % 6 == 0:
                rt.check_bootstrap()
                rt.lock.remind()
                rt.throttle.prune()
                rt.delegations.prune()
                audit_expired_keys(rt)
            if ticks % 360 == 0:
                rt.trail.sweep()
        except Exception:  # noqa: BLE001 - housekeeping must never stop
            log.exception("housekeeping failed; trying again in 10 s")


async def start(settings: Settings) -> Runtime:
    global current
    database = Database.open(settings.data_dir / DATABASE_FILE)
    keys = IdentityKeys.load(settings.keys_dir, settings.svc_dir)
    rt = Runtime(settings=settings, db=database, trail=Trail(database), keys=keys,
                 hasher=passwords.Hasher())
    rt.throttle = Throttle(unlocked_at=lambda account: _unlocked_at(database, account))
    secret_store.start(rt)   # step 1: the keyring; locks on keyring_unreadable (D8)

    principals.ensure(database, settings.svc_dir)
    keys.publish(force=True)

    if await asyncio.to_thread(rt.hasher.selftest):
        await asyncio.to_thread(rt.hasher.prepare)
    else:
        rt.lock.add("argon2_selftest")
    _check_settings(rt)
    rt.check_bootstrap(at_start=True)

    if rt.lock.active:
        for reason, variable in rt.lock.reasons:
            rt.trail.record(action="locked_mode", outcome="failed", actor=CLI,
                            target=reason, detail={"variable": variable})
        rt.lock.remind()
    audit_expired_keys(rt)
    rt.tasks.append(asyncio.create_task(_every_minute(rt)))
    current = rt
    return rt


def _unlocked_at(database: Database, account: str) -> float | None:
    value = database.meta(f"unlock.{account}")
    return float(value) if value else None


def audit_expired_keys(rt: Runtime) -> None:
    """One `key_expired` row for each key whose expiry passed since the last look (§2.1).

    Expiry is a time, not an event, so it is looked for: once at start and
    then every minute, from where the previous look stopped.
    """
    stamp = iso(dbmod.now())
    since = rt.db.meta("keys_expired_through") or ""
    for row in rt.db.all("SELECT id, user_id FROM api_keys WHERE expires_at > ? "
                         "AND expires_at <= ? AND revoked_at IS NULL", (since, stamp)):
        rt.trail.record(action="key_expired", outcome="ok", target=row["id"],
                        detail={"user": row["user_id"]})
    rt.db.set_meta("keys_expired_through", stamp)


async def stop() -> None:
    global current
    rt = current
    current = None
    if rt is None:
        return
    for task in rt.tasks:
        task.cancel()
    await asyncio.gather(*rt.tasks, return_exceptions=True)
    rt.trail.flush()
    rt.db.close()
