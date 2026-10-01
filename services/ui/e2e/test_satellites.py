"""The Satellites tab in a browser, against the local stack: every control a
satellite's row has, the hub's own settings under the list (wake words and
what each does, Try a word, custom models, Activity, Telemetry, Firmware),
and what each says when something is missing, refused, gone or still coming.

THE STACK. A real hub, with three scripted satellites (fakes.py): Kitchen
(020000000001, an ESP32-Korvo) and Lounge (020000000002, a Raspberry Pi with a
phone playing to its AirPlay receiver) adopted, Hallway (020000000003, a Korvo)
waiting. The wake words are hey_jarvis and alexa, both commands that echo what
they heard. Home Assistant, a language model server and a webhook receiver are
fakes on the control port (fake.ha_url, fake.llm_url, fake.hook_url), so no
action a test sets up reaches past loopback.

WHAT IS ASSERTED. What the page sent (browser_log: the method, path and JSON
body as they left the page), what reached a device (fake.satellite_received:
the message the hub then sent the scripted satellite) or a fake service
(fake.requests), and what the page shows. Never a sound: the browser is muted,
and nothing here listens.

THE HUB'S STATE. A test that changes what the hub or a scripted satellite
holds asks for `changes`, which restarts the hub with nothing on it and the
satellites as they started; a test that reads the starting state asks for
`reads`, which restarts the hub only when a test before it changed something.
So no test depends on the order they run in, and the hub (under a second to
restart) is restarted only where it has to be. Setting a test up is done on
the hub directly over loopback, as another device or Home Assistant would,
and only what the test is about is done through the page.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect
from stack import UNCHECKED_SIGNATURE, WAKEWORD_CACHE
from test_routes import settled

KITCHEN, LOUNGE, HALLWAY = "020000000001", "020000000002", "020000000003"
KORVO = "esp32-korvo-v1.1"
FIXTURES = Path(__file__).resolve().parents[2] / "satellites" / "tests" / "fixtures"
HEY_JARVIS = "hey_jarvis_en_gb.wav"
HA_TOKEN = "ha-e2e-token"
LLM_KEY = "SATELLITES_LLM_API_KEY"
ROW_BUTTONS = "#tab-satellites :is(.row, .actions) > button:not(.link)"

# The page's own sentences (SAT_COPY in ui.html), where a test reads one whole.
NO_HUB = "This deployment has no satellite hub: voice-satellites is not running."
WHY_OFFLINE = "It is offline, so settings you change here are sent when it reconnects."
TRY_OFFLINE = "Offline, so nothing here reaches it."
DEV_OFFLINE = "Offline, so Reboot, Move and Update wait for it to reconnect."
NO_MUTE = "Keep Mute mic on one button other than Side, or a muted satellite could never be unmuted."
BAD_HOOK = "Write the webhook as a full web address, starting with https or http."
WW_CLEAN, WW_DIRTY = "No changes to save.", "Unsaved changes."


# ---- the hub's state -------------------------------------------------------------------

# True at the start: a file run before this one (test_live, test_routes) may
# have renamed or forgotten a satellite with conftest's fresh_hub, which
# restarts the hub before its test and not after, so the first test here that
# reads the starting state gets a restarted hub too.
_CHANGED = {"since_restart": True}


@pytest.fixture
def changes(fresh_hub):
    """A test that changes what the hub or a scripted satellite holds: a hub
    restarted with nothing on it, and the next `reads` test gets one too."""
    _CHANGED["since_restart"] = True
    return fresh_hub


@pytest.fixture
def reads(stack):
    """A test that reads the session's starting state: the hub is restarted
    only when a test before this one changed it."""
    if _CHANGED["since_restart"]:
        stack.restart_hub()
        _CHANGED["since_restart"] = False
    return stack


def hub(stack) -> httpx.Client:
    """The hub itself, over loopback: how a test sets up what another device
    or Home Assistant would have done."""
    return httpx.Client(base_url=stack.hub, timeout=30)


def put_words(stack, edit=None, ptt=None) -> dict:
    """PUT /satellites/wake-words with the hub's words as `edit` leaves them
    (it gets the list and changes it in place), and push-to-talk's entry when
    one is given."""
    with hub(stack) as h:
        view = h.get("/satellites/wake-words").json()
        words = [{k: v for k, v in w.items() if k not in ("state", "error")} for w in view["words"]]
        if edit:
            edit(words)
        r = h.put("/satellites/wake-words", json={"words": words} | ({"ptt": ptt} if ptt else {}))
        assert r.status_code == 200, r.text
        return r.json()


def word(words: list[dict], name: str) -> dict:
    return next(w for w in words if w["name"] == name)


def llm_action(base_url: str, model: str = "fake-small", **more) -> dict:
    return {"destination": {"type": "llm", "base_url": base_url, "model": model,
                            "api_key_env": LLM_KEY, "tools": []} | more,
            "reply_to": "same", "voice": None, "fallback": None}


def store_secret(stack, name: str, value: str | None) -> None:
    with hub(stack) as h:
        h.put("/satellites/secrets", json={"name": name, "value": value}).raise_for_status()


def image(version: str, size: int = 64 * 1024) -> bytes:
    """An ESP32 application image as far as the hub checks one: its first
    byte is 0xE9. The version is in the bytes, so each image has a SHA-256 of
    its own, which is how the hub keys them."""
    head = b"\xe9" + version.encode()
    return head + bytes((i * 7) & 0xFF for i in range(size - len(head)))


def upload_firmware(stack, version: str, model: str = KORVO, signed: bool = True) -> dict:
    params = {"model": model, "version": version} | ({"signature": UNCHECKED_SIGNATURE} if signed else {})
    with hub(stack) as h:
        r = h.post("/satellites/firmware", params=params, content=image(version),
                   headers={"Content-Type": "application/octet-stream"})
        assert r.status_code < 300, r.text
        return next(f for f in h.get("/satellites/firmware").json()["firmware"] if f["version"] == version)


def words_ready(page, stack) -> None:
    """Every wake word loaded on the hub: a hub just restarted is still
    loading them, and hears nothing until it has."""
    with hub(stack) as h:
        until(page, lambda: all(w["state"] == "ready" for w in h.get("/satellites/wake-words").json()["words"]),
              "the wake words loaded", seconds=20)


def custom_model() -> bytes:
    """An openWakeWord classifier the hub will load: hey_mycroft's, from the
    pinned models the stack copies into the hub, under a name of our own."""
    path = WAKEWORD_CACHE / "hey_mycroft_v0.1.onnx"
    if not path.exists():
        pytest.skip(f"no hey_mycroft model in {WAKEWORD_CACHE} to stand in for a custom one")
    return path.read_bytes()


# ---- the page --------------------------------------------------------------------------


def at(page, goto, address: str) -> None:
    """Load an address of this tab and wait until the router has resolved it
    and the hub's event stream is open. The stream opens after the first
    read of the lists, and an event the hub publishes before then is never
    sent to this page (the stream has no replay), so a test that makes one
    happen waits for it."""
    goto(address)
    settled(page)
    stream_open(page)


def stream_open(page) -> None:
    page.wait_for_function("() => !!SATELLITES.events && SATELLITES.events.readyState === 1")


def sat(page, nid: str):
    return page.locator(f'li.sat[data-id="{nid}"]')


def chip(page, nid: str):
    return sat(page, nid).locator(".sat-state").first


def line(page, nid: str):
    return sat(page, nid).locator(".sat-line").first


def section(page, nid: str, part: str):
    """One of a row's disclosures: sat-try, sat-buttons, sat-device or sat-ap."""
    return sat(page, nid).locator(f"details.sub.{part}")


def slider(page, nid: str, cfg: str):
    return sat(page, nid).locator(f'[data-cfg="{cfg}"]')


def readout(page, nid: str, cfg: str):
    return sat(page, nid).locator(f'.slider:has([data-cfg="{cfg}"]) output')


def ww(page, name: str):
    """A wake word's row, or push-to-talk's (`ptt`)."""
    return page.locator('#wwptt li[data-name="ptt"]' if name == "ptt"
                        else f'#wwlist li.ww[data-name="{name}"]')


def field(page, name: str, f: str):
    return ww(page, name).locator(f'[data-f="{f}"]')


def activity(page):
    """Activity's lines, newest first, as who and what (the time left out)."""
    return page.locator("#satelliteevents li > span")


def sent(browser_log, method: str, path: str) -> list[dict]:
    return browser_log.sent(method, path)


def bodies(browser_log, method: str, path: str) -> list:
    return [r.get("json") for r in browser_log.sent(method, path)]


def patches(browser_log, nid: str) -> list[dict]:
    return bodies(browser_log, "PATCH", rf"^/ui/api/satellites/{nid}$")


def until(page, condition, what: str, seconds: float = 10.0) -> None:
    """Wait on the test's side, looking every 100 ms."""
    ends = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < ends, f"never happened: {what}"
        page.wait_for_timeout(100)


def refreshed(page) -> None:
    """One read of the hub's three lists, drawn: what a poll does."""
    page.evaluate("() => satellitesRefresh()")


def focused(page, js: str = "e => e") -> object:
    return page.evaluate(f"() => ({js})(document.activeElement)")


def saved_words(browser_log) -> dict:
    """The last PUT /satellites/wake-words the page sent, by word."""
    body = bodies(browser_log, "PUT", r"^/ui/api/satellites/wake-words$")[-1]
    return {"words": {w["name"]: w for w in body["words"]}, "ptt": body.get("ptt")}


def lost_hub_is_not_a_fault(browser_log) -> None:
    """The hub going away is what such a test does: its answers while it is
    gone, and the event stream it cuts, are not faults of the page."""
    for status in (502, 503):
        browser_log.allow(status, r"^/ui/api/satellites")


def forget_cut_streams(browser_log) -> None:
    browser_log.failed[:] = [f for f in browser_log.failed if "/ui/api/satellites" not in f["url"]]


# ---- the list and the hub ------------------------------------------------------------


def test_the_satellites_tab_lists_the_pending_one_first_then_the_adopted_by_name(page, goto, reads):
    at(page, goto, "/ui/satellites")
    rows = page.locator("#satellitelist > li")
    expect(rows).to_have_count(3)
    assert rows.evaluate_all("els => els.map(e => e.dataset.id)") == [HALLWAY, KITCHEN, LOUNGE]
    expect(sat(page, HALLWAY)).to_have_class(re.compile(r"\bpending\b"))
    expect(chip(page, HALLWAY)).to_have_text("New")
    expect(line(page, HALLWAY)).to_have_text("Waiting to be adopted")
    expect(sat(page, HALLWAY).locator(".sat-name")).to_have_text(f"Satellite {HALLWAY}")
    for nid, name in ((KITCHEN, "Kitchen"), (LOUNGE, "Lounge")):
        expect(sat(page, nid).locator(".sat-name").first).to_have_text(name)
        expect(chip(page, nid)).to_have_text("Online")
        expect(line(page, nid)).to_have_text("Listens for hey jarvis, alexa", timeout=15_000)
    # Healthy is quiet: no chip colour on Online, and nothing on the health line.
    expect(chip(page, KITCHEN)).to_have_class("sat-state")
    expect(page.locator("#sathealth")).to_have_text("")


def test_the_tab_says_it_is_asking_the_hub_until_the_lists_answer(page, goto, reads):
    """The first read of the lists held unanswered: one sentence, and none of
    the hub's sections drawn empty under it."""
    held = []
    page.route(re.compile(r"/ui/api/satellites$"), lambda route: held.append(route))
    goto("/ui/satellites")
    asking = page.locator("#satellitesnone")
    until(page, lambda: held, "the list asked for")
    expect(asking).to_have_text("Asking the satellite hub what is on the network…")
    expect(asking).to_be_visible()
    expect(page.locator("#satellitesman")).to_be_hidden()
    for route in held:
        route.continue_()
    page.unroute(re.compile(r"/ui/api/satellites$"))
    expect(asking).to_be_hidden()
    expect(page.locator("#satellitelist > li")).to_have_count(3)


def test_a_hub_with_no_satellites_says_how_to_add_one(page, goto, stack, fake, changes):
    for key in ("kitchen", "lounge", "hallway"):
        fake.satellite_drop(key)
    with hub(stack) as h:
        for nid in (KITCHEN, LOUNGE, HALLWAY):
            h.post(f"/satellites/{nid}/forget").raise_for_status()
        assert h.get("/satellites").json()["satellites"] == []
    at(page, goto, "/ui/satellites")
    empty = page.locator("#satellitesempty")
    expect(empty).to_be_visible()
    expect(empty.locator(".sat-empty")).to_have_text("No satellites yet.")
    expect(empty.locator(".hint").first).to_have_text(
        "Join the Wi-Fi network a new satellite opens, named calliope-sat-XXXX, to set it up.")
    expect(page.locator("#satellitelist > li")).to_have_count(0)
    expect(page.locator("#satellitecount")).to_have_text("")


def test_a_satellite_that_drops_reads_offline_with_its_last_seen_time_and_is_counted(page, goto, fake, changes):
    at(page, goto, "/ui/satellites")
    expect(chip(page, KITCHEN)).to_have_text("Online")
    fake.satellite_drop("kitchen")
    expect(chip(page, KITCHEN)).to_have_text("Offline")
    expect(line(page, KITCHEN)).to_have_text(re.compile(r"^Last seen \d\d:\d\d$"))
    expect(page.locator("#sathealth")).to_have_text("1 offline")
    expect(sat(page, KITCHEN)).to_have_attribute("data-state", "offline")


def test_a_satellite_that_comes_back_clears_the_offline_count(page, goto, fake, changes):
    at(page, goto, "/ui/satellites")
    fake.satellite_drop("kitchen")
    expect(page.locator("#sathealth")).to_have_text("1 offline")
    fake.satellite_start("kitchen")
    expect(chip(page, KITCHEN)).to_have_text("Online", timeout=15_000)
    expect(page.locator("#sathealth")).to_have_text("")


def test_open_rows_are_remembered_across_a_reload(page, goto, stack, reads):
    """Two adopted satellites, so neither starts open; the one opened stays
    open on the next visit to the list, which is the viewer's own memory."""
    at(page, goto, "/ui/satellites")
    kitchen = sat(page, KITCHEN).locator(":scope > details.sat-row")
    lounge = sat(page, LOUNGE).locator(":scope > details.sat-row")
    expect(kitchen).not_to_have_attribute("open", "")
    kitchen.locator(":scope > summary").click()
    expect(kitchen).to_have_attribute("open", "")
    # Written on the disclosure's toggle event, which follows the click.
    until(page, lambda: json.loads(page.evaluate("localStorage.getItem('aiv.sat.open') || 'null'")) == [KITCHEN],
          "the open row remembered")
    at(page, goto, "/ui/satellites")
    expect(kitchen).to_have_attribute("open", "")
    expect(lounge).not_to_have_attribute("open", "")


def test_a_lone_adopted_satellite_starts_open(page, goto, stack, changes):
    """With nothing remembered and one satellite there is no list to scan, so
    its row starts open."""
    with hub(stack) as h:
        h.post(f"/satellites/{LOUNGE}/forget").raise_for_status()
    at(page, goto, "/ui/satellites")
    expect(sat(page, KITCHEN).locator(":scope > details.sat-row")).to_have_attribute("open", "")


HUB_LOST = re.compile(r"^The hub stopped answering, so this may be out of date: ")


def test_a_lost_hub_keeps_the_list_and_says_so(page, goto, stack, browser_log, changes):
    lost_hub_is_not_a_fault(browser_log)
    at(page, goto, "/ui/satellites")
    expect(chip(page, KITCHEN)).to_have_text("Online")
    stack.stop_hub()
    try:
        # The poll that finds it gone (made here rather than waited for: when
        # the next one comes is the test below).
        refreshed(page)
        expect(page.locator("#sathubfail")).to_have_text(HUB_LOST)
        # The last good copy stays, rows and sections with it.
        expect(page.locator("#satellitelist > li")).to_have_count(3)
        expect(page.locator("#satellitesman")).to_be_visible()
        expect(page.locator("#satellitesnone")).to_be_hidden()
    finally:
        stack.start_hub()
    refreshed(page)
    expect(page.locator("#sathubfail")).to_have_text("")
    forget_cut_streams(browser_log)


def test_a_lost_hub_that_answers_again_is_noticed_at_the_three_second_cadence(page, goto, stack, browser_log,
                                                                             changes):
    """The README's table: with the stream down, the open tab polls every
    3 s. So a hub gone is said within a few seconds of the stream going with
    it, and a hub back from a restart is noticed as soon, and the note that
    it stopped answering goes."""
    lost_hub_is_not_a_fault(browser_log)
    at(page, goto, "/ui/satellites")
    stack.stop_hub()
    try:
        page.wait_for_function("() => !SATELLITES.events", timeout=15_000)
        expect(page.locator("#sathubfail")).to_have_text(HUB_LOST, timeout=8_000)
    finally:
        stack.start_hub()
    forget_cut_streams(browser_log)
    expect(page.locator("#sathubfail")).to_have_text("", timeout=8_000)


