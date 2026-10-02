"""The secret store: Admin › Secrets, the services' fetch and import, and the keyring.

D8 (keyring source), D38-D43 (one encrypted table, name binding, write-only,
allowed hosts, serving, rotation), D44 and D66 (the import window), D46
(status rows), and recheck L14 (atomic keyring writes) and L18 (the import
allowlist is the service's own word).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import (PASSWORD, SAME_ORIGIN, Clock, MockBackend, bearer, gateway,
                      internal_client, make_key, make_user, service_key, sign_in)
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HA_TOKEN = "ha-token-7c1e9a0f-only-in-the-store"
LLM_KEY = "sk-or-v1-3b9d-only-in-the-store"
WEBHOOK = "https://ha.lan:8123/api/webhook/button-only-in-the-store"
RUNNER_KEY = "runner-key-51f0-only-in-the-store"


# ── helpers ───────────────────────────────────────────────────────────────────


async def signed_in_admin(client, *, step_up: bool = True) -> None:
    """The page as an admin: a session, same-origin headers, and a fresh password."""
    make_user("ana")
    assert (await sign_in(client)).status_code == 200
    client.headers.update(SAME_ORIGIN)
    if step_up:
        assert (await client.post("/auth/step-up", json={"password": PASSWORD})
                ).status_code == 200


async def put(client, name: str, value: str, **fields):
    return await client.put(f"/admin/secrets/{name}", json={"value": value, **fields})


async def fetch(name: str, service: str = "satellites"):
    async with internal_client() as internal:
        return await internal.get(f"/internal/secrets/{name}",
                                  headers=bearer(service_key(service)))


async def offer(service: str, secrets: list[dict], *, declared: dict | None = None,
                final: bool = False):
    async with internal_client() as internal:
        return await internal.post("/internal/secrets/import",
                                   headers=bearer(service_key(service)),
                                   json={"secrets": secrets, "declared": declared or {},
                                         "final": final})


def entry(name: str, value: str, *, kind: str = "bearer", hosts=(), source=None) -> dict:
    found = {"name": name, "kind": kind, "value": value, "allowed_hosts": list(hosts)}
    if source:
        found["source"] = source
    return found


def listed(response, name: str) -> dict:
    return next(row for row in response.json()["secrets"] if row["name"] == name)


def audited(main, action: str) -> list:
    return main.runtime.get().db.all(
        "SELECT * FROM audit WHERE action = ? AND aggregated = 0 ORDER BY id", (action,))


def ciphertext(main, name: str) -> bytes:
    row = main.runtime.get().db.one("SELECT ciphertext FROM secrets WHERE name = ?", (name,))
    return bytes(row["ciphertext"])


def keyring_file(tmp_path, *keys: bytes):
    path = tmp_path / "calliope-master"
    path.write_bytes(b"".join(key + b"\n" for key in keys))
    return path


# ── encryption and serving ────────────────────────────────────────────────────


async def test_a_stored_value_reaches_its_consumer_and_is_encrypted_at_rest(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        stored = await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN,
                           consumers=["satellites"], allowed_hosts=["HA.lan.:8123"])
        served = await fetch("SATELLITES_HA_TOKEN")
        at_rest = ciphertext(main, "SATELLITES_HA_TOKEN")
        key = (main.runtime.get().settings.keys_dir / "master.keys").read_bytes().split()[0]

    assert stored.status_code == 201
    assert served.status_code == 200
    assert served.headers["cache-control"] == "no-store"
    assert served.json() == {"value": HA_TOKEN, "version": 1, "kind": "bearer",
                             "allowed_hosts": ["https://ha.lan:8123"], "max_age": 60}
    assert HA_TOKEN.encode() not in at_rest
    assert json.loads(Fernet(key).decrypt(at_rest)) \
        == {"n": "SATELLITES_HA_TOKEN", "v": HA_TOKEN}, "the name is not bound to the value"


async def test_a_ciphertext_swapped_between_rows_is_refused_not_served(monkeypatch):
    """The plaintext names its row (D39): a copied ciphertext opens to the wrong name."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        await put(client, "SATELLITES_LLM_API_KEY", LLM_KEY, consumers=["satellites"])
        db = main.runtime.get().db
        ha = ciphertext(main, "SATELLITES_HA_TOKEN")
        llm = ciphertext(main, "SATELLITES_LLM_API_KEY")
        db.execute("UPDATE secrets SET ciphertext = ? WHERE name = 'SATELLITES_HA_TOKEN'", (llm,))
        db.execute("UPDATE secrets SET ciphertext = ? WHERE name = 'SATELLITES_LLM_API_KEY'", (ha,))
        served = await fetch("SATELLITES_HA_TOKEN")
        again = await fetch("SATELLITES_HA_TOKEN")
        listing = await client.get("/admin/secrets")
        main.runtime.get().trail.flush()
        failures = db.all("SELECT * FROM audit WHERE action = 'secret_decrypt_failed'")

    assert served.status_code == again.status_code == 503
    assert served.json()["error"]["code"] == "undecryptable"
    assert LLM_KEY not in served.text and HA_TOKEN not in served.text
    assert listed(listing, "SATELLITES_HA_TOKEN")["undecryptable"] is True
    assert listed(listing, "SATELLITES_LLM_API_KEY")["undecryptable"] is True
    assert [(row["target"], row["outcome"], row["aggregated"]) for row in failures] \
        == [("SATELLITES_HA_TOKEN", "failed", 0)], "a consumer asking again was audited again"


