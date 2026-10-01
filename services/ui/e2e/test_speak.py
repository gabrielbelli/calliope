"""The Speak tab in a browser, against the local stack: every control on it,
what each one puts on the wire, and what the page says back.

WHAT IS ASSERTED, AND WHERE. What arrived is read from the fakes' own log
(fake.requests), after the gateway and the page server have had their say,
and what left the page from the browser's (browser_log); what the page shows
is read from the page. Nothing is listened to. The browser is muted, so the
audio the page makes is checked by what it is -- a blob in the player, a
transport that moves, a file with a name -- and never by ear.

THE STACK AS THE SESSION STARTS IT. Kokoro's 48 voices come from the fake
tts-stack, and bm_george is the page's default. There is one cloned voice,
narrator, a 12 s clip. tts-long offers the three engines voice_common
catalogues:

    Chatterbox        the default; exaggeration, cfg_weight, temperature
    Chatterbox Turbo  English only; temperature only; clips over 5 s
    Voxtral           its own preset voices, each carrying its language

A cloned voice's option is `chatterbox:<name>` once /ui/health has named the
engines, which is why most tests wait for that before choosing one (ready()).

A clip a test makes is deleted again when the test ends, from the test's
side of the loopback: another device, as far as the page can tell.
"""

from __future__ import annotations

import math
import random
import re
import sys
import time
import wave
from array import array
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import expect
from test_routes import history_length, open_tab, seeded_job, settled

SENTENCE = "The quick brown fox jumps over the lazy dog, and then it does it again."
# About twenty seconds of speech at the fake's 15 characters a second, and
# forty at the slowest synthesis speed: a stream of some eighty deltas, ten
# seconds on the wire, which is long enough to press things while it runs.
LONG = " ".join(["It was a bright cold day in April, and the clocks were striking thirteen."] * 4)
CLONE_TEXT = "A short line for the queue, read in a cloned voice."
PORTUGUESE = "Olá, você pode me dizer onde fica a estação? Eu não sei para onde ir agora."
# English and Portuguese in one request, by the page's own two regular
# expressions (codeSwitchNote): "obrigado" for one, "really", "and" and
# "that" for the other.
MIXED = "I really want to say obrigado to everyone, and that is all for today."


# ---- media -----------------------------------------------------------------------------


def tone(path: Path, seconds: float, rate: int = 24000, channels: int = 1) -> Path:
    """A WAV the browser can decode: a voiced hum under a syllable-rate
    swell, so a level meter and a waveform have something to show."""
    frames = int(seconds * rate)
    one = array("h", (int(8000 * math.sin(2 * math.pi * 170 * i / rate)
                          * (0.55 + 0.45 * math.sin(2 * math.pi * 3.7 * i / rate)))
                      for i in range(frames)))
    data = one
    if channels == 2:
        data = array("h", bytes(4 * frames))
        data[0::2] = one
        data[1::2] = one
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data.tobytes())
    return path


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    """The clips the sheet is given, written once per session outside the
    repository. `take` is 44.1 kHz stereo on purpose: the page has to turn
    it into 24 kHz mono before anything is sent."""
    root = tmp_path_factory.mktemp("speak-media")
    junk = root / "junk.mp3"
    junk.write_bytes(bytes(random.Random(7).getrandbits(8) for _ in range(4096)))
    return {"take": tone(root / "Interview Take.wav", 12, rate=44100, channels=2),
            "long": tone(root / "long.wav", 40), "short": tone(root / "short.wav", 4),
            "tiny": tone(root / "tiny.wav", 3), "clip": tone(root / "clip.wav", 12), "junk": junk}


class Clips:
    """Cloned voices on the page server, as another device sees them."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.made: list[str] = []

    def add(self, name: str, path: Path) -> None:
        answer = httpx.post(f"{self.base}/ui/clips", data={"name": name},
                            files={"file": (path.name, path.read_bytes(), "audio/wav")}, timeout=30)
        answer.raise_for_status()
        self.made.append(name)

    def kept(self, name: str) -> None:
        """A voice the page will make, to be deleted when the test ends."""
        self.made.append(name)

    def listed(self) -> dict[str, dict[str, Any]]:
        voices = httpx.get(f"{self.base}/ui/clips", timeout=10).json()["voices"]
        return {voice["name"]: voice for voice in voices}


@pytest.fixture
def clips(stack):
    """add(name, path) puts a cloned voice on the server before the page
    asks; every name added or kept() is deleted when the test ends, so no
    test sees another's voices."""
    store = Clips(stack.url)
    yield store
    for name in store.made:
        httpx.delete(f"{stack.url}/ui/clips/{name}", timeout=10)


# ---- helpers ---------------------------------------------------------------------------


def ready(page) -> None:
    """Speak as a reader finds it once everything has answered: the router
    has resolved the address, and /ui/health is in, which is what names the
    engines and so the values of the cloned voices' options."""
    settled(page)
    page.wait_for_function("""() => engineIds().length > 0
      && [...document.getElementById("voice").options].some(o => o.value.startsWith("chatterbox:"))""")


def option(page, label: str) -> str:
    """The value of the picker's option that reads `label`."""
    value = page.evaluate("""label => {
      const found = [...document.getElementById("voice").options].find(o => o.textContent.trim() === label);
      return found ? found.value : null;
    }""", label)
    assert value, f"no voice called {label!r} in the picker"
    return value


def choose(page, label: str) -> str:
    value = option(page, label)
    page.locator("#voice").select_option(value)
    return value


def kokoro(page) -> list[str]:
    """The Kokoro voices the picker offers, in its order."""
    return page.evaluate("""() => [...document.getElementById("voice").options]
      .filter(o => o.value.startsWith("k:")).map(o => o.value.slice(2))""")


def voice_in_bar(page) -> str | None:
    return page.evaluate("new URLSearchParams(location.search).get('voice')")


def arrived(fake, since: int, backend: str, path: str, count: int = 1, method: str = "POST") -> list[dict]:
    """What reached a fake backend after `since`, waiting for `count` of it.
    A streamed answer is logged when it ends, which can be a moment after
    the page has drawn it."""
    ends = time.monotonic() + 10
    while True:
        got = fake.requests(backend=backend, method=method, path=path, since=since)
        if len(got) >= count or time.monotonic() > ends:
            return got
        time.sleep(0.1)


def eventually(read, timeout: float = 10.0):
    """read() once it returns something, for what the page sends without
    drawing anything (an abandon on the way out of the sheet)."""
    ends = time.monotonic() + timeout
    while True:
        got = read()
        if got or time.monotonic() > ends:
            return got
        time.sleep(0.1)


def nudge(page, selector: str, key: str, times: int) -> None:
    """Move a range with the keyboard, as a reader's arrow keys do: one step
    and one trusted input event per press."""
    page.locator(selector).focus()
    for _ in range(times):
        page.keyboard.press(key)


def open_expert(page, which: str = "fast") -> None:
    page.locator(f"#tts-expert-{which} > summary").click()
    expect(page.locator(f"#tts-expert-{which}")).to_have_attribute("open", "")


def stored_jobs(page) -> list[str]:
    return [job["id"] for job in page.evaluate("JSON.parse(localStorage.getItem('aiv.jobs') || '[]')")]


