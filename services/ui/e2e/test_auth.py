"""Signing in and out, in a browser, against the session's own gateway (D11-D24, D55, D61).

The admin's first sign-in happens once per gateway, so the session makes it
before any test runs (conftest.E2ESession.first_sign_in) and keeps what the
browser was shown and sent; the first test here reads that record. Every other
test that signs in does so in a page of its own, as USER_JOBS, and never touches
the session the rest of the suite shares: signing out ends only the session it
is pressed in.

The gateway allows twenty sign-in attempts per address per ten minutes (D19)
and every test comes from 127.0.0.1, so each test here signs in at most twice.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import stack as st
from conftest import SIGN_IN_MS, USER_JOBS
from playwright.sync_api import expect

INCORRECT = "Incorrect username or password."


# ---- helpers ---------------------------------------------------------------------------


def sign_in(page, person: st.Account) -> None:
    """The sign-in form, filled and sent, on a page already at /login."""
    page.locator("#username").fill(person.username)
    page.locator("#password").fill(person.password)
    page.locator("#signin-button").click()


def current_session(page) -> str:
    """The reference of the session this page is signed in with, as Account lists it."""
    return page.evaluate("""async () => {
      const listing = await (await fetch("/auth/sessions")).json();
      return listing.sessions.find(s => s.current).id;
    }""")


def at_sign_in(page, stack, back_to: str) -> None:
    """The page is /login, and it will send the reader back to `back_to`."""
    page.wait_for_url(lambda url: urlsplit(url).path == "/login")
    address = urlsplit(page.url)
    assert f"{address.scheme}://{address.netloc}" == stack.url, page.url
    assert parse_qs(address.query).get("next") == [back_to], page.url
    expect(page.locator("#signin")).to_be_visible()


def cookie(page, stack) -> str | None:
    found = [c["value"] for c in page.context.cookies(stack.url) if c["name"] == st.COOKIE]
    return found[0] if found else None


# ---- the first sign-in -----------------------------------------------------------------


def test_the_first_admin_signs_in_with_the_bootstrap_value_and_must_choose_their_own(first_sign_in):
    """Signed in with CALLIOPE_ADMIN_PASSWORD, the admin is asked for a new
    password and nothing else: no current-password field, since the person
    choosing may never have been told the value (D21). The value itself is
    in no answer the gateway gave and in no markup or field the browser
    held, at the forced change or once signed in (D22, M5)."""
    bootstrap = first_sign_in["bootstrap"]
    fields = first_sign_in["change_fields"]
    assert [f["id"] for f in fields] == ["new-password", "confirm-password"], fields
    assert all(f["autocomplete"] == "new-password" for f in fields), fields

    answers = first_sign_in["responses"]
    signed = [a for a in answers if a["url"].endswith("/auth/login")]
    assert signed and signed[0]["status"] == 200 and '"must_change":true' in signed[0]["text"], signed
    changed = [a for a in answers if a["url"].endswith("/auth/password")]
    assert changed and changed[0]["status"] == 200, changed
    assert [a["url"] for a in answers if bootstrap in a["text"]] == []

    steps = [shown["step"] for shown in first_sign_in["shown"]]
    assert steps == ["change", "signed in"], steps
    for shown in first_sign_in["shown"]:
        assert bootstrap not in shown["html"], f"the value is in the markup at {shown['step']}"
        assert bootstrap not in shown["values"], f"a field holds the value at {shown['step']}"
    assert first_sign_in["shown"][-1]["url"].endswith("/ui")


# ---- signing in and out ----------------------------------------------------------------


def test_a_wrong_password_is_told_in_the_one_sentence_every_failure_gets(new_page, goto, stack, people):
    """The sentence every failed sign-in gets, a person's with the wrong
    password and an account that does not exist alike, so it never says
    which accounts there are (D19); and no session either way."""
    page = new_page(user=None)
    goto("/login", target=page)
    person = people(USER_JOBS)
    answers = []
    for username in (person.username, "nobody"):
        with page.expect_response(f"{stack.url}/auth/login", timeout=SIGN_IN_MS) as answered:
            sign_in(page, st.Account(username, "user-jobs", "not " + person.password))
        answers.append((answered.value.status, answered.value.json()))
        expect(page.locator("#signin-error")).to_have_text(INCORRECT)
        expect(page.locator("#password")).to_have_value("")
        expect(page.locator("#change")).to_be_hidden()
        assert cookie(page, stack) is None
    assert answers[0][0] == 401, answers
    assert answers[1] == answers[0], "an unknown account was answered differently"


def test_a_tab_opened_without_a_session_goes_to_sign_in_with_the_way_back(new_page, goto, stack):
    page = new_page(user=None)
    goto("/ui/vocabulary", target=page)
    at_sign_in(page, stack, "/ui/vocabulary")
    expect(page.locator("#username")).to_be_focused()


def test_signing_out_ends_that_session_and_no_other(new_page, goto, stack, people):
    """Sign out revokes the session it is pressed in: its cookie is gone,
    and the value it held authenticates nothing afterwards (D11). The
    person's other session, which the rest of the suite is using, stays."""
    person = people(USER_JOBS)
    page = new_page(user=None)
    goto("/ui/jobs", target=page)
    sign_in(page, person)
    page.wait_for_url(f"{stack.url}/ui/jobs", timeout=SIGN_IN_MS)
    held = cookie(page, stack)
    assert held

    page.locator("#who > summary").click()
    page.locator("#who-signout").click()
    page.wait_for_url(lambda url: urlsplit(url).path == "/login")
    assert cookie(page, stack) is None
    goto("/ui/jobs", target=page)
    at_sign_in(page, stack, "/ui/jobs")

    with httpx.Client(base_url=stack.url, cookies={st.COOKIE: held}, headers=st.SAME_ORIGIN) as http:
        assert http.get("/auth/me").status_code == 401
    with stack.person(person) as http:
        assert http.get("/auth/me").status_code == 200


