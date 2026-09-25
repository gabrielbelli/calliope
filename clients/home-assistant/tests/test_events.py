"""The event stream: entity states, the bus event, reconnecting."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.calliope.const import DOMAIN, EVENT_CALLIOPE

from .conftest import until
from .fake_calliope import BEDROOM_ID, KITCHEN_ID, FakeCalliope, satellite


def _state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


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


async def test_buttons_and_conversations(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Every happening type reaches the event entity."""
    bus = async_capture_events(hass, EVENT_CALLIOPE)
    seen: list[str] = []
    for n, event in enumerate(
        (
            {"type": "button", "button": "play", "action": "press", "held_ms": 0},
            {"type": "button", "button": "play", "action": "release", "held_ms": 240},
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
    assert seen == [
        "button_press",
        "button_release",
        "conversation_started",
        "command",
        "conversation_ended",
    ]
    assert [e.data["kind"] for e in bus] == seen
    assert hass.states.get("event.kitchen_voice").attributes["turns"] == 2
    assert _state(hass, "sensor.kitchen_last_command") == "and tomorrow?"


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
    """A PATCH from the Satellites page shows up through the satellite's
    next status: the settings it reports changed, so the record is read."""
    fake.satellites[KITCHEN_ID]["config"]["speaker_enabled"] = False
    fake.push(
        {
            "type": "status",
            "satellite": KITCHEN_ID,
            "status": {
                "rssi": -58,
                "volume": 60,
                "mic_enabled": True,
                "speaker_enabled": False,
                "lights_enabled": False,
            },
        }
    )
    await until(hass, lambda: _state(hass, "switch.kitchen_speaker") == "off")
    assert fake.calls("GET", f"/satellites/{KITCHEN_ID}")


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

    fake.satellites[KITCHEN_ID]["config"]["volume"] = 35
    fake.events_status = None
    await fake.wait_streams(2)
    await until(hass, lambda: _state(hass, "number.kitchen_volume") == "35.0")
    assert len(fake.calls("GET", "/satellites")) > reads
    assert _state(hass, "switch.kitchen_microphone") == "on"


async def test_adopted_later_and_forgotten(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A satellite adopted while running gets its entities; one forgotten
    loses its device."""
    fake.satellites[BEDROOM_ID] = satellite(BEDROOM_ID, "bedroom")
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
    fake.push({"type": "pending", "satellite": BEDROOM_ID, "address": "192.168.1.51"})
    await until(
        hass,
        lambda: devices.async_get_device(identifiers={(DOMAIN, BEDROOM_ID)}) is None,
    )
    assert devices.async_get_device(identifiers={(DOMAIN, KITCHEN_ID)}) is not None


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
