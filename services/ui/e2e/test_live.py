"""What the page reads by itself, in a browser, against the local stack.

The table is in services/ui/README.md, "What updates by itself": the hub's
stream opened at load, health on a chain that stops while the page is
hidden, the satellite lists every 30 s when calm and every 3 s while
something moves, the profiles and voices read again on the way into their
tabs, the jobs asked for when health shows somebody else's job, and nothing
asked for at all a minute after the page goes into the background.
test_live_data.py, test_satellites_live.py and test_jobs_live.py drive each
rule in Node; this is the same against the real page, gateway and hub.

"Another device" is plain httpx from the test to the gateway, with an admin
key of the same admin the page is signed in as, never a second browser. Long
timers run on Playwright's clock (page.clock), installed before
the page loads. Tests that change what the hub holds use `fresh_hub` and are
the last in the file.
"""

from __future__ import annotations

import array
import copy
import io
import math
import re
import time
import wave

import httpx
import pytest
from conftest import fetch_as_page
from playwright.sync_api import expect
from test_routes import seeded_job
from test_satellites import forget_cut_streams, lost_hub_is_not_a_fault

KITCHEN, LOUNGE, HALLWAY = "020000000001", "020000000002", "020000000003"
# Twelve sentences: a scripted job is queued for a second and then speaks one
# sentence every 1.2 s, so this one stays live for about fifteen seconds.
LONG_TEXT = " ".join(f"This is sentence number {n} of a job queued on another device." for n in range(1, 13))
LISTS = r"^/satellites$"


# ---- helpers ---------------------------------------------------------------------------


def settled(page) -> None:
    """The router has an answer for the address (as in test_routes.py)."""
    page.wait_for_function("""() => {
      const kind = NAV_WAITS[NAV.route.view];
      return (!kind || NAV.settled.has(kind)) && !NAV.pending
        && navPath(NAV.route) === location.pathname + location.search;
    }""")


def stream_open(page) -> None:
    until(page, lambda: page.evaluate("() => !!SATELLITES.events && SATELLITES.events.readyState === 1"),
          "the hub's stream open", seconds=20)


def calm(page) -> None:
    """The Satellites tab on its calm cadence: the stream is open, and a poll
    has been drawn since it opened (the first poll opens it, and schedules
    the next at 3 s because it is still connecting)."""
    stream_open(page)
    until(page, lambda: page.evaluate("() => SATELLITES.applied >= 2 && !SATELLITES.polling"),
          "a poll drawn since the stream opened", seconds=20)


def pause(page) -> None:
    """Stop the page's clock where it is; only run_for moves it from here.
    pause_at takes seconds, not the page's milliseconds. While it is paused,
    wait on the test's side (until, expect): wait_for_function polls on the
    page's animation frames, which a paused clock does not run."""
    page.clock.pause_at(page.evaluate("Date.now()") / 1000 + 0.05)


def open_tab(page, tab: str) -> None:
    page.locator(f'[role=tab][data-tab="{tab}"]').click()
    expect(page.locator(f'[role=tab][data-tab="{tab}"]')).to_have_attribute("aria-selected", "true")


def until(page, condition, what: str, seconds: float = 10.0, state=None) -> None:
    """Wait for a condition on the test's side (what the page asked for),
    looking every 100 ms. `state`, when given, is called for the failure
    message."""
    ends = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < ends, f"never happened: {what}" + (f"; {state()}" if state else "")
        page.wait_for_timeout(100)


def lists(browser_log) -> int:
    return len(browser_log.sent("GET", LISTS))


def another_device(stack) -> httpx.Client:
    """The admin's phone: the same person, with an admin key."""
    return stack.client()


def wav(seconds: float = 6.0, rate: int = 24000) -> bytes:
    """A voice clip as another device would upload one: a plain tone."""
    samples = array.array("h", (int(6000 * math.sin(2 * math.pi * 220 * i / rate))
                                for i in range(int(seconds * rate))))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return out.getvalue()


