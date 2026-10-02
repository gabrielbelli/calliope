"""Default deny, checked row by row (D3, D48, D49, recheck M-1, L2, L4).

The route table is the whole authorisation model of the stack: a backend
checks nothing itself. So these tests walk the live apps -- both listeners --
rather than a list written beside them, and every row is driven twice: once
by a credential that lacks its scope, once by one that holds exactly it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from conftest import (PASSWORD, SAME_ORIGIN, bearer, gateway, internal_client, make_key,
                      make_user, reload_gateway, sign_in)
from voice_common import scopes as scope_rules

from app import main as collected
from app import routetable

PARAMS = {"nid": "020000000001", "name": "dictation", "job_id": "j1", "sha256": "ab" * 32,
          "command": "play", "model_id": "kokoro", "rest": "x", "user_id": "u_aaaaaaaaaaaaaaaa",
          "key_id": "k_aaaaaaaaaaaa", "ref": "r" * 24}


def concrete(path: str) -> str:
    for name, value in PARAMS.items():
        path = path.replace("{" + name + "}", value).replace("{" + name + ":path}", value)
    return path


def rows(*, session_only: bool) -> list[tuple[str, str, routetable.Rule]]:
    return [(method, path, rule) for method, path, rule in routetable.rules_of(collected.app)
            if method != routetable.WEBSOCKET and not rule.public and rule.scopes
            and rule.session_only == session_only]


KEY_ROWS = rows(session_only=False)
ADMIN_SESSION_ROWS = [row for row in rows(session_only=True)
                      if not row[2].scopes <= scope_rules.ROLES["user-jobs"]]
# Every row that needs a scope user-jobs holds and user does not: the GPU
# lane, the job routes, the Jobs tab's addresses and saving a voice clip.
JOBS_ROWS = [row for row in rows(session_only=False) + rows(session_only=True)
             if row[2].scopes & scope_rules.JOBS_ONLY]
# Every row a user's session may use, which is what fast Transcribe and fast
# Speak are made of.
USER_ROWS = [row for row in rows(session_only=False) + rows(session_only=True)
             if row[2].scopes <= scope_rules.ROLES["user"] and not row[2].step_up]


# ── the table itself ──────────────────────────────────────────────────────────


def test_every_route_on_both_listeners_has_a_rule_or_is_public():
    from app import internal

    for app in (collected.app, internal.app):
        for method, path, rule in routetable.rules_of(app):
            assert rule.public or rule.scopes or rule.session_only, (method, path)
    public = {(m, p) for m, p, r in routetable.rules_of(collected.app) if r.public}
    assert public == {("GET", "/health"), ("GET", "/login"), ("GET", "/"),
                      ("POST", "/auth/login"), ("WS", "/satellites/ws"), ("WS", "/nodes/ws")}
    assert not [r for _, _, r in routetable.rules_of(internal.app) if r.public]


def test_a_route_added_without_a_rule_stops_the_import():
    from fastapi import FastAPI

    app = FastAPI()
    app.add_api_route("/new", lambda: None, methods=["GET"])
    with pytest.raises(RuntimeError, match="no rule"):
        routetable.bind(app, {})


def test_a_step_up_row_must_be_session_only_and_so_must_a_session_only_scope():
    with pytest.raises(ValueError):
        routetable.rule("users:manage", step_up=True)
    with pytest.raises(ValueError):
        routetable.rule("keys:manage:own")
    for app_rows in (routetable.rules_of(collected.app),):
        for method, path, rule in app_rows:
            if rule.step_up or rule.scopes & scope_rules.SESSION_ONLY:
                assert rule.session_only, (method, path)


def test_a_row_needing_two_scopes_needs_both():
    """/ui/fetch is ingest:links AND speech:transcribe (recheck L2)."""
    fetch = next(r for m, p, r in routetable.rules_of(collected.app)
                 if (m, p) == ("POST", "/ui/fetch"))
    assert fetch.scopes == {"ingest:links", "speech:transcribe"}


@pytest.mark.parametrize("method,path", sorted(
    (m, p) for m, p, _ in routetable.rules_of(collected.app) if m != routetable.WEBSOCKET))
def test_every_row_is_found_by_the_router_s_own_match(method, path):
    """The requirement is looked up with the same match Starlette dispatches
    with, first match in declaration order (recheck M-1): a concrete request
    for each row resolves to that row and no other."""
    scope = {"type": "http", "method": method, "path": concrete(path), "headers": [],
             "query_string": b"", "root_path": ""}
    route, _, _ = routetable.resolve(collected.app, scope)
    assert route is not None and route.path == path


# ── each row denies without its scope and allows with it ──────────────────────


@pytest.mark.parametrize("method,path,rule", KEY_ROWS,
                         ids=[f"{m} {p}" for m, p, _ in KEY_ROWS])
async def test_a_key_row_refuses_a_key_without_its_scope_and_admits_one_with_it(
        monkeypatch, method, path, rule):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        owner = make_user("ana")
        # Without the scope and without its :all form, which implies it (D29).
        wider = {scope[:-len("own")] + "all" for scope in rule.scopes if scope.endswith(":own")}
        lacking = make_key(owner, scopes=scope_rules.PRESETS["admin"].scopes - rule.scopes
                           - wider)
        holding = make_key(owner, scopes=rule.scopes)
        url = concrete(path)
        refused = await client.request(method, url, headers=bearer(lacking), json={})
        admitted = await client.request(method, url, headers=bearer(holding), json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "insufficient_scope"
    assert admitted.status_code not in (401, 403), admitted.text


@pytest.mark.parametrize("method,path,rule", ADMIN_SESSION_ROWS,
                         ids=[f"{m} {p}" for m, p, _ in ADMIN_SESSION_ROWS])
async def test_an_admin_only_session_row_refuses_a_user_jobs_user(monkeypatch, method, path,
                                                                   rule):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        make_user("sam", role="user-jobs")
        await sign_in(client, "sam")
        client.headers.update(SAME_ORIGIN)
        await client.post("/auth/step-up", json={"password": PASSWORD})
        refused = await client.request(method, concrete(path), json={})

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_scope"


def test_the_rows_a_user_is_refused_are_the_jobs_the_gpu_lane_and_clips():
    assert {(m, p) for m, p, _ in JOBS_ROWS} == {
        ("POST", "/jobs"), ("GET", "/jobs"), ("GET", "/jobs/{job_id}"),
        ("DELETE", "/jobs/{job_id}"), ("GET", "/jobs/{job_id}/audio"),
        ("DELETE", "/jobs/{job_id}/audio"), ("GET", "/ui/jobs"), ("GET", "/ui/jobs/{rest:path}"),
        ("POST", "/ui/clips"), ("DELETE", "/ui/clips/{name}")}
    assert {(m, p) for m, p, _ in USER_ROWS} >= {
        ("POST", "/v1/audio/transcriptions"), ("POST", "/transcribe"), ("POST", "/speak"),
        ("POST", "/v1/audio/speech"), ("GET", "/voices"),
        ("POST", "/ui/fetch"), ("GET", "/ui/media"), ("GET", "/ui/clips"),
        ("GET", "/glossaries"), ("PUT", "/glossaries/{name}"), ("GET", "/ui/speak"),
        ("GET", "/ui/transcribe"), ("GET", "/ui/account")}


async def user_session(client) -> None:
    make_user("una", role="user")
    await sign_in(client, "una")
    client.headers.update(SAME_ORIGIN)


@pytest.mark.parametrize("method,path,rule", JOBS_ROWS,
                         ids=[f"{m} {p}" for m, p, _ in JOBS_ROWS])
async def test_a_user_session_is_refused_every_row_that_runs_or_reads_a_job(
        monkeypatch, method, path, rule):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await user_session(client)
        refused = await client.request(method, concrete(path), json={})

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "insufficient_scope"


@pytest.mark.parametrize("method,path,rule", USER_ROWS,
                         ids=[f"{m} {p}" for m, p, _ in USER_ROWS])
async def test_a_user_session_is_admitted_to_every_row_its_role_holds(
        monkeypatch, method, path, rule):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        await user_session(client)
        admitted = await client.request(method, concrete(path), json={})

    assert admitted.status_code not in (401, 403), admitted.text


async def test_the_reserved_glossary_takes_glossaries_ha_and_never_own(monkeypatch):
    """Home Assistant's key reads and writes home-assistant with glossaries:ha;
    glossaries:read:own reaches every other name and not that one (D33, D34)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        owner = make_user("ana")
        ha = make_key(owner, scopes={"glossaries:ha"})
        own = make_key(owner, scopes={"glossaries:read:own", "glossaries:write:own"})
        results = {
            "ha reads it": await client.get("/glossaries/home-assistant", headers=bearer(ha)),
            "ha writes it": await client.put("/glossaries/home-assistant", headers=bearer(ha),
                                             content=b"a = b"),
            "own reads it": await client.get("/glossaries/home-assistant",
                                             headers=bearer(own)),
            "own writes it": await client.put("/glossaries/home-assistant",
                                              headers=bearer(own), content=b"a = b"),
            "ha reads another": await client.get("/glossaries/dictation",
                                                 headers=bearer(ha)),
            "own reads another": await client.get("/glossaries/dictation",
                                                  headers=bearer(own)),
        }

    codes = {name: r.status_code for name, r in results.items()}
    assert codes == {"ha reads it": 200, "ha writes it": 200, "own reads it": 403,
                     "own writes it": 403, "ha reads another": 403,
                     "own reads another": 200}


