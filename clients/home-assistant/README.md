# Calliope for Home Assistant

A custom integration that brings a Calliope stack into Home Assistant. It does
four things:

- **Satellites become devices.** Each adopted satellite gets the entities for
  the hardware it has, and only those: a Korvo gets its ring and its buttons,
  a Raspberry Pi its sound cards and its AirPlay receiver.
- **A satellite with a speaker becomes a media player.** It plays what Home
  Assistant plays (`play_media`, `tts.speak`, announcements), shows what a
  phone plays on it through AirPlay, and is the satellite's volume control.
- **What a satellite hears becomes a trigger.** Wake words, trigger words,
  button presses and transcribed commands appear in the automation editor as
  device triggers. All logic stays in Home Assistant.
- **Calliope becomes a speech engine for Assist.** Each speech-to-text engine
  the stack serves, and Kokoro for text-to-speech, can be chosen in any
  Assist pipeline.

It talks to the gateway only, with an API key made in Calliope. It holds one
long-lived connection to the hub's event stream, and reads a satellite again
only when an event says its record changed. The one thing it polls is
`GET /health`, every 5 minutes (every 30 seconds while the stack loads its
models), to see which speech-to-text engines the stack serves.

Built and tested against Home Assistant **2026.8.1**.

## Install

The integration is the folder `custom_components/calliope`.

**By hand.** Copy the folder into the `custom_components` folder of your
Home Assistant configuration, then restart Home Assistant. For Home Assistant
Container that is `/config/custom_components/calliope` inside the container.

```bash
docker cp custom_components/calliope homeassistant:/config/custom_components/
docker restart homeassistant
```

**With HACS.** HACS reads a repository from its root, so it cannot install from
this monorepo directly. This directory is laid out as a HACS repository root
(`hacs.json` beside `custom_components/`), so it works once it is published as
a repository of its own, for example with:

```bash
git subtree split --prefix clients/home-assistant -b home-assistant
```

Push that branch to its own repository, then in HACS open *Integrations* →
*Custom repositories* and add it as an *Integration*.

## Set up

In Home Assistant open *Settings* → *Devices & services* → *Add integration*
and choose **Calliope**.

| Field | |
|---|---|
| URL | The gateway: the address that serves both the API and the page, e.g. `https://calliope.example.com`, or a proxy in front of it |
| API key | **Required.** A key made in Calliope with the `home-assistant` preset (below) |
| Verify the TLS certificate | On. Turn it off only for a self-signed certificate |

The flow checks the key's shape first: a Calliope key starts with `calliope_`
and is 45 characters long, ending in a checksum, so a key pasted short or with
a typo is refused before the gateway is asked. Then it asks the gateway:

| Request | Refused with | The form says |
|---|---|---|
| `GET /health` | no answer, or not Calliope's | Calliope did not answer, or it is not a Calliope gateway |
| `GET /v1/models` | 401 | Calliope refused the key: unknown, revoked or expired |
| `GET /health` | an answer without the backends | the key lacks `health:read` (`/health` never answers 403) |
| `GET /v1/models`, `GET /voices`, `POST /v1/audio/transcriptions` (with no audio), `GET /glossaries/home-assistant` | 403 | the key lacks the scopes it names (from the `WWW-Authenticate` challenge) |
| `GET /satellites` | 403 | a warning: the entry is saved, speech works, and the satellites stay out of reach |

One answer names every scope the key lacks for speech, so one new key fixes
it. Only scope names with Calliope's grammar are shown: a challenge that names
none says only that the key lacks a scope of the preset.

*Reconfigure* on the integration's menu changes the same three fields later,
and the entry reloads. Moving the entry to a gateway that is set up already is
refused.

A gateway deployed without the satellite hub still works: speech-to-text and
text-to-speech are there, and there are no satellites.

### The API key

In Calliope open *Account* → *API keys* → *New key*, choose the preset
**`home-assistant`**, and copy the key: Calliope shows it once. The preset
holds exactly what the integration uses:

| Scope | For |
|---|---|
| `models:read` | the key check (`GET /v1/models`) |
| `health:read` | the speech-to-text engines in `GET /health`; without it `/health` answers only whether the stack is up |
| `speech:transcribe` | speech-to-text |
| `speech:speak` | text-to-speech and the voice list |
| `glossaries:ha` | writing the `home-assistant` vocabulary, and naming it when transcribing |
| `satellites:read` | the satellites, their events, wake words and AirPlay covers |
| `satellites:control` | switches, numbers, selects, the media player, announcements and the actions |
| `satellites:update` | firmware updates |

