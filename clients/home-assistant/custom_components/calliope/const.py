"""Constants for the Calliope integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "calliope"

# Shown as an example beside the URL field, never filled in for anyone: the
# address is each deployment's own.
EXAMPLE_URL: Final = "https://calliope.example.com"

# The README's section on the API key: which preset, where to make it and
# what each scope is for. The repair issue for a missing scope links to it.
KEY_DOCS_URL: Final = (
    "https://github.com/gabrielbelli/calliope/tree/main/clients/home-assistant"
    "#the-api-key"
)

# The one bus event every satellite event is fired as, for YAML automations.
# Its data is the hub's own event plus "device_id", "satellite_name" and
# "kind" (see KIND_* below).
EVENT_CALLIOPE: Final = "calliope_event"

# The glossary profile this integration keeps on the stack's speech-to-text:
# Home Assistant's own names (vocabulary.py). The hub asks for it by this name
# too, so the two must not drift.
GLOSSARY_PROFILE: Final = "home-assistant"

# In the entry's data: the speech-to-text engine that holds the unique id the
# one entity had before there were several ("{entry_id}_stt"), decided once, so
# a new default on the stack does not move stt.calliope_parakeet to it.
CONF_LEGACY_STT: Final = "stt_legacy_engine"

# What happened, as the event entity's event_type, a device trigger's type
# and the "kind" of a calliope_event. One vocabulary for all three.
KIND_WAKE_WORD: Final = "wake_word"
KIND_TRIGGER_WORD: Final = "trigger_word"
KIND_COMMAND: Final = "command"
KIND_BUTTON_PRESS: Final = "button_press"
KIND_BUTTON_RELEASE: Final = "button_release"
KIND_CONVERSATION_STARTED: Final = "conversation_started"
KIND_CONVERSATION_ENDED: Final = "conversation_ended"

# What a satellite heard or said, as the Voice event entity's event types.
# Button presses are not among them: each button is an event entity of its own
# (BUTTON_EVENT_TYPES), made only for the buttons the satellite has.
VOICE_EVENT_TYPES: Final = [
    KIND_WAKE_WORD,
    KIND_TRIGGER_WORD,
    KIND_COMMAND,
    KIND_CONVERSATION_STARTED,
    KIND_CONVERSATION_ENDED,
]
# A device trigger's type: the voice kinds and a button's two edges.
TRIGGER_TYPES: Final = [*VOICE_EVENT_TYPES, KIND_BUTTON_PRESS, KIND_BUTTON_RELEASE]
# A button event entity's event types.
BUTTON_EVENT_TYPES: Final = ["press", "release"]
# How a button is named where it is printed on the board. A button a later
# firmware adds is named by its id in capitals.
BUTTON_LABELS: Final = {
    "play": "PLAY",
    "set": "SET",
    "mode": "MODE",
    "rec": "REC",
    "vol_up": "VOL+",
    "vol_down": "VOL-",
    "key1": "KEY1",
}

# Hub events that are state, not happenings: kept off the bus. A status
# arrives every 10 s from every satellite; a config event carries only the
# names of the settings that changed, and the record read after it is what
# the entities show.
QUIET_HUB_EVENTS: Final = frozenset({"status", "config"})

# The hub's range for mic_gain_db, for a satellite that does not say its own
# (caps.mic.max_gain_db): the Korvo's codec takes all of it.
MIC_GAIN_MAX_DB: Final = 37.5
# What volume_up and volume_down move the media player by: 5 of the hub's
# 100 steps.
VOLUME_STEP: Final = 0.05
# The media player's app_name: what plays, the phone's AirPlay or a stream
# Home Assistant sent.
APP_AIRPLAY: Final = "AirPlay"
APP_CALLIOPE: Final = "Calliope"

# Reconnect backoff for the event stream, in seconds.
BACKOFF_MIN: Final = 1.0
BACKOFF_MAX: Final = 60.0
# The hub sends a keepalive comment every 15 s; three missed means the
# connection is dead even if TCP has not noticed.
SSE_READ_TIMEOUT: Final = 45.0
# The gateway ends every event stream after 15 minutes, so that the key is
# checked again. A stream that ends cleanly after at least this many seconds
# is taken for that and opened again at once, with the entities left as they
# are. One that ends sooner is taken for an outage and retried with backoff,
# so a proxy that closes each stream as soon as it opens is not asked again
# in a tight loop.
STREAM_ROUTINE_AFTER: Final = 60.0

# Parakeet TDT 0.6B v3's 25 European languages (ISO 639-1). It detects the
# language itself and refuses a `language` field, so these are declared to
# Home Assistant but never sent.
PARAKEET_LANGUAGES: Final = [
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "mt",
    "nl",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "uk",
]

# Whisper large-v3's languages, for a deployment with STT_MODEL=whisper. That
# engine takes the language as a hint, so it is sent.
WHISPER_LANGUAGES: Final = [
    "af",
    "am",
    "ar",
    "as",
    "az",
    "ba",
    "be",
    "bg",
    "bn",
    "bo",
    "br",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "eu",
    "fa",
    "fi",
    "fo",
    "fr",
    "gl",
    "gu",
    "ha",
    "haw",
    "he",
    "hi",
    "hr",
    "ht",
    "hu",
    "hy",
    "id",
    "is",
    "it",
    "ja",
    "jw",
    "ka",
    "kk",
    "km",
    "kn",
    "ko",
    "la",
    "lb",
    "ln",
    "lo",
    "lt",
    "lv",
    "mg",
    "mi",
    "mk",
    "ml",
    "mn",
    "mr",
    "ms",
    "mt",
    "my",
    "ne",
    "nl",
    "nn",
    "no",
    "oc",
    "pa",
    "pl",
    "ps",
    "pt",
    "ro",
    "ru",
    "sa",
    "sd",
    "si",
    "sk",
    "sl",
    "sn",
    "so",
    "sq",
    "sr",
    "su",
    "sv",
    "sw",
    "ta",
    "te",
    "tg",
    "th",
    "tk",
    "tl",
    "tr",
    "tt",
    "uk",
    "ur",
    "uz",
    "vi",
    "yi",
    "yo",
    "yue",
    "zh",
]

# Kokoro encodes the language in the first letter of a voice name
# (services/tts/app/openai_api.py, LANGUAGE_BY_PREFIX), as Home Assistant
# language tags.
KOKORO_LANGUAGE_BY_PREFIX: Final = {
    "a": "en-US",
    "b": "en-GB",
    "e": "es",
    "f": "fr-FR",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-BR",
    "z": "zh-CN",
}
# The voice a language gets when a pipeline names none. Anything not listed
# takes the first of its voices in alphabetical order.
KOKORO_DEFAULT_VOICES: Final = {
    "en-GB": "bm_george",
    "en-US": "af_heart",
    "pt-BR": "pf_dora",
}
# /v1/audio/speech refuses input over 4096 characters.
TTS_MAX_CHARS: Final = 4000
# Kokoro's pcm: headerless 24 kHz, 16-bit, mono.
KOKORO_RATE: Final = 24000

ATTR_SPEED: Final = "speed"
ATTR_TEXT: Final = "text"
ATTR_VOICE: Final = "voice"
ATTR_FREQUENCY: Final = "frequency"
ATTR_SECONDS: Final = "seconds"
ATTR_WAKE_WORD: Final = "wake_word"

SERVICE_SAY: Final = "say"
SERVICE_TONE: Final = "tone"
SERVICE_PUSH_TO_TALK: Final = "push_to_talk"
