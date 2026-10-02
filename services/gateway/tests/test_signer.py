"""The identity key: identity.pub on every volume, and rotation without an outage (D7, D69, L15)."""

from __future__ import annotations

import json

import pytest
from conftest import Clock, gateway
from voice_common import identity
from voice_common.scopes import SERVICE_PRINCIPALS


async def test_every_service_volume_gets_identity_pub_and_its_own_key(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        settings = main.runtime.get().settings
        keys = {name: (settings.svc_dir / name / "service.key").read_text().strip()
                for name in SERVICE_PRINCIPALS}
        documents = {name: json.loads((settings.svc_dir / name / "identity.pub").read_text())
                     for name in SERVICE_PRINCIPALS}
        keyring = settings.keys_dir / "identity.json"
        mode = keyring.stat().st_mode & 0o777

    assert len(set(keys.values())) == len(SERVICE_PRINCIPALS)
    assert all(key.startswith("calliope_svc_") for key in keys.values())
    assert all(doc["keys"][0]["kid"] == "1" for doc in documents.values())
    assert mode == 0o400


async def test_a_restart_keeps_the_signing_key_and_the_service_keys(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        first = main.runtime.get().keys.signer().public_key
        settings = main.runtime.get().settings
        service_key = (settings.svc_dir / "stt" / "service.key").read_text()
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        again = main.runtime.get().keys.signer().public_key
        same_key = (settings.svc_dir / "stt" / "service.key").read_text()

    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = lambda key: key.public_bytes(Encoding.Raw, PublicFormat.Raw)  # noqa: E731
    assert raw(first) == raw(again)
    assert service_key == same_key


async def test_an_unreadable_keyring_stops_the_gateway_with_its_name(monkeypatch, tmp_path):
    """The one fault locked mode cannot cover: nothing could be signed, the
    device relay included (recheck L12)."""
    from app import signer

    (tmp_path / "keys").mkdir(parents=True)
    (tmp_path / "keys" / "identity.json").write_text("{ not json")
    with pytest.raises(signer.Unreadable, match="identity.json"):
        async with gateway(monkeypatch, authenticate=False):
            pass


async def test_rotation_keeps_the_old_kid_for_two_minutes_then_drops_it(monkeypatch):
    """D69: a backend verifies an assertion signed by the previous key for two
    minutes after a rotation, and not after. The gateway's own delegation
    check keeps it for the 30 minutes a delegation lives (recheck L15)."""
    from app import admin

    clock = Clock(monkeypatch)
    backend_clock = [0.0]
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        rt = main.runtime.get()
        before = rt.keys.signer()
        credentials = identity.Credentials(rt.settings.svc_dir / "stt",
                                           clock=lambda: backend_clock[0])

        def signed_by_the_old_key() -> str:
            return before.assertion(audience="stt", sub="svc:stt", kind="service",
                                    scopes={"runs:write"}, cred="svc:stt", now=clock.at)

        admin.main(["rotate-identity-key"])
        clock.advance(2)
        after = rt.keys.signer()
        clock.advance(100)
        rt.keys.publish()
        backend_clock[0] += 31
        within = identity.verify(signed_by_the_old_key(), "stt", credentials, now=clock.at)
        clock.advance(30)
        rt.keys.publish()
        backend_clock[0] += 31
        with pytest.raises(identity.InvalidAssertion) as dropped:
            identity.verify(signed_by_the_old_key(), "stt", credentials, now=clock.at)
        delegations = rt.keys.delegation_keys()
        clock.advance(30 * 60)
        later = rt.keys.delegation_keys()

    assert (before.kid, after.kid) == ("1", "2")
    assert within.sub == "svc:stt"
    assert dropped.value.reason == "unknown_key"
    assert set(delegations) == {"1", "2"} and set(later) == {"2"}
