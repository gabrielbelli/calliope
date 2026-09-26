"""voice-satellites against a fake device on Starlette's test socket.

Nothing starts a server: TestClient drives the ASGI app and its
websocket_connect plays the satellite. Each test gets its own data directory,
because the store is the thing half of these are about.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import struct

import pytest
from fastapi.testclient import TestClient
from voice_common.conformance import assert_four_field_envelope

MAC = "94:B9:7E:7B:8B:E8"
NID = "94b97e7b8be8"
MODEL = "esp32-korvo-v1.1"


def hello(token: str = "", model: str = MODEL) -> dict:
    """Firmware from before 2026-09-25: no settings in the hello."""
    return {"type": "hello", "id": MAC, "model": model, "fw": "v0", "token": token,
            "caps": {"mic": {"rate": 16000, "channels": 4},
                     "speaker": {"rate": 48000, "channels": 1}}}


# Current firmware says these in every hello as well as in its status.
SETTINGS = {"volume": 60, "mic_gain_db": 30.0, "mic_enabled": True, "speaker_enabled": True,
            "lights_enabled": True}


def mic_frame(seq: int, frames: int = 320, value: int = 0) -> bytes:
    pcm = struct.pack("<h", value) * (frames * 4)
    return struct.pack("<BBBBIQ", 1, 0, 4, 0, seq, 0) + pcm


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SATELLITES_API_KEYS", raising=False)
    monkeypatch.delenv("SATELLITES_TTS_URL", raising=False)
    return importlib.reload(importlib.import_module("app.main"))


@pytest.fixture
def client(app):
    with TestClient(app.app) as c:
        yield c


def adopt(client, ws, name="kitchen") -> str:
    ws.send_json(hello() | SETTINGS)
    assert ws.receive_json() == {"type": "pending"}
    assert client.post(f"/satellites/{NID}/adopt", json={"name": name}).status_code == 200
    msg = ws.receive_json()
    assert msg["type"] == "adopt" and msg["name"] == name
    ws.send_json(hello(msg["token"]) | SETTINGS)
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return msg["token"]


def test_a_new_satellite_waits_and_its_microphone_is_not_listened_to(client):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        ws.send_bytes(mic_frame(0))
        listed = client.get("/satellites").json()["satellites"]
        assert [(n["id"], n["adopted"], n["online"]) for n in listed] == [(NID, False, True)]
        r = client.get(f"/satellites/{NID}/listen", params={"seconds": 1})
        assert r.status_code == 409
        assert_four_field_envelope(r)


def test_adoption_issues_a_token_the_hub_keeps_only_as_a_hash(client, app, tmp_path):
    with client.websocket_connect("/satellites/ws") as ws:
        token = adopt(client, ws)
    stored = (tmp_path / "satellites.json").read_text()
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_a_wrong_token_is_pending_not_welcome(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello("not-the-token"))
        assert ws.receive_json() == {"type": "pending"}


def test_adoption_survives_a_restart_of_the_hub(app, tmp_path):
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        token = adopt(c, ws)
    fresh = importlib.reload(app)
    with TestClient(fresh.app) as c, c.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello(token))
        assert ws.receive_json()["type"] == "welcome"


def test_listen_returns_the_channels_the_satellite_sent(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        # Frames must be flowing while the request waits; the test client runs
        # the request in the same portal, so queue a second's worth first and
        # let the tap pick them up from the next frames.
        import threading

        def feed():
            for i in range(80):
                ws.send_bytes(mic_frame(i, value=100))

        t = threading.Thread(target=feed)
        t.start()
        r = client.get(f"/satellites/{NID}/listen", params={"seconds": 1, "channel": 1})
        t.join()
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert len(r.content) == 44 + 16000 * 2


def test_a_satellite_is_found_by_name_as_well_as_by_id(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, name="kitchen")
        assert client.get("/satellites/kitchen").json()["id"] == NID
        assert client.get(f"/satellites/{MAC}").json()["id"] == NID
        r = client.get("/satellites/nowhere")
        assert r.status_code == 404
        assert_four_field_envelope(r)


def test_config_changes_reach_the_satellite_and_are_bounded(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch("/satellites/kitchen", json={"volume": 30}).status_code == 200
        assert ws.receive_json() == {"type": "config", "volume": 30}
        # FastAPI's own 422 and detail list, as on every native route in the
        # estate; only /v1 is held to OpenAI's 400 envelope.
        r = client.patch("/satellites/kitchen", json={"volume": 101})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "volume"]


def test_firmware_that_is_not_an_esp32_image_is_refused(client):
    r = client.post("/satellites/firmware", params={"model": MODEL}, content=b"hello")
    assert r.status_code == 400
    assert_four_field_envelope(r)


def test_an_update_is_sent_in_frames_the_satellite_can_take(client, app):
    # THE DEFECT: 16 KB chunks. The Arduino WebSockets library drops the whole
    # connection on any frame over 15 KB, and the first real update died 17 ms
    # in. The header is 8 bytes on top of the chunk.
    assert app.OTA_CHUNK + 8 <= 15 * 1024
    image = bytes([0xE9]) + bytes(range(256)) * 200
    fw = client.post("/satellites/firmware", params={"model": MODEL, "version": "v1"},
                     content=image).json()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post("/satellites/ota", json={"satellite": "kitchen", "sha256": fw["sha256"]})
        assert r.json() == {"started": [NID], "skipped": {}}
        start = ws.receive_json()
        assert start == {"type": "ota", "size": len(image), "sha256": fw["sha256"], "version": "v1"}
        got = bytearray()
        while len(got) < len(image):
            ws.send_json({"type": "ota_next", "offset": len(got)})
            frame = ws.receive_bytes()
            assert frame[0] == 3 and struct.unpack_from("<I", frame, 4)[0] == len(got)
            got += frame[8:]
        assert bytes(got) == image


def test_an_update_is_not_sent_to_the_wrong_model(client):
    image = bytes([0xE9]) + bytes(1000)
    fw = client.post("/satellites/firmware", params={"model": "other-board"}, content=image).json()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post("/satellites/ota", json={"satellite": "all", "sha256": fw["sha256"]}).json()
    assert r["started"] == [] and "other-board" in r["skipped"][NID]


def test_say_without_a_voice_configured_says_why(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post("/satellites/kitchen/say", json={"text": "hello"})
    assert r.status_code == 503 and "SATELLITES_TTS_URL" in r.json()["error"]["message"]


def test_the_socket_refuses_anything_but_a_hello_first(client):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_text(json.dumps({"type": "status"}))
        with pytest.raises(Exception):
            ws.receive_json()


def test_adopting_a_dark_satellite_keeps_it_dark(client):
    """A satellite moved from another hub reports lights_enabled false while
    it is pending. The new hub's default is true, and adoption used to take
    the default -- the welcome would have lit a ring in a room where someone
    was asleep."""
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        ws.send_json({"type": "status", "lights_enabled": False, "volume": 35, "rssi": -60})
        client.get("/satellites")  # let the status land before adopting
        assert client.post(f"/satellites/{NID}/adopt", json={"name": "bedroom"}).status_code == 200
        token = ws.receive_json()["token"]
        ws.send_json(hello(token))
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        assert welcome["config"]["lights_enabled"] is False
        assert welcome["config"]["volume"] == 35


def test_forgetting_a_satellite_that_was_only_seen_removes_it(client):
    """It used to stay on the Satellites tab as "seen, not adopted" until
    the hub restarted, with Forget answering 204 and changing nothing."""
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
    assert [n["id"] for n in client.get("/satellites").json()["satellites"]] == [NID]
    assert client.post(f"/satellites/{NID}/forget").status_code == 204
    assert client.get("/satellites").json()["satellites"] == []


def test_a_finished_update_leaves_the_card_after_a_while(client, app, monkeypatch):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        ws.send_json({"type": "ota", "state": "failed", "error": "bad signature", "version": "x"})
        client.get("/satellites")
        assert client.get("/satellites/kitchen").json()["ota"]["error"] == "bad signature"
        later = app.time.time() + app.OTA_RESULT_S + 1
        monkeypatch.setattr(app.time, "time", lambda: later)
        assert client.get("/satellites/kitchen").json()["ota"] is None


# ---- the rename from "nodes" (2026-09-25) ----------------------------------


def test_a_board_on_firmware_from_before_the_rename_still_reaches_the_hub(client, app):
    """A board in the field connects to /nodes/ws until it is updated, and the
    update itself arrives over that socket. If the old path stopped answering,
    or reached a handler that behaved differently, the board could only be
    brought back over USB."""
    sockets = {r.path: r.endpoint for r in app.app.routes
               if r.path in (app.SOCKET, app.LEGACY_SOCKET)}
    assert sockets == {"/satellites/ws": app.satellite_socket,
                       "/nodes/ws": app.satellite_socket}
    with client.websocket_connect("/nodes/ws") as ws:
        token = adopt(client, ws)
    # One adoption, whichever path the board comes back on.
    for path in ("/nodes/ws", "/satellites/ws"):
        with client.websocket_connect(path) as ws:
            ws.send_json(hello(token))
            assert ws.receive_json()["type"] == "welcome", path


def _nodes_json(tmp_path, token: str) -> None:
    """nodes.json exactly as the hub wrote it before the rename."""
    (tmp_path / "nodes.json").write_text(json.dumps({"nodes": [{
        "id": NID, "name": "bedroom", "model": MODEL,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "adopted_at": 1.0, "config": {"volume": 35, "lights_enabled": False}}]}, indent=2))


def test_a_volume_from_before_the_rename_keeps_every_adoption(app, tmp_path):
    """The hub wrote nodes.json until 2026-09-25. A hub that looked only for
    satellites.json would start with nothing adopted, every board would come
    back pending, and a dark bedroom board would be adopted again with its
    lights on by default."""
    _nodes_json(tmp_path, "token-from-before")
    with TestClient(app.app) as c:
        with c.websocket_connect("/satellites/ws") as ws:
            ws.send_json(hello("token-from-before"))
            welcome = ws.receive_json()
    assert welcome["type"] == "welcome" and welcome["name"] == "bedroom"
    assert welcome["config"]["lights_enabled"] is False
    assert welcome["config"]["volume"] == 35
    migrated = json.loads((tmp_path / "satellites.json").read_text())
    assert [s["id"] for s in migrated["satellites"]] == [NID]
    # Left in place: the image from before the rename still starts with it.
    assert json.loads((tmp_path / "nodes.json").read_text())["nodes"][0]["id"] == NID


def test_the_old_file_is_not_read_again_once_it_has_been_migrated(app, tmp_path):
    """nodes.json stays on the volume after the migration. Read at every start,
    it would bring back a satellite forgotten since, token and all."""
    _nodes_json(tmp_path, "token-from-before")
    with TestClient(app.app) as c:
        assert c.post(f"/satellites/{NID}/forget").status_code == 204
    with TestClient(app.app) as c:
        assert c.get("/satellites").json()["satellites"] == []
        with c.websocket_connect("/satellites/ws") as ws:
            ws.send_json(hello("token-from-before"))
            assert ws.receive_json() == {"type": "pending"}


# ---- settings the satellite has not reported yet ------------------------------


def status(**settings) -> dict:
    return {"type": "status", "rssi": -60, "muted": False} | settings


def config_of(client) -> dict:
    return client.get(f"/satellites/{NID}").json()["config"]


def until(check, what: str, timeout: float = 5.0):
    """A message sent on the test socket is handled on the hub's own loop, so
    a request right after it can overtake it."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def adopt_before_any_status(client, ws, name="bedroom") -> tuple[dict, str]:
    """Firmware from before 2026-09-25, adopted in the seconds before its
    first status: the hub has heard none of its settings. The welcome, and
    the token."""
    ws.send_json(hello())
    assert ws.receive_json() == {"type": "pending"}
    assert client.post(f"/satellites/{NID}/adopt", json={"name": name}).status_code == 200
    token = ws.receive_json()["token"]
    ws.send_json(hello(token))
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return welcome, token


