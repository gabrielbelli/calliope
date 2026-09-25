"""Command, Conversation and trigger words, end to end through the hub: the
satellite's socket, its Ear, the Conversation, dialogue.run_turn and back to
its speaker, lights and earcons.

The fakes are test_pipeline.py's: the satellite is Starlette's test socket,
STT, TTS, the LLM and Home Assistant are *.test handlers, and a wake word is a
marker sample (MarkerWords knows three: hey_jarvis, alexa and lumos). The
barge-in tests switch the front-end on and build what a Korvo would send
while it plays a reply: its own clipped voice on the loopback channel and, as
echo, on the three microphones, then a talker over it (test_frontend.py's
scenes).
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import struct
import threading
import time
import wave
from collections import deque

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import audio, wakeword
from app.wakeword import Detection
from test_frontend import dbfs, echo, echo_paths, plane_wave, sensor_noise, speechlike
from test_pipeline import (EARCON_CAPS, FRAME, NID, RATE, EarconStore, FakeWakeWords, Satellite,
                           Services, adopt, floor, hello, of, route_to_fakes, voiced, wait)
from test_pipeline import env  # noqa: F401  (a fixture)

MARKS = {30000: "hey_jarvis", -30000: "lumos", 20000: "alexa"}
SECRET = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LWhhLXRva2Vu.do-not-leak"
LLM = {"type": "llm", "base_url": "http://llm.test/v1", "model": "tiny"}
HA = {"type": "ha_conversation", "url": "http://ha.test:8123"}


class MarkerWords(FakeWakeWords):
    """FakeWakeWords with a marker per word: a frame of one sample value."""

    def clone(self, names=None):
        twin = MarkerWords(self.thresholds)
        twin.names = list(self.names if names is None else names)
        twin.thresholds = self.thresholds
        return twin

    def feed(self, pcm):
        self.position += len(pcm)
        for value, name in MARKS.items():
            hits = np.flatnonzero(pcm == value)
            if len(hits) >= FRAME // 2 and name in self.names:
                return [Detection(name, 0.9, self.position - len(pcm) + int(hits[-1]) + 1)]
        return []


def mark(name: str) -> np.ndarray:
    return np.full(FRAME, next(v for v, n in MARKS.items() if n == name), np.int16)


def sse(pieces: list[str], delay: float = 0.0, done: list | None = None) -> httpx.Response:
    async def body():
        for p in pieces:
            if delay:
                await asyncio.sleep(delay)
            yield f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n".encode()
        if done is not None:
            done.append(time.monotonic())
        yield b"data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


class Fakes(Services):
    """test_pipeline's STT and TTS, plus a streaming LLM and Home Assistant.
    `transcripts` and `answers` are taken one per call, in order."""

    def __init__(self):
        super().__init__()
        self.transcripts: deque[str] = deque()
        self.answers: deque[list[str]] = deque()
        self.llm_delay = 0.0
        self.llm_done: list[float] = []
        self.tts_audio: deque[np.ndarray] = deque()
        self.handlers |= {"llm.test": self.llm, "ha.test": self.ha}
        self.ha_answer = {"response_type": "action_done",
                          "speech": {"plain": {"speech": "Done."}}}

    async def stt(self, request):
        while self.stt_gate is not None and not self.stt_gate.is_set():
            await asyncio.sleep(0.01)
        text = self.transcripts.popleft() if self.transcripts else self.transcript
        return httpx.Response(200, json={"text": text}, headers={"x-stt-engine": "parakeet"})

    def tts(self, request):
        if self.tts_audio:
            return httpx.Response(200, content=self.tts_audio.popleft().astype("<i2").tobytes())
        return super().tts(request)

    def llm(self, request):
        return sse(self.answers.popleft() if self.answers else ["Okay."], self.llm_delay,
                   self.llm_done)

    def ha(self, request):
        return httpx.Response(200, json={"conversation_id": "01HA", "response": self.ha_answer})

    def sent(self, host: str, path: str | None = None) -> list[httpx.Request]:
        return [r for r in list(self.seen) if r.url.host == host
                and (path is None or r.url.path == path)]


@pytest.fixture
def services() -> Fakes:
    return Fakes()


@pytest.fixture
def app(env, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(wakeword, "ensure_models", lambda names, model_dir, **kw: None)
    monkeypatch.setattr(wakeword, "WakeWords", MarkerWords)
    monkeypatch.delenv("SATELLITES_HA_TOKEN", raising=False)
    # A custom model file, so "lumos" is a name the hub can load.
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "lumos.onnx").write_bytes(b"")
    return importlib.reload(importlib.import_module("app.main"))


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
    def make(ws, nid: str = NID, answer=None) -> Satellite:
        def backlog() -> int:
            s = app.hub.sessions.get(nid)
            return s.mic.qsize() if s is not None else 0
        return Satellite(ws, answer=answer, backlog=backlog)
    return make


def save(client, *entries: dict, ptt: dict | None = None) -> dict:
    body: dict = {"words": list(entries)}
    if ptt is not None:
        body["ptt"] = ptt
    r = client.put("/satellites/wake-words", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def conversation(name="hey_jarvis", follow_up_s=2.0, **extra) -> dict:
    return {"name": name, "mode": "conversation", "action": {"destination": LLM},
            "conversation": {"follow_up_s": follow_up_s} | extra}


def settled(app, nid: str = NID):
    """The conversation on `nid`, once it waits for its next turn."""
    s = app.hub.sessions[nid]
    return wait(lambda: s.conversation is not None and s.conversation.phase == "listening"
                and s.conversation.turns and s.conversation, what="the follow-up")


def say(satellite: Satellite, seconds: float = 0.8, seed: int = 2) -> None:
    """A follow-up: the loopback's quiet guard, the words, then the room."""
    satellite.send(np.concatenate((floor(0.3, seed=seed), voiced(seconds), floor(1.2, seed=seed + 1))))


