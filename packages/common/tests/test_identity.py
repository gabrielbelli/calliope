"""The identity assertion: what verifies, what does not, and what a handler sees.

Every refusal below is a token someone could actually send: one meant for
another service, one captured a minute ago, one signed by any key but the
gateway's, or one that names a role in the hope that a backend reads it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import fields
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voice_common import identity
from voice_common.conformance import FakeGateway
from voice_common.errors import install_errors
from voice_common.identity import (ASSERTION_HEADER, DELEGATION_HEADER,
                                   Claims, Credentials, InvalidAssertion,
                                   Signer, verify, verify_delegation)

USER = "u_mfrggzdfmztwq2lk"
NOW = 1_800_000_000


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def forge(signer: Signer, body: str | dict) -> str:
    """Sign an arbitrary payload with the REAL key: what a buggy or newer gateway could mint."""
    raw = body if isinstance(body, str) else json.dumps(body)
    payload = b64(raw.encode("utf-8"))
    signing_input = f"v1.{signer.kid}.{payload}"
    return f"{signing_input}.{b64(signer.private_key.sign(signing_input.encode()))}"


def body(**overrides: object) -> dict:
    claims = {"iss": "calliope-gateway", "aud": "stt", "sub": USER,
              "kind": "user", "scopes": ["speech:transcribe"],
              "cred": "session", "iat": NOW, "exp": NOW + 60,
              "jti": "abcdefghijklmnop"}
    claims.update(overrides)
    return claims


@pytest.fixture
def signer() -> Signer:
    return Signer.generate("7")


@pytest.fixture
def keys(signer: Signer) -> dict:
    return {"7": signer.public_key}


def reason(token: str, keys: dict | Credentials, audience: str = "stt",
           now: float = NOW) -> str:
    with pytest.raises(InvalidAssertion) as caught:
        verify(token, audience, keys, now=now)
    return caught.value.reason


# ── the token ────────────────────────────────────────────────────────────────

def test_an_assertion_round_trips_with_every_claim(signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="stt", sub=USER, kind="user",
                             scopes={"speech:transcribe", "jobs:read:own"},
                             cred="k_abcdefghijkl", now=NOW)
    claims = verify(token, "stt", keys, now=NOW + 30)
    assert claims.sub == USER
    assert claims.kind == "user"
    assert claims.scopes == {"speech:transcribe", "jobs:read:own"}
    assert claims.cred == "k_abcdefghijkl"
    assert claims.exp - claims.iat == 60
    assert claims.aud == "stt" and not claims.dlg


def test_the_claims_have_no_role_field() -> None:
    """A backend that tested `role == "admin"` would widen an admin's read-only key (M1)."""
    assert "role" not in {field.name for field in fields(Claims)}


def test_an_assertion_carrying_a_role_is_refused_even_when_validly_signed(
        signer: Signer, keys: dict) -> None:
    assert reason(forge(signer, body(role="admin")), keys) == "bad_claims"


def test_an_assertion_missing_a_claim_is_refused(signer: Signer, keys: dict) -> None:
    claims = body()
    del claims["jti"]
    assert reason(forge(signer, claims), keys) == "bad_claims"


def test_duplicate_claim_keys_are_refused(signer: Signer, keys: dict) -> None:
    """Two parsers reading two different `sub`s from one token is not a token."""
    raw = json.dumps(body())[:-1] + ', "sub": "u_aaaaaaaaaaaaaaaa"}'
    assert reason(forge(signer, raw), keys) == "bad_claims"


def test_an_assertion_for_another_audience_is_refused(signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="satellites", sub=USER, kind="user",
                             scopes=(), cred="session", now=NOW)
    assert reason(token, keys, audience="stt") == "wrong_audience"


def test_an_assertion_from_another_issuer_is_refused(signer: Signer, keys: dict) -> None:
    assert reason(forge(signer, body(iss="someone-else")), keys) == "wrong_issuer"


def test_an_expired_assertion_is_refused(signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW)
    verify(token, "stt", keys, now=NOW + 60)  # within the leeway
    assert reason(token, keys, now=NOW + 66) == "expired"


def test_an_assertion_from_the_future_is_refused(signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW + 120)
    assert reason(token, keys, now=NOW) == "not_yet_valid"


