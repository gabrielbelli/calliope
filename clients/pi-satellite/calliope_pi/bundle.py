"""Signed release bundles, and moving between releases.

A BUNDLE is a gzipped tar with a manifest.json at its top ({"version",
"model"}), this package, its scripts and an install.sh. It is signed the way
the ESP32 firmware is (ADR 0021): DER ECDSA P-256 over the bundle's SHA-256,
by the key whose public half the satellite was set up with, so a hub that is
compromised still cannot install its own code.

RELEASES live side by side in /opt/calliope/releases/<version>; `current`
and `previous` are symlinks. Installing one unpacks it, runs its install.sh,
swaps the links and leaves pending.json with a deadline. The new release
clears it once the hub has welcomed it (commit); if it never does, the
rollback timer puts `previous` back (rollback_if_due), as the ESP32's
bootloader does with an image that never confirmed itself.

Everything here that changes the system runs as root, through calliope-root
(root.py); the agent itself only receives the bytes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from . import paths

MODEL = "raspberry-pi"
MAX_BYTES = 64 * 1024 * 1024
CONFIRM_S = 180            # the ESP32's: a new release that has not met the hub by then goes
VERSION = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


class BundleError(Exception):
    """Why a bundle is refused, in words for the hub's page."""


def key_id(pubkey_path: Path | None = None) -> str | None:
    """The first 16 hex of the SHA-256 of the DER public key, as the ESP32
    and the hub name it; None when the satellite has no key."""
    try:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_public_key,
        )
        key = load_pem_public_key((pubkey_path or paths.PUBKEY).read_bytes())
    except (OSError, ValueError, ImportError):
        return None
    der = key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def decode_signature(text: str | None) -> bytes:
    if not text:
        raise BundleError("unsigned image")
    try:
        pad = "=" * (-len(text) % 4)
        return base64.b64decode(text.replace("-", "+").replace("_", "/") + pad, validate=True)
    except (binascii.Error, ValueError):
        raise BundleError("bad signature (not base64)") from None


def verify(data: bytes, signature: str | None, pubkey_path: Path | None = None) -> None:
    """Raise BundleError unless `signature` is the key's over `data`. With no
    key at all the satellite accepts nothing: an update is always signed."""
    path = pubkey_path or paths.PUBKEY
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError:
        raise BundleError("python3-cryptography is not installed, so nothing can be verified") from None
    try:
        key = load_pem_public_key(path.read_bytes())
    except OSError:
        raise BundleError(f"no public key at {path}: this satellite accepts no update") from None
    der = decode_signature(signature)
    try:
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise BundleError("bad signature") from None


def manifest(data: bytes) -> dict:
    """The bundle's manifest.json, checked, without unpacking anything else."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = tar.getmember("manifest.json")
            raw = json.loads(tar.extractfile(member).read())
    except (tarfile.TarError, KeyError, ValueError, AttributeError):
        raise BundleError("not a satellite bundle (no manifest.json in a .tar.gz)") from None
    if not isinstance(raw, dict) or not VERSION.match(str(raw.get("version", ""))):
        raise BundleError("the bundle's manifest has no usable version")
    if raw.get("model") != MODEL:
        raise BundleError(f"the bundle is for {raw.get('model')!r}, not {MODEL!r}")
    return raw


def unpack(data: bytes, dest: Path) -> None:
    """Into `dest`, which must not exist. tarfile's data filter refuses
    absolute paths, links out of the tree and device files."""
    tmp = dest.with_name(dest.name + ".unpacking")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(tmp, filter="data")
    os.replace(tmp, dest)


def _target(link: Path) -> Path | None:
    try:
        return Path(os.readlink(link))
    except OSError:
        return None


def _link(link: Path, target: Path) -> None:
    tmp = link.with_name(link.name + ".new")
    with_suppress_unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def with_suppress_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def run_hook(release: Path, env: dict | None = None) -> None:
    hook = release / "install.sh"
    if not hook.exists():
        return
    done = subprocess.run(["/bin/sh", str(hook)], cwd=release, capture_output=True, text=True,
                          timeout=1800, env={**os.environ, "CALLIOPE_RELEASE": str(release), **(env or {})})
    if done.returncode != 0:
        tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
        raise BundleError(f"install.sh failed ({done.returncode}): {' | '.join(tail)[:300]}")


def install(data: bytes, signature: str | None, *, hook: bool = True, now: float | None = None) -> dict:
    """Verify, unpack, run install.sh, swap the links, and leave pending.json.
    The manifest of what was installed."""
    if len(data) > MAX_BYTES:
        raise BundleError(f"the bundle is over {MAX_BYTES // 1048576} MB")
    verify(data, signature)
    man = manifest(data)
    releases = paths.releases()
    releases.mkdir(parents=True, exist_ok=True)
    dest = releases / man["version"]
    old = _target(paths.current())
    if old is not None and old.resolve() == dest.resolve():
        raise BundleError(f"{man['version']} is the release already running")
    shutil.rmtree(dest, ignore_errors=True)
    unpack(data, dest)
    if hook:
        run_hook(dest)
    if old is not None:
        _link(paths.previous(), old)
    _link(paths.current(), dest)
    pending = {"version": man["version"], "previous": str(old) if old else None,
               "deadline": (now or time.time()) + CONFIRM_S}
    paths.pending().parent.mkdir(parents=True, exist_ok=True)
    paths.pending().write_text(json.dumps(pending))
    return man


def pending() -> dict | None:
    try:
        return json.loads(paths.pending().read_text())
    except (OSError, ValueError):
        return None


def commit() -> str | None:
    """The new release met the hub: it stays. Its version, or None when
    nothing was pending."""
    p = pending()
    if p is None:
        return None
    with_suppress_unlink(paths.pending())
    return p.get("version")


def rollback(reason: str, *, hook: bool = True) -> str | None:
    """Put the previous release back. The version rolled back to, or None
    when there is nothing to go back to."""
    p = pending()
    back = Path(p["previous"]) if p and p.get("previous") else _target(paths.previous())
    if back is None or not back.exists():
        with_suppress_unlink(paths.pending())
        return None
    if hook:
        try:
            run_hook(back)
        except BundleError:
            pass  # the old release ran before; its units are what they were
    _link(paths.current(), back)
    with_suppress_unlink(paths.pending())
    (paths.STATE / "rolled_back.json").write_text(json.dumps(
        {"from": p.get("version") if p else None, "to": back.name, "reason": reason, "at": time.time()}))
    return back.name


def rollback_if_due(now: float | None = None) -> str | None:
    p = pending()
    if p is None or (now or time.time()) < float(p.get("deadline", 0)):
        return None
    return rollback(f"{p.get('version')} did not reach the hub within {CONFIRM_S} s")


def prune(keep: int = 3) -> list[str]:
    """Delete old releases, never `current` or `previous`."""
    keepers = {t.resolve() for t in (_target(paths.current()), _target(paths.previous())) if t}
    all_ = sorted((d for d in paths.releases().iterdir() if d.is_dir() and not d.name.endswith(".unpacking")),
                  key=lambda d: d.stat().st_mtime, reverse=True) if paths.releases().is_dir() else []
    gone = []
    for d in all_[keep:]:
        if d.resolve() not in keepers:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
    return gone
