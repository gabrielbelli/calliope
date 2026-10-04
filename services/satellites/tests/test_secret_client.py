"""The hub's view of the secret store (secret_client.py) and its one credential (gateway.py).

Against conftest's FakeStore: the gateway's /internal/secrets on its own
address, answering only to the service key the FakeGateway wrote.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from voice_common.identity import SERVICE_KEY_FILE

from app import gateway as hub_gateway
from app import secret_client
from app.secret_client import HostNotAllowed, Secrets, origin

NAME = "SATELLITES_HA_TOKEN"
TOKEN = "ha-token-do-not-leak"
HA = "https://ha.lan:8123"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(store) -> Clock:
    tick = Clock()
    secret_client.configure(Secrets(transport=httpx.MockTransport(store), clock=tick))
    return tick


def secrets() -> Secrets:
    return secret_client.current()


# ---- where a value may go (D41) ----------------------------------------------------


@pytest.mark.parametrize("entry", ["https://ha.lan", "HA.lan.", "user@ha.lan", "ha.lan:443",
                                   "https://HA.LAN:443/", "ha.lan"])
def test_every_spelling_of_one_host_is_the_same_entry(entry):
    assert origin(entry, entry=True) == "https://ha.lan:443"


def test_an_entry_with_a_path_admits_nothing_as_the_store_refuses_it():
    assert origin("https://ha.lan/api", entry=True) is None


def test_a_target_is_held_to_its_scheme_and_port():
    held = secret_client.Held(TOKEN, secret_client.origins(["ha.lan:8123"]))
    assert held.allows("https://ha.lan:8123/api/conversation/process")
    # An entry without a scheme is https only: a bearer over plain http only
    # when an entry says http.
    assert not held.allows("http://ha.lan:8123")
    assert not held.allows("https://ha.lan")            # another port
    assert not held.allows("https://ha.lan.evil.test:8123")
    assert secret_client.Held(TOKEN, secret_client.origins(["http://ha.lan:8123"])).allows(
        "http://ha.lan:8123/api")


def test_names_are_compared_as_idna_and_an_empty_list_allows_nothing():
    assert origin("https://bücher.example") == "https://xn--bcher-kva.example:443"
    assert origin("mqtts://broker.test") == "mqtts://broker.test:8883"
    assert origin("ha.lan") is None and origin("ftp://ha.lan") is None
    assert not secret_client.Held(TOKEN, frozenset()).allows(HA)


async def test_a_value_is_never_given_for_a_host_its_secret_does_not_name(store, clock):
    store.put(NAME, TOKEN, [HA])
    assert await secrets().value_for(NAME, HA + "/api/config") == TOKEN
    with pytest.raises(HostNotAllowed) as refused:
        await secrets().value_for(NAME, "https://attacker.test/api")
    assert refused.value.origin == "https://attacker.test:443"
    assert TOKEN not in str(refused.value)


# ---- the cache, a miss and stale-if-error (D42) --------------------------------------


async def test_a_value_is_kept_for_a_minute_and_a_miss_is_never_kept(store, clock):
    assert await secrets().held(NAME) is None
    store.put(NAME, TOKEN, [HA])
    assert (await secrets().held(NAME)).value == TOKEN, "a miss was kept"
    store.put(NAME, "rotated", [HA])
    assert (await secrets().held(NAME)).value == TOKEN
    clock.now += 61
    assert (await secrets().held(NAME)).value == "rotated"
    assert store.asked == [NAME] * 3


async def test_a_cleared_secret_stops_and_its_last_value_with_it(store, clock):
    store.put(NAME, TOKEN, [HA])
    await secrets().held(NAME)
    store.clear(NAME)
    clock.now += 61
    assert await secrets().held(NAME) is None
    store.status = 503   # and a later outage does not bring it back
    clock.now += 61
    assert await secrets().held(NAME) is None


@pytest.mark.parametrize("trouble", ["unreachable", "503"])
async def test_the_last_value_is_kept_while_the_gateway_cannot_answer_and_said_once(
        store, clock, caplog, trouble):
    """A gateway in locked mode (keyring_unreadable) or restarting does not
    take the household's token from the satellites."""
    store.put(NAME, TOKEN, [HA])
    await secrets().held(NAME)
    if trouble == "unreachable":
        store.down = True
    else:
        store.status = 503
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        for _ in range(3):
            clock.now += 61
            assert await secrets().value_for(NAME, HA) == TOKEN
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 1 and NAME in said[0] and TOKEN not in caplog.text


