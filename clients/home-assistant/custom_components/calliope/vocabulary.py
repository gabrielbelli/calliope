"""Home Assistant's own names, kept on the stack as a glossary profile.

What Assist can act on is named by the people who live with it: "Luz da
cama", "Sala", an alias added to a lamp. Parakeet has never been trained on
those names, and a name it mishears is a command Assist cannot match. So this
integration keeps one profile on the stack's speech-to-text, GLOSSARY_PROFILE,
holding:

  - every floor and area, with their aliases;
  - the name and aliases of every entity exposed to Assist;
  - a short list of the words commands are made of, for each language an
    Assist pipeline (or Home Assistant itself) is set to.

Every line is a bare term, which the stack treats as a hotword: it biases
Parakeet's decoder when a request asks for `boost`, and repairs its own
capitalisation. A bare term has no `heard =` side, so it never rewrites some
other word into a name. The stack measured absent terms at no cost on Parakeet
(services/stt/app/openai_api.py, _boost), so a list that names every room is
safe on a command about one of them. Not on Whisper, where it measured them
raising the word error rate by 28%: Whisper is not sent the profile (stt.py).

The order is floors, areas, entities, command words. The stack boosts at most
STT_BOOST_MAX_PHRASES (200) phrases a request, chosen alphabetically from the
terms and every repair's intended side, which it boosts too. So the terms stop
at MAX_TERMS less the repairs' distinct intended sides: then the stack boosts
every phrase, and what is dropped when a house has more names is decided here,
in this order, and said. Terms shorter than MIN_TERM_CHARS are left out, since
the stack does not boost them (STT_BOOST_MIN_PHRASE_CHARS).

The profile is first built once Home Assistant has started, then after any
change to a registry or to what is exposed; each time once things have been
quiet for DEBOUNCE_S, which also keeps the first write out of the burst of
registry events a start produces. It is also rebuilt every REFRESH_INTERVAL,
for a friendly name that changed without a registry event, and written only
when the text differs from what was last written.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .api import CalliopeClient, CalliopeError
from .const import GLOSSARY_PROFILE

_LOGGER = logging.getLogger(__name__)

ASSISTANT = "conversation"
DEBOUNCE_S = 10.0
REFRESH_INTERVAL = timedelta(hours=1)
MAX_TERMS = 200
MIN_TERM_CHARS = 4
MAX_TERM_CHARS = 100

# The words commands are made of, by primary language subtag. Home Assistant's
# own sentences (home-assistant/intents) use these verbs and nouns; the names
# above are what they act on.
COMMAND_WORDS: dict[str, tuple[str, ...]] = {
    "pt": (
        "liga", "desliga", "acende", "apaga", "abre", "fecha", "aumenta",
        "diminui", "abaixa", "a luz", "as luzes", "lâmpada", "temperatura", "volume",
        "cortina", "persiana", "ventilador", "ar condicionado", "tomada",
        "alarme", "temporizador",
    ),
    "en": (
        "turn on", "turn off", "switch on", "switch off", "lights", "open",
        "close", "brightness", "temperature", "volume", "curtains", "blinds",
        "timer", "alarm",
    ),
}

# Rewrites of what the model writes for a command it ran together, by primary
# language subtag. Spoken quickly, "desliga a luz da cama" elides "a" into the
# verb and "luz" comes back as the pronoun ending -los: "desliga-los da cama",
# which Assist cannot match (seen from the pt-BR fine-tune, 28 Sep 2026). "luz"
# is too short to boost (the stack skips terms under four characters), so the
# fix is a repair after decoding. Each left-hand side is two words, which the
# stack accepts without `force`, and "desliga-los da" is not something anyone
# asks a voice assistant for.
REPAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "pt": tuple(
        (f"{verb}{pronoun} {prep}", f"{verb} a luz {prep}")
        for verb in ("liga", "ligue", "desliga", "desligue", "acende", "acenda",
                     "apaga", "apague")
        for pronoun in ("-los", "-lo")
        for prep in ("da", "do", "de", "na", "no")
    ),
}

# Typographic punctuation Home Assistant's frontend and phones insert, as the
# ASCII the model's vocabulary spells. "Guest’s Bedroom" had no token
# sequence for '’', and the stack refused every boosted request naming it.
TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2026": "...", "\u00a0": " ",
})

HEADER = """\
# Written by the Calliope integration in Home Assistant, from its floors,
# areas and the entities exposed to Assist. Rewritten whenever they change:
# edits made here are overwritten.
"""


def _languages(hass: HomeAssistant) -> list[str]:
    """Primary subtags of Home Assistant's language and every pipeline's."""
    found = [hass.config.language]
    if "assist_pipeline" in hass.config.components:
        from homeassistant.components.assist_pipeline import (  # noqa: PLC0415
            async_get_pipelines,
        )

        found += [p.language for p in async_get_pipelines(hass)]
    langs: list[str] = []
    for tag in found:
        primary = (tag or "").split("-")[0].split("_")[0].lower()
        if primary and primary not in langs:
            langs.append(primary)
    return langs


def _exposed_entities(hass: HomeAssistant) -> Iterable[tuple[str, list[str]]]:
    """(name, aliases) of each entity exposed to Assist, by entity id.
    Nothing when exposure is unknown: the homeassistant component is not
    loaded."""
    if "homeassistant" not in hass.config.components:
        return
    from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
        async_should_expose,
    )

    entities = er.async_get(hass)
    for state in sorted(hass.states.async_all(), key=lambda s: s.entity_id):
        if not async_should_expose(hass, ASSISTANT, state.entity_id):
            continue
        entry = entities.async_get(state.entity_id)
        # An alias can be the marker for "the entity's own name", not a string.
        aliases = [a for a in (entry.aliases if entry else ()) if isinstance(a, str)]
        yield state.name, aliases


@callback
def collect_repairs(hass: HomeAssistant) -> list[tuple[str, str]]:
    """The repair rules for the languages Assist is set to."""
    return [rule for lang in _languages(hass) for rule in REPAIRS.get(lang, ())]


@callback
def collect(hass: HomeAssistant) -> list[str]:
    """The terms, in order, cleaned, de-duplicated and capped at what is
    left of MAX_TERMS once the repairs' intended sides are counted."""
    repairs = collect_repairs(hass)
    raw: list[str] = []
    for floor in fr.async_get(hass).async_list_floors():
        raw += [floor.name, *sorted(floor.aliases)]
    for area in ar.async_get(hass).async_list_areas():
        raw += [area.name, *sorted(area.aliases)]
    for name, aliases in _exposed_entities(hass):
        raw += [name, *aliases]
    for lang in _languages(hass):
        raw += COMMAND_WORDS.get(lang, ())
    return clean(raw, MAX_TERMS - len({intended for _, intended in repairs}))


