"""Wake words and endpointing, on recorded speech and synthetic signals.

The speech fixtures were made offline with macOS's `say`, which writes a file
and plays nothing, then brought to 16 kHz mono s16le:

    say -v Samantha -o x.aiff "hey jarvis, what time is it"   # hey_jarvis_en_us.wav
    say -v "Daniel (English (UK))" -o x.aiff "hey jarvis, what time is it"   # _en_gb
    say -v Samantha -o x.aiff "the weather is fine today"     # weather_en_us.wav
    ffmpeg -i x.aiff -ar 16000 -ac 1 -c:a pcm_s16le -map_metadata -1 out.wav

Not every voice works: the same phrase in the system's default voice peaked
at 0.05 for hey_jarvis, far under any usable threshold. The model is not
voice-independent, and a test that passes on these two voices says nothing
about a third.

The openWakeWord models these tests load (5.4 MB) are too big for fixtures.
They are fetched once per session into a temporary directory, or into
NODES_TEST_WAKEWORD_DIR if that is set, so repeated runs can skip the fetch.
Offline, every test that needs them is skipped with the reason; the Endpointer
and ensure_models tests need no network and always run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from app import wakeword
from app.wakeword import Detection, Endpointer, WakeWords, ensure_models

FIXTURES = Path(__file__).parent / "fixtures"
RATE = 16000
LOADED = {"hey_jarvis": 0.5, "alexa": 0.5, "hey_mycroft": 0.5}


def clip(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int16)


def floor(seconds: float, dbfs: float = -60, seed: int = 0) -> np.ndarray:
    """A microphone's noise floor. Digital zeros are not what a room sounds like."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * RATE)) * 32768 * 10 ** (dbfs / 20)).astype(np.int16)


def voiced(seconds: float, level: int = 6000) -> np.ndarray:
    """Speech-like: a gliding 130 Hz harmonic series, modulated at a syllable
    rate of 4 Hz. webrtcvad calls every frame of it voiced; no wake word
    model should fire on it."""
    t = np.arange(int(seconds * RATE)) / RATE
    phase = 2 * np.pi * np.cumsum(130 + 15 * np.sin(2 * np.pi * 0.8 * t)) / RATE
    y = sum(np.sin(k * phase) / k for k in range(1, 25)) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return (y / np.abs(y).max() * level).astype(np.int16)


def cat(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts).astype(np.int16)


def room(seconds: float, *speech: tuple[float, np.ndarray]) -> np.ndarray:
    """A continuous noise floor with speech added at the given offsets, the
    way a microphone hears a room. Concatenating a clip between two stretches
    of floor instead splices in the clip's own fade to digital silence, which
    no microphone produces and which holds webrtcvad on "speech" for 3.5 s
    (see Endpointer)."""
    y = floor(seconds).astype(np.int32)
    for at, x in speech:
        y[int(at * RATE):int(at * RATE) + len(x)] += x
    return np.clip(y, -32768, 32767).astype(np.int16)


def chunks(x: np.ndarray, n: int):
    for off in range(0, len(x), n):
        yield x[off:off + n]


def feed_all(ww: WakeWords, x: np.ndarray, n: int = 320) -> list[Detection]:
    """In the node's own 20 ms frames unless told otherwise."""
    return [d for c in chunks(x, n) for d in ww.feed(c)]


def run(ep: Endpointer, x: np.ndarray, n: int = 320) -> float | None:
    """Seconds of audio fed when the endpointer said done, or None."""
    fed = 0
    for c in chunks(x, n):
        fed += len(c)
        if ep.feed(c):
            return fed / RATE
    return None


