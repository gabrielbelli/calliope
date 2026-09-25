"""Where a transcript goes after a wake word, and what comes back to be spoken.

Four kinds, told apart by "type" in the rules file:

    ha_conversation  Home Assistant's REST conversation API
    llm              any OpenAI-compatible /chat/completions
    webhook          a JSON POST to a URL of the operator's choosing
    echo             the transcript itself, so a fresh hub can be tested end
                     to end with nothing else running

Each one answers with the text to speak, or None for "nothing to say", and
raises DestinationError for anything that went wrong. The router turns both
into an Outcome; nothing here decides where the reply is played.

SECRETS ARE NAMED, NEVER HELD. A destination that needs a credential carries
the NAME of an environment variable (token_env, api_key_env) and reads the
value at call time. rules.json is written by an API that answers GET with the
whole ruleset, so a token stored in it would be one GET away from anyone with
an API key, and in every backup of the data volume. Two things enforce that
rather than hope for it:

  * every model forbids unknown fields, so {"token": "..."} pasted into a rule
    is a validation error rather than a secret quietly saved;
  * an env var name must look like one (upper case, digits, underscore). A
    Home Assistant long-lived token is a JWT and an OpenAI key starts "sk-";
    neither fits, so pasting the value where the name belongs is refused too.

URLs with a user:password part are refused for the same reason.

NO SSRF FILTERING, AND THAT IS DELIBERATE. Every URL here comes from the
operator's own rules, set through PUT /satellites/routing, which sits behind
the same API keys (or the gateway) as adopting satellites and flashing
firmware. The legitimate targets are exactly the addresses an SSRF filter would
block: Home Assistant on the LAN, an LLM on localhost, Node-RED in the next
container. A filter would break the main use and protect nothing, because
whoever can PUT a rule can already reflash every satellite. The trust boundary
is who may write the rules, not what they write. Redirects are not followed, so
a destination cannot bounce a request, and its bearer token, to a host the rule
never named.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# Upper case only, on purpose: see the module docstring. Real env var names
# may be lower case, but refusing that costs nothing here and turns "pasted
# the token into token_env" into a 422 instead of a secret in rules.json.
ENV_NAME = r"^[A-Z][A-Z0-9_]{0,63}$"
HTTP_URL = r"^https?://[^/\s?#]+(/[^\s]*)?$"


class DestinationError(Exception):
    """A destination failed in a way worth one sentence to the operator."""


@dataclass(frozen=True)
class Request:
    """What a destination is told about one utterance."""

    satellite_id: str
    satellite_name: str
    wake_word: str
    text: str
    audio_seconds: float
    language: str | None = None


def _check_url(url: str) -> str:
    authority = url.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority:
        raise ValueError("credentials in a URL would be stored in rules.json; "
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


class HaConversation(_Base):
    """Home Assistant's POST /api/conversation/process. The reply is whatever
    HA's own voice pipeline would have spoken, including its "Sorry, I
    couldn't understand that": that sentence is the answer to the question,
    not a failure of the hub, so it is spoken rather than turned into an error.
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
        r = await client.post(f"{self.url}/api/conversation/process", json=body,
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=self.timeout)
        if r.status_code == 401:
            raise DestinationError(f"Home Assistant refused the token in {self.token_env} (401)")
        data = _json(r, "Home Assistant")
        try:
            speech = data["response"]["speech"]["plain"]["speech"]
        except (KeyError, TypeError):
            # A command HA carried out silently can come back with no speech;
            # that is success with nothing to say, not an error.
            return None
        return speech if isinstance(speech, str) and speech.strip() else None


class Llm(_Base):
    """An OpenAI-compatible chat model: OpenAI itself, llama.cpp, Ollama, vLLM."""

    type: Literal["llm"]
    base_url: Url  # e.g. https://api.openai.com/v1
    model: str = Field(min_length=1, max_length=120)
    system: str | None = Field(default=None, max_length=8000)
    # A local server usually needs no key, so an unset variable means "send no
    # Authorization" rather than an error. A server that does need one answers
    # 401, and GET /satellites/routing shows the variable as unset.
    api_key_env: str | None = Field(default="SATELLITES_LLM_API_KEY", pattern=ENV_NAME)
    max_tokens: int = Field(default=400, ge=1, le=8192)
    timeout: float = Field(default=30.0, gt=0, le=120)


    def env_vars(self) -> list[str]:
        return [self.api_key_env] if self.api_key_env else []

    async def call(self, client: httpx.AsyncClient, req: Request) -> str | None:
        messages = ([{"role": "system", "content": self.system}] if self.system else [])
        messages.append({"role": "user", "content": req.text})
        key = _secret(self.api_key_env)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = await client.post(f"{self.base_url}/chat/completions", headers=headers,
                              timeout=self.timeout,
                              json={"model": self.model, "messages": messages,
                                    "max_tokens": self.max_tokens})
        data = _json(r, "the LLM")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise DestinationError("the LLM answered without choices[0].message.content") from None
        if not isinstance(content, str):
            return None
        # Reasoning models (DeepSeek-R1, Qwen3) put their working in <think>
        # blocks inside content. Spoken aloud it is minutes of muttering before
        # the answer, so it is dropped here rather than left to each prompt.
        content = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S).strip()
        return content or None


class Webhook(_Base):
    """POST {satellite, satellite_id, wake_word, text, audio_seconds} as
    JSON. A JSON answer with a "reply" string is spoken; any other 2xx is
    success with nothing to say, so a fire-and-forget automation needs no
    special reply."""

    type: Literal["webhook"]
    url: Url
    # Optional bearer for receivers that check one. Note that a URL which is
    # itself the secret (HA's /api/webhook/<id>) is shown by GET
    # /satellites/routing like any other URL; prefer a token here when the
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
            "wake_word": req.wake_word, "text": req.text,
            "audio_seconds": round(req.audio_seconds, 3)})
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


Destination = Annotated[HaConversation | Llm | Webhook | Echo, Field(discriminator="type")]
