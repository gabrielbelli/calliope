"""clip_start and clip_end: one window of the file, on the file's own timeline.

voice-ui has no ffmpeg, so a link's Start at and Stop at arrive here as two
fields beside the whole file. Only the window is decoded and transcribed, and
every time comes back shifted onto the file's timeline, so the page can play
the whole file and the highlight still lines up.

The fake engine reports one word per whole second of what it is handed, timed
from zero in that pass, as a real recogniser does: it never knows a window is
a window, so the shift under test is entirely this service's.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest
from starlette.testclient import TestClient
from voice_common.conformance import FakeGateway

from app import asr, audio, glossary, openai_api, pipeline
from app.main import app

RATE = 16_000


def wav(seconds: float) -> bytes:
    frames = int(seconds * RATE)
    tone = 8000 * np.sin(2 * np.pi * 220 * np.arange(frames) / RATE)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(tone.astype("<i2").tobytes())
    return buffer.getvalue()


class Counting:
    """Both engines' shape, as far as a window is concerned: a word a second,
    each in a segment of its own, and the size of every pass it was handed."""

    name = "whisper"
    accepts_vocabulary = True
    accepts_language = True
    accepts_temperature = True
    can_translate = True
    can_stream = True
    reports_language = True
    reports_segments = True
    reports_token_logprobs = False
    reports_token_ids = True

    def __init__(self) -> None:
        self.calls: list[int] = []

    def transcribe(self, samples, opts):  # noqa: ANN001, ANN201
        del opts
        self.calls.append(int(samples.size))
        words = tuple(asr.Word(f"w{n}", float(n), n + 0.5, -0.1)
                      for n in range(int(samples.size // RATE)))
        segments = tuple(asr.Segment(id=n, seek=0, start=w.start, end=w.end, text=" " + w.word,
                                     tokens=(n,), temperature=0.0, avg_logprob=-0.1,
                                     compression_ratio=1.0, no_speech_prob=0.0, words=(w,))
                         for n, w in enumerate(words))
        return asr.Recognition(text=" ".join(w.word for w in words), language="en",
                               segments=segments, words=words)

    def stream(self, samples, opts):  # noqa: ANN001, ANN201
        raise NotImplementedError


@pytest.fixture
def engine() -> Counting:
    return Counting()


@pytest.fixture
def client(gateway: FakeGateway, engine: Counting) -> TestClient:
    """No lifespan, for test_parity's reason: it would load a real model."""
    pipeline.state.clear()
    pipeline.state["asr"] = engine
    pipeline.state["rules"] = glossary.compile_rules({})
    yield TestClient(app, headers=gateway.headers("stt"))
    pipeline.state.clear()


def post(client: TestClient, seconds: float = 10.0, path: str = "/v1/audio/transcriptions",
         **fields):  # noqa: ANN003, ANN201
    return client.post(path, files={"file": ("clip.wav", wav(seconds), "audio/wav")},
                       data={"model": "whisper-1", "response_format": "verbose_json",
                             "timestamp_granularities[]": ["word", "segment"], **fields})


def test_a_window_is_all_the_recogniser_is_handed(client, engine):
    response = post(client, clip_start="2", clip_end="5")
    assert response.status_code == 200, response.text
    [size] = engine.calls
    assert abs(size - 3 * RATE) <= 32


def test_the_times_come_back_on_the_file_s_timeline(client):
    body = post(client, clip_start="2", clip_end="5").json()
    assert [w["start"] for w in body["words"]] == [2.0, 3.0, 4.0]
    assert [s["start"] for s in body["segments"]] == [2.0, 3.0, 4.0]
    assert [s["end"] for s in body["segments"]] == [2.5, 3.5, 4.5]
    assert body["duration"] == pytest.approx(3.0, abs=0.01)


def test_a_window_with_no_end_runs_to_the_end_of_the_file(client, engine):
    body = post(client, clip_start="7").json()
    assert abs(engine.calls[0] - 3 * RATE) <= 32
    assert [w["start"] for w in body["words"]] == [7.0, 8.0, 9.0]


def test_nested_words_are_shifted_too(engine):
    pipeline.state.clear()
    pipeline.state["rules"] = glossary.compile_rules({})
    result = pipeline.run(wav(10.0), asr.Options(want_words=True, want_segments=True),
                          allow_resample=True, recogniser=engine, window=(2.0, 5.0))
    pipeline.state.clear()
    assert [s.words[0].start for s in result.segments] == [2.0, 3.0, 4.0]
    assert [w.start for w in result.words] == [2.0, 3.0, 4.0]
    assert result.audio_seconds == pytest.approx(3.0, abs=0.01)


def test_decoding_stops_at_clip_end(monkeypatch):
    """Memory follows the window, not the file: past clip_end nothing more is decoded."""
    import av.audio.resampler as resampler

    frames: list[object] = []
    real = resampler.AudioResampler

    class Counted:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.real = real(*args, **kwargs)

        def resample(self, frame):  # noqa: ANN001, ANN201
            frames.append(frame)
            return self.real.resample(frame)

    monkeypatch.setattr(resampler, "AudioResampler", Counted)
    data = wav(60.0)
    whole = audio.decode(data)
    decoded_whole = len(frames)
    frames.clear()
    window = audio.decode(data, (1.0, 2.0))
    assert abs(window.samples.size - RATE) <= 32
    assert whole.samples.size == 60 * RATE
    assert len(frames) < decoded_whole / 10, (len(frames), decoded_whole)
    np.testing.assert_allclose(window.samples[:100], whole.samples[RATE:RATE + 100], atol=1e-6)


@pytest.mark.parametrize("fields,param", [
    ({"clip_start": "5", "clip_end": "5"}, "clip_end"),
    ({"clip_start": "5", "clip_end": "2"}, "clip_end"),
    ({"clip_end": "0"}, "clip_end"),
    ({"clip_start": "-1"}, "clip_start"),
    ({"clip_start": "1e308"}, "clip_start"),
    ({"clip_end": "86401"}, "clip_end"),
    ({"clip_start": "nan"}, "clip_start"),
    ({"clip_start": "soon"}, "clip_start"),
])
def test_a_window_that_is_not_one_is_a_400_naming_the_field(client, engine, fields, param):
    response = post(client, **fields)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["param"] == param
    assert not engine.calls


def test_a_window_past_the_end_of_the_file_is_a_400(client):
    response = post(client, 10.0, clip_start="30")
    assert response.status_code == 400
    assert "past the end" in response.json()["error"]["message"]


def test_a_window_cannot_be_streamed(client):
    response = post(client, clip_start="1", stream="true", response_format="json",
                    **{"timestamp_granularities[]": []})
    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("unsupported_parameter", "clip_start")


def test_translations_do_not_take_a_window(client):
    response = client.post("/v1/audio/translations",
                           files={"file": ("clip.wav", wav(2.0), "audio/wav")},
                           data={"model": "whisper-1", "clip_start": "1"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("unknown_parameter", "clip_start")


def test_the_schema_lists_both_fields():
    schema = openai_api._TRANSCRIPTION_SCHEMA["requestBody"]["content"][
        "multipart/form-data"]["schema"]["properties"]
    for name in ("clip_start", "clip_end"):
        assert schema[name] == {"type": "number", "minimum": 0, "maximum": 86400}
        assert name in openai_api.TRANSCRIPTION_FIELDS
        assert name not in openai_api.TRANSLATION_FIELDS
