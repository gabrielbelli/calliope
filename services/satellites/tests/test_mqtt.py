"""The MQTT bridge against a fake broker and the real Hub.

No broker runs and nothing leaves the process: client_factory hands the bridge
a fake client that records what it publishes and replays what a test sends it.
The Hub and Store are the real ones, so the satellite dicts are exactly what
Hub.describe() returns; the sessions are stand-ins that record anything sent
to the device, and every test that looks expects nothing to have been.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from types import SimpleNamespace

import aiomqtt
import pytest

from app import mqtt
from app.main import Hub
from app.store import Store

NID = "94b97e7b8be8"
MODEL = "esp32-korvo-v1.1"
BUTTONS = ["vol_up", "vol_down", "set", "play", "mode", "rec"]
URL = "mqtt://hub:s3cret@broker.test:1883"  # .test never resolves; the fake never dials
ROOT = f"calliope/satellites/{NID}"


# ---- a broker in a list ------------------------------------------------------


class FakeBroker:
    def __init__(self) -> None:
        self.log: list[tuple[str, bytes, int, bool]] = []  # every publish, in order
        self.retained: dict[str, bytes] = {}  # what a subscriber arriving now would get
        self.clients: list[FakeClient] = []
        self.refuse = 0  # connection attempts to refuse before accepting

    def factory(self, **kwargs) -> FakeClient:
        client = FakeClient(self, kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakeClient:
        return self.clients[-1]

    def send(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self.client.inbox.put_nowait(aiomqtt.Message(topic, payload, 1, retain, 0, None))

    def drop(self) -> None:
        self.client.inbox.put_nowait(aiomqtt.MqttError("Disconnected during message iteration"))

    def published(self, topic: str) -> list[bytes]:
        return [p for t, p, _, _ in self.log if t == topic]

    def satellite_topics(self) -> dict[str, bytes]:
        return {t: p for t, p in self.retained.items() if NID in t}


class FakeClient:
    def __init__(self, broker: FakeBroker, kwargs: dict) -> None:
        self.broker, self.kwargs = broker, kwargs
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.subscriptions: list[str] = []

    async def __aenter__(self) -> FakeClient:
        if self.broker.refuse:
            self.broker.refuse -= 1
            raise aiomqtt.MqttError("[code:5] Connection refused")
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append(topic)

    async def publish(self, topic: str, payload=None, qos: int = 0, retain: bool = False) -> None:
        payload = payload or b""
        self.broker.log.append((topic, payload, qos, retain))
        if retain:
            if payload:
                self.broker.retained[topic] = payload
            else:
                self.broker.retained.pop(topic, None)

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        while True:
            item = await self.inbox.get()
            if isinstance(item, Exception):
                raise item
            yield item


# ---- fixtures ----------------------------------------------------------------


async def until(check, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the bridge")
        await asyncio.sleep(0.005)


def connect(hub: Hub, *, rssi: int = -61, caps: dict | None = None) -> SimpleNamespace:
    """A satellite session as far as Hub.describe() looks, which records
    anything sent to the device."""
    s = SimpleNamespace(
        id=NID, model=MODEL, fw="v0.3.0", address="192.0.2.10", connected_at=0.0,
        status={"rssi": rssi, "volume": 60}, ota=None, hello={},
        caps={"buttons": BUTTONS} if caps is None else caps,
        adopted=NID in hub.store.satellites, sent=[])

    async def send(obj) -> None:
        s.sent.append(obj)

    s.send_json = s.send_bytes = send
    hub.sessions[NID] = s
    return s


@pytest.fixture
def hub(tmp_path) -> Hub:
    return Hub(Store(tmp_path))


@pytest.fixture
def broker(monkeypatch) -> FakeBroker:
    monkeypatch.setattr(mqtt, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(mqtt, "BACKOFF_MAX_S", 0.04)
    return FakeBroker()


@pytest.fixture
async def bridge(hub, broker):
    calls: list[tuple[str, dict]] = []

    async def on_command(nid: str, change: dict) -> None:
        # What PATCH /satellites/{id} does to the record; the real one also
        # tells the satellite.
        calls.append((nid, change))
        hub.store.satellites[nid].config.update(change)

    b = mqtt.MqttBridge(URL, on_command=on_command, client_factory=broker.factory)
    b.calls = calls
    yield b
    await b.stop()


async def adopted_and_published(hub, broker, bridge, **kw) -> SimpleNamespace:
    hub.store.adopt(NID, "kitchen", MODEL)
    s = connect(hub, **kw)
    await bridge.start(hub)
    await until(lambda: f"{ROOT}/state" in broker.retained)
    return s


def discovery(broker: FakeBroker) -> dict[str, dict]:
    return {t: json.loads(p) for t, p in broker.retained.items()
            if t.startswith("homeassistant/") and f"/calliope_{NID}/" in t}


# ---- off -----------------------------------------------------------------------


async def test_a_bridge_with_no_url_is_a_no_op_that_never_builds_a_client(hub):
    def boom(**kwargs):
        raise AssertionError("a disabled bridge built an MQTT client")

    hub.store.adopt(NID, "kitchen", MODEL)
    b = mqtt.MqttBridge(None, client_factory=boom)
    await b.start(hub)
    b.publish_satellite(hub.describe(NID))
    b.publish_event({"type": "button", "satellite": NID, "button": "play", "action": "press"})
    await asyncio.sleep(0.02)
    await b.stop()
    assert b.enabled is False
    assert b.health() == {"enabled": False, "connected": False, "broker": None, "error": None}


def test_a_bad_url_turns_mqtt_off_without_putting_the_password_in_the_log(caplog):
    with caplog.at_level(logging.ERROR, logger="voice-satellites.mqtt"):
        b = mqtt.MqttBridge("http://hub:s3cret@broker.test:1883")
    assert b.enabled is False
    assert "mqtt:// or mqtts://" in b.error
    assert "s3cret" not in caplog.text and "s3cret" not in b.error
    assert mqtt.MqttBridge(URL, base="calliope/#").enabled is False


def test_the_url_gives_host_port_decoded_credentials_and_tls():
    assert mqtt.parse_url("mqtt://hub:p%40ss%3Aword@broker.test:1884") == {
        "hostname": "broker.test", "port": 1884, "username": "hub",
        "password": "p@ss:word", "tls": False}
    assert mqtt.parse_url("mqtts://broker.test")["port"] == 8883
    assert mqtt.parse_url("mqtt://broker.test")["username"] is None
    for bad in ("mqtt://", "mqtt://broker.test:99999", "mqtt://broker.test/topic"):
        with pytest.raises(ValueError):
            mqtt.parse_url(bad)


async def test_mqtts_connects_with_tls_and_leaves_a_last_will(hub, broker):
    b = mqtt.MqttBridge("mqtts://hub:s3cret@broker.test", client_factory=broker.factory)
    await b.start(hub)
    await until(lambda: b.connected)
    kw = broker.client.kwargs
    assert (kw["hostname"], kw["port"], kw["username"], kw["password"]) == (
        "broker.test", 8883, "hub", "s3cret")
    assert isinstance(kw["tls_context"], ssl.SSLContext)
    assert (kw["will"].topic, kw["will"].payload, kw["will"].retain) == (
        "calliope/satellites/bridge/availability", b"offline", True)
    assert broker.retained["calliope/satellites/bridge/availability"] == b"online"
    await b.stop()
    # A clean disconnect suppresses the will, so stop() has to say it.
    assert broker.retained["calliope/satellites/bridge/availability"] == b"offline"


# ---- discovery -------------------------------------------------------------------


async def test_an_adopted_satellite_is_one_device_with_every_entity_retained(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    configs = discovery(broker)
    kinds = sorted(t.removeprefix("homeassistant/").split("/")[0] + "/" + t.split("/")[3]
                   for t in configs)
    assert kinds == sorted(
        ["sensor/rssi", "binary_sensor/online", "switch/microphone", "switch/speaker",
         "switch/lights", "number/volume", "sensor/last_wake_word", "event/wake_word"]
        + [f"event/button_{b}" for b in BUTTONS])
    for topic, cfg in configs.items():
        assert cfg["device"] == {"identifiers": [NID], "name": "kitchen", "model": MODEL,
                                 "sw_version": "v0.3.0"}, topic
        assert cfg["availability"][0] == {"topic": "calliope/satellites/bridge/availability"}
    assert len({c["unique_id"] for c in configs.values()}) == len(configs)
    assert all(retain and qos == 1 for t, _, qos, retain in broker.log if t in configs)


async def test_discovery_payloads_have_the_shapes_home_assistant_reads(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    c = {t.split("/")[3]: cfg for t, cfg in discovery(broker).items()}
    satellite_avail = {"topic": f"{ROOT}/availability"}

    assert c["rssi"]["device_class"] == "signal_strength"
    assert c["rssi"]["unit_of_measurement"] == "dBm"
    assert satellite_avail in c["rssi"]["availability"] and c["rssi"]["availability_mode"] == "all"

    online = c["online"]
    assert (online["state_topic"], online["payload_on"], online["payload_off"]) == (
        f"{ROOT}/availability", "online", "offline")
    assert online["device_class"] == "connectivity"
    # Bridge availability only: an offline satellite must read "off", not
    # "unavailable".
    assert online["availability"] == [{"topic": "calliope/satellites/bridge/availability"}]

    for object_id, key in (("microphone", "mic_enabled"), ("speaker", "speaker_enabled"),
                           ("lights", "lights_enabled")):
        assert c[object_id]["command_topic"] == f"{ROOT}/set/{key}"
        assert c[object_id]["state_topic"] == f"{ROOT}/state"
        assert f"value_json.{key}" in c[object_id]["value_template"]

    vol = c["volume"]
    assert (vol["min"], vol["max"], vol["step"], vol["command_topic"]) == (
        0, 100, 1, f"{ROOT}/set/volume")

    assert c["button_play"]["event_types"] == ["press", "release"]
    assert c["button_play"]["state_topic"] == f"{ROOT}/button/play"
    assert c["button_vol_up"]["name"] == "Volume up button"
    assert c["wake_word"]["event_types"] == ["detected"]
    assert c["last_wake_word"]["state_topic"] == f"{ROOT}/wake_word"

    assert broker.retained[f"{ROOT}/availability"] == b"online"
    assert json.loads(broker.retained[f"{ROOT}/state"]) == {
        "rssi": -61, "volume": 60, "mic_enabled": True, "speaker_enabled": True,
        "lights_enabled": True}


async def test_an_unadopted_satellite_publishes_nothing(hub, broker, bridge):
    s = connect(hub)
    await bridge.start(hub)
    await until(lambda: bridge.connected)
    bridge.publish_satellite(hub.describe(NID))
    bridge.publish_event({"type": "pending", "satellite": NID, "address": s.address})
    bridge.publish_event({"type": "button", "satellite": NID, "button": "play", "action": "press"})
    await asyncio.sleep(0.05)
    assert [t for t, *_ in broker.log if NID in t] == []


async def test_a_status_heartbeat_republishes_state_but_not_discovery(hub, broker, bridge):
    s = await adopted_and_published(hub, broker, bridge)
    before = len(broker.log)
    s.status = {"rssi": -70}
    bridge.publish_event({"type": "status", "satellite": NID, "status": s.status})
    await until(lambda: json.loads(broker.retained[f"{ROOT}/state"])["rssi"] == -70)
    assert [t for t, *_ in broker.log[before:]] == [f"{ROOT}/state"]


async def test_an_offline_satellite_keeps_its_device_and_reads_offline(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    configs = discovery(broker)
    del hub.sessions[NID]
    bridge.publish_event({"type": "offline", "satellite": NID})
    await until(lambda: broker.retained[f"{ROOT}/availability"] == b"offline")
    # No caps while offline, yet the button entities must not be removed, and
    # the firmware seen earlier must not be dropped from the device.
    assert discovery(broker) == configs


async def test_home_assistant_coming_back_gets_every_config_again(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    count = len(discovery(broker))
    before = len(broker.log)
    broker.send("homeassistant/status", b"online")
    await until(lambda: sum(t in discovery(broker) for t, *_ in broker.log[before:]) == count)


# ---- forgetting --------------------------------------------------------------------


async def test_a_forgotten_satellite_clears_every_config_it_published(hub, broker, bridge):
    s = await adopted_and_published(hub, broker, bridge)
    bridge.publish_event({"type": "wake", "satellite": NID, "wake_word": "hey_jarvis"})
    await until(lambda: f"{ROOT}/wake_word" in broker.retained)
    published = set(broker.satellite_topics())
    hub.store.forget(NID)
    s.adopted = False
    bridge.publish_satellite(hub.describe(NID))
    await until(lambda: not broker.satellite_topics())
    for topic in published:
        assert broker.published(topic)[-1] == b"", topic


async def test_a_satellite_forgotten_while_the_broker_was_down_is_cleared_on_reconnect(
        hub, broker, bridge):
    hub.store.adopt(NID, "kitchen", MODEL)
    broker.retained.update({  # left by an earlier run of the hub
        f"homeassistant/switch/calliope_{NID}/lights/config": b"{}",
        f"homeassistant/event/calliope_{NID}/button_play/config": b"{}",
        f"{ROOT}/availability": b"online"})
    broker.refuse = 3
    await bridge.start(hub)
    hub.store.forget(NID)
    bridge.publish_satellite(hub.describe(NID))
    await until(lambda: bridge.connected)
    await until(lambda: not broker.satellite_topics())


# ---- commands ------------------------------------------------------------------------


async def test_switch_and_volume_commands_reach_on_command_as_patch_changes(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    for field, payload in (("mic_enabled", b"OFF"), ("speaker_enabled", b"OFF"),
                           ("lights_enabled", b"OFF"), ("volume", b"35"), ("volume", b"72.6")):
        broker.send(f"{ROOT}/set/{field}", payload)
    await until(lambda: len(bridge.calls) == 5)
    assert bridge.calls == [(NID, {"mic_enabled": False}), (NID, {"speaker_enabled": False}),
                            (NID, {"lights_enabled": False}), (NID, {"volume": 35}),
                            (NID, {"volume": 73})]
    # The entities are not optimistic: the result has to come back as state.
    await until(lambda: json.loads(broker.retained[f"{ROOT}/state"]) == {
        "rssi": -61, "volume": 73, "mic_enabled": False, "speaker_enabled": False,
        "lights_enabled": False})


async def test_the_lights_switch_changes_only_lights_enabled_and_sends_the_satellite_nothing(
        hub, broker, bridge):
    s = await adopted_and_published(hub, broker, bridge)
    hub.store.satellites[NID].config["lights_enabled"] = False
    broker.send(f"{ROOT}/set/lights_enabled", b"ON")
    broker.send(f"{ROOT}/set/lights_enabled", b"OFF")
    await until(lambda: len(bridge.calls) == 2)
    assert bridge.calls == [(NID, {"lights_enabled": True}), (NID, {"lights_enabled": False})]
    assert s.sent == []  # no pattern, no config: only on_command may talk to the satellite


async def test_commands_that_are_not_ours_to_obey_are_ignored(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    other = "aabbccddeeff"  # seen by the hub, never adopted
    for topic, payload, retain in (
            (f"{ROOT}/set/mic_enabled", b"maybe", False),
            (f"{ROOT}/set/mic_enabled", b"on", False),
            (f"{ROOT}/set/volume", b"loud", False),
            (f"{ROOT}/set/volume", b"150", False),
            (f"{ROOT}/set/volume", b"nan", False),
            (f"{ROOT}/set/mic_gain_db", b"37.5", False),  # no entity, so no command
            (f"{ROOT}/set/name", b"attic", False),
            (f"{ROOT}/set/mic_enabled", b"ON", True),  # a replayed retained command
            (f"calliope/satellites/{other}/set/mic_enabled", b"OFF", False),  # not adopted
            ("calliope/satellites/bridge/set/mic_enabled", b"OFF", False),
            (f"{ROOT}/button/set", b'{"event_type":"press"}', False)):
        broker.send(topic, payload, retain)
    broker.send(f"{ROOT}/set/volume", b"20")  # the one that counts, sent last
    await until(lambda: bridge.calls)
    await asyncio.sleep(0.02)
    assert bridge.calls == [(NID, {"volume": 20})]


async def test_a_failing_command_does_not_stop_the_bridge(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    seen = []

    async def on_command(nid, change):
        seen.append(change)
        if len(seen) == 1:
            raise RuntimeError("satellite 94b97e7b8be8 is not connected")

    bridge.on_command = on_command
    broker.send(f"{ROOT}/set/mic_enabled", b"OFF")
    broker.send(f"{ROOT}/set/mic_enabled", b"ON")
    await until(lambda: len(seen) == 2)
    assert bridge.connected and len(broker.clients) == 1


# ---- events -----------------------------------------------------------------------------


async def test_buttons_and_wake_words_become_home_assistant_events(hub, broker, bridge):
    await adopted_and_published(hub, broker, bridge)
    bridge.publish_event({"at": 1.0, "type": "button", "satellite": NID, "button": "play",
                          "action": "press", "held_ms": None})
    bridge.publish_event({"at": 1.2, "type": "button", "satellite": NID, "button": "play",
                          "action": "release", "held_ms": 180})
    bridge.publish_event({"type": "wake", "satellite": NID, "wake_word": "hey_jarvis", "score": 0.93})
    await until(lambda: broker.published(f"{ROOT}/wake") and f"{ROOT}/wake_word" in broker.retained)
    presses = [(json.loads(p), retain) for t, p, _, retain in broker.log
               if t == f"{ROOT}/button/play"]
    assert presses == [({"event_type": "press"}, False),
                       ({"event_type": "release", "held_ms": 180}, False)]
    assert json.loads(broker.published(f"{ROOT}/wake")[0]) == {
        "event_type": "detected", "wake_word": "hey_jarvis", "score": 0.93}
    assert broker.retained[f"{ROOT}/wake_word"] == b"hey_jarvis"
    assert f"{ROOT}/wake" not in broker.retained  # an event, never replayed to a new subscriber


async def test_a_press_while_the_broker_is_away_is_dropped_not_replayed_late(
        hub, broker, bridge, monkeypatch):
    monkeypatch.setattr(mqtt, "BACKOFF_MIN_S", 0.1)
    hub.store.adopt(NID, "kitchen", MODEL)
    connect(hub)
    broker.refuse = 1
    await bridge.start(hub)
    await until(lambda: len(broker.clients) == 1)  # refused; now waiting to retry
    bridge.publish_event({"type": "button", "satellite": NID, "button": "play", "action": "press"})
    await until(lambda: f"{ROOT}/state" in broker.retained)
    await asyncio.sleep(0.02)
    assert broker.published(f"{ROOT}/button/play") == []


# ---- the connection --------------------------------------------------------------------------


async def test_a_lost_connection_is_retried_with_backoff_and_everything_resent(
        hub, broker, bridge, caplog):
    await adopted_and_published(hub, broker, bridge)
    count = len(discovery(broker))
    broker.refuse = 3
    before = len(broker.log)
    with caplog.at_level(logging.WARNING, logger="voice-satellites.mqtt"):
        broker.drop()
        await until(lambda: len(broker.clients) == 5 and bridge.connected)
        await until(lambda: sum(t in discovery(broker) for t, *_ in broker.log[before:]) == count)
    delays = [r.getMessage().rsplit("retrying in ", 1)[1] for r in caplog.records
              if "retrying in" in r.getMessage()]
    assert delays == ["0.01s", "0.02s", "0.04s", "0.04s"]  # doubling, then capped
    assert "s3cret" not in caplog.text


async def test_a_bug_in_the_bridge_is_logged_and_survived_not_raised_into_the_hub(
        hub, broker, bridge, caplog):
    hub.store.adopt(NID, "kitchen", MODEL)
    connect(hub)
    real = hub.describe
    calls = 0

    def flaky(nid):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyError("first describe fails")
        return real(nid)

    hub.describe = flaky
    with caplog.at_level(logging.ERROR, logger="voice-satellites.mqtt"):
        await bridge.start(hub)
        await until(lambda: f"{ROOT}/state" in broker.retained)
    assert "MQTT bridge failed" in caplog.text
