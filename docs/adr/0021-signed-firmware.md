# ADR 0021 — Satellites install only firmware signed on the developer's machine, and the hub cannot waive it

**Status:** accepted
**Date:** 2026-09-25

## Context

Firmware reaches a satellite over its socket to the hub
([ADR 0013](0013-satellites-one-door.md)). Whoever controls the hub, or holds
a gateway key that reaches `POST /satellites/firmware` and
`POST /satellites/ota`, could send any image, and a satellite has
microphones in a room. ADR 0013 named signed images as the next step for the
device's own trust.

## Decision

A satellite built with a public key installs an update only when it carries
an ECDSA P-256 signature, by the matching private key, over the image's
SHA-256, in DER.

- **The private key stays on the developer's machine**
  (`~/.config/calliope/firmware-signing.pem` by default). The upload script
  signs there and sends the signature with the image. The hub only carries
  it: in with the upload, out in the `ota` message.
- **The satellite checks it itself, twice**: against the SHA-256 the hub
  announces, before it writes the spare slot, and against the digest of the
  bytes it received, before the slot is made bootable. mbedTLS in the Arduino
  core verifies it.
- **The hub cannot waive it.** A build with a key refuses an unsigned or
  wrongly signed image whatever the hub says. `SATELLITES_FIRMWARE_PUBKEY`
  makes the hub refuse such an upload early, and skip a satellite that would
  refuse the image, but it adds no trust: it saves a 15 s transfer that would
  end in `bad signature`.
- **The repository carries no key.** A clone builds unsigned satellites, with
  a warning, until its owner makes a key pair. A public key in the repository
  would make every satellite built from it take updates only from the
  maintainer.
- **A signed build says so** in `hello.caps.ota_key`, the id of its key.

## Moving to a new key

A satellite trusts only the key it was built with. To move it, build an image
with the new public key and sign that image with the old private key: the
satellite checks it against the old key, installs it, and from then on
trusts the new one. The upload script refuses that pairing on purpose, so
this one image is signed and uploaded by hand
([keys/README.md](../../clients/korvo-satellite/keys/README.md)).

## What it costs

- **Losing the private key means USB.** A satellite that trusts a key nobody
  has can be updated only by flashing it by hand.
- **The first signed image goes over the air to an unsigned satellite
  unchecked.** A satellite built without a key accepts any image, so the
  first signed build installs as any update would. From then on it refuses
  unsigned ones.
- **ECDSA, not Ed25519.** The mbedTLS in the ESP32 Arduino core (2.28) cannot
  verify Ed25519.
- **The check has been run on a desktop against the same mbedTLS release**
  (`clients/korvo-satellite/scripts/sig_host_check.sh`) and on the board for a refused and an
  accepted image, but its time on the ESP32 is not measured.

## Rejected

- **Signing on the hub.** A hub that signs can sign anything, which is the
  attack this decision exists to stop.
- **A key the hub can switch off.** The same attack, one setting away.
