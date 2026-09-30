"""The double-check of a wake word (verify.py, Hub.on_wake): STT is given the
audio that held the word, told to listen for it, and the hub answers only
when the word is in what it heard.

The matcher is tested on its own, on what STT wrote for real wakes on 29 and
30 Sep 2026 and for a Portuguese video that woke "alexa". The rest runs through the
hub as test_conversation.py does: a satellite on the test socket, a wake word
as a marker sample (MarkerWords), and a fake STT whose answer to the wake
word's audio a test chooses (Services.check). That is how a TV is made to say
"Obrigado." where the model heard "alexa". Nothing calls a real STT, and
nothing plays.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import wave
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
import test_conversation
import test_pipeline
from test_conversation import conversation, mark, save, settled
from test_listening import MARK, Marker, run
from test_pipeline import EARCON_CAPS, NID, RATE, EarconStore, adopt, floor, of, voiced, wait
from test_router import PARAKEET, multipart

from app import telemetry, verify
from app.listening import VERIFY_WINDOW_S, Ear, room_floor

# test_conversation's fixtures: its hub hears alexa, hey_jarvis and lumos.
env = test_pipeline.env
app, client, events, plug, services = (test_conversation.app, test_conversation.client,
                                       test_conversation.events, test_conversation.plug,
                                       test_conversation.services)

ECHO = {"destination": {"type": "echo"}}
ALEXA_ON = {"name": "alexa", "action": ECHO, "verify": {"mode": "on"}}
SEEN = ("earcon", "duck", "lights")   # what a satellite is sent that someone would notice


def answering(text: str | None = None, gate: threading.Event | None = None, delay: float = 0.0):
    """Services.check: STT's answer to a wake word's audio, `text` or the
    word itself, once `gate` is open and after `delay`."""
    async def check(request: httpx.Request, said: str) -> httpx.Response:
        while gate is not None and not gate.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"text": said if text is None else text},
                              headers={"x-stt-engine": "parakeet"})
    return check


def said(word: str, seed: int = 1) -> np.ndarray:
    """The word, a command, then the room."""
    return np.concatenate((floor(0.2, seed=seed), mark(word), voiced(0.8), floor(1.2, seed=seed + 1)))


def noticed(sat) -> list[dict]:
    return [m for m in sat.texts() if m["type"] in SEEN]


def wakes(app, n: int) -> list[dict]:
    """The first n wake records, once they are written."""
    return wait(lambda: len(app.hub.telemetry.read(kinds={"wake"})) >= n
                and app.hub.telemetry.read(kinds={"wake"}), what=f"{n} wake records")


def telemetry_on(client, level: str = "full") -> None:
    assert client.put("/satellites/telemetry", json={"enabled": True, "level": level}).status_code == 200


# ---- the matcher --------------------------------------------------------------------------


@pytest.mark.parametrize("transcript,word,heard", [
    # A real "hey jarvis" on 30 Sep 2026: 0.67 of "jarvis" by its letters,
    # and T R F S by its sounds, as "jarvis" is.
    ("Hey hey Dervis.", "hey_jarvis", True),
    ("Hey Jarvis", "hey_jarvis", True),
    ("Hey, Jarvis.", "hey_jarvis", True),
    ("Hey, Jervis", "hey_jarvis", True),
    ("Hey Travis", "hey_jarvis", True),
    ("Hey Javis", "hey_jarvis", True),
    ("Ei Jarvis", "hey_jarvis", True),
    ("Alexa.", "alexa", True),
    ("Alécia,", "alexa", True),
    ("Aleksa, liga a luz", "alexa", True),
    ("Alessa", "alexa", True),
    ("Alexia", "alexa", True),
    ("Lumos", "lumos", True),
    ("Lumus", "lumos", True),
    ("Lúmos", "lumos", True),
    ("Hey Claude", "hey_claude", True),
    ("Hey Cloud", "hey_claude", True),
    ("Hey Claud", "hey_claude", True),   # 0.91 of "claude", which is not short
    ("Hey Clod", "hey_claude", True),
    ("Hey Grok", "hey_grok", True),
    ("Hey Groc", "hey_grok", True),
    ("chatgpt", "hey_chat_gpt", True),
    ("Hey Chat GPT", "hey_chat_gpt", True),
    ("Chat GPT, what is new", "hey_chat_gpt", True),
    # What a Portuguese video said where the model heard "alexa", and
    # Portuguese that is close to it by its letters or its sounds.
    ("Obrigado.", "alexa", False),
    ("De manipular.", "alexa", False),
    ("deixa", "alexa", False),
    ("a lei já", "alexa", False),
    ("relaxa", "alexa", False),
    ("a caixa", "alexa", False),
    ("puxa vida", "alexa", False),
    ("que horas são", "alexa", False),
    ("tudo bem", "alexa", False),
    # 0.8 of "alessa" and of "alexia" by their letters, but two letters
    # shorter: "essa" was 1 in 100 sentences of everyday Portuguese.
    ("essa", "alexa", False),
    ("Leia isto", "alexa", False),
    ("less is more", "alexa", False),
    # Everyday words one letter away from a short spelling (SHORT_SPELLING),
    # or with all the sounds of one too short to be told by them (SOUND_MIN).
    ("I could do that", "hey_claude", False),
    ("so loud", "hey_claude", False),
    ("the rock", "hey_grok", False),
    # One sound short of "jarvis": T F S, and T R S.
    ("Davis", "hey_jarvis", False),
    ("três", "hey_jarvis", False),
    ("atrás", "hey_jarvis", False),
    ("dress", "hey_jarvis", False),
    # One sound changed, what one changed sound let through most often in
    # everyday Portuguese and English: T L F S, T L F S, T R T S and so on.
    ("Talvez amanhã", "hey_jarvis", False),
    ("Vi na televisão", "hey_jarvis", False),
    ("direitos humanos", "hey_jarvis", False),
    ("Os jornais de hoje", "hey_jarvis", False),
    ("Os termos do acordo", "hey_jarvis", False),
    ("I love to travel", "hey_jarvis", False),
    ("the girls", "hey_jarvis", False),
    ("sweet dreams", "hey_jarvis", False),
    ("magic tricks", "hey_jarvis", False),
    ("traffic", "hey_jarvis", False),
    # All of "jarvis"'s sounds, but a vowel before them, or too far from it
    # by its letters.
    ("através da porta", "hey_jarvis", False),
    ("otherwise", "hey_jarvis", False),
    ("as tarefas", "hey_jarvis", False),
    ("as tarifas", "hey_jarvis", False),
    ("tariffs", "hey_jarvis", False),
    *[("", word, False) for word in verify.SPELLINGS],
])
def test_a_word_is_heard_as_stt_spells_it_or_as_it_sounds_and_not_in_everyday_words(transcript, word, heard):
    assert (verify.matches(transcript, verify.spellings(word)) is not None) is heard


def test_a_spelling_sounds_as_its_consonants_do_one_class_a_sound():
    """What differs between the ways STT writes one name is mostly its
    vowels, and a d for a j. Three sounds are everyday words as well, which
    is why a spelling needs SOUND_MIN of them to be heard by them."""
    assert verify.sounds("jarvis") == verify.sounds("dervis") == verify.sounds("travis") == "TRFS"
    assert verify.sounds("alexa") == verify.sounds("aleksa") == verify.sounds("alecsa") == "LKS"
    assert verify.sounds("chat gpt") == verify.sounds("chatgpt") == verify.sounds("chad gbt") == "XTKPT"
    assert verify.sounds("Caça") == verify.sounds("cassa") == "KS"
    assert verify.sounds("ação") == verify.sounds("asão") == "S"
    assert [verify.sounds(w) for w in ("gente", "gato", "filha", "phone", "chuva", "hey")] == [
        "TMT", "KT", "FL", "FM", "XF", ""]
    assert verify.sounds("could") == verify.sounds("claude") == "KLT"
    assert verify.sounds("lucas") == verify.sounds("alexa")


def test_a_long_spelling_is_heard_by_all_its_sounds_from_its_first_and_near_it_by_its_letters():
    jarvis = verify.spellings("hey_jarvis")
    assert verify.matches("Hey Travis", jarvis) == "jarvis"      # T R F S, 0.67
    assert verify.matches("Hey Jarbas", jarvis) is None          # T R P S
    assert verify.matches("Hey Carvas", jarvis) is None          # K R F S
    assert verify.matches("Hey drivers", jarvis) is None         # T R F R S
    assert verify.matches("através", jarvis) is None             # T R F S after a vowel
    assert verify.matches("tarefas", jarvis) is None             # T R F S, 0.62 of "jarves"
    assert verify.matches("Chad GPS", verify.spellings("hey_chat_gpt")) is None
    # What it costs: "drives" is T R F S and 0.67 of "jarves", as "Dervis"
    # is of "jarvis", in 5 of 31,976 everyday English sentences.
    assert verify.matches("She drives to work", jarvis) == "jarves"
    # Three sounds are not told by their sounds at all: "Lucas" is "alexa"'s L K S.
    assert verify.matches("Lucas", verify.spellings("alexa")) is None


def test_stt_is_told_the_words_name_as_a_transcript_writes_it_and_its_own_spellings_as_typed():
    """Not the built-in misspellings: matches() takes those anyway, and
    STT told to listen for them would be told to get the word wrong. A
    comma is a space, which stt-stack would otherwise read as two terms."""
    assert verify.vocabulary("hey_jarvis") == ["Jarvis"]
    assert verify.vocabulary("hey_chat_gpt") == ["Chat GPT"]
    assert verify.vocabulary("alexa_ptbr", ["Hey Lexa", "alexa", "Aléxia"]) == ["Alexa", "Lexa", "Aléxia"]
    assert verify.vocabulary("hey_mycroft") == ["Mycroft"]
    assert verify.vocabulary("ok_nabu", ["Hey, Nabu!", "nah, boo"]) == ["Nabu", "nah boo"]
    # Nor a "hey" on its own, which is in every "hey jarvis", nor the
    # version a model's file name carries.
    assert verify.vocabulary("hey_jarvis", ["hey", "OK", "Hey!"]) == ["Jarvis"]
    assert verify.vocabulary("hey_nabu_v2") == ["Nabu"]


def test_a_words_own_spellings_count_too_and_any_word_is_its_own_name():
    """A custom model has no built-in spellings: its name is one, without
    its "hey", and the word's verify.spellings add what STT was seen to
    write. alexa_ptbr is spelt as alexa is."""
    assert verify.spellings("hey_mycroft") == ["mycroft"]
    assert verify.spellings("ok_nabu", ["Hey Nabu", "nah boo"]) == ["nabu", "nah boo"]
    assert verify.matches("Nah, boo!", verify.spellings("ok_nabu")) is None
    assert verify.matches("Nah, boo!", verify.spellings("ok_nabu", ["nah boo"])) == "nah boo"
    assert verify.spellings("alexa_ptbr") == verify.spellings("alexa")
    # A "hey" alone would match any wake, and the "v2" of a file name is not said.
    assert verify.spellings("hey_jarvis", ["hey", "ok"]) == verify.spellings("hey_jarvis")
    assert verify.matches("Hey, what's up?", verify.spellings("hey_jarvis", ["hey"])) is None
    assert verify.spellings("hey_nabu_v2") == ["nabu"]
    # A word named nothing but "hey" still has its name.
    assert verify.spellings("hey") == ["hey"]


# ---- the Ear ------------------------------------------------------------------------------


def test_the_ear_hands_the_wake_word_audio_with_a_wake():
    """What the model scored, up to where it fired: the word's last sample
    is the clip's last, and the command after it is not in it."""
    ear = Ear(channels=1, frontend=False, wake=Marker())
    before = room_floor(2.51)
    # The marker ends mid-chunk (40160 of 40000-40320), so the clip is cut
    # inside process().
    audio = np.concatenate((before[:-10], np.full(10, MARK, np.int16), voiced(1.0),
                            room_floor(1.2, seed=1)))
    heard = run(ear, audio)[0]
    clip = np.frombuffer(heard.clip, "<i2")
    assert len(clip) == VERIFY_WINDOW_S * RATE == 2.0 * ear.rate
    assert (clip[-10:] == MARK).all()
    np.testing.assert_array_equal(clip[:-10], before[-len(clip):-10])

    # A trigger's clip ends where its chunk does, and holds it too.
    ear = Ear(channels=1, frontend=False, wake=Marker())
    ear.triggers = frozenset({"hey_jarvis"})
    heard = run(ear, np.concatenate((before[:-10], np.full(10, MARK, np.int16), room_floor(0.1))))[0]
    assert len(heard.clip) == 2 * VERIFY_WINDOW_S * RATE and np.frombuffer(heard.clip, "<i2").max() == MARK

    # Push-to-talk has nothing to check.
    ear = Ear(channels=1, frontend=False, wake=None)
    ear.push_to_talk()
    assert run(ear, np.concatenate((voiced(0.8), room_floor(1.2))))[0].clip is None


