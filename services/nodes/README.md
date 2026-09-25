# voice-nodes

The hub for thin audio devices. A node is a microphone array, a speaker and a
ring of lights on Wi-Fi, and it makes no decisions: it streams its microphones
here and plays, lights and reports whatever it is told.

```text
node ──wss /nodes/ws──▶ voice-gateway :30080 ──ws──▶ voice-nodes :8003
                         (TLS, the one door)          adoption, config, audio, OTA
browser ──/ui/api/nodes──▶ voice-ui ──▶ voice-gateway ──▶ voice-nodes
```

Sibling of `stt`, `tts`, `tts-long`, `gateway` and `ui`. The first node is the
ESP32-Korvo v1.1: [`clients/korvo-node`](../../clients/korvo-node/README.md).
The decision to bring nodes in through the gateway, and to keep their socket
out of `GATEWAY_API_KEYS`, is [ADR 0013](../../docs/adr/0013-nodes-one-door.md).

## Status

- **Working on the first board:** adoption, config, lights, a four-channel
  capture over Wi-Fi with no dropped packets, and a 1.1 MB over-the-air update
  in 15 s with rollback, through the gateway relay.
- **Not built yet:** the live audio front-end (echo cancellation, beamforming,
  noise suppression), wake words, routing to assistants, and MQTT.

## Adoption

A node that has never been adopted says `hello` with no token and is answered
`pending`. It stays connected and shows up in `GET /nodes` and on the Nodes tab.
From a pending node the hub accepts no microphone audio, and it sends that node
nothing but `pending`.

`POST /nodes/{id}/adopt` issues a random token. The node stores it and says
`hello` again with it; the hub answers `welcome` with the node's name and
config. The hub keeps only the token's SHA-256, so a copy of `nodes.json` cannot
impersonate a node. `POST /nodes/{id}/forget` drops the record and tells the
node, which goes back to pending.

A node is addressed by its id (the MAC, with or without colons) or its name.

## Routes

| Route | What it does |
|---|---|
| `WS /nodes/ws` | The device connection. Protocol below. |
| `GET /nodes` | Every node seen since the hub started, adopted or not |
| `GET /nodes/events` | Server-sent events: buttons, status, updates, nodes coming and going |
| `GET /nodes/{id}` | One node |
| `PATCH /nodes/{id}` | `name`, `volume` (0-100), `mic_gain_db` (0-37.5), `mic_enabled`, `speaker_enabled`, `local_volume_buttons`, `lights_enabled` |
| `POST /nodes/{id}/adopt` | `{"name": "..."}` |
| `POST /nodes/{id}/forget` | |
| `POST /nodes/{id}/identify` | Blink for five seconds. Works before adoption, which is the point. |
| `POST /nodes/{id}/reboot` | |
| `POST /nodes/{id}/lights` | `{"mode": "off|solid|pulse|spin|pixels", "color": [r,g,b], "brightness": 0-255, "pixels": [[r,g,b], ...]}` |
| `POST /nodes/{id}/tone` | `{"frequency": 440, "seconds": 1}` on the node's speaker |
| `POST /nodes/{id}/say` | `{"text": "...", "voice": "bm_george"}`: Kokoro, via `NODES_TTS_URL` |
| `POST /nodes/{id}/flush` | Drop queued speaker audio |
| `GET /nodes/{id}/listen?seconds=5&channel=` | A WAV of the raw mic channels, up to 60 s |
| `POST /nodes/{id}/set-hub` | `{"url": "wss://host:port"}`: the node saves it and reboots onto that hub |
| `GET /nodes/firmware` | Uploaded images |
| `POST /nodes/firmware?model=&version=` | The `.bin` as the raw body. It must start with the ESP32 image magic (0xE9) and fit a 4 MB slot. |
| `DELETE /nodes/firmware/{sha256}` | |
| `POST /nodes/ota` | `{"node": "<id>|<name>|all", "sha256": "..."}`. Images are only sent to adopted, online nodes of the image's model. |

## The device protocol

One WebSocket. Control travels as JSON text frames; audio and firmware travel
as binary frames whose first byte names the kind.

**Binary frames**

| Kind | Direction | Layout |
|---|---|---|
| `1` mic | node → hub | 16-byte header (`kind, 0, channels, 0, seq u32, capture time µs u64`, little-endian), then interleaved s16le |
| `2` speaker | hub → node | same header, then mono s16le at the node's speaker rate |
| `3` firmware | hub → node | `kind, 0, 0, 0, offset u32`, then up to 8 KB of image |

The Korvo sends 4 channels at 16 kHz in 20 ms frames: the speaker loopback
first, then the three microphones. It plays 48 kHz mono. The hub paces speaker
audio at real time plus a 300 ms lead.

**Text frames, node → hub:** `hello` (id, model, firmware, token, caps),
`status` (every 10 s: RSSI, heap, mute, volume, drop counters), `button`
(`press` or `release`, with `held_ms`), `ota` (`started`, `progress`,
`rebooting`, `failed`, `verified`), and `ota_next` (`offset`).

**Text frames, hub → node:** `pending`, `adopt`, `welcome` (name and config),
`config` (any subset), `lights`, `identify`, `reboot`, `forget`, `set_hub`,
`flush`, and `ota` (size, sha256, version).

**Updates.** The hub sends `ota`. The node begins writing to its spare slot and
asks for chunks with `ota_next`, one at a time, hashing as it goes. On a match
it reboots into the new image, which the bootloader holds as pending-verify. The
new image marks itself valid only after it hears from the hub again. A crash, a
hang, or 180 s without the hub rolls it back to the image it had.

## Configuration

| Variable | Default | |
|---|---|---|
| `NODES_DATA_DIR` | `/data` | `nodes.json` and `firmware/`. Mount a volume: losing it un-adopts every node. |
| `NODES_TTS_URL` | unset | tts-stack's base URL, for `say`. Unset, `say` answers 503 and names this variable. |
| `NODES_TTS_VOICE` | `bm_george` | |
| `NODES_API_KEYS` | unset | As on the other backends. Behind the gateway it stays unset. |
| `NODES_LOG_LEVEL` | `INFO` | |

## Tests

```bash
pip install -r services/nodes/requirements-dev.txt   # from the repository root
cd services/nodes && python -m pytest -q
```

The device is played by Starlette's test socket. Nothing starts a server, and
no board is needed.

## What is not here

- The audio front-end, wake words and routing. Microphone audio reaches the hub
  and stops at `listen`.
- Discovery. A node is told its hub in the setup portal, or moved with
  `set-hub`.
- Signed firmware. The node checks the SHA-256 the hub gives it, and the
  bootloader rolls back an image that cannot reach the hub, but the node does
  not yet verify who built the image.

## Licence

BSD 2-Clause. See [LICENSE](LICENSE).
