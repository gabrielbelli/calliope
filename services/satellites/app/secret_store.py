"""API keys the hub holds, by name, so a key can be given from the page.

    <SATELLITES_DATA_DIR>/secrets.json, mode 0600
    {"version": 1, "secrets": {"OPENROUTER_API_KEY": "sk-or-..."}}

    secret_store.configure(SecretStore(data_dir))    main.py's lifespan
    secret_store.current().get("OPENROUTER_API_KEY") -> the value, or None
    secret_store.current().set("OPENROUTER_API_KEY", None)   clears it

AN ACTION STILL NAMES ITS KEY, AND NEVER HOLDS IT. api_key_env (and token_env)
is exactly what it was: the NAME of a secret. What changed is where the value
may live: in the process environment, as before, or here. destinations._secret
reads the environment first and this second, on every request, so a key stored
or cleared applies from the next request, with no restart and no re-save. So
wake_words.json, the models, the merge rules and every GET are unchanged, and
an action written for the environment works as it did.

WRITTEN FROM THE PAGE, NEVER READ BACK BY IT. PUT /satellites/secrets takes
{"name", "value"} in the body, never the path, because the gateway and voice-ui
log paths and not bodies. No route answers a value: GET /satellites/wake-words
says which names have one and where it lives ("environment" or "hub"), as
booleans said before. The log names the key and never its value, not even to
say it was set; a file that does not load is logged by the exception's type,
because a JSON error can quote the line it failed on.

A SEPARATE FILE, 0600. wake_words.json is answered whole by GET, so a key in
it would be one request from anyone with an API key. This file is written by
store.write_atomic with mode 0600 set before a byte goes in; one found wider
at start (a restore, a hand copy) is tightened, with a warning. It is removed
when its last key is cleared, so a hub that never stored one has none. The
cost is stated, not hidden: the data volume's backups now hold the keys. Keep
a key in the environment instead to avoid that; the environment wins, and
storing a key under a name the environment already sets is refused (main.py),
so a stored value is never silently shadowed.

WRITTEN FIRST AND TAKEN SECOND, as wakewords_config.Assignment.replace is: a
write that fails leaves the keys that were in use, in use. A SecretStore made
without a directory (the default, and every test that never configures one)
holds what it is given and writes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from .store import write_atomic

log = logging.getLogger("voice-satellites.secrets")

FILE = "secrets.json"
VERSION = 1
# Printable ASCII with no space: what a bearer token can be without breaking
# the header it travels in. A pasted key with a newline or a space around it
# is refused rather than trimmed on the hub, which never guesses at a secret;
# the page trims before it sends.
SECRET_VALUE = r"^[\x21-\x7e]{1,4096}$"
# destinations.ENV_NAME, restated because destinations imports this module;
# tests/test_secret_store.py holds the two equal.
NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
MODE = 0o600


class SecretStore:
    def __init__(self, data_dir: str | Path | None = None):
        self.path = Path(data_dir) / FILE if data_dir is not None else None
        self.load_error: str | None = None
        self._values: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & ~MODE:
                os.chmod(self.path, MODE)
                log.warning("%s was mode %03o, readable beyond the hub's own user; it is 0600 now",
                            self.path, mode)
            body = json.loads(self.path.read_text())
            held = body.get("secrets") if isinstance(body, dict) else None
            if not isinstance(held, dict):
                raise ValueError("no secrets object")
        except (OSError, ValueError) as e:
            # The type only: a JSON error quotes the text it choked on.
            self.load_error = (f"{self.path} could not be loaded ({type(e).__name__}), so no key "
                               "stored on the hub is sent until a key is stored again, which "
                               "replaces the file")
            log.error("%s", self.load_error)
            return {}
        # fullmatch, not match: Python's $ also matches before a final
        # newline, and a value ending in one would break the header it is
        # sent in. (pydantic's patterns, which SecretBody uses, are Rust's.)
        values = {}
        for name, value in held.items():
            named = isinstance(name, str) and re.fullmatch(NAME, name)
            if named and isinstance(value, str) and re.fullmatch(SECRET_VALUE, value):
                values[name] = value
            else:
                log.warning("%s: the entry %s is not a key name and a printable value, and is "
                            "ignored", self.path, name if named else "with a malformed name")
        return values

    def get(self, name: str | None) -> str | None:
        return self._values.get(name) if name else None

    def names(self) -> list[str]:
        return sorted(self._values)

    def set(self, name: str, value: str | None) -> None:
        """Store `value` under `name`, or clear it when `value` is None.
        Written first and taken second."""
        values = dict(self._values)
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
        if self.path is not None:
            if values:
                write_atomic(self.path, json.dumps({"version": VERSION, "secrets": values},
                                                   indent=2, sort_keys=True) + "\n", mode=MODE)
            else:
                self.path.unlink(missing_ok=True)
        self._values = values
        self.load_error = None


_current = SecretStore()


def configure(store: SecretStore) -> SecretStore:
    """Set the store destinations._secret reads. main.py's lifespan calls
    this, so each app start (and each test's TestClient) reads its own data
    directory."""
    global _current
    _current = store
    return store


def current() -> SecretStore:
    return _current
