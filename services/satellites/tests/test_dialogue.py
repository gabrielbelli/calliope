"""One turn, streamed, and what a conversation remembers: dialogue.py,
language.py and the destinations it streams from, against fakes.

Every outside service is an httpx.MockTransport on a *.test host, which never
resolves, or (Home Assistant's websocket) a fake connection handed to
HaAssist in place of a real one. Nothing is played anywhere: the sink is
dialogue.Collect, or a recorder that notes when each sentence arrived.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import numpy as np
import pytest

from app import dialogue, language
from app import router as routing
from app.destinations import HaAssist, ThinkFilter

NID = "94b97e7b8be8"
SECRET = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LWhhLXRva2Vu.do-not-leak"
ONE_SECOND = b"\x00\x00" * 16000
LLM = {"type": "llm", "base_url": "http://llm.test/v1", "model": "tiny", "system": "Be brief."}
HA = {"type": "ha_conversation", "url": "http://ha.test:8123"}


def sse(pieces: list[str], delay: float = 0.0) -> httpx.Response:
    """An OpenAI-style streamed answer: one chunk per piece, `delay` apart."""
    async def body():
        yield b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
        for p in pieces:
            if delay:
                await asyncio.sleep(delay)
            yield f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


class Fake:
    """STT, TTS and whatever else a test adds, on *.test hosts."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.stt_text = "what time is it"
        self.stt_engine: str | None = None
        self.handlers = {"stt.test": self.stt, "tts.test": self.tts}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        handler = self.handlers.get(request.url.host)
        if handler is None:
            return httpx.Response(404)
        out = handler(request)
        return await out if asyncio.iscoroutine(out) else out

    def stt(self, request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "model": self.stt_engine})
        headers = {"x-stt-engine": self.stt_engine} if self.stt_engine else {}
        return httpx.Response(200, json={"text": self.stt_text}, headers=headers)

    def tts(self, request):
        return httpx.Response(200, content=np.full(2400, 500, "<i2").tobytes())

    def sent(self, host: str, path: str | None = None) -> list[httpx.Request]:
        return [r for r in self.seen if r.url.host == host and (path is None or r.url.path == path)]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SATELLITES_HA_TOKEN", "SATELLITES_LLM_API_KEY", "SATELLITES_TTS_VOICE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake() -> Fake:
    return Fake()


class Words:
    """A stand-in for wakewords_config.WordActions: behaviours by name."""

    editable = False
    load_error = None

    def __init__(self, **behaviours):
        self.b = {k: routing.Behaviour.model_validate(v) for k, v in behaviours.items()}

    def named(self, name):
        return routing.Route(name, self.b[name]) if name in self.b else None

    def find(self, satellite_id, satellite_name, wake_word):
        return self.named(wake_word)

    def warnings(self, lookup=None):
        return []

    def env_vars(self):
        return routing.env_status(b.action.destination for b in self.b.values() if b.action)

    def listing(self):
        return []


def router(fake: Fake, **behaviours) -> routing.Router:
    return routing.Router(Words(**behaviours), stt_url="http://stt.test", tts_url="http://tts.test",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))


def multipart(request: httpx.Request) -> dict[str, bytes]:
    head = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    msg = BytesParser(policy=HTTP).parsebytes(head + request.content)
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
            for p in msg.iter_parts()}


class Timed(dialogue.Collect):
    """Collect, noting when each sentence was handed over."""

    def __init__(self):
        super().__init__()
        self.at: list[float] = []

    async def play(self, pcm48k, text):
        self.at.append(time.monotonic())
        return await super().play(pcm48k, text)


async def turn(r: routing.Router, word: str, text: str | None = None, *, audio: bytes | None = None,
               memory: dialogue.Memory | None = None, sink=None, **kw) -> routing.Outcome:
    return await dialogue.run_turn(r, r.find(NID, "kitchen", word), satellite_id=NID,
                                   satellite_name="kitchen", wake_word=word, text=text, audio=audio,
                                   memory=memory, sink=sink or dialogue.Collect(), **kw)


# ---- sentences, ending phrases, memory ---------------------------------------------------


def test_a_streamed_answer_is_cut_into_sentences_as_each_one_ends_and_not_after_a_title():
    s = dialogue.Sentences()
    got = []
    for piece in ["Sure. Dr. Sil", "va said 3.5 degrees", "! Then:\n1. Preheat the oven",
                  " to J. R. R. Tolkien's taste? Done"]:
        got += s.feed(piece)
    got += s.flush()
    assert got == ["Sure.", "Dr. Silva said 3.5 degrees!", "Then:", "1. Preheat the oven to "
                   "J. R. R. Tolkien's taste?", "Done"]


