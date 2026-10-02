"""The Vocabulary tab, in a browser, against the local stack.

The fake stt-stack starts with the two built-in profiles, dictation and tech,
read from services/stt/glossaries, and a volume it can write to. Every test
here starts from that: the autouse fixture below resets the fakes, so a
profile one test saved, or a deployment one test made read-only, is gone
before the next. `fake.glossaries()` switches the two things a real
deployment varies: whether it can write at all (and its reason when not), and
the service's single-word rule, which refuses `belly = Belli` unless the save
sends force.

What the page sent is read from browser_log (the browser's side) and from the
fake's request log (what reached the service); what it shows is read from the
page. "Another device" is plain httpx to the gateway with an admin key of the
same admin the page is signed in as, never a second browser.
The flow inventory is in the spec's §6; the addresses are in
services/ui/README.md, "Addresses".
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect
from test_routes import here, history_length, open_tab, settled, wait_for_address

GLOSSARIES = Path(__file__).resolve().parents[2] / "stt" / "glossaries"
TECH = (GLOSSARIES / "tech.txt").read_text("utf-8")
NAME_RULE = "A name takes lower case letters, digits, and the . - _ characters."
NO_VOLUME = ("nothing is mounted at /glossaries: this deployment has no writable glossary volume, "
             "so a profile written here would be gone on the next restart. Mount a volume there "
             "and restart")


@pytest.fixture(autouse=True)
def profiles(fake):
    """The two built-ins, a writable volume and the lenient parser, whatever
    the test before this one saved or switched."""
    fake.reset()


# ---- helpers ---------------------------------------------------------------------------


def another_device(stack) -> httpx.Client:
    """The admin's phone: the same person, with an admin key."""
    return stack.client()


def listings(browser_log) -> list[dict]:
    """The reader's own listing of the profiles. An admin's page also lists
    everyone's (?owner=all), to group other people's under their names (§4.4),
    and that is a second request per read, not a second read."""
    return [r for r in browser_log.sent("GET", r"^/glossaries$") if not r["query"]]


def served(stack) -> dict[str, dict]:
    """The listing as the service holds it, asked for by another device."""
    with another_device(stack) as http:
        return {g["name"]: g for g in http.get("/glossaries").json()["glossaries"]}


def until(page, condition, what: str, seconds: float = 10.0) -> None:
    """A condition on the test's side (a dialog answered, a request seen)."""
    ends = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < ends, f"never happened: {what}"
        page.wait_for_timeout(100)


def profile_button(page, name: str):
    return page.locator(f'#glossnames [data-open="{name}"]')


def chooser_button(page, name: str):
    """The same profile in the Transcribe tab's chooser."""
    return page.locator(f'#gloss [data-gloss="{name}"]')


def editor(page):
    """The editor's controls, by what they are."""
    return {k: page.locator(f"#gloss{k}") for k in
            ("name", "text", "save", "force", "delete", "new", "source", "terms", "bytes", "why", "note",
             "form", "man", "none")}


def size(text: str) -> str:
    """The size pill's own sum: UTF-8 bytes, in KB to one place."""
    return f"{len(text.encode()) / 1024:.1f} KB"


# ---- the list --------------------------------------------------------------------------


def test_the_vocabulary_tab_lists_the_services_profiles(page, goto, stack, browser_log):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    group = page.get_by_role("group", name="Vocabulary profiles")
    expect(group.locator("[data-open]")).to_have_text(["dictation", "tech"])
    for name, g in served(stack).items():
        expect(profile_button(page, name)).to_have_attribute("title", f"Built-in, {g['terms']} terms")
        expect(profile_button(page, name)).to_have_attribute("aria-pressed", "false")
    expect(e["none"]).to_be_hidden()
    expect(e["form"]).to_be_hidden()
    expect(e["new"]).to_be_enabled()
    expect(e["why"]).to_have_text("")
    expect(e["note"]).to_have_text("")
    assert page.locator("#tab-vocab h2").count() == 0, "the card repeats the tab's name"
    assert len(listings(browser_log)) == 1
    assert not browser_log.sent("GET", r"^/glossaries/"), "a profile was read before one was opened"


def test_the_tab_says_it_is_asking_until_the_service_answers(page, goto, fake):
    fake.fail(r"^/glossaries$", status=None, delay=2, times=1)
    goto("/ui/vocabulary")
    e = editor(page)
    expect(e["none"]).to_have_text("Asking the transcription service for its vocabulary profiles…")
    expect(e["none"]).to_be_visible()
    expect(e["man"]).to_be_hidden()
    expect(profile_button(page, "tech")).to_be_visible(timeout=10_000)
    expect(e["none"]).to_be_hidden()


