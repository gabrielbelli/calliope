"""Who may reach the hub, and what each caller is shown (D15, D41, D53, D62, D65).

The gateway decides who may call what. The hub requires its assertion on
every request and the relay's on the device socket, bounds what a stranger's
hellos cost, and shapes its answers by the caller's scopes: a button mapping
and the wake words' actions only to satellites:admin, and a PATCH beyond the
controls only from satellites:admin.

conftest signs every request as an admin's session and the socket as the
relay; a narrower caller is played with gateway.headers(...).
"""

from __future__ import annotations

import importlib
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from voice_common.identity import ASSERTION_HEADER

from app import wakeword
from app.store import Store
from test_pipeline import FakeWakeWords
from test_satellites import MODEL, NID, adopt, hello, published_by, until

RELAY = "svc:gateway-relay"
READER = ["satellites:read", "satellites:control", "satellites:update"]  # the home-assistant preset's
HOOK_NAME = "SATELLITES_BUTTON_HOOK"
HOOK_URL = "https://ha.test/api/webhook/s3cret-hook-id"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    return importlib.reload(importlib.import_module("app.main"))


@pytest.fixture
def client(app):
    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def no_downloads(monkeypatch):
    """A wake word saved is not fetched from the network: a stand-in loads it."""
    monkeypatch.setattr(wakeword, "ensure_models", lambda names, model_dir, **kw: None)
    monkeypatch.setattr(wakeword, "WakeWords", FakeWakeWords)


def reader(gateway) -> dict:
    return gateway.headers("satellites", scopes=READER)


def mac(n: int, block: int = 1) -> str:
    return f"02:00:00:00:{block:02x}:{n:02x}"


def say_hello(client, *, forwarded: str | None = None, **fields) -> dict:
    """One tokenless hello on a fresh socket: what the hub answered."""
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    with client.websocket_connect("/satellites/ws", headers=headers) as ws:
        ws.send_json(hello() | fields)
        return ws.receive_json()


# ---- the device socket (D53) -----------------------------------------------------------


@pytest.mark.parametrize("who", ["nobody", "a person", "the relay, for another service"])
def test_the_device_socket_opens_only_for_the_gateways_relay(client, gateway, who):
    headers = {
        "nobody": {ASSERTION_HEADER: ""},
        "a person": gateway.headers("satellites"),
        "the relay, for another service": gateway.headers("ui", sub=RELAY, kind="service",
                                                          scopes=()),
    }[who]
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/satellites/ws", headers=headers) as ws:
            ws.send_json(hello())
            ws.receive_json()
    assert closed.value.code == 1008
    assert client.get("/satellites").json()["satellites"] == []


@pytest.mark.parametrize("method, path", [
    ("GET", "/satellites"), ("POST", f"/satellites/{NID}/adopt"), ("GET", "/satellites/telemetry"),
    ("GET", "/satellites/wake-words")])
def test_the_relays_assertion_opens_no_http_route(client, gateway, method, path):
    """It exists to relay the device socket, and holds no scope the hub could
    check: anywhere else it is refused, in case it is ever minted or
    replayed for another path within its minute."""
    relay = gateway.headers("satellites", sub=RELAY, kind="service", scopes=())
    r = client.request(method, path, headers=relay)
    assert r.status_code == 403 and r.json()["error"]["code"] == "relay_only", r.text


def test_an_unadopted_device_still_gets_pending_through_the_relay(client):
    assert say_hello(client) == {"type": "pending"}


def test_forty_adopted_satellites_reconnecting_from_one_address_are_all_welcomed(app, tmp_path):
    """After a hub restart they all come back at once, all through the
    gateway's one address: a hello with a valid token is never held back."""
    held = Store(tmp_path)
    tokens = {mac(i): held.adopt(mac(i).replace(":", ""), f"satellite-{i}", MODEL)
              for i in range(40)}
    with TestClient(app.app) as c:
        welcomed = []
        for address, token in tokens.items():
            with c.websocket_connect("/satellites/ws",
                                     headers={"X-Forwarded-For": "198.51.100.7"}) as ws:
                ws.send_json(hello(token) | {"id": address})
                welcomed.append(ws.receive_json()["type"])
    assert welcomed == ["welcome"] * 40


