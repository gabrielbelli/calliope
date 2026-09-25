"""What survives a restart: adopted nodes and uploaded firmware images.

One JSON file and one directory under NODES_DATA_DIR. A node's adoption token
is kept only as a SHA-256; the node holds the token itself, so a copy of this
file cannot impersonate a node to the hub.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG = {
    "volume": 60,
    "mic_gain_db": 30.0,
    "mic_enabled": True,
    "speaker_enabled": True,
    "local_volume_buttons": True,
    "lights_enabled": True,
    # What the hub does when a button is pressed. The node only reports
    # presses, so this never goes to it (HUB_ONLY). PLAY talks, SET stops.
    "buttons": {"play": {"press": "ptt"}, "set": {"press": "stop"}},
}
# Config the hub acts on itself and never sends to the node: the firmware would
# ignore it, and it would cost a JSON document on a board with 300 KB of heap.
HUB_ONLY = frozenset({"buttons"})


def default_config() -> dict:
    """A deep copy: "buttons" is a dict of dicts, and a shallow copy would let
    one node's mapping be edited through another's."""
    return copy.deepcopy(DEFAULT_CONFIG)


def node_config(config: dict) -> dict:
    """What the node itself is told: the config without the hub's own keys."""
    return {k: v for k, v in config.items() if k not in HUB_ONLY}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Node:
    id: str
    name: str
    model: str
    token_sha256: str
    adopted_at: float
    config: dict = field(default_factory=default_config)

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
    # or None for an unsigned image. Carried in the "ota" message; the node
    # does the checking (signing.py). Absent from an index.json written before
    # signatures existed, hence the default.
    signature: str | None = None


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.fw_dir = root / "firmware"
        self.fw_dir.mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, Node] = {}
        self.firmware: dict[str, Firmware] = {}
        self._load()

    # -- nodes --------------------------------------------------------------

    def _load(self) -> None:
        f = self.root / "nodes.json"
        if f.exists():
            for n in json.loads(f.read_text()).get("nodes", []):
                cfg = default_config() | n.get("config", {})
                self.nodes[n["id"]] = Node(**(n | {"config": cfg}))
        idx = self.fw_dir / "index.json"
        if idx.exists():
            for fw in json.loads(idx.read_text()):
                if (self.fw_dir / f"{fw['sha256']}.bin").exists():
                    self.firmware[fw["sha256"]] = Firmware(**fw)

    def save_nodes(self) -> None:
        body = {"nodes": [asdict(n) for n in self.nodes.values()]}
        _write_atomic(self.root / "nodes.json", json.dumps(body, indent=2))

    def adopt(self, node_id: str, name: str, model: str, reported: dict | None = None) -> str:
        """`reported` is what the node says it is set to right now, and it wins
        over both the defaults and an old record: the node is the thing in the
        room. The case this exists for is a bedroom node with its lights off,
        moved to a new hub, whose default lights_enabled is true -- adopting it
        must not light it."""
        token = secrets.token_urlsafe(32)
        prev = self.nodes.get(node_id)
        config = dict(prev.config) if prev else default_config()
        config.update({k: v for k, v in (reported or {}).items() if k in config})
        self.nodes[node_id] = Node(
            id=node_id, name=name, model=model, token_sha256=token_hash(token),
            adopted_at=time.time(), config=config,
        )
        self.save_nodes()
        return token

    def forget(self, node_id: str) -> bool:
        gone = self.nodes.pop(node_id, None) is not None
        self.save_nodes()
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
        _write_atomic(self.fw_dir / "index.json",
                      json.dumps([asdict(f) for f in self.firmware.values()], indent=2))
