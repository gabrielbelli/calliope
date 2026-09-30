"""The settings a satellite's caps offer: its sound cards, the satellite it
plays through, microphone gain, ring brightness, AirPlay, and what its
status says about privacy and power."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN

from .conftest import until
from .fake_calliope import (
    BEDROOM_ID,
    KITCHEN_ID,
    LOUNGE_ID,
    PI_SINKS,
    FakeCalliope,
    korvo,
    pi,
)

USB = PI_SINKS[0]["name"]
BUILT_IN = PI_SINKS[1]["name"]


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


def _options(hass: HomeAssistant, entity_id: str) -> list[str]:
    return hass.states.get(entity_id).attributes["options"]


async def _call(hass: HomeAssistant, domain: str, service: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


def _status(fake: FakeCalliope, sid: str, **changes: Any) -> None:
    """The satellite's next status, with these changes."""
    status = fake.satellites[sid]["status"] | changes
    fake.satellites[sid]["status"] = status
    fake.push({"type": "status", "satellite": sid, "status": status})


async def test_output_device_lists_the_outputs_and_patches_the_node_name(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Options are the system default and each device by its description (a
    second card of the same kind numbered); the PATCH names the node. A
    configured card that is unplugged shows as unknown."""
    assert _options(hass, "select.lounge_output") == [
        "default",
        "USB Audio Analog Stereo",
        "Built-in Audio Stereo",
    ]
    assert _state(hass, "select.lounge_output") == "USB Audio Analog Stereo"
    assert _options(hass, "select.lounge_microphone_input") == [
        "default",
        "USB Audio Analog Stereo",
    ]
    assert _state(hass, "select.lounge_microphone_input") == "default"

    await _call(
        hass,
        "select",
        "select_option",
        entity_id="select.lounge_output",
        option="Built-in Audio Stereo",
    )
    await _call(
        hass,
        "select",
        "select_option",
        entity_id="select.lounge_output",
        option="default",
    )
    assert fake.calls("PATCH", f"/satellites/{LOUNGE_ID}") == [
        {"audio_sink": BUILT_IN},
        {"audio_sink": ""},
    ]
    assert _state(hass, "select.lounge_output") == "default"

    audio = fake.satellites[LOUNGE_ID]["status"]["audio"]
    second = PI_SINKS[0] | {"name": "alsa_output.usb-Other_USB_Audio-01.analog-stereo"}
    fake.satellites[LOUNGE_ID]["config"]["audio_sink"] = "alsa_output.gone"
    _status(fake, LOUNGE_ID, audio=audio | {"sinks": [*PI_SINKS, second]})
    fake.push({"type": "config", "satellite": LOUNGE_ID, "changed": ["audio_sink"]})
    await until(
        hass,
        lambda: "USB Audio Analog Stereo (2)" in _options(hass, "select.lounge_output"),
    )
    await until(hass, lambda: _state(hass, "select.lounge_output") == "unknown")


async def test_output_satellite_lists_other_speakers(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Its own speaker, then every other adopted satellite that has one, a
    shared name, or one named "own", told apart by the id's end; the hub's
    refusal is shown."""
    fake.satellites[BEDROOM_ID] = pi(BEDROOM_ID, "lounge", mic=False)
    fake.satellites["0a1b2c3d4e60"] = korvo("0a1b2c3d4e60", "hallway")
    del fake.satellites["0a1b2c3d4e60"]["caps"]["speaker"]
    fake.satellites["0a1b2c3d4e61"] = pi("0a1b2c3d4e61", "own", mic=False)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()

    assert _options(hass, "select.kitchen_plays_through") == [
        "own",
        "lounge (0002)",
        "lounge (4e5f)",
        "own (4e61)",
    ]
    assert _state(hass, "select.kitchen_plays_through") == "own"
    await _call(
        hass,
        "select",
        "select_option",
        entity_id="select.kitchen_plays_through",
        option="lounge (0002)",
    )
    assert fake.calls("PATCH", f"/satellites/{KITCHEN_ID}") == [
        {"output_satellite": LOUNGE_ID}
    ]
    assert _state(hass, "select.kitchen_plays_through") == "lounge (0002)"

    fake.patch_refusal = (422, "that would make a loop", "output_loop")
    with pytest.raises(HomeAssistantError, match="loop") as err:
        await _call(
            hass,
            "select",
            "select_option",
            entity_id="select.kitchen_plays_through",
            option="own",
        )
    assert err.value.translation_key == "hub_refused"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_mic_gain_range_is_the_satellites_own(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The Pi clamps its input at 3.5 dB, the Korvo takes the hub's 37.5; in
    the hub's 0.5 dB steps."""
    lounge = hass.states.get("number.lounge_microphone_gain").attributes
    assert (lounge["min"], lounge["max"], lounge["step"]) == (0, 3.5, 0.5)
    assert lounge["unit_of_measurement"] == "dB"
    assert lounge["mode"] == "box"
    assert hass.states.get("number.kitchen_microphone_gain").attributes["max"] == 37.5
    await _call(
        hass,
        "number",
        "set_value",
        entity_id="number.lounge_microphone_gain",
        value=2.5,
    )
    assert fake.calls("PATCH", f"/satellites/{LOUNGE_ID}") == [{"mic_gain_db": 2.5}]
    assert _state(hass, "number.lounge_microphone_gain") == "2.5"


async def test_ring_brightness(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """1 to 100 %, an integer to the hub."""
    brightness = hass.states.get("number.kitchen_ring_brightness")
    assert brightness.state == "55.0"
    assert (brightness.attributes["min"], brightness.attributes["max"]) == (1, 100)
    await _call(
        hass,
        "number",
        "set_value",
        entity_id="number.kitchen_ring_brightness",
        value=80,
    )
    assert fake.calls("PATCH", f"/satellites/{KITCHEN_ID}") == [{"brightness": 80}]
    assert _state(hass, "number.kitchen_ring_brightness") == "80.0"


async def test_airplay_switch_and_name(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The receiver on and off, and the name phones show; empty is the
    satellite's own name."""
    await _call(hass, "switch", "turn_off", entity_id="switch.lounge_airplay")
    assert _state(hass, "switch.lounge_airplay") == "off"
    name = hass.states.get("text.lounge_airplay_name")
    assert name.state == ""
    assert name.attributes["max"] == 64
    await _call(
        hass, "text", "set_value", entity_id="text.lounge_airplay_name", value="Lounge"
    )
    assert _state(hass, "text.lounge_airplay_name") == "Lounge"
    await _call(
        hass, "text", "set_value", entity_id="text.lounge_airplay_name", value=""
    )
    assert fake.calls("PATCH", f"/satellites/{LOUNGE_ID}") == [
        {"airplay_enabled": False},
        {"airplay_name": "Lounge"},
        {"airplay_name": ""},
    ]
    assert _state(hass, "text.lounge_airplay_name") == ""
    with pytest.raises(ValueError, match="pattern"):
        await _call(
            hass,
            "text",
            "set_value",
            entity_id="text.lounge_airplay_name",
            value="a\tb",
        )


async def test_a_switch_the_hub_has_no_config_for_shows_the_satellites_status(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The hub never stored the Pi's AirPlay setting (the receiver starts
    on): the switch shows what the Pi reports."""
    assert "airplay_enabled" not in fake.satellites[LOUNGE_ID]["config"]
    assert _state(hass, "switch.lounge_airplay") == "on"
    _status(fake, LOUNGE_ID, airplay_enabled=False)
    await until(hass, lambda: _state(hass, "switch.lounge_airplay") == "off")


async def test_privacy_mute_and_under_voltage(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """From each satellite's status; unavailable while it is offline."""
    assert _state(hass, "binary_sensor.kitchen_privacy_mute") == "off"
    assert _state(hass, "binary_sensor.lounge_under_voltage") == "off"
    _status(fake, KITCHEN_ID, muted=True)
    _status(fake, LOUNGE_ID, under_voltage=True)
    await until(
        hass, lambda: _state(hass, "binary_sensor.kitchen_privacy_mute") == "on"
    )
    await until(
        hass, lambda: _state(hass, "binary_sensor.lounge_under_voltage") == "on"
    )
    fake.push({"type": "offline", "satellite": KITCHEN_ID})
    await until(
        hass,
        lambda: _state(hass, "binary_sensor.kitchen_privacy_mute") == "unavailable",
    )


async def test_uptime_is_when_the_satellite_started_and_moves_only_on_a_restart(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Now less the uptime, which moves by a second or so between statuses:
    the state stays until the satellite restarts (a smaller uptime), so a
    status every 10 s is not a new state every 10 s."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{KITCHEN_ID}_uptime",
        suggested_object_id="kitchen_uptime",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()
    started = _state(hass, "sensor.kitchen_uptime")
    assert started not in ("unknown", "unavailable")

    _status(fake, KITCHEN_ID, uptime_s=3610, rssi=-44)
    await until(hass, lambda: _state(hass, "sensor.kitchen_wi_fi_signal") == "-44")
    assert _state(hass, "sensor.kitchen_uptime") == started

    _status(fake, KITCHEN_ID, uptime_s=5)
    await until(hass, lambda: _state(hass, "sensor.kitchen_uptime") != started)
    assert _state(hass, "sensor.kitchen_uptime") > started
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
