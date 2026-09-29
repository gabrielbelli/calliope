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

MAC = "02:00:00:00:00:01"
NID = "020000000001"
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


def test_an_oversized_upload_is_refused_before_it_is_held_whole(client, app):
    """request.body() held an upload of any size in memory before comparing
    it with the limit. A declared length over it is refused before a byte is
    read, and one sent without a length as soon as it passes it."""
    big = bytes([0xE9]) + bytes(app.MAX_FIRMWARE)
    r = client.post("/satellites/firmware", params={"model": MODEL}, content=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "upload_too_large"

    def chunks():
        for _ in range(app.MAX_FIRMWARE // 65536 + 2):
            yield bytes([0xE9]) * 65536
    r = client.post("/satellites/firmware", params={"model": MODEL}, content=chunks())
    assert r.status_code == 413 and r.json()["error"]["code"] == "upload_too_large"
    assert client.get("/satellites/firmware").json()["firmware"] == []


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


# ---- the satellite's own volume buttons -----------------------------------------


def press(ws, button: str) -> None:
    ws.send_json({"type": "button", "button": button, "action": "press", "held_ms": 0})


def heard(client, rssi: int):
    """A status that carries a marker, so a test knows the hub has handled it
    and every message before it."""
    until(lambda: client.get(f"/satellites/{NID}").json()["status"].get("rssi") == rssi,
          f"the status with rssi {rssi}")


def test_the_volume_a_satellite_set_with_its_own_buttons_becomes_the_hubs(client, app):
    """VOL+ changes the volume on the satellite, which reports it at once. The
    hub took a reported volume only before it had one of its own, so the page
    kept showing the old value, and the next welcome put it back on the
    satellite."""
    published = []
    publish = app.hub.publish
    app.hub.publish = lambda e: (published.append(e), publish(e))
    with client.websocket_connect("/satellites/ws") as ws:
        token = adopt(client, ws)
        press(ws, "vol_up")
        ws.send_json(status(**SETTINGS | {"volume": 70}))
        until(lambda: config_of(client)["volume"] == 70, "the volume from the buttons")
    assert [(e["satellite"], e["settings"]) for e in published if e["type"] == "settings"] == [
        (NID, {"volume": 70})]
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello(token) | SETTINGS | {"volume": 70})
        assert ws.receive_json()["config"]["volume"] == 70


def test_only_the_status_that_answers_the_press_is_taken(client):
    """The one sent with the new volume. A heartbeat after it, or one that
    follows another button, is not believed on the volume: it may have left
    before the page changed it."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        ws.send_json(status(**SETTINGS | {"volume": 35}))
        press(ws, "play")
        ws.send_json(status(**SETTINGS | {"volume": 35, "rssi": -50}))
        heard(client, -50)
        assert config_of(client)["volume"] == 60
        press(ws, "vol_down")
        ws.send_json(status(**SETTINGS | {"volume": 50}))
        ws.send_json(status(**SETTINGS | {"volume": 35, "rssi": -51}))
        heard(client, -51)
        assert config_of(client)["volume"] == 50


def test_a_status_long_after_the_press_is_a_heartbeat_not_the_answer(client, app, monkeypatch):
    monkeypatch.setattr(app, "VOLUME_PRESS_S", 0.05)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        press(ws, "vol_up")
        import time
        time.sleep(0.2)
        ws.send_json(status(**SETTINGS | {"volume": 70, "rssi": -50}))
        heard(client, -50)
        assert config_of(client)["volume"] == 60


def test_a_press_of_a_button_mapped_to_nothing_changes_nothing_on_the_hub(client):
    """The satellite leaves its volume alone and reports the press only; its
    status is the volume the hub gave it, however it reads."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"buttons": {
            "rec": {"press": "mute"}}}).status_code == 200
        press(ws, "vol_down")
        ws.send_json(status(**SETTINGS | {"volume": 50, "rssi": -50}))
        heard(client, -50)
        assert config_of(client)["volume"] == 60


