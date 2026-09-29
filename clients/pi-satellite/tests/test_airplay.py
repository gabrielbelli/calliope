"""AirPlay: Shairport Sync's configuration, the user service, and ducking
every other stream while the satellite speaks or the hub asks. systemctl and
pactl are fakes that record what they were asked."""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope_pi import agent as agentmod
from calliope_pi import airplay, pipewire, state


def sink_input(index: int, volume: int, role: str | None = None, app: str = "AirPlay") -> dict:
    props = {"application.name": app}
    if role:
        props["media.role"] = role
    return {"index": index, "properties": props,
            "volume": {"front-left": {"value": volume, "value_percent": "?"},
                       "front-right": {"value": volume, "value_percent": "?"}}}


@pytest.fixture
def shell(monkeypatch):
    """A fake systemctl and pactl: what was run, and what pactl lists."""
    world = {"ran": [], "inputs": [], "active": "active"}

    async def run(*argv, timeout=15.0):
        world["ran"].append(argv)
        if argv[:2] == ("pactl", "-f"):
            return 0, json.dumps(world["inputs"])
        if argv[:3] == ("systemctl", "--user", "is-active"):
            return 0, world["active"] + "\n"
        return 0, ""
    monkeypatch.setattr(airplay, "_run", run)
    monkeypatch.setattr(airplay, "available", lambda: True)
    return world


def test_the_name_is_quoted_so_a_name_cannot_break_the_configuration():
    text = airplay.config('Kitchen "big" speaker\\ \n')
    assert 'name = "Kitchen \\"big\\" speaker\\\\ ";' in text
    assert 'output_backend = "alsa";' in text and 'output_format = "S16";' in text
    assert 'ignore_volume_control = "yes";' in text
    assert f'run_this_when_volume_is_set = "{airplay.VOLUME_HOOK} 60 ";' in text
    assert 'output_device = "default";' in text and "output_rate = 44100;" in text


async def test_it_runs_as_a_user_service_and_restarts_only_when_its_name_changed(shell, tmp_path):
    ap = airplay.AirPlay(conf=tmp_path / "shairport-sync.conf", pipe=tmp_path / "metadata")
    await ap.apply(True, "pi-edifier")
    assert ("systemctl", "--user", "restart", airplay.UNIT) in shell["ran"]
    assert 'name = "pi-edifier";' in (tmp_path / "shairport-sync.conf").read_text()
    shell["ran"].clear()
    await ap.apply(True, "pi-edifier")
    assert ("systemctl", "--user", "start", airplay.UNIT) in shell["ran"], "no restart: the music goes on"
    assert not any(a[2] == "restart" for a in shell["ran"] if a[0] == "systemctl")
    await ap.apply(False, "pi-edifier")
    assert shell["ran"][-1] == ("systemctl", "--user", "disable", "--now", airplay.UNIT)


def test_the_satellites_own_voice_is_never_ducked():
    listed = [sink_input(3, 65536), sink_input(9, 32768, role="Assistant", app="pw-cat")]
    assert airplay.others(listed) == [{"index": 3, "volume": 1.0, "app": "AirPlay"}]


async def test_ducking_lowers_every_other_stream_and_gives_each_its_own_volume_back(shell):
    shell["inputs"] = [sink_input(3, 65536), sink_input(4, 32768, app="Bluetooth")]
    d = airplay.Ducker()
    await d.set(0.2)
    await asyncio.sleep(0.05)
    sets = [a for a in shell["ran"] if a[:2] == ("pactl", "set-sink-input-volume")]
    assert ("pactl", "set-sink-input-volume", "3", "0.200") in sets
    assert ("pactl", "set-sink-input-volume", "4", "0.100") in sets
    shell["inputs"].append(sink_input(7, 65536, app="Late"))   # starts while ducked
    await asyncio.sleep(0.6)
    assert ("pactl", "set-sink-input-volume", "7", "0.200") in shell["ran"]
    shell["ran"].clear()
    await d.set(None)
    back = {a[2]: a[3] for a in shell["ran"] if a[:2] == ("pactl", "set-sink-input-volume")}
    assert back == {"3": "1.000", "4": "0.500", "7": "1.000"}


async def test_the_agent_ducks_while_it_speaks_and_while_the_hub_holds_a_duck(shell, monkeypatch):
    monkeypatch.setattr(agentmod.system, "satellite_id", lambda: "b827eb121359")
    a = agentmod.Agent(state.State(hub="ws://x", token="t", name="pi-edifier"))
    levels = []

    async def fake_set(level):
        levels.append(level)
    a.ducker.set = fake_set
    await a.on_text({"type": "duck", "level": 20, "ms": 60000})
    await a._speaking(True)
    await a.on_text({"type": "unduck"})      # the reply began: the hub lifts its duck
    await a._speaking(False)
    assert levels == [0.2, agentmod.SPEAK_DUCK, agentmod.SPEAK_DUCK, None]
    assert a.caps()["airplay"] == {"version": 1}
    assert a.airplay_name() == "pi-edifier"
    a.st.config["airplay_name"] = "Living room"
    assert a.airplay_name() == "Living room"


