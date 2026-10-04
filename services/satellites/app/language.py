"""Which language an utterance is in, and which voice answers in it.

    code = detect("que horas são", prior="en")      -> "pt"
    tag(code)                                        -> "pt-BR"   (sent on; "pt-PT" in a
                                                                   household that says so)
    reply_tag(code)                                  -> "pt-BR"   (answered in)
    voice_for(code, default="bm_george")             -> "pf_dora"

WHY THE HUB DETECTS IT, FROM TEXT. stt-stack's default engine, Parakeet TDT
0.6B v3, recognises 25 European languages on its own and takes no hint: it
refuses a `language` field with a 400, and its verbose_json says "unknown"
for the language it heard (services/stt/README.md). The words it returns are
in the language that was spoken, with that language's accents, so the
transcript is where the language can be read.

WHY py3langid. Measured on a Mac, 2026-09-25, on 96 short commands and
questions in English, Brazilian Portuguese, Spanish, French, Italian, German,
Dutch and Polish ("what time is it", "apaga a luz da cozinha", "¿qué hora
es?", "quelle heure est-il"...), both restricted to the recogniser's
languages:

    py3langid 0.4.0                   94/96   0.02 ms a call   4.4 MB installed
    lingua-language-detector 2.2.0    90/96   0.2 ms a call    295 MB installed

py3langid costs memory instead: 128 MB more resident on the Mac after loading,
of which about 50 MB is the model it keeps (its automaton and one table per
language); the rest is the decompression peak, which macOS does not give
back. Its misses were "liga a luz" (Romanian) and "e a Roma?"; lingua's
included "tell me a joke" (Finnish) and "open the garage door" (Dutch).

ONE WORD SAYS LITTLE, SO THE LANGUAGE ALREADY IN USE WINS A CLOSE CALL. The
prior is the conversation's language so far, or else the household's main
language: the first of SATELLITES_LANGUAGES, English when it is unset.
Another language has to reach MIN_CONFIDENCE and beat the prior by
PRIOR_MARGIN of probability. In an English household "sim" alone is not
evidence enough to leave English, and in a Portuguese conversation "ok" does
not switch it to English.

THE HOUSEHOLD'S LANGUAGES (SATELLITES_LANGUAGES, BCP 47 tags, most spoken
first: "en", "fr", "pt-PT", "en,pt-BR"). The first is the prior above, and
the language an answer falls back to when Kokoro cannot speak the one heard.
Each one's region is the tag its language is sent on as: Portuguese is
"pt-PT" in a household that says so, and "pt-BR" (TAGS) in one that does not.
Home Assistant keeps pt and pt-BR intents apart, so the tag decides which
sentences a command is matched against.

WHAT KOKORO CAN SAY. tts-stack's voices cover English, Brazilian Portuguese,
Spanish, French and Italian (the first letter of a voice names its
phonemiser; services/tts/app/openai_api.py). A language the recogniser
understands and Kokoro cannot speak, German or Polish, is answered in English
with the default voice, and an LLM is told so (destinations.answer_instruction):
a German sentence read by an English voice is worse than an English answer.
A household whose main language Kokoro speaks is answered in that instead.
"""

from __future__ import annotations

import logging
import os
import re
import threading

log = logging.getLogger("voice-satellites.language")

# Parakeet TDT 0.6B v3's languages (its model card): what stt-stack can
# transcribe, and so what a transcript can be in.
RECOGNISED = ("bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hr", "hu", "it",
              "lt", "lv", "mt", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "uk")

# Kokoro voices in tts-stack's voices-v1.0.bin, by language. English is the
# hub's own default voice (SATELLITES_TTS_VOICE), so it is not listed.
VOICES = {"pt": "pf_dora", "es": "ef_dora", "fr": "ff_siwis", "it": "if_sara"}
SPOKEN = frozenset({"en", *VOICES})
# The language of the hub's default voice, and the household's when
# SATELLITES_LANGUAGES names none.
DEFAULT = "en"
ENV = "SATELLITES_LANGUAGES"

# The tag a detected language is sent on as (Home Assistant, the LLM) when the
# household's languages name no region for it. Kokoro's only Portuguese voices
# are Brazilian, so a Portuguese answer is spoken with a Brazilian accent
# whatever the tag; HA reads "pt" as European Portuguese.
TAGS = {"pt": "pt-BR"}
_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")

NAMES = {"pt-br": "Brazilian Portuguese", "pt-pt": "European Portuguese", "en-gb": "British English",
         "en-us": "American English", "bg": "Bulgarian", "cs": "Czech", "da": "Danish",
         "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
         "fi": "Finnish", "fr": "French", "hr": "Croatian", "hu": "Hungarian", "it": "Italian",
         "lt": "Lithuanian", "lv": "Latvian", "mt": "Maltese", "nl": "Dutch", "pl": "Polish",
         "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sk": "Slovak",
         "sl": "Slovenian", "sv": "Swedish", "uk": "Ukrainian"}

