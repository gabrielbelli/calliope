"""The Jobs tab, in a browser, against the local stack.

The stack seeds five runs (stack.py, fakes.seed_jobs): a clone with its audio,
a clone whose audio was deleted, a failed clone whose audio was swept, a
Kokoro run and a transcription. The tts-long fake answers every filter with
every live and failed run in it, as the service does. Every test starts from
those five and nothing else (`seeded`), so a test that deletes a run or adds
one changes nothing for the next.

What each test asserts is what the page sent (the browser's request log, or
what reached the fakes) and what it shows. Nothing is listened to: the
browser is muted, and the chime is read off a spy on AudioContext.

The address and the title are test_routes.py's; what updates by itself is
test_live.py's. What is here is the tab's own controls and states, and the
places the two meet it: a row's text has an address, and the ladder decides
when the list is asked for.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
from playwright.sync_api import expect
from test_routes import history_length, seeded_job, settled, wait_for_address

ROW_BUTTONS = "#tab-jobs .row > button, #tab-jobs .job .acts > button"
# A listing of the runs: the gateway's own /jobs, the one door the page has
# (see test_a_filtered_listing_is_asked_through_the_pages_own_door).
LISTING = r"^/jobs$"
UNREACHABLE = "This page could not reach the server. Check the connection and try again."
# The query each filter sends, from JOB_FILTERS in the page. Everything sends
# none, and a kind other than All kinds adds `kind`.
FILTER_QUERY = {"all": {}, "playable": {"audio": "present,pending"}, "deleted": {"audio": "deleted"},
                "expired": {"audio": "expired"}, "failed": {"status": "failed,cancelled"}}
KINDS = ["all", "clone", "speech", "transcribe"]


# ---- helpers ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def seeded(fake):
    """The five seeded runs and nothing else, the fakes' failures and health
    as they start, and an empty request log. Reset after the test as well, so
    a run left going here is not still live under the next file's tests."""
    fake.reset()
    yield
    fake.reset()


def record(stack, job: str) -> dict | None:
    """What tts-long keeps of a run, behind the page's back; None once it is gone."""
    return stack.fake.job(job)


def job_row(page, job: str):
    return page.locator(f'.job[data-job="{job}"]')


def status(page, job: str):
    return job_row(page, job).locator(".top .status")


def actions(page, job: str) -> list[str]:
    return [label.strip() for label in job_row(page, job).locator(".acts button").all_text_contents()]


def listings(browser_log) -> list[dict]:
    return browser_log.sent("GET", LISTING)


def query(entry: dict) -> dict[str, str]:
    return dict(parse_qsl(entry["query"]))


def drawn(page) -> None:
    """The list holds an answer rather than the Loading line a filter
    change puts up while it asks."""
    expect(page.locator("#joblist")).not_to_have_text("Loading…")


def refreshed(page) -> None:
    """Refresh pressed and its answer drawn: the button gives its label
    back only when the listing has landed."""
    page.locator("#refresh").click()
    expect(page.locator("#refresh")).to_have_text("Refresh")
    expect(page.locator("#refresh")).to_be_enabled()


def until(page, condition, what: str, seconds: float = 10.0) -> None:
    """Wait on the test's side, looking every 100 ms. The page's own
    wait_for_function polls on animation frames, which a paused clock stops."""
    ends = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < ends, f"never happened: {what}"
        page.wait_for_timeout(100)


def scripted(fake, sentences: int, *, wait: float, per: float, **fields) -> str:
    """A run on the fake's own clock, queued for `wait` seconds and then
    making one two-second segment every `per` seconds. The fake's default
    (queued for 1 s) is quicker than a page that is still loading can be
    sure of seeing."""
    text = " ".join(f"This is sentence number {n} of a run the Jobs tab is watching."
                    for n in range(1, sentences + 1))
    script = {"start": time.time() + wait, "per": per, "seconds": [2.0] * sentences}
    return fake.add_job(text=text, _script=script, **fields)["id"]


def running(fake, **fields) -> str:
    """A run that stays running until something stops it."""
    now = time.time()
    return fake.add_job(scripted=False, status="running", created_at=now, started_at=now, **fields)["id"]


# What a finished run rings and what it notifies, recorded. Notification keeps
# the browser's own permission, so a page whose context was not granted it is
# still refused; AudioContext keeps making real (muted) sound.
SPIES = """(() => {
  window.__notes = [];
  window.__chimes = [];
  const Native = window.Notification;
  if (Native) {
    const Spy = function (title, options) { window.__notes.push({ title, body: options && options.body }); };
    Object.defineProperty(Spy, "permission", { configurable: true, get: () => Native.permission });
    Spy.requestPermission = (...args) => Native.requestPermission(...args);
    window.Notification = Spy;
  }
  const Sound = window.AudioContext;
  window.AudioContext = class extends Sound {
    createOscillator() { const o = super.createOscillator(); window.__chimes.push(o); return o; }
  };
})();"""


PERMISSION = "() => [typeof Notification, window.Notification && Notification.permission]"


def scale(transform: str) -> float:
    """The fill of a progress bar, from its scaleX()."""
    return float(re.fullmatch(r"scaleX\(([\d.]+)\)", transform).group(1))


def chimes(page) -> list[float]:
    return page.evaluate("() => window.__chimes.map(o => o.frequency.value)")


# ---- what the tab opens on and asks for ------------------------------------------------


def test_the_tab_opens_on_every_run_and_asks_for_no_filter(page, goto, stack, browser_log):
    """Playable was the default, and on a stack whose audio had been swept it
    opened on "No playable runs. 5000 records are hidden by this filter." The
    tab now opens on Everything: the first listing names no audio state, and
    a run whose audio was deleted is on the list from the start."""
    deleted = seeded_job(stack, "clone", "done", "deleted")
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#jobfilter")).to_have_value("all")
    expect(job_row(page, deleted)).to_be_visible()
    assert listings(browser_log), "the tab never asked for its runs"
    assert "audio=" not in listings(browser_log)[0]["query"], \
        f"the first listing is filtered: {listings(browser_log)[0]['query']}"
    assert page.evaluate("location.pathname + location.search") == "/ui/jobs"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("show", list(FILTER_QUERY))
def test_each_filter_and_kind_is_a_new_request_and_is_remembered(page, goto, stack, browser_log, show, kind):
    """Both selects are questions for tts-long, never a pass over the rows
    already here: the list shown is exactly what the service answered for
    that query. The choice outlives the page, and the bare address then
    opens on it and says it."""
    start = "/ui/jobs" if (show, kind) == ("failed", "speech") else "/ui/jobs?show=failed&kind=speech"
    wanted = FILTER_QUERY[show] | ({} if kind == "all" else {"kind": kind})
    named = [f"show={show}"] * (show != "all") + [f"kind={kind}"] * (kind != "all")
    address = "/ui/jobs" + ("?" + "&".join(named) if named else "")
    goto(start)
    settled(page)
    before = len(listings(browser_log))
    page.locator("#jobkind").select_option(kind)
    page.locator("#jobfilter").select_option(show)
    wait_for_address(page, address)
    drawn(page)
    asked = listings(browser_log)[before:]
    assert asked, "changing the filter asked for nothing"
    assert query(asked[-1]) == wanted, f"asked {asked[-1]['query']!r} for {show}/{kind}"
    answer = stack.api.get("/jobs", params=wanted).json()["jobs"]
    shown = page.locator("#joblist .job").evaluate_all("rows => rows.map(r => r.dataset.job)")
    assert sorted(shown) == sorted(j["id"] for j in answer), "the rows are not the service's answer"

    mark = len(listings(browser_log))
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#jobfilter")).to_have_value(show)
    expect(page.locator("#jobkind")).to_have_value(kind)
    wait_for_address(page, address)
    after = listings(browser_log)[mark:]
    assert after and query(after[0]) == wanted, "the remembered filter was not the first question asked"


def test_a_filtered_listing_is_asked_through_the_pages_own_door(page, goto, browser_log):
    goto("/ui/jobs?show=failed&kind=clone")
    settled(page)
    asked = listings(browser_log)
    assert asked and all(r["path"] == "/jobs" for r in asked), [(r["path"], r["query"]) for r in asked]


