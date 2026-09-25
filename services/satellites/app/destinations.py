"""Where a transcript goes after a wake word, and what comes back to be spoken.

Five kinds, told apart by "type" in a wake word's action:

    ha_conversation  Home Assistant's REST conversation API
    ha_assist        a Home Assistant Assist pipeline, over HA's websocket API
    llm              any OpenAI-compatible /chat/completions, streamed
    webhook          a JSON POST to a URL of the operator's choosing
    echo             the transcript itself, so a fresh hub can be tested end
                     to end with nothing else running

A trigger word has no action at all: the hub publishes that it was heard, and
Home Assistant's own automations decide what it does (main.py).

Each one answers with the text to speak, or None for "nothing to say", and
raises DestinationError for anything that went wrong. answer() gives the same
text as it arrives, in pieces, so the first sentence can be spoken before the
last one exists; only the llm destination really streams, the others answer
in one piece. Nothing here decides where the reply is played.

WHAT A DESTINATION IS TOLD (Request): the transcript, the language it was
spoken in and the language the answer has to be in, and in a conversation the
turns so far (history) and a small dict of its own (state) that lives as long
as the conversation. The llm destination sends the history as chat messages;
the webhook sends it as a list; Home Assistant keeps its own context and gets
only its conversation_id back through state; echo ignores all of it.

"DID NOT UNDERSTAND" IS NOT AN ERROR, BUT IT IS WORTH KNOWING. Home Assistant
answers a sentence no intent matches with response_type "error" and a spoken
"Sorry, I couldn't understand that". That sentence is the answer, and it is
spoken as before; NotUnderstood carries it so that a wake word with a
`fallback` can hand the same transcript to a conversation instead.

SECRETS ARE NAMED, NEVER HELD. A destination that needs a credential carries
the NAME of an environment variable (token_env, api_key_env) and reads the
value at call time. wake_words.json is written by an API that answers GET with
every action, so a token stored in it would be one GET away from anyone with
an API key, and in every backup of the data volume. Two things enforce that
rather than hope for it:

  * every model forbids unknown fields, so {"token": "..."} pasted into an
    action is a validation error rather than a secret quietly saved;
  * an env var name must look like one (upper case, digits, underscore). A
    Home Assistant long-lived token is a JWT and an OpenAI key starts "sk-";
    neither fits, so pasting the value where the name belongs is refused too.

URLs with a user:password part are refused for the same reason.

NO SSRF FILTERING, AND THAT IS DELIBERATE. Every URL here comes from the
operator's own configuration, set through PUT /satellites/wake-words, which
sits behind the same API keys (or the gateway) as adopting satellites and
flashing firmware. The legitimate targets are exactly the addresses an SSRF
filter would block: Home Assistant on the LAN, an LLM on localhost, Node-RED in
the next container. A filter would break the main use and protect nothing,
because whoever can save an action can already reflash every satellite. The
trust boundary is who may write the configuration, not what it says. Redirects
are not followed, so a destination cannot bounce a request, and its bearer
token, to a host the action never named.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, AsyncIterator, ClassVar, Literal

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from . import language as lang

# Upper case only, on purpose: see the module docstring. Real env var names
# may be lower case, but refusing that costs nothing here and turns "pasted
# the token into token_env" into a 422 instead of a secret in the file.
ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
HTTP_URL = r"^https?://[^/\s?#]+(/[^\s]*)?$"


class DestinationError(Exception):
    """A destination failed in a way worth one sentence to the operator."""


class NotUnderstood(DestinationError):
    """The destination answered, but did not understand the request. `speech`
    is what it said about that, spoken when nothing takes over."""

    def __init__(self, message: str, speech: str | None = None):
        super().__init__(message)
        self.speech = speech


@dataclass(frozen=True)
class Turn:
    """One exchange of a conversation: what was said, and what was answered
    (as much of it as was spoken, when the answer was interrupted)."""

    user: str
    assistant: str


@dataclass(frozen=True)
class Request:
    """What a destination is told about one utterance."""

    satellite_id: str
    satellite_name: str
    wake_word: str
    text: str
    audio_seconds: float
    # BCP 47. `language` is what was spoken (detected from the transcript, or
    # the wake word's hint); `reply_language` is what the answer has to be in,
    # because the voice that reads it speaks only that one. They differ for a
    # language the recogniser understands and Kokoro cannot speak (language.py).
    language: str | None = None
    reply_language: str | None = None
    mode: str = "command"
    history: tuple[Turn, ...] = ()
    # The destination's own memory for this conversation (Home Assistant's
    # conversation_id). Mutable on purpose, and never compared.
    state: dict = field(default_factory=dict, compare=False)


def _check_url(url: str) -> str:
    authority = url.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority:
        raise ValueError("credentials in a URL would be stored in wake_words.json; "
                         "use the destination's *_env field instead")
    return url.rstrip("/")


Url = Annotated[str, Field(pattern=HTTP_URL, max_length=500), AfterValidator(_check_url)]


def _secret(name: str | None) -> str | None:
    """The value of a named variable, or None when unset or empty. An empty
    value is treated as unset because `FOO=` in a compose file is how people
    blank a variable, and "Bearer " with nothing after it is never right."""
    return (os.environ.get(name) or None) if name else None


def _json(r: httpx.Response, who: str) -> dict:
    if r.status_code >= 400:
        # The body, clipped: an upstream's own error sentence is usually the
        # most useful thing to show, and it cannot contain our token because
        # the token only ever travels in a request header.
        raise DestinationError(f"{who} answered {r.status_code}: {r.text[:200].strip()}")
    try:
        body = r.json()
    except ValueError:
        raise DestinationError(f"{who} answered {r.status_code} with a body that is not JSON") from None
    if not isinstance(body, dict):
        raise DestinationError(f"{who} answered JSON that is not an object")
    return body


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def env_vars(self) -> list[str]:
        """Names of the environment variables this destination reads."""
        return []

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        raise NotImplementedError

    async def answer(self, client: httpx.AsyncClient, req: Request) -> AsyncIterator[str]:
        """The answer as it arrives, in pieces. One piece, unless the
        destination can stream."""
        text = await self.call(client, req)
        if text:
            yield text


class HaConversation(_Base):
    """Home Assistant's POST /api/conversation/process. The reply is whatever
    HA's own voice pipeline would have spoken, including its "Sorry, I
    couldn't understand that": that sentence is the answer to the question,
    not a failure of the hub, so it is spoken rather than turned into an
    error -- unless the wake word names a `fallback`, which then gets the
    transcript instead (NotUnderstood).
    """

    type: Literal["ha_conversation"]
    url: Url
    token_env: str = Field(default="SATELLITES_HA_TOKEN", pattern=ENV_NAME)
    # Which conversation agent to use, e.g. "conversation.openai". Unset, HA
    # uses its default (Assist), which is what HA's own satellites do.
    agent_id: str | None = Field(default=None, max_length=120)
    timeout: float = Field(default=15.0, gt=0, le=120)

    def env_vars(self) -> list[str]:
        return [self.token_env]

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        token = _secret(self.token_env)
        if token is None:
            # Checked before the request: HA would only answer 401, and "401"
            # does not tell the operator which variable to set.
            raise DestinationError(f"{self.token_env} is not set, so there is no token for Home Assistant")
        body: dict = {"text": req.text}
        if req.language:
            body["language"] = req.language
        if self.agent_id:
            body["agent_id"] = self.agent_id
        # HA keeps its own context per conversation_id (the last device named,
        # "and the other one"); within one of our conversations it is sent back.
        if req.state.get("ha_conversation_id"):
            body["conversation_id"] = req.state["ha_conversation_id"]
        r = await client.post(f"{self.url}/api/conversation/process", json=body,
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=self.timeout)
        if r.status_code == 401:
            raise DestinationError(f"Home Assistant refused the token in {self.token_env} (401)")
        data = _json(r, "Home Assistant")
        cid = data.get("conversation_id")
        if isinstance(cid, str) and cid:
            req.state["ha_conversation_id"] = cid
        response = data.get("response") if isinstance(data.get("response"), dict) else {}
        try:
            speech = response["speech"]["plain"]["speech"]
        except (KeyError, TypeError):
            # A command HA carried out silently can come back with no speech;
            # that is success with nothing to say, not an error.
            speech = None
        speech = speech if isinstance(speech, str) and speech.strip() else None
        if response.get("response_type") == "error":
            code = (response.get("data") or {}).get("code") if isinstance(response.get("data"), dict) else None
            raise NotUnderstood(f"Home Assistant did not understand ({code or 'error'})", speech)
        return speech


def answer_instruction(language: str | None, reply_language: str | None) -> str | None:
    """The line that tells a model which language to answer in. The voice
    that reads the answer speaks one language (language.voice_for), so an
    answer in any other would be read with the wrong phonemes."""
    if not reply_language:
        return None
    reply = lang.name(reply_language)
    if language and lang.primary(language) != lang.primary(reply_language):
        return (f"Answer in {reply}: the user spoke {lang.name(language)}, but the voice that "
                "reads your answer aloud cannot speak it.")
    return f"Answer in {reply}, the language the user is speaking."


class ThinkFilter:
    """Drops <think>...</think> from text that arrives in pieces, including a
    tag split across two pieces. Reasoning models (DeepSeek-R1, Qwen3) put
    their working there; spoken aloud it is minutes of muttering."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False

    @staticmethod
    def _partial(text: str, tag: str) -> int:
        """How many characters at the end of `text` could start `tag`."""
        for k in range(min(len(tag) - 1, len(text)), 0, -1):
            if text.endswith(tag[:k]):
                return k
        return 0

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = []
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[len(self.buf) - self._partial(self.buf, self.CLOSE):]
                    break
                self.buf = self.buf[i + len(self.CLOSE):]
                self.inside = False
            else:
                i = self.buf.find(self.OPEN)
                if i < 0:
                    keep = self._partial(self.buf, self.OPEN)
                    out.append(self.buf[:len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True
        return "".join(out)

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


class Llm(_Base):
    """An OpenAI-compatible chat model: OpenAI itself, llama.cpp, Ollama, vLLM.

    Streamed by default ("stream": true, server-sent events), so the first
    sentence can be synthesised while the model is still writing the rest. A
    server that ignores the flag and answers with one JSON body is read as
    that; `stream: false` asks for one body in the first place."""

    type: Literal["llm"]
    base_url: Url  # e.g. https://api.openai.com/v1
    model: str = Field(min_length=1, max_length=120)
    system: str | None = Field(default=None, max_length=8000)
    # A local server usually needs no key, so an unset variable means "send no
    # Authorization" rather than an error. A server that does need one answers
    # 401, and GET /satellites/wake-words shows the variable as unset.
    api_key_env: str | None = Field(default="SATELLITES_LLM_API_KEY", pattern=ENV_NAME)
    max_tokens: int = Field(default=400, ge=1, le=8192)
    timeout: float = Field(default=30.0, gt=0, le=120)
    stream: bool = True

    def env_vars(self) -> list[str]:
        return [self.api_key_env] if self.api_key_env else []

    def messages(self, req: Request) -> list[dict]:
        """The system prompt (with the language to answer in), the
        conversation so far, and the new turn."""
        system = "\n\n".join(p for p in (self.system,
                                          answer_instruction(req.language, req.reply_language)) if p)
        messages = [{"role": "system", "content": system}] if system else []
        for turn in req.history:
            messages.append({"role": "user", "content": turn.user})
            if turn.assistant:
                messages.append({"role": "assistant", "content": turn.assistant})
        messages.append({"role": "user", "content": req.text})
        return messages

    def _headers(self) -> dict:
        key = _secret(self.api_key_env)
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _body(self, req: Request) -> dict:
        return {"model": self.model, "messages": self.messages(req), "max_tokens": self.max_tokens}

    @staticmethod
    def _content(data: dict) -> str | None:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise DestinationError("the LLM answered without choices[0].message.content") from None
        if not isinstance(content, str):
            return None
        content = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S).strip()
        return content or None

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        r = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                              timeout=self.timeout, json=self._body(req))
        return self._content(_json(r, "the LLM"))

    async def answer(self, client: httpx.AsyncClient, req: Request) -> AsyncIterator[str]:
        if not self.stream:
            text = await self.call(client, req)
            if text:
                yield text
            return
        async with client.stream("POST", f"{self.base_url}/chat/completions",
                                 headers=self._headers(), timeout=self.timeout,
                                 json=self._body(req) | {"stream": True}) as r:
            if r.status_code >= 400:
                await r.aread()
                raise DestinationError(f"the LLM answered {r.status_code}: {r.text[:200].strip()}")
            if "text/event-stream" not in r.headers.get("content-type", ""):
                # A server that does not stream (or ignores the flag) answers
                # with the whole body: the fallback, read as the buffered call.
                await r.aread()
                text = self._content(_json(r, "the LLM"))
                if text:
                    yield text
                return
            think = ThinkFilter()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue  # comments, event names, blank separators
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    raise DestinationError("the LLM streamed a chunk that is not JSON") from None
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise DestinationError(f"the LLM streamed an error: {str(chunk['error'])[:200]}")
                try:
                    piece = chunk["choices"][0]["delta"].get("content")
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue  # a role-only or usage chunk
                if isinstance(piece, str) and piece:
                    out = think.feed(piece)
                    if out:
                        yield out
            rest = think.flush()
            if rest:
                yield rest


