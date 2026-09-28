"""The language model destination against any OpenAI-compatible server, and
the hub's routes that list a server's models and ask it one question.

The servers are fakes: an httpx.MockTransport on a *.test host (RFC 2606:
reserved, never resolves), answering with the bodies real providers send,
copied from their own error answers with the key and the links changed. So
nothing here reaches the network, a provider or a device.

What these prevent:

  * a newer OpenAI model refusing every turn, because the hub sends
    max_tokens and it takes only max_completion_tokens;
  * a provider's refusal shown as a JSON blob or a Python dict, or with the
    end of the key in it, which OpenAI and DeepSeek both repeat;
  * an answer that is spoken as silence: content in parts (Mistral's
    reasoning models), or a reasoning model that spent its whole limit
    thinking;
  * reasoning read aloud;
  * a base URL pasted as the whole endpoint, which answered 404 on every turn;
  * a key in the environment with a CR or a newline on the end, which h11
    refused in a sentence that quoted it whole, past the scrub, into the
    turn's error, the log and the page.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import h11
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from voice_common import errors

from app import destinations, secret_store
from app import router as router_module
from app.destinations import Llm, Request
from app.router import Rules, RuleSet, Router, current, quiet_validation, routes
from test_router import Fake, rule

NID = "94b97e7b8be8"
# Ends in WXYZ, which is what a provider's masked echo of it would end in.
LLM_KEY = "sk-test-do-not-leak-0123456789WXYZ"
LLM = {"type": "llm", "base_url": "http://llm.test/v1", "model": "tiny"}

# OpenAI's answer to max_tokens from a reasoning model, verbatim.
WANTS_COMPLETION_TOKENS = {"error": {
    "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
               "Use 'max_completion_tokens' instead.",
    "type": "invalid_request_error", "param": "max_tokens", "code": "unsupported_parameter"}}


def one_body(text: str | list | None = "Hi.", finish: str = "stop", **message) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": finish,
                                                  "message": {"role": "assistant", "content": text,
                                                              **message}}]})


def sse(*chunks: dict) -> httpx.Response:
    """A streamed answer: each chunk as one server-sent event, then [DONE]."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def delta(finish: str | None = None, **fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": finish}]}


def sent(fake: Fake) -> list[dict]:
    return [json.loads(r.content) for r in fake.seen if r.url.host == "llm.test"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SATELLITES_LLM_API_KEY", "SATELLITES_STT_URL", "SATELLITES_TTS_URL"):
        monkeypatch.delenv(name, raising=False)
    # What a server takes its token limit by is learned per process; each
    # test starts knowing nothing.
    monkeypatch.setattr(Llm, "limit_names", {})


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def client(fake) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake))


def ask(text: str = "what time is it") -> Request:
    return Request(satellite_id=NID, satellite_name="kitchen", wake_word="hey_jarvis",
                   text=text, audio_seconds=1.0)


async def said(destination: dict, client: httpx.AsyncClient) -> str:
    """What the destination answers, in the pieces a turn would speak."""
    return "".join([p async for p in Llm.model_validate(destination).answer(client, ask())])


