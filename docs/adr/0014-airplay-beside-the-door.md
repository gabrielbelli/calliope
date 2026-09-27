# ADR 0014 — AirPlay comes in beside the one door, on an address of its own

**Status:** accepted
**Date:** 2026-09-27

## Context

The owner asked for AirPlay on the Korvo satellite. [ADR 0013](0013-satellites-one-door.md)
put everything the satellites need through the stack's one published port,
the gateway's 30080. AirPlay 2 cannot come through it. A phone finds a
receiver by mDNS and then speaks RTSP to it on 7000, with PTP timing on UDP
319 and 320. None of that is HTTP, and the phone chooses the ports.

The satellite cannot be the receiver either. It is an ESP32 already carrying a
TLS socket, four microphone channels and the speaker stream, and an AirPlay 2
receiver (pairing, AAC and ALAC decoding, PTP) is a firmware project of its
own. It would also break the rule the satellite is built on: it decides
nothing, and every sound it plays comes from the hub.

## Decision

**The receiver is Shairport Sync, in a container beside the hub, and the hub
plays what it receives.** Shairport Sync writes the audio as raw 48 kHz 16-bit
mono, the satellite's speaker format, into a named pipe in a volume the two
containers share. The hub reads the pipe and mixes the music under its own
voice on the satellite the pipe is named for (`services/satellites/app/airplay.py`).
So the satellite is unchanged, and the hub still decides everything it plays:
the music fades out while a conversation is open, and speaker-off still means
nothing reaches it.

**The receiver has its own address on the LAN, by macvlan, not the host's
network.** The options were:

| | Ports on the NAS | mDNS | What the container can reach |
|---|---|---|---|
| Host network, its own avahi | 7000, 319, 320 | a second responder beside TrueNAS's avahi, which avahi itself calls unreliable | the host's network stack |
| Host network, the host's avahi over D-Bus | 7000, 319, 320 | one responder | the host's system bus, as root: root on the host |
| **macvlan, 192.168.1.113** | **none** | **its own, on its own address** | **its own address, and the pipe** |

The macvlan keeps the NAS's own port surface at the gateway's one port, which
is what ADR 0013 was protecting. The receiver is a separate device on the LAN,
`korvo-airplay.local`, outside the DHCP pool and with a fixed MAC. The host
cannot reach a macvlan address, and does not need to: the audio crosses in the
volume.

## Consequences

- A second AirPlay satellite is a second receiver: another service with its
  own name, pipe, address and MAC. One receiver cannot be several speakers.
- The image is third-party and pinned by digest. It runs as root in its own
  network namespace, with only the pipe volume mounted.
- The satellite's DAC clock drifts from the hub's. Replies are too short for
  it to matter; an hour of music is not, so the hub steers by the buffer level
  the satellite reports (`airplay.py`).
- A hub restarted mid-song leaves the receiver writing to a pipe nobody reads,
  until the next play.
- The format is not negotiated: raw PCM cannot say what it is. The hub
  refuses a start that does not arrive at 48 kHz 16-bit mono's rate, because
  anything else would play as full-scale noise.