def bad_colour(page) -> str:
    """What var(--bad) computes to on this page, in the form getComputedStyle uses."""
    return page.evaluate("""() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--bad)";
      document.getElementById("tab-speak").append(probe);
      const colour = getComputedStyle(probe).color;
      probe.remove();
      return colour;
    }""")


# ---- Kokoro: the text, the language and the voice ------------------------------------------


def test_the_character_counter_counts_and_turns_red_past_4096(page, goto):
    goto("/ui/speak")
    ready(page)
    text, chars = page.locator("#text"), page.locator("#chars")
    expect(chars).to_have_text("0")
    text.fill("Hello there")
    expect(chars).to_have_text("11")
    text.fill("x" * 4096)
    expect(chars).to_have_text("4096")
    assert chars.evaluate("el => el.style.color") == ""
    text.fill("x" * 4097)
    expect(chars).to_have_text("4097")
    assert chars.evaluate("el => getComputedStyle(el).color") == bad_colour(page)


def test_typing_portuguese_labels_auto_detect_and_filters_the_voices(page, goto, fake):
    """The language control stays on Auto-detect and says what it heard; the
    picker offers the voices that can say it, and the code that is sent is
    the one it heard."""
    goto("/ui/speak")
    ready(page)
    auto = page.locator('#lang option[value="auto"]')
    expect(auto).to_have_text("Auto-detect")
    page.locator("#text").fill(PORTUGUESE)
    expect(auto).to_have_text("Auto-detect (Portuguese)")
    expect(page.locator("#lang")).to_have_value("auto")
    expect(page.locator("#voice")).to_have_value("k:pf_dora")
    assert kokoro(page) == ["pf_dora", "pm_alex", "pm_santa"]
    groups = page.evaluate("() => [...document.querySelectorAll('#voice optgroup')].map(g => g.label)")
    assert [g for g in groups if g.startswith("Kokoro")] == ["Kokoro, instant: Portuguese (Brazil)"]
    page.wait_for_function("() => new URLSearchParams(location.search).get('voice') === 'k:pf_dora'")
    open_expert(page)
    page.locator("#x-troute").select_option("speak")
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    [sent] = arrived(fake, mark, "tts", r"^/speak$")
    assert sent["json"]["voice"] == "pf_dora"
    assert sent["json"]["language"] == "pt-br"


def test_choosing_a_language_lists_its_voices_again(page, goto, browser_log):
    goto("/ui/speak")
    ready(page)
    asked = len(browser_log.sent("GET", r"^/ui/api/voices$"))
    page.locator("#lang").select_option("es")
    page.wait_for_function("""() => [...document.getElementById("voice").options]
      .filter(o => o.value.startsWith("k:")).length === 3""")
    assert kokoro(page) == ["ef_dora", "em_alex", "em_santa"]
    expect(page.locator("#voice")).to_have_value("k:ef_dora")
    assert len(browser_log.sent("GET", r"^/ui/api/voices$")) == asked + 1
    # Every language again: the voice chosen for Spanish is kept, not reset.
    page.locator("#lang").select_option("auto")
    page.wait_for_function("""() => [...document.getElementById("voice").options]
      .filter(o => o.value.startsWith("k:")).length === 48""")
    expect(page.locator("#voice")).to_have_value("k:ef_dora")


# ---- Kokoro: generating -------------------------------------------------------------------


def test_generate_on_the_speak_route_sends_voice_language_speed_format_and_segments(page, goto, fake):
    """/speak takes the language and the text cut into sentences, and its
    segment offsets are what the karaoke copy of the text follows."""
    goto("/ui/speak")
    ready(page)
    open_expert(page)
    page.locator("#x-troute").select_option("speak")
    page.locator("#x-tfmt").select_option("flac")
    page.locator("#lang").select_option("pt")
    expect(page.locator("#voice")).to_have_value("k:pf_dora")
    nudge(page, "#speed", "ArrowRight", 2)
    expect(page.locator("#speedout")).to_have_text("1.10")
    page.locator("#text").fill("Bom dia a todos. Hoje vamos falar de vozes.")
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    [sent] = arrived(fake, mark, "tts", r"^/speak$")
    assert sent["json"] == {"voice": "pf_dora", "language": "pt-br", "speed": 1.1, "format": "flac",
                            "segments": [{"text": "Bom dia a todos."}, {"text": "Hoje vamos falar de vozes."}]}
    expect(page.locator("#speaktext")).to_be_visible()
    expect(page.locator("#speaktext")).to_contain_text("Hoje vamos falar de vozes.")
    expect(page.locator("#speak-meta")).to_have_text("2.8× realtime")


def test_generate_and_listen_streams_short_text_over_sse_with_a_chunk_plan(page, goto, fake):
    """Short Kokoro text defaults to the stream: /v1 with stream_format sse,
    raw pcm whatever format is picked, and X-Chunk-Plan to ask for the
    careful first chunk. What arrived becomes a WAV in the player."""
    goto("/ui/speak")
    ready(page)
    page.locator("#text").fill(SENTENCE)
    expect(page.locator("#x-troute")).to_have_value("v1")
    expect(page.locator("#x-stream")).to_have_value("sse")
    expect(page.locator("#x-tfmt")).to_be_disabled()
    mark = fake.last_seq()
    page.locator("#go-tts").click()
    expect(page.locator("#speak-transport")).to_be_visible()
    expect(page.locator("#player")).to_be_visible(timeout=20_000)
    [sent] = arrived(fake, mark, "tts", r"^/v1/audio/speech$")
    assert sent["json"] == {"model": "kokoro", "input": SENTENCE, "voice": "bm_george",
                            "response_format": "pcm", "speed": 1, "stream_format": "sse"}
    assert sent["headers"].get("x-chunk-plan") == "1"
    assert page.locator("#player").evaluate("p => p.src.startsWith('blob:')")
    expect(page.locator("#playrow")).to_be_visible()
    expect(page.locator("#speak-meta")).to_have_text(re.compile(r"^\d+(\.\d)?× on the wire$"))
    with page.expect_download() as download:
        page.locator("#dl-audio").click()
    assert download.value.suggested_filename == "speech.wav"


def test_the_stream_transport_pauses_scrubs_and_stops(page, goto):
    goto("/ui/speak")
    ready(page)
    page.locator("#speed").focus()
    page.keyboard.press("Home")
    expect(page.locator("#speedout")).to_have_text("0.50")
    page.locator("#text").fill(LONG)
    page.locator("#go-tts").click()
    play, at, said = page.locator("#speak-play"), page.locator("#speak-pos"), page.locator("#speak-note")
    # It starts by itself once enough is in hand, and says so.
    expect(play).to_have_text("Pause")
    expect(said).to_have_text("Playing as it is made.")
    expect(at).not_to_have_text(re.compile(r"^0:00 /"))
    play.click()
    expect(play).to_have_text("Play")
    expect(said).to_have_text("")
    # Back to the start with the keyboard, which is a scrub that commits.
    page.locator("#speak-seek").focus()
    page.keyboard.press("Home")
    expect(at).to_have_text(re.compile(r"^0:00 / "))
    expect(play).to_have_text("Play")
    play.click()
    expect(play).to_have_text("Pause")
    expect(at).to_have_text(re.compile(r"^0:0[1-9] / "))
    page.locator("#speak-stop").click()
    expect(page.locator("#speak-transport")).to_be_hidden()
    expect(said).to_have_text("")
    # Stopping the sound is not stopping the request: the file still arrives.
    expect(page.locator("#player")).to_be_visible(timeout=20_000)
    expect(page.locator("#speak-transport")).to_be_hidden()