def _spellable(text: str) -> str:
    """Typographic punctuation as ASCII, and no emoji or other symbols, which
    no speech model's vocabulary spells. What is left can still hold a
    character the model lacks (a script it was not trained on); speech-to-text
    then falls back to transcribing without the vocabulary."""
    text = unicodedata.normalize("NFC", text.translate(TYPOGRAPHIC))
    return "".join(
        " " if unicodedata.category(ch).startswith("S") else ch for ch in text
    )


def clean(terms: Iterable[object], limit: int = MAX_TERMS) -> list[str]:
    """One line each: spellable, whitespace collapsed, no '=' (that would make
    a replacement rule) or leading '#' (a comment), no duplicates in the
    stack's own sense (case-insensitive), nothing it would not boost, and no
    more than `limit`."""
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        text = _spellable(str(term or "")).replace("=", " ")
        text = " ".join(text.split()).lstrip("#").strip()
        if not MIN_TERM_CHARS <= len(text) <= MAX_TERM_CHARS:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    if len(out) > limit:
        _LOGGER.warning(
            "Home Assistant has %d terms for the speech-to-text vocabulary; "
            "only the first %d are sent (floors, areas, entities, then command "
            "words), so that the stack boosts every one",
            len(out),
            limit,
        )
    return out[:limit]


def render(terms: list[str], repairs: list[tuple[str, str]] = ()) -> str:
    """The profile as the stack stores it: a header, one term a line, then
    one `heard = intended` repair a line."""
    return (
        HEADER
        + "".join(f"{term}\n" for term in terms)
        + "".join(f"{heard} = {intended}\n" for heard, intended in repairs)
    )


class Vocabulary:
    """Keeps GLOSSARY_PROFILE on the stack in step with Home Assistant."""

    def __init__(self, hass: HomeAssistant, client: CalliopeClient) -> None:
        """Nothing is written until async_start."""
        self.hass = hass
        self.client = client
        # The text the stack last accepted. `available` is what speech-to-text
        # reads: a request may only name a profile the stack has.
        self.written: str | None = None
        self.available = False
        self.terms = 0
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=DEBOUNCE_S,
            immediate=False,
            function=self.async_refresh,
        )

    async def async_refresh(self) -> None:
        """Build the profile and write it when it changed. A failure is
        logged and leaves the last written profile in use."""
        terms = collect(self.hass)
        text = render(terms, collect_repairs(self.hass))
        if text == self.written:
            return
        try:
            await self.client.put_glossary(GLOSSARY_PROFILE, text)
        except CalliopeError as err:
            _LOGGER.warning(
                "Could not write the %s vocabulary to Calliope: %s", GLOSSARY_PROFILE, err
            )
            return
        self.written = text
        self.available = True
        self.terms = len(terms)
        _LOGGER.debug("Wrote the %s vocabulary: %d terms", GLOSSARY_PROFILE, len(terms))

    @callback
    def async_lost(self) -> None:
        """The stack refused the profile's name: stop naming it, write again."""
        self.available = False
        self.written = None
        self.async_schedule()

    @callback
    def async_schedule(self, _event: Event | None = None) -> None:
        """Rebuild once things have been quiet for DEBOUNCE_S."""
        self._debouncer.async_schedule_call()

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """Write once Home Assistant has started and gone quiet for
        DEBOUNCE_S, then follow its changes. Returns what to call on unload."""
        hass = self.hass

        @callback
        def _first(_hass: HomeAssistant) -> None:
            self.async_schedule()

        @callback
        def _hourly(_now: datetime) -> None:
            self.async_schedule()

        unsubs = [
            async_at_started(hass, _first),
            async_track_time_interval(hass, _hourly, REFRESH_INTERVAL),
            self._debouncer.async_shutdown,
        ]
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
            fr.EVENT_FLOOR_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
        ):
            unsubs.append(hass.bus.async_listen(event_type, self.async_schedule))
        if "homeassistant" in hass.config.components:
            from homeassistant.components.homeassistant.exposed_entities import (  # noqa: PLC0415
                async_listen_entity_updates,
            )

            unsubs.append(
                async_listen_entity_updates(hass, ASSISTANT, self.async_schedule)
            )
        return unsubs
