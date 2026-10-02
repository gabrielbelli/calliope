"""The deployed gateway and the services behind it, or nothing at all.

The mocked suite proves the routing rules. It cannot prove the two things that
only a real deployment can disagree about: that these paths exist on the far
side with these shapes, and that streaming a real multipart upload and a real
audio response produces the bytes the client asked for.

This used to run the app in-process, pointed at the backends' own ports. It
cannot any more, and should not: a backend now answers only requests carrying
an assertion signed by ITS gateway's key, which this test does not have and
must never be given. So it talks to the deployed gateway, as any client does,
with a key from the Account page. It is SKIPPED, not failed, when no gateway
is named or it is unreachable -- the usual case on a CI runner, on a train, or
when the NAS is asleep -- because a test suite that fails when someone's house
is offline stops being read.

Nothing here queues a Chatterbox job. tts-long runs one job at a time on a
6.5 GB model that takes minutes to load, and a test suite that enqueued work on
a shared machine would be a test suite people learn to avoid running. The long
path is exercised read-only, through GET /jobs.

    GATEWAY_LIVE_URL=https://calliope.example   the deployed gateway (unset: skipped)
    GATEWAY_LIVE_KEY=calliope_…                 a key from the `user-jobs` preset
    GATEWAY_LIVE=0                              skip even when it is reachable
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

URL = os.getenv("GATEWAY_LIVE_URL", "").rstrip("/")
KEY = os.getenv("GATEWAY_LIVE_KEY", "")


def _reachable() -> str | None:
    """None if the gateway answers /health, otherwise why not."""
    if os.getenv("GATEWAY_LIVE") == "0":
        return "GATEWAY_LIVE=0"
    if not URL or not KEY:
        return "GATEWAY_LIVE_URL and GATEWAY_LIVE_KEY are not both set"
    try:
        httpx.get(f"{URL}/health", timeout=3.0).raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure is a skip
        return f"{URL} is not reachable: {type(exc).__name__}"
    return None


_why = _reachable()
pytestmark = pytest.mark.skipif(_why is not None, reason=f"live gateway: {_why}")


@pytest.fixture
async def live():
    """Real httpx, real sockets, the deployed gateway, and a person's key."""
    async with httpx.AsyncClient(base_url=URL, timeout=60.0,
                                 headers={"Authorization": f"Bearer {KEY}"}) as client:
        yield client


async def test_health_reports_all_three_live_backends(live):
    """The call this component exists to make possible: one poll, every answer,
    for a key holding health:read."""
    response = await live.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok", json.dumps(payload, indent=2)
    for name in ("stt", "tts", "tts_long"):
        assert payload["backends"][name]["reachable"] is True
        assert payload["backends"][name]["health"]["status"] in {"ok", "loading"}


async def test_the_advertised_names_match_what_is_running(live):
    """/v1/models is answered here, so nothing but this test compares it to reality."""
    ids = {m["id"] for m in (await live.get("/v1/models")).json()["data"]}
    assert {"kokoro", "chatterbox", "parakeet"} <= ids

    health = (await live.get("/health")).json()["backends"]
    assert health["stt"]["health"]["model"] == "parakeet"


async def test_voices_are_proxied_from_tts_stack(live):
    response = await live.get("/voices")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["voices"]) > 40
    assert "bm_george" in payload["voices"]


async def test_a_default_model_synthesises_real_audio(live):
    """The whole fast path, end to end: buffer the body, route on `model`, stream back."""
    response = await live.post("/v1/audio/speech", json={
        "model": "kokoro", "input": "Here is the change to make.",
        "voice": "bm_george", "response_format": "mp3"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    # tts-stack's own measurement, forwarded rather than recomputed.
    assert float(response.headers["x-realtime-factor"]) > 0


async def test_an_unknown_model_gets_audio_rather_than_a_400(live):
    """The asymmetry, against the real backend: unknown goes fast and still works."""
    response = await live.post("/v1/audio/speech", json={
        "model": "whatever-the-ui-was-holding", "input": "Short.",
        "response_format": "wav"})

    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


async def test_a_real_upload_streams_through_to_stt(live):
    """A multipart body, forwarded chunk by chunk, transcribed by the real model.

    One second of 16 kHz silence: enough to prove the pipeline answers, small
    enough not to occupy a shared box.
    """
    silence = (b"RIFF" + (36 + 32000).to_bytes(4, "little") + b"WAVEfmt "
               + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
               + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
               + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little")
               + (16).to_bytes(2, "little") + b"data"
               + (32000).to_bytes(4, "little") + b"\x00" * 32000)

    response = await live.post("/v1/audio/transcriptions",
                               files={"file": ("silence.wav", silence, "audio/wav")},
                               data={"model": "whisper-1"})

    assert response.status_code == 200
    assert "text" in response.json()


async def test_the_long_backends_job_list_is_reachable_flat(live):
    """Read-only: GET /jobs proves the unprefixed mount without queueing work."""
    response = await live.get("/jobs")

    assert response.status_code == 200
    assert "jobs" in response.json()


async def test_the_schema_is_not_published_through_the_gateway(live):
    """stt-stack put its own behind a key; a wildcard here would undo that."""
    for path in ("/docs", "/openapi.json"):
        response = await live.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "unknown_url"
