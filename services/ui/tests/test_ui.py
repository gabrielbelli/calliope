"""The page, the assertion every route needs, and what this service no longer does.

It used to forward the page's calls to the gateway with a key of its own, so
whoever could reach it acted as one shared credential. The page now calls the
gateway itself with its session cookie, and this service answers only its own
routes, each behind the gateway's signed assertion. The tests below pin both
halves: what it serves, and what it refuses or no longer has.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time

import pytest
from voice_common.conformance import assert_four_field_envelope
from voice_common.identity import ASSERTION_HEADER, DELEGATION_HEADER

from conftest import ALICE, USER_JOBS

# Every route this service answers, as the gateway's UI_PATHS forwards them
# (§3.4), with one concrete path each. /health is the one exception and is
# tested on its own.
ROUTES = [
    ("GET", "/ui"),
    *[("GET", f"/ui/{tab}{tail}")
      for tab in ("transcribe", "speak", "jobs", "vocabulary", "satellites",
                  "account", "admin")
      for tail in ("", "/deep/link")],
    ("GET", "/ui/config"),
    ("GET", "/ui/clips"),
    ("POST", "/ui/clips"),
    ("DELETE", "/ui/clips/someone"),
    ("POST", "/ui/clips/from-link"),
    ("POST", "/ui/resolve"),
    ("POST", "/ui/commit"),
    ("POST", "/ui/abandon"),
    ("GET", "/ui/progress?token=https://media.example/x"),
    ("POST", "/ui/fetch"),
    ("POST", "/ui/captions"),
    ("GET", "/ui/media?token=https://media.example/x"),
]


def _send(api, method, path, headers):
    if method == "POST":
        return api.post(path, json={}, headers=headers)
    return api.request(method, path, headers=headers)


# ------------------------------------------------- the assertion, everywhere --


def test_the_route_list_here_is_every_route_the_app_answers(client):
    """So the refusal tests below cannot silently miss a route added later."""
    api, _, _ = client()
    from app import main

    served = {(method, re.sub(r"\{[^}]*\}", "x", route.path))
              for route in main.app.routes for method in getattr(route, "methods", ())
              if method != "HEAD" and route.path != "/health"}
    listed = {(method, re.sub(r"/deep/link$", "/x", path.split("?")[0])
               .replace("/ui/clips/someone", "/ui/clips/x"))
              for method, path in ROUTES}
    assert served == listed, (served ^ listed)


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_refuses_a_request_with_no_assertion(client, method, path):
    """The gateway is the only door, and this is what makes that true (D52)."""
    api, gateway, tube = client()
    response = _send(api, method, path, headers={ASSERTION_HEADER: "",
                                                 DELEGATION_HEADER: ""})
    assert response.status_code == 401, response.text
    assert_four_field_envelope(response)
    assert not tube.requests and not gateway.seen, "a refused request reached a backend"


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_refuses_an_assertion_meant_for_another_service(client, sign,
                                                                    calliope_gateway,
                                                                    method, path):
    api, _, _ = client()
    for forged in (calliope_gateway.assertion("tts", sub=ALICE, scopes=USER_JOBS),
                   calliope_gateway.assertion("ui", sub=ALICE, scopes=USER_JOBS,
                                              now=time.time() - 300),
                   "v1.1.e30.e30"):
        response = _send(api, method, path, headers={**sign(), ASSERTION_HEADER: forged})
        assert response.status_code == 401, (forged[:12], response.text)


def test_health_needs_no_assertion_and_asks_nobody_anything(client):
    """The container healthcheck has no assertion and no way to get one.

    It also no longer calls the gateway: the page reads the gateway's /health
    itself now, and a probe that depended on the gateway would report this
    container unhealthy whenever a backend restarted.
    """
    api, gateway, tube = client()
    response = api.get("/health", headers={ASSERTION_HEADER: ""})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["ui"] == "ok"
    assert body["features"] == {"ingestion": True, "probe": False, "cloning": False}
    assert not gateway.seen and not tube.requests


def test_health_says_not_ready_until_the_gateway_has_written_the_credentials(
        client, calliope_gateway):
    """Every request but /health would be refused, so `ok` would be a lie (§2.4)."""
    (calliope_gateway.directory / "identity.pub").unlink()
    (calliope_gateway.directory / "service.key").unlink()
    api, _, _ = client()
    assert api.get("/health").json()["status"] == "not_ready"


# ------------------------------------------------------------------- page --


def test_the_page_is_served_to_a_forwarded_request(client):
    api, _, _ = client()
    response = api.get("/ui")
    assert response.status_code == 200
    assert "<title>Calliope</title>" in response.text
    # Every call the page makes goes to its own origin, which is the gateway.
    assert "connect-src 'self'" in response.headers["content-security-policy"]


def test_the_root_belongs_to_the_gateway_and_is_not_served_here(client):
    """GET / is the gateway's public 303 to /ui or /login (D49); this one is gone."""
    api, _, _ = client()
    response = api.get("/", follow_redirects=False)
    assert response.status_code == 404
    assert_four_field_envelope(response)