def test_an_assertion_signed_to_live_longer_than_a_minute_is_refused(
        signer: Signer, keys: dict) -> None:
    """The lifetime is part of the contract: a signed hour is not accepted as a minute."""
    token = signer.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW, lifetime=3600)
    assert reason(token, keys, now=NOW + 1) == "lifetime"


def test_an_assertion_signed_by_another_key_under_the_same_kid_is_refused(
        keys: dict) -> None:
    forger = Signer.generate("7")
    token = forger.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW)
    assert reason(token, keys) == "bad_signature"


def test_a_tampered_payload_is_refused(signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="stt", sub=USER, kind="user",
                             scopes={"speech:transcribe"}, cred="session", now=NOW)
    version, kid, payload, signature = token.split(".")
    wider = b64(json.dumps(body(scopes=["jobs:read:all"])).encode())
    assert reason(".".join((version, kid, wider, signature)), keys) == "bad_signature"


@pytest.mark.parametrize("garbage", [
    "v1", "v1.7.a", "v2.7.e30.AAAA", "v1.x.e30.AAAA", "v1.7.e30.AAAA.AAAA",
    "v1.7.é.AAAA", "v1.7.e30.A", "v1.7.e30=.AAAA", "v1.7.e30.AAAA" + "A" * 5000,
    "Bearer v1.7.e30.AAAA",
])
def test_a_malformed_header_is_refused_and_never_an_exception(
        garbage: str, keys: dict) -> None:
    assert reason(garbage, keys) in {"malformed", "bad_signature"}


def test_an_empty_header_is_missing(keys: dict) -> None:
    assert reason("", keys) == "missing"


def test_an_unknown_kid_is_refused(signer: Signer) -> None:
    token = signer.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW)
    assert reason(token, {}) == "unknown_key"


@pytest.mark.parametrize(("sub", "kind", "cred"), [
    ("svc:satellites", "user", "session"),       # a service dressed as a user
    (USER, "service", USER),                     # a user dressed as a service
    ("svc:satellites", "service", "session"),    # a service claiming a session
    (USER, "user", "svc:satellites"),            # a user claiming a service's cred
    (USER, "user", "session:abcdefghijklmnopq"), # a delegation cred on an assertion
    (USER, "admin", "session"),                  # a kind that does not exist
    ("u_ABCDEFGHIJKLMNOP", "user", "session"),   # not a user ID
])
def test_sub_kind_and_cred_must_agree(sub: str, kind: str, cred: str,
                                      signer: Signer, keys: dict) -> None:
    """Backends partition by `sub`; a user `sub` with a service shape would land in system."""
    assert reason(forge(signer, body(sub=sub, kind=kind, cred=cred)), keys) == "bad_claims"


@pytest.mark.parametrize("scopes", [["jobs:*"], ["Jobs:read"], "speech:speak",
                                    [1], ["a:b"] * 129])
def test_scopes_that_break_the_grammar_are_refused(scopes: object, signer: Signer,
                                                   keys: dict) -> None:
    assert reason(forge(signer, body(scopes=scopes)), keys) == "bad_claims"


@pytest.mark.parametrize("value", [True, 1.5, "1800000000", None])
def test_a_timestamp_must_be_an_integer(value: object, signer: Signer,
                                        keys: dict) -> None:
    """`true` is an int to Python and is not a time."""
    assert reason(forge(signer, body(iat=value)), keys) == "bad_claims"


def test_the_signer_refuses_to_mint_what_no_backend_would_accept(signer: Signer) -> None:
    with pytest.raises(InvalidAssertion):
        signer.assertion(audience="stt", sub="admin", kind="user", scopes=(),
                         cred="session")
    with pytest.raises(ValueError):
        signer.assertion(audience="gateway", sub=USER, kind="user", scopes=(),
                         cred="session")


# ── delegation (D64) ─────────────────────────────────────────────────────────

def test_a_delegation_token_verifies_for_the_gateway_for_thirty_minutes(
        signer: Signer, keys: dict) -> None:
    token = signer.delegation(sub=USER, cred="session:abcdefghijklmnopq", now=NOW)
    claims = verify_delegation(token, keys, now=NOW + 29 * 60)
    assert claims.dlg and claims.aud == "gateway" and claims.sub == USER
    assert claims.cred == "session:abcdefghijklmnopq"
    with pytest.raises(InvalidAssertion) as caught:
        verify_delegation(token, keys, now=NOW + 31 * 60)
    assert caught.value.reason == "expired"


