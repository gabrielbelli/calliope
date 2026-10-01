"""The page's addresses, in a browser, against the local stack.

Every tab and every place in one has a path (the table is in
services/ui/README.md, "Addresses"), the page reads its location to open it,
and Back and Forward walk what the reader opened. test_navigation.py drives
the pure half in Node; this is the rest: the server and the gateway serving
each path, the page opening what it names once the lists have answered, the
history each press writes, and what a link to something gone says.

The stack is the session's: Kitchen (020000000001, a Korvo) and Lounge
(020000000002, a Pi playing AirPlay) adopted, Hallway (020000000003) waiting,
wake words hey_jarvis and alexa, profiles dictation and tech, five seeded jobs.
Tests that change what the hub holds use `fresh_hub`, and they are the last
in this file, so nothing before them sees a renamed or forgotten satellite.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

KITCHEN, LOUNGE, HALLWAY = "020000000001", "020000000002", "020000000003"
TABS = {"transcribe": "Transcribe", "speak": "Speak", "jobs": "Jobs",
        "vocab": "Vocabulary", "satellites": "Satellites"}
SLUGS = {"transcribe": "/ui", "speak": "/ui/speak", "jobs": "/ui/jobs",
         "vocab": "/ui/vocabulary", "satellites": "/ui/satellites"}


# ---- helpers ---------------------------------------------------------------------------


def here(page) -> str:
    return page.evaluate("location.pathname + location.search")


def wait_for_address(page, address: str) -> None:
    page.wait_for_function("a => location.pathname + location.search === a", arg=address)


def settled(page) -> None:
    """The router has an answer for the address: the list it waits on has
    answered, nothing is pending on the hub, and the bar holds what it
    resolved to. The page's own names, read from its global scope."""
    page.wait_for_function("""() => {
      const kind = NAV_WAITS[NAV.route.view];
      return (!kind || NAV.settled.has(kind)) && !NAV.pending
        && navPath(NAV.route) === location.pathname + location.search;
    }""")


def history_length(page) -> int:
    return page.evaluate("history.length")


def sat(page, nid: str):
    return page.locator(f'li.sat[data-id="{nid}"]')


def row(page, nid: str):
    return sat(page, nid).locator(":scope > details.sat-row")


def seeded_job(stack, kind: str, status: str, audio: str | None = None) -> str:
    """The id of a job the fake tts-long seeded, asked of it directly."""
    jobs = httpx.get(f"http://127.0.0.1:{stack.ports['long']}/jobs", timeout=5).json()["jobs"]
    for job in jobs:
        if job["kind"] == kind and job["status"] == status and (
                audio is None or (job.get("audio") or {}).get("state") == audio):
            return job["id"]
    raise AssertionError(f"no seeded {kind} job that is {status} with audio {audio}: {jobs}")


def open_tab(page, tab: str) -> None:
    page.locator(f'[role=tab][data-tab="{tab}"]').click()
    expect(page.locator(f'[role=tab][data-tab="{tab}"]')).to_have_attribute("aria-selected", "true")


# ---- served ----------------------------------------------------------------------------


ADDRESSES = ["/ui", "/ui/transcribe", "/ui/transcribe/expert", "/ui/speak", "/ui/speak?voice=k:af_heart",
             "/ui/speak/clone", "/ui/speak/expert", "/ui/jobs", "/ui/jobs?show=failed&kind=clone",
             "/ui/jobs/0123456789abcdef", "/ui/vocabulary", "/ui/vocabulary/tech", "/ui/satellites",
             "/ui/satellites/kitchen", "/ui/satellites/lounge/airplay", "/ui/satellites/wake-words",
             "/ui/satellites/wake-words/hey_jarvis", "/ui/satellites/wake-words/hey_jarvis/more",
             "/ui/satellites/try-a-word", "/ui/satellites/custom-models", "/ui/satellites/activity",
             "/ui/satellites/telemetry", "/ui/satellites/firmware"]


