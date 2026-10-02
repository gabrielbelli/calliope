"""The Transcribe tab as drawn and as pressed, in a browser, against the local
stack.

WHAT IS ASSERTED, AND WHERE IT IS READ. What the page sent is read twice: from
the browser's own request log (browser_log) for the page's routes, and from the
fakes' log (fake.requests) for what arrived at stt-stack and MeTube, where a
multipart form is parsed into its fields. What the page shows is read off the
page. Nothing is ever heard: the browser runs with --mute-audio, and a player
is judged by its element's state (src, readyState, currentTime), never by
sound.

THE SOURCES. A file is chosen through the hidden <input type=file>, or dropped
as a DataTransfer on the drop zone, exactly as a browser delivers either. The
microphone is Chromium's synthetic one (conftest.browser_args). The media are
written once per module (the `media` fixture): WAVs at 16 and 44.1 kHz, a
forty-second one for the compute budget, random bytes named .mp3 that no
browser can decode, and, when ffmpeg is on PATH, a WebM with a picture.

LINKS. Resolving a link runs the page server's metadata probe, which on this
stack is e2e/bin/yt-dlp: it answers by a word in the link (live, subs, long,
unprobed), and the fake MeTube fails a download whose link says "broken" and
refuses one that says "unsupported". Links use example.com, the one name the
stack's resolver answers. A fake download takes three seconds.

THE CONFIG. Two ceilings come from the server (/ui/config): the upload ceiling
and the gateway's compute budget. A test that needs one lower answers the
page's own config request with the real answer plus its override
(`override_config`), so no second stack is started.
"""

from __future__ import annotations

import base64
import math
import random
import re
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pytest
from conftest import fetch_as_page
from fakes import DEFAULT_TRANSCRIPT
from playwright.sync_api import expect
from test_routes import here, history_length, open_tab, settled, wait_for_address

TRANSCRIBED = re.compile(r"^/(v1/audio/transcriptions|transcribe)$")


# ---- media -----------------------------------------------------------------------------


def tone(path: Path, seconds: float, rate: int) -> Path:
    """A mono 16-bit WAV of a warbling tone: something every browser decodes,
    of a length the fake stt reads back off its header."""
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            int(9000 * math.sin(2 * math.pi * (220 + 40 * math.sin(i / rate * 3)) * i / rate))
            .to_bytes(2, "little", signed=True) for i in range(frames)))
    return path


def ffmpeg(*args: str) -> None:
    subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, timeout=60, stdin=subprocess.DEVNULL, capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    """The files the tests pick, written once for the module. The WebM is made
    only when ffmpeg is installed, and the tests that need it skip without."""
    root = tmp_path_factory.mktemp("transcribe-media")
    junk = root / "junk.mp3"
    junk.write_bytes(random.Random(7).randbytes(64 * 1024))
    found = {"sixteen": tone(root / "speech-16k.wav", 10.0, 16000),
             "cd": tone(root / "speech-44k.wav", 6.0, 44100),
             "forty": tone(root / "forty-seconds.wav", 40.0, 16000),
             "junk": junk}
    if shutil.which("ffmpeg"):
        found["webm"] = root / "clip.webm"
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=6",
               "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
               "-c:v", "libvpx", "-b:v", "300k", "-c:a", "libopus", "-shortest", str(found["webm"]))
    return found


def needs(media: dict[str, Path], key: str) -> Path:
    if key not in media:
        pytest.skip("ffmpeg is not on PATH, so there is no video to drop")
    return media[key]


# ---- helpers ---------------------------------------------------------------------------


def ready(page) -> None:
    """The tab as the reader first meets it: the config, the profiles and the
    voices have answered."""
    settled(page)
    page.wait_for_function("() => NAV.settled.has('glossaries') && NAV.settled.has('voices')")


def override_config(page, **fields) -> None:
    """The page's own /ui/config, answered with the server's real one and
    `fields` over it. Installed before the page loads; boot reads it once."""
    def answer(route) -> None:
        response = fetch_as_page(route)
        route.fulfill(response=response, json=response.json() | fields)
    page.route("**/ui/config", answer)


def choose(page, path: Path) -> None:
    page.locator("#file").set_input_files(str(path))


def extracted(page) -> None:
    expect(page.locator("#stt-note .note.ok")).to_contain_text("Audio extracted")


def drop(page, path: Path, mime: str) -> None:
    """A file dropped on the drop zone, as the browser delivers one: the
    bytes in a DataTransfer on dragenter, dragover and drop."""
    page.evaluate("""([name, type, data]) => {
      const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
      const transfer = new DataTransfer();
      transfer.items.add(new File([bytes], name, { type }));
      const zone = document.getElementById("drop");
      for (const kind of ["dragenter", "dragover", "drop"])
        zone.dispatchEvent(new DragEvent(kind, { bubbles: true, cancelable: true, dataTransfer: transfer }));
    }""", [path.name, mime, base64.b64encode(path.read_bytes()).decode()])


