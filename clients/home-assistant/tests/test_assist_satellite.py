"""The Assist satellite: announcements through the hub's voice lane, which
answers once they have played. No ffmpeg runs and nothing plays (conftest
`converted`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN

from .conftest import until
from .fake_calliope import KITCHEN_ID, LOUNGE_ID, FakeCalliope, pi

DOORBELL = "http://192.0.2.10/sounds/doorbell.mp3"


async def _announce(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        "assist_satellite",
        "announce",
        {"entity_id": entity_id, "media_id": DOORBELL, **data},
        blocking=True,
    )


async def test_announce_plays_the_chime_and_the_message_and_waits_until_played(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
) -> None:
    """One WAV, the chime then the message, in the Korvo's voice format; the
    action returns only when the hub says it has played, and the satellite
    shows it is responding meanwhile."""
    hass.config.internal_url = "http://192.0.2.10:8123"
    fake.media_hold.clear()
    announcing = asyncio.create_task(_announce(hass, "assist_satellite.kitchen"))
    # Not conftest.until: waiting for Home Assistant's tasks would wait for
    # the announcement, which the hub is holding.
    async with asyncio.timeout(5):
        while not fake.calls("POST", f"/satellites/{KITCHEN_ID}/media"):
            await asyncio.sleep(0.01)
    [sent] = fake.calls("POST", f"/satellites/{KITCHEN_ID}/media")
    assert sent["announce"] == "1"
    assert converted == [
        {
            "sources": [
                "http://192.0.2.10:8123/api/assist_satellite/static/preannounce.mp3",
                DOORBELL,
            ],
            "rate": 48000,
            "channels": 1,
            "satellite": "kitchen",
        }
    ]
    await asyncio.sleep(0.05)
    assert not announcing.done()
    assert hass.states.get("assist_satellite.kitchen").state == "responding"

    fake.media_hold.set()
    await announcing
    assert hass.states.get("assist_satellite.kitchen").state == "idle"

    await _announce(hass, "assist_satellite.kitchen", preannounce=False)
    assert converted[-1]["sources"] == [DOORBELL]


async def test_an_announcement_the_hub_refuses_is_an_error(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    converted: list[dict[str, Any]],
) -> None:
    """The hub's reason, with the satellite's name."""
    fake.media_refusal = (
        409,
        f"satellite {LOUNGE_ID} has its speaker turned off (speaker_enabled is false)",
        "speaker_disabled",
    )
    with pytest.raises(HomeAssistantError, match="speaker turned off") as err:
        await _announce(hass, "assist_satellite.lounge", preannounce=False)
    assert err.value.translation_key == "satellite_refused"
    assert err.value.translation_placeholders["satellite"] == "lounge"


async def test_only_satellites_with_a_microphone_and_a_speaker_are_assist_satellites(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A Pi without a microphone is a speaker, not an Assist satellite; one
    that has both announces."""
    fake.satellites[LOUNGE_ID] = pi(LOUNGE_ID, "lounge", mic=False)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()
    assert hass.states.get("assist_satellite.lounge") is None
    kitchen = hass.states.get("assist_satellite.kitchen")
    assert kitchen.attributes["supported_features"] == (
        AssistSatelliteEntityFeature.ANNOUNCE
    )
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_announcement_ffmpeg_cannot_make_says_why(
    hass: HomeAssistant,
    fake: FakeCalliope,
    loaded: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversion that fails halfway ends the upload; the person is told
    ffmpeg's reason, not that a connection broke."""

    async def broken(*args: Any, **kwargs: Any) -> AsyncIterator[bytes]:
        yield b"RIFF"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="play_failed",
            translation_placeholders={"satellite": "kitchen", "error": "no such file"},
        )

    monkeypatch.setattr("custom_components.calliope.media.wav_chunks", broken)
    with pytest.raises(HomeAssistantError) as err:
        await _announce(hass, "assist_satellite.kitchen", preannounce=False)
    assert err.value.translation_key == "play_failed"