def test_every_page_address_is_served_through_the_gateway(stack):
    """A reload or a pasted link reaches the gateway first; each address has
    to cross its allowlist and voice-ui's, and come back as the page."""
    with httpx.Client(base_url=stack.url, timeout=10) as http:
        page = http.get("/ui").text
        for address in ADDRESSES:
            response = http.get(address)
            assert response.status_code == 200, address
            assert response.headers["content-type"].startswith("text/html"), address
            assert response.text == page, f"{address} is not the same page"
        nope = http.get("/ui/nope")
        assert nope.status_code == 404
        assert nope.json()["error"]["code"] == "unknown_url"


# ---- tabs ------------------------------------------------------------------------------


# Where the bead is, relative to the selected tab, on each of the first twenty
# frames the dock is drawn. Installed before the page's own script runs.
BEAD_FRAMES = """
(() => {
  const seen = [];
  window.__bead = seen;
  const look = () => {
    const bead = document.getElementById("bead"), dock = document.getElementById("dock");
    const tab = document.querySelector('[role=tab][aria-selected="true"]');
    if (bead && dock && dock.classList.contains("ready") && tab) {
      const b = bead.getBoundingClientRect(), t = tab.getBoundingClientRect();
      seen.push((b.left + b.width / 2) - (t.left + t.width / 2));
    }
    if (seen.length < 20) requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
})();
"""


@pytest.mark.parametrize("tab", list(SLUGS))
def test_each_tab_address_opens_its_own_tab_on_load_without_the_bead_travelling(page, goto, tab):
    """Before the router the page always opened on Transcribe, so a link to
    Jobs would have drawn Transcribe first and then slid the bead across.
    The tab is chosen before the first paint: the bead is under it from the
    first frame the dock is drawn, and stays there."""
    page.add_init_script(BEAD_FRAMES)
    goto(SLUGS[tab])
    button = page.locator(f'[role=tab][data-tab="{tab}"]')
    expect(button).to_have_attribute("aria-selected", "true")
    visible = page.locator("[role=tabpanel]").evaluate_all(
        "els => els.filter(e => !e.hidden && getComputedStyle(e).display !== 'none').map(e => e.id)")
    assert visible == [f"tab-{tab}"], visible
    expect(page.locator("#word")).to_have_text(TABS[tab])
    page.wait_for_function("() => window.__bead && window.__bead.length >= 20")
    frames = page.evaluate("window.__bead")
    settled_at = frames[-1]
    assert abs(frames[0] - settled_at) <= 2, f"the bead started {frames[0] - settled_at:.1f}px away: {frames}"
    assert max(abs(f - settled_at) for f in frames) <= 2, f"the bead travelled: {frames}"


def test_clicking_a_tab_puts_its_address_in_the_bar(page, goto):
    goto("/ui")
    settled(page)
    for tab in ("speak", "jobs", "vocab", "satellites", "transcribe"):
        open_tab(page, tab)
        wait_for_address(page, SLUGS[tab])
        assert page.title().endswith(f"{TABS[tab]} · Calliope"), page.title()


def test_arrow_keys_and_a_dock_drag_change_the_address_like_a_click(page, goto):
    """Three ways to choose a tab, one handler, one address each."""
    goto("/ui")
    settled(page)
    page.locator("#tab-btn-transcribe").focus()
    page.keyboard.press("ArrowRight")
    wait_for_address(page, "/ui/speak")
    page.keyboard.press("End")
    wait_for_address(page, "/ui/satellites")
    page.keyboard.press("Home")
    wait_for_address(page, "/ui")
    before = history_length(page)
    drag_bead_to(page, "jobs")
    wait_for_address(page, "/ui/jobs")
    assert history_length(page) == before + 1