def test_a_status_the_satellites_button_caused_is_taken_whatever_the_button(client, app):
    """Current firmware marks it: lights and brightness as well as volume,
    and no press window needed. Other settings in it are not believed."""
    published = []
    publish = app.hub.publish
    app.hub.publish = lambda e: (published.append(e), publish(e))
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        # Current firmware says its brightness, as it does its other settings.
        ws.send_json(status(**SETTINGS | {"brightness": 100, "rssi": -50}))
        heard(client, -50)
        ws.send_json(status(**SETTINGS | {"lights_enabled": False, "brightness": 35,
                                         "mic_gain_db": 12.0}) | {"cause": "button"})
        until(lambda: config_of(client)["brightness"] == 35, "the button's settings")
        config = config_of(client)
    assert config["lights_enabled"] is False and config["mic_gain_db"] == 30.0
    assert [e["settings"] for e in published if e["type"] == "settings"] == [
        {"lights_enabled": False, "brightness": 35}]


def test_brightness_is_a_setting_from_1_to_100(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"brightness": 40}).status_code == 200
        assert ws.receive_json() == {"type": "config", "brightness": 40}
        assert [client.patch(f"/satellites/{NID}", json={"brightness": b}).status_code
                for b in (0, 101, 50.5)] == [422, 422, 422]


@pytest.mark.parametrize("local, pair", [(True, True), (False, False)])
def test_a_mapping_saved_before_button_actions_keeps_what_the_buttons_did(app, tmp_path, local, pair):
    """No Rec in it (the firmware kept the mute) and no volume pair while the
    firmware kept that too: given them, so nothing changes under a finger."""
    (tmp_path / "satellites.json").write_text(json.dumps({"satellites": [{
        "id": NID, "name": "bedroom", "model": MODEL, "token_sha256": "0" * 64,
        "adopted_at": 1.0, "config": {"local_volume_buttons": local,
                                      "buttons": {"play": {"press": "ptt"}}}}]}))
    with TestClient(app.app) as c:
        buttons = c.get(f"/satellites/{NID}").json()["config"]["buttons"]
    assert buttons["rec"] == {"press": "mute"} and buttons["play"] == {"press": "ptt"}
    assert ("vol_up" in buttons and "vol_down" in buttons) is pair


def test_a_mapping_saved_with_its_only_mute_on_key1_gets_rec_as_the_mute(app, tmp_path):
    """The hub took a mute on key1 alone as a mute, and the firmware does not:
    a stock board does not wire KEY1, so it makes Rec's press the mute
    whatever the table says. The saved mapping now says what the satellite
    does, and the welcome sends it that."""
    (tmp_path / "satellites.json").write_text(json.dumps({"satellites": [{
        "id": NID, "name": "bedroom", "model": MODEL, "token_sha256": "0" * 64,
        "adopted_at": 1.0, "config": {"buttons": {
            "key1": {"press": "mute"}, "rec": {"press": "ptt", "release": "stop"}}}}]}))
    with TestClient(app.app) as c:
        buttons = c.get(f"/satellites/{NID}").json()["config"]["buttons"]
    assert buttons["rec"] == {"press": "mute", "release": "stop"}, buttons
    assert buttons["key1"] == {"press": "mute"}, "a key1 wired on the board still mutes"