def test_a_delegation_token_is_never_accepted_as_an_identity_assertion(
        signer: Signer, keys: dict) -> None:
    """voice-ui holds one for 30 minutes; it must not open any backend."""
    token = signer.delegation(sub=USER, cred="k_abcdefghijkl", now=NOW)
    for audience in identity.AUDIENCES:
        assert reason(token, keys, audience=audience) == "delegation"


def test_an_identity_assertion_is_never_accepted_as_a_delegation_token(
        signer: Signer, keys: dict) -> None:
    token = signer.assertion(audience="ui", sub=USER, kind="user", scopes=(),
                             cred="session", now=NOW)
    with pytest.raises(InvalidAssertion) as caught:
        verify_delegation(token, keys, now=NOW)
    assert caught.value.reason == "not_delegation"


def test_a_delegation_token_must_name_the_session_it_came_from(
        signer: Signer, keys: dict) -> None:
    """A bare "session" cannot be re-checked live, so it cannot be delegated."""
    with pytest.raises(InvalidAssertion):
        signer.delegation(sub=USER, cred="session", now=NOW)


# ── the credential files ─────────────────────────────────────────────────────

class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_an_unknown_kid_reloads_identity_pub_and_then_verifies(tmp_path: Path) -> None:
    """Rotation (D69): the gateway writes the new kid, then signs with it."""
    old, new = Signer.generate("1"), Signer.generate("2")
    identity.write_public_keys(tmp_path / "identity.pub", {"1": old.public_key})
    credentials = Credentials(tmp_path)
    token = new.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                          cred="session", now=NOW)
    assert reason(token, credentials) == "unknown_key"
    identity.write_public_keys(tmp_path / "identity.pub",
                               {"1": old.public_key, "2": new.public_key})
    assert verify(token, "stt", credentials, now=NOW).sub == USER


def test_a_kid_dropped_from_identity_pub_stops_verifying_within_the_recheck_interval(
        tmp_path: Path) -> None:
    """A cache that only grew would keep a compromised, rotated-out key alive forever."""
    old, new = Signer.generate("1"), Signer.generate("2")
    identity.write_public_keys(tmp_path / "identity.pub",
                               {"1": old.public_key, "2": new.public_key})
    clock = Clock()
    credentials = Credentials(tmp_path, clock=clock)
    token = old.assertion(audience="stt", sub=USER, kind="user", scopes=(),
                          cred="session", now=NOW)
    assert verify(token, "stt", credentials, now=NOW)
    identity.write_public_keys(tmp_path / "identity.pub", {"2": new.public_key})
    clock.now += identity.RECHECK_SECONDS
    assert reason(token, credentials) == "unknown_key"


