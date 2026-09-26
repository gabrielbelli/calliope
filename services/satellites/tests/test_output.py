"""Speaker or headphones, from the loopback (app/output.py).

Timestamps are passed in, so nothing here sleeps: each test lays out 20 ms
frames on a timeline, idle, then a sound, then quiet after it.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.output import HEADPHONES, SPEAKER, OutputSense, Sound, level_dbfs

FRAME_S = 0.02
QUIET = 1       # -90 dBFS, about the Korvo's idle loopback (-89)
LOUD = 300      # -41 dBFS, a reply at volume 60


def frame(loopback: int, mics: int = 0) -> bytes:
    a = np.full((320, 4), mics, dtype="<i2")
    a[:, 0] = loopback
    return a.tobytes()


def run(sense: OutputSense, t0: float, t1: float, loopback, mics: int = 0,
        playing=lambda t: False) -> list[tuple[float, str]]:
    """Feed frames from t0 to t1; loopback(t) is channel 0's value. The
    answers feed() gave, with when."""
    said = []
    for t in np.arange(t0, t1, FRAME_S):
        got = sense.feed(frame(loopback(t), mics), float(t), playing(t))
        if got is not None:
            said.append((round(float(t), 2), got))
    return said


def played(sense: OutputSense, start=1.0, end=2.0, level=-20.0, volume=60) -> None:
    sense.played(Sound(start, end, level, volume))


def test_levels_are_dbfs_and_digital_silence_is_finite():
    assert level_dbfs(np.full(320, 32767, "<i2")) == pytest.approx(0, abs=0.01)
    assert level_dbfs(np.full(320, LOUD, "<i2")) == pytest.approx(-40.8, abs=0.1)
    assert level_dbfs(np.zeros(320, "<i2")) == -120.0


def test_a_sound_the_loopback_heard_means_the_speaker():
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense)
    said = run(sense, 1.0, 3.0, lambda t: LOUD if 1.3 <= t <= 2.0 else QUIET)
    assert said and said[0][1] == SPEAKER
    assert said[0][0] >= 2.0 + 0.6, "judged before the sound had been heard out"
    assert sense.output == SPEAKER and sense.at is not None


def test_a_sound_the_loopback_did_not_hear_means_headphones():
    """A plug opens the jack's contacts that carry the codec's output to the
    amplifier and the loopback: the sound plays, and the loopback stays
    where it idles."""
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense)
    assert [s for _, s in run(sense, 1.0, 3.0, lambda t: QUIET)] == [HEADPHONES]


def test_only_the_loopback_counts_not_the_microphones():
    """The room is loud (someone talking over the reply): that is the
    microphones, and says nothing about where the reply went."""
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET, mics=3000)
    played(sense)
    assert [s for _, s in run(sense, 1.0, 3.0, lambda t: QUIET, mics=3000)] == [HEADPHONES]


@pytest.mark.parametrize("level, volume", [(-60.0, 60), (-20.0, 5), (-20.0, 0)])
def test_silence_after_a_quiet_sound_or_at_a_low_volume_proves_nothing(level, volume):
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense, level=level, volume=volume)
    assert run(sense, 1.0, 3.0, lambda t: QUIET) == []
    assert sense.output is None


def test_a_volume_the_satellite_has_not_reported_does_not_stop_a_verdict():
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense, volume=None)
    assert [s for _, s in run(sense, 1.0, 3.0, lambda t: QUIET)] == [HEADPHONES]


def test_a_rise_between_the_thresholds_leaves_the_last_answer_standing():
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense)
    run(sense, 1.0, 3.0, lambda t: LOUD if 1.3 <= t <= 2.0 else QUIET)
    assert sense.output == SPEAKER
    played(sense, start=3.0, end=4.0)
    # 2 against 1 is 6 dB: more than flat, less than heard.
    assert run(sense, 3.0, 5.0, lambda t: 2 if 3.3 <= t <= 4.0 else QUIET) == []
    assert sense.output == SPEAKER


def test_an_answer_is_reported_when_it_changes_not_at_every_sound():
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    said = []
    for start in (1.0, 3.0, 5.0):
        played(sense, start=start, end=start + 1.0)
        said += run(sense, start, start + 2.0,
                    lambda t, s=start: LOUD if (start < 5.0 and s + 0.3 <= t <= s + 1.0) else QUIET)
    assert [s for _, s in said] == [SPEAKER, HEADPHONES]


def test_the_idle_level_is_only_what_arrived_with_nothing_playing():
    """Frames while the hub's speaker loop is at work, or in the tail of a
    sound, are not the idle level. With none that are, nothing is judged."""
    sense = OutputSense(4)
    played(sense, start=0.0, end=2.0)
    assert run(sense, 0.0, 2.7, lambda t: QUIET, playing=lambda t: t < 2.0) == []
    assert sense.output is None and not sense.pending
    after = [t for t in np.arange(0.0, 2.7, FRAME_S) if t > 2.0 + 0.2]
    assert len(sense.idle) == len(after)


def test_a_sound_with_no_loopback_over_it_is_dropped_unjudged():
    """The microphones were off while it played (muted, or mic off): frames
    resume long after, and the sound is let go rather than judged on them."""
    sense = OutputSense(4)
    run(sense, 0.0, 1.0, lambda t: QUIET)
    played(sense)
    assert run(sense, 10.0, 11.0, lambda t: QUIET) == []
    assert not sense.pending and sense.output is None
