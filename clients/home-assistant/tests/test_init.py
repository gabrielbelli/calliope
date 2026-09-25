"""Setup, devices, and entity states from GET /satellites."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN

from .fake_calliope import KITCHEN_ID, PENDING_ID, FakeCalliope


async def test_setup_exposes_adopted_satellites_only(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The kitchen is a device; the pending satellite is not."""
    assert loaded.state is ConfigEntryState.LOADED
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, KITCHEN_ID)}) is not None
    assert devices.async_get_device(identifiers={(DOMAIN, PENDING_ID)}) is None
    service = devices.async_get_device(
        identifiers={(DOMAIN, f"entry_{loaded.entry_id}")}
    )
    assert service is not None
    assert service.name == "Calliope"

    kitchen = devices.async_get_device(identifiers={(DOMAIN, KITCHEN_ID)})
    assert kitchen.name == "kitchen"
    assert kitchen.via_device_id == service.id
    assert kitchen.connections == {(dr.CONNECTION_NETWORK_MAC, "94:b9:7e:7b:8b:e8")}
    entities = er.async_entries_for_device(er.async_get(hass), kitchen.id)
    assert sorted(e.entity_id for e in entities) == [
        "binary_sensor.kitchen_online",
        "button.kitchen_identify",
        "event.kitchen_voice",
        "number.kitchen_volume",
        "sensor.kitchen_last_command",
        "sensor.kitchen_last_wake_word",
        "sensor.kitchen_wi_fi_signal",
        "switch.kitchen_lights",
        "switch.kitchen_microphone",
        "switch.kitchen_speaker",
    ]
    assert hass.states.get("stt.calliope_parakeet") is not None
    assert hass.states.get("tts.calliope_kokoro") is not None


async def test_states_from_the_hub(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Switches and volume show the hub's config; RSSI the status."""
    assert hass.states.get("binary_sensor.kitchen_online").state == "on"
    assert hass.states.get("sensor.kitchen_wi_fi_signal").state == "-58"
    assert hass.states.get("switch.kitchen_microphone").state == "on"
    assert hass.states.get("switch.kitchen_speaker").state == "on"
    assert hass.states.get("switch.kitchen_lights").state == "off"
    assert hass.states.get("number.kitchen_volume").state == "60.0"
    assert hass.states.get("event.kitchen_voice").state == "unknown"
    assert hass.states.get("event.kitchen_voice").attributes["event_types"] == [
        "wake_word",
        "trigger_word",
        "command",
        "button_press",
        "button_release",
        "conversation_started",
        "conversation_ended",
    ]


async def test_unload(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Unloading ends the stream task and the entities."""
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("switch.kitchen_microphone").state == "unavailable"


async def test_not_ready_while_the_gateway_is_down(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakeCalliope
) -> None:
    """Retried later, not failed."""
    await fake.stop()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_key_refused_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, fake: FakeCalliope
) -> None:
    """A 401 at setup asks for a new key."""
    fake.api_key = "sk-new"
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
