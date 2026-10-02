"""The satellites, kept current by the hub's event stream.

One long-lived GET /satellites/events per config entry. GET /satellites is
read when the stream opens (at start and after every reconnect), so nothing is
missed while it was down; after that every change arrives as an event, and
one satellite is read again (GET /satellites/{id}) when an event says its
record changed. The gateway ends every event stream after 15 minutes so that
the key is checked again; that end is routine, and the stream is opened again
without the entities going unavailable.

Each satellite has a revision, bumped whenever anything about it changes. An
entity writes its state only when its satellite's revision (or the stream's
health) moved, so the status every satellite sends every 10 s rewrites that
satellite's entities and nobody else's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CalliopeApiError,
    CalliopeAuthError,
    CalliopeClient,
    CalliopeConnectionError,
    CalliopeError,
)
from .capabilities import caps_of, known, wanted
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
    STREAM_ROUTINE_AFTER,
)
from .vocabulary import Vocabulary

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
    vocabulary: Vocabulary | None = None


def signal_voice(entry_id: str, satellite_id: str) -> str:
    """The dispatcher signal a satellite's happenings are sent on."""
    return f"{DOMAIN}_{entry_id}_{satellite_id}_voice"


def signal_removed(entry_id: str) -> str:
    """The dispatcher signal that names the satellites whose devices were
    just removed, so each platform adds them afresh if they come back."""
    return f"{DOMAIN}_{entry_id}_removed"


