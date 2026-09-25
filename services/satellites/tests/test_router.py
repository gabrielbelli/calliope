"""Routing after a wake word, against fake STT, TTS, Home Assistant, LLM and
webhook services.

Every outside service is an httpx.MockTransport keyed on a host under .test
(RFC 2606: reserved, never resolves), so nothing here can reach the network,
the real stack or a device, and nothing is played anywhere.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import wave
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from voice_common import errors

from app import audio
from app.destinations import Echo
from app.router import Rule, Rules, RuleSet, Router, current, routes

NID = "94b97e7b8be8"
MAC = "94:B9:7E:7B:8B:E8"
SECRET = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LWhhLXRva2Vu.do-not-leak"
LLM_KEY = "sk-test-do-not-leak-0123456789"
TTS_SAMPLES = 2400  # 100 ms at Kokoro's 24 kHz
ONE_SECOND = b"\x00\x00" * 16000

HA = {"type": "ha_conversation", "url": "http://ha.test:8123"}
LLM = {"type": "llm", "base_url": "http://llm.test/v1", "model": "tiny", "system": "Be brief."}
HOOK = {"type": "webhook", "url": "http://hook.test/voice"}


def ha_answer(speech: str) -> httpx.Response:
    return httpx.Response(200, json={"response": {
        "response_type": "action_done", "language": "en",
        "speech": {"plain": {"speech": speech, "extra_data": None}}},
        "conversation_id": "01J"})


class Fake:
    """Every outside service at once, recording each request it is sent."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.stt_text = "what time is it"
        self.handlers = {
            "stt.test": lambda r: httpx.Response(200, json={"text": self.stt_text}),
            "tts.test": lambda r: httpx.Response(200, content=audio.tone(440, TTS_SAMPLES / 24000, 24000)),
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        handler = self.handlers.get(request.url.host)
        if handler is None:
            return httpx.Response(404, text=f"no fake for {request.url.host}")
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.seen]

    def sent(self, host: str) -> httpx.Request:
        return next(r for r in self.seen if r.url.host == host)


def rule(id: str, destination: dict | None = None, **kw) -> Rule:
    return Rule.model_validate({"id": id, "destination": destination or {"type": "echo"}, **kw})


def multipart(request: httpx.Request) -> dict[str, bytes]:
    head = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n"
    msg = BytesParser(policy=HTTP).parsebytes(head + request.content)
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
            for p in msg.iter_parts()}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SATELLITES_HA_TOKEN", "SATELLITES_LLM_API_KEY", "SATELLITES_STT_URL", "SATELLITES_TTS_URL",
                 "SATELLITES_TTS_VOICE", "HOOK_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def make(tmp_path, fake):
    def build(*rules: Rule, **kw) -> Router:
        r = Rules(tmp_path)
        if rules:
            r.replace(RuleSet(rules=list(rules)))
        opts = {"stt_url": "http://stt.test", "tts_url": "http://tts.test",
                "client": httpx.AsyncClient(transport=httpx.MockTransport(fake))}
        return Router(r, **(opts | kw))
    return build


@pytest.fixture
def api(make):
    router = make()
    app = FastAPI()
    errors.install_errors(app)
    app.include_router(routes)
    app.dependency_overrides[current] = lambda: router
    with TestClient(app) as client:
        yield client, router


# ---- the default and the happy path -------------------------------------------


async def test_a_fresh_hub_echoes_what_it_heard_back_to_the_satellite_that_heard_it(make, fake):
    out = await make().handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert (out.rule_id, out.transcript, out.reply_text, out.reply_to, out.error) == \
        ("default", "what time is it", "what time is it", NID, None)
    # 24 kHz in, 48 kHz out: twice the samples, two bytes each.
    assert len(out.reply_pcm48k) == TTS_SAMPLES * 2 * 2
    assert fake.hosts() == ["stt.test", "tts.test"]

    parts = multipart(fake.sent("stt.test"))
    assert fake.sent("stt.test").url.path == "/v1/audio/transcriptions"
    assert parts["model"] == b"whisper-1"
    with wave.open(io.BytesIO(parts["file"])) as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 16000)

    tts = fake.sent("tts.test")
    assert tts.url.path == "/v1/audio/speech"
    assert json.loads(tts.content) == {"model": "kokoro", "voice": "bm_george",
                                       "input": "what time is it", "response_format": "pcm"}


