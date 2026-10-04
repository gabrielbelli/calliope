"""A Raspberry Pi (or any Linux board with PipeWire) as a Calliope satellite.

The agent speaks the hub's satellite protocol (services/satellites/README.md,
"The device protocol") over one WebSocket, as the ESP32 firmware does: it says
hello with what it can do, is adopted from the Satellites tab, plays what the
hub sends to the output it is told to use, streams the microphone if it has
one, and installs signed updates the hub pushes.

Everything audio goes through PipeWire, so any device Linux supports (the
board's own jack, HDMI, a USB sound card or microphone, a DAC HAT, later
Bluetooth) can be the input or the output.

Runs on the Python 3 of Raspberry Pi OS with python3-websockets and
python3-cryptography, nothing from pip.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def version() -> str:
    """The release this code came in, from its bundle's manifest.json; "dev"
    when it runs from a checkout."""
    try:
        return json.loads((HERE.parent / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "dev"
