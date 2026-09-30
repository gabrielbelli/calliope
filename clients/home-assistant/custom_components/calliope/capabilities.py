"""What a satellite has, and so which entities it gets.

The hub reports each satellite's hello.caps: live while it is connected, the
last ones it saw while it is offline, and {} for one it has never seen. The
caps decide the hardware, never the model name or whether a status key
happens to be there. This module is the only place the entity matrix lives:
the platforms, the coordinator's pruning, device triggers and diagnostics all
ask it.

Unknown caps ({}) are not the same as absent ones. A satellite whose caps are
unknown gets only what every satellite has, and nothing of it is removed,
because guessing another model's hardware is what gave the Pi the Korvo's
buttons.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import Platform

# A button id the hub may name, as the hub's own ButtonsBody checks it. A
# stranger id is left out rather than made into an entity.
BUTTON_ID = re.compile(r"^[a-z0-9_-]{1,32}$")

# What every satellite gets, whatever it has.
ALWAYS: dict[str, Platform] = {
    "online": Platform.BINARY_SENSOR,
    "rssi": Platform.SENSOR,
    "uptime": Platform.SENSOR,
    "restart": Platform.BUTTON,
    "firmware": Platform.UPDATE,
}


def caps_of(sat: dict[str, Any] | None) -> dict[str, Any]:
    """The satellite's caps; {} when the hub has never seen them."""
    caps = (sat or {}).get("caps")
    return caps if isinstance(caps, dict) else {}


def known(sat: dict[str, Any] | None) -> bool:
    """Whether the hub knows what the satellite has."""
    return bool(caps_of(sat))


def has(caps: dict[str, Any], name: str) -> bool:
    """The gating rule the hub, the page and this integration share: a cap
    counts when it is present and truthy. A list counts when it is not empty,
    a number when it is above 0, a dict or true always."""
    value = caps.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return True
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, (list, tuple, str)):
        return len(value) > 0
    return False


def _listed(caps: dict[str, Any], name: str) -> list[Any]:
    value = caps.get(name)
    return list(value) if isinstance(value, (list, tuple)) else []


def buttons_of(caps: dict[str, Any]) -> list[str]:
    """The buttons the satellite says it has, in its own order. There is no
    fallback: a satellite that lists none has none."""
    return [
        b for b in _listed(caps, "buttons") if isinstance(b, str) and BUTTON_ID.match(b)
    ]


def wanted(sat: dict[str, Any] | None) -> dict[str, Platform]:
    """Every entity the satellite should have, as key (the unique id's
    suffix) to platform. Unknown caps give only the ALWAYS rows."""
    want = dict(ALWAYS)
    caps = caps_of(sat)
    if not caps:
        return want
    speaker, mic, lights = has(caps, "speaker"), has(caps, "mic"), has(caps, "lights")
    if speaker:
        want["media_player"] = Platform.MEDIA_PLAYER
        want["speaker"] = Platform.SWITCH
        want["output_satellite"] = Platform.SELECT
    if speaker or lights:
        # The Korvo blinks its ring; the Pi, which has none, chimes.
        want["identify"] = Platform.BUTTON
    if mic:
        want["microphone"] = Platform.SWITCH
        want["mic_gain"] = Platform.NUMBER
        want["voice"] = Platform.EVENT
        want["last_command"] = Platform.SENSOR
        want["last_wake_word"] = Platform.SENSOR
    if mic and speaker:
        want["assist_satellite"] = Platform.ASSIST_SATELLITE
    if lights:
        want["lights"] = Platform.SWITCH
        want["brightness"] = Platform.NUMBER
    if "mute" in _listed(caps, "actions"):
        # Only a board with a privacy mute button can be muted that way.
        want["privacy_mute"] = Platform.BINARY_SENSOR
    for button in buttons_of(caps):
        want[f"button_{button}"] = Platform.EVENT
    if has(caps, "audio_devices"):
        want["output_device"] = Platform.SELECT
        if mic:
            want["input_device"] = Platform.SELECT
            want["echo_reference"] = Platform.SWITCH
    if has(caps, "airplay"):
        want["airplay"] = Platform.SWITCH
        want["airplay_name"] = Platform.TEXT
    health = _listed(caps, "health")
    if "temp_c" in health:
        want["cpu_temperature"] = Platform.SENSOR
    if "under_voltage" in health:
        want["under_voltage"] = Platform.BINARY_SENSOR
    return want
