"""What a role shows, in a browser: the user and user-jobs users beside the admin (§4.2, §4.3, D60).

Hiding a tab or a control is wayfinding and never the check: every route is
checked again at the gateway. So each test here says both halves where it
can, what the page draws and what the gateway answers.

The user role is user-jobs without jobs: no speech:long, no jobs scope and no
voices:write:own. What it must never meet is a control that needs one of
them, or a request the page sends for it on the reader's behalf: fast Speak
and fast Transcribe work, and nothing reaches /jobs, a long-form engine or
the clip store.
"""

from __future__ import annotations

import json

import pytest
from conftest import USER, USER_JOBS
from playwright.sync_api import expect
from test_routes import settled
from test_speak import SENTENCE, arrived
from test_transcribe import choose, extracted, stt_forms, tone, transcribe
from voice_common.scopes import (JOBS_ONLY, SERVICE_ONLY, SESSION_ONLY, presets_for,
                                 session_scopes)

# Each role's tabs on the bar: the sections it may use. Account is the person
# icon at the top right, for every role.
USER_JOBS_TABS = ["transcribe", "speak", "jobs", "vocab"]
USER_TABS = ["transcribe", "speak", "vocab"]
# What the page may never ask for on a user's behalf: the job routes, the
# Jobs tab's addresses and the clip store.
JOB_PATHS = r"^/(jobs|ui/jobs|ui/clips)(/|$)"
WRITE_REFUSED = 'Bearer error="insufficient_scope", scope="glossaries:write:all"'


def shown_tabs(page) -> list[str]:
    return page.locator("[role=tab]").evaluate_all(
        "els => els.filter(e => !e.hidden && e.checkVisibility()).map(e => e.dataset.tab)")


def test_a_user_jobs_user_sees_their_four_sections_and_their_account(user_jobs_page, goto, browser_log):
    """Transcribe, Speak, Jobs and Vocabulary on the bar and Account under the
    person icon; no Satellites and no Admin anywhere, and the hub is never
    asked for anything (§4.2)."""
    goto("/ui", target=user_jobs_page)
    settled(user_jobs_page)
    assert shown_tabs(user_jobs_page) == USER_JOBS_TABS
    expect(user_jobs_page.locator("#who-role")).to_have_text("user-jobs")
    user_jobs_page.locator("#who > summary").click()
    expect(user_jobs_page.locator("#who-account")).to_be_visible()
    expect(user_jobs_page.locator("#who-admin")).to_be_hidden()
    assert not browser_log.sent(path=r"^/satellites"), browser_log.sent(path=r"^/satellites")
    assert not browser_log.sent(path=r"^/admin/")


@pytest.mark.parametrize("address", ["/ui/satellites", "/ui/satellites/kitchen", "/ui/admin/users"])
def test_an_address_the_role_may_not_open_lands_on_the_page(user_jobs_page, goto, stack, address):
    """Typed or followed from a link, the gateway answers a tab the role
    lacks with a 303 to the page's own address (D55), which opens the first
    tab the person may see. The 303 is the gateway's, not the page putting
    a new address in the bar once it has loaded."""
    landed = goto(address, target=user_jobs_page)
    asked = landed.request.redirected_from
    assert asked is not None and asked.url == stack.url + address, "the gateway served the address"
    assert asked.response().status == 303
    assert landed.url == user_jobs_page.url == f"{stack.url}/ui"
    settled(user_jobs_page)
    expect(user_jobs_page.locator('[role=tab][data-tab="transcribe"]')).to_have_attribute("aria-selected", "true")


@pytest.mark.parametrize("path", ["/satellites", "/admin/users"])
def test_the_gateway_refuses_a_user_jobs_session_what_the_hidden_tabs_read(user_jobs_page, goto, path):
    """The other half: what the Satellites and Admin tabs read is refused to
    the user-jobs user's session, asked by their own page (D3, D55)."""
    goto("/ui", target=user_jobs_page)
    answered = user_jobs_page.evaluate("""async path => {
      const r = await fetch(path);
      return {status: r.status, code: (await r.json()).error.code};
    }""", path)
    assert answered == {"status": 403, "code": "insufficient_scope"}, answered


