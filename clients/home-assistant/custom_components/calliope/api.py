"""A small client for the Calliope gateway: HTTP routes and the event stream.

Everything goes to one base URL, the gateway (https://host:30080). The only
route that never needs a key is /health, so a key is checked against
/v1/models, which the gateway answers itself.
"""

from __future__ import annotations

import io
import json
import logging
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from .const import SSE_READ_TIMEOUT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Transcription and synthesis are measured in seconds of audio; a long
# announcement through the hub's /say waits for Kokoro before it answers.
AUDIO_TIMEOUT = aiohttp.ClientTimeout(total=120)


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
            return str(err.get("message") or f"HTTP {status}"), err.get("code")
        detail = data.get("detail")
        if detail is not None:
            return str(detail)[:300], None
    return f"HTTP {status}", None


class CalliopeClient:
    """The gateway's routes that the integration uses."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, api_key: str | None = None
    ) -> None:
        """Keep the session Home Assistant owns; nothing here closes it."""
        self._session = session
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _check(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        body = await resp.text()
        message, code = _error_from(resp.status, body)
        if resp.status == 401:
            raise CalliopeAuthError(message)
        raise CalliopeApiError(resp.status, message, code)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
        raw: bool = False,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                json=json_body,
                data=data,
                headers=self._headers,
                timeout=timeout,
            ) as resp:
                await self._check(resp)
                if raw:
                    return await resp.read()
                if resp.status == 204 or resp.content_length == 0:
                    return None
                return await resp.json(content_type=None)
        except CalliopeError:
            raise
        except TimeoutError as err:
            raise CalliopeConnectionError(
                f"{method} {path} timed out after {timeout.total:.0f} s"
            ) from err
        except (aiohttp.ClientError, ValueError) as err:
            raise CalliopeConnectionError(f"{method} {path}: {err}") from err

    # -- the stack -------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health: never needs a key, always 200; read `status`."""
        return await self._request("GET", "/health")

    async def check_key(self) -> None:
        """GET /v1/models, answered by the gateway itself and behind the key."""
        await self._request("GET", "/v1/models")

    async def voices(self) -> dict[str, Any]:
        """GET /voices: Kokoro's voices."""
        return await self._request("GET", "/voices")

    async def transcribe(
        self, wav: bytes, *, model: str = "parakeet", language: str | None = None
    ) -> str:
        """POST /v1/audio/transcriptions with a WAV; the transcript's text."""
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="speech.wav", content_type="audio/wav")
        form.add_field("model", model)
        form.add_field("response_format", "json")
        if language:
            form.add_field("language", language)
        result = await self._request(
            "POST", "/v1/audio/transcriptions", data=form, timeout=AUDIO_TIMEOUT
        )
        return str((result or {}).get("text") or "")

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
        """POST /satellites/{id}/{action}: identify, say, tone, flush, ptt."""
        await self._request(
            "POST",
            f"/satellites/{satellite_id}/{action}",
            json_body=body if body is not None else {},
            timeout=AUDIO_TIMEOUT if action == "say" else REQUEST_TIMEOUT,
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