def test_a_satellite_adopted_before_it_reported_keeps_its_own_settings_and_the_hub_takes_them_from_its_status(
        client):
    """The welcome used to carry the hub's defaults for settings it had never
    heard, which switched on the ring and speaker of a satellite that was dark
    and silent. Left out, the satellite keeps its own, and the status it sends
    on applying the welcome is where the hub learns them."""
    with client.websocket_connect("/satellites/ws") as ws:
        welcome, _ = adopt_before_any_status(client, ws)
        assert welcome["config"] == {"local_volume_buttons": True}
        ws.send_json(status(**SETTINGS | {"lights_enabled": False, "volume": 35}))
        until(lambda: config_of(client)["mic_enabled"] is True, "the status")
        config = config_of(client)
    assert config["lights_enabled"] is False and config["volume"] == 35
    assert config["speaker_enabled"] is True and config["mic_gain_db"] == 30.0


def test_until_a_satellite_reports_its_lights_the_hub_does_not_light_it_and_then_it_may(client):
    """The hub's record says off for a switch it has not heard, so nothing
    lights a ring on a setting the hub made up. Once the satellite says its
    lights are on, they are."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_before_any_status(client, ws)
        r = client.post(f"/satellites/{NID}/lights", json={"mode": "solid"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "lights_disabled"
        ws.send_json(status(**SETTINGS))
        until(lambda: config_of(client)["lights_enabled"] is True, "the status")
        assert client.post(f"/satellites/{NID}/lights", json={"mode": "solid"}).status_code == 204
        assert ws.receive_json()["type"] == "lights"


def test_a_setting_changed_before_the_satellite_reported_it_is_not_undone_by_its_report(client):
    """Changed on the page in the second after adoption, the volume is the
    hub's and is sent to the satellite. A status that was already on its way
    with the old value must not put that back in the record."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_before_any_status(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"volume": 20}).status_code == 200
        assert ws.receive_json() == {"type": "config", "volume": 20}
        ws.send_json(status(**SETTINGS | {"volume": 35}))
        until(lambda: config_of(client)["mic_enabled"] is True, "the status")
        assert config_of(client)["volume"] == 20