# ---- through the hub ----------------------------------------------------------------------


def test_a_word_on_on_is_answered_only_after_the_stt_hears_it(client, app, events, services, plug):
    """Until STT has heard the word, nothing: no earcon, duck, ring or
    conversation, while the Ear captures the command. Heard, the
    conversation starts and takes that command. Not heard (a TV saying
    "Obrigado."), the wake is dropped with its command, silently, and the
    satellite listens for its wake words again."""
    gate = threading.Event()
    services.check = answering(gate=gate)
    store = EarconStore()
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, **EARCON_CAPS)
        sat = plug(ws, answer=store)
        wait(lambda: len(store.files) == 3 and store.putting is None, what="the earcons")
        save(client, ALEXA_ON)
        telemetry_on(client)
        sat.send(said("alexa"))
        wait(lambda: services.checks, what="the check")
        time.sleep(0.4)  # the command has ended meanwhile
        assert noticed(sat) == [] and of(events, "wake") == [] and sat.speaker() == []
        gate.set()
        [done] = wait(lambda: of(events, "routed"), what="the answer")
        wait(lambda: app.hub.sessions[NID].conversation is None, what="the end")
        answered, played = len(noticed(sat)), len(sat.speaker())

        services.check = answering("Obrigado.")
        sat.send(said("alexa", seed=3))
        [rejected] = wait(lambda: of(events, "wake_rejected"), what="the rejection")
        time.sleep(0.5)
        ignored = noticed(sat)[answered:]
        assert len(sat.speaker()) == played

        services.check = answering()
        sat.send(said("alexa", seed=5))
        wait(lambda: len(of(events, "routed")) == 2, what="the next wake word")
    assert [m["id"] for m in sat.texts("earcon")] == ["wake", "wake"]  # the first and the third
    assert done["wake_word"] == "alexa" and done["transcript"] == "what time is it"
    assert ignored == [] and len(of(events, "wake")) == 2
    # The rejected wake's command went nowhere: two commands transcribed.
    assert len(services.sent("stt.test", "/v1/audio/transcriptions")) == 2
    assert {k: rejected[k] for k in ("satellite", "word", "score", "heard", "mode")} == {
        "satellite": NID, "word": "alexa", "score": 0.9, "heard": "Obrigado.", "mode": "on"}
    first, second, _ = wakes(app, 3)
    assert first["decision"] == "started" and second["decision"] == "rejected"
    assert {k: first["verify"][k] for k in ("mode", "decision", "heard", "matched")} == {
        "mode": "on", "decision": "accepted", "heard": "Alexa.", "matched": "alexa"}
    assert {k: second["verify"][k] for k in ("mode", "decision", "heard", "matched")} == {
        "mode": "on", "decision": "rejected", "heard": "Obrigado.", "matched": None}
    assert all(r["verify"]["ms"] >= 0 for r in (first, second))


