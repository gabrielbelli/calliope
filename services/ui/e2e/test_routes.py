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

import json
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


# What the dock shows on each of the first twenty frames it is drawn, measured
# against the tab the ADDRESS names. Measured against whichever tab was
# selected on the frame, a frame drawn before the page had chosen passed as
# "under jobs" with the pill under Transcribe. Per frame: the pill's centre
# less that tab's, every tab's name opacity, the accent the pill is painted
# in, and every icon's height above the bar's centre line. Called with the
# tab's name, before the page's own script runs.
DOCK_FRAMES = """target => {
  const seen = [];
  window.__dock = seen;
  const look = () => {
    const pill = document.getElementById("highlight"), dock = document.getElementById("dock");
    const tab = document.querySelector(`[role=tab][data-tab="${target}"]`);
    const tabs = [...document.querySelectorAll("[role=tab]")];
    if (pill && dock && tab && tabs.length === 5) {
      const b = pill.getBoundingClientRect(), t = tab.getBoundingClientRect(), d = dock.getBoundingClientRect();
      const mid = r => r.top + r.height / 2;
      seen.push({
        off: (b.left + b.width / 2) - (t.left + t.width / 2),
        names: Object.fromEntries(tabs.map(e => [e.dataset.tab,
                                                 Number(getComputedStyle(e.querySelector(".label")).opacity)])),
        glow: getComputedStyle(pill).getPropertyValue("--glow-rgb").trim(),
        rise: Object.fromEntries(tabs.map(e => [e.dataset.tab,
                                                mid(d) - mid(e.querySelector("svg").getBoundingClientRect())])),
      });
    }
    if (seen.length < 20) requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
}"""

# How far the pill's centre is from the selected tab's, and whether anything
# in the bar is still moving. The page's own elements, read in one pass.
PILL = """() => {
  const pill = document.getElementById("highlight");
  const tab = document.querySelector('[role=tab][aria-selected="true"]');
  const b = pill.getBoundingClientRect(), t = tab.getBoundingClientRect();
  return { off: (b.left + b.width / 2) - (t.left + t.width / 2), width: b.width - t.width,
           moving: document.getElementById("dock").getAnimations({ subtree: true }).length };
}"""


def accent(page, tab: str) -> str:
    """The tab's own accent as the bare channels the pill is painted from."""
    hexa = page.locator(f'[role=tab][data-tab="{tab}"]').evaluate(
        "e => getComputedStyle(e).getPropertyValue('--acc').trim()")
    return " ".join(str(int(hexa[i:i + 2], 16)) for i in (1, 3, 5))


def pill_under(page, tab: str) -> None:
    """The tab is selected and the bar has come to rest with the pill under it, one tab wide."""
    expect(page.locator(f'[role=tab][data-tab="{tab}"]')).to_have_attribute("aria-selected", "true")
    page.wait_for_function(f"() => {{ const p = ({PILL})(); return Math.abs(p.off) <= 1 && !p.moving; }}")
    assert abs(page.evaluate(PILL)["width"]) <= 1, "the pill is not one tab wide"


@pytest.mark.parametrize("tab", list(SLUGS))
def test_each_tab_address_opens_its_own_tab_on_load_without_the_pill_travelling(page, goto, tab):
    """Before the router the page always opened on Transcribe, so a link to
    Jobs would have drawn Transcribe first and then slid the selection across.
    The tab is chosen before the first paint, and from the first frame the
    dock is drawn nothing in it moves: the pill is under the tab, only its
    name shows, the pill is in its accent and only its icon is raised. A link
    to Jobs once opened with "Transcribe" fading out for 150 ms."""
    page.add_init_script(f"({DOCK_FRAMES})({json.dumps(tab)})")
    goto(SLUGS[tab])
    button = page.locator(f'[role=tab][data-tab="{tab}"]')
    expect(button).to_have_attribute("aria-selected", "true")
    visible = page.locator("[role=tabpanel]").evaluate_all(
        "els => els.filter(e => !e.hidden && getComputedStyle(e).display !== 'none').map(e => e.id)")
    assert visible == [f"tab-{tab}"], visible
    expect(page.locator("#word")).to_have_text(TABS[tab])
    page.wait_for_function("() => window.__dock && window.__dock.length >= 20")
    frames = page.evaluate("window.__dock")
    named = {t: (1 if t == tab else 0) for t in SLUGS}
    rest = frames[-1]["rise"]
    assert rest[tab] > 4 and all(abs(rest[t]) < .5 for t in SLUGS if t != tab), f"the icons at rest: {rest}"
    for n, frame in enumerate(frames):
        assert abs(frame["off"]) <= 1, f"frame {n}: the pill was not under {tab}: {frame}"
        assert frame["names"] == named, f"frame {n}: the names were not {tab}'s alone: {frame}"
        assert frame["glow"] == accent(page, tab), f"frame {n}: the pill was not in {tab}'s accent: {frame}"
        assert all(abs(frame["rise"][t] - rest[t]) < .5 for t in SLUGS), f"frame {n}: an icon moved: {frame}"
    pill_under(page, tab)


