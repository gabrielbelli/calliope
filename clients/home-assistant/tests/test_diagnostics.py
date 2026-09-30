"""Diagnostics: what a bug report needs, without keys, addresses, prompts,
webhook URLs or what someone is listening to."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import DOMAIN
from custom_components.calliope.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

from .conftest import until
from .fake_calliope import KITCHEN_ID, LOUNGE_ID, FakeCalliope, airplay_playing

REDACTED = "**REDACTED**"


async def test_diagnostics_redact_addresses_urls_titles_and_webhooks_but_keep_caps_buttons(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The key, the gateway's address, the stack's URLs, an LLM's prompt,
    a satellite's address, the phone and its track are redacted; a webhook
    keeps its kind and loses its URL; the buttons a board has are kept."""
    fake.api_key = "sk-secret"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_API_KEY: "sk-secret"}
    )
    fake.words.append(
        {
            "name": "hey_grok",
            "mode": "conversation",
            "satellites": ["*"],
            "destination": {
                "kind": "llm",
                "base_url": "https://llm.example.com/v1",
                "system": "You are a kitchen assistant.",
            },
        }
    )
    fake.satellites[LOUNGE_ID]["status"]["airplay"] = airplay_playing()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in (
        "sk-secret",
        fake.url.split("//", 1)[1],
        "http://stt-stack:8000",
        "llm.example.com",
        "kitchen assistant",
        "192.0.2.50",
        "hooks.example.com",
        "Miles Davis",
        "So What",
        "Someone's iPhone",
        "fe80::1",
    ):
        assert secret not in text, secret
    assert diag["entry"]["data"][CONF_API_KEY] == REDACTED
    assert diag["entry"]["title"] == REDACTED
    assert diag["health"]["backends"]["stt"]["url"] == REDACTED
    assert diag["wake_words"][-1]["destination"]["system"] == REDACTED
    kitchen = diag["satellites"][KITCHEN_ID]
    assert kitchen["address"] == REDACTED
    assert kitchen["caps"]["buttons"] == fake.satellites[KITCHEN_ID]["caps"]["buttons"]
    assert kitchen["config"]["buttons"] == {
        "play": {"press": "ptt"},
        "set": {"press": "stop"},
        "rec": {"press": "webhook:**REDACTED**"},
    }
    airplay = diag["satellites"][LOUNGE_ID]["status"]["airplay"]
    assert airplay["player"] == "Playing"
    assert airplay["client_info"] == REDACTED
    assert airplay["raw"] == REDACTED
    assert diag["event_stream_connected"] is True
    assert diag["voices"] == len(fake.voices)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_device_diagnostics(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """One satellite: its record, what its caps want and what Home Assistant
    made of it."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, LOUNGE_ID)})
    diag = await async_get_device_diagnostics(hass, loaded, device)
    assert diag["event_stream_connected"] is True
    assert diag["satellite"]["id"] == LOUNGE_ID
    assert diag["satellite"]["address"] == REDACTED
    assert "airplay_name" in diag["wanted"]
    assert "lights" not in diag["wanted"]
    assert "text.lounge_airplay_name" in diag["entities"]
    assert "sensor.lounge_uptime" in diag["entities"]  # disabled, but there
