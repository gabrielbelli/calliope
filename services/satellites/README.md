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
voice-satellites ──▶ a SearXNG and Open-Meteo, only for a language model word with those tools on
```

Sibling of `stt`, `tts`, `tts-long`, `gateway` and `ui`. The first satellite is
the ESP32-Korvo v1.1:
[`clients/korvo-satellite`](../../clients/korvo-satellite/README.md). The
decision to bring satellites in through the gateway, and to keep their socket
out of `GATEWAY_API_KEYS`, is
[ADR 0013](../../docs/adr/0013-satellites-one-door.md).

**Contents:** [Quick start](#quick-start) · [Deploy](#deploy) ·
[Upgrade](#upgrade) · [Status](#status) · [Adoption](#adoption) ·
[Routes](#routes) · [Events](#events) · [The device protocol](#the-device-protocol) ·
[Listening](#listening) · [Wake words](#wake-words) ·
[Destinations](#destinations) · [Keys](#keys) · [Lights](#lights) ·
[Buttons](#buttons) · [Command, conversation and trigger](#command-conversation-and-trigger) ·
[Speech-to-text](#speech-to-text) · [Routing](#routing) ·
[Home Assistant over MQTT](#home-assistant-over-mqtt) ·
[Signed firmware](#signed-firmware) ·
[Verifying the pipeline without a voice](#verifying-the-pipeline-without-a-voice) ·
[Configuration](#configuration) · [Install and tests](#install-and-tests)

## Quick start

With the stack deployed and the hub in it ([Deploy](#deploy)):

1. Flash a satellite once over USB, with the hub's address built in
   ([first flash](../../clients/korvo-satellite/README.md#first-flash-once-over-usb)).
2. Join the Wi-Fi network it opens, `calliope-sat-XXXX`, from a phone, and
   give it your Wi-Fi and the hub's address.
3. On the page's **Satellites** tab, name it and press **Adopt**.
4. Under **Wake words**, add a word or open `hey_jarvis`, and choose what it
   does under **Action**. Press **Save wake words**.
5. Type a sentence under **Try a word** to run that action without speaking,
   then say the wake word to the satellite.

## Deploy

The hub is optional. The stack runs without it, and adding it changes nothing
else.

1. **Add the hub to the deployment.** `compose.yaml` has the
   `voice-satellites` block and its `nodes-data` volume. The volume holds the
   adoptions, the wake words, stored keys, firmware and models: losing it
   un-adopts every satellite. The gateway reaches the hub through
   `GATEWAY_SATELLITES_URL` (default `http://voice-satellites:8003`).
2. **Point it at speech.** `SATELLITES_STT_URL` and `SATELLITES_TTS_URL`, as
   compose sets them. Without STT, wake words are heard and nothing is
   transcribed.
