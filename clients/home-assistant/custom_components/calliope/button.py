"""Identify: blink a satellite's ring for five seconds."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeError
from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_per_satellite

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One identify button per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[CalliopeIdentify]:
        return [CalliopeIdentify(coordinator, sid)]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeIdentify(CalliopeSatelliteEntity, ButtonEntity):
    """POST /satellites/{id}/identify."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "identify"."""
        super().__init__(coordinator, sid, "identify")

    async def async_press(self) -> None:
        """Blink."""
        try:
            await self.coordinator.client.action(self.satellite_id, "identify")
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
