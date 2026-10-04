"""The event stream: entity states, the bus event, reconnecting."""

from __future__ import annotations

import asyncio
import logging

import pytest
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.calliope.const import DOMAIN, EVENT_CALLIOPE

from .conftest import until
from .fake_calliope import BEDROOM_ID, KITCHEN_ID, LOUNGE_ID, FakeCalliope, korvo


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


def _unavailable(changes: list[Event]) -> list[str]:
    """The entities that became unavailable, from captured state changes."""
    return [
        change.data["entity_id"]
        for change in changes
        if (new := change.data["new_state"]) is not None and new.state == STATE_UNAVAILABLE
    ]


async def test_wake_word(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A wake word reaches the event entity, the sensor and the bus."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {
            "type": "wake",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "score": 0.97,
            "direction": 120,
        }
    )
    await until(hass, lambda: bus)
    voice = hass.states.get("event.kitchen_voice")
    assert voice.attributes["event_type"] == "wake_word"
    assert voice.attributes["wake_word"] == "hey_jarvis"
    assert voice.attributes["score"] == 0.97
    assert _state(hass, "sensor.kitchen_last_wake_word") == "hey_jarvis"
    kitchen = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KITCHEN_ID)})
    assert bus[0].data == {
        "at": 1_700_000_100.0,
        "type": "wake",
        "satellite": KITCHEN_ID,
        "wake_word": "hey_jarvis",
        "score": 0.97,
        "direction": 120,
        "device_id": kitchen.id,
        "satellite_name": "kitchen",
        "kind": "wake_word",
    }


async def test_trigger_word(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The hub's `triggered` is a trigger_word."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {
            "type": "triggered",
            "satellite": KITCHEN_ID,
            "satellite_name": "kitchen",
            "wake_word": "lumos",
            "score": 0.81,
        }
    )
    await until(hass, lambda: bus)
    voice = hass.states.get("event.kitchen_voice")
    assert voice.attributes["event_type"] == "trigger_word"
    assert voice.attributes["wake_word"] == "lumos"
    last = hass.states.get("sensor.kitchen_last_wake_word")
    assert last.state == "lumos"
    assert last.attributes["trigger"] is True
    assert bus[0].data["kind"] == "trigger_word"


async def test_command(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A routed event with a transcript is a command; without one it is not."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {
            "type": "routed",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "rule_id": "default-echo",
            "reply_to": KITCHEN_ID,
            "error": None,
            "transcript": "What time is it?",
            "reply_text": "It is three.",
            "timings_ms": {"stt": 120},
            "endpoint": "silence",
            "command_s": 1.4,
            "played": True,
            "note": None,
        }
    )
    await until(hass, lambda: bus)
    assert _state(hass, "sensor.kitchen_last_command") == "What time is it?"
    last = hass.states.get("sensor.kitchen_last_command")
    assert last.attributes["reply"] == "It is three."
    assert hass.states.get("event.kitchen_voice").attributes["event_type"] == "command"
    assert bus[0].data["kind"] == "command"
    assert bus[0].data["transcript"] == "What time is it?"

    fake.push(
        {
            "type": "routed",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "error": "nothing was said after the wake word",
            "transcript": None,
        }
    )
    await until(hass, lambda: len(bus) == 2)
    assert bus[1].data["kind"] == "routed"
    assert _state(hass, "sensor.kitchen_last_command") == "What time is it?"


