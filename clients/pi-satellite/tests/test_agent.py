"""The agent against a fake hub on a real WebSocket: hello and caps, adoption,
settings and the one volume, speaker and media audio, earcons, AirPlay's
controls and cover, and an update from first chunk to restart. PipeWire,
Shairport Sync and calliope-root are fakes; nothing plays and nothing is
installed."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import time

import build_bundle
import pytest
from websockets.asyncio.server import serve

from calliope_pi import agent as agentmod
from calliope_pi import airplay, bundle, pipewire, state

SINK = pipewire.Device("alsa_output.usb", "USB Audio", "alsa", 41)
BUILTIN = pipewire.Device("alsa_output.builtin", "Built-in Audio", "alsa", 40)
MIC = pipewire.Device("alsa_input.usb", "USB Mic", "alsa", 42)


class FakePlayer:
    def __init__(self):
        self.writes, self.flushes, self.once, self.target, self.dropped = [], 0, [], None, 0

    async def write(self, pcm):
        self.writes.append(pcm)

    async def flush(self):
        self.flushes += 1

    async def play_once(self, pcm, rate):
        self.once.append((len(pcm), rate))

    def buffered_ms(self):
        return 0


@pytest.fixture
def fake(monkeypatch):
    # `volumes`: each output's own volume as wpctl reads it back, by name
    # (None: the default output); what set_volume sets, and what a test sets
    # for the phone's slider. `gate`: while set, a reading waits for it (a
    # slow wpctl), with `reading` set once it has the value it will return.
    world = {"devices": pipewire.Devices((BUILTIN, SINK), (), BUILTIN.name, None), "calls": [], "root": [],
             "volumes": {}, "gate": None, "reading": None}

    async def devices():
        return world["devices"]

    async def set_default(dev):
        world["calls"].append(("default", dev.name))
        return True

    async def set_volume(dev, fraction):
        world["calls"].append(("volume", dev.name if dev else None, round(fraction, 2)))
        world["volumes"][dev.name if dev else None] = fraction
        return True

    async def set_source_volume(dev, fraction):
        world["calls"].append(("mic", dev.name if dev else None, round(fraction, 2)))
        return True

    async def get_volume(dev):
        now = world["volumes"].get(dev.name if dev else None)
        if world["gate"] is not None:
            world["reading"].set()
            await world["gate"].wait()
        return now

    monkeypatch.setattr(pipewire, "devices", devices)
    monkeypatch.setattr(pipewire, "set_default", set_default)
    monkeypatch.setattr(pipewire, "set_volume", set_volume)
    monkeypatch.setattr(pipewire, "set_source_volume", set_source_volume)
    monkeypatch.setattr(pipewire, "get_volume", get_volume)
    monkeypatch.setattr(agentmod.system, "satellite_id", lambda: "b827eb123456")
    return world


@pytest.fixture
def phone(monkeypatch, tmp_path):
    """Shairport Sync, faked where the agent asks it: running, what MPRIS
    says (`player`, `metadata`), whether the phone answers its remote control
    (`available`), and what each command does (`answer`, `then`)."""
    world = {"player": "Playing", "metadata": {"trackid": "/org/gnome/ShairportSync/mper_1"},
             "available": True, "answer": (True, 204, None), "then": {}, "commands": [], "metadata_reads": 0,
             "gate": None}   # while set, a command waits for it before the phone answers

    async def mpris_status():
        return world["player"]

    async def mpris_metadata():
        world["metadata_reads"] += 1
        return dict(world["metadata"])

    async def remote_available():
        return world["available"]

    async def remote_command(command):
        world["commands"].append(command)
        if world["gate"] is not None:
            await world["gate"].wait()
        world["player"] = world["then"].get("player", world["player"])
        world["metadata"] |= world["then"].get("metadata", {})
        return world["answer"]

    async def sink_inputs():
        return []

    async def nothing(*args, **kw):
        return None

    async def running(self):
        return True
    monkeypatch.setattr(airplay, "available", lambda: True)
    monkeypatch.setattr(airplay, "mpris_status", mpris_status)
    monkeypatch.setattr(airplay, "mpris_metadata", mpris_metadata)
    monkeypatch.setattr(airplay, "remote_available", remote_available)
    monkeypatch.setattr(airplay, "remote_command", remote_command)
    monkeypatch.setattr(airplay, "sink_inputs", sink_inputs)
    monkeypatch.setattr(airplay.AirPlay, "apply", nothing)
    monkeypatch.setattr(airplay.AirPlay, "running", running)
    world["covers"] = tmp_path / "covers"
    return world


async def talk(agent: agentmod.Agent, hub) -> list:
    """Run the agent against `hub(ws, got)` until it returns; what the agent
    sent. An assertion that fails in `hub` fails the test: the server would
    otherwise only log it and close the connection."""
    got: list = []
    done = asyncio.Event()
    failed: list[BaseException] = []

    async def handler(ws):
        try:
            await hub(ws, got)
        except Exception as e:
            failed.append(e)
        finally:
            done.set()
            await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(agent.session(f"ws://127.0.0.1:{port}/satellites/ws"))
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if failed:
        raise failed[0]
    return got


async def recv(ws, got, kind=None):
    while True:
        msg = await asyncio.wait_for(ws.recv(), 5)
        m = json.loads(msg) if isinstance(msg, str) else msg
        got.append(m)
        if kind is None or (isinstance(m, dict) and m.get("type") == kind):
            return m


def agent_with(tmp_path=None, **kw) -> agentmod.Agent:
    a = agentmod.Agent(state.State(**kw))
    a.player, a.media = FakePlayer(), FakePlayer()
    if tmp_path is not None:   # the cover and Shairport Sync's cache, where the test can see them
        a.metadata.art_dir, a.metadata.art_file = tmp_path, tmp_path / "calliope-airplay-cover"
        a.airplay.covers = tmp_path / "covers"
    return a


def of(got, kind) -> list:
    return [m for m in got if isinstance(m, dict) and m.get("type") == kind]


async def roundtrip(ws, got):
    """Everything sent before it has been handled: the agent answers in order."""
    await ws.send(json.dumps({"type": "earcon_list"}))
    await recv(ws, got, "earcons")


async def test_a_new_satellite_says_what_it_is_waits_and_is_adopted(fake):
    a = agent_with(hub="ws://x")

    async def hub(ws, got):
        hello = await recv(ws, got, "hello")
        assert hello["token"] == "" and hello["model"] == "raspberry-pi" and hello["id"] == "b827eb123456"
        await ws.send(json.dumps({"type": "pending"}))
        await ws.send(json.dumps({"type": "adopt", "token": "t0k", "name": "Kitchen speaker"}))
        again = await recv(ws, got, "hello")
        assert again["token"] == "t0k" and again["name"] == "Kitchen speaker"
    got = await talk(a, hub)
    hello = got[0]
    assert hello["caps"]["speaker"] == {"rate": 44100, "channels": 1, "format": "s16le"}
    assert "mic" not in hello["caps"], "a board with no microphone offers none"
    assert hello["caps"]["duck"] is True and hello["caps"]["audio_devices"] is True
    assert [s["name"] for s in hello["audio"]["sinks"]] == ["alsa_output.builtin", "alsa_output.usb"]
    assert state.load().token == "t0k"


async def test_the_socket_upgrade_carries_no_origin_header(fake):
    """The gateway refuses a device socket that names a web origin, since a
    browser always sends one: a page must not open the device door. So the
    Pi's upgrade request sends no Origin at all."""
    a = agent_with(hub="ws://x")

    async def hub(ws, got):
        got.append({"origin": ws.request.headers.get("Origin")})
        await recv(ws, got, "hello")
    got = await talk(a, hub)
    assert got[0] == {"origin": None}


