"""Setup, devices, and entity states from GET /satellites; the key, what it
lacks, and the entry migration that made it required."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.api import UNNAMED_SCOPE
from custom_components.calliope.const import DOMAIN

from .conftest import until
from .fake_calliope import KITCHEN_ID, LOUNGE_ID, PENDING_ID, FakeCalliope, new_key

SATELLITE_SCOPES = {"satellites:read", "satellites:control", "satellites:update"}


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"missing_scope_{entry.entry_id}")


def _reauths(hass: HomeAssistant) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == "reauth"
    ]


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
    assert kitchen.connections == {(dr.CONNECTION_NETWORK_MAC, "02:00:00:00:00:01")}
    entities = er.async_entries_for_device(er.async_get(hass), kitchen.id)
    assert sorted(e.entity_id for e in entities) == [
        "assist_satellite.kitchen",
        "binary_sensor.kitchen_online",
        "binary_sensor.kitchen_privacy_mute",
        "button.kitchen_identify",
        "event.kitchen_voice",
        "media_player.kitchen",
        "number.kitchen_microphone_gain",
        "number.kitchen_ring_brightness",
        "select.kitchen_plays_through",
        "sensor.kitchen_last_command",
        "sensor.kitchen_last_wake_word",
        "sensor.kitchen_wi_fi_signal",
        "switch.kitchen_lights",
        "switch.kitchen_microphone",
        "switch.kitchen_speaker",
        "update.kitchen_firmware",
    ]
    assert devices.async_get_device(identifiers={(DOMAIN, LOUNGE_ID)}) is not None
    assert hass.states.get("stt.calliope_parakeet") is not None
    assert hass.states.get("tts.calliope_kokoro") is not None


async def test_states_from_the_hub(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Switches and the media player's volume show the hub's config; RSSI
    the status."""
    assert hass.states.get("binary_sensor.kitchen_online").state == "on"
    assert hass.states.get("sensor.kitchen_wi_fi_signal").state == "-58"
    assert hass.states.get("switch.kitchen_microphone").state == "on"
    assert hass.states.get("switch.kitchen_speaker").state == "on"
    assert hass.states.get("switch.kitchen_lights").state == "off"
    kitchen = hass.states.get("media_player.kitchen")
    assert kitchen.state == "idle"
    assert kitchen.attributes["volume_level"] == 0.6
    assert hass.states.get("event.kitchen_voice").state == "unknown"


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
    """A 401 at setup asks for a new key. /health answered it as it answers
    anyone, without the backends, and that is not taken for a missing
    health:read: the key is not known at all."""
    fake.api_key = new_key()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert _issue(hass, entry) is None


async def test_a_key_revoked_while_running_asks_for_a_new_one(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The event stream reconnects with the old key, is refused with 401,
    and Home Assistant asks for a new key rather than retrying for ever."""
    fake.api_key = new_key()
    fake.drop_streams()
    await until(hass, lambda: _reauths(hass))
    assert not loaded.runtime_data.coordinator.connected


async def test_a_reread_refused_with_401_asks_for_a_new_key(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The stream still open, a hub-wide event rereads the satellites with a
    key revoked meanwhile: that 401 asks for a new key too."""
    fake.api_key = new_key()
    fake.push({"type": "wake_words"})
    await until(hass, lambda: _reauths(hass))


async def test_an_entry_from_before_keys_were_required_asks_for_one_at_once(
    hass: HomeAssistant, fake: FakeCalliope
) -> None:
    """A version 1 entry without a key is migrated to version 2 and asks for
    a key before anything is sent; with one, it loads."""
    old = MockConfigEntry(
        domain=DOMAIN,
        title="127.0.0.1",
        unique_id=fake.url.split("//", 1)[1],
        version=1,
        data={CONF_URL: fake.url, CONF_VERIFY_SSL: True},
    )
    old.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(old.entry_id)
    assert old.version == 2
    assert old.state is ConfigEntryState.SETUP_ERROR
    assert fake.hits == []
    [flow] = _reauths(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: fake.api_key}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert old.state is ConfigEntryState.LOADED
    assert old.data[CONF_API_KEY] == fake.api_key
    await hass.config_entries.async_unload(old.entry_id)
    await hass.async_block_till_done()


async def test_an_entry_from_a_newer_integration_is_not_loaded(
    hass: HomeAssistant, fake: FakeCalliope
) -> None:
    """Going back a release must not guess at a shape it cannot know."""
    newer = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_URL: fake.url, CONF_API_KEY: fake.api_key, CONF_VERIFY_SSL: True},
    )
    newer.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(newer.entry_id)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR
    assert fake.hits == []


