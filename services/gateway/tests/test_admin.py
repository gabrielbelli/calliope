"""Admin › Users, Roles, Keys and Audit, and the operator's CLI (§3.2, D20, D24, D25, D68)."""

from __future__ import annotations

import pytest
from conftest import (PASSWORD, SAME_ORIGIN, MockBackend, bearer, gateway, make_key, make_user,
                      sign_in)


async def admin_page(client):
    make_user("ana")
    await sign_in(client)
    client.headers.update(SAME_ORIGIN)
    assert (await client.post("/auth/step-up", json={"password": PASSWORD})
            ).status_code == 200


async def test_changing_a_user_needs_a_fresh_password(monkeypatch):
    """Step-up (D13): an admin session left open must not quietly make users."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        client.headers.update(SAME_ORIGIN)
        listed = await client.get("/admin/users")
        refused = await client.post("/admin/users", json={"username": "ben",
                                                          "role": "user-jobs"})

    assert listed.status_code == 200
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "step_up_required"


async def test_a_new_user_gets_a_temporary_password_shown_once(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await admin_page(client)
        created = await client.post("/admin/users", json={"username": "ben",
                                                          "role": "user-jobs"})
        listing = await client.get("/admin/users")
        import httpx
        ben = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url))
        first = await sign_in(ben, "ben", created.json()["temporary_password"])
        await ben.aclose()

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    temporary = created.json()["temporary_password"]
    assert temporary not in listing.text
    assert first.json()["must_change"] is True


async def test_a_reset_ends_every_session_and_by_default_every_key(monkeypatch):
    """A reset is what an admin does when an account is suspected (D20)."""
    import httpx
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        key = make_key(ben, scopes={"models:read"})
        browser = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url),
                                    headers=SAME_ORIGIN)
        await sign_in(browser, "ben")
        reset = await client.post(f"/admin/users/{ben['id']}/reset-password", json={})
        session_after = await browser.get("/auth/me")
        key_after = await client.get("/v1/models", headers=bearer(key))
        await browser.aclose()

    assert reset.status_code == 200
    assert reset.headers["cache-control"] == "no-store"
    assert session_after.status_code == 401
    assert key_after.status_code == 401
    assert reset.json()["user"]["must_change"] is True


async def test_enabling_a_disabled_user_again_does_not_bring_back_their_sessions_or_keys(
        monkeypatch):
    """Disabling is the incident response; a stolen cookie or key must stay dead."""
    import httpx
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        key = make_key(ben, scopes={"models:read"})
        browser = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url),
                                    headers=SAME_ORIGIN)
        await sign_in(browser, "ben")
        disabled = await client.patch(f"/admin/users/{ben['id']}", json={"disabled": True})
        enabled = await client.patch(f"/admin/users/{ben['id']}", json={"disabled": False})
        session_after = await browser.get("/auth/me")
        key_after = await client.get("/v1/models", headers=bearer(key))
        await browser.aclose()

    assert disabled.status_code == enabled.status_code == 200
    assert enabled.json()["user"]["disabled"] is False
    assert session_after.status_code == 401
    assert key_after.status_code == 401


async def test_a_reset_can_keep_the_keys(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        key = make_key(ben, scopes={"models:read"})
        await client.post(f"/admin/users/{ben['id']}/reset-password",
                          json={"revoke_keys": False})
        after = await client.get("/v1/models", headers=bearer(key))
    assert after.status_code == 200


async def test_deleting_a_user_is_soft_and_the_audit_still_names_the_actor(monkeypatch):
    """The row, the username and every audit row naming the person stay (D68)."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await admin_page(client)
        ben = make_user("ben")
        rt = main.runtime.get()
        rt.trail.record(action="example", outcome="ok",
                        actor=main.authn.Actor("user", ben["id"], "session"))
        key = make_key(ben)
        deleted = await client.delete(f"/admin/users/{ben['id']}")
        again = await client.post("/admin/users", json={"username": "ben",
                                                        "role": "user-jobs"})
        row = rt.db.one("SELECT * FROM users WHERE id = ?", (ben["id"],))
        named = rt.db.one("SELECT COUNT(*) AS n FROM audit WHERE actor_id = ?", (ben["id"],))
        key_after = await client.get("/v1/models", headers=bearer(key))

    assert deleted.status_code == 204
    assert row is not None and row["deleted_at"] and row["disabled_at"]
    assert again.status_code == 409, "a deleted user's name was handed out again"
    assert named["n"] >= 1
    assert key_after.status_code == 401