def test_no_vocabulary_service_says_so_instead_of_an_empty_heading(page, goto, fake, browser_log):
    """A 404 is a deployment without the service, and keeps that sentence. A
    503 is a service that did not answer this time, which is not the same
    thing and no longer reads as if it were."""
    browser_log.allow(404, r"^/glossaries$")
    browser_log.allow(503, r"^/glossaries$")
    e = editor(page)
    fake.fail(r"^/glossaries$", status=404, json_body={"detail": "Not Found"})
    goto("/ui/vocabulary")
    expect(e["none"]).to_have_text("This deployment has no vocabulary service, so there "
                                   "are no profiles to manage here.")
    expect(e["man"]).to_be_hidden()
    open_tab(page, "transcribe")
    expect(page.locator("#glossbox")).to_be_hidden()

    fake.clear_failures()
    fake.fail(r"^/glossaries$", status=503)
    goto("/ui/vocabulary")
    expect(e["none"]).to_have_text("The transcription service did not answer, so its profiles cannot be "
                                   "listed. This tab asks again when you come back to it.")
    expect(e["man"]).to_be_hidden()


def test_a_service_holding_no_profiles_offers_only_new_profile(page, goto, fake):
    """An answer with nothing in it is not the same as no answer: the panel
    is there, New profile works, and the empty row of names draws nothing
    rather than a 2px box."""
    fake.fail(r"^/glossaries$", status=200, json_body={"glossaries": [], "writable": True, "default": []})
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    expect(e["man"]).to_be_visible()
    expect(e["none"]).to_be_hidden()
    expect(page.locator("#glossnames")).to_be_hidden()
    expect(e["new"]).to_be_enabled()
    e["new"].click()
    expect(e["name"]).to_be_focused()
    open_tab(page, "transcribe")
    expect(page.locator("#glossbox")).to_be_hidden()


# ---- opening a profile -----------------------------------------------------------------


def test_opening_a_profile_shows_its_file_text_and_source(page, goto, stack, browser_log):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    terms = served(stack)["tech"]["terms"]
    before = history_length(page)
    profile_button(page, "tech").click()
    wait_for_address(page, "/ui/vocabulary/tech")
    expect(e["form"]).to_be_visible()
    expect(e["name"]).to_have_value("tech")
    # The file as written, comments and all, not a rebuild from the parsed halves.
    expect(e["text"]).to_have_value(TECH)
    expect(e["source"]).to_have_text("Built-in")
    expect(e["terms"]).to_have_text(f"{terms} terms")
    expect(e["bytes"]).to_have_text(size(TECH))
    # Read-only and not disabled, so a built-in can still be selected and copied.
    expect(e["text"]).not_to_be_editable()
    expect(e["text"]).to_be_enabled()
    assert e["text"].evaluate("t => { t.select(); return t.selectionEnd - t.selectionStart; }") == len(TECH)
    expect(e["save"]).to_be_disabled()
    expect(e["delete"]).to_be_disabled()
    expect(e["why"]).to_have_text('"tech" is a built-in profile. Save it under a new name to change it.')
    expect(profile_button(page, "tech")).to_have_attribute("aria-pressed", "true")
    expect(profile_button(page, "dictation")).to_have_attribute("aria-pressed", "false")
    assert page.title() == "tech · Vocabulary · Calliope"
    assert history_length(page) == before + 1, "opening a profile is a step Back undoes"
    assert len(browser_log.sent("GET", r"^/glossaries/tech$")) == 1

    # The keyboard opens one the same way.
    profile_button(page, "dictation").focus()
    page.keyboard.press("Enter")
    expect(e["name"]).to_have_value("dictation")
    expect(e["text"]).to_have_value((GLOSSARIES / "dictation.txt").read_text("utf-8"))
    wait_for_address(page, "/ui/vocabulary/dictation")


def test_a_profile_button_is_pressed_at_once_while_its_text_is_on_the_way(page, goto, fake):
    """The press is the acknowledgement: the button shows it before the
    profile arrives, not 300 ms later with the answer."""
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    fake.fail(r"^/glossaries/tech$", status=None, delay=1.5, times=1)
    profile_button(page, "tech").click()
    expect(profile_button(page, "tech")).to_have_attribute("aria-pressed", "true", timeout=1000)
    expect(e["form"]).to_be_hidden()
    expect(e["name"]).to_have_value("tech", timeout=10_000)
    expect(e["form"]).to_be_visible()


def test_a_profile_that_cannot_be_read_says_why_and_shows_no_editor(page, goto, fake, browser_log):
    browser_log.allow(500, r"^/glossaries/tech$")
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    fake.fail(r"^/glossaries/tech$", status=500, json_body={"detail": "the volume could not be read"}, times=1)
    profile_button(page, "tech").click()
    expect(e["note"].locator(".note.bad")).to_have_text("the volume could not be read")
    expect(e["form"]).to_be_hidden()
    expect(profile_button(page, "tech")).to_have_attribute("aria-pressed", "false")
    # Asked again, it opens: the failure was the read's, not the profile's.
    profile_button(page, "tech").click()
    expect(e["name"]).to_have_value("tech")
    expect(e["note"]).to_have_text("")


