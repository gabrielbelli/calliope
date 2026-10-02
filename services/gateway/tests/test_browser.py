"""What a browser may do with the session cookie, and from where (D14, D15, D16, D61).

The cookie rides every request a browser makes to this host name, whichever
page asked for it: a sibling service on the same NAS, a link in an email, an
<img> on a forum. These tests are those requests, header for header, and each
asserts that the cookie bought them nothing:

* H2: login CSRF and session fixation;
* H3: a same-site sibling opening a microphone or embedding a recording;
* the Fetch Metadata matrix itself, with the Origin fallback and the Bearer skip.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import (PASSWORD, PUBLIC_ORIGIN, SAME_ORIGIN, MockBackend, bearer, gateway,
                      make_key, make_user, sign_in)

COOKIE = "__Host-calliope_session"


def fetch(site: str | None, *, mode: str = "cors", dest: str = "empty") -> dict[str, str]:
    """The Fetch Metadata a browser attaches; site None means a client that sends none."""
    if site is None:
        return {}
    return {"sec-fetch-site": site, "sec-fetch-mode": mode, "sec-fetch-dest": dest}


def navigation(site: str) -> dict[str, str]:
    return fetch(site, mode="navigate", dest="document")


async def signed_in(client: httpx.AsyncClient, role: str = "admin") -> None:
    make_user("ana", role=role)
    response = await sign_in(client)
    assert response.status_code == 200


# ── H2: login CSRF and fixation ───────────────────────────────────────────────


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_a_login_from_another_site_is_refused(monkeypatch, site):
    """A cross-site form could sign the victim in as the attacker, and their
    transcripts would land in the attacker's account. same-site gets no
    exception: the siblings on the NAS are same-site (D14)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        response = await sign_in(client, **fetch(site))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf"
    assert COOKIE not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize("origin,status", [("https://evil.example", 403),
                                           (PUBLIC_ORIGIN, 200)])
async def test_a_login_without_fetch_metadata_needs_the_public_origin(monkeypatch, origin,
                                                                       status):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        response = await client.post("/auth/login", headers={"origin": origin},
                                     json={"username": "ana", "password": PASSWORD})
    assert response.status_code == status


@pytest.mark.parametrize("content_type,body", [
    ("application/x-www-form-urlencoded", b"username=ana&password=x"),
    ("multipart/form-data; boundary=x", b"--x\r\n\r\n--x--"),
    ("text/plain", b'{"username":"ana","password":"x"}'),
])
async def test_a_login_an_html_form_could_send_is_415(monkeypatch, content_type, body):
    """JSON only: these three are the bodies a form can submit cross-site
    without a preflight (D61)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.post("/auth/login", content=body,
                                     headers={**SAME_ORIGIN, "content-type": content_type})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "json_required"


async def test_a_login_never_upgrades_the_session_id_it_arrived_with(monkeypatch):
    """A sibling can plant a cookie for this host name; signing in must issue
    a fresh ID and the planted one must not authenticate afterwards (D61)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        await sign_in(client)
        planted = client.cookies.get(COOKIE)
        again = await sign_in(client)
        fresh = client.cookies.get(COOKIE)
        replay = await client.get("/auth/me",
                                  headers={**SAME_ORIGIN, "cookie": f"{COOKIE}={planted}"})

    assert again.status_code == 200
    assert fresh and fresh != planted
    assert replay.status_code == 401


async def test_a_login_on_another_host_name_is_refused(monkeypatch):
    """Cookies are not isolated by port, so a session is made only on the host
    name that serves nothing but Calliope (D16, D61). NAS:30080 is another."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        other = httpx.AsyncClient(transport=client._transport, base_url="https://nas.lan:30080")
        response = await sign_in(other)
        page = await other.get("/login", headers=navigation("none"))
        await other.aclose()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "wrong_host"
    assert page.status_code == 303
    assert page.headers["location"] == f"{PUBLIC_ORIGIN}/login"


async def test_a_session_cookie_presented_on_another_host_name_is_absent(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client)
        cookie = client.cookies.get(COOKIE)
        other = httpx.AsyncClient(transport=client._transport, base_url="https://nas.lan:30080")
        response = await other.get("/auth/me",
                                   headers={**SAME_ORIGIN, "cookie": f"{COOKIE}={cookie}"})
        await other.aclose()

    assert response.status_code == 401


async def test_a_cookie_login_over_plain_http_is_refused(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("ana")
        plain = httpx.AsyncClient(transport=client._transport, base_url="http://gateway.test")
        response = await sign_in(plain)
        await plain.aclose()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "wrong_host"


async def test_a_logout_from_another_site_is_refused(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client)
        forged = await client.post("/auth/logout", headers=fetch("cross-site"))
        still = await client.get("/auth/me", headers=SAME_ORIGIN)

    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "csrf"
    assert still.status_code == 200


async def test_no_response_carries_a_cors_header(monkeypatch):
    """The Bearer skip of the CSRF check depends on no browser being allowed
    to send a Bearer header cross-site (D14): no Access-Control-* anywhere,
    preflights included."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        key = make_key(make_user("ana"))
        evil = {"origin": "https://evil.example"}
        responses = [
            await client.options("/v1/models", headers={
                **evil, "access-control-request-method": "GET",
                "access-control-request-headers": "authorization"}),
            await client.get("/v1/models", headers={**evil, **bearer(key)}),
            await client.get("/health", headers=evil),
            await client.post("/auth/login", headers={**evil, **SAME_ORIGIN},
                              json={"username": "ana", "password": PASSWORD}),
            await client.get("/nope", headers={**evil, **bearer(key)}),
        ]

    assert responses[0].status_code == 405
    for response in responses:
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]


