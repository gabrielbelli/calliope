"""One turn, streamed, and what a conversation remembers between turns.

    out = await run_turn(router, route, satellite_id=..., satellite_name=...,
                         wake_word=..., audio=pcm16k, sink=player, memory=memory)

A TURN IS A PIPELINE, NOT A SEQUENCE. The destination's answer arrives in
pieces (an LLM streams tokens); Sentences cuts it into sentences as each one
ends; each sentence is synthesised as soon as the one before it has been, and
handed to the sink (main.Player queues it on the satellite's speaker) while
the destination is still writing the next. So the first sentence is heard
before the answer exists in full:

    destination ─pieces─▶ Sentences ─sentence─▶ queue ─▶ TTS ─pcm─▶ sink ─▶ speaker
    (streams)             (cuts at . ! ?)               (one at a time)    (plays at real time)

The first sentence goes to TTS alone, for the earliest first audio. Later
sentences that have queued up while TTS was busy go together, up to
BATCH_CHARS: fewer requests once the reply is already playing.

TIMINGS. Every turn records, from the end of speech (the endpoint, less the
silence it waited for), when STT was done, when the first piece of the answer
arrived, when its first audio was handed to the satellite, when the answer was
complete and when the reply had finished playing (Outcome.timeline_ms).
answer_done minus first_audio is the overlap the pipeline buys: positive when
the satellite was already speaking while the destination was still writing.

HANDOVER. A command whose destination fails, or does not understand
(destinations.NotUnderstood), and whose action names a `fallback` wake word,
hands the same transcript to that word's conversation before anything has
been spoken, and on_handover lets main.py start the conversation. Once any of
the answer has arrived, a failure is just an error: part of it may have been
heard.

MEMORY is the last MAX_TURNS exchanges, and no more than MAX_HISTORY_CHARS of
them: an LLM's context and the time it takes to read it both grow with every
turn, and a spoken conversation rarely needs more than its last few.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections import deque
import logging
from typing import Callable, Protocol

import httpx

from . import language as lang
from .destinations import DestinationError, NotUnderstood, Request, Turn
from .router import Failed, Outcome, Route, Router, clip, strip_wake_phrase

log = logging.getLogger("voice-satellites.router")

MAX_TURNS = 20
MAX_HISTORY_CHARS = 8000
# Later TTS requests take every sentence queued while TTS was busy, up to
# this many characters. Kokoro takes 4096; a few sentences keep one request
# well under a second of synthesis.
BATCH_CHARS = 300

# Said on their own (with "ok", "no", "then" and the like around them), these
# end a conversation. English and Brazilian Portuguese, the household's.
END_PHRASES = ("that's all", "that is all", "that's it", "stop", "goodbye", "bye", "thanks",
               "thank you", "never mind", "obrigado", "obrigada", "tchau", "pode parar", "para",
               "valeu", "é só isso", "só isso", "chega", "até logo")
FILLERS = frozenset({"ok", "okay", "alright", "right", "great", "cool", "no", "yes", "well",
                     "then", "so", "very", "much", "oh", "ah", "please", "então", "tá", "ta",
                     "beleza", "muito", "sim", "não", "por", "favor", "e", "and"})


# ---- ending phrases ------------------------------------------------------------


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.casefold().replace("’", "'"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[\w']+", text)


def is_ending(text: str, phrases: tuple[str, ...] | list[str] = END_PHRASES) -> bool:
    """True when `text` is nothing but ending phrases and filler: "thanks",
    "ok, that's all", "obrigado, tchau". A sentence that says anything else
    ("thanks, and what about tomorrow?") carries on."""
    words = _words(text)
    wanted = sorted({tuple(_words(p)) for p in phrases if _words(p)}, key=len, reverse=True)
    fillers = {w for f in FILLERS for w in _words(f)}
    i, found = 0, False
    while i < len(words):
        for p in wanted:
            if tuple(words[i:i + len(p)]) == p:
                i += len(p)
                found = True
                break
        else:
            if words[i] not in fillers:
                return False
            i += 1
    return found


# ---- sentences --------------------------------------------------------------------


# Words a full stop follows without ending the sentence. English and
# Portuguese titles and the Latin ones people write in both.
ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "dra", "prof", "profa", "sr", "sra", "srta",
                           "st", "jr", "vs", "etc", "e.g", "i.e", "eg", "ie", "no", "nº", "av",
                           "p.ex", "approx", "min", "max"})
_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)|\n+")


def speakable(text: str) -> str:
    """What an LLM writes for a screen, made fit to be read aloud: markdown
    emphasis, headings, bullets and code marks go (Kokoro reads "asterisk")."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^[ \t]*(?:[-•*]|#+)[ \t]+", "", text, flags=re.M)  # bullets, headings
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