@pytest.mark.parametrize("shape", ["flat", "grouped", "absent"])
def test_filter_options_show_counts_except_playable_and_failures(page, goto, stack, fake, shape):
    """Each option says how many records it would show, from the counts the
    listing carries over every record, flat or grouped by what the filter
    names. Playable and Failures each cover two states the service counts
    apart, so they say no number rather than one this page added up; and a
    listing with no counts says none at all."""
    answer = stack.api.get("/jobs").json()
    counts = answer["counts"]
    if shape == "grouped":
        answer["counts"] = {"all": counts["all"],
                            "audio": {k: counts[k] for k in ("present", "deleted", "expired", "never", "pending")},
                            "kind": {k: counts[k] for k in ("clone", "speech", "transcribe")}}
    elif shape == "absent":
        del answer["counts"]
    if shape != "flat":
        fake.fail(r"^/jobs$", status=200, method="GET", backend="tts_long", json_body=answer)
    goto("/ui/jobs")
    settled(page)

    def n(name: str) -> str:
        return "" if shape == "absent" else f" ({counts[name]})"

    expect(page.locator("#jobfilter option")).to_have_text(
        [f"Everything{n('all')}", "Playable", f"Audio deleted{n('deleted')}",
         f"Audio expired{n('expired')}", "Failures"])
    expect(page.locator("#jobkind option")).to_have_text(
        [f"All kinds{n('all')}", f"Cloned voices{n('clone')}", f"Speech{n('speech')}",
         f"Transcriptions{n('transcribe')}"])
    if shape == "flat":
        stack.api.delete(f"/jobs/{seeded_job(stack, 'speech', 'done')}")
        refreshed(page)
        expect(page.locator("#jobfilter option").first).to_have_text(f"Everything ({counts['all'] - 1})")
        expect(page.locator("#jobkind option").nth(2)).to_have_text("Speech (0)")


def test_a_stored_filter_this_page_does_not_know_falls_back_to_everything(page, goto, browser_log):
    """A value from an older build, or one typed into storage by hand, is
    not sent as a query nothing answers."""
    page.add_init_script("""localStorage.setItem("aiv.jobfilter", JSON.stringify("swept-last-week"));
                            localStorage.setItem("aiv.jobkind", JSON.stringify("podcasts"));""")
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#jobfilter")).to_have_value("all")
    expect(page.locator("#jobkind")).to_have_value("all")
    assert listings(browser_log)[0]["query"] == ""
    assert page.evaluate("location.pathname + location.search") == "/ui/jobs"


def test_an_address_with_filters_this_page_does_not_know_opens_everything_and_drops_them(page, goto,
                                                                                        browser_log):
    goto("/ui/jobs?show=bogus&kind=nonsense")
    wait_for_address(page, "/ui/jobs")
    settled(page)
    expect(page.locator("#jobfilter")).to_have_value("all")
    expect(page.locator("#jobkind")).to_have_value("all")
    assert all(entry["query"] == "" for entry in listings(browser_log))


# ---- empty, loading and failing --------------------------------------------------------


def test_a_filter_that_hides_every_run_offers_to_show_everything(page, goto):
    """No transcription keeps audio, so none can have had it swept: the list
    is empty, says how many records the filter hides, and ends with the press
    that undoes it. The focus lands on the filter that press changed."""
    goto("/ui/jobs?show=expired&kind=transcribe")
    settled(page)
    empty = page.locator("#joblist .hint")
    expect(empty).to_contain_text("No runs whose audio expired among transcriptions.")
    expect(empty).to_contain_text("hidden by this filter.")
    expect(page.locator("#joblist .job")).to_have_count(0)
    page.locator("#jobsemptyall").click()
    expect(page.locator("#jobfilter")).to_have_value("all")
    expect(page.locator("#jobkind")).to_have_value("all")
    wait_for_address(page, "/ui/jobs")
    expect(page.locator("#joblist .job").first).to_be_visible()
    expect(page.locator("#jobfilter")).to_be_focused()


def test_an_empty_filter_says_how_many_records_it_hides(page, goto, stack):
    """The count is of every record, and one record is said as one."""
    for job in stack.api.get("/jobs").json()["jobs"]:
        if job["kind"] != "transcribe":
            stack.api.delete(f"/jobs/{job['id']}")
    goto("/ui/jobs?show=failed")
    settled(page)
    expect(page.locator("#joblist .hint")).to_have_text(
        "No failed runs. 1 record is hidden by this filter. Show everything")
    page.locator("#jobkind").select_option("speech")
    drawn(page)
    expect(page.locator("#joblist .hint")).to_have_text(
        "No failed runs among speech. 1 record is hidden by this filter. Show everything")


def test_a_service_with_no_runs_says_what_will_land_here(page, goto, stack):
    """Nothing is filtered and nothing is there: no count, no way out of a
    filter, and nothing left in the title."""
    for job in stack.api.get("/jobs").json()["jobs"]:
        stack.api.delete(f"/jobs/{job['id']}")
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#joblist")).to_have_text(
        "No runs yet. Cloned voices, instant speech and transcriptions all land here.")
    expect(page.locator("#jobsemptyall")).to_have_count(0)
    expect(page).to_have_title("Jobs · Calliope")


def test_changing_a_filter_takes_the_old_rows_away_while_the_new_list_is_asked_for(page, goto, fake, stack):
    """The previous answer is not left under a label that contradicts it:
    the list says it is loading until the new one lands."""
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#joblist .job")).to_have_count(5)
    fake.fail(r"^/jobs$", status=None, delay=1.5, times=1, method="GET", backend="tts_long")
    page.locator("#jobfilter").select_option("failed")
    expect(page.locator("#joblist")).to_have_text("Loading…")
    expect(page.locator("#joblist .job")).to_have_count(0)
    expect(job_row(page, seeded_job(stack, "clone", "failed"))).to_be_visible(timeout=5_000)


def test_refresh_shows_it_is_working_and_asks_again(page, goto, fake, browser_log):
    """The press is the acknowledgement: an unchanged list redrawn the same
    used to make Refresh look as if it had done nothing."""
    goto("/ui/jobs")
    settled(page)
    before = len(listings(browser_log))
    fake.fail(r"^/jobs$", status=None, delay=1.5, times=1, method="GET", backend="tts_long")
    refresh = page.locator("#refresh")
    refresh.click()
    expect(refresh).to_have_text("Refreshing…")
    expect(refresh).to_be_disabled()
    expect(refresh).to_have_attribute("aria-busy", "true")
    expect(refresh).to_have_text("Refresh", timeout=5_000)
    expect(refresh).to_be_enabled()
    expect(refresh).not_to_have_attribute("aria-busy", "true")
    assert len(listings(browser_log)) == before + 1


def test_a_job_list_that_fails_keeps_the_last_rows_on_screen(page, goto, fake, browser_log):
    """A 503 while tts-long restarts must not empty the list: the runs are
    still there on the other side of it."""
    browser_log.allow(503, LISTING)
    goto("/ui/jobs")
    settled(page)
    shown = page.locator("#joblist .job").evaluate_all("rows => rows.map(r => r.dataset.job)")
    assert len(shown) == 5
    fake.fail(r"^/jobs$", status=503, method="GET", backend="tts_long")
    refreshed(page)
    assert any(r["status"] == 503 and re.search(LISTING, r["path"]) for r in browser_log.responses)
    assert page.locator("#joblist .job").evaluate_all("rows => rows.map(r => r.dataset.job)") == shown
    expect(page.locator("#joblist > .note.bad")).to_have_count(0)


def test_a_job_list_that_fails_at_load_does_not_claim_there_are_no_runs(page, goto, fake, browser_log):
    browser_log.allow(503, LISTING)
    fake.fail(r"^/jobs$", status=503, method="GET", backend="tts_long")
    goto("/ui/jobs")
    settled(page)
    until(page, lambda: any(r["status"] == 503 for r in browser_log.responses), "the failed listing")
    expect(page.locator("#joblist")).not_to_be_empty()
    expect(page.locator("#joblist")).not_to_contain_text("No runs yet")


def test_a_truncated_listing_says_older_runs_need_narrower_filters(page, goto, stack, fake):
    answer = stack.api.get("/jobs").json() | {"truncated": True}
    fake.fail(r"^/jobs$", status=200, method="GET", backend="tts_long", json_body=answer)
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#joblist > .hint").last).to_have_text(
        "The service sent the most recent runs only. Narrow the filters to reach the older ones.")


