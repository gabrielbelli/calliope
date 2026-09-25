"""Diagnostics: the entry, the gateway's health and the satellites, with the
key redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .coordinator import CalliopeConfigEntry

# "address" is the satellite's LAN address; "buttons" may hold a webhook URL.
TO_REDACT = {CONF_API_KEY, "token", "address", "buttons"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CalliopeConfigEntry
) -> dict[str, Any]:
    """What a bug report needs."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "health": runtime.health,
        "voices": len(runtime.voices),
        "event_stream_connected": coordinator.connected,
        "has_hub": coordinator.has_hub,
        "wake_words": coordinator.words,
        "satellites": async_redact_data(coordinator.data or {}, TO_REDACT),
    }