def test_a_model_that_never_ends_a_sentence_is_still_spoken_before_it_finishes():
    s = dialogue.Sentences()
    out = s.feed("word, " * 60)  # 360 characters and no full stop
    assert out and len(out[0]) <= dialogue.Sentences.MAX_CHARS and out[0].endswith(",")


def test_markdown_meant_for_a_screen_is_not_read_aloud():
    assert dialogue.speakable("**Bold** and `code` and\n- a bullet\n# Heading") == \
        "Bold and code and\na bullet\nHeading"


@pytest.mark.parametrize("text", ["thanks", "Thank you!", "ok, that's all", "Goodbye.", "stop",
                                  "obrigado", "Obrigada, tchau!", "pode parar", "é só isso",
                                  "e so isso", "muito obrigado"])
def test_an_ending_phrase_said_on_its_own_ends_the_conversation(text):
    assert dialogue.is_ending(text)


@pytest.mark.parametrize("text", ["thanks, and what about tomorrow?", "stop the music in the kitchen",
                                  "obrigado, mas e amanhã?", "what's all this", ""])
def test_a_sentence_that_says_more_than_goodbye_carries_the_conversation_on(text):
    assert not dialogue.is_ending(text)


def test_memory_keeps_the_last_twenty_turns_and_no_more_than_its_characters():
    m = dialogue.Memory()
    for i in range(25):
        m.add(f"question {i}", f"answer {i}")
    assert len(m.turns) == 20 and m.turns[0].user == "question 5"
    m = dialogue.Memory(max_chars=100)
    for i in range(10):
        m.add("q" * 30, "a" * 30)
    assert len(m.turns) == 1  # 60 characters a turn; the newest always stays
    m.add("x" * 500, "y")
    assert [t.user for t in m.turns] == ["x" * 500]


def test_a_think_block_split_across_streamed_pieces_is_never_spoken():
    f = ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hi <thi", "nk>they want", " the time</th", "ink> It is ", "seven."])
    assert out + f.flush() == "Hi  It is seven."


# ---- language ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text,code", [
    ("what time is it", "en"), ("turn off the kitchen lights", "en"),
    ("que horas são", "pt"), ("apaga a luz da cozinha", "pt"),
    ("¿qué hora es?", "es"), ("quelle heure est-il", "fr"), ("che ore sono", "it"),
    ("wie spät ist es", "de"),
])
def test_the_language_of_a_short_command_is_read_from_its_transcript(text, code):
    assert language.detect(text) == code


def test_one_word_does_not_move_a_conversation_out_of_its_language():
    assert language.detect("sim") == "en"                    # nothing to go on: English
    assert language.detect("ok", prior="pt-BR") == "pt"      # a Portuguese conversation stays
    assert language.detect("que horas são", prior="en") == "pt"


@pytest.mark.parametrize("spoken,voice,answer", [
    ("en", "bm_george", "en"), ("pt-BR", "pf_dora", "pt-BR"), ("es", "ef_dora", "es"),
    ("fr", "ff_siwis", "fr"), ("it", "if_sara", "it"), ("de", "bm_george", "en"),
    ("pl", "bm_george", "en"),
])
def test_the_reply_voice_speaks_the_language_spoken_or_english_when_kokoro_cannot(spoken, voice, answer):
    assert language.voice_for(spoken, "bm_george") == voice
    assert language.reply_tag(spoken) == answer


# ---- one turn --------------------------------------------------------------------------------


@pytest.mark.parametrize("said,voice", [
    ("what time is it", "bm_george"), ("que horas são agora", "pf_dora"),
    ("¿qué hora es ahora?", "ef_dora"), ("wie spät ist es jetzt", "bm_george"),
])
async def test_each_utterance_is_answered_in_the_voice_of_the_language_it_was_spoken_in(fake, said, voice):
    r = router(fake, hey_jarvis={"action": {"destination": {"type": "echo"}}})
    out = await turn(r, "hey_jarvis", said)
    assert out.error is None and out.language_source == "detected"
    assert json.loads(fake.sent("tts.test")[0].content)["voice"] == voice


