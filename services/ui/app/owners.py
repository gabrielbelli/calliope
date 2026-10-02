"""Whose link is whose: the person who resolved a URL, and nobody else, may use it (D36).

MeTube's queue has no owner, and the token every ingest route takes is the URL
itself, which anybody can guess. So /ui/resolve records who asked, and every
other ingest route answers 404 to anyone else: one person cannot watch,
cancel, play or transcribe another person's download by pasting the same link.

IN MEMORY, AND THAT IS A DECISION. After a restart the map is empty, and a
record MeTube still holds belongs to nobody: only a holder of `jobs:read:all`
can reach it, which is how the spec treats every record whose owner is
unknown. Persisting the map would mean a second store of who-fetched-what
that outlives the downloads it describes.

CLAIMED FIRST, KEPT ONLY ONCE THE RESOLVE SUCCEEDS. /ui/resolve claims a link
before it asks MeTube anything, so nobody else can take it meanwhile, and keeps
it when MeTube and the probe have accepted it. A claim that is released instead
cost its owner nothing: a refused link or a typo never evicts a live one.

ONE RESOLVE OF A LINK AT A TIME (`turn`). Resolving awaits MeTube for seconds,
and two people pasting one link in that window would otherwise each see the
gap between the other's claim and MeTube's record of it: the second would find
no record, decide the link was free, and take the first person's download.

BOUNDED, because anyone with `ingest:links` chooses the URLs (recheck M-4).
Each person keeps at most PER_OWNER links, so the one who pastes the most only
ever evicts their own oldest; the whole map is capped at CAPACITY, least
recently used first; and a link nobody has touched for TTL seconds is dropped.
An evicted link fails closed: its owner gets the same 404 as anyone else.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

__all__ = ["Owners", "CAPACITY", "PER_OWNER", "TTL"]

# 4096 URLs of at most 2048 characters is under 10 MB in a container limited to
# 384 MB. One person at the default 12 resolves a minute reaches PER_OWNER in
# about five minutes, so the cap binds on the person flooding, not on the rest.
CAPACITY = 4096
PER_OWNER = 64
# A day since the link was last touched. The page polls progress while a
# download runs, which touches it, so a two-hour podcast is never dropped
# mid-download; a link left alone overnight is.
TTL = 24 * 3600.0


@dataclass(slots=True)
class _Link:
    sub: str
    seen: float
    # False while the resolve that claimed it is still running.
    kept: bool = False


class Owners:
    """URL -> the `sub` that resolved it, least recently used first."""

    def __init__(self, *, capacity: int = CAPACITY, per_owner: int = PER_OWNER,
                 ttl: float = TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self.per_owner = per_owner
        self.ttl = ttl
        self.clock = clock
        self._entries: OrderedDict[str, _Link] = OrderedDict()
        # Kept links per person. A claim still being resolved is not counted,
        # so it can never push one of that person's live links out.
        self._counts: dict[str, int] = {}
        # A lock per link while a resolve of it is running or waiting, and how
        # many are. Removed with the last of them, so this holds no more
        # entries than there are requests in flight.
        self._turns: dict[str, asyncio.Lock] = {}
        self._waiting: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def owner(self, url: str) -> str | None:
        """Who resolved `url`, or None if nobody has (or it was dropped). Touches it."""
        self._expire()
        link = self._entries.get(url)
        if link is None:
            return None
        self._touch(url, link)
        return link.sub

    def claim(self, url: str, sub: str) -> str | None:
        """Hold `url` for `sub` while their resolve runs. Returns the OTHER owner, unchanged, if it has one."""
        holder = self.owner(url)
        if holder is not None:
            return None if holder == sub else holder
        while len(self._entries) >= self.capacity:
            self._drop(next(iter(self._entries)))
        self._entries[url] = _Link(sub, self.clock())
        return None

    def keep(self, url: str, sub: str) -> None:
        """`sub`'s resolve of `url` succeeded: it becomes one of their links, which may cost them their oldest.

        A claim that was dropped while the resolve ran (the map was full) is
        not put back: the link then belongs to nobody, which fails closed.
        """
        link = self._entries.get(url)
        if link is None or link.sub != sub:
            return
        self._touch(url, link)
        if link.kept:
            return
        if self._counts.get(sub, 0) >= self.per_owner:
            self._drop(next(key for key, other in self._entries.items()
                            if other.sub == sub and other.kept))
        link.kept = True
        self._counts[sub] = self._counts.get(sub, 0) + 1

    def release(self, url: str, sub: str) -> None:
        """Forget `url`, but only if `sub` owns it: nobody releases another person's link."""
        link = self._entries.get(url)
        if link is not None and link.sub == sub:
            self._drop(url)

    @contextlib.asynccontextmanager
    async def turn(self, url: str) -> AsyncIterator[None]:
        """Wait for any other resolve of `url` to finish, then run this one alone."""
        lock = self._turns.setdefault(url, asyncio.Lock())
        self._waiting[url] = self._waiting.get(url, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._waiting[url] -= 1
            if not self._waiting[url]:
                del self._waiting[url], self._turns[url]

    def _touch(self, url: str, link: _Link) -> None:
        link.seen = self.clock()
        self._entries.move_to_end(url)

    def _expire(self) -> None:
        cutoff = self.clock() - self.ttl
        while self._entries:
            url, link = next(iter(self._entries.items()))
            if link.seen > cutoff:
                return
            self._drop(url)

    def _drop(self, url: str) -> None:
        link = self._entries.pop(url)
        if not link.kept:
            return
        left = self._counts[link.sub] - 1
        if left:
            self._counts[link.sub] = left
        else:
            del self._counts[link.sub]
