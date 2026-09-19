"""The three /v1 routes an aggregator's provider check asks for.

Each of them was a 404 from this service while the thing behind it worked, or
worked here and nowhere else. One test per rule, named for the rule.

WHAT THESE TESTS ARE FOR, SPECIFICALLY. Not "the route exists" -- a test that
asserts a line of code is present passes the day the line is present and says
nothing about what the line does. Every assertion below is on the wire: the
request a mock backend actually received, the status a caller actually gets,
the bytes of an SSE stream a reader would actually parse.
"""

from __future__ import annotations

import base64
import json

import pytest
from conftest import MockBackend, Slow, Unreachable, gateway

CHAT = "/v1/chat/completions"
TRANSLATIONS = "/v1/audio/translations"

# 44 bytes of RIFF header and nothing else. It is never decoded: the mock
# stt-stack answers whatever it is told to, and the point of the bytes is that
# they survive base64 and arrive at the backend byte for byte.
CLIP = (b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")


def audio_part(data: bytes = CLIP, fmt: str = "wav") -> dict:
    return {"type": "input_audio",
            "input_audio": {"data": base64.b64encode(data).decode(),
                            "format": fmt}}


def ask(*parts, model: str = "whisper-1", **extra) -> dict:
    """A chat body carrying whatever content parts are handed to it."""
    content: object = list(parts) if parts else "what is the capital of Brazil?"
    return {"model": model,
            "messages": [{"role": "user", "content": content}],
            **extra}


def transcribing(text: str = "Here is the change to make.",
                 engine: str = "parakeet") -> MockBackend:
    """A mock stt-stack that answers a transcription the way the real one does."""
    stt = MockBackend("stt-stack")
    stt.reply = lambda record: (
        200,
        {"content-type": "application/json", "x-stt-engine": engine,
         "x-realtime-factor": "9.1"},
        json.dumps({"text": text}).encode())
    return stt


def events(raw: bytes) -> list[str]:
    """The `data:` payloads of an SSE body, in order."""
    return [line[len("data: "):] for line in raw.decode().split("\n\n")
            if line.startswith("data: ")]


# ----------------------------------------------------- translations, routed --


async def test_a_translation_reaches_the_only_stt_backend(monkeypatch, backends):
    """THE DEFECT: stt-stack has answered POST /v1/audio/translations all along
    and this table never carried it, so the route was a 404 through the only
    published port -- the DELETE /jobs/{id}/audio failure on the surface this
    service exists for."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(TRANSLATIONS, content=b"multipart-ish")

    assert response.status_code == 200
    assert stt.last["path"] == TRANSLATIONS
    assert stt.last["method"] == "POST"
    assert stt.last["body"] == b"multipart-ish"
    assert not tts.seen and not long.seen


async def test_a_translation_body_is_streamed_and_not_buffered(monkeypatch,
                                                               backends):
    """An hour of wav is 100 MB+. The sibling route streams and so must this
    one, or the gateway doubles resident memory on a box already holding 6.5 GB
    of Chatterbox."""
    stt, tts, long = backends

    async def chunks():
        yield b"first-"
        yield b"second"

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(TRANSLATIONS, content=chunks())

    assert response.status_code == 200
    assert stt.last["body"] == b"first-second"
    assert stt.last["chunks"] > 1, "the upload was buffered before forwarding"


async def test_a_translation_refusal_from_the_backend_is_not_rewrapped(
        monkeypatch, backends):
    """Under Parakeet stt-stack answers 400 naming the engine and the variable
    that changes it. That is the answer a caller can act on, and it is the whole
    reason routing beats the 404 this table used to give."""
    stt, tts, long = backends
    envelope = json.dumps({"error": {
        "message": "Unsupported value: translation requires an engine with a "
                   "translate task, and this deployment loaded 'parakeet'",
        "type": "invalid_request_error", "param": "model",
        "code": "unsupported_value"}}).encode()
    stt.reply = lambda record: (400, {"content-type": "application/json"},
                                envelope)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(TRANSLATIONS, content=b"x")

    assert response.status_code == 400
    assert response.content == envelope


# --------------------------------------------------------- retrieve a model --


async def test_every_advertised_model_can_also_be_retrieved(monkeypatch,
                                                            backends):
    """The failure this rules out: a provider that lists models it then 404s on.

    Walked over the published list rather than a literal, so an engine added to
    GATEWAY_LONG_MODELS is covered the moment it is advertised.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox,chatterbox-turbo") as (client, _):
        listed = (await client.get("/v1/models")).json()["data"]
        for row in listed:
            response = await client.get("/v1/models/" + row["id"])
            assert response.status_code == 200, row["id"]
            assert response.json() == row

    assert not stt.seen and not tts.seen and not long.seen


async def test_a_retrieved_row_names_the_engine_that_owns_it(monkeypatch,
                                                             backends):
    """`owned_by` is what lets a client tell an STT name from a TTS one, and it
    has to say the same thing on both endpoints or the field is noise."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox") as (client, _):
        owners = {row["id"]: row["owned_by"]
                  for row in (await client.get("/v1/models")).json()["data"]}
        for model_id, owner in owners.items():
            assert (await client.get(f"/v1/models/{model_id}")).json()["owned_by"] \
                == owner

    assert owners["whisper-1"] == "stt-stack"
    assert owners["parakeet"] == "stt-stack"
    assert owners["kokoro"] == "tts-stack"
    assert owners["chatterbox"] == "tts-long"


async def test_retrieve_takes_the_same_spelling_the_router_takes(monkeypatch,
                                                                 backends):
    """`speech` routes on `model.strip().lower()`, so "Chatterbox" is audio from
    the long backend. A retrieve that 404d on the same string would be this
    service disagreeing with itself about what a model name is."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox") as (client, _):
        response = await client.get("/v1/models/Chatterbox")

    assert response.status_code == 200
    # The CANONICAL spelling comes back, so a client that stores what it
    # retrieved stores the id the list published.
    assert response.json()["id"] == "chatterbox"


