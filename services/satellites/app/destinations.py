"""Where a transcript goes after a wake word, and what comes back to be spoken.

Five kinds, told apart by "type" in a wake word's action:

    ha_conversation  Home Assistant's REST conversation API
    ha_assist        a Home Assistant Assist pipeline, over HA's websocket API,
                     which also hears the command and speaks the reply
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

SECRETS ARE NAMED IN AN ACTION, AND NEVER HELD IN ONE. A destination that
needs a credential carries the NAME of a secret (token_env, api_key_env) and
reads the value at call time (_secret): from the process environment, or else
from the keys the hub holds in secrets.json (secret_store.py), which the page
can store a key into and never read one back from. Never from wake_words.json:
it is written by an API that answers GET with every action, so a token stored
in it would be one GET away from anyone with an API key. No route answers a
value. Two things keep a value out of an action rather than hope for it:

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

import contextlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Annotated, AsyncIterator, ClassVar, Literal

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from . import audio
from . import language as lang
from . import secret_store

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


def _held(name: str | None) -> str | None:
    """The value a named secret has, as it was found, or None: the process
    environment first, then a key the hub holds (secret_store.py). Read on
    every request, so a key stored or cleared from the page applies to the
    next one. An empty variable is treated as unset because `FOO=` in a
    compose file is how people blank a variable, and "Bearer " with nothing
    after it is never right. This is the one place a name becomes a value:
    router.env_status reads it too, so "set" means "the action will find a
    value". What is sent is _secret's, which checks it first."""
    if not name:
        return None
    return os.environ.get(name) or secret_store.current().get(name) or None


def _secret(name: str | None) -> str | None:
    """The value to send for a named secret, or None when it has none.

    A VALUE NO HEADER CAN CARRY IS REFUSED, NOT SENT. A key the hub holds was
    checked against SECRET_VALUE when it was stored and again when the file
    loaded; one in the environment was checked by nobody. A .env saved with
    Windows line endings leaves a CR on the end, and a Kubernetes secret made
    from a file keeps the file's final newline. httpx then refuses the
    Authorization header with h11's "Illegal header value b'Bearer sk-...\\r'",
    which quotes the key whole and writes the CR as an escape, so _scrub's
    exact match never finds it, and the sentence reached Outcome.error, the
    INFO log, the event stream and the page. So such a value is a
    DestinationError that names the variable and says nothing of the value,
    not its length, not where the bad character sits. Refused rather than
    trimmed, as a pasted key is (secret_store.py): the hub never guesses at a
    secret, and the fix is one line where the variable is set."""
    value = _held(name)
    if value is not None and not re.fullmatch(secret_store.SECRET_VALUE, value):
        raise DestinationError(
            f"{name} is set in the hub's environment with a line break, a space or a character "
            "outside printable ASCII in it, which no request header can carry, so it is not "
            "sent; set it again without one (a .env saved with Windows line endings, or a "
            "secret made from a file that ends in a newline, does this)")
    return value


def transport_error(e: httpx.HTTPError, *keys: str | None) -> str:
    """An httpx failure in one line that holds no key. Most of them name a
    host or a socket ("ConnectError: [Errno 111] Connection refused") and are
    worth showing. A LocalProtocolError is h11 refusing what the hub was about
    to send, and it quotes the refused line, headers included: its words are
    never shown, only its type and what it means. Each of `keys` is scrubbed
    from the rest as well."""
    if isinstance(e, httpx.LocalProtocolError):
        return (f"{type(e).__name__}: the hub could not write the request, and h11's reason is "
                "not shown because it can quote a header that holds a key")
    text = f"{type(e).__name__}: {e}"
    for key in keys:
        text = _scrub(text, key)
    return text


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


# ---- the language model: any OpenAI-compatible /chat/completions -----------------

# GET {base_url}/models, for the page's picker: a list is small, but a server
# that streams one without end must not hold the hub's memory or a request.
MODELS_TIMEOUT_S = 10.0
MODELS_MAX_BYTES = 8 * 1024 * 1024
MODELS_MAX = 2000
MODEL_ID_MAX = 120          # Llm.model's own max_length
# Of a refusal's body: its first sentence is what is shown.
ERROR_READ_MAX = 64 * 1024


