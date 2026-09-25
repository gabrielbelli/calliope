# Calliope for Home Assistant

A custom integration that brings a Calliope stack into Home Assistant. It does
three things:

- **Satellites become devices.** Each adopted satellite gets switches for its
  microphone, speaker and lights, a volume slider, an identify button, and
  sensors for its Wi-Fi signal, the last command and the last wake word.
- **What a satellite hears becomes a trigger.** Wake words, trigger words,
  button presses and transcribed commands appear in the automation editor as
  device triggers. All logic stays in Home Assistant.
- **Calliope becomes a speech engine for Assist.** Parakeet (speech-to-text)
  and Kokoro (text-to-speech) can be chosen in any Assist pipeline.

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
| URL | The gateway, `https://orko.gabrielbelli.com:30080` by default |
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
Satellites tab does. A volume changed with the satellite's own VOL buttons is
not written back to that record, so it does not show here either.

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
is shown. **`push_to_talk` fails today** with a message saying the hub has no
push-to-talk route yet. The hub starts listening from its PLAY button
internally, but has no HTTP route for it.

## Calliope in an Assist pipeline

The integration adds two engines to Home Assistant.

| Entity | Engine | Languages |
|---|---|---|
| `stt.calliope_parakeet` | Parakeet TDT 0.6B v3, through `POST /v1/audio/transcriptions` | Parakeet's 25 European languages |
| `tts.calliope_kokoro` | Kokoro, through `POST /v1/audio/speech` | English (US and UK), Brazilian Portuguese, Spanish, French, Hindi, Italian, Japanese and Mandarin, as far as the stack has voices |

To use them:

1. Open *Settings* → *Voice assistants*, and add or open an assistant.
2. Set *Speech-to-text* to **Calliope Parakeet**.
3. Set *Text-to-speech* to **Calliope Kokoro**, then choose a language and a
   voice.

**Speech-to-text.** Parakeet detects the language itself and refuses a
`language` field. The pipeline's language only decides whether Home Assistant
offers the engine, and the integration never sends it.

Parakeet's languages are Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French,
German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish,
Portuguese, Romanian, Russian, Slovak, Slovenian, Spanish, Swedish and
Ukrainian.

A stack running Whisper (`STT_MODEL=whisper`) is read from
`GET /health` at setup. The entity is then `stt.calliope_whisper`, offers
Whisper's languages, and sends the language as a hint.

**Text-to-speech.** The languages and voices come from `GET /voices` at setup.
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

## What the hub is expected to provide

The integration reads only what the hub publishes today. It also handles the
events and fields the hub is about to add:

- `triggered` (`satellite`, `satellite_name`, `wake_word`, `score`) for a
  trigger word, and `conversation_started`, `turn` and `conversation_ended`
  for conversations.
- `mode` on each word in `GET /satellites/wake-words`, so that the editor
  lists trigger words and wake words apart.
- `POST /satellites/{id}/ptt`, with an optional `{"wake_word": "..."}`, for
  `calliope.push_to_talk`. The gateway must also route it.

Sending dictation into an Assist pipeline is the hub's own `ha_assist` action.
It calls Home Assistant's `assist_pipeline/run` over the websocket with a
long-lived token. It needs nothing from this integration. If it passes the
satellite's Home Assistant `device_id`, Home Assistant knows the satellite's
area, and "turn on the lights" means the lights in that room.

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
- Glossary profiles for speech-to-text. The engine is called without one.
- A satellite renamed on the hub keeps its old name in Home Assistant until it
  is renamed there too.

## Licence

BSD 2-Clause, as the rest of the repository.