async def test_a_row_no_key_opens_is_listed_not_served_until_it_is_stored_again(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        foreign = Fernet(Fernet.generate_key()).encrypt(
            json.dumps({"n": "SATELLITES_HA_TOKEN", "v": HA_TOKEN}).encode())
        main.runtime.get().db.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = 'SATELLITES_HA_TOKEN'", (foreign,))
        listing = await client.get("/admin/secrets")
        refused = await fetch("SATELLITES_HA_TOKEN")
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN)
        mended = await fetch("SATELLITES_HA_TOKEN")

    assert listed(listing, "SATELLITES_HA_TOKEN")["undecryptable"] is True
    assert listing.json()["counts"]["undecryptable"] == 1
    assert refused.status_code == 503 and HA_TOKEN not in refused.text
    assert mended.status_code == 200 and mended.json()["version"] == 2


async def test_a_cleared_secret_answers_404_at_once_and_keeps_its_bindings(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"],
                  allowed_hosts=["ha.lan:8123"])
        cleared = await client.delete("/admin/secrets/SATELLITES_HA_TOKEN")
        served = await fetch("SATELLITES_HA_TOKEN")
        row = listed(await client.get("/admin/secrets"), "SATELLITES_HA_TOKEN")
        missing = await client.delete("/admin/secrets/NEVER_SET")

    assert cleared.status_code == 204
    assert served.status_code == 404
    assert row["set"] is False
    assert (row["consumers"], row["allowed_hosts"]) == (["satellites"], ["https://ha.lan:8123"])
    assert missing.status_code == 404


async def test_a_service_that_is_not_a_consumer_gets_403_and_is_audited(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "TTS_RUNNER_API_KEY", RUNNER_KEY, consumers=["tts-long"])
        hub = await fetch("TTS_RUNNER_API_KEY", "satellites")
        stt = await fetch("TTS_RUNNER_API_KEY", "stt")
        owner = await fetch("TTS_RUNNER_API_KEY", "tts-long")
        denied = audited(main, "secret_fetch_denied")

    assert hub.status_code == 403 and hub.json()["error"]["code"] == "not_a_consumer"
    assert RUNNER_KEY not in hub.text
    assert stt.status_code == 403, "a service without secrets:fetch reached the store"
    assert owner.status_code == 200 and owner.json()["value"] == RUNNER_KEY
    assert [(row["target"], row["actor_id"]) for row in denied] \
        == [("TTS_RUNNER_API_KEY", "svc:satellites")]


async def test_a_fetch_is_audited_once_an_hour_per_version_and_every_read_is_on_the_row(
        monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        for _ in range(3):
            await fetch("SATELLITES_HA_TOKEN")
        after_three = len(audited(main, "secret_fetched"))
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN + "-new")
        await fetch("SATELLITES_HA_TOKEN")
        clock.advance(3600)
        await fetch("SATELLITES_HA_TOKEN")
        rows = audited(main, "secret_fetched")
        row = listed(await client.get("/admin/secrets", headers=SAME_ORIGIN),
                     "SATELLITES_HA_TOKEN")

    assert after_three == 1
    assert [json.loads(r["detail"])["version"] for r in rows] == [1, 2, 2]
    assert row["last_read_by"] == "svc:satellites" and row["last_read_at"]


