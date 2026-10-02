"""What a role shows, in a browser: the speech user beside the admin (§4.2, §4.3, D60).

Hiding a tab or a control is wayfinding and never the check: every route is
checked again at the gateway. So each test here says both halves where it
can, what the page draws and what the gateway answers.
"""

from __future__ import annotations

import pytest
from conftest import SPEECH
from playwright.sync_api import expect
from test_routes import settled
from voice_common.scopes import SERVICE_ONLY, SESSION_ONLY, session_scopes

# The speech role's tabs: the four sections it may use, then its Account.
SPEECH_TABS = ["transcribe", "speak", "jobs", "vocab", "account"]
WRITE_REFUSED = 'Bearer error="insufficient_scope", scope="glossaries:write:all"'


def shown_tabs(page) -> list[str]:
    return page.locator("[role=tab]").evaluate_all(
        "els => els.filter(e => !e.hidden && e.checkVisibility()).map(e => e.dataset.tab)")


def test_a_speech_user_sees_their_four_sections_and_their_account(speech_page, goto, browser_log):
    """Transcribe, Speak, Jobs, Vocabulary and Account; no Satellites and no
    Admin, and the hub is never asked for anything (§4.2)."""
    goto("/ui", target=speech_page)
    settled(speech_page)
    assert shown_tabs(speech_page) == SPEECH_TABS
    expect(speech_page.locator("#who-role")).to_have_text("speech")
    assert not browser_log.sent(path=r"^/satellites"), browser_log.sent(path=r"^/satellites")
    assert not browser_log.sent(path=r"^/admin/")


@pytest.mark.parametrize("address", ["/ui/satellites", "/ui/satellites/kitchen", "/ui/admin/users"])
def test_an_address_the_role_may_not_open_lands_on_the_page(speech_page, goto, stack, address):
    """Typed or followed from a link, the gateway answers a tab the role
    lacks with a 303 to the page's own address (D55), which opens the first
    tab the person may see. The 303 is the gateway's, not the page putting
    a new address in the bar once it has loaded."""
    landed = goto(address, target=speech_page)
    asked = landed.request.redirected_from
    assert asked is not None and asked.url == stack.url + address, "the gateway served the address"
    assert asked.response().status == 303
    assert landed.url == speech_page.url == f"{stack.url}/ui"
    settled(speech_page)
    expect(speech_page.locator('[role=tab][data-tab="transcribe"]')).to_have_attribute("aria-selected", "true")


@pytest.mark.parametrize("path", ["/satellites", "/admin/users"])
def test_the_gateway_refuses_a_speech_session_what_the_hidden_tabs_read(speech_page, goto, path):
    """The other half: what the Satellites and Admin tabs read is refused to
    the speech user's session, asked by their own page (D3, D55)."""
    goto("/ui", target=speech_page)
    answered = speech_page.evaluate("""async path => {
      const r = await fetch(path);
      return {status: r.status, code: (await r.json()).error.code};
    }""", path)
    assert answered == {"status": 403, "code": "insufficient_scope"}, answered


def test_a_refusal_for_a_missing_scope_is_said_and_switches_off_the_control_that_asked(
        speech_page, goto, fake):
    """A 403 insufficient_scope, here stt refusing a save, is said in a toast
    naming the scope the challenge names, and the button that was pressed
    stays off for the rest of the session (§4.3)."""
    fake.fail(r"^/glossaries/mine$", method="PUT", backend="stt", status=403,
              json_body={"error": {"message": "This credential lacks glossaries:write:all.",
                                   "type": "invalid_request_error", "param": None,
                                   "code": "insufficient_scope"}},
              headers={"WWW-Authenticate": WRITE_REFUSED})
    goto("/ui/vocabulary", target=speech_page)
    settled(speech_page)
    speech_page.locator("#glossnew").click()
    speech_page.locator("#glossname").fill("mine")
    speech_page.locator("#glosstext").fill("cloud code = Claude Code")
    save = speech_page.locator("#glosssave")
    save.click()
    expect(speech_page.locator("#toast")).to_have_text(
        "Your account cannot do this (needs glossaries:write:all).")
    expect(save).to_be_disabled()
    expect(save).to_have_attribute("title", "Needs glossaries:write:all")


@pytest.mark.parametrize("user", ["admin", SPEECH])
def test_the_new_key_form_offers_only_scopes_a_key_may_hold(new_page, goto, user):
    """Never a session-only scope (D60) and never a service's, and nothing
    outside the person's role: a box the gateway would refuse is a box the
    form does not draw."""
    page = new_page(user=user)
    goto("/ui/account", target=page)
    settled(page)
    page.locator("#keynew > summary").click()
    expect(page.locator("#key-scopes input[type=checkbox]").first).to_be_visible()
    offered = set(page.locator("#key-scopes input[type=checkbox]").evaluate_all(
        "els => els.map(e => e.value)"))
    grantable = session_scopes("admin" if user == "admin" else "speech") - SESSION_ONLY
    assert offered, "the form offers no scope at all"
    assert not offered & SESSION_ONLY, sorted(offered & SESSION_ONLY)
    assert not offered & SERVICE_ONLY, sorted(offered & SERVICE_ONLY)
    assert offered == grantable, sorted(offered ^ grantable)


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
