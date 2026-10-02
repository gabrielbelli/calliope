"""Signing in: the login route, the bootstrap, throttling and sessions.

D11-D24, D55, D61 and the recheck's M-6, M-7 and L6. Each test is a rule a
person or an attacker would meet from the outside, driven through the real
app with the page's own headers; the browser-isolation rules (CSRF, resource
isolation) are in test_browser.py and API keys in test_keys.py.
"""

from __future__ import annotations

import pytest
from conftest import (ADMIN_PASSWORD, PASSWORD, SAME_ORIGIN, Clock, bearer, gateway,
                      make_key, make_user, service_key, sign_in)

COOKIE = "__Host-calliope_session"


def page(client):
    """The client as the page uses it: same-origin Fetch Metadata on every call."""
    client.headers.update(SAME_ORIGIN)
    return client


# ── the login route ───────────────────────────────────────────────────────────


async def test_a_login_sets_a_host_only_secure_session_cookie(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        response = await sign_in(client)
        me = await page(client).get("/auth/me")

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Lax",
                      f"Max-Age={365 * 24 * 3600}"):
        assert attribute in cookie, attribute
    assert "Domain" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert me.json()["user"]["username"] == "ana"
    assert "users:manage" in me.json()["scopes"]


async def test_every_login_failure_reads_the_same(monkeypatch):
    """Which accounts exist is not something the login page says (D19)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        wrong = await sign_in(client, "ana", "not the password at all")
        unknown = await sign_in(client, "nobody", PASSWORD)

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["message"] == "Incorrect username or password."


async def test_every_login_path_costs_one_argon2_verify(monkeypatch):
    """A known user, an unknown one and the bootstrap admin each pay exactly
    one verify, so the time a login takes does not say which it was (D19, D21)."""
    from app import passwords

    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        calls: list[str] = []
        real = passwords.Hasher.verify_sync

        def counted(password, phc):
            calls.append("argon2")
            return real(password, phc)

        monkeypatch.setattr(passwords.Hasher, "verify_sync", staticmethod(counted))
        paths = {}
        for name, password in (("ana", "wrong but long enough"),
                               ("nobody", "wrong but long enough"),
                               ("admin", "wrong but long enough"),
                               ("admin", ADMIN_PASSWORD)):
            calls.clear()
            await sign_in(client, name, password)
            paths[(name, password)] = list(calls)

    assert all(spent == ["argon2"] for spent in paths.values()), paths


async def test_a_refused_login_body_never_echoes_the_password(monkeypatch):
    """A 422 used to carry pydantic's `input`, the rejected value itself (D55)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.post(
            "/auth/login", headers=SAME_ORIGIN,
            json={"username": ["not", "a", "string"], "password": "hunter2-but-longer-xyz",
                  "surplus": "hunter2-but-longer-xyz"})

    assert response.status_code == 422
    assert "hunter2" not in response.text


