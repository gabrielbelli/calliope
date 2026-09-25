"""Actions on satellites: say, tone, push_to_talk.

Each takes a target (satellite devices, their entities, an area, a label),
and acts on every Calliope satellite the target resolves to.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)

from .api import CalliopeApiError, CalliopeClient, CalliopeError
from .const import (
    ATTR_FREQUENCY,
    ATTR_SECONDS,
    ATTR_TEXT,
    ATTR_VOICE,
    ATTR_WAKE_WORD,
    DOMAIN,
    SERVICE_PUSH_TO_TALK,
    SERVICE_SAY,
    SERVICE_TONE,
)
from .coordinator import satellite_id_of

SAY_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Required(ATTR_TEXT): vol.All(cv.string, vol.Length(min=1, max=2000)),
        vol.Optional(ATTR_VOICE): cv.string,
    }
)
TONE_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Optional(ATTR_FREQUENCY, default=440): vol.All(
            vol.Coerce(float), vol.Range(min=50, max=8000)
        ),
        vol.Optional(ATTR_SECONDS, default=1): vol.All(
            vol.Coerce(float), vol.Range(min=0.05, max=10)
        ),
    }
)
PUSH_TO_TALK_SCHEMA = vol.Schema(
    {
        **cv.TARGET_SERVICE_FIELDS,
        vol.Optional(ATTR_WAKE_WORD): vol.All(cv.string, vol.Length(min=1, max=64)),
    }
)


def _targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[CalliopeClient, str, str]]:
    """(client, satellite id, device name) for every Calliope satellite the
    call's target names, directly or through an entity, area or label."""
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    device_ids = set(selected.referenced_devices)
    ent_reg = er.async_get(hass)
    for entity_id in selected.referenced | selected.indirectly_referenced:
        if (entry := ent_reg.async_get(entity_id)) is not None and entry.device_id:
            device_ids.add(entry.device_id)
    dev_reg = dr.async_get(hass)
    found: list[tuple[CalliopeClient, str, str]] = []
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None or (sid := satellite_id_of(device)) is None:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if (
                entry is not None
                and entry.domain == DOMAIN
                and hasattr(entry, "runtime_data")
            ):
                found.append((entry.runtime_data.client, sid, device.name or sid))
                break
    if not found:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_satellites"
        )
    return found


async def _each(
    hass: HomeAssistant, call: ServiceCall, action: str, body: dict[str, Any]
) -> None:
    for client, sid, name in _targets(hass, call):
        try:
            await client.action(sid, action, body)
        except CalliopeApiError as err:
            if (
                action == "ptt"
                and err.status in (404, 405)
                and err.code != "satellite_not_found"
            ):
                # The gateway answers 404 for a route it does not route.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="push_to_talk_unsupported",
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="satellite_refused",
                translation_placeholders={"satellite": name, "error": err.message},
            ) from err
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="satellite_refused",
                translation_placeholders={"satellite": name, "error": str(err)},
            ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the three actions."""

    async def say(call: ServiceCall) -> None:
        body = {"text": call.data[ATTR_TEXT]}
        if voice := call.data.get(ATTR_VOICE):
            body["voice"] = voice
        await _each(hass, call, "say", body)

    async def tone(call: ServiceCall) -> None:
        await _each(
            hass,
            call,
            "tone",
            {
                "frequency": call.data[ATTR_FREQUENCY],
                "seconds": call.data[ATTR_SECONDS],
            },
        )

    async def push_to_talk(call: ServiceCall) -> None:
        body = {}
        if word := call.data.get(ATTR_WAKE_WORD):
            body["wake_word"] = word
        await _each(hass, call, "ptt", body)

    hass.services.async_register(DOMAIN, SERVICE_SAY, say, schema=SAY_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_TONE, tone, schema=TONE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_PUSH_TO_TALK, push_to_talk, schema=PUSH_TO_TALK_SCHEMA
    )
