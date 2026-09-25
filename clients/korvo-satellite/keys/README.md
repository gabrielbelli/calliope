# Firmware signing key

A satellite built while `firmware-signing.pub.pem` is in this folder installs
an over-the-air update only when the update carries a valid signature from the
matching private key. The hub carries the signature but cannot make one, so
whoever controls the hub still cannot put their own firmware on a satellite.

The algorithm is ECDSA on P-256 with SHA-256. The mbedTLS in the ESP32 Arduino
core (2.28) can verify it; it cannot verify Ed25519.

The private key never enters this repository. `.gitignore` admits only
`README.md` and `*.pub.pem` from this folder.

## Make the key pair (once)

```bash
mkdir -p ~/.config/calliope && chmod 700 ~/.config/calliope
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out ~/.config/calliope/firmware-signing.pem
chmod 600 ~/.config/calliope/firmware-signing.pem
openssl pkey -in ~/.config/calliope/firmware-signing.pem -pubout \
  -out clients/korvo-satellite/keys/firmware-signing.pub.pem
```

Commit `firmware-signing.pub.pem`. Keep a backup of the private key somewhere
other than this machine, such as a password manager. Without it, the only way
to update a satellite that trusts this key is over USB.

To protect the private key with a passphrase, add `-aes-256-cbc` to the
`genpkey` line. The upload then goes through the openssl CLI, which asks for
the passphrase.

## What the build does with it

| `firmware-signing.pub.pem` | The build | The satellite it makes |
|---|---|---|
| present | defines `FIRMWARE_SIGNING` and compiles the key in | refuses an update with no signature (`unsigned image`) or a wrong one (`bad signature`); reports the key as `caps.ota_key` in its hello |
| absent | prints an `UNSIGNED BUILD` warning and carries on | accepts any update the hub sends: development only |

`CALLIOPE_FIRMWARE_PUBKEY` names another public key file for one build.

`pio run -e ota -t upload` signs with the private key at `CALLIOPE_SIGNING_KEY`
(default `~/.config/calliope/firmware-signing.pem`). It stops before uploading
anything when that key is missing and the build trusts one, or when the key is
not the private half of `firmware-signing.pub.pem`.

## Moving a satellite to a new key

A satellite trusts only the key it was built with. To change keys, build an
image with the new public key here and sign it with the old private key. The
satellite checks the update against the old key, installs it, and from then on
trusts the new one.

The upload script refuses that pairing on purpose, so sign and upload this one
image by hand. If the hub sets `SATELLITES_FIRMWARE_PUBKEY`, it must still name
the old key until every satellite has moved.

```bash
pio run -e ota                     # builds with the new public key; uploads nothing
BIN=.pio/build/ota/firmware.bin
SIG=$(openssl dgst -sha256 -sign OLD-KEY.pem "$BIN" | base64 | tr '+/' '-_' | tr -d '=\n')
curl -fsS --data-binary @"$BIN" -H 'Content-Type: application/octet-stream' \
  "$CALLIOPE_URL/satellites/firmware?model=esp32-korvo-v1.1&version=rekey&signature=$SIG"
# then POST /satellites/ota with the sha256 it returns, or use the Satellites tab
```