async def test_the_player_says_when_a_voice_starts_and_ends(monkeypatch):
    seen = []

    class Proc:
        returncode = None

        class stdin:
            @staticmethod
            def write(b):
                pass

            @staticmethod
            async def drain():
                pass

            @staticmethod
            def close():
                pass

        async def wait(self):
            self.returncode = 0

    async def spawn(*a, **k):
        return Proc()
    monkeypatch.setattr(pipewire.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(pipewire, "IDLE_S", 0.05)
    p = pipewire.Player()

    async def on_active(active):
        seen.append(active)
    p.on_active = on_active
    await p.write(b"\x00\x00" * 480)       # 10 ms
    await asyncio.sleep(0.2)
    assert seen == [True, False]


def item(kind: str, code: str, data: bytes = b"") -> bytes:
    import base64
    head = (f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>"
            f"<length>{len(data)}</length>").encode()
    if data:
        return head + b'\n<data encoding="base64">\n' + base64.b64encode(data) + b"</data></item>\n"
    return head + b"</item>\n"


def test_the_metadata_pipe_says_who_plays_what_and_when_it_pauses():
    changes = []
    m = airplay.Metadata(on_change=lambda: changes.append(1))
    stream = (item("ssnc", "abeg") + item("ssnc", "snam", b"Gabriel's iPhone") + item("ssnc", "pbeg")
              + item("ssnc", "mdst") + item("core", "minm", b"Clair de Lune") + item("core", "asar", b"Debussy")
              + item("core", "asal", b"Suite bergamasque") + item("ssnc", "pvol", b"-15.00,0.00,-30.00,0.00"))
    rest = m.feed(stream[:-20])            # a read that ends mid-item keeps the rest
    rest = m.feed(rest + stream[-20:])
    assert rest == b""
    s = m.state
    assert (s["session"], s["playing"], s["client"]) == (True, True, "Gabriel's iPhone")
    assert (s["title"], s["artist"], s["album"], s["volume"]) == ("Clair de Lune", "Debussy", "Suite bergamasque", 50)
    m.feed(item("ssnc", "pfls"))
    assert m.state["playing"] is False and m.state["session"] is True
    m.feed(item("ssnc", "aend"))
    assert m.state["session"] is False and m.state["client"] is None and m.state["title"] is None
    assert changes, "the page is told at once"


def test_the_stream_says_its_format_rate_and_bit_rate():
    si = [{"index": 5, "properties": {"application.process.binary": "shairport-sync"},
           "sample_specification": "s16le 2ch 44100Hz",
           "corked": False, "buffer_latency_usec": 180000, "sink_latency_usec": 20000},
          {"index": 6, "properties": {"application.process.binary": "pw-cat", "media.role": "Assistant"},
           "sample_specification": "s16le 1ch 48000Hz"}]
    assert airplay.stream(si) == {"format": "s16le 2ch 44100Hz", "corked": False, "bits": 16, "channels": 2,
                                  "rate": 44100, "bitrate_kbps": 1411, "latency_ms": 200}
    assert airplay.stream(si[1:]) is None
    assert airplay.stream([si[0] | {"sample_specification": "float32le 2ch 48000Hz"}])["bitrate_kbps"] == 3072


def test_the_configuration_names_the_metadata_pipe(tmp_path):
    text = airplay.config("pi", tmp_path / "meta")
    assert 'enabled = "yes";' in text and f'pipe_name = "{tmp_path / "meta"}";' in text


def test_the_output_says_what_the_card_is_driven_at():
    from calliope_pi import pipewire
    sinks = [{"name": "alsa_output.usb-dac", "sample_specification": "s32le 2ch 44100Hz", "state": "RUNNING"},
             {"name": "alsa_output.builtin", "sample_specification": "s16le 2ch 48000Hz", "state": "SUSPENDED"}]
    assert pipewire.output_format(sinks, "alsa_output.usb-dac") == {
        "name": "alsa_output.usb-dac", "format": "s32le 2ch 44100Hz", "state": "running"}
    assert pipewire.output_format(sinks, None)["name"] == "alsa_output.usb-dac"
    assert pipewire.output_format(sinks, "gone") is None


def test_a_resume_that_sends_no_resume_event_is_still_playing():
    m = airplay.Metadata()
    m.feed(item("ssnc", "pbeg") + item("ssnc", "pfls"))
    assert m.state["playing"] is False
    m.feed(item("ssnc", "prgr", b"1000/45100/4411000"))   # progress: sent as it resumes
    assert m.state["playing"] is True
    assert m.state["progress"]["position_s"] == 1.0 and m.state["progress"]["duration_s"] == 100.0
    m.feed(item("core", "caps", bytes([3])))
    assert m.state["playing"] is False
    m.feed(item("core", "caps", bytes([4])))
    assert m.state["playing"] is True


def test_everything_airplay_sends_is_kept_decoded_and_raw_but_the_remote_token_stays_here(tmp_path):
    m = airplay.Metadata(pipe=tmp_path / "meta", art_dir=tmp_path)
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 1000
    m.feed(item("ssnc", "snam", b"Gabriel's iPhone") + item("ssnc", "snua", b"AirPlay/870.14.1")
           + item("ssnc", "clip", b"192.0.2.44") + item("ssnc", "daid", b"ABCDEF0123456789")
           + item("ssnc", "acre", b"1234567890") + item("core", "asgn", b"Jazz")
           + item("core", "astm", (245000).to_bytes(4, "big")) + item("core", "asyr", (2019).to_bytes(2, "big"))
           + item("core", "asbr", (256).to_bytes(2, "big")) + item("core", "asdt", b"AAC audio file")
           + item("ssnc", "pcst") + item("ssnc", "PICT", jpeg) + item("ssnc", "pcen"))
    t, c = m.state["track"], m.state["client_info"]
    assert (t["genre"], t["duration_ms"], t["year"], t["bitrate_kbps"], t["kind"]) == (
        "Jazz", 245000, 2019, 256, "AAC audio file")
    assert (c["client_name"], c["client_agent"], c["client_ip"], c["dacp_id"]) == (
        "Gabriel's iPhone", "AirPlay/870.14.1", "192.0.2.44", "ABCDEF0123456789")
    assert m.raw["core/astm"]["value"] == 245000 and m.raw["ssnc/snam"]["value"] == "Gabriel's iPhone"
    assert "ssnc/acre" not in m.raw and m.remote == {"acre": "1234567890"}
    assert m.view()["remote_control"] is True and "acre" not in json.dumps(m.view())
    art = m.state["artwork"]
    assert art["type"] == "jpeg" and art["bytes"] == len(jpeg)
    assert (tmp_path / "calliope-airplay-cover").read_bytes() == jpeg
    assert "ssnc/PICT" not in m.raw, "the picture is kept once, as a file"
    m.feed(item("ssnc", "aend"))
    assert m.state["track"] == {} and m.state["artwork"] is None and m.remote == {}


async def test_the_players_own_word_on_playing(shell, monkeypatch):
    async def run(*argv, timeout=15.0):
        if argv[0] == "busctl":
            return 0, '{"type":"s","data":"Playing"}'
        return 1, ""
    monkeypatch.setattr(airplay, "_run", run)
    assert await airplay.mpris_status() == "Playing"
    async def gone(*argv, timeout=15.0):
        return 1, "Unknown object"
    monkeypatch.setattr(airplay, "_run", gone)
    assert await airplay.mpris_status() is None


def test_the_phones_volume_becomes_the_outputs_own_spread_over_60_db(tmp_path):
    """The hook, run as Shairport Sync runs it, with pactl recording what it
    was asked: the slider's 30 dB spread over 60, as a linear factor (the
    pactl here refuses dB); the top is full volume; -144 mutes."""
    import os
    import subprocess
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "bundle" / "bin" / "calliope-airplay-volume"
    log = tmp_path / "pactl.log"
    fake = tmp_path / "pactl"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    for db in ("-15.000000", "0.000000", "-28.125000", "-144.000000"):
        subprocess.run([str(hook), "60", db], env=env, check=True)
    subprocess.run([str(hook), "60"], env=env, check=True)   # no volume: nothing
    assert log.read_text().splitlines() == [
        "set-sink-mute @DEFAULT_SINK@ 0", "set-sink-volume @DEFAULT_SINK@ 0.031623",   # -30 dB
        "set-sink-mute @DEFAULT_SINK@ 0", "set-sink-volume @DEFAULT_SINK@ 1.000000",
        "set-sink-mute @DEFAULT_SINK@ 0", "set-sink-volume @DEFAULT_SINK@ 0.001540",   # first step, -56.25 dB
        "set-sink-mute @DEFAULT_SINK@ 1"]


def test_the_airplay_stream_is_found_through_alsa_too():
    through_alsa = [{"index": 7, "properties": {"application.name": "PipeWire ALSA [shairport-sync]",
                                                "node.name": "alsa_playback.shairport-sync"},
                     "sample_specification": "s16le 2ch 44100Hz", "corked": False}]
    assert airplay.stream(through_alsa)["format"] == "s16le 2ch 44100Hz"


async def test_mpris_fills_the_track_when_the_pipe_has_not_said_it(monkeypatch):
    reply = {"type": "a{sv}", "data": {
        "xesam:title": {"type": "s", "data": "Clair de Lune"},
        "xesam:artist": {"type": "as", "data": ["Debussy", "Kocsis"]},
        "xesam:album": {"type": "s", "data": ""},
        "mpris:artUrl": {"type": "s", "data": "file:///run/user/1000/cover.jpg"}}}

    async def run(*argv, timeout=10.0):
        return 0, json.dumps(reply)
    monkeypatch.setattr(airplay, "_run", run)
    assert await airplay.mpris_metadata() == {"title": "Clair de Lune", "artist": "Debussy, Kocsis",
                                              "art_url": "file:///run/user/1000/cover.jpg"}

    async def gone(*argv, timeout=10.0):
        return 1, "no such service"
    monkeypatch.setattr(airplay, "_run", gone)
    assert await airplay.mpris_metadata() == {}