def test_a_refusal_for_a_missing_scope_is_said_and_switches_off_the_control_that_asked(
        user_jobs_page, goto, fake):
    """A 403 insufficient_scope, here stt refusing a save, is said in a toast
    naming the scope the challenge names, and the button that was pressed
    stays off for the rest of the session (§4.3)."""
    fake.fail(r"^/glossaries/mine$", method="PUT", backend="stt", status=403,
              json_body={"error": {"message": "This credential lacks glossaries:write:all.",
                                   "type": "invalid_request_error", "param": None,
                                   "code": "insufficient_scope"}},
              headers={"WWW-Authenticate": WRITE_REFUSED})
    goto("/ui/vocabulary", target=user_jobs_page)
    settled(user_jobs_page)
    user_jobs_page.locator("#glossnew").click()
    user_jobs_page.locator("#glossname").fill("mine")
    user_jobs_page.locator("#glosstext").fill("cloud code = Claude Code")
    save = user_jobs_page.locator("#glosssave")
    save.click()
    expect(user_jobs_page.locator("#toast")).to_have_text(
        "Your account cannot do this (needs glossaries:write:all).")
    expect(save).to_be_disabled()
    expect(save).to_have_attribute("title", "Needs glossaries:write:all")


@pytest.mark.parametrize("user,role", [("admin", "admin"), (USER_JOBS, "user-jobs"), (USER, "user")])
def test_the_new_key_form_offers_only_scopes_and_presets_a_key_may_hold(new_page, goto, user, role):
    """Never a session-only scope (D60) and never a service's, and nothing
    outside the person's role: a box the gateway would refuse is a box the
    form does not draw, and a preset it would refuse is not in the list."""
    page = new_page(user=user)
    goto("/ui/account", target=page)
    settled(page)
    page.locator("#keynew > summary").click()
    expect(page.locator("#key-scopes input[type=checkbox]").first).to_be_visible()
    offered = set(page.locator("#key-scopes input[type=checkbox]").evaluate_all(
        "els => els.map(e => e.value)"))
    grantable = session_scopes(role) - SESSION_ONLY
    assert offered, "the form offers no scope at all"
    assert not offered & SESSION_ONLY, sorted(offered & SESSION_ONLY)
    assert not offered & SERVICE_ONLY, sorted(offered & SERVICE_ONLY)
    assert offered == grantable, sorted(offered ^ grantable)
    presets = page.locator("#key-preset option").evaluate_all("els => els.map(e => e.value)")
    assert presets == ["", *presets_for(role)], presets
    if role == "user":
        assert presets == ["", "user", "transcribe-only"]
        assert not offered & JOBS_ONLY


def test_a_user_key_made_from_its_preset_holds_no_job_scope(new_page, goto, stack, people):
    """The preset ticks the boxes, Create sends them, and the key the gateway
    made can transcribe and cannot read a job."""
    page = new_page(user=USER)
    goto("/ui/account", target=page)
    settled(page)
    page.locator("#keynew > summary").click()
    page.locator("#key-preset").select_option("user")
    page.locator("#key-name").fill("e2e user preset")
    with page.expect_response(lambda r: r.url.endswith("/auth/keys") and r.request.method == "POST") as made:
        page.locator("#key-create").click()
    assert made.value.status == 201, made.value.text()
    sent = json.loads(made.value.request.post_data)
    assert sent["preset"] == "user" and not set(sent["scopes"]) & JOBS_ONLY, sent
    plaintext = made.value.json()["plaintext"]
    with stack.client(plaintext) as theirs:
        assert theirs.get("/v1/models").status_code == 200
        refused = theirs.get("/jobs")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "insufficient_scope"
    with stack.person(people(USER)) as http:
        mine = [k for k in http.get("/auth/keys").json()["keys"] if k["name"] == "e2e user preset"]
        for key in mine:
            http.delete(f"/auth/keys/{key['id']}")
    assert [k["preset"] for k in mine] == ["user"]


