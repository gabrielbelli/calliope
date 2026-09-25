"""Voice: what a satellite heard, as an event entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import VOICE_EVENT_TYPES
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, signal_voice
from .entity import CalliopeSatelliteEntity, add_per_satellite

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One Voice event entity per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[CalliopeVoice]:
        return [CalliopeVoice(coordinator, sid)]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeVoice(CalliopeSatelliteEntity, EventEntity):
    """wake_word, trigger_word, command, button_press, button_release,
    conversation_started and conversation_ended; the word, button or
    transcript is in the event's attributes."""

    _attr_translation_key = "voice"
    _attr_event_types = VOICE_EVENT_TYPES
    available_offline = True

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "voice"."""
        super().__init__(coordinator, sid, "voice")

    async def async_added_to_hass(self) -> None:
        """Listen for the satellite's happenings."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_voice(self.coordinator.config_entry.entry_id, self.satellite_id),
                self._on_voice,
            )
        )

    @callback
    def _on_voice(self, kind: str, attributes: dict[str, Any]) -> None:
        self._trigger_event(kind, attributes)
        self.async_write_ha_state()
