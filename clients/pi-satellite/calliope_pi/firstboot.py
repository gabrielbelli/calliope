"""The first start of a freshly written SD card, as root, once.

cloud-init has already unpacked the bundle from the boot partition into
/opt/calliope/releases/<version>, pointed `current` at it and started this.
Then:

  1. the hub address and Wi-Fi country from calliope/setup.json on the boot
     partition, and the update key beside it into /etc/calliope
  2. the host name calliope-sat-XXXX, as the setup network is named
  3. a network: Ethernet, or the Wi-Fi setup network (portal.py) until one
     is chosen
  4. the release's install.sh: packages, the agent's service, the timers
  5. the agent, which says hello to the hub and waits to be adopted

It leaves /var/lib/calliope/firstboot.done, and disables itself."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

from . import bundle, paths, portal, system
from .state import load, save

DONE = paths.STATE / "firstboot.done"


def sh(*argv: str) -> int:
    print("+", " ".join(argv), flush=True)
    return subprocess.run(list(argv), check=False).returncode


def setup() -> dict:
    try:
        return json.loads((paths.BOOT / "calliope" / "setup.json").read_text())
    except (OSError, ValueError):
        return {}


def main() -> int:
    if DONE.exists():
        return 0
    cfg = setup()
    paths.STATE.mkdir(parents=True, exist_ok=True)
    key = paths.BOOT / "calliope" / "firmware-signing.pub.pem"
    if key.exists():
        paths.PUBKEY.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(key, paths.PUBKEY)
    st = load()
    if cfg.get("hub") and not st.hub:
        st.hub = cfg["hub"]
        save(st)
    if cfg.get("country"):
        sh("raspi-config", "nonint", "do_wifi_country", str(cfg["country"]))
    sh("rfkill", "unblock", "wifi")
    try:
        sh("hostnamectl", "set-hostname", f"calliope-sat-{system.satellite_id()[-4:]}")
    except RuntimeError:
        pass

    deadline = time.monotonic() + 60
    while not portal.online() and time.monotonic() < deadline:
        time.sleep(3)
    if not portal.online():
        portal.Portal().run()

    try:
        bundle.run_hook(paths.current().resolve())
    except bundle.BundleError as e:
        print(f"install.sh failed: {e}; the next boot tries again", flush=True)
        return 1
    sh("chown", "-R", "calliope:calliope", str(paths.STATE))
    DONE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    sh("systemctl", "disable", "calliope-firstboot.service")
    sh("systemctl", "--no-block", "restart", "calliope-agent.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
