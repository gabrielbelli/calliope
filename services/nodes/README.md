# voice-nodes

The hub for thin audio devices. A node is a microphone array, a speaker and a
ring of lights on Wi-Fi, and it makes no decisions. It streams its microphones
here and plays, lights and reports whatever it is told. The hub listens for a
wake word on every node, sends what follows to an assistant, and speaks the
answer.

```text
node ──wss /nodes/ws──▶ voice-gateway :30080 ──ws──▶ voice-nodes :8003
                         (TLS, the one door)          adoption, config, audio, OTA,
                                                      wake words, routing
browser ──/ui/api/nodes──▶ voice-ui ──▶ voice-gateway ──▶ voice-nodes
voice-nodes ──▶ stt-stack, tts-stack, and whatever a rule names (Home Assistant, an LLM, a webhook)
```

Sibling of `stt`, `tts`, `tts-long`, `gateway` and `ui`. The first node is the
ESP32-Korvo v1.1: [`clients/korvo-node`](../../clients/korvo-node/README.md).
The decision to bring nodes in through the gateway, and to keep their socket
out of `GATEWAY_API_KEYS`, is [ADR 0013](../../docs/adr/0013-nodes-one-door.md).

## Status

Measured on orko (25 Sep 2026) with the first board, an ESP32-Korvo, adopted
over `wss://orko.gabrielbelli.com:30080`:

