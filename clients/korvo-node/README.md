# korvo-node

Firmware that turns an **ESP32-Korvo v1.1** into a Calliope node: three
microphones and a speaker on Wi-Fi, told what to do by
[voice-nodes](../../services/nodes/README.md).

The board is Espressif's original ESP32-Korvo (ESP32-WROVER-E, 16 MB flash,
PSRAM). It has an ES7210 four-channel ADC carrying three analogue mics 65 mm
apart plus a loopback of the speaker output for echo cancellation, an ES8311
codec for the speaker and headphone jack, twelve WS2812 LEDs and six buttons.
Pins come from Espressif's schematics; see `src/board.h`.

## First flash (once, over USB)

The UART port is a CP2102N. esptool fails above 230400 baud on this board, so
the `usb` env is pinned there.

```bash
cd clients/korvo-node
pio run -e usb -t erase        # clears any old Wi-Fi credentials
pio run -e usb -t upload
```

## Wi-Fi and adoption

1. On first boot the node opens a Wi-Fi network called `calliope-node-XXXX`,
   and the ring breathes orange.
2. Join it from a phone. Choose your 2.4 GHz network and enter the password.
   The hub address is pre-filled with `wss://orko.gabrielbelli.com:30080`.
3. The node connects, and the ring breathes white while it waits.
4. Adopt it on the **Nodes** tab.

Release builds connect over TLS only and verify the gateway's certificate
against ISRG Root X1. A development build with a plain `ws://` hub is made
with `DEV_HUB`:

```bash
PLATFORMIO_BUILD_FLAGS='-DDEV_HUB=\"ws://192.168.1.177:8003\"' pio run -e usb -t upload
```

## Updates, over the air

```bash
CALLIOPE_NODE=kitchen pio run -e ota -t upload     # or CALLIOPE_NODE=all
```

This builds the image, uploads it to the hub (`CALLIOPE_URL`, default
`https://orko.gabrielbelli.com:30080`), and asks the hub to update the node.
The node pulls the image over its own connection. It keeps the new image only
if it reaches the hub again afterwards; otherwise the bootloader rolls back.
An image can also be uploaded from the Nodes tab.

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

With `lights_enabled` off (the Nodes tab's **Lights** box), the ring stays dark
through everything above: reboots, updates and mute included. The setting is
saved on the node and read before the first frame is drawn.

## Buttons

Every press and release is reported to the hub, which decides what they do.
Four things are handled on the node as well, because they must work whatever
the hub does:

| Button | On the node |
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

## Licence

BSD 2-Clause, like the rest of the repository. Code ported from Espressif's
esp_codec_dev (Apache-2.0) and the libraries fetched at build time are listed
in [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
