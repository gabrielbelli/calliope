"""AirPlay into a satellite (app/airplay.py).

The receiver is played by a thread writing into a real named pipe at real
time, as Shairport Sync does: 48 kHz 16-bit mono, 20 ms at a time. Music is a
constant, so a frame's mean says how much music is in it: 1000 at full level,
0 when it has faded out under a voice.
"""

from __future__ import annotations

import importlib
import os
import threading
import time

import numpy as np
import pytest

from app import airplay
from test_pipeline import NID, adopt, of, wait
from test_pipeline import client, env, events, fake_models, plug, services  # noqa: F401

RATE = 48000
CHUNK = RATE // 50 * 2  # 20 ms of 16-bit mono
LEVEL = 1000


def const(seconds: float, value: int = LEVEL) -> bytes:
    return np.full(int(seconds * RATE), value, "<i2").tobytes()


# ---- the music buffer ------------------------------------------------------------


def stream(m: airplay.Music, t0: float, seconds: float, per_block: float = 0.02,
           value: int = LEVEL) -> float:
    """A receiver writing 20 ms blocks every 20 ms, each holding per_block
    seconds of 48 kHz 16-bit mono: 0.04 is what stereo, or 32-bit, looks
    like. The time after the last."""
    t = t0
    for _ in range(round(seconds / 0.02)):
        m.feed(const(per_block, value), now=t)
        t += 0.02
    return t


def test_music_waits_for_its_prefill_and_then_goes_a_chunk_at_a_time():
    m = airplay.Music(RATE)
    t = stream(m, 0.0, 0.2)
    assert m.on(t) and not m.ready(CHUNK, now=t)
    t = stream(m, t, 0.1)
    assert m.ready(CHUNK, now=t)
    assert m.take(CHUNK) == const(0.02)
    m.take(m.size)
    assert not m.ready(CHUNK, now=t)
    stream(m, t, 0.02)
    assert m.ready(CHUNK, now=t + 0.02), "primed once, a chunk is enough until it stops"


def test_music_arriving_at_twice_its_rate_is_refused_as_noise_until_it_stops():
    """Stereo, or 32-bit samples, in a pipe the hub reads as 16-bit mono
    would play as full-scale noise."""
    m = airplay.Music(RATE)
    t = stream(m, 0.0, 0.3, per_block=0.04)
    assert not m.ready(CHUNK, now=t) and m.refused and not m.on(t)
    t = stream(m, t, 0.5)
    assert not m.on(t) and m.size == 0, "refused until it stops"
    t = stream(m, t + 1.5, 0.3)
    assert m.ready(CHUNK, now=t) and m.on(t), "a start after a stop is checked again"


def test_a_short_take_is_padded_with_silence():
    m = airplay.Music(RATE)
    m.feed(const(0.01), now=0.0)
    got = np.frombuffer(m.take(CHUNK), "<i2")
    assert len(got) == CHUNK // 2 and got[:480].min() == LEVEL and got[480:].max() == 0


def test_music_stops_after_a_second_without_any_and_starts_again_with_a_prefill_and_a_fade():
    m = airplay.Music(RATE)
    t = stream(m, 0.0, 0.4)
    assert m.ready(CHUNK, now=t)
    m.gain = 1.0
    assert m.on(t + 0.9) and not m.on(t + 1.1)
    m.clear()
    t = stream(m, 5.0, 0.1)
    assert m.on(t) and not m.ready(CHUNK, now=t) and m.gain == 0.0


def test_a_stalled_loop_keeps_only_the_newest_second():
    m = airplay.Music(RATE)
    for i in range(60):
        m.feed(const(0.05, value=i), now=i * 0.05)
    assert m.size == int(airplay.MAX_BUFFER_S * RATE) * 2
    assert np.frombuffer(m.take(CHUNK), "<i2")[0] == 40


def test_the_clock_servo_drops_a_chunk_when_the_satellite_holds_too_much():
    m = airplay.Music(RATE)
    m.feed(const(0.02, 1) + const(0.02, 2) + const(0.02, 3), now=0.0)
    m.buffered(800)
    assert np.frombuffer(m.take(CHUNK), "<i2")[0] == 2
    assert np.frombuffer(m.take(CHUNK), "<i2")[0] == 3, "once per report"


def test_the_clock_servo_repeats_a_chunk_when_the_satellite_holds_too_little():
    m = airplay.Music(RATE)
    m.feed(const(0.02, 1) + const(0.02, 2), now=0.0)
    first = m.take(CHUNK)
    m.buffered(40)
    assert m.take(CHUNK) == first
    assert np.frombuffer(m.take(CHUNK), "<i2")[0] == 2


@pytest.mark.parametrize("report", [None, "800", True, float("nan")])
def test_a_report_that_is_not_a_number_moves_nothing(report):
    m = airplay.Music(RATE)
    m.buffered(report)
    assert m.adjust == 0


# ---- the mix -------------------------------------------------------------------------


def test_the_voice_goes_on_top_of_the_music_and_the_sum_is_clipped():
    music = const(0.02, 30000)
    voice = const(0.02, 10000)
    out = np.frombuffer(airplay.mix(voice, music, 1.0, 1.0, 960), "<i2")
    assert out.max() == 32767
    assert np.frombuffer(airplay.mix(voice, music, 0.0, 0.0, 960), "<i2").tolist() == [10000] * 960