@pytest.fixture
def make(tmp_path, fake):
    def build(destination: dict) -> Router:
        rules = Rules(tmp_path)
        rules.replace(RuleSet(rules=[rule("llm", destination)]))
        return Router(rules, stt_url="http://stt.test", tts_url="http://tts.test",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return build


# ---- the token limit's two names ------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "one-body"])
async def test_a_server_that_refuses_max_tokens_is_asked_again_with_max_completion_tokens(
        fake, client, stream):
    def server(request):
        body = json.loads(request.content)
        if "max_tokens" in body:
            return httpx.Response(400, json=WANTS_COMPLETION_TOKENS)
        return sse(delta(content="It is seven.")) if stream else one_body("It is seven.")
    fake.handlers["llm.test"] = server

    assert await said(LLM | {"stream": stream}, client) == "It is seven."
    first, second = sent(fake)
    assert first["max_tokens"] == 400 and "max_completion_tokens" not in first
    assert second["max_completion_tokens"] == 400 and "max_tokens" not in second
    assert second["messages"] == first["messages"]

    # Learned: the next turn goes straight to the name this model takes.
    assert await said(LLM | {"stream": stream}, client) == "It is seven."
    assert len(sent(fake)) == 3 and "max_completion_tokens" in sent(fake)[2]
    assert Llm.model_validate(LLM).limit() == "max_completion_tokens"


@pytest.mark.parametrize("body, shown", [
    ({"error": {"message": "max_tokens is too large: 9000. This model supports at most 4096.",
                "type": "invalid_request_error", "param": "max_tokens", "code": "invalid_value"}},
     "the LLM answered 400: max_tokens is too large: 9000. This model supports at most 4096."),
    ({"error": {"message": "The model `tiny` does not exist.", "type": "invalid_request_error",
                "param": None, "code": "model_not_found"}},
     "the LLM answered 400: The model `tiny` does not exist."),
], ids=["limit-too-large", "no-such-model"])
async def test_a_400_about_anything_else_is_not_asked_again(make, fake, body, shown):
    fake.handlers["llm.test"] = lambda r: httpx.Response(400, json=body)
    out = await make(LLM).handle_text(NID, "hey_jarvis", "hi")
    assert out.error == f"destination: {shown}"
    assert len(sent(fake)) == 1
    assert Llm.limit_names == {}


async def test_the_body_is_model_messages_and_one_limit_and_nothing_else(fake, client):
    """No temperature (a reasoning model refuses any but its default), no
    stream_options (some servers refuse it), and no field only one provider
    knows."""
    fake.handlers["llm.test"] = lambda r: sse(delta(content="Hi."))
    await said(LLM | {"system": "Be brief."}, client)
    fake.handlers["llm.test"] = lambda r: one_body("Hi.")
    await said(LLM | {"stream": False}, client)
    streamed, plain = sent(fake)
    assert set(streamed) == {"model", "messages", "max_tokens", "stream"}
    assert streamed["stream"] is True
    assert set(plain) == {"model", "messages", "max_tokens"}


# ---- a refusal, in the provider's words ------------------------------------------------


@pytest.mark.parametrize("answer, expected", [
    # OpenAI, with its link moved to example.com.
    (httpx.Response(401, json={"error": {
        "message": "Incorrect API key provided: sk-proj-************************WXYZ. You can find "
                   "your API key at https://platform.example.com/api-keys.",
        "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}),
     "destination: the LLM refused the key in SATELLITES_LLM_API_KEY (401): Incorrect API key "
     "provided: [key hidden] You can find your API key at https://platform.example.com/api-keys."),
    # DeepSeek.
    (httpx.Response(401, json={"error": {
        "message": "Authentication Fails, Your api key: ****WXYZ is invalid",
        "type": "authentication_error", "param": None, "code": "invalid_request_error"}}),
     "destination: the LLM refused the key in SATELLITES_LLM_API_KEY (401): Authentication Fails, "
     "Your api key: [key hidden] is invalid"),
    # A proxy that repeats the key whole, in FastAPI's own shape.
    (httpx.Response(403, json={"detail": f"key {LLM_KEY} is not allowed here"}),
     "destination: the LLM refused the key in SATELLITES_LLM_API_KEY (403): key [key hidden] is "
     "not allowed here"),
    # An error in the middle of a stream, once written as a Python dict.
    (sse(delta(content=""), {"error": {"message": "Your api key: ****WXYZ is invalid",
                                       "type": "authentication_error"}}),
     "destination: the LLM streamed an error: Your api key: [key hidden] is invalid"),
    # Not JSON at all: the text, on one line.
    (httpx.Response(502, text="<html>\n  Bad gateway\n</html>"),
     "destination: the LLM answered 502: <html> Bad gateway </html>"),
], ids=["openai-401", "deepseek-401", "proxy-403", "streamed", "not-json"])
async def test_the_providers_own_sentence_is_the_error_and_a_masked_key_is_hidden(
        make, fake, monkeypatch, caplog, answer, expected):
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    fake.handlers["llm.test"] = lambda r: answer
    router = make(LLM)
    with caplog.at_level(logging.INFO, logger="voice-satellites.router"):
        out = await router.handle_text(NID, "hey_jarvis", "hi")
        router.log_outcome(NID, "hey_jarvis", out)

    assert out.error == expected
    for text in (out.error, json.dumps(out.as_json()), caplog.text):
        assert LLM_KEY not in text and "WXYZ" not in text and "****" not in text
    assert expected in caplog.text  # the INFO line, and it is the same sentence
    assert fake.sent("llm.test").headers["authorization"] == f"Bearer {LLM_KEY}"


@pytest.mark.parametrize("named, expected", [
    (True, "destination: the LLM wants a key (401) and SATELLITES_LLM_API_KEY has none: "
           "You didn't provide an API key."),
    (False, "destination: the LLM wants a key (401) and the action names none to send: "
            "You didn't provide an API key."),
], ids=["variable-unset", "no-variable"])
async def test_a_server_that_wants_a_key_when_none_was_sent_says_which_variable(make, fake, named, expected):
    fake.handlers["llm.test"] = lambda r: httpx.Response(401, json={"error": {
        "message": "You didn't provide an API key.", "type": "invalid_request_error"}})
    out = await make(LLM if named else LLM | {"api_key_env": None}).handle_text(NID, "hey_jarvis", "hi")
    assert out.error == expected
    assert "authorization" not in fake.sent("llm.test").headers


# ---- a key the environment holds with something a header cannot carry --------------------------

# What a .env saved with Windows line endings, a secret made from a file, or a
# careless paste leaves in a key. h11 refuses each in a header, in a sentence
# that quotes it whole with the control character escaped past _scrub.
UNSENDABLE = {"cr": LLM_KEY + "\r", "lf": LLM_KEY + "\n", "crlf": LLM_KEY + "\r\n",
              "tab": "\t" + LLM_KEY, "escape": LLM_KEY + "\x1b", "space": LLM_KEY.replace("-", " ", 1),
              "non-ascii": LLM_KEY + "é"}
REFUSED = ("SATELLITES_LLM_API_KEY is set in the hub's environment with a line break, a space or "
           "a character outside printable ASCII in it, which no request header can carry, so it "
           "is not sent")


def h11_refusal(value: str) -> httpx.LocalProtocolError:
    """The error httpx raises for an Authorization header h11 will not write,
    in h11's own words: made by h11, so the test cannot drift from what h11
    says. httpcore passes the sentence on unchanged."""
    try:
        h11.Request(method="POST", target="/v1/chat/completions",
                    headers=[("Host", "llm.test"), ("Authorization", f"Bearer {value}".encode())])
    except h11.LocalProtocolError as e:
        return httpx.LocalProtocolError(str(e))
    raise AssertionError("h11 took the header")


def nowhere(text: str, value: str = LLM_KEY) -> None:
    """Neither the key, nor the value as it was set, nor any sign of either:
    the key's end, or h11's sentence that quotes it."""
    for sign in (LLM_KEY, value.strip(), "WXYZ", "Illegal header"):
        assert sign not in text, text


@pytest.mark.parametrize("value", UNSENDABLE.values(), ids=UNSENDABLE.keys())
async def test_a_key_in_the_environment_no_header_can_carry_is_named_and_not_sent(
        make, fake, monkeypatch, caplog, value):
    """Measured before the fix: a CR on the end reached h11, whose refusal
    quoted the key, and _scrub's exact match missed it because the CR was
    written as an escape. It was the turn's error, its INFO line and its
    event."""
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", value)
    fake.handlers["llm.test"] = lambda r: sse(delta(content="Hi."))
    router = make(LLM)
    with caplog.at_level(logging.INFO, logger="voice-satellites.router"):
        out = await router.handle_text(NID, "hey_jarvis", "hi")
        router.log_outcome(NID, "hey_jarvis", out)

    assert out.error.startswith(f"destination: {REFUSED}; set it again without one"), out.error
    for text in (out.error, json.dumps(out.as_json()), caplog.text):
        nowhere(text, value)
    assert "llm.test" not in fake.hosts(), "the key went anyway"
    # It is set, and says so; what is wrong with it is the turn's to say.
    assert router_module.env_status([Llm.model_validate(LLM)]) == {"SATELLITES_LLM_API_KEY": True}


@pytest.mark.parametrize("value", UNSENDABLE.values(), ids=UNSENDABLE.keys())
def test_the_picker_and_the_test_name_such_a_key_and_send_nothing(api, fake, monkeypatch, value):
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", value)
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json=LISTED)
    for path, body in (("/satellites/llm/models", {"base_url": "http://llm.test/v1"}),
                       ("/satellites/llm/test", LLM)):
        r = api.post(path, json=body)
        assert r.status_code == 502 and r.json()["error"]["code"] == "llm", r.text
        assert r.json()["error"]["message"].startswith(REFUSED), r.text
        nowhere(r.text, value)
    assert fake.seen == []


async def test_a_transport_error_that_quotes_a_header_is_shown_by_its_type_alone(
        api, make, fake, monkeypatch, caplog):
    """Whatever h11 refuses, its words are never shown: the destination's
    stage, a stage that runs through Router.stage (speech here; Home
    Assistant's text-to-speech sends its token there), and both routes. Made
    with a valid key, so what is proved is the transport path on its own."""
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)

    def refuse(request):
        raise h11_refusal(LLM_KEY + "\r")
    fake.handlers["llm.test"] = refuse
    withheld = ("LocalProtocolError: the hub could not write the request, and h11's reason is "
                "not shown because it can quote a header that holds a key")
    router = make(LLM)
    with caplog.at_level(logging.INFO, logger="voice-satellites.router"):
        out = await router.handle_text(NID, "hey_jarvis", "hi")
        router.log_outcome(NID, "hey_jarvis", out)
    assert out.error == f"destination: {withheld}"

    fake.handlers["llm.test"] = lambda r: sse(delta(content="Hi."))
    fake.handlers["tts.test"] = refuse
    spoken = await make(LLM).handle_text(NID, "hey_jarvis", "hi")
    assert spoken.error == f"tts: {withheld}"

    fake.handlers["llm.test"] = refuse
    answers = [api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"}),
               api.post("/satellites/llm/test", json=LLM)]
    for r in answers:
        assert r.status_code == 502 and r.json()["error"]["message"] == withheld, r.text
    for text in (out.error, json.dumps(out.as_json()), spoken.error, caplog.text,
                 *(r.text for r in answers)):
        nowhere(text)


def test_any_other_transport_error_keeps_its_words_without_the_key():
    request = httpx.Request("GET", "http://llm.test/v1/models")
    refused = httpx.ConnectError("[Errno 111] Connection refused", request=request)
    assert destinations.transport_error(refused, LLM_KEY) == \
        "ConnectError: [Errno 111] Connection refused"
    echoed = httpx.RemoteProtocolError(f"peer said {LLM_KEY}", request=request)
    assert destinations.transport_error(echoed, None, LLM_KEY) == \
        "RemoteProtocolError: peer said [key hidden]"


# ---- what is spoken ------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "one-body"])
async def test_content_in_parts_is_spoken_and_thinking_parts_are_not(fake, client, stream):
    """Mistral's reasoning models: a "thinking" part, then a "text" part.
    Read as a string only, the whole answer was spoken as silence."""
    thinking = {"type": "thinking", "thinking": [{"type": "text", "text": "They want the time."}]}
    if stream:
        fake.handlers["llm.test"] = lambda r: sse(
            delta(role="assistant", content=""), delta(content=[thinking]),
            delta(content=[{"type": "text", "text": "It is "}]), delta(content="seven."),
            delta(finish="stop"))
    else:
        fake.handlers["llm.test"] = lambda r: one_body([thinking, {"type": "text", "text": "It is seven."}])
    assert await said(LLM | {"stream": stream}, client) == "It is seven."


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "one-body"])
async def test_a_model_that_spent_its_whole_limit_thinking_says_so(make, fake, stream):
    """400 tokens is a sentence's worth of reasoning for some models. Before
    this, nothing was said and nothing said why."""
    if stream:
        fake.handlers["llm.test"] = lambda r: sse(
            delta(role="assistant", reasoning_content="Let me think about the time"),
            delta(reasoning_content=" in every zone"), delta(content="<think>and more"),
            delta(finish="length"))
    else:
        fake.handlers["llm.test"] = lambda r: one_body("", finish="length",
                                                       reasoning_content="Let me think")
    out = await make(LLM | {"stream": stream}).handle_text(NID, "hey_jarvis", "hi")
    assert out.error == ("destination: the model spent its whole reply limit (400 tokens) before "
                         "answering; raise it under More, or choose a model that does not reason first")
    assert "tts.test" not in fake.hosts()


