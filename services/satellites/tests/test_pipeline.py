"""The listening path wired into the hub: socket, Ear, Conversation, router,
and back to a satellite's speaker, lights and earcons.

Everything a satellite or a service would be is a fake. The satellite is
Starlette's test socket, with a thread that records every frame the hub sends
it. STT, TTS and webhooks are httpx.MockTransport handlers on *.test hosts,
which never resolve. The wake word model is FakeWakeWords, which fires on a
marker sample rather than on speech, so these tests need no model files; the
two at the end that do use the real openWakeWord models are skipped offline.

SATELLITES_FRONTEND is 0 unless a test says otherwise, so the mono stream is
the first microphone as sent and a marker survives to the detector.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import struct
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import listening, signing, wakeword, wakewords_config
from app import router as routing
from app.wakeword import Detection

MAC = "94:B9:7E:7B:8B:E8"
NID = "94b97e7b8be8"
MAC2 = "94:B9:7E:00:00:02"
NID2 = "94b97e000002"
MODEL = "esp32-korvo-v1.1"
RATE = 16000
FRAME = 320  # the satellite's 20 ms
MARK = 30000  # the fake wake word: a sample no test speech reaches
FIXTURES = Path(__file__).parent / "fixtures"
TTS_SAMPLES = 4800  # 0.2 s of Kokoro at 24 kHz


def caps(**extra) -> dict:
    base = {"mic": {"rate": RATE, "channels": 4}, "speaker": {"rate": 48000, "channels": 1},
            "lights": 12, "buttons": ["vol_up", "vol_down", "set", "play", "mode", "rec"],
            "duck": True}
    return base | extra


def hello(token: str = "", mac: str = MAC, **caps_extra) -> dict:
    """A hello as firmware from before 2026-09-25 sends it, with no settings:
    those arrive only in its status."""
    return {"type": "hello", "id": mac, "model": MODEL, "fw": "v1", "token": token,
            "caps": caps(**caps_extra)}


# What current firmware says about itself in every hello and every status. The
# hub takes a setting it has not been told from here, never from its defaults.
SETTINGS = {"volume": 60, "mic_gain_db": 30.0, "mic_enabled": True, "speaker_enabled": True,
            "lights_enabled": True}


# ---- audio -------------------------------------------------------------------


def floor(seconds: float, dbfs: float = -60, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * RATE)) * 32768 * 10 ** (dbfs / 20)).astype(np.int16)


def voiced(seconds: float, level: int = 6000) -> np.ndarray:
    """Speech-like enough for webrtcvad to call every frame voiced."""
    t = np.arange(int(seconds * RATE)) / RATE
    phase = 2 * np.pi * np.cumsum(130 + 15 * np.sin(2 * np.pi * 0.8 * t)) / RATE
    y = sum(np.sin(k * phase) / k for k in range(1, 25)) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return (y / np.abs(y).max() * level).astype(np.int16)


def wake_mark() -> np.ndarray:
    return np.full(FRAME, MARK, dtype=np.int16)


def utterance(speech_s: float = 1.0, after_s: float = 1.2) -> np.ndarray:
    """A wake word, a command, then the room: what one conversation hears."""
    return np.concatenate((floor(0.2), wake_mark(), voiced(speech_s), floor(after_s, seed=1)))


def mic_frames(mono: np.ndarray, seq0: int = 0):
    """The satellite's frames, the same audio on all three microphones and a
    quiet loopback, which is all a front-end that is switched off looks at."""
    for i, off in enumerate(range(0, len(mono) - FRAME + 1, FRAME)):
        x = mono[off:off + FRAME]
        pcm = np.stack((np.zeros(FRAME, np.int16), x, x, x), axis=1).astype("<i2").tobytes()
        yield struct.pack("<BBBBIQ", 1, 0, 4, 0, seq0 + i, 0) + pcm


def wav(mono: np.ndarray, rate: int = RATE, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(mono.astype("<i2").tobytes())
    return buf.getvalue()


# ---- fakes -------------------------------------------------------------------


class FakeWakeWords:
    """WakeWords' interface, firing on the marker instead of on a voice."""

    def __init__(self, models, model_dir=None, *, refractory_s=1.5):
        self.thresholds = dict(models)
        self.names = list(models)
        self.position = 0

    def clone(self, names=None):
        twin = FakeWakeWords(self.thresholds)
        # As the real clone: only the names given, and the thresholds shared.
        twin.names = list(self.names if names is None else names)
        twin.thresholds = self.thresholds
        return twin

    def reset(self):
        self.position = 0

    def feed(self, pcm):
        assert pcm.dtype == np.int16 and pcm.ndim == 1
        self.position += len(pcm)
        hits = np.flatnonzero(pcm >= MARK)
        if not len(hits) or "hey_jarvis" not in self.names:
            return []
        return [Detection("hey_jarvis", 0.9, self.position - len(pcm) + int(hits[-1]) + 1)]


