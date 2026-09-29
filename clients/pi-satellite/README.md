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

## Audio quality

The best each piece of hardware takes, and no conversion that is not needed
(`bundle/pipewire`, `bundle/wireplumber`, installed by `install.sh`):

| Link | Setting | Why |
|---|---|---|
| PipeWire's rate | 44.1 kHz, AirPlay's own (88.2 or 176.4 kHz for a source at those); nothing at 48 kHz moves it | A reply or an earcon that starts mid-song does not switch the card to 48 kHz and convert the music. The hub's voice arrives at 44.1 kHz already |
| Conversion, where it is needed | quality 10 of 15 (the default is 4) | An earcon at 48 kHz, or a card that cannot do 44.1 kHz at all; a Pi 3 does this for stereo easily |
| Channels | never upmixed | Stereo stays stereo. The hub's voice is mono, as on the ESP32, and PipeWire plays it on both speakers; nothing else becomes mono |
| Outputs | never suspended | No clipped first syllable, no click as a card wakes |
| Sample format | the widest the card takes (32 or 24 bits on a USB DAC, 16 on the Pi's own jack), no dither | AirPlay's 16-bit samples reach the card unchanged, carried in the wider word; a reply mixed over the music is mixed without rounding the music to 16 bits |
| AirPlay | **bit-perfect**: Shairport Sync hands on the phone's own 16-bit, 44.1 kHz samples and applies no volume of its own | The phone's slider sets the output's own volume instead (`bin/calliope-airplay-volume`), in the DAC's hardware where it has a control: AirPlay's 30 dB spread over 60, so the slider's first step is quiet and its top is full volume |

What is left is the hardware, and the agent says what each output is
(`quality` on each device in `audio`, from the kernel):

| Kind | What it is | Marked |
|---|---|---|
| `usb` | A USB DAC: every format and rate its interface offers, from `/proc/asound/cardN/stream0` | "USB DAC · up to 32-bit · 384 kHz" |
| `i2s` | A DAC HAT on the Pi's I2S pins | "DAC HAT" |
| `hdmi` | HDMI: the display's or receiver's own DAC makes the sound | "HDMI: the display's own DAC" |
| `pwm` | **The Pi's own 3.5 mm jack: pulse-width modulation from the processor, not a DAC.** 16-bit at 48 kHz at most, with audible hiss and less detail than even a cheap USB DAC | an asterisk, and its limits under **Output** |

A USB DAC or an I2S DAC HAT is the upgrade, and appears under **Output** by
itself.

**Jack detection.** A card that detects its jacks (the kernel's `... Jack`
controls; many USB DACs, not the Pi's own jack) says whether something is
plugged into each output and input: `jack` on each device, `plugged`,
`unplugged`, or `null` where it cannot tell. The agent follows PipeWire's
events (`pactl subscribe`) and sends its status within a second of a plug
going in or out; the hub publishes a `jack` event. A combo headset socket
often reports a microphone for any plug, a speaker cable included, so the
microphone's jack does not decide whether the satellite has one: its
**Microphone** switch does. Classic AirPlay is always ALAC, lossless, 16-bit at
44.1 kHz (1,411 kb/s); AirPlay 2 adds 48 kHz.

The status says what the output is driven at now (`audio.playing_at`), and
the Satellites tab shows it under Device and in the AirPlay section, whose
**Path** says "Bit-perfect" when the phone's samples reach the card unchanged,
or what they were converted from and to. Two things are never bit-perfect by
design: a reply ducks the music while it speaks, and a reply mixed over music
is mixed. The phone's volume and the satellite's **Volume** set the same
output volume, so the last one moved wins.

## AirPlay

Shairport Sync from Debian runs as the `calliope` user
(`calliope-airplay.service`, a user unit) and plays into PipeWire, through
ALSA at 16 bits, as the phone sent them (Audio quality, above), so it
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

**What it plays: everything AirPlay says, kept.** Shairport Sync writes its
metadata to a pipe in calliope's runtime directory, cover art included; the
agent (`airplay.Metadata`) keeps every item raw (`airplay.raw`, by
`type/code`, for integrations still to come) and decodes what a person
reads: the track (title, artist, album, album artist, genre, composer,
year, track and disc numbers, duration, and the phone's own file: its kind,
bit rate and rate), progress, the phone (name, model, address, app, DACP
id), its volume, and the cover (saved on the Pi, named by its SHA-256).
Whether it plays comes from Shairport Sync's own MPRIS interface on
calliope's session bus, the player's own word, and from the metadata's
events where MPRIS is not there. The phone's remote-control token
(`acre`) stays on the Pi: the hub's API has no key, and the token controls
the phone's playback.

**Volume.** A phone keeps its own: it sends its slider's position when it
connects and whenever it moves, and that sets the output's volume. (Setting
the phone's slider from here, over Shairport Sync's remote control, was
tried on 29 Sep 2026: phones did not keep it.)

**The cover** of what plays goes to the hub once per picture (`artwork`
message, JPEG or PNG up to 2 MB, named by its SHA-256), which serves it at
`GET /satellites/{id}/airplay/artwork`; the Satellites tab shows it in the
AirPlay section. The title, artist and album come from MPRIS where the
metadata has not said them yet.

The status
carries them as `airplay` and is sent at once when they change, so the
Satellites tab's AirPlay section says Playing as the music starts.

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
