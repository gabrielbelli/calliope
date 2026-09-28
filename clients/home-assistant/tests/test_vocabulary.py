"""Home Assistant's names as the stack's `home-assistant` glossary profile."""

from __future__ import annotations

import pytest
from homeassistant.components import stt
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope import vocabulary
from custom_components.calliope.const import GLOSSARY_PROFILE

from .conftest import until
from .fake_calliope import FakeCalliope
from .test_speech import METADATA, _audio, _stt

PUT = f"/glossaries/{GLOSSARY_PROFILE}"


@pytest.fixture(autouse=True)
def quick_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuild 50 ms after a change, not ten seconds."""
    monkeypatch.setattr(vocabulary, "DEBOUNCE_S", 0.05)


@pytest.fixture
async def house(hass: HomeAssistant) -> None:
    """A floor, a room with an alias, a bedside lamp exposed to Assist with an
    alias, and a lamp that is not exposed."""
    assert await async_setup_component(hass, "homeassistant", {})
    fr.async_get(hass).async_create("Térreo")
    ar.async_get(hass).async_create("Quarto", aliases={"Dormitório"})
    entities = er.async_get(hass)
    for key, name in (("cama", "Luz da cama"), ("oculta", "Luz escondida")):
        entry = entities.async_get_or_create(
            "light", "test", key, suggested_object_id=key
        )
        hass.states.async_set(entry.entity_id, "off", {"friendly_name": name})
    entities.async_update_entity(
        "light.cama", aliases=["Abajur", er.COMPUTED_NAME]
    )
    async_expose_entity(hass, "conversation", "light.cama", True)
    async_expose_entity(hass, "conversation", "light.oculta", False)


def _terms(fake: FakeCalliope) -> list[str]:
    text = fake.glossaries[GLOSSARY_PROFILE]
    return [
        ln for ln in text.splitlines() if ln and not ln.startswith("#") and "=" not in ln
    ]


def _repairs(fake: FakeCalliope) -> list[str]:
    return [ln for ln in fake.glossaries[GLOSSARY_PROFILE].splitlines() if " = " in ln]


async def test_written_at_start(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Floors, areas and aliases, then exposed entities and their aliases,
    then the command words of Home Assistant's language. Nothing that is not
    exposed, and no alias that is the name marker rather than a string."""
    await until(hass, lambda: GLOSSARY_PROFILE in fake.glossaries)
    terms = _terms(fake)
    assert terms[:5] == ["Térreo", "Quarto", "Dormitório", "Luz da cama", "Abajur"]
    assert "Luz escondida" not in terms
    assert "turn on" in terms  # the test instance's language is en
    assert "desliga" not in terms
    assert _repairs(fake) == []
    runtime = loaded.runtime_data.vocabulary
    assert runtime.available
    assert runtime.terms == len(terms)


