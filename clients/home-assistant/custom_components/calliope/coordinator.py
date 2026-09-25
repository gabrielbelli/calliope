"""The satellites, kept current by the hub's event stream.

One long-lived GET /satellites/events per config entry. GET /satellites is
read when the stream opens (at start and after every reconnect), so nothing is
missed while it was down; after that every change arrives as an event and
nothing is polled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CalliopeApiError, CalliopeAuthError, CalliopeClient, CalliopeError
from .const import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    DOMAIN,
    EVENT_CALLIOPE,
    KIND_BUTTON_PRESS,
    KIND_BUTTON_RELEASE,
    KIND_COMMAND,
    KIND_CONVERSATION_ENDED,
    KIND_CONVERSATION_STARTED,
    KIND_TRIGGER_WORD,
    KIND_WAKE_WORD,
    QUIET_HUB_EVENTS,
    SETTING_KEYS,
)

_LOGGER = logging.getLogger(__name__)

type Satellites = dict[str, dict[str, Any]]
type CalliopeConfigEntry = ConfigEntry[CalliopeRuntime]


@dataclass
class CalliopeRuntime:
    """What a loaded entry holds."""

    client: CalliopeClient
    coordinator: CalliopeCoordinator
    health: dict[str, Any]
    voices: list[str] = field(default_factory=list)


def signal_voice(entry_id: str, satellite_id: str) -> str:
    """The dispatcher signal a satellite's happenings are sent on."""
    return f"{DOMAIN}_{entry_id}_{satellite_id}_voice"