class Webhook(_Base):
    """POST {satellite, satellite_id, wake_word, mode, text, language,
    audio_seconds, history} as JSON. A JSON answer with a "reply" string is
    spoken; any other 2xx is success with nothing to say, so a fire-and-forget
    automation needs no special reply."""

    type: Literal["webhook"]
    url: Url
    # Optional bearer for receivers that check one. Note that a URL which is
    # itself the secret (HA's /api/webhook/<id>) is shown by GET
    # /satellites/wake-words like any other URL; prefer a token here when the
    # receiver allows it.
    token_env: str | None = Field(default=None, pattern=ENV_NAME)
    timeout: float = Field(default=15.0, gt=0, le=120)

    def env_vars(self) -> list[str]:
        return [self.token_env] if self.token_env else []

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        headers = {}
        if self.token_env:
            token = _secret(self.token_env)
            if token is None:
                raise DestinationError(f"{self.token_env} is not set, so there is no token for the webhook")
            headers["Authorization"] = f"Bearer {token}"
        r = await client.post(self.url, headers=headers, timeout=self.timeout, json={
            "satellite": req.satellite_name or req.satellite_id,
            "satellite_id": req.satellite_id,
            "wake_word": req.wake_word, "mode": req.mode, "text": req.text,
            "language": req.language, "audio_seconds": round(req.audio_seconds, 3),
            "history": [{"user": t.user, "assistant": t.assistant} for t in req.history]})
        if r.status_code >= 400:
            raise DestinationError(f"the webhook answered {r.status_code}: {r.text[:200].strip()}")
        try:
            body = r.json()
        except ValueError:
            return None
        reply = body.get("reply") if isinstance(body, dict) else None
        return reply if isinstance(reply, str) and reply.strip() else None


