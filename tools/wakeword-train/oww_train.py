"""Run openWakeWord's own train.py, with two fixes that need no edit to it.

    python oww_train.py --training_config cfg.yaml --generate_clips
    python oww_train.py --training_config cfg.yaml --augment_clips
    python oww_train.py --training_config cfg.yaml --train_model

Arguments pass straight through to openwakeword/train.py (the pinned commit in
/opt/openWakeWord). Before it runs, this does three things:

1. WORDS MISSING FROM CMUdict. openWakeWord builds its adversarial phrases from
   the CMU pronouncing dictionary and falls back to a DeepPhonemizer model for
   any word not in it. That model's download URL has returned 403 since at
   least 2026, so a target such as "grok" or "lumos" crashes the clip
   generation. The config key `extra_pronunciations` (word -> ARPAbet) is
   added to the dictionary `pronouncing` holds in memory, so the lookup
   succeeds and the fallback is never reached.

2. HOMOPHONES AMONG THE ADVERSARIAL PHRASES. generate_adversarial_texts means
   to leave out words that sound exactly like the target, but it compares a
   phone string with a list, which is never equal, so none are left out:
   "clawed" is a negative for "claude", "gee pea tea" for "g p t". A clip
   that sounds identical to the wake word, labelled negative, teaches the
   model to reject the wake word. The wrapper drops any generated phrase whose
   pronunciation equals a target phrase's, and logs how many it dropped.

3. LOGGING. train.py never configures logging; the first module to do so is
   piper-sample-generator, at DEBUG, which floods the log. INFO is set here
   first, so train.py's own progress messages still show.
"""

from __future__ import annotations

import itertools
import logging
import re
import runpy
import sys

import yaml

TRAIN_PY = "/opt/openWakeWord/openwakeword/train.py"

log = logging.getLogger("oww_train")


def _config_path(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--training_config":
            return argv[i + 1]
        if arg.startswith("--training_config="):
            return arg.split("=", 1)[1]
    sys.exit("oww_train.py: --training_config is required")


def _add_pronunciations(extra: dict[str, str]) -> None:
    import pronouncing

    pronouncing.init_cmu()
    for word, phones in extra.items():
        word = word.lower()
        if phones not in pronouncing.lookup.get(word, []):
            pronouncing.pronunciations.append((word, phones))
            pronouncing.lookup[word].append(phones)
            log.info("pronunciation added: %s = %s", word, phones)


def _sounds(text: str) -> set[str] | None:
    """Every stress-free phone string `text` can be read as, or None when a
    word is not in the dictionary (such a phrase is kept: nothing says it is
    a homophone)."""
    import pronouncing

    per_word = []
    for word in text.split():
        phones = pronouncing.phones_for_word(word)
        if not phones:
            return None
        per_word.append({re.sub(r"\d", "", p) for p in phones})
    return {" ".join(combo) for combo in itertools.product(*per_word)}


def _filter_homophones(targets: list[str]) -> None:
    import openwakeword.data as owd

    original = owd.generate_adversarial_texts
    target_sounds: set[str] = set()
    for t in targets:
        target_sounds |= _sounds(t) or set()

    def generate_adversarial_texts(*args, **kwargs):
        texts = original(*args, **kwargs)
        kept, dropped = [], []
        for t in texts:
            (dropped if (_sounds(t) or set()) & target_sounds else kept).append(t)
        if dropped:
            log.info("dropped %d of %d adversarial phrases that sound like the target, e.g. %s",
                     len(dropped), len(texts), sorted(set(dropped))[:8])
        return kept

    # train.py does `from openwakeword.data import generate_adversarial_texts`
    # when it runs, after this, so it picks up the wrapper.
    owd.generate_adversarial_texts = generate_adversarial_texts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    argv = sys.argv[1:]
    config = yaml.safe_load(open(_config_path(argv)))
    _add_pronunciations(config.get("extra_pronunciations") or {})
    _filter_homophones(list(config["target_phrase"]))
    sys.argv = [TRAIN_PY, *argv]
    runpy.run_path(TRAIN_PY, run_name="__main__")


if __name__ == "__main__":
    main()
