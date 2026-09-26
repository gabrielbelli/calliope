# korvo-satellite

Firmware that turns an **ESP32-Korvo v1.1** into a Calliope satellite: three
microphones and a speaker on Wi-Fi, told what to do by
[voice-satellites](../../services/satellites/README.md).

The board is Espressif's original ESP32-Korvo (ESP32-WROVER-E, 16 MB flash,
PSRAM). It has an ES7210 four-channel ADC carrying three analogue mics 65 mm
apart plus a loopback of the speaker output for echo cancellation, an ES8311
codec for the speaker and headphone jack, twelve WS2812 LEDs and six buttons.
Pins come from Espressif's schematics; see `src/board.h`.

## First flash (once, over USB)

The UART port is a CP2102N. esptool fails above 230400 baud on this board, so
the `usb` env is pinned there.

```bash
cd clients/korvo-satellite
pio run -e usb -t erase        # clears any old Wi-Fi credentials
pio run -e usb -t upload
```

## Wi-Fi and adoption

1. On first boot the satellite opens a Wi-Fi network called
   `calliope-sat-XXXX`, and the ring breathes orange.
2. Join it from a phone. Choose your 2.4 GHz network and enter the password.
   The hub address is pre-filled with `wss://orko.gabrielbelli.com:30080`.
3. The satellite connects, and the ring breathes white while it waits.
4. Adopt it on the **Satellites** tab.

Release builds connect over TLS only and verify the gateway's certificate
against ISRG Root X1. A development build with a plain `ws://` hub is made
with `DEV_HUB`:

```bash
PLATFORMIO_BUILD_FLAGS='-DDEV_HUB=\"ws://192.168.1.177:8003\"' pio run -e usb -t upload
```

## Updates, over the air

```bash
CALLIOPE_SATELLITE=kitchen pio run -e ota -t upload     # or CALLIOPE_SATELLITE=all
```

This builds the image, signs it, uploads it to the hub (`CALLIOPE_URL`, default
`https://orko.gabrielbelli.com:30080`), and asks the hub to update the
satellite. The satellite pulls the image over its own connection. It keeps the
new image only if it reaches the hub again afterwards; otherwise the bootloader
rolls back. An image can also be uploaded from the Satellites tab, but only
unsigned, so a satellite that requires signatures refuses it.

This firmware connects to `/satellites/ws`. Firmware from before 2026-09-25,
when the feature was called nodes, connects to `/nodes/ws` and names its setup
network `calliope-node-XXXX`. The gateway and the hub still answer the old
path, so a board on that firmware is updated over the air like any other
([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)).

### Signed firmware

A satellite built while `keys/firmware-signing.pub.pem` exists installs only
images signed by the matching private key. The signature is ECDSA P-256 over
the image's SHA-256, in DER; mbedTLS in the Arduino core verifies it on the
satellite. The hub only carries the signature, so a hub that is compromised
still cannot install its own firmware. [`keys/README.md`](keys/README.md) says
how to make the key pair; the private key never enters the repository.

| | Build with the public key | Build without it |
|---|---|---|
| The build | prints `firmware signing: updates must be signed by key <id>` | prints an `UNSIGNED BUILD` warning and carries on |
| An update with no signature | refused: `ota` `failed`, `unsigned image` | installed |
| An update with a wrong signature | refused: `failed`, `bad signature` | installed |
| `hello` | `caps.ota_key` = the key's id | no `ota_key` |

The satellite checks the signature twice. It checks first against the SHA-256
the hub announces, so a bad signature is refused before the spare slot is
written. It checks again against the digest of the bytes it received, before
`Update.end()` makes the slot bootable; that second check decides. Rollback is
unchanged: a signed image that cannot reach the hub is still rolled back.

`pio run -e ota -t upload` signs with the private key at `CALLIOPE_SIGNING_KEY`
(default `~/.config/calliope/firmware-signing.pem`), through the `cryptography`
package when PlatformIO's Python has it and the openssl CLI otherwise. It sends
the signature as `signature=<base64url>` on `POST /satellites/firmware`. It
stops before uploading when the build trusts a key but the private key is
missing, or when the private key is not the build's.

`scripts/sig_host_check.sh` builds the satellite's check (`src/ota_sig.cpp`) on
a desktop against the same mbedTLS release (2.28.7) and runs it with throwaway
keys. It accepts good signatures in base64 and base64url, and it refuses a
signature of another image, a flipped bit, another key, truncated DER, a
trailing byte, and text that is not base64. The check has not yet been run on
the board itself.

## Earcons

Earcons are short feedback sounds, such as "I heard you", "done" and "that
failed". The hub uploads them once and the satellite keeps them in flash. When
the hub sends `{"type": "earcon", "id": "wake"}`, the satellite plays the sound
from its own storage. The only thing that crosses the network is that message.

- Storage is LittleFS on the `storage` partition (about 8 MB at `0x810000`).
  The partition table is unchanged. On a board whose partition was never
  formatted, the first boot formats it in a background task, so boot and Wi-Fi
  do not wait. Until the format is done the satellite answers `earcon_list`
  with `"ready": false`.
- Each earcon is raw mono s16le at 48 kHz: at most 2 s (192 000 bytes), at most
  16 earcons, with ids of `[a-z0-9_-]{1,24}`.
