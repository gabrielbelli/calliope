"""Whether each satellite is connected, muted by its own button, or short of
power."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Connectivity for every satellite; the rest as its caps say."""

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeSatelliteEntity:
        if key == "online":
            return CalliopeOnline(coordinator, sid, key)
        if key == "privacy_mute":
            return CalliopePrivacyMute(coordinator, sid, key)
        return CalliopeUnderVoltage(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.BINARY_SENSOR, build)


class CalliopeOnline(CalliopeSatelliteEntity, BinarySensorEntity):
    """On while the satellite holds its socket to the hub."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"
    available_offline = True

    @property
    def is_on(self) -> bool:
        """The hub's word for it."""
        return bool((self.satellite or {}).get("online"))


class CalliopePrivacyMute(CalliopeSatelliteEntity, BinarySensorEntity):
    """On while the board's mute button has cut its microphone. Only that
    button turns it off again, and push-to-talk is refused meanwhile; the
    Microphone switch is the hub's setting, which this does not change."""

    _attr_translation_key = "privacy_mute"

    @property
    def is_on(self) -> bool | None:
        """From the satellite's status."""
        muted = self.status.get("muted")
        return None if muted is None else bool(muted)


class CalliopeUnderVoltage(CalliopeSatelliteEntity, BinarySensorEntity):
    """On while the Pi's supply is too weak: it slows down, and USB audio
    may drop out."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "under_voltage"

    @property
    def is_on(self) -> bool | None:
        """From the satellite's status."""
        value = self.status.get("under_voltage")
        return None if value is None else bool(value)
