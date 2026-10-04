"""The name a satellite's AirPlay receiver shows on phones."""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """For a satellite with an AirPlay receiver."""

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeAirPlayName:
        return CalliopeAirPlayName(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.TEXT, build)


class CalliopeAirPlayName(CalliopeSatelliteEntity, TextEntity):
    """PATCH airplay_name: at most 64 printable characters, as the hub takes
    it. Empty means the satellite's own name."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "airplay_name"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 64
    _attr_pattern = r"^[^\x00-\x1f\x7f]*$"
    # The hub keeps it while the satellite is away.
    available_offline = True

    @property
    def native_value(self) -> str:
        """The hub's config; empty for the satellite's own name."""
        return str(self.config.get("airplay_name") or "")

    async def async_set_value(self, value: str) -> None:
        """PATCH it."""
        await self._patch(airplay_name=value)
