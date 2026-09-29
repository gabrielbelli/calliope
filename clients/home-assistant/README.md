# Calliope for Home Assistant

A custom integration that brings a Calliope stack into Home Assistant. It does
three things:

- **Satellites become devices.** Each adopted satellite gets switches for its
  microphone, speaker and lights, a volume slider, an identify button, and
  sensors for its Wi-Fi signal, the last command and the last wake word.
- **What a satellite hears becomes a trigger.** Wake words, trigger words,
  button presses and transcribed commands appear in the automation editor as
  device triggers. All logic stays in Home Assistant.
- **Calliope becomes a speech engine for Assist.** Each speech-to-text engine
  the stack serves, and Kokoro for text-to-speech, can be chosen in any
  Assist pipeline.

It talks to the gateway only (`https://host:30080`). It holds one long-lived
connection to the hub's event stream and polls nothing.

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
| API key | Only if the gateway sets `GATEWAY_API_KEYS`. Leave it empty for a keyless gateway |
| Verify the TLS certificate | On. Turn it off only for a self-signed certificate |

The flow checks `GET /health`, which never needs a key, and then
`GET /v1/models`, which the gateway answers itself behind the key. *Configure*
on the integration changes the same three fields later, and the entry reloads.
If the gateway starts refusing the key, Home Assistant asks for a new one.

A gateway deployed without the satellite hub still works: speech-to-text and
text-to-speech are there, and there are no satellites.

## What each satellite gets

Only adopted satellites appear. A satellite waiting on the Satellites tab does
not. One adopted later appears without a restart, and one forgotten on the hub
leaves Home Assistant.

| Entity | What it shows or does |
|---|---|
| Online | Connectivity. On while the satellite holds its socket to the hub |
| Wi-Fi signal | RSSI in dBm, from the satellite's status every 10 s. Diagnostic |
| Microphone, Speaker, Lights | Switches. Each is one `PATCH /satellites/{id}` |
| Volume | 0 to 100, also a `PATCH` |
| Identify | Blinks the ring for five seconds |
| Voice | An event entity. See below |
| Last command | The last transcript. The full text, the reply and any error are attributes |
| Last wake word | The last wake word or trigger word heard, with its score |

The switches and the slider show the **hub's** record of the satellite, as the
Satellites tab does. A setting changed with the satellite's own buttons
(volume, night mode, brightness) is written back to that record, so it shows
here too.

While a satellite is offline, the switches and the slider still take changes:
the hub keeps them and sends them at the next connect. Wi-Fi signal and
Identify are unavailable. While the event stream itself is down, every entity
is unavailable, because Home Assistant cannot know their state.

The **Voice** event entity fires one of these event types. The word, button or
transcript is in the event's attributes.

| Event type | From the hub's event | Attributes |
|---|---|---|
| `wake_word` | `wake` | `wake_word`, `score`, `direction` |
| `trigger_word` | `triggered` | `wake_word`, `score` |
| `command` | `routed` or `turn` with a transcript | `transcript`, `wake_word`, `reply_text`, `rule_id` |
| `button_press`, `button_release` | `button` | `button`, `held_ms` |
| `conversation_started`, `conversation_ended` | the same names | as the hub sends them |

Names and translations are in English and Brazilian Portuguese.

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

The words offered are the ones assigned to that satellite on the hub. A hub
that says each word's mode lists a trigger word only as a trigger word, and any
other word only as a wake word. A hub that does not say lists every word both
ways.

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
on the bus as `calliope_event`, except `status`, which arrives every 10 s. Its data is the
hub's event as sent, plus `device_id`, `satellite_name` and `kind`. `kind` is
one of the Voice event types above, or the hub's own type for anything else
(`online`, `offline`, `ota`).

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

`calliope.push_to_talk` starts listening on the satellite as if its PLAY
button had been pressed, so the next thing said is the command. Without
`wake_word`, the hub's push-to-talk entry decides what happens to it. With
`wake_word`, that word's mode and action apply, so a conversation word starts
a conversation. The hub refuses, and the action shows its reason, when:

| Reason | Why |
|---|---|
| `satellite_busy` | The satellite is in a conversation already |
| `satellite_muted` | Its privacy mute is on. Only a button on the device turns it off |
| `mic_disabled` | Its microphone switch is off |
| `trigger_word` | The word named is a trigger word, so there is nothing to listen for after it |
| `wake_word_not_found` | No wake word of that name has an action |

A message that the hub has no push-to-talk route appears only against a hub
or gateway from before 2026-09-25, when the route was added.

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
127.0.0.1 with the routes and event shapes of the real services. They cover
the config, options and reauth flows, the event stream and its reconnects,
entity states from events, device triggers firing real automations, the STT
and TTS engines through Home Assistant's own components, a whole Assist
pipeline with both engines, the actions, and diagnostics.

## What is not here

- An Assist satellite entity. The hub runs its own wake words, endpointing and
  routing, so a satellite is not an Assist satellite in Home Assistant's sense.
- A satellite renamed on the hub keeps its old name in Home Assistant until it
  is renamed there too.

## Licence

BSD 2-Clause, as the rest of the repository.