class Services:
    """STT, TTS and anything else on a *.test host."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.transcript = "what time is it"
        self.stt_gate: threading.Event | None = None
        self.handlers = {"stt.test": self.stt, "tts.test": self.tts}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        handler = self.handlers.get(request.url.host)
        if handler is None:
            return httpx.Response(404)
        out = handler(request)
        return await out if asyncio.iscoroutine(out) else out

    async def stt(self, request):
        while self.stt_gate is not None and not self.stt_gate.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, json={"text": self.transcript})

    def tts(self, request):
        return httpx.Response(200, content=np.full(TTS_SAMPLES, 1000, "<i2").tobytes())

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.seen]


class Satellite:
    """A device on the test socket. A thread records everything the hub sends
    it, and can answer, so a test never blocks on a receive that will not
    come."""

    def __init__(self, ws, answer=None, backlog=None):
        self.ws = ws
        self.got: list = []
        self.answer = answer
        self.backlog = backlog
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while True:
                m = self.ws.receive()
                if m["type"] == "websocket.close":
                    return
                item = json.loads(m["text"]) if m.get("text") is not None else m.get("bytes")
                self.got.append(item)
                if self.answer is not None:
                    for reply in self.answer(item) or ():
                        if isinstance(reply, bytes):
                            self.ws.send_bytes(reply)
                        else:
                            self.ws.send_json(reply)
        except Exception:
            return

    def texts(self, kind: str | None = None) -> list[dict]:
        return [m for m in list(self.got) if isinstance(m, dict)
                and (kind is None or m.get("type") == kind)]

    def speaker(self) -> list[bytes]:
        return [m for m in list(self.got) if isinstance(m, bytes) and m[:1] == b"\x02"]

    def send(self, mono: np.ndarray) -> None:
        self.send_frames(mic_frames(mono))

    def send_frames(self, frames) -> None:
        """As fast as the hub keeps up, and no faster. A real satellite
        sends in real time; a test that outran the listener would overflow
        its queue and test the dropping instead (which has a test of its
        own)."""
        for f in frames:
            while self.backlog is not None and self.backlog() > 20:
                time.sleep(0.002)
            self.ws.send_bytes(f)


def wait(predicate, timeout: float = 10.0, what: str = "the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# ---- fixtures --------------------------------------------------------------------


@pytest.fixture
def services() -> Services:
    return Services()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_jarvis:0.5")
    monkeypatch.setenv("SATELLITES_FRONTEND", "0")
    monkeypatch.delenv("SATELLITES_API_KEYS", raising=False)
    monkeypatch.delenv("SATELLITES_TTS_URL", raising=False)


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setattr(wakeword, "ensure_models", lambda names, model_dir, **kw: None)
    monkeypatch.setattr(wakeword, "WakeWords", FakeWakeWords)


@pytest.fixture
def app(env, fake_models):
    return importlib.reload(importlib.import_module("app.main"))


def route_to_fakes(app, services, tmp_path) -> None:
    """The hub's own routing (each wake word's entry), with STT, TTS and
    every destination on the fakes."""
    routing.configure(routing.Router(
        wakewords_config.WordActions(app.hub.voice.assignment), stt_url="http://stt.test",
        tts_url="http://tts.test", client=httpx.AsyncClient(transport=httpx.MockTransport(services)),
        lookup=app.lookup_satellite))


@pytest.fixture
def client(app, services, tmp_path):
    with TestClient(app.app) as c:
        route_to_fakes(app, services, tmp_path)
        wait(lambda: app.hub.voice.state == "ready", what="the wake word models")
        yield c


@pytest.fixture
def events(client, app) -> list[dict]:
    got: list[dict] = []
    publish = app.hub.publish

    def record(event: dict) -> None:
        got.append(event)
        publish(event)
    app.hub.publish = record
    return got


@pytest.fixture
def plug(app):
    """A Satellite whose sending waits on this hub's listener."""
    def make(ws, nid: str = NID, answer=None) -> Satellite:
        def backlog() -> int:
            s = app.hub.sessions.get(nid)
            return s.mic.qsize() if s is not None else 0
        return Satellite(ws, answer=answer, backlog=backlog)
    return make


def adopt(client, ws, name="kitchen", mac=MAC, **caps_extra) -> str:
    """A satellite on current firmware, lights, speaker and microphone on,
    adopted."""
    nid = mac.replace(":", "").lower()
    ws.send_json(hello(mac=mac, **caps_extra) | SETTINGS)
    assert ws.receive_json() == {"type": "pending"}
    assert client.post(f"/satellites/{nid}/adopt", json={"name": name}).status_code == 200
    msg = ws.receive_json()
    ws.send_json(hello(msg["token"], mac=mac, **caps_extra) | SETTINGS)
    assert ws.receive_json()["type"] == "welcome"
    return msg["token"]


def of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in list(events) if e.get("type") == kind]


def routed(events, n: int = 1) -> list[dict]:
    return wait(lambda: len(of(events, "routed")) >= n and of(events, "routed"),
                what="the routed event")


# ---- the promise about lights ----------------------------------------------------------


