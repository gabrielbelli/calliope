# voice-satellites

The hub for thin audio devices. A satellite is a microphone array, a speaker
and a ring of lights on Wi-Fi, and it makes no decisions. It streams its
microphones here and plays, lights and reports whatever it is told. The hub
listens on each satellite for the wake words assigned to it, sends what follows
to an assistant, and speaks the answer.

```text
satellite ──wss /satellites/ws──▶ voice-gateway :30080 ──ws──▶ voice-satellites :8003
                                  (TLS, the one door)          adoption, config, audio, OTA,
                                                               wake words, routing
browser ──/ui/api/satellites──▶ voice-ui ──▶ voice-gateway ──▶ voice-satellites
voice-satellites ──▶ stt-stack, tts-stack, and whatever a rule names (Home Assistant, an LLM, a webhook)
```

Sibling of `stt`, `tts`, `tts-long`, `gateway` and `ui`. The first satellite is
the ESP32-Korvo v1.1:
[`clients/korvo-satellite`](../../clients/korvo-satellite/README.md). The
decision to bring satellites in through the gateway, and to keep their socket
out of `GATEWAY_API_KEYS`, is
[ADR 0013](../../docs/adr/0013-satellites-one-door.md).

## Status

Measured on orko (25 Sep 2026) with the first board, an ESP32-Korvo, adopted
over `wss://orko.gabrielbelli.com:30080`:

- **Working on the board:** adoption (keeping the satellite's own settings),
  config, lights off, a four-channel stream with no dropped frames, and live
  listening: front-end plus wake word at a real-time factor of 0.09 on orko's
  Xeon, 24 % of one core, 215 MiB.
- **Signed updates, on the board:** a signed 1.17 MB release image installed in
  20 s, including through the Satellites tab's route. The hub will not send an
  unsigned image to a satellite that advertises a key. With the hub bypassed,
  an image signed by another key was refused by the satellite itself
  (`bad signature`), and the satellite stayed on its image.
- **The listening path on orko, from recorded clips (`/inject`):** "hey jarvis,
  what time is it" is detected (score 0.995), transcribed by Parakeet as "What
  time is it?", routed by the default echo rule, and answered by Kokoro
  (1.3 s). "The weather is fine today" is ignored.
- **Not yet heard live:** a wake word spoken in the room. Earcons and ducking
  have not played on the board yet; its speaker is off.

## Adoption

A satellite that has never been adopted says `hello` with no token and is
answered `pending`. It stays connected and shows up in `GET /satellites` and on
the Satellites tab. From a pending satellite the hub accepts no microphone
audio, and it sends that satellite nothing but `pending`.

`POST /satellites/{id}/adopt` issues a random token. The satellite stores it
and says `hello` again with it; the hub answers `welcome` with the satellite's
name and config. The hub keeps only the token's SHA-256, so a copy of
`satellites.json` cannot impersonate a satellite.
`POST /satellites/{id}/forget` drops the record and tells the satellite, which
goes back to pending. Whatever the hub was playing to it stops first, and a
duck it held is lifted.

**Adoption takes the satellite's own settings** (volume, mic gain, and the
microphone, speaker and lights switches), because the satellite is the thing
in the room: a bedroom satellite that was dark stays dark. Current firmware
says them in its `hello`. Firmware from before 2026-09-25 says them only in its
`status`, every 10 s, so a satellite adopted in its first seconds has said
nothing yet. The hub does not guess: the welcome leaves those settings out, so
the satellite keeps what it has, and until the satellite reports them the
record holds the switches as off, so nothing lights or plays on a value the hub
made up. The firmware reports as soon as it applies the welcome. A setting
changed with `PATCH` in the meantime is the hub's, and the report does not undo
it. Only these settings are taken, and only within the ranges `PATCH` accepts;
`buttons` is never the satellite's to report.

A satellite's id is its MAC, which is no secret. While an adopted satellite is
connected, a `hello` with its id and without its token is closed with 1008,
so another connection cannot push it offline and report as it. A board that
reconnects with its token replaces its own stale socket as before.

A satellite is addressed by its id (the MAC, with or without colons) or its
name.

## Routes

