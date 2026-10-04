"""Delegation tokens for link transcription: two uses, re-checked live (D64, §3.7).

    token = delegations.issue(signer, sub=user_id, cred="session:<ref>")   # :8080, /ui/fetch
    delegations.claim(claims)                                              # :8081, per use

The gateway hands voice-ui a token it can spend on POST
/v1/audio/transcriptions through the internal listener, so the run record is
the user's. The token alone is never enough:

* each `jti` is accepted at most twice (one retry), counted here in memory;
* on every use the gateway checks again that the session or key named in
  `cred` still authenticates and its user is active, so logging out,
  revoking the key or disabling the user ends the delegation at once;
* the scopes are the user's CURRENT effective scopes intersected with
  `speech:transcribe`, worked out by the caller at the moment of use.

A jti the gateway did not issue in this process (it restarted) is refused:
voice-ui's one retry then fails and the user starts the transcription again.

**Bounded** (recheck M-4): entries expire at the token's `exp` and the map
holds at most 100k of them.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from voice_common import identity

from . import db as dbmod

MAX_USES = 2
MAX_ENTRIES = 100_000


@dataclass
class _Use:
    sub: str
    cred: str
    exp: float
    uses: int = 0


class Delegations:
    def __init__(self, limit: int = MAX_ENTRIES) -> None:
        self.limit = limit
        self._issued: OrderedDict[str, _Use] = OrderedDict()

    def issue(self, signer: identity.Signer, *, sub: str, cred: str) -> str:
        token = signer.delegation(sub=sub, cred=cred, now=dbmod.now())
        claims = identity.verify_delegation(token, {signer.kid: signer.public_key},
                                            now=dbmod.now())
        self.prune()
        self._issued[claims.jti] = _Use(sub=sub, cred=cred, exp=claims.exp)
        while len(self._issued) > self.limit:
            self._issued.popitem(last=False)
        return token

    def claim(self, claims: identity.Claims) -> str | None:
        """Count one use. Returns the refusal reason, or None when the use is allowed."""
        entry = self._issued.get(claims.jti)
        if entry is None or entry.exp <= dbmod.now():
            return "unknown_jti"
        if (entry.sub, entry.cred) != (claims.sub, claims.cred):
            return "mismatch"
        if entry.uses >= MAX_USES:
            return "used_up"
        entry.uses += 1
        return None

    def prune(self) -> None:
        # Every token lives the same 30 minutes, so issue order is expiry
        # order and the expired ones are all at the front.
        at = dbmod.now()
        while self._issued:
            jti, entry = next(iter(self._issued.items()))
            if entry.exp > at:
                break
            del self._issued[jti]