def test_unknown_kids_never_reparse_an_unchanged_file(
        tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rate-limited (recheck L15): a flood of made-up kids costs a stat each, not a parse."""
    identity.write_public_keys(tmp_path / "identity.pub", {"7": signer.public_key})
    parses = []
    real = identity._parse_public_keys
    monkeypatch.setattr(identity, "_parse_public_keys",
                        lambda text: parses.append(1) or real(text))
    credentials = Credentials(tmp_path)
    for kid in range(1, 500):
        credentials.public_key(str(kid + 1000))
    assert len(parses) == 1


def test_a_torn_identity_pub_keeps_the_keys_already_loaded(
        tmp_path: Path, signer: Signer) -> None:
    identity.write_public_keys(tmp_path / "identity.pub", {"7": signer.public_key})
    credentials = Credentials(tmp_path)
    assert credentials.public_key("7") is not None
    path = tmp_path / "identity.pub"
    path.chmod(0o644)
    path.write_text('{"keys": [', encoding="utf-8")
    assert credentials.public_key("8") is None
    assert credentials.public_key("7") is not None


def test_a_broken_identity_pub_is_parsed_once_not_once_per_request(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "identity.pub").write_text("not json", encoding="utf-8")
    credentials = Credentials(tmp_path)
    for kid in range(100):
        credentials.public_key(str(kid))
    assert len([r for r in caplog.records if "could not be read" in r.getMessage()]) == 1


def test_a_missing_identity_pub_verifies_nothing(tmp_path: Path, signer: Signer) -> None:
    identity.write_public_keys(tmp_path / "identity.pub", {"7": signer.public_key})
    clock = Clock()
    credentials = Credentials(tmp_path, clock=clock)
    assert credentials.public_key("7") is not None
    (tmp_path / "identity.pub").unlink()
    clock.now += identity.RECHECK_SECONDS
    assert credentials.public_key("7") is None


def test_credentials_are_not_ready_until_both_files_exist(tmp_path: Path,
                                                          signer: Signer) -> None:
    """§2.4: a backend reports not_ready until the gateway has minted its files."""
    clock = Clock()
    credentials = Credentials(tmp_path, clock=clock)
    assert not credentials.ready
    identity.write_public_keys(tmp_path / "identity.pub", {"7": signer.public_key})
    clock.now += identity.POLL_SECONDS
    assert not credentials.ready
    (tmp_path / "service.key").write_text("calliope_svc_" + "a" * 36 + "\n")
    assert not credentials.ready  # looked for at most every two seconds
    clock.now += identity.POLL_SECONDS
    assert credentials.ready
    assert credentials.service_key() == "calliope_svc_" + "a" * 36


def test_a_service_key_file_that_holds_no_service_key_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "service.key").write_text("calliope_AbCdEfGh\n")
    assert Credentials(tmp_path).service_key() is None


def test_reload_service_key_reads_the_rotated_key_at_once(tmp_path: Path) -> None:
    """After a 401 from the internal listener, the next read is now, not in two seconds."""
    (tmp_path / "service.key").write_text("calliope_svc_" + "a" * 36)
    credentials = Credentials(tmp_path, clock=Clock())
    assert credentials.service_key().endswith("a" * 36)
    (tmp_path / "service.key").write_text("calliope_svc_" + "b" * 36)
    assert credentials.service_key().endswith("a" * 36)
    assert credentials.reload_service_key().endswith("b" * 36)


def test_the_directory_follows_the_environment_so_tests_can_move_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = Credentials()
    monkeypatch.setenv(identity.RUN_DIR_ENV, str(tmp_path))
    assert credentials.directory == tmp_path
    monkeypatch.delenv(identity.RUN_DIR_ENV)
    assert credentials.directory == Path("/run/calliope")


def test_identity_pub_is_a_jwk_set_written_read_only(tmp_path: Path,
                                                    signer: Signer) -> None:
    path = tmp_path / "identity.pub"
    identity.write_public_keys(path, {"7": signer.public_key})
    document = json.loads(path.read_text())
    assert document == {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": "7",
                                  "x": document["keys"][0]["x"]}]}
    assert path.stat().st_mode & 0o777 == 0o444
    assert not [p for p in tmp_path.iterdir() if p.name != "identity.pub"]


# ── the middleware ───────────────────────────────────────────────────────────

def build(gateway: FakeGateway, **kwargs: object) -> tuple[FastAPI, TestClient]:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {"headers": sorted(request.headers.keys()),
                "sub": identity.claims_of(request).sub,
                "delegation": identity.delegation_of(request)}

    @app.post("/ui/fetch")
    async def fetch(request: Request) -> dict[str, object]:
        return {"delegation": identity.delegation_of(request)}

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("open")
        await websocket.close()

    @app.websocket("/health")
    async def health_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    identity.install(app, "ui", **kwargs)  # type: ignore[arg-type]
    return app, TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway(tmp_path / "run")
    monkeypatch.setenv(identity.RUN_DIR_ENV, str(fake.directory))
    return fake


def test_install_removes_both_assertion_headers_from_the_scope(
        gateway: FakeGateway) -> None:
    """A handler that proxies onward has nothing to forward by mistake (D65)."""
    _, client = build(gateway)
    response = client.get("/echo", headers={
        **gateway.headers("ui"), DELEGATION_HEADER: gateway.delegation(),
        "X-Calliope-Something-Else": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["sub"] == FakeGateway.USER
    assert not [h for h in response.json()["headers"] if h.startswith("x-calliope-")]


def test_a_middleware_added_after_install_still_never_sees_the_assertion(
        gateway: FakeGateway) -> None:
    """add_middleware puts the newest outermost; install wraps the finished stack instead."""
    app, client = build(gateway)
    seen: list[list[str]] = []

    @app.middleware("http")
    async def nosy(request, call_next):  # noqa: ANN001, ANN202
        seen.append([h for h in request.headers if h.startswith("x-calliope-")])
        return await call_next(request)

    assert client.get("/echo", headers=gateway.headers("ui")).status_code == 200
    assert seen == [[]]


def test_the_delegation_token_reaches_only_the_delegating_path(
        gateway: FakeGateway) -> None:
    """§3.7: voice-ui's /ui/fetch passes it on; no other handler ever holds it."""
    _, client = build(gateway, delegation_paths=["/ui/fetch"])
    token = gateway.delegation()
    headers = {**gateway.headers("ui"), DELEGATION_HEADER: token}
    assert client.post("/ui/fetch", headers=headers).json() == {"delegation": token}
    assert client.get("/echo", headers=headers).json()["delegation"] is None


def test_a_delegation_token_for_another_user_or_from_another_key_is_withheld(
        gateway: FakeGateway, tmp_path: Path) -> None:
    """A handler is never handed a token it would be passing on for somebody else."""
    _, client = build(gateway, delegation_paths=["/ui/fetch"])
    others = gateway.delegation(sub="u_bbbbbbbbbbbbbbbb")
    forged = FakeGateway(tmp_path / "forger").delegation()
    for token in (others, forged, "not-a-token"):
        response = client.post("/ui/fetch", headers={**gateway.headers("ui"),
                                                     DELEGATION_HEADER: token})
        assert response.json() == {"delegation": None}


def test_a_delegation_header_alone_is_not_an_identity(gateway: FakeGateway) -> None:
    _, client = build(gateway, delegation_paths=["/ui/fetch"])
    response = client.post("/ui/fetch",
                           headers={DELEGATION_HEADER: gateway.delegation()})
    assert response.status_code == 401


def test_health_is_open_and_nothing_else_is(gateway: FakeGateway) -> None:
    _, client = build(gateway)
    assert client.get("/health").status_code == 200
    assert client.get("/health/").status_code != 401
    assert client.head("/health").status_code != 401
    assert client.post("/health").status_code == 401
    assert client.get("/healthz").status_code == 401
    assert client.get("/echo").status_code == 401


def test_the_health_exemption_reaches_the_health_route_and_no_catch_all(
        gateway: FakeGateway) -> None:
    """D49 and D52 open /health, not every path a catch-all route would answer.

    Stripping every trailing slash used to let /health// through, and a
    catch-all (an SPA fallback, say) would have served it with no assertion.
    """
    app, client = build(gateway)

    @app.get("/{rest:path}")
    async def anything(rest: str) -> dict[str, str]:
        return {"served": rest}

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/").json() == {"status": "ok"}
    for path in ("/health//", "/health///"):
        assert client.get(path).status_code == 401, path


def test_a_request_without_an_assertion_is_a_401_in_the_envelope(
        gateway: FakeGateway) -> None:
    _, client = build(gateway)
    response = client.get("/echo")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthenticated"
    assert set(response.json()["error"]) == {"message", "type", "param", "code"}


def test_two_assertion_headers_are_refused(gateway: FakeGateway) -> None:
    """Which one a framework would pick is not something to rely on."""
    _, client = build(gateway)
    token = gateway.assertion("ui")
    response = client.get("/echo", headers=[(ASSERTION_HEADER, token),
                                            (ASSERTION_HEADER, token)])
    assert response.status_code == 401


def test_docs_routes_are_removed_even_for_a_valid_assertion(
        gateway: FakeGateway) -> None:
    _, client = build(gateway)
    for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
        assert client.get(path, headers=gateway.headers("ui")).status_code == 404, path


def test_a_websocket_without_an_assertion_never_opens(gateway: FakeGateway) -> None:
    _, client = build(gateway)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws"):
            pass
    assert closed.value.code == 1008
    with client.websocket_connect("/ws", headers=gateway.headers("ui")) as socket:
        assert socket.receive_text() == "open"


def test_a_websocket_on_the_health_path_gets_no_exemption(gateway: FakeGateway) -> None:
    """Exempt means GET /health over HTTP, not every scope type at that path (recheck L4)."""
    _, client = build(gateway)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/health"):
            pass


def test_install_refuses_an_unknown_audience() -> None:
    with pytest.raises(ValueError):
        identity.install(FastAPI(), "gateway")


def test_install_twice_is_refused() -> None:
    app = FastAPI()
    identity.install(app, "stt")
    with pytest.raises(RuntimeError):
        identity.install(app, "stt")


def test_refusals_become_one_aggregated_audit_line_without_the_token(
        gateway: FakeGateway, capsys: pytest.CaptureFixture[str]) -> None:
    app, client = build(gateway)
    wrong = gateway.assertion("stt")
    for _ in range(3):
        client.get("/echo", headers={ASSERTION_HEADER: wrong})
    app.state.voice_common_identity.failures.flush()
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.startswith("audit ")]
    assert len(lines) == 1
    row = json.loads(lines[0][len("audit "):])
    assert row["action"] == "assertion_rejected" and row["outcome"] == "denied"
    assert row["aggregated"] == 1 and row["target"] == "ui"
    assert row["detail"]["reason"] == "wrong_audience"
    assert row["detail"]["count"] == 3 and row["detail"]["paths"] == ["/echo"]
    assert wrong not in lines[0] and wrong.split(".")[2] not in lines[0]


