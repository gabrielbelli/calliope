"""Firmware signatures, on the hub's side.

A satellite built with a public key (clients/korvo-satellite/keys) installs an
update only when it carries an ECDSA P-256 signature, by the matching private
key, over the image's SHA-256. The developer signs on their own machine
(scripts/upload_via_hub.py) and the hub only carries the signature: the
upload's `signature` query parameter in, the "ota" message's `signature` out.

Nothing here is what keeps a satellite safe; the satellite checks for itself,
so a hub that is compromised still cannot install its own firmware. What this
module does is refuse mistakes before a 15 s transfer ends in "bad signature":
a signature that is not one, and, when SATELLITES_FIRMWARE_PUBKEY names the
key, one made with the wrong key.

`cryptography` is imported only when SATELLITES_FIRMWARE_PUBKEY is set, so a
hub that does not check signatures does not need it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from typing import Any

ENV_KEY = "SATELLITES_FIRMWARE_PUBKEY"
# DER ECDSA P-256: SEQUENCE { INTEGER r, INTEGER s }, each integer at most 33
# bytes (32, plus a zero when the top bit is set).
MAX_DER = 72
MIN_DER = 8


class SignatureError(ValueError):
    """The signature, or the key it should be checked with, is unusable. The
    message is written for the person uploading."""


def _integer(der: bytes, at: int) -> int:
    """Checks one DER INTEGER starting at `at` and returns where it ends."""
    if at + 2 > len(der) or der[at] != 0x02:
        raise SignatureError("not a DER ECDSA signature (expected an INTEGER)")
    n = der[at + 1]
    body = der[at + 2:at + 2 + n]
    if not 1 <= n <= 33 or len(body) != n:
        raise SignatureError("not a DER ECDSA P-256 signature (integer length)")
    if body[0] & 0x80:
        raise SignatureError("not a DER ECDSA signature (negative integer)")
    if n > 1 and body[0] == 0 and not body[1] & 0x80:
        raise SignatureError("not a DER ECDSA signature (integer not minimally encoded)")
    if not any(body):
        raise SignatureError("not an ECDSA signature (zero r or s)")
    return at + 2 + n


def check_der(der: bytes) -> None:
    """The shape mbedTLS on the satellite will parse, checked without a key."""
    if not MIN_DER <= len(der) <= MAX_DER:
        raise SignatureError(f"a P-256 signature is {MIN_DER} to {MAX_DER} bytes of DER; "
                             f"this is {len(der)}")
    # Every valid length fits DER's short form, so the second byte is the length.
    if der[0] != 0x30 or der[1] != len(der) - 2:
        raise SignatureError("not a DER ECDSA signature (expected one SEQUENCE)")
    if _integer(der, _integer(der, 2)) != len(der):
        raise SignatureError("not a DER ECDSA signature (bytes after the second INTEGER)")


def decode(text: str) -> bytes:
    """base64 or base64url, padded or not, to checked DER. The upload script
    sends base64url because it travels in a query string."""
    text = text.strip()
    if not text:
        raise SignatureError("empty signature")
    std = text.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        der = base64.b64decode(std + "=" * (-len(std) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("signature is not base64 or base64url") from None
    check_der(der)
    return der


def encode(der: bytes) -> str:
    """Standard padded base64: the form the "ota" message carries."""
    return base64.b64encode(der).decode()


def load_public_key(value: str | None = None) -> Any | None:
    """The hub's copy of the firmware key, from SATELLITES_FIRMWARE_PUBKEY:
    a path to a PEM file, or the PEM itself. None when the variable is unset
    or empty."""
    value = os.environ.get(ENV_KEY, "") if value is None else value
    if not value.strip():
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        raise SignatureError(f"{ENV_KEY} is set, but the cryptography package is not "
                             "installed") from None
    pem = value.encode() if "-----BEGIN" in value else _read(value)
    try:
        key = load_pem_public_key(pem)
    except ValueError as e:
        raise SignatureError(f"{ENV_KEY} is not a PEM public key: {e}") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise SignatureError(f"{ENV_KEY} is not an ECDSA P-256 key, the only kind the "
                             "satellite can verify")
    return key


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise SignatureError(f"{ENV_KEY}: cannot read {path}: {e.strerror}") from None


def key_id(public_key: Any) -> str:
    """The same short name the firmware build prints and the satellite
    reports as caps.ota_key: the first 16 hex of the DER public key's
    SHA-256."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def verify(image: bytes, der: bytes, public_key: Any) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        public_key.verify(der, image, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise SignatureError(f"the signature does not match this image under key "
                             f"{key_id(public_key)} ({ENV_KEY})") from None


def accept_upload(image: bytes, signature: str | None, public_key: Any | None = None) -> str | None:
    """What POST /satellites/firmware keeps from its `signature` parameter:
    standard base64 to store beside the image and send in the "ota" message,
    or None for an unsigned image. Raises SignatureError (answer 400) for a
    malformed signature and, when the hub has a key, for a missing or wrong
    one."""
    if public_key is None:
        public_key = load_public_key()
    if not signature:
        if public_key is not None:
            raise SignatureError(f"unsigned image: this hub has {ENV_KEY} set (key "
                                 f"{key_id(public_key)}), so every image must be signed")
        return None
    der = decode(signature)
    if public_key is not None:
        verify(image, der, public_key)
    return encode(der)


def skip_reason(caps: dict, signature: str | None, public_key: Any | None = None) -> str | None:
    """Why POST /satellites/ota should skip a satellite rather than start a
    transfer the satellite will refuse, or None to go ahead. A satellite
    that reports no `ota_key` was built without one and takes anything."""
    satellite_key = (caps or {}).get("ota_key")
    if not satellite_key:
        return None
    if not signature:
        return (f"the satellite only installs images signed by key {satellite_key}; "
                "this one is unsigned")
    if public_key is not None and key_id(public_key) != satellite_key:
        return (f"the satellite trusts key {satellite_key}, but this image was checked "
                f"against key {key_id(public_key)}")
    return None
