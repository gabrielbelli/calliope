"""The runner's certificate: minted once into /state/tls, pinned by tts-long.

THERE IS NO PLAIN-HTTP PATH AND NO SECOND TRUST MODE. tts-long talks to a
runner only through `HTTPSConnection`, and the bearer key crosses on every
request, so TLS is always on. The certificate is self-signed and tts-long pins
it by the SHA-256 of its DER bytes (`TTS_RUNNER_FINGERPRINT`), exactly as it
pins offpeak's. Pinning verifies one specific certificate, which is stricter
than validating a chain, so this does not break the rule in
voice-entrypoint.sh: that rule exists to stop clients being taught to skip
verification, and a pinned client skips nothing.

THE FINGERPRINT CHANGES ONLY WHEN THE CERTIFICATE DOES. Losing the /state
volume mints a new one, and tts-long then refuses the runner until its
fingerprint is updated. That is the intended failure: the CPU only, never an
unverified connection.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import secrets
import ssl
from pathlib import Path

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
COMMON_NAME = "calliope-tts-runner"
# Ten years. In pin mode tts-long checks neither the dates nor the name, so an
# expiry would only ever be a way for a working runner to stop working.
VALID_DAYS = 3650


def ensure(directory: Path) -> tuple[Path, Path]:
    """The certificate and key in `directory`, minting a pair if there is none.

    HALF A PAIR IS REFUSED, NOT REPAIRED. A key without its certificate (or the
    reverse) is a volume somebody has edited by hand, and minting over it would
    silently change the fingerprint tts-long pins.
    """
    directory = Path(directory)
    cert, key = directory / CERT_NAME, directory / KEY_NAME
    have_cert, have_key = cert.exists(), key.exists()
    if have_cert and have_key:
        return cert, key
    if have_cert or have_key:
        present, missing = (cert, key) if have_cert else (key, cert)
        raise SystemExit(f"tts-runner: {present} exists and {missing} does not; "
                         f"delete both to mint a new pair")
    _mint(directory, cert, key)
    return cert, key


def fingerprint(cert: Path) -> str:
    """SHA-256 of the certificate's DER bytes, lower-case hex.

    The value tts-long compares in `RunnerClient._connect`, and the same
    function `offpeak fingerprint` prints for offpeak's certificate.
    """
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def _mint(directory: Path, cert: Path, key: Path) -> None:
    # Imported here: `fingerprint` runs in the CLI and needs none of this.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        # Five minutes back, so a client whose clock is a little behind this
        # host's does not read a certificate from the future.
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .sign(private, hashes.SHA256()))
    # THE KEY FIRST AND THE CERTIFICATE LAST, so a crash between the two leaves
    # half a pair, which `ensure` refuses by name, rather than a certificate
    # whose key was never written.
    _write(key, private.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()), 0o600)
    _write(cert, certificate.public_bytes(serialization.Encoding.PEM), 0o644)


def _write(path: Path, data: bytes, mode: int) -> None:
    """Write under a temporary name, then rename: never a short file in place."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        # The mode again, explicitly: os.open's is filtered by the umask.
        os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
