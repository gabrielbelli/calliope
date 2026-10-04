"""Entities of one satellite, and the Calliope service device."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceEntryType,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import CalliopeError
from .capabilities import caps_of, known, wanted
from .const import DOMAIN
from .coordinator import (
    CalliopeConfigEntry,
    CalliopeCoordinator,
    device_name,
    manufacturer_of,
    signal_removed,
)


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


def satellite_device_info(coordinator: CalliopeCoordinator, sid: str) -> DeviceInfo:
    """A satellite's device as the hub describes it. The coordinator keeps
    it current afterwards (a rename, a new firmware)."""
    sat = (coordinator.data or {}).get(sid) or {}
    mac = format_mac(sid) if len(sid) == 12 else None
    model = sat.get("model")
    return DeviceInfo(
        identifiers={(DOMAIN, sid)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        name=device_name(sid, sat),
        manufacturer=manufacturer_of(model),
        model=model,
        sw_version=sat.get("firmware"),
        configuration_url=coordinator.client.url + "/ui",
        via_device=(DOMAIN, f"entry_{coordinator.config_entry.entry_id}"),
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
        self._attr_device_info = satellite_device_info(coordinator, satellite_id)
        self._mark: tuple[Any, ...] | None = None

    @property
    def satellite(self) -> dict[str, Any] | None:
        """The satellite as the hub last described it."""
        return (self.coordinator.data or {}).get(self.satellite_id)

    @property
    def config(self) -> dict[str, Any]:
        """The hub's config for the satellite: what the switches show."""
        return (self.satellite or {}).get("config") or {}

    @property
    def status(self) -> dict[str, Any]:
        """What the satellite last reported; {} while it is offline."""
        status = (self.satellite or {}).get("status")
        return status if isinstance(status, dict) else {}

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

    def _marks(self) -> tuple[Any, ...]:
        """What this entity's state depends on. The satellite's revision
        moves with every change to it; an entity that also shows another
        satellite (the one it plays through) adds that one's."""
        return (
            self.coordinator.revision.get(self.satellite_id),
            self.coordinator.connected,
            self.coordinator.last_update_success,
        )

    def _on_update(self) -> None:
        """Recompute what the entity keeps rather than reads (its features,
        state and attributes), before its state is written."""

    async def async_added_to_hass(self) -> None:
        """Compute the first state, then follow the coordinator."""
        self._mark = self._marks()
        self._on_update()
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write the state only when this satellite (or the stream) changed:
        another satellite's status every 10 s is not this entity's news."""
        mark = self._marks()
        if mark == self._mark:
            return
        self._mark = mark
        self._on_update()
        self.async_write_ha_state()

    async def _patch(self, **changes: Any) -> None:
        """PATCH the satellite's settings; the answer is its new record."""
        try:
            sat = await self.coordinator.client.configure(self.satellite_id, **changes)
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_satellite(sat)


def add_for_caps(
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: Platform,
    factory: Callable[[CalliopeCoordinator, str, str], Entity],
) -> None:
    """Add this platform's entities for every adopted satellite, as its caps
    want them (capabilities.wanted), now and whenever a satellite is adopted
    or its caps change. `factory` builds one entity from (coordinator,
    satellite id, key).

    A key the caps no longer want is forgotten here: the coordinator has
    removed its registry entry, and Home Assistant the entity with it, so it
    is added afresh if the cap comes back. While a satellite's caps are
    unknown, only what every satellite has is added and nothing is forgotten.
    A satellite whose device was removed (forgotten on the hub) is forgotten
    too, so adopted again it gets its entities back. Not on any refresh that
    lacks it: a hub answering 404 or 503 lists none and removes no device,
    and its entities are still there."""
    coordinator = entry.runtime_data.coordinator
    created: dict[str, set[str]] = {}
    seen: dict[str, str] = {}

    @callback
    def add_new() -> None:
        new: list[Entity] = []
        for sid, sat in (coordinator.data or {}).items():
            fingerprint = json.dumps(caps_of(sat), sort_keys=True, default=str)
            if sid in created and seen.get(sid) == fingerprint:
                continue
            seen[sid] = fingerprint
            have = created.setdefault(sid, set())
            want = [k for k, p in wanted(sat).items() if p is platform]
            if known(sat):
                have.intersection_update(want)
            for key in want:
                if key not in have:
                    have.add(key)
                    new.append(factory(coordinator, sid, key))
        if new:
            async_add_entities(new)

    @callback
    def removed(sids: set[str]) -> None:
        for sid in sids:
            created.pop(sid, None)
            seen.pop(sid, None)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))
    entry.async_on_unload(
        async_dispatcher_connect(
            coordinator.hass, signal_removed(entry.entry_id), removed
        )
    )
