"""Constants for the Calliope integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "calliope"

DEFAULT_URL: Final = "https://orko.gabrielbelli.com:30080"

# The one bus event every satellite event is fired as, for YAML automations.
# Its data is the hub's own event plus "device_id", "satellite_name" and
# "kind" (see KIND_* below).
EVENT_CALLIOPE: Final = "calliope_event"

# What happened, as the event entity's event_type, a device trigger's type
# and the "kind" of a calliope_event. One vocabulary for all three.
KIND_WAKE_WORD: Final = "wake_word"
KIND_TRIGGER_WORD: Final = "trigger_word"
KIND_COMMAND: Final = "command"
KIND_BUTTON_PRESS: Final = "button_press"
KIND_BUTTON_RELEASE: Final = "button_release"
KIND_CONVERSATION_STARTED: Final = "conversation_started"
KIND_CONVERSATION_ENDED: Final = "conversation_ended"

VOICE_EVENT_TYPES: Final = [
    KIND_WAKE_WORD,
    KIND_TRIGGER_WORD,
    KIND_COMMAND,
    KIND_BUTTON_PRESS,
    KIND_BUTTON_RELEASE,
    KIND_CONVERSATION_STARTED,
    KIND_CONVERSATION_ENDED,
]

# Hub events that are state, not happenings: kept off the bus. A status
# arrives every 10 s from every satellite.
QUIET_HUB_EVENTS: Final = frozenset({"status"})

# The buttons of the ESP32-Korvo, which the firmware lists in hello.caps. Used
# for a satellite whose caps are not known (offline since HA started).
KORVO_BUTTONS: Final = ("play", "set", "mode", "rec", "vol_up", "vol_down")

# The settings a satellite's switches and slider change with PATCH.
SETTING_KEYS: Final = (
    "volume",
    "mic_enabled",
    "speaker_enabled",
    "lights_enabled",
)

# Reconnect backoff for the event stream, in seconds.
BACKOFF_MIN: Final = 1.0
BACKOFF_MAX: Final = 60.0
# The hub sends a keepalive comment every 15 s; three missed means the
# connection is dead even if TCP has not noticed.
SSE_READ_TIMEOUT: Final = 45.0

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
