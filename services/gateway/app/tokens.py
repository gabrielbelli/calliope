"""The shapes of every secret and identifier the gateway mints (D26, §2.1).

    calliope_<30 base62><6 base62 CRC32>        a user's API key
    calliope_svc_<30 base62><6 base62 CRC32>    a service principal's key
    u_<16 base32>  k_<12 base32>                user and key IDs

**The checksum rejects a typo without a database lookup.** CRC32 over the
prefix and the random part, in six base62 characters (62^6 is above 2^32), so
a key pasted with one character wrong is refused by shape, and a secret
scanner can tell a Calliope key from noise. base62 rather than base64 so a
double-click selects the whole key.

**Only a hash is stored.** SHA-256 of the whole key: the keys are 178 bits
of randomness, so a fast hash is enough, and a leaked database holds nothing
that authenticates (D27).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string
import zlib

BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
BASE32 = "abcdefghijklmnopqrstuvwxyz234567"

USER_KEY_PREFIX = "calliope_"
SERVICE_KEY_PREFIX = "calliope_svc_"
RANDOM_LENGTH = 30
CHECK_LENGTH = 6

USER_KEY = re.compile(r"^calliope_[0-9A-Za-z]{36}$")
SERVICE_KEY = re.compile(r"^calliope_svc_[0-9A-Za-z]{36}$")


def _base62(number: int, width: int) -> str:
    digits = []
    for _ in range(width):
        number, rest = divmod(number, 62)
        digits.append(BASE62[rest])
    return "".join(reversed(digits))


def _check(prefix: str, body: str) -> str:
    return _base62(zlib.crc32((prefix + body).encode("ascii")), CHECK_LENGTH)


def mint(prefix: str) -> str:
    body = "".join(secrets.choice(BASE62) for _ in range(RANDOM_LENGTH))
    return prefix + body + _check(prefix, body)


def well_formed(token: str, prefix: str) -> bool:
    """The right prefix, length and alphabet, and a checksum that matches."""
    pattern = SERVICE_KEY if prefix == SERVICE_KEY_PREFIX else USER_KEY
    if not pattern.fullmatch(token):
        return False
    body = token[len(prefix):len(prefix) + RANDOM_LENGTH]
    return secrets.compare_digest(token[-CHECK_LENGTH:], _check(prefix, body))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def identifier(prefix: str, length: int) -> str:
    return prefix + "".join(secrets.choice(BASE32) for _ in range(length))


def display(key: str) -> str:
    """calliope_AbCd…wxyz: enough to recognise a key in a list, never enough to use it."""
    return f"{USER_KEY_PREFIX}{key[len(USER_KEY_PREFIX):][:4]}…{key[-4:]}"