def test_the_music_is_ramped_across_the_chunk_so_a_fade_does_not_click():
    out = np.frombuffer(airplay.mix(None, const(0.02), 0.0, 1.0, 960), "<i2")
    assert out[0] == 0 and out[-1] > 990 and np.all(np.diff(out.astype(int)) >= 0)


def test_a_fade_takes_fade_s_either_way():
    g, steps = 0.0, 0
    while g < 1.0:
        g, steps = airplay.fade_step(g, 1.0, 0.02), steps + 1
    assert steps == round(airplay.FADE_S / 0.02)
    assert airplay.fade_step(1.0, 0.0, 0.02) == pytest.approx(1 - 0.02 / airplay.FADE_S)


# ---- the pipes ---------------------------------------------------------------------


def test_a_pipe_is_read_as_it_is_written_and_again_after_its_writer_goes(tmp_path):
    """No writer yet, a writer, the writer gone, another one: the reader
    opens without waiting, reads each, and does not spin in between."""
    os.mkfifo(tmp_path / "kitchen")
    (tmp_path / "notes.txt").write_text("not a pipe")
    got: list[tuple[str, bytes]] = []
    pipes = airplay.Pipes(tmp_path, lambda name, data: got.append((name, data)))
    try:
        assert pipes.scan() == ["kitchen"]
        assert pipes.scan() == [], "one reader per pipe"
        for n in (1, 2):
            fd = os.open(tmp_path / "kitchen", os.O_WRONLY)
            os.write(fd, bytes([n]) * 100)
            os.close(fd)
            wait(lambda: sum(len(d) for _, d in got) == 100 * n, what=f"write {n}")
        assert {name for name, _ in got} == {"kitchen"}
        before = time.thread_time()
        time.sleep(0.5)
        assert time.thread_time() - before < 0.2, "the reader spins with no writer"
    finally:
        pipes.stop()


# ---- through the hub --------------------------------------------------------------


@pytest.fixture
def pipes(tmp_path, monkeypatch):
    d = tmp_path / "airplay"
    d.mkdir()
    os.mkfifo(d / "kitchen")
    monkeypatch.setenv("SATELLITES_AIRPLAY_DIR", str(d))
    return d


@pytest.fixture
def app(env, fake_models, pipes):  # noqa: F811
    return importlib.reload(importlib.import_module("app.main"))


def play(path, seconds: float, value: int = LEVEL) -> threading.Thread:
    """Shairport Sync: 20 ms of music into the pipe every 20 ms."""
    def run():
        fd = os.open(path, os.O_WRONLY)
        try:
            t0 = time.monotonic()
            for i in range(int(seconds * 50)):
                os.write(fd, const(0.02, value))
                time.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.monotonic()))
        finally:
            os.close(fd)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def means(satellite) -> list[float]:
    return [float(np.frombuffer(f[16:], "<i2").mean()) for f in satellite.speaker()]


def test_music_in_a_pipe_named_for_a_satellite_plays_on_it(client, app, events, plug, pipes):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, name="kitchen")
        satellite = plug(ws)
        player = play(pipes / "kitchen", 1.2)
        wait(lambda: any(m == LEVEL for m in means(satellite)), what="music at full level")
        assert client.get(f"/satellites/{NID}").json()["airplay"] is True
        player.join()
        wait(lambda: [e["state"] for e in of(events, "airplay")] == ["playing", "stopped"],
             timeout=5, what="the music to stop")
    levels = means(satellite)
    assert levels[0] < LEVEL, "music came in without a fade"
    # 1.2 s of music, and nothing invented: silence padding at most at the end.
    assert 1.0 <= len(levels) * 0.02 <= 1.3


def test_music_fades_out_under_a_voice_and_comes_back_after_it(client, app, events, plug, pipes):
    """A reply (here a tone) plays alone; the music under it is out, not down,
    so the loopback goes quiet for a follow-up."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, name="kitchen")
        satellite = plug(ws)
        player = play(pipes / "kitchen", 3.0)
        wait(lambda: any(m == LEVEL for m in means(satellite)), what="music at full level")
        assert client.post(f"/satellites/{NID}/tone",
                           json={"frequency": 440, "seconds": 0.8}).status_code == 204
        player.join()
    levels = means(satellite)
    full = [i for i, m in enumerate(levels) if m == LEVEL]
    tone_only = [i for i, m in enumerate(levels) if abs(m) < 300 and i > full[0]]
    assert tone_only, "the music never went out under the tone"
    assert any(i > tone_only[-1] for i in full), "the music never came back"
    assert len(tone_only) >= 0.8 / 0.02 - 2 * airplay.FADE_S / 0.02


def test_with_its_speaker_off_a_satellite_is_sent_no_music(client, app, events, plug, pipes):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, name="kitchen")
        assert client.patch(f"/satellites/{NID}", json={"speaker_enabled": False}).status_code == 200
        assert ws.receive_json()["type"] == "config"
        satellite = plug(ws)
        play(pipes / "kitchen", 0.8).join()
        wait(lambda: of(events, "airplay"), what="the music")
        assert client.get(f"/satellites/{NID}").json()["airplay"] is True
        time.sleep(0.2)
    assert satellite.speaker() == []


def test_music_for_a_satellite_that_is_not_here_goes_nowhere(client, app, events, pipes):
    play(pipes / "kitchen", 0.5).join()
    time.sleep(0.2)
    assert of(events, "airplay") == []