async def test_a_reply_cut_short_by_the_limit_is_still_spoken(fake, client):
    fake.handlers["llm.test"] = lambda r: sse(delta(content="It is seven and"), delta(finish="length"))
    assert await said(LLM, client) == "It is seven and"


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "one-body"])
async def test_reasoning_content_is_never_spoken(fake, client, stream):
    """DeepSeek and vLLM put the model's working in reasoning_content,
    OpenRouter in reasoning. Only content is the answer."""
    if stream:
        fake.handlers["llm.test"] = lambda r: sse(
            delta(role="assistant", reasoning_content="The user asks the time."),
            delta(reasoning="Seven, then."), delta(content="It is seven."), delta(finish="stop"))
    else:
        fake.handlers["llm.test"] = lambda r: one_body("It is seven.", reasoning_content="Hmm.",
                                                       reasoning="Seven.")
    assert await said(LLM | {"stream": stream}, client) == "It is seven."


# ---- the base URL -------------------------------------------------------------------------


@pytest.mark.parametrize("pasted", ["http://llm.test/v1/chat/completions",
                                    "http://llm.test/v1/chat/completions/",
                                    "http://llm.test/v1//chat/completions"])
async def test_a_base_url_pasted_with_chat_completions_is_trimmed(fake, client, tmp_path, pasted):
    """The address in a provider's curl example is the whole endpoint.
    Trimmed, not refused: a wake_words.json holding one would otherwise stop
    loading, and that switches every wake word off."""
    assert Llm.model_validate(LLM | {"base_url": pasted}).base_url == "http://llm.test/v1"
    fake.handlers["llm.test"] = lambda r: one_body("Hi.")
    assert await said(LLM | {"base_url": pasted, "stream": False}, client) == "Hi."
    assert str(fake.sent("llm.test").url) == "http://llm.test/v1/chat/completions"
    # And saved trimmed, so what GET answers is the address that works.
    rules = Rules(tmp_path)
    rules.replace(RuleSet(rules=[rule("llm", LLM | {"base_url": pasted})]))
    saved = json.loads((tmp_path / "rules.json").read_text())
    assert saved["rules"][0]["destination"]["base_url"] == "http://llm.test/v1"


