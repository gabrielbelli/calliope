"""AirPlay through the hub: the phone's transport controls, and the cover.

A command is answered only when the satellite has answered it, so each one is
sent from a thread while the test plays the Pi agent on the socket. Nothing
here reaches a phone.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import re
import threading

import pytest
from fastapi.testclient import TestClient

from test_media import PI, PI_CAPS, adopt_pi, pi_hello, text_until
from test_satellites import NID, adopt, until

EVERYTHING = ["play", "pause", "play_pause", "next", "previous", "stop", "disconnect"]
JPEG = b"\xff\xd8\xff\xe0" + b"cover" * 100
SHA = hashlib.sha256(JPEG).hexdigest()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SATELLITES_API_KEYS", raising=False)
    return importlib.reload(importlib.import_module("app.main"))


@pytest.fixture
def client(app):
    with TestClient(app.app) as c:
        yield c


def phone(ws, client, controls=EVERYTHING, session: bool = True, artwork: dict | None = None,
          rssi: int = -50) -> None:
    """A status saying what the phone playing to the Pi takes now, and a
    round trip so the hub has read it."""
    ws.send_json({"type": "status", "rssi": rssi, "airplay": {
        "session": session, "playing": session, "artwork": artwork,
        "remote": {"available": bool(session and len(controls) > 1),
                   "controls": controls if session else [], "last": None}}})
    until(lambda: client.get(f"/satellites/{PI}").json()["status"].get("rssi") == rssi, "the status")


class Command(threading.Thread):
    def __init__(self, client, command: str, nid: str = PI):
        super().__init__(daemon=True)
        self.client, self.command, self.nid, self.answer = client, command, nid, None
        self.start()

    def run(self) -> None:
        self.answer = self.client.post(f"/satellites/{self.nid}/airplay/{self.command}")

    def result(self):
        self.join(timeout=10)
        assert not self.is_alive(), "the command was never answered"
        return self.answer


def test_an_airplay_command_reaches_the_satellite_and_its_result_is_the_answer(client, app):
    published = []
    publish = app.hub.publish
    app.hub.publish = lambda e: (published.append(e), publish(e))
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        phone(ws, client)
        sent = Command(client, "next")
        msg = text_until(ws, "airplay_command")
        assert msg["command"] == "next" and re.fullmatch(r"[0-9a-f]{32}", msg["id"])
        ws.send_json({"type": "airplay_result", "id": msg["id"], "command": "next", "ok": True,
                      "status": 204, "confirmed": True, "error": None})
        r = sent.result()
    assert r.status_code == 200 and r.json() == {"command": "next", "status": 204, "confirmed": True}
    assert [e for e in published if e["type"] == "airplay_command"] == [
        {"type": "airplay_command", "satellite": PI, "command": "next", "ok": True, "status": 204,
         "confirmed": True}]


def test_the_phone_refusing_is_a_502_with_shairports_reason(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        phone(ws, client)
        sent = Command(client, "pause")
        msg = text_until(ws, "airplay_command")
        ws.send_json({"type": "airplay_result", "id": msg["id"], "command": "pause", "ok": False,
                      "status": 491, "confirmed": False, "error": "the phone refused the connection"})
        r = sent.result()
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "airplay_refused"
    assert r.json()["error"]["message"] == "the phone did not take pause (491: the phone refused the connection)"


def test_no_answer_is_a_504(client, app, monkeypatch):
    monkeypatch.setattr(app, "AIRPLAY_WAIT_S", 0.1)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        phone(ws, client)
        r = client.post(f"/satellites/{PI}/airplay/play")
    assert r.status_code == 504 and r.json()["error"]["code"] == "satellite_timeout"


def test_a_command_needs_a_session_and_remote_control_but_disconnect_needs_only_a_session(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        phone(ws, client, session=False)
        r = client.post(f"/satellites/{PI}/airplay/play")
        assert r.status_code == 409 and r.json()["error"]["code"] == "airplay_idle"
        phone(ws, client, controls=["disconnect"], rssi=-51)
        r = client.post(f"/satellites/{PI}/airplay/next")
        assert r.status_code == 409 and r.json()["error"]["code"] == "airplay_no_remote"
        sent = Command(client, "disconnect")
        msg = text_until(ws, "airplay_command")
        assert msg["command"] == "disconnect"
        ws.send_json({"type": "airplay_result", "id": msg["id"], "command": "disconnect", "ok": True,
                      "status": None, "confirmed": True, "error": None})
        assert sent.result().status_code == 200
        assert client.post(f"/satellites/{PI}/airplay/shuffle").status_code == 422


def test_an_agent_without_controls_is_told_so_at_once(client):
    """An agent from before commands ignores one without a word: the hub
    would wait out its timeout for an answer that never comes."""
    old = PI_CAPS | {"airplay": {"version": 1}}
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(pi_hello() | {"caps": old})
        assert ws.receive_json() == {"type": "pending"}
        client.post(f"/satellites/{PI}/adopt", json={"name": "Lounge"})
        ws.send_json(pi_hello(ws.receive_json()["token"]) | {"caps": old})
        assert ws.receive_json()["type"] == "welcome"
        phone(ws, client)
        r = client.post(f"/satellites/{PI}/airplay/next")
        assert r.status_code == 409 and r.json()["error"]["code"] == "airplay_no_controls"
        assert client.post(f"/satellites/{PI}/identify").status_code == 204
        assert ws.receive_json()["type"] == "identify", "nothing was sent before it"


def test_a_korvo_has_no_airplay(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post(f"/satellites/{NID}/airplay/play")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_airplay"


def test_a_disconnect_before_the_answer_is_offline_not_a_hang(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        phone(ws, client)
        sent = Command(client, "next")
        text_until(ws, "airplay_command")
    r = sent.result()
    assert r.status_code == 409 and r.json()["error"]["code"] == "satellite_offline"


def send_cover(ws) -> None:
    ws.send_json({"type": "artwork", "sha256": SHA, "format": "jpeg",
                  "data": base64.b64encode(JPEG).decode()})


def test_the_cover_goes_when_the_status_stops_naming_it(client):
    """It stayed after the music stopped, so the page and Home Assistant
    showed the cover of what had finished."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        send_cover(ws)
        phone(ws, client, artwork={"sha256": SHA, "type": "jpeg"})
        assert client.get(f"/satellites/{PI}/airplay/artwork").status_code == 200
        phone(ws, client, session=False, artwork=None, rssi=-51)
        assert client.get(f"/satellites/{PI}/airplay/artwork").status_code == 404
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        send_cover(ws)
        ws.send_json({"type": "status", "rssi": -60})
        until(lambda: client.get(f"/satellites/{NID}").json()["status"].get("rssi") == -60, "the status")
        assert client.get(f"/satellites/{NID}/airplay/artwork").status_code == 200, \
            "a status without airplay drops nothing"


def test_a_cover_asked_for_by_another_sha_is_not_served(client):
    """A picture kept under the SHA it was asked for must be that picture."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        send_cover(ws)
        phone(ws, client, artwork={"sha256": SHA, "type": "jpeg"})
        other = client.get(f"/satellites/{PI}/airplay/artwork", params={"v": "0" * 64})
        same = client.get(f"/satellites/{PI}/airplay/artwork", params={"v": SHA})
        bad = client.get(f"/satellites/{PI}/airplay/artwork", params={"v": "not-a-sha"})
    assert other.status_code == 404 and other.json()["error"]["code"] == "no_artwork"
    assert same.status_code == 200 and same.content == JPEG and same.headers["etag"] == f'"{SHA}"'
    assert bad.status_code == 422