# A wake word on a satellite, delivered through the page's own stream handler
# as the hub's stream delivers one. Through the hub itself (push-to-talk) the
# wake is followed, at a speed nobody controls, by the end of the turn, which
# clears what this sets; delivered here, the page's clock alone decides.
WAKE = """id => SATELLITES.events.onmessage({ data: JSON.stringify(
  { type: "wake", satellite: id, wake_word: "alexa", score: 0.93, at: Date.now() / 1000 }) })"""


def chip(page, nid: str):
    return page.locator(f'li.sat[data-id="{nid}"] .sat-state').first


@pytest.fixture
def live_jobs(stack) -> list[str]:
    """The ids of jobs a test starts, cancelled on tts-long when it ends: each
    lasts about fifteen seconds, and one still running would keep the next
    test's page polling for it, hidden or not."""
    ids: list[str] = []
    yield ids
    for job_id in ids:
        stack.api.delete(f"/jobs/{job_id}")


# ---- the dock's counts -----------------------------------------------------------------


def test_the_pending_satellite_badge_shows_on_first_load_without_opening_satellites(page, goto):
    """Hallway is waiting to be adopted. The count used to appear only once
    somebody had visited Satellites."""
    goto("/ui")
    expect(page.locator("#satellitecount")).to_have_text("1")
    stream_open(page)
    expect(page.locator("#tab-satellites")).to_be_hidden()


def test_a_job_started_elsewhere_raises_the_jobs_badge_without_a_reload(page, goto, stack, live_jobs):
    """A job queued from the phone moves tts-long's queue, which the next
    health read shows; the page asks for the jobs and the dock counts it,
    with the Jobs tab never opened."""
    page.clock.install()
    goto("/ui")
    page.wait_for_function("() => LIVE.queue !== null")
    expect(page.locator("#jobcount")).to_have_text("")
    with another_device(stack) as http:
        answer = http.post("/jobs", json={"text": LONG_TEXT, "voice": "narrator"})
    answer.raise_for_status()
    live_jobs.append(answer.json()["id"])
    # The gateway keeps a health probe for 5 s (D50): the next read the page
    # makes is one taken after the job was queued.
    stack.fake.fresh_health()
    page.clock.fast_forward(30_000)
    expect(page.locator("#jobcount")).to_have_text("1")


def test_a_job_started_elsewhere_appears_on_the_open_jobs_tab_within_one_health_poll(page, goto, stack,
                                                                                     live_jobs):
    page.clock.install()
    goto("/ui/jobs")
    settled(page)
    page.wait_for_function("() => LIVE.queue !== null")
    with another_device(stack) as http:
        job = http.post("/jobs", json={"text": LONG_TEXT, "voice": "narrator"}).json()
    live_jobs.append(job["id"])
    stack.fake.fresh_health()
    page.clock.fast_forward(30_000)
    expect(page.locator(f'.job[data-job="{job["id"]}"]')).to_be_visible()


# ---- vocabulary ------------------------------------------------------------------------


def test_a_profile_saved_elsewhere_appears_when_vocabulary_is_opened(page, goto, stack):
    page.clock.install()
    goto("/ui")
    page.wait_for_function("() => LOADED.glossaries > 0 && NAV.settled.has('glossaries')")
    with another_device(stack) as http:
        http.put("/glossaries/fromphone", json={"text": "kubernetes\n"}).raise_for_status()
        try:
            page.clock.fast_forward(6_000)
            open_tab(page, "vocab")
            expect(page.locator('#glossnames [data-open="fromphone"]')).to_be_visible()
        finally:
            http.delete("/glossaries/fromphone")


