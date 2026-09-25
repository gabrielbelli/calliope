"""Which wake words the hub listens for, and on which satellites.

    <SATELLITES_DATA_DIR>/wake_words.json
    {"words": [{"name": "hey_jarvis", "threshold": 0.5, "satellites": ["*"]},
               {"name": "alexa", "threshold": 0.6, "satellites": ["94b97e7b8be8"]}]}

    a = Assignment.open(data_dir, os.environ["SATELLITES_WAKE_WORDS"])
    a.effective("94b97e7b8be8") -> ["hey_jarvis", "alexa"]
    a.replace(check(body, available=available(model_dir), known=ids))

"*" means every satellite, including one adopted after the word was set;
otherwise a word is heard only on the satellites it lists. A satellite with
no word assigned still streams and still takes push-to-talk. Routing rules
stay keyed by wake word (router.py), so the same word can do different things
in different rooms and a word can be moved between rooms without touching a
rule.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import wakeword
from .listening import PTT, parse_wake_words
from .store import write_atomic

log = logging.getLogger("voice-satellites.wakewords")

FILE = "wake_words.json"
EVERY = "*"
# The range a person can set. Under 0.1 every cough in the room wakes the
# satellite, and a model that has to score over 0.95 hardly ever fires on a
# real voice: the fixtures' "hey jarvis" peaked at 0.99 and 1.00 in two voices
# and at 0.05 in a third (tests/test_wakeword.py).
MIN_THRESHOLD = 0.1
MAX_THRESHOLD = 0.95
DEFAULT_THRESHOLD = 0.5
# Every word assigned to a satellite is one more ONNX session run on each of
# its 80 ms frames. openWakeWord ships five; the rest would be custom models.
MAX_WORDS = 16


@dataclass
class Word:
    name: str
    threshold: float
    satellites: list[str] = field(default_factory=lambda: [EVERY])

    def covers(self, satellite_id: str) -> bool:
        return EVERY in self.satellites or satellite_id in self.satellites


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


def check(entries: object, *, available: Iterable[str] | None = None,
          known: Iterable[str] | None = None) -> list[Word]:
    """The words in `entries` (a list of {"name", "threshold", "satellites"}),
    or ValueError with a sentence naming the first thing wrong.

    `available` and `known` are checked only when given. A PUT gives both. A
    file being loaded gives neither: its model may be dropped into the volume
    later, and the hub has seen no satellite at all in the first second after
    a restart."""
    if not isinstance(entries, list):
        raise ValueError("words must be a list")
    if len(entries) > MAX_WORDS:
        raise ValueError(f"{len(entries)} wake words; a hub takes at most {MAX_WORDS}")
    can_load = set(available) if available is not None else None
    ids = set(known) if known is not None else None
    words: list[Word] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"word {i} is not an object with a name, a threshold and satellites")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"word {i} has no name")
        if name == PTT:
            raise ValueError(f"{PTT!r} is push-to-talk, not a wake word: a button stands in "
                             "for it, and it is never listened for")
        if not wakeword.NAME.match(name):
            raise ValueError(f"{name!r} is not a model name: letters, digits, _ and -, "
                             "up to 64, starting with a letter or digit")
        if can_load is not None and name not in can_load:
            raise ValueError(f"{name!r} is not a wake word this hub can load; it can load "
                             f"{', '.join(sorted(can_load))}")
        if any(w.name == name for w in words):
            raise ValueError(f"{name!r} is listed twice; list each wake word once, with "
                             "every satellite it is for")
        threshold = entry.get("threshold", DEFAULT_THRESHOLD)
        # NaN compares false both ways, so it fails the range check too.
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD):
            raise ValueError(f"the threshold for {name!r} is {threshold!r}; it must be a "
                             f"number from {MIN_THRESHOLD} to {MAX_THRESHOLD}")
        satellites = entry.get("satellites", [EVERY])
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
        words.append(Word(name, float(threshold), satellites))
    return words


class Assignment:
    """wake_words.json, as loaded, and every change to it.

    Built with no path it holds nothing and writes nothing: a Hub made
    without a lifespan (tests/test_mqtt.py) still has one to ask."""

    def __init__(self, path: Path | None = None, words: list[Word] | None = None):
        self.path = path
        self.words: list[Word] = list(words or [])
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
                a.words = check(body.get("words", []))
            except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
                a.load_error = (f"{a.path} could not be loaded, so no wake word is listened "
                                "for until it is fixed or replaced with PUT "
                                f"/satellites/wake-words: {str(e)[:500]}")
                log.error("%s", a.load_error)
            return a
        try:
            a.words = seed_words(seed)
        except ValueError as e:
            # Nothing is written: with the variable fixed, the next start
            # seeds from it.
            a.load_error = f"SATELLITES_WAKE_WORDS: {e}"
            log.error("wake words are off: %s", a.load_error)
            return a
        try:
            a._write(a.words)
            log.info("%s seeded from SATELLITES_WAKE_WORDS: %s", a.path,
                     ", ".join(w.name for w in a.words) or "no wake words")
        except OSError as e:
            log.error("could not write %s (%s); the seed applies until the next start", a.path, e)
        return a

    def effective(self, satellite_id: str) -> list[str]:
        """The wake words this satellite listens for, in file order."""
        return [w.name for w in self.words if w.covers(satellite_id)]

    def thresholds(self) -> dict[str, float]:
        return {w.name: w.threshold for w in self.words}

    def satellite_ids(self) -> set[str]:
        """Every id a word is assigned to by name. A PUT accepts these as known
        even when the satellite has not been seen since the hub started, so
        sending back what GET returned is never refused for data the hub
        itself wrote."""
        return {s for w in self.words for s in w.satellites if s != EVERY}

    def as_json(self) -> list[dict]:
        return [asdict(w) for w in self.words]

    def replace(self, words: list[Word]) -> None:
        """Written first and taken second, so a write that fails leaves the
        words that were live, live."""
        self._write(words)
        self.words = list(words)
        self.load_error = None
        self.version += 1

    def forget(self, satellite_id: str) -> bool:
        """Take a forgotten satellite out of every word that names it. A word
        left naming nobody stays, assigned to nobody, rather than disappear
        from the list someone made. True when anything changed."""
        if not any(satellite_id in w.satellites for w in self.words):
            return False
        self.replace([Word(w.name, w.threshold, [s for s in w.satellites if s != satellite_id])
                      for w in self.words])
        return True

    def _write(self, words: list[Word]) -> None:
        if self.path is None:
            return
        write_atomic(self.path, json.dumps({"words": [asdict(w) for w in words]}, indent=2) + "\n")


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
        words.append(Word(name, kept, [EVERY]))
    # The same rules the file is loaded by, so a seed can never write a file
    # the next start refuses (a name like "../x" parses as a name above).
    return check([asdict(w) for w in words])


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