def test_a_session_that_ends_while_the_page_is_open_signs_in_again_and_comes_back(
        new_page, goto, stack, people):
    """Ended elsewhere -- here by the person's own Account tab on another
    device, which signs out one session -- the page finds out at its next
    request, sends the reader to sign in, and after it lands where they were
    (§4.3, D55)."""
    person = people(USER_JOBS)
    page = new_page(user=None)
    goto("/ui/jobs", target=page)
    sign_in(page, person)
    page.wait_for_url(f"{stack.url}/ui/jobs", timeout=SIGN_IN_MS)
    expect(page.locator('[role=tab][data-tab="jobs"]')).to_have_attribute("aria-selected", "true")
    ended = current_session(page)
    with stack.person(person) as http:
        assert http.delete(f"/auth/sessions/{ended}").status_code == 204

    page.locator("#jobfilter").select_option("failed")
    at_sign_in(page, stack, "/ui/jobs?show=failed")
    sign_in(page, person)
    page.wait_for_url(f"{stack.url}/ui/jobs?show=failed", timeout=SIGN_IN_MS)
    expect(page.locator('[role=tab][data-tab="jobs"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#jobfilter")).to_have_value("failed")


# ---- another site's form ---------------------------------------------------------------


@pytest.mark.parametrize("site", ["127.0.0.1", "localhost"], ids=["a sibling on the same host", "another site"])
def test_another_sites_form_cannot_sign_the_browser_in(new_page, stack, fake, people, site):
    """A page elsewhere that posts a form to /auth/login with an account it
    controls must not sign the visitor in to that account, or what they then
    transcribe and save would be the attacker's to read (login CSRF, D14,
    D61). A sibling on the same host name is a different origin of the same
    site, which is why same-site earns no exception."""
    attacker = people(USER_JOBS)
    page = new_page(user=None)
    with page.expect_response(f"{stack.url}/auth/login") as answered:
        page.goto(fake.elsewhere(f"{stack.url}/auth/login", attacker.username, attacker.password,
                                 host=site))
    refusal = answered.value
    assert refusal.status == 403 and refusal.json()["error"]["code"] == "csrf", refusal.text()
    assert cookie(page, stack) is None
    page.goto(f"{stack.url}/ui")
    at_sign_in(page, stack, "/ui")