def test_a_profile_changed_elsewhere_reloads_an_untouched_editor_and_warns_an_edited_one(page, goto, stack):
    """Nobody has typed in the editor: it is brought up to date in place.
    Somebody has: their text stays, and they are told."""
    with another_device(stack) as http:
        http.put("/glossaries/livetest", json={"text": "alpha = Alpha\n"}).raise_for_status()
        try:
            page.clock.install()
            goto("/ui/vocabulary/livetest")
            settled(page)
            text = page.locator("#glosstext")
            expect(text).to_have_value("alpha = Alpha\n")

            http.put("/glossaries/livetest", json={"text": "beta = Beta\n"}).raise_for_status()
            page.clock.fast_forward(6_000)
            open_tab(page, "transcribe")
            open_tab(page, "vocab")
            expect(text).to_have_value("beta = Beta\n")
            expect(page.locator("#glossnote")).to_have_text("")

            text.fill("beta = Beta\nmine = Mine\n")
            http.put("/glossaries/livetest", json={"text": "gamma = Gamma\n"}).raise_for_status()
            page.clock.fast_forward(6_000)
            open_tab(page, "transcribe")
            open_tab(page, "vocab")
            expect(page.locator("#glossnote")).to_contain_text(
                "This profile was changed on another device since you opened it.")
            expect(text).to_have_value("beta = Beta\nmine = Mine\n")
        finally:
            http.delete("/glossaries/livetest")


def test_saving_over_a_profile_changed_elsewhere_asks_first(page, goto, stack, dialogs, browser_log):
    with another_device(stack) as http:
        http.put("/glossaries/savetest", json={"text": "one = One\n"}).raise_for_status()
        try:
            goto("/ui/vocabulary/savetest")
            settled(page)
            expect(page.locator("#glosstext")).to_have_value("one = One\n")
            page.locator("#glosstext").fill("one = One\nmine = Mine\n")
            http.put("/glossaries/savetest", json={"text": "two = Two\n"}).raise_for_status()

            answers = [False, True]
            seen = dialogs(answer=lambda dialog: answers.pop(0)).seen
            page.locator("#glosssave").click()
            until(page, lambda: len(seen) == 1, "the question before saving")
            assert seen[0][0] == "confirm"
            assert "was changed on another device since you opened it. Save yours over it?" in seen[0][1]
            expect(page.locator("#glosssave")).to_be_enabled()
            assert not browser_log.sent("PUT", r"/glossaries/savetest$"), "a No still saved"

            page.locator("#glosssave").click()
            expect(page.locator("#glossnote")).to_contain_text("Saved")
            assert http.get("/glossaries/savetest").json()["text"] == "one = One\nmine = Mine\n"
        finally:
            http.delete("/glossaries/savetest")


def test_vocabulary_recovers_after_the_service_was_down_at_load(page, goto, fake, browser_log):
    """A 503 at load used to read as a deployment with no vocabulary service,
    for good. It says the service did not answer, and the tab asks again."""
    browser_log.allow(503, r"^/glossaries$")
    fake.fail(r"^/glossaries$", status=503, times=1)
    page.clock.install()
    goto("/ui/vocabulary")
    expect(page.locator("#glossnone")).to_have_text(
        "The transcription service did not answer, so its profiles cannot be listed. "
        "This tab asks again when you come back to it.")
    page.clock.fast_forward(6_000)
    open_tab(page, "speak")
    open_tab(page, "vocab")
    expect(page.locator('#glossnames [data-open="dictation"]')).to_be_visible()
    expect(page.locator("#glossnone")).to_be_hidden()


# ---- voices and engines ----------------------------------------------------------------


def test_the_voice_list_recovers_after_tts_was_down_at_load(page, goto, fake, browser_log):
    browser_log.allow(503, r"^/voices$")
    fake.fail(r"^/voices$", status=503, times=1)
    page.clock.install()
    goto("/ui/speak")
    until(page, lambda: "Could not list voices" in page.locator("#speak-note").inner_text(),
          "the note saying the voices could not be listed", state=lambda: (
              page.evaluate("() => [VOICES_FAILED, Object.keys(VOICES.kokoro || {}).length]"),
              [(r["status"], r["path"]) for r in browser_log.responses if "voices" in r["path"]],
              fake.requests(path="voices")))
    page.clock.fast_forward(6_000)
    open_tab(page, "transcribe")
    open_tab(page, "speak")
    expect(page.locator("#speak-note")).to_have_text("")
    expect(page.locator('#voice option[value="k:af_heart"]')).to_be_attached()


def clone_option(page, name: str):
    """A cloned voice's option, by its name: its value carries the engine."""
    return page.locator("#voice option").filter(has_text=re.compile(f"^{re.escape(name)}$"))