async def test_an_unknown_model_is_a_404_in_the_envelope(monkeypatch, backends):
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.get("/v1/models/banana")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"
    assert "/v1/models" in error["message"]


async def test_retrieving_a_disabled_long_model_says_which_variable_enables_it(
        monkeypatch, backends):
    """Two endpoints saying "no such model" and "add it to GATEWAY_LONG_MODELS"
    about one string would send an operator looking for two faults."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox") as (client, _):
        response = await client.get("/v1/models/chatterbox-turbo")

    assert response.status_code == 404
    message = response.json()["error"]["message"]
    assert "GATEWAY_LONG_MODELS" in message
    assert "TTS_ENGINES" in message


# ------------------------------------------------------------ chat, honestly --


async def test_a_chat_message_with_audio_comes_back_as_its_transcript(
        monkeypatch, backends):
    """The one thing this route does. The clip reaches stt-stack as a real
    multipart upload and the text it answers with is the assistant message."""
    _, tts, long = backends
    stt = transcribing("Here is the change to make.")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 200
    assert stt.last["path"] == "/v1/audio/transcriptions"
    assert stt.last["method"] == "POST"
    # The bytes survived base64 and the multipart hop untouched.
    assert CLIP in stt.last["body"]
    assert b'name="model"' in stt.last["body"]
    assert b"whisper-1" in stt.last["body"]

    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["id"].startswith("chatcmpl-")
    assert payload["model"] == "whisper-1"
    choice = payload["choices"][0]
    assert choice["message"] == {"role": "assistant",
                                 "content": "Here is the change to make.",
                                 "refusal": None}
    assert choice["finish_reason"] == "stop"
    # The engine that actually ran, on the response, exactly as stt-stack puts
    # it on its own. Honesty rather than obedience: `model` echoes the caller.
    assert response.headers["x-stt-engine"] == "parakeet"


async def test_the_transcript_is_the_only_text_that_can_come_back(monkeypatch,
                                                                  backends):
    """NOTHING HERE INVENTS AN ASSISTANT MESSAGE. Whatever stt-stack says is
    what the caller reads, even when it reads like nonsense -- because the
    alternative is a service that decides when a transcript is good enough and
    writes its own."""
    _, tts, long = backends
    stt = transcribing("qwerty uiop")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.json()["choices"][0]["message"]["content"] == "qwerty uiop"


async def test_a_text_only_request_says_this_is_not_a_language_model(
        monkeypatch, backends):
    """THE DEFECT A PLAUSIBLE ANSWER WOULD CAUSE: a router added later concludes
    from a fluent reply that the NAS runs an LLM and sends it real traffic.

    So the fixed reply has to be unmistakable, and the assertions are on that
    rather than on its exact wording: it denies being a model, and it names the
    routes that do work.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask())

    assert response.status_code == 200
    text = response.json()["choices"][0]["message"]["content"]
    assert "not a language model" in text
    assert "input_audio" in text
    assert "/v1/audio/transcriptions" in text
    # The question was never answered, and no backend was troubled with it.
    assert "Bras" not in text
    assert not stt.seen and not tts.seen and not long.seen


