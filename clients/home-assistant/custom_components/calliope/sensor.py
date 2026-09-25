"""Wi-Fi signal, the last command transcribed and the last wake word heard."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_COMMAND, KIND_TRIGGER_WORD, KIND_WAKE_WORD
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, signal_voice
from .entity import CalliopeSatelliteEntity, add_per_satellite

PARALLEL_UPDATES = 0

# A state is at most 255 characters; the whole transcript is an attribute.
MAX_STATE = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Three sensors per satellite."""

    def build(coordinator: CalliopeCoordinator, sid: str) -> list[SensorEntity]:
        return [
            CalliopeRssi(coordinator, sid),
            CalliopeLastCommand(coordinator, sid),
            CalliopeLastWakeWord(coordinator, sid),
        ]

    add_per_satellite(entry, async_add_entities, build)


class CalliopeRssi(CalliopeSatelliteEntity, SensorEntity):
    """RSSI from the satellite's status, every 10 s."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "wifi_signal"

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "rssi"."""
        super().__init__(coordinator, sid, "rssi")

    @property
    def native_value(self) -> int | None:
        """Unknown until the first status after a connect."""
        rssi = ((self.satellite or {}).get("status") or {}).get("rssi")
        return rssi if isinstance(rssi, (int, float)) else None


class _LastHeard(CalliopeSatelliteEntity, RestoreSensor):
    """A sensor fed by the satellite's happenings rather than its record,
    and kept across restarts. Available whenever the stream is: what was
    last said stays true while the satellite is offline."""

    available_offline = True
    kinds: tuple[str, ...] = ()
    attribute_keys: tuple[str, ...] = ()

    async def async_added_to_hass(self) -> None:
        """Restore, then listen."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        if (state := await self.async_get_last_state()) is not None:
            self._attr_extra_state_attributes = {
                k: state.attributes.get(k) for k in self.attribute_keys
            }
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_voice(self.coordinator.config_entry.entry_id, self.satellite_id),
                self._on_voice,
            )
        )

    @callback
    def _on_voice(self, kind: str, attributes: dict[str, Any]) -> None:
        if kind in self.kinds:
            self._take(kind, attributes)
            self.async_write_ha_state()

    def _take(self, kind: str, attributes: dict[str, Any]) -> None:
        raise NotImplementedError


class CalliopeLastCommand(_LastHeard):
    """The transcript of the last command."""

    _attr_translation_key = "last_command"
    kinds = (KIND_COMMAND,)
    attribute_keys = ("transcript", "wake_word", "reply", "error")

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "last_command"."""
        super().__init__(coordinator, sid, "last_command")

    def _take(self, kind: str, attributes: dict[str, Any]) -> None:
        transcript = str(attributes.get("transcript") or "")
        self._attr_native_value = transcript[:MAX_STATE]
        self._attr_extra_state_attributes = {
            "transcript": transcript,
            "wake_word": attributes.get("wake_word"),
            "reply": attributes.get("reply_text"),
            "error": attributes.get("error"),
        }


class CalliopeLastWakeWord(_LastHeard):
    """The last wake word or trigger word heard."""

    _attr_translation_key = "last_wake_word"
    kinds = (KIND_WAKE_WORD, KIND_TRIGGER_WORD)
    attribute_keys = ("score", "trigger")

    def __init__(self, coordinator: CalliopeCoordinator, sid: str) -> None:
        """Keyed "last_wake_word"."""
        super().__init__(coordinator, sid, "last_wake_word")

    def _take(self, kind: str, attributes: dict[str, Any]) -> None:
        self._attr_native_value = attributes.get("wake_word")
        self._attr_extra_state_attributes = {
            "score": attributes.get("score"),
            "trigger": kind == KIND_TRIGGER_WORD,
        }
