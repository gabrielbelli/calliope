"""Where the satellite keeps things. Each can be moved with an environment
variable, which is how the tests run it in a temporary directory."""

from __future__ import annotations

import os
from pathlib import Path


def _p(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# Releases, each in its own directory, and `current` / `previous` symlinks.
OPT = _p("CALLIOPE_OPT", "/opt/calliope")
# What the satellite remembers: hub, token, name, settings, earcons.
STATE = _p("CALLIOPE_STATE", "/var/lib/calliope")
# The public key updates must be signed with. Absent, updates are refused.
PUBKEY = _p("CALLIOPE_PUBKEY", "/etc/calliope/firmware-signing.pub.pem")
# The FAT boot partition, where the SD card was prepared from another computer.
BOOT = _p("CALLIOPE_BOOT", "/boot/firmware")


def releases() -> Path:
    return OPT / "releases"


def current() -> Path:
    return OPT / "current"


def previous() -> Path:
    return OPT / "previous"


def pending() -> Path:
    """Present while a new release has not yet reached the hub: the rollback
    timer puts the previous one back if it never does."""
    return STATE / "pending.json"


def state_file() -> Path:
    return STATE / "state.json"


def earcons() -> Path:
    return STATE / "earcons"


def incoming() -> Path:
    return STATE / "incoming"