def test_pressing_play_on_the_finished_file_silences_the_stream(page, goto):
    goto("/ui/speak")
    ready(page)
    page.locator("#speed").focus()
    page.keyboard.press("Home")
    page.locator("#text").fill(LONG)
    page.locator("#go-tts").click()
    expect(page.locator("#speak-play")).to_have_text("Pause")
    expect(page.locator("#player")).to_be_visible(timeout=20_000)
    # Forty seconds made in ten: the stream is still playing what it made.
    expect(page.locator("#speak-transport")).to_be_visible()
    page.locator("#player").evaluate("p => p.play()")
    expect(page.locator("#speak-transport")).to_be_hidden()
    expect(page.locator("#speak-note")).to_have_text("")


def test_the_result_reports_realtime_ignored_parameters_and_a_clamped_speed(page, goto, fake):
    """The fake names no deviations, because the page never sends a field
    tts-stack would ignore or a speed it would clamp; the two headers are
    added to its answer here, as the real service writes them, to see that
    the page reports what it is told."""
    def deviations(route) -> None:
        answer = route.fetch()
        route.fulfill(response=answer, headers={**answer.headers, "x-ignored-parameters": "instructions, model",
                                                "x-speed-clamped": "4 to 2"})

    page.route("**/ui/api/v1/audio/speech", deviations)
    goto("/ui/speak")
    ready(page)
    page.locator("#text").fill(SENTENCE)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#speak-meta")).to_have_text(
        "2.8× realtime · ignored: instructions, model · the server clamped speed")
    [sent] = arrived(fake, mark, "tts", r"^/v1/audio/speech$")
    # Generate wants the file, so nothing about it is a stream.
    assert sent["json"] == {"model": "kokoro", "input": SENTENCE, "voice": "bm_george",
                            "response_format": "mp3", "speed": 1}
    assert "x-chunk-plan" not in sent["headers"]