def test_record_only_never_blocks_and_says_what_it_would_have_done(client, app, events, services, plug):
    """Every word's default: the wake goes ahead at once, STT or no STT, and
    what "on" would have done is published and recorded beside it."""
    gate = threading.Event()
    services.check = answering("Obrigado.", gate)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "alexa", "action": ECHO})
        telemetry_on(client)
        sat = plug(ws)
        sat.send(said("alexa"))
        [done] = wait(lambda: of(events, "routed"), what="the answer, with STT still thinking")
        assert of(events, "wake_rejected") == [] and not gate.is_set()
        gate.set()
        [would] = wait(lambda: of(events, "wake_rejected"), what="what it would have done")
    assert done["error"] is None and done["played"] is True
    assert (would["mode"], would["heard"]) == ("log", "Obrigado.")
    [record] = wakes(app, 1)
    assert record["decision"] == "started"
    assert {k: record["verify"][k] for k in ("mode", "decision", "matched")} == {
        "mode": "log", "decision": "would_reject", "matched": None}


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_a_failing_stt_lets_the_wake_through(client, app, events, services, plug, monkeypatch, failure):
    """The model has already fired: an STT that fails, or takes longer than
    VERIFY_TIMEOUT_S, must not silence a satellite set to "on"."""
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT_S", 0.3)
    services.check = (answering(delay=3.0) if failure == "timeout"
                      else lambda request, said: httpx.Response(500, text="the engine fell over"))
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON)
        telemetry_on(client)
        plug(ws).send(said("alexa"))
        [done] = wait(lambda: of(events, "routed"), what="the answer")
    [record] = wakes(app, 1)
    assert done["error"] is None and of(events, "wake_rejected") == []
    assert (record["decision"], record["verify"]["decision"], record["verify"]["heard"]) == (
        "started", "error", None)
    assert record["verify"]["ms"] < 3000


