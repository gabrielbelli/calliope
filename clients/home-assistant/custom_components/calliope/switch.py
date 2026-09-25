"""A satellite's microphone, speaker and lights, switched through the hub."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeError
from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_per_satellite

PARALLEL_UPDATES = 1

# (translation key, config field). The switches show the hub's config, not
# the satellite's status, as the Satellites page and the MQTT bridge do: the
# config changes the moment PATCH answers.
SWITCHES = (
    ("microphone", "mic_enabled"),
    ("speaker", "speaker_enabled"),
    ("lights", "lights_enabled"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Three switches per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[CalliopeSwitch]:
        return [CalliopeSwitch(coordinator, sid, key, field) for key, field in SWITCHES]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeSwitch(CalliopeSatelliteEntity, SwitchEntity):
    """One on/off setting of a satellite."""

    # The hub keeps the record while the satellite is away and sends it at
    # the next welcome, so a change is taken while it is offline.
    available_offline = True

    def __init__(
        self, coordinator: CalliopeCoordinator, sid: str, key: str, field: str
    ) -> None:
        """Keyed by what it switches."""
        super().__init__(coordinator, sid, key)
        self._attr_translation_key = key
        self._field = field

    @property
    def is_on(self) -> bool | None:
        """The hub's config; unknown until the satellite has reported it."""
        value = self.config.get(self._field)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """PATCH it on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """PATCH it off."""
        await self._set(False)

    async def _set(self, value: bool) -> None:
        try:
            sat = await self.coordinator.client.configure(
                self.satellite_id, **{self._field: value}
            )
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_satellite(sat)
