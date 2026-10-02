"""Scopes, roles, key presets and service principals: code, not data.

    effective(key_scopes, role)  what an API key may do right now
    session_scopes(role)         what a signed-in session may do
    ungrantable(scopes, role)    what POST /auth/keys must refuse

**Constants, not database rows** (D2). They are reviewed with the code that
checks them, the gateway and every backend read this one copy, and adding a
scope needs no migration. The Admin › Roles view shows them read-only.

**`:all` implies `:own`, on both sides of every comparison** (D29). A role or a
key that holds `jobs:read:all` also holds `jobs:read:own`, so `expand` runs on
the key AND on the role before they are intersected. Expanding only one side
made a key with `:all` lose `:own` when the role listed only `:all`.

**Session-only scopes are subtracted when scopes are evaluated, not only when a
key is created** (D60). A key row written outside the API, by a migration or by
hand, still cannot carry `users:manage` or `keys:manage:own`: an admin key that
could manage users or mint keys was a permanent bypass of step-up, and a leaked
key could mint keys that outlived its own revocation.

**Unknown means nothing** (deny by default). An unknown role has no scopes, an
unknown service has no import allowlist, and a scope this file does not name
cannot be granted.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .auth import REMOVED_VARIABLES

__all__ = [
    "SCOPE", "SCOPES", "SESSION_ONLY", "SERVICE_ONLY", "ADMIN_ONLY",
    "EXPIRY_CAPPED", "ROLES", "PRESETS", "Preset", "SERVICE_PRINCIPALS",
    "KEY_EXPIRY_DAYS", "DEFAULT_KEY_EXPIRY_DAYS", "CAPPED_KEY_EXPIRY_DAYS",
    "ADMIN_KEY_EXPIRY_DAYS",
    "USER_ID", "KEY_ID", "SERVICE_SUB", "CREDENTIAL", "SECRET_NAME",
    "OWNER_FILTERS",
    "IMPORT_ALLOWLIST", "ImportRule",
    "valid_scope", "expand", "effective", "session_scopes", "grantable",
    "ungrantable", "needs_step_up", "max_expiry_days", "expiry_allowed",
    "presets_for", "principal", "is_system_owner", "check_owner_filter",
    "importable",
]

# The grammar of a scope (§1.5). No wildcards: a stored `*` would be a scope
# that grows every time this file does, without anyone deciding it should.
SCOPE = re.compile(r"^[a-z]+:[a-z]+(:own|:all)?$")

# Every scope there is, with what it covers, in the order the Roles view lists
# them. The text is for that view; the gateway's route table is what binds a
# scope to a path.
SCOPES: Mapping[str, str] = MappingProxyType({
    "models:read": "GET /v1/models",
    "health:read": "/health with per-service status, engines and models",
    "health:detail": "/health in full: runner, MQTT, satellite topology",
    "speech:transcribe": "transcription, translation and chat completions",
    "speech:speak": "the CPU speech lane, /speak and GET /voices",
    "speech:long": "the GPU lane: POST /jobs and long-model speech",
    "ingest:links": "link downloads: resolve, commit, progress, media",
    "jobs:read:own": "your own jobs and run records",
    "jobs:read:all": "everyone's jobs and run records, and system ones",
    "jobs:delete:own": "delete your own jobs",
    "jobs:delete:all": "delete anyone's jobs",
    "glossaries:read:own": "your own glossaries and the built-ins",
    "glossaries:read:all": "every glossary, system ones included",
    "glossaries:write:own": "write your own glossaries",
    "glossaries:write:all": "write any glossary, home-assistant included",
    "glossaries:ha": "read, write and select the home-assistant glossary",
    "voices:read": "list voice clips",
    "voices:write:own": "add and delete your own voice clips",
    "voices:write:all": "list and delete anyone's voice clips",
    "satellites:read": "list satellites, their events, firmware and artwork",
    "satellites:control": "lights, tones, speech, media and identify",
    "satellites:listen": "open a satellite's microphone",
    "satellites:update": "push an over-the-air update",
    "satellites:firmware": "upload and delete firmware images",
    "satellites:admin": "adopt, routing, wake words, telemetry, config",
    "secrets:manage": "Admin › Secrets (session only)",
    "keys:manage:own": "your own API keys (session only)",
    "keys:manage:all": "list and revoke anyone's API keys (session only)",
    "users:manage": "Admin › Users and Roles (session only)",
    "audit:read": "Admin › Audit",
    "runs:write": "post a run record (service only)",
    "secrets:fetch": "read a secret this service consumes (service only)",
    "secrets:import": "import secrets during the import window (service only)",
    "speech:delegate": "transcribe for a user with a delegation token (service only)",
})

# Never held by an API key (D60): the step-up they need exists only for a
# session, and each one could otherwise outlive the credential that used it.
SESSION_ONLY = frozenset({"users:manage", "secrets:manage",
                          "keys:manage:own", "keys:manage:all"})

# Held only by service principals on the internal listener (D6).
SERVICE_ONLY = frozenset({"runs:write", "secrets:fetch", "secrets:import",
                          "speech:delegate"})

ROLES: Mapping[str, frozenset[str]] = MappingProxyType({
    # Every non-service scope, both forms of each, session-only included: a
    # signed-in admin can do everything a person can do.
    "admin": frozenset(SCOPES) - SERVICE_ONLY,
    "speech": frozenset({
        "models:read", "health:read", "speech:transcribe", "speech:speak",
        "speech:long", "ingest:links", "jobs:read:own", "jobs:delete:own",
        "glossaries:read:own", "glossaries:write:own", "voices:read",
        "voices:write:own", "keys:manage:own",
    }),
})

# What only the admin role holds. Granting any of these to a key needs a
# step-up (D13): an admin's session left open must not quietly become a key.
ADMIN_ONLY = ROLES["admin"] - ROLES["speech"]

# What caps a key at 90 days (D28). Named explicitly rather than derived from
# ADMIN_ONLY, because the home-assistant preset holds admin-only scopes and
# must still be allowed a year: Assist breaking every 90 days is a cost the
# household pays for nothing (recheck M-5). Every `:all` is here because it
# reads or deletes other people's data. Any other admin-only scope may live a
# year but never forever: a leaked key that can push firmware must still die.
EXPIRY_CAPPED = frozenset(
    {"satellites:admin", "satellites:listen", "satellites:firmware",
     "audit:read", "health:detail"}
    | {scope for scope in SCOPES if scope.endswith(":all")})

# The expiries the page offers, in days; None is "never".
KEY_EXPIRY_DAYS = (30, 90, 365, None)
DEFAULT_KEY_EXPIRY_DAYS = 90
CAPPED_KEY_EXPIRY_DAYS = 90
ADMIN_KEY_EXPIRY_DAYS = 365


@dataclass(frozen=True)
class Preset:
    """A starting point for the scope boxes. Informational once the key exists."""

    scopes: frozenset[str]
    description: str


PRESETS: Mapping[str, Preset] = MappingProxyType({
    "admin": Preset(ROLES["admin"] - SESSION_ONLY,
                    "everything a key may hold; at most 90 days"),
    "speech": Preset(ROLES["speech"] - {"ingest:links", "keys:manage:own"},
                     "speech, jobs, glossaries and voices"),
    "transcribe-only": Preset(frozenset({"models:read", "speech:transcribe"}),
                              "transcription only"),
    # jobs:read:own because a long request answers 202 and has to be polled.
    "speak-only": Preset(frozenset({"models:read", "speech:speak", "speech:long",
                                    "jobs:read:own"}),
                         "speech synthesis only"),
    "read-only": Preset(frozenset({"models:read", "health:read", "jobs:read:own",
                                   "glossaries:read:own", "voices:read"}),
                        "read your own data"),
    "monitor": Preset(frozenset({"models:read", "health:read", "health:detail",
                                 "jobs:read:all", "glossaries:read:all",
                                 "satellites:read", "audit:read"}),
                      "dashboards and alerting"),
    "home-assistant": Preset(frozenset({"models:read", "health:read",
                                        "speech:transcribe", "speech:speak",
                                        "glossaries:ha", "satellites:read",
                                        "satellites:control",
                                        "satellites:update"}),
                             "the Home Assistant integration"),
    "firmware-release": Preset(frozenset({"satellites:read",
                                          "satellites:firmware",
                                          "satellites:update"}),
                               "upload_via_hub.py and the ship scripts"),
})

# Service principals (§1.6). They cannot log in; each holds one key file on
# its own volume. secrets:import is honoured only inside the import window.
# The hub holds glossaries:ha because it transcribes Assist commands with the
# home-assistant profile, which stt gives only to that scope (D34).
SERVICE_PRINCIPALS: Mapping[str, frozenset[str]] = MappingProxyType({
    "satellites": frozenset({"speech:transcribe", "speech:speak", "health:read",
                             "glossaries:ha", "secrets:fetch", "secrets:import"}),
    # The spec calls this `delegate`; it is `speech:delegate` here so that it
    # obeys the grammar every other scope does, and says what it delegates.
    "ui": frozenset({"speech:delegate"}),
    "stt": frozenset({"runs:write"}),
    "tts": frozenset({"runs:write"}),
    "tts-long": frozenset({"secrets:fetch", "secrets:import"}),
})

# Identifiers, checked before any is used, including in a path join (D32).
USER_ID = re.compile(r"^u_[a-z2-7]{16}$")
KEY_ID = re.compile(r"^k_[a-z2-7]{12}$")
SERVICE_SUB = re.compile(r"^svc:[a-z][a-z0-9-]{0,31}$")
# How a request authenticated, as an assertion's `cred` and a record's
# `credential`: a session, an API key's ID, or the service principal itself.
CREDENTIAL = re.compile(r"^(session|k_[a-z2-7]{12}|svc:[a-z][a-z0-9-]{0,31})$")

# ?owner= on the jobs, glossary and clip routes, besides a user ID.
OWNER_FILTERS = ("me", "all", "system")

# A secret's name keeps the environment-variable shape (D38), so wake-word
# actions that name one need no migration.
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def valid_scope(scope: object) -> bool:
    """Does this string have the shape of a scope? Says nothing about whether it exists."""
    return isinstance(scope, str) and SCOPE.fullmatch(scope) is not None


def expand(scopes: Iterable[str]) -> frozenset[str]:
    """Each `X:all` also as `X:own`. Applied to BOTH sides before intersecting."""
    given = frozenset(scopes)
    return given | {scope[:-len("all")] + "own" for scope in given
                     if scope.endswith(":all")}


def session_scopes(role: str) -> frozenset[str]:
    """What a session of this role holds. Session-only scopes included; unknown role, none."""
    return expand(ROLES.get(role, frozenset()))


def effective(key_scopes: Iterable[str], role: str) -> frozenset[str]:
    """What an API key may do now: the key ∩ the owner's CURRENT role − SESSION_ONLY (D29).

    Evaluated on every request, so a demotion narrows existing keys at once.
    The caller checks that the owner is active; a disabled owner's keys hold
    nothing whatever this returns.
    """
    return (expand(key_scopes) & session_scopes(role)) - SESSION_ONLY


def grantable(role: str) -> frozenset[str]:
    """Every scope a key owned by this role may be created with."""
    return session_scopes(role) - SESSION_ONLY - SERVICE_ONLY


def ungrantable(scopes: Iterable[str], role: str) -> frozenset[str]:
    """The requested scopes POST /auth/keys must refuse (400 scope_not_grantable)."""
    return frozenset(scopes) - grantable(role)


def needs_step_up(scopes: Iterable[str]) -> bool:
    """Does creating a key with these scopes need a fresh password (D13)?"""
    return bool(expand(scopes) & ADMIN_ONLY)


def max_expiry_days(scopes: Iterable[str]) -> int | None:
    """The longest a key with these scopes may live, in days; None means no limit."""
    expanded = expand(scopes)
    if expanded & EXPIRY_CAPPED:
        return CAPPED_KEY_EXPIRY_DAYS
    return ADMIN_KEY_EXPIRY_DAYS if expanded & ADMIN_ONLY else None


def expiry_allowed(scopes: Iterable[str], days: int | None) -> bool:
    """Is this expiry one the page offers, and within the cap these scopes carry?"""
    if days not in KEY_EXPIRY_DAYS:
        return False
    cap = max_expiry_days(scopes)
    return cap is None or (days is not None and days <= cap)


def presets_for(role: str) -> tuple[str, ...]:
    """The presets a user of this role can create a key from, in display order."""
    allowed = grantable(role)
    return tuple(name for name, preset in PRESETS.items()
                 if preset.scopes <= allowed)


def principal(name: str) -> str:
    """The `sub` of a service principal, the form run records and jobs store as owner."""
    if name not in SERVICE_PRINCIPALS:
        raise ValueError(f"{name!r} is not a service principal")
    return f"svc:{name}"


def is_system_owner(owner: object) -> bool:
    """Is a record with this owner a system record (D32)?

    No owner (every record written before ownership existed) and any service
    owner are system: visible only to holders of the `:all` scope.
    """
    return owner is None or (isinstance(owner, str) and owner.startswith("svc:"))


def check_owner_filter(value: str) -> str:
    """A `?owner=` value, validated before it is used anywhere (D32).

    Raises ValueError on anything but `me`, `all`, `system` or a user ID, so
    `../x` never reaches a path join; the caller answers 400.
    """
    if value in OWNER_FILTERS or USER_ID.fullmatch(value):
        return value
    raise ValueError("owner must be me, all, system or a user ID")


@dataclass(frozen=True)
class ImportRule:
    """Which secret names one service may import during its window (D66).

    `declared` admits the names the service reports finding in its own
    configuration (secrets.json, api_key_env, token_env, url_secret). That
    list comes from the service, so it is no defence against a compromised
    one: the real controls are the window closing and every imported row
    staying `unreviewed` until an admin confirms it (recheck L18).
    """

    names: frozenset[str]
    prefixes: tuple[str, ...] = ()
    declared: bool = False


IMPORT_ALLOWLIST: Mapping[str, ImportRule] = MappingProxyType({
    "satellites": ImportRule(
        names=frozenset({"SATELLITES_HA_TOKEN", "SATELLITES_LLM_API_KEY",
                         "SATELLITES_MQTT_PASSWORD"}),
        prefixes=("SATELLITES_BUTTON_",),
        declared=True),
    "tts-long": ImportRule(names=frozenset({"TTS_RUNNER_API_KEY"})),
})

# Never importable by anyone: the gateway's own settings, and the variables
# this release removed, which are credentials of a scheme that no longer exists.
_RESERVED_PREFIXES = ("CALLIOPE_",)


def importable(service: str, name: str, declared: Collection[str] = ()) -> bool:
    """May `service` import a secret called `name`? Unknown service or odd name: no."""
    rule = IMPORT_ALLOWLIST.get(service)
    if rule is None or not SECRET_NAME.fullmatch(name):
        return False
    if name.startswith(_RESERVED_PREFIXES) or name in REMOVED_VARIABLES:
        return False
    # A name another service imports by fixed rule is that service's alone, so
    # a compromised hub cannot pre-plant the GPU runner's key.
    if any(name in other.names for owner, other in IMPORT_ALLOWLIST.items()
           if owner != service):
        return False
    if name in rule.names or name.startswith(rule.prefixes):
        return True
    return rule.declared and name in declared
