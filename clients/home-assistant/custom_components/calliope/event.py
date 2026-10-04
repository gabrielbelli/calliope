"""What a satellite heard, and each of its buttons, as event entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    BUTTON_EVENT_TYPES,
    BUTTON_LABELS,
    KIND_BUTTON_PRESS,
    KIND_BUTTON_RELEASE,
    VOICE_EVENT_TYPES,
)
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, signal_voice
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 0

# The hub's button event edge as the button entity's event type.
EDGES = {KIND_BUTTON_PRESS: "press", KIND_BUTTON_RELEASE: "release"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Voice for a satellite with a microphone; one entity per button it
    has."""

    def build(coordinator: CalliopeCoordinator, sid: str, key: str) -> _Happenings:
        if key == "voice":
            return CalliopeVoice(coordinator, sid, key)
        return CalliopeButtonEvent(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.EVENT, build)


class _Happenings(CalliopeSatelliteEntity, EventEntity):
    """An event entity fed by the satellite's happenings (coordinator
    classify()). Available whenever the stream is: the last event stays true
    while the satellite is offline."""

    available_offline = True

    async def async_added_to_hass(self) -> None:
        """Listen for the satellite's happenings."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_voice(self.coordinator.config_entry.entry_id, self.satellite_id),
                self._on_happening,
            )
        )

    @callback
    def _on_happening(self, kind: str, attributes: dict[str, Any]) -> None:
        raise NotImplementedError


class CalliopeVoice(_Happenings):
    """wake_word, trigger_word, command, conversation_started and
    conversation_ended; the word or transcript is in the event's
    attributes."""

    _attr_translation_key = "voice"
    _attr_event_types = VOICE_EVENT_TYPES

    @callback
    def _on_happening(self, kind: str, attributes: dict[str, Any]) -> None:
        if kind in VOICE_EVENT_TYPES:
            self._trigger_event(kind, attributes)
            self.async_write_ha_state()


class CalliopeButtonEvent(_Happenings):
    """One button of the satellite, pressed and released, with held_ms. Off
    by default, as for other speakers with buttons: most people automate a
    button through a device trigger, and these add seven entities to a
    Korvo."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = BUTTON_EVENT_TYPES
    _attr_translation_key = "button"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: CalliopeCoordinator, sid: str, key: str) -> None:
        """Keyed "button_<id>"; named as the board prints it."""
        super().__init__(coordinator, sid, key)
        self._button = key.removeprefix("button_")
        self._attr_translation_placeholders = {
            "button": BUTTON_LABELS.get(self._button, self._button.upper())
        }

    @property
    def suggested_object_id(self) -> str | None:
        """From the button's id, not its label: "VOL+ button" and "VOL-
        button" both slug to vol_button, and Home Assistant would number
        the second in whichever order they came. Home Assistant still puts
        the device's name first."""
        return f"{self._button} button"

    @callback
    def _on_happening(self, kind: str, attributes: dict[str, Any]) -> None:
        if kind in EDGES and attributes.get("button") == self._button:
            self._trigger_event(EDGES[kind], attributes)
            self.async_write_ha_state()