def drag_bead_to(page, tab: str) -> None:
    """Press on the bead, drag it past the dock's 7px hysteresis to the
    tab's centre, and let go: the pointer path a finger takes."""
    bead = page.locator("#bead").bounding_box()
    target = page.locator(f'[role=tab][data-tab="{tab}"]').bounding_box()
    y = bead["y"] + bead["height"] / 2
    page.mouse.move(bead["x"] + bead["width"] / 2, y)
    page.mouse.down()
    page.mouse.move(target["x"] + target["width"] / 2, y, steps=12)
    page.mouse.up()


def test_dragging_the_bead_writes_the_new_tab_into_the_masthead(page, goto):
    """A REAL DEFECT: the dock wrote the <h1> from a listener of its own that a
    drag suppressed, so the bead and the panel moved to the new tab and the
    heading kept the old one."""
    goto("/ui")
    settled(page)
    drag_bead_to(page, "vocab")
    expect(page.locator("#tab-btn-vocab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#word")).to_have_text("Vocabulary")
    wait_for_address(page, "/ui/vocabulary")


def test_back_and_forward_walk_the_tabs_visited(page, goto):
    goto("/ui")
    settled(page)
    open_tab(page, "speak")
    open_tab(page, "jobs")
    page.go_back()
    wait_for_address(page, "/ui/speak")
    expect(page.locator("#tab-btn-speak")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#word")).to_have_text("Speak")
    # The focus goes back to the tablist's one stop, the tab now selected.
    expect(page.locator("#tab-btn-speak")).to_be_focused()
    page.go_back()
    wait_for_address(page, "/ui")
    expect(page.locator("#tab-btn-transcribe")).to_have_attribute("aria-selected", "true")
    page.go_forward()
    wait_for_address(page, "/ui/speak")
    expect(page.locator("#tab-btn-speak")).to_have_attribute("aria-selected", "true")
    assert not page.locator("#tab-speak").is_hidden()


def test_reselecting_the_open_tab_adds_no_history(page, goto):
    goto("/ui/jobs")
    settled(page)
    before = history_length(page)
    page.locator("#tab-btn-jobs").click()
    page.locator("#tab-btn-jobs").click()
    page.wait_for_timeout(200)
    assert history_length(page) == before
    assert here(page) == "/ui/jobs"


def test_a_reload_returns_to_the_same_place(page, goto):
    goto("/ui/satellites/lounge/airplay")
    settled(page)
    page.reload()
    settled(page)
    assert here(page) == "/ui/satellites/lounge/airplay"
    expect(row(page, LOUNGE)).to_have_attribute("open", "")
    expect(sat(page, LOUNGE).locator("details.sub.sat-ap")).to_have_attribute("open", "")


TITLES = [
    ("/ui", "Transcribe · Calliope"),
    ("/ui/transcribe/expert", "Expert · Transcribe · Calliope"),
    ("/ui/speak/clone", "Use my own voice · Speak · Calliope"),
    ("/ui/jobs/{job}", "Job {short} · Jobs · Calliope"),
    ("/ui/vocabulary/tech", "tech · Vocabulary · Calliope"),
    ("/ui/satellites/lounge/airplay", "Lounge · AirPlay · Satellites · Calliope"),
    ("/ui/satellites/kitchen", "Kitchen · Satellites · Calliope"),
    ("/ui/satellites/wake-words", "Wake words · Satellites · Calliope"),
    ("/ui/satellites/wake-words/hey_jarvis", "hey jarvis · Wake words · Satellites · Calliope"),
    ("/ui/satellites/wake-words/ptt", "Push-to-talk · Wake words · Satellites · Calliope"),
    ("/ui/satellites/try-a-word", "Try a word · Satellites · Calliope"),
]


@pytest.mark.parametrize(("address", "title"), TITLES, ids=[a for a, _ in TITLES])
def test_the_title_names_the_place_most_specific_first(page, goto, stack, address, title):
    """A narrow browser tab shows the start of the title, so the start is the
    thing and "Calliope" is last."""
    job = seeded_job(stack, "clone", "done", "present")
    goto(address.format(job=job))
    settled(page)
    expect(page).to_have_title(title.format(short=job[:8]))


def test_a_running_job_prefixes_the_title_with_its_time_left_on_any_tab(page, goto, fake):
    """What the reader switched away to wait for leads the title on every
    tab, and goes when the job is done."""
    fake.add_job(text="One sentence here. Another sentence there. A third one follows. And a fourth.")
    goto("/ui")
    expect(page).to_have_title(re.compile(r"^\d+m left · Transcribe · Calliope$"))
    expect(page).to_have_title("Transcribe · Calliope", timeout=20_000)


# ---- satellites ------------------------------------------------------------------------


def test_a_satellite_address_by_name_opens_and_focuses_its_row(page, goto):
    goto("/ui/satellites/kitchen")
    settled(page)
    expect(row(page, KITCHEN)).to_have_attribute("open", "")
    expect(row(page, KITCHEN).locator(":scope > summary")).to_be_focused()
    assert here(page) == "/ui/satellites/kitchen"


def test_a_satellite_address_by_id_is_replaced_by_its_name(page, goto):
    goto(f"/ui/satellites/{LOUNGE}")
    wait_for_address(page, "/ui/satellites/lounge")
    expect(row(page, LOUNGE)).to_have_attribute("open", "")
    expect(row(page, LOUNGE).locator(":scope > summary")).to_be_focused()


def test_an_airplay_address_opens_the_section_inside_its_row(page, goto):
    goto("/ui/satellites/lounge/airplay")
    settled(page)
    section = sat(page, LOUNGE).locator("details.sub.sat-ap")
    expect(row(page, LOUNGE)).to_have_attribute("open", "")
    expect(section).to_have_attribute("open", "")
    expect(section.locator(":scope > summary")).to_be_focused()
    assert here(page) == "/ui/satellites/lounge/airplay"


def test_a_section_a_satellite_lacks_falls_back_to_its_row(page, goto):
    """The Korvo has no AirPlay: the section is dropped from the address and
    the row opens, with nothing said, since the satellite is there."""
    goto("/ui/satellites/kitchen/airplay")
    wait_for_address(page, "/ui/satellites/kitchen")
    expect(row(page, KITCHEN)).to_have_attribute("open", "")
    expect(page.locator("#satnote")).to_have_text("")


def test_a_pending_satellite_is_addressed_by_id_and_focuses_its_name_box(page, goto):
    goto(f"/ui/satellites/{HALLWAY}")
    settled(page)
    expect(sat(page, HALLWAY).locator("input").first).to_be_focused()
    assert here(page) == f"/ui/satellites/{HALLWAY}"


def test_a_satellite_that_is_not_there_says_so_and_shows_the_list(page, goto):
    goto("/ui/satellites/garage")
    wait_for_address(page, "/ui/satellites")
    expect(page.locator("#satnote")).to_have_text(
        "Nothing called garage is on this hub now, so the whole list is shown.")
    expect(page.locator("#satellitelist > li.sat")).to_have_count(3)


HUB_SECTIONS = [("wake-words", ["sat-wakewords"]), ("try-a-word", ["sat-wakewords", "ww-try"]),
                ("custom-models", ["sat-wakewords", "ww-models"]), ("activity", ["sat-activity"]),
                ("telemetry", ["sat-telemetry"]), ("firmware", ["sat-firmware"])]


@pytest.mark.parametrize(("key", "boxes"), HUB_SECTIONS, ids=[k for k, _ in HUB_SECTIONS])
def test_each_hub_section_address_opens_that_section(page, goto, key, boxes):
    goto(f"/ui/satellites/{key}")
    settled(page)
    for box in boxes:
        expect(page.locator(f"#{box}")).to_have_attribute("open", "")
    expect(page.locator(f"#{boxes[-1]} > summary")).to_be_focused()
    assert here(page) == f"/ui/satellites/{key}"


def test_a_wake_word_address_opens_its_row_and_more_opens_its_extras(page, goto):
    goto("/ui/satellites/wake-words/hey_jarvis/more")
    settled(page)
    word = page.locator('#wwlist li.ww[data-name="hey_jarvis"]')
    expect(page.locator("#sat-wakewords")).to_have_attribute("open", "")
    expect(word.locator(":scope > details.sat-row")).to_have_attribute("open", "")
    expect(word.locator("details.ww-more")).to_have_attribute("open", "")
    expect(word.locator("details.ww-more > summary")).to_be_focused()
    assert here(page) == "/ui/satellites/wake-words/hey_jarvis/more"


def test_push_to_talk_is_addressed_as_ptt(page, goto):
    goto("/ui/satellites/wake-words/ptt")
    settled(page)
    expect(page.locator('#wwptt li[data-name="ptt"] > details.sat-row')).to_have_attribute("open", "")
    assert here(page) == "/ui/satellites/wake-words/ptt"


def test_a_wake_word_that_is_not_there_says_so_in_the_wake_word_note(page, goto):
    goto("/ui/satellites/wake-words/hey_mycroft")
    wait_for_address(page, "/ui/satellites/wake-words")
    expect(page.locator("#wwnote")).to_have_text("There is no wake word called hey mycroft on this hub now.")
    expect(page.locator("#sat-wakewords")).to_have_attribute("open", "")


# ---- jobs ------------------------------------------------------------------------------


def test_a_job_address_highlights_scrolls_to_and_opens_that_job(new_page, goto, stack, browser_log):
    """The row is found in the listing, marked as the current one, scrolled
    to, focused, and its text opened, which is the one request it costs."""
    page = new_page((1200, 500))
    job = seeded_job(stack, "clone", "done", "present")
    goto(f"/ui/jobs/{job}", target=page)
    settled(page)
    card = page.locator(f'.job[data-job="{job}"]')
    expect(card).to_have_class(re.compile(r"\bhere\b"))
    expect(card).to_have_attribute("aria-current", "true")
    expect(card).to_be_focused()
    expect(card.locator("details.jobtext")).to_have_attribute("open", "")
    expect(card.locator("[data-text]")).not_to_have_text("Loading…")
    top, scrolled, height = card.evaluate("e => [e.getBoundingClientRect().top, scrollY, innerHeight]")
    assert scrolled > 0 and -1 <= top < height, f"the row is at {top} of {height} after a scroll of {scrolled}"
    assert page.locator(".job.here").count() == 1
    assert len(browser_log.sent("GET", rf"^/ui/api/jobs/{job}$")) == 1


def test_a_job_filtered_out_offers_to_show_everything(page, goto, stack):
    job = seeded_job(stack, "clone", "done", "present")
    goto(f"/ui/jobs/{job}?show=failed")
    expect(page.locator("#jobnote")).to_contain_text(f"Job {job[:8]} is not in this list.")
    assert here(page) == f"/ui/jobs/{job}?show=failed", "a job that exists keeps its address"
    page.locator("#jobshowall").click()
    wait_for_address(page, f"/ui/jobs/{job}?show=all")
    expect(page.locator("#jobfilter")).to_have_value("all")
    expect(page.locator("#jobkind")).to_have_value("all")
    card = page.locator(f'.job[data-job="{job}"]')
    expect(card).to_have_class(re.compile(r"\bhere\b"))
    expect(card).to_be_focused()
    expect(page.locator("#jobnote")).to_have_text("")


def test_a_job_that_is_gone_says_so_and_drops_the_id(page, goto, browser_log):
    browser_log.allow(404, r"^/ui/api/jobs/0000000000000000$")
    goto("/ui/jobs/0000000000000000")
    wait_for_address(page, "/ui/jobs")
    expect(page.locator("#jobnote")).to_have_text(
        "There is no job 00000000 here now, so the whole list is shown.")
    # The note is about the link, so the next move takes it back.
    page.locator("#jobfilter").select_option("all")
    wait_for_address(page, "/ui/jobs?show=all")
    expect(page.locator("#jobnote")).to_have_text("")


def test_the_jobs_filter_and_kind_live_in_the_query_and_are_restored(page, goto, new_page):
    goto("/ui/jobs")
    settled(page)
    before = history_length(page)
    page.locator("#jobfilter").select_option("failed")
    page.locator("#jobkind").select_option("clone")
    wait_for_address(page, "/ui/jobs?show=failed&kind=clone")
    assert history_length(page) == before, "a filter is a setting, not a step"
    page.reload()
    settled(page)
    expect(page.locator("#jobfilter")).to_have_value("failed")
    expect(page.locator("#jobkind")).to_have_value("clone")
    # And from the address alone, in a browser that has never set them.
    other = new_page()
    goto("/ui/jobs?show=failed&kind=clone", target=other)
    settled(other)
    expect(other.locator("#jobfilter")).to_have_value("failed")
    expect(other.locator("#jobkind")).to_have_value("clone")
    # Back on a tab reached by a click, the address says what the list shows.
    open_tab(other, "speak")
    open_tab(other, "jobs")
    wait_for_address(other, "/ui/jobs?show=failed&kind=clone")


# ---- vocabulary, speak, transcribe -----------------------------------------------------


def test_a_vocabulary_address_opens_that_profile_and_a_missing_one_says_so(page, goto):
    goto("/ui/vocabulary/TECH")
    wait_for_address(page, "/ui/vocabulary/tech")
    expect(page.locator("#glossform")).to_be_visible()
    expect(page.locator("#glossname")).to_have_value("tech")
    expect(page.locator('#glossnames [data-open="tech"]')).to_be_focused()
    goto("/ui/vocabulary/nope")
    wait_for_address(page, "/ui/vocabulary")
    expect(page.locator("#glossnote")).to_have_text("There is no profile called nope, so none is open.")
    expect(page.locator("#glossform")).to_be_hidden()
    # A profile opened by its button is a step Back undoes.
    before = history_length(page)
    page.locator('#glossnames [data-open="dictation"]').click()
    wait_for_address(page, "/ui/vocabulary/dictation")
    assert history_length(page) == before + 1
    expect(page.locator("#glossnote")).to_have_text("")


def test_a_speak_address_selects_the_voice_it_names_or_says_it_is_missing(page, goto):
    goto("/ui/speak?voice=k:af_heart")
    settled(page)
    expect(page.locator("#voice")).to_have_value("k:af_heart")
    goto("/ui/speak?voice=k:nobody")
    expect(page.locator("#speak-note")).to_have_text(
        "That link names a voice this server does not have: k:nobody.")
    page.wait_for_function("() => !location.search.includes('nobody')")
    assert page.locator("#voice").input_value() != "k:nobody"


def test_the_clone_address_opens_the_sheet_only_where_cloning_is_on(page, goto, new_page):
    goto("/ui/speak/clone")
    settled(page)
    expect(page.locator("#clone")).to_be_visible()
    expect(page.locator("#voice")).to_have_value("new")
    expect(page.locator("#clipname")).to_be_focused()
    # A deployment that cannot keep a clip: the same link opens Speak.
    other = new_page()

    def no_cloning(route) -> None:
        answer = route.fetch()
        route.fulfill(response=answer, json={**answer.json(), "cloning": False})

    other.route("**/ui/config", no_cloning)
    goto("/ui/speak/clone", target=other)
    other.wait_for_function("() => location.pathname === '/ui/speak'")
    expect(other.locator("#clone")).to_be_hidden()
    assert other.locator("#voice").input_value() != "new"


def test_the_expert_addresses_open_the_expert_panels(page, goto):
    goto("/ui/transcribe/expert")
    settled(page)
    expect(page.locator("#stt-expert")).to_have_attribute("open", "")
    expect(page.locator("#stt-expert > summary")).to_be_focused()
    goto("/ui/speak/expert")
    settled(page)
    shown = page.locator("#tts-expert-fast:not([hidden]), #tts-expert-clone:not([hidden])")
    expect(shown).to_have_count(1)
    expect(shown).to_have_attribute("open", "")
    assert here(page).startswith("/ui/speak/expert")


# ---- history ---------------------------------------------------------------------------


def test_opening_a_row_pushes_and_opening_a_section_inside_it_replaces(page, goto):
    goto("/ui/satellites")
    settled(page)
    start = history_length(page)
    row(page, LOUNGE).locator(":scope > summary").click()
    wait_for_address(page, "/ui/satellites/lounge")
    assert history_length(page) == start + 1
    sat(page, LOUNGE).locator("details.sub.sat-ap > summary").click()
    wait_for_address(page, "/ui/satellites/lounge/airplay")
    assert history_length(page) == start + 1, "a section inside the row added a step"
    # Closing the section is a detail too: the row's address, rewritten.
    sat(page, LOUNGE).locator("details.sub.sat-ap > summary").click()
    wait_for_address(page, "/ui/satellites/lounge")
    assert history_length(page) == start + 1


def test_closing_the_row_the_address_names_goes_back_rather_than_forward(page, goto):
    goto("/ui/satellites")
    settled(page)
    start = history_length(page)
    summary = row(page, KITCHEN).locator(":scope > summary")
    summary.click()
    wait_for_address(page, "/ui/satellites/kitchen")
    summary.click()
    wait_for_address(page, "/ui/satellites")
    assert history_length(page) == start + 1, "closing added an entry instead of going back"
    expect(row(page, KITCHEN)).not_to_have_attribute("open", "")
    # The entry that was closed is still there, one step forward.
    page.go_forward()
    wait_for_address(page, "/ui/satellites/kitchen")
    expect(row(page, KITCHEN)).to_have_attribute("open", "")


def test_back_restores_the_scroll_and_focus_of_the_list(new_page, goto):
    page = new_page((1000, 500))
    goto("/ui/satellites", target=page)
    settled(page)
    room = page.evaluate("document.documentElement.scrollHeight - innerHeight")
    assert room > 40, "the page is too short to scroll, so this proves nothing"
    page.evaluate(f"scrollTo(0, {min(160, room)})")
    summary = row(page, LOUNGE).locator(":scope > summary")
    summary.focus()
    page.keyboard.press("Enter")
    wait_for_address(page, "/ui/satellites/lounge")
    page.evaluate("scrollTo(0, 0)")
    page.go_back()
    wait_for_address(page, "/ui/satellites")
    # What the list's entry was left with, written on it as the row opened.
    kept = page.evaluate("history.state.scroll")
    assert kept > 0, "the scroll was not kept on the entry"
    page.wait_for_function("y => Math.abs(scrollY - y) <= 1", arg=kept)
    expect(summary).to_be_focused()
    expect(row(page, LOUNGE)).not_to_have_attribute("open", "")


def test_back_closes_what_the_entry_being_left_opened(page, goto):
    goto("/ui/satellites")
    settled(page)
    page.locator("#sat-wakewords > summary").click()
    wait_for_address(page, "/ui/satellites/wake-words")
    page.go_back()
    wait_for_address(page, "/ui/satellites")
    expect(page.locator("#sat-wakewords")).not_to_have_attribute("open", "")
    page.go_forward()
    wait_for_address(page, "/ui/satellites/wake-words")
    expect(page.locator("#sat-wakewords")).to_have_attribute("open", "")


def test_back_while_the_link_dialog_is_open_abandons_the_link(page, goto, browser_log):
    """The dialog is a question about the entry being left; Back answers it No,
    and a link that was resolved is let go on the server as Don't fetch
    would let it go."""
    goto("/ui")
    settled(page)
    open_tab(page, "speak")
    open_tab(page, "transcribe")
    page.locator("#url").fill("https://example.com/a-talk")
    page.locator("#resolve").click()
    expect(page.locator("#confirm")).to_have_attribute("open", "")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/ui/abandon")):
        page.go_back()
    wait_for_address(page, "/ui/speak")
    expect(page.locator("#confirm")).not_to_have_attribute("open", "")


def test_restoring_any_address_sends_nothing_but_reads(page, goto, stack, browser_log):
    """A URL must never cause an effect: loading every address the page
    writes asks for things and changes nothing."""
    # The one made-up job in ADDRESSES is looked up, and it is not there.
    browser_log.allow(404, r"^/ui/api/jobs/0123456789abcdef$")
    job = seeded_job(stack, "clone", "done", "present")
    for address in ADDRESSES + [f"/ui/jobs/{job}", "/ui/satellites/wake-words/ptt",
                                f"/ui/satellites/{HALLWAY}"]:
        goto(address)
        settled(page)
    writes = [r for r in browser_log.requests if r["method"] not in ("GET", "HEAD")]
    assert not writes, f"loading an address sent {writes}"


# ---- what the page does itself (these change the hub, and run last) --------------------


def test_generate_on_a_job_voice_lands_on_that_jobs_address(page, goto):
    goto("/ui/speak")
    settled(page)
    voice = page.evaluate("""() => [...document.getElementById("voice").options]
      .find(o => o.textContent.trim() === "narrator" && !o.disabled).value""")
    page.locator("#voice").select_option(voice)
    page.locator("#text").fill("A short line for the queue.")
    page.locator("#go-tts-quiet").click()
    page.wait_for_function("() => /^\\/ui\\/jobs\\/[0-9a-f-]{36}$/.test(location.pathname)")
    job = page.evaluate("location.pathname.split('/').pop()")
    expect(page.locator(f'.job[data-job="{job}"]')).to_have_class(re.compile(r"\bhere\b"))
    expect(page).to_have_title(re.compile(rf"Job {job[:8]} · Jobs · Calliope$"))
    page.go_back()
    page.wait_for_function("() => location.pathname === '/ui/speak'")
    expect(page.locator("#tab-btn-speak")).to_have_attribute("aria-selected", "true")


def test_change_wake_words_is_a_step_back_can_undo(page, goto):
    goto("/ui/satellites/kitchen")
    settled(page)
    before = history_length(page)
    sat(page, KITCHEN).locator('[data-act="wakewords"]').click()
    wait_for_address(page, "/ui/satellites/wake-words")
    assert history_length(page) == before + 1
    expect(page.locator("#sat-wakewords")).to_have_attribute("open", "")
    page.go_back()
    wait_for_address(page, "/ui/satellites/kitchen")
    expect(page.locator("#sat-wakewords")).not_to_have_attribute("open", "")
    expect(row(page, KITCHEN)).to_have_attribute("open", "")


def test_a_rename_replaces_the_address_with_the_new_name(page, goto, fresh_hub):
    goto("/ui/satellites/kitchen/device")
    settled(page)
    before = history_length(page)
    field = sat(page, KITCHEN).locator(".sat-rename input")
    field.fill("Pantry")
    field.press("Enter")
    wait_for_address(page, "/ui/satellites/pantry/device")
    assert history_length(page) == before, "a rename is not a step"
    expect(page).to_have_title("Pantry · Device · Satellites · Calliope")
    # The entry keeps the satellite's ID, so a reload finds it by either.
    page.reload()
    settled(page)
    assert here(page) == "/ui/satellites/pantry/device"
    expect(sat(page, KITCHEN).locator("details.sub.sat-device")).to_have_attribute("open", "")


def test_forgetting_the_satellite_the_address_names_returns_to_the_list(page, goto, fresh_hub, dialogs):
    dialogs()
    goto("/ui/satellites/kitchen/device")
    settled(page)
    sat(page, KITCHEN).locator('[data-act="forget"]').click()
    wait_for_address(page, "/ui/satellites")
    assert dialogs.seen and dialogs.seen[0][1].startswith("Forget Kitchen?"), dialogs.seen
    expect(page).to_have_title("Satellites · Calliope")
