"""Calliope as the speech-to-text of an Assist pipeline: one entity per engine.

The stack lists the engines it serves in GET /health (backends.stt.health.
models), the default first, with the languages each hears and whether it takes
a `language` field. Each becomes an entity, so Assist's speech-to-text menu
offers what this Calliope actually runs: "Parakeet" (25 languages, detected),
"Parakeet pt-BR" (Portuguese only), and so on. A stack older than that list
names one engine (backends.stt.health.model), which is the one entity.

Audio goes to POST /v1/audio/transcriptions as one WAV, with `model` naming
the entity's engine. Parakeet detects the language itself and refuses a
`language` field, so none is sent to it; Whisper takes one as a hint.

Home Assistant's names (vocabulary.py) go only to an engine that boosts
(`accepts_boost`, Parakeet), boosted unless the stack has decode-time biasing
off (/health's `hotwords`). Whisper is not sent them: it takes a glossary's
terms as hotwords, and the stack measured terms absent from the audio raising
its word error rate by 28%.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeApiError, CalliopeError, wav_bytes
from .const import CONF_LEGACY_STT, GLOSSARY_PROFILE, PARAKEET_LANGUAGES, WHISPER_LANGUAGES
from .coordinator import CalliopeConfigEntry
from .entity import service_device_info

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


# Names for engine ids that read badly as ids. Anything else is shown as its id.
ENGINE_NAMES = {"parakeet-pt-br": "Parakeet pt-BR"}


def stt_engines(health: dict) -> list[dict]:
    """The engines the stack serves, the default first. An engine is a dict:
    id, family, default, languages, accepts_language, accepts_boost."""
    try:
        models = health["backends"]["stt"]["health"]["models"]
    except (KeyError, TypeError):
        models = None
    listed = [
        m for m in (models if isinstance(models, list) else [])
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
    ]
    if listed:
        return listed
    engine = stt_engine(health)
    return [
        {
            "id": engine,
            "family": engine,
            "default": True,
            "languages": WHISPER_LANGUAGES if engine == "whisper" else PARAKEET_LANGUAGES,
            "accepts_language": engine == "whisper",
            "accepts_boost": engine == "parakeet",
        }
    ]


def stt_hotwords(health: dict) -> bool:
    """Whether the stack biases its decoder at all (STT_HOTWORDS); True when
    it does not say."""
    try:
        return health["backends"]["stt"]["health"]["hotwords"] is not False
    except (KeyError, TypeError):
        return True


def refuses_boost(err: CalliopeApiError) -> bool:
    """Whether a 400 is the stack refusing `boost`, not the vocabulary: a
    term its model cannot spell, or biasing that is off."""
    return "'boost'" in err.message


def stt_ready(health: dict) -> bool:
    """Whether the stack's speech-to-text has finished loading its engines."""
    try:
        return health["backends"]["stt"]["health"]["status"] == "ok"
    except (KeyError, TypeError):
        return False


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
    """One speech-to-text entity per engine the stack serves.

    Each is keyed on its engine's id, except the engine that held the one
    entity's id before there were several, which keeps it: a pipeline that
    picked stt.calliope_parakeet still finds the same engine. Which one that
    is was the stack's default, and is kept in the entry once the stack has
    loaded its engines. Keyed on the default flag itself, reordering
    STT_MODELS moved the old entity to another engine, and Assist pipelines,
    which store the entity id, switched engines without a word."""
    health = entry.runtime_data.health
    engines = stt_engines(health)
    legacy = entry.data.get(CONF_LEGACY_STT)
    if legacy is None:
        legacy = next((e["id"] for e in engines if e.get("default")), engines[0]["id"])
        if stt_ready(health):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LEGACY_STT: legacy}
            )
    hotwords = stt_hotwords(health)
    async_add_entities(
        CalliopeSpeechToText(entry, engine, legacy=engine["id"] == legacy, hotwords=hotwords)
        for engine in engines
    )


