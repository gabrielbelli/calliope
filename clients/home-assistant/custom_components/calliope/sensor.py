"""Wi-Fi signal, uptime, the Pi's temperature, the last command transcribed
and the last wake word heard."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import KIND_COMMAND, KIND_TRIGGER_WORD, KIND_WAKE_WORD
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, signal_voice
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 0

# A state is at most 255 characters; the whole transcript is an attribute.
MAX_STATE = 255
# Now less the uptime moves by a second or so between two statuses: uptime
# is whole seconds, and each status arrives a little late. A start this far
# from the last one shown is a new start.
UPTIME_SLACK = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Signal and uptime for every satellite; the rest as its caps say."""
    classes: dict[str, type[CalliopeSatelliteEntity]] = {
        "rssi": CalliopeRssi,
        "uptime": CalliopeUptime,
        "cpu_temperature": CalliopeCpuTemperature,
        "last_command": CalliopeLastCommand,
        "last_wake_word": CalliopeLastWakeWord,
    }

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeSatelliteEntity:
        return classes[key](coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.SENSOR, build)


class CalliopeRssi(CalliopeSatelliteEntity, SensorEntity):
    """RSSI from the satellite's status, every 10 s."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "wifi_signal"

    @property
    def native_value(self) -> int | None:
        """Unknown until the first status after a connect, and for good on a
        Pi wired to Ethernet."""
        rssi = self.status.get("rssi")
        return rssi if isinstance(rssi, (int, float)) else None


class CalliopeUptime(CalliopeSatelliteEntity, SensorEntity):
    """When the satellite last started, from the uptime in its status. Off by
    default, as uptime sensors are: it matters only when chasing restarts."""

    _attr_device_class = SensorDeviceClass.UPTIME
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "uptime"
    _attr_native_value: datetime | None = None
    _uptime: float | None = None

    def _on_update(self) -> None:
        """Now less the uptime, kept while each status agrees with it to
        within UPTIME_SLACK: recomputed every time, it would be a new state,
        and a recorder row, every other status. A smaller uptime than the
        last is a restart, however soon after the last start."""
        uptime = self.status.get("uptime_s")
        if not isinstance(uptime, (int, float)) or isinstance(uptime, bool):
            self._attr_native_value = self._uptime = None
            return
        started = dt_util.utcnow() - timedelta(seconds=uptime)
        shown = self._attr_native_value
        if (
            shown is None
            or self._uptime is None
            or uptime < self._uptime
            or abs(started - shown) > UPTIME_SLACK
        ):
            self._attr_native_value = started
        self._uptime = uptime


class CalliopeCpuTemperature(CalliopeSatelliteEntity, SensorEntity):
    """The Pi's SoC temperature. It throttles itself at 80 °C."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "cpu_temperature"

    @property
    def native_value(self) -> float | None:
        """From the satellite's status."""
        temp = self.status.get("temp_c")
        return temp if isinstance(temp, (int, float)) else None


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

    def _take(self, kind: str, attributes: dict[str, Any]) -> None:
        self._attr_native_value = attributes.get("wake_word")
        self._attr_extra_state_attributes = {
            "score": attributes.get("score"),
            "trigger": kind == KIND_TRIGGER_WORD,
        }