@pytest.mark.parametrize("value,expected", [
    ("//evil.example", "/ui"), ("/\\evil.example", "/ui"),
    ("https://evil.example", "/ui"), ("/%0d", "/ui"), ("/\r\nx", "/ui"),
    ("/ui/jobs/abc", "/ui/jobs/abc"), (None, "/ui"),
])
async def test_next_is_honoured_only_as_a_relative_path(monkeypatch, value, expected):
    """An open redirect from the login page would hand a phishing link this
    host name's credibility (D55)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        response = await client.post("/auth/login", headers=SAME_ORIGIN,
                                     json={"username": "ana", "password": PASSWORD,
                                           "next": value})
    assert response.json()["next"] == expected


async def test_an_invalid_bearer_never_falls_back_to_the_cookie(monkeypatch):
    """A header that does not authenticate is a 401 even beside a good cookie (D15)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        response = await page(client).get(
            "/auth/me", headers={"authorization": "Bearer calliope_not_a_real_key"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_a_service_key_is_useless_on_the_public_port(monkeypatch):
    """Service keys work on :8081 only (D6): one leaked from a container buys
    nothing from outside the compose network."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.get("/v1/models", headers=bearer(service_key("satellites")))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# ── the bootstrap ─────────────────────────────────────────────────────────────


async def test_the_bootstrap_value_signs_in_to_a_restricted_session(monkeypatch):
    """The first-access value opens only the forced change: no keys, no
    speech, no admin, for 15 minutes (D21)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await sign_in(client, "admin", ADMIN_PASSWORD)
        me = await page(client).get("/auth/me")
        models = await client.get("/v1/models")
        keys = await client.post("/auth/keys", json={"name": "x", "preset": "read-only"})

    assert response.status_code == 200 and response.json()["must_change"] is True
    assert f"Max-Age={15 * 60}" in response.headers["set-cookie"]
    assert me.json()["must_change"] is True
    assert models.status_code == keys.status_code == 401


async def test_the_bootstrap_is_consumed_by_the_change_and_not_by_the_login(monkeypatch):
    """A closed tab cannot strand the owner, and a second browser that also
    used the value loses its session the moment the first one changes it (D22)."""
    async with gateway(monkeypatch, authenticate=False) as (first, main):
        rt = main.runtime.get()
        import httpx
        second = httpx.AsyncClient(transport=first._transport, base_url=str(first.base_url),
                                   headers=SAME_ORIGIN)
        await sign_in(first, "admin", ADMIN_PASSWORD)
        await sign_in(second, "admin", ADMIN_PASSWORD)
        consumed_after_logins = rt.db.meta("bootstrap_consumed")
        changed = await page(first).post("/auth/password", json={"new_password": PASSWORD})
        second_after = await second.get("/auth/me")
        first_after = await first.get("/v1/models")
        again = await sign_in(second, "admin", ADMIN_PASSWORD)
        await second.aclose()
        marker = rt.marker.exists()

    assert consumed_after_logins == "0"
    assert changed.status_code == 200
    assert first_after.status_code == 200
    assert second_after.status_code == 401
    assert again.status_code == 401, "the variable still worked after it was replaced"
    assert marker, "the bootstrap_consumed marker was not written to calliope-keys"


async def test_the_new_password_must_differ_from_the_bootstrap_value(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await sign_in(client, "admin", ADMIN_PASSWORD)
        same = await page(client).post("/auth/password",
                                       json={"new_password": ADMIN_PASSWORD})
        padded = await client.post("/auth/password",
                                   json={"new_password": ADMIN_PASSWORD + "2026!"})

    for response in (same, padded):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "weak_password"
        assert ADMIN_PASSWORD not in response.text


async def test_only_one_session_can_consume_the_bootstrap(monkeypatch):
    """BEGIN IMMEDIATE and `WHERE value = '0'`: two changes at once cannot
    both succeed (recheck L6)."""
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        rt = main.runtime.get()
        admin = rt.db.one("SELECT id FROM users WHERE username = 'admin'")["id"]
        assert rt.consume_bootstrap(admin, "first") is True
        assert rt.consume_bootstrap(admin, "second") is False
        stored = rt.db.one("SELECT password_hash FROM users WHERE id = ?",
                           (admin,))["password_hash"]

    assert stored == "first"


@pytest.mark.parametrize("failing", ["hash", "store"])
async def test_a_password_change_that_fails_leaves_the_bootstrap_usable(monkeypatch,
                                                                       failing):
    """Consumed with no password stored, the admin would have neither the
    bootstrap value nor a password of their own (D22)."""
    from app import passwords, users

    class Broken(Exception):
        pass

    def broken(*_args, **_kwargs):
        raise Broken(failing)

    async def broken_hash(*_args, **_kwargs):
        raise Broken(failing)

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        await sign_in(client, "admin", ADMIN_PASSWORD)
        with monkeypatch.context() as patch:
            if failing == "hash":
                patch.setattr(passwords.Hasher, "hash", broken_hash)
            else:
                patch.setattr(users, "set_password", broken)
            with pytest.raises(Broken):
                await page(client).post("/auth/password", json={"new_password": PASSWORD})
        armed, marker = rt.bootstrap_armed(), rt.marker.exists()
        again = await sign_in(client, "admin", ADMIN_PASSWORD)

    assert armed and not marker
    assert again.status_code == 200


async def test_an_account_with_a_temporary_password_gets_a_restricted_session(monkeypatch):
    """Not only the bootstrap: a password an admin read out, or the CLI printed,
    must not create keys that outlive the change (recheck M-7)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana", must_change=True)
        response = await sign_in(client)
        keys = await page(client).post("/auth/keys",
                                       json={"name": "x", "preset": "read-only"})
        changed = await client.post("/auth/password", json={"new_password":
                                                            "an entirely new passphrase"})
        after = await client.post("/auth/step-up", json={"password":
                                                         "an entirely new passphrase"})

    assert response.json()["must_change"] is True
    assert keys.status_code == 401
    assert changed.status_code == 200
    assert after.status_code == 200


# ── throttling ────────────────────────────────────────────────────────────────


async def test_a_correct_password_inside_a_delay_is_refused_like_any_other(monkeypatch):
    """From the 5th consecutive failure the password is not even checked (D19)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        failures = [await sign_in(client, "ana", "wrong but long enough")
                    for _ in range(5)]
        correct = await sign_in(client)

    assert [r.status_code for r in failures] == [401] * 5
    assert correct.status_code == 429
    assert int(correct.headers["retry-after"]) >= 1


async def test_an_unknown_username_is_throttled_exactly_like_a_known_one(monkeypatch):
    """Otherwise the throttle would say which accounts exist."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        known = [(await sign_in(client, "ana", "wrong but long enough")).status_code
                 for _ in range(6)]
        unknown = [(await sign_in(client, "nobody", "wrong but long enough")).status_code
                   for _ in range(6)]

    assert known == unknown == [401] * 5 + [429]


async def test_failures_from_one_address_do_not_delay_a_login_from_another(monkeypatch):
    """Keyed by (account, IP), so an internet guesser cannot lock the admin
    out from the admin's own address (D19, M4)."""
    import httpx
    async with gateway(monkeypatch, authenticate=False) as (attacker, main):
        make_user("ana")
        for _ in range(6):
            await sign_in(attacker, "ana", "wrong but long enough")
        owner = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app, client=("198.51.100.20", 1)),
            base_url=str(attacker.base_url))
        mine = await sign_in(owner)
        await owner.aclose()

    assert mine.status_code == 200


async def test_twenty_attempts_from_one_address_in_ten_minutes_is_the_limit(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        codes = [(await sign_in(client, f"name{i}", "wrong but long enough")).status_code
                 for i in range(21)]

    assert codes == [401] * 20 + [429]


async def test_the_account_ceiling_spares_an_address_that_signed_in_recently(monkeypatch):
    """Past 50 failures in 10 minutes from everywhere, addresses the owner has
    not used make one attempt a minute between them; the owner's own are
    exempt, so the ceiling cannot lock the owner out (D19)."""
    from app import throttle

    Clock(monkeypatch)
    gate = throttle.Throttle()
    known = lambda: {"203.0.113.1"}  # noqa: E731
    for i in range(51):
        assert gate.check("ana", f"10.0.{i // 250}.{i % 250}", known) is None
        gate.failed("ana", f"10.0.{i // 250}.{i % 250}")

    assert gate.check("ana", "198.51.100.1", known) is None
    assert gate.check("ana", "198.51.100.2", known) is not None, \
        "a second stranger in the same minute was let through past the ceiling"
    assert gate.check("ana", "203.0.113.1", known) is None


async def test_unlock_from_the_cli_clears_the_delay(monkeypatch):
    from app import admin

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        for _ in range(5):
            await sign_in(client, "ana", "wrong but long enough")
        before = await sign_in(client)
        admin.unlock(main.runtime.get().settings, "ana")
        after = await sign_in(client)

    assert before.status_code == 429
    assert after.status_code == 200


async def test_step_up_failures_count_towards_the_login_throttle(monkeypatch):
    """Otherwise step-up is an unthrottled password oracle for anyone holding
    a stolen session (D13)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        wrong = [await page(client).post("/auth/step-up",
                                         json={"password": "wrong but long enough"})
                 for _ in range(5)]
        login = await sign_in(client)

    assert [r.status_code for r in wrong] == [403] * 5
    assert login.status_code == 429


async def test_ten_thousand_invented_usernames_cost_a_bounded_amount_of_memory(monkeypatch):
    """Every map is an LRU with a cap; an evicted entry forgets a delay and
    never locks anybody out (recheck M-4). Ten thousand names against a cap
    of a thousand show the bound without a million-iteration test."""
    from app import throttle

    Clock(monkeypatch)
    gate = throttle.Throttle(max_entries=1000)
    for i in range(10_000):
        gate.check(f"user{i}", f"10.{i % 200}.0.1", lambda: ())
        gate.failed(f"user{i}", f"10.{i % 200}.0.1")

    assert len(gate.pairs) <= 1000 and len(gate.accounts) <= 1000
    assert len(gate.ips) <= 1000


# ── sessions ──────────────────────────────────────────────────────────────────


async def test_a_session_ends_after_thirty_idle_days(monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        page(client)
        clock.advance(29 * 24 * 3600)
        still = await client.get("/auth/me")
        clock.advance(29 * 24 * 3600)
        touched = await client.get("/auth/me")
        clock.advance(30 * 24 * 3600 + 60)
        idle = await client.get("/auth/me")

    assert still.status_code == touched.status_code == 200
    assert idle.status_code == 401


async def test_a_session_ends_a_year_after_it_began_however_busy(monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        page(client)
        codes = []
        for _ in range(53):
            clock.advance(7 * 24 * 3600)
            codes.append((await client.get("/auth/me")).status_code)

    assert codes[:52] == [200] * 52
    assert codes[-1] == 401


async def test_logout_ends_the_session_on_the_server(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        cookie = client.cookies.get(COOKIE)
        out = await page(client).post("/auth/logout")
        replay = await client.get("/auth/me", headers={"cookie": f"{COOKIE}={cookie}"})

    assert out.status_code == 204
    assert "Max-Age=0" in out.headers["set-cookie"]
    assert replay.status_code == 401


async def test_a_password_change_ends_every_other_session(monkeypatch):
    import httpx
    async with gateway(monkeypatch, authenticate=False) as (laptop, _):
        make_user("ana")
        phone = httpx.AsyncClient(transport=laptop._transport, base_url=str(laptop.base_url),
                                  headers=SAME_ORIGIN)
        await sign_in(laptop)
        await sign_in(phone)
        changed = await page(laptop).post("/auth/password", json={
            "current_password": PASSWORD, "new_password": "an entirely new passphrase"})
        laptop_after = await laptop.get("/auth/me")
        phone_after = await phone.get("/auth/me")
        await phone.aclose()

    assert changed.status_code == 200
    assert laptop_after.status_code == 200, "the browser that changed it was signed out"
    assert phone_after.status_code == 401


async def test_a_password_change_needs_the_current_password(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        missing = await page(client).post("/auth/password", json={
            "new_password": "an entirely new passphrase"})
        wrong = await client.post("/auth/password", json={
            "current_password": "not it at all, sorry", "new_password":
            "an entirely new passphrase"})

    assert missing.status_code == wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "wrong_password"


async def test_signing_out_other_sessions_keeps_this_one(monkeypatch):
    import httpx
    async with gateway(monkeypatch, authenticate=False) as (laptop, _):
        make_user("ana")
        phone = httpx.AsyncClient(transport=laptop._transport, base_url=str(laptop.base_url),
                                  headers=SAME_ORIGIN)
        await sign_in(laptop)
        await sign_in(phone)
        listed = (await page(laptop).get("/auth/sessions")).json()["sessions"]
        out = await laptop.delete("/auth/sessions")
        laptop_after = await laptop.get("/auth/me")
        phone_after = await phone.get("/auth/me")
        await phone.aclose()

    assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
    assert out.status_code == 204
    assert (laptop_after.status_code, phone_after.status_code) == (200, 401)


async def test_a_session_used_through_a_key_only_route_is_refused(monkeypatch):
    """GET /auth/me is for a session; a key asking it is told so (D60)."""
    async with gateway(monkeypatch) as (client, _):
        response = await client.get("/auth/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "session_required"


async def test_a_key_reaches_what_its_scopes_name_and_nothing_else(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        key = make_key(make_user("sam", role="speech"),
                       scopes={"models:read", "speech:transcribe"})
        models = await client.get("/v1/models", headers=bearer(key))
        voices = await client.get("/voices", headers=bearer(key))

    assert models.status_code == 200
    assert voices.status_code == 403
    assert voices.headers["www-authenticate"] == \
        'Bearer error="insufficient_scope", scope="speech:speak"'


async def test_an_oversized_login_body_is_refused_before_it_is_read(monkeypatch):
    """The login route is public, so its body is bounded like an upload."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.post(
            "/auth/login", content=b'{"username":"' + b"a" * 70_000 + b'"}',
            headers={**SAME_ORIGIN, "content-type": "application/json"})

    assert response.status_code == 413