def test_a_voice_cloned_elsewhere_appears_in_the_picker_on_return(page, goto, stack, hide, show):
    page.clock.install()
    goto("/ui/speak")
    settled(page)
    with another_device(stack) as http:
        http.post("/ui/clips", data={"name": "phonevoice"},
                  files={"file": ("phonevoice.wav", wav(), "audio/wav")}).raise_for_status()
        try:
            hide(page)
            page.clock.fast_forward(61_000)
            show(page)
            expect(clone_option(page, "phonevoice")).to_be_attached()
        finally:
            http.delete("/ui/clips/phonevoice")


def test_a_selected_clone_deleted_elsewhere_is_said_not_swapped_silently(page, goto, stack):
    with another_device(stack) as http:
        http.post("/ui/clips", data={"name": "goneclip"},
                  files={"file": ("goneclip.wav", wav(), "audio/wav")}).raise_for_status()
        page.clock.install()
        goto("/ui/speak")
        settled(page)
        chosen = page.locator("#voice").select_option(label="goneclip")[0]
        http.delete("/ui/clips/goneclip").raise_for_status()
    page.clock.fast_forward(6_000)
    open_tab(page, "transcribe")
    open_tab(page, "speak")
    expect(page.locator("#speak-note")).to_contain_text(
        "goneclip is no longer on this server, so another voice is selected.")
    expect(page.locator("#voice")).not_to_have_value(chosen)


def engine_radios(page):
    radios = page.locator("#engineopts input[name=engine]")
    expect(radios.first).to_be_attached()
    return radios


def test_the_engine_picker_follows_the_runner_without_a_reload(page, goto, stack, fake):
    """An engine whose runner goes away is disabled with the reason at the
    next health read, rather than staying selectable until its job fails."""
    page.clock.install()
    goto("/ui/speak")
    settled(page)
    page.locator("#voice").select_option(label="narrator")
    enabled = engine_radios(page).evaluate_all("els => els.filter(e => !e.disabled).map(e => e.value)")
    target = next(e for e in enabled if e != "chatterbox")
    original = stack.fake.backend_health("tts_long")["engines"]
    changed = copy.deepcopy(original)
    changed[target]["local"] = dict(changed[target]["local"], ready=False, why="not in TTS_LOCAL_ENGINES")
    changed[target]["runner"] = dict(changed[target]["runner"], ready=False, why="the runner is switched off")
    fake.health("tts_long", engines=changed)
    try:
        radio = page.locator(f'#engineopts input[value="{target}"]')
        expect(radio).to_be_enabled()
        page.clock.fast_forward(30_000)
        expect(radio).to_be_disabled()
    finally:
        fake.health("tts_long", engines=original)


def test_a_focused_engine_radio_keeps_focus_across_a_health_poll(page, goto):
    """renderEngines runs on every health read, and rewrote the radios each
    time: the one under the keyboard was replaced by a copy without it."""
    page.clock.install()
    goto("/ui/speak")
    settled(page)
    page.locator("#voice").select_option(label="narrator")
    radio = engine_radios(page).locator("visible=true").first
    radio.focus()
    radio.evaluate("el => { el.__kept = true; }")
    before = page.evaluate("LIVE.healthAt")
    page.clock.fast_forward(30_000)
    page.wait_for_function("t => LIVE.healthAt > t", arg=before)
    assert page.evaluate("document.activeElement && document.activeElement.__kept === true"), \
        "the focused radio was rebuilt under the keyboard"


# ---- satellites ------------------------------------------------------------------------


def test_a_status_change_on_a_satellite_reaches_its_row_without_a_poll(page, goto, fake, browser_log):
    """The status a satellite sends every few seconds is the poll's own
    answer; the row takes it from the stream. Muted is read from it."""
    goto("/ui/satellites/kitchen")
    settled(page)
    calm(page)
    before = lists(browser_log)
    fake.satellite_status("kitchen", muted=True)
    try:
        expect(chip(page, KITCHEN)).to_have_text("Muted", timeout=2_000)
        assert lists(browser_log) == before, "the row waited for a poll"
    finally:
        fake.satellite_status("kitchen", muted=False)
    expect(chip(page, KITCHEN)).not_to_have_text("Muted")


