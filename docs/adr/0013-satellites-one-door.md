# ADR 0013 — Satellites come in through the one door, and their socket is not behind a key

**Status:** accepted
**Date:** 2026-09-25

## Renamed

This feature was called **nodes** until 2026-09-25, when it was renamed
**satellites**. The decision below did not change.

**A pre-release migration.** The rename came before the feature was
released, so only a deployment that ran a pre-release image, and a board
flashed from one, has anything below to do. A new install has none of it:
no `voice-nodes`, no `NODES_*` settings, no `nodes.json`, and firmware that
dials `/satellites/ws`. What the hub and gateway keep for the old names (the
`/nodes/ws` alias, reading `nodes.json`, the start-up warnings about
`NODES_*`) does nothing on such an install, and goes once no board reports
firmware from before the rename. The volume keeps the name `nodes-data` for
the reason given below, on new installs too.

### What changed for an operator

| Before | After | What to do |
|---|---|---|
| Service and container `voice-nodes` | `voice-satellites` | Remove the old container when deploying: `docker compose up -d --remove-orphans`, or remove `voice-nodes` by hand, then check that it is gone. It has `restart: unless-stopped`, so left behind it keeps running on the same volume as the new one. With MQTT set, it also publishes under the same Home Assistant unique ids, and takes the entities back each time it restarts. |
| Image `ghcr.io/gabrielbelli/calliope-nodes` | `ghcr.io/gabrielbelli/calliope-satellites` | The first push creates a new package. Check that it is public before deploying: with `pull_policy: always`, a private package fails the deploy. |
| Routes `/nodes/...` | `/satellites/...` | Scripts and bookmarks move. The device socket keeps its old path as well (below). |
| JSON field `node` | `satellite` | In the body of `POST /satellites/ota` and `POST /satellites/routing/test`, and in every event from `GET /satellites/events`. |
| Settings `NODES_*` | `SATELLITES_*` | Rename them, including the secrets in the app's settings. The hub does not read the old names, so an MQTT URL still set as `NODES_MQTT_URL` leaves MQTT off. At start the hub logs a warning for each `NODES_*` variable that is still set, with the name it now reads. The exception is a secret that a wake word's action names (below). |
| `GATEWAY_NODES_URL`, `GATEWAY_NODES_TIMEOUT` | `GATEWAY_SATELLITES_URL`, `GATEWAY_SATELLITES_TIMEOUT` | The gateway does not read the old names. Its default URL is now `http://voice-satellites:8003`. |
| Gateway `/health`: `backends.nodes` | `backends.satellites` | Move any monitoring that reads the key. |
| MQTT base `calliope/nodes` | `calliope/satellites` | Home Assistant entities survive, because their unique ids did not change. Automations on raw topics, such as `calliope/nodes/<id>/button/<name>` or `calliope/nodes/<id>/wake`, stop firing. Either set `SATELLITES_MQTT_BASE=calliope/nodes` to keep the old topics, or move the automations. The old retained topics stay on the broker until someone clears them. |
| Setup network `calliope-node-XXXX` | `calliope-sat-XXXX` | Only a board on the new firmware uses the new name. |
| The Nodes tab | The Satellites tab | |

### What kept its old name, and why

- **The device socket also answers at `/nodes/ws`**, in the gateway and in the
  hub, with the same handler as `/satellites/ws`. A board in the field runs
  firmware that connects to `/nodes/ws`, and its next firmware arrives over
  that socket. Without the old path, the board could not be updated over the
  air and would need USB. Remove the alias when no board reports firmware from
  before the rename.
- **The volume is still `nodes-data`.** Compose names a volume after the
  project and the volume key, and nothing else, so a new key is a new, empty
  volume on the next `up`. Every board would come back pending,
  `wake_words.json` would be gone, so every word would go back to
  `SATELLITES_WAKE_WORDS` and echo what it hears, and the stored keys, the
  firmware store and the fetched models would go with it.