def test_current_firmware_says_its_settings_in_the_hello_so_a_dark_satellite_adopted_at_once_is_welcomed_dark(
        client):
    """No wait for a status at all: the hello has them, and the welcome
    repeats the satellite's own settings back to it."""
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello() | SETTINGS | {"lights_enabled": False})
        assert ws.receive_json() == {"type": "pending"}
        assert client.post(f"/satellites/{NID}/adopt", json={"name": "bedroom"}).status_code == 200
        ws.send_json(hello(ws.receive_json()["token"]) | SETTINGS | {"lights_enabled": False})
        welcome = ws.receive_json()
    assert welcome["config"] == SETTINGS | {"lights_enabled": False, "local_volume_buttons": True}


def test_settings_a_satellite_has_not_reported_stay_out_of_its_welcome_across_a_restart_of_the_hub(app):
    """Restarted between the adoption and the first status, a hub that saved
    only the config would welcome the satellite with the values it holds in
    their place, lights and speaker off: a lit satellite would go dark."""
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        _, token = adopt_before_any_status(c, ws)
    fresh = importlib.reload(app)
    with TestClient(fresh.app) as c, c.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello(token))
        welcome = ws.receive_json()
        config = c.get(f"/satellites/{NID}").json()["config"]
    assert welcome["type"] == "welcome" and welcome["config"] == {"local_volume_buttons": True}
    assert config["lights_enabled"] is False and config["speaker_enabled"] is False