def test_the_page_has_no_external_reference_of_any_kind(client):
    """No CDN, no font, no analytics: it works on a NAS with no internet."""
    api, _, _ = client()
    page = api.get("/ui").text
    for marker in ("http://", "https://cdn", "googleapis", "unpkg", "jsdelivr"):
        # AN XML NAMESPACE IS NOT A REQUEST. The favicon is an inline SVG data
        # URI, and an SVG that does not declare xmlns="http://www.w3.org/2000/svg"
        # does not render at all. Nothing is fetched from it.
        clean = page.replace("http://127.0.0.1", "").replace("http://www.w3.org/2000/svg", "")
        assert marker not in clean, marker


def _directives(response) -> dict[str, list[str]]:
    policy = response.headers["content-security-policy"]
    return {name: values for name, *values in
            (part.split() for part in policy.split(";") if part.strip())}


def test_only_the_page_s_own_inline_script_may_run(client):
    """A hash per inline script and nothing else (§4.7).

    'unsafe-inline' let any <script> or onerror= that reached the DOM run --
    and a MeTube title is chosen by whoever uploaded the video. With a hash,
    only the exact bytes this service served execute. 'self' is absent too:
    the page loads no script file, so allowing one from this origin would only
    admit something another route served.
    """
    api, _, _ = client()
    response = api.get("/ui")
    scripts = _directives(response)["script-src"]
    inline = re.findall(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script",
                        response.text, re.IGNORECASE | re.DOTALL)
    assert inline, "the page has no inline script to hash"
    expected = sorted(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in inline)
    assert sorted(scripts) == expected, scripts
    assert "'unsafe-inline'" not in scripts and "'self'" not in scripts


def test_the_page_carries_the_session_s_scopes_for_the_dock_s_first_paint(client, sign):
    """The dock is drawn with the tabs this session may open from the first
    frame; learnt from /auth/me after it, an admin's bar grew under the reader."""
    api, _, _ = client()
    speech = api.get("/ui/jobs").text
    admin = api.get("/ui/jobs", headers=sign(scopes=["users:manage", "satellites:read",
                                                     "speech:speak"])).text
    assert re.search(r'<html lang="en" data-scopes="[^"]*\bspeech:transcribe\b', speech)
    assert "users:manage" not in speech.split("<head", 1)[0]
    assert '<html lang="en" data-scopes="satellites:read speech:speak users:manage">' in admin


def test_the_page_cannot_be_framed_post_elsewhere_or_rebased(client):
    api, _, _ = client()
    directives = _directives(api.get("/ui/jobs/abc"))
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["form-action"] == ["'self'"]
    assert directives["base-uri"] == ["'none'"]


