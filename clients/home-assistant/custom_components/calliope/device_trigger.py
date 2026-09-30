"""Device triggers: what the automation editor offers for a satellite.

Each trigger listens for calliope_event with the satellite's device_id and
the matching "kind" (and word or button), so a trigger fires exactly when the
event entity does. The event is in the trigger data:
{{ trigger.event.data.transcript }}, {{ trigger.event.data.wake_word }}.

Only what the satellite has is offered: words, commands and conversations
with a microphone, and the buttons its caps list. A satellite whose caps are
not known yet gets none, rather than another model's.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    EVENT_CALLIOPE,
    KIND_BUTTON_PRESS,
    KIND_BUTTON_RELEASE,
    KIND_COMMAND,
    KIND_CONVERSATION_ENDED,
    KIND_CONVERSATION_STARTED,
    KIND_TRIGGER_WORD,
    KIND_WAKE_WORD,
    TRIGGER_TYPES,
)
from .capabilities import buttons_of, caps_of, has
from .coordinator import CalliopeCoordinator, satellite_id_of

CONF_SUBTYPE = "subtype"

WORD_TYPES = (KIND_WAKE_WORD, KIND_TRIGGER_WORD)
BUTTON_TYPES = (KIND_BUTTON_PRESS, KIND_BUTTON_RELEASE)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        # A word or a button. Not checked against the hub's current list:
        # words are reassigned at any time, and an automation for a word that
        # comes back later should still work.
        vol.Optional(CONF_SUBTYPE): vol.All(str, vol.Length(min=1, max=64)),
    }
)


def _find(
    hass: HomeAssistant, device_id: str
) -> tuple[CalliopeCoordinator, str] | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None or (sid := satellite_id_of(device)) is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and hasattr(entry, "runtime_data")
        ):
            return entry.runtime_data.coordinator, sid
    return None


def _word_kind(coordinator: CalliopeCoordinator, word: str) -> str:
    """A trigger word (mode "trigger") fires trigger_word; a command or
    conversation word, or one whose mode the hub does not say, wake_word."""
    if coordinator.word_mode(word) == "trigger":
        return KIND_TRIGGER_WORD
    return KIND_WAKE_WORD


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """The words assigned to the satellite, its buttons, commands and
    conversations."""
    found = _find(hass, device_id)
    if found is None:
        return []
    coordinator, sid = found
    sat = (coordinator.data or {}).get(sid) or {}
    caps = caps_of(sat)
    base = {CONF_PLATFORM: "device", CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id}
    triggers: list[dict[str, Any]] = []
    if has(caps, "mic"):
        for word in sat.get("wake_words") or []:
            kind = _word_kind(coordinator, word)
            triggers.append({**base, CONF_TYPE: kind, CONF_SUBTYPE: word})
        for kind in (KIND_COMMAND, KIND_CONVERSATION_STARTED, KIND_CONVERSATION_ENDED):
            triggers.append({**base, CONF_TYPE: kind})
    for button in buttons_of(caps):
        for kind in BUTTON_TYPES:
            triggers.append({**base, CONF_TYPE: kind, CONF_SUBTYPE: button})
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the matching calliope_event."""
    kind = config[CONF_TYPE]
    event_data: dict[str, Any] = {"device_id": config[CONF_DEVICE_ID], "kind": kind}
    if (subtype := config.get(CONF_SUBTYPE)) is not None:
        if kind in WORD_TYPES:
            event_data["wake_word"] = subtype
        elif kind in BUTTON_TYPES:
            event_data["button"] = subtype
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_CALLIOPE,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