def test_off_never_calls_the_stt(client, app, events, services, plug):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, {"name": "alexa", "action": ECHO, "verify": {"mode": "off"}})
        telemetry_on(client)
        plug(ws).send(said("alexa"))
        wait(lambda: of(events, "routed"), what="the answer")
    [record] = wakes(app, 1)
    assert services.checks == [] and record["verify"] is None


def test_push_to_talk_is_never_checked(client, app, events, services, plug):
    """A button is never a TV: push-to-talk, and Home Assistant's naming a
    word that is set to "on", start at once with no STT before them."""
    services.check = answering("Obrigado.")
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON, ptt={"mode": "command", "action": ECHO, "verify": {"mode": "on"}})
        sat = plug(ws)
        assert client.post(f"/satellites/{NID}/ptt", json={"wake_word": "alexa"}).status_code == 204
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=1))))
        wait(lambda: of(events, "routed"), what="the first")
        wait(lambda: app.hub.sessions[NID].conversation is None, what="its end")
        ws.send_json({"type": "button", "button": "play", "action": "press"})
        time.sleep(0.1)
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=2))))
        wait(lambda: len(of(events, "routed")) == 2, what="the second")
    assert services.checks == [] and of(events, "wake_rejected") == []
    assert [e["wake_word"] for e in of(events, "routed")] == ["alexa", "ptt"]


