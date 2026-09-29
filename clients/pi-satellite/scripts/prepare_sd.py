#!/usr/bin/env python3
"""Write a Raspberry Pi satellite's SD card, once, from macOS.

    python3 scripts/prepare_sd.py --disk /dev/diskN --image raspios-trixie-arm64-lite.img.xz \\
        --bundle dist/calliope-pi-<version>.tar.gz --hub wss://calliope.example.com \\
        [--country GB] [--timezone Europe/London] [--ssh-key ~/.ssh/id_ed25519.pub --admin you]

ERASES THE DISK. It refuses a disk that is internal, not removable, or over
256 GB, and asks you to type its name before writing.

Raspberry Pi Imager's command line writes the image with the cloud-init
user-data and network-config from setup/ (Imager 2.0.3 or later; macOS asks
for your password to write to the disk). Then this copies onto the boot
partition, under calliope/:

  bundle.tar.gz, .sig        the release the first boot installs (build_bundle.py)
  firmware-signing.pub.pem   the key every later update must be signed with
  setup.json                 the hub address the setup page is filled in with,
                             and the Wi-Fi country
  firstboot.sh               what cloud-init runs to start it all

After this the card is never needed again: updates come from the hub.
--ssh-key adds an admin user with sudo who logs in with that key only;
without it the board has no login at all."""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
IMAGER = "/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
MAX_BYTES = 256 * 1000 ** 3


def disk_info(disk: str) -> dict:
    out = subprocess.run(["diskutil", "info", "-plist", disk], capture_output=True, check=True).stdout
    return plistlib.loads(out)


def check_disk(disk: str) -> dict:
    info = disk_info(disk)
    if info.get("Internal") or not (info.get("RemovableMedia") or info.get("Removable")
                                    or info.get("Ejectable")):
        raise SystemExit(f"{disk} is internal or not removable; refusing to erase it")
    if info.get("WholeDisk") is False:
        raise SystemExit(f"{disk} is a partition; name the whole disk (/dev/diskN)")
    if int(info.get("TotalSize") or info.get("Size") or 0) > MAX_BYTES:
        raise SystemExit(f"{disk} is over 256 GB; that is not an SD card, refusing")
    return info


def user_data(args) -> str:
    admin = ""
    if args.ssh_key:
        key = Path(args.ssh_key).expanduser().read_text().strip()
        admin = (f"  - name: {args.admin}\n"
                 "    shell: /bin/bash\n"
                 "    groups: [sudo, adm, systemd-journal, audio]\n"
                 '    sudo: "ALL=(ALL) NOPASSWD:ALL"\n'
                 "    lock_passwd: true\n"
                 "    ssh_authorized_keys:\n"
                 f"      - {key}\n")
    return (HERE / "setup" / "user-data.tmpl").read_text().format(
        enable_ssh="true" if args.ssh_key else "false", timezone=args.timezone,
        country=args.country, admin=admin.rstrip("\n"))


def wait_for_boot_volume(timeout: float = 90) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name in ("bootfs", "boot"):
            p = Path("/Volumes") / name
            if (p / "config.txt").exists():
                return p
        time.sleep(2)
    raise SystemExit("the boot partition did not mount; mount it (diskutil mountDisk) and copy "
                     "calliope/ by hand, or run this again")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--disk", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--hub", required=True, help="wss://... the satellites connect to")
    ap.add_argument("--country", default="GB", help="the Wi-Fi regulatory country, two letters")
    ap.add_argument("--timezone", default="Etc/UTC")
    ap.add_argument("--pubkey", default="~/.config/calliope/firmware-signing.pub.pem")
    ap.add_argument("--ssh-key")
    ap.add_argument("--admin", default="calliope-admin")
    ap.add_argument("--yes", action="store_true", help="do not ask to type the disk's name")
    args = ap.parse_args(argv)

    bundle = Path(args.bundle)
    sig = bundle.parent / (bundle.name + ".sig")
    pub = Path(args.pubkey).expanduser()
    for need in (Path(args.image), bundle, sig, pub, Path(IMAGER)):
        if not need.exists():
            raise SystemExit(f"missing: {need}")
    if not args.hub.startswith(("wss://", "ws://")):
        raise SystemExit("--hub starts with wss:// (or ws://)")
    info = check_disk(args.disk)
    size_gb = int(info.get("TotalSize") or 0) / 1e9
    print(f"{args.disk}: {info.get('MediaName')} {size_gb:.1f} GB, will be ERASED")
    if not args.yes and input(f"type {args.disk} to go on: ").strip() != args.disk:
        print("not written")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        ud, nc = Path(tmp) / "user-data", Path(tmp) / "network-config"
        ud.write_text(user_data(args))
        shutil.copyfile(HERE / "setup" / "network-config", nc)
        subprocess.run(["diskutil", "unmountDisk", args.disk], check=False)
        done = subprocess.run([IMAGER, "--cli", "--cloudinit-userdata", str(ud),
                               "--cloudinit-networkconfig", str(nc), "--disable-eject",
                               args.image, args.disk])
        if done.returncode:
            raise SystemExit(f"Raspberry Pi Imager failed ({done.returncode}); nothing else was done")

    subprocess.run(["diskutil", "mountDisk", args.disk], check=False)
    boot = wait_for_boot_volume()
    dest = boot / "calliope"
    dest.mkdir(exist_ok=True)
    shutil.copyfile(bundle, dest / "bundle.tar.gz")
    shutil.copyfile(sig, dest / "bundle.tar.gz.sig")
    shutil.copyfile(pub, dest / "firmware-signing.pub.pem")
    shutil.copyfile(HERE / "setup" / "firstboot.sh", dest / "firstboot.sh")
    (dest / "setup.json").write_text(json.dumps({"hub": args.hub, "country": args.country}, indent=1) + "\n")
    print(f"copied the release and setup to {dest}")
    subprocess.run(["sync"], check=False)
    subprocess.run(["diskutil", "eject", args.disk], check=False)
    print("done: put the card in the Pi and power it on. It opens the Wi-Fi network "
          "calliope-sat-XXXX within about two minutes; join it from a phone.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
