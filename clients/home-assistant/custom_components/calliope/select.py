"""Where a satellite plays and listens: its output and input devices (a Pi's
sound cards), and the satellite whose speaker it answers through."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .capabilities import caps_of, has, known
from .coordinator import CalliopeConfigEntry, CalliopeCoordinator, device_name
from .entity import CalliopeSatelliteEntity, add_for_caps

PARALLEL_UPDATES = 1

# The option that means "no choice of its own": translated, unlike the device
# and satellite names beside it.
DEFAULT = "default"
OWN = "own"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalliopeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """The selects a satellite's caps want."""

    def build(
        coordinator: CalliopeCoordinator, sid: str, key: str
    ) -> CalliopeSatelliteEntity:
        if key == "output_satellite":
            return CalliopeOutputSatellite(coordinator, sid, key)
        if key == "output_device":
            return CalliopeAudioDevice(coordinator, sid, key, "audio_sink", "sinks")
        return CalliopeAudioDevice(coordinator, sid, key, "audio_source", "sources")

    add_for_caps(entry, async_add_entities, Platform.SELECT, build)


class CalliopeAudioDevice(CalliopeSatelliteEntity, SelectEntity):
    """A Pi's output or input: the system default, or one of the devices it
    reports (status.audio). An option is the device's description, as the
    Satellites page shows it; the PATCH names the PipeWire node."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: CalliopeCoordinator,
        sid: str,
        key: str,
        field: str,
        listed: str,
    ) -> None:
        """Keyed output_device (audio_sink) or input_device (audio_source)."""
        super().__init__(coordinator, sid, key)
        self._attr_translation_key = key
        self._field = field
        self._listed = listed

    def _nodes(self) -> dict[str, str]:
        """Option to node name. A description two devices share gets " (2)",
        " (3)" after the first."""
        audio = self.status.get("audio")
        devices = audio.get(self._listed) if isinstance(audio, dict) else None
        nodes: dict[str, str] = {DEFAULT: ""}
        for device in devices if isinstance(devices, list) else []:
            if not isinstance(device, dict) or not device.get("name"):
                continue
            label = str(device.get("description") or device["name"])
            option, n = label, 1
            while option in nodes:
                n += 1
                option = f"{label} ({n})"
            nodes[option] = str(device["name"])
        return nodes

    @property
    def options(self) -> list[str]:
        """The default, then each device."""
        return list(self._nodes())

    @property
    def current_option(self) -> str | None:
        """The configured device; unknown when it is not plugged in."""
        chosen = self.config.get(self._field) or ""
        return next((o for o, n in self._nodes().items() if n == chosen), None)

    async def async_select_option(self, option: str) -> None:
        """PATCH the node name, or "" for the system default."""
        await self._patch(**{self._field: self._nodes()[option]})


class CalliopeOutputSatellite(CalliopeSatelliteEntity, SelectEntity):
    """Which speaker answers for this satellite: its own, or another
    adopted satellite's (output_satellite). Offered are the satellites with a
    speaker, and those whose caps are not known yet."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "output_satellite"
    # The hub keeps it while the satellite is away.
    available_offline = True

    def _others(self) -> dict[str, str]:
        """Option to satellite id. A name two satellites share, or the name
        "own" (the option for its own speaker), gets the last four
        characters of the id."""
        others = {
            sid: device_name(sid, sat)
            for sid, sat in (self.coordinator.data or {}).items()
            if sid != self.satellite_id
            and (not known(sat) or has(caps_of(sat), "speaker"))
        }
        names = [*others.values(), OWN]
        options: dict[str, str] = {OWN: ""}
        for sid, name in sorted(others.items(), key=lambda item: item[1]):
            options[f"{name} ({sid[-4:]})" if names.count(name) > 1 else name] = sid
        return options

    def _marks(self) -> tuple[Any, ...]:
        """Another satellite renamed, adopted or forgotten changes the
        options."""
        return (*super()._marks(), tuple(self._others().items()))

    @property
    def options(self) -> list[str]:
        """Its own speaker, then the others by name."""
        return list(self._others())

    @property
    def current_option(self) -> str | None:
        """The configured satellite; unknown when the hub no longer has it."""
        chosen = self.config.get("output_satellite") or ""
        return next((o for o, s in self._others().items() if s == chosen), None)

    async def async_select_option(self, option: str) -> None:
        """PATCH output_satellite, "" for its own speaker. The hub refuses a
        loop (422) with its reason."""
        await self._patch(output_satellite=self._others()[option])
