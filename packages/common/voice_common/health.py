"""The health CONTRACT, not the payload.

Three decisions live here and nothing else does. The payloads themselves stay
per-service — they share only `status` and `threads`, and the rest is a
callable the caller supplies.

**The route is `async def`, always.** tts-long/app/main.py:176 documents why,
having paid for it: a sync route runs on AnyIO's worker pool, forty threads
shared with every other sync route in the app. tts-long's synthesis routes
hold a thread for up to TTS_OPENAI_SYNC_TIMEOUT seconds each, so forty
concurrent callers took the whole pool, `/health` stopped answering, and the
orchestrator restarted a service that was merely busy. Registering the route
here removes the choice. The consequence is that `details` must not block —
it runs on the event loop.

**The route is PATH, and PATH is what voice_common.identity leaves open.** Both
read the one constant, so a container healthcheck cannot be locked out by a
rename that looked local. It used to be two literals per repo, free to
disagree.

**What an operator must fix is in the body.** `ignored_variables` names every
removed setting still present (voice_common.auth), and a service whose
credential files the gateway has not written yet says `not_ready` (§2.4)
rather than `ok`: it would refuse every request.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI

from .auth import ignored_variables

__all__ = ["PATH", "install_health"]

PATH = "/health"

# Where identity.install leaves its middleware, so this route can ask whether
# the credential files have arrived without importing the module that needs
# `cryptography`.
IDENTITY_STATE = "voice_common_identity"

Details = Callable[[], "dict[str, Any] | Awaitable[dict[str, Any]]"]


def install_health(app: FastAPI, details: Details | None = None) -> None:
    """Register GET /health.

    `details` returns the per-service body — model_loaded, threads, queued.
    It is merged under a fixed `{"status": ...}` envelope and may override
    `status` itself, which is how a service still loading its model says so.
    It may be a coroutine function, but it must not block either way: see the
    module docstring.
    """

    @app.get(PATH)
    async def health() -> dict[str, Any]:  # noqa: D401 - the docstring is above
        payload: dict[str, Any] = {"status": "ok"}
        if details is not None:
            extra = details()
            if inspect.isawaitable(extra):
                extra = await extra
            payload.update(extra)
        guard = getattr(app.state, IDENTITY_STATE, None)
        if guard is not None and not guard.credentials.ready and payload["status"] == "ok":
            payload["status"] = "not_ready"
        # MERGED, NOT REPLACED. A service may name variables of its own that it
        # now ignores -- the hub's credentials once they live in the secret
        # store -- and the removed settings of this release are added to them.
        ignored = sorted(set(payload.get("ignored_variables") or ()) | set(ignored_variables()))
        if ignored:
            payload["ignored_variables"] = ignored
        else:
            payload.pop("ignored_variables", None)
        return payload
