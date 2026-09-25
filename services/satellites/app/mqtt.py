"""Home Assistant over MQTT: every adopted satellite as one device, by
discovery.

Optional. Nothing happens unless SATELLITES_MQTT_URL is set
(mqtt://user:pass@host:1883 or mqtts://...). Without it MqttBridge is the same
object with nothing behind it, so the hub calls it the same way either way.

    SATELLITES_MQTT_URL      the broker; unset means no MQTT at all
    SATELLITES_MQTT_PREFIX   Home Assistant's discovery prefix (homeassistant)
    SATELLITES_MQTT_BASE     where the hub's own topics live (calliope/satellites)

The hub's topics, for a satellite <id> (its MAC without colons):

    <base>/bridge/availability   online | offline, retained; also the last will
    <base>/<id>/availability     online | offline, retained
    <base>/<id>/state            JSON, retained: rssi, volume, *_enabled
    <base>/<id>/wake_word        the last wake word heard, retained
    <base>/<id>/button/<name>    {"event_type": "press"|"release", "held_ms"}
    <base>/<id>/wake             {"event_type": "detected", "wake_word", "score"}
    <base>/<id>/set/<field>      from Home Assistant: mic_enabled, speaker_enabled,
                                 lights_enabled (ON|OFF), volume (0-100)

Commands sit under set/ rather than ending in /set because the Korvo has a
button called "set": <id>/button/set would match a <id>/+/set subscription and
the bridge would read its own button presses as commands.

THE BRIDGE DECIDES NOTHING ABOUT A SATELLITE. A command from Home Assistant
becomes a change dict for on_command, which the hub points at the same code as
PATCH /satellites/{id}, so validation, saving and telling the satellite happen
in one place. The bridge holds no session and never sends a satellite anything.
That is how the lights switch keeps its promise: it changes lights_enabled and
nothing else, so it cannot send a light pattern, least of all to a satellite
whose lights are off.

SWITCHES AND VOLUME SHOW THE HUB'S CONFIG, NOT THE DEVICE'S STATUS. The
Satellites tab shows the same, and the config changes the moment PATCH returns,
while the satellite's status only follows a round trip later: a switch fed from
status would flick back to its old position between the two. The cost is that a
volume changed with the satellite's own VOL buttons does not show in Home
Assistant, as it does not on the Satellites tab, because the hub does not write
local changes back into the config (and sends its own value again at the next
welcome).

EVENTS ARE DROPPED WHILE THE BROKER IS AWAY; STATE IS NOT. A button press
delivered a minute late fires an automation at the wrong moment, which is
worse than not firing it. Discovery, availability and state are retained and
sent again in full on every reconnect and whenever Home Assistant announces
itself on <prefix>/status.

FORGETTING A SATELLITE REMOVES ITS DEVICE. An empty retained config is how Home
Assistant deletes an entity, and the device goes with its last entity. The list
of what was published lives in memory, seeded with every adopted satellite at
start, so a satellite forgotten while the broker is down is still cleared when
it comes back. The one gap: forgotten while the broker is down AND the hub
restarted before it returns. That device stays in Home Assistant until it is
deleted there by hand.

Checked against Home Assistant 2026.8.1's own MQTT integration (its test
harness, paho mocked): a Korvo becomes one device with 14 entities, a null
rssi reads "unknown" rather than raising a template error, and the empty
configs remove all 14 entities and the device.

One base per hub. Two hubs on one broker need different SATELLITES_MQTT_BASE
values, or they share one bridge availability topic and one last will.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger("voice-satellites.mqtt")

OnCommand = Callable[[str, dict], Awaitable[Any]]

QOS = 1
ORIGIN = {"name": "calliope voice-satellites"}
# Only for clearing: a satellite forgotten while offline since the hub started
# never showed the hub its caps, so its button entities are cleared by the
# names the Korvo's hello lists. Clearing a topic that holds nothing is free.
KNOWN_BUTTONS = ("vol_up", "vol_down", "set", "play", "mode", "rec")
BUTTON_LABELS = {"vol_up": "Volume up", "vol_down": "Volume down"}
SWITCHES = {  # config key -> (object id, entity name, icon)
    "mic_enabled": ("microphone", "Microphone", "mdi:microphone"),
    "speaker_enabled": ("speaker", "Speaker", "mdi:speaker"),
    "lights_enabled": ("lights", "Lights", "mdi:led-on"),
}
# A satellite id is satellite_id() in main.py: lower-case hex. Anything else in
# that topic level is not ours, "bridge" included.
NID = re.compile(r"^[0-9a-f]{1,32}$")
# Home Assistant accepts [a-zA-Z0-9_-] in a discovery topic's object id.
BUTTON = re.compile(r"^[a-z0-9_-]{1,32}$")

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
# A connection that lasted this long was not a flapping one, so the next
# failure starts the backoff again from the bottom.
STABLE_S = 60.0
COMMAND_TIMEOUT_S = 10.0


def parse_url(url: str) -> dict:
    """The broker's host, port, credentials and whether to use TLS.

    Raises ValueError saying what is wrong without repeating the URL, which
    carries a password and would otherwise end up in the log.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("mqtt", "mqtts"):
        raise ValueError(f"the scheme must be mqtt:// or mqtts://, not {parts.scheme or 'none'}://")
    if not parts.hostname:
        raise ValueError("there is no broker host")
    tls = parts.scheme == "mqtts"
    try:
        port = parts.port or (8883 if tls else 1883)
    except ValueError:
        raise ValueError("the port is not a number from 1 to 65535") from None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("a path, query or fragment means nothing to MQTT; "
                         "give scheme://user:pass@host:port")
    return {
        "hostname": parts.hostname,
        "port": port,
        # Percent-decoded, so a password with @ or : in it can be written %40 or %3A.
        "username": unquote(parts.username) if parts.username else None,
        "password": unquote(parts.password) if parts.password else None,
        "tls": tls,
    }


