"""Failing tests from an adversarial review of the lights, speaker and
adoption promises (2026-09-25). Each one shows a way round a promise the
README makes; none of them is fixed yet, so every test here fails until it is.

The fakes are test_pipeline.py's: the satellite is Starlette's test socket,
STT and TTS are *.test handlers, and the wake word is a marker sample.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from test_pipeline import (MAC, NID, adopt, hello, routed, utterance, wait)
from test_pipeline import app, client, env, events, fake_models, plug, services  # noqa: F401


# ---- the speaker ---------------------------------------------------------------


def test_forgetting_a_satellite_mid_reply_stops_its_audio_rather_than_streaming_it_to_a_pending_satellite(
        client, plug):
    """forget() drops the adoption and stops the listener, but not the
    speaker queue: the reply or tone in hand streamed on, frame after frame,
    to a connection that is now pending. The hub promises a pending
    connection nothing but "pending", and does not rely on the firmware
    ignoring the frames."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        client.post(f"/satellites/{NID}/tone", json={"frequency": 440, "seconds": 3})
        wait(lambda: len(satellite.speaker()) >= 10, what="the tone to start")
        assert client.post(f"/satellites/{NID}/forget").status_code == 204
        at_forget = len(satellite.speaker())
        time.sleep(0.6)
        after = len(satellite.speaker()) - at_forget
    assert after <= 2  # one frame may be in flight; 3 s of tone is 150 frames


def test_a_speaker_turned_off_while_the_reply_is_being_handed_over_is_not_played_to(
        client, app, events, plug, monkeypatch):
    """Conversation._reply reads speaker_enabled, then awaits two sends (the
    flush and the unduck), then queues the reply. A PATCH that lands in
    either await flushes a queue that is still empty, and the reply queued
    after it streams in full to a satellite whose speaker is off. The speaker
    loop does not look at the setting, so nothing stops it.

    The PATCH is run from inside the unduck here so that the interleaving is
    certain; on a real socket it needs only the unduck's send to wait on the
    lock or the transport."""
    real_release = app.hub.release_duck
    patched = {"done": False}

    async def release_then_patch(s):
        await real_release(s)
        if not patched["done"]:
            patched["done"] = True
            await app.configure(NID, app.ConfigBody(speaker_enabled=False))
    monkeypatch.setattr(app.hub, "release_duck", release_then_patch)

    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        routed(events)
        time.sleep(0.5)  # the reply is 0.4 s at real time plus 300 ms lead
    assert patched["done"], "the reply path never reached the unduck"
    assert client.get(f"/satellites/{NID}").json()["config"]["speaker_enabled"] is False
    assert satellite.speaker() == []


# ---- adoption --------------------------------------------------------------------


def test_adopting_a_satellite_before_its_first_status_does_not_switch_its_lights_or_speaker_on(
        client):
    """The firmware reports its settings in "status", every 10 s, and not in
    "hello". A satellite adopted in the seconds after it connects has
    reported nothing, so store.adopt takes the defaults (lights and speaker
    on) and the welcome turns on the ring and speaker of a satellite that
    was dark and silent: the case test_adopting_a_dark_satellite_keeps_it_dark
    exists for, with the status not yet in. Either adoption waits for the
    satellite's own settings, or neither the welcome nor the hub's record
    claims settings the hub never had: a record that says "on" until the
    first status lets a conversation in those seconds send "lights" to a
    ring that is dark only because the firmware says so."""
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        r = client.post(f"/satellites/{NID}/adopt", json={"name": "bedroom"})
        if r.status_code != 200:
            assert r.status_code == 409  # refusing until the status is in is a fix too
            return
        token = ws.receive_json()["token"]
        ws.send_json(hello(token))
        welcome = ws.receive_json()
        record = client.get(f"/satellites/{NID}").json()["config"]
    assert welcome["type"] == "welcome"
    assert welcome["config"].get("lights_enabled") is not True
    assert welcome["config"].get("speaker_enabled") is not True
    assert record["lights_enabled"] is not True and record["speaker_enabled"] is not True


def test_a_pending_satellite_cannot_plant_a_button_mapping_that_adoption_then_keeps(client):
    """store.adopt copies every reported key that is in the config, and
    "buttons" is in the config. A pending connection, which anyone can open,
    reports a mapping in "status"; once someone adopts it the hub keeps that
    mapping unvalidated -- a webhook with credentials in its URL, which PATCH
    refuses -- and posts to it on the next press. Buttons are the hub's own
    (store.HUB_ONLY) and never the satellite's to report."""
    planted = {"play": {"press": "webhook:http://user:secret@hooks.test/steal"}}
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        ws.send_json({"type": "status", "lights_enabled": True, "buttons": planted})
        client.get("/satellites")  # let the status land before adopting
        assert client.post(f"/satellites/{NID}/adopt", json={"name": "hall"}).status_code == 200
    buttons = client.get(f"/satellites/{NID}").json()["config"]["buttons"]
    assert buttons != planted
    assert "webhook:" not in str(buttons)


# ---- an unadopted connection ---------------------------------------------------------


def test_a_hello_without_a_token_does_not_throw_the_adopted_satellite_with_that_id_off(
        client, app):
    """The socket answers anyone, and a satellite's id is its MAC, which is
    no secret. A second hello with that id and no token closed the adopted
    satellite's socket (1012) and took its place in hub.sessions, so any
    connection could keep a satellite offline and have its own status shown,
    and sent to Home Assistant, as that satellite's. Only a connection that
    proves the adoption may replace an adopted one."""
    with client.websocket_connect("/satellites/ws") as real:
        adopt(client, real)
        with client.websocket_connect("/satellites/ws") as impostor:
            impostor.send_json(hello(mac=MAC))
            try:
                impostor.receive_json()
            except WebSocketDisconnect:
                pass  # refusing the impostor outright is a fix too
            assert app.hub.sessions[NID].adopted is True
            assert client.post(f"/satellites/{NID}/reboot").status_code == 204
            assert real.receive_json() == {"type": "reboot"}


# ---- the moment of sending ---------------------------------------------------------


class _RecordingSocket:
    headers: dict = {}
    client = None

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(repr(data[:1]))


async def test_lights_waiting_behind_another_send_are_not_sent_once_lights_are_turned_off(
        app, tmp_path):
    """send_lights reads lights_enabled and only then waits for the socket's
    lock, which the speaker loop holds for every 20 ms frame of a reply. A
    PATCH that turns the lights off meanwhile does not stop the "lights"
    already waiting: it goes out after the hub's own config says false (and
    before the satellite's "config" does). The same gap is in Hub.earcon for
    the speaker. The check belongs inside the lock."""
    hub = app.Hub(app.Store(tmp_path))
    ws = _RecordingSocket()
    s = app.Session(ws, hello())
    hub.store.adopt(s.id, "bedroom", s.model)
    s.adopted = True
    hub.sessions[s.id] = s

    await s.lock.acquire()  # a speaker frame is being sent
    pending = asyncio.create_task(hub.send_lights(s, {"mode": "solid"}))
    await asyncio.sleep(0)
    hub.store.satellites[s.id].config["lights_enabled"] = False  # what PATCH does first
    s.lock.release()
    await pending
    assert not any('"lights"' in m for m in ws.sent)
