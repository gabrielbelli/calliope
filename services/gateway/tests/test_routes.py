

# ------------------------------------------- the routes the agents needed --


def test_the_captions_route_reaches_the_ui():
    """A captions download is already a transcript and never touches stt.
    Absent from UI_PATHS the page 404s on it from the published port, which is
    how DELETE /jobs/{id} stayed unreachable while tts-long had it all along."""
    from app.main import UI_PATHS

    assert ("POST", "/ui/captions") in {(m, p) for m, p, _ in UI_PATHS}


def test_every_glossary_route_is_routed():
    from app.main import app

    routes = {(m, r.path) for r in app.routes
              for m in getattr(r, "methods", ()) or ()}
    for method, path in (("GET", "/glossaries"), ("GET", "/glossaries/{name}"),
                         ("PUT", "/glossaries/{name}"),
                         ("DELETE", "/glossaries/{name}")):
        assert (method, path) in routes, f"{method} {path} is not routed"


async def test_the_query_string_survives_the_proxy(monkeypatch):
    """?force=true is what lets a single-word left-hand side through. Dropped
    silently, a `fennel = Fennell` rule becomes unenterable through the front door
    while appearing to work."""
    from conftest import MockBackend, gateway

    stt = MockBackend("stt-stack")
    async with gateway(monkeypatch, stt=stt) as (client, _):
        await client.put("/glossaries/dictation?force=true", content=b"fennel = Fennell\n")

    assert (stt.last["path"], stt.last["query"]) == ("/glossaries/dictation", "force=true")


def test_the_media_relay_is_routed():
    """Byte ranges are what let <video> seek. Unrouted here, playback 404s from
    the published port -- the DELETE /jobs/{id} failure again."""
    from app.main import UI_PATHS

    assert ("GET", "/ui/media") in {(m, p) for m, p, _ in UI_PATHS}


def test_proxy_does_not_strip_range_headers():
    """The relay parses no ranges of its own; it depends on this hop leaving
    Range and Content-Range alone."""
    from app.main import DROP_FROM_REQUEST, HOP_BY_HOP

    for header in ("range", "if-range", "content-range", "accept-ranges"):
        assert header not in DROP_FROM_REQUEST, f"{header} is dropped"
        assert header not in HOP_BY_HOP, f"{header} is treated as hop-by-hop"


# ------------------------------------------------------ the page's addresses --
#
# The page has an address per tab and per place inside one, and a reload or a
# pasted link arrives at the gateway first. Each tab name is one GET pair --
# the bare name and everything under it -- and voice-ui answers all of them
# with the same static file. The two tests below keep that from widening into
# the catch-all the UI_PATHS comment forbids.

PAGE_VIEWS = ("transcribe", "speak", "jobs", "vocabulary", "satellites", "account",
              "admin")


def test_every_page_view_reaches_the_ui():
    """A tab address absent here is a 404 on reload from the published port,
    while the same address works when the page was reached by clicking."""
    from app.main import UI_PATHS

    pairs = {(method, path) for method, path, _ in UI_PATHS}
    for view in PAGE_VIEWS:
        assert ("GET", f"/ui/{view}") in pairs, f"/ui/{view} is not routed"
        assert ("GET", f"/ui/{view}/{{rest:path}}") in pairs, f"/ui/{view}/... is not routed"


def test_every_path_tail_in_ui_paths_sits_under_a_page_view():
    """A {rest:path} anywhere else would be a door to whatever voice-ui grows
    next under that prefix, which is the wildcard this table exists to refuse.
    The /ui/api/ mount was the one exception, and it is gone."""
    from app.main import UI_PATHS

    allowed = tuple(f"/ui/{view}/" for view in PAGE_VIEWS)
    tails = [path for _, path, _ in UI_PATHS if "{rest:path}" in path]
    assert tails, "found no path tails at all, so this proves nothing"
    for path in tails:
        assert path.startswith(allowed), f"{path} is a path tail outside the page"


def test_a_page_view_is_for_a_session_and_opens_only_with_its_tabs_scope():
    """Every page address is session-only (§3.4): a key has no use for the
    page shell, and a tab a role lacks lands on /ui rather than half-loading."""
    from app.main import PAGE_TABS, UI_PATHS

    scopes = dict(PAGE_TABS)
    for method, path, requirement in UI_PATHS:
        tab = path.split("/")[2] if path.count("/") >= 2 else None
        if tab in scopes:
            assert requirement.session_only, path
            assert requirement.scopes == {scopes[tab]}, path