async def test_a_microphone_is_offered_with_the_output_as_its_reference_channel(fake):
    fake["devices"] = pipewire.Devices((SINK,), (MIC,), SINK.name, MIC.name)
    a = agent_with(hub="ws://x")

    async def hub(ws, got):
        await recv(ws, got, "hello")
    got = await talk(a, hub)
    assert got[0]["caps"]["mic"] == {"rate": 16000, "channels": 2, "format": "s16le", "reference": True,
                                     "max_gain_db": 3.5}


async def test_the_welcome_chooses_the_output_and_its_volume_and_confirms_a_new_release(fake, key):
    data = build_bundle.build("v2", epoch=1_700_000_000)
    bundle.install(data, build_bundle.sign(data, key), hook=False)
    a = agent_with(hub="ws://x", token="t0k", name="k")
    a.media.dropped = 3

    async def hub(ws, got):
        hello = await recv(ws, got, "hello")
        assert hello["ota_pending"] is True
        await ws.send(json.dumps({"type": "welcome", "name": "Kitchen", "config": {
            "volume": 40, "audio_sink": "alsa_output.usb", "lights_enabled": True}}))
        await recv(ws, got, "status")
    got = await talk(a, hub)
    assert {"type": "ota", "state": "verified", "version": "v2"} in got
    assert bundle.pending() is None
    assert ("default", "alsa_output.usb") in fake["calls"] and ("volume", "alsa_output.usb", 0.4) in fake["calls"]
    assert a.player.target == a.media.target == "alsa_output.usb", "the music plays where the voice does"
    status = [m for m in got if isinstance(m, dict) and m.get("type") == "status"][-1]
    assert status["volume"] == 40 and status["audio_sink"] == "alsa_output.usb"
    assert (status["media_buffered_ms"], status["media_dropped"]) == (0, 3)
    assert "lights_enabled" not in status


