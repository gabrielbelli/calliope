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
voice-satellites ──▶ stt-stack, tts-stack, and whatever a wake word's action names (Home Assistant, an LLM, a webhook)
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
  time is it?", routed by the default echo action, and answered by Kokoro
  (1.3 s). "The weather is fine today" is ignored.
- **Not yet heard live:** a wake word spoken in the room. Earcons and ducking
  have not played on the board yet; its speaker is off.
- **Built and tested against fakes only (25 Sep 2026):** conversation and
  trigger words, follow-ups, barge-in, streamed replies and the language of
  each utterance. None of it has run on the board or against orko's services.

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
| `GET /satellites/events` | Server-sent events: buttons, wake words, routing, conversations and their turns, triggers, status, updates, satellites coming and going, a wake word's model becoming ready, a volume set with the satellite's own buttons (`volume`), speaker or headphones (`output`) |
| `GET /satellites/wake-words` | `{"available", "words", "ptt", "custom", "env", "warnings", "load_error"}`: the names the hub can load, each word's whole entry with its `state` and `error`, push-to-talk's entry, and which secret variables the actions name are set. [Wake words](#wake-words). |
| `PUT /satellites/wake-words` | `{"words": [...], "ptt": {...}}`: replace them all, live. A field an entry leaves out keeps its saved value. A bad set is a 422 and the old one stays. |
| `POST /satellites/wake-words/models?name=` | A custom wake word: the `.onnx` as the raw body, checked to be an openWakeWord classifier (input `[batch, 16, 96]`, under 5 MB) before it is written. Then offered in `available` and assigned like a built-in. |
| `DELETE /satellites/wake-words/models/{name}` | Only a custom model, and only once no wake word uses it (409 otherwise). |
| `GET /satellites/{id}` | One satellite, with `latency`: how quickly its last 20 replies began and ended, and `output`: `speaker`, `headphones` or `null` ([Speaker or headphones](#speaker-or-headphones)) |
| `PATCH /satellites/{id}` | `name`, `volume` (0-100), `mic_gain_db` (0-37.5), `mic_enabled`, `speaker_enabled`, `local_volume_buttons`, `lights_enabled`, `buttons` |
| `POST /satellites/{id}/adopt` | `{"name": "..."}` |
| `POST /satellites/{id}/forget` | |
| `POST /satellites/{id}/identify` | Blink for five seconds. Works before adoption, which is the point. |
| `POST /satellites/{id}/reboot` | |
| `POST /satellites/{id}/lights` | `{"mode": "off|solid|pulse|spin|pixels", "color": [r,g,b], "brightness": 0-255, "pixels": [[r,g,b], ...]}`. 409 for a satellite with `lights_enabled` false. |
| `POST /satellites/{id}/tone` | `{"frequency": 440, "seconds": 1}` on the satellite's speaker. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/say` | `{"text": "...", "voice": "bm_george"}`: Kokoro, via `SATELLITES_TTS_URL`. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/flush` | Stop: drop the speaker audio queued and playing, and cancel the conversation in progress |
| `POST /satellites/{id}/ptt` | `{"wake_word": "..."}`, optional: listen as if the satellite's push-to-talk button had been pressed, handled by that word's entry or by `ptt`. 204, or 409 naming why not (`satellite_busy`, `satellite_muted`, `mic_disabled`, `trigger_word`). For Home Assistant. |
| `GET /satellites/{id}/listen?seconds=5&channel=` | A WAV of the raw mic channels, up to 60 s |
| `POST /satellites/{id}/set-hub` | `{"url": "wss://host:port"}`: the satellite saves it and reboots onto that hub |
| `POST /satellites/{id}/inject?play=0&wake_word=` | A 16 kHz mono 16-bit WAV as the body, through the satellite's own wake words, the endpoint and routing path as if the satellite had heard it. [Verifying](#verifying-the-pipeline-without-a-voice). |
| `GET /satellites/routing` | What each wake word does, which secret variables are set (never their values), the STT and TTS URLs and engine, warnings |
| `PUT /satellites/routing` | 409 `routing_per_wake_word`: routing is saved with each wake word. [Routing](#routing). |
| `POST /satellites/routing/test` | `{"satellite", "wake_word", "text"}`: a typed sentence through that word's action and TTS. Plays nothing. |
| `GET /satellites/firmware` | Uploaded images |
| `POST /satellites/firmware?model=&version=&signature=` | The `.bin` as the raw body. It must start with the ESP32 image magic (0xE9) and fit a 4 MB slot. `signature` is base64 or base64url DER ECDSA. |
| `DELETE /satellites/firmware/{sha256}` | |
| `POST /satellites/ota` | `{"satellite": "<id>|<name>|all", "sha256": "..."}`. Images are only sent to adopted, online satellites of the image's model, and not to a satellite that would refuse the signature. |

The gateway routes all of these. The Satellites tab uses all but five: `GET
/satellites/{id}`, because the list already carries every satellite; `GET` and
`PUT /satellites/routing`, because routing now lives on each wake word and the
tab edits it there; `ptt`, which is Home Assistant's; and `inject`, which is
for scripts: a button that runs a clip through a satellite's real actions
would be one press from Home Assistant acting on it.

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
 (socket)     (1 s, drops      echo cancel,  hey_jarvis   the command   STT, the    on the satellite
               the oldest)     beam, denoise              after it      word's      the action names
                                                                        action, TTS
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
4. **What the word does**, one conversation per satellite at a time: the
   `wake` earcon if the satellite holds it, the ring pointed at the talker,
   the satellite ducked (and the satellite the word answers on, if that is
   another one), then STT, the word's action and the reply, streamed
   sentence by sentence. The reply is played on the action's `reply_to`
   satellite, over nothing: audio still playing there is dropped and the duck
   lifted first, because the firmware ducks the hub's audio and the reply is
   the hub's audio. An error plays `error`; a success with nothing to say
   plays `done`. A conversation then listens for its next turn; a trigger word
   skips all of this. [Command, conversation and trigger](#command-conversation-and-trigger).

A wake word heard while a reply is playing interrupts it, as speech over the
reply does ([Barge-in](#barge-in)). A wake word heard while a conversation is
still listening for its command, or routing it, is dropped.

**Events.** Each conversation publishes `{"type": "wake", "satellite",
"wake_word", "score", "direction"}`. A command then publishes `{"type":
"routed", "satellite", "wake_word", "rule_id", "mode", "reply_to", "error",
"transcript", "language", "language_source", "reply_language", "voice",
"reply_text", "spoken_text", "interrupted", "timings_ms", "timeline_ms",
"endpoint", "command_s", "played", "note"}`, where `rule_id` names the wake
word that answered. A conversation publishes `conversation_started`
(`rule_id`, `reason`: `wake_word` or `fallback`, `from_rule`,
`follow_up_s`), a `turn` for each exchange (the fields of `routed`, and
`turn`, `ended`, `handed_over_to`), and `conversation_ended` (`rule_id`,
`turns`, `seconds`, `reason`: `silence`, `phrase`, `error`, `no_audio`,
`stop`, `muted`, `mic_off`, `wake_word`, `trigger`, `unadopted`). A trigger
word publishes only `triggered`. Transcripts are in the event stream, which
is behind the same keys as the rest of the API, and not in the INFO log.

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

A wake word is the unit of configuration. Each entry in `wake_words.json` (in
`SATELLITES_DATA_DIR`) says which model to listen for, how sure the detector
must be, which satellites listen for it, and what happens once it is heard.
The Satellites tab edits the same entries through `GET` and `PUT
/satellites/wake-words`.

```json
{"version": 2,
 "words": [
  {"name": "alexa", "threshold": 0.6, "satellites": ["*"],
   "mode": "command", "language": null,
   "action": {"destination": {"type": "ha_assist", "url": "http://homeassistant.lan:8123"},
              "reply_to": "same", "voice": null, "fallback": "hey_jarvis"},
   "silence_ms": 800},
  {"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"],
   "mode": "conversation", "language": "en",
   "action": {"destination": {"type": "llm", "base_url": "http://ollama:11434/v1",
                              "model": "llama3.2", "system": "Be brief."},
              "reply_to": "same"},
   "conversation": {"follow_up_s": 8, "silence_ms": 600, "end_phrases": null}},
  {"name": "lumos", "threshold": 0.7, "satellites": ["94b97e7b8be8"],
   "mode": "trigger",
   "trigger": {"feedback": "earcon", "cooldown_s": 3, "ends_conversation": false}}],
 "ptt": {"mode": "command", "action": {"destination": {"type": "echo"}}}}
```

| Field | |
|---|---|
| `name` | One of `available` in `GET /satellites/wake-words`: a built-in openWakeWord model (`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `weather`), or `<name>.onnx` of your own in `SATELLITES_MODEL_DIR`. Each name once. `ptt` is push-to-talk and never a wake word. |
| `threshold` | 0.1 to 0.95; higher is stricter. Unset, 0.5, or 0.7 for a trigger word. |
| `satellites` | `["*"]` for every satellite, including one adopted later, or satellite ids. Empty means nobody hears the word. |
| `mode` | `command`, `conversation` or `trigger`. [Command, conversation and trigger](#command-conversation-and-trigger). |
| `language` | Unset (or `"auto"`): read from each transcript. A BCP 47 tag such as `pt-BR` is a hint: the word is always spoken in that language, and detection is skipped. |
| `action` | Command and conversation only; a trigger has none. `destination` (below), `reply_to` (`same`, `none`, or another satellite's id or name), `voice` (a Kokoro voice; unset, the voice of the language), and for a command `fallback`: the name of a conversation word that takes over when this destination fails or does not understand. |
| `silence_ms` | The pause that ends the command after the wake word. 800. |
| `conversation` | `follow_up_s` (8): how long the satellite listens for the next turn after a reply. `silence_ms` (600): the pause that ends a follow-up turn. `end_phrases` (unset: English and Brazilian Portuguese defaults; `[]` for none). |
| `trigger` | `feedback`: `earcon` (the satellite's `done` and a flash of the ring) or `none`. `cooldown_s` (3). `ends_conversation` (false). |

The file's `ptt` block is push-to-talk's own entry, without a name, threshold
or satellites. It cannot be a trigger.

**Destinations.** A destination that needs a credential names an environment
variable and never holds the value. Unknown fields are refused, a variable
name must look like one (so a pasted token is a 422), and a URL with a user
and password is refused. `GET /satellites/wake-words` says in `env` which
named variables are set, as booleans.

| `type` | Fields | |
|---|---|---|
| `ha_conversation` | `url`, `token_env` (`SATELLITES_HA_TOKEN`), `agent_id`, `timeout` (15) | Home Assistant's `POST /api/conversation/process`, with the language that was spoken |
| `ha_assist` | `url`, `token_env`, `pipeline` (an Assist pipeline id; unset, HA's preferred one), `timeout` | An Assist pipeline over HA's websocket API, run at its intent stage with the satellite's own HA device (so "the lights" are that room's). The reply is read by Kokoro. |
| `llm` | `base_url`, `model`, `system`, `api_key_env` (`SATELLITES_LLM_API_KEY`), `max_tokens` (400), `timeout` (30), `stream` (true) | Any OpenAI-compatible `/chat/completions`, streamed, with the conversation so far |
| `webhook` | `url`, `token_env`, `timeout` (15) | POST `{satellite, satellite_id, wake_word, mode, text, language, audio_seconds, history}`; a JSON `reply` string is spoken |
| `echo` | | Says back what it heard |

Both Home Assistant destinations keep HA's `conversation_id` for as long as a
conversation lasts, so HA keeps its own context between turns.

**A save merges by name.** A field an entry leaves out keeps what was saved
for that name, so a client that knows only `name`, `threshold` and
`satellites` cannot wipe an action by moving a word to another room. A new
name that gives no mode gets what the hub has always done: a command, echoed.
An entry that names a mode must say what that mode needs, or the PUT is a 422
and the old words stay: a command or a conversation needs an action, a
trigger must have none, and a `fallback` must name a conversation word.

A satellite listens only for the words assigned to it, and `GET /satellites`
lists them as its `wake_words`. Its stream runs only those models, so a word
for the kitchen costs the bedroom nothing. A satellite with no word still
streams and still takes push-to-talk.

`PUT /satellites/wake-words` replaces the whole set, and the change is live at
once, with no restart:

- **A threshold** is changed in place on every detector. Nothing is rebuilt.
- **An action** applies from the next time its word is heard.
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

A PUT is also refused with 422 `invalid_wake_words`, and the file is not
changed, for a name the hub cannot load, a name given twice, a threshold
outside 0.1 to 0.95, `"*"` together with ids, and an id the hub does not know.
A known id is one adopted, seen since the hub started, or already in the file,
so the list `GET` returned can always be sent back. A MAC with colons is taken
as the id.

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

**From `rules.json`.** Until 2026-09-25 routing lived in `rules.json`, keyed by
wake word. A `wake_words.json` from then has no `version`, and on its first
start each word, and push-to-talk, gets the rule it would have taken: the
first rule in file order that names the word or `*` and names no satellites.
A rule that named satellites cannot become one entry for every room; the log
names each one left behind. The hub writes the file as version 2 and leaves
`rules.json` where it is, untouched. A `rules.json` that does not load
migrates nothing, and those words route nowhere, as before, until they are
given an action.

### Lights

While a satellite listens, its ring points at the talker (`pixels`, full on the
nearest LED, soft on its neighbours), or pulses when there is no direction yet.
It spins while the router works, goes out when the reply starts, and pulses
again while a conversation waits for its next turn. LED 0 is assumed to
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
well, but the hub does not rely on it. A conversation still runs on a
satellite with its speaker off: each reply goes to the event stream only, and
the satellite listens for the next turn at once.

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
| `ptt` | Push-to-talk: listen as if a wake word had been heard, and do what the `ptt` entry in `wake_words.json` says |
| `stop` | What `POST /satellites/{id}/flush` does |
| `webhook:<url>` | POST `{"satellite", "satellite_id", "button", "action", "held_ms"}` to the URL, 10 s at most, no redirects followed |
| `none` | Nothing |

The default is the one above without the webhook. A PATCH replaces the whole
mapping. `rec` cannot be mapped: the firmware mutes on it before the hub hears
of the press. Every press is still published as an event, whatever it maps to.
The mapping stays on the hub and is never sent to the satellite.

**The volume buttons.** With `local_volume_buttons` on (the default), VOL+ and
VOL− change the volume on the satellite itself, which sends a status with the
new one straight after the press. That status, and only that one (within 2 s
of the press), is taken as the hub's volume too: the page and Home Assistant
show it, a `{"type": "volume", "satellite", "volume"}` event says so, and the
next welcome does not put the old value back. Any other status is not believed
on the volume, because one already on its way when the page changed it carries
the old value.

### Speaker or headphones

The Korvo's headphone jack switches in hardware, and no GPIO reads its detect
pin. With no plug, the codec's output runs through the jack's
normally-closed contacts to the speaker amplifier and to the loopback (ES7210
channel 0). A plug opens those contacts, so the loopback hears nothing
([schematic](https://dl.espressif.com/dl/schematics/ESP32-KORVO_V1.1_schematics.pdf),
sheet 4). After each sound the hub plays (a reply, a tone, an earcon), it
compares the loopback over that sound with its idle level (`app/output.py`):

| Loopback over the sound | Output |
|---|---|
| 10 dB or more above idle | `speaker` |
| 3 dB or less above idle, for a sound louder than −45 dBFS at volume 10 or more | `headphones` |
| Between, or a quiet sound, or too little loopback to judge | unchanged |

So it is known only once something has played since the satellite connected,
and a plug put in or pulled out in silence is seen at the next sound. A change
publishes `{"type": "output", "satellite", "output"}`. Nothing needs a
firmware change.

## Command, conversation and trigger

Each wake word has a mode (`app/router.py`, `app/dialogue.py`, and
`Conversation` in `app/main.py`).

| Mode | After the wake word |
|---|---|
| `command` | The words that follow go to the action once. The reply plays, and that is all. This is what every word did before modes existed. |
| `conversation` | The same, then the satellite keeps listening without the wake word, and each thing said is the next turn, with the turns so far. |
| `trigger` | Nothing. The word itself is the command: the hub publishes `triggered`, and Home Assistant decides what it does. |

### Conversations

After each reply has finished playing, or at once when nothing was played
(the speaker is off, or the answer was silent), the satellite listens for
`follow_up_s` without the wake word. The ring pulses while it waits. A
follow-up is heard only once the satellite's own voice has been off its
loopback channel for 250 ms, so the tail of the reply is never taken for the
next question.

A conversation ends on:

- **silence** for `follow_up_s`;
- **an ending phrase** said on its own: "that's all", "stop", "goodbye",
  "thanks", "obrigado", "tchau", "pode parar" and a few more, with filler
  around them ("ok, thanks"). "Thanks, and what about tomorrow?" carries on.
  The hub plays `done` and asks the assistant nothing;
- **an error** from STT, the assistant or TTS;
- **the stop button**, the privacy mute, the microphone turned off, forgetting
  the satellite, or another wake word;
- **no audio**: the device stopped streaming without a word, noticed 3 s after
  `follow_up_s`.

The conversation remembers its last 20 turns, and no more than 8000
characters of them. An `llm` destination gets them as chat messages (system
prompt, then the turns, then the new one), a `webhook` as `history`, and both
Home Assistant destinations keep HA's own `conversation_id` instead. A reply
that was interrupted is remembered as far as it was heard.

**Handing over.** A command whose action names a `fallback` hands the same
transcript to that conversation word when its destination fails, or does not
understand. Home Assistant says so with `response_type: "error"` (no intent
matched). The fallback answers in its own voice and language hint, and the
satellite is then in a conversation. Once any of the answer has been spoken,
a failure is only an error. Without a `fallback`, Home Assistant's "Sorry, I
couldn't understand that" is spoken, as it always was.

### Language

The hub reads the language of every utterance from its transcript, because
stt-stack's Parakeet recognises 25 European languages by itself, refuses a
`language` field with a 400, and does not say which language it heard. A
word's `language` hint replaces the detection.

The language decides three things. Home Assistant is sent it (Portuguese as
`pt-BR`). An `llm` is told to answer in it. The reply voice speaks it:

| Spoken | Voice | Answer in |
|---|---|---|
| English | `SATELLITES_TTS_VOICE` (`bm_george`) | English |
| Portuguese | `pf_dora` | Brazilian Portuguese |
| Spanish | `ef_dora` | Spanish |
| French | `ff_siwis` | French |
| Italian | `if_sara` | Italian |
| Any other Parakeet language (German, Polish, ...) | `SATELLITES_TTS_VOICE` | English, and an `llm` is told why |

A word's `voice` overrides the table. In a conversation the language follows
each utterance, and a short one ("ok", "sim") keeps the language already in
use: another language has to reach a probability of 0.3 and beat it by 0.1.

A hint is sent on to STT only when the engine takes one. The hub learns the
engine from the `x-stt-engine` header of every transcription, or asks
`/health` once before the first hinted request. Whisper gets the hint;
Parakeet never does.

The detector is py3langid 0.4.0, restricted to Parakeet's languages. On 96
short commands and questions in eight languages it got 94 right, against 90
for lingua-language-detector 2.2.0, and it installs in 4.4 MB rather than 295.
It costs about 50 MB of memory. The two misses were "liga a luz" (Romanian)
and "e a Roma?" (English, the prior). `app/language.py` has the rest.

### Streaming

Nothing waits for the whole answer. An `llm` streams its tokens, the hub cuts
them into sentences as each one ends, and each sentence goes to Kokoro and
then to the satellite while the model is still writing the next:

```text
llm ─tokens─▶ sentences ─▶ Kokoro, one request at a time ─▶ speaker queue ─▶ satellite
```

The first sentence goes to Kokoro alone, for the earliest first audio.
Sentences that queue up while Kokoro is busy go in one request, up to 300
characters. The speaker loop carries on from what the satellite still has
buffered, so a reply in ten pieces keeps the same 300 ms lead as one piece.

Each turn is timed from the end of speech, which is the endpoint less the
silence it waited for: `stt_done`, `first_token`, `first_audio`,
`answer_done`, `reply_done`. The turn event carries them as `timeline_ms`, and
`GET /satellites/{id}` keeps a summary of the last 20 spoken replies under
`latency`: medians of each, and the 90th percentile of `first_audio`.

With the fakes in `tests/test_conversation.py`, an LLM that writes three
sentences 0.5 s apart finishes 2.30 s after the end of speech. The first
audio reached the satellite at 1.30 s, 1.0 s before the model finished. That
1.30 s includes the 0.8 s endpoint silence and the model's own 0.5 s before
its first sentence.

### Barge-in

While a reply plays, the satellite's Ear keeps running, and three things
interrupt it:

- **Speech over the reply**, on the echo-cancelled output. The speaker is
  flushed, on the hub and on the satellite, the rest of the answer is
  cancelled, and in a conversation what was said becomes the next turn from
  its first syllable: the capture starts 600 ms before the detection. In a
  command, it only stops the reply.
- **The wake word.** The conversation's own word carries it on with what
  follows the word. Another word ends it and starts its own.
- **The stop button**, as before.

Barge-in by voice needs the front-end, and is armed only when the reply plays
on the satellite that heard: the canceller subtracts that satellite's own
loopback, and knows nothing of a reply playing in another room.

The detector reads the front-end's decision for each 16 ms block. A block
counts as speech when it is voiced by the front-end's own test, and, while
the satellite is playing, only when its SNR against the noise and the
canceller's model of its leftover echo reaches 10 dB. Barge-in needs 18 such
blocks in the last 30: 288 ms of speech within 480 ms. The front-end holds
voiced blocks down for the first 2 s of a satellite's first playback, while
the canceller learns the room.

The 10 dB bar comes from synthetic scenes on darwin, with the reply at -20
dBFS and each microphone hearing it through a different room response:

| Scene | Voiced blocks at the front-end's usual bar (3 dB) | Most blocks counted in any 30 |
|---|---|---|
| Linear echo, canceller converged (27-38 dB ERLE) | none | 0 of the 18 needed |
| The loudspeaker clipping at -14 to -24 dBFS (4-12 dB ERLE) | 32-42 % | 4 |
| A talker at -30 to -42 dBFS over it | | caught 0.30-0.37 s after they began |

`tests/test_conversation.py` runs this through the hub. The satellite plays a
9 s reply and hears its own clipped voice for 5 s, which stops nothing. Then
a talker at -30 dBFS speaks over it. The detection came 352 ms after their
first syllable, the reply stopped, and the captured turn held all 2 s of what
they said.

Not measured: anything on the board. A change of the echo path in mid-reply
is the weak case. Swapping the whole room response brought the count to 17 of
the 18 needed while the canceller re-converged, so moving the satellite while
it speaks may stop its reply.

### Trigger words

A trigger word acts on its detection alone, with no second step to catch a
false one, so it is stricter by default: threshold 0.7 rather than 0.5. The
same word does not fire again within `cooldown_s` (3 s), because one
utterance can score over the threshold in several frames. Heard during a
conversation, a trigger fires and the conversation goes on, unless
`ends_conversation` says otherwise.

The hub publishes `{"type": "triggered", "satellite", "satellite_name",
"wake_word", "score", "direction", "at"}` and does nothing else. The
Calliope integration for Home Assistant (`clients/home-assistant`) turns that
into a device trigger, and an automation there decides what "lumos" does. With
`feedback: "earcon"` the satellite plays `done` where its speaker is on, and
flashes its ring where its lights are on and no conversation holds the ring.

## Routing

`GET /satellites/routing` shows what each wake word does, which named secret
variables are set (never their values), the STT and TTS URLs, the STT engine
and warnings. `PUT /satellites/routing` answers 409 `routing_per_wake_word`:
routing is saved with the wake words. `POST /satellites/routing/test` takes
`{"satellite", "wake_word", "text"}`, runs that word's action and TTS without
STT, and plays nothing.

Destination URLs are not filtered for private addresses, on purpose: Home
Assistant on the LAN is the main target, and whoever can save a wake word can
already reflash every satellite. Redirects are not followed, over HTTP or over
Home Assistant's websocket. Every external call has a hard time limit: STT 30
s, TTS 30 s, the destination its own `timeout`.

## Home Assistant over MQTT

With `SATELLITES_MQTT_URL` set, every adopted satellite is one Home Assistant
device, by discovery: Wi-Fi signal, online, microphone, speaker and lights
switches, volume, audio output (speaker or headphones, unknown until something
has played), last wake word, a wake word event and one event per button. A
switch goes through the same code as `PATCH /satellites/{id}`, and nothing but
those four settings can be changed from the broker. Button presses and wake
words are dropped while the broker is away rather than delivered late; state,
discovery and availability are retained and sent again on every reconnect.
Forgetting a satellite removes its device. Injected test clips are not
published. Checked against Home Assistant 2026.8.1's own MQTT integration: 14
entities under one device. The audio output sensor came after, and is checked
against that version's sensor code only: an unknown output renders `None`,
which it takes as no value rather than as an invalid option.

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
ended, its length), `outcome` (the word that answered, transcript, language,
voice, reply text, errors, timings, reply audio length) and `played`. An
injected clip is always one turn, whatever the word's mode, and a trigger
word answers `"triggered": true` with its event marked injected. Detection listens for the satellite's own
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
| `SATELLITES_HA_TOKEN` | unset | The default `token_env` of the `ha_conversation` and `ha_assist` destinations: a long-lived access token |
| `SATELLITES_LLM_API_KEY` | unset | The default `api_key_env` of an `llm` destination. Unset, no `Authorization` is sent. |
| `SATELLITES_FIRMWARE_PUBKEY` | unset | A PEM public key, or a path to one. Set, uploads must be signed by it. A bad value stops the service at start. |
| `SATELLITES_API_KEYS` | unset | As on the other backends. Behind the gateway it stays unset. |
| `SATELLITES_LOG_LEVEL` | `INFO` | Transcripts and replies are logged only at `DEBUG`. |

Before 2026-09-25 every one of these was `NODES_*`. The old names are not
read. At start the hub logs a warning for each `NODES_*` variable still set,
with the name it reads now, except one that a wake word's action names: an
action carried over from a rule saved before the rename says `"token_env":
"NODES_HA_TOKEN"` and reads exactly that
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
- Barge-in while the satellite is still listening or routing. It applies while
  a reply plays and while a conversation waits for its next turn. During
  playback the beam keeps the steering it had before playback started.
- Barge-in by voice without the front-end, or on a reply played on another
  satellite. Only the wake word and the stop button interrupt those.
- Tokens from the destination read aloud as they arrive. The unit is the
  sentence, because Kokoro reads a whole sentence better than a fragment.
- Discovery. A satellite is told its hub in the setup portal, or moved with
  `set-hub`.
- Speaker identification, and a television or a second talker is speech to the
  front-end: it tells speech from noise by how steady it is.

## Licence

BSD 2-Clause. See [LICENSE](LICENSE). The wake word models in the image are
CC BY-NC-SA 4.0 (non-commercial) and carry their own notice; see
[THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
