#!/usr/bin/env python3
"""Build and sign a satellite release bundle.

    python3 scripts/build_bundle.py [--out dist] [--key ~/.config/calliope/firmware-signing.pem]

Writes dist/calliope-pi-<version>.tar.gz and, beside it, .sig: the
signature in base64, the form POST /satellites/firmware takes as `signature`.
The version is `git describe --tags --always --dirty`, as the ESP32 build
stamps its own, so the Satellites tab orders the two the same way.

The key is the ESP32 firmware's (clients/korvo-satellite/keys/README.md):
CALLIOPE_SIGNING_KEY, else ~/.config/calliope/firmware-signing.pem. A bundle
is never built unsigned: the satellite accepts nothing that is not signed.

The tarball is reproducible: files in sorted order, owned by root, dated
from the commit, gzip without a name or a time."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MODEL = "raspberry-pi"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, check=True).stdout.strip()


def files() -> list[tuple[str, Path]]:
    """(name in the bundle, file) for everything a release carries."""
    out = []
    for p in sorted((HERE / "calliope_pi").glob("*.py")):
        out.append((f"calliope_pi/{p.name}", p))
    for p in sorted((HERE / "bundle").rglob("*")):
        if p.is_file():
            out.append((str(p.relative_to(HERE / "bundle")), p))
    return out


def build(version: str, epoch: int) -> bytes:
    manifest = json.dumps({"version": version, "model": MODEL, "built_from": git("rev-parse", "HEAD")},
                          indent=1).encode() + b"\n"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add(name: str, data: bytes, mode: int) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, epoch
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", manifest, 0o644)
        for name, path in files():
            executable = os.access(path, os.X_OK) or name.endswith(".sh") or name.startswith("bin/")
            add(name, path.read_bytes(), 0o755 if executable else 0o644)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def sign(data: bytes, key_path: Path) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise SystemExit(f"{key_path} is not an ECDSA P-256 private key")
    return base64.b64encode(key.sign(data, ec.ECDSA(hashes.SHA256()))).decode()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(HERE / "dist"))
    ap.add_argument("--key", default=os.environ.get("CALLIOPE_SIGNING_KEY",
                                                    "~/.config/calliope/firmware-signing.pem"))
    ap.add_argument("--version", help="instead of git describe")
    args = ap.parse_args(argv)
    key = Path(args.key).expanduser()
    if not key.exists():
        raise SystemExit(f"no signing key at {key} (set CALLIOPE_SIGNING_KEY)")
    version = args.version or git("describe", "--tags", "--always", "--dirty")
    epoch = int(git("log", "-1", "--format=%ct"))
    data = build(version, epoch)
    signature = sign(data, key)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"calliope-pi-{version}.tar.gz"
    path.write_bytes(data)
    sig = path.parent / (path.name + ".sig")
    sig.write_text(signature + "\n")
    print(json.dumps({"bundle": str(path), "version": version, "model": MODEL, "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "signature": str(sig)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