# ---- what a row says -------------------------------------------------------------------


def test_each_row_says_what_ran_where_and_how_fast(page, goto, stack, fake):
    """The voice or the transcript's language, the pieces in the word the
    engine uses, the measurements, and where it ran with why that engine.
    Every value from the service is text, never markup."""
    clone = seeded_job(stack, "clone", "done", "present")
    local = fake.add_job(scripted=False, status="done", kind="speech", service="tts-stack", engine="kokoro",
                         voice="af_heart", backend="local", host="nas", engine_reason="pinned", chunks=1,
                         audio_seconds=3.2, speech_seconds=3.2, compute_seconds=0.29,
                         realtime_factor=11.03)["id"]
    heard = fake.add_job(scripted=False, status="done", kind="transcribe", service="stt-stack",
                         engine="parakeet", voice=None, language="pt", backend="local", host="nas",
                         engine_reason="alias:openai", chunks=1, chars=12, audio_seconds=5.0,
                         speech_seconds=4.1, compute_seconds=0.3, realtime_factor=16.67)["id"]
    nowhere = fake.add_job(scripted=False, status="failed", engine="voxtral", backend=None,
                           engine_reason="weird:reason", error="voxtral has no lane on this server")["id"]
    fell = fake.add_job(scripted=False, status="done", backend="local", fell_back=True, host="nas",
                        runner_host="desk")["id"]
    mute = fake.add_job(scripted=False, status="failed", error="")["id"]
    markup = fake.add_job(scripted=False, status="done", voice="<img src=x onerror=window.__pwned=1>")["id"]
    goto("/ui/jobs")
    settled(page)

    row = job_row(page, clone)
    expect(row.locator(".top strong")).to_have_text("narrator")
    expect(row.locator(".top .id")).to_have_text(clone[:8])
    expect(row.locator(".top .id")).to_have_attribute("title", clone)
    expect(row.locator(".top .hint")).to_have_text("2 segments")
    expect(row).to_contain_text("GPU e2e-gpu · Chatterbox (default)")
    expect(row).to_contain_text("42 s of audio · 42 s of speech · 61 s of compute · 0.70× realtime")

    row = job_row(page, local)
    expect(row.locator(".top .hint")).to_have_text("1 segment")
    expect(row).to_contain_text("nas · Kokoro (asked for by name)")
    expect(row).to_contain_text("3.2 s of audio · 3.2 s of speech · 0.29 s of compute · 11.0× faster than realtime")

    row = job_row(page, heard)
    expect(row.locator(".top strong")).to_have_text("Transcript · pt")
    expect(row.locator(".top .hint")).to_have_text("")
    expect(row).to_contain_text("nas · Parakeet (via an OpenAI model name)")
    expect(row).to_contain_text(
        "5 s of audio · 4.1 s of speech · 0.3 s of compute · 16.7× faster than realtime · 12 characters")

    expect(job_row(page, nowhere).locator(".note.bad")).to_have_text("voxtral has no lane on this server")
    expect(job_row(page, nowhere)).to_contain_text("Voxtral (weird:reason)")
    expect(job_row(page, nowhere)).not_to_contain_text("e2e-fake")
    expect(job_row(page, fell)).to_contain_text("nas, after desk gave up")
    expect(job_row(page, mute).locator(".note.bad")).to_have_text("no reason given")

    expect(job_row(page, markup).locator(".top strong")).to_have_text("<img src=x onerror=window.__pwned=1>")
    expect(job_row(page, markup).locator("img")).to_have_count(0)
    assert page.evaluate("() => window.__pwned") is None


def test_a_row_whose_text_was_not_kept_says_so(page, goto, fake):
    """A run recovered from disk after a restart kept neither its text nor
    its voice; a run whose preview outlived its text says that when opened."""
    recovered = fake.add_job(scripted=False, status="done", text="", voice=None, recovered=True)["id"]
    lost = fake.add_job(scripted=False, status="done", text="", text_preview="A preview the record outlived")["id"]
    goto("/ui/jobs")
    settled(page)
    expect(job_row(page, recovered).locator(".top strong")).to_have_text("voice unknown")
    expect(job_row(page, recovered)).to_contain_text(
        "The server recovered this job after a restart. It did not keep the text or the voice.")
    expect(job_row(page, recovered).locator("details.jobtext")).to_have_count(0)
    job_row(page, lost).locator("details.jobtext > summary").click()
    expect(job_row(page, lost).locator("[data-text]")).to_have_text("The text was not kept for this job.")


def test_each_row_offers_the_actions_its_kind_and_state_allow(page, goto, stack, fake):
    """Audio to fetch gives Play and Download; a tts-long run that failed
    gives Retry; a Kokoro run, which keeps no file, gives Speak again; a
    transcription gives its text; a live run gives Stop. The last button
    names what it deletes."""
    present = seeded_job(stack, "clone", "done", "present")
    deleted = seeded_job(stack, "clone", "done", "deleted")
    failed = seeded_job(stack, "clone", "failed")
    kokoro = seeded_job(stack, "speech", "done")
    heard = seeded_job(stack, "transcribe", "done")
    preset = fake.add_job(scripted=False, status="failed", kind="speech", engine="voxtral", voice="Paul")["id"]
    preset_done = fake.add_job(scripted=False, status="done", kind="speech", engine="voxtral", voice="Paul",
                               path="/out/preset.wav", bytes=96000, audio_seconds=2.0)["id"]
    stopped = fake.add_job(scripted=False, status="cancelled", path="/out/stopped.wav", bytes=96000,
                           audio_seconds=3.0)["id"]
    live = running(fake)
    goto("/ui/jobs")
    settled(page)
    with_audio = ["Play", "Download the audio", "Delete the audio", "Delete the record too"]
    expected = {present: with_audio, deleted: ["Delete the record"], failed: ["Retry", "Delete the record"],
                kokoro: ["Speak again", "Delete the record"],
                heard: ["Copy the transcript", "Delete the transcript"],
                preset: ["Retry", "Delete the record"], preset_done: with_audio, stopped: with_audio,
                live: ["Stop and keep what's done"]}
    for job, labels in expected.items():
        expect(job_row(page, job)).to_be_visible()
        assert actions(page, job) == labels, f"{job}: {actions(page, job)}"
    expect(job_row(page, deleted)).to_contain_text("The audio was deleted. This record was kept.")
    expect(job_row(page, failed)).to_contain_text("The audio expired and was removed. This record was kept.")
    expect(job_row(page, failed).locator(".note.bad")).to_have_text(
        "the runner went away in the middle of segment 3")


# ---- a run from queued to done ---------------------------------------------------------


def test_a_queued_job_moves_to_running_and_done_with_a_measured_bar(page, goto, fake):
    """Queued, the bar is the estimate; running, it counts the segments the
    service has made; done, the bar goes and the measurements come. While it
    is live the dock counts it and the tab says the page can be closed."""
    job = scripted(fake, 3, wait=5, per=1.5)
    goto("/ui/jobs")
    settled(page)
    row = job_row(page, job)
    expect(status(page, job)).to_have_text("queued")
    expect(row.locator(".bar-track")).to_be_visible()
    expect(row).to_contain_text(re.compile(r"\d+s in, .+ left · estimate"))
    expect(row.locator("[data-stop]")).to_be_visible()
    expect(page.locator("#jobsafe")).to_have_text("Close this page if you want. The job runs on the server.")
    expect(page.locator("#jobsafe")).to_be_visible()
    expect(page.locator("#jobcount")).to_have_text("1")
    expect(page.locator("#jobcount")).to_have_class(re.compile(r"\blive\b"))

    seen: list[dict] = []

    def counted() -> bool:
        state = row.evaluate("""r => ({ status: r.querySelector(".status").textContent,
            text: r.textContent.replace(/\\s+/g, " "),
            bar: (r.querySelector(".bar-fill") || {}).style?.transform || "" })""")
        made = re.search(r"(\d) of 3 segments · .+ left · measured", state["text"])
        if state["status"] == "running" and made:
            seen.append(state | {"made": int(made.group(1))})
        return bool(seen) or state["status"] == "done"

    until(page, counted, "a running row counting its segments", seconds=15)
    assert seen, "the row went from queued to done without a count of what it had made"
    assert abs(scale(seen[0]["bar"]) - seen[0]["made"] / 3) < 1e-3, seen[0]

    expect(status(page, job)).to_have_text("done", timeout=15_000)
    expect(row.locator(".bar-track")).to_have_count(0)
    expect(row).to_contain_text("6 s of audio · 6 s of speech · 4.5 s of compute · 1.3× faster than realtime")
    assert actions(page, job)[:2] == ["Play", "Download the audio"]
    expect(page.locator("#jobsafe")).to_be_hidden()
    expect(page.locator("#jobcount")).to_have_text("")
    expect(page.locator("#jobcount")).not_to_have_class(re.compile(r"\blive\b"))


