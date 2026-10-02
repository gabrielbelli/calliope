"""What the hub held itself before the secret store, moved into it once (D44, D45, D62, D66).

    job = secret_import.Import(data_dir=..., targets=..., satellites=..., on_buttons=...)
    hub.spawn(job.run())                                     main.py's lifespan

WHAT IS MOVED. Everything with a value the hub could see before this release:

    secrets.json                    the keys an older hub stored from the page
    the environment                 every name an action reads (token_env, api_key_env,
                                    url_secret), SATELLITES_HA_TOKEN and SATELLITES_LLM_API_KEY
    SATELLITES_MQTT_URL             its password, as SATELLITES_MQTT_PASSWORD
    satellites.json                 every button action "webhook:https://…", as a secret_url
                                    named SATELLITES_BUTTON_<SATELLITE>_<BUTTON>_<EVENT>

Each goes to POST http://voice-gateway:8081/internal/secrets/import with the
hosts the hub's own configuration sends it to as its allowed_hosts, in batches,
the last one with "final": true, which closes the hub's import window for good
(D66; `python -m app.admin reopen-import satellites` on the gateway reopens it):

    {"secrets": [{"name": "SATELLITES_HA_TOKEN", "value": "…", "kind": "bearer",
                  "allowed_hosts": ["https://ha.lan:8123"], "source": "env SATELLITES_HA_TOKEN"}],
     "declared": {"SATELLITES_HA_TOKEN": ["https://ha.lan:8123"], "OPENROUTER_API_KEY": []},
     "final": true}

`declared` is every secret name the hub's configuration names, with the hosts
it sends each to. The gateway decides what it takes (scopes.importable), keeps
a row's hosts within those, never overwrites a name already set, and keeps
every imported row "unreviewed" until an admin confirms it: the hub's word
about its own configuration is no defence against a compromised hub, and is
not meant to be one (recheck L18).

GATHERED ONCE, AT START. What is sent, and the hosts each of the hub's own
copies may go to, are read when the hub starts and never again: a wake word
saved while the gateway is away cannot widen where a copy goes (D41).

THE HUB'S OWN COPIES LAST UNTIL THE GATEWAY ANSWERS. From the start until the
gateway answers the import, those values stand in for the store
(secret_client's `local`), so a hub started before its gateway keeps every
satellite working, and the next attempt is a minute later. Any answer ends
them: a 2xx (taken), a 410 (the window is closed) or any other 4xx, which is
a refusal that will not change by asking again (a key the gateway does not
take, secrets:import removed in a later release, a body it does not accept).
A 5xx other than that, or no answer at all, is tried again.

CONFIRMED MEANS THE HUB CAN READ IT BACK. Once the gateway has answered, each
name is asked of the store as any lookup would ask it, and only a name the
store answers for is confirmed:

  * secrets.json is overwritten with zeros, flushed to disk and removed once
    every key in it is confirmed. Nothing named secrets.json.imported is kept:
    a plaintext copy on the data volume would defeat the store's encryption;
  * a button's raw URL is replaced by webhook:secret:<NAME> in satellites.json;
  * a variable still set in the environment is named at WARNING as ignored;
  * anything the store did not take is named at WARNING, by name.

DONE IS WRITTEN DOWN. When every name has been asked, MARKER is written on the
data volume (names and outcomes only), and from then on every start skips the
import and keeps no copy at all: the environment and secrets.json are only
named as ignored. Without it, every restart would bring the copies back until
the gateway answered again. To import again after `reopen-import`, delete the
marker and restart the hub.

The import never sends a value anywhere but the gateway's internal listener,
and logs names, counts and status codes only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx
from voice_common.scopes import SECRET_NAME

from . import gateway
from . import secret_client as secrets
from .destinations import Target
from .mqtt import parse_url
from .store import write_atomic

log = logging.getLogger("voice-satellites.secrets")

PATH = "/internal/secrets/import"
LEGACY_FILE = "secrets.json"
MARKER = "secret-import.done"
RETRY_S = 60.0
BATCH = 20
BUTTON_PREFIX = "SATELLITES_BUTTON_"
MQTT_PASSWORD = "SATELLITES_MQTT_PASSWORD"
# Read whether or not an action names them: the defaults of every Home
# Assistant and language model action.
FIXED = ("SATELLITES_HA_TOKEN", "SATELLITES_LLM_API_KEY")
RAW_WEBHOOK = re.compile(r"^webhook:(https?://\S+)$")
SECRET_WEBHOOK = re.compile(r"^webhook:secret:([A-Z][A-Z0-9_]{0,63})$")

# The hub's own settings are never imported from the environment, whatever an
# action names: under the hub's prefix with no word naming a credential, they
# are its configuration (SATELLITES_MQTT_URL carries the broker's password).
# Without this, an action naming one as its key would carry it into the store
# with that action's host as its allowed host.
_PREFIXES = ("SATELLITES_", "NODES_")
_CREDENTIAL_WORDS = frozenset({"TOKEN", "KEY", "SECRET", "PASSWORD"})

# A button's secret name reads as its satellite, button and edge only when
# nothing is lost on the way: a lower-case hex id (main.satellite_id), a
# button of lower-case letters, digits and _, and an edge the firmware sends.
_PLAIN_ID = re.compile(r"[0-9a-f]+")
_PLAIN_BUTTON = re.compile(r"[a-z0-9_]+")
_EDGES = frozenset({"press", "release"})


def own_setting(name: str) -> bool:
    prefix = next((p for p in _PREFIXES if name.startswith(p)), None)
    return prefix is not None and not set(name[len(prefix):].split("_")) & _CREDENTIAL_WORDS


def button_secret(nid: str, button: str, edge: str) -> str:
    """The secret a button's webhook URL is imported as (D62): upper case,
    anything but a letter or a digit as _, at most SECRET_NAME's 64 characters.

    One name per (satellite, button, edge), always. "vol-up" and "vol_up" are
    both valid button names and read the same once upper-cased, so did a long
    name cut short; either would have sent one button's press to the other's
    webhook. So a name that loses anything on the way, or is too long, keeps
    its start and ends in a hash of the exact triple. A readable name ends in
    PRESS or RELEASE and a hashed one in eight hex digits, so they never meet."""
    name = re.sub(r"[^A-Z0-9]", "_", f"{BUTTON_PREFIX}{nid}_{button}_{edge}".upper())
    readable = bool(_PLAIN_ID.fullmatch(nid) and _PLAIN_BUTTON.fullmatch(button) and edge in _EDGES)
    if readable and len(name) <= 64:
        return name
    exact = sha256(json.dumps([nid, button, edge]).encode()).hexdigest()[:8].upper()
    return f"{name[:55]}_{exact}"


@dataclass
class Candidate:
    name: str
    value: str
    kind: str
    hosts: list[str]
    source: str
    # (satellite, button, edge) for a button's URL, so a confirmed one can be
    # rewritten to webhook:secret:<name>.
    button: tuple[str, str, str] | None = None

    def wire(self) -> dict:
        return {"name": self.name, "value": self.value, "kind": self.kind,
                "allowed_hosts": self.hosts, "source": self.source}


@dataclass
class Found:
    """Everything the hub could import, and the names its configuration uses."""

    candidates: dict[str, Candidate] = field(default_factory=dict)
    # Each name the configuration uses, with the hosts it sends that name to.
    declared: dict[str, list[str]] = field(default_factory=dict)
    legacy: set[str] = field(default_factory=set)   # the valid names in secrets.json


def read_legacy(data_dir: Path) -> dict[str, str]:
    """secrets.json as an older hub wrote it, or {}. The error is
    logged by its type: a JSON error quotes the text it choked on."""
    path = data_dir / LEGACY_FILE
    if not path.exists():
        return {}
    try:
        held = json.loads(path.read_text()).get("secrets")
        if not isinstance(held, dict):
            raise ValueError("no secrets object")
    except (OSError, ValueError, AttributeError) as e:
        log.error("%s could not be read (%s); its keys are not imported", path, type(e).__name__)
        return {}
    return {k: v for k, v in held.items()
            if isinstance(k, str) and SECRET_NAME.fullmatch(k) and isinstance(v, str)
            and secrets.SENDABLE.fullmatch(v)}


def gather(*, data_dir: Path, targets: Iterable[Target],
           satellites: Mapping[str, dict], environ: Mapping[str, str]) -> Found:
    """What to import. `targets` is every secret the wake words and
    push-to-talk name (Destination.targets); `satellites` is each adopted
    satellite's config."""
    found = Found()
    hosts: dict[str, set[str]] = {}
    url_secrets: set[str] = set()
    for target in targets:
        host = secrets.origin(target.url) if target.url else None
        hosts.setdefault(target.name, set()).update([host] if host else [])
        if target.holds_url:
            url_secrets.add(target.name)

    def add(name: str, value: str, kind: str, source: str) -> None:
        if kind == "secret_url":
            own = secrets.origin(value)
            allowed = sorted({own} if own else set())
        else:
            allowed = sorted(hosts.get(name, ()))
        found.candidates[name] = Candidate(name, value, kind, allowed, source)

    legacy = read_legacy(data_dir)
    found.legacy = set(legacy)
    for name, value in legacy.items():
        add(name, value, "secret_url" if name in url_secrets else "bearer", LEGACY_FILE)
    # The environment won over secrets.json before the store, so it does here.
    for name in sorted(set(hosts) | set(FIXED)):
        value = environ.get(name)
        if value and not own_setting(name) and SECRET_NAME.fullmatch(name):
            add(name, value, "secret_url" if name in url_secrets else "bearer", f"env {name}")

    mqtt_url = environ.get("SATELLITES_MQTT_URL")
    if mqtt_url:
        try:
            broker = parse_url(mqtt_url)
        except ValueError:
            broker = None
        own = secrets.origin(mqtt_url)
        if broker and broker["password"] and own:
            found.candidates[MQTT_PASSWORD] = Candidate(
                MQTT_PASSWORD, broker["password"], "password", [own], "SATELLITES_MQTT_URL")

    for nid, config in sorted(satellites.items()):
        for button, edges in sorted((config.get("buttons") or {}).items()):
            for edge, action in sorted((edges or {}).items()):
                raw = RAW_WEBHOOK.match(action) if isinstance(action, str) else None
                host = secrets.origin(raw.group(1)) if raw else None
                if raw and host and "@" not in raw.group(1).split("/")[2]:
                    name = button_secret(nid, button, edge)
                    found.candidates[name] = Candidate(
                        name, raw.group(1), "secret_url", [host],
                        f"button {button} {edge} of satellite {nid}", (nid, button, edge))

    for name in set(hosts) | found.legacy | set(found.candidates):
        own = found.candidates[name].hosts if name in found.candidates else ()
        found.declared[name] = sorted(hosts.get(name, set()) | set(own))
    return found


