"""Which wake words the hub listens for, on which satellites, and what each
one does once it is heard.

    <SATELLITES_DATA_DIR>/wake_words.json
    {"version": 2,
     "words": [{"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"],
                "mode": "conversation", "language": null,
                "action": {"destination": {"type": "llm", ...}, "reply_to": "same",
                           "voice": null, "fallback": null},
                "silence_ms": 800,
                "conversation": {"follow_up_s": 8, "silence_ms": 600, "end_phrases": null},
                "trigger": {"feedback": "earcon", "cooldown_s": 3, "ends_conversation": false}},
               {"name": "lumos", "threshold": 0.7, "satellites": ["94b97e7b8be8"],
                "mode": "trigger", "action": null, ...}],
     "ptt": {"mode": "command", "language": null, "action": {...}, ...}}

    a = Assignment.open(data_dir, os.environ["SATELLITES_WAKE_WORDS"])
    a.effective("94b97e7b8be8") -> ["hey_jarvis", "lumos"]
    a.replace(*check(body, available=available(model_dir), known=ids, saved=a))

A WAKE WORD IS THE UNIT OF CONFIGURATION. Its entry says which model, how
sure the detector must be, which satellites listen for it, and what it does:
its mode, an optional language hint, its action and the settings of its mode
(router.Behaviour). Push-to-talk, which a button stands in for, is not a wake
word and is never listened for, so its behaviour is the file's "ptt" block.
WordActions is how the router reads all of this.

"*" means every satellite, including one adopted after the word was set;
otherwise a word is heard only on the satellites it lists. A satellite with
no word assigned still streams and still takes push-to-talk.

A SAVE MERGES, BY NAME. A field an entry leaves out keeps what was saved for
that name: a client that knows only name, threshold and satellites (the
Satellites tab before 2026-09-25) must not wipe every action when it moves a
word to another room. A new name that leaves out its mode as well is the old
shape, and gets what the hub has always done: a command, echoed. An entry
that names a mode says everything that mode needs, or is refused: a command
or a conversation needs an action, a trigger must have none.

A TRIGGER WORD ACTS ON NOTHING BUT ITS OWN DETECTION, with no second step to
catch a false one, so it is stricter by default: threshold 0.7 rather than
0.5, and a cooldown (3 s) during which the same word does not fire again.

MIGRATION FROM rules.json. A file written before words carried an action
(no "version") gets, for each word and for push-to-talk, the rule that word
would have taken: the first rule in file order that names it or "*" and
names no satellites (router.Rules.for_word). Rules that named satellites
cannot be carried to a word that is one entry for every room; each is named
in the log. rules.json is left where it is, untouched. A rules.json that
does not load migrates nothing: those words route nowhere, as they did,
until they are given an action.

SATELLITES_WAKE_WORDS SEEDS THE FILE, ONCE. On the first start with a volume
that has no wake_words.json, every word it names is written here assigned to
"*", which is what the variable meant before words could be assigned. From
then on the file is the only source, changed by PUT /satellites/wake-words,
because two places that both claim to decide which words are live would
disagree the moment the Satellites tab saved anything.

A FILE THAT DOES NOT LOAD TURNS WAKE WORDS OFF rather than falling back to the
seed, and says why in GET /satellites/wake-words and /health, as a rules.json
that does not load turns routing off: a hand edit with a typo must not bring
back words someone removed from the bedroom. The file is left as it is until
a PUT replaces it.

Nothing here loads a model or talks to a satellite: main.Voice turns this
into running detectors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from . import router as routing
from . import wakeword
from .listening import PTT, parse_wake_words
from .store import write_atomic

log = logging.getLogger("voice-satellites.wakewords")

FILE = "wake_words.json"
VERSION = 2
EVERY = "*"
# The range a person can set. Under 0.1 every cough in the room wakes the
# satellite, and a model that has to score over 0.95 hardly ever fires on a
# real voice: the fixtures' "hey jarvis" peaked at 0.99 and 1.00 in two voices
# and at 0.05 in a third (tests/test_wakeword.py).
MIN_THRESHOLD = 0.1
MAX_THRESHOLD = 0.95
DEFAULT_THRESHOLD = 0.5
# A trigger acts on the detection alone, so it asks for more of it.
DEFAULT_TRIGGER_THRESHOLD = 0.7
# Every word assigned to a satellite is one more ONNX session run on each of
# its 80 ms frames. openWakeWord ships five; the rest would be custom models.
MAX_WORDS = 16
BEHAVIOUR_FIELDS = ("mode", "language", "action", "silence_ms", "conversation", "trigger")


def default_behaviour() -> routing.Behaviour:
    """What a word, or push-to-talk, did before it could be told: echo."""
    return routing.Behaviour(mode="command", action=routing.Action())


@dataclass
class Word:
    name: str
    threshold: float
    satellites: list[str] = field(default_factory=lambda: [EVERY])
    # None: a word migrated from a rules.json that did not load, which routes
    # nowhere until it is given an action.
    behaviour: routing.Behaviour | None = field(default_factory=default_behaviour)

    def covers(self, satellite_id: str) -> bool:
        return EVERY in self.satellites or satellite_id in self.satellites

    @property
    def mode(self) -> str | None:
        return self.behaviour.mode if self.behaviour else None

    def as_json(self) -> dict:
        body = {"name": self.name, "threshold": self.threshold, "satellites": list(self.satellites)}
        if self.behaviour is not None:
            body |= self.behaviour.model_dump(mode="json")
        return body


def available(model_dir: str | Path) -> list[str]:
    """The names openWakeWord can load here: the built-in ones, which are
    fetched on first use, and <name>.onnx of anyone's own in the model
    directory.

    The built-in files are named hey_jarvis_v0.1.onnx and so on; the dot keeps
    them out of the second list, since wakeword.NAME allows none. The two
    feature models every wake word shares are not wake words."""
    names = set(wakeword.MODELS)
    try:
        for f in Path(model_dir).glob("*.onnx"):
            if (f.name not in wakeword.FEATURES and f.stem != PTT
                    and wakeword.NAME.match(f.stem)):
                names.add(f.stem)
    except OSError:
        pass  # no model directory yet: the built-in names are still fetchable
    return sorted(names)


def _satellite_ref(ref: str) -> str:
    # The hub's ids are the MAC in lower case without separators
    # (main.satellite_id); a MAC pasted with colons is the same satellite.
    # Not satellite_id() itself: that strips every non-hex letter and would
    # turn a name like "kitchen" into the id "ce".
    ref = ref.strip()
    return ref if ref == EVERY else ref.lower().replace(":", "").replace("-", "")


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "").removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


def behaviour_of(entry: dict, saved: routing.Behaviour | None, what: str) -> routing.Behaviour:
    """The Behaviour an entry describes, with what it leaves out taken from
    `saved` (the same name, as saved before). Raises ValueError naming the
    first thing wrong."""
    given = {k: entry[k] for k in BEHAVIOUR_FIELDS if k in entry}
    if saved is None and "mode" not in given and "action" not in given:
        base = default_behaviour().model_dump(mode="json")
    elif saved is not None:
        base = saved.model_dump(mode="json")
    else:
        base = {}
    merged = base | given
    # Switching to a trigger drops the action unless the entry gives one (to
    # be refused): a trigger has none, and the old one was the other mode's.
    if merged.get("mode") == "trigger" and "action" not in given:
        merged["action"] = None
    try:
        return routing.Behaviour.model_validate(merged)
    except ValidationError as e:
        raise ValueError(f"{what}: {_first_error(e)}") from None


def check(entries: object, *, available: Iterable[str] | None = None,
          known: Iterable[str] | None = None, saved: Assignment | None = None,
          ptt: object = None) -> tuple[list[Word], routing.Behaviour]:
    """The words in `entries` and push-to-talk's behaviour, or ValueError with
    a sentence naming the first thing wrong.

    `available` and `known` are checked only when given. A PUT gives both. A
    file being loaded gives neither: its model may be dropped into the volume
    later, and the hub has seen no satellite at all in the first second after
    a restart. `saved` is what a PUT merges into (see the module docstring);
    `ptt` None keeps its saved behaviour."""
    if not isinstance(entries, list):
        raise ValueError("words must be a list")
    if len(entries) > MAX_WORDS:
        raise ValueError(f"{len(entries)} wake words; a hub takes at most {MAX_WORDS}")
    can_load = set(available) if available is not None else None
    ids = set(known) if known is not None else None
    before = {w.name: w for w in saved.words} if saved is not None else {}
    words: list[Word] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"word {i} is not an object with a name, a threshold and satellites")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"word {i} has no name")
        if name == PTT:
            raise ValueError(f"{PTT!r} is push-to-talk, not a wake word: a button stands in "
                             "for it, and it is never listened for; its action is \"ptt\"")
        if not wakeword.NAME.match(name):
            raise ValueError(f"{name!r} is not a model name: letters, digits, _ and -, "
                             "up to 64, starting with a letter or digit")
        if can_load is not None and name not in can_load:
            raise ValueError(f"{name!r} is not a wake word this hub can load; it can load "
                             f"{', '.join(sorted(can_load))}")
        if any(w.name == name for w in words):
            raise ValueError(f"{name!r} is listed twice; list each wake word once, with "
                             "every satellite it is for")
        old = before.get(name)
        behaviour = behaviour_of(entry, old.behaviour if old else None, repr(name))
        default = DEFAULT_TRIGGER_THRESHOLD if behaviour.mode == "trigger" else DEFAULT_THRESHOLD
        threshold = entry.get("threshold")
        if threshold is None:
            threshold = old.threshold if old else default
        # NaN compares false both ways, so it fails the range check too.
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD):
            raise ValueError(f"the threshold for {name!r} is {threshold!r}; it must be a "
                             f"number from {MIN_THRESHOLD} to {MAX_THRESHOLD}")
        satellites = entry.get("satellites")
        if satellites is None:
            satellites = list(old.satellites) if old else [EVERY]
        if (not isinstance(satellites, list)
                or not all(isinstance(s, str) and s.strip() for s in satellites)):
            raise ValueError(f"the satellites for {name!r} must be a list of satellite ids, "
                             f'or ["{EVERY}"] for every satellite')
        satellites = list(dict.fromkeys(_satellite_ref(s) for s in satellites))
        if EVERY in satellites and len(satellites) > 1:
            raise ValueError(f'the satellites for {name!r} mix "{EVERY}" with ids; "{EVERY}" '
                             "already means every satellite, so give it alone")
        if ids is not None:
            unknown = [s for s in satellites if s != EVERY and s not in ids]
            if unknown:
                raise ValueError(f"{name!r} is assigned to {unknown[0]!r}, which is not a "
                                 "satellite this hub knows (adopted, or seen since it started)")
        words.append(Word(name, float(threshold), satellites, behaviour))
    if ptt is None:
        push = saved.ptt if saved is not None else default_behaviour()
    elif not isinstance(ptt, dict):
        raise ValueError("ptt must be an object: push-to-talk's mode, language and action")
    else:
        push = behaviour_of(ptt, saved.ptt if saved is not None else None, "ptt")
    if push.mode == "trigger":
        raise ValueError("ptt: push-to-talk cannot be a trigger: the button is pressed to speak")
    _check_fallbacks(words, push)
    return words, push


def _check_fallbacks(words: list[Word], push: routing.Behaviour) -> None:
    by_name = {w.name: w for w in words}
    for who, b in [(repr(w.name), w.behaviour) for w in words] + [("ptt", push)]:
        fallback = b.action.fallback if b is not None and b.action else None
        if not fallback:
            continue
        target = by_name.get(fallback)
        if target is None:
            raise ValueError(f"{who}: its fallback {fallback!r} is not one of the wake words")
        if target.mode != "conversation":
            raise ValueError(f"{who}: its fallback {fallback!r} must be a conversation wake word; "
                             f"it is a {target.mode}")


def migrate(words: list[Word], data_dir: Path) -> tuple[list[Word], routing.Behaviour, bool]:
    """Give each word, and push-to-talk, the rule it would have taken in
    rules.json (router.Rules.for_word). False as the third value when
    rules.json did not load and nothing was migrated."""
    rules = routing.Rules(data_dir)
    if rules.load_error:
        log.error("wake words not migrated: %s", rules.load_error)
        return [Word(w.name, w.threshold, w.satellites, None) for w in words], default_behaviour(), False

    def carry(name: str) -> routing.Behaviour | None:
        chosen, skipped = rules.for_word(name)
        for r in skipped:
            log.warning("rules.json: rule %r (wake word %r, satellites %s) is not carried over: "
                        "a wake word now does one thing on every satellite it is assigned to",
                        r.id, r.wake_word, r.satellites)
        return chosen.behaviour() if chosen else None

    out = [Word(w.name, w.threshold, w.satellites, carry(w.name)) for w in words]
    for w in out:
        log.info("wake word %s: %s", w.name,
                 f"{w.behaviour.action.destination.type} from rules.json" if w.behaviour
                 else "no rule in rules.json names it, so it routes nowhere until given an action")
    return out, carry(PTT) or default_behaviour(), True


class Assignment:
    """wake_words.json, as loaded, and every change to it.

    Built with no path it holds nothing and writes nothing: a Hub made
    without a lifespan (tests/test_mqtt.py) still has one to ask."""

    def __init__(self, path: Path | None = None, words: list[Word] | None = None,
                 ptt: routing.Behaviour | None = None):
        self.path = path
        self.words: list[Word] = list(words or [])
        self.ptt: routing.Behaviour = ptt or default_behaviour()
        self.load_error: str | None = None
        # Bumped by every replace(), so main.Voice can tell that the words
        # changed while it was fetching a model and look again.
        self.version = 0

    @classmethod
    def open(cls, data_dir: str | Path, seed: str) -> Assignment:
        a = cls(Path(data_dir) / FILE)
        if a.path.exists():
            try:
                body = json.loads(a.path.read_text())
                if not isinstance(body, dict):
                    raise ValueError('the file is not a JSON object with "words" in it')
                old = body.get("version") is None
                if old:
                    # Entries from before actions: only their model, threshold
                    # and satellites are read here; migrate() gives the rest.
                    a.words = [Word(w.name, w.threshold, w.satellites) for w in check(
                        [{k: e.get(k) for k in ("name", "threshold", "satellites") if k in e}
                         for e in body.get("words", []) if isinstance(e, dict)])[0]]
                else:
                    a.words, a.ptt = check(body.get("words", []), ptt=body.get("ptt"))
            except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
                a.load_error = (f"{a.path} could not be loaded, so no wake word is listened "
                                "for until it is fixed or replaced with PUT "
                                f"/satellites/wake-words: {str(e)[:500]}")
                log.error("%s", a.load_error)
                return a
            if old:
                a.words, a.ptt, carried = migrate(a.words, Path(data_dir))
                if carried:
                    a._save_quietly("migrated from rules.json")
            return a
        try:
            a.words = seed_words(seed)
        except ValueError as e:
            # Nothing is written: with the variable fixed, the next start
            # seeds from it.
            a.load_error = f"SATELLITES_WAKE_WORDS: {e}"
            log.error("wake words are off: %s", a.load_error)
            return a
        a.words, a.ptt, _ = migrate(a.words, Path(data_dir))
        a._save_quietly("seeded from SATELLITES_WAKE_WORDS: "
                        + (", ".join(w.name for w in a.words) or "no wake words"))
        return a

    def _save_quietly(self, why: str) -> None:
        try:
            self._write(self.words, self.ptt)
            log.info("%s %s", self.path, why)
        except OSError as e:
            log.error("could not write %s (%s); this applies until the next start", self.path, e)

    def effective(self, satellite_id: str) -> list[str]:
        """The wake words this satellite listens for, in file order."""
        return [w.name for w in self.words if w.covers(satellite_id)]

    def thresholds(self) -> dict[str, float]:
        return {w.name: w.threshold for w in self.words}

    def triggers(self) -> frozenset[str]:
        return frozenset(w.name for w in self.words if w.mode == "trigger")

    def get(self, name: str) -> Word | None:
        return next((w for w in self.words if w.name == name), None)

    def behaviour(self, name: str) -> routing.Behaviour | None:
        if name == PTT:
            return self.ptt
        word = self.get(name)
        return word.behaviour if word else None

    def satellite_ids(self) -> set[str]:
        """Every id a word is assigned to by name. A PUT accepts these as known
        even when the satellite has not been seen since the hub started, so
        sending back what GET returned is never refused for data the hub
        itself wrote."""
        return {s for w in self.words for s in w.satellites if s != EVERY}

    def as_json(self) -> list[dict]:
        return [w.as_json() for w in self.words]

    def replace(self, words: list[Word], ptt: routing.Behaviour | None = None) -> None:
        """Written first and taken second, so a write that fails leaves the
        words that were live, live."""
        ptt = ptt or self.ptt
        self._write(words, ptt)
        self.words = list(words)
        self.ptt = ptt
        self.load_error = None
        self.version += 1

    def forget(self, satellite_id: str) -> bool:
        """Take a forgotten satellite out of every word that names it. A word
        left naming nobody stays, assigned to nobody, rather than disappear
        from the list someone made. True when anything changed."""
        if not any(satellite_id in w.satellites for w in self.words):
            return False
        self.replace([Word(w.name, w.threshold, [s for s in w.satellites if s != satellite_id],
                           w.behaviour) for w in self.words])
        return True

    def _write(self, words: list[Word], ptt: routing.Behaviour) -> None:
        if self.path is None:
            return
        write_atomic(self.path, json.dumps({
            "version": VERSION, "words": [w.as_json() for w in words],
            "ptt": ptt.model_dump(mode="json")}, indent=2) + "\n")


def seed_words(spec: str) -> list[Word]:
    """SATELLITES_WAKE_WORDS as words for every satellite. The variable takes
    any threshold in (0, 1], and a PUT only 0.1 to 0.95, so an old value
    outside that is brought inside it, and the log says so, rather than
    written as a file every later save would be refused over."""
    words = []
    for name, threshold in parse_wake_words(spec).items():
        kept = min(max(threshold, MIN_THRESHOLD), MAX_THRESHOLD)
        if kept != threshold:
            log.warning("SATELLITES_WAKE_WORDS: %s at %g is outside %g to %g; seeded at %g",
                        name, threshold, MIN_THRESHOLD, MAX_THRESHOLD, kept)
        words.append({"name": name, "threshold": kept, "satellites": [EVERY]})
    # The same rules the file is loaded by, so a seed can never write a file
    # the next start refuses (a name like "../x" parses as a name above).
    return check(words)[0]


class WordActions:
    """The router's view of the wake word entries (router.Actions): what a
    word does, found by its name. Satellites are not looked at here: a
    satellite only ever hears the words assigned to it, and the routing test
    runs a word whichever satellite it names."""

    editable = False

    def __init__(self, assignment: Assignment):
        self.assignment = assignment

    @property
    def load_error(self) -> str | None:
        return self.assignment.load_error

    def named(self, name: str) -> routing.Route | None:
        b = self.assignment.behaviour(name)
        if b is None:
            word = next((w for w in self.assignment.words
                         if routing._wake_key(w.name) == routing._wake_key(name)), None)
            b, name = (word.behaviour, word.name) if word else (None, name)
        return routing.Route(name, b) if b is not None else None

    def find(self, satellite_id: str, satellite_name: str, wake_word: str) -> routing.Route | None:
        return self.named(wake_word)

    def _all(self) -> list[tuple[str, routing.Behaviour]]:
        return ([(w.name, w.behaviour) for w in self.assignment.words if w.behaviour]
                + [(PTT, self.assignment.ptt)])

    def warnings(self, lookup=None) -> list[str]:
        out = [f"wake word {w.name!r} has no action, so what follows it goes nowhere"
               for w in self.assignment.words if w.behaviour is None]
        for name, b in self._all():
            reply_to = b.action.reply_to if b.action else None
            if lookup and reply_to not in (None, "same", "none") and lookup(reply_to) is None:
                out.append(f"{name!r} replies to {reply_to!r}, which is not a known satellite")
        return out

    def env_vars(self) -> dict[str, bool]:
        return routing.env_status(b.action.destination for _, b in self._all() if b.action)

    def listing(self) -> list[dict]:
        return [{"wake_word": name, **b.model_dump(mode="json")} for name, b in self._all()]


# ---- custom models --------------------------------------------------------

MAX_MODEL_BYTES = 5 * 1024 * 1024   # openWakeWord classifiers are ~0.2-1 MB


def custom(model_dir: str | Path) -> list[str]:
    """The names in available() that are someone's own <name>.onnx, not a
    built-in: the ones that can be uploaded over and deleted."""
    return [n for n in available(model_dir) if n not in wakeword.MODELS]


def check_model(name: str, data: bytes) -> None:
    """Refuse anything that is not an openWakeWord classifier before it lands
    in the model directory, where the next detector build would load it and
    fail for every satellite at once. Raises ValueError saying why."""
    if not wakeword.NAME.match(name) or name == PTT:
        raise ValueError(f"{name!r} is not a usable wake word name: letters, digits, _ and - only")
    if name in wakeword.MODELS:
        raise ValueError(f"{name!r} is a built-in model; give your own another name")
    if f"{name}.onnx" in wakeword.FEATURES:
        raise ValueError(f"{name!r} is one of the shared feature models")
    if not data:
        raise ValueError("empty body: send the .onnx file as the request body")
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError(f"{len(data)} bytes; a wake word model is under {MAX_MODEL_BYTES}")
    import onnxruntime as ort

    try:
        sess = ort.InferenceSession(data, providers=["CPUExecutionProvider"])
    except Exception as e:  # onnxruntime raises its own types for a bad graph
        raise ValueError(f"not an ONNX model onnxruntime can load: {e}") from None
    inputs = sess.get_inputs()
    shape = list(inputs[0].shape) if inputs else []
    # An openWakeWord classifier reads 16 frames of the 96-wide embedding.
    if len(inputs) != 1 or shape[-2:] != [16, 96]:
        raise ValueError(f"not an openWakeWord classifier: its input is {shape}, "
                         "expected [batch, 16, 96]")
