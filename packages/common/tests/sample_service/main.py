"""A minimal consumer, built the way the real services are.

Its job is to be the app voice_common.conformance runs against, so that the
suite the package ships is proven against a real FastAPI app here rather than
only in the consumers' CI.

identity.install is called BEFORE a middleware of the service's own on
purpose: the guard is placed outermost whatever the order, and the suite's
header probe would fail if the later middleware could see the assertion.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import Field

from voice_common import errors, health, identity, logging as voice_logging
from voice_common.models import OpenAISpeechRequest, Segment

log = voice_logging.setup("sample-service", "TTS")

app = FastAPI(title="sample-service")
errors.install_errors(app)
health.install_health(app, details=lambda: {"threads": 4})
identity.install(app, "tts")


@app.middleware("http")
async def no_assertion_here(request, call_next):  # noqa: ANN001, ANN201
    if any(name.startswith("x-calliope-") for name in request.headers):
        raise AssertionError("a middleware saw an assertion header")
    return await call_next(request)


class SpeechRequest(OpenAISpeechRequest):
    response_format: str = Field(default="wav", pattern="^(wav|pcm)$")


class SpeakRequest(OpenAISpeechRequest):
    segments: list[Segment] | None = None


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest) -> dict[str, object]:
    return {"format": req.response_format, "input": req.input}


@app.post("/speak")
async def speak(req: SpeakRequest) -> dict[str, object]:
    return {"input": req.input}
