"""The agent against a fake hub on a real WebSocket: hello and caps, adoption,
settings, speaker audio, earcons, and an update from first chunk to restart.
PipeWire and calliope-root are fakes; nothing plays and nothing is installed."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct

import build_bundle
import pytest
from websockets.asyncio.server import serve

from calliope_pi import agent as agentmod
from calliope_pi import bundle, pipewire, state

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
    world = {"devices": pipewire.Devices((BUILTIN, SINK), (), BUILTIN.name, None), "calls": [], "root": []}

    async def devices():
        return world["devices"]

    async def set_default(dev):
        world["calls"].append(("default", dev.name))
        return True

    async def set_volume(dev, fraction):
        world["calls"].append(("volume", dev.name if dev else None, round(fraction, 2)))
        return True

    async def set_source_volume(dev, fraction):
        world["calls"].append(("mic", dev.name if dev else None, round(fraction, 2)))
        return True

    monkeypatch.setattr(pipewire, "devices", devices)
    monkeypatch.setattr(pipewire, "set_default", set_default)
    monkeypatch.setattr(pipewire, "set_volume", set_volume)
    monkeypatch.setattr(pipewire, "set_source_volume", set_source_volume)
    monkeypatch.setattr(agentmod.system, "satellite_id", lambda: "b827eb123456")
    return world


async def talk(agent: agentmod.Agent, hub) -> list:
    """Run the agent against `hub(ws, got)` until it returns; what the agent sent."""
    got: list = []
    done = asyncio.Event()

    async def handler(ws):
        try:
            await hub(ws, got)
        finally:
            done.set()
            await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(agent.session(f"ws://127.0.0.1:{port}/satellites/ws"))
        await asyncio.wait_for(done.wait(), 10)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return got


async def recv(ws, got, kind=None):
    while True:
        msg = await asyncio.wait_for(ws.recv(), 5)
        m = json.loads(msg) if isinstance(msg, str) else msg
        got.append(m)
        if kind is None or (isinstance(m, dict) and m.get("type") == kind):
            return m


def agent_with(**kw) -> agentmod.Agent:
    a = agentmod.Agent(state.State(**kw))
    a.player = FakePlayer()
    return a


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


async def test_a_microphone_is_offered_with_the_output_as_its_reference_channel(fake):
    fake["devices"] = pipewire.Devices((SINK,), (MIC,), SINK.name, MIC.name)
    a = agent_with(hub="ws://x")

    async def hub(ws, got):
        await recv(ws, got, "hello")
    got = await talk(a, hub)
    assert got[0]["caps"]["mic"] == {"rate": 16000, "channels": 2, "format": "s16le", "reference": True}


async def test_the_welcome_chooses_the_output_and_its_volume_and_confirms_a_new_release(fake, key):
    data = build_bundle.build("v2", epoch=1_700_000_000)
    bundle.install(data, build_bundle.sign(data, key), hook=False)
    a = agent_with(hub="ws://x", token="t0k", name="k")

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
    assert a.player.target == "alsa_output.usb"
    status = [m for m in got if isinstance(m, dict) and m.get("type") == "status"][-1]
    assert status["volume"] == 40 and status["audio_sink"] == "alsa_output.usb"
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


async def test_the_cover_goes_to_the_hub_once_per_picture_and_again_on_a_new_connection(fake, tmp_path):
    a = agent_with(hub="ws://x", token="t", name="k")
    jpeg = b"\xff\xd8\xff" + b"cover" * 10
    a.metadata.art_dir, a.metadata.art_file = tmp_path, tmp_path / "calliope-airplay-cover"
    a.metadata._artwork(jpeg)
    a.airplay_state = {"artwork": dict(a.metadata.state["artwork"])}

    async def hub(ws, got):
        await recv(ws, got, "hello")
        await ws.send(json.dumps({"type": "welcome", "name": "k", "config": {}}))
        await recv(ws, got, "artwork")
        await a._send_artwork()   # the same picture: not again
        await ws.send(json.dumps({"type": "earcon_list"}))
        await recv(ws, got, "earcons")
    got = await talk(a, hub)
    sent = [m for m in got if isinstance(m, dict) and m.get("type") == "artwork"]
    assert len(sent) == 1
    assert sent[0]["sha256"] == hashlib.sha256(jpeg).hexdigest() and sent[0]["format"] == "jpeg"
    assert base64.b64decode(sent[0]["data"]) == jpeg


async def test_a_phone_that_connects_is_moved_to_the_starting_volume(fake, monkeypatch):
    asked, ran = [], []

    async def phone(percent):
        asked.append(percent)
        return len(asked) == 1   # the first phone takes remote control; the second does not

    class Proc:
        async def wait(self):
            return 0

    async def run(*argv, **kw):
        ran.append(argv)
        return Proc()
    monkeypatch.setattr(agentmod.airplay, "set_phone_volume", phone)
    monkeypatch.setattr(agentmod.asyncio, "create_subprocess_exec", run)
    nap = asyncio.sleep
    monkeypatch.setattr(agentmod.asyncio, "sleep", lambda s: nap(0))
    a = agent_with(hub="ws://x", token="t", name="k")
    a.st.config["airplay_volume"] = 40
    await a._starting_volume()
    assert asked == [40] and ran == []
    await a._starting_volume()
    assert ran == [(agentmod.airplay.VOLUME_HOOK, "60", "-18.0")]
