"""Whether each satellite is connected to the hub."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_per_satellite

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One connectivity sensor per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[CalliopeOnline]:
        return [CalliopeOnline(coordinator, sid)]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeOnline(CalliopeSatelliteEntity, BinarySensorEntity):
    """On while the satellite holds its socket to the hub."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"
    available_offline = True

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "online"."""
        super().__init__(coordinator, sid, "online")

    @property
    def is_on(self) -> bool:
        """The hub's word for it."""
        return bool((self.satellite or {}).get("online"))