class Sentences:
    """Text in pieces -> whole sentences, each as soon as it has ended. A
    sentence is cut at . ! ? or … followed by a space, or at a line break,
    but not after a title ("Dr. Silva"), an initial ("J. R. R."), or a list
    number at the start of a line ("1. Preheat"). A run of MAX_CHARS with no
    end at all is cut at its last comma, or its last space, so a model that
    never uses a full stop is still spoken before it has finished."""

    MAX_CHARS = 250

    def __init__(self) -> None:
        self.buf = ""

    def _is_end(self, m: re.Match) -> bool:
        if m.group().startswith("\n"):
            return True
        if not m.group().startswith("."):
            return True
        before = self.buf[:m.start()]
        word = re.search(r"([\w.ºª]+)$", before)
        if word is None:
            return True
        w = word.group(1).casefold()
        if w in ABBREVIATIONS or (len(w) == 1 and w.isalpha()):
            return False
        line = before[before.rfind("\n") + 1:]
        if w.isdigit() and line.strip() == w:
            return False  # "1." at the start of a line is a list marker
        return True

    def feed(self, piece: str) -> list[str]:
        self.buf += piece
        out = []
        while True:
            cut = None
            for m in _END.finditer(self.buf):
                if self._is_end(m):
                    cut = m.end()
                    break
            if cut is None and len(self.buf) > self.MAX_CHARS:
                head = self.buf[:self.MAX_CHARS]
                at = max(head.rfind(", "), head.rfind("; "), head.rfind(": "))
                cut = at + 1 if at > self.MAX_CHARS // 3 else (head.rfind(" ") + 1 or self.MAX_CHARS)
            if cut is None:
                return out
            sentence, self.buf = self.buf[:cut], self.buf[cut:]
            self._emit(sentence, out)

    def flush(self) -> list[str]:
        out: list[str] = []
        self._emit(self.buf, out)
        self.buf = ""
        return out

    @staticmethod
    def _emit(sentence: str, out: list[str]) -> None:
        s = speakable(sentence)
        if any(c.isalnum() for c in s):
            out.append(s)


# ---- memory -------------------------------------------------------------------------


class Memory:
    """What a conversation keeps between turns: the exchanges, the
    destination's own state (Home Assistant's conversation_id) and the
    language the last utterance was in, which is the prior for the next."""

    def __init__(self, max_turns: int = MAX_TURNS, max_chars: int = MAX_HISTORY_CHARS):
        self.turns: deque[Turn] = deque()
        self.state: dict = {}
        self.language: str | None = None
        self.max_turns, self.max_chars = max_turns, max_chars

    def history(self) -> tuple[Turn, ...]:
        return tuple(self.turns)

    def add(self, user: str, assistant: str) -> None:
        self.turns.append(Turn(user, assistant))
        # The oldest go first; the newest exchange stays even when it alone is
        # over the limit, or the next turn would have no context at all.
        while len(self.turns) > 1 and (len(self.turns) > self.max_turns or self.chars > self.max_chars):
            self.turns.popleft()

    @property
    def chars(self) -> int:
        return sum(len(t.user) + len(t.assistant) for t in self.turns)


# ---- the turn --------------------------------------------------------------------------


class Sink(Protocol):
    """Where a turn's audio goes. main.Player queues it on a satellite;
    Collect keeps it."""

    async def play(self, pcm48k: bytes, text: str) -> bool:
        """Hand one sentence's audio over; False when it will not be played
        (a speaker off, a satellite gone)."""

    async def finish(self) -> None:
        """Return once everything handed over has played out, or been dropped."""

    def spoken(self) -> str:
        """The text of every sentence that has started playing."""


class Collect:
    """A sink that plays nothing and keeps the audio: /routing/test, a test,
    or /inject without ?play=1."""

    def __init__(self) -> None:
        self.pcm = bytearray()
        self.texts: list[str] = []

    async def play(self, pcm48k: bytes, text: str) -> bool:
        self.pcm += pcm48k
        self.texts.append(text)
        return True

    async def finish(self) -> None:
        return None

    def spoken(self) -> str:
        return " ".join(self.texts)


def _ms(since: float) -> float:
    return round((time.monotonic() - since) * 1000, 1)