def test_clicking_a_tab_puts_its_address_in_the_bar(page, goto):
    goto("/ui")
    settled(page)
    for tab in ("speak", "jobs", "vocab", "satellites", "transcribe"):
        open_tab(page, tab)
        wait_for_address(page, SLUGS[tab])
        assert page.title().endswith(f"{TABS[tab]} · Calliope"), page.title()


def test_arrow_keys_change_the_address_like_a_click(page, goto):
    """The keyboard's ways of choosing a tab go through the handler a click
    does: one address each, one step of history, the masthead and the pill
    following."""
    goto("/ui")
    settled(page)
    page.locator("#tab-btn-transcribe").focus()
    before = history_length(page)
    page.keyboard.press("ArrowRight")
    wait_for_address(page, "/ui/speak")
    assert history_length(page) == before + 1
    expect(page.locator("#word")).to_have_text("Speak")
    pill_under(page, "speak")
    page.keyboard.press("End")
    wait_for_address(page, "/ui/satellites")
    pill_under(page, "satellites")
    page.keyboard.press("Home")
    wait_for_address(page, "/ui")
    expect(page.locator("#word")).to_have_text("Transcribe")
    pill_under(page, "transcribe")


def test_the_pill_follows_a_click_and_back_and_forward(page, goto):
    """Back and Forward choose the tab through the same handler, so the pill
    is under whatever tab the entry names, not under the one last clicked."""
    goto("/ui")
    settled(page)
    open_tab(page, "jobs")
    pill_under(page, "jobs")
    open_tab(page, "satellites")
    pill_under(page, "satellites")
    page.go_back()
    wait_for_address(page, "/ui/jobs")
    pill_under(page, "jobs")
    page.go_back()
    wait_for_address(page, "/ui")
    pill_under(page, "transcribe")
    page.go_forward()
    wait_for_address(page, "/ui/jobs")
    pill_under(page, "jobs")


# The pill the moment a click has landed: where it is, and what is moving it,
# the wash, the root and the chosen tab's icon. Clicked and read in one task,
# so no frame can pass in between.
CLICK_AND_LOOK = """name => {
  const tab = document.querySelector(`[role=tab][data-tab="${name}"]`);
  tab.click();
  const look = (""" + PILL + """)();
  const running = e => e.getAnimations().map(a => a.transitionProperty).sort();
  look.running = running(document.getElementById("highlight"));
  look.wash = running(document.getElementById("bloom"));
  look.root = running(document.documentElement);
  look.icon = running(tab.querySelector("svg"));
  return look;
}"""


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_the_pill_travels_and_under_reduced_motion_it_arrives(new_page, goto, motion):
    """The pill slides to the chosen tab on a transform transition; asked for
    less motion, it is under the tab the moment the click lands and the icon
    is set at its height, and the name still appears. The colour cross-fades
    either way: it is not motion, and it says which tab was chosen. It fades
    on the pill and the wash, and never on the root, where every element on
    the page inherited it and was restyled on every frame."""
    page = new_page(reduced_motion=motion)
    goto("/ui", target=page)
    settled(page)
    look = page.evaluate(CLICK_AND_LOOK, "satellites")
    if motion == "reduce":
        assert look["running"] == ["--glow-rgb"], f"the pill travels under reduced motion: {look}"
        assert abs(look["off"]) <= 1, f"the pill did not arrive: {look}"
        assert look["icon"] == ["color"], f"the icon rises under reduced motion: {look}"
    else:
        assert look["running"] == ["--glow-rgb", "transform"], f"the pill jumped instead of travelling: {look}"
        assert abs(look["off"]) > 100, f"the pill set off from somewhere else: {look}"
        assert look["icon"] == ["color", "transform"], f"the icon jumped instead of rising: {look}"
    assert look["wash"] == ["--glow-rgb"], f"the colour switched instead of cross-fading: {look}"
    assert look["root"] == [], f"the colour fades on the root, so the whole page restyles: {look}"
    pill_under(page, "satellites")
    label = page.locator('[role=tab][data-tab="satellites"] .label')
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=label.element_handle())


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
    # Everything is the default, so its address carries no filter at all.
    wait_for_address(page, f"/ui/jobs/{job}")
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
    page.locator("#jobfilter").select_option("failed")
    wait_for_address(page, "/ui/jobs?show=failed")
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