- An upload works like a firmware update. The hub sends `earcon_put` (id, size,
  sha256). The satellite asks for each piece with `earcon_next`, and the hub
  answers with binary frame kind `4` (`4, 0, 0, 0, offset u32`, then up to 8
  KB). The satellite writes to a temporary file. It keeps the earcon only when
  the SHA-256 matches, and then replies `earcon_stored`. Any failure replies
  `earcon_failed` with `op` (`put`, `play` or `delete`) and an `error`. An
  upload is dropped after 5 s with no chunk, or on disconnect.
- `earcon_list` is answered with `earcons`: the items (id, size, sha256), the
  `ready` flag, and `last_load_us`, which is the time it took to read the last
  earcon played from flash. That load time has not been measured on the board
  yet.
- An earcon plays immediately, mixed over any audio the hub is sending. It
  replaces an earcon that is still sounding. `flush` does not stop it, and
  ducking does not lower it. It plays at the satellite's volume, under the same
  ceiling, and it appears on the loopback channel like any other speaker
  output, so echo cancellation still works.

## Ducking

`{"type": "duck", "level": 20, "ms": 0}` lowers the hub's audio, for example
while the satellite is listening. `level` uses the same 0-100 scale as
`volume`, so a duck to 20 sounds like the volume set to 20. A level at or above
the current volume changes nothing. The gain fades over at most 50 ms, so it
does not click.

- `ms` > 0 restores the volume after that many milliseconds. `ms` = 0 keeps the
  duck until `{"type": "unduck"}` arrives.
- A duck is not saved to NVS, because it changes too often for flash to take.
  A reboot or a lost hub connection ends it; otherwise a hub that restarted
  could leave the satellite quiet with nothing to lift it.
- It is applied to the samples, not the codec, so it can only lower the
  output. Earcons are not ducked, so a chime can still be heard over lowered
  speech.
- `status` reports `duck` (the level, or `null`).

## The ring

| Ring | Meaning |
|---|---|
| orange, breathing | Wi-Fi setup network is open |
| blue, spinning | connecting to Wi-Fi or the hub |
| white, breathing slowly | connected, waiting to be adopted |
| amber, breathing | adopted, but the hub is unreachable |
| red, solid | privacy mute: the mics are powered down |
| green, filling | firmware update in progress |
| anything else | whatever the hub set |

With `lights_enabled` off (the Satellites tab's **Lights** box), the ring stays
dark through everything above: reboots, updates and mute included. The setting
is saved on the satellite and read before the first frame is drawn.

The satellite says its settings (volume, mic gain, and the microphone, speaker
and lights switches) in every `hello` as well as in `status`. A hub that adopts
it before its first status therefore welcomes it with its own settings, so a
dark satellite stays dark. Firmware before 2026-09-25 said them only in
`status`; the hub leaves such a satellite's settings out of the welcome until
it has reported them.

## Buttons

Every press and release is reported to the hub, which decides what they do.
Four things are handled on the satellite as well, because they must work
whatever the hub does:

| Button | On the satellite |
|---|---|
| REC | toggles the privacy mute |
| VOL+ / VOL- | volume ±10 %, while `local_volume_buttons` is on |
| SET, held 5 s | reopens the Wi-Fi setup network |
| MODE, held 10 s | factory reset: forgets Wi-Fi, hub and adoption |

## Kept on the device, whatever the hub says

- **The privacy mute.** It powers down the ES7210 mic front-end, and only the
  REC button turns it off.
- **The volume ceiling.** 100 % is 0 dB at the DAC; the codec's +32 dB of
  digital gain is never used.
- **Recovery:** the setup portal, the factory reset, and firmware rollback.
- **The firmware signature**, in a build with a public key. The hub cannot
  waive it.

## Power: the board does not start by itself after a power cut

**Measured on 26 Sep 2026.** A cold power-on through the **POWER** port puts
the chip in the ROM's download mode, and it waits there indefinitely. It
answers a flashing tool without being reset, and the ring stays as it was.
Any reset afterwards boots it normally: the RST button, a reset pulse on the
UART, or the BOOT and RST buttons together. After that it reaches the hub in
about 2.5 s.

The cause is a race on GPIO0 (the BOOT strap), from the schematic:

- GPIO0 has no external pull-up (R40, 47 kΩ, is not fitted). It relies on the
  chip's weak internal one.
- The BOOT button line carries C14 (0.1 µF), and the microphone codec's
  clock input hangs off the same net.
- At power-on, the strap is read before GPIO0 has charged high, so the chip
  starts in download mode. By the time of any later reset it has charged, so
  a reset boots normally.

The firmware cannot fix this: the decision is made in ROM before any code
runs. The boot watchdog below never gets to run in this case.

**The UART port does not power the board.** D17, the diode from its VBUS, is
not fitted. The USB-serial chip is powered from the board's own 3.3 V, so the
UART cable alone leaves the board off.

| Fix | |
|---|---|
| Fit a **10 kΩ resistor on the empty R40 pad** (GPIO0 pull-up) | permanent; recommended |
| Or a **1-10 µF capacitor from EN to GND** | permanent; holds reset until GPIO0 is high |
| Keep the UART connected to a computer and reset through it | works, but needs the computer |
| Press **RST** once after power is applied | works, by hand, after every power cut |

The boot journal (`src/boot.cpp`) is for the other kind of stall, one inside
the firmware. It stamps each start-up step, sends the stamps to the hub
(`GET /satellites/{id}` → `boot`), and restarts a start-up that has not
reached Wi-Fi after 20 s.

## Licence

BSD 2-Clause, like the rest of the repository. Code ported from Espressif's
esp_codec_dev (Apache-2.0) and the libraries fetched at build time are listed
in [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