async def test_usage_is_omitted_rather_than_filled_with_zeroes(monkeypatch,
                                                               backends):
    """Nothing in this process tokenises anything -- fastapi, uvicorn, httpx and
    no tokeniser -- so a `prompt_tokens: 0` would be a measurement never taken.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        payload = (await client.post(CHAT, json=ask())).json()

    assert "usage" not in payload


async def test_stream_true_is_honoured_because_probes_test_it(monkeypatch,
                                                              backends):
    """Role, one content delta, a finish_reason and [DONE] -- the shape
    openai-python's stream reader requires."""
    _, tts, long = backends
    stt = transcribing("streamed transcript")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT,
                                     json=ask(audio_part(), stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = events(response.content)
    assert payloads[-1] == "[DONE]"

    chunks = [json.loads(p) for p in payloads[:-1]]
    assert [c["object"] for c in chunks] == ["chat.completion.chunk"] * 3
    assert len({c["id"] for c in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[1]["choices"][0]["delta"]["content"] == "streamed transcript"
    assert chunks[2]["choices"][0]["finish_reason"] == "stop"


async def test_a_streamed_transcript_is_one_delta_and_not_a_fake_drip(
        monkeypatch, backends):
    """stt-stack's buffered transcription arrives whole. Slicing it into timed
    fragments would invent a latency profile a client builds a progress bar on;
    the genuinely incremental surface is stream=true on
    /v1/audio/transcriptions, which faster-whisper can do and Parakeet cannot.
    """
    _, tts, long = backends
    stt = transcribing("one two three four five six seven eight")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT,
                                     json=ask(audio_part(), stream=True))

    chunks = [json.loads(p) for p in events(response.content)[:-1]]
    with_text = [c for c in chunks
                 if c["choices"][0]["delta"].get("content")]
    assert len(with_text) == 1


async def test_stream_true_also_works_without_any_audio(monkeypatch, backends):
    """A provider probe sends text and stream together. Both halves have to
    answer or the check fails on the combination neither test covered."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(stream=True))

    payloads = events(response.content)
    assert payloads[-1] == "[DONE]"
    text = json.loads(payloads[1])["choices"][0]["delta"]["content"]
    assert "not a language model" in text


@pytest.mark.parametrize("field,value", [
    ("temperature", 0.7),
    ("top_p", 0.9),
    ("max_tokens", 16),
    ("max_completion_tokens", 16),
    ("n", 2),
    ("seed", 7),
    ("stop", ["\n"]),
    ("logprobs", True),
    ("response_format", {"type": "json_object"}),
    ("tools", []),
    ("tool_choice", "auto"),
    ("stream_options", {"include_usage": True}),
    ("user", "someone"),
    ("metadata", {"k": "v"}),
    ("modalities", ["text"]),
    ("audio", {"voice": "alloy", "format": "wav"}),
])
async def test_a_field_this_route_cannot_honour_is_refused_by_name(
        monkeypatch, backends, field, value):
    """THE HOUSE RULE: every field is honoured or refused BY NAME, never
    accepted and dropped.

    It bites hardest here. CreateChatCompletionRequest is about thirty fields
    and this route can honour three, so a lenient server would take a caller's
    temperature, return the same fixed sentence, and have told them their
    settings landed on a model.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(**{field: value}))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == field
    assert error["code"] == "unsupported_parameter"
    # The reason, in the terms of the thing that is missing. "unsupported" is
    # what a message says when nobody looked.
    assert len(error["message"]) > len(f"Unsupported parameter: '{field}'.")
    assert not stt.seen


async def test_an_invented_field_is_refused_with_openais_own_sentence(
        monkeypatch, backends):
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(banana=1))

    error = response.json()["error"]
    assert error["code"] == "unknown_parameter"
    assert error["message"] == "Unrecognized request argument supplied: banana"


