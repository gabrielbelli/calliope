"""Wake words assigned to satellites: which satellite hears which word, and
what changes live, without a restart.

The detector is WordFake, which fires a word on that word's own marker sample
(MARKS) and only for the words its clone was made with, as
WakeWords.clone(names) runs only those models. So "the bedroom did not hear
alexa" here means the hub never gave the bedroom's detector alexa, not that a
model happened to score low. The satellites are Starlette's test socket, and
STT and TTS are the *.test fakes from test_pipeline.py. ensure_models is a
recorder that can be held or made to fail, so a "download" is instant, slow or
broken on demand and nothing is fetched.

The tests at the end use the real openWakeWord models and the recorded "hey
jarvis", and skip with the reason when the models cannot be fetched.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import listening, wakeword, wakewords_config
from app.wakeword import Detection
from test_pipeline import (FIXTURES, FRAME, MAC, MAC2, NID, NID2, Satellite, Services, adopt,
                           floor, of, route_to_fakes, routed, voiced, wait, wav)

MARKS = {"hey_jarvis": 30000, "alexa": 29000, "hey_mycroft": 28000}


class WordFake:
    """WakeWords' interface: fires `name` on a frame of MARKS[name], for the
    names it was built or cloned with."""

    def __init__(self, models, model_dir=None, *, refractory_s=1.5):
        self.thresholds = dict(models)
        self.names = list(models)
        self.position = 0

    def clone(self, names=None):
        twin = copy.copy(self)  # the thresholds dict is shared, as in WakeWords
        twin.names = list(self.names if names is None else names)
        twin.position = 0
        return twin

    def reset(self):
        self.position = 0

    def feed(self, pcm):
        assert pcm.dtype == np.int16 and pcm.ndim == 1
        self.position += len(pcm)
        found = []
        for name in self.names:
            hits = np.flatnonzero(pcm == MARKS[name])
            if len(hits):
                found.append(Detection(name, 0.9, self.position - len(pcm) + int(hits[-1]) + 1))
        return found


def said(name: str) -> np.ndarray:
    """A wake word, a command, then the room."""
    mark = np.full(FRAME, MARKS[name], dtype=np.int16)
    return np.concatenate((floor(0.2), mark, voiced(1.0), floor(1.2, seed=1)))


class Fetcher:
    """ensure_models: records every name asked for; a name in `gates` waits
    for its event, and a name in `fail` raises."""

    def __init__(self):
        self.asked: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self.fail: dict[str, Exception] = {}

    def __call__(self, names, model_dir, **kw):
        for name in names:
            self.asked.append(name)
            if name in self.gates:
                assert self.gates[name].wait(10), f"{name} was never let through"
            if name in self.fail:
                raise self.fail[name]


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SATELLITES_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_jarvis:0.5")
    monkeypatch.setenv("SATELLITES_FRONTEND", "0")
    monkeypatch.delenv("SATELLITES_API_KEYS", raising=False)
    monkeypatch.delenv("SATELLITES_TTS_URL", raising=False)


@pytest.fixture
def fetcher(monkeypatch) -> Fetcher:
    f = Fetcher()
    monkeypatch.setattr(wakeword, "ensure_models", f)
    monkeypatch.setattr(wakeword, "WakeWords", WordFake)
    return f


@pytest.fixture
def services() -> Services:
    return Services()


@pytest.fixture
def app(env, fetcher):
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


def plug(app, ws, nid: str = NID) -> Satellite:
    def backlog() -> int:
        s = app.hub.sessions.get(nid)
        return s.mic.qsize() if s is not None else 0
    return Satellite(ws, backlog=backlog)


def send_all(app, satellite: Satellite, nid: str, audio: np.ndarray) -> None:
    """Send audio and return once the listener has run all of it through the
    satellite's Ear and handled what it heard, so the absence of a wake event
    afterwards means it was not heard, not that it was not processed yet."""
    ear = wait(lambda: app.hub.sessions[nid].ear, what="the listener")
    start = ear.samples
    satellite.send(audio)
    wait(lambda: app.hub.sessions[nid].ear.samples >= start + len(audio) // FRAME * FRAME,
         what="the listener to catch up")
    time.sleep(0.2)  # the events of the last batch are handled after it returns


def put(client, *words: dict):
    return client.put("/satellites/wake-words", json={"words": list(words)})


def word(name: str, *satellites: str, threshold: float = 0.5) -> dict:
    return {"name": name, "threshold": threshold, "satellites": list(satellites) or ["*"]}


def states(client) -> dict[str, str]:
    return {w["name"]: w["state"] for w in client.get("/satellites/wake-words").json()["words"]}


# ---- which satellite hears which word ------------------------------------------------


def test_a_word_assigned_to_one_satellite_does_not_wake_another(client, app, events):
    with client.websocket_connect("/satellites/ws") as ws1, \
            client.websocket_connect("/satellites/ws") as ws2:
        adopt(client, ws1, name="kitchen")
        adopt(client, ws2, name="bedroom", mac=MAC2)
        # The kitchen by its MAC as printed on the board: the same satellite.
        r = put(client, word("hey_jarvis", MAC), word("alexa", NID2))
        assert r.status_code == 200, r.text
        wait(lambda: states(client) == {"hey_jarvis": "ready", "alexa": "ready"}, what="alexa")
        kitchen, bedroom = plug(app, ws1), plug(app, ws2, NID2)
        send_all(app, bedroom, NID2, said("hey_jarvis"))
        send_all(app, kitchen, NID, said("alexa"))
        assert of(events, "wake") == []
        # Not only unheard but never run there: each satellite's detector is
        # a clone on its own words, so the kitchen's word costs the bedroom
        # nothing.
        detectors = {nid: app.hub.sessions[nid].ear.wake.names for nid in (NID, NID2)}
        bedroom.send(said("alexa"))
        [done] = routed(events)
        listed = {s["id"]: s["wake_words"] for s in client.get("/satellites").json()["satellites"]}
    [wake] = of(events, "wake")
    assert (wake["satellite"], wake["wake_word"]) == (NID2, "alexa")
    assert done["satellite"] == NID2 and done["error"] is None
    assert listed == detectors == {NID: ["hey_jarvis"], NID2: ["alexa"]}
    assert [w["satellites"] for w in r.json()["words"]] == [[NID], [NID2]]


def test_a_word_for_every_satellite_is_heard_on_one_adopted_after_it_was_set(client, app, events):
    assert put(client, word("alexa", "*")).status_code == 200
    wait(lambda: states(client) == {"alexa": "ready"}, what="alexa")
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.get(f"/satellites/{NID}").json()["wake_words"] == ["alexa"]
        plug(app, ws).send(said("alexa"))
        routed(events)
    assert [(e["satellite"], e["wake_word"]) for e in of(events, "wake")] == [(NID, "alexa")]


def test_a_satellite_with_no_wake_word_still_streams_and_talks_on_push_to_talk(client, app, events):
    assert put(client).status_code == 200
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(app, ws)
        send_all(app, satellite, NID, said("hey_jarvis"))
        assert of(events, "wake") == []
        ws.send_json({"type": "button", "button": "play", "action": "press"})
        satellite.send(np.concatenate((voiced(1.0), floor(1.2, seed=1))))
        [done] = routed(events)
        described = client.get(f"/satellites/{NID}").json()
    assert done["wake_word"] == "ptt" and done["error"] is None
    assert described["wake_words"] == [] and described["listening"]["wake_words"] is False


# ---- live changes ------------------------------------------------------------------------


def test_a_removed_word_is_not_acted_on_even_from_audio_already_being_processed(
        client, app, events, monkeypatch):
    """The batch that holds the wake word is in the thread pool, on the old
    detector, when the word is removed. What it heard must still be dropped:
    "removed" has to mean removed when the PUT answers, not one batch later,
    or a word taken off the bedroom at night can still wake it once."""
    real = listening.Ear.process
    entered, release = threading.Event(), threading.Event()
    armed = {"on": False}

    def held(self, frames):
        if armed["on"] and (frames[:, 1] == MARKS["hey_jarvis"]).any():
            armed["on"] = False
            entered.set()
            release.wait(10)
        return real(self, frames)
    monkeypatch.setattr(listening.Ear, "process", held)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(app, ws)
        satellite.send(said("hey_jarvis"))
        routed(events)  # the control: heard while it was assigned
        ear = app.hub.sessions[NID].ear
        start = ear.samples
        armed["on"] = True
        audio = said("hey_jarvis")
        sender = threading.Thread(target=satellite.send, args=(audio,), daemon=True)
        sender.start()
        assert entered.wait(10), "the wake word never reached the listener"
        assert put(client).status_code == 200
        release.set()
        sender.join(10)
        wait(lambda: app.hub.sessions[NID].ear.samples >= start + len(audio) // FRAME * FRAME,
             what="the listener to catch up")
        time.sleep(0.2)
        # And the next one is not heard either: the detector was rebuilt.
        send_all(app, satellite, NID, said("hey_jarvis"))
    assert len(of(events, "wake")) == 1 and len(of(events, "routed")) == 1


def test_a_threshold_change_reaches_a_listening_satellite_without_resetting_its_detector(
        client, app):
    """A new detector is deaf for its first 1.2 s (WakeWords' warm-up), so a
    slider moved on the page must not rebuild every satellite's."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(app, ws)
        send_all(app, satellite, NID, floor(0.2))
        detector = app.hub.sessions[NID].ear.wake
        assert put(client, word("hey_jarvis", threshold=0.8)).status_code == 200
        send_all(app, satellite, NID, floor(0.2))
        after = app.hub.sessions[NID].ear.wake
    assert after is detector and detector.thresholds["hey_jarvis"] == 0.8


def test_a_newly_named_word_downloads_in_the_background_and_is_then_heard_without_a_restart(
        client, app, events, fetcher):
    fetcher.gates["alexa"] = threading.Event()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(app, ws)
        r = put(client, word("hey_jarvis"), word("alexa"))
        answered = {w["name"]: w["state"] for w in r.json()["words"]}
        # Downloading holds nothing else up: the word that was ready still is.
        satellite.send(said("hey_jarvis"))
        routed(events)
        fetcher.gates["alexa"].set()
        wait(lambda: states(client) == {"hey_jarvis": "ready", "alexa": "ready"}, what="alexa")
        satellite.send(said("alexa"))
        routed(events, 2)
    assert r.status_code == 200 and answered == {"hey_jarvis": "ready", "alexa": "downloading"}
    assert [e["wake_word"] for e in of(events, "wake")] == ["hey_jarvis", "alexa"]
    assert fetcher.asked == ["hey_jarvis", "alexa"]  # the seed at start, then the new word
    # The page is told when the download ends rather than having to poll.
    # Only the event that names alexa: the start-up load of hey_jarvis
    # publishes one too, and the fixture's wait can see "ready" a moment
    # before that one is published, so it is recorded here now and then.
    [told] = [e for e in events if e["type"] == "wake_words"
              and any(w["name"] == "alexa" for w in e["words"])]
    assert {w["name"]: w["state"] for w in told["words"]} == {"hey_jarvis": "ready",
                                                              "alexa": "ready"}


def test_a_word_that_cannot_be_fetched_says_why_the_others_keep_working_and_a_save_retries(
        client, app, events, fetcher):
    fetcher.fail["alexa"] = RuntimeError("no route to github.com")
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        satellite = plug(app, ws)
        put(client, word("hey_jarvis"), word("alexa"))
        wait(lambda: states(client)["alexa"] == "error", what="alexa to fail")
        [alexa] = [w for w in client.get("/satellites/wake-words").json()["words"]
                   if w["name"] == "alexa"]
        health = client.get("/health").json()["voice"]
        satellite.send(said("hey_jarvis"))
        routed(events)
        fetcher.fail.clear()
        put(client, word("hey_jarvis"), word("alexa"))
        wait(lambda: states(client)["alexa"] == "ready", what="the retry")
    assert "no route to github.com" in alexa["error"]
    assert health["state"] == "ready" and "alexa" in health["error"]
    assert of(events, "wake")[0]["wake_word"] == "hey_jarvis"


def test_a_save_made_while_a_fetch_is_failing_retries_it_rather_than_taking_its_failure(
        client, monkeypatch):
    """The network comes back and the person saves again while the first
    fetch is still hanging. That failure used to land after the save had
    cleared it, and the word stayed in "error" until yet another save, which
    the page gives no reason to make."""
    attempts, hold = [], threading.Event()

    def flaky(names, model_dir, **kw):
        for name in names:
            attempts.append(name)
            if name == "alexa" and attempts.count("alexa") == 1:
                assert hold.wait(10), "the first fetch was never let go"
                raise RuntimeError("no route to github.com")
    monkeypatch.setattr(wakeword, "ensure_models", flaky)
    assert put(client, word("hey_jarvis"), word("alexa")).status_code == 200
    wait(lambda: "alexa" in attempts, what="the first fetch")
    assert put(client, word("hey_jarvis"), word("alexa", threshold=0.6)).status_code == 200
    hold.set()
    wait(lambda: states(client)["alexa"] != "downloading", what="the fetch to end")
    assert states(client)["alexa"] == "ready"
    assert attempts.count("alexa") == 2


# ---- what is refused -------------------------------------------------------------------------


@pytest.mark.parametrize("words,says", [
    ([word("hey_siri")], "not a wake word this hub can load"),
    ([word("alexa"), word("alexa", NID)], "listed twice"),
    ([word("alexa", threshold=0.05)], "from 0.1 to 0.95"),
    ([word("alexa", threshold=0.99)], "from 0.1 to 0.95"),
    ([word("alexa", "0123456789ab")], "not a satellite this hub knows"),
    ([word("alexa", "*", NID)], "already means every satellite"),
    ([word("ptt")], "push-to-talk"),
    ([word("../alexa")], "not a model name"),
], ids=["unknown-model", "twice", "too-low", "too-high", "unknown-satellite",
        "every-and-one", "push-to-talk", "path"])
def test_a_wake_word_setting_that_cannot_work_is_refused_with_the_reason_and_changes_nothing(
        client, tmp_path, words, says):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        before = (tmp_path / "wake_words.json").read_text()
        r = client.put("/satellites/wake-words", json={"words": words})
        after = client.get("/satellites/wake-words").json()["words"]
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "invalid_wake_words" and says in error["message"], error
    assert (tmp_path / "wake_words.json").read_text() == before
    assert [(w["name"], w["satellites"]) for w in after] == [("hey_jarvis", ["*"])]


def test_the_wake_word_routes_are_not_taken_for_a_satellite_called_wake_words(client):
    r = client.get("/satellites/wake-words")
    assert r.status_code == 200
    assert {"alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy"} <= set(r.json()["available"])


# ---- what survives -------------------------------------------------------------------------------


def test_the_assignment_survives_a_restart_of_the_hub(app, services, tmp_path):
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        adopt(c, ws)
        assert put(c, word("hey_jarvis", NID, threshold=0.7), word("alexa")).status_code == 200
    fresh = importlib.reload(app)
    with TestClient(fresh.app) as c:
        wait(lambda: fresh.hub.voice.state == "ready", what="the models")
        words = c.get("/satellites/wake-words").json()["words"]
    assert [(w["name"], w["threshold"], w["satellites"], w["state"]) for w in words] == [
        ("hey_jarvis", 0.7, [NID], "ready"), ("alexa", 0.5, ["*"], "ready")]
    assert json.loads((tmp_path / "wake_words.json").read_text()) == {"words": [
        {"name": "hey_jarvis", "threshold": 0.7, "satellites": [NID]},
        {"name": "alexa", "threshold": 0.5, "satellites": ["*"]}]}


def test_the_first_start_seeds_every_word_for_every_satellite_and_a_later_start_does_not(
        env, fetcher, monkeypatch, tmp_path):
    """SATELLITES_WAKE_WORDS meant "these words on every satellite" before
    words could be assigned, so that is what it seeds. After that the file
    decides: a variable read at every start would undo what the page saved."""
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_jarvis:0.6,alexa")
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        first = c.get("/satellites/wake-words").json()["words"]
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_mycroft")
    app = importlib.reload(app)
    with TestClient(app.app) as c:
        second = c.get("/satellites/wake-words").json()["words"]
    assert [(w["name"], w["threshold"], w["satellites"]) for w in first] == [
        ("hey_jarvis", 0.6, ["*"]), ("alexa", 0.5, ["*"])]
    assert [w["name"] for w in second] == ["hey_jarvis", "alexa"]


def test_a_seeded_threshold_a_person_could_not_set_is_brought_inside_the_range():
    """Written as it was, every later save from the page would be refused
    over a word nobody touched."""
    words = wakewords_config.seed_words("hey_jarvis:0.99,alexa:0.02")
    assert [(w.name, w.threshold, w.satellites) for w in words] == [
        ("hey_jarvis", 0.95, ["*"]), ("alexa", 0.1, ["*"])]


def test_forgetting_a_satellite_takes_it_off_every_word(client, tmp_path):
    with client.websocket_connect("/satellites/ws") as ws1, \
            client.websocket_connect("/satellites/ws") as ws2:
        adopt(client, ws1, name="kitchen")
        adopt(client, ws2, name="bedroom", mac=MAC2)
        put(client, word("hey_jarvis", NID, NID2), word("alexa", NID))
        assert client.post(f"/satellites/{NID}/forget").status_code == 204
        words = client.get("/satellites/wake-words").json()["words"]
    assert [(w["name"], w["satellites"]) for w in words] == [("hey_jarvis", [NID2]),
                                                             ("alexa", [])]
    saved = json.loads((tmp_path / "wake_words.json").read_text())["words"]
    assert [w["satellites"] for w in saved] == [[NID2], []]


def test_what_get_returned_can_be_sent_back_after_a_restart_that_forgot_who_was_seen(
        app, tmp_path):
    """A word assigned to a satellite that is pending, and not connected after
    a restart, names an id the hub has not seen since it started. Sending the
    page's list back unchanged must not be refused for that."""
    with TestClient(app.app) as c, c.websocket_connect("/satellites/ws") as ws:
        ws.send_json({"type": "hello", "id": MAC2, "model": "esp32-korvo-v1.1", "fw": "v1",
                      "token": "", "caps": {}})
        assert ws.receive_json() == {"type": "pending"}
        assert put(c, word("hey_jarvis", NID2)).status_code == 200
    fresh = importlib.reload(app)
    with TestClient(fresh.app) as c:
        got = c.get("/satellites/wake-words").json()
        again = c.put("/satellites/wake-words", json={"words": got["words"]})
    assert again.status_code == 200, again.text
    assert again.json()["words"][0]["satellites"] == [NID2]


def test_a_wake_words_file_that_does_not_load_turns_them_off_says_why_and_a_save_fixes_it(
        env, fetcher, tmp_path):
    """Not back to the seed: that would bring back words someone removed."""
    (tmp_path / "wake_words.json").write_text('{"words": [{"name": "hey_jarvis", "threshold": 7')
    app = importlib.reload(importlib.import_module("app.main"))
    with TestClient(app.app) as c:
        broken = c.get("/satellites/wake-words").json()
        health = c.get("/health").json()["voice"]
        fixed = put(c, word("alexa"))
    assert broken["words"] == [] and "could not be loaded" in broken["load_error"]
    assert health["state"] == "off" and "could not be loaded" in health["error"]
    assert fixed.status_code == 200 and fixed.json()["load_error"] is None
    assert [w["name"] for w in fixed.json()["words"]] == ["alexa"]


def test_available_names_the_built_in_words_and_your_own_but_no_feature_model(tmp_path):
    for name in ("my_word.onnx", "melspectrogram.onnx", "embedding_model.onnx",
                 "hey_jarvis_v0.1.onnx", "ptt.onnx", ".alexa_v0.1.onnx.123.part", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    assert wakewords_config.available(tmp_path) == sorted([*wakeword.MODELS, "my_word"])
    assert wakewords_config.available(tmp_path / "missing") == sorted(wakeword.MODELS)


# ---- inject ---------------------------------------------------------------------------------------


def test_inject_listens_for_the_satellites_own_words(client):
    with client.websocket_connect("/satellites/ws") as ws1, \
            client.websocket_connect("/satellites/ws") as ws2:
        adopt(client, ws1, name="kitchen")
        adopt(client, ws2, name="bedroom", mac=MAC2)
        put(client, word("alexa", NID), word("hey_jarvis", NID2))
        wait(lambda: states(client)["alexa"] == "ready", what="alexa")
        other = client.post(f"/satellites/{NID}/inject", content=wav(said("hey_jarvis")))
        own = client.post(f"/satellites/{NID}/inject", content=wav(said("alexa")))
        put(client, word("hey_jarvis", NID2))
        none = client.post(f"/satellites/{NID}/inject", content=wav(said("alexa")))
        ptt = client.post(f"/satellites/{NID}/inject", params={"wake_word": "ptt"},
                          content=wav(np.concatenate((voiced(1.0), floor(0.3)))))
    assert other.status_code == 200 and other.json()["heard"] is None
    assert own.json()["heard"]["wake_word"] == "alexa"
    assert none.status_code == 409 and none.json()["error"]["code"] == "no_wake_words"
    assert ptt.status_code == 200 and ptt.json()["heard"]["wake_word"] == "ptt"


# ---- with the real models --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory) -> Path:
    pytest.importorskip("openwakeword", reason="openwakeword is not installed")
    d = Path(os.environ.get("SATELLITES_TEST_WAKEWORD_DIR") or tmp_path_factory.mktemp("wakewords"))
    try:
        wakeword.ensure_models(["hey_jarvis", "alexa"], d)
    except httpx.HTTPError as e:
        pytest.skip(f"openWakeWord models could not be fetched from GitHub (offline?): {e!r}")
    return d


def recorded(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int16)


def room_with(clip: np.ndarray) -> np.ndarray:
    y = floor(1.0 + len(clip) / 16000 + 1.0).astype(np.int32)
    y[16000:16000 + len(clip)] += clip
    return np.clip(y, -32768, 32767).astype(np.int16)


def test_a_clone_on_a_subset_scores_what_a_fresh_instance_of_those_models_scores(model_dir):
    """clone(names) leans on openwakeword 0.6.0's Model.models, so it is held
    to the one thing that matters: the same audio gives the same detections
    as a stream that loaded only those models, and a stream without the word
    does not fire on it."""
    audio = room_with(recorded("hey_jarvis_en_us.wav"))
    base = wakeword.WakeWords({"hey_jarvis": 0.5, "alexa": 0.5}, model_dir)

    def feed(ww):
        return [d for off in range(0, len(audio), 320) for d in ww.feed(audio[off:off + 320])]
    fresh = feed(wakeword.WakeWords({"hey_jarvis": 0.5}, model_dir))
    subset = base.clone(["hey_jarvis"])
    heard = feed(subset)
    assert fresh and [(d.name, d.sample) for d in heard] == [(d.name, d.sample) for d in fresh]
    assert [d.score for d in heard] == pytest.approx([d.score for d in fresh], abs=1e-6)
    assert feed(base.clone(["alexa"])) == []
    assert subset.names == ["hey_jarvis"] and base.names == ["hey_jarvis", "alexa"]
    # Only its session runs on the subset's frames, which is the point of a
    # subset: Model.predict runs every session in Model.models.
    assert list(subset._model.models) == ["hey_jarvis_v0.1"]
    assert list(base._model.models) == ["hey_jarvis_v0.1", "alexa_v0.1"]
    with pytest.raises(ValueError):
        base.clone(["hey_mycroft"])


def test_a_recorded_hey_jarvis_is_heard_only_on_the_satellite_it_is_assigned_to(
        env, model_dir, monkeypatch, services, tmp_path):
    monkeypatch.setenv("SATELLITES_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "hey_jarvis:0.5,alexa:0.5")
    app = importlib.reload(importlib.import_module("app.main"))
    clip = wav(recorded("hey_jarvis_en_us.wav"))
    with TestClient(app.app) as c:
        route_to_fakes(app, services, tmp_path)
        wait(lambda: app.hub.voice.state in ("ready", "failed"), timeout=60, what="the models")
        assert app.hub.voice.state == "ready", app.hub.voice.error
        with c.websocket_connect("/satellites/ws") as ws1, \
                c.websocket_connect("/satellites/ws") as ws2:
            adopt(c, ws1, name="kitchen")
            adopt(c, ws2, name="bedroom", mac=MAC2)
            assert put(c, word("hey_jarvis", NID), word("alexa", NID2)).status_code == 200
            bedroom = c.post(f"/satellites/{NID2}/inject", content=clip).json()
            kitchen = c.post(f"/satellites/{NID}/inject", content=clip).json()
    assert bedroom["heard"] is None
    assert kitchen["heard"]["wake_word"] == "hey_jarvis" and kitchen["heard"]["score"] >= 0.5
    assert kitchen["outcome"]["transcript"] == "what time is it"