async def test_speaker_frames_play_and_flush_stops_them(fake):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        for seq in (1, 2):
            await ws.send(struct.pack("<BBBBIQ", 2, 0, 1, 0, seq, 0) + b"\x01\x00" * 480)
        await ws.send(json.dumps({"type": "flush"}))
        await ws.send(json.dumps({"type": "earcon_list"}))
        await recv(ws, got, "earcons")
    await talk(a, hub)
    assert len(a.player.writes) == 2 and a.player.flushes == 1


async def test_an_earcon_is_uploaded_in_chunks_checked_and_played(fake):
    a = agent_with(hub="ws://x", token="t", name="k")
    pcm = bytes(range(256)) * 40   # 10240 bytes: two chunks
    digest = hashlib.sha256(pcm).hexdigest()

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "earcon_put", "id": "wake", "size": len(pcm), "sha256": digest}))
        while True:
            m = await recv(ws, got)
            if m.get("type") == "earcon_next":
                off = m["offset"]
                await ws.send(struct.pack("<BBBBI", 4, 0, 0, 0, off) + pcm[off:off + 8192])
            elif m.get("type") == "earcon_stored":
                break
        await ws.send(json.dumps({"type": "earcon", "id": "wake"}))
        await ws.send(json.dumps({"type": "earcon_list"}))
        listed = await recv(ws, got, "earcons")
        assert listed["items"] == [{"id": "wake", "size": len(pcm), "sha256": digest}]
    got = await talk(a, hub)
    assert [m["offset"] for m in got if isinstance(m, dict) and m.get("type") == "earcon_next"] == [0, 8192]
    await asyncio.sleep(0.05)
    assert a.player.once == [(len(pcm), 48000)]


async def run_update(fake, key, monkeypatch, *, signature=None, root_code=0):
    data = build_bundle.build("v9", epoch=1_700_000_000)
    sig = signature if signature is not None else build_bundle.sign(data, key)
    calls = []

    async def fake_root(self, *args, timeout=60):
        calls.append(args)
        return (root_code, json.dumps({"installed": "v9"} if not root_code else {"error": "apt failed"}))
    monkeypatch.setattr(agentmod.Agent, "_root", fake_root)
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "ota", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                  "version": "v9", "signature": sig}))
        while True:
            m = await recv(ws, got)
            if m.get("type") == "ota_next":
                off = m["offset"]
                await ws.send(struct.pack("<BBBBI", 3, 0, 0, 0, off) + data[off:off + 8192])
            elif m.get("type") == "ota" and m["state"] in ("failed", "rebooting"):
                break
        await asyncio.sleep(0.05)
    got = await talk(a, hub)
    return [m for m in got if isinstance(m, dict) and m.get("type") == "ota"], calls


async def test_an_update_arrives_in_chunks_is_verified_and_installed_by_calliope_root(fake, key, monkeypatch):
    ota, calls = await run_update(fake, key, monkeypatch)
    states = [m["state"] for m in ota]
    assert states[0] == "started" and states[-1] == "rebooting" and "progress" in states
    assert calls[0][0] == "install" and calls[0][1].endswith(".bundle") and calls[-1] == ("restart-agent",)


async def test_an_update_with_a_bad_signature_is_refused_before_root_is_asked(fake, key, monkeypatch):
    other = build_bundle.sign(b"something else", key)
    ota, calls = await run_update(fake, key, monkeypatch, signature=other)
    assert ota[-1] == {"type": "ota", "state": "failed", "version": "v9", "error": "bad signature"}
    assert calls == []


async def test_an_install_that_fails_says_why_and_does_not_restart(fake, key, monkeypatch):
    ota, calls = await run_update(fake, key, monkeypatch, root_code=1)
    assert ota[-1]["state"] == "failed" and ota[-1]["error"] == "apt failed"
    assert ("restart-agent",) not in calls