async def test_home_assistant_is_told_the_language_that_was_detected(fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = lambda r: httpx.Response(200, json={"response": {
        "response_type": "action_done", "speech": {"plain": {"speech": "Pronto."}}}})
    r = router(fake, hey_jarvis={"action": {"destination": HA}})
    out = await turn(r, "hey_jarvis", "apaga a luz da cozinha")
    assert json.loads(fake.sent("ha.test")[0].content) == {"text": "apaga a luz da cozinha",
                                                          "language": "pt-BR"}
    assert (out.language, out.voice) == ("pt-BR", "pf_dora")


async def test_a_hinted_word_uses_its_hint_even_for_words_that_look_english(fake):
    r = router(fake, alexa={"language": "pt-BR", "action": {"destination": {"type": "echo"}}})
    out = await turn(r, "alexa", "play some music")
    assert (out.language, out.language_source, out.voice) == ("pt-BR", "hint", "pf_dora")


async def test_an_explicit_voice_overrides_the_language_voice(fake):
    r = router(fake, hey_jarvis={"action": {"destination": {"type": "echo"}, "voice": "am_onyx"}})
    await turn(r, "hey_jarvis", "que horas são")
    assert json.loads(fake.sent("tts.test")[0].content)["voice"] == "am_onyx"


@pytest.mark.parametrize("engine,sent", [("whisper", b"pt"), ("parakeet", None), (None, None)])
async def test_a_language_hint_reaches_stt_only_when_it_runs_whisper(fake, engine, sent):
    """Parakeet refuses `language` with a 400 and detects the language
    itself; Whisper honours it. The engine is asked of /health before the
    first hinted request, and learnt from every answer's x-stt-engine."""
    fake.stt_engine = engine
    r = router(fake, alexa={"language": "pt-BR", "action": {"destination": {"type": "echo"}}})
    for _ in range(2):
        await turn(r, "alexa", audio=ONE_SECOND)
    for request in fake.sent("stt.test", "/v1/audio/transcriptions"):
        assert multipart(request).get("language") == sent
    assert len(fake.sent("stt.test", "/health")) == 1  # asked once, then remembered


async def test_no_request_to_stt_carries_a_language_when_the_word_has_no_hint(fake):
    fake.stt_engine = "whisper"
    r = router(fake, hey_jarvis={"action": {"destination": {"type": "echo"}}})
    await turn(r, "hey_jarvis", audio=ONE_SECOND)
    assert "language" not in multipart(fake.sent("stt.test", "/v1/audio/transcriptions")[0])
    assert fake.sent("stt.test", "/health") == []


async def test_an_llm_is_told_to_answer_in_english_when_kokoro_cannot_speak_the_language(fake):
    fake.handlers["llm.test"] = lambda r: sse(["Es ist sieben."])
    r = router(fake, hey_jarvis={"action": {"destination": LLM}})
    out = await turn(r, "hey_jarvis", "wie spät ist es jetzt")
    system = json.loads(fake.sent("llm.test")[0].content)["messages"][0]["content"]
    assert "Answer in English: the user spoke German" in system
    assert (out.language, out.reply_language, out.voice) == ("de", "en", "bm_george")


async def test_three_turns_carry_their_history_to_a_streaming_llm(fake):
    answers = iter([["It is ", "seven."], ["In London ", "it is eleven."], ["You asked ", "twice."]])
    fake.handlers["llm.test"] = lambda r: sse(next(answers))
    r = router(fake, hey_jarvis={"mode": "conversation", "action": {"destination": LLM}})
    memory = dialogue.Memory()
    for said in ["what time is it", "and in London?", "what did I ask?"]:
        out = await turn(r, "hey_jarvis", said, memory=memory)
        memory.add(out.transcript, out.spoken_text)
    last = json.loads(fake.sent("llm.test")[2].content)
    assert last["stream"] is True
    assert [(m["role"], m["content"]) for m in last["messages"][1:]] == [
        ("user", "what time is it"), ("assistant", "It is seven."),
        ("user", "and in London?"), ("assistant", "In London it is eleven."),
        ("user", "what did I ask?")]
    assert out.reply_text == "You asked twice."


async def test_the_first_sentence_is_spoken_while_a_slow_llm_is_still_writing(fake):
    """The pipeline's point: sentence one is synthesised and handed to the
    speaker before the model has written sentence three."""
    fake.handlers["llm.test"] = lambda r: sse(["Sure, here goes. ", "The first part is this. ",
                                               "And the last part is that."], delay=0.4)
    r = router(fake, hey_jarvis={"action": {"destination": LLM}})
    sink = Timed()
    out = await turn(r, "hey_jarvis", "tell me a story", sink=sink)
    t = out.timeline_ms
    assert out.error is None and len(sink.texts) >= 2
    assert t["first_audio"] < t["answer_done"] - 500  # measured: 419 ms against 1209 ms
    assert sink.texts[0] == "Sure, here goes."


async def test_later_sentences_that_queued_up_go_to_tts_together(fake):
    fake.handlers["llm.test"] = lambda r: sse(["One. Two. Three. Four. Five."])
    r = router(fake, hey_jarvis={"action": {"destination": LLM}})
    await turn(r, "hey_jarvis", "count")
    inputs = [json.loads(q.content)["input"] for q in fake.sent("tts.test")]
    assert inputs[0] == "One." and len(inputs) < 5 and " ".join(inputs) == "One. Two. Three. Four. Five."


async def test_a_server_that_does_not_stream_is_read_as_one_answer(fake):
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json={"choices": [{"message": {
        "content": "<think>hmm</think>Seven."}}]})
    r = router(fake, hey_jarvis={"action": {"destination": LLM}})
    assert (await turn(r, "hey_jarvis", "time?")).reply_text == "Seven."


