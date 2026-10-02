"""The second stage of a wake word: was the word actually said?

    verify.matches("Aleksa, liga a luz", verify.spellings("alexa"))    -> "aleksa"
    verify.matches("Obrigado.", verify.spellings("alexa"))             -> None
    verify.matches("Hey hey Dervis.", verify.spellings("hey_jarvis"))  -> "jarvis"

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

STT IS TOLD WHICH WORD TO LISTEN FOR. The transcription of that audio carries
the word's name as vocabulary (vocabulary(), router.Router.transcribe's
`boost`), so that a Parakeet which began the word right finishes it as the
word ("Jarv..." is "Jarvis", not "Jarves") rather than as something close.

A WORD IS HEARD WHEN one of its spellings is close to some run of the
transcript's words (difflib, VERIFY_RATIO), or is one of them exactly for a
spelling of SHORT_SPELLING letters or fewer, or, for a spelling long enough
to tell by its sounds (SOUND_MIN), when the run has its sounds (sounds()) and
is still near it by its letters (SOUND_RATIO). On 30 Sep 2026 a real "hey
jarvis" came back as "Hey hey Dervis.": "dervis" is 0.67 of "jarvis" by its
letters and the same by its sounds, T R F S. The spellings are the ones STT
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
# and "a lei já" at most 0.73, while "Alécia" is 0.83 of "alexia". Never a
# run two letters or more shorter than the spelling: "essa", as common a
# word as Portuguese has, is 0.8 of "alessa", and "leia" of "alexia".
VERIFY_RATIO = 0.8
# Except a spelling of this many letters or fewer, which must be heard as it
# is written. One letter changed in five is already 0.8, and the everyday
# word one letter away from a short spelling is close: at 0.8 "could" and
# "loud" were "cloud", "the rock" was "grock" and "Davis" was "javis". What
# else STT writes for a short word is a spelling of its own (SPELLINGS, or
# the word's verify.spellings).
SHORT_SPELLING = 5
# A spelling of this many sounds or more (sounds()) also matches a run that
# has exactly its sounds, in their order, begins with a vowel only where the
# spelling does, and is SOUND_RATIO of it by its letters: "Dervis" and
# "Travis" are "jarvis", T R F S. Fewer sounds do not tell a name from
# everyday words: "could" has the sounds of "claude" (K L T), and "Lucas",
# "lagos" and "locais" those of "alexa" (L K S), so a shorter spelling is
# matched by its letters alone. Not one sound more, less or changed: "três",
# "atrás" and "dress" are "jarvis" without its F, and with one sound changed
# "talvez", "televisão", "direitos", "travel" and "girls" were "jarvis" too,
# in 1 to 2 of every 100 sentences of everyday Portuguese and English. A
# Portuguese TV is what the check is for. Nor a vowel first: "através" and
# "otherwise" are T R F S.
SOUND_MIN = 4
# How close to the spelling by its letters a run heard by its sounds must
# still be. "Dervis" and "Travis" are 0.67 of "jarvis", two letters of six
# changed, while "tarefas" is 0.62 of "jarves", and "tarifas" and "tariffs"
# 0.62 of "jarvis", all T R F S. Measured on 64,939 sentences of everyday
# Portuguese and English (FLEURS, mTEDx, Tatoeba), "jarvis" is heard in 5 of
# them this way, all "drives" (0.67 of "jarves"), where its sounds alone
# heard it in 125.
SOUND_RATIO = 0.65
# How long the hub waits for STT before it lets the wake through anyway: the
# model has already fired, and a failing STT must not silence a satellite.
VERIFY_TIMEOUT_S = 1.5

# What STT writes for each built-in word. The first is the word's name as a
# transcript writes it, capitals and all, which is what STT is told to listen
# for (vocabulary()); the rest are what STT writes when it gets the word
# wrong, in lower case. To add one: a new line here, with spaces between
# words; a leading "hey" or "ok" is dropped by spellings(), so "hey jarvis"
# and "jarvis" are the same.
_ALEXA = ("Alexa", "alexia", "aleksa", "alecsa", "alessa")
_CLAUDE = ("Claude", "cloud", "clod", "klaud")
_GROK = ("Grok", "grock", "groc")
_GEMINI = ("Gemini", "jemini")
_GPT = ("Chat GPT", "gpt")
SPELLINGS: dict[str, tuple[str, ...]] = {
    "alexa": _ALEXA,
    "alexa_ptbr": _ALEXA,
    "hey_jarvis": ("Jarvis", "jarves", "javis"),
    "lumos": ("Lumos", "lumus", "lumo"),
    "hey_claude": _CLAUDE, "claude": _CLAUDE,
    "hey_grok": _GROK, "grok": _GROK,
    "hey_gemini": _GEMINI, "gemini": _GEMINI,
    "hey_chat_gpt": _GPT, "gpt": _GPT,
}
# SPELLINGS THAT ARE EVERYDAY WORDS count only straight after a "hey" or an
# "ok". On 2 Oct 2026 a video wakened hey_claude and the check let it through,
# because "cloud" is a spelling of "Claude" and was heard anywhere in the
# transcript; a language model then got the room's audio. "Hey cloud" is
# still Claude misheard; "the cloud is down" is a video. Names nobody says in
# passing ("Jarvis", "Alexa", "Grok") need no lead.
EVERYDAY = frozenset({"cloud", "clod", "gpt"})
_LEADS = frozenset({"hey", "ok", "okay"})
# A "hey" or an "ok" before a name, or on its own: every "hey jarvis" has one,
# so as a spelling it would match any wake at all, and as vocabulary it
# would tell STT to hear it.
_LEADING = re.compile(r"^(?:hey|ok)(?: |$)", re.IGNORECASE)
# The version a model file's name can end in. openWakeWord's own are
# hey_jarvis_v0.1.onnx, whose dot keeps them out of a name (wakeword.NAME),
# but a custom model named after them as hey_nabu_v2.onnx is "hey_nabu_v2",
# and the "v2" is not said.
_VERSION = re.compile(r"[_-]v\d+$", re.IGNORECASE)

# The sound classes sounds() writes a spelling in, one capital each. A class
# is the letters English and Portuguese write for one sound, or for two that
# STT does not keep apart in a word it does not know:
#   T  d t j, dj, and g before e, i or y. The three are made close together
#      in the mouth, and Brazilian Portuguese says "di" and "ti" as "dji" and
#      "tchi", so a d is often what STT keeps of a j: "Dervis" is what
#      Parakeet wrote for "Jarvis" on 30 Sep 2026. A g before e or i is soft
#      in Portuguese ("gente") and most English ("gem"), though not in "get"
#      or "girl", which this gets wrong.
#   P  b p                    F  f v w, ph
#   K  k q, ck, and c or g before anything but e, i or y. An x is K then S,
#      as it is in the names a wake word has: "Alexa" is "Aleksa" and
#      "Alecsa". That is by choice. In Portuguese an x is more often the X of
#      "caixa" and "lixo", or an S or a Z ("próximo", "exame"), and no one
#      class is right for all of them.
#   S  s z, ss, ç (normalise() writes it "s"), and c before e, i or y
#   X  ch sh: "chuva" and "show" begin with one sound, "chat" with a close one
#   L  l, lh                  R  r, rr                   M  m n, nh
# Vowels, "y" and "h" are no class at all: they are what an accent changes
# most and what STT gets wrong most ("Jarvis", "Jervis", "Jarves"), so a
# spelling's sounds are its consonants. Two of one class with nothing or
# only vowels between them are one sound ("ss", "rr", "ck", "dj", and the
# two of "dado"), and a letter in no class (a digit, a letter of another
# alphabet) is a class of its own.
_CLASS = {**dict.fromkeys("dtj", "T"), **dict.fromkeys("bp", "P"), **dict.fromkeys("fvw", "F"),
          **dict.fromkeys("kq", "K"), **dict.fromkeys("sz", "S"), "l": "L", "r": "R",
          **dict.fromkeys("mn", "M")}
_SOFTENS = "eiy"   # what makes a c an S and a g a T
_SILENT = "aeiouyh "   # and the space between two words


def normalise(text: str) -> str:
    """Lower case, without accents, and every run of anything that is not a
    letter or a digit (an underscore included) as one space: "Alécia," is
    "alecia" and "hey_jarvis" is "hey jarvis". A "ç" is an "s", the sound it
    always is, where stripping its accent would leave the c of "casa"."""
    text = text.replace("ç", "s").replace("Ç", "s")
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", bare.lower()).strip()


def sounds(text: str) -> str:
    """The sound classes of a text, in order, one capital a sound (the table
    above): "jarvis", "dervis" and "travis" are all "TRFS", and "chat gpt"
    and "chatgpt" are both "XTKPT". The text is normalised first, so an
    accent or a "ç" is never a sound of its own. A space only separates: a
    digraph, and a c or g made soft, are within one word."""
    text = normalise(text)
    out: list[str] = []
    i = 0
    while i < len(text):
        c, after = text[i], text[i + 1:i + 2]
        i += 1
        if c in _SILENT:
            continue
        if after == "h" and c in "csp":
            heard = "F" if c == "p" else "X"
            i += 1
        elif c in "cg":
            heard = ("S" if c == "c" else "T") if after and after in _SOFTENS else "K"
        elif c == "x":
            heard = "KS"
        else:
            heard = _CLASS.get(c, c)
        for sound in heard:
            if not out or out[-1] != sound:
                out.append(sound)
    return "".join(out)


def _written(text: str) -> str:
    """`text` without a leading "hey" or "ok", with every run of anything
    that is not a letter or a digit as one space, and its case and accents
    as they were: "Hey, Nabu!" is "Nabu", and "nah, boo" is "nah boo",
    which stt-stack would otherwise take as two terms."""
    return _LEADING.sub("", re.sub(r"[\W_]+", " ", text).strip())


def _spelling(text: str) -> str:
    return _LEADING.sub("", normalise(text))


def spellings(word: str, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """What counts as `word` in a transcript: its built-in spellings, or its
    own name for a word with none (underscores as spaces, "hey" and a
    version dropped: "hey_nabu_v2" is "nabu"), and then `extra`, the word's
    own verify.spellings. Normalised, each once, in that order, and none
    that is only a "hey" or an "ok". A word named nothing else still has its
    name, so that no word is left with nothing to be heard as."""
    out = [_spelling(s) for s in (*SPELLINGS.get(word, (_VERSION.sub("", word),)), *extra)]
    return [s for s in dict.fromkeys(out) if s] or [normalise(word)]


def vocabulary(word: str, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """What STT is told to listen for in the word's audio (router.Router.
    transcribe's `boost`): the word's name as a transcript writes it, which
    is its first in SPELLINGS ("Jarvis", "Chat GPT") or else its own name
    with capitals and without a version ("hey_mycroft" is "Mycroft", and
    "hey_nabu_v2" "Nabu"), and then `extra`, the word's own
    verify.spellings, as they were typed. Written as _written() writes
    them, each once, and none that is only a "hey" or an "ok".

    Not the rest of SPELLINGS: those are what STT writes when it gets the
    word wrong, which matches() takes anyway, and telling STT to listen for
    them would only ask it to get the word wrong."""
    name = SPELLINGS[word][0] if word in SPELLINGS else _written(_VERSION.sub("", word)).title()
    out: dict[str, str] = {}
    for term in (name, *extra):
        written = _written(term)
        out.setdefault(normalise(written), written)
    return [written for key, written in out.items() if key]


def matches(transcript: str, spellings: list[str]) -> str | None:
    """The first spelling heard in `transcript`, or None.

    A spelling of k words is compared with every run of k words of the
    transcript, joined with spaces ("chat gpt"), and with every run of up to
    k words joined without them against the spelling written the same way,
    so "chatgpt" and "Chat GPT" both match "chat gpt". Never runs of more
    words than the spelling has: "a lei já" run together is "aleija", 0.83
    of "alexia", and was a TV saying something else. A short spelling
    (SHORT_SPELLING) matches only itself by its letters. A spelling of
    SOUND_MIN sounds or more also matches a run with its sounds that is near
    it by its letters (SOUND_RATIO). An empty transcript matches nothing."""
    words = normalise(transcript).split()
    if not words:
        return None
    for spelling in spellings:
        want = normalise(spelling)
        k = len(want.split())
        tight = want.replace(" ", "")
        short = len(tight) <= SHORT_SPELLING
        sound = sounds(want)
        led = want in EVERYDAY
        for n in range(1, k + 1):
            for i in range(len(words) - n + 1):
                if led and (i == 0 or words[i - 1] not in _LEADS):
                    continue
                run = words[i:i + n]
                if n == k and _close(" ".join(run), want, short, sound):
                    return spelling
                if _close("".join(run), tight, short, sound):
                    return spelling
    return None


def _close(heard: str, want: str, short: bool, sound: str) -> bool:
    if heard == want:
        return True
    ratio = difflib.SequenceMatcher(None, heard, want).ratio()
    if not short and ratio >= VERIFY_RATIO and len(heard) >= len(want) - 1:
        return True
    return (len(sound) >= SOUND_MIN and ratio >= SOUND_RATIO and sounds(heard) == sound
            and (heard[0] in _SILENT) == (want[0] in _SILENT))