def test_a_tab_chosen_from_deep_in_another_opens_at_its_title(new_page, goto):
    """Scroll restoration is manual, so a tab chosen from far down another
    opened at the old offset, clamped to the new page, with the masthead that
    says where you are off screen. Back still has the old offset."""
    page = new_page((1000, 500))
    goto("/ui/satellites", target=page)
    settled(page)
    room = page.evaluate("document.documentElement.scrollHeight - innerHeight")
    assert room > 40, "the page is too short to scroll, so this proves nothing"
    page.evaluate(f"scrollTo(0, {min(160, room)})")
    page.wait_for_function("() => scrollY > 0")
    open_tab(page, "speak")
    page.wait_for_function("() => scrollY === 0")
    expect(page.locator("#word")).to_be_in_viewport()
    page.go_back()
    wait_for_address(page, "/ui/satellites")
    page.wait_for_function("() => scrollY > 0")


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
    # THE ENTRY BACK RETURNS TO, read off the bar once the voice list has
    # answered. Speak's entry gains ?voice= when the list answers while it is
    # showing, which a warm stack does: after test_smoke.py this failed on
    # every run, waiting for a bare /ui/speak that was no longer the entry.
    settled(page)
    speak = here(page)
    open_tab(page, "transcribe")
    page.locator("#url").fill("https://example.com/a-talk")
    page.locator("#resolve").click()
    expect(page.locator("#confirm")).to_have_attribute("open", "")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/ui/abandon")):
        page.go_back()
    wait_for_address(page, speak)
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


# ---- the dock, as drawn ---------------------------------------------------------------


def contrast(fore: list[float], back: list[float]) -> float:
    """WCAG 2.1 contrast between two sRGB colours given as 0-255 channels."""
    def lum(rgb):
        r, g, b = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4
                   for c in rgb[:3]]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    high, low = sorted((lum(fore), lum(back)), reverse=True)
    return (high + 0.05) / (low + 0.05)


RGB = "c => getComputedStyle(c)[PROP].match(/[\\d.]+/g).slice(0, 3).map(Number)"


def test_pointing_at_a_tab_names_it(page, goto):
    """Four of the five tabs are a glyph until chosen; resting the pointer on
    one shows its name, and moving away hides it again."""
    goto("/ui")
    settled(page)
    label = page.locator('[role=tab][data-tab="vocab"] .label')
    assert float(label.evaluate("e => getComputedStyle(e).opacity")) < 0.05
    page.locator('[role=tab][data-tab="vocab"]').hover()
    page.wait_for_function("e => getComputedStyle(e).opacity === '1'", arg=label.element_handle())
    # In its icon's grey: in the accent, with no pill behind it, it read as a
    # tab half chosen.
    grey = page.locator('[role=tab][data-tab="vocab"] svg').evaluate("e => getComputedStyle(e).color")
    assert label.evaluate("e => getComputedStyle(e).color") == grey, "the pointed-at name is in the accent"
    page.mouse.move(5, 5)
    page.wait_for_function("e => Number(getComputedStyle(e).opacity) < 0.05", arg=label.element_handle())


def test_the_dock_focus_ring_encloses_the_whole_tab(page, goto):
    """The keyboard's ring is drawn round the tab and its name, not inset over
    the icon and through the name, and the pill has come to rest under the
    chosen tab by the time the ring is read."""
    goto("/ui")
    settled(page)
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("Tab")
    tab = page.locator('[role=tab][data-tab="transcribe"]')
    expect(tab).to_be_focused()
    page.keyboard.press("ArrowRight")
    tab = page.locator('[role=tab][data-tab="speak"]')
    expect(tab).to_be_focused()
    page.wait_for_timeout(700)
    ring = tab.evaluate("""b => {
      const after = getComputedStyle(b, "::after"), box = b.getBoundingClientRect();
      const inset = k => parseFloat(after[k]);
      return { content: after.content, shadow: after.boxShadow, outline: getComputedStyle(b).outlineStyle,
               left: box.left + inset("left"), right: box.right - inset("right"),
               top: box.top + inset("top"), bottom: box.bottom - inset("bottom") };
    }""")
    assert ring["content"] not in ("none", "normal"), "no ring is drawn on the focused tab"
    assert "2px" in ring["shadow"] and ring["outline"] == "none"
    box = tab.locator(".label").bounding_box()
    assert ring["left"] <= box["x"] and box["x"] + box["width"] <= ring["right"], "the ring cuts the name"
    assert ring["top"] <= box["y"] and box["y"] + box["height"] <= ring["bottom"], "the ring cuts the name"
    assert tab.locator(".label").evaluate("e => getComputedStyle(e).opacity") == "1"