def test_the_eleventh_hello_without_a_token_in_a_minute_from_one_address_is_refused(client):
    for i in range(10):
        assert say_hello(client, forwarded="203.0.113.9", id=mac(i, 2)) == {"type": "pending"}
    with client.websocket_connect("/satellites/ws",
                                  headers={"X-Forwarded-For": "203.0.113.9"}) as ws:
        ws.send_json(hello() | {"id": mac(10, 2)})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1013
    # Another address is not held back by that one.
    assert say_hello(client, forwarded="203.0.113.10", id=mac(11, 2)) == {"type": "pending"}


def test_the_hello_count_forgets_after_a_minute_and_stays_bounded(app):
    """Anyone can make up addresses (recheck M-4): an address forgotten
    loses only its count."""
    now = [0.0]
    hellos = app.HelloThrottle(clock=lambda: now[0])
    assert all(hellos.allow("192.0.2.1") for _ in range(app.HELLOS_PER_MINUTE))
    assert not hellos.allow("192.0.2.1")
    now[0] += 60
    assert hellos.allow("192.0.2.1")
    for i in range(100_000):
        hellos.allow(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}")
    assert len(hellos._heard) == app.HELLO_ADDRESSES


def test_at_most_32_pending_satellites_are_kept_and_the_oldest_goes(client, app):
    """Anyone on the internet can say hello under any MAC (recheck L10): the
    newest are kept, the oldest connected one is closed, and each shows the
    address it said hello from."""
    with client.websocket_connect("/satellites/ws",
                                  headers={"X-Forwarded-For": "192.0.2.200"}) as oldest:
        oldest.send_json(hello() | {"id": mac(0, 3)})
        assert oldest.receive_json() == {"type": "pending"}
        for i in range(1, app.PENDING_MAX + 2):
            assert say_hello(client, forwarded=f"192.0.2.{i}", id=mac(i, 3)) == {"type": "pending"}
        with pytest.raises(WebSocketDisconnect) as closed:
            oldest.receive_json()
    assert closed.value.code == 1013
    pending = {s["id"]: s["address"] for s in client.get("/satellites").json()["satellites"]}
    assert len(pending) == app.PENDING_MAX
    assert mac(0, 3).replace(":", "") not in pending and mac(1, 3).replace(":", "") not in pending
    assert pending[mac(app.PENDING_MAX + 1, 3).replace(":", "")] == f"192.0.2.{app.PENDING_MAX + 1}"


@pytest.mark.parametrize("forwarded, shown", [
    ("198.51.100.20", "198.51.100.20"),
    ("203.0.113.5, 198.51.100.20", "198.51.100.20"),    # the gateway's own entry is the last
    ("not-an-address", "testclient"),
])
def test_the_address_shown_is_the_one_the_gateway_forwarded(client, forwarded, shown):
    with client.websocket_connect("/satellites/ws", headers={"X-Forwarded-For": forwarded}) as ws:
        ws.send_json(hello())
        ws.receive_json()
        [listed] = client.get("/satellites").json()["satellites"]
    assert listed["address"] == shown


# ---- button webhooks (D62, H4) -----------------------------------------------------------


def test_without_satellites_admin_no_button_mapping_is_shown(client, app, gateway):
    events = published_by(app)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        saved = client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}, "mode": {"press": f"webhook:secret:{HOOK_NAME}"}}})
        listed = client.get("/satellites", headers=reader(gateway)).json()["satellites"][0]
        one = client.get(f"/satellites/{NID}", headers=reader(gateway)).json()
        admin = client.get(f"/satellites/{NID}").json()
    assert saved.status_code == 200, saved.text
    assert "buttons" not in listed["config"] and "buttons" not in one["config"]
    assert listed["config"]["volume"] == 60      # the rest is there
    assert admin["config"]["buttons"]["mode"] == {"press": f"webhook:secret:{HOOK_NAME}"}
    # The config event names what changed; MQTT gets describe() with no claims.
    assert [e for e in events if e["type"] == "config"] == [
        {"type": "config", "satellite": NID, "changed": ["buttons"]}]
    assert "buttons" not in app.hub.describe(NID)["config"]


def test_a_raw_webhook_url_in_a_button_mapping_is_refused_with_use_secret(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        before = client.get(f"/satellites/{NID}").json()["config"]["buttons"]
        r = client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}, "mode": {"press": f"webhook:{HOOK_URL}"}}})
        after = client.get(f"/satellites/{NID}").json()["config"]["buttons"]
    assert r.status_code == 422 and r.json()["error"]["code"] == "use_secret", r.text
    assert "s3cret" not in r.text and after == before