def llm_messages(services: Fakes, i: int) -> list[tuple[str, str]]:
    body = json.loads(services.sent("llm.test")[i].content)
    return [(m["role"], m["content"]) for m in body["messages"]]


# ---- a conversation --------------------------------------------------------------------


def test_three_turns_follow_each_other_without_the_wake_word_and_carry_their_memory(
        client, app, events, services, plug):
    services.transcripts.extend(["what time is it", "and in London", "what did I ask first"])
    services.answers.extend([["It is seven."], ["It is eleven there."], ["You asked the time."]])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation())
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        for n in (1, 2):
            settled(app)
            wait(lambda: len(of(events, "turn")) == n, what=f"turn {n}")
            say(sat, seed=10 * n)
        wait(lambda: len(of(events, "turn")) == 3, what="turn 3")
        settled(app)
        sat.send(floor(3.0, seed=40))  # nothing said for follow_up_s
        [ended] = wait(lambda: of(events, "conversation_ended"), what="the end")
    [started] = of(events, "conversation_started")
    assert (started["rule_id"], started["reason"]) == ("hey_jarvis", "wake_word")
    assert len(of(events, "wake")) == 1 and of(events, "routed") == []
    turns = of(events, "turn")
    assert [t["transcript"] for t in turns] == ["what time is it", "and in London",
                                                "what did I ask first"]
    assert all(t["error"] is None and t["played"] for t in turns)
    assert llm_messages(services, 2)[-5:] == [
        ("user", "what time is it"), ("assistant", "It is seven."),
        ("user", "and in London"), ("assistant", "It is eleven there."),
        ("user", "what did I ask first")]
    assert (ended["reason"], ended["turns"]) == ("silence", 3)


@pytest.mark.parametrize("goodbye", ["Thanks, that's all.", "Obrigado, tchau!"])
def test_an_ending_phrase_ends_the_conversation_without_asking_the_assistant(
        client, app, events, services, plug, goodbye):
    services.transcripts.extend(["what time is it", goodbye])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation())
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        say(sat)
        [ended] = wait(lambda: of(events, "conversation_ended"), what="the end")
    assert ended["reason"] == "phrase"
    assert len(services.sent("llm.test")) == 1
    assert of(events, "turn")[-1]["ended"] is True


def test_a_privacy_mute_ends_the_conversation_at_once(client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=30))
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        ws.send_json({"type": "status", "muted": True})
        [ended] = wait(lambda: of(events, "conversation_ended"), timeout=3, what="the end")
        assert app.hub.sessions[NID].conversation is None
    assert ended["reason"] == "muted"


