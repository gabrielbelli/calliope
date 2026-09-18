"""Composed and decomposed spellings of the same word must sound the same.

Phonemes rather than audio, deliberately. Comparing two synthesised waveforms
would need the 310 MB of weights this suite refuses to pull, and it would
answer a weaker question: samples can differ for reasons that have nothing to
do with the text, while a phoneme string is exactly what the defect changed.
`Synth.plan` returns those strings and loads no model, so the whole class of
bug is testable for the cost of wiring espeak-ng.

Two levels are checked, and both are needed. The stub test holds the contract
the service owns — whatever text arrives, the phonemiser is handed one
spelling of it — and runs anywhere. The espeak test holds the reason that
contract exists, and would start failing if espeak-ng ever learned to read a
combining mark on its own; that would be the day this normalisation stops
being load-bearing, and finding out from a red test is better than never
finding out.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.synth import Synth

# The same word twice: U+00F3, and o followed by U+0301 COMBINING ACUTE. They
# are canonically equivalent, they render identically in this file, and macOS
# hands the second one to any client that reads a pasteboard selection.
COMPOSED = "avó"
DECOMPOSED = "avó"


def _synth(tokenizer: object) -> Synth:
    """A Synth around a tokenizer and nothing else.

    `Synth.__init__` wires espeak and loads 310 MB of Kokoro weights, neither
    of which `plan` touches — it reaches `self._k.tokenizer` and stops. Built
    this way rather than with a fixture that monkeypatches the constructor
    because the constructor is not what is under test and should not have to
    be kept working for these to run.
    """
    made = object.__new__(Synth)
    made._k = SimpleNamespace(tokenizer=tokenizer)  # type: ignore[attr-defined]
    return made


class RecordingTokenizer:
    """Echoes its input back as phonemes, and keeps every string it was given."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def phonemize(self, text: str, lang: str) -> str:
        self.seen.append(text)
        return text


def _espeak_tokenizer() -> tuple[object | None, str]:
    """Kokoro's real tokenizer, or None and the reason it could not be had.

    No weights are read: `Tokenizer.__init__` resolves the espeak shared
    library and the data directory, and that is all. Measured at 1.3 s for the
    import and both phonemisations together on the development machine.

    THE REASON IS CARRIED OUT, not swallowed, because the alternative is a
    measurement test that deletes itself in silence. A broken wiring and a
    machine with no espeak-ng both arrive here as an exception, and a bare
    `return None` makes them the same green dot in `-q` output; the only
    evidence of the mispronunciation this module exists for would then be a
    test nobody could see had stopped running. `pytest -rs` prints the reason
    a skip gives, so putting the exception in it is the difference between
    "espeak is not installed on this box" and "the import of _wire_espeak is
    broken and three other tests are about to be wrong too".
    """
    try:
        from app.synth import _wire_espeak
        _wire_espeak()
        from kokoro_onnx.tokenizer import Tokenizer
        return Tokenizer(), ""
    except Exception as exc:  # noqa: BLE001 - any failure here means "not available"
        return None, f"{type(exc).__name__}: {exc}"


def test_both_spellings_reach_the_phonemiser_as_one_string() -> None:
    """The service's own half of the contract, with espeak out of the picture.

    Before the fix these two calls handed the phonemiser two different
    strings, and everything downstream — the chunk plan, the token count in
    the SSE done event, the audio — followed the difference.
    """
    tokenizer = RecordingTokenizer()
    synth = _synth(tokenizer)

    composed = synth.plan(COMPOSED, "pt-br")
    decomposed = synth.plan(DECOMPOSED, "pt-br")

    assert tokenizer.seen == [COMPOSED, COMPOSED]
    assert composed == decomposed
    # The combining mark is gone rather than merely tolerated. A test that
    # only compared the two results would also pass if normalisation ran in
    # the wrong direction and composed text were pulled apart into NFD.
    assert "́" not in tokenizer.seen[1]


def test_a_decomposed_segment_is_normalised_too() -> None:
    """/speak's segments reach the phonemiser through `speak`, not `plan`.

    Worth its own test because the seam is one call deep from the route: a
    normalisation added at the top of `/v1/audio/speech` instead would have
    left this path exactly as broken as it was, and nothing in the native
    route's own tests would have said so.
    """
    tokenizer = RecordingTokenizer()
    synth = _synth(tokenizer)
    synth.speak_chunk = (  # type: ignore[method-assign]
        lambda phonemes, voice, language, speed: np.zeros(1, dtype=np.float32))

    synth.speak_segments([(DECOMPOSED, 0.0, "pf_dora")], "pt-br", 1.0)

    assert tokenizer.seen == [COMPOSED]


