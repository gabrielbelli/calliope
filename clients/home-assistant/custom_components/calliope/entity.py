"""Entities of one satellite, and the Calliope service device."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator


def service_device_info(entry: CalliopeConfigEntry) -> DeviceInfo:
    """The device the STT and TTS entities belong to, and satellites hang
    off."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"entry_{entry.entry_id}")},
        name="Calliope",
        manufacturer="Calliope",
        model="Speech gateway",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=entry.runtime_data.client.url + "/ui",
    )


class CalliopeSatelliteEntity(CoordinatorEntity[CalliopeCoordinator]):
    """One entity of one adopted satellite."""

    _attr_has_entity_name = True
    # True for what still means something while the satellite is offline:
    # its connectivity, the settings the hub keeps for it, and what it last
    # heard. RSSI and identify need it connected.
    available_offline = False

    def __init__(
        self, coordinator: CalliopeCoordinator, satellite_id: str, key: str
    ) -> None:
        """Name the entity by its satellite and key."""
        super().__init__(coordinator)
        self.satellite_id = satellite_id
        self._attr_unique_id = f"{satellite_id}_{key}"
        sat = self.satellite or {}
        mac = format_mac(satellite_id) if len(satellite_id) == 12 else None
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, satellite_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name=sat.get("name") or f"satellite-{satellite_id[-4:]}",
            manufacturer="Espressif"
            if str(sat.get("model", "")).startswith("korvo")
            else None,
            model=sat.get("model"),
            sw_version=sat.get("firmware"),
            via_device=(DOMAIN, f"entry_{entry.entry_id}"),
        )

    @property
    def satellite(self) -> dict[str, Any] | None:
        """The satellite as the hub last described it."""
        return (self.coordinator.data or {}).get(self.satellite_id)

    @property
    def config(self) -> dict[str, Any]:
        """The hub's config for the satellite: what the switches show."""
        return (self.satellite or {}).get("config") or {}

    @property
    def available(self) -> bool:
        """Known only while the event stream is up; most entities also need
        the satellite online."""
        sat = self.satellite
        if sat is None or not self.coordinator.connected:
            return False
        if not self.coordinator.last_update_success:
            return False
        return self.available_offline or bool(sat.get("online"))


def add_per_satellite(
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[CalliopeCoordinator, str], list[Any]],
) -> None:
    """Add a platform's entities for every adopted satellite, now and each
    time one is adopted later."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [sid for sid in (coordinator.data or {}) if sid not in known]
        if not new:
            return
        known.update(new)
        async_add_entities([e for sid in new for e in build(coordinator, sid)])

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
