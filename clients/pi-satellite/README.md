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
| AirPlay | An AirPlay receiver under the satellite's name (or one you give it), on the same output, turned down while the satellite speaks or someone talks to it. Play, pause, skip and disconnect from the Satellites tab and Home Assistant |
| Music | Home Assistant's media player, in stereo, on a stream of its own that goes down under the voice ([Music from the hub](#music-from-the-hub)) |

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
is mixed. The phone's volume and the satellite's **Volume** are one volume,
the output's own: the last one moved wins, and the satellite reports what
the phone set (**One volume** under [AirPlay](#airplay)).

## Music from the hub

Home Assistant's media player plays through the hub's media lane: binary
frames of kind 5, 44.1 kHz stereo (`caps.media`), which the agent plays on a
stream of its own (`media.role` Music), apart from the voice. A reply, Say or
tone does not stop the music: the music goes down under the voice, as AirPlay
does, and comes back afterwards. `media_flush` drops the music that is
buffered and leaves the voice alone; `flush` drops only the voice.
Announcements come on the voice lane, as a reply does, and nothing plays
while the speaker is off. The status reports `media_buffered_ms` and
`media_dropped`.

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
(`acre`) is never kept, not even on the Pi: with it, anyone on the network
could control the phone. Shairport Sync keeps its own copy, and the remote
control goes through Shairport Sync (below).

**One volume.** A phone keeps its own volume: it sends its slider's
position when it connects and whenever it moves, and
`bin/calliope-airplay-volume` sets the output's volume from it. The agent
reads the output's volume back (`wpctl get-volume`) at each periodic status
and whenever AirPlay's state changes, once the hub has welcomed it. When
the phone has moved it, the agent takes it as the satellite's own `volume`,
saves it and reports it with `cause: "local"`, so the hub, the Satellites
tab and Home Assistant show the real volume. The agent sets the output's
volume only on `welcome`, on a `config` that contains `volume`, and when the
output changes (a new output has a volume of its own, which must not be
taken for the phone's): a change to any other setting no longer puts the
phone's volume back. While the chosen output is unplugged nothing is read
back, as what plays meanwhile is a stand-in. Setting it also
unmutes the output, which the phone's hook mutes at the bottom of its
slider. (Setting the phone's slider from here, over Shairport Sync's remote
control, was tried on 29 Sep 2026: phones did not keep it.)

**Remote control.** The hub's `airplay_command` (the Satellites tab's
controls, Home Assistant's media player) asks the phone to play, pause, play
or pause, skip forward or back, or stop. It goes through Shairport Sync's
D-Bus call `RemoteCommand`, the only one that returns the phone's answer: the
phone's HTTP status (normally 204), or Shairport Sync's own 490 to 498 when
it could not ask (the answer's `error` says why). A 494, Shairport Sync busy
with its own once-a-second probe of the phone, is asked once more.
`disconnect` drops the session (`DropSession`), which needs nothing from the
phone, and the phone loses the receiver.

The phone decides. A 204 says only that it took the request, so the agent
then watches MPRIS for up to 1.5 s and answers `confirmed: true` when it saw
the change: playing, paused, another track, stopped. An app that takes a
request and does nothing with it gives `confirmed: false`, and so does a
play/pause or skip when MPRIS could not be read before it, as there is then
nothing to see a change against. The hub waits 6 s for the answer, so the
watching never takes it past 5 s after the command arrived; only a phone
slow to answer can. The status says
what can be asked now, in `airplay.remote`:

| `controls` | When |
|---|---|
| `[]` | No session (`available` is `null`) |
| `["disconnect"]` | A session, but the phone does not answer its remote control (Shairport Sync's `RemoteControl.Available` is false: five failed probes in a row) |
| all seven | A session, and the phone answers |

`remote.last` is the last command and how it went.

**The cover** of what plays goes to the hub once per picture (`artwork`
message, JPEG or PNG up to 2 MB, named by its SHA-256), which serves it at
`GET /satellites/{id}/airplay/artwork`; the Satellites tab shows it in the
AirPlay section. The cover always goes before the status that names it: the
hub drops a cover that the status no longer names, so the cover of music
that has stopped goes too.

**The cover after a restart.** The pipe delivers each picture once, to
whoever reads it then: an agent restarted in the middle of a track misses
it, and nothing can ask the phone to send it again. Shairport Sync also
keeps the picture in its cover cache as `cover-<md5>.jpg` (or `.png`), and
MPRIS's `artUrl` points at it. The agent has Shairport Sync keep that cache
in a private directory (`$XDG_RUNTIME_DIR/calliope-airplay-covers`, mode
0700, emptied at reboot) instead of its default in `/tmp`, which any user
can write to. After a restart the agent takes the cover from there, and only:

- while MPRIS says Playing or Paused. The `artUrl` outlives the session, so
  a stopped player still names the cover of music that is over;
- from a file straight in that directory whose MD5 is its name, which also
  refuses a file that is half written;
- when the pipe has sent no picture (or "no picture") in this session.

The title, artist and album come back from MPRIS the same way, where the
metadata has not said them yet. The release that adds the cache directory
changes Shairport Sync's configuration, so Shairport Sync restarts once when
that release is installed, which ends any AirPlay session playing then.

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
   On a Pi 3, a **5 GHz** network is the better choice: its 2.4 GHz Wi-Fi
   shares the radio with Bluetooth.
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

The caps in its `hello` say what it has, and the hub, the Satellites tab and
Home Assistant offer only that:

| Cap | What it says |
|---|---|
| `speaker` | The voice: 44.1 kHz mono s16le, kind 2 frames |
| `media` | The media lane: 44.1 kHz stereo s16le, kind 5 frames and `media_flush` ([Music from the hub](#music-from-the-hub)) |
| `mic` | Only while it has a microphone: 16 kHz, with the output's monitor as channel 0 (`reference`). `max_gain_db` 3.5 is the most `mic_gain_db` does here: the agent raises the input's volume to 1.5 at most, 20·log10(1.5) = 3.52 dB |
| `airplay` | `{"version": 2, "controls": true}` where Shairport Sync is installed: it takes `airplay_command`, and its status has `airplay.remote` |
| `health` | The board's readings its status carries: `temp_c`, `throttled`, `under_voltage`, `load` |
| `earcons`, `duck`, `audio_devices`, `bundle`, `ota_key` | As in the hub's README (`services/satellites/README.md`) |

## Tests

```bash
python3 -m pytest    # needs pytest, pytest-asyncio, websockets, cryptography
```

They run the agent against a fake hub on a real WebSocket, with PipeWire,
Shairport Sync and `calliope-root` replaced, and install, confirm and roll
back real signed bundles in a temporary directory. Nothing plays.