# ── write-only ────────────────────────────────────────────────────────────────


async def test_no_value_appears_in_any_response_or_log_line(monkeypatch, caplog, capsys):
    """Only the consumer's fetch carries a value (D40); errors and the audit never do."""
    caplog.set_level(logging.DEBUG)
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        answers = [
            await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"]),
            await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, kind="no-such-kind"),
            await put(client, "SATELLITES_LLM_API_KEY", LLM_KEY + "\r\nX-Injected: 1"),
            await put(client, "SATELLITES_BUTTON_A", LLM_KEY, kind="secret_url"),
            await client.patch("/admin/secrets/SATELLITES_HA_TOKEN",
                               json={"allowed_hosts": ["ha.lan:8123"]}),
            await client.get("/admin/secrets"),
            await offer("tts-long", [entry("TTS_RUNNER_API_KEY", RUNNER_KEY,
                                           source="file TTS_RUNNER_API_KEY_FILE")]),
            await client.post("/admin/secrets/rotate-master"),
        ]
        served = await fetch("SATELLITES_HA_TOKEN")
        audit_rows = [dict(row) for row in main.runtime.get().db.all("SELECT * FROM audit")]
    printed = capsys.readouterr()

    assert served.json()["value"] == HA_TOKEN
    assert [a.status_code for a in answers] == [201, 422, 400, 400, 200, 200, 200, 200]
    for value in (HA_TOKEN, LLM_KEY, RUNNER_KEY):
        for answer in answers:
            assert value not in answer.text, answer.request.url
        assert value not in caplog.text
        assert value not in printed.out and value not in printed.err
        assert value not in json.dumps(audit_rows)


async def test_a_value_the_store_cannot_use_is_refused_by_field_and_never_repeated(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        answers = {
            "invalid_name": await put(client, "lower_case", HA_TOKEN),
            "invalid_consumer": await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN,
                                          consumers=["ui"]),
            "invalid_host": await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN,
                                      allowed_hosts=["https://ha.lan/api"]),
            "invalid_value": await put(client, "SATELLITES_BUTTON_A", "not a url " + HA_TOKEN,
                                       kind="secret_url"),
        }
        nothing = await client.patch("/admin/secrets/SATELLITES_HA_TOKEN", json={})

    for code, answer in answers.items():
        assert answer.status_code == 400, code
        assert answer.json()["error"]["code"] == code
        assert HA_TOKEN not in answer.text
    assert nothing.status_code == 400


async def test_the_store_has_a_ceiling_for_people_and_for_imports(monkeypatch):
    from app import secret_store
    monkeypatch.setattr(secret_store, "MAX_SECRETS", 1)
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        first = await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN)
        second = await put(client, "SATELLITES_LLM_API_KEY", LLM_KEY)
        imported = await offer("tts-long", [entry("TTS_RUNNER_API_KEY", RUNNER_KEY)])

    assert first.status_code == 201
    assert second.status_code == 409 and second.json()["error"]["code"] == "store_full"
    assert imported.json()["results"] == [{"name": "TTS_RUNNER_API_KEY",
                                           "outcome": "refused", "reason": "store_full"}]


# ── step-up and session-only (D13, D60) ───────────────────────────────────────


async def test_every_secret_write_needs_a_fresh_password_and_a_key_can_do_none_of_it(
        monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client, step_up=False)
        listing = await client.get("/admin/secrets")
        writes = [await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN),
                  await client.patch("/admin/secrets/SATELLITES_HA_TOKEN",
                                     json={"reviewed": True}),
                  await client.delete("/admin/secrets/SATELLITES_HA_TOKEN"),
                  await client.post("/admin/secrets/rotate-master")]
        key = bearer(make_key(make_user("root")))
        by_key = [await client.get("/admin/secrets", headers=key),
                  await client.put("/admin/secrets/SATELLITES_HA_TOKEN", headers=key,
                                   json={"value": HA_TOKEN})]

    assert listing.status_code == 200
    for refused in writes:
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "step_up_required"
    for refused in by_key:
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "session_required"