def test_the_hub_address_becomes_the_socket_url():
    assert agentmod.socket_url("wss://calliope.example.com") == "wss://calliope.example.com/satellites/ws"
    assert agentmod.socket_url("wss://h:8443/") == "wss://h:8443/satellites/ws"
    assert agentmod.socket_url("ws://h/nodes/ws") == "ws://h/nodes/ws"


async def test_the_cover_goes_to_the_hub_once_per_picture_and_again_on_a_new_connection(fake, phone, tmp_path):
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    jpeg = b"\xff\xd8\xff" + b"cover" * 10
    a.metadata._artwork(jpeg)

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {}}))
        await recv(ws, got, "status")
        await a._send_artwork()   # the same picture: not again
        await roundtrip(ws, got)
    for connection in (1, 2):
        got = await talk(a, hub)
        kinds = [m.get("type") for m in got if isinstance(m, dict)]
        assert kinds.count("artwork") == 1, f"once on connection {connection}"
        assert kinds.index("artwork") < kinds.index("status"), "before the status that names it"
        [sent] = of(got, "artwork")
        assert sent["sha256"] == hashlib.sha256(jpeg).hexdigest() and sent["format"] == "jpeg"
        assert base64.b64decode(sent["data"]) == jpeg
        assert of(got, "status")[0]["airplay"]["artwork"]["sha256"] == sent["sha256"]


@pytest.mark.parametrize("path", ["ticker", "push"])
async def test_a_new_cover_goes_before_the_status_that_names_it_on_every_path(fake, phone, tmp_path,
                                                                               monkeypatch, path):
    if path == "ticker":
        monkeypatch.setattr(agentmod, "STATUS_S", 0.05)
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    jpeg = b"\xff\xd8\xff" + b"next track" * 10
    sha = hashlib.sha256(jpeg).hexdigest()

    def named(m) -> bool:
        return m.get("type") == "status" and ((m["airplay"] or {}).get("artwork") or {}).get("sha256") == sha

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {}}))
        await roundtrip(ws, got)
        a.metadata._artwork(jpeg)   # the pipe's picture for the next track
        if path == "push":
            a._push_status()
        while not named(await recv(ws, got, "status")):
            pass
    got = [m for m in await talk(a, hub) if isinstance(m, dict)]
    sent = [i for i, m in enumerate(got) if m.get("type") == "artwork" and m["sha256"] == sha]
    assert sent and sent[0] < next(i for i, m in enumerate(got) if named(m))


async def test_a_cover_is_never_sent_under_another_pictures_name(fake, tmp_path):
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    first = b"\xff\xd8\xff" + b"first" * 10
    a.metadata._artwork(first)
    a.airplay_state = {"artwork": a.metadata.state["artwork"]}
    a.metadata.art_file.write_bytes(b"\xff\xd8\xff" + b"next!" * 10)   # the pipe's next picture, meanwhile
    sent = []

    class Socket:
        async def send(self, msg):
            sent.append(json.loads(msg))
    a.ws = Socket()
    await a._send_artwork()
    assert sent == [] and a._art_sent is None
    a.metadata.art_file.write_bytes(first)
    await a._send_artwork()
    assert [m["sha256"] for m in sent] == [hashlib.sha256(first).hexdigest()]


# ---- caps, the media lane and the one volume -----------------------------------


async def test_caps_offer_the_media_lane_controls_health_and_the_mic_gain_range(fake, phone, key):
    fake["devices"] = pipewire.Devices((SINK,), (MIC,), SINK.name, MIC.name)
    a = agent_with(hub="ws://x")

    async def hub(ws, got):
        await recv(ws, got, "hello")
    got = await talk(a, hub)
    assert got[0]["caps"] == {
        "speaker": {"rate": 44100, "channels": 1, "format": "s16le"},
        "media": {"rate": 44100, "channels": 2, "format": "s16le"},
        "mic": {"rate": 16000, "channels": 2, "format": "s16le", "reference": True, "max_gain_db": 3.5},
        "earcons": {"max": 16, "max_bytes": 524288, "rate": 48000},
        "duck": True, "audio_devices": True, "bundle": "tar.gz", "ota_key": bundle.key_id(),
        "airplay": {"version": 2, "controls": True},
        "health": ["temp_c", "throttled", "under_voltage", "load"]}
    assert bundle.key_id()