def transcribe(page):
    """Press Transcribe and wait for the run to settle; the answer's response."""
    with page.expect_response(lambda r: r.request.method == "POST"
                              and TRANSCRIBED.search(urlparse(r.url).path)) as answer:
        page.locator("#go-stt").click()
    expect(page.locator("#stt-progress")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_enabled()
    return answer.value


def stt_forms(fake, since: int, path: str = r"^/v1/audio/transcriptions$") -> list[dict]:
    """The multipart forms stt-stack received since `since`, parsed."""
    return [r["form"] for r in fake.requests(backend="stt", method="POST", path=path, since=since)]


def expert(page) -> None:
    if page.locator("#stt-expert").get_attribute("open") is None:
        page.locator("#stt-expert > summary").click()
    expect(page.locator("#stt-expert")).to_have_attribute("open", "")


def link(word: str = "talk") -> str:
    """A link of its own for each test, so MeTube's record of one is never
    another's. The suffix is hex, which spells none of the stand-ins' words."""
    return f"https://example.com/{word}-{uuid.uuid4().hex[:8]}"


def resolve(page, url: str) -> None:
    page.locator("#url").fill(url)
    page.locator("#resolve").click()
    expect(page.locator("#confirm")).to_have_attribute("open", "")


def facts(page) -> dict[str, str]:
    """The confirm card's Length, Download and Transcribe rows."""
    return page.evaluate("""() => {
      const out = {};
      for (const dt of document.querySelectorAll("#c-facts dt"))
        out[dt.textContent] = dt.nextElementSibling.textContent;
      return out;
    }""")


def fetched(page) -> None:
    """Fetch and transcribe pressed: wait out the three-second download and
    the transcription after it."""
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/fetch", timeout=20_000):
        page.locator("#c-go").click()
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#stt-note")).to_be_empty()
    expect(page.locator("#go-stt")).to_be_enabled()


def committed(browser_log) -> list[dict]:
    return [r["json"] for r in browser_log.sent("POST", r"^/ui/commit$")]


def metube(fake, since: int, path: str) -> list[dict]:
    return fake.requests(backend="metube", path=path, since=since)


def words_of(text: str) -> int:
    return len(text.split())


@pytest.fixture
def transcript(fake):
    """fake.transcript(text) for one test, and the session's own put back."""
    yield fake.transcript
    fake.transcript(DEFAULT_TRANSCRIPT)


# ---- choosing a source -----------------------------------------------------------------


def test_choosing_a_wav_extracts_sixteen_kilohertz_audio_and_enables_transcribe(page, goto, fake, media):
    """The browser decodes the file and uploads 16 kHz mono, which is what the
    model reads anyway: a 44.1 kHz recording goes up at a third of its size,
    and the native route, which takes nothing else, stays open for it."""
    goto("/ui")
    ready(page)
    expect(page.locator("#go-stt")).to_be_disabled()
    since = fake.last_seq()
    choose(page, media["cd"])
    extracted(page)
    expect(page.locator("#stt-note")).to_contain_text("Audio extracted: 6s, now 0.2 MB instead of 0.5 MB.")
    expect(page.locator("#picked .filechip")).to_contain_text("speech-44k.wav")
    expect(page.locator("#go-stt")).to_be_enabled()
    expect(page.locator('#x-route option[value="native"]')).to_be_enabled()
    expect(page.locator('#x-route option[value="native"]')).to_have_text("/transcribe (16 kHz only)")
    assert not stt_forms(fake, since), "choosing a file sent it"
    transcribe(page)
    sent = stt_forms(fake, since)[-1]["file"]
    assert sent["filename"] == "speech-44k.wav" and sent["content_type"] == "audio/wav", sent
    assert sent["bytes"] == 44 + 6 * 16000 * 2, f"not six seconds of 16 kHz mono: {sent}"


def test_dropping_a_file_on_the_drop_zone_prepares_it_like_choosing_one(page, goto, fake, media):
    goto("/ui")
    ready(page)
    page.evaluate("""() => document.getElementById("drop").dispatchEvent(
      new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }))""")
    expect(page.locator("#drop")).to_have_class(re.compile(r"\bover\b"))
    drop(page, media["sixteen"], "audio/wav")
    expect(page.locator("#drop")).not_to_have_class(re.compile(r"\bover\b"))
    extracted(page)
    expect(page.locator("#stt-note")).to_contain_text("Audio extracted: 10s")
    expect(page.locator("#picked .filechip")).to_contain_text("speech-16k.wav")
    since = fake.last_seq()
    transcribe(page)
    assert stt_forms(fake, since)[-1]["file"]["bytes"] == 44 + 10 * 16000 * 2


def test_dropping_a_link_fills_the_box_and_does_not_resolve_it(page, goto, browser_log):
    """A link dragged out of another tab lands in the box with the caret in
    it. Resolving is a press of its own: it adds the link to MeTube."""
    goto("/ui")
    ready(page)
    url = link()
    page.evaluate("""url => {
      const transfer = new DataTransfer();
      transfer.setData("text/plain", url);
      document.getElementById("drop").dispatchEvent(
        new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    }""", url)
    expect(page.locator("#url")).to_have_value(url)
    expect(page.locator("#url")).to_be_focused()
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")
    assert not browser_log.sent("POST", r"^/ui/resolve$"), "a dropped link was resolved"


def test_pasting_a_link_with_nothing_focused_fills_the_box_once_and_does_not_resolve_it(
        page, goto, browser_log):
    """Two pastes: one with nothing focused, which the tab catches and puts in
    the box, and one into the box itself, which the tab leaves to the box. The
    second used to be caught as well and the link went in twice."""
    goto("/ui")
    ready(page)
    url = link()
    page.evaluate("""url => {
      document.activeElement && document.activeElement.blur && document.activeElement.blur();
      const transfer = new DataTransfer();
      transfer.setData("text/plain", url);
      document.body.dispatchEvent(new ClipboardEvent("paste",
        { bubbles: true, cancelable: true, clipboardData: transfer }));
    }""", url)
    expect(page.locator("#url")).to_have_value(url)
    expect(page.locator("#url")).to_be_focused()
    page.locator("#url").fill("")
    page.evaluate("url => navigator.clipboard.writeText(url)", url)
    page.locator("#url").focus()
    page.keyboard.press("ControlOrMeta+V")
    expect(page.locator("#url")).to_have_value(url)
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")
    assert not browser_log.sent("POST", r"^/ui/resolve$"), "a pasted link was resolved"


def test_pasting_a_link_into_another_field_is_left_to_that_field(page, goto):
    """A link pasted where the reader is typing belongs there: in Speak's text
    box, and in the confirm card's own fields, the link box is not touched."""
    goto("/ui")
    ready(page)
    url = link()
    page.evaluate("url => navigator.clipboard.writeText(url)", url)
    open_tab(page, "speak")
    page.locator("#text").fill("")
    page.locator("#text").focus()
    page.keyboard.press("ControlOrMeta+V")
    expect(page.locator("#text")).to_have_value(url)
    open_tab(page, "transcribe")
    expect(page.locator("#url")).to_have_value("")
    first = link()
    resolve(page, first)
    page.locator("#c-start").focus()
    page.keyboard.press("ControlOrMeta+V")
    expect(page.locator("#url")).to_have_value(first)
    page.locator("#c-cancel").click()


def test_removing_the_chosen_file_disables_transcribe_and_clears_the_note(page, goto, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    page.locator("#unpick").click()
    expect(page.locator("#picked")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_disabled()
    expect(page.locator("#stt-note")).to_be_empty()
    assert page.locator("#file").evaluate("input => input.files.length") == 0
    # The same file again is a change, because the input was emptied.
    choose(page, media["sixteen"])
    extracted(page)
    expect(page.locator("#go-stt")).to_be_enabled()


def test_a_file_this_browser_cannot_decode_is_uploaded_as_it_is_with_a_note_saying_so(
        page, goto, fake, media):
    goto("/ui")
    ready(page)
    choose(page, media["junk"])
    note = page.locator("#stt-note .note.flat")
    expect(note).to_contain_text("This browser could not decode that file")
    expect(note).to_contain_text("so it uploads as it is.")
    expect(page.locator("#go-stt")).to_be_enabled()
    since = fake.last_seq()
    transcribe(page)
    sent = stt_forms(fake, since)[-1]["file"]
    assert sent["filename"] == "junk.mp3" and sent["bytes"] == media["junk"].stat().st_size, sent


def test_a_file_over_the_upload_ceiling_is_refused_before_anything_is_sent(page, goto, fake, media):
    override_config(page, max_upload_bytes=100_000)
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    choose(page, media["sixteen"])
    expect(page.locator("#stt-note .note.bad")).to_have_text(
        re.compile(r"That file is 0\.3 MB\.\s+The server\s+refuses anything over 0\.1 MB\."))
    expect(page.locator("#go-stt")).to_be_disabled()
    assert not stt_forms(fake, since), "a file over the ceiling was sent"


def test_a_recording_past_the_compute_budget_warns_before_transcribing(page, goto, fake, media):
    """Past the gateway's budget the run is a 504 after the work is done, so
    the page says so as soon as it knows the length, and still lets it run."""
    override_config(page, stt_budget_seconds=2)
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    choose(page, media["forty"])
    warning = page.locator("#stt-note .note.warn")
    expect(warning).to_contain_text("2 s ceiling")
    expect(warning).to_contain_text("Split the")
    expect(page.locator("#go-stt")).to_be_enabled()
    assert not stt_forms(fake, since), "the warning sent the recording"


def test_stop_and_transcribe_records_from_the_microphone_and_transcribes(page, goto, fake, browser_log):
    """The button said "Stop and transcribe" and the page stopped at preparing
    the file. One press now sends it, and the stop mark is drawn, not typed."""
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    page.locator("#sttrec").click()
    stop = page.locator("#sttrecstop")
    expect(stop).to_be_visible()
    expect(page.locator("#sttrec")).to_be_disabled()
    expect(stop.locator("svg.glyph")).to_have_count(1)
    expect(stop).to_have_text("Stop and transcribe")
    expect(page.locator("#sttring")).not_to_have_text("0:00", timeout=5000)
    stop.click()
    expect(page.locator("#result")).to_be_visible(timeout=20_000)
    expect(page.locator("#sttrecrow")).to_be_hidden()
    expect(page.locator("#sttrec")).to_be_enabled()
    sent = browser_log.sent("POST", r"^/(v1/audio/transcriptions|transcribe)$")
    assert len(sent) == 1, f"one press should send exactly one transcription: {sent}"
    assert stt_forms(fake, since)[-1]["file"]["filename"] == "recording.wav"


def test_a_refused_microphone_says_so_and_points_to_a_file(page, goto):
    page.add_init_script("""navigator.mediaDevices.getUserMedia = () =>
      Promise.reject(new DOMException("Permission denied", "NotAllowedError"));""")
    goto("/ui")
    ready(page)
    page.locator("#sttrec").click()
    expect(page.locator("#stt-note .note.bad")).to_have_text(
        "No microphone: Permission denied. Drop a file instead.")
    expect(page.locator("#sttrecrow")).to_be_hidden()
    expect(page.locator("#sttrec")).to_be_enabled()


# ---- links -----------------------------------------------------------------------------


def test_resolving_a_link_opens_the_confirm_card_and_downloads_nothing(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    url = link()
    resolve(page, url)
    assert [r["json"] for r in browser_log.sent("POST", r"^/ui/resolve$")] == [{"url": url}]
    added = metube(fake, since, r"^/add$")
    assert [a["json"]["auto_start"] for a in added] == [False], f"resolving started a download: {added}"
    assert not metube(fake, since, r"^/start$") and not committed(browser_log)
    slug = url.rsplit("/", 1)[1]
    expect(page.locator("#c-title")).to_have_text("Fetch this?")
    expect(page.locator("#c-sub")).to_have_text(f"Probed talk {slug} · Example Channel")
    assert facts(page) == {"Length": "3m 00s", "Download": "2.7 MB of audio only",
                           "Transcribe": "about 20 seconds at 8.5×"}
    expect(page.locator("#c-live")).to_be_empty()
    expect(page.locator("#c-start")).to_have_value("")
    expect(page.locator("#c-end")).to_have_value("")
    expect(page.locator("#picked .filechip")).to_contain_text(f"Probed talk {slug}")
    expect(page.locator("#picked .filechip")).to_contain_text("3m 00s")
    page.locator("#c-cancel").click()


def test_resolving_says_nothing_is_downloaded_yet_and_holds_the_button(page, goto, fake):
    fake.fail(r"^/add$", status=None, backend="metube", delay=1.5, times=1)
    goto("/ui")
    ready(page)
    page.locator("#url").fill(link())
    page.locator("#resolve").click()
    expect(page.locator("#linkhint")).to_have_text("Resolving. Nothing is downloaded yet.")
    expect(page.locator("#resolve")).to_be_disabled()
    expect(page.locator("#confirm")).to_have_attribute("open", "")
    expect(page.locator("#linkhint")).to_be_empty()
    expect(page.locator("#resolve")).to_be_enabled()
    page.locator("#c-cancel").click()


def test_enter_in_the_link_box_resolves_it(page, goto, browser_log):
    goto("/ui")
    ready(page)
    page.locator("#url").fill(link())
    page.locator("#url").press("Enter")
    expect(page.locator("#confirm")).to_have_attribute("open", "")
    assert len(browser_log.sent("POST", r"^/ui/resolve$")) == 1
    page.locator("#c-cancel").click()


@pytest.mark.parametrize(("url", "said"), [
    ("https://example.com/unsupported-thing", "Unsupported URL"),
    ("https://example.com:8080/talk", "only ports 80 and 443 are fetched, not 8080"),
], ids=["metube-refuses", "the-guard-refuses"])
def test_an_unsupported_link_says_why_under_the_box(page, goto, fake, url, said):
    goto("/ui")
    ready(page)
    page.locator("#url").fill(url)
    page.locator("#resolve").click()
    expect(page.locator("#linkhint")).to_contain_text(said)
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")
    expect(page.locator("#picked")).to_be_hidden()
    expect(page.locator("#resolve")).to_be_enabled()


def test_an_unprobed_link_says_there_is_no_length_or_size(page, goto):
    """The probe failed, so the card has MeTube's title and nothing else, and
    says the estimate is missing rather than drawing one from nothing."""
    goto("/ui")
    ready(page)
    url = link("unprobed")
    resolve(page, url)
    expect(page.locator("#c-live")).to_have_text("No length or size for this link. The estimate is unavailable.")
    assert facts(page) == {"Length": "unknown", "Download": "unknown, audio only", "Transcribe": "unknown"}
    expect(page.locator("#c-sub")).to_have_text("Example talk: " + url.rsplit("/", 1)[1].replace("-", " "))
    expect(page.locator("#picked .filechip")).to_contain_text("duration unknown")
    page.locator("#c-cancel").click()


def test_ticking_keep_the_video_changes_the_download_row_and_is_cleared_for_the_next_link(page, goto):
    goto("/ui")
    ready(page)
    resolve(page, link())
    page.locator("#c-video").check()
    assert facts(page)["Download"] == "gigabytes, not the 2.7 MB of audio"
    page.locator("#c-video").uncheck()
    assert facts(page)["Download"] == "2.7 MB of audio only"
    page.locator("#c-video").check()
    page.locator("#c-cancel").click()
    resolve(page, link())
    expect(page.locator("#c-video")).not_to_be_checked()
    assert facts(page)["Download"] == "2.7 MB of audio only"
    page.locator("#c-cancel").click()


def test_keeping_the_video_asks_metube_for_the_video(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    resolve(page, link())
    page.locator("#c-video").check()
    fetched(page)
    assert committed(browser_log)[-1]["video"] is True
    started = [a["json"] for a in metube(fake, since, r"^/add$") if a["json"]["auto_start"]]
    assert started and started[-1]["download_type"] == "video", started
    expect(page.locator("#sttplay")).to_be_visible()


def test_dont_fetch_closes_the_card_and_abandons_the_link(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    url = link()
    resolve(page, url)
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/abandon") as answer:
        page.locator("#c-cancel").click()
    assert answer.value.json() == {"token": url, "reaped": True}
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")
    assert [r["json"] for r in browser_log.sent("POST", r"^/ui/abandon$")] == [{"token": url}]
    assert url not in fake.metube(), "MeTube still has the declined link"
    expect(page.locator("#picked")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_disabled()
    expect(page.locator("#stt-note")).to_be_empty()
    assert not committed(browser_log)


def test_escape_on_the_confirm_card_abandons_the_link(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    url = link()
    resolve(page, url)
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/abandon"):
        page.keyboard.press("Escape")
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")
    assert url not in fake.metube()
    expect(page.locator("#picked")).to_be_hidden()
    assert not committed(browser_log)


def test_fetch_and_transcribe_downloads_then_transcribes_with_timings(page, goto, fake, browser_log):
    """MeTube downloads the audio (the only time anything is downloaded), the
    page server hands it to stt itself, and the page asks for the body that
    carries word and segment timings, so the transcript can be followed."""
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    url = link()
    resolve(page, url)
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/fetch", timeout=20_000) as answer:
        page.locator("#c-go").click()
        expect(page.locator("#confirm")).not_to_have_attribute("open", "")
        said = page.locator("#stt-note .dlsaid")
        expect(said).to_have_text(re.compile(r"^Downloading \d+% · 2\.4 MB/s · \ds left$"))
        expect(page.locator("#stt-note .bar-fill")).to_be_attached()
        expect(page.locator("#go-stt")).to_be_disabled()
    assert answer.value.ok
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#stt-note")).to_be_empty()
    assert committed(browser_log) == [{"token": url, "clip_start": None, "clip_end": None, "video": False}]
    assert len(metube(fake, since, r"^/start$")) == 1, "an untrimmed audio fetch starts the parked record"
    query = parse_qs(urlparse(answer.value.url).query)
    assert query == {"response_format": ["verbose_json"], "timestamp_granularities": ["word", "segment"]}
    form = stt_forms(fake, since)[-1]
    assert form["model"] == "parakeet" and form["response_format"] == "verbose_json"
    assert form["timestamp_granularities[]"] == ["word", "segment"]
    assert form["file"]["filename"].endswith(".wav"), form
    expect(page.locator("#transcript .cue")).to_have_count(words_of(DEFAULT_TRANSCRIPT))
    expect(page.locator("#result-meta")).to_contain_text("12 s of audio")
    expect(page.locator("#go-stt")).to_be_enabled()


def test_a_trimmed_link_sends_its_start_and_stop_seconds(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    url = link()
    resolve(page, url)
    page.locator("#c-start").fill("30")
    page.locator("#c-end").fill("90")
    fetched(page)
    assert committed(browser_log)[-1] == {"token": url, "clip_start": 30, "clip_end": 90, "video": False}
    started = [a["json"] for a in metube(fake, since, r"^/add$") if a["json"]["auto_start"]]
    assert len(started) == 1 and started[0]["clip_start"] == 30 and started[0]["clip_end"] == 90, started


def test_stop_and_forget_it_abandons_a_download_in_progress(page, goto, fake, browser_log):
    """The note's own way out: the link is let go on the server, and the
    poll that was waiting on it asks nothing more."""
    goto("/ui")
    ready(page)
    url = link()
    resolve(page, url)
    page.locator("#c-go").click()
    stop = page.locator("#stopdl")
    expect(stop).to_be_visible()
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/abandon"):
        stop.click()
    mark = len(browser_log.requests)
    expect(page.locator("#picked")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_disabled()
    assert url not in fake.metube(), "MeTube still has the abandoned download"
    # One poll interval and a little more, watched from here in short steps.
    ends = time.monotonic() + 3.0
    while time.monotonic() < ends:
        page.wait_for_timeout(250)
    later = [r["path"] + "?" + r["query"] for r in browser_log.requests[mark:]]
    assert not [p for p in later if p.startswith("/ui/progress")], f"the poll went on after Stop: {later}"
    expect(page.locator("#stt-note")).not_to_contain_text("Lost track of the download")


def test_a_finished_link_plays_through_the_media_route_with_ranges(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    url = link()
    resolve(page, url)
    fetched(page)
    player = page.locator("#sttplayer")
    expect(page.locator("#sttplay")).to_be_visible()
    expect(page.locator("#sttwhy")).to_be_hidden()
    assert player.get_attribute("src") == "/ui/media?token=" + quote(url, safe="")
    page.wait_for_function("() => document.getElementById('sttplayer').readyState >= 1")
    assert abs(player.evaluate("p => p.duration") - 12.0) < 0.2
    assert browser_log.sent("GET", r"^/ui/media$"), "the player never asked the media route"
    served = metube(fake, since, r"^/(audio_)?download/")
    assert any(s["headers"].get("range") and s["status"] == 206 for s in served), \
        f"MeTube was never asked for a byte range: {served}"


def test_a_live_stream_offers_the_first_ten_minutes(page, goto, fake, browser_log):
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    resolve(page, link("live"))
    expect(page.locator("#c-title")).to_have_text("This is a live stream.")
    expect(page.locator("#c-live .note.bad")).to_contain_text("The stream has no end.")
    assert facts(page)["Length"] == "unknown"
    expect(page.locator("#c-end")).to_have_value("600")
    page.locator("#c-end").fill("")
    page.locator("#c-ten").click()
    expect(page.locator("#c-start")).to_have_value("0")
    expect(page.locator("#c-end")).to_have_value("600")
    fetched(page)
    assert committed(browser_log)[-1]["clip_start"] == 0 and committed(browser_log)[-1]["clip_end"] == 600
    started = [a["json"] for a in metube(fake, since, r"^/add$") if a["json"]["auto_start"]]
    assert started[-1]["clip_end"] == 600, started


def test_a_link_with_real_subtitles_reads_them_instead_of_transcribing(page, goto, fake, browser_log, media):
    """Subtitles a person wrote are already the transcript: MeTube fetches only
    them, the page parses them, and stt is never asked."""
    goto("/ui")
    ready(page)
    since = fake.last_seq()
    resolve(page, link("subs"))
    expect(page.locator("#c-subs .note.ok")).to_contain_text("This has real subtitles already")
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/captions", timeout=20_000):
        page.get_by_role("button", name="Use the existing subtitles").click()
        expect(page.locator("#confirm")).not_to_have_attribute("open", "")
        expect(page.locator("#go-stt")).to_have_text("Read the subtitles")
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#stt-note")).to_be_empty()
    assert committed(browser_log)[-1]["captions"] is True
    started = [a["json"] for a in metube(fake, since, r"^/add$") if a["json"]["auto_start"]]
    assert started[-1]["download_type"] == "captions", started
    assert not stt_forms(fake, since) and not browser_log.sent("POST", r"^/ui/fetch"), "the subtitles were transcribed"
    expect(page.locator("#result-meta")).to_have_text(re.compile(r"^subtitles · \d+ cues$"))
    expect(page.locator("#transcript")).to_contain_text("Calliope is listening.")
    expect(page.locator("#sttwhy")).to_have_text("No player: no media was downloaded.")
    expect(page.locator("#sttplay")).to_be_hidden()
    expect(page.locator("#dl-srt")).to_be_visible()
    expect(page.locator("#dl-vtt")).to_be_visible()
    # The button says what it will do, and a file is transcribed again.
    choose(page, media["sixteen"])
    expect(page.locator("#go-stt")).to_have_text("Transcribe")


def test_a_link_past_the_budget_asks_to_be_trimmed(page, goto):
    """Two hours at 8.5x is fourteen minutes of compute; under a ten-minute
    budget the card says it will not finish and points at the trim fields."""
    override_config(page, stt_budget_seconds=600)
    goto("/ui")
    ready(page)
    resolve(page, link("long"))
    assert facts(page) == {"Length": "2h 00m", "Download": "110 MB of audio only",
                           "Transcribe": "about 14 minutes at 8.5×"}
    expect(page.locator("#c-live .note.warn")).to_contain_text("This will not finish in one request.")
    expect(page.locator("#c-live .note.warn")).to_contain_text("Set Start at and Stop at to trim it.")
    page.locator("#c-cancel").click()


def test_a_download_metube_cannot_finish_says_so_and_reenables_transcribe(page, goto):
    goto("/ui")
    ready(page)
    resolve(page, link("broken"))
    page.locator("#c-go").click()
    expect(page.locator("#go-stt")).to_be_disabled()
    expect(page.locator("#stt-note .note.bad")).to_have_text(
        "MeTube could not download that: ERROR: [generic] Unable to download webpage: "
        "HTTP Error 403: Forbidden.", timeout=10_000)
    expect(page.locator("#go-stt")).to_be_enabled()
    expect(page.locator("#result")).to_be_hidden()


def test_removing_a_finished_link_lets_it_go_on_the_server(page, goto, fake):
    goto("/ui")
    ready(page)
    url = link()
    resolve(page, url)
    fetched(page)
    assert url in fake.metube()
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/abandon"):
        page.locator("#unpick").click()
    expect(page.locator("#picked")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_disabled()
    assert url not in fake.metube()


def test_transcribing_a_fetched_link_again_asks_stt_without_downloading_it_again(
        page, goto, fake, browser_log):
    """The finished file stays on the NAS, so a second run with another
    format is a second /ui/fetch and no second download."""
    goto("/ui")
    ready(page)
    resolve(page, link())
    fetched(page)
    since = fake.last_seq()
    expert(page)
    page.locator("#x-rf").select_option("text")
    with page.expect_response(lambda r: urlparse(r.url).path == "/ui/fetch"):
        page.locator("#go-stt").click()
    expect(page.locator("#go-stt")).to_be_enabled()
    assert len(committed(browser_log)) == 1, "the second run downloaded again"
    assert stt_forms(fake, since)[-1]["response_format"] == "text"
    expect(page.locator("#transcript .cue")).to_have_count(0)
    expect(page.locator("#sttplayhint")).to_have_text("No timings in this response.")


# ---- what is sent, and what comes back -------------------------------------------------


def test_transcribe_sends_verbose_json_with_word_and_segment_timings_by_default(page, goto, fake, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    since = fake.last_seq()
    response = transcribe(page)
    assert urlparse(response.url).path == "/v1/audio/transcriptions"
    form = stt_forms(fake, since)[-1]
    assert set(form) == {"file", "model", "response_format", "timestamp_granularities[]"}, form
    assert form["model"] == "parakeet" and form["response_format"] == "verbose_json"
    assert form["timestamp_granularities[]"] == ["word", "segment"]
    expect(page.locator("#result")).to_be_visible()
    expect(page.locator("#transcript .cue")).to_have_count(words_of(DEFAULT_TRANSCRIPT))
    expect(page.locator("#transcript")).to_have_class(re.compile(r"\bkaraoke\b"))
    meta = page.locator("#result-meta")
    expect(meta).to_contain_text("10 s of audio")
    expect(meta).to_contain_text("· Parakeet ·")
    expect(meta).to_have_text(re.compile(r"· [\d.]+× realtime$"))
    expect(page.locator("#sttplayhint")).to_have_text("Select a word to jump there.")
    expect(page.locator("#download")).to_have_text("Download .txt")
    expect(page.locator("#dl-srt")).to_be_visible()
    expect(page.locator("#dl-vtt")).to_be_visible()
    expect(page.locator("#repaired")).to_be_hidden()
    expect(page.locator("#stt-note")).to_be_empty()


def test_a_running_transcription_shows_its_progress_and_holds_the_button(page, goto, fake, media):
    fake.fail(r"^/v1/audio/transcriptions$", status=None, backend="stt", delay=2.0, times=1)
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    page.locator("#go-stt").click()
    expect(page.locator("#stt-note")).to_have_text("Transcribing…")
    expect(page.locator("#stt-progress")).to_be_visible()
    expect(page.locator("#go-stt")).to_be_disabled()
    expect(page.locator("#stt-elapsed")).to_have_text(re.compile(r"^\ds in, .* left · estimate$"))
    expect(page.locator("#result")).to_be_visible(timeout=10_000)
    expect(page.locator("#stt-progress")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_enabled()


def test_ticked_vocabulary_profiles_are_sent_as_one_glossary_field(page, goto, fake, media):
    goto("/ui")
    ready(page)
    expect(page.locator("#glossbox")).to_be_visible()
    for name in ("dictation", "tech"):
        toggle = page.locator(f'#gloss button[data-gloss="{name}"]')
        expect(toggle).to_have_attribute("aria-pressed", "false")
        toggle.click()
        expect(toggle).to_have_attribute("aria-pressed", "true")
    choose(page, media["sixteen"])
    extracted(page)
    since = fake.last_seq()
    transcribe(page)
    assert stt_forms(fake, since)[-1]["glossary"] == "dictation,tech"
    page.locator('#gloss button[data-gloss="dictation"]').click()
    since = fake.last_seq()
    transcribe(page)
    assert stt_forms(fake, since)[-1]["glossary"] == "tech"


def test_moving_a_vad_slider_sends_the_whole_chunking_strategy(page, goto, fake, media):
    """Defaults are not sent at all; one slider off its default sends all
    four fields, because the service takes the strategy whole."""
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-vad-t").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#x-vad-t")).to_have_value("0.51")
    expect(page.locator("#x-vad-t + output")).to_have_text("0.51")
    since = fake.last_seq()
    transcribe(page)
    form = stt_forms(fake, since)[-1]
    assert {k: v for k, v in form.items() if k.startswith("chunking_strategy")} == {
        "chunking_strategy[type]": "server_vad", "chunking_strategy[threshold]": "0.51",
        "chunking_strategy[prefix_padding_ms]": "100", "chunking_strategy[silence_duration_ms]": "300"}


def test_logprobs_switches_the_format_to_json_and_is_sent(page, goto, fake, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-gran").select_option("word")
    page.locator("#x-logprobs").check()
    expect(page.locator("#x-rf")).to_have_value("json")
    expect(page.locator("#x-gran")).to_have_value("")
    since = fake.last_seq()
    transcribe(page)
    form = stt_forms(fake, since)[-1]
    assert form["response_format"] == "json" and form["include[]"] == "logprobs", form
    assert "timestamp_granularities[]" not in form, form
    expect(page.locator("#transcript .cue")).to_have_count(0)
    expect(page.locator("#sttplayhint")).to_have_text("No timings in this response.")
    expect(page.locator("#result-meta")).to_contain_text("10 s billed")


def test_word_granularity_switches_the_format_to_verbose_json(page, goto, fake, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-rf").select_option("json")
    page.locator("#x-gran").select_option("word")
    expect(page.locator("#x-rf")).to_have_value("verbose_json")
    since = fake.last_seq()
    transcribe(page)
    form = stt_forms(fake, since)[-1]
    assert form["response_format"] == "verbose_json" and form["timestamp_granularities[]"] == "word", form
    expect(page.locator("#transcript .cue")).to_have_count(words_of(DEFAULT_TRANSCRIPT))


def test_the_native_route_sends_only_the_file_and_shows_compute_figures(page, goto, fake, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-route").select_option("native")
    for greyed in ("#x-rf", "#x-gran", "#x-logprobs", "#x-vad-t", "#x-vad-p", "#x-vad-s"):
        expect(page.locator(greyed)).to_be_disabled()
    since = fake.last_seq()
    response = transcribe(page)
    assert urlparse(response.url).path == "/transcribe"
    form = stt_forms(fake, since, r"^/transcribe$")[-1]
    assert set(form) == {"file"} and form["file"]["bytes"] == 44 + 10 * 16000 * 2, form
    assert not stt_forms(fake, since), "the native run also went to /v1"
    expect(page.locator("#result-meta")).to_contain_text("10.0 s of audio · 1.1 s of compute · 8.8× realtime")
    expect(page.locator("#transcript .cue")).to_have_count(0)
    expect(page.locator("#sttplayhint")).to_have_text("No timings in this response.")
    expect(page.locator("#dl-srt")).to_be_hidden()
    expect(page.locator("#download")).to_have_text("Download .txt")


def test_the_native_route_is_refused_for_audio_that_is_not_sixteen_kilohertz(page, goto, fake, media):
    """The native route takes 16 kHz only, and only a file the browser decoded
    is known to be that: an undecodable one turns the route off, and a choice
    of it already made goes back to /v1 rather than failing at the server."""
    goto("/ui")
    ready(page)
    expert(page)
    page.locator("#x-route").select_option("native")
    choose(page, media["junk"])
    expect(page.locator("#stt-note .note.flat")).to_contain_text("could not decode")
    native = page.locator('#x-route option[value="native"]')
    expect(native).to_be_disabled()
    expect(native).to_have_text("/transcribe: unavailable, this file is not 16 kHz")
    expect(page.locator("#x-route")).to_have_value("v1")
    since = fake.last_seq()
    response = transcribe(page)
    assert urlparse(response.url).path == "/v1/audio/transcriptions"
    assert not stt_forms(fake, since, r"^/transcribe$")


@pytest.mark.parametrize(("format", "start"), [
    ("srt", "1\n00:00:00,000 --> "),
    ("vtt", "WEBVTT\n\n00:00:00.000 --> "),
])
def test_an_expert_subtitle_format_writes_its_own_download_and_rules_out_the_native_route(
        page, goto, fake, media, format, start):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-rf").select_option(format)
    native = page.locator('#x-route option[value="native"]')
    expect(native).to_be_disabled()
    expect(native).to_have_text("/transcribe: unavailable, it cannot write subtitles")
    expect(page.locator("#x-route")).to_have_value("v1")
    since = fake.last_seq()
    transcribe(page)
    form = stt_forms(fake, since)[-1]
    assert form["response_format"] == format and "timestamp_granularities[]" not in form, form
    expect(page.locator("#download")).to_have_text(f"Download .{format}")
    expect(page.locator("#transcript .cue")).to_have_count(3)
    with page.expect_download() as saved:
        page.locator("#download").click()
    assert saved.value.suggested_filename == f"transcript.{format}"
    assert Path(saved.value.path()).read_text().startswith(start)


@pytest.mark.parametrize("format", ["json", "text"])
def test_an_answer_without_timings_offers_no_subtitle_files(page, goto, media, format):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-rf").select_option(format)
    transcribe(page)
    expect(page.locator("#transcript")).to_have_text(DEFAULT_TRANSCRIPT)
    expect(page.locator("#dl-srt")).to_be_hidden()
    expect(page.locator("#dl-vtt")).to_be_hidden()
    expect(page.locator("#download")).to_have_text("Download .txt")
    expect(page.locator("#sttplayhint")).to_have_text("No timings in this response.")


def test_the_expert_controls_a_link_cannot_carry_are_greyed(page, goto):
    """A link is transcribed by the page server, which carries the format and
    the granularities and nothing else, so the rest is greyed while a link is
    the source and comes back when it is let go."""
    goto("/ui")
    ready(page)
    expert(page)
    resolve(page, link())
    for greyed in ("#x-route", "#x-logprobs", "#x-vad-t", "#x-vad-p", "#x-vad-s"):
        expect(page.locator(greyed)).to_be_disabled()
    for open_ in ("#x-rf", "#x-gran"):
        expect(page.locator(open_)).to_be_enabled()
    page.locator("#c-cancel").click()
    for control in ("#x-route", "#x-logprobs", "#x-vad-t", "#x-rf", "#x-gran"):
        expect(page.locator(control)).to_be_enabled()


def test_stop_during_transcription_aborts_and_says_stopped(page, goto, fake, media):
    fake.fail(r"^/v1/audio/transcriptions$", status=None, backend="stt", delay=5.0, times=1)
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    page.locator("#go-stt").click()
    expect(page.locator("#stt-progress")).to_be_visible()
    page.locator("#stt-cancel").click()
    expect(page.locator("#stt-note")).to_have_text("Stopped.")
    expect(page.locator("#stt-progress")).to_be_hidden()
    expect(page.locator("#go-stt")).to_be_enabled()
    expect(page.locator("#result")).to_be_hidden()


def test_a_failed_transcription_shows_the_services_reason(page, goto, fake, browser_log, media):
    fake.fail(r"^/v1/audio/transcriptions$", status=500, backend="stt", times=1)
    browser_log.allow(500, r"^/v1/audio/transcriptions$")
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    expect(page.locator("#stt-note .note.bad")).to_have_text("Transcription failed: injected 500")
    expect(page.locator("#result")).to_be_hidden()


def test_a_transcription_that_never_reaches_the_server_says_so_in_words(page, goto, media):
    """A request that never arrived used to read "Failed to fetch". It is cut
    here as a dropped connection is, before any server sees it."""
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    page.route("**/v1/audio/transcriptions", lambda route: route.abort("aborted"))
    page.locator("#go-stt").click()
    expect(page.locator("#stt-note .note.bad")).to_have_text(
        "Transcription failed: This page could not reach the server. Check the connection and try again.")
    expect(page.locator("#go-stt")).to_be_enabled()
    expect(page.locator("#stt-progress")).to_be_hidden()


def test_copy_puts_the_transcript_on_the_clipboard(page, goto, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    page.locator("#copy").click()
    expect(page.locator("#copy")).to_have_text("Copied")
    assert page.evaluate("() => navigator.clipboard.readText()") == DEFAULT_TRANSCRIPT
    expect(page.locator("#copy")).to_have_text("Copy")


def test_download_srt_and_vtt_each_write_their_own_file(page, goto, media):
    """Three files off one run: the prose, and the segment cues as SubRip and
    as WebVTT, each named for what it is."""
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    files = {}
    for button in ("#download", "#dl-srt", "#dl-vtt"):
        with page.expect_download() as saved:
            page.locator(button).click()
        files[saved.value.suggested_filename] = Path(saved.value.path()).read_text()
    assert set(files) == {"transcript.txt", "transcript.srt", "transcript.vtt"}
    assert files["transcript.txt"] == DEFAULT_TRANSCRIPT
    assert re.match(r"1\n00:00:00,000 --> 00:00:0\d,\d{3}\nCalliope is listening\.\n\n2\n",
                    files["transcript.srt"]), files["transcript.srt"]
    assert re.match(r"WEBVTT\n\n00:00:00\.000 --> 00:00:0\d\.\d{3}\nCalliope is listening\.\n\n00:",
                    files["transcript.vtt"]), files["transcript.vtt"]
    assert files["transcript.srt"].count(" --> ") == files["transcript.vtt"].count(" --> ") == 3


def test_selecting_a_word_seeks_the_player_to_it_and_highlights_it(page, goto, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    page.wait_for_function("() => document.getElementById('sttplayer').readyState >= 1")
    word = page.locator("#transcript .cue").nth(9)
    start = page.evaluate("() => karaoke.cues[9].start")
    word.click()
    expect(word).to_have_class(re.compile(r"\bon\b"))
    assert page.locator("#transcript .cue.on").count() == 1
    assert abs(page.evaluate("() => document.getElementById('sttplayer').currentTime") - start) < 1.0
    page.evaluate("() => document.getElementById('sttplayer').pause()")


def test_a_dropped_webm_video_plays_in_the_stage_with_the_caption_band(page, goto, media):
    clip = needs(media, "webm")
    goto("/ui")
    ready(page)
    drop(page, clip, "video/webm")
    extracted(page)
    transcribe(page)
    expect(page.locator("#sttstage")).to_be_visible()
    expect(page.locator("#sttplayer")).to_be_hidden()
    video = page.locator("#sttvideo")
    assert (video.get_attribute("src") or "").startswith("blob:")
    page.wait_for_function("() => document.getElementById('sttvideo').readyState >= 1")
    assert video.evaluate("v => v.videoWidth") == 320
    video.evaluate("async v => { await v.play(); v.pause(); }")
    # Seeked while paused, so the line under the picture is the one at that
    # moment and not wherever playback had run on to.
    video.evaluate("v => { v.currentTime = 0.2; }")
    expect(page.locator("#sttband")).to_be_visible()
    expect(page.locator("#sttbandtext")).to_have_text("Calliope is listening.")
    video.evaluate("v => { v.currentTime = 2; }")
    expect(page.locator("#sttbandtext")).to_have_text(
        "The quick brown fox jumps over the lazy dog, and the transcript follows the audio word by word.")


def test_a_video_the_browser_cannot_play_falls_back_to_audio_only(page, goto, media):
    """A picture this browser will not render costs the picture and nothing
    else. No container reliably makes this browser's <video> fail while its
    audio still decodes, so the element's failure is raised on it as the
    browser raises one: an `error` event."""
    clip = needs(media, "webm")
    goto("/ui")
    ready(page)
    choose(page, clip)
    extracted(page)
    transcribe(page)
    expect(page.locator("#sttstage")).to_be_visible()
    page.evaluate("() => document.getElementById('sttvideo').dispatchEvent(new Event('error'))")
    expect(page.locator("#sttstage")).to_be_hidden()
    expect(page.locator("#sttplayer")).to_be_visible()
    assert (page.locator("#sttplayer").get_attribute("src") or "").startswith("blob:")
    expect(page.locator("#sttplayhint")).to_have_text("That video will not play here; audio only.")
    expect(page.locator("#transcript .cue")).to_have_count(words_of(DEFAULT_TRANSCRIPT))


def test_playback_speed_is_shared_by_every_player_and_remembered(page, goto, media):
    goto("/ui")
    ready(page)
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    page.locator("#sttrate").select_option("1.5")
    rates = page.evaluate("""() => ["sttplayer", "sttvideo", "player", "jobplayer"]
      .map(id => document.getElementById(id).playbackRate)""")
    assert rates == [1.5, 1.5, 1.5, 1.5], rates
    for select in ("#playrate", "#jobrate"):
        expect(page.locator(select)).to_have_value("1.5")
    page.reload()
    ready(page)
    expect(page.locator("#sttrate")).to_have_value("1.5")


def test_vocabulary_rewrites_on_the_native_route_are_listed(page, goto, fake, media, transcript):
    """A silent rewrite is worse than none: the terms the profile changed are
    named over the transcript."""
    transcript("I asked cloud code and entropic for a hand.")
    goto("/ui")
    ready(page)
    page.locator('#gloss button[data-gloss="dictation"]').click()
    choose(page, media["sixteen"])
    extracted(page)
    expert(page)
    page.locator("#x-route").select_option("native")
    since = fake.last_seq()
    transcribe(page)
    assert stt_forms(fake, since, r"^/transcribe$")[-1]["glossary"] == "dictation"
    expect(page.locator("#repaired")).to_be_visible()
    expect(page.locator("#repaired")).to_have_text("Vocabulary rewrote: Claude Code, Anthropic")
    expect(page.locator("#transcript")).to_have_text("I asked Claude Code and Anthropic for a hand.")


def test_the_default_route_hands_the_page_the_terms_a_profile_rewrote(page, goto, media, transcript):
    """What the next test needs from the stack, proved apart from it: on /v1
    the service names the rewritten terms in a header, and the gateway and
    the page server carry it all the way to the page."""
    transcript("I asked cloud code for a hand.")
    goto("/ui")
    ready(page)
    page.locator('#gloss button[data-gloss="dictation"]').click()
    choose(page, media["sixteen"])
    extracted(page)
    response = transcribe(page)
    assert unquote(response.headers.get("x-glossary-repaired", "")) == "Claude Code", response.headers
    expect(page.locator("#transcript")).to_have_text("I asked Claude Code for a hand.")


def test_vocabulary_rewrites_on_the_default_route_are_listed(page, goto, media, transcript):
    transcript("I asked cloud code for a hand.")
    goto("/ui")
    ready(page)
    page.locator('#gloss button[data-gloss="dictation"]').click()
    choose(page, media["sixteen"])
    extracted(page)
    transcribe(page)
    expect(page.locator("#transcript")).to_have_text("I asked Claude Code for a hand.")
    expect(page.locator("#repaired")).to_have_text("Vocabulary rewrote: Claude Code", timeout=2000)


# ---- addresses, and what changes by itself ---------------------------------------------


def test_the_transcribe_address_is_the_bare_page(page, goto):
    """/ui is Transcribe's address; its long form and an unknown tail under it
    are replaced, not kept."""
    for address in ("/ui/transcribe", "/ui/transcribe/nonsense"):
        goto(address)
        ready(page)
        wait_for_address(page, "/ui")
        assert page.title() == "Transcribe · Calliope"
        expect(page.locator("#stt-expert")).not_to_have_attribute("open", "")


def test_opening_expert_pushes_its_address_and_closing_it_goes_back(page, goto):
    goto("/ui")
    ready(page)
    before = history_length(page)
    page.locator("#stt-expert > summary").click()
    wait_for_address(page, "/ui/transcribe/expert")
    assert history_length(page) == before + 1
    assert page.title() == "Expert · Transcribe · Calliope"
    page.locator("#stt-expert > summary").click()
    wait_for_address(page, "/ui")
    assert history_length(page) == before + 1, "closing it pushed instead of going back"
    page.go_forward()
    wait_for_address(page, "/ui/transcribe/expert")
    expect(page.locator("#stt-expert")).to_have_attribute("open", "")
    page.reload()
    ready(page)
    expect(page.locator("#stt-expert")).to_have_attribute("open", "")
    expect(page.locator("#stt-expert > summary")).to_be_focused()
    assert here(page) == "/ui/transcribe/expert"


def test_a_profile_saved_elsewhere_appears_in_the_chooser_when_transcribe_is_entered_again(
        page, goto, stack):
    """The chooser is read again on the way into the tab once it is five
    seconds old, and what the reader had ticked stays ticked."""
    page.clock.install()
    goto("/ui")
    ready(page)
    page.locator('#gloss button[data-gloss="tech"]').click()
    with stack.client() as other:
        other.put("/glossaries/fromtablet", json={"text": "kuber netes = Kubernetes\n"}).raise_for_status()
        try:
            open_tab(page, "speak")
            page.clock.fast_forward(6000)
            open_tab(page, "transcribe")
            expect(page.locator('#gloss button[data-gloss="fromtablet"]')).to_be_visible()
            expect(page.locator('#gloss button[data-gloss="tech"]')).to_have_attribute("aria-pressed", "true")
            expect(page.locator('#gloss button[data-gloss="fromtablet"]')).to_have_attribute(
                "aria-pressed", "false")
        finally:
            other.delete("/glossaries/fromtablet")


def test_the_chooser_is_hidden_when_the_service_lists_no_profiles(page, goto, fake, browser_log):
    fake.fail(r"^/glossaries$", status=503, backend="stt")
    browser_log.allow(503, r"^/glossaries$")
    goto("/ui")
    ready(page)
    expect(page.locator("#glossbox")).to_be_hidden()
    expect(page.locator("#gloss button")).to_have_count(0)


def test_without_link_ingestion_the_link_box_is_hidden_and_a_pasted_link_is_ignored(page, goto):
    override_config(page, ingestion=False)
    goto("/ui")
    ready(page)
    expect(page.locator("#linkrow")).to_be_hidden()
    page.evaluate("""url => {
      const transfer = new DataTransfer();
      transfer.setData("text/plain", url);
      document.body.dispatchEvent(new ClipboardEvent("paste",
        { bubbles: true, cancelable: true, clipboardData: transfer }));
    }""", link())
    expect(page.locator("#url")).to_have_value("")


# ---- design ----------------------------------------------------------------------------


def test_the_vocabulary_chooser_is_separate_toggles_with_a_group_name(page, goto):
    """Any number of profiles can be ticked, so each is a toggle with its own
    edge rather than a segment of one strip, and the group says what it is."""
    goto("/ui")
    ready(page)
    group = page.get_by_role("group", name="Vocabulary")
    expect(group).to_be_visible()
    toggles = group.locator("button").evaluate_all("""els => els.map(e => {
      const s = getComputedStyle(e), b = e.getBoundingClientRect();
      return { left: b.left, right: b.right, radius: parseFloat(s.borderTopLeftRadius),
               border: parseFloat(s.borderLeftWidth), pressed: e.getAttribute("aria-pressed") };
    })""")
    assert len(toggles) >= 2, toggles
    for toggle in toggles:
        assert toggle["pressed"] in ("true", "false"), "a profile is not a toggle"
        assert toggle["radius"] > 0 and toggle["border"] >= 1, "a profile has no edge of its own"
    for one, two in zip(toggles, toggles[1:]):
        assert two["left"] - one["right"] >= 3, "two profiles are joined into one strip"


def test_the_link_box_keeps_its_name_while_typing(page, goto):
    """Its only name was a placeholder, which goes with the first keystroke."""
    goto("/ui")
    ready(page)
    box = page.get_by_role("textbox", name="Link to a video or audio page")
    expect(box).to_be_visible()
    box.fill("https://example.com/talk")
    expect(page.get_by_role("textbox", name="Link to a video or audio page")).to_have_value(
        "https://example.com/talk")
