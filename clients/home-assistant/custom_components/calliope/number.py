"""A satellite's volume."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
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
    """One volume slider per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[CalliopeVolume]:
        return [CalliopeVolume(coordinator, sid)]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeVolume(CalliopeSatelliteEntity, NumberEntity):
    """PATCH volume, 0 to 100, as the hub takes it."""

    _attr_translation_key = "volume"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER
    available_offline = True

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "volume"."""
        super().__init__(coordinator, sid, "volume")

    @property
    def native_value(self) -> float | None:
        """The hub's config. A volume changed with the satellite's own VOL
        buttons is not written back to it, as on the Satellites page."""
        value = self.config.get("volume")
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """PATCH it."""
        try:
            sat = await self.coordinator.client.configure(
                self.satellite_id, volume=round(value)
            )
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_satellite(sat)
