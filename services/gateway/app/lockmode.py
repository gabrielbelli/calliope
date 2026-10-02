"""Locked mode: start anyway, keep the satellites up, refuse people (D63).

    lock.add("bootstrap_required", "CALLIOPE_ADMIN_PASSWORD")
    if lock.active: return render(errors.locked(*lock.primary))

**This replaces every "refuse to start".** A leftover GATEWAY_API_KEYS, a
failed Argon2 self-test or a missing public origin used to be a reason to
exit, and an exit takes the device relay and the internal listener with it:
the hub reaches stt and tts only through :8081, so every satellite would go
quiet over a line nobody deleted from an app config.

So the gateway starts. /health answers (`degraded`), the device socket is
relayed, :8081 serves service keys as normal, and every other route on :8080
answers 503 `locked` with the reason and the NAME of the variable to fix,
never its value. An ERROR is logged every minute and one audit row is written
when the lock is entered.

The bootstrap reasons are looked at again every minute, so running
`python -m app.admin reset-password admin` lifts `bootstrap_data_lost`
without a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("voice-gateway.lock")

# Fix these first: each one, alone, explains every 503 the gateway sends.
REASONS = ("argon2_selftest", "keyring_unreadable", "removed_variable",
           "public_origin_required", "dev_insecure_cookie", "admin_password_weak",
           "bootstrap_data_lost", "bootstrap_required")
BOOTSTRAP_REASONS = frozenset({"bootstrap_data_lost", "bootstrap_required"})


@dataclass
class Lock:
    reasons: list[tuple[str, str | None]] = field(default_factory=list)

    def add(self, reason: str, variable: str | None = None) -> None:
        if reason not in REASONS:
            raise ValueError(f"{reason!r} is not a locked-mode reason")
        if (reason, variable) not in self.reasons:
            self.reasons.append((reason, variable))
            self.reasons.sort(key=lambda item: REASONS.index(item[0]))

    def clear(self, reasons: frozenset[str]) -> None:
        self.reasons = [item for item in self.reasons if item[0] not in reasons]

    @property
    def active(self) -> bool:
        return bool(self.reasons)

    @property
    def primary(self) -> tuple[str, str | None]:
        return self.reasons[0]

    def remind(self) -> None:
        for reason, variable in self.reasons:
            log.error("the gateway is LOCKED (%s)%s: every route on :8080 but "
                      "/health, /login and the device socket answers 503 until it "
                      "is fixed and the gateway restarted", reason,
                      f"; fix {variable}" if variable else "")
