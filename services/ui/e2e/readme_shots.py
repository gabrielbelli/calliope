"""Screenshots for the README. Not a test of behaviour: it asserts only what it
must wait for before a shot.

Run it by path. The name does not match pytest's test_*.py, so a run of the
whole directory never collects it:

    $PY -m pytest -q services/ui/e2e/readme_shots.py -p no:cacheprovider

Each shot is WEBUI/shots/readme-<name>-<light|dark>.png, 1280 x 800 CSS pixels
at two device pixels per CSS pixel. The browser, the lock, the stack and the
clean-up are the harness's own (conftest.py); the only thing changed here is
the device scale of the contexts these tests make.

The content is stand-in data that reads like a household's: `alex`, an admin,
with three API keys and a few finished jobs; two other people the admin has
added; a short meeting transcribed with the tech vocabulary ticked.
"""

from __future__ import annotations

import time

import pytest
from conftest import password_if_asked
from fakes import DEFAULT_TRANSCRIPT
from playwright.sync_api import expect
from test_routes import settled
from test_satellites import LOUNGE, sat, stream_open
from test_speak import choose as choose_voice
from test_speak import ready as speak_ready
from test_transcribe import choose, extracted, tone, transcribe
from test_transcribe import ready as transcribe_ready

SIZE = (1280, 800)
SCHEMES = ("light", "dark")
ALEX = "alex"

MEETING = (
    "Morning, everyone, let's keep this one short. The release candidate went to staging last night "
    "and the smoke tests all passed. The only open item is the slow query on the reports page. "
    "We traced it to a missing index in post gres, and the fix is up for review on git hub. "
    "Support asked for the launch notes by Thursday, so please add anything customer-facing to the "
    "shared document today. Design signed off the new onboarding screens yesterday, so that work is "
    "unblocked. If nothing else comes up, we ship on Monday morning.")

BRIEFING = (
    "Good morning. Here is your briefing for Thursday. It stays dry and bright until mid-afternoon, "
    "with showers arriving from the west by six. You have two meetings today: the design review at "
    "ten, and a call with the suppliers at half past two.")

KEYS = [("Home Assistant", "home-assistant", 365, True),
        ("Dictation shortcut", "transcribe-only", 365, True),
        ("Grafana", "monitor", 90, False)]

CHAPTER = (
    "The lighthouse keeper climbed the spiral stairs for the last time that winter. Below him the sea "
    "had gone quiet, the way it does before a change in the weather. He trimmed the wick, wound the "
    "clockwork, and sat down by the window to wait for the boat.")
REMINDER = "Your eleven o'clock with the design team has moved to half past twelve. The room is the same."
INTERVIEW = (
    "Thanks for having me. The short version is that we started with a single microphone in the "
    "kitchen, and the whole house grew out of that one experiment.")


@pytest.fixture
def hidpi(e2e, monkeypatch):
    """Every context this test makes draws two device pixels per CSS pixel."""
    browser = e2e.launch()
    make = browser.new_context
    monkeypatch.setattr(browser, "new_context", lambda **kw: make(**(kw | {"device_scale_factor": 2})))


@pytest.fixture(scope="module")
def alex(e2e):
    """alex, an admin, signed in through /login once; jordan and priya, added
    by the admin and not yet signed in; alex's keys, two of them used once;
    and alex's finished jobs."""
    stack = e2e.stack
    person = stack.create_person(ALEX, "admin")
    e2e.person(ALEX)
    stack.create_person("jordan", "user")
    stack.create_person("priya", "user-jobs")
    for name, preset, days, used in KEYS:
        key = stack.mint_key(person, preset, name=name, expires_days=days)
        if used:
            with stack.client(key) as http:
                http.get("/v1/models").raise_for_status()
    now = time.time()
    mine = {"owner": person.id, "credential": "session", "scripted": False, "status": "done",
            "host": "tower", "runner_host": "tower"}
    jobs = [
        # (text, engine, voice, age in s, audio s, compute s, extra)
        (MEETING, "parakeet", None, 540, 34.0, 0.6,
         {"kind": "transcribe", "service": "stt-stack", "route": "/v1/audio/transcriptions"}),
        (CHAPTER, "chatterbox", "narrator", 2600, 212.4, 151.7, {}),
        (REMINDER, "kokoro", "bf_emma", 5200, 6.8, 2.4, {"kind": "speech", "service": "tts-stack"}),
        (INTERVIEW, "chatterbox", "narrator", 9100, 64.9, 47.3, {}),
    ]
    for text, engine, voice, age, audio, compute, extra in jobs:
        created = now - age
        chunks = max(1, text.count(". ") + 1)
        fields = mine | {"text": text, "engine": engine, "voice": voice, "created_at": created,
                         "started_at": created + 1, "finished_at": created + 1 + compute,
                         "audio_seconds": audio, "speech_seconds": audio, "compute_seconds": compute,
                         "realtime_factor": round(audio / compute, 2), "chunks": chunks,
                         "offsets": [round(audio * i / chunks, 2) for i in range(chunks)]} | extra
        if engine == "chatterbox":
            # A clone keeps its audio: any path makes the fake call it present.
            fields |= {"path": f"/out/{int(created)}.wav", "bytes": int(audio * 48000)}
        stack.fake.add_job(**fields)
    return person


