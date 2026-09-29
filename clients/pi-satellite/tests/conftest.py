"""Every test runs the satellite in a temporary directory: its releases, its
state and its update key, through the CALLIOPE_* variables paths.py reads."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture
def key(tmp_path):
    """A P-256 key pair: the private one to sign with, the public one where
    the satellite looks for it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    priv = tmp_path / "signing.pem"
    priv.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    pub = tmp_path / "etc" / "firmware-signing.pub.pem"
    pub.parent.mkdir()
    pub.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_OPT", str(tmp_path / "opt"))
    monkeypatch.setenv("CALLIOPE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("CALLIOPE_PUBKEY", str(tmp_path / "etc" / "firmware-signing.pub.pem"))
    monkeypatch.setenv("CALLIOPE_BOOT", str(tmp_path / "boot"))
    from calliope_pi import paths
    importlib.reload(paths)
    for name in ("bundle", "state", "root", "agent", "portal", "firstboot"):
        mod = sys.modules.get(f"calliope_pi.{name}")
        if mod is not None:
            importlib.reload(mod)
    yield