# ── allowed hosts (D41) ───────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["HA.lan.", "user@ha.lan", "ha.lan:443",
                                      "https://ha.lan", "https://HA.LAN:443/"])
def test_every_spelling_of_one_https_host_normalises_to_the_same_entry(spelling):
    from app.secret_store import normalise_host
    assert normalise_host(spelling) == normalise_host("https://ha.lan") == "https://ha.lan:443"


@pytest.mark.parametrize("spelling,expected", [
    ("http://ha.lan", "http://ha.lan:80"),
    ("ha.lan:8123", "https://ha.lan:8123"),
    ("bücher.example", "https://xn--bcher-kva.example:443"),
    ("[::1]:8123", "https://[::1]:8123"),
    ("192.0.2.10", "https://192.0.2.10:443"),
    ("http://homeassistant_1:8123", "http://homeassistant_1:8123"),
    # The hub's broker password is bound to its broker (D70).
    ("mqtt://broker", "mqtt://broker:1883"),
    ("mqtts://broker", "mqtts://broker:8883"),
])
def test_ports_schemes_and_international_names_are_written_out(spelling, expected):
    from app.secret_store import normalise_host
    assert normalise_host(spelling) == expected


def test_a_secret_url_is_an_http_address_and_never_a_broker():
    from app.secret_store import value_problem
    assert value_problem("secret_url", "https://ha.lan/api/webhook/x") is None
    assert value_problem("secret_url", "mqtt://broker/topic") is not None
    assert value_problem("password", "mqtt-password") is None


def test_an_entry_without_a_scheme_never_admits_plain_http():
    from app.secret_store import normalise_host
    assert normalise_host("ha.lan") != normalise_host("http://ha.lan")


@pytest.mark.parametrize("spelling", ["", "ftp://ha.lan", "https://ha.lan/api", "*.lan",
                                      "ha lan", "https://ha.lan:0", "https://ha.lan:99999",
                                      "https://", "ha.lan\\@evil.example", "ha.lan?x=1",
                                      # httpx refuses these two, so no entry admits them.
                                      "ex\u200dample.com", "\uff26\uff35\uff2c\uff2c.example"])
def test_what_is_not_a_host_is_refused(spelling):
    from app.secret_store import normalise_host
    with pytest.raises(ValueError):
        normalise_host(spelling)


@pytest.mark.parametrize("url", ["https://faß.de/x", "https://homeaßistant.example/api",
                                 "https://ςa.gr/", "https://Bücher.Example:8443/"])
def test_a_target_normalises_to_the_host_the_client_connects_to(url):
    """IDNA 2008, as httpx encodes it. Python's IDNA 2003 codec folds faß into fass."""
    import httpx
    from app.secret_store import normalise_host
    target = httpx.URL(url)
    assert normalise_host(url, entry=False) \
        == f"https://{target.raw_host.decode('ascii')}:{target.port or 443}"


def test_an_entry_never_admits_a_different_domain_that_folds_to_the_same_letters():
    from app.secret_store import normalise_host
    assert normalise_host("https://faß.de/x", entry=False) == "https://xn--fa-hia.de:443"
    assert normalise_host("https://faß.de/x", entry=False) != normalise_host("fass.de")
    assert normalise_host("https://homeaßistant.example/api", entry=False) \
        != normalise_host("homeassistant.example")


@pytest.mark.parametrize("spelling", ["010.0.0.1", "0x7f.0.0.1", "127.1", "2130706433",
                                      "1.2.3.4.5", "ha.0x10"])
def test_a_numeric_host_that_is_not_an_ip_address_is_refused(spelling):
    """A resolver reads these as octal, hex or short addresses: no entry may name one."""
    from app.secret_store import normalise_host
    with pytest.raises(ValueError):
        normalise_host(spelling)


def test_a_target_url_normalises_to_its_origin_and_userinfo_never_moves_the_host():
    from app.secret_store import normalise_host
    assert normalise_host(WEBHOOK, entry=False) == "https://ha.lan:8123"
    assert normalise_host("https://ha.lan@evil.example/x", entry=False) \
        == "https://evil.example:443"


