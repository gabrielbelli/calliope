"""voice-nodes against a fake device on Starlette's test socket.

Nothing starts a server: TestClient drives the ASGI app and its websocket_connect
plays the node. Each test gets its own data directory, because the store is the
thing half of these are about.
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
    return {"type": "hello", "id": MAC, "model": model, "fw": "v0", "token": token,
            "caps": {"mic": {"rate": 16000, "channels": 4},
                     "speaker": {"rate": 48000, "channels": 1}}}


def mic_frame(seq: int, frames: int = 320, value: int = 0) -> bytes:
    pcm = struct.pack("<h", value) * (frames * 4)
    return struct.pack("<BBBBIQ", 1, 0, 4, 0, seq, 0) + pcm


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NODES_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NODES_API_KEYS", raising=False)
    monkeypatch.delenv("NODES_TTS_URL", raising=False)
    return importlib.reload(importlib.import_module("app.main"))


@pytest.fixture
def client(app):
    with TestClient(app.app) as c:
        yield c


def adopt(client, ws, name="kitchen") -> str:
    ws.send_json(hello())
    assert ws.receive_json() == {"type": "pending"}
    assert client.post(f"/nodes/{NID}/adopt", json={"name": name}).status_code == 200
    msg = ws.receive_json()
    assert msg["type"] == "adopt" and msg["name"] == name
    ws.send_json(hello(msg["token"]))
    welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    return msg["token"]


def test_a_new_node_waits_and_its_microphone_is_not_listened_to(client):
    with client.websocket_connect("/nodes/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        ws.send_bytes(mic_frame(0))
        listed = client.get("/nodes").json()["nodes"]
        assert [(n["id"], n["adopted"], n["online"]) for n in listed] == [(NID, False, True)]
        r = client.get(f"/nodes/{NID}/listen", params={"seconds": 1})
        assert r.status_code == 409
        assert_four_field_envelope(r)


def test_adoption_issues_a_token_the_hub_keeps_only_as_a_hash(client, app, tmp_path):
    with client.websocket_connect("/nodes/ws") as ws:
        token = adopt(client, ws)
    stored = (tmp_path / "nodes.json").read_text()
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_a_wrong_token_is_pending_not_welcome(client):
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws)
    with client.websocket_connect("/nodes/ws") as ws:
        ws.send_json(hello("not-the-token"))
        assert ws.receive_json() == {"type": "pending"}


def test_adoption_survives_a_restart_of_the_hub(app, tmp_path):
    with TestClient(app.app) as c, c.websocket_connect("/nodes/ws") as ws:
        token = adopt(c, ws)
    fresh = importlib.reload(app)
    with TestClient(fresh.app) as c, c.websocket_connect("/nodes/ws") as ws:
        ws.send_json(hello(token))
        assert ws.receive_json()["type"] == "welcome"


def test_listen_returns_the_channels_the_node_sent(client):
    with client.websocket_connect("/nodes/ws") as ws:
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
        r = client.get(f"/nodes/{NID}/listen", params={"seconds": 1, "channel": 1})
        t.join()
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert len(r.content) == 44 + 16000 * 2


def test_a_node_is_found_by_name_as_well_as_by_id(client):
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws, name="kitchen")
        assert client.get("/nodes/kitchen").json()["id"] == NID
        assert client.get(f"/nodes/{MAC}").json()["id"] == NID
        r = client.get("/nodes/nowhere")
        assert r.status_code == 404
        assert_four_field_envelope(r)


def test_config_changes_reach_the_node_and_are_bounded(client):
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws)
        assert client.patch("/nodes/kitchen", json={"volume": 30}).status_code == 200
        assert ws.receive_json() == {"type": "config", "volume": 30}
        # FastAPI's own 422 and detail list, as on every native route in the
        # estate; only /v1 is held to OpenAI's 400 envelope.
        r = client.patch("/nodes/kitchen", json={"volume": 101})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "volume"]


def test_firmware_that_is_not_an_esp32_image_is_refused(client):
    r = client.post("/nodes/firmware", params={"model": MODEL}, content=b"hello")
    assert r.status_code == 400
    assert_four_field_envelope(r)


def test_an_update_is_sent_in_frames_the_node_can_take(client, app):
    # THE DEFECT: 16 KB chunks. The Arduino WebSockets library drops the whole
    # connection on any frame over 15 KB, and the first real update died 17 ms
    # in. The header is 8 bytes on top of the chunk.
    assert app.OTA_CHUNK + 8 <= 15 * 1024
    image = bytes([0xE9]) + bytes(range(256)) * 200
    fw = client.post("/nodes/firmware", params={"model": MODEL, "version": "v1"},
                     content=image).json()
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws)
        r = client.post("/nodes/ota", json={"node": "kitchen", "sha256": fw["sha256"]})
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
    fw = client.post("/nodes/firmware", params={"model": "other-board"}, content=image).json()
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws)
        r = client.post("/nodes/ota", json={"node": "all", "sha256": fw["sha256"]}).json()
    assert r["started"] == [] and "other-board" in r["skipped"][NID]


def test_say_without_a_voice_configured_says_why(client):
    with client.websocket_connect("/nodes/ws") as ws:
        adopt(client, ws)
        r = client.post("/nodes/kitchen/say", json={"text": "hello"})
    assert r.status_code == 503 and "NODES_TTS_URL" in r.json()["error"]["message"]


def test_the_socket_refuses_anything_but_a_hello_first(client):
    with client.websocket_connect("/nodes/ws") as ws:
        ws.send_text(json.dumps({"type": "status"}))
        with pytest.raises(Exception):
            ws.receive_json()
