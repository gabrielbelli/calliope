"""A small client for the Calliope gateway: HTTP routes and the event stream.

Everything goes to one base URL, the gateway, and every request carries the
key. /health is the one route that answers without it, and then says only
whether the stack is up: the engines Assist offers are in the answer only for
a key that holds health:read. So a key is checked against /v1/models, which
the gateway answers itself.

A key the gateway refuses is a 401 (CalliopeAuthError: Home Assistant asks for
a new one). A key it knows but that lacks a route's scope is a 403 that names
the scopes in its WWW-Authenticate challenge (CalliopeScopeError), and the
client hands it to `on_missing_scope` before raising, so every route reports a
missing scope the same way without each caller doing it. /health is the
exception: it never refuses, and answers a key without health:read with its
status alone (lacks_health_read).
"""

from __future__ import annotations

import io
import json
import logging
import re
import string
import wave
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from .const import GLOSSARY_PROFILE, SSE_READ_TIMEOUT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Transcription and synthesis are measured in seconds of audio; a long
# announcement through the hub's /say waits for Kokoro before it answers.
AUDIO_TIMEOUT = aiohttp.ClientTimeout(total=120)
# A media upload lasts as long as the music: the hub answers only when the
# stream ends or is stopped, so neither the whole request nor a read has a
# limit. The gateway ends an upload that stalls (GATEWAY_SATELLITES_TIMEOUT).
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
# The hub waits up to 6 s for the phone to answer an AirPlay command.
AIRPLAY_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CalliopeError(Exception):
    """Anything that went wrong talking to Calliope."""


class CalliopeConnectionError(CalliopeError):
    """The gateway could not be reached, or did not answer in time."""


class CalliopeAuthError(CalliopeError):
    """The gateway refused the API key (401)."""


class CalliopeApiError(CalliopeError):
    """The gateway or a backend answered with an error."""

    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        """Keep the status and the envelope's code, for callers that branch."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class CalliopeScopeError(CalliopeApiError):
    """The gateway knows the key, and it lacks the scope a route needs (403)."""

    def __init__(self, message: str, scopes: tuple[str, ...]) -> None:
        """`scopes`: what the route needs, as the challenge names them."""
        super().__init__(403, message, "insufficient_scope")
        self.scopes = scopes

    @property
    def lacking(self) -> tuple[str, ...]:
        """What to tell the user the key lacks: the challenge's scopes, or a
        fixed phrase for a challenge that names none. Never the server's
        message: it is free text, and this goes into a repair issue and a
        form, which Home Assistant renders as Markdown."""
        return self.scopes or (UNNAMED_SCOPE,)


# The gateway's key format (services/gateway/app/tokens.py): the prefix, 30
# random base62 characters and a CRC32 of the prefix and those 30, as six
# base62 digits. Checked here so a key pasted short, or with one character
# wrong, is refused in the form rather than as "Calliope refused the key". A
# service key (calliope_svc_) does not match: it belongs to a service, not to
# Home Assistant.
KEY_PREFIX = "calliope_"
_KEY = re.compile(r"calliope_([0-9A-Za-z]{30})([0-9A-Za-z]{6})")
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
# Bearer error="insufficient_scope", scope="models:read speech:speak"
_CHALLENGE_SCOPE = re.compile(r'scope="([^"]*)"')
# The gateway's scope grammar (packages/common/voice_common/scopes.py), which
# its challenge always keeps to. Only names of this shape are taken from a
# challenge: a server that is not the gateway, or an answer changed on its way
# over plain http://, could otherwise put a link in front of the user where
# they are told what to do about their key.
SCOPE = re.compile(r"^[a-z]+:[a-z]+(:own|:all)?$")
# What a refusal is said to lack when its challenge names no such scope.
UNNAMED_SCOPE = "a scope the home-assistant preset holds"


def _checksum(body: str) -> str:
    number = zlib.crc32((KEY_PREFIX + body).encode("ascii"))
    digits = []
    for _ in range(6):
        number, rest = divmod(number, 62)
        digits.append(_BASE62[rest])
    return "".join(reversed(digits))


def well_formed_key(key: str) -> bool:
    """Whether `key` has a Calliope API key's shape and its checksum matches."""
    match = _KEY.fullmatch(key)
    return match is not None and match[2] == _checksum(match[1])


HEALTH_READ = "health:read"


def lacks_health_read(health: Any) -> bool:
    """Whether a /health answer is the one a key without health:read gets:
    the status and no backends. Liveness is public, so /health never refuses
    with 403 and this is the only sign of the missing scope. A key the gateway
    does not know gets the same answer, so this means the scope is missing
    only once the key is known to be accepted."""
    return isinstance(health, dict) and "status" in health and "backends" not in health


def wav_bytes(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Headerless PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _error_from(status: int, body: str) -> tuple[str, str | None]:
    """The message and code from an OpenAI envelope, FastAPI's detail, or
    the raw text."""
    try:
        data = json.loads(body)
    except ValueError:
        return (body.strip()[:300] or f"HTTP {status}"), None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or f"HTTP {status}")[:300], err.get("code")
        detail = data.get("detail")
        if detail is not None:
            return str(detail)[:300], None
    return f"HTTP {status}", None