- **Working on the board:** adoption (keeping the node's own settings), config,
  lights off, a four-channel stream with no dropped frames, and live listening:
  front-end plus wake word at a real-time factor of 0.09 on orko's Xeon, 24 % of
  one core, 215 MiB.
- **Signed updates, on the board:** a signed 1.17 MB release image installed in
  20 s, including through the Nodes tab's route. The hub will not send an
  unsigned image to a node that advertises a key. With the hub bypassed, an
  image signed by another key was refused by the node itself
  (`bad signature`), and the node stayed on its image.
- **The listening path on orko, from recorded clips (`/inject`):** "hey jarvis,
  what time is it" is detected (score 0.995), transcribed by Parakeet as "What
  time is it?", routed by the default echo rule, and answered by Kokoro
  (1.3 s). "The weather is fine today" is ignored.
- **Not yet heard live:** a wake word spoken in the room. Earcons and ducking
  have not played on the board yet; its speaker is off.

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
| `GET /nodes` | Every node seen since the hub started, adopted or not, with its listening state and earcons |
| `GET /nodes/events` | Server-sent events: buttons, wake words, routing, status, updates, nodes coming and going |
| `GET /nodes/{id}` | One node |
| `PATCH /nodes/{id}` | `name`, `volume` (0-100), `mic_gain_db` (0-37.5), `mic_enabled`, `speaker_enabled`, `local_volume_buttons`, `lights_enabled`, `buttons` |
| `POST /nodes/{id}/adopt` | `{"name": "..."}` |
| `POST /nodes/{id}/forget` | |
| `POST /nodes/{id}/identify` | Blink for five seconds. Works before adoption, which is the point. |
| `POST /nodes/{id}/reboot` | |
| `POST /nodes/{id}/lights` | `{"mode": "off|solid|pulse|spin|pixels", "color": [r,g,b], "brightness": 0-255, "pixels": [[r,g,b], ...]}`. 409 for a node with `lights_enabled` false. |
| `POST /nodes/{id}/tone` | `{"frequency": 440, "seconds": 1}` on the node's speaker |
| `POST /nodes/{id}/say` | `{"text": "...", "voice": "bm_george"}`: Kokoro, via `NODES_TTS_URL` |
| `POST /nodes/{id}/flush` | Stop: drop the speaker audio queued and playing, and cancel the conversation in progress |
| `GET /nodes/{id}/listen?seconds=5&channel=` | A WAV of the raw mic channels, up to 60 s |
| `POST /nodes/{id}/set-hub` | `{"url": "wss://host:port"}`: the node saves it and reboots onto that hub |
| `POST /nodes/{id}/inject?play=0&wake_word=` | A 16 kHz mono 16-bit WAV as the body, through the wake word, endpoint and routing path as if the node had heard it. [Verifying](#verifying-the-pipeline-without-a-voice). |
| `GET /nodes/routing` | The rules, which secret variables are set (never their values), the STT and TTS URLs, warnings |
| `PUT /nodes/routing` | Replace the whole ruleset. A bad one is a 422 and the old rules stay. |
| `POST /nodes/routing/test` | `{"node", "wake_word", "text"}`: a typed sentence through the rules and TTS. Plays nothing. |
| `GET /nodes/firmware` | Uploaded images |
| `POST /nodes/firmware?model=&version=&signature=` | The `.bin` as the raw body. It must start with the ESP32 image magic (0xE9) and fit a 4 MB slot. `signature` is base64 or base64url DER ECDSA. |
| `DELETE /nodes/firmware/{sha256}` | |
| `POST /nodes/ota` | `{"node": "<id>|<name>|all", "sha256": "..."}`. Images are only sent to adopted, online nodes of the image's model, and not to a node that would refuse the signature. |

The gateway routes all of these. The Nodes tab uses all but `inject`, which is
for scripts: a button that runs a clip through a node's real rules would be one
press from Home Assistant acting on it.

## The device protocol

One WebSocket. Control travels as JSON text frames; audio and firmware travel
as binary frames whose first byte names the kind.

**Binary frames**

| Kind | Direction | Layout |
|---|---|---|
| `1` mic | node → hub | 16-byte header (`kind, 0, channels, 0, seq u32, capture time µs u64`, little-endian), then interleaved s16le |
| `2` speaker | hub → node | same header, then mono s16le at the node's speaker rate |
| `3` firmware | hub → node | `kind, 0, 0, 0, offset u32`, then up to 8 KB of image |
| `4` earcon | hub → node | `kind, 0, 0, 0, offset u32`, then up to 8 KB of s16le at 48 kHz |

The Korvo sends 4 channels at 16 kHz in 20 ms frames: the speaker loopback
first, then the three microphones. It plays 48 kHz mono. The hub paces speaker
audio at real time plus a 300 ms lead.

**Text frames, node → hub:** `hello` (id, model, firmware, token, caps),
`status` (every 10 s: RSSI, heap, mute, volume, drop counters, `duck`,
`earcons_ready`), `button` (`press` or `release`, with `held_ms`), `ota`
(`started`, `progress`, `rebooting`, `failed`, `verified`; a signed build fails
with `unsigned image` or `bad signature`), `ota_next` (`offset`), and for
earcons `earcons` (`ready`, `items`, `last_load_us`), `earcon_next` (`id`,
`offset`), `earcon_stored` (`id`, `size`, `sha256`) and `earcon_failed` (`op`
put, play or delete, `id`, `error`).

`hello.caps` names what the node can do: `mic`, `speaker`, `lights` (the LED
count), `buttons`, and on current firmware `earcons` (`max`, `max_bytes`,
`rate`), `duck: true`, and on a signed build `ota_key`. The hub sends earcon,
duck and signature messages only to a node whose caps say it takes them; older
firmware ignores them without a word.

**Text frames, hub → node:** `pending`, `adopt`, `welcome` (name and config),
`config` (any subset), `lights`, `identify`, `reboot`, `forget`, `set_hub`,
`flush`, `ota` (size, sha256, version, and `signature` when the image has one),
`earcon_put` (`id`, `size`, `sha256`), `earcon` (`id`: play it), `earcon_list`,
`earcon_delete`, `duck` (`level` 0-100 on the volume scale, `ms`, 0 for until
`unduck`) and `unduck`.

**Updates.** The hub sends `ota`. The node begins writing to its spare slot and
asks for chunks with `ota_next`, one at a time, hashing as it goes. On a match
it reboots into the new image, which the bootloader holds as pending-verify. The
new image marks itself valid only after it hears from the hub again. A crash, a
hang, or 180 s without the hub rolls it back to the image it had.

**Earcons** are short sounds the node keeps in flash: `wake` (a rising two-tone,
150 ms), `done` (the same falling, 150 ms) and `error` (two low pulses, 210 ms),
synthesised in `app/earcons.py` and peaking at -13 to -12 dBFS. After a
`welcome` the hub asks for `earcon_list` and uploads whatever the node lacks or
holds with other content, one at a time, as for firmware. A node whose storage
is still being formatted answers `ready: false`, and the hub asks again every
5 s for up to two minutes. It plays an earcon only when the node holds it.

## Listening

Every adopted node with its microphone enabled and not muted is listened to:

```text
mic frames ─▶ bounded queue ─▶ FrontEnd ─▶ WakeWords ─▶ Endpointer ─▶ router ─▶ reply
 (socket)     (1 s, drops      echo cancel,  hey_jarvis   the command   STT, rule,  on the node
               the oldest)     beam, denoise              after it      TTS         the rule names
```

The socket loop only queues frames. One task per node drains the queue in
batches and runs the signal work (`app/listening.py`) in a thread pool, so the
event loop never does it. A full queue drops its oldest frame and counts it as
`mic_dropped` in `GET /nodes`.

1. **Front-end** (`app/frontend.py`): echo cancellation against the node's own
   loopback channel, MVDR beamforming across the three microphones, and noise
   suppression, on 16 ms blocks, 16 ms behind the input. `NODES_FRONTEND=0`
   skips it and uses the first microphone as sent.
2. **Wake words** (`app/wakeword.py`): openWakeWord's ONNX models, one stream
   per node on sessions loaded once and shared by all of them.
3. **Endpointer:** webrtcvad. The command ends after 800 ms without speech, at
   10 s, or at 4 s if no speech started.
4. **A conversation** per wake word, one per node at a time: the `wake` earcon
   if the node holds it, the ring pointed at the talker, the node ducked (and
   the node the rule answers on, if that is another one), then the router. The
   reply is played on the rule's `reply_to` node, over nothing: audio still
   playing there is dropped and the duck lifted first, because the firmware
   ducks the hub's audio and the reply is the hub's audio. An error plays
   `error`; a success with nothing to say plays `done`.

A wake word heard while a reply is still playing starts a new conversation. The
node ducks the old reply while the new command is heard, and drops it when the
new answer arrives.

**Events.** Each conversation publishes `{"type": "wake", "node", "wake_word",
"score", "direction"}` and then `{"type": "routed", "node", "wake_word",
"rule_id", "reply_to", "error", "transcript", "reply_text", "timings_ms",
"endpoint", "command_s", "played", "note"}` on `/nodes/events`. Transcripts are
in the event stream, which is behind the same keys as the rest of the API, and
not in the INFO log.

**Measured** on darwin (Apple silicon) and in the image on arm64, 2026-09-25:

| What | Result |
|---|---|
| The two "hey jarvis" fixtures on three simulated microphones, white noise -60 to -27 dBFS, through the front-end | detected every time; the command ended on silence 0.4 to 0.7 s after the speech |
| The same with the raw first microphone at -27 dBFS | detected, and the endpointer never ended: webrtcvad takes that much noise for speech |
| Hub resident memory in the image, nodes streaming 4 channels in real time | 228 MiB idle with hey_jarvis loaded, 239 MiB with one node, 289 MiB with three, 326 MiB with six |
| The same run | every wake word heard, 0 microphone frames dropped at six nodes |
| Front-end real-time factor in the image | 0.03 to 0.05 per node |
| Wake word memory per extra node, two wake words loaded | 3.6 MB with shared sessions, against 55 MB for a second instance |

Not measured: anything on the board. The Korvo's microphone spacing (65 mm) and
orientation are assumed, so `direction` is relative to the board and may be
rotated or mirrored, and wake word thresholds are untuned for its microphones.

### Lights

While a node listens, its ring points at the talker (`pixels`, full on the
nearest LED, soft on its neighbours), or pulses when there is no direction yet.
It spins while the router works and goes out at the end. LED 0 is assumed to
sit towards microphone 1, which has not been checked on a board.

**A node with `lights_enabled` false is never sent `lights`.** Every lights
message goes through one function (`Hub.send_lights`) that reads the setting at
the moment of sending, and `POST /nodes/{id}/lights` answers 409 rather than go
around it. A ring left lit when lights were turned off mid-conversation is put
out when they are turned back on. `tests/test_pipeline.py` runs a whole wake,
route and reply cycle on a dark node and checks that it received no `lights`.

### Buttons

`buttons` in a node's config maps a button and an edge to an action:

```json
{"play": {"press": "ptt"}, "set": {"press": "stop"}, "mode": {"release": "webhook:http://nodered:1880/korvo"}}
```

| Action | What happens |
|---|---|
| `ptt` | Push-to-talk: listen as if the wake word `ptt` had been heard |
| `stop` | What `POST /nodes/{id}/flush` does |
| `webhook:<url>` | POST `{"node", "node_id", "button", "action", "held_ms"}` to the URL, 10 s at most, no redirects followed |
| `none` | Nothing |

The default is the one above without the webhook. A PATCH replaces the whole
mapping. `rec` cannot be mapped: the firmware mutes on it before the hub hears
of the press. Every press is still published as an event, whatever it maps to.
The mapping stays on the hub and is never sent to the node.

## Routing

`rules.json` in `NODES_DATA_DIR` decides what happens after a wake word. The
first rule, in file order, whose `wake_word` and `nodes` both match wins.

```json
{"version": 1, "rules": [
  {"id": "kitchen-ha", "wake_word": "hey_jarvis", "nodes": ["kitchen"],
   "destination": {"type": "ha_conversation", "url": "http://homeassistant.lan:8123"},
   "language": "en-GB"},
  {"id": "ask-anywhere", "wake_word": "*", "reply_to": "same",
   "destination": {"type": "llm", "base_url": "http://ollama:11434/v1", "model": "llama3.2"}}
]}
```

| Field | |
|---|---|
| `id` | Unique; named in logs and events |
| `wake_word` | A model name, `ptt` for push-to-talk, or `*` |
| `nodes` | Ids or names; empty means every node |
| `destination` | `ha_conversation` (`url`, `token_env`, `agent_id`), `llm` (`base_url`, `model`, `system`, `api_key_env`, `max_tokens`), `webhook` (`url`, `token_env`), or `echo` |
| `reply_to` | `same`, `none` (act and say nothing), or another node's id or name |
| `language` | BCP 47, as `pt-BR`. STT gets the first part, Home Assistant the whole. |
| `voice` | A Kokoro voice for this rule; unset, `NODES_TTS_VOICE` |

Without a file, one rule echoes anything heard back to the node that heard it,
which proves microphone, STT, TTS and speaker before anything else exists. A
file that does not load turns routing off rather than falling back to echo, and
`GET /nodes/routing` says why.

**A rule never holds a secret.** It names an environment variable
(`token_env`, `api_key_env`), unknown fields are refused, a variable name must
look like one (so a pasted token is a 422), and a URL with a user and password
is refused. Destination URLs are not filtered for private addresses on purpose:
Home Assistant on the LAN is the main target, and whoever can PUT a rule can
already reflash every node. Redirects are not followed. Every external call has
a hard time limit: STT 30 s, TTS 30 s, the destination its own `timeout`.

## Home Assistant over MQTT

With `NODES_MQTT_URL` set, every adopted node is one Home Assistant device, by
discovery: Wi-Fi signal, online, microphone, speaker and lights switches,
volume, last wake word, a wake word event and one event per button. A switch
goes through the same code as `PATCH /nodes/{id}`, and nothing but those four
settings can be changed from the broker. Button presses and wake words are
dropped while the broker is away rather than delivered late; state, discovery
and availability are retained and sent again on every reconnect. Forgetting a
node removes its device. Injected test clips are not published. Checked against
Home Assistant 2026.8.1's own MQTT integration: 14 entities under one device.

## Signed firmware

A node built with a public key (`clients/korvo-node/keys`) installs an update
only with an ECDSA P-256 signature over the image by the matching private key,
made on the developer's machine. The hub only carries it: in with the upload's
`signature`, out in the `ota` message. With `NODES_FIRMWARE_PUBKEY` set, the hub
also refuses an unsigned or wrongly signed upload with 400 `bad_signature`, and
`POST /nodes/ota` skips a node whose `caps.ota_key` the image would not satisfy,
saying why, rather than sending 15 s of image to be refused. The running
unsigned firmware ignores `signature`, so the first signed build installs over
the air as usual; after that the node requires one.

## Verifying the pipeline without a voice

`POST /nodes/{id}/inject` runs a recorded clip through the same path a live
node's audio takes, from the wake word on, and answers with what happened.
Without `?play=1` nothing at all is sent to any node: no sound, no light, no
duck. Its events are marked `"injected": true` and are not sent to MQTT.

```bash
say -v Samantha -o /tmp/q.aiff "hey jarvis, what time is it"   # writes a file, plays nothing
ffmpeg -loglevel error -i /tmp/q.aiff -ar 16000 -ac 1 -c:a pcm_s16le /tmp/q.wav
curl -sS -H "Authorization: Bearer $KEY" --data-binary @/tmp/q.wav \
  https://orko.gabrielbelli.com:30080/nodes/kitchen/inject | jq
```

The answer carries `heard` (wake word, score, position), `command` (why it
ended, its length), `outcome` (rule, transcript, reply text, errors, timings,
reply audio length) and `played`. `?wake_word=ptt` skips detection and takes the
whole clip as the command. The wake word models are voice-dependent: the same
phrase in macOS's default voice peaked at 0.05, and in Samantha and Daniel at
0.99 and 1.00.

## Configuration

| Variable | Default | |
|---|---|---|
| `NODES_DATA_DIR` | `/data` | `nodes.json`, `rules.json`, `firmware/` and `models/`. Mount a volume: losing it un-adopts every node. |
| `NODES_TTS_URL` | unset | tts-stack's base URL, for `say` and every reply. Unset, `say` answers 503 and names this variable. |
| `NODES_TTS_VOICE` | `bm_george` | |
| `NODES_STT_URL` | unset | stt-stack's base URL. Unset, wake words are still heard and published, and routing says there is no STT. |
| `NODES_WAKE_WORDS` | `hey_jarvis:0.5` | Names and thresholds, comma-separated. A name alone gets 0.5. Empty means none; push-to-talk still works. |
| `NODES_MODEL_DIR` | `$NODES_DATA_DIR/models` | Where wake word models live. The image's own are copied here at start; other built-in names (`alexa`, `hey_mycroft`, `hey_rhasspy`, `weather`) are fetched here once, and `<name>.onnx` of your own loads by its name. |
| `NODES_FRONTEND` | `1` | `0` skips echo cancellation, beamforming and noise suppression. |
| `NODES_MQTT_URL` | unset | `mqtt://user:pass@host:1883` or `mqtts://...` (the system CA store, or `SSL_CERT_FILE`). Unset, no MQTT. |
| `NODES_MQTT_PREFIX` | `homeassistant` | Home Assistant's discovery prefix |
| `NODES_MQTT_BASE` | `calliope/nodes` | The hub's own topics; two hubs on one broker need two |
| `NODES_HA_TOKEN` | unset | The default `token_env` of an `ha_conversation` rule: a long-lived access token |
| `NODES_LLM_API_KEY` | unset | The default `api_key_env` of an `llm` rule. Unset, no `Authorization` is sent. |
| `NODES_FIRMWARE_PUBKEY` | unset | A PEM public key, or a path to one. Set, uploads must be signed by it. A bad value stops the service at start. |
| `NODES_API_KEYS` | unset | As on the other backends. Behind the gateway it stays unset. |
| `NODES_LOG_LEVEL` | `INFO` | Transcripts and replies are logged only at `DEBUG`. |

`ORT_DISABLE_TELEMETRY=1` is set in the image and by `app/wakeword.py`. ONNX
Runtime's Linux wheel reports usage to Microsoft and writes a device id without
it; measured in this image, two connections to its collector within 15 s of a
session, and none with it set. See THIRD-PARTY-NOTICES.md.

## Install and tests

```bash
# from the repository root
pip install -r services/nodes/requirements-dev.txt
pip install --no-deps -r services/nodes/requirements-nodeps.txt
cd services/nodes && python -m pytest -q
```

The second line is openwakeword on its own: its metadata still asks for
tflite-runtime, which has no wheel for Python 3.13, so it goes in without
dependencies and what it imports is pinned in `requirements.txt`.

The device is played by Starlette's test socket, and STT, TTS, webhooks and
Home Assistant by `httpx.MockTransport` on `*.test` hosts, which never resolve.
Nothing starts a server and no board is needed. Most pipeline tests stand a fake
in for the wake word model; the tests that need the real ones fetch them from
openWakeWord's GitHub release once per run (5.4 MB, or `NODES_TEST_WAKEWORD_DIR`
to keep them) and skip with the reason when offline.

## What is not here

- Anything measured on the board: the front-end, the wake word thresholds, the
  ring's orientation, earcon load times and ECDSA verification time on the
  ESP32 are all unmeasured there.
- Barge-in during the command itself. The beam keeps the steering it had before
  playback started, and a wake word is not listened for while a conversation is
  still listening or routing.
- Discovery. A node is told its hub in the setup portal, or moved with
  `set-hub`.
- Speaker identification, and a television or a second talker is speech to the
  front-end: it tells speech from noise by how steady it is.

## Licence

BSD 2-Clause. See [LICENSE](LICENSE). The wake word models in the image are
CC BY-NC-SA 4.0 (non-commercial) and carry their own notice; see
[THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