def test_a_base_url_that_only_looks_like_the_endpoint_is_left_alone():
    for url in ("http://llm.test/v1", "http://llm.test/chat/completions-proxy",
                "http://chat/completions"):
        assert Llm.model_validate(LLM | {"base_url": url}).base_url == url


# ---- the model picker: POST /satellites/llm/models ------------------------------------------


@pytest.fixture
def api(fake, tmp_path):
    router = Router(Rules(tmp_path), stt_url="http://stt.test", tts_url="http://tts.test",
                    client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    app = FastAPI()
    errors.install_errors(app)
    quiet_validation(app)
    app.include_router(routes)
    app.dependency_overrides[current] = lambda: router
    with TestClient(app) as client:
        yield client


# OpenRouter's shape, abridged, with the ids it would never send mixed in.
LISTED = {"object": "list", "data": [
    {"id": "vendor/zeta-large", "object": "model"}, {"id": "Alpha-mini", "object": "model"},
    {"id": "beta"}, {"id": "beta"}, {"id": "x" * 121}, {"id": 7}, {"name": "no id"}, "gamma"]}


@pytest.mark.parametrize("where", ["environment", "hub"])
def test_the_model_list_is_the_servers_ids_sorted_and_asked_with_the_key(api, fake, monkeypatch, where):
    if where == "environment":
        monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    else:
        secret_store.current().set("SATELLITES_LLM_API_KEY", LLM_KEY)
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json=LISTED)
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1/chat/completions",
                                                 "api_key_env": "SATELLITES_LLM_API_KEY"})
    assert r.status_code == 200, r.text
    assert r.json() == {"models": ["Alpha-mini", "beta", "gamma", "vendor/zeta-large"]}
    asked = fake.sent("llm.test")
    assert (asked.method, str(asked.url)) == ("GET", "http://llm.test/v1/models")
    assert asked.headers["authorization"] == f"Bearer {LLM_KEY}"
    assert LLM_KEY not in r.text


