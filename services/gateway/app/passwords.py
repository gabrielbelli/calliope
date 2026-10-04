"""Passwords: Argon2id from `cryptography`, two at a time, and the NIST rules.

    hasher = Hasher()
    phc = await hasher.hash("correct horse battery staple")
    ok = await hasher.verify(candidate, phc)
    problem("short", username="ana")  ->  "too_short"

**Argon2id, t=3, m=64 MiB, p=1, a 16-byte salt, stored as a PHC string**
(D17). OWASP's first choice, from the `cryptography` wheel every service
already installs (D10). One hash takes about 0.6 s on the NAS, so it runs in a
worker thread, and a semaphore of two bounds the memory at about 128 MiB of
the gateway's 512 MiB whatever the login rate: the audio routes on the event
loop never wait for it.

**Every path costs one verify.** An unknown user and the bootstrap login both
verify against a dummy hash, so the time a login takes does not say whether
the account exists (D19, D21).

**The rules are NIST SP 800-63B-4's** (D18): NFKC, 15 to 128 characters, no
composition rules, and a refusal list. The list is the 10k most common
passwords, the username, `calliope` and the bootstrap value. Nearly every
common password is shorter than 15 characters, so the list is also checked
against the password with its trailing digits and symbols removed and against
a word said over and over: `password12345678` and `qwertyqwertyqwerty` are the
long passwords people actually choose from it.

If the self-test fails (OpenSSL older than 3.2 has no Argon2), the gateway
enters locked mode rather than refusing to start (D10, D63).
"""

from __future__ import annotations

import asyncio
import os
import re
import string
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

COMMON_PASSWORDS = Path(__file__).with_name("common_passwords.txt")
MIN_LENGTH = 15
MAX_LENGTH = 128
# The one word every guesser tries against this service.
SERVICE_WORD = "calliope"


@dataclass(frozen=True)
class Params:
    iterations: int = 3
    memory_cost: int = 64 * 1024   # KiB
    lanes: int = 1
    length: int = 32
    salt_bytes: int = 16

    def phc_tag(self) -> str:
        return f"m={self.memory_cost},t={self.iterations},p={self.lanes}"


PARAMS = Params()
CONCURRENCY = 2

_PHC = re.compile(r"^\$argon2id\$v=19\$(m=\d+,t=\d+,p=\d+)\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$")


def normalise(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


class Hasher:
    def __init__(self, params: Params | None = None, concurrency: int = CONCURRENCY) -> None:
        self.params = params or PARAMS
        self.concurrency = concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._dummy: str | None = None

    def _kdf(self) -> Argon2id:
        p = self.params
        return Argon2id(salt=os.urandom(p.salt_bytes), length=p.length,
                        iterations=p.iterations, lanes=p.lanes,
                        memory_cost=p.memory_cost)

    def hash_sync(self, password: str) -> str:
        return self._kdf().derive_phc_encoded(normalise(password).encode("utf-8"))

    @staticmethod
    def verify_sync(password: str, phc: str) -> bool:
        if not _PHC.fullmatch(phc or ""):
            return False
        try:
            Argon2id.verify_phc_encoded(normalise(password).encode("utf-8"), phc)
        except (InvalidKey, ValueError):
            return False
        return True

    def needs_rehash(self, phc: str) -> bool:
        match = _PHC.fullmatch(phc or "")
        return match is None or match.group(1) != self.params.phc_tag()

    def selftest(self) -> bool:
        """Can this OpenSSL do Argon2id at all? Any failure is a no."""
        try:
            phc = self.hash_sync("calliope self-test passphrase")
            return (self.verify_sync("calliope self-test passphrase", phc)
                    and not self.verify_sync("calliope self-test passphrasf", phc))
        except Exception:  # noqa: BLE001 - UnsupportedAlgorithm, or anything else
            return False

    def _gate(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    async def hash(self, password: str) -> str:
        async with self._gate():
            return await asyncio.to_thread(self.hash_sync, password)

    async def verify(self, password: str, phc: str) -> bool:
        async with self._gate():
            return await asyncio.to_thread(self.verify_sync, password, phc)

    def prepare(self) -> None:
        """Make the dummy hash now, at start: made on first use, the first
        unknown-user login would cost two operations and stand out."""
        if self._dummy is None:
            self._dummy = self.hash_sync(os.urandom(24).hex())

    async def dummy_verify(self, password: str) -> None:
        """The cost of one real verify, spent on a hash nobody owns."""
        if self._dummy is None:
            await asyncio.to_thread(self.prepare)
        await self.verify(password, self._dummy or "")


@cache
def common_passwords() -> frozenset[str]:
    lines = COMMON_PASSWORDS.read_text(encoding="utf-8").splitlines()
    return frozenset(normalise(line).casefold() for line in lines
                     if line and not line.startswith("#"))


_TRAILING = string.digits + string.punctuation + string.whitespace


def _cores(folded: str) -> set[str]:
    """The password and the shorter words it is made of, as a guesser sees them."""
    cores = {folded, folded.rstrip(_TRAILING), folded.strip(_TRAILING)}
    for size in range(1, len(folded) // 2 + 1):
        if len(folded) % size == 0 and folded == folded[:size] * (len(folded) // size):
            cores.add(folded[:size])
    cores.discard("")
    return cores


def problem(password: str, *, username: str, bootstrap: str | None = None) -> str | None:
    """Why this password is refused, as a code, or None. Never echoes the password.

    too_short, too_long, common (on the list, or a list word padded or
    repeated), username, service_name, bootstrap.
    """
    text = normalise(password)
    if len(text) < MIN_LENGTH:
        return "too_short"
    if len(text) > MAX_LENGTH:
        return "too_long"
    cores = _cores(text.casefold())
    if bootstrap and (text == normalise(bootstrap)
                      or normalise(bootstrap).casefold() in cores):
        return "bootstrap"
    if normalise(username).casefold() in cores:
        return "username"
    if SERVICE_WORD in cores:
        return "service_name"
    if cores & common_passwords():
        return "common"
    return None


PROBLEMS = {
    "too_short": f"Use at least {MIN_LENGTH} characters.",
    "too_long": f"Use at most {MAX_LENGTH} characters.",
    "common": "That password is one of the most common ones. Choose another.",
    "username": "The password must not be your username.",
    "service_name": "The password must not be the name of this service.",
    "bootstrap": "The new password must differ from the first-access password.",
}
