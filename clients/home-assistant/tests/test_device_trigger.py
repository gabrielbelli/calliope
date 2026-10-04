"""Device triggers: what the automation editor lists, and that they fire."""

from __future__ import annotations

from typing import Any

from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.calliope.const import DOMAIN

from .conftest import until
from .fake_calliope import BEDROOM_ID, KITCHEN_ID, LOUNGE_ID, FakeCalliope, korvo, pi

OFFICE_ID = "0a1b2c3d4e60"


def _kitchen(hass: HomeAssistant) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, KITCHEN_ID)})


def _listed(triggers: list[dict[str, Any]]) -> set[tuple[str, str | None]]:
    return {(t["type"], t.get("subtype")) for t in triggers if t["domain"] == DOMAIN}


async def _offered(hass: HomeAssistant, sid: str) -> set[tuple[str, str | None]]:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, sid)})
    return _listed(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device.id
        )
    )


WORDS_AND_COMMANDS = {
    ("wake_word", "hey_jarvis"),
    ("trigger_word", "lumos"),
    ("command", None),
    ("conversation_started", None),
    ("conversation_ended", None),
}


async def test_triggers_are_the_satellites_own(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The Korvo: its words (hey_jarvis a command word, lumos a trigger
    word), commands, conversations and its seven buttons. A Pi with a
    microphone: words and commands, no buttons. A Pi without one, and a
    satellite whose caps are not known: nothing, rather than a guess."""
    fake.satellites[BEDROOM_ID] = pi(BEDROOM_ID, "bedroom", mic=False)
    fake.satellites[OFFICE_ID] = korvo(OFFICE_ID, "office", online=False)
    fake.satellites[OFFICE_ID]["caps"] = {}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    buttons = {"play", "set", "mode", "rec", "vol_up", "vol_down", "key1"}
    assert await _offered(hass, KITCHEN_ID) == (
        WORDS_AND_COMMANDS
        | {("button_press", b) for b in buttons}
        | {("button_release", b) for b in buttons}
    )
    assert await _offered(hass, LOUNGE_ID) == WORDS_AND_COMMANDS
    assert await _offered(hass, BEDROOM_ID) == set()
    assert await _offered(hass, OFFICE_ID) == set()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _automation(
    hass: HomeAssistant, trigger: dict[str, Any]
) -> list[ServiceCall]:
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": _kitchen(hass).id,
                        **trigger,
                    },
                    "action": {
                        "service": "test.automation",
                        "data_template": {
                            "kind": "{{ trigger.event.data.kind }}",
                            "word": "{{ trigger.event.data.wake_word }}",
                            "transcript": "{{ trigger.event.data.transcript }}",
                            "button": "{{ trigger.event.data.button }}",
                        },
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()
    return calls


async def test_wake_word_trigger(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Fires for its word on its satellite, and for nothing else."""
    calls = await _automation(hass, {"type": "wake_word", "subtype": "hey_jarvis"})
    fake.push(
        {"type": "wake", "satellite": KITCHEN_ID, "wake_word": "alexa", "score": 0.9}
    )
    fake.push(
        {
            "type": "wake",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "score": 0.9,
        }
    )
    await until(hass, lambda: calls)
    await hass.async_block_till_done()
    assert [c.data["word"] for c in calls] == ["hey_jarvis"]


async def test_trigger_word_trigger(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Trigger word lumos heard on the kitchen."""
    calls = await _automation(hass, {"type": "trigger_word", "subtype": "lumos"})
    fake.push(
        {"type": "wake", "satellite": KITCHEN_ID, "wake_word": "lumos", "score": 0.9}
    )
    fake.push(
        {
            "type": "triggered",
            "satellite": KITCHEN_ID,
            "satellite_name": "kitchen",
            "wake_word": "lumos",
            "score": 0.88,
        }
    )
    await until(hass, lambda: calls)
    await hass.async_block_till_done()
    assert [(c.data["kind"], c.data["word"]) for c in calls] == [
        ("trigger_word", "lumos")
    ]


async def test_command_trigger_carries_the_transcript(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The transcript is trigger data; a command with none does not fire."""
    calls = await _automation(hass, {"type": "command"})
    fake.push(
        {
            "type": "routed",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "error": "nothing was said after the wake word",
            "transcript": None,
        }
    )
    fake.push(
        {
            "type": "routed",
            "satellite": KITCHEN_ID,
            "wake_word": "hey_jarvis",
            "transcript": "open the blinds",
            "reply_text": None,
        }
    )
    await until(hass, lambda: calls)
    await hass.async_block_till_done()
    assert [c.data["transcript"] for c in calls] == ["open the blinds"]


async def test_button_trigger(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """PLAY pressed, not released, not SET."""
    calls = await _automation(hass, {"type": "button_press", "subtype": "play"})
    fake.push(
        {"type": "button", "satellite": KITCHEN_ID, "button": "set", "action": "press"}
    )
    fake.push(
        {
            "type": "button",
            "satellite": KITCHEN_ID,
            "button": "play",
            "action": "release",
            "held_ms": 300,
        }
    )
    fake.push(
        {
            "type": "button",
            "satellite": KITCHEN_ID,
            "button": "play",
            "action": "press",
            "held_ms": 0,
        }
    )
    await until(hass, lambda: calls)
    await hass.async_block_till_done()
    assert [c.data["button"] for c in calls] == ["play"]
