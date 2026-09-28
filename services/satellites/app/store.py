"""What survives a restart: adopted satellites and uploaded firmware images.

One JSON file and one directory under SATELLITES_DATA_DIR. A satellite's
adoption token is kept only as a SHA-256; the satellite holds the token itself,
so a copy of this file cannot impersonate a satellite to the hub.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG = {
    "volume": 60,
    "mic_gain_db": 30.0,
    "mic_enabled": True,
    "speaker_enabled": True,
    # Read by firmware from before button actions (2026-09-27), which keeps
    # VOL+/- to itself while this is on; current firmware takes
    # "button_actions" instead (DEVICE_ACTIONS).
    "local_volume_buttons": True,
    "lights_enabled": True,
    "brightness": 100,
    # How the ring is mounted: the LED at 12 o'clock (0-11), where a bar on
    # the ring starts, and whether the LEDs run anticlockwise as seen (upside
    # down), so that the bar still fills clockwise.
    "ring_top": 0,
    "ring_upside_down": False,
    # What each button does, on press and on release. The actions the
    # satellite runs itself (DEVICE_ACTIONS) go to it as "button_actions"; the
    # rest the hub runs when it hears of the press. Rec mutes, the volume pair
    # sets the volume, Play talks and Set stops, as before there was a choice.
    "buttons": {"rec": {"press": "mute"}, "vol_up": {"press": "volume_up"},
                "vol_down": {"press": "volume_down"}, "play": {"press": "ptt"},
                "set": {"press": "stop"}},
}
# Button actions the satellite runs itself: they work with the hub down, and
# only a button, never the hub, can undo the privacy mute.
DEVICE_ACTIONS = frozenset({"mute", "volume_up", "volume_down", "lights", "dimmer", "brighter"})
# Config the hub acts on itself and never sends to the satellite: the firmware
# would ignore it, and it would cost a JSON document on a board with 300 KB of
# heap.
HUB_ONLY = frozenset({"buttons"})


def _flag(v: object) -> bool:
    return isinstance(v, bool)


def _between(low: float, high: float, *, whole: bool = False):
    def ok(v: object) -> bool:
        # bool is an int to Python, and true is not a volume.
        kinds = int if whole else (int, float)
        return isinstance(v, kinds) and not isinstance(v, bool) and low <= v <= high
    return ok


# The settings a satellite reports about itself, in "status" and, on firmware
# from 2026-09-25, in "hello", with what a value must be to be believed. The
# ranges are PATCH's (main.ConfigBody). Anything else a satellite says --
# "buttons" above all, which is the hub's own -- is never taken into the
# config: anyone can open the socket and say anything before adoption.
REPORTED = {
    "volume": _between(0, 100, whole=True),
    "mic_gain_db": _between(0, 37.5),
    "mic_enabled": _flag,
    "speaker_enabled": _flag,
    "lights_enabled": _flag,
    "brightness": _between(1, 100, whole=True),
    "ring_top": _between(0, 11, whole=True),
    "ring_upside_down": _flag,
}
# What the hub's record says for a switch the satellite has not reported yet:
# off. Nothing is sent to a satellite, or heard from it, on a setting the hub
# made up; the satellite's first status puts in the real value.
UNREPORTED = {"mic_enabled": False, "speaker_enabled": False, "lights_enabled": False}


def reported_config(msg: dict | None) -> dict:
    """The settings in a satellite's hello or status that the hub believes."""
    return {k: v for k, v in (msg or {}).items() if k in REPORTED and REPORTED[k](v)}


def default_config() -> dict:
    """A deep copy: "buttons" is a dict of dicts, and a shallow copy would let
    one satellite's mapping be edited through another's."""
    return copy.deepcopy(DEFAULT_CONFIG)


def device_actions(buttons: dict | None) -> dict:
    """The part of a button mapping the satellite runs itself."""
    out: dict = {}
    for button, edges in (buttons or {}).items():
        for edge, act in (edges or {}).items():
            if act in DEVICE_ACTIONS:
                out.setdefault(button, {})[edge] = act
    return out