def parse_command(field: str, payload: str) -> dict | None:
    """A Home Assistant command as a PATCH /satellites/{id} change, or None.

    Only the four fields that have entities are accepted: a command topic is
    writable by anything on the broker, and it must not reach settings Home
    Assistant was never given, such as the satellite's name or microphone gain.
    """
    if field in SWITCHES:
        value = {"ON": True, "OFF": False}.get(payload)
        return None if value is None else {field: value}
    if field == "volume":
        try:
            v = float(payload)
        except ValueError:
            return None
        if not math.isfinite(v) or not 0 <= v <= 100:
            return None
        return {"volume": round(v)}
    return None


def satellite_state(satellite: dict) -> dict:
    """The retained state for a satellite, as Hub.describe() gives it."""
    cfg = satellite.get("config") or {}
    status = satellite.get("status") or {}
    return {
        "rssi": status.get("rssi"),
        "volume": cfg.get("volume"),
        "mic_enabled": cfg.get("mic_enabled"),
        "speaker_enabled": cfg.get("speaker_enabled"),
        "lights_enabled": cfg.get("lights_enabled"),
    }


def discovery_configs(satellite: dict, *, prefix: str, base: str,
                      buttons: tuple[str, ...] | None = None,
                      firmware: str | None = None) -> dict[str, dict]:
    """Discovery topic -> config, one entity each, all under one device.

    Button entities are left out when `buttons` is None (caps unknown), which
    leaves whatever an earlier run retained for them in place.
    """
    nid = satellite["id"]
    root = f"{base}/{nid}"
    state = f"{root}/state"
    bridge = {"topic": f"{base}/bridge/availability"}
    device = {"identifiers": [nid], "name": satellite.get("name") or f"satellite-{nid[-4:]}"}
    if satellite.get("model"):
        device["model"] = satellite["model"]
    if firmware:
        device["sw_version"] = firmware

    # Settings live in the hub and reach the satellite at its next welcome, so
    # they stay usable while the satellite is away and only go when the hub
    # does. What the satellite itself reports goes unavailable with the
    # satellite as well.
    hub_only = {"availability": [bridge]}
    live = {"availability": [bridge, {"topic": f"{root}/availability"}],
            "availability_mode": "all"}

    entities: list[tuple[str, str, dict]] = [
        ("sensor", "rssi", {
            "name": "Wi-Fi signal", "state_topic": state,
            "value_template": "{{ value_json.rssi }}",
            "device_class": "signal_strength", "unit_of_measurement": "dBm",
            "state_class": "measurement", "entity_category": "diagnostic", **live}),
        ("binary_sensor", "online", {
            # The satellite's own availability topic, read as a state: an
            # offline satellite is "off" here, not "unavailable", so it can be
            # alerted on.
            "name": "Online", "state_topic": f"{root}/availability",
            "payload_on": "online", "payload_off": "offline",
            "device_class": "connectivity", "entity_category": "diagnostic", **hub_only}),
    ]
    for key, (object_id, name, icon) in SWITCHES.items():
        entities.append(("switch", object_id, {
            "name": name, "icon": icon, "state_topic": state,
            "value_template": f"{{{{ 'ON' if value_json.{key} else 'OFF' }}}}",
            "command_topic": f"{root}/set/{key}",
            "payload_on": "ON", "payload_off": "OFF", **hub_only}))
    entities += [
        ("number", "volume", {
            "name": "Volume", "icon": "mdi:volume-high", "state_topic": state,
            "value_template": "{{ value_json.volume }}",
            "command_topic": f"{root}/set/volume",
            "min": 0, "max": 100, "step": 1, "mode": "slider",
            "unit_of_measurement": "%", **hub_only}),
        ("sensor", "last_wake_word", {
            "name": "Last wake word", "icon": "mdi:account-voice",
            "state_topic": f"{root}/wake_word", **hub_only}),
        ("event", "wake_word", {
            "name": "Wake word", "icon": "mdi:account-voice",
            "state_topic": f"{root}/wake", "event_types": ["detected"], **live}),
    ]
    for b in buttons or ():
        label = BUTTON_LABELS.get(b, b.replace("_", " ").capitalize())
        entities.append(("event", f"button_{b}", {
            "name": f"{label} button", "state_topic": f"{root}/button/{b}",
            "device_class": "button", "event_types": ["press", "release"], **live}))

    return {
        f"{prefix}/{component}/calliope_{nid}/{object_id}/config": {
            "unique_id": f"calliope_{nid}_{object_id}", **cfg,
            "device": device, "origin": ORIGIN, "qos": QOS,
        }
        for component, object_id, cfg in entities
    }


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


