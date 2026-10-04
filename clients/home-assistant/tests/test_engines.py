"""One speech-to-text entity per engine the stack serves, following changes."""

from __future__ import annotations

from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.calliope import ENGINE_CHECK, ENGINE_CHECK_LOADING

from .conftest import until
from .fake_calliope import FakeCalliope
from .test_speech import METADATA, _audio

PARAKEET = {"id": "parakeet", "family": "parakeet", "default": True,
            "languages": ["en", "pt"], "accepts_language": False, "accepts_boost": True}
PT_BR = {"id": "parakeet-pt-br", "family": "parakeet", "default": False,
         "languages": ["pt"], "accepts_language": False, "accepts_boost": True}
WHISPER = {"id": "whisper-turbo", "family": "whisper", "default": False,
           "languages": ["en", "ja", "pt"], "accepts_language": True, "accepts_boost": False}


def _entity(hass: HomeAssistant, entity_id: str) -> stt.SpeechToTextEntity:
    entity = stt.async_get_speech_to_text_entity(hass, entity_id)
    assert entity is not None, entity_id
    return entity


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_each_engine_is_an_entity_that_names_its_model(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The default keeps stt.calliope_parakeet; the fine-tune is offered for
    Portuguese only, and is asked for by its own id."""
    fake.stt_models = [PARAKEET, PT_BR, WHISPER]
    await _setup(hass, entry)
    assert _entity(hass, "stt.calliope_parakeet").supported_languages == ["en", "pt"]
    pt_br = _entity(hass, "stt.calliope_parakeet_pt_br")
    assert pt_br.supported_languages == ["pt"]
    assert hass.states.get("stt.calliope_parakeet_pt_br").name == "Calliope Parakeet pt-BR"

    result = await pt_br.async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    sent = fake.calls("POST", "/v1/audio/transcriptions")[-1]
    assert sent["model"] == "parakeet-pt-br"
    assert "language" not in sent

    await _entity(hass, "stt.calliope_whisper_turbo").async_process_audio_stream(
        METADATA, _audio()
    )
    sent = fake.calls("POST", "/v1/audio/transcriptions")[-1]
    assert (sent["model"], sent["language"]) == ("whisper-turbo", "pt")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_new_engine_on_the_stack_appears_without_a_restart(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Checked every ENGINE_CHECK: a stack still loading changes nothing; one
    that serves a different list reloads the entry."""
    fake.stt_models = [PARAKEET]
    await _setup(hass, entry)
    assert hass.states.get("stt.calliope_parakeet_pt_br") is None

    fake.stt_models, fake.stt_status = [], "loading"
    async_fire_time_changed(hass, dt_util.utcnow() + ENGINE_CHECK)
    await hass.async_block_till_done()
    assert hass.states.get("stt.calliope_parakeet") is not None

    fake.stt_models, fake.stt_status = [PARAKEET, PT_BR], "ok"
    async_fire_time_changed(hass, dt_util.utcnow() + 2 * ENGINE_CHECK)
    await until(hass, lambda: hass.states.get("stt.calliope_parakeet_pt_br") is not None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_stack_still_loading_at_setup_is_checked_every_30_s(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Home Assistant and the stack restarting together: the menu fills in
    within ENGINE_CHECK_LOADING of the stack being ready, not ENGINE_CHECK."""
    fake.stt_models, fake.stt_status = [], "loading"
    await _setup(hass, entry)
    assert hass.states.get("stt.calliope_parakeet_pt_br") is None

    fake.stt_models, fake.stt_status = [PARAKEET, PT_BR], "ok"
    async_fire_time_changed(hass, dt_util.utcnow() + ENGINE_CHECK_LOADING)
    await until(hass, lambda: hass.states.get("stt.calliope_parakeet_pt_br") is not None)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_new_default_on_the_stack_does_not_move_an_entity_to_another_engine(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The first entity's id went to whichever engine the stack listed first,
    so STT_MODELS reordered moved stt.calliope_parakeet to the pt-BR
    fine-tune, and every English pipeline that used it with it."""
    fake.stt_models = [PARAKEET, PT_BR]
    await _setup(hass, entry)
    fake.stt_models = [PT_BR | {"default": True}, PARAKEET | {"default": False}]
    async_fire_time_changed(hass, dt_util.utcnow() + ENGINE_CHECK)
    await hass.async_block_till_done()
    await until(hass, lambda: hass.states.get("stt.calliope_parakeet") is not None)
    await hass.async_block_till_done()
    assert _entity(hass, "stt.calliope_parakeet")._model == "parakeet"
    assert _entity(hass, "stt.calliope_parakeet_pt_br")._model == "parakeet-pt-br"
    assert hass.states.get("stt.calliope_parakeet_2") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