def classify(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """A hub event as (kind, attributes) for the event entities and device
    triggers, or None when it is not a happening a person would automate:
    wake, triggered, routed and turn (with a transcript), button,
    conversation_started and conversation_ended. Unknown fields are passed
    through, so a field the hub adds later reaches automations without a
    release here."""
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
        # Satellites the hub lists but nobody has adopted. They publish status
        # too, and must not cause a read every 10 s.
        self.pending: set[str] = set()
        # Bumped each time something about a satellite changes; its entities
        # write their state when it moves (entity.py).
        self.revision: dict[str, int] = {}
        # Each satellite's caps as sorted JSON, the last time they were known.
        # A change removes the entities of what it no longer has.
        self._fingerprint: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._backoff = BACKOFF_MIN
        self._refreshing: set[str] = set()
        self._reread: set[str] = set()

    # -- reading -----------------------------------------------------------

    async def _async_update_data(self) -> Satellites:
        try:
            listed = await self.client.satellites()
        except CalliopeAuthError as err:
            # The coordinator turns this into a reauth: a revoked or expired
            # key will not come back by retrying.
            raise ConfigEntryAuthFailed(f"Calliope refused the API key: {err}") from err
        except CalliopeApiError as err:
            if err.status in (403, 404, 503):
                # A gateway deployed without the satellite hub, or a key that
                # may not see it (the client has raised a repair issue for
                # that): speech works, there are just no satellites.
                if self.has_hub:
                    _LOGGER.info("Calliope's satellites are out of reach (%s)", err)
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
            self._reconcile(sid, sat)
        for sid in set(self.revision) - set(satellites):
            del self.revision[sid]
            self._fingerprint.pop(sid, None)
        self._remove_forgotten(satellites)
        return satellites

    # -- keeping the registries in step with the hub -------------------------

    def _bump(self, sid: str) -> None:
        self.revision[sid] = self.revision.get(sid, 0) + 1

    @callback
    def _reconcile(self, sid: str, sat: dict[str, Any]) -> None:
        """A satellite as the hub now describes it: its entities write their
        state, its device follows the hub, and once its caps are known and
        have changed, the entities of what it no longer has are removed."""
        self._bump(sid)
        self._sync_device(sid, sat)
        if not known(sat):
            # Unknown is not absent: nothing is removed on a guess.
            return
        fingerprint = json.dumps(caps_of(sat), sort_keys=True, default=str)
        if self._fingerprint.get(sid) != fingerprint:
            self._fingerprint[sid] = fingerprint
            self._prune(sid, sat)

    @callback
    def _sync_device(self, sid: str, sat: dict[str, Any]) -> None:
        """The device registry as the hub has it: a rename on the Satellites
        page, a new firmware after OTA. Written only when something differs,
        as every write is an event."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, sid)})
        if device is None:
            return  # its first entity creates it, from the same record
        model = sat.get("model")
        wanted_fields = {
            "name": device_name(sid, sat),
            "model": model,
            "sw_version": sat.get("firmware"),
            "manufacturer": manufacturer_of(model),
            "configuration_url": self.client.url + "/ui",
        }
        changes = {
            k: v
            for k, v in wanted_fields.items()
            # An unknown value (a firmware the hub forgot at its restart) never
            # replaces a known one.
            if v is not None and getattr(device, k) != v
        }
        if changes:
            registry.async_update_device(device.id, **changes)

    @callback
    def _prune(self, sid: str, sat: dict[str, Any]) -> None:
        """Remove from the entity registry every entity of this satellite
        that its caps no longer want: a Lights switch on a satellite without a
        ring, the volume slider the media player replaced, a microphone that
        was unplugged. Disabled entities too. Home Assistant removes a loaded
        entity with its registry entry."""
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, sid)})
        if device is None:
            return
        want = wanted(sat)
        registry = er.async_get(self.hass)
        prefix = f"{sid}_"
        removed = []
        for entry in er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        ):
            if entry.config_entry_id != self.config_entry.entry_id or not (
                entry.unique_id.startswith(prefix)
            ):
                continue
            if want.get(entry.unique_id[len(prefix) :]) != entry.domain:
                registry.async_remove(entry.entity_id)
                removed.append(entry.entity_id)
        if removed:
            _LOGGER.info(
                "Removed %s: %s does not have what they were for",
                ", ".join(removed),
                device_name(sid, sat),
            )

    @callback
    def _remove_forgotten(self, satellites: Satellites) -> None:
        """A satellite forgotten on the hub leaves Home Assistant too. The
        platforms are told, so one adopted again gets its device and
        entities back rather than being taken for one they already have."""
        registry = dr.async_get(self.hass)
        removed = set()
        for device in dr.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            sid = satellite_id_of(device)
            if sid is not None and sid not in satellites:
                registry.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )
                removed.add(sid)
        if removed:
            async_dispatcher_send(
                self.hass, signal_removed(self.config_entry.entry_id), removed
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
                    # Whatever changed while the stream was down. A read that
                    # fails leaves every entity unavailable, and nothing on the
                    # stream would read again: reconnect, with backoff.
                    await self.async_refresh()
                    if not self.last_update_success:
                        raise CalliopeConnectionError("could not read the satellites")
                    healthy_since = time.monotonic()
                    async for event in stream:
                        try:
                            self._on_event(event)
                        except Exception:  # noqa: BLE001 - one bad event must not end the stream
                            _LOGGER.exception(
                                "Could not handle a Calliope %s event",
                                event.get("type"),
                            )
                lasted = time.monotonic() - healthy_since
                if lasted >= STREAM_ROUTINE_AFTER:
                    # The gateway's 15-minute limit, which makes the key be
                    # checked again. Opened again at once, and the entities
                    # stay as they are: they go unavailable only if that
                    # fails, which is how a hub that restarted is found, and
                    # a 401 then still asks for a new key.
                    _LOGGER.debug(
                        "Calliope ended the event stream after %.0f s; reopening it",
                        lasted,
                    )
                    continue
                reason = "the hub closed the event stream"
            except CalliopeAuthError as err:
                reason = f"the API key was refused ({err})"
                self._backoff = BACKOFF_MAX
                self.config_entry.async_start_reauth(self.hass)
            except CalliopeApiError as err:
                reason = str(err)
                if err.status in (403, 404, 503):
                    # No hub behind the gateway, or none this key may see.
                    self._backoff = BACKOFF_MAX
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
        if kind in ("wake_words", "firmware"):
            # Hub-wide: the words assigned, or the images an update can offer.
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
            status = event.get("status")
            if not isinstance(status, dict):
                _LOGGER.debug("Skipping a status of %s that is not an object", sid)
                return
            sat["status"] = status
            sat["online"] = True
        elif kind == "online":
            sat["online"] = True
            if event.get("name"):
                sat["name"] = event["name"]
            if event.get("firmware"):
                sat["firmware"] = event["firmware"]
            self._refresh_one(sid)  # caps, address and config come with it
        elif kind == "offline":
            # The caps stay: the hub keeps them for an offline satellite too.
            sat["online"] = False
            sat["status"] = {}
        elif kind in ("config", "settings"):
            # A PATCH from the page, or a setting the satellite changed itself
            # (a button, the phone's AirPlay volume): the hub's record changed.
            self._refresh_one(sid)
            return
        elif kind == "ota":
            ota = dict(sat.get("ota") or {})
            ota.update(
                {
                    k: event[k]
                    for k in ("state", "pct", "version", "error")
                    if k in event and (k != "version" or event[k] is not None)
                }
            )
            sat["ota"] = ota
            if event.get("state") in ("verified", "failed"):
                self._refresh_one(sid)  # the update on offer changes
        elif kind == "media":
            # The stream plays on one satellite and was addressed to another
            # (an Output set on the page): both describe it.
            self._refresh_one(sid)
            source = event.get("source")
            if isinstance(source, str) and source != sid and source in self.data:
                self._refresh_one(source)
            return
        elif kind == "pending":
            # Forgotten, or its token no longer matches: not ours any more.
            self.hass.async_create_task(self.async_request_refresh())
            return
        else:
            return
        self._bump(sid)
        self.async_update_listeners()

    @callback
    def _refresh_one(self, sid: str) -> None:
        if sid in self._refreshing:
            # The read under way may have been answered before this change:
            # read once more when it is done.
            self._reread.add(sid)
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
            sat = None
        finally:
            self._refreshing.discard(sid)
        # Not reached when cancelled: the entry is unloading, and a read
        # started now would outlive it (Home Assistant cancels only the
        # tasks it had when the unload began).
        if sid in self._reread:
            self._reread.discard(sid)
            self._refresh_one(sid)
        if sat is not None:
            self.async_set_satellite(sat)

    @callback
    def async_set_satellite(self, sat: dict[str, Any]) -> None:
        """A satellite as the hub describes it (GET or PATCH answer). Only
        its own entities are written, and a full read that is pending, or a
        failed one, is left as it is: async_set_updated_data would cancel the
        one and paper over the other."""
        sid = sat.get("id")
        if not sid or not self.data or sid not in self.data:
            return
        if not sat.get("adopted"):
            self.hass.async_create_task(self.async_request_refresh())
            return
        self.data[sid] = sat
        self._reconcile(sid, sat)
        self.async_update_listeners()

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


def device_name(sid: str, sat: dict[str, Any]) -> str:
    """The satellite's name on the hub, or one made from its id."""
    return sat.get("name") or f"satellite-{sid[-4:]}"


def manufacturer_of(model: str | None) -> str | None:
    """Who made the board, from the model the firmware reports."""
    model = str(model or "")
    if model.startswith("esp32-korvo"):
        return "Espressif"
    if model == "raspberry-pi":
        return "Raspberry Pi"
    return None


def satellite_id_of(device: dr.DeviceEntry) -> str | None:
    """The hub's id for a satellite device; None for the Calliope service
    device itself."""
    for domain, ident in device.identifiers:
        if domain == DOMAIN and not ident.startswith("entry_"):
            return ident
    return None