async def test_a_satellites_read_key_cannot_read_telemetry_or_routing(monkeypatch):
    """The home-assistant and monitor presets hold satellites:read. Matched as
    /satellites/{nid}, these GETs would be theirs (recheck M-1)."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        key = make_key(make_user("ana"), scopes={"satellites:read"})
        answers = {path: (await client.get(path, headers=bearer(key))).status_code
                   for path in ("/satellites/telemetry", "/satellites/telemetry/summary",
                                "/satellites/routing", "/satellites/020000000001")}

    assert answers == {"/satellites/telemetry": 403, "/satellites/telemetry/summary": 403,
                       "/satellites/routing": 403, "/satellites/020000000001": 200}


@pytest.mark.parametrize("raw", [
    b"/satellites/x/../telemetry", b"/satellites/./telemetry", b"/satellites/a%2Ftelemetry",
    b"/glossaries/a%5Cb", b"/v1//models", b"/jobs/%2e%2e/x",
])
async def test_a_path_the_router_and_a_backend_might_read_differently_is_refused(
        monkeypatch, raw):
    from urllib.parse import unquote

    async with gateway(monkeypatch) as (client, main):
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await main.app({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "https", "path": unquote(raw.decode()),
            "raw_path": raw, "query_string": b"", "root_path": "",
            "headers": [(b"host", b"gateway.test"),
                        (b"authorization", client.headers["authorization"].encode())],
            "client": ("192.0.2.1", 1), "server": ("gateway.test", 443)}, receive, send)

    assert sent[0]["status"] == 400


async def _send_raw(app, method: str, raw: bytes, key: str) -> list[dict]:
    """One request with exactly this request target, decoded as uvicorn decodes it."""
    from urllib.parse import unquote

    sent: list[dict] = []
    finished = asyncio.Event()
    asked: list[bool] = []

    async def receive():
        # The body once; then the client hangs up only after the answer, as
        # a real one does. A streamed answer waits on this for a disconnect.
        if not asked:
            asked.append(True)
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    path, _, query = raw.partition(b"?")
    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "https", "path": unquote(path.decode()),
        "raw_path": path, "query_string": query, "root_path": "",
        "headers": [(b"host", b"gateway.test"), (b"authorization", f"Bearer {key}".encode())],
        "client": ("192.0.2.1", 1), "server": ("gateway.test", 443)}, receive, send)
    return sent


@pytest.mark.parametrize("method,raw,scopes", [
    (method, path + smuggled, scopes)
    for method, path, scopes in (
        ("GET", b"/satellites/telemetry", {"satellites:read"}),
        ("GET", b"/satellites/routing", {"satellites:read"}),
        ("GET", b"/satellites/telemetry/clips/a.wav", {"satellites:read"}),
        ("GET", b"/glossaries/home-assistant", {"glossaries:read:own"}),
        ("PUT", b"/glossaries/home-assistant", {"glossaries:write:own"}))
    for smuggled in (b"%3F", b"%23", b"%3Fowner=all", b"%252Fsummary")
])
async def test_a_path_that_hides_another_path_never_reaches_a_backend(
        monkeypatch, method, raw, scopes):
    """A decoded `?` or `#` once cut the forwarded path short, and `%25` was
    decoded a second time by the backend: a satellites:read key reached the
    telemetry settings and its recordings, and an :own key the reserved
    glossary (recheck M-1)."""
    from conftest import MockBackend

    hub, stt = MockBackend("voice-satellites"), MockBackend("stt-stack")
    async with gateway(monkeypatch, satellites=hub, stt=stt, authenticate=False) as (_, main):
        key = make_key(make_user("ana"), scopes=scopes)
        sent = await _send_raw(main.app, method, raw, key)

    assert sent[0]["status"] in (400, 403)
    assert hub.seen == [] and stt.seen == []


@pytest.mark.parametrize("raw,path,query", [
    (b"/glossaries/my%20list", "/glossaries/my list", ""),
    (b"/glossaries/caf%C3%A9?force=true", "/glossaries/café", "force=true"),
])
async def test_the_backend_decodes_exactly_the_path_that_was_authorised(
        monkeypatch, raw, path, query):
    from conftest import MockBackend

    stt = MockBackend("stt-stack")
    async with gateway(monkeypatch, stt=stt, authenticate=False) as (_, main):
        key = make_key(make_user("ana"), scopes={"glossaries:read:own"})
        sent = await _send_raw(main.app, "GET", raw, key)

    assert sent[0]["status"] == 200
    assert (stt.last["path"], stt.last["query"]) == (path, query)


async def test_a_route_added_after_the_table_is_bound_is_refused_on_both_listeners(
        monkeypatch):
    """bind() fences import time; a route registered after it matches with no
    rule, and must be refused rather than open to every credential (D48)."""
    from conftest import service_key

    from app import internal

    ran: list[str] = []

    async def late() -> dict:
        ran.append("ran")
        return {}

    # A copy, so the route added below leaves with this test.
    monkeypatch.setattr(internal.app.router, "routes", list(internal.app.router.routes))
    async with gateway(monkeypatch) as (client, main):
        main.app.add_api_route("/late", late, methods=["GET"])
        internal.app.add_api_route("/late", late, methods=["GET"])
        public = await client.get("/late")
        async with internal_client() as private_client:
            private = await private_client.get("/late", headers=bearer(service_key("ui")))

    assert (public.status_code, private.status_code) == (500, 500)
    assert ran == []


async def test_head_is_checked_as_its_get_row_and_options_is_405(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        owner = make_user("ana")
        without = make_key(owner, scopes={"speech:speak"})
        refused = await client.head("/v1/models", headers=bearer(without))
        options = await client.options("/v1/models", headers=bearer(without))

    assert refused.status_code == 403
    assert options.status_code == 405


async def test_an_unknown_path_asks_for_a_credential_before_it_says_404(monkeypatch):
    """Otherwise the 404s and 405s would map the route table for anyone."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        anonymous = await client.get("/v1/nope")
        key = make_key(make_user("ana"))
        known = await client.get("/v1/nope", headers=bearer(key))

    assert anonymous.status_code == 401
    assert known.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_the_documentation_routes_exist_on_neither_listener(monkeypatch, path):
    from conftest import service_key

    async with gateway(monkeypatch) as (client, _):
        public = await client.get(path)
        async with internal_client() as internal:
            private = await internal.get(path, headers=bearer(service_key("ui")))

    assert public.status_code == 404
    assert private.status_code == 404