class CalliopeSpeechToText(stt.SpeechToTextEntity):
    """One engine on the Calliope stack: Parakeet, a fine-tune of it, Whisper."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: CalliopeConfigEntry,
        engine: dict,
        *,
        legacy: bool = False,
        hotwords: bool = True,
    ) -> None:
        """The engine's id, family, languages and what it takes."""
        self._client = entry.runtime_data.client
        self._vocabulary = entry.runtime_data.vocabulary
        self._model = engine["id"]
        self._engine = str(engine.get("family") or self._model)
        self._accepts_language = bool(engine.get("accepts_language"))
        # Names only to an engine that boosts; the boost itself only while
        # the stack has biasing on and has not refused it for this text.
        self._names = bool(engine.get("accepts_boost"))
        self._accepts_boost = self._names and hotwords
        self._boost_refused_for: str | None = None
        languages = engine.get("languages")
        self._languages = (
            [str(lang) for lang in languages]
            if isinstance(languages, list) and languages
            else (WHISPER_LANGUAGES if self._engine == "whisper" else PARAKEET_LANGUAGES)
        )
        if legacy:
            # The id the one entity had before there were several
            # (async_setup_entry).
            self._attr_unique_id = f"{entry.entry_id}_stt"
        else:
            self._attr_unique_id = f"{entry.entry_id}_stt_{self._model}"
        if self._model in ("parakeet", "whisper"):
            self._attr_translation_key = self._model
        else:
            self._attr_name = ENGINE_NAMES.get(self._model, self._model)
        self._attr_device_info = service_device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        """What this engine hears: Parakeet v3's 25 European languages,
        Portuguese alone for its pt-BR fine-tune, or Whisper's."""
        return self._languages

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
        if self._accepts_language and metadata.language:
            language = metadata.language.split("-")[0].lower()
        vocabulary = self._vocabulary
        glossary = (
            GLOSSARY_PROFILE if self._names and vocabulary and vocabulary.available else None
        )
        boost = (
            glossary is not None
            and self._accepts_boost
            and (self._boost_refused_for is None
                 or self._boost_refused_for != vocabulary.written)
        )
        try:
            try:
                text = await self._transcribe(wav, language, glossary, boost)
            except CalliopeApiError as err:
                # The vocabulary must never cost a transcription. A refused
                # boost (a term the model cannot spell, biasing off) keeps
                # the names and their repairs, which the stack still
                # applies, and is not asked for again until the vocabulary
                # changes, or the entity reloads when biasing is off.
                if not (boost and err.status == 400 and refuses_boost(err)):
                    raise
                _LOGGER.warning(
                    "Calliope refused to boost the %s vocabulary; its names go "
                    "without the boost: %s",
                    glossary,
                    err,
                )
                if err.code == "unsupported_parameter":
                    self._accepts_boost = False
                else:
                    self._boost_refused_for = vocabulary.written
                text = await self._transcribe(wav, language, glossary, False)
        except CalliopeApiError as err:
            # Any other 400 on a request that named the vocabulary is the
            # vocabulary's: a profile the stack no longer has (its volume
            # was reset). Heard again without it.
            if glossary is None or err.status != 400:
                _LOGGER.error("Calliope could not transcribe: %s", err)
                return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
            _LOGGER.warning(
                "Calliope refused the %s vocabulary, transcribing without it: %s",
                glossary,
                err,
            )
            if vocabulary is not None and err.message.startswith("Unknown glossary profile"):
                vocabulary.async_lost()
            try:
                text = await self._transcribe(wav, language, None, False)
            except CalliopeError as again:
                _LOGGER.error("Calliope could not transcribe: %s", again)
                return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except CalliopeError as err:
            _LOGGER.error("Calliope could not transcribe: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text.strip(), stt.SpeechResultState.SUCCESS)

    async def _transcribe(
        self, wav: bytes, language: str | None, glossary: str | None, boost: bool
    ) -> str:
        """The transcript, with Home Assistant's vocabulary when named, and
        boosted into the decoder when `boost`."""
        return await self._client.transcribe(
            wav, model=self._model, language=language, glossary=glossary, boost=boost
        )
