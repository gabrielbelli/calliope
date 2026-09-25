"""Actions (say, tone, push_to_talk), the switches, volume and identify,
and diagnostics."""

from __future__ import annotations

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN
from custom_components.calliope.diagnostics import async_get_config_entry_diagnostics

from .conftest import until
from .fake_calliope import KITCHEN_ID, PENDING_ID, FakeCalliope


def _kitchen(hass: HomeAssistant) -> str:
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KITCHEN_ID)}).id


async def test_say_by_device(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """calliope.say posts the text and voice to the hub."""
    await hass.services.async_call(
        DOMAIN,
        "say",
        {"device_id": _kitchen(hass), "text": "Dinner is ready.", "voice": "pf_dora"},
        blocking=True,
    )
    assert fake.calls("POST", f"/satellites/{KITCHEN_ID}/say") == [
        {"text": "Dinner is ready.", "voice": "pf_dora"}
    ]


async def test_say_by_area_and_entity(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """An area or any of the satellite's entities names the satellite."""
    area = ar.async_get(hass).async_create("Kitchen")
    dr.async_get(hass).async_update_device(_kitchen(hass), area_id=area.id)
    await hass.services.async_call(
        DOMAIN, "say", {"area_id": area.id, "text": "One."}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        "say",
        {"entity_id": "switch.kitchen_speaker", "text": "Two."},
        blocking=True,
    )
    assert fake.calls("POST", f"/satellites/{KITCHEN_ID}/say") == [
        {"text": "One."},
        {"text": "Two."},
    ]


async def test_say_refused(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The hub's reason reaches the person."""
    fake.satellites[KITCHEN_ID]["config"]["speaker_enabled"] = False
    with pytest.raises(HomeAssistantError, match="speaker turned off"):
        await hass.services.async_call(
            DOMAIN,
            "say",
            {"device_id": _kitchen(hass), "text": "Hello."},
            blocking=True,
        )


async def test_no_satellite_in_target(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The Calliope service device is not a satellite."""
    service = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"entry_{loaded.entry_id}")}
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "say", {"device_id": service.id, "text": "Hello."}, blocking=True
        )


async def test_tone(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Frequency and duration, with defaults."""
    await hass.services.async_call(
        DOMAIN, "tone", {"device_id": _kitchen(hass)}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        "tone",
        {"device_id": _kitchen(hass), "frequency": 880, "seconds": 0.25},
        blocking=True,
    )
    assert fake.calls("POST", f"/satellites/{KITCHEN_ID}/tone") == [
        {"frequency": 440.0, "seconds": 1.0},
        {"frequency": 880.0, "seconds": 0.25},
    ]


async def test_push_to_talk_without_a_hub_route(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Today's gateway has no route for it: a clear failure, not a 404."""
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "push_to_talk", {"device_id": _kitchen(hass)}, blocking=True
        )
    assert err.value.translation_key == "push_to_talk_unsupported"


async def test_push_to_talk(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """With the route: POST /satellites/{id}/ptt, the wake word optional."""
    fake.ptt_route = True
    await hass.services.async_call(
        DOMAIN, "push_to_talk", {"device_id": _kitchen(hass)}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        "push_to_talk",
        {"device_id": _kitchen(hass), "wake_word": "hey_jarvis"},
        blocking=True,
    )
    assert fake.calls("POST", f"/satellites/{KITCHEN_ID}/ptt") == [
        {},
        {"wake_word": "hey_jarvis"},
    ]


async def test_switch_and_volume(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Each is one PATCH, and the answer is the new state."""
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.kitchen_lights"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_microphone"}, blocking=True
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.kitchen_volume", "value": 42},
        blocking=True,
    )
    assert fake.calls("PATCH", f"/satellites/{KITCHEN_ID}") == [
        {"lights_enabled": True},
        {"mic_enabled": False},
        {"volume": 42},
    ]
    assert hass.states.get("switch.kitchen_lights").state == "on"
    assert hass.states.get("switch.kitchen_microphone").state == "off"
    assert hass.states.get("number.kitchen_volume").state == "42.0"


async def test_identify(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The ring blinks."""
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.kitchen_identify"}, blocking=True
    )
    assert fake.calls("POST", f"/satellites/{KITCHEN_ID}/identify") == [{}]


async def test_pending_satellites_cause_no_reads(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A satellite waiting to be adopted reports status every 10 s; that
    must not become a read of GET /satellites every 10 s."""
    reads = len(fake.calls("GET", "/satellites"))
    for _ in range(3):
        fake.push({"type": "status", "satellite": PENDING_ID, "status": {"rssi": -60}})
    fake.push(
        {
            "type": "status",
            "satellite": KITCHEN_ID,
            "status": {
                "rssi": -44,
                "volume": 60,
                "mic_enabled": True,
                "speaker_enabled": True,
                "lights_enabled": False,
            },
        }
    )
    await until(
        hass, lambda: hass.states.get("sensor.kitchen_wi_fi_signal").state == "-44"
    )
    assert len(fake.calls("GET", "/satellites")) == reads


async def test_diagnostics_redact_the_key(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """No key, no address, no button webhooks."""
    hass.config_entries.async_update_entry(
        loaded, data={**loaded.data, CONF_API_KEY: "sk-secret"}
    )
    diag = await async_get_config_entry_diagnostics(hass, loaded)
    assert diag["entry"]["data"][CONF_API_KEY] == "**REDACTED**"
    assert "sk-secret" not in str(diag)
    kitchen = diag["satellites"][KITCHEN_ID]
    assert kitchen["address"] == "**REDACTED**"
    assert kitchen["config"]["buttons"] == "**REDACTED**"
    assert diag["event_stream_connected"] is True
    assert diag["voices"] == len(fake.voices)
