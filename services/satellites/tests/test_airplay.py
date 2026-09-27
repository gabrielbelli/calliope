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
    assert m.on(t) and not m.ready(CHUNK)
    t = stream(m, t, 0.1)
    assert m.ready(CHUNK)
    assert m.take(CHUNK) == const(0.02)
    m.take(m.size)
    assert not m.ready(CHUNK)
    stream(m, t, 0.02)
    assert m.ready(CHUNK), "primed once, a chunk is enough until it stops"


def sine(freq: float, seconds: float, level: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")


def feed_all(m: airplay.Music, pcm: bytes, now: float, block: int = 4096) -> None:
    """At once, in the reader's 4096-byte blocks: how the lead-in comes."""
    for i in range(0, len(pcm), block):
        m.feed(pcm[i:i + block], now=now)


def test_the_lead_in_silence_comes_at_once_and_is_kept_to_be_played():
    """Shairport Sync writes everything up to the first sample's moment as
    zeros, in a millisecond (its player.c, for an output with no delay). The
    first rate check read that as a format twice too fast and refused every
    song; and the silence is the timing, so none of it may be dropped."""
    m = airplay.Music(RATE)
    feed_all(m, const(2.0, 0), now=0.0)
    assert not m.refused and m.size == len(const(2.0, 0))
    assert m.ready(CHUNK), "silence counts towards the prefill"
    t = stream(m, 0.001, 3.5)          # then the music, in real time
    assert not m.refused and m.on(t)


def test_the_early_release_burst_and_its_pace_are_not_mistaken_for_stereo():
    """audio_backend_buffer_desired_length_in_seconds = 0.5: the first half
    second of music comes at once, then real time."""
    m = airplay.Music(RATE)
    feed_all(m, const(1.5, 0), now=0.0)
    feed_all(m, sine(440, 0.5).tobytes(), now=0.01)
    t = 0.01
    for _ in range(175):               # 3.5 s more, 20 ms at a time
        t += 0.02
        m.feed(sine(440, 0.02).tobytes(), now=t)
    assert m.rate_checked and not m.refused


def test_32_bit_samples_read_as_16_are_refused_at_their_first_audio():
    """The low halves between the high ones: a loud buzz, stopped before a
    chunk of it plays."""
    m = airplay.Music(RATE)
    feed_all(m, const(1.0, 0), now=0.0)
    s32 = sine(440, 0.05).astype(np.int32) << 16
    rng = np.random.default_rng(1)
    s32 |= rng.integers(0, 1 << 16, s32.size)      # a decoder's low bits, or dither
    m.feed(s32.astype("<i4").tobytes()[:4096], now=0.01)
    assert m.refused and not m.on(0.02) and m.size == 0


def test_32_bit_samples_from_a_16_bit_source_are_refused_too():
    """Zeros for low halves: the song at half speed with a zero between every
    two samples."""
    m = airplay.Music(RATE)
    s32 = (sine(1000, 0.05).astype(np.int32) << 16).astype("<i4")
    m.feed(s32.tobytes()[:4096], now=0.0)
    assert m.refused


def test_stereo_read_as_mono_is_refused_once_its_rate_shows():
    """Not noise, the song at half speed: it plays until three seconds of
    audio say its rate is twice mono's."""
    m = airplay.Music(RATE)
    t = stream(m, 0.0, 2.9, per_block=0.04)
    assert not m.refused
    stream(m, t, 0.2, per_block=0.04)
    assert m.refused


@pytest.mark.parametrize("freq", [50, 440, 1000, 4000, 8000, 11000, 16000, 20000])
def test_music_is_not_mistaken_for_interleaved_samples(freq):
    assert not airplay.interleaved(sine(freq, 0.05))


def test_neither_is_noise_nor_a_mix():
    rng = np.random.default_rng(2)
    noise = (rng.normal(0, 3000, 2048)).astype("<i2")
    chord = (sine(220, 0.05) // 3 + sine(277, 0.05) // 3 + sine(330, 0.05) // 3)
    assert not airplay.interleaved(noise) and not airplay.interleaved(chord)


def test_music_stops_after_a_second_without_any_and_starts_again_with_a_prefill_and_a_fade():
    m = airplay.Music(RATE)
    t = stream(m, 0.0, 0.4)
    assert m.ready(CHUNK)
    m.gain = 1.0
    assert m.on(t + 0.9) and not m.on(t + 1.1)
    m.clear()
    t = stream(m, 5.0, 0.1)
    assert m.on(t) and not m.ready(CHUNK) and m.gain == 0.0


def test_a_stalled_loop_keeps_only_the_newest_second():
    m = airplay.Music(RATE)
    for i in range(100):
        m.feed(const(0.05, value=100 + i), now=i * 0.05)
    assert m.size == int(airplay.MAX_BUFFER_S * RATE) * 2
    assert np.frombuffer(m.take(CHUNK), "<i2")[0] == 100 + 100 - int(airplay.MAX_BUFFER_S / 0.05)


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


def play(path, seconds: float, value: int = LEVEL, lead_in: float = 0.0) -> threading.Thread:
    """Shairport Sync: its lead-in as silence at once, then 20 ms of music
    into the pipe every 20 ms."""
    def run():
        fd = os.open(path, os.O_WRONLY)
        try:
            if lead_in:
                os.write(fd, const(lead_in, 0))
            t0 = time.monotonic() + lead_in
            for i in range(int(seconds * 50)):
                os.write(fd, const(0.02, value))
                time.sleep(max(0.0, t0 + (i + 1) * 0.02 - time.monotonic()))
        except BrokenPipeError:
            pass  # the hub went first: the test is over
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


def test_a_start_with_shairport_syncs_lead_in_plays_the_silence_then_the_music(
        client, app, events, plug, pipes):
    """The lead-in comes at once and is the timing: the music follows it,
    rather than being refused or starting early."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws, name="kitchen")
        satellite = plug(ws)
        t0 = time.monotonic()
        play(pipes / "kitchen", 1.0, lead_in=0.6)
        wait(lambda: any(m == LEVEL for m in means(satellite)), what="music at full level")
        heard = time.monotonic() - t0
    levels = means(satellite)
    assert levels[0] == 0, "the lead-in was dropped"
    assert heard >= 0.6 - 0.3, "the music came before its lead-in was over"


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