def test_a_second_wake_during_a_check_supersedes_it(client, app, events, services, plug):
    """Two wakes while the first is still being checked: the first one's
    result is recorded and not acted on, and the second one, heard, takes
    the satellite from the conversation that was waiting there."""
    gate = threading.Event()
    services.check = lambda request, said: answering(gate=None if said == "Hey Jarvis." else gate)(request, said)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=30), ALEXA_ON)
        telemetry_on(client)
        sat = plug(ws)
        sat.send(said("hey_jarvis"))
        settled(app)
        sat.send(np.concatenate((floor(0.2), mark("alexa"), floor(0.3, seed=7), mark("alexa"),
                                 voiced(0.8), floor(1.2, seed=8))))
        wait(lambda: len(services.checks) == 3, what="both checks")
        time.sleep(0.3)
        gate.set()
        wait(lambda: len(of(events, "routed")) == 1, what="the second wake's answer")
        [ended] = wait(lambda: of(events, "conversation_ended"), what="the conversation it took over")
    # Both checks answer at once, in either order.
    records = wakes(app, 3)
    assert sorted((r["word"], r["decision"]) for r in records) == [
        ("alexa", "interrupted"), ("alexa", "superseded"), ("hey_jarvis", "started")]
    assert [r["verify"]["decision"] for r in records] == ["accepted"] * 3
    assert [e["wake_word"] for e in of(events, "wake")] == ["hey_jarvis", "alexa"]
    assert of(events, "routed")[0]["wake_word"] == "alexa" and ended["reason"] == "wake_word"


def test_push_to_talk_waits_for_a_wake_word_being_checked(client, app, events, services, plug):
    """The Ear is capturing what followed the word, for the word: the
    button is refused as busy meanwhile, and taken once the check is done
    with."""
    gate = threading.Event()
    services.check = answering(gate=gate)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON)
        sat = plug(ws)
        sat.send(said("alexa"))
        wait(lambda: services.checks, what="the check")
        busy = client.post(f"/satellites/{NID}/ptt", json={"wake_word": "alexa"})
        gate.set()
        wait(lambda: of(events, "routed"), what="the wake word's answer")
        wait(lambda: app.hub.sessions[NID].conversation is None, what="its end")
        assert client.post(f"/satellites/{NID}/ptt", json={"wake_word": "alexa"}).status_code == 204
        sat.send(np.concatenate((voiced(0.8), floor(1.2, seed=2))))
        wait(lambda: len(of(events, "routed")) == 2, what="push-to-talk's answer")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "satellite_busy")
    assert len(services.checks) == 1