def test_a_closed_minute_is_written_when_the_next_refusal_arrives(
        capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    failures = identity._Failures("stt", clock=clock)
    failures.record("missing", "10.0.0.1", "/a")
    assert "audit " not in capsys.readouterr().out
    clock.now += 60
    failures.record("missing", "10.0.0.1", "/b")
    out = capsys.readouterr().out
    assert out.count("audit ") == 1 and '"/a"' in out and '"/b"' not in out


def test_the_refusal_log_is_bounded_however_many_peers_send(
        capsys: pytest.CaptureFixture[str]) -> None:
    """Every address on the internet can reach the gateway's door (recheck M-4)."""
    failures = identity._Failures("stt", clock=Clock())
    for n in range(20_000):
        failures.record("missing", f"10.{n // 65536}.{n // 256 % 256}.{n % 256}",
                        f"/path/{n}")
    assert len(failures.windows) <= identity.MAX_FAILURE_KEYS + 1
    assert all(len(entry["paths"]) <= identity.MAX_PATHS
               for entry in failures.windows.values())
    failures.flush()
    rows = [json.loads(line[6:]) for line in capsys.readouterr().out.splitlines()]
    assert sum(row["detail"]["count"] for row in rows) == 20_000


# ── what a handler uses ──────────────────────────────────────────────────────

def claims(*scopes: str) -> Claims:
    return Claims(iss="calliope-gateway", aud="stt", sub=USER, kind="user",
                  scopes=frozenset(scopes), cred="session", iat=NOW,
                  exp=NOW + 60, jti="abcdefghijklmnop")


def test_has_treats_all_as_implying_own() -> None:
    assert identity.has(claims("jobs:read:all"), "jobs:read:own")
    assert identity.has(claims("jobs:read:all"), "jobs:read:all")
    assert not identity.has(claims("jobs:read:own"), "jobs:read:all")
    assert not identity.has(claims("jobs:read:all"), "jobs:delete:own")


def test_require_answers_403_naming_the_scope(gateway: FakeGateway) -> None:
    app = FastAPI()
    install_errors(app)

    @app.post("/runs")
    async def runs(request: Request) -> dict[str, str]:
        return {"sub": identity.require(request, "runs:write").sub}

    identity.install(app, "tts-long")
    client = TestClient(app, raise_server_exceptions=False)
    refused = client.post("/runs", headers=gateway.headers("tts-long",
                                                           scopes=["jobs:read:own"]))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_scope"
    assert 'scope="runs:write"' in refused.headers["WWW-Authenticate"]
    allowed = client.post("/runs", headers=gateway.headers(
        "tts-long", sub="svc:stt", kind="service"))
    assert allowed.json() == {"sub": "svc:stt"}


def test_outbound_headers_are_built_from_named_values_only() -> None:
    assert identity.outbound_headers(content_type="application/json",
                                     authorization="Bearer k",
                                     x_calliope_delegation="v1.x",
                                     user_agent=None) == {
        "Content-Type": "application/json", "Authorization": "Bearer k",
        "X-Calliope-Delegation": "v1.x"}


@pytest.mark.parametrize("explicit", [
    {"user-agent": "copied from an inbound request"},
    {"x_calliope_identity": "v1.1.a.b"},
    {"cookie": "__Host-calliope_session=abc"},
    {"authorization": "Bearer k\r\nX-Injected: 1"},
])
def test_outbound_headers_refuse_what_must_never_travel(explicit: dict) -> None:
    with pytest.raises(ValueError):
        identity.outbound_headers(**explicit)