def test_the_satellites_tab_polls_every_thirty_seconds_when_calm_and_three_while_listening(
        page, goto, browser_log):
    """Calm, with the stream open, the tab asks every 30 s. A wake word is
    followed by Answering, which no event reports, so the next read is
    brought forward to 3 s from the wake word."""
    page.clock.install()
    goto("/ui/satellites")
    settled(page)
    calm(page)
    pause(page)
    start = lists(browser_log)
    page.clock.run_for(29_000)
    page.wait_for_timeout(300)
    assert lists(browser_log) == start, "asked before 30 s with nothing moving"
    page.clock.run_for(2_000)
    until(page, lambda: lists(browser_log) == start + 1, "the 30 s read", state=lambda: page.evaluate(
        "() => [Date.now(), SATELLITES.timer, SATELLITES.polling, SATELLITES.applied, SATELLITES.asked,"
        " SATELLITES.events && SATELLITES.events.readyState, satellitesCadence()]"))
    until(page, lambda: not page.evaluate("() => !!SATELLITES.polling"), "the 30 s read drawn")

    woken = lists(browser_log)
    page.evaluate(WAKE, KITCHEN)
    page.clock.run_for(2_800)
    page.wait_for_timeout(300)
    assert lists(browser_log) == woken, "asked before 3 s"
    page.clock.run_for(400)
    until(page, lambda: lists(browser_log) == woken + 1, "the 3 s read after a wake word")


def test_the_listening_chip_clears_after_fifteen_seconds_on_its_own_timer(page, goto, browser_log):
    """Listening is lit by a wake word and lasts 15 s. It is cleared by a
    timer of its own, not by the next poll: here every read of the lists is
    held unanswered, as a hub busy sending an update would leave it, and the
    chip still goes out on time."""
    page.clock.install()
    goto("/ui/satellites")
    settled(page)
    calm(page)
    pause(page)
    held = []
    page.route(re.compile(r"//[^/]+/satellites$"), lambda route: held.append(route))
    try:
        page.clock.run_for(30_100)
        until(page, lambda: held, "the next read of the lists")
        page.evaluate(WAKE, KITCHEN)
        expect(chip(page, KITCHEN)).to_have_text("Listening")
        page.clock.run_for(14_000)
        expect(chip(page, KITCHEN)).to_have_text("Listening")
        page.clock.run_for(1_200)
        expect(chip(page, KITCHEN)).not_to_have_text("Listening")
        assert len(held) == 1, "the chip waited for a read of the lists"
    finally:
        # Unrouting lets the held reads go on to the hub.
        page.unroute(re.compile(r"//[^/]+/satellites$"))


# ---- jobs ------------------------------------------------------------------------------


def test_focus_on_a_job_button_survives_the_polls_of_a_live_job(page, goto, fake, live_jobs):
    """A live job redraws the list every 2 s; the Stop button under the
    keyboard was replaced each time, and the focus fell to the page."""
    job = fake.add_job(text=LONG_TEXT)
    live_jobs.append(job["id"])
    goto("/ui/jobs")
    settled(page)
    stop = page.locator(f'[data-stop="{job["id"]}"]')
    expect(stop).to_be_visible()
    stop.focus()
    page.evaluate("""() => {
      window.__draws = 0;
      const draw = window.renderJobs;
      window.renderJobs = function () { window.__draws++; return draw.apply(this, arguments); };
    }""")
    page.wait_for_function("() => window.__draws >= 2", timeout=15_000)
    assert page.evaluate("document.activeElement && document.activeElement.dataset.stop") == job["id"]