@pytest.mark.parametrize("stt,decision", [(None, "accepted"), ("Obrigado.", "rejected")])
@pytest.mark.parametrize("how", ["stop", "fault"])
def test_a_wake_word_being_checked_goes_with_the_stop_button_or_a_failed_listener(
        client, app, events, services, plug, monkeypatch, how, stt, decision):
    """Stop pressed, or the listener starting again after a fault, while STT
    is still thinking: whatever STT then says, the wake is recorded as
    superseded and nothing else comes of it. No conversation (a fresh Ear
    after a fault has no command to give one), no wake_rejected and no
    clip, and the satellite listens for its wake words again."""
    gate = threading.Event()
    services.check = answering(stt, gate)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON)
        telemetry_on(client)
        sat = plug(ws)
        sat.send(said("alexa"))
        wait(lambda: services.checks, what="the check")
        session = app.hub.sessions[NID]
        assert session.checking is not None
        if how == "stop":
            assert client.post(f"/satellites/{NID}/flush").status_code == 204
        else:
            real, failed = Ear.process, []

            def process(self, frames):
                if not failed:
                    failed.append(self)
                    raise RuntimeError("the front-end fell over")
                return real(self, frames)
            monkeypatch.setattr(Ear, "process", process)
            sat.send(floor(0.1, seed=4))
            wait(lambda: failed and session.ear is not failed[0], what="the listener starting again")
        assert session.checking is None
        gate.set()
        [record] = wakes(app, 1)
        time.sleep(0.3)
        assert session.conversation is None and of(events, "wake") == []
        services.check = answering()
        sat.send(said("alexa", seed=3))
        wait(lambda: of(events, "routed"), what="the next wake word's answer")
    assert (record["decision"], record["verify"]["decision"]) == ("superseded", decision)
    assert record["verify"]["clip"] is None and of(events, "wake_rejected") == []
    assert client.get("/satellites/telemetry").json()["clips"] == {"count": 0, "bytes": 0}
    assert len(of(events, "wake")) == 1  # the second wake word's


def test_the_check_does_not_leave_its_transcription_running_for_the_routers_timeout(
        client, app, events, services, plug):
    """Every word is checked by default: a transcription the hub has stopped
    waiting for must not hold a slow STT for 30 s ahead of the commands."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON)
        plug(ws).send(said("alexa"))
        wait(lambda: of(events, "routed"), what="the answer")
    [check] = services.checks
    assert check.extensions["timeout"]["read"] == verify.VERIFY_TIMEOUT_S
    [command] = services.sent("stt.test", "/v1/audio/transcriptions")
    assert command.extensions["timeout"]["read"] == 30.0


def test_the_check_tells_stt_to_listen_for_the_word_and_the_command_is_not_told(
        client, app, events, services, plug):
    """On a stack that boosts, the check's transcription carries the word's
    name and its own spellings (verify.vocabulary) for that transcription
    alone: the command after it has Home Assistant's names, boosted as
    ever, and nothing of the word's."""
    stt = services.handlers["stt.test"]

    async def parakeet(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "hotwords": True, "models": [PARAKEET]})
        return await stt(request)
    services.handlers["stt.test"] = parakeet
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON | {"verify": {"mode": "on", "spellings": ["Hey Lexa", "alexa"]}})
        plug(ws).send(said("alexa"))
        wait(lambda: of(events, "routed"), what="the answer")
    [check] = [multipart(r) for r in services.checks]
    [command] = [multipart(r) for r in services.sent("stt.test", "/v1/audio/transcriptions")]
    assert (check["prompt"], check["boost"], check["glossary"]) == (b"Alexa, Lexa", b"true", b"home-assistant")
    assert "prompt" not in command and (command["boost"], command["glossary"]) == (b"true", b"home-assistant")


def test_a_word_not_heard_while_a_conversation_waits_leaves_it_its_turn(client, app, events, services, plug):
    """The conversation was listening for its next turn anyway: what was
    said after a word STT did not hear is that turn, as if nothing had been
    detected, and not a command dropped with the word."""
    services.check = lambda request, said: answering(None if said == "Hey Jarvis." else "Obrigado.")(
        request, said)
    services.transcripts.extend(["what time is it", "and in London"])
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, conversation(follow_up_s=30), ALEXA_ON)
        sat = plug(ws)
        sat.send(said("hey_jarvis"))
        settled(app)
        sat.send(said("alexa", seed=7))
        wait(lambda: len(of(events, "turn")) == 2, what="the next turn")
    assert [e["mode"] for e in of(events, "wake_rejected")] == ["on"]
    assert [e["wake_word"] for e in of(events, "wake")] == ["hey_jarvis"]
    assert [t["transcript"] for t in of(events, "turn")] == ["what time is it", "and in London"]


