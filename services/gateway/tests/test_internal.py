"""The internal listener: service keys, /runs and delegation (D6, D31, D64, §3.6, §3.7)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import (PASSWORD, SAME_ORIGIN, MockBackend, bearer, gateway, internal_client, make_key,
                      make_user, service_key, sign_in)
from voice_common.identity import ASSERTION_HEADER, DELEGATION_HEADER, Credentials, verify

TRANSCRIBE = "/v1/audio/transcriptions"


def claims_seen(backend: MockBackend, audience: str, svc_dir):
    """What the gateway asserted to `backend`, verified as the backend would."""
    return verify(backend.last["headers"][ASSERTION_HEADER.lower()], audience,
                  Credentials(svc_dir / "stt"))


async def test_a_service_reaches_what_its_principal_holds_and_nothing_else(monkeypatch):
    tts = MockBackend("tts-stack")
    async with gateway(monkeypatch, tts=tts, authenticate=False) as (_, main):
        async with internal_client() as internal:
            speak = await internal.post("/v1/audio/speech", json={"input": "Hi."},
                                        headers=bearer(service_key("satellites")))
            jobs = await internal.get("/jobs", headers=bearer(service_key("satellites")))
            nothing = await internal.get("/voices")
            user_key = await internal.get("/voices", headers=bearer(make_key(make_user("ana"))))
        svc_dir = main.runtime.get().settings.svc_dir

    assert speak.status_code == 200
    claims = verify(tts.last["headers"][ASSERTION_HEADER.lower()], "tts",
                    Credentials(svc_dir / "tts"))
    assert (claims.sub, claims.kind, claims.cred) == ("svc:satellites", "service",
                                                      "svc:satellites")
    assert jobs.status_code == 403
    assert nothing.status_code == user_key.status_code == 401


async def test_runs_take_runs_write_and_reach_tts_long_as_the_service(monkeypatch):
    long = MockBackend("tts-long")
    async with gateway(monkeypatch, long=long) as (public, main):
        async with internal_client() as internal:
            stt = await internal.post("/runs", json={"id": "r1"},
                                      headers=bearer(service_key("stt")))
            hub = await internal.post("/runs", json={"id": "r2"},
                                      headers=bearer(service_key("satellites")))
        browser = await public.post("/runs", json={"id": "r3"})
        svc_dir = main.runtime.get().settings.svc_dir

    assert stt.status_code == 200
    claims = verify(long.last["headers"][ASSERTION_HEADER.lower()], "tts-long",
                    Credentials(svc_dir / "tts-long"))
    assert claims.sub == "svc:stt" and "runs:write" in claims.scopes
    assert hub.status_code == 403
    assert browser.status_code == 404, "the browser can write into the run history"
    assert [r["path"] for r in long.seen] == ["/runs"]


async def test_the_hub_reads_stt_s_engines_and_its_glossary_from_the_internal_health(
        monkeypatch):
    """The hub reaches stt only through :8081, so its health is there too (D50)."""
    import json
    stt = MockBackend("stt-stack")
    stt.reply = lambda r: (200, {"content-type": "application/json"}, json.dumps(
        {"status": "ok", "models": [{"id": "parakeet", "default": True}],
         "glossaries": ["tech", "home-assistant"], "host_label": "nas"}).encode())
    async with gateway(monkeypatch, stt=stt, authenticate=False):
        async with internal_client() as internal:
            hub = await internal.get("/health", headers=bearer(service_key("satellites")))
            runs_only = await internal.get("/health", headers=bearer(service_key("stt")))
            nobody = await internal.get("/health")

    shown = hub.json()["backends"]["stt"]["health"]
    assert shown["models"] == [{"id": "parakeet", "default": True}]
    assert shown["glossaries"] == ["tech", "home-assistant"]
    assert "host_label" not in shown, "the hub holds health:read, not health:detail"
    assert runs_only.status_code == 403
    assert nobody.status_code == 401


async def test_the_hub_cannot_change_the_home_assistant_glossary_on_the_internal_listener(
        monkeypatch):
    """glossaries:ha lets the hub select that profile for Assist, not rewrite it."""
    stt = MockBackend("stt-stack")
    async with gateway(monkeypatch, stt=stt, authenticate=False):
        async with internal_client() as internal:
            hub = bearer(service_key("satellites"))
            put = await internal.put("/glossaries/home-assistant", content=b"a b = C\n",
                                     headers=hub)
            delete = await internal.delete("/glossaries/home-assistant", headers=hub)

    assert put.status_code == delete.status_code == 403
    assert stt.seen == []


# ── delegation ────────────────────────────────────────────────────────────────


async def _fetch_token(client: httpx.AsyncClient, ui: MockBackend, **headers) -> str:
    response = await client.post("/ui/fetch", json={"token": "t"},
                                 headers={**SAME_ORIGIN, **headers})
    assert response.status_code == 200, response.text
    return ui.last["headers"][DELEGATION_HEADER.lower()]


async def _spend(token: str, *, key: str | None = None) -> httpx.Response:
    async with internal_client() as internal:
        return await internal.post(TRANSCRIBE, content=b"RIFF",
                                   headers={**bearer(key or service_key("ui")),
                                            DELEGATION_HEADER: token})


async def test_a_delegation_works_twice_and_fails_the_third_time(monkeypatch):
    """One retry, never a replay (D64)."""
    ui, stt = MockBackend("voice-ui"), MockBackend("stt-stack")
    async with gateway(monkeypatch, ui=ui, stt=stt, authenticate=False) as (client, main):
        user = make_user("ana", role="speech")
        await sign_in(client, "ana")
        token = await _fetch_token(client, ui)
        uses = [await _spend(token) for _ in range(3)]
        svc_dir = main.runtime.get().settings.svc_dir
        claims = claims_seen(stt, "stt", svc_dir)

    assert [r.status_code for r in uses] == [200, 200, 403]
    assert (claims.sub, claims.kind, claims.cred) == (user["id"], "user", "session")
    assert claims.scopes == {"speech:transcribe"}
    assert len(stt.seen) == 2


async def test_ui_fetch_carries_the_delegation_and_no_inbound_calliope_header(monkeypatch):
    ui = MockBackend("voice-ui")
    async with gateway(monkeypatch, ui=ui, authenticate=False) as (client, _):
        make_user("ana", role="speech")
        await sign_in(client, "ana")
        await _fetch_token(client, ui, **{"x-calliope-delegation": "forged"})

    headers = ui.last["raw_headers"]
    delegations = [v for k, v in headers if k == DELEGATION_HEADER.lower()]
    assert len(delegations) == 1 and delegations[0] != "forged"
    assert "cookie" not in dict(headers)


@pytest.mark.parametrize("revoke", ["logout", "disable", "password_change"])
async def test_a_delegation_ends_when_the_session_or_user_does(monkeypatch, revoke):
    from app import runtime, users

    ui = MockBackend("voice-ui")
    async with gateway(monkeypatch, ui=ui, authenticate=False) as (client, _):
        user = make_user("ana", role="speech")
        make_user("root")
        await sign_in(client, "ana")
        token = await _fetch_token(client, ui)
        db = runtime.get().db
        if revoke == "logout":
            await client.post("/auth/logout", headers=SAME_ORIGIN)
        elif revoke == "disable":
            users.update(db, actor_id=None, user_id=user["id"], disabled=True)
        else:
            await client.post("/auth/password", headers=SAME_ORIGIN, json={
                "current_password": PASSWORD, "new_password": "a different passphrase"})
        spent = await _spend(token)

    assert spent.status_code == 403
    assert spent.json()["error"]["code"] == "delegation_refused"


async def test_a_delegation_through_a_key_ends_when_the_key_is_revoked(monkeypatch):
    from app import apikeys, runtime

    ui = MockBackend("voice-ui")
    async with gateway(monkeypatch, ui=ui, authenticate=False) as (client, _):
        owner = make_user("ana", role="speech")
        key = make_key(owner, scopes={"ingest:links", "speech:transcribe"})
        token = await _fetch_token(client, ui, **bearer(key))
        first = await _spend(token)
        key_id = runtime.get().db.one("SELECT id FROM api_keys")["id"]
        apikeys.revoke(runtime.get().db, key_id, by="test")
        second = await _spend(token)

    assert first.status_code == 200
    assert second.status_code == 403


class SlowTranscription(httpx.AsyncBaseTransport):
    """stt working on a long link: it answers only when cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize("revoke", ["logout", "key_revoked"])