def test_a_filter_answer_that_lands_late_does_not_replace_the_current_filter(page, goto, fake, stack, browser_log):
    """The Playable listing at load is held for 2 s; meanwhile the reader
    chooses Failures. The Playable answer lands after the Failures one and is
    not drawn under that label. Playable comes from the address, because the
    tab opens on Everything."""
    # Asked before the delay is set: this read would be the one it held.
    done = seeded_job(stack, "clone", "done", "present")
    failed = seeded_job(stack, "clone", "failed")
    fake.fail(r"^/jobs$", status=None, delay=2, times=1)
    goto("/ui/jobs?show=playable")
    page.locator("#jobfilter").select_option("failed")
    expect(page.locator(f'.job[data-job="{failed}"]')).to_be_visible()
    until(page, lambda: any(r["path"] == "/jobs" and "audio=present" in r["url"]
                            for r in browser_log.responses), "the held Playable listing", seconds=10)
    page.wait_for_timeout(300)
    expect(page.locator(f'.job[data-job="{done}"]')).to_have_count(0)
    expect(page.locator(f'.job[data-job="{failed}"]')).to_be_visible()
    expect(page.locator("#jobfilter")).to_have_value("failed")


def test_a_job_deleted_while_a_poll_is_in_flight_does_not_come_back(page, goto, fake):
    """A listing that left holding the job lands after the job is deleted.
    Merged, the row came back until the next poll, a delete that looked as
    if it had not worked."""
    job = fake.add_job(text="One sentence for a run that failed.", scripted=False, status="failed",
                       error="made for this test")
    goto("/ui/jobs")
    settled(page)
    row = page.locator(f'.job[data-job="{job["id"]}"]')
    expect(row).to_be_visible()
    held = []
    # The listing, with or without a filter in its query: Everything, the
    # default, sends none.
    listing = re.compile(r"//[^/]+/jobs(\?[^/]*)?$")
    page.route(listing, lambda route: held.append(route))
    try:
        page.locator("#refresh").click()
        until(page, lambda: held, "the Refresh listing")
        answer = fetch_as_page(held[0])
        assert job["id"] in answer.text(), "the held listing does not carry the job, so it proves nothing"
        page.locator(f'[data-forget="{job["id"]}"]').click()
        expect(row).to_have_count(0)
        held[0].fulfill(response=answer)
        expect(page.locator("#refresh")).to_have_text("Refresh")
        page.wait_for_timeout(300)
        expect(row).to_have_count(0)
    finally:
        page.unroute(listing)


# ---- link downloads --------------------------------------------------------------------


def test_the_download_note_keeps_its_stop_button_under_the_keyboard(page, goto):
    """The note was rewritten whole every 2 s, its Stop and forget it button
    with it, so the keyboard on that button lost it at the next tick."""
    goto("/ui")
    settled(page)
    page.locator("#url").fill("https://example.com/a-long-talk")
    page.locator("#resolve").click()
    expect(page.locator("#confirm")).to_have_attribute("open", "")
    page.locator("#c-go").click()
    stop = page.locator("#stopdl")
    expect(stop).to_be_visible()
    first = page.locator("#stt-note .dlsaid").inner_text()
    stop.focus()
    stop.evaluate("el => { el.__kept = true; }")
    page.wait_for_function("""first => {
      const said = document.querySelector("#stt-note .dlsaid");
      return said && said.textContent !== first;
    }""", arg=first)
    assert page.evaluate("document.activeElement && document.activeElement.__kept === true"), \
        "the button under the keyboard was rebuilt"


# ---- a page in the background ----------------------------------------------------------


def test_a_hidden_page_sends_no_requests_after_a_minute(page, goto, hide, browser_log):
    page.clock.install()
    goto("/ui")
    settled(page)
    stream_open(page)
    page.wait_for_function("() => LOADED.voices > 0 && LOADED.glossaries > 0 && LIVE.healthAt > 0")
    pause(page)
    hide(page)
    page.clock.run_for(61_000)
    until(page, lambda: page.evaluate("() => SATELLITES.events === null && SATELLITES.evGap === true"),
          "the stream closed a minute after hiding")
    mark = len(browser_log.requests)
    page.clock.run_for(600_000)
    page.wait_for_timeout(500)
    assert browser_log.requests[mark:] == [], \
        f"a hidden page asked for {[r['path'] for r in browser_log.requests[mark:]]}"