def classify(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """A hub event as (kind, attributes) for the event entity and device
    triggers, or None when it is not a happening a person would automate.

    Written for the events the hub publishes today (wake, routed, button) and
    those it is gaining (triggered, turn, conversation_started,
    conversation_ended). Unknown fields are passed through, so a field the
    hub adds later reaches automations without a release here."""
    kind = event.get("type")
    extra = {
        k: v
        for k, v in event.items()
        if k not in ("type", "satellite", "at", "injected")
        and isinstance(v, (str, int, float, bool, type(None)))
    }
    if kind == "wake":
        return KIND_WAKE_WORD, extra
    if kind == "triggered":
        return KIND_TRIGGER_WORD, extra
    if kind in ("routed", "turn"):
        transcript = event.get("transcript") or event.get("text")
        if not transcript:
            return None  # nothing was said, or it failed before STT
        return KIND_COMMAND, extra | {"transcript": transcript}
    if kind == "button":
        edge = event.get("action")
        if edge == "press":
            return KIND_BUTTON_PRESS, extra
        if edge == "release":
            return KIND_BUTTON_RELEASE, extra
        return None
    if kind == "conversation_started":
        return KIND_CONVERSATION_STARTED, extra
    if kind == "conversation_ended":
        return KIND_CONVERSATION_ENDED, extra
    return None


class CalliopeCoordinator(DataUpdateCoordinator[Satellites]):
    """Adopted satellites by id, and the event stream that keeps them."""

    config_entry: CalliopeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: CalliopeConfigEntry, client: CalliopeClient
    ) -> None:
        """No update_interval: the event stream is the update."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
            # Reads asked for by events (a satellite adopted or forgotten)
            # come in bursts; one read a second is plenty.
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=1.0, immediate=True
            ),
        )
        self.client = client
        self.connected = False
        self.has_hub = True
        # GET /satellites/wake-words "words": name, threshold, satellites, and
        # on hubs from 2026-09-25 each word's mode (command, conversation or
        # trigger).
        self.words: list[dict[str, Any]] = []
        # A satellite's buttons from its caps, remembered while it is offline
        # (the hub reports caps only for a connected satellite).
        self.buttons: dict[str, list[str]] = {}
        # Satellites the hub lists but nobody has adopted. They publish status
        # too, and must not cause a read every 10 s.
        self.pending: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._backoff = BACKOFF_MIN
        self._refreshing: set[str] = set()

    # -- reading -----------------------------------------------------------

    async def _async_update_data(self) -> Satellites:
        try:
            listed = await self.client.satellites()
        except CalliopeAuthError as err:
            raise UpdateFailed(f"Calliope refused the API key: {err}") from err
        except CalliopeApiError as err:
            if err.status in (404, 503):
                # A gateway deployed without the satellite hub: speech works,
                # there are just no satellites.
                if self.has_hub:
                    _LOGGER.info("Calliope has no satellite hub (%s)", err)
                self.has_hub = False
                return {}
            raise UpdateFailed(str(err)) from err
        except CalliopeError as err:
            raise UpdateFailed(str(err)) from err
        self.has_hub = True
        try:
            self.words = list((await self.client.wake_words()).get("words") or [])
        except CalliopeError as err:
            _LOGGER.debug("Wake words not read: %s", err)
        satellites = {s["id"]: s for s in listed if s.get("adopted") and s.get("id")}
        self.pending = {s["id"] for s in listed if s.get("id") and not s.get("adopted")}
        for sid, sat in satellites.items():
            self._remember_caps(sid, sat)
        self._remove_forgotten(satellites)
        return satellites

    def _remember_caps(self, sid: str, sat: dict[str, Any]) -> None:
        buttons = (sat.get("caps") or {}).get("buttons")
        if isinstance(buttons, list) and buttons:
            self.buttons[sid] = [str(b) for b in buttons]

    @callback
    def _remove_forgotten(self, satellites: Satellites) -> None:
        """A satellite forgotten on the hub leaves Home Assistant too."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            sid = satellite_id_of(device)
            if sid is not None and sid not in satellites:
                registry.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )

    def word_mode(self, name: str) -> str | None:
        """command, conversation or trigger, when the hub says; else None."""
        for word in self.words:
            if word.get("name") == name:
                mode = word.get("mode")
                return str(mode) if mode else None
        return None

    # -- the event stream --------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Open the stream in the background, for as long as the entry is
        loaded."""
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._run(), f"{DOMAIN} event stream"
        )

    async def _run(self) -> None:
        while True:
            try:
                async with self.client.event_stream() as stream:
                    if not self.connected:
                        _LOGGER.info("Connected to the Calliope event stream")
                    self.connected = True
                    self._backoff = BACKOFF_MIN
                    # Whatever changed while the stream was down.
                    await self.async_refresh()
                    async for event in stream:
                        try:
                            self._on_event(event)
                        except Exception:  # noqa: BLE001 - one bad event must not end the stream
                            _LOGGER.exception(
                                "Could not handle a Calliope %s event",
                                event.get("type"),
                            )
                reason = "the hub closed the event stream"
            except CalliopeAuthError as err:
                reason = f"the API key was refused ({err})"
                self._backoff = BACKOFF_MAX
                self.config_entry.async_start_reauth(self.hass)
            except CalliopeApiError as err:
                reason = str(err)
                if err.status in (404, 503):
                    self._backoff = BACKOFF_MAX  # no hub behind the gateway
            except CalliopeError as err:
                reason = str(err)
            if self.connected:
                _LOGGER.warning(
                    "Lost the Calliope event stream: %s; reconnecting", reason
                )
                self.connected = False
                self.async_update_listeners()
            else:
                _LOGGER.debug("Calliope event stream unavailable: %s", reason)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    @callback
    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        sid = event.get("satellite")
        if event.get("injected"):
            # A recorded clip run through the pipeline to test it
            # (POST /satellites/{id}/inject). The hub keeps these away from
            # MQTT so they cannot fire the household's automations; so does
            # this.
            return
        if kind == "wake_words":
            self.hass.async_create_task(self.async_request_refresh())
            return
        if not isinstance(sid, str) or not sid:
            return
        sat = (self.data or {}).get(sid)
        if sat is None:
            # Pending satellites are not exposed, so their events are dropped.
            # "online" is published only for an adopted satellite, and an id
            # never seen may be one adopted since the last read: read again.
            if kind == "online" or (kind != "pending" and sid not in self.pending):
                self.hass.async_create_task(self.async_request_refresh())
            return
        self._apply(sid, sat, event)
        if kind not in QUIET_HUB_EVENTS:
            self._fire(sid, sat, event)

    @callback
    def _apply(self, sid: str, sat: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "status":
            status = event.get("status") or {}
            before = {k: (sat.get("status") or {}).get(k) for k in SETTING_KEYS}
            after = {k: status.get(k) for k in SETTING_KEYS}
            sat["status"] = status
            sat["online"] = True
            # The satellite reports after every config it is sent, so a change
            # made on the Satellites page shows here first. Read the hub's
            # record then, rather than every 10 s.
            if any(v is not None for v in before.values()) and before != after:
                self._refresh_one(sid)
        elif kind == "online":
            sat["online"] = True
            if event.get("name"):
                sat["name"] = event["name"]
            if event.get("firmware"):
                sat["firmware"] = event["firmware"]
            self._refresh_one(sid)  # caps, address and config come with it
        elif kind == "offline":
            sat["online"] = False
            sat["status"] = {}
        elif kind == "pending":
            # Forgotten, or its token no longer matches: not ours any more.
            self.hass.async_create_task(self.async_request_refresh())
            return
        else:
            return
        self.async_update_listeners()

    @callback
    def _refresh_one(self, sid: str) -> None:
        if sid in self._refreshing:
            return
        self._refreshing.add(sid)
        self.config_entry.async_create_background_task(
            self.hass, self._read_one(sid), f"{DOMAIN} read {sid}"
        )

    async def _read_one(self, sid: str) -> None:
        try:
            sat = await self.client.satellite(sid)
        except CalliopeError as err:
            _LOGGER.debug("Could not read satellite %s: %s", sid, err)
            return
        finally:
            self._refreshing.discard(sid)
        self.async_set_satellite(sat)

    @callback
    def async_set_satellite(self, sat: dict[str, Any]) -> None:
        """A satellite as the hub describes it (GET or PATCH answer)."""
        sid = sat.get("id")
        if not sid or not self.data or sid not in self.data:
            return
        if not sat.get("adopted"):
            self.hass.async_create_task(self.async_request_refresh())
            return
        self._remember_caps(sid, sat)
        self.async_set_updated_data({**self.data, sid: sat})

    @callback
    def _fire(self, sid: str, sat: dict[str, Any], event: dict[str, Any]) -> None:
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, sid)})
        classified = classify(event)
        data = {
            **event,
            "device_id": device.id if device else None,
            "satellite_name": sat.get("name") or sid,
            "kind": classified[0] if classified else event.get("type"),
        }
        if classified is not None:
            kind, attributes = classified
            # Fields the automation editor's triggers match on, filled in when
            # the hub names them differently.
            if kind == KIND_COMMAND:
                data["transcript"] = attributes["transcript"]
            async_dispatcher_send(
                self.hass,
                signal_voice(self.config_entry.entry_id, sid),
                kind,
                attributes,
            )
        self.hass.bus.async_fire(EVENT_CALLIOPE, data)


def satellite_id_of(device: dr.DeviceEntry) -> str | None:
    """The hub's id for a satellite device; None for the Calliope service
    device itself."""
    for domain, ident in device.identifiers:
        if domain == DOMAIN and not ident.startswith("entry_"):
            return ident
    return None
