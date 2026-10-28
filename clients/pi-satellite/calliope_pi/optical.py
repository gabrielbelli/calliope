"""The optical input of a C-Media CM6206 card (StarTech ICUSBAUDIO7D and
its kin), played to the speakers by the card itself.

WHY THE CARD AND NOT PIPEWIRE. The chip mutes what it records from S/PDIF
when the stream is marked copy-prohibited (SCMS), which a TV's optical out
usually is: the capture locks and every sample is zero. Its own mix of the
S/PDIF input into the analogue outputs (REG1 bit 0, "SPDIF In Mix Enable")
is not muted, and adds no delay. The cost is that the TV never passes
through PipeWire, so the only thing that changes its level is the card's
own output volume (the ALSA "Speaker" control), which the voice and AirPlay
go through as well.

TWO VOLUMES. bundle/wireplumber/calliope-zz-optical.conf gives this card a
software mixer, so the satellite's `volume` (and the phone's slider) moves
only PipeWire's own audio, and `optical_volume` here is the card's: the
TV's, and the ceiling for everything else.

DUCKING. While the satellite speaks (or the hub asks), the card's volume
goes down by the duck and the agent's own streams (media.role "Assistant")
go up by as much as the output's software volume leaves room for, so the
voice keeps its level while the TV dips. Other streams are not lowered as
well: the card already lowers them.

Registers are read and written over the card's HID interface (cm6206ctl's
protocol: [report 0, 0x30 read | 0x20 write, low, high, register]);
bundle/udev gives the audio group that hidraw node."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import select
from pathlib import Path

from . import airplay
from .pipewire import _run

log = logging.getLogger("calliope.optical")

USB_ID = "0d8c:0102"
HID_ID = "0003:00000D8C:00000102"
CONTROL = "Speaker"
REG_MIX, MIX_ON = 1, 0x0001
SYS = Path("/sys")
ASOUND = Path("/proc/asound")
BOOST_EVERY_S = 0.1
DB = re.compile(r"\[(-?[0-9]+(?:\.[0-9]+)?)dB\]")


def card(asound: Path = ASOUND) -> str | None:
    """The ALSA card number of the first CM6206, or None without one."""
    for usbid in sorted(asound.glob("card*/usbid")):
        with contextlib.suppress(OSError):
            if usbid.read_text().strip() == USB_ID:
                return usbid.parent.name.removeprefix("card")
    return None


def hidraw(sys_: Path = SYS) -> Path | None:
    for node in sorted((sys_ / "class/hidraw").glob("hidraw*")):
        with contextlib.suppress(OSError):
            if f"HID_ID={HID_ID}" in (node / "device/uevent").read_text():
                return Path("/dev") / node.name
    return None


def _register(path: Path, reg: int, value: int | None = None) -> int | None:
    """Write `value` to the register when given, then read it back."""
    fd = os.open(path, os.O_RDWR)
    try:
        if value is not None:
            os.write(fd, bytes([0, 0x20, value & 0xFF, value >> 8, reg]))
        os.write(fd, bytes([0, 0x30, 0, 0, reg]))
        for _ in range(5):
            if not select.select([fd], [], [], 0.5)[0]:
                break
            b = os.read(fd, 8)
            if len(b) >= 3 and b[0] & 0xE0 == 0x20:
                return b[2] << 8 | b[1]
        return None
    finally:
        os.close(fd)


def present() -> bool:
    return card() is not None and hidraw() is not None


def headroom(sink_volume: float | None, level: float) -> float:
    """How far the agent's own streams go up while the card is down to
    `level`: back to where they were, or as far as the output's software
    volume (wpctl's 0-1, cubic) leaves before they would clip."""
    room = 1 / max(sink_volume, 1e-3) ** 3 if sink_volume is not None else 1.0
    return max(1.0, min(1 / max(level, 1e-3), room))


class Optical:
    """The card's mix and volume, applied where a CM6206 is plugged in."""

    def __init__(self) -> None:
        self.enabled = False
        self.volume = 0            # 0-100, ALSA's mapped scale (amixer -M)
        self.level: float | None = None
        self.base_db: float | None = None   # the card's volume in dB, not ducked
        self.gain = 1.0
        self.boosted: dict[int, float] = {}
        self._task: asyncio.Task | None = None

    async def _amixer(self, *args: str) -> tuple[int, str]:
        c = card()
        if c is None:
            return 1, "no CM6206"
        return await _run("amixer", "-c", c, *args)

    async def _mix(self, on: bool) -> bool:
        path = hidraw()
        if path is None:
            return False
        def flip() -> int | None:
            now = _register(path, REG_MIX)
            if now is None:
                return None
            want = now | MIX_ON if on else now & ~MIX_ON
            return now if want == now else _register(path, REG_MIX, want)
        try:
            got = await asyncio.to_thread(flip)
        except OSError as e:
            log.warning("could not reach the card's registers (%s): %s", path, e)
            return False
        return got is not None and bool(got & MIX_ON) == on

    async def _set_volume(self, pct: int) -> None:
        code, out = await self._amixer("-q", "-M", "sset", CONTROL, f"{pct}%")
        if code:
            log.warning("amixer %s %d%%: %s", CONTROL, pct, out.strip()[:200])
        self.base_db = await self._db()

    async def _db(self) -> float | None:
        code, out = await self._amixer("sget", CONTROL)
        m = DB.search(out) if code == 0 else None
        return float(m.group(1)) if m else None

    async def apply(self, enabled: bool, volume: int) -> None:
        """The mix on or off and the card's volume, as the settings say. Again
        after the card comes back: its registers reset when it is plugged in."""
        if card() is None:
            self.enabled = False
            return
        self.enabled, self.volume = enabled, max(0, min(100, int(volume)))
        if not await self._mix(enabled):
            log.warning("the card's optical mix did not take %s", "on" if enabled else "off")
        await self._set_volume(self.volume)
        if self.level is not None:
            await self._duck_card(self.level)

    async def _duck_card(self, level: float) -> None:
        if self.base_db is None:
            self.base_db = await self._db()
        if self.base_db is not None:
            db = self.base_db + 20 * math.log10(max(level, 1e-3))
            await self._amixer("-q", "--", "sset", CONTROL, f"{db:.2f}dB")

    async def duck(self, level: float | None, sink_volume: float | None) -> None:
        """Down to `level` (0-1, linear), or None to put the volume back."""
        if level == self.level:
            return
        self.level = level
        if level is None:
            if self._task is not None:
                self._task.cancel()
                self._task = None
            await self._set_volume(self.volume)
            await self._unboost()
            return
        self.gain = headroom(sink_volume, level)
        await self._duck_card(level)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._hold())

    async def _hold(self) -> None:
        """Raises each of the agent's own streams as it appears."""
        while self.level is not None:
            for s in await airplay.sink_inputs():
                props = s.get("properties") or {}
                idx = s.get("index")
                if props.get("media.role") == airplay.OURS and idx not in self.boosted:
                    self.boosted[idx] = self.gain
                    await _run("pactl", "set-sink-input-volume", str(idx), f"{self.gain:.3f}")
            await asyncio.sleep(BOOST_EVERY_S)

    async def _unboost(self) -> None:
        boosted, self.boosted = self.boosted, {}
        present_ = {s.get("index") for s in await airplay.sink_inputs()}
        for idx in boosted:
            if idx in present_:
                await _run("pactl", "set-sink-input-volume", str(idx), "1.000")