async def test_conversations(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A conversation's start, turns and end reach the Voice event entity."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    seen: list[str] = []
    for n, event in enumerate(
        (
            {
                "type": "conversation_started",
                "wake_word": "hey_jarvis",
                "conversation_id": "c1",
            },
            {
                "type": "turn",
                "wake_word": "hey_jarvis",
                "transcript": "and tomorrow?",
                "reply_text": "Rain.",
            },
            {"type": "conversation_ended", "wake_word": "hey_jarvis", "turns": 2},
        ),
        start=1,
    ):
        fake.push({"satellite": KITCHEN_ID} | event)
        await until(hass, lambda n=n: len(bus) == n)
        seen.append(hass.states.get("event.kitchen_voice").attributes["event_type"])
    assert seen == ["conversation_started", "command", "conversation_ended"]
    assert [e.data["kind"] for e in bus] == seen
    assert hass.states.get("event.kitchen_voice").attributes["turns"] == 2
    assert _state(hass, "sensor.kitchen_last_command") == "and tomorrow?"


async def test_button_event_entities_fire_press_and_release(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Each button is its own event entity (off by default, here turned on):
    PLAY's gets PLAY's press and release, and nothing of SET's."""
    er.async_get(hass).async_get_or_create(
        "event",
        DOMAIN,
        f"{KITCHEN_ID}_button_play",
        suggested_object_id="kitchen_play_button",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()
    play = hass.states.get("event.kitchen_play_button")
    assert play.name == "kitchen PLAY button"
    assert play.attributes["device_class"] == "button"
    assert play.attributes["event_types"] == ["press", "release"]

    bus = async_capture_events(hass, EVENT_CALLIOPE)
    seen: list[str] = []
    for n, event in enumerate(
        (
            {"button": "play", "action": "press", "held_ms": 0},
            {"button": "play", "action": "release", "held_ms": 240},
            {"button": "set", "action": "press", "held_ms": 0},
        ),
        start=1,
    ):
        fake.push({"type": "button", "satellite": KITCHEN_ID} | event)
        await until(hass, lambda n=n: len(bus) == n)
        seen.append(
            hass.states.get("event.kitchen_play_button").attributes["event_type"]
        )
    assert seen == ["press", "release", "release"]
    assert hass.states.get("event.kitchen_play_button").attributes["held_ms"] == 240
    assert [e.data["kind"] for e in bus] == [
        "button_press",
        "button_release",
        "button_press",
    ]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_voice_event_has_no_button_types(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Buttons are not something a satellite heard: the Voice entity offers
    only the five voice types, and a press leaves it as it was."""
    voice = hass.states.get("event.kitchen_voice")
    assert voice.attributes["event_types"] == [
        "wake_word",
        "trigger_word",
        "command",
        "conversation_started",
        "conversation_ended",
    ]
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {"type": "button", "satellite": KITCHEN_ID, "button": "play", "action": "press"}
    )
    await until(hass, lambda: bus)
    assert _state(hass, "event.kitchen_voice") == "unknown"


@pytest.mark.usefixtures("kitchen_wifi_signal")
async def test_quiet_and_injected_events(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """status is state, not a happening; an injected test clip fires nothing."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {
            "type": "wake",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "score": 0.99,
            "injected": True,
        }
    )
    fake.push(
        {
            "type": "status",
            "satellite": KITCHEN_ID,
            "status": {
                "rssi": -71,
                "volume": 60,
                "mic_enabled": True,
                "speaker_enabled": True,
                "lights_enabled": False,
            },
        }
    )
    await until(hass, lambda: _state(hass, "sensor.kitchen_wi_fi_signal") == "-71")
    assert bus == []
    assert _state(hass, "event.kitchen_voice") == "unknown"


@pytest.mark.usefixtures("kitchen_wifi_signal")
async def test_offline_and_online(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Offline: connectivity off, RSSI and identify unavailable, the switches
    still take changes (the hub sends them at the next welcome)."""
    fake.satellites[KITCHEN_ID]["online"] = False
    fake.push({"type": "offline", "satellite": KITCHEN_ID})
    await until(hass, lambda: _state(hass, "binary_sensor.kitchen_online") == "off")
    assert _state(hass, "sensor.kitchen_wi_fi_signal") == "unavailable"
    assert _state(hass, "button.kitchen_identify") == "unavailable"
    assert _state(hass, "switch.kitchen_microphone") == "on"

    fake.satellites[KITCHEN_ID]["online"] = True
    fake.push(
        {
            "type": "online",
            "satellite": KITCHEN_ID,
            "name": "kitchen",
            "firmware": "0.4.3",
        }
    )
    await until(hass, lambda: _state(hass, "binary_sensor.kitchen_online") == "on")
    await until(hass, lambda: _state(hass, "sensor.kitchen_wi_fi_signal") == "-58")


async def test_change_made_elsewhere(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A PATCH from the Satellites page is published as config, with the
    names of what changed: the record is read at once, whatever the setting,
    and the event is not fired on the bus."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.satellites[KITCHEN_ID]["config"]["speaker_enabled"] = False
    fake.satellites[KITCHEN_ID]["config"]["brightness"] = 20
    fake.push(
        {
            "type": "config",
            "satellite": KITCHEN_ID,
            "changed": ["brightness", "speaker_enabled"],
        }
    )
    await until(hass, lambda: _state(hass, "switch.kitchen_speaker") == "off")
    assert _state(hass, "number.kitchen_ring_brightness") == "20.0"
    assert fake.calls("GET", f"/satellites/{KITCHEN_ID}")
    assert bus == []


async def test_a_media_event_reads_the_satellite_that_plays_and_the_one_addressed(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The kitchen answers through the lounge's speaker: a stream sent to
    the kitchen plays in the lounge, and both records describe it."""
    fake.satellites[KITCHEN_ID]["config"]["output_satellite"] = LOUNGE_ID
    playing = {"id": "ab" * 16, "source": KITCHEN_ID, "since": 1_700_000_100.0}
    fake.satellites[KITCHEN_ID]["playing"] = playing
    fake.satellites[LOUNGE_ID]["playing"] = playing
    fake.push(
        {
            "type": "media",
            "satellite": LOUNGE_ID,
            "source": KITCHEN_ID,
            "id": "ab" * 16,
            "announce": False,
            "state": "playing",
            "reason": None,
            "played_s": None,
        }
    )
    await until(hass, lambda: _state(hass, "media_player.kitchen") == "playing")
    await until(hass, lambda: _state(hass, "media_player.lounge") == "playing")
    assert fake.calls("GET", f"/satellites/{KITCHEN_ID}")
    assert fake.calls("GET", f"/satellites/{LOUNGE_ID}")


async def test_reconnect_reads_what_was_missed(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The hub restarts: entities go unavailable, and the reconnect reads
    GET /satellites again, so a change made meanwhile is not lost."""
    reads = len(fake.calls("GET", "/satellites"))
    fake.events_status = 503
    fake.drop_streams()
    await until(
        hass, lambda: _state(hass, "switch.kitchen_microphone") == "unavailable"
    )
    assert _state(hass, "binary_sensor.kitchen_online") == "unavailable"

    fake.satellites[KITCHEN_ID]["config"]["brightness"] = 35
    fake.events_status = None
    await fake.wait_streams(2)
    await until(hass, lambda: _state(hass, "number.kitchen_ring_brightness") == "35.0")
    assert len(fake.calls("GET", "/satellites")) > reads
    assert _state(hass, "switch.kitchen_microphone") == "on"


@pytest.fixture
def routine_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every clean end of the stream is taken for the gateway's 15-minute
    limit, not only one after STREAM_ROUTINE_AFTER."""
    monkeypatch.setattr("custom_components.calliope.coordinator.STREAM_ROUTINE_AFTER", 0)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.calliope")
    ]


@pytest.mark.usefixtures("routine_at_once")
async def test_the_gateways_15_minute_end_of_the_stream_leaves_every_entity_available(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gateway ends every event stream after 15 minutes so the key is
    checked again. One new stream opens at once and what changed meanwhile is
    read, while no entity goes unavailable and nothing is logged as lost."""
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    caplog.clear()
    reads = len(fake.calls("GET", "/satellites"))
    fake.satellites[KITCHEN_ID]["config"]["brightness"] = 35
    fake.drop_streams()
    await fake.wait_streams(2)
    await until(hass, lambda: _state(hass, "number.kitchen_ring_brightness") == "35.0")
    assert len(fake.calls("GET", "/satellites")) > reads
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert fake.stream_count == 2
    assert _unavailable(changes) == []
    assert loaded.runtime_data.coordinator.connected
    assert _warnings(caplog) == []


@pytest.mark.usefixtures("routine_at_once")
async def test_after_the_15_minute_end_entities_go_unavailable_only_if_the_stream_cannot_reopen(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new stream refused (the hub went down meanwhile) is an outage: the
    entities go unavailable, it is logged once, and they come back with the
    stream."""
    caplog.clear()
    fake.events_status = 503
    fake.drop_streams()
    await until(hass, lambda: _state(hass, "switch.kitchen_microphone") == "unavailable")
    [lost] = _warnings(caplog)
    assert lost.startswith("Lost the Calliope event stream")
    fake.events_status = None
    await until(hass, lambda: _state(hass, "switch.kitchen_microphone") == "on")


async def test_a_stream_that_ends_soon_after_it_opened_is_taken_for_an_outage(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reopened at once, a stream that a proxy closes as soon as it opens
    would be asked for again in a tight loop. One that ends before
    STREAM_ROUTINE_AFTER is retried with backoff, and logged as lost."""
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    caplog.clear()
    fake.drop_streams()
    await fake.wait_streams(2)
    await until(hass, lambda: _state(hass, "switch.kitchen_microphone") == "on")
    assert "switch.kitchen_microphone" in _unavailable(changes)
    [lost] = _warnings(caplog)
    assert lost.startswith("Lost the Calliope event stream")


async def test_adopted_later_and_forgotten(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A satellite adopted while running gets its entities; one forgotten
    loses its device."""
    fake.satellites[BEDROOM_ID] = korvo(BEDROOM_ID, "bedroom")
    fake.push(
        {
            "type": "online",
            "satellite": BEDROOM_ID,
            "name": "bedroom",
            "firmware": "0.4.2",
        }
    )
    await until(hass, lambda: hass.states.get("switch.bedroom_microphone") is not None)
    assert _state(hass, "binary_sensor.bedroom_online") == "on"

    devices = dr.async_get(hass)
    del fake.satellites[BEDROOM_ID]
    fake.push({"type": "pending", "satellite": BEDROOM_ID, "address": "192.0.2.51"})
    await until(
        hass,
        lambda: devices.async_get_device(identifiers={(DOMAIN, BEDROOM_ID)}) is None,
    )
    assert devices.async_get_device(identifiers={(DOMAIN, KITCHEN_ID)}) is not None


async def test_forgotten_and_adopted_again_gets_its_device_back(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Each platform remembered every satellite it had added, forgotten or
    not, so a board forgotten and adopted again had no device and no
    entities until a reload."""
    fake.satellites[BEDROOM_ID] = korvo(BEDROOM_ID, "bedroom")
    fake.push({"type": "online", "satellite": BEDROOM_ID, "name": "bedroom"})
    await until(hass, lambda: hass.states.get("switch.bedroom_microphone") is not None)

    devices = dr.async_get(hass)
    del fake.satellites[BEDROOM_ID]
    fake.push({"type": "pending", "satellite": BEDROOM_ID, "address": "192.0.2.51"})
    await until(
        hass,
        lambda: devices.async_get_device(identifiers={(DOMAIN, BEDROOM_ID)}) is None,
    )
    fake.satellites[BEDROOM_ID] = korvo(BEDROOM_ID, "bedroom")
    fake.push({"type": "online", "satellite": BEDROOM_ID, "name": "bedroom"})
    await until(
        hass,
        lambda: (
            devices.async_get_device(identifiers={(DOMAIN, BEDROOM_ID)}) is not None
        ),
    )
    await until(hass, lambda: hass.states.get("switch.bedroom_microphone") is not None)
    assert _state(hass, "binary_sensor.bedroom_online") == "on"


async def test_last_heard_survives_a_restart(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The last command is restored when the entry loads again."""
    fake.push(
        {
            "type": "routed",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "transcript": "lights off",
            "reply_text": None,
        }
    )
    await until(
        hass, lambda: _state(hass, "sensor.kitchen_last_command") == "lights off"
    )
    await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    await until(hass, lambda: loaded.runtime_data.coordinator.connected)
    assert _state(hass, "sensor.kitchen_last_command") == "lights off"
    assert (
        hass.states.get("sensor.kitchen_last_command").attributes["wake_word"]
        == "hey_jarvis"
    )


async def test_a_malformed_event_does_not_end_the_stream(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Logged and skipped; the next event is handled on the same stream."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    fake.push(
        {"type": "status", "satellite": KITCHEN_ID, "status": ["not", "a", "dict"]}
    )
    fake.push(
        {"type": "wake", "satellite": KITCHEN_ID, "wake_word": "hey_jarvis", "score": 1}
    )
    await until(hass, lambda: bus)
    assert fake.stream_count == 1