def test_a_run_past_its_estimate_says_so_rather_than_filling_the_bar(page, goto, fake):
    """Before the first segment the bar is time against the estimate, held
    under the end, and a run past it is said to be past it."""
    started = time.time() - 100
    job = fake.add_job(scripted=False, status="running", created_at=started, started_at=started,
                       estimated_seconds=30)["id"]
    goto("/ui/jobs")
    settled(page)
    row = job_row(page, job)
    expect(row).to_contain_text(re.compile(r"1m 4\ds in, past the 30s estimate · estimate"))
    assert scale(row.locator(".bar-fill").evaluate("e => e.style.transform")) == 0.95


def test_a_live_run_that_refresh_finds_is_followed_at_once(page, goto, fake):
    goto("/ui/jobs")
    settled(page)
    job = scripted(fake, 2, wait=1, per=1.0)
    refreshed(page)
    expect(status(page, job)).to_have_text(re.compile(r"^(queued|running)$"))
    expect(status(page, job)).to_have_text("done", timeout=12_000)


@pytest.mark.parametrize("permission", ["granted", "denied"])
def test_a_finished_job_chimes_and_notifies_when_allowed(new_page, goto, fake, permission):
    """A run that finishes while the page watches rings once, high for done,
    and says so as a notification only where the browser allows one."""
    page = new_page(notifications=permission)
    page.add_init_script(SPIES)
    job = scripted(fake, 2, wait=3, per=1.0)
    goto("/ui/jobs", target=page)
    settled(page)
    expect(status(page, job)).to_have_text(re.compile(r"^(queued|running)$"))
    expect(status(page, job)).to_have_text("done", timeout=15_000)
    assert chimes(page) == [880]
    notes = page.evaluate("() => window.__notes")
    assert notes == ([{"title": "Speech is ready", "body": f"narrator · {job[:8]}"}]
                     if permission == "granted" else []), page.evaluate(PERMISSION)


def test_a_live_job_lost_to_a_service_restart_is_marked_failed_with_the_reason(new_page, goto, fake):
    """This browser queued it and saw it running; tts-long restarted and its
    listing no longer has it. The work died with the process, so the row
    says failed and why, offers Retry, rings low and notifies, and stays."""
    page = new_page(notifications="granted")
    page.add_init_script(SPIES)
    job = running(fake)
    goto("/ui/jobs", target=page)
    settled(page)
    expect(status(page, job)).to_have_text("running")
    page.evaluate("id => adopt(id, null)", job)
    fake.reset()
    refreshed(page)
    expect(status(page, job)).to_have_text("failed")
    expect(job_row(page, job).locator(".note.bad")).to_have_text("lost when the service restarted")
    assert actions(page, job) == ["Retry", "Delete the record"]
    assert page.evaluate("() => window.__notes") == [{"title": "A job failed", "body": f"narrator · {job[:8]}"}], \
        page.evaluate(PERMISSION)
    assert chimes(page) == [300]
    refreshed(page)
    expect(status(page, job)).to_have_text("failed")


# ---- a row's text ----------------------------------------------------------------------


def test_opening_a_rows_text_loads_it_once_and_keeps_it_open_across_polls(page, goto, stack, browser_log):
    job = seeded_job(stack, "clone", "done", "present")
    text = record(stack, job)["text"]
    goto("/ui/jobs")
    settled(page)
    row = job_row(page, job)
    expect(row.locator("details.jobtext > summary")).to_have_text(text if len(text) <= 140 else text[:140] + "…")
    row.locator("details.jobtext > summary").click()
    expect(row.locator("[data-text]")).to_have_text(text)
    for _ in range(2):
        refreshed(page)
        expect(row.locator("details.jobtext")).to_have_attribute("open", "")
        expect(row.locator("[data-text]")).to_have_text(text)
    row.locator("details.jobtext > summary").click()
    expect(row.locator("details.jobtext")).not_to_have_attribute("open", "")
    row.locator("details.jobtext > summary").click()
    expect(row.locator("[data-text]")).to_have_text(text)
    assert len(browser_log.sent("GET", rf"^/jobs/{job}$")) == 1


def test_a_rows_text_that_cannot_be_loaded_says_why_and_is_asked_for_again(page, goto, stack, fake,
                                                                          browser_log):
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}$", status=503, method="GET", backend="tts_long", times=1,
              json_body={"detail": "the job store is being rebuilt"})
    browser_log.allow(503, rf"^/jobs/{job}$")
    summary = job_row(page, job).locator("details.jobtext > summary")
    summary.click()
    expect(job_row(page, job).locator("[data-text]")).to_have_text("Could not load it: the job store is being rebuilt")
    summary.click()
    summary.click()
    expect(job_row(page, job).locator("[data-text]")).to_have_text(record(stack, job)["text"])
    assert len(browser_log.sent("GET", rf"^/jobs/{job}$")) == 2


def test_opening_a_rows_text_puts_the_job_in_the_address_and_closing_it_goes_back(page, goto, stack):
    """The open row is a place: a step in the history, under the filter the
    list is showing, named in the title. Closing it is the step back."""
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs?show=playable")
    settled(page)
    before = history_length(page)
    summary = job_row(page, job).locator("details.jobtext > summary")
    summary.click()
    wait_for_address(page, f"/ui/jobs/{job}?show=playable")
    assert history_length(page) == before + 1
    expect(page).to_have_title(f"Job {job[:8]} · Jobs · Calliope")
    summary.click()
    wait_for_address(page, "/ui/jobs?show=playable")
    expect(page).to_have_title("Jobs · Calliope")
    expect(job_row(page, job).locator("details.jobtext")).not_to_have_attribute("open", "")


def test_a_job_address_under_a_filter_that_holds_it_keeps_the_filter(page, goto, stack):
    job = seeded_job(stack, "clone", "failed")
    goto(f"/ui/jobs/{job}?show=failed")
    settled(page)
    row = job_row(page, job)
    expect(row).to_have_class(re.compile(r"\bhere\b"))
    expect(row).to_have_attribute("aria-current", "true")
    expect(row).to_be_focused()
    expect(page.locator("#jobfilter")).to_have_value("failed")
    assert page.evaluate("location.pathname + location.search") == f"/ui/jobs/{job}?show=failed"
    expect(page.locator("#jobnote")).to_have_text("")


def test_a_job_address_the_service_cannot_answer_for_keeps_the_link_and_says_nothing(page, goto, stack, fake,
                                                                                    browser_log):
    """The listing and the job's own record both fail: that the job is gone
    is not known, so the link stays and no note claims anything."""
    job = seeded_job(stack, "clone", "done", "present")
    fake.fail(r"^/jobs", status=503, method="GET", backend="tts_long")
    browser_log.allow(503, r"^/jobs")
    goto(f"/ui/jobs/{job}")
    until(page, lambda: any(r["status"] == 503 and r["path"] == f"/jobs/{job}"
                            for r in browser_log.responses), "the job's own record asked for")
    page.wait_for_timeout(300)
    assert page.evaluate("location.pathname + location.search") == f"/ui/jobs/{job}"
    expect(page.locator("#jobnote")).to_have_text("")


# ---- the player ------------------------------------------------------------------------