async def test_every_refusal_carries_all_four_envelope_fields(monkeypatch,
                                                              backends):
    """openai-python reads `error.message`; a client generated from the schema
    reads `param` and `code` whether or not they are null."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        for payload in ({"model": "whisper-1"},
                        {"messages": []},
                        {"model": "whisper-1", "messages": [], "stream": 1},
                        ask(temperature=1)):
            response = await client.post(CHAT, json=payload)
            assert response.status_code == 400
            assert set(response.json()["error"]) == {"message", "type", "param",
                                                     "code"}


async def test_an_image_part_is_refused_and_names_what_is_missing(monkeypatch,
                                                                  backends):
    stt, tts, long = backends
    part = {"type": "image_url", "image_url": {"url": "http://x/y.png"}}
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(part))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "messages.0.content.0.type"
    assert "vision" in error["message"]


async def test_two_clips_in_one_request_are_refused_rather_than_joined(
        monkeypatch, backends):
    """Two recordings have no defined join: no silence between them, no speaker
    change recorded, and the transcript would read as one utterance nobody
    spoke."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT,
                                     json=ask(audio_part(), audio_part()))

    assert response.status_code == 400
    assert "one clip per request" in response.json()["error"]["message"]
    assert not stt.seen


async def test_a_text_part_beside_the_clip_is_not_read_as_an_instruction(
        monkeypatch, backends):
    """A caller pairs a prompt with their clip out of habit. The transcript is
    still the honest answer, and treating the text as an instruction is the
    invention this route exists to refuse."""
    _, tts, long = backends
    stt = transcribing("the transcript")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(
            {"type": "text", "text": "summarise this in one word"},
            audio_part()))

    assert response.json()["choices"][0]["message"]["content"] == "the transcript"


async def test_bad_base64_names_the_part_it_came_from(monkeypatch, backends):
    """`messages.0.content.0.input_audio.data` is something a caller can find in
    their own request; "the audio" is not."""
    stt, tts, long = backends
    part = {"type": "input_audio",
            "input_audio": {"data": "not base64 at all!", "format": "wav"}}
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(part))

    assert response.status_code == 400
    assert response.json()["error"]["param"] == \
        "messages.0.content.0.input_audio.data"


async def test_a_data_uri_prefix_is_stripped_rather_than_failing_to_decode(
        monkeypatch, backends):
    """A browser that built the part from a FileReader result sends the whole
    URI. Refusing it would report invalid base64 rather than the prefix."""
    _, tts, long = backends
    stt = transcribing()
    part = {"type": "input_audio",
            "input_audio": {"format": "wav",
                            "data": "data:audio/wav;base64,"
                                    + base64.b64encode(CLIP).decode()}}

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(part))

    assert response.status_code == 200
    assert CLIP in stt.last["body"]