# Every element in the dock whose box reaches past the bar's, as "tag.class x,y wxh".
OUTSIDE_THE_BAR = """() => {
  const bar = document.getElementById("dock").getBoundingClientRect();
  return [...document.querySelectorAll("#dock *")]
    .map(e => [e, e.getBoundingClientRect()])
    .filter(([, r]) => r.width && r.height && (r.left < bar.left - .5 || r.right > bar.right + .5
                                              || r.top < bar.top - .5 || r.bottom > bar.bottom + .5))
    .map(([e, r]) => `${e.tagName.toLowerCase()}.${e.getAttribute("class")} `
                     + `${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)}`);
}"""


# For each tab, how far its group sits from the bar's centre line: the icon
# alone, or for the tab named, the icon and the name under it together.
CENTRED = """name => {
  const d = document.getElementById("dock").getBoundingClientRect(), mid = d.top + d.height / 2;
  return Object.fromEntries([...document.querySelectorAll("[role=tab]")].map(t => {
    const i = t.querySelector("svg").getBoundingClientRect(), l = t.querySelector(".label").getBoundingClientRect();
    return [t.dataset.tab, (t.dataset.tab === name ? (i.top + l.bottom) / 2 : (i.top + i.bottom) / 2) - mid];
  }));
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("form", ["desktop", "mobile"])
def test_the_dock_is_one_bar_with_everything_inside_it(new_page, goto, screenshot, form, scheme):
    """The selection used to be a bead that rose out of the bar through a
    notch in its outline. Nothing in the dock reaches past the bar's box now,
    and the chosen tab's name sits inside its pill -- Vocabulary, the longest,
    included, at phone width. Photographed with Satellites (whose badge
    counts Hallway) and Jobs selected."""
    page = new_page(form, scheme=scheme)
    goto("/ui", target=page)
    settled(page)
    for tab in ("satellites", "jobs", "vocab", "speak", "transcribe"):
        open_tab(page, tab)
        pill_under(page, tab)
        outside = page.evaluate(OUTSIDE_THE_BAR)
        assert not outside, f"{form} {scheme} {tab}: outside the bar: {outside}"
        name = page.locator(f'[role=tab][data-tab="{tab}"] .label').bounding_box()
        pill = page.locator("#highlight").bounding_box()
        assert pill["x"] <= name["x"] and name["x"] + name["width"] <= pill["x"] + pill["width"], \
            f"{tab}: the name overhangs its pill"
        assert pill["y"] <= name["y"] and name["y"] + name["height"] <= pill["y"] + pill["height"], \
            f"{tab}: the name is not inside its pill"
        # Every group is centred in the bar: a lone icon on the centre line,
        # and the chosen tab's icon and name taken together. The four lone
        # icons once rode 9px high over an empty strip.
        off = page.evaluate(CENTRED, tab)
        assert all(abs(v) < 1 for v in off.values()), f"{form} {scheme} {tab}: off the centre line: {off}"
        # A badge on the chosen tab stands clear of the pill's edges; it once
        # came within 1.35px of the top one and read as a collision.
        badge = page.locator(f'[role=tab][data-tab="{tab}"] .count')
        if badge.count() and badge.is_visible():
            b = badge.bounding_box()
            room = min(b["y"] - pill["y"], pill["y"] + pill["height"] - b["y"] - b["height"],
                       b["x"] - pill["x"], pill["x"] + pill["width"] - b["x"] - b["width"])
            assert room >= 4, f"{form} {scheme} {tab}: the badge is {room:.2f}px from its pill's edge"
        if tab in ("satellites", "jobs"):
            screenshot(f"dock-{form}-{scheme}-{tab}", target=page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_satellites_badge_is_a_readable_pill_in_both_themes(new_page, goto, scheme):
    """The plate is dark in both themes and the badge followed the page's ink,
    which in light was 1.01:1 on the plate. Hallway waits to be adopted, so the
    badge reads 1 from the first load."""
    page = new_page(scheme=scheme)
    goto("/ui", target=page)
    badge = page.locator("#satellitecount")
    expect(badge).to_have_text("1")
    ink = badge.evaluate(RGB.replace("PROP", '"color"'))
    ground = badge.evaluate(RGB.replace("PROP", '"backgroundColor"'))
    assert contrast(ink, ground) >= 7, f"{scheme}: the numeral is {contrast(ink, ground):.2f}:1 on its pill"


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