def test_a_reported_setting_is_believed_only_within_what_patch_accepts(app):
    """A pending connection can say anything in its status. A volume of 500
    or a switch that says "yes" is not a setting, and adopting that
    connection must not put it in the record, where PATCH could never have."""
    from app.store import REPORTED, reported_config

    assert set(REPORTED) <= set(app.ConfigBody.model_fields), "a reported key PATCH cannot set"
    assert reported_config({"volume": 500, "mic_gain_db": 40, "lights_enabled": "yes",
                            "speaker_enabled": 1, "mic_enabled": None, "buttons": {}}) == {}
    assert reported_config({"volume": True}) == {}
    good = {"volume": 100, "mic_gain_db": 37.5, "lights_enabled": False, "speaker_enabled": True,
            "mic_enabled": False}
    assert reported_config(good | {"rssi": -60, "uptime_s": 3}) == good
    app.ConfigBody(**good)  # and PATCH takes every one of them


# ---- who may take an adopted satellite's place --------------------------------


def test_a_satellite_that_reconnects_with_its_token_still_takes_over_from_its_stale_socket(client, app):
    """The other side of refusing a hello without the token: a board that
    reboots comes back before the hub has noticed its old socket is dead, and
    must not be kept out by it."""
    with client.websocket_connect("/satellites/ws") as old:
        token = adopt(client, old)
        with client.websocket_connect("/satellites/ws") as new:
            new.send_json(hello(token) | SETTINGS)
            assert new.receive_json()["type"] == "welcome"
            assert client.post(f"/satellites/{NID}/reboot").status_code == 204
            assert new.receive_json() == {"type": "reboot"}


