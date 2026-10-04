"""Long streams end when their credential does, and every 15 minutes anyway (D54)."""

from __future__ import annotations

import asyncio

import httpx
from conftest import SAME_ORIGIN, bearer, gateway, make_key, make_user, sign_in


class Endless(httpx.AsyncBaseTransport):
    """The hub's /satellites/events: one event, then keep-alives for ever."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self

        class Events(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"event: hello\ndata: {}\n\n"
                transport.opened.set()
                while True:
                    await asyncio.sleep(0.02)
                    yield b": keep-alive\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Events())


async def _watch(client: httpx.AsyncClient, **headers) -> httpx.Response:
    return await client.get("/satellites/events", headers=headers)


async def test_revoking_a_key_closes_the_event_stream_it_opened(monkeypatch):
    hub = Endless()
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, main):
        key = make_key(make_user("ana"), scopes={"satellites:read"})
        watching = asyncio.create_task(_watch(client, **bearer(key)))
        await asyncio.wait_for(hub.opened.wait(), 2)
        key_id = main.runtime.get().db.one("SELECT id FROM api_keys")["id"]
        from app import apikeys
        apikeys.revoke(main.runtime.get().db, key_id, by="test")
        closed = main.runtime.get().streams.close(credential=f"key:{key_id}")
        response = await asyncio.wait_for(watching, 2)

    assert closed == 1
    assert response.status_code == 200
    assert response.text.startswith("event: hello")


async def test_logging_out_closes_the_event_stream_of_that_session(monkeypatch):
    hub = Endless()
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        client.headers.update(SAME_ORIGIN)
        watching = asyncio.create_task(_watch(client))
        await asyncio.wait_for(hub.opened.wait(), 2)
        out = await client.post("/auth/logout")
        response = await asyncio.wait_for(watching, 2)

    assert out.status_code == 204
    assert response.status_code == 200


async def test_disabling_a_user_closes_every_stream_they_have_open(monkeypatch):
    hub = Endless()
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, main):
        ana = make_user("ana")
        key = make_key(ana, scopes={"satellites:read"})
        watching = asyncio.create_task(_watch(client, **bearer(key)))
        await asyncio.wait_for(hub.opened.wait(), 2)
        admin = make_user("root")
        await sign_in(client, "root")
        client.headers.update(SAME_ORIGIN)
        from conftest import PASSWORD
        await client.post("/auth/step-up", json={"password": PASSWORD})
        disabled = await client.patch(f"/admin/users/{ana['id']}", json={"disabled": True})
        response = await asyncio.wait_for(watching, 2)

    assert admin and disabled.status_code == 200
    assert response.status_code == 200


async def test_an_event_stream_ends_at_its_cap_so_the_credential_is_checked_again(
        monkeypatch):
    from app import authn

    monkeypatch.setattr(authn, "SSE_CAP_SECONDS", 0.2)
    hub = Endless()
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, _):
        key = make_key(make_user("ana"), scopes={"satellites:read"})
        response = await asyncio.wait_for(_watch(client, **bearer(key)), 2)

    assert response.status_code == 200
    assert response.text.startswith("event: hello")


async def test_ten_anonymous_health_calls_cost_each_backend_one_probe(monkeypatch):
    """Anonymous /health must not be an amplifier against the backends (D50)."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        probed: list[str] = []
        real = main._probe

        async def counted(backend):
            probed.append(backend.name)
            return await real(backend)

        monkeypatch.setattr(main, "_probe", counted)
        answers = await asyncio.gather(*(client.get("/health") for _ in range(10)))

    assert all(a.json() == {"status": "ok"} for a in answers)
    assert sorted(probed) == sorted({"stt-stack", "tts-stack", "tts-long",
                                     "voice-satellites"})