def test_a_conversation_ends_when_the_satellite_stops_sending_audio(client, app, events, plug,
                                                                    monkeypatch):
    """The device stops streaming when muted (or its Wi-Fi drops) and says
    nothing: the follow-up must not wait for ever."""
    monkeypatch.setattr(app, "FOLLOW_UP_SLACK_S", 0.5)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=1))
        plug(ws).send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8),
                                      floor(1.2, seed=1))))
        [ended] = wait(lambda: of(events, "conversation_ended"), timeout=6, what="the end")
    assert ended["reason"] == "no_audio" and ended["turns"] == 1


def test_a_command_home_assistant_did_not_understand_becomes_a_conversation(
        client, app, events, services, plug, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    services.ha_answer = {"response_type": "error", "data": {"code": "no_intent_match"},
                         "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}}}
    services.transcripts.extend(["what is a black hole", "and a white one"])
    services.answers.extend([["A collapsed star."], ["A theoretical one."]])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "alexa", "mode": "command",
                      "action": {"destination": HA, "fallback": "hey_jarvis"}},
             conversation())
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("alexa"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        say(sat)
        wait(lambda: len(of(events, "turn")) == 2, what="the second turn")
    [started] = of(events, "conversation_started")
    assert (started["reason"], started["from_rule"], started["rule_id"]) == ("fallback", "alexa",
                                                                             "hey_jarvis")
    first, second = of(events, "turn")[:2]
    assert first["handed_over_to"] == "hey_jarvis" and first["reply_text"] == "A collapsed star."
    assert json.loads(services.sent("ha.test")[0].content)["text"] == "what is a black hole"
    assert llm_messages(services, 1)[-3:] == [("user", "what is a black hole"),
                                              ("assistant", "A collapsed star."),
                                              ("user", "and a white one")]
    assert of(events, "routed") == []


def test_the_first_audio_reaches_the_satellite_before_a_slow_llm_has_finished(
        client, app, events, services, plug):
    services.llm_delay = 0.5
    services.answers.append(["The first sentence is here. ", "The second one follows. ",
                             "And the third ends it."])
    first_frame: list[float] = []

    def note(item):
        if isinstance(item, bytes) and item[:1] == b"\x02" and not first_frame:
            first_frame.append(time.monotonic())
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "hey_jarvis", "action": {"destination": LLM}})
        plug(ws, answer=note).send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8),
                                                   floor(1.2, seed=1))))
        [done] = wait(lambda: of(events, "routed"), what="the routed event")
    t = done["timeline_ms"]
    assert first_frame and services.llm_done and first_frame[0] < services.llm_done[0]
    assert t["first_audio"] < t["answer_done"] and t["answer_done"] < t["reply_done"]
    print(f"\npipeline: first audio {t['first_audio']} ms, answer done {t['answer_done']} ms, "
          f"reply done {t['reply_done']} ms after the end of speech; the satellite's first frame "
          f"came {round((services.llm_done[0] - first_frame[0]) * 1000)} ms before the LLM finished")


# ---- what a satellite may be sent --------------------------------------------------------


def test_a_satellite_with_its_speaker_off_converses_in_events_alone(client, app, events, services, plug):
    """No speaker frames and no earcons, not even the ones it holds, and the
    follow-up opens at once since nothing plays."""
    services.transcripts.extend(["hello", "and again"])
    store = EarconStore()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        sat = plug(ws, answer=store)
        wait(lambda: len(store.files) == 3 and store.putting is None, what="the earcons")
        assert client.patch(f"/satellites/{NID}", json={"speaker_enabled": False}).status_code == 200
        save(client, conversation())
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        say(sat)
        wait(lambda: len(of(events, "turn")) == 2, what="the second turn")
        time.sleep(0.2)
    assert sat.speaker() == [] and sat.texts("earcon") == []
    assert all(t["played"] is False and "speaker off" in t["note"] for t in of(events, "turn"))
    assert [t["reply_text"] for t in of(events, "turn")] == ["Okay.", "Okay."]


def test_a_failed_turn_ends_with_the_error_earcon_and_a_silent_success_with_done(
        client, app, events, plug):
    """What the satellite held before replies were streamed: the reply says
    it is done by itself, and a turn with nothing to say, or one that failed,
    says so with an earcon."""
    store = EarconStore()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        sat = plug(ws, answer=store)
        wait(lambda: len(store.files) == 3 and store.putting is None, what="the earcons")
        save(client, {"name": "hey_jarvis", "action": {"destination": LLM, "reply_to": "none"}},
             {"name": "alexa", "action": {"destination": HA}})  # no token set: it fails
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        wait(lambda: of(events, "routed"), what="the first")
        sat.send(np.concatenate((floor(0.2), mark("alexa"), voiced(0.8), floor(1.2, seed=2))))
        wait(lambda: len(of(events, "routed")) == 2, what="the second")
        time.sleep(0.2)
    assert [m["id"] for m in sat.texts("earcon")] == ["wake", "done", "wake", "error"]
    assert "SATELLITES_HA_TOKEN" in of(events, "routed")[1]["error"]


