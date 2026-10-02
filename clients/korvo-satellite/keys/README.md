# Firmware signing key

A satellite built with a firmware signing public key installs an over-the-air
update only when the update carries a valid signature from the matching
private key. The hub carries the signature but cannot make one, so whoever
controls the hub still cannot put their own firmware on a satellite.

The algorithm is ECDSA on P-256 with SHA-256. The mbedTLS in the ESP32 Arduino
core (2.28) can verify it; it cannot verify Ed25519.

**The repository carries no key, public or private.** A fresh clone builds
unsigned satellites, with a loud warning, until you make a key pair of your
own. A public key in the repository would be someone else's: every satellite
built from it would take updates only from them. `.gitignore` admits only
this `README.md` from this folder.

## Make the key pair (once)

```bash
mkdir -p ~/.config/calliope && chmod 700 ~/.config/calliope
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out ~/.config/calliope/firmware-signing.pem
chmod 600 ~/.config/calliope/firmware-signing.pem
openssl pkey -in ~/.config/calliope/firmware-signing.pem -pubout \
  -out ~/.config/calliope/firmware-signing.pub.pem
```

Both halves stay in `~/.config/calliope`, where the build and the upload find
them. Keep a backup of the private key somewhere other than this machine, such
as a password manager. Without it, the only way to update a satellite that
trusts this key is over USB.

To protect the private key with a passphrase, add `-aes-256-cbc` to the
`genpkey` line. The upload then goes through the openssl CLI, which asks for
the passphrase.

## What the build does with it

The build takes the first public key it finds:

1. the file `CALLIOPE_FIRMWARE_PUBKEY` names; a path that does not exist stops
   the build, so a typo cannot make it unsigned
2. `~/.config/calliope/firmware-signing.pub.pem`
3. `keys/firmware-signing.pub.pem` in this folder, untracked

| A public key | The build | The satellite it makes |
|---|---|---|
| found | defines `FIRMWARE_SIGNING`, compiles the key in and prints its id | refuses an update with no signature (`unsigned image`) or a wrong one (`bad signature`); reports the key as `caps.ota_key` in its hello |
| none | prints an `UNSIGNED BUILD` warning and carries on | accepts any update the hub sends: development only |

`pio run -e ota -t upload` signs with the private key at `CALLIOPE_SIGNING_KEY`
(default `~/.config/calliope/firmware-signing.pem`). It stops before uploading
anything when that key is missing and the build trusts one, or when the key is
not the private half of the public key the build compiled in.

## Moving a satellite to a new key

A satellite trusts only the key it was built with. To change keys, build an
image with the new public key (`CALLIOPE_FIRMWARE_PUBKEY=NEW-KEY.pub.pem`) and sign
it with the old private key. The satellite checks the update against the old
key, installs it, and from then on trusts the new one.

The upload script refuses that pairing on purpose, so sign and upload this one
image by hand. If the hub sets `SATELLITES_FIRMWARE_PUBKEY`, it must still name
the old key until every satellite has moved.

```bash
CALLIOPE_FIRMWARE_PUBKEY=NEW-KEY.pub.pem pio run -e ota   # builds; uploads nothing
BIN=.pio/build/ota/firmware.bin
VERSION=$(git describe --always --dirty --tags)           # what the build stamped
SIG=$(openssl dgst -sha256 -sign OLD-KEY.pem "$BIN" | base64 | tr '+/' '-_' | tr -d '=\n')
curl -fsS --data-binary @"$BIN" -H 'Content-Type: application/octet-stream' \
  -H "Authorization: Bearer $CALLIOPE_API_KEY" \
  "$CALLIOPE_URL/satellites/firmware?model=esp32-korvo-v1.1&version=$VERSION&signature=$SIG"
# then POST /satellites/ota with the sha256 it returns, or use the Satellites tab
```

`CALLIOPE_API_KEY` is a key with the `firmware-release` preset: the gateway
refuses the upload without one.
The version must be the one the build stamped, because the Satellites tab
compares it with what each satellite reports.
