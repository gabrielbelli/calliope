"""The rename from nodes to satellites must not cost the deployed hub its data.

Added by the rename review (2026-09-25) to demonstrate a defect; it fails until
compose.yaml mounts the hub's existing volume again.
"""

from __future__ import annotations

# What the hub's volume was called when it was deployed. Compose prefixes a
# named volume with the project name and nothing else, so a different key here
# is a different, empty volume on the next `up`, whatever the service is called.
DEPLOYED_VOLUME = "nodes-data"


def test_renaming_the_feature_does_not_hand_the_deployed_hub_an_empty_volume(compose):
    """Every board would come back pending, rules.json would fall back to the
    echo rule, and the uploaded firmware and fetched models would be gone.

    compose.yaml's own rule (above `volumes:`) is that volumes stay "unchanged,
    and named here so the deploy does not create new empty ones". The firmware
    keeps its NVS namespace "node" for the same reason (settings.cpp), and the
    hub keeps /nodes/ws and reads nodes.json. That migration only runs if the
    old volume is mounted: a new empty one has no nodes.json to read.
    """
    mounts = compose["services"]["voice-satellites"]["volumes"]
    data = [m.split(":", 1)[0] for m in mounts
            if isinstance(m, str) and m.split(":")[1] == "/data"]
    assert data == [DEPLOYED_VOLUME], (
        f"voice-satellites mounts {data} at /data, not the deployed "
        f"{DEPLOYED_VOLUME!r}: the first start after the rename un-adopts every "
        "satellite unless someone copies the volume by hand first")
    assert DEPLOYED_VOLUME in (compose.get("volumes") or {}), (
        f"{DEPLOYED_VOLUME!r} is not declared under the top-level volumes")