@pytest.mark.parametrize("refusal", [401, 400, 500])
async def test_a_definite_answer_from_the_gateway_stops_a_cached_value_and_is_said_once(
        store, clock, caplog, refusal):
    """Only a gateway that cannot be asked, or answers 503, keeps the last
    value (D42). A 401 is a key it does not take even read again, a 500 a
    row it cannot serve: either is the gateway's word, and deny by default
    says a secret does not outlive it."""
    store.put(NAME, TOKEN, [HA])
    await secrets().held(NAME)
    store.status = refusal
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        for _ in range(2):
            clock.now += 61
            assert await secrets().held(NAME) is None
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 1 and NAME in said[0] and str(refusal) in said[0], said
    assert TOKEN not in caplog.text
    store.status = 503   # and a later outage does not bring it back
    clock.now += 61
    assert await secrets().held(NAME) is None


async def test_a_refused_secret_is_no_value_and_said_once(store, clock, caplog):
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "not_a_consumer"}})
    secret_client.configure(Secrets(transport=httpx.MockTransport(refuse), clock=clock))
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        assert await secrets().held(NAME) is None
        assert await secrets().held(NAME) is None
    assert len(caplog.records) == 1 and "consumers" in caplog.text


async def test_invalidate_and_again_ask_the_store_afresh(store, clock):
    store.put(NAME, TOKEN, [HA])
    await secrets().held(NAME)
    store.put(NAME, "rotated", [HA])
    assert await secrets().again(NAME, TOKEN, HA) == "rotated"
    # Nothing new: no second attempt.
    assert await secrets().again(NAME, "rotated", HA) is None


async def test_a_name_that_is_not_a_secret_name_never_reaches_the_gateway(store, clock):
    for name in ("../health", "satellites_ha_token", "SATELLITES_HA_TOKEN/x", ""):
        assert await secrets().held(name) is None
    assert store.asked == []


# ---- the service key ---------------------------------------------------------------


async def test_the_service_key_goes_to_the_gateways_internal_listener_and_nowhere_else(gateway):
    seen: list[httpx.Request] = []

    def anywhere(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(anywhere)) as client:
        for url in ("http://voice-gateway:8081/v1/audio/speech",
                    "http://voice-gateway:8080/v1/audio/speech",
                    "http://stt-stack:8000/v1/audio/transcriptions",
                    "https://voice-gateway:8081/v1/audio/speech"):
            await hub_gateway.request(client, "POST", url, json={})
    assert [r.headers.get("authorization") for r in seen] == [
        f"Bearer {gateway.service_key}", None, None, None]


async def test_a_401_from_the_gateway_reads_the_key_again_and_tries_once_more(gateway, store,
                                                                              clock):
    """The gateway rotated the key (rotate-service-keys) since the hub read it."""
    store.put(NAME, TOKEN, [HA])
    await secrets().held(NAME)        # the hub has read the key
    rotated = "calliope_svc_" + "R" * 36
    (gateway.directory / SERVICE_KEY_FILE).write_text(rotated + "\n")
    store.key = rotated
    clock.now += 61
    assert (await secrets().held(NAME)).value == TOKEN
    assert store.asked == [NAME, NAME]


async def test_without_its_key_the_hub_asks_nothing_and_says_why(gateway, store, clock):
    """Before the gateway has written service.key (§2.4)."""
    (gateway.directory / SERVICE_KEY_FILE).unlink()
    hub_gateway.CREDENTIALS.reload_service_key()
    assert await secrets().held(NAME) is None
    assert store.asked == []
    with pytest.raises(hub_gateway.NotReady, match="service.key"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(store)) as client:
            await hub_gateway.request(client, "GET", "http://voice-gateway:8081/health")
