"""Routing: what happens after a wake word.

    handle(satellite_id, satellite_name, wake_word, utterance_pcm16k) -> Outcome
      1. the first rule, in file order, whose wake word and satellites match
      2. STT   the utterance as a WAV to
               SATELLITES_STT_URL/v1/audio/transcriptions
      3. the rule's destination (destinations.py) with the transcript
      4. TTS   the reply to SATELLITES_TTS_URL/v1/audio/speech as 24 kHz pcm,
               resampled to the 48 kHz the Korvo plays

handle() returns audio and says where it should go; it never plays it, never
lights a ring and never touches a Session. That keeps the one promise about
lights (a satellite with lights_enabled false is never sent "lights") out of
this module entirely: whoever plays the Outcome owns that check.

handle() NEVER RAISES. It runs on the audio path of a device someone is
standing next to; an exception out of it would kill whatever task called it
and leave the satellite deaf until a reconnect. Every failure, down to a bug
in this file, becomes Outcome.error, and every external call has a timeout.

PRECEDENCE IS FILE ORDER, nothing cleverer. The first rule whose wake word and
satellite list both match wins, so a specific rule has to sit above a
catch-all. A rule that an earlier one makes unreachable is reported in GET and
PUT /satellites/routing as a warning rather than refused, because moving a
catch-all above something is a legitimate way to switch it off for an evening.

    GET  /satellites/routing       the rules, which secret env vars are set
                                   (never their values), STT/TTS URLs, warnings
    PUT  /satellites/routing       replace the whole ruleset, validated
    POST /satellites/routing/test  {"satellite","wake_word","text"}: skips STT,
                                   runs the destination and TTS, returns the
                                   Outcome as JSON and plays nothing

THESE ROUTES MUST BE INCLUDED BEFORE main.py's /satellites/{nid} routes:
FastAPI matches in registration order and GET /satellites/{nid} would otherwise
take "routing" for a satellite id and answer 404.

Where the URLs in a rule may point is not filtered; destinations.py explains
why the trust boundary is who may PUT the rules, not what they contain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import (AliasChoices, BaseModel, ConfigDict, Field, field_validator,
                      model_validator)
from voice_common.errors import ApiError

from . import audio
from .destinations import Destination, DestinationError, Echo, Request

log = logging.getLogger("voice-satellites.router")

MIC_RATE = 16000       # what handle() is given: the front end's mono output
TTS_RATE = 24000       # Kokoro's pcm (voice_common.audio.SAMPLE_RATE)
SPEAKER_RATE = 48000   # what the Korvo plays; Session.spk_rate for others
# tts-stack refuses input over its schema's 4096 characters with a 400, so a
# long LLM answer is cut at a sentence end below that rather than lost whole.
MAX_TTS_CHARS = 4096

RULE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
WAKE_WORD = r"^(\*|[A-Za-z0-9][A-Za-z0-9 _.-]{0,63})$"
# BCP 47 as HA writes it ("en", "pt-BR"). STT is given only the primary
# subtag, which is the ISO 639-1 code Whisper takes.
LANGUAGE = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"

# ref -> (satellite id, satellite name), or None when no single satellite
# matches. main.py supplies one built on Hub.find; without it a ref is taken as
# both.
Lookup = Callable[[str], tuple[str, str] | None]


# ---- rules ------------------------------------------------------------------


def _wake_key(word: str) -> str:
    """"Hey Jarvis", "hey_jarvis" and "hey-jarvis" are one wake word: model
    files use underscores and people type spaces."""
    return re.sub(r"[\s_.-]+", "_", word.strip().casefold())


def _mac_key(ref: str) -> str:
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
    # "same" (the satellite that heard), "none" (act, say nothing) or a
    # satellite's id or name, for "ask in the bedroom, answer in the kitchen".
    reply_to: str = Field(default="same", min_length=1, max_length=64)
    language: str | None = Field(default=None, pattern=LANGUAGE)
    voice: str | None = Field(default=None, max_length=64)  # unset: SATELLITES_TTS_VOICE

    @field_validator("reply_to")
    @classmethod
    def _keywords_in_one_case(cls, v: str) -> str:
        return v.lower() if v.lower() in ("same", "none") else v

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
    Assistant exists."""
    return RuleSet(rules=[Rule(id="default", wake_word="*", destination=Echo(type="echo"),
                               reply_to="same")])