def test_play_loads_the_audio_into_the_one_player_and_starts_it(page, goto, stack, browser_log):
    """One player outside the list, so the rows can be redrawn under it
    without restarting it; its speed is the page's shared playback speed."""
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#jobplay")).to_be_hidden()
    job_row(page, job).locator("[data-play]").click()
    expect(page.locator("#jobplay")).to_be_visible()
    expect(page.locator("#jobplaying")).to_have_text(f"Playing {job[:8]}")
    playing = "() => { const p = document.getElementById('jobplayer'); " \
              "return p.src.startsWith('blob:') && !p.paused && p.currentTime > 0; }"
    page.wait_for_function(playing)
    source = page.locator("#jobplayer").evaluate("p => p.src")
    page.locator("#jobrate").select_option("1.5")
    assert page.locator("#jobplayer").evaluate("p => p.playbackRate") == 1.5
    refreshed(page)
    assert page.locator("#jobplayer").evaluate("p => p.src") == source
    page.wait_for_function(playing)
    assert len(browser_log.sent("GET", rf"^/jobs/{job}/audio$")) == 1


def test_close_the_player_stops_and_hides_it(page, goto, stack):
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-play]").click()
    page.wait_for_function("() => !document.getElementById('jobplayer').paused")
    page.locator("#jobclose").click()
    expect(page.locator("#jobplay")).to_be_hidden()
    assert page.locator("#jobplayer").evaluate("p => [p.paused, p.hasAttribute('src')]") == [True, False]


def test_the_quieter_engines_loudness_note_is_shown_once(page, goto, stack, fake):
    """Turbo normalises 5 dB under the default engine, which sounds like a
    fault; the player says so the first time it plays one, and never again
    in this browser. The figures are the service's, from /health."""
    engines = stack.fake.backend_health("tts_long")["engines"]
    engines["chatterbox"]["loudness_lufs"] = -22
    engines["chatterbox-turbo"]["loudness_lufs"] = -27
    fake.health("tts_long", engines=engines)
    base = seeded_job(stack, "clone", "done", "present")
    turbo = fake.add_job(scripted=False, status="done", engine="chatterbox-turbo", path="/out/turbo.wav",
                         bytes=96000, audio_seconds=2.0)["id"]
    goto("/ui/jobs")
    settled(page)
    loud = page.locator("#jobloud")
    job_row(page, base).locator("[data-play]").click()
    expect(page.locator("#jobplaying")).to_have_text(f"Playing {base[:8]}")
    expect(loud).to_be_hidden()
    job_row(page, turbo).locator("[data-play]").click()
    expect(loud).to_have_text("Chatterbox Turbo normalises to -27 LUFS, about 5 dB quieter than Chatterbox. "
                              "That is the engine and not the run. Turn it up rather than changing the text.")
    expect(loud).to_be_visible()
    job_row(page, base).locator("[data-play]").click()
    expect(loud).to_be_hidden()
    page.reload()
    settled(page)
    job_row(page, turbo).locator("[data-play]").click()
    expect(page.locator("#jobplaying")).to_have_text(f"Playing {turbo[:8]}")
    expect(loud).to_be_hidden()


@pytest.mark.parametrize(("failure", "said"), [
    (404, "That audio is no longer on the server."),
    (409, "That audio is not ready yet."),
    (500, "The audio could not be fetched."),
    ("unreachable", "Could not load it: " + UNREACHABLE)])
def test_play_on_audio_that_is_gone_says_it_is_not_available(page, goto, stack, fake, browser_log, failure, said):
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    if failure == "unreachable":
        page.route(re.compile(rf"/jobs/{job}/audio$"), lambda route: route.abort("aborted"))
    else:
        fake.fail(rf"^/jobs/{job}/audio$", status=failure, method="GET", backend="tts_long")
        browser_log.allow(failure, rf"^/jobs/{job}/audio$")
    job_row(page, job).locator("[data-play]").click()
    expect(page.locator("#jobplaying")).to_have_text(said)
    assert not page.locator("#jobplayer").evaluate("p => p.hasAttribute('src')")


# ---- download --------------------------------------------------------------------------


def test_download_the_audio_saves_a_wav_named_after_the_job(page, goto, stack, fake, browser_log):
    """The audio is fetched through the page (the route needs the key the
    server adds) and handed over as a file named after the job. The press
    says it is fetching while it waits."""
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}/audio$", status=None, delay=1.0, times=1, method="GET", backend="tts_long")
    button = job_row(page, job).locator("[data-get]")
    with page.expect_download() as info:
        button.click()
        expect(button).to_have_text("Fetching…")
        expect(button).to_be_disabled()
    download = info.value
    assert download.suggested_filename == f"{job}.wav"
    assert Path(download.path()).read_bytes()[:4] == b"RIFF"
    expect(button).to_have_text("Download the audio")
    assert len(browser_log.sent("GET", rf"^/jobs/{job}/audio$")) == 1


@pytest.mark.parametrize(("failure", "said"), [
    (409, "That audio is not ready yet."),
    (404, "That audio is no longer on the server."),
    (500, "The audio could not be fetched.")])
def test_download_on_audio_not_ready_says_so_in_words(page, goto, stack, fake, dialogs, browser_log, failure, said):
    """The alert read "Not ready: 409". It says what the answer means for a
    job, and never the number."""
    job = seeded_job(stack, "clone", "done", "present")
    dialogs()
    fake.fail(rf"^/jobs/{job}/audio$", status=failure, times=1, backend="tts_long")
    browser_log.allow(failure, rf"^/jobs/{job}/audio$")
    goto(f"/ui/jobs/{job}")
    settled(page)
    job_row(page, job).locator("[data-get]").click()
    page.wait_for_function("() => !document.querySelector('[data-get][aria-busy]')")
    assert dialogs.seen == [("alert", said)], dialogs.seen


def test_download_while_the_server_cannot_be_reached_says_so(page, goto, stack, dialogs, browser_log):
    job = seeded_job(stack, "clone", "done", "present")
    dialogs()
    goto("/ui/jobs")
    settled(page)
    page.route(re.compile(rf"/jobs/{job}/audio$"), lambda route: route.abort("aborted"))
    job_row(page, job).locator("[data-get]").click()
    page.wait_for_function("() => !document.querySelector('[data-get][aria-busy]')")
    assert [message for _, message in dialogs.seen] and UNREACHABLE in dialogs.seen[-1][1], dialogs.seen
    assert not browser_log.errors, browser_log.errors


# ---- stop ------------------------------------------------------------------------------


def test_stop_and_keep_whats_done_marks_the_row_stopping_then_cancelled(page, goto, fake, browser_log):
    """The row says "stopping…" at the press, never "stopped" before the
    service has; then cancelled, with the part already made to play."""
    job = running(fake, offsets=[0.0, 2.0])
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}$", status=None, delay=1.0, times=1, method="DELETE", backend="tts_long")
    job_row(page, job).locator("[data-stop]").click()
    expect(status(page, job)).to_have_text("stopping…")
    expect(job_row(page, job).locator("[data-stop]")).to_have_count(0)
    expect(status(page, job)).to_have_text("cancelled", timeout=5_000)
    assert len(browser_log.sent("DELETE", rf"^/jobs/{job}$")) == 1
    assert actions(page, job) == ["Play", "Download the audio", "Delete the audio", "Delete the record too"]


def test_a_stop_the_service_refuses_puts_the_row_back_and_says_why(page, goto, fake, dialogs, browser_log):
    job = running(fake)
    dialogs()
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}$", status=409, method="DELETE", backend="tts_long", times=1,
              json_body={"detail": "that job is finishing and cannot be stopped now"})
    browser_log.allow(409, rf"^/jobs/{job}$")
    job_row(page, job).locator("[data-stop]").click()
    until(page, lambda: dialogs.seen, "the refusal said")
    assert dialogs.seen == [("alert", "Could not stop it: that job is finishing and cannot be stopped now")]
    expect(status(page, job)).to_have_text("running")
    expect(job_row(page, job).locator("[data-stop]")).to_have_text("Stop and keep what's done")


# ---- retry, speak again, copy ----------------------------------------------------------


def failed_clone(fake) -> str:
    return fake.add_job(scripted=False, status="failed", text="First sentence here. Second one there.",
                        voice="narrator", engine="chatterbox", language="en", exaggeration=0.7,
                        cfg_weight=0.3, temperature=0.9, error="made for this test")["id"]


