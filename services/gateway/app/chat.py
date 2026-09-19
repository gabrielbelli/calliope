"""POST /v1/chat/completions, which transcribes and never converses.

WHY A SPEECH GATEWAY ANSWERS A CHAT ROUTE AT ALL. An aggregator probes
/v1/chat/completions before it will list a provider, and a 404 there fails the
whole provider check — so the stack becomes invisible to everything behind that
aggregator, including /v1/audio/speech and /v1/audio/transcriptions, which work
perfectly. The route exists to pass that probe and to give an OpenAI-shaped
client a second way into the transcriber it already knows how to talk to.

NOTHING HERE INVENTS AN ASSISTANT MESSAGE, AND THAT IS THE WHOLE DESIGN. The
cheap way to pass a provider probe is a fake: accept the messages, return a
plausible sentence, collect the tick. There are exactly two strings this module
can put in an assistant message and neither is composed here — a transcript
that came out of stt-stack, or NO_MODEL_REPLY below, which is a constant in
this file. The failure that rules the design is not a rude answer, it is a
SILENT PROMOTION: a router, an agent or a summariser added later sees a
plausible reply, concludes this endpoint is a language model, and starts
sending it real traffic. The box has 6.5 GB of Chatterbox and 1.4 GB of
Parakeet resident and not one parameter of anything that could answer a
question, so every one of those requests would come back wrong with a 200 on
it. A canned sentence that says "this is not a language model" is the only
answer that cannot be mistaken for one.

THE HOUSE RULE, AS STATED IN services/stt/app/openai_api.py: every field is
either honoured or refused BY NAME, never accepted and dropped. It bites harder
here than anywhere else in the estate, because CreateChatCompletionRequest is
about thirty fields and this route can honour three of them. `temperature`,
`top_p`, `seed` and the rest configure a sampler, and there is no sampler — a
server that took them and returned the same fixed sentence would be telling a
caller their settings had landed on a model. REFUSED_BECAUSE below names every
one of them with the reason in the model's own terms, which is the difference
between "unsupported" and an answer a caller can act on.

`usage` IS OMITTED RATHER THAN FILLED WITH ZEROES, for the same reason. Nothing
in this process tokenises anything — the gateway installs fastapi, uvicorn and
httpx and no tokeniser — so a `prompt_tokens: 0` would be a measurement this
service did not take. `stream_options.include_usage` is refused by name and
says so.

STREAMING EMITS THE TRANSCRIPT AS ONE DELTA, NOT AS A DRIP. stt-stack's
buffered transcription answers whole; slicing it into timed fragments would
invent a latency profile a client then builds a progress bar on. The real
incremental surface is `stream=true` on /v1/audio/transcriptions, which is
honoured on the engine that genuinely emits before it finishes (faster-whisper:
a 297 s clip's first delta at 7.8 s of a 68.6 s transcription) and refused by
name on the one that cannot. This route sends role, one content delta, a
finish_reason and [DONE] — the shape openai-python's stream reader requires,
with no timing claim in it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from typing import Any, Iterator, NamedTuple

from voice_common.errors import ApiError

# The same vocabulary services/stt uses on its by-hand multipart validation, so
# a client can branch on one set of `code` strings across both surfaces.
CODE_MISSING = "missing_required_parameter"
CODE_INVALID = "invalid_value"
CODE_UNKNOWN_PARAM = "unknown_parameter"
CODE_UNSUPPORTED_PARAM = "unsupported_parameter"

# The three fields that reach something real. `model` is echoed and forwarded to
# stt-stack, which treats it exactly as it treats the one on
# /v1/audio/transcriptions: required, and unable to choose an engine. `messages`
# carries the audio. `stream` picks the response framing.
CHAT_FIELDS = frozenset({"model", "messages", "stream"})

_NO_SAMPLER = (
    "configures a sampler, and there is no language model behind this route to "
    "sample from. The assistant message is either a transcript from stt-stack "
    "or a fixed sentence; neither has a temperature")

# Every field CreateChatCompletionRequest defines that this route cannot
# honour, with the reason in the terms of the thing that is missing. A generic
# "unsupported" tells a caller nothing about whether a newer build could help;
# "there is no sampler" tells them to stop sending it.
REFUSED_BECAUSE: dict[str, str] = {
    "temperature": _NO_SAMPLER,
    "top_p": _NO_SAMPLER,
    "n": _NO_SAMPLER,
    "stop": _NO_SAMPLER,
    "seed": _NO_SAMPLER,
    "presence_penalty": _NO_SAMPLER,
    "frequency_penalty": _NO_SAMPLER,
    "logit_bias": _NO_SAMPLER,
    "prediction": _NO_SAMPLER,
    "reasoning_effort": _NO_SAMPLER,
    "max_tokens":
        "caps a transcript at a token count nothing here can measure. A "
        "transcript is as long as the speech was, and truncating one would "
        "hand back a sentence that stops mid-word with finish_reason 'stop' on "
        "it. Send a shorter clip",
    "max_completion_tokens":
        "caps a transcript at a token count nothing here can measure. A "
        "transcript is as long as the speech was, and truncating one would "
        "hand back a sentence that stops mid-word with finish_reason 'stop' on "
        "it. Send a shorter clip",
    "logprobs":
        "asks for per-token probabilities from a decoder this route does not "
        "run. POST /v1/audio/transcriptions honours include[]=logprobs on the "
        "engine that reports them",
    "top_logprobs":
        "asks for per-token probabilities from a decoder this route does not "
        "run. POST /v1/audio/transcriptions honours include[]=logprobs on the "
        "engine that reports them",
    "response_format":
        "constrains a generation into a schema, and the only content this "
        "route produces is a transcript. POST /v1/audio/transcriptions has a "
        "response_format of its own with json, text, verbose_json, srt and vtt "
        "in it",
    "tools":
        "would be called by a model, and there is none. This route transcribes "
        "audio and returns the text",
    "tool_choice":
        "would be called by a model, and there is none. This route transcribes "
        "audio and returns the text",
    "parallel_tool_calls":
        "would be called by a model, and there is none. This route transcribes "
        "audio and returns the text",
    "functions":
        "would be called by a model, and there is none. This route transcribes "
        "audio and returns the text",
    "function_call":
        "would be called by a model, and there is none. This route transcribes "
        "audio and returns the text",
    "modalities":
        "selects an output modality, and this route emits text only. POST "
        "/v1/audio/speech is the route that makes audio",
    "audio":
        "configures spoken OUTPUT. POST /v1/audio/speech is the route that "
        "makes audio, and it takes a voice and a response_format",
    "stream_options":
        "carries include_usage, and this route reports no usage at all: "
        "nothing in this process tokenises anything, so the counts would be "
        "invented. `usage` is omitted rather than returned as zeroes",
    "store":
        "asks this service to keep the exchange, and it keeps none. The run "
        "record tts-long holds is about synthesis and recognition, not about "
        "chat bodies",
    "metadata":
        "annotates a stored completion, and nothing is stored here for it to "
        "annotate",
    "user":
        "attributes a request for abuse tracking, and there is no per-user "
        "accounting behind this gateway. One key opens the whole door; see "
        "GATEWAY_API_KEYS",
    "service_tier":
        "selects a capacity pool, and this is one container with one queue in "
        "front of one GPU",
    "web_search_options":
        "belongs to a model that can search, and there is none",
}

# Content part types a message may carry beside `input_audio`, and why the rest
# are refused. `text` is accepted and IGNORED for content on purpose — see
# _audio: a caller pairs a prompt with their clip out of habit and the
# transcript is still the honest answer, so the text is not silently treated as
# an instruction.
_PART_REFUSED_BECAUSE: dict[str, str] = {
    "image_url":
        "needs a vision model, and this stack holds a recogniser and two "
        "synthesisers",
    "input_image":
        "needs a vision model, and this stack holds a recogniser and two "
        "synthesisers",
    "file":
        "uploads a document for a model to read. Send the audio as an "
        "input_audio part, or use POST /v1/audio/transcriptions",
    "refusal":
        "is something an assistant sends, not something this route reads",
}

# THE ONLY ASSISTANT MESSAGE THIS SERVICE EVER COMPOSES, and it is a constant so
# that it cannot drift into sounding like an answer. It says what the stack is,
# what this route does, and where the real routes are, because a caller who
# reaches here with a question has a client pointed at the wrong base URL and
# the reply is the only place they will read that.
NO_MODEL_REPLY = (
    "This is a speech gateway, not a language model: there is no chat model "
    "behind this endpoint and it will never answer a question. It exists so an "
    "OpenAI-shaped client can transcribe through the chat surface. Send a user "
    "message whose content is a list containing an input_audio part -- "
    '{"type": "input_audio", "input_audio": {"data": "<base64>", "format": '
    '"wav"}} -- and the transcript comes back as the assistant message. POST '
    "/v1/audio/transcriptions is the same thing without the chat envelope, "
    "POST /v1/audio/speech synthesises, and GET /v1/models lists what this "
    "deployment takes."
)

# An extension, not a MIME type: it becomes the filename stt-stack sees, and a
# filename is the one part of a multipart upload that a caller controls and a
# server writes down. Anything with a slash, a dot or a NUL in it is refused
# here rather than passed on, because the value arrives from a JSON body and
# the next hop treats it as a name.
_FORMAT = re.compile(r"^[A-Za-z0-9]{1,8}$")

# Spellings a client may send instead of bare base64. openai-python sends the
# bare form; a browser that built the part out of a FileReader result sends the
# whole data URI, and the difference is a `data:audio/wav;base64,` prefix that
# would otherwise fail to decode with a message about invalid base64 rather
# than about the prefix. Stripped rather than refused: the payload after it is
# exactly what the specification asks for, and `format` stays authoritative for
# what the bytes are.
_DATA_URI = re.compile(r"^data:[^;,]*(;[^;,]+)*;base64,", re.IGNORECASE)

# LINE BREAKS ARE WHAT base64(1) PRODUCES, AND THIS ROUTE IS THE ONE PEOPLE
# BUILD A REQUEST FOR BY HAND. `base64 clip.wav` wraps at 76 columns on both
# macOS and coreutils, `openssl base64` at 64, and MIME has required wrapping
# since RFC 2045 — so the obvious way to fill in the README's own curl example,
# `--data "$(base64 clip.wav)"`, produced "is not valid base64: Only base64
# data is allowed." That message sends the reader looking at their file. It is
# stripped for exactly the reason the data: prefix above is: the payload is
# what the specification asks for, and the difference is framing the sender's
# tooling added. `validate=True` survives the strip, so genuine rubbish is
# still refused by the same sentence.
_WHITESPACE = re.compile(r"\s+")

_CONTENT_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
                  "ogg": "audio/ogg", "opus": "audio/opus", "m4a": "audio/mp4",
                  "mp4": "audio/mp4", "webm": "audio/webm"}


# What a message may carry. `role` and `content` are read; the rest are read by
# nobody and that is the point of listing them.
#
# `refusal` AND `annotations` ARE HERE BECAUSE A CLIENT HANDS BACK WHAT IT WAS
# GIVEN. The ordinary way to hold a conversation with openai-python is
# `messages.append(completion.choices[0].message.model_dump())`, and an
# assistant message dumped that way carries `refusal: null` — a field this
# route puts in its OWN replies, one screen down in `completion`. Refusing it
# answered "Unrecognized request argument supplied: messages.1.refusal", which
# is not true twice over: the specification defines the field, and this service
# emitted it. A caller cannot act on a message that says their client invented
# something this service sent them.
#
# Read and ignored rather than honoured, like `name` above it and like a `text`
# part beside a clip: a previous turn's assistant text is not an instruction
# here, because nothing here follows instructions. Only `input_audio` is acted
# on, and it is acted on wherever it appears.
_MESSAGE_FIELDS = frozenset({"role", "content", "name", "refusal",
                             "annotations"})


class Audio(NamedTuple):
    """One decoded input_audio part, ready to be posted to stt-stack."""

    data: bytes
    filename: str
    content_type: str
    # Where it came from in the body, so an error about the bytes can name the
    # part rather than the request. `messages.1.content.0.input_audio.data` is
    # something a caller can find; "the audio" is not.
    param: str


class Ask(NamedTuple):
    """A validated request: what to answer with, and what to call it."""

    model: str
    stream: bool
    audio: Audio | None


def _bad(message: str, *, param: str | None = None,
         code: str = CODE_INVALID) -> ApiError:
    return ApiError(400, message, code=code, param=param)


def _unsupported(param: str, why: str) -> ApiError:
    return ApiError(400, f"Unsupported parameter: '{param}' {why}.",
                    code=CODE_UNSUPPORTED_PARAM, param=param)


def read(body: object) -> Ask:
    """Validate a chat body and pull the one thing this route can act on.

    Raises ApiError, which install_errors renders in the OpenAI envelope. The
    order is the order a caller would want to be told about their mistakes:
    the shape of the body, then the fields that cannot be honoured, then the
    fields that are wrong, then the messages.
    """
    if not isinstance(body, dict):
        raise _bad("request body must be a JSON object with 'model' and "
                   "'messages' in it.")

    for name in sorted(body):
        if name in CHAT_FIELDS:
            continue
        why = REFUSED_BECAUSE.get(name)
        if why is not None:
            raise _unsupported(name, why)
        # Not in the specification at all. OpenAI answers "Unrecognized request
        # argument supplied: x" and so does services/stt; the string is copied
        # deliberately so a client matching on it matches everywhere.
        raise _bad(f"Unrecognized request argument supplied: {name}",
                   param=name, code=CODE_UNKNOWN_PARAM)

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _bad("Missing required parameter: 'model'.", param="model",
                   code=CODE_MISSING)

    stream = body.get("stream", False)
    if stream is None:
        stream = False
    if not isinstance(stream, bool):
        raise _bad("'stream' must be true or false.", param="stream")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad("Missing required parameter: 'messages' must be a non-empty "
                   "array of messages.", param="messages", code=CODE_MISSING)

    return Ask(model=model.strip(), stream=stream, audio=_audio(messages))


def _audio(messages: list[Any]) -> Audio | None:
    """The one input_audio part in the conversation, or None.

    MORE THAN ONE IS REFUSED RATHER THAN CONCATENATED. Two clips in one request
    have no defined join: there is no silence between them, no speaker change
    recorded, and the transcript would read as one utterance that nobody spoke.
    stt-stack transcribes one file per call and that is the honest unit here
    too.
    """
    found: list[Audio] = []
    for index, message in enumerate(messages):
        where = f"messages.{index}"
        if not isinstance(message, dict):
            raise _bad(f"'{where}' must be an object with 'role' and "
                       "'content'.", param=where)
        role = message.get("role")
        if not isinstance(role, str) or not role:
            raise _bad(f"Missing required parameter: '{where}.role'.",
                       param=f"{where}.role", code=CODE_MISSING)
        for key in sorted(message):
            if key in _MESSAGE_FIELDS:
                continue
            if key in {"tool_calls", "tool_call_id", "function_call"}:
                raise _unsupported(
                    f"{where}.{key}",
                    "replays a tool exchange, and there is no model here to "
                    "have called a tool")
            if key == "audio":
                raise _unsupported(
                    f"{where}.{key}",
                    "refers by id to spoken output a previous completion "
                    "produced, and this route has never produced any: it "
                    "returns text. POST /v1/audio/speech is what makes audio "
                    "here")
            raise _bad(f"Unrecognized request argument supplied: {where}.{key}",
                       param=f"{where}.{key}", code=CODE_UNKNOWN_PARAM)
        found.extend(_parts(message.get("content"), where))

    if len(found) > 1:
        raise _bad(
            f"{len(found)} input_audio parts were sent and this route "
            "transcribes one clip per request; there is no defined way to join "
            "two recordings into one transcript. Send them as separate "
            "requests, or use POST /v1/audio/transcriptions.",
            param=found[1].param)
    return found[0] if found else None


def _parts(content: object, where: str) -> Iterator[Audio]:
    """Every input_audio part in one message's content."""
    if content is None or isinstance(content, str):
        # A plain string is the ordinary chat shape and carries no audio. It is
        # not refused: it is exactly what a probe sends, and NO_MODEL_REPLY is
        # the answer to it.
        return
    if not isinstance(content, list):
        raise _bad(f"'{where}.content' must be a string or an array of content "
                   "parts.", param=f"{where}.content")

    for index, part in enumerate(content):
        spot = f"{where}.content.{index}"
        if not isinstance(part, dict):
            raise _bad(f"'{spot}' must be a content-part object with a 'type'.",
                       param=spot)
        kind = part.get("type")
        if kind == "text":
            continue
        if kind != "input_audio":
            why = _PART_REFUSED_BECAUSE.get(kind if isinstance(kind, str) else "")
            if why is not None:
                raise _unsupported(f"{spot}.type", f"is '{kind}', which {why}")
            raise _bad(f"'{spot}.type' is {kind!r}; this route reads 'text' and "
                       "'input_audio' parts.", param=f"{spot}.type")
        yield _one(part.get("input_audio"), f"{spot}.input_audio")