def test_a_dark_satellite_is_sent_no_lights_through_a_whole_wake_route_reply_cycle(client, events, services, plug):
    """THE USER-FACING PROMISE: a satellite with lights_enabled false is
    never sent "lights". Every stage that lights a ring runs here
    (listening, thinking, putting it out) and the reply is really played, so
    the absence below is the guard working and not a cycle that stopped
    early."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"lights_enabled": False}).status_code == 200
        assert ws.receive_json() == {"type": "config", "lights_enabled": False}
        satellite = plug(ws)
        satellite.send(utterance())
        [done] = routed(events)
        wait(satellite.speaker, what="the reply on the speaker")

    assert done["error"] is None and done["played"] is True
    assert done["transcript"] == "what time is it"
    assert services.hosts() == ["stt.test", "tts.test"]
    assert satellite.texts("lights") == []
    # The satellite was really talked to during the cycle: ducked, then
    # unducked.
    assert [m["type"] for m in satellite.texts()] == ["duck", "unduck"]


def test_the_duck_is_lifted_before_the_reply_starts_or_the_reply_is_ducked_too(client, events, plug):
    """The firmware ducks the hub's audio, and the reply is hub audio."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        routed(events)
        wait(satellite.speaker, what="the reply")
    got = list(satellite.got)
    unduck = next(i for i, m in enumerate(got) if isinstance(m, dict) and m["type"] == "unduck")
    first_sample = next(i for i, m in enumerate(got) if isinstance(m, bytes))
    assert unduck < first_sample


def test_forgetting_a_satellite_mid_conversation_lifts_its_duck_first_and_sends_it_nothing_after(
        client, events, services, plug):
    """Forgotten while the hub held its duck, a satellite was left with its
    audio lowered for up to a minute, and the cancelled conversation then sent
    "unduck" and "lights" to a satellite that was pending by then. The duck
    is lifted ahead of the "forget", while the satellite still takes it, and
    after the "forget" there is nothing."""
    services.stt_gate = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        wait(lambda: satellite.texts("duck"), what="the duck")
        wait(lambda: any(m["mode"] == "spin" for m in satellite.texts("lights")), what="thinking")
        assert client.post(f"/satellites/{NID}/forget").status_code == 204
        services.stt_gate.set()
        routed(events)
        time.sleep(0.3)
    got = list(satellite.got)
    kinds = [m["type"] if isinstance(m, dict) else "audio" for m in got]
    forget = kinds.index("forget")
    assert "unduck" in kinds[:forget]
    assert kinds[forget + 1:] == []


def test_a_flush_stops_the_audio_already_playing_not_only_what_is_queued(client, plug):
    """A reply is one item in the speaker queue. Emptying the queue alone let
    the item in hand play to its end, so "stop" stopped nothing that mattered."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        client.post(f"/satellites/{NID}/tone", json={"frequency": 440, "seconds": 3})
        wait(lambda: len(satellite.speaker()) >= 10, what="the tone to start")
        assert client.post(f"/satellites/{NID}/flush").status_code == 204
        at_flush = len(satellite.speaker())
        time.sleep(0.6)
        after = len(satellite.speaker()) - at_flush
    assert satellite.texts("flush")
    assert after <= 2  # one frame may be in flight; 3 s of tone is 150 frames


def test_a_lit_satellite_is_shown_listening_then_thinking_then_put_out(client, events, plug):
    """The control for the test above: the same cycle with lights on does light
    the ring, so the absence there is not a ring nothing ever lights."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        routed(events)
        wait(lambda: satellite.texts("lights") and satellite.texts("lights")[-1]["mode"] == "off",
             what="the ring to be put out")
    modes = [m["mode"] for m in satellite.texts("lights")]
    # No front-end here, so no direction: a pulse rather than a pointer.
    assert modes == ["pulse", "spin", "off"]


def test_a_ring_left_lit_when_lights_were_turned_off_is_put_out_when_they_return(
        client, events, services, plug):
    """Lights turned off mid-conversation: the "off" at the end may not be
    sent, so the hub's layer is left showing on a dark ring. When lights come
    back on it would reappear, a spinning ring for a conversation long over,
    unless the hub puts it out then."""
    services.stt_gate = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        wait(lambda: any(m["mode"] == "spin" for m in satellite.texts("lights")), what="thinking")
        client.patch(f"/satellites/{NID}", json={"lights_enabled": False})
        services.stt_gate.set()
        routed(events)
        wait(satellite.speaker, what="the reply")
        dark = len(satellite.texts("lights"))
        client.patch(f"/satellites/{NID}", json={"lights_enabled": True})
        wait(lambda: len(satellite.texts("lights")) > dark, what="the ring to be put out")
    lights = satellite.texts("lights")
    assert [m["mode"] for m in lights] == ["pulse", "spin", "off"]
    # And the "off" came after lights were enabled again, not before.
    config = [i for i, m in enumerate(satellite.texts()) if m.get("type") == "config"]
    off = next(i for i, m in enumerate(satellite.texts()) if m.get("mode") == "off")
    assert off > config[-1]