The satellite scopes are not in the speech role, so only an account with the
admin role can make this key, and Calliope asks for the password again first.
The key may live up to 365 days; because it can push firmware, it cannot be
made to never expire. Calliope never shows a key
again; to replace one, make a new key and enter it with *Reconfigure*.

While the integration runs:

- **401** (the key was revoked, has expired, or its account was disabled):
  Home Assistant shows *Reauthentication required* and asks for a new key.
- **403** (the key lacks a scope a route needs): a repair issue names the
  scopes and points to the `home-assistant` preset. One issue per entry lists
  every scope refused; it is a warning when only satellite scopes are missing
  and an error otherwise, and it goes when the entry is set up again with a
  new key. A key that cannot reach the satellites at setup still loads speech;
  one that lacks `models:read`, `health:read` or `speech:speak` fails setup
  until it is replaced, because retrying cannot give a key a scope.

## What each satellite gets

Only adopted satellites appear. A satellite waiting on the Satellites tab does
not. One adopted later appears without a restart, and one forgotten on the hub
leaves Home Assistant.

The satellite's caps decide its entities: what it says it has when it
connects (`hello.caps`), which the hub keeps while it is offline. The model
name decides nothing.

| Entity | When | Shows or does |
|---|---|---|
| Online | always | Connectivity: on while the satellite holds its socket to the hub. Diagnostic |
| Wi-Fi signal | always | RSSI in dBm, from its status every 10 s. Diagnostic |
| Uptime | always | When it last started. Diagnostic, off by default |
| Restart | always | `POST /satellites/{id}/reboot`. Off by default |
| Firmware | always | The firmware it runs and the update the hub would send it. See [Firmware updates](#firmware-updates) |
| The media player | a speaker | See [Media player](#media-player) |
| Speaker | a speaker | Switch |
| Plays through | a speaker | Its own speaker, or another satellite's (`output_satellite`) |
| Identify | a speaker or a ring | Blinks the ring for five seconds, or, without a ring, plays the wake chime three times |
| Microphone | a microphone | Switch |
| Microphone gain | a microphone | 0 dB up to what the satellite does with it: 3.5 dB on a Pi, 37.5 dB on a Korvo |
| Voice | a microphone | An event entity. See below |
| Last command | a microphone | The last transcript. The full text, the reply and any error are attributes |
| Last wake word | a microphone | The last wake word or trigger word heard, with its score |
| The Assist satellite | a microphone and a speaker | See [Announcements](#announcements) |
| Lights, Ring brightness | a ring | Switch, and 1 to 100 % |
| Privacy mute | a mute button | On while the board's own button has cut its microphone. Only that button turns it off |
| *Button* button | each of its buttons | An event entity per button (`press`, `release`, with `held_ms`). Off by default |
| Output, Microphone input | sound cards (a Pi) | The system default, or a card by its description |
| Echo reference | sound cards and a microphone | Switch. Off by default |
| AirPlay, AirPlay name | an AirPlay receiver (a Pi) | The receiver on or off, and the name phones show (empty: the satellite's name) |
| CPU temperature, Under-voltage | a Pi's health | Diagnostic |

For the live boards that is 25 entities on a Korvo, 9 of them off by default,
and 22 on a Pi with a USB microphone, with no lights, buttons or privacy mute.

**Caps that are not known yet.** A satellite the hub has never seen connected
(or a hub from before saved caps, while the satellite is offline) has empty
caps. It gets only the entities in the *always* rows, no device triggers for
hardware, and nothing it had before is removed. Unknown is not taken for
absent: guessing another model's hardware is what gave a Pi the Korvo's
buttons.

**Caps that change** add and remove entities at once. A Pi whose USB
microphone is plugged in gets its microphone entities; unplugged, they leave
Home Assistant, customisations and all.

The switches, selects and numbers show the **hub's** record of the satellite,
as the Satellites tab does, and each change is one `PATCH /satellites/{id}`.
A setting changed on the page, or by the satellite itself (a button, a
phone's AirPlay volume), is published by the hub, and the record is read
again at once. A setting the hub has never stored (a Pi's AirPlay, which
starts on) shows what the satellite reports.

While a satellite is offline, its settings still take changes: the hub keeps
them and sends them at the next connect. Wi-Fi signal, Identify and the media
player are unavailable. While the event stream itself is down, every entity
is unavailable, because Home Assistant cannot know their state. The gateway
ends the stream every 15 minutes so that the key is checked again; that is
routine, and the integration opens a new one at once without the entities
going unavailable. They do only if the new stream is refused.

The device follows the hub too: a rename on the Satellites page, a new
firmware after an update, the maker (Espressif for a Korvo, Raspberry Pi for
a Pi), and a link to the Satellites page.

The **Voice** event entity fires one of these event types. The word or
transcript is in the event's attributes.

| Event type | From the hub's event | Attributes |
|---|---|---|
| `wake_word` | `wake` | `wake_word`, `score`, `direction` |
| `trigger_word` | `triggered` | `wake_word`, `score` |
| `command` | `routed` or `turn` with a transcript | `transcript`, `wake_word`, `reply_text`, `rule_id` |
| `conversation_started`, `conversation_ended` | the same names | as the hub sends them |

A button press is not something the satellite heard: each button has its own
event entity, and a device trigger.

Names and translations are in English and Brazilian Portuguese.

## Media player

Each satellite with a speaker is a media player named after the satellite.

**Volume.** The media player is the satellite's one volume control
(`volume_set`, `volume_up` and `volume_down` in steps of 5, and Assist's "set
the volume to…"). It is the volume of the satellite that actually plays: a
Korvo whose *Plays through* is a Pi shows and sets the Pi's volume, as its own
does nothing meanwhile. On a Pi there is one volume, its output's, so a phone
moving its AirPlay slider moves the media player too.

**AirPlay.** While a phone plays on a Pi, the media player shows it: playing
or paused, title, artist, album, duration and position, *AirPlay* as the app,
and the cover. The cover comes from the hub with the integration's own key,
through Home Assistant's image proxy, so the browser never needs to reach
the hub. Play, Pause, Next, Previous and Stop appear only while the phone
takes them from the receiver (`status.airplay.remote.controls`); the phone
decides, per app. See [AirPlay](#airplay).

**Playing from Home Assistant.** `media_player.play_media` plays anything
Home Assistant can play: a file from *My media*, an http or https URL, a
radio stream, text to speech. *Browse media* offers Home Assistant's media,
audio only. A `file:` URL is refused: any user may call `play_media`, and
only *My media* keeps to the folders it serves. Home Assistant resolves the
media and converts it with its own ffmpeg to exactly the WAV the hub names
for that satellite (44.1 kHz stereo for a Pi's media lane, the speaker's
rate in mono for a Korvo), and uploads it to the hub as ffmpeg writes it.
The hub never decodes. The action returns at once; the player shows
*Calliope* as the app, and the file's name as the title, until the hub says
the stream ended. A new `play_media` replaces what plays; an announcement
does not (see [Announcements](#announcements)).

**Stop** ends Home Assistant's stream and asks the hub to drop what the
satellite has buffered, or, while a phone plays, asks the phone to stop. The
Satellites page's Stop button stops both too. Pausing and seeking a stream
from Home Assistant are not offered.

On a Korvo, music gives way to the voice: it pauses while a reply, a Say, a
tone or an announcement plays. A Pi ducks the music under the voice.

For a music library on a Pi, Music Assistant's AirPlay provider can stream
to its AirPlay receiver directly, with metadata and cover.

## Announcements

An announcement plays on the hub's voice lane, over whatever plays: the
music, Home Assistant's own included, pauses on a Korvo or ducks on a Pi,
and goes on afterwards. Announcements come from:

- `tts.speak` with a satellite's media player as the target;
- `media_player.play_media` with `announce: true`;
- `assist_satellite.announce` on a satellite with a microphone and a speaker,
  with its chime first unless `preannounce: false`;
- "broadcast dinner is ready" to Assist, which announces on every Assist
  satellite but the one it was said to (`HassBroadcast`).

The last two return only once the announcement has played, as Home
Assistant expects of an Assist satellite. `tts.speak` and `play_media`
return at once, as for music. The media player goes on showing what it
showed, and its **Stop** leaves an announcement playing.

The Assist satellite entity does announcements only. The hub runs the
satellite's wake words, listening and routing, so Home Assistant never runs a
pipeline for it; its state shows announcements, and a conversation on the
satellite leaves it idle.

**Home Assistant needs an internal URL** for its own media (the chime, text
to speech, *My media*): its ffmpeg fetches them. Set one in *Settings* →
*System* → *Network* if announcements fail with an error about the URL.

## Firmware updates

Each satellite has a Firmware update entity. The hub works out the update:
the newest image uploaded for the satellite's model that it would actually
send, signed where the satellite demands a signature, and newer than what the
satellite runs. Home Assistant offers exactly that image (the versions are
git-describe names, which only the hub orders as the Satellites page does).

*Install* starts the update through the hub. Its progress shows while the
image is sent, then while the satellite restarts, and the entity shows the
new version once the satellite reports it. When the hub will not start it
(the satellite is offline, another update is underway, a signature the
satellite would refuse), the reason is shown. Upload images on the Satellites
page; an upload or a deletion reaches Home Assistant at once.

## AirPlay

A Pi's AirPlay receiver has a switch and a name (empty: the satellite's own
name). While a phone plays, the media player shows it, as above.

The transport controls go to the phone through Shairport Sync. The phone
decides what it takes: some apps take none, and then only the Satellites
page's Disconnect is offered. The phone may also take a command and not act
on it; Home Assistant shows what happens in the satellite's next status.

## Triggers

Open an automation, add a trigger, choose *Device* and then a satellite. The
editor offers:

| Trigger | Fires when |
|---|---|
| Wake word *x* heard | the satellite heard wake word *x* |
| Trigger word *x* heard | the satellite heard trigger word *x* (a word whose mode is `trigger`) |
| Command transcribed | a command after a wake word was transcribed |
| *Button* pressed, *Button* released | a button on the satellite changed state |
| Conversation started, Conversation ended | a conversation began or finished |

Only what the satellite has is offered. Words, commands and conversations
need a microphone; the words are the ones assigned to that satellite on the
hub, a trigger word as a trigger word and any other word as a wake word.
Buttons are the ones the satellite lists in its caps. A satellite whose caps
are not known yet offers no triggers.

Everything the hub sent is trigger data:

```yaml
triggers:
  - trigger: device
    domain: calliope
    device_id: 0123456789abcdef0123456789abcdef
    type: command
actions:
  - action: notify.mobile_app_phone
    data:
      message: "Kitchen heard: {{ trigger.event.data.transcript }}"
```

**For YAML.** The integration also fires every event from an adopted satellite
on the bus as `calliope_event`, except `status`, which arrives every 10 s, and
`config`, which says only which settings changed. Its data is the hub's event
as sent, plus `device_id`, `satellite_name` and `kind`. `kind` is one of the
Voice event types above, `button_press` or `button_release`, or the hub's own
type for anything else (`online`, `offline`, `ota`, `settings`, `media`,
`airplay_command`, and `wake_rejected`, which carries what STT heard when a
wake word's double-check did not hear the word).

```yaml
triggers:
  - trigger: event
    event_type: calliope_event
    event_data:
      kind: trigger_word
      wake_word: lumos
```

An event from a clip run through `POST /satellites/{id}/inject` is marked
`injected` by the hub. The integration drops it, as the MQTT bridge does, so a
test clip cannot fire an automation.

## Actions

Each action takes a target: satellites, any of their entities, an area or a
label.

| Action | Fields | Hub route |
|---|---|---|
| `calliope.say` | `text`, and optionally `voice` (a Kokoro voice such as `pf_dora`) | `POST /satellites/{id}/say` |
| `calliope.tone` | `frequency` (50 to 8000 Hz, default 440), `seconds` (up to 10, default 1) | `POST /satellites/{id}/tone` |
| `calliope.push_to_talk` | optionally `wake_word`, so that word's mode and action apply | `POST /satellites/{id}/ptt` |

```yaml
action: calliope.say
target:
  area_id: kitchen
data:
  text: "O jantar está pronto."
  voice: pf_dora
```

A satellite with its speaker off refuses `say` and `tone`, and the hub's reason
is shown.

`calliope.push_to_talk` starts listening on the satellite as its push-to-talk
button does, so the next thing said is the command. Without `wake_word`, the
hub's push-to-talk entry decides what happens to it. With `wake_word`, that
word's mode and action apply, so a conversation word starts a conversation.
The hub refuses, and the action shows its reason, when:

| Reason | Why |
|---|---|
| `satellite_busy` | The satellite is in a conversation already, or checking a wake word it heard |
| `satellite_muted` | Its privacy mute is on. Only a button on the device turns it off |
| `mic_disabled` | Its microphone switch is off |
| `not_listening` | The hub could not start listening to it (it has no microphone, or its audio could not be set up); the message says why |
| `trigger_word` | The word named is a trigger word, so there is nothing to listen for after it |
| `wake_word_not_found` | No wake word of that name has an action |

The actions' target picker offers satellites, and the media players (`say`,
`tone`) or the Assist satellites and Voice entities (`push_to_talk`) that name
them.

## Calliope in an Assist pipeline

The integration adds the stack's engines to Home Assistant: one
speech-to-text entity for each engine the stack serves (`GET /health`,
`backends.stt.health.models`), and Kokoro for text-to-speech. It checks the
list every 30 seconds while the stack loads its models, then every 5 minutes,
and reloads itself when the list changes, so a model added to the stack
appears in Assist's menu without a restart. Each entity stays with
its engine: a new order in `STT_MODELS`, or a new default, does not move an
entity id to another engine, so an assistant keeps the engine it was set up
with.

| Entity | Engine | Languages |
|---|---|---|
| `stt.calliope_parakeet` | Parakeet TDT 0.6B v3, through `POST /v1/audio/transcriptions` | Parakeet's 25 European languages |
| `stt.calliope_parakeet_pt_br` | Its Brazilian Portuguese fine-tune, when the stack loads it (`STT_MODELS=parakeet,parakeet-pt-br`) | Portuguese |
| `stt.calliope_whisper` | Whisper large-v3, when the stack loads it (`STT_MODEL=whisper`, or `whisper` in `STT_MODELS`) | Whisper's languages |
| `tts.calliope_kokoro` | Kokoro, through `POST /v1/audio/speech` | English (US and UK), Brazilian Portuguese, Spanish, French, Hindi, Italian, Japanese and Mandarin, as far as the stack has voices |

To use them:

1. Open *Settings* → *Voice assistants*, and add or open an assistant.
2. Set *Speech-to-text* to **Calliope Parakeet**.
3. Set *Text-to-speech* to **Calliope Kokoro**, then choose a language and a
   voice.

### Speech-to-text

Each entity sends its engine's id as `model`. Parakeet
detects the language itself and refuses a `language` field, so for a Parakeet
engine the pipeline's language only decides whether Home Assistant offers the
engine, and the integration never sends it. Whisper takes it as a hint.

Parakeet's languages are Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French,
German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish,
Portuguese, Romanian, Russian, Slovak, Slovenian, Spanish, Swedish and
Ukrainian.

### Vocabulary

The integration keeps a glossary profile named `home-assistant` on the stack.
It holds the names of your floors and areas, the names of the entities
exposed to Assist, the aliases of all three, and a short list of command
words ("liga", "apaga", "turn on"…) for the languages your pipelines use:
Portuguese, English, Spanish, French, German, Italian and Dutch. A pipeline in
another language gets the names alone.

**It needs a stack that can store it.** The integration writes the profile
with `PUT /glossaries/home-assistant` through the gateway, with the API key it
was set up with. stt-stack refuses every write with 503 unless a volume is
mounted at `/glossaries`
([Turning the write routes on](../../services/stt/README.md#turning-the-write-routes-on)).
Until a write succeeds, transcriptions go without the profile, and Home
Assistant's log has a warning that begins `Could not write the home-assistant
vocabulary to Calliope`. Diagnostics do not include the profile's state or
its term count.

**The name is reserved.** A profile you made by hand called `home-assistant`
is replaced on the next write. The satellite hub names the same profile when
it transcribes a command itself, so this integration is where the hub's
vocabulary comes from too.
[ADR 0017](../../docs/adr/0017-home-assistant-vocabulary.md) records how the
two share it.

Every transcription on a Parakeet engine names the profile, and Parakeet
boosts the terms in its decoder, unless the stack has biasing off
(`STT_HOTWORDS=0`). The terms only bias recognition: a term never replaces
another word. Whisper is not sent the profile. It takes a profile's terms as
hotwords, and the stack measured terms absent from the audio raising its word
error rate by 28%. If the stack refuses the boost (a name with a character
the model cannot spell), the names and repairs still go, without the boost,
until the vocabulary changes.

For Portuguese the profile also carries repair rules for commands the model
runs together: spoken quickly, "desliga a luz da cama" can come back as
"desliga-los da cama", which is rewritten to the command after decoding.

The profile is written about 10 seconds after Home Assistant starts, and again
10 seconds after an area, floor, device, entity or exposure setting changes.
It is also checked every hour, and written only when it changed. The stack
boosts at most 200 phrases a request, the repairs' corrected phrases among
them. So the profile holds at most 200 terms less those, taken in the order
above, and a house with more names logs which were left out.

### Text-to-speech

The languages and voices come from `GET /voices` at setup.
A Kokoro voice name starts with its language (`pf_dora` is Brazilian
Portuguese). The default voice for English (UK) is `bm_george`, for English
(US) `af_heart`, and for Brazilian Portuguese `pf_dora`.

The integration passes the `speed` option (0.5 to 2.0) on. Kokoro's audio is taken as raw 24 kHz PCM,
so nothing is encoded on the server. A message longer than the route's
4096-character limit is sent in pieces and joined.

## Alongside MQTT

The hub can also publish satellites to Home Assistant over MQTT
(`SATELLITES_MQTT_URL`). With both on, each satellite appears twice, once per
integration. Use one of them. This integration adds triggers, actions and the
Assist engines. MQTT needs no custom integration.
[ADR 0020](../../docs/adr/0020-home-assistant-integration-beside-mqtt.md)
records why there are both.

## Assist commands from a satellite

A satellite's spoken command reaches an Assist pipeline through the hub's own
`ha_assist` action, not through this integration. The hub calls Home
Assistant's `assist_pipeline/run` over the websocket with a long-lived token,
and sends the satellite's Home Assistant device as `device_id`. Home
Assistant then knows the satellite's area, so "turn on the lights" means the
lights in that room. This integration is what registers the device, so
without it the command still runs, with no room.

## Tests

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component` 0.13.355,
which pins `homeassistant==2026.8.1`, and the requirements of the core
components the tests load. The tests run a fake Calliope gateway on
127.0.0.1 with the routes and event shapes of the real services, and a Korvo
and a Pi with their live caps. The fake holds a key as the gateway does: 401
without it, and 403 for a route whose scope it lacks. Its key starts with the
`home-assistant` preset, held to the real one in `packages/common`, so the
suite passing means the integration needs nothing outside that preset. The
tests cover the key's local check against the gateway's own key format; the
config, reconfigure and reauth flows (the reconfigure flow replaced the
options flow in 0.2) and the entry migration; 401 and 403 at setup and at
runtime, and the repair issue; the event
stream and its reconnects, the entities each satellite's caps give it and the
registry pruning when they change, the media player, announcements and
firmware updates, device triggers firing real automations, the STT and TTS
engines through Home Assistant's own components, a whole Assist pipeline with
both engines, the actions, and diagnostics. No test runs ffmpeg on the
integration's behalf, and nothing plays.

## Upgrading from 0.2

- **A key is required.** Calliope no longer answers anything but liveness
  without one. An entry saved without a key asks for one as soon as Home
  Assistant starts (*Reauthentication required*): make a `home-assistant` key
  ([The API key](#the-api-key)) and paste it. Until then Assist has no
  Calliope speech; the satellites themselves keep working.
- The entry's version goes from 1 to 2. Going back to 0.2 cannot load it.
- The vocabulary needs `glossaries:ha`, which the preset holds. The
  reauthentication refuses a key without it; a key that loses it later gets a
  repair issue, and transcriptions go without the profile.

## Upgrading from 0.1

- `number.*_volume` is gone: the media player is the volume control, and it
  sets the volume of the satellite that actually plays.
- The Lights switch is removed from satellites without a ring.
- The button event types moved from the Voice entity to one event entity per
  button, off by default. An automation on the Voice entity's
  `button_press` should use the button's device trigger or its event entity.
- Button triggers appear only for the buttons a satellite has: a Pi offers
  none.
- An entity removed this way loses its customisations (name, area, icon).
  Every entity that stays keeps its unique id and entity id.
- *Configure* is now *Reconfigure*, and the entry follows the gateway's host.

## What is not here

- Pausing, resuming or seeking a stream from Home Assistant: Stop only.
- A speaker-only mute. The Korvo's only mute is the privacy mute, which cuts
  the microphone.
- Turning the media player on and off: the Speaker switch does that.
- Grouping, queues, shuffle and repeat. *Plays through* sends one satellite's
  audio to another's speaker; it is not synchronised multi-room.
- Starting a conversation or asking a question from Home Assistant, and
  wake word settings in Home Assistant's satellite dialog: the hub owns
  listening.
- Assist timers on a satellite.
- The ring as a light entity, and a select per button action: the Satellites
  page's guided ring and button set-up does these.

## Licence

BSD 2-Clause, as the rest of the repository.
