"""Locked mode: a configuration fault refuses people and keeps the satellites up (D63, H5).

Every "refuse to start" in the first design became a reason here, because an
exit takes the device relay and the internal listener with it, and the hub
reaches stt and tts only through :8081. So for each reason the same four
things are asserted: /health still answers, a device is still relayed, a
service key still works on :8081, and a person gets 503 `locked` naming the
reason and the variable to fix -- never its value.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import (ADMIN_PASSWORD, SAME_ORIGIN, MockBackend, bearer, gateway,
                      internal_client, reload_gateway, service_key, sign_in)


# Values a lock must name the variable of and never repeat.
SECRET_VALUES = ("sk-left-behind", "tooshort42")


def _weak(monkeypatch):
    monkeypatch.setenv("CALLIOPE_ADMIN_PASSWORD", "tooshort42")


def _removed(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEYS", "sk-left-behind")


def _no_argon2(monkeypatch):
    from app import passwords
    monkeypatch.setattr(passwords.Hasher, "selftest", lambda self: False)


def _dev_cookie(monkeypatch):
    monkeypatch.setenv("CALLIOPE_DEV_INSECURE_COOKIE", "1")


def _no_origin(monkeypatch):
    monkeypatch.delenv("CALLIOPE_PUBLIC_ORIGIN")


def _bad_origin(monkeypatch):
    monkeypatch.setenv("CALLIOPE_PUBLIC_ORIGIN", "https://calliope.example/path?x")


def _no_bootstrap(monkeypatch):
    monkeypatch.delenv("CALLIOPE_ADMIN_PASSWORD")


CASES = [
    (_removed, "removed_variable", "GATEWAY_API_KEYS"),
    (_no_argon2, "argon2_selftest", None),
    (_dev_cookie, "dev_insecure_cookie", "CALLIOPE_DEV_INSECURE_COOKIE"),
    (_no_origin, "public_origin_required", "CALLIOPE_PUBLIC_ORIGIN"),
    (_bad_origin, "public_origin_required", "CALLIOPE_PUBLIC_ORIGIN"),
    (_no_bootstrap, "bootstrap_required", "CALLIOPE_ADMIN_PASSWORD"),
    (_weak, "admin_password_weak", "CALLIOPE_ADMIN_PASSWORD"),
]


@pytest.mark.parametrize("cause,reason,variable", CASES,
                         ids=[reason for _, reason, _ in CASES])
async def test_a_locked_gateway_refuses_people_and_serves_the_satellites(
        monkeypatch, cause, reason, variable):
    cause(monkeypatch)
    tts = MockBackend("tts-stack")
    async with gateway(monkeypatch, tts=tts, authenticate=False) as (client, main):
        health = await client.get("/health")
        refused = {path: await request for path, request in (
            ("/v1/models", client.get("/v1/models")),
            ("/ui", client.get("/ui", headers=SAME_ORIGIN)),
            ("/auth/login", client.post("/auth/login", headers=SAME_ORIGIN,
                                        json={"username": "admin",
                                              "password": ADMIN_PASSWORD})))}
        login_page = await client.get("/login")
        async with internal_client() as internal:
            served = await internal.post("/v1/audio/speech",
                                         headers=bearer(service_key("satellites")),
                                         json={"input": "The hub still speaks."})
        audited = main.runtime.get().db.one(
            "SELECT target FROM audit WHERE action = 'locked_mode'")

    assert health.status_code == 200 and health.json() == {"status": "degraded"}
    for path, response in refused.items():
        assert response.status_code == 503, path
        body = response.json()
        assert body["error"]["code"] == "locked"
        assert (body["reason"], body["variable"]) == (reason, variable), path
    assert login_page.status_code == 200, "the page that explains the lock is locked too"
    assert served.status_code == 200 and tts.last["path"] == "/v1/audio/speech"
    assert audited["target"] == reason
    for response in (health, login_page, *refused.values()):
        assert not any(value in response.text for value in SECRET_VALUES), \
            "a variable's value reached a response"


def test_a_locked_gateway_still_relays_a_device_with_the_relay_assertion(monkeypatch):
    """The device socket never depends on a login, so a lock cannot take the
    satellites down (D63), and the hop to the hub carries the assertion the
    hub requires (D53)."""
    from starlette.testclient import TestClient
    from voice_common.identity import ASSERTION_HEADER, Credentials, verify

    monkeypatch.setenv("GATEWAY_API_KEYS", "sk-left-behind")
    main = reload_gateway(monkeypatch)
    hub = FakeHub()

    async def connect(target, **options):
        hub.headers = options["additional_headers"]
        return hub

    monkeypatch.setattr(main, "ws_connect", connect)
    with TestClient(main.app) as client:
        assert main.runtime.get().lock.active
        with client.websocket_connect("/satellites/ws") as device:
            device.send_text('{"type":"hello"}')
        directory = main.runtime.get().settings.svc_dir / "satellites"

    claims = verify(hub.headers[ASSERTION_HEADER], "satellites", Credentials(directory))
    assert (claims.sub, claims.kind, claims.scopes) == ("svc:gateway-relay", "service",
                                                        frozenset())
    assert hub.sent == ['{"type":"hello"}']


class FakeHub:
    """Enough of a websockets client connection for the relay."""

    def __init__(self) -> None:
        self.sent: list = []
        self.headers: dict = {}
        self.close_code = 1000

    async def send(self, message) -> None:
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)

    async def close(self) -> None:
        return None


# ── the bootstrap reasons ─────────────────────────────────────────────────────


async def test_a_lost_database_does_not_rearm_the_bootstrap_and_the_cli_recovers(
        monkeypatch, capsys):
    """gateway-data gone, calliope-keys kept: the variable everyone could read
    in docker inspect must not reopen admin (D24). reset-password admin does,
    and the running gateway notices without a restart."""
    from app import admin, runtime

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        await sign_in(client, "admin", ADMIN_PASSWORD)
        await client.post("/auth/password", headers=SAME_ORIGIN,
                          json={"new_password": "the owner's own passphrase"})
        data_dir = main.runtime.get().settings.data_dir

    for leftover in data_dir.iterdir():
        leftover.unlink()

    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = runtime.get()
        lost = rt.lock.reasons[:]
        bootstrap = await sign_in(client, "admin", ADMIN_PASSWORD)
        admin.main(["reset-password", "admin"])
        temporary = capsys.readouterr().out.split("\n\n")[1].strip()
        rt.check_bootstrap()
        after = rt.lock.reasons[:]
        recovered = await sign_in(client, "admin", temporary)

    assert lost == [("bootstrap_data_lost", None)]
    assert bootstrap.status_code == 503
    assert after == []
    assert recovered.status_code == 200 and recovered.json()["must_change"] is True


async def test_a_weak_value_left_set_after_the_bootstrap_does_not_lock(monkeypatch):
    """The strength rule applies only while the bootstrap is armed (recheck
    M-6): a leftover variable must never lock HA's key out of the API."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await sign_in(client, "admin", ADMIN_PASSWORD)
        await client.post("/auth/password", headers=SAME_ORIGIN,
                          json={"new_password": "the owner's own passphrase"})

    monkeypatch.setenv("CALLIOPE_ADMIN_PASSWORD", "password")
    async with gateway(monkeypatch) as (client, main):
        locked = main.runtime.get().lock.reasons
        models = await client.get("/v1/models")

    assert locked == []
    assert models.status_code == 200


