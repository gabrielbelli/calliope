"""What the board says about itself: its id, model, and the health figures
the status message carries. Each reader returns None where the board has no
such thing, so the agent runs on a laptop too."""

from __future__ import annotations

import re
import time
from pathlib import Path

PROC = Path("/proc")
SYS = Path("/sys")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def satellite_id(interfaces: tuple[str, ...] = ("wlan0", "eth0", "end0")) -> str:
    """The MAC of the first interface that has one, without colons, as the
    ESP32 reports its own: the hub knows a satellite by it."""
    for name in interfaces:
        mac = _read(SYS / "class" / "net" / name / "address")
        if mac and mac.strip() not in ("", "00:00:00:00:00:00"):
            return mac.strip().replace(":", "").lower()
    for path in sorted((SYS / "class" / "net").glob("*")):
        mac = _read(path / "address")
        if path.name != "lo" and mac and mac.strip() not in ("", "00:00:00:00:00:00"):
            return mac.strip().replace(":", "").lower()
    raise RuntimeError("no network interface with a MAC address")


def board() -> str | None:
    """"Raspberry Pi 3 Model B Plus Rev 1.3", from the device tree."""
    text = _read(PROC / "device-tree" / "model")
    return text.strip("\x00\n ") if text else None


def uptime_s() -> int | None:
    text = _read(PROC / "uptime")
    return int(float(text.split()[0])) if text else None


def rssi(interface: str = "wlan0") -> int | None:
    """The Wi-Fi signal in dBm, from /proc/net/wireless; None on Ethernet."""
    text = _read(PROC / "net" / "wireless")
    if not text:
        return None
    for line in text.splitlines():
        if line.strip().startswith(interface + ":"):
            fields = line.split()
            try:
                return int(float(fields[3].rstrip(".")))
            except (IndexError, ValueError):
                return None
    return None


def memory_available() -> int | None:
    """Bytes the kernel could give a program now (MemAvailable), reported as
    `heap` so the Satellites tab and telemetry read it as they read the
    ESP32's free heap."""
    text = _read(PROC / "meminfo")
    m = re.search(r"^MemAvailable:\s+(\d+) kB", text or "", re.M)
    return int(m.group(1)) * 1024 if m else None


def temperature_c() -> float | None:
    text = _read(SYS / "class" / "thermal" / "thermal_zone0" / "temp")
    try:
        return round(int(text) / 1000, 1) if text else None
    except ValueError:
        return None


def throttled() -> int | None:
    """The firmware's throttling flags (vcgencmd get_throttled), from sysfs:
    0 is healthy; bit 0 under-voltage now, bit 2 throttled now, bits 16-19
    the same since boot. A Pi 3 on a weak supply shows it here first."""
    text = _read(SYS / "devices" / "platform" / "soc" / "soc:firmware" / "get_throttled")
    try:
        return int(text.strip(), 16) if text else None
    except ValueError:
        return None


def under_voltage() -> bool | None:
    """Whether the supply is below what the board needs now, from the
    firmware's hwmon (rpi_volt, in0_lcrit_alarm): the kernels without
    get_throttled have this. A Pi 3 on a weak supply browns out Wi-Fi and
    USB audio first; None where the board has no such sensor."""
    for hw in sorted((SYS / "class" / "hwmon").glob("hwmon*")):
        if (_read(hw / "name") or "").strip() == "rpi_volt":
            alarm = _read(hw / "in0_lcrit_alarm")
            return alarm.strip() == "1" if alarm else None
    return None


class Clock:
    """Microseconds since the agent started, for the mic frames' capture time."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def us(self) -> int:
        return int((time.monotonic() - self.t0) * 1_000_000)