async def test_a_refused_scope_raises_a_repair_issue_naming_it(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A 403 from any route names what the key lacks. A satellite control
    alone is a warning; the home-assistant glossary, refused too, widens the
    same issue to an error that names both."""
    fake.scopes.discard("satellites:control")
    with pytest.raises(HomeAssistantError, match="satellites:control"):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.kitchen_lights"},
            blocking=True,
        )
    issue = _issue(hass, loaded)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "missing_scope"
    assert issue.translation_placeholders == {
        "entry": loaded.title,
        "scopes": "satellites:control",
    }

    fake.scopes.discard("glossaries:ha")
    await loaded.runtime_data.vocabulary.async_refresh()
    issue = _issue(hass, loaded)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["scopes"] == (
        "glossaries:ha, glossaries:write:all, satellites:control"
    )


async def test_a_repair_issue_shows_only_well_formed_scopes_and_never_the_servers_text(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The Repairs entry is rendered as Markdown and tells the user what to
    do with their key: a challenge that names no scope with the gateway's
    grammar gives a fixed phrase, never the server's message or a link."""
    fake.scopes.discard("satellites:control")
    fake.hostile_refusal = (
        "[Renew your key](https://example.net/)",
        "[Paste your key here](https://example.net/) " * 20,
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.kitchen_lights"},
            blocking=True,
        )
    issue = _issue(hass, loaded)
    assert issue.translation_placeholders["scopes"] == UNNAMED_SCOPE
    # Not named, so not known to be a satellite scope: deny by default.
    assert issue.severity is ir.IssueSeverity.ERROR


async def test_the_repair_issue_goes_when_the_entry_is_set_up_with_a_fuller_key(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """Entering a new key reloads the entry, and what the old one lacked is
    asked again rather than remembered."""
    fake.scopes.discard("glossaries:ha")
    await loaded.runtime_data.vocabulary.async_refresh()
    assert _issue(hass, loaded) is not None
    fake.scopes.add("glossaries:ha")
    assert await hass.config_entries.async_reload(loaded.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, loaded) is None


async def test_a_key_that_cannot_reach_the_satellites_still_serves_speech(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """403 on GET /satellites is a stack without satellites to this key:
    speech-to-text and text-to-speech load, no satellite does, and the issue
    is a warning naming satellites:read."""
    fake.scopes -= SATELLITE_SCOPES
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("stt.calliope_parakeet") is not None
    assert hass.states.get("tts.calliope_kokoro") is not None
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={(DOMAIN, KITCHEN_ID)}) is None
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["scopes"] == "satellites:read"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_key_without_health_read_fails_setup_with_a_repair_issue_naming_it(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """/health answers such a key without the engines rather than with a 403.
    Loaded anyway, Assist would be offered a guessed engine and the stack
    polled for ever for engines it never lists; so setup fails as for any
    speech scope, and the issue names it."""
    fake.scopes.discard("health:read")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauths(hass) == []
    assert hass.states.async_entity_ids("stt") == []
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["scopes"] == "health:read"
    assert ("GET", "/satellites/events") not in fake.hits


async def test_a_key_without_a_speech_scope_fails_setup_until_it_is_replaced(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Retrying cannot give a key a scope, so setup fails rather than
    retries, without asking for a key the gateway accepts; the issue says
    which scope, and goes with the entry."""
    fake.scopes.discard("speech:speak")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauths(hass) == []
    issue = _issue(hass, entry)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["scopes"] == "speech:speak"
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
