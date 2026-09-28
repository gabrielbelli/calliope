"""Calliope as the speech-to-text of an Assist pipeline.

Audio goes to POST /v1/audio/transcriptions as one WAV. The engine is the one
the deployment loaded (GET /health, backends.stt.health.model): Parakeet by
default, which detects the language itself and refuses a `language` field,
so none is sent to it; Whisper takes one as a hint.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeApiError, CalliopeError, wav_bytes
from .const import GLOSSARY_PROFILE, PARAKEET_LANGUAGES, WHISPER_LANGUAGES
from .coordinator import CalliopeConfigEntry
from .entity import service_device_info

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def stt_engine(health: dict) -> str:
    """parakeet or whisper, from the gateway's /health; parakeet when it
    does not say."""
    try:
        model = health["backends"]["stt"]["health"]["model"]
    except (KeyError, TypeError):
        return "parakeet"
    return "whisper" if str(model).lower().startswith("whisper") else "parakeet"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One speech-to-text entity per gateway."""
    async_add_entities([CalliopeSpeechToText(entry)])


class CalliopeSpeechToText(stt.SpeechToTextEntity):
    """Parakeet (or Whisper) on the Calliope stack."""

    _attr_has_entity_name = True

    def __init__(self, entry: CalliopeConfigEntry) -> None:
        """Decide the languages from the engine the stack runs."""
        self._client = entry.runtime_data.client
        self._vocabulary = entry.runtime_data.vocabulary
        self._engine = stt_engine(entry.runtime_data.health)
        self._attr_translation_key = self._engine
        self._attr_unique_id = f"{entry.entry_id}_stt"
        self._attr_device_info = service_device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        """Parakeet v3's 25 European languages, or Whisper's."""
        return WHISPER_LANGUAGES if self._engine == "whisper" else PARAKEET_LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """What Assist sends."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Raw PCM."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """16-bit."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """16 kHz, the rate both engines run at."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Mono."""
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Collect the utterance, send it as one WAV."""
        audio = bytearray()
        async for chunk in stream:
            audio.extend(chunk)
        # A pipeline streams headerless PCM; /api/stt may carry a whole WAV
        # file, which is sent as it is.
        wav = bytes(audio) if audio[:4] == b"RIFF" else wav_bytes(bytes(audio), 16000)
        language = None
        if self._engine == "whisper" and metadata.language:
            language = metadata.language.split("-")[0].lower()
        vocabulary = self._vocabulary
        glossary = GLOSSARY_PROFILE if vocabulary and vocabulary.available else None
        try:
            text = await self._transcribe(wav, language, glossary)
            if text is None and vocabulary is not None:
                # The stack no longer has the profile (its volume was reset):
                # this utterance is heard without it, and it is written again.
                vocabulary.async_lost()
                text = await self._transcribe(wav, language, None)
        except CalliopeError as err:
            _LOGGER.error("Calliope could not transcribe: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult((text or "").strip(), stt.SpeechResultState.SUCCESS)

    async def _transcribe(
        self, wav: bytes, language: str | None, glossary: str | None
    ) -> str | None:
        """The transcript, with Home Assistant's vocabulary when the stack has
        it: boosted into Parakeet's decoder, and as hotwords on Whisper, which
        has no boost switch. None when the stack refused the profile's name."""
        try:
            return await self._client.transcribe(
                wav,
                model="whisper-1" if self._engine == "whisper" else "parakeet",
                language=language,
                glossary=glossary,
                boost=glossary is not None and self._engine == "parakeet",
            )
        except CalliopeApiError as err:
            # The stack's own words for a profile it does not have; its code
            # is the generic invalid_value.
            if glossary and err.status == 400 and err.message.startswith(
                "Unknown glossary profile"
            ):
                return None
            raise