async def test_media_frames_play_on_their_own_stereo_stream_and_media_flush_stops_only_them(fake):
    own = agentmod.Agent(state.State(hub="ws://x")).media
    assert (own.channels, own.role, own.on_active) == (2, "Music", None), "music never ducks anything"
    a = agent_with(hub="ws://x", token="t", name="k")
    stereo = b"\x01\x00\x02\x00" * 882

    async def hub(ws, got):
        await recv(ws, got, "hello")
        for seq in (1, 2):
            await ws.send(struct.pack("<BBBBIQ", 5, 0, 2, 0, seq, 0) + stereo)
        await ws.send(struct.pack("<BBBBIQ", 2, 0, 1, 0, 1, 0) + b"\x01\x00" * 882)
        await ws.send(json.dumps({"type": "media_flush"}))
        await roundtrip(ws, got)
        assert (a.media.flushes, a.player.flushes) == (1, 0), "media_flush leaves the voice alone"
        await ws.send(json.dumps({"type": "flush"}))
        await roundtrip(ws, got)
        assert (a.media.flushes, a.player.flushes) == (1, 1), "flush leaves the music alone"
    await talk(a, hub)
    assert a.media.writes == [stereo, stereo] and len(a.player.writes) == 1


async def test_media_is_not_played_with_the_speaker_off(fake):
    a = agent_with(hub="ws://x", token="t", name="k")
    a.st.config["speaker_enabled"] = False

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(struct.pack("<BBBBIQ", 5, 0, 2, 0, 1, 0) + b"\x00" * 3528)
        await roundtrip(ws, got)
    await talk(a, hub)
    assert a.media.writes == []


async def test_the_welcome_sets_the_configured_volume(fake):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 45}}))
        await recv(ws, got, "status")
    await talk(a, hub)
    assert ("volume", None, 0.45) in fake["calls"]


async def test_a_config_without_volume_leaves_the_output_volume_alone(fake):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 80}}))
        await recv(ws, got, "status")
        fake["calls"].clear()
        await ws.send(json.dumps({"type": "config", "lights_enabled": True}))
        await recv(ws, got, "status")
        assert not [c for c in fake["calls"] if c[0] == "volume"], "the phone's volume stays"
        await ws.send(json.dumps({"type": "config", "volume": 80}))
        await recv(ws, got, "status")
    await talk(a, hub)
    assert ("volume", None, 0.8) in fake["calls"], "a volume the hub sends is set, even the same one"


async def test_a_volume_the_phone_set_becomes_the_satellites_own(fake, monkeypatch):
    monkeypatch.setattr(agentmod, "STATUS_S", 0.05)
    fake["volumes"][None] = 0.32      # the phone's slider, before the hub has said anything
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        early = await recv(ws, got, "status")
        assert "cause" not in early and early["volume"] == 60, "nothing is taken before the welcome"
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        fake["volumes"][None] = 0.32  # and after it: the phone's slider moves
        while (await recv(ws, got, "status")).get("cause") != "local":
            pass
        await recv(ws, got, "status")
    got = await talk(a, hub)
    statuses = of(got, "status")
    first_local = next(i for i, s in enumerate(statuses) if s.get("cause") == "local")
    assert statuses[first_local]["volume"] == 32
    assert "cause" not in statuses[-1] and statuses[-1]["volume"] == 32, "taken once"
    assert state.load().config["volume"] == 32
    assert not [c for c in fake["calls"] if c[0] == "volume" and c[2] != 0.6], "the phone's volume is not undone"


async def test_the_phones_slider_reaches_the_hub_at_once_not_at_the_next_tick(fake, monkeypatch):
    """pactl reports the hook's volume change as a sink event: the status
    with cause "local" goes out then, long before the ticker would send it."""
    monkeypatch.setattr(agentmod, "STATUS_S", 30)
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        fake["volumes"][None] = 0.45          # the phone's slider, through the hook
        started = asyncio.get_running_loop().time()
        await a._devices_changed()            # what pactl subscribe's sink event calls
        while (await recv(ws, got, "status")).get("cause") != "local":
            pass
        got.append({"took_s": asyncio.get_running_loop().time() - started})
    got = await talk(a, hub)
    local = [s for s in of(got, "status") if s.get("cause") == "local"]
    assert local and local[0]["volume"] == 45
    assert next(g["took_s"] for g in got if isinstance(g, dict) and "took_s" in g) < 2, "not the 30 s tick"


async def test_a_sink_event_with_nothing_moved_sends_nothing(fake, monkeypatch):
    monkeypatch.setattr(agentmod, "STATUS_S", 30)
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        before = len(of(got, "status"))
        fake["volumes"][None] = 0.60          # the hub's own volume: no change
        await a._devices_changed()
        await asyncio.sleep(0.6)
        await ws.send(json.dumps({"type": "earcon_list"}))
        await recv(ws, got, "earcons")
        got.append({"statuses_after": len(of(got, "status")) - before})
    got = await talk(a, hub)
    assert next(g["statuses_after"] for g in got if isinstance(g, dict) and "statuses_after" in g) == 0