def shred(path: Path) -> None:
    """Overwrite with zeros, flush to the disk, then remove (D45)."""
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.write(b"\0" * size)
        f.flush()
        os.fsync(f.fileno())
    path.unlink()


async def post(found: Found, base_url: str) -> str:
    """"taken" (every batch answered 2xx), "closed" (any 4xx: 410 is the
    window, the rest a refusal that asking again will not change), or
    "retry" (no answer, or a 5xx)."""
    pending = sorted(found.candidates.values(), key=lambda c: c.name)
    batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)] or [[]]
    client = secrets.current().client()
    for i, batch in enumerate(batches):
        body = {"secrets": [c.wire() for c in batch], "declared": found.declared,
                "final": i == len(batches) - 1}
        try:
            answer = await gateway.request(client, "POST", base_url + PATH, json=body)
        except (gateway.NotReady, httpx.HTTPError) as e:
            log.warning("secrets: the import could not reach the gateway (%s); the hub keeps "
                        "its own copies and tries again in %.0f s", type(e).__name__, RETRY_S)
            return "retry"
        status = answer.status_code
        if 400 <= status < 500:
            if status != 410:
                log.warning("secrets: the gateway refused the import (%d); the hub gives up its "
                            "own copies all the same, and the secret store is the one source "
                            "from now on", status)
            return "closed"
        if not 200 <= status < 300:
            log.warning("secrets: the gateway answered the import with %d; the hub keeps its "
                        "own copies and tries again in %.0f s", status, RETRY_S)
            return "retry"
    return "taken"