async def test_portuguese_gets_its_command_repairs(
    hass: HomeAssistant, house: None, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """With Portuguese in use, "a luz" run into the verb ("desliga-los da
    cama", heard from the pt-BR model) is rewritten after decoding; English
    gets no such rules (test_written_at_start runs in English)."""
    hass.config.language = "pt-BR"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: GLOSSARY_PROFILE in fake.glossaries)
    repairs = _repairs(fake)
    assert "desliga-los da = desliga a luz da" in repairs
    assert "ligue-los do = ligue a luz do" in repairs
    assert "a luz" in _terms(fake) and "desliga" in _terms(fake)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_stt_asks_for_it(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Once the profile is written, Parakeet gets it by name and boosted."""
    await until(hass, lambda: loaded.runtime_data.vocabulary.available)
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    [sent] = fake.calls("POST", "/v1/audio/transcriptions")
    assert (sent["glossary"], sent["boost"]) == (GLOSSARY_PROFILE, "true")


async def test_whisper_is_not_sent_the_names(
    hass: HomeAssistant, house: None, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Whisper takes a profile's terms as hotwords, and the stack measured
    terms absent from the audio raising its word error rate by 28%: a list
    of every room is mostly absent terms. It is sent no vocabulary."""
    fake.stt_model = "whisper"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.vocabulary.available)
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    [sent] = fake.calls("POST", "/v1/audio/transcriptions")
    assert "glossary" not in sent and "boost" not in sent
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_stack_with_biasing_off_gets_the_names_without_boost(
    hass: HomeAssistant, house: None, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """STT_HOTWORDS=0: /health says so, and every request refused `boost`,
    was sent again without the names, and logged a warning."""
    fake.stt_hotwords = False
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.vocabulary.available)
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    [sent] = fake.calls("POST", "/v1/audio/transcriptions")
    assert sent["glossary"] == GLOSSARY_PROFILE and "boost" not in sent
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rewritten_on_change_only(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A renamed room is written again; a change that alters no name is not."""
    await until(hass, lambda: GLOSSARY_PROFILE in fake.glossaries)
    areas = ar.async_get(hass)
    areas.async_update(areas.async_get_area_by_name("Quarto").id, name="Suíte")
    await until(hass, lambda: len(fake.calls("PUT", PUT)) == 2)
    assert "Suíte" in _terms(fake)
    assert "Quarto" not in _terms(fake)

    areas.async_update(areas.async_get_area_by_name("Suíte").id, icon="mdi:bed")
    loaded.runtime_data.vocabulary.async_schedule()
    await hass.async_block_till_done()
    await until(hass, lambda: not loaded.runtime_data.vocabulary._debouncer._timer_task)
    assert len(fake.calls("PUT", PUT)) == 2


async def test_exposing_an_entity_adds_it(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Exposure is followed as well as the registries."""
    await until(hass, lambda: GLOSSARY_PROFILE in fake.glossaries)
    async_expose_entity(hass, "conversation", "light.oculta", True)
    await until(hass, lambda: "Luz escondida" in _terms(fake))


async def test_a_lost_profile_is_heard_without_and_written_again(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The stack forgot the profile (a reset volume): the utterance is still
    transcribed, without it, and the profile is written again."""
    await until(hass, lambda: loaded.runtime_data.vocabulary.available)
    del fake.glossaries[GLOSSARY_PROFILE]
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result == stt.SpeechResult(
        "turn on the kitchen lights", stt.SpeechResultState.SUCCESS
    )
    first, second = fake.calls("POST", "/v1/audio/transcriptions")
    assert first["glossary"] == GLOSSARY_PROFILE
    assert "glossary" not in second and "boost" not in second
    await until(hass, lambda: GLOSSARY_PROFILE in fake.glossaries)
    assert loaded.runtime_data.vocabulary.available


async def test_a_failed_write_leaves_speech_plain(
    hass: HomeAssistant, house: None, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The stack would not store the profile: setup still succeeds, and
    speech-to-text names no profile the stack does not have."""
    fake.glossary_status = 503
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: fake.calls("PUT", PUT))
    assert not entry.runtime_data.vocabulary.available
    result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
    assert result.result is stt.SpeechResultState.SUCCESS
    [sent] = fake.calls("POST", "/v1/audio/transcriptions")
    assert "glossary" not in sent
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_refused_boost_keeps_the_names_and_is_not_asked_again(
    hass: HomeAssistant, house: None, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A profile holding a term the model cannot spell (as one written before
    typographic apostrophes were straightened did): the stack refuses the
    boost. The names and their repairs, which the stack still applies, stay;
    the boost is not asked for again until the vocabulary changes. Every
    utterance used to go twice, the second time with no names at all."""
    await until(hass, lambda: loaded.runtime_data.vocabulary.available)
    fake.glossaries[GLOSSARY_PROFILE] = "Guest\u2019s Bedroom\n"
    for _ in range(3):
        result = await _stt(hass).async_process_audio_stream(METADATA, _audio())
        assert result == stt.SpeechResult(
            "turn on the kitchen lights", stt.SpeechResultState.SUCCESS
        )
    boosted, *plain = fake.calls("POST", "/v1/audio/transcriptions")
    assert boosted["boost"] == "true"
    assert len(plain) == 3
    assert all(s["glossary"] == GLOSSARY_PROFILE and "boost" not in s for s in plain)
    assert loaded.runtime_data.vocabulary.available  # the profile exists


def test_clean() -> None:
    """One term a line, never a rule or a comment, no duplicates in the
    stack's case-insensitive sense, nothing too short to boost, and capped."""
    assert vocabulary.clean(
        ["  Luz   da cama ", "luz da CAMA", "a=b test", "#Sala", "TV", None, "Sala"]
    ) == ["Luz da cama", "a b test", "Sala"]
    # Typographic punctuation as ASCII, symbols gone: the model spells neither.
    assert vocabulary.clean(
        ["Guest\u2019s Bedroom", "\U0001f4a1 Lamp", "Sala \u2013 TV"]
    ) == ["Guest's Bedroom", "Lamp", "Sala - TV"]
    many = vocabulary.clean(f"lamp {n:03d}" for n in range(300))
    assert len(many) == vocabulary.MAX_TERMS
    assert many[-1] == f"lamp {vocabulary.MAX_TERMS - 1:03d}"


async def test_the_terms_leave_room_for_the_repairs(hass: HomeAssistant) -> None:
    """The stack boosts its first 200 phrases alphabetically, the repairs'
    intended sides among them. A Portuguese house with 165 names sent 186
    terms, the stack chose from 226 phrases, and the ones cut were the
    command words and repairs, silently. The terms now stop where the
    repairs' phrases fit beside them, and what is cut is the last names."""
    hass.config.language = "pt-BR"
    areas = ar.async_get(hass)
    for n in range(300):
        areas.async_create(f"Sala {n:03d}")
    terms = vocabulary.collect(hass)
    intended = {i for _, i in vocabulary.collect_repairs(hass)}
    assert intended and len(terms) + len(intended) == vocabulary.MAX_TERMS
    assert terms[0] == "Sala 000" and "desliga" not in terms