async def run_turn(router: Router, route: Route | None, *, satellite_id: str, satellite_name: str,
                   wake_word: str, sink: Sink, audio: bytes | None = None, text: str | None = None,
                   memory: Memory | None = None, out: Outcome | None = None,
                   speech_end: float | None = None,
                   on_handover: Callable[[Route], None] | None = None,
                   on_transcript: Callable[[Outcome], None] | None = None) -> Outcome:
    """One utterance through to its reply. Never raises, except
    CancelledError: a barge-in cancels the task, and `out` (the caller's own)
    then holds whatever the turn had got to.

    `speech_end` is time.monotonic() at the end of speech; the timeline is
    measured from it. `on_transcript` runs once the words are known, before
    anything is sent anywhere: main.py ends a conversation there on an ending
    phrase, and publishes nothing yet."""
    out = out if out is not None else Outcome()
    start = time.monotonic()
    speech_end = speech_end if speech_end is not None else start
    try:
        if route is None:
            out.error = (f"no rule matches wake word {wake_word!r} on satellite "
                         f"{satellite_name or satellite_id!r}")
            log.info("routing: %s; the utterance is dropped", out.error)
            return out
        behaviour = route.behaviour
        out.rule_id, out.mode = route.id, behaviour.mode
        if behaviour.action is None:
            out.error = f"{route.id!r} is a trigger word: it has no action, only its event"
            return out
        if audio is not None:
            said = await router.stage(out, "stt", router.stt_timeout,
                                      router.transcribe(audio, behaviour.language))
            out.timeline_ms["stt_done"] = _ms(speech_end)
        else:
            said = text or ""
        said = strip_wake_phrase(said, wake_word)
        out.transcript = said
        if not said.strip():
            out.error = "stt: nothing was heard (the transcript is empty)"
            return out
        if behaviour.mode == "conversation" and memory is not None:
            phrases = behaviour.conversation.end_phrases
            if is_ending(said, END_PHRASES if phrases is None else phrases):
                out.ended = True
                return out
        if on_transcript is not None:
            on_transcript(out)
        await _language(out, behaviour, said, memory)
        await _answer(router, route, out, sink, memory=memory, satellite_id=satellite_id,
                      satellite_name=satellite_name, wake_word=wake_word, said=said,
                      audio_seconds=len(audio) / 2 / 16000 if audio else 0.0,
                      speech_end=speech_end, on_handover=on_handover)
    except Failed as e:
        out.error = str(e)
    except asyncio.CancelledError:
        out.spoken_text = sink.spoken() or None
        raise
    except Exception as e:  # the promise in router.py's docstring
        log.exception("routing failed for satellite %s", satellite_id)
        out.error = f"internal: {type(e).__name__}: {e}"
    finally:
        out.timings_ms["total"] = round((time.monotonic() - start) * 1000, 1)
    out.spoken_text = sink.spoken() or None
    if isinstance(sink, Collect) and sink.pcm:
        out.reply_pcm48k = bytes(sink.pcm)
    router.log_outcome(satellite_id, wake_word, out)
    return out


async def _language(out: Outcome, behaviour, said: str, memory: Memory | None) -> None:
    """The language spoken and the one to answer in: the wake word's hint,
    or read from the transcript, with the conversation's language so far as
    the prior."""
    if behaviour.language:
        spoken, out.language_source = behaviour.language, "hint"
    else:
        if not lang.detector.loaded:
            await asyncio.to_thread(lang.detector.load)
        spoken = lang.tag(lang.detect(said, prior=memory.language if memory else None))
        out.language_source = "detected"
    out.language, out.reply_language = spoken, lang.reply_tag(spoken)
    if memory is not None:
        memory.language = spoken