# ---- the user role: fast Transcribe and Speak, and no jobs ----------------------------------


def speak_ready(page) -> None:
    """Speak once the voices and /health have answered, so the picker is the
    one drawn with the engines known: what a user is not shown is decided."""
    settled(page)
    page.wait_for_function("""() => NAV.settled.has("voices") && engineIds().length > 0
      && [...document.getElementById("voice").options].length > 0""")


def no_job_asked(browser_log) -> None:
    """Nothing the page sent touched a job, the GPU lane or the clip store. A
    navigation the test typed is the test's, not the page's, and is left out."""
    asked = [r for r in browser_log.sent(path=JOB_PATHS) if r["type"] != "document"]
    assert not asked, asked
    long_form = [r for r in browser_log.sent("POST", r"^/v1/audio/speech$")
                 if (r.get("json") or {}).get("model") != "kokoro"]
    assert not long_form, long_form


def test_a_user_sees_three_sections_and_their_account_and_no_jobs(user_page, goto, browser_log):
    """Transcribe, Speak and Vocabulary on the bar, Account under the person
    icon with the role beside the name, and no Jobs tab anywhere."""
    goto("/ui", target=user_page)
    settled(user_page)
    assert shown_tabs(user_page) == USER_TABS
    expect(user_page.locator('[role=tab][data-tab="jobs"]')).to_be_hidden()
    expect(user_page.locator("#who-role")).to_have_text("user")
    user_page.locator("#who > summary").click()
    expect(user_page.locator("#who-account")).to_be_visible()
    expect(user_page.locator("#who-admin")).to_be_hidden()
    no_job_asked(browser_log)
    assert not browser_log.sent(path=r"^/satellites"), browser_log.sent(path=r"^/satellites")


@pytest.mark.parametrize("address", ["/ui/jobs", "/ui/jobs/0123456789abcdef", "/ui/jobs?show=failed"])
def test_a_jobs_address_lands_a_user_on_the_page(user_page, goto, stack, browser_log, address):
    """The gateway answers the Jobs tab's addresses with a 303 to /ui for a
    role without jobs:read:own, and the page opens Transcribe."""
    landed = goto(address, target=user_page)
    asked = landed.request.redirected_from
    assert asked is not None and asked.response().status == 303
    assert landed.url == f"{stack.url}/ui"
    settled(user_page)
    expect(user_page.locator('[role=tab][data-tab="transcribe"]')).to_have_attribute("aria-selected", "true")
    no_job_asked(browser_log)


@pytest.mark.parametrize("path", ["/jobs", "/ui/clips"])
def test_the_gateway_refuses_a_user_session_the_jobs_and_saving_a_clip(user_page, goto, path):
    """The other half: what the hidden controls would send is refused to the
    user's own session, asked by their own page."""
    goto("/ui", target=user_page)
    answered = user_page.evaluate("""async path => {
      const r = await fetch(path, path === "/jobs" ? {} : {method: "POST", body: new FormData()});
      return {status: r.status, code: (await r.json()).error.code};
    }""", path)
    assert answered == {"status": 403, "code": "insufficient_scope"}, answered


def test_speak_shows_a_user_kokoro_and_nothing_that_queues(user_page, goto, browser_log):
    """No Chatterbox, no long-form engine's voices, no Clone a new voice, no
    engine choice, no clone sheet, no Chatterbox panel and no language only a
    long-form engine speaks; the clone sheet's own address opens Speak."""
    goto("/ui/speak", target=user_page)
    speak_ready(user_page)
    values = user_page.locator("#voice option").evaluate_all("els => els.map(e => e.value)")
    assert values and all(v.startswith("k:") for v in values), [v for v in values if not v.startswith("k:")]
    groups = user_page.locator("#voice optgroup").evaluate_all("els => els.map(e => e.label)")
    assert groups and all(g.startswith("Kokoro") for g in groups), groups
    expect(user_page.locator("#voice")).to_have_value("k:bm_george")
    for hidden in ("#enginerow", "#clone", "#tts-expert-clone", "#delvoice", "#allclips"):
        expect(user_page.locator(hidden)).to_be_hidden()
    expect(user_page.locator("#tts-expert-fast")).to_be_visible()
    languages = user_page.locator("#lang option").evaluate_all("els => els.map(e => e.value)")
    kokoro = user_page.evaluate("() => [...new Set(KOKORO_LANGS.map(([c]) => isoStem(c)))].sort()")
    assert sorted(languages) == sorted(["auto", *kokoro]), languages
    assert "de" not in languages

    landed = goto("/ui/speak/clone", target=user_page)
    assert landed.status == 200
    speak_ready(user_page)
    user_page.wait_for_function("() => location.pathname === '/ui/speak'")
    expect(user_page.locator("#clone")).to_be_hidden()
    no_job_asked(browser_log)


