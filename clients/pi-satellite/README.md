# Raspberry Pi satellite

A Raspberry Pi (or any Linux board with PipeWire) as a Calliope satellite: a
speaker first, a microphone if it has one. It speaks the hub's satellite
protocol, is adopted on the Satellites tab like the ESP32 board, and takes
signed updates from the hub, so the SD card is written once.

Tested target: Raspberry Pi 3 Model B+ on Raspberry Pi OS Lite 64-bit
(trixie). A Pi 4 or 5 runs the same bundle.

| | |
|---|---|
| Output | Any device PipeWire has: the 3.5 mm jack, HDMI, a USB sound card, a DAC HAT. Chosen on the Satellites tab (**Output**) |
| Microphone | Optional: any USB microphone or input. Sent at 16 kHz with the output's monitor as channel 0, so the hub's echo canceller removes the satellite's own voice and music |
| Updates | Signed bundles from the hub, installed beside the running release, rolled back if the new one does not reach the hub within 180 s |
| Wi-Fi | On first boot, or after three minutes without a network, an open network `calliope-sat-XXXX` with a setup page, as on the ESP32 |

| AirPlay | An AirPlay receiver under the satellite's name (or one you give it), on the same output, turned down while the satellite speaks or someone talks to it |

Bluetooth comes next, through the same output.

## AirPlay

Shairport Sync from Debian runs as the `calliope` user
(`calliope-airplay.service`, a user unit) and plays through PipeWire, so it
goes to the output chosen on the Satellites tab and mixes with the
satellite's voice. The tab turns it on and off (**AirPlay**) and names it
(**AirPlay name**, the satellite's own name when empty); the agent writes
`~/.config/calliope/shairport-sync.conf` and restarts it only when the name
changes. Debian's package runs its own system service straight to ALSA:
`install.sh` disables it.

This is **classic AirPlay**: trixie's Shairport Sync 4.3.7 is built without
AirPlay 2, so iPhones, iPads and Macs list it, but multi-room and the Home
app do not. AirPlay 2 needs Shairport Sync 5.5 with NQPTP, which Debian
ships only from forky.

**Ducking.** While the satellite plays a reply, and while the hub holds a
duck (someone is speaking to it, or to a satellite that plays through it),
every other stream goes down to 20% of its own volume, and back afterwards.
The agent does it with `pactl` on each stream (`airplay.Ducker`), and never
touches its own (`media.role` Assistant).

## Set up a card, once

You need Raspberry Pi Imager 2.0.3 or later (its command line writes the
card), the firmware signing key the ESP32 build uses
([keys/README.md](../korvo-satellite/keys/README.md)) and Pi OS Lite 64-bit
(trixie) from [raspberrypi.com](https://www.raspberrypi.com/software/operating-systems/).

```bash
cd clients/pi-satellite
python3 scripts/build_bundle.py           # dist/calliope-pi-<version>.tar.gz and .sig
diskutil list external                    # find the card: /dev/diskN
python3 scripts/prepare_sd.py --disk /dev/diskN \
  --image ~/Downloads/2026-09-15-raspios-trixie-arm64-lite.img.xz \
  --bundle dist/calliope-pi-<version>.tar.gz \
  --hub wss://calliope.example.com --country GB --timezone Europe/London \
  --ssh-key ~/.ssh/id_ed25519.pub         # optional: an admin login, key only
```

`prepare_sd.py` erases the card. It refuses an internal disk or one over
256 GB, and asks you to type the disk's name first. macOS asks for your
password when Imager writes.

## First boot

1. Put the card in the Pi and power it on. cloud-init creates the `calliope`
   user and unpacks the release from the card.
2. With no Ethernet cable, the Pi opens the Wi-Fi network
   `calliope-sat-XXXX` (the last four of its MAC) within two minutes.
3. Join it from a phone. The setup page lists the networks it found. Choose
   yours, enter the password, check the hub address, and press **Join**.
   On a Pi 3, choose a **5 GHz** network if you will use Bluetooth audio:
   its 2.4 GHz Wi-Fi shares the Bluetooth radio, and music stutters.
4. The Pi joins, installs PipeWire (a few minutes on the first boot), and
   appears on the **Satellites** tab. Adopt it.

If it cannot join, the setup network comes back with the reason on the page.
If the router changes later, the setup network opens again after three
minutes without a network.

## Updates

```bash
python3 scripts/build_bundle.py
curl -X POST "https://calliope.example.com/satellites/firmware?model=raspberry-pi&version=<version>&signature=$(cat dist/calliope-pi-<version>.tar.gz.sig)" \
  --data-binary @dist/calliope-pi-<version>.tar.gz
```

Then **Update** on the Satellites tab (Firmware), as for the ESP32. The hub
sends the bundle over the satellite's socket. The satellite:

1. checks its SHA-256 and signature, and refuses an unsigned or wrongly
   signed one (`bad signature`) before anything is written;
2. hands it to `calliope-root`, which checks the signature again, unpacks it
   into `/opt/calliope/releases/<version>`, runs its `install.sh` (packages,
   services) and points `current` at it;
3. restarts on the new release. Once the hub welcomes it, it stays
   (`verified`). If it has not reached the hub within 180 s, the rollback
   timer puts the previous release back, as the ESP32's bootloader does.

The operating system updates itself with apt as any Pi OS does.

## How it is put together

```
/opt/calliope/releases/<version>/   a release: calliope_pi/, install.sh, systemd/, bin/, sudoers.d/
/opt/calliope/current, previous     symlinks
/var/lib/calliope/state.json        hub, token, name, settings (mode 0600)
/var/lib/calliope/earcons/          the sounds the hub stores
/etc/calliope/firmware-signing.pub.pem   the key updates must be signed with
```

| Unit | What it does |
|---|---|
| `calliope-agent.service` | The agent (`python3 -m calliope_pi`), as `calliope`, with that user's PipeWire session (kept running by linger) |
| `calliope-firstboot.service` | Once: network, packages, agent |
| `calliope-portal.service` | The Wi-Fi setup network |
| `calliope-netcheck.timer` | Every minute: opens the setup network after three minutes offline |
| `calliope-rollback.timer` | Every 30 s: puts the previous release back when a new one is overdue |

The agent runs without root. `calliope-root` is the one command it may run
as root (`sudoers.d/calliope`): install a bundle it received, restart itself,
roll back, reboot, open the setup network. `calliope-root` checks a bundle's
signature itself, on its own copy, so a compromised agent cannot install
code as root.

What the agent reports beyond the ESP32's fields: `board` (the device-tree
model), `audio` (PipeWire's outputs and inputs, and the defaults), and in
its status `temp_c`, `throttled` (the firmware's under-voltage and
throttling flags) and `load`. `heap` is the memory available.

## Tests

```bash
python3 -m pytest    # needs pytest, pytest-asyncio, websockets, cryptography
```

They run the agent against a fake hub on a real WebSocket, with PipeWire and
`calliope-root` replaced, and install, confirm and roll back real signed
bundles in a temporary directory.
