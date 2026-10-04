"""Diagnostics: the entry, the gateway's health and the satellites, with
secrets, addresses and what someone is listening to redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .capabilities import wanted
from .coordinator import CalliopeConfigEntry, satellite_id_of

# Redacted wherever they appear, at any depth:
# - the key, a satellite's token, its LAN address, and the entry's URL,
#   title and unique id (the gateway's host);
# - the stack's backend URLs, an LLM destination's base_url and system
#   prompt, and the MQTT broker, from /health and the wake words;
# - AirPlay's phone and its addresses (client_ip and server_ip are
#   link-local addresses that carry MACs), and what it plays.
TO_REDACT = {
    CONF_API_KEY,
    "token",
    "address",
    "url",
    "base_url",
    "broker",
    "system",
    "unique_id",
    "title",
    "client",
    "client_ip",
    "server_ip",
    "dacp_id",
    "client_mac",
    "client_device_id",
    "client_info",
    "raw",
    "artist",
    "album",
    "track",
}
# A button mapped to a webhook keeps its kind; its URL may carry a secret.
WEBHOOK_MARK = "webhook:**REDACTED**"


def _without_webhooks(buttons: Any) -> Any:
    """config.buttons with each webhook's URL replaced: which button does
    what is the point of a bug report about buttons."""
    if not isinstance(buttons, dict):
        return buttons
    return {
        button: {
            edge: WEBHOOK_MARK
            if isinstance(action, str) and action.startswith("webhook:")
            else action
            for edge, action in edges.items()
        }
        if isinstance(edges, dict)
        else edges
        for button, edges in buttons.items()
    }


def _satellite(sat: dict[str, Any]) -> dict[str, Any]:
    out = async_redact_data(sat, TO_REDACT)
    config = out.get("config")
    if isinstance(config, dict) and "buttons" in config:
        config["buttons"] = _without_webhooks(config["buttons"])
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CalliopeConfigEntry
) -> dict[str, Any]:
    """What a bug report needs."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "health": async_redact_data(runtime.health, TO_REDACT),
        "voices": len(runtime.voices),
        "event_stream_connected": coordinator.connected,
        "has_hub": coordinator.has_hub,
        "wake_words": async_redact_data(coordinator.words, TO_REDACT),
        "satellites": {
            sid: _satellite(sat) for sid, sat in (coordinator.data or {}).items()
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: CalliopeConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """One satellite: the hub's record, the entities its caps want and the
    ones Home Assistant has."""
    coordinator = entry.runtime_data.coordinator
    sid = satellite_id_of(device)
    sat = (coordinator.data or {}).get(sid) if sid else None
    return {
        "event_stream_connected": coordinator.connected,
        "satellite": _satellite(sat) if sat is not None else None,
        "wanted": sorted(wanted(sat)) if sat is not None else [],
        "entities": sorted(
            e.entity_id
            for e in er.async_entries_for_device(
                er.async_get(hass), device.id, include_disabled_entities=True
            )
        ),
    }