async def test_a_format_that_is_not_an_extension_never_reaches_the_backend(
        monkeypatch, backends):
    """`format` becomes the FILENAME on the next hop, and it arrives from a JSON
    body. A slash or a dot in it is a name the backend writes down, so it is
    refused here rather than passed on."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        for bad in ("../../etc/passwd", "wav/../x", "wa v", ""):
            response = await client.post(CHAT, json=ask(audio_part(fmt=bad)))
            assert response.status_code == 400, bad
            assert response.json()["error"]["param"].endswith("format"), bad

    assert not stt.seen


async def test_the_chat_route_never_reaches_a_tts_backend(monkeypatch, backends):
    """It is a recognition surface. A chat request that woke Chatterbox would be
    a nine-minute job nobody asked for."""
    _, tts, long = backends
    stt = transcribing()
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        await client.post(CHAT, json=ask(audio_part()))
        await client.post(CHAT, json=ask())

    assert not tts.seen and not long.seen


async def test_a_backend_refusal_reaches_the_chat_caller_unrewrapped(
        monkeypatch, backends):
    """stt-stack says which field was wrong and which engine is loaded. Anything
    written here instead would be a worse version of that with an envelope round
    it."""
    stt, tts, long = backends
    envelope = json.dumps({"error": {"message": "'file' is empty.",
                                     "type": "invalid_request_error",
                                     "param": "file",
                                     "code": "invalid_value"}}).encode()
    stt.reply = lambda record: (400, {"content-type": "application/json"},
                                envelope)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 400
    assert response.content == envelope


async def test_a_200_with_no_text_field_is_502_and_not_an_empty_message(
        monkeypatch, backends):
    """An empty assistant message reads as "the clip was silent". A backend that
    answered a shape this service cannot read is a different fact and says so.
    """
    stt, tts, long = backends
    stt.reply = lambda record: (200, {"content-type": "application/json"},
                                b'{"transcript":"wrong key"}')

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "backend_error"


async def test_a_down_stt_container_is_503_and_names_itself(monkeypatch,
                                                            backends):
    _, tts, long = backends
    async with gateway(monkeypatch, stt=Unreachable(), tts=tts,
                       long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 503
    assert "stt-stack" in response.json()["error"]["message"]
    assert response.headers["Retry-After"] == "30"


async def test_a_wedged_stt_container_is_504_with_the_way_out(monkeypatch,
                                                              backends):
    _, tts, long = backends
    async with gateway(monkeypatch, stt=Slow(), tts=tts,
                       long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 504
    message = response.json()["error"]["message"]
    assert "stt-stack" in message
    # The same sentence the transcription route's timeout carries; the rate and
    # the way out are single-sourced on the Backend row.
    assert "Split the recording." in message


async def test_a_malformed_body_is_the_one_400_that_is_not_about_a_field(
        monkeypatch, backends):
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, content=b"{not json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_value"


# ------------------------------------------- the one body this process holds --


async def test_a_chat_body_over_the_ceiling_is_refused_and_never_buffered(
        monkeypatch, backends):
    """THE ONLY ROUTE HERE THAT HOLDS AUDIO, AND THE CONTAINER IS 512 MB.

    compose.yaml sizes this process at 512 MB on the written grounds that it
    "moves bytes between two sockets and never holds them ... the single
    buffered body is /v1/audio/speech's JSON, which is kilobytes". A chat body
    carries a base64 clip and cannot be streamed, and it was measured at 3.8x
    its own size resident while it is being read -- so an uncapped 400 MB body
    is 1.5 GB and an OOM kill of the only published port.
    """
    monkeypatch.setenv("GATEWAY_CHAT_MAX_BYTES", "4096")
    stt, tts, long = backends
    body = json.dumps(ask(audio_part(b"\x00" * 8192))).encode()
    assert len(body) > 4096

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(
            CHAT, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "upload_too_large"
    # The way out, not just the refusal: the streaming route takes the clip.
    assert "/v1/audio/transcriptions" in error["message"]
    assert not stt.seen, "an oversized body still reached a backend"


async def test_the_ceiling_counts_bytes_rather_than_believing_content_length(
        monkeypatch, backends):
    """A chunked request declares no length, and `curl -H 'Transfer-Encoding:
    chunked'` is one flag away. A check that stopped at the header would hold
    only against the callers who were never the problem."""
    monkeypatch.setenv("GATEWAY_CHAT_MAX_BYTES", "4096")
    stt, tts, long = backends
    body = json.dumps(ask(audio_part(b"\x00" * 8192))).encode()

    async def chunked():
        for start in range(0, len(body), 1024):
            yield body[start:start + 1024]

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        request = client.build_request(
            "POST", CHAT, content=chunked(),
            headers={"content-type": "application/json"})
        assert "content-length" not in request.headers, (
            "this test only means something while the request is chunked")
        response = await client.send(request)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert not stt.seen


async def test_a_body_under_the_ceiling_is_untouched_by_it(monkeypatch,
                                                           backends):
    """The ceiling is a ceiling, not a tax. The ordinary clip still lands."""
    monkeypatch.setenv("GATEWAY_CHAT_MAX_BYTES", "1048576")
    _, tts, long = backends
    stt = transcribing("under the ceiling")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(audio_part()))

    assert response.status_code == 200
    assert CLIP in stt.last["body"]


# ------------------------------------ what a client actually puts on the wire --


async def test_line_wrapped_base64_is_read_rather_than_called_invalid(
        monkeypatch, backends):
    """`base64 clip.wav` WRAPS AT 76 COLUMNS, on macOS and on coreutils alike,
    and this is the route people build a request for by hand.

    Filling in the README's own curl example with `$(base64 clip.wav)` met "is
    not valid base64: Only base64 data is allowed.", which sends the reader off
    to look at their file. The prefix of a data: URI is already stripped for
    exactly this reason; the wrapping its own tooling added is the same fact.
    """
    _, tts, long = backends
    stt = transcribing()
    # A clip long enough to be wrapped at all: CLIP alone is 44 bytes, which
    # base64 renders in 60 characters and no tool would break.
    clip = CLIP + bytes(range(256)) * 4
    encoded = base64.b64encode(clip).decode()
    wrapped = "\n".join(encoded[at:at + 76] for at in range(0, len(encoded), 76))
    assert wrapped.count("\n") > 5

    part = {"type": "input_audio",
            "input_audio": {"data": wrapped, "format": "wav"}}
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json=ask(part))

    assert response.status_code == 200
    # Byte for byte, so the strip cannot be a decode that quietly lost data.
    assert clip in stt.last["body"]


async def test_rubbish_is_still_rubbish_after_the_whitespace_is_stripped(
        monkeypatch, backends):
    """THE STRIP MUST NOT BECOME "ACCEPT ANYTHING", and `validate=False` is
    exactly what that would look like: it does not REFUSE a character outside
    the alphabet, it DELETES it and closes the gap. Four mangled characters in
    the middle of a payload -- one bad copy-paste, one truncated shell variable
    -- then decode to a clip three bytes short with everything after the damage
    shifted, and stt-stack transcribes whatever that turned out to be and
    answers 200. Measured on the payload below: lenient decoding returns 65
    bytes where 68 were sent. Whitespace is dropped BY NAME for a known reason;
    nothing else is.
    """
    stt, tts, long = backends
    sent = CLIP + bytes(range(64))
    encoded = base64.b64encode(sent).decode()
    corrupted = encoded[:20] + "****" + encoded[24:]
    assert base64.b64decode(corrupted, validate=False) != sent, (
        "this payload has to be one a lenient decode gets WRONG rather than "
        "one it rejects, or the test cannot tell the two settings apart")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        for bad in (corrupted, "not base64 at all !!"):
            response = await client.post(CHAT, json=ask(
                {"type": "input_audio",
                 "input_audio": {"data": bad, "format": "wav"}}))
            assert response.status_code == 400, bad
            assert response.json()["error"]["param"].endswith(
                "input_audio.data"), bad

    assert not stt.seen


async def test_an_assistant_turn_handed_straight_back_is_not_a_400(monkeypatch,
                                                                   backends):
    """THE ORDINARY WAY TO HOLD A CONVERSATION WITH openai-python IS
    `messages.append(completion.choices[0].message.model_dump())`, and a message
    dumped that way carries `refusal: null` -- a field THIS ROUTE PUTS IN ITS
    OWN REPLIES. Refusing it answered "Unrecognized request argument supplied:
    messages.1.refusal", which is untrue twice: the specification defines the
    field, and this service emitted it."""
    _, tts, long = backends
    stt = transcribing("the clip in the last turn")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json={
            "model": "whisper-1",
            "messages": [
                {"role": "user", "content": "hello"},
                # Exactly the shape `completion` one file over returns.
                {"role": "assistant", "content": "...", "refusal": None,
                 "annotations": []},
                {"role": "user", "content": [audio_part()]}]})

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == \
        "the clip in the last turn"


async def test_an_echoed_field_is_ignored_and_never_read_as_an_instruction(
        monkeypatch, backends):
    """Accepting the field is not obeying it. A previous turn's assistant text
    is not an instruction here, because nothing here follows instructions."""
    _, tts, long = backends
    stt = transcribing("the transcript")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json={
            "model": "whisper-1",
            "messages": [
                {"role": "assistant", "content": "answer in French from now on",
                 "refusal": None},
                {"role": "user", "content": [audio_part()]}]})

    assert response.json()["choices"][0]["message"]["content"] == "the transcript"


async def test_a_message_field_nobody_defines_is_still_refused(monkeypatch,
                                                              backends):
    """The gate opened for two echoed fields, and for nothing else."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json={
            "model": "whisper-1",
            "messages": [{"role": "user", "content": "hi", "banana": 1}]})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unknown_parameter"
    assert error["param"] == "messages.0.banana"