async def test_logout_or_revoking_the_key_ends_a_delegated_transcription_in_flight(
        monkeypatch, revoke):
    """A link transcription can run for 15 minutes; it is registered under the
    session or key it came from, so ending that credential ends it (D54)."""
    ui, stt = MockBackend("voice-ui"), SlowTranscription()
    async with gateway(monkeypatch, ui=ui, stt=stt, authenticate=False) as (client, main):
        owner = make_user("ana", role="speech")
        await sign_in(client, "ana")
        key = make_key(owner, scopes={"ingest:links", "speech:transcribe"})
        token = await _fetch_token(client, ui, **(bearer(key) if revoke == "key_revoked"
                                                   else {}))
        spending = asyncio.create_task(_spend(token))
        await asyncio.wait_for(stt.started.wait(), 2)
        if revoke == "logout":
            ended = await client.post("/auth/logout", headers=SAME_ORIGIN)
        else:
            key_id = main.runtime.get().db.one("SELECT id FROM api_keys")["id"]
            ended = await client.delete(f"/auth/keys/{key_id}", headers=SAME_ORIGIN)
        spent = await asyncio.wait_for(spending, 2)

    assert ended.status_code in (200, 204)
    assert spent.status_code == 401


async def test_an_identity_assertion_is_not_a_delegation(monkeypatch):
    """A token for aud=ui, which voice-ui does receive, cannot be spent (§3.7)."""
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        user = make_user("ana")
        assertion = main.runtime.get().keys.signer().assertion(
            audience="ui", sub=user["id"], kind="user", scopes={"speech:transcribe"},
            cred="session")
        spent = await _spend(assertion)

    assert spent.status_code == 403


@pytest.mark.parametrize("service,path", [("satellites", TRANSCRIBE),
                                          ("ui", "/v1/audio/speech")])
async def test_the_delegation_header_is_honoured_on_one_row_from_one_scope(
        monkeypatch, service, path):
    """Anyone else sending it is a bug or an attempt, never a switch of
    identity (recheck L3)."""
    ui = MockBackend("voice-ui")
    async with gateway(monkeypatch, ui=ui, authenticate=False) as (client, _):
        make_user("ana", role="speech")
        await sign_in(client, "ana")
        token = await _fetch_token(client, ui)
        async with internal_client() as internal:
            response = await internal.post(path, json={"input": "x"},
                                           headers={**bearer(service_key(service)),
                                                    DELEGATION_HEADER: token})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "delegation_not_allowed"


async def test_voice_ui_cannot_transcribe_without_a_delegation(monkeypatch):
    async with gateway(monkeypatch, authenticate=False):
        async with internal_client() as internal:
            response = await internal.post(TRANSCRIBE, content=b"RIFF",
                                           headers=bearer(service_key("ui")))
    assert response.status_code == 403