def test_a_local_server_is_asked_for_its_models_without_a_key(api, fake):
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json=[{"id": "local-7b"}, "local-1b"])
    for named in ({"api_key_env": None}, {}):  # no name, or a name with nothing set
        r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test:8080/v1"} | named)
        assert r.status_code == 200 and r.json() == {"models": ["local-1b", "local-7b"]}
    assert all("authorization" not in q.headers for q in fake.seen)


def paged(ids: list[str], size: int = 20):
    """A server that pages its /models as Anthropic's does: `size` at a time
    unless asked for up to 1000, from after_id, with has_more and last_id."""
    def answer(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        start = ids.index(q["after_id"]) + 1 if "after_id" in q else 0
        page = ids[start:start + int(q.get("limit", size))]
        return httpx.Response(200, json={"data": [{"type": "model", "id": i} for i in page],
                                         "has_more": start + len(page) < len(ids),
                                         "first_id": page[0] if page else None,
                                         "last_id": page[-1] if page else None})
    return answer


def test_a_model_list_that_pages_is_read_to_its_end(api, fake, monkeypatch):
    """Read as one page, 45 models were 20, and the page said "20 models to
    pick from" as if that were all."""
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    ids = [f"model-{n:02}" for n in range(45)]
    fake.handlers["llm.test"] = paged(ids)
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert r.status_code == 200 and r.json() == {"models": ids}, r.text
    # The first as a server that does not page is asked; then from its last.
    assert [str(q.url) for q in fake.seen] == [
        "http://llm.test/v1/models", "http://llm.test/v1/models?limit=1000&after_id=model-19"]
    assert all(q.headers["authorization"] == f"Bearer {LLM_KEY}" for q in fake.seen)


def test_a_server_that_ignores_after_id_or_never_ends_is_asked_a_bounded_number_of_times(
        api, fake, monkeypatch):
    first = paged([f"model-{n:02}" for n in range(45)])
    fake.handlers["llm.test"] = lambda r: first(httpx.Request("GET", "http://llm.test/v1/models"))
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert r.status_code == 200 and len(r.json()["models"]) == 20 and len(fake.seen) == 2

    fake.seen.clear()
    endless = iter(range(10 ** 6))
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json={
        "data": [{"id": f"model-{next(endless)}"}], "has_more": True,
        "last_id": f"cursor-{len(fake.seen)}"})
    monkeypatch.setattr(destinations, "MODELS_PAGES_MAX", 3)
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert r.status_code == 200 and len(r.json()["models"]) == 4 and len(fake.seen) == 4