# How much more probable (0-1) another language must be than the one in use,
# and how probable at all. A single word spreads its probability thin: "sim"
# scored Estonian 0.14 and English 0.03. Measured on the 96 phrases above,
# the two together changed no correct answer and kept single words ("sim",
# "tchau", "danke") in the language already in use.
PRIOR_MARGIN = 0.1
MIN_CONFIDENCE = 0.3
# Fewer letters than this is no evidence at all ("ok", "42").
MIN_LETTERS = 3


def primary(tag: str | None) -> str:
    """"pt-BR" -> "pt"; None -> the household's main language."""
    return (tag or household()[0]).split("-")[0].lower()


def _canonical(tag: str) -> str:
    """"PT-br" -> "pt-BR", "zh-hant-tw" -> "zh-Hant-TW"."""
    first, *rest = tag.split("-")
    return "-".join([first.lower(), *(p.upper() if len(p) == 2 else p.title() if len(p) == 4
                                      else p.lower() for p in rest)])


def _household() -> tuple[tuple[str, ...], list[str]]:
    tags, unused = [], []
    for raw in (os.getenv(ENV) or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        t = _canonical(raw) if _TAG.fullmatch(raw) else None
        if t is None or t.split("-")[0] not in RECOGNISED:
            unused.append(raw)
        elif t not in tags:
            tags.append(t)
    return tuple(tags) or (DEFAULT,), unused


def household() -> tuple[str, ...]:
    """The household's languages, most spoken first (SATELLITES_LANGUAGES),
    else English. A value that is not a tag, or names a language the
    recogniser cannot hear, is left out (household_problems)."""
    return _household()[0]


def household_problems() -> list[str]:
    """A sentence for each SATELLITES_LANGUAGES entry the hub leaves out, for
    the log at start: set and silently ignored, it would look like a household
    whose language nobody detects."""
    return [f"{ENV}: {raw!r} is left out: not a language tag, or not one the speech "
            f"recogniser hears ({', '.join(RECOGNISED)})" for raw in _household()[1]]


def main() -> str:
    """The household's main language, as a code: the prior when nothing
    else is known."""
    return primary(household()[0])


def name(tag: str | None) -> str:
    t = (tag or household()[0]).lower()
    return NAMES.get(t) or NAMES.get(primary(t)) or t


def tag(code: str) -> str:
    """What a detected language is sent on as: the household's own tag for
    it ("pt" in a "pt-PT" household -> "pt-PT"), else TAGS ("pt" -> "pt-BR"),
    else the code."""
    return next((t for t in household() if primary(t) == code), None) or TAGS.get(code, code)


def reply_tag(spoken: str) -> str:
    """The language the answer is in: the one spoken, when a voice can say
    it, else the household's first language a voice can say, else English.
    Takes a code or a tag and keeps a hint's region."""
    if primary(spoken) in SPOKEN:
        return spoken
    return next((t for t in household() if primary(t) in SPOKEN), DEFAULT)


def voice_for(spoken: str, default: str) -> str:
    """The voice for an answer to something said in `spoken` (a code or a
    tag). English, and every language no voice speaks, get `default`."""
    return VOICES.get(primary(reply_tag(spoken)), default)


class _Detector:
    """py3langid, loaded once (0.4 s) and restricted to RECOGNISED. Loading
    runs off the event loop (main.py warms it at start); a detect() before
    that loads it in the caller's thread."""

    def __init__(self) -> None:
        self._ident = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._ident is not None

    def load(self) -> None:
        with self._lock:
            if self._ident is not None:
                return
            from py3langid.langid import MODEL_FILE, LanguageIdentifier

            ident = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
            ident.set_languages(list(RECOGNISED))
            self._ident = ident

    def scores(self, text: str) -> dict[str, float]:
        self.load()
        return {code: float(p) for code, p in self._ident.rank(text)}

    def detect(self, text: str, prior: str | None = None) -> str:
        prior = primary(prior) if prior else main()
        if sum(c.isalpha() for c in text) < MIN_LETTERS:
            return prior
        s = self.scores(text)
        top = max(s, key=s.get)
        if top != prior and (s[top] < MIN_CONFIDENCE or s[top] - s.get(prior, 0.0) < PRIOR_MARGIN):
            return prior
        return top


detector = _Detector()


def detect(text: str, prior: str | None = None) -> str:
    """The language `text` is in, as an ISO 639-1 code from RECOGNISED."""
    return detector.detect(text, prior)