3. **Serve the gateway over TLS with a certificate the firmware trusts.**
   Release firmware connects only to `wss://`, and it checks the certificate
   against the CA roots compiled into it: ISRG Root X1 and X2 (Let's Encrypt)
   unless the build names others. The certificate can be on the gateway
   (`GATEWAY_TLS_CERT`) or on a proxy in front of it. With any other CA, an
   internal or a self-signed one, build the firmware with `CALLIOPE_HUB_CA`
   naming that CA's root
   ([korvo-satellite](../../clients/korvo-satellite/README.md#wi-fi-and-adoption)).
   A satellite that cannot verify the hub spins blue and says nothing more.
   Plain `ws://` works only in a development build (`DEV_HUB`).
4. **Deploy with `--remove-orphans`**
   (`docker compose up -d --remove-orphans`). It matters only on a deployment
   that ran a pre-release image, which had a `voice-nodes` container
   ([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)).
5. **Flash, connect and adopt** a satellite, as in the [Quick start](#quick-start).
   The hub address is `wss://<host>` with the port if it is not 443, such as
   `wss://calliope.example.com` or `wss://calliope.example.com:30080`. The
   firmware adds `/satellites/ws` itself.
6. **Give a wake word something to do.** Every satellite hears `hey_jarvis`
   at first, and it echoes what it heard until it has an action
   ([Wake words](#wake-words)).
7. **Then, as needed:** an API key for a language model ([Keys](#keys)),
   Home Assistant through the Calliope integration
   ([`clients/home-assistant`](../../clients/home-assistant/README.md)) or
   over MQTT ([below](#home-assistant-over-mqtt)), and a firmware signing key
   ([Signed firmware](#signed-firmware)).

## Upgrade

**Update the hub first, then the firmware.** The hub checks what each
satellite says it can do (`hello.caps`) before it sends anything newer, so a
new hub works with old firmware. Old hubs know nothing of newer firmware.

1. Raise the image tag of `voice-satellites` in `compose.yaml`, and of
   `voice-gateway` and `voice-ui` when the release changes them, then
   `docker compose up -d --remove-orphans`.
2. Update each satellite over the air: `pio run -e ota -t upload`, or upload
   the image on the Satellites tab and press **Update every satellite**
   ([korvo-satellite](../../clients/korvo-satellite/README.md#updates-over-the-air)).
   A satellite that cannot reach the hub on its new image rolls back.

A release is a `v*` tag, and compose names a version rather than a moving
tag ([ADR 0012](../../docs/adr/0012-one-branch.md)). A push to a `feat/**`
branch also publishes images, as `:feat-<branch>` (moving) and
`:feat-<branch>-<sha>` (fixed), and never `:latest`. Deploy the fixed one to
try a feature before its release.

### Firmware compatibility

| Firmware from | What it adds | What the hub does for older firmware |
|---|---|---|
| 2026-09-25 | `/satellites/ws`, the setup network `calliope-sat-XXXX`, and its settings in `hello` | Answers `/nodes/ws` too. Leaves the settings out of the welcome until the satellite's first `status` reports them ([Adoption](#adoption)) |
| 2026-09-26 | `boot` (the start-up stages) in `hello`, and a restart when start-up stalls | Shows no `boot` in `GET /satellites/{id}` |
| 2026-09-27 | Every button mappable (`caps.actions`, `button_actions`), a status marked `"cause": "button"`, twelve volume steps, `ring_top` and `ring_upside_down` | Keeps the old buttons: Rec mutes, and the volume pair works on the device while `local_volume_buttons` is on. Takes a status within 2 s of a volume press as the button's |
| 2026-09-28 | The `listen` light mode (`caps.light_modes`), a ring with no random dim LEDs, and a privacy mute that survives a restart | Sends a pulse where it would send `listen` |

A signed build adds `caps.ota_key`, and the hub sends it only images signed
for that key ([Signed firmware](#signed-firmware)).

## Status

**Run live on an ESP32-Korvo,** adopted over `wss://` through a proxy in front
of the gateway, with the hub on a development server with no GPU (an
eight-thread Xeon E5-2697 v4):

- Adoption keeping the satellite's own settings, config, lights off, and a
  four-channel stream with no dropped frames (25 Sep 2026).
- Listening: the front-end and the wake word at a real-time factor of 0.09 on
  that Xeon, 24 % of one core, 215 MiB (25 Sep 2026).
- Signed updates: a 1.17 MB release image installed in 20 s, including through
  the Satellites tab's route. The hub will not send an unsigned image to a
  satellite that advertises a key, and with the hub bypassed, the satellite
  refused an image signed by another key (`bad signature`) and stayed on its
  image (25 Sep 2026).
- A spoken command answered by Home Assistant, its reply played through the
  3.5 mm jack. That run found that a plug in the jack cuts the echo reference
  ([Barge-in](#barge-in)) (27 Sep 2026).
- The button ladder, and why KEY1 sends nothing (27 Sep 2026).

**Run on the server from recorded clips** (`/inject`): "hey jarvis, what time
is it" is detected (score 0.995), transcribed by Parakeet as "What time is
it?", routed by the default echo action, and answered by Kokoro (1.3 s). "The
weather is fine today" is ignored.

**Tested only against fakes:** conversation and trigger words, follow-ups,
voice barge-in on the satellite's own speaker, streamed replies, language
model tools, and the language of each utterance. Earcons and ducking have not
been recorded as heard on the board, nor the direction of the listening arc.

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

**Adoption takes the satellite's own settings**: volume, mic gain, and the
microphone, speaker and lights switches. The satellite is the thing in the
room, so a bedroom satellite that was dark stays dark. The satellite says
these settings in its `hello`, and the welcome keeps them. Only these are
taken, only within the ranges `PATCH` accepts, and never `buttons`.

Firmware from before 2026-09-25 says them only in its `status`, every 10 s,
so a satellite adopted in its first seconds has said nothing yet. For such a
satellite the hub does not guess:

- The welcome leaves those settings out, so the satellite keeps what it has.
- Until the satellite reports them, the record holds its switches as off, so
  nothing lights or plays on a value the hub made up. The firmware reports as
  soon as it applies the welcome.
- A setting changed with `PATCH` in the meantime is the hub's, and the report
  does not undo it.

A satellite's id is its MAC, which is no secret. While an adopted satellite is
connected, a `hello` with its id and without its token is closed with 1008,
so another connection cannot push it offline and report as it. A board that
reconnects with its token replaces its own stale socket as before. While it
is offline, such a connection is only pending under its id: the satellite
stays offline, with its own firmware and settings, on the page and in Home
Assistant, and nothing that connection reports is published as its. A board
that lost its token (a factory reset) is forgotten and adopted again.

A satellite is addressed by its id (the MAC, with or without colons) or its
name.

## Routes

| Route | What it does |
|---|---|
| `WS /satellites/ws` | The device connection. Protocol below. |
| `WS /nodes/ws` | The same handler, under the name pre-release builds used until 2026-09-25. A board flashed from one runs firmware that connects here, and its next firmware arrives over this socket, so the old path stays until no board reports firmware from before the rename ([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)). The gateway relays both. |
| `GET /health` | No key. `status`, and `satellites` (`online`, `adopted`, `pending`), `tts`, `voice` (the wake word engine's state, its words and thresholds, the front-end, the model directory), `routing` (how many words have an action, the STT URL and engine, a `load_error`) and `mqtt` (null without it). The gateway's own `/health` carries it as `backends.satellites` |
| `GET /satellites` | Every satellite seen since the hub started, adopted or not, each as `GET /satellites/{id}` describes it |
| `GET /satellites/events` | Server-sent events, one JSON object each. [Events](#events) lists every type |
| `GET /satellites/wake-words` | `{"available", "words", "ptt", "custom", "env", "secrets", "tools", "warnings", "load_error"}`: the names the hub can load, each word's whole entry with its `state` and `error`, push-to-talk's entry, which secrets the actions name have a value, where each value lives, and which language model tools work here (`web_search` only with `SATELLITES_SEARXNG_URL`). [Wake words](#wake-words). |
| `PUT /satellites/wake-words` | `{"words": [...], "ptt": {...}}`: replace them all, live. A field an entry leaves out keeps its saved value. A bad set is a 422 and the old one stays. |
| `POST /satellites/wake-words/models?name=` | A custom wake word: the `.onnx` as the raw body, checked to be an openWakeWord classifier (input `[batch, 16, 96]`, under 5 MB) before it is written. Then offered in `available` and assigned like a built-in. [`tools/wakeword-train`](../../tools/wakeword-train/README.md) trains one; a model trained there is CC BY-NC-SA 4.0, because its training features and feature models are. |
| `DELETE /satellites/wake-words/models/{name}` | Only a custom model, and only once no wake word uses it (409 otherwise). |
| `GET /satellites/{id}` | One satellite: its name, whether it is adopted and online, model, `firmware`, `config`, its last `status`, `caps`, the update in progress (`ota`), `listening`, `earcons`, `wake_words` (the names assigned to it), `output` (`speaker`, `jack` or `null`, [Speaker or jack](#speaker-or-jack)), `boot` (`reset_reason`, `stages_ms`, and `stalled_in` and `stall_restarts` after a stalled start; firmware from 2026-09-26) and `latency` (how quickly its last 20 replies began and ended, [Streaming](#streaming)) |
| `PATCH /satellites/{id}` | `name`, `volume` (0-100), `mic_gain_db` (0-37.5), `mic_enabled`, `speaker_enabled`, `lights_enabled`, `brightness` (1-100), `ring_top` (0-11: the LED at 12 o'clock as mounted, where a bar on the ring starts) and `ring_upside_down` (the bar then runs the other way, so it still fills clockwise as seen), `buttons` ([Buttons](#buttons)); `local_volume_buttons` for firmware from before 2026-09-27 |
| `POST /satellites/{id}/adopt` | `{"name": "..."}` |
| `POST /satellites/{id}/forget` | |
| `POST /satellites/{id}/identify` | Blink for five seconds. Works before adoption, which is the point. |
| `POST /satellites/{id}/reboot` | |
| `POST /satellites/{id}/lights` | `{"mode": "off|solid|pulse|spin|pixels", "color": [r,g,b], "brightness": 0-255, "pixels": [[r,g,b], ...]}`. 409 for a satellite with `lights_enabled` false. `listen` is the hub's own, for a conversation, and is not offered here. |
| `POST /satellites/{id}/tone` | `{"frequency": 440, "seconds": 1}` on the satellite's speaker. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/say` | `{"text": "...", "voice": "bm_george"}`: Kokoro, via `SATELLITES_TTS_URL`. 409 for a satellite with `speaker_enabled` false. |
| `POST /satellites/{id}/flush` | Stop: drop the speaker audio queued and playing, and cancel the conversation in progress |
| `POST /satellites/{id}/ptt` | `{"wake_word": "..."}`, optional: listen as if the satellite's push-to-talk button had been pressed, handled by that word's entry or by `ptt`. 204, or 409 naming why not (`satellite_busy`, `satellite_muted`, `mic_disabled`, `trigger_word`), or 404 `wake_word_not_found`. For Home Assistant. |
| `GET /satellites/{id}/listen?seconds=5&channel=` | A WAV of the raw mic channels, up to 60 s |
| `POST /satellites/{id}/set-hub` | `{"url": "wss://host:port"}`: the satellite saves it and reboots onto that hub |
| `POST /satellites/{id}/inject?play=0&wake_word=` | A 16 kHz mono 16-bit WAV as the body, through the satellite's own wake words, the endpoint and routing path as if the satellite had heard it. [Verifying](#verifying-the-pipeline-without-a-voice). |
| `GET /satellites/routing` | What each wake word does, which secret variables are set (never their values), the STT and TTS URLs and engine, warnings |
| `PUT /satellites/routing` | 409 `routing_per_wake_word`: routing is saved with each wake word. [Routing](#routing). |
| `POST /satellites/routing/test` | `{"satellite", "wake_word", "text"}`: a typed sentence through that word's action and TTS. Plays nothing. |
| `POST /satellites/ha/pipelines` | `{"url", "token_env"}`: Home Assistant's Assist pipelines and its preferred one, asked with the token that variable holds, for an `ha_assist` word's picker. 409 `token_missing` when it holds none. |
| `POST /satellites/llm/models` | `{"base_url", "api_key_env"}`: `{"models": [...]}`, the ids a language model server lists at `GET {base_url}/models`, asked with that key, for an `llm` word's picker. A listing that pages (`has_more` and `last_id`, as Anthropic's does) is read to its end, from `after_id`, up to ten pages more. 502 in the server's own words, 504 after 10 s. |
| `POST /satellites/llm/test` | An `llm` destination, saved or not: one short question through the same path a turn takes, with no TTS. `{"model", "reply", "first_token_ms", "total_ms", "token_limit"}`, or 502 in the provider's words, or 504 after 25 s. |
| `PUT /satellites/secrets` | `{"name": "OPENAI_API_KEY", "value": "..."}`: store an API key on the hub under that name, or clear it with `"value": null`. Answers the wake word view. No route reads a value back. 409 `set_in_environment` when the environment already sets the name. [Keys](#keys). |
| `GET /satellites/firmware` | Uploaded images |
| `POST /satellites/firmware?model=&version=&signature=` | The `.bin` as the raw body. It must start with the ESP32 image magic (0xE9) and fit a 4 MB slot. `signature` is base64 or base64url DER ECDSA. |
| `DELETE /satellites/firmware/{sha256}` | |
| `POST /satellites/ota` | `{"satellite": "<id>|<name>|all", "sha256": "..."}`. Images are only sent to adopted, online satellites of the image's model, and not to a satellite that would refuse the signature. |

The gateway routes all of these. The Satellites tab uses all but five:

- `GET /satellites/{id}`, because the list already carries every satellite.
- `GET` and `PUT /satellites/routing`, because routing lives on each wake
  word and the tab edits it there.
- `ptt`, which is Home Assistant's.
- `inject`, which is for scripts. A button that runs a clip through a
  satellite's real actions would be one press from Home Assistant acting on
  it.

### Events

`GET /satellites/events` is a server-sent event stream behind the same keys
as the rest of the API. Each event is one JSON object, and `satellite` is the
satellite's id. An event from a clip run through `/inject` carries
`"injected": true`.

| `type` | When | Fields |
|---|---|---|
| `online` | An adopted satellite connects | `name`, `firmware` |
| `offline` | A satellite's socket closes | |
| `pending` | A satellite with no token connects | `address` |
| `status` | Every status report, about every 10 s | `status`: the report as sent ([protocol](#the-device-protocol)) |
| `settings` | A button on the satellite changed a setting | `settings`: what changed, among `volume`, `lights_enabled` and `brightness` |
| `output` | The hub decides the audio goes to the speaker or the jack | `output` ([Speaker or jack](#speaker-or-jack)) |
| `button` | A button is pressed or released | `button`, `action` (`press` or `release`), `held_ms` |
| `ota` | An update moves on | `state` (`started`, `progress`, `rebooting`, `verified`, `failed`), `pct`, `version`, `error` |
| `wake_words` | A wake word's model finishes downloading, or fails | `words`: every word with its `state` and `error` |
| `wake` | A wake word is heard, or push-to-talk pressed | `wake_word`, `score`, `direction` |
| `routed` | A command, or an injected clip, has been answered | `wake_word`, `rule_id` (the word that answered), `mode`, `reply_to`, `error`, `transcript`, `language`, `language_source`, `reply_language`, `voice`, `reply_text`, `spoken_text`, `interrupted`, `timings_ms`, `timeline_ms`, `endpoint`, `command_s`, `played`, `note` |
| `conversation_started` | A conversation word is heard, or a command hands over | `wake_word`, `rule_id`, `reason` (`wake_word` or `fallback`), `from_rule`, `follow_up_s` |
| `turn` | Each exchange in a conversation | the fields of `routed`, and `turn`, `ended`, `handed_over_to` |
| `conversation_ended` | A conversation ends | `rule_id`, `turns`, `seconds`, `reason` ([Conversations](#conversations)) |
| `triggered` | A trigger word is heard | `satellite_name`, `wake_word`, `score`, `direction` |

Transcripts and replies are in this stream and not in the INFO log.

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

**Text frames, satellite → hub**

| `type` | When | Fields |
|---|---|---|
| `hello` | On connecting, and again after adoption with the new token | `id` (the MAC), `model`, `fw`, `token`, `name`, `reset_reason`, `ota_pending`, `caps` (below), `boot` (`stages_ms`, and `stalled_in` and `stall_restarts` after a stalled start; firmware from 2026-09-26), and its settings: `volume`, `mic_gain_db`, `mic_enabled`, `speaker_enabled`, `lights_enabled`, `brightness`, `ring_top`, `ring_upside_down` (firmware from 2026-09-25; the last two from 2026-09-27) |
| `status` | Every 10 s, after every `welcome` or `config`, and at once after a button changed a setting | `uptime_s`, `rssi`, `heap`, `psram`, `muted`, the same settings as `hello`, `mic_dropped`, `spk_dropped`, `spk_buffered_ms`, `duck`, `earcons_ready`, `buttons_mv` (the button ladder's `now`, `min`, `max` and `polls`), and `cause: "button"` after a button's action (firmware from 2026-09-27) |
| `button` | A press or a release | `button`, `action` (`press` or `release`), `held_ms` |
| `ota` | An update moves on | `state` (`started`, `progress`, `rebooting`, `failed`, `verified`), `version`, `pct`, `error`. A signed build fails with `unsigned image` or `bad signature` |
| `ota_next` | The next piece of an update, please | `offset` |
| `earcons` | The answer to `earcon_list` | `ready`, `items` (`id`, `size`, `sha256`), `last_load_us` |
| `earcon_next`, `earcon_stored`, `earcon_failed` | An earcon upload moves on | `id` and `offset`; `id`, `size` and `sha256`; `op` (`put`, `play` or `delete`), `id` and `error` |

`hello.caps` names what the satellite can do. The hub sends a message that
needs a capability only to a satellite whose caps list it. Older firmware
ignores what it does not know without a word.

| Capability | Meaning |
|---|---|
| `mic` | `rate`, `channels`, `format` of the microphone stream |
| `speaker` | `rate`, `channels`, `format` it plays |
| `lights` | The number of LEDs |
| `light_modes` | The light modes it draws. `listen` is in it from 2026-09-28; without it the hub uses the first five |
| `buttons` | The buttons it reports |
| `actions` | The button actions it runs itself, which `button_actions` may name (from 2026-09-27) |
| `earcons` | `max`, `max_bytes`, `rate` of the sounds it can keep |
| `duck` | `true`: it takes `duck` and `unduck` |
| `ota_key` | On a signed build, the id of the key an update must be signed by |

**Text frames, hub → satellite**

| `type` | Fields |
|---|---|
| `pending` | None: not adopted |
| `adopt` | `token`, `name` |
| `welcome` | `name`, and `config`: `volume`, `mic_gain_db`, `mic_enabled`, `speaker_enabled`, `lights_enabled`, `brightness`, `ring_top`, `ring_upside_down`, `local_volume_buttons`, and `button_actions` for firmware that lists `actions` |
| `config` | Any subset of the same |
| `lights` | `mode` (`off`, `solid`, `pulse`, `spin`, `pixels`, `listen`), `color` `[r, g, b]`, `brightness` 0-255, `pixels`, and for `listen` the talker's `direction` in degrees or `null` |
| `identify` | `seconds` |
| `reboot`, `forget`, `flush`, `unduck`, `earcon_list` | None |
| `set_hub` | `url` |
| `ota` | `size`, `sha256`, `version`, and `signature` when the image has one |
| `earcon_put` | `id`, `size`, `sha256` |
| `earcon` | `id`: play it |
| `earcon_delete` | `id`. The firmware takes it; the hub does not send it today |
| `duck` | `level` 0-100 on the volume scale, and `ms`, 0 for until `unduck` |

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
3. **Endpointer:** webrtcvad. The command ends after the word's `silence_ms`
   without speech (800 ms unless set), at 10 s, or at 4 s if no speech
   started.
4. **What the word does**, one conversation per satellite at a time: the
   `wake` earcon if the satellite holds it, the ring lit in the word's colour
   ([Lights](#lights)),
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

**Events.** A conversation publishes `wake`, then `routed` for a command or
`conversation_started`, a `turn` per exchange and `conversation_ended` for a
conversation, and a trigger word only `triggered`
([Events](#events)). `conversation_ended` gives its `reason`: `silence`,
`phrase`, `error`, `no_audio`, `stop`, `muted`, `mic_off`, `wake_word`,
`trigger` or `unadopted`.

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
   "action": {"destination": {"type": "ha_assist", "url": "http://homeassistant.local:8123"},
              "reply_to": "same", "voice": null, "fallback": "hey_jarvis"},
   "silence_ms": 800},
  {"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"],
   "mode": "conversation", "language": "en",
   "action": {"destination": {"type": "llm", "base_url": "https://api.openai.com/v1",
                              "model": "<a model id the provider lists>",
                              "api_key_env": "OPENAI_API_KEY", "system": "Be brief."},
              "reply_to": "same"},
   "conversation": {"follow_up_s": 8, "silence_ms": 600, "end_phrases": null}},
  {"name": "lumos", "threshold": 0.7, "satellites": ["020000000001"],
   "mode": "trigger", "colour": "#ffb000",
   "trigger": {"feedback": "earcon", "cooldown_s": 3, "ends_conversation": false}}],
 "ptt": {"mode": "command", "action": {"destination": {"type": "echo"}}}}
```

| Field | Default | Range | What it does |
|---|---|---|---|
| `name` | | | One of `available` in `GET /satellites/wake-words`: a built-in openWakeWord model (`alexa`, `hey_jarvis`, `hey_mycroft`, `hey_rhasspy`, `weather`), or `<name>.onnx` of your own in `SATELLITES_MODEL_DIR`. Each name once. `ptt` is push-to-talk and never a wake word. [`tools/wakeword-train`](../../tools/wakeword-train/README.md) trains a model of your own |
| `threshold` | 0.5; 0.7 for a trigger word | 0.1 to 0.95 | How sure the detector must be. Higher is stricter |
| `satellites` | | | `["*"]` for every satellite, including one adopted later, or satellite ids. Empty means nobody hears the word |
| `mode` | `command` | `command`, `conversation`, `trigger` | [Command, conversation and trigger](#command-conversation-and-trigger) |
| `language` | unset | a BCP 47 tag, or `"auto"` | Unset or `"auto"`: read from each transcript. A tag such as `pt-BR` is a hint that the word is always spoken in that language, and detection is skipped |
| `action` | an echo | | Command and conversation only; a trigger has none. `destination` ([Destinations](#destinations)), `reply_to` (`same`, `none`, or another satellite's id or name), `voice` (a Kokoro voice; unset, the voice of the language), and for a command `fallback`: the name of a conversation word that takes over when this destination fails or does not understand |
| `silence_ms` | 800 | 200 to 3000 | The pause that ends the command after the wake word |
| `colour` | unset: the listening blue | `#rrggbb` | The ring's colour while this word listens, thinks and, for a trigger, flashes. Anything else is a 422 |
| `conversation.follow_up_s` | 8 | 1 to 60 | How long the satellite listens for the next turn after a reply |
| `conversation.silence_ms` | 600 | 200 to 3000 | The pause that ends a follow-up turn |
| `conversation.end_phrases` | unset | up to 64, each up to 64 characters | Unset: the usual ones in English and in the conversation's languages ([Conversations](#conversations)). `[]` for none |
| `trigger.feedback` | `earcon` | `earcon`, `none` | `earcon` plays the satellite's `done` and flashes the ring |
| `trigger.cooldown_s` | 3 | 0 to 600 | How long the same word cannot fire again |
| `trigger.ends_conversation` | false | | Whether the word ends a conversation it is heard in |

The file's `ptt` block is push-to-talk's own entry, without a name, threshold
or satellites. It cannot be a trigger.

#### Saving

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
changed, for a name the hub cannot load, a name given twice, a value outside
its range in the table above, `"*"` together with ids, and an id the hub does
not know.
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

### Destinations

A destination that needs a credential names it and never holds it:
`token_env` or `api_key_env` is the name of a secret, whose value the hub
reads on every request from its environment or from the keys it holds
([Keys](#keys)). `GET /satellites/wake-words` says in `env` which named
secrets have a value, as booleans, and in `secrets` where each value lives.

The hub refuses, with a 422:

- a field it does not know;
- a name that does not look like a variable's, so a pasted token is refused;
- a URL with a user and password in it;
- a name that is one of the hub's own settings: under `SATELLITES_` (or
  `NODES_`) with no `TOKEN`, `KEY`, `SECRET` or `PASSWORD` in it as a word.
  `SATELLITES_MQTT_URL`, which carries the broker's password, is one. Such a
  name holds nothing for any destination, wherever an action, a picker or the
  key box names it.

| `type` | Fields | What it does |
|---|---|---|
| `ha_conversation` | `url`, `token_env` (`SATELLITES_HA_TOKEN`), `agent_id`, `timeout` (15, up to 120) | Home Assistant's `POST /api/conversation/process`, with the language that was spoken |
| `ha_assist` | `url`, `token_env`, `pipeline` (an Assist pipeline id, picked by name on the page; unset, HA's preferred one), `timeout` (15, up to 120) | An Assist pipeline over HA's websocket API, **set up in Home Assistant**. It hears the command with its own speech-to-text, understands it with the satellite's own HA device (so "the lights" are that room's), and speaks the reply with its own text-to-speech and voice, in its own language. The word's language and voice are not read. A pipeline with no speech-to-text or text-to-speech leaves that part to Calliope's own. The satellite's device exists only where the Calliope integration is installed ([`clients/home-assistant`](../../clients/home-assistant/README.md)) |
| `llm` | [below](#language-model-destination) | Any OpenAI-compatible `POST /chat/completions`, streamed, with the conversation so far |
| `webhook` | `url`, `token_env`, `timeout` (15, up to 120) | POST `{satellite, satellite_id, wake_word, mode, text, language, audio_seconds, history}`. A JSON `reply` string is spoken |
| `echo` | | Says back what it heard |

Both Home Assistant destinations keep HA's `conversation_id` for as long as a
conversation lasts, so HA keeps its own context between turns.

#### Language model destination

| Field | Default | Range | What it does |
|---|---|---|---|
| `base_url` | | | The address before `/chat/completions`, such as `https://api.openai.com/v1`. One saved with `/chat/completions` on the end is saved without it |
| `model` | | up to 120 characters | The model id, from the server's list or typed |
| `system` | unset | up to 8000 characters | The system prompt. The hub adds the date and time, what the tools are for, and the language to answer in |
| `api_key_env` | `SATELLITES_LLM_API_KEY` | a variable's name, or `null` | The key's name ([Keys](#keys)). With no value, no `Authorization` is sent, which suits a server of your own |
| `max_tokens` | 400 | 1 to 8192 | The reply limit. A reasoning model spends part of it thinking |
| `timeout` | 30 | up to 120 | Seconds |
| `stream` | true | | Stream the reply, so the first sentence is spoken while the model writes the rest ([Streaming](#streaming)) |
| `tools` | none | `web_search`, `weather` | [Tools and the date](#tools-and-the-date) |

The hub sends `model`, `messages`, the token limit and `stream`, and no
`temperature`. The limit goes as `max_tokens`. A server that refuses it and
asks for `max_completion_tokens` (OpenAI's reasoning and GPT-5-class models)
is asked again once with that name, and the hub keeps the name for that model
until it restarts.

Only the answer's text is spoken, never reasoning: not `reasoning_content`,
`reasoning`, a `<think>` block or a "thinking" part. A model that spent its
whole `max_tokens` thinking is an error that says so. A refusal is shown in
the provider's words, with any key in it hidden.

#### Tools and the date

Every language model prompt carries the local date and time, with tools or
without, so "what day is it" costs no round trip. The time zone is
`SATELLITES_TIMEZONE`, else Home Assistant's, else `TZ`, else UTC.

A word's `tools` lets the model call these:

| Tool | Source | What leaves the hub, and to whom | Limits | Needs |
|---|---|---|---|---|
| `web_search` | A SearXNG you run (`SATELLITES_SEARXNG_URL`), JSON output on | The model's search query goes to your SearXNG, and from there to the search engines it is set to use | 4 s; the top 5 results' titles and snippets, 280 characters each, and any direct answer or infobox. No page is fetched | `SATELLITES_SEARXNG_URL`. Unset, the tool tells the model search is not set up, and the page greys the box |
| `weather` | Open-Meteo, `api.open-meteo.com` and its geocoder `geocoding-api.open-meteo.com` | The home's coordinates, or the place the question names, go to Open-Meteo over the internet, even when the language model is on your own network | 4 s; now and the next three days, in the household's units (`SATELLITES_UNITS`) | Nothing. Open-Meteo's free API is for non-commercial use, and its data is CC BY 4.0 ([THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md)) |

**Home** is `SATELLITES_HOME_LAT`, `SATELLITES_HOME_LON` and
`SATELLITES_HOME_NAME` when set. Otherwise the hub asks Home Assistant's
`GET /api/config`, through the first Home Assistant action a wake word has
and that action's token, and keeps the answer an hour. A failed ask is not
repeated for five minutes. With neither, the weather needs a place named.

The hub runs the calls a model asks for together, sends the results back and
asks again, at most twice. Then it asks with `tool_choice` `none`, so the
model must answer. A tool that fails, times out or finds nothing answers the
model in words, and the turn goes on. With no tool ticked, the request has no
`tools` field at all, so a server without tool calling is never sent one.

**The model and the server must support OpenAI's tool calling.** Ticked tools
go with every question, and a server that cannot take them refuses every
request. On a server of your own that may mean switching tool calling on:
llama.cpp's `--jinja`, vLLM's automatic tool choice, or an Ollama model that
has tools. [ADR 0018](../../docs/adr/0018-language-model-tools.md) records
why these two tools and not more.

#### Language model providers

Any server that answers OpenAI's `POST /chat/completions` works. The
Satellites tab's Provider list fills in these base URLs, and a model is
picked from the ids the server lists at `GET {base_url}/models`, or typed.

Asking for that list sends the key. So the page asks by itself only for an
address and key name a word was saved with. After any change to the
provider, the address or the key, press **List models**.

| Provider | `base_url` | |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | |
| Anthropic | `https://api.anthropic.com/v1` | Its OpenAI compatibility layer. Its `/models` may want its own `x-api-key` header, so the list can be empty: type the id. |
| OpenRouter | `https://openrouter.ai/api/v1` | Ids are `vendor/model`, several hundred of them: type to narrow the list. |
| Groq | `https://api.groq.com/openai/v1` | |
| Mistral | `https://api.mistral.ai/v1` | |
| DeepSeek | `https://api.deepseek.com` | |
| Your own | e.g. `http://llm.example.com:8080/v1` | llama.cpp's server, vLLM, Ollama's `/v1`. Usually no key: set `api_key_env` to `null`, or leave the variable it names unset. The address is the hub's view of the network, so `localhost` is the hub's own container. |

The list shows every id the server returns, including models that cannot
chat (embeddings, speech). The Satellites tab's Test asks the form as it
stands one short question through `POST /satellites/llm/test` and shows how
long the first words and the whole reply took.

### Keys

An action names its key (`api_key_env`, `token_env`) and never holds it. The
hub reads the value on every request, from one of two places, so a change
applies from the next turn with no restart.

1. **The hub's environment**, like any other setting: `OPENAI_API_KEY` in the
   container's secret settings. The environment wins. A value with a line
   break, a space or a character outside printable ASCII in it is not sent:
   a `.env` saved with Windows line endings, or a secret made from a file
   that ends in a newline, leaves one on the end. The turn, the model list
   and Test say which variable to set again, and never what it holds.
2. **A key stored on the hub**, from a language model word's API key box on
   the Satellites tab, or with `PUT /satellites/secrets` and `{"name":
   "OPENAI_API_KEY", "value": "..."}`; `"value": null` clears it. The hub
   keeps it in `secrets.json` in `SATELLITES_DATA_DIR`, mode 0600, beside
   `wake_words.json` and never in it. Clearing the last key removes the file.

No route answers a value, so a stored key is never shown again. `GET
/satellites/wake-words` says in `secrets` which names have a value and where
(`"environment"` or `"hub"`), and warns about a stored key that no action
reads. The name and the value travel in the request body, because the
gateway and voice-ui log paths. The hub refuses a key under a name the
environment already sets, with 409 `set_in_environment`, because it would
never send that value. A 422 says what was wrong and never repeats what was
sent. The log names a key and never its value.

**The data volume's backups hold every stored key.** Keep a key in the
environment instead if that is not acceptable.
[ADR 0015](../../docs/adr/0015-the-hub-may-hold-a-key.md) has the reasoning.

### Lights

Each wake word lights the ring in its own `colour`, or in the listening blue
(40, 110, 255) when it has none:

| While | The ring |
|---|---|
| The satellite listens for the command | Breathes, with a brighter arc gliding towards the talker. The board draws this `listen` mode itself, from the direction the hub sends when it moves by half an LED. Firmware without `listen` in `caps.light_modes` gets a pulse instead |
| The hub transcribes and asks the destination | Spins |
| The reply plays | Off |
| A conversation waits for its next turn | Breathes again, with no arc until someone speaks |
| A trigger word with `feedback: "earcon"` fires | Flashes, where no conversation holds the ring |

LED 0 is assumed to sit towards microphone 1, with the LEDs counting the same
way round as the microphones. Neither has been checked on a board, so the arc
may be rotated or mirrored.

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

`buttons` in a satellite's config maps each button (`rec`, `mode`, `play`,
`set`, `vol_down`, `vol_up`, and `key1`, the side button) and edge to an
action. No button is special; the default is what they did before there was a
choice:

```json
{"rec": {"press": "mute"}, "vol_up": {"press": "volume_up"}, "vol_down": {"press": "volume_down"},
 "play": {"press": "ptt"}, "set": {"press": "stop"}}
```

| Action | Runs on | What happens |
|---|---|---|
| `mute` | the satellite | Toggles the privacy mute. Going on, it is a kill switch: the satellite's conversation ends (and any other one answering or ducked on it), its audio stops, the duck lifts and the hub's lights go out, so one press stops a satellite that is stuck |
| `volume_up`, `volume_down` | the satellite | Volume up or down one of 12 steps (step k is k × 100 / 12 %, about 4 dB apart), and the ring shows the level for 1.5 s |
| `lights` | the satellite | Night mode: the ring off, or back on |
| `dimmer`, `brighter` | the satellite | Brightness down or up a step (10, 20, 35, 60, 100 %) |
| `ptt` | the hub | Push-to-talk: listen as if a wake word had been heard, and do what the `ptt` entry in `wake_words.json` says |
| `stop` | the hub | What `POST /satellites/{id}/flush` does |
| `webhook:<url>` | the hub | POST `{"satellite", "satellite_id", "button", "action", "held_ms"}` to the URL, 10 s at most, no redirects followed |
| `none` | | Nothing |

**The satellite's own actions run on it**, sent as `button_actions` in the
welcome and in each `config` that changes the mapping, to firmware that lists
them in its hello (`caps.actions`). They work with the hub down, and only a
button, never the hub, can undo the mute. So a mapping must keep `mute` on at
least one button: the hub refuses one without, and the firmware keeps Rec as
the mute if it is ever sent one anyway. Holding Set 5 s (Wi-Fi setup) and Mode
10 s (factory reset) are recovery, whatever those buttons are mapped to.

A PATCH replaces the whole mapping. Every press and release is published as
an event, whatever it maps to.

**What a button changed is the hub's too.** A button can change the volume,
`lights_enabled` or `brightness` on the satellite. The satellite then sends a
status marked `"cause": "button"`, and the hub takes those settings from it:

- the page and Home Assistant show them;
- a `settings` event says what changed ([Events](#events));
- the next welcome does not put the old values back.

The hub does not take these settings from any other status. A status already
on its way when the page changed a setting carries the old value. Firmware
from before 2026-09-27 does not mark the status, so for it the status within
2 s of a volume press counts. `local_volume_buttons` is read only by that
firmware, and is turned into the volume pair's mapping once.

### Speaker or jack

The Korvo's 3.5 mm jack (headphones, or an aux cable to another amplifier)
switches in hardware, and no GPIO reads its detect pin. With no plug, the codec's output runs through the jack's
normally-closed contacts to the speaker amplifier and to the loopback (ES7210
channel 0). A plug opens those contacts, so the loopback hears nothing
([schematic](https://dl.espressif.com/dl/schematics/ESP32-KORVO_V1.1_schematics.pdf),
sheet 4). After each sound the hub plays (a reply, a tone, an earcon), it
compares the loopback over that sound with its idle level (`app/output.py`):

| Loopback over the sound | Output |
|---|---|
| 10 dB or more above idle | `speaker` |
| 3 dB or less above idle, for a sound louder than −45 dBFS at volume 10 or more | `jack` |
| Between, or a quiet sound, or too little loopback to judge | unchanged |

So it is known only once something has played since the satellite connected,
and a plug put in or pulled out in silence is seen at the next sound. A change
publishes an `output` event. Nothing needs a firmware change.

With a plug in, the echo canceller has no reference, so speaking over a reply
does not stop it ([Barge-in](#barge-in)).

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
  "thanks" and a few more, with filler around them ("ok, thanks"). "Thanks,
  and what about tomorrow?" carries on. The hub plays `done` and asks the
  assistant nothing. Each language Kokoro speaks has its own ("obrigado,
  tchau", "gracias", "merci, c'est tout", "basta così"); a conversation ends
  on English's and on those of `SATELLITES_LANGUAGES`, of the word's
  `language` hint, of the language the conversation is in so far and of the
  one the goodbye is said in. A word's `end_phrases` replaces them;
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

The hub reads the language of every utterance from its transcript. Parakeet,
stt-stack's default engine, recognises 25 European languages by itself,
refuses a `language` field with a 400, and does not say which language it
heard. A word's `language` hint replaces the detection.

The language decides three things. Home Assistant is sent it. An `llm` is
told to answer in it. The reply voice speaks it:

| Spoken | Voice | Answer in |
|---|---|---|
| English | `SATELLITES_TTS_VOICE` (`bm_george`) | English |
| Portuguese | `pf_dora` | Portuguese: `pt-BR`, unless `SATELLITES_LANGUAGES` names another region |
| Spanish | `ef_dora` | Spanish |
| French | `ff_siwis` | French |
| Italian | `if_sara` | Italian |
| Any other Parakeet language (German, Polish, ...) | the voice of the household's main language, else `SATELLITES_TTS_VOICE` | the household's main language when Kokoro speaks it, else English; an `llm` is told why |

A word's `voice` overrides the table. In a conversation the language follows
each utterance, and a short one ("ok", "sim") keeps the language already in
use: another language has to reach a probability of 0.3 and beat it by 0.1.
The first utterance has no language in use yet, so it is weighed against the
household's main language.

`SATELLITES_LANGUAGES` names the household's languages as BCP 47 tags, most
spoken first; unset, it is `en`. The first is the main language above. Each
tag's region is the one its language is sent on in: with `en-GB,pt-PT`,
detected English goes to Home Assistant as `en-GB` and Portuguese as `pt-PT`,
so a command is matched against Home Assistant's European Portuguese
sentences, not its Brazilian ones. Kokoro's only Portuguese voice is
Brazilian whatever the tag, which is why `pt-BR` is the default.

The detector is py3langid 0.4.0, restricted to Parakeet's languages. On 96
short commands and questions in eight languages it got 94 right, against 90
for lingua-language-detector 2.2.0, and it installs in 4.4 MB rather than 295.
It costs about 50 MB of memory. The two misses were "liga a luz" (Romanian)
and "e a Roma?" (English, the prior). `app/language.py` has the rest.

### Speech-to-text

The hub asks stt-stack's `/health` once a start, before its first
transcription, for the engines it serves and what each takes
([stt-stack](../stt/README.md#what-each-engine-can-do)). Then, for each
command:

- **The engine.** A word whose `language` hint is the one language an engine
  was loaded for goes to that engine by its id. With
  `STT_MODELS=parakeet,parakeet-pt-br`, a word with a Portuguese hint (`pt`,
  `pt-BR`) is heard by the Brazilian Portuguese fine-tune. Every other command goes to the
  stack's default engine. To hear every command on the fine-tune, list it
  first in `STT_MODELS`, which makes it the default for every other client
  too. It transcribes English badly (a word error rate of 0.89), so do that
  only in a household that speaks no English to it.
- **The hint.** A word's `language` hint is sent only to an engine that takes
  one: Whisper gets it; Parakeet never does.
- **Home Assistant's names.** The Calliope integration for Home Assistant
  keeps the `home-assistant` glossary profile on the stack
  ([`clients/home-assistant`](../../clients/home-assistant/README.md#vocabulary)).
  The hub names it on every transcription to an engine that boosts
  (Parakeet), with `boost=true` unless the stack has biasing off (`hotwords`
  false in its `/health`). Whisper is not sent it: it takes a glossary's terms
  as hotwords, and stt-stack measured terms absent from the audio raising its
  word error rate by 28%.

The vocabulary never costs a transcription:

- While the stack's `/health` does not list the profile, which is a hub
  without the integration, the hub does not name it, and looks again every
  10 minutes.
- If the stack refuses the `boost`, the hub sends the names again without it,
  so the repairs still apply, and leaves the boost off for 10 minutes. The
  log has a WARNING that begins `routing: stt-stack refused to boost`.
- If the stack refuses the profile with any other 400, the hub transcribes
  again without it, leaves it out for 10 minutes, and logs a WARNING that
  begins `routing: stt-stack refused the home-assistant vocabulary`.

[ADR 0017](../../docs/adr/0017-home-assistant-vocabulary.md) records the
profile as a contract between the integration and the hub.

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

**It also needs the loopback to hear the reply.** With a plug in the jack it
never does ([Speaker or jack](#speaker-or-jack)), so speaking over a reply
does nothing, and only the wake word or the stop button interrupts it. Before
this rule, the microphones heard the reply from the speakers on the aux
cable, took it for the talker, and stopped it. A follow-up after such a reply
starts listening 0.6 s after the reply ends, instead of waiting for a quiet
loopback.

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
Assistant on the LAN is the main target. Whoever can save a wake word can
send the credentials an action names to an address of their choosing, so the
trust boundary is who holds a key (behind the gateway, every client key). The
hub's own settings are never sent ([Wake words](#wake-words)). Redirects are
not followed, over HTTP or over Home Assistant's websocket. Every external call
has a hard time limit: STT 30 s, TTS 30 s, the destination its own `timeout`.

## Home Assistant over MQTT

With `SATELLITES_MQTT_URL` set, every adopted satellite is one Home Assistant
device, by discovery: Wi-Fi signal, online, microphone, speaker and lights
switches, volume, audio output (speaker or jack, unknown until something
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
  https://calliope.example.com/satellites/kitchen/inject | jq
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
| `SATELLITES_DATA_DIR` | `/data` | Where the hub keeps its state ([The data volume](#the-data-volume)). Mount a volume: losing it un-adopts every satellite. |
| `SATELLITES_TTS_URL` | unset | tts-stack's base URL, for `say` and every reply. Unset, `say` answers 503 and names this variable. |
| `SATELLITES_TTS_VOICE` | `bm_george` | |
| `SATELLITES_SEARXNG_URL` | unset | A SearXNG with JSON output on (`search.formats: [html, json]`), for the `web_search` tool. Unset, the tool tells the model search is not set up. SafeSearch is the instance's own setting. |
| `SATELLITES_HOME_LAT`, `SATELLITES_HOME_LON`, `SATELLITES_HOME_NAME` | unset | Home, for the `weather` tool. Unset, Home Assistant's own location is used (`GET /api/config`, through the first Home Assistant action and its token, kept an hour; a failed ask is not repeated for five minutes). |
| `SATELLITES_TIMEZONE` | Home Assistant's, else `TZ`, else UTC | The zone of the date and time every language model prompt carries, with tools or without. |
| `SATELLITES_UNITS` | Home Assistant's unit system, else `metric` | `us` or `metric`: the `weather` tool's °F, mph and inches, or °C, km/h and mm. |
| `SATELLITES_LANGUAGES` | `en` | The household's languages as BCP 47 tags, most spoken first: `fr`, `pt-PT`, `en,pt-BR`. The first is what a new conversation is expected to be in and what an answer falls back to; each tag's region is how its language is sent on ([Language](#language)). A tag for a language the recogniser does not hear is left out, with a warning at start. |
| `SATELLITES_STT_URL` | unset | stt-stack's base URL. Unset, wake words are still heard and published, and routing says there is no STT. |
| `SATELLITES_WAKE_WORDS` | `hey_jarvis:0.5` | Read once: the words `wake_words.json` starts with, each for every satellite, on the first start with a volume that has none. Names and thresholds, comma-separated. A name alone gets 0.5. Empty means none; push-to-talk still works. After that, [Wake words](#wake-words). |
| `SATELLITES_MODEL_DIR` | `$SATELLITES_DATA_DIR/models` | Where wake word models live. The image's own are copied here at start; other built-in names (`alexa`, `hey_mycroft`, `hey_rhasspy`, `weather`) are fetched here once, when a word first names them, and `<name>.onnx` of your own loads by its name ([`tools/wakeword-train`](../../tools/wakeword-train/README.md) trains one). |
| `SATELLITES_FRONTEND` | `1` | `0` skips echo cancellation, beamforming and noise suppression. |
| `SATELLITES_DEBUG_AUDIO` | `0` | `1` keeps the last ten commands' surroundings under `$SATELLITES_DATA_DIR/debug`: 16 s of processed output, the first raw microphone, and the command, as WAV. It records the room; switch it on to find out why a command came back empty, then off. |
| `SATELLITES_MQTT_URL` | unset | `mqtt://user:pass@host:1883` or `mqtts://...` (the system CA store, or `SSL_CERT_FILE`). Unset, no MQTT. |
| `SATELLITES_MQTT_PREFIX` | `homeassistant` | Home Assistant's discovery prefix |
| `SATELLITES_MQTT_BASE` | `calliope/satellites` | The hub's own topics; two hubs on one broker need two |
| `SATELLITES_HA_TOKEN` | unset | The default `token_env` of the `ha_conversation` and `ha_assist` destinations: a long-lived access token |
| `SATELLITES_LLM_API_KEY` | unset | The default `api_key_env` of an `llm` destination. A key can also be stored under this name from the Satellites tab ([Keys](#keys)); set here, it wins. With neither, no `Authorization` is sent. |
| `SATELLITES_FIRMWARE_PUBKEY` | unset | A PEM public key, or a path to one. Set, uploads must be signed by it. A bad value stops the service at start. |
| `SATELLITES_API_KEYS` | unset | As on the other backends. Behind the gateway it stays unset. |
| `SATELLITES_LOG_LEVEL` | `INFO` | Transcripts and replies are logged only at `DEBUG`. |

In pre-release builds before 2026-09-25 every one of these was `NODES_*`. The old names are not
read. At start the hub logs a warning for each `NODES_*` variable still set,
with the name it reads now, except one that a wake word's action names: an
action carried over from a rule saved before the rename says `"token_env":
"NODES_HA_TOKEN"` and reads exactly that
([ADR 0013](../../docs/adr/0013-satellites-one-door.md#renamed)).

### The data volume

| In `SATELLITES_DATA_DIR` | What it holds |
|---|---|
| `satellites.json` | Each adopted satellite: its name, model and config, and its token's SHA-256 only |
| `wake_words.json` | The wake words and push-to-talk, each with its action ([Wake words](#wake-words)) |
| `secrets.json` | The API keys stored from the Satellites tab, mode 0600, in plain text ([Keys](#keys)) |
| `firmware/` | Uploaded firmware images |
| `models/` | Wake word models, fetched and uploaded (`SATELLITES_MODEL_DIR`) |
| `debug/` | With `SATELLITES_DEBUG_AUDIO=1`, the last ten commands' audio |
| `rules.json` | Routing from before 2026-09-25. Read once into `wake_words.json`, then left where it is |
| `nodes.json` | A pre-release build's satellites. Read once into `satellites.json`, then left where it is |

**Backups of the volume hold every stored key.** Keep a key in the
environment instead if that is not acceptable.

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

The device is played by Starlette's test socket, and STT, TTS, webhooks,
language model servers and Home Assistant by `httpx.MockTransport` on `*.test`
hosts, which never resolve.
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
- Barge-in by voice without the front-end, on a reply played on another
  satellite, or with a plug in the jack. Only the wake word and the stop
  button interrupt those.
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
