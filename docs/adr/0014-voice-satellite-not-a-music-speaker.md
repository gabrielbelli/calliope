# ADR 0014 — The Korvo is a voice satellite, not a music speaker

**Status:** accepted (AirPlay built on 27 Sep 2026 and withdrawn the same day)
**Date:** 2026-09-27

## Context

AirPlay 2 was added to the Korvo satellite: Shairport Sync in a sidecar on its
own LAN address (macvlan), writing into a named pipe, and the hub mixing that
music under its own voice on the satellite (commits `ba8078e` and `32c1261`).
It worked end to end. Played through the board's 3.5 mm jack into a stereo
amplifier, it sounded thin, with the stereo image smeared, and moving one's
head in front of the speakers was dizzying.

The cause is the board, not the software (main board schematic, sheet 4):

- **One channel.** The ES8311 is a mono codec. There is no second DAC channel
  anywhere on the board.
- **The jack is driven differentially.** The codec's OUTP goes to the tip and
  OUTN, its inverse, to the ring. Headphones cope. Two speakers on an aux
  cable play the same signal in opposite polarity, which cancels the bass and
  moves the cancellation around the listener's head.
- **A plug cuts the echo reference.** The jack's normally-closed contacts feed
  both the speaker amplifier and the ES7210 loopback, so with a plug in, the
  hub's echo cancellation has nothing to cancel against, and wake words have
  to be heard over the music unaided.

## Decision

The Korvo is voice I/O: microphones, replies, earcons, lights and buttons.
Music belongs on a player built for it, beside the amplifier. The AirPlay code,
the sidecar, its macvlan network and its volume were removed. The hub can
still fade another player's music on a wake word later, through that
player's own control, without the satellite carrying the music.

## If it comes back

- Stereo on this board needs an I2S DAC (a PCM5102A, say) wired to I2S0's
  BCLK, LRCK and DOUT (GPIO25, 22, 13, at R212-R214) beside the ES8311, and
  stereo frames from the hub.
- Shairport Sync's pipe backend writes the whole lead-in at start as silence,
  at once (its player.c, for an output with no delay function): a buffer that
  caps or a rate check that times it breaks every start. The code is in the
  two commits above.
