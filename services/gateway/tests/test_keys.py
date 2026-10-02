"""API keys: their shape, their scopes, and what they can never do (D26-D30, D60, H1).

H1 was the review's first finding: an `admin` key held users:manage,
secrets:manage and keys:manage:own, so a 90-day key was a permanent step-up
bypass, and a leaked key could mint keys that outlived its own revocation.
Every test marked H1 is one way that could come back.
"""

from __future__ import annotations

import json

import pytest
from conftest import (PASSWORD, SAME_ORIGIN, Clock, MockBackend, bearer, gateway, make_key,
                      make_user, sign_in)
from voice_common import scopes as scope_rules


async def admin_page(client, *, step_up: bool = True):
    """Signed in as the admin `ana`, with a fresh step-up unless told otherwise."""
    make_user("ana")
    await sign_in(client)
    client.headers.update(SAME_ORIGIN)
    if step_up:
        assert (await client.post("/auth/step-up", json={"password": PASSWORD})
                ).status_code == 200


# ── shape and storage ─────────────────────────────────────────────────────────


async def test_a_key_is_shown_once_and_stored_only_as_a_hash(monkeypatch):
    from app import tokens

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("sam", role="speech")
        await sign_in(client, "sam")
        client.headers.update(SAME_ORIGIN)
        created = await client.post("/auth/keys", json={"name": "laptop",
                                                        "preset": "speech"})
        listed = await client.get("/auth/keys")
        row = main.runtime.get().db.one("SELECT * FROM api_keys")

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    plaintext = created.json()["plaintext"]
    assert tokens.well_formed(plaintext, tokens.USER_KEY_PREFIX)
    assert row["hash"] == tokens.digest(plaintext)
    assert plaintext not in json.dumps(dict(row))
    assert plaintext not in listed.text
    assert listed.json()["keys"][0]["display"].startswith("calliope_")
    assert "…" in listed.json()["keys"][0]["display"]


async def test_a_key_with_one_character_wrong_is_refused_by_its_checksum(monkeypatch):
    """A typo is refused by shape, before any lookup (D26)."""
    from app import tokens

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        key = make_key(make_user("ana"))
        flipped = key[:-8] + ("A" if key[-8] != "A" else "B") + key[-7:]
        lookups: list[str] = []
        db = main.runtime.get().db
        real_one = db.one
        monkeypatch.setattr(db, "one", lambda sql, params=(): (
            lookups.append(sql) if "api_keys" in sql else None) or real_one(sql, params))
        response = await client.get("/v1/models", headers=bearer(flipped))

    assert not tokens.well_formed(flipped, tokens.USER_KEY_PREFIX)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert not lookups, "a malformed key cost a database lookup"