def with_button_defaults(config: dict) -> dict:
    """A mapping saved before button actions had no Rec (the firmware kept
    it) and no volume pair while the firmware kept those too: given them, so
    that the buttons do what they did."""
    buttons = {k: dict(v) for k, v in (config.get("buttons") or {}).items()}
    if not any(a == "mute" for edges in buttons.values() for a in edges.values()):
        buttons.setdefault("rec", {})["press"] = "mute"
    if config.get("local_volume_buttons", True):
        for key, act in (("vol_up", "volume_up"), ("vol_down", "volume_down")):
            if key not in buttons:
                buttons[key] = {"press": act}
    return buttons


def satellite_config(config: dict, unreported: list[str] | tuple = ()) -> dict:
    """What the satellite itself is told: the config without the hub's own
    keys, and without any the satellite has not reported yet, which it keeps
    as it has them."""
    return {k: v for k, v in config.items() if k not in HUB_ONLY and k not in unreported}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Satellite:
    id: str
    name: str
    model: str
    token_sha256: str
    adopted_at: float
    config: dict = field(default_factory=default_config)
    # REPORTED keys this satellite has not told the hub yet. Until it does,
    # the record holds UNREPORTED for them and the welcome leaves them out, so
    # the satellite keeps its own. Empty in a file from before this existed.
    unreported: list[str] = field(default_factory=list)

    def accepts(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token_hash(token), self.token_sha256)


@dataclass
class Firmware:
    sha256: str
    size: int
    model: str
    version: str
    uploaded_at: float
    # Standard base64 DER ECDSA over the image, from the upload's ?signature=,
    # or None for an unsigned image. Carried in the "ota" message; the
    # satellite does the checking (signing.py). Absent from an index.json
    # written before signatures existed, hence the default.
    signature: str | None = None


