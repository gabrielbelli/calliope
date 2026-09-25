"""Routing: what happens after a wake word.

A WAKE WORD SAYS WHAT IT DOES. Each entry in wake_words.json carries, beside
its model, threshold and satellites, a Behaviour: its mode, an optional
language hint, and its action (a destination and where the reply is played).
wakewords_config.py holds the entries; this module holds the models they are
checked against, and the services a turn uses.

    mode "command"       the words after the wake word go to the action, once
    mode "conversation"  the same, then the satellite keeps listening for
                         follow-ups without the wake word (dialogue.py, main.py)
    mode "trigger"       the wake word is the whole command ("lumos"): the hub
                         publishes that it was heard, and Home Assistant's own
                         automations decide what it does. No action, no STT.

A turn (dialogue.run_turn) is:
  1. STT   the utterance as a WAV to SATELLITES_STT_URL/v1/audio/transcriptions
  2. the language, from the transcript or the word's hint (language.py)
  3. the destination (destinations.py), streamed where it can be
  4. TTS   sentence by sentence to SATELLITES_TTS_URL/v1/audio/speech as 24 kHz
           pcm, resampled to the 48 kHz the Korvo plays, in the voice of that
           language

handle() and handle_text() run one turn and collect the audio; they never play
it, never light a ring and never touch a Session. That keeps the one promise
about lights (a satellite with lights_enabled false is never sent "lights")
out of this module entirely: whoever plays the audio owns that check.

handle() NEVER RAISES. It runs on the audio path of a device someone is
standing next to; an exception out of it would kill whatever task called it
and leave the satellite deaf until a reconnect. Every failure, down to a bug
in this file, becomes Outcome.error, and every external call has a timeout.

rules.json, the routing of 2026-09-25 and before, is still read. Rules is a
provider of Behaviours like the wake word entries are: the first rule, in file
order, whose wake word and satellites match wins. main.py reads it once, to
give each wake word that has no action yet the one its rule would have run
(wakewords_config.migrate), and routes by the wake word entries from then on;
PUT /satellites/routing answers 409 there, naming the route that replaced it.

    GET  /satellites/routing       what each wake word does, which secret env
                                   vars are set (never their values), the STT
                                   and TTS URLs and engine, warnings
    PUT  /satellites/routing       409 behind the hub: see PUT /satellites/wake-words
    POST /satellites/routing/test  {"satellite","wake_word","text"}: skips STT,
                                   runs the word's action and TTS, returns the
                                   Outcome as JSON and plays nothing

THESE ROUTES MUST BE INCLUDED BEFORE main.py's /satellites/{nid} routes:
FastAPI matches in registration order and GET /satellites/{nid} would otherwise
take "routing" for a satellite id and answer 404.

Where the URLs in an action may point is not filtered; destinations.py explains
why the trust boundary is who may write the configuration, not what it holds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends
from pydantic import (AliasChoices, BaseModel, ConfigDict, Field, StringConstraints,
                      field_validator, model_validator)
from voice_common.errors import ApiError

from . import audio
from . import language as lang
from .destinations import Destination, DestinationError, Echo

log = logging.getLogger("voice-satellites.router")

MIC_RATE = 16000       # what handle() is given: the front end's mono output
TTS_RATE = 24000       # Kokoro's pcm (voice_common.audio.SAMPLE_RATE)
SPEAKER_RATE = 48000   # what the Korvo plays; Session.spk_rate for others
# tts-stack refuses input over its schema's 4096 characters with a 400, so a
# long LLM answer is cut at a sentence end below that rather than lost whole.
MAX_TTS_CHARS = 4096
# How long to wait for stt-stack's /health when the engine is not known yet.
ENGINE_PROBE_S = 3.0

RULE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
WAKE_WORD = r"^(\*|[A-Za-z0-9][A-Za-z0-9 _.-]{0,63})$"
# BCP 47 as HA writes it ("en", "pt-BR"), or "auto".
LANGUAGE = r"^([a-z]{2,3}(-[A-Za-z0-9]{2,8})*|auto)$"

Mode = Literal["command", "conversation", "trigger"]

# ref -> (satellite id, satellite name), or None when no single satellite
# matches. main.py supplies one built on Hub.find; without it a ref is taken as
# both.
Lookup = Callable[[str], tuple[str, str] | None]


# ---- what a wake word does ------------------------------------------------------


def _wake_key(word: str) -> str:
    """"Hey Jarvis", "hey_jarvis" and "hey-jarvis" are one wake word: model
    files use underscores and people type spaces."""
    import re
    return re.sub(r"[\s_.-]+", "_", word.strip().casefold())


def _mac_key(ref: str) -> str:
    import re
    return re.sub(r"[:-]", "", ref.strip().lower())


def strip_wake_phrase(text: str, wake_word: str) -> str:
    """"Jarvis, what time is it" -> "what time is it". The listener rewinds
    by the detector's latency so the command is not lost, and the price is
    that the tail of the wake word can lead the transcript -- and misheard:
    Parakeet on orko returned "Harvis, what time is it?" for the en_us fixture.
    So a LEADING run of words that each resemble the wake word's own words, in
    order, is removed (difflib ratio >= 0.6: "harvis" is 0.83 of "jarvis").
    A command that merely mentions the name further in is left alone."""
    import difflib
    import re

    words = [w for w in _wake_key(wake_word).split("_") if w]
    if not words or wake_word == "ptt":
        return text
    tokens = list(re.finditer(r"[\w']+", text))
    like = lambda a, b: difflib.SequenceMatcher(None, a.casefold(), b).ratio() >= 0.6
    for start in range(len(words)):  # the whole phrase, then shorter tails of it
        tail = words[start:]
        if len(tokens) >= len(tail) and all(like(t.group(), w) for t, w in zip(tokens, tail)):
            # Nothing but the wake word: there is no command, and "Hey Jarvis."
            # must not reach a destination as if it were one.
            return text[tokens[len(tail)].start():] if len(tokens) > len(tail) else ""
    return text


EndPhrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class ConversationSettings(BaseModel):
    """How a conversation carries on after each reply."""

    model_config = ConfigDict(extra="forbid")

    # How long the satellite listens for the next turn, without the wake word,
    # after a reply has finished playing. Silence for this long ends it.
    follow_up_s: float = Field(default=8.0, ge=1, le=60)
    # The pause that ends a follow-up turn. Shorter than the first command's:
    # someone mid-conversation starts talking at once, and each turn waits it.
    silence_ms: int = Field(default=600, ge=200, le=3000)
    # Said on their own, these end the conversation. None: dialogue.END_PHRASES
    # (English and Brazilian Portuguese); [] turns them off.
    end_phrases: list[EndPhrase] | None = Field(default=None, max_length=64)


class TriggerSettings(BaseModel):
    """For a trigger word, which has no second step to catch a false
    detection: what the satellite does to show it was heard, and how soon the
    same word may fire again."""

    model_config = ConfigDict(extra="forbid")

    # "earcon": the satellite's own "done" and a flash of the ring, each only
    # where the speaker or the lights are on; "none": nothing seen or heard.
    feedback: Literal["none", "earcon"] = "earcon"
    # One utterance can score over the threshold in several frames, and the
    # detector's own refractory window is 1.5 s; this is the word's.
    cooldown_s: float = Field(default=3.0, ge=0, le=600)
    # Heard during a conversation, a trigger fires and the conversation goes
    # on; true ends the conversation as well.
    ends_conversation: bool = False


class Action(BaseModel):
    """Where a wake word's words go, and where the answer is played."""

    model_config = ConfigDict(extra="forbid")

    destination: Destination = Field(default_factory=lambda: Echo(type="echo"))
    # "same" (the satellite that heard), "none" (act, say nothing) or a
    # satellite's id or name, for "ask in the bedroom, answer in the kitchen".
    reply_to: str = Field(default="same", min_length=1, max_length=64)
    # A Kokoro voice. Unset: the voice of the language spoken (language.py).
    voice: str | None = Field(default=None, max_length=64)
    # Command mode only: another wake word whose conversation takes the same
    # transcript when this destination fails or does not understand.
    fallback: str | None = Field(default=None, max_length=64)

    @field_validator("reply_to")
    @classmethod
    def _keywords_in_one_case(cls, v: str) -> str:
        return v.lower() if v.lower() in ("same", "none") else v