# ---- handing over ------------------------------------------------------------------------------


def ha_error(request):
    return httpx.Response(200, json={"conversation_id": "01HA", "response": {
        "response_type": "error", "data": {"code": "no_intent_match"},
        "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}}}})


async def test_a_command_home_assistant_did_not_understand_is_handed_to_its_conversation(fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = ha_error
    fake.handlers["llm.test"] = lambda r: sse(["A black hole is ", "very dense."])
    r = router(fake,
               alexa={"action": {"destination": HA, "fallback": "hey_jarvis"}},
               hey_jarvis={"mode": "conversation", "language": "en", "action": {"destination": LLM}})
    started = []
    out = await turn(r, "alexa", "what is a black hole", on_handover=lambda t: started.append(t.id))
    assert started == ["hey_jarvis"] and out.handed_over_to == "hey_jarvis"
    assert out.error is None and out.reply_text == "A black hole is very dense."
    assert json.loads(fake.sent("llm.test")[0].content)["messages"][-1]["content"] == "what is a black hole"
    assert "Sorry" not in (out.spoken_text or "")


async def test_without_a_fallback_home_assistants_did_not_understand_is_spoken_as_before(fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = ha_error
    r = router(fake, alexa={"action": {"destination": HA}})
    out = await turn(r, "alexa", "flibble the wotsit")
    assert out.error is None and out.reply_text == "Sorry, I couldn't understand that"
    assert out.handed_over_to is None


async def test_a_destination_that_fails_outright_hands_over_too(fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = lambda r: httpx.Response(500, text="boom")
    fake.handlers["llm.test"] = lambda r: sse(["Here."])
    r = router(fake, alexa={"action": {"destination": HA, "fallback": "hey_jarvis"}},
               hey_jarvis={"mode": "conversation", "action": {"destination": LLM}})
    out = await turn(r, "alexa", "what now")
    assert out.handed_over_to == "hey_jarvis" and out.reply_text == "Here."


# ---- Home Assistant Assist pipelines -------------------------------------------------------------


class FakeHaSocket:
    """Home Assistant's websocket API as far as HaAssist uses it: auth, then
    one pipeline run answered with its events."""

    def __init__(self, speech="The lights are off.", response_type="action_done",
                 conversation_id="01ASSIST", devices=None):
        self.sent: list[dict] = []
        self.speech, self.response_type, self.cid = speech, response_type, conversation_id
        self.devices = devices if devices is not None else [
            {"id": "ha-device-hub", "identifiers": [["calliope", "entry_01"]]},
            {"id": "ha-device-kitchen", "identifiers": [["calliope", NID]], "area_id": "kitchen"}]
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.outbox.put_nowait({"type": "auth_required", "ha_version": "2026.8.1"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["type"] == "auth":
            self.outbox.put_nowait({"type": "auth_ok"} if msg["access_token"] == SECRET
                                   else {"type": "auth_invalid", "message": "Invalid access token"})
        elif msg["type"] == "config/device_registry/list":
            self.outbox.put_nowait({"id": msg["id"], "type": "result", "success": True,
                                    "result": self.devices})
        elif msg["type"] == "assist_pipeline/run":
            i = msg["id"]
            for m in ({"id": i, "type": "result", "success": True, "result": None},
                      {"id": i, "type": "event", "event": {"type": "run-start", "data": {}}},
                      {"id": i, "type": "event", "event": {"type": "intent-start", "data": {}}},
                      {"id": i, "type": "event", "event": {"type": "intent-end", "data": {
                          "intent_output": {"conversation_id": self.cid, "response": {
                              "response_type": self.response_type, "data": {"code": "no_intent_match"},
                              "speech": {"plain": {"speech": self.speech}}}}}}},
                      {"id": i, "type": "event", "event": {"type": "run-end", "data": None}}):
                self.outbox.put_nowait(m)

    async def recv(self):
        return json.dumps(await self.outbox.get())


@pytest.fixture
def ha_socket(monkeypatch):
    sockets: list[FakeHaSocket] = []
    made: dict = {}

    async def connect(url, timeout):
        made["url"] = url
        sock = FakeHaSocket(**made.get("kw", {}))
        sockets.append(sock)
        return sock
    monkeypatch.setattr(HaAssist, "connect", staticmethod(connect))
    monkeypatch.setattr(HaAssist, "devices", {})
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    return sockets, made


ASSIST = {"type": "ha_assist", "url": "https://ha.test:8123", "pipeline": "01PIPELINE"}


async def test_an_assist_pipeline_runs_the_transcript_at_its_intent_stage_and_its_speech_is_spoken(
        fake, ha_socket, caplog):
    sockets, made = ha_socket
    r = router(fake, hey_jarvis={"mode": "conversation", "action": {"destination": ASSIST}})
    memory = dialogue.Memory()
    with caplog.at_level(logging.DEBUG):
        out = await turn(r, "hey_jarvis", "turn off the lights", memory=memory)
        out2 = await turn(r, "hey_jarvis", "and the fan", memory=memory)
    assert made["url"] == "https://ha.test:8123"
    auth, devices, run = sockets[0].sent
    assert auth == {"type": "auth", "access_token": SECRET}
    assert devices == {"id": 1, "type": "config/device_registry/list"}
    # The satellite's own device, so Assist knows which room's lights.
    assert run == {"id": 2, "type": "assist_pipeline/run", "start_stage": "intent",
                   "end_stage": "intent", "input": {"text": "turn off the lights"},
                   "pipeline": "01PIPELINE", "device_id": "ha-device-kitchen"}
    # HA's conversation_id comes back on the next turn of the same
    # conversation, and the device is not looked up again.
    assert [m["type"] for m in sockets[1].sent] == ["auth", "assist_pipeline/run"]
    assert sockets[1].sent[1]["conversation_id"] == "01ASSIST"
    assert sockets[1].sent[1]["device_id"] == "ha-device-kitchen"
    assert out.reply_text == "The lights are off." and out2.error is None
    assert json.loads(fake.sent("tts.test")[0].content)["input"] == "The lights are off."
    for text in (json.dumps(out.as_json()), caplog.text):
        assert SECRET not in text


async def test_a_satellite_home_assistant_has_no_device_for_runs_without_one(fake, ha_socket):
    sockets, made = ha_socket
    made["kw"] = {"devices": [{"id": "other", "identifiers": [["calliope", "aabbccddeeff"]]}]}
    r = router(fake, hey_jarvis={"action": {"destination": ASSIST}})
    out = await turn(r, "hey_jarvis", "lights off")
    assert out.error is None and "device_id" not in sockets[0].sent[-1]


async def test_an_assist_pipeline_that_did_not_understand_hands_over_like_ha_conversation(
        fake, ha_socket):
    sockets, made = ha_socket
    made["kw"] = {"speech": "Sorry", "response_type": "error"}
    fake.handlers["llm.test"] = lambda r: sse(["Let me think."])
    r = router(fake, alexa={"action": {"destination": ASSIST, "fallback": "hey_jarvis"}},
               hey_jarvis={"mode": "conversation", "action": {"destination": LLM}})
    out = await turn(r, "alexa", "sing me a song")
    assert out.handed_over_to == "hey_jarvis" and out.reply_text == "Let me think."


async def test_a_refused_token_is_named_by_its_variable_and_never_shown(fake, ha_socket, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", "wrong-token-value")
    r = router(fake, hey_jarvis={"action": {"destination": ASSIST}})
    out = await turn(r, "hey_jarvis", "lights")
    assert "SATELLITES_HA_TOKEN" in out.error and "wrong-token-value" not in out.error


def test_home_assistants_websocket_is_reached_without_following_a_redirect():
    """The token travels in the first message after the handshake, so a
    followed redirect would hand it to whichever host the redirect named."""
    from websockets.asyncio.client import connect
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from app import destinations
    conn = asyncio.run(destinations.ha_websocket("https://ha.test:8123", 5))
    assert conn.uri == "wss://ha.test:8123/api/websocket"
    moved = InvalidStatus(Response(302, "Found", Headers({"Location": "wss://elsewhere.test/api/websocket"})))
    assert isinstance(connect.process_redirect(conn, moved), str)  # the library would follow it
    assert conn.process_redirect(moved) is moved                   # this refuses