async def test_the_last_admin_cannot_be_removed_and_nobody_demotes_themselves(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        # The bootstrap admin is an admin too; make ana the only active one.
        rt.db.execute("UPDATE users SET disabled_at = '2026-01-01T00:00:00Z' "
                      "WHERE username = 'admin'")
        await admin_page(client)
        me = (await client.get("/auth/me")).json()["user"]["id"]
        demote_self = await client.patch(f"/admin/users/{me}", json={"role": "user-jobs"})
        disable_self = await client.patch(f"/admin/users/{me}", json={"disabled": True})
        delete_self = await client.delete(f"/admin/users/{me}")
        ben = make_user("ben")
        demote_other = await client.patch(f"/admin/users/{ben['id']}",
                                          json={"role": "user-jobs"})
        demote_ben_again = await client.patch(f"/admin/users/{ben['id']}",
                                              json={"role": "user-jobs"})

    assert [r.status_code for r in (demote_self, disable_self, delete_self)] == [409] * 3
    assert demote_self.json()["error"]["code"] == "self"
    assert demote_other.status_code == 200
    assert demote_ben_again.status_code == 200


async def test_the_last_active_admin_cannot_be_demoted_by_another(monkeypatch):
    from app import users

    async with gateway(monkeypatch, authenticate=False) as (_, main):
        db = main.runtime.get().db
        db.execute("UPDATE users SET disabled_at = '2026-01-01T00:00:00Z'")
        last = make_user("ana")
        with pytest.raises(users.Refused) as refused:
            users.update(db, actor_id=None, user_id=last["id"], role="user-jobs")
    assert refused.value.code == "last_admin"


async def test_roles_are_the_code_constants_with_session_only_scopes_marked(monkeypatch):
    from voice_common import scopes as scope_rules

    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        roles = (await client.get("/admin/roles")).json()

    assert set(roles["session_only"]) == scope_rules.SESSION_ONLY
    assert roles["presets"]["home-assistant"]["max_expiry_days"] == 365
    assert roles["presets"]["admin"]["max_expiry_days"] == 90
    assert roles["presets"]["monitor"]["roles"] == ["admin"]
    assert list(roles["roles"]) == ["admin", "user", "user-jobs"]
    assert roles["presets"]["read-only"]["roles"] == ["admin", "user-jobs"]
    assert roles["presets"]["user"]["roles"] == ["admin", "user", "user-jobs"]
    assert roles["presets"]["user-jobs"]["roles"] == ["admin", "user-jobs"]
    assert "speech" not in roles["roles"] and "speech" not in roles["presets"]


async def test_an_admin_sees_every_key_but_never_a_plaintext(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        plaintext = make_key(ben, scopes={"models:read"})
        listed = await client.get("/admin/keys")
        mine = await client.get(f"/admin/keys?user={ben['id']}")
        bad = await client.get("/admin/keys?user=../x")
        key_id = mine.json()["keys"][0]["id"]
        revoked = await client.delete(f"/admin/keys/{key_id}")
        after = await client.get("/v1/models", headers=bearer(plaintext))

    assert plaintext not in listed.text
    assert [k["username"] for k in mine.json()["keys"]] == ["ben"]
    assert bad.status_code == 400
    assert revoked.status_code == 204 and after.status_code == 401


async def test_a_monitor_key_reads_the_audit_and_a_persons_key_does_not(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        monitor = make_key(make_user("ana"), scopes=scope_rules_preset("monitor"))
        persons = make_key(make_user("sam", role="user-jobs"), scopes={"models:read"})
        allowed = await client.get("/admin/audit", headers=bearer(monitor))
        refused = await client.get("/admin/audit", headers=bearer(persons))

    assert allowed.status_code == 200 and "rows" in allowed.json()
    assert refused.status_code == 403


# ── the three roles ───────────────────────────────────────────────────────────


def test_the_roles_the_api_takes_are_the_roles_in_scopes_py():
    """routes_admin spells them out for pydantic; users.py reads scopes.py."""
    from typing import get_args

    from voice_common import scopes as scope_rules

    from app import routes_admin, users

    assert get_args(routes_admin.Role) == users.ROLES == tuple(scope_rules.ROLES) \
        == ("admin", "user", "user-jobs")


@pytest.mark.parametrize("role", ["admin", "user", "user-jobs"])
async def test_an_admin_can_make_a_person_of_each_role(monkeypatch, role):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        created = await client.post("/admin/users", json={"username": "ben", "role": role})
        listed = (await client.get("/admin/users")).json()["users"]

    assert created.status_code == 201
    assert created.json()["user"]["role"] == role
    assert [u["role"] for u in listed if u["username"] == "ben"] == [role]


@pytest.mark.parametrize("role", ["speech", "jobs", ""])
async def test_a_role_that_is_not_one_of_the_three_is_refused(monkeypatch, role):
    """`speech` is gone (migration 0002), and is refused like any typo."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        created = await client.post("/admin/users", json={"username": "cal", "role": role})
        changed = await client.patch(f"/admin/users/{ben['id']}", json={"role": role})
        after = (await client.get("/admin/users")).json()["users"]

    assert created.status_code == changed.status_code == 422, (created.text, changed.text)
    assert "'admin', 'user' or 'user-jobs'" in created.text
    assert [u["role"] for u in after if u["username"] == "ben"] == ["user-jobs"]
    assert not [u for u in after if u["username"] == "cal"]


async def test_the_model_layer_refuses_an_unknown_role_naming_the_three(monkeypatch):
    from app import users

    async with gateway(monkeypatch, authenticate=False) as (_, main):
        db = main.runtime.get().db
        with pytest.raises(users.Refused) as refused:
            users.create(db, username="cal", role="speech", password_hash=None,
                         must_change=False, created_by=None)
    assert refused.value.code == "invalid_role"
    assert refused.value.message == "The role is admin, user or user-jobs."


async def test_moving_a_person_to_user_takes_the_jobs_off_their_keys_at_once(monkeypatch):
    """The key is intersected with the owner's role on every request (D29)."""
    long = MockBackend("tts-long")
    async with gateway(monkeypatch, long=long, authenticate=False) as (client, _):
        await admin_page(client)
        ben = make_user("ben", role="user-jobs")
        key = make_key(ben, scopes=scope_rules_preset("user-jobs"))
        before = await client.get("/jobs", headers=bearer(key))
        moved = await client.patch(f"/admin/users/{ben['id']}", json={"role": "user"})
        after = await client.get("/jobs", headers=bearer(key))
        models = await client.get("/v1/models", headers=bearer(key))

    assert before.status_code == 200
    assert moved.status_code == 200 and moved.json()["user"]["role"] == "user"
    assert after.status_code == 403
    assert after.json()["error"]["code"] == "insufficient_scope"
    assert models.status_code == 200


def scope_rules_preset(name: str) -> frozenset[str]:
    from voice_common import scopes as scope_rules
    return scope_rules.PRESETS[name].scopes


# ── the CLI ───────────────────────────────────────────────────────────────────


async def test_the_cli_reset_prints_a_password_that_must_be_changed(monkeypatch, capsys):
    from app import admin

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        ben = make_user("ben", role="user-jobs")
        key = make_key(ben, scopes={"models:read"})
        admin.main(["reset-password", "ben", "--revoke-keys"])
        printed = capsys.readouterr().out
        temporary = printed.split("\n\n")[1].strip()
        signed = await sign_in(client, "ben", temporary)
        key_after = await client.get("/v1/models", headers=bearer(key))
        row = main.runtime.get().db.one(
            "SELECT actor_kind FROM audit WHERE action = 'password_reset'")

    assert signed.json()["must_change"] is True
    assert key_after.status_code == 401
    assert row["actor_kind"] == "cli"


@pytest.mark.parametrize("owner", [1000, 0])
def test_the_cli_started_as_root_becomes_the_owner_of_the_keys_volume(
        monkeypatch, tmp_path, owner):
    """A key written as root is one the gateway cannot read, and stops on at
    its next restart; a root-owned volume means the gateway runs as root."""
    import os
    import types

    from app import admin

    became: list[tuple[str, object]] = []
    real_stat = type(tmp_path).stat
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(type(tmp_path), "stat", lambda self, **kw: types.SimpleNamespace(
        st_uid=owner, st_gid=owner) if self == tmp_path else real_stat(self, **kw))
    for call in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, call, lambda value, call=call: became.append((call, value)))
    admin.run_as_the_gateway(types.SimpleNamespace(keys_dir=tmp_path))

    assert became == ([("setgroups", []), ("setgid", 1000), ("setuid", 1000)]
                      if owner else [])


async def test_the_cli_lists_users_and_reopens_an_import_window(monkeypatch, capsys):
    from app import admin

    async with gateway(monkeypatch, authenticate=False) as (_, main):
        db = main.runtime.get().db
        db.set_meta("import_done.satellites", "1")
        admin.main(["list-users"])
        admin.main(["reopen-import", "satellites"])
        printed = capsys.readouterr().out
        reopened = db.meta("import_done.satellites")
        with pytest.raises(SystemExit):
            admin.main(["reopen-import", "stt"])

    assert "admin" in printed
    assert reopened is None


async def test_the_cli_rotates_service_keys_and_the_old_one_stops(monkeypatch):
    from app import admin
    from conftest import internal_client, service_key

    async with gateway(monkeypatch, authenticate=False):
        old = service_key("satellites")
        admin.main(["rotate-service-keys", "satellites"])
        new = service_key("satellites")
        async with internal_client() as internal:
            refused = await internal.get("/voices", headers=bearer(old))
            allowed = await internal.get("/voices", headers=bearer(new))

    assert old != new
    assert refused.status_code == 401
    assert allowed.status_code == 200
