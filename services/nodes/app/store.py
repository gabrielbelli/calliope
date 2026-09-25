"""What survives a restart: adopted nodes and uploaded firmware images.

One JSON file and one directory under NODES_DATA_DIR. A node's adoption token
is kept only as a SHA-256; the node holds the token itself, so a copy of this
file cannot impersonate a node to the hub.
"""

from __future__ import annotations

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
}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Node:
    id: str
    name: str
    model: str
    token_sha256: str
    adopted_at: float
    config: dict = field(default_factory=lambda: dict(DEFAULT_CONFIG))

    def accepts(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(token_hash(token), self.token_sha256)


@dataclass
class Firmware:
    sha256: str
    size: int
    model: str
    version: str
    uploaded_at: float


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
                cfg = dict(DEFAULT_CONFIG) | n.get("config", {})
                self.nodes[n["id"]] = Node(**(n | {"config": cfg}))
        idx = self.fw_dir / "index.json"
        if idx.exists():
            for fw in json.loads(idx.read_text()):
                if (self.fw_dir / f"{fw['sha256']}.bin").exists():
                    self.firmware[fw["sha256"]] = Firmware(**fw)

    def save_nodes(self) -> None:
        body = {"nodes": [asdict(n) for n in self.nodes.values()]}
        _write_atomic(self.root / "nodes.json", json.dumps(body, indent=2))

    def adopt(self, node_id: str, name: str, model: str) -> str:
        token = secrets.token_urlsafe(32)
        prev = self.nodes.get(node_id)
        self.nodes[node_id] = Node(
            id=node_id, name=name, model=model, token_sha256=token_hash(token),
            adopted_at=time.time(), config=prev.config if prev else dict(DEFAULT_CONFIG),
        )
        self.save_nodes()
        return token

    def forget(self, node_id: str) -> bool:
        gone = self.nodes.pop(node_id, None) is not None
        self.save_nodes()
        return gone

    # -- firmware -----------------------------------------------------------

    def add_firmware(self, image: bytes, model: str, version: str) -> Firmware:
        sha = hashlib.sha256(image).hexdigest()
        (self.fw_dir / f"{sha}.bin").write_bytes(image)
        fw = Firmware(sha, len(image), model, version, time.time())
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
