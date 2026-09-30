"""Identify and restart a satellite.

Identify blinks the ring for five seconds, or, on a satellite without one,
plays its wake chime three times.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeError
from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Restart for every satellite; identify for one with a speaker or a
    ring."""

    def build(coordinator: CalliopeCoordinator, sid: str, key: str) -> CalliopeAction:
        if key == "restart":
            return CalliopeRestart(coordinator, sid, key)
        return CalliopeIdentify(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.BUTTON, build)


class CalliopeAction(CalliopeSatelliteEntity, ButtonEntity):
    """POST /satellites/{id}/{action}."""

    _attr_entity_category = EntityCategory.CONFIG
    action = ""

    async def async_press(self) -> None:
        """Ask the hub."""
        try:
            await self.coordinator.client.action(self.satellite_id, self.action)
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err


class CalliopeIdentify(CalliopeAction):
    """POST /satellites/{id}/identify: the ring blinks, or the Pi chimes."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    action = "identify"


class CalliopeRestart(CalliopeAction):
    """POST /satellites/{id}/reboot. Off by default: a restart ends a
    conversation and whatever plays."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_registry_enabled_default = False
    action = "reboot"