# ── the device socket ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_the_device_socket_needs_no_credential_and_refuses_a_browser(monkeypatch, origin):
    """Devices cannot log in, so the socket is public here; the hub decides by
    adoption token. A browser always sends its own Origin, and the devices
    send none (the Pi) or `file://` (the Korvo's library), so any other Origin
    is a page trying the door (D53, CSWSH)."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    main = reload_gateway(monkeypatch)
    dialled: list[str] = []

    async def no_hub(target, **_):
        dialled.append(target)
        raise OSError("no hub in a unit test")

    monkeypatch.setattr(main, "ws_connect", no_hub)
    with TestClient(main.app) as client:
        with client.websocket_connect("/satellites/ws") as device:
            with pytest.raises(WebSocketDisconnect) as closed:
                device.receive_text()
        with client.websocket_connect("/nodes/ws", headers={"origin": "file://"}) as korvo:
            with pytest.raises(WebSocketDisconnect) as korvo_closed:
                korvo.receive_text()
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/satellites/ws", headers={"origin": origin}):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ui/anything"):
                pass

    assert closed.value.code == korvo_closed.value.code == 1013, \
        "a device was not relayed at all"
    assert refused.value.code == 1008
    assert len(dialled) == 2


async def test_a_plain_get_on_the_socket_path_is_not_public(monkeypatch):
    """PUBLIC names the socket by its ASGI type (recheck L4): an HTTP GET on
    /satellites/ws is GET /satellites/{nid}, which needs satellites:read."""
    async with gateway(monkeypatch, authenticate=False) as (client, _):
        response = await client.get("/satellites/ws")
    assert response.status_code == 401


def test_the_access_log_never_carries_a_pasted_link():
    """The page polls /ui/progress?token=<the link> once a second, and uvicorn
    wrote every poll to the access log. A /ui/ path loses its query string
    there; any other path keeps it."""
    access = logging.getLogger("uvicorn.access")

    def line(path: str) -> str:
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
            ("192.0.2.7:50000", "GET", path, "1.1", 200), None)
        assert access.filter(record)
        return record.getMessage()

    polled = line("/ui/progress?token=https://media.example/watch?v=abc")
    assert "media.example" not in polled and "token" not in polled
    assert '"GET /ui/progress HTTP/1.1" 200' in polled
    assert "/jobs?status=done" in line("/jobs?status=done")
    assert len([f for f in access.filters
                if type(f).__name__ == "_NoLinkInTheAccessLog"]) == 1, "added once per import"
