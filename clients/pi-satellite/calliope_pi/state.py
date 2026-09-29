"""What the satellite remembers between starts: the hub, its adoption token,
the name it was given and the settings the hub sent. One JSON file, written
whole and renamed into place, so a power cut leaves the old one or the new."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from . import paths

DEFAULT_CONFIG = {
    "volume": 60,              # 0-100, the output's own volume
    "mic_gain_db": 0.0,        # added to the input's own level
    "mic_enabled": True,
    "speaker_enabled": True,
    "audio_sink": None,        # node.name of the output; None keeps PipeWire's default
    "audio_source": None,      # node.name of the microphone; None keeps the default
    "echo_reference": True,    # send what the output plays as channel 0
    "airplay_enabled": True,   # an AirPlay receiver (airplay.py), where shairport-sync is installed
    "airplay_name": None,      # what phones list it as; None is the satellite's own name
    "airplay_volume": 70,      # % of the phone's slider a session starts at after a minute idle
}
CONFIG_KEYS = frozenset(DEFAULT_CONFIG)


@dataclass
class State:
    hub: str | None = None
    token: str = ""
    name: str = ""
    config: dict = field(default_factory=lambda: dict(DEFAULT_CONFIG))

    def adopted(self) -> bool:
        return bool(self.token)

    def apply(self, changes: dict) -> dict:
        """Take the settings the hub sent that this satellite has; the ones
        that changed, with their new values."""
        changed = {}
        for key, value in (changes or {}).items():
            if key in CONFIG_KEYS and self.config.get(key) != value:
                self.config[key] = value
                changed[key] = value
        return changed


def load() -> State:
    try:
        raw = json.loads(paths.state_file().read_text())
    except (OSError, ValueError):
        raw = {}
    s = State()
    if isinstance(raw, dict):
        s.hub = raw.get("hub") if isinstance(raw.get("hub"), str) else None
        s.token = raw.get("token") if isinstance(raw.get("token"), str) else ""
        s.name = raw.get("name") if isinstance(raw.get("name"), str) else ""
        if isinstance(raw.get("config"), dict):
            s.config = dict(DEFAULT_CONFIG) | {k: v for k, v in raw["config"].items() if k in CONFIG_KEYS}
    return s


def save(s: State) -> None:
    path = paths.state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=1) + "\n")
    os.chmod(tmp, 0o600)  # the token
    os.replace(tmp, path)