# ── the import window (D44, D66) ──────────────────────────────────────────────


async def test_an_import_never_overwrites_a_stored_or_cleared_secret(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        await put(client, "SATELLITES_LLM_API_KEY", LLM_KEY, consumers=["satellites"])
        await client.delete("/admin/secrets/SATELLITES_LLM_API_KEY")
        offered = await offer("satellites", [
            entry("SATELLITES_HA_TOKEN", "from-the-environment"),
            entry("SATELLITES_LLM_API_KEY", "from-the-environment")])
        ha = await fetch("SATELLITES_HA_TOKEN")
        llm = await fetch("SATELLITES_LLM_API_KEY")

    assert [r["outcome"] for r in offered.json()["results"]] == ["exists", "exists"]
    assert ha.json()["value"] == HA_TOKEN
    assert llm.status_code == 404, "an import refilled a secret an admin cleared"


async def test_an_import_outside_the_services_allowlist_is_refused_and_audited(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        hub = await offer("satellites", [
            entry("TTS_RUNNER_API_KEY", RUNNER_KEY),           # tts-long's, by fixed rule
            entry("CALLIOPE_MASTER_KEY_FILE", RUNNER_KEY),     # the gateway's own
            entry("SOMETHING_UNDECLARED", RUNNER_KEY),
            entry("lower_case", RUNNER_KEY),
            entry("SATELLITES_HA_TOKEN", HA_TOKEN, kind="ssh_key")])
        runner = await offer("tts-long", [entry("TTS_RUNNER_API_KEY", RUNNER_KEY)])
        refused = audited(main, "secret_import_refused")
        main.runtime.get().trail.flush()
        counted = main.runtime.get().db.all(
            "SELECT detail FROM audit WHERE action = 'secret_import_refused' "
            "AND aggregated = 1")

    assert [(r["name"], r["outcome"], r.get("reason")) for r in hub.json()["results"]] == [
        ("TTS_RUNNER_API_KEY", "refused", "not_allowed"),
        ("CALLIOPE_MASTER_KEY_FILE", "refused", "not_allowed"),
        ("SOMETHING_UNDECLARED", "refused", "not_allowed"),
        (None, "refused", "invalid_name"),
        ("SATELLITES_HA_TOKEN", "refused", "invalid_kind")]
    assert runner.json()["results"][0]["outcome"] == "imported"
    assert [(row["target"], json.loads(row["detail"])["reason"]) for row in refused] == [
        ("TTS_RUNNER_API_KEY", "not_allowed"), ("CALLIOPE_MASTER_KEY_FILE", "not_allowed"),
        ("SOMETHING_UNDECLARED", "not_allowed"), (None, "invalid_name"),
        ("SATELLITES_HA_TOKEN", "invalid_kind")], "a name the hub tried to plant is not on record"
    assert sum(json.loads(row["detail"])["count"] for row in counted) == 5


async def test_a_flood_of_refused_names_is_named_up_to_a_ceiling_each_minute_and_counted(
        monkeypatch):
    from app import secret_store
    monkeypatch.setattr(secret_store, "NAMED_REFUSALS", 3)
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        await offer("satellites", [entry(f"PLANTED_{n}", RUNNER_KEY) for n in range(5)])
        await offer("satellites", [entry("PLANTED_0", RUNNER_KEY)])
        clock.advance(60)
        await offer("satellites", [entry("PLANTED_4", RUNNER_KEY)])
        named = audited(main, "secret_import_refused")
        main.runtime.get().trail.flush()
        counted = main.runtime.get().db.all(
            "SELECT detail FROM audit WHERE action = 'secret_import_refused' "
            "AND aggregated = 1")

    assert [row["target"] for row in named] == ["PLANTED_0", "PLANTED_1", "PLANTED_2",
                                                "PLANTED_4"]
    assert [json.loads(row["detail"])["count"] for row in counted] == [6, 1]


async def test_after_the_final_batch_import_answers_410_until_the_operator_reopens_it(
        monkeypatch):
    from app import admin
    from app.runtime import Settings
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        last = await offer("satellites", [entry("SATELLITES_HA_TOKEN", HA_TOKEN)],
                           final=True)
        closed = await offer("satellites", [entry("SATELLITES_LLM_API_KEY", LLM_KEY)])
        other_service = await offer("tts-long", [entry("TTS_RUNNER_API_KEY", RUNNER_KEY)])
        admin.reopen_import(Settings.from_env(), "satellites")
        reopened = await offer("satellites", [entry("SATELLITES_LLM_API_KEY", LLM_KEY)])
        closing = audited(main, "secret_import_closed")

    assert last.status_code == 200 and last.json()["closed"] is True
    assert closed.status_code == 410 and closed.json()["error"]["code"] == "import_closed"
    assert other_service.json()["results"][0]["outcome"] == "imported", \
        "one service's final batch closed another's window"
    assert reopened.json()["results"][0]["outcome"] == "imported"
    assert [row["target"] for row in closing] == ["satellites"]


async def test_an_import_asking_for_hosts_wider_than_its_config_is_narrowed(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        offered = await offer("satellites", [
            entry("SATELLITES_HA_TOKEN", HA_TOKEN,
                  hosts=["https://ha.lan:8123", "https://attacker.example"]),
            entry("SATELLITES_BUTTON_KORVO_1_A_PRESS", WEBHOOK, kind="secret_url",
                  hosts=["https://ha.lan:8123", "https://other.lan"]),
            entry("SATELLITES_LLM_API_KEY", LLM_KEY, hosts=["https://openrouter.ai"])],
            declared={"SATELLITES_HA_TOKEN": ["ha.lan:8123"],
                      "SATELLITES_BUTTON_KORVO_1_A_PRESS": ["https://ha.lan:8123",
                                                            "https://other.lan"]})
        await signed_in_admin(client)
        listing = await client.get("/admin/secrets")

    assert [r["outcome"] for r in offered.json()["results"]] == ["imported"] * 3
    assert listed(listing, "SATELLITES_HA_TOKEN")["allowed_hosts"] == ["https://ha.lan:8123"]
    assert listed(listing, "SATELLITES_BUTTON_KORVO_1_A_PRESS")["allowed_hosts"] \
        == ["https://ha.lan:8123"], "a webhook may go only to its own URL's host"
    assert listed(listing, "SATELLITES_LLM_API_KEY")["allowed_hosts"] == [], \
        "a host no configuration names was allowed"


async def test_an_imported_row_stays_unreviewed_until_an_admin_confirms_it(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await offer("satellites", [entry("SATELLITES_HA_TOKEN", HA_TOKEN,
                                         source="env SATELLITES_HA_TOKEN")])
        await signed_in_admin(client)
        imported = await client.get("/admin/secrets")
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN + "-replaced")
        replaced = await client.get("/admin/secrets")
        confirmed = await client.patch("/admin/secrets/SATELLITES_HA_TOKEN",
                                       json={"reviewed": True})
        reviews = audited(main, "secret_reviewed")
        served = await fetch("SATELLITES_HA_TOKEN")

    row = listed(imported, "SATELLITES_HA_TOKEN")
    assert row["unreviewed"] is True and imported.json()["counts"]["unreviewed"] == 1
    assert row["imported_from"] == "env SATELLITES_HA_TOKEN @ satellites"
    assert (row["consumers"], row["created_by"]) == (["satellites"], "svc:satellites")
    assert listed(replaced, "SATELLITES_HA_TOKEN")["unreviewed"] is True
    assert confirmed.json()["secret"]["unreviewed"] is False
    assert [row["target"] for row in reviews] == ["SATELLITES_HA_TOKEN"]
    assert served.json()["value"] == HA_TOKEN + "-replaced"


# ── the keyring (D8, D43) ─────────────────────────────────────────────────────


async def test_a_generated_keyring_warns_and_the_listing_says_so(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="voice-gateway.secrets")
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        keyring = (await client.get("/admin/secrets")).json()["keyring"]
        path = main.runtime.get().settings.keys_dir / "master.keys"

    assert keyring == {"source": "generated", "variable": "CALLIOPE_MASTER_KEY_FILE",
                       "keys": 1, "rotatable": True}
    assert "Back that volume up separately" in caplog.text
    assert path.stat().st_mode & 0o777 == 0o400


async def test_rotating_the_generated_master_key_changes_every_ciphertext_and_keeps_every_value(
        monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        await put(client, "SATELLITES_LLM_API_KEY", LLM_KEY, consumers=["satellites"])
        path = main.runtime.get().settings.keys_dir / "master.keys"
        before = {name: ciphertext(main, name) for name in ("SATELLITES_HA_TOKEN",
                                                             "SATELLITES_LLM_API_KEY")}
        old_keyring = path.read_bytes()
        rotated = await client.post("/admin/secrets/rotate-master")
        after = {name: ciphertext(main, name) for name in before}
        values = [(await fetch(name)).json()["value"] for name in before]
        new_keyring = path.read_bytes()
        audit = audited(main, "master_key_rotated")

    assert rotated.json() == {"rotated": 2, "undecryptable": []}
    assert all(before[name] != after[name] for name in before)
    assert values == [HA_TOKEN, LLM_KEY]
    assert new_keyring != old_keyring and len(new_keyring.splitlines()) == 1, \
        "the old key was not dropped once every row had moved"
    assert old_keyring.strip() not in new_keyring
    assert json.loads(audit[0]["detail"]) == {"rotated": 2, "undecryptable": 0}


async def test_a_new_first_line_in_the_keyring_file_re_encrypts_every_row_at_start(
        monkeypatch, tmp_path, caplog):
    old, new = Fernet.generate_key(), Fernet.generate_key()
    path = keyring_file(tmp_path, old)
    monkeypatch.setenv("CALLIOPE_MASTER_KEY_FILE", str(path))
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])
        refused = await client.post("/admin/secrets/rotate-master")
        before = ciphertext(main, "SATELLITES_HA_TOKEN")

    keyring_file(tmp_path, new, old)
    caplog.set_level(logging.INFO, logger="voice-gateway.secrets")
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        after = ciphertext(main, "SATELLITES_HA_TOKEN")
        moved = audited(main, "secrets_reencrypted")

    keyring_file(tmp_path, new)
    async with gateway(monkeypatch, authenticate=False):
        served = await fetch("SATELLITES_HA_TOKEN")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "keyring_from_file"
    assert after != before
    assert Fernet(new).decrypt(after)
    assert "re-encrypted 1 stored secret" in caplog.text
    assert json.loads(moved[0]["detail"]) == {"count": 1, "source": "file"}
    assert served.json()["value"] == HA_TOKEN, "the value did not survive the old key's removal"


async def test_a_generated_keyring_given_a_file_later_moves_every_row_under_the_file(
        monkeypatch, tmp_path):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        await put(client, "SATELLITES_HA_TOKEN", HA_TOKEN, consumers=["satellites"])

    key = Fernet.generate_key()
    monkeypatch.setenv("CALLIOPE_MASTER_KEY_FILE", str(keyring_file(tmp_path, key)))
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        served = await fetch("SATELLITES_HA_TOKEN")
        under_file = Fernet(key).decrypt(ciphertext(main, "SATELLITES_HA_TOKEN"))
        source = main.runtime.get().db.meta("keyring_source")
        changed = audited(main, "keyring_source_changed")

    assert served.json()["value"] == HA_TOKEN
    assert json.loads(under_file)["v"] == HA_TOKEN
    assert source == "file"
    assert json.loads(changed[0]["detail"]) == {"from": "generated", "to": "file"}


def _missing(tmp_path):
    return tmp_path / "not-there"


def _empty(tmp_path):
    return keyring_file(tmp_path)


def _not_a_key(tmp_path):
    path = tmp_path / "calliope-master"
    path.write_text("this is not a fernet key\n")
    return path


def _blank(tmp_path):
    return " "


def _unreadable(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = keyring_file(tmp_path, Fernet.generate_key())
    path.chmod(0)
    return path


@pytest.mark.parametrize("keyring", [_missing, _empty, _not_a_key, _unreadable, _blank],
                         ids=["missing", "empty", "not_a_key", "unreadable", "blank_variable"])
async def test_a_keyring_file_that_cannot_be_used_locks_the_gateway_and_never_generates_a_key(
        monkeypatch, tmp_path, keyring):
    monkeypatch.setenv("CALLIOPE_MASTER_KEY_FILE", str(keyring(tmp_path)))
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        page = await client.get("/admin/secrets", headers=SAME_ORIGIN)
        served = await fetch("SATELLITES_HA_TOKEN")
        imported = await offer("tts-long", [entry("TTS_RUNNER_API_KEY", RUNNER_KEY)])
        health = await client.get("/health")
        generated = (rt.settings.keys_dir / "master.keys").exists()

    assert ("keyring_unreadable", "CALLIOPE_MASTER_KEY_FILE") in rt.lock.reasons
    assert not generated, "a key was generated in place of the operator's file"
    for refused in (page, served, imported):
        assert refused.status_code == 503
        assert (refused.json()["reason"], refused.json()["variable"]) \
            == ("keyring_unreadable", "CALLIOPE_MASTER_KEY_FILE")
    assert health.status_code == 200 and health.json() == {"status": "degraded"}


async def test_a_generated_keyring_that_cannot_be_read_is_never_written_over(monkeypatch):
    """It is the only copy of the key: a fresh one in its place would lose every secret."""
    path = Path(os.environ["CALLIOPE_KEYS_DIR"]) / "master.keys"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("half a key\n")
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        reasons = main.runtime.get().lock.reasons
        served = await fetch("SATELLITES_HA_TOKEN")

    assert ("keyring_unreadable", "CALLIOPE_MASTER_KEY_FILE") in reasons
    assert served.status_code == 503
    assert path.read_text() == "half a key\n"


async def test_a_keyring_file_used_once_is_never_replaced_by_a_generated_one(
        monkeypatch, tmp_path):
    monkeypatch.setenv("CALLIOPE_MASTER_KEY_FILE",
                       str(keyring_file(tmp_path, Fernet.generate_key())))
    async with gateway(monkeypatch, authenticate=False):
        pass
    monkeypatch.delenv("CALLIOPE_MASTER_KEY_FILE")
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        rt = main.runtime.get()
        served = await fetch("SATELLITES_HA_TOKEN")

    assert ("keyring_unreadable", "CALLIOPE_MASTER_KEY_FILE") in rt.lock.reasons
    assert not (rt.settings.keys_dir / "master.keys").exists()
    assert served.status_code == 503


# ── status rows (D46) ─────────────────────────────────────────────────────────


def _certificate(path, days: int) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "calliope.test")])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(1)
                   .not_valid_before(now - timedelta(days=1))
                   .not_valid_after(now + timedelta(days=days))
                   .sign(key, hashes.SHA256()))
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _health_reply(body: dict):
    return lambda record: (200, {"content-type": "application/json"},
                           json.dumps(body).encode())


