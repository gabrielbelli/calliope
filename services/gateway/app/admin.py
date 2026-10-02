"""The operator's commands, run inside the container (D19, D24, D66, D69).

    docker exec -u 1000:1000 voice-gateway python -m app.admin reset-password <user> [--revoke-keys]
    docker exec -u 1000:1000 voice-gateway python -m app.admin unlock <user>
    docker exec -u 1000:1000 voice-gateway python -m app.admin rotate-service-keys [<service> ...]
    docker exec -u 1000:1000 voice-gateway python -m app.admin rotate-identity-key
    docker exec -u 1000:1000 voice-gateway python -m app.admin reopen-import <service>
    docker exec -u 1000:1000 voice-gateway python -m app.admin list-users

Started as root (a `docker exec` without `-u`, or the TrueNAS shell), they
drop to the owner of the keys volume first, because a key file written as root
is one the gateway cannot read and stops on at its next restart.

Anyone who can run these already controls the host, so they need no password;
each one is audited as `actor_kind=cli`. A temporary password is printed to
this terminal and nowhere else: never to the log, never to the audit.

`reset-password admin` on an empty database creates the admin, which is the
way out of locked mode `bootstrap_data_lost` (D24). The running gateway notices
within a minute.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

from voice_common.scopes import IMPORT_ALLOWLIST, SERVICE_PRINCIPALS

from . import apikeys, passwords, principals, sessions, users
from .audit import CLI, Trail
from .db import Database
from .runtime import DATABASE_FILE, Settings
from .signer import IdentityKeys


def _open(settings: Settings) -> tuple[Database, Trail]:
    database = Database.open(settings.data_dir / DATABASE_FILE)
    return database, Trail(database)


def reset_password(settings: Settings, username: str, *, revoke_keys: bool) -> str:
    """A one-time password for `username`, who must choose a new one at sign-in."""
    database, trail = _open(settings)
    temporary = secrets.token_urlsafe(18)
    phc = passwords.Hasher().hash_sync(temporary)
    user = users.by_username(database, username)
    if user is None:
        if users.count(database) or username.casefold() != "admin":
            raise SystemExit(f"no user called {username!r}; list-users shows who exists")
        with database.transaction():
            user = users.create(database, username="admin", role="admin", password_hash=phc,
                                must_change=True, created_by="cli")
            # The variable's first-access path stays shut: this admin was
            # made here, by someone at the host (D24).
            database.set_meta("bootstrap_consumed", "1")
    else:
        users.set_password(database, user["id"], phc, must_change=True)
    revoked = sessions.revoke_user(database, user["id"])
    keys = apikeys.revoke_user(database, user["id"], by="cli") if revoke_keys else []
    trail.record(action="password_reset", outcome="ok", actor=CLI, target=user["id"],
                 detail={"sessions_revoked": len(revoked), "keys_revoked": len(keys)})
    return temporary


def unlock(settings: Settings, username: str) -> None:
    database, trail = _open(settings)
    account = users.account_key(username)
    # Read by the running gateway's throttle on the next attempt (throttle.py).
    database.set_meta(f"unlock.{account}", repr(time.time()))
    user = users.by_username(database, username)
    trail.record(action="throttle_unlocked", outcome="ok", actor=CLI,
                 target=user["id"] if user else "<unknown>")


def rotate_service_keys(settings: Settings, names: list[str]) -> list[str]:
    database, trail = _open(settings)
    rotated = principals.rotate(database, settings.svc_dir, names or None)
    trail.record(action="service_keys_rotated", outcome="ok", actor=CLI,
                 detail={"services": rotated})
    return rotated


def rotate_identity_key(settings: Settings) -> str:
    database, trail = _open(settings)
    kid = IdentityKeys.load(settings.keys_dir, settings.svc_dir).rotate()
    trail.record(action="identity_key_rotated", outcome="ok", actor=CLI,
                 detail={"kid": kid})
    return kid


def reopen_import(settings: Settings, service: str) -> None:
    if service not in IMPORT_ALLOWLIST:
        raise SystemExit(f"{service!r} has no import window; one of: "
                         f"{', '.join(sorted(IMPORT_ALLOWLIST))}")
    database, trail = _open(settings)
    database.delete_meta(f"import_done.{service}")
    trail.record(action="import_reopened", outcome="ok", actor=CLI, target=service)


def list_users(settings: Settings) -> list[str]:
    database, _ = _open(settings)
    lines = []
    for row in users.listing(database):
        flags = [flag for flag, on in (("disabled", row["disabled_at"]),
                                       ("deleted", row["deleted_at"]),
                                       ("must-change", row["must_change"])) if on]
        lines.append(f"{row['id']}  {row['username']:<24} {row['role']:<7} "
                     f"{' '.join(flags)}".rstrip())
    return lines


def run_as_the_gateway(settings: Settings) -> None:
    """As root, become the user that owns the keys volume (uid 1000 in the image).

    A volume root owns means the gateway itself runs as root, so there is
    nothing to drop to; a volume not made yet the gateway makes at start.
    """
    if os.geteuid() != 0:
        return
    try:
        owner = settings.keys_dir.stat()
    except FileNotFoundError:
        return
    if owner.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(owner.st_gid)
    os.setuid(owner.st_uid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset-password", help="print a one-time password")
    reset.add_argument("username")
    reset.add_argument("--revoke-keys", action="store_true",
                       help="also revoke every API key the user holds")
    commands.add_parser("unlock", help="clear a login throttle").add_argument("username")
    rotate = commands.add_parser("rotate-service-keys", help="new service keys")
    rotate.add_argument("services", nargs="*", choices=list(SERVICE_PRINCIPALS),
                        metavar="service")
    commands.add_parser("rotate-identity-key", help="a new signing key, kid + 1")
    commands.add_parser("reopen-import", help="reopen a service's import window") \
        .add_argument("service")
    commands.add_parser("list-users", help="every account and its role")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    run_as_the_gateway(settings)

    if args.command == "reset-password":
        temporary = reset_password(settings, args.username, revoke_keys=args.revoke_keys)
        print(f"Temporary password for {args.username} (shown once; they choose a new "
              f"one at sign-in):\n\n    {temporary}\n")
    elif args.command == "unlock":
        unlock(settings, args.username)
        print(f"Unlocked {args.username}: earlier failed logins no longer delay them.")
    elif args.command == "rotate-service-keys":
        print("Rotated: " + ", ".join(rotate_service_keys(settings, args.services)))
    elif args.command == "rotate-identity-key":
        kid = rotate_identity_key(settings)
        print(f"Signing with kid {kid}; the previous kid leaves identity.pub in 2 minutes.")
    elif args.command == "reopen-import":
        reopen_import(settings, args.service)
        print(f"{args.service} may import secrets again until it sends its final batch.")
    elif args.command == "list-users":
        print("\n".join(list_users(settings)) or "No users yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