def write_atomic(path: Path, data: str, mode: int | None = None) -> None:
    """Replace `path` with `data`, so that a reader, a crash or a power cut
    finds the old file or the new one and never half of either.

    A unique temporary name, so two requests saving at once cannot write into
    the same half-finished file, and fsync before the rename, because a
    rename can reach the disk before the data it points at: after a power cut
    that is an empty satellites.json, which un-adopts every satellite.

    `mode` is set on the temporary file before a byte is written, so the
    file never exists, under either name, with wider permissions. mkstemp
    already makes it 0600, but that is its implementation; secrets.json
    (secret_store.py) states what it relies on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


FILE = "satellites.json"
# What the hub wrote before the feature was renamed (2026-09-25). A volume from
# then holds this and not FILE, and ignoring it would un-adopt every board.
LEGACY_FILE = "nodes.json"


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.fw_dir = root / "firmware"
        self.fw_dir.mkdir(parents=True, exist_ok=True)
        self.satellites: dict[str, Satellite] = {}
        self.firmware: dict[str, Firmware] = {}
        self._load()

    # -- satellites ---------------------------------------------------------

    def _load(self) -> None:
        f = self.root / FILE
        legacy = self.root / LEGACY_FILE
        if f.exists():
            records = json.loads(f.read_text()).get("satellites", [])
        elif legacy.exists():
            # Migrated once, on the first start after the rename: read and
            # written again under the new name. The old file is left where it
            # is, so the previous image still starts with what it had.
            records = json.loads(legacy.read_text()).get("nodes", [])
        else:
            records = None
        for n in records or ():
            cfg = default_config() | n.get("config", {})
            cfg["buttons"] = with_button_defaults(cfg)
            # ring_bottom was the setting for an hour on 27 Sep 2026, and
            # named the LED opposite the top.
            if "ring_bottom" in cfg:
                bottom = cfg.pop("ring_bottom")
                if "ring_top" not in n.get("config", {}) and isinstance(bottom, int):
                    cfg["ring_top"] = (bottom + 6) % 12
            self.satellites[n["id"]] = Satellite(**(n | {"config": cfg}))
        if records is not None and not f.exists():
            self.save_satellites()
        idx = self.fw_dir / "index.json"
        if idx.exists():
            for fw in json.loads(idx.read_text()):
                if (self.fw_dir / f"{fw['sha256']}.bin").exists():
                    self.firmware[fw["sha256"]] = Firmware(**fw)

    def save_satellites(self) -> None:
        body = {"satellites": [asdict(n) for n in self.satellites.values()]}
        write_atomic(self.root / FILE, json.dumps(body, indent=2))

    def adopt(self, satellite_id: str, name: str, model: str,
              reported: dict | None = None) -> str:
        """`reported` is what the connected satellite has said it is set to
        (its hello and status), and it wins over both the defaults and an old
        record: the satellite is the thing in the room. The case this exists
        for is a bedroom satellite with its lights off, moved to a new hub,
        whose default lights_enabled is true -- adopting it must not light it.

        A setting it has not reported is not guessed. Firmware before
        2026-09-25 reports only in "status", every 10 s, so a satellite
        adopted in its first seconds has said nothing: taking the defaults
        then would switch on the ring and speaker of a satellite that was dark
        and silent. Such a setting is left out of the welcome and held as
        UNREPORTED until take_report() has it.

        None is not a satellite that said nothing: it is a caller with no
        satellite to ask (a script, a test), which gets the defaults."""
        token = secrets.token_urlsafe(32)
        prev = self.satellites.get(satellite_id)
        given = reported_config(reported)
        if prev:
            config, unreported = dict(prev.config), list(prev.unreported)
        else:
            config = default_config()
            unreported = [] if reported is None else list(REPORTED)
        unreported = [k for k in unreported if k not in given]
        config.update({k: UNREPORTED[k] for k in unreported if k in UNREPORTED})
        config.update(given)
        self.satellites[satellite_id] = Satellite(
            id=satellite_id, name=name, model=model, token_sha256=token_hash(token),
            adopted_at=time.time(), config=config, unreported=unreported,
        )
        self.save_satellites()
        return token

    def take_report(self, satellite_id: str, reported: dict) -> bool:
        """Fill the settings an adopted satellite had not reported from what it
        reports now. Only those: a setting the hub holds is the hub's, and the
        satellite is told it rather than asked. True when the record changed."""
        rec = self.satellites.get(satellite_id)
        if rec is None or not rec.unreported:
            return False
        given = {k: v for k, v in reported_config(reported).items() if k in rec.unreported}
        if not given:
            return False
        rec.config.update(given)
        rec.unreported = [k for k in rec.unreported if k not in given]
        self.save_satellites()
        return True

    def take_own(self, satellite_id: str, settings: dict) -> bool:
        """Settings an adopted satellite changed itself, with its own buttons:
        what it has now, so the record takes them whatever it held. True when
        the record changed."""
        rec = self.satellites.get(satellite_id)
        if rec is None:
            return False
        given = {k: v for k, v in reported_config(settings).items() if rec.config.get(k) != v}
        if not given:
            return False
        rec.config.update(given)
        rec.unreported = [k for k in rec.unreported if k not in given]
        self.save_satellites()
        return True

    def forget(self, satellite_id: str) -> bool:
        gone = self.satellites.pop(satellite_id, None) is not None
        self.save_satellites()
        return gone

    # -- firmware -----------------------------------------------------------

    def add_firmware(self, image: bytes, model: str, version: str,
                     signature: str | None = None) -> Firmware:
        sha = hashlib.sha256(image).hexdigest()
        (self.fw_dir / f"{sha}.bin").write_bytes(image)
        fw = Firmware(sha, len(image), model, version, time.time(), signature)
        self.firmware[sha] = fw
        self._save_index()
        return fw

    def firmware_bytes(self, sha: str) -> bytes:
        return (self.fw_dir / f"{sha}.bin").read_bytes()

    def delete_firmware(self, sha: str) -> bool:
        if self.firmware.pop(sha, None) is None:
            return False
        (self.fw_dir / f"{sha}.bin").unlink(missing_ok=True)
        self._save_index()
        return True

    def _save_index(self) -> None:
        write_atomic(self.fw_dir / "index.json",
                      json.dumps([asdict(f) for f in self.firmware.values()], indent=2))
