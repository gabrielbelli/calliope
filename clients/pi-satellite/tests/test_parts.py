"""The small parts: frames, PipeWire's device list, the state file, the
board's readings and the Wi-Fi setup page."""

from __future__ import annotations

import json
import struct
from array import array

from calliope_pi import frames, pipewire, portal, state, system

DUMP = [
    {"id": 31, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
     "metadata": [{"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON",
                   "value": {"name": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo"}},
                  {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
                   "value": {"name": "alsa_input.usb-Generic_USB_Audio-00.mono-fallback"}}]},
    {"id": 40, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Sink", "node.name": "alsa_output.platform-bcm2835_audio.stereo-fallback",
        "node.description": "Built-in Audio Stereo", "device.api": "alsa"}}},
    {"id": 41, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Sink", "node.name": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
        "node.description": "USB Audio Analog Stereo", "device.api": "alsa"}}},
    {"id": 42, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Source", "node.name": "alsa_input.usb-Generic_USB_Audio-00.mono-fallback",
        "node.description": "USB Audio Mono", "device.api": "alsa"}}},
    {"id": 50, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Stream/Output/Audio", "node.name": "shairport-sync"}}},
    {"id": 51, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Video/Source", "node.name": "v4l2_input.camera"}}},
]


def test_pipewires_outputs_and_inputs_are_listed_with_the_defaults_and_nothing_else():
    d = pipewire.parse_dump(DUMP)
    assert [s.description for s in d.sinks] == ["Built-in Audio Stereo", "USB Audio Analog Stereo"]
    assert [s.name for s in d.sources] == ["alsa_input.usb-Generic_USB_Audio-00.mono-fallback"]
    assert d.default_sink.startswith("alsa_output.usb") and d.sink(d.default_sink).id == 41
    assert d.view()["sinks"][0] == {"name": "alsa_output.platform-bcm2835_audio.stereo-fallback",
                                    "description": "Built-in Audio Stereo", "api": "alsa"}
    assert pipewire.parse_dump("not a list") == pipewire.NONE


def test_the_reference_is_channel_zero_and_the_microphone_channel_one():
    ref, mic = array("h", [1, 2, 3]).tobytes(), array("h", [-1, -2, -3]).tobytes()
    out = array("h")
    out.frombytes(pipewire.interleave(ref, mic))
    assert list(out) == [1, -1, 2, -2, 3, -3]


def test_pw_cat_is_asked_for_raw_s16_with_the_role_and_the_sink_monitor():
    argv = pipewire._cat("record", 16000, 1, "alsa_output.x", None, capture_sink=True)
    assert argv[:4] == ["pw-cat", "--record", "--raw", "--format"]
    assert argv[argv.index("--target") + 1] == "alsa_output.x"
    assert json.loads(argv[argv.index("--properties") + 1]) == {"stream.capture.sink": True}
    play = pipewire._cat("playback", 48000, 1, None, "Assistant")
    assert "--target" not in play and json.loads(play[play.index("--properties") + 1]) == {
        "media.role": "Assistant"}
    assert play[-1] == "-"


def test_frames_have_the_hubs_layout():
    f = frames.mic(7, 2, 123456, b"\x01\x00\x02\x00")
    assert f[:16] == struct.pack("<BBBBIQ", 1, 0, 2, 0, 7, 123456) and f[16:] == b"\x01\x00\x02\x00"
    sp = frames.parse(struct.pack("<BBBBIQ", 2, 0, 1, 0, 9, 0) + b"pcm")
    assert (sp.kind, sp.seq, sp.payload) == (frames.SPEAKER, 9, b"pcm")
    fw = frames.parse(struct.pack("<BBBBI", 3, 0, 0, 0, 8192) + b"chunk")
    assert (fw.kind, fw.offset, fw.payload) == (frames.FIRMWARE, 8192, b"chunk")
    assert frames.parse(b"\x02short") is None and frames.parse(b"") is None


def test_the_state_survives_a_restart_and_takes_only_known_settings():
    s = state.load()
    assert not s.adopted() and s.config["volume"] == 60
    s.hub, s.token, s.name = "wss://hub.example", "t0k", "Kitchen speaker"
    assert s.apply({"volume": 35, "audio_sink": "alsa_output.x", "lights_enabled": False}) == {
        "volume": 35, "audio_sink": "alsa_output.x"}
    state.save(s)
    again = state.load()
    assert (again.hub, again.token, again.name, again.config["volume"]) == (
        "wss://hub.example", "t0k", "Kitchen speaker", 35)
    assert oct(state.paths.state_file().stat().st_mode & 0o777) == "0o600"


def test_readings_are_none_where_the_board_has_no_such_thing(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "PROC", tmp_path)
    monkeypatch.setattr(system, "SYS", tmp_path)
    assert system.rssi() is None and system.temperature_c() is None and system.board() is None
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "wireless").write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        " wlan0: 0000   58.  -52.  -256        0      0      0\n")
    assert system.rssi() == -52
    (tmp_path / "class" / "net" / "wlan0").mkdir(parents=True)
    (tmp_path / "class" / "net" / "wlan0" / "address").write_text("b8:27:eb:12:34:56\n")
    assert system.satellite_id() == "b827eb123456"
    assert system.under_voltage() is None
    hw = tmp_path / "class" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("rpi_volt\n")
    (hw / "in0_lcrit_alarm").write_text("0\n")
    assert system.under_voltage() is False
    (hw / "in0_lcrit_alarm").write_text("1\n")
    assert system.under_voltage() is True


def test_the_wifi_password_goes_into_a_keyfile_and_an_open_network_has_none():
    secure = portal.keyfile("Home 5G", "hunter2 pass")
    assert "ssid=Home 5G" in secure and "key-mgmt=wpa-psk" in secure and "psk=hunter2 pass" in secure
    assert "psk=" not in portal.keyfile("Cafe", "")


def test_the_setup_page_escapes_what_the_air_says(monkeypatch):
    monkeypatch.setattr(portal, "ap_name", lambda: "calliope-sat-3456")
    p = portal.Portal()
    p.networks = [{"ssid": '<script>x</script>"', "signal": 70, "secure": True, "band": "5 GHz"}]
    p.error = "Could not join <b>"
    page = p.page()
    assert "<script>x" not in page and "&lt;script&gt;" in page and "Could not join &lt;b&gt;" in page
    assert "calliope-sat-3456" in page and "5 GHz, 70%" in page


def test_the_setup_network_opens_only_after_three_minutes_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "OFFLINE_FILE", tmp_path / "offline-since")
    monkeypatch.setattr(portal, "online", lambda: False)
    assert portal.offline_long_enough(now=1000.0) is False
    assert portal.offline_long_enough(now=1000.0 + portal.OFFLINE_S - 1) is False
    assert portal.offline_long_enough(now=1000.0 + portal.OFFLINE_S) is True
    monkeypatch.setattr(portal, "online", lambda: True)
    assert portal.offline_long_enough(now=5000.0) is False and not (tmp_path / "offline-since").exists()