# ---- the rename from "nodes" (2026-09-25): settings left behind ------------------


def test_a_setting_left_under_its_nodes_name_is_named_at_start_with_the_name_now_read(
        app, monkeypatch, caplog):
    """NODES_MQTT_URL set in the app's secret settings stopped being read at
    the rename, and MQTT switched off without a word. The warning names it
    and the new name, and never the value, which holds the broker's password."""
    import logging

    monkeypatch.setenv("NODES_MQTT_URL", "mqtt://calliope:s3cret@broker.test:1883")
    with caplog.at_level(logging.WARNING), TestClient(app.app):
        pass
    said = [r.getMessage() for r in caplog.records if "NODES_MQTT_URL" in r.getMessage()]
    assert said and "SATELLITES_MQTT_URL" in said[0] and "not set" in said[0]
    assert "s3cret" not in caplog.text


def test_a_nodes_secret_a_routing_rule_still_names_is_not_called_unread(app):
    """rules.json keeps every field, so a rule saved before the rename reads
    NODES_HA_TOKEN by name. Calling that variable unread would send the
    operator off to rename the one secret that works; what is worth saying is
    a rule whose NODES_ secret is not set, which is a secret renamed and a
    rule that was not."""
    from app import router as routing

    rule = routing.Rule(id="ha", destination={"type": "ha_conversation",
                                              "url": "http://ha.test:8123",
                                              "token_env": "NODES_HA_TOKEN"})
    assert app.legacy_settings({"NODES_HA_TOKEN": "t"}, [rule]) == []
    [unset] = app.legacy_settings({"SATELLITES_HA_TOKEN": "t"}, [rule])
    assert "'ha'" in unset and "NODES_HA_TOKEN" in unset and "SATELLITES_HA_TOKEN" in unset


def test_a_satellites_account_of_its_start_up_is_kept_and_shown(client):
    """A board sat between lighting its ring and starting Wi-Fi for hours on
    one power source; the hub is where its own report of that now lands."""
    with client.websocket_connect("/satellites/ws") as ws:
        report = {"stages_ms": {"start": 0, "codec": 95012, "wifi": 95040},
                  "stalled_in": "codec", "stall_restarts": 1}
        ws.send_json(hello() | {"reset_reason": 3, "boot": report})
        assert ws.receive_json()["type"] == "pending"
        boot = client.get(f"/satellites/{NID}").json()["boot"]
        assert boot["stalled_in"] == "codec" and boot["stages_ms"]["codec"] == 95012
        assert boot["reset_reason"] == 3