def seconds(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE


# ---- models, once per session ---------------------------------------------------


@pytest.fixture(scope="session")
def model_dir(tmp_path_factory) -> Path:
    pytest.importorskip("openwakeword", reason="openwakeword is not installed")
    d = Path(os.environ.get("NODES_TEST_WAKEWORD_DIR") or tmp_path_factory.mktemp("wakewords"))
    try:
        ensure_models(list(LOADED), d)
    except httpx.HTTPError as e:
        pytest.skip(f"openWakeWord models could not be fetched from GitHub (offline?): {e!r}")
    return d


@pytest.fixture
def ww(model_dir) -> WakeWords:
    return WakeWords(LOADED, model_dir)


def padded(name: str, before: float = 1.0, after: float = 1.0) -> np.ndarray:
    x = clip(name)
    return room(before + len(x) / RATE + after, (before, x))


# ---- wake words -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hey_jarvis_en_us.wav", "hey_jarvis_en_gb.wav"])
def test_hey_jarvis_fires_once_per_phrase_and_the_other_models_stay_quiet(ww, name):
    found = feed_all(ww, padded(name))
    assert [d.name for d in found] == ["hey_jarvis"]
    assert found[0].score >= LOADED["hey_jarvis"]
    # Inside the phrase, after "jarvis" has begun: not the silence around it.
    assert RATE * 1.3 < found[0].sample < RATE * 1.0 + len(clip(name))


def test_a_sentence_without_the_wake_word_fires_nothing(ww):
    assert feed_all(ww, padded("weather_en_us.wav")) == []


@pytest.mark.parametrize("signal", [
    pytest.param(lambda: floor(10.0), id="noise-floor"),
    pytest.param(lambda: np.zeros(10 * RATE, dtype=np.int16), id="digital-silence"),
    pytest.param(lambda: floor(5.0, dbfs=-20), id="loud-white-noise"),
    pytest.param(lambda: voiced(5.0), id="speech-like-hum"),
])
def test_silence_and_noise_fire_nothing(ww, signal):
    assert feed_all(ww, signal()) == []


@pytest.mark.parametrize("n", [1, 7, 320, 1280, 4000, 10 ** 9])
def test_chunk_length_does_not_change_the_detection(model_dir, n):
    x = padded("hey_jarvis_en_us.wav")
    reference = feed_all(WakeWords(LOADED, model_dir), x, 1280)
    assert feed_all(WakeWords(LOADED, model_dir), x, n) == reference


def test_detection_sample_marks_where_the_command_begins(ww):
    """The integration: whatever of the current chunk follows Detection.sample
    is the start of the command, and goes to the endpointer."""
    ep = None
    for c in chunks(padded("hey_jarvis_en_us.wav", after=3.0), 320):
        if ep is None:
            found = ww.feed(c)
            if found:
                ep = Endpointer()
                ep.feed(c[len(c) - (ww.position - found[0].sample):])
        elif ep.feed(c):
            break
    assert ep is not None and ep.reason == "silence" and ep.had_speech
    # "what time is it" without "hey jarvis": longer than a word, shorter than the clip.
    assert 0.8 < seconds(ep.audio) < len(clip("hey_jarvis_en_us.wav")) / RATE


def test_two_wake_words_further_apart_than_the_refractory_window_both_fire(ww):
    x = clip("hey_jarvis_en_us.wav")
    gap = len(x) / RATE + 1.0
    found = feed_all(ww, room(1.0 + 2 * gap, (1.0, x), (1.0 + gap, x)))
    assert [d.name for d in found] == ["hey_jarvis", "hey_jarvis"]
    assert found[1].sample - found[0].sample > 1.5 * RATE


def test_refractory_window_swallows_a_repeat_inside_it(model_dir):
    x = clip("hey_jarvis_en_us.wav")
    ww = WakeWords(LOADED, model_dir, refractory_s=10)
    both = room(2.0 + 2 * len(x) / RATE, (1.0, x), (1.0 + len(x) / RATE, x))
    assert [d.name for d in feed_all(ww, both)] == ["hey_jarvis"]


def test_a_score_that_stays_high_fires_once_even_with_a_short_refractory(model_dir):
    # hey_jarvis stays over 0.5 for 560 ms on this clip; with a 100 ms window
    # only the re-arm rule stands between that run and several detections.
    ww = WakeWords(LOADED, model_dir, refractory_s=0.1)
    assert [d.name for d in feed_all(ww, padded("hey_jarvis_en_us.wav"))] == ["hey_jarvis"]


def test_each_model_is_held_to_its_own_threshold(model_dir):
    # If thresholds leaked between models (the lowest applied to all, say),
    # hey_jarvis would fire here on alexa's.
    ww = WakeWords({"hey_jarvis": 1.0, "alexa": 0.05}, model_dir)
    assert [d for d in feed_all(ww, padded("hey_jarvis_en_us.wav")) if d.name == "hey_jarvis"] == []


def test_openwakeword_random_warm_up_never_fires(ww):
    """openWakeWord seeds its window with embeddings of random noise from
    numpy's global generator. At seed 231 that noise alone takes alexa to 0.7
    on a quiet room; about 1 reset in 100 does something like it."""
    x = floor(1.2)
    saved = np.random.get_state()
    try:
        np.random.seed(231)
        ww._model.reset()
        raw = max(float(ww._model.predict(x[off:off + wakeword.FRAME])[ww._keys["alexa"]])
                  for off in range(0, len(x) - wakeword.FRAME + 1, wakeword.FRAME))
        if raw < 0.5:
            pytest.skip(f"seed 231 no longer false-fires raw openWakeWord (alexa {raw:.3f}), "
                        "so this test proves nothing; find another seed or drop the warm-up")
        np.random.seed(231)
        ww.reset()
        assert feed_all(ww, x) == []
    finally:
        np.random.set_state(saved)


def test_reset_rearms_a_model_that_just_fired(model_dir):
    ww = WakeWords(LOADED, model_dir, refractory_s=60)
    x = padded("hey_jarvis_en_us.wav")
    first = feed_all(ww, x)
    assert feed_all(ww, x) == []            # still inside the refractory window
    ww.reset()
    assert ww.position == 0
    assert feed_all(ww, x) == first         # a fresh stream, the same answer


def test_a_clone_scores_exactly_what_a_fresh_instance_scores(model_dir):
    """clone() shares the ONNX sessions and nothing else. It leans on
    openwakeword 0.6.0's internals, so it is held to the one thing that
    matters: the same audio gives the same detections at the same samples."""
    audio = padded("hey_jarvis_en_us.wav")
    fresh = feed_all(WakeWords(LOADED, model_dir), audio)
    twin = feed_all(WakeWords(LOADED, model_dir).clone(), audio)
    assert [(d.name, d.sample) for d in twin] == [(d.name, d.sample) for d in fresh]
    assert [d.score for d in twin] == pytest.approx([d.score for d in fresh], abs=1e-6)


def test_two_clones_on_different_nodes_do_not_hear_each_other(model_dir):
    """Each node's audio buffer must be its own. openWakeWord reads the last
    1760 samples of it per 80 ms frame, so a buffer shared between two nodes
    mixes 30 ms of one into every frame of the other and moves every score
    without always stopping a detection; and a reset of one would clear the
    other's. So the talking node, fed alongside a node that hears only the
    room and is reset halfway, must score exactly what it scores alone."""
    phrase = padded("hey_jarvis_en_gb.wav")
    alone = feed_all(WakeWords(LOADED, model_dir), phrase)
    base = WakeWords(LOADED, model_dir)
    speaking, quiet = base.clone(), base.clone()
    heard, silent = [], []
    room_only = floor(len(phrase) / RATE, seed=5)
    for i, (a, b) in enumerate(zip(chunks(phrase, 320), chunks(room_only, 320))):
        if i == 75:  # 1.5 s in: the other node reconnects halfway through "jarvis"
            quiet.reset()
        heard += speaking.feed(a)
        silent += quiet.feed(b)
    assert silent == []
    assert [(d.name, d.sample) for d in heard] == [(d.name, d.sample) for d in alone]
    assert [d.score for d in heard] == pytest.approx([d.score for d in alone], abs=1e-6)


def test_a_custom_model_in_the_volume_loads_by_its_file_name(model_dir, tmp_path):
    for f in wakeword.FEATURES:
        shutil.copy(model_dir / f, tmp_path / f)
    shutil.copy(model_dir / wakeword.MODELS["hey_jarvis"][0], tmp_path / "my_word.onnx")
    ensure_models(["my_word"], tmp_path)    # present, so nothing to fetch and no error
    found = feed_all(WakeWords({"my_word": 0.5}, tmp_path), padded("hey_jarvis_en_us.wav"))
    assert [d.name for d in found] == ["my_word"]


def test_audio_that_is_not_mono_int16_is_refused_not_misheard(ww):
    x = clip("hey_jarvis_en_us.wav")
    for bad in (x.astype(np.float32) / 32768, np.stack([x, x], axis=1), x.tobytes()):
        with pytest.raises(TypeError):
            ww.feed(bad)


def test_available_names_the_wake_words_and_not_the_timer_intents():
    names = WakeWords.available()
    assert {"hey_jarvis", "alexa", "hey_mycroft"} <= set(names)
    assert "timer" not in names


def test_the_pinned_files_are_the_ones_openwakeword_names():
    """Guards a version bump: if openwakeword renames or re-versions a model,
    this fails here rather than as a hash mismatch on someone's first boot."""
    oww = pytest.importorskip("openwakeword")
    for name, (file, _) in wakeword.MODELS.items():
        url = oww.MODELS[name]["download_url"].replace(".tflite", ".onnx")
        assert url == wakeword.RELEASE + file
    features = {m["download_url"].replace(".tflite", ".onnx") for m in oww.FEATURE_MODELS.values()}
    assert features == {wakeword.RELEASE + f for f in wakeword.FEATURES}


def test_missing_model_files_say_to_run_ensure_models(tmp_path):
    with pytest.raises(FileNotFoundError, match="ensure_models"):
        WakeWords({"hey_jarvis": 0.5}, tmp_path)


@pytest.mark.parametrize("models", [{}, {"hey_jarvis": 0}, {"hey_jarvis": 1.5}])
def test_an_empty_set_or_a_threshold_outside_0_to_1_is_refused(tmp_path, models):
    with pytest.raises(ValueError):
        WakeWords(models, tmp_path)


# ---- ensure_models, with no network ----------------------------------------------


@pytest.fixture
def fake_release(monkeypatch):
    """Stand-in release assets with their own pinned hashes; records every fetch."""
    content = {"melspectrogram.onnx": b"mel", "embedding_model.onnx": b"emb",
               "hey_jarvis_v0.1.onnx": b"jarvis"}
    sha = {f: hashlib.sha256(b).hexdigest() for f, b in content.items()}
    monkeypatch.setattr(wakeword, "FEATURES", {f: sha[f] for f in ("melspectrogram.onnx",
                                                                    "embedding_model.onnx")})
    monkeypatch.setattr(wakeword, "MODELS", {"hey_jarvis": ("hey_jarvis_v0.1.onnx",
                                                            sha["hey_jarvis_v0.1.onnx"])})
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return content[url.rsplit("/", 1)[-1]]

    monkeypatch.setattr(wakeword, "_fetch", fetch)
    return fetched


def test_ensure_models_fetches_only_what_is_missing(tmp_path, fake_release):
    target = tmp_path / "volume" / "wakewords"
    target.mkdir(parents=True)
    (target / "melspectrogram.onnx").write_bytes(b"mel")
    ensure_models(["hey_jarvis"], target)
    assert sorted(fake_release) == [wakeword.RELEASE + "embedding_model.onnx",
                                    wakeword.RELEASE + "hey_jarvis_v0.1.onnx"]
    assert (target / "hey_jarvis_v0.1.onnx").read_bytes() == b"jarvis"


def test_ensure_models_never_fetches_twice(tmp_path, fake_release):
    ensure_models(["hey_jarvis"], tmp_path)
    fake_release.clear()
    ensure_models(["hey_jarvis"], tmp_path)
    assert fake_release == []


def test_a_download_with_the_wrong_hash_leaves_no_model_behind(tmp_path, fake_release, monkeypatch):
    monkeypatch.setattr(wakeword, "_fetch", lambda url: b"<html>rate limited</html>")
    with pytest.raises(RuntimeError, match="SHA-256"):
        ensure_models(["hey_jarvis"], tmp_path)
    # Otherwise the next start-up would find the file and trust it.
    assert list(tmp_path.iterdir()) == []


def test_a_seed_directory_fills_the_volume_without_touching_the_network(tmp_path, fake_release):
    """The image carries the default wake word so that a first start needs no
    network: ensure_models copies it onto the volume instead of fetching it."""
    seed = tmp_path / "image"
    seed.mkdir()
    for file, data in (("melspectrogram.onnx", b"mel"), ("embedding_model.onnx", b"emb"),
                       ("hey_jarvis_v0.1.onnx", b"jarvis")):
        (seed / file).write_bytes(data)
    ensure_models(["hey_jarvis"], tmp_path / "volume", seeds=[seed])
    assert fake_release == []
    assert (tmp_path / "volume" / "hey_jarvis_v0.1.onnx").read_bytes() == b"jarvis"


def test_a_seed_file_with_another_hash_is_fetched_rather_than_trusted(tmp_path, fake_release):
    """An image older than the pinned table must not supply a model the table
    no longer names."""
    seed = tmp_path / "image"
    seed.mkdir()
    (seed / "hey_jarvis_v0.1.onnx").write_bytes(b"an older jarvis")
    ensure_models(["hey_jarvis"], tmp_path / "volume", seeds=[seed])
    assert wakeword.RELEASE + "hey_jarvis_v0.1.onnx" in fake_release
    assert (tmp_path / "volume" / "hey_jarvis_v0.1.onnx").read_bytes() == b"jarvis"


@pytest.mark.parametrize("name", ["hey_siri", "../../etc/passwd", "hey.jarvis", ""])
def test_unknown_or_path_like_model_names_are_refused(tmp_path, fake_release, name):
    with pytest.raises(ValueError):
        ensure_models([name], tmp_path)
    assert fake_release == []


# ---- the endpointer ------------------------------------------------------------------


def test_command_ends_at_the_silence_after_it():
    ep = Endpointer()
    done = run(ep, cat(floor(0.5), voiced(1.5), floor(3.0, seed=1)))
    assert ep.reason == "silence" and ep.had_speech
    # Speech ends at 2.0 s; webrtcvad holds its decision ~80 ms; then 800 ms.
    assert 2.8 <= done <= 3.0
    # 300 ms before and after the speech, not the whole 2.9 s fed.
    assert 2.0 <= seconds(ep.audio) <= 2.3


def test_a_pause_shorter_than_silence_ms_does_not_cut_the_command_off():
    ep = Endpointer()
    done = run(ep, cat(floor(0.5), voiced(1.0), floor(0.6, seed=1), voiced(1.0), floor(3.0, seed=2)))
    assert ep.reason == "silence"
    assert 3.9 <= done <= 4.1
    assert seconds(ep.audio) >= 2.6    # both bursts and the pause between them


def test_no_speech_gives_up_at_start_timeout_with_nothing_to_transcribe():
    ep = Endpointer()
    assert run(ep, floor(6.0)) == pytest.approx(4.0, abs=0.02)
    assert (ep.reason, ep.had_speech, ep.audio) == ("no_speech", False, b"")


def test_digital_silence_is_not_speech():
    ep = Endpointer(start_timeout_s=2.0)
    run(ep, np.zeros(3 * RATE, dtype=np.int16))
    assert (ep.reason, ep.had_speech) == ("no_speech", False)


@pytest.mark.parametrize("burst", [
    pytest.param(lambda: voiced(0.06), id="60ms-burst"),
    pytest.param(lambda: np.r_[20000, -20000, 15000, np.zeros(317)].astype(np.int16), id="click"),
])
def test_a_click_or_knock_does_not_start_a_command(burst):
    ep = Endpointer()
    run(ep, cat(floor(1.0), burst(), floor(4.0, seed=1)))
    assert (ep.reason, ep.had_speech) == ("no_speech", False)


def test_a_click_in_the_pause_does_not_hold_the_command_open():
    click = np.r_[20000, -20000, 15000, np.zeros(317)].astype(np.int16)
    plain, clicked = Endpointer(), Endpointer()
    t_plain = run(plain, cat(floor(0.5), voiced(1.0), floor(3.0, seed=1)))
    t_clicked = run(clicked, cat(floor(0.5), voiced(1.0), floor(0.4, seed=1), click, floor(3.0, seed=2)))
    assert clicked.reason == "silence"
    assert t_clicked == pytest.approx(t_plain, abs=0.021)


def test_speech_that_never_pauses_stops_at_max_seconds():
    ep = Endpointer(max_seconds=3.0)
    assert run(ep, voiced(12.0)) == pytest.approx(3.0, abs=0.02)
    assert ep.reason == "max_length" and ep.had_speech


def test_loud_steady_noise_is_bounded_by_max_seconds_not_endless():
    """The known limit: webrtcvad hears -20 dBFS white noise as speech. The
    command then ends at max_seconds; what must never happen is no end."""
    ep = Endpointer(max_seconds=5.0)
    assert run(ep, floor(12.0, dbfs=-20)) == pytest.approx(5.0, abs=0.02)
    assert ep.reason == "max_length"


def test_real_speech_ends_after_the_last_word():
    x = clip("hey_jarvis_en_gb.wav")
    ep = Endpointer()
    done = run(ep, padded("hey_jarvis_en_gb.wav", before=0.5, after=3.0))
    assert ep.reason == "silence" and ep.had_speech
    assert done < 0.5 + len(x) / RATE + 1.0
    assert seconds(ep.audio) >= 1.5


@pytest.mark.parametrize("n", [1, 13, 320, 1000, 10 ** 9])
def test_chunk_length_does_not_change_where_the_command_ends(n):
    x = cat(floor(0.5), voiced(1.2), floor(0.4, seed=1), voiced(0.5), floor(2.0, seed=2))
    reference = Endpointer()
    run(reference, x, 320)
    ep = Endpointer()
    run(ep, x, n)
    assert (ep.reason, ep.audio) == (reference.reason, reference.audio)


def test_feeding_after_the_end_changes_nothing():
    ep = Endpointer()
    run(ep, cat(floor(0.5), voiced(1.0), floor(2.0, seed=1)))
    audio = ep.audio
    assert ep.feed(voiced(1.0)) is True
    assert ep.audio == audio and ep.reason == "silence"


def test_endpointer_refuses_audio_that_is_not_mono_int16():
    with pytest.raises(TypeError):
        Endpointer().feed(voiced(0.1).astype(np.float32))


def test_a_rate_webrtcvad_cannot_take_is_refused_up_front():
    with pytest.raises(ValueError, match="44100"):
        Endpointer(rate=44100)


def test_onnx_runtime_is_told_not_to_report_to_microsoft_before_it_loads():
    """onnxruntime 1.30's Linux wheel posts usage to Microsoft and keeps a
    device id unless ORT_DISABLE_TELEMETRY is set when it initialises. Measured
    in the image: two connections to the collector without it, none with it.
    Importing this module is what sets it, so it must hold by now."""
    import os
    import sys
    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"
    source = Path(wakeword.__file__).read_text()
    assert source.index('os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")') < \
        source.index("from openwakeword.model import Model")
    assert "onnxruntime" not in sys.modules or os.environ["ORT_DISABLE_TELEMETRY"] == "1"