def calm(page, ms: int = 700) -> None:
    """Long enough for the dock's spring and a panel's entrance, which run on
    requestAnimationFrame and which Playwright cannot stop."""
    page.wait_for_timeout(ms)


def align(page, selector: str, edge: str = "top", at: int = 24, card: bool = False) -> dict:
    """Scroll so the `edge` of `selector`, or of the card around it, sits `at`
    CSS pixels below the window's top. The browser stops at the page's ends.
    Returns, and prints, where it ended up."""
    where = page.evaluate("""([sel, edge, at, card]) => {
      let el = document.querySelector(sel);
      if (card) el = el.closest(".card");
      const r = el.getBoundingClientRect();
      window.scrollTo({ top: Math.max(0, scrollY + r[edge] - at), behavior: "instant" });
      const now = el.getBoundingClientRect();
      return { scrollY, max: document.documentElement.scrollHeight - innerHeight,
               top: now.top, bottom: now.bottom };
    }""", [selector, edge, at, card])
    print(f"align {selector} {edge}@{at}: {where}")
    calm(page, 400)
    return where


def rect(page, selector: str) -> dict:
    found = page.evaluate("s => { const r = document.querySelector(s).getBoundingClientRect();"
                          " return { top: r.top, bottom: r.bottom }; }", selector)
    print(f"rect {selector}: {found}")
    return found


def both(page, screenshot, name: str) -> None:
    for scheme in SCHEMES:
        page.emulate_media(color_scheme=scheme)
        calm(page, 500)
        screenshot(f"readme-{name}-{scheme}", target=page)
    page.emulate_media(color_scheme="light")


def test_login(hidpi, new_page, goto, screenshot):
    page = new_page(SIZE, user=None)
    goto("/login", target=page)
    expect(page.locator("#signin")).to_be_visible()
    page.locator("#username").fill(ALEX)
    page.locator("#password").fill("correct horse battery staple")
    page.locator("#password").evaluate("e => e.blur()")
    calm(page)
    both(page, screenshot, "login")


def test_transcribe(hidpi, alex, new_page, goto, screenshot, fake, tmp_path):
    fake.transcript(MEETING)
    # Held a little, so the realtime figure is a GPU's and not a stand-in's.
    fake.fail(r"^/v1/audio/transcriptions$", status=None, delay=0.55, backend="stt")
    try:
        page = new_page(SIZE, user=ALEX)
        goto("/ui", target=page)
        transcribe_ready(page)
        page.locator('#gloss button[data-gloss="tech"]').click()
        choose(page, tone(tmp_path / "weekly-planning.wav", 34.0, 16000))
        extracted(page)
        transcribe(page)
        expect(page.locator("#repaired")).to_be_visible()
        page.locator("#go-stt").evaluate("e => e.blur()")
        align(page, "#drop", edge="bottom", at=-2)
        rect(page, "#result")
        rect(page, "#dock")
        rect(page, "#bead")
        both(page, screenshot, "transcribe")
    finally:
        fake.transcript(DEFAULT_TRANSCRIPT)


def test_speak(hidpi, alex, new_page, goto, screenshot):
    page = new_page(SIZE, user=ALEX)
    goto("/ui/speak", target=page)
    speak_ready(page)
    page.locator("#text").fill(BRIEFING)
    choose_voice(page, "bf_emma")
    page.locator("#go-tts-quiet").click()
    expect(page.locator("#player")).to_be_visible()
    page.locator("#go-tts-quiet").evaluate("e => e.blur()")
    calm(page)
    both(page, screenshot, "speak")


def test_jobs(hidpi, alex, new_page, goto, screenshot):
    page = new_page(SIZE, user=ALEX)
    goto("/ui/jobs", target=page)
    settled(page)
    expect(page.locator("#joblist .job[data-job]").nth(3)).to_be_visible()
    calm(page)
    both(page, screenshot, "jobs")


def test_satellites(hidpi, alex, new_page, goto, screenshot):
    page = new_page(SIZE, user=ALEX)
    goto("/ui/satellites", target=page)
    settled(page)
    stream_open(page)
    lounge = sat(page, LOUNGE).locator(":scope > details.sat-row")
    lounge.locator(":scope > summary").click()
    expect(lounge).to_have_attribute("open", "")
    calm(page)
    both(page, screenshot, "satellites")


def test_account(hidpi, alex, new_page, goto, screenshot):
    page = new_page(SIZE, user=ALEX)
    goto("/ui/account", target=page)
    settled(page)
    expect(page.locator("#keys tbody tr").nth(len(KEYS) - 1)).to_be_visible()
    align(page, "#sessions", edge="top", at=24, card=True)
    both(page, screenshot, "account")


def test_admin(hidpi, alex, new_page, goto, screenshot):
    page = new_page(SIZE, user=ALEX)
    goto("/ui/admin", target=page)
    settled(page)
    password_if_asked(page, alex.password, page.locator("#users tbody tr").nth(4))
    calm(page)
    both(page, screenshot, "admin")