def test_a_model_list_the_server_refuses_is_a_502_in_its_own_words(api, fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    fake.handlers["llm.test"] = lambda r: httpx.Response(401, json={"error": {
        "message": "Incorrect API key provided: sk-proj-****WXYZ.", "code": "invalid_api_key"}})
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "llm"
    assert r.json()["error"]["message"] == ("the server refused the key in SATELLITES_LLM_API_KEY "
                                            "(401): Incorrect API key provided: [key hidden]")
    assert "WXYZ" not in r.text and LLM_KEY not in r.text


@pytest.mark.parametrize("answer, said", [
    (httpx.Response(200, json={"object": "list", "data": "none"}), "without a list of models"),
    (httpx.Response(200, json={"models": ["a"]}), "without a list of models"),
    (httpx.Response(200, text="<html>models</html>"), "without a list of models"),
    (httpx.Response(404, text="404 page not found"), "the server answered 404: 404 page not found"),
    (httpx.Response(301, headers={"location": "http://elsewhere.test/"}), "the server answered 301"),
], ids=["data-not-a-list", "other-shape", "not-json", "no-models-route", "redirect"])
def test_a_models_answer_that_is_not_a_list_is_a_502(api, fake, answer, said):
    fake.handlers["llm.test"] = lambda r: answer
    r = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert r.status_code == 502 and said in r.json()["error"]["message"], r.text
    assert "elsewhere.test" not in fake.hosts()  # a redirect is not followed


def test_a_model_list_too_long_to_hold_is_refused_and_a_silent_server_is_a_504(api, fake, monkeypatch):
    monkeypatch.setattr(destinations, "MODELS_MAX_BYTES", 64)
    fake.handlers["llm.test"] = lambda r: httpx.Response(200, json={"data": [{"id": "m" * 40}] * 4})
    big = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert big.status_code == 502 and "model list is over" in big.json()["error"]["message"]

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": []})
    monkeypatch.setattr(router_module, "MODELS_TIMEOUT_S", 0.05)
    fake.handlers["llm.test"] = hang
    slow = api.post("/satellites/llm/models", json={"base_url": "http://llm.test/v1"})
    assert slow.status_code == 504 and slow.json()["error"]["code"] == "llm_timeout"


def test_the_model_list_takes_what_an_action_takes_and_repeats_none_of_it(api, fake):
    for body in ({"base_url": "ftp://llm.test"}, {"base_url": "http://llm.test", "api_key_env": LLM_KEY},
                 {"base_url": "http://llm.test", "api_key": LLM_KEY},
                 {"base_url": f"http://user:{LLM_KEY}@llm.test/v1"}):
        r = api.post("/satellites/llm/models", json=body)
        assert r.status_code == 422 and LLM_KEY not in r.text, r.text
    assert fake.seen == []


# ---- the Test: POST /satellites/llm/test ------------------------------------------------------


def test_the_test_answers_with_the_reply_and_how_long_it_took(api, fake, monkeypatch):
    monkeypatch.setattr(destinations.tooling, "now_line", lambda: "Now it is noon.")
    async def slow_first_word(request):
        await asyncio.sleep(0.05)
        return sse(delta(role="assistant", content=""), delta(content="Hello there,\n"),
                   delta(content=" friend."), delta(finish="stop"))
    fake.handlers["llm.test"] = slow_first_word
    r = api.post("/satellites/llm/test", json=LLM | {"system": "Be brief."})
    body = r.json()
    assert r.status_code == 200, r.text
    assert (body["model"], body["reply"], body["token_limit"]) == ("tiny", "Hello there, friend.",
                                                                   "max_tokens")
    assert 50 <= body["first_token_ms"] <= body["total_ms"]
    # The question a turn would ask, with the form's own system prompt, and
    # nothing sent to speech.
    sent_body = json.loads(fake.sent("llm.test").content)
    assert sent_body["messages"] == [{"role": "system", "content": "Be brief.\n\nNow it is noon."},
                                     {"role": "user", "content": router_module.LLM_TEST_TEXT}]
    assert fake.hosts() == ["llm.test"]


def test_the_test_learns_max_completion_tokens_like_a_turn(api, fake, make):
    def server(request):
        if "max_tokens" in json.loads(request.content):
            return httpx.Response(400, json=WANTS_COMPLETION_TOKENS)
        return sse(delta(content="Hi."))
    fake.handlers["llm.test"] = server
    r = api.post("/satellites/llm/test", json=LLM)
    assert r.status_code == 200 and r.json()["token_limit"] == "max_completion_tokens"
    assert r.json()["reply"] == "Hi."
    # And a word's next turn goes straight to it.
    asyncio.run(make(LLM).handle_text(NID, "hey_jarvis", "hi"))
    assert "max_completion_tokens" in sent(fake)[-1] and len(sent(fake)) == 3


def test_a_test_the_provider_refuses_is_a_502_without_the_key(api, fake, monkeypatch):
    monkeypatch.setenv("SATELLITES_LLM_API_KEY", LLM_KEY)
    fake.handlers["llm.test"] = lambda r: httpx.Response(401, json={"error": {
        "message": "Authentication Fails, Your api key: ****WXYZ is invalid",
        "type": "authentication_error"}})
    r = api.post("/satellites/llm/test", json=LLM)
    assert r.status_code == 502 and r.json()["error"]["code"] == "llm"
    assert r.json()["error"]["message"] == ("the LLM refused the key in SATELLITES_LLM_API_KEY (401): "
                                            "Authentication Fails, Your api key: [key hidden] is invalid")
    assert LLM_KEY not in r.text and "WXYZ" not in r.text
    assert fake.sent("llm.test").headers["authorization"] == f"Bearer {LLM_KEY}"


def test_a_test_that_hangs_ends_at_the_ceiling_before_the_proxy_does(api, fake, monkeypatch):
    """voice-ui gives up on the hub after 30 s with a bare 504 of its own; the
    hub's sentence has to arrive first."""
    ui = (Path(__file__).resolve().parents[2] / "ui" / "app" / "main.py").read_text()
    proxy_read_s = float(re.search(r"timeout=httpx\.Timeout\(([\d.]+), connect=", ui).group(1))
    assert router_module.LLM_TEST_CEILING_S < proxy_read_s

    async def hang(request):
        await asyncio.sleep(5)
        return one_body("Too late.")
    fake.handlers["llm.test"] = hang
    monkeypatch.setattr(router_module, "LLM_TEST_CEILING_S", 0.05)
    r = api.post("/satellites/llm/test", json=LLM | {"timeout": 90})
    assert r.status_code == 504 and r.json()["error"] == {
        "message": "the model did not answer within 0.05 s", "type": "server_error",
        "param": None, "code": "llm_timeout"}


def test_the_test_takes_only_an_llm_destination_and_repeats_none_of_it(api, fake):
    for body in ({"type": "echo"}, LLM | {"api_key_env": LLM_KEY}, LLM | {"api_key": LLM_KEY},
                 {"type": "llm", "base_url": "http://llm.test/v1"}):
        r = api.post("/satellites/llm/test", json=body)
        assert r.status_code == 422 and LLM_KEY not in r.text, r.text
    assert fake.seen == []
