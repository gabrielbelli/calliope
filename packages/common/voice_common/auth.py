"""The variables of the old key scheme: named, ignored, and never fatal.

    STT_API_KEYS=k1   ->  ERROR every minute, "ignored_variables" in /health

Backends no longer check credentials. The gateway does, once, and hands each
backend a signed identity assertion that voice_common.identity verifies (D3,
D4, D52). The key middleware that used to live here is gone, and so is every
variable that configured it.

**A leftover variable is reported, not obeyed, and never stops the service**
(D63). The old rule was "set but naming no key refuses to start". Applied to a
removed variable that rule would take the hub down over a line nobody deleted
from an app config, and every satellite with it. So the service keeps serving,
logs ERROR once a minute for as long as the variable is set, and lists it in
its health body as `ignored_variables`, where the gateway's full health tier
and the Admin page show it.

Names only, always. A value is never logged and never reported: these
variables held keys.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["REMOVED_VARIABLES", "REMINDER_SECONDS", "ignored_variables",
           "watch_removed_variables"]

log = logging.getLogger("voice_common.auth")

# Each removed variable and what replaced it, which is the half of the ERROR
# line an operator can act on.
REMOVED_VARIABLES: Mapping[str, str] = MappingProxyType({
    "GATEWAY_API_KEYS": "per-user API keys created on the Account page",
    "UI_GATEWAY_API_KEY": "the session cookie and the gateway's assertion",
    "STT_API_KEYS": "the gateway's identity assertion",
    "TTS_API_KEYS": "the gateway's identity assertion",
    "SATELLITES_API_KEYS": "the gateway's identity assertion",
    "RUNLOG_KEY": "the service key the gateway writes to service.key",
})

REMINDER_SECONDS = 60.0

_watch_lock = threading.Lock()
_watching = threading.Event()


def ignored_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """The removed variables set in this environment, by name, sorted.

    Set means present, even empty: `-e STT_API_KEYS=$SECRET` with SECRET unset
    is still a line someone has to delete.
    """
    env = os.environ if environ is None else environ
    return sorted(name for name in REMOVED_VARIABLES if name in env)


def _remind() -> None:
    for name in ignored_variables():
        log.error("%s is set and ignored: this release replaced it with %s. "
                  "Remove it from this service's environment.",
                  name, REMOVED_VARIABLES[name])


def _repeat(interval: float) -> None:
    # For the life of the process: a removed variable cannot be unset without
    # a restart, and a reminder that could stop would need something to stop it.
    while True:
        time.sleep(interval)
        _remind()


def watch_removed_variables(interval: float = REMINDER_SECONDS) -> list[str]:
    """Log each removed variable now and every `interval` seconds. Returns their names.

    One daemon thread per process, started only if a variable is set, and
    re-reading the environment on every reminder. Called by
    identity.install, so no backend has to remember to.
    """
    names = ignored_variables()
    if not names:
        return names
    _remind()
    with _watch_lock:
        if not _watching.is_set():
            _watching.set()
            threading.Thread(target=_repeat, args=(interval,),
                             name="removed-variables", daemon=True).start()
    return names
