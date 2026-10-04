"""Calliope as the text-to-speech of an Assist pipeline: Kokoro's voices.

The languages are Kokoro's own, read from the voices the stack has
(GET /voices): the first letter of a voice name is its language. The audio
is asked for as headerless pcm, Kokoro's native 24 kHz, so nothing is
encoded on the server, and a message longer than one request takes is sent
in pieces and joined.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components import tts
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import language as language_util

from .api import CalliopeError, wav_bytes
from .const import (
    ATTR_SPEED,
    DOMAIN,
    KOKORO_DEFAULT_VOICES,
    KOKORO_LANGUAGE_BY_PREFIX,
    KOKORO_RATE,
    TTS_MAX_CHARS,
)
from .coordinator import CalliopeConfigEntry
from .entity import service_device_info

PARALLEL_UPDATES = 0


def voice_language(voice: str) -> str | None:
    """en-GB for bm_george; None for a name not in Kokoro's form."""
    if len(voice) > 3 and voice[1] in "fm" and voice[2] == "_":
        return KOKORO_LANGUAGE_BY_PREFIX.get(voice[0])
    return None


def voice_label(voice: str) -> str:
    """Dora (female) for pf_dora."""
    gender = "female" if voice[1] == "f" else "male"
    return f"{voice[3:].replace('_', ' ').title()} ({gender})"


def voices_by_language(voices: list[str]) -> dict[str, list[str]]:
    """Kokoro voices grouped by Home Assistant language tag, sorted."""
    grouped: dict[str, list[str]] = {}
    for voice in sorted(voices):
        if (language := voice_language(voice)) is not None:
            grouped.setdefault(language, []).append(voice)
    return grouped


def split_text(text: str, limit: int = TTS_MAX_CHARS) -> list[str]:
    """Pieces of at most `limit` characters, cut at sentence ends where
    possible, then at spaces."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One text-to-speech entity per gateway."""
    async_add_entities([CalliopeTextToSpeech(hass, entry)])


class CalliopeTextToSpeech(tts.TextToSpeechEntity):
    """Kokoro on the Calliope stack."""

    # Named: Home Assistant's TTS manager refuses an engine whose name is
    # None, which is what a device-named entity reports.
    _attr_has_entity_name = True
    _attr_translation_key = "kokoro"
    _attr_supported_options = [tts.ATTR_VOICE, ATTR_SPEED]

    def __init__(self, hass: HomeAssistant, entry: CalliopeConfigEntry) -> None:
        """Languages and voices from GET /voices at setup."""
        self._client = entry.runtime_data.client
        self._voices = voices_by_language(entry.runtime_data.voices)
        self._attr_unique_id = f"{entry.entry_id}_tts"
        self._attr_device_info = service_device_info(entry)
        self._attr_supported_languages = sorted(self._voices)
        # Home Assistant's own language when Kokoro speaks it, else Kokoro's
        # default voice's (bm_george, en-GB).
        matches = language_util.matches(
            hass.config.language, self._attr_supported_languages, hass.config.country
        )
        self._attr_default_language = matches[0] if matches else "en-GB"

    @callback
    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        """Kokoro's voices for one language."""
        voices = self._voices.get(language)
        if voices is None:
            return None
        return [tts.Voice(voice_id=v, name=voice_label(v)) for v in voices]

    def default_voice(self, language: str) -> str:
        """The voice for a language when none was chosen."""
        if (voice := KOKORO_DEFAULT_VOICES.get(language)) in self._voices.get(
            language, []
        ):
            return voice
        voices = self._voices.get(language) or self._voices.get(
            self._attr_default_language
        )
        if not voices:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_voice",
                translation_placeholders={"language": language},
            )
        return voices[0]

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> tts.TtsAudioType:
        """Kokoro's pcm, joined and wrapped as a WAV."""
        voice = options.get(tts.ATTR_VOICE) or self.default_voice(language)
        speed = options.get(ATTR_SPEED)
        pcm = bytearray()
        try:
            for piece in split_text(message):
                pcm.extend(
                    await self._client.speech_pcm(
                        piece, voice, float(speed) if speed is not None else None
                    )
                )
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="tts_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return "wav", wav_bytes(bytes(pcm), KOKORO_RATE)