class Rules:
    """rules.json in the data directory. Absent, the default ruleset applies
    and nothing is written until someone saves."""

    FILE = "rules.json"

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
        """Each env var a destination reads, and whether it is set. Only ever
        a boolean: this goes out over GET, and a value, a prefix or even a
        length would be a start on the secret."""
        names = sorted({n for r in self.rules for n in r.destination.env_vars()})
        return {n: bool(os.environ.get(n)) for n in names}


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

    def as_json(self) -> dict:
        n = len(self.reply_pcm48k) if self.reply_pcm48k else 0
        return {"rule_id": self.rule_id, "transcript": self.transcript,
                "reply_text": self.reply_text, "reply_to": self.reply_to,
                "error": self.error, "timings_ms": self.timings_ms,
                "reply_audio_bytes": n,
                "reply_audio_seconds": round(n / 2 / SPEAKER_RATE, 3)}


class _Failed(Exception):
    """A stage failed; the message is already the sentence for Outcome.error."""


def _clip(text: str, limit: int = MAX_TTS_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    return cut[:end + 1] if end > limit // 2 else cut


class Router:
    def __init__(self, rules: Rules, *, stt_url: str | None = None, tts_url: str | None = None,
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
        self._own_client = client is None
        # follow_redirects stays False (httpx's default, stated so it is not
        # changed casually): a redirect would carry a destination's bearer
        # token to a host the rule never named.
        self.client = client or httpx.AsyncClient(follow_redirects=False)

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    # -- entry points ---------------------------------------------------------

    async def handle(self, satellite_id: str, satellite_name: str, wake_word: str,
                     utterance_pcm16k: bytes) -> Outcome:
        out = Outcome()
        start = time.monotonic()
        try:
            rule = self._match(out, satellite_id, satellite_name, wake_word)
            if rule is None:
                return out
            text = await self._stage(out, "stt", self.stt_timeout,
                                     self._transcribe(utterance_pcm16k, rule.language))
            text = strip_wake_phrase(text, wake_word)
            out.transcript = text
            if not text.strip():
                out.error = "stt: nothing was heard (the transcript is empty)"
                return out
            await self._respond(out, rule, Request(
                satellite_id=satellite_id, satellite_name=satellite_name, wake_word=wake_word,
                text=text, audio_seconds=len(utterance_pcm16k) / 2 / MIC_RATE,
                language=rule.language))
        except _Failed as e:
            out.error = str(e)
        except Exception as e:  # the promise in the module docstring
            log.exception("routing failed for satellite %s", satellite_id)
            out.error = f"internal: {type(e).__name__}: {e}"
        finally:
            out.timings_ms["total"] = round((time.monotonic() - start) * 1000, 1)
            self._log(satellite_id, wake_word, out)
        return out

    async def handle_text(self, satellite: str, wake_word: str, text: str) -> Outcome:
        """The pipeline from a typed sentence: no STT, and nothing is played.
        Behind POST /satellites/routing/test."""
        out = Outcome()
        start = time.monotonic()
        found = self.lookup(satellite) if self.lookup else None
        satellite_id, satellite_name = found or (satellite, satellite)
        try:
            rule = self._match(out, satellite_id, satellite_name, wake_word)
            if rule is None:
                return out
            out.transcript = text
            await self._respond(out, rule, Request(
                satellite_id=satellite_id, satellite_name=satellite_name, wake_word=wake_word,
                text=text, audio_seconds=0.0, language=rule.language))
        except _Failed as e:
            out.error = str(e)
        except Exception as e:
            log.exception("routing test failed for satellite %s", satellite)
            out.error = f"internal: {type(e).__name__}: {e}"
        finally:
            out.timings_ms["total"] = round((time.monotonic() - start) * 1000, 1)
        return out

    def describe(self) -> dict:
        return {"rules": [r.model_dump(mode="json") for r in self.rules.rules],
                "env": self.rules.env_vars(),
                "services": {"stt": self.stt_url or None, "tts": self.tts_url or None,
                             "voice": self.voice},
                "warnings": self.rules.warnings(self.lookup),
                "load_error": self.rules.load_error}

    # -- stages ---------------------------------------------------------------

    def _match(self, out: Outcome, satellite_id: str, satellite_name: str,
               wake_word: str) -> Rule | None:
        rule = self.rules.match(satellite_id, satellite_name, wake_word)
        if rule is None:
            out.error = (f"no rule matches wake word {wake_word!r} on satellite "
                         f"{satellite_name or satellite_id!r}")
            log.info("routing: %s; the utterance is dropped", out.error)
            return None
        out.rule_id = rule.id
        return rule

    async def _respond(self, out: Outcome, rule: Rule, req: Request) -> None:
        dest = rule.destination
        reply = await self._stage(out, "destination", getattr(dest, "timeout", 5.0),
                                  dest.call(self.client, req))
        out.reply_text = reply
        out.reply_to = self._target(rule, req.satellite_id)
        if reply is None or out.reply_to is None:
            return  # nothing to say, or a rule that acts without answering
        out.reply_pcm48k = await self._stage(out, "tts", self.tts_timeout,
                                             self._synthesise(reply, rule.voice))

    def _target(self, rule: Rule, satellite_id: str) -> str | None:
        if rule.reply_to == "same":
            return satellite_id
        if rule.reply_to == "none":
            return None
        found = self.lookup(rule.reply_to) if self.lookup else None
        return found[0] if found else rule.reply_to

    async def _stage(self, out: Outcome, name: str, timeout: float, work) -> object:
        """Run one external call under a hard ceiling. httpx's own timeout is
        per read, so a server that trickles a byte a second never trips it;
        asyncio.timeout bounds the whole call."""
        t = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                return await work
        except (TimeoutError, httpx.TimeoutException):
            raise _Failed(f"{name}: no answer within {timeout:g} s") from None
        except DestinationError as e:
            raise _Failed(f"{name}: {e}") from None
        except httpx.HTTPError as e:
            raise _Failed(f"{name}: {type(e).__name__}: {e}") from None
        finally:
            out.timings_ms[name] = round((time.monotonic() - t) * 1000, 1)

    async def _transcribe(self, pcm: bytes, language: str | None) -> str:
        if not self.stt_url:
            raise DestinationError("SATELLITES_STT_URL is not set, so nothing can be transcribed")
        pcm = pcm[:len(pcm) & ~1]  # whole samples only
        data = {"model": "whisper-1", "response_format": "json"}
        if language:
            data["language"] = language.split("-")[0]
        r = await self.client.post(
            f"{self.stt_url}/v1/audio/transcriptions", data=data, timeout=self.stt_timeout,
            files={"file": ("utterance.wav", audio.wav(pcm, MIC_RATE, 1), "audio/wav")})
        if r.status_code != 200:
            raise DestinationError(f"STT answered {r.status_code}: {r.text[:200].strip()}")
        try:
            text = r.json()["text"]
        except (ValueError, KeyError, TypeError):
            raise DestinationError("STT answered without a \"text\" field") from None
        if not isinstance(text, str):
            raise DestinationError("STT answered a \"text\" that is not a string")
        return text.strip()

    async def _synthesise(self, text: str, voice: str | None) -> bytes:
        if not self.tts_url:
            raise DestinationError("SATELLITES_TTS_URL is not set, so the reply cannot be spoken")
        r = await self.client.post(f"{self.tts_url}/v1/audio/speech", timeout=self.tts_timeout,
                                   json={"model": "kokoro", "voice": voice or self.voice,
                                         "input": _clip(text), "response_format": "pcm"})
        if r.status_code != 200:
            raise DestinationError(f"TTS answered {r.status_code}: {r.text[:200].strip()}")
        pcm = r.content[:len(r.content) & ~1]
        return audio.resample(pcm, TTS_RATE, SPEAKER_RATE)

    def _log(self, satellite_id: str, wake_word: str, out: Outcome) -> None:
        if out.rule_id is None:
            return  # _match has already said so
        if out.error:
            result = out.error
        elif out.reply_pcm48k:
            result = f"reply for {out.reply_to}"
        else:
            result = "no reply"
        stages = ", ".join(f"{k} {v:.0f}" for k, v in out.timings_ms.items() if k != "total")
        log.info("routing: satellite %s wake %r rule %s: %s, %.0f ms (%s)", satellite_id,
                 wake_word, out.rule_id, result, out.timings_ms.get("total", 0), stages)
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