async def test_an_output_past_full_is_taken_once_as_100_even_when_it_cannot_be_saved(fake, monkeypatch):
    monkeypatch.setattr(agentmod, "STATUS_S", 0.05)
    a = agent_with(hub="ws://x", token="t", name="k")

    def read_only(st):
        raise OSError("read-only file system")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        monkeypatch.setattr(agentmod, "save", read_only)
        fake["volumes"][None] = 1.5   # someone took the output past full
        while (await recv(ws, got, "status")).get("cause") != "local":
            pass
        for _ in range(3):
            await recv(ws, got, "status")
    got = await talk(a, hub)
    assert [s["volume"] for s in of(got, "status") if s.get("cause") == "local"] == [100], "clamped, taken once"
    assert a.st.config["volume"] == 100 and of(got, "status")[-1]["volume"] == 100


async def test_a_new_output_is_given_the_satellites_volume_not_taken_for_the_phones(fake, monkeypatch):
    monkeypatch.setattr(agentmod, "STATUS_S", 0.05)
    fake["volumes"][SINK.name] = 1.0   # a USB DAC fresh from its box: at full
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 80}}))
        await roundtrip(ws, got)
        await ws.send(json.dumps({"type": "config", "audio_sink": SINK.name}))
        await roundtrip(ws, got)
        assert ("volume", SINK.name, 0.8) in fake["calls"] and a.media.target == SINK.name
        for _ in range(3):
            await recv(ws, got, "status")   # ticks that read the new output back
        fake["calls"].clear()
        await ws.send(json.dumps({"type": "config", "audio_sink": SINK.name}))
        await roundtrip(ws, got)
        assert not [c for c in fake["calls"] if c[0] == "volume"], "the same output again is not a new one"
    got = await talk(a, hub)
    assert not [s for s in of(got, "status") if s.get("cause") == "local"]
    assert a.st.config["volume"] == 80


async def test_nothing_is_taken_from_a_stand_in_while_the_chosen_output_is_unplugged(fake, monkeypatch):
    monkeypatch.setattr(agentmod, "STATUS_S", 0.05)
    fake["devices"] = pipewire.Devices((BUILTIN, SINK), (), SINK.name, None)
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {
            "volume": 80, "audio_sink": SINK.name}}))
        await roundtrip(ws, got)
        fake["devices"] = pipewire.Devices((BUILTIN,), (), BUILTIN.name, None)
        fake["volumes"][None] = 0.3   # the built-in output plays meanwhile, at its own volume
        for _ in range(4):
            await recv(ws, got, "status")
    got = await talk(a, hub)
    assert not [s for s in of(got, "status") if s.get("cause") == "local"]
    assert a.st.config["volume"] == 80


async def test_a_volume_read_before_the_hubs_arrived_is_not_taken_for_the_phones(fake):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        fake["gate"], fake["reading"] = asyncio.Event(), asyncio.Event()
        a._push_status()                 # AirPlay's state changed: the output's volume is read
        await fake["reading"].wait()     # wpctl has said 60 %, and the answer is on its way
        await ws.send(json.dumps({"type": "config", "volume": 30}))
        await asyncio.sleep(0.1)         # the config would be handled by now, were nothing holding it
        fake["gate"].set()
        while (await recv(ws, got, "status"))["volume"] != 30:
            pass
        await roundtrip(ws, got)
    got = await talk(a, hub)
    assert not [s for s in of(got, "status") if s.get("cause") == "local"], "the hub's volume was not undone"
    assert a.st.config["volume"] == 30 and state.load().config["volume"] == 30
    assert fake["volumes"][None] == 0.3


# ---- AirPlay: the phone's controls ----------------------------------------------


async def test_the_agent_answers_an_airplay_command_with_its_result(fake, phone):
    phone["then"] = {"metadata": {"trackid": "/org/gnome/ShairportSync/mper_2"}}
    a = agent_with(hub="ws://x", token="t", name="k")
    rid = "0123456789abcdef" * 2

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "airplay_command", "id": rid, "command": "next"}))
        result = await recv(ws, got, "airplay_result")
        assert result == {"type": "airplay_result", "id": rid, "command": "next", "ok": True, "status": 204,
                          "confirmed": True, "error": None}
        status = await recv(ws, got, "status")
        last = status["airplay"]["remote"]["last"]
        assert (last["command"], last["ok"], last["status"], last["confirmed"]) == ("next", True, 204, True)
        phone["answer"] = (False, 491, "the phone refused the connection")
        await ws.send(json.dumps({"type": "airplay_command", "id": "r2", "command": "pause"}))
        refused = await recv(ws, got, "airplay_result")
        assert (refused["ok"], refused["status"], refused["confirmed"], refused["error"]) == (
            False, 491, False, "the phone refused the connection")
    await talk(a, hub)
    assert phone["commands"] == ["next", "pause"]