async def test_a_plain_http_public_origin_on_a_network_bind_gives_no_session(monkeypatch):
    """Cookie login is refused over plain HTTP (D16). An http:// origin would
    match a cleartext request, and a client that is not a browser keeps the
    Secure cookie a browser would drop."""
    import httpx

    monkeypatch.setenv("CALLIOPE_PUBLIC_ORIGIN", "http://gateway.test")
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        reasons = main.runtime.get().lock.reasons
        plain = httpx.AsyncClient(transport=client._transport, base_url="http://gateway.test")
        login = await sign_in(plain, "admin", ADMIN_PASSWORD)
        await plain.aclose()

    assert reasons == [("public_origin_required", "CALLIOPE_PUBLIC_ORIGIN")]
    assert login.status_code == 503 and "set-cookie" not in login.headers


async def test_the_dev_cookie_is_honoured_on_a_loopback_bind(monkeypatch):
    """The e2e harness runs on 127.0.0.1 over plain HTTP; nothing else may."""
    import httpx

    monkeypatch.setenv("CALLIOPE_DEV_INSECURE_COOKIE", "1")
    monkeypatch.setenv("GATEWAY_BIND", "127.0.0.1")
    monkeypatch.setenv("CALLIOPE_PUBLIC_ORIGIN", "http://127.0.0.1:8080")
    from conftest import make_user
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        local = httpx.AsyncClient(transport=client._transport,
                                  base_url="http://127.0.0.1:8080")
        response = await sign_in(local)
        me = await local.get("/auth/me", headers=SAME_ORIGIN)
        await local.aclose()
        locked = main.runtime.get().lock.reasons

    assert locked == []
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("calliope_session=") and "Secure" not in cookie
    assert me.status_code == 200


async def test_an_http_origin_is_refused_on_a_socket_that_is_not_loopback(monkeypatch):
    """GATEWAY_BIND says loopback, but the socket is what the request reached:
    a session over cleartext from the network is never issued (D16)."""
    import httpx

    monkeypatch.setenv("GATEWAY_BIND", "127.0.0.1")
    monkeypatch.setenv("CALLIOPE_PUBLIC_ORIGIN", "http://calliope.lan")
    from conftest import make_user
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        lan = httpx.AsyncClient(transport=client._transport, base_url="http://calliope.lan")
        response = await sign_in(lan)
        await lan.aclose()
        locked = main.runtime.get().lock.reasons

    assert locked == []
    assert response.status_code == 403
    assert "set-cookie" not in response.headers or "Max-Age=0" in response.headers["set-cookie"]
