"""The small parts: frames, PipeWire's device list, its players and volumes,
the state file, the board's readings and the Wi-Fi setup page."""

from __future__ import annotations

import json
import struct
import time
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
                                    "description": "Built-in Audio Stereo", "api": "alsa", "quality": None,
                                    "jack": None}
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


def test_a_media_frame_is_parsed_with_its_sequence():
    f = frames.parse(struct.pack("<BBBBIQ", 5, 0, 2, 0, 41, 0) + b"\x01\x00\x02\x00")
    assert (f.kind, f.seq, f.payload) == (frames.MEDIA, 41, b"\x01\x00\x02\x00")
    assert frames.parse(b"\x05short") is None


class FakeProc:
    """A pw-cat that takes everything and ends when asked."""
    returncode = None

    def __init__(self):
        self.stdin = self

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    def kill(self):
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode or 0


async def test_a_stereo_player_opens_a_two_channel_stream_and_counts_its_time_in_frames(monkeypatch):
    started = []

    async def spawn(*argv, **kw):
        started.append(argv)
        return FakeProc()
    monkeypatch.setattr(pipewire.asyncio, "create_subprocess_exec", spawn)
    p = pipewire.Player(channels=2, role="Music")
    pcm = b"\x00\x00" * 2 * 882            # 20 ms of stereo at 44.1 kHz: 3,528 bytes
    before = time.monotonic()
    await p.write(pcm)
    after = time.monotonic()
    [argv] = started
    assert argv[argv.index("--channels") + 1] == "2" and argv[argv.index("--rate") + 1] == "44100"
    assert json.loads(argv[argv.index("--properties") + 1]) == {"media.role": "Music"}
    # A frame is two channels of two bytes: 3,528 bytes are 20 ms, not 40.
    assert before + len(pcm) / 4 / 44100 <= p.until <= after + len(pcm) / 4 / 44100
    await p.flush()


async def test_get_volume_reads_wpctl_and_ignores_muted(monkeypatch):
    asked, answers = [], iter([(0, "Volume: 0.32\n"), (0, "Volume: 0.32 [MUTED]\n"), (0, "Volume: 1.00\n"),
                               (1, "Object not found\n"), (0, "nonsense\n")])

    async def run(*argv, timeout=5.0):
        asked.append(argv)
        return next(answers)
    monkeypatch.setattr(pipewire, "_run", run)
    sink = pipewire.Device("alsa_output.usb", "USB Audio", "alsa", 41)
    assert await pipewire.get_volume(sink) == 0.32
    assert await pipewire.get_volume(sink) == 0.32, "a mute is not a volume"
    assert await pipewire.get_volume(None) == 1.0
    assert await pipewire.get_volume(sink) is None and await pipewire.get_volume(sink) is None
    assert asked[0] == ("wpctl", "get-volume", "41") and asked[2] == ("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")


async def test_setting_the_volume_unmutes_the_output(monkeypatch):
    asked = []

    async def run(*argv, timeout=5.0):
        asked.append(argv)
        return 0, ""
    monkeypatch.setattr(pipewire, "_run", run)
    assert await pipewire.set_volume(pipewire.Device("alsa_output.usb", "USB Audio", "alsa", 41), 0.4)
    await pipewire.set_volume(None, 1.2)
    assert asked == [("wpctl", "set-volume", "41", "0.400"), ("wpctl", "set-mute", "41", "0"),
                     ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.000"),
                     ("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0")]


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


REALTEK = """Realtek Realtek USB2.0 Audio at usb-3f980000.usb-1.1.3, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S16_LE
    Channels: 2
    Rates: 44100, 48000, 96000, 192000, 384000
    Bits: 16
  Interface 1
    Altset 3
    Format: S32_LE
    Channels: 2
    Rates: 44100, 48000, 96000, 192000, 384000
    Bits: 32

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: S16_LE
    Channels: 1
    Rates: 48000
    Bits: 16
"""


def test_each_output_says_what_hardware_it_is_and_what_it_can_do(tmp_path):
    (tmp_path / "card2").mkdir()
    (tmp_path / "card2" / "stream0").write_text(REALTEK)
    (tmp_path / "card0").mkdir()
    (tmp_path / "card3").mkdir()
    (tmp_path / "card3" / "id").write_text("sndrpihifiberry\n")
    usb = pipewire.Device("alsa_output.usb-Realtek", "Realtek USB2.0 Audio", "alsa", 51, 2, "Realtek USB2.0 Audio")
    jack = pipewire.Device("alsa_output.platform-mailbox", "Built-in Audio", "alsa", 40, 0, "bcm2835 Headphones")
    hdmi = pipewire.Device("alsa_output.hdmi", "Built-in HDMI", "alsa", 41, 1, "vc4-hdmi")
    hat = pipewire.Device("alsa_output.hat", "HiFiBerry DAC+", "alsa", 42, 3, "snd_rpi_hifiberry_dacplus")
    bt = pipewire.Device("bluez_output.x", "Speaker", "bluez5", 43)
    assert pipewire.quality(usb, "playback", tmp_path) == {
        "kind": "usb", "dac": True, "formats": ["S16_LE", "S32_LE"], "bits": [16, 32],
        "rates": [44100, 48000, 96000, 192000, 384000]}
    assert pipewire.quality(usb, "capture", tmp_path)["rates"] == [48000]
    assert pipewire.quality(jack, "playback", tmp_path) == {"kind": "pwm", "dac": False, "bits": [16],
                                                            "rates": [48000]}
    assert pipewire.quality(hdmi, "playback", tmp_path)["kind"] == "hdmi"
    assert pipewire.quality(hat, "playback", tmp_path) == {"kind": "i2s", "dac": True}
    assert pipewire.quality(bt, "playback", tmp_path) is None


def test_the_card_behind_each_node_comes_from_pipewire():
    dump = [{"id": 40, "type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Sink", "node.name": "alsa_output.platform-mailbox", "device.api": "alsa",
        "node.description": "Built-in Audio Stereo", "alsa.card": 0, "alsa.card_name": "bcm2835 Headphones"}}}]
    [jack] = pipewire.parse_dump(dump).sinks
    assert (jack.card, jack.card_name) == (0, "bcm2835 Headphones")


def test_a_card_that_detects_its_jack_says_whether_something_is_plugged_in():
    listed = [{"name": "alsa_output.usb-Realtek", "active_port": "analog-output-headphones",
               "ports": [{"name": "analog-output-headphones", "availability": "available"}]},
              {"name": "alsa_output.usb-other", "active_port": "analog-output-headphones",
               "ports": [{"name": "analog-output-headphones", "availability": "not available"}]},
              {"name": "alsa_output.platform-mailbox", "active_port": "analog-output",
               "ports": [{"name": "analog-output", "availability": "availability unknown"}]}]
    assert pipewire.jacks(listed) == {"alsa_output.usb-Realtek": "plugged", "alsa_output.usb-other": "unplugged",
                                      "alsa_output.platform-mailbox": None}
    assert pipewire.jacks("nonsense") == {}