def test_a_button_cannot_name_a_secret_that_is_not_an_address(client, store):
    """recheck L5: a bearer named as a webhook's address would be posted to
    as a URL. Refused when the mapping is saved, by its name only."""
    store.put("SATELLITES_HA_TOKEN", "ha-token-do-not-leak", ["https://ha.test"])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}, "mode": {"press": "webhook:secret:SATELLITES_HA_TOKEN"}}})
        before_it_is_stored = client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}, "mode": {"press": f"webhook:secret:{HOOK_NAME}"}}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_secret_url", r.text
    assert "SATELLITES_HA_TOKEN is a bearer" in r.text and "do-not-leak" not in r.text
    assert before_it_is_stored.status_code == 200, before_it_is_stored.text


def test_a_press_posts_only_to_a_host_its_secret_names_and_logs_no_address(
        client, app, store, caplog):
    """M-2: what is logged is the secret's name, a status and an exception's
    type, never the address, whose path is Home Assistant's webhook id. And
    a redirect from an allowed host is not followed (D41)."""
    posted: list[httpx.Request] = []

    def hook(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        if "refuse" in request.url.path:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        if "moved" in request.url.path:
            return httpx.Response(307, headers={"Location": "https://elsewhere.test/collect"})
        return httpx.Response(500, text=f"no webhook at {request.url}")
    app.hub.http = httpx.AsyncClient(transport=httpx.MockTransport(hook))
    store.put(HOOK_NAME, HOOK_URL, ["https://ha.test"], kind="secret_url")
    store.put("SATELLITES_BUTTON_DOWN", "https://ha.test/api/webhook/s3cret-refuse",
              ["https://ha.test"], kind="secret_url")
    store.put("SATELLITES_BUTTON_ELSEWHERE", "https://elsewhere.test/api/webhook/s3cret-two",
              ["https://ha.test"], kind="secret_url")
    store.put("SATELLITES_BUTTON_MOVED", "https://ha.test/api/webhook/s3cret-moved",
              ["https://ha.test"], kind="secret_url")
    with caplog.at_level(logging.DEBUG), client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}, "mode": {"press": f"webhook:secret:{HOOK_NAME}"},
            "play": {"press": "webhook:secret:SATELLITES_BUTTON_ELSEWHERE"},
            "set": {"press": "webhook:secret:SATELLITES_BUTTON_DOWN"},
            "vol_up": {"press": "webhook:secret:SATELLITES_BUTTON_MOVED"}}})
        for button in ("mode", "play", "set", "vol_up"):
            ws.send_json({"type": "button", "button": button, "action": "press"})
        until(lambda: caplog.text.count("button webhook") >= 4, "the four presses")
    assert sorted(str(r.url) for r in posted) == [
        "https://ha.test/api/webhook/s3cret-hook-id", "https://ha.test/api/webhook/s3cret-moved",
        "https://ha.test/api/webhook/s3cret-refuse"]
    assert "button webhook SATELLITES_BUTTON_MOVED answered 307" in caplog.text
    assert all(not k.lower().startswith("x-calliope-") for r in posted for k in r.headers)
    assert f"button webhook {HOOK_NAME} answered 500" in caplog.text
    assert "button webhook SATELLITES_BUTTON_DOWN failed: ConnectError" in caplog.text
    assert "SATELLITES_BUTTON_ELSEWHERE: nothing was sent" in caplog.text
    assert "https://elsewhere.test:443" in caplog.text     # the host, never the path
    assert "s3cret" not in caplog.text


# ---- the microphone (D15, H3) -------------------------------------------------------------


def test_listening_is_a_post_and_a_get_opens_no_microphone(client, app):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.get(f"/satellites/{NID}/listen", params={"seconds": 1})
        tapped = app.hub.sessions[NID].taps
    assert r.status_code == 405 and not tapped


# ---- PATCH: the controls and the configuration (§3.5) ----------------------------------


def test_the_controls_need_satellites_control_and_anything_else_satellites_admin(client, gateway):
    control = gateway.headers("satellites", scopes=["satellites:read", "satellites:control"])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        ok = client.patch(f"/satellites/{NID}", headers=control, json={
            "volume": 30, "mic_enabled": True, "speaker_enabled": True, "lights_enabled": True,
            "brightness": 50, "mic_gain_db": 20})
        refused = [client.patch(f"/satellites/{NID}", headers=control, json=body) for body in (
            {"name": "attic"}, {"ring_top": 3}, {"local_volume_buttons": False},
            {"volume": 20, "buttons": {"rec": {"press": "mute"}}})]
        config = client.get(f"/satellites/{NID}").json()
        renamed = client.patch(f"/satellites/{NID}", json={"name": "attic"})
    assert ok.status_code == 200, ok.text
    assert "buttons" not in ok.json()["config"]
    for r in refused:
        assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope", r.text
        assert 'scope="satellites:admin"' in r.headers["WWW-Authenticate"]
    # Refused whole: not even the volume beside the mapping changed.
    assert (config["name"], config["config"]["volume"], config["config"]["ring_top"]) == (
        "kitchen", 30, 0)
    assert renamed.status_code == 200 and renamed.json()["name"] == "attic"