def test_a_dark_satellites_lights_route_is_refused_rather_than_sent(client, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.patch(f"/satellites/{NID}", json={"lights_enabled": False})
        assert ws.receive_json()["type"] == "config"
        satellite = plug(ws)
        r = client.post(f"/satellites/{NID}/lights", json={"mode": "solid"})
        time.sleep(0.1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "lights_disabled"
    assert satellite.texts("lights") == []


def test_the_ring_points_at_the_talker_and_wraps_round():
    from app.main import ring
    c = (0, 0, 100)
    assert int(np.argmax([p[2] for p in ring(0, 12, c)])) == 0
    assert int(np.argmax([p[2] for p in ring(90, 12, c)])) == 3
    near_top = [p[2] for p in ring(355, 12, c)]
    assert int(np.argmax(near_top)) == 0 and near_top[11] > near_top[1] > 0
    assert all(len(p) == 3 for p in ring(123, 12, c))


# ---- one conversation, end to end ----------------------------------------------------


def test_a_wake_word_is_published_with_its_score_and_the_command_is_routed(client, events, services, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance(speech_s=1.0))
        [done] = routed(events)
        # All of it: the hub paces a reply at real time plus 300 ms, so the
        # first frame arrives long before the last.
        wait(lambda: sum(len(f) - 16 for f in satellite.speaker()) >= TTS_SAMPLES * 2 * 2,
             what="the whole reply")
    [wake] = of(events, "wake")
    assert (wake["satellite"], wake["wake_word"], wake["score"]) == (NID, "hey_jarvis", 0.9)
    assert "direction" in wake
    # rule_id names what answered: the wake word's own entry.
    assert (done["rule_id"], done["reply_to"], done["endpoint"]) == ("hey_jarvis", NID, "silence")
    # The command sent to STT is the speech after the marker plus the
    # endpointer's padding, not the marker and not the whole second of room.
    assert 1.0 <= done["command_s"] <= 1.7
    # 0.2 s at 24 kHz became 0.4 s at 48 kHz, in 20 ms frames of 1920 bytes.
    assert sum(len(f) - 16 for f in satellite.speaker()) == TTS_SAMPLES * 2 * 2


def test_a_second_wake_word_during_a_conversation_is_not_a_second_conversation(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        audio = np.concatenate((floor(0.2), wake_mark(), voiced(0.4), wake_mark(), voiced(0.4),
                                floor(1.2, seed=1)))
        satellite.send(audio)
        routed(events)
        time.sleep(0.2)
    assert len(of(events, "wake")) == 1 and len(of(events, "routed")) == 1


def test_nothing_said_after_the_wake_word_is_not_sent_to_stt(client, events, services, plug):
    """Silence given to Whisper-style models comes back as invented text."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(np.concatenate((floor(0.2), wake_mark(), floor(4.5, seed=2))))
        [done] = routed(events)
    assert done["endpoint"] == "no_speech" and done["error"] == "nothing was said after the wake word"
    assert services.hosts() == []
    assert satellite.speaker() == []


def test_without_stt_configured_a_wake_word_is_still_published_and_routing_says_why(
        client, app, events, tmp_path, plug):
    routing.configure(routing.Router(routing.Rules(tmp_path), stt_url="", tts_url="",
                                     lookup=app.lookup_satellite))
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        plug(ws).send(utterance())
        [done] = routed(events)
    assert len(of(events, "wake")) == 1
    assert "SATELLITES_STT_URL" in done["error"] and done["played"] is False


def test_a_reply_for_another_satellite_is_played_there_and_both_are_ducked(client, events, tmp_path, plug):
    with client.websocket_connect("/satellites/ws") as ws1, client.websocket_connect("/satellites/ws") as ws2:
        adopt(client, ws1, name="kitchen")
        adopt(client, ws2, name="bedroom", mac=MAC2)
        assert client.put("/satellites/wake-words", json={"words": [
            {"name": "hey_jarvis", "action": {"destination": {"type": "echo"},
                                              "reply_to": "bedroom"}}]}).status_code == 200
        kitchen, bedroom = plug(ws1), plug(ws2, NID2)
        kitchen.send(utterance())
        [done] = routed(events)
        wait(bedroom.speaker, what="the reply in the bedroom")
    assert done["reply_to"] == NID2 and done["played"] is True
    assert kitchen.speaker() == []
    assert [m["type"] for m in bedroom.texts() if m["type"] in ("duck", "unduck")] == ["duck", "unduck"]
    assert [m["type"] for m in kitchen.texts() if m["type"] in ("duck", "unduck")] == ["duck", "unduck"]


def test_a_wake_word_heard_while_an_injected_conversation_holds_the_satellite_is_ignored(
        client, events, services, plug):
    """The Ear only reports a wake word while it is idle, but an injected
    conversation takes the satellite without the Ear knowing: the hub's own
    one-at-a-time check is what stops a second conversation then."""
    services.stt_gate = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(r=client.post(
            f"/satellites/{NID}/inject", params={"play": 1}, content=wav(utterance()))))
        t.start()
        wait(lambda: of(events, "wake"), what="the injected wake word")
        satellite.send(utterance())
        time.sleep(0.3)
        services.stt_gate.set()
        t.join(10)
        wait(lambda: len(of(events, "routed")) >= 1, what="the injected conversation")
        time.sleep(0.2)
    assert result["r"].status_code == 200
    assert len(of(events, "wake")) == 1 and len(of(events, "routed")) == 1


def test_a_satellite_with_its_speaker_off_is_not_played_to(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.patch(f"/satellites/{NID}", json={"speaker_enabled": False})
        assert ws.receive_json()["type"] == "config"
        satellite = plug(ws)
        satellite.send(utterance())
        [done] = routed(events)
        time.sleep(0.2)
    assert done["played"] is False and "speaker off" in done["note"]
    assert satellite.speaker() == []


def test_a_tone_or_say_for_a_satellite_with_its_speaker_off_is_refused_not_streamed(client, plug):
    """/tone and /say queued audio without asking, so a satellite with its
    speaker off was streamed a tone and kept silent only by its own firmware.
    The promise is the hub's, as it is for lights. /say is refused before
    Kokoro is asked (here there is no Kokoro, which would be a 503)."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"speaker_enabled": False}).status_code == 200
        assert ws.receive_json()["type"] == "config"
        satellite = plug(ws)
        tone = client.post(f"/satellites/{NID}/tone", json={"frequency": 440, "seconds": 1})
        say = client.post(f"/satellites/{NID}/say", json={"text": "hello"})
        time.sleep(0.3)
    for r in (tone, say):
        assert r.status_code == 409 and r.json()["error"]["code"] == "speaker_disabled"
    assert satellite.speaker() == []


def test_turning_a_speaker_off_stops_what_it_is_playing(client, plug):
    """The reply or tone in hand used to stream on to its end after the
    speaker was turned off, because only new audio was checked."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        client.post(f"/satellites/{NID}/tone", json={"frequency": 440, "seconds": 3})
        wait(lambda: len(satellite.speaker()) >= 10, what="the tone to start")
        assert client.patch(f"/satellites/{NID}", json={"speaker_enabled": False}).status_code == 200
        at_off = len(satellite.speaker())
        time.sleep(0.6)
        after = len(satellite.speaker()) - at_off
    assert satellite.texts("flush")
    assert after <= 2  # one frame may be in flight; 3 s of tone is 150 frames


def test_a_muted_satellites_frames_are_not_listened_to(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        ws.send_json({"type": "status", "muted": True})
        satellite = plug(ws)
        satellite.send(utterance())
        time.sleep(0.5)
    assert of(events, "wake") == []


# ---- the listener itself ------------------------------------------------------------


def test_microphone_frames_past_the_queue_drop_the_oldest_and_are_counted(app):
    from types import SimpleNamespace
    s = app.Session(SimpleNamespace(headers={}, client=None), hello())
    for i in range(app.MIC_QUEUE + 10):
        s.offer_mic(bytes([i]))
    assert s.mic.qsize() == app.MIC_QUEUE and s.mic_dropped == 10
    assert s.mic.get_nowait() == bytes([10])  # the first ten went, not the last


def test_the_front_end_and_wake_words_run_off_the_event_loop(client, events, monkeypatch, plug):
    """A 20 ms frame's worth of numpy on the event loop is 20 ms in which no
    other satellite's socket is read. The Ear runs in the thread pool,
    always."""
    on_loop: list[bool] = []
    real = listening.Ear.process

    def spy(self, frames):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(self, frames)
    monkeypatch.setattr(listening.Ear, "process", spy)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        plug(ws).send(utterance())
        routed(events)
    assert on_loop and not any(on_loop)


def test_a_fault_in_the_signal_code_does_not_leave_the_satellite_deaf(client, events, monkeypatch, plug):
    real = listening.Ear.process
    calls = {"n": 0}

    def flaky(self, frames):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloatingPointError("a bad block")
        return real(self, frames)
    monkeypatch.setattr(listening.Ear, "process", flaky)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(floor(0.5))
        wait(lambda: calls["n"] > 1, what="the listener to carry on")
        satellite.send(utterance())
        [done] = routed(events)
    assert done["error"] is None


# ---- buttons ----------------------------------------------------------------------------


def test_push_to_talk_starts_a_conversation_named_ptt(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        ws.send_json({"type": "button", "button": "play", "action": "press"})
        satellite.send(np.concatenate((voiced(1.0), floor(1.2, seed=1))))
        [done] = routed(events)
    [wake] = of(events, "wake")
    assert wake["wake_word"] == "ptt" and wake["score"] is None
    assert done["error"] is None and done["wake_word"] == "ptt"
    assert [e["type"] for e in events if e["type"] in ("button", "wake")] == ["button", "wake"]


def test_the_stop_button_cancels_the_conversation_and_flushes_the_speaker(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(np.concatenate((floor(0.2), wake_mark(), voiced(0.5))))
        wait(lambda: of(events, "wake"), what="the wake word")
        ws.send_json({"type": "button", "button": "set", "action": "press"})
        [done] = routed(events)
        wait(lambda: satellite.texts("flush"), what="the flush")
        # And the satellite listens again afterwards.
        satellite.send(utterance())
        routed(events, 2)
    assert done["error"] == "cancelled" and done["note"] == "cancelled"
    assert [m["type"] for m in satellite.texts() if m["type"] in ("duck", "unduck")][:2] == ["duck", "unduck"]


def test_a_button_mapping_is_validated_and_never_sent_to_the_satellite(client, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        ok = client.patch(f"/satellites/{NID}", json={"buttons": {
            "play": {"press": "ptt"}, "mode": {"release": "webhook:https://hooks.test/x"}}})
        bad = [client.patch(f"/satellites/{NID}", json={"buttons": b}) for b in (
            {"rec": {"press": "ptt"}},                               # the privacy mute
            {"play": {"press": "webhook:https://u:p@hooks.test/"}},  # credentials in a URL
            {"play": {"press": "launch"}},                           # not an action
            {"play": {"hold": "ptt"}},                               # not an action kind
        )]
        time.sleep(0.1)
    assert ok.status_code == 200
    assert ok.json()["config"]["buttons"]["mode"] == {"release": "webhook:https://hooks.test/x"}
    assert [r.status_code for r in bad] == [422] * 4
    assert satellite.texts("config") == []


def test_a_webhook_button_posts_the_press_and_still_publishes_it(client, app, events):
    posted: list[dict] = []

    def hook(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(204)
    app.hub.http = httpx.AsyncClient(transport=httpx.MockTransport(hook))
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.patch(f"/satellites/{NID}", json={"buttons": {"mode": {"press": "webhook:http://hooks.test/b"}}})
        ws.send_json({"type": "button", "button": "mode", "action": "press"})
        wait(lambda: posted, what="the webhook")
    assert posted == [{"satellite": "kitchen", "satellite_id": NID, "button": "mode", "action": "press",
                       "held_ms": None}]
    assert of(events, "button")[0]["button"] == "mode"


# ---- earcons ------------------------------------------------------------------------------


class EarconStore:
    """The satellite's side of earcons.py: an empty store that takes
    uploads."""

    def __init__(self, ready_after: int = 0):
        self.files: dict[str, bytearray] = {}
        self.putting: dict | None = None
        self.lists = 0
        self.ready_after = ready_after

    def __call__(self, msg):
        if isinstance(msg, bytes):
            if msg[0] != 4 or self.putting is None:
                return []
            off = struct.unpack_from("<I", msg, 4)[0]
            buf = self.files.setdefault(self.putting["id"], bytearray())
            assert off == len(buf)
            buf += msg[8:]
            if len(buf) < self.putting["size"]:
                return [{"type": "earcon_next", "id": self.putting["id"], "offset": len(buf)}]
            p, self.putting = self.putting, None
            return [{"type": "earcon_stored", "id": p["id"], "size": p["size"], "sha256": p["sha256"]}]
        if msg.get("type") == "earcon_list":
            self.lists += 1
            return [{"type": "earcons", "ready": self.lists > self.ready_after, "items": [],
                     "last_load_us": 0}]
        if msg.get("type") == "earcon_put":
            self.putting = msg
            return [{"type": "earcon_next", "id": msg["id"], "offset": 0}]
        return []


EARCON_CAPS = {"earcons": {"max": 16, "max_bytes": 192000, "rate": 48000}}


def test_earcons_the_satellite_lacks_are_uploaded_at_welcome_and_the_wake_one_is_played(client, events, plug):
    store = EarconStore()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        satellite = plug(ws, answer=store)  # answers the earcon_list that followed the welcome
        wait(lambda: len(satellite.texts("earcon_put")) == 3 and store.putting is None
             and len(store.files) == 3, what="three uploads")
        wait(lambda: client.get(f"/satellites/{NID}").json()["earcons"]["have"] == ["done", "error", "wake"],
             what="the hub to count them")
        satellite.send(utterance())
        routed(events)
    from app import earcons
    assert {k: bytes(v) for k, v in store.files.items()} == earcons.defaults()
    played = [m["id"] for m in satellite.texts("earcon")]
    assert played == ["wake"]  # and no "done": the reply itself says it is done


def test_earcon_storage_still_formatting_is_asked_again(client, app, monkeypatch, plug):
    monkeypatch.setattr(app, "EARCON_RETRY_S", 0.05)
    store = EarconStore(ready_after=2)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        plug(ws, answer=store)
        wait(lambda: len(store.files) == 3, what="the uploads after the storage came up")
    assert store.lists >= 3


def test_a_satellite_without_earcon_support_is_never_sent_earcon_messages(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        satellite.send(utterance())
        routed(events)
    assert not [m for m in satellite.texts() if m["type"].startswith("earcon")]


# ---- inject -----------------------------------------------------------------------------------


def test_inject_runs_a_clip_through_the_whole_path_and_sends_the_satellite_nothing(client, events, services, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        r = client.post(f"/satellites/{NID}/inject", content=wav(utterance()))
        time.sleep(0.2)
    body = r.json()
    assert r.status_code == 200, body
    assert body["heard"]["wake_word"] == "hey_jarvis"
    assert body["command"]["reason"] == "silence" and body["command"]["had_speech"] is True
    assert body["outcome"]["transcript"] == "what time is it" and body["outcome"]["error"] is None
    assert body["outcome"]["reply_audio_bytes"] == TTS_SAMPLES * 2 * 2
    assert body["played"] is False
    assert services.hosts() == ["stt.test", "tts.test"]
    assert satellite.got == []  # not a light, not a duck, not a sample
    assert [e.get("injected") for e in events if e["type"] in ("wake", "routed")] == [True, True]


def test_inject_with_play_speaks_the_reply_on_the_satellite(client, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(ws)
        r = client.post(f"/satellites/{NID}/inject", params={"play": 1}, content=wav(utterance()))
        wait(satellite.speaker, what="the reply")
    assert r.json()["played"] is True
    assert [m["type"] for m in satellite.texts() if m["type"] in ("duck", "unduck")] == ["duck", "unduck"]


def test_the_stop_button_cancels_an_injected_conversation_that_is_playing(client, events, services, plug):
    services.stt_gate = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        plug(ws)
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(r=client.post(
            f"/satellites/{NID}/inject", params={"play": 1}, content=wav(utterance()))))
        t.start()
        wait(lambda: of(events, "wake"), what="the injected wake word")
        ws.send_json({"type": "button", "button": "set", "action": "press"})
        t.join(10)
        services.stt_gate.set()
    body = result["r"].json()
    assert result["r"].status_code == 200 and body["played"] is False
    assert body["outcome"]["error"] == "cancelled"


def test_inject_with_a_wake_word_takes_the_whole_clip_as_the_command(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post(f"/satellites/{NID}/inject", params={"wake_word": "ptt"},
                        content=wav(np.concatenate((voiced(1.0), floor(0.3)))))
    body = r.json()
    assert body["heard"] == {"wake_word": "ptt", "score": None, "at_s": 0.0}
    assert body["outcome"]["transcript"] == "what time is it"


def test_inject_that_hears_no_wake_word_routes_nothing(client, services):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post(f"/satellites/{NID}/inject", content=wav(voiced(1.0)))
    assert r.json() == {"satellite": NID, "heard": None, "command": None, "outcome": None,
                        "played": False}
    assert services.hosts() == []


@pytest.mark.parametrize("body,code", [
    (b"", 400),
    (b"RIFF not really", 400),
    (wav(voiced(0.5), rate=8000), 400),
    (wav(np.repeat(voiced(0.5), 2), channels=2), 400),
], ids=["empty", "not-wav", "8-khz", "stereo"])
def test_inject_refuses_audio_it_would_misread(client, body, code):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        r = client.post(f"/satellites/{NID}/inject", content=body)
    assert r.status_code == code and "error" in r.json()


def test_inject_needs_an_adopted_satellite_and_play_needs_it_online(client):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        ws.receive_json()
        assert client.post(f"/satellites/{NID}/inject", content=wav(utterance())).status_code == 409
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
    r = client.post(f"/satellites/{NID}/inject", params={"play": 1}, content=wav(utterance()))
    assert r.status_code == 409 and r.json()["error"]["code"] == "satellite_offline"


# ---- the rest of the wiring --------------------------------------------------------------------


def test_the_routing_routes_are_not_taken_for_a_satellite_called_routing(client):
    r = client.get("/satellites/routing")
    assert r.status_code == 200 and r.json()["rules"][0]["wake_word"] == "hey_jarvis"


def test_a_home_assistant_switch_goes_through_the_same_path_as_patch(client, app):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.portal.call(app.mqtt_command, NID, {"volume": 20})
        assert ws.receive_json() == {"type": "config", "volume": 20}
    assert client.get(f"/satellites/{NID}").json()["config"]["volume"] == 20


class Bridge:
    def __init__(self):
        self.events, self.satellites = [], []

    def publish_event(self, e):
        self.events.append(e)

    def publish_satellite(self, n):
        self.satellites.append(n)

    def health(self):
        return {"enabled": True}

    async def stop(self):
        return None


def test_hub_events_reach_mqtt_but_an_injected_test_does_not(client, app):
    bridge = app.hub.bridge = Bridge()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        client.post(f"/satellites/{NID}/inject", content=wav(utterance()))
        ws.send_json({"type": "button", "button": "vol_up", "action": "press"})
        wait(lambda: any(e["type"] == "button" for e in bridge.events), what="the button")
        client.patch(f"/satellites/{NID}", json={"volume": 10})
    assert not [e for e in bridge.events if e["type"] in ("wake", "routed")]
    assert bridge.satellites and bridge.satellites[-1]["config"]["volume"] == 10


def test_health_reports_listening_routing_and_mqtt(client):
    body = client.get("/health").json()
    assert body["voice"]["state"] == "ready" and body["voice"]["wake_words"] == {"hey_jarvis": 0.5}
    assert body["routing"]["rules"] == 1 and body["routing"]["stt"] == "http://stt.test"
    assert body["mqtt"] == {"enabled": False, "connected": False, "broker": None, "error": None}


def test_a_bad_wake_word_setting_leaves_the_hub_up_and_says_why(env, fake_models, monkeypatch):
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_jarvis:loud")
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        body = c.get("/health").json()
    assert body["voice"]["state"] == "off" and "SATELLITES_WAKE_WORDS" in body["voice"]["error"]


# ---- firmware signatures ---------------------------------------------------------------------------


IMAGE = b"\xe9" + bytes(range(256)) * 64


@pytest.fixture
def signed(tmp_path, monkeypatch, env, fake_models):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv(signing.ENV_KEY, pem.decode())
    app = importlib.reload(importlib.import_module("app.main"))
    der = key.sign(IMAGE, ec.ECDSA(hashes.SHA256()))
    return app, signing.encode(der), signing.key_id(key.public_key())


def test_a_signed_image_keeps_its_signature_and_the_update_carries_it(signed):
    app, sig, kid = signed
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        unsigned = c.post("/satellites/firmware", params={"model": MODEL}, content=IMAGE)
        fw = c.post("/satellites/firmware", params={"model": MODEL, "version": "v2", "signature": sig},
                    content=IMAGE)
        adopt(c, ws, ota_key=kid)
        r = c.post("/satellites/ota", json={"satellite": NID, "sha256": fw.json()["sha256"]})
        start = ws.receive_json()
    assert unsigned.status_code == 400 and unsigned.json()["error"]["code"] == "bad_signature"
    assert fw.status_code == 200 and fw.json()["signature"] == sig
    assert r.json()["started"] == [NID]
    assert start["type"] == "ota" and start["signature"] == sig


def test_an_unsigned_image_is_not_sent_to_a_satellite_that_only_takes_signed_ones(app):
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        fw = c.post("/satellites/firmware", params={"model": MODEL}, content=IMAGE).json()
        adopt(c, ws, ota_key="0123456789abcdef")
        r = c.post("/satellites/ota", json={"satellite": NID, "sha256": fw["sha256"]}).json()
    assert fw["signature"] is None
    assert r["started"] == [] and "unsigned" in r["skipped"][NID]


def test_a_malformed_signature_is_refused_before_it_is_stored(app, tmp_path):
    with TestClient(app.app) as c:
        r = c.post("/satellites/firmware", params={"model": MODEL, "signature": "bm90IGEgc2ln"},
                   content=IMAGE)
        listed = c.get("/satellites/firmware").json()["firmware"]
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_signature"
    assert listed == []


# ---- with the real models ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    pytest.importorskip("openwakeword", reason="openwakeword is not installed")
    d = Path(os.environ.get("SATELLITES_TEST_WAKEWORD_DIR") or tmp_path_factory.mktemp("wakewords"))
    try:
        wakeword.ensure_models(["hey_jarvis"], d)
    except httpx.HTTPError as e:
        pytest.skip(f"openWakeWord models could not be fetched from GitHub (offline?): {e!r}")
    return d


def fixture(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int16)


@pytest.fixture
def real(env, model_dir, monkeypatch, services, tmp_path):
    monkeypatch.setenv("SATELLITES_MODEL_DIR", str(model_dir))
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        route_to_fakes(app, services, tmp_path)
        wait(lambda: app.hub.voice.state in ("ready", "failed"), timeout=60, what="the models")
        assert app.hub.voice.state == "ready", app.hub.voice.error
        yield app, c


def test_a_recorded_hey_jarvis_is_heard_endpointed_and_routed_by_inject(real):
    app, c = real
    with c.websocket_connect("/satellites/ws") as ws:
        adopt(c, ws)
        r = c.post(f"/satellites/{NID}/inject", content=wav(fixture("hey_jarvis_en_us.wav")))
    body = r.json()
    assert body["heard"]["wake_word"] == "hey_jarvis" and body["heard"]["score"] >= 0.5
    assert body["command"]["reason"] == "silence" and body["command"]["had_speech"]
    assert body["outcome"]["transcript"] == "what time is it"


def test_hey_jarvis_through_the_front_end_on_three_microphones_is_heard_live(
        real, monkeypatch, services):
    """The whole listening path as the Korvo would drive it: four channels at
    16 kHz over the socket, the front-end on, the real model, the endpointer
    on the front-end's output. The talker reaches the three microphones 0, 2
    and 3 samples apart, over a -45 dBFS noise floor of their own."""
    app, c = real
    monkeypatch.setattr(app.hub.voice, "frontend", True)
    events: list[dict] = []
    publish = app.hub.publish
    app.hub.publish = lambda e: (events.append(e), publish(e))
    rng = np.random.default_rng(3)
    speech = fixture("hey_jarvis_en_us.wav").astype(np.float64)
    total, at = int(7 * RATE), int(1.5 * RATE)
    mics = []
    for d in (0, 2, 3):
        m = rng.standard_normal(total) * 32768 * 10 ** (-45 / 20)
        m[at + d:at + d + len(speech)] += speech
        mics.append(m)
    ref = rng.standard_normal(total) * 32768 * 10 ** (-89 / 20)
    capture = np.clip(np.stack([ref, *mics], axis=1), -32768, 32767).astype("<i2")
    with c.websocket_connect("/satellites/ws") as ws:
        adopt(c, ws)
        satellite = Satellite(ws, backlog=lambda: app.hub.sessions[NID].mic.qsize())
        satellite.send_frames(struct.pack("<BBBBIQ", 1, 0, 4, 0, i, 0) + capture[off:off + FRAME].tobytes()
                         for i, off in enumerate(range(0, total - FRAME + 1, FRAME)))
        [done] = routed(events)
        stats = c.get(f"/satellites/{NID}").json()["listening"]
    [wake] = of(events, "wake")
    assert wake["wake_word"] == "hey_jarvis" and wake["score"] >= 0.5
    assert done["endpoint"] == "silence" and done["error"] is None
    assert stats["frontend"] is True and stats["mic_dropped"] == 0
    assert satellite.texts("lights")  # lit on the way, and the dark test covers the other case