def test_how_the_ring_is_mounted_is_its_top_led_and_which_way_it_runs(client):
    """Where a bar on the ring starts (12 o'clock) and whether it runs the
    other way round, as the satellite is mounted."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        config = config_of(client)
        assert (config["ring_top"], config["ring_upside_down"]) == (0, False)
        assert client.patch(f"/satellites/{NID}", json={"ring_top": 6, "ring_upside_down": True}).status_code == 200
        assert ws.receive_json() == {"type": "config", "ring_top": 6, "ring_upside_down": True}
        assert [client.patch(f"/satellites/{NID}", json={"ring_top": b}).status_code
                for b in (-1, 12, 3.5)] == [422, 422, 422]


def test_a_ring_bottom_from_its_hour_as_a_setting_becomes_the_top_opposite(app, tmp_path):
    (tmp_path / "satellites.json").write_text(json.dumps({"satellites": [{
        "id": NID, "name": "bedroom", "model": MODEL, "token_sha256": "0" * 64,
        "adopted_at": 1.0, "config": {"ring_bottom": 2}}]}))
    with TestClient(app.app) as c:
        config = c.get(f"/satellites/{NID}").json()["config"]
    assert config["ring_top"] == 8 and "ring_bottom" not in config


# ---- a Linux satellite (clients/pi-satellite) ----------------------------------

PI_MAC = "b827eb123456"


def pi_hello(token: str = "", mic: bool = False) -> dict:
    """What calliope_pi.agent says: a speaker, maybe a microphone with the
    output as its reference, its PipeWire devices and which ones it uses."""
    caps = {"speaker": {"rate": 48000, "channels": 1, "format": "s16le"}, "duck": True,
            "audio_devices": True, "bundle": "tar.gz"}
    if mic:
        caps["mic"] = {"rate": 16000, "channels": 2, "format": "s16le", "reference": True}
    return {"type": "hello", "id": PI_MAC, "model": "raspberry-pi", "fw": "v0.1.2-200-gabc", "token": token,
            "caps": caps, "volume": 55, "mic_gain_db": 0.0, "mic_enabled": True, "speaker_enabled": True,
            "audio_sink": "alsa_output.usb-Generic.analog-stereo", "audio_source": None,
            "echo_reference": True,
            "audio": {"sinks": [{"name": "alsa_output.usb-Generic.analog-stereo",
                                 "description": "USB Audio", "api": "alsa"}],
                      "sources": [], "default_sink": "alsa_output.usb-Generic.analog-stereo",
                      "default_source": None}}


def test_a_satellite_with_no_microphone_is_adopted_as_a_speaker_and_not_listened_to(client, app):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(pi_hello())
        assert ws.receive_json() == {"type": "pending"}
        assert client.post(f"/satellites/{PI_MAC}/adopt", json={"name": "Lounge"}).status_code == 200
        token = ws.receive_json()["token"]
        ws.send_json(pi_hello(token))
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome"
        # Its own choices, from the hello, are what the hub keeps and sends back.
        assert welcome["config"]["audio_sink"] == "alsa_output.usb-Generic.analog-stereo"
        assert welcome["config"]["volume"] == 55 and welcome["config"]["mic_gain_db"] == 0.0
        s = app.hub.sessions[PI_MAC]
        assert s.listener is None and s.listen_error == "it has no microphone"
        got = client.get(f"/satellites/{PI_MAC}").json()
        assert got["model"] == "raspberry-pi" and got["adopted"] and got["online"]


def test_its_output_and_microphone_are_chosen_from_the_page(client):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(pi_hello())
        ws.receive_json()
        client.post(f"/satellites/{PI_MAC}/adopt", json={"name": "Lounge"})
        ws.send_json(pi_hello(ws.receive_json()["token"]))
        ws.receive_json()
        r = client.patch(f"/satellites/{PI_MAC}", json={"audio_sink": "alsa_output.platform-bcm2835.stereo",
                                                        "echo_reference": False})
        assert r.status_code == 200, r.json()
        msg = ws.receive_json()
        while msg["type"] != "config":
            msg = ws.receive_json()
        assert msg == {"type": "config", "audio_sink": "alsa_output.platform-bcm2835.stereo",
                       "echo_reference": False}
        assert client.patch(f"/satellites/{PI_MAC}",
                            json={"audio_sink": "not a node; rm -rf"}).status_code == 422


def test_a_korvo_has_no_audio_devices_to_choose(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.patch(f"/satellites/{NID}", json={"audio_sink": "alsa_output.x"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "no_audio_devices"


def test_a_release_bundle_is_stored_beside_esp32_images(client):
    import gzip
    bundle = gzip.compress(b"manifest")
    r = client.post("/satellites/firmware", params={"model": "raspberry-pi", "version": "v1"}, content=bundle)
    assert r.status_code == 200, r.json()
    assert r.json()["model"] == "raspberry-pi" and r.json()["size"] == len(bundle)


def adopt_pi(client, ws, name="Lounge") -> None:
    ws.send_json(pi_hello())
    assert ws.receive_json() == {"type": "pending"}
    client.post(f"/satellites/{PI_MAC}/adopt", json={"name": name})
    ws.send_json(pi_hello(ws.receive_json()["token"]))
    assert ws.receive_json()["type"] == "welcome"


def next_binary(ws) -> bytes:
    while True:
        msg = ws.receive()
        if msg.get("bytes"):
            return msg["bytes"]


def test_a_satellite_can_play_through_another_and_falls_back_to_its_own_when_that_one_is_gone(client, app):
    """The Korvo hears; the Pi beside the amplifier speaks. A tone asked of
    the Korvo comes out of the Pi, and nothing reaches the Korvo's speaker.
    With the Pi gone, the Korvo plays it itself."""
    with client.websocket_connect("/satellites/ws") as korvo:
        adopt(client, korvo)
        with client.websocket_connect("/satellites/ws") as pi:
            adopt_pi(client, pi)
            r = client.patch(f"/satellites/{NID}", json={"output_satellite": PI_MAC})
            assert r.status_code == 200 and r.json()["config"]["output_satellite"] == PI_MAC
            assert app.hub.output_of(NID) == PI_MAC and app.hub.output_of(PI_MAC) == PI_MAC
            assert client.post(f"/satellites/{NID}/tone", json={"seconds": 0.1}).status_code == 204
            frame = next_binary(pi)
            assert frame[0] == 2, "the Pi was sent the tone"
        assert app.hub.output_of(NID) == NID, "the Pi is gone, so the Korvo plays for itself"
        assert client.post(f"/satellites/{NID}/tone", json={"seconds": 0.1}).status_code == 204
        assert next_binary(korvo)[0] == 2


def test_an_output_is_another_adopted_satellite_never_itself_and_never_a_loop(client, app):
    with client.websocket_connect("/satellites/ws") as korvo:
        adopt(client, korvo)
        with client.websocket_connect("/satellites/ws") as pi:
            adopt_pi(client, pi)
            bad = {"itself": {"output_satellite": NID}, "unknown": {"output_satellite": "0000000000ff"},
                   "not an id": {"output_satellite": "kitchen"}}
            for why, body in bad.items():
                assert client.patch(f"/satellites/{NID}", json=body).status_code == 422, why
            assert client.patch(f"/satellites/{NID}", json={"output_satellite": PI_MAC}).status_code == 200
            loop = client.patch(f"/satellites/{PI_MAC}", json={"output_satellite": NID})
            assert loop.status_code == 422 and loop.json()["error"]["code"] == "output_loop"
            back = client.patch(f"/satellites/{NID}", json={"output_satellite": ""})
            assert back.status_code == 200 and back.json()["config"]["output_satellite"] is None
            assert app.hub.output_of(NID) == NID
    # The hub's own routing: never sent to the satellite.
    rec = app.hub.store.satellites[NID]
    from app.store import satellite_config
    assert "output_satellite" not in satellite_config(rec.config | {"output_satellite": PI_MAC})


def test_a_word_that_answers_on_the_same_satellite_answers_where_its_output_is():
    from app import router as routing
    r = routing.Router(rules=None, output_of=lambda nid: {"korvo": "pi"}.get(nid, nid))
    b = routing.Behaviour.model_validate({"mode": "command", "action": {
        "destination": {"type": "echo"}, "reply_to": "same"}})
    assert r.target(b, "korvo") == "pi" and r.target(b, "pi") == "pi"
    b2 = routing.Behaviour.model_validate({"mode": "command", "action": {
        "destination": {"type": "echo"}, "reply_to": "none"}})
    assert r.target(b2, "korvo") is None


def test_a_pi_that_is_an_airplay_receiver_is_turned_on_off_and_named_from_the_page(client):
    with client.websocket_connect("/satellites/ws") as ws:
        hello = pi_hello() | {"airplay_enabled": True, "airplay_name": None}
        hello["caps"] = hello["caps"] | {"airplay": {"version": 1}}
        ws.send_json(hello)
        ws.receive_json()
        client.post(f"/satellites/{PI_MAC}/adopt", json={"name": "Lounge"})
        ws.send_json(hello | {"token": ws.receive_json()["token"]})
        assert ws.receive_json()["config"]["airplay_enabled"] is True
        r = client.patch(f"/satellites/{PI_MAC}", json={"airplay_name": "Living room"})
        assert r.status_code == 200
        msg = ws.receive_json()
        while msg["type"] != "config":
            msg = ws.receive_json()
        assert msg == {"type": "config", "airplay_name": "Living room"}
        assert client.patch(f"/satellites/{PI_MAC}", json={"airplay_name": "bad\nname"}).status_code == 422
        client.patch(f"/satellites/{PI_MAC}", json={"airplay_name": ""})
        assert client.get(f"/satellites/{PI_MAC}").json()["config"]["airplay_name"] is None
    with client.websocket_connect("/satellites/ws") as korvo:
        adopt(client, korvo)
        r = client.patch(f"/satellites/{NID}", json={"airplay_enabled": True})
        assert r.status_code == 409 and r.json()["error"]["code"] == "no_airplay"