# ---- new, copy, name ------------------------------------------------------------------


def test_new_profile_starts_empty_with_the_name_focused(page, goto, browser_log):
    goto("/ui/vocabulary/tech")
    settled(page)
    e = editor(page)
    expect(e["name"]).to_have_value("tech")
    before = history_length(page)
    e["new"].click()
    expect(e["name"]).to_be_focused()
    expect(e["name"]).to_have_value("")
    expect(e["text"]).to_have_value("")
    expect(e["text"]).to_be_editable()
    expect(e["source"]).to_have_text("New")
    expect(e["terms"]).to_have_text("not saved")
    expect(e["bytes"]).to_have_text("0.0 KB")
    expect(e["save"]).to_be_disabled()
    expect(e["delete"]).to_be_disabled()
    expect(e["why"]).to_have_text("")
    expect(page.locator('#glossnames [aria-pressed="true"]')).to_have_count(0)
    # A profile not yet saved has no address: the entry goes back to the tab's own.
    wait_for_address(page, "/ui/vocabulary")
    assert history_length(page) == before
    assert page.title() == "Vocabulary · Calliope"
    e["name"].press_sequentially("notes")
    expect(e["save"]).to_be_enabled()
    expect(e["source"]).to_have_text("New")
    assert not browser_log.sent("PUT"), "typing a name wrote something"


def test_a_built_in_profile_can_be_copied_to_a_new_name_and_saved(page, goto, stack, fake, browser_log):
    """The documented way out of a built-in: type a new name over it, and
    the panel becomes a new profile holding the built-in's text."""
    goto("/ui/vocabulary/tech")
    settled(page)
    e = editor(page)
    terms = served(stack)["tech"]["terms"]
    expect(e["text"]).to_have_value(TECH)
    e["name"].fill("mytech")
    expect(e["source"]).to_have_text("New")
    expect(e["terms"]).to_have_text("not saved")
    expect(e["text"]).to_be_editable()
    expect(e["save"]).to_be_enabled()
    expect(e["delete"]).to_be_disabled()
    expect(e["why"]).to_have_text("")
    expect(page.locator('#glossnames [aria-pressed="true"]')).to_have_count(0)

    before = history_length(page)
    e["save"].click()
    expect(e["note"]).to_contain_text("Saved")
    wait_for_address(page, "/ui/vocabulary/mytech")
    assert history_length(page) == before, "a save rewrites the entry it is on"
    assert page.title() == "mytech · Vocabulary · Calliope"
    put = browser_log.sent("PUT", r"^/glossaries/")
    assert [(p["path"], p["query"], p["json"]) for p in put] == [
        ("/glossaries/mytech", "", {"text": TECH})]
    assert [r["status"] for r in fake.requests(backend="stt", method="PUT")] == [201]
    expect(profile_button(page, "mytech")).to_have_attribute("title", f"Custom, {terms} terms")
    expect(profile_button(page, "mytech")).to_have_attribute("aria-pressed", "true")
    expect(e["source"]).to_have_text("Custom")
    expect(e["terms"]).to_have_text(f"{terms} terms")
    expect(e["delete"]).to_be_enabled()
    expect(chooser_button(page, "mytech")).to_have_attribute("aria-pressed", "false")
    assert served(stack)["mytech"]["source"] == "custom"