async def _answer(router: Router, route: Route, out: Outcome, sink: Sink, *, memory: Memory | None,
                  satellite_id: str, satellite_name: str, wake_word: str, said: str,
                  audio_seconds: float, speech_end: float,
                  on_handover: Callable[[Route], None] | None) -> None:
    behaviour = route.behaviour
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    produced: list[str] = []

    def request(b, mem: Memory | None) -> Request:
        return Request(satellite_id=satellite_id, satellite_name=satellite_name,
                       wake_word=wake_word, text=said, audio_seconds=audio_seconds,
                       language=out.language, reply_language=out.reply_language,
                       mode=b.mode, history=mem.history() if mem else (),
                       state=mem.state if mem else {})

    async def produce(b, req: Request) -> None:
        dest = b.action.destination
        splitter = Sentences()
        t = time.monotonic()
        try:
            async with asyncio.timeout(getattr(dest, "timeout", 5.0)):
                async for piece in dest.answer(router.client, req):
                    if not produced:
                        out.timeline_ms["first_token"] = _ms(speech_end)
                    produced.append(piece)
                    for sentence in splitter.feed(piece):
                        await queue.put(sentence)
        except NotUnderstood as e:
            if produced:
                raise Failed(f"destination: {e}") from None
            raise _Declined(f"destination: {e}", e.speech) from None
        except (TimeoutError, httpx.TimeoutException):
            raise Failed(f"destination: no answer within {getattr(dest, 'timeout', 5.0):g} s") from None
        except DestinationError as e:
            raise Failed(f"destination: {e}") from None
        except httpx.HTTPError as e:
            raise Failed(f"destination: {type(e).__name__}: {e}") from None
        finally:
            out.timings_ms["destination"] = round(out.timings_ms.get("destination", 0.0)
                                                  + (time.monotonic() - t) * 1000, 1)
        for sentence in splitter.flush():
            await queue.put(sentence)
        out.timeline_ms["answer_done"] = _ms(speech_end)

    async def speak(voice: str) -> None:
        first = True
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            batch, done = [sentence], False
            while not first and not queue.empty() and len(" ".join(batch)) < BATCH_CHARS:
                nxt = queue.get_nowait()
                if nxt is None:
                    done = True
                    break
                batch.append(nxt)
            first = False
            words = clip(" ".join(batch))
            pcm = await router.stage(out, "tts", router.tts_timeout, router.synthesise(words, voice))
            if await sink.play(pcm, words):
                out.audio_bytes += len(pcm)
                out.timeline_ms.setdefault("first_audio", _ms(speech_end))
            if done:
                return

    async def run(r: Route, mem: Memory | None) -> None:
        b = r.behaviour
        out.reply_to = router.target(b, satellite_id)
        out.voice = router.voice_for(b, out.reply_language) if out.reply_to else None
        speaking = out.reply_to is not None
        req = request(b, mem)
        if not speaking:
            await produce(b, req)
            return
        speaker = asyncio.create_task(speak(out.voice))
        try:
            await produce(b, req)
            await queue.put(None)
            await speaker
        except BaseException:
            speaker.cancel()
            await asyncio.gather(speaker, return_exceptions=True)
            raise

    try:
        try:
            await run(route, memory)
        except (_Declined, Failed) as e:
            fallback = behaviour.action.fallback
            target = router.rules.named(fallback) if fallback and not produced else None
            if target is None or target.behaviour.mode != "conversation":
                if fallback and not produced:
                    log.warning("routing: %s names fallback %r, which is not a conversation "
                                "wake word", route.id, fallback)
                if isinstance(e, _Declined) and e.speech:
                    # No one to hand over to: what the destination said about
                    # not understanding is its answer, as it always was.
                    await _say(router, out, sink, e.speech, route, satellite_id, speech_end)
                    return
                raise Failed(str(e)) from None
            # A fresh conversation, in the fallback word's own language hint
            # (if it has one) and voice; the transcript is the same.
            out.handed_over_to = target.id
            if on_handover is not None:
                on_handover(target)
            fresh = memory if memory is not None else Memory()
            if target.behaviour.language:
                out.language, out.language_source = target.behaviour.language, "hint"
                out.reply_language = lang.reply_tag(out.language)
            fresh.language = out.language
            await run(target, fresh)
        if produced:
            out.reply_text = speakable("".join(produced)) or None
        await sink.finish()
        if out.audio_bytes:
            out.timeline_ms["reply_done"] = _ms(speech_end)
    finally:
        if produced and out.reply_text is None:
            out.reply_text = speakable("".join(produced)) or None


class _Declined(Failed):
    """The destination did not understand. `speech` is what it said."""

    def __init__(self, message: str, speech: str | None):
        super().__init__(message)
        self.speech = speech


async def _say(router: Router, out: Outcome, sink: Sink, text: str, route: Route,
               satellite_id: str, speech_end: float) -> None:
    out.reply_text = text
    out.reply_to = router.target(route.behaviour, satellite_id)
    if out.reply_to is None:
        return
    out.voice = router.voice_for(route.behaviour, out.reply_language)
    pcm = await router.stage(out, "tts", router.tts_timeout, router.synthesise(text, out.voice))
    if await sink.play(pcm, text):
        out.audio_bytes += len(pcm)
        out.timeline_ms.setdefault("first_audio", _ms(speech_end))
    await sink.finish()
    if out.audio_bytes:
        out.timeline_ms["reply_done"] = _ms(speech_end)