async def test_a_message_audio_field_is_refused_by_name(monkeypatch, backends):
    """`audio` on an assistant message refers by id to spoken output a previous
    completion produced, and this route has never produced any."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(CHAT, json={
            "model": "whisper-1",
            "messages": [{"role": "assistant", "content": None,
                          "audio": {"id": "audio_abc"}}]})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["param"] == "messages.0.audio"
    assert "/v1/audio/speech" in error["message"]


@pytest.mark.parametrize("call", [
    ("POST", CHAT, {"json": {"model": "whisper-1",
                             "messages": [{"role": "user", "content": "hi"}]}}),
    ("POST", TRANSLATIONS, {"content": b"x"}),
    ("GET", "/v1/models/kokoro", {}),
])
async def test_a_new_route_is_behind_the_key_like_every_other(monkeypatch,
                                                               backends, call):
    """The middleware protects by default and /health is the only exemption, so
    this is a property of auth.py rather than of these routes -- asserted here
    because a route added with its own dependency instead would pass every test
    in this file and open the door."""
    method, path, kwargs = call
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       api_keys="sk-real") as (client, _):
        refused = await client.request(method, path, **kwargs)
        # Checked INSIDE the context and before the authorised call, because a
        # 401 that still forwarded the body would be invisible afterwards.
        assert not stt.seen and not tts.seen and not long.seen, (
            "a request refused at the door still reached a backend")
        allowed = await client.request(
            method, path, headers={"Authorization": "Bearer sk-real"}, **kwargs)

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200


async def test_the_chat_route_writes_one_log_line(monkeypatch, backends, caplog):
    """One line per request is the whole observability budget, and a refusal
    that leaves no line is the failure nobody can be asked about afterwards."""
    stt, tts, long = backends
    with caplog.at_level("INFO", logger="voice-gateway"):
        async with gateway(monkeypatch, stt=stt, tts=tts,
                           long=long) as (client, _):
            await client.post(CHAT, json=ask())
            await client.post(CHAT, json=ask(temperature=1))

    lines = [r.getMessage() for r in caplog.records
             if CHAT in r.getMessage()]
    assert len(lines) == 2
    assert "status=200-noaudio" in lines[0]
    assert "status=400-unsupported_parameter" in lines[1]