| Route | What it does |
|---|---|
| `WS /satellites/ws` | The device connection. Protocol below. |
| `WS /nodes/ws` | The same handler, under the feature's name until 2026-09-25. A board in the field runs firmware that connects here, and its next firmware arrives over this socket, so the old path stays until no board reports firmware from before the rename ([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)). The gateway relays both. |
| `GET /satellites` | Every satellite seen since the hub started, adopted or not, with its listening state, earcons and `wake_words` (the names assigned to it) |
| `GET /satellites/events` | Server-sent events: buttons, wake words, routing, status, updates, satellites coming and going, a wake word's model becoming ready |
| `GET /satellites/wake-words` | `{"available", "words", "load_error"}`: the names the hub can load, and each word with its `threshold`, `satellites`, `state` and `error`. [Wake words](#wake-words). |
| `PUT /satellites/wake-words` | `{"words": [{"name", "threshold", "satellites"}]}`: replace them all, live. A bad set is a 422 and the old one stays. |
| `GET /satellites/{id}` | One satellite |
| `PATCH /satellites/{id}` | `name`, `volume` (0-100), `mic_gain_db` (0-37.5), `mic_enabled`, `speaker_enabled`, `local_volume_buttons`, `lights_enabled`, `buttons` |
| `POST /satellites/{id}/adopt` | `{"name": "..."}` |
| `POST /satellites/{id}/forget` | |
| `POST /satellites/{id}/identify` | Blink for five seconds. Works before adoption, which is the point. |
| `POST /satellites/{id}/reboot` | |
| `POST /satellites/{id}/lights` | `{"mode": "off|solid|pulse|spin|pixels", "color": [r,g,b], "brightness": 0-255, "pixels": [[r,g,b], ...]}`. 409 for a satellite with `lights_enabled` false. |
| `POST /satellites/{id}/tone` | `{"frequency": 440, "seconds": 1}` on the satellite's speaker. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/say` | `{"text": "...", "voice": "bm_george"}`: Kokoro, via `SATELLITES_TTS_URL`. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/flush` | Stop: drop the speaker audio queued and playing, and cancel the conversation in progress |
| `GET /satellites/{id}/listen?seconds=5&channel=` | A WAV of the raw mic channels, up to 60 s |
| `POST /satellites/{id}/set-hub` | `{"url": "wss://host:port"}`: the satellite saves it and reboots onto that hub |
| `POST /satellites/{id}/inject?play=0&wake_word=` | A 16 kHz mono 16-bit WAV as the body, through the satellite's own wake words, the endpoint and routing path as if the satellite had heard it. [Verifying](#verifying-the-pipeline-without-a-voice). |
| `GET /satellites/routing` | The rules, which secret variables are set (never their values), the STT and TTS URLs, warnings |
| `PUT /satellites/routing` | Replace the whole ruleset. A bad one is a 422 and the old rules stay. |
| `POST /satellites/routing/test` | `{"satellite", "wake_word", "text"}`: a typed sentence through the rules and TTS. Plays nothing. |
| `GET /satellites/firmware` | Uploaded images |
| `POST /satellites/firmware?model=&version=&signature=` | The `.bin` as the raw body. It must start with the ESP32 image magic (0xE9) and fit a 4 MB slot. `signature` is base64 or base64url DER ECDSA. |
| `DELETE /satellites/firmware/{sha256}` | |
| `POST /satellites/ota` | `{"satellite": "<id>|<name>|all", "sha256": "..."}`. Images are only sent to adopted, online satellites of the image's model, and not to a satellite that would refuse the signature. |

The gateway routes all of these. The Satellites tab uses all but two: `GET
/satellites/{id}`, because the list already carries every satellite, and
`inject`, which is for scripts: a button that runs a clip through a
satellite's real rules would be one press from Home Assistant acting on it.

## The device protocol

One WebSocket. Control travels as JSON text frames; audio and firmware travel
as binary frames whose first byte names the kind.

**Binary frames**

| Kind | Direction | Layout |
|---|---|---|
| `1` mic | satellite → hub | 16-byte header (`kind, 0, channels, 0, seq u32, capture time µs u64`, little-endian), then interleaved s16le |
| `2` speaker | hub → satellite | same header, then mono s16le at the satellite's speaker rate |
| `3` firmware | hub → satellite | `kind, 0, 0, 0, offset u32`, then up to 8 KB of image |
| `4` earcon | hub → satellite | `kind, 0, 0, 0, offset u32`, then up to 8 KB of s16le at 48 kHz |

The Korvo sends 4 channels at 16 kHz in 20 ms frames: the speaker loopback
first, then the three microphones. It plays 48 kHz mono. The hub paces speaker
audio at real time plus a 300 ms lead.

**Text frames, satellite → hub:** `hello` (id, model, firmware, token, caps,
and on firmware from 2026-09-25 its settings: `volume`, `mic_gain_db`,
`mic_enabled`, `speaker_enabled`, `lights_enabled`), `status` (every 10 s and
after every `welcome` or `config`: RSSI, heap, mute, the same settings, drop
counters, `duck`, `earcons_ready`), `button` (`press` or `release`, with `held_ms`), `ota`
(`started`, `progress`, `rebooting`, `failed`, `verified`; a signed build fails
with `unsigned image` or `bad signature`), `ota_next` (`offset`), and for
earcons `earcons` (`ready`, `items`, `last_load_us`), `earcon_next` (`id`,
`offset`), `earcon_stored` (`id`, `size`, `sha256`) and `earcon_failed` (`op`
put, play or delete, `id`, `error`).

`hello.caps` names what the satellite can do: `mic`, `speaker`, `lights` (the
LED count), `buttons`, and on current firmware `earcons` (`max`, `max_bytes`,
`rate`), `duck: true`, and on a signed build `ota_key`. The hub sends earcon,
duck and signature messages only to a satellite whose caps say it takes them;
older firmware ignores them without a word.

**Text frames, hub → satellite:** `pending`, `adopt`, `welcome` (name and
config), `config` (any subset), `lights`, `identify`, `reboot`, `forget`,
`set_hub`, `flush`, `ota` (size, sha256, version, and `signature` when the
image has one), `earcon_put` (`id`, `size`, `sha256`), `earcon` (`id`: play
it), `earcon_list`, `earcon_delete`, `duck` (`level` 0-100 on the volume scale,
`ms`, 0 for until `unduck`) and `unduck`.

**Updates.** The hub sends `ota`. The satellite begins writing to its spare
slot and asks for chunks with `ota_next`, one at a time, hashing as it goes. On
a match it reboots into the new image, which the bootloader holds as
pending-verify. The new image marks itself valid only after it hears from the
hub again. A crash, a hang, or 180 s without the hub rolls it back to the image
it had.

**Earcons** are short sounds the satellite keeps in flash: `wake` (a rising
two-tone, 150 ms), `done` (the same falling, 150 ms) and `error` (two low
pulses, 210 ms), synthesised in `app/earcons.py` and peaking at -13 to -12
dBFS. After a `welcome` the hub asks for `earcon_list` and uploads whatever the
satellite lacks or holds with other content, one at a time, as for firmware. A
satellite whose storage is still being formatted answers `ready: false`, and
the hub asks again every 5 s for up to two minutes. It plays an earcon only
when the satellite holds it.

## Listening

Every adopted satellite with its microphone enabled and not muted is listened
to, for the wake words assigned to it:

```text
mic frames ─▶ bounded queue ─▶ FrontEnd ─▶ WakeWords ─▶ Endpointer ─▶ router ─▶ reply
 (socket)     (1 s, drops      echo cancel,  hey_jarvis   the command   STT, rule,  on the satellite
               the oldest)     beam, denoise              after it      TTS         the rule names
```

The socket loop only queues frames. One task per satellite drains the queue in
batches and runs the signal work (`app/listening.py`) in a thread pool, so the
event loop never does it. A full queue drops its oldest frame and counts it as
`mic_dropped` in `GET /satellites`.

1. **Front-end** (`app/frontend.py`): echo cancellation against the satellite's
   own loopback channel, MVDR beamforming across the three microphones, and
   noise suppression, on 16 ms blocks, 16 ms behind the input.
   `SATELLITES_FRONTEND=0` skips it and uses the first microphone as sent.
2. **Wake words** (`app/wakeword.py`): openWakeWord's ONNX models, loaded
   once. Each satellite gets a stream of its own that runs only its own words,
   on sessions shared by all of them.
3. **Endpointer:** webrtcvad. The command ends after 800 ms without speech, at
   10 s, or at 4 s if no speech started.
4. **A conversation** per wake word, one per satellite at a time: the `wake`
   earcon if the satellite holds it, the ring pointed at the talker, the
   satellite ducked (and the satellite the rule answers on, if that is another
   one), then the router. The reply is played on the rule's `reply_to`
   satellite, over nothing: audio still playing there is dropped and the duck
   lifted first, because the firmware ducks the hub's audio and the reply is
   the hub's audio. An error plays `error`; a success with nothing to say plays
   `done`.

A wake word heard while a reply is still playing starts a new conversation. The
satellite ducks the old reply while the new command is heard, and drops it when
the new answer arrives.

**Events.** Each conversation publishes `{"type": "wake", "satellite",
"wake_word", "score", "direction"}` and then `{"type": "routed", "satellite",
"wake_word", "rule_id", "reply_to", "error", "transcript", "reply_text",
"timings_ms", "endpoint", "command_s", "played", "note"}` on
`/satellites/events`. Transcripts are in the event stream, which is behind the
same keys as the rest of the API, and not in the INFO log.

**Measured** on darwin (Apple silicon) and in the image on arm64, 2026-09-25:

| What | Result |
|---|---|
| The two "hey jarvis" fixtures on three simulated microphones, white noise -60 to -27 dBFS, through the front-end | detected every time; the command ended on silence 0.4 to 0.7 s after the speech |
| The same with the raw first microphone at -27 dBFS | detected, and the endpointer never ended: webrtcvad takes that much noise for speech |
| Hub resident memory in the image, satellites streaming 4 channels in real time | 228 MiB idle with hey_jarvis loaded, 239 MiB with one satellite, 289 MiB with three, 326 MiB with six |
| The same run | every wake word heard, 0 microphone frames dropped at six satellites |
| Front-end real-time factor in the image | 0.03 to 0.05 per satellite |
| Wake word memory per extra satellite, two wake words loaded | 3.6 MB with shared sessions, against 55 MB for a second instance |

Not measured: anything on the board. The Korvo's microphone spacing (65 mm) and
orientation are assumed, so `direction` is relative to the board and may be
rotated or mirrored, and wake word thresholds are untuned for its microphones.

### Wake words

`wake_words.json` in `SATELLITES_DATA_DIR` says which wake words the hub
listens for, and on which satellites:

```json
{"words": [
  {"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"]},
  {"name": "alexa", "threshold": 0.6, "satellites": ["94b97e7b8be8"]}
]}
```

| Field | |
|---|---|
| `name` | One of `available` in `GET /satellites/wake-words`: a built-in openWakeWord model (`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `weather`), or `<name>.onnx` of your own in `SATELLITES_MODEL_DIR`. Each name once. `ptt` is push-to-talk and never a wake word. |
| `threshold` | 0.1 to 0.95; higher is stricter. Unset, 0.5. |
| `satellites` | `["*"]` for every satellite, including one adopted later, or satellite ids. Empty means nobody hears the word. |

A satellite listens only for the words assigned to it, and `GET /satellites`
lists them as its `wake_words`. Its stream runs only those models, so a word
for the kitchen costs the bedroom nothing. A satellite with no word still
streams and still takes push-to-talk. Routing rules stay keyed by wake word,
so a word moved to another room takes its rules with it.

`PUT /satellites/wake-words` replaces the whole set, and the change is live at
once, with no restart:

- **A threshold** is changed in place on every detector. Nothing is rebuilt.
- **A satellite whose words changed** gets a new detector before its next batch
  of audio. A new detector hears nothing for its first 1.2 s (openWakeWord's
  warm-up, see `app/wakeword.py`). The other satellites are not touched.
- **A word taken off a satellite** is not acted on there from the moment the
  PUT answers, even in audio that was already being processed.
- **A name not loaded yet** is `downloading`: a built-in name is fetched from
  openWakeWord's GitHub release into `SATELLITES_MODEL_DIR` in the background
  and checked against a pinned SHA-256. Then every detector is rebuilt with it,
  the word is `ready`, and a `{"type": "wake_words", "words": [...]}` event is
  published. A fetch that fails is `error`, with the reason. The other words
  keep working, and the next PUT tries again.

A PUT is refused with 422 `invalid_wake_words`, and the file is not changed,
for a name the hub cannot load, a name given twice, a threshold outside 0.1 to
0.95, `"*"` together with ids, and an id the hub does not know. A known id is
one adopted, seen since the hub started, or already in the file, so the list
`GET` returned can always be sent back. A MAC with colons is taken as the id.

Forgetting a satellite takes it out of every word that names it. Adopted
again, it hears the `"*"` words until it is given more.

**`SATELLITES_WAKE_WORDS` only seeds the file.** On the first start with a
volume that has no `wake_words.json`, each word it names is written with
`["*"]`, which is what the variable meant before words could be assigned. A
threshold outside 0.1 to 0.95 is moved inside it, with a warning in the log.
After that the file decides, and changing the variable does nothing: delete
the file to seed again. A file that does not load turns wake words off rather
than back to the seed, which could bring back a word someone removed.
`GET /satellites/wake-words` (`load_error`) and `/health` say why, and the
next PUT replaces the file.

### Lights

While a satellite listens, its ring points at the talker (`pixels`, full on the
nearest LED, soft on its neighbours), or pulses when there is no direction yet.
It spins while the router works and goes out at the end. LED 0 is assumed to
sit towards microphone 1, which has not been checked on a board.

**A satellite with `lights_enabled` false is never sent `lights`.** Every
lights message goes through one function (`Hub.send_lights`) that reads the
setting at the moment of sending, and `POST /satellites/{id}/lights` answers
409 rather than go around it. A ring left lit when lights were turned off
mid-conversation is put out when they are turned back on.
`tests/test_pipeline.py` runs a whole wake, route and reply cycle on a dark
satellite and checks that it received no `lights`.

**A satellite with `speaker_enabled` false is sent nothing audible.** Earcons
and replies read the setting when they are sent, `POST /satellites/{id}/tone`
and `/say` answer 409 (`speaker_disabled`), and turning the speaker off drops
the audio still queued or playing and tells the satellite to flush. The
speaker loop reads the setting again before every 20 ms frame, so a reply
handed over at the moment the speaker is turned off, or a tone playing when a
satellite is forgotten, stops there. The firmware keeps its amplifier off as
well, but the hub does not rely on it.

Both settings, and whether the satellite is adopted, are read inside the
socket's lock, at the moment of sending. A `lights` message or an earcon that
waited for the lock behind a speaker frame is not sent if the setting changed
while it waited.

### Buttons

`buttons` in a satellite's config maps a button and an edge to an action:

```json
{"play": {"press": "ptt"}, "set": {"press": "stop"}, "mode": {"release": "webhook:http://nodered:1880/korvo"}}
```

| Action | What happens |
|---|---|
| `ptt` | Push-to-talk: listen as if the wake word `ptt` had been heard |
| `stop` | What `POST /satellites/{id}/flush` does |
| `webhook:<url>` | POST `{"satellite", "satellite_id", "button", "action", "held_ms"}` to the URL, 10 s at most, no redirects followed |
| `none` | Nothing |

The default is the one above without the webhook. A PATCH replaces the whole
mapping. `rec` cannot be mapped: the firmware mutes on it before the hub hears
of the press. Every press is still published as an event, whatever it maps to.
The mapping stays on the hub and is never sent to the satellite.

## Routing

`rules.json` in `SATELLITES_DATA_DIR` decides what happens after a wake word.
The first rule, in file order, whose `wake_word` and `satellites` both match
wins.

```json
{"version": 1, "rules": [
  {"id": "kitchen-ha", "wake_word": "hey_jarvis", "satellites": ["kitchen"],
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
| `satellites` | Ids or names; empty means every satellite. A rule written before the rename says `nodes`, which is read as this. |
| `destination` | `ha_conversation` (`url`, `token_env`, `agent_id`), `llm` (`base_url`, `model`, `system`, `api_key_env`, `max_tokens`), `webhook` (`url`, `token_env`), or `echo` |
| `reply_to` | `same`, `none` (act and say nothing), or another satellite's id or name |
| `language` | BCP 47, as `pt-BR`. STT gets the first part, Home Assistant the whole. |
| `voice` | A Kokoro voice for this rule; unset, `SATELLITES_TTS_VOICE` |

Without a file, one rule echoes anything heard back to the satellite that heard
it, which proves microphone, STT, TTS and speaker before anything else exists.
A file that does not load turns routing off rather than falling back to echo,
and `GET /satellites/routing` says why.

**A rule never holds a secret.** It names an environment variable (`token_env`,
`api_key_env`), unknown fields are refused, a variable name must look like one
(so a pasted token is a 422), and a URL with a user and password is refused.
Destination URLs are not filtered for private addresses on purpose: Home
Assistant on the LAN is the main target, and whoever can PUT a rule can already
reflash every satellite. Redirects are not followed. Every external call has a
hard time limit: STT 30 s, TTS 30 s, the destination its own `timeout`.

## Home Assistant over MQTT

With `SATELLITES_MQTT_URL` set, every adopted satellite is one Home Assistant
device, by discovery: Wi-Fi signal, online, microphone, speaker and lights
switches, volume, last wake word, a wake word event and one event per button. A
switch goes through the same code as `PATCH /satellites/{id}`, and nothing but
those four settings can be changed from the broker. Button presses and wake
words are dropped while the broker is away rather than delivered late; state,
discovery and availability are retained and sent again on every reconnect.
Forgetting a satellite removes its device. Injected test clips are not
published. Checked against Home Assistant 2026.8.1's own MQTT integration: 14
entities under one device.

## Signed firmware

A satellite built with a public key (`clients/korvo-satellite/keys`) installs
an update only with an ECDSA P-256 signature over the image by the matching
private key, made on the developer's machine. The hub only carries it: in with
the upload's `signature`, out in the `ota` message. With
`SATELLITES_FIRMWARE_PUBKEY` set, the hub also refuses an unsigned or wrongly
signed upload with 400 `bad_signature`, and `POST /satellites/ota` skips a
satellite whose `caps.ota_key` the image would not satisfy, saying why, rather
than sending 15 s of image to be refused. The running unsigned firmware ignores
`signature`, so the first signed build installs over the air as usual; after
that the satellite requires one.

## Verifying the pipeline without a voice

`POST /satellites/{id}/inject` runs a recorded clip through the same path a
live satellite's audio takes, from the wake word on, and answers with what
happened. Without `?play=1` nothing at all is sent to any satellite: no sound,
no light, no duck. Its events are marked `"injected": true` and are not sent to
MQTT.

```bash
say -v Samantha -o /tmp/q.aiff "hey jarvis, what time is it"   # writes a file, plays nothing
ffmpeg -loglevel error -i /tmp/q.aiff -ar 16000 -ac 1 -c:a pcm_s16le /tmp/q.wav
curl -sS -H "Authorization: Bearer $KEY" --data-binary @/tmp/q.wav \
  https://orko.gabrielbelli.com:30080/satellites/kitchen/inject | jq
```

The answer carries `heard` (wake word, score, position), `command` (why it
ended, its length), `outcome` (rule, transcript, reply text, errors, timings,
reply audio length) and `played`. Detection listens for the satellite's own
wake words, as it would live; a satellite with none assigned answers 409
`no_wake_words`. `?wake_word=ptt` skips detection and takes the whole clip as
the command. The wake word models are voice-dependent: the same
phrase in macOS's default voice peaked at 0.05, and in Samantha and Daniel at
0.99 and 1.00.

## Configuration

| Variable | Default | |
|---|---|---|
| `SATELLITES_DATA_DIR` | `/data` | `satellites.json`, `rules.json`, `wake_words.json`, `firmware/` and `models/`. Mount a volume: losing it un-adopts every satellite. A volume from before the rename holds `nodes.json` instead; the hub reads it once and writes `satellites.json`, and leaves the old file where it is. |
| `SATELLITES_TTS_URL` | unset | tts-stack's base URL, for `say` and every reply. Unset, `say` answers 503 and names this variable. |
| `SATELLITES_TTS_VOICE` | `bm_george` | |
| `SATELLITES_STT_URL` | unset | stt-stack's base URL. Unset, wake words are still heard and published, and routing says there is no STT. |
| `SATELLITES_WAKE_WORDS` | `hey_jarvis:0.5` | Read once: the words `wake_words.json` starts with, each for every satellite, on the first start with a volume that has none. Names and thresholds, comma-separated. A name alone gets 0.5. Empty means none; push-to-talk still works. After that, [Wake words](#wake-words). |
| `SATELLITES_MODEL_DIR` | `$SATELLITES_DATA_DIR/models` | Where wake word models live. The image's own are copied here at start; other built-in names (`alexa`, `hey_mycroft`, `hey_rhasspy`, `weather`) are fetched here once, when a word first names them, and `<name>.onnx` of your own loads by its name. |
| `SATELLITES_FRONTEND` | `1` | `0` skips echo cancellation, beamforming and noise suppression. |
| `SATELLITES_DEBUG_AUDIO` | `0` | `1` keeps the last ten commands' surroundings under `$SATELLITES_DATA_DIR/debug`: 16 s of processed output, the first raw microphone, and the command, as WAV. It records the room; switch it on to find out why a command came back empty, then off. |
| `SATELLITES_MQTT_URL` | unset | `mqtt://user:pass@host:1883` or `mqtts://...` (the system CA store, or `SSL_CERT_FILE`). Unset, no MQTT. |
| `SATELLITES_MQTT_PREFIX` | `homeassistant` | Home Assistant's discovery prefix |
| `SATELLITES_MQTT_BASE` | `calliope/satellites` | The hub's own topics; two hubs on one broker need two |
| `SATELLITES_HA_TOKEN` | unset | The default `token_env` of an `ha_conversation` rule: a long-lived access token |
| `SATELLITES_LLM_API_KEY` | unset | The default `api_key_env` of an `llm` rule. Unset, no `Authorization` is sent. |
| `SATELLITES_FIRMWARE_PUBKEY` | unset | A PEM public key, or a path to one. Set, uploads must be signed by it. A bad value stops the service at start. |
| `SATELLITES_API_KEYS` | unset | As on the other backends. Behind the gateway it stays unset. |
| `SATELLITES_LOG_LEVEL` | `INFO` | Transcripts and replies are logged only at `DEBUG`. |

Before 2026-09-25 every one of these was `NODES_*`. The old names are not
read. At start the hub logs a warning for each `NODES_*` variable still set,
with the name it reads now, except one that a routing rule names: a rule saved
before the rename says `"token_env": "NODES_HA_TOKEN"` and reads exactly that
([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)).

`ORT_DISABLE_TELEMETRY=1` is set in the image and by `app/wakeword.py`. ONNX
Runtime's Linux wheel reports usage to Microsoft and writes a device id without
it; measured in this image, two connections to its collector within 15 s of a
session, and none with it set. See THIRD-PARTY-NOTICES.md.

## Install and tests

```bash
# from the repository root
pip install -r services/satellites/requirements-dev.txt
pip install --no-deps -r services/satellites/requirements-nodeps.txt
cd services/satellites && python -m pytest -q
```

The second line is openwakeword on its own: its metadata still asks for
tflite-runtime, which has no wheel for Python 3.13, so it goes in without
dependencies and what it imports is pinned in `requirements.txt`.

The device is played by Starlette's test socket, and STT, TTS, webhooks and
Home Assistant by `httpx.MockTransport` on `*.test` hosts, which never resolve.
Nothing starts a server and no board is needed. Most pipeline tests stand a
fake in for the wake word model; the tests that need the real ones fetch them
from openWakeWord's GitHub release once per run (5.4 MB, or
`SATELLITES_TEST_WAKEWORD_DIR` to keep them) and skip with the reason when
offline.

## What is not here

- Anything measured on the board: the front-end, the wake word thresholds, the
  ring's orientation, earcon load times and ECDSA verification time on the
  ESP32 are all unmeasured there.
- Barge-in during the command itself. The beam keeps the steering it had before
  playback started, and a wake word is not listened for while a conversation is
  still listening or routing.
- Discovery. A satellite is told its hub in the setup portal, or moved with
  `set-hub`.
- Speaker identification, and a television or a second talker is speech to the
  front-end: it tells speech from noise by how steady it is.

## Licence

BSD 2-Clause. See [LICENSE](LICENSE). The wake word models in the image are
CC BY-NC-SA 4.0 (non-commercial) and carry their own notice; see
[THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
