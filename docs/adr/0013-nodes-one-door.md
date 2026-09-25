# ADR 0013 — Nodes come in through the one door, and their socket is not behind a key

**Status:** accepted
**Date:** 2026-09-25

## Decision

Thin audio devices ("nodes": a mic array, a speaker and lights on Wi-Fi) reach
the stack at `wss://<host>:30080/nodes/ws`. That is the gateway's published
port and its certificate, and the gateway relays the socket to a new service,
`voice-nodes`. There is no second published port.

The socket is **not** behind `GATEWAY_API_KEYS`. What a connection may do is
decided by `voice-nodes`, using a per-node token it issues when someone adopts
the node through the authenticated `/nodes` routes. An unadopted connection can
send `hello` and be told `pending`. Nothing else crosses in either direction
until adoption: no microphone audio is accepted, and nothing is sent but that
one word.

Firmware updates travel over the same socket, in frames, and not as a
download from a URL.

## Why one door

`compose.yaml` says that a second `ports:` entry means the file has stopped
doing its job. voice-ui once had one (30081) and it was closed for that reason.
A node port would be the same exception with a worse audience: devices on Wi-Fi
rather than a browser. The gateway already terminates TLS with the wildcard
certificate, so riding on it gives the devices a real certificate chain to
verify (ISRG Root X1, embedded in the firmware) at no cost.

This cost one thing: the gateway, which proxied only request and response, now
relays a WebSocket. It is about forty lines, with `websockets` as the client,
and it is the only one.

## Why not a key

A gateway key in firmware would be in every flash dump of every node, and
rotating it would mean re-flashing the house. A key typed in at setup would be
the same key on every device with more steps. Neither survives the first lost
node.

The adoption token is per node, stored on the hub only as a SHA-256, and
revoked by forgetting the node. That is the property a key was meant to give,
at the right granularity. The gateway's key middleware could not see a
WebSocket scope anyway (it is `@app.middleware("http")`), so this makes a
decision where there was only an accident before.

## Why updates travel over the socket

A download URL would have to be reachable by the node. Through the gateway,
that means either a route exempt from the key, which is a second unauthenticated
surface, or a key on the node, rejected above. The node is already connected,
authenticated and on TLS. Frames of 8 KB over that connection moved a 1.1 MB
image in 15 s on the first board. The frame size is set by the Arduino
WebSockets library, which drops the connection on anything over 15 KB. 16 KB
killed the first attempt 17 ms in.

## Consequences

- `voice-nodes` is the sixth image and optional. With `GATEWAY_NODES_URL=""`
  the routes answer 503, `/health` leaves it out, and the rest of the stack is
  unchanged.
- The gateway's route table grows by the `/nodes` family, listed explicitly
  like everything else. voice-ui's `PROXIED` grows by the routes the Nodes tab
  uses, and the `/ui/api` passthrough gains `PATCH`.
- Release firmware refuses `ws://`. Plain sockets exist only in development
  builds (`DEV_HUB`), pointed at a developer's own machine.
- Anyone on the network can open the socket and appear as a pending node. That
  is the same exposure as a UniFi controller's inform port, and adoption is the
  gate, as it is there.

## What this deliberately does not add

- No device certificates or mutual TLS. The token is the credential. Signed
  firmware images are the next step for the device's own trust.
- No discovery. A node is told its hub in the setup portal, or moved with
  `set-hub`, as UniFi's set-inform does.
