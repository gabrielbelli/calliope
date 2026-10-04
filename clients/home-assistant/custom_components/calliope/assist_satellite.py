"""Each satellite with a microphone and a speaker as an Assist satellite that
announces.

The hub runs the satellite's wake words, listening and routing itself, so
Home Assistant never runs a pipeline for it and this entity does only what
needs none: assist_satellite.announce, its chime, and "broadcast …"
(HassBroadcast) from any Assist device. Its state shows announcements only;
a conversation on the satellite leaves it idle.
"""

from __future__ import annotations

from homeassistant.components.assist_pipeline import PipelineEvent
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import media
from .api import CalliopeError
from .const import DOMAIN
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, device_name
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One per satellite with a microphone and a speaker."""

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeAssistSatellite:
        return CalliopeAssistSatellite(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.ASSIST_SATELLITE, build)


class CalliopeAssistSatellite(CalliopeSatelliteEntity, AssistSatelliteEntity):
    """Announcements through the hub's voice lane, which plays them over the
    music and answers once they have played."""

    _attr_name = None

    def _on_update(self) -> None:
        """ANNOUNCE only on a hub that plays media."""
        self._attr_supported_features = (
            AssistSatelliteEntityFeature.ANNOUNCE
            if (self.satellite or {}).get("media")
            else AssistSatelliteEntityFeature(0)
        )

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """The hub owns the wake words (its Wake words tab), so Home
        Assistant's satellite dialog shows none, as for Wyoming."""
        raise NotImplementedError

    async def async_set_configuration(
        self, config: AssistSatelliteConfiguration
    ) -> None:
        """The hub owns the wake words."""
        raise NotImplementedError

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Never called: Home Assistant runs no pipeline for this entity."""

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        """The chime, then the message, as one WAV in the satellite's voice
        format. Returns once the hub says it has played, as Home Assistant
        expects of an announcement."""
        sat = self.satellite or {}
        name = device_name(self.satellite_id, sat)
        fmt = (sat.get("media") or {}).get("announce")
        if not isinstance(fmt, dict):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hub_cannot_play"
            )
        sources = [announcement.media_id]
        if announcement.preannounce_media_id:
            sources.insert(0, announcement.preannounce_media_id)
        try:
            result = await self.coordinator.client.media(
                self.satellite_id,
                media.wav_chunks(
                    self.hass,
                    sources,
                    int(fmt["rate"]),
                    int(fmt["channels"]),
                    satellite=name,
                ),
                announce=True,
            )
        except CalliopeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="satellite_refused",
                translation_placeholders={"satellite": name, "error": str(err)},
            ) from err
        media.log_end(name, result)
