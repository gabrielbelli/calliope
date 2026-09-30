"""A satellite's microphone gain and ring brightness.

There is no volume slider here: the media player is the one volume control,
and it sets the volume of the satellite that actually plays.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .capabilities import caps_of
from .const import MIC_GAIN_MAX_DB
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Gain for a satellite with a microphone; brightness for one with a
    ring."""

    def build(coordinator: CalliopeCoordinator, sid: str, key: str) -> CalliopeNumber:
        if key == "mic_gain":
            return CalliopeMicGain(coordinator, sid, key)
        return CalliopeBrightness(coordinator, sid, key)

    add_for_caps(entry, async_add_entities, Platform.NUMBER, build)


class CalliopeNumber(CalliopeSatelliteEntity, NumberEntity):
    """One number setting, PATCHed as the hub takes it."""

    _attr_entity_category = EntityCategory.CONFIG
    # The hub keeps it while the satellite is away and sends it at the next
    # welcome.
    available_offline = True
    field = ""

    @property
    def native_value(self) -> float | None:
        """The hub's config."""
        value = self.config.get(self.field)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """PATCH it."""
        await self._patch(**{self.field: self._to_hub(value)})

    def _to_hub(self, value: float) -> float | int:
        return value


class CalliopeMicGain(CalliopeNumber):
    """mic_gain_db, in the hub's 0.5 dB steps, up to what this satellite
    does with it: the Pi clamps its input at 1.5x (3.5 dB), so a larger gain
    would only look different on the slider."""

    _attr_translation_key = "mic_gain"
    _attr_native_min_value = 0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.BOX
    field = "mic_gain_db"

    @property
    def native_max_value(self) -> float:
        """caps.mic.max_gain_db, else the hub's own limit."""
        mic = caps_of(self.satellite).get("mic")
        top = mic.get("max_gain_db") if isinstance(mic, dict) else None
        if isinstance(top, (int, float)) and not isinstance(top, bool) and top > 0:
            return float(top)
        return MIC_GAIN_MAX_DB


class CalliopeBrightness(CalliopeNumber):
    """The ring's brightness, 1 to 100 %."""

    _attr_translation_key = "brightness"
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER
    field = "brightness"

    def _to_hub(self, value: float) -> float | int:
        return round(value)  # the hub takes an integer
