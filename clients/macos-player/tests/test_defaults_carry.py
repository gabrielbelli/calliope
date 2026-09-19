"""The rename of the defaults suite must not lose the speed step or the reader state.

Without the carry, the first run under com.gabrielbelli.calliope-player reads an empty suite:
speed falls back to 1.0 and the reader opens closed, with nothing to say why. These run the real
carryDefaults through a compiled harness, one process per step, which is the only way to see it
happen without starting the player.
"""
import pathlib
import re

import pytest

MAIN = (pathlib.Path(__file__).resolve().parent.parent / "player/main.swift").read_text()


def values(dump):
    return dict(line.split("=", 1) for line in dump.splitlines())


def test_opening_the_settings_is_what_carries_them(defaults_harness, suites):
    """Through playerDefaults, the call main.swift makes, and not through carryDefaults.

    Every other test here reaches carryDefaults directly, which leaves the one line joining it
    to the player covered by nothing: drop that call out of playerDefaults and the rest of this
    file stays green while the first run after the rename resets everybody."""
    old, new = suites
    defaults_harness("set", old, "speed", "float", "2.0")
    defaults_harness("set", old, "karaoke", "bool", "true")

    opened = values(defaults_harness("open", old, new))

    assert float(opened["speed"]) == 2.0
    assert opened["karaoke"] == "1"
    # The store handed back has to be the new suite, so the next write lands there too.
    assert float(values(defaults_harness("dump", new))["speed"]) == 2.0


@pytest.mark.parametrize("step", re.search(r"speedSteps: \[Float\] = \[([^\]]+)\]", MAIN).group(1).split(", "))
def test_every_speed_step_survives_the_carry_exactly(defaults_harness, suites, step):
    """`speedSteps.contains(saved)` is an exact Float comparison, so a step that arrives a hair
    off is not a slightly wrong speed — it is silently 1.0, the very reset the carry exists to
    prevent. The value goes Float -> plist real -> Float on the way across."""
    old, new = suites
    defaults_harness("set", old, "speed", "float", step)

    defaults_harness("carry", old, new)

    assert float(values(defaults_harness("dump", new))["speed"]) == float(step)


def test_carries_speed_and_reader_to_the_new_suite(defaults_harness, suites):
    old, new = suites
    defaults_harness("set", old, "speed", "float", "2.0")
    defaults_harness("set", old, "karaoke", "bool", "true")

    assert defaults_harness("carry", old, new) == "carried"

    carried = values(defaults_harness("dump", new))
    assert float(carried["speed"]) == 2.0
    assert carried["karaoke"] == "1"


def test_leaves_the_old_suite_untouched(defaults_harness, suites):
    old, new = suites
    defaults_harness("set", old, "speed", "float", "1.5")

    defaults_harness("carry", old, new)

    assert float(values(defaults_harness("dump", old))["speed"]) == 1.5


def test_does_not_overwrite_a_choice_made_under_the_new_name(defaults_harness, suites):
    old, new = suites
    defaults_harness("set", old, "speed", "float", "3.0")
    defaults_harness("set", new, "speed", "float", "1.0")

    assert defaults_harness("carry", old, new) == "kept"

    assert float(values(defaults_harness("dump", new))["speed"]) == 1.0


def test_carries_only_the_keys_that_exist(defaults_harness, suites):
    old, new = suites
    defaults_harness("set", old, "karaoke", "bool", "true")

    assert defaults_harness("carry", old, new) == "carried"

    carried = values(defaults_harness("dump", new))
    assert carried["karaoke"] == "1"
    assert carried["speed"] == "-"


def test_nothing_to_carry_is_not_a_carry(defaults_harness, suites):
    old, new = suites

    assert defaults_harness("carry", old, new) == "kept"
    assert values(defaults_harness("dump", new)) == {"speed": "-", "karaoke": "-"}
