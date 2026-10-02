"""Whose each thing is, in a browser: one person's work beside another's and the admin's (D5, D31-D36).

SPEECH makes a run, a vocabulary profile and a voice clip, with a key of the
speech preset through the gateway, as a script of theirs would. OTHER, a
second speech user, must find none of them on any tab; the admin, who holds
the `:all` scopes, finds each one and is told whose it is. A satellite's run is
the hub's, and so in no person's own list at all.

The backends behind the gateway partition what they hold by the identity it
asserts (fakes.py does as the services do), so what is checked here is the
whole path: the page, the gateway's assertion, and the backend's filter.
"""

from __future__ import annotations

import pytest
from conftest import OTHER, SPEECH
from fakes import SATELLITE_TEXT, wav
from playwright.sync_api import expect
from test_routes import settled
from voice_common.scopes import KEY_ID

TEXT = "Sam's chapter, read aloud in his own voice for nobody else."
PROFILE = "sams-words"
CLIP = "sams voice"
CLIP_NAME = "sams-voice"


@pytest.fixture
def sams_work(stack, api_key, people, fake):
    """A run, a profile and a clip of SPEECH's, gone again after the test."""
    fake.reset()
    owner = people(SPEECH)
    since = fake.last_seq()
    with stack.client(api_key("speech", user=SPEECH)) as theirs:
        job = theirs.post("/jobs", json={"text": TEXT, "model": "chatterbox", "voice": CLIP_NAME})
        assert job.status_code == 202, job.text
        profile = theirs.put(f"/glossaries/{PROFILE}", json={"text": "kubernetes = Kubernetes"})
        assert profile.status_code == 201, profile.text
        clip = theirs.post("/ui/clips", data={"name": CLIP},
                           files={"file": ("sams voice.wav", wav(12.0), "audio/wav")})
        assert clip.status_code == 201, clip.text
        made = {"job": job.json()["id"], "owner": owner, "clip": clip.json()["voice"]["name"],
                "since": since}
        yield made
        theirs.delete(f"/ui/clips/{made['clip']}")
        theirs.delete(f"/glossaries/{PROFILE}")
    fake.reset()


def test_one_persons_run_profile_and_clip_are_on_no_tab_of_another(new_page, goto, stack, api_key,
                                                                    sams_work):
    page = new_page(user=OTHER)
    goto("/ui/jobs", target=page)
    settled(page)
    expect(page.locator("#joblist")).not_to_contain_text("Sam's chapter")
    expect(page.locator(f'.job[data-job="{sams_work["job"]}"]')).to_have_count(0)
    expect(page.locator("#jobowner")).to_be_hidden()

    page.locator('[role=tab][data-tab="vocab"]').click()
    settled(page)
    expect(page.locator("#glossnames [data-open]").first).to_be_visible()
    expect(page.locator(f'#glossnames [data-open="{PROFILE}"]')).to_have_count(0)
    expect(page.locator("#glossothers")).to_be_hidden()

    page.locator('[role=tab][data-tab="speak"]').click()
    settled(page)
    assert sams_work["clip"] not in page.locator("#voice option").all_text_contents()
    expect(page.locator("#allclips")).to_be_hidden()

    # And asked straight, the run is not there to be found: 404, not 403 (D5).
    with stack.client(api_key("speech", user=OTHER)) as theirs:
        assert theirs.get(f"/jobs/{sams_work['job']}").status_code == 404
        assert theirs.get(f"/glossaries/{PROFILE}").status_code == 404


def test_a_backend_is_told_who_asked_and_is_handed_none_of_their_credentials(new_page, goto, fake,
                                                                             sams_work):
    """What reaches stt, tts and tts-long carries the gateway's assertion and
    nothing of the person's: no cookie, no Authorization header, no X-Calliope
    header but the gateway's own (D51, D65). The assertion names the person,
    and the key they used or their session (D4)."""
    page = new_page(user=SPEECH)
    goto("/ui/jobs", target=page)
    settled(page)
    reached = [r for r in fake.requests(since=sams_work["since"]) if r["backend"] in ("stt", "tts", "tts_long")]
    for r in reached:
        assert not r["cookie"] and not r["authorization"], r
        assert set(r["calliope_headers"]) <= {"x-calliope-identity"}, r
    asked = [r for r in reached if r["path"] != "/health"]
    assert asked and all(r["calliope_headers"] == ["x-calliope-identity"] and r["identity"]
                         for r in asked), asked
    owner = sams_work["owner"].id
    by_key = [r["identity"] for r in asked
              if (r["method"], r["path"]) in (("POST", "/jobs"), ("PUT", f"/glossaries/{PROFILE}"))]
    assert len(by_key) == 2 and all(i["sub"] == owner and KEY_ID.fullmatch(i["cred"]) for i in by_key), by_key
    by_page = [r["identity"] for r in asked if (r["method"], r["path"]) == ("GET", "/jobs")
               and r["identity"]["cred"] == "session"]
    assert by_page and all(i["sub"] == owner for i in by_page), asked


def test_the_admin_finds_everyones_work_and_is_told_whose_it_is(page, goto, sams_work):
    goto("/ui/jobs")
    settled(page)
    expect(page.locator(f'.job[data-job="{sams_work["job"]}"]')).to_have_count(0)
    page.locator("#jobowner").select_option("all")
    row = page.locator(f'.job[data-job="{sams_work["job"]}"]')
    expect(row).to_be_visible()
    expect(row).to_contain_text(SPEECH)

    page.locator('[role=tab][data-tab="vocab"]').click()
    settled(page)
    theirs = page.locator(f'#glossothers [data-open="{PROFILE}"]')
    expect(theirs).to_be_visible()
    expect(theirs).to_have_attribute("data-owner", sams_work["owner"].id)
    expect(page.locator("#glossothers")).to_contain_text(SPEECH)

    page.locator('[role=tab][data-tab="speak"]').click()
    settled(page)
    page.locator("#allclipsbox > summary").click()
    clip = page.locator("#allclipslist tr", has_text=sams_work["clip"])
    expect(clip).to_contain_text(SPEECH)
    assert sams_work["clip"] not in page.locator("#voice option").all_text_contents(), \
        "the admin may delete another person's clip but never speak with it (D35)"


def test_a_satellites_run_is_in_no_persons_jobs_and_is_the_systems(new_page, goto, fake):
    """The hub's transcription is owned by svc:satellites (D31): on nobody's
    own list, the admin's included, and under The system's for the admin."""
    fake.reset()
    hub_run = next(j["id"] for j in fake.jobs()["jobs"] if j.get("owner") == "svc:satellites")
    for user in (SPEECH, "admin"):
        page = new_page(user=user)
        goto("/ui/jobs", target=page)
        settled(page)
        if user == "admin":
            # The admin's own five are listed; the hub's run is not among them.
            expect(page.locator("#joblist .job")).to_have_count(5)
        expect(page.locator(f'.job[data-job="{hub_run}"]')).to_have_count(0)
        expect(page.locator("#joblist")).not_to_contain_text(SATELLITE_TEXT)
    page.locator("#jobowner").select_option("system")
    row = page.locator(f'.job[data-job="{hub_run}"]')
    expect(row).to_be_visible()
    expect(row).to_contain_text("System (satellites)")