class CalliopeClient:
    """The gateway's routes that the integration uses."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str,
        *,
        on_missing_scope: Callable[[CalliopeScopeError], None] | None = None,
    ) -> None:
        """Keep the session Home Assistant owns; nothing here closes it."""
        self._session = session
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._on_missing_scope = on_missing_scope

    async def _check(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        body = await resp.text()
        message, code = _error_from(resp.status, body)
        if resp.status == 401:
            raise CalliopeAuthError(message)
        if resp.status == 403 and code == "insufficient_scope":
            challenge = _CHALLENGE_SCOPE.search(resp.headers.get("WWW-Authenticate", ""))
            named = challenge[1].split() if challenge else []
            err = CalliopeScopeError(
                message, tuple(scope for scope in named if SCOPE.fullmatch(scope))
            )
            if self._on_missing_scope is not None:
                self._on_missing_scope(err)
            raise err
        raise CalliopeApiError(resp.status, message, code)

    @asynccontextmanager
    async def _open(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """One request, its error answered with the gateway's reason, and a
        failure to reach it as CalliopeConnectionError, also while the body is
        read."""
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                json=json_body,
                data=data,
                headers={**self._headers, **(headers or {})},
                timeout=timeout,
            ) as resp:
                await self._check(resp)
                yield resp
        except CalliopeError:
            raise
        except TimeoutError as err:
            # A stream has no overall limit: only its connect can time out.
            limit = timeout.total or timeout.sock_connect
            raise CalliopeConnectionError(
                f"{method} {path} timed out after {limit:.0f} s"
            ) from err
        except (aiohttp.ClientError, ValueError) as err:
            raise CalliopeConnectionError(f"{method} {path}: {err}") from err

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
        raw: bool = False,
    ) -> Any:
        async with self._open(
            method,
            path,
            json_body=json_body,
            data=data,
            headers=headers,
            timeout=timeout,
        ) as resp:
            if raw:
                return await resp.read()
            if resp.status == 204 or resp.content_length == 0:
                return None
            return await resp.json(content_type=None)

    # -- the stack -------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health: always 200 and `status`; the backends' detail only
        when the key holds health:read."""
        return await self._request("GET", "/health")

    async def check_key(self) -> None:
        """GET /v1/models, answered by the gateway itself and behind the key."""
        await self._request("GET", "/v1/models")

    async def check_transcribe(self) -> None:
        """POST /v1/audio/transcriptions without audio: the gateway checks the
        key's scope before the stack sees the request, so only a key without
        speech:transcribe gets a 403. One with it gets the stack's refusal of
        a request that has no file, and nothing is transcribed."""
        await self._request("POST", "/v1/audio/transcriptions", data=aiohttp.FormData())

    async def check_glossary(self) -> None:
        """GET /glossaries/home-assistant: only a key without glossaries:ha
        gets a 403. One with it gets the profile, or a 404 before Home
        Assistant has first written it."""
        await self._request("GET", f"/glossaries/{GLOSSARY_PROFILE}", raw=True)

    async def voices(self) -> dict[str, Any]:
        """GET /voices: Kokoro's voices."""
        return await self._request("GET", "/voices")

    async def transcribe(
        self,
        wav: bytes,
        *,
        model: str = "parakeet",
        language: str | None = None,
        glossary: str | None = None,
        boost: bool = False,
    ) -> str:
        """POST /v1/audio/transcriptions with a WAV; the transcript's text.

        `glossary` names a profile on the stack; `boost` also sends its terms
        into Parakeet's decoder, which Whisper refuses by name."""
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="speech.wav", content_type="audio/wav")
        form.add_field("model", model)
        form.add_field("response_format", "json")
        if language:
            form.add_field("language", language)
        if glossary:
            form.add_field("glossary", glossary)
        if boost:
            form.add_field("boost", "true")
        result = await self._request(
            "POST", "/v1/audio/transcriptions", data=form, timeout=AUDIO_TIMEOUT
        )
        return str((result or {}).get("text") or "")

    async def put_glossary(self, name: str, text: str) -> None:
        """PUT /glossaries/{name}: create or replace a profile. The stack
        writes nothing if any line is refused, and says which."""
        await self._request("PUT", f"/glossaries/{name}", json_body={"text": text})

    async def speech_pcm(
        self, text: str, voice: str, speed: float | None = None
    ) -> bytes:
        """POST /v1/audio/speech as pcm: headerless 24 kHz 16-bit mono."""
        body: dict[str, Any] = {
            "model": "kokoro",
            "input": text,
            "voice": voice,
            "response_format": "pcm",
        }
        if speed is not None:
            body["speed"] = speed
        return await self._request(
            "POST", "/v1/audio/speech", json_body=body, timeout=AUDIO_TIMEOUT, raw=True
        )

    # -- satellites ------------------------------------------------------------

    async def satellites(self) -> list[dict[str, Any]]:
        """GET /satellites: every satellite the hub knows, adopted or not."""
        result = await self._request("GET", "/satellites")
        return list((result or {}).get("satellites") or [])

    async def satellite(self, satellite_id: str) -> dict[str, Any]:
        """GET /satellites/{id}."""
        return await self._request("GET", f"/satellites/{satellite_id}")

    async def wake_words(self) -> dict[str, Any]:
        """GET /satellites/wake-words."""
        return await self._request("GET", "/satellites/wake-words")

    async def configure(self, satellite_id: str, **changes: Any) -> dict[str, Any]:
        """PATCH /satellites/{id}; the satellite as the hub now describes it."""
        return await self._request(
            "PATCH", f"/satellites/{satellite_id}", json_body=changes
        )

    async def action(
        self, satellite_id: str, action: str, body: dict[str, Any] | None = None
    ) -> None:
        """POST /satellites/{id}/{action}: identify, say, tone, flush, ptt,
        reboot."""
        await self._request(
            "POST",
            f"/satellites/{satellite_id}/{action}",
            json_body=body if body is not None else {},
            timeout=AUDIO_TIMEOUT if action == "say" else REQUEST_TIMEOUT,
        )

    async def media(
        self,
        satellite_id: str,
        chunks: AsyncIterator[bytes],
        *,
        announce: bool,
    ) -> dict[str, Any]:
        """POST /satellites/{id}/media: a WAV in the format the satellite's
        media block names, streamed as it is made. The hub answers when the
        music ends or is stopped, or once an announcement has played.

        When making the WAV fails, that error is raised rather than the broken
        connection it caused: aiohttp reports a body that raised only as a
        failure to send bytes."""
        broke: list[BaseException] = []

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in chunks:
                    yield chunk
            except Exception as err:
                broke.append(err)
                raise

        try:
            return await self._request(
                "POST",
                f"/satellites/{satellite_id}/media?announce={int(announce)}",
                data=body(),
                headers={"Content-Type": "audio/wav"},
                timeout=STREAM_TIMEOUT,
            )
        except CalliopeError:
            if broke:
                raise broke[0] from None
            raise

    async def media_stop(self, satellite_id: str) -> None:
        """POST /satellites/{id}/media/stop: ends the stream that plays on the
        satellite's speaker, whether or not one does."""
        await self._request("POST", f"/satellites/{satellite_id}/media/stop")

    async def airplay(self, satellite_id: str, command: str) -> dict[str, Any]:
        """POST /satellites/{id}/airplay/{command}: asks the phone, through the
        satellite; the answer says whether the phone acted on it."""
        return await self._request(
            "POST",
            f"/satellites/{satellite_id}/airplay/{command}",
            timeout=AIRPLAY_TIMEOUT,
        )

    async def airplay_artwork(
        self, satellite_id: str, sha256: str
    ) -> tuple[bytes | None, str | None]:
        """GET /satellites/{id}/airplay/artwork?v=: the cover, and its type.
        (None, None) when the hub holds no cover or another one: an old
        picture is never taken for the one asked for."""
        path = f"/satellites/{satellite_id}/airplay/artwork?v={sha256}"
        try:
            async with self._open("GET", path) as resp:
                return await resp.read(), resp.content_type
        except CalliopeApiError as err:
            if err.status == 404:
                return None, None
            raise

    async def start_ota(self, satellite_id: str, sha256: str) -> dict[str, Any]:
        """POST /satellites/ota: "started" and "skipped" (id to reason)."""
        return await self._request(
            "POST",
            "/satellites/ota",
            json_body={"satellite": satellite_id, "sha256": sha256},
        )

    @asynccontextmanager
    async def event_stream(self) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """GET /satellites/events, held open. Yields an iterator of events;
        it ends when the hub closes the stream and raises when the connection
        breaks or goes quiet for longer than three keepalives."""
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=15, sock_read=SSE_READ_TIMEOUT
        )
        try:
            async with self._session.get(
                f"{self.url}/satellites/events",
                headers={**self._headers, "Accept": "text/event-stream"},
                timeout=timeout,
            ) as resp:
                await self._check(resp)
                yield _parse_sse(resp.content)
        except CalliopeError:
            raise
        except TimeoutError as err:
            raise CalliopeConnectionError("the event stream went quiet") from err
        except (aiohttp.ClientError, ValueError) as err:
            # ValueError: aiohttp's "line too long", an event over 64 KiB.
            raise CalliopeConnectionError(f"the event stream broke: {err}") from err


async def _parse_sse(content: aiohttp.StreamReader) -> AsyncIterator[dict[str, Any]]:
    """Server-sent events to dicts. Comments (the hub's keepalives) and
    anything that is not a JSON object are skipped."""
    data: list[str] = []
    async for raw in content:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                data = []
                try:
                    event = json.loads(payload)
                except ValueError:
                    _LOGGER.debug("Skipping an event that is not JSON: %.200s", payload)
                    continue
                if isinstance(event, dict):
                    yield event
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" "))