def test_the_default_ruleset_is_one_echo_rule_and_is_not_written_until_saved(tmp_path):
    rules = Rules(tmp_path)
    assert [(r.id, r.wake_word, r.satellites, r.destination.type, r.reply_to) for r in rules.rules] == \
        [("default", "*", [], "echo", "same")]
    assert not (tmp_path / "rules.json").exists()


def test_a_rule_written_before_the_rename_still_loads_and_is_saved_renamed(tmp_path):
    """rules.json named a rule's satellites "nodes" until 2026-09-25. A file
    that does not load turns routing off for the whole house (Rules._load),
    so the old name is read as the new one, and written back as the new one."""
    (tmp_path / "rules.json").write_text(json.dumps({"version": 1, "rules": [
        {"id": "kitchen", "wake_word": "hey_jarvis", "nodes": ["kitchen", MAC],
         "destination": {"type": "echo"}}]}))
    rules = Rules(tmp_path)
    assert rules.load_error is None
    assert rules.rules[0].satellites == ["kitchen", MAC]
    assert rules.match(NID, "anything", "hey_jarvis").id == "kitchen"

    rules.replace(rules.ruleset)
    saved = json.loads((tmp_path / "rules.json").read_text())["rules"][0]
    assert saved["satellites"] == ["kitchen", MAC] and "nodes" not in saved


# ---- matching -----------------------------------------------------------------


def test_the_first_matching_rule_wins_so_file_order_is_the_precedence(tmp_path):
    rules = Rules(tmp_path)
    rules.replace(RuleSet(rules=[
        rule("kitchen-jarvis", wake_word="hey_jarvis", satellites=["kitchen"]),
        rule("by-mac", wake_word="alexa", satellites=[MAC]),
        rule("any-jarvis", wake_word="Hey Jarvis"),
        rule("catch-all"),
    ]))

    def pick(nid, name, wake):
        return rules.match(nid, name, wake).id

    assert pick(NID, "Kitchen", "hey jarvis") == "kitchen-jarvis"   # names ignore case
    assert pick("aabbccddeeff", "bedroom", "hey-jarvis") == "any-jarvis"
    assert pick(NID, "anything", "ALEXA") == "by-mac"               # MAC with colons matches the id
    assert pick("aabbccddeeff", "bedroom", "alexa") == "catch-all"
    assert rules.warnings() == []


def test_a_catch_all_above_a_specific_rule_takes_everything_and_is_reported(tmp_path):
    rules = Rules(tmp_path)
    rules.replace(RuleSet(rules=[rule("catch-all"), rule("kitchen", wake_word="alexa", satellites=["kitchen"])]))

    assert rules.match(NID, "kitchen", "alexa").id == "catch-all"
    assert rules.warnings() == ["rule 'kitchen' can never match: rule 'catch-all' above it "
                                "matches everything it would"]


async def test_no_matching_rule_is_logged_and_nothing_is_sent_anywhere(make, fake, caplog):
    router = make(rule("alexa-only", wake_word="alexa"))
    with caplog.at_level(logging.INFO, logger="voice-satellites.router"):
        out = await router.handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert out.rule_id is None and out.reply_pcm48k is None
    assert "no rule matches wake word 'hey_jarvis'" in out.error
    assert "no rule matches" in caplog.text
    assert fake.seen == []  # not even STT: nobody asked for this utterance


# ---- destinations ---------------------------------------------------------------


