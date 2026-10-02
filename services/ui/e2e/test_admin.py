"""The Admin and Account tabs, in a browser, as the admin (§4.5, §4.6, D13, D25-D30, D40).

Everything shown once -- a temporary password, a new key, a secret's value --
is checked for in the markup, in what every field holds and in every answer
the page was given afterwards, and must be in none of them.

The shared admin session entered its password again when the session minted
the harness's key, so for its first ten minutes a step-up may already hold:
the tests that act through it answer the prompt if it comes. The one test
about the prompt itself signs in afresh, where it always comes.
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qs, urlsplit

import stack as st
from conftest import SIGN_IN_MS, password_if_asked
from playwright.sync_api import expect
from test_routes import settled

KEY = re.compile(r"^calliope_[0-9A-Za-z]{36}$")


# ---- helpers ---------------------------------------------------------------------------


def answers(page) -> list[dict]:
    """Every answer this page is given from now on, with its text."""
    seen: list[dict] = []

    def keep(response) -> None:
        try:
            text = response.text()
        except Exception:  # a redirect, or a stream that was cut: nothing to search
            text = ""
        seen.append({"url": response.url, "text": text})

    page.on("response", keep)
    return seen


def nowhere_on(page, seen: list[dict], value: str) -> None:
    """`value` is not in the markup, in any field, or in any answer kept."""
    assert value not in page.content(), "the value is in the markup"
    assert value not in page.locator("input, textarea").evaluate_all("els => els.map(e => e.value)"), \
        "a field still holds the value"
    assert [a["url"] for a in seen if value in a["text"]] == [], "an answer carried the value"


def sign_in(page, username: str, password: str) -> None:
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.locator("#signin-button").click()


# ---- users -----------------------------------------------------------------------------


def test_a_new_user_signs_in_with_the_temporary_password_and_must_choose_their_own(
        page, goto, new_page, stack):
    """The temporary password is shown once, to the admin; the person signs
    in with it and is asked for a password of their own and nothing else, and
    lands on the page (D25, recheck M-7)."""
    goto("/ui/admin/users")
    settled(page)
    page.locator("#user-name").fill("casey")
    page.locator("#user-role").select_option("speech")
    page.locator("#user-create").click()
    shown = page.locator("#userreset .secretonce")
    password_if_asked(page, stack.admin.password, shown)
    temporary = shown.inner_text()
    expect(page.locator("#users")).to_contain_text("casey")
    page.locator("#userreset").get_by_role("button", name="Done").click()
    assert temporary not in page.content(), "Done left the temporary password in the page"

    theirs = new_page(user=None)
    goto("/ui", target=theirs)
    sign_in(theirs, "casey", temporary)
    expect(theirs.locator("#change")).to_be_visible(timeout=SIGN_IN_MS)
    assert theirs.locator("#change input").evaluate_all(
        "els => els.filter(e => e.checkVisibility()).map(e => e.id)") == ["new-password", "confirm-password"]
    # Until then the session can do nothing else: a key made on a password
    # the admin was shown would outlive the change (recheck M-7).
    assert theirs.evaluate("""async () => (await fetch("/auth/keys", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: "early", preset: "read-only", expires_days: 30})})).status""") == 401
    chosen = st.new_password()
    theirs.locator("#new-password").fill(chosen)
    theirs.locator("#confirm-password").fill(chosen)
    theirs.locator("#change-button").click()
    theirs.wait_for_url(f"{stack.url}/ui", timeout=SIGN_IN_MS)
    expect(theirs.locator("#who-name")).to_have_text("casey")


def test_the_password_is_asked_again_before_a_user_is_created(new_page, goto, stack, browser_log):
    """A session that has not entered its password in the last ten minutes
    is asked for it, and the same request is sent again once it has (D13)."""
    page = new_page(user=None)
    goto("/ui/admin/users", target=page)
    sign_in(page, "admin", stack.admin.password)
    page.wait_for_url(f"{stack.url}/ui/admin/users", timeout=SIGN_IN_MS)
    settled(page)
    page.locator("#user-name").fill("jordan")
    page.locator("#user-create").click()
    expect(page.locator("#stepup")).to_be_visible()
    expect(page.locator("#stepup-password")).to_be_focused()
    page.locator("#stepup-password").fill(stack.admin.password)
    page.locator("#stepup-ok").click()
    expect(page.locator("#userreset .secretonce")).to_be_visible(timeout=SIGN_IN_MS)
    asked = [r["path"] for r in browser_log.requests if r["method"] == "POST"]
    assert asked == ["/auth/login", "/admin/users", "/auth/step-up", "/admin/users"], asked


# ---- keys ------------------------------------------------------------------------------


def test_a_new_key_is_shown_once_and_never_again(page, goto, stack):
    """Created on Account, the key is shown with Copy and works at once;
    after a reload the list shows its short form and no answer carries it
    (D27)."""
    goto("/ui/account")
    settled(page)
    page.locator("#keynew > summary").click()
    page.locator("#key-name").fill("shown once")
    page.locator("#key-preset").select_option("read-only")
    page.locator("#key-create").click()
    shown = page.locator("#keyshown .secretonce")
    expect(shown).to_be_visible()
    plaintext = shown.inner_text()
    assert KEY.match(plaintext), plaintext
    with stack.client(plaintext) as program:
        assert program.get("/v1/models").status_code == 200

    seen = answers(page)
    page.reload()
    settled(page)
    row = page.locator("#keys tr", has_text="shown once")
    expect(row).to_contain_text(f"{plaintext[:13]}…{plaintext[-4:]}")
    nowhere_on(page, seen, plaintext)


def test_a_revoked_key_is_refused_from_its_next_request(page, goto, stack, dialogs):
    key = stack.mint_key(stack.admin, "read-only", name="revoke me")
    with stack.client(key) as program:
        assert program.get("/v1/models").status_code == 200
        goto("/ui/account")
        settled(page)
        dialogs()
        page.locator("#keys tr", has_text="revoke me").get_by_role("button", name="Revoke").click()
        expect(page.locator("#keysnote")).to_have_text('"revoke me" is revoked.')
        refused = program.get("/v1/models")
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "invalid_api_key"


# ---- secrets ---------------------------------------------------------------------------


def test_a_stored_secret_is_never_shown_again(page, goto, stack):
    """Stored from Admin › Secrets, the value goes into the store and never
    comes back: not in the list, not in a field, not in any answer, before
    or after a reload (D40)."""
    value = "e2e-" + secrets.token_urlsafe(24)
    goto("/ui/admin/secrets")
    settled(page)
    seen = answers(page)
    page.locator("#secretnew > summary").click()
    page.locator("#secret-name").fill("E2E_SHOWN_ONCE")
    page.locator("#secret-kind").select_option("bearer")
    page.locator("#secret-hosts").fill("https://ha.example:8123")
    page.locator("#secret-value").fill(value)
    page.locator("#secret-new").click()
    password_if_asked(page, stack.admin.password, page.locator('#secrets tr[data-secret="E2E_SHOWN_ONCE"]'))
    expect(page.locator("#secretsnote")).to_have_text("E2E_SHOWN_ONCE is stored.")
    nowhere_on(page, seen, value)
    page.reload()
    settled(page)
    expect(page.locator('#secrets tr[data-secret="E2E_SHOWN_ONCE"]')).to_contain_text("https://ha.example:8123")
    nowhere_on(page, seen, value)


# ---- audit -----------------------------------------------------------------------------


def test_the_audit_lists_what_the_admin_did(page, goto, stack):
    """A key the admin just made is the newest row, theirs; and the session's
    first sign-in, which consumed the bootstrap, is on record (§2.1)."""
    stack.mint_key(stack.admin, "read-only", name="audited")
    goto("/ui/admin/audit")
    settled(page)
    # The page says "you" for the reader's own rows.
    newest = page.locator("#audit tbody tr").first.locator("td")
    expect(newest.nth(1)).to_have_text("you")
    expect(newest.nth(4)).to_have_text("key_created")
    page.locator("#audit-action").fill("bootstrap_consumed")
    page.locator("#audit-apply").click()
    expect(page.locator("#audit tbody tr")).to_have_count(1)
    expect(page.locator("#audit tbody tr td").nth(1)).to_have_text("you")


def test_a_look_at_everyones_jobs_is_on_record_as_a_read_that_was_allowed(page, goto):
    """Reading other people's work is a security event of its own: one row
    for the request, the admin's, naming the owner filter, with the outcome
    ok, and listed without the per-minute counts of refusals (§2.1)."""
    goto("/ui/jobs")
    settled(page)
    with page.expect_response(lambda r: urlsplit(r.url).path == "/jobs"
                              and parse_qs(urlsplit(r.url).query).get("owner") == ["all"]) as listed:
        page.locator("#jobowner").select_option("all")
    assert listed.value.ok
    goto("/ui/admin/audit")
    settled(page)
    page.locator("#audit-action").fill("read_all")
    page.locator("#audit-apply").click()
    newest = page.locator("#audit tbody tr").first.locator("td")
    expect(newest.nth(1)).to_have_text("you")
    expect(newest.nth(4)).to_have_text("read_all")
    expect(newest.nth(5)).to_have_text("all")
    expect(newest.nth(6)).to_have_text("ok")
