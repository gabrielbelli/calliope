"""Requests in flight, by user and credential, so revoking one ends them (D54).

    handle = streams.open(user_id, credential, task)
    streams.close(user="u_…")            # disable, delete, role change
    streams.close(credential="key:k_…")  # revoke, logout

**Why.** A credential is checked when a request arrives. An event stream (the
Satellites tab's /satellites/events) or a long download outlives that check by
as long as it runs, so a logout, a revoked key or a disabled account would not
reach it. Every authenticated request is registered here while it runs, and
the places that revoke something close what it opened.

**Event streams are also capped at 15 minutes** (the middleware arms the
timer when a response turns out to be text/event-stream), so EventSource
reconnects and the credential is checked again even when nothing is revoked.

One uvicorn worker is an invariant of this design (§7): this registry, the
login throttle and the delegation counts all live in this process's memory.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

SSE_CAP_SECONDS = 15 * 60


class Handle:
    def __init__(self, registry: Streams, keys: tuple[str, ...], task: asyncio.Future) -> None:
        self.registry = registry
        self.keys = keys
        self.task = task

    def release(self) -> None:
        for key in self.keys:
            tasks = self.registry._open.get(key)
            if tasks is not None:
                tasks.discard(self.task)
                if not tasks:
                    del self.registry._open[key]


class Streams:
    def __init__(self) -> None:
        self._open: dict[str, set[asyncio.Future]] = defaultdict(set)

    def open(self, user: str, credential: str, task: asyncio.Future) -> Handle:
        keys = (f"user:{user}", f"cred:{credential}")
        for key in keys:
            self._open[key].add(task)
        return Handle(self, keys, task)

    def close(self, *, user: str | None = None, credential: str | None = None,
              keep: str | None = None) -> int:
        """Cancel what this user or credential has open, except credential `keep`.

        Never the request doing the closing: a logout would otherwise cancel
        itself before it could answer.
        """
        keys = [f"user:{user}"] if user else []
        if credential:
            keys.append(f"cred:{credential}")
        spared = set(self._open.get(f"cred:{keep}", set())) if keep else set()
        spared.add(asyncio.current_task())
        doomed = {task for key in keys for task in self._open.get(key, ())} - spared
        for task in doomed:
            task.cancel()
        return len(doomed)