# ---- the wake words (§3.5) -----------------------------------------------------------------

LLM_WORD = {"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"], "mode": "command",
            "action": {"destination": {"type": "llm", "base_url": "http://llm.test/v1",
                                       "model": "tiny", "api_key_env": "SATELLITES_LLM_API_KEY"}}}


def test_a_wake_words_action_is_shown_by_kind_alone_without_satellites_admin(
        no_downloads, client, app, gateway):
    saved = client.put("/satellites/wake-words", json={"words": [LLM_WORD]})
    seen = client.get("/satellites/wake-words", headers=reader(gateway)).json()
    whole = client.get("/satellites/wake-words").json()
    assert saved.status_code == 200, saved.text
    [word] = seen["words"]
    assert {k: word[k] for k in ("name", "threshold", "satellites", "mode", "action")} == {
        "name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"], "mode": "command",
        "action": {"type": "llm"}}
    assert set(seen) == {"words", "ptt"}
    for secretish in ("llm.test", "SATELLITES_LLM_API_KEY", "tiny"):
        assert secretish not in json.dumps(seen)
    assert whole["words"][0]["action"]["destination"]["base_url"] == "http://llm.test/v1"
    # And the event that says a word's model became ready.
    event = {"type": "wake_words", "words": app.hub.voice.views()}
    assert "llm.test" not in json.dumps(app.for_subscriber(event, admin=False, hears=True))
    assert app.for_subscriber(event, admin=True, hears=True) == event


@pytest.mark.parametrize("kind", ["turn", "routed", "wake_rejected"])
def test_what_a_turn_heard_and_said_reaches_only_a_subscriber_that_may_hear_it(app, kind):
    """satellites:read alone (the firmware-release and monitor presets) must not
    be a live feed of what is said in the house (D32)."""
    event = {"type": kind, "satellite": "s1", "rule_id": "r", "transcript": "unlock the door",
             "reply_text": "Done.", "spoken_text": "Done.", "error": "said: unlock the door",
             "heard": "hey jarvis unlock"}
    seen = app.for_subscriber(event, admin=False, hears=False)
    assert seen == {"type": kind, "satellite": "s1", "rule_id": "r"}
    assert app.for_subscriber(event, admin=False, hears=True) == event


def test_a_webhooks_url_secret_must_be_an_address(no_downloads, client, store):
    """recheck L5, for a wake word: url_secret names a secret_url, and a
    store that does not say what a secret is says no."""
    store.put("SATELLITES_HA_TOKEN", "ha-token-do-not-leak", ["https://ha.test"])
    store.put(HOOK_NAME, HOOK_URL, ["https://ha.test"], kind="secret_url")
    hook = LLM_WORD | {"action": {"destination": {"type": "webhook",
                                                  "url_secret": "SATELLITES_HA_TOKEN"}}}
    refused = client.put("/satellites/wake-words", json={"words": [hook]})
    store.kinds = False
    unsaid = client.put("/satellites/wake-words", json={"words": [
        hook | {"action": {"destination": {"type": "webhook", "url_secret": HOOK_NAME}}}]})
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "not_a_secret_url" and "do-not-leak" not in refused.text
    assert unsaid.status_code == 422 and "no stated kind" in unsaid.text, unsaid.text


# ---- the gateway down --------------------------------------------------------------------


def test_the_hub_serves_its_satellites_while_the_gateway_is_down(app, store):
    store.down = True
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        adopt(c, ws)
        healthy = c.get("/health").json()
    assert healthy["status"] == "ok" and healthy["satellites"]["adopted"] == 1


def test_a_hello_with_a_valid_token_is_never_held_back_by_ones_without(client):
    with client.websocket_connect("/satellites/ws") as ws:
        token = adopt(client, ws)
    refused = 0
    for i in range(12):
        try:
            say_hello(client, id=mac(i, 4))
        except WebSocketDisconnect:
            refused += 1
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello(token))
        assert ws.receive_json()["type"] == "welcome"
    assert refused >= 2
