"""Firmware signing for the build scripts: ECDSA P-256 over the image's SHA-256.

Plain Python, no PlatformIO, so the hub's tests can import it and check that a
signature made here verifies there. PlatformIO's own Python is not guaranteed
to have the `cryptography` package (Homebrew's 6.2.0 does, as a dependency of
esptool), so every operation falls back to the openssl CLI.

The satellite verifies with mbedTLS (ESP32 Arduino core 2.0.17 ships 2.28.7),
which has ECDSA on P-256 but no Ed25519: that, and nothing else, fixes the
algorithm.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile

# DER SubjectPublicKeyInfo of every P-256 public key: SEQUENCE { AlgorithmIdentifier
# { id-ecPublicKey, prime256v1 }, BIT STRING { 0x00 pad, 0x04 uncompressed point } }.
# Only the 64 bytes of X and Y follow, so a fixed prefix and a length check are a
# complete test that a key is P-256, with no ASN.1 parser.
P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d030107034200" "04")
P256_SPKI_LEN = len(P256_SPKI_PREFIX) + 64

DEFAULT_PRIVATE_KEY = "~/.config/calliope/firmware-signing.pem"


class SigningError(Exception):
    """An upload that could only fail on the satellite. Stopping on the
    developer's machine saves a full transfer, and the satellite's answer is
    less specific."""


def pem_to_der(pem: str, label: str = "PUBLIC KEY") -> bytes:
    begin, end = f"-----BEGIN {label}-----", f"-----END {label}-----"
    if begin not in pem or end not in pem:
        raise ValueError(f"not a PEM {label} (no '{begin}' line)")
    body = pem.split(begin, 1)[1].split(end, 1)[0]
    return base64.b64decode("".join(body.split()), validate=True)


def check_p256_public_key(der: bytes) -> None:
    if len(der) != P256_SPKI_LEN or not der.startswith(P256_SPKI_PREFIX):
        raise ValueError("not an uncompressed ECDSA P-256 public key; the satellite can only "
                         "verify P-256 (see keys/README.md)")


def key_id(der: bytes) -> str:
    """Short name for a public key, shown by the satellite and the hub so a
    mismatch is visible before an update is tried."""
    return hashlib.sha256(der).hexdigest()[:16]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _cryptography_private_key(key_path: str):
    """The key through `cryptography`, or None to use openssl instead: when the
    package is missing, or when the key is passphrase-protected (openssl asks
    for the passphrase on the terminal; this module never handles one)."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return None
    with open(key_path, "rb") as f:
        data = f.read()
    try:
        return load_pem_private_key(data, password=None)
    except TypeError:  # encrypted
        return None


def public_der_of_private(key_path: str) -> bytes:
    key = _cryptography_private_key(key_path)
    if key is not None:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return subprocess.run(["openssl", "pkey", "-in", key_path, "-pubout", "-outform", "DER"],
                          check=True, capture_output=True).stdout


def sign(image: bytes, key_path: str) -> bytes:
    """DER ECDSA signature over SHA-256(image), the form mbedtls_pk_verify takes."""
    key = _cryptography_private_key(key_path)
    if key is not None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError(f"{key_path} is not an ECDSA P-256 private key")
        return key.sign(image, ec.ECDSA(hashes.SHA256()))
    check_p256_public_key(public_der_of_private(key_path))
    with tempfile.TemporaryDirectory() as tmp:
        img, sig = os.path.join(tmp, "image.bin"), os.path.join(tmp, "image.sig")
        with open(img, "wb") as f:
            f.write(image)
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path, "-out", sig, img],
                       check=True)
        with open(sig, "rb") as f:
            return f.read()


def sign_for_upload(image: bytes, key_path: str, pub_path: str) -> str | None:
    """The `signature` query parameter for POST /satellites/firmware,
    base64url, or None to upload unsigned. `pub_path` is the key the build
    compiled in."""
    key_path = os.path.expanduser(key_path)
    have_key, have_pub = os.path.exists(key_path), os.path.exists(pub_path)
    if not have_key:
        if have_pub:
            raise SigningError(
                f"no signing key at {key_path} (set CALLIOPE_SIGNING_KEY). This build trusts "
                "a public key, so the satellites running it would refuse an unsigned image.")
        print("uploading UNSIGNED: only a satellite built without a public key accepts it",
              file=sys.stderr)
        return None
    got = public_der_of_private(key_path)
    if have_pub:
        with open(pub_path) as f:
            want = pem_to_der(f.read())
        if got != want:
            raise SigningError(
                f"{key_path} is not the private half of {pub_path} "
                f"(key {key_id(got)}; the build trusts {key_id(want)})")
    else:
        # Signed, so a satellite that requires signatures installs it, and from
        # then on accepts anything. Allowed, because that is how a satellite
        # goes back to development, but never quietly.
        print("WARNING: signing an image built WITHOUT a public key. A satellite that installs "
              "it accepts unsigned updates from then on.", file=sys.stderr)
    sig = sign(image, key_path)
    print(f"signed with key {key_id(got)} ({len(sig)}-byte DER signature)")
    return b64url(sig)