def test_a_dark_satellite_is_sent_no_lights_through_a_whole_conversation(client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.patch(f"/satellites/{NID}", json={"lights_enabled": False}).status_code == 200
        save(client, conversation())
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        say(sat)
        settled(app)
        sat.send(floor(3.0, seed=40))
        wait(lambda: of(events, "conversation_ended"), what="the end")
    assert len(of(events, "turn")) == 2
    assert sat.texts("lights") == []


def test_a_lit_satellite_pulses_again_while_it_waits_for_a_follow_up(client, app, events, plug):
    """The control for the dark test: the same conversation does light the
    ring, and says with it when it is listening again."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation())
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        sat.send(floor(3.0, seed=40))
        wait(lambda: of(events, "conversation_ended"), what="the end")
        wait(lambda: sat.texts("lights")[-1]["mode"] == "off", what="the ring out")
    assert [m["mode"] for m in sat.texts("lights")] == ["pulse", "spin", "off", "pulse", "off"]


def test_a_satellite_that_is_not_adopted_never_gets_a_conversation(client, app, events, services, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        time.sleep(0.5)
    assert of(events, "wake") == [] and services.seen == [] and sat.got == []


def test_forgetting_a_satellite_mid_conversation_ends_it_and_sends_nothing_after(
        client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=30))
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        settled(app)
        assert client.post(f"/satellites/{NID}/forget").status_code == 204
        [ended] = wait(lambda: of(events, "conversation_ended"), what="the end")
        say(sat)
        time.sleep(0.5)
    kinds = [m["type"] if isinstance(m, dict) else "audio" for m in sat.got]
    assert ended["reason"] == "unadopted"
    assert kinds[kinds.index("forget") + 1:] == []


# ---- trigger words ------------------------------------------------------------------------


LUMOS = {"name": "lumos", "mode": "trigger", "trigger": {"cooldown_s": 3}}


def test_a_trigger_word_publishes_one_event_and_nothing_else_happens(client, app, events, services, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        body = save(client, LUMOS)
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("lumos"), floor(0.5, seed=1),
                                 mark("lumos"), floor(0.5, seed=2))))
        wait(lambda: of(events, "triggered"), what="the trigger")
        time.sleep(0.3)
    assert [w["threshold"] for w in body["words"] if w["name"] == "lumos"] == [0.7]
    [fired] = of(events, "triggered")  # the second, 0.5 s later, is inside the cooldown
    assert {k: fired[k] for k in ("satellite", "satellite_name", "wake_word", "score")} == {
        "satellite": NID, "satellite_name": "kitchen", "wake_word": "lumos", "score": 0.9}
    assert services.seen == [] and of(events, "wake") == [] and of(events, "routed") == []


def test_a_trigger_fires_again_once_its_cooldown_is_over(client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, LUMOS | {"trigger": {"cooldown_s": 0.2}})
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("lumos"), floor(0.3))))
        wait(lambda: of(events, "triggered"), what="the first")
        time.sleep(0.3)
        sat.send(np.concatenate((mark("lumos"), floor(0.3, seed=1))))
        wait(lambda: len(of(events, "triggered")) == 2, what="the second")


@pytest.mark.parametrize("speaker,lights", [(True, True), (False, False)])
def test_trigger_feedback_is_heard_and_seen_only_where_the_satellite_allows_it(
        client, app, events, plug, speaker, lights):
    store = EarconStore()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        sat = plug(ws, answer=store)
        wait(lambda: len(store.files) == 3 and store.putting is None, what="the earcons")
        client.patch(f"/satellites/{NID}", json={"speaker_enabled": speaker,
                                                  "lights_enabled": lights})
        save(client, LUMOS)
        sat.send(np.concatenate((floor(0.2), mark("lumos"), floor(0.3))))
        wait(lambda: of(events, "triggered"), what="the trigger")
        time.sleep(0.6)
    assert [m["id"] for m in sat.texts("earcon")] == (["done"] if speaker else [])
    assert [m["mode"] for m in sat.texts("lights")] == (["solid", "off"] if lights else [])


def test_a_trigger_heard_while_a_conversation_waits_fires_and_the_conversation_goes_on(
        client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=30), LUMOS)
        sat = plug(ws)
        sat.send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        conv = settled(app)
        sat.send(np.concatenate((floor(0.3), mark("lumos"), floor(0.5, seed=1))))
        wait(lambda: of(events, "triggered"), what="the trigger")
        time.sleep(0.2)
        assert app.hub.sessions[NID].conversation is conv
        assert of(events, "conversation_ended") == []


@pytest.mark.parametrize("entry,says", [
    ({"name": "lumos", "mode": "trigger", "action": {"destination": {"type": "echo"}}},
     "a trigger word has no action"),
    # A new name: one already saved keeps its saved action when it leaves it out.
    ({"name": "alexa", "mode": "conversation"}, "a conversation needs an action"),
    ({"name": "alexa", "mode": "command", "action": {"destination": {"type": "echo"},
                                                     "fallback": "lumos"}},
     "its fallback 'lumos' is not one of the wake words"),
], ids=["trigger-with-action", "conversation-without", "fallback-to-nothing"])
def test_an_entry_whose_mode_and_action_do_not_fit_is_refused(client, entry, says):
    r = client.put("/satellites/wake-words", json={"words": [entry]})
    assert r.status_code == 422 and says in r.json()["error"]["message"]


def test_push_to_talk_cannot_be_a_trigger(client):
    r = client.put("/satellites/wake-words", json={"words": [], "ptt": {"mode": "trigger"}})
    assert r.status_code == 422 and "push-to-talk" in r.json()["error"]["message"]


# ---- the configuration ----------------------------------------------------------------------


def test_push_to_talk_has_its_own_action(client, app, events, services, plug):
    services.answers.append(["From the button."])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "hey_jarvis"}, ptt={"mode": "command", "action": {"destination": LLM}})
        sat = plug(ws)
        ws.send_json({"type": "button", "button": "play", "action": "press"})
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=1))))
        [done] = wait(lambda: of(events, "routed"), what="the routed event")
    assert (done["rule_id"], done["reply_text"]) == ("ptt", "From the button.")


def test_an_entry_saved_by_a_client_that_knows_only_models_keeps_its_action(client):
    save(client, conversation())
    r = client.put("/satellites/wake-words", json={"words": [
        {"name": "hey_jarvis", "threshold": 0.6, "satellites": ["*"]}]})
    [w] = r.json()["words"]
    assert (w["threshold"], w["mode"], w["action"]["destination"]["type"]) == (0.6, "conversation", "llm")


def test_secrets_are_named_by_their_variable_and_never_returned(client, app, monkeypatch, tmp_path):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    save(client, {"name": "alexa", "action": {"destination": HA}},
         {"name": "hey_jarvis", "mode": "conversation",
          "action": {"destination": {"type": "ha_assist", "url": "http://ha.test:8123"}}})
    pasted = client.put("/satellites/wake-words", json={"words": [
        {"name": "alexa", "action": {"destination": HA | {"token_env": SECRET}}}]})
    got = client.get("/satellites/wake-words")
    routing = client.get("/satellites/routing")
    assert pasted.status_code == 422
    assert got.json()["env"] == {"SATELLITES_HA_TOKEN": True}
    for text in (got.text, routing.text, pasted.text, (tmp_path / "wake_words.json").read_text()):
        assert SECRET not in text


def test_rules_json_is_no_longer_written_behind_the_hub(client):
    r = client.put("/satellites/routing", json={"rules": [{"id": "x", "destination": {"type": "echo"}}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "routing_per_wake_word"


def test_the_routing_test_runs_a_wake_words_own_action(client, services):
    services.answers.append(["Tested."])
    save(client, conversation())
    r = client.post("/satellites/routing/test", json={"satellite": NID, "wake_word": "hey_jarvis",
                                                      "text": "que horas são"})
    body = r.json()
    assert (body["rule_id"], body["mode"], body["reply_text"]) == ("hey_jarvis", "conversation", "Tested.")
    assert (body["language"], body["voice"]) == ("pt-BR", "pf_dora")
    assert services.sent("stt.test") == []


def test_a_satellite_reports_how_quickly_it_has_been_answering(client, app, events, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "hey_jarvis", "action": {"destination": LLM}})
        plug(ws).send(np.concatenate((floor(0.2), mark("hey_jarvis"), voiced(0.8), floor(1.2, seed=1))))
        wait(lambda: of(events, "routed"), what="the routed event")
        latency = client.get(f"/satellites/{NID}").json()["latency"]
    assert latency["turns"] == 1 and latency["p50_first_audio_ms"] > 0
    assert latency["p50_first_audio_ms"] <= latency["p50_reply_done_ms"]


# ---- push-to-talk from Home Assistant ----------------------------------------------------------


def test_home_assistant_can_start_push_to_talk_and_name_the_word_that_answers(
        client, app, events, services, plug):
    services.answers.append(["Asked from Home Assistant."])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "alexa", "action": {"destination": LLM}})
        sat = plug(ws)
        r = client.post(f"/satellites/{NID}/ptt", json={"wake_word": "alexa"})
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=1))))
        [done] = wait(lambda: of(events, "routed"), what="the routed event")
        wait(lambda: app.hub.sessions[NID].conversation is None, what="the end")
        plain = client.post(f"/satellites/{NID}/ptt", json={})
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=2))))
        wait(lambda: len(of(events, "routed")) == 2, what="the second")
    assert r.status_code == 204 and plain.status_code == 204
    assert (done["wake_word"], done["rule_id"], done["reply_text"]) == ("alexa", "alexa",
                                                                        "Asked from Home Assistant.")
    assert of(events, "routed")[1]["rule_id"] == "ptt"


def test_push_to_talk_from_home_assistant_says_why_it_cannot_start(client, app, events, services, plug):
    services.stt_gate = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, LUMOS)
        sat = plug(ws)
        unknown = client.post(f"/satellites/{NID}/ptt", json={"wake_word": "hey_mycroft"})
        trigger = client.post(f"/satellites/{NID}/ptt", json={"wake_word": "lumos"})
        nobody = client.post("/satellites/aabbccddeeff/ptt", json={})
        assert client.post(f"/satellites/{NID}/ptt").status_code == 204
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=1))))
        wait(lambda: app.hub.sessions[NID].conversation is not None, what="the conversation")
        busy = client.post(f"/satellites/{NID}/ptt", json={})
        services.stt_gate.set()
        wait(lambda: of(events, "routed"), what="the routed event")
        wait(lambda: app.hub.sessions[NID].conversation is None, what="the end")
        client.patch(f"/satellites/{NID}", json={"mic_enabled": False})
        mic_off = client.post(f"/satellites/{NID}/ptt", json={})
        client.patch(f"/satellites/{NID}", json={"mic_enabled": True})
        ws.send_json({"type": "status", "muted": True})
        wait(lambda: app.hub.sessions[NID].status.get("muted"), what="the mute")
        muted = client.post(f"/satellites/{NID}/ptt", json={})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "wake_word_not_found")
    assert (trigger.status_code, trigger.json()["error"]["code"]) == (409, "trigger_word")
    assert nobody.status_code == 404
    assert [(r.status_code, r.json()["error"]["code"]) for r in (busy, mic_off, muted)] == [
        (409, "satellite_busy"), (409, "mic_disabled"), (409, "satellite_muted")]


def test_push_to_talk_is_refused_to_a_satellite_that_is_not_adopted(client, services):
    with client.websocket_connect("/satellites/ws") as ws:
        ws.send_json(hello())
        assert ws.receive_json() == {"type": "pending"}
        r = client.post(f"/satellites/{NID}/ptt", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "satellite_not_adopted")
    assert services.seen == []


# ---- migration from rules.json ------------------------------------------------------------------


def test_words_saved_before_actions_take_the_rule_they_would_have_run(env, monkeypatch, tmp_path, caplog):  # noqa: F811
    monkeypatch.setattr(wakeword, "ensure_models", lambda names, model_dir, **kw: None)
    monkeypatch.setattr(wakeword, "WakeWords", MarkerWords)
    rules = json.dumps({"version": 1, "rules": [
        {"id": "kitchen-ha", "wake_word": "hey_jarvis", "satellites": ["kitchen"], "destination": HA},
        {"id": "ha-all", "wake_word": "hey_jarvis", "destination": HA, "language": "pt-BR"},
        {"id": "ask", "wake_word": "*", "destination": LLM, "reply_to": "none"}]})
    (tmp_path / "rules.json").write_text(rules)
    (tmp_path / "wake_words.json").write_text(json.dumps({"words": [
        {"name": "hey_jarvis", "threshold": 0.6, "satellites": ["*"]},
        {"name": "alexa", "threshold": 0.5, "satellites": ["*"]}]}))
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        body = c.get("/satellites/wake-words").json()
    saved = json.loads((tmp_path / "wake_words.json").read_text())
    words = {w["name"]: w for w in saved["words"]}
    assert saved["version"] == 2
    assert (words["hey_jarvis"]["threshold"], words["hey_jarvis"]["language"],
            words["hey_jarvis"]["action"]["destination"]["type"]) == (0.6, "pt-BR", "ha_conversation")
    assert words["alexa"]["action"] == {"destination": LLM | {"system": None, "api_key_env": "SATELLITES_LLM_API_KEY",
                                                              "max_tokens": 400, "timeout": 30.0, "stream": True},
                                        "reply_to": "none", "voice": None, "fallback": None}
    assert saved["ptt"]["action"]["destination"]["type"] == "llm"
    assert body["words"][0]["mode"] == "command"
    assert (tmp_path / "rules.json").read_text() == rules  # left as it was
    assert "'kitchen-ha'" in caplog.text and "not carried over" in caplog.text


def test_a_rules_json_that_does_not_load_migrates_nothing(env, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(wakeword, "ensure_models", lambda names, model_dir, **kw: None)
    monkeypatch.setattr(wakeword, "WakeWords", MarkerWords)
    (tmp_path / "rules.json").write_text('{"version": 1, "rules": [{"id": ')
    before = json.dumps({"words": [{"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"]}]})
    (tmp_path / "wake_words.json").write_text(before)
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        body = c.get("/satellites/wake-words").json()
    assert "mode" not in body["words"][0]
    assert any("has no action" in w for w in body["warnings"])
    assert (tmp_path / "wake_words.json").read_text() == before


# ---- barge-in ------------------------------------------------------------------------------------


def korvo(far: np.ndarray, near: np.ndarray | None = None, *, clip_db: float = -20.0,
          seed: int = 3) -> np.ndarray:
    """What the Korvo sends while its speaker plays `far`: the loopback, then
    three microphones that hear the speaker (clipped at clip_db, as a small
    loudspeaker driven hard does, which the linear canceller cannot model)
    through a room, the talker `near` from 120 degrees, and their own noise."""
    played = dbfs(clip_db) * np.tanh(far / dbfs(clip_db))
    mics = echo(played, echo_paths() * 2.0)
    if near is not None:
        mics = mics + plane_wave(near, 120)
    mics = mics + sensor_noise(mics.shape, level=-62, seed=seed)
    return np.clip(np.rint(np.column_stack([far, mics.T])), -32768, 32767).astype(np.int16)


def frames(capture: np.ndarray, seq0: int = 0):
    for i, off in enumerate(range(0, len(capture) - FRAME + 1, FRAME)):
        yield struct.pack("<BBBBIQ", 1, 0, 4, 0, seq0 + i, 0) + capture[off:off + FRAME].tobytes()


def wav_samples(request: httpx.Request) -> np.ndarray:
    from email.parser import BytesParser
    from email.policy import HTTP
    head = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    msg = BytesParser(policy=HTTP).parsebytes(head + request.content)
    part = next(p for p in msg.iter_parts() if p.get_param("name", header="content-disposition") == "file")
    with wave.open(io.BytesIO(part.get_payload(decode=True))) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64)


@pytest.mark.parametrize("mode", ["conversation", "command"])
def test_speech_over_the_reply_stops_it_and_its_own_voice_does_not(
        client, app, events, services, plug, monkeypatch, mode):
    """THE SATELLITE MUST NOT INTERRUPT ITSELF, AND MUST LET A PERSON DO SO.

    The reply is 9 s of speech. For 5 s the satellite hears only itself:
    its voice on the loopback and, clipped by its loudspeaker and coloured by
    the room, on all three microphones. Nothing may stop the reply then --
    the canceller's first 2 s of playback included. Then someone talks over
    it at -30 dBFS: the speaker is flushed, the rest of the answer is
    cancelled, and in a conversation what they said, from its first
    syllable, is the next turn. In a command the reply only stops."""
    monkeypatch.setattr(app.hub.voice, "frontend", True)
    reply = speechlike(9, seed=1) * dbfs(-20)
    reply += np.random.default_rng(9).standard_normal(len(reply)) * dbfs(-50)
    services.tts_audio.append(np.frombuffer(audio.resample(
        np.clip(reply, -32768, 32767).astype("<i2").tobytes(), 16000, 24000), "<i2"))
    services.transcripts.extend(["tell me a long story", "stop, tell me a joke instead"])
    services.answers.extend([["Once upon a time there was a very long story."],
                             ["Why did the chicken cross the road?"]])
    interrupted = threading.Event()
    detected: list[float] = []
    real = app.Conversation.barge_in

    def spy(self, ev):
        detected.append(ev.at_s)
        return real(self, ev)
    monkeypatch.setattr(app.Conversation, "barge_in", spy)

    def note(item):
        if isinstance(item, dict) and item.get("type") == "flush":
            interrupted.set()
    quiet = korvo(np.zeros(int(1.0 * RATE)))
    user = speechlike(1.6, seed=31) * dbfs(-30)
    command = np.concatenate((korvo(np.zeros(len(user)), user), quiet))
    talker = speechlike(2.0, seed=21) * dbfs(-30)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "hey_jarvis"}, ptt={"mode": mode, "action": {"destination": LLM}})
        sat = plug(ws, answer=note)
        sat.send_frames(frames(quiet))
        ws.send_json({"type": "button", "button": "play", "action": "press"})
        sat.send_frames(frames(command, 100))
        wait(lambda: len(sat.speaker()) >= 5, what="the reply to start")
        s = app.hub.sessions[NID]
        # Five seconds of the satellite hearing itself, and nothing else.
        own = korvo(reply[:5 * RATE])
        start = s.ear.samples
        sat.send_frames(frames(own, 1000))
        wait(lambda: s.ear.samples >= start + len(own) - 2 * FRAME, what="the listener")
        time.sleep(0.2)
        assert not interrupted.is_set(), "the satellite's own voice stopped its reply"
        playing = len(sat.speaker())
        # Then someone talks over it, and stops.
        near = np.zeros(int(3.0 * RATE))
        onset = int(0.2 * RATE)
        near[onset:onset + len(talker)] = talker
        over = korvo(reply[5 * RATE:8 * RATE], near)
        before = s.ear.samples
        sat.send_frames(frames(np.concatenate((over, korvo(np.zeros(int(1.5 * RATE))))), 2000))
        assert interrupted.wait(10), "speech over the reply did not stop it"
        at_flush = len(sat.speaker())
        if mode == "conversation":
            wait(lambda: len(services.sent("stt.test", "/v1/audio/transcriptions")) == 2,
                 what="the interruption to be transcribed")
            wait(lambda: len(of(events, "turn")) == 2, what="the next turn")
        else:
            wait(lambda: of(events, "routed"), what="the routed event")
        time.sleep(0.4)
        after = len(sat.speaker()) - at_flush
    assert playing > 5
    first = (of(events, "turn") or of(events, "routed"))[0]
    assert first["interrupted"] is True and "interrupted by voice" in first["note"]
    assert after <= 30  # the second reply (0.2 s) at most; the 9 s reply was cut
    if mode == "conversation":
        captured = wav_samples(services.sent("stt.test", "/v1/audio/transcriptions")[1])
        loud = np.sqrt(np.mean(captured[int(0.5 * RATE):int(1.5 * RATE)] ** 2))
        lead = np.sqrt(np.mean(captured[:int(0.15 * RATE)] ** 2))
        # From its first syllable: the capture holds all 2 s of it, and starts
        # in the quiet just before it rather than inside the speech.
        assert len(captured) >= len(talker) and lead < 0.3 * loud
        second = of(events, "turn")[1]
        assert second["transcript"] == "stop, tell me a joke instead"
        assert llm_messages(services, 1)[-2][0] == "assistant"
    else:
        assert len(services.sent("stt.test", "/v1/audio/transcriptions")) == 1
    began = (before + onset + int(np.flatnonzero(talker)[0])) / RATE  # speechlike opens quiet
    print(f"\nbarge-in ({mode}): the talker's first syllable at {began:.2f} s of the stream, "
          f"detected at {detected[0]:.2f} s, {(detected[0] - began) * 1000:.0f} ms later")
