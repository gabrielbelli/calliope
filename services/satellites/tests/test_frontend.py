"""The live audio front-end, on synthetic scenes built here from seeded random
numbers. Nothing touches a device, a network or a file.

A scene is what the Korvo sends: channel 0 the speaker loopback, channels 1-3
three microphones on an equilateral triangle of side 65 mm. Talkers and noise
sources are plane waves with exact fractional delays; the speaker's echo is a
direct path plus a decaying random tail, different at each microphone.

SNR is measured by shadow filtering: the front-end records the beam weights and
the suppression gain it used for every block, and those are replayed on the
talker and the noise separately. That is exact, because with a silent
reference the canceller passes each component through untouched, and with the
weights and gains fixed the rest of the chain is linear.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from app import frontend
from app.frontend import EchoCanceller, FrontEnd

RATE = 16000


# ---- scenes -------------------------------------------------------------------


def dbfs(level: float) -> float:
    return 32768.0 * 10 ** (level / 20)


def speechlike(seconds: float, seed: int) -> np.ndarray:
    """Voiced syllables with a gliding pitch and three formants, in phrases
    with pauses between them; unit RMS over the voiced part. Enough of
    speech's structure (harmonics, onsets, gaps) for every stage to treat it
    as speech, and nothing else."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    out = np.zeros(n)
    t = int(0.2 * RATE)
    while t < n:
        phrase_end = min(n, t + int(rng.uniform(1.2, 2.5) * RATE))
        while t < phrase_end:
            length = min(int(rng.uniform(0.10, 0.25) * RATE), n - t)
            if length <= 16:
                break
            f0a, f0b = rng.uniform(100, 220, 2)
            f0 = np.linspace(f0a, f0b, length)
            phase = 2 * np.pi * np.cumsum(f0) / RATE
            formants = rng.uniform(300, 800), rng.uniform(900, 2200), rng.uniform(2400, 3200)
            k = np.arange(1, int(RATE / 2 * 0.9 // max(f0a, f0b)) + 1)
            fk = k * f0.mean()
            env = sum(1 / (1 + ((fk - f) / (0.1 * f + 60)) ** 2) for f in formants) + 0.05
            amp = env / np.sqrt(k)
            seg = (amp[:, None] * np.sin(k[:, None] * phase[None, :])).sum(0)
            seg += 0.05 * amp.max() * rng.standard_normal(length)
            ramp = min(length // 3, int(0.03 * RATE))
            w = np.ones(length)
            w[:ramp] = 0.5 - 0.5 * np.cos(np.pi * np.arange(ramp) / ramp)
            w[-ramp:] = w[:ramp][::-1]
            out[t:t + length] += seg * w
            t += length + int(rng.uniform(0.03, 0.10) * RATE)
        t = phrase_end + int(rng.uniform(0.3, 0.7) * RATE)
    return out / np.sqrt(np.mean(out[out != 0] ** 2))


def delayed(x: np.ndarray, samples: float) -> np.ndarray:
    nfft = 1 << math.ceil(math.log2(len(x) + 64))
    f = np.fft.rfftfreq(nfft)
    return np.fft.irfft(np.fft.rfft(x, nfft) * np.exp(-2j * np.pi * f * samples), nfft)[: len(x)]


def plane_wave(x: np.ndarray, azimuth: float) -> np.ndarray:
    """(3, n): `x` arriving from `azimuth`, in the module's own convention."""
    u = np.array([math.cos(math.radians(azimuth)), math.sin(math.radians(azimuth))])
    t = -(frontend.mic_positions(0.065) @ u) / frontend.SPEED_OF_SOUND * RATE
    return np.stack([delayed(x, d) for d in t - t[0]])


def echo_paths(seed: int = 7, taps: int = 1024) -> np.ndarray:
    """(3, taps): a fractional direct path and a 25 ms decaying tail per mic."""
    rng = np.random.default_rng(seed)
    paths = []
    for m in range(3):
        h = np.zeros(taps)
        k = np.arange(64)
        h[:64] = np.sinc(k - (3.0 + 0.7 * m)) * np.kaiser(64, 6)
        tail = rng.standard_normal(taps) * np.exp(-np.arange(taps) / (0.025 * RATE))
        tail[:40] = 0
        h += 0.6 * tail
        paths.append(h / np.sqrt((h ** 2).sum()) * 0.3)
    return np.array(paths)


def echo(far: np.ndarray, paths: np.ndarray) -> np.ndarray:
    return np.stack([np.convolve(far, h)[: len(far)] for h in paths])


def satellite_frames(reference: np.ndarray, mics: np.ndarray) -> np.ndarray:
    """What the satellite sends: int16 (n, 4), loopback first."""
    return np.clip(np.rint(np.column_stack([reference, mics.T])), -32768, 32767).astype(np.int16)


def sensor_noise(shape: tuple[int, int], level: float = -75.0, seed: int = 3) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(shape) * dbfs(level)


def feed(fe: FrontEnd, frames: np.ndarray, chunk: int = 320) -> np.ndarray:
    """In the satellite's own 20 ms frames."""
    return np.concatenate([fe.process(frames[i:i + chunk]) for i in range(0, len(frames), chunk)])


def energy_db(x: np.ndarray) -> float:
    return 10 * math.log10(float(np.sum(np.asarray(x, float) ** 2)) + 1e-9)


def talker_and_noise(talker_az: float, noise_az: float, seconds: float = 12,
                     snr_db: float = 0.0, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """(talker, noise) at the three mics; white noise from a point source,
    plus each mic's own noise."""
    talk = speechlike(seconds, seed) * dbfs(-30)
    noise = np.random.default_rng(seed).standard_normal(len(talk))
    noise *= np.sqrt(np.mean(talk[talk != 0] ** 2) / np.mean(noise ** 2)) * 10 ** (-snr_db / 20)
    return plane_wave(talk, talker_az), plane_wave(noise, noise_az) + sensor_noise((3, len(talk)))


def traced_run(fe: FrontEnd, frames: np.ndarray) -> np.ndarray:
    """Feed one block per call, so each recorded (weights, gain) belongs to
    exactly one block."""
    fe._trace = []
    return np.concatenate([fe.process(frames[i:i + fe.hop]) for i in range(0, len(frames), fe.hop)])


def replay(fe: FrontEnd, component: np.ndarray, gains: bool = True) -> np.ndarray:
    """One component through the recorded beam (and suppression) of a run."""
    n = fe.hop
    prev = np.zeros((component.shape[0], n))
    ola = np.zeros(n)
    out = []
    for b, (w, g) in enumerate(fe._trace):
        cur = component[:, b * n:(b + 1) * n]
        z = np.fft.rfft(np.concatenate((prev, cur), axis=1) * fe.window, axis=-1)
        prev = cur
        s = np.einsum("fm,mf->f", w.conj(), z) * (g if gains else 1.0)
        synth = np.fft.irfft(s, n=fe.nfft) * fe.window
        out.append(ola + synth[:n])
        ola = synth[n:]
    return np.concatenate(out)


def aligned(fe: FrontEnd, x: np.ndarray, length: int) -> np.ndarray:
    """A microphone signal shifted by the front-end's one-block delay."""
    return np.concatenate((np.zeros(fe.hop), x))[:length]


# ---- echo ---------------------------------------------------------------------


def test_echo_of_the_satellites_own_speaker_drops_by_at_least_15_db():
    far = speechlike(12, seed=1) * dbfs(-20)
    mics = echo(far, echo_paths()) + sensor_noise((3, len(far)))
    frames = satellite_frames(far, mics)
    fe = FrontEnd()
    out = feed(fe, frames)
    late = slice(8 * RATE, 12 * RATE)
    # Measured 55.4 dB here, and 37-55 dB for every stretch after the first 2 s.
    assert energy_db(frames[late, 1]) - energy_db(out[late]) >= 15.0


def test_the_canceller_itself_converges_so_the_suppressor_does_not_carry_it():
    # The suppressor alone could hide a canceller that never converged, by
    # gating the echo down to its floor. That would take near-end speech with
    # it during playback, so the linear filter is held to its own standard.
    far = speechlike(12, seed=1) * dbfs(-20)
    mics = echo(far, echo_paths()) + sensor_noise((3, len(far)))
    aec = EchoCanceller(256, 8, 3, RATE)
    floor = 256 * frontend._dbfs_power(frontend.FAR_END_DBFS)
    errors = []
    for b in range(len(far) // 256):
        x = far[b * 256:(b + 1) * 256]
        e, _ = aec.process(np.ascontiguousarray(mics[:, b * 256:(b + 1) * 256]), x, float(x @ x) > floor)
        errors.append(e)
    err = np.concatenate(errors, axis=1)
    late = slice(8 * RATE, 12 * RATE)
    # Measured 35.8 dB of ERLE over 8-12 s.
    assert energy_db(mics[:, late]) - energy_db(err[:, late]) >= 20.0


def misalignment_db(aec: EchoCanceller, paths: np.ndarray) -> float:
    got = aec.impulse_response()
    want = np.zeros_like(got)
    want[:, : paths.shape[1]] = paths
    return 10 * math.log10(float(((got - want) ** 2).sum() / (want ** 2).sum()))


def test_someone_talking_over_playback_does_not_drag_the_echo_filter_off():
    # Double-talk: 4 s of a near-end talker 6 dB above the echo, while the
    # satellite plays. Measured: misalignment -23.4 dB before, -23.7 dB after. A
    # fixed-step filter with the two-path logic disabled went from -23.4 to
    # -7.5 dB on the same scene, so this test does tell the two apart.
    secs, start, stop = 12, 6, 10
    far = speechlike(secs, seed=1) * dbfs(-20)
    far += np.random.default_rng(9).standard_normal(len(far)) * dbfs(-45)  # never quite silent
    paths = echo_paths()
    mics = echo(far, paths)
    near = speechlike(secs, seed=21)
    near[: start * RATE] = 0
    near[stop * RATE:] = 0
    near *= np.sqrt(np.mean(mics[0] ** 2)) * 10 ** (6 / 20)
    mics = mics + plane_wave(near, 120) + sensor_noise(mics.shape)
    aec = EchoCanceller(256, 8, 3, RATE)
    floor = 256 * frontend._dbfs_power(frontend.FAR_END_DBFS)
    before = None
    for b in range(len(far) // 256):
        if b * 256 == start * RATE:
            before = misalignment_db(aec, paths)
        x = far[b * 256:(b + 1) * 256]
        aec.process(np.ascontiguousarray(mics[:, b * 256:(b + 1) * 256]), x, float(x @ x) > floor)
        if (b + 1) * 256 >= stop * RATE:
            break
    after = misalignment_db(aec, paths)
    assert before is not None and before < -15.0
    assert after < before + 3.0


def test_the_satellites_own_voice_never_steers_the_beam():
    # The loudest, most speech-like thing a satellite hears is its own speaker.
    # If the beam's statistics ran during playback, it would steer at itself.
    far = speechlike(12, seed=1) * dbfs(-20)
    mics = echo(far, echo_paths()) + sensor_noise((3, len(far)), level=-62)
    fe = FrontEnd()
    flags = []
    frames = satellite_frames(far, mics)
    for i in range(0, len(frames), 320):
        fe.process(frames[i:i + 320])
        flags.append(fe.speech)
    assert fe.direction is None
    assert fe.stats["beamformer"] == "average"
    # Nor is the residual echo taken for a talker once the canceller has
    # converged: measured, no block flagged in the whole 12 s.
    assert np.mean(flags[len(flags) // 3:]) <= 0.02


# ---- beam -----------------------------------------------------------------------


@pytest.mark.parametrize("talker_az,noise_az", [(60, 200), (300, 90)])
def test_a_talker_is_heard_over_a_noise_from_another_direction(talker_az, noise_az):
    talk, noise = talker_and_noise(talker_az, noise_az)
    fe = FrontEnd()
    out = traced_run(fe, satellite_frames(np.zeros(talk.shape[1]), talk + noise))
    assert fe.stats["beamformer"] == "mvdr"
    late = slice(len(out) // 2, None)  # after the statistics have settled

    def snr(s, v):
        return energy_db(s[late]) - energy_db(v[late])

    mic1 = snr(aligned(fe, talk[0], len(out)), aligned(fe, noise[0], len(out)))
    beam = snr(replay(fe, talk, gains=False), replay(fe, noise, gains=False))
    full = snr(replay(fe, talk), replay(fe, noise))
    # Measured: mic 1 -2.0 dB; beam +22.6 and +22.3 dB; full +35.1 and
    # +36.6 dB, for the two scenes.
    assert beam - mic1 >= 10.0
    assert full - mic1 >= 15.0
    # And the talker comes through at the level mic 1 heard, not cancelled
    # with the noise: measured -0.3 dB.
    speech_gain = energy_db(replay(fe, talk, gains=False)[late]) - energy_db(aligned(fe, talk[0], len(out))[late])
    assert abs(speech_gain) <= 1.0
    # Measured 2.4 and 1.2 degrees off; the tolerance is for a real board.
    assert fe.direction is not None
    assert abs((fe.direction - talker_az + 180) % 360 - 180) <= 10.0


def test_direction_is_withdrawn_when_nobody_has_spoken_for_a_while():
    talk, noise = talker_and_noise(60, 200, seconds=8, snr_db=10)
    fe = FrontEnd()
    assert fe.direction is None
    feed(fe, satellite_frames(np.zeros(talk.shape[1]), talk + noise))
    assert fe.direction is not None
    quiet = plane_wave(np.random.default_rng(4).standard_normal(4 * RATE), 200) * dbfs(-50)
    feed(fe, satellite_frames(np.zeros(quiet.shape[1]), quiet + sensor_noise(quiet.shape)))
    assert fe.direction is None


def test_the_speech_flag_follows_the_talker_and_drops_after():
    talk, noise = talker_and_noise(60, 200, seconds=8, snr_db=10)
    tail = 2 * RATE
    talk = np.concatenate((talk, np.zeros((3, tail))), axis=1)
    noise = np.concatenate((noise, plane_wave(np.random.default_rng(5).standard_normal(tail), 200)
                            * np.std(noise[0]) + sensor_noise((3, tail))), axis=1)
    frames = satellite_frames(np.zeros(talk.shape[1]), talk + noise)
    fe = FrontEnd()
    flags = []
    for i in range(0, len(frames), fe.hop):
        fe.process(frames[i:i + fe.hop])
        flags.append(fe.speech)
    blocks = np.array([np.mean(talk[0, i:i + fe.hop] ** 2) for i in range(0, talk.shape[1], fe.hop)])
    talking = blocks > 1e-3 * np.median(blocks[blocks > np.percentile(blocks, 60)])
    # Measured: 99.3% of talking blocks flagged.
    assert np.mean(np.array(flags)[talking]) >= 0.9
    assert not fe.speech


# ---- the stream -------------------------------------------------------------------


def test_silence_in_is_silence_out():
    fe = FrontEnd()
    out = fe.process(np.zeros((3 * RATE, 4), np.int16))
    assert len(out) == 3 * RATE // fe.hop * fe.hop and not out.any()
    assert not fe.speech and fe.direction is None

    # The Korvo's idle floor: loopback -89 dBFS, microphones about -63 dBFS.
    rng = np.random.default_rng(0)
    idle = np.column_stack([rng.standard_normal(5 * RATE) * dbfs(-89),
                            rng.standard_normal((5 * RATE, 3)) * dbfs(-63)])
    idle = np.rint(idle).astype(np.int16)
    fe = FrontEnd()
    out = feed(fe, idle)
    late = slice(2 * RATE, None)
    # Measured 22.6 dB down: the 18 dB floor, plus the beam averaging three
    # independent noises.
    assert energy_db(out[late]) <= energy_db(idle[late, 1]) - 15.0
    assert not fe.speech


def test_output_length_tracks_input_length_whatever_the_chunking():
    fe = FrontEnd()
    rng = np.random.default_rng(1)
    fed = got = 0
    for size in [1, 7, 320, 0, 999, 256, 255, 1, 4000, 13, 320, 320]:
        chunk = np.rint(rng.standard_normal((size, 4)) * 1000).astype(np.int16)
        out = fe.process(chunk)
        assert out.dtype == np.int16 and out.ndim == 1
        fed += size
        got += len(out)
        # Everything but the unfilled part of one block comes straight back.
        assert got == fed // fe.hop * fe.hop
        assert fed - got < fe.hop


def test_latency_is_one_block_and_within_budget():
    rng = np.random.default_rng(2)
    x = rng.standard_normal(3 * RATE) * dbfs(-60)
    x[RATE:2 * RATE] = rng.standard_normal(RATE) * dbfs(-20)
    frames = satellite_frames(np.zeros(len(x)), np.stack([x, x, x]))
    out = feed(FrontEnd(), frames).astype(float)
    xc = np.fft.irfft(np.fft.rfft(out, 2 * len(out)) * np.fft.rfft(frames[:, 1].astype(float), 2 * len(out)).conj())
    lag = int(np.argmax(xc[:4000]))
    fe = FrontEnd()
    assert lag == fe.hop
    assert fe.latency_s <= 0.064


def test_no_nans_from_all_zero_or_clipped_input():
    fe = FrontEnd()
    fe.process(np.zeros((2 * RATE, 4), np.int16))
    t = np.arange(4 * RATE)
    square = np.where(np.sin(2 * np.pi * 440 * t / RATE) >= 0, 32767, -32768)
    rng = np.random.default_rng(3)
    clipped = np.column_stack([square, square, -square,
                               np.clip(rng.standard_normal(len(t)) * 1e6, -32768, 32767)]).astype(np.int16)
    out = fe.process(clipped)
    for state in (fe.aec.W, fe.aec.Wf, fe.bf.w, fe.ns.g_prev, fe.ns.tracker.var, fe._ola):
        assert np.isfinite(state).all()
    assert all(v is None or not isinstance(v, float) or math.isfinite(v) for v in fe.stats.values())
    # And it is still a working front-end afterwards, not a stuck one.
    talk, noise = talker_and_noise(60, 200, seconds=4, snr_db=10)
    after = feed(fe, satellite_frames(np.zeros(talk.shape[1]), talk + noise))
    assert energy_db(after) > energy_db(np.zeros(1)) + 60
    assert len(out) == len(clipped)


def test_reset_is_as_good_as_a_new_front_end():
    talk, noise = talker_and_noise(60, 200, seconds=4, snr_db=10)
    frames = satellite_frames(np.zeros(talk.shape[1]), talk + noise)
    fresh = feed(FrontEnd(), frames)
    fe = FrontEnd()
    feed(fe, frames[: 3 * RATE + 17])
    fe.reset()
    assert fe.stats["seconds"] == 0 and fe.direction is None and not fe.speech
    assert np.array_equal(feed(fe, frames), fresh)


def test_a_single_microphone_still_gets_echo_cancellation_and_suppression():
    far = speechlike(10, seed=1) * dbfs(-20)
    mics = echo(far, echo_paths()) + sensor_noise((3, len(far)))
    frames = satellite_frames(far, mics)
    fe = FrontEnd(mic_channels=(1,))
    out = feed(fe, frames)
    late = slice(6 * RATE, None)
    assert fe.stats["beamformer"] == "single" and fe.direction is None
    # Measured 43.4 dB.
    assert energy_db(frames[late, 1]) - energy_db(out[late]) >= 15.0


def test_too_few_channels_is_an_error_not_a_misread_stream():
    with pytest.raises(ValueError):
        FrontEnd().process(np.zeros((320, 2), np.int16))


def test_ten_seconds_of_audio_take_well_under_ten_seconds():
    # Everything running at once: playback, a talker and a noise source, so
    # every stage, the steering fit and the MVDR solve are all on the clock.
    far = speechlike(10, seed=1) * dbfs(-20)
    talk, noise = talker_and_noise(60, 200, seconds=10, snr_db=5)
    mics = echo(far, echo_paths()) + talk + noise
    frames = satellite_frames(far, mics)
    fe = FrontEnd()
    start = time.perf_counter()
    feed(fe, frames)
    elapsed = time.perf_counter() - start
    # Measured 0.15-0.16 s on one core of an M2 Max: a real-time factor of
    # 0.015. The bound leaves room for a machine sixteen times slower.
    assert elapsed < 2.5
    assert fe.stats["rtf"] < 0.25
