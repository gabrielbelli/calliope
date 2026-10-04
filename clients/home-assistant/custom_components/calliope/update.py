"""A satellite's firmware, and the update the hub would send it.

The hub works out the update (GET /satellites/{id} "update"): the newest
image for the satellite's model that it would actually send, signed where the
satellite demands a signature, and newer than what it runs. Only the hub
knows the satellite's signing key and the firmware key, and only it orders
the versions as the Satellites page does.
"""

from __future__ import annotations

import contextlib
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CalliopeError
from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, device_name
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1

# ota.state while an update is on its way: asked for, being sent, written
# and restarting. "verified" and "failed" are the end.
UNDERWAY = ("requested", "started", "progress", "rebooting")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One firmware entity per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str, key: str) -> CalliopeFirmware:
        return CalliopeFirmware(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.UPDATE, build)


class CalliopeFirmware(CalliopeSatelliteEntity, UpdateEntity):
    """What the satellite runs, what the hub would send, and the update's
    progress from the hub's ota events."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    @property
    def _update(self) -> dict[str, Any] | None:
        update = (self.satellite or {}).get("update")
        return update if isinstance(update, dict) and update.get("sha256") else None

    @property
    def _ota(self) -> dict[str, Any]:
        ota = (self.satellite or {}).get("ota")
        return ota if isinstance(ota, dict) else {}

    @property
    def installed_version(self) -> str | None:
        """The firmware the satellite reported at its last hello."""
        return (self.satellite or {}).get("firmware")

    @property
    def latest_version(self) -> str | None:
        """The version on its way while an update installs, else the hub's
        update, else what is installed: a hub from before the update field
        offers none. The hub offers no update of the version it is
        installing, so without the first the entity would say "up to date"
        with the old version all through the install."""
        if self.in_progress and self._ota.get("version"):
            return str(self._ota["version"])
        if (update := self._update) is not None and update.get("version"):
            return str(update["version"])
        return self.installed_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """The hub offers only a newer image, so any other version is newer.
        AwesomeVersion cannot order git-describe versions (v0.1.2-193-g…)."""
        return latest_version != installed_version

    @property
    def in_progress(self) -> bool:
        """From the hub's ota record and events. A property, not an
        attribute: Home Assistant resets the attribute when async_install
        returns, which is when the update has only just started."""
        return self._ota.get("state") in UNDERWAY

    @property
    def update_percentage(self) -> int | float | None:
        """While the image is being sent."""
        if self._ota.get("state") in ("started", "progress"):
            pct = self._ota.get("pct")
            return pct if isinstance(pct, (int, float)) else None
        return None

    @property
    def title(self) -> str | None:
        """The board, as the firmware names it."""
        return (self.satellite or {}).get("model")

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """POST /satellites/ota with the image the hub offers. The hub says
        at once why it will not (offline, another update underway, a
        signature the satellite would refuse)."""
        name = device_name(self.satellite_id, self.satellite or {})
        if (update := self._update) is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_update",
                translation_placeholders={"satellite": name},
            )
        client = self.coordinator.client
        try:
            result = await client.start_ota(self.satellite_id, update["sha256"])
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hub_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        skipped = (result or {}).get("skipped") or {}
        if self.satellite_id in skipped:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="update_skipped",
                translation_placeholders={"reason": str(skipped[self.satellite_id])},
            )
        # The hub's record now says "requested": show the update underway
        # before the satellite's first ota event, which says it soon enough
        # if this read fails.
        with contextlib.suppress(CalliopeError):
            sat = await client.satellite(self.satellite_id)
            self.coordinator.async_set_satellite(sat)