def test_the_saved_note_names_the_profile_in_plain_words(page, goto):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("plain")
    e["text"].fill("kuber netes = Kubernetes\nnginx\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("Saved")
    expect(e["note"]).to_have_text("Saved plain, 2 terms.", timeout=1000)


def test_a_name_the_service_would_refuse_greys_save_with_the_reason(page, goto, browser_log):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["text"].fill("nginx\n")
    for refused in ("my notes", "-notes", "notes/../x", "über"):
        e["name"].fill(refused)
        expect(e["why"]).to_have_text(NAME_RULE)
        expect(e["save"]).to_be_disabled()
        expect(e["delete"]).to_be_disabled()

    # A built-in's name is taken, and its text cannot be typed over either.
    e["name"].fill("dictation")
    expect(e["why"]).to_have_text('"dictation" is a built-in profile. Save it under a new name to change it.')
    expect(e["save"]).to_be_disabled()
    expect(e["text"]).not_to_be_editable()
    expect(e["source"]).to_have_text("Built-in")

    # 64 characters is the service's own ceiling, so the 65th key does nothing.
    e["name"].fill("")
    e["name"].press_sequentially("a" * 70)
    expect(e["name"]).to_have_value("a" * 64)
    expect(e["save"]).to_be_enabled()

    # Spaces round it and capitals in it are not refusals: the name is trimmed
    # and lower-cased, which is what the service files it under.
    e["name"].fill("  Notes.v2_x  ")
    expect(e["why"]).to_have_text("")
    expect(e["save"]).to_be_enabled()
    assert not browser_log.sent("PUT"), "a refused name was sent"
    e["save"].click()
    wait_for_address(page, "/ui/vocabulary/notes.v2_x")
    assert [p["path"] for p in browser_log.sent("PUT")] == ["/glossaries/notes.v2_x"]
    expect(profile_button(page, "notes.v2_x")).to_be_visible()


def test_the_name_box_decides_what_save_and_delete_act_on(page, goto, stack, dialogs, browser_log):
    """One rule settles the whole panel: what the typed name resolves to in
    the listing, never which name button was pressed."""
    with another_device(stack) as http:
        http.put("/glossaries/team", json={"text": "kuber netes = Kubernetes\nnginx\n"}).raise_for_status()
    goto("/ui/vocabulary/tech")
    settled(page)
    e = editor(page)
    expect(e["delete"]).to_be_disabled()
    e["name"].fill("team")
    expect(e["source"]).to_have_text("Custom")
    expect(e["terms"]).to_have_text("2 terms")
    expect(e["delete"]).to_be_enabled()
    expect(e["text"]).to_be_editable()
    expect(profile_button(page, "team")).to_have_attribute("aria-pressed", "true")
    expect(profile_button(page, "tech")).to_have_attribute("aria-pressed", "false")
    dialogs(answer=True)
    e["delete"].click()
    expect(e["note"]).to_have_text("Deleted team.")
    assert [d["path"] for d in browser_log.sent("DELETE")] == ["/glossaries/team"]
    assert "team" not in served(stack)


# ---- what the service refuses ----------------------------------------------------------


def test_a_profile_over_64_kilobytes_greys_save_and_marks_the_size(page, goto, browser_log):
    """Bytes, not characters: 33,000 accented letters are 66,000 bytes, so a
    count of characters would have called this comfortable and sent it."""
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("big")
    big = "é" * 33_000
    e["text"].fill(big)
    expect(e["bytes"]).to_have_text(size(big))
    expect(e["bytes"]).to_have_class("pill bad")
    expect(e["why"]).to_have_text(f"That is {size(big)}. The ceiling is 64 KB.")
    expect(e["save"]).to_be_disabled()
    under = "é" * 32_000
    e["text"].fill(under)
    expect(e["bytes"]).to_have_text(size(under))
    expect(e["bytes"]).to_have_class("pill")
    expect(e["why"]).to_have_text("")
    expect(e["save"]).to_be_enabled()
    assert not browser_log.sent("PUT"), "a profile over the ceiling was sent"


BROKEN = "kuber netes = Kubernetes\n= nothing heard\nnginx\nempty side =\n"


def test_lines_the_service_refuses_are_listed_by_number_and_nothing_is_written(page, goto, stack, fake,
                                                                               browser_log):
    browser_log.allow(400, r"^/glossaries/broken$")
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("broken")
    e["text"].fill(BROKEN)
    e["save"].click()
    expect(e["note"]).to_contain_text("2 line(s) rejected; nothing was written.")
    expect(e["note"]).to_contain_text("Line 2: = nothing heard")
    expect(e["note"]).to_contain_text("Line 4: empty side =")
    expect(e["note"].locator(".note")).to_have_class("note bad")
    # Not one a force can answer, so none is offered.
    expect(e["force"]).to_be_hidden()
    # The editor still holds the only copy, and Save is there to try again.
    expect(e["text"]).to_have_value(BROKEN)
    expect(e["save"]).to_be_enabled()
    assert [r["status"] for r in fake.requests(backend="stt", method="PUT")] == [400]
    assert "broken" not in served(stack)
    expect(profile_button(page, "broken")).to_have_count(0)
    wait_for_address(page, "/ui/vocabulary")

    e["text"].fill("kuber netes = Kubernetes\nnginx\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("Saved")
    assert served(stack)["broken"]["terms"] == 2


def test_each_refused_line_is_drawn_on_its_own_with_its_reason(page, goto, browser_log):
    browser_log.allow(400, r"^/glossaries/broken$")
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("broken")
    e["text"].fill(BROKEN)
    e["save"].click()
    expect(e["note"]).to_contain_text("rejected")
    lines = e["note"].locator(".hint")
    expect(lines).to_have_count(2, timeout=1000)
    expect(lines.nth(0)).to_contain_text("Line 2: = nothing heard")
    expect(lines.nth(0)).to_contain_text("a replacement needs text on both sides of '='")
    assert "<div" not in e["note"].inner_text()


def test_save_anyway_appears_only_for_forceable_refusals_and_goes_on_the_next_edit(page, goto, stack, fake,
                                                                                    browser_log):
    """`force` answers the single-word rule and nothing else, and it answers
    one refusal of one body: the moment the text changes, it is withdrawn."""
    browser_log.allow(400, r"^/glossaries/mine$")
    fake.glossaries(strict=True)
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("mine")
    e["text"].fill("belly = Belli\nkuber netes = Kubernetes\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("1 line(s) rejected; nothing was written.")
    expect(e["note"]).to_contain_text("send force")
    expect(e["force"]).to_be_visible()

    forced = "belly = Belli\nkuber netes = Kubernetes\nnginx\n"
    e["text"].fill(forced)
    expect(e["force"]).to_be_hidden()
    e["save"].click()
    expect(e["force"]).to_be_visible()
    e["force"].click()
    expect(e["note"]).to_contain_text("Saved")
    expect(e["force"]).to_be_hidden()
    puts = browser_log.sent("PUT", r"^/glossaries/mine$")
    assert [p["query"] for p in puts] == ["", "", "force=true"]
    assert puts[-1]["json"] == {"text": forced}, "Save anyway sent a body other than the one refused"
    assert served(stack)["mine"]["terms"] == 3

    # A refusal force cannot answer, beside one it can, offers nothing to force.
    e["text"].fill("belly = Belli\n= nothing heard\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("2 line(s) rejected")
    expect(e["force"]).to_be_hidden()
    assert len(browser_log.sent("PUT", r"^/glossaries/mine$")) == 4


def test_save_says_saving_and_is_off_while_the_write_is_in_flight(page, goto, fake):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("slow")
    e["text"].fill("nginx\n")
    fake.fail(r"^/glossaries/slow$", method="PUT", status=None, delay=1.5, times=1)
    e["save"].click()
    expect(e["note"]).to_have_text("Saving…", timeout=1000)
    expect(e["save"]).to_be_disabled()
    expect(e["note"]).to_contain_text("Saved", timeout=10_000)
    expect(e["save"]).to_be_enabled()


def test_saving_an_existing_profile_updates_its_term_count(page, goto, stack, fake, browser_log):
    with another_device(stack) as http:
        http.put("/glossaries/notes", json={"text": "kuber netes = Kubernetes\nnginx\n"}).raise_for_status()
    goto("/ui/vocabulary/notes")
    settled(page)
    e = editor(page)
    expect(e["text"]).to_have_value("kuber netes = Kubernetes\nnginx\n")
    expect(e["source"]).to_have_text("Custom")
    expect(e["terms"]).to_have_text("2 terms")
    expect(e["text"]).to_be_editable()
    expect(e["delete"]).to_be_enabled()
    expect(e["why"]).to_have_text("")
    expect(profile_button(page, "notes")).to_have_attribute("title", "Custom, 2 terms")
    before = history_length(page)

    e["text"].fill("kuber netes = Kubernetes\nnginx\nRedis\nValkey\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("Saved")
    expect(e["terms"]).to_have_text("4 terms")
    expect(profile_button(page, "notes")).to_have_attribute("title", "Custom, 4 terms")
    expect(chooser_button(page, "notes")).to_have_attribute("title", "4 terms")
    assert [r["status"] for r in fake.requests(backend="stt", method="PUT")] == [201, 200]
    assert here(page) == "/ui/vocabulary/notes" and history_length(page) == before


def test_a_write_refused_by_a_read_only_volume_says_the_servers_reason(page, goto, fake, browser_log):
    """The volume can go away after the listing said it was there. The 503
    carries the service's own sentence, and the editor keeps the text."""
    browser_log.allow(503, r"^/glossaries/late$")
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    e["new"].click()
    e["name"].fill("late")
    e["text"].fill("nginx\n")
    fake.glossaries(writable=False, reason=NO_VOLUME)
    e["save"].click()
    expect(e["note"].locator(".note.bad")).to_have_text("glossary profiles are read-only here: " + NO_VOLUME)
    expect(e["text"]).to_have_value("nginx\n")
    expect(e["save"]).to_be_enabled()


# ---- delete ----------------------------------------------------------------------------


def test_deleting_a_profile_asks_first_and_removes_it_from_both_tabs(page, goto, stack, dialogs, browser_log):
    with another_device(stack) as http:
        http.put("/glossaries/gone", json={"text": "nginx\n"}).raise_for_status()
    goto("/ui/vocabulary/gone")
    settled(page)
    e = editor(page)
    expect(e["name"]).to_have_value("gone")
    expect(chooser_button(page, "gone")).to_be_attached()

    answers = [False, True]
    seen = dialogs(answer=lambda dialog: answers.pop(0)).seen
    e["delete"].click()
    until(page, lambda: len(seen) == 1, "the question before deleting")
    assert seen[0] == ("confirm", 'Delete the vocabulary profile "gone"?\nThe file is deleted from disk.')
    assert not browser_log.sent("DELETE"), "a No still deleted"
    expect(e["form"]).to_be_visible()
    expect(profile_button(page, "gone")).to_be_visible()

    e["delete"].click()
    until(page, lambda: len(seen) == 2, "the second question")
    expect(e["form"]).to_be_hidden()
    expect(e["note"]).to_have_text("Deleted gone.")
    assert [d["path"] for d in browser_log.sent("DELETE")] == ["/glossaries/gone"]
    assert "gone" not in served(stack)
    expect(profile_button(page, "gone")).to_have_count(0)
    expect(chooser_button(page, "gone")).to_have_count(0)
    wait_for_address(page, "/ui/vocabulary")
    assert page.title() == "Vocabulary · Calliope"
    open_tab(page, "transcribe")
    expect(page.locator("#gloss [data-gloss]")).to_have_text(["dictation", "tech"])


def test_a_failure_with_quotes_in_it_is_shown_as_the_service_wrote_it(page, goto, stack, dialogs, browser_log):
    """A profile deleted on another device and then deleted here: the
    service's 404 names it in quotes."""
    browser_log.allow(404, r"^/glossaries/vanished$")
    with another_device(stack) as http:
        http.put("/glossaries/vanished", json={"text": "nginx\n"}).raise_for_status()
        goto("/ui/vocabulary/vanished")
        settled(page)
        e = editor(page)
        expect(e["name"]).to_have_value("vanished")
        http.delete("/glossaries/vanished").raise_for_status()
    dialogs(answer=True)
    e["delete"].click()
    expect(e["note"].locator(".note.bad")).to_contain_text("vanished")
    expect(e["note"]).to_have_text("no glossary profile named 'vanished'", timeout=1000)


# ---- a deployment that cannot write ----------------------------------------------------


def test_a_deployment_that_cannot_write_says_why_beside_every_greyed_control(page, goto, stack, fake,
                                                                             browser_log):
    with another_device(stack) as http:
        http.put("/glossaries/mine", json={"text": "nginx\nRedis\n"}).raise_for_status()
    fake.glossaries(writable=False, reason=NO_VOLUME)
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    # With nothing open the reason still stands beside New profile.
    expect(e["new"]).to_be_disabled()
    expect(e["why"]).to_have_text(NO_VOLUME)
    for name in ("mine", "tech"):
        profile_button(page, name).click()
        expect(e["name"]).to_have_value(name)
        expect(e["text"]).not_to_be_editable()
        expect(e["save"]).to_be_disabled()
        expect(e["delete"]).to_be_disabled()
        expect(e["why"]).to_have_text(NO_VOLUME)
    e["name"].fill("another")
    expect(e["save"]).to_be_disabled()
    expect(e["why"]).to_have_text(NO_VOLUME)
    assert not browser_log.sent("PUT") and not browser_log.sent("DELETE")

    # A server that gave no reason of its own still gets a sentence.
    fake.glossaries(writable=False)
    goto("/ui/vocabulary")
    settled(page)
    expect(e["new"]).to_be_disabled()
    expect(e["why"]).to_have_text("This server cannot write a profile.")


# ---- the Transcribe tab's chooser ------------------------------------------------------


def test_the_transcribe_chooser_keeps_its_ticks_after_a_profile_is_saved(page, goto, dialogs):
    goto("/ui")
    settled(page)
    e = editor(page)
    tech, dictation = chooser_button(page, "tech"), chooser_button(page, "dictation")
    # Independent toggles, not a radio group: several at once, each undone alone.
    tech.click()
    dictation.click()
    expect(tech).to_have_attribute("aria-pressed", "true")
    expect(dictation).to_have_attribute("aria-pressed", "true")
    dictation.click()
    expect(dictation).to_have_attribute("aria-pressed", "false")

    open_tab(page, "vocab")
    e["new"].click()
    e["name"].fill("extra")
    e["text"].fill("nginx\n")
    e["save"].click()
    expect(e["note"]).to_contain_text("Saved")
    open_tab(page, "transcribe")
    expect(page.locator("#gloss [data-gloss]")).to_have_text(["dictation", "extra", "tech"])
    expect(tech).to_have_attribute("aria-pressed", "true")
    expect(dictation).to_have_attribute("aria-pressed", "false")
    expect(chooser_button(page, "extra")).to_have_attribute("aria-pressed", "false")
    assert page.evaluate("() => chosenGlossaries()") == ["tech"]

    # And after one is deleted.
    open_tab(page, "vocab")
    profile_button(page, "extra").click()
    expect(e["name"]).to_have_value("extra")
    dialogs(answer=True)
    e["delete"].click()
    expect(e["note"]).to_have_text("Deleted extra.")
    open_tab(page, "transcribe")
    expect(page.locator("#gloss [data-gloss]")).to_have_text(["dictation", "tech"])
    expect(tech).to_have_attribute("aria-pressed", "true")


# ---- addresses -------------------------------------------------------------------------


def test_back_never_throws_away_edits_that_were_not_saved(page, goto, stack, dialogs):
    """A swipe back on a phone is enough. Back to another profile asks first,
    and No keeps the edits and their profile's address."""
    with another_device(stack) as http:
        http.put("/glossaries/mine", json={"text": "nginx\n"}).raise_for_status()
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    profile_button(page, "tech").click()
    wait_for_address(page, "/ui/vocabulary/tech")
    profile_button(page, "mine").click()
    wait_for_address(page, "/ui/vocabulary/mine")
    e["text"].fill("nginx\nRedis\n")
    dialogs(answer=False)
    page.go_back()
    until(page, lambda: dialogs.seen, "the question")
    expect(e["name"]).to_have_value("mine")
    expect(e["text"]).to_have_value("nginx\nRedis\n")
    wait_for_address(page, "/ui/vocabulary/mine")


def test_back_and_forward_reopen_the_profiles_the_reader_opened(page, goto):
    goto("/ui/vocabulary")
    settled(page)
    e = editor(page)
    profile_button(page, "tech").click()
    expect(e["name"]).to_have_value("tech")
    profile_button(page, "dictation").click()
    expect(e["name"]).to_have_value("dictation")
    wait_for_address(page, "/ui/vocabulary/dictation")

    page.go_back()
    wait_for_address(page, "/ui/vocabulary/tech")
    expect(e["name"]).to_have_value("tech")
    expect(e["text"]).to_have_value(TECH)
    expect(page).to_have_title("tech · Vocabulary · Calliope")
    page.go_forward()
    wait_for_address(page, "/ui/vocabulary/dictation")
    expect(e["name"]).to_have_value("dictation")
    page.go_back()
    page.go_back()
    wait_for_address(page, "/ui/vocabulary")
    expect(page).to_have_title("Vocabulary · Calliope")
    # The address says no profile is open, and none is.
    expect(page.locator("#glossform")).to_be_hidden()

    # A reload of a profile's address opens it again.
    page.go_forward()
    wait_for_address(page, "/ui/vocabulary/tech")
    page.reload()
    settled(page)
    expect(e["name"]).to_have_value("tech")
    expect(profile_button(page, "tech")).to_be_focused()


def test_a_profile_address_with_a_name_no_profile_can_have_asks_for_nothing(page, goto, browser_log):
    """A name that is not a filename the service could hold cannot be on the
    service, so the page says so without asking."""
    goto("/ui/vocabulary/My%20Notes")
    settled(page)
    wait_for_address(page, "/ui/vocabulary")
    expect(page.locator("#glossnote")).to_have_text("There is no profile called my notes, so none is open.")
    expect(page.locator("#glossform")).to_be_hidden()
    assert not browser_log.sent("GET", r"^/glossaries/"), "a name no profile can have was asked for"


def test_a_profile_address_is_kept_when_the_service_did_not_answer(page, goto, fake, browser_log):
    """A listing that failed decides nothing: the link is not called wrong,
    it is kept for when the service answers."""
    browser_log.allow(503, r"^/glossaries$")
    fake.fail(r"^/glossaries$", status=503)
    goto("/ui/vocabulary/tech")
    settled(page)
    wait_for_address(page, "/ui/vocabulary/tech")
    expect(page.locator("#glossnone")).to_contain_text("did not answer")
    expect(page.locator("#glossnote")).to_have_text("")
    expect(page).to_have_title("tech · Vocabulary · Calliope")


# ---- live ------------------------------------------------------------------------------


def test_a_profile_deleted_elsewhere_is_said_and_saving_puts_it_back(page, goto, stack, browser_log):
    browser_log.allow(404, r"^/glossaries/shared$")
    text = "kuber netes = Kubernetes\nnginx\n"
    with another_device(stack) as http:
        http.put("/glossaries/shared", json={"text": text}).raise_for_status()
        page.clock.install()
        goto("/ui/vocabulary/shared")
        settled(page)
        e = editor(page)
        expect(e["text"]).to_have_value(text)
        http.delete("/glossaries/shared").raise_for_status()
        page.clock.fast_forward(6_000)
        open_tab(page, "transcribe")
        open_tab(page, "vocab")
        expect(e["note"].locator(".note.warn")).to_have_text(
            "This profile was deleted on another device. Saving puts it back.")
        expect(profile_button(page, "shared")).to_have_count(0)
        expect(chooser_button(page, "shared")).to_have_count(0)
        expect(e["text"]).to_have_value(text)
        expect(e["source"]).to_have_text("New")

        e["save"].click()
        expect(e["note"]).to_contain_text("Saved")
        assert http.get("/glossaries/shared").json()["text"] == text
    expect(profile_button(page, "shared")).to_be_visible()


def test_a_profile_saved_elsewhere_reaches_the_transcribe_chooser_on_the_way_in(page, goto, stack):
    with another_device(stack) as http:
        page.clock.install()
        goto("/ui/vocabulary")
        settled(page)
        http.put("/glossaries/fromphone", json={"text": "nginx\n"}).raise_for_status()
        page.clock.fast_forward(6_000)
        open_tab(page, "transcribe")
        expect(chooser_button(page, "fromphone")).to_be_visible()
        expect(chooser_button(page, "fromphone")).to_have_attribute("aria-pressed", "false")


def test_the_profiles_are_read_again_on_the_way_in_after_five_seconds_and_on_return_after_thirty(
        page, goto, browser_log, hide, show):
    page.clock.install()
    goto("/ui/vocabulary")
    settled(page)

    def reads() -> int:
        return len(listings(browser_log))

    def loaded() -> int:
        return page.evaluate("() => LOADED.glossaries")

    def come_back() -> None:
        """Show the page and wait for the live layer's catch-up to have run."""
        before = page.evaluate("() => LIVE.caughtUp")
        show(page)
        page.wait_for_function("b => LIVE.caughtUp !== b", arg=before)

    assert reads() == 1
    first = loaded()
    # Into the tab again within five seconds: the listing is fresh enough.
    open_tab(page, "transcribe")
    open_tab(page, "vocab")
    assert loaded() == first

    # Back to the page after ten seconds away: still fresh enough on return.
    hide(page)
    page.clock.fast_forward(10_000)
    come_back()
    assert loaded() == first

    # After thirty, it is read again.
    hide(page)
    page.clock.fast_forward(31_000)
    come_back()
    assert loaded() != first
    until(page, lambda: reads() == 2, "the listing read again on return")


def test_reading_the_same_listing_again_redraws_nothing(page, goto):
    """An identical answer leaves every button where it was: a redraw would
    take the focus off a toggle and the ticks with it."""
    page.clock.install()
    goto("/ui/vocabulary/tech")
    settled(page)
    expect(page.locator("#glossname")).to_have_value("tech")
    page.evaluate("""() => {
      document.querySelector('#glossnames [data-open="tech"]').kept = true;
      document.querySelector('#gloss [data-gloss="tech"]').kept = true;
    }""")
    first = page.evaluate("() => LOADED.glossaries")
    page.clock.fast_forward(6_000)
    open_tab(page, "transcribe")
    page.wait_for_function("f => LOADED.glossaries !== f", arg=first)
    page.wait_for_function("() => NAV.settled.has('glossaries')")
    open_tab(page, "vocab")
    expect(page.locator("#glossname")).to_have_value("tech")
    assert page.evaluate("""() => [
      document.querySelector('#glossnames [data-open="tech"]').kept === true,
      document.querySelector('#gloss [data-gloss="tech"]').kept === true]""") == [True, True]


# ---- design ----------------------------------------------------------------------------


def test_the_editor_has_a_gap_between_every_group(page, goto):
    """Three spacing rules reached for a `.body` the editor lost when
    Vocabulary became a tab, so the profile chips, Name, Terms and the actions
    sat flush against one another."""
    goto("/ui/vocabulary/tech")
    settled(page)
    expect(page.locator("#glossform")).to_be_visible()
    edges = page.evaluate("""() => {
      const box = el => el.getBoundingClientRect();
      const chips = document.querySelector("#glossman > .row");
      const fields = document.querySelector("#glossform > .row.fields");
      const terms = document.querySelector('#glossform > label[for="glosstext"]');
      const text = document.getElementById("glosstext");
      const actions = document.querySelector("#glossform > .actions");
      return { chips_to_name: box(fields).top - box(chips).bottom,
               name_to_terms: box(terms).top - box(fields).bottom,
               terms_to_actions: box(actions).top - box(text).bottom };
    }""")
    for gap, px in edges.items():
        assert px >= 12, f"{gap}: {px:.1f}px"


def test_the_source_pills_are_separate_pills(page, goto):
    """Source, Terms and Size are three facts, so three pills with their own
    edges and air between them, and a group named Source."""
    goto("/ui/vocabulary/tech")
    settled(page)
    group = page.get_by_role("group", name="Source")
    expect(group).to_be_visible()
    pills = group.locator(".pill").evaluate_all("""els => els.filter(e => e.checkVisibility()).map(e => {
      const s = getComputedStyle(e), b = e.getBoundingClientRect();
      return { radius: parseFloat(s.borderTopLeftRadius), border: parseFloat(s.borderTopWidth),
               left: b.left, right: b.right, text: e.textContent.trim() };
    })""")
    assert len(pills) >= 2, f"fewer than two facts shown: {pills}"
    for pill in pills:
        assert pill["radius"] >= 8 and pill["border"] >= 1, f"{pill['text']} is not drawn as a pill"
    for one, two in zip(pills, pills[1:]):
        assert two["left"] - one["right"] >= 3, f"{one['text']} and {two['text']} run together"
