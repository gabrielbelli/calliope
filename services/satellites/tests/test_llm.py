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
  * a base URL pasted as the whole endpoint, which answered 404 on every turn.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.destinations import Llm, Request
from app.router import Rules, RuleSet, Router
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