def _one(payload: object, where: str) -> Audio:
    if not isinstance(payload, dict):
        raise _bad(f"'{where}' must be an object with 'data' and 'format'.",
                   param=where)

    fmt = payload.get("format")
    if not isinstance(fmt, str) or not fmt:
        raise _bad(f"Missing required parameter: '{where}.format'.",
                   param=f"{where}.format", code=CODE_MISSING)
    if not _FORMAT.match(fmt):
        # See _FORMAT: this value becomes a filename on the next hop.
        raise _bad(f"'{where}.format' must be a bare extension such as 'wav' "
                   f"or 'mp3'; got {fmt!r}.", param=f"{where}.format")

    raw = payload.get("data")
    if not isinstance(raw, str) or not raw:
        raise _bad(f"Missing required parameter: '{where}.data'.",
                   param=f"{where}.data", code=CODE_MISSING)
    try:
        data = base64.b64decode(
            _WHITESPACE.sub("", _DATA_URI.sub("", raw.strip())), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _bad(f"'{where}.data' is not valid base64: {exc}.",
                   param=f"{where}.data") from exc
    if not data:
        raise _bad(f"'{where}.data' decoded to zero bytes.",
                   param=f"{where}.data")

    lowered = fmt.lower()
    return Audio(data=data, filename=f"audio.{lowered}",
                 content_type=_CONTENT_TYPES.get(lowered,
                                                 "application/octet-stream"),
                 param=f"{where}.data")


def identifier() -> str:
    """OpenAI's own prefix, because clients log and correlate on it."""
    return "chatcmpl-" + uuid.uuid4().hex


def completion(*, model: str, text: str, ident: str,
               created: int | None = None) -> dict[str, Any]:
    """The buffered body.

    No `usage` key. The specification marks it optional and every count this
    service could put in it would be invented; see the module docstring.
    """
    return {
        "id": ident,
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text, "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }],
    }


def _event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def stream(*, model: str, text: str, ident: str,
           created: int | None = None) -> Iterator[bytes]:
    """The same answer as `completion`, in the four events a reader expects.

    Role, one content delta, a finish_reason chunk and [DONE]. One delta and
    not many on purpose — see the module docstring: the transcript arrives
    whole, so fragments here would be a latency claim rather than a fact.
    """
    base = {"id": ident, "object": "chat.completion.chunk",
            "created": created if created is not None else int(time.time()),
            "model": model}
    yield _event({**base, "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": ""},
         "logprobs": None, "finish_reason": None}]})
    yield _event({**base, "choices": [
        {"index": 0, "delta": {"content": text}, "logprobs": None,
         "finish_reason": None}]})
    yield _event({**base, "choices": [
        {"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}]})
    # openai-python's stream reader stops on this sentinel and nothing else.
    yield b"data: [DONE]\n\n"