def test_espeak_gives_the_two_spellings_the_same_phonemes_after_the_fix() -> None:
    """The measurement the fix was written from, run rather than quoted.

    On this image's espeak-ng, `avó` written with U+00F3 phonemises pt-br to
    `avˈɔ` and the decomposed spelling to `avˈo`: the mark is dropped, the
    open vowel closes, and the word is spoken as "avô". The second assertion
    is the one that matters — the first only records that espeak still has
    the behaviour, so a future espeak that fixes it turns this test red
    instead of leaving a normalisation nobody can justify.
    """
    tokenizer, why = _espeak_tokenizer()
    if tokenizer is None:
        pytest.skip(f"espeak-ng could not be wired here -- {why}")

    raw_composed = tokenizer.phonemize(COMPOSED, "pt-br")
    raw_decomposed = tokenizer.phonemize(DECOMPOSED, "pt-br")
    assert raw_decomposed != raw_composed, (
        "espeak now reads the combining mark; the normalisation in "
        "Synth.plan may no longer be needed")

    synth = _synth(tokenizer)
    assert synth.plan(DECOMPOSED, "pt-br") == synth.plan(COMPOSED, "pt-br")
    assert synth.plan(DECOMPOSED, "pt-br") == [raw_composed]


# The four calls on Synth that take text rather than phonemes, each reduced to
# "run it and tell me what the phonemiser was handed". `speak_chunk` and
# `token_count` are absent on purpose: both are given the output of `plan` and
# neither can see text.
ENTRY_POINTS = {
    "plan": lambda synth: synth.plan(DECOMPOSED, "pt-br"),
    "speak": lambda synth: synth.speak(DECOMPOSED, "pf_dora", "pt-br", 1.0),
    "stream": lambda synth: list(
        synth.stream(DECOMPOSED, "pf_dora", "pt-br", 1.0)),
    "speak_segments": lambda synth: synth.speak_segments(
        [(DECOMPOSED, 0.0, "pf_dora")], "pt-br", 1.0),
}


@pytest.mark.parametrize("entry_point", sorted(ENTRY_POINTS))
def test_every_text_entry_point_composes_its_input(entry_point: str) -> None:
    """THE COMMENT ON THE FIX CLAIMS FOUR ENTRY POINTS; THIS COUNTS THEM.

    One line in `plan` covers /speak, /speak's segments, the buffered /v1 body
    and the SSE stream only for as long as all four keep arriving through
    `plan`, and that is a property of the code rather than a law. The obvious
    way to lose it is a fast path: `stream` calling `self._k.tokenizer` itself
    to save a function call, or a future route phonemising once and passing
    phonemes around. Either would leave the fix in place, leave the other
    tests in this file green, and put the defect back on the path that carries
    the streamed requests — the ones a person is listening to live, and so the
    ones where a wrong vowel is heard soonest.

    Written against the callables rather than the routes because the routes
    replace `Synth` with a fake (tests/test_openai_speech.py FakeSynth), which
    has its own `plan` and cannot answer this question at all.
    """
    tokenizer = RecordingTokenizer()
    synth = _synth(tokenizer)
    synth.speak_chunk = (  # type: ignore[method-assign]
        lambda phonemes, voice, language, speed: np.zeros(1, dtype=np.float32))

    ENTRY_POINTS[entry_point](synth)

    assert tokenizer.seen == [COMPOSED], (
        f"{entry_point} handed the phonemiser {tokenizer.seen!r}")


def test_text_with_nothing_to_compose_is_handed_over_untouched() -> None:
    """Every request goes through this line, not only the accented ones.

    So the cost of getting it wrong is not a mispronounced word in Portuguese
    but a change to every English sentence the service has ever spoken. NFC
    is a no-op on text with no marks in it, and this is what says so.
    """
    tokenizer = RecordingTokenizer()
    plain = "One. Two. Three. Four. Five."

    assert _synth(tokenizer).plan(plain, "en-us") == ["One. Two. Three. Four. Five."]
    assert tokenizer.seen == [plain]
