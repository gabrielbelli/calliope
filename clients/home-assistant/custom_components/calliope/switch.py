"""A satellite's speaker, microphone, lights, echo reference and AirPlay
receiver, switched through the hub."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1

# key (the translation key too): the setting it PATCHes, its entity category,
# and whether it is on by default. The echo reference is off by default: it
# is a Pi's USB card setting that needs a card with a loopback input, and the
# wrong choice is heard only in the transcripts.
SWITCHES: dict[str, tuple[str, EntityCategory | None, bool]] = {
    "speaker": ("speaker_enabled", None, True),
    "microphone": ("mic_enabled", None, True),
    "lights": ("lights_enabled", None, True),
    "echo_reference": ("echo_reference", EntityCategory.CONFIG, False),
    "airplay": ("airplay_enabled", EntityCategory.CONFIG, True),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The switches a satellite's caps want."""

    def build(coordinator: CalliopeCoordinator, sid: str, key: str) -> CalliopeSwitch:
        return CalliopeSwitch(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.SWITCH, build)


class CalliopeSwitch(CalliopeSatelliteEntity, SwitchEntity):
    """One on/off setting of a satellite."""

    # The hub keeps the record while the satellite is away and sends it at
    # the next welcome, so a change is taken while it is offline.
    available_offline = True

    def __init__(self, coordinator: CalliopeCoordinator, sid: str, key: str) -> None:
        """Keyed by what it switches."""
        super().__init__(coordinator, sid, key)
        self._field, self._attr_entity_category, enabled = SWITCHES[key]
        self._attr_translation_key = key
        self._attr_entity_registry_enabled_default = enabled

    @property
    def is_on(self) -> bool | None:
        """The hub's config, as the Satellites page shows it: it changes the
        moment PATCH answers. A setting the hub has never stored (a Pi's
        AirPlay, which starts on) shows what the satellite reports; unknown
        until it has."""
        value = self.config.get(self._field)
        if value is None:
            value = self.status.get(self._field)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """PATCH it on."""
        await self._patch(**{self._field: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """PATCH it off."""
        await self._patch(**{self._field: False})