async def confirm(names: Iterable[str]) -> tuple[set[str], set[str], bool]:
    """(stored, not stored, every name was asked): what the store answers for."""
    stored, missing, asked = set(), set(), True
    for name in sorted(names):
        answer = await secrets.current().stored(name)
        if answer is None:
            asked = False
        elif answer:
            stored.add(name)
        else:
            missing.add(name)
    return stored, missing, asked


def _ignored(candidates: Iterable[Candidate], why: str) -> None:
    """Name each variable still set where the hub once read it (D44)."""
    for c in sorted(candidates, key=lambda c: c.name):
        if c.source.startswith("env "):
            log.warning("secrets: %s is set in the hub's environment and ignored, because %s; "
                        "remove it from the hub's settings", c.name, why)
        elif c.name == MQTT_PASSWORD:
            log.warning("secrets: the password in SATELLITES_MQTT_URL is ignored, because %s "
                        "(%s); remove it from the URL", why, MQTT_PASSWORD)


class Import:
    """The hub's one import. Gathered when made; `run` tries until it is done."""

    def __init__(self, *, data_dir: Path, targets: Iterable[Target],
                 satellites: Mapping[str, dict],
                 on_buttons: Callable[[dict[tuple[str, str, str], str]], None],
                 environ: Mapping[str, str] | None = None):
        self.data_dir = data_dir
        self.on_buttons = on_buttons
        self.found = gather(data_dir=data_dir, targets=targets, satellites=satellites,
                            environ=os.environ if environ is None else environ)
        # How the gateway answered the import: None until it has.
        self.answered: str | None = None
        self.done = (data_dir / MARKER).exists()
        if self.done:
            _ignored(self.found.candidates.values(), "the import is done")
            if self.found.legacy:
                log.warning("secrets: %s is still on the data volume and is never read: the "
                            "import is done. Store any of its keys the secret store lacks (%s) "
                            "in Admin › Secrets, then delete the file", LEGACY_FILE,
                            ", ".join(sorted(self.found.legacy)))
            return
        secrets.current().keep_local({
            c.name: secrets.Held(c.value, secrets.origins(c.hosts), c.kind)
            for c in self.found.candidates.values()})

    async def attempt(self) -> bool:
        """One try at what is left. True when the import is done."""
        if self.done:
            return True
        if self.answered is None:
            outcome = await post(self.found, secrets.current().base_url)
            if outcome == "retry":
                return False
            # Whatever the gateway answered, it has answered: the store is the
            # one source from here, before any file is touched.
            self.answered = outcome
            secrets.current().drop_local()
        stored, missing, asked = await confirm(self.found.candidates)
        if not asked:
            return False
        self._finish(stored, missing)
        self.done = True
        return True

    def _finish(self, stored: set[str], missing: set[str]) -> None:
        rewritten = {c.button: c.name for c in self.found.candidates.values()
                     if c.button is not None and c.name in stored}
        if rewritten:
            self.on_buttons(rewritten)
        legacy = self.data_dir / LEGACY_FILE
        if self.found.legacy and self.found.legacy <= stored and legacy.exists():
            shred(legacy)
            log.info("secrets: every key in %s is in the secret store; the file is removed", legacy)
        _ignored((c for c in self.found.candidates.values() if c.name in stored),
                 "the secret store holds it")
        if missing:
            log.warning("secrets: the secret store did not take %s, so the hub no longer sends "
                        "them. Store them in Admin › Secrets; or reopen the import "
                        "(python -m app.admin reopen-import satellites), delete %s from the "
                        "hub's data volume and restart the hub", ", ".join(sorted(missing)),
                        MARKER)
        write_atomic(self.data_dir / MARKER, json.dumps({
            "done": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gateway": self.answered, "stored": sorted(stored), "missing": sorted(missing)},
            indent=2))
        log.info("secrets: the import is done (%s); the store holds %d of the hub's %d",
                 "this hub's last batch was taken" if self.answered == "taken"
                 else "the gateway closed it", len(stored), len(self.found.candidates))

    async def run(self) -> None:
        """attempt() until the import is done, a minute apart (D45). Only
        success ends it: an error nobody foresaw (a file that cannot be
        written, an answer of an odd shape) is named by its type and tried
        again, as an unreachable gateway is."""
        while not await self._attempt_quietly():
            await asyncio.sleep(RETRY_S)

    async def _attempt_quietly(self) -> bool:
        try:
            return await self.attempt()
        except Exception as e:
            log.warning("secrets: the import failed (%s); it is tried again in %.0f s",
                        type(e).__name__, RETRY_S)
            return False