def test_a_rejected_wake_is_kept_as_a_clip_only_at_full_telemetry(client, app, events, services, plug, tmp_path):
    """The audio STT did not hear the word in is what the word's model is
    retrained on: kept as a WAV at level full, where words are kept, served
    by name, pruned and deleted with its day's records; at level timings,
    neither the clip nor the transcript."""
    services.check = answering("Obrigado.")
    clips = tmp_path / "telemetry" / "clips"
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        save(client, ALEXA_ON)
        telemetry_on(client, "timings")
        sat = plug(ws)
        sat.send(said("alexa"))
        wakes(app, 1)
        telemetry_on(client, "full")
        sat.send(said("alexa", seed=3))
        wakes(app, 2)
    plain, kept = wakes(app, 2)
    assert "heard" not in plain["verify"] and plain["verify"]["clip"] is None
    assert kept["verify"]["heard"] == "Obrigado."
    name = kept["verify"]["clip"]
    assert telemetry.CLIP.match(name) and name.endswith(f"-{NID}-alexa.wav")
    assert [p.name for p in clips.iterdir()] == [name]

    r = client.get(f"/satellites/telemetry/clips/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        samples = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    assert len(samples) == VERIFY_WINDOW_S * RATE and np.count_nonzero(samples == 20000) >= 160
    for missing in ("20260101T000000Z-020000000001-alexa.wav", "telemetry.json", "..%2Ftelemetry.json"):
        assert client.get(f"/satellites/telemetry/clips/{missing}").status_code == 404
    status = client.get("/satellites/telemetry").json()
    assert status["clips"] == {"count": 1, "bytes": len(r.content)}
    assert status["bytes"] >= len(r.content) + sum(f["bytes"] for f in status["files"])

    # An old day's clip goes with its day; today's stays.
    old = f"{datetime.now(UTC) - timedelta(days=30):%Y%m%d}T120000Z-{NID}-alexa.wav"
    (clips / old).write_bytes(r.content)
    app.hub.telemetry.prune()
    assert [p.name for p in clips.iterdir()] == [name]

    gone = client.delete("/satellites/telemetry").json()
    assert gone["clips"] == {"count": 0, "bytes": 0} and list(clips.iterdir()) == []


def test_the_verify_setting_is_validated_and_defaults_to_record_only(client):
    body = client.get("/satellites/wake-words").json()
    assert [w["verify"] for w in body["words"]] == [{"mode": "log", "spellings": []}]
    assert body["ptt"]["verify"] == {"mode": "log", "spellings": []}

    saved = save(client, {"name": "alexa", "action": ECHO,
                          "verify": {"mode": "on", "spellings": [" Alexandra ", "hey lexa"]}})
    [alexa] = saved["words"]
    assert alexa["verify"] == {"mode": "on", "spellings": ["Alexandra", "hey lexa"]}
    # A save that leaves it out keeps it, as every other field.
    [alexa] = save(client, {"name": "alexa", "threshold": 0.6})["words"]
    assert alexa["verify"]["mode"] == "on"

    for bad in ({"mode": "sometimes"}, {"spellings": ["lexa"] * 13}, {"spellings": ["x" * 41]},
                {"spellings": ["  "]}, {"spellings": ["a\nb"]}, {"spellings": "alexa"},
                {"mode": "on", "threshold": 0.9}):
        r = client.put("/satellites/wake-words", json={"words": [{"name": "alexa", "verify": bad}]})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_wake_words", bad
    [alexa] = client.get("/satellites/wake-words").json()["words"]
    assert alexa["verify"] == {"mode": "on", "spellings": ["Alexandra", "hey lexa"]}
    # The most it takes: twelve spellings of forty characters.
    [alexa] = save(client, {"name": "alexa", "verify": {"mode": "log", "spellings": ["x" * 40] * 12}})["words"]
    assert alexa["verify"]["spellings"] == ["x" * 40] * 12
