"""Speech-to-text and text-to-speech, and an Assist pipeline using both."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator

import pytest
from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import (
    PipelineEventType,
    async_pipeline_from_audio_stream,
)
from homeassistant.components.assist_pipeline.pipeline import (
    async_create_default_pipeline,
)
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import PARAKEET_LANGUAGES

from .conftest import until
from .fake_calliope import FakeCalliope

METADATA = stt.SpeechMetadata(
    language="pt-BR",
    format=stt.AudioFormats.WAV,
    codec=stt.AudioCodecs.PCM,
    bit_rate=stt.AudioBitRates.BITRATE_16,
    sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
    channel=stt.AudioChannels.CHANNEL_MONO,
)


async def _audio(seconds: float = 0.5) -> AsyncIterator[bytes]:
    for _ in range(int(seconds / 0.02)):
        yield b"\x00\x00" * 320  # 20 ms of 16 kHz silence


def _stt(hass: HomeAssistant) -> stt.SpeechToTextEntity:
    entity = stt.async_get_speech_to_text_entity(
        hass,
        "stt.calliope_whisper"
        if hass.states.get("stt.calliope_whisper")
        else "stt.calliope_parakeet",
    )
    assert entity is not None
    return entity


async def test_stt_parakeet(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Parakeet's 25 languages are declared; the audio goes as one WAV and
    no language is sent, because Parakeet refuses one."""
    entity = _stt(hass)
    assert entity.supported_languages == PARAKEET_LANGUAGES
    assert "ja" not in entity.supported_languages
    result = await entity.async_process_audio_stream(METADATA, _audio())
    assert result == stt.SpeechResult(
        "turn on the kitchen lights", stt.SpeechResultState.SUCCESS
    )
    [sent] = fake.calls("POST", "/v1/audio/transcriptions")
    assert "language" not in sent
    assert sent["model"] == "parakeet"
    with wave.open(io.BytesIO(sent["file"])) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            16000,
            1,
            2,
        )
        assert wav.getnframes() == 8000


async def test_stt_whisper_takes_the_language(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A deployment running Whisper: its languages, and the hint is sent."""
    fake.stt_model = "whisper"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity = _stt(hass)
    assert "ja" in entity.supported_languages
    result = await entity.async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    assert fake.calls("POST", "/v1/audio/transcriptions")[0]["language"] == "pt"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_stt_failure_is_an_error_result(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The gateway gone mid-request: an error result, not an exception."""
    await fake.stop()
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result == stt.SpeechResult(None, stt.SpeechResultState.ERROR)


async def test_tts_languages_and_voices(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Languages come from the voice prefixes; voices per language."""
    engine = get_engine_instance(hass, "tts.calliope_kokoro")
    assert engine.supported_languages == ["en-GB", "en-US", "es", "pt-BR", "zh-CN"]
    assert engine.default_language == "en-US"  # the test instance is en, country US
    voices = engine.async_get_supported_voices("pt-BR")
    assert [(v.voice_id, v.name) for v in voices] == [
        ("pf_dora", "Dora (female)"),
        ("pm_alex", "Alex (male)"),
        ("pm_santa", "Santa (male)"),
    ]


async def test_tts_audio(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Through Home Assistant's TTS manager: the language's default voice,
    then an explicit voice and speed; Kokoro's pcm comes back as a WAV."""
    media_id = tts.generate_media_source_id(
        hass,
        "Olá, tudo bem?",
        engine="tts.calliope_kokoro",
        language="pt-BR",
        cache=False,
    )
    # Home Assistant turns what an engine returns into mp3 for players.
    extension, data = await tts.async_get_media_source_audio(hass, media_id)
    assert extension == "mp3"
    assert data
    assert fake.calls("POST", "/v1/audio/speech")[-1] == {
        "model": "kokoro",
        "input": "Olá, tudo bem?",
        "voice": "pf_dora",
        "response_format": "pcm",
    }

    media_id = tts.generate_media_source_id(
        hass,
        "Good evening.",
        engine="tts.calliope_kokoro",
        language="en-GB",
        options={"voice": "bf_emma", "speed": 0.9},
        cache=False,
    )
    await tts.async_get_media_source_audio(hass, media_id)
    sent = fake.calls("POST", "/v1/audio/speech")[-1]
    assert (sent["voice"], sent["speed"]) == ("bf_emma", 0.9)


async def test_tts_long_text_is_split(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The speech route refuses input over 4096 characters: the text goes in
    pieces, and the entity returns one WAV of Kokoro's 24 kHz pcm."""
    text = "This is one sentence of a long announcement. " * 200  # 9,200 characters
    engine = get_engine_instance(hass, "tts.calliope_kokoro")
    extension, data = await engine.async_get_tts_audio(text, "en-GB", {})
    assert extension == "wav"
    sent = fake.calls("POST", "/v1/audio/speech")
    assert len(sent) == 3
    assert all(len(s["input"]) <= 4000 for s in sent)
    assert " ".join(s["input"] for s in sent) == text.strip()
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            24000,
            1,
            2,
        )
        assert wav.getnframes() == 3 * 2400


async def test_tts_error(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A voice Kokoro does not have is refused with its reason."""
    media_id = tts.generate_media_source_id(
        hass,
        "Hi.",
        engine="tts.calliope_kokoro",
        language="en-GB",
        options={"voice": "xx_nobody"},
        cache=False,
    )
    with pytest.raises(HomeAssistantError):
        await tts.async_get_media_source_audio(hass, media_id)


async def test_assist_pipeline(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry, hass_client
) -> None:
    """Calliope as both ends of an Assist pipeline: the transcript reaches
    the conversation agent, and its answer is spoken by Kokoro."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    pipeline = await async_create_default_pipeline(
        hass,
        stt_engine_id="stt.calliope_parakeet",
        tts_engine_id="tts.calliope_kokoro",
        pipeline_name="Calliope",
    )
    assert pipeline is not None
    assert (pipeline.stt_engine, pipeline.stt_language) == (
        "stt.calliope_parakeet",
        "en",
    )
    assert pipeline.tts_engine == "tts.calliope_kokoro"

    fake.transcript = "what time is it"
    events = []
    await async_pipeline_from_audio_stream(
        hass,
        context=Context(),
        event_callback=events.append,
        stt_metadata=stt.SpeechMetadata(
            language=pipeline.stt_language,
            format=METADATA.format,
            codec=METADATA.codec,
            bit_rate=METADATA.bit_rate,
            sample_rate=METADATA.sample_rate,
            channel=METADATA.channel,
        ),
        stt_stream=_audio(1.0),
        pipeline_id=pipeline.id,
    )
    by_type = {e.type: e.data for e in events}
    assert PipelineEventType.ERROR not in by_type, by_type.get(PipelineEventType.ERROR)
    assert by_type[PipelineEventType.STT_END]["stt_output"]["text"] == "what time is it"
    speech = by_type[PipelineEventType.INTENT_END]["intent_output"]["response"][
        "speech"
    ]
    answer = speech["plain"]["speech"]
    assert answer
    # What a satellite does with tts-end: fetch the URL.
    url = by_type[PipelineEventType.TTS_END]["tts_output"]["url"]
    client = await hass_client()
    resp = await client.get(url)
    assert resp.status == 200
    assert await resp.read()
    await until(hass, lambda: fake.calls("POST", "/v1/audio/speech"))
    assert fake.calls("POST", "/v1/audio/speech")[0]["input"] == answer