async def test_an_expired_key_says_so(monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        key = make_key(make_user("ana"), days=30)
        before = await client.get("/v1/models", headers=bearer(key))
        clock.advance(31 * 24 * 3600)
        after = await client.get("/v1/models", headers=bearer(key))

    assert before.status_code == 200
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "api_key_expired"


async def test_a_revoked_key_stops_at_once(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await admin_page(client, step_up=False)
        created = (await client.post("/auth/keys", json={"name": "x", "preset": "read-only"})
                   ).json()
        key = created["plaintext"]
        before = await client.get("/v1/models", headers=bearer(key))
        await client.delete(f"/auth/keys/{created['key']['id']}")
        after = await client.get("/v1/models", headers=bearer(key))

    assert before.status_code == 200
    assert after.status_code == 401


async def test_a_key_can_do_only_what_its_owner_can_do_now(monkeypatch):
    """Effective scopes are the key's ∩ the owner's CURRENT role (D29): a
    demotion narrows every existing key, and a disabled owner's keys stop."""
    from app import users

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        make_user("root")
        owner = make_user("ana")
        key = make_key(owner)
        before = await client.get("/satellites", headers=bearer(key))
        users.update(rt.db, actor_id=None, user_id=owner["id"], role="speech")
        demoted = await client.get("/satellites", headers=bearer(key))
        still = await client.get("/v1/models", headers=bearer(key))
        users.update(rt.db, actor_id=None, user_id=owner["id"], disabled=True)
        disabled = await client.get("/v1/models", headers=bearer(key))

    assert before.status_code == 200
    assert demoted.status_code == 403
    assert still.status_code == 200
    assert disabled.status_code == 401


async def test_last_used_is_written_at_most_once_a_minute(monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        key = make_key(make_user("ana"))
        db = main.runtime.get().db
        await client.get("/v1/models", headers=bearer(key))
        first = db.one("SELECT last_used_at FROM api_keys")["last_used_at"]
        clock.advance(30)
        await client.get("/v1/models", headers=bearer(key))
        unchanged = db.one("SELECT last_used_at FROM api_keys")["last_used_at"]
        clock.advance(31)
        await client.get("/v1/models", headers=bearer(key))
        moved = db.one("SELECT last_used_at, last_used_ip FROM api_keys")

    assert first is not None and unchanged == first
    assert moved["last_used_at"] > first
    assert moved["last_used_ip"] == "192.0.2.1"


async def test_a_key_polling_health_keeps_the_address_it_was_used_from(monkeypatch):
    """Home Assistant's coordinator polls /health with its key: the key used
    most must not be the one that shows no address."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        key = make_key(make_user("ana"))
        await client.get("/health", headers=bearer(key))
        row = main.runtime.get().db.one("SELECT last_used_at, last_used_ip FROM api_keys")

    assert row["last_used_at"] is not None
    assert row["last_used_ip"] == "192.0.2.1"


# ── H1: session-only scopes ───────────────────────────────────────────────────


@pytest.mark.parametrize("scope", sorted(scope_rules.SESSION_ONLY))
async def test_no_session_only_scope_can_be_granted_to_a_key(monkeypatch, scope):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        response = await client.post("/auth/keys", json={
            "name": "x", "scopes": ["models:read", scope], "expires_days": 30})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "scope_not_grantable"
    assert scope in response.json()["error"]["message"]


async def test_a_speech_user_cannot_give_a_key_the_power_to_make_keys(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("sam", role="speech")
        await sign_in(client, "sam")
        client.headers.update(SAME_ORIGIN)
        response = await client.post("/auth/keys", json={
            "name": "x", "scopes": ["keys:manage:own"]})
        beyond = await client.post("/auth/keys", json={
            "name": "x", "scopes": ["satellites:read"]})

    assert response.status_code == beyond.status_code == 400
    assert response.json()["error"]["code"] == "scope_not_grantable"


async def test_a_key_cannot_create_keys(monkeypatch):
    """Whatever it holds: a leaked key must not mint keys that outlive it."""
    async with gateway(monkeypatch) as (client, _):
        response = await client.post("/auth/keys", json={"name": "x",
                                                         "preset": "read-only"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "session_required"


async def test_every_session_only_row_refuses_a_key_holding_every_scope(monkeypatch):
    """A key row written straight into the database with every scope there
    is, session-only ones included, still cannot reach a single session-only
    route: the row is refused before the scope check (D60), and effective
    scopes drop session-only ones anyway (D29)."""
    from app import routetable, tokens
    from app.db import iso

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        owner = make_user("ana")
        plaintext = tokens.mint(tokens.USER_KEY_PREFIX)
        main.runtime.get().db.execute(
            "INSERT INTO api_keys (id, user_id, name, hash, display, scopes, created_at, "
            "created_by) VALUES ('k_aaaaaaaaaaaa', ?, 'forged', ?, 'x', ?, ?, 'test')",
            (owner["id"], tokens.digest(plaintext), json.dumps(sorted(scope_rules.SCOPES)),
             iso(0)))
        rows = [(method, path) for method, path, rule in routetable.rules_of(main.app)
                if rule.session_only and method != routetable.WEBSOCKET]
        answers = {}
        for method, path in rows:
            concrete = path.replace("{rest:path}", "x").replace("{user_id}", owner["id"])
            for name in ("ref", "key_id", "name"):
                concrete = concrete.replace("{" + name + "}", "x")
            answers[(method, path)] = await client.request(
                method, concrete, headers=bearer(plaintext), json={})

    assert rows, "found no session-only rows"
    wrong = {row: r.status_code for row, r in answers.items()
             if r.status_code != 403 or r.json()["error"]["code"] != "session_required"}
    assert not wrong, wrong


async def test_an_admin_preset_key_cannot_manage_users(monkeypatch):
    async with gateway(monkeypatch) as (client, _):
        response = await client.post("/admin/users", json={"username": "eve",
                                                            "role": "admin"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "session_required"


async def test_the_admin_preset_is_the_admin_role_without_session_only_scopes(monkeypatch):
    assert scope_rules.PRESETS["admin"].scopes == \
        scope_rules.ROLES["admin"] - scope_rules.SESSION_ONLY


# ── what a key may be created with ────────────────────────────────────────────


async def test_an_admin_only_scope_needs_a_step_up(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client, step_up=False)
        without = await client.post("/auth/keys", json={"name": "ha",
                                                        "preset": "home-assistant"})
        await client.post("/auth/step-up", json={"password": PASSWORD})
        stepped = await client.post("/auth/keys", json={"name": "ha",
                                                        "preset": "home-assistant"})

    assert without.status_code == 403
    assert without.json()["error"]["code"] == "step_up_required"
    assert stepped.status_code == 201


async def test_the_home_assistant_preset_may_live_a_year_but_not_forever_and_a_monitor_key_may_not(
        monkeypatch):
    """Assist must not break every 90 days for nothing (recheck M-5), but a key
    that can push firmware must still expire; audit and every :all scope are
    capped at 90."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ha = await client.post("/auth/keys", json={"name": "ha", "preset": "home-assistant",
                                                   "expires_days": 365})
        ha_forever = await client.post("/auth/keys", json={
            "name": "ha", "preset": "home-assistant", "expires_days": None})
        monitor = await client.post("/auth/keys", json={"name": "m", "preset": "monitor",
                                                        "expires_days": 365})
        forever = await client.post("/auth/keys", json={"name": "m", "preset": "admin",
                                                        "expires_days": None})

    assert ha.status_code == 201
    assert monitor.status_code == forever.status_code == ha_forever.status_code == 400
    assert monitor.json()["error"]["param"] == "expires_days"


async def test_a_preset_outside_the_role_is_refused(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("sam", role="speech")
        await sign_in(client, "sam")
        client.headers.update(SAME_ORIGIN)
        response = await client.post("/auth/keys", json={"name": "x", "preset": "monitor"})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "preset"


async def test_an_unrecognised_scope_is_refused_and_never_repeated(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        response = await client.post("/auth/keys", json={
            "name": "x", "scopes": ["models:read", "<script>alert(1)</script>"]})

    assert response.status_code == 400
    assert "<script>" not in response.text


async def test_the_long_lane_needs_its_own_scope(monkeypatch):
    """speech:speak covers Kokoro; queueing minutes of GPU work through
    /v1/audio/speech with a long-form model needs speech:long (§1.5)."""
    long = MockBackend("tts-long")
    async with gateway(monkeypatch, long=long, authenticate=False) as (client, _):
        key = make_key(make_user("sam", role="speech"),
                       scopes={"speech:speak", "models:read"})
        fast = await client.post("/v1/audio/speech", headers=bearer(key),
                                 json={"model": "kokoro", "input": "Hi."})
        slow = await client.post("/v1/audio/speech", headers=bearer(key),
                                 json={"model": "chatterbox", "input": "Hi."})

    assert fast.status_code == 200
    assert slow.status_code == 403
    assert slow.headers["www-authenticate"].endswith('scope="speech:long"')
    assert not long.seen