class Behaviour(BaseModel):
    """Everything a wake word does once it is heard: a wake word entry
    without its name, threshold and satellites. Push-to-talk has one too."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = "command"
    # A hint: the language this wake word is spoken in. Unset (or "auto"), the
    # language is read from each transcript.
    language: str | None = Field(default=None, pattern=LANGUAGE)
    # What a command or a conversation does with its words. A trigger has
    # none: Home Assistant decides what it does.
    action: Action | None = None
    # The pause that ends the command after the wake word.
    silence_ms: int = Field(default=800, ge=200, le=3000)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    trigger: TriggerSettings = Field(default_factory=TriggerSettings)

    @field_validator("language")
    @classmethod
    def _auto_is_unset(cls, v: str | None) -> str | None:
        return None if v in (None, "auto") else v

    @model_validator(mode="after")
    def _fits_the_mode(self) -> Behaviour:
        if self.mode == "trigger":
            if self.action is not None:
                raise ValueError("a trigger word has no action: the hub publishes that it was "
                                 "heard (a \"triggered\" event) and Home Assistant's automation "
                                 "decides what it does; leave \"action\" out")
        elif self.action is None:
            raise ValueError(f"a {self.mode} needs an action: where its words go")
        elif self.action.fallback and self.mode != "command":
            raise ValueError("only a command hands over to a fallback; a conversation already "
                             "is one")
        return self

    @property
    def speaks(self) -> bool:
        return self.action is not None and self.action.reply_to != "none"


@dataclass(frozen=True)
class Route:
    """A Behaviour and the name it was found by: a wake word's, or a rule's
    id. Events carry the name as rule_id."""

    id: str
    behaviour: Behaviour


class Actions(Protocol):
    """Where the Router finds what a wake word does: Rules (rules.json) or,
    in the hub, wakewords_config.WordActions."""

    editable: bool
    load_error: str | None

    def find(self, satellite_id: str, satellite_name: str, wake_word: str) -> Route | None: ...

    def named(self, name: str) -> Route | None: ...

    def warnings(self, lookup: Lookup | None = None) -> list[str]: ...

    def env_vars(self) -> dict[str, bool]: ...

    def listing(self) -> list[dict]: ...


def env_status(destinations) -> dict[str, bool]:
    """Each env var the destinations read, and whether it is set. Only ever
    a boolean: this goes out over GET, and a value, a prefix or even a
    length would be a start on the secret."""
    names = sorted({n for d in destinations for n in d.env_vars()})
    return {n: bool(os.environ.get(n)) for n in names}


# ---- rules.json: the routing before wake words said what they do ------------------


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=RULE_ID)
    wake_word: str = Field(default="*", pattern=WAKE_WORD)
    # Satellite ids (MAC, colons optional) or names. Empty means every
    # satellite. "nodes" is the same list under the feature's name until
    # 2026-09-25: rules.json on a hub's volume may still say it, and a rule
    # that stopped loading would switch routing off for the whole house.
    satellites: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=64,
        validation_alias=AliasChoices("satellites", "nodes"))
    destination: Destination
    reply_to: str = Field(default="same", min_length=1, max_length=64)
    language: str | None = Field(default=None, pattern=LANGUAGE)
    voice: str | None = Field(default=None, max_length=64)  # unset: the language's voice

    @field_validator("reply_to")
    @classmethod
    def _keywords_in_one_case(cls, v: str) -> str:
        return v.lower() if v.lower() in ("same", "none") else v

    def behaviour(self) -> Behaviour:
        """What this rule does, as a command: rules had no other mode."""
        return Behaviour(mode="command", language=self.language, action=Action(
            destination=self.destination, reply_to=self.reply_to, voice=self.voice))

    def matches(self, satellite_id: str, satellite_name: str, wake_word: str) -> bool:
        if self.wake_word != "*" and _wake_key(self.wake_word) != _wake_key(wake_word):
            return False
        if not self.satellites:
            return True
        nid, name = _mac_key(satellite_id), satellite_name.casefold()
        return any(_mac_key(n) == nid or (name and n.casefold() == name)
                   for n in self.satellites)

    def covers(self, other: Rule) -> bool:
        """True when every utterance `other` would match, this matches too."""
        if self.wake_word != "*" and _wake_key(self.wake_word) != _wake_key(other.wake_word):
            return False
        if not self.satellites:
            return True
        mine = ({n.casefold() for n in self.satellites}
                | {_mac_key(n) for n in self.satellites})
        return bool(other.satellites) and all(
            n.casefold() in mine or _mac_key(n) in mine for n in other.satellites)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rules: list[Rule] = Field(max_length=256)

    @model_validator(mode="after")
    def _unique_ids(self) -> RuleSet:
        seen: set[str] = set()
        for r in self.rules:
            if r.id in seen:
                raise ValueError(f"two rules have the id {r.id!r}")
            seen.add(r.id)
        return self


def default_ruleset() -> RuleSet:
    """Any wake word on any satellite is echoed back to it: a fresh hub
    proves the whole loop (microphone, STT, TTS, speaker) before Home
    Assistant exists. No language: the echo answers in the one spoken."""
    return RuleSet(rules=[Rule(id="default", wake_word="*", destination=Echo(type="echo"),
                               reply_to="same")])


class Rules:
    """rules.json in the data directory. Absent, the default ruleset applies
    and nothing is written until someone saves."""

    FILE = "rules.json"
    editable = True

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / self.FILE
        self.load_error: str | None = None
        self.ruleset = self._load()

    @property
    def rules(self) -> list[Rule]:
        return self.ruleset.rules

    def _load(self) -> RuleSet:
        if not self.path.exists():
            return default_ruleset()
        try:
            return RuleSet.model_validate_json(self.path.read_text())
        except (OSError, ValueError) as e:  # pydantic's ValidationError is a ValueError
            # Routing goes OFF, not back to the echo default. A hand edit with
            # a typo must not turn a house that was talking to Home Assistant
            # into one that repeats every sentence aloud, at night included.
            # The file is left as it is, for the operator to fix or replace.
            self.load_error = (f"{self.path} could not be loaded, so no rule applies until "
                               f"it is fixed or replaced: {str(e)[:500]}")
            log.error("%s", self.load_error)
            return RuleSet(rules=[])

    def replace(self, ruleset: RuleSet) -> None:
        self._write(ruleset)
        self.ruleset = ruleset
        self.load_error = None

    def _write(self, ruleset: RuleSet) -> None:
        # Every field, None included: dropping a None on the way out would
        # bring a default back on the way in (api_key_env: null would reload
        # as SATELLITES_LLM_API_KEY).
        body = json.dumps(ruleset.model_dump(mode="json"), indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temporary name, so two PUTs at once cannot write into the
        # same half-finished file, and fsync before the rename, so a power cut
        # leaves the old rules or the new ones and never an empty file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".rules.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def match(self, satellite_id: str, satellite_name: str, wake_word: str) -> Rule | None:
        return next((r for r in self.rules
                     if r.matches(satellite_id, satellite_name, wake_word)), None)

    def find(self, satellite_id: str, satellite_name: str, wake_word: str) -> Route | None:
        rule = self.match(satellite_id, satellite_name, wake_word)
        return Route(rule.id, rule.behaviour()) if rule else None

    def named(self, name: str) -> Route | None:
        rule = next((r for r in self.rules if r.id == name), None)
        return Route(rule.id, rule.behaviour()) if rule else None

    def for_word(self, word: str) -> tuple[Rule | None, list[Rule]]:
        """The rule a wake word heard on any satellite would take (the first
        with no satellite list, else the first at all), and the other rules
        for that word that name satellites: what a migration to per-word
        actions cannot carry over."""
        mine = [r for r in self.rules if r.wake_word == "*" or _wake_key(r.wake_word) == _wake_key(word)]
        chosen = next((r for r in mine if not r.satellites), mine[0] if mine else None)
        return chosen, [r for r in mine if r is not chosen and r.satellites]

    def warnings(self, lookup: Lookup | None = None) -> list[str]:
        out = []
        for j, later in enumerate(self.rules):
            for earlier in self.rules[:j]:
                if earlier.covers(later):
                    out.append(f"rule {later.id!r} can never match: rule {earlier.id!r} "
                               "above it matches everything it would")
                    break
            if lookup and later.reply_to not in ("same", "none") and lookup(later.reply_to) is None:
                out.append(f"rule {later.id!r} replies to {later.reply_to!r}, "
                           "which is not a known satellite")
        return out

    def env_vars(self) -> dict[str, bool]:
        return env_status(r.destination for r in self.rules)

    def listing(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self.rules]


# ---- the pipeline -------------------------------------------------------------


@dataclass
class Outcome:
    rule_id: str | None = None
    transcript: str | None = None
    reply_text: str | None = None
    reply_pcm48k: bytes | None = field(default=None, repr=False)
    reply_to: str | None = None  # a satellite id when it could be resolved, else the ref as written
    error: str | None = None
    # Per stage, so "the assistant is slow" can be pinned on STT, the
    # destination or TTS from one log line instead of a guess.
    timings_ms: dict[str, float] = field(default_factory=dict)
    # From the end of speech: stt_done, first_token, first_audio,
    # answer_done, reply_done (dialogue.py).
    timeline_ms: dict[str, float] = field(default_factory=dict)
    mode: str = "command"
    language: str | None = None          # what was spoken, BCP 47
    reply_language: str | None = None    # what the answer is in
    language_source: str | None = None   # "detected" or "hint"
    voice: str | None = None
    handed_over_to: str | None = None    # the fallback that answered instead
    spoken_text: str | None = None       # as much of the reply as was played
    interrupted: bool = False
    ended: bool = False                  # an ending phrase: nothing was routed
    audio_bytes: int = 0                 # reply audio handed to the player

    def as_json(self) -> dict:
        n = len(self.reply_pcm48k) if self.reply_pcm48k else self.audio_bytes
        return {"rule_id": self.rule_id, "mode": self.mode, "transcript": self.transcript,
                "language": self.language, "reply_language": self.reply_language,
                "language_source": self.language_source, "voice": self.voice,
                "reply_text": self.reply_text, "reply_to": self.reply_to,
                "handed_over_to": self.handed_over_to, "error": self.error,
                "timings_ms": self.timings_ms, "timeline_ms": self.timeline_ms,
                "reply_audio_bytes": n,
                "reply_audio_seconds": round(n / 2 / SPEAKER_RATE, 3)}


class Failed(Exception):
    """A stage failed; the message is already the sentence for Outcome.error."""


def clip(text: str, limit: int = MAX_TTS_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    return cut[:end + 1] if end > limit // 2 else cut


class Router:
    def __init__(self, rules: Actions, *, stt_url: str | None = None, tts_url: str | None = None,
                 voice: str | None = None, client: httpx.AsyncClient | None = None,
                 lookup: Lookup | None = None, stt_timeout: float = 30.0,
                 tts_timeout: float = 30.0):
        self.rules = rules
        env = os.environ.get
        self.stt_url = (stt_url if stt_url is not None
                        else env("SATELLITES_STT_URL", "")).rstrip("/")
        self.tts_url = (tts_url if tts_url is not None
                        else env("SATELLITES_TTS_URL", "")).rstrip("/")
        self.voice = voice or env("SATELLITES_TTS_VOICE") or "bm_george"
        self.lookup = lookup
        self.stt_timeout, self.tts_timeout = stt_timeout, tts_timeout
        # Which engine stt-stack runs ("parakeet" or "whisper"), from the
        # x-stt-engine header every /v1 answer carries, or its /health when a
        # hint is to be sent before any answer has been seen. A language hint
        # reaches STT only under Whisper: Parakeet refuses the field (400).
        self.stt_engine: str | None = None
        self._engine_asked = False
        self._own_client = client is None
        # follow_redirects stays False (httpx's default, stated so it is not
        # changed casually): a redirect would carry a destination's bearer
        # token to a host the action never named.
        self.client = client or httpx.AsyncClient(follow_redirects=False)

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    # -- entry points ---------------------------------------------------------

    def find(self, satellite_id: str, satellite_name: str, wake_word: str) -> Route | None:
        return self.rules.find(satellite_id, satellite_name, wake_word)

    async def handle(self, satellite_id: str, satellite_name: str, wake_word: str,
                     utterance_pcm16k: bytes) -> Outcome:
        """One command, from its audio, with the reply collected rather than
        played: what a test or an injected clip without a satellite needs."""
        from . import dialogue

        route = self.find(satellite_id, satellite_name, wake_word)
        out = await dialogue.run_turn(self, route, satellite_id=satellite_id,
                                      satellite_name=satellite_name, wake_word=wake_word,
                                      audio=utterance_pcm16k, sink=dialogue.Collect())
        return out

    async def handle_text(self, satellite: str, wake_word: str, text: str) -> Outcome:
        """The pipeline from a typed sentence: no STT, and nothing is played.
        Behind POST /satellites/routing/test."""
        from . import dialogue

        found = self.lookup(satellite) if self.lookup else None
        satellite_id, satellite_name = found or (satellite, satellite)
        route = self.find(satellite_id, satellite_name, wake_word)
        return await dialogue.run_turn(self, route, satellite_id=satellite_id,
                                       satellite_name=satellite_name, wake_word=wake_word,
                                       text=text, sink=dialogue.Collect())

    def describe(self) -> dict:
        return {"rules": self.rules.listing(),
                "env": self.rules.env_vars(),
                "services": {"stt": self.stt_url or None, "tts": self.tts_url or None,
                             "voice": self.voice, "stt_engine": self.stt_engine},
                "editable": self.rules.editable,
                "warnings": self.rules.warnings(self.lookup),
                "load_error": self.rules.load_error}

    # -- stages ---------------------------------------------------------------

    def target(self, behaviour: Behaviour, satellite_id: str) -> str | None:
        if behaviour.action is None:
            return None
        reply_to = behaviour.action.reply_to
        if reply_to == "same":
            return satellite_id
        if reply_to == "none":
            return None
        found = self.lookup(reply_to) if self.lookup else None
        return found[0] if found else reply_to

    def voice_for(self, behaviour: Behaviour, reply_language: str | None) -> str:
        explicit = behaviour.action.voice if behaviour.action else None
        return explicit or lang.voice_for(reply_language or lang.DEFAULT, self.voice)

    async def stage(self, out: Outcome, name: str, timeout: float, work) -> object:
        """Run one external call under a hard ceiling. httpx's own timeout is
        per read, so a server that trickles a byte a second never trips it;
        asyncio.timeout bounds the whole call."""
        t = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                return await work
        except (TimeoutError, httpx.TimeoutException):
            raise Failed(f"{name}: no answer within {timeout:g} s") from None
        except DestinationError as e:
            raise Failed(f"{name}: {e}") from None
        except httpx.HTTPError as e:
            raise Failed(f"{name}: {type(e).__name__}: {e}") from None
        finally:
            out.timings_ms[name] = round(out.timings_ms.get(name, 0.0)
                                         + (time.monotonic() - t) * 1000, 1)

    async def _engine(self) -> str | None:
        """The STT engine, asked of /health once when no answer has named it
        yet. None when that fails: then no hint is sent, which every engine
        takes."""
        if self.stt_engine is None and self.stt_url and not self._engine_asked:
            # Once per start: every transcription answer names the engine too.
            self._engine_asked = True
            try:
                r = await self.client.get(f"{self.stt_url}/health", timeout=ENGINE_PROBE_S)
                model = r.json().get("model") if r.status_code == 200 else None
                if isinstance(model, str) and model:
                    self.stt_engine = model.lower()
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                log.info("routing: could not ask stt-stack which engine it runs (%s); "
                         "no language hint is sent", type(e).__name__)
        return self.stt_engine

    async def transcribe(self, pcm: bytes, hint: str | None = None) -> str:
        """The transcript. `hint` (a wake word's language) is sent only to an
        engine that takes one: Whisper does, and Parakeet refuses the field
        with a 400 and detects the language itself."""
        if not self.stt_url:
            raise DestinationError("SATELLITES_STT_URL is not set, so nothing can be transcribed")
        pcm = pcm[:len(pcm) & ~1]  # whole samples only
        data = {"model": "whisper-1", "response_format": "json"}
        if hint and await self._engine() == "whisper":
            data["language"] = lang.primary(hint)  # Whisper takes ISO 639-1
        r = await self.client.post(
            f"{self.stt_url}/v1/audio/transcriptions", data=data, timeout=self.stt_timeout,
            files={"file": ("utterance.wav", audio.wav(pcm, MIC_RATE, 1), "audio/wav")})
        engine = r.headers.get("x-stt-engine")
        if engine:
            self.stt_engine = engine.lower()
        if r.status_code != 200:
            raise DestinationError(f"STT answered {r.status_code}: {r.text[:200].strip()}")
        try:
            text = r.json()["text"]
        except (ValueError, KeyError, TypeError):
            raise DestinationError("STT answered without a \"text\" field") from None
        if not isinstance(text, str):
            raise DestinationError("STT answered a \"text\" that is not a string")
        return text.strip()

    async def synthesise(self, text: str, voice: str) -> bytes:
        if not self.tts_url:
            raise DestinationError("SATELLITES_TTS_URL is not set, so the reply cannot be spoken")
        r = await self.client.post(f"{self.tts_url}/v1/audio/speech", timeout=self.tts_timeout,
                                   json={"model": "kokoro", "voice": voice,
                                         "input": clip(text), "response_format": "pcm"})
        if r.status_code != 200:
            raise DestinationError(f"TTS answered {r.status_code}: {r.text[:200].strip()}")
        pcm = r.content[:len(r.content) & ~1]
        return audio.resample(pcm, TTS_RATE, SPEAKER_RATE)

    def log_outcome(self, satellite_id: str, wake_word: str, out: Outcome) -> None:
        if out.rule_id is None:
            return  # no action: already said
        if out.error:
            result = out.error
        elif out.audio_bytes or out.reply_pcm48k:
            result = f"reply for {out.reply_to}"
        else:
            result = "no reply"
        stages = ", ".join(f"{k} {v:.0f}" for k, v in out.timings_ms.items() if k != "total")
        log.info("routing: satellite %s wake %r rule %s (%s, %s): %s, %.0f ms (%s)", satellite_id,
                 wake_word, out.rule_id, out.mode, out.language, result,
                 out.timings_ms.get("total", 0), stages)
        # What was said stays at DEBUG: INFO logs are kept and shipped, and a
        # household's sentences do not belong in them by default.
        log.debug("routing: satellite %s heard %r, replied %r", satellite_id, out.transcript,
                  out.reply_text)


# ---- API ----------------------------------------------------------------------

_current: Router | None = None


def configure(router: Router) -> Router:
    """Set the Router the routes use. main.py's lifespan should call this, so
    each app start (and each test's TestClient) gets its own data dir."""
    global _current
    _current = router
    return router


def current() -> Router:
    """The configured Router, or one built from the environment on first use."""
    global _current
    if _current is None:
        _current = Router(Rules(Path(os.environ.get("SATELLITES_DATA_DIR", "/data"))))
    return _current


class TryBody(BaseModel):
    satellite: str = Field(min_length=1, max_length=64)
    wake_word: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)


routes = APIRouter()
# A dependency rather than the global read directly, so a test can hand the
# routes its own Router with app.dependency_overrides[current].
CurrentRouter = Annotated[Router, Depends(current)]


@routes.get("/satellites/routing")
async def get_routing(router: CurrentRouter) -> dict:
    return router.describe()


@routes.put("/satellites/routing")
async def put_routing(body: RuleSet, router: CurrentRouter) -> dict:
    if not router.rules.editable:
        raise ApiError(409, "routing is set on each wake word now: its mode, language and "
                            "action are part of the entry in PUT /satellites/wake-words",
                       code="routing_per_wake_word")
    try:
        router.rules.replace(body)
    except OSError as e:
        raise ApiError(500, f"could not write {router.rules.path.name}: {e}",
                       type_="server_error") from None
    log.info("routing: %d rules saved", len(body.rules))
    return router.describe()


@routes.post("/satellites/routing/test")
async def try_routing(body: TryBody, router: CurrentRouter) -> dict:
    return (await router.handle_text(body.satellite, body.wake_word, body.text)).as_json()