class Echo(_Base):
    """Says back what it heard. Exercises microphone, STT, TTS and speaker
    with no assistant in the loop, which is what a freshly deployed hub needs
    to prove before anyone points it at Home Assistant."""

    type: Literal["echo"]

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        return req.text or None


def _ha_websocket_url(url: str) -> str:
    return re.sub(r"^http", "ws", url, count=1) + "/api/websocket"


async def ha_websocket(url: str, timeout: float):
    """A connection to Home Assistant's websocket API that never follows a
    redirect. The token is sent in the first message after the handshake, so
    a followed redirect would hand it to whichever host the redirect named;
    httpx is told the same (follow_redirects=False) for the REST calls.
    Tests stand a fake in for this function."""
    from websockets.asyncio.client import connect

    class _Direct(connect):
        def process_redirect(self, exc: Exception) -> Exception:
            return exc

    return _Direct(_ha_websocket_url(url), open_timeout=timeout, max_size=1 << 20)


class HaAssist(_Base):
    """A Home Assistant Assist pipeline, over HA's websocket API: the
    transcript goes in at the intent stage and the pipeline's spoken
    response comes back, to be read by Calliope's own voice in the language
    that was spoken. `pipeline` names one of HA's pipelines by id; unset,
    HA's preferred one. The conversation_id HA answers with is sent back on
    every later turn of the same Calliope conversation, so HA keeps its own
    context ("and the other one").

        auth_required -> {"type": "auth", "access_token": ...} -> auth_ok
        {"id": 1, "type": "config/device_registry/list"} -> result (cached)
        {"id": 2, "type": "assist_pipeline/run", "start_stage": "intent",
         "end_stage": "intent", "input": {"text": ...}, "pipeline"?,
         "conversation_id"?, "device_id"?}
        -> result, then events run-start, intent-start, intent-end, run-end

    device_id is the satellite's own device in Home Assistant, which the
    Calliope integration registers: with it, Assist knows the satellite's
    area, and "turn on the lights" means that room's.

    An intent that HA did not understand (response_type "error") is
    NotUnderstood, as for ha_conversation, so a `fallback` can take over."""

    type: Literal["ha_assist"]
    url: Url
    token_env: str = Field(default="SATELLITES_HA_TOKEN", pattern=ENV_NAME)
    pipeline: str | None = Field(default=None, max_length=120)
    timeout: float = Field(default=15.0, gt=0, le=120)

    connect: ClassVar = staticmethod(ha_websocket)
    # (HA's URL, satellite id) -> (HA device id or None, time.monotonic() it
    # is good until). The Calliope integration registers each satellite as a
    # device, identified ["calliope", <satellite id>]; passed as device_id,
    # it tells Assist which area "the lights" are in. Looked up again after
    # DEVICE_TTL_S, so an integration installed later is found without a
    # restart, at one extra message every ten minutes.
    devices: ClassVar[dict] = {}
    DEVICE_TTL_S: ClassVar[float] = 600.0

    def env_vars(self) -> list[str]:
        return [self.token_env]

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        token = _secret(self.token_env)
        if token is None:
            raise DestinationError(f"{self.token_env} is not set, so there is no token for Home Assistant")
        run: dict = {"id": 2, "type": "assist_pipeline/run", "start_stage": "intent",
                     "end_stage": "intent", "input": {"text": req.text}}
        if self.pipeline:
            run["pipeline"] = self.pipeline
        if req.state.get("ha_assist_conversation_id"):
            run["conversation_id"] = req.state["ha_assist_conversation_id"]
        from websockets.exceptions import WebSocketException

        try:
            async with await type(self).connect(self.url, self.timeout) as ws:
                hello = await self._receive(ws)
                if hello.get("type") != "auth_required":
                    raise DestinationError("Home Assistant's websocket did not ask for authentication")
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                auth = await self._receive(ws)
                if auth.get("type") != "auth_ok":
                    # HA's own message, which names no token.
                    raise DestinationError(f"Home Assistant refused the token in {self.token_env} "
                                           f"({str(auth.get('message') or auth.get('type'))[:100]})")
                device = await self._device(ws, req.satellite_id)
                if device:
                    run["device_id"] = device
                await ws.send(json.dumps(run))
                return await self._run(ws, req)
        except DestinationError:
            raise
        # Refused, reset, a failed handshake (a redirect included), or a frame
        # that is not JSON. The message names the host at most, never the token.
        except (OSError, ValueError, WebSocketException) as e:
            raise DestinationError(f"Home Assistant's websocket failed: {type(e).__name__}: "
                                   f"{str(e)[:150]}") from None

    @staticmethod
    async def _receive(ws) -> dict:
        msg = json.loads(await ws.recv())
        if not isinstance(msg, dict):
            raise DestinationError("Home Assistant's websocket sent something that is not an object")
        return msg

    async def _device(self, ws, satellite_id: str) -> str | None:
        """The satellite's device id in Home Assistant, from its device
        registry (message 1), or None when no device is the satellite's."""
        key = (self.url, satellite_id)
        cached = self.devices.get(key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
        while True:
            msg = await self._receive(ws)
            if msg.get("id") == 1 and msg.get("type") == "result":
                break
        found = None
        for device in (msg.get("result") or []) if msg.get("success") else []:
            if not isinstance(device, dict):
                continue
            ids = device.get("identifiers") or []
            if any(isinstance(i, list) and i == ["calliope", satellite_id] for i in ids):
                found = device.get("id") if isinstance(device.get("id"), str) else None
                break
        self.devices[key] = (found, time.monotonic() + self.DEVICE_TTL_S)
        return found

    async def _run(self, ws, req: Request) -> str | None:
        while True:
            msg = await self._receive(ws)
            if msg.get("id") != 2:
                continue
            if msg.get("type") == "result":
                if not msg.get("success", False):
                    err = msg.get("error") or {}
                    raise DestinationError(f"Home Assistant refused the pipeline run: "
                                           f"{str(err.get('message') or err)[:200]}")
                continue
            event = msg.get("event") or {}
            kind, data = event.get("type"), event.get("data") or {}
            if kind == "error":
                raise DestinationError(f"the Assist pipeline failed: "
                                       f"{str(data.get('message') or data.get('code'))[:200]}")
            if kind == "intent-end":
                out = data.get("intent_output") or {}
                cid = out.get("conversation_id")
                if isinstance(cid, str) and cid:
                    req.state["ha_assist_conversation_id"] = cid
                response = out.get("response") or {}
                try:
                    speech = response["speech"]["plain"]["speech"]
                except (KeyError, TypeError):
                    speech = None
                speech = speech if isinstance(speech, str) and speech.strip() else None
                if response.get("response_type") == "error":
                    code = (response.get("data") or {}).get("code")
                    raise NotUnderstood(f"Home Assistant did not understand ({code or 'error'})",
                                        speech)
                return speech
            if kind == "run-end":
                return None  # a run with no intent stage output: nothing to say


Destination = Annotated[HaConversation | HaAssist | Llm | Webhook | Echo,
                        Field(discriminator="type")]