async def test_the_status_rows_show_the_certificate_expiry_and_what_the_services_report(
        monkeypatch, tmp_path):
    _certificate(tmp_path / "cert.pem", days=10)
    monkeypatch.setenv("GATEWAY_TLS_CERT", str(tmp_path / "cert.pem"))
    long, hub = MockBackend("tts-long"), MockBackend("voice-satellites")
    long.reply = _health_reply({"status": "ok", "runner_key_file": True})
    hub.reply = _health_reply({"status": "ok", "firmware_key_id": "9f86d081884c7d65"})
    async with gateway(monkeypatch, long=long, satellites=hub,
                       authenticate=False) as (client, _):
        await signed_in_admin(client)
        rows = {row["id"]: row for row in (await client.get("/admin/secrets")).json()["status"]}

    expires = datetime.strptime(rows["tls_certificate"]["expires_at"],
                                "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert rows["tls_certificate"]["state"] == "expiring"
    assert abs(expires - (datetime.now(UTC) + timedelta(days=10))) < timedelta(minutes=1)
    assert rows["runner_key_file"]["state"] == "present"
    assert (rows["firmware_signing_key"]["state"], rows["firmware_signing_key"]["key_id"]) \
        == ("configured", "9f86d081884c7d65")


async def test_a_service_that_does_not_report_a_status_row_shows_unknown(monkeypatch):
    monkeypatch.delenv("GATEWAY_TLS_CERT", raising=False)
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in_admin(client)
        rows = {row["id"]: row["state"]
                for row in (await client.get("/admin/secrets")).json()["status"]}

    assert rows == {"tls_certificate": "not_configured", "runner_key_file": "unknown",
                    "firmware_signing_key": "unknown"}