- **The files on the volume keep working.** The hub reads `nodes.json` once,
  when `satellites.json` does not exist yet, and writes `satellites.json`. A
  `rules.json` from either name is read once into `wake_words.json`
  ([the satellites README](../../services/satellites/README.md#saving)).
- **Actions carried over from routing rules still name the old secrets.** A
  rule saved before the rename says `"token_env": "NODES_HA_TOKEN"` or
  `"api_key_env": "NODES_LLM_API_KEY"`, and the move to `wake_words.json`
  kept that name in the word's action, which reads that variable. If you
  rename the secret and not the action, the action fails with
  "NODES_HA_TOKEN is not set". Either keep the old variable while an action
  names it, or change the action (the wake word's Action on the Satellites
  tab, or `PUT /satellites/wake-words`) and then rename the secret. The
  start-up warning does not report a `NODES_*` variable that an action names.
  It reports an action that names a `NODES_*` variable that is not set.
- **The firmware's settings namespace is still `node`**, so a board updated
  over the air keeps its hub address, adoption token and settings.
- **The Home Assistant unique ids** (`calliope_<id>_*`) did not change.

## Since

**2026-09-25:** routing moved from `rules.json` onto each wake word's action,
in `wake_words.json`. `PUT /satellites/routing` now answers 409
`routing_per_wake_word`, and the Satellites tab has no Routing card. Signed
firmware images, which the last bullet below calls the next step, exist:
[ADR 0021](0021-signed-firmware.md). The decision below is unchanged.

**2026-09-28:** the CA roots the firmware trusts can be named at build time
(`CALLIOPE_HUB_CA`), so the gateway's certificate need not be Let's
Encrypt's.

## Decision

Thin audio devices ("satellites": a mic array, a speaker and lights on Wi-Fi)
reach the stack at `wss://<host>:30080/satellites/ws`. That is the gateway's
published port and its certificate, and the gateway relays the socket to a new
service, `voice-satellites`. There is no second published port.

The socket is **not** behind `GATEWAY_API_KEYS`. What a connection may do is
decided by `voice-satellites`, using a per-satellite token it issues when
someone adopts the satellite through the authenticated `/satellites` routes. An
unadopted connection can send `hello` and be told `pending`. Nothing else
crosses in either direction until adoption: no microphone audio is accepted,
and nothing is sent but that one word.

Firmware updates travel over the same socket, in frames, and not as a
download from a URL.

## Why one door

`compose.yaml` says that a second `ports:` entry means the file has stopped
doing its job. voice-ui once had one (30081) and it was closed for that reason.
A satellite port would be the same exception with a worse audience: devices on
Wi-Fi rather than a browser. The gateway already terminates TLS with the
wildcard certificate, so riding on it gives the devices a real certificate
chain to verify (its CA's root, embedded in the firmware: Let's Encrypt's
unless the build names another with `CALLIOPE_HUB_CA`) at no cost.

This cost one thing: the gateway, which proxied only request and response, now
relays a WebSocket. It is about forty lines, with `websockets` as the client,
and it is the only one.

## Why not a key

A gateway key in firmware would be in every flash dump of every satellite, and
rotating it would mean re-flashing the house. A key typed in at setup would be
the same key on every device with more steps. Neither survives the first lost
satellite.

The adoption token is per satellite, stored on the hub only as a SHA-256, and
revoked by forgetting the satellite. That is the property a key was meant to
give, at the right granularity. The gateway's key middleware could not see a
WebSocket scope anyway (it is `@app.middleware("http")`), so this makes a
decision where there was only an accident before.

## Why updates travel over the socket

A download URL would have to be reachable by the satellite. Through the
gateway, that means either a route exempt from the key, which is a second
unauthenticated surface, or a key on the satellite, rejected above. The
satellite is already connected, authenticated and on TLS. Frames of 8 KB over
that connection moved a 1.1 MB image in 15 s on the first board. The frame size
is set by the Arduino WebSockets library, which drops the connection on
anything over 15 KB. 16 KB killed the first attempt 17 ms in.

## Consequences

- `voice-satellites` is the sixth image and optional. With
  `GATEWAY_SATELLITES_URL=""` the routes answer 503, `/health` leaves it out,
  and the rest of the stack is unchanged.
- The gateway's route table grows by the `/satellites` family, listed
  explicitly like everything else. voice-ui's `PROXIED` grows by the routes the
  Satellites tab uses, and the `/ui/api` passthrough gains `PATCH`.
- Release firmware refuses `ws://`. Plain sockets exist only in development
  builds (`DEV_HUB`), pointed at a developer's own machine.
- Anyone on the network can open the socket and appear as a pending satellite.
  That is the same exposure as a UniFi controller's inform port, and adoption
  is the gate, as it is there. A satellite's id is its MAC, which is no secret,
  so while an adopted satellite is connected, a `hello` with its id and without
  its token is closed (1008). Otherwise any connection could push the real
  satellite offline and have its own status shown as that satellite's.

## What this deliberately does not add

- No device certificates or mutual TLS. The token is the credential. Signed
  firmware images are the next step for the device's own trust.
- No discovery. A satellite is told its hub in the setup portal, or moved with
  `set-hub`, as UniFi's set-inform does.
