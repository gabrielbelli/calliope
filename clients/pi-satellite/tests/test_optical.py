"""The optical input of a CM6206 card: finding the card, its mix register,
its volume, and ducking with the card while the voice keeps its level."""

from __future__ import annotations

import asyncio
import math

import pytest

from calliope_pi import agent as agentmod
from calliope_pi import airplay, optical, state


@pytest.fixture
def card(tmp_path, monkeypatch):
    """A CM6206 as card 2 and hidraw0, a fake amixer that keeps the card's
    volume in dB, and the register reads and writes it was sent."""
    asound = tmp_path / "asound"
    (asound / "card0").mkdir(parents=True)
    (asound / "card0" / "usbid").write_text("0bda:4014\n")
    (asound / "card2").mkdir()
    (asound / "card2" / "usbid").write_text("0d8c:0102\n")
    hid = tmp_path / "sys" / "class" / "hidraw" / "hidraw0" / "device"
    hid.mkdir(parents=True)
    hid.joinpath("uevent").write_text("DRIVER=hid-generic\nHID_ID=0003:00000D8C:00000102\n")
    monkeypatch.setattr(optical, "ASOUND", asound)
    monkeypatch.setattr(optical, "SYS", tmp_path / "sys")
    monkeypatch.setattr(optical.card, "__defaults__", (asound,))
    monkeypatch.setattr(optical.hidraw, "__defaults__", (tmp_path / "sys",))
    world = {"regs": {1: 0x3000}, "db": -20.0, "ran": [], "inputs": []}

    def register(path, reg, value=None):
        if value is not None:
            world["regs"][reg] = value
        return world["regs"].get(reg)

    async def run(*argv, timeout=5.0):
        world["ran"].append(argv)
        if argv[0] == "amixer" and "sget" in argv:
            return 0, f"  Front Left: Playback 99 [50%] [{world['db']:.2f}dB] [on]\n"
        if argv[0] == "amixer" and "sset" in argv:
            value = argv[-1]
            if value.endswith("dB"):
                world["db"] = float(value[:-2])
            elif value.endswith("%"):
                world["db"] = -60 + 0.6 * int(value[:-1])
        return 0, ""

    async def sink_inputs():
        return world["inputs"]

    monkeypatch.setattr(optical, "_register", register)
    monkeypatch.setattr(optical, "_run", run)
    monkeypatch.setattr(airplay, "sink_inputs", sink_inputs)
    return world


def own(index):
    return {"index": index, "properties": {"media.role": "Assistant"}, "volume": {}}


def test_the_card_is_found_by_its_usb_id(card):
    assert optical.card() == "2"
    assert str(optical.hidraw()) == "/dev/hidraw0"
    assert optical.present()


def test_no_card_no_optical(tmp_path, monkeypatch):
    monkeypatch.setattr(optical.card, "__defaults__", (tmp_path / "none",))
    assert optical.card() is None and not optical.present()


def test_the_voice_goes_back_up_only_as_far_as_the_output_leaves_room():
    assert optical.headroom(1.0, 0.2) == 1.0                     # at full: no room
    assert optical.headroom(0.5, 0.2) == pytest.approx(5.0)        # 1/0.125 = 8, the duck asks 5
    assert optical.headroom(0.8, 0.2) == pytest.approx(1 / 0.512)  # room for less than the duck
    assert optical.headroom(None, 0.2) == 1.0


async def test_the_mix_and_the_volume_are_set_and_set_again(card):
    o = optical.Optical()
    await o.apply(True, 50)
    assert card["regs"][1] == 0x3001 and o.enabled
    assert ("amixer", "-c", "2", "-q", "-M", "sset", "Speaker", "50%") in card["ran"]
    await o.apply(False, 50)
    assert card["regs"][1] == 0x3000 and not o.enabled


async def test_ducking_lowers_the_card_and_raises_the_voice_then_puts_both_back(card):
    o = optical.Optical()
    await o.apply(True, 50)            # -30 dB on the fake's scale
    card["inputs"] = [own(7), {"index": 3, "properties": {"media.role": "Music"}, "volume": {}}]
    await o.duck(0.2, 0.5)
    await asyncio.sleep(0.05)
    assert card["db"] == pytest.approx(-30 + 20 * math.log10(0.2), abs=0.01)
    assert ("pactl", "set-sink-input-volume", "7", "5.000") in card["ran"]
    assert not any(a[:3] == ("pactl", "set-sink-input-volume", "3") for a in card["ran"]), \
        "the card already lowers the other streams"
    await o.duck(0.5, 0.5)             # a different duck starts from the card's own volume
    assert card["db"] == pytest.approx(-30 + 20 * math.log10(0.5), abs=0.01)
    await o.duck(None, None)
    assert card["db"] == pytest.approx(-30)
    assert card["ran"][-1] == ("pactl", "set-sink-input-volume", "7", "1.000")


async def test_with_the_mix_on_the_agent_ducks_with_the_card(card, monkeypatch):
    monkeypatch.setattr(agentmod.system, "satellite_id", lambda: "b827eb121359")
    a = agentmod.Agent(state.State(hub="ws://x", token="t", name="pi-edifier"))
    others, cards = [], []

    async def fake_set(level):
        others.append(level)

    async def fake_duck(level, sink_volume):
        cards.append(level)

    async def get_volume(dev):
        return 0.5
    a.ducker.set = fake_set
    a.optical.duck = fake_duck
    monkeypatch.setattr(agentmod.pipewire, "get_volume", get_volume)
    assert a.caps()["optical"] == {"version": 1}
    assert a.settings()["optical_enabled"] is True and a.settings()["optical_volume"] == 50
    await a.optical.apply(True, 50)
    await a._speaking(True)
    await a._speaking(False)
    assert cards == [agentmod.SPEAK_DUCK, None] and others == [None, None]
    await a.optical.apply(False, 50)
    await a._speaking(True)
    assert others[-1] == agentmod.SPEAK_DUCK and cards[-1] is None
