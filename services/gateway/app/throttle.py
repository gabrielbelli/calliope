"""Login throttling, in memory, keyed by the name typed whether or not it exists (D19).

    wait = throttle.check(account, ip, known_ips)   # seconds to wait, or None
    throttle.failed(account, ip)                      # True when a delay starts
    throttle.succeeded(account, ip)

Three limits, and each answers 429 with Retry-After without checking the
password, so a correct password inside a delay window is refused like any
other attempt:

1. **Per (account, IP)**: from the 5th consecutive failure, 1 s, doubling to
   15 min. Keyed by the pair so an internet guesser cannot lock `admin` out
   from the admin's own address.
2. **Per account**: more than 50 failures in 10 min from all addresses
   together lets the addresses that have not signed in to that account in the
   last 30 days make one attempt a minute between them, so a guess spread
   over a botnet slows down too. The addresses that have signed in are exempt,
   so this ceiling cannot lock the owner out either.
3. **Per IP**: 20 attempts in 10 min.

**Bounded** (recheck M-4). Every map is an LRU of at most 100k entries, and
an entry also expires when its window has passed. Evicting an entry forgets a
delay; it never locks anybody out.

**`unlock` comes from another process.** `python -m app.admin unlock <user>`
writes a timestamp to the database; the throttle reads it for the account on
each attempt and forgets every failure older than it.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from . import db as dbmod

FREE_FAILURES = 4                 # the 5th consecutive failure starts a delay
FIRST_DELAY = 1.0
MAX_DELAY = 15 * 60.0
ACCOUNT_WINDOW = 10 * 60.0
ACCOUNT_CEILING = 50
ACCOUNT_GAP = 60.0
IP_WINDOW = 10 * 60.0
IP_ATTEMPTS = 20
MAX_ENTRIES = 100_000


@dataclass
class _Pair:
    since: float
    failures: int = 0
    until: float = 0.0
    last: float = 0.0


@dataclass
class _Times:
    stamps: deque[float] = field(default_factory=deque)
    gated: float = float("-inf")   # the last attempt let through past the ceiling


class Throttle:
    def __init__(self, *, unlocked_at: Callable[[str], float | None] = lambda _: None,
                 max_entries: int = MAX_ENTRIES) -> None:
        self.unlocked_at = unlocked_at
        self.max_entries = max_entries
        self.pairs: OrderedDict[tuple[str, str], _Pair] = OrderedDict()
        self.accounts: OrderedDict[str, _Times] = OrderedDict()
        self.ips: OrderedDict[str, _Times] = OrderedDict()
        # The newest unlock already applied per account, so one costs a scan once.
        self._applied: dict[str, float] = {}

    def _bound(self, table: OrderedDict) -> None:
        while len(table) > self.max_entries:
            table.popitem(last=False)

    def _recent(self, table: OrderedDict[str, _Times], key: str, window: float,
                at: float, *, keep: int) -> _Times:
        entry = table.get(key)
        if entry is None:
            entry = table[key] = _Times(deque(maxlen=keep))
            self._bound(table)
        else:
            table.move_to_end(key)
        while entry.stamps and entry.stamps[0] <= at - window:
            entry.stamps.popleft()
        return entry

    def _forget_before_unlock(self, account: str) -> None:
        unlocked = self.unlocked_at(account)
        if unlocked is None or self._applied.get(account, float("-inf")) >= unlocked:
            return
        self._applied[account] = unlocked
        for key in [k for k, pair in self.pairs.items()
                    if k[0] == account and pair.since <= unlocked]:
            del self.pairs[key]
        times = self.accounts.get(account)
        if times is not None:
            while times.stamps and times.stamps[0] <= unlocked:
                times.stamps.popleft()

    def check(self, account: str, ip: str, known_ips: Callable[[], Iterable[str]]) -> float | None:
        """Seconds this attempt must wait, or None. Counts the attempt against the IP."""
        at = dbmod.now()
        self._forget_before_unlock(account)

        attempts = self._recent(self.ips, ip, IP_WINDOW, at, keep=IP_ATTEMPTS + 1)
        if len(attempts.stamps) >= IP_ATTEMPTS:
            return max(1.0, attempts.stamps[0] + IP_WINDOW - at)
        attempts.stamps.append(at)

        pair = self.pairs.get((account, ip))
        if pair is not None:
            self.pairs.move_to_end((account, ip))
            if pair.until > at:
                return pair.until - at

        failures = self.accounts.get(account)
        if failures is not None:
            while failures.stamps and failures.stamps[0] <= at - ACCOUNT_WINDOW:
                failures.stamps.popleft()
            if len(failures.stamps) > ACCOUNT_CEILING and ip not in set(known_ips()):
                if at - failures.gated < ACCOUNT_GAP:
                    return failures.gated + ACCOUNT_GAP - at
                failures.gated = at
        return None

    def failed(self, account: str, ip: str) -> bool:
        """Count a failure. True when this failure starts a delay (an audit event, §2.1)."""
        at = dbmod.now()
        pair = self.pairs.get((account, ip))
        if pair is None:
            pair = self.pairs[(account, ip)] = _Pair(since=at)
            self._bound(self.pairs)
        else:
            self.pairs.move_to_end((account, ip))
        pair.failures += 1
        pair.last = at
        started = False
        if pair.failures > FREE_FAILURES:
            delay = min(FIRST_DELAY * 2 ** (pair.failures - FREE_FAILURES - 1), MAX_DELAY)
            pair.until = at + delay
            started = pair.failures == FREE_FAILURES + 1
        self._recent(self.accounts, account, ACCOUNT_WINDOW, at,
                     keep=ACCOUNT_CEILING + 1).stamps.append(at)
        return started

    def succeeded(self, account: str, ip: str) -> None:
        self.pairs.pop((account, ip), None)

    def prune(self) -> None:
        """Drop entries whose windows have all passed. Called once a minute."""
        at = dbmod.now()
        for key in [k for k, p in self.pairs.items()
                    if p.until <= at and p.last <= at - MAX_DELAY]:
            del self.pairs[key]
        for table, window in ((self.accounts, ACCOUNT_WINDOW), (self.ips, IP_WINDOW)):
            for key in [k for k, t in table.items()
                        if not t.stamps or t.stamps[-1] <= at - window]:
                del table[key]