def test_download_writes_the_audio_in_the_chosen_format(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    open_expert(page)
    page.locator("#x-troute").select_option("speak")
    page.locator("#x-tfmt").select_option("opus")
    page.locator("#text").fill(SENTENCE)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    [sent] = arrived(fake, mark, "tts", r"^/speak$")
    assert sent["json"]["format"] == "opus"
    with page.expect_download() as download:
        page.locator("#dl-audio").click()
    assert download.value.suggested_filename == "speech.opus"
    # The bytes the service answered, untouched (the fake's are a WAV).
    assert Path(download.value.path()).read_bytes()[:4] == b"RIFF"


def test_synthesis_speed_is_sent_and_its_readout_follows_the_slider(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    nudge(page, "#speed", "ArrowLeft", 4)
    expect(page.locator("#speedout")).to_have_text("0.80")
    page.locator("#text").fill(SENTENCE)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    [sent] = arrived(fake, mark, "tts", r"^/v1/audio/speech$")
    assert sent["json"]["speed"] == 0.8
    # A cloned voice has no such field: the slider is greyed back to 1.
    choose(page, "default (no clip needed)")
    expect(page.locator("#speed")).to_be_disabled()
    expect(page.locator("#speed")).to_have_value("1")
    expect(page.locator("#speedout")).to_have_text("1.00")
    page.locator("#voice").select_option("k:af_heart")
    expect(page.locator("#speed")).to_be_enabled()


def test_the_v1_route_greys_the_segments_editor_and_the_language(page, goto):
    goto("/ui/speak")
    ready(page)
    open_expert(page)
    # Nothing typed yet, so nothing rules segments out: /speak is the default.
    expect(page.locator("#x-troute")).to_have_value("speak")
    expect(page.locator("#x-stream")).to_be_disabled()
    page.locator("#addseg").click()
    segment = ["#segments .seg-text", "#segments .seg-pause", "#segments .seg-voice", "#segments .seg-del"]
    page.locator("#x-troute").select_option("v1")
    for selector in ["#addseg", "#lang", *segment]:
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator("#x-stream")).to_be_enabled()
    page.locator("#x-stream").select_option("sse")
    page.locator("#text").fill(SENTENCE)
    # A stream short enough to play as it is made is pcm, whatever is picked.
    expect(page.locator("#x-tfmt")).to_be_disabled()
    page.locator("#x-stream").select_option("audio")
    expect(page.locator("#x-tfmt")).to_be_enabled()
    page.locator("#x-troute").select_option("speak")
    for selector in ["#addseg", "#lang", *segment]:
        expect(page.locator(selector)).to_be_enabled()
    expect(page.locator("#x-stream")).to_be_disabled()


def test_an_added_segment_is_sent_with_its_pause_and_voice_and_can_be_removed(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    open_expert(page)
    page.locator("#addseg").click()
    page.locator("#addseg").click()
    rows = page.locator("#segments > .row")
    expect(rows).to_have_count(2)
    rows.nth(0).locator(".seg-text").fill("First part.")
    rows.nth(0).locator(".seg-pause").fill("1.5")
    rows.nth(0).locator(".seg-voice").select_option("pf_dora")
    rows.nth(1).locator(".seg-text").fill("Second part.")
    # The text box is empty and the segments are enough.
    expect(page.locator("#go-tts-quiet")).to_be_enabled()
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    [sent] = arrived(fake, mark, "tts", r"^/speak$")
    assert sent["json"]["segments"] == [{"text": "First part.", "pause_after": 1.5, "voice": "pf_dora"},
                                        {"text": "Second part."}]
    assert "text" not in sent["json"]
    rows.nth(0).locator(".seg-del").click()
    expect(rows).to_have_count(1)
    expect(rows.nth(0).locator(".seg-text")).to_have_value("Second part.")
    rows.nth(0).locator(".seg-del").click()
    expect(rows).to_have_count(0)
    expect(page.locator("#go-tts-quiet")).to_be_disabled()
    expect(page.locator("#go-tts")).to_be_disabled()


def test_mixed_english_and_portuguese_offers_the_segments_editor_once(page, goto):
    """The note points at the segments editor, so it is offered only where
    that editor can be used (/speak), and once dismissed it stays dismissed,
    across a reload too."""
    goto("/ui/speak")
    ready(page)
    note = page.locator("#codeswitch")
    page.locator("#text").fill(MIXED)
    expect(page.locator("#x-troute")).to_have_value("v1")
    expect(note).to_be_empty()
    open_expert(page)
    page.locator("#x-troute").select_option("speak")
    expect(note).to_have_text(
        "One request sends one language. Use the segments editor to change voice mid-text. Got it")
    page.locator("#dismisscs").click()
    expect(note).to_be_empty()
    page.locator("#text").fill(MIXED + " Obrigado again, and thanks.")
    expect(note).to_be_empty()
    # The address names the open panel, so a reload opens it again.
    page.reload()
    ready(page)
    expect(page.locator("#tts-expert-fast")).to_have_attribute("open", "")
    page.locator("#x-troute").select_option("speak")
    page.locator("#text").fill(MIXED)
    expect(note).to_be_empty()


def test_generate_is_disabled_with_empty_text(page, goto):
    goto("/ui/speak")
    ready(page)
    quiet, listen = page.locator("#go-tts-quiet"), page.locator("#go-tts")
    for state in ("", "   \n  "):
        page.locator("#text").fill(state)
        expect(quiet).to_be_disabled()
        expect(listen).to_be_disabled()
    page.locator("#text").fill("Hello.")
    expect(quiet).to_be_enabled()
    expect(listen).to_be_enabled()


def test_generate_is_disabled_on_the_clone_sheet(page, goto):
    goto("/ui/speak")
    ready(page)
    page.locator("#text").fill("Hello.")
    page.locator("#voice").select_option("new")
    expect(page.locator("#clone")).to_be_visible()
    expect(page.locator("#go-tts-quiet")).to_be_disabled(timeout=3000)
    expect(page.locator("#go-tts")).to_be_disabled(timeout=3000)


# ---- Kokoro: what goes wrong, and the wait ------------------------------------------------


def test_a_speech_request_that_fails_says_why(page, goto, fake, browser_log):
    browser_log.allow(500, r"/v1/audio/speech$")
    browser_log.allow(503, r"/v1/audio/speech$")
    goto("/ui/speak")
    ready(page)
    fake.fail(r"^/v1/audio/speech$", status=500, backend="tts", times=1, json_body={"error": {
        "message": "synthesis failed: espeak-ng is not installed", "type": "server_error", "param": None,
        "code": "synthesis_failed"}})
    page.locator("#text").fill(SENTENCE)
    page.locator("#go-tts-quiet").click()
    bad = page.locator("#speak-note .note.bad")
    expect(bad).to_have_text("synthesis failed: espeak-ng is not installed")
    expect(page.locator("#go-tts-quiet")).to_be_enabled()
    expect(page.locator("#speak-progress")).to_be_hidden()
    expect(page.locator("#player")).to_be_hidden()
    # The answer to the last press stands while the text is edited.
    page.locator("#text").fill(SENTENCE + " Again.")
    expect(bad).to_have_text("synthesis failed: espeak-ng is not installed")
    # A failure with no reason of its own is said in words, not as a status.
    fake.fail(r"^/v1/audio/speech$", status=503, backend="tts", times=1, json_body={})
    page.locator("#go-tts-quiet").click()
    expect(bad).to_have_text("The service is starting up or busy. Try again in a moment.")
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    expect(page.locator("#speak-note")).to_have_text("")


def test_a_stream_that_breaks_keeps_what_arrived_and_points_to_jobs(page, goto, fake, stack):
    """tts-stack ends a stream it cannot finish with an in-band error frame
    (the 200 has gone by then); tts-long's streams carry X-Job-Id, which is
    what the rest of the run is found by."""
    job = seeded_job(stack, "speech", "done")
    goto("/ui/speak")
    ready(page)
    fake.fail(r"^/v1/audio/speech$", status=None, backend="tts", times=1, cut_after=4,
              headers={"X-Job-Id": job})
    page.locator("#text").fill(LONG)
    page.locator("#go-tts").click()
    expect(page.locator("#speak-note .note.warn")).to_have_text(
        "The stream ended early. 2 s were kept. The rest is on the Jobs tab.")
    expect(page.locator("#player")).to_be_visible()
    assert job in stored_jobs(page)
    expect(page.locator("#go-tts")).to_be_enabled()
    # Nothing in hand is nothing to keep, and the reason is the service's own.
    fake.fail(r"^/v1/audio/speech$", status=None, backend="tts", times=1, cut_after=0)
    page.locator("#go-tts").click()
    expect(page.locator("#speak-note .note.bad")).to_have_text(
        "synthesis failed: the fake was told to stop here")


def test_a_generation_shows_its_estimate_while_it_waits(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    fake.fail(r"^/v1/audio/speech$", status=None, backend="tts", times=1, delay=2.5)
    page.locator("#text").fill(SENTENCE)
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#speak-progress")).to_be_visible()
    # Nothing is playing yet, so there is nothing to stop.
    expect(page.locator("#speak-stop")).to_be_hidden()
    expect(page.locator("#speak-lead")).to_have_text(
        re.compile(r"^\d+s in, (a few seconds left · estimate|past the \d+s estimate)$"))
    expect(page.locator("#go-tts-quiet")).to_be_disabled()
    expect(page.locator("#go-tts")).to_be_disabled()
    expect(page.locator("#player")).to_be_visible()
    expect(page.locator("#speak-progress")).to_be_hidden()
    expect(page.locator("#go-tts-quiet")).to_be_enabled()


def test_an_answer_that_comes_back_as_a_job_is_pointed_at_and_not_played(page, goto, fake, stack):
    job = seeded_job(stack, "speech", "done")
    goto("/ui/speak")
    ready(page)
    fake.fail(r"^/v1/audio/speech$", status=202, backend="tts", times=1,
              json_body={"id": job, "status": "queued"})
    page.locator("#text").fill(SENTENCE)
    page.locator("#go-tts-quiet").click()
    bad = page.locator("#speak-note .note.bad")
    expect(bad).to_have_text("That came back as a job. Open the Jobs tab.")
    expect(page.locator("#player")).to_be_hidden()
    assert job in stored_jobs(page)
    # JSON that is neither a job nor an error is not written into a file.
    fake.fail(r"^/v1/audio/speech$", status=200, backend="tts", times=1, json_body={"ok": True})
    page.locator("#go-tts-quiet").click()
    expect(bad).to_have_text("The speech service answered with something this page cannot play.")
    expect(page.locator("#player")).to_be_hidden()


def test_a_voice_list_that_cannot_be_read_says_so_and_still_offers_the_cloned_voices(page, goto, fake,
                                                                                    browser_log):
    browser_log.allow(500, r"/voices$")
    fake.fail(r"^/voices$", status=500, backend="tts")
    goto("/ui/speak")
    settled(page)
    bad = page.locator("#speak-note .note.bad")
    expect(bad).to_have_text("Could not list voices: injected 500")
    assert kokoro(page) == []
    assert option(page, "narrator")
    # It stands through every redraw a keystroke makes, until a read succeeds.
    page.locator("#text").fill(SENTENCE)
    expect(bad).to_have_text("Could not list voices: injected 500")


# ---- cloning: the sheet and its three sources ---------------------------------------------


def test_choosing_clone_a_new_voice_opens_the_sheet_and_focuses_the_name(page, goto):
    goto("/ui/speak")
    ready(page)
    page.locator("#voice").select_option("new")
    expect(page.locator("#clone")).to_be_visible()
    expect(page.locator("#clipname")).to_be_focused()
    page.wait_for_function("() => location.pathname === '/ui/speak/clone' && !location.search")
    expect(page).to_have_title("Use my own voice · Speak · Calliope")
    expect(page.locator("#saveclip")).to_be_disabled()


def test_recording_fifteen_seconds_prepares_a_reference_clip(page, goto):
    """The countdown runs on the page's clock, moved here a tenth at a time
    so the recorder's stop lands between two ticks as it does in real time;
    the microphone is Chromium's synthetic one."""
    page.clock.install()
    goto("/ui/speak/clone")
    settled(page)
    record, ring = page.locator("#rec"), page.locator("#ring")
    record.click()
    expect(record).to_have_text("Stop")
    # Time runs as it does until here, so the recorder has a second of sound.
    expect(ring).to_have_text("14")
    for _ in range(200):
        if ring.inner_text() == "0":
            break
        page.clock.run_for(100)
    expect(ring).to_have_text("0")
    expect(record).to_have_text("Record 15 s")
    expect(page.locator("#clipnote")).to_have_text(re.compile(r"^\d+\.\d s ready\.( Under 10 s is thin\.)?$"))
    expect(page.locator("#saveclip")).to_be_enabled()
    expect(page.locator("#clippreview")).to_be_visible()
    expect(page.locator("#clipname")).to_have_value("my-voice")


def test_uploading_a_clip_converts_it_and_offers_save(page, goto, media):
    goto("/ui/speak/clone")
    settled(page)
    with page.expect_file_chooser() as chooser:
        page.locator("#cliporfile").click()
    chooser.value.set_files(media["take"])
    expect(page.locator("#clipnote .note.ok")).to_have_text("12.0 s ready.")
    expect(page.locator("#clipname")).to_have_value("interview-take")
    expect(page.locator("#saveclip")).to_be_enabled()
    preview = page.locator("#clippreview")
    expect(preview).to_be_visible()
    page.wait_for_function("() => document.getElementById('clippreview').readyState >= 1")
    assert preview.evaluate("a => a.duration") == pytest.approx(12.0, abs=0.05)


def test_a_clip_over_thirty_seconds_is_trimmed_and_says_so(page, goto, media):
    goto("/ui/speak/clone")
    settled(page)
    page.locator("#clipfile").set_input_files(media["long"])
    expect(page.locator("#clipnote .note.ok")).to_have_text("30.0 s ready. Trimmed to 30 s.")


def test_a_short_clip_is_accepted_with_a_thin_warning(page, goto, media):
    goto("/ui/speak/clone")
    settled(page)
    page.locator("#clipfile").set_input_files(media["short"])
    expect(page.locator("#clipnote .note.warn")).to_have_text("4.0 s ready. Under 10 s is thin.")
    expect(page.locator("#saveclip")).to_be_enabled()


def test_a_clip_this_browser_cannot_decode_is_offered_as_it_is(page, goto, media):
    goto("/ui/speak/clone")
    settled(page)
    page.locator("#clipfile").set_input_files(media["junk"])
    expect(page.locator("#clipnote .note.ok")).to_have_text(
        "Sending the original file. This browser could not decode it.")
    expect(page.locator("#clipname")).to_have_value("junk")
    expect(page.locator("#saveclip")).to_be_enabled()


def test_saving_a_clip_posts_it_and_lists_the_new_voice(page, goto, media, clips, browser_log):
    goto("/ui/speak/clone")
    ready(page)
    page.locator("#clipfile").set_input_files(media["take"])
    expect(page.locator("#clipnote")).to_have_text("12.0 s ready.")
    page.locator("#clipname").fill("e2e-saved")
    clips.kept("e2e-saved")
    page.locator("#saveclip").click()
    expect(page.locator("#speak-note")).to_have_text("e2e-saved is ready.")
    expect(page.locator("#clone")).to_be_hidden()
    expect(page.locator("#clipnote")).to_be_empty()
    assert option(page, "e2e-saved").endswith(":e2e-saved")
    assert len(browser_log.sent("POST", r"^/ui/clips$")) == 1
    # What the server kept is the page's 24 kHz mono WAV, not the 44.1 kHz stereo file.
    saved = clips.listed()["e2e-saved"]
    assert saved["seconds"] == pytest.approx(12.0, abs=0.05)
    assert saved["bytes"] == pytest.approx(44 + 12 * 24000 * 2, abs=64)


def test_saving_a_clip_selects_the_new_voice(page, goto, media, clips):
    goto("/ui/speak/clone")
    ready(page)
    page.locator("#clipfile").set_input_files(media["take"])
    expect(page.locator("#clipnote")).to_have_text("12.0 s ready.")
    page.locator("#clipname").fill("e2e-chosen")
    clips.kept("e2e-chosen")
    page.locator("#saveclip").click()
    expect(page.locator("#speak-note")).to_have_text("e2e-chosen is ready.")
    expect(page.locator("#voice")).to_have_value(option(page, "e2e-chosen"), timeout=3000)


def test_save_without_a_name_asks_for_one(page, goto, media, browser_log):
    goto("/ui/speak/clone")
    settled(page)
    page.locator("#clipfile").set_input_files(media["take"])
    expect(page.locator("#clipnote")).to_have_text("12.0 s ready.")
    page.locator("#clipname").fill("")
    page.locator("#saveclip").click()
    expect(page.locator("#clipnote .note.warn")).to_have_text("Give the voice a name first.")
    expect(page.locator("#saveclip")).to_be_enabled()
    assert not browser_log.sent("POST", r"^/ui/clips$")


def test_a_name_the_server_refuses_is_said_and_save_stays_on(page, goto, media):
    goto("/ui/speak/clone")
    settled(page)
    page.locator("#clipfile").set_input_files(media["take"])
    expect(page.locator("#clipnote")).to_have_text("12.0 s ready.")
    bad = page.locator("#clipnote .note.bad")
    page.locator("#clipname").fill("default")
    page.locator("#saveclip").click()
    expect(bad).to_have_text("'default' is the name of Chatterbox's own built-in speaker; pick another")
    expect(page.locator("#saveclip")).to_be_enabled()
    page.locator("#clipname").fill("narrator")
    page.locator("#saveclip").click()
    expect(bad).to_have_text("a voice called 'narrator' already exists")


def resolve(page, url: str) -> None:
    page.locator("#cliplink").fill(url)
    page.locator("#clipresolve").click()


def test_a_clip_from_a_link_fetches_only_the_chosen_window_and_saves_it(page, goto, fake, clips, browser_log):
    """yt-dlp trims at the source, so the window asked for is what MeTube is
    told to fetch: start 5 s, take 12 s, end at 17 s."""
    link = "https://example.com/podcast-episode"
    goto("/ui/speak/clone")
    ready(page)
    resolve(page, link)
    hint = page.locator("#cliplinkhint")
    expect(hint).to_have_text("Probed talk podcast-episode · 3m 00s")
    expect(page.locator("#cliprange")).to_be_visible()
    expect(page.locator("#clipname")).to_have_value("probed-talk-podcast-episode")
    expect(page.locator("#cliplen")).to_have_attribute("max", "30")
    assert browser_log.sent("POST", r"^/ui/resolve$")[-1]["json"] == {"url": link}
    page.locator("#clipname").fill("e2e-link")
    clips.kept("e2e-link")
    page.locator("#clipstart").fill("5")
    page.locator("#cliplen").fill("12")
    mark = fake.last_seq()
    page.locator("#clipimport").click()
    expect(hint).to_have_text("Saved as e2e-link.", timeout=20_000)
    assert browser_log.sent("POST", r"^/ui/commit$")[-1]["json"] == {
        "token": link, "for_clip": True, "clip_start": 5, "clip_end": 17}
    added = arrived(fake, mark, "metube", r"^/add$")[-1]["json"]
    assert (added["url"], added["clip_start"], added["clip_end"]) == (link, 5, 17)
    assert browser_log.sent("POST", r"^/ui/clips/from-link$")[-1]["json"] == {
        "token": link, "name": "e2e-link", "replace": True}
    expect(page.locator("#cliprange")).to_be_hidden()
    expect(page.locator("#cliplink")).to_have_value("")
    expect(page.locator("#clone")).to_be_hidden()
    assert option(page, "e2e-link")
    assert "e2e-link" in clips.listed()


def test_a_clip_from_a_link_selects_the_new_voice(page, goto, clips):
    goto("/ui/speak/clone")
    ready(page)
    resolve(page, "https://example.net/second-interview")
    expect(page.locator("#cliprange")).to_be_visible()
    page.locator("#clipname").fill("e2e-linked")
    clips.kept("e2e-linked")
    page.locator("#clipimport").click()
    expect(page.locator("#cliplinkhint")).to_have_text("Saved as e2e-linked.", timeout=20_000)
    expect(page.locator("#voice")).to_have_value(option(page, "e2e-linked"), timeout=3000)


def test_a_link_title_is_shown_as_it_is_written(page, goto):
    def titled(route) -> None:
        answer = route.fetch()
        route.fulfill(response=answer, json={**answer.json(), "title": "Q&A with Ada <live>"})

    page.route("**/ui/resolve", titled)
    goto("/ui/speak/clone")
    settled(page)
    resolve(page, "https://example.com/questions")
    expect(page.locator("#cliplinkhint")).to_have_text("Q&A with Ada <live> · 3m 00s", timeout=3000)


def test_resolving_a_second_clip_link_abandons_the_first(page, goto, browser_log):
    first, second = "https://example.com/first-talk", "https://example.org/second-talk"
    goto("/ui/speak/clone")
    settled(page)
    hint = page.locator("#cliplinkhint")
    resolve(page, first)
    expect(hint).to_have_text("Probed talk first-talk · 3m 00s")
    resolve(page, second)
    expect(hint).to_have_text("Probed talk second-talk · 3m 00s")
    assert [a["json"] for a in browser_log.sent("POST", r"^/ui/abandon$")] == [{"token": first}]
    # The name the first link suggested is the reader's now, and is kept.
    expect(page.locator("#clipname")).to_have_value("probed-talk-first-talk")


def test_cancel_on_the_clone_sheet_abandons_a_resolved_link_and_returns_to_a_kokoro_voice(page, goto,
                                                                                        browser_log):
    link = "https://example.com/cancelled-talk"
    goto("/ui/speak/clone")
    ready(page)
    resolve(page, link)
    expect(page.locator("#cliprange")).to_be_visible()
    page.locator("#cancelclip").click()
    expect(page.locator("#clone")).to_be_hidden()
    expect(page.locator("#voice")).to_have_value("k:bm_george")
    expect(page.locator("#cliplink")).to_have_value("")
    expect(page.locator("#cliplinkhint")).to_be_empty()
    expect(page.locator("#cliprange")).to_be_hidden()
    abandoned = eventually(lambda: browser_log.sent("POST", r"^/ui/abandon$"))
    assert [a["json"] for a in abandoned] == [{"token": link}]
    page.wait_for_function("""() => location.pathname === "/ui/speak"
      && new URLSearchParams(location.search).get("voice") === "k:bm_george" """)


def test_a_clip_link_that_cannot_be_used_says_why_under_the_box(page, goto):
    goto("/ui/speak/clone")
    settled(page)
    resolve(page, "https://example.com/unsupported-thing")
    expect(page.locator("#cliplinkhint .note.bad")).to_contain_text("Unsupported URL")
    expect(page.locator("#cliprange")).to_be_hidden()
    expect(page.locator("#clipresolve")).to_be_enabled()


def test_deleting_a_cloned_voice_asks_first_and_removes_it(page, goto, dialogs, clips, media, browser_log):
    clips.add("e2e-doomed", media["clip"])
    goto("/ui/speak")
    ready(page)
    choose(page, "e2e-doomed")
    # The button takes the speed slider's place: a clip has no speed field.
    expect(page.locator("#delvoice")).to_be_visible()
    expect(page.locator("#speedwrap")).to_be_hidden()
    answers = [False, True]
    seen = dialogs(answer=lambda dialog: answers.pop(0))
    page.locator("#delvoice").click()
    expect(page.locator("#voice")).to_have_value(option(page, "e2e-doomed"))
    assert seen.seen == [("confirm", 'Delete the voice "e2e-doomed"? The clip is deleted from disk.')]
    assert not browser_log.sent("DELETE", r"^/ui/clips/")
    page.locator("#delvoice").click()
    expect(page.locator("#speak-note")).to_have_text("Deleted e2e-doomed.")
    assert [r["path"] for r in browser_log.sent("DELETE", r"^/ui/clips/")] == ["/ui/clips/e2e-doomed"]
    expect(page.locator("#voice option", has_text="e2e-doomed")).to_have_count(0)
    assert "e2e-doomed" not in clips.listed()
    # The built-in speaker is not a clip on disk, so it offers no delete.
    choose(page, "default (no clip needed)")
    expect(page.locator("#delvoice")).to_be_hidden()
    expect(page.locator("#speedwrap")).to_be_visible()


# ---- cloned voices: engines, controls and the queue ---------------------------------------


def test_a_cloned_voice_queues_a_job_with_the_engines_own_controls_and_opens_jobs(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    radios = page.locator('#engineopts input[name="engine"]')
    expect(page.locator("#enginerow")).to_be_visible()
    expect(radios).to_have_count(2)
    expect(page.locator('#engineopts input[value="chatterbox"]')).to_be_checked()
    # Nothing to listen to yet: one button, and it is the primary one.
    expect(page.locator("#go-tts")).to_be_hidden()
    expect(page.locator("#go-tts-quiet")).to_have_class(re.compile(r"\bprimary\b"))
    expect(page.locator("#tts-expert-fast")).to_be_hidden()
    expect(page.locator("#tts-expert-clone > summary")).to_have_text("Expert: Chatterbox voices")
    open_expert(page, "clone")
    nudge(page, "#x-exag", "ArrowRight", 2)
    nudge(page, "#x-cfg", "ArrowLeft", 2)
    nudge(page, "#x-temp", "ArrowRight", 1)
    page.locator("#text").fill(CLONE_TEXT)
    expect(page.locator("#go-tts-quiet")).to_have_text(re.compile(r"^Generate \((a few seconds|about \d+ seconds)\)$"))
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    page.wait_for_function("() => /^\\/ui\\/jobs\\/[0-9a-f-]{36}$/.test(location.pathname)")
    expect(page.locator("#tab-btn-jobs")).to_have_attribute("aria-selected", "true")
    [sent] = arrived(fake, mark, "tts_long", r"^/jobs$")
    assert sent["json"] == {"voice": "narrator", "language": "en", "model": "chatterbox", "exaggeration": 0.4,
                            "cfg_weight": 0.2, "temperature": 0.65, "text": CLONE_TEXT}
    job = page.evaluate("location.pathname.split('/').pop()")
    expect(page.locator(f'.job[data-job="{job}"]')).to_have_class(re.compile(r"\bhere\b"))


def test_turbo_is_sent_temperature_only_and_hides_the_other_two_sliders(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    open_expert(page, "clone")
    expect(page.locator("#clone-expressive")).to_be_visible()
    page.locator('#engineopts input[value="chatterbox-turbo"]').check()
    expect(page.locator("#clone-expressive")).to_be_hidden()
    expect(page.locator("#clone-temp")).to_be_visible()
    expect(page.locator("#clone-noexpress")).to_have_text(
        "Chatterbox Turbo does not take exaggeration or cfg_weight: they are refused by name rather than "
        "accepted and dropped. Delivery on this engine is set with temperature, which this deployment "
        "configures rather than this page.")
    page.locator("#text").fill(CLONE_TEXT)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    [sent] = arrived(fake, mark, "tts_long", r"^/jobs$")
    assert sent["json"] == {"voice": "narrator", "language": "en", "model": "chatterbox-turbo",
                            "temperature": 0.6, "text": CLONE_TEXT}


def test_the_expert_panel_names_the_engine_chosen_on_the_radio(page, goto):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    summary = page.locator("#tts-expert-clone > summary")
    expect(summary).to_have_text("Expert: Chatterbox voices")
    page.locator('#engineopts input[value="chatterbox-turbo"]').check()
    expect(summary).to_have_text("Expert: Chatterbox Turbo voices", timeout=3000)


def test_a_clip_too_short_for_an_engine_disables_it_with_the_reason(page, goto, clips, media):
    clips.add("e2e-short", media["tiny"])
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    page.locator('#engineopts input[value="chatterbox-turbo"]').check()
    choose(page, "e2e-short")
    turbo = page.locator('#engineopts input[value="chatterbox-turbo"]')
    expect(turbo).to_be_disabled()
    expect(page.locator("#engineopts label.engine.off")).to_contain_text(
        "this voice's clip is 3.0 s and it needs one over 5 s")
    expect(page.locator('#engineopts input[value="chatterbox"]')).to_be_checked()
    expect(page.locator("#enginenote")).to_have_text(
        "Chatterbox Turbo cannot take this voice, so Chatterbox is selected.")


def test_an_engine_that_cannot_speak_the_chosen_language_moves_it_and_says_so(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    page.locator("#lang").select_option("de")
    expect(page.locator("#lang")).to_have_value("de")
    page.locator('#engineopts input[value="chatterbox-turbo"]').check()
    expect(page.locator("#lang")).to_have_value("en")
    expect(page.locator("#enginenote")).to_have_text(
        "Chatterbox Turbo does not speak German, so Language is now English.")
    expect(page.locator('#lang option[value="de"]')).to_be_disabled()
    page.locator("#text").fill(CLONE_TEXT)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    [sent] = arrived(fake, mark, "tts_long", r"^/jobs$")
    assert (sent["json"]["model"], sent["json"]["language"]) == ("chatterbox-turbo", "en")


def test_a_preset_voice_carries_its_language_and_greys_the_language_control(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    page.locator("#voice").select_option("voxtral:casual_female")
    expect(page.locator("#lang")).to_be_disabled()
    expect(page.locator("#langsaid")).to_have_text(
        "Voxtral's language is carried by the voice: casual_female is English. Pick another voice to change it.")
    assert page.evaluate("""() => [...document.getElementById("lang").options]
      .filter(o => !o.disabled).map(o => o.value)""") == ["auto"]
    # The voice names the engine, so there is no engine to choose.
    expect(page.locator("#enginerow")).to_be_hidden()
    open_expert(page, "clone")
    for gone in ("#clone-expressive", "#clone-temp", "#resetclone"):
        expect(page.locator(gone)).to_have_js_property("hidden", True)
    expect(page.locator("#clone-noexpress")).to_have_text(
        "Voxtral does not take exaggeration, cfg_weight or temperature: they are refused by name rather than "
        "accepted and dropped. Delivery on this engine is set with cfg_alpha and flow_steps, which this "
        "deployment configures rather than this page.")
    page.locator("#text").fill(CLONE_TEXT)
    mark = fake.last_seq()
    page.locator("#go-tts-quiet").click()
    [sent] = arrived(fake, mark, "tts_long", r"^/jobs$")
    assert sent["json"] == {"voice": "casual_female", "model": "voxtral", "text": CLONE_TEXT}


def test_reset_to_defaults_restores_the_three_sliders_and_their_readouts(page, goto):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    open_expert(page, "clone")
    nudge(page, "#x-exag", "ArrowRight", 3)
    nudge(page, "#x-cfg", "ArrowLeft", 2)
    nudge(page, "#x-temp", "ArrowRight", 4)
    readouts = page.locator("#tts-expert-clone .slider output")
    expect(readouts).to_have_text(["0.45", "0.20", "0.80"])
    page.locator("#resetclone").click()
    for selector, value in (("#x-exag", "0.3"), ("#x-cfg", "0.3"), ("#x-temp", "0.6")):
        expect(page.locator(selector)).to_have_value(value)
    expect(readouts).to_have_text(["0.30", "0.30", "0.60"])
    assert page.locator("#x-exag").evaluate("el => el.style.getPropertyValue('--fill')") == "30.00%"


def test_a_job_longer_than_ten_minutes_offers_a_kokoro_voice_or_queues_it_anyway(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    page.locator("#text").fill("This sentence is long enough to be read. " * 140)
    expect(page.locator("#speak-note .note.warn")).to_contain_text("of compute.", timeout=3000)
    expect(page.locator("#useq")).to_have_text("Use bf_alice instead", timeout=3000)
    page.locator("#useq").click()
    expect(page.locator("#voice")).to_have_value("k:bf_alice")
    choose(page, "narrator")
    mark = fake.last_seq()
    page.locator("#anyway").click()
    assert arrived(fake, mark, "tts_long", r"^/jobs$")
    # The job it queued is minutes long and would still be running under the
    # next test, prefixing every title with its time left.
    fake.reset()


def test_a_job_tts_long_refuses_queues_nothing_and_stays_on_speak(page, goto, fake, browser_log):
    """Each refusal is turned into its own sentence, read here as the page
    writes it; whether the sentence then stays on screen is the next test."""
    browser_log.allow(503, r"/jobs$")
    browser_log.allow(504, r"/jobs$")
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    page.locator("#text").fill(CLONE_TEXT)
    # Every note the page writes under Speak, as it is added: what it said
    # is read here even where the page takes it down again in the same task.
    page.evaluate("""() => {
      window.__written = [];
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes)
          if (node.nodeType === 1 && node.classList.contains("note"))
            window.__written.push([node.className, node.textContent]);
      }).observe(document.getElementById("speak-note"), { childList: true });
    }""")
    for status in (429, 503, 504):
        fake.fail(r"^/jobs$", status=status, method="POST", backend="tts_long", times=1,
                  headers={"Retry-After": "30"}, json_body={"detail": "busy"})
        with page.expect_response(lambda r: r.url.endswith("/ui/api/jobs") and r.request.method == "POST") as answer:
            page.locator("#go-tts-quiet").click()
        assert answer.value.status == status
        expect(page.locator("#go-tts-quiet")).to_be_enabled()
        expect(page.locator("#tab-btn-speak")).to_have_attribute("aria-selected", "true")
    assert stored_jobs(page) == []
    written = [" ".join(text.split()) for kind, text in page.evaluate("window.__written") if kind == "note bad"]
    assert written == ["The queue is full. Nothing was queued. Try again in 30 s.",
                       "The cloning service is not answering. Try again in half a minute.",
                       "The gateway gave up waiting. Check the Jobs tab before you send it again."]


def test_a_busy_tts_long_answers_with_when_to_try_again(page, goto, fake):
    goto("/ui/speak")
    ready(page)
    choose(page, "narrator")
    page.locator("#text").fill(CLONE_TEXT)
    fake.fail(r"^/jobs$", status=429, method="POST", backend="tts_long", times=1,
              headers={"Retry-After": "30"}, json_body={"detail": "busy"})
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#speak-note .note.bad")).to_have_text(
        "The queue is full. Nothing was queued. Try again in 30 s.", timeout=3000)


def test_the_glyphs_on_record_and_stop_are_drawn_not_typed(page, goto):
    """A dot and a square came from whatever font had them; they are drawn in
    the dock's stroke now, and the button still names what it does."""
    goto("/ui/speak/clone")
    settled(page)
    record = page.locator("#rec")
    expect(record).to_be_visible()
    expect(record).to_have_text("Record 15 s")
    expect(record.locator("svg.glyph circle")).to_have_count(1)
    record.click()
    expect(record).to_have_text("Stop")
    expect(record.locator("svg.glyph rect")).to_have_count(1)
    record.click()
    expect(record).to_have_text("Record 15 s")
    expect(record.locator("svg.glyph circle")).to_have_count(1)


# ---- live, addresses and the tab as a whole ------------------------------------------------


def test_entering_speak_asks_for_the_voices_again_only_once_they_are_five_seconds_old(page, goto, clips,
                                                                                     media, browser_log):
    """A voice cloned on another device is in the picker the next time the
    tab is entered, without a reload; entering again at once asks nothing."""
    page.clock.install()
    goto("/ui")
    settled(page)
    page.wait_for_function("() => NAV.settled.has('voices')")
    loaded = page.evaluate("LOADED.voices")
    asked = len(browser_log.sent("GET", r"^/ui/api/voices$"))
    open_tab(page, "speak")
    assert page.evaluate("LOADED.voices") == loaded
    open_tab(page, "transcribe")
    clips.add("e2e-elsewhere", media["clip"])
    page.clock.fast_forward(6_000)
    open_tab(page, "speak")
    expect(page.locator("#voice option", has_text="e2e-elsewhere")).to_have_count(1)
    assert len(browser_log.sent("GET", r"^/ui/api/voices$")) == asked + 1


def test_choosing_a_voice_rewrites_the_address_and_adds_no_history(page, goto):
    goto("/ui/speak")
    ready(page)
    before = history_length(page)
    page.locator("#voice").select_option("k:af_heart")
    page.wait_for_function("() => new URLSearchParams(location.search).get('voice') === 'k:af_heart'")
    narrator = choose(page, "narrator")
    page.wait_for_function("v => new URLSearchParams(location.search).get('voice') === v", arg=narrator)
    page.locator("#voice").select_option("new")
    page.wait_for_function("() => location.pathname === '/ui/speak/clone' && !location.search")
    assert history_length(page) == before
    page.reload()
    settled(page)
    expect(page.locator("#clone")).to_be_visible()


def test_a_link_to_the_expert_panel_with_a_voice_opens_both(page, goto):
    goto("/ui/speak/expert?voice=k:af_heart")
    ready(page)
    expect(page.locator("#voice")).to_have_value("k:af_heart")
    expect(page.locator("#tts-expert-fast")).to_have_attribute("open", "")
    expect(page).to_have_title("Expert · Speak · Calliope")
    assert voice_in_bar(page) == "k:af_heart"


def test_a_link_to_the_expert_panel_with_a_voice_focuses_the_panel(page, goto):
    goto("/ui/speak/expert?voice=k:af_heart")
    ready(page)
    expect(page.locator("#tts-expert-fast")).to_have_attribute("open", "")
    expect(page.locator("#tts-expert-fast > summary")).to_be_focused(timeout=3000)


def test_opening_an_expert_panel_by_hand_is_a_place_and_closing_it_goes_back(page, goto):
    goto("/ui/speak")
    ready(page)
    narrator = choose(page, "narrator")
    before = history_length(page)
    summary = page.locator("#tts-expert-clone > summary")
    summary.click()
    page.wait_for_function("() => location.pathname === '/ui/speak/expert'")
    assert voice_in_bar(page) == narrator
    assert history_length(page) == before + 1
    expect(page).to_have_title("Expert · Speak · Calliope")
    summary.click()
    page.wait_for_function("() => location.pathname === '/ui/speak'")
    expect(page.locator("#tts-expert-clone")).not_to_have_attribute("open", "")
    # Back, not a new entry: Forward is the panel again.
    assert history_length(page) == before + 1
    page.go_forward()
    page.wait_for_function("() => location.pathname === '/ui/speak/expert'")
    expect(page.locator("#tts-expert-clone")).to_have_attribute("open", "")


def test_a_link_to_a_cloned_voice_survives_a_health_answer_that_lands_after_the_voices(page, goto):
    held: list = []
    page.route("**/ui/health", lambda route: held.append(route))
    goto("/ui/speak?voice=chatterbox:narrator")
    page.wait_for_function("() => NAV.settled.has('voices')")
    for route in held:
        route.continue_()
    page.unroute("**/ui/health")
    page.wait_for_function("() => engineIds().length > 0")
    expect(page.locator("#speak-note")).not_to_contain_text("does not have", timeout=3000)
    expect(page.locator("#voice")).to_have_value("chatterbox:narrator", timeout=3000)


def test_no_heading_on_speak_repeats_the_tab_name_with_every_panel_open(page, goto):
    """The masthead already says Speak. The smoke test reads the tab as it
    loads; this opens what only a press shows -- the clone sheet and each
    expert panel -- and reads every heading, disclosure and legend again."""
    goto("/ui/speak")
    ready(page)
    open_expert(page)
    choose(page, "narrator")
    open_expert(page, "clone")
    page.locator("#voice").select_option("new")
    expect(page.locator("#clone")).to_be_visible()
    headings = page.locator("#tab-speak :is(h2, h3, summary, legend)").evaluate_all(
        "els => els.filter(e => !e.closest('[hidden]')).map(e => e.textContent.trim())")
    assert headings, "nothing on the tab was read"
    name = page.locator("#word").inner_text().strip()
    assert name == "Speak"
    assert [h for h in headings if re.search(rf"\b{name}\b", h, re.IGNORECASE)] == []