def test_a_user_speaks_and_transcribes_fast_and_no_job_is_ever_asked_for(
        user_page, goto, fake, browser_log, tmp_path):
    """Generate & listen on Kokoro, then a file transcribed, as a user. What
    left the page is read from the browser's own log: no /jobs, no /ui/jobs,
    no clip, and no speech for any model but kokoro. Then the two things that
    would make the page look for jobs by itself, a queue that moved and the
    polling ladder's tick, are made to happen, and still nothing is asked."""
    goto("/ui/speak", target=user_page)
    speak_ready(user_page)
    user_page.locator("#text").fill(SENTENCE)
    mark = fake.last_seq()
    user_page.locator("#go-tts").click()
    expect(user_page.locator("#player")).to_be_visible(timeout=20_000)
    [spoken] = arrived(fake, mark, "tts", r"^/v1/audio/speech$")
    assert spoken["json"]["model"] == "kokoro" and spoken["json"]["voice"] == "bm_george"
    expect(user_page.locator("#speak-note")).not_to_contain_text("Jobs")

    user_page.locator('[role=tab][data-tab="transcribe"]').click()
    settled(user_page)
    since = fake.last_seq()
    choose(user_page, tone(tmp_path / "user-16k.wav", 4.0, 16000))
    extracted(user_page)
    answer = transcribe(user_page)
    assert answer.status == 200
    expect(user_page.locator("#result")).to_be_visible()
    assert stt_forms(fake, since), "nothing reached stt"

    user_page.evaluate("() => { jobsStale(); schedule(0); }")
    user_page.wait_for_timeout(1500)
    no_job_asked(browser_log)
    assert not fake.requests(backend="long", path=r"^/jobs", since=mark)


@pytest.mark.parametrize("path", ["/satellites/telemetry", "/satellites/routing",
                                  "/satellites/telemetry/summary"])
def test_a_key_that_may_read_the_satellites_cannot_read_their_settings(stack, api_key, path):
    """Home Assistant's key lists the satellites, but not their settings.
    /satellites/telemetry and /satellites/routing would also match
    /satellites/{nid}, which satellites:read opens: the gateway resolves the
    route as the hub's router does, so they need satellites:admin (recheck
    M-1). The telemetry summary matches no satellite's route, and is here as
    one more setting the key must not read."""
    with stack.client(api_key("home-assistant")) as ha:
        assert ha.get("/satellites").status_code == 200
        refused = ha.get(path)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "insufficient_scope"
    assert "satellites:admin" in refused.headers["WWW-Authenticate"]


def test_a_key_that_may_read_the_satellites_is_not_shown_their_buttons(stack, api_key):
    """A button's mapping may name a webhook's secret, so only
    satellites:admin is shown it (D62): the admin's key sees the adopted
    satellites' buttons, and Home Assistant's the same list without them."""
    with stack.client(api_key("home-assistant")) as ha:
        shown = {s["id"]: s.get("config") or {} for s in ha.get("/satellites").json()["satellites"]}
    full = {s["id"]: s.get("config") or {} for s in stack.api.get("/satellites").json()["satellites"]}
    assert any("buttons" in config for config in full.values()), full
    assert shown.keys() == full.keys()
    assert [nid for nid, config in shown.items() if "buttons" in config] == [], shown