def test_retry_resubmits_a_failed_clone_with_its_engine_and_controls(page, goto, fake, browser_log):
    """The parameters come from the service's record, not this browser, so
    Retry works on any device; the engine and the controls it declares go
    back as they were, and the press says it is retrying while it waits."""
    job = failed_clone(fake)
    goto("/ui/jobs")
    settled(page)
    fake.fail(r"^/jobs$", status=None, delay=1.0, times=1, method="POST", backend="tts_long")
    button = job_row(page, job).locator("[data-retry]")
    button.click()
    expect(button).to_have_text("Retrying…")
    expect(button).to_be_disabled()
    until(page, lambda: browser_log.sent("POST", LISTING), "the resubmission")
    sent = browser_log.sent("POST", LISTING)[0]["json"]
    assert {k: sent.get(k) for k in ("voice", "model", "exaggeration", "cfg_weight", "temperature", "language")} \
        == {"voice": "narrator", "model": "chatterbox", "exaggeration": 0.7, "cfg_weight": 0.3,
            "temperature": 0.9, "language": "en"}, sent
    assert len(browser_log.sent("GET", rf"^/jobs/{job}$")) == 1


def test_retry_runs_the_same_text_again(page, goto, stack, fake, dialogs, browser_log):
    job = failed_clone(fake)
    dialogs()
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-retry]").click()
    until(page, lambda: browser_log.sent("POST", LISTING) or dialogs.seen, "the resubmission")
    sent = browser_log.sent("POST", LISTING)[0]["json"]
    said = sent.get("text") or " ".join(s.get("text") or "" for s in sent.get("segments") or [])
    assert said == record(stack, job)["text"], sent
    expect(page.locator("#joblist .job")).to_have_count(7)
    assert dialogs.seen == []


def test_retry_falls_back_on_what_this_browser_sent_when_the_record_is_gone(page, goto, stack, fake,
                                                                           browser_log):
    """The service swept the record of a run this browser queued. What this
    browser kept of the request is then the only copy, and Retry sends it."""
    browser_log.allow(404, r"^/jobs/[0-9a-f-]+$")
    job = failed_clone(fake)
    params = {"voice": "narrator", "text": "Kept by this browser.", "model": "chatterbox"}
    goto("/ui/jobs")
    settled(page)
    page.evaluate("([id, params]) => adopt(id, params)", [job, params])
    stack.api.delete(f"/jobs/{job}")
    refreshed(page)
    expect(job_row(page, job)).to_be_visible()
    job_row(page, job).locator("[data-retry]").click()
    until(page, lambda: browser_log.sent("POST", LISTING), "the resubmission")
    sent = browser_log.sent("POST", LISTING)[0]["json"]
    assert sent["voice"] == "narrator" and sent["text"] == "Kept by this browser." and sent["model"] == "chatterbox"
    expect(page.locator("#joblist .job")).to_have_count(7)


def test_a_retry_the_service_refuses_says_why(page, goto, fake, dialogs, browser_log):
    job = failed_clone(fake)
    dialogs()
    goto("/ui/jobs")
    settled(page)
    fake.fail(r"^/jobs$", status=503, method="POST", backend="tts_long",
              json_body={"detail": "the queue is closed for maintenance"})
    browser_log.allow(503, LISTING)
    job_row(page, job).locator("[data-retry]").click()
    until(page, lambda: dialogs.seen, "the refusal said")
    assert dialogs.seen == [("alert", "Retry failed: the queue is closed for maintenance")]
    expect(job_row(page, job).locator("[data-retry]")).to_have_text("Retry")
    expect(job_row(page, job).locator("[data-retry]")).to_be_enabled()


def test_speak_again_says_a_kokoro_run_through_speak_and_plays_it(page, goto, stack, fake, browser_log):
    """Kokoro keeps no file, so the run is made again from its record, with
    only the fields /speak declares, and played in the tab's one player."""
    job = seeded_job(stack, "speech", "done")
    text = record(stack, job)["text"]
    goto("/ui/jobs")
    settled(page)
    before = len(listings(browser_log))
    job_row(page, job).locator("[data-again]").click()
    expect(page.locator("#jobplaying")).to_have_text(f"Speaking {job[:8]} again")
    page.wait_for_function("() => document.getElementById('jobplayer').src.startsWith('blob:')")
    reached = fake.requests(backend="tts", method="POST", path=r"^/speak$")
    assert [r["json"] for r in reached] == [{"voice": "af_heart", "text": text, "format": "wav", "language": "en"}]
    until(page, lambda: len(listings(browser_log)) > before, "the list asked for again")


def test_speak_again_on_a_run_whose_text_was_not_kept_says_so(page, goto, fake, dialogs, browser_log):
    job = fake.add_job(scripted=False, status="done", kind="speech", service="tts-stack", engine="kokoro",
                       voice="af_heart", text="")["id"]
    dialogs()
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-again]").click()
    until(page, lambda: dialogs.seen, "the refusal said")
    assert dialogs.seen == [("alert", "The text for that run was not kept, so it cannot be said again.")]
    assert not browser_log.sent("POST", r"^/speak$")


def test_copy_the_transcript_puts_it_on_the_clipboard(page, goto, stack, browser_log):
    """The transcript is the whole of what the run made, so it can leave the
    tab; a row already opened costs no second request."""
    job = seeded_job(stack, "transcribe", "done")
    text = record(stack, job)["text"]
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("details.jobtext > summary").click()
    expect(job_row(page, job).locator("[data-text]")).to_have_text(text)
    copy = job_row(page, job).locator("[data-copy]")
    copy.click()
    expect(copy).to_have_text("Copied")
    assert page.evaluate("() => navigator.clipboard.readText()") == text
    expect(copy).to_have_text("Copy the transcript")
    assert len(browser_log.sent("GET", rf"^/jobs/{job}$")) == 1


def test_copy_on_a_transcript_that_was_not_kept_says_so(page, goto, fake, dialogs):
    job = fake.add_job(scripted=False, status="done", kind="transcribe", service="stt-stack", engine="parakeet",
                       voice=None, text="")["id"]
    dialogs()
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-copy]").click()
    until(page, lambda: dialogs.seen, "the refusal said")
    assert dialogs.seen == [("alert", "The transcript was not kept for that run.")]


# ---- delete ----------------------------------------------------------------------------


def test_delete_the_audio_asks_and_keeps_the_record(page, goto, stack, dialogs, browser_log):
    """The audio is the only copy, so the press asks; No changes nothing,
    Yes frees the disk and the row stays, saying its audio was deleted."""
    job = seeded_job(stack, "clone", "done", "present")
    answers = [False, True]
    dialogs(answer=lambda dialog: answers.pop(0))
    goto("/ui/jobs")
    settled(page)
    asked = (f"Delete the audio for job {job[:8]}?\n\nThe record stays: what was said, which voice, how long "
             "it took and where it ran. The audio cannot be recovered.")
    job_row(page, job).locator("[data-delaudio]").click()
    until(page, lambda: dialogs.seen, "the question")
    assert dialogs.seen == [("confirm", asked)]
    page.wait_for_timeout(300)
    assert not browser_log.sent("DELETE", r"^/jobs/")
    assert actions(page, job)[0] == "Play"
    job_row(page, job).locator("[data-delaudio]").click()
    expect(job_row(page, job)).to_contain_text("The audio was deleted. This record was kept.")
    assert actions(page, job) == ["Delete the record"]
    assert [r["path"] for r in browser_log.sent("DELETE", r"^/jobs/")] == [f"/jobs/{job}/audio"]
    assert record(stack, job)["audio"] == {"state": "deleted"}


def test_deleting_a_record_with_audio_asks_first(page, goto, stack, dialogs, browser_log):
    job = seeded_job(stack, "clone", "done", "present")
    answers = [False, True]
    dialogs(answer=lambda dialog: answers.pop(0))
    goto("/ui/jobs")
    settled(page)
    asked = (f"Delete the audio for job {job[:8]}?\n\nThis cannot be undone. It is deleted on the server, "
             "so it goes from every device.")
    job_row(page, job).locator("[data-forget]").click()
    until(page, lambda: dialogs.seen, "the question")
    page.wait_for_timeout(300)
    assert not browser_log.sent("DELETE", r"^/jobs/")
    expect(job_row(page, job)).to_be_visible()
    job_row(page, job).locator("[data-forget]").click()
    expect(job_row(page, job)).to_have_count(0)
    assert dialogs.seen == [("confirm", asked)] * 2
    assert [r["path"] for r in browser_log.sent("DELETE", r"^/jobs/")] == [f"/jobs/{job}"]
    assert record(stack, job) is None