def test_returning_to_a_hidden_page_brings_the_open_view_up_to_date_and_marks_the_gap(
        page, goto, hide, show, browser_log):
    page.clock.install()
    goto("/ui/satellites")
    settled(page)
    stream_open(page)
    pause(page)
    hide(page)
    page.clock.run_for(61_000)
    until(page, lambda: page.evaluate("() => SATELLITES.events === null"), "the stream closed")
    mark = len(browser_log.requests)
    show(page)
    stream_open(page)
    expect(page.locator("#satelliteevents li").first).to_contain_text("reconnected")
    asked = {r["path"] for r in browser_log.requests[mark:]}
    assert {"/satellites", "/health", "/satellites/events"} <= asked, asked


def test_the_activity_stream_reconnects_after_a_hub_restart_with_the_tab_closed(page, goto, stack, browser_log):
    """A reconnect answered 503 while the hub restarts closes the stream for
    good; nothing polls with the tab shut, so Activity stayed silent until a
    reload. The live layer opens another once the hub answers, and Activity
    marks the gap."""
    lost_hub_is_not_a_fault(browser_log)
    goto("/ui")
    settled(page)
    stream_open(page)
    stack.stop_hub()
    until(page, lambda: page.evaluate("() => !SATELLITES.events || SATELLITES.events.readyState !== 1"),
          "the stream lost with the hub")
    stack.start_hub()
    stream_open(page)
    forget_cut_streams(browser_log)
    open_tab(page, "satellites")
    expect(page.locator("#satelliteevents li").first).to_contain_text("reconnected")


# ---- the hub changed: these restart it first -------------------------------------------


def test_a_satellite_waiting_to_be_adopted_raises_the_badge_while_another_tab_is_open(page, goto, fake, fresh_hub):
    goto("/ui")
    expect(page.locator("#satellitecount")).to_have_text("1")
    stream_open(page)
    fake.satellite_drop("hallway")
    expect(page.locator("#satellitecount")).to_have_text("")
    fake.satellite_start("hallway")
    expect(page.locator("#satellitecount")).to_have_text("1")


def test_a_change_made_in_home_assistant_is_seen_through_its_config_event(page, goto, stack, fresh_hub):
    """Home Assistant renames the kitchen through the hub's API. The tab asks
    every 30 s while calm, and the config event the hub publishes brings the
    new name at once."""
    goto("/ui/satellites")
    settled(page)
    calm(page)
    stack.api.patch(f"/satellites/{KITCHEN}", json={"name": "Kitchen Two"}).raise_for_status()
    expect(page.locator(f'li.sat[data-id="{KITCHEN}"] .sat-name').first).to_have_text(
        "Kitchen Two", timeout=5_000)


def test_telemetry_size_follows_new_records_while_open(page, goto, stack, fake, browser_log, fresh_hub):
    """Telemetry is read every 30 s while its section is open, so what the hub
    records meanwhile (here, turned on elsewhere and given a satellite's
    status to record) shows beside Download without reopening it; closed, it
    is not read at all."""
    reads = lambda: len(browser_log.sent("GET", r"^/satellites/telemetry$"))  # noqa: E731
    page.clock.install()
    goto("/ui/satellites/telemetry")
    settled(page)
    expect(page.locator("#tm-sum")).to_have_text("off")
    expect(page.locator("#tmsize")).to_have_text("Nothing recorded yet.")
    hub = stack.client()
    hub.put("/satellites/telemetry", json={"enabled": True, "level": "full"}).raise_for_status()
    fake.satellite_status("kitchen")
    until(page, lambda: hub.get("/satellites/telemetry").json().get("bytes", 0) > 0, "a record on the hub")
    hub.close()
    before = reads()
    page.clock.fast_forward(30_000)
    until(page, lambda: reads() == before + 1, "the 30 s telemetry read")
    expect(page.locator("#tm-sum")).to_have_text("recording everything")
    expect(page.locator("#tmsize")).to_contain_text("kept, over 1 day")
    page.locator("#sat-telemetry > summary").click()
    expect(page.locator("#sat-telemetry")).not_to_have_attribute("open", "")
    closed = reads()
    page.clock.fast_forward(30_000)
    page.clock.fast_forward(30_000)
    page.wait_for_timeout(300)
    assert reads() == closed, "telemetry was read with its section closed"

