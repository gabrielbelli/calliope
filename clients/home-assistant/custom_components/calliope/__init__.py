"""Calliope: satellites, their speakers, triggers and speech for Assist, from a
Calliope hub."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from . import issues
from .api import (
    HEALTH_READ,
    CalliopeAuthError,
    CalliopeClient,
    CalliopeError,
    CalliopeScopeError,
    lacks_health_read,
)
from .const import DOMAIN
from .coordinator import (
    CalliopeConfigEntry,
    CalliopeCoordinator,
    CalliopeRuntime,
    satellite_id_of,
)
from .entity import service_device_info
from .services import async_setup_services
from .stt import stt_engines, stt_ready
from .vocabulary import Vocabulary

PLATFORMS = [
    Platform.ASSIST_SATELLITE,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.STT,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.TTS,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

# How often the stack is asked which speech-to-text engines it serves. A change
# (a model added to or taken off the stack) reloads the entry, so Assist's
# menu follows the stack without anyone touching Home Assistant.
ENGINE_CHECK = timedelta(minutes=5)
# How often while the stack is still loading its engines, as it is when Home
# Assistant and the stack restart together: then the menu fills in within
# half a minute of the stack being ready rather than up to ENGINE_CHECK later.
ENGINE_CHECK_LOADING = timedelta(seconds=30)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, for every entry."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 2: Calliope requires a key. An entry from version 1 is kept as
    it is, with or without one: setup refuses an entry without a key before
    it sends anything, and that starts reauth at once."""
    if entry.version > 2:
        # Written by a newer integration; this one cannot know its shape.
        return False
    if entry.version == 1:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CalliopeConfigEntry) -> bool:
    """Connect to the gateway, read the satellites, open the event stream."""
    # A new key, or the same key after its owner's role changed: what it
    # lacked before is found again rather than remembered.
    issues.clear(hass, entry)
    if not entry.data.get(CONF_API_KEY):
        raise ConfigEntryAuthFailed("Calliope requires an API key")
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)
    )
    report = issues.reporter(hass, entry)
    client = CalliopeClient(
        session, entry.data[CONF_URL], entry.data[CONF_API_KEY], on_missing_scope=report
    )
    try:
        health = await client.health()
        await client.check_key()
        if lacks_health_read(health):
            # The key is known, so this is the answer to a key without
            # health:read, which /health gives rather than a 403. Without the
            # engines, Assist would be offered a guess, and the stack polled
            # for ever for engines it never lists.
            refused = CalliopeScopeError(
                f"This credential lacks the scope it needs: {HEALTH_READ}.", (HEALTH_READ,)
            )
            report(refused)
            raise refused
        voices = list((await client.voices()).get("voices") or [])
    except CalliopeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CalliopeScopeError as err:
        # Retrying cannot give a key a scope: the repair issue says which
        # key to make instead.
        raise ConfigEntryError(str(err)) from err
    except CalliopeError as err:
        raise ConfigEntryNotReady(f"Calliope is not ready: {err}") from err

    coordinator = CalliopeCoordinator(hass, entry, client)
    entry.runtime_data = CalliopeRuntime(
        client=client,
        coordinator=coordinator,
        health=health,
        voices=voices,
        vocabulary=Vocabulary(hass, client),
    )
    await coordinator.async_config_entry_first_refresh()

    # Before the platforms: every satellite names it as its via_device.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **service_device_info(entry)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    for unsub in entry.runtime_data.vocabulary.async_start():
        entry.async_on_unload(unsub)
    entry.async_on_unload(_watch_engines(hass, entry, client))
    return True


@callback
def _watch_engines(
    hass: HomeAssistant, entry: CalliopeConfigEntry, client: CalliopeClient
):
    """Reload the entry when the stack's speech-to-text engines change. The
    list is only compared once the stack says it has finished loading them:
    one still starting lists none."""
    serving = [e["id"] for e in stt_engines(entry.runtime_data.health)]
    loading: list[CALLBACK_TYPE] = []

    async def _check(_now: datetime) -> None:
        try:
            health = await client.health()
        except CalliopeError:
            return
        if not stt_ready(health):
            return
        while loading:
            loading.pop()()
        now_serving = [e["id"] for e in stt_engines(health)]
        if now_serving != serving:
            _LOGGER.info(
                "Calliope's speech-to-text engines changed from %s to %s; reloading",
                ", ".join(serving),
                ", ".join(now_serving),
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)

    regular = async_track_time_interval(hass, _check, ENGINE_CHECK)
    if not stt_ready(entry.runtime_data.health):
        loading.append(async_track_time_interval(hass, _check, ENGINE_CHECK_LOADING))

    @callback
    def _stop() -> None:
        regular()
        while loading:
            loading.pop()()

    return _stop


async def async_unload_entry(hass: HomeAssistant, entry: CalliopeConfigEntry) -> bool:
    """Close the stream (a background task of the entry), the platforms, and
    the issue about what its key lacks."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        issues.clear(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """An entry whose setup failed for a missing scope was never unloaded,
    and keeps its issue until it is set up again or removed."""
    issues.clear(hass, entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CalliopeConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A satellite's device may be deleted by hand once the hub has
    forgotten it; an adopted one would only come back."""
    sid = satellite_id_of(device)
    if sid is None:
        return False
    return sid not in (entry.runtime_data.coordinator.data or {})