class MqttBridge:
    """Publishes adopted satellites to Home Assistant and hands its commands
    back.

    publish_satellite() and publish_event() never wait: they note what changed
    and a background task sends it, so a slow or absent broker costs the hub
    nothing. Several changes to one satellite before the task runs are sent
    once, as the latest.
    """

    def __init__(self, url: str | None, prefix: str = "homeassistant",
                 base: str = "calliope/satellites", *, on_command: OnCommand | None = None,
                 client_factory: Callable[..., Any] | None = None) -> None:
        self.prefix = prefix.strip("/")
        self.base = base.strip("/")
        self.on_command = on_command
        self.error: str | None = None
        self.connected = False
        self._broker: dict | None = None
        if url:
            try:
                for what, topic in (("discovery prefix", self.prefix), ("base topic", self.base)):
                    if not topic or "+" in topic or "#" in topic:
                        raise ValueError(f"the {what} {topic!r} must be a topic with no wildcards")
                self._broker = parse_url(url)
            except ValueError as e:
                # Off, not fatal: a typo in an optional integration must not be
                # the reason the hub, and every satellite with it, does not
                # start.
                self.error = str(e)
                log.error("MQTT is off: %s", e)
        self.enabled = self._broker is not None
        self._factory = client_factory
        self._hub: Any = None
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._wake = asyncio.Event()
        self._dirty: dict[str, dict] = {}  # satellite id -> latest describe()
        self._events: deque[tuple[str, str, bytes]] = deque(maxlen=64)
        self._sent: dict[str, bytes] = {}  # retained topic -> payload, this connection
        self._published: set[str] = set()  # satellite ids that may have discovery on the broker
        self._buttons: dict[str, tuple[str, ...]] = {}
        self._firmware: dict[str, str] = {}
        self._last_wake: dict[str, str] = {}

    @classmethod
    def from_env(cls, on_command: OnCommand | None = None) -> MqttBridge:
        return cls(os.environ.get("SATELLITES_MQTT_URL") or None,
                   os.environ.get("SATELLITES_MQTT_PREFIX") or "homeassistant",
                   os.environ.get("SATELLITES_MQTT_BASE") or "calliope/satellites",
                   on_command=on_command)

    def health(self) -> dict:
        b = self._broker
        return {"enabled": self.enabled, "connected": self.connected,
                "broker": f"{b['hostname']}:{b['port']}" if b else None, "error": self.error}

    @property
    def _bridge_topic(self) -> str:
        return f"{self.base}/bridge/availability"

    # ---- the hub's side ------------------------------------------------------

    async def start(self, hub: Any, on_command: OnCommand | None = None) -> None:
        if on_command is not None:
            self.on_command = on_command
        if not self.enabled or self._task is not None:
            return
        self._hub = hub
        # Made here, not in __init__: an Event is bound to the first loop that
        # waits on it, and a bridge built at import time may outlive a loop.
        self._wake = asyncio.Event()
        self._wake.set()
        # Every adopted satellite may have discovery on the broker from an
        # earlier run. Counting it as published is what lets a satellite
        # forgotten before the first connection still be cleared by that
        # connection.
        self._published = set(hub.store.satellites)
        self._task = asyncio.create_task(self._run(), name="mqtt-bridge")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        client = self._client
        if client is not None and self.connected:
            # A clean DISCONNECT tells the broker not to send the last will,
            # so the bridge has to say it is going itself.
            try:
                await asyncio.wait_for(
                    client.publish(self._bridge_topic, b"offline", qos=QOS, retain=True), 2)
            except Exception as e:
                log.debug("MQTT: could not mark the bridge offline: %s", e)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def publish_satellite(self, satellite: dict) -> None:
        """Queue a satellite, as Hub.describe() returns it. An adopted
        satellite is published or updated; one that is no longer adopted is
        removed if it was published, and otherwise publishes nothing."""
        if not self.enabled or not satellite.get("id"):
            return
        self._dirty[satellite["id"]] = satellite
        self._wake.set()

    def publish_event(self, event: dict) -> None:
        """Take any Hub.publish() event. Buttons and wake words become Home
        Assistant events; status, online, offline and pending refresh the
        satellite; the rest are ignored. So the hub may forward every event."""
        if not self.enabled:
            return
        kind, nid = event.get("type"), event.get("satellite")
        if not isinstance(nid, str) or not NID.fullmatch(nid):
            return
        root = f"{self.base}/{nid}"
        if kind in ("status", "online", "offline", "pending"):
            self._refresh(nid)
        elif kind == "button":
            button, action = event.get("button"), event.get("action")
            if (isinstance(button, str) and BUTTON.fullmatch(button)
                    and action in ("press", "release")):
                payload: dict = {"event_type": action}
                if event.get("held_ms") is not None:
                    payload["held_ms"] = event["held_ms"]
                self._event(f"{root}/button/{button}", nid, payload)
        elif kind in ("wake", "wake_word"):  # main.py has not named this event yet
            word = event.get("wake_word")
            if isinstance(word, str) and word:
                payload = {"event_type": "detected", "wake_word": word}
                if isinstance(event.get("score"), (int, float)):
                    payload["score"] = event["score"]
                self._event(f"{root}/wake", nid, payload)
                self._last_wake[nid] = word
                self._refresh(nid)

    def _refresh(self, nid: str) -> None:
        if self._hub is None:
            return
        try:
            self.publish_satellite(self._hub.describe(nid))
        except Exception:
            # Called from inside the hub's own event path: a fault here must
            # cost Home Assistant one update, not a satellite its connection.
            log.exception("MQTT: could not describe satellite %s", nid)

    def _event(self, topic: str, nid: str, payload: dict) -> None:
        # Dropped rather than queued while disconnected: see the module notes.
        if self.connected:
            self._events.append((topic, nid, _json(payload)))
            self._wake.set()

    # ---- the broker's side ---------------------------------------------------

    async def _run(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            self.error = "aiomqtt is not installed"
            log.error("SATELLITES_MQTT_URL is set but aiomqtt is not installed; MQTT is off")
            return
        factory = self._factory or aiomqtt.Client
        b = self._broker
        assert b is not None
        delay = BACKOFF_MIN_S
        while True:
            began = time.monotonic()
            try:
                client = factory(
                    hostname=b["hostname"], port=b["port"],
                    username=b["username"], password=b["password"],
                    will=aiomqtt.Will(self._bridge_topic, b"offline", qos=QOS, retain=True),
                    # The system CA store; SSL_CERT_FILE points it at a
                    # private CA without a code change.
                    tls_context=ssl.create_default_context() if b["tls"] else None,
                    keepalive=30, timeout=10)
                async with client:
                    self._client = client
                    await self._session(client)
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError) as e:
                self.error = str(e) or type(e).__name__
            except Exception as e:
                # Not a network fault, so a bug: logged in full, and still
                # retried, because the hub must outlive its MQTT bridge.
                self.error = f"{type(e).__name__}: {e}"
                log.exception("MQTT bridge failed")
            finally:
                self._client = None
                self.connected = False
                self._events.clear()
            if time.monotonic() - began >= STABLE_S:
                delay = BACKOFF_MIN_S
            log.warning("MQTT %s:%d: %s; retrying in %gs",
                        b["hostname"], b["port"], self.error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def _session(self, client: Any) -> None:
        await client.subscribe(f"{self.base}/+/set/+", qos=QOS)
        await client.subscribe(f"{self.prefix}/status", qos=QOS)
        await client.publish(self._bridge_topic, b"online", qos=QOS, retain=True)
        self._sent.clear()
        self.connected, self.error = True, None
        log.info("MQTT connected to %s:%d", self._broker["hostname"], self._broker["port"])
        self._resync()
        tasks = [asyncio.create_task(self._pump(client)), asyncio.create_task(self._listen(client))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for t in done:
            if t.exception() is not None:
                raise t.exception()
        raise ConnectionError("the broker's message stream ended")

    def _resync(self) -> None:
        """Everything again: after a reconnect, or when Home Assistant restarts."""
        for nid in set(self._hub.store.satellites) | self._published:
            self._dirty[nid] = self._hub.describe(nid)
        self._wake.set()

    async def _pump(self, client: Any) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._events:  # first: a press is worth more fresh than state is
                topic, nid, payload = self._events.popleft()
                if nid in self._hub.store.satellites:
                    await client.publish(topic, payload, qos=QOS, retain=False)
            while self._dirty:
                nid = next(iter(self._dirty))
                satellite = self._dirty.pop(nid)
                try:
                    await self._sync(client, satellite)
                except BaseException:
                    # Kept for the next connection, unless something newer came in.
                    self._dirty.setdefault(nid, satellite)
                    raise

    async def _sync(self, client: Any, satellite: dict) -> None:
        nid = satellite["id"]
        if not NID.fullmatch(nid):
            return
        if not satellite.get("adopted"):
            if nid in self._published:
                await self._clear(client, nid)
            return
        caps_buttons = (satellite.get("caps") or {}).get("buttons")
        if isinstance(caps_buttons, list):
            self._buttons[nid] = tuple(b for b in caps_buttons
                                       if isinstance(b, str) and BUTTON.fullmatch(b))
        if satellite.get("firmware"):
            self._firmware[nid] = satellite["firmware"]
        root = f"{self.base}/{nid}"
        wanted = {topic: _json(cfg) for topic, cfg in discovery_configs(
            satellite, prefix=self.prefix, base=self.base,
            buttons=self._buttons.get(nid), firmware=self._firmware.get(nid)).items()}
        wanted[f"{root}/availability"] = b"online" if satellite.get("online") else b"offline"
        wanted[f"{root}/state"] = _json(satellite_state(satellite))
        if nid in self._last_wake:
            wanted[f"{root}/wake_word"] = self._last_wake[nid].encode()
        self._published.add(nid)
        for topic, payload in wanted.items():
            # A status arrives every 10 s per satellite; only what changed is
            # sent.
            if self._sent.get(topic) != payload:
                await client.publish(topic, payload, qos=QOS, retain=True)
                self._sent[topic] = payload

    async def _clear(self, client: Any, nid: str) -> None:
        buttons = tuple(sorted(set(self._buttons.get(nid, ())) | set(KNOWN_BUTTONS)))
        root = f"{self.base}/{nid}"
        topics = list(discovery_configs({"id": nid}, prefix=self.prefix, base=self.base,
                                        buttons=buttons))
        topics += [f"{root}/availability", f"{root}/state", f"{root}/wake_word"]
        for topic in topics:
            await client.publish(topic, b"", qos=QOS, retain=True)
            self._sent.pop(topic, None)
        self._published.discard(nid)
        for memo in (self._buttons, self._firmware, self._last_wake):
            memo.pop(nid, None)
        log.info("MQTT: satellite %s is no longer adopted; its Home Assistant device is removed",
                 nid)

    async def _listen(self, client: Any) -> None:
        own = f"{self.base}/"
        async for message in client.messages:
            topic = str(message.topic)
            raw = message.payload
            payload = (raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray))
                       else str(raw or ""))
            if topic == f"{self.prefix}/status":
                # Home Assistant's birth message. It may have lost what it knew,
                # so it gets everything again, as its MQTT docs recommend.
                if payload == "online":
                    self._sent.clear()
                    self._resync()
            elif topic.startswith(own):
                parts = topic[len(own):].split("/")
                if len(parts) == 3 and parts[1] == "set":
                    await self._command(parts[0], parts[2], payload, bool(message.retain))

    async def _command(self, nid: str, field: str, payload: str, retained: bool) -> None:
        if retained:
            # Home Assistant never retains a command. A retained one would be
            # replayed at every reconnect, switching a microphone back on long
            # after anyone asked, so it is not obeyed.
            log.warning("MQTT: ignoring a retained command on %s/%s/set/%s", self.base, nid, field)
            return
        if not NID.fullmatch(nid) or nid not in self._hub.store.satellites:
            log.info("MQTT: command for %s, which is not an adopted satellite; ignored", nid)
            return
        change = parse_command(field, payload)
        if change is None:
            log.warning("MQTT: ignoring %r on %s/%s/set/%s", payload[:40], self.base, nid, field)
            return
        if self.on_command is None:
            log.warning("MQTT: no on_command handler, so %s for satellite %s goes nowhere",
                        change, nid)
            return
        try:
            result = self.on_command(nid, change)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, COMMAND_TIMEOUT_S)
        except Exception as e:
            log.warning("MQTT: %s for satellite %s failed: %s", change, nid, e)
        # Whatever the hub now holds, success or not: the entities are not
        # optimistic, so Home Assistant waits for the state topic to move.
        self._refresh(nid)
