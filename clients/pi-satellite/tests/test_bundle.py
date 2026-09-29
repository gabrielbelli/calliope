"""Signed releases: built, verified, installed beside the old one, confirmed
by the hub's welcome or put back when they never reach it."""

from __future__ import annotations

import base64
import io
import json
import tarfile
import time

import build_bundle
import pytest

from calliope_pi import bundle, paths, root


def make(version: str, key, *, model: str = "raspberry-pi", hook: str | None = None) -> tuple[bytes, str]:
    data = build_bundle.build(version, epoch=1_700_000_000)
    if model != "raspberry-pi" or hook is not None:
        raw = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as src, \
                tarfile.open(fileobj=raw, mode="w:gz") as out:
            for m in src.getmembers():
                body = src.extractfile(m).read() if m.isfile() else None
                if m.name == "manifest.json":
                    body = json.dumps(json.loads(body) | {"model": model}).encode()
                if m.name == "install.sh" and hook is not None:
                    body = hook.encode()
                if body is not None:
                    m.size = len(body)
                    out.addfile(m, io.BytesIO(body))
        data = raw.getvalue()
    return data, build_bundle.sign(data, key)


def test_a_bundle_is_reproducible_and_carries_its_manifest_and_code(key):
    one = build_bundle.build("v1", epoch=1_700_000_000)
    assert one == build_bundle.build("v1", epoch=1_700_000_000)
    with tarfile.open(fileobj=io.BytesIO(one), mode="r:gz") as tar:
        names = tar.getnames()
        assert json.loads(tar.extractfile("manifest.json").read())["model"] == "raspberry-pi"
        assert tar.getmember("install.sh").mode == 0o755
    assert "calliope_pi/agent.py" in names and "systemd/calliope-agent.service" in names
    assert "bin/calliope-root" in names and "sudoers.d/calliope" in names


def test_only_a_bundle_signed_by_the_satellites_key_is_accepted(key, tmp_path):
    data, sig = make("v1", key)
    bundle.verify(data, sig)
    bundle.verify(data, sig.replace("+", "-").replace("/", "_").rstrip("="))  # base64url too
    with pytest.raises(bundle.BundleError, match="bad signature"):
        bundle.verify(data + b"x", sig)
    with pytest.raises(bundle.BundleError, match="unsigned"):
        bundle.verify(data, None)
    paths.PUBKEY.unlink()
    with pytest.raises(bundle.BundleError, match="accepts no update"):
        bundle.verify(data, sig)


def test_a_bundle_for_another_board_is_refused(key):
    data, _ = make("v1", key, model="esp32-korvo-v1.1")
    with pytest.raises(bundle.BundleError, match="not 'raspberry-pi'"):
        bundle.manifest(data)
    with pytest.raises(bundle.BundleError, match="not a satellite bundle"):
        bundle.manifest(b"\xe9 an ESP32 image")


def test_the_key_is_named_as_the_esp32_and_the_hub_name_it(key):
    import hashlib

    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )
    der = load_pem_public_key(paths.PUBKEY.read_bytes()).public_bytes(Encoding.DER,
                                                                      PublicFormat.SubjectPublicKeyInfo)
    assert bundle.key_id() == hashlib.sha256(der).hexdigest()[:16]


def test_install_swaps_releases_and_the_welcome_keeps_it(key):
    v1, s1 = make("v1", key)
    bundle.install(v1, s1, hook=False)
    bundle.commit()
    v2, s2 = make("v2", key)
    man = bundle.install(v2, s2, hook=False, now=1000.0)
    assert man["version"] == "v2"
    assert paths.current().resolve().name == "v2" and paths.previous().resolve().name == "v1"
    assert (paths.current() / "calliope_pi" / "agent.py").exists()
    p = bundle.pending()
    assert p["version"] == "v2" and p["deadline"] == 1000.0 + bundle.CONFIRM_S
    assert bundle.rollback_if_due(now=1000.0 + bundle.CONFIRM_S - 1) is None
    assert bundle.commit() == "v2" and bundle.pending() is None
    assert bundle.rollback_if_due(now=10 ** 10) is None, "a confirmed release is never rolled back"


def test_a_release_that_never_reaches_the_hub_is_rolled_back(key):
    for v in ("v1", "v2"):
        data, sig = make(v, key)
        bundle.install(data, sig, hook=False, now=1000.0)
        if v == "v1":
            bundle.commit()
    assert bundle.rollback_if_due(now=1000.0 + bundle.CONFIRM_S) == "v1"
    assert paths.current().resolve().name == "v1" and bundle.pending() is None
    why = json.loads((paths.STATE / "rolled_back.json").read_text())
    assert why["from"] == "v2" and why["to"] == "v1" and "did not reach the hub" in why["reason"]


def test_a_failing_install_script_changes_nothing(key):
    v1, s1 = make("v1", key)
    bundle.install(v1, s1, hook=False)
    bundle.commit()
    bad, sig = make("v2", key, hook="#!/bin/sh\necho 'apt: no space left'\nexit 3\n")
    with pytest.raises(bundle.BundleError, match="install.sh failed .3.: apt: no space left"):
        bundle.install(bad, sig)
    assert paths.current().resolve().name == "v1" and bundle.pending() is None


def test_the_running_release_is_not_installed_over_itself(key):
    data, sig = make("v1", key)
    bundle.install(data, sig, hook=False)
    with pytest.raises(bundle.BundleError, match="already running"):
        bundle.install(data, sig, hook=False)


def test_old_releases_are_pruned_but_never_current_or_previous(key):
    for v in ("v1", "v2", "v3", "v4", "v5"):
        data, sig = make(v, key)
        bundle.install(data, sig, hook=False)
        bundle.commit()
        time.sleep(0.01)
    gone = bundle.prune(keep=2)
    assert sorted(gone) == ["v1", "v2", "v3"]
    assert {d.name for d in paths.releases().iterdir()} == {"v4", "v5"}


def test_calliope_root_installs_only_from_the_agents_incoming_directory(key, tmp_path, capsys):
    data, sig = make("v1", key)
    outside = tmp_path / "elsewhere.bundle"
    outside.write_bytes(data)
    assert root.main(["install", str(outside), sig]) == 1
    assert "is not in" in capsys.readouterr().out
    paths.incoming().mkdir(parents=True)
    inside = paths.incoming() / "x.bundle"
    inside.write_bytes(data)
    bad = base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01").decode()
    assert root.main(["install", str(inside), bad]) == 1
    assert "bad signature" in capsys.readouterr().out
    assert not paths.current().exists()