def test_a_hub_that_is_down_at_load_says_one_sentence_and_no_empty_sections(page, goto, stack, browser_log, changes):
    lost_hub_is_not_a_fault(browser_log)
    stack.stop_hub()
    try:
        goto("/ui/satellites")
        expect(page.locator("#satellitesnone")).to_have_text(NO_HUB)
        expect(page.locator("#satellitesnone")).to_be_visible()
        expect(page.locator("#satellitesman")).to_be_hidden()
        for part in ("#satellitelist", "#sat-wakewords", "#sat-activity", "#sat-telemetry", "#sat-firmware"):
            expect(page.locator(part)).to_be_hidden()
    finally:
        stack.start_hub()
    forget_cut_streams(browser_log)


def test_a_muted_satellite_reads_muted_and_names_the_buttons_that_unmute_it(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    fake.satellite_status("kitchen", muted=True)
    expect(chip(page, KITCHEN)).to_have_text("Muted")
    kitchen = sat(page, KITCHEN)
    expect(kitchen.locator(".sat-why")).to_have_text(
        "It is muted on the device, and only its Rec button turns the microphones back on.")
    expect(kitchen.locator(".sat-tryhint")).to_have_text(
        "Muted on the device, so the ring shows red and Listen 5 s hears nothing.")
    expect(kitchen.locator('.sat-try [data-act="listen"]')).to_be_disabled()
    expect(kitchen.locator('.sat-try [data-act="identify"]')).to_be_disabled()
    # A choice, not a fault: not counted.
    expect(page.locator("#sathealth")).to_have_text("")


def test_a_satellite_that_cannot_listen_reads_not_listening_with_the_error(page, goto, fake, changes):
    """A Korvo whose microphones report 48 kHz: the hub will not listen to
    it, and says why."""
    fake.satellite_caps("kitchen", mic={"rate": 48000, "channels": 4, "format": "s16le"})
    at(page, goto, "/ui/satellites")
    expect(chip(page, KITCHEN)).to_have_text("Not listening", timeout=15_000)
    expect(line(page, KITCHEN)).to_have_text(
        "Not listening for wake words: the microphones run at 48000 Hz; listening needs 16000")
    expect(chip(page, KITCHEN)).to_have_class(re.compile(r"\bfailed\b"))
    expect(page.locator("#sathealth")).to_have_text("1 not listening")


# ---- adoption ----------------------------------------------------------------------------


def test_adopting_a_new_satellite_names_it_opens_its_row_and_focuses_it(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites")
    hallway = sat(page, HALLWAY)
    hallway.locator(".sat-adopt input").fill("Hallway")
    hallway.locator('.sat-adopt button[type="submit"]').click()
    row = hallway.locator(":scope > details.sat-row")
    expect(row).to_have_attribute("open", "")
    expect(hallway.locator(":scope > details.sat-row > .body > .sat-note")).to_have_text("Hallway adopted.")
    assert focused(page, "e => e.tagName === 'SUMMARY' && e.closest('li.sat').dataset.id") == HALLWAY
    assert bodies(browser_log, "POST", rf"^/ui/api/satellites/{HALLWAY}/adopt$") == [{"name": "Hallway"}]
    assert [m["name"] for m in fake.satellite_received("hallway", type="adopt")] == ["Hallway"]
    expect(chip(page, HALLWAY)).to_have_text("Online", timeout=15_000)
    # Adopted, it sorts by name among the others, and the address names it.
    assert page.locator("#satellitelist > li").evaluate_all(
        "els => els.map(e => e.dataset.id)") == [HALLWAY, KITCHEN, LOUNGE]
    expect(page).to_have_url(re.compile(r"/ui/satellites/hallway$"))


def test_enter_in_the_name_field_adopts(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites")
    name = sat(page, HALLWAY).locator(".sat-adopt input")
    name.fill("Porch")
    name.press("Enter")
    expect(sat(page, HALLWAY).locator(":scope > details.sat-row")).to_have_attribute("open", "")
    assert bodies(browser_log, "POST", rf"^/ui/api/satellites/{HALLWAY}/adopt$") == [{"name": "Porch"}]


def test_blink_on_a_pending_satellite_asks_it_to_identify(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites")
    hallway = sat(page, HALLWAY)
    expect(hallway.locator(".sat-pendhint")).to_have_text(
        "Blink lights its ring for five seconds, so you can tell identical boxes apart.")
    since = time.time()
    hallway.locator('[data-act="identify"]').click()
    expect(hallway.locator(":scope > .sat-note")).to_have_text("Blinking white for five seconds.")
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{HALLWAY}/identify$")) == 1
    until(page, lambda: fake.satellite_received("hallway", type="identify", since=since), "identify at the device")
    assert fake.satellite_received("hallway", type="identify", since=since)[-1]["seconds"] == 5


def test_a_pending_satellite_that_goes_away_offers_forget_without_asking(page, goto, fake, browser_log, dialogs,
                                                                       changes):
    dialogs()
    at(page, goto, "/ui/satellites")
    fake.satellite_drop("hallway")
    hallway = sat(page, HALLWAY)
    expect(chip(page, HALLWAY)).to_have_text("Seen")
    expect(line(page, HALLWAY)).to_have_text(re.compile(rf"^(Seen \d\d:\d\d, now offline|Now offline) · ID {HALLWAY}$"))
    expect(hallway.locator(".sat-newname")).to_be_hidden()
    expect(hallway.locator('.sat-adopt button[type="submit"]')).to_be_disabled()
    expect(hallway.locator(".sat-pendhint")).to_have_text("It has to be online to be adopted.")
    forget = hallway.locator('[data-act="dismiss"]')
    expect(forget).to_be_visible()
    forget.click()
    expect(hallway).to_have_count(0)
    assert dialogs.seen == [], "forgetting a satellite that was only seen asked a question"
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{HALLWAY}/forget$")) == 1


def test_the_dock_badge_counts_satellites_waiting_to_be_adopted(page, goto, changes):
    at(page, goto, "/ui")
    tab = page.locator("#tab-btn-satellites")
    expect(page.locator("#satellitecount")).to_have_text("1")
    expect(tab).to_have_attribute("aria-label", "Satellites, 1 waiting to be adopted")
    tab.click()
    sat(page, HALLWAY).locator(".sat-adopt input").fill("Hallway")
    sat(page, HALLWAY).locator('.sat-adopt button[type="submit"]').click()
    expect(page.locator("#satellitecount")).to_have_text("")
    expect(tab).not_to_have_attribute("aria-label", re.compile("."))


# ---- a row's levels, outputs and switches ------------------------------------------------


def test_volume_on_a_korvo_moves_in_twelve_steps_and_sends_a_percent(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen")
    volume = slider(page, KITCHEN, "volume")
    expect(volume).to_have_attribute("max", "12")
    expect(volume).to_have_attribute("data-steps", "12")
    expect(readout(page, KITCHEN, "volume")).to_have_text("7 of 12")  # 55 % is step 6.6
    since = time.time()
    volume.fill("3")
    until(page, lambda: patches(browser_log, KITCHEN), "the volume sent")
    assert patches(browser_log, KITCHEN) == [{"volume": 25}]
    expect(readout(page, KITCHEN, "volume")).to_have_text("3 of 12")
    expect(volume).to_have_attribute("aria-valuetext", "step 3 of 12")
    until(page, lambda: any(m.get("volume") == 25 for m in fake.satellite_received("kitchen", type="config",
                                                                                     since=since)),
          "the new volume at the device")


def test_volume_on_a_pi_is_a_percent(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge")
    volume = slider(page, LOUNGE, "volume")
    expect(volume).to_have_attribute("max", "100")
    expect(volume).not_to_have_attribute("data-steps", re.compile("."))
    expect(readout(page, LOUNGE, "volume")).to_have_text("40%")
    volume.fill("70")
    until(page, lambda: patches(browser_log, LOUNGE), "the volume sent")
    assert patches(browser_log, LOUNGE) == [{"volume": 70}]
    expect(readout(page, LOUNGE, "volume")).to_have_text("70%")
    expect(volume).to_have_attribute("aria-valuetext", "70 percent")


def drag(control, value: str) -> None:
    """A thumb moved and still held: the value changes and `input` fires, as
    each step of a drag does; `change` waits for the release."""
    control.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); }", value)


def release(control) -> None:
    control.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")


def test_mic_gain_and_brightness_are_sent_on_release_not_while_dragging(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen")
    for cfg, steps, sent_as, said in (("mic_gain_db", ("3", "9", "12"), 12, "12 dB"),
                                      ("brightness", ("50", "45", "40"), 40, "40%")):
        control = slider(page, KITCHEN, cfg)
        before = len(patches(browser_log, KITCHEN))
        for value in steps:
            drag(control, value)
        expect(readout(page, KITCHEN, cfg)).to_have_text(said)
        page.wait_for_timeout(300)
        assert len(patches(browser_log, KITCHEN)) == before, f"{cfg} was sent while it was being dragged"
        release(control)
        until(page, lambda: len(patches(browser_log, KITCHEN)) == before + 1, f"{cfg} sent on release")
        assert patches(browser_log, KITCHEN)[-1] == {cfg: sent_as}


def test_a_poll_does_not_move_a_slider_being_dragged(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/kitchen")
    volume = slider(page, KITCHEN, "volume")
    drag(volume, "2")
    # The device's own buttons turn it up meanwhile, and the hub takes it.
    fake.satellite_status("kitchen", volume=80, cause="local")
    page.wait_for_function(f"""() => {{
      const n = SATELLITES.list.find(x => x.id === "{KITCHEN}");
      return n && n.config && n.config.volume === 80 && !SATELLITES.polling; }}""")
    refreshed(page)
    expect(volume).to_have_value("2")
    expect(readout(page, KITCHEN, "volume")).to_have_text("2 of 12")
    # Let go without a change (a cancelled pointer): the next poll writes the device's value.
    volume.evaluate("el => el.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))")
    refreshed(page)
    expect(volume).to_have_value("10")
    expect(readout(page, KITCHEN, "volume")).to_have_text("10 of 12")


def test_a_volume_change_made_on_the_device_moves_the_slider(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen")
    fake.satellite_status("kitchen", volume=25, cause="local")
    expect(readout(page, KITCHEN, "volume")).to_have_text("3 of 12")
    expect(slider(page, KITCHEN, "volume")).to_have_value("3")
    assert patches(browser_log, KITCHEN) == [], "the page sent the device's own change back"
    expect(activity(page).first).to_have_text("Kitchen set on the device: volume 3 of 12")


def test_switching_the_speaker_off_greys_say_and_tone_and_says_why(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    kitchen = sat(page, KITCHEN)
    since = time.time()
    kitchen.locator('[data-cfg="speaker_enabled"]').uncheck()
    until(page, lambda: patches(browser_log, KITCHEN), "the switch sent")
    assert patches(browser_log, KITCHEN) == [{"speaker_enabled": False}]
    expect(kitchen.locator(".sat-sayform button[type=submit]")).to_be_disabled()
    expect(kitchen.locator('.sat-try [data-act="tone"]')).to_be_disabled()
    expect(kitchen.locator('.sat-try [data-act="listen"]')).to_be_enabled()
    expect(kitchen.locator(".sat-tryhint")).to_have_text("Turn Speaker on to use Say and Play a tone.")
    expect(chip(page, KITCHEN)).to_have_text("Speaker off")
    until(page, lambda: any(m.get("speaker_enabled") is False
                            for m in fake.satellite_received("kitchen", type="config", since=since)),
          "the switch at the device")


def test_switching_the_microphone_off_greys_listen(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    kitchen = sat(page, KITCHEN)
    kitchen.locator('[data-cfg="mic_enabled"]').uncheck()
    until(page, lambda: patches(browser_log, KITCHEN), "the switch sent")
    assert patches(browser_log, KITCHEN) == [{"mic_enabled": False}]
    expect(kitchen.locator('.sat-try [data-act="listen"]')).to_be_disabled()
    expect(kitchen.locator('.sat-try [data-act="tone"]')).to_be_enabled()
    expect(kitchen.locator(".sat-tryhint")).to_have_text("Turn Microphone on to use Listen 5 s.")
    expect(chip(page, KITCHEN)).to_have_text("Mic off")


def test_switching_the_lights_off_greys_blink_show_and_the_ring_set_up(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    kitchen = sat(page, KITCHEN)
    kitchen.locator('[data-cfg="lights_enabled"]').uncheck()
    until(page, lambda: patches(browser_log, KITCHEN), "the switch sent")
    assert patches(browser_log, KITCHEN) == [{"lights_enabled": False}]
    for act in ("identify", "lights"):
        expect(kitchen.locator(f'.sat-try [data-act="{act}"]')).to_be_disabled()
    expect(kitchen.locator(".sat-tryhint")).to_have_text("Turn Lights on to use Blink and Show.")
    expect(kitchen.locator('[data-act="ring"]')).to_be_disabled()
    expect(kitchen.locator(".sat-ringwhy")).to_have_text("Turn Lights on to set up the ring.")


def test_choosing_another_satellite_as_the_output_delegates_to_it(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen")
    output = sat(page, KITCHEN).locator("[data-output]")
    assert output.locator("option").evaluate_all("os => os.map(o => [o.value, o.textContent])") == [
        ["dev:", "Its own speaker"], [f"sat:{LOUNGE}", "Lounge"]]
    output.select_option(f"sat:{LOUNGE}")
    until(page, lambda: patches(browser_log, KITCHEN), "the output sent")
    assert patches(browser_log, KITCHEN) == [{"output_satellite": LOUNGE}]
    expect(sat(page, KITCHEN).locator(".sat-outhint")).to_have_text(
        "Replies, earcons, Say and tones play on Lounge.")
    output.select_option("dev:")
    until(page, lambda: len(patches(browser_log, KITCHEN)) == 2, "its own speaker sent")
    assert patches(browser_log, KITCHEN)[-1] == {"output_satellite": ""}
    expect(sat(page, KITCHEN).locator(".sat-outhint")).to_have_text("")


PWM = ("* The Pi's own jack is PWM from the processor, not a DAC. It plays 16-bit at 48 kHz only, "
       "with audible hiss and less detail than a DAC. For music, choose a USB DAC or a DAC HAT.")
USB_SINK = "alsa_output.usb-Generic_USB_Audio-00.analog-stereo"
JACK_SINK = "alsa_output.platform-bcm2835_audio.stereo-fallback"


def test_choosing_a_pi_output_device_sends_its_sink_and_shows_its_quality(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge")
    output = sat(page, LOUNGE).locator("[data-output]")
    assert output.locator("option").evaluate_all("os => os.map(o => [o.value, o.textContent])") == [
        ["dev:", "The system's default"],
        [f"dev:{USB_SINK}", "USB Audio Analog Stereo · USB DAC · up to 24-bit · 96 kHz"],
        [f"dev:{JACK_SINK}", "Built-in Audio Stereo * · PWM, not a DAC · nothing plugged in"],
        [f"sat:{KITCHEN}", "Kitchen"]]
    expect(output).to_have_value(f"dev:{USB_SINK}")
    quality = sat(page, LOUNGE).locator(".sat-outq")
    expect(quality).to_have_text("It takes 16 or 24-bit, 44.1 to 96 kHz.")
    since = time.time()
    output.select_option(f"dev:{JACK_SINK}")
    until(page, lambda: patches(browser_log, LOUNGE), "the output sent")
    assert patches(browser_log, LOUNGE) == [{"output_satellite": "", "audio_sink": JACK_SINK}]
    expect(quality).to_have_text(PWM)
    until(page, lambda: any(m.get("audio_sink") == JACK_SINK
                            for m in fake.satellite_received("lounge", type="config", since=since)),
          "the sink at the device")


def test_a_pi_microphone_input_can_be_chosen(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge")
    source = sat(page, LOUNGE).locator('[data-cfg="audio_source"]')
    expect(source).to_be_visible()
    assert source.locator("option").evaluate_all("os => os.map(o => [o.value, o.textContent])") == [
        ["", "The system's default"],
        ["alsa_input.usb-Generic_USB_Audio-00.analog-stereo", "USB Audio Analog Stereo"]]
    since = time.time()
    source.select_option("alsa_input.usb-Generic_USB_Audio-00.analog-stereo")
    until(page, lambda: patches(browser_log, LOUNGE), "the input sent")
    assert patches(browser_log, LOUNGE) == [{"audio_source": "alsa_input.usb-Generic_USB_Audio-00.analog-stereo"}]
    until(page, lambda: any(m.get("audio_source") for m in fake.satellite_received("lounge", type="config",
                                                                                   since=since)),
          "the input at the device")
    # A Korvo has no input to choose.
    expect(sat(page, KITCHEN).locator(".sat-audio")).to_be_hidden()


def test_change_wake_words_opens_and_focuses_the_wake_words_editor(page, goto, reads):
    at(page, goto, "/ui/satellites/kitchen")
    link = sat(page, KITCHEN).get_by_role("button", name="Change wake words for Kitchen")
    link.click()
    expect(page.locator("#sat-wakewords")).to_have_attribute("open", "")
    assert focused(page, "e => e.parentElement.id") == "sat-wakewords"
    expect(page).to_have_url(re.compile(r"/ui/satellites/wake-words$"))


# ---- AirPlay on the Pi -------------------------------------------------------------------


def apfacts(page) -> dict[str, str]:
    return dict(section(page, LOUNGE, "sat-ap").locator(".sat-apfacts").evaluate(
        "dl => [...dl.querySelectorAll('dt')].map(dt => [dt.textContent, dt.nextElementSibling.textContent])"))


def ap(page, act: str):
    return section(page, LOUNGE, "sat-ap").locator(f'[data-act="ap-{act}"]')


def test_airplay_shows_the_cover_and_what_is_playing(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/lounge/airplay")
    box = section(page, LOUNGE, "sat-ap")
    expect(box).to_have_attribute("open", "")
    cover = box.locator(".sat-apart")
    expect(cover).to_be_visible()
    assert cover.evaluate("img => img.naturalWidth") == 96
    expect(cover).to_have_attribute("src", re.compile(rf"/ui/api/satellites/{LOUNGE}/airplay/artwork\?v=[0-9a-f]{{64}}$"))
    expect(box.locator("summary .sum-note")).to_have_text("playing")
    expect(box.locator(".sat-aphint")).to_have_text("Phones and Macs list it as Lounge.")
    facts = apfacts(page)
    assert facts["Status"] == "Playing"
    assert facts["From"] == "Gabriel's iPhone"
    assert facts["Now playing"] == "So What · Miles Davis"
    assert facts["Album"] == "Kind of Blue"
    assert re.fullmatch(r"\d+:\d\d / 9:22", facts["Position"]), facts
    assert facts["Source"] == "ALAC, lossless · 44.1 kHz · 16-bit · stereo"
    expect(ap(page, "toggle")).to_have_text("Pause")
    for act in ("previous", "toggle", "next", "disconnect"):
        expect(ap(page, act)).to_be_enabled()
    art = [r for r in browser_log.responses if "/airplay/artwork" in r["path"]]
    assert art and art[-1]["status"] == 200


def test_pause_and_play_are_sent_to_the_phone_and_the_label_follows(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "toggle").click()
    expect(ap(page, "toggle")).to_have_text("Play")
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{LOUNGE}/airplay/pause$")) == 1
    assert [m["command"] for m in fake.satellite_received("lounge", type="airplay_command")] == ["pause"]
    expect(section(page, LOUNGE, "sat-ap").locator("summary .sum-note")).to_have_text("paused")
    assert apfacts(page)["Status"] == "Paused"
    ap(page, "toggle").click()
    expect(ap(page, "toggle")).to_have_text("Pause")
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{LOUNGE}/airplay/play$")) == 1


def test_next_and_previous_are_sent(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "next").click()
    until(page, lambda: apfacts(page).get("Now playing") == "Blue in Green · Miles Davis", "the next track")
    ap(page, "previous").click()
    until(page, lambda: apfacts(page).get("Now playing") == "So What · Miles Davis", "the track before")
    assert [r["path"].rsplit("/", 1)[1] for r in sent(browser_log, "POST", rf"/{LOUNGE}/airplay/")] == [
        "next", "previous"]
    assert [m["command"] for m in fake.satellite_received("lounge", type="airplay_command")] == ["next", "previous"]


def test_disconnect_asks_first_naming_whose_music_it_ends(page, goto, browser_log, dialogs, changes):
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "disconnect").click()
    until(page, lambda: len(dialogs.seen) == 1, "the question")
    assert dialogs.seen[0] == ("confirm", "Disconnect Gabriel's iPhone from Lounge? The music stops playing here.")
    page.wait_for_timeout(300)
    assert not sent(browser_log, "POST", r"/airplay/disconnect$"), "a No still disconnected the phone"
    ap(page, "disconnect").click()
    box = section(page, LOUNGE, "sat-ap")
    expect(box.locator(".sat-apctl")).to_be_hidden()
    expect(box.locator(".sat-apart")).to_be_hidden()
    expect(box.locator("summary .sum-note")).to_have_text("waiting")
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{LOUNGE}/airplay/disconnect$")) == 1


def test_the_focus_goes_to_the_airplay_summary_when_disconnect_hides_the_controls(page, goto, browser_log,
                                                                               dialogs, changes):
    """The phone takes its time to answer, as a real one does: the request is
    held a moment, as the hub holds it until the phone has answered."""
    dialogs()
    held = []
    page.route(re.compile(r"/airplay/disconnect$"), lambda route: held.append(route))
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "disconnect").focus()
    page.keyboard.press("Enter")
    until(page, lambda: held, "Disconnect sent")
    page.wait_for_timeout(300)
    held[0].continue_()
    expect(section(page, LOUNGE, "sat-ap").locator(".sat-apctl")).to_be_hidden()
    where = focused(page, "e => e.tagName + ' ' + (e.parentElement && e.parentElement.className)")
    assert where == "SUMMARY sub sat-ap", f"the focus is on {where}"


def test_a_phone_that_refuses_a_command_is_reported_under_the_controls(page, goto, fake, browser_log, changes):
    browser_log.allow(502, rf"/{LOUNGE}/airplay/pause$")
    fake.satellite_airplay("lounge", on_command="refuse")
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "toggle").click()
    note = section(page, LOUNGE, "sat-ap").locator(".sat-apnote")
    expect(note).to_have_text("the phone did not take pause (403: the phone answered 403)")
    expect(note.locator(".note")).to_have_class(re.compile(r"\bbad\b"))
    expect(ap(page, "toggle")).to_have_text("Pause")


def test_a_phone_that_does_not_answer_is_reported_as_a_timeout(page, goto, fake, browser_log, changes):
    browser_log.allow(504, rf"/{LOUNGE}/airplay/next$")
    fake.satellite_airplay("lounge", on_command="ignore")
    at(page, goto, "/ui/satellites/lounge/airplay")
    ap(page, "next").click()
    expect(ap(page, "next")).to_have_text("Skipping…")
    expect(section(page, LOUNGE, "sat-ap").locator(".sat-apnote")).to_have_text(
        f"satellite {LOUNGE} did not answer next within 6 s", timeout=15_000)
    expect(ap(page, "next")).to_have_text("Next")


def test_turning_airplay_off_hides_the_controls_and_the_cover(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge/airplay")
    box = section(page, LOUNGE, "sat-ap")
    since = time.time()
    box.locator('[data-cfg="airplay_enabled"]').uncheck()
    until(page, lambda: patches(browser_log, LOUNGE), "AirPlay off sent")
    assert patches(browser_log, LOUNGE) == [{"airplay_enabled": False}]
    expect(box.locator(".sat-apctl")).to_be_hidden()
    expect(box.locator(".sat-apart")).to_be_hidden()
    expect(box.locator("summary .sum-note")).to_have_text("off")
    expect(box.locator(".sat-apfacts dt")).to_have_count(0)
    until(page, lambda: any(m.get("airplay_enabled") is False
                            for m in fake.satellite_received("lounge", type="config", since=since)),
          "AirPlay off at the device")


def test_renaming_the_airplay_receiver_sends_its_new_name(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/lounge/airplay")
    box = section(page, LOUNGE, "sat-ap")
    name = box.locator('[data-cfg="airplay_name"]')
    expect(name).to_have_attribute("placeholder", "Lounge")
    since = time.time()
    name.fill("Living room")
    name.press("Tab")
    until(page, lambda: patches(browser_log, LOUNGE), "the name sent")
    assert patches(browser_log, LOUNGE) == [{"airplay_name": "Living room"}]
    expect(box.locator(".sat-aphint")).to_have_text("Phones and Macs list it as Living room.")
    until(page, lambda: any(m.get("airplay_name") == "Living room"
                            for m in fake.satellite_received("lounge", type="config", since=since)),
          "the name at the device")


def test_an_idle_receiver_shows_no_controls(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/lounge/airplay")
    fake.satellite_airplay("lounge", state="idle")
    box = section(page, LOUNGE, "sat-ap")
    expect(box.locator("summary .sum-note")).to_have_text("waiting")
    expect(box.locator(".sat-apctl")).to_be_hidden()
    expect(box.locator(".sat-apart")).to_be_hidden()
    expect(box.locator(".sat-aphint")).to_have_text("Phones and Macs list it as Lounge.")
    expect(box.locator(".sat-apfacts dt")).to_have_count(0)


# ---- Try it ------------------------------------------------------------------------------


def try_it(page, nid: str):
    return section(page, nid, "sat-try")


def frames_at(fake, key: str) -> int:
    """How many audio frames the hub has sent one scripted satellite."""
    return sum(next(s for s in fake.satellites() if s["key"] == key)["audio_frames"].values())


def test_say_sends_the_text_to_the_satellite(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    box = try_it(page, KITCHEN)
    before, seq = frames_at(fake, "kitchen"), fake.last_seq()
    box.locator(".sat-sayform input").fill("Dinner is ready")
    box.locator(".sat-sayform input").press("Enter")
    until(page, lambda: sent(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/say$"), "Say sent")
    assert bodies(browser_log, "POST", rf"/{KITCHEN}/say$") == [{"text": "Dinner is ready"}]
    until(page, lambda: fake.requests(backend="tts", path=r"^/v1/audio/speech$", since=seq), "the speech asked for")
    assert fake.requests(backend="tts", path=r"^/v1/audio/speech$", since=seq)[-1]["json"]["input"] == "Dinner is ready"
    until(page, lambda: frames_at(fake, "kitchen") > before, "the speech at the device")
    expect(box.locator(".sat-sayform button[type=submit]")).to_have_text("Say")


def test_say_with_nothing_typed_says_so_and_sends_nothing(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/try")
    box = try_it(page, KITCHEN)
    box.locator(".sat-sayform button[type=submit]").click()
    expect(box.locator(":scope > .body > .sat-note")).to_have_text("Type something for it to say first.")
    assert not sent(browser_log, "POST", r"/say$")


def test_play_a_tone_sends_a_one_second_440_hertz_tone(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    try_it(page, KITCHEN).locator('[data-act="tone"]').click()
    until(page, lambda: sent(browser_log, "POST", rf"/{KITCHEN}/tone$"), "the tone sent")
    assert bodies(browser_log, "POST", rf"/{KITCHEN}/tone$") == [{"frequency": 440, "seconds": 1}]
    expect(try_it(page, KITCHEN).locator('[data-act="tone"]')).to_have_text("Play a tone")


def test_blink_on_a_korvo_and_chime_on_a_pi(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/try")
    since = time.time()
    blink = try_it(page, KITCHEN).locator('[data-act="identify"]')
    expect(blink).to_have_text("Blink")
    blink.click()
    expect(try_it(page, KITCHEN).locator(":scope > .body > .sat-note")).to_have_text("Blinking white for five seconds.")
    until(page, lambda: fake.satellite_received("kitchen", type="identify", since=since), "Blink at the Korvo")
    at(page, goto, "/ui/satellites/lounge/try")
    chime = try_it(page, LOUNGE).locator('[data-act="identify"]')
    expect(chime).to_have_text("Chime")
    expect(try_it(page, LOUNGE).locator(".sat-lights")).to_be_hidden()
    chime.click()
    until(page, lambda: fake.satellite_received("lounge", type="identify", since=since), "Chime at the Pi")
    assert len(sent(browser_log, "POST", rf"/{LOUNGE}/identify$")) == 1


def test_listen_records_five_seconds_into_a_player_that_does_not_start_itself(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/try")
    box = try_it(page, KITCHEN)
    fake.satellite_mic("kitchen", 8)
    box.locator('[data-act="listen"]').click()
    note = box.locator(":scope > .body > .sat-note")
    expect(note).to_have_text("Recording five seconds…")
    player = box.locator("audio")
    expect(player).to_be_visible(timeout=20_000)
    expect(note).to_have_text("Four channels: the speaker loopback, then the three microphones.")
    assert player.evaluate("a => a.src.startsWith('blob:') && a.paused && !a.autoplay")
    listens = sent(browser_log, "GET", rf"^/ui/api/satellites/{KITCHEN}/listen$")
    assert [r["query"] for r in listens] == ["seconds=5"]


def test_stop_asks_the_satellite_to_stop_talking(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/try")
    since = time.time()
    try_it(page, KITCHEN).locator('[data-act="stop"]').click()
    until(page, lambda: sent(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/flush$"), "Stop sent")
    until(page, lambda: fake.satellite_received("kitchen", type="flush", since=since), "flush at the device")
    expect(try_it(page, KITCHEN).locator('[data-act="stop"]')).to_have_text("Stop")


def test_show_sends_the_chosen_colour_and_pattern(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/try")
    box = try_it(page, KITCHEN)
    box.locator("input[type=color]").fill("#00ff40")
    box.locator(".sat-pattern select").select_option("spin")
    since = time.time()
    box.locator('[data-act="lights"]').click()
    until(page, lambda: sent(browser_log, "POST", rf"/{KITCHEN}/lights$"), "Show sent")
    assert bodies(browser_log, "POST", rf"/{KITCHEN}/lights$") == [
        {"mode": "spin", "color": [0, 255, 64], "brightness": 96}]
    until(page, lambda: fake.satellite_received("kitchen", type="lights", since=since), "the lights at the device")
    assert fake.satellite_received("kitchen", type="lights", since=since)[-1]["mode"] == "spin"


def test_every_try_it_control_is_greyed_while_the_satellite_is_offline_with_the_reason(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/kitchen/try")
    fake.satellite_drop("kitchen")
    expect(chip(page, KITCHEN)).to_have_text("Offline")
    box = try_it(page, KITCHEN)
    for control in (".sat-sayform button[type=submit]", '[data-act="identify"]', '[data-act="tone"]',
                    '[data-act="listen"]', '[data-act="stop"]', '[data-act="lights"]'):
        expect(box.locator(control)).to_be_disabled()
    expect(box.locator(".sat-tryhint")).to_have_text(TRY_OFFLINE)
    expect(sat(page, KITCHEN).locator(".sat-why")).to_have_text(WHY_OFFLINE)
    expect(sat(page, KITCHEN).locator(".sat-devhint")).to_have_text(DEV_OFFLINE)


# ---- Buttons -------------------------------------------------------------------------------


def pick(page, button: str, edge: str = "press"):
    return section(page, KITCHEN, "sat-buttons").locator(f'select[data-btn="{button}"][data-edge="{edge}"]')


def hook_box(page, button: str, edge: str = "press"):
    return page.locator(f"#{pick(page, button, edge).get_attribute('data-url')}")


def buttons_note(page):
    return section(page, KITCHEN, "sat-buttons").locator(".sat-note")


DEFAULT_BUTTONS = {"rec": {"press": "mute"}, "vol_up": {"press": "volume_up"},
                   "vol_down": {"press": "volume_down"}, "play": {"press": "ptt"}, "set": {"press": "stop"}}


def test_changing_a_buttons_action_sends_the_whole_mapping(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/buttons")
    names = section(page, KITCHEN, "sat-buttons").locator(".sat-btns .bn").all_text_contents()
    assert names == ["Rec", "Mode", "Play", "Set", "Vol −", "Vol +", "Side"]
    since = time.time()
    pick(page, "mode").select_option("lights")
    until(page, lambda: patches(browser_log, KITCHEN), "the mapping sent")
    assert patches(browser_log, KITCHEN) == [{"buttons": DEFAULT_BUTTONS | {"mode": {"press": "lights"}}}]
    expect(section(page, KITCHEN, "sat-buttons").locator("summary .sum-note")).to_have_text(
        "Mode switches the lights, Play talks, Set stops")
    until(page, lambda: any("button_actions" in m for m in fake.satellite_received("kitchen", type="config",
                                                                                     since=since)),
          "the actions the device runs itself, at the device")


def test_choosing_webhook_asks_for_its_address_before_saving(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/buttons")
    pick(page, "mode").select_option("webhook")
    address = hook_box(page, "mode")
    expect(address).to_be_visible()
    expect(address).to_be_focused()
    page.wait_for_timeout(300)
    assert patches(browser_log, KITCHEN) == [], "a webhook with no address was sent"
    address.fill(fake.hook_url("kitchen-mode"))
    address.press("Tab")
    until(page, lambda: patches(browser_log, KITCHEN), "the mapping sent")
    assert patches(browser_log, KITCHEN)[-1]["buttons"]["mode"] == {"press": f"webhook:{fake.hook_url('kitchen-mode')}"}
    expect(section(page, KITCHEN, "sat-buttons").locator("summary .sum-note")).to_have_text(
        "Mode calls a webhook, Play talks, Set stops")


def test_a_webhook_address_that_is_not_a_url_is_refused_before_sending(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/buttons")
    pick(page, "mode").select_option("webhook")
    hook_box(page, "mode").fill("hooks.example.com/press")
    hook_box(page, "mode").press("Tab")
    expect(buttons_note(page)).to_have_text(BAD_HOOK)
    page.wait_for_timeout(300)
    assert patches(browser_log, KITCHEN) == []


def test_a_mapping_without_a_mute_is_refused_before_sending(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/buttons")
    pick(page, "rec").select_option("none")
    expect(buttons_note(page)).to_have_text(NO_MUTE)
    # A mute on Side alone does not count: a stock board does not wire it.
    pick(page, "key1").select_option("mute")
    expect(buttons_note(page)).to_have_text(NO_MUTE)
    page.wait_for_timeout(300)
    assert patches(browser_log, KITCHEN) == []
    pick(page, "mode").select_option("mute")
    until(page, lambda: patches(browser_log, KITCHEN), "a mapping with a mute sent")
    mapping = patches(browser_log, KITCHEN)[-1]["buttons"]
    assert "rec" not in mapping and mapping["mode"] == {"press": "mute"} and mapping["key1"] == {"press": "mute"}
    expect(buttons_note(page)).to_have_text("")


def test_the_buttons_summary_names_only_what_differs_from_the_keys(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen")
    summary = section(page, KITCHEN, "sat-buttons").locator("summary .sum-note")
    # Rec mutes and the volume pair set the volume: what the keys say goes unsaid.
    expect(summary).to_have_text("Play talks, Set stops")
    section(page, KITCHEN, "sat-buttons").locator(":scope > summary").click()
    pick(page, "vol_up").select_option("volume_down")
    until(page, lambda: patches(browser_log, KITCHEN), "the mapping sent")
    expect(summary).to_have_text("Play talks, Set stops, Vol + turns it down")


def test_a_button_pressed_on_the_device_appears_in_activity(page, goto, fake, reads):
    at(page, goto, "/ui/satellites/activity")
    fake.satellite_button("kitchen", "mode", "press")
    expect(activity(page).first).to_have_text("Kitchen Mode pressed")
    expect(page.locator("#evnone")).to_be_hidden()
    expect(sat(page, KITCHEN).locator(".sat-last")).to_have_text(re.compile(r"^Last event: Mode pressed, "))


def test_a_webhook_button_press_reaches_the_webhook(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/buttons")
    pick(page, "mode").select_option("webhook")
    hook_box(page, "mode").fill(fake.hook_url("kitchen-mode"))
    hook_box(page, "mode").press("Tab")
    until(page, lambda: patches(browser_log, KITCHEN), "the mapping sent")
    page.wait_for_function(f"""() => {{
      const n = SATELLITES.list.find(x => x.id === "{KITCHEN}");
      return n && n.config.buttons.mode; }}""")
    seq = fake.last_seq()
    fake.satellite_button("kitchen", "mode", "press")
    until(page, lambda: fake.requests(backend="hook", since=seq), "the webhook called")
    call = fake.requests(backend="hook", since=seq)[-1]
    assert (call["method"], call["path"]) == ("POST", "/__hook/kitchen-mode")
    assert call["json"] == {"satellite": "Kitchen", "satellite_id": KITCHEN, "button": "mode",
                            "action": "press", "held_ms": None}


# ---- Device --------------------------------------------------------------------------------


def facts(page, nid: str) -> dict[str, str]:
    return dict(section(page, nid, "sat-device").locator(".facts").evaluate(
        "dl => [...dl.querySelectorAll('dt')].map(dt => [dt.textContent, dt.nextElementSibling.textContent])"))


def device(page, nid: str = KITCHEN):
    return section(page, nid, "sat-device")


def test_device_facts_show_model_firmware_address_signal_and_id(page, goto, reads):
    at(page, goto, "/ui/satellites/kitchen/device")
    kitchen = facts(page, KITCHEN)
    assert list(kitchen) == ["Model", "Firmware", "Address", "Wi-Fi signal", "Output", "Connected", "ID"]
    assert kitchen["Model"] == KORVO and kitchen["Firmware"] == "v0.2.0"
    assert kitchen["Address"] == "127.0.0.1"
    assert kitchen["Wi-Fi signal"] == "−58 dBm, strong"
    assert kitchen["Output"] == "Not known until it plays something"
    assert re.fullmatch(r"since \d\d:\d\d", kitchen["Connected"]), kitchen
    assert kitchen["ID"] == KITCHEN
    expect(device(page).locator("summary .sum-note")).to_have_text("v0.2.0")
    at(page, goto, "/ui/satellites/lounge/device")
    lounge = facts(page, LOUNGE)
    assert lounge["Model"] == "raspberry-pi" and lounge["Firmware"] == "v0.1.2-193-g0ae5ed7"
    assert lounge["Wi-Fi signal"] == "−61 dBm, fair"
    assert lounge["Temperature"] == "48.5 °C" and lounge["Power supply"] == "Good"
    assert lounge["ID"] == LOUNGE
    # No ring on the Pi, so none of its set-up.
    expect(device(page, LOUNGE).locator(".sat-ringparts")).to_be_hidden()


def test_rename_sends_the_new_name_moves_the_row_and_says_so(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/device")
    name = device(page).locator(".sat-rename input")
    expect(name).to_have_value("Kitchen")
    since = time.time()
    name.fill("Pantry")
    device(page).locator(".sat-rename button[type=submit]").click()
    expect(device(page).locator(".sat-note")).to_have_text("Kitchen is now called Pantry.")
    assert patches(browser_log, KITCHEN) == [{"name": "Pantry"}]
    expect(sat(page, KITCHEN).locator(".sat-name").first).to_have_text("Pantry")
    assert page.locator("#satellitelist > li").evaluate_all(
        "els => els.map(e => e.dataset.id)") == [HALLWAY, LOUNGE, KITCHEN]
    expect(name).to_be_focused()
    until(page, lambda: any(m.get("name") == "Pantry"
                            for m in fake.satellite_received("kitchen", type="config", since=since)),
          "the name at the device")


def test_rename_with_an_empty_name_says_so_and_keeps_the_focus(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/device")
    name = device(page).locator(".sat-rename input")
    name.fill("   ")
    device(page).locator(".sat-rename button[type=submit]").click()
    expect(device(page).locator(":scope > .body > .sat-note")).to_have_text("Type a name first.")
    expect(name).to_be_focused()
    assert patches(browser_log, KITCHEN) == []


def lit(body: dict) -> list[int]:
    """Which LEDs a set-up draw lit, brightest first."""
    shown = [(max(p), i) for i, p in enumerate(body["pixels"]) if max(p) > 6]
    return [i for _, i in sorted(shown, key=lambda s: -s[0])]


def lights_sent(browser_log) -> list[dict]:
    return bodies(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/lights$")


def test_setting_up_the_ring_lights_one_led_moves_it_and_saves_top_and_direction(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/device")
    box = device(page)
    start = box.locator('[data-act="ring"]')
    start.click()
    expect(start).to_have_attribute("aria-expanded", "true")
    expect(box.locator(".sat-ringsay")).to_have_text(
        "Move the lit LED with the arrows until it is at 12 o'clock, then press That's the top.")
    expect(box.locator('[data-act="ring-next"]')).to_be_focused()
    until(page, lambda: len(lights_sent(browser_log)) == 1, "the first LED lit")
    assert lights_sent(browser_log)[0]["mode"] == "pixels" and lit(lights_sent(browser_log)[0]) == [0]
    box.locator('[data-act="ring-next"]').click()
    box.locator('[data-act="ring-next"]').click()
    until(page, lambda: len(lights_sent(browser_log)) == 3, "two moves drawn")
    assert lit(lights_sent(browser_log)[-1]) == [2]
    box.locator('[data-act="ring-top"]').click()
    expect(box.locator(".sat-ringsay")).to_have_text(
        "The bar runs from the brightest LED. Which way does it go, as you look at it?")
    expect(box.locator('[data-act="ring-cw"]')).to_be_focused()
    until(page, lambda: len(lights_sent(browser_log)) == 4, "the bar drawn")
    assert lit(lights_sent(browser_log)[-1]) == [2, 3, 4]
    box.locator('[data-act="ring-cw"]').click()
    expect(box.locator(".sat-note")).to_have_text("Saved: the volume bar starts at 12 o'clock and fills clockwise.")
    assert patches(browser_log, KITCHEN) == [{"ring_top": 2, "ring_upside_down": False}]
    until(page, lambda: lights_sent(browser_log)[-1] == {"mode": "off"}, "the ring put out")
    expect(box.locator(".sat-ringset")).to_be_hidden()
    expect(start).to_have_attribute("aria-expanded", "false")
    expect(start).to_be_focused()
    expect(box.locator('[data-cfg="ring_top"]')).to_have_value("2")


def test_escape_cancels_the_ring_set_up_and_puts_the_ring_out(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/kitchen/device")
    box = device(page)
    box.locator('[data-act="ring"]').click()
    until(page, lambda: len(lights_sent(browser_log)) == 1, "the first LED lit")
    page.keyboard.press("Escape")
    expect(box.locator(".sat-ringset")).to_be_hidden()
    until(page, lambda: lights_sent(browser_log)[-1] == {"mode": "off"}, "the ring put out")
    expect(box.locator('[data-act="ring"]')).to_be_focused()
    assert patches(browser_log, KITCHEN) == [], "a cancelled set-up saved something"


def test_the_ring_set_up_refuses_while_muted(page, goto, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/kitchen/device")
    fake.satellite_status("kitchen", muted=True)
    expect(chip(page, KITCHEN)).to_have_text("Muted")
    expect(device(page).locator('[data-act="ring"]')).to_be_disabled()
    expect(device(page).locator(".sat-ringwhy")).to_have_text(
        "Unmute it with its button to set up the ring; muted, the ring shows red.")
    assert lights_sent(browser_log) == []


def test_the_ring_set_up_cancels_itself_after_two_idle_minutes(page, goto, browser_log, reads):
    page.clock.install()
    at(page, goto, "/ui/satellites/kitchen/device")
    box = device(page)
    box.locator('[data-act="ring"]').click()
    until(page, lambda: len(lights_sent(browser_log)) == 1, "the first LED lit")
    page.clock.run_for(119_000)
    page.wait_for_timeout(200)
    expect(box.locator(".sat-ringset")).to_be_visible()
    page.clock.run_for(2_000)
    expect(box.locator(".sat-ringset")).to_be_hidden()
    until(page, lambda: lights_sent(browser_log)[-1] == {"mode": "off"}, "the ring put out")


def test_reboot_asks_first_and_the_row_reads_restarting_not_offline(page, goto, fake, browser_log, dialogs,
                                                                    changes):
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    device(page).locator('[data-act="reboot"]').click()
    expect(device(page).locator(":scope > .body > .sat-note")).to_have_text(
        "Kitchen is rebooting; it is back in about ten seconds.")
    assert dialogs.seen == [("confirm", "Reboot Kitchen? It is back in about ten seconds.")]
    assert len(sent(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/reboot$")) == 1
    seen, health = set(), set()

    def back() -> bool:
        seen.add(chip(page, KITCHEN).text_content())
        health.add(page.locator("#sathealth").text_content())
        return "Restarting" in seen and seen and chip(page, KITCHEN).text_content() == "Online"

    until(page, back, "Restarting, then Online", seconds=20)
    assert "Offline" not in seen, f"a reboot read as Offline: {seen}"
    assert "1 offline" not in health, f"a reboot was counted offline: {health}"


def move_box(page):
    return device(page).locator(".sat-moveform input")


def test_move_to_another_hub_checks_the_address_asks_and_sends_it(page, goto, fake, browser_log, dialogs, reads):
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    move = device(page).locator('[data-act="move"]')
    move.click()
    expect(move).to_have_attribute("aria-expanded", "true")
    expect(move_box(page)).to_be_focused()
    move_box(page).fill("http://hub.example.com:8443")
    move_box(page).press("Enter")
    note = device(page).locator(":scope > .body > .sat-note")
    expect(note).to_have_text("Write the address as ws://host:port or wss://host:port.")
    assert dialogs.seen == [] and not sent(browser_log, "POST", r"/set-hub$")
    since = time.time()
    move_box(page).fill("wss://hub.example.com:8443")
    move_box(page).press("Enter")
    expect(note).to_have_text("Moving. Forget it here once the other hub has adopted it.")
    assert dialogs.seen == [("confirm", "Move Kitchen to wss://hub.example.com:8443? "
                                        "It reboots and waits to be adopted by that hub.")]
    assert bodies(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/set-hub$") == [
        {"url": "wss://hub.example.com:8443"}]
    until(page, lambda: fake.satellite_received("kitchen", type="set_hub", since=since), "the address at the device")
    expect(move_box(page)).to_be_hidden()
    expect(move).to_be_focused()


def test_escape_closes_the_move_form_and_returns_focus(page, goto, reads):
    at(page, goto, "/ui/satellites/kitchen/device")
    move = device(page).locator('[data-act="move"]')
    move.click()
    expect(move_box(page)).to_be_focused()
    move_box(page).press("Escape")
    expect(move_box(page)).to_be_hidden()
    expect(move).to_have_attribute("aria-expanded", "false")
    expect(move).to_be_focused()


def test_forget_asks_first_removes_the_row_and_moves_the_focus(page, goto, browser_log, dialogs, changes):
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    device(page).locator('[data-act="forget"]').click()
    until(page, lambda: sent(browser_log, "POST", rf"^/ui/api/satellites/{KITCHEN}/forget$"), "Forget sent")
    assert dialogs.seen == [("confirm", "Forget Kitchen? It stops streaming and waits to be adopted again.")]
    # The row that took its place has the focus, not a button that is gone.
    until(page, lambda: focused(page, "e => e.closest && e.closest('li.sat') && e.closest('li.sat').dataset.id")
          == LOUNGE, "the focus on the next row")
    expect(page).to_have_url(re.compile(r"/ui/satellites$"))
    # It reconnects and waits to be adopted again.
    expect(chip(page, KITCHEN)).to_have_text("New", timeout=15_000)


def test_dismissing_a_destructive_question_changes_nothing(page, goto, stack, browser_log, dialogs, changes):
    dialogs(answer=False)
    upload_firmware(stack, "v0.3.0")
    at(page, goto, "/ui/satellites/kitchen/device")
    box = device(page)
    update = box.locator('.sat-update [data-act="update"]')
    expect(update).to_have_text("Update to v0.3.0")
    for press in ('[data-act="reboot"]', '[data-act="forget"]', '.sat-update [data-act="update"]'):
        box.locator(press).click()
    box.locator('[data-act="move"]').click()
    move_box(page).fill("wss://hub.example.com:8443")
    move_box(page).press("Enter")
    until(page, lambda: len(dialogs.seen) == 4, "four questions")
    assert [m.split("?")[0] for _, m in dialogs.seen] == [
        "Reboot Kitchen", "Forget Kitchen", "Update Kitchen to v0.3.0", "Move Kitchen to wss://hub.example.com:8443"]
    page.wait_for_timeout(300)
    assert not [r for r in browser_log.sent("POST") if re.search(r"/(reboot|forget|set-hub|ota)$", r["path"])]
    expect(chip(page, KITCHEN)).to_have_text("Online")
    with hub(stack) as h:
        assert h.get(f"/satellites/{KITCHEN}").json()["adopted"] is True


# ---- Wake words ------------------------------------------------------------------------------


def test_the_wake_words_editor_lists_the_hubs_words_with_their_state(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words")
    rows = page.locator("#wwlist > li.ww")
    assert rows.evaluate_all("els => els.map(e => e.dataset.name)") == ["hey_jarvis", "alexa"]
    for name, label in (("hey_jarvis", "hey jarvis"), ("alexa", "alexa")):
        expect(ww(page, name).locator(".ww-label")).to_have_text(label)
        expect(ww(page, name).locator(".sat-state")).to_have_text("Ready", timeout=15_000)
        expect(ww(page, name).locator(".sat-line")).to_have_text("Command · every satellite · echo what it heard")
    expect(ww(page, "ptt").locator(".ww-label")).to_have_text("Push-to-talk")
    expect(ww(page, "ptt").locator(".sat-state")).to_have_text("Talk buttons")
    expect(page.locator("#ww-sum")).to_have_text("hey jarvis, alexa")
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text(WW_CLEAN)
    assert page.locator("#wwadd option").evaluate_all("os => os.map(o => o.value)") == [
        "hey_mycroft", "hey_rhasspy", "weather"]


def test_adding_a_word_stages_it_opens_it_and_save_downloads_and_readies_it(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words")
    page.locator("#wwadd").select_option("hey_mycroft")
    page.locator("#wwaddgo").click()
    row = ww(page, "hey_mycroft")
    expect(row.locator(":scope > details.sat-row")).to_have_attribute("open", "")
    expect(row.locator('[data-mode="command"]')).to_be_focused()
    expect(row.locator(".sat-state")).to_have_text("Not saved yet")
    expect(page).to_have_url(re.compile(r"/ui/satellites/wake-words/hey_mycroft$"))
    expect(page.locator("#wwdirty")).to_have_text(WW_DIRTY)
    expect(page.locator("#wwsave")).to_be_enabled()
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    added = saved_words(browser_log)["words"]["hey_mycroft"]
    assert added["threshold"] == 0.5 and added["satellites"] == ["*"] and added["mode"] == "command"
    assert added["action"]["destination"] == {"type": "echo"}
    expect(row.locator(".sat-state")).to_have_text("Ready", timeout=20_000)
    expect(line(page, KITCHEN)).to_have_text("Listens for hey jarvis, alexa, hey mycroft")
    expect(page.locator("#wwsave")).to_be_disabled()


def test_a_word_whose_model_cannot_download_reads_failed_and_offers_try_again(page, goto, browser_log, changes):
    """hey_rhasspy's model is not on the hub, and fetching it is refused by
    the stack's network guard, as a hub with no internet would find."""
    at(page, goto, "/ui/satellites/wake-words")
    page.locator("#wwadd").select_option("hey_rhasspy")
    page.locator("#wwaddgo").click()
    page.locator("#wwsave").click()
    row = ww(page, "hey_rhasspy")
    expect(row.locator(".sat-state")).to_have_text("Failed", timeout=20_000)
    expect(row.locator(".ww-error")).to_have_text(re.compile(r"^It did not download: ."))
    expect(page.locator("#sathealth")).to_have_text("hey rhasspy failed to download")
    expect(page.locator("#ww-sum")).to_have_text("hey rhasspy failed")
    retry = row.locator('[data-ww="retry"]')
    expect(retry).to_be_visible()
    puts = len(sent(browser_log, "PUT", r"^/ui/api/satellites/wake-words$"))
    retry.click()
    until(page, lambda: len(sent(browser_log, "PUT", r"^/ui/api/satellites/wake-words$")) == puts + 1, "Try again")
    assert set(saved_words(browser_log)["words"]) == {"hey_jarvis", "alexa", "hey_rhasspy"}
    expect(retry).to_have_text("Try again")


def test_save_is_off_until_something_changes_and_names_the_changed_word(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    threshold = ww(page, "hey_jarvis").locator("input[type=range]")
    expect(page.locator("#wwsave")).to_be_disabled()
    threshold.fill("0.65")
    expect(ww(page, "hey_jarvis").locator(".sat-state")).to_have_text("Changed")
    expect(ww(page, "alexa").locator(".sat-state")).to_have_text("Ready")
    expect(page.locator("#wwsave")).to_be_enabled()
    expect(page.locator("#wwdirty")).to_have_text(WW_DIRTY)
    expect(page.locator("#ww-sum")).to_have_text("unsaved changes")
    threshold.fill("0.5")
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text(WW_CLEAN)
    expect(ww(page, "hey_jarvis").locator(".sat-state")).to_have_text("Ready")


def test_removing_a_saved_word_is_staged_and_keep_takes_it_back(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/alexa")
    alexa = ww(page, "alexa")
    remove = alexa.locator('[data-ww="remove"]')
    remove.click()
    expect(alexa.locator(".sat-state")).to_have_text("Removed on save")
    expect(alexa).to_have_attribute("data-removed", "")
    expect(remove).to_have_text("Keep")
    expect(remove).to_be_focused()
    expect(alexa.locator('[data-f="dest"]')).to_be_disabled()
    expect(page.locator("#wwsave")).to_be_enabled()
    remove.click()
    expect(alexa.locator(".sat-state")).to_have_text("Ready")
    expect(remove).to_have_text("Remove")
    expect(page.locator("#wwsave")).to_be_disabled()
    remove.click()
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert set(saved_words(browser_log)["words"]) == {"hey_jarvis"}
    expect(alexa).to_have_count(0)
    expect(line(page, KITCHEN)).to_have_text("Listens for hey jarvis")


def test_removing_a_word_never_saved_just_drops_it(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/wake-words")
    page.locator("#wwadd").select_option("hey_mycroft")
    page.locator("#wwaddgo").click()
    ww(page, "hey_mycroft").locator('[data-ww="remove"]').click()
    expect(ww(page, "hey_mycroft")).to_have_count(0)
    expect(page.locator("#wwadd")).to_be_focused()
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text(WW_CLEAN)
    assert not sent(browser_log, "PUT", r"wake-words$")


def test_switching_to_trigger_raises_the_threshold_and_restores_the_action_on_the_way_back(page, goto, fake, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator('[data-f="dest"]').select_option("webhook")
    row.locator('[data-f="d.url"]').fill(fake.hook_url("jarvis"))
    row.locator('[data-mode="trigger"]').click()
    expect(row.locator('[data-mode="trigger"]')).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".slider output")).to_have_text("0.70")
    expect(row.locator(".sat-line")).to_have_text("Trigger · every satellite · Home Assistant decides")
    expect(row.locator('[data-f="dest"]')).to_be_hidden()
    expect(row.locator('[data-f="t.feedback"]')).to_be_visible()
    expect(row.locator(".ww-modehint")).to_have_text(
        "The word is the command: the hub reports it and Home Assistant decides what it does.")
    row.locator('[data-mode="command"]').click()
    expect(row.locator(".slider output")).to_have_text("0.50")
    expect(row.locator('[data-f="dest"]')).to_have_value("webhook")
    expect(row.locator('[data-f="d.url"]')).to_have_value(fake.hook_url("jarvis"))
    expect(row.locator(".sat-line")).to_have_text("Command · every satellite · webhook")


def test_choosing_satellites_starts_from_every_adopted_one_ticked(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    expect(row.locator(".ww-scopehint")).to_have_text("Includes satellites adopted later.")
    row.locator('[data-scope="chosen"]').click()
    ticks = row.locator(".ww-pick input[data-sat]")
    assert ticks.evaluate_all("ts => ts.map(t => [t.dataset.sat, t.checked, t.parentElement.textContent.trim()])") == [
        [KITCHEN, True, "Kitchen"], [LOUNGE, True, "Lounge"]]
    row.locator(f'.ww-pick input[data-sat="{LOUNGE}"]').uncheck()
    expect(row.locator(".sat-line")).to_have_text("Command · Kitchen · echo what it heard")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert saved_words(browser_log)["words"]["hey_jarvis"]["satellites"] == [KITCHEN]
    expect(line(page, LOUNGE)).to_have_text("Listens for alexa")
    expect(line(page, KITCHEN)).to_have_text("Listens for hey jarvis, alexa")


def test_a_word_left_with_no_satellite_keeps_save_off_and_says_which(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator('[data-scope="chosen"]').click()
    for nid in (KITCHEN, LOUNGE):
        row.locator(f'.ww-pick input[data-sat="{nid}"]').uncheck()
    expect(row.locator(".sat-line")).to_have_text("Command · no satellite · echo what it heard")
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text(
        "Pick at least one satellite for hey jarvis, or choose Every satellite.")


# Each destination, and the fields that are its own; More is opened first so
# the rare ones count too.
OWN_FIELDS = {
    "ha_assist": {"d.url", "d.env", "d.pipeline", "a.reply_to"},
    "ha_conversation": {"lang", "d.url", "d.env", "a.reply_to", "a.voice", "d.agent_id"},
    "webhook": {"lang", "d.url", "d.env", "a.reply_to", "a.voice"},
    "llm": {"lang", "preset", "d.base_url", "d.model", "d.env", "a.reply_to", "a.voice", "d.max_tokens",
            "d.system"},
    "echo": {"lang", "a.reply_to", "a.voice"},
}
ACTION_FIELDS = sorted(set().union(*OWN_FIELDS.values()))


def test_each_action_shows_only_its_own_fields(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator(".ww-more > summary").click()
    expect(row.locator(".ww-more")).to_have_attribute("open", "")
    for dest, own in OWN_FIELDS.items():
        row.locator('[data-f="dest"]').select_option(dest)
        shown = set(row.locator("[data-f]").evaluate_all(
            "fs => fs.filter(f => f.checkVisibility()).map(f => f.dataset.f)")) & set(ACTION_FIELDS)
        assert shown == own, f"{dest}: shows {sorted(shown)}, owns {sorted(own)}"
        # The key box, the tools and Test are a language model's alone.
        assert row.locator(".ww-llm").is_visible() == (dest == "llm"), dest
    row.locator('[data-f="dest"]').select_option("webhook")
    expect(row.locator(".ww-envlabel")).to_have_text("Token variable (optional)")
    row.locator('[data-f="dest"]').select_option("llm")
    expect(row.locator(".ww-envlabel")).to_have_text("Key name")
    row.locator('[data-f="dest"]').select_option("ha_conversation")
    expect(row.locator(".ww-envlabel")).to_have_text("Token variable")
    # Nothing was asked of anybody: no address was given.
    assert not sent(browser_log, "POST", r"/(ha/pipelines|llm/models)$")


def test_other_language_asks_for_a_tag_and_checks_it(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator('[data-f="lang"]').select_option("other")
    tag = row.locator('[data-f="tag"]')
    expect(tag).to_be_visible()
    expect(tag).to_be_focused()
    tag.fill("Deutsch")
    expect(row.locator(".sat-state")).to_have_text("Incomplete")
    tag.press("Tab")
    expect(row.locator(".sat-state")).to_have_text("Needs a fix")
    expect(row.locator(".ww-fix")).to_have_text("Write the language as a tag, for example de or nl-BE.")
    expect(tag).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text("hey jarvis needs a fix before the wake words can be saved.")
    tag.fill("de")
    tag.press("Tab")
    expect(row.locator(".sat-state")).to_have_text("Changed")
    expect(row.locator(".sat-line")).to_have_text("Command · every satellite · echo what it heard · de")
    expect(page.locator("#wwsave")).to_be_enabled()


# What the hub would refuse, each named on the word before Save. A step is
# ("mode", m), ("dest", type), ("more",), or (data-f, value) to type and leave.
FIXES = {
    "url": ([("dest", "webhook"), ("d.url", "ftp://hooks.example.com/x")],
            "Write the address in full, starting with https or http.", False),
    "userinfo": ([("dest", "webhook"), ("d.url", "https://user:secret@hooks.example.com/x")],
                 "Leave the user and password out of the address; name a token variable instead.", False),
    "userinfo-key": ([("dest", "llm"), ("d.base_url", "https://user:secret@llm.example.com/v1")],
                     "Leave the user and password out of the address; name a key under Key name instead.", False),
    "model": ([("dest", "llm"), ("d.base_url", "LLM"), ("d.model", "")],
              "Name the model to ask, as the provider lists it.", True),
    "max-tokens": ([("dest", "llm"), ("d.base_url", "LLM"), ("d.model", "fake-small"), ("more",),
                    ("d.max_tokens", "9000")], "Make the reply limit a whole number from 1 to 8192.", False),
    "env-needed": ([("dest", "ha_conversation"), ("d.url", "HA"), ("d.env", "")],
                   "Name the variable that holds Home Assistant's token.", True),
    "env": ([("dest", "ha_conversation"), ("d.url", "HA"), ("d.env", "my token")],
            "Write the name of the variable that holds the token, never the token itself.", False),
    "env-key": ([("dest", "llm"), ("d.base_url", "LLM"), ("d.model", "fake-small"), ("d.env", "my_key")],
                "Write the key's name in capitals, digits and underscores, never the key itself.", False),
    "env-hub": ([("dest", "ha_conversation"), ("d.url", "HA"), ("d.env", "SATELLITES_DATA_DIR")],
                "That name is one of the hub's own settings: name a variable with TOKEN or KEY in it.", False),
    "follow": ([("mode", "conversation"), ("c.follow_up_s", "90")], "Keep listening for 1 to 60 seconds.", False),
    "pause": ([("silence_ms", "5")], "Pause for 0.2 to 3 seconds before the command ends.", False),
    "pause-follow": ([("mode", "conversation"), ("c.silence_ms", "9")],
                     "Pause for 0.2 to 3 seconds before a follow-up ends.", False),
    "phrases": ([("mode", "conversation"), ("more",), ("c.end_phrases", ", ".join(f"bye {n}" for n in range(65)))],
                "Keep to 64 end phrases, each of 64 characters at most.", False),
    "spellings": ([("v.spellings", ", ".join(f"jarvis {n}" for n in range(13)))],
                  "Keep to 12 other spellings, each of 40 characters at most.", False),
    "cooldown": ([("mode", "trigger"), ("t.cooldown_s", "700")], "Make the cooldown 0 to 600 seconds.", False),
}


@pytest.mark.parametrize("case", list(FIXES))
def test_each_value_the_hub_would_refuse_is_named_before_save(page, goto, fake, browser_log, reads, case):
    steps, said, unfinished = FIXES[case]
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    for step in steps:
        if step[0] == "mode":
            row.locator(f'[data-mode="{step[1]}"]').click()
        elif step[0] == "dest":
            row.locator('[data-f="dest"]').select_option(step[1])
        elif step[0] == "more":
            row.locator(".ww-more > summary").click()
        else:
            value = {"LLM": fake.llm_url, "HA": fake.ha_url}.get(step[1], step[1])
            row.locator(f'[data-f="{step[0]}"]').fill(value)
            row.locator(f'[data-f="{step[0]}"]').press("Tab")
    expect(row.locator(".ww-fix")).to_have_text(said)
    expect(row.locator(".sat-state")).to_have_text("Incomplete" if unfinished else "Needs a fix")
    expect(page.locator("#wwsave")).to_be_disabled()
    expect(page.locator("#wwdirty")).to_have_text(
        "Finish setting up hey jarvis to save the wake words." if unfinished
        else "hey jarvis needs a fix before the wake words can be saved.")
    page.locator("#wwsave").click(force=True)
    assert not sent(browser_log, "PUT", r"wake-words$")


def test_a_word_whose_model_has_gone_from_the_hub_says_to_upload_it_again(page, goto, stack, changes):
    """A custom model deleted from the hub's volume behind its back: the hub
    would refuse every Save while a word names it, so the word says so."""
    with hub(stack) as h:
        h.post("/satellites/wake-words/models", params={"name": "lumos"}, content=custom_model(),
               headers={"Content-Type": "application/octet-stream"}).raise_for_status()
    put_words(stack, lambda words: words.append(
        dict(word(words, "alexa"), name="lumos", threshold=0.5, satellites=["*"])))
    (stack.run / "hub-data" / "models" / "lumos.onnx").unlink()
    at(page, goto, "/ui/satellites/wake-words/lumos")
    row = ww(page, "lumos")
    expect(row.locator(".ww-fix")).to_have_text(
        "Its model is no longer on the hub: upload it again under Custom models, or remove this word.")
    expect(row.locator('[data-ww="retry"]')).to_be_hidden()


def test_a_422_from_the_hub_keeps_the_draft_and_shows_the_reason(page, goto, browser_log, reads):
    """The page checks everything it can before a Save; what the hub refuses
    anyway (a rule changed on the hub before the page was reloaded) comes
    back as 422 with the hub's own sentence, answered here as the hub would."""
    reason = "'hey_jarvis' is not a wake word this hub can load; it can load alexa"
    page.route(re.compile(r"/ui/api/satellites/wake-words$"), lambda route: route.fulfill(
        status=422, json={"error": {"message": reason, "type": "invalid_request_error", "param": None,
                                    "code": "invalid_wake_words"}})
        if route.request.method == "PUT" else route.continue_())
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator("input[type=range]").fill("0.8")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text(reason)
    expect(page.locator("#wwnote .note")).to_have_class(re.compile(r"\bbad\b"))
    expect(row.locator(".slider output")).to_have_text("0.80")
    expect(row.locator(".sat-state")).to_have_text("Changed")
    expect(page.locator("#wwsave")).to_be_enabled()
    expect(page.locator("#wwdirty")).to_have_text(WW_DIRTY)


def test_the_double_check_defaults_to_record_only_and_is_sent_as_set(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    check = field(page, "hey_jarvis", "v.mode")
    expect(check).to_have_value("log")
    expect(check.locator("option:checked")).to_have_text("Record only")
    check.select_option("on")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert saved_words(browser_log)["words"]["hey_jarvis"]["verify"] == {"mode": "on", "spellings": []}
    assert saved_words(browser_log)["words"]["alexa"]["verify"] == {"mode": "log", "spellings": []}


def test_also_accept_spellings_are_sent_as_a_list_and_limited_to_twelve_of_forty_characters(page, goto, browser_log,
                                                                                         changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    spell = field(page, "hey_jarvis", "v.spellings")
    fix = ww(page, "hey_jarvis").locator(".ww-fix")
    too_many = "Keep to 12 other spellings, each of 40 characters at most."
    spell.fill(", ".join(f"jarvis {n}" for n in range(13)))
    spell.press("Tab")
    expect(fix).to_have_text(too_many)
    spell.fill("hey jarvice, " + "j" * 41)
    spell.press("Tab")
    expect(fix).to_have_text(too_many)
    spell.fill("hey jarvice,  hay jarvis , ")
    spell.press("Tab")
    expect(fix).to_have_text("")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert saved_words(browser_log)["words"]["hey_jarvis"]["verify"] == {
        "mode": "log", "spellings": ["hey jarvice", "hay jarvis"]}


def test_push_to_talk_offers_no_double_check(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words/ptt")
    row = ww(page, "ptt")
    expect(row.locator(":scope > details.sat-row")).to_have_attribute("open", "")
    expect(row.locator(".ww-verify")).to_be_hidden()
    expect(row.locator('[data-mode="trigger"]')).to_be_hidden()
    expect(row.locator(".ww-listen")).to_be_hidden()
    expect(row.locator('[data-ww="remove"]')).to_be_hidden()
    expect(row.locator(".ww-rowhint")).to_have_text("It runs on any button set to Talk in a satellite's Buttons.")


def test_ring_colour_is_sent_and_use_the_default_clears_it(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    colour = field(page, "hey_jarvis", "colour")
    default = row.locator('[data-ww="colourdefault"]')
    expect(colour).to_have_value("#286eff")
    expect(default).to_be_hidden()
    colour.fill("#ff0000")
    expect(default).to_be_visible()
    expect(row.locator(".ww-dot")).to_have_css("background-color", "rgb(255, 0, 0)")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert saved_words(browser_log)["words"]["hey_jarvis"]["colour"] == "#ff0000"
    default.click()
    expect(default).to_be_hidden()
    expect(colour).to_be_focused()
    expect(colour).to_have_value("#286eff")
    page.locator("#wwsave").click()
    until(page, lambda: len(sent(browser_log, "PUT", r"wake-words$")) == 2, "the second save")
    assert saved_words(browser_log)["words"]["hey_jarvis"]["colour"] is None


def test_trigger_feedback_cooldown_and_ends_a_conversation_are_sent(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator('[data-mode="trigger"]').click()
    field(page, "hey_jarvis", "t.feedback").select_option("none")
    field(page, "hey_jarvis", "t.cooldown_s").fill("10")
    row.locator('[data-t="ends_conversation"]').check()
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    sent_word = saved_words(browser_log)["words"]["hey_jarvis"]
    assert sent_word["mode"] == "trigger" and sent_word["threshold"] == 0.7
    assert sent_word["trigger"] == {"feedback": "none", "cooldown_s": 10, "ends_conversation": True}
    assert "action" not in sent_word, "a trigger was sent an action"


def test_more_fields_reply_on_voice_agent_end_phrases_limit_and_system_prompt_are_sent(page, goto, fake, browser_log,
                                                                                     changes):
    """Two words in one save: alexa a Home Assistant conversation that replies
    on the Lounge in a voice of its own, hey jarvis a language model with a
    reply limit and a system prompt."""
    at(page, goto, "/ui/satellites/wake-words/alexa")
    alexa = ww(page, "alexa")
    alexa.locator('[data-mode="conversation"]').click()
    field(page, "alexa", "dest").select_option("ha_conversation")
    field(page, "alexa", "d.url").fill(fake.ha_url)
    field(page, "alexa", "d.url").press("Tab")
    alexa.locator(".ww-more > summary").click()
    field(page, "alexa", "a.reply_to").select_option(LOUNGE)
    voice = field(page, "alexa", "a.voice").locator("option").nth(1).get_attribute("value")
    field(page, "alexa", "a.voice").select_option(voice)
    field(page, "alexa", "d.agent_id").fill("conversation.openai")
    field(page, "alexa", "c.end_phrases").fill("thank you, goodbye")
    # Closed, More still says what in it is not the default.
    expect(alexa.locator(".ww-more .sum-note")).to_have_text(re.compile(r"^replies on Lounge, voice ."))
    jarvis = ww(page, "hey_jarvis")
    jarvis.locator(":scope > details.sat-row > summary").click()
    field(page, "hey_jarvis", "dest").select_option("llm")
    field(page, "hey_jarvis", "d.base_url").fill(fake.llm_url)
    field(page, "hey_jarvis", "d.model").fill("fake-small")
    jarvis.locator(".ww-more > summary").click()
    field(page, "hey_jarvis", "d.max_tokens").fill("300")
    field(page, "hey_jarvis", "d.system").fill("Answer in one sentence.")
    field(page, "hey_jarvis", "d.system").press("Tab")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    words = saved_words(browser_log)["words"]
    a = words["alexa"]
    assert a["mode"] == "conversation"
    assert a["action"]["reply_to"] == LOUNGE and a["action"]["voice"] == voice
    assert a["action"]["destination"] == {"type": "ha_conversation", "url": fake.ha_url,
                                          "token_env": "SATELLITES_HA_TOKEN", "agent_id": "conversation.openai"}
    assert a["conversation"]["end_phrases"] == ["thank you", "goodbye"]
    d = words["hey_jarvis"]["action"]["destination"]
    assert (d["type"], d["base_url"], d["model"], d["max_tokens"], d["system"]) == (
        "llm", fake.llm_url, "fake-small", 300, "Answer in one sentence.")


def test_a_command_can_fall_back_only_to_a_conversation_word(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    fallback = field(page, "hey_jarvis", "a.fallback")
    hint = ww(page, "hey_jarvis").locator(".ww-fbhint")
    expect(fallback).to_be_disabled()
    expect(hint).to_have_text("Only a conversation word can take over, and there is none yet.")
    ww(page, "alexa").locator(":scope > details.sat-row > summary").click()
    ww(page, "alexa").locator('[data-mode="conversation"]').click()
    expect(fallback).to_be_enabled()
    expect(hint).to_have_text("")
    assert fallback.locator("option").evaluate_all("os => os.map(o => o.value)") == ["", "alexa"]
    fallback.select_option("alexa")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    words = saved_words(browser_log)["words"]
    assert words["hey_jarvis"]["action"]["fallback"] == "alexa" and words["alexa"]["mode"] == "conversation"
    # alexa made a command again: hey jarvis would hand over to nobody.
    ww(page, "alexa").locator('[data-mode="command"]').click()
    expect(ww(page, "hey_jarvis").locator(".ww-fix")).to_have_text(
        "alexa is not a conversation word, so it cannot take over.")
    expect(page.locator("#wwsave")).to_be_disabled()


def pipelines(page) -> list[list[str]]:
    return field(page, "hey_jarvis", "d.pipeline").locator("option").evaluate_all(
        "os => os.map(o => [o.value, o.textContent])")


def test_home_assistant_pipelines_are_listed_once_the_address_and_token_are_given(page, goto, stack, fake, browser_log,
                                                                                changes):
    store_secret(stack, "SATELLITES_HA_TOKEN", HA_TOKEN)
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    field(page, "hey_jarvis", "dest").select_option("ha_assist")
    hint = row.locator(".ww-pipehint")
    expect(hint).to_have_text("Fill in the address and token variable to list the pipelines set up in Home Assistant.")
    address = field(page, "hey_jarvis", "d.url")
    address.fill(fake.ha_url)
    page.wait_for_timeout(300)
    assert not sent(browser_log, "POST", r"/ha/pipelines$"), "the pipelines were asked for a half-typed address"
    address.blur()
    until(page, lambda: sent(browser_log, "POST", r"^/ui/api/satellites/ha/pipelines$"), "the pipelines asked for")
    assert bodies(browser_log, "POST", r"/ha/pipelines$") == [{"url": fake.ha_url, "token_env": "SATELLITES_HA_TOKEN"}]
    expect(hint).to_have_text("Hears with faster_whisper (en), speaks with piper as en_GB-alba-medium.")
    assert pipelines(page) == [["", "Home Assistant's preferred (Home)"], ["01home", "Home"],
                               ["02kitchen", "Kitchen pipeline"]]
    field(page, "hey_jarvis", "d.pipeline").select_option("02kitchen")
    expect(hint).to_have_text("Hears with Calliope's own, speaks with google_translate.")
    ws = [r["json"] for r in fake.requests(backend="ha", method="WS")]
    assert ws[0] == {"type": "auth", "token": True} and ws[1]["type"] == "assist_pipeline/pipeline/list"


def test_a_pipeline_list_that_failed_can_be_asked_again(page, goto, stack, fake, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    field(page, "hey_jarvis", "dest").select_option("ha_assist")
    field(page, "hey_jarvis", "d.url").fill(fake.ha_url)
    field(page, "hey_jarvis", "d.url").blur()
    hint = row.locator(".ww-pipehint")
    expect(hint).to_have_text("Could not list the pipelines: SATELLITES_HA_TOKEN is not set on the hub, "
                              "so Home Assistant cannot be asked")
    again = row.locator('[data-ww="pipes"]')
    expect(again).to_have_text("Ask again")
    store_secret(stack, "SATELLITES_HA_TOKEN", HA_TOKEN)
    again.click()
    expect(hint).to_have_text("Hears with faster_whisper (en), speaks with piper as en_GB-alba-medium.")
    expect(again).to_be_hidden()
    expect(field(page, "hey_jarvis", "d.pipeline")).to_be_focused()
    assert len(sent(browser_log, "POST", r"/ha/pipelines$")) == 2


def test_a_language_model_provider_fills_the_base_url_and_lists_its_models(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    field(page, "hey_jarvis", "dest").select_option("llm")
    provider = field(page, "hey_jarvis", "preset")
    assert provider.locator("option").evaluate_all("os => os.map(o => o.textContent)") == [
        "OpenAI", "Anthropic", "OpenRouter", "Groq", "Mistral", "DeepSeek", "Other or self-hosted"]
    provider.select_option(label="OpenAI")
    base = field(page, "hey_jarvis", "d.base_url")
    expect(base).to_have_value("https://api.openai.com/v1")
    hint = row.locator(".ww-modelhint")
    # A key is never sent to a provider just because it was picked.
    expect(hint).to_have_text(f"List models sends the key in {LLM_KEY} to api.openai.com; check the key is for it.")
    expect(row.locator('[data-ww="models"]')).to_have_text("List models")
    provider.select_option(label="Other or self-hosted")
    expect(base).to_have_value("")
    expect(base).to_be_focused()
    base.fill(fake.llm_url)
    base.press("Tab")
    host = fake.llm_url.split("://")[1].split("/")[0]
    expect(hint).to_have_text(f"List models sends the key in {LLM_KEY} to {host}; check the key is for it.")
    assert not sent(browser_log, "POST", r"/llm/models$")
    seq = fake.last_seq()
    row.locator('[data-ww="models"]').click()
    expect(hint).to_have_text("3 models to pick from; type to narrow the list.")
    expect(field(page, "hey_jarvis", "d.model")).to_be_focused()
    assert bodies(browser_log, "POST", r"^/ui/api/satellites/llm/models$") == [
        {"base_url": fake.llm_url, "api_key_env": LLM_KEY}]
    assert row.locator("datalist option").evaluate_all("os => os.map(o => o.value)") == [
        "fake-large", "fake-small", "fake-tiny"]
    listed = fake.requests(backend="llm", method="GET", since=seq)
    assert [r["query"] for r in listed] == ["", "limit=1000&after_id=fake-small"]


def key_box(page):
    return ww(page, "hey_jarvis").locator('input[type="password"]')


def llm_word(page, goto, fake) -> None:
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    field(page, "hey_jarvis", "dest").select_option("llm")
    field(page, "hey_jarvis", "d.base_url").fill(fake.llm_url)
    field(page, "hey_jarvis", "d.model").fill("fake-small")
    field(page, "hey_jarvis", "d.model").press("Tab")


def test_storing_a_key_sends_it_once_empties_the_box_and_never_shows_it_again(page, goto, stack, fake, browser_log,
                                                                            changes):
    secret = "sk-e2e-0123456789abcdef"
    llm_word(page, goto, fake)
    row = ww(page, "hey_jarvis")
    hint = row.locator(".ww-keyhint")
    expect(hint).to_have_text(f"Store a key as {LLM_KEY}, or save to learn whether the environment sets it.")
    key_box(page).fill(secret)
    row.locator('[data-ww="key"]').click()
    expect(row.locator(".ww-keynote")).to_have_text("Stored. It is not shown again.")
    expect(key_box(page)).to_have_value("")
    expect(hint).to_have_text(f"A key is stored on the hub as {LLM_KEY}.")
    expect(row.locator('[data-ww="key"]')).to_have_text("Replace key")
    expect(row.locator('[data-ww="keyclear"]')).to_be_visible()
    assert bodies(browser_log, "PUT", r"^/ui/api/satellites/secrets$") == [{"name": LLM_KEY, "value": secret}]
    assert secret not in page.content()
    with hub(stack) as h:
        assert secret not in h.get("/satellites/wake-words").text
        assert h.get("/satellites/wake-words").json()["secrets"] == {LLM_KEY: "hub"}


def test_replacing_a_key_shared_by_other_words_asks_first(page, goto, stack, fake, browser_log, dialogs, changes):
    def both(words):
        for name in ("hey_jarvis", "alexa"):
            word(words, name)["action"] = llm_action(fake.llm_url)
    put_words(stack, both)
    store_secret(stack, LLM_KEY, "sk-old")
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    expect(row.locator(".ww-keyhint")).to_have_text(f"A key is stored on the hub as {LLM_KEY}. Also used by alexa.")
    key_box(page).fill("sk-new")
    row.locator('[data-ww="key"]').click()
    until(page, lambda: len(dialogs.seen) == 1, "the question")
    assert dialogs.seen[0] == ("confirm", f"Replace the key stored as {LLM_KEY}? alexa will send the new one too.")
    expect(key_box(page)).to_have_value("sk-new")
    assert not sent(browser_log, "PUT", r"/secrets$")
    row.locator('[data-ww="key"]').click()
    expect(row.locator(".ww-keynote")).to_have_text("Stored. It is not shown again.")
    assert bodies(browser_log, "PUT", r"/secrets$") == [{"name": LLM_KEY, "value": "sk-new"}]


def test_clearing_a_key_asks_first(page, goto, stack, fake, browser_log, dialogs, changes):
    put_words(stack, lambda words: word(words, "hey_jarvis").update(action=llm_action(fake.llm_url)))
    store_secret(stack, LLM_KEY, "sk-old")
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    clear = row.locator('[data-ww="keyclear"]')
    clear.click()
    until(page, lambda: len(dialogs.seen) == 1, "the question")
    assert dialogs.seen[0] == ("confirm", f"Clear the key stored as {LLM_KEY}? Every action that names it stops sending it.")
    assert not sent(browser_log, "PUT", r"/secrets$")
    clear.click()
    expect(row.locator(".ww-keynote")).to_have_text(f"The key stored as {LLM_KEY} is cleared.")
    assert bodies(browser_log, "PUT", r"/secrets$") == [{"name": LLM_KEY, "value": None}]
    expect(clear).to_be_hidden()
    expect(key_box(page)).to_be_focused()


def test_enter_in_the_key_box_stores_the_key(page, goto, fake, browser_log, changes):
    llm_word(page, goto, fake)
    key_box(page).fill("sk-enter")
    key_box(page).press("Enter")
    expect(ww(page, "hey_jarvis").locator(".ww-keynote")).to_have_text("Stored. It is not shown again.")
    assert bodies(browser_log, "PUT", r"/secrets$") == [{"name": LLM_KEY, "value": "sk-enter"}]


def test_web_search_is_greyed_without_a_search_server_and_weather_is_not(page, goto, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    field(page, "hey_jarvis", "dest").select_option("llm")
    expect(row.locator(".ww-toolhint")).to_have_text(
        "Web search needs SATELLITES_SEARXNG_URL set on the hub; the weather needs nothing.")
    expect(row.locator('[data-tool="weather"]')).to_be_enabled()
    expect(row.locator('[data-tool="web_search"]')).to_be_disabled()


def test_test_asks_the_model_one_question_and_reports_the_times(page, goto, fake, browser_log, reads):
    llm_word(page, goto, fake)
    row = ww(page, "hey_jarvis")
    seq = fake.last_seq()
    row.locator('[data-ww="llmtest"]').click()
    expect(row.locator(".ww-llmresult")).to_have_text(
        re.compile(r"^Answered in \d+\.\d s, the first words in \d+\.\d s: Hello there, friend\.$"), timeout=15_000)
    tested = bodies(browser_log, "POST", r"^/ui/api/satellites/llm/test$")
    assert len(tested) == 1 and (tested[0]["type"], tested[0]["base_url"], tested[0]["model"]) == (
        "llm", fake.llm_url, "fake-small")
    asked = fake.requests(backend="llm", path=r"/chat/completions$", since=seq)
    assert len(asked) == 1 and asked[0]["json"]["stream"] is True
    assert asked[0]["json"]["messages"][-1] == {"role": "user", "content": "Say hello in five words or fewer."}
    # A result is about the form it tested: changing the model takes it away.
    field(page, "hey_jarvis", "d.model").fill("fake-tiny")
    expect(row.locator(".ww-llmresult")).to_have_text("")


def test_a_model_that_does_not_answer_is_reported_by_test(page, goto, fake, browser_log, reads):
    browser_log.allow(502, r"/llm/test$")
    fake.fail(r"^/__llm/", backend="llm", status=401, json_body={"error": {
        "message": "Incorrect API key provided.", "type": "invalid_request_error", "code": "invalid_api_key"}})
    llm_word(page, goto, fake)
    row = ww(page, "hey_jarvis")
    row.locator('[data-ww="llmtest"]').click()
    result = row.locator(".ww-llmresult")
    expect(result).to_have_text(re.compile(
        rf"^The model did not answer: the LLM wants a key \(401\) and {LLM_KEY} has none: "), timeout=15_000)
    expect(result.locator(".note")).to_have_class(re.compile(r"\bbad\b"))
    expect(row.locator('[data-ww="llmtest"]')).to_have_text("Test")


def test_push_to_talk_can_be_changed_and_is_sent_only_when_it_changed(page, goto, browser_log, changes):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    ww(page, "hey_jarvis").locator("input[type=range]").fill("0.6")
    page.locator("#wwsave").click()
    expect(page.locator("#wwnote")).to_have_text("Saved.")
    assert saved_words(browser_log)["ptt"] is None, "push-to-talk was sent with no change to it"
    ptt = ww(page, "ptt")
    ptt.locator(":scope > details.sat-row > summary").click()
    ptt.locator('[data-mode="conversation"]').click()
    expect(ptt.locator(".sat-state")).to_have_text("Changed")
    page.locator("#wwsave").click()
    until(page, lambda: len(sent(browser_log, "PUT", r"wake-words$")) == 2, "the second save")
    assert saved_words(browser_log)["ptt"]["mode"] == "conversation"


def test_a_poll_never_undoes_an_unsaved_edit(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/wake-words/hey_jarvis")
    row = ww(page, "hey_jarvis")
    row.locator("input[type=range]").fill("0.8")
    row.locator('[data-mode="conversation"]').click()
    asked = len(sent(browser_log, "GET", r"^/ui/api/satellites/wake-words$"))
    refreshed(page)
    until(page, lambda: len(sent(browser_log, "GET", r"^/ui/api/satellites/wake-words$")) > asked, "a poll")
    page.wait_for_function("() => !SATELLITES.polling")
    expect(row.locator(".slider output")).to_have_text("0.80")
    expect(row.locator('[data-mode="conversation"]')).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".sat-state")).to_have_text("Changed")
    expect(page.locator("#wwsave")).to_be_enabled()


# ---- Try a word ---------------------------------------------------------------------------


def try_word(page, name: str, text: str) -> None:
    page.locator("#routeword").select_option(name)
    page.locator("#routesay").fill(text)
    page.locator("#routetest").click()


def frames(fake) -> int:
    return sum(sum(s["audio_frames"].values()) for s in fake.satellites())


def test_try_a_word_runs_the_saved_echo_action_and_plays_nothing(page, goto, fake, browser_log, reads):
    at(page, goto, "/ui/satellites/try-a-word")
    expect(page.locator("#ww-try")).to_have_attribute("open", "")
    before, seq = frames(fake), fake.last_seq()
    try_word(page, "hey_jarvis", "what time is it")
    expect(page.locator("#routeresult")).to_have_text(re.compile(
        r'^hey jarvis: "what time is it" \(en\) answered "what time is it", first sound after \d+ ms$'),
        timeout=15_000)
    assert bodies(browser_log, "POST", r"^/ui/api/satellites/routing/test$") == [
        {"satellite": "any", "wake_word": "hey_jarvis", "text": "what time is it"}]
    assert fake.requests(backend="tts", method="POST", since=seq), "no reply was made"
    page.wait_for_timeout(500)
    assert frames(fake) == before, "Try a word played something on a satellite"


def test_try_a_word_with_nothing_typed_says_so(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/try-a-word")
    page.locator("#routetest").click()
    expect(page.locator("#routeresult")).to_have_text("Type something to send to the word's action.")
    assert not sent(browser_log, "POST", r"/routing/test$")


def test_try_a_word_lists_saved_words_only_and_never_triggers(page, goto, stack, changes):
    def trigger(words):
        alexa = word(words, "alexa")
        alexa.update(mode="trigger", threshold=0.7)
        alexa.pop("action")
    put_words(stack, trigger)
    at(page, goto, "/ui/satellites/try-a-word")
    options = page.locator("#routeword option")
    assert options.evaluate_all("os => os.map(o => [o.value, o.textContent])") == [
        ["hey_jarvis", "hey jarvis"], ["ptt", "Push-to-talk"]]
    # A word added and not saved is not one the hub can run.
    page.locator("#wwadd").select_option("hey_mycroft")
    page.locator("#wwaddgo").click()
    assert options.evaluate_all("os => os.map(o => o.value)") == ["hey_jarvis", "ptt"]


def test_try_a_word_through_a_language_model_reports_its_reply(page, goto, stack, fake, changes):
    put_words(stack, lambda words: word(words, "hey_jarvis").update(action=llm_action(fake.llm_url)))
    at(page, goto, "/ui/satellites/try-a-word")
    seq = fake.last_seq()
    try_word(page, "hey_jarvis", "tell me a joke")
    expect(page.locator("#routeresult")).to_have_text(
        re.compile(r'^hey jarvis: "tell me a joke"( \([\w-]+\))? answered "Hello there, friend\."'), timeout=15_000)
    asked = fake.requests(backend="llm", path=r"/chat/completions$", since=seq)
    assert asked and asked[-1]["json"]["messages"][-1]["content"] == "tell me a joke"


def test_try_a_word_through_home_assistant_conversation_reports_its_reply(page, goto, stack, fake, changes):
    store_secret(stack, "SATELLITES_HA_TOKEN", HA_TOKEN)
    put_words(stack, lambda words: word(words, "hey_jarvis").update(action={
        "destination": {"type": "ha_conversation", "url": fake.ha_url, "token_env": "SATELLITES_HA_TOKEN"},
        "reply_to": "same", "voice": None, "fallback": None}))
    at(page, goto, "/ui/satellites/try-a-word")
    seq = fake.last_seq()
    try_word(page, "hey_jarvis", "turn on the kitchen lights")
    expect(page.locator("#routeresult")).to_have_text(
        re.compile(r'^hey jarvis: "turn on the kitchen lights"( \([\w-]+\))? answered "Turned on the kitchen lights\."'),
        timeout=15_000)
    asked = fake.requests(backend="ha", method="POST", path=r"/api/conversation/process$", since=seq)
    assert asked and asked[-1]["json"]["text"] == "turn on the kitchen lights"
    assert asked[-1]["headers"]["authorization"] == f"Bearer {HA_TOKEN}"


# ---- Custom models ------------------------------------------------------------------------


def onnx(name: str = "lumos.onnx") -> dict:
    return {"name": name, "mimeType": "application/octet-stream", "buffer": custom_model()}


def upload_model(stack, name: str = "lumos") -> None:
    with hub(stack) as h:
        h.post("/satellites/wake-words/models", params={"name": name}, content=custom_model(),
               headers={"Content-Type": "application/octet-stream"}).raise_for_status()


def test_uploading_a_custom_model_adds_it_to_the_list_of_words_to_add(page, goto, stack, browser_log, changes):
    at(page, goto, "/ui/satellites/custom-models")
    expect(page.locator("#ww-models-sum")).to_have_text("none")
    page.locator("#wwfile").set_input_files(onnx("my-model.onnx"))
    page.locator("#wwmodelname").fill("lumos")
    page.locator("#wwupload").click()
    expect(page.locator("#wwmodelnote")).to_have_text("Uploaded lumos. Add it like any other wake word.")
    upload = sent(browser_log, "POST", r"^/ui/api/satellites/wake-words/models$")
    assert len(upload) == 1 and upload[0]["query"] == "name=lumos"
    with hub(stack) as h:
        assert h.get("/satellites/wake-words").json()["custom"] == ["lumos"]
    expect(page.locator("#wwcustom .sat-name")).to_have_text(["lumos"])
    expect(page.locator("#wwcustom button")).to_be_enabled()
    expect(page.locator("#ww-models-sum")).to_have_text("lumos")
    assert "lumos" in page.locator("#wwadd option").evaluate_all("os => os.map(o => o.value)")
    expect(page.locator("#wwmodelname")).to_have_value("")
    assert page.locator("#wwfile").evaluate("f => f.files.length") == 0


def test_upload_without_a_file_or_with_a_bad_name_is_refused_before_sending(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/custom-models")
    note = page.locator("#wwmodelnote")
    page.locator("#wwupload").click()
    expect(note).to_have_text("Choose an .onnx file first.")
    page.locator("#wwfile").set_input_files(onnx())
    for bad in ("lumos maxima", "ptt", "-lumos"):
        page.locator("#wwmodelname").fill(bad)
        page.locator("#wwupload").click()
        expect(note).to_have_text("Name it with letters, digits, _ and - only.")
    assert not sent(browser_log, "POST", r"/wake-words/models$")


def test_a_custom_model_cannot_take_a_built_in_words_name(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/custom-models")
    page.locator("#wwfile").set_input_files(onnx())
    page.locator("#wwmodelname").fill("weather")
    page.locator("#wwupload").click()
    expect(page.locator("#wwmodelnote")).to_have_text("That is a built-in wake word's name, so give yours another.")
    assert not sent(browser_log, "POST", r"/wake-words/models$")


def test_a_custom_model_in_use_cannot_be_deleted_and_says_why(page, goto, stack, changes):
    upload_model(stack)
    put_words(stack, lambda words: words.append(dict(word(words, "alexa"), name="lumos", threshold=0.5)))
    at(page, goto, "/ui/satellites/custom-models")
    row = page.locator("#wwcustom li")
    expect(row.locator(".sat-name")).to_have_text("lumos")
    expect(row.get_by_role("button", name="Delete the model lumos")).to_be_disabled()
    expect(row.locator(".hint")).to_have_text("It is a wake word, so remove it from the list first.")


def test_deleting_an_unused_custom_model_asks_first(page, goto, stack, browser_log, dialogs, changes):
    upload_model(stack)
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/custom-models")
    delete = page.get_by_role("button", name="Delete the model lumos")
    delete.click()
    until(page, lambda: len(dialogs.seen) == 1, "the question")
    assert dialogs.seen[0] == ("confirm", "Delete the model lumos? Upload it again to use it.")
    assert not sent(browser_log, "DELETE", r"/models/")
    delete.click()
    expect(page.locator("#wwmodelnote")).to_have_text("Deleted lumos.")
    assert len(sent(browser_log, "DELETE", r"^/ui/api/satellites/wake-words/models/lumos$")) == 1
    expect(page.locator("#wwcustom li")).to_have_count(0)
    expect(page.locator("#ww-models-sum")).to_have_text("none")
    expect(page.locator("#wwfile")).to_be_focused()


# ---- Activity ------------------------------------------------------------------------------


def test_a_button_press_arrives_in_activity_newest_first(page, goto, fake, reads):
    at(page, goto, "/ui/satellites/activity")
    expect(page.locator("#evnone")).to_have_text("Press a button on a satellite to see its event arrive.")
    fake.satellite_button("kitchen", "mode", "press")
    expect(activity(page).first).to_have_text("Kitchen Mode pressed")
    fake.satellite_button("kitchen", "mode", "release")
    expect(activity(page)).to_have_text(["Kitchen Mode released", "Kitchen Mode pressed"])
    expect(page.locator("#satelliteevents li time").first).to_have_text(re.compile(r"^\d\d:\d\d:\d\d$"))


def test_online_and_offline_are_logged_with_the_satellites_name(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/activity")
    fake.satellite_drop("kitchen")
    expect(activity(page).first).to_have_text("Kitchen went offline")
    fake.satellite_start("kitchen")
    expect(activity(page).first).to_have_text(re.compile(r"^Kitchen connected(, on v0\.2\.0)?$"), timeout=15_000)


def test_a_satellite_waiting_to_be_adopted_is_logged(page, goto, fake, changes):
    at(page, goto, "/ui/satellites/activity")
    fake.satellite_drop("hallway")
    fake.satellite_start("hallway")
    expect(activity(page).filter(has_text="waiting to be adopted")).to_have_text(
        [f"New satellite waiting to be adopted (ID {HALLWAY})"], timeout=15_000)


def test_activity_keeps_fifty_lines(page, goto, fake, reads):
    at(page, goto, "/ui/satellites/activity")
    for held in range(1, 56):
        fake.satellite_send("kitchen", {"type": "button", "button": "mode", "action": "release", "held_ms": held})
    expect(activity(page).first).to_have_text("Kitchen Mode released after 55 ms")
    expect(activity(page)).to_have_count(50)
    expect(activity(page).last).to_have_text("Kitchen Mode released after 6 ms")


def test_a_wake_word_heard_by_a_satellite_lights_listening_and_logs_the_score(page, goto, stack, fake, changes):
    """A person saying hey jarvis, through Kitchen's microphones."""
    at(page, goto, "/ui/satellites/activity")
    words_ready(page, stack)
    fake.satellite_mic("kitchen", 8, clip=HEY_JARVIS)
    expect(chip(page, KITCHEN)).to_have_text("Listening", timeout=15_000)
    heard = activity(page).filter(has_text="heard hey jarvis")
    expect(heard).to_have_text([re.compile(r"^Kitchen heard hey jarvis \(0\.\d\d\)$")])
    expect(ww(page, "hey_jarvis").locator(".ww-last")).to_have_text(
        re.compile(r"^Last heard on Kitchen at \d\d:\d\d, scoring 0\.\d\d\.$"))
    # The command it heard is answered, and that ends Listening.
    expect(activity(page).filter(has_text="hey jarvis: ")).to_have_count(1, timeout=20_000)
    expect(chip(page, KITCHEN)).not_to_have_text("Listening", timeout=20_000)


def test_an_injected_wake_is_logged_as_a_test_and_lights_nothing(page, goto, stack, changes):
    """A recorded clip run through the hub's listening path (/inject): its
    events are marked as a test, heard by nobody, so Kitchen's row is not lit."""
    at(page, goto, "/ui/satellites/activity")
    words_ready(page, stack)
    chips: set[str] = set()
    result = stack.inject("kitchen", (FIXTURES / HEY_JARVIS).read_bytes())
    assert result["heard"]["wake_word"] == "hey_jarvis"
    expect(activity(page).filter(has_text="test: heard")).to_have_text(
        [re.compile(r"^Kitchen test: heard hey jarvis \(\d\.\d\d\)$")])
    expect(activity(page).filter(has_text="test: hey jarvis")).to_have_count(1)
    for _ in range(10):
        chips.add(chip(page, KITCHEN).text_content())
        page.wait_for_timeout(100)
    assert chips == {"Online"}, f"an injected wake lit the row: {chips}"


def test_a_wake_word_the_double_check_rejects_is_logged_in_activity(page, goto, stack, fake, changes):
    """The double-check transcribes the wake word's own audio; the fake STT
    hears something else, so in Record only the wake goes ahead and Activity
    says what On would have done."""
    at(page, goto, "/ui/satellites/activity")
    words_ready(page, stack)
    fake.transcript("the weather today")
    try:
        fake.satellite_mic("kitchen", 8, clip=HEY_JARVIS)
        expect(activity(page).filter(has_text="would have been ignored")).to_have_text(
            ["Kitchen hey jarvis would have been ignored: heard “the weather today”"], timeout=15_000)
    finally:
        fake.reset()


def test_a_conversation_is_one_state_on_the_row_and_two_lines_in_activity(page, goto, stack, fake, changes):
    """Push-to-talk set to a conversation, and Play pressed: one chip for the
    whole of it, though the hub's phases alternate under it, and a line in
    Activity where it starts and one where it ends."""
    with hub(stack) as h:
        ptt = h.get("/satellites/wake-words").json()["ptt"]
    put_words(stack, ptt=dict(ptt, mode="conversation"))
    at(page, goto, "/ui/satellites/activity")
    fake.satellite_mic("kitchen", 20)
    fake.satellite_button("kitchen", "play", "press")
    expect(chip(page, KITCHEN)).to_have_text("In conversation", timeout=10_000)
    chips: set[str] = set()

    def ended() -> bool:
        chips.add(chip(page, KITCHEN).text_content())
        return activity(page).filter(has_text="conversation ended").count() > 0

    until(page, ended, "the conversation's end", seconds=30)
    assert chips - {"In conversation", "Online"} == set(), f"the row flipped between states: {chips}"
    expect(activity(page).filter(has_text="conversation with")).to_have_text(
        ["Kitchen conversation with push-to-talk started"])
    expect(activity(page).filter(has_text="conversation ended")).to_have_text(
        [re.compile(r"^Kitchen conversation ended after \d+ turns?: .")])
    expect(chip(page, KITCHEN)).to_have_text("Online", timeout=10_000)


# ---- Telemetry -----------------------------------------------------------------------------


def test_telemetry_is_off_by_default_and_the_summary_says_so(page, goto, reads):
    at(page, goto, "/ui/satellites")
    expect(page.locator("#tm-sum")).to_have_text("off")
    page.locator("#sat-telemetry > summary").click()
    expect(page.locator("#tmon")).not_to_be_checked()
    expect(page.locator("#tmlevel")).to_have_value("full")
    expect(page.locator("#tmdays")).to_have_value("14")
    expect(page.locator("#tmsize")).to_have_text("Nothing recorded yet.")
    expect(page.locator("#tmdownload")).to_be_hidden()
    expect(page.locator("#tmdelete")).to_be_disabled()


def test_turning_telemetry_on_and_choosing_a_level_are_saved(page, goto, stack, browser_log, changes):
    at(page, goto, "/ui/satellites/telemetry")
    page.locator("#tmon").click()
    expect(page.locator("#tm-sum")).to_have_text("recording everything")
    expect(page.locator("#tmon")).to_be_checked()
    page.locator("#tmlevel").select_option("timings")
    expect(page.locator("#tm-sum")).to_have_text("recording timings only")
    page.locator("#tmdays").fill("30")
    page.locator("#tmdays").press("Tab")
    until(page, lambda: len(sent(browser_log, "PUT", r"^/ui/api/satellites/telemetry$")) == 3, "three changes")
    assert bodies(browser_log, "PUT", r"/telemetry$") == [
        {"enabled": True}, {"level": "timings"}, {"retention_days": 30}]
    with hub(stack) as h:
        state = h.get("/satellites/telemetry").json()
    assert (state["enabled"], state["level"], state["retention_days"]) == (True, "timings", 30)


def test_days_kept_outside_one_to_365_is_refused_before_sending(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/telemetry")
    for days in ("0", "400", "2.5"):
        page.locator("#tmdays").fill(days)
        page.locator("#tmdays").press("Tab")
        expect(page.locator("#tmnote")).to_have_text("Days kept is a whole number from 1 to 365.")
        page.locator("#tmnote").evaluate("n => n.textContent = ''")
    assert not sent(browser_log, "PUT", r"/telemetry$")


def test_recorded_telemetry_can_be_downloaded_and_deleted(page, goto, stack, fake, browser_log, dialogs, changes):
    with hub(stack) as h:
        h.put("/satellites/telemetry", json={"enabled": True, "level": "full"}).raise_for_status()
        fake.satellite_status("kitchen")
        until(page, lambda: h.get("/satellites/telemetry").json().get("bytes", 0) > 0, "a record on the hub")
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/telemetry")
    expect(page.locator("#tmsize")).to_have_text(re.compile(r"^\d+ KB kept, over 1 day\.$"))
    with page.expect_download() as got:
        page.locator("#tmdownload").click()
    download = got.value
    assert download.suggested_filename == "telemetry.json"
    json.loads(Path(download.path()).read_text())
    page.locator("#tmdelete").click()
    until(page, lambda: len(dialogs.seen) == 1, "the question")
    assert dialogs.seen[0][1] == ("Delete every telemetry record and kept clip on the hub? "
                                  "Its settings stay as they are.")
    assert not sent(browser_log, "DELETE", r"/telemetry$")
    page.locator("#tmdelete").click()
    expect(page.locator("#tmsize")).to_have_text("Nothing recorded yet.")
    assert len(sent(browser_log, "DELETE", r"^/ui/api/satellites/telemetry$")) == 1
    expect(page.locator("#tmdownload")).to_be_hidden()
    expect(page.locator("#tmon")).to_be_checked()


# ---- Firmware ------------------------------------------------------------------------------


def bin_file(version: str = "v0.3.0") -> dict:
    return {"name": "firmware.bin", "mimeType": "application/octet-stream", "buffer": image(version)}


def fw_row(page, version: str):
    return page.locator("#fwlist > li.fw").filter(has=page.locator(".sat-name", has_text=re.compile(
        rf"^{re.escape(version)}$")))


def test_uploading_an_image_needs_a_file_a_version_and_a_model(page, goto, browser_log, reads):
    at(page, goto, "/ui/satellites/firmware")
    note = page.locator("#fwnote")
    page.locator("#fwupload").click()
    expect(note).to_have_text("Choose a .bin first.")
    page.locator("#fwfile").set_input_files(bin_file())
    page.locator("#fwupload").click()
    expect(note).to_have_text("Type the version its build stamped: git describe --always --dirty --tags, "
                              "run where it was built.")
    expect(page.locator("#fwversion")).to_be_focused()
    page.locator("#fwversion").fill("v0.3.0")
    page.locator("#fwmodel").fill("")
    page.locator("#fwupload").click()
    expect(note).to_have_text("Type the model the satellites report, as each one's Device shows it.")
    expect(page.locator("#fwmodel")).to_be_focused()
    assert not sent(browser_log, "POST", r"/satellites/firmware$")


def test_the_model_defaults_to_the_one_the_satellites_run_until_typed(page, goto, reads):
    at(page, goto, "/ui/satellites/firmware")
    model = page.locator("#fwmodel")
    expect(model).to_have_value(KORVO)
    # Emptied without typing, the next poll writes it again.
    model.evaluate("el => el.value = ''")
    refreshed(page)
    expect(model).to_have_value(KORVO)
    model.fill("my-board")
    page.locator("#fwversion").focus()
    refreshed(page)
    page.wait_for_function("() => !SATELLITES.polling")
    expect(model).to_have_value("my-board")


def test_an_uploaded_signed_image_is_offered_to_its_satellites(page, goto, stack, browser_log, changes):
    at(page, goto, "/ui/satellites/firmware")
    expect(page.locator("#fw-sum")).to_have_text("no images")
    page.locator("#fwfile").set_input_files(bin_file("v0.3.0"))
    page.locator("#fwversion").fill("v0.3.0")
    page.locator("#fwsig").fill(UNCHECKED_SIGNATURE)
    page.locator("#fwupload").click()
    expect(page.locator("#fwnote")).to_have_text("Uploaded v0.3.0.")
    upload = sent(browser_log, "POST", r"^/ui/api/satellites/firmware$")
    assert len(upload) == 1
    assert upload[0]["query"] == f"model={KORVO}&version=v0.3.0&signature={UNCHECKED_SIGNATURE}"
    with hub(stack) as h:
        held = h.get("/satellites/firmware").json()["firmware"]
    assert [(f["version"], f["model"], f["size"]) for f in held] == [("v0.3.0", KORVO, len(image("v0.3.0")))]
    for box in ("#fwversion", "#fwsig"):
        expect(page.locator(box)).to_have_value("")
    row = fw_row(page, "v0.3.0")
    expect(row.locator(".sat-line")).to_have_text(re.compile(rf"^{KORVO} · 0\.06 MB · signed · \d\d:\d\d$"))
    expect(page.locator("#fw-sum")).to_have_text("v0.3.0 is ready for 1 satellite")
    expect(device(page).locator("summary .sum-note")).to_have_text("v0.2.0 · update available")
    expect(device(page).locator('.sat-update [data-act="update"]')).to_have_text("Update to v0.3.0")
    expect(device(page).locator(".sat-update .hint")).to_have_text("v0.3.0 is available for this model.")
    # The Pi runs another model, so nothing is offered to it.
    expect(device(page, LOUNGE).locator(".sat-update")).to_be_hidden()


def test_update_every_satellite_updates_only_those_due_and_follows_their_progress(page, goto, stack, browser_log,
                                                                                dialogs, changes):
    with hub(stack) as h:
        h.post(f"/satellites/{HALLWAY}/adopt", json={"name": "Hallway"}).raise_for_status()
    image_v3 = upload_firmware(stack, "v0.3.0")
    dialogs()
    at(page, goto, "/ui/satellites/firmware")
    expect(chip(page, HALLWAY)).to_have_text("Online", timeout=15_000)
    go = fw_row(page, "v0.3.0").locator('[data-fw="all"]')
    expect(go).to_be_enabled()
    go.click()
    expect(page.locator("#fwnote")).to_have_text("Updating 2 satellites.", timeout=15_000)
    assert dialogs.seen == [("confirm", "Update 2 satellites to v0.3.0? Each one reboots when its transfer ends.")]
    asked = bodies(browser_log, "POST", r"^/ui/api/satellites/ota$")
    assert sorted(a["satellite"] for a in asked) == [KITCHEN, HALLWAY]
    assert {a["sha256"] for a in asked} == {image_v3["sha256"]}
    for nid in (KITCHEN, HALLWAY):
        expect(device(page, nid).locator("summary .sum-note")).to_have_text("v0.3.0", timeout=30_000)
    expect(fw_row(page, "v0.3.0").locator(".fw-why")).to_have_text(
        "Every satellite of this model that is online already runs it.")
    expect(go).to_be_disabled()


def test_an_unsigned_image_is_skipped_for_a_signing_satellite_and_says_why(page, goto, stack, browser_log, dialogs,
                                                                         changes):
    upload_firmware(stack, "v0.3.0", signed=False)
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    device(page).locator('.sat-update [data-act="update"]').click()
    note = device(page).locator(":scope > .body > .sat-note")
    expect(note).to_have_text(re.compile(r"^Not started: .+\.$"))
    expect(note.locator(".note")).to_have_class(re.compile(r"\bbad\b"))
    assert len(sent(browser_log, "POST", r"/satellites/ota$")) == 1
    expect(chip(page, KITCHEN)).to_have_text("Online")


def test_a_satellites_own_update_button_asks_and_starts_its_update(page, goto, stack, browser_log, dialogs, changes):
    image_v3 = upload_firmware(stack, "v0.3.0")
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    device(page).locator('.sat-update [data-act="update"]').click()
    expect(device(page).locator(":scope > .body > .sat-note")).to_have_text(
        "Updating to v0.3.0; its row shows the progress.")
    assert dialogs.seen == [("confirm", "Update Kitchen to v0.3.0? It reboots when the transfer ends.")]
    assert bodies(browser_log, "POST", r"^/ui/api/satellites/ota$") == [
        {"satellite": KITCHEN, "sha256": image_v3["sha256"]}]
    expect(device(page).locator("summary .sum-note")).to_have_text("v0.3.0", timeout=30_000)
    expect(device(page).locator(".sat-update")).to_be_hidden()


def test_an_update_writes_one_activity_line_that_follows_its_progress(page, goto, stack, dialogs, changes):
    upload_firmware(stack, "v0.3.0")
    dialogs()
    at(page, goto, "/ui/satellites/kitchen/device")
    page.locator("#sat-activity > summary").click()
    device(page).locator('.sat-update [data-act="update"]').click()
    expect(activity(page).filter(has_text="now on v0.3.0")).to_have_count(1, timeout=30_000)
    taking = activity(page).filter(has_text=re.compile(r"update to v0\.3\.0 started|updating to v0\.3\.0"))
    expect(taking).to_have_count(1)
    expect(taking).to_have_text([re.compile(r"^Kitchen (update to v0\.3\.0 started|updating to v0\.3\.0: \d+%)$")])


def test_an_older_image_offers_roll_back_named_as_one(page, goto, stack, browser_log, dialogs, changes):
    upload_firmware(stack, "v0.3.0")
    upload_firmware(stack, "v0.1.0")
    dialogs(answer=False)
    at(page, goto, "/ui/satellites/firmware")
    newer, older = fw_row(page, "v0.3.0"), fw_row(page, "v0.1.0")
    expect(newer.get_by_role("button", name="Update every satellite to v0.3.0")).to_be_visible()
    expect(newer.locator('[data-fw="back"]')).to_have_count(0)
    back = older.get_by_role("button", name="Roll back every satellite to v0.1.0")
    expect(older.locator('[data-fw="all"]')).to_have_count(0)
    back.click()
    until(page, lambda: dialogs.seen, "the question")
    assert dialogs.seen == [("confirm", "Roll back Kitchen to v0.1.0, an older image? "
                                        "It reboots when the transfer ends.")]
    assert not sent(browser_log, "POST", r"/satellites/ota$")


def test_update_every_satellite_is_greyed_with_the_reason_when_none_is_due(page, goto, stack, changes):
    upload_firmware(stack, "v0.2.0")
    upload_firmware(stack, "v1.0.0", model="other-board")
    at(page, goto, "/ui/satellites/firmware")
    same, other = fw_row(page, "v0.2.0"), fw_row(page, "v1.0.0")
    expect(same.locator('[data-fw="all"]')).to_be_disabled()
    expect(same.locator(".fw-why")).to_have_text("Every satellite of this model that is online already runs it.")
    expect(other.locator('[data-fw="all"]')).to_be_disabled()
    expect(other.locator(".fw-why")).to_have_text("No satellite of this model is online.")
    expect(page.locator("#fw-sum")).to_have_text("every satellite is up to date")


def test_deleting_an_image_asks_first_and_moves_the_focus(page, goto, stack, browser_log, dialogs, changes):
    first = upload_firmware(stack, "v0.3.0")
    upload_firmware(stack, "v0.2.0")
    answers = iter([False, True])
    dialogs(answer=lambda dialog: next(answers))
    at(page, goto, "/ui/satellites/firmware")
    delete = fw_row(page, "v0.3.0").get_by_role("button", name="Delete firmware v0.3.0")
    delete.click()
    until(page, lambda: dialogs.seen, "the question")
    assert dialogs.seen == [("confirm", "Delete firmware v0.3.0? Satellites already on it keep it.")]
    assert not sent(browser_log, "DELETE", r"/firmware/")
    delete.click()
    expect(fw_row(page, "v0.3.0")).to_have_count(0)
    assert len(sent(browser_log, "DELETE", rf"^/ui/api/satellites/firmware/{first['sha256']}$")) == 1
    # The image that took its place: its first button that is not greyed.
    assert focused(page, "e => [e.closest('li.fw') && e.closest('li.fw').querySelector('.sat-name').textContent,"
                         " e.textContent]") == ["v0.2.0", "Delete"]


# ---- design ----------------------------------------------------------------------------------


def test_the_ring_arrows_are_drawn_glyphs_with_their_labels(page, goto, reads):
    """The arrows were two triangles from a font; they are drawn, and the
    labels that say what each does are where they were."""
    at(page, goto, "/ui/satellites/kitchen/device")
    row = sat(page, KITCHEN)
    for act, name in (("ring-prev", "Move the lit LED back"), ("ring-next", "Move the lit LED on")):
        arrow = row.locator(f'[data-act="{act}"]')
        expect(arrow).to_have_attribute("aria-label", name)
        expect(arrow.locator("svg.glyph.line")).to_have_count(1)
        assert arrow.evaluate("e => e.textContent.trim()") == "", f"{act} still carries a typed glyph"


def test_every_row_button_is_44px_on_a_touch_screen(new_page, goto, reads):
    """Small buttons in a row took their height from a token the
    coarse-pointer floor did not reach, so they stayed at 38px under a thumb."""
    page = new_page("mobile")
    goto("/ui/satellites/kitchen/device", target=page)
    settled(page)
    heights = page.locator(ROW_BUTTONS).evaluate_all(
        "els => els.filter(e => e.checkVisibility()).map(e => [e.textContent.trim(), e.getBoundingClientRect().height])")
    assert len(heights) >= 3, f"too few row buttons to measure: {heights}"
    short = [(name, h) for name, h in heights if h < 43.5]
    assert not short, f"buttons under a thumb's 44px: {short}"
