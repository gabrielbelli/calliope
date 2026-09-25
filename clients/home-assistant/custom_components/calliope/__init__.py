"""Calliope: satellites, triggers and speech for Assist, from a Calliope hub."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import CalliopeAuthError, CalliopeClient, CalliopeError
from .const import DOMAIN
from .coordinator import (
    CalliopeConfigEntry,
    CalliopeCoordinator,
    CalliopeRuntime,
    satellite_id_of,
)
from .entity import service_device_info
from .services import async_setup_services

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.STT,
    Platform.SWITCH,
    Platform.TTS,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, for every entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CalliopeConfigEntry) -> bool:
    """Connect to the gateway, read the satellites, open the event stream."""
    session = async_get_clientsession(
        hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)
    )
    client = CalliopeClient(session, entry.data[CONF_URL], entry.data.get(CONF_API_KEY))
    try:
        health = await client.health()
        await client.check_key()
        voices = list((await client.voices()).get("voices") or [])
    except CalliopeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CalliopeError as err:
        raise ConfigEntryNotReady(f"Calliope is not ready: {err}") from err

    coordinator = CalliopeCoordinator(hass, entry, client)
    entry.runtime_data = CalliopeRuntime(
        client=client, coordinator=coordinator, health=health, voices=voices
    )
    await coordinator.async_config_entry_first_refresh()

    # Before the platforms: every satellite names it as its via_device.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **service_device_info(entry)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CalliopeConfigEntry) -> bool:
    """Close the stream (a background task of the entry) and the platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CalliopeConfigEntry, device: dr.DeviceEntry
) -> bool:
    """A satellite's device may be deleted by hand once the hub has
    forgotten it; an adopted one would only come back."""
    sid = satellite_id_of(device)
    if sid is None:
        return False
    return sid not in (entry.runtime_data.coordinator.data or {})