def test_deleting_a_transcript_asks_first(page, goto, stack, dialogs, browser_log):
    """A transcription keeps no audio and its record is the only copy of
    what it made, so it is asked about like audio is."""
    job = seeded_job(stack, "transcribe", "done")
    answers = [False, True]
    dialogs(answer=lambda dialog: answers.pop(0))
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-forget]").click()
    until(page, lambda: dialogs.seen, "the question")
    assert dialogs.seen == [("confirm", (
        f"Delete the transcript for job {job[:8]}?\n\nThe text is all this run produced -- the recording was "
        "never kept, and it cannot be transcribed again from here. It is deleted on the server, so it goes "
        "from every device."))]
    page.wait_for_timeout(300)
    assert not browser_log.sent("DELETE", r"^/jobs/")
    job_row(page, job).locator("[data-forget]").click()
    expect(job_row(page, job)).to_have_count(0)
    assert record(stack, job) is None


@pytest.mark.parametrize("which", ["failed clone", "audio deleted", "kokoro run"])
def test_deleting_a_record_with_nothing_to_lose_does_not_ask(page, goto, stack, dialogs, browser_log, which):
    job = {"failed clone": lambda: seeded_job(stack, "clone", "failed"),
           "audio deleted": lambda: seeded_job(stack, "clone", "done", "deleted"),
           "kokoro run": lambda: seeded_job(stack, "speech", "done")}[which]()
    dialogs()
    goto("/ui/jobs")
    settled(page)
    job_row(page, job).locator("[data-forget]").click()
    expect(job_row(page, job)).to_have_count(0)
    assert dialogs.seen == []
    assert [r["path"] for r in browser_log.sent("DELETE", r"^/jobs/")] == [f"/jobs/{job}"]
    refreshed(page)
    expect(job_row(page, job)).to_have_count(0)


def test_deleting_a_record_the_service_no_longer_has_takes_the_row_off(page, goto, stack, fake, browser_log):
    """A 404 is the outcome the press asked for."""
    job = seeded_job(stack, "clone", "failed")
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}$", status=404, method="DELETE", backend="tts_long", times=1)
    browser_log.allow(404, rf"^/jobs/{job}$")
    job_row(page, job).locator("[data-forget]").click()
    expect(job_row(page, job)).to_have_count(0)


@pytest.mark.parametrize(("button", "path", "said"), [
    ("delaudio", "/audio", "Could not delete the audio: the volume is read-only"),
    ("forget", "", "Could not forget it: the volume is read-only")])
def test_a_delete_the_service_refuses_says_why_and_keeps_the_row(page, goto, stack, fake, dialogs, browser_log,
                                                                button, path, said):
    job = seeded_job(stack, "clone", "done", "present")
    dialogs()
    goto("/ui/jobs")
    settled(page)
    fake.fail(rf"^/jobs/{job}{path}$", status=503, method="DELETE", backend="tts_long",
              json_body={"detail": "the volume is read-only"})
    browser_log.allow(503, rf"^/jobs/{job}{path}$")
    job_row(page, job).locator(f"[data-{button}]").click()
    until(page, lambda: len(dialogs.seen) == 2, "the question and the refusal")
    assert dialogs.seen[1] == ("alert", said)
    expect(job_row(page, job)).to_be_visible()
    assert actions(page, job)[0] == "Play"


def test_a_live_job_offers_stop_and_not_a_delete_that_would_only_cancel_it(page, goto, fake):
    """tts-long answers DELETE on a queued or running job by cancelling it and
    keeping the record, so "Delete the record" on a live row took the row away
    and the next listing drew it back as cancelled. A live row offers Stop;
    the record can be deleted once the run has ended."""
    job = running(fake, offsets=[0.0, 2.0])
    goto("/ui/jobs")
    settled(page)
    expect(job_row(page, job).locator("[data-stop]")).to_be_visible()
    expect(job_row(page, job).locator("[data-forget]")).to_have_count(0)


# ---- the GPU runner --------------------------------------------------------------------


GPU = "NVIDIA GeForce RTX 4070 · 3% used · 2.1 GB of 12.0 GB · 41 °C · running Chatterbox"
RUNNERS = {
    "idle": ({}, "GPU runner: idle", "", GPU),
    "working": ({"state": "busy", "job_running": True}, "GPU runner: busy · working", "", GPU),
    "busy elsewhere": ({"state": "busy", "can_run": False, "machine_state": "busy"},
                       "GPU runner: busy", "Busy with something else.", GPU),
    "occupied": ({"state": "occupied", "can_run": False, "machine_state": "occupied",
                  "seconds_until_available": 90},
                 "GPU runner: occupied", "Somebody is using that machine. Free again in 1m 30s.", GPU),
    "an answer this page has no words for": (
        {"state": "odd", "can_run": False, "machine_state": "hibernating"},
        "GPU runner: odd", "It cannot take work right now.", GPU),
    "no gpu reading": ({"gpu": {}, "services": []}, "GPU runner: idle", "", ""),
}


@pytest.mark.parametrize("case", list(RUNNERS))
def test_the_gpu_runner_panel_says_its_state_reason_and_gpu(page, goto, stack, fake, case):
    """From /health, which the page reads anyway: the state is the
    headline, why it will not take work is its own line, and the GPU line
    says what the card is doing. The usual mode, "auto", is not said at all."""
    change, headline, why, gpu = RUNNERS[case]
    runner = stack.fake.backend_health("tts_long")["runner"] | change
    fake.health("tts_long", runner=runner)
    goto("/ui/jobs")
    expect(page.locator("#runnerbox")).to_be_visible()
    expect(page.locator("#runnerstate")).to_have_text(headline)
    expect(page.locator("#runnerwhy")).to_have_text(why)
    expect(page.locator("#runnergpu")).to_have_text(gpu)
    expect(page.locator("#runnerwhere")).to_have_text("")
    expect(page.locator("#runnerwhere")).to_be_hidden()


def test_a_deployment_without_a_gpu_runner_shows_no_panel(page, goto, fake):
    fake.health("tts_long", runner=None)
    goto("/ui/jobs")
    settled(page)
    page.wait_for_function("() => HEALTH !== null")
    expect(page.locator("#runnerbox")).to_be_hidden()


def test_the_gpu_runner_that_does_not_answer_is_said_in_words(page, goto, fake):
    """tts-long reports the exception's class name; "The last attempt gave
    RemoteUnavailable" was the panel's whole explanation. Each name becomes a
    sentence about the machine, and no name reaches the page."""
    cases = {"RemoteUnavailable": "Nothing replied. The machine may be switched off, asleep or restarting.",
             "ConnectTimeout": "It did not reply in time. It may be asleep or busy.",
             "IncompleteRead": "Its reply broke off or could not be read. It may be restarting.",
             "HTTP 503": "It answered with an error. It may be starting up or shutting down."}
    for error, said in cases.items():
        fake.health("tts_long", runner={"reachable": False, "error": error})
        goto("/ui/jobs")
        expect(page.locator("#runnerstate")).to_have_text("GPU runner: not answering")
        expect(page.locator("#runnerwhy")).to_have_text(said)
        expect(page.locator("#runnergpu")).to_have_text("")
        assert error not in page.locator("#runnerbox").inner_text()