# ── H3: same-site siblings ────────────────────────────────────────────────────


@pytest.mark.parametrize("site", ["same-site", "cross-site", "none"])
async def test_a_cookie_cannot_open_a_microphone_from_anywhere_but_the_page(
        monkeypatch, site):
    """A sibling page on the NAS (MeTube with an attacker's title) is
    same-site, and SameSite=Lax lets the cookie ride. Only the page itself may
    open a microphone (D15, H3)."""
    hub = MockBackend("voice-satellites")
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, _):
        await signed_in(client)
        refused = await client.post("/satellites/020000000001/listen?seconds=5",
                                    headers=fetch(site))
        allowed = await client.post("/satellites/020000000001/listen?seconds=5",
                                    headers=SAME_ORIGIN)

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "csrf"
    assert allowed.status_code == 200
    assert [(r["method"], r["path"]) for r in hub.seen] == [
        ("POST", "/satellites/020000000001/listen")]


async def test_listen_is_a_post_and_a_get_is_405(monkeypatch):
    hub = MockBackend("voice-satellites")
    async with gateway(monkeypatch, satellites=hub) as (client, _):
        response = await client.get("/satellites/020000000001/listen")

    assert response.status_code == 405
    assert not hub.seen


async def test_a_sibling_cannot_embed_a_recording_with_the_cookie(monkeypatch):
    """<audio src="https://calliope.example/jobs/x/audio"> on a same-site page."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client)
        response = await client.get("/jobs/j1/audio",
                                    headers=fetch("same-site", mode="no-cors", dest="audio"))
        own = await client.get("/jobs/j1/audio",
                               headers=fetch("same-origin", mode="no-cors", dest="audio"))

    assert response.status_code == 403
    assert own.status_code == 200


@pytest.mark.parametrize("site", ["same-site", "cross-site", "none"])
async def test_a_link_to_a_tab_opens_from_anywhere_but_a_link_to_data_does_not(
        monkeypatch, site):
    """A top-level navigation to the page shell is harmless and useful (a
    bookmark, a link in a chat); one to /jobs would hand the data to a link."""
    ui = MockBackend("voice-ui")
    async with gateway(monkeypatch, ui=ui, authenticate=False) as (client, _):
        await signed_in(client)
        tab = await client.get("/ui/jobs", headers=navigation(site))
        data = await client.get("/jobs", headers=navigation(site))

    assert tab.status_code == 200 and ui.last["path"] == "/ui/jobs"
    assert data.status_code == 403


async def test_a_cookie_request_without_fetch_metadata_is_refused_unless_it_is_a_page(
        monkeypatch):
    """Every supported browser sends Sec-Fetch-Site; a request without it is
    curl or something older, and gets the page shell at most."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client)
        data = await client.get("/jobs")
        shell = await client.get("/ui/jobs")

    assert data.status_code == 403
    assert shell.status_code == 200


async def test_a_bearer_request_skips_the_browser_checks(monkeypatch):
    """A key is never sent by a browser on its own, so there is nothing to forge."""
    hub = MockBackend("voice-satellites")
    async with gateway(monkeypatch, satellites=hub, authenticate=False) as (client, _):
        key = make_key(make_user("ana"))
        response = await client.post("/satellites/020000000001/listen",
                                     headers={**bearer(key), **fetch("cross-site")})
    assert response.status_code == 200


@pytest.mark.parametrize("site,mode,status", [
    ("same-origin", "cors", 200),
    ("same-site", "cors", 403),
    ("cross-site", "cors", 403),
    ("none", "cors", 403),
    ("none", "navigate", 403),    # an unsafe navigation is still not same-origin
    (None, None, 403),            # no Fetch Metadata and no Origin
])
async def test_the_fetch_metadata_matrix_for_an_unsafe_cookie_request(
        monkeypatch, site, mode, status):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client)
        headers = {} if site is None else {"sec-fetch-site": site, "sec-fetch-mode": mode,
                                           "sec-fetch-dest": "empty"}
        response = await client.post("/v1/audio/speech", headers=headers,
                                     json={"input": "Hello."})
    assert response.status_code == status


# ── navigation ────────────────────────────────────────────────────────────────


async def test_a_page_navigation_without_a_session_goes_to_login_with_next(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.get("/ui/jobs/abc", headers=navigation("none"))
        plain = await client.get("/ui/jobs")
        api = await client.get("/v1/models")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/ui/jobs/abc"
    assert plain.status_code == 303, "curl to a tab is a navigation (§5.3 Verify)"
    assert api.status_code == 401


async def test_a_tab_the_role_lacks_lands_on_ui(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await signed_in(client, role="speech")
        satellites = await client.get("/ui/satellites", headers=navigation("same-origin"))
        jobs = await client.get("/ui/jobs", headers=navigation("same-origin"))

    assert satellites.status_code == 303 and satellites.headers["location"] == "/ui"
    assert jobs.status_code == 200


async def test_the_root_sends_a_person_to_the_page_or_to_login(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        before = await client.get("/", headers=navigation("none"))
        await signed_in(client)
        after = await client.get("/", headers=navigation("none"))

    assert before.headers["location"] == "/login"
    assert after.headers["location"] == "/ui"


async def test_the_login_page_carries_a_strict_policy_and_no_external_asset(monkeypatch):
    """Self-contained (recheck L8), scripts allowed by hash only (§4.7)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.get("/login", headers=navigation("none"))

    policy = response.headers["content-security-policy"]
    assert response.status_code == 200
    assert "script-src 'sha256-" in policy and "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy
    assert " src=" not in response.text and "<link" not in response.text
    assert "onerror=" not in response.text and "onclick=" not in response.text