def test_a_script_saved_with_windows_line_endings_is_hashed_as_the_browser_reads_it():
    """The HTML parser turns CRLF into LF before the script exists, so the
    hash has to be of the LF form or the page's one script is blocked."""
    from app import main

    page = "<script>\r\nlet a = 1;\r\n</script>"
    unix = "\nlet a = 1;\n"
    digest = base64.b64encode(hashlib.sha256(unix.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in main.policy(page)


def test_a_page_with_no_inline_script_allows_no_script_at_all():
    from app import main
    assert "script-src 'none';" in main.policy("<p>nothing to run</p>")


def test_the_page_may_load_media_from_its_own_origin(client):
    """/ui/media is same-origin, so `media-src 'self'` covers it -- but a CSP
    tightened to `media-src blob:` alone would block every link's playback with
    a console message and no visible cause."""
    api, _, _ = client()
    assert "'self'" in _directives(api.get("/ui"))["media-src"]


def test_the_page_is_never_cached(client):
    """It carried no Cache-Control at all, so browsers applied their own
    heuristic and served a stale copy. A control that had been added and
    deployed was reported as missing, and the diagnosis went through the
    markup, the boot order and the route table before reaching the cache.
    """
    api, _, _ = client()
    for path in ("/ui", "/ui/speak"):
        cache = api.get(path).headers.get("cache-control", "")
        assert "no-cache" in cache, f"{path} served with cache-control={cache!r}"


def test_the_page_has_no_key_box(client):
    """It is not a bring-your-own-key tool; a person signs in."""
    page = client()[0].get("/ui").text
    assert 'id="key"' not in page
    assert 'id="keyshow"' not in page
    assert 'store.get("key"' not in page, "a key is still kept in localStorage"


def test_the_page_never_attaches_an_authorization_header(client):
    """Every XHR goes through api(), which is a bare same-origin fetch: the
    session cookie authenticates it, and a header set here would be a
    credential typed into a browser."""
    page = client()[0].get("/ui").text
    assert "Bearer" not in page.replace("WWW-Authenticate", ""), (
        "the page is building an Authorization header again")


def test_the_page_has_no_global_expert_gate(client):
    """One control per thing hidden. The <details> panels are the control.

    The checkbox was a second, global way to hide the same three panels, so a
    setting could be out of sight for two unrelated reasons and finding it
    meant reasoning about both.
    """
    page = client()[0].get("/ui").text
    assert 'id="expert"' not in page
    assert 'class="expert-only"' not in page
    assert "body:not(.expert)" not in page
    assert 'store.get("expert"' not in page


def test_the_three_expert_panels_are_still_on_the_page_and_openable(client):
    """Removing the gate must not remove the panels behind it."""
    page = client()[0].get("/ui").text
    for panel in ("stt-expert", "tts-expert-fast", "tts-expert-clone"):
        assert f'<details id="{panel}">' in page, f"{panel} is gone"


def test_the_two_engine_panels_still_swap_with_the_chosen_voice(client):
    """This is per-ENGINE, not per-expertise, and it is not what was removed.

    onVoiceChange() shows Kokoro's panel for an instant voice and tts-long's
    for anything that queues. Deleting the global gate must leave that alone,
    or both panels appear at once and half the controls on screen belong to a
    model that is not going to run.

    THE TEST IS WHERE IT RUNS, NOT WHAT THE VOICE IS. It was `clone`, which
    answered the same way only while every tts-long voice was a clone: a preset
    voice is not a clone and is not instant either, and under the old test it
    opened KOKORO's panel -- a synthesis-speed slider and a segments editor,
    neither of which that request can carry.
    """
    page = client()[0].get("/ui").text
    assert '$("tts-expert-fast").hidden = job;' in page
    assert '$("tts-expert-clone").hidden = !job;' in page
    # ANCHORED IN onVoiceChange, because `const job = isJob(voice);` also
    # appears in estimate() -- so a search over the whole page passes while
    # this function quietly goes back to reading the voice's kind.
    body = page[page.index("function onVoiceChange()"):
                page.index('$("voice").addEventListener')]
    assert "const job = isJob(voice);" in body, "the panels swap on the voice again"


# ------------------------------------------------------- the page's addresses --
#
# Every tab has a path, and every place inside one a path under it, which the
# page reads to open itself. The server's half is small and has to be exact:
# each such path is the page, byte for byte and header for header, and none of
# them may take a route this service already had.


def test_every_tab_has_an_address_the_server_serves(client):
    """The tab names are read off the page's own buttons, so a tab without a
    route fails here rather than 404ing on its first reload -- and Account and
    Admin are served whether or not the page has drawn their buttons yet."""
    api, _, _ = client()
    from app import main

    tabs = re.findall(r'role="tab" id="tab-btn-\w+" data-tab="(\w+)"', main.PAGE.read_text())
    assert tabs, "read no tabs off the page"
    routes = {route.path for route in main.app.routes}
    for tab in {"vocabulary" if tab == "vocab" else tab for tab in tabs} | {"account", "admin"}:
        assert f"/ui/{tab}" in routes, f"/ui/{tab} is not served"
        assert f"/ui/{tab}/{{rest:path}}" in routes, f"/ui/{tab}/... is not served"


def test_a_deep_link_serves_the_same_page_with_the_same_headers(client):
    """Anything else would be two pages, and a policy that differed by path
    would be a hole in whichever was looser."""
    api, _, _ = client()
    page = api.get("/ui")
    for path in ("/ui/satellites/kitchen/airplay", "/ui/account/keys", "/ui/admin/audit"):
        deep = api.get(path)
        assert deep.status_code == 200, path
        assert deep.text == page.text
        for header in ("content-security-policy", "cache-control", "content-type"):
            assert deep.headers[header] == page.headers[header], header


def test_a_deep_link_costs_no_outbound_request(client):
    """The page is static markup: loading it at any address asks MeTube and
    the gateway nothing, so it loads in exactly the cases /ui does."""
    api, gateway, tube = client()
    for path in ("/ui/transcribe", "/ui/speak/clone", "/ui/jobs/abc",
                 "/ui/vocabulary/tech", "/ui/satellites/wake-words/hey_jarvis/more"):
        assert api.get(path).status_code == 200, path
    assert not gateway.seen and not tube.requests


def test_the_page_views_shadow_no_route_of_this_service(client):
    """The tab names are the only doors: the service's own /ui routes still
    answer as themselves and a near miss is the 404 envelope."""
    api, _, _ = client()
    for path in ("/ui/config", "/ui/clips"):
        response = api.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("application/json"), path
    for path in ("/ui/nope", "/ui/transcribex", "/ui/vocab", "/ui/accounts"):
        response = api.get(path)
        assert response.status_code == 404, path
        assert_four_field_envelope(response)


# ------------------------------------------------ what this service no longer does --


def test_nothing_is_forwarded_to_the_gateway_any_more(client):
    """The proxy table, its /ui/api mount and /ui/health are gone (§1.9).

    The page calls the gateway's own paths with its cookie. Each of these was
    a path this service used to forward with a container key; each is now a
    404 here, and nothing leaves the service to find that out.
    """
    api, gateway, tube = client()
    for method, path in (("GET", "/voices"), ("POST", "/v1/audio/transcriptions"),
                         ("DELETE", "/jobs/abc"), ("GET", "/satellites"),
                         ("PUT", "/satellites/secrets"), ("GET", "/glossaries/tech"),
                         ("GET", "/ui/api/voices"), ("GET", "/ui/api/satellites"),
                         ("GET", "/ui/health"), ("GET", "/docs"),
                         ("GET", "/openapi.json")):
        response = api.request(method, path)
        assert response.status_code in (404, 405), (method, path)
        assert_four_field_envelope(response)
    assert not gateway.seen and not tube.requests


def test_no_removed_variable_is_read_by_this_service():
    """UI_GATEWAY_API_KEY is gone with the proxy it signed for. It is reported
    by voice_common.auth if set, and nothing here reads it any more."""
    from pathlib import Path

    source = "\n".join(p.read_text() for p in
                       (Path(__file__).resolve().parents[1] / "app").glob("*.py"))
    for name in ("UI_GATEWAY_API_KEY", "UI_GATEWAY_URL", "UI_GATEWAY_VERIFY"):
        assert name not in source, name


# ----------------------------------------------------------------- config --


def test_config_is_the_flags_and_limits_and_nothing_else(client):
    """Any session may read it, so it names features and ceilings: never an
    address, never who is asking, never anything per person."""
    api, _, _ = client()
    payload = api.get("/ui/config").json()
    assert set(payload) == {"ingestion", "cloning", "max_upload_bytes",
                            "max_clip_seconds", "stt_rtf_seed", "stt_budget_seconds"}
    assert payload["ingestion"] is True
    assert "metube" not in json.dumps(payload).lower()
    assert ALICE not in json.dumps(payload)
    # The seed is the conservative figure the gateway's own 900 s timeout was
    # built on, not the root README's optimistic one.
    assert payload["stt_rtf_seed"] == 8.5


def test_config_says_ingestion_is_off_when_metube_is_unset(client):
    api, _, _ = client(UI_METUBE_URL="")
    assert api.get("/ui/config").json()["ingestion"] is False


def test_resolve_is_a_clean_501_rather_than_a_hang_when_unconfigured(client):
    api, _, _ = client(UI_METUBE_URL="")
    response = api.post("/ui/resolve", json={"url": "https://example.com/v"})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "ingestion_not_configured"


@pytest.mark.parametrize("value,expected", [
    (None, "http://voice-gateway:8081"),
    ("http://voice-gateway:8081", "http://voice-gateway:8081"),
    ("http://127.0.0.1:18081", "http://127.0.0.1:18081"),
    ("http://[::1]:18081/", "http://[::1]:18081"),
])
def test_the_internal_listener_is_the_gateway_s_or_one_on_this_machine(value, expected):
    from app import config
    assert config._internal_url(value) == (expected, False)


def test_an_ignored_internal_listener_is_named_at_start_and_its_value_is_not(
        client, caplog):
    with caplog.at_level("ERROR", logger="voice-ui"):
        client(UI_GATEWAY_INTERNAL_URL="http://user:hunter2@evil.example:8081")
    logged = "\n".join(caplog.messages)
    assert "UI_GATEWAY_INTERNAL_URL is ignored" in logged
    assert "hunter2" not in logged and "evil.example" not in logged


@pytest.mark.parametrize("value", [
    "https://voice-gateway:8080", "http://evil.example:8081",
    "http://10.0.0.5:8081", "http://user:pw@127.0.0.1:8081",
    "http://127.0.0.1:8081/elsewhere", "ftp://127.0.0.1:8081",
])
def test_an_internal_listener_anywhere_else_is_ignored(value):
    """/ui/fetch sends this service's key there, so a setting may not point it
    off the box. It falls back to the gateway rather than switching link
    transcription off, and the lifespan says so by name."""
    from app import config
    assert config._internal_url(value) == ("http://voice-gateway:8081", True)
