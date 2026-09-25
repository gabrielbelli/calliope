"""listening.Ear on its own: states, push-to-talk, where a command starts, and
the NODES_WAKE_WORDS parser. The wake word model is a fake that fires on a
marker sample; the endpointer is the real one (webrtcvad needs no files)."""

from __future__ import annotations

import numpy as np
import pytest

from app.listening import PTT, Command, Ear, Heard, parse_wake_words, room_floor
from app.wakeword import Detection

RATE = 16000
MARK = 30000


class Marker:
    def __init__(self):
        self.position = 0

    def feed(self, pcm):
        self.position += len(pcm)
        hits = np.flatnonzero(pcm >= MARK)
        return ([Detection("hey_jarvis", 0.9, self.position - len(pcm) + int(hits[-1]) + 1)]
                if len(hits) else [])


def voiced(seconds: float, level: int = 6000) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    phase = 2 * np.pi * np.cumsum(130 + 15 * np.sin(2 * np.pi * 0.8 * t)) / RATE
    y = sum(np.sin(k * phase) / k for k in range(1, 25)) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return (y / np.abs(y).max() * level).astype(np.int16)


def run(ear: Ear, mono: np.ndarray, n: int = 320) -> list:
    out = []
    for off in range(0, len(mono), n):
        out += ear.process(mono[off:off + n].reshape(-1, 1))
    return out


def test_the_command_is_the_audio_after_the_wake_word_and_not_the_word_itself():
    ear = Ear(channels=1, frontend=False, wake=Marker())
    speech = voiced(1.0, level=5000)
    # The marker lands mid-chunk, so the split must be inside process().
    audio = np.concatenate((room_floor(0.21), np.full(10, MARK, np.int16), speech,
                            room_floor(1.2, seed=1)))
    events = run(ear, audio)
    heard, command = events
    assert isinstance(heard, Heard) and isinstance(command, Command)
    assert heard.wake_word == "hey_jarvis" and heard.at_s == pytest.approx(3370 / RATE, abs=1e-3)
    assert command.reason == "silence" and command.had_speech
    got = np.frombuffer(command.audio, "<i2")
    # No marker in the command, and the speech is in it whole.
    assert got.max() < MARK and len(got) >= len(speech)
    assert ear.state == "busy"


def test_a_wake_word_while_busy_or_listening_is_not_reported():
    ear = Ear(channels=1, frontend=False, wake=Marker())
    mark = np.full(320, MARK, np.int16)
    assert isinstance(run(ear, mark)[0], Heard)
    assert run(ear, mark) == []          # listening
    ear.state = "busy"
    assert run(ear, mark) == []          # busy
    ear.release()
    assert isinstance(run(ear, mark)[0], Heard)


def test_push_to_talk_takes_the_next_audio_as_the_command_even_with_no_model():
    ear = Ear(channels=1, frontend=False, wake=None)
    ear.push_to_talk()
    events = run(ear, np.concatenate((voiced(0.8), room_floor(1.2))))
    assert [type(e) for e in events] == [Heard, Command]
    assert events[0].wake_word == PTT and events[0].score is None and events[0].at_s == 0.0


def test_push_to_talk_cancelled_before_audio_arrives_starts_nothing():
    ear = Ear(channels=1, frontend=False, wake=None)
    ear.push_to_talk()
    ear.cancel_ptt()
    assert run(ear, voiced(0.5)) == [] and ear.state == "idle"


def test_without_the_front_end_the_first_microphone_is_listened_to_not_the_loopback():
    ear = Ear(channels=4, frontend=False, wake=Marker())
    frames = np.zeros((320, 4), np.int16)
    frames[:, 0] = MARK  # the speaker's own loopback: never a wake word
    assert ear.process(frames) == []
    frames[:, 0], frames[:, 1] = 0, MARK
    assert isinstance(ear.process(frames)[0], Heard)


def test_a_microphone_rate_the_models_cannot_take_is_refused():
    with pytest.raises(ValueError, match="16000"):
        Ear(rate=48000, channels=4)


@pytest.mark.parametrize("spec,expected", [
    ("hey_jarvis:0.5", {"hey_jarvis": 0.5}),
    ("hey_jarvis:0.5, alexa:0.6", {"hey_jarvis": 0.5, "alexa": 0.6}),
    ("hey_jarvis", {"hey_jarvis": 0.5}),
    ("", {}),
    (" , ", {}),
])
def test_wake_word_settings_parse(spec, expected):
    assert parse_wake_words(spec) == expected


@pytest.mark.parametrize("spec", ["hey_jarvis:loud", "hey_jarvis:0", "hey_jarvis:1.5", ":0.5", "ptt:0.5"])
def test_a_wake_word_setting_that_cannot_be_right_is_refused_by_name(spec):
    with pytest.raises(ValueError):
        parse_wake_words(spec)