def test_every_gpu_runner_has_a_card_and_a_job_names_the_one_it_ran_on(page, goto, stack, fake):
    """tts-long drives offpeak's desktop and a Linux GPU box at once and lists
    both under `runners`. Each is drawn as the first card always was, named by
    the operator's label or numbered, and a card goes when its runner does."""
    first = stack.fake.backend_health("tts_long")["runner"]
    second = {"reachable": True, "can_run": True, "state": "busy", "machine_state": "free",
              "mode": "always-on", "job_running": True, "seconds_until_available": 0,
              "gpu": {"util_gpu": 97, "mem_used_mib": 4980}, "services": []}
    away = {"reachable": False, "error": "ConnectTimeout"}
    fake.health("tts_long", runners=[{"lane": "runner", "label": None, **first},
                                     {"lane": "runner2", "label": "Linux GPU", **second},
                                     {"lane": "runner3", "label": None, **away}])
    on_second = fake.add_job(scripted=False, status="done", backend="runner2",
                             runner_host="gpu-box", engine="chatterbox")["id"]
    page.clock.install()
    goto("/ui/jobs")
    settled(page)
    expect(page.locator("#runnerstate")).to_have_text("GPU runner 1: idle")
    expect(page.locator("#runnerstate-runner2")).to_have_text("Linux GPU: busy · working")
    expect(page.locator("#runnerwhere-runner2")).to_have_text("always-on")
    expect(page.locator("#runnergpu-runner2")).to_contain_text("97% used")
    expect(page.locator("#runnerstate-runner3")).to_have_text("GPU runner 3: not answering")
    expect(page.locator("#runnerwhy-runner3")).to_have_text(
        "It did not reply in time. It may be asleep or busy.")
    assert page.locator(".jobsrunner").evaluate_all("cards => cards.map(c => c.id)") == \
        ["runnerbox", "runnerbox-runner2", "runnerbox-runner3"], "the cards are out of order"
    # THE SAME CARD, NOT A NEW STYLE: every copy measures as the first does.
    sizes = page.locator(".jobsrunner").evaluate_all(
        "cards => cards.map(c => [getComputedStyle(c).borderBottomWidth, getComputedStyle(c).marginBottom])")
    assert len(set(map(tuple, sizes))) == 1, sizes
    expect(job_row(page, on_second)).to_contain_text("GPU gpu-box · Chatterbox")

    fake.health("tts_long", runners=[{"lane": "runner", "label": None, **first}])
    page.clock.fast_forward(30_000)
    expect(page.locator("#runnerbox-runner2")).to_have_count(0)
    expect(page.locator("#runnerbox-runner3")).to_have_count(0)
    expect(page.locator("#runnerstate")).to_have_text("GPU runner: idle")


# ---- when the list is asked for --------------------------------------------------------


SCHEDULE_SPY = """() => {
  window.__ticks = [];
  const original = window.schedule;
  window.schedule = function (delay) {
    window.__ticks.push(delay === undefined ? null : delay);
    return original.apply(this, arguments);
  };
}"""


def pause(page) -> None:
    """Stop the page's clock where it is; only run_for moves it from here.
    pause_at takes seconds, not the page's milliseconds."""
    page.clock.pause_at(page.evaluate("Date.now()") / 1000 + 0.05)


def advance(page, browser_log, ms: int) -> tuple[int, int | None]:
    """Run the page's clock for `ms` and say how many listings it asked for,
    and the delay it chose for its next tick once the last of them landed."""
    lists, ticks = len(listings(browser_log)), page.evaluate("() => window.__ticks.length")
    page.clock.run_for(ms)
    page.wait_for_timeout(300)
    asked = len(listings(browser_log)) - lists
    if asked:
        until(page, lambda: page.evaluate("t => window.__ticks.slice(t).some(delay => delay)", ticks),
              "the next tick scheduled once the listing landed")
    return asked, page.evaluate("() => window.__ticks.at(-1)")


def test_polling_slows_down_for_old_jobs_and_when_the_page_is_hidden(page, goto, stack, fake, browser_log, hide):
    """A live run that has stopped growing is asked about every 10 s while
    it is young and every 30 s once it is five minutes old; a page in the
    background asks every 30 s while something is live and not at all once
    nothing is."""
    started = time.time() - 60
    job = fake.add_job(scripted=False, status="running", created_at=started, started_at=started)["id"]
    page.clock.install()
    goto("/ui/jobs")
    settled(page)
    page.evaluate(SCHEDULE_SPY)
    pause(page)

    assert advance(page, browser_log, 10_500) == (1, 10_000)
    assert advance(page, browser_log, 9_500)[0] == 0
    assert advance(page, browser_log, 1_000) == (1, 10_000)

    assert advance(page, browser_log, 300_000) == (1, 30_000)
    assert advance(page, browser_log, 29_000)[0] == 0
    assert advance(page, browser_log, 2_000) == (1, 30_000)

    hide(page)
    assert advance(page, browser_log, 10) == (1, 30_000)
    assert advance(page, browser_log, 29_000)[0] == 0
    stack.api.delete(f"/jobs/{job}")
    assert advance(page, browser_log, 2_000) == (1, 30_000)
    expect(status(page, job)).to_have_text("cancelled")
    assert advance(page, browser_log, 95_000)[0] == 0, "a hidden page with nothing live asked for its jobs"


def test_a_job_deleted_on_another_device_leaves_the_open_list_at_the_next_poll(page, goto, stack):
    job = seeded_job(stack, "speech", "done")
    page.clock.install()
    goto("/ui/jobs")
    settled(page)
    expect(job_row(page, job)).to_be_visible()
    with stack.client() as phone:
        phone.delete(f"/jobs/{job}").raise_for_status()
    page.clock.fast_forward(31_000)
    expect(job_row(page, job)).to_have_count(0)


# ---- how it is drawn -------------------------------------------------------------------


def test_an_expired_row_keeps_its_buttons_at_full_contrast(page, goto, stack):
    """The swept row was drawn at 55% opacity, Retry and Delete with it. Its
    words are dimmed now and its buttons are the same as any other row's."""
    swept = seeded_job(stack, "clone", "failed")
    kept = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    row = job_row(page, swept)
    expect(row).to_have_class(re.compile(r"\bexpired\b"))
    assert row.evaluate("e => getComputedStyle(e).opacity") == "1"
    colour = "e => getComputedStyle(e).color"
    assert (row.locator("[data-forget]").evaluate(colour)
            == job_row(page, kept).locator("[data-forget]").evaluate(colour)), \
        "Delete on the swept row is drawn fainter than on any other"


def test_the_destructive_buttons_sit_at_the_end_of_a_row(page, goto, stack):
    """Delete the audio and Delete the record too end the row together, away
    from Play and Download, where a hand moving along it lands by momentum."""
    job = seeded_job(stack, "clone", "done", "present")
    goto("/ui/jobs")
    settled(page)
    acts = job_row(page, job).locator(".acts")
    end = acts.bounding_box()
    forget = acts.locator("[data-forget]").bounding_box()
    drop = acts.locator("[data-delaudio]").bounding_box()
    get = acts.locator("[data-get]").bounding_box()
    assert abs((forget["x"] + forget["width"]) - (end["x"] + end["width"])) <= 1, "Delete is not at the end"
    assert drop["x"] < forget["x"] and abs(drop["y"] - forget["y"]) <= 1, "the two deletes are apart"
    assert drop["x"] - (get["x"] + get["width"]) > 24, "nothing separates Download from the deletes"


def test_refresh_stays_on_the_filters_line_on_a_phone(new_page, goto):
    """On a phone the kind takes a line and the filter shares the next with
    Refresh, which asks again for what that filter shows."""
    page = new_page("mobile")
    goto("/ui/jobs", target=page)
    settled(page)
    refresh = page.locator("#refresh").bounding_box()
    shown = page.locator("#jobfilter").bounding_box()
    assert abs(refresh["y"] - shown["y"]) <= 4, f"Refresh is on another line: {refresh} {shown}"


def test_every_row_button_is_44px_on_a_touch_screen(new_page, goto):
    """.row > button.small took its height from a token the coarse-pointer
    floor did not reach, so Refresh and every row action stayed at 38px."""
    page = new_page("mobile")
    goto("/ui/jobs", target=page)
    settled(page)
    expect(page.locator("#joblist .job").first).to_be_visible()
    heights = page.locator(ROW_BUTTONS).evaluate_all(
        "els => els.filter(e => e.checkVisibility()).map(e => [e.textContent.trim(), e.getBoundingClientRect().height])")
    assert heights, "no row buttons to measure"
    short = [(name, h) for name, h in heights if h < 43.5]
    assert not short, f"buttons under a thumb's 44px: {short}"


def test_a_run_that_died_in_the_runners_transport_says_so_in_words(page, goto, fake):
    """tts-long's reason for a run lost in transport is the request and the
    exception; the row says what that means and names neither."""
    job = fake.add_job(scripted=False, status="failed", kind="clone",
                       error="GET /v1/status: IncompleteRead(0 bytes read)")["id"]
    goto(f"/ui/jobs/{job}")
    settled(page)
    said = job_row(page, job).locator(".note.bad")
    expect(said).to_have_text("The GPU runner stopped answering during this run.")