async def test_ha_is_sent_the_bearer_from_its_env_var_and_its_plain_speech_is_spoken(make, fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = lambda r: ha_answer("It is half past seven.")
    router = make(rule("ha", HA, language="pt-BR"))

    out = await router.handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert out.error is None and out.reply_text == "It is half past seven."
    sent = fake.sent("ha.test")
    assert str(sent.url) == "http://ha.test:8123/api/conversation/process"
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(sent.content) == {"text": "what time is it", "language": "pt-BR"}
    # The hint is Home Assistant's and the voice's. STT is not sent it: this
    # fake names no engine, and stt-stack's default (Parakeet) refuses the
    # field with a 400 (test_a_language_hint_reaches_stt_only_when_it_runs_whisper).
    [transcription] = [r for r in fake.seen if r.url.path == "/v1/audio/transcriptions"]
    assert "language" not in multipart(transcription)
    tts = json.loads(fake.sent("tts.test").content)
    assert tts["input"] == "It is half past seven." and tts["voice"] == "pf_dora"


async def test_the_llm_gets_the_system_prompt_and_key_and_its_think_block_is_not_spoken(make, fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": "<think>they want the time</think>\nIt is seven."}}]})
    out = await make(rule("llm", LLM)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert out.error is None and out.reply_text == "It is seven."
    sent = fake.sent("llm.test")
    assert str(sent.url) == "http://llm.test/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {LLM_KEY}"
    # Streamed by default; this fake answers with one JSON body, which is the
    # fallback for a server that does not stream. The language to answer in
    # follows the system prompt.
    assert json.loads(sent.content) == {"model": "tiny", "max_tokens": 400, "stream": True,
                                        "messages": [
        {"role": "system", "content": "Be brief.\n\nAnswer in English, the language the user is speaking."},
        {"role": "user", "content": "what time is it"}]}


async def test_a_local_llm_with_no_key_set_is_called_without_an_authorization_header(make, fake):
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Hi."}}]})
    out = await make(rule("llm", LLM)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.reply_text == "Hi." and "authorization" not in fake.sent("llm.test").headers


async def test_the_webhook_gets_the_documented_fields_and_a_json_reply_is_spoken(make, fake):
    fake.handlers["hook.test"] = lambda r: httpx.Response(200, json={"reply": "Done."})
    out = await make(rule("hook", HOOK)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert out.error is None and out.reply_text == "Done." and out.reply_pcm48k
    assert json.loads(fake.sent("hook.test").content) == {
        "satellite": "kitchen", "satellite_id": NID, "wake_word": "hey_jarvis", "mode": "command",
        "text": "what time is it", "language": "en", "audio_seconds": 1.0, "history": []}


async def test_a_webhook_that_answers_without_a_reply_is_success_with_nothing_to_say(make, fake):
    fake.handlers["hook.test"] = lambda r: httpx.Response(204)
    out = await make(rule("hook", HOOK)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert (out.error, out.reply_text, out.reply_pcm48k) == (None, None, None)
    assert "tts.test" not in fake.hosts()


# ---- failures are outcomes, not exceptions ----------------------------------------


async def _hang(request):
    await asyncio.sleep(5)
    return httpx.Response(200)


def _refuse(request):
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("handler, expected", [
    (lambda r: httpx.Response(500, text="Traceback: boom"), "destination: Home Assistant answered 500: Traceback: boom"),
    (lambda r: httpx.Response(200, text="<html>proxy</html>"), "destination: Home Assistant answered 200 with a body that is not JSON"),
    (lambda r: httpx.Response(401), "destination: Home Assistant refused the token in SATELLITES_HA_TOKEN (401)"),
    (_refuse, "destination: ConnectError: connection refused"),
    # MockTransport ignores httpx's timeout, so only the hard ceiling can end this.
    (_hang, "destination: no answer within 0.05 s"),
], ids=["500", "not-json", "401", "refused", "hangs"])
async def test_a_failing_destination_becomes_an_error_and_never_an_exception(make, fake, monkeypatch, handler, expected):
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = handler
    out = await make(rule("ha", HA | {"timeout": 0.05})).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)

    assert out.error == expected
    assert out.transcript == "what time is it" and out.reply_pcm48k is None
    assert "tts.test" not in fake.hosts()
    assert SECRET not in json.dumps(out.as_json())


async def test_an_unset_ha_token_is_named_and_home_assistant_is_never_called(make, fake):
    out = await make(rule("ha", HA)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.error == "destination: SATELLITES_HA_TOKEN is not set, so there is no token for Home Assistant"
    assert "ha.test" not in fake.hosts()


async def test_a_bug_inside_a_destination_still_comes_back_as_an_error(make, monkeypatch):
    async def broken(self, client, req):
        raise RuntimeError("a bug")
    monkeypatch.setattr(Echo, "call", broken)
    out = await make().handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.error == "internal: RuntimeError: a bug"


@pytest.mark.parametrize("change, expected", [
    ({"stt_url": ""}, "stt: SATELLITES_STT_URL is not set, so nothing can be transcribed"),
    ({"stt": lambda r: httpx.Response(503, text="busy")}, "stt: STT answered 503: busy"),
    ({"stt": lambda r: httpx.Response(200, json={"duration": 1})}, 'stt: STT answered without a "text" field'),
], ids=["no-url", "503", "no-text"])
async def test_an_stt_failure_stops_before_the_destination(make, fake, change, expected):
    if "stt" in change:
        fake.handlers["stt.test"] = change.pop("stt")
    out = await make(**change).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.error == expected and out.reply_text is None
    assert "tts.test" not in fake.hosts()


async def test_an_empty_transcript_does_not_wake_the_assistant(make, fake):
    fake.stt_text = "  "
    fake.handlers["hook.test"] = lambda r: httpx.Response(200, json={"reply": "?"})
    out = await make(rule("hook", HOOK)).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.error == "stt: nothing was heard (the transcript is empty)"
    assert fake.hosts() == ["stt.test"]


async def test_a_tts_failure_keeps_the_reply_text_so_it_can_still_be_shown(make, fake):
    fake.handlers["tts.test"] = lambda r: httpx.Response(400, text="unknown voice")
    out = await make(rule("echo", voice="nope")).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.reply_text == "what time is it" and out.reply_pcm48k is None
    assert out.error == "tts: TTS answered 400: unknown voice"
    assert json.loads(fake.sent("tts.test").content)["voice"] == "nope"


# ---- where the reply goes ------------------------------------------------------------


async def test_reply_to_none_acts_without_synthesising_anything(make, fake):
    fake.handlers["hook.test"] = lambda r: httpx.Response(200, json={"reply": "Lights off."})
    out = await make(rule("hook", HOOK, reply_to="none")).handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert (out.reply_text, out.reply_to, out.reply_pcm48k, out.error) == ("Lights off.", None, None, None)
    assert "tts.test" not in fake.hosts()


async def test_reply_to_another_satellite_is_resolved_to_its_id_through_the_lookup(make):
    satellites = {"bedroom": ("aabbccddeeff", "bedroom")}
    router = make(rule("relay", reply_to="bedroom"), lookup=satellites.get)
    out = await router.handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    assert out.reply_to == "aabbccddeeff" and out.reply_pcm48k


def test_reply_to_a_satellite_that_does_not_exist_is_reported(make):
    router = make(rule("relay", reply_to="attic"), lookup={}.get)
    assert "rule 'relay' replies to 'attic', which is not a known satellite" in router.describe()["warnings"]


async def test_a_reply_longer_than_tts_accepts_is_cut_at_a_sentence_not_refused(make, fake):
    fake.stt_text = "Sentence number one is here. " * 200  # 5800 characters
    await make().handle(NID, "kitchen", "hey_jarvis", ONE_SECOND)
    spoken = json.loads(fake.sent("tts.test").content)["input"]
    assert len(spoken) <= 4096 and spoken.endswith("here.")


# ---- persistence ---------------------------------------------------------------------


def test_saved_rules_survive_a_restart_including_explicit_nulls(tmp_path):
    saved = RuleSet(rules=[rule("llm", LLM | {"api_key_env": None}, wake_word="alexa",
                                satellites=["kitchen"], language="en")])
    Rules(tmp_path).replace(saved)
    again = Rules(tmp_path)
    assert again.ruleset == saved
    # Dropped on save, a null would reload as the default and start sending a key.
    assert again.rules[0].destination.api_key_env is None
    assert os.listdir(tmp_path) == ["rules.json"]  # no temporary file left behind


def test_a_broken_rules_file_turns_routing_off_rather_than_back_to_echo(tmp_path):
    (tmp_path / "rules.json").write_text('{"rules": [{"id": "ha", "destination": {"type": "ha_conv')
    rules = Rules(tmp_path)
    assert rules.rules == [] and rules.match(NID, "kitchen", "hey_jarvis") is None
    assert "rules.json could not be loaded" in rules.load_error
    assert (tmp_path / "rules.json").read_text().endswith("ha_conv")  # left for the operator


def test_a_failed_save_leaves_the_previous_rules_file_whole(tmp_path, monkeypatch):
    rules = Rules(tmp_path)
    rules.replace(RuleSet(rules=[rule("first")]))
    before = (tmp_path / "rules.json").read_text()

    def power_cut(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", power_cut)
    with pytest.raises(OSError):
        rules.replace(RuleSet(rules=[rule("second")]))

    assert (tmp_path / "rules.json").read_text() == before
    assert os.listdir(tmp_path) == ["rules.json"]
    assert [r.id for r in rules.rules] == ["first"]


# ---- the API ----------------------------------------------------------------------


def test_the_ha_token_never_appears_in_get_routing_or_rules_json(api, fake, tmp_path, monkeypatch):
    client, _ = api
    monkeypatch.setenv("SATELLITES_HA_TOKEN", SECRET)
    fake.handlers["ha.test"] = lambda r: ha_answer("OK.")

    put = client.put("/satellites/routing", json={"rules": [{"id": "ha", "destination": HA}]})
    got = client.get("/satellites/routing")
    tried = client.post("/satellites/routing/test", json={"satellite": "kitchen", "wake_word": "x", "text": "hi"})

    assert put.status_code == got.status_code == tried.status_code == 200
    assert got.json()["env"] == {"SATELLITES_HA_TOKEN": True}
    for text in (put.text, got.text, tried.text, (tmp_path / "rules.json").read_text()):
        assert SECRET not in text
    # And it was really used, so the absence above is not an unused token.
    assert fake.sent("ha.test").headers["authorization"] == f"Bearer {SECRET}"


def test_get_routing_says_which_secrets_are_missing_without_values(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    client.put("/satellites/routing", json={"rules": [
        {"id": "ha", "destination": HA},
        {"id": "llm", "destination": LLM},
        {"id": "hook", "destination": HOOK | {"token_env": "HOOK_TOKEN"}}]})
    body = client.get("/satellites/routing").json()
    assert body["env"] == {"HOOK_TOKEN": False, "SATELLITES_HA_TOKEN": False, "SATELLITES_LLM_API_KEY": True}
    assert body["services"] == {"stt": "http://stt.test", "tts": "http://tts.test", "voice": "bm_george",
                                "stt_engine": None}
    assert LLM_KEY not in json.dumps(body)


@pytest.mark.parametrize("destination", [
    HA | {"token": SECRET},                           # the token itself, in a field of its own
    HA | {"token_env": SECRET},                       # the token pasted where its name belongs
    LLM | {"api_key_env": LLM_KEY},
    HA | {"url": f"http://user:{SECRET}@ha.test"},     # credentials in the URL
], ids=["token-field", "token-in-token_env", "key-in-api_key_env", "userinfo-url"])
def test_a_secret_pasted_into_a_rule_is_refused_rather_than_saved(api, tmp_path, destination):
    client, _ = api
    r = client.put("/satellites/routing", json={"rules": [{"id": "ha", "destination": destination}]})
    assert r.status_code == 422
    assert not (tmp_path / "rules.json").exists()


@pytest.mark.parametrize("rules", [
    [{"id": "a", "destination": {"type": "echo"}}, {"id": "a", "destination": {"type": "echo"}}],
    [{"id": "a", "destination": {"type": "carrier_pigeon"}}],
    [{"id": "a", "destination": {"type": "llm", "base_url": "ftp://llm.test", "model": "m"}}],
    [{"id": "a", "wake_word": "", "destination": {"type": "echo"}}],
    [{"id": "a", "language": "Portuguese", "destination": {"type": "echo"}}],
], ids=["duplicate-id", "unknown-type", "not-http", "empty-wake-word", "language-not-a-code"])
def test_an_invalid_ruleset_is_refused_and_the_old_one_stays(api, rules):
    client, router = api
    assert client.put("/satellites/routing", json={"rules": rules}).status_code == 422
    assert [r.id for r in router.rules.rules] == ["default"]


def test_the_test_route_skips_stt_and_returns_the_outcome_without_audio_bytes(api, fake):
    client, _ = api
    r = client.post("/satellites/routing/test", json={"satellite": NID, "wake_word": "hey_jarvis",
                                                 "text": "good morning"})
    body = r.json()
    assert r.status_code == 200 and body["error"] is None
    assert (body["rule_id"], body["transcript"], body["reply_text"], body["reply_to"]) == \
        ("default", "good morning", "good morning", NID)
    assert body["reply_audio_seconds"] == TTS_SAMPLES / 24000
    assert fake.hosts() == ["tts.test"]  # no STT: the text was typed


def test_the_wake_word_is_taken_off_the_front_of_the_transcript_and_nowhere_else():
    from app.router import strip_wake_phrase
    assert strip_wake_phrase("Hey Jarvis, what time is it?", "hey_jarvis") == "what time is it?"
    assert strip_wake_phrase("Jarvis what time is it", "hey_jarvis") == "what time is it"
    # Measured on orko: Parakeet's rendering of the rewound tail of the word.
    assert strip_wake_phrase("Harvis, what time is it?", "hey_jarvis") == "what time is it?"
    assert strip_wake_phrase("What time is it?", "hey_jarvis") == "What time is it?"
    # The wake word and nothing after it is no command at all.
    assert strip_wake_phrase("Hey Jarvis.", "hey_jarvis") == ""
    assert strip_wake_phrase("ask jarvis about it", "hey_jarvis") == "ask jarvis about it"
    assert strip_wake_phrase("turn the lights off", "ptt") == "turn the lights off"
