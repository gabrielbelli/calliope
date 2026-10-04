"""Which entities each satellite gets, from its caps, and how they follow
the hub: caps that change, orphans from 0.1, device info, and writes that
touch only the satellite that changed."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN

from .conftest import until
from .fake_calliope import KITCHEN_ID, LOUNGE_ID, FakeCalliope, pi

ALWAYS = {"binary_sensor.{n}_online", "update.{n}_firmware"}
ALWAYS_OFF = {"sensor.{n}_wi_fi_signal", "sensor.{n}_uptime", "button.{n}_restart"}
KORVO_BUTTONS = {
    "event.kitchen_play_button",
    "event.kitchen_set_button",
    "event.kitchen_mode_button",
    "event.kitchen_rec_button",
    "event.kitchen_key1_button",
    # Named "VOL+ button" and "VOL- button", which slug alike: their ids
    # come from the button's id.
    "event.kitchen_vol_up_button",
    "event.kitchen_vol_down_button",
}
MIC = {
    "switch.{n}_microphone",
    "number.{n}_microphone_gain",
    "event.{n}_voice",
    "sensor.{n}_last_command",
    "sensor.{n}_last_wake_word",
}
SPEAKER = {
    "media_player.{n}",
    "switch.{n}_speaker",
    "select.{n}_plays_through",
    "button.{n}_identify",
}


def _named(ids: set[str], name: str) -> set[str]:
    return {i.format(n=name) for i in ids}


def _device(hass: HomeAssistant, sid: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, sid)})


def _entities(hass: HomeAssistant, sid: str) -> dict[str, bool]:
    """Entity id to whether it is enabled, disabled ones included."""
    device = _device(hass, sid)
    return {
        e.entity_id: e.disabled_by is None
        for e in er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True
        )
    }


def _registered(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    sid: str,
    name: str,
    keys: dict[str, str],
) -> None:
    """The device and entities an earlier version left in the registries:
    unique id key to entity id."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, sid)}, name=name
    )
    for key, entity_id in keys.items():
        domain, object_id = entity_id.split(".")
        er.async_get(hass).async_get_or_create(
            domain,
            DOMAIN,
            f"{sid}_{key}",
            suggested_object_id=object_id,
            config_entry=entry,
            device_id=device.id,
        )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_korvo_gets_what_it_has(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Its speaker, microphone, ring, privacy mute and seven buttons: 25
    entities, the Wi-Fi signal, uptime, restart and buttons off by
    default."""
    entities = _entities(hass, KITCHEN_ID)
    enabled = {e for e, on in entities.items() if on}
    disabled = {e for e, on in entities.items() if not on}
    assert enabled == (
        _named(ALWAYS | MIC | SPEAKER, "kitchen")
        | {
            "assist_satellite.kitchen",
            "switch.kitchen_lights",
            "number.kitchen_ring_brightness",
            "binary_sensor.kitchen_privacy_mute",
        }
    )
    assert disabled == _named(ALWAYS_OFF, "kitchen") | KORVO_BUTTONS
    assert len(entities) == 25


async def test_a_pi_gets_no_ring_no_buttons_and_its_airplay(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Its sound cards, AirPlay and health; no lights, brightness, privacy
    mute or buttons. Its gain goes only as far as the Pi takes it."""
    entities = _entities(hass, LOUNGE_ID)
    assert set(entities) == (
        _named(ALWAYS | ALWAYS_OFF | MIC | SPEAKER, "lounge")
        | {
            "assist_satellite.lounge",
            "select.lounge_output",
            "select.lounge_microphone_input",
            "switch.lounge_echo_reference",
            "switch.lounge_airplay",
            "text.lounge_airplay_name",
            "sensor.lounge_cpu_temperature",
            "binary_sensor.lounge_under_voltage",
        }
    )
    assert {e for e, on in entities.items() if not on} == _named(
        ALWAYS_OFF, "lounge"
    ) | {"switch.lounge_echo_reference"}
    assert hass.states.get("number.lounge_microphone_gain").attributes["max"] == 3.5
    assert hass.states.get("number.kitchen_microphone_gain").attributes["max"] == 37.5


async def test_a_pi_without_a_microphone_gets_no_voice_entities(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """No microphone: nothing heard, no Assist satellite, no input device
    and no echo reference."""
    fake.satellites[LOUNGE_ID] = pi(LOUNGE_ID, "lounge", mic=False)
    await _setup(hass, entry)
    entities = set(_entities(hass, LOUNGE_ID))
    assert not entities & _named(MIC, "lounge")
    assert not entities & {
        "assist_satellite.lounge",
        "select.lounge_microphone_input",
        "switch.lounge_echo_reference",
    }
    assert _named(SPEAKER, "lounge") <= entities
    assert "select.lounge_output" in entities
    await _unload(hass, entry)


async def test_unknown_caps_create_only_what_every_satellite_has_and_remove_nothing(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A satellite the hub has never seen connected: no guess at its
    hardware, and nothing an earlier run made is removed on a guess."""
    fake.satellites[LOUNGE_ID]["caps"] = {}
    _registered(hass, entry, LOUNGE_ID, "lounge", {"lights": "switch.lounge_lights"})
    await _setup(hass, entry)
    assert set(_entities(hass, LOUNGE_ID)) == _named(ALWAYS | ALWAYS_OFF, "lounge") | {
        "switch.lounge_lights"
    }
    await _unload(hass, entry)


async def test_entities_follow_caps_that_change(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A USB microphone plugged into the Pi brings the voice entities; taken
    out, they leave the registry."""
    fake.satellites[LOUNGE_ID] = pi(LOUNGE_ID, "lounge", mic=False)
    await _setup(hass, entry)
    assert not set(_entities(hass, LOUNGE_ID)) & _named(MIC, "lounge")

    fake.satellites[LOUNGE_ID] = pi(LOUNGE_ID, "lounge")
    fake.push({"type": "online", "satellite": LOUNGE_ID, "name": "lounge"})
    await until(hass, lambda: hass.states.get("event.lounge_voice") is not None)
    assert _named(MIC, "lounge") <= set(_entities(hass, LOUNGE_ID))
    assert hass.states.get("assist_satellite.lounge") is not None

    fake.satellites[LOUNGE_ID] = pi(LOUNGE_ID, "lounge", mic=False)
    fake.push({"type": "online", "satellite": LOUNGE_ID, "name": "lounge"})
    await until(
        hass, lambda: not set(_entities(hass, LOUNGE_ID)) & _named(MIC, "lounge")
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.lounge_voice") is None
    assert "assist_satellite.lounge" not in _entities(hass, LOUNGE_ID)
    await _unload(hass, entry)


async def test_orphans_from_before_are_removed(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """0.1 gave every satellite a volume slider and a Lights switch. The
    media player replaces the slider everywhere; the Pi has no ring, so its
    Lights go too, and the Korvo keeps its own under the same id."""
    _registered(
        hass,
        entry,
        KITCHEN_ID,
        "kitchen",
        {"volume": "number.kitchen_volume", "lights": "switch.kitchen_lights"},
    )
    _registered(
        hass,
        entry,
        LOUNGE_ID,
        "lounge",
        {"volume": "number.lounge_volume", "lights": "switch.lounge_lights"},
    )
    await _setup(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get("number.kitchen_volume") is None
    assert registry.async_get("number.lounge_volume") is None
    assert registry.async_get("switch.lounge_lights") is None
    lights = registry.async_get("switch.kitchen_lights")
    assert lights is not None
    assert lights.unique_id == f"{KITCHEN_ID}_lights"
    assert hass.states.get("switch.kitchen_lights").state == "off"
    await _unload(hass, entry)


async def test_surviving_entities_keep_their_unique_ids_and_entity_ids(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Entity ids someone chose in 0.1 (the key suffixes are unchanged) are
    the same entities after the upgrade."""
    _registered(
        hass,
        entry,
        KITCHEN_ID,
        "kitchen",
        {
            "online": "binary_sensor.kitchen_connected",
            "rssi": "sensor.kitchen_rssi",
            "identify": "button.kitchen_blink",
            "microphone": "switch.kitchen_mic",
            "speaker": "switch.kitchen_loudspeaker",
            "lights": "switch.kitchen_ring",
            "voice": "event.kitchen_heard",
            "last_command": "sensor.kitchen_said",
            "last_wake_word": "sensor.kitchen_woken_by",
        },
    )
    await _setup(hass, entry)
    for entity_id in (
        "binary_sensor.kitchen_connected",
        "sensor.kitchen_rssi",
        "button.kitchen_blink",
        "switch.kitchen_mic",
        "switch.kitchen_loudspeaker",
        "switch.kitchen_ring",
        "event.kitchen_heard",
        "sensor.kitchen_said",
        "sensor.kitchen_woken_by",
    ):
        assert hass.states.get(entity_id).state != "unavailable", entity_id
    assert hass.states.get("switch.kitchen_microphone") is None
    assert hass.states.get("switch.kitchen_mic").state == "on"
    await _unload(hass, entry)


async def test_device_info_follows_the_hub(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Manufacturer from the model, a link to the Satellites page, a rename
    made on the page, and the firmware after an update."""
    kitchen, lounge = _device(hass, KITCHEN_ID), _device(hass, LOUNGE_ID)
    assert (kitchen.manufacturer, kitchen.model) == ("Espressif", "esp32-korvo-v1.1")
    assert (lounge.manufacturer, lounge.model) == ("Raspberry Pi", "raspberry-pi")
    assert kitchen.configuration_url == fake.url + "/ui"
    assert kitchen.sw_version == "v0.1.2-163-gf60932d"

    fake.satellites[KITCHEN_ID]["name"] = "cozinha"
    fake.push({"type": "config", "satellite": KITCHEN_ID, "changed": ["name"]})
    await until(hass, lambda: _device(hass, KITCHEN_ID).name == "cozinha")

    fake.satellites[KITCHEN_ID]["firmware"] = "v0.1.3"
    fake.push(
        {
            "type": "online",
            "satellite": KITCHEN_ID,
            "name": "cozinha",
            "firmware": "v0.1.3",
        }
    )
    await until(hass, lambda: _device(hass, KITCHEN_ID).sw_version == "v0.1.3")


@pytest.mark.usefixtures("kitchen_wifi_signal")
async def test_a_status_writes_only_its_own_satellites_entities(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Every satellite reports every 10 s: the kitchen's status rewrites the
    kitchen's entities, and leaves the lounge's alone."""
    before = hass.states.get("binary_sensor.lounge_online").last_reported
    player = hass.states.get("media_player.lounge").last_reported
    fake.push(
        {
            "type": "status",
            "satellite": KITCHEN_ID,
            "status": {"rssi": -44, "volume": 60, "muted": False},
        }
    )
    await until(
        hass, lambda: hass.states.get("sensor.kitchen_wi_fi_signal").state == "-44"
    )
    assert hass.states.get("binary_sensor.lounge_online").last_reported == before
    assert hass.states.get("media_player.lounge").last_reported == player


async def test_a_failed_read_after_the_stream_opens_reconnects(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A hub that restarts may take the stream before it can list its
    satellites. A failed read left every entity unavailable until some event
    asked again; now the stream is opened again, with backoff, until the
    read works."""
    fake.list_status = 500
    fake.drop_streams()
    await fake.wait_streams(4)
    assert hass.states.get("switch.kitchen_microphone").state == "unavailable"

    fake.list_status = None
    await until(
        hass, lambda: hass.states.get("switch.kitchen_microphone").state == "on"
    )


async def test_a_single_read_does_not_cancel_a_pending_full_refresh(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A wake word change asks for a full read, which waits out the
    previous one's cooldown; reading one satellite meanwhile (a config event)
    must not cancel it."""
    coordinator = loaded.runtime_data.coordinator
    coordinator._debounced_refresh.cooldown = 0.5
    reads = len(fake.calls("GET", "/satellites"))
    fake.push({"type": "wake_words"})
    await until(hass, lambda: len(fake.calls("GET", "/satellites")) == reads + 1)
    fake.push({"type": "wake_words"})  # within the cooldown: at its end
    fake.push({"type": "config", "satellite": KITCHEN_ID, "changed": ["volume"]})
    await until(hass, lambda: fake.calls("GET", f"/satellites/{KITCHEN_ID}"))
    await until(hass, lambda: len(fake.calls("GET", "/satellites")) == reads + 2)


async def test_a_read_cancelled_by_an_unload_starts_no_other(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A change that arrived during a read asks for one more; an unload that
    cancels the read must not start it, or it would outlive the entry (Home
    Assistant cancels only the tasks it had when the unload began)."""
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator._refresh_one(KITCHEN_ID)
    coordinator._refresh_one(KITCHEN_ID)  # during the first: queued
    await hass.config_entries.async_unload(entry.entry_id)
    reads = [
        t
        for t in asyncio.all_tasks()
        if t.get_name() == f"{DOMAIN} read {KITCHEN_ID}" and not t.done()
    ]
    assert reads == []
    assert not coordinator._refreshing