async def test_an_airplay_command_the_phone_took_but_did_not_act_on_is_not_confirmed(fake, phone, monkeypatch):
    monkeypatch.setattr(agentmod, "CONFIRM_S", 0.3)
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "airplay_command", "id": "r1", "command": "pause"}))
        result = await recv(ws, got, "airplay_result")
        assert (result["ok"], result["status"], result["confirmed"]) == (True, 204, False)
    await talk(a, hub)


async def test_a_change_is_not_confirmed_without_a_reading_from_before_it(fake, phone, monkeypatch):
    monkeypatch.setattr(agentmod, "CONFIRM_S", 0.3)
    phone["metadata"] = {}   # MPRIS gave no track id before the command
    phone["then"] = {"metadata": {"trackid": "/org/gnome/ShairportSync/mper_2"}}
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "airplay_command", "id": "r1", "command": "next"}))
        result = await recv(ws, got, "airplay_result")
        assert (result["ok"], result["confirmed"]) == (True, False), "any track would do: no evidence"
    await talk(a, hub)
    assert await a._confirm("play_pause", None, time.monotonic() + 1) is False
    assert await a._confirm("play_pause", "Paused", time.monotonic() + 1) is True


async def test_watching_for_a_commands_effect_never_outlasts_the_hubs_wait(fake, phone, monkeypatch):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def unanswered():
        await asyncio.sleep(10)   # a session bus that does not answer
    monkeypatch.setattr(airplay, "mpris_status", unanswered)
    started = time.monotonic()
    assert await a._confirm("pause", None, started + 0.2) is False
    assert time.monotonic() - started < 1


async def test_a_result_is_not_sent_on_a_connection_that_never_asked_for_it(fake, phone):
    phone["gate"], phone["then"] = asyncio.Event(), {"player": "Paused"}
    a = agent_with(hub="ws://x", token="t", name="k")

    async def first(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "airplay_command", "id": "r1", "command": "pause"}))
        while not phone["commands"]:   # with the phone, which answers once the connection is gone
            await asyncio.sleep(0.01)

    async def second(ws, got):
        await recv(ws, got, "hello")
        phone["gate"].set()
        await asyncio.gather(*a._tasks)
        await roundtrip(ws, got)
    await talk(a, first)
    got = await talk(a, second)
    assert of(got, "airplay_result") == []
    assert a.remote_last["command"] == "pause", "what happened is still in the status"


async def test_the_status_after_a_command_is_not_lost_behind_one_under_way(fake, phone):
    phone["then"] = {"metadata": {"trackid": "/org/gnome/ShairportSync/mper_2"}}
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {"volume": 60}}))
        await roundtrip(ws, got)
        fake["gate"], fake["reading"] = asyncio.Event(), asyncio.Event()
        a._push_status()               # a new track: this one has read AirPlay's state already
        await fake["reading"].wait()
        await ws.send(json.dumps({"type": "airplay_command", "id": "r1", "command": "next"}))
        await recv(ws, got, "airplay_result")
        fake["gate"].set()
        while (await recv(ws, got, "status"))["airplay"]["remote"]["last"] is None:
            pass
    got = await talk(a, hub)
    assert of(got, "status")[-1]["airplay"]["remote"]["last"]["command"] == "next"


async def test_an_unknown_airplay_command_is_answered_not_ignored(fake, phone):
    a = agent_with(hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "airplay_command", "id": "r1", "command": "shuffle"}))
        await ws.send(json.dumps({"type": "airplay_command", "command": "next"}))
        first, second = await recv(ws, got, "airplay_result"), await recv(ws, got, "airplay_result")
        assert first == {"type": "airplay_result", "id": "r1", "command": "shuffle", "ok": False, "status": None,
                         "confirmed": False, "error": "unknown command"}
        assert (second["id"], second["ok"], second["error"]) == (None, False, "unknown command")
    await talk(a, hub)
    assert phone["commands"] == [], "nothing reached the phone"