def _trim_endpoint(url: str) -> str:
    """A base URL pasted as the whole endpoint, as a provider's curl example
    writes it, cut back to the base: ".../v1/chat/completions" would be asked
    for ".../v1/chat/completions/chat/completions" and answer 404 on every
    turn. Trimmed rather than refused, because a wake_words.json that holds
    one such URL would stop loading, and a file that does not load switches
    every wake word off (wakewords_config.Assignment.open)."""
    trimmed = re.sub(r"/+chat/completions$", "", url)
    return trimmed if re.match(HTTP_URL, trimmed) else url


LlmUrl = Annotated[Url, AfterValidator(_trim_endpoint)]


def _text(content: object) -> str:
    """The words in a message's `content`: a string, or the "text" parts of a
    list of parts. Mistral's reasoning models answer with a "thinking" part
    and a "text" part; the thinking is not the answer. Anything else holds no
    words to speak."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p["text"] for p in content if isinstance(p, dict)
                       and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def _scrub(text: str, key: str | None) -> str:
    """`text` with the key taken out. OpenAI's 401 says "Incorrect API key
    provided: sk-proj-****...WXYZ" and DeepSeek's "Your api key: ****WXYZ is
    invalid": masked, but ending in the key's real last characters, and
    env_status rules out even a prefix. So the exact value goes, and so does
    any word with three or more asterisks in it, which is how every provider
    seen so far writes a masked key."""
    if key:
        text = text.replace(key, "[key hidden]")
    return re.sub(r"\S+", lambda m: "[key hidden]" if m.group().count("*") >= 3 else m.group(),
                  text)


def _parsed(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _said(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value.strip() else None


def _provider_message(raw: str | dict, key: str | None) -> str:
    """The provider's own sentence out of an error body, or out of an error
    chunk of a stream: error.message, else error.code or error.type, else
    error as a string, else message, else detail as a string, else the body
    as it came. One line, at most 300 characters, and scrubbed of the key."""
    body = raw if isinstance(raw, dict) else _parsed(raw)
    text = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            text = _said(err.get("message")) or _said(err.get("code")) or _said(err.get("type"))
        else:
            text = _said(err)
        text = text or _said(body.get("message"))
        if text is None and isinstance(body.get("detail"), str):
            text = _said(body["detail"])
    if text is None:
        text = raw if isinstance(raw, str) else json.dumps(raw)
    return " ".join(_scrub(text, key).split())[:300] or "no reason given"


def _refusal(status: int, raw: str, key: str | None, key_env: str | None,
             who: str = "the LLM") -> str:
    """A refusal in the provider's words. A 401 or 403 also names the
    variable to fix, as Home Assistant's does, because "401" alone does not
    say which of several keys it was."""
    said = _provider_message(raw, key)
    if status in (401, 403):
        if key:
            return f"{who} refused the key in {key_env} ({status}): {said}"
        if key_env:
            return f"{who} wants a key ({status}) and {key_env} has none: {said}"
        return f"{who} wants a key ({status}) and the action names none to send: {said}"
    return f"{who} answered {status}: {said}"


def _wants_completion_tokens(status: int, raw: str) -> bool:
    """A 400 about max_tokens that asks for max_completion_tokens instead.
    OpenAI's, verbatim: {"error": {"message": "Unsupported parameter:
    'max_tokens' is not supported with this model. Use
    'max_completion_tokens' instead.", "type": "invalid_request_error",
    "param": "max_tokens", "code": "unsupported_parameter"}}."""
    if status != 400:
        return False
    body = _parsed(raw)
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message, param, code = (str(err.get(k) or "") for k in ("message", "param", "code"))
    else:
        message, param, code = str(err or raw), "", ""
    return ("max_tokens" in f"{message} {param} {code}"
            and ("max_completion_tokens" in message or code == "unsupported_parameter"))


async def _read_capped(r: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """At most `cap` bytes of a streamed body, and whether there was more."""
    got = bytearray()
    async for chunk in r.aiter_bytes():
        got += chunk
        if len(got) > cap:
            return bytes(got[:cap]), True
    return bytes(got), False


async def _read_error(r: httpx.Response) -> str:
    data, _ = await _read_capped(r, ERROR_READ_MAX)
    return data.decode("utf-8", "replace")


class Llm(_Base):
    """Any OpenAI-compatible chat model, hosted or your own: OpenAI,
    Anthropic's compatibility endpoint, OpenRouter, Groq, Mistral, DeepSeek,
    or llama.cpp, vLLM or Ollama on a machine of yours. What is sent is the
    part of POST /chat/completions every one of them takes: `model`,
    `messages`, one token limit and `stream`, and nothing more.

    Streamed by default ("stream": true, server-sent events), so the first
    sentence can be synthesised while the model is still writing the rest. A
    server that ignores the flag and answers with one JSON body is read as
    that; `stream: false` asks for one body in the first place.

    THE TOKEN LIMIT HAS TWO NAMES. OpenAI's reasoning and GPT-5-class models
    refuse `max_tokens` with a 400 and take `max_completion_tokens`, and
    llama.cpp, Ollama, DeepSeek and Mistral know only `max_tokens`; some
    strict servers refuse a field they do not know. So max_tokens goes first,
    and a 400 that names it and asks for the other (_wants_completion_tokens)
    is asked again, once, with max_completion_tokens at the same number. The
    answer is kept for the process in limit_names, so later turns go straight
    to the right name: one refused request per model per start, and no host
    sniffing that would miss Azure or a proxy in front of OpenAI.

    NO TEMPERATURE, AND NO stream_options. A reasoning model refuses any
    temperature but its default, every provider's default is sensible, and
    some servers refuse stream_options outright.

    WHAT IS SPOKEN is the answer's text: `content` as a string, or the "text"
    parts of a list of parts (_text). `reasoning_content` and `reasoning`
    (DeepSeek, vLLM, OpenRouter) are never read, and an inline <think> block
    is dropped (ThinkFilter). A model that spent its whole limit thinking and
    said nothing (finish_reason "length") is an error that names the fix,
    rather than a reply spoken as silence.

    A REFUSAL IS SHOWN IN THE PROVIDER'S OWN WORDS (_provider_message), with
    any key in it hidden (_scrub): it reaches Outcome.error, the event stream,
    the INFO log and the page."""

    type: Literal["llm"]
    base_url: LlmUrl  # e.g. https://api.openai.com/v1
    model: str = Field(min_length=1, max_length=MODEL_ID_MAX)
    system: str | None = Field(default=None, max_length=8000)
    # A local server usually needs no key, so an unset variable means "send no
    # Authorization" rather than an error. A server that does need one answers
    # 401, and GET /satellites/wake-words shows the variable as unset.
    api_key_env: str | None = Field(default="SATELLITES_LLM_API_KEY", pattern=ENV_NAME)
    max_tokens: int = Field(default=400, ge=1, le=8192)
    timeout: float = Field(default=30.0, gt=0, le=120)
    stream: bool = True

    # (base_url, model) -> "max_completion_tokens", once that server refused
    # max_tokens for that model. Per process and learned, like HaAssist.devices.
    limit_names: ClassVar[dict] = {}

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

    def limit(self) -> str:
        """The name this server takes the token limit by, as far as is known."""
        return self.limit_names.get((self.base_url, self.model), "max_tokens")

    def _body(self, req: Request, limit: str, stream: bool) -> dict:
        body = {"model": self.model, "messages": self.messages(req), limit: self.max_tokens}
        if stream:
            body["stream"] = True
        return body

    def _spent(self) -> str:
        return (f"the model spent its whole reply limit ({self.max_tokens} tokens) before "
                "answering; raise it under More, or choose a model that does not reason first")

    @contextlib.asynccontextmanager
    async def _open(self, client: httpx.AsyncClient, req: Request, stream: bool):
        """POST /chat/completions, as a response under 400. The one retry with
        max_completion_tokens happens here, before the caller reads anything;
        any other refusal is raised in the provider's words."""
        key = _secret(self.api_key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        limit = self.limit()
        while True:
            async with client.stream("POST", f"{self.base_url}/chat/completions", headers=headers,
                                     timeout=self.timeout,
                                     json=self._body(req, limit, stream)) as r:
                if r.status_code >= 400:
                    raw = await _read_error(r)
                    if limit == "max_tokens" and _wants_completion_tokens(r.status_code, raw):
                        limit = "max_completion_tokens"
                        self.limit_names[(self.base_url, self.model)] = limit
                        continue
                    raise DestinationError(_refusal(r.status_code, raw, key, self.api_key_env))
                yield r
                return

    def _content(self, data: dict) -> str | None:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            # A model stopped by its limit may send no content at all.
            content = (message.get("content") if choice.get("finish_reason") == "length"
                       else message["content"])
        except (KeyError, IndexError, TypeError, AttributeError):
            raise DestinationError("the LLM answered without choices[0].message.content") from None
        text = re.sub(r"<think>.*?(</think>|$)", "", _text(content), flags=re.S).strip()
        if not text and choice.get("finish_reason") == "length":
            raise DestinationError(self._spent())
        return text or None

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        async with self._open(client, req, stream=False) as r:
            await r.aread()
        return self._content(_json(r, "the LLM"))

    async def answer(self, client: httpx.AsyncClient, req: Request) -> AsyncIterator[str]:
        if not self.stream:
            text = await self.call(client, req)
            if text:
                yield text
            return
        async with self._open(client, req, stream=True) as r:
            if "text/event-stream" not in r.headers.get("content-type", ""):
                # A server that does not stream (or ignores the flag) answers
                # with the whole body: the fallback, read as the buffered call.
                await r.aread()
                text = self._content(_json(r, "the LLM"))
                if text:
                    yield text
                return
            think = ThinkFilter()
            spoke, finish = False, None
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
                    raise DestinationError("the LLM streamed an error: "
                                           + _provider_message(chunk, _held(self.api_key_env)))
                try:
                    choice = chunk["choices"][0]
                    delta = choice.get("delta") or {}
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue  # a usage chunk
                finish = choice.get("finish_reason") or finish
                piece = _text(delta.get("content")) if isinstance(delta, dict) else ""
                if piece:
                    out = think.feed(piece)
                    if out:
                        spoke = spoke or bool(out.strip())
                        yield out
            rest = think.flush()
            if rest:
                spoke = spoke or bool(rest.strip())
                yield rest
            if not spoke and finish == "length":
                raise DestinationError(self._spent())

    @staticmethod
    async def list_models(client: httpx.AsyncClient, base_url: str,
                          api_key_env: str | None) -> list[str]:
        """The model ids a server lists at GET {base_url}/models, for the
        page's picker, asked with the key the action would send. OpenAI's
        shape is {"data": [{"id": ...}]}; a bare list of those, or of ids, is
        taken too. Ids over MODEL_ID_MAX characters could not be saved and are
        left out; the rest come back unique, sorted without regard to case,
        and at most MODELS_MAX of them (OpenRouter lists several hundred).
        Nothing is filtered by what it looks like: a name-based rule for
        embedding or speech models would rot, and typing narrows the list."""
        key = _secret(api_key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with client.stream("GET", f"{base_url}/models", headers=headers,
                                 timeout=MODELS_TIMEOUT_S) as r:
            if r.status_code >= 300:
                raise DestinationError(_refusal(r.status_code, await _read_error(r), key,
                                                api_key_env, who="the server"))
            data, over = await _read_capped(r, MODELS_MAX_BYTES)
        if over:
            raise DestinationError(f"the server's model list is over {MODELS_MAX_BYTES >> 20} MiB")
        body = _parsed(data.decode("utf-8", "replace"))
        items = body.get("data") if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise DestinationError("the server answered /models without a list of models")
        ids = {i for i in ((x.get("id") if isinstance(x, dict) else x) for x in items)
               if isinstance(i, str) and i.strip() and len(i) <= MODEL_ID_MAX}
        return sorted(ids, key=lambda i: (i.casefold(), i))[:MODELS_MAX]


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


# The command audio goes to Home Assistant in pieces of this many bytes: well
# under any websocket frame limit, and few enough frames for a long command.
STT_CHUNK = 8192


@dataclass(frozen=True)
class Pipeline:
    """What HaAssist takes from one of Home Assistant's Assist pipelines: the
    language it understands, and the engines and voice it hears and speaks
    with. Read from HA on every command, so a pipeline is set up in Home
    Assistant and nowhere else, and a change there is heard at the next one."""

    id: str
    name: str
    language: str | None = None      # BCP 47, what its conversation agent takes
    stt_engine: str | None = None
    stt_language: str | None = None
    tts_engine: str | None = None
    tts_language: str | None = None
    tts_voice: str | None = None

    @classmethod
    def of(cls, raw: object) -> Pipeline:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise DestinationError("Home Assistant described a pipeline without an id")

        def text(key: str) -> str | None:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None
        # "*" is an agent that takes every language (an LLM's): the
        # pipeline's own language is the one it is spoken to in.
        spoken = text("conversation_language")
        return cls(id=raw["id"], name=text("name") or raw["id"],
                   language=spoken if spoken and spoken != "*" else text("language"),
                   stt_engine=text("stt_engine"), stt_language=text("stt_language"),
                   tts_engine=text("tts_engine"), tts_language=text("tts_language"),
                   tts_voice=text("tts_voice"))

    @property
    def voice(self) -> str | None:
        """The reply's voice, as an Outcome names it."""
        return self.tts_voice or self.tts_engine

    def summary(self) -> dict:
        return asdict(self)


class HaAssist(_Base):
    """A Home Assistant Assist pipeline, over HA's websocket API. THE
    PIPELINE IS SET UP IN HOME ASSISTANT: it hears the command with its own
    speech-to-text, understands it, and speaks the reply with its own
    text-to-speech and voice, in its own language. Calliope's language and
    voice settings are not read for this destination. `pipeline` names one of
    HA's pipelines by id; unset, HA's preferred one.

    A command is three calls (dialogue.run_turn):

    1. transcribe(): the pipeline's settings, then its stt stage alone, fed
       the command audio the hub has already cut (no_vad: HA's own voice
       detection would cut it again)
           {"id": 1, "type": "assist_pipeline/pipeline/get", "pipeline_id"?}
           {"id": 2, "type": "assist_pipeline/run", "start_stage": "stt",
            "end_stage": "stt", "pipeline", "input": {"sample_rate", "no_vad"}}
           -> run-start names a binary handler; each audio frame is that
              handler's byte and 16-bit mono PCM, and the handler's byte on
              its own ends it; -> stt-end
    2. call(): the transcript at the intent stage
           {"id": 1, "type": "config/device_registry/list"} (cached)
           {"id": 2, "type": "assist_pipeline/run", "start_stage": "intent",
            "end_stage": "intent", "input": {"text": ...}, "pipeline"?,
            "conversation_id"?, "device_id"?} -> intent-end
    3. synthesise(): each sentence of the reply, over REST
           POST /api/tts_get_url with the pipeline's engine, language and voice,
           and a preferred format of WAV at the speaker's rate, which HA
           converts to (with its ffmpeg) whatever the engine makes; then GET
           the /api/tts_proxy/ path it answers, which needs no token.

    Each call opens its own connection and authenticates: auth_required ->
    {"type": "auth", "access_token": ...} -> auth_ok. A typed sentence
    (POST /satellites/routing/test) has no audio, and asks speech() for the
    settings instead of transcribe(). A pipeline with no speech-to-text or no
    text-to-speech leaves that part to Calliope's own.

    The conversation_id HA answers with is sent back on every later turn of
    the same Calliope conversation, so HA keeps its own context ("and the
    other one"). device_id is the satellite's own device in Home Assistant,
    which the Calliope integration registers: with it, Assist knows the
    satellite's area, and "turn on the lights" means that room's.

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

    def _token(self) -> str:
        token = _secret(self.token_env)
        if token is None:
            raise DestinationError(f"{self.token_env} is not set, so there is no token for Home Assistant")
        return token

    @contextlib.asynccontextmanager
    async def _session(self):
        """A connection to HA's websocket API, authenticated. Whatever goes
        wrong on it, here or in the caller's block, comes out as a
        DestinationError that names the host at most, never the token."""
        token = self._token()
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
                yield ws
        except DestinationError:
            raise
        # Refused, reset, a failed handshake (a redirect included), or a frame
        # that is not JSON.
        except (OSError, ValueError, WebSocketException) as e:
            raise DestinationError(f"Home Assistant's websocket failed: {type(e).__name__}: "
                                   f"{str(e)[:150]}") from None

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        run: dict = {"id": 2, "type": "assist_pipeline/run", "start_stage": "intent",
                     "end_stage": "intent", "input": {"text": req.text}}
        if self.pipeline:
            run["pipeline"] = self.pipeline
        if req.state.get("ha_assist_conversation_id"):
            run["conversation_id"] = req.state["ha_assist_conversation_id"]
        async with self._session() as ws:
            device = await self._device(ws, req.satellite_id)
            if device:
                run["device_id"] = device
            await ws.send(json.dumps(run))
            return await self._run(ws, req)

    # -- the pipeline's own speech -----------------------------------------------

    async def speech(self) -> Pipeline:
        """The pipeline's settings, for a turn that has no audio to hear."""
        async with self._session() as ws:
            return await self._pipeline(ws)

    async def transcribe(self, pcm: bytes, rate: int) -> tuple[str | None, Pipeline]:
        """What was said, heard by the pipeline's own speech-to-text, and the
        pipeline. The text is None when the pipeline has no speech-to-text."""
        async with self._session() as ws:
            pipeline = await self._pipeline(ws)
            if pipeline.stt_engine is None:
                return None, pipeline
            await ws.send(json.dumps({"id": 2, "type": "assist_pipeline/run", "start_stage": "stt",
                                      "end_stage": "stt", "pipeline": pipeline.id,
                                      "input": {"sample_rate": rate, "no_vad": True}}))
            return await self._hear(ws, pcm[:len(pcm) & ~1]), pipeline

    async def synthesise(self, client: httpx.AsyncClient, text: str, pipeline: Pipeline,
                         rate: int) -> bytes:
        """`text` in the pipeline's voice, as 16-bit mono PCM at `rate`."""
        options: dict = {"preferred_format": "wav", "preferred_sample_rate": rate,
                         "preferred_sample_channels": 1, "preferred_sample_bytes": 2}
        if pipeline.tts_voice:
            options["voice"] = pipeline.tts_voice
        body: dict = {"engine_id": pipeline.tts_engine, "message": text, "options": options}
        if pipeline.tts_language:
            body["language"] = pipeline.tts_language
        r = await client.post(f"{self.url}/api/tts_get_url", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self._token()}"})
        path = _json(r, "Home Assistant's text-to-speech").get("path")
        # Only ever a path on the same Home Assistant, so the audio is never
        # fetched from a host the action did not name.
        if not isinstance(path, str) or not path.startswith("/api/tts_proxy/"):
            raise DestinationError("Home Assistant's text-to-speech answered without an audio path")
        r = await client.get(f"{self.url}{path}", timeout=self.timeout)
        if r.status_code != 200:
            raise DestinationError(f"Home Assistant's text-to-speech answered {r.status_code} "
                                   f"for the audio")
        try:
            pcm, got = audio.pcm_from_wav(r.content)
        except ValueError as e:
            raise DestinationError(f"Home Assistant's text-to-speech sent audio that is not "
                                   f"16-bit WAV: {e}") from None
        return audio.resample(pcm, got, rate)

    async def pipelines(self) -> dict:
        """HA's pipelines and its preferred one, for the page's picker."""
        async with self._session() as ws:
            await ws.send(json.dumps({"id": 1, "type": "assist_pipeline/pipeline/list"}))
            msg = await self._result(ws, 1)
        if not msg.get("success"):
            raise DestinationError(f"Home Assistant would not list its pipelines: "
                                   f"{_ha_error(msg)}")
        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        found = []
        for raw in result.get("pipelines") or []:
            try:
                found.append(Pipeline.of(raw).summary())
            except DestinationError:
                continue
        preferred = result.get("preferred_pipeline")
        return {"preferred": preferred if isinstance(preferred, str) else None, "pipelines": found}

    # -- the websocket ---------------------------------------------------------------

    @staticmethod
    async def _receive(ws) -> dict:
        msg = json.loads(await ws.recv())
        if not isinstance(msg, dict):
            raise DestinationError("Home Assistant's websocket sent something that is not an object")
        return msg

    async def _result(self, ws, msg_id: int) -> dict:
        while True:
            msg = await self._receive(ws)
            if msg.get("id") == msg_id and msg.get("type") == "result":
                return msg

    async def _pipeline(self, ws) -> Pipeline:
        get: dict = {"id": 1, "type": "assist_pipeline/pipeline/get"}
        if self.pipeline:
            get["pipeline_id"] = self.pipeline
        await ws.send(json.dumps(get))
        msg = await self._result(ws, 1)
        if not msg.get("success"):
            which = f"the pipeline {self.pipeline}" if self.pipeline else "its preferred pipeline"
            raise DestinationError(f"Home Assistant could not find {which}: {_ha_error(msg)}")
        return Pipeline.of(msg.get("result"))

    async def _device(self, ws, satellite_id: str) -> str | None:
        """The satellite's device id in Home Assistant, from its device
        registry (message 1), or None when no device is the satellite's."""
        key = (self.url, satellite_id)
        cached = self.devices.get(key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
        msg = await self._result(ws, 1)
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

    async def _events(self, ws):
        """The events of pipeline run 2, as (type, data), after HA accepted it."""
        while True:
            msg = await self._receive(ws)
            if msg.get("id") != 2:
                continue
            if msg.get("type") == "result":
                if not msg.get("success", False):
                    raise DestinationError(f"Home Assistant refused the pipeline run: "
                                           f"{_ha_error(msg)}")
                continue
            event = msg.get("event") or {}
            yield event.get("type"), event.get("data") or {}

    async def _hear(self, ws, pcm: bytes) -> str:
        async for kind, data in self._events(ws):
            if kind == "run-start":
                handler = (data.get("runner_data") or {}).get("stt_binary_handler_id")
                if not isinstance(handler, int) or not 0 <= handler < 256:
                    raise DestinationError("Home Assistant's pipeline run named no handler for the audio")
                prefix = bytes([handler])
                for i in range(0, len(pcm), STT_CHUNK):
                    await ws.send(prefix + pcm[i:i + STT_CHUNK])
                await ws.send(prefix)  # the end of the audio
            elif kind == "error":
                if data.get("code") == "stt-no-text-recognized":
                    return ""
                raise DestinationError(f"Home Assistant's speech-to-text failed: "
                                       f"{str(data.get('message') or data.get('code'))[:200]}")
            elif kind == "stt-end":
                text = (data.get("stt_output") or {}).get("text")
                return text.strip() if isinstance(text, str) else ""
            elif kind == "run-end":
                return ""
        return ""

    async def _run(self, ws, req: Request) -> str | None:
        async for kind, data in self._events(ws):
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
        return None


def _ha_error(msg: dict) -> str:
    err = msg.get("error") or {}
    return str(err.get("message") or err.get("code") or err)[:200] if isinstance(err, dict) \
        else str(err)[:200]


Destination = Annotated[HaConversation | HaAssist | Llm | Webhook | Echo,
                        Field(discriminator="type")]
