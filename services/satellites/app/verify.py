"""The second stage of a wake word: was the word actually said?

    verify.matches("Aleksa, liga a luz", verify.spellings("alexa")) -> "aleksa"
    verify.matches("Obrigado.", verify.spellings("alexa"))          -> None

WHY. A wake word model scores a sound, not a word, and a voice on a TV or on
another device in the room can score as high as the person who lives there:
on 29 Sep 2026 a Portuguese video woke "alexa" at 0.905 to 0.956 with
"Obrigado." and "De manipular.", while real wakes scored 0.877 to 1.0. The two
ranges overlap, so no threshold separates them, and knowing what is playing
does not help either: the TV and other devices are not the hub's. So the hub
does what Amazon does: when a word fires, the short stretch of audio that
holds it (listening.Heard.clip) is transcribed by the stack's own STT, and the
hub answers only when the word is in that transcript. That works for any
source and any word, and main.Hub.on_wake is where it runs.

A WORD IS HEARD WHEN one of its spellings is close to some run of the
transcript's words (difflib, VERIFY_RATIO), or is one of them exactly for a
spelling of SHORT_SPELLING letters or fewer. The spellings are the ones STT
is known to write for the word (SPELLINGS: Parakeet writes "Alécia" and
"Hey Cloud" as readily as "Alexa" and "Hey Claude"), plus the word's own
`verify.spellings`, which is how a custom model's name, or a spelling found
in the Activity log, is added without a release.

Nothing here talks to anything: this is the pure half, tested on its own.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# A spelling matches a run of the transcript's words at this ratio or more.
# Measured on the words that woke "alexa" by mistake: "deixa" is 0.6 of it
# and "a lei já" at most 0.73, while "Alécia" is 0.83 of "alexia".
VERIFY_RATIO = 0.8
# Except a spelling of this many letters or fewer, which must be heard as it
# is written. One letter changed in five is already 0.8, and the everyday
# word one letter away from a short spelling is close: at 0.8 "could" and
# "loud" were "cloud", "the rock" was "grock" and "Davis" was "javis". What
# else STT writes for a short word is a spelling of its own (SPELLINGS, or
# the word's verify.spellings).
SHORT_SPELLING = 5
# How long the hub waits for STT before it lets the wake through anyway: the
# model has already fired, and a failing STT must not silence a satellite.
VERIFY_TIMEOUT_S = 1.5

# What STT writes for each built-in word, beyond the word itself. To add one:
# a new line here, lower case, with spaces between words; a leading "hey" or
# "ok" is dropped by spellings(), so "hey jarvis" and "jarvis" are the same.
_ALEXA = ("alexa", "alexia", "aleksa", "alecsa", "alessa")
_CLAUDE = ("claude", "cloud", "clod", "klaud")
_GROK = ("grok", "grock", "groc")
_GEMINI = ("gemini", "jemini")
_GPT = ("chat gpt", "gpt")
SPELLINGS: dict[str, tuple[str, ...]] = {
    "alexa": _ALEXA,
    "alexa_ptbr": _ALEXA,
    "hey_jarvis": ("jarvis", "jarves", "javis"),
    "lumos": ("lumos", "lumus", "lumo"),
    "hey_claude": _CLAUDE, "claude": _CLAUDE,
    "hey_grok": _GROK, "grok": _GROK,
    "hey_gemini": _GEMINI, "gemini": _GEMINI,
    "hey_chat_gpt": _GPT, "gpt": _GPT,
}
_LEADING = ("hey ", "ok ")


def normalise(text: str) -> str:
    """Lower case, without accents, and every run of anything that is not a
    letter or a digit (an underscore included) as one space: "Alécia," is
    "alecia" and "hey_jarvis" is "hey jarvis"."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", bare.lower()).strip()


def _spelling(text: str) -> str:
    s = normalise(text)
    for lead in _LEADING:
        if s.startswith(lead):
            return s[len(lead):]
    return s


def spellings(word: str, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """What counts as `word` in a transcript: its built-in spellings, or its
    own name for a word with none (underscores as spaces, "hey" dropped),
    and then `extra`, the word's own verify.spellings. Normalised, each
    once, in that order."""
    out = [_spelling(s) for s in (*SPELLINGS.get(word, (word,)), *extra)]
    return [s for s in dict.fromkeys(out) if s]


def matches(transcript: str, spellings: list[str]) -> str | None:
    """The first spelling heard in `transcript`, or None.

    A spelling of k words is compared with every run of k words of the
    transcript, joined with spaces ("chat gpt"), and with every run of up to
    k words joined without them against the spelling written the same way,
    so "chatgpt" and "Chat GPT" both match "chat gpt". Never runs of more
    words than the spelling has: "a lei já" run together is "aleija", 0.83
    of "alexia", and was a TV saying something else. A short spelling
    (SHORT_SPELLING) matches only itself. An empty transcript matches
    nothing."""
    words = normalise(transcript).split()
    if not words:
        return None
    for spelling in spellings:
        want = normalise(spelling)
        k = len(want.split())
        tight = want.replace(" ", "")
        short = len(tight) <= SHORT_SPELLING
        for n in range(1, k + 1):
            for i in range(len(words) - n + 1):
                run = words[i:i + n]
                if n == k and _close(" ".join(run), want, short):
                    return spelling
                if _close("".join(run), tight, short):
                    return spelling
    return None


def _close(a: str, b: str, short: bool) -> bool:
    return a == b if short else difflib.SequenceMatcher(None, a, b).ratio() >= VERIFY_RATIO