async def test_controls_are_offered_only_while_a_phone_plays_and_takes_remote_control(fake, phone):
    a = agent_with(hub="ws://x", token="t", name="k")
    phone["player"] = "Stopped"
    s = await a._airplay_state()
    assert s["session"] is False and s["remote"] == {"available": None, "controls": [], "last": None}
    assert "remote_control" not in s
    phone["player"], phone["available"] = "Playing", False
    s = await a._airplay_state()
    assert s["session"] is True and s["remote"]["controls"] == ["disconnect"] and s["remote"]["available"] is False
    phone["player"], phone["available"] = "Paused", True
    s = await a._airplay_state()
    assert s["remote"]["controls"] == ["play", "pause", "play_pause", "next", "previous", "stop", "disconnect"]
    phone["player"] = "Stopped"
    a.metadata.feed(b"<item><type>73736e63</type><code>61626567</code><length>0</length></item>")   # ssnc/abeg
    s = await a._airplay_state()
    assert s["session"] is True and s["remote"]["available"] is True, "the pipe's word for a session counts too"


# ---- AirPlay: the cover and title after the agent restarts ----------------------


def cached(phone, data=b"\xff\xd8\xff\xe0" + b"cached" * 40) -> tuple[bytes, str]:
    phone["covers"].mkdir(parents=True, exist_ok=True)
    path = phone["covers"] / f"cover-{hashlib.md5(data).hexdigest()}.jpg"
    path.write_bytes(data)
    phone["metadata"]["art_url"] = f"file://{path}"
    return data, hashlib.sha256(data).hexdigest()


async def test_the_cover_is_recovered_from_shairports_cache_after_a_restart(fake, phone, tmp_path):
    data, sha = cached(phone)
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {}}))
        await recv(ws, got, "status")
    got = await talk(a, hub)
    kinds = [m.get("type") for m in got if isinstance(m, dict)]
    assert "artwork" in kinds and kinds.index("artwork") < kinds.index("status"), "the cover before its status"
    [art] = of(got, "artwork")
    assert art["sha256"] == sha and base64.b64decode(art["data"]) == data
    assert of(got, "status")[0]["airplay"]["artwork"]["sha256"] == sha
    assert a.metadata.art_file.read_bytes() == data, "kept as the pipe's picture would be"


async def test_a_stopped_player_recovers_no_cover(fake, phone, tmp_path):
    cached(phone)
    phone["player"] = "Stopped"
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {}}))
        await recv(ws, got, "status")
        await roundtrip(ws, got)
    got = await talk(a, hub)
    assert of(got, "artwork") == [] and of(got, "status")[0]["airplay"]["artwork"] is None


async def test_a_picture_from_the_pipe_is_never_replaced_by_the_cache(fake, phone, tmp_path):
    cached(phone)
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    piped = b"\xff\xd8\xff\xe0" + b"piped" * 40
    a.metadata._artwork(piped)
    assert (await a._airplay_state())["artwork"]["sha256"] == hashlib.sha256(piped).hexdigest()
    b = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    b.metadata._artwork(b"")          # the phone said: no picture for this track
    assert (await b._airplay_state())["artwork"] is None


async def test_a_picture_the_pipe_sends_while_the_cache_is_read_is_the_one_kept(fake, phone, tmp_path,
                                                                                monkeypatch):
    cached(phone)
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    a.metadata.take("core", "minm", b"Clair de Lune")   # the title is known: MPRIS is asked only for the cover
    piped = b"\xff\xd8\xff\xe0" + b"piped" * 40
    ask = airplay.mpris_metadata

    async def meanwhile():
        a.metadata._artwork(piped)    # the pipe's thread, while MPRIS answers
        return await ask()
    monkeypatch.setattr(airplay, "mpris_metadata", meanwhile)
    s = await a._airplay_state()
    assert s["artwork"]["sha256"] == hashlib.sha256(piped).hexdigest()
    assert a.metadata.art_file.read_bytes() == piped


async def test_the_title_comes_from_mpris_after_a_restart_while_playing(fake, phone, tmp_path):
    phone["metadata"] |= {"title": "Clair de Lune", "artist": "Debussy", "album": "Suite bergamasque"}
    cached(phone)
    a = agent_with(tmp_path, hub="ws://x", token="t", name="k")
    assert a.metadata.state["session"] is False
    s = await a._airplay_state()
    assert (s["session"], s["title"], s["artist"], s["album"]) == (
        True, "Clair de Lune", "Debussy", "Suite bergamasque")
    assert s["artwork"] is not None and phone["metadata_reads"] == 1, "MPRIS asked once for both"
